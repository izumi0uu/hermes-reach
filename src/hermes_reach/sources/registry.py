"""I/O-free construction of the Alpha-1 source-operation registry."""

from __future__ import annotations

from ..agent_reach_bridge import FEEDPARSER_VERSION
from ..catalog import EXA_SETUP_REQUIRED_REASON
from ..runtime.adapters import AdapterBinding, AdapterCallable, AdapterRegistry
from ..runtime.availability import Availability
from ..runtime.dispatcher import RuntimeDispatcher
from .bilibili import production_bilibili_backend
from .media import (
    AuditedBilibiliBackend,
    AuditedYouTubeBackend,
    bilibili_backend_is_eligible,
    bilibili_bindings,
    youtube_backend_is_eligible,
    youtube_bindings,
)
from .public_http import PublicHttpClient, PublicHttpTransport
from .rss import RssAdapter
from .youtube import production_youtube_backend


def build_alpha1_registry(
    http_client: PublicHttpClient | None = None,
    exa_client: None = None,
    youtube_backend: AuditedYouTubeBackend | None = None,
    bilibili_backend: AuditedBilibiliBackend | None = None,
) -> AdapterRegistry:
    """Register deterministic adapters without probing network or secrets."""

    if exa_client is not None:
        raise TypeError("Exa client injection is no longer supported.")
    registry = AdapterRegistry()
    client = http_client if http_client is not None else PublicHttpTransport()
    rss = RssAdapter(client)

    _register(
        registry,
        "rss",
        "read.feed",
        "feedparser",
        rss.execute,
        backend_version=FEEDPARSER_VERSION,
    )
    _register(
        registry,
        "rss",
        "browse.entries",
        "feedparser",
        rss.execute,
        backend_version=FEEDPARSER_VERSION,
    )

    _mark_exa(
        registry,
        "setup_required",
        EXA_SETUP_REQUIRED_REASON,
    )
    _register_media_backends(registry, youtube_backend, bilibili_backend)
    return registry


def build_alpha1_runtime(
    http_client: PublicHttpClient | None = None,
    exa_client: None = None,
    youtube_backend: AuditedYouTubeBackend | None = None,
    bilibili_backend: AuditedBilibiliBackend | None = None,
) -> RuntimeDispatcher:
    return RuntimeDispatcher(
        build_alpha1_registry(
            http_client,
            exa_client,
            youtube_backend,
            bilibili_backend,
        )
    )


def _register(
    registry: AdapterRegistry,
    source: str,
    operation: str,
    backend_id: str,
    execute: AdapterCallable,
    *,
    backend_version: str = "1",
) -> None:
    registry.register(
        AdapterBinding(
            source=source,
            operation=operation,
            backend_id=backend_id,
            backend_version=backend_version,
            priority=10,
            required_scope="public",
            equivalence_group=f"{source}:{operation}:v1",
            execute=execute,
        )
    )


def _mark_exa(registry: AdapterRegistry, state: Availability, reason: str) -> None:
    for operation in ("search.web", "search.code"):
        registry.mark(
            "exa",
            operation,
            state,
            reason,
        )


def _register_media_backends(
    registry: AdapterRegistry,
    youtube_backend: AuditedYouTubeBackend | None,
    bilibili_backend: AuditedBilibiliBackend | None,
) -> None:
    if youtube_backend is None:
        youtube_backend = production_youtube_backend()
    if youtube_backend_is_eligible(youtube_backend):
        for binding in youtube_bindings(youtube_backend):
            registry.register(binding)
        _mark_media(
            registry,
            "youtube",
            ("read.comments",),
            "setup_required",
            "The yt-dlp backend cannot satisfy stable comment pagination.",
        )
    else:
        _mark_media(
            registry,
            "youtube",
            ("search.videos", "read.video", "read.subtitles"),
            "unavailable",
            "The configured YouTube backend failed the exact safety gate.",
        )
        _mark_media(
            registry,
            "youtube",
            ("read.comments",),
            "setup_required",
            "The yt-dlp backend cannot satisfy stable comment pagination.",
        )

    if bilibili_backend is None:
        for binding in bilibili_bindings(production_bilibili_backend()):
            registry.register(binding)
    elif bilibili_backend_is_eligible(bilibili_backend):
        for binding in bilibili_bindings(bilibili_backend):
            registry.register(binding)
    else:
        _mark_media(
            registry,
            "bilibili",
            ("search.videos", "read.video", "browse.hot", "browse.rank"),
            "unavailable",
            "The configured Bilibili backend failed the exact safety gate.",
        )


def _mark_media(
    registry: AdapterRegistry,
    source: str,
    operations: tuple[str, ...],
    state: Availability,
    reason: str,
) -> None:
    for operation in operations:
        registry.mark(source, operation, state, reason)
