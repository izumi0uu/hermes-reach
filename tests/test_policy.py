from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from hermes_reach.catalog import get_operation, get_source
from hermes_reach.contracts import OperationCall, validate_browse, validate_search
from hermes_reach.runtime.adapters import (
    AdapterBinding,
    AdapterRegistry,
    AdapterResult,
)
from hermes_reach.runtime.dispatcher import RuntimeDispatcher
from hermes_reach.runtime.policy import ReadOnlyPolicy, RuntimePolicyError


async def _successful_adapter(_: object) -> AdapterResult:
    return AdapterResult()


def _binding(
    backend_id: str,
    *,
    source: str = "github",
    operation: str = "search.repositories",
    required_scope: str = "public",
) -> AdapterBinding:
    return AdapterBinding(
        source=source,
        operation=operation,
        backend_id=backend_id,
        backend_version="1.0",
        priority=10,
        required_scope=required_scope,  # type: ignore[arg-type]
        equivalence_group="catalog-operation",
        execute=_successful_adapter,
    )


def test_policy_authorizes_canonical_validated_operation() -> None:
    call = validate_search(
        {
            "requests": [
                {
                    "source": "github",
                    "operation": "search.repositories",
                    "query": "public topic",
                }
            ]
        }
    )[0]

    authorized = ReadOnlyPolicy("policy-1").authorize(call)

    assert authorized.call is call
    assert authorized.policy_revision == "policy-1"
    assert authorized.effective_scope == "public"


def test_policy_rejects_forged_or_cross_source_catalog_rows() -> None:
    github = get_source("github")
    youtube = get_source("youtube")
    assert github is not None
    assert youtube is not None
    operation = get_operation(github, "search.repositories")
    assert operation is not None
    forged = OperationCall(youtube, operation, {}, query="public")

    with pytest.raises(RuntimePolicyError) as exc_info:
        ReadOnlyPolicy().authorize(forged)

    assert exc_info.value.code == "policy_denied"
    assert "public" not in str(exc_info.value)


def test_policy_rejects_forged_options_before_adapter_selection() -> None:
    call = validate_search(
        {
            "requests": [
                {
                    "source": "github",
                    "operation": "search.repositories",
                    "query": "public",
                }
            ]
        }
    )[0]
    forged = replace(call, options={"argv": "private-backend-argument"})

    with pytest.raises(RuntimePolicyError) as exc_info:
        ReadOnlyPolicy().authorize(forged)

    assert exc_info.value.code == "policy_denied"
    assert "private-backend-argument" not in str(exc_info.value)


def test_policy_requires_account_visible_scope_for_private_feeds() -> None:
    call = validate_browse(
        {
            "source": "facebook",
            "operation": "browse.feed",
            "options": {"limit": 5},
        }
    )
    policy = ReadOnlyPolicy()

    with pytest.raises(RuntimePolicyError) as exc_info:
        policy.authorize(call)

    assert exc_info.value.code == "policy_denied"
    assert policy.authorize(call, "account_visible").operation == call.operation


def test_registry_rejects_unknown_duplicate_and_scope_widening_bindings() -> None:
    registry = AdapterRegistry()

    with pytest.raises(ValueError, match="catalog operation"):
        registry.register(_binding("unknown", operation="search.unknown"))

    registry.register(_binding("backend-one"))
    with pytest.raises(ValueError, match="only once"):
        registry.register(_binding("backend-one"))
    with pytest.raises(ValueError, match="cannot widen"):
        registry.register(_binding("backend-two", required_scope="account_visible"))


def test_registry_returns_deterministically_sorted_candidates() -> None:
    registry = AdapterRegistry()
    registry.register(_binding("zeta", required_scope="public"))
    registry.register(replace(_binding("alpha", required_scope="public"), priority=5))
    call = validate_search(
        {
            "requests": [
                {
                    "source": "github",
                    "operation": "search.repositories",
                    "query": "public",
                }
            ]
        }
    )[0]

    assert [
        binding.backend_id
        for binding in registry.candidates(ReadOnlyPolicy().authorize(call))
    ] == ["alpha", "zeta"]


def test_dispatcher_authorizes_before_executing_registered_bindings() -> None:
    invocations: list[str] = []

    async def execute(_: object) -> AdapterResult:
        invocations.append("executed")
        return AdapterResult()

    registry = AdapterRegistry()
    registry.register(
        replace(_binding("backend"), execute=execute)  # type: ignore[arg-type]
    )
    dispatcher = RuntimeDispatcher(registry)
    call = validate_search(
        {
            "requests": [
                {
                    "source": "github",
                    "operation": "search.repositories",
                    "query": "public",
                }
            ]
        }
    )[0]
    github = get_source("github")
    youtube = get_source("youtube")
    assert github is not None
    assert youtube is not None
    forged = OperationCall(youtube, call.operation, {}, query="public")

    with pytest.raises(RuntimePolicyError):
        asyncio.run(dispatcher.dispatch(forged))
    assert invocations == []

    result = asyncio.run(dispatcher.dispatch(call))
    assert result is not None
    assert invocations == ["executed"]
