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
from hermes_reach.sources.connector import (
    PRODUCTION_CONNECTOR_BACKEND_BY_OPERATION,
    PRODUCTION_CONNECTOR_OPERATIONS,
    connector_bindings,
)
from hermes_reach.sources.registry import build_alpha1_registry
from hermes_reach.tools import reach_search

NOW = 1_800_000_000
BACKEND = PublicBackendIdentity("reach-bounded-executor-v1", "1")
OPENCLI_SOCIAL_BACKEND = PublicBackendIdentity("opencli", "1.8.6-hermes.1")
XUEQIU_BACKEND = PublicBackendIdentity("xueqiu-api", "1.5.0+search.v1")
EXPECTED_PRODUCTION_CONNECTOR_OPERATIONS = (
    ("reddit", "search.posts"),
    ("reddit", "read.post"),
    ("reddit", "browse.subreddit"),
    ("reddit", "browse.hot"),
    ("reddit", "browse.popular"),
    ("reddit", "browse.all"),
    ("reddit", "read.subreddit"),
    ("facebook", "search"),
    ("facebook", "read.profile"),
    ("facebook", "browse.feed"),
    ("facebook", "browse.groups"),
    ("instagram", "search.users"),
    ("instagram", "read.profile"),
    ("instagram", "browse.user_posts"),
    ("instagram", "browse.explore"),
    ("twitter", "search.posts"),
    ("xiaohongshu", "search.notes"),
    ("xueqiu", "search.stocks"),
)
EXPECTED_PRODUCTION_CONNECTOR_BACKENDS = (
    *(
        (operation, OPENCLI_SOCIAL_BACKEND)
        for operation in EXPECTED_PRODUCTION_CONNECTOR_OPERATIONS[:17]
    ),
    (("xueqiu", "search.stocks"), XUEQIU_BACKEND),
)


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
        backend = PRODUCTION_CONNECTOR_BACKEND_BY_OPERATION.get(
            (call.source.name, call.operation.name),
            BACKEND,
        )
        receipt = create_signed_receipt(
            self._connector,
            message_id=_id(self._slot + 2),
            receipt_id=_id(self._slot + 3),
            request=request,
            decision="allow",
            failure=None,
            usage=ReceiptUsage(1, 4),
            backend=backend,
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


@pytest.mark.parametrize(
    ("source", "operation", "expected_backend"),
    (
        ("twitter", "search.posts", OPENCLI_SOCIAL_BACKEND),
        ("xiaohongshu", "search.notes", OPENCLI_SOCIAL_BACKEND),
        ("xueqiu", "search.stocks", XUEQIU_BACKEND),
    ),
)
def test_fixture_receipts_follow_the_production_backend_map(
    source: str,
    operation: str,
    expected_backend: PublicBackendIdentity,
) -> None:
    client = _FixtureConnectorClient([])
    call = validate_search(
        {
            "requests": [
                {
                    "source": source,
                    "operation": operation,
                    "query": "fixture",
                }
            ]
        }
    )[0]

    response = asyncio.run(client.execute(call, trace_id="d" * 32))

    assert response.receipt.backend == expected_backend


class _ErrorConnectorClient:
    def __init__(self, code: ConnectorErrorCode) -> None:
        self._code = code

    async def execute(
        self, call: OperationCall, *, trace_id: str
    ) -> OperationResponseV1:
        del call, trace_id
        raise ConnectorError(self._code)


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
    for binding in connector_bindings(client, _availability, (("rss", "read.feed"),)):
        registry.register(binding)
    call = validate_read(
        {
            "source": "rss",
            "operation": "read.feed",
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
    for binding in connector_bindings(client, _availability, (("rss", "read.feed"),)):
        registry.register(binding)
    call = validate_read(
        {
            "source": "rss",
            "operation": "read.feed",
            "target": {"url": "https://example.com/offline"},
        }
    )

    result = asyncio.run(RuntimeDispatcher(registry).dispatch(call, trace_id="b" * 32))

    assert result is not None
    assert result.failure_class == "transient"
    assert len(result.attempts) == 1
    assert client.calls == 1


@pytest.mark.parametrize(
    ("code", "failure_class"),
    (
        (ConnectorErrorCode.BACKEND_INVALID_INPUT, "invalid_input"),
        (ConnectorErrorCode.BACKEND_NOT_FOUND, "not_found"),
        (ConnectorErrorCode.BACKEND_AUTHENTICATION_REQUIRED, "authentication"),
        (ConnectorErrorCode.BACKEND_AUTHORIZATION_DENIED, "authorization"),
        (ConnectorErrorCode.BACKEND_UNAVAILABLE, "transient"),
        (ConnectorErrorCode.BACKEND_INCOMPATIBLE, "setup_required"),
        (ConnectorErrorCode.BACKEND_DEADLINE_EXCEEDED, "transient"),
        (ConnectorErrorCode.BACKEND_RATE_LIMITED, "rate_limit"),
        (ConnectorErrorCode.BACKEND_TRANSIENT, "transient"),
        (ConnectorErrorCode.BACKEND_PERMANENT, "permanent"),
        (ConnectorErrorCode.BACKEND_CONTRACT_VIOLATION, "permanent"),
    ),
)
def test_connector_binding_preserves_closed_backend_failure_classes(
    code: ConnectorErrorCode, failure_class: str
) -> None:
    registry = AdapterRegistry()
    for binding in connector_bindings(
        _ErrorConnectorClient(code), _availability, (("rss", "read.feed"),)
    ):
        registry.register(binding)
    call = validate_read(
        {
            "source": "rss",
            "operation": "read.feed",
            "target": {"url": "https://example.com/backend-failure"},
        }
    )

    result = asyncio.run(RuntimeDispatcher(registry).dispatch(call, trace_id="c" * 32))

    assert result is not None
    assert result.failure_class == failure_class
    assert len(result.attempts) == 1


def test_connector_factory_rejects_duplicate_unknown_and_planned_operations() -> None:
    client = _FixtureConnectorClient([])
    with pytest.raises(ValueError, match="selection"):
        connector_bindings(
            client,
            _availability,
            (("rss", "read.feed"), ("rss", "read.feed")),
        )
    with pytest.raises(ValueError, match="selection"):
        connector_bindings(client, _availability, (("rss", "read.unknown"),))
    assert (
        len(connector_bindings(client, _availability, (("reddit", "read.post"),))) == 1
    )
    social = connector_bindings(
        client,
        _availability,
        (
            ("reddit", "browse.hot"),
            ("facebook", "browse.feed"),
            ("instagram", "browse.explore"),
        ),
    )
    assert {
        (binding.source, binding.operation, binding.backend_id, binding.backend_version)
        for binding in social
    } == {
        ("reddit", "browse.hot", "opencli", "1.8.6-hermes.1"),
        ("facebook", "browse.feed", "opencli", "1.8.6-hermes.1"),
        ("instagram", "browse.explore", "opencli", "1.8.6-hermes.1"),
    }
    for operation in ("search.web", "search.code"):
        with pytest.raises(ValueError, match="selection"):
            connector_bindings(client, _availability, (("exa", operation),))
    for operation in ("search.people", "search.jobs"):
        with pytest.raises(ValueError, match="selection"):
            connector_bindings(client, _availability, (("linkedin", operation),))


def test_production_connector_manifest_is_exact_and_ordered() -> None:
    client = _FixtureConnectorClient([])

    assert PRODUCTION_CONNECTOR_OPERATIONS == EXPECTED_PRODUCTION_CONNECTOR_OPERATIONS
    assert tuple(PRODUCTION_CONNECTOR_BACKEND_BY_OPERATION.items()) == (
        EXPECTED_PRODUCTION_CONNECTOR_BACKENDS
    )
    bindings = connector_bindings(
        client,
        _availability,
        PRODUCTION_CONNECTOR_OPERATIONS,
    )
    assert tuple(
        (
            binding.source,
            binding.operation,
            binding.backend_id,
            binding.backend_version,
        )
        for binding in bindings
    ) == tuple(
        (source, operation, backend.backend_id, backend.backend_version)
        for (source, operation), backend in EXPECTED_PRODUCTION_CONNECTOR_BACKENDS
    )


def test_multi_source_tool_trace_reaches_each_connector_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _FixtureConnectorClient([])
    registry = AdapterRegistry()
    for binding in connector_bindings(
        client,
        _degraded_availability,
        (("youtube", "search.videos"), ("bilibili", "search.videos")),
    ):
        registry.register(binding)
    monkeypatch.setattr(reach_tools, "_RUNTIME", RuntimeDispatcher(registry))

    response = json.loads(
        asyncio.run(
            reach_search(
                {
                    "requests": [
                        {
                            "source": "youtube",
                            "operation": "search.videos",
                            "query": "first",
                        },
                        {
                            "source": "bilibili",
                            "operation": "search.videos",
                            "query": "second",
                        },
                    ]
                }
            )
        )
    )

    assert response["outcome"] == "ok"
    assert len(client.calls) == 2
    assert [(call.source.name, call.operation.name) for call, _ in client.calls] == [
        ("youtube", "search.videos"),
        ("bilibili", "search.videos"),
    ]
    assert {trace for _, trace in client.calls} == {response["trace_id"]}


def test_default_alpha1_registry_contains_no_connector_binding() -> None:
    registry = build_alpha1_registry()
    calls = (
        validate_read(
            {
                "source": "rss",
                "operation": "read.feed",
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
