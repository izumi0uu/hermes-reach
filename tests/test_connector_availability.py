from __future__ import annotations

from hermes_reach.catalog import get_source
from hermes_reach.runtime.availability import AvailabilityRecord
from hermes_reach.status import source_status


def _exa_source():
    source = get_source("exa")
    assert source is not None
    return source


def test_default_status_operation_shape_has_no_connector_fields() -> None:
    result = source_status(_exa_source(), include_planned=False)

    assert set(result["operations"][0]) == {
        "name",
        "tool",
        "alpha_wave",
        "access_class",
        "release_state",
        "availability",
        "reason",
    }


def test_connector_availability_fields_are_projected_when_present() -> None:
    def resolve(source: str, operation: str) -> AvailabilityRecord:
        assert source == "exa"
        assert operation in {"search.web", "search.code"}
        return AvailabilityRecord(
            "degraded",
            "The paired Connector snapshot is stale.",
            "connector-v1",
            "1",
            "connector_offline",
            1_753_510_400,
        )

    result = source_status(_exa_source(), include_planned=False, resolver=resolve)

    for operation in result["operations"]:
        assert operation["cause_code"] == "connector_offline"
        assert operation["snapshot_at"] == 1_753_510_400


def test_local_available_operation_wins_source_aggregation() -> None:
    def resolve(source: str, operation: str) -> AvailabilityRecord:
        assert source == "exa"
        if operation == "search.web":
            return AvailabilityRecord(
                "degraded",
                "The paired Connector snapshot is stale.",
                cause_code="connector_offline",
                snapshot_at=1_753_510_400,
            )
        return AvailabilityRecord("available", "A local adapter is available.")

    result = source_status(_exa_source(), include_planned=False, resolver=resolve)

    assert result["availability"] == "available"
    operations = {operation["name"]: operation for operation in result["operations"]}
    assert operations["search.web"]["availability"] == "degraded"
    assert operations["search.web"]["cause_code"] == "connector_offline"
    assert operations["search.code"]["availability"] == "available"
    assert "cause_code" not in operations["search.code"]
    assert "snapshot_at" not in operations["search.code"]
