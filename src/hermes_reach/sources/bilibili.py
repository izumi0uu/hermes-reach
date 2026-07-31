"""Production binding for fork-owned Agent-Reach Bilibili execution."""

from __future__ import annotations

import asyncio
import os
import signal
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Final

from ..agent_reach_bridge import BILIBILI_CLI_VERSION
from ..runtime.adapters import (
    AdapterResult,
    FailureClass,
    ItemKind,
    MediaCoverage,
    MediaMetadata,
    RawItem,
)
from ._worker_cleanup import cleanup_worker_resources
from .bilibili_worker import (
    MAX_OUTPUT_BYTES,
    BilibiliProjection,
    BilibiliProtocolError,
    BilibiliVideoProjection,
    ForkExecutionFailure,
    WorkerErrorCode,
    WorkerOperation,
    decode_response,
    encode_request,
)
from .media import (
    BILIBILI_OPERATIONS,
    AuditedBilibiliBackend,
    MediaBackendAttestation,
)

_WORKER_MODULE: Final = "hermes_reach.sources.bilibili_worker"
_TRANSIENT_FORK_ERRORS: Final = frozenset(
    {
        "backend_incompatible",
        "backend_contract_violation",
        "backend_unavailable",
        "cancelled",
        "deadline_exceeded",
        "transient",
    }
)
_PRODUCT_SEMANTICS: Final[Mapping[WorkerOperation, tuple[ItemKind, MediaCoverage]]] = {
    "search.videos": ("result", "partial"),
    "read.video": ("content", "complete"),
    "browse.hot": ("entry", "partial"),
    "browse.rank": ("entry", "partial"),
}


class BilibiliWorkerError(Exception):
    """A classified worker failure containing no request or backend text."""

    def __init__(self, failure_class: FailureClass) -> None:
        super().__init__("bilibili_worker_failed")
        self.failure_class = failure_class


class BilibiliWorker:
    """Execute the fixed Agent-Reach worker with isolated ambient authority."""

    async def execute(
        self,
        operation: WorkerOperation,
        *,
        query: str | None = None,
        url: str | None = None,
        limit: int | None = None,
    ) -> BilibiliProjection:
        try:
            request = encode_request(operation, query=query, url=url, limit=limit)
        except BilibiliProtocolError:
            raise BilibiliWorkerError("permanent") from None
        if not os.path.isabs(sys.executable):
            raise BilibiliWorkerError("permanent")

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
                response = decode_response(
                    output,
                    operation=operation,
                    limit=limit,
                )
            except BilibiliProtocolError:
                raise BilibiliWorkerError("permanent") from None
            if isinstance(response, ForkExecutionFailure):
                raise BilibiliWorkerError(_fork_failure_class(response.error_code))
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


class ProductionBilibiliClient:
    """Map closed fork items into the existing media adapter types."""

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
            projection = await self._worker.execute(
                operation,
                query=query,
                url=url,
                limit=limit,
            )
            return _project_fork_result(operation, projection)
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


def _project_fork_result(
    operation: WorkerOperation,
    projection: BilibiliProjection,
) -> AdapterResult:
    if projection.operation != operation:
        raise BilibiliProtocolError("worker_response_invalid")
    kind, coverage = _PRODUCT_SEMANTICS[operation]
    return AdapterResult(
        tuple(
            _project_item(item, kind=kind, coverage=coverage)
            for item in projection.items
        ),
        truncated=projection.truncated,
    )


def _project_item(
    item: BilibiliVideoProjection,
    *,
    kind: ItemKind,
    coverage: MediaCoverage,
) -> RawItem:
    return RawItem(
        text=item.text,
        native_id=item.native_id,
        kind=kind,
        title=item.title,
        url=item.url,
        author=item.author,
        media=MediaMetadata(
            duration_seconds=item.duration_seconds,
            view_count=item.view_count,
            coverage=coverage,
        ),
    )


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
    process: asyncio.subprocess.Process,
    request: bytes,
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
