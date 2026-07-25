from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import pytest

from hermes_reach.agent_reach_bridge import (
    AGENT_REACH_VERSION,
    SAFE_AGENT_REACH_DOCTOR_CHANNELS,
    AgentReachBridgeError,
    AgentReachCatalog,
    AgentReachChannel,
    ReadOnlyAgentReachConfig,
    collect_agent_reach_health,
    load_agent_reach_catalog,
    upstream_doctor_data,
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


def _version(_: str) -> str:
    return AGENT_REACH_VERSION


def test_catalog_reuses_the_actual_pinned_agent_reach_channel_registry() -> None:
    catalog = load_agent_reach_catalog()

    assert catalog.version == AGENT_REACH_VERSION
    assert len(catalog.channels) == 15
    assert {channel.source for channel in catalog.channels} == {
        source.name for source in SOURCE_CATALOG
    }


def test_catalog_maps_the_upstream_registry_without_running_a_health_probe() -> None:
    catalog = load_agent_reach_catalog(lambda: _channels(), _version)

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
        load_agent_reach_catalog(lambda: channels, version_reader)


def test_upstream_doctor_projection_is_redacted_and_uses_read_only_config() -> None:
    catalog = load_agent_reach_catalog(lambda: _channels(), _version)

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

    report = collect_agent_reach_health(
        ReadOnlyAgentReachConfig(), lambda: channels
    )

    assert calls == SAFE_AGENT_REACH_DOCTOR_CHANNELS
    assert "xiaoyuzhou" not in SAFE_AGENT_REACH_DOCTOR_CHANNELS
    assert report["xueqiu"]["reach_policy"] == "connector_required"
    assert report["xiaoyuzhou"]["reach_policy"] == "connector_required"
    assert "raw-upstream-message" not in str(report)
