from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_reach.agent_reach_bridge import (
    AGENT_REACH_FORK_COMMIT,
    AGENT_REACH_OFFICIAL_BASE_COMMIT,
    AGENT_REACH_PROTOCOL_VERSION,
    AGENT_REACH_VERSION,
    BILIBILI_CLI_VERSION,
    FEEDPARSER_VERSION,
    YTDLP_VERSION,
)
from hermes_reach.catalog import all_operations
from hermes_reach.runtime.adapters import AdapterBinding, AdapterRegistry, AdapterResult
from hermes_reach.sources.registry import build_alpha1_registry

ROOT = Path(__file__).resolve().parents[1]
DECISIONS = ROOT / "docs" / "agent-reach-reuse-decisions.json"
OPERATION_LEDGER = ROOT / "docs" / "agent-reach-operation-ledger.json"
REVIEW_FIELDS = frozenset(
    {
        "source",
        "operation",
        "upstream_execution",
        "classification",
        "semantic_delta",
        "approval",
        "current_backend",
        "review_milestone",
        "decision_record",
    }
)
OPERATION_LEDGER_FIELDS = frozenset(
    {
        "source",
        "operation",
        "implementation_state",
        "classification",
        "binding_surface",
        "execution_contract",
        "decision_record",
    }
)

DIRECT_AGENT_REACH_RUNTIME: frozenset[tuple[str, str]] = frozenset()

DIRECT_OWNER_FORK_RUNTIME = frozenset(
    {
        ("rss", "browse.entries"),
        ("rss", "read.feed"),
    }
)

EXACT_BACKEND_THIN_WRAPPER = frozenset(
    {
        ("youtube", "search.videos"),
        ("youtube", "read.video"),
        ("youtube", "read.subtitles"),
        ("reddit", "read.post"),
        ("bilibili", "search.videos"),
        ("bilibili", "read.video"),
        ("bilibili", "browse.hot"),
        ("bilibili", "browse.rank"),
    }
)

IMPLEMENTED_BUT_UNBOUND = frozenset({("youtube", "read.comments")})

HERMES_NATIVE_EQUIVALENT: frozenset[tuple[str, str]] = frozenset()

P0_BLOCKED_NOT_IMPLEMENTED = frozenset(
    {
        ("exa", "search.web"),
        ("exa", "search.code"),
    }
)

CLOSED_PLATFORM_EXCEPTIONS = frozenset(
    {
        ("github", "search.repositories"),
        ("github", "search.code"),
        ("github", "read.repository"),
        ("github", "read.issue"),
        ("github", "read.pull_request"),
        ("github", "browse.actions"),
        ("github", "read.action_run"),
        ("github", "browse.releases"),
        ("web", "read.url"),
        ("v2ex", "browse.hot"),
        ("v2ex", "browse.node_topics"),
        ("v2ex", "read.topic"),
        ("v2ex", "read.user"),
    }
)

P1_RSS_FORK_RUNTIME = DIRECT_OWNER_FORK_RUNTIME

P2_BILIBILI_EXACT_WRAPPERS = frozenset(
    {
        ("bilibili", "search.videos"),
        ("bilibili", "read.video"),
        ("bilibili", "browse.hot"),
        ("bilibili", "browse.rank"),
    }
)

P2_YOUTUBE_EXACT_WRAPPERS = frozenset(
    {
        ("youtube", "search.videos"),
        ("youtube", "read.video"),
        ("youtube", "read.subtitles"),
    }
)

REDDIT_CONNECTOR_EXACT_WRAPPER = frozenset({("reddit", "read.post")})

REACH_REIMPLEMENTATION: frozenset[tuple[str, str]] = frozenset()

FROZEN_IMPLEMENTED_CLASSIFICATIONS = (
    DIRECT_AGENT_REACH_RUNTIME,
    DIRECT_OWNER_FORK_RUNTIME,
    EXACT_BACKEND_THIN_WRAPPER,
    IMPLEMENTED_BUT_UNBOUND,
    HERMES_NATIVE_EQUIVALENT,
    REACH_REIMPLEMENTATION,
)


async def _unexpected_closed_platform_execution(_: object) -> AdapterResult:
    raise AssertionError("a closed platform operation reached execution")


def test_every_implemented_operation_has_one_frozen_reuse_classification() -> None:
    implemented = {
        (operation.source, operation.name)
        for operation in all_operations()
        if operation.implementation_state == "implemented"
    }

    classified: set[tuple[str, str]] = set()
    for classification in FROZEN_IMPLEMENTED_CLASSIFICATIONS:
        assert classified.isdisjoint(classification)
        classified.update(classification)

    assert classified == implemented


def test_frozen_reuse_audit_counts_are_review_visible() -> None:
    operations = all_operations()

    assert len(operations) == 63
    assert len(DIRECT_AGENT_REACH_RUNTIME) == 0
    assert len(DIRECT_OWNER_FORK_RUNTIME) == 2
    assert EXACT_BACKEND_THIN_WRAPPER == (
        P2_BILIBILI_EXACT_WRAPPERS
        | P2_YOUTUBE_EXACT_WRAPPERS
        | REDDIT_CONNECTOR_EXACT_WRAPPER
    )
    assert len(EXACT_BACKEND_THIN_WRAPPER) == 8
    assert IMPLEMENTED_BUT_UNBOUND == {("youtube", "read.comments")}
    assert HERMES_NATIVE_EQUIVALENT == frozenset()
    assert REACH_REIMPLEMENTATION == frozenset()
    assert (
        sum(operation.implementation_state == "implemented" for operation in operations)
        == 11
    )
    assert (
        sum(operation.implementation_state == "planned" for operation in operations)
        == 52
    )


def test_operation_ledger_closes_every_catalog_operation_and_fork_contract() -> None:
    ledger = json.loads(OPERATION_LEDGER.read_text(encoding="utf-8"))
    assert set(ledger) == {"schema_version", "agent_reach", "operations"}
    assert ledger["schema_version"] == "v1"
    assert ledger["agent_reach"] == {
        "version": AGENT_REACH_VERSION,
        "official_repository": "Panniantong/Agent-Reach",
        "official_base_commit": AGENT_REACH_OFFICIAL_BASE_COMMIT,
        "fork_repository": "izumi0uu/Agent-Reach",
        "fork_execution_commit": AGENT_REACH_FORK_COMMIT,
        "execution_protocol": AGENT_REACH_PROTOCOL_VERSION,
    }

    operations = ledger["operations"]
    assert isinstance(operations, list)
    catalog = all_operations()
    assert [
        (row["source"], row["operation"], row["implementation_state"])
        for row in operations
    ] == [
        (operation.source, operation.name, operation.implementation_state)
        for operation in catalog
    ]
    keyed = {(row["source"], row["operation"]): row for row in operations}
    assert len(keyed) == len(operations) == 63
    assert {
        key
        for key, row in keyed.items()
        if row["classification"] == "direct_owner_fork_runtime"
    } == DIRECT_OWNER_FORK_RUNTIME
    assert {
        key
        for key, row in keyed.items()
        if row["classification"] == "exact_backend_thin_wrapper"
    } == EXACT_BACKEND_THIN_WRAPPER
    assert {
        key
        for key, row in keyed.items()
        if row["classification"] == "implemented_but_unbound"
    } == IMPLEMENTED_BUT_UNBOUND
    assert {
        key for key, row in keyed.items() if row["classification"] == "not_implemented"
    } == {
        (operation.source, operation.name)
        for operation in catalog
        if operation.implementation_state == "planned"
    }

    default_local = (
        DIRECT_OWNER_FORK_RUNTIME
        | P2_BILIBILI_EXACT_WRAPPERS
        | P2_YOUTUBE_EXACT_WRAPPERS
    )
    for key, row in keyed.items():
        assert set(row) == OPERATION_LEDGER_FIELDS
        assert (ROOT / row["decision_record"]).is_file()
        if key in default_local:
            assert row["binding_surface"] == "default_local"
        elif key in REDDIT_CONNECTOR_EXACT_WRAPPER:
            assert row["binding_surface"] == "connector_only"
        elif key in IMPLEMENTED_BUT_UNBOUND:
            assert row["binding_surface"] == "unbound"
        else:
            assert row["binding_surface"] == "none"
        if key not in DIRECT_OWNER_FORK_RUNTIME:
            assert row["execution_contract"] is None

    shared_limits = {
        "maximum_document_bytes": 1_048_576,
        "maximum_metadata_bytes": 16_384,
        "maximum_output_bytes": 1_048_576,
        "maximum_content_type_characters": 512,
        "maximum_content_location_characters": 8_192,
        "maximum_text_characters": 16_000,
        "maximum_title_characters": 4_096,
        "maximum_url_characters": 8_192,
        "maximum_native_id_characters": 512,
        "maximum_author_characters": 2_048,
        "maximum_published_characters": 512,
    }
    for operation, argument_schema, result_schema, maximum_items in (
        ("read.feed", "rss.read.feed.arguments.v1", "rss.feed.v1", 1),
        ("browse.entries", "rss.browse.entries.arguments.v1", "rss.entry.v1", 21),
    ):
        contract = keyed[("rss", operation)]["execution_contract"]
        assert contract == {
            "protocol_version": AGENT_REACH_PROTOCOL_VERSION,
            "argument_schema_id": argument_schema,
            "result_schema_ids": [result_schema],
            "backend_id": "feedparser",
            "backend_version": FEEDPARSER_VERSION,
            "required_host_capabilities": ["fetched_document.v1"],
            "limits": {"maximum_items": maximum_items, **shared_limits},
        }


def test_governance_docs_preserve_worker_and_recovery_tag_boundaries() -> None:
    plugin_boundary = (ROOT / "docs" / "agent-reach-plugin-boundary.md").read_text(
        encoding="utf-8"
    )
    reuse_boundary = (ROOT / "docs" / "agent-reach-reuse-boundary.md").read_text(
        encoding="utf-8"
    )
    rss_decision = (
        ROOT / "docs" / "agent-reach-decisions" / "rss-feedparser-6.0.12.md"
    ).read_text(encoding="utf-8")
    normalized_plugin_boundary = " ".join(plugin_boundary.split())
    normalized_reuse_boundary = " ".join(reuse_boundary.split())
    normalized_rss_decision = " ".join(rss_decision.split())

    assert "not a kernel-level syscall sandbox" in normalized_plugin_boundary
    assert "both parent package initializers" in normalized_plugin_boundary
    assert "before any fork code is imported" in normalized_plugin_boundary
    assert "not an operating-system syscall sandbox" in normalized_rss_decision
    assert "could attempt filesystem or network syscalls" in normalized_rss_decision
    assert "never a branch or tag" in normalized_plugin_boundary
    assert "not a dependency selector" in normalized_plugin_boundary
    assert "Hermes never depends on that tag" in normalized_reuse_boundary
    assert "the exact commit pin is authoritative" in normalized_reuse_boundary


@pytest.mark.parametrize(("source", "operation"), sorted(CLOSED_PLATFORM_EXCEPTIONS))
def test_closed_platform_exceptions_cannot_be_rebound(
    source: str,
    operation: str,
) -> None:
    registry = AdapterRegistry()

    with pytest.raises(ValueError, match="implemented catalog operation"):
        registry.register(
            AdapterBinding(
                source=source,
                operation=operation,
                backend_id="forbidden-platform-backend",
                backend_version="1",
                priority=10,
                required_scope="public",
                equivalence_group="forbidden-platform-exception",
                execute=_unexpected_closed_platform_execution,
            )
        )

    assert registry.has_binding(source, operation) is False


def test_review_decisions_are_pinned_to_catalog_and_runtime_state() -> None:
    manifest = json.loads(DECISIONS.read_text(encoding="utf-8"))
    assert set(manifest) == {"schema_version", "agent_reach", "reviews"}
    assert manifest["schema_version"] == "v2"
    assert manifest["agent_reach"] == {
        "version": AGENT_REACH_VERSION,
        "official_repository": "Panniantong/Agent-Reach",
        "official_base_commit": AGENT_REACH_OFFICIAL_BASE_COMMIT,
        "fork_repository": "izumi0uu/Agent-Reach",
        "fork_execution_commit": AGENT_REACH_FORK_COMMIT,
        "execution_protocol": AGENT_REACH_PROTOCOL_VERSION,
    }

    reviews = manifest["reviews"]
    assert isinstance(reviews, list)
    keyed = {(review["source"], review["operation"]): review for review in reviews}
    assert len(keyed) == len(reviews)
    assert set(keyed) == (
        CLOSED_PLATFORM_EXCEPTIONS
        | P0_BLOCKED_NOT_IMPLEMENTED
        | P1_RSS_FORK_RUNTIME
        | P2_BILIBILI_EXACT_WRAPPERS
        | P2_YOUTUBE_EXACT_WRAPPERS
    )
    assert {
        key
        for key, review in keyed.items()
        if review["classification"] == "hermes_native_equivalent"
    } == set()
    assert {
        key
        for key, review in keyed.items()
        if review["classification"] == "not_implemented"
    } == (CLOSED_PLATFORM_EXCEPTIONS | P0_BLOCKED_NOT_IMPLEMENTED)
    assert {
        key
        for key, review in keyed.items()
        if review["classification"] == "reach_reimplementation"
    } == set()
    assert {
        key
        for key, review in keyed.items()
        if review["classification"] == "direct_owner_fork_runtime"
    } == P1_RSS_FORK_RUNTIME
    assert {
        key
        for key, review in keyed.items()
        if review["classification"] == "exact_backend_thin_wrapper"
    } == (P2_BILIBILI_EXACT_WRAPPERS | P2_YOUTUBE_EXACT_WRAPPERS)

    catalog = {
        (operation.source, operation.name): operation for operation in all_operations()
    }
    registry = build_alpha1_registry()
    for key, review in keyed.items():
        assert set(review) == REVIEW_FIELDS
        assert review["upstream_execution"]
        assert review["semantic_delta"]
        assert review["approval"].startswith("owner-approved-")
        assert review["review_milestone"]
        assert (ROOT / review["decision_record"]).is_file()
        assert catalog[key].runtime.data_scope == "public"
        availability = registry.availability(*key)
        if key in CLOSED_PLATFORM_EXCEPTIONS:
            assert catalog[key].implementation_state == "planned"
            assert review["current_backend"] is None
            assert availability.state == "unavailable"
            assert availability.backend_id is None
            assert registry.has_binding(*key) is False
        elif key in P0_BLOCKED_NOT_IMPLEMENTED:
            assert catalog[key].implementation_state == "planned"
            assert review["current_backend"] is None
            assert availability.state == "setup_required"
            assert availability.backend_id is None
            assert registry.has_binding(*key) is False
        else:
            assert catalog[key].implementation_state == "implemented"
            assert availability.state == "available"
            assert availability.backend_id == review["current_backend"]
            if key in P1_RSS_FORK_RUNTIME:
                assert availability.backend_version == FEEDPARSER_VERSION
            if key in P2_BILIBILI_EXACT_WRAPPERS:
                assert availability.backend_version == BILIBILI_CLI_VERSION
            if key in P2_YOUTUBE_EXACT_WRAPPERS:
                assert availability.backend_version == YTDLP_VERSION

    default_local_bindings = DIRECT_OWNER_FORK_RUNTIME | (
        EXACT_BACKEND_THIN_WRAPPER - REDDIT_CONNECTOR_EXACT_WRAPPER
    )
    assert len(default_local_bindings) == 9
    assert all(registry.has_binding(*key) for key in default_local_bindings)

    reddit = registry.availability("reddit", "read.post")
    assert reddit.state == "unavailable"
    assert reddit.backend_id is None
    assert registry.has_binding("reddit", "read.post") is False

    comments = registry.availability("youtube", "read.comments")
    assert comments.state == "setup_required"
    assert comments.backend_id is None
    assert registry.has_binding("youtube", "read.comments") is False
