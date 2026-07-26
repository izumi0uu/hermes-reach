"""Closed one-use execution composition for trusted Connector operations."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Protocol

from ..catalog import get_operation, get_source
from .authority import AuthorizedExecution
from .errors import ConnectorError, ConnectorErrorCode
from .protocol import (
    GrantScope,
    OperationResultV1,
    ProtocolValidationError,
    PublicBackendIdentity,
    canonical_operation_result_bytes,
)
from .secrets import (
    CapabilityId,
    SecretBindingCatalog,
    SecretProvider,
    prepare_secret_execution,
)

ExecutorEnvironment = Mapping[str, str]
ExecutorCleanup = Callable[[], Awaitable[None]]
_EMPTY_ENVIRONMENT: ExecutorEnvironment = MappingProxyType({})


class ConnectorExecutor(Protocol):
    """Exact injected executor; production supplies no implementation."""

    async def execute(
        self,
        execution: AuthorizedExecution,
        environment: ExecutorEnvironment,
    ) -> OperationResultV1: ...


@dataclass(frozen=True, slots=True, repr=False)
class SecretExecutionPlan:
    """Trusted-local secret composition for one exact executor binding."""

    catalog: SecretBindingCatalog = field(repr=False, compare=False)
    provider: SecretProvider = field(repr=False, compare=False)
    injection_target: str = field(repr=False)

    def validate_for(self, scope: GrantScope) -> None:
        if not isinstance(self.catalog, SecretBindingCatalog) or not callable(
            getattr(self.provider, "resolve", None)
        ):
            raise ValueError("The Connector secret execution plan is invalid.")
        try:
            capability_id = CapabilityId.parse(scope.capability_id)
            binding = self.catalog.require_active(
                capability_id,
                source=scope.source,
                operation=scope.operation,
            )
        except (ConnectorError, ValueError):
            raise ValueError(
                "The Connector secret execution plan does not match its scope."
            ) from None
        if binding.injection_target != self.injection_target:
            raise ValueError(
                "The Connector secret execution plan does not match its scope."
            )

    def __repr__(self) -> str:
        return "SecretExecutionPlan(<redacted>)"


@dataclass(frozen=True, slots=True, repr=False)
class ConnectorExecutorBinding:
    """One immutable exact operation-to-executor composition row."""

    required_scope: GrantScope
    backend: PublicBackendIdentity
    executor: ConnectorExecutor = field(repr=False, compare=False)
    cleanup: ExecutorCleanup | None = field(default=None, repr=False, compare=False)
    secret: SecretExecutionPlan | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if (
            not isinstance(self.required_scope, GrantScope)
            or not isinstance(self.backend, PublicBackendIdentity)
            or not callable(getattr(self.executor, "execute", None))
            or (self.cleanup is not None and not callable(self.cleanup))
            or (
                self.secret is not None
                and not isinstance(self.secret, SecretExecutionPlan)
            )
        ):
            raise ValueError("The Connector executor binding is invalid.")
        source = get_source(self.required_scope.source)
        operation = (
            get_operation(source, self.required_scope.operation)
            if source is not None
            else None
        )
        if operation is None or operation.tool == "status":
            raise ValueError("The Connector executor binding is invalid.")
        if (self.required_scope.capability_id is None) != (self.secret is None):
            raise ValueError("The Connector executor binding capability is invalid.")
        if self.secret is not None:
            self.secret.validate_for(self.required_scope)

    @property
    def source(self) -> str:
        return self.required_scope.source

    @property
    def operation(self) -> str:
        return self.required_scope.operation

    def __repr__(self) -> str:
        return (
            "ConnectorExecutorBinding("
            f"source={self.source!r}, operation={self.operation!r}, "
            f"backend={self.backend!r}, composition=<redacted>)"
        )


class ConnectorExecutionComposition:
    """Select one exact trusted binding without consulting request arguments."""

    __slots__ = ("_bindings",)

    def __init__(self, bindings: Sequence[ConnectorExecutorBinding] = ()) -> None:
        if isinstance(bindings, str | bytes | bytearray):
            raise ValueError("The Connector executor composition is invalid.")
        selected: dict[tuple[str, str], ConnectorExecutorBinding] = {}
        for binding in bindings:
            if not isinstance(binding, ConnectorExecutorBinding):
                raise ValueError("The Connector executor composition is invalid.")
            key = (binding.source, binding.operation)
            if key in selected:
                raise ValueError("The Connector executor composition has a duplicate.")
            selected[key] = binding
        self._bindings = MappingProxyType(selected)

    def required_scope(self, source: str, operation: str) -> GrantScope | None:
        binding = self._bindings.get((source, operation))
        return None if binding is None else binding.required_scope

    def prepare(
        self, execution: AuthorizedExecution, *, deadline: float
    ) -> PreparedConnectorExecution:
        if not isinstance(execution, AuthorizedExecution) or type(deadline) not in {
            int,
            float,
        }:
            raise TypeError("The Connector execution preparation is invalid.")
        binding = self._bindings.get(
            (execution.request.source, execution.request.operation)
        )
        if binding is not None and binding.required_scope != execution.required_scope:
            return PreparedConnectorExecution(
                execution,
                None,
                float(deadline),
                failure_code=ConnectorErrorCode.CONNECTOR_STATE_INVALID,
            )
        return PreparedConnectorExecution(
            execution,
            binding,
            float(deadline),
            failure_code=(
                ConnectorErrorCode.BACKEND_UNBOUND if binding is None else None
            ),
        )

    def __repr__(self) -> str:
        return f"ConnectorExecutionComposition(count={len(self._bindings)})"


class PreparedConnectorExecution:
    """One-use execution whose cancellation is bounded to one exact binding."""

    __slots__ = (
        "_binding",
        "_deadline",
        "_execution",
        "_failure_code",
        "_mutex",
        "_used",
    )

    def __init__(
        self,
        execution: AuthorizedExecution,
        binding: ConnectorExecutorBinding | None,
        deadline: float,
        *,
        failure_code: ConnectorErrorCode | None,
    ) -> None:
        self._execution = execution
        self._binding = binding
        self._deadline = deadline
        self._failure_code = failure_code
        self._mutex = threading.Lock()
        self._used = False

    @property
    def backend(self) -> PublicBackendIdentity | None:
        binding = self._binding
        return None if binding is None else binding.backend

    def close(self) -> None:
        """Abandon a preparation before execution; repeated close is harmless."""

        with self._mutex:
            self._used = True

    async def execute(self) -> OperationResultV1:
        with self._mutex:
            if self._used:
                raise ConnectorError(ConnectorErrorCode.BACKEND_UNBOUND)
            self._used = True
        failure_code = self._failure_code
        if failure_code is not None:
            raise ConnectorError(failure_code)
        binding = self._binding
        if binding is None:
            raise ConnectorError(ConnectorErrorCode.BACKEND_UNBOUND)
        try:
            async with asyncio.timeout_at(self._deadline):
                result = await self._execute_binding(binding)
            self._validate_result(result, binding)
            return result
        except asyncio.CancelledError:
            await _cleanup_binding(binding)
            raise
        except TimeoutError:
            await _cleanup_binding(binding)
            raise ConnectorError(
                ConnectorErrorCode.CONNECTOR_DEADLINE_EXCEEDED
            ) from None
        except (ConnectorError, ProtocolValidationError):
            await _cleanup_binding(binding)
            raise
        except Exception:
            await _cleanup_binding(binding)
            raise ConnectorError(ConnectorErrorCode.CONNECTOR_STATE_INVALID) from None

    async def _execute_binding(
        self, binding: ConnectorExecutorBinding
    ) -> OperationResultV1:
        async def execute(environment: ExecutorEnvironment) -> OperationResultV1:
            return await binding.executor.execute(self._execution, environment)

        secret = binding.secret
        if secret is None:
            return await execute(_EMPTY_ENVIRONMENT)
        return await prepare_secret_execution(
            self._execution,
            catalog=secret.catalog,
            provider=secret.provider,
            injection_target=secret.injection_target,
            deadline=self._deadline,
            execute=execute,
        )

    @staticmethod
    def _validate_result(result: object, binding: ConnectorExecutorBinding) -> None:
        source = get_source(binding.source)
        operation = (
            get_operation(source, binding.operation) if source is not None else None
        )
        if (
            operation is None
            or not isinstance(result, OperationResultV1)
            or len(result.items) > operation.runtime.maximum_items
            or result.character_count() > operation.runtime.maximum_characters
        ):
            raise ProtocolValidationError(
                "The Connector executor result exceeds the operation bounds."
            )
        canonical_operation_result_bytes(result)

    def __repr__(self) -> str:
        return "PreparedConnectorExecution(<redacted>)"


async def _cleanup_binding(binding: ConnectorExecutorBinding) -> None:
    cleanup = binding.cleanup
    if cleanup is None:
        return
    try:
        await cleanup()
    except BaseException:
        pass


__all__ = [
    "ConnectorExecutionComposition",
    "ConnectorExecutor",
    "ConnectorExecutorBinding",
    "ExecutorEnvironment",
    "PreparedConnectorExecution",
    "SecretExecutionPlan",
]
