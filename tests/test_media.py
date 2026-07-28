from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
from dataclasses import replace

import pytest

import hermes_reach.tools as reach_tools
from hermes_reach.contracts import validate_browse, validate_read, validate_search
from hermes_reach.runtime.adapters import AdapterResult, MediaMetadata, RawItem
from hermes_reach.sources.media import (
    AuditedBilibiliBackend,
    AuditedYouTubeBackend,
    MediaBackendAttestation,
    bilibili_backend_is_eligible,
    youtube_backend_is_eligible,
)
from hermes_reach.sources.registry import build_alpha1_runtime
from hermes_reach.tools import reach_read


def _youtube_attestation() -> MediaBackendAttestation:
    return MediaBackendAttestation(
        provider_id="yt-dlp",
        provider_version="2026.07.24",
        operations=frozenset(
            {"search.videos", "read.video", "read.subtitles", "read.comments"}
        ),
        logs_queries=False,
        persists_content=False,
        hidden_model_processing=False,
        runtime_dependency_install=False,
        reads_ambient_configuration=False,
        imports_credentials=False,
        imports_cookies=False,
        uses_proxy=False,
        uses_browser=False,
        uses_shell=False,
        delegates_to_ytdlp=False,
    )


def _bilibili_attestation() -> MediaBackendAttestation:
    return MediaBackendAttestation(
        provider_id="bili-cli",
        provider_version="1.2.3",
        operations=frozenset(
            {"search.videos", "read.video", "browse.hot", "browse.rank"}
        ),
        logs_queries=False,
        persists_content=False,
        hidden_model_processing=False,
        runtime_dependency_install=False,
        reads_ambient_configuration=False,
        imports_credentials=False,
        imports_cookies=False,
        uses_proxy=False,
        uses_browser=False,
        uses_shell=False,
        delegates_to_ytdlp=False,
    )


class FixtureYouTubeClient:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []

    async def search_videos(self, query: str, limit: int) -> AdapterResult:
        self.calls.append(("search", query, limit))
        return AdapterResult((RawItem("search", kind="result"),))

    async def read_video(self, video_url: str) -> AdapterResult:
        self.calls.append(("video", video_url))
        return AdapterResult((RawItem("video", kind="content"),))

    async def read_subtitles(
        self, video_url: str, language: str | None
    ) -> AdapterResult:
        self.calls.append(("subtitles", video_url, language))
        return AdapterResult(
            (
                RawItem(
                    "subtitle text",
                    kind="content",
                    media=MediaMetadata(
                        subtitle_language=language or "en",
                        subtitle_origin="manual",
                    ),
                ),
            )
        )

    async def read_comments(
        self, video_url: str, limit: int, page: int
    ) -> AdapterResult:
        self.calls.append(("comments", video_url, limit, page))
        return AdapterResult((RawItem("comment", kind="entry"),))


class FixtureBilibiliClient:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []

    async def search_videos(self, query: str, limit: int) -> AdapterResult:
        self.calls.append(("search", query, limit))
        return AdapterResult((RawItem("search", kind="result"),))

    async def read_video(self, video_url: str) -> AdapterResult:
        self.calls.append(("video", video_url))
        return AdapterResult((RawItem("video", kind="content"),))

    async def browse_hot(self, limit: int) -> AdapterResult:
        self.calls.append(("hot", limit))
        return AdapterResult((RawItem("hot", kind="entry"),))

    async def browse_rank(self, limit: int) -> AdapterResult:
        self.calls.append(("rank", limit))
        return AdapterResult((RawItem("rank", kind="entry"),))


def _unexpected_side_effect(*args: object, **kwargs: object) -> None:
    del args, kwargs
    raise AssertionError(
        "media setup must not discover local binaries or configuration"
    )


def test_media_default_availability_activates_only_public_bilibili_rows() -> None:
    runtime = build_alpha1_runtime()

    assert (
        runtime.operation_availability("youtube", "search.videos").state
        == "setup_required"
    )
    assert (
        runtime.operation_availability("youtube", "read.comments").state
        == "setup_required"
    )
    assert (
        runtime.operation_availability("youtube", "transcribe.video").state
        == "unavailable"
    )
    assert runtime.operation_availability("bilibili", "read.video").state == "available"
    assert (
        runtime.operation_availability("bilibili", "read.subtitles").state
        == "unavailable"
    )
    assert (
        runtime.operation_availability("bilibili", "transcribe.video").state
        == "unavailable"
    )


def test_default_media_registry_does_not_probe_and_setup_groups_make_zero_attempts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(shutil, "which", _unexpected_side_effect)
    monkeypatch.setattr(subprocess, "run", _unexpected_side_effect)
    monkeypatch.setattr(os, "getenv", _unexpected_side_effect)
    runtime = build_alpha1_runtime()
    monkeypatch.setattr(reach_tools, "_RUNTIME", runtime)

    response = json.loads(
        asyncio.run(
            reach_read(
                {
                    "source": "youtube",
                    "operation": "read.video",
                    "target": {"url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"},
                }
            )
        )
    )

    group = response["groups"][0]
    assert group["availability"] == "setup_required"
    assert group["attempts"] == []
    assert "dQw4w9WgXcQ" not in json.dumps(response)


def test_eligible_media_backends_receive_only_their_typed_validated_methods() -> None:
    youtube_client = FixtureYouTubeClient()
    bilibili_client = FixtureBilibiliClient()
    runtime = build_alpha1_runtime(
        youtube_backend=AuditedYouTubeBackend(youtube_client, _youtube_attestation()),
        bilibili_backend=AuditedBilibiliBackend(
            bilibili_client, _bilibili_attestation()
        ),
    )
    youtube_search = validate_search(
        {
            "requests": [
                {
                    "source": "youtube",
                    "operation": "search.videos",
                    "query": "private search",
                    "options": {"limit": 2},
                }
            ]
        }
    )[0]
    subtitles = validate_read(
        {
            "source": "youtube",
            "operation": "read.subtitles",
            "target": {"url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"},
            "options": {"language": "en"},
        }
    )
    comments = validate_read(
        {
            "source": "youtube",
            "operation": "read.comments",
            "target": {"url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"},
            "options": {"limit": 2, "page": 3},
        }
    )
    rank = validate_browse(
        {
            "source": "bilibili",
            "operation": "browse.rank",
            "options": {"limit": 4},
        }
    )

    results = [
        asyncio.run(runtime.dispatch(call))
        for call in (youtube_search, subtitles, comments, rank)
    ]

    assert all(result is not None for result in results)
    assert youtube_client.calls == [
        ("search", "private search", 2),
        ("subtitles", "https://www.youtube.com/watch?v=dQw4w9WgXcQ", "en"),
        ("comments", "https://www.youtube.com/watch?v=dQw4w9WgXcQ", 2, 3),
    ]
    assert bilibili_client.calls == [("rank", 4)]
    assert results[2] is not None
    assert results[2].items[0].media == MediaMetadata(coverage="partial")
    assert results[2].selected_backend_id == "youtube-audited-backend"


def test_failed_or_cross_source_attestation_never_binds_or_attempts() -> None:
    youtube_client = FixtureYouTubeClient()
    invalid_youtube = AuditedYouTubeBackend(
        youtube_client, replace(_youtube_attestation(), imports_cookies=True)
    )
    invalid_bilibili = AuditedBilibiliBackend(
        FixtureBilibiliClient(),
        replace(_bilibili_attestation(), delegates_to_ytdlp=True),
    )
    wrong_provider = AuditedBilibiliBackend(
        FixtureBilibiliClient(), replace(_bilibili_attestation(), provider_id="yt-dlp")
    )

    assert youtube_backend_is_eligible(invalid_youtube) is False
    assert bilibili_backend_is_eligible(invalid_bilibili) is False
    assert bilibili_backend_is_eligible(wrong_provider) is False
    runtime = build_alpha1_runtime(
        youtube_backend=invalid_youtube,
        bilibili_backend=invalid_bilibili,
    )
    call = validate_search(
        {
            "requests": [
                {
                    "source": "youtube",
                    "operation": "search.videos",
                    "query": "private query",
                }
            ]
        }
    )[0]

    assert (
        runtime.operation_availability("youtube", "search.videos").state
        == "unavailable"
    )
    assert asyncio.run(runtime.dispatch(call)) is None
    assert youtube_client.calls == []
