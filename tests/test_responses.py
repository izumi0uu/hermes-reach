from __future__ import annotations

import json

from hermes_reach.contracts import OperationCall, validate_search
from hermes_reach.runtime.adapters import MediaMetadata, RawItem
from hermes_reach.runtime.availability import AvailabilityRecord
from hermes_reach.runtime.responses import (
    execution_response,
    runner_group,
    unavailable_group,
)
from hermes_reach.runtime.runner import AttemptProvenance, RunnerResult


def _search_calls() -> tuple[OperationCall, OperationCall]:
    calls = validate_search(
        {
            "requests": [
                {
                    "source": "exa",
                    "operation": "search.web",
                    "query": "private-query-alpha",
                },
                {
                    "source": "github",
                    "operation": "search.repositories",
                    "query": "private-query-beta",
                },
            ]
        }
    )
    return calls[0], calls[1]


def test_execution_response_maps_complete_result_to_ok() -> None:
    exa, _ = _search_calls()
    result = RunnerResult(
        (RawItem("body", kind="result", title="Result"),),
        False,
        (AttemptProvenance("exa-client", "1.2.3", 4, "success"),),
        selected_backend_id="exa-client",
        selected_backend_version="1.2.3",
    )

    response = execution_response((runner_group(exa, result),), "trace-safe")

    assert response["outcome"] == "ok"
    assert "error" not in response
    assert response["groups"][0]["items"][0]["title"] == "Result"
    assert response["groups"][0]["provenance"]["backend_id"] == "exa-client"


def test_execution_response_emits_only_the_closed_versioned_media_projection() -> None:
    exa, _ = _search_calls()
    result = RunnerResult(
        (
            RawItem(
                "subtitle text",
                kind="content",
                media=MediaMetadata(
                    subtitle_language="zh-Hans",
                    subtitle_origin="automatic",
                    coverage="partial",
                ),
            ),
        ),
        False,
        (),
    )

    response = execution_response((runner_group(exa, result),), "trace-safe")

    assert response["groups"][0]["items"][0]["media"] == {
        "version": "v1",
        "coverage": "partial",
        "subtitle_language": "zh-Hans",
        "subtitle_origin": "automatic",
    }


def test_execution_response_preserves_group_order_and_maps_mixed_to_partial() -> None:
    exa, github = _search_calls()
    success = runner_group(
        exa,
        RunnerResult(
            (RawItem("body", kind="result"),),
            False,
            (),
            selected_backend_id="exa-client",
            selected_backend_version="1.2.3",
        ),
    )
    unavailable = unavailable_group(
        github,
        AvailabilityRecord("unavailable", "No adapter is configured."),
    )

    response = execution_response((success, unavailable), "trace-safe")

    assert response["outcome"] == "partial"
    assert [group["source"] for group in response["groups"]] == ["exa", "github"]
    assert "error" not in response
    serialized = json.dumps(response)
    assert "private-query-alpha" not in serialized
    assert "private-query-beta" not in serialized


def test_execution_response_maps_incomplete_source_to_partial() -> None:
    exa, _ = _search_calls()
    partial = runner_group(
        exa,
        RunnerResult(
            (RawItem("usable", kind="result"),),
            False,
            (AttemptProvenance("exa-client", "1.2.3", 8, "partial"),),
            partial_failure_class="transient",
            selected_backend_id="exa-client",
            selected_backend_version="1.2.3",
        ),
    )

    response = execution_response((partial,), "trace-safe")

    assert response["outcome"] == "partial"
    assert response["groups"][0]["availability"] == "degraded"
    assert response["groups"][0]["error"]["code"] == "partial_source_result"


def test_execution_response_maps_all_failures_to_redacted_error() -> None:
    exa, github = _search_calls()
    failed = runner_group(
        exa,
        RunnerResult(
            (),
            False,
            (AttemptProvenance("exa-client", "1.2.3", 5, "transient"),),
            failure_class="transient",
        ),
    )
    setup_required = unavailable_group(
        github,
        AvailabilityRecord("setup_required", "Complete local operator setup."),
    )

    response = execution_response((failed, setup_required), "trace-safe")

    assert response["outcome"] == "error"
    assert response["error"]["code"] == "all_sources_failed"
    assert [group["source"] for group in response["groups"]] == ["exa", "github"]
    serialized = json.dumps(response)
    assert "private-query-alpha" not in serialized
    assert "private-query-beta" not in serialized
