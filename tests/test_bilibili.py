from __future__ import annotations

import asyncio
from collections.abc import Mapping

import pytest

from hermes_reach.runtime.adapters import AdapterResult, MediaMetadata
from hermes_reach.sources.bilibili import (
    ProductionBilibiliClient,
    production_bilibili_backend,
)


def _summary(
    bvid: str = "BV1xx411c7mD", *, description: str = "Description"
) -> dict[str, object]:
    return {
        "id": bvid,
        "bvid": bvid,
        "aid": 123,
        "title": "Video title",
        "description": description,
        "duration_seconds": 125,
        "duration": "02:05",
        "url": f"https://www.bilibili.com/video/{bvid}",
        "owner": {"id": "7", "name": "Author"},
        "stats": {
            "view": 99,
            "danmaku": 8,
            "like": 7,
            "coin": 6,
            "favorite": 5,
            "share": 4,
        },
    }


def _video_command() -> dict[str, object]:
    return {
        "video": _summary(),
        "subtitle": {"available": False, "format": "plain", "text": "", "items": []},
        "ai_summary": "",
        "comments": [],
        "related": [],
        "warnings": [],
    }


class FixtureWorker:
    def __init__(self, responses: Mapping[str, Mapping[str, object]]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def execute(self, operation: str, **kwargs: object) -> Mapping[str, object]:
        self.calls.append((operation, kwargs))
        return self.responses[operation]


def _client(
    responses: Mapping[str, Mapping[str, object]],
) -> tuple[ProductionBilibiliClient, FixtureWorker]:
    fixture = FixtureWorker(responses)
    return ProductionBilibiliClient(fixture), fixture  # type: ignore[arg-type]


def test_client_projects_all_four_exact_backend_shapes() -> None:
    responses = {
        "search.videos": {
            "ok": True,
            "schema_version": "1",
            "data": [
                {
                    "id": "BV1xx411c7mD",
                    "bvid": "BV1xx411c7mD",
                    "title": "Search title",
                    "author": "Search author",
                    "play": 42,
                    "duration": "01:30",
                }
            ],
        },
        "read.video": {"ok": True, "schema_version": "1", "data": _video_command()},
        "browse.hot": {
            "ok": True,
            "schema_version": "1",
            "data": {"items": [_summary()], "page": 1, "count": 2},
        },
        "browse.rank": {
            "ok": True,
            "schema_version": "1",
            "data": {"items": [_summary(description="")], "day": 3, "count": 3},
        },
    }
    client, worker = _client(responses)

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
    assert search.items[0].media == MediaMetadata(
        duration_seconds=90,
        view_count=42,
        coverage="partial",
    )
    assert video.items[0].text == "Description"
    assert video.items[0].media == MediaMetadata(
        duration_seconds=125,
        view_count=99,
        coverage="complete",
    )
    assert hot.items[0].kind == "entry"
    assert rank.items[0].text == "Video title"
    assert [call[0] for call in worker.calls] == [
        "search.videos",
        "read.video",
        "browse.hot",
        "browse.rank",
    ]


async def _all(*awaitables: object) -> tuple[AdapterResult, ...]:
    return tuple(await asyncio.gather(*awaitables))  # type: ignore[arg-type,return-value]


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("invalid_input", "invalid_input"),
        ("not_found", "not_found"),
        ("not_authenticated", "authentication"),
        ("permission_denied", "authorization"),
        ("rate_limited", "rate_limit"),
        ("network_error", "transient"),
        ("upstream_error", "permanent"),
        ("internal_error", "permanent"),
        ("future_error", "permanent"),
    ],
)
def test_client_maps_only_allowlisted_error_codes(code: str, expected: str) -> None:
    client, _ = _client(
        {
            "browse.hot": {
                "ok": False,
                "schema_version": "1",
                "error": {
                    "code": code,
                    "message": "/private/path query=secret",
                    "details": {"credential": "hidden"},
                },
            }
        }
    )

    result = asyncio.run(client.browse_hot(1))

    assert result.failure_class == expected
    assert "secret" not in str(result)


@pytest.mark.parametrize(
    "data",
    [
        {**_video_command(), "comments": [{"message": "unexpected"}]},
        {**_video_command(), "subtitle": {"available": True}},
        {**_video_command(), "unknown": True},
        {**_video_command(), "video": {**_summary(), "url": "https://evil.test"}},
    ],
)
def test_video_optional_or_schema_drift_fails_closed(data: dict[str, object]) -> None:
    client, _ = _client(
        {"read.video": {"ok": True, "schema_version": "1", "data": data}}
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
