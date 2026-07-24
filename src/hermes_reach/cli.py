"""Operator-facing, local-only CLI registration for ``hermes reach``."""

from __future__ import annotations

import argparse
from typing import Any

from .bootstrap import DEFAULT_RUNTIME
from .catalog import SOURCE_CATALOG
from .contracts import (
    ReachValidationError,
    error_response,
    internal_error_response,
    json_result,
    new_trace_id,
    success_response,
    validate_status,
)
from .runtime.release import check_release_pins
from .status import doctor_data, sources_data, status_data, unavailable_command_data


def register_cli(subparser: argparse.ArgumentParser) -> None:
    """Build the documented ``hermes reach`` subcommand tree."""

    commands = subparser.add_subparsers(dest="reach_command")

    status = commands.add_parser("status", help="Show local Reach capability status")
    _add_json_flag(status)
    status.add_argument("--source", action="append", dest="sources")

    sources = commands.add_parser("sources", help="List registered Reach sources")
    _add_json_flag(sources)

    doctor = commands.add_parser("doctor", help="Run the local-only Reach doctor")
    _add_json_flag(doctor)

    setup = commands.add_parser("setup", help="Configure Reach capabilities")
    setup.add_argument("--dry-run", action="store_true")
    setup.add_argument("--safe", action="store_true")
    setup.add_argument("--yes", action="store_true")
    _add_json_flag(setup)

    updates = commands.add_parser("updates", help="Inspect Reach updates")
    update_commands = updates.add_subparsers(dest="reach_updates_command")
    check = update_commands.add_parser("check", help="Check pinned Reach updates")
    _add_json_flag(check)

    subparser.set_defaults(func=reach_command)


def reach_command(args: argparse.Namespace) -> None:
    """Render a local-only command result for Hermes's CLI dispatcher."""

    print(render_command(args))


def render_command(args: argparse.Namespace) -> str:
    """Return command output without calling sys.exit for domain failures."""

    payload = command_payload(args)
    if getattr(args, "json", False):
        return json_result(payload)
    return _human_output(payload)


def command_payload(args: argparse.Namespace) -> dict[str, object]:
    """Execute only bundled catalog/status projections."""

    trace_id = new_trace_id()
    try:
        command = getattr(args, "reach_command", None)
        if command == "status":
            request = validate_status({"sources": getattr(args, "sources", None)})
            return success_response(
                trace_id,
                status_data(
                    request.sources,
                    request.include_planned,
                    DEFAULT_RUNTIME.operation_availability,
                ),
            )
        if command == "sources":
            return success_response(trace_id, sources_data(SOURCE_CATALOG))
        if command == "doctor":
            return success_response(
                trace_id,
                doctor_data(SOURCE_CATALOG, DEFAULT_RUNTIME.operation_availability),
            )
        if command == "setup":
            return _unavailable_response("setup", trace_id)
        if (
            command == "updates"
            and getattr(args, "reach_updates_command", None) == "check"
        ):
            return success_response(trace_id, check_release_pins().as_data())
        raise ReachValidationError(
            "invalid_argument",
            "A Reach subcommand is required.",
            "Use hermes reach status, sources, doctor, setup, or updates check.",
        )
    except ReachValidationError as error:
        return error_response(error, trace_id)
    except Exception:
        return internal_error_response(trace_id)


def _add_json_flag(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true", help="Emit JSON output")


def _unavailable_response(command: str, trace_id: str) -> dict[str, object]:
    details = unavailable_command_data(command)
    return {
        "protocol_version": "v1",
        "trace_id": trace_id,
        "outcome": "error",
        "groups": [],
        "error": details,
    }


def _human_output(payload: dict[str, object]) -> str:
    if payload["outcome"] == "error":
        error = payload["error"]
        if isinstance(error, dict):
            return f"{error['code']}: {error['message']}"
        return "internal_error: The command could not be processed."
    data: Any = payload["data"]
    catalog_version = data.get("catalog_version", "v1")
    if "sources" in data:
        return f"Hermes Reach catalog {catalog_version}: {len(data['sources'])} sources"
    return f"Hermes Reach catalog {catalog_version}"
