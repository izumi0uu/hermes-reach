from __future__ import annotations

import argparse
import json
import socket
import time
from pathlib import Path

import pytest

from hermes_reach import cli, plugin
from hermes_reach.cli import connector_command, register_cli
from hermes_reach.connector.cli import (
    ConnectorStatusInspection,
    execute_connector_inspection,
    render_connector_devices,
    render_connector_error,
    render_connector_grants,
    render_connector_status,
)
from hermes_reach.connector.errors import ConnectorError, ConnectorErrorCode
from hermes_reach.connector.identity import DevicePrivateIdentity
from hermes_reach.connector.store import (
    AuthorityStore,
    DeviceInspection,
    GrantInspection,
    GrantScopeInspection,
    StoreWriterLease,
)
from hermes_reach.connector.tls import ConnectorTLSStore

STATE_DIRECTORY = Path("/operator/connector-state")
KEY_ID = "k" * 32
FINGERPRINT = "sha256:" + "0123-" * 15 + "0123"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    register_cli(parser)
    return parser


def _connector_parser(parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
    reach_commands = next(
        action
        for action in parser._actions
        if isinstance(action, argparse._SubParsersAction)
    )
    connector = reach_commands.choices["connector"]
    assert isinstance(connector, argparse.ArgumentParser)
    return connector


def _connector_commands(parser: argparse.ArgumentParser) -> set[str]:
    connector = _connector_parser(parser)
    commands = next(
        action
        for action in connector._actions
        if isinstance(action, argparse._SubParsersAction)
    )
    return set(commands.choices)


def _option_strings(parser: argparse.ArgumentParser) -> set[str]:
    result: set[str] = set()
    pending = [parser]
    while pending:
        current = pending.pop()
        for action in current._actions:
            result.update(action.option_strings)
            if isinstance(action, argparse._SubParsersAction):
                pending.extend(action.choices.values())
    return result


def _forbidden_side_effect(*_args: object, **_kwargs: object) -> None:
    raise AssertionError("parser or renderer attempted an external side effect")


def _status() -> ConnectorStatusInspection:
    return ConnectorStatusInspection(
        connector_key_id=KEY_ID,
        connector_fingerprint=FINGERPRINT,
        service_state="stopped",
        lock_state="locked",
        schema_version=1,
    )


def _devices() -> tuple[DeviceInspection, ...]:
    return (
        DeviceInspection(
            device_id="d" * 26,
            label="trusted-vps",
            key_id="v" * 32,
            fingerprint=FINGERPRINT,
            paired_at=1_900_000_000,
            revoked_at=None,
        ),
    )


def _grants() -> tuple[GrantInspection, ...]:
    return (
        GrantInspection(
            grant_id="g" * 26,
            revision=2,
            device_id="d" * 26,
            subject_key_id="v" * 32,
            policy_revision=3,
            issued_at=1_900_000_000,
            not_before=1_900_000_000,
            expires_at=1_900_028_800,
            max_uses=200,
            used_count=7,
            revoked_at=None,
            superseded_at=None,
            scopes=(GrantScopeInspection("github", "search.repositories", "public"),),
        ),
    )


def test_parser_exposes_only_the_bounded_connector_namespace() -> None:
    parser = _parser()

    assert _connector_commands(parser) == {
        "init",
        "serve",
        "pair",
        "status",
        "devices",
        "grants",
    }
    for command in ("status", "devices", "grants"):
        args = parser.parse_args(
            ["connector", command, "--state-directory", str(STATE_DIRECTORY), "--json"]
        )
        assert args.func is connector_command
        assert args.state_directory == STATE_DIRECTORY
        assert args.json is True


def test_connector_namespace_has_no_non_tty_approval_or_secret_flags() -> None:
    options = _option_strings(_connector_parser(_parser()))

    assert {
        "--yes",
        "--passphrase",
        "--approval",
        "--approve",
        "--deny",
        "--provider",
        "--project",
        "--selector",
        "--env",
        "--secret",
        "--token",
    }.isdisjoint(options)


def test_parser_construction_and_parsing_are_side_effect_free(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(Path, "mkdir", _forbidden_side_effect)
    monkeypatch.setattr(Path, "open", _forbidden_side_effect)
    monkeypatch.setattr(Path, "write_text", _forbidden_side_effect)
    monkeypatch.setattr(socket, "create_connection", _forbidden_side_effect)

    args = _parser().parse_args(
        ["connector", "devices", "--state-directory", str(STATE_DIRECTORY)]
    )

    assert args.reach_connector_command == "devices"


def test_closed_dto_rendering_is_side_effect_free_and_exact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(Path, "mkdir", _forbidden_side_effect)
    monkeypatch.setattr(Path, "open", _forbidden_side_effect)
    monkeypatch.setattr(Path, "write_text", _forbidden_side_effect)
    monkeypatch.setattr(socket, "create_connection", _forbidden_side_effect)

    status_json = json.loads(render_connector_status(_status(), json_output=True))
    devices_json = json.loads(render_connector_devices(_devices(), json_output=True))
    grants_json = json.loads(render_connector_grants(_grants(), json_output=True))

    assert status_json == {
        "status": {
            "connector_fingerprint": FINGERPRINT,
            "connector_key_id": KEY_ID,
            "lock_state": "locked",
            "protocol_version": "reach-connector/v1",
            "schema_version": 1,
            "service_state": "stopped",
        }
    }
    assert devices_json["devices"][0]["label"] == "trusted-vps"
    assert grants_json["grants"][0]["scopes"] == [
        {
            "data_scope": "public",
            "operation": "search.repositories",
            "source": "github",
        }
    ]
    assert "Connector service: stopped" in render_connector_status(
        _status(), json_output=False
    )
    assert "trusted-vps" in render_connector_devices(_devices(), json_output=False)
    assert "uses=7/200" in render_connector_grants(_grants(), json_output=False)


def test_status_dto_rejects_unverified_identity_version_and_state_fields() -> None:
    values: dict[str, object] = {
        "connector_key_id": KEY_ID,
        "connector_fingerprint": FINGERPRINT,
        "service_state": "stopped",
        "lock_state": "locked",
        "schema_version": 1,
        "protocol_version": "reach-connector/v1",
    }
    invalid_fields = {
        "connector_key_id": "TOKEN_CANARY",
        "connector_fingerprint": "/private/PATH_CANARY",
        "service_state": "unlocked",
        "lock_state": "unlocked",
        "schema_version": 2,
        "protocol_version": "reach-connector/v2",
    }

    for field, invalid in invalid_fields.items():
        candidate = {**values, field: invalid}
        with pytest.raises(ValueError, match="status inspection is invalid"):
            ConnectorStatusInspection(**candidate)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "label",
    ["unsafe\nlabel", "terminal\x1b[31m", "cafe\N{LATIN SMALL LETTER E WITH ACUTE}"],
)
def test_device_inspection_rejects_labels_unsafe_for_rendering(label: str) -> None:
    with pytest.raises(ValueError, match="device inspection is invalid"):
        DeviceInspection(
            device_id="d" * 26,
            label=label,
            key_id="v" * 32,
            fingerprint=FINGERPRINT,
            paired_at=1_900_000_000,
            revoked_at=None,
        )


def test_offline_inspector_reads_verified_state_and_reports_lease_contention(
    tmp_path: Path,
) -> None:
    from hermes_reach.connector.cli import OfflineConnectorInspector

    state_directory = tmp_path / "connector"
    state_directory.mkdir(mode=0o700)
    now = int(time.time())
    signer = DevicePrivateIdentity._from_seed_for_testing(bytes(range(32)))
    ConnectorTLSStore(state_directory, _platform="linux").initialize(signer, now=now)
    lease = StoreWriterLease(state_directory, _platform="linux")
    AuthorityStore.initialize(
        state_directory,
        signer.public_identity,
        lease,
        initial_policy_digest="0" * 64,
        now=now,
    )
    lease.close()
    inspector = OfflineConnectorInspector()

    status = inspector.inspect_status(state_directory)

    assert status.connector_key_id == signer.public_identity.key_id
    assert status.connector_fingerprint == signer.public_identity.fingerprint
    assert status.service_state == "stopped"
    assert status.lock_state == "locked"
    assert status.schema_version == 1
    assert inspector.inspect_devices(state_directory) == ()
    assert inspector.inspect_grants(state_directory) == ()

    running_lease = StoreWriterLease(state_directory, _platform="linux")
    try:
        running = inspector.inspect_status(state_directory)
        assert running.service_state == "running"
        assert running.lock_state == "unknown"
        assert running.schema_version is None
        with pytest.raises(ConnectorError) as devices_failure:
            inspector.inspect_devices(state_directory)
        assert (
            devices_failure.value.code
            == ConnectorErrorCode.CONNECTOR_SERVICE_RUNNING.value
        )
    finally:
        running_lease.close()


def test_renderers_project_no_service_or_error_context_canaries() -> None:
    canaries = (
        "/private/PATH_CANARY",
        "QUERY_CANARY",
        "TOKEN_CANARY",
        "PROJECT_CANARY",
        "SELECTOR_CANARY",
        "ENV_CANARY",
        "SECRET_CANARY",
    )

    class InspectorWithUnsafePrivateState:
        unsafe_path = canaries[0]
        unsafe_query = canaries[1]
        unsafe_token = canaries[2]
        unsafe_project = canaries[3]
        unsafe_selector = canaries[4]
        unsafe_env = canaries[5]
        unsafe_secret = canaries[6]

        def inspect_status(self, state_directory: Path) -> ConnectorStatusInspection:
            assert state_directory == STATE_DIRECTORY
            return _status()

        def inspect_devices(
            self, state_directory: Path
        ) -> tuple[DeviceInspection, ...]:
            assert state_directory == STATE_DIRECTORY
            return _devices()

        def inspect_grants(self, state_directory: Path) -> tuple[GrantInspection, ...]:
            assert state_directory == STATE_DIRECTORY
            return _grants()

    inspector = InspectorWithUnsafePrivateState()
    parser = _parser()
    outputs: list[str] = []
    for command in ("status", "devices", "grants"):
        for json_flag in ([], ["--json"]):
            args = parser.parse_args(
                [
                    "connector",
                    command,
                    "--state-directory",
                    str(STATE_DIRECTORY),
                    *json_flag,
                ]
            )
            outputs.append(execute_connector_inspection(args, inspector))
    unsafe_error = ConnectorError(
        ConnectorErrorCode.CONNECTOR_STATE_INVALID,
        unsafe_context=" ".join(canaries),
    )
    outputs.extend(
        (
            render_connector_error(unsafe_error, json_output=False),
            render_connector_error(unsafe_error, json_output=True),
        )
    )

    rendered = "\n".join(outputs)
    for canary in canaries:
        assert canary not in rendered


def test_services_are_called_only_during_command_execution(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class FakeReader:
        def __init__(self) -> None:
            self.output: list[str] = []
            self.closed = False

        def _write(self, value: str) -> None:
            self.output.append(value)

        def close(self) -> None:
            self.closed = True

    class MutationSpy:
        def __init__(self) -> None:
            self.calls: list[tuple[str, Path]] = []

        def initialize_connector(self, state_directory: Path, reader: object) -> None:
            assert reader is fake_reader
            self.calls.append(("init", state_directory))

    class InspectionSpy:
        def __init__(self) -> None:
            self.calls: list[tuple[str, Path]] = []

        def inspect_status(self, state_directory: Path) -> ConnectorStatusInspection:
            self.calls.append(("status", state_directory))
            return _status()

    fake_reader = FakeReader()
    mutations = MutationSpy()
    inspections = InspectionSpy()
    monkeypatch.setattr(cli, "TtyPassphraseReader", lambda: fake_reader)
    parser = _parser()
    init_args = parser.parse_args(
        [
            "connector",
            "init",
            "--role",
            "connector",
            "--state-directory",
            str(STATE_DIRECTORY),
        ]
    )
    status_args = parser.parse_args(
        ["connector", "status", "--state-directory", str(STATE_DIRECTORY)]
    )

    assert mutations.calls == []
    assert inspections.calls == []
    render_connector_status(_status(), json_output=False)
    assert inspections.calls == []

    connector_command(init_args, mutation_service=mutations)  # type: ignore[arg-type]
    assert mutations.calls == [("init", STATE_DIRECTORY)]
    assert fake_reader.closed
    monkeypatch.setattr(cli, "TtyPassphraseReader", _forbidden_side_effect)
    connector_command(status_args, inspection_service=inspections)  # type: ignore[arg-type]
    assert inspections.calls == [("status", STATE_DIRECTORY)]
    assert "Connector service: stopped" in capsys.readouterr().out


@pytest.mark.parametrize("json_output", [False, True])
def test_inspection_failures_write_stderr_and_exit_nonzero(
    capsys: pytest.CaptureFixture[str], json_output: bool
) -> None:
    error = ConnectorError(ConnectorErrorCode.CONNECTOR_SERVICE_RUNNING)

    class FailingInspector:
        def inspect_status(self, state_directory: Path) -> ConnectorStatusInspection:
            assert state_directory == STATE_DIRECTORY
            raise error

    arguments = ["connector", "status", "--state-directory", str(STATE_DIRECTORY)]
    if json_output:
        arguments.append("--json")
    args = _parser().parse_args(arguments)

    with pytest.raises(SystemExit) as exited:
        connector_command(args, inspection_service=FailingInspector())  # type: ignore[arg-type]

    captured = capsys.readouterr()
    assert exited.value.code == 1
    assert captured.out == ""
    assert captured.err == render_connector_error(error, json_output=json_output) + "\n"


def test_public_agent_tool_registration_remains_exactly_five(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Context:
        def __init__(self) -> None:
            self.tools: list[str] = []

        def register_tool(self, **kwargs: object) -> None:
            name = kwargs["name"]
            assert isinstance(name, str)
            self.tools.append(name)

        def register_cli_command(self, **_kwargs: object) -> None:
            pass

        def register_skill(self, **_kwargs: object) -> None:
            pass

    monkeypatch.setattr(plugin, "load_agent_reach_catalog", lambda: None)
    context = Context()

    plugin.register(context)

    assert context.tools == [
        "reach_search",
        "reach_read",
        "reach_browse",
        "reach_transcribe",
        "reach_status",
    ]


@pytest.mark.parametrize(
    "unsupported",
    [
        "lock",
        "unlock",
        "approve",
        "deny",
        "grant",
        "revoke",
        "rotate",
        "receipts",
        "provider",
        "file",
    ],
)
def test_commands_without_an_owner_control_channel_fail_closed(
    unsupported: str,
) -> None:
    with pytest.raises(SystemExit) as failure:
        _parser().parse_args(["connector", unsupported])

    assert failure.value.code == 2
