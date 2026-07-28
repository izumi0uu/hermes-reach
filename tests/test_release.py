from __future__ import annotations

from importlib.metadata import PackageNotFoundError

from hermes_reach.runtime.release import check_release_pins


def test_release_check_reports_current_pins_from_injected_metadata() -> None:
    report = check_release_pins(
        lambda package: {
            "hermes-agent": "0.19.0",
            "agent-reach": "1.5.0",
            "feedparser": "6.0.12",
            "bilibili-cli": "0.6.2",
        }.get(package, "0.1.0a0")
    )

    assert report.status == "current"
    assert report.hermes_version == "0.19.0"
    assert report.agent_reach_version == "1.5.0"
    assert report.feedparser_version == "6.0.12"
    assert report.bilibili_cli_version == "0.6.2"
    assert report.catalog_version == "v1"


def test_release_check_reports_unsupported_hermes_as_degraded() -> None:
    report = check_release_pins(
        lambda package: "1.5.0" if package == "agent-reach" else "0.20.0"
    )

    assert report.status == "degraded"
    assert "outside" in report.reason


def test_release_check_does_not_assume_missing_metadata_is_current() -> None:
    def missing(_: str) -> str:
        raise PackageNotFoundError

    report = check_release_pins(missing)

    assert report.status == "unavailable"
    assert report.hermes_version is None


def test_release_check_reports_an_incompatible_agent_reach_version() -> None:
    report = check_release_pins(
        lambda package: {
            "hermes-agent": "0.19.0",
            "agent-reach": "1.5.1",
        }.get(package, "0.1.0a0")
    )

    assert report.status == "degraded"
    assert report.agent_reach_version == "1.5.1"
    assert "Agent-Reach" in report.reason


def test_release_check_reports_an_incompatible_rss_backend_version() -> None:
    report = check_release_pins(
        lambda package: {
            "hermes-agent": "0.19.0",
            "agent-reach": "1.5.0",
            "feedparser": "6.0.11",
        }.get(package, "0.1.0a0")
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
        }.get(package, "0.1.0a0")

    report = check_release_pins(versions)

    assert report.status == "unavailable"
    assert report.feedparser_version is None
    assert "RSS backend" in report.reason


def test_release_check_reports_an_incompatible_bilibili_backend_version() -> None:
    report = check_release_pins(
        lambda package: {
            "hermes-agent": "0.19.0",
            "agent-reach": "1.5.0",
            "feedparser": "6.0.12",
            "bilibili-cli": "0.6.1",
        }.get(package, "0.1.0a0")
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
        }.get(package, "0.1.0a0")

    report = check_release_pins(versions)

    assert report.status == "unavailable"
    assert report.bilibili_cli_version is None
    assert "Bilibili backend" in report.reason
