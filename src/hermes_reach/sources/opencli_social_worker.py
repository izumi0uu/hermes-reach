"""Closed Agent-Reach OpenCLI social execution inside an isolated worker."""

from __future__ import annotations

import ipaddress
import json
import math
import re
import signal
import sys
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import BinaryIO, Final, Literal, Protocol, TypeAlias, cast
from urllib.parse import urlsplit

from ..agent_reach_bridge import (
    AgentReachExecutionApi,
    validate_agent_reach_execution_contract,
)
from ..normalized import MAX_NORMALIZED_INTEGER, normalized_item_characters

WorkerSource = Literal[
    "reddit",
    "facebook",
    "instagram",
    "twitter",
    "xiaohongshu",
]
WorkerOperation = Literal[
    "search.posts",
    "read.post",
    "browse.subreddit",
    "browse.hot",
    "browse.popular",
    "browse.all",
    "read.subreddit",
    "search",
    "read.profile",
    "browse.feed",
    "browse.groups",
    "search.users",
    "browse.user_posts",
    "browse.explore",
    "search.notes",
]
WorkerErrorCode = Literal[
    "unsupported_protocol_version",
    "invalid_request",
    "unsupported_source",
    "unsupported_operation",
    "host_capability_missing",
    "backend_unavailable",
    "backend_incompatible",
    "deadline_exceeded",
    "cancelled",
    "invalid_input",
    "not_found",
    "authentication",
    "authorization",
    "rate_limit",
    "transient",
    "permanent",
    "backend_contract_violation",
]
ResultKind = Literal["content", "entry", "topic", "reply", "profile", "result"]
ArgumentValue = str | int

PROTOCOL_VERSION: Final = "v1"
EXPECTED_BACKEND_ID: Final = "opencli"
EXPECTED_BACKEND_VERSION: Final = "1.8.6-hermes.1"
MAX_REQUEST_BYTES: Final = 64 * 1024
MAX_OUTPUT_BYTES: Final = 512 * 1024
MAX_QUERY_CHARACTERS: Final = 4_096
MAX_LIMIT: Final = 50
MAX_RESULT_ITEMS: Final = 20
MAX_RESULT_CHARACTERS: Final = 16_000
MAX_TEXT_CHARACTERS: Final = 16_000
MAX_TITLE_CHARACTERS: Final = 4_096
MAX_URL_CHARACTERS: Final = 8_192
MAX_NATIVE_ID_CHARACTERS: Final = 512
MAX_AUTHOR_CHARACTERS: Final = 2_048
MAX_PUBLISHED_CHARACTERS: Final = 512
MAX_JSON_DEPTH: Final = 12
MAX_JSON_NODES: Final = 1_024
MAX_JSON_STRING_BYTES: Final = MAX_TEXT_CHARACTERS * 4
_PUBLIC_TITLE_CHARACTERS: Final = 512
_PUBLIC_URL_CHARACTERS: Final = 4_096
_PUBLIC_AUTHOR_CHARACTERS: Final = 256
_PUBLIC_PUBLISHED_CHARACTERS: Final = 128
_LENGTH_BYTES: Final = 4
_CANCELLATION_REQUESTED = threading.Event()

_REQUEST_FIELDS: Final = frozenset(
    {"arguments", "deadline", "operation", "protocol", "session", "source"}
)
_SESSION_FIELDS: Final = frozenset(
    {
        "node_executable",
        "node_sha256",
        "opencli_root",
        "opencli_cli",
        "opencli_tree_sha256",
        "session_home",
    }
)
_SUCCESS_FIELDS: Final = frozenset(
    {"backend", "items", "operation", "protocol", "schema", "source", "truncated"}
)
_FAILURE_FIELDS: Final = frozenset(
    {"backend", "error", "operation", "protocol", "source"}
)
_BACKEND_FIELDS: Final = frozenset({"id", "version"})
_ERROR_FIELDS: Final = frozenset({"code"})
_PROJECTED_ITEM_FIELDS: Final = frozenset(
    {"author", "kind", "native_id", "published_at", "text", "title", "url"}
)
_ERROR_CODES: Final[frozenset[str]] = frozenset(
    {
        "unsupported_protocol_version",
        "invalid_request",
        "unsupported_source",
        "unsupported_operation",
        "host_capability_missing",
        "backend_unavailable",
        "backend_incompatible",
        "deadline_exceeded",
        "cancelled",
        "invalid_input",
        "not_found",
        "authentication",
        "authorization",
        "rate_limit",
        "transient",
        "permanent",
        "backend_contract_violation",
    }
)


class _WorkerCancellationRequested(Exception):
    pass


def _request_worker_cancellation(_signum: int, _frame: object) -> None:
    _CANCELLATION_REQUESTED.set()


_RESULT_KINDS: Final[frozenset[str]] = frozenset(
    {"content", "entry", "topic", "reply", "profile", "result"}
)
_SHA256: Final = re.compile(r"[0-9a-f]{64}")
_SUBREDDIT: Final = re.compile(r"[A-Za-z][A-Za-z0-9_]{2,20}")
_SOCIAL_USERNAME: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
_REDDIT_POST_ID: Final = re.compile(r"[a-z0-9]{1,32}")
_REDDIT_POST_SLUG: Final = re.compile(r"[A-Za-z0-9_-]{1,256}")
_TWITTER_POST_ID: Final = re.compile(r"[1-9][0-9]{0,31}")
_XIAOHONGSHU_NOTE_ID: Final = re.compile(r"[0-9a-f]{24}")


@dataclass(frozen=True, slots=True)
class _OperationContract:
    schema: str
    maximum_items: int


_OPERATION_CONTRACTS: Final = MappingProxyType(
    {
        ("reddit", "search.posts"): _OperationContract("reddit.post.v1", 20),
        ("reddit", "read.post"): _OperationContract("reddit.thread.item.v1", 14),
        ("reddit", "browse.subreddit"): _OperationContract("reddit.post.v1", 20),
        ("reddit", "browse.hot"): _OperationContract("reddit.post.v1", 20),
        ("reddit", "browse.popular"): _OperationContract("reddit.post.v1", 20),
        ("reddit", "browse.all"): _OperationContract("reddit.post.v1", 20),
        ("reddit", "read.subreddit"): _OperationContract("reddit.subreddit.v1", 1),
        ("facebook", "search"): _OperationContract("facebook.search.result.v1", 20),
        ("facebook", "read.profile"): _OperationContract("facebook.profile.v1", 1),
        ("facebook", "browse.feed"): _OperationContract("facebook.post.v1", 20),
        ("facebook", "browse.groups"): _OperationContract("facebook.group.v1", 20),
        ("instagram", "search.users"): _OperationContract("instagram.user.v1", 20),
        ("instagram", "read.profile"): _OperationContract("instagram.profile.v1", 1),
        ("instagram", "browse.user_posts"): _OperationContract("instagram.post.v1", 20),
        ("instagram", "browse.explore"): _OperationContract("instagram.post.v1", 20),
        ("twitter", "search.posts"): _OperationContract("twitter.post.v1", 20),
        ("xiaohongshu", "search.notes"): _OperationContract("xiaohongshu.note.v1", 20),
    }
)

_NATIVE_FIELDS: Final = MappingProxyType(
    {
        "reddit.post.v1": frozenset(
            {
                "author",
                "comment_count",
                "media_type",
                "native_id",
                "published_at",
                "score",
                "subreddit",
                "text",
                "title",
                "url",
            }
        ),
        "reddit.thread.item.v1": frozenset(
            {
                "author",
                "kind",
                "media_type",
                "native_id",
                "score",
                "text",
                "title",
                "url",
            }
        ),
        "reddit.subreddit.v1": frozenset(
            {
                "active_count",
                "native_id",
                "nsfw",
                "published_at",
                "subscriber_count",
                "subreddit_type",
                "text",
                "title",
                "url",
            }
        ),
        "facebook.search.result.v1": frozenset({"native_id", "text", "title", "url"}),
        "facebook.profile.v1": frozenset(
            {"follower_count", "friend_count", "native_id", "text", "title", "url"}
        ),
        "facebook.post.v1": frozenset(
            {
                "author",
                "comment_count",
                "native_id",
                "reaction_count",
                "share_count",
                "text",
            }
        ),
        "facebook.group.v1": frozenset({"native_id", "text", "title", "url"}),
        "instagram.user.v1": frozenset(
            {"native_id", "private", "title", "url", "verified"}
        ),
        "instagram.profile.v1": frozenset(
            {
                "follower_count",
                "following_count",
                "native_id",
                "post_count",
                "text",
                "title",
                "url",
                "verified",
            }
        ),
        "instagram.post.v1": frozenset(
            {
                "author",
                "comment_count",
                "media_type",
                "native_id",
                "published_at",
                "reaction_count",
                "text",
            }
        ),
        "twitter.post.v1": frozenset(
            {
                "author",
                "has_media",
                "native_id",
                "published_at",
                "reaction_count",
                "text",
                "url",
                "view_count",
            }
        ),
        "xiaohongshu.note.v1": frozenset(
            {
                "author",
                "native_id",
                "published_at",
                "reaction_count",
                "text",
                "title",
                "url",
            }
        ),
    }
)


class OpenCliSocialProtocolError(ValueError):
    """The closed worker input, output, or fork contract was violated."""


class _DeadlineExpired(Exception):
    pass


@dataclass(frozen=True, slots=True, repr=False)
class WorkerSession:
    node_executable: str
    node_sha256: str
    opencli_root: str
    opencli_cli: str
    opencli_tree_sha256: str
    session_home: str

    def as_fields(self) -> dict[str, str]:
        return {
            "node_executable": self.node_executable,
            "node_sha256": self.node_sha256,
            "opencli_root": self.opencli_root,
            "opencli_cli": self.opencli_cli,
            "opencli_tree_sha256": self.opencli_tree_sha256,
            "session_home": self.session_home,
        }

    def __repr__(self) -> str:
        return "WorkerSession(<redacted>)"


@dataclass(frozen=True, slots=True, repr=False)
class WorkerRequest:
    source: WorkerSource
    operation: WorkerOperation
    arguments: Mapping[str, ArgumentValue]
    session: WorkerSession
    deadline: float

    def __repr__(self) -> str:
        return (
            "WorkerRequest("
            f"source={self.source!r}, operation={self.operation!r}, payload=<redacted>)"
        )


@dataclass(frozen=True, slots=True, repr=False)
class SocialItemProjection:
    kind: ResultKind
    text: str
    native_id: str | None = None
    title: str | None = None
    url: str | None = None
    author: str | None = None
    published_at: str | None = None

    def __repr__(self) -> str:
        return "SocialItemProjection(<redacted>)"


@dataclass(frozen=True, slots=True, repr=False)
class OpenCliSocialProjection:
    source: WorkerSource
    operation: WorkerOperation
    items: tuple[SocialItemProjection, ...]
    truncated: bool

    def __repr__(self) -> str:
        return (
            "OpenCliSocialProjection("
            f"source={self.source!r}, operation={self.operation!r}, result=<redacted>)"
        )


@dataclass(frozen=True, slots=True)
class ForkExecutionFailure:
    source: WorkerSource
    operation: WorkerOperation
    error_code: WorkerErrorCode


WorkerResponse: TypeAlias = OpenCliSocialProjection | ForkExecutionFailure


class _ExecutionItem(Protocol):
    schema_id: object
    fields: object


class _ExecutionSuccess(Protocol):
    protocol_version: object
    source: object
    operation: object
    backend_id: object
    backend_version: object
    items: object
    truncated: object
    partial_error_code: object


class _ExecutionFailure(Protocol):
    protocol_version: object
    source: object
    operation: object
    backend_id: object
    backend_version: object
    error_code: object


ExecutionApiProvider = Callable[[], AgentReachExecutionApi]


def _load_execution_api() -> AgentReachExecutionApi:
    return validate_agent_reach_execution_contract(runtime_module="opencli_social")


def encode_request(
    source: str,
    operation: str,
    arguments: Mapping[str, object],
    session: Mapping[str, str],
    *,
    deadline: float,
) -> bytes:
    """Encode one of the 17 fixed social requests for the isolated worker."""

    request = _validated_request(
        {
            "arguments": dict(arguments),
            "deadline": deadline,
            "operation": operation,
            "protocol": PROTOCOL_VERSION,
            "session": dict(session),
            "source": source,
        }
    )
    return _encode_frame(_request_value(request), MAX_REQUEST_BYTES)


def decode_response(
    raw: bytes | bytearray,
    *,
    source: str,
    operation: str,
    arguments: Mapping[str, object],
) -> WorkerResponse:
    """Independently validate a complete worker response and its correlation."""

    key = _operation_key(source, operation)
    validated_arguments = _validated_arguments(key, arguments)
    contract = _OPERATION_CONTRACTS[key]
    maximum_items = _requested_item_limit(key, validated_arguments, contract)
    value = _decode_frame(raw, MAX_OUTPUT_BYTES)
    if not isinstance(value, dict):
        raise OpenCliSocialProtocolError("worker_response_invalid")
    if set(value) == _SUCCESS_FIELDS:
        return _decode_success(
            value,
            key=key,
            arguments=validated_arguments,
            schema=contract.schema,
            maximum_items=maximum_items,
        )
    if set(value) == _FAILURE_FIELDS:
        return _decode_failure(value, key=key)
    raise OpenCliSocialProtocolError("worker_response_invalid")


def _read_request(stream: BinaryIO) -> WorkerRequest:
    header = stream.read(_LENGTH_BYTES)
    if len(header) != _LENGTH_BYTES:
        raise OpenCliSocialProtocolError("worker_request_invalid")
    length = int.from_bytes(header, "big")
    if not 0 < length <= MAX_REQUEST_BYTES:
        raise OpenCliSocialProtocolError("worker_request_invalid")
    payload = stream.read(length)
    if len(payload) != length or stream.read(1):
        raise OpenCliSocialProtocolError("worker_request_invalid")
    return _validated_request(_load_json(payload, maximum=MAX_REQUEST_BYTES))


def _validated_request(value: object) -> WorkerRequest:
    if not isinstance(value, dict) or set(value) != _REQUEST_FIELDS:
        raise OpenCliSocialProtocolError("worker_request_invalid")
    if value.get("protocol") != PROTOCOL_VERSION:
        raise OpenCliSocialProtocolError("worker_request_invalid")
    key = _operation_key(value.get("source"), value.get("operation"))
    arguments = _validated_arguments(key, value.get("arguments"))
    session = _validated_session(value.get("session"))
    deadline_value = value.get("deadline")
    if type(deadline_value) not in {int, float}:
        raise OpenCliSocialProtocolError("worker_request_invalid")
    deadline = cast(int | float, deadline_value)
    if not math.isfinite(deadline) or deadline <= 0:
        raise OpenCliSocialProtocolError("worker_request_invalid")
    return WorkerRequest(
        cast(WorkerSource, key[0]),
        cast(WorkerOperation, key[1]),
        MappingProxyType(arguments),
        session,
        float(deadline),
    )


def _validated_session(value: object) -> WorkerSession:
    if not isinstance(value, dict) or set(value) != _SESSION_FIELDS:
        raise OpenCliSocialProtocolError("worker_request_invalid")
    if any(
        type(name) is not str or type(item) is not str for name, item in value.items()
    ):
        raise OpenCliSocialProtocolError("worker_request_invalid")
    fields = cast(Mapping[str, str], value)
    paths = (
        fields["node_executable"],
        fields["opencli_root"],
        fields["opencli_cli"],
        fields["session_home"],
    )
    if (
        any(not _valid_absolute_path(path) for path in paths)
        or _SHA256.fullmatch(fields["node_sha256"]) is None
        or _SHA256.fullmatch(fields["opencli_tree_sha256"]) is None
        or fields["opencli_cli"] == fields["opencli_root"]
        or not _path_is_below(fields["opencli_cli"], fields["opencli_root"])
    ):
        raise OpenCliSocialProtocolError("worker_request_invalid")
    return WorkerSession(**{name: fields[name] for name in _SESSION_FIELDS})


def _operation_key(source: object, operation: object) -> tuple[str, str]:
    if type(source) is not str or type(operation) is not str:
        raise OpenCliSocialProtocolError("worker_request_invalid")
    key = (source, operation)
    if key not in _OPERATION_CONTRACTS:
        raise OpenCliSocialProtocolError("worker_request_invalid")
    return key


def _validated_arguments(
    key: tuple[str, str], value: object
) -> dict[str, ArgumentValue]:
    if not isinstance(value, Mapping) or any(type(name) is not str for name in value):
        raise OpenCliSocialProtocolError("worker_request_invalid")
    arguments = dict(value)
    if key in {
        ("reddit", "search.posts"),
        ("facebook", "search"),
        ("instagram", "search.users"),
        ("twitter", "search.posts"),
        ("xiaohongshu", "search.notes"),
    }:
        if set(arguments) != {"query", "limit"}:
            raise OpenCliSocialProtocolError("worker_request_invalid")
        return {
            "query": _bounded_query(arguments["query"]),
            "limit": _bounded_limit(arguments["limit"]),
        }
    if key == ("reddit", "read.post"):
        if set(arguments) != {"url"} or _reddit_post_id(arguments.get("url")) is None:
            raise OpenCliSocialProtocolError("worker_request_invalid")
        return {"url": cast(str, arguments["url"])}
    if key == ("reddit", "browse.subreddit"):
        if set(arguments) != {"subreddit", "limit"}:
            raise OpenCliSocialProtocolError("worker_request_invalid")
        return {
            "subreddit": _bounded_subreddit(arguments["subreddit"]),
            "limit": _bounded_limit(arguments["limit"]),
        }
    if key == ("reddit", "read.subreddit"):
        if set(arguments) != {"subreddit"}:
            raise OpenCliSocialProtocolError("worker_request_invalid")
        return {"subreddit": _bounded_subreddit(arguments["subreddit"])}
    if key in {
        ("reddit", "browse.hot"),
        ("reddit", "browse.popular"),
        ("reddit", "browse.all"),
        ("facebook", "browse.feed"),
        ("facebook", "browse.groups"),
        ("instagram", "browse.explore"),
    }:
        if set(arguments) != {"limit"}:
            raise OpenCliSocialProtocolError("worker_request_invalid")
        return {"limit": _bounded_limit(arguments["limit"])}
    if key in {
        ("facebook", "read.profile"),
        ("instagram", "read.profile"),
    }:
        if set(arguments) != {"username"}:
            raise OpenCliSocialProtocolError("worker_request_invalid")
        return {"username": _bounded_username(arguments["username"])}
    if key == ("instagram", "browse.user_posts"):
        if set(arguments) != {"username", "limit"}:
            raise OpenCliSocialProtocolError("worker_request_invalid")
        return {
            "username": _bounded_username(arguments["username"]),
            "limit": _bounded_limit(arguments["limit"]),
        }
    raise OpenCliSocialProtocolError("worker_request_invalid")


def _request_value(request: WorkerRequest) -> dict[str, object]:
    return {
        "arguments": dict(request.arguments),
        "deadline": request.deadline,
        "operation": request.operation,
        "protocol": PROTOCOL_VERSION,
        "session": request.session.as_fields(),
        "source": request.source,
    }


def _execute_request(
    request: WorkerRequest,
    *,
    execution_api_provider: ExecutionApiProvider | None = None,
) -> Mapping[str, object]:
    key = (request.source, request.operation)
    provider = execution_api_provider or _load_execution_api
    try:
        _worker_checkpoint(request.deadline)
        api = provider()
        _worker_checkpoint(request.deadline)
    except _WorkerCancellationRequested:
        return _failure_value(key, "cancelled")
    except _DeadlineExpired:
        return _failure_value(key, "deadline_exceeded")
    except Exception:
        return _failure_value(key, "backend_contract_violation")

    try:
        request_factory = cast(Callable[..., object], api.execution_request_type)
        limits_factory = cast(Callable[..., object], api.execution_limits_type)
        context_factory = cast(Callable[..., object], api.execution_context_type)
        session_type = getattr(api, "opencli_session_type", None)
        if not isinstance(session_type, type):
            return _failure_value(key, "backend_contract_violation")
        session_factory = cast(Callable[..., object], session_type)
        contract = _OPERATION_CONTRACTS[key]
        maximum_items = _requested_item_limit(key, request.arguments, contract)
        execution_request = request_factory(
            PROTOCOL_VERSION,
            request.source,
            request.operation,
            dict(request.arguments),
        )
        limits = limits_factory(
            maximum_items=maximum_items,
            maximum_text_characters=MAX_TEXT_CHARACTERS,
        )

        def checkpoint() -> None:
            _worker_checkpoint(request.deadline)

        context = context_factory(
            (session_factory(**request.session.as_fields()),),
            checkpoint=checkpoint,
            limits=limits,
        )
        result = api.execute(execution_request, context)
        _worker_checkpoint(request.deadline)
        if type(result) is api.execution_success_type:
            return _success_value(
                cast(_ExecutionSuccess, result),
                api=api,
                request=request,
                schema=contract.schema,
                maximum_items=maximum_items,
            )
        if type(result) is api.execution_failure_type:
            failure = cast(_ExecutionFailure, result)
            if _valid_execution_identity(failure, key):
                code = failure.error_code
                if type(code) is str and code in _ERROR_CODES:
                    return _failure_value(key, cast(WorkerErrorCode, code))
    except _WorkerCancellationRequested:
        return _failure_value(key, "cancelled")
    except _DeadlineExpired:
        return _failure_value(key, "deadline_exceeded")
    except Exception:
        return _failure_value(key, "backend_contract_violation")
    return _failure_value(key, "backend_contract_violation")


def _success_value(
    success: _ExecutionSuccess,
    *,
    api: AgentReachExecutionApi,
    request: WorkerRequest,
    schema: str,
    maximum_items: int,
) -> Mapping[str, object]:
    key = (request.source, request.operation)
    items = success.items
    if (
        not _valid_execution_identity(success, key)
        or success.partial_error_code is not None
        or type(success.truncated) is not bool
        or type(items) is not tuple
        or len(items) > maximum_items
    ):
        return _failure_value(key, "backend_contract_violation")
    projected: list[SocialItemProjection] = []
    try:
        for index, raw_item in enumerate(items):
            if type(raw_item) is not api.execution_item_type:
                raise OpenCliSocialProtocolError("fork_result_invalid")
            item = cast(_ExecutionItem, raw_item)
            if item.schema_id != schema:
                raise OpenCliSocialProtocolError("fork_result_invalid")
            projected.append(
                _project_native_item(
                    schema,
                    item.fields,
                    request=request,
                    index=index,
                )
            )
        _validate_projection_sequence(request, tuple(projected))
        selected, normalized_truncated = _fit_result_budget(
            tuple(projected), key=key, arguments=request.arguments
        )
        _validate_parent_projection(key, request.arguments, selected)
    except OpenCliSocialProtocolError:
        return _failure_value(key, "backend_contract_violation")
    return {
        "backend": _backend_value(),
        "items": [_projected_item_value(item) for item in selected],
        "operation": request.operation,
        "protocol": PROTOCOL_VERSION,
        "schema": schema,
        "source": request.source,
        "truncated": bool(success.truncated or normalized_truncated),
    }


def _valid_execution_identity(
    value: _ExecutionSuccess | _ExecutionFailure,
    key: tuple[str, str],
) -> bool:
    return bool(
        value.protocol_version == PROTOCOL_VERSION
        and value.source == key[0]
        and value.operation == key[1]
        and value.backend_id == EXPECTED_BACKEND_ID
        and value.backend_version == EXPECTED_BACKEND_VERSION
    )


def _project_native_item(
    schema: str,
    value: object,
    *,
    request: WorkerRequest,
    index: int,
) -> SocialItemProjection:
    fields = _closed_native_fields(schema, value)
    if schema == "reddit.post.v1":
        native_id = _required_text(fields["native_id"], MAX_NATIVE_ID_CHARACTERS)
        url = _required_url(fields["url"], host="reddit.com")
        subreddit = _optional_text(fields["subreddit"], 64)
        url_subreddit = _reddit_post_subreddit(url)
        if (
            _REDDIT_POST_ID.fullmatch(native_id) is None
            or _reddit_post_id(url) != native_id
            or url_subreddit is None
            or (
                subreddit is not None
                and subreddit.casefold() != url_subreddit.casefold()
            )
            or (
                request.operation == "browse.subreddit"
                and (
                    subreddit is None
                    or subreddit.casefold()
                    != cast(str, request.arguments["subreddit"]).casefold()
                )
            )
        ):
            raise OpenCliSocialProtocolError("fork_result_invalid")
        return _public_projection(
            "entry",
            _rich_text(
                _optional_text(fields["text"], MAX_TEXT_CHARACTERS),
                ("score", _optional_integer(fields["score"])),
                ("comments", _optional_integer(fields["comment_count"])),
                ("subreddit", subreddit),
                ("media", _optional_text(fields["media_type"], 64)),
            ),
            native_id=native_id,
            title=_required_text(fields["title"], MAX_TITLE_CHARACTERS),
            url=url,
            author=_optional_text(fields["author"], MAX_AUTHOR_CHARACTERS),
            published_at=_optional_text(
                fields["published_at"], MAX_PUBLISHED_CHARACTERS
            ),
        )
    if schema == "reddit.thread.item.v1":
        native_kind = _required_text(fields["kind"], 16)
        expected_kind = "post" if index == 0 else "comment"
        if native_kind != expected_kind:
            raise OpenCliSocialProtocolError("fork_result_invalid")
        thread_native_id = _optional_text(fields["native_id"], MAX_NATIVE_ID_CHARACTERS)
        thread_title = _optional_text(fields["title"], MAX_TITLE_CHARACTERS)
        thread_url = _optional_url(fields["url"], host="reddit.com")
        if index == 0:
            requested_id = _reddit_post_id(request.arguments["url"])
            if (
                requested_id is None
                or thread_native_id != requested_id
                or thread_url is None
                or _reddit_post_id(thread_url) != thread_native_id
            ):
                raise OpenCliSocialProtocolError("fork_result_invalid")
        elif (
            thread_native_id is not None
            or thread_title is not None
            or thread_url is not None
        ):
            raise OpenCliSocialProtocolError("fork_result_invalid")
        return _public_projection(
            "content" if index == 0 else "reply",
            _rich_text(
                _required_text(fields["text"], MAX_TEXT_CHARACTERS),
                ("score", _optional_integer(fields["score"])),
                ("media", _optional_text(fields["media_type"], 64)),
            ),
            native_id=thread_native_id,
            title=thread_title,
            url=thread_url,
            author=_optional_text(fields["author"], MAX_AUTHOR_CHARACTERS),
        )
    if schema == "reddit.subreddit.v1":
        native_id = _required_text(fields["native_id"], 64)
        requested = cast(str, request.arguments["subreddit"])
        url = _required_url(fields["url"], host="reddit.com")
        if (
            _SUBREDDIT.fullmatch(native_id) is None
            or native_id.casefold() != requested.casefold()
            or url != f"https://www.reddit.com/r/{native_id}/"
        ):
            raise OpenCliSocialProtocolError("fork_result_invalid")
        nsfw = _binary_integer(fields["nsfw"])
        return _public_projection(
            "profile",
            _rich_text(
                _optional_text(fields["text"], MAX_TEXT_CHARACTERS),
                ("subscribers", _optional_integer(fields["subscriber_count"])),
                ("active", _optional_integer(fields["active_count"])),
                ("nsfw", "yes" if nsfw else "no"),
                ("type", _optional_text(fields["subreddit_type"], 64)),
            ),
            native_id=native_id,
            title=_required_text(fields["title"], MAX_TITLE_CHARACTERS),
            url=url,
            published_at=_optional_text(
                fields["published_at"], MAX_PUBLISHED_CHARACTERS
            ),
        )
    if schema in {"facebook.search.result.v1", "facebook.group.v1"}:
        native_id = _ordered_identifier(fields["native_id"], index)
        return _public_projection(
            "result",
            _optional_text(fields["text"], MAX_TEXT_CHARACTERS) or "",
            native_id=native_id,
            title=_required_text(fields["title"], MAX_TITLE_CHARACTERS),
            url=_required_url(fields["url"], host="facebook.com"),
        )
    if schema == "facebook.profile.v1":
        native_id = _required_text(fields["native_id"], MAX_NATIVE_ID_CHARACTERS)
        requested = cast(str, request.arguments["username"])
        url = _required_url(fields["url"], host="facebook.com")
        if (
            not _valid_username(native_id)
            or native_id.casefold() != requested.casefold()
            or not _url_path_matches_username(url, native_id)
        ):
            raise OpenCliSocialProtocolError("fork_result_invalid")
        return _public_projection(
            "profile",
            _rich_text(
                _optional_text(fields["text"], MAX_TEXT_CHARACTERS),
                ("friends", _optional_integer(fields["friend_count"])),
                ("followers", _optional_integer(fields["follower_count"])),
            ),
            native_id=native_id,
            title=_required_text(fields["title"], MAX_TITLE_CHARACTERS),
            url=url,
        )
    if schema == "facebook.post.v1":
        return _public_projection(
            "entry",
            _rich_text(
                _required_text(fields["text"], MAX_TEXT_CHARACTERS),
                ("reactions", _optional_integer(fields["reaction_count"])),
                ("comments", _optional_integer(fields["comment_count"])),
                ("shares", _optional_integer(fields["share_count"])),
            ),
            native_id=_ordered_identifier(fields["native_id"], index),
            author=_optional_text(fields["author"], MAX_AUTHOR_CHARACTERS),
        )
    if schema == "instagram.user.v1":
        native_id = _required_text(fields["native_id"], MAX_NATIVE_ID_CHARACTERS)
        url = _required_url(fields["url"], host="instagram.com")
        if not _valid_username(native_id) or not _url_path_matches_username(
            url, native_id
        ):
            raise OpenCliSocialProtocolError("fork_result_invalid")
        return _public_projection(
            "profile",
            _rich_text(
                None,
                ("verified", "yes" if _binary_integer(fields["verified"]) else "no"),
                ("private", "yes" if _binary_integer(fields["private"]) else "no"),
            ),
            native_id=native_id,
            title=_required_text(fields["title"], MAX_TITLE_CHARACTERS),
            url=url,
        )
    if schema == "instagram.profile.v1":
        native_id = _required_text(fields["native_id"], MAX_NATIVE_ID_CHARACTERS)
        requested = cast(str, request.arguments["username"])
        url = _required_url(fields["url"], host="instagram.com")
        if (
            not _valid_username(native_id)
            or native_id.casefold() != requested.casefold()
            or not _url_path_matches_username(url, native_id)
        ):
            raise OpenCliSocialProtocolError("fork_result_invalid")
        return _public_projection(
            "profile",
            _rich_text(
                _optional_text(fields["text"], MAX_TEXT_CHARACTERS),
                ("followers", _optional_integer(fields["follower_count"])),
                ("following", _optional_integer(fields["following_count"])),
                ("posts", _optional_integer(fields["post_count"])),
                ("verified", "yes" if _binary_integer(fields["verified"]) else "no"),
            ),
            native_id=native_id,
            title=_required_text(fields["title"], MAX_TITLE_CHARACTERS),
            url=url,
        )
    if schema == "instagram.post.v1":
        author = _optional_text(fields["author"], MAX_AUTHOR_CHARACTERS)
        if author is not None and not _valid_username(author):
            raise OpenCliSocialProtocolError("fork_result_invalid")
        if request.operation == "browse.user_posts":
            requested = cast(str, request.arguments["username"])
            if author is None or author.casefold() != requested.casefold():
                raise OpenCliSocialProtocolError("fork_result_invalid")
        return _public_projection(
            "entry",
            _rich_text(
                _optional_text(fields["text"], MAX_TEXT_CHARACTERS),
                ("reactions", _optional_integer(fields["reaction_count"])),
                ("comments", _optional_integer(fields["comment_count"])),
                ("media", _optional_text(fields["media_type"], 64)),
            ),
            native_id=_ordered_identifier(fields["native_id"], index),
            author=author,
            published_at=_optional_text(
                fields["published_at"], MAX_PUBLISHED_CHARACTERS
            ),
        )
    if schema == "twitter.post.v1":
        native_id = _required_text(fields["native_id"], MAX_NATIVE_ID_CHARACTERS)
        url = _required_url(fields["url"], host="x.com")
        if _TWITTER_POST_ID.fullmatch(native_id) is None or url != (
            f"https://x.com/i/status/{native_id}"
        ):
            raise OpenCliSocialProtocolError("fork_result_invalid")
        has_media = _binary_integer(fields["has_media"])
        return _public_projection(
            "entry",
            _rich_text(
                _optional_text(fields["text"], MAX_TEXT_CHARACTERS),
                ("reactions", _optional_integer(fields["reaction_count"])),
                ("views", _optional_integer(fields["view_count"])),
                ("media", "yes" if has_media else "no"),
            ),
            native_id=native_id,
            url=url,
            author=_optional_text(fields["author"], MAX_AUTHOR_CHARACTERS),
            published_at=_optional_text(
                fields["published_at"], MAX_PUBLISHED_CHARACTERS
            ),
        )
    if schema == "xiaohongshu.note.v1":
        native_id = _required_text(fields["native_id"], MAX_NATIVE_ID_CHARACTERS)
        text = _required_text(fields["text"], MAX_TEXT_CHARACTERS)
        title = _required_text(fields["title"], MAX_TITLE_CHARACTERS)
        url = _required_url(fields["url"], host="xiaohongshu.com")
        if (
            _XIAOHONGSHU_NOTE_ID.fullmatch(native_id) is None
            or text != title
            or url != f"https://www.xiaohongshu.com/explore/{native_id}"
        ):
            raise OpenCliSocialProtocolError("fork_result_invalid")
        return _public_projection(
            "entry",
            _rich_text(
                text,
                ("reactions", _optional_integer(fields["reaction_count"])),
            ),
            native_id=native_id,
            title=title,
            url=url,
            author=_optional_text(fields["author"], MAX_AUTHOR_CHARACTERS),
            published_at=_optional_text(
                fields["published_at"], MAX_PUBLISHED_CHARACTERS
            ),
        )
    raise OpenCliSocialProtocolError("fork_result_invalid")


def _closed_native_fields(schema: str, value: object) -> Mapping[str, object]:
    expected = _NATIVE_FIELDS.get(schema)
    if expected is None or not isinstance(value, Mapping) or set(value) != expected:
        raise OpenCliSocialProtocolError("fork_result_invalid")
    if any(type(name) is not str for name in value):
        raise OpenCliSocialProtocolError("fork_result_invalid")
    return cast(Mapping[str, object], value)


def _validate_projection_sequence(
    request: WorkerRequest, items: tuple[SocialItemProjection, ...]
) -> None:
    if request.operation == "read.post":
        if (
            not items
            or items[0].kind != "content"
            or any(item.kind != "reply" for item in items[1:])
        ):
            raise OpenCliSocialProtocolError("fork_result_invalid")
    if request.operation in {"read.subreddit", "read.profile"} and len(items) != 1:
        raise OpenCliSocialProtocolError("fork_result_invalid")
    identities = [
        item.native_id.casefold() for item in items if item.native_id is not None
    ]
    if request.operation != "read.post" and len(identities) != len(set(identities)):
        raise OpenCliSocialProtocolError("fork_result_invalid")


def _public_projection(
    kind: ResultKind,
    text: str,
    *,
    native_id: str | None = None,
    title: str | None = None,
    url: str | None = None,
    author: str | None = None,
    published_at: str | None = None,
) -> SocialItemProjection:
    return SocialItemProjection(
        kind,
        _normalized_public_text(text),
        native_id=native_id,
        title=None if title is None else _normalized_public_text(title),
        url=url,
        author=None if author is None else _normalized_public_text(author),
        published_at=(
            None if published_at is None else _normalized_public_text(published_at)
        ),
    )


def _fit_result_budget(
    items: tuple[SocialItemProjection, ...],
    *,
    key: tuple[str, str],
    arguments: Mapping[str, object],
) -> tuple[tuple[SocialItemProjection, ...], bool]:
    selected: list[SocialItemProjection] = []
    remaining = MAX_RESULT_CHARACTERS
    truncated = False
    for original in items:
        item, scalar_truncated = _bound_public_scalars(original)
        truncated = truncated or scalar_truncated
        for field in ("published_at", "author", "url", "title"):
            if _projected_item_characters(replace(item, text="")) <= remaining:
                break
            if getattr(item, field) is not None:
                item = _without_projection_field(item, field)
                truncated = True
        base = _projected_item_characters(replace(item, text=""))
        if base > remaining:
            truncated = True
            break
        allowed = remaining - base
        if len(item.text) > allowed:
            item = replace(item, text=item.text[:allowed])
            truncated = True
        try:
            _validate_parent_projection(key, arguments, (*selected, item))
        except OpenCliSocialProtocolError:
            truncated = True
            break
        selected.append(item)
        remaining -= _projected_item_characters(item)
    if len(selected) != len(items):
        truncated = True
    return tuple(selected), truncated


def _bound_public_scalars(
    item: SocialItemProjection,
) -> tuple[SocialItemProjection, bool]:
    bounded = replace(
        item,
        title=(None if item.title is None else item.title[:_PUBLIC_TITLE_CHARACTERS]),
        url=(
            item.url
            if item.url is None or len(item.url) <= _PUBLIC_URL_CHARACTERS
            else None
        ),
        author=(
            None if item.author is None else item.author[:_PUBLIC_AUTHOR_CHARACTERS]
        ),
        published_at=(
            None
            if item.published_at is None
            else item.published_at[:_PUBLIC_PUBLISHED_CHARACTERS]
        ),
    )
    return bounded, bounded != item


def _projected_item_characters(item: SocialItemProjection) -> int:
    return normalized_item_characters(
        kind=item.kind,
        text=item.text,
        native_id=item.native_id,
        title=item.title,
        url=item.url,
        author=item.author,
        published_at=item.published_at,
        media_characters=0,
    )


def _without_projection_field(
    item: SocialItemProjection, field: str
) -> SocialItemProjection:
    if field == "published_at":
        return replace(item, published_at=None)
    if field == "author":
        return replace(item, author=None)
    if field == "url":
        return replace(item, url=None)
    if field == "title":
        return replace(item, title=None)
    raise OpenCliSocialProtocolError("worker_response_invalid")


def _rich_text(base: str | None, *metadata: tuple[str, object | None]) -> str:
    parts = [] if base is None or not base else [_normalized_public_text(base)]
    for label, value in metadata:
        if value is not None:
            parts.append(f"{label}: {value}")
    return " | ".join(parts)


def _normalized_public_text(value: str) -> str:
    return " ".join(value.split())


def _failure_value(
    key: tuple[str, str], error_code: WorkerErrorCode
) -> dict[str, object]:
    return {
        "backend": _backend_value(),
        "error": {"code": error_code},
        "operation": key[1],
        "protocol": PROTOCOL_VERSION,
        "source": key[0],
    }


def _backend_value() -> dict[str, str]:
    return {"id": EXPECTED_BACKEND_ID, "version": EXPECTED_BACKEND_VERSION}


def _decode_success(
    value: Mapping[str, object],
    *,
    key: tuple[str, str],
    arguments: Mapping[str, object],
    schema: str,
    maximum_items: int,
) -> OpenCliSocialProjection:
    _validate_response_identity(value, key)
    _decode_backend(value["backend"])
    items = value["items"]
    truncated = value["truncated"]
    if (
        value["schema"] != schema
        or type(truncated) is not bool
        or not isinstance(items, list)
        or len(items) > maximum_items
    ):
        raise OpenCliSocialProtocolError("worker_response_invalid")
    projected = tuple(_decode_projected_item(item) for item in items)
    _validate_parent_projection(key, arguments, projected)
    if (
        sum(_projected_item_characters(item) for item in projected)
        > MAX_RESULT_CHARACTERS
    ):
        raise OpenCliSocialProtocolError("worker_response_invalid")
    return OpenCliSocialProjection(
        cast(WorkerSource, key[0]),
        cast(WorkerOperation, key[1]),
        projected,
        truncated,
    )


def _validate_parent_projection(
    key: tuple[str, str],
    arguments: Mapping[str, object],
    items: tuple[SocialItemProjection, ...],
) -> None:
    source, operation = key
    if operation == "read.post":
        if not items:
            raise OpenCliSocialProtocolError("worker_response_invalid")
        requested_id = _reddit_post_id(arguments["url"])
        first = items[0]
        if (
            requested_id is None
            or first.kind != "content"
            or first.native_id != requested_id
            or first.title is None
            or first.url is None
            or _reddit_post_id(first.url) != requested_id
            or first.published_at is not None
            or any(
                item.kind != "reply"
                or item.native_id is not None
                or item.title is not None
                or item.url is not None
                or item.published_at is not None
                for item in items[1:]
            )
        ):
            raise OpenCliSocialProtocolError("worker_response_invalid")
        return

    expected_kind = _expected_projection_kind(key)
    if any(item.kind != expected_kind for item in items):
        raise OpenCliSocialProtocolError("worker_response_invalid")
    identities = [
        item.native_id.casefold() for item in items if item.native_id is not None
    ]
    if len(identities) != len(items) or len(identities) != len(set(identities)):
        raise OpenCliSocialProtocolError("worker_response_invalid")

    if source == "reddit":
        _validate_parent_reddit_projection(operation, arguments, items)
    elif source == "facebook":
        _validate_parent_facebook_projection(operation, arguments, items)
    elif source == "instagram":
        _validate_parent_instagram_projection(operation, arguments, items)
    elif source == "twitter":
        _validate_parent_twitter_projection(items)
    elif source == "xiaohongshu":
        _validate_parent_xiaohongshu_projection(items)
    else:
        raise OpenCliSocialProtocolError("worker_response_invalid")


def _expected_projection_kind(key: tuple[str, str]) -> ResultKind:
    kinds: dict[tuple[str, str], ResultKind] = {
        ("reddit", "search.posts"): "entry",
        ("reddit", "browse.subreddit"): "entry",
        ("reddit", "browse.hot"): "entry",
        ("reddit", "browse.popular"): "entry",
        ("reddit", "browse.all"): "entry",
        ("reddit", "read.subreddit"): "profile",
        ("facebook", "search"): "result",
        ("facebook", "read.profile"): "profile",
        ("facebook", "browse.feed"): "entry",
        ("facebook", "browse.groups"): "result",
        ("instagram", "search.users"): "profile",
        ("instagram", "read.profile"): "profile",
        ("instagram", "browse.user_posts"): "entry",
        ("instagram", "browse.explore"): "entry",
        ("twitter", "search.posts"): "entry",
        ("xiaohongshu", "search.notes"): "entry",
    }
    try:
        return kinds[key]
    except KeyError:
        raise OpenCliSocialProtocolError("worker_response_invalid") from None


def _validate_parent_reddit_projection(
    operation: str,
    arguments: Mapping[str, object],
    items: tuple[SocialItemProjection, ...],
) -> None:
    if operation == "read.subreddit":
        if len(items) != 1:
            raise OpenCliSocialProtocolError("worker_response_invalid")
        item = items[0]
        requested = cast(str, arguments["subreddit"])
        if (
            item.native_id is None
            or item.native_id.casefold() != requested.casefold()
            or item.title is None
            or item.url != f"https://www.reddit.com/r/{item.native_id}/"
            or item.author is not None
        ):
            raise OpenCliSocialProtocolError("worker_response_invalid")
        return

    requested_subreddit = (
        cast(str, arguments["subreddit"]) if operation == "browse.subreddit" else None
    )
    for item in items:
        if (
            item.native_id is None
            or _REDDIT_POST_ID.fullmatch(item.native_id) is None
            or item.title is None
            or item.url is None
            or _reddit_post_id(item.url) != item.native_id
        ):
            raise OpenCliSocialProtocolError("worker_response_invalid")
        actual_subreddit = _reddit_post_subreddit(item.url)
        if actual_subreddit is None or (
            requested_subreddit is not None
            and actual_subreddit.casefold() != requested_subreddit.casefold()
        ):
            raise OpenCliSocialProtocolError("worker_response_invalid")


def _validate_parent_facebook_projection(
    operation: str,
    arguments: Mapping[str, object],
    items: tuple[SocialItemProjection, ...],
) -> None:
    if operation == "read.profile":
        if len(items) != 1:
            raise OpenCliSocialProtocolError("worker_response_invalid")
        item = items[0]
        requested = cast(str, arguments["username"])
        if (
            item.native_id is None
            or item.native_id.casefold() != requested.casefold()
            or item.title is None
            or item.url is None
            or _required_url(item.url, host="facebook.com") != item.url
            or not _url_path_matches_username(item.url, item.native_id)
            or item.author is not None
            or item.published_at is not None
        ):
            raise OpenCliSocialProtocolError("worker_response_invalid")
        return

    for index, item in enumerate(items):
        if item.native_id != str(index + 1):
            raise OpenCliSocialProtocolError("worker_response_invalid")
        if operation in {"search", "browse.groups"}:
            if (
                item.title is None
                or item.url is None
                or _required_url(item.url, host="facebook.com") != item.url
                or item.author is not None
                or item.published_at is not None
            ):
                raise OpenCliSocialProtocolError("worker_response_invalid")
        elif operation == "browse.feed" and (
            item.title is not None
            or item.url is not None
            or item.published_at is not None
        ):
            raise OpenCliSocialProtocolError("worker_response_invalid")


def _validate_parent_instagram_projection(
    operation: str,
    arguments: Mapping[str, object],
    items: tuple[SocialItemProjection, ...],
) -> None:
    if operation in {"search.users", "read.profile"}:
        if operation == "read.profile" and len(items) != 1:
            raise OpenCliSocialProtocolError("worker_response_invalid")
        requested = (
            cast(str, arguments["username"]) if operation == "read.profile" else None
        )
        for item in items:
            if (
                item.native_id is None
                or not _valid_username(item.native_id)
                or item.title is None
                or item.url is None
                or _required_url(item.url, host="instagram.com") != item.url
                or not _url_path_matches_username(item.url, item.native_id)
                or item.author is not None
                or item.published_at is not None
                or (
                    requested is not None
                    and item.native_id.casefold() != requested.casefold()
                )
            ):
                raise OpenCliSocialProtocolError("worker_response_invalid")
        return

    requested = (
        cast(str, arguments["username"]) if operation == "browse.user_posts" else None
    )
    for index, item in enumerate(items):
        if (
            item.native_id != str(index + 1)
            or item.title is not None
            or item.url is not None
            or (
                requested is not None
                and (
                    item.author is None
                    or item.author.casefold() != requested.casefold()
                )
            )
        ):
            raise OpenCliSocialProtocolError("worker_response_invalid")


def _validate_parent_twitter_projection(
    items: tuple[SocialItemProjection, ...],
) -> None:
    for item in items:
        native_id = item.native_id
        if (
            native_id is None
            or _TWITTER_POST_ID.fullmatch(native_id) is None
            or item.title is not None
            or item.url != f"https://x.com/i/status/{native_id}"
        ):
            raise OpenCliSocialProtocolError("worker_response_invalid")


def _validate_parent_xiaohongshu_projection(
    items: tuple[SocialItemProjection, ...],
) -> None:
    for item in items:
        native_id = item.native_id
        if (
            native_id is None
            or _XIAOHONGSHU_NOTE_ID.fullmatch(native_id) is None
            or item.title is None
            or item.url != f"https://www.xiaohongshu.com/explore/{native_id}"
        ):
            raise OpenCliSocialProtocolError("worker_response_invalid")


def _decode_failure(
    value: Mapping[str, object], *, key: tuple[str, str]
) -> ForkExecutionFailure:
    _validate_response_identity(value, key)
    _decode_backend(value["backend"])
    error = value["error"]
    if not isinstance(error, dict) or set(error) != _ERROR_FIELDS:
        raise OpenCliSocialProtocolError("worker_response_invalid")
    code = error["code"]
    if type(code) is not str or code not in _ERROR_CODES:
        raise OpenCliSocialProtocolError("worker_response_invalid")
    return ForkExecutionFailure(
        cast(WorkerSource, key[0]),
        cast(WorkerOperation, key[1]),
        cast(WorkerErrorCode, code),
    )


def _validate_response_identity(
    value: Mapping[str, object], key: tuple[str, str]
) -> None:
    if (
        value["protocol"] != PROTOCOL_VERSION
        or value["source"] != key[0]
        or value["operation"] != key[1]
    ):
        raise OpenCliSocialProtocolError("worker_response_invalid")


def _decode_backend(value: object) -> None:
    if (
        not isinstance(value, dict)
        or set(value) != _BACKEND_FIELDS
        or value["id"] != EXPECTED_BACKEND_ID
        or value["version"] != EXPECTED_BACKEND_VERSION
    ):
        raise OpenCliSocialProtocolError("worker_response_invalid")


def _decode_projected_item(value: object) -> SocialItemProjection:
    if not isinstance(value, dict) or set(value) != _PROJECTED_ITEM_FIELDS:
        raise OpenCliSocialProtocolError("worker_response_invalid")
    kind = value["kind"]
    if type(kind) is not str or kind not in _RESULT_KINDS:
        raise OpenCliSocialProtocolError("worker_response_invalid")
    text = _required_public_text(value["text"], MAX_RESULT_CHARACTERS, allow_empty=True)
    native_id = _optional_public_text(value["native_id"], MAX_NATIVE_ID_CHARACTERS)
    title = _optional_public_text(value["title"], _PUBLIC_TITLE_CHARACTERS)
    url = _optional_url(value["url"])
    if url is not None and len(url) > _PUBLIC_URL_CHARACTERS:
        raise OpenCliSocialProtocolError("worker_response_invalid")
    author = _optional_public_text(value["author"], _PUBLIC_AUTHOR_CHARACTERS)
    published_at = _optional_public_text(
        value["published_at"], _PUBLIC_PUBLISHED_CHARACTERS
    )
    return SocialItemProjection(
        cast(ResultKind, kind), text, native_id, title, url, author, published_at
    )


def _projected_item_value(item: SocialItemProjection) -> dict[str, object]:
    return {
        "author": item.author,
        "kind": item.kind,
        "native_id": item.native_id,
        "published_at": item.published_at,
        "text": item.text,
        "title": item.title,
        "url": item.url,
    }


def _requested_item_limit(
    key: tuple[str, str],
    arguments: Mapping[str, object],
    contract: _OperationContract,
) -> int:
    limit = arguments.get("limit", contract.maximum_items)
    if type(limit) is not int:
        raise OpenCliSocialProtocolError("worker_request_invalid")
    return min(limit, contract.maximum_items)


def _ordered_identifier(value: object, index: int) -> str:
    identifier = _required_text(value, MAX_NATIVE_ID_CHARACTERS)
    if identifier != str(index + 1):
        raise OpenCliSocialProtocolError("fork_result_invalid")
    return identifier


def _bounded_query(value: object) -> str:
    query = _required_text(value, MAX_QUERY_CHARACTERS)
    if query != query.strip():
        raise OpenCliSocialProtocolError("worker_request_invalid")
    return query


def _bounded_limit(value: object) -> int:
    if type(value) is not int or not 1 <= value <= MAX_LIMIT:
        raise OpenCliSocialProtocolError("worker_request_invalid")
    return value


def _bounded_subreddit(value: object) -> str:
    if (
        type(value) is not str
        or not value.isascii()
        or _SUBREDDIT.fullmatch(value) is None
    ):
        raise OpenCliSocialProtocolError("worker_request_invalid")
    return value


def _bounded_username(value: object) -> str:
    if type(value) is not str or not _valid_username(value):
        raise OpenCliSocialProtocolError("worker_request_invalid")
    return value


def _required_text(value: object, maximum: int, *, allow_empty: bool = False) -> str:
    if (
        type(value) is not str
        or len(value) > maximum
        or (not allow_empty and not value)
        or _contains_invalid_scalar(value)
    ):
        raise OpenCliSocialProtocolError("worker_value_invalid")
    return value


def _optional_text(value: object, maximum: int) -> str | None:
    if value is None:
        return None
    return _required_text(value, maximum, allow_empty=True)


def _required_public_text(
    value: object, maximum: int, *, allow_empty: bool = False
) -> str:
    text = _required_text(value, maximum, allow_empty=allow_empty)
    if text != _normalized_public_text(text):
        raise OpenCliSocialProtocolError("worker_response_invalid")
    return text


def _optional_public_text(value: object, maximum: int) -> str | None:
    if value is None:
        return None
    return _required_public_text(value, maximum, allow_empty=True)


def _optional_integer(value: object) -> int | None:
    if value is None:
        return None
    if type(value) is not int or not 0 <= value <= MAX_NORMALIZED_INTEGER:
        raise OpenCliSocialProtocolError("worker_value_invalid")
    return value


def _binary_integer(value: object) -> int:
    if type(value) is not int or value not in {0, 1}:
        raise OpenCliSocialProtocolError("worker_value_invalid")
    return value


def _required_url(value: object, *, host: str | None = None) -> str:
    if (
        type(value) is not str
        or len(value) > MAX_URL_CHARACTERS
        or not _valid_public_url(value)
    ):
        raise OpenCliSocialProtocolError("worker_value_invalid")
    if host is not None:
        parsed_host = urlsplit(value).hostname
        if parsed_host != host and not (
            type(parsed_host) is str and parsed_host.endswith(f".{host}")
        ):
            raise OpenCliSocialProtocolError("worker_value_invalid")
    return value


def _optional_url(value: object, *, host: str | None = None) -> str | None:
    if value is None:
        return None
    return _required_url(value, host=host)


def _valid_public_url(value: str) -> bool:
    if (
        value != value.strip()
        or not value.isascii()
        or "\\" in value
        or any(
            character.isspace() or ord(character) < 32 or ord(character) == 127
            for character in value
        )
    ):
        return False
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        if (
            parsed.scheme not in {"http", "https"}
            or host is None
            or parsed.username is not None
            or parsed.password is not None
            or not host.isascii()
        ):
            return False
        expected_port = 443 if parsed.scheme == "https" else 80
        if parsed.port not in {None, expected_port}:
            return False
    except (UnicodeError, ValueError):
        return False
    normalized = host.rstrip(".").lower()
    if (
        not normalized
        or normalized == "localhost"
        or normalized.endswith((".localhost", ".local"))
    ):
        return False
    try:
        address = ipaddress.ip_address(normalized)
    except ValueError:
        labels = normalized.split(".")
        return bool(
            len(labels) >= 2
            and all(
                0 < len(label) <= 63
                and label[0].isalnum()
                and label[-1].isalnum()
                and all(character.isalnum() or character == "-" for character in label)
                for label in labels
            )
        )
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return bool(
        address.is_global
        and not address.is_private
        and not address.is_loopback
        and not address.is_link_local
        and not address.is_reserved
        and not address.is_multicast
        and not address.is_unspecified
    )


def _reddit_post_id(value: object) -> str | None:
    if type(value) is not str or len(value) > 320 or not value.isascii():
        return None
    try:
        parsed = urlsplit(value)
    except (UnicodeError, ValueError):
        return None
    host = parsed.hostname
    if (
        parsed.scheme != "https"
        or host is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port is not None
        or parsed.query
        or parsed.fragment
        or (host != "reddit.com" and not host.endswith(".reddit.com"))
    ):
        return None
    parts = parsed.path.split("/")
    if len(parts) not in {6, 7} or parts[1] != "r" or parts[3] != "comments":
        return None
    if parts[0] or (len(parts) == 7 and parts[-1]):
        return None
    subreddit, post_id, slug = parts[2], parts[4], parts[5]
    if (
        re.fullmatch(r"[A-Za-z0-9_]{1,32}", subreddit) is None
        or _REDDIT_POST_ID.fullmatch(post_id.lower()) is None
        or _REDDIT_POST_SLUG.fullmatch(slug) is None
    ):
        return None
    return post_id.lower()


def _reddit_post_subreddit(value: object) -> str | None:
    if _reddit_post_id(value) is None:
        return None
    return urlsplit(cast(str, value)).path.split("/")[2]


def _valid_username(value: str) -> bool:
    return bool(value.isascii() and _SOCIAL_USERNAME.fullmatch(value))


def _url_path_matches_username(url: str, username: str) -> bool:
    parsed = urlsplit(url)
    return bool(
        parsed.scheme == "https"
        and parsed.port is None
        and not parsed.query
        and not parsed.fragment
        and parsed.path.rstrip("/").casefold() == f"/{username}".casefold()
    )


def _valid_absolute_path(value: str) -> bool:
    return bool(
        0 < len(value) <= 8_192
        and value.startswith("/")
        and value.isprintable()
        and "\x00" not in value
        and all(part not in {"", ".", ".."} for part in value.split("/")[1:])
    )


def _path_is_below(path: str, root: str) -> bool:
    prefix = root.rstrip("/") + "/"
    return path.startswith(prefix) and path != root


def _deadline_checkpoint(deadline: float) -> None:
    if time.monotonic() >= deadline:
        raise _DeadlineExpired


def _worker_checkpoint(deadline: float) -> None:
    if _CANCELLATION_REQUESTED.is_set():
        raise _WorkerCancellationRequested
    _deadline_checkpoint(deadline)


def _encode_frame(value: Mapping[str, object], maximum: int) -> bytes:
    try:
        payload = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8", errors="strict")
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise OpenCliSocialProtocolError("worker_frame_invalid") from None
    if not 0 < len(payload) <= maximum:
        raise OpenCliSocialProtocolError("worker_frame_invalid")
    return len(payload).to_bytes(_LENGTH_BYTES, "big") + payload


def _decode_frame(raw: bytes | bytearray, maximum: int) -> object:
    if not isinstance(raw, bytes | bytearray) or len(raw) < _LENGTH_BYTES + 1:
        raise OpenCliSocialProtocolError("worker_frame_invalid")
    length = int.from_bytes(raw[:_LENGTH_BYTES], "big")
    if not 0 < length <= maximum or len(raw) != _LENGTH_BYTES + length:
        raise OpenCliSocialProtocolError("worker_frame_invalid")
    return _load_json(raw[_LENGTH_BYTES:], maximum=maximum)


def _load_json(raw: bytes | bytearray, *, maximum: int) -> object:
    if not 0 < len(raw) <= maximum:
        raise OpenCliSocialProtocolError("worker_json_invalid")
    try:
        value = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (OpenCliSocialProtocolError, UnicodeError, ValueError, RecursionError):
        raise OpenCliSocialProtocolError("worker_json_invalid") from None
    _validate_json_shape(value)
    return value


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise OpenCliSocialProtocolError("worker_json_invalid")
        value[key] = item
    return value


def _reject_constant(_: str) -> object:
    raise OpenCliSocialProtocolError("worker_json_invalid")


def _validate_json_shape(value: object) -> None:
    pending: list[tuple[object, int]] = [(value, 1)]
    nodes = 0
    while pending:
        current, depth = pending.pop()
        nodes += 1
        if nodes > MAX_JSON_NODES or depth > MAX_JSON_DEPTH:
            raise OpenCliSocialProtocolError("worker_json_invalid")
        if current is None or type(current) in {bool, int}:
            continue
        if type(current) is float:
            if not math.isfinite(current):
                raise OpenCliSocialProtocolError("worker_json_invalid")
            continue
        if type(current) is str:
            if len(
                current.encode("utf-8", errors="strict")
            ) > MAX_JSON_STRING_BYTES or _contains_invalid_scalar(current):
                raise OpenCliSocialProtocolError("worker_json_invalid")
            continue
        if isinstance(current, list):
            pending.extend((item, depth + 1) for item in current)
            continue
        if isinstance(current, dict):
            pending.extend((item, depth + 1) for item in current.values())
            continue
        raise OpenCliSocialProtocolError("worker_json_invalid")


def _contains_invalid_scalar(value: str) -> bool:
    return any(
        character == "\x00" or 0xD800 <= ord(character) <= 0xDFFF for character in value
    )


def _main() -> int:
    try:
        signal.signal(signal.SIGTERM, _request_worker_cancellation)
    except (AttributeError, OSError, ValueError):
        return 1
    try:
        request = _read_request(sys.stdin.buffer)
    except Exception:
        return 1
    try:
        value = _execute_request(request)
    except Exception:
        return 1
    try:
        output = _encode_frame(value, MAX_OUTPUT_BYTES)
    except OpenCliSocialProtocolError:
        try:
            output = _encode_frame(
                _failure_value(
                    (request.source, request.operation), "backend_contract_violation"
                ),
                MAX_OUTPUT_BYTES,
            )
        except Exception:
            return 1
    try:
        sys.stdout.buffer.write(output)
        sys.stdout.buffer.flush()
        return 0
    except Exception:
        return 1


if __name__ == "__main__":
    raise SystemExit(_main())


__all__ = [
    "EXPECTED_BACKEND_ID",
    "EXPECTED_BACKEND_VERSION",
    "ForkExecutionFailure",
    "MAX_OUTPUT_BYTES",
    "OpenCliSocialProjection",
    "OpenCliSocialProtocolError",
    "SocialItemProjection",
    "WorkerErrorCode",
    "WorkerOperation",
    "WorkerRequest",
    "WorkerResponse",
    "WorkerSession",
    "WorkerSource",
    "decode_response",
    "encode_request",
]
