from __future__ import annotations

import json
from collections.abc import Callable
from importlib.metadata import PackageNotFoundError

from hermes_reach.agent_reach_bridge import (
    AGENT_REACH_FORK_COMMIT,
    AGENT_REACH_FORK_URL,
    AGENT_REACH_OFFICIAL_BASE_COMMIT,
)
from hermes_reach.runtime.release import ReleaseReport, check_release_pins


def _direct_url(_: str) -> str:
    return json.dumps(
        {
            "url": AGENT_REACH_FORK_URL,
            "vcs_info": {
                "vcs": "git",
                "requested_revision": AGENT_REACH_FORK_COMMIT,
                "commit_id": AGENT_REACH_FORK_COMMIT,
            },
        }
    )


def _release(version_reader: Callable[[str], str]) -> ReleaseReport:
    return check_release_pins(version_reader, _direct_url)


def test_release_check_reports_current_pins_from_injected_metadata() -> None:
    report = _release(
        lambda package: {
            "hermes-agent": "0.19.0",
            "agent-reach": "1.5.0",
            "feedparser": "6.0.12",
            "bilibili-cli": "0.6.2",
            "yt-dlp": "2026.7.4",
            "yt-dlp-ejs": "0.8.0",
            "deno": "2.8.3",
        }.get(package, "0.1.0a2")
    )

    assert report.status == "current"
    assert report.hermes_version == "0.19.0"
    assert report.agent_reach_version == "1.5.0"
    assert report.feedparser_version == "6.0.12"
    assert report.bilibili_cli_version == "0.6.2"
    assert report.yt_dlp_version == "2026.7.4"
    assert report.yt_dlp_ejs_version == "0.8.0"
    assert report.deno_version == "2.8.3"
    assert report.catalog_version == "v1"
    assert report.agent_reach_baseline == AGENT_REACH_FORK_COMMIT
    assert report.agent_reach_official_base_commit == AGENT_REACH_OFFICIAL_BASE_COMMIT
    assert report.agent_reach_fork_commit == AGENT_REACH_FORK_COMMIT
    assert report.agent_reach_protocol_version == "v1"
    assert report.agent_reach_expected_protocol_version == "v1"


def test_release_check_reports_unsupported_hermes_as_degraded() -> None:
    report = _release(lambda package: "1.5.0" if package == "agent-reach" else "0.20.0")

    assert report.status == "degraded"
    assert "outside" in report.reason


def test_release_check_does_not_assume_missing_metadata_is_current() -> None:
    def missing(_: str) -> str:
        raise PackageNotFoundError

    report = _release(missing)

    assert report.status == "unavailable"
    assert report.hermes_version is None


def test_release_check_reports_an_incompatible_agent_reach_version() -> None:
    report = _release(
        lambda package: {
            "hermes-agent": "0.19.0",
            "agent-reach": "1.5.1",
        }.get(package, "0.1.0a2")
    )

    assert report.status == "degraded"
    assert report.agent_reach_version == "1.5.1"
    assert "Agent-Reach" in report.reason


def test_release_check_reports_an_incompatible_rss_backend_version() -> None:
    report = _release(
        lambda package: {
            "hermes-agent": "0.19.0",
            "agent-reach": "1.5.0",
            "feedparser": "6.0.11",
        }.get(package, "0.1.0a2")
    )

    assert report.status == "degraded"
    assert report.feedparser_version == "6.0.11"
    assert "feedparser" in report.reason


def test_release_check_reports_a_missing_rss_backend() -> None:
    def versions(package: str) -> str:
        if package == "feedparser":
            raise PackageNotFoundError
        return {
            "hermes-agent": "0.19.0",
            "agent-reach": "1.5.0",
        }.get(package, "0.1.0a2")

    report = _release(versions)

    assert report.status == "unavailable"
    assert report.feedparser_version is None
    assert "RSS backend" in report.reason


def test_release_check_reports_an_incompatible_bilibili_backend_version() -> None:
    report = _release(
        lambda package: {
            "hermes-agent": "0.19.0",
            "agent-reach": "1.5.0",
            "feedparser": "6.0.12",
            "bilibili-cli": "0.6.1",
        }.get(package, "0.1.0a2")
    )

    assert report.status == "degraded"
    assert report.bilibili_cli_version == "0.6.1"
    assert "bili-cli" in report.reason


def test_release_check_reports_a_missing_bilibili_backend() -> None:
    def versions(package: str) -> str:
        if package == "bilibili-cli":
            raise PackageNotFoundError
        return {
            "hermes-agent": "0.19.0",
            "agent-reach": "1.5.0",
            "feedparser": "6.0.12",
        }.get(package, "0.1.0a2")

    report = _release(versions)

    assert report.status == "unavailable"
    assert report.bilibili_cli_version is None
    assert "Bilibili backend" in report.reason


def test_release_check_reports_youtube_dependency_drift_and_absence() -> None:
    current = {
        "hermes-reach": "0.1.0a2",
        "hermes-agent": "0.19.0",
        "agent-reach": "1.5.0",
        "feedparser": "6.0.12",
        "bilibili-cli": "0.6.2",
        "yt-dlp": "2026.7.4",
        "yt-dlp-ejs": "0.8.0",
        "deno": "2.8.3",
    }

    drifted = _release(
        lambda package: "2026.7.3" if package == "yt-dlp" else current[package]
    )

    def missing_ejs(package: str) -> str:
        if package == "yt-dlp-ejs":
            raise PackageNotFoundError
        return current[package]

    missing = _release(missing_ejs)
    deno_drift = _release(
        lambda package: "2.8.2" if package == "deno" else current[package]
    )

    assert drifted.status == "degraded"
    assert drifted.yt_dlp_version == "2026.7.3"
    assert missing.status == "unavailable"
    assert missing.yt_dlp_ejs_version is None
    assert deno_drift.status == "degraded"
    assert deno_drift.deno_version == "2.8.2"


def test_release_check_reports_owner_fork_provenance_drift() -> None:
    current = {
        "hermes-reach": "0.1.0a2",
        "hermes-agent": "0.19.0",
        "agent-reach": "1.5.0",
        "feedparser": "6.0.12",
        "bilibili-cli": "0.6.2",
        "yt-dlp": "2026.7.4",
        "yt-dlp-ejs": "0.8.0",
        "deno": "2.8.3",
    }
    drifted = json.loads(_direct_url("agent-reach"))
    drifted["vcs_info"]["requested_revision"] = "hermes/execution-v1"

    report = check_release_pins(
        current.__getitem__,
        lambda _: json.dumps(drifted),
    )

    assert report.status == "degraded"
    assert "owner-fork pin" in report.reason
    assert report.agent_reach_fork_commit == AGENT_REACH_FORK_COMMIT
    assert report.agent_reach_protocol_version is None
    assert report.agent_reach_expected_protocol_version == "v1"


def test_release_check_requires_the_verified_execution_api_before_current() -> None:
    current = {
        "hermes-reach": "0.1.0a2",
        "hermes-agent": "0.19.0",
        "agent-reach": "1.5.0",
        "feedparser": "6.0.12",
        "bilibili-cli": "0.6.2",
        "yt-dlp": "2026.7.4",
        "yt-dlp-ejs": "0.8.0",
        "deno": "2.8.3",
    }

    report = check_release_pins(
        current.__getitem__,
        _direct_url,
        execution_module_loader=lambda: object(),
    )

    assert report.status == "degraded"
    assert "execution API" in report.reason
    assert report.agent_reach_protocol_version is None
    assert report.agent_reach_expected_protocol_version == "v1"
