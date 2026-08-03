"""Side-effect-free Connector CLI parsing and public-safe inspection views."""

from __future__ import annotations

import argparse
import json
import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from .errors import ConnectorError, ConnectorErrorCode
from .identity import DevicePublicIdentity
from .limits import CONNECTOR_PROTOCOL_VERSION, CONNECTOR_STORAGE_SCHEMA_VERSION
from .protocol import format_grant_scope
from .store import (
    AuthorityStore,
    DeviceInspection,
    GrantInspection,
    StoreWriterLease,
)
from .tls import ConnectorTLSStore

ServiceState = Literal["running", "stopped"]
LockState = Literal["locked", "unknown"]
_KEY_ID = re.compile(r"[a-z2-7]{32}")
_FINGERPRINT = re.compile(r"sha256:(?:[0-9a-f]{4}-){15}[0-9a-f]{4}")


@dataclass(frozen=True, slots=True)
class ConnectorStatusInspection:
    """Closed status metadata that is safe for operator rendering."""

    connector_key_id: str
    connector_fingerprint: str
    service_state: ServiceState
    lock_state: LockState
    schema_version: int | None
    protocol_version: str = CONNECTOR_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        stopped = (
            self.service_state == "stopped"
            and self.lock_state == "locked"
            and type(self.schema_version) is int
            and self.schema_version == CONNECTOR_STORAGE_SCHEMA_VERSION
        )
        running = (
            self.service_state == "running"
            and self.lock_state == "unknown"
            and self.schema_version is None
        )
        if (
            type(self.connector_key_id) is not str
            or _KEY_ID.fullmatch(self.connector_key_id) is None
            or type(self.connector_fingerprint) is not str
            or _FINGERPRINT.fullmatch(self.connector_fingerprint) is None
            or type(self.service_state) is not str
            or type(self.lock_state) is not str
            or type(self.protocol_version) is not str
            or self.protocol_version != CONNECTOR_PROTOCOL_VERSION
            or not (stopped or running)
        ):
            raise ValueError("The Connector status inspection is invalid.")


class ConnectorInspector(Protocol):
    """Injected owner of all filesystem-backed inspection work."""

    def inspect_status(self, state_directory: Path) -> ConnectorStatusInspection: ...

    def inspect_devices(
        self, state_directory: Path
    ) -> tuple[DeviceInspection, ...]: ...

    def inspect_grants(self, state_directory: Path) -> tuple[GrantInspection, ...]: ...


class OfflineConnectorInspector:
    """Inspect verified local state without opening a socket or unlocking a key."""

    def inspect_status(self, state_directory: Path) -> ConnectorStatusInspection:
        identity = ConnectorTLSStore(state_directory).load_public_identity()
        try:
            with self._open_store(state_directory, identity):
                return ConnectorStatusInspection(
                    connector_key_id=identity.key_id,
                    connector_fingerprint=identity.fingerprint,
                    service_state="stopped",
                    lock_state="locked",
                    schema_version=CONNECTOR_STORAGE_SCHEMA_VERSION,
                )
        except ConnectorError as error:
            if error.code != ConnectorErrorCode.CONNECTOR_SERVICE_RUNNING.value:
                raise
            return ConnectorStatusInspection(
                connector_key_id=identity.key_id,
                connector_fingerprint=identity.fingerprint,
                service_state="running",
                lock_state="unknown",
                schema_version=None,
            )

    def inspect_devices(self, state_directory: Path) -> tuple[DeviceInspection, ...]:
        with self._verified_store(state_directory) as store:
            return store.inspect_devices()

    def inspect_grants(self, state_directory: Path) -> tuple[GrantInspection, ...]:
        with self._verified_store(state_directory) as store:
            return store.inspect_grants()

    @contextmanager
    def _verified_store(self, state_directory: Path) -> Iterator[AuthorityStore]:
        identity = ConnectorTLSStore(state_directory).load_public_identity()
        with self._open_store(state_directory, identity) as store:
            yield store

    @contextmanager
    def _open_store(
        self, state_directory: Path, identity: DevicePublicIdentity
    ) -> Iterator[AuthorityStore]:
        lease = StoreWriterLease(state_directory, create=False)
        try:
            store = AuthorityStore.open(state_directory, identity, lease)
            yield store
        finally:
            lease.close()


def register_connector_cli(
    commands: argparse._SubParsersAction[argparse.ArgumentParser],
    handler: Callable[[argparse.Namespace], None],
) -> None:
    """Register the bounded Connector operator namespace without performing I/O."""

    connector_init = commands.add_parser(
        "init", help="Initialize one Connector or VPS device identity"
    )
    connector_init.add_argument("--role", choices=("connector", "vps"), required=True)
    _add_state_directory(connector_init)
    connector_init.set_defaults(func=handler)

    connector_serve = commands.add_parser(
        "serve", help="Run a locked foreground Connector"
    )
    _add_state_directory(connector_serve)
    connector_serve.add_argument("--bind", dest="bind_host", required=True)
    connector_serve.add_argument("--port", type=int, required=True)
    connector_serve.add_argument(
        "--opencli-social-node",
        type=Path,
        help="Absolute Node executable for the attested social runtime",
    )
    connector_serve.add_argument(
        "--opencli-social-root",
        type=Path,
        help="Absolute dedicated OpenCLI npm prefix",
    )
    connector_serve.add_argument(
        "--opencli-social-cli",
        type=Path,
        help="Absolute fixed OpenCLI entrypoint inside the npm prefix",
    )
    connector_serve.add_argument(
        "--opencli-social-session-home",
        type=Path,
        help="Absolute trusted-device home containing the live OpenCLI session",
    )
    connector_serve.add_argument(
        "--xueqiu-binding-manifest",
        type=Path,
        help="Absolute owner-only Xueqiu capability binding manifest",
    )
    connector_serve.set_defaults(func=handler)

    connector_pair = commands.add_parser(
        "pair", help="Pair this VPS with a trusted foreground Connector"
    )
    _add_state_directory(connector_pair)
    connector_pair.add_argument("--connector", dest="connector_endpoint", required=True)
    connector_pair.add_argument("--device-label", required=True)
    connector_pair.add_argument("--scope", action="append", required=True)
    connector_pair.set_defaults(func=handler)

    for name, help_text in (
        ("status", "Inspect local Connector lifecycle and compatibility state"),
        ("devices", "List paired Connector devices"),
        ("grants", "List immutable Connector grant revisions"),
    ):
        parser = commands.add_parser(name, help=help_text)
        _add_state_directory(parser)
        parser.add_argument("--json", action="store_true", help="Emit JSON output")
        parser.set_defaults(func=handler)


def execute_connector_inspection(
    args: argparse.Namespace, inspector: ConnectorInspector
) -> str:
    """Call one injected inspector, then render only its closed DTO."""

    command = getattr(args, "reach_connector_command", None)
    state_directory = getattr(args, "state_directory", None)
    if command not in {"status", "devices", "grants"} or not isinstance(
        state_directory, Path
    ):
        raise ConnectorError(ConnectorErrorCode.CONNECTOR_STATE_INVALID)
    json_output = getattr(args, "json", False) is True
    if command == "status":
        return render_connector_status(
            inspector.inspect_status(state_directory), json_output=json_output
        )
    if command == "devices":
        return render_connector_devices(
            inspector.inspect_devices(state_directory), json_output=json_output
        )
    return render_connector_grants(
        inspector.inspect_grants(state_directory), json_output=json_output
    )


def render_connector_status(
    status: ConnectorStatusInspection, *, json_output: bool
) -> str:
    """Render status without consulting runtime or persistent state."""

    if not isinstance(status, ConnectorStatusInspection):
        raise TypeError("Connector status rendering requires a closed inspection DTO.")
    data: dict[str, object] = {
        "connector_fingerprint": status.connector_fingerprint,
        "connector_key_id": status.connector_key_id,
        "lock_state": status.lock_state,
        "protocol_version": status.protocol_version,
        "schema_version": status.schema_version,
        "service_state": status.service_state,
    }
    if json_output:
        return _json({"status": data})
    schema = status.schema_version if status.schema_version is not None else "unknown"
    return "\n".join(
        (
            f"Connector service: {status.service_state}",
            f"Lock state: {status.lock_state}",
            f"Fingerprint: {status.connector_fingerprint}",
            f"Protocol: {status.protocol_version}",
            f"Schema: {schema}",
        )
    )


def render_connector_devices(
    devices: tuple[DeviceInspection, ...], *, json_output: bool
) -> str:
    """Render exact public-safe paired-device fields."""

    if type(devices) is not tuple or not all(
        isinstance(device, DeviceInspection) for device in devices
    ):
        raise TypeError("Connector device rendering requires closed inspection DTOs.")
    rows = [
        {
            "device_id": device.device_id,
            "fingerprint": device.fingerprint,
            "key_id": device.key_id,
            "label": device.label,
            "paired_at": device.paired_at,
            "revoked_at": device.revoked_at,
        }
        for device in devices
    ]
    if json_output:
        return _json({"devices": rows})
    if not devices:
        return "No paired Connector devices."
    return "\n".join(
        f"{device.device_id} {device.label} {device.fingerprint} "
        f"paired={device.paired_at} "
        f"revoked={device.revoked_at if device.revoked_at is not None else '-'}"
        for device in devices
    )


def render_connector_grants(
    grants: tuple[GrantInspection, ...], *, json_output: bool
) -> str:
    """Render exact public-safe grant and scope fields."""

    if type(grants) is not tuple or not all(
        isinstance(grant, GrantInspection) for grant in grants
    ):
        raise TypeError("Connector grant rendering requires closed inspection DTOs.")
    rows = [
        {
            "device_id": grant.device_id,
            "expires_at": grant.expires_at,
            "grant_id": grant.grant_id,
            "issued_at": grant.issued_at,
            "max_uses": grant.max_uses,
            "not_before": grant.not_before,
            "policy_revision": grant.policy_revision,
            "revision": grant.revision,
            "revoked_at": grant.revoked_at,
            "scopes": [
                {
                    "data_scope": scope.data_scope,
                    "operation": scope.operation,
                    "source": scope.source,
                }
                for scope in grant.scopes
            ],
            "subject_key_id": grant.subject_key_id,
            "superseded_at": grant.superseded_at,
            "used_count": grant.used_count,
        }
        for grant in grants
    ]
    if json_output:
        return _json({"grants": rows})
    if not grants:
        return "No Connector grant revisions."
    lines: list[str] = []
    for grant in grants:
        scopes = ",".join(
            format_grant_scope(
                scope.source,
                scope.operation,
                scope.data_scope,
                None,
            )
            for scope in grant.scopes
        )
        lines.append(
            f"{grant.grant_id}@{grant.revision} device={grant.device_id} "
            f"uses={grant.used_count}/{grant.max_uses} expires={grant.expires_at} "
            f"revoked={grant.revoked_at if grant.revoked_at is not None else '-'} "
            "superseded="
            f"{grant.superseded_at if grant.superseded_at is not None else '-'} "
            f"scopes={scopes}"
        )
    return "\n".join(lines)


def render_connector_error(error: ConnectorError, *, json_output: bool) -> str:
    """Render only the closed Connector error definition."""

    if not isinstance(error, ConnectorError):
        raise TypeError("Connector error rendering requires a closed error.")
    if json_output:
        return _json({"error": error.as_data(), "outcome": "error"})
    return f"{error.code}: {error.message}\nremediation: {error.remediation}"


def _add_state_directory(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--state-directory", type=Path, required=True)


def _json(value: dict[str, object]) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
