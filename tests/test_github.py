from __future__ import annotations

import asyncio

from hermes_reach.contracts import validate_browse, validate_read, validate_search
from hermes_reach.sources.public_http import HttpResponse
from hermes_reach.sources.registry import build_alpha1_registry, build_alpha1_runtime


class NoHttpClient:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def get(self, url: str) -> HttpResponse:
        self.calls.append(url)
        raise AssertionError("disabled GitHub operations attempted HTTP execution")


def test_all_github_rows_fail_closed_without_http_execution() -> None:
    client = NoHttpClient()
    registry = build_alpha1_registry(client)
    runtime = build_alpha1_runtime(client)
    calls = (
        validate_search(
            {
                "requests": [
                    {
                        "source": "github",
                        "operation": "search.repositories",
                        "query": "repositories",
                    }
                ]
            }
        )[0],
        validate_search(
            {
                "requests": [
                    {
                        "source": "github",
                        "operation": "search.code",
                        "query": "code",
                        "options": {"limit": 3},
                    }
                ]
            }
        )[0],
        validate_read(
            {
                "source": "github",
                "operation": "read.repository",
                "target": {"native_id": "openai/hermes-reach"},
            }
        ),
        validate_read(
            {
                "source": "github",
                "operation": "read.issue",
                "target": {"native_id": "openai/hermes-reach#42"},
            }
        ),
        validate_read(
            {
                "source": "github",
                "operation": "read.pull_request",
                "target": {"native_id": "openai/hermes-reach#43"},
            }
        ),
        validate_browse(
            {
                "source": "github",
                "operation": "browse.actions",
                "target": {"native_id": "openai/hermes-reach"},
                "options": {"limit": 2},
            }
        ),
        validate_read(
            {
                "source": "github",
                "operation": "read.action_run",
                "target": {"native_id": "openai/hermes-reach#15"},
            }
        ),
        validate_browse(
            {
                "source": "github",
                "operation": "browse.releases",
                "target": {"native_id": "openai/hermes-reach"},
            }
        ),
    )
    expected_reason = (
        "The Agent-Reach-selected gh backend remains frozen pending a "
        "credential-free, read-only execution review."
    )

    assert len(calls) == 8
    for call in calls:
        operation = call.operation
        record = registry.availability("github", operation.name)

        assert operation.implementation_state == "planned"
        assert operation.unavailable_reason == expected_reason
        assert record.state == "unavailable"
        assert record.reason == expected_reason
        assert record.backend_id is None
        assert record.backend_version is None
        assert registry.has_binding("github", operation.name) is False
        assert asyncio.run(runtime.dispatch(call)) is None

    assert client.calls == []
