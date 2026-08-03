"""Operator-facing, local-only CLI registration for ``hermes reach``."""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path
from typing import Any, Protocol, cast

from .agent_reach_bridge import AgentReachBridgeError, upstream_doctor_data
from .bootstrap import DEFAULT_RUNTIME
from .catalog import SOURCE_CATALOG, get_operation, get_source
from .connector.cli import (
    ConnectorInspector,
    OfflineConnectorInspector,
    execute_connector_inspection,
    register_connector_cli,
    render_connector_error,
)
from .connector.client import (
    PairingDisplay,
    VpsPairingOrchestrator,
    VpsProfileStore,
)
from .connector.errors import ConnectorError, ConnectorErrorCode
from .connector.execution import ConnectorExecutionComposition
from .connector.identity import TtyPassphraseReader, VpsKeyStore
from .connector.limits import DEFAULT_GRANT_TTL_SECONDS, DEFAULT_GRANT_USES
from .connector.protocol import (
    GrantScope,
    ProtocolValidationError,
    PublicBackendIdentity,
    format_grant_scope,
)
from .connector.service import ConnectorService
from .connector.transport import WssEndpoint
from .contracts import (
    ReachValidationError,
    error_response,
    internal_error_response,
    json_result,
    new_trace_id,
    success_response,
    validate_status,
)
from .runtime.dispatcher import RuntimeDispatcher
from .runtime.release import check_release_pins
from .sources.opencli_social import (
    attest_opencli_social_session,
    opencli_social_execution_composition,
    opencli_social_scopes,
)
from .sources.opencli_social_contract import OPENCLI_SOCIAL_BACKEND
from .sources.xueqiu import XUEQIU_BACKEND
from .sources.xueqiu_activation import activate_xueqiu_binding
from .status import doctor_data, sources_data, status_data, unavailable_command_data

_RUNTIME: RuntimeDispatcher = DEFAULT_RUNTIME
_SOCIAL_SERVE_PATH_FIELDS = (
    "opencli_social_node",
    "opencli_social_root",
    "opencli_social_cli",
    "opencli_social_session_home",
)
_ActivationSummary = tuple[
    PublicBackendIdentity,
    tuple[tuple[str, str], ...],
    tuple[GrantScope, ...],
    tuple[str, ...],
]


def register_cli(subparser: argparse.ArgumentParser) -> None:
    """Build the documented ``hermes reach`` subcommand tree."""

    commands = subparser.add_subparsers(dest="reach_command")

    status = commands.add_parser("status", help="Show local Reach capability status")
    _add_json_flag(status)
    status.add_argument("--source", action="append", dest="sources")

    sources = commands.add_parser("sources", help="List registered Reach sources")
    _add_json_flag(sources)

    doctor = commands.add_parser("doctor", help="Inspect Reach capabilities")
    _add_json_flag(doctor)
    doctor.add_argument(
        "--upstream",
        action="store_true",
        help="Run the explicit Agent-Reach backend health check.",
    )

    setup = commands.add_parser("setup", help="Configure Reach capabilities")
    setup.add_argument("--dry-run", action="store_true")
    setup.add_argument("--safe", action="store_true")
    setup.add_argument("--yes", action="store_true")
    _add_json_flag(setup)

    updates = commands.add_parser("updates", help="Inspect Reach updates")
    update_commands = updates.add_subparsers(dest="reach_updates_command")
    check = update_commands.add_parser("check", help="Check pinned Reach updates")
    _add_json_flag(check)

    connector = commands.add_parser(
        "connector", help="Operate the trusted foreground Connector"
    )
    connector_commands = connector.add_subparsers(dest="reach_connector_command")
    register_connector_cli(connector_commands, connector_command)

    subparser.set_defaults(func=reach_command)


def reach_command(args: argparse.Namespace) -> None:
    """Render a local-only command result for Hermes's CLI dispatcher."""

    print(render_command(args))


class ConnectorMutationService(Protocol):
    """Injected owner of Connector identity, pairing, and service mutations."""

    def initialize_connector(
        self, state_directory: Path, reader: TtyPassphraseReader
    ) -> None: ...

    def initialize_vps(self, state_directory: Path) -> str: ...

    def pair(self, args: argparse.Namespace, reader: TtyPassphraseReader) -> None: ...

    def serve(
        self,
        state_directory: Path,
        *,
        reader: TtyPassphraseReader,
        bind_host: str,
        port: int,
        execution_composition: ConnectorExecutionComposition | None = None,
    ) -> None: ...


class _DefaultConnectorMutationService:
    def initialize_connector(
        self, state_directory: Path, reader: TtyPassphraseReader
    ) -> None:
        ConnectorService.initialize_state_directory(state_directory, tty_reader=reader)

    def initialize_vps(self, state_directory: Path) -> str:
        return VpsKeyStore(state_directory).initialize().fingerprint

    def pair(self, args: argparse.Namespace, reader: TtyPassphraseReader) -> None:
        asyncio.run(_pair_vps(args, reader))

    def serve(
        self,
        state_directory: Path,
        *,
        reader: TtyPassphraseReader,
        bind_host: str,
        port: int,
        execution_composition: ConnectorExecutionComposition | None = None,
    ) -> None:
        service = ConnectorService.open_state_directory(
            state_directory,
            tty_reader=reader,
            bind_host=bind_host,
            port=port,
            execution_composition=execution_composition,
        )
        asyncio.run(service.serve_foreground())


def connector_command(
    args: argparse.Namespace,
    *,
    mutation_service: ConnectorMutationService | None = None,
    inspection_service: ConnectorInspector | None = None,
) -> None:
    """Execute one bounded Connector operator command."""

    command = getattr(args, "reach_connector_command", None)
    if command in {"status", "devices", "grants"}:
        inspector = (
            OfflineConnectorInspector()
            if inspection_service is None
            else inspection_service
        )
        try:
            print(execute_connector_inspection(args, inspector))
        except ConnectorError as error:
            print(
                render_connector_error(
                    error, json_output=getattr(args, "json", False) is True
                ),
                file=sys.stderr,
            )
            raise SystemExit(1) from None
        return

    reader: TtyPassphraseReader | None = None
    try:
        reader = TtyPassphraseReader()
        mutations = (
            _DefaultConnectorMutationService()
            if mutation_service is None
            else mutation_service
        )
        state_directory = getattr(args, "state_directory", None)
        if not isinstance(state_directory, Path):
            raise ConnectorError(ConnectorErrorCode.CONNECTOR_STATE_INVALID)
        if command == "init":
            role = getattr(args, "role", None)
            if role == "connector":
                mutations.initialize_connector(state_directory, reader)
                reader._write("Connector state initialized.\n")
                return
            if role == "vps":
                fingerprint = mutations.initialize_vps(state_directory)
                reader._write(
                    f"VPS identity initialized.\nfingerprint: {fingerprint}\n"
                )
                return
            raise ConnectorError(ConnectorErrorCode.CONNECTOR_STATE_INVALID)
        if command == "pair":
            mutations.pair(args, reader)
            return
        if command == "serve":
            bind_host = getattr(args, "bind_host", None)
            port = getattr(args, "port", None)
            if type(bind_host) is not str or type(port) is not int:
                raise ConnectorError(ConnectorErrorCode.CONNECTOR_STATE_INVALID)
            social_inputs = _optional_path_group(args, _SOCIAL_SERVE_PATH_FIELDS)
            xueqiu_manifest = getattr(args, "xueqiu_binding_manifest", None)
            if xueqiu_manifest is not None and not isinstance(xueqiu_manifest, Path):
                raise ConnectorError(ConnectorErrorCode.CONNECTOR_STATE_INVALID)
            if social_inputs is None and xueqiu_manifest is None:
                mutations.serve(
                    state_directory,
                    reader=reader,
                    bind_host=bind_host,
                    port=port,
                )
                return

            compositions: list[ConnectorExecutionComposition] = []
            summaries: list[_ActivationSummary] = []
            if social_inputs is not None:
                social_attestation = attest_opencli_social_session(
                    social_inputs[0],
                    social_inputs[1],
                    social_inputs[2],
                    social_inputs[3],
                )
                social_scopes = opencli_social_scopes()
                compositions.append(
                    opencli_social_execution_composition(social_attestation)
                )
                summaries.append(
                    (
                        OPENCLI_SOCIAL_BACKEND,
                        (
                            ("node-sha256", social_attestation.node_sha256),
                            (
                                "opencli-tree-sha256",
                                social_attestation.opencli_tree_sha256,
                            ),
                        ),
                        social_scopes,
                        (),
                    )
                )
            if isinstance(xueqiu_manifest, Path):
                xueqiu_activation = activate_xueqiu_binding(xueqiu_manifest)
                compositions.append(xueqiu_activation.composition)
                summaries.append(
                    (
                        XUEQIU_BACKEND,
                        (("bws-sha256", xueqiu_activation.bws_sha256),),
                        (xueqiu_activation.scope,),
                        (),
                    )
                )

            reader._write(_render_activation_summary(tuple(summaries)))
            if not reader._confirm("Type enable to continue: ", "enable"):
                raise ConnectorError(ConnectorErrorCode.INTERACTIVE_UNLOCK_REQUIRED)
            composition = ConnectorExecutionComposition.combine(compositions)
            mutations.serve(
                state_directory,
                reader=reader,
                bind_host=bind_host,
                port=port,
                execution_composition=composition,
            )
            return
        raise ConnectorError(ConnectorErrorCode.CONNECTOR_STATE_INVALID)
    except ConnectorError as error:
        if reader is not None:
            reader._write(f"{error.code}: {error.message}\n")
        else:
            print(f"{error.code}: {error.message}", file=sys.stderr)
    finally:
        if reader is not None:
            reader.close()


async def _pair_vps(args: argparse.Namespace, reader: TtyPassphraseReader) -> None:
    state_directory = getattr(args, "state_directory", None)
    endpoint_value = getattr(args, "connector_endpoint", None)
    device_label = getattr(args, "device_label", None)
    raw_scopes = getattr(args, "scope", None)
    if (
        not isinstance(state_directory, Path)
        or type(endpoint_value) is not str
        or type(device_label) is not str
        or type(raw_scopes) is not list
        or not all(type(value) is str for value in raw_scopes)
    ):
        raise ConnectorError(ConnectorErrorCode.CONNECTOR_STATE_INVALID)
    endpoint = WssEndpoint.parse(endpoint_value)
    scopes = _parse_grant_scopes(raw_scopes)
    key_store = VpsKeyStore(state_directory)
    vps_identity = key_store.load().public_identity
    now = int(time.time())
    orchestrator = VpsPairingOrchestrator(
        key_store,
        VpsProfileStore(state_directory),
    )

    def display(challenge: PairingDisplay) -> None:
        rendered_scopes = ", ".join(
            format_grant_scope(source, operation, data_scope, capability_id)
            for source, operation, data_scope, capability_id in challenge.scopes
        )
        reader._write(
            "Compare this pairing on the trusted Connector terminal.\n"
            f"VPS fingerprint: {vps_identity.fingerprint}\n"
            f"Connector fingerprint: {challenge.connector_fingerprint}\n"
            f"SAS: {challenge.sas}\n"
            f"scopes: {rendered_scopes}\n"
            f"expires_at: {challenge.grant_expires_at}\n"
            f"max_uses: {challenge.grant_max_uses}\n"
        )

    profile = await orchestrator.pair(
        endpoint,
        device_label=device_label,
        requested_scopes=scopes,
        grant_expires_at=now + DEFAULT_GRANT_TTL_SECONDS,
        grant_max_uses=DEFAULT_GRANT_USES,
        display=display,
    )
    reader._write(
        "Pairing complete.\n"
        f"Connector fingerprint: {profile.connector_identity.fingerprint}\n"
    )


def _parse_grant_scopes(values: list[str]) -> tuple[GrantScope, ...]:
    scopes: list[GrantScope] = []
    for value in values:
        parts = value.split(":")
        if len(parts) not in {2, 3, 4} or any(not part for part in parts):
            raise ConnectorError(ConnectorErrorCode.GRANT_SCOPE_DENIED)
        source = get_source(parts[0])
        operation = get_operation(source, parts[1]) if source is not None else None
        if operation is None or operation.tool == "status":
            raise ConnectorError(ConnectorErrorCode.GRANT_SCOPE_DENIED)
        data_scope = operation.runtime.data_scope
        if len(parts) >= 3 and parts[2] != data_scope:
            raise ConnectorError(ConnectorErrorCode.GRANT_SCOPE_DENIED)
        try:
            scopes.append(
                GrantScope(
                    parts[0],
                    parts[1],
                    data_scope,
                    parts[3] if len(parts) == 4 else None,
                )
            )
        except ProtocolValidationError:
            raise ConnectorError(ConnectorErrorCode.GRANT_SCOPE_DENIED) from None
    ordered = tuple(sorted(scopes, key=lambda item: (item.source, item.operation)))
    if len({(item.source, item.operation) for item in ordered}) != len(ordered):
        raise ConnectorError(ConnectorErrorCode.GRANT_SCOPE_DENIED)
    return ordered


def _optional_path_group(
    args: argparse.Namespace,
    names: tuple[str, ...],
) -> tuple[Path, ...] | None:
    values = tuple(getattr(args, name, None) for name in names)
    if all(value is None for value in values):
        return None
    if any(not isinstance(value, Path) for value in values):
        raise ConnectorError(ConnectorErrorCode.CONNECTOR_STATE_INVALID)
    return cast("tuple[Path, ...]", values)


def _render_activation_summary(summaries: tuple[_ActivationSummary, ...]) -> str:
    if not summaries:
        raise ConnectorError(ConnectorErrorCode.CONNECTOR_STATE_INVALID)
    lines = ["Enable trusted-device Connector executors:"]
    scopes: list[GrantScope] = []
    for backend, digests, selected_scopes, prerequisites in summaries:
        lines.append(f"backend: {backend.backend_id}/{backend.backend_version}")
        lines.extend(f"digest: {name}:{value}" for name, value in digests)
        lines.extend(f"prerequisite: {value}" for value in prerequisites)
        scopes.extend(selected_scopes)
    lines.append(f"scopes: {len(scopes)}")
    lines.extend(
        "scope: "
        + format_grant_scope(
            scope.source,
            scope.operation,
            scope.data_scope,
            scope.capability_id,
        )
        for scope in scopes
    )
    return "\n".join(lines) + "\n"


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
                    _RUNTIME.operation_availability,
                ),
            )
        if command == "sources":
            return success_response(trace_id, sources_data(SOURCE_CATALOG))
        if command == "doctor":
            local_doctor = doctor_data(SOURCE_CATALOG, _RUNTIME.operation_availability)
            if not getattr(args, "upstream", False):
                return success_response(trace_id, local_doctor)
            try:
                upstream = upstream_doctor_data()
            except AgentReachBridgeError:
                raise ReachValidationError(
                    "capability_unavailable",
                    (
                        "The installed Agent-Reach package cannot provide a "
                        "compatible doctor report."
                    ),
                    (
                        "Reinstall the pinned Agent-Reach dependency, then retry "
                        "the operator doctor."
                    ),
                ) from None
            return success_response(
                trace_id,
                {"local": local_doctor, "agent_reach": upstream},
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
            (
                "Use hermes reach status, sources, doctor, setup, updates check, "
                "or connector."
            ),
        )
    except ReachValidationError as error:
        return error_response(error, trace_id)
    except Exception:
        return internal_error_response(trace_id)


def _add_json_flag(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true", help="Emit JSON output")


def _set_runtime(runtime: RuntimeDispatcher) -> None:
    """Install the process runtime selected once during plugin registration."""

    if not isinstance(runtime, RuntimeDispatcher):
        raise TypeError("The Reach CLI runtime is invalid.")
    global _RUNTIME
    _RUNTIME = runtime


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
