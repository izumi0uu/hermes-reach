"""Lazy, compatibility-checked access to the pinned Agent-Reach package."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, is_dataclass
from importlib import import_module
from importlib.metadata import PackageNotFoundError, distribution, version
from inspect import Parameter, signature
from pathlib import Path
from types import FunctionType, MappingProxyType, ModuleType, NoneType, UnionType
from typing import Final, Literal, cast, get_args, get_origin

from .catalog import SOURCE_CATALOG

AGENT_REACH_DISTRIBUTION: Final = "agent-reach"
AGENT_REACH_VERSION: Final = "1.5.0"
AGENT_REACH_OFFICIAL_BASE_COMMIT: Final = "b4d52c46c9113cb0f653d6df4cf71ebadf4930ac"
AGENT_REACH_FORK_URL: Final = "https://github.com/izumi0uu/Agent-Reach.git"
AGENT_REACH_FORK_COMMIT: Final = "281dc3352c63cdb644f02e028cc5d645c279954a"
AGENT_REACH_PROTOCOL_VERSION: Final = "v1"
AGENT_REACH_FETCHED_DOCUMENT_CAPABILITY: Final = "fetched_document.v1"
AGENT_REACH_NETWORK_ACCESS_CAPABILITY: Final = "network_access.v1"
AGENT_REACH_PRIVATE_WORKSPACE_CAPABILITY: Final = "private_workspace.v1"
AGENT_REACH_MCPORTER_ARTIFACTS_CAPABILITY: Final = "mcporter_artifacts.v1"
AGENT_REACH_OPENCLI_SESSION_CAPABILITY: Final = "opencli_session.v1"
# Compatibility alias for callers that previously knew only one exact dependency pin.
AGENT_REACH_COMMIT: Final = AGENT_REACH_FORK_COMMIT
BILIBILI_CLI_DISTRIBUTION: Final = "bilibili-cli"
BILIBILI_CLI_VERSION: Final = "0.6.2"
FEEDPARSER_DISTRIBUTION: Final = "feedparser"
FEEDPARSER_VERSION: Final = "6.0.12"
YTDLP_DISTRIBUTION: Final = "yt-dlp"
YTDLP_VERSION: Final = "2026.7.4"
YTDLP_EJS_DISTRIBUTION: Final = "yt-dlp-ejs"
YTDLP_EJS_VERSION: Final = "0.8.0"
DENO_DISTRIBUTION: Final = "deno"
DENO_VERSION: Final = "2.8.3"
SAFE_AGENT_REACH_DOCTOR_CHANNELS: Final[frozenset[str]] = frozenset(
    {"web", "rss", "v2ex", "youtube"}
)
_UPSTREAM_TO_REACH: Final[Mapping[str, str]] = MappingProxyType(
    {
        "github": "github",
        "twitter": "twitter",
        "youtube": "youtube",
        "reddit": "reddit",
        "facebook": "facebook",
        "instagram": "instagram",
        "bilibili": "bilibili",
        "xiaohongshu": "xiaohongshu",
        "linkedin": "linkedin",
        "xiaoyuzhou": "xiaoyuzhou",
        "v2ex": "v2ex",
        "xueqiu": "xueqiu",
        "rss": "rss",
        "exa_search": "exa",
        "web": "web",
    }
)
_REACH_SOURCES: Final[frozenset[str]] = frozenset(
    source.name for source in SOURCE_CATALOG
)
_UPSTREAM_STATES: Final[frozenset[str]] = frozenset({"ok", "warn", "off", "error"})
_INCOMPATIBLE_VERSION: Final = "Agent-Reach has an incompatible installed version."
_INCOMPATIBLE_PROVENANCE: Final = (
    "Agent-Reach has incompatible installed source provenance."
)
_INCOMPATIBLE_EXECUTION_CONTRACT: Final = (
    "Agent-Reach has an incompatible execution capability contract."
)
_INCOMPATIBLE_REGISTRY: Final = "Agent-Reach has an incompatible channel registry."
_INCOMPATIBLE_DOCTOR: Final = "Agent-Reach returned an incompatible doctor report."
_AGENT_REACH_MODULE: Final = "agent_reach"
_EXECUTION_PACKAGE_MODULE: Final = f"{_AGENT_REACH_MODULE}.execution"
_EXECUTION_MODULE: Final = f"{_EXECUTION_PACKAGE_MODULE}.v1"
_EXECUTION_CONTRACTS_MODULE: Final = f"{_EXECUTION_MODULE}.contracts"
_EXECUTION_REGISTRY_MODULE: Final = f"{_EXECUTION_MODULE}.registry"
_EXECUTION_RSS_MODULE: Final = f"{_EXECUTION_MODULE}.rss"
_EXECUTION_BILIBILI_MODULE: Final = f"{_EXECUTION_MODULE}.bilibili"
_EXECUTION_YOUTUBE_MODULE: Final = f"{_EXECUTION_MODULE}.youtube"
_EXECUTION_V2EX_TRANSPORT_MODULE: Final = f"{_EXECUTION_MODULE}._v2ex_transport"
_EXECUTION_V2EX_MODULE: Final = f"{_EXECUTION_MODULE}.v2ex"
_EXECUTION_EXA_MODULE: Final = f"{_EXECUTION_MODULE}.exa"
_EXECUTION_OPENCLI_SOCIAL_MODULE: Final = f"{_EXECUTION_MODULE}.opencli_social"
_EXECUTION_OPENCLI_GUARD_RESOURCE: Final = f"{_EXECUTION_MODULE}._opencli_no_lifecycle"
_EXECUTION_MODULE_FILES: Final[Mapping[str, tuple[str, str, int]]] = MappingProxyType(
    {
        _AGENT_REACH_MODULE: (
            "agent_reach/__init__.py",
            "nj1R2PRQkU3mBukvsKQF09jpjRrvwOWnb3MCkcLA5Es",
            603,
        ),
        _EXECUTION_PACKAGE_MODULE: (
            "agent_reach/execution/__init__.py",
            "MxyIym6KexY2aMfAnfG2EZcPMXZpTH0acTSUC_cQ3FE",
            122,
        ),
        _EXECUTION_MODULE: (
            "agent_reach/execution/v1/__init__.py",
            "E0Rh3_5lAxmaCRb14bq33zUslhtYfAAT7PK4EZd-fEU",
            1_327,
        ),
        _EXECUTION_CONTRACTS_MODULE: (
            "agent_reach/execution/v1/contracts.py",
            "GNEszOYYOi1rhQo0XJGBO8vJ0tUTGy-Nb08FPPwfwvo",
            54_266,
        ),
        _EXECUTION_REGISTRY_MODULE: (
            "agent_reach/execution/v1/registry.py",
            "allM21uoqORwCHcNPHRp8b0qCyJeCbesDLrkdaFGXHI",
            23_126,
        ),
        _EXECUTION_RSS_MODULE: (
            "agent_reach/execution/v1/rss.py",
            "_zKpG9cm_CCdEYiGp8EWWl6UN8cvmZuRyvn6jsbha18",
            9_169,
        ),
        _EXECUTION_BILIBILI_MODULE: (
            "agent_reach/execution/v1/bilibili.py",
            "YGvZLZvSl2SMC2CTejiNF9XtcyqG_D8GNqJN9vAgdNA",
            20_572,
        ),
        _EXECUTION_YOUTUBE_MODULE: (
            "agent_reach/execution/v1/youtube.py",
            "YVcP6M9wKQcae8NIi4MpEn3TkRHSIB2ww7hK7PVPTQg",
            29_927,
        ),
        _EXECUTION_V2EX_TRANSPORT_MODULE: (
            "agent_reach/execution/v1/_v2ex_transport.py",
            "5LG_QSzeW0iGjxx-K-bSwWHs1l3RhJXQfvcZdzF5aVw",
            21_875,
        ),
        _EXECUTION_V2EX_MODULE: (
            "agent_reach/execution/v1/v2ex.py",
            "F5RE2KXl7s2UX5NtxWIlsEaLqCEKwxtGkTH2-Vk031w",
            16_706,
        ),
        _EXECUTION_EXA_MODULE: (
            "agent_reach/execution/v1/exa.py",
            "106HW8O8nRfdpsUrGjacUUmkHyucAZ13YKjUlBypby4",
            32_724,
        ),
        _EXECUTION_OPENCLI_SOCIAL_MODULE: (
            "agent_reach/execution/v1/opencli_social.py",
            "GNpKPH9gmWAJY37PD6X1ZunVSwsHbPj3su7TeH6uEUE",
            68_824,
        ),
        _EXECUTION_OPENCLI_GUARD_RESOURCE: (
            "agent_reach/execution/v1/_opencli_no_lifecycle.mjs",
            "nJzZv4Fj-z-6hj-Upx6eoJ6jMjuE-JoGtV49HxlRUhM",
            3_539,
        ),
    }
)
_EXPECTED_EXECUTION_EXPORTS: Final = (
    "EXECUTION_ERROR_CODES",
    "FETCHED_DOCUMENT_CAPABILITY",
    "MCPORTER_ARTIFACTS_CAPABILITY",
    "NETWORK_ACCESS_CAPABILITY",
    "OPENCLI_SESSION_CAPABILITY",
    "PRIVATE_WORKSPACE_CAPABILITY",
    "PROTOCOL_VERSION",
    "ExecutionContextV1",
    "ExecutionErrorCodeV1",
    "ExecutionFailureV1",
    "ExecutionItemV1",
    "ExecutionLimitsV1",
    "ExecutionRequestV1",
    "ExecutionResultV1",
    "ExecutionSuccessV1",
    "FetchedDocumentV1",
    "McporterArtifactsV1",
    "NetworkAccessV1",
    "OpenCliSessionV1",
    "OperationCapabilityV1",
    "PrivateWorkspaceV1",
    "execute",
    "list_capabilities",
)
_EXPECTED_EXECUTION_ERROR_CODES: Final = (
    "unsupported_protocol_version",
    "invalid_request",
    "unsupported_source",
    "unsupported_operation",
    "host_capability_missing",
    "backend_unavailable",
    "backend_incompatible",
    "deadline_exceeded",
    "cancelled",
    "invalid_input",
    "not_found",
    "authentication",
    "authorization",
    "rate_limit",
    "transient",
    "permanent",
    "backend_contract_violation",
)
_CAPABILITY_FIELDS: Final = (
    "protocol_version",
    "source",
    "operation",
    "argument_schema_id",
    "result_schema_ids",
    "backend_id",
    "backend_version",
    "required_host_capabilities",
    "maximum_items",
    "maximum_document_bytes",
    "maximum_metadata_bytes",
    "maximum_output_bytes",
    "maximum_content_type_characters",
    "maximum_content_location_characters",
    "maximum_text_characters",
    "maximum_title_characters",
    "maximum_url_characters",
    "maximum_native_id_characters",
    "maximum_author_characters",
    "maximum_published_characters",
)
_EXECUTION_CLASS_FIELDS: Final[Mapping[str, tuple[str, ...]]] = MappingProxyType(
    {
        "OperationCapabilityV1": _CAPABILITY_FIELDS,
        "ExecutionRequestV1": (
            "protocol_version",
            "source",
            "operation",
            "arguments",
        ),
        "FetchedDocumentV1": ("body", "content_type", "content_location"),
        "NetworkAccessV1": (),
        "PrivateWorkspaceV1": (),
        "McporterArtifactsV1": (
            "node_executable",
            "node_sha256",
            "mcporter_root",
            "mcporter_cli",
            "mcporter_tree_sha256",
            "config_path",
            "config_sha256",
        ),
        "OpenCliSessionV1": (
            "node_executable",
            "node_sha256",
            "opencli_root",
            "opencli_cli",
            "opencli_tree_sha256",
            "session_home",
        ),
        "ExecutionLimitsV1": (
            "maximum_items",
            "maximum_text_characters",
        ),
        "ExecutionContextV1": ("host_capabilities", "checkpoint", "limits"),
        "ExecutionItemV1": ("schema_id", "fields"),
        "ExecutionSuccessV1": (
            "protocol_version",
            "source",
            "operation",
            "backend_id",
            "backend_version",
            "items",
            "truncated",
            "partial_error_code",
        ),
        "ExecutionFailureV1": (
            "protocol_version",
            "source",
            "operation",
            "backend_id",
            "backend_version",
            "error_code",
        ),
    }
)
_EXPECTED_EXECUTION_CAPABILITIES: Final = (
    (
        "v1",
        "rss",
        "read.feed",
        "rss.read.feed.arguments.v1",
        ("rss.feed.v1",),
        "feedparser",
        "6.0.12",
        ("fetched_document.v1",),
        1,
        1_048_576,
        16_384,
        1_048_576,
        512,
        8_192,
        16_000,
        4_096,
        8_192,
        512,
        2_048,
        512,
    ),
    (
        "v1",
        "rss",
        "browse.entries",
        "rss.browse.entries.arguments.v1",
        ("rss.entry.v1",),
        "feedparser",
        "6.0.12",
        ("fetched_document.v1",),
        21,
        1_048_576,
        16_384,
        1_048_576,
        512,
        8_192,
        16_000,
        4_096,
        8_192,
        512,
        2_048,
        512,
    ),
    (
        "v1",
        "bilibili",
        "search.videos",
        "bilibili.search.videos.arguments.v1",
        ("bilibili.video.v1",),
        "bili-cli",
        "0.6.2",
        ("network_access.v1",),
        50,
        1_048_576,
        16_384,
        524_288,
        512,
        8_192,
        16_000,
        4_096,
        8_192,
        512,
        1_024,
        512,
    ),
    (
        "v1",
        "bilibili",
        "read.video",
        "bilibili.read.video.arguments.v1",
        ("bilibili.video.v1",),
        "bili-cli",
        "0.6.2",
        ("network_access.v1",),
        1,
        1_048_576,
        16_384,
        524_288,
        512,
        8_192,
        16_000,
        4_096,
        8_192,
        512,
        1_024,
        512,
    ),
    (
        "v1",
        "bilibili",
        "browse.hot",
        "bilibili.browse.hot.arguments.v1",
        ("bilibili.video.v1",),
        "bili-cli",
        "0.6.2",
        ("network_access.v1",),
        50,
        1_048_576,
        16_384,
        524_288,
        512,
        8_192,
        16_000,
        4_096,
        8_192,
        512,
        1_024,
        512,
    ),
    (
        "v1",
        "bilibili",
        "browse.rank",
        "bilibili.browse.rank.arguments.v1",
        ("bilibili.video.v1",),
        "bili-cli",
        "0.6.2",
        ("network_access.v1",),
        50,
        1_048_576,
        16_384,
        524_288,
        512,
        8_192,
        16_000,
        4_096,
        8_192,
        512,
        1_024,
        512,
    ),
    (
        "v1",
        "youtube",
        "read.video",
        "youtube.read.video.arguments.v1",
        ("youtube.video.v1",),
        "yt-dlp",
        "2026.7.4",
        ("network_access.v1",),
        1,
        1_048_576,
        16_384,
        524_288,
        512,
        8_192,
        16_000,
        4_096,
        8_192,
        512,
        1_024,
        512,
    ),
    (
        "v1",
        "youtube",
        "search.videos",
        "youtube.search.videos.arguments.v1",
        ("youtube.video.v1",),
        "yt-dlp",
        "2026.7.4",
        ("network_access.v1",),
        50,
        1_048_576,
        16_384,
        524_288,
        512,
        8_192,
        16_000,
        4_096,
        8_192,
        512,
        1_024,
        512,
    ),
    (
        "v1",
        "youtube",
        "read.subtitles",
        "youtube.read.subtitles.arguments.v1",
        ("youtube.subtitle.v1",),
        "yt-dlp",
        "2026.7.4",
        ("network_access.v1", "private_workspace.v1"),
        1,
        1_048_576,
        16_384,
        524_288,
        512,
        8_192,
        16_000,
        4_096,
        8_192,
        512,
        1_024,
        512,
    ),
    (
        "v1",
        "v2ex",
        "browse.hot",
        "v2ex.browse.hot.arguments.v1",
        ("v2ex.topic.v1",),
        "v2ex-public-api",
        "legacy-json-2026-07-31",
        ("network_access.v1",),
        50,
        1_048_576,
        16_384,
        1_048_576,
        512,
        8_192,
        16_000,
        4_096,
        8_192,
        512,
        2_048,
        512,
    ),
    (
        "v1",
        "v2ex",
        "browse.node_topics",
        "v2ex.browse.node_topics.arguments.v1",
        ("v2ex.topic.v1",),
        "v2ex-public-api",
        "legacy-json-2026-07-31",
        ("network_access.v1",),
        50,
        1_048_576,
        16_384,
        1_048_576,
        512,
        8_192,
        16_000,
        4_096,
        8_192,
        512,
        2_048,
        512,
    ),
    (
        "v1",
        "v2ex",
        "read.topic",
        "v2ex.read.topic.arguments.v1",
        ("v2ex.topic.v1", "v2ex.reply.v1"),
        "v2ex-public-api",
        "legacy-json-2026-07-31",
        ("network_access.v1",),
        21,
        1_048_576,
        16_384,
        1_048_576,
        512,
        8_192,
        16_000,
        4_096,
        8_192,
        512,
        2_048,
        512,
    ),
    (
        "v1",
        "v2ex",
        "read.user",
        "v2ex.read.user.arguments.v1",
        ("v2ex.profile.v1",),
        "v2ex-public-api",
        "legacy-json-2026-07-31",
        ("network_access.v1",),
        1,
        1_048_576,
        16_384,
        1_048_576,
        512,
        8_192,
        16_000,
        4_096,
        8_192,
        512,
        2_048,
        512,
    ),
    (
        "v1",
        "exa",
        "search.web",
        "exa.search.web.arguments.v1",
        ("exa.search.result.v1",),
        "exa-mcporter",
        "0.12.3+exa-web.v1",
        ("network_access.v1", "mcporter_artifacts.v1"),
        20,
        1_048_576,
        16_384,
        524_288,
        512,
        8_192,
        16_000,
        4_096,
        8_192,
        512,
        2_048,
        512,
    ),
    (
        "v1",
        "reddit",
        "search.posts",
        "reddit.search.posts.arguments.v1",
        ("reddit.post.v1",),
        "opencli",
        "1.8.6-hermes.1",
        ("opencli_session.v1",),
        50,
        1_048_576,
        16_384,
        524_288,
        512,
        8_192,
        16_000,
        4_096,
        8_192,
        512,
        2_048,
        512,
    ),
    (
        "v1",
        "reddit",
        "read.post",
        "reddit.read.post.arguments.v1",
        ("reddit.thread.item.v1",),
        "opencli",
        "1.8.6-hermes.1",
        ("opencli_session.v1",),
        14,
        1_048_576,
        16_384,
        524_288,
        512,
        8_192,
        16_000,
        4_096,
        8_192,
        512,
        2_048,
        512,
    ),
    (
        "v1",
        "reddit",
        "browse.subreddit",
        "reddit.browse.subreddit.arguments.v1",
        ("reddit.post.v1",),
        "opencli",
        "1.8.6-hermes.1",
        ("opencli_session.v1",),
        50,
        1_048_576,
        16_384,
        524_288,
        512,
        8_192,
        16_000,
        4_096,
        8_192,
        512,
        2_048,
        512,
    ),
    (
        "v1",
        "reddit",
        "browse.hot",
        "reddit.browse.hot.arguments.v1",
        ("reddit.post.v1",),
        "opencli",
        "1.8.6-hermes.1",
        ("opencli_session.v1",),
        50,
        1_048_576,
        16_384,
        524_288,
        512,
        8_192,
        16_000,
        4_096,
        8_192,
        512,
        2_048,
        512,
    ),
    (
        "v1",
        "reddit",
        "browse.popular",
        "reddit.browse.popular.arguments.v1",
        ("reddit.post.v1",),
        "opencli",
        "1.8.6-hermes.1",
        ("opencli_session.v1",),
        50,
        1_048_576,
        16_384,
        524_288,
        512,
        8_192,
        16_000,
        4_096,
        8_192,
        512,
        2_048,
        512,
    ),
    (
        "v1",
        "reddit",
        "browse.all",
        "reddit.browse.all.arguments.v1",
        ("reddit.post.v1",),
        "opencli",
        "1.8.6-hermes.1",
        ("opencli_session.v1",),
        50,
        1_048_576,
        16_384,
        524_288,
        512,
        8_192,
        16_000,
        4_096,
        8_192,
        512,
        2_048,
        512,
    ),
    (
        "v1",
        "reddit",
        "read.subreddit",
        "reddit.read.subreddit.arguments.v1",
        ("reddit.subreddit.v1",),
        "opencli",
        "1.8.6-hermes.1",
        ("opencli_session.v1",),
        1,
        1_048_576,
        16_384,
        524_288,
        512,
        8_192,
        16_000,
        4_096,
        8_192,
        512,
        2_048,
        512,
    ),
    (
        "v1",
        "facebook",
        "search",
        "facebook.search.arguments.v1",
        ("facebook.search.result.v1",),
        "opencli",
        "1.8.6-hermes.1",
        ("opencli_session.v1",),
        50,
        1_048_576,
        16_384,
        524_288,
        512,
        8_192,
        16_000,
        4_096,
        8_192,
        512,
        2_048,
        512,
    ),
    (
        "v1",
        "facebook",
        "read.profile",
        "facebook.read.profile.arguments.v1",
        ("facebook.profile.v1",),
        "opencli",
        "1.8.6-hermes.1",
        ("opencli_session.v1",),
        1,
        1_048_576,
        16_384,
        524_288,
        512,
        8_192,
        16_000,
        4_096,
        8_192,
        512,
        2_048,
        512,
    ),
    (
        "v1",
        "facebook",
        "browse.feed",
        "facebook.browse.feed.arguments.v1",
        ("facebook.post.v1",),
        "opencli",
        "1.8.6-hermes.1",
        ("opencli_session.v1",),
        50,
        1_048_576,
        16_384,
        524_288,
        512,
        8_192,
        16_000,
        4_096,
        8_192,
        512,
        2_048,
        512,
    ),
    (
        "v1",
        "facebook",
        "browse.groups",
        "facebook.browse.groups.arguments.v1",
        ("facebook.group.v1",),
        "opencli",
        "1.8.6-hermes.1",
        ("opencli_session.v1",),
        50,
        1_048_576,
        16_384,
        524_288,
        512,
        8_192,
        16_000,
        4_096,
        8_192,
        512,
        2_048,
        512,
    ),
    (
        "v1",
        "instagram",
        "search.users",
        "instagram.search.users.arguments.v1",
        ("instagram.user.v1",),
        "opencli",
        "1.8.6-hermes.1",
        ("opencli_session.v1",),
        50,
        1_048_576,
        16_384,
        524_288,
        512,
        8_192,
        16_000,
        4_096,
        8_192,
        512,
        2_048,
        512,
    ),
    (
        "v1",
        "instagram",
        "read.profile",
        "instagram.read.profile.arguments.v1",
        ("instagram.profile.v1",),
        "opencli",
        "1.8.6-hermes.1",
        ("opencli_session.v1",),
        1,
        1_048_576,
        16_384,
        524_288,
        512,
        8_192,
        16_000,
        4_096,
        8_192,
        512,
        2_048,
        512,
    ),
    (
        "v1",
        "instagram",
        "browse.user_posts",
        "instagram.browse.user_posts.arguments.v1",
        ("instagram.post.v1",),
        "opencli",
        "1.8.6-hermes.1",
        ("opencli_session.v1",),
        50,
        1_048_576,
        16_384,
        524_288,
        512,
        8_192,
        16_000,
        4_096,
        8_192,
        512,
        2_048,
        512,
    ),
    (
        "v1",
        "instagram",
        "browse.explore",
        "instagram.browse.explore.arguments.v1",
        ("instagram.post.v1",),
        "opencli",
        "1.8.6-hermes.1",
        ("opencli_session.v1",),
        50,
        1_048_576,
        16_384,
        524_288,
        512,
        8_192,
        16_000,
        4_096,
        8_192,
        512,
        2_048,
        512,
    ),
)
HealthState = Literal["available", "setup_required", "degraded", "unavailable"]
ExecutionRuntimeModule = Literal[
    "rss", "bilibili", "youtube", "v2ex", "exa", "opencli_social"
]


class AgentReachBridgeError(RuntimeError):
    """The installed Agent-Reach package cannot satisfy the bridge contract."""


@dataclass(frozen=True, slots=True)
class AgentReachChannel:
    """The non-executing metadata Reach may reuse from one upstream channel."""

    source: str
    upstream_name: str
    description: str
    backends: tuple[str, ...]
    tier: int


@dataclass(frozen=True, slots=True)
class AgentReachCatalog:
    """A validated projection of the upstream channel registry."""

    version: str
    channels: tuple[AgentReachChannel, ...]


@dataclass(frozen=True, slots=True)
class AgentReachInstalledFile:
    """One execution file and its exact installed RECORD evidence."""

    path: Path
    hash_algorithm: str | None
    hash_value: str | None
    size: int | None


@dataclass(frozen=True, slots=True)
class AgentReachInstallation:
    """PEP 610 metadata and execution files from one installed distribution."""

    direct_url_document: str | None
    files: Mapping[str, AgentReachInstalledFile]

    def __post_init__(self) -> None:
        object.__setattr__(self, "files", MappingProxyType(dict(self.files)))


@dataclass(frozen=True, slots=True)
class AgentReachExecutionApi:
    """Validated callable and contract identities for one execution attempt."""

    protocol_version: str
    fetched_document_capability: str
    network_access_capability: str
    private_workspace_capability: str
    mcporter_artifacts_capability: str
    opencli_session_capability: str
    capabilities: tuple[object, ...]
    operation_capability_type: type[object]
    execution_request_type: type[object]
    fetched_document_type: type[object]
    network_access_type: type[object]
    private_workspace_type: type[object]
    mcporter_artifacts_type: type[object]
    opencli_session_type: type[object]
    execution_limits_type: type[object]
    execution_context_type: type[object]
    execution_item_type: type[object]
    execution_success_type: type[object]
    execution_failure_type: type[object]
    execute: Callable[[object, object], object]
    list_capabilities: Callable[[], object]


class ReadOnlyAgentReachConfig:
    """Satisfy channel checks without reading or writing upstream configuration."""

    __slots__ = ()

    def get(self, _key: str, default: object | None = None) -> object | None:
        return default

    def is_configured(self, _feature: str) -> bool:
        return False


ChannelLoader = Callable[[], Sequence[object]]
VersionReader = Callable[[str], str]
DirectUrlReader = Callable[[str], str | None]
InstallationReader = Callable[[str], AgentReachInstallation]
ExecutionModuleLoader = Callable[[], object]
ModuleLoader = Callable[[str], object]
DoctorProvider = Callable[[ReadOnlyAgentReachConfig], Mapping[str, object]]
CatalogProvider = Callable[[], AgentReachCatalog]


def load_agent_reach_catalog(
    channel_loader: ChannelLoader | None = None,
    version_reader: VersionReader = version,
    *,
    direct_url_reader: DirectUrlReader | None = None,
    installation_reader: InstallationReader | None = None,
    execution_module_loader: ExecutionModuleLoader | None = None,
    module_loader: ModuleLoader = import_module,
) -> AgentReachCatalog:
    """Load and validate upstream channel metadata without executing a probe."""

    validate_agent_reach_execution_contract(
        version_reader=version_reader,
        direct_url_reader=direct_url_reader,
        installation_reader=installation_reader,
        execution_module_loader=execution_module_loader,
        module_loader=module_loader,
    )

    loader = channel_loader if channel_loader is not None else _default_channel_loader
    channels = tuple(loader())
    if len(channels) != len(_UPSTREAM_TO_REACH):
        raise AgentReachBridgeError(_INCOMPATIBLE_REGISTRY)

    seen_upstream: set[str] = set()
    seen_sources: set[str] = set()
    projected: list[AgentReachChannel] = []
    for channel in channels:
        upstream_name = _channel_name(channel)
        if upstream_name in seen_upstream or upstream_name not in _UPSTREAM_TO_REACH:
            raise AgentReachBridgeError(_INCOMPATIBLE_REGISTRY)
        source = _UPSTREAM_TO_REACH[upstream_name]
        if source in seen_sources:
            raise AgentReachBridgeError(_INCOMPATIBLE_REGISTRY)
        seen_upstream.add(upstream_name)
        seen_sources.add(source)
        projected.append(
            AgentReachChannel(
                source=source,
                upstream_name=upstream_name,
                description=_channel_description(channel),
                backends=_channel_backends(channel),
                tier=_channel_tier(channel),
            )
        )

    if seen_upstream != set(_UPSTREAM_TO_REACH) or seen_sources != _REACH_SOURCES:
        raise AgentReachBridgeError(_INCOMPATIBLE_REGISTRY)
    return AgentReachCatalog(AGENT_REACH_VERSION, tuple(projected))


def validate_agent_reach_provenance(
    direct_url_reader: DirectUrlReader | None = None,
) -> None:
    """Require the exact reviewed PEP 610 VCS provenance without importing code."""

    reader = (
        direct_url_reader
        if direct_url_reader is not None
        else _default_direct_url_reader
    )
    try:
        document = reader(AGENT_REACH_DISTRIBUTION)
    except Exception:
        raise AgentReachBridgeError(_INCOMPATIBLE_PROVENANCE) from None
    _validate_agent_reach_provenance_document(document)


def _validate_agent_reach_provenance_document(document: object) -> None:
    if type(document) is not str or not document:
        raise AgentReachBridgeError(_INCOMPATIBLE_PROVENANCE)
    try:
        payload = json.loads(
            document,
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
    except (TypeError, ValueError):
        raise AgentReachBridgeError(_INCOMPATIBLE_PROVENANCE) from None
    if not isinstance(payload, dict) or set(payload) != {"url", "vcs_info"}:
        raise AgentReachBridgeError(_INCOMPATIBLE_PROVENANCE)
    vcs_info = payload.get("vcs_info")
    if (
        payload.get("url") != AGENT_REACH_FORK_URL
        or not isinstance(vcs_info, dict)
        or set(vcs_info) != {"vcs", "requested_revision", "commit_id"}
        or vcs_info.get("vcs") != "git"
        or vcs_info.get("requested_revision") != AGENT_REACH_FORK_COMMIT
        or vcs_info.get("commit_id") != AGENT_REACH_FORK_COMMIT
    ):
        raise AgentReachBridgeError(_INCOMPATIBLE_PROVENANCE)


def validate_agent_reach_execution_contract(
    *,
    version_reader: VersionReader = version,
    direct_url_reader: DirectUrlReader | None = None,
    installation_reader: InstallationReader | None = None,
    execution_module_loader: ExecutionModuleLoader | None = None,
    module_loader: ModuleLoader = import_module,
    runtime_module: ExecutionRuntimeModule | None = None,
) -> AgentReachExecutionApi:
    """Return the closed v1 API only after provenance and origin validation."""

    installed_version = _installed_version(AGENT_REACH_DISTRIBUTION, version_reader)
    if type(installed_version) is not str or installed_version != AGENT_REACH_VERSION:
        raise AgentReachBridgeError(_INCOMPATIBLE_VERSION)

    evidence_reader = (
        installation_reader
        if installation_reader is not None
        else _default_installation_reader
    )
    try:
        installation = evidence_reader(AGENT_REACH_DISTRIBUTION)
        if type(installation) is not AgentReachInstallation:
            raise TypeError
        document = installation.direct_url_document
        if direct_url_reader is not None:
            document = direct_url_reader(AGENT_REACH_DISTRIBUTION)
        _validate_agent_reach_provenance_document(document)
    except AgentReachBridgeError:
        raise
    except Exception:
        raise AgentReachBridgeError(_INCOMPATIBLE_PROVENANCE) from None

    if runtime_module not in {
        None,
        "rss",
        "bilibili",
        "youtube",
        "v2ex",
        "exa",
        "opencli_social",
    }:
        raise AgentReachBridgeError(_INCOMPATIBLE_EXECUTION_CONTRACT)

    _validate_execution_installation(installation)
    loader = (
        execution_module_loader
        if execution_module_loader is not None
        else _default_execution_module_loader
    )
    try:
        execution_module = loader()
        return _validated_execution_api(
            execution_module,
            installation,
            module_loader,
            runtime_module=runtime_module,
        )
    except AgentReachBridgeError:
        raise
    except Exception:
        raise AgentReachBridgeError(_INCOMPATIBLE_EXECUTION_CONTRACT) from None


def upstream_doctor_data(
    doctor_provider: DoctorProvider | None = None,
    catalog_provider: CatalogProvider = load_agent_reach_catalog,
) -> dict[str, object]:
    """Run the explicit upstream doctor and return only a redacted projection."""

    catalog = catalog_provider()
    provider = (
        doctor_provider if doctor_provider is not None else _default_doctor_provider
    )
    raw_report = provider(ReadOnlyAgentReachConfig())
    expected = {channel.upstream_name for channel in catalog.channels}
    if set(raw_report) != expected:
        raise AgentReachBridgeError(_INCOMPATIBLE_DOCTOR)

    channels: list[dict[str, object]] = []
    for channel in catalog.channels:
        entry = raw_report[channel.upstream_name]
        if not isinstance(entry, Mapping):
            raise AgentReachBridgeError(_INCOMPATIBLE_DOCTOR)
        upstream_state = entry.get("status")
        if (
            not isinstance(upstream_state, str)
            or upstream_state not in _UPSTREAM_STATES
        ):
            raise AgentReachBridgeError(_INCOMPATIBLE_DOCTOR)
        policy = entry.get("reach_policy")
        if policy is not None and policy != "connector_required":
            raise AgentReachBridgeError(_INCOMPATIBLE_DOCTOR)
        active_backend = entry.get("active_backend")
        availability = (
            "setup_required"
            if policy == "connector_required"
            else _availability(upstream_state, channel.tier)
        )
        reason = (
            "Reach requires a trusted Connector before probing this upstream channel."
            if policy == "connector_required"
            else _doctor_reason(upstream_state, channel.tier)
        )
        item: dict[str, object] = {
            "source": channel.source,
            "upstream_channel": channel.upstream_name,
            "availability": availability,
            "reason": reason,
            "tier": channel.tier,
            "backends": list(channel.backends),
        }
        if isinstance(active_backend, str) and active_backend in channel.backends:
            item["active_backend"] = active_backend
        channels.append(item)
    return {
        "version": catalog.version,
        "pinned_commit": AGENT_REACH_COMMIT,
        "official_base_commit": AGENT_REACH_OFFICIAL_BASE_COMMIT,
        "fork_commit": AGENT_REACH_FORK_COMMIT,
        "execution_protocol_version": AGENT_REACH_PROTOCOL_VERSION,
        "channels": channels,
    }


def _installed_version(distribution: str, version_reader: VersionReader) -> str:
    try:
        return version_reader(distribution)
    except PackageNotFoundError as error:
        raise AgentReachBridgeError(
            f"The required {distribution} distribution is not installed."
        ) from error


def _default_direct_url_reader(distribution_name: str) -> str | None:
    try:
        installed_distribution = distribution(distribution_name)
    except PackageNotFoundError:
        return None
    return installed_distribution.read_text("direct_url.json")


def _default_installation_reader(
    distribution_name: str,
) -> AgentReachInstallation:
    try:
        installed_distribution = distribution(distribution_name)
    except PackageNotFoundError:
        return AgentReachInstallation(None, {})

    required = {record[0] for record in _EXECUTION_MODULE_FILES.values()}
    located: dict[str, AgentReachInstalledFile] = {}
    seen: set[str] = set()
    duplicates: set[str] = set()
    for entry in installed_distribution.files or ():
        relative = entry.as_posix()
        if relative not in required:
            continue
        if relative in seen:
            duplicates.add(relative)
            located.pop(relative, None)
            continue
        seen.add(relative)
        try:
            record_hash = entry.hash
            located[relative] = AgentReachInstalledFile(
                Path(str(installed_distribution.locate_file(entry))),
                None if record_hash is None else record_hash.mode,
                None if record_hash is None else record_hash.value,
                entry.size,
            )
        except (AttributeError, OSError, RuntimeError, TypeError, ValueError):
            continue
    for relative in duplicates:
        located.pop(relative, None)
    return AgentReachInstallation(
        installed_distribution.read_text("direct_url.json"),
        located,
    )


def _default_execution_module_loader() -> object:
    return import_module(_EXECUTION_MODULE)


def _validate_execution_installation(
    installation: AgentReachInstallation,
) -> None:
    expected_files = {record[0] for record in _EXECUTION_MODULE_FILES.values()}
    if set(installation.files) != expected_files or not all(
        type(relative) is str for relative in installation.files
    ):
        raise AgentReachBridgeError(_INCOMPATIBLE_EXECUTION_CONTRACT)
    for module_name in _EXECUTION_MODULE_FILES:
        _installation_file(installation, module_name)


def _validated_execution_api(
    execution_module: object,
    installation: AgentReachInstallation,
    module_loader: ModuleLoader,
    *,
    runtime_module: ExecutionRuntimeModule | None,
) -> AgentReachExecutionApi:
    for parent_module_name in (_AGENT_REACH_MODULE, _EXECUTION_PACKAGE_MODULE):
        _validated_execution_module(
            module_loader(parent_module_name),
            parent_module_name,
            installation,
            expected_package=parent_module_name,
        )
    public_module = _validated_execution_module(
        execution_module,
        _EXECUTION_MODULE,
        installation,
    )
    exports = getattr(public_module, "__all__", None)
    if type(exports) is not list or tuple(exports) != _EXPECTED_EXECUTION_EXPORTS:
        raise AgentReachBridgeError(_INCOMPATIBLE_EXECUTION_CONTRACT)

    contracts_module = _validated_execution_module(
        module_loader(_EXECUTION_CONTRACTS_MODULE),
        _EXECUTION_CONTRACTS_MODULE,
        installation,
    )
    registry_module = _validated_execution_module(
        module_loader(_EXECUTION_REGISTRY_MODULE),
        _EXECUTION_REGISTRY_MODULE,
        installation,
    )

    protocol = _owned_export(public_module, contracts_module, "PROTOCOL_VERSION")
    fetched_document_capability = _owned_export(
        public_module,
        contracts_module,
        "FETCHED_DOCUMENT_CAPABILITY",
    )
    network_access_capability = _owned_export(
        public_module,
        contracts_module,
        "NETWORK_ACCESS_CAPABILITY",
    )
    private_workspace_capability = _owned_export(
        public_module,
        contracts_module,
        "PRIVATE_WORKSPACE_CAPABILITY",
    )
    mcporter_artifacts_capability = _owned_export(
        public_module,
        contracts_module,
        "MCPORTER_ARTIFACTS_CAPABILITY",
    )
    opencli_session_capability = _owned_export(
        public_module,
        contracts_module,
        "OPENCLI_SESSION_CAPABILITY",
    )
    error_codes = _owned_export(
        public_module,
        contracts_module,
        "EXECUTION_ERROR_CODES",
    )
    if (
        type(protocol) is not str
        or protocol != AGENT_REACH_PROTOCOL_VERSION
        or type(fetched_document_capability) is not str
        or fetched_document_capability != AGENT_REACH_FETCHED_DOCUMENT_CAPABILITY
        or type(network_access_capability) is not str
        or network_access_capability != AGENT_REACH_NETWORK_ACCESS_CAPABILITY
        or type(private_workspace_capability) is not str
        or private_workspace_capability != AGENT_REACH_PRIVATE_WORKSPACE_CAPABILITY
        or type(mcporter_artifacts_capability) is not str
        or mcporter_artifacts_capability != AGENT_REACH_MCPORTER_ARTIFACTS_CAPABILITY
        or type(opencli_session_capability) is not str
        or opencli_session_capability != AGENT_REACH_OPENCLI_SESSION_CAPABILITY
        or type(error_codes) is not frozenset
        or error_codes != frozenset(_EXPECTED_EXECUTION_ERROR_CODES)
    ):
        raise AgentReachBridgeError(_INCOMPATIBLE_EXECUTION_CONTRACT)

    class_exports: dict[str, type[object]] = {}
    contracts_origin = _installation_file(
        installation,
        _EXECUTION_CONTRACTS_MODULE,
    )
    for name, expected_fields in _EXECUTION_CLASS_FIELDS.items():
        symbol = _owned_export(public_module, contracts_module, name)
        class_exports[name] = _validated_execution_class(
            symbol,
            name,
            expected_fields,
            contracts_origin,
        )

    error_type = _owned_export(
        public_module,
        contracts_module,
        "ExecutionErrorCodeV1",
    )
    result_type = _owned_export(
        public_module,
        contracts_module,
        "ExecutionResultV1",
    )
    argument_scalar_type = getattr(contracts_module, "ArgumentScalarV1", None)
    result_scalar_type = getattr(contracts_module, "ResultScalarV1", None)
    host_capability_type = getattr(contracts_module, "HostCapabilityV1", None)
    if (
        get_origin(error_type) is not Literal
        or get_args(error_type) != _EXPECTED_EXECUTION_ERROR_CODES
        or get_origin(argument_scalar_type) is not UnionType
        or get_args(argument_scalar_type) != (str, int, bool, NoneType)
        or get_origin(result_scalar_type) is not UnionType
        or get_args(result_scalar_type) != (str, int, NoneType)
        or get_origin(host_capability_type) is not UnionType
        or get_args(host_capability_type)
        != (
            class_exports["FetchedDocumentV1"],
            class_exports["NetworkAccessV1"],
            class_exports["PrivateWorkspaceV1"],
            class_exports["McporterArtifactsV1"],
            class_exports["OpenCliSessionV1"],
        )
        or get_origin(result_type) is not UnionType
        or get_args(result_type)
        != (
            class_exports["ExecutionSuccessV1"],
            class_exports["ExecutionFailureV1"],
        )
    ):
        raise AgentReachBridgeError(_INCOMPATIBLE_EXECUTION_CONTRACT)

    execute = _validated_execution_function(
        _owned_export(public_module, registry_module, "execute"),
        "execute",
        ("request", "context"),
        _installation_file(installation, _EXECUTION_REGISTRY_MODULE),
        _EXECUTION_REGISTRY_MODULE,
    )
    capability_loader = _validated_execution_function(
        _owned_export(public_module, registry_module, "list_capabilities"),
        "list_capabilities",
        (),
        _installation_file(installation, _EXECUTION_REGISTRY_MODULE),
        _EXECUTION_REGISTRY_MODULE,
    )
    if runtime_module is not None:
        runtime_parameters: tuple[str, ...]
        if runtime_module == "rss":
            runtime_module_name = _EXECUTION_RSS_MODULE
            runtime_function_name = "execute_rss"
            runtime_parameters = ("request", "context", "document")
        elif runtime_module == "bilibili":
            runtime_module_name = _EXECUTION_BILIBILI_MODULE
            runtime_function_name = "execute_bilibili"
            runtime_parameters = ("request", "context")
        elif runtime_module == "youtube":
            runtime_module_name = _EXECUTION_YOUTUBE_MODULE
            runtime_function_name = "execute_youtube"
            runtime_parameters = ("request", "context")
        elif runtime_module == "v2ex":
            runtime_module_name = _EXECUTION_V2EX_MODULE
            runtime_function_name = "execute_v2ex"
            runtime_parameters = ("request", "context")
        elif runtime_module == "exa":
            runtime_module_name = _EXECUTION_EXA_MODULE
            runtime_function_name = "execute_exa"
            runtime_parameters = ("request", "context")
        elif runtime_module == "opencli_social":
            runtime_module_name = _EXECUTION_OPENCLI_SOCIAL_MODULE
            runtime_function_name = "execute_opencli_social"
            runtime_parameters = ("request", "context")
        else:
            raise AgentReachBridgeError(_INCOMPATIBLE_EXECUTION_CONTRACT)
        validated_runtime_module = _validated_execution_module(
            module_loader(runtime_module_name),
            runtime_module_name,
            installation,
        )
        if runtime_module == "v2ex":
            _validated_execution_module(
                module_loader(_EXECUTION_V2EX_TRANSPORT_MODULE),
                _EXECUTION_V2EX_TRANSPORT_MODULE,
                installation,
            )
        _validated_execution_function(
            getattr(validated_runtime_module, runtime_function_name, None),
            runtime_function_name,
            runtime_parameters,
            _installation_file(installation, runtime_module_name),
            runtime_module_name,
        )
    capabilities = capability_loader()
    capability_type = class_exports["OperationCapabilityV1"]
    if type(capabilities) is not tuple or len(capabilities) != len(
        _EXPECTED_EXECUTION_CAPABILITIES
    ):
        raise AgentReachBridgeError(_INCOMPATIBLE_EXECUTION_CONTRACT)
    for capability, expected in zip(
        capabilities,
        _EXPECTED_EXECUTION_CAPABILITIES,
        strict=True,
    ):
        if type(capability) is not capability_type:
            raise AgentReachBridgeError(_INCOMPATIBLE_EXECUTION_CONTRACT)
        actual = tuple(getattr(capability, name) for name in _CAPABILITY_FIELDS)
        if not _closed_values_equal(actual, expected):
            raise AgentReachBridgeError(_INCOMPATIBLE_EXECUTION_CONTRACT)

    return AgentReachExecutionApi(
        protocol_version=protocol,
        fetched_document_capability=fetched_document_capability,
        network_access_capability=network_access_capability,
        private_workspace_capability=private_workspace_capability,
        mcporter_artifacts_capability=mcporter_artifacts_capability,
        opencli_session_capability=opencli_session_capability,
        capabilities=capabilities,
        operation_capability_type=capability_type,
        execution_request_type=class_exports["ExecutionRequestV1"],
        fetched_document_type=class_exports["FetchedDocumentV1"],
        network_access_type=class_exports["NetworkAccessV1"],
        private_workspace_type=class_exports["PrivateWorkspaceV1"],
        mcporter_artifacts_type=class_exports["McporterArtifactsV1"],
        opencli_session_type=class_exports["OpenCliSessionV1"],
        execution_limits_type=class_exports["ExecutionLimitsV1"],
        execution_context_type=class_exports["ExecutionContextV1"],
        execution_item_type=class_exports["ExecutionItemV1"],
        execution_success_type=class_exports["ExecutionSuccessV1"],
        execution_failure_type=class_exports["ExecutionFailureV1"],
        execute=cast(Callable[[object, object], object], execute),
        list_capabilities=cast(Callable[[], object], capability_loader),
    )


def _validated_execution_module(
    value: object,
    expected_name: str,
    installation: AgentReachInstallation,
    *,
    expected_package: str = _EXECUTION_MODULE,
) -> ModuleType:
    if type(value) is not ModuleType:
        raise AgentReachBridgeError(_INCOMPATIBLE_EXECUTION_CONTRACT)
    module = value
    expected_file = _installation_file(installation, expected_name)
    specification = module.__spec__
    if (
        module.__name__ != expected_name
        or module.__package__ != expected_package
        or specification is None
        or specification.name != expected_name
        or type(module.__file__) is not str
        or type(specification.origin) is not str
    ):
        raise AgentReachBridgeError(_INCOMPATIBLE_EXECUTION_CONTRACT)
    try:
        expected_origin = expected_file.resolve(strict=True)
        module_origin = Path(module.__file__).resolve(strict=True)
        specification_origin = Path(specification.origin).resolve(strict=True)
    except (OSError, RuntimeError):
        raise AgentReachBridgeError(_INCOMPATIBLE_EXECUTION_CONTRACT) from None
    if module_origin != expected_origin or specification_origin != expected_origin:
        raise AgentReachBridgeError(_INCOMPATIBLE_EXECUTION_CONTRACT)
    return module


def _owned_export(
    public_module: ModuleType,
    owner_module: ModuleType,
    name: str,
) -> object:
    exported = getattr(public_module, name)
    if exported is not getattr(owner_module, name):
        raise AgentReachBridgeError(_INCOMPATIBLE_EXECUTION_CONTRACT)
    return exported


def _installation_file(
    installation: AgentReachInstallation,
    module_name: str,
) -> Path:
    relative, expected_hash, expected_size = _EXECUTION_MODULE_FILES[module_name]
    installed_file = installation.files.get(relative)
    if (
        type(installed_file) is not AgentReachInstalledFile
        or installed_file.hash_algorithm != "sha256"
        or type(installed_file.hash_value) is not str
        or not hmac.compare_digest(installed_file.hash_value, expected_hash)
        or type(installed_file.size) is not int
        or installed_file.size != expected_size
        or not isinstance(installed_file.path, Path)
    ):
        raise AgentReachBridgeError(_INCOMPATIBLE_EXECUTION_CONTRACT)
    try:
        if installed_file.path.is_symlink() or not installed_file.path.is_file():
            raise OSError
        expected_file = installed_file.path.resolve(strict=True)
        if expected_file.stat().st_size != expected_size:
            raise OSError
        with expected_file.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").digest()
        actual_hash = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    except (OSError, RuntimeError, UnicodeError, ValueError):
        raise AgentReachBridgeError(_INCOMPATIBLE_EXECUTION_CONTRACT) from None
    if not hmac.compare_digest(actual_hash, expected_hash):
        raise AgentReachBridgeError(_INCOMPATIBLE_EXECUTION_CONTRACT)
    return expected_file


def _validated_execution_class(
    value: object,
    expected_name: str,
    expected_fields: tuple[str, ...],
    expected_origin: Path,
) -> type[object]:
    if (
        type(value) is not type
        or value.__module__ != _EXECUTION_CONTRACTS_MODULE
        or value.__name__ != expected_name
        or value.__qualname__ != expected_name
        or not is_dataclass(value)
    ):
        raise AgentReachBridgeError(_INCOMPATIBLE_EXECUTION_CONTRACT)
    value_type = cast(type[object], value)
    parameters = getattr(value_type, "__dataclass_params__", None)
    dataclass_fields = getattr(value_type, "__dataclass_fields__", None)
    slots = getattr(value_type, "__slots__", None)
    post_init = value_type.__dict__.get("__post_init__")
    is_fieldless_marker = expected_name in {"NetworkAccessV1", "PrivateWorkspaceV1"}
    if (
        getattr(parameters, "frozen", False) is not True
        or type(dataclass_fields) is not dict
        or tuple(dataclass_fields) != expected_fields
        or type(slots) is not tuple
        or slots != expected_fields
        or (is_fieldless_marker and post_init is not None)
        or (
            not is_fieldless_marker
            and (
                type(post_init) is not FunctionType
                or post_init.__module__ != _EXECUTION_CONTRACTS_MODULE
            )
        )
    ):
        raise AgentReachBridgeError(_INCOMPATIBLE_EXECUTION_CONTRACT)
    if is_fieldless_marker:
        return value_type
    assert isinstance(post_init, FunctionType)
    try:
        implementation_origin = Path(post_init.__code__.co_filename).resolve(
            strict=True
        )
    except (OSError, RuntimeError):
        raise AgentReachBridgeError(_INCOMPATIBLE_EXECUTION_CONTRACT) from None
    if implementation_origin != expected_origin:
        raise AgentReachBridgeError(_INCOMPATIBLE_EXECUTION_CONTRACT)
    return value_type


def _validated_execution_function(
    value: object,
    expected_name: str,
    expected_parameters: tuple[str, ...],
    expected_origin: Path,
    expected_module: str,
) -> FunctionType:
    if (
        type(value) is not FunctionType
        or not callable(value)
        or value.__module__ != expected_module
        or value.__name__ != expected_name
        or value.__qualname__ != expected_name
    ):
        raise AgentReachBridgeError(_INCOMPATIBLE_EXECUTION_CONTRACT)
    parameters = tuple(signature(value).parameters.values())
    if tuple(parameter.name for parameter in parameters) != expected_parameters or any(
        parameter.kind is not Parameter.POSITIONAL_OR_KEYWORD
        or parameter.default is not Parameter.empty
        for parameter in parameters
    ):
        raise AgentReachBridgeError(_INCOMPATIBLE_EXECUTION_CONTRACT)
    try:
        implementation_origin = Path(value.__code__.co_filename).resolve(strict=True)
    except (OSError, RuntimeError):
        raise AgentReachBridgeError(_INCOMPATIBLE_EXECUTION_CONTRACT) from None
    if implementation_origin != expected_origin:
        raise AgentReachBridgeError(_INCOMPATIBLE_EXECUTION_CONTRACT)
    return value


def _default_channel_loader() -> Sequence[object]:
    module = import_module("agent_reach.channels")
    loader = getattr(module, "get_all_channels", None)
    if not callable(loader):
        raise AgentReachBridgeError("Agent-Reach has no compatible channel loader.")
    return cast(Sequence[object], loader())


def _default_doctor_provider(
    config: ReadOnlyAgentReachConfig,
) -> Mapping[str, object]:
    return collect_agent_reach_health(config)


def collect_agent_reach_health(
    config: ReadOnlyAgentReachConfig,
    channel_loader: ChannelLoader | None = None,
) -> Mapping[str, object]:
    """Run only upstream checks reviewed not to inspect credentials or sessions."""

    if channel_loader is None:
        validate_agent_reach_execution_contract()
    loader = channel_loader if channel_loader is not None else _default_channel_loader
    report: dict[str, object] = {}
    for channel in loader():
        upstream_name = _channel_name(channel)
        if upstream_name not in SAFE_AGENT_REACH_DOCTOR_CHANNELS:
            report[upstream_name] = {
                "status": "off",
                "active_backend": None,
                "reach_policy": "connector_required",
            }
            continue
        check = _attribute(channel, "check")
        if not callable(check):
            raise AgentReachBridgeError(_INCOMPATIBLE_REGISTRY)
        try:
            result = check(config)
        except Exception:
            report[upstream_name] = {"status": "error", "active_backend": None}
            continue
        if (
            not isinstance(result, tuple)
            or len(result) != 2
            or result[0] not in _UPSTREAM_STATES
            or not isinstance(result[1], str)
        ):
            raise AgentReachBridgeError(_INCOMPATIBLE_DOCTOR)
        active_backend = _attribute(channel, "active_backend")
        report[upstream_name] = {
            "status": result[0],
            "active_backend": active_backend,
        }
    return report


def _channel_name(channel: object) -> str:
    return _required_string(channel, "name")


def _channel_description(channel: object) -> str:
    return _required_string(channel, "description")


def _channel_backends(channel: object) -> tuple[str, ...]:
    value = _attribute(channel, "backends")
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item for item in value
    ):
        raise AgentReachBridgeError(_INCOMPATIBLE_REGISTRY)
    return tuple(value)


def _channel_tier(channel: object) -> int:
    value = _attribute(channel, "tier")
    if isinstance(value, bool) or not isinstance(value, int) or value not in {0, 1, 2}:
        raise AgentReachBridgeError(_INCOMPATIBLE_REGISTRY)
    return value


def _required_string(channel: object, attribute: str) -> str:
    value = _attribute(channel, attribute)
    if not isinstance(value, str) or not value:
        raise AgentReachBridgeError(_INCOMPATIBLE_REGISTRY)
    return value


def _attribute(channel: object, attribute: str) -> object:
    try:
        return getattr(channel, attribute)
    except AttributeError as error:
        raise AgentReachBridgeError(_INCOMPATIBLE_REGISTRY) from error


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON object key")
        value[key] = item
    return value


def _reject_json_constant(_value: str) -> object:
    raise ValueError("invalid JSON constant")


def _closed_values_equal(
    actual: tuple[object, ...], expected: tuple[object, ...]
) -> bool:
    return len(actual) == len(expected) and all(
        _closed_value_equal(actual_value, expected_value)
        for actual_value, expected_value in zip(actual, expected, strict=True)
    )


def _closed_value_equal(actual: object, expected: object) -> bool:
    if type(actual) is not type(expected):
        return False
    if isinstance(expected, tuple):
        return _closed_values_equal(cast(tuple[object, ...], actual), expected)
    return actual == expected


def _availability(upstream_state: str, tier: int) -> HealthState:
    if upstream_state == "ok":
        return "available"
    if upstream_state == "warn":
        return "degraded"
    if upstream_state == "off":
        return "setup_required" if tier > 0 else "unavailable"
    return "degraded"


def _doctor_reason(upstream_state: str, tier: int) -> str:
    if upstream_state == "ok":
        return "Agent-Reach reports a usable backend."
    if upstream_state == "warn":
        return "Agent-Reach reports the selected backend is degraded."
    if upstream_state == "off" and tier > 0:
        return "Agent-Reach reports operator setup is required."
    if upstream_state == "off":
        return "Agent-Reach reports no usable backend in this environment."
    return "Agent-Reach could not complete its backend health check."
