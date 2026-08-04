from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

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
from hermes_reach.connector.secrets import CapabilityId
from hermes_reach.connector.transport import WssEndpoint
from hermes_reach.runtime.release import ReleaseReport
from hermes_reach.sources.opencli_social import OpenCliSessionAttestation

_SOCIAL_FLAG_PATHS = (
    ("--opencli-social-node", Path("/private/node-canary")),
    ("--opencli-social-root", Path("/private/opencli-canary")),
    (
        "--opencli-social-cli",
        Path(
            "/private/opencli-canary/node_modules/@jackwener/opencli/dist/src/main.js"
        ),
    ),
    ("--opencli-social-session-home", Path("/private/session-canary")),
)
_XUEQIU_MANIFEST = Path("/private/xueqiu-binding-canary.json")
_SOCIAL_SCOPE_LABELS = (
    "reddit:search.posts:public",
    "reddit:read.post:public",
    "reddit:browse.subreddit:public",
    "reddit:browse.hot:public",
    "reddit:browse.popular:public",
    "reddit:browse.all:public",
    "reddit:read.subreddit:public",
    "facebook:search:public",
    "facebook:read.profile:public",
    "facebook:browse.feed:account_visible",
    "facebook:browse.groups:account_visible",
    "instagram:search.users:public",
    "instagram:read.profile:public",
    "instagram:browse.user_posts:public",
    "instagram:browse.explore:account_visible",
    "twitter:search.posts:public",
    "xiaohongshu:search.notes:public",
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    register_cli(parser)
    return parser


def test_grant_scope_parser_preserves_an_opaque_secret_capability() -> None:
    capability_id = CapabilityId.new(lambda size: b"\x01" * size).for_grant()

    assert cli._parse_grant_scopes(
        [f"xueqiu:search.stocks:public:{capability_id}"]
    ) == (GrantScope("xueqiu", "search.stocks", "public", capability_id),)

    for invalid_scope in (
        "xueqiu:search.stocks:account_visible:invalid",
        "xueqiu:search.stocks:public:NOT-Canonical!",
    ):
        with pytest.raises(cli.ConnectorError):
            cli._parse_grant_scopes([invalid_scope])


def _social_serve_arguments(
    mask: int = 0b1111,
    *,
    xueqiu: bool = False,
) -> list[str]:
    arguments = [
        "connector",
        "serve",
        "--state-directory",
        "/private/connector",
        "--bind",
        "127.0.0.1",
        "--port",
        "8443",
    ]
    for index, (flag, path) in enumerate(_SOCIAL_FLAG_PATHS):
        if mask & (1 << index):
            arguments.extend((flag, str(path)))
    if xueqiu:
        arguments.extend(("--xueqiu-binding-manifest", str(_XUEQIU_MANIFEST)))
    return arguments


class _ReaderSpy:
    def __init__(self, enabled: bool, events: list[str] | None = None) -> None:
        self.enabled = enabled
        self.events = events
        self.output: list[str] = []
        self.prompts: list[tuple[str, str]] = []
        self.closed = False

    def _write(self, value: str) -> None:
        self.output.append(value)

    def _confirm(self, prompt: str, expected: str) -> bool:
        self.prompts.append((prompt, expected))
        if self.events is not None:
            self.events.append("confirm")
        return self.enabled

    def close(self) -> None:
        self.closed = True


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
            "0.1.0a2",
            "0.19.0",
            "1.5.0",
            "6.0.12",
            "0.6.2",
            "2026.7.4",
            "0.8.0",
            "2.8.3",
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
    assert updates["data"]["feedparser_version"] == "6.0.12"
    assert updates["data"]["bilibili_cli_version"] == "0.6.2"
    assert updates["data"]["yt_dlp_version"] == "2026.7.4"
    assert updates["data"]["yt_dlp_ejs_version"] == "0.8.0"
    assert updates["data"]["deno_version"] == "2.8.3"


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
    assert serve.opencli_social_node is None
    assert serve.opencli_social_root is None
    assert serve.opencli_social_cli is None
    assert serve.opencli_social_session_home is None
    assert serve.xueqiu_binding_manifest is None
    social_serve = parser.parse_args(_social_serve_arguments())
    assert social_serve.opencli_social_node == _SOCIAL_FLAG_PATHS[0][1]
    assert social_serve.opencli_social_root == _SOCIAL_FLAG_PATHS[1][1]
    assert social_serve.opencli_social_cli == _SOCIAL_FLAG_PATHS[2][1]
    assert social_serve.opencli_social_session_home == _SOCIAL_FLAG_PATHS[3][1]
    trusted_serve = parser.parse_args(_social_serve_arguments(mask=0, xueqiu=True))
    assert trusted_serve.xueqiu_binding_manifest == _XUEQIU_MANIFEST
    with pytest.raises(SystemExit):
        parser.parse_args([*_social_serve_arguments(mask=0), "--linkedin-node", "/x"])
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


def test_connector_serve_without_activation_inputs_has_zero_group_side_effects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reader = _ReaderSpy(False)
    calls: list[ConnectorExecutionComposition | None] = []

    class MutationSpy:
        def serve(
            self,
            state_directory: Path,
            *,
            reader: object,
            bind_host: str,
            port: int,
            execution_composition: ConnectorExecutionComposition | None = None,
        ) -> None:
            assert state_directory == Path("/private/connector")
            assert bind_host == "127.0.0.1"
            assert port == 8443
            calls.append(execution_composition)

    monkeypatch.setattr(cli, "TtyPassphraseReader", lambda: reader)
    monkeypatch.setattr(
        cli,
        "attest_opencli_social_session",
        lambda *_: pytest.fail("unconfigured serve must not attest OpenCLI"),
    )
    monkeypatch.setattr(
        cli,
        "activate_xueqiu_binding",
        lambda *_: pytest.fail("unconfigured serve must not read Xueqiu manifest"),
    )

    connector_command(
        _parser().parse_args(_social_serve_arguments(mask=0)),
        mutation_service=MutationSpy(),  # type: ignore[arg-type]
    )

    assert calls == [None]
    assert reader.output == []
    assert reader.prompts == []
    assert reader.closed


@pytest.mark.parametrize("mask", range(1, 0b1111))
def test_connector_serve_rejects_every_partial_social_input_set(
    monkeypatch: pytest.MonkeyPatch,
    mask: int,
) -> None:
    reader = _ReaderSpy(False)
    serve_calls = 0

    class MutationSpy:
        def serve(self, *_: object, **__: object) -> None:
            nonlocal serve_calls
            serve_calls += 1

    monkeypatch.setattr(cli, "TtyPassphraseReader", lambda: reader)
    monkeypatch.setattr(
        cli,
        "attest_opencli_social_session",
        lambda *_: pytest.fail("partial social inputs must not be attested"),
    )

    connector_command(
        _parser().parse_args(_social_serve_arguments(mask=mask)),
        mutation_service=MutationSpy(),  # type: ignore[arg-type]
    )

    rendered = "".join(reader.output)
    assert serve_calls == 0
    assert reader.prompts == []
    assert "connector_state_invalid" in rendered
    assert all(str(path) not in rendered for _, path in _SOCIAL_FLAG_PATHS)
    assert reader.closed


def test_connector_serve_social_activation_attests_before_exact_tty_enable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    node = _SOCIAL_FLAG_PATHS[0][1]
    root = _SOCIAL_FLAG_PATHS[1][1]
    entrypoint = _SOCIAL_FLAG_PATHS[2][1]
    session_home = _SOCIAL_FLAG_PATHS[3][1]
    node_sha256 = "a" * 64
    tree_sha256 = "b" * 64
    events: list[str] = []
    attestation_calls: list[tuple[Path, Path, Path, Path]] = []

    def attest(
        selected_node: Path,
        selected_root: Path,
        selected_entrypoint: Path,
        selected_session_home: Path,
    ) -> OpenCliSessionAttestation:
        events.append("attest")
        attestation_calls.append(
            (
                selected_node,
                selected_root,
                selected_entrypoint,
                selected_session_home,
            )
        )
        return OpenCliSessionAttestation(
            selected_node,
            node_sha256,
            selected_root,
            selected_entrypoint,
            tree_sha256,
            selected_session_home,
        )

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
            events.append("serve")
            assert state_directory == Path("/private/connector")
            assert bind_host == "127.0.0.1"
            assert port == 8443
            self.compositions.append(execution_composition)

    args = _parser().parse_args(_social_serve_arguments())
    mutation = MutationSpy()
    monkeypatch.setattr(cli, "attest_opencli_social_session", attest)

    denied = _ReaderSpy(False, events)
    monkeypatch.setattr(cli, "TtyPassphraseReader", lambda: denied)
    connector_command(args, mutation_service=mutation)  # type: ignore[arg-type]

    denied_output = "".join(denied.output)
    assert events == ["attest", "confirm"]
    assert mutation.compositions == []
    assert denied.prompts == [("Type enable to continue: ", "enable")]
    assert "interactive_unlock_required" in denied_output
    assert all(str(path) not in denied_output for _, path in _SOCIAL_FLAG_PATHS)
    assert denied.closed

    events.clear()
    enabled = _ReaderSpy(True, events)
    monkeypatch.setattr(cli, "TtyPassphraseReader", lambda: enabled)
    connector_command(args, mutation_service=mutation)  # type: ignore[arg-type]

    rendered = "".join(enabled.output)
    rendered_scope_labels = tuple(
        line.removeprefix("scope: ")
        for line in rendered.splitlines()
        if line.startswith("scope: ")
    )
    assert events == ["attest", "confirm", "serve"]
    assert attestation_calls == [(node, root, entrypoint, session_home)] * 2
    assert "backend: opencli/1.8.6-hermes.1" in rendered
    assert f"digest: node-sha256:{node_sha256}" in rendered
    assert f"digest: opencli-tree-sha256:{tree_sha256}" in rendered
    assert "linkedin-service-log-threshold" not in rendered
    assert "operator-declared" not in rendered
    assert "scopes: 17" in rendered
    assert rendered_scope_labels == _SOCIAL_SCOPE_LABELS
    assert all(str(path) not in rendered for _, path in _SOCIAL_FLAG_PATHS)
    assert enabled.prompts == [("Type enable to continue: ", "enable")]
    assert enabled.closed
    assert len(mutation.compositions) == 1
    composition = mutation.compositions[0]
    assert repr(composition) == "ConnectorExecutionComposition(count=17)"
    assert composition is not None
    for label in _SOCIAL_SCOPE_LABELS:
        source, operation, data_scope = label.split(":")
        assert composition.required_scope(source, operation) == GrantScope(
            source, operation, data_scope
        )


def test_connector_serve_combines_all_configured_groups_after_one_confirmation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    capability_id = CapabilityId.new(lambda size: b"\x05" * size).for_grant()
    social_composition = ConnectorExecutionComposition()
    xueqiu_composition = ConnectorExecutionComposition()
    combined_composition = ConnectorExecutionComposition()
    social_attestation = SimpleNamespace(
        node_sha256="1" * 64,
        opencli_tree_sha256="2" * 64,
    )
    xueqiu_activation = SimpleNamespace(
        composition=xueqiu_composition,
        scope=GrantScope("xueqiu", "search.stocks", "public", capability_id),
        bws_sha256="b" * 64,
    )

    def social_attest(*paths: Path) -> object:
        events.append("social-attest")
        assert paths == tuple(path for _, path in _SOCIAL_FLAG_PATHS)
        return social_attestation

    def xueqiu_activate(path: Path) -> object:
        events.append("xueqiu-activate")
        assert path == _XUEQIU_MANIFEST
        return xueqiu_activation

    def combine(
        compositions: list[ConnectorExecutionComposition],
    ) -> ConnectorExecutionComposition:
        events.append("combine")
        assert compositions == [
            social_composition,
            xueqiu_composition,
        ]
        return combined_composition

    class MutationSpy:
        def serve(
            self,
            state_directory: Path,
            *,
            reader: object,
            bind_host: str,
            port: int,
            execution_composition: ConnectorExecutionComposition | None = None,
        ) -> None:
            events.append("serve")
            assert state_directory == Path("/private/connector")
            assert bind_host == "127.0.0.1"
            assert port == 8443
            assert execution_composition is combined_composition

    monkeypatch.setattr(cli, "attest_opencli_social_session", social_attest)
    monkeypatch.setattr(
        cli,
        "opencli_social_execution_composition",
        lambda value: social_composition,
    )
    monkeypatch.setattr(cli, "activate_xueqiu_binding", xueqiu_activate)
    monkeypatch.setattr(cli.ConnectorExecutionComposition, "combine", combine)
    reader = _ReaderSpy(True, events)
    monkeypatch.setattr(cli, "TtyPassphraseReader", lambda: reader)

    connector_command(
        _parser().parse_args(_social_serve_arguments(xueqiu=True)),
        mutation_service=MutationSpy(),  # type: ignore[arg-type]
    )

    assert events == [
        "social-attest",
        "xueqiu-activate",
        "confirm",
        "combine",
        "serve",
    ]
    assert reader.prompts == [("Type enable to continue: ", "enable")]
    rendered = "".join(reader.output)
    assert "backend: opencli/1.8.6-hermes.1" in rendered
    assert "backend: xueqiu-api/1.5.0+search.v1" in rendered
    assert f"scope: xueqiu:search.stocks:public:{capability_id}" in rendered
    assert "scopes: 18" in rendered
    for _, path in _SOCIAL_FLAG_PATHS:
        assert str(path) not in rendered
    assert str(_XUEQIU_MANIFEST) not in rendered
    assert "/private/" not in rendered
    for locator in (
        "project",
        "selector",
        "token",
        "profile_home",
        "query",
        "cookie",
        "password",
        "server_url",
    ):
        assert locator not in rendered.lower()
    assert reader.closed


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
                    scopes=(("github", "search.repositories", "public", None),),
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
