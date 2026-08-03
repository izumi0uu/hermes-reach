"""Connector-only binding for fork-owned Agent-Reach Xueqiu execution."""

from __future__ import annotations

import asyncio
import os
import signal
import sys
import tempfile
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Final, Protocol

from ..catalog import get_operation, get_source
from ..connector.authority import AuthorizedExecution
from ..connector.errors import ConnectorError, ConnectorErrorCode
from ..connector.execution import (
    ConnectorExecutionComposition,
    ConnectorExecutorBinding,
    ExecutorEnvironment,
    SecretExecutionPlan,
)
from ..connector.protocol import (
    GrantScope,
    OperationResultItemV1,
    OperationResultV1,
    ProtocolValidationError,
    PublicBackendIdentity,
)
from ..connector.secrets import CapabilityId, SecretBindingCatalog, SecretProvider
from ..contracts import operation_call_is_valid
from ._worker_cleanup import cleanup_worker_resources
from .xueqiu_worker import (
    MAX_OUTPUT_BYTES,
    ForkExecutionFailure,
    WorkerResponse,
    XueqiuProjection,
    XueqiuProtocolError,
    decode_response,
    encode_request,
)

XUEQIU_COOKIE_INJECTION_TARGET: Final = "HERMES_REACH_XUEQIU_COOKIE_HEADER"
XUEQIU_BACKEND: Final = PublicBackendIdentity("xueqiu-api", "1.5.0+search.v1")

_SOURCE: Final = "xueqiu"
_OPERATION: Final = "search.stocks"
_WORKER_MODULE: Final = "hermes_reach.sources.xueqiu_worker"
_WORKER_GRACEFUL_SHUTDOWN_SECONDS: Final = 3.0
_PUBLIC_NAME_CHARACTERS: Final = 512
_ERROR_CODE_MAP: Final[dict[str, ConnectorErrorCode]] = {
    "unsupported_protocol_version": ConnectorErrorCode.BACKEND_CONTRACT_VIOLATION,
    "invalid_request": ConnectorErrorCode.BACKEND_CONTRACT_VIOLATION,
    "unsupported_source": ConnectorErrorCode.BACKEND_CONTRACT_VIOLATION,
    "unsupported_operation": ConnectorErrorCode.BACKEND_CONTRACT_VIOLATION,
    "host_capability_missing": ConnectorErrorCode.BACKEND_CONTRACT_VIOLATION,
    "backend_unavailable": ConnectorErrorCode.BACKEND_UNAVAILABLE,
    "backend_incompatible": ConnectorErrorCode.BACKEND_INCOMPATIBLE,
    "deadline_exceeded": ConnectorErrorCode.BACKEND_DEADLINE_EXCEEDED,
    "cancelled": ConnectorErrorCode.BACKEND_DEADLINE_EXCEEDED,
    "invalid_input": ConnectorErrorCode.BACKEND_INVALID_INPUT,
    "not_found": ConnectorErrorCode.BACKEND_NOT_FOUND,
    "authentication": ConnectorErrorCode.BACKEND_AUTHENTICATION_REQUIRED,
    "authorization": ConnectorErrorCode.BACKEND_AUTHORIZATION_DENIED,
    "rate_limit": ConnectorErrorCode.BACKEND_RATE_LIMITED,
    "transient": ConnectorErrorCode.BACKEND_TRANSIENT,
    "permanent": ConnectorErrorCode.BACKEND_PERMANENT,
    "backend_contract_violation": ConnectorErrorCode.BACKEND_CONTRACT_VIOLATION,
}


class _WorkerClient(Protocol):
    async def execute(
        self,
        query: str,
        limit: int,
        cookie_header: str,
        *,
        deadline: float,
    ) -> WorkerResponse: ...


class _IsolatedXueqiuWorker:
    """Supervise one fixed secret-bearing Agent-Reach execution attempt."""

    async def execute(
        self,
        query: str,
        limit: int,
        cookie_header: str,
        *,
        deadline: float,
    ) -> WorkerResponse:
        try:
            request = encode_request(
                query,
                limit,
                cookie_header,
                deadline=deadline,
            )
        except XueqiuProtocolError:
            return ForkExecutionFailure("backend_contract_violation")

        process: asyncio.subprocess.Process | None = None
        temporary: tempfile.TemporaryDirectory[str] | None = None
        output = bytearray()
        response_validated = False
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return ForkExecutionFailure("deadline_exceeded")
            if not os.path.isabs(sys.executable):
                return ForkExecutionFailure("backend_incompatible")
            temporary = tempfile.TemporaryDirectory(prefix="hermes-reach-xueqiu-")
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
                return ForkExecutionFailure("backend_unavailable")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return ForkExecutionFailure("deadline_exceeded")
            try:
                async with asyncio.timeout(remaining):
                    output = await _exchange_bounded(process, request)
            except TimeoutError:
                return ForkExecutionFailure("deadline_exceeded")
            if process.returncode != 0:
                return ForkExecutionFailure("transient")
            try:
                response = decode_response(output, limit=limit)
            except XueqiuProtocolError:
                return ForkExecutionFailure("backend_contract_violation")
            response_validated = True
            return response
        except asyncio.CancelledError:
            raise
        except (BrokenPipeError, ChildProcessError, ConnectionError, OSError):
            return ForkExecutionFailure("transient")
        except Exception:
            return ForkExecutionFailure("backend_contract_violation")
        finally:
            _zero(request)
            _zero(output)
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


class XueqiuExecutor:
    """Map one authorized stock search into the isolated fork runtime."""

    __slots__ = ("_monotonic_clock", "_required_scope", "_wall_clock", "_worker")

    def __init__(
        self,
        required_scope: GrantScope,
        worker: _WorkerClient | None = None,
        *,
        wall_clock: Callable[[], float] = time.time,
        monotonic_clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if (
            not isinstance(required_scope, GrantScope)
            or required_scope.source != _SOURCE
            or required_scope.operation != _OPERATION
            or required_scope.capability_id is None
            or (worker is not None and not callable(getattr(worker, "execute", None)))
            or not callable(wall_clock)
            or not callable(monotonic_clock)
        ):
            raise TypeError("The Xueqiu executor configuration is invalid.")
        self._required_scope = required_scope
        self._worker = worker if worker is not None else _IsolatedXueqiuWorker()
        self._wall_clock = wall_clock
        self._monotonic_clock = monotonic_clock

    async def execute(
        self,
        execution: AuthorizedExecution,
        environment: ExecutorEnvironment,
    ) -> OperationResultV1:
        if not isinstance(execution, AuthorizedExecution):
            raise ConnectorError(ConnectorErrorCode.CONNECTOR_STATE_INVALID)
        call = execution.operation_call()
        if (
            execution.required_scope != self._required_scope
            or execution.request.source != _SOURCE
            or execution.request.operation != _OPERATION
            or call.source.name != _SOURCE
            or call.operation.name != _OPERATION
            or call.operation.source != _SOURCE
            or call.target is not None
            or type(call.query) is not str
            or not operation_call_is_valid(call)
            or not set(call.options).issubset({"limit"})
        ):
            raise ConnectorError(ConnectorErrorCode.BACKEND_INVALID_INPUT)
        requested_limit = call.options.get(
            "limit", call.operation.runtime.maximum_items
        )
        if type(requested_limit) is not int or not 1 <= requested_limit <= 50:
            raise ConnectorError(ConnectorErrorCode.BACKEND_INVALID_INPUT)
        effective_limit = min(
            requested_limit,
            call.operation.runtime.maximum_items,
            50,
        )
        remaining = execution.request.deadline - float(self._wall_clock())
        if remaining <= 0:
            raise ConnectorError(ConnectorErrorCode.CONNECTOR_DEADLINE_EXCEEDED)
        deadline = float(self._monotonic_clock()) + remaining
        cookie_header = _secret_from_environment(environment)
        try:
            response = await self._worker.execute(
                call.query,
                effective_limit,
                cookie_header,
                deadline=deadline,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            raise ConnectorError(
                ConnectorErrorCode.BACKEND_CONTRACT_VIOLATION
            ) from None
        if isinstance(response, XueqiuProjection):
            return _operation_result(
                response,
                force_truncated=requested_limit > effective_limit,
            )
        if (
            not isinstance(response, ForkExecutionFailure)
            or response.error_code not in _ERROR_CODE_MAP
        ):
            raise ConnectorError(ConnectorErrorCode.BACKEND_CONTRACT_VIOLATION)
        raise ConnectorError(_ERROR_CODE_MAP[response.error_code])

    def __repr__(self) -> str:
        return "XueqiuExecutor(<redacted>)"


def xueqiu_scope(capability_id: CapabilityId) -> GrantScope:
    """Return the exact opaque capability scope for stock search."""

    if not isinstance(capability_id, CapabilityId):
        raise TypeError("The Xueqiu capability is invalid.")
    return GrantScope(_SOURCE, _OPERATION, "public", capability_id.for_grant())


def xueqiu_execution_composition(
    capability_id: CapabilityId,
    catalog: SecretBindingCatalog,
    provider: SecretProvider,
) -> ConnectorExecutionComposition:
    """Compose one exact SecretProvider-backed Xueqiu binding."""

    if not isinstance(catalog, SecretBindingCatalog) or not callable(
        getattr(provider, "resolve", None)
    ):
        raise TypeError("The Xueqiu secret composition is invalid.")
    scope = xueqiu_scope(capability_id)
    try:
        binding = catalog.require_active(
            capability_id,
            source=_SOURCE,
            operation=_OPERATION,
        )
    except ConnectorError:
        raise ValueError("The Xueqiu secret composition is invalid.") from None
    if binding.injection_target != XUEQIU_COOKIE_INJECTION_TARGET:
        raise ValueError("The Xueqiu secret composition is invalid.")
    secret = SecretExecutionPlan(
        catalog,
        provider,
        XUEQIU_COOKIE_INJECTION_TARGET,
    )
    return ConnectorExecutionComposition(
        (
            ConnectorExecutorBinding(
                required_scope=scope,
                backend=XUEQIU_BACKEND,
                executor=XueqiuExecutor(scope),
                secret=secret,
            ),
        )
    )


def _secret_from_environment(environment: Mapping[str, str]) -> str:
    try:
        if not isinstance(environment, Mapping) or set(environment) != {
            XUEQIU_COOKIE_INJECTION_TARGET
        }:
            raise ConnectorError(ConnectorErrorCode.CONNECTOR_STATE_INVALID)
        value = environment[XUEQIU_COOKIE_INJECTION_TARGET]
    except ConnectorError:
        raise
    except Exception:
        raise ConnectorError(ConnectorErrorCode.CONNECTOR_STATE_INVALID) from None
    if type(value) is not str:
        raise ConnectorError(ConnectorErrorCode.CONNECTOR_STATE_INVALID)
    return value


def _operation_result(
    projection: XueqiuProjection,
    *,
    force_truncated: bool = False,
) -> OperationResultV1:
    try:
        if (
            type(projection) is not XueqiuProjection
            or type(force_truncated) is not bool
        ):
            raise ProtocolValidationError("The Xueqiu result is invalid.")
        items: list[OperationResultItemV1] = []
        normalized_truncated = False
        seen: set[str] = set()
        for stock in projection.items:
            if stock.symbol in seen:
                raise ProtocolValidationError("The Xueqiu result is invalid.")
            seen.add(stock.symbol)
            public_name = stock.name[:_PUBLIC_NAME_CHARACTERS]
            normalized_truncated = normalized_truncated or public_name != stock.name
            items.append(
                OperationResultItemV1(
                    "result",
                    (
                        f"{public_name} | "
                        f"Symbol: {stock.symbol} | "
                        f"Exchange: {stock.exchange}"
                    ),
                    native_id=stock.symbol,
                )
            )
        result = OperationResultV1(
            tuple(items),
            bool(projection.truncated or normalized_truncated or force_truncated),
        )
        source = get_source(_SOURCE)
        operation = get_operation(source, _OPERATION) if source is not None else None
        if (
            operation is None
            or len(result.items) > operation.runtime.maximum_items
            or result.character_count() > operation.runtime.maximum_characters
        ):
            raise ProtocolValidationError("The Xueqiu result exceeds its bounds.")
        return result
    except (AttributeError, ProtocolValidationError, TypeError, ValueError):
        raise ConnectorError(ConnectorErrorCode.BACKEND_CONTRACT_VIOLATION) from None


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
    request: bytearray,
) -> bytearray:
    writer = process.stdin
    reader = process.stdout
    if writer is None or reader is None:
        raise OSError("worker transport unavailable")
    try:
        writer.write(request)
        await writer.drain()
    finally:
        writer.close()
        _zero(request)
    output = bytearray()
    while len(output) <= MAX_OUTPUT_BYTES + 4:
        chunk = await reader.read(min(8_192, MAX_OUTPUT_BYTES + 5 - len(output)))
        if not chunk:
            break
        output.extend(chunk)
        if len(output) > MAX_OUTPUT_BYTES + 4:
            _zero(output)
            raise XueqiuProtocolError("worker_response_invalid")
    await process.wait()
    return output


async def _cleanup_process_group(
    process: asyncio.subprocess.Process,
    *,
    terminate_group: bool,
) -> None:
    if process.returncode is not None and not terminate_group:
        return
    if terminate_group:
        if process.returncode is None:
            try:
                process.terminate()
            except (ProcessLookupError, OSError):
                pass
            try:
                async with asyncio.timeout(_WORKER_GRACEFUL_SHUTDOWN_SECONDS):
                    await process.wait()
            except TimeoutError:
                pass
            except (BrokenPipeError, ChildProcessError, ConnectionError, OSError):
                pass
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
    if process.returncode is not None:
        return
    try:
        await process.wait()
    except (BrokenPipeError, ChildProcessError, ConnectionError, OSError):
        pass


def _zero(value: bytearray) -> None:
    value[:] = b"\x00" * len(value)


__all__ = [
    "XUEQIU_BACKEND",
    "XUEQIU_COOKIE_INJECTION_TARGET",
    "XueqiuExecutor",
    "xueqiu_execution_composition",
    "xueqiu_scope",
]
