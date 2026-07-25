from __future__ import annotations

import argparse
import json

import pytest

from hermes_reach import cli
from hermes_reach.agent_reach_bridge import AgentReachBridgeError
from hermes_reach.cli import command_payload, register_cli, render_command
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
