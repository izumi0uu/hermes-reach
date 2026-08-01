from __future__ import annotations

import io
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import SimpleNamespace
from typing import cast

import pytest

import hermes_reach.sources.opencli_social_worker as worker

POST_URL = "https://www.reddit.com/r/python/comments/abc123/fixture_post"
_SESSION_FIELDS = {
    "node_executable": "/opt/hermes-reach/opencli/bin/node",
    "node_sha256": "a" * 64,
    "opencli_root": "/opt/hermes-reach/opencli",
    "opencli_cli": (
        "/opt/hermes-reach/opencli/node_modules/@jackwener/opencli/dist/src/main.js"
    ),
    "opencli_tree_sha256": "b" * 64,
    "session_home": "/Users/operator/opencli-session",
}

CASES = (
    ("reddit", "search.posts", {"query": "private query", "limit": 7}),
    ("reddit", "read.post", {"url": POST_URL}),
    ("reddit", "browse.subreddit", {"subreddit": "Python_3", "limit": 7}),
    ("reddit", "browse.hot", {"limit": 7}),
    ("reddit", "browse.popular", {"limit": 7}),
    ("reddit", "browse.all", {"limit": 7}),
    ("reddit", "read.subreddit", {"subreddit": "Python_3"}),
    ("facebook", "search", {"query": "private query", "limit": 7}),
    ("facebook", "read.profile", {"username": "open.ai-profile"}),
    ("facebook", "browse.feed", {"limit": 7}),
    ("facebook", "browse.groups", {"limit": 7}),
    ("instagram", "search.users", {"query": "private query", "limit": 7}),
    ("instagram", "read.profile", {"username": "openai.dev"}),
    (
        "instagram",
        "browse.user_posts",
        {"username": "openai.dev", "limit": 7},
    ),
    ("instagram", "browse.explore", {"limit": 7}),
)


@dataclass(frozen=True)
class _ExecutionRequest:
    protocol_version: str
    source: str
    operation: str
    arguments: Mapping[str, object]


@dataclass(frozen=True)
class _OpenCliSession:
    node_executable: str
    node_sha256: str
    opencli_root: str
    opencli_cli: str
    opencli_tree_sha256: str
    session_home: str


@dataclass(frozen=True)
class _Limits:
    maximum_items: int
    maximum_text_characters: int


@dataclass(frozen=True)
class _Context:
    host_capabilities: tuple[object, ...]
    checkpoint: Callable[[], None]
    limits: _Limits

    def __init__(
        self,
        host_capabilities: tuple[object, ...],
        *,
        checkpoint: Callable[[], None],
        limits: _Limits,
    ) -> None:
        object.__setattr__(self, "host_capabilities", host_capabilities)
        object.__setattr__(self, "checkpoint", checkpoint)
        object.__setattr__(self, "limits", limits)


@dataclass(frozen=True)
class _Item:
    schema_id: object
    fields: object


@dataclass(frozen=True)
class _Success:
    protocol_version: object = "v1"
    source: object = "reddit"
    operation: object = "search.posts"
    backend_id: object = "opencli"
    backend_version: object = "1.8.6-hermes.1"
    items: object = ()
    truncated: object = False
    partial_error_code: object = None


@dataclass(frozen=True)
class _Failure:
    protocol_version: object = "v1"
    source: object = "reddit"
    operation: object = "search.posts"
    backend_id: object = "opencli"
    backend_version: object = "1.8.6-hermes.1"
    error_code: object = "transient"


def _api(execute: Callable[[object, object], object]) -> SimpleNamespace:
    return SimpleNamespace(
        execution_request_type=_ExecutionRequest,
        opencli_session_type=_OpenCliSession,
        execution_limits_type=_Limits,
        execution_context_type=_Context,
        execution_item_type=_Item,
        execution_success_type=_Success,
        execution_failure_type=_Failure,
        execute=execute,
    )


def _provider(value: SimpleNamespace) -> worker.ExecutionApiProvider:
    return cast(worker.ExecutionApiProvider, lambda: value)


def _session() -> worker.WorkerSession:
    return worker.WorkerSession(**_SESSION_FIELDS)


def _request(
    source: str,
    operation: str,
    arguments: Mapping[str, object],
    *,
    deadline: float | None = None,
) -> worker.WorkerRequest:
    raw = worker.encode_request(
        source,
        operation,
        arguments,
        _SESSION_FIELDS,
        deadline=time.monotonic() + 60 if deadline is None else deadline,
    )
    return worker._read_request(io.BytesIO(raw))


def _schema(source: str, operation: str) -> str:
    return worker._OPERATION_CONTRACTS[(source, operation)].schema


def _native_item(source: str, operation: str) -> _Item:
    schema = _schema(source, operation)
    samples: dict[str, dict[str, object]] = {
        "reddit.post.v1": {
            "text": "Post body",
            "native_id": "abc123",
            "title": "Post title",
            "url": (
                "https://www.reddit.com/r/Python_3/comments/abc123/fixture_post"
                if operation == "browse.subreddit"
                else POST_URL
            ),
            "author": "alice",
            "published_at": "2026-08-01T00:00:00Z",
            "score": 42,
            "comment_count": 7,
            "subreddit": "Python_3" if operation == "browse.subreddit" else "python",
            "media_type": "self",
        },
        "reddit.thread.item.v1": {
            "text": "Post body",
            "native_id": "abc123",
            "title": "Post title",
            "url": POST_URL,
            "author": "alice",
            "score": 42,
            "kind": "post",
            "media_type": "self",
        },
        "reddit.subreddit.v1": {
            "text": "Community description",
            "native_id": "Python_3",
            "title": "Python 3",
            "url": "https://www.reddit.com/r/Python_3/",
            "published_at": "2008-01-01",
            "subscriber_count": 100,
            "active_count": 5,
            "nsfw": 0,
            "subreddit_type": "public",
        },
        "facebook.search.result.v1": {
            "text": "Search summary",
            "native_id": "1",
            "title": "Search result",
            "url": "https://www.facebook.com/result",
        },
        "facebook.profile.v1": {
            "text": None,
            "native_id": "open.ai-profile",
            "title": "Open AI",
            "url": "https://www.facebook.com/open.ai-profile",
            "friend_count": 12,
            "follower_count": 34,
        },
        "facebook.post.v1": {
            "text": "Feed post",
            "native_id": "1",
            "author": "Alice",
            "reaction_count": 12,
            "comment_count": 3,
            "share_count": 2,
        },
        "facebook.group.v1": {
            "text": "Last activity",
            "native_id": "1",
            "title": "A group",
            "url": "https://www.facebook.com/groups/example",
        },
        "instagram.user.v1": {
            "native_id": "openai.dev",
            "title": "OpenAI",
            "url": "https://www.instagram.com/openai.dev/",
            "verified": 1,
            "private": 0,
        },
        "instagram.profile.v1": {
            "text": "Profile bio",
            "native_id": "openai.dev",
            "title": "OpenAI",
            "url": "https://www.instagram.com/openai.dev/",
            "follower_count": 100,
            "following_count": 20,
            "post_count": 30,
            "verified": 1,
        },
        "instagram.post.v1": {
            "text": "Post caption",
            "native_id": "1",
            "author": "openai.dev",
            "published_at": (None if operation == "browse.explore" else "2026-08-01"),
            "reaction_count": 50,
            "comment_count": 4,
            "media_type": "image",
        },
    }
    return _Item(schema, samples[schema])


def _framed(value: Mapping[str, object]) -> bytes:
    return worker._encode_frame(value, worker.MAX_OUTPUT_BYTES)


def _worker_success_value(
    source: str,
    operation: str,
    arguments: Mapping[str, object],
) -> dict[str, object]:
    request = _request(source, operation, arguments)
    value = worker._execute_request(
        request,
        execution_api_provider=_provider(
            _api(
                lambda *_: _Success(
                    source=source,
                    operation=operation,
                    items=(_native_item(source, operation),),
                )
            )
        ),
    )
    assert "items" in value
    return dict(value)


@pytest.mark.parametrize(("source", "operation", "arguments"), CASES)
def test_all_fifteen_worker_requests_are_closed_and_round_trip(
    source: str,
    operation: str,
    arguments: dict[str, object],
) -> None:
    request = _request(source, operation, arguments)

    assert request.source == source
    assert request.operation == operation
    assert dict(request.arguments) == arguments
    assert request.session == _session()
    assert "private query" not in repr(request)
    assert "/Users/operator" not in repr(request)


@pytest.mark.parametrize(("source", "operation", "arguments"), CASES)
def test_worker_builds_exact_fork_request_context_and_normalizes_every_operation(
    source: str,
    operation: str,
    arguments: dict[str, object],
) -> None:
    request = _request(source, operation, arguments)
    calls: list[tuple[object, object]] = []

    def execute(execution_request: object, context: object) -> object:
        calls.append((execution_request, context))
        return _Success(
            source=source,
            operation=operation,
            items=(_native_item(source, operation),),
        )

    value = worker._execute_request(
        request,
        execution_api_provider=_provider(_api(execute)),
    )

    assert len(calls) == 1
    execution_request = cast(_ExecutionRequest, calls[0][0])
    context = cast(_Context, calls[0][1])
    assert execution_request == _ExecutionRequest("v1", source, operation, arguments)
    assert context.host_capabilities == (_OpenCliSession(**_SESSION_FIELDS),)
    assert context.limits.maximum_items == min(
        cast(int, arguments.get("limit", 20)),
        worker._OPERATION_CONTRACTS[(source, operation)].maximum_items,
    )
    assert context.limits.maximum_text_characters == 16_000
    context.checkpoint()

    response = worker.decode_response(
        _framed(value),
        source=source,
        operation=operation,
        arguments=arguments,
    )
    assert isinstance(response, worker.OpenCliSocialProjection)
    assert response.source == source
    assert response.operation == operation
    assert len(response.items) == 1
    assert "private query" not in repr(response)


def test_worker_preserves_rich_fields_in_deterministic_bounded_text() -> None:
    request = _request("instagram", "read.profile", {"username": "openai.dev"})
    value = worker._execute_request(
        request,
        execution_api_provider=_provider(
            _api(
                lambda *_: _Success(
                    source="instagram",
                    operation="read.profile",
                    items=(_native_item("instagram", "read.profile"),),
                )
            )
        ),
    )
    response = worker.decode_response(
        _framed(value),
        source="instagram",
        operation="read.profile",
        arguments={"username": "openai.dev"},
    )

    assert isinstance(response, worker.OpenCliSocialProjection)
    assert response.items[0].text == (
        "Profile bio | followers: 100 | following: 20 | posts: 30 | verified: yes"
    )


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: {**value, "command": "forbidden"},
        lambda value: {**value, "source": "twitter"},
        lambda value: {**value, "operation": "write.post"},
        lambda value: {**value, "arguments": {"query": " query", "limit": 1}},
        lambda value: {**value, "arguments": {"limit": True}},
        lambda value: {
            **value,
            "session": {**cast(dict[str, object], value["session"]), "cookie": "x"},
        },
        lambda value: {**value, "deadline": float("nan")},
    ],
)
def test_worker_request_rejects_authority_and_shape_drift(
    mutation: Callable[[dict[str, object]], dict[str, object]],
) -> None:
    value: dict[str, object] = {
        "arguments": {"query": "private query", "limit": 1},
        "deadline": time.monotonic() + 60,
        "operation": "search.posts",
        "protocol": "v1",
        "session": dict(_SESSION_FIELDS),
        "source": "reddit",
    }

    with pytest.raises(worker.OpenCliSocialProtocolError):
        worker._validated_request(mutation(value))


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://www.reddit.com/r/python/comments/abc123/fixture_post", "abc123"),
        ("https://www.reddit.com/r/python/comments/abc123/fixture_post/", "abc123"),
        ("https://www.reddit.com/r/python/comments/abc123/fixture_post/extra", None),
    ],
)
def test_reddit_post_url_accepts_only_the_optional_trailing_slash(
    url: str,
    expected: str | None,
) -> None:
    assert worker._reddit_post_id(url) == expected


def test_worker_preserves_only_closed_correlated_failure_code() -> None:
    request = _request("facebook", "search", {"query": "private query", "limit": 1})
    value = worker._execute_request(
        request,
        execution_api_provider=_provider(
            _api(
                lambda *_: _Failure(
                    source="facebook",
                    operation="search",
                    error_code="rate_limit",
                )
            )
        ),
    )

    assert worker.decode_response(
        _framed(value),
        source="facebook",
        operation="search",
        arguments={"query": "private query", "limit": 1},
    ) == worker.ForkExecutionFailure("facebook", "search", "rate_limit")
    assert "private query" not in repr(value)
    assert "/Users/operator" not in repr(value)


@pytest.mark.parametrize(
    "result",
    [
        _Success(source="twitter"),
        _Success(backend_version="future"),
        _Success(partial_error_code="transient"),
        _Success(items=(_Item("future.schema", {}),)),
        _Failure(error_code="future"),
        object(),
    ],
)
def test_worker_converts_fork_identity_or_contract_drift_to_closed_failure(
    result: object,
) -> None:
    request = _request("reddit", "search.posts", {"query": "private query", "limit": 1})

    value = worker._execute_request(
        request,
        execution_api_provider=_provider(_api(lambda *_: result)),
    )

    assert value == worker._failure_value(
        ("reddit", "search.posts"), "backend_contract_violation"
    )
    assert "private query" not in repr(value)


def test_worker_absolute_deadline_stops_before_contract_loading() -> None:
    request = worker.WorkerRequest(
        "reddit",
        "search.posts",
        {"query": "private query", "limit": 1},
        _session(),
        time.monotonic() - 1,
    )
    called = False

    def provider() -> object:
        nonlocal called
        called = True
        return _api(lambda *_: object())

    value = worker._execute_request(
        request,
        execution_api_provider=cast(worker.ExecutionApiProvider, provider),
    )

    assert value == worker._failure_value(
        ("reddit", "search.posts"), "deadline_exceeded"
    )
    assert called is False


def test_parent_decoder_rejects_backend_identity_payload_and_result_drift() -> None:
    base = {
        "backend": {"id": "opencli", "version": "1.8.6-hermes.1"},
        "items": [],
        "operation": "search.posts",
        "protocol": "v1",
        "schema": "reddit.post.v1",
        "source": "reddit",
        "truncated": False,
    }
    mutations = (
        {**base, "query": "private query"},
        {**base, "source": "facebook"},
        {**base, "schema": "future"},
        {**base, "truncated": 1},
        {**base, "backend": {"id": "opencli", "version": "future"}},
        {
            **base,
            "items": [
                {
                    "author": None,
                    "kind": "entry",
                    "native_id": None,
                    "published_at": None,
                    "text": "x" * 16_001,
                    "title": None,
                    "url": None,
                }
            ],
        },
    )

    for mutation in mutations:
        with pytest.raises(worker.OpenCliSocialProtocolError):
            worker.decode_response(
                _framed(mutation),
                source="reddit",
                operation="search.posts",
                arguments={"query": "private query", "limit": 1},
            )


@pytest.mark.parametrize(
    ("changes", "arguments"),
    [
        ({"native_id": "different.user"}, {"username": "openai.dev"}),
        (
            {"url": "https://www.instagram.com/different.user/"},
            {"username": "openai.dev"},
        ),
        ({"url": "https://example.com/openai.dev/"}, {"username": "openai.dev"}),
        (
            {"url": "https://www.instagram.com/openai.dev/?next=private"},
            {"username": "openai.dev"},
        ),
        ({"kind": "reply"}, {"username": "openai.dev"}),
        ({"author": "injected.user"}, {"username": "openai.dev"}),
        ({"text": "two\nlines"}, {"username": "openai.dev"}),
    ],
)
def test_parent_decoder_rejects_profile_target_or_projection_drift(
    changes: Mapping[str, object],
    arguments: Mapping[str, object],
) -> None:
    value = _worker_success_value("instagram", "read.profile", arguments)
    items = cast(list[dict[str, object]], value["items"])
    value["items"] = [{**items[0], **changes}]

    with pytest.raises(worker.OpenCliSocialProtocolError):
        worker.decode_response(
            _framed(value),
            source="instagram",
            operation="read.profile",
            arguments=arguments,
        )


def test_parent_decoder_rejects_subreddit_target_drift() -> None:
    arguments = {"subreddit": "Python_3", "limit": 1}
    value = _worker_success_value("reddit", "browse.subreddit", arguments)
    items = cast(list[dict[str, object]], value["items"])
    value["items"] = [
        {
            **items[0],
            "url": "https://www.reddit.com/r/python/comments/abc123/fixture_post",
        }
    ]

    with pytest.raises(worker.OpenCliSocialProtocolError):
        worker.decode_response(
            _framed(value),
            source="reddit",
            operation="browse.subreddit",
            arguments=arguments,
        )


def test_parent_decoder_rejects_thread_order_and_duplicate_identifiers() -> None:
    thread_arguments = {"url": POST_URL}
    thread = _worker_success_value("reddit", "read.post", thread_arguments)
    thread_items = cast(list[dict[str, object]], thread["items"])
    thread["items"] = [{**thread_items[0], "kind": "reply"}]

    with pytest.raises(worker.OpenCliSocialProtocolError):
        worker.decode_response(
            _framed(thread),
            source="reddit",
            operation="read.post",
            arguments=thread_arguments,
        )

    search_arguments = {"query": "private query", "limit": 2}
    search = _worker_success_value("facebook", "search", search_arguments)
    search_items = cast(list[dict[str, object]], search["items"])
    search["items"] = [search_items[0], dict(search_items[0])]

    with pytest.raises(worker.OpenCliSocialProtocolError):
        worker.decode_response(
            _framed(search),
            source="facebook",
            operation="search",
            arguments=search_arguments,
        )


@pytest.mark.parametrize(
    ("source", "operation", "arguments", "field", "value"),
    [
        (
            "facebook",
            "read.profile",
            {"username": "open.ai-profile"},
            "url",
            "https://www.facebook.com/different.user",
        ),
        (
            "instagram",
            "browse.user_posts",
            {"username": "openai.dev", "limit": 1},
            "author",
            "different.user",
        ),
    ],
)
def test_parent_decoder_rejects_operation_specific_target_drift(
    source: str,
    operation: str,
    arguments: Mapping[str, object],
    field: str,
    value: object,
) -> None:
    response = _worker_success_value(source, operation, arguments)
    items = cast(list[dict[str, object]], response["items"])
    response["items"] = [{**items[0], field: value}]

    with pytest.raises(worker.OpenCliSocialProtocolError):
        worker.decode_response(
            _framed(response),
            source=source,
            operation=operation,
            arguments=arguments,
        )


def test_worker_rejects_native_subreddit_target_drift() -> None:
    arguments = {"subreddit": "Python_3", "limit": 1}
    request = _request("reddit", "browse.subreddit", arguments)
    native = _native_item("reddit", "browse.subreddit")
    fields = cast(dict[str, object], native.fields)
    drifted = _Item(
        native.schema_id,
        {
            **fields,
            "subreddit": "python",
            "url": "https://www.reddit.com/r/python/comments/abc123/fixture_post",
        },
    )

    value = worker._execute_request(
        request,
        execution_api_provider=_provider(
            _api(
                lambda *_: _Success(
                    source="reddit",
                    operation="browse.subreddit",
                    items=(drifted,),
                )
            )
        ),
    )

    assert value == worker._failure_value(
        ("reddit", "browse.subreddit"), "backend_contract_violation"
    )


def test_projection_budget_truncates_without_changing_item_order() -> None:
    items = tuple(
        worker.SocialItemProjection(
            "entry",
            f"{index}:" + "x" * 4_000,
            native_id=str(index),
            title="title",
        )
        for index in range(10)
    )

    selected, truncated = worker._fit_result_budget(items)

    assert truncated is True
    assert [item.native_id for item in selected] == [
        str(index) for index in range(len(selected))
    ]
    assert sum(worker._projected_item_characters(item) for item in selected) <= 16_000


def test_request_frame_rejects_duplicate_json_keys_without_echoing_values() -> None:
    payload = b'{"protocol":"v1","protocol":"v1"}'
    framed = len(payload).to_bytes(4, "big") + payload

    with pytest.raises(worker.OpenCliSocialProtocolError) as caught:
        worker._read_request(io.BytesIO(framed))

    assert "protocol" not in str(caught.value)
