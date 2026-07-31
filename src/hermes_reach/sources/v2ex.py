"""Production binding for fork-owned Agent-Reach V2EX execution."""

from __future__ import annotations

import asyncio
import os
import signal
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Final, cast

from ..runtime.adapters import AdapterResult, FailureClass, ItemKind, RawItem
from ..runtime.policy import AuthorizedCall
from ._worker_cleanup import cleanup_worker_resources
from .v2ex_worker import (
    MAX_OUTPUT_BYTES,
    ForkExecutionFailure,
    V2exItemProjection,
    V2exProfileProjection,
    V2exProjection,
    V2exProtocolError,
    V2exReplyProjection,
    V2exTopicProjection,
    WorkerErrorCode,
    WorkerOperation,
    decode_response,
    encode_request,
)

_WORKER_MODULE: Final = "hermes_reach.sources.v2ex_worker"
_TRANSIENT_FORK_ERRORS: Final = frozenset(
    {
        "cancelled",
        "deadline_exceeded",
        "transient",
    }
)
_PRODUCT_KINDS: Final[Mapping[str, ItemKind]] = {
    "v2ex.topic.v1": "topic",
    "v2ex.reply.v1": "reply",
    "v2ex.profile.v1": "profile",
}


class V2exWorkerError(Exception):
    """A classified worker failure containing no request or backend text."""

    def __init__(self, failure_class: FailureClass) -> None:
        super().__init__("v2ex_worker_failed")
        self.failure_class = failure_class


class V2exWorker:
    """Execute the fixed Agent-Reach V2EX worker with isolated authority."""

    async def execute(
        self,
        operation: WorkerOperation,
        *,
        node: str | None = None,
        page: int | None = None,
        limit: int | None = None,
        topic_id: str | None = None,
        username: str | None = None,
    ) -> V2exProjection:
        try:
            request = encode_request(
                operation,
                node=node,
                page=page,
                limit=limit,
                topic_id=topic_id,
                username=username,
            )
        except V2exProtocolError:
            raise V2exWorkerError("permanent") from None
        if not os.path.isabs(sys.executable):
            raise V2exWorkerError("permanent")

        process: asyncio.subprocess.Process | None = None
        temporary: tempfile.TemporaryDirectory[str] | None = None
        response_validated = False
        try:
            temporary = tempfile.TemporaryDirectory(prefix="hermes-reach-v2ex-")
            root = Path(temporary.name)
            environment = _isolated_environment(root)
            try:
                process = await asyncio.create_subprocess_exec(
                    sys.executable,
                    "-I",
                    "-m",
                    _WORKER_MODULE,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                    cwd=str(root),
                    env=environment,
                    close_fds=True,
                    start_new_session=True,
                )
            except asyncio.CancelledError:
                raise
            except (OSError, ValueError):
                raise V2exWorkerError("transient") from None
            output = await _exchange_bounded(process, request)
            if process.returncode != 0:
                raise V2exWorkerError("transient")
            try:
                response = decode_response(
                    output,
                    operation=operation,
                    node=node,
                    page=page,
                    limit=limit,
                    topic_id=topic_id,
                    username=username,
                )
            except V2exProtocolError:
                raise V2exWorkerError("permanent") from None
            if isinstance(response, ForkExecutionFailure):
                raise V2exWorkerError(_fork_failure_class(response.error_code))
            response_validated = True
            return response
        except asyncio.CancelledError:
            raise
        except V2exWorkerError:
            raise
        except (BrokenPipeError, ChildProcessError, ConnectionError, OSError):
            raise V2exWorkerError("transient") from None
        except Exception:
            raise V2exWorkerError("transient") from None
        finally:
            await cleanup_worker_resources(
                (
                    _cleanup_process_group(
                        process,
                        terminate_group=not response_validated,
                    )
                    if process is not None
                    else None
                ),
                temporary,
            )


class V2exAdapter:
    """Map closed fork V2EX items into the stable Reach product contract."""

    def __init__(self, worker: V2exWorker | None = None) -> None:
        self._worker = worker if worker is not None else V2exWorker()

    async def execute(self, authorized: AuthorizedCall) -> AdapterResult:
        try:
            operation = _validated_operation(authorized)
            if operation == "browse.hot":
                projection = await self._worker.execute(
                    operation,
                    limit=_integer_option(
                        authorized.call.options,
                        "limit",
                        authorized.operation.runtime.maximum_items,
                        maximum=50,
                    ),
                )
            elif operation == "browse.node_topics":
                projection = await self._worker.execute(
                    operation,
                    node=_identifier_option(authorized.call.options, "node"),
                    page=_integer_option(
                        authorized.call.options,
                        "page",
                        1,
                        maximum=100,
                    ),
                    limit=_integer_option(
                        authorized.call.options,
                        "limit",
                        authorized.operation.runtime.maximum_items,
                        maximum=50,
                    ),
                )
            elif operation == "read.topic":
                projection = await self._worker.execute(
                    operation,
                    topic_id=_native_id(authorized),
                )
            elif operation == "read.user":
                projection = await self._worker.execute(
                    operation,
                    username=_native_id(authorized),
                )
            else:
                return AdapterResult(failure_class="invalid_input")
            return _project_fork_result(operation, projection)
        except asyncio.CancelledError:
            raise
        except V2exWorkerError as error:
            return AdapterResult(failure_class=error.failure_class)
        except (AttributeError, V2exProtocolError, TypeError, ValueError):
            return AdapterResult(failure_class="permanent")
        except Exception:
            return AdapterResult(failure_class="transient")


def _validated_operation(authorized: AuthorizedCall) -> WorkerOperation:
    operation = authorized.operation.name
    call = authorized.call
    if (
        call.source.name != "v2ex"
        or authorized.operation.source != "v2ex"
        or call.operation != authorized.operation
        or call.query is not None
        or operation
        not in {"browse.hot", "browse.node_topics", "read.topic", "read.user"}
    ):
        raise V2exProtocolError("adapter_request_invalid")
    if operation in {"browse.hot", "browse.node_topics"}:
        if call.target is not None:
            raise V2exProtocolError("adapter_request_invalid")
        expected_options = (
            {"limit"} if operation == "browse.hot" else {"node", "page", "limit"}
        )
        if not set(call.options).issubset(expected_options) or (
            operation == "browse.node_topics" and "node" not in call.options
        ):
            raise V2exProtocolError("adapter_request_invalid")
    elif call.options or call.target is None or set(call.target) != {"native_id"}:
        raise V2exProtocolError("adapter_request_invalid")
    return cast(WorkerOperation, operation)


def _project_fork_result(
    operation: WorkerOperation,
    projection: V2exProjection,
) -> AdapterResult:
    if projection.operation != operation:
        raise V2exProtocolError("worker_response_invalid")
    partial_failure = (
        None
        if projection.partial_error_code is None
        else _partial_failure_class(projection.partial_error_code)
    )
    return AdapterResult(
        tuple(_project_item(item) for item in projection.items),
        partial_failure_class=partial_failure,
        truncated=projection.truncated,
    )


def _project_item(item: V2exItemProjection) -> RawItem:
    kind = _PRODUCT_KINDS.get(item.schema_id)
    if kind is None:
        raise V2exProtocolError("worker_response_invalid")
    if item.schema_id == "v2ex.topic.v1":
        if not isinstance(item, V2exTopicProjection):
            raise V2exProtocolError("worker_response_invalid")
        return RawItem(
            text=item.text or "",
            native_id=item.native_id,
            kind=kind,
            title=item.title,
            url=item.url,
            author=item.author,
            published_at=item.published_at,
        )
    if item.schema_id == "v2ex.reply.v1":
        if not isinstance(item, V2exReplyProjection):
            raise V2exProtocolError("worker_response_invalid")
        return RawItem(
            text=item.text,
            native_id=item.native_id,
            kind=kind,
            url=item.url,
            author=item.author,
            published_at=item.published_at,
        )
    if not isinstance(item, V2exProfileProjection):
        raise V2exProtocolError("worker_response_invalid")
    return RawItem(
        text=item.text or "",
        native_id=item.native_id,
        kind=kind,
        title=item.title,
        url=item.url,
        published_at=item.published_at,
    )


def _native_id(authorized: AuthorizedCall) -> str:
    target = authorized.call.target
    if target is None or set(target) != {"native_id"}:
        raise V2exProtocolError("adapter_request_invalid")
    value = target["native_id"]
    if type(value) is not str:
        raise V2exProtocolError("adapter_request_invalid")
    return value


def _identifier_option(options: Mapping[str, object], name: str) -> str:
    value = options.get(name)
    if type(value) is not str:
        raise V2exProtocolError("adapter_request_invalid")
    return value


def _integer_option(
    options: Mapping[str, object],
    name: str,
    default: int,
    *,
    maximum: int,
) -> int:
    value = options.get(name, default)
    if type(value) is not int or not 1 <= value <= maximum:
        raise V2exProtocolError("adapter_request_invalid")
    return value


def _fork_failure_class(error_code: WorkerErrorCode) -> FailureClass:
    if error_code == "invalid_input":
        return "invalid_input"
    if error_code == "not_found":
        return "not_found"
    if error_code == "authentication":
        return "authentication"
    if error_code == "authorization":
        return "authorization"
    if error_code == "rate_limit":
        return "rate_limit"
    if error_code in _TRANSIENT_FORK_ERRORS:
        return "transient"
    return "permanent"


def _partial_failure_class(error_code: WorkerErrorCode) -> FailureClass:
    if error_code == "not_found":
        return "not_found"
    if error_code == "authentication":
        return "authentication"
    if error_code == "authorization":
        return "authorization"
    if error_code == "rate_limit":
        return "rate_limit"
    if error_code == "transient":
        return "transient"
    return "permanent"


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
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "TZ": "UTC",
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
    process: asyncio.subprocess.Process,
    request: bytes,
) -> bytes:
    writer = process.stdin
    reader = process.stdout
    if writer is None or reader is None:
        raise V2exWorkerError("transient")
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
                raise V2exWorkerError("permanent")
        await process.wait()
        return bytes(output)
    finally:
        output[:] = b"\x00" * len(output)


async def _cleanup_process_group(
    process: asyncio.subprocess.Process,
    *,
    terminate_group: bool,
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
