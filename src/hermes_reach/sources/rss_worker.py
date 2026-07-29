"""Closed Agent-Reach RSS execution over already-fetched feed bytes."""

from __future__ import annotations

import ipaddress
import json
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import BinaryIO, Final, Literal, Protocol, TypeAlias, cast
from urllib.parse import urlsplit

from ..agent_reach_bridge import (
    AgentReachExecutionApi,
    validate_agent_reach_execution_contract,
)

WorkerOperation = Literal["read.feed", "browse.entries"]
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
    "permanent",
    "backend_contract_violation",
]

FRAME_VERSION: Final = 1
FORK_PROTOCOL_VERSION: Final = "v1"
EXPECTED_SOURCE: Final = "rss"
EXPECTED_BACKEND_ID: Final = "feedparser"
EXPECTED_BACKEND_VERSION: Final = "6.0.12"
FEED_SCHEMA: Final = "rss.feed.v1"
ENTRY_SCHEMA: Final = "rss.entry.v1"

MAX_FEED_BYTES: Final = 1_048_576
MAX_METADATA_BYTES: Final = 16_384
MAX_OUTPUT_BYTES: Final = 1_048_576
# One extra projection preserves the runner's existing overflow/truncation signal.
MAX_ENTRIES: Final = 21
MAX_CONTENT_TYPE_CHARACTERS: Final = 512
MAX_CONTENT_LOCATION_CHARACTERS: Final = 8192
MAX_TEXT_CHARACTERS: Final = 16_000
MAX_TITLE_CHARACTERS: Final = 4096
MAX_URL_CHARACTERS: Final = 8192
MAX_NATIVE_ID_CHARACTERS: Final = 512
MAX_AUTHOR_CHARACTERS: Final = 2048
MAX_PUBLISHED_CHARACTERS: Final = 512

_METADATA_LENGTH_BYTES: Final = 4
_REQUEST_FIELDS: Final = frozenset(
    {"content_location", "content_type", "max_entries", "operation", "version"}
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
_FEED_FIELDS: Final = frozenset({"text", "title", "url"})
_ENTRY_FIELDS: Final = frozenset(
    {"author", "native_id", "published_at", "text", "title", "url"}
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
        "permanent",
        "backend_contract_violation",
    }
)


class FeedparserProtocolError(ValueError):
    """The closed worker input or output contract was violated."""


@dataclass(frozen=True, slots=True)
class FeedProjection:
    """Closed feed-level fields returned by the fork."""

    text: str | None
    title: str | None
    url: str | None


@dataclass(frozen=True, slots=True)
class EntryProjection:
    """Closed entry fields returned by the fork."""

    text: str | None
    native_id: str | None
    title: str | None
    url: str | None
    author: str | None
    published_at: str | None


@dataclass(frozen=True, slots=True)
class FeedparserProjection:
    """A fully revalidated successful fork result."""

    operation: WorkerOperation
    feed: FeedProjection | None
    entries: tuple[EntryProjection, ...]
    partial_error_code: Literal["permanent"] | None
    truncated: bool


@dataclass(frozen=True, slots=True)
class ForkExecutionFailure:
    """A fully revalidated, redacted fork failure."""

    operation: WorkerOperation
    error_code: WorkerErrorCode


WorkerResponse: TypeAlias = FeedparserProjection | ForkExecutionFailure


@dataclass(frozen=True, slots=True)
class _WorkerRequest:
    operation: WorkerOperation
    content_type: str
    content_location: str
    max_entries: int
    body: bytes


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
    return validate_agent_reach_execution_contract(validate_runtime_module=True)


def encode_request(
    body: bytes,
    *,
    operation: WorkerOperation,
    content_type: str,
    content_location: str,
    max_entries: int,
) -> bytes:
    """Frame one bounded byte-only fork request for stdin."""

    if type(body) is not bytes or not 0 < len(body) <= MAX_FEED_BYTES:
        raise FeedparserProtocolError("feed_body_invalid")
    metadata = _validated_metadata(
        {
            "content_location": content_location,
            "content_type": content_type,
            "max_entries": max_entries,
            "operation": operation,
            "version": FRAME_VERSION,
        }
    )
    raw_metadata = json.dumps(
        metadata,
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    if not 0 < len(raw_metadata) <= MAX_METADATA_BYTES:
        raise FeedparserProtocolError("feed_metadata_invalid")
    return (
        len(raw_metadata).to_bytes(_METADATA_LENGTH_BYTES, "big") + raw_metadata + body
    )


def decode_response(
    raw: bytes,
    *,
    operation: WorkerOperation,
    max_entries: int,
) -> WorkerResponse:
    """Independently validate the complete bounded worker response."""

    _validate_operation_limit(operation, max_entries, "feed_response_invalid")
    if type(raw) is not bytes or not 0 < len(raw) <= MAX_OUTPUT_BYTES:
        raise FeedparserProtocolError("feed_response_invalid")
    try:
        value = _load_json(raw.decode("utf-8", errors="strict"))
    except (
        UnicodeError,
        json.JSONDecodeError,
        RecursionError,
        FeedparserProtocolError,
    ):
        raise FeedparserProtocolError("feed_response_invalid") from None
    if not isinstance(value, dict):
        raise FeedparserProtocolError("feed_response_invalid")
    if set(value) == _SUCCESS_FIELDS:
        return _decode_success(value, operation=operation, max_entries=max_entries)
    if set(value) == _FAILURE_FIELDS:
        return _decode_failure(value, operation=operation)
    raise FeedparserProtocolError("feed_response_invalid")


def _validated_metadata(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != _REQUEST_FIELDS:
        raise FeedparserProtocolError("feed_metadata_invalid")
    metadata = cast(dict[str, object], value)
    operation = metadata["operation"]
    content_type = metadata["content_type"]
    content_location = metadata["content_location"]
    max_entries = metadata["max_entries"]
    if (
        type(metadata["version"]) is not int
        or metadata["version"] != FRAME_VERSION
        or operation not in {"read.feed", "browse.entries"}
        or type(content_type) is not str
        or len(content_type) > MAX_CONTENT_TYPE_CHARACTERS
        or not content_type.isascii()
        or content_type != content_type.strip()
        or _contains_control(content_type)
        or not _valid_content_location(content_location)
        or type(max_entries) is not int
    ):
        raise FeedparserProtocolError("feed_metadata_invalid")
    _validate_operation_limit(
        cast(WorkerOperation, operation),
        max_entries,
        "feed_metadata_invalid",
    )
    return {
        "content_location": content_location,
        "content_type": content_type,
        "max_entries": max_entries,
        "operation": operation,
        "version": FRAME_VERSION,
    }


def _validate_operation_limit(
    operation: object,
    max_entries: object,
    code: str,
) -> None:
    if (
        type(max_entries) is not int
        or (operation == "read.feed" and max_entries != 1)
        or (operation == "browse.entries" and not 1 <= max_entries <= MAX_ENTRIES)
    ):
        raise FeedparserProtocolError(code)
    if operation not in {"read.feed", "browse.entries"}:
        raise FeedparserProtocolError(code)


def _valid_content_location(value: object) -> bool:
    if (
        type(value) is not str
        or not 0 < len(value) <= MAX_CONTENT_LOCATION_CHARACTERS
        or not value.isascii()
        or value != value.strip()
        or _contains_control(value)
        or "\\" in value
    ):
        return False
    try:
        parsed = urlsplit(value)
        scheme = parsed.scheme.lower()
        hostname = parsed.hostname
        if (
            scheme not in {"http", "https"}
            or parsed.username is not None
            or parsed.password is not None
            or hostname is None
            or parsed.query
            or parsed.fragment
        ):
            return False
        host = hostname.rstrip(".").lower()
        if not host or host == "localhost" or host.endswith((".localhost", ".local")):
            return False
        port = parsed.port or (443 if scheme == "https" else 80)
        if port != (443 if scheme == "https" else 80):
            return False
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            return all(
                0 < len(label) <= 63
                and label[0].isalnum()
                and label[-1].isalnum()
                and all(character.isalnum() or character == "-" for character in label)
                for label in host.split(".")
            )
        return _is_global_address(address)
    except (UnicodeError, ValueError):
        return False


def _read_request(stream: BinaryIO) -> _WorkerRequest:
    header = stream.read(_METADATA_LENGTH_BYTES)
    if len(header) != _METADATA_LENGTH_BYTES:
        raise FeedparserProtocolError("feed_request_invalid")
    metadata_length = int.from_bytes(header, "big")
    if not 0 < metadata_length <= MAX_METADATA_BYTES:
        raise FeedparserProtocolError("feed_request_invalid")
    raw_metadata = stream.read(metadata_length)
    if len(raw_metadata) != metadata_length:
        raise FeedparserProtocolError("feed_request_invalid")
    try:
        loaded = _load_json(raw_metadata.decode("ascii", errors="strict"))
    except (
        UnicodeError,
        json.JSONDecodeError,
        RecursionError,
        FeedparserProtocolError,
    ):
        raise FeedparserProtocolError("feed_request_invalid") from None
    metadata = _validated_metadata(loaded)
    body = stream.read(MAX_FEED_BYTES + 1)
    if not 0 < len(body) <= MAX_FEED_BYTES:
        raise FeedparserProtocolError("feed_body_invalid")
    return _WorkerRequest(
        cast(WorkerOperation, metadata["operation"]),
        cast(str, metadata["content_type"]),
        cast(str, metadata["content_location"]),
        cast(int, metadata["max_entries"]),
        body,
    )


def _execute_request(
    request: _WorkerRequest,
    *,
    execution_api_provider: ExecutionApiProvider | None = None,
) -> Mapping[str, object]:
    # Revalidate the current isolated interpreter immediately before every call.
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
        arguments: dict[str, int] = {}
        if request.operation == "browse.entries":
            arguments["max_entries"] = request.max_entries
        request_factory = cast(Callable[..., object], api.execution_request_type)
        document_factory = cast(Callable[..., object], api.fetched_document_type)
        limits_factory = cast(Callable[..., object], api.execution_limits_type)
        context_factory = cast(Callable[..., object], api.execution_context_type)
        execution_request = request_factory(
            FORK_PROTOCOL_VERSION,
            EXPECTED_SOURCE,
            request.operation,
            arguments,
        )
        document = document_factory(
            request.body,
            request.content_type,
            request.content_location,
        )
        limits = limits_factory(
            maximum_items=request.max_entries,
            maximum_text_characters=MAX_TEXT_CHARACTERS,
        )
        context = context_factory((document,), limits=limits)
        result = api.execute(execution_request, context)

        if type(result) is api.execution_success_type:
            success = cast(_ExecutionSuccess, result)
            expected_schema = _schema_for(request.operation)
            items = success.items
            if (
                not _exact_text(success.protocol_version, FORK_PROTOCOL_VERSION)
                or not _exact_text(success.source, EXPECTED_SOURCE)
                or not _exact_text(success.operation, request.operation)
                or not _exact_text(success.backend_id, EXPECTED_BACKEND_ID)
                or not _exact_text(success.backend_version, EXPECTED_BACKEND_VERSION)
                or not _valid_partial_error(success.partial_error_code)
                or type(success.truncated) is not bool
                or (request.operation == "read.feed" and success.truncated)
                or type(items) is not tuple
                or len(items) > request.max_entries
                or (request.operation == "read.feed" and len(items) != 1)
                or (success.partial_error_code is not None and not items)
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
                if not _exact_text(item.schema_id, expected_schema):
                    return _failure_value(
                        request.operation,
                        "backend_contract_violation",
                    )
                projected.append(
                    _project_execution_fields(request.operation, item.fields)
                )
            return {
                "backend": _backend_value(),
                "items": projected,
                "operation": request.operation,
                "partial": success.partial_error_code,
                "protocol": FORK_PROTOCOL_VERSION,
                "schema": expected_schema,
                "source": EXPECTED_SOURCE,
                "truncated": success.truncated,
            }

        if type(result) is api.execution_failure_type:
            failure = cast(_ExecutionFailure, result)
            if (
                _exact_text(failure.protocol_version, FORK_PROTOCOL_VERSION)
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


def _project_execution_fields(
    operation: WorkerOperation,
    value: object,
) -> dict[str, object]:
    expected_fields = _FEED_FIELDS if operation == "read.feed" else _ENTRY_FIELDS
    if (
        not isinstance(value, Mapping)
        or set(value) != expected_fields
        or not all(type(name) is str for name in value)
    ):
        raise FeedparserProtocolError("feed_response_invalid")
    if operation == "read.feed":
        return {
            "text": _decode_string(value["text"], MAX_TEXT_CHARACTERS),
            "title": _decode_string(value["title"], MAX_TITLE_CHARACTERS),
            "url": _decode_string(value["url"], MAX_URL_CHARACTERS),
        }
    return {
        "author": _decode_string(value["author"], MAX_AUTHOR_CHARACTERS),
        "native_id": _decode_string(
            value["native_id"],
            MAX_NATIVE_ID_CHARACTERS,
        ),
        "published_at": _decode_string(
            value["published_at"],
            MAX_PUBLISHED_CHARACTERS,
        ),
        "text": _decode_string(value["text"], MAX_TEXT_CHARACTERS),
        "title": _decode_string(value["title"], MAX_TITLE_CHARACTERS),
        "url": _decode_string(value["url"], MAX_URL_CHARACTERS),
    }


def _exact_text(value: object, expected: str) -> bool:
    return type(value) is str and value == expected


def _valid_partial_error(value: object) -> bool:
    return value is None or _exact_text(value, "permanent")


def _backend_value() -> dict[str, str]:
    return {"id": EXPECTED_BACKEND_ID, "version": EXPECTED_BACKEND_VERSION}


def _failure_value(
    operation: WorkerOperation,
    error_code: WorkerErrorCode,
) -> dict[str, object]:
    return {
        "backend": _backend_value(),
        "error": {"code": error_code},
        "operation": operation,
        "protocol": FORK_PROTOCOL_VERSION,
        "source": EXPECTED_SOURCE,
    }


def _response_bytes(value: Mapping[str, object]) -> bytes:
    try:
        raw = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8", errors="strict")
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise FeedparserProtocolError("feed_response_invalid") from None
    if not 0 < len(raw) <= MAX_OUTPUT_BYTES:
        raise FeedparserProtocolError("feed_response_too_large")
    return raw


def _decode_success(
    response: Mapping[str, object],
    *,
    operation: WorkerOperation,
    max_entries: int,
) -> FeedparserProjection:
    _validate_identity(response, operation)
    _decode_backend(response["backend"])
    expected_schema = _schema_for(operation)
    if response["schema"] != expected_schema:
        raise FeedparserProtocolError("feed_response_invalid")
    partial = response["partial"]
    if partial is not None and partial != "permanent":
        raise FeedparserProtocolError("feed_response_invalid")
    truncated = response["truncated"]
    if type(truncated) is not bool:
        raise FeedparserProtocolError("feed_response_invalid")
    items = response["items"]
    if not isinstance(items, list) or len(items) > max_entries:
        raise FeedparserProtocolError("feed_response_invalid")
    if partial is not None and not items:
        raise FeedparserProtocolError("feed_response_invalid")

    if operation == "read.feed":
        if len(items) != 1 or truncated:
            raise FeedparserProtocolError("feed_response_invalid")
        return FeedparserProjection(
            operation,
            _decode_feed(items[0]),
            (),
            cast(Literal["permanent"] | None, partial),
            False,
        )
    return FeedparserProjection(
        operation,
        None,
        tuple(_decode_entry(item) for item in items),
        cast(Literal["permanent"] | None, partial),
        truncated,
    )


def _decode_failure(
    response: Mapping[str, object],
    *,
    operation: WorkerOperation,
) -> ForkExecutionFailure:
    _validate_identity(response, operation)
    _decode_backend(response["backend"])
    error = response["error"]
    if not isinstance(error, dict) or set(error) != _ERROR_FIELDS:
        raise FeedparserProtocolError("feed_response_invalid")
    code = error["code"]
    if type(code) is not str or code not in _ERROR_CODES:
        raise FeedparserProtocolError("feed_response_invalid")
    return ForkExecutionFailure(operation, cast(WorkerErrorCode, code))


def _validate_identity(
    response: Mapping[str, object],
    operation: WorkerOperation,
) -> None:
    if (
        response["protocol"] != FORK_PROTOCOL_VERSION
        or response["source"] != EXPECTED_SOURCE
        or response["operation"] != operation
    ):
        raise FeedparserProtocolError("feed_response_invalid")


def _decode_backend(value: object) -> None:
    if (
        not isinstance(value, dict)
        or set(value) != _BACKEND_FIELDS
        or value["id"] != EXPECTED_BACKEND_ID
        or value["version"] != EXPECTED_BACKEND_VERSION
    ):
        raise FeedparserProtocolError("feed_response_invalid")


def _schema_for(operation: WorkerOperation) -> str:
    return FEED_SCHEMA if operation == "read.feed" else ENTRY_SCHEMA


def _decode_feed(value: object) -> FeedProjection:
    if not isinstance(value, dict) or set(value) != _FEED_FIELDS:
        raise FeedparserProtocolError("feed_response_invalid")
    feed = cast(dict[str, object], value)
    return FeedProjection(
        _decode_string(feed["text"], MAX_TEXT_CHARACTERS),
        _decode_string(feed["title"], MAX_TITLE_CHARACTERS),
        _decode_string(feed["url"], MAX_URL_CHARACTERS),
    )


def _decode_entry(value: object) -> EntryProjection:
    if not isinstance(value, dict) or set(value) != _ENTRY_FIELDS:
        raise FeedparserProtocolError("feed_response_invalid")
    entry = cast(dict[str, object], value)
    return EntryProjection(
        _decode_string(entry["text"], MAX_TEXT_CHARACTERS),
        _decode_string(entry["native_id"], MAX_NATIVE_ID_CHARACTERS),
        _decode_string(entry["title"], MAX_TITLE_CHARACTERS),
        _decode_string(entry["url"], MAX_URL_CHARACTERS),
        _decode_string(entry["author"], MAX_AUTHOR_CHARACTERS),
        _decode_string(entry["published_at"], MAX_PUBLISHED_CHARACTERS),
    )


def _decode_string(value: object, maximum: int) -> str | None:
    if value is None:
        return None
    if (
        type(value) is not str
        or not 0 < len(value) <= maximum
        or _contains_invalid_scalar(value)
    ):
        raise FeedparserProtocolError("feed_response_invalid")
    return value


def _contains_control(value: str) -> bool:
    return any(ord(character) < 32 or ord(character) == 127 for character in value)


def _contains_invalid_scalar(value: str) -> bool:
    return any(
        character == "\x00" or 0xD800 <= ord(character) <= 0xDFFF for character in value
    )


def _load_json(raw: str) -> object:
    return json.loads(
        raw,
        object_pairs_hook=_unique_object,
        parse_constant=_reject_constant,
    )


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for name, item in pairs:
        if name in value:
            raise FeedparserProtocolError("feed_json_duplicate_key")
        value[name] = item
    return value


def _reject_constant(_: str) -> object:
    raise FeedparserProtocolError("feed_json_constant_invalid")


def _is_global_address(
    address: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> bool:
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return _is_global_address(address.ipv4_mapped)
    return bool(
        address.is_global
        and not address.is_private
        and not address.is_loopback
        and not address.is_link_local
        and not address.is_reserved
        and not address.is_multicast
        and not address.is_unspecified
    )


def _main() -> int:
    try:
        request = _read_request(sys.stdin.buffer)
    except Exception:
        return 1
    try:
        value = _execute_request(request)
    except Exception:
        value = _failure_value(request.operation, "backend_contract_violation")
    try:
        output = _response_bytes(value)
        sys.stdout.buffer.write(output)
        sys.stdout.buffer.flush()
        return 0
    except Exception:
        return 1


if __name__ == "__main__":
    raise SystemExit(_main())
