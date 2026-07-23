"""Local capability status projections backed only by bundled catalog data."""

from __future__ import annotations

from collections.abc import Mapping

from .catalog import CATALOG_VERSION, SourceSpec


def source_status(source: SourceSpec, include_planned: bool) -> dict[str, object]:
    """Describe one source without probing a backend or environment."""

    operations: list[dict[str, object]] = []
    if include_planned:
        operations = [
            {
                "name": operation.name,
                "tool": operation.tool,
                "alpha_wave": operation.alpha_wave,
                "access_class": operation.access_class,
                "release_state": operation.implementation_state,
                "availability": "unavailable",
                "reason": operation.unavailable_reason,
            }
            for operation in source.operations
        ]
    return {
        "source": source.name,
        "display_name": source.display_name,
        "alpha_wave": source.alpha_wave,
        "access_class": source.access_class,
        "availability": "unavailable",
        "reason": "No source adapter is installed in the foundation release.",
        "operations": operations,
    }


def status_data(
    sources: tuple[SourceSpec, ...], include_planned: bool
) -> dict[str, object]:
    """Return a stable local status projection in caller source order."""

    return {
        "catalog_version": CATALOG_VERSION,
        "sources": [source_status(source, include_planned) for source in sources],
    }


def doctor_data(sources: tuple[SourceSpec, ...]) -> dict[str, object]:
    """Return a no-network doctor report for the foundation release."""

    return {
        "catalog_version": CATALOG_VERSION,
        "overall_availability": "unavailable",
        "network_checked": False,
        "sources": [source_status(source, include_planned=True) for source in sources],
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
