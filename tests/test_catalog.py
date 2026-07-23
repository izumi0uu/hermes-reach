from __future__ import annotations

from hermes_reach.catalog import (
    SOURCE_CATALOG,
    all_operations,
    get_operation,
    get_source,
)

EXPECTED_OPERATIONS = {
    "github": {
        "search.repositories",
        "search.code",
        "read.repository",
        "read.issue",
        "read.pull_request",
        "browse.actions",
        "read.action_run",
        "browse.releases",
    },
    "twitter": {
        "search.posts",
        "read.post",
        "read.article",
        "browse.home",
        "browse.user_posts",
        "read.user",
    },
    "youtube": {
        "search.videos",
        "read.video",
        "read.subtitles",
        "read.comments",
        "transcribe.video",
    },
    "reddit": {
        "search.posts",
        "read.post",
        "browse.subreddit",
        "browse.hot",
        "browse.popular",
        "browse.all",
        "read.subreddit",
    },
    "facebook": {"search", "read.profile", "browse.feed", "browse.groups"},
    "instagram": {
        "search.users",
        "read.profile",
        "browse.user_posts",
        "browse.explore",
    },
    "bilibili": {
        "search.videos",
        "read.video",
        "read.subtitles",
        "browse.hot",
        "browse.rank",
        "transcribe.video",
    },
    "xiaohongshu": {
        "search.notes",
        "read.note",
        "read.comments",
        "browse.feed",
        "browse.user_posts",
    },
    "linkedin": {
        "search.people",
        "search.jobs",
        "read.person_profile",
        "read.company_profile",
    },
    "xiaoyuzhou": {"transcribe.episode"},
    "v2ex": {"browse.hot", "browse.node_topics", "read.topic", "read.user"},
    "xueqiu": {
        "search.stocks",
        "read.stock_quote",
        "browse.hot_posts",
        "browse.hot_stocks",
    },
    "rss": {"read.feed", "browse.entries"},
    "exa": {"search.web", "search.code"},
    "web": {"read.url"},
}


def test_catalog_covers_every_parent_matrix_source_and_operation() -> None:
    assert {source.name for source in SOURCE_CATALOG} == set(EXPECTED_OPERATIONS)
    assert {
        source.name: {operation.name for operation in source.operations}
        for source in SOURCE_CATALOG
    } == EXPECTED_OPERATIONS


def test_foundation_catalog_has_no_available_operation() -> None:
    operations = all_operations()

    assert operations
    assert {operation.implementation_state for operation in operations} == {"planned"}
    assert all(operation.unavailable_reason for operation in operations)


def test_catalog_lookup_never_crosses_source_boundaries() -> None:
    github = get_source("github")

    assert github is not None
    assert get_operation(github, "search.repositories") is not None
    assert get_operation(github, "search.videos") is None
    assert get_source("unknown") is None
