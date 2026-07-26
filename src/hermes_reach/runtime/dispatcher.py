"""The sole in-process boundary from validated calls to adapter execution."""

from __future__ import annotations

from ..contracts import OperationCall
from .adapters import AdapterRegistry
from .availability import AvailabilityRecord
from .policy import EffectiveScope, ReadOnlyPolicy
from .runner import BoundedRunner, RunnerResult


class RuntimeDispatcher:
    """Authorize and execute only registered read-only adapter bindings."""

    def __init__(
        self,
        registry: AdapterRegistry | None = None,
        policy: ReadOnlyPolicy | None = None,
        runner: BoundedRunner | None = None,
    ) -> None:
        self._registry = registry or AdapterRegistry()
        self._policy = policy or ReadOnlyPolicy()
        self._runner = runner or BoundedRunner()

    def is_unavailable(self, call: OperationCall) -> bool:
        """Return whether a validated public call has no registered adapter."""

        return not self._registry.has_binding(call.source.name, call.operation.name)

    def operation_availability(self, source: str, operation: str) -> AvailabilityRecord:
        """Project registry state without contacting an adapter."""

        return self._registry.availability(source, operation)

    async def dispatch(
        self,
        call: OperationCall,
        effective_scope: EffectiveScope = "public",
        *,
        trace_id: str | None = None,
    ) -> RunnerResult | None:
        """Run a call through policy and bounds, or report an empty registry."""

        authorized = self._policy.authorize(call, effective_scope, trace_id=trace_id)
        bindings = self._registry.candidates(authorized)
        if not bindings:
            return None
        return await self._runner.run(authorized, bindings)
