"""Exact trusted-device OpenCLI execution for one Reddit read operation."""

from __future__ import annotations

import asyncio
import os
import re
import signal
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Final, Protocol, cast

import yaml

from ..connector.authority import AuthorizedExecution
from ..connector.errors import ConnectorError, ConnectorErrorCode
from ..connector.execution import ExecutorEnvironment
from ..connector.protocol import (
    OperationResultItemV1,
    OperationResultV1,
    ProtocolValidationError,
)
from ..contracts import operation_call_is_valid, reddit_post_id_from_url
from .documents import normalize_whitespace

_OPENCLI_READ_ARGUMENTS: Final[tuple[str, ...]] = (
    "reddit",
    "read",
    "--sort",
    "best",
    "--limit",
    "3",
    "--depth",
    "2",
    "--replies",
    "2",
    "--max-length",
    "800",
    "-f",
    "yaml",
)
_ROW_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "type",
        "author",
        "score",
        "text",
        "post_hint",
        "url_overridden_by_dest",
        "preview_image_url",
        "gallery_urls",
    }
)
_MAX_OUTPUT_BYTES: Final = 65_536
_MAX_RESULT_ITEMS: Final = 14
_MAX_TEXT_CHARACTERS: Final = 1_000
_MAX_TITLE_CHARACTERS: Final = 512
_MAX_AUTHOR_CHARACTERS: Final = 64
_OPENCLI_POST_ID: Final = re.compile(r"[a-z0-9]{1,32}")
_COMMENT_LEVEL: Final = re.compile(r"L[0-9]+")
_LOCALE_ENVIRONMENT_NAMES: Final[tuple[str, ...]] = (
    "LANG",
    "LC_ALL",
    "TMPDIR",
    "TZ",
)
MonotonicClock = Callable[[], float]
WallClock = Callable[[], float]


class OpenCliProcess(Protocol):
    """Trusted-local process boundary for the fixed OpenCLI argv."""

    async def run(self, argv: tuple[str, ...], *, deadline: float) -> str: ...


class OpenCliSubprocess:
    """Run an explicit local OpenCLI binary without ambient secret environment."""

    __slots__ = ("_clock", "_environment", "_executable")

    def __init__(
        self,
        executable: Path,
        *,
        environment: Mapping[str, str],
        clock: MonotonicClock = time.monotonic,
    ) -> None:
        if (
            not isinstance(executable, Path)
            or not executable.is_absolute()
            or not callable(clock)
        ):
            raise ValueError("The OpenCLI process configuration is invalid.")
        self._executable = executable
        self._environment = _minimal_opencli_environment(environment)
        self._clock = clock

    async def run(self, argv: tuple[str, ...], *, deadline: float) -> str:
        """Run only a fixed argument vector and return bounded UTF-8 stdout."""

        if not _is_fixed_opencli_read_argv(argv):
            raise ConnectorError(ConnectorErrorCode.CONNECTOR_STATE_INVALID)
        remaining = deadline - self._clock()
        if remaining <= 0:
            raise ConnectorError(ConnectorErrorCode.CONNECTOR_DEADLINE_EXCEEDED)
        process: asyncio.subprocess.Process | None = None
        try:
            async with asyncio.timeout(remaining):
                try:
                    process = await asyncio.create_subprocess_exec(
                        str(self._executable),
                        *argv,
                        stdin=asyncio.subprocess.DEVNULL,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.DEVNULL,
                        cwd=self._environment["HOME"],
                        env=dict(self._environment),
                        close_fds=True,
                        start_new_session=True,
                    )
                except (OSError, ValueError):
                    raise ConnectorError(
                        ConnectorErrorCode.CONNECTOR_STATE_INVALID
                    ) from None
                stdout = await _read_stdout_bounded(process)
                if process.returncode != 0:
                    raise ConnectorError(ConnectorErrorCode.CONNECTOR_STATE_INVALID)
                return stdout.decode("utf-8", errors="strict")
        except TimeoutError:
            raise ConnectorError(
                ConnectorErrorCode.CONNECTOR_DEADLINE_EXCEEDED
            ) from None
        except UnicodeError:
            raise ConnectorError(ConnectorErrorCode.CONNECTOR_STATE_INVALID) from None
        except asyncio.CancelledError:
            raise
        except ConnectorError:
            raise
        except (BrokenPipeError, ChildProcessError, ConnectionError, OSError):
            raise ConnectorError(ConnectorErrorCode.CONNECTOR_STATE_INVALID) from None
        except Exception:
            raise ConnectorError(ConnectorErrorCode.CONNECTOR_STATE_INVALID) from None
        finally:
            if process is not None:
                await _kill_process_group(process)


class OpenCliRedditReadExecutor:
    """Execute only a bounded Reddit post read through an injected local process."""

    __slots__ = ("_monotonic_clock", "_process", "_wall_clock")

    def __init__(
        self,
        process: OpenCliProcess,
        *,
        wall_clock: WallClock = time.time,
        monotonic_clock: MonotonicClock = time.monotonic,
    ) -> None:
        if (
            not callable(getattr(process, "run", None))
            or not callable(wall_clock)
            or not callable(monotonic_clock)
        ):
            raise TypeError("The Reddit executor configuration is invalid.")
        self._process = process
        self._wall_clock = wall_clock
        self._monotonic_clock = monotonic_clock

    async def execute(
        self,
        execution: AuthorizedExecution,
        environment: ExecutorEnvironment,
    ) -> OperationResultV1:
        """Map the exact read operation without forwarding local configuration."""

        if not isinstance(execution, AuthorizedExecution) or dict(environment):
            raise ConnectorError(ConnectorErrorCode.CONNECTOR_STATE_INVALID)
        call = execution.operation_call()
        target = call.target
        if (
            not operation_call_is_valid(call)
            or call.source.name != "reddit"
            or call.operation.name != "read.post"
            or dict(call.options)
            or call.query is not None
            or target is None
            or set(target) != {"url"}
        ):
            raise ConnectorError(ConnectorErrorCode.CONNECTOR_STATE_INVALID)
        post_id = reddit_post_id_from_url(target["url"])
        if post_id is None:
            raise ConnectorError(ConnectorErrorCode.CONNECTOR_STATE_INVALID)
        remaining = execution.request.deadline - self._wall_clock()
        if remaining <= 0:
            raise ConnectorError(ConnectorErrorCode.CONNECTOR_DEADLINE_EXCEEDED)
        try:
            output = await self._process.run(
                _opencli_argv(post_id), deadline=self._monotonic_clock() + remaining
            )
        except (asyncio.CancelledError, ConnectorError):
            raise
        except Exception:
            raise ConnectorError(ConnectorErrorCode.CONNECTOR_STATE_INVALID) from None
        return _operation_result_from_yaml(output, post_id)


def _opencli_argv(post_id: str) -> tuple[str, ...]:
    return (
        _OPENCLI_READ_ARGUMENTS[0],
        _OPENCLI_READ_ARGUMENTS[1],
        post_id,
        *_OPENCLI_READ_ARGUMENTS[2:],
    )


def _is_fixed_opencli_read_argv(argv: object) -> bool:
    if (
        type(argv) is not tuple
        or len(argv) != len(_OPENCLI_READ_ARGUMENTS) + 1
        or argv[:2] != _OPENCLI_READ_ARGUMENTS[:2]
        or argv[3:] != _OPENCLI_READ_ARGUMENTS[2:]
    ):
        return False
    post_id = argv[2]
    return type(post_id) is str and _OPENCLI_POST_ID.fullmatch(post_id) is not None


def _operation_result_from_yaml(output: str, post_id: str) -> OperationResultV1:
    if type(output) is not str:
        raise ConnectorError(ConnectorErrorCode.CONNECTOR_STATE_INVALID)
    try:
        if len(output.encode("utf-8")) > _MAX_OUTPUT_BYTES:
            raise ConnectorError(ConnectorErrorCode.CONNECTOR_STATE_INVALID)
        loaded = yaml.safe_load(output)
    except (RecursionError, UnicodeError, yaml.YAMLError):
        raise ConnectorError(ConnectorErrorCode.CONNECTOR_STATE_INVALID) from None
    if not isinstance(loaded, list) or not 1 <= len(loaded) <= _MAX_RESULT_ITEMS:
        raise ConnectorError(ConnectorErrorCode.CONNECTOR_STATE_INVALID)
    try:
        items: list[OperationResultItemV1] = []
        for index, value in enumerate(loaded):
            row = _validated_row(value)
            row_type = cast(str, row["type"])
            text = _bounded_text(row["text"])
            if index == 0:
                if row_type != "POST":
                    raise ConnectorError(ConnectorErrorCode.CONNECTOR_STATE_INVALID)
                items.append(
                    OperationResultItemV1(
                        "content",
                        text,
                        native_id=post_id,
                        title=_title(cast(str, row["text"])),
                        url=f"https://www.reddit.com/comments/{post_id}",
                        author=_optional_author(row["author"]),
                    )
                )
                continue
            if not (row_type == "" or _COMMENT_LEVEL.fullmatch(row_type)):
                raise ConnectorError(ConnectorErrorCode.CONNECTOR_STATE_INVALID)
            items.append(
                OperationResultItemV1(
                    "reply", text, author=_optional_author(row["author"])
                )
            )
        return OperationResultV1(tuple(items), False)
    except ProtocolValidationError:
        raise ConnectorError(ConnectorErrorCode.CONNECTOR_STATE_INVALID) from None


def _validated_row(value: object) -> Mapping[str, object]:
    if not isinstance(value, dict) or set(value) != _ROW_FIELDS:
        raise ConnectorError(ConnectorErrorCode.CONNECTOR_STATE_INVALID)
    row = cast(dict[str, object], value)
    if (
        not all(
            type(row[name]) is str
            for name in (
                "type",
                "author",
                "text",
                "post_hint",
                "url_overridden_by_dest",
                "preview_image_url",
            )
        )
        or not (type(row["score"]) is int or row["score"] == "")
        or not isinstance(row["gallery_urls"], list)
        or not all(type(url) is str for url in row["gallery_urls"])
    ):
        raise ConnectorError(ConnectorErrorCode.CONNECTOR_STATE_INVALID)
    return row


def _bounded_text(value: object) -> str:
    if type(value) is not str or not value:
        raise ConnectorError(ConnectorErrorCode.CONNECTOR_STATE_INVALID)
    normalized = _normalized_result_string(value)
    if not normalized:
        raise ConnectorError(ConnectorErrorCode.CONNECTOR_STATE_INVALID)
    return normalized[:_MAX_TEXT_CHARACTERS]


def _title(text: str) -> str:
    title = _normalized_result_string(text.split("\n", maxsplit=1)[0])
    return title[:_MAX_TITLE_CHARACTERS] or "Reddit post"


def _optional_author(value: object) -> str | None:
    if type(value) is not str:
        raise ConnectorError(ConnectorErrorCode.CONNECTOR_STATE_INVALID)
    normalized = _normalized_result_string(value)
    return normalized[:_MAX_AUTHOR_CHARACTERS] or None


def _normalized_result_string(value: str) -> str:
    if any(
        character == "\x00" or 0xD800 <= ord(character) <= 0xDFFF for character in value
    ):
        raise ConnectorError(ConnectorErrorCode.CONNECTOR_STATE_INVALID)
    return normalize_whitespace(value)


def _minimal_opencli_environment(parent: Mapping[str, str]) -> dict[str, str]:
    home = parent.get("HOME")
    path = parent.get("PATH")
    if (
        type(home) is not str
        or not home
        or "\x00" in home
        or type(path) is not str
        or not path
        or "\x00" in path
    ):
        raise ValueError("The OpenCLI process configuration is invalid.")
    environment = {"HOME": home, "PATH": path, "NO_COLOR": "1"}
    for name in _LOCALE_ENVIRONMENT_NAMES:
        value = parent.get(name)
        if type(value) is str and value and "\x00" not in value:
            environment[name] = value
    return environment


async def _read_stdout_bounded(process: asyncio.subprocess.Process) -> bytes:
    reader = process.stdout
    if reader is None:
        raise ConnectorError(ConnectorErrorCode.CONNECTOR_STATE_INVALID)
    output = bytearray()
    while True:
        chunk = await reader.read(8192)
        if not chunk:
            break
        output.extend(chunk)
        if len(output) > _MAX_OUTPUT_BYTES:
            output[:] = b"\x00" * len(output)
            raise ConnectorError(ConnectorErrorCode.CONNECTOR_STATE_INVALID)
    await process.wait()
    return bytes(output)


async def _kill_process_group(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except OSError:
        if process.returncode is None:
            try:
                process.kill()
            except OSError:
                pass
    try:
        await process.wait()
    except (BrokenPipeError, ChildProcessError, ConnectionError, OSError):
        pass


__all__ = ["OpenCliProcess", "OpenCliRedditReadExecutor", "OpenCliSubprocess"]
