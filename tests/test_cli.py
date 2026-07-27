from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from collections.abc import Callable
from pathlib import Path

import pytest

from hermes_reach import cli
from hermes_reach.agent_reach_bridge import AgentReachBridgeError
from hermes_reach.cli import (
    command_payload,
    connector_command,
    register_cli,
    render_command,
)
from hermes_reach.connector.client import PairingDisplay
from hermes_reach.connector.execution import ConnectorExecutionComposition
from hermes_reach.connector.protocol import GrantScope
from hermes_reach.connector.transport import WssEndpoint
from hermes_reach.runtime.release import ReleaseReport


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    register_cli(parser)
    return parser


def test_status_json_is_local_catalog_data() -> None:
    args = _parser().parse_args(["status", "--json", "--source", "github"])

    response = json.loads(render_command(args))

    assert response["outcome"] == "ok"
    assert [source["source"] for source in response["data"]["sources"]] == ["github"]


def test_sources_and_doctor_are_available_without_setup() -> None:
    parser = _parser()

    sources = command_payload(parser.parse_args(["sources"]))
    doctor = command_payload(parser.parse_args(["doctor", "--json"]))

    assert len(sources["data"]["sources"]) == 15
    assert doctor["data"]["network_checked"] is False


def test_setup_fails_closed_and_updates_report_local_pins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parser = _parser()
    monkeypatch.setattr(
        cli,
        "check_release_pins",
        lambda: ReleaseReport(
            "current",
            "0.1.0a0",
            "0.19.0",
            "1.5.0",
            "v1",
            "baseline",
            "Pinned locally.",
        ),
    )

    setup = command_payload(parser.parse_args(["setup", "--yes"]))
    updates = command_payload(parser.parse_args(["updates", "check", "--json"]))

    assert setup["error"]["code"] == "capability_unavailable"
    assert updates["outcome"] == "ok"
    assert updates["data"]["status"] == "current"


def test_upstream_doctor_is_only_requested_with_the_explicit_cli_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parser = _parser()
    called = False

    def upstream() -> dict[str, object]:
        nonlocal called
        called = True
        return {"version": "1.5.0", "channels": []}

    monkeypatch.setattr(cli, "upstream_doctor_data", upstream)

    local = command_payload(parser.parse_args(["doctor", "--json"]))
    assert called is False
    upstream_result = command_payload(
        parser.parse_args(["doctor", "--upstream", "--json"])
    )

    assert called is True
    assert "agent_reach" not in local["data"]
    assert upstream_result["data"]["agent_reach"]["version"] == "1.5.0"


def test_upstream_doctor_does_not_expose_bridge_failure_details(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parser = _parser()

    def unavailable() -> dict[str, object]:
        raise AgentReachBridgeError("/private/secret-command --token=redacted")

    monkeypatch.setattr(cli, "upstream_doctor_data", unavailable)

    response = command_payload(parser.parse_args(["doctor", "--upstream", "--json"]))

    assert response["error"]["code"] == "capability_unavailable"
    assert "secret-command" not in str(response)


def test_connector_cli_exposes_role_init_pair_and_foreground_serve() -> None:
    parser = _parser()

    initialize = parser.parse_args(
        [
            "connector",
            "init",
            "--role",
            "connector",
            "--state-directory",
            "/private/connector",
        ]
    )
    vps_initialize = parser.parse_args(
        [
            "connector",
            "init",
            "--role",
            "vps",
            "--state-directory",
            "/private/vps",
        ]
    )
    serve = parser.parse_args(
        [
            "connector",
            "serve",
            "--state-directory",
            "/private/connector",
            "--bind",
            "100.64.0.9",
            "--port",
            "8443",
        ]
    )
    pair = parser.parse_args(
        [
            "connector",
            "pair",
            "--state-directory",
            "/private/vps",
            "--connector",
            "wss://100.64.0.9:8443",
            "--device-label",
            "reach-vps",
            "--scope",
            "github:search.repositories",
            "--scope",
            "youtube:read.video:public",
        ]
    )

    assert initialize.func is connector_command
    assert initialize.role == "connector"
    assert initialize.state_directory == Path("/private/connector")
    assert vps_initialize.role == "vps"
    assert serve.func is connector_command
    assert serve.bind_host == "100.64.0.9"
    assert serve.port == 8443
    assert serve.reddit_opencli is None
    assert pair.func is connector_command
    assert pair.connector_endpoint == "wss://100.64.0.9:8443"
    assert pair.scope == [
        "github:search.repositories",
        "youtube:read.video:public",
    ]
    with pytest.raises(SystemExit):
        parser.parse_args(
            ["connector", "init", "--state-directory", "/private/connector"]
        )
    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                "connector",
                "init",
                "--role",
                "connector",
                "--state-directory",
                "/private/connector",
                "--yes",
            ]
        )


def test_connector_serve_reddit_activation_requires_exact_tty_enable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    executable = tmp_path / "opencli"
    executable.write_bytes(b"fixture-opencli")
    executable.chmod(0o700)

    class FakeReader:
        def __init__(self, enabled: bool) -> None:
            self.enabled = enabled
            self.output: list[str] = []
            self.prompts: list[tuple[str, str]] = []
            self.closed = False

        def _write(self, value: str) -> None:
            self.output.append(value)

        def _confirm(self, prompt: str, expected: str) -> bool:
            self.prompts.append((prompt, expected))
            return self.enabled

        def close(self) -> None:
            self.closed = True

    class MutationSpy:
        def __init__(self) -> None:
            self.compositions: list[ConnectorExecutionComposition | None] = []

        def serve(
            self,
            state_directory: Path,
            *,
            reader: object,
            bind_host: str,
            port: int,
            execution_composition: ConnectorExecutionComposition | None = None,
        ) -> None:
            assert state_directory == tmp_path / "connector"
            assert bind_host == "127.0.0.1"
            assert port == 8443
            self.compositions.append(execution_composition)

    args = _parser().parse_args(
        [
            "connector",
            "serve",
            "--state-directory",
            str(tmp_path / "connector"),
            "--bind",
            "127.0.0.1",
            "--port",
            "8443",
            "--reddit-opencli",
            str(executable),
        ]
    )
    assert args.reddit_opencli == executable
    mutation = MutationSpy()

    denied = FakeReader(False)
    monkeypatch.setattr(cli, "TtyPassphraseReader", lambda: denied)
    connector_command(args, mutation_service=mutation)  # type: ignore[arg-type]
    assert mutation.compositions == []
    assert denied.prompts == [("Type enable to continue: ", "enable")]
    assert denied.closed

    enabled = FakeReader(True)
    monkeypatch.setattr(cli, "TtyPassphraseReader", lambda: enabled)
    connector_command(args, mutation_service=mutation)  # type: ignore[arg-type]

    rendered = "".join(enabled.output)
    assert "scope: reddit:read.post:public" in rendered
    assert f"OpenCLI path: {executable.resolve()}" in rendered
    assert hashlib.sha256(b"fixture-opencli").hexdigest() in rendered
    assert enabled.prompts == [("Type enable to continue: ", "enable")]
    assert enabled.closed
    assert len(mutation.compositions) == 1
    assert repr(mutation.compositions[0]) == "ConnectorExecutionComposition(count=1)"


def test_connector_init_dispatches_through_tty_only_service_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeReader:
        def __init__(self) -> None:
            self.output: list[str] = []
            self.closed = False

        def _write(self, value: str) -> None:
            self.output.append(value)

        def close(self) -> None:
            self.closed = True

    reader = FakeReader()
    called: dict[str, object] = {}

    def initialize(state_directory: Path, *, tty_reader: object) -> None:
        called["state_directory"] = state_directory
        called["tty_reader"] = tty_reader

    monkeypatch.setattr(cli, "TtyPassphraseReader", lambda: reader)
    monkeypatch.setattr(
        cli.ConnectorService,
        "initialize_state_directory",
        staticmethod(initialize),
    )
    args = _parser().parse_args(
        [
            "connector",
            "init",
            "--role",
            "connector",
            "--state-directory",
            "/private/connector",
        ]
    )

    connector_command(args)

    assert called == {
        "state_directory": Path("/private/connector"),
        "tty_reader": reader,
    }
    assert reader.output == ["Connector state initialized.\n"]
    assert reader.closed


def test_connector_vps_init_uses_the_unattended_owner_only_key_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeReader:
        def __init__(self) -> None:
            self.output: list[str] = []
            self.closed = False

        def _write(self, value: str) -> None:
            self.output.append(value)

        def close(self) -> None:
            self.closed = True

    class FakePublicIdentity:
        fingerprint = "sha256:" + "0123-" * 15 + "0123"

    reader = FakeReader()
    state_directories: list[Path] = []

    class FakeKeyStore:
        def __init__(self, state_directory: Path) -> None:
            state_directories.append(state_directory)

        def initialize(self) -> FakePublicIdentity:
            return FakePublicIdentity()

    monkeypatch.setattr(cli, "TtyPassphraseReader", lambda: reader)
    monkeypatch.setattr(cli, "VpsKeyStore", FakeKeyStore)
    args = _parser().parse_args(
        [
            "connector",
            "init",
            "--role",
            "vps",
            "--state-directory",
            "/private/vps",
        ]
    )

    connector_command(args)

    assert state_directories == [Path("/private/vps")]
    assert reader.output[0].startswith(
        "VPS identity initialized.\nfingerprint: sha256:"
    )
    assert reader.closed


def test_connector_pair_dispatches_after_tty_capture_without_echoing_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeReader:
        def __init__(self) -> None:
            self.output: list[str] = []
            self.closed = False

        def _write(self, value: str) -> None:
            self.output.append(value)

        def close(self) -> None:
            self.closed = True

    reader = FakeReader()
    called: dict[str, object] = {}

    async def pair(args: argparse.Namespace, tty_reader: object) -> None:
        called["command"] = args.reach_connector_command
        called["reader"] = tty_reader
        raise cli.ConnectorError(cli.ConnectorErrorCode.CONNECTOR_OFFLINE)

    monkeypatch.setattr(cli, "TtyPassphraseReader", lambda: reader)
    monkeypatch.setattr(cli, "_pair_vps", pair)
    endpoint = "wss://100.64.0.9:8443"
    args = _parser().parse_args(
        [
            "connector",
            "pair",
            "--state-directory",
            "/private/vps",
            "--connector",
            endpoint,
            "--device-label",
            "reach-vps",
            "--scope",
            "github:search.repositories",
        ]
    )

    connector_command(args)

    assert called == {"command": "pair", "reader": reader}
    assert reader.output == ["connector_offline: The Connector is offline.\n"]
    assert endpoint not in "".join(reader.output)
    assert "/private/vps" not in "".join(reader.output)
    assert reader.closed


def test_vps_pairing_display_uses_the_exact_persisted_grant_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeReader:
        def __init__(self) -> None:
            self.output: list[str] = []

        def _write(self, value: str) -> None:
            self.output.append(value)

    class FakePublicIdentity:
        key_id = "a" * 32
        fingerprint = "sha256:" + "0123-" * 15 + "0123"

    class FakePrivateIdentity:
        public_identity = FakePublicIdentity()

    class FakeKeyStore:
        def __init__(self, state_directory: Path) -> None:
            assert state_directory == Path("/private/vps")

        def load(self) -> FakePrivateIdentity:
            return FakePrivateIdentity()

    class FakeProfileStore:
        def __init__(self, state_directory: Path) -> None:
            assert state_directory == Path("/private/vps")

    class FakePairedProfile:
        connector_identity = FakePublicIdentity()

    class FakeOrchestrator:
        def __init__(self, key_store: object, profile_store: object) -> None:
            assert isinstance(key_store, FakeKeyStore)
            assert isinstance(profile_store, FakeProfileStore)

        async def pair(
            self,
            endpoint: WssEndpoint,
            *,
            device_label: str,
            requested_scopes: tuple[GrantScope, ...],
            grant_expires_at: int,
            grant_max_uses: int,
            display: Callable[[PairingDisplay], None],
        ) -> FakePairedProfile:
            assert endpoint.uri == "wss://100.64.0.9:8443"
            assert device_label == "reach-vps"
            assert len(requested_scopes) == 1
            assert grant_expires_at > 1_900_000_000
            assert grant_max_uses == 200
            display(
                PairingDisplay(
                    pairing_id="a" * 26,
                    connector_key_id="b" * 32,
                    connector_fingerprint="sha256:" + "abcd-" * 15 + "abcd",
                    sas="0123456789",
                    deadline=1_900_000_300,
                    scopes=(("github", "search.repositories", "public"),),
                    grant_expires_at=1_900_028_800,
                    grant_max_uses=199,
                )
            )
            return FakePairedProfile()

    monkeypatch.setattr(cli, "VpsKeyStore", FakeKeyStore)
    monkeypatch.setattr(cli, "VpsProfileStore", FakeProfileStore)
    monkeypatch.setattr(cli, "VpsPairingOrchestrator", FakeOrchestrator)
    monkeypatch.setattr(cli.time, "time", lambda: 1_900_000_001)
    reader = FakeReader()
    args = _parser().parse_args(
        [
            "connector",
            "pair",
            "--state-directory",
            "/private/vps",
            "--connector",
            "wss://100.64.0.9:8443",
            "--device-label",
            "reach-vps",
            "--scope",
            "github:search.repositories",
        ]
    )

    asyncio.run(cli._pair_vps(args, reader))  # type: ignore[arg-type]

    output = "".join(reader.output)
    assert "SAS: 0123456789" in output
    assert "scopes: github:search.repositories:public" in output
    assert "expires_at: 1900028800" in output
    assert "max_uses: 199" in output
    assert "wss://" not in output
    assert "/private/vps" not in output
