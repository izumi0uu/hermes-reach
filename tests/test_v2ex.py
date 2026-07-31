from __future__ import annotations

import asyncio
from collections.abc import Mapping

import pytest

from hermes_reach.contracts import validate_browse, validate_read
from hermes_reach.runtime.adapters import AdapterResult
from hermes_reach.runtime.policy import ReadOnlyPolicy
from hermes_reach.sources.v2ex import V2exAdapter, V2exWorkerError
from hermes_reach.sources.v2ex_worker import (
    V2exProfileProjection,
    V2exProjection,
    V2exReplyProjection,
    V2exTopicProjection,
)


def _topic(*, native_id: str = "42", node: str = "python") -> V2exTopicProjection:
    return V2exTopicProjection(
        "Topic body",
        native_id,
        "Topic title",
        f"https://www.v2ex.com/t/{native_id}",
        "alice",
        "2023-11-14T22:13:20+00:00",
        node,
    )


def _reply(*, native_id: str = "7", topic_id: str = "42") -> V2exReplyProjection:
    return V2exReplyProjection(
        "Reply body",
        native_id,
        f"https://www.v2ex.com/t/{topic_id}#reply{native_id}",
        "bob",
        None,
    )


def _profile() -> V2exProfileProjection:
    return V2exProfileProjection(
        "Bio Shanghai",
        "9",
        "Alice",
        "https://www.v2ex.com/member/Alice",
        None,
    )


class FixtureWorker:
    def __init__(
        self,
        responses: Mapping[str, V2exProjection | V2exWorkerError],
    ) -> None:
        self.responses = responses
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def execute(self, operation: str, **kwargs: object) -> V2exProjection:
        self.calls.append((operation, kwargs))
        response = self.responses[operation]
        if isinstance(response, V2exWorkerError):
            raise response
        return response


def _adapter(
    responses: Mapping[str, V2exProjection | V2exWorkerError],
) -> tuple[V2exAdapter, FixtureWorker]:
    fixture = FixtureWorker(responses)
    return V2exAdapter(fixture), fixture  # type: ignore[arg-type]


def test_adapter_maps_all_four_operations_and_forwards_closed_arguments() -> None:
    adapter, worker = _adapter(
        {
            "browse.hot": V2exProjection("browse.hot", (_topic(),), False),
            "browse.node_topics": V2exProjection(
                "browse.node_topics",
                (_topic(node="Python"),),
                True,
            ),
            "read.topic": V2exProjection(
                "read.topic",
                (_topic(), _reply()),
                False,
            ),
            "read.user": V2exProjection("read.user", (_profile(),), False),
        }
    )
    policy = ReadOnlyPolicy()
    calls = (
        policy.authorize(
            validate_browse(
                {
                    "source": "v2ex",
                    "operation": "browse.hot",
                    "options": {"limit": 3},
                }
            )
        ),
        policy.authorize(
            validate_browse(
                {
                    "source": "v2ex",
                    "operation": "browse.node_topics",
                    "options": {"node": "python", "page": 4, "limit": 5},
                }
            )
        ),
        policy.authorize(
            validate_read(
                {
                    "source": "v2ex",
                    "operation": "read.topic",
                    "target": {"native_id": "0042"},
                }
            )
        ),
        policy.authorize(
            validate_read(
                {
                    "source": "v2ex",
                    "operation": "read.user",
                    "target": {"native_id": "alice"},
                }
            )
        ),
    )

    hot, node, topic, profile = asyncio.run(
        _all(*(adapter.execute(call) for call in calls))
    )

    assert [item.kind for item in hot.items] == ["topic"]
    assert [item.kind for item in node.items] == ["topic"]
    assert [item.kind for item in topic.items] == ["topic", "reply"]
    assert [item.kind for item in profile.items] == ["profile"]
    assert topic.items[0].title == "Topic title"
    assert topic.items[1].title is None
    assert profile.items[0].title == "Alice"
    assert node.truncated is True
    assert worker.calls == [
        ("browse.hot", {"limit": 3}),
        ("browse.node_topics", {"node": "python", "page": 4, "limit": 5}),
        ("read.topic", {"topic_id": "0042"}),
        ("read.user", {"username": "alice"}),
    ]


async def _all(*awaitables: object) -> tuple[AdapterResult, ...]:
    return tuple(await asyncio.gather(*awaitables))  # type: ignore[arg-type,return-value]


def test_adapter_uses_catalog_defaults_for_browse_page_and_limit() -> None:
    adapter, worker = _adapter(
        {
            "browse.hot": V2exProjection("browse.hot", (), False),
            "browse.node_topics": V2exProjection("browse.node_topics", (), False),
        }
    )
    policy = ReadOnlyPolicy()

    asyncio.run(
        _all(
            adapter.execute(
                policy.authorize(
                    validate_browse({"source": "v2ex", "operation": "browse.hot"})
                )
            ),
            adapter.execute(
                policy.authorize(
                    validate_browse(
                        {
                            "source": "v2ex",
                            "operation": "browse.node_topics",
                            "options": {"node": "go"},
                        }
                    )
                )
            ),
        )
    )

    assert worker.calls == [
        ("browse.hot", {"limit": 20}),
        ("browse.node_topics", {"node": "go", "page": 1, "limit": 20}),
    ]


@pytest.mark.parametrize(
    ("partial_code", "expected"),
    [
        ("not_found", "not_found"),
        ("authentication", "authentication"),
        ("authorization", "authorization"),
        ("rate_limit", "rate_limit"),
        ("transient", "transient"),
        ("permanent", "permanent"),
        ("backend_contract_violation", "permanent"),
    ],
)
def test_adapter_preserves_closed_partial_failure(
    partial_code: str,
    expected: str,
) -> None:
    adapter, _ = _adapter(
        {
            "read.topic": V2exProjection(
                "read.topic",
                (_topic(),),
                False,
                partial_code,  # type: ignore[arg-type]
            )
        }
    )
    call = ReadOnlyPolicy().authorize(
        validate_read(
            {
                "source": "v2ex",
                "operation": "read.topic",
                "target": {"native_id": "42"},
            }
        )
    )

    result = asyncio.run(adapter.execute(call))

    assert result.is_success
    assert result.partial_failure_class == expected
    assert [item.native_id for item in result.items] == ["42"]


@pytest.mark.parametrize(
    "failure_class",
    [
        "invalid_input",
        "not_found",
        "authentication",
        "authorization",
        "rate_limit",
        "transient",
        "permanent",
    ],
)
def test_adapter_preserves_worker_failure_class(failure_class: str) -> None:
    adapter, _ = _adapter(
        {
            "browse.hot": V2exWorkerError(failure_class),  # type: ignore[arg-type]
        }
    )
    call = ReadOnlyPolicy().authorize(
        validate_browse({"source": "v2ex", "operation": "browse.hot"})
    )

    result = asyncio.run(adapter.execute(call))

    assert result.failure_class == failure_class


def test_adapter_rejects_cross_operation_projection() -> None:
    adapter, _ = _adapter(
        {"read.user": V2exProjection("browse.hot", (_profile(),), False)}
    )
    call = ReadOnlyPolicy().authorize(
        validate_read(
            {
                "source": "v2ex",
                "operation": "read.user",
                "target": {"native_id": "alice"},
            }
        )
    )

    result = asyncio.run(adapter.execute(call))

    assert result.failure_class == "permanent"


def test_adapter_construction_performs_no_worker_or_backend_work() -> None:
    adapter = V2exAdapter()

    assert adapter._worker.__class__.__name__ == "V2exWorker"
