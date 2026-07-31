"""Production binding for fork-owned Agent-Reach Exa Web execution."""

from __future__ import annotations

import asyncio
import os
import signal
import sys
import tempfile
from pathlib import Path
from typing import Final, Protocol

from ..contracts import operation_call_is_valid
from ..runtime.adapters import AdapterBinding, AdapterResult, FailureClass, RawItem
from ..runtime.policy import AuthorizedCall
from ._worker_cleanup import cleanup_worker_resources
from .exa_artifacts import ExaArtifactAttestation
from .exa_worker import (
    EXPECTED_BACKEND_ID,
    EXPECTED_BACKEND_VERSION,
    MAX_LIMIT,
    MAX_OUTPUT_BYTES,
    ExaProjection,
    ExaProtocolError,
    ExaResultProjection,
    ForkExecutionFailure,
    WorkerErrorCode,
    decode_response,
    encode_request,
)

_WORKER_MODULE: Final = "hermes_reach.sources.exa_worker"
_PROXY_VARIABLES: Final = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "no_proxy",
)


class ExaWorkerError(Exception):
    """A classified worker failure containing no query or backend text."""

    def __init__(self, failure_class: FailureClass) -> None:
        super().__init__("exa_worker_failed")
        self.failure_class = failure_class


class ExaWorkerClient(Protocol):
    async def execute(self, query: str, limit: int) -> ExaProjection: ...


class ExaWorker:
    """Supervise one fixed isolated Agent-Reach Exa worker invocation."""

    def __init__(self, artifacts: ExaArtifactAttestation) -> None:
        if type(artifacts) is not ExaArtifactAttestation:
            raise ValueError("The Exa worker attestation is invalid.")
        self._artifacts = artifacts

    async def execute(self, query: str, limit: int) -> ExaProjection:
        try:
            request = bytearray(encode_request(query, limit, self._artifacts))
        except ExaProtocolError:
            raise ExaWorkerError("permanent") from None

        process: asyncio.subprocess.Process | None = None
        temporary: tempfile.TemporaryDirectory[str] | None = None
        response_validated = False
        try:
            if not os.path.isabs(sys.executable):
                raise ExaWorkerError("permanent")
            temporary = tempfile.TemporaryDirectory(prefix="hermes-reach-exa-")
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
                raise ExaWorkerError("transient") from None
            output = await _exchange_bounded(process, request)
            if process.returncode != 0:
                raise ExaWorkerError("transient")
            try:
                response = decode_response(output, limit=limit)
            except ExaProtocolError:
                raise ExaWorkerError("permanent") from None
            if isinstance(response, ForkExecutionFailure):
                raise ExaWorkerError(_fork_failure_class(response.error_code))
            if response.operation != "search.web":
                raise ExaWorkerError("permanent")
            response_validated = True
            return response
        except asyncio.CancelledError:
            raise
        except ExaWorkerError:
            raise
        except (BrokenPipeError, ChildProcessError, ConnectionError, OSError):
            raise ExaWorkerError("transient") from None
        except Exception:
            raise ExaWorkerError("transient") from None
        finally:
            request[:] = b"\x00" * len(request)
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


class ExaAdapter:
    """Map the single closed Exa worker result into Reach adapter items."""

    def __init__(
        self,
        artifacts: ExaArtifactAttestation,
        worker: ExaWorkerClient | None = None,
    ) -> None:
        if type(artifacts) is not ExaArtifactAttestation:
            raise ValueError("The Exa adapter attestation is invalid.")
        if worker is not None and not callable(getattr(worker, "execute", None)):
            raise ValueError("The Exa worker client is invalid.")
        self._worker = worker if worker is not None else ExaWorker(artifacts)

    async def execute(self, authorized: AuthorizedCall) -> AdapterResult:
        try:
            if not _valid_authorized_call(authorized):
                return AdapterResult(failure_class="invalid_input")
            query = authorized.call.query
            if type(query) is not str:
                return AdapterResult(failure_class="invalid_input")
            limit_value = authorized.call.options.get(
                "limit",
                authorized.operation.runtime.maximum_items,
            )
            if type(limit_value) is not int or not 1 <= limit_value <= MAX_LIMIT:
                return AdapterResult(failure_class="invalid_input")
            projection = await self._worker.execute(query, limit_value)
            return _project_result(projection)
        except asyncio.CancelledError:
            raise
        except ExaWorkerError as error:
            return AdapterResult(failure_class=error.failure_class)
        except (ExaProtocolError, TypeError, ValueError):
            return AdapterResult(failure_class="permanent")
        except Exception:
            return AdapterResult(failure_class="transient")


def production_exa_binding(
    artifacts: ExaArtifactAttestation,
    *,
    worker: ExaWorkerClient | None = None,
) -> AdapterBinding:
    """Create the sole Exa binding without probing artifacts or the provider."""

    adapter = ExaAdapter(artifacts, worker)
    return AdapterBinding(
        source="exa",
        operation="search.web",
        backend_id=EXPECTED_BACKEND_ID,
        backend_version=EXPECTED_BACKEND_VERSION,
        priority=10,
        required_scope="public",
        equivalence_group="exa:search.web:v1",
        execute=adapter.execute,
        retry_owner="binding",
    )


def exa_bindings(
    artifacts: ExaArtifactAttestation,
    *,
    worker: ExaWorkerClient | None = None,
) -> tuple[AdapterBinding, ...]:
    """Return the exact one-operation binding tuple used by registry composition."""

    return (production_exa_binding(artifacts, worker=worker),)


def _valid_authorized_call(authorized: object) -> bool:
    if type(authorized) is not AuthorizedCall:
        return False
    call = authorized.call
    return bool(
        call.source.name == "exa"
        and call.operation.source == "exa"
        and call.operation.name == "search.web"
        and call.operation.tool == "search"
        and call.target is None
        and operation_call_is_valid(call)
    )


def _project_result(projection: object) -> AdapterResult:
    if type(projection) is not ExaProjection or projection.operation != "search.web":
        raise ExaProtocolError("worker_response_invalid")
    return AdapterResult(
        tuple(_project_item(item) for item in projection.items),
        truncated=projection.truncated,
    )


def _project_item(item: ExaResultProjection) -> RawItem:
    if type(item) is not ExaResultProjection:
        raise ExaProtocolError("worker_response_invalid")
    return RawItem(
        text=item.text,
        kind="result",
        title=item.title,
        url=item.url,
        author=item.author,
        published_at=item.published_at,
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
    if error_code in {"backend_unavailable", "backend_incompatible"}:
        return "setup_required"
    if error_code in {"transient", "deadline_exceeded", "cancelled"}:
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
    environment = {
        **{name: str(path) for name, path in directories.items()},
        "PYTHONUTF8": "1",
        "PYTHONNOUSERSITE": "1",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "TZ": "UTC",
    }
    environment.update({name: "" for name in _PROXY_VARIABLES})
    return environment


async def _exchange_bounded(
    process: asyncio.subprocess.Process,
    request: bytearray,
) -> bytes:
    writer = process.stdin
    reader = process.stdout
    if writer is None or reader is None:
        raise ExaWorkerError("transient")

    try:
        writer.write(request)
        await writer.drain()
    finally:
        writer.close()

    output = bytearray()
    try:
        while len(output) <= MAX_OUTPUT_BYTES + 4:
            chunk = await reader.read(min(8_192, MAX_OUTPUT_BYTES + 5 - len(output)))
            if not chunk:
                break
            output.extend(chunk)
            if len(output) > MAX_OUTPUT_BYTES + 4:
                raise ExaWorkerError("permanent")
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


__all__ = [
    "ExaAdapter",
    "ExaWorker",
    "ExaWorkerClient",
    "ExaWorkerError",
    "exa_bindings",
    "production_exa_binding",
]
