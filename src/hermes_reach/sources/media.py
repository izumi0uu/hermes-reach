"""Audited media backend boundary with closed source-specific clients."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Final, Protocol

from ..runtime.adapters import (
    AdapterBinding,
    AdapterCallable,
    AdapterResult,
    MediaMetadata,
    RawItem,
)
from ..runtime.policy import AuthorizedCall

_YOUTUBE_OPERATIONS: Final = frozenset(
    {"search.videos", "read.video", "read.subtitles", "read.comments"}
)
BILIBILI_OPERATIONS: Final = frozenset(
    {"search.videos", "read.video", "browse.hot", "browse.rank"}
)
_VERSION: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,63}")


class YouTubeMediaClient(Protocol):
    """The exact normalized methods an approved YouTube backend may receive."""

    async def search_videos(self, query: str, limit: int) -> AdapterResult: ...

    async def read_video(self, video_url: str) -> AdapterResult: ...

    async def read_subtitles(
        self, video_url: str, language: str | None
    ) -> AdapterResult: ...

    async def read_comments(
        self, video_url: str, limit: int, page: int
    ) -> AdapterResult: ...


class BilibiliMediaClient(Protocol):
    """The exact normalized methods an approved Bilibili backend may receive."""

    async def search_videos(self, query: str, limit: int) -> AdapterResult: ...

    async def read_video(self, video_url: str) -> AdapterResult: ...

    async def browse_hot(self, limit: int) -> AdapterResult: ...

    async def browse_rank(self, limit: int) -> AdapterResult: ...


@dataclass(frozen=True, slots=True)
class MediaBackendAttestation:
    """Operator-reviewed claims required before a media backend can bind."""

    provider_id: str
    provider_version: str
    operations: frozenset[str]
    logs_queries: bool
    persists_content: bool
    hidden_model_processing: bool
    runtime_dependency_install: bool
    reads_ambient_configuration: bool
    imports_credentials: bool
    imports_cookies: bool
    uses_proxy: bool
    uses_browser: bool
    uses_shell: bool
    delegates_to_ytdlp: bool


@dataclass(frozen=True, slots=True)
class AuditedYouTubeBackend:
    client: YouTubeMediaClient
    attestation: MediaBackendAttestation


@dataclass(frozen=True, slots=True)
class AuditedBilibiliBackend:
    client: BilibiliMediaClient
    attestation: MediaBackendAttestation


def youtube_backend_is_eligible(bundle: AuditedYouTubeBackend) -> bool:
    """Require a complete exact attestation without probing the backend."""

    return _attestation_is_eligible(
        bundle.attestation, provider_id="yt-dlp", operations=_YOUTUBE_OPERATIONS
    )


def bilibili_backend_is_eligible(bundle: AuditedBilibiliBackend) -> bool:
    """Reject yt-dlp and every non-bili-cli source identity by construction."""

    return _attestation_is_eligible(
        bundle.attestation, provider_id="bili-cli", operations=BILIBILI_OPERATIONS
    )


def youtube_bindings(
    bundle: AuditedYouTubeBackend,
) -> tuple[AdapterBinding, ...]:
    """Build bindings only after the source-specific safety gate passes."""

    if not youtube_backend_is_eligible(bundle):
        raise ValueError("The injected YouTube backend failed the safety gate.")
    adapter = _YouTubeAdapter(bundle.client)
    return _bindings(
        "youtube",
        _YOUTUBE_OPERATIONS,
        "youtube-audited-backend",
        bundle.attestation,
        adapter.execute,
    )


def bilibili_bindings(
    bundle: AuditedBilibiliBackend,
) -> tuple[AdapterBinding, ...]:
    """Build Bilibili bindings only for the separate approved client."""

    if not bilibili_backend_is_eligible(bundle):
        raise ValueError("The injected Bilibili backend failed the safety gate.")
    adapter = _BilibiliAdapter(bundle.client)
    return _bindings(
        "bilibili",
        BILIBILI_OPERATIONS,
        "bili-cli",
        bundle.attestation,
        adapter.execute,
    )


def _attestation_is_eligible(
    attestation: MediaBackendAttestation,
    *,
    provider_id: str,
    operations: frozenset[str],
) -> bool:
    return bool(
        attestation.provider_id == provider_id
        and _VERSION.fullmatch(attestation.provider_version) is not None
        and attestation.operations == operations
        and not attestation.logs_queries
        and not attestation.persists_content
        and not attestation.hidden_model_processing
        and not attestation.runtime_dependency_install
        and not attestation.reads_ambient_configuration
        and not attestation.imports_credentials
        and not attestation.imports_cookies
        and not attestation.uses_proxy
        and not attestation.uses_browser
        and not attestation.uses_shell
        and not attestation.delegates_to_ytdlp
    )


def _bindings(
    source: str,
    operations: frozenset[str],
    backend_id: str,
    attestation: MediaBackendAttestation,
    execute: AdapterCallable,
) -> tuple[AdapterBinding, ...]:
    return tuple(
        AdapterBinding(
            source=source,
            operation=operation,
            backend_id=backend_id,
            backend_version=attestation.provider_version,
            priority=10,
            required_scope="public",
            equivalence_group=f"{source}:{operation}:v1",
            execute=execute,
        )
        for operation in sorted(operations)
    )


class _YouTubeAdapter:
    def __init__(self, client: YouTubeMediaClient) -> None:
        self._client = client

    async def execute(self, authorized: AuthorizedCall) -> AdapterResult:
        try:
            operation = authorized.operation.name
            if operation == "search.videos":
                query = _query(authorized)
                return _result(
                    await self._client.search_videos(query, _limit(authorized))
                )
            video_url = _video_url(authorized)
            if operation == "read.video":
                return _result(await self._client.read_video(video_url))
            if operation == "read.subtitles":
                result = _result(
                    await self._client.read_subtitles(
                        video_url, _string_option(authorized, "language")
                    )
                )
                return _subtitle_result(result)
            if operation == "read.comments":
                result = _result(
                    await self._client.read_comments(
                        video_url,
                        _limit(authorized),
                        _integer_option(authorized, "page", 1),
                    )
                )
                return _comments_result(result)
            return AdapterResult(failure_class="invalid_input")
        except Exception:
            return AdapterResult(failure_class="transient")


class _BilibiliAdapter:
    def __init__(self, client: BilibiliMediaClient) -> None:
        self._client = client

    async def execute(self, authorized: AuthorizedCall) -> AdapterResult:
        try:
            operation = authorized.operation.name
            if operation == "search.videos":
                return _result(
                    await self._client.search_videos(
                        _query(authorized), _limit(authorized)
                    )
                )
            if operation == "read.video":
                return _result(await self._client.read_video(_video_url(authorized)))
            if operation == "browse.hot":
                return _result(await self._client.browse_hot(_limit(authorized)))
            if operation == "browse.rank":
                return _result(await self._client.browse_rank(_limit(authorized)))
            return AdapterResult(failure_class="invalid_input")
        except Exception:
            return AdapterResult(failure_class="transient")


def _result(value: object) -> AdapterResult:
    if not isinstance(value, AdapterResult):
        return AdapterResult(failure_class="permanent")
    return value


def _subtitle_result(result: AdapterResult) -> AdapterResult:
    if not result.is_success:
        return result
    if not result.items:
        return AdapterResult(failure_class="not_found")
    if any(
        item.media is None
        or item.media.subtitle_language is None
        or item.media.subtitle_origin is None
        for item in result.items
    ):
        return AdapterResult(failure_class="permanent")
    return result


def _comments_result(result: AdapterResult) -> AdapterResult:
    if not result.is_success:
        return result
    items = tuple(_partial_comment(item) for item in result.items)
    return AdapterResult(items, partial_failure_class=result.partial_failure_class)


def _partial_comment(item: RawItem) -> RawItem:
    media = item.media
    if media is None:
        return replace(item, media=MediaMetadata(coverage="partial"))
    if media.coverage == "unknown":
        return replace(item, media=replace(media, coverage="partial"))
    return item


def _query(authorized: AuthorizedCall) -> str:
    query = authorized.call.query
    if query is None:
        raise ValueError("query_missing")
    return query


def _video_url(authorized: AuthorizedCall) -> str:
    target = authorized.call.target
    if target is None or not isinstance(target.get("url"), str):
        raise ValueError("video_url_missing")
    return target["url"]


def _limit(authorized: AuthorizedCall) -> int:
    return _integer_option(
        authorized, "limit", authorized.operation.runtime.maximum_items
    )


def _integer_option(authorized: AuthorizedCall, name: str, default: int) -> int:
    value = authorized.call.options.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("integer_option_invalid")
    return value


def _string_option(authorized: AuthorizedCall, name: str) -> str | None:
    value = authorized.call.options.get(name)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("string_option_invalid")
    return value
