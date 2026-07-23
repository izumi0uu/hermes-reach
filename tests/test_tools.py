from __future__ import annotations

import argparse
import builtins
import json
import os
import socket
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from hermes_reach.cli import command_payload, register_cli
from hermes_reach.tools import (
    reach_browse,
    reach_read,
    reach_search,
    reach_status,
    reach_transcribe,
)


def _unexpected_side_effect(*args: object, **kwargs: object) -> None:
    del args, kwargs
    raise AssertionError("foundation tools must not perform this side effect")


def test_known_operation_is_unavailable_without_network_or_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(socket, "create_connection", _unexpected_side_effect)
    monkeypatch.setattr(subprocess, "run", _unexpected_side_effect)

    response = json.loads(
        reach_search(
            {
                "requests": [
                    {
                        "source": "github",
                        "operation": "search.repositories",
                        "query": "private-query-token",
                    }
                ]
            }
        )
    )

    assert response["outcome"] == "error"
    assert response["error"]["code"] == "capability_unavailable"
    assert response["groups"][0]["availability"] == "unavailable"
    assert "private-query-token" not in json.dumps(response)


@pytest.mark.parametrize(
    ("handler", "args"),
    [
        (
            reach_read,
            {
                "source": "web",
                "operation": "read.url",
                "target": {"url": "https://example.com"},
            },
        ),
        (reach_browse, {"source": "v2ex", "operation": "browse.hot"}),
        (
            reach_transcribe,
            {
                "source": "youtube",
                "operation": "transcribe.video",
                "target": {"url": "https://example.com/media"},
            },
        ),
    ],
)
def test_non_search_tools_use_the_same_planned_contract(
    handler: Callable[[dict[str, object]], str], args: dict[str, object]
) -> None:
    response = json.loads(handler(args))

    assert response["error"]["code"] == "capability_unavailable"
    assert response["groups"][0]["availability"] == "unavailable"


def test_status_is_local_and_lists_all_sources() -> None:
    response = json.loads(reach_status({}))

    assert response["outcome"] == "ok"
    assert len(response["data"]["sources"]) == 15
    assert {source["availability"] for source in response["data"]["sources"]} == {
        "unavailable"
    }


def test_invalid_input_returns_redacted_error() -> None:
    private_value = "super-secret-user-input"
    response = json.loads(
        reach_search(
            {
                "requests": [
                    {
                        "source": "github",
                        "operation": "search.repositories",
                        "query": private_value,
                        "unexpected": private_value,
                    }
                ]
            }
        )
    )

    assert response["error"]["code"] == "invalid_argument"
    assert private_value not in json.dumps(response)


def test_all_public_entrypoints_are_side_effect_free(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parser = argparse.ArgumentParser()
    register_cli(parser)

    monkeypatch.setattr(socket, "create_connection", _unexpected_side_effect)
    monkeypatch.setattr(subprocess, "run", _unexpected_side_effect)
    monkeypatch.setattr(subprocess, "Popen", _unexpected_side_effect)
    monkeypatch.setattr(os, "getenv", _unexpected_side_effect)
    monkeypatch.setattr(builtins, "open", _unexpected_side_effect)
    monkeypatch.setattr(Path, "write_text", _unexpected_side_effect)
    monkeypatch.setattr(Path, "write_bytes", _unexpected_side_effect)
    monkeypatch.setattr(Path, "mkdir", _unexpected_side_effect)

    tool_calls: tuple[
        tuple[Callable[[dict[str, object]], str], dict[str, object]], ...
    ] = (
        (
            reach_search,
            {
                "requests": [
                    {
                        "source": "github",
                        "operation": "search.repositories",
                        "query": "query",
                    }
                ]
            },
        ),
        (
            reach_read,
            {
                "source": "web",
                "operation": "read.url",
                "target": {"url": "https://example.com"},
            },
        ),
        (reach_browse, {"source": "v2ex", "operation": "browse.hot"}),
        (
            reach_transcribe,
            {
                "source": "youtube",
                "operation": "transcribe.video",
                "target": {"url": "https://example.com/media"},
            },
        ),
        (reach_status, {}),
    )
    for handler, arguments in tool_calls:
        assert json.loads(handler(arguments))["trace_id"]

    command_arguments = (
        parser.parse_args(["status", "--json"]),
        parser.parse_args(["sources", "--json"]),
        parser.parse_args(["doctor", "--json"]),
        parser.parse_args(["setup", "--yes", "--json"]),
        parser.parse_args(["updates", "check", "--json"]),
    )
    for arguments in command_arguments:
        assert command_payload(arguments)["trace_id"]
