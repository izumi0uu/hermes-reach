"""Closed Agent-Reach V2EX execution inside an isolated worker."""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import BinaryIO, Final, Literal, Protocol, TypeAlias, cast

from ..agent_reach_bridge import (
    AgentReachExecutionApi,
    validate_agent_reach_execution_contract,
)
from ..normalized import MAX_NORMALIZED_INTEGER

WorkerOperation = Literal[
    "browse.hot",
    "browse.node_topics",
    "read.topic",
    "read.user",
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
V2exSchemaId = Literal[
    "v2ex.topic.v1",
    "v2ex.reply.v1",
    "v2ex.profile.v1",
]

PROTOCOL_VERSION: Final = "v1"
EXPECTED_SOURCE: Final = "v2ex"
EXPECTED_BACKEND_ID: Final = "v2ex-public-api"
EXPECTED_BACKEND_VERSION: Final = "legacy-json-2026-07-31"
TOPIC_SCHEMA: Final = "v2ex.topic.v1"
REPLY_SCHEMA: Final = "v2ex.reply.v1"
PROFILE_SCHEMA: Final = "v2ex.profile.v1"

MAX_REQUEST_BYTES: Final = 32 * 1024
MAX_OUTPUT_BYTES: Final = 1_048_576
MAX_LIMIT: Final = 50
MAX_PAGE: Final = 100
MAX_TOPIC_ITEMS: Final = 21
MAX_IDENTIFIER_CHARACTERS: Final = 64
MAX_TOPIC_ID_CHARACTERS: Final = 32
MAX_TEXT_CHARACTERS: Final = 16_000
MAX_TITLE_CHARACTERS: Final = 4_096
MAX_AUTHOR_CHARACTERS: Final = 2_048
MAX_PUBLISHED_CHARACTERS: Final = 512
_LENGTH_BYTES: Final = 4
_BASE_URL: Final = "https://www.v2ex.com"
_REQUEST_OPERATIONS: Final = frozenset(
    {"browse.hot", "browse.node_topics", "read.topic", "read.user"}
)
_SUCCESS_FIELDS: Final = frozenset(
    {
        "backend",
        "items",
        "operation",
        "partial",
        "protocol",
        "source",
        "truncated",
    }
)
_FAILURE_FIELDS: Final = frozenset(
    {"backend", "error", "operation", "protocol", "source"}
)
_BACKEND_FIELDS: Final = frozenset({"id", "version"})
_ERROR_FIELDS: Final = frozenset({"code"})
_ITEM_ENVELOPE_FIELDS: Final = frozenset({"fields", "schema"})
_TOPIC_FIELDS: Final = frozenset(
    {"author", "native_id", "node", "published_at", "text", "title", "url"}
)
_REPLY_FIELDS: Final = frozenset({"author", "native_id", "published_at", "text", "url"})
_PROFILE_FIELDS: Final = frozenset(
    {"native_id", "published_at", "text", "title", "url"}
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
_PARTIAL_ERROR_CODES: Final[frozenset[str]] = frozenset(
    {
        "not_found",
        "authentication",
        "authorization",
        "rate_limit",
        "transient",
        "permanent",
        "backend_contract_violation",
    }
)
_IDENTIFIER: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
_POSITIVE_DECIMAL: Final = re.compile(r"[1-9][0-9]{0,31}")


class V2exProtocolError(ValueError):
    """The closed worker input or output contract was violated."""


@dataclass(frozen=True, slots=True)
class WorkerRequest:
    operation: WorkerOperation
    node: str | None = None
    page: int | None = None
    limit: int | None = None
    topic_id: str | None = None
    username: str | None = None


@dataclass(frozen=True, slots=True)
class V2exTopicProjection:
    """One fully revalidated V2EX topic returned by the fork."""

    text: str | None
    native_id: str
    title: str
    url: str
    author: str | None
    published_at: str | None
    node: str

    @property
    def schema_id(self) -> Literal["v2ex.topic.v1"]:
        return TOPIC_SCHEMA


@dataclass(frozen=True, slots=True)
class V2exReplyProjection:
    """One fully revalidated V2EX reply returned by the fork."""

    text: str
    native_id: str
    url: str
    author: str
    published_at: str | None

    @property
    def schema_id(self) -> Literal["v2ex.reply.v1"]:
        return REPLY_SCHEMA


@dataclass(frozen=True, slots=True)
class V2exProfileProjection:
    """One fully revalidated V2EX member profile returned by the fork."""

    text: str | None
    native_id: str
    title: str
    url: str
    published_at: str | None

    @property
    def schema_id(self) -> Literal["v2ex.profile.v1"]:
        return PROFILE_SCHEMA


V2exItemProjection: TypeAlias = (
    V2exTopicProjection | V2exReplyProjection | V2exProfileProjection
)


@dataclass(frozen=True, slots=True)
class V2exProjection:
    """A fully revalidated successful fork result."""

    operation: WorkerOperation
    items: tuple[V2exItemProjection, ...]
    truncated: bool
    partial_error_code: WorkerErrorCode | None = None


@dataclass(frozen=True, slots=True)
class ForkExecutionFailure:
    """A fully revalidated, redacted fork failure."""

    operation: WorkerOperation
    error_code: WorkerErrorCode


WorkerResponse: TypeAlias = V2exProjection | ForkExecutionFailure


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
    return validate_agent_reach_execution_contract(runtime_module="v2ex")


def encode_request(
    operation: WorkerOperation,
    *,
    node: str | None = None,
    page: int | None = None,
    limit: int | None = None,
    topic_id: str | None = None,
    username: str | None = None,
) -> bytes:
    """Encode one closed operation request for the isolated worker."""

    request = _request_from_arguments(
        operation,
        node=node,
        page=page,
        limit=limit,
        topic_id=topic_id,
        username=username,
    )
    return _encode_frame(_request_value(request), MAX_REQUEST_BYTES)


def decode_response(
    raw: bytes,
    *,
    operation: WorkerOperation,
    node: str | None = None,
    page: int | None = None,
    limit: int | None = None,
    topic_id: str | None = None,
    username: str | None = None,
) -> WorkerResponse:
    """Independently validate the complete bounded worker response."""

    request = _request_from_arguments(
        operation,
        node=node,
        page=page,
        limit=limit,
        topic_id=topic_id,
        username=username,
    )
    value = _decode_frame(raw, MAX_OUTPUT_BYTES)
    if not isinstance(value, dict):
        raise V2exProtocolError("worker_response_invalid")
    if set(value) == _SUCCESS_FIELDS:
        return _decode_success(value, request=request)
    if set(value) == _FAILURE_FIELDS:
        return _decode_failure(value, operation=request.operation)
    raise V2exProtocolError("worker_response_invalid")


def _read_request(stream: BinaryIO) -> WorkerRequest:
    header = stream.read(_LENGTH_BYTES)
    if len(header) != _LENGTH_BYTES:
        raise V2exProtocolError("worker_request_invalid")
    length = int.from_bytes(header, "big")
    if not 0 < length <= MAX_REQUEST_BYTES:
        raise V2exProtocolError("worker_request_invalid")
    payload = stream.read(length)
    if len(payload) != length or stream.read(1):
        raise V2exProtocolError("worker_request_invalid")
    return _validated_request(_load_json(payload, maximum=MAX_REQUEST_BYTES))


def _request_from_arguments(
    operation: WorkerOperation,
    *,
    node: str | None,
    page: int | None,
    limit: int | None,
    topic_id: str | None,
    username: str | None,
) -> WorkerRequest:
    value: dict[str, object] = {
        "protocol_version": PROTOCOL_VERSION,
        "operation": operation,
    }
    for name, selected in (
        ("node", node),
        ("page", page),
        ("limit", limit),
        ("topic_id", topic_id),
        ("username", username),
    ):
        if selected is not None:
            value[name] = selected
    return _validated_request(value)


def _validated_request(value: object) -> WorkerRequest:
    if not isinstance(value, dict):
        raise V2exProtocolError("worker_request_invalid")
    operation = value.get("operation")
    if (
        value.get("protocol_version") != PROTOCOL_VERSION
        or type(operation) is not str
        or operation not in _REQUEST_OPERATIONS
    ):
        raise V2exProtocolError("worker_request_invalid")
    if operation == "browse.hot":
        if set(value) != {"protocol_version", "operation", "limit"}:
            raise V2exProtocolError("worker_request_invalid")
        return WorkerRequest("browse.hot", limit=_bounded_limit(value.get("limit")))
    if operation == "browse.node_topics":
        if set(value) != {
            "protocol_version",
            "operation",
            "node",
            "page",
            "limit",
        }:
            raise V2exProtocolError("worker_request_invalid")
        return WorkerRequest(
            "browse.node_topics",
            node=_bounded_identifier(value.get("node")),
            page=_bounded_page(value.get("page")),
            limit=_bounded_limit(value.get("limit")),
        )
    if operation == "read.topic":
        if set(value) != {"protocol_version", "operation", "topic_id"}:
            raise V2exProtocolError("worker_request_invalid")
        return WorkerRequest(
            "read.topic",
            topic_id=_bounded_topic_id(value.get("topic_id")),
        )
    if set(value) != {"protocol_version", "operation", "username"}:
        raise V2exProtocolError("worker_request_invalid")
    return WorkerRequest(
        "read.user",
        username=_bounded_identifier(value.get("username")),
    )


def _request_value(request: WorkerRequest) -> dict[str, object]:
    value: dict[str, object] = {
        "protocol_version": PROTOCOL_VERSION,
        "operation": request.operation,
    }
    for name in ("node", "page", "limit", "topic_id", "username"):
        selected = getattr(request, name)
        if selected is not None:
            value[name] = selected
    return value


def _execute_request(
    request: WorkerRequest,
    *,
    execution_api_provider: ExecutionApiProvider | None = None,
) -> Mapping[str, object]:
    # Revalidate the isolated interpreter immediately before every network call.
    provider = (
        execution_api_provider
        if execution_api_provider is not None
        else _load_execution_api
    )
    try:
        api = provider()
    except Exception:
        return _failure_value(request.operation, "backend_contract_violation")

    try:
        request_factory = cast(Callable[..., object], api.execution_request_type)
        network_factory = cast(Callable[..., object], api.network_access_type)
        limits_factory = cast(Callable[..., object], api.execution_limits_type)
        context_factory = cast(Callable[..., object], api.execution_context_type)
        execution_request = request_factory(
            PROTOCOL_VERSION,
            EXPECTED_SOURCE,
            request.operation,
            _request_arguments(request),
        )
        limits = limits_factory(
            maximum_items=_request_item_limit(request),
            maximum_text_characters=MAX_TEXT_CHARACTERS,
        )
        context = context_factory((network_factory(),), limits=limits)
        result = api.execute(execution_request, context)

        if type(result) is api.execution_success_type:
            success = cast(_ExecutionSuccess, result)
            items = success.items
            partial = success.partial_error_code
            if (
                not _exact_text(success.protocol_version, PROTOCOL_VERSION)
                or not _exact_text(success.source, EXPECTED_SOURCE)
                or not _exact_text(success.operation, request.operation)
                or not _exact_text(success.backend_id, EXPECTED_BACKEND_ID)
                or not _exact_text(
                    success.backend_version,
                    EXPECTED_BACKEND_VERSION,
                )
                or type(success.truncated) is not bool
                or type(items) is not tuple
                or len(items) > _request_item_limit(request)
                or not _valid_partial_code(request.operation, partial)
            ):
                return _failure_value(
                    request.operation,
                    "backend_contract_violation",
                )
            projected: list[V2exItemProjection] = []
            for raw_item in items:
                if type(raw_item) is not api.execution_item_type:
                    return _failure_value(
                        request.operation,
                        "backend_contract_violation",
                    )
                item = cast(_ExecutionItem, raw_item)
                projected.append(_decode_projection(item.schema_id, item.fields))
            selected = tuple(projected)
            _validate_projection_sequence(request, selected, partial)
            return {
                "backend": _backend_value(),
                "items": [_projection_value(item) for item in selected],
                "operation": request.operation,
                "partial": partial,
                "protocol": PROTOCOL_VERSION,
                "source": EXPECTED_SOURCE,
                "truncated": success.truncated,
            }

        if type(result) is api.execution_failure_type:
            failure = cast(_ExecutionFailure, result)
            if (
                _exact_text(failure.protocol_version, PROTOCOL_VERSION)
                and _exact_text(failure.source, EXPECTED_SOURCE)
                and _exact_text(failure.operation, request.operation)
                and _exact_text(failure.backend_id, EXPECTED_BACKEND_ID)
                and _exact_text(
                    failure.backend_version,
                    EXPECTED_BACKEND_VERSION,
                )
                and type(failure.error_code) is str
                and failure.error_code in _ERROR_CODES
            ):
                return _failure_value(
                    request.operation,
                    cast(WorkerErrorCode, failure.error_code),
                )
    except Exception:
        return _failure_value(request.operation, "backend_contract_violation")
    return _failure_value(request.operation, "backend_contract_violation")


def _request_arguments(request: WorkerRequest) -> dict[str, str | int]:
    if request.operation == "browse.hot":
        if request.limit is None:
            raise V2exProtocolError("worker_request_invalid")
        return {"limit": request.limit}
    if request.operation == "browse.node_topics":
        if request.node is None or request.page is None or request.limit is None:
            raise V2exProtocolError("worker_request_invalid")
        return {
            "node": request.node,
            "page": request.page,
            "limit": request.limit,
        }
    if request.operation == "read.topic":
        if request.topic_id is None:
            raise V2exProtocolError("worker_request_invalid")
        return {"topic_id": request.topic_id}
    if request.username is None:
        raise V2exProtocolError("worker_request_invalid")
    return {"username": request.username}


def _request_item_limit(request: WorkerRequest) -> int:
    if request.operation in {"browse.hot", "browse.node_topics"}:
        if request.limit is None:
            raise V2exProtocolError("worker_request_invalid")
        return request.limit
    if request.operation == "read.topic":
        return MAX_TOPIC_ITEMS
    return 1


def _failure_value(
    operation: WorkerOperation,
    error_code: WorkerErrorCode,
) -> dict[str, object]:
    return {
        "backend": _backend_value(),
        "error": {"code": error_code},
        "operation": operation,
        "protocol": PROTOCOL_VERSION,
        "source": EXPECTED_SOURCE,
    }


def _backend_value() -> dict[str, str]:
    return {
        "id": EXPECTED_BACKEND_ID,
        "version": EXPECTED_BACKEND_VERSION,
    }


def _decode_success(
    value: Mapping[str, object],
    *,
    request: WorkerRequest,
) -> V2exProjection:
    _validate_identity(value, request.operation)
    _decode_backend(value["backend"])
    truncated = value["truncated"]
    partial = value["partial"]
    items = value["items"]
    if (
        type(truncated) is not bool
        or not isinstance(items, list)
        or len(items) > _request_item_limit(request)
        or not _valid_partial_code(request.operation, partial)
    ):
        raise V2exProtocolError("worker_response_invalid")
    projected = tuple(_decode_projection_frame(item) for item in items)
    _validate_projection_sequence(request, projected, partial)
    return V2exProjection(
        request.operation,
        projected,
        truncated,
        cast(WorkerErrorCode | None, partial),
    )


def _decode_failure(
    value: Mapping[str, object],
    *,
    operation: WorkerOperation,
) -> ForkExecutionFailure:
    _validate_identity(value, operation)
    _decode_backend(value["backend"])
    error = value["error"]
    if not isinstance(error, dict) or set(error) != _ERROR_FIELDS:
        raise V2exProtocolError("worker_response_invalid")
    code = error["code"]
    if type(code) is not str or code not in _ERROR_CODES:
        raise V2exProtocolError("worker_response_invalid")
    return ForkExecutionFailure(operation, cast(WorkerErrorCode, code))


def _decode_projection_frame(value: object) -> V2exItemProjection:
    if not isinstance(value, dict) or set(value) != _ITEM_ENVELOPE_FIELDS:
        raise V2exProtocolError("worker_response_invalid")
    return _decode_projection(value["schema"], value["fields"])


def _decode_projection(schema: object, value: object) -> V2exItemProjection:
    if type(schema) is not str:
        raise V2exProtocolError("worker_response_invalid")
    if schema == TOPIC_SCHEMA:
        return _decode_topic(value)
    if schema == REPLY_SCHEMA:
        return _decode_reply(value)
    if schema == PROFILE_SCHEMA:
        return _decode_profile(value)
    raise V2exProtocolError("worker_response_invalid")


def _decode_topic(value: object) -> V2exTopicProjection:
    fields = _closed_fields(value, _TOPIC_FIELDS)
    native_id = _decode_native_id(fields["native_id"])
    url = fields["url"]
    if type(url) is not str or url != _topic_url(native_id):
        raise V2exProtocolError("worker_response_invalid")
    return V2exTopicProjection(
        _decode_text(fields["text"], MAX_TEXT_CHARACTERS, nullable=True),
        native_id,
        _decode_required_text(fields["title"], MAX_TITLE_CHARACTERS),
        url,
        _decode_identifier(fields["author"], nullable=True),
        _decode_timestamp(fields["published_at"]),
        cast(str, _decode_identifier(fields["node"])),
    )


def _decode_reply(value: object) -> V2exReplyProjection:
    fields = _closed_fields(value, _REPLY_FIELDS)
    native_id = _decode_native_id(fields["native_id"])
    url = fields["url"]
    if (
        type(url) is not str
        or re.fullmatch(
            rf"https://www[.]v2ex[.]com/t/[1-9][0-9]{{0,31}}#reply{re.escape(native_id)}",
            url,
        )
        is None
    ):
        raise V2exProtocolError("worker_response_invalid")
    return V2exReplyProjection(
        _decode_required_text(fields["text"], MAX_TEXT_CHARACTERS),
        native_id,
        url,
        cast(str, _decode_identifier(fields["author"])),
        _decode_timestamp(fields["published_at"]),
    )


def _decode_profile(value: object) -> V2exProfileProjection:
    fields = _closed_fields(value, _PROFILE_FIELDS)
    native_id = _decode_native_id(fields["native_id"])
    username = cast(str, _decode_identifier(fields["title"]))
    url = fields["url"]
    if type(url) is not str or url != _profile_url(username):
        raise V2exProtocolError("worker_response_invalid")
    return V2exProfileProjection(
        _decode_text(fields["text"], MAX_TEXT_CHARACTERS, nullable=True),
        native_id,
        username,
        url,
        _decode_timestamp(fields["published_at"]),
    )


def _projection_value(item: V2exItemProjection) -> dict[str, object]:
    if isinstance(item, V2exTopicProjection):
        fields: dict[str, object] = {
            "author": item.author,
            "native_id": item.native_id,
            "node": item.node,
            "published_at": item.published_at,
            "text": item.text,
            "title": item.title,
            "url": item.url,
        }
    elif isinstance(item, V2exReplyProjection):
        fields = {
            "author": item.author,
            "native_id": item.native_id,
            "published_at": item.published_at,
            "text": item.text,
            "url": item.url,
        }
    else:
        fields = {
            "native_id": item.native_id,
            "published_at": item.published_at,
            "text": item.text,
            "title": item.title,
            "url": item.url,
        }
    return {"fields": fields, "schema": item.schema_id}


def _validate_projection_sequence(
    request: WorkerRequest,
    items: tuple[V2exItemProjection, ...],
    partial: object,
) -> None:
    maximum = _request_item_limit(request)
    if len(items) > maximum:
        raise V2exProtocolError("worker_response_invalid")
    if request.operation in {"browse.hot", "browse.node_topics"}:
        if partial is not None or any(
            not isinstance(item, V2exTopicProjection) for item in items
        ):
            raise V2exProtocolError("worker_response_invalid")
        topics = cast(tuple[V2exTopicProjection, ...], items)
        if len({topic.native_id for topic in topics}) != len(topics):
            raise V2exProtocolError("worker_response_invalid")
        if request.operation == "browse.node_topics":
            if request.node is None or any(
                topic.node.casefold() != request.node.casefold() for topic in topics
            ):
                raise V2exProtocolError("worker_response_invalid")
        return
    if request.operation == "read.topic":
        if (
            request.topic_id is None
            or not items
            or not isinstance(items[0], V2exTopicProjection)
            or any(not isinstance(item, V2exReplyProjection) for item in items[1:])
        ):
            raise V2exProtocolError("worker_response_invalid")
        topic = items[0]
        expected_topic_id = _canonical_topic_id(request.topic_id)
        replies = cast(tuple[V2exReplyProjection, ...], items[1:])
        if (
            topic.native_id != expected_topic_id
            or (partial is not None and len(items) != 1)
            or len({reply.native_id for reply in replies}) != len(replies)
            or any(
                reply.url != _reply_url(expected_topic_id, reply.native_id)
                for reply in replies
            )
        ):
            raise V2exProtocolError("worker_response_invalid")
        return
    if (
        request.username is None
        or partial is not None
        or len(items) != 1
        or not isinstance(items[0], V2exProfileProjection)
        or items[0].title.casefold() != request.username.casefold()
    ):
        raise V2exProtocolError("worker_response_invalid")


def _valid_partial_code(operation: WorkerOperation, value: object) -> bool:
    return value is None or bool(
        operation == "read.topic"
        and type(value) is str
        and value in _PARTIAL_ERROR_CODES
    )


def _closed_fields(value: object, expected: frozenset[str]) -> Mapping[str, object]:
    if (
        not isinstance(value, Mapping)
        or set(value) != expected
        or not all(type(name) is str for name in value)
    ):
        raise V2exProtocolError("worker_response_invalid")
    return cast(Mapping[str, object], value)


def _decode_text(
    value: object,
    maximum: int,
    *,
    nullable: bool = False,
) -> str | None:
    if value is None and nullable:
        return None
    if (
        type(value) is not str
        or not 0 < len(value) <= maximum
        or _contains_invalid_scalar(value)
        or value != " ".join(value.split())
    ):
        raise V2exProtocolError("worker_response_invalid")
    return value


def _decode_required_text(value: object, maximum: int) -> str:
    decoded = _decode_text(value, maximum)
    if decoded is None:
        raise V2exProtocolError("worker_response_invalid")
    return decoded


def _decode_identifier(value: object, *, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        raise V2exProtocolError("worker_response_invalid")
    return value


def _decode_native_id(value: object) -> str:
    if (
        type(value) is not str
        or _POSITIVE_DECIMAL.fullmatch(value) is None
        or int(value) > MAX_NORMALIZED_INTEGER
    ):
        raise V2exProtocolError("worker_response_invalid")
    return value


def _decode_timestamp(value: object) -> str | None:
    if value is None:
        return None
    if (
        type(value) is not str
        or not 0 < len(value) <= MAX_PUBLISHED_CHARACTERS
        or not value.isascii()
    ):
        raise V2exProtocolError("worker_response_invalid")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise V2exProtocolError("worker_response_invalid") from None
    if (
        parsed.tzinfo is None
        or parsed.utcoffset() != UTC.utcoffset(parsed)
        or parsed.year < 1970
        or parsed.isoformat() != value
    ):
        raise V2exProtocolError("worker_response_invalid")
    return value


def _validate_identity(
    value: Mapping[str, object],
    operation: WorkerOperation,
) -> None:
    if (
        value["protocol"] != PROTOCOL_VERSION
        or value["source"] != EXPECTED_SOURCE
        or value["operation"] != operation
    ):
        raise V2exProtocolError("worker_response_invalid")


def _decode_backend(value: object) -> None:
    if (
        not isinstance(value, dict)
        or set(value) != _BACKEND_FIELDS
        or value["id"] != EXPECTED_BACKEND_ID
        or value["version"] != EXPECTED_BACKEND_VERSION
    ):
        raise V2exProtocolError("worker_response_invalid")


def _bounded_identifier(value: object) -> str:
    if (
        type(value) is not str
        or len(value) > MAX_IDENTIFIER_CHARACTERS
        or _IDENTIFIER.fullmatch(value) is None
    ):
        raise V2exProtocolError("worker_request_invalid")
    return value


def _bounded_topic_id(value: object) -> str:
    if (
        type(value) is not str
        or not 1 <= len(value) <= MAX_TOPIC_ID_CHARACTERS
        or not value.isascii()
        or not value.isdigit()
        or int(value) <= 0
    ):
        raise V2exProtocolError("worker_request_invalid")
    return value


def _bounded_limit(value: object) -> int:
    if type(value) is not int or not 1 <= value <= MAX_LIMIT:
        raise V2exProtocolError("worker_request_invalid")
    return value


def _bounded_page(value: object) -> int:
    if type(value) is not int or not 1 <= value <= MAX_PAGE:
        raise V2exProtocolError("worker_request_invalid")
    return value


def _canonical_topic_id(value: str) -> str:
    return str(int(value))


def _topic_url(topic_id: str) -> str:
    return f"{_BASE_URL}/t/{topic_id}"


def _reply_url(topic_id: str, reply_id: str) -> str:
    return f"{_topic_url(topic_id)}#reply{reply_id}"


def _profile_url(username: str) -> str:
    return f"{_BASE_URL}/member/{username}"


def _contains_invalid_scalar(value: str) -> bool:
    return any(
        character == "\x00" or 0xD800 <= ord(character) <= 0xDFFF for character in value
    )


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
        raise V2exProtocolError("worker_frame_invalid") from None
    if not 0 < len(payload) <= maximum:
        raise V2exProtocolError("worker_frame_invalid")
    return len(payload).to_bytes(_LENGTH_BYTES, "big") + payload


def _decode_frame(value: bytes, maximum: int) -> object:
    if type(value) is not bytes or len(value) < _LENGTH_BYTES + 1:
        raise V2exProtocolError("worker_frame_invalid")
    length = int.from_bytes(value[:_LENGTH_BYTES], "big")
    if not 0 < length <= maximum or len(value) != length + _LENGTH_BYTES:
        raise V2exProtocolError("worker_frame_invalid")
    return _load_json(value[_LENGTH_BYTES:], maximum=maximum)


def _load_json(value: bytes, *, maximum: int) -> object:
    if not 0 < len(value) <= maximum:
        raise V2exProtocolError("worker_json_invalid")
    try:
        return json.loads(
            value.decode("utf-8", errors="strict"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (V2exProtocolError, UnicodeError, ValueError, RecursionError):
        raise V2exProtocolError("worker_json_invalid") from None


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise V2exProtocolError("worker_json_invalid")
        result[key] = value
    return result


def _reject_constant(_: str) -> object:
    raise V2exProtocolError("worker_json_invalid")


def _exact_text(value: object, expected: str) -> bool:
    return type(value) is str and value == expected


def _main() -> int:
    try:
        request = _read_request(sys.stdin.buffer)
    except Exception:
        return 1
    try:
        value = _execute_request(request)
        output = _encode_frame(value, MAX_OUTPUT_BYTES)
        sys.stdout.buffer.write(output)
        sys.stdout.buffer.flush()
        return 0
    except Exception:
        return 1


if __name__ == "__main__":
    raise SystemExit(_main())
