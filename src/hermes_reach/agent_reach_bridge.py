"""Lazy, compatibility-checked access to the pinned Agent-Reach package."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from importlib import import_module
from importlib.metadata import PackageNotFoundError, version
from types import MappingProxyType
from typing import Final, Literal, cast

from .catalog import SOURCE_CATALOG

AGENT_REACH_DISTRIBUTION: Final = "agent-reach"
AGENT_REACH_VERSION: Final = "1.5.0"
AGENT_REACH_COMMIT: Final = "1494c2ab239e7355a77e7cceaf3271453a1f34b5"
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
_INCOMPATIBLE_RSS_BACKEND: Final = (
    "Agent-Reach's RSS backend has an incompatible installed version."
)
_INCOMPATIBLE_REGISTRY: Final = "Agent-Reach has an incompatible channel registry."
_INCOMPATIBLE_DOCTOR: Final = "Agent-Reach returned an incompatible doctor report."
HealthState = Literal["available", "setup_required", "degraded", "unavailable"]


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


class ReadOnlyAgentReachConfig:
    """Satisfy channel checks without reading or writing upstream configuration."""

    __slots__ = ()

    def get(self, _key: str, default: object | None = None) -> object | None:
        return default

    def is_configured(self, _feature: str) -> bool:
        return False


ChannelLoader = Callable[[], Sequence[object]]
VersionReader = Callable[[str], str]
DoctorProvider = Callable[[ReadOnlyAgentReachConfig], Mapping[str, object]]
CatalogProvider = Callable[[], AgentReachCatalog]


def load_agent_reach_catalog(
    channel_loader: ChannelLoader | None = None,
    version_reader: VersionReader = version,
) -> AgentReachCatalog:
    """Load and validate upstream channel metadata without executing a probe."""

    installed_version = _installed_version(AGENT_REACH_DISTRIBUTION, version_reader)
    if installed_version != AGENT_REACH_VERSION:
        raise AgentReachBridgeError(_INCOMPATIBLE_VERSION)
    if (
        _installed_version(FEEDPARSER_DISTRIBUTION, version_reader)
        != FEEDPARSER_VERSION
    ):
        raise AgentReachBridgeError(_INCOMPATIBLE_RSS_BACKEND)

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
    return AgentReachCatalog(installed_version, tuple(projected))


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
        "channels": channels,
    }


def _installed_version(distribution: str, version_reader: VersionReader) -> str:
    try:
        return version_reader(distribution)
    except PackageNotFoundError as error:
        raise AgentReachBridgeError(
            f"The required {distribution} distribution is not installed."
        ) from error


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
