from __future__ import annotations

import asyncio
from collections.abc import Iterator, Mapping

import pytest

from hermes_reach.normalized import MAX_NORMALIZED_INTEGER
from hermes_reach.runtime.adapters import MediaMetadata
from hermes_reach.sources.youtube import (
    ProductionYouTubeClient,
    production_youtube_backend,
)
from hermes_reach.sources.youtube_worker import WorkerOperation

VIDEO_ID = "dQw4w9WgXcQ"
VIDEO_URL = f"https://www.youtube.com/watch?v={VIDEO_ID}"
OTHER_VIDEO_ID = "aaaaaaaaaaa"
OTHER_VIDEO_URL = f"https://www.youtube.com/watch?v={OTHER_VIDEO_ID}"


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


def _fork_video_item(**overrides: object) -> dict[str, object]:
    return {
        "text": "Video description",
        "native_id": VIDEO_ID,
        "title": "Video title",
        "url": VIDEO_URL,
        "author": "Channel",
        "published_at": "2009-10-25",
        "duration_seconds": 213,
        "view_count": 42,
        "comment_count": 7,
        **overrides,
    }


def _fork_video_result(
    *,
    item: object | None = None,
    truncated: object = False,
) -> dict[str, object]:
    return {
        "item": _fork_video_item() if item is None else item,
        "truncated": truncated,
    }


class BrokenMapping(Mapping[str, object]):
    def __getitem__(self, _key: str) -> object:
        raise RuntimeError("private mapping detail")

    def __iter__(self) -> Iterator[str]:
        raise RuntimeError("private mapping detail")

    def __len__(self) -> int:
        return 1


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
            "read.video": _success(
                "read.video",
                _fork_video_result(truncated=True),
            ),
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
    assert video.items[0].text == "Video description"
    assert video.items[0].native_id == VIDEO_ID
    assert video.items[0].title == "Video title"
    assert video.items[0].url == VIDEO_URL
    assert video.items[0].author == "Channel"
    assert video.items[0].published_at == "2009-10-25"
    assert video.items[0].media == MediaMetadata(
        duration_seconds=213,
        view_count=42,
        comment_count=7,
        coverage="complete",
    )
    assert video.truncated is True
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
        {**_fork_video_result(), "unknown": "value"},
        {"truncated": False},
        {"item": None, "truncated": False},
        BrokenMapping(),
        _fork_video_result(truncated=1),
        _fork_video_result(item={**_fork_video_item(), "unknown": "value"}),
        _fork_video_result(
            item={
                name: value
                for name, value in _fork_video_item().items()
                if name != "comment_count"
            }
        ),
        _fork_video_result(item=_fork_video_item(text="")),
        _fork_video_result(item=_fork_video_item(title=None)),
        _fork_video_result(item=_fork_video_item(author="")),
        _fork_video_result(item=_fork_video_item(text="value\x00hidden")),
        _fork_video_result(item=_fork_video_item(text="\ud800")),
        _fork_video_result(item=_fork_video_item(text="x" * 16_001)),
        _fork_video_result(item=_fork_video_item(title=("😀" * 256) + "x")),
        _fork_video_result(item=_fork_video_item(author=("中" * 341) + "文")),
        _fork_video_result(item=_fork_video_item(native_id="invalid")),
        _fork_video_result(
            item=_fork_video_item(url="https://www.youtube.com/watch?v=aaaaaaaaaaa")
        ),
        _fork_video_result(item=_fork_video_item(published_at="1969-12-31")),
        _fork_video_result(item=_fork_video_item(published_at="2026-02-31")),
        _fork_video_result(item=_fork_video_item(published_at=20260731)),
        _fork_video_result(item=_fork_video_item(duration_seconds=True)),
        _fork_video_result(item=_fork_video_item(duration_seconds=1.0)),
        _fork_video_result(item=_fork_video_item(view_count=-1)),
        _fork_video_result(
            item=_fork_video_item(comment_count=MAX_NORMALIZED_INTEGER + 1)
        ),
    ],
)
def test_client_fails_closed_on_fork_read_video_schema_drift(data: object) -> None:
    client, _ = _client({"read.video": _success("read.video", data)})

    result = asyncio.run(client.read_video(VIDEO_URL))

    assert result.failure_class == "permanent"


def test_read_video_accepts_exact_text_limit_and_nullable_fields() -> None:
    text = "😀" * 16_000
    client, _ = _client(
        {
            "read.video": _success(
                "read.video",
                _fork_video_result(
                    item=_fork_video_item(
                        text=text,
                        author=None,
                        published_at=None,
                        duration_seconds=None,
                        view_count=None,
                        comment_count=None,
                    )
                ),
            )
        }
    )

    result = asyncio.run(client.read_video(VIDEO_URL))

    assert result.failure_class is None
    assert result.truncated is False
    assert result.items[0].text == text
    assert result.items[0].author is None
    assert result.items[0].published_at is None
    assert result.items[0].media == MediaMetadata(coverage="complete")


def test_parent_correlates_read_results_with_the_original_request_url() -> None:
    client, _ = _client(
        {
            "read.video": _success(
                "read.video",
                _fork_video_result(
                    item=_fork_video_item(
                        native_id=OTHER_VIDEO_ID,
                        url=OTHER_VIDEO_URL,
                    )
                ),
            ),
            "read.subtitles": _success(
                "read.subtitles",
                {
                    "id": OTHER_VIDEO_ID,
                    "title": "Other video",
                    "language": "en",
                    "origin": "manual",
                    "text": "WEBVTT\n\n00:00.000 --> 00:01.000\nOther",
                    "truncated": False,
                    "url": OTHER_VIDEO_URL,
                },
            ),
        }
    )

    video = asyncio.run(client.read_video(VIDEO_URL))
    subtitles = asyncio.run(client.read_subtitles(VIDEO_URL, "en"))

    assert video.failure_class == "permanent"
    assert subtitles.failure_class == "permanent"
    assert video.items == ()
    assert subtitles.items == ()


@pytest.mark.parametrize("field", ["title", "author"])
@pytest.mark.parametrize("character", ["a", "\u4e2d", "\U0001f600"])
def test_read_video_revalidates_exact_source_text_byte_boundaries(
    field: str,
    character: str,
) -> None:
    width = len(character.encode("utf-8"))
    count = 1_024 // width
    exact = (character * count) + ("a" * (1_024 - (count * width)))
    oversized = exact + "a"
    assert len(exact.encode("utf-8")) == 1_024
    assert len(oversized.encode("utf-8")) == 1_025
    exact_client, _ = _client(
        {
            "read.video": _success(
                "read.video",
                _fork_video_result(item=_fork_video_item(**{field: exact})),
            )
        }
    )
    oversized_client, _ = _client(
        {
            "read.video": _success(
                "read.video",
                _fork_video_result(item=_fork_video_item(**{field: oversized})),
            )
        }
    )

    exact_result = asyncio.run(exact_client.read_video(VIDEO_URL))
    oversized_result = asyncio.run(oversized_client.read_video(VIDEO_URL))

    assert exact_result.failure_class is None
    assert getattr(exact_result.items[0], field) == exact
    assert oversized_result.failure_class == "permanent"


def test_search_and_subtitles_keep_the_legacy_worker_projection() -> None:
    client, _ = _client(
        {
            "search.videos": _success(
                "search.videos",
                [
                    {
                        **_video(),
                        "description": " Legacy\nsearch text ",
                        "uploader": " Legacy channel ",
                    }
                ],
            ),
            "read.subtitles": _success(
                "read.subtitles",
                {
                    "id": VIDEO_ID,
                    "title": " Legacy subtitle title ",
                    "language": "en",
                    "origin": "automatic",
                    "text": "WEBVTT\n\n00:00.000 --> 00:01.000\nLegacy",
                    "truncated": False,
                    "url": VIDEO_URL,
                },
            ),
        }
    )

    search = asyncio.run(client.search_videos("query", 1))
    subtitles = asyncio.run(client.read_subtitles(VIDEO_URL, None))

    assert search.items[0].text == "Legacy search text"
    assert search.items[0].author == "Legacy channel"
    assert search.items[0].published_at == "2009-10-25"
    assert search.items[0].kind == "result"
    assert subtitles.items[0].text.endswith("Legacy")
    assert subtitles.items[0].title == "Legacy subtitle title"
    assert subtitles.items[0].media == MediaMetadata(
        subtitle_language="en",
        subtitle_origin="automatic",
        coverage="complete",
    )
    assert subtitles.truncated is False


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
