from __future__ import annotations

import argparse
import asyncio
import builtins
import json
import os
import socket
import subprocess
from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest

import hermes_reach.tools as reach_tools
from hermes_reach.cli import command_payload, register_cli
from hermes_reach.sources.public_http import HttpResponse
from hermes_reach.sources.registry import build_alpha1_runtime
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


def _run(value: Awaitable[str]) -> dict[str, object]:
    return json.loads(asyncio.run(value))


class _FixtureHttpClient:
    def __init__(self, response: HttpResponse) -> None:
        self.response = response
        self.calls: list[str] = []

    async def get(self, url: str) -> HttpResponse:
        self.calls.append(url)
        return self.response


def test_github_execution_uses_the_injected_public_http_client_without_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(socket, "create_connection", _unexpected_side_effect)
    monkeypatch.setattr(subprocess, "run", _unexpected_side_effect)
    client = _FixtureHttpClient(
        HttpResponse(
            200,
            "application/vnd.github+json",
            b'{"items":[]}',
            "https://api.github.com/search/repositories",
        )
    )
    monkeypatch.setattr(reach_tools, "_RUNTIME", build_alpha1_runtime(client))

    response = _run(
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

    assert response["outcome"] == "ok"
    assert response["groups"][0]["availability"] == "available"
    assert response["groups"][0]["items"] == []
    assert client.calls == [
        "https://api.github.com/search/repositories?q=private-query-token&per_page=20"
    ]
    assert "private-query-token" not in json.dumps(response)


def test_planned_exa_search_requires_setup_without_echoing_query() -> None:
    private_query = "private-exa-query"

    response = _run(
        reach_search(
            {
                "requests": [
                    {
                        "source": "exa",
                        "operation": "search.web",
                        "query": private_query,
                    }
                ]
            }
        )
    )

    assert response["outcome"] == "error"
    assert response["groups"][0]["availability"] == "setup_required"
    assert response["groups"][0]["error"]["code"] == "setup_required"
    assert response["groups"][0]["attempts"] == []
    assert private_query not in json.dumps(response)


@pytest.mark.parametrize(
    ("handler", "args"),
    [
        (
            reach_read,
            {
                "source": "reddit",
                "operation": "read.post",
                "target": {
                    "url": "https://www.reddit.com/r/python/comments/abc123/a_post"
                },
            },
        ),
        (reach_browse, {"source": "reddit", "operation": "browse.hot"}),
        (
            reach_transcribe,
            {
                "source": "youtube",
                "operation": "transcribe.video",
                "target": {"url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"},
            },
        ),
    ],
)
def test_unbound_or_planned_non_search_tools_return_unavailable(
    handler: Callable[[dict[str, object]], Awaitable[str]],
    args: dict[str, object],
) -> None:
    response = _run(handler(args))

    assert response["error"]["code"] == "all_sources_failed"
    assert response["groups"][0]["availability"] == "unavailable"


def test_reach_read_executes_injected_web_runtime_end_to_end(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private_url = "https://example.com/article?private=token"
    client = _FixtureHttpClient(
        HttpResponse(
            200,
            "text/html; charset=utf-8",
            b"<html><head><title>Fixture</title></head><body>Useful body</body></html>",
            "https://example.com/article",
        )
    )
    monkeypatch.setattr(reach_tools, "_RUNTIME", build_alpha1_runtime(client))

    response = _run(
        reach_read(
            {
                "source": "web",
                "operation": "read.url",
                "target": {"url": private_url},
            }
        )
    )

    assert client.calls == [private_url]
    assert response["outcome"] == "ok"
    group = response["groups"][0]
    assert group["source"] == "web"
    assert group["provenance"]["backend_id"] == "web-public-http-v1"
    assert group["items"][0]["title"] == "Fixture"
    assert group["items"][0]["url"] == "https://example.com/article"
    assert private_url not in json.dumps(response)


def test_status_is_local_and_lists_all_sources() -> None:
    response = json.loads(reach_status({}))

    assert response["outcome"] == "ok"
    assert len(response["data"]["sources"]) == 15
    availability = {
        source["source"]: source["availability"]
        for source in response["data"]["sources"]
    }
    assert availability["web"] == "available"
    assert availability["rss"] == "available"
    assert availability["v2ex"] == "available"
    assert availability["exa"] == "setup_required"
    assert availability["github"] == "available"
    assert availability["youtube"] == "setup_required"
    assert availability["bilibili"] == "available"


def test_status_can_filter_planned_operations_without_hiding_released_rows() -> None:
    response = json.loads(reach_status({"include_planned": False}))
    sources = {source["source"]: source for source in response["data"]["sources"]}

    assert [operation["name"] for operation in sources["web"]["operations"]] == [
        "read.url"
    ]
    assert sources["exa"]["operations"] == []
    assert sources["exa"]["availability"] == "unavailable"
    assert len(sources["github"]["operations"]) == 8
    assert sources["github"]["availability"] == "available"
    assert [operation["name"] for operation in sources["youtube"]["operations"]] == [
        "search.videos",
        "read.video",
        "read.subtitles",
        "read.comments",
    ]


def test_invalid_input_returns_redacted_error() -> None:
    private_value = "super-secret-user-input"
    response = _run(
        reach_search(
            {
                "requests": [
                    {
                        "source": "exa",
                        "operation": "search.web",
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
        tuple[Callable[[dict[str, object]], Awaitable[str]], dict[str, object]], ...
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
                "source": "github",
                "operation": "read.repository",
                "target": {"native_id": "owner/repository"},
            },
        ),
        (reach_browse, {"source": "reddit", "operation": "browse.hot"}),
        (
            reach_transcribe,
            {
                "source": "youtube",
                "operation": "transcribe.video",
                "target": {"url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"},
            },
        ),
    )
    for handler, arguments in tool_calls:
        assert _run(handler(arguments))["trace_id"]
    assert json.loads(reach_status({}))["trace_id"]

    command_arguments = (
        parser.parse_args(["status", "--json"]),
        parser.parse_args(["sources", "--json"]),
        parser.parse_args(["doctor", "--json"]),
        parser.parse_args(["setup", "--yes", "--json"]),
        parser.parse_args(["updates", "check", "--json"]),
    )
    for arguments in command_arguments:
        assert command_payload(arguments)["trace_id"]
