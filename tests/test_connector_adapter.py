from __future__ import annotations

import asyncio
import base64
import json
from dataclasses import dataclass

import pytest

import hermes_reach.tools as reach_tools
from hermes_reach.connector.errors import ConnectorError, ConnectorErrorCode
from hermes_reach.connector.identity import DevicePrivateIdentity
from hermes_reach.connector.protocol import (
    OperationResponseV1,
    OperationResultItemV1,
    OperationResultMediaV1,
    OperationResultV1,
    PublicBackendIdentity,
    ReceiptUsage,
    create_signed_receipt,
    create_signed_request,
    protect_operation_call,
)
from hermes_reach.contracts import OperationCall, validate_read, validate_search
from hermes_reach.runtime.adapters import AdapterRegistry
from hermes_reach.runtime.availability import AvailabilityRecord
from hermes_reach.runtime.dispatcher import RuntimeDispatcher
from hermes_reach.runtime.policy import ReadOnlyPolicy
from hermes_reach.sources.connector import connector_bindings
from hermes_reach.sources.registry import build_alpha1_registry
from hermes_reach.tools import reach_search

NOW = 1_800_000_000
BACKEND = PublicBackendIdentity("reach-bounded-executor-v1", "1")


def _id(value: int) -> str:
    return base64.b32encode(value.to_bytes(16, "big")).decode().rstrip("=").lower()


@dataclass
class _FixtureConnectorClient:
    calls: list[tuple[OperationCall, str]]

    def __post_init__(self) -> None:
        self._connector = DevicePrivateIdentity._from_seed_for_testing(bytes([20]) * 32)
        self._vps = DevicePrivateIdentity._from_seed_for_testing(bytes([21]) * 32)
        self._slot = 100

    async def execute(
        self, call: OperationCall, *, trace_id: str
    ) -> OperationResponseV1:
        self.calls.append((call, trace_id))
        self._slot += 10
        protected = protect_operation_call(call)
        request = create_signed_request(
            self._vps,
            message_id=_id(self._slot),
            request_id=_id(self._slot + 1),
            trace_id=trace_id,
            audience_key_id=self._connector.public_identity.key_id,
            grant_id=_id(1),
            grant_revision=1,
            policy_revision=1,
            source=call.source.name,
            operation=call.operation.name,
            issued_at=NOW,
            deadline=NOW + 30,
            protected_payload=protected,
        )
        result = OperationResultV1(
            (
                OperationResultItemV1(
                    "result",
                    "connector fixture",
                    native_id="native-1",
                    title="Fixture",
                    url="https://example.com/item",
                    author="author",
                    published_at="2026-07-26T00:00:00+00:00",
                    media=OperationResultMediaV1(
                        "partial",
                        duration_seconds=30,
                        view_count=100,
                        comment_count=5,
                        subtitle_language="en",
                        subtitle_origin="manual",
                    ),
                ),
            ),
            True,
        )
        receipt = create_signed_receipt(
            self._connector,
            message_id=_id(self._slot + 2),
            receipt_id=_id(self._slot + 3),
            request=request,
            decision="allow",
            failure=None,
            usage=ReceiptUsage(1, 4),
            backend=BACKEND,
            started_at=NOW,
            ended_at=NOW + 1,
            expires_at=NOW + 120,
            result=result,
            outcome="ok",
        )
        return OperationResponseV1(receipt.message_id, receipt, result)


class _OfflineConnectorClient:
    def __init__(self) -> None:
        self.calls = 0

    async def execute(
        self, call: OperationCall, *, trace_id: str
    ) -> OperationResponseV1:
        del call, trace_id
        self.calls += 1
        raise ConnectorError(ConnectorErrorCode.CONNECTOR_OFFLINE)


def _availability(source: str, operation: str) -> AvailabilityRecord:
    assert source and operation
    return AvailabilityRecord("available", "Verified local Connector snapshot.")


def _degraded_availability(source: str, operation: str) -> AvailabilityRecord:
    assert source and operation
    return AvailabilityRecord(
        "degraded",
        "No recent Connector snapshot.",
        cause_code=ConnectorErrorCode.CONNECTOR_OFFLINE.value,
    )


def test_connector_binding_maps_verified_result_and_preserves_truncation() -> None:
    client = _FixtureConnectorClient([])
    registry = AdapterRegistry()
    for binding in connector_bindings(client, _availability, (("web", "read.url"),)):
        registry.register(binding)
    call = validate_read(
        {
            "source": "web",
            "operation": "read.url",
            "target": {"url": "https://example.com/connector-adapter"},
        }
    )

    result = asyncio.run(RuntimeDispatcher(registry).dispatch(call, trace_id="a" * 32))

    assert result is not None
    assert result.truncated is True
    assert result.selected_backend_id == BACKEND.backend_id
    assert result.selected_backend_version == BACKEND.backend_version
    assert len(result.attempts) == 1
    assert result.items[0].native_id == "native-1"
    assert result.items[0].media is not None
    assert result.items[0].media.coverage == "partial"
    assert result.items[0].media.subtitle_origin == "manual"
    assert client.calls[0][1] == "a" * 32


def test_connector_binding_owns_transient_retry() -> None:
    client = _OfflineConnectorClient()
    registry = AdapterRegistry()
    for binding in connector_bindings(client, _availability, (("web", "read.url"),)):
        registry.register(binding)
    call = validate_read(
        {
            "source": "web",
            "operation": "read.url",
            "target": {"url": "https://example.com/offline"},
        }
    )

    result = asyncio.run(RuntimeDispatcher(registry).dispatch(call, trace_id="b" * 32))

    assert result is not None
    assert result.failure_class == "transient"
    assert len(result.attempts) == 1
    assert client.calls == 1


def test_connector_factory_rejects_duplicate_unknown_and_planned_operations() -> None:
    client = _FixtureConnectorClient([])
    with pytest.raises(ValueError, match="selection"):
        connector_bindings(
            client,
            _availability,
            (("web", "read.url"), ("web", "read.url")),
        )
    with pytest.raises(ValueError, match="selection"):
        connector_bindings(client, _availability, (("web", "read.unknown"),))
    with pytest.raises(ValueError, match="selection"):
        connector_bindings(client, _availability, (("reddit", "read.post"),))


def test_multi_source_tool_trace_reaches_each_connector_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _FixtureConnectorClient([])
    registry = AdapterRegistry()
    for binding in connector_bindings(
        client,
        _degraded_availability,
        (("github", "search.repositories"), ("exa", "search.web")),
    ):
        registry.register(binding)
    monkeypatch.setattr(reach_tools, "_RUNTIME", RuntimeDispatcher(registry))

    response = json.loads(
        asyncio.run(
            reach_search(
                {
                    "requests": [
                        {
                            "source": "github",
                            "operation": "search.repositories",
                            "query": "first",
                        },
                        {
                            "source": "exa",
                            "operation": "search.web",
                            "query": "second",
                        },
                    ]
                }
            )
        )
    )

    assert response["outcome"] == "ok"
    assert len(client.calls) == 2
    assert {trace for _, trace in client.calls} == {response["trace_id"]}


def test_default_alpha1_registry_contains_no_connector_binding() -> None:
    registry = build_alpha1_registry()
    calls = (
        validate_read(
            {
                "source": "web",
                "operation": "read.url",
                "target": {"url": "https://example.com"},
            }
        ),
        validate_search(
            {
                "requests": [
                    {
                        "source": "exa",
                        "operation": "search.web",
                        "query": "fixture",
                    }
                ]
            }
        )[0],
    )

    assert all(
        binding.backend_id != BACKEND.backend_id
        for call in calls
        for binding in registry.candidates(ReadOnlyPolicy().authorize(call))
    )
