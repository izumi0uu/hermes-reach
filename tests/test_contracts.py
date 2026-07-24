from __future__ import annotations

import pytest

from hermes_reach.contracts import (
    ReachValidationError,
    error_response,
    planned_response,
    validate_browse,
    validate_read,
    validate_search,
)


def test_search_requires_explicit_unique_sources_in_input_order() -> None:
    calls = validate_search(
        {
            "protocol_version": "v1",
            "requests": [
                {
                    "source": "youtube",
                    "operation": "search.videos",
                    "query": "public query",
                    "options": {"limit": 5},
                },
                {
                    "source": "github",
                    "operation": "search.repositories",
                    "query": "public query",
                },
            ],
        }
    )

    assert [call.source.name for call in calls] == ["youtube", "github"]
    assert calls[0].options == {"limit": 5}


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"requests": []},
        {
            "requests": [
                {
                    "source": "github",
                    "operation": "search.repositories",
                    "query": "one",
                },
                {"source": "github", "operation": "search.code", "query": "two"},
            ]
        },
        {
            "requests": [
                {
                    "source": "github",
                    "operation": "search.repositories",
                    "query": "one",
                    "argv": ["issue", "create"],
                }
            ]
        },
    ],
)
def test_search_rejects_ambiguous_or_raw_backend_input(payload: object) -> None:
    with pytest.raises(ReachValidationError) as exc_info:
        validate_search(payload)

    assert exc_info.value.code == "invalid_argument"


def test_unknown_source_specific_option_is_rejected() -> None:
    with pytest.raises(ReachValidationError) as exc_info:
        validate_search(
            {
                "requests": [
                    {
                        "source": "github",
                        "operation": "search.repositories",
                        "query": "one",
                        "options": {"command": "gh issue create"},
                    }
                ]
            }
        )

    assert exc_info.value.code == "invalid_argument"


def test_read_target_is_closed_and_http_urls_are_checked() -> None:
    with pytest.raises(ReachValidationError) as exc_info:
        validate_read(
            {
                "source": "web",
                "operation": "read.url",
                "target": {"url": "file:///private-data"},
            }
        )

    assert exc_info.value.code == "invalid_target"


def test_operation_owned_targets_and_required_options_are_enforced() -> None:
    rss = validate_browse(
        {
            "source": "rss",
            "operation": "browse.entries",
            "target": {"url": "https://example.com/feed.xml"},
        }
    )
    node = validate_browse(
        {
            "source": "v2ex",
            "operation": "browse.node_topics",
            "options": {"node": "python", "page": 2},
        }
    )

    assert rss.target == {"url": "https://example.com/feed.xml"}
    assert node.options == {"node": "python", "page": 2}

    with pytest.raises(ReachValidationError):
        validate_browse({"source": "rss", "operation": "browse.entries"})
    with pytest.raises(ReachValidationError):
        validate_browse({"source": "v2ex", "operation": "browse.node_topics"})
    with pytest.raises(ReachValidationError):
        validate_read(
            {
                "source": "v2ex",
                "operation": "read.topic",
                "target": {"native_id": "not-a-topic-id"},
            }
        )


def test_deferred_operations_keep_foundation_target_behavior() -> None:
    for target in (
        {"url": "https://example.com/repository"},
        {"native_id": "owner/repository"},
        {"resource_ref": "opaque-reference"},
    ):
        call = validate_read(
            {
                "source": "github",
                "operation": "read.repository",
                "target": target,
            }
        )
        assert call.target == target


def test_planned_response_does_not_echo_query_or_target() -> None:
    secret_query = "private-query-token-should-not-appear"
    calls = validate_search(
        {
            "requests": [
                {
                    "source": "github",
                    "operation": "search.repositories",
                    "query": secret_query,
                }
            ]
        }
    )

    response = planned_response(calls, "trace")
    validation_error = ReachValidationError(
        "invalid_argument", "The request does not match the tool contract.", "Fix it."
    )

    assert secret_query not in str(response)
    assert secret_query not in str(error_response(validation_error, "trace"))
