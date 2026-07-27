from __future__ import annotations

from hermes_reach.catalog import all_operations

DIRECT_AGENT_REACH_RUNTIME: frozenset[tuple[str, str]] = frozenset()

EXACT_BACKEND_THIN_WRAPPER = frozenset(
    {
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
        ("exa", "search.web"),
        ("exa", "search.code"),
        ("web", "read.url"),
    }
)

REACH_REIMPLEMENTATION = frozenset(
    {
        ("v2ex", "browse.hot"),
        ("v2ex", "browse.node_topics"),
        ("v2ex", "read.topic"),
        ("v2ex", "read.user"),
        ("rss", "read.feed"),
        ("rss", "browse.entries"),
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
    assert len(EXACT_BACKEND_THIN_WRAPPER) == 9
    assert len(HERMES_NATIVE_EQUIVALENT) == 11
    assert len(REACH_REIMPLEMENTATION) == 6
    assert (
        sum(operation.implementation_state == "planned" for operation in operations)
        == 37
    )
