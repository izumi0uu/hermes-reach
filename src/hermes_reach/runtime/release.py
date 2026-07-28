"""Offline package and catalog pin inspection for ``reach updates check``."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass
from importlib.metadata import PackageNotFoundError, version
from typing import Literal

from ..agent_reach_bridge import (
    AGENT_REACH_COMMIT,
    AGENT_REACH_VERSION,
    BILIBILI_CLI_DISTRIBUTION,
    BILIBILI_CLI_VERSION,
    DENO_DISTRIBUTION,
    DENO_VERSION,
    FEEDPARSER_DISTRIBUTION,
    FEEDPARSER_VERSION,
    YTDLP_DISTRIBUTION,
    YTDLP_EJS_DISTRIBUTION,
    YTDLP_EJS_VERSION,
    YTDLP_VERSION,
)
from ..catalog import CATALOG_VERSION

PINNED_AGENT_REACH_BASELINE = AGENT_REACH_COMMIT
PINNED_AGENT_REACH_VERSION = AGENT_REACH_VERSION
ReleaseStatus = Literal["current", "degraded", "unavailable"]
VersionReader = Callable[[str], str]


@dataclass(frozen=True, slots=True)
class ReleaseReport:
    """A local compatibility conclusion with no remote update lookup."""

    status: ReleaseStatus
    package_version: str | None
    hermes_version: str | None
    agent_reach_version: str | None
    feedparser_version: str | None
    bilibili_cli_version: str | None
    yt_dlp_version: str | None
    yt_dlp_ejs_version: str | None
    deno_version: str | None
    catalog_version: str
    agent_reach_baseline: str
    reason: str

    def as_data(self) -> dict[str, object]:
        """Return JSON-compatible data for the operator CLI response."""

        return asdict(self)


def check_release_pins(version_reader: VersionReader = version) -> ReleaseReport:
    """Inspect installed metadata and bundled constants without a registry call."""

    package_version = _read_version("hermes-reach", version_reader)
    hermes_version = _read_version("hermes-agent", version_reader)
    agent_reach_version = _read_version("agent-reach", version_reader)
    feedparser_version = _read_version(FEEDPARSER_DISTRIBUTION, version_reader)
    bilibili_cli_version = _read_version(BILIBILI_CLI_DISTRIBUTION, version_reader)
    yt_dlp_version = _read_version(YTDLP_DISTRIBUTION, version_reader)
    yt_dlp_ejs_version = _read_version(YTDLP_EJS_DISTRIBUTION, version_reader)
    deno_version = _read_version(DENO_DISTRIBUTION, version_reader)

    def report(status: ReleaseStatus, reason: str) -> ReleaseReport:
        return ReleaseReport(
            status=status,
            package_version=package_version,
            hermes_version=hermes_version,
            agent_reach_version=agent_reach_version,
            feedparser_version=feedparser_version,
            bilibili_cli_version=bilibili_cli_version,
            yt_dlp_version=yt_dlp_version,
            yt_dlp_ejs_version=yt_dlp_ejs_version,
            deno_version=deno_version,
            catalog_version=CATALOG_VERSION,
            agent_reach_baseline=PINNED_AGENT_REACH_BASELINE,
            reason=reason,
        )

    if hermes_version is None:
        return report(
            "unavailable",
            "The supported Hermes host package is not installed.",
        )
    if agent_reach_version is None:
        return report(
            "unavailable",
            "The pinned Agent-Reach package is not installed.",
        )
    if not _is_supported_hermes(hermes_version):
        return report(
            "degraded",
            "The installed Hermes version is outside the supported 0.19 release line.",
        )
    if agent_reach_version != PINNED_AGENT_REACH_VERSION:
        return report(
            "degraded",
            "The installed Agent-Reach version differs from the pinned 1.5.0 release.",
        )
    if feedparser_version is None:
        return report(
            "unavailable",
            "The exact Agent-Reach RSS backend is not installed.",
        )
    if feedparser_version != FEEDPARSER_VERSION:
        return report(
            "degraded",
            "The installed feedparser version differs from the pinned 6.0.12 backend.",
        )
    if bilibili_cli_version is None:
        return report(
            "unavailable",
            "The exact Agent-Reach Bilibili backend is not installed.",
        )
    if bilibili_cli_version != BILIBILI_CLI_VERSION:
        return report(
            "degraded",
            "The installed bili-cli version differs from the pinned 0.6.2 backend.",
        )
    if yt_dlp_version is None:
        return report(
            "unavailable",
            "The exact Agent-Reach YouTube backend is not installed.",
        )
    if yt_dlp_version != YTDLP_VERSION:
        return report(
            "degraded",
            "The installed yt-dlp version differs from the pinned 2026.7.4 backend.",
        )
    if yt_dlp_ejs_version is None:
        return report(
            "unavailable",
            "The exact packaged yt-dlp EJS solver is not installed.",
        )
    if yt_dlp_ejs_version != YTDLP_EJS_VERSION:
        return report(
            "degraded",
            "The installed yt-dlp-ejs version differs from the pinned 0.8.0 solver.",
        )
    if deno_version is None:
        return report(
            "unavailable",
            "The exact packaged Deno runtime is not installed.",
        )
    if deno_version != DENO_VERSION:
        return report(
            "degraded",
            "The installed Deno version differs from the pinned 2.8.3 runtime.",
        )
    return report(
        "current",
        "Installed package metadata matches the bundled Reach compatibility pins.",
    )


def _read_version(package: str, version_reader: VersionReader) -> str | None:
    try:
        return version_reader(package)
    except PackageNotFoundError:
        return None


def _is_supported_hermes(value: str) -> bool:
    parts = value.split(".")
    if len(parts) < 2:
        return False
    try:
        return int(parts[0]) == 0 and int(parts[1]) == 19
    except ValueError:
        return False
