from __future__ import annotations

import asyncio
from collections.abc import Mapping

import pytest

from hermes_reach.runtime.adapters import AdapterResult, MediaMetadata
from hermes_reach.sources.bilibili import (
    BilibiliWorkerError,
    ProductionBilibiliClient,
    production_bilibili_backend,
)
from hermes_reach.sources.bilibili_worker import (
    BilibiliProjection,
    BilibiliVideoProjection,
)


def _video(*, text: str = "Description") -> BilibiliVideoProjection:
    return BilibiliVideoProjection(
        text,
        "BV1xx411c7mD",
        "Video title",
        "https://www.bilibili.com/video/BV1xx411c7mD",
        "Author",
        125,
        99,
    )


class FixtureWorker:
    def __init__(
        self,
        responses: Mapping[str, BilibiliProjection | BilibiliWorkerError],
    ) -> None:
        self.responses = responses
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def execute(self, operation: str, **kwargs: object) -> BilibiliProjection:
        self.calls.append((operation, kwargs))
        response = self.responses[operation]
        if isinstance(response, BilibiliWorkerError):
            raise response
        return response


def _client(
    responses: Mapping[str, BilibiliProjection | BilibiliWorkerError],
) -> tuple[ProductionBilibiliClient, FixtureWorker]:
    fixture = FixtureWorker(responses)
    return ProductionBilibiliClient(fixture), fixture  # type: ignore[arg-type]


def test_client_maps_fork_items_using_only_operation_product_semantics() -> None:
    responses = {
        operation: BilibiliProjection(
            operation, (_video(),), operation == "browse.rank"
        )
        for operation in (
            "search.videos",
            "read.video",
            "browse.hot",
            "browse.rank",
        )
    }
    client, worker = _client(responses)  # type: ignore[arg-type]

    search, video, hot, rank = asyncio.run(
        _all(
            client.search_videos("query", 1),
            client.read_video("https://www.bilibili.com/video/BV1xx411c7mD"),
            client.browse_hot(2),
            client.browse_rank(3),
        )
    )

    assert [result.is_success for result in (search, video, hot, rank)] == [
        True,
        True,
        True,
        True,
    ]
    assert [result.items[0].kind for result in (search, video, hot, rank)] == [
        "result",
        "content",
        "entry",
        "entry",
    ]
    assert [result.items[0].media for result in (search, video, hot, rank)] == [
        MediaMetadata(duration_seconds=125, view_count=99, coverage="partial"),
        MediaMetadata(duration_seconds=125, view_count=99, coverage="complete"),
        MediaMetadata(duration_seconds=125, view_count=99, coverage="partial"),
        MediaMetadata(duration_seconds=125, view_count=99, coverage="partial"),
    ]
    assert [result.items[0].text for result in (search, video, hot, rank)] == [
        "Description"
    ] * 4
    assert rank.truncated is True
    assert [call[0] for call in worker.calls] == [
        "search.videos",
        "read.video",
        "browse.hot",
        "browse.rank",
    ]


async def _all(*awaitables: object) -> tuple[AdapterResult, ...]:
    return tuple(await asyncio.gather(*awaitables))  # type: ignore[arg-type,return-value]


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
def test_client_preserves_worker_failure_class(failure_class: str) -> None:
    client, _ = _client(
        {
            "browse.hot": BilibiliWorkerError(failure_class),  # type: ignore[arg-type]
        }
    )

    result = asyncio.run(client.browse_hot(1))

    assert result.failure_class == failure_class


def test_client_rejects_cross_operation_projection() -> None:
    client, _ = _client(
        {
            "read.video": BilibiliProjection("browse.hot", (_video(),), False),
        }
    )

    result = asyncio.run(
        client.read_video("https://www.bilibili.com/video/BV1xx411c7mD")
    )

    assert result.failure_class == "permanent"


def test_production_bundle_is_exact_and_i_o_free_to_construct() -> None:
    bundle = production_bilibili_backend()

    assert isinstance(bundle.client, ProductionBilibiliClient)
    assert bundle.attestation.provider_id == "bili-cli"
    assert bundle.attestation.provider_version == "0.6.2"
    assert bundle.attestation.operations == frozenset(
        {"search.videos", "read.video", "browse.hot", "browse.rank"}
    )
    assert bundle.attestation.imports_credentials is False
    assert bundle.attestation.imports_cookies is False
    assert bundle.attestation.uses_proxy is False
    assert bundle.attestation.uses_shell is False
