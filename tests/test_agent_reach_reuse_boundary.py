from __future__ import annotations

import json
from pathlib import Path

from hermes_reach.agent_reach_bridge import (
    AGENT_REACH_COMMIT,
    AGENT_REACH_VERSION,
    BILIBILI_CLI_VERSION,
    FEEDPARSER_VERSION,
)
from hermes_reach.catalog import all_operations
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
        ("youtube", "read.comments"),
        ("reddit", "read.post"),
        ("bilibili", "search.videos"),
        ("bilibili", "read.video"),
        ("bilibili", "browse.hot"),
        ("bilibili", "browse.rank"),
    }
)

HERMES_NATIVE_EQUIVALENT = frozenset(
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
    }
)

P0_BLOCKED_NOT_IMPLEMENTED = frozenset(
    {
        ("exa", "search.web"),
        ("exa", "search.code"),
    }
)

P1_V2EX_EXCEPTIONS = frozenset(
    {
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

REACH_REIMPLEMENTATION = frozenset(
    {
        ("v2ex", "browse.hot"),
        ("v2ex", "browse.node_topics"),
        ("v2ex", "read.topic"),
        ("v2ex", "read.user"),
    }
)

FROZEN_IMPLEMENTED_CLASSIFICATIONS = (
    DIRECT_AGENT_REACH_RUNTIME,
    EXACT_BACKEND_THIN_WRAPPER,
    HERMES_NATIVE_EQUIVALENT,
    REACH_REIMPLEMENTATION,
)


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
    assert len(EXACT_BACKEND_THIN_WRAPPER) == 11
    assert len(HERMES_NATIVE_EQUIVALENT) == 9
    assert len(REACH_REIMPLEMENTATION) == 4
    assert (
        sum(operation.implementation_state == "planned" for operation in operations)
        == 39
    )


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
        HERMES_NATIVE_EQUIVALENT
        | P0_BLOCKED_NOT_IMPLEMENTED
        | P1_V2EX_EXCEPTIONS
        | P1_RSS_EXACT_WRAPPERS
        | P2_BILIBILI_EXACT_WRAPPERS
    )
    assert {
        key
        for key, review in keyed.items()
        if review["classification"] == "hermes_native_equivalent"
    } == HERMES_NATIVE_EQUIVALENT
    assert {
        key
        for key, review in keyed.items()
        if review["classification"] == "not_implemented"
    } == P0_BLOCKED_NOT_IMPLEMENTED
    assert {
        key
        for key, review in keyed.items()
        if review["classification"] == "reach_reimplementation"
    } == P1_V2EX_EXCEPTIONS
    assert {
        key
        for key, review in keyed.items()
        if review["classification"] == "exact_backend_thin_wrapper"
    } == P1_RSS_EXACT_WRAPPERS | P2_BILIBILI_EXACT_WRAPPERS

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
        if key in P0_BLOCKED_NOT_IMPLEMENTED:
            assert catalog[key].implementation_state == "planned"
            assert review["current_backend"] is None
            assert availability.state == "setup_required"
            assert availability.backend_id is None
        else:
            assert catalog[key].implementation_state == "implemented"
            assert availability.state == "available"
            assert availability.backend_id == review["current_backend"]
            if key in P1_RSS_EXACT_WRAPPERS:
                assert availability.backend_version == FEEDPARSER_VERSION
            if key in P2_BILIBILI_EXACT_WRAPPERS:
                assert availability.backend_version == BILIBILI_CLI_VERSION
