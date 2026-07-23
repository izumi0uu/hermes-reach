from __future__ import annotations

import argparse
import json

from hermes_reach.cli import command_payload, register_cli, render_command


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


def test_setup_and_updates_fail_closed_without_mutation() -> None:
    parser = _parser()

    setup = command_payload(parser.parse_args(["setup", "--yes"]))
    updates = command_payload(parser.parse_args(["updates", "check", "--json"]))

    assert setup["error"]["code"] == "capability_unavailable"
    assert updates["error"]["code"] == "capability_unavailable"
