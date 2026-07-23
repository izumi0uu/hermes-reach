from __future__ import annotations

from importlib.metadata import PackageNotFoundError

from hermes_reach.runtime.release import check_release_pins


def test_release_check_reports_current_pins_from_injected_metadata() -> None:
    report = check_release_pins(
        lambda package: "0.19.0" if package == "hermes-agent" else "0.1.0a0"
    )

    assert report.status == "current"
    assert report.hermes_version == "0.19.0"
    assert report.catalog_version == "v1"


def test_release_check_reports_unsupported_hermes_as_degraded() -> None:
    report = check_release_pins(lambda _: "0.20.0")

    assert report.status == "degraded"
    assert "outside" in report.reason


def test_release_check_does_not_assume_missing_metadata_is_current() -> None:
    def missing(_: str) -> str:
        raise PackageNotFoundError

    report = check_release_pins(missing)

    assert report.status == "unavailable"
    assert report.hermes_version is None
