from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_reach.agent_reach_bridge import (
    AGENT_REACH_COMMIT,
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

DIRECT_AGENT_REACH_RUNTIME: frozenset[tuple[str, str]] = frozenset()

EXACT_BACKEND_THIN_WRAPPER = frozenset(
    {
        ("rss", "browse.entries"),
        ("rss", "read.feed"),
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

P1_RSS_EXACT_WRAPPERS = frozenset(
    {
        ("rss", "browse.entries"),
        ("rss", "read.feed"),
    }
)

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
    assert EXACT_BACKEND_THIN_WRAPPER == (
        P1_RSS_EXACT_WRAPPERS
        | P2_BILIBILI_EXACT_WRAPPERS
        | P2_YOUTUBE_EXACT_WRAPPERS
        | REDDIT_CONNECTOR_EXACT_WRAPPER
    )
    assert len(EXACT_BACKEND_THIN_WRAPPER) == 10
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
    assert manifest["schema_version"] == "v1"
    assert manifest["agent_reach"] == {
        "version": AGENT_REACH_VERSION,
        "commit": AGENT_REACH_COMMIT,
    }

    reviews = manifest["reviews"]
    assert isinstance(reviews, list)
    keyed = {(review["source"], review["operation"]): review for review in reviews}
    assert len(keyed) == len(reviews)
    assert set(keyed) == (
        CLOSED_PLATFORM_EXCEPTIONS
        | P0_BLOCKED_NOT_IMPLEMENTED
        | P1_RSS_EXACT_WRAPPERS
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
        if review["classification"] == "exact_backend_thin_wrapper"
    } == (
        P1_RSS_EXACT_WRAPPERS | P2_BILIBILI_EXACT_WRAPPERS | P2_YOUTUBE_EXACT_WRAPPERS
    )

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
            if key in P1_RSS_EXACT_WRAPPERS:
                assert availability.backend_version == FEEDPARSER_VERSION
            if key in P2_BILIBILI_EXACT_WRAPPERS:
                assert availability.backend_version == BILIBILI_CLI_VERSION
            if key in P2_YOUTUBE_EXACT_WRAPPERS:
                assert availability.backend_version == YTDLP_VERSION

    default_local_exact_wrappers = (
        EXACT_BACKEND_THIN_WRAPPER - REDDIT_CONNECTOR_EXACT_WRAPPER
    )
    assert len(default_local_exact_wrappers) == 9
    assert all(registry.has_binding(*key) for key in default_local_exact_wrappers)

    reddit = registry.availability("reddit", "read.post")
    assert reddit.state == "unavailable"
    assert reddit.backend_id is None
    assert registry.has_binding("reddit", "read.post") is False

    comments = registry.availability("youtube", "read.comments")
    assert comments.state == "setup_required"
    assert comments.backend_id is None
    assert registry.has_binding("youtube", "read.comments") is False
