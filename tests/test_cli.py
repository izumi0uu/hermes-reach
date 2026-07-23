from __future__ import annotations

import argparse
import json

import pytest

from hermes_reach import cli
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
            "current", "0.1.0a0", "0.19.0", "v1", "baseline", "Pinned locally."
        ),
    )

    setup = command_payload(parser.parse_args(["setup", "--yes"]))
    updates = command_payload(parser.parse_args(["updates", "check", "--json"]))

    assert setup["error"]["code"] == "capability_unavailable"
    assert updates["outcome"] == "ok"
    assert updates["data"]["status"] == "current"
