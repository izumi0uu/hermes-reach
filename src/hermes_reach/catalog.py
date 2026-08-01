"""Immutable source-operation catalog for the Hermes Reach public contract."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final, Literal

ToolFamily = Literal["search", "read", "browse", "transcribe", "status"]
AccessClass = Literal["credential_free", "api_key", "account_session", "unsupported"]
ImplementationState = Literal["planned", "implemented"]
OptionKind = Literal["integer", "boolean", "string"]
StringFormat = Literal[
    "text",
    "identifier",
    "positive_integer",
    "github_repository",
    "github_resource",
    "reddit_post_url",
    "subreddit_identifier",
    "social_username",
    "youtube_video_url",
    "bilibili_video_url",
]
TargetKind = Literal["url", "native_id", "resource_ref", "local_file"]
DataScope = Literal["public", "account_visible"]

CATALOG_VERSION: Final = "v1"
PROTOCOL_VERSION: Final = "v1"
EXA_SETUP_REQUIRED_REASON: Final = (
    "The exact Agent-Reach-selected mcporter artifact bundle requires setup."
)
EXA_CODE_UNAVAILABLE_REASON: Final = (
    "The Agent-Reach-selected Exa code method has an incompatible deprecated "
    "live contract."
)
WEB_UNAVAILABLE_REASON: Final = (
    "The pinned Agent-Reach Web callable remains frozen pending a bounded, "
    "cancellable execution review."
)
GITHUB_UNAVAILABLE_REASON: Final = (
    "The Agent-Reach-selected gh backend remains frozen pending a credential-free, "
    "read-only execution review."
)


@dataclass(frozen=True, slots=True)
class OptionSpec:
    """A closed, source-operation option accepted by the runtime validator."""

    name: str
    kind: OptionKind
    minimum: int | None = None
    maximum: int | None = None
    required: bool = False
    string_format: StringFormat = "text"


@dataclass(frozen=True, slots=True)
class TargetSpec:
    """One catalog-owned routing target accepted by an operation."""

    kind: TargetKind
    maximum: int = 4096
    string_format: StringFormat = "text"


@dataclass(frozen=True, slots=True)
class OperationRuntimeSpec:
    """Conservative execution limits and scope for a catalog operation."""

    data_scope: DataScope = "public"
    maximum_items: int = 20
    maximum_characters: int = 16_000
    attempt_timeout_seconds: int = 15
    total_timeout_seconds: int = 30
    resource_ref_eligible: bool = False
    continuation_eligible: bool = False


@dataclass(frozen=True, slots=True)
class OperationSpec:
    """One versioned operation owned by a source and tool family."""

    source: str
    name: str
    tool: ToolFamily
    alpha_wave: int
    access_class: AccessClass
    options: tuple[OptionSpec, ...]
    targets: tuple[TargetSpec, ...] = ()
    runtime: OperationRuntimeSpec = OperationRuntimeSpec()
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
LANGUAGE: Final = OptionSpec(
    name="language", kind="string", maximum=32, string_format="identifier"
)
NODE: Final = OptionSpec(
    name="node",
    kind="string",
    maximum=64,
    required=True,
    string_format="identifier",
)
URL_TARGET: Final = TargetSpec("url")
NATIVE_ID_TARGET: Final = TargetSpec("native_id")
POSITIVE_ID_TARGET: Final = TargetSpec(
    "native_id", maximum=32, string_format="positive_integer"
)
USERNAME_TARGET: Final = TargetSpec("native_id", maximum=64, string_format="identifier")
RESOURCE_REF_TARGET: Final = TargetSpec("resource_ref")
LOCAL_FILE_TARGET: Final = TargetSpec("local_file")
GITHUB_REPOSITORY_TARGET: Final = TargetSpec(
    "native_id", maximum=140, string_format="github_repository"
)
GITHUB_RESOURCE_TARGET: Final = TargetSpec(
    "native_id", maximum=160, string_format="github_resource"
)
YOUTUBE_VIDEO_TARGET: Final = TargetSpec(
    "url", maximum=128, string_format="youtube_video_url"
)
BILIBILI_VIDEO_TARGET: Final = TargetSpec(
    "url", maximum=128, string_format="bilibili_video_url"
)
REDDIT_POST_TARGET: Final = TargetSpec(
    "url", maximum=320, string_format="reddit_post_url"
)
SUBREDDIT_TARGET: Final = TargetSpec(
    "native_id", maximum=21, string_format="subreddit_identifier"
)
SOCIAL_USERNAME_TARGET: Final = TargetSpec(
    "native_id", maximum=64, string_format="social_username"
)


def _operation(
    source: str,
    name: str,
    tool: ToolFamily,
    wave: int,
    access_class: AccessClass,
    options: tuple[OptionSpec, ...] = (),
    data_scope: DataScope = "public",
    resource_ref_eligible: bool = False,
    continuation_eligible: bool = False,
    *,
    targets: tuple[TargetSpec, ...] | None = None,
    attempt_timeout_seconds: int = 15,
    total_timeout_seconds: int = 30,
    implementation_state: ImplementationState = "planned",
    unavailable_reason: str = "No adapter is implemented for this operation yet.",
) -> OperationSpec:
    operation_targets = _default_targets(tool) if targets is None else targets
    return OperationSpec(
        source=source,
        name=name,
        tool=tool,
        alpha_wave=wave,
        access_class=access_class,
        options=options,
        targets=operation_targets,
        runtime=OperationRuntimeSpec(
            data_scope=data_scope,
            attempt_timeout_seconds=attempt_timeout_seconds,
            total_timeout_seconds=total_timeout_seconds,
            resource_ref_eligible=resource_ref_eligible,
            continuation_eligible=continuation_eligible,
        ),
        implementation_state=implementation_state,
        unavailable_reason=unavailable_reason,
    )


def _default_targets(tool: ToolFamily) -> tuple[TargetSpec, ...]:
    if tool == "read":
        return (URL_TARGET, NATIVE_ID_TARGET, RESOURCE_REF_TARGET)
    if tool == "transcribe":
        return (
            URL_TARGET,
            NATIVE_ID_TARGET,
            RESOURCE_REF_TARGET,
            LOCAL_FILE_TARGET,
        )
    return ()


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
                unavailable_reason=GITHUB_UNAVAILABLE_REASON,
            ),
            _operation(
                "github",
                "search.code",
                "search",
                1,
                "credential_free",
                (LIMIT,),
                unavailable_reason=GITHUB_UNAVAILABLE_REASON,
            ),
            _operation(
                "github",
                "read.repository",
                "read",
                1,
                "credential_free",
                targets=(GITHUB_REPOSITORY_TARGET,),
                unavailable_reason=GITHUB_UNAVAILABLE_REASON,
            ),
            _operation(
                "github",
                "read.issue",
                "read",
                1,
                "credential_free",
                targets=(GITHUB_RESOURCE_TARGET,),
                unavailable_reason=GITHUB_UNAVAILABLE_REASON,
            ),
            _operation(
                "github",
                "read.pull_request",
                "read",
                1,
                "credential_free",
                targets=(GITHUB_RESOURCE_TARGET,),
                unavailable_reason=GITHUB_UNAVAILABLE_REASON,
            ),
            _operation(
                "github",
                "browse.actions",
                "browse",
                1,
                "credential_free",
                (LIMIT,),
                targets=(GITHUB_REPOSITORY_TARGET,),
                unavailable_reason=GITHUB_UNAVAILABLE_REASON,
            ),
            _operation(
                "github",
                "read.action_run",
                "read",
                1,
                "credential_free",
                targets=(GITHUB_RESOURCE_TARGET,),
                unavailable_reason=GITHUB_UNAVAILABLE_REASON,
            ),
            _operation(
                "github",
                "browse.releases",
                "browse",
                1,
                "credential_free",
                (LIMIT,),
                targets=(GITHUB_REPOSITORY_TARGET,),
                unavailable_reason=GITHUB_UNAVAILABLE_REASON,
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
                "twitter",
                "browse.home",
                "browse",
                2,
                "account_session",
                (LIMIT,),
                "account_visible",
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
                "youtube",
                "search.videos",
                "search",
                1,
                "credential_free",
                (LIMIT,),
                implementation_state="implemented",
            ),
            _operation(
                "youtube",
                "read.video",
                "read",
                1,
                "credential_free",
                targets=(YOUTUBE_VIDEO_TARGET,),
                implementation_state="implemented",
            ),
            _operation(
                "youtube",
                "read.subtitles",
                "read",
                1,
                "credential_free",
                (LANGUAGE,),
                targets=(YOUTUBE_VIDEO_TARGET,),
                implementation_state="implemented",
            ),
            _operation(
                "youtube",
                "read.comments",
                "read",
                1,
                "credential_free",
                (LIMIT, PAGE),
                targets=(YOUTUBE_VIDEO_TARGET,),
                implementation_state="implemented",
            ),
            _operation(
                "youtube",
                "transcribe.video",
                "transcribe",
                1,
                "api_key",
                (LANGUAGE,),
                targets=(YOUTUBE_VIDEO_TARGET,),
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
                "reddit",
                "search.posts",
                "search",
                2,
                "account_session",
                (LIMIT,),
                attempt_timeout_seconds=20,
                total_timeout_seconds=20,
                implementation_state="implemented",
            ),
            _operation(
                "reddit",
                "read.post",
                "read",
                2,
                "account_session",
                targets=(REDDIT_POST_TARGET,),
                attempt_timeout_seconds=20,
                total_timeout_seconds=20,
                implementation_state="implemented",
            ),
            _operation(
                "reddit",
                "browse.subreddit",
                "browse",
                2,
                "account_session",
                (LIMIT,),
                targets=(SUBREDDIT_TARGET,),
                attempt_timeout_seconds=20,
                total_timeout_seconds=20,
                implementation_state="implemented",
            ),
            _operation(
                "reddit",
                "browse.hot",
                "browse",
                2,
                "account_session",
                (LIMIT,),
                attempt_timeout_seconds=20,
                total_timeout_seconds=20,
                implementation_state="implemented",
            ),
            _operation(
                "reddit",
                "browse.popular",
                "browse",
                2,
                "account_session",
                (LIMIT,),
                attempt_timeout_seconds=20,
                total_timeout_seconds=20,
                implementation_state="implemented",
            ),
            _operation(
                "reddit",
                "browse.all",
                "browse",
                2,
                "account_session",
                (LIMIT,),
                attempt_timeout_seconds=20,
                total_timeout_seconds=20,
                implementation_state="implemented",
            ),
            _operation(
                "reddit",
                "read.subreddit",
                "read",
                2,
                "account_session",
                targets=(SUBREDDIT_TARGET,),
                attempt_timeout_seconds=20,
                total_timeout_seconds=20,
                implementation_state="implemented",
            ),
        ),
    ),
    _source(
        "facebook",
        "Facebook",
        3,
        "account_session",
        (
            _operation(
                "facebook",
                "search",
                "search",
                3,
                "account_session",
                (LIMIT,),
                attempt_timeout_seconds=20,
                total_timeout_seconds=20,
                implementation_state="implemented",
            ),
            _operation(
                "facebook",
                "read.profile",
                "read",
                3,
                "account_session",
                targets=(SOCIAL_USERNAME_TARGET,),
                attempt_timeout_seconds=20,
                total_timeout_seconds=20,
                implementation_state="implemented",
            ),
            _operation(
                "facebook",
                "browse.feed",
                "browse",
                3,
                "account_session",
                (LIMIT,),
                "account_visible",
                attempt_timeout_seconds=20,
                total_timeout_seconds=20,
                implementation_state="implemented",
            ),
            _operation(
                "facebook",
                "browse.groups",
                "browse",
                3,
                "account_session",
                (LIMIT,),
                "account_visible",
                attempt_timeout_seconds=20,
                total_timeout_seconds=20,
                implementation_state="implemented",
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
                "instagram",
                "search.users",
                "search",
                3,
                "account_session",
                (LIMIT,),
                attempt_timeout_seconds=20,
                total_timeout_seconds=20,
                implementation_state="implemented",
            ),
            _operation(
                "instagram",
                "read.profile",
                "read",
                3,
                "account_session",
                targets=(SOCIAL_USERNAME_TARGET,),
                attempt_timeout_seconds=20,
                total_timeout_seconds=20,
                implementation_state="implemented",
            ),
            _operation(
                "instagram",
                "browse.user_posts",
                "browse",
                3,
                "account_session",
                (LIMIT,),
                targets=(SOCIAL_USERNAME_TARGET,),
                attempt_timeout_seconds=20,
                total_timeout_seconds=20,
                implementation_state="implemented",
            ),
            _operation(
                "instagram",
                "browse.explore",
                "browse",
                3,
                "account_session",
                (LIMIT,),
                "account_visible",
                attempt_timeout_seconds=20,
                total_timeout_seconds=20,
                implementation_state="implemented",
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
                "bilibili",
                "search.videos",
                "search",
                1,
                "credential_free",
                (LIMIT,),
                implementation_state="implemented",
            ),
            _operation(
                "bilibili",
                "read.video",
                "read",
                1,
                "credential_free",
                targets=(BILIBILI_VIDEO_TARGET,),
                implementation_state="implemented",
            ),
            _operation(
                "bilibili",
                "read.subtitles",
                "read",
                1,
                "account_session",
                (LANGUAGE,),
                targets=(BILIBILI_VIDEO_TARGET,),
            ),
            _operation(
                "bilibili",
                "browse.hot",
                "browse",
                1,
                "credential_free",
                (LIMIT,),
                implementation_state="implemented",
            ),
            _operation(
                "bilibili",
                "browse.rank",
                "browse",
                1,
                "credential_free",
                (LIMIT,),
                implementation_state="implemented",
            ),
            _operation(
                "bilibili",
                "transcribe.video",
                "transcribe",
                1,
                "api_key",
                (LANGUAGE,),
                targets=(BILIBILI_VIDEO_TARGET,),
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
            _operation(
                "v2ex",
                "browse.hot",
                "browse",
                1,
                "credential_free",
                (LIMIT,),
                implementation_state="implemented",
            ),
            _operation(
                "v2ex",
                "browse.node_topics",
                "browse",
                1,
                "credential_free",
                (NODE, LIMIT, PAGE),
                implementation_state="implemented",
            ),
            _operation(
                "v2ex",
                "read.topic",
                "read",
                1,
                "credential_free",
                targets=(POSITIVE_ID_TARGET,),
                implementation_state="implemented",
            ),
            _operation(
                "v2ex",
                "read.user",
                "read",
                1,
                "credential_free",
                targets=(USERNAME_TARGET,),
                implementation_state="implemented",
            ),
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
            _operation(
                "rss",
                "read.feed",
                "read",
                1,
                "credential_free",
                targets=(URL_TARGET,),
                implementation_state="implemented",
            ),
            _operation(
                "rss",
                "browse.entries",
                "browse",
                1,
                "credential_free",
                (LIMIT,),
                targets=(URL_TARGET,),
                implementation_state="implemented",
            ),
        ),
    ),
    _source(
        "exa",
        "Exa",
        1,
        "credential_free",
        (
            _operation(
                "exa",
                "search.web",
                "search",
                1,
                "credential_free",
                (LIMIT,),
                implementation_state="implemented",
                unavailable_reason=EXA_SETUP_REQUIRED_REASON,
            ),
            _operation(
                "exa",
                "search.code",
                "search",
                1,
                "credential_free",
                (LIMIT,),
                unavailable_reason=EXA_CODE_UNAVAILABLE_REASON,
            ),
        ),
    ),
    _source(
        "web",
        "Generic Web",
        1,
        "credential_free",
        (
            _operation(
                "web",
                "read.url",
                "read",
                1,
                "credential_free",
                targets=(URL_TARGET,),
                unavailable_reason=WEB_UNAVAILABLE_REASON,
            ),
        ),
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
