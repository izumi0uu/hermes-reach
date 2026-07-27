"""I/O-free construction of the Alpha-1 source-operation registry."""

from __future__ import annotations

from ..catalog import EXA_SETUP_REQUIRED_REASON
from ..runtime.adapters import AdapterBinding, AdapterCallable, AdapterRegistry
from ..runtime.availability import Availability
from ..runtime.dispatcher import RuntimeDispatcher
from .github import GitHubAdapter
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
from .v2ex import V2exAdapter
from .web import WebAdapter


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
    web = WebAdapter(client)
    rss = RssAdapter(client)
    v2ex = V2exAdapter(client)
    github = GitHubAdapter(client)

    _register(registry, "web", "read.url", "web-public-http-v1", web.execute)
    _register(registry, "rss", "read.feed", "rss-atom-parser-v1", rss.execute)
    _register(registry, "rss", "browse.entries", "rss-atom-parser-v1", rss.execute)
    for operation in (
        "browse.hot",
        "browse.node_topics",
        "read.topic",
        "read.user",
    ):
        _register(
            registry,
            "v2ex",
            operation,
            "v2ex-public-api-v1",
            v2ex.execute,
        )

    for operation in (
        "search.repositories",
        "search.code",
        "read.repository",
        "read.issue",
        "read.pull_request",
        "browse.actions",
        "read.action_run",
        "browse.releases",
    ):
        _register(
            registry,
            "github",
            operation,
            "github-public-rest-v1",
            github.execute,
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
) -> None:
    registry.register(
        AdapterBinding(
            source=source,
            operation=operation,
            backend_id=backend_id,
            backend_version="1",
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
        _mark_media(
            registry,
            "youtube",
            ("search.videos", "read.video", "read.subtitles", "read.comments"),
            "setup_required",
            "Configure an audited YouTube backend through operator setup.",
        )
    elif youtube_backend_is_eligible(youtube_backend):
        for binding in youtube_bindings(youtube_backend):
            registry.register(binding)
    else:
        _mark_media(
            registry,
            "youtube",
            ("search.videos", "read.video", "read.subtitles", "read.comments"),
            "unavailable",
            "The configured YouTube backend failed the exact safety gate.",
        )

    if bilibili_backend is None:
        _mark_media(
            registry,
            "bilibili",
            ("search.videos", "read.video", "browse.hot", "browse.rank"),
            "setup_required",
            "Configure an audited Bilibili backend through operator setup.",
        )
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
