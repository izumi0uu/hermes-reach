"""Production binding for the exact Agent-Reach-selected Bilibili backend."""

from __future__ import annotations

import asyncio
import os
import re
import signal
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Final, cast

from ..agent_reach_bridge import BILIBILI_CLI_VERSION
from ..normalized import MAX_NORMALIZED_INTEGER
from ..runtime.adapters import (
    AdapterResult,
    FailureClass,
    ItemKind,
    MediaMetadata,
    RawItem,
)
from .bilibili_worker import (
    MAX_OUTPUT_BYTES,
    BilibiliProtocolError,
    WorkerOperation,
    decode_response,
    encode_request,
)
from .documents import normalize_whitespace
from .media import (
    BILIBILI_OPERATIONS,
    AuditedBilibiliBackend,
    MediaBackendAttestation,
)

_WORKER_MODULE: Final = "hermes_reach.sources.bilibili_worker"
_BVID: Final = re.compile(r"BV[A-Za-z0-9]{10}")
_ERROR_CLASSES: Final[Mapping[str, FailureClass]] = {
    "invalid_input": "invalid_input",
    "not_found": "not_found",
    "not_authenticated": "authentication",
    "permission_denied": "authorization",
    "rate_limited": "rate_limit",
    "network_error": "transient",
    "upstream_error": "permanent",
    "internal_error": "permanent",
}


class BilibiliWorkerError(Exception):
    """A classified worker failure containing no request or backend text."""

    def __init__(self, failure_class: FailureClass) -> None:
        super().__init__("bilibili_worker_failed")
        self.failure_class = failure_class


class BilibiliWorker:
    """Execute the fixed backend worker with isolated ambient authority."""

    async def execute(
        self,
        operation: WorkerOperation,
        *,
        query: str | None = None,
        url: str | None = None,
        limit: int | None = None,
    ) -> Mapping[str, object]:
        try:
            request = encode_request(operation, query=query, url=url, limit=limit)
        except BilibiliProtocolError:
            raise BilibiliWorkerError("permanent") from None

        process: asyncio.subprocess.Process | None = None
        temporary: tempfile.TemporaryDirectory[str] | None = None
        response_validated = False
        try:
            temporary = tempfile.TemporaryDirectory(prefix="hermes-reach-bilibili-")
            root = temporary.name
            environment = _isolated_environment(Path(root))
            try:
                process = await asyncio.create_subprocess_exec(
                    sys.executable,
                    "-I",
                    "-m",
                    _WORKER_MODULE,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                    cwd=root,
                    env=environment,
                    close_fds=True,
                    start_new_session=True,
                )
            except asyncio.CancelledError:
                raise
            except (OSError, ValueError):
                raise BilibiliWorkerError("transient") from None
            output = await _exchange_bounded(process, request)
            if process.returncode != 0:
                raise BilibiliWorkerError("transient")
            try:
                response = decode_response(output)
            except BilibiliProtocolError:
                raise BilibiliWorkerError("permanent") from None
            response_validated = True
            return response
        except asyncio.CancelledError:
            raise
        except BilibiliWorkerError:
            raise
        except (BrokenPipeError, ChildProcessError, ConnectionError, OSError):
            raise BilibiliWorkerError("transient") from None
        except Exception:
            raise BilibiliWorkerError("transient") from None
        finally:
            if process is not None:
                await _cleanup_process_group(
                    process, terminate_group=not response_validated
                )
            if temporary is not None:
                temporary.cleanup()


class ProductionBilibiliClient:
    """Project the pinned backend envelope into existing media adapter types."""

    def __init__(self, worker: BilibiliWorker | None = None) -> None:
        self._worker = worker if worker is not None else BilibiliWorker()

    async def search_videos(self, query: str, limit: int) -> AdapterResult:
        return await self._execute("search.videos", query=query, limit=limit)

    async def read_video(self, video_url: str) -> AdapterResult:
        return await self._execute("read.video", url=video_url)

    async def browse_hot(self, limit: int) -> AdapterResult:
        return await self._execute("browse.hot", limit=limit)

    async def browse_rank(self, limit: int) -> AdapterResult:
        return await self._execute("browse.rank", limit=limit)

    async def _execute(
        self,
        operation: WorkerOperation,
        *,
        query: str | None = None,
        url: str | None = None,
        limit: int | None = None,
    ) -> AdapterResult:
        try:
            envelope = await self._worker.execute(
                operation, query=query, url=url, limit=limit
            )
            return _project_envelope(operation, envelope, limit)
        except asyncio.CancelledError:
            raise
        except BilibiliWorkerError as error:
            return AdapterResult(failure_class=error.failure_class)
        except (BilibiliProtocolError, TypeError, ValueError):
            return AdapterResult(failure_class="permanent")
        except Exception:
            return AdapterResult(failure_class="transient")


def production_bilibili_backend() -> AuditedBilibiliBackend:
    """Construct the reviewed default bundle without probing the environment."""

    return AuditedBilibiliBackend(
        ProductionBilibiliClient(),
        MediaBackendAttestation(
            provider_id="bili-cli",
            provider_version=BILIBILI_CLI_VERSION,
            operations=BILIBILI_OPERATIONS,
            logs_queries=False,
            persists_content=False,
            hidden_model_processing=False,
            runtime_dependency_install=False,
            reads_ambient_configuration=False,
            imports_credentials=False,
            imports_cookies=False,
            uses_proxy=False,
            uses_browser=False,
            uses_shell=False,
            delegates_to_ytdlp=False,
        ),
    )


def _project_envelope(
    operation: WorkerOperation,
    envelope: Mapping[str, object],
    limit: int | None,
) -> AdapterResult:
    if envelope.get("ok") is False:
        error = envelope.get("error")
        if not isinstance(error, Mapping):
            raise BilibiliProtocolError("backend_error_invalid")
        code = error.get("code")
        failure_class = _ERROR_CLASSES.get(code) if isinstance(code, str) else None
        return AdapterResult(failure_class=failure_class or "permanent")
    if envelope.get("ok") is not True:
        raise BilibiliProtocolError("backend_envelope_invalid")
    data = envelope.get("data")
    if operation == "search.videos":
        items = _project_search_items(data, _required_limit(limit))
    elif operation == "read.video":
        items = (_project_video_command(data),)
    elif operation == "browse.hot":
        items = _project_listing(data, _required_limit(limit), page=1)
    elif operation == "browse.rank":
        items = _project_listing(data, _required_limit(limit), day=3)
    else:
        raise BilibiliProtocolError("backend_operation_invalid")
    return AdapterResult(items)


def _project_search_items(value: object, limit: int) -> tuple[RawItem, ...]:
    if not isinstance(value, list) or len(value) > limit:
        raise BilibiliProtocolError("backend_data_invalid")
    return tuple(_project_search_item(item) for item in value)


def _project_search_item(value: object) -> RawItem:
    item = _closed_mapping(value, {"id", "bvid", "title", "author", "play", "duration"})
    bvid = _bvid(item.get("bvid"))
    if item.get("id") != bvid:
        raise BilibiliProtocolError("backend_item_invalid")
    title = _required_text(item.get("title"), 4096)
    author = _optional_text(item.get("author"), 1024)
    view_count = _non_negative_integer(item.get("play"))
    duration = _duration_seconds(item.get("duration"))
    return RawItem(
        text=title,
        native_id=bvid,
        kind="result",
        title=title,
        url=_video_url(bvid),
        author=author,
        media=MediaMetadata(
            duration_seconds=duration,
            view_count=view_count,
            coverage="partial",
        ),
    )


def _project_video_command(value: object) -> RawItem:
    data = _closed_mapping(
        value,
        {"video", "subtitle", "ai_summary", "comments", "related", "warnings"},
    )
    subtitle = _closed_mapping(
        data.get("subtitle"), {"available", "format", "text", "items"}
    )
    if (
        subtitle != {"available": False, "format": "plain", "text": "", "items": []}
        or data.get("ai_summary") != ""
        or data.get("comments") != []
        or data.get("related") != []
        or data.get("warnings") != []
    ):
        raise BilibiliProtocolError("backend_optional_path_invalid")
    return _project_video_summary(data.get("video"), kind="content", complete=True)


def _project_listing(
    value: object,
    limit: int,
    *,
    page: int | None = None,
    day: int | None = None,
) -> tuple[RawItem, ...]:
    discriminator = "page" if page is not None else "day"
    expected = page if page is not None else day
    data = _closed_mapping(value, {"items", discriminator, "count"})
    items = data.get("items")
    if (
        data.get(discriminator) != expected
        or data.get("count") != limit
        or not isinstance(items, list)
        or len(items) > limit
    ):
        raise BilibiliProtocolError("backend_listing_invalid")
    return tuple(
        _project_video_summary(item, kind="entry", complete=False) for item in items
    )


def _project_video_summary(value: object, *, kind: ItemKind, complete: bool) -> RawItem:
    item = _closed_mapping(
        value,
        {
            "id",
            "bvid",
            "aid",
            "title",
            "description",
            "duration_seconds",
            "duration",
            "url",
            "owner",
            "stats",
        },
    )
    bvid = _bvid(item.get("bvid"))
    if item.get("id") != bvid or item.get("url") != _video_url(bvid):
        raise BilibiliProtocolError("backend_item_invalid")
    _non_negative_integer(item.get("aid"))
    title = _required_text(item.get("title"), 4096)
    description = _optional_text(item.get("description"), 64 * 1024)
    duration = _non_negative_integer(item.get("duration_seconds"))
    if _duration_seconds(item.get("duration")) != duration:
        raise BilibiliProtocolError("backend_item_invalid")
    owner = _closed_mapping(item.get("owner"), {"id", "name"})
    _optional_text(owner.get("id"), 64)
    author = _optional_text(owner.get("name"), 1024)
    stats = _closed_mapping(
        item.get("stats"),
        {"view", "danmaku", "like", "coin", "favorite", "share"},
    )
    view_count = _non_negative_integer(stats.get("view"))
    for name in ("danmaku", "like", "coin", "favorite", "share"):
        _non_negative_integer(stats.get(name))
    return RawItem(
        text=description or title,
        native_id=bvid,
        kind=kind,
        title=title,
        url=_video_url(bvid),
        author=author,
        media=MediaMetadata(
            duration_seconds=duration,
            view_count=view_count,
            coverage="complete" if complete else "partial",
        ),
    )


def _isolated_environment(root: Path) -> dict[str, str]:
    directories = {
        "HOME": root / "home",
        "XDG_CONFIG_HOME": root / "config",
        "XDG_CACHE_HOME": root / "cache",
        "XDG_DATA_HOME": root / "data",
        "TMPDIR": root / "tmp",
    }
    for directory in directories.values():
        directory.mkdir(mode=0o700)
    return {
        **{name: str(path) for name, path in directories.items()},
        "PYTHONUTF8": "1",
        "PYTHONNOUSERSITE": "1",
        "HTTP_PROXY": "",
        "HTTPS_PROXY": "",
        "ALL_PROXY": "",
        "NO_PROXY": "*",
        "http_proxy": "",
        "https_proxy": "",
        "all_proxy": "",
        "no_proxy": "*",
    }


async def _exchange_bounded(
    process: asyncio.subprocess.Process, request: bytes
) -> bytes:
    writer = process.stdin
    reader = process.stdout
    if writer is None or reader is None:
        raise BilibiliWorkerError("transient")
    writer.write(request)
    await writer.drain()
    writer.close()

    output = bytearray()
    try:
        while len(output) <= MAX_OUTPUT_BYTES + 4:
            chunk = await reader.read(min(8192, MAX_OUTPUT_BYTES + 5 - len(output)))
            if not chunk:
                break
            output.extend(chunk)
            if len(output) > MAX_OUTPUT_BYTES + 4:
                raise BilibiliWorkerError("permanent")
        await process.wait()
        return bytes(output)
    finally:
        output[:] = b"\x00" * len(output)


async def _cleanup_process_group(
    process: asyncio.subprocess.Process, *, terminate_group: bool
) -> None:
    if process.returncode is not None and not terminate_group:
        return
    if terminate_group:
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
    if process.returncode is not None:
        return
    try:
        await process.wait()
    except (BrokenPipeError, ChildProcessError, ConnectionError, OSError):
        pass


def _closed_mapping(value: object, fields: set[str]) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise BilibiliProtocolError("backend_data_invalid")
    if not all(isinstance(key, str) for key in value):
        raise BilibiliProtocolError("backend_data_invalid")
    return cast(Mapping[str, object], value)


def _required_text(value: object, maximum: int) -> str:
    text = _optional_text(value, maximum)
    if text is None:
        raise BilibiliProtocolError("backend_text_invalid")
    return text


def _optional_text(value: object, maximum: int) -> str | None:
    if not isinstance(value, str) or len(value) > maximum:
        raise BilibiliProtocolError("backend_text_invalid")
    normalized = normalize_whitespace(value)
    return normalized or None


def _bvid(value: object) -> str:
    if not isinstance(value, str) or _BVID.fullmatch(value) is None:
        raise BilibiliProtocolError("backend_bvid_invalid")
    return value


def _non_negative_integer(value: object) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 0 <= value <= MAX_NORMALIZED_INTEGER
    ):
        raise BilibiliProtocolError("backend_integer_invalid")
    return value


def _duration_seconds(value: object) -> int:
    if not isinstance(value, str) or not 1 <= len(value) <= 16:
        raise BilibiliProtocolError("backend_duration_invalid")
    parts = value.split(":")
    if len(parts) not in {2, 3} or any(
        not part.isascii() or not part.isdigit() for part in parts
    ):
        raise BilibiliProtocolError("backend_duration_invalid")
    numbers = [int(part) for part in parts]
    if any(number < 0 for number in numbers) or any(
        number >= 60 for number in numbers[-2:]
    ):
        raise BilibiliProtocolError("backend_duration_invalid")
    seconds = numbers[-1] + numbers[-2] * 60
    if len(numbers) == 3:
        seconds += numbers[0] * 3600
    return _non_negative_integer(seconds)


def _required_limit(value: int | None) -> int:
    if value is None:
        raise BilibiliProtocolError("backend_limit_invalid")
    return value


def _video_url(bvid: str) -> str:
    return f"https://www.bilibili.com/video/{bvid}"
