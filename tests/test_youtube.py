from __future__ import annotations

import asyncio
from collections.abc import Mapping

import pytest

from hermes_reach.runtime.adapters import MediaMetadata
from hermes_reach.sources.youtube import (
    ProductionYouTubeClient,
    production_youtube_backend,
)
from hermes_reach.sources.youtube_worker import WorkerOperation

VIDEO_ID = "dQw4w9WgXcQ"
VIDEO_URL = f"https://www.youtube.com/watch?v={VIDEO_ID}"


def _video() -> dict[str, object]:
    return {
        "id": VIDEO_ID,
        "title": "Video title",
        "description": "Video description",
        "uploader": "Channel",
        "duration_seconds": 213,
        "view_count": 42,
        "comment_count": 7,
        "upload_date": "2009-10-25",
        "url": VIDEO_URL,
    }


class FixtureWorker:
    def __init__(self, responses: Mapping[str, Mapping[str, object]]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def execute(
        self,
        operation: WorkerOperation,
        *,
        query: str | None = None,
        url: str | None = None,
        limit: int | None = None,
        language: str | None = None,
    ) -> Mapping[str, object]:
        kwargs: dict[str, object] = {
            "query": query,
            "url": url,
            "limit": limit,
            "language": language,
        }
        self.calls.append((operation, kwargs))
        return self.responses[operation]


def _client(
    responses: Mapping[str, Mapping[str, object]],
) -> tuple[ProductionYouTubeClient, FixtureWorker]:
    fixture = FixtureWorker(responses)
    return ProductionYouTubeClient(fixture), fixture


def _success(operation: str, data: object) -> dict[str, object]:
    return {
        "protocol_version": "v1",
        "operation": operation,
        "ok": True,
        "data": data,
    }


def test_client_projects_the_three_reviewed_operations() -> None:
    client, worker = _client(
        {
            "search.videos": _success("search.videos", [_video()]),
            "read.video": _success("read.video", _video()),
            "read.subtitles": _success(
                "read.subtitles",
                {
                    "id": VIDEO_ID,
                    "title": "Video title",
                    "language": "en",
                    "origin": "manual",
                    "text": "WEBVTT\n\n00:00.000 --> 00:01.000\nHello",
                    "truncated": True,
                    "url": VIDEO_URL,
                },
            ),
        }
    )

    search = asyncio.run(client.search_videos("private query", 2))
    video = asyncio.run(client.read_video(VIDEO_URL))
    subtitles = asyncio.run(client.read_subtitles(VIDEO_URL, "en"))

    assert search.items[0].kind == "result"
    assert search.items[0].media == MediaMetadata(
        duration_seconds=213,
        view_count=42,
        comment_count=7,
        coverage="partial",
    )
    assert video.items[0].kind == "content"
    assert video.items[0].media == MediaMetadata(
        duration_seconds=213,
        view_count=42,
        comment_count=7,
        coverage="complete",
    )
    assert subtitles.truncated is True
    assert subtitles.items[0].media == MediaMetadata(
        subtitle_language="en",
        subtitle_origin="manual",
        coverage="complete",
    )
    assert worker.calls == [
        (
            "search.videos",
            {"query": "private query", "url": None, "limit": 2, "language": None},
        ),
        (
            "read.video",
            {"query": None, "url": VIDEO_URL, "limit": None, "language": None},
        ),
        (
            "read.subtitles",
            {"query": None, "url": VIDEO_URL, "limit": None, "language": "en"},
        ),
    ]


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("setup_required", "permanent"),
        ("not_found", "not_found"),
        ("authentication", "authentication"),
        ("authorization", "authorization"),
        ("rate_limit", "rate_limit"),
        ("transient", "transient"),
        ("permanent", "permanent"),
        ("future", "permanent"),
    ],
)
def test_client_maps_only_closed_worker_error_codes(code: str, expected: str) -> None:
    client, _ = _client(
        {
            "read.video": {
                "protocol_version": "v1",
                "operation": "read.video",
                "ok": False,
                "error": {"code": code},
            }
        }
    )

    result = asyncio.run(client.read_video(VIDEO_URL))

    assert result.failure_class == expected
    assert "secret" not in str(result)


def test_client_rejects_worker_error_details_without_exposing_them() -> None:
    client, _ = _client(
        {
            "read.video": {
                "protocol_version": "v1",
                "operation": "read.video",
                "ok": False,
                "error": {
                    "code": "not_found",
                    "message": "query=secret /private/path",
                },
            }
        }
    )

    result = asyncio.run(client.read_video(VIDEO_URL))

    assert result.failure_class == "permanent"
    assert "secret" not in str(result)


@pytest.mark.parametrize(
    "data",
    [
        {**_video(), "url": "https://evil.test/watch?v=dQw4w9WgXcQ"},
        {**_video(), "unknown": "value"},
        {**_video(), "duration_seconds": True},
    ],
)
def test_client_fails_closed_on_projected_schema_drift(
    data: dict[str, object],
) -> None:
    client, _ = _client({"read.video": _success("read.video", data)})

    result = asyncio.run(client.read_video(VIDEO_URL))

    assert result.failure_class == "permanent"


def test_production_bundle_is_exact_and_i_o_free_to_construct() -> None:
    bundle = production_youtube_backend()

    assert isinstance(bundle.client, ProductionYouTubeClient)
    assert bundle.attestation.provider_id == "yt-dlp"
    assert bundle.attestation.provider_version == "2026.7.4"
    assert bundle.attestation.operations == frozenset(
        {"search.videos", "read.video", "read.subtitles"}
    )
    assert bundle.attestation.reads_ambient_configuration is False
    assert bundle.attestation.imports_credentials is False
    assert bundle.attestation.imports_cookies is False
    assert bundle.attestation.uses_proxy is False
    assert bundle.attestation.uses_shell is False
