"""Local capability status projections backed only by bundled catalog data."""

from __future__ import annotations

from collections.abc import Callable, Mapping

from .catalog import CATALOG_VERSION, SourceSpec
from .runtime.availability import AvailabilityRecord

AvailabilityResolver = Callable[[str, str], AvailabilityRecord]


def _catalog_availability(source: str, operation: str) -> AvailabilityRecord:
    del source, operation
    return AvailabilityRecord(
        "unavailable", "No source adapter is installed in the foundation release."
    )


def source_status(
    source: SourceSpec,
    include_planned: bool,
    resolver: AvailabilityResolver = _catalog_availability,
) -> dict[str, object]:
    """Describe one source without probing a backend or environment."""

    selected = [
        operation
        for operation in source.operations
        if include_planned or operation.implementation_state != "planned"
    ]
    operations: list[dict[str, object]] = []
    records: list[AvailabilityRecord] = []
    for operation in selected:
        record = resolver(source.name, operation.name)
        records.append(record)
        operations.append(
            {
                "name": operation.name,
                "tool": operation.tool,
                "alpha_wave": operation.alpha_wave,
                "access_class": operation.access_class,
                "release_state": operation.implementation_state,
                "availability": record.state,
                "reason": record.reason,
            }
        )
    source_availability = _aggregate_availability(records)
    return {
        "source": source.name,
        "display_name": source.display_name,
        "alpha_wave": source.alpha_wave,
        "access_class": source.access_class,
        "availability": source_availability.state,
        "reason": source_availability.reason,
        "operations": operations,
    }


def status_data(
    sources: tuple[SourceSpec, ...],
    include_planned: bool,
    resolver: AvailabilityResolver = _catalog_availability,
) -> dict[str, object]:
    """Return a stable local status projection in caller source order."""

    return {
        "catalog_version": CATALOG_VERSION,
        "sources": [
            source_status(source, include_planned, resolver) for source in sources
        ],
    }


def doctor_data(
    sources: tuple[SourceSpec, ...],
    resolver: AvailabilityResolver = _catalog_availability,
) -> dict[str, object]:
    """Return a no-network doctor report for the foundation release."""

    return {
        "catalog_version": CATALOG_VERSION,
        "overall_availability": _aggregate_availability(
            [
                resolver(source.name, operation.name)
                for source in sources
                for operation in source.operations
            ]
        ).state,
        "network_checked": False,
        "sources": [
            source_status(source, include_planned=True, resolver=resolver)
            for source in sources
        ],
    }


def sources_data(sources: tuple[SourceSpec, ...]) -> dict[str, object]:
    """Return catalog identities without exposing backend configuration."""

    return {
        "catalog_version": CATALOG_VERSION,
        "sources": [
            {
                "source": source.name,
                "display_name": source.display_name,
                "alpha_wave": source.alpha_wave,
                "access_class": source.access_class,
                "operations": [operation.name for operation in source.operations],
            }
            for source in sources
        ],
    }


def unavailable_command_data(command: str) -> Mapping[str, object]:
    """Return the same safe mutation denial for foundation placeholders."""

    return {
        "code": "capability_unavailable",
        "message": (
            "This operator command is not implemented in the foundation release."
        ),
        "remediation": (
            "Use status, sources, or doctor; complete the dedicated task before "
            "setup or updates."
        ),
        "details": {"command": command},
    }


def _aggregate_availability(
    records: list[AvailabilityRecord],
) -> AvailabilityRecord:
    if not records:
        return AvailabilityRecord("unavailable", "No released operation is visible.")
    for state in ("available", "degraded", "setup_required", "unavailable"):
        record = next((item for item in records if item.state == state), None)
        if record is not None:
            return AvailabilityRecord(record.state, record.reason)
    return AvailabilityRecord("unavailable", "No operation state is available.")
