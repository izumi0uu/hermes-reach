from __future__ import annotations

import asyncio
import json

from hermes_reach.contracts import validate_browse, validate_read, validate_search
from hermes_reach.runtime.policy import ReadOnlyPolicy
from hermes_reach.sources.github import GitHubAdapter
from hermes_reach.sources.public_http import HttpFailure, HttpResponse


class FixtureHttpClient:
    def __init__(self, *responses: HttpResponse | Exception) -> None:
        self._responses = list(responses)
        self.calls: list[str] = []

    async def get(self, url: str) -> HttpResponse:
        self.calls.append(url)
        value = self._responses.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


def _json_response(value: object) -> HttpResponse:
    return HttpResponse(
        200,
        "application/vnd.github+json; charset=utf-8",
        json.dumps(value).encode(),
        "https://api.github.com/redacted",
    )


def _authorized_read(operation: str, native_id: str):
    return ReadOnlyPolicy().authorize(
        validate_read(
            {
                "source": "github",
                "operation": operation,
                "target": {"native_id": native_id},
            }
        )
    )


def test_github_adapter_uses_only_fixed_routes_and_projects_known_fields() -> None:
    client = FixtureHttpClient(
        _json_response(
            {
                "items": [
                    {
                        "full_name": "openai/hermes-reach",
                        "name": "hermes-reach",
                        "description": "Useful repository",
                        "owner": {"login": "openai"},
                        "updated_at": "2026-07-24T00:00:00Z",
                    }
                ]
            }
        ),
        _json_response(
            {
                "items": [
                    {
                        "name": "plugin.py",
                        "path": "src/hermes_reach/plugin.py",
                        "repository": {"full_name": "openai/hermes-reach"},
                    }
                ]
            }
        ),
        _json_response(
            {
                "full_name": "openai/hermes-reach",
                "name": "hermes-reach",
                "description": "Useful repository",
                "owner": {"login": "openai"},
            }
        ),
        _json_response(
            {
                "number": 42,
                "title": "Issue title",
                "body": "Issue body",
                "user": {"login": "alice"},
                "created_at": "2026-07-24T00:00:00Z",
            }
        ),
        _json_response(
            {
                "number": 43,
                "title": "Pull title",
                "body": "Pull body",
                "user": {"login": "bob"},
            }
        ),
        _json_response(
            {
                "workflow_runs": [
                    {
                        "id": 14,
                        "name": "test",
                        "display_title": "Run tests",
                        "status": "completed",
                        "conclusion": "success",
                    }
                ]
            }
        ),
        _json_response({"id": 15, "name": "release", "status": "queued"}),
        _json_response(
            [
                {
                    "id": 99,
                    "tag_name": "v1.0.0",
                    "name": "Release one",
                    "body": "Notes",
                    "author": {"login": "maintainer"},
                }
            ]
        ),
    )
    adapter = GitHubAdapter(client)
    search_calls = validate_search(
        {
            "requests": [
                {
                    "source": "github",
                    "operation": "search.repositories",
                    "query": "private repository query",
                }
            ]
        }
    )
    code_calls = validate_search(
        {
            "requests": [
                {
                    "source": "github",
                    "operation": "search.code",
                    "query": "private code query",
                    "options": {"limit": 3},
                }
            ]
        }
    )
    calls = (
        ReadOnlyPolicy().authorize(search_calls[0]),
        ReadOnlyPolicy().authorize(code_calls[0]),
        _authorized_read("read.repository", "openai/hermes-reach"),
        _authorized_read("read.issue", "openai/hermes-reach#42"),
        _authorized_read("read.pull_request", "openai/hermes-reach#43"),
        ReadOnlyPolicy().authorize(
            validate_browse(
                {
                    "source": "github",
                    "operation": "browse.actions",
                    "target": {"native_id": "openai/hermes-reach"},
                    "options": {"limit": 2},
                }
            )
        ),
        _authorized_read("read.action_run", "openai/hermes-reach#15"),
        ReadOnlyPolicy().authorize(
            validate_browse(
                {
                    "source": "github",
                    "operation": "browse.releases",
                    "target": {"native_id": "openai/hermes-reach"},
                }
            )
        ),
    )

    results = [asyncio.run(adapter.execute(call)) for call in calls]

    assert all(result.is_success for result in results)
    assert [result.items[0].native_id for result in results] == [
        "openai/hermes-reach",
        "openai/hermes-reach:src/hermes_reach/plugin.py",
        "openai/hermes-reach",
        "openai/hermes-reach#42",
        "openai/hermes-reach#43",
        "openai/hermes-reach#14",
        "openai/hermes-reach#15",
        "99",
    ]
    assert client.calls == [
        "https://api.github.com/search/repositories?q=private+repository+query&per_page=20",
        "https://api.github.com/search/code?q=private+code+query&per_page=3",
        "https://api.github.com/repos/openai/hermes-reach",
        "https://api.github.com/repos/openai/hermes-reach/issues/42",
        "https://api.github.com/repos/openai/hermes-reach/pulls/43",
        "https://api.github.com/repos/openai/hermes-reach/actions/runs?per_page=2",
        "https://api.github.com/repos/openai/hermes-reach/actions/runs/15",
        "https://api.github.com/repos/openai/hermes-reach/releases?per_page=20",
    ]
    assert results[0].items[0].url == "https://github.com/openai/hermes-reach"
    assert (
        results[7].items[0].url
        == "https://github.com/openai/hermes-reach/releases/tag/v1.0.0"
    )


def test_github_adapter_rejects_malformed_payload_and_maps_closed_http_failure() -> (
    None
):
    malformed = FixtureHttpClient(_json_response({"items": [{"name": "missing"}]}))
    call = validate_search(
        {
            "requests": [
                {
                    "source": "github",
                    "operation": "search.repositories",
                    "query": "private query",
                }
            ]
        }
    )[0]
    result = asyncio.run(
        GitHubAdapter(malformed).execute(ReadOnlyPolicy().authorize(call))
    )
    assert result.failure_class == "permanent"

    rate_limited = FixtureHttpClient(HttpFailure("rate_limit", "private-location"))
    result = asyncio.run(
        GitHubAdapter(rate_limited).execute(ReadOnlyPolicy().authorize(call))
    )
    assert result.failure_class == "rate_limit"
