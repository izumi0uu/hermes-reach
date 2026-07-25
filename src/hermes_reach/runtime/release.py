"""Offline package and catalog pin inspection for ``reach updates check``."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass
from importlib.metadata import PackageNotFoundError, version
from typing import Literal

from ..agent_reach_bridge import AGENT_REACH_COMMIT, AGENT_REACH_VERSION
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
    if hermes_version is None:
        return ReleaseReport(
            "unavailable",
            package_version,
            None,
            agent_reach_version,
            CATALOG_VERSION,
            PINNED_AGENT_REACH_BASELINE,
            "The supported Hermes host package is not installed.",
        )
    if agent_reach_version is None:
        return ReleaseReport(
            "unavailable",
            package_version,
            hermes_version,
            None,
            CATALOG_VERSION,
            PINNED_AGENT_REACH_BASELINE,
            "The pinned Agent-Reach package is not installed.",
        )
    if not _is_supported_hermes(hermes_version):
        return ReleaseReport(
            "degraded",
            package_version,
            hermes_version,
            agent_reach_version,
            CATALOG_VERSION,
            PINNED_AGENT_REACH_BASELINE,
            "The installed Hermes version is outside the supported 0.19 release line.",
        )
    if agent_reach_version != PINNED_AGENT_REACH_VERSION:
        return ReleaseReport(
            "degraded",
            package_version,
            hermes_version,
            agent_reach_version,
            CATALOG_VERSION,
            PINNED_AGENT_REACH_BASELINE,
            "The installed Agent-Reach version differs from the pinned 1.5.0 release.",
        )
    return ReleaseReport(
        "current",
        package_version,
        hermes_version,
        agent_reach_version,
        CATALOG_VERSION,
        PINNED_AGENT_REACH_BASELINE,
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
