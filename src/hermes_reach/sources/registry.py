"""I/O-free construction of the Alpha-1 source-operation registry."""

from __future__ import annotations

from ..runtime.adapters import AdapterBinding, AdapterCallable, AdapterRegistry
from ..runtime.availability import Availability
from ..runtime.dispatcher import RuntimeDispatcher
from .exa import AuditedExaClient, exa_bindings, exa_client_is_eligible
from .public_http import PublicHttpClient, PublicHttpTransport
from .rss import RssAdapter
from .v2ex import V2exAdapter
from .web import WebAdapter


def build_alpha1_registry(
    http_client: PublicHttpClient | None = None,
    exa_client: AuditedExaClient | None = None,
) -> AdapterRegistry:
    """Register deterministic adapters without probing network or secrets."""

    registry = AdapterRegistry()
    client = http_client if http_client is not None else PublicHttpTransport()
    web = WebAdapter(client)
    rss = RssAdapter(client)
    v2ex = V2exAdapter(client)

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

    if exa_client is None:
        _mark_exa(
            registry,
            "setup_required",
            "Configure a separately audited Exa client through operator setup.",
        )
    elif exa_client_is_eligible(exa_client):
        for binding in exa_bindings(exa_client):
            registry.register(binding)
    else:
        _mark_exa(
            registry,
            "unavailable",
            "The configured Exa client failed the exact-provider safety gate.",
        )
    return registry


def build_alpha1_runtime(
    http_client: PublicHttpClient | None = None,
    exa_client: AuditedExaClient | None = None,
) -> RuntimeDispatcher:
    return RuntimeDispatcher(build_alpha1_registry(http_client, exa_client))


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
