"""Exact, audited Exa client boundary with no local secret resolution."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Protocol

from ..runtime.adapters import AdapterBinding, AdapterResult, RawItem
from ..runtime.policy import AuthorizedCall

_EXA_OPERATIONS: Final = frozenset({"search.web", "search.code"})


class ExaClient(Protocol):
    """The only Exa methods an injected implementation may expose to Reach."""

    async def search_web(self, query: str, limit: int) -> tuple[RawItem, ...]: ...

    async def search_code(self, query: str, limit: int) -> tuple[RawItem, ...]: ...


@dataclass(frozen=True, slots=True)
class ExaClientAttestation:
    """Operator-reviewed equivalence facts, supplied outside tool input."""

    provider_id: str
    provider_version: str
    operations: frozenset[str]
    logs_queries: bool
    persists_content: bool
    hidden_model_processing: bool
    runtime_dependency_install: bool


@dataclass(frozen=True, slots=True)
class AuditedExaClient:
    client: ExaClient
    attestation: ExaClientAttestation


def exa_client_is_eligible(bundle: AuditedExaClient) -> bool:
    """Require exact provider identity and every no-side-effect claim."""

    attestation = bundle.attestation
    return bool(
        attestation.provider_id == "exa"
        and 0 < len(attestation.provider_version) <= 64
        and attestation.operations == _EXA_OPERATIONS
        and not attestation.logs_queries
        and not attestation.persists_content
        and not attestation.hidden_model_processing
        and not attestation.runtime_dependency_install
    )


def exa_bindings(bundle: AuditedExaClient) -> tuple[AdapterBinding, ...]:
    """Build exact search bindings only after the equivalence gate passes."""

    if not exa_client_is_eligible(bundle):
        raise ValueError("The injected Exa client failed the equivalence gate.")
    adapter = _ExaAdapter(bundle.client)
    version = bundle.attestation.provider_version
    return tuple(
        AdapterBinding(
            source="exa",
            operation=operation,
            backend_id="exa-audited-client",
            backend_version=version,
            priority=10,
            required_scope="public",
            equivalence_group=f"exa:{operation}:v1",
            execute=adapter.execute,
        )
        for operation in sorted(_EXA_OPERATIONS)
    )


class _ExaAdapter:
    def __init__(self, client: ExaClient) -> None:
        self._client = client

    async def execute(self, authorized: AuthorizedCall) -> AdapterResult:
        query = authorized.call.query
        if query is None:
            return AdapterResult(failure_class="invalid_input")
        raw_limit = authorized.call.options.get(
            "limit", authorized.operation.runtime.maximum_items
        )
        if isinstance(raw_limit, bool) or not isinstance(raw_limit, int):
            return AdapterResult(failure_class="invalid_input")
        try:
            if authorized.operation.name == "search.web":
                items = await self._client.search_web(query, raw_limit)
            elif authorized.operation.name == "search.code":
                items = await self._client.search_code(query, raw_limit)
            else:
                return AdapterResult(failure_class="invalid_input")
            if not isinstance(items, tuple) or not all(
                isinstance(item, RawItem) for item in items
            ):
                return AdapterResult(failure_class="permanent")
            return AdapterResult(items)
        except Exception:
            return AdapterResult(failure_class="transient")
