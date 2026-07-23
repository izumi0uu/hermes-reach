"""Immutable source-operation catalog for the Hermes Reach public contract."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final, Literal

ToolFamily = Literal["search", "read", "browse", "transcribe", "status"]
AccessClass = Literal["credential_free", "api_key", "account_session", "unsupported"]
ImplementationState = Literal["planned"]
OptionKind = Literal["integer", "boolean", "string"]

CATALOG_VERSION: Final = "v1"
PROTOCOL_VERSION: Final = "v1"


@dataclass(frozen=True, slots=True)
class OptionSpec:
    """A closed, source-operation option accepted by the runtime validator."""

    name: str
    kind: OptionKind
    minimum: int | None = None
    maximum: int | None = None


@dataclass(frozen=True, slots=True)
class OperationSpec:
    """One versioned operation owned by a source and tool family."""

    source: str
    name: str
    tool: ToolFamily
    alpha_wave: int
    access_class: AccessClass
    options: tuple[OptionSpec, ...]
    implementation_state: ImplementationState = "planned"
    unavailable_reason: str = "No adapter is implemented for this operation yet."


@dataclass(frozen=True, slots=True)
class SourceSpec:
    """A canonical source and its explicitly supported read-only operations."""

    name: str
    display_name: str
    alpha_wave: int
    access_class: AccessClass
    operations: tuple[OperationSpec, ...]


LIMIT: Final = OptionSpec(name="limit", kind="integer", minimum=1, maximum=50)
PAGE: Final = OptionSpec(name="page", kind="integer", minimum=1, maximum=100)
LANGUAGE: Final = OptionSpec(name="language", kind="string", maximum=32)


def _operation(
    source: str,
    name: str,
    tool: ToolFamily,
    wave: int,
    access_class: AccessClass,
    options: tuple[OptionSpec, ...] = (),
) -> OperationSpec:
    return OperationSpec(
        source=source,
        name=name,
        tool=tool,
        alpha_wave=wave,
        access_class=access_class,
        options=options,
    )


def _source(
    name: str,
    display_name: str,
    wave: int,
    access_class: AccessClass,
    operations: tuple[OperationSpec, ...],
) -> SourceSpec:
    return SourceSpec(
        name=name,
        display_name=display_name,
        alpha_wave=wave,
        access_class=access_class,
        operations=operations,
    )


SOURCE_CATALOG: Final[tuple[SourceSpec, ...]] = (
    _source(
        "github",
        "GitHub",
        1,
        "credential_free",
        (
            _operation(
                "github",
                "search.repositories",
                "search",
                1,
                "credential_free",
                (LIMIT,),
            ),
            _operation(
                "github", "search.code", "search", 1, "credential_free", (LIMIT,)
            ),
            _operation("github", "read.repository", "read", 1, "credential_free"),
            _operation("github", "read.issue", "read", 1, "credential_free"),
            _operation("github", "read.pull_request", "read", 1, "credential_free"),
            _operation(
                "github", "browse.actions", "browse", 1, "credential_free", (LIMIT,)
            ),
            _operation("github", "read.action_run", "read", 1, "credential_free"),
            _operation(
                "github", "browse.releases", "browse", 1, "credential_free", (LIMIT,)
            ),
        ),
    ),
    _source(
        "twitter",
        "Twitter/X",
        2,
        "account_session",
        (
            _operation(
                "twitter", "search.posts", "search", 2, "account_session", (LIMIT,)
            ),
            _operation("twitter", "read.post", "read", 2, "account_session"),
            _operation("twitter", "read.article", "read", 2, "account_session"),
            _operation(
                "twitter", "browse.home", "browse", 2, "account_session", (LIMIT,)
            ),
            _operation(
                "twitter", "browse.user_posts", "browse", 2, "account_session", (LIMIT,)
            ),
            _operation("twitter", "read.user", "read", 2, "account_session"),
        ),
    ),
    _source(
        "youtube",
        "YouTube",
        1,
        "credential_free",
        (
            _operation(
                "youtube", "search.videos", "search", 1, "credential_free", (LIMIT,)
            ),
            _operation("youtube", "read.video", "read", 1, "credential_free"),
            _operation(
                "youtube", "read.subtitles", "read", 1, "credential_free", (LANGUAGE,)
            ),
            _operation(
                "youtube", "read.comments", "read", 1, "credential_free", (LIMIT, PAGE)
            ),
            _operation(
                "youtube", "transcribe.video", "transcribe", 1, "api_key", (LANGUAGE,)
            ),
        ),
    ),
    _source(
        "reddit",
        "Reddit",
        2,
        "account_session",
        (
            _operation(
                "reddit", "search.posts", "search", 2, "account_session", (LIMIT,)
            ),
            _operation("reddit", "read.post", "read", 2, "account_session"),
            _operation(
                "reddit", "browse.subreddit", "browse", 2, "account_session", (LIMIT,)
            ),
            _operation(
                "reddit", "browse.hot", "browse", 2, "account_session", (LIMIT,)
            ),
            _operation(
                "reddit", "browse.popular", "browse", 2, "account_session", (LIMIT,)
            ),
            _operation(
                "reddit", "browse.all", "browse", 2, "account_session", (LIMIT,)
            ),
            _operation("reddit", "read.subreddit", "read", 2, "account_session"),
        ),
    ),
    _source(
        "facebook",
        "Facebook",
        3,
        "account_session",
        (
            _operation("facebook", "search", "search", 3, "account_session", (LIMIT,)),
            _operation("facebook", "read.profile", "read", 3, "account_session"),
            _operation(
                "facebook", "browse.feed", "browse", 3, "account_session", (LIMIT,)
            ),
            _operation(
                "facebook", "browse.groups", "browse", 3, "account_session", (LIMIT,)
            ),
        ),
    ),
    _source(
        "instagram",
        "Instagram",
        3,
        "account_session",
        (
            _operation(
                "instagram", "search.users", "search", 3, "account_session", (LIMIT,)
            ),
            _operation("instagram", "read.profile", "read", 3, "account_session"),
            _operation(
                "instagram",
                "browse.user_posts",
                "browse",
                3,
                "account_session",
                (LIMIT,),
            ),
            _operation(
                "instagram", "browse.explore", "browse", 3, "account_session", (LIMIT,)
            ),
        ),
    ),
    _source(
        "bilibili",
        "Bilibili",
        1,
        "credential_free",
        (
            _operation(
                "bilibili", "search.videos", "search", 1, "credential_free", (LIMIT,)
            ),
            _operation("bilibili", "read.video", "read", 1, "credential_free"),
            _operation(
                "bilibili", "read.subtitles", "read", 1, "account_session", (LANGUAGE,)
            ),
            _operation(
                "bilibili", "browse.hot", "browse", 1, "credential_free", (LIMIT,)
            ),
            _operation(
                "bilibili", "browse.rank", "browse", 1, "credential_free", (LIMIT,)
            ),
            _operation(
                "bilibili", "transcribe.video", "transcribe", 1, "api_key", (LANGUAGE,)
            ),
        ),
    ),
    _source(
        "xiaohongshu",
        "Xiaohongshu",
        2,
        "account_session",
        (
            _operation(
                "xiaohongshu", "search.notes", "search", 2, "account_session", (LIMIT,)
            ),
            _operation("xiaohongshu", "read.note", "read", 2, "account_session"),
            _operation(
                "xiaohongshu",
                "read.comments",
                "read",
                2,
                "account_session",
                (LIMIT, PAGE),
            ),
            _operation(
                "xiaohongshu", "browse.feed", "browse", 2, "account_session", (LIMIT,)
            ),
            _operation(
                "xiaohongshu",
                "browse.user_posts",
                "browse",
                2,
                "account_session",
                (LIMIT,),
            ),
        ),
    ),
    _source(
        "linkedin",
        "LinkedIn",
        3,
        "account_session",
        (
            _operation(
                "linkedin", "search.people", "search", 3, "account_session", (LIMIT,)
            ),
            _operation(
                "linkedin", "search.jobs", "search", 3, "account_session", (LIMIT,)
            ),
            _operation("linkedin", "read.person_profile", "read", 3, "account_session"),
            _operation(
                "linkedin", "read.company_profile", "read", 3, "account_session"
            ),
        ),
    ),
    _source(
        "xiaoyuzhou",
        "Xiaoyuzhou",
        2,
        "api_key",
        (
            _operation(
                "xiaoyuzhou",
                "transcribe.episode",
                "transcribe",
                2,
                "api_key",
                (LANGUAGE,),
            ),
        ),
    ),
    _source(
        "v2ex",
        "V2EX",
        1,
        "credential_free",
        (
            _operation("v2ex", "browse.hot", "browse", 1, "credential_free", (LIMIT,)),
            _operation(
                "v2ex",
                "browse.node_topics",
                "browse",
                1,
                "credential_free",
                (LIMIT, PAGE),
            ),
            _operation("v2ex", "read.topic", "read", 1, "credential_free"),
            _operation("v2ex", "read.user", "read", 1, "credential_free"),
        ),
    ),
    _source(
        "xueqiu",
        "Xueqiu",
        2,
        "account_session",
        (
            _operation(
                "xueqiu", "search.stocks", "search", 2, "account_session", (LIMIT,)
            ),
            _operation("xueqiu", "read.stock_quote", "read", 2, "account_session"),
            _operation(
                "xueqiu", "browse.hot_posts", "browse", 2, "account_session", (LIMIT,)
            ),
            _operation(
                "xueqiu", "browse.hot_stocks", "browse", 2, "account_session", (LIMIT,)
            ),
        ),
    ),
    _source(
        "rss",
        "RSS/Atom",
        1,
        "credential_free",
        (
            _operation("rss", "read.feed", "read", 1, "credential_free"),
            _operation(
                "rss", "browse.entries", "browse", 1, "credential_free", (LIMIT,)
            ),
        ),
    ),
    _source(
        "exa",
        "Exa",
        1,
        "api_key",
        (
            _operation("exa", "search.web", "search", 1, "api_key", (LIMIT,)),
            _operation("exa", "search.code", "search", 1, "api_key", (LIMIT,)),
        ),
    ),
    _source(
        "web",
        "Generic Web",
        1,
        "credential_free",
        (_operation("web", "read.url", "read", 1, "credential_free"),),
    ),
)

_SOURCE_BY_NAME: Final[Mapping[str, SourceSpec]] = MappingProxyType(
    {source.name: source for source in SOURCE_CATALOG}
)


def get_source(name: str) -> SourceSpec | None:
    """Return a source by canonical ID without accepting aliases."""

    return _SOURCE_BY_NAME.get(name)


def get_operation(source: SourceSpec, name: str) -> OperationSpec | None:
    """Return an operation from one source without cross-source routing."""

    return next(
        (operation for operation in source.operations if operation.name == name), None
    )


def all_operations() -> tuple[OperationSpec, ...]:
    """Return the catalog's immutable operation projection in source order."""

    return tuple(
        operation for source in SOURCE_CATALOG for operation in source.operations
    )
