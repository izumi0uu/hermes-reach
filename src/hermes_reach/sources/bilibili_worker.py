"""Closed Agent-Reach Bilibili execution inside an isolated worker."""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import BinaryIO, Final, Literal, Protocol, TypeAlias, cast
from urllib.parse import urlsplit

from ..agent_reach_bridge import (
    AgentReachExecutionApi,
    validate_agent_reach_execution_contract,
)
from ..normalized import MAX_NORMALIZED_INTEGER

WorkerOperation = Literal["search.videos", "read.video", "browse.hot", "browse.rank"]
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

PROTOCOL_VERSION: Final = "v1"
EXPECTED_SOURCE: Final = "bilibili"
EXPECTED_BACKEND_ID: Final = "bili-cli"
EXPECTED_BACKEND_VERSION: Final = "0.6.2"
VIDEO_SCHEMA: Final = "bilibili.video.v1"

MAX_REQUEST_BYTES: Final = 32 * 1024
MAX_OUTPUT_BYTES: Final = 512 * 1024
MAX_QUERY_CHARACTERS: Final = 4096
MAX_URL_CHARACTERS: Final = 128
MAX_LIMIT: Final = 50
MAX_TEXT_CHARACTERS: Final = 16_000
MAX_TITLE_CHARACTERS: Final = 4096
MAX_AUTHOR_CHARACTERS: Final = 1024
_LENGTH_BYTES: Final = 4
_REQUEST_OPERATIONS: Final = frozenset(
    {"search.videos", "read.video", "browse.hot", "browse.rank"}
)
_SUCCESS_FIELDS: Final = frozenset(
    {
        "backend",
        "items",
        "operation",
        "partial",
        "protocol",
        "schema",
        "source",
        "truncated",
    }
)
_FAILURE_FIELDS: Final = frozenset(
    {"backend", "error", "operation", "protocol", "source"}
)
_BACKEND_FIELDS: Final = frozenset({"id", "version"})
_ERROR_FIELDS: Final = frozenset({"code"})
_ITEM_FIELDS: Final = frozenset(
    {
        "author",
        "duration_seconds",
        "native_id",
        "text",
        "title",
        "url",
        "view_count",
    }
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
_BVID: Final = re.compile(r"BV[A-Za-z0-9]{10}")


class BilibiliProtocolError(ValueError):
    """The closed worker input or output contract was violated."""


@dataclass(frozen=True, slots=True)
class WorkerRequest:
    operation: WorkerOperation
    query: str | None = None
    url: str | None = None
    limit: int | None = None


@dataclass(frozen=True, slots=True)
class BilibiliVideoProjection:
    """One fully revalidated source-native item returned by the fork."""

    text: str
    native_id: str
    title: str
    url: str
    author: str | None
    duration_seconds: int
    view_count: int


@dataclass(frozen=True, slots=True)
class BilibiliProjection:
    """A fully revalidated successful fork result."""

    operation: WorkerOperation
    items: tuple[BilibiliVideoProjection, ...]
    truncated: bool


@dataclass(frozen=True, slots=True)
class ForkExecutionFailure:
    """A fully revalidated, redacted fork failure."""

    operation: WorkerOperation
    error_code: WorkerErrorCode


WorkerResponse: TypeAlias = BilibiliProjection | ForkExecutionFailure


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
    return validate_agent_reach_execution_contract(runtime_module="bilibili")


def encode_request(
    operation: WorkerOperation,
    *,
    query: str | None = None,
    url: str | None = None,
    limit: int | None = None,
) -> bytes:
    """Encode one closed operation request for the isolated worker."""

    request = _validated_request(
        {
            "protocol_version": PROTOCOL_VERSION,
            "operation": operation,
            **({"query": query} if query is not None else {}),
            **({"url": url} if url is not None else {}),
            **({"limit": limit} if limit is not None else {}),
        }
    )
    value: dict[str, object] = {
        "protocol_version": PROTOCOL_VERSION,
        "operation": request.operation,
    }
    if request.query is not None:
        value["query"] = request.query
    if request.url is not None:
        value["url"] = request.url
    if request.limit is not None:
        value["limit"] = request.limit
    return _encode_frame(value, MAX_REQUEST_BYTES)


def decode_response(
    raw: bytes,
    *,
    operation: WorkerOperation,
    limit: int | None,
) -> WorkerResponse:
    """Independently validate the complete bounded worker response."""

    maximum_items = _response_item_limit(operation, limit)
    value = _decode_frame(raw, MAX_OUTPUT_BYTES)
    if not isinstance(value, dict):
        raise BilibiliProtocolError("worker_response_invalid")
    if set(value) == _SUCCESS_FIELDS:
        return _decode_success(
            value,
            operation=operation,
            maximum_items=maximum_items,
        )
    if set(value) == _FAILURE_FIELDS:
        return _decode_failure(value, operation=operation)
    raise BilibiliProtocolError("worker_response_invalid")


def _read_request(stream: BinaryIO) -> WorkerRequest:
    header = stream.read(_LENGTH_BYTES)
    if len(header) != _LENGTH_BYTES:
        raise BilibiliProtocolError("worker_request_invalid")
    length = int.from_bytes(header, "big")
    if not 0 < length <= MAX_REQUEST_BYTES:
        raise BilibiliProtocolError("worker_request_invalid")
    payload = stream.read(length)
    if len(payload) != length or stream.read(1):
        raise BilibiliProtocolError("worker_request_invalid")
    return _validated_request(_load_json(payload))


def _validated_request(value: object) -> WorkerRequest:
    if not isinstance(value, dict):
        raise BilibiliProtocolError("worker_request_invalid")
    operation = value.get("operation")
    if (
        value.get("protocol_version") != PROTOCOL_VERSION
        or type(operation) is not str
        or operation not in _REQUEST_OPERATIONS
    ):
        raise BilibiliProtocolError("worker_request_invalid")
    if operation == "search.videos":
        if set(value) != {"protocol_version", "operation", "query", "limit"}:
            raise BilibiliProtocolError("worker_request_invalid")
        return WorkerRequest(
            "search.videos",
            query=_bounded_text(value.get("query"), MAX_QUERY_CHARACTERS),
            limit=_bounded_limit(value.get("limit")),
        )
    if operation == "read.video":
        if set(value) != {"protocol_version", "operation", "url"}:
            raise BilibiliProtocolError("worker_request_invalid")
        url = _bounded_text(value.get("url"), MAX_URL_CHARACTERS)
        if not _valid_video_url(url):
            raise BilibiliProtocolError("worker_request_invalid")
        return WorkerRequest("read.video", url=url)
    if set(value) != {"protocol_version", "operation", "limit"}:
        raise BilibiliProtocolError("worker_request_invalid")
    return WorkerRequest(
        cast(WorkerOperation, operation),
        limit=_bounded_limit(value.get("limit")),
    )


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
        network_access = network_factory()
        maximum_items = _request_item_limit(request)
        limits = limits_factory(
            maximum_items=maximum_items,
            maximum_text_characters=MAX_TEXT_CHARACTERS,
        )
        context = context_factory((network_access,), limits=limits)
        result = api.execute(execution_request, context)

        if type(result) is api.execution_success_type:
            success = cast(_ExecutionSuccess, result)
            items = success.items
            if (
                not _exact_text(success.protocol_version, PROTOCOL_VERSION)
                or not _exact_text(success.source, EXPECTED_SOURCE)
                or not _exact_text(success.operation, request.operation)
                or not _exact_text(success.backend_id, EXPECTED_BACKEND_ID)
                or not _exact_text(success.backend_version, EXPECTED_BACKEND_VERSION)
                or success.partial_error_code is not None
                or type(success.truncated) is not bool
                or type(items) is not tuple
                or len(items) > maximum_items
                or (request.operation == "read.video" and len(items) != 1)
            ):
                return _failure_value(
                    request.operation,
                    "backend_contract_violation",
                )
            projected: list[dict[str, object]] = []
            for raw_item in items:
                if type(raw_item) is not api.execution_item_type:
                    return _failure_value(
                        request.operation,
                        "backend_contract_violation",
                    )
                item = cast(_ExecutionItem, raw_item)
                if not _exact_text(item.schema_id, VIDEO_SCHEMA):
                    return _failure_value(
                        request.operation,
                        "backend_contract_violation",
                    )
                projected.append(_project_execution_fields(item.fields))
            return {
                "backend": _backend_value(),
                "items": projected,
                "operation": request.operation,
                "partial": None,
                "protocol": PROTOCOL_VERSION,
                "schema": VIDEO_SCHEMA,
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
                and _exact_text(failure.backend_version, EXPECTED_BACKEND_VERSION)
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
    if request.operation == "search.videos":
        if request.query is None or request.limit is None:
            raise BilibiliProtocolError("worker_request_invalid")
        return {"query": request.query, "limit": request.limit}
    if request.operation == "read.video":
        if request.url is None:
            raise BilibiliProtocolError("worker_request_invalid")
        return {"url": request.url}
    if request.limit is None:
        raise BilibiliProtocolError("worker_request_invalid")
    return {"limit": request.limit}


def _request_item_limit(request: WorkerRequest) -> int:
    if request.operation == "read.video":
        return 1
    if request.limit is None:
        raise BilibiliProtocolError("worker_request_invalid")
    return _bounded_limit(request.limit)


def _project_execution_fields(value: object) -> dict[str, object]:
    item = _decode_item(value)
    return {
        "author": item.author,
        "duration_seconds": item.duration_seconds,
        "native_id": item.native_id,
        "text": item.text,
        "title": item.title,
        "url": item.url,
        "view_count": item.view_count,
    }


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
    return {"id": EXPECTED_BACKEND_ID, "version": EXPECTED_BACKEND_VERSION}


def _decode_success(
    value: Mapping[str, object],
    *,
    operation: WorkerOperation,
    maximum_items: int,
) -> BilibiliProjection:
    _validate_identity(value, operation)
    _decode_backend(value["backend"])
    if value["schema"] != VIDEO_SCHEMA or value["partial"] is not None:
        raise BilibiliProtocolError("worker_response_invalid")
    truncated = value["truncated"]
    items = value["items"]
    if (
        type(truncated) is not bool
        or not isinstance(items, list)
        or len(items) > maximum_items
        or (operation == "read.video" and len(items) != 1)
    ):
        raise BilibiliProtocolError("worker_response_invalid")
    return BilibiliProjection(
        operation,
        tuple(_decode_item(item) for item in items),
        truncated,
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
        raise BilibiliProtocolError("worker_response_invalid")
    code = error["code"]
    if type(code) is not str or code not in _ERROR_CODES:
        raise BilibiliProtocolError("worker_response_invalid")
    return ForkExecutionFailure(operation, cast(WorkerErrorCode, code))


def _decode_item(value: object) -> BilibiliVideoProjection:
    if (
        not isinstance(value, Mapping)
        or set(value) != _ITEM_FIELDS
        or not all(type(name) is str for name in value)
    ):
        raise BilibiliProtocolError("worker_response_invalid")
    native_id = _decode_bvid(value["native_id"])
    url = value["url"]
    if type(url) is not str or url != _video_url(native_id):
        raise BilibiliProtocolError("worker_response_invalid")
    return BilibiliVideoProjection(
        _decode_required_text(value["text"], MAX_TEXT_CHARACTERS),
        native_id,
        _decode_required_text(value["title"], MAX_TITLE_CHARACTERS),
        url,
        _decode_text(value["author"], MAX_AUTHOR_CHARACTERS, nullable=True),
        _decode_integer(value["duration_seconds"]),
        _decode_integer(value["view_count"]),
    )


def _validate_identity(
    value: Mapping[str, object],
    operation: WorkerOperation,
) -> None:
    if (
        value["protocol"] != PROTOCOL_VERSION
        or value["source"] != EXPECTED_SOURCE
        or value["operation"] != operation
    ):
        raise BilibiliProtocolError("worker_response_invalid")


def _decode_backend(value: object) -> None:
    if (
        not isinstance(value, dict)
        or set(value) != _BACKEND_FIELDS
        or value["id"] != EXPECTED_BACKEND_ID
        or value["version"] != EXPECTED_BACKEND_VERSION
    ):
        raise BilibiliProtocolError("worker_response_invalid")


def _response_item_limit(operation: WorkerOperation, limit: int | None) -> int:
    if operation == "read.video":
        if limit is not None:
            raise BilibiliProtocolError("worker_response_invalid")
        return 1
    if operation not in _REQUEST_OPERATIONS or limit is None:
        raise BilibiliProtocolError("worker_response_invalid")
    try:
        return _bounded_limit(limit)
    except BilibiliProtocolError:
        raise BilibiliProtocolError("worker_response_invalid") from None


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
    ):
        raise BilibiliProtocolError("worker_response_invalid")
    return value


def _decode_required_text(value: object, maximum: int) -> str:
    decoded = _decode_text(value, maximum)
    if decoded is None:
        raise BilibiliProtocolError("worker_response_invalid")
    return decoded


def _decode_bvid(value: object) -> str:
    if type(value) is not str or _BVID.fullmatch(value) is None:
        raise BilibiliProtocolError("worker_response_invalid")
    return value


def _decode_integer(value: object) -> int:
    if type(value) is not int or not 0 <= value <= MAX_NORMALIZED_INTEGER:
        raise BilibiliProtocolError("worker_response_invalid")
    return value


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
        raise BilibiliProtocolError("worker_frame_invalid") from None
    if not 0 < len(payload) <= maximum:
        raise BilibiliProtocolError("worker_frame_invalid")
    return len(payload).to_bytes(_LENGTH_BYTES, "big") + payload


def _decode_frame(value: bytes, maximum: int) -> object:
    if type(value) is not bytes or len(value) < _LENGTH_BYTES + 1:
        raise BilibiliProtocolError("worker_frame_invalid")
    length = int.from_bytes(value[:_LENGTH_BYTES], "big")
    if not 0 < length <= maximum or len(value) != length + _LENGTH_BYTES:
        raise BilibiliProtocolError("worker_frame_invalid")
    return _load_json(value[_LENGTH_BYTES:])


def _load_json(value: bytes) -> object:
    if not 0 < len(value) <= MAX_OUTPUT_BYTES:
        raise BilibiliProtocolError("worker_json_invalid")
    try:
        return json.loads(
            value.decode("utf-8", errors="strict"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (BilibiliProtocolError, UnicodeError, ValueError, RecursionError):
        raise BilibiliProtocolError("worker_json_invalid") from None


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise BilibiliProtocolError("worker_json_invalid")
        result[key] = value
    return result


def _reject_constant(_: str) -> object:
    raise BilibiliProtocolError("worker_json_invalid")


def _bounded_text(value: object, maximum: int) -> str:
    if (
        type(value) is not str
        or value != value.strip()
        or not 0 < len(value) <= maximum
    ):
        raise BilibiliProtocolError("worker_request_invalid")
    if _contains_invalid_scalar(value):
        raise BilibiliProtocolError("worker_request_invalid")
    return value


def _bounded_limit(value: object) -> int:
    if type(value) is not int or not 1 <= value <= MAX_LIMIT:
        raise BilibiliProtocolError("worker_request_invalid")
    return value


def _valid_video_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
    except (UnicodeError, ValueError):
        return False
    parts = parsed.path.split("/")
    return bool(
        parsed.scheme == "https"
        and parsed.netloc == "www.bilibili.com"
        and not parsed.query
        and not parsed.fragment
        and len(parts) == 3
        and parts[1] == "video"
        and _BVID.fullmatch(parts[2])
    )


def _video_url(bvid: str) -> str:
    return f"https://www.bilibili.com/video/{bvid}"


def _contains_invalid_scalar(value: str) -> bool:
    return any(
        character == "\x00" or 0xD800 <= ord(character) <= 0xDFFF for character in value
    )


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
