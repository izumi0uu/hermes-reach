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


def test_catalog_marks_only_alpha1_web_source_boundaries_implemented() -> None:
    operations = all_operations()

    assert operations
    implemented = {
        (operation.source, operation.name)
        for operation in operations
        if operation.implementation_state == "implemented"
    }
    assert implemented == {
        ("web", "read.url"),
        ("rss", "read.feed"),
        ("rss", "browse.entries"),
        ("v2ex", "browse.hot"),
        ("v2ex", "browse.node_topics"),
        ("v2ex", "read.topic"),
        ("v2ex", "read.user"),
        ("exa", "search.web"),
        ("exa", "search.code"),
    }
    assert all(operation.unavailable_reason for operation in operations)


def test_alpha1_operation_inputs_are_catalog_owned() -> None:
    rss = get_source("rss")
    v2ex = get_source("v2ex")
    web = get_source("web")
    assert rss is not None
    assert v2ex is not None
    assert web is not None

    assert [target.kind for target in get_operation(web, "read.url").targets] == ["url"]
    assert [target.kind for target in get_operation(rss, "browse.entries").targets] == [
        "url"
    ]
    node_topics = get_operation(v2ex, "browse.node_topics")
    assert node_topics is not None
    node = next(option for option in node_topics.options if option.name == "node")
    assert node.required is True
    assert node.string_format == "identifier"
    assert [
        target.string_format for target in get_operation(v2ex, "read.topic").targets
    ] == ["positive_integer"]


def test_catalog_runtime_defaults_and_account_visible_overrides_are_explicit() -> None:
    operations = all_operations()
    account_visible = {
        ("twitter", "browse.home"),
        ("facebook", "browse.feed"),
        ("facebook", "browse.groups"),
    }

    for operation in operations:
        expected_scope = (
            "account_visible"
            if (operation.source, operation.name) in account_visible
            else "public"
        )
        assert operation.runtime.data_scope == expected_scope
        assert operation.runtime.maximum_items == 20
        assert operation.runtime.maximum_characters == 16_000
        assert operation.runtime.attempt_timeout_seconds == 15
        assert operation.runtime.total_timeout_seconds == 30
        assert operation.runtime.resource_ref_eligible is False
        assert operation.runtime.continuation_eligible is False


def test_catalog_lookup_never_crosses_source_boundaries() -> None:
    github = get_source("github")

    assert github is not None
    assert get_operation(github, "search.repositories") is not None
    assert get_operation(github, "search.videos") is None
    assert get_source("unknown") is None
