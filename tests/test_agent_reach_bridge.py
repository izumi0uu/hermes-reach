from __future__ import annotations

import json
import sys
from collections.abc import Callable
from dataclasses import dataclass, fields, replace
from importlib import import_module
from pathlib import Path
from typing import cast

import pytest

import hermes_reach.agent_reach_bridge as bridge
from hermes_reach.agent_reach_bridge import (
    AGENT_REACH_DISTRIBUTION,
    AGENT_REACH_FORK_COMMIT,
    AGENT_REACH_FORK_URL,
    AGENT_REACH_VERSION,
    SAFE_AGENT_REACH_DOCTOR_CHANNELS,
    AgentReachBridgeError,
    AgentReachCatalog,
    AgentReachChannel,
    AgentReachInstallation,
    ReadOnlyAgentReachConfig,
    collect_agent_reach_health,
    load_agent_reach_catalog,
    upstream_doctor_data,
    validate_agent_reach_execution_contract,
    validate_agent_reach_provenance,
)
from hermes_reach.catalog import SOURCE_CATALOG


@dataclass(frozen=True)
class FakeChannel:
    name: str
    description: str
    backends: list[str]
    tier: int

    def check(self, _config: object) -> tuple[str, str]:
        raise AssertionError("catalog discovery must not run an upstream health check")


@dataclass(frozen=True)
class FakeCapability:
    protocol_version: str
    source: str
    operation: str
    argument_schema_id: str
    result_schema_ids: tuple[str, ...]
    backend_id: str
    backend_version: str
    required_host_capabilities: tuple[str, ...]
    maximum_items: int
    maximum_document_bytes: int
    maximum_metadata_bytes: int
    maximum_output_bytes: int
    maximum_content_type_characters: int
    maximum_content_location_characters: int
    maximum_text_characters: int
    maximum_title_characters: int
    maximum_url_characters: int
    maximum_native_id_characters: int
    maximum_author_characters: int
    maximum_published_characters: int


def _channels() -> list[FakeChannel]:
    return [
        FakeChannel(
            "exa_search" if source.name == "exa" else source.name,
            f"{source.display_name} description",
            [f"{source.name}-backend"],
            1 if source.access_class != "credential_free" else 0,
        )
        for source in SOURCE_CATALOG
    ]


def _version(distribution: str) -> str:
    assert distribution == AGENT_REACH_DISTRIBUTION
    return AGENT_REACH_VERSION


def _direct_url(distribution: str) -> str:
    assert distribution == AGENT_REACH_DISTRIBUTION
    return json.dumps(
        {
            "url": AGENT_REACH_FORK_URL,
            "vcs_info": {
                "vcs": "git",
                "requested_revision": AGENT_REACH_FORK_COMMIT,
                "commit_id": AGENT_REACH_FORK_COMMIT,
            },
        }
    )


def _capabilities() -> tuple[FakeCapability, FakeCapability]:
    common = {
        "protocol_version": "v1",
        "source": "rss",
        "backend_id": "feedparser",
        "backend_version": "6.0.12",
        "required_host_capabilities": ("fetched_document.v1",),
        "maximum_document_bytes": 1_048_576,
        "maximum_metadata_bytes": 16_384,
        "maximum_output_bytes": 1_048_576,
        "maximum_content_type_characters": 512,
        "maximum_content_location_characters": 8_192,
        "maximum_text_characters": 16_000,
        "maximum_title_characters": 4_096,
        "maximum_url_characters": 8_192,
        "maximum_native_id_characters": 512,
        "maximum_author_characters": 2_048,
        "maximum_published_characters": 512,
    }
    return (
        FakeCapability(
            **common,
            operation="read.feed",
            argument_schema_id="rss.read.feed.arguments.v1",
            result_schema_ids=("rss.feed.v1",),
            maximum_items=1,
        ),
        FakeCapability(
            **common,
            operation="browse.entries",
            argument_schema_id="rss.browse.entries.arguments.v1",
            result_schema_ids=("rss.entry.v1",),
            maximum_items=21,
        ),
    )


def _catalog() -> AgentReachCatalog:
    return load_agent_reach_catalog(
        lambda: _channels(),
        _version,
        direct_url_reader=_direct_url,
    )


def _execution_module() -> object:
    return import_module("agent_reach.execution.v1")


def _registry_module() -> object:
    return import_module("agent_reach.execution.v1.registry")


def _drifted_capability(capability: object, field: str, value: object) -> object:
    drifted = object.__new__(type(capability))
    for descriptor in fields(capability):
        object.__setattr__(
            drifted, descriptor.name, getattr(capability, descriptor.name)
        )
    object.__setattr__(drifted, field, value)
    return drifted


def test_catalog_reuses_the_actual_pinned_agent_reach_channel_registry() -> None:
    catalog = load_agent_reach_catalog()

    assert catalog.version == AGENT_REACH_VERSION
    assert len(catalog.channels) == 15
    assert {channel.source for channel in catalog.channels} == {
        source.name for source in SOURCE_CATALOG
    }


def test_catalog_maps_the_upstream_registry_without_running_a_health_probe() -> None:
    catalog = _catalog()

    exa = next(channel for channel in catalog.channels if channel.source == "exa")

    assert exa.upstream_name == "exa_search"
    assert exa.backends == ("exa-backend",)


@pytest.mark.parametrize(
    ("channels", "version_reader"),
    [
        (_channels()[:-1], _version),
        (_channels() + [_channels()[0]], _version),
        (_channels(), lambda _: "1.5.1"),
    ],
)
def test_catalog_rejects_agent_reach_drift(
    channels: list[FakeChannel],
    version_reader: Callable[[str], str],
) -> None:
    with pytest.raises(AgentReachBridgeError):
        load_agent_reach_catalog(
            lambda: channels,
            version_reader,
            direct_url_reader=_direct_url,
        )


def test_catalog_registration_does_not_read_feedparser_distribution_metadata() -> None:
    reads: list[str] = []

    def version_reader(distribution: str) -> str:
        reads.append(distribution)
        if distribution != AGENT_REACH_DISTRIBUTION:
            raise AssertionError(
                "registration must not inspect the backend distribution"
            )
        return AGENT_REACH_VERSION

    catalog = load_agent_reach_catalog(
        lambda: _channels(),
        version_reader,
        direct_url_reader=_direct_url,
    )

    assert len(catalog.channels) == 15
    assert reads == [AGENT_REACH_DISTRIBUTION]


def test_exact_pep610_owner_fork_provenance_is_accepted() -> None:
    validate_agent_reach_provenance(_direct_url)


@pytest.mark.parametrize(
    "document",
    [
        None,
        "",
        "{}",
        json.dumps(
            {
                "url": "https://github.com/Panniantong/Agent-Reach.git",
                "vcs_info": {
                    "vcs": "git",
                    "requested_revision": AGENT_REACH_FORK_COMMIT,
                    "commit_id": AGENT_REACH_FORK_COMMIT,
                },
            }
        ),
        json.dumps(
            {
                "url": AGENT_REACH_FORK_URL,
                "vcs_info": {
                    "vcs": "git",
                    "requested_revision": "hermes/execution-v1",
                    "commit_id": AGENT_REACH_FORK_COMMIT,
                },
            }
        ),
        json.dumps(
            {
                "url": AGENT_REACH_FORK_URL,
                "vcs_info": {
                    "vcs": "git",
                    "requested_revision": AGENT_REACH_FORK_COMMIT,
                    "commit_id": "b4d52c46c9113cb0f653d6df4cf71ebadf4930ac",
                },
            }
        ),
        json.dumps(
            {
                "url": AGENT_REACH_FORK_URL,
                "vcs_info": {
                    "vcs": "git",
                    "requested_revision": AGENT_REACH_FORK_COMMIT,
                    "commit_id": AGENT_REACH_FORK_COMMIT,
                },
                "dir_info": {},
            }
        ),
        (
            f'{{"url":"{AGENT_REACH_FORK_URL}",'
            f'"url":"{AGENT_REACH_FORK_URL}",'
            f'"vcs_info":{{"vcs":"git","requested_revision":"'
            f'{AGENT_REACH_FORK_COMMIT}","commit_id":"'
            f'{AGENT_REACH_FORK_COMMIT}"}}}}'
        ),
    ],
)
def test_pep610_provenance_drift_fails_closed(document: object) -> None:
    with pytest.raises(AgentReachBridgeError, match="source provenance"):
        validate_agent_reach_provenance(lambda _: document)  # type: ignore[return-value]


def test_provenance_fails_before_any_agent_reach_import() -> None:
    imports: list[str] = []

    def import_forbidden(name: str) -> object:
        imports.append(name)
        raise AssertionError("provenance drift must prevent package import")

    with pytest.raises(AgentReachBridgeError, match="source provenance"):
        validate_agent_reach_execution_contract(
            direct_url_reader=lambda _: "{}",
            execution_module_loader=lambda: import_forbidden(
                "agent_reach.execution.v1"
            ),
        )

    assert imports == []


def test_execution_version_fails_before_any_agent_reach_import() -> None:
    imports: list[str] = []

    def import_forbidden(name: str) -> object:
        imports.append(name)
        raise AssertionError("version drift must prevent package import")

    with pytest.raises(AgentReachBridgeError, match="installed version"):
        validate_agent_reach_execution_contract(
            version_reader=lambda _: "1.5.1",
            direct_url_reader=_direct_url,
            execution_module_loader=lambda: import_forbidden(
                "agent_reach.execution.v1"
            ),
        )

    assert imports == []


def test_static_handshake_only_discovers_capabilities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delitem(sys.modules, "agent_reach.execution.v1.rss", raising=False)

    api = validate_agent_reach_execution_contract(direct_url_reader=_direct_url)

    assert api.execute is _execution_module().execute
    assert len(api.capabilities) == 2
    assert "agent_reach.execution.v1.rss" not in sys.modules


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("hash_algorithm", "sha512"),
        ("hash_value", "invalid-record-hash"),
        ("size", 5_028),
    ],
)
def test_static_handshake_rejects_record_metadata_drift(
    field: str,
    value: object,
) -> None:
    relative = "agent_reach/execution/v1/registry.py"
    imports: list[str] = []
    installation = bridge._default_installation_reader(AGENT_REACH_DISTRIBUTION)
    installed_file = installation.files[relative]
    drifted_file = replace(installed_file, **{field: value})
    drifted_files = dict(installation.files)
    drifted_files[relative] = drifted_file

    with pytest.raises(AgentReachBridgeError, match="capability contract"):
        validate_agent_reach_execution_contract(
            direct_url_reader=_direct_url,
            installation_reader=lambda _: AgentReachInstallation(
                installation.direct_url_document,
                drifted_files,
            ),
            execution_module_loader=lambda: imports.append("execution"),
        )

    assert imports == []


def test_static_handshake_rejects_record_content_drift(tmp_path: Path) -> None:
    relative = "agent_reach/execution/v1/registry.py"
    imports: list[str] = []
    installation = bridge._default_installation_reader(AGENT_REACH_DISTRIBUTION)
    installed_file = installation.files[relative]
    shadow = tmp_path / "registry.py"
    shadow.write_bytes(b"x" * cast(int, installed_file.size))
    drifted_file = replace(installed_file, path=shadow)
    drifted_files = dict(installation.files)
    drifted_files[relative] = drifted_file

    with pytest.raises(AgentReachBridgeError, match="capability contract"):
        validate_agent_reach_execution_contract(
            direct_url_reader=_direct_url,
            installation_reader=lambda _: AgentReachInstallation(
                installation.direct_url_document,
                drifted_files,
            ),
            execution_module_loader=lambda: imports.append("execution"),
        )

    assert imports == []


def test_static_handshake_requires_the_fork_rss_record() -> None:
    relative = "agent_reach/execution/v1/rss.py"
    imports: list[str] = []
    installation = bridge._default_installation_reader(AGENT_REACH_DISTRIBUTION)
    drifted_files = dict(installation.files)
    drifted_files.pop(relative)

    with pytest.raises(AgentReachBridgeError, match="capability contract"):
        validate_agent_reach_execution_contract(
            direct_url_reader=_direct_url,
            installation_reader=lambda _: AgentReachInstallation(
                installation.direct_url_document,
                drifted_files,
            ),
            execution_module_loader=lambda: imports.append("execution"),
        )

    assert imports == []


@pytest.mark.parametrize(
    ("field", "drifted_value"),
    [
        ("protocol_version", "v2"),
        ("source", "web"),
        ("operation", "browse.feed"),
        ("argument_schema_id", "rss.browse.entries.arguments.v2"),
        ("result_schema_ids", ("rss.entry.v2",)),
        ("backend_id", "other-parser"),
        ("backend_version", "6.0.13"),
        ("required_host_capabilities", ("url.v1",)),
        ("maximum_items", 20),
        ("maximum_document_bytes", 1_048_575),
        ("maximum_metadata_bytes", 16_383),
        ("maximum_output_bytes", 1_048_575),
        ("maximum_content_type_characters", 511),
        ("maximum_content_location_characters", 8_191),
        ("maximum_text_characters", 15_999),
        ("maximum_title_characters", 4_095),
        ("maximum_url_characters", 8_191),
        ("maximum_native_id_characters", 511),
        ("maximum_author_characters", 2_047),
        ("maximum_published_characters", 511),
    ],
)
def test_static_handshake_rejects_every_descriptor_drift(
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    drifted_value: object,
) -> None:
    execution = _execution_module()
    registry = _registry_module()
    capabilities = list(execution.list_capabilities())
    capabilities[1] = _drifted_capability(
        capabilities[1],
        field,
        drifted_value,
    )
    monkeypatch.setattr(registry, "_CAPABILITIES", tuple(capabilities))

    with pytest.raises(AgentReachBridgeError, match="capability contract"):
        validate_agent_reach_execution_contract(direct_url_reader=_direct_url)


@pytest.mark.parametrize(
    "drift",
    [
        "protocol",
        "host_capability",
        "missing_capability",
        "reversed_capabilities",
        "list_capabilities",
    ],
)
def test_static_handshake_rejects_protocol_host_and_registry_drift(
    monkeypatch: pytest.MonkeyPatch,
    drift: str,
) -> None:
    execution = _execution_module()
    registry = _registry_module()
    capabilities = execution.list_capabilities()
    if drift == "protocol":
        monkeypatch.setattr(execution, "PROTOCOL_VERSION", "v2")
    elif drift == "host_capability":
        monkeypatch.setattr(execution, "FETCHED_DOCUMENT_CAPABILITY", "fetched_url.v1")
    elif drift == "missing_capability":
        monkeypatch.setattr(registry, "_CAPABILITIES", capabilities[:1])
    elif drift == "reversed_capabilities":
        monkeypatch.setattr(registry, "_CAPABILITIES", tuple(reversed(capabilities)))
    else:
        monkeypatch.setattr(registry, "_CAPABILITIES", list(capabilities))

    with pytest.raises(AgentReachBridgeError, match="capability contract"):
        validate_agent_reach_execution_contract(direct_url_reader=_direct_url)


def test_static_handshake_rejects_a_shape_compatible_fake_dataclass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_registry_module(), "_CAPABILITIES", _capabilities())

    with pytest.raises(AgentReachBridgeError, match="capability contract"):
        validate_agent_reach_execution_contract(direct_url_reader=_direct_url)


def test_static_handshake_rejects_a_missing_execute_export(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execution = _execution_module()
    monkeypatch.setattr(execution, "execute", None)

    with pytest.raises(AgentReachBridgeError, match="capability contract"):
        validate_agent_reach_execution_contract(direct_url_reader=_direct_url)


def test_static_handshake_rejects_a_callable_from_a_mixed_module(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execution = _execution_module()
    monkeypatch.setattr(execution, "execute", lambda *_: object())

    with pytest.raises(AgentReachBridgeError, match="capability contract"):
        validate_agent_reach_execution_contract(direct_url_reader=_direct_url)


def test_static_handshake_rejects_a_fake_required_contract_class(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execution = _execution_module()
    monkeypatch.setattr(execution, "ExecutionRequestV1", FakeCapability)

    with pytest.raises(AgentReachBridgeError, match="capability contract"):
        validate_agent_reach_execution_contract(direct_url_reader=_direct_url)


@pytest.mark.parametrize(
    "module_name",
    [
        "agent_reach.execution.v1",
        "agent_reach.execution.v1.registry",
        "agent_reach.execution.v1.contracts",
    ],
)
def test_static_handshake_rejects_module_origin_drift(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    module_name: str,
) -> None:
    shadow = tmp_path / "shadow.py"
    shadow.write_text("# shadow\n", encoding="ascii")
    monkeypatch.setattr(import_module(module_name), "__file__", str(shadow))

    with pytest.raises(AgentReachBridgeError, match="capability contract"):
        validate_agent_reach_execution_contract(direct_url_reader=_direct_url)


def test_worker_handshake_rejects_rss_runtime_module_origin_drift(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    shadow = tmp_path / "rss.py"
    shadow.write_text("# shadow\n", encoding="ascii")
    monkeypatch.setattr(
        import_module("agent_reach.execution.v1.rss"),
        "__file__",
        str(shadow),
    )

    with pytest.raises(AgentReachBridgeError, match="capability contract"):
        validate_agent_reach_execution_contract(
            direct_url_reader=_direct_url,
            validate_runtime_module=True,
        )


def test_registration_orders_provenance_capabilities_then_channels() -> None:
    events: list[str] = []

    def direct_url(distribution: str) -> str:
        events.append("provenance")
        return _direct_url(distribution)

    def execution_module() -> object:
        events.append("capabilities")
        return _execution_module()

    def channels() -> list[FakeChannel]:
        events.append("channels")
        return _channels()

    load_agent_reach_catalog(
        channels,
        _version,
        direct_url_reader=direct_url,
        execution_module_loader=execution_module,
    )

    assert events == ["provenance", "capabilities", "channels"]


def test_upstream_doctor_projection_is_redacted_and_uses_read_only_config() -> None:
    catalog = _catalog()

    def doctor(config: ReadOnlyAgentReachConfig) -> dict[str, object]:
        assert config.get("token", "absent") == "absent"
        assert config.is_configured("groq_whisper") is False
        assert not hasattr(config, "__dict__")
        assert not hasattr(config, "set")
        return {
            channel.upstream_name: {
                "status": "warn" if channel.source == "youtube" else "ok",
                "message": "/private/host-command --token=secret",
                "active_backend": channel.backends[0],
            }
            for channel in catalog.channels
        }

    result = upstream_doctor_data(doctor, lambda: catalog)

    youtube = next(item for item in result["channels"] if item["source"] == "youtube")
    assert youtube["availability"] == "degraded"
    assert youtube["active_backend"] == "youtube-backend"
    assert "host-command" not in str(result)
    assert "secret" not in str(result)
    assert result["pinned_commit"] == AGENT_REACH_FORK_COMMIT
    assert result["fork_commit"] == AGENT_REACH_FORK_COMMIT
    assert result["execution_protocol_version"] == "v1"


def test_upstream_doctor_rejects_an_incomplete_or_unknown_report() -> None:
    catalog = AgentReachCatalog(
        AGENT_REACH_VERSION,
        (AgentReachChannel("web", "web", "Web", ("Jina",), 0),),
    )

    with pytest.raises(AgentReachBridgeError):
        upstream_doctor_data(lambda _: {}, lambda: catalog)


def test_upstream_health_never_probes_session_or_credential_channels() -> None:
    calls: set[str] = set()

    @dataclass
    class ProbeChannel:
        name: str
        active_backend: str | None = None

        def check(self, _config: object) -> tuple[str, str]:
            if self.name not in SAFE_AGENT_REACH_DOCTOR_CHANNELS:
                raise AssertionError("restricted channel probe")
            calls.add(self.name)
            self.active_backend = f"{self.name}-backend"
            return "ok", "/private/raw-upstream-message"

    channels = [
        ProbeChannel("exa_search" if source.name == "exa" else source.name)
        for source in SOURCE_CATALOG
    ]

    report = collect_agent_reach_health(ReadOnlyAgentReachConfig(), lambda: channels)

    assert calls == SAFE_AGENT_REACH_DOCTOR_CHANNELS
    assert "xiaoyuzhou" not in SAFE_AGENT_REACH_DOCTOR_CHANNELS
    assert report["xueqiu"]["reach_policy"] == "connector_required"
    assert report["xiaoyuzhou"]["reach_policy"] == "connector_required"
    assert "raw-upstream-message" not in str(report)
