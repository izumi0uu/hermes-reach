"""Closed Agent-Reach Xueqiu execution inside an isolated worker."""

from __future__ import annotations

import json
import math
import re
import signal
import sys
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Final, Literal, Protocol, TypeAlias, cast

from ..agent_reach_bridge import (
    AgentReachExecutionApi,
    validate_agent_reach_execution_contract,
)

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
EXPECTED_SOURCE: Final = "xueqiu"
EXPECTED_OPERATION: Final = "search.stocks"
EXPECTED_BACKEND_ID: Final = "xueqiu-api"
EXPECTED_BACKEND_VERSION: Final = "1.5.0+search.v1"
EXPECTED_SCHEMA_ID: Final = "xueqiu.stock.v1"

MAX_REQUEST_BYTES: Final = 32 * 1024
MAX_OUTPUT_BYTES: Final = 512 * 1024
MAX_QUERY_CHARACTERS: Final = 4_096
MAX_QUERY_BYTES: Final = 16_384
MAX_COOKIE_BYTES: Final = 8_192
MAX_LIMIT: Final = 50
MAX_NAME_CHARACTERS: Final = 4_096
MAX_SYMBOL_CHARACTERS: Final = 64
MAX_EXCHANGE_CHARACTERS: Final = 16
MAX_JSON_DEPTH: Final = 8
MAX_JSON_NODES: Final = 512
MAX_JSON_STRING_BYTES: Final = 16_384

_LENGTH_BYTES: Final = 4
_REQUEST_MAGIC: Final = b"HXQ1"
_REQUEST_FIXED_BYTES: Final = len(_REQUEST_MAGIC) + 8 + 1 + 2 + 2
_CANCELLATION_REQUESTED = threading.Event()
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
_SUCCESS_FIELDS: Final = frozenset(
    {"backend", "items", "operation", "protocol", "schema", "source", "truncated"}
)
_FAILURE_FIELDS: Final = frozenset(
    {"backend", "error", "operation", "protocol", "source"}
)
_BACKEND_FIELDS: Final = frozenset({"id", "version"})
_ERROR_FIELDS: Final = frozenset({"code"})
_ITEM_FIELDS: Final = frozenset({"exchange", "name", "symbol"})
_MAINLAND_SYMBOL: Final = re.compile(r"(SH|SZ|BJ)[0-9]{6}")
_QUALIFIED_SYMBOL: Final = re.compile(r"([A-Z]{2,16}):[A-Z0-9]+(?:[.-][A-Z0-9]+)*")
_EXCHANGE: Final = re.compile(r"[A-Z]{2,16}")


class XueqiuProtocolError(ValueError):
    """The closed worker input or output contract was violated."""


class _WorkerCancellationRequested(Exception):
    pass


class _DeadlineExpired(Exception):
    pass


@dataclass(frozen=True, slots=True, repr=False)
class WorkerRequest:
    """One secret-bearing worker request with an explicit mutable lifetime."""

    query: str
    limit: int
    deadline: float
    cookie_header: bytearray = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if (
            not _valid_query(self.query)
            or type(self.limit) is not int
            or not 1 <= self.limit <= MAX_LIMIT
            or not _valid_deadline(self.deadline)
            or type(self.cookie_header) is not bytearray
            or not _valid_cookie_bytes(self.cookie_header)
        ):
            if type(self.cookie_header) is bytearray:
                _zero(self.cookie_header)
            raise XueqiuProtocolError("worker_request_invalid")

    def close(self) -> None:
        _zero(self.cookie_header)

    def __repr__(self) -> str:
        return "WorkerRequest(<redacted>)"


@dataclass(frozen=True, slots=True)
class XueqiuStockProjection:
    """One independently validated stock projection."""

    symbol: str
    name: str
    exchange: str

    def __post_init__(self) -> None:
        if not _valid_stock(self.symbol, self.name, self.exchange):
            raise XueqiuProtocolError("worker_response_invalid")


@dataclass(frozen=True, slots=True)
class XueqiuProjection:
    """A fully revalidated successful fork result."""

    items: tuple[XueqiuStockProjection, ...]
    truncated: bool

    def __post_init__(self) -> None:
        if (
            type(self.items) is not tuple
            or len(self.items) > MAX_LIMIT
            or any(type(item) is not XueqiuStockProjection for item in self.items)
            or len({item.symbol for item in self.items}) != len(self.items)
            or type(self.truncated) is not bool
        ):
            raise XueqiuProtocolError("worker_response_invalid")


@dataclass(frozen=True, slots=True)
class ForkExecutionFailure:
    """A fully revalidated, redacted fork failure."""

    error_code: WorkerErrorCode


WorkerResponse: TypeAlias = XueqiuProjection | ForkExecutionFailure


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


class _BinaryInput(Protocol):
    def readinto(self, buffer: bytearray | memoryview) -> int | None: ...


ExecutionApiProvider = Callable[[], AgentReachExecutionApi]


def _load_execution_api() -> AgentReachExecutionApi:
    return validate_agent_reach_execution_contract(runtime_module="xueqiu")


def encode_request(
    query: str,
    limit: int,
    cookie_header: str,
    *,
    deadline: float,
) -> bytearray:
    """Encode one closed request without creating an immutable Cookie copy."""

    if (
        not _valid_query(query)
        or type(limit) is not int
        or not 1 <= limit <= MAX_LIMIT
        or not _valid_deadline(deadline)
        or type(cookie_header) is not str
        or not _valid_cookie_text(cookie_header)
    ):
        raise XueqiuProtocolError("worker_request_invalid")
    try:
        query_bytes = query.encode("utf-8", errors="strict")
    except UnicodeError:
        raise XueqiuProtocolError("worker_request_invalid") from None
    if not 0 < len(query_bytes) <= MAX_QUERY_BYTES:
        raise XueqiuProtocolError("worker_request_invalid")

    frame = bytearray(_LENGTH_BYTES)
    try:
        frame.extend(_REQUEST_MAGIC)
        frame.extend(math.ceil(deadline * 1_000_000_000).to_bytes(8, "big"))
        frame.append(limit)
        frame.extend(len(query_bytes).to_bytes(2, "big"))
        frame.extend(len(cookie_header).to_bytes(2, "big"))
        frame.extend(query_bytes)
        for character in cookie_header:
            frame.append(ord(character))
        payload_size = len(frame) - _LENGTH_BYTES
        if not 0 < payload_size <= MAX_REQUEST_BYTES:
            raise XueqiuProtocolError("worker_request_invalid")
        frame[:_LENGTH_BYTES] = payload_size.to_bytes(_LENGTH_BYTES, "big")
        return frame
    except Exception:
        _zero(frame)
        raise


def decode_response(raw: bytes | bytearray, *, limit: int) -> WorkerResponse:
    """Independently validate the complete bounded worker response."""

    if type(limit) is not int or not 1 <= limit <= MAX_LIMIT:
        raise XueqiuProtocolError("worker_response_invalid")
    value = _decode_frame(raw, MAX_OUTPUT_BYTES)
    if not isinstance(value, dict):
        raise XueqiuProtocolError("worker_response_invalid")
    if set(value) == _SUCCESS_FIELDS:
        return _decode_success(value, limit=limit)
    if set(value) == _FAILURE_FIELDS:
        return _decode_failure(value)
    raise XueqiuProtocolError("worker_response_invalid")


def _read_request(stream: _BinaryInput) -> WorkerRequest:
    header = bytearray(_LENGTH_BYTES)
    payload = bytearray()
    tail = bytearray(1)
    cookie = bytearray()
    try:
        _read_exact_into(stream, header)
        length = int.from_bytes(header, "big")
        if not _REQUEST_FIXED_BYTES < length <= MAX_REQUEST_BYTES:
            raise XueqiuProtocolError("worker_request_invalid")
        payload = bytearray(length)
        _read_exact_into(stream, payload)
        if _read_some_into(stream, tail) != 0:
            raise XueqiuProtocolError("worker_request_invalid")
        if payload[: len(_REQUEST_MAGIC)] != _REQUEST_MAGIC:
            raise XueqiuProtocolError("worker_request_invalid")

        offset = len(_REQUEST_MAGIC)
        deadline_ns = int.from_bytes(payload[offset : offset + 8], "big")
        offset += 8
        limit = payload[offset]
        offset += 1
        query_length = int.from_bytes(payload[offset : offset + 2], "big")
        offset += 2
        cookie_length = int.from_bytes(payload[offset : offset + 2], "big")
        offset += 2
        if (
            not 0 < query_length <= MAX_QUERY_BYTES
            or not 0 < cookie_length <= MAX_COOKIE_BYTES
            or offset + query_length + cookie_length != len(payload)
        ):
            raise XueqiuProtocolError("worker_request_invalid")
        query = bytes(payload[offset : offset + query_length]).decode(
            "utf-8", errors="strict"
        )
        offset += query_length
        cookie = bytearray(cookie_length)
        with (
            memoryview(payload) as payload_view,
            memoryview(cookie) as cookie_view,
            payload_view[offset : offset + cookie_length] as cookie_source,
        ):
            cookie_view[:] = cookie_source
        return WorkerRequest(query, limit, deadline_ns / 1_000_000_000, cookie)
    except (UnicodeError, OverflowError):
        _zero(cookie)
        raise XueqiuProtocolError("worker_request_invalid") from None
    except Exception:
        _zero(cookie)
        raise
    finally:
        _zero(header)
        _zero(payload)
        _zero(tail)


def _execute_request(
    request: WorkerRequest,
    *,
    execution_api_provider: ExecutionApiProvider | None = None,
) -> Mapping[str, object]:
    provider = execution_api_provider or _load_execution_api
    session: object | None = None
    try:
        _worker_checkpoint(request.deadline)
        api = provider()
        _worker_checkpoint(request.deadline)
        request_factory = cast(Callable[..., object], api.execution_request_type)
        limits_factory = cast(Callable[..., object], api.execution_limits_type)
        context_factory = cast(Callable[..., object], api.execution_context_type)
        session_factory = cast(Callable[[bytearray], object], api.xueqiu_session_type)
        execution_request = request_factory(
            PROTOCOL_VERSION,
            EXPECTED_SOURCE,
            EXPECTED_OPERATION,
            {"query": request.query, "limit": request.limit},
        )
        limits = limits_factory(
            maximum_items=request.limit,
            maximum_text_characters=16_000,
        )

        def checkpoint() -> None:
            _worker_checkpoint(request.deadline)

        session = session_factory(request.cookie_header)
        context = context_factory(
            (session,),
            checkpoint=checkpoint,
            limits=limits,
        )
        result = api.execute(execution_request, context)
        _worker_checkpoint(request.deadline)
        if type(result) is api.execution_success_type:
            return _success_value(
                cast(_ExecutionSuccess, result),
                api=api,
                limit=request.limit,
            )
        if type(result) is api.execution_failure_type:
            failure = cast(_ExecutionFailure, result)
            if _valid_execution_identity(failure):
                code = failure.error_code
                if type(code) is str and code in _ERROR_CODES:
                    return _failure_value(cast(WorkerErrorCode, code))
    except _WorkerCancellationRequested:
        return _failure_value("cancelled")
    except _DeadlineExpired:
        return _failure_value("deadline_exceeded")
    except Exception:
        return _failure_value("backend_contract_violation")
    finally:
        request.close()
        _close_session(session)
    return _failure_value("backend_contract_violation")


def _success_value(
    success: _ExecutionSuccess,
    *,
    api: AgentReachExecutionApi,
    limit: int,
) -> Mapping[str, object]:
    items = success.items
    if (
        not _valid_execution_identity(success)
        or success.partial_error_code is not None
        or type(success.truncated) is not bool
        or type(items) is not tuple
        or len(items) > limit
    ):
        return _failure_value("backend_contract_violation")
    projected: list[XueqiuStockProjection] = []
    seen: set[str] = set()
    try:
        for raw_item in items:
            if type(raw_item) is not api.execution_item_type:
                raise XueqiuProtocolError("fork_result_invalid")
            item = cast(_ExecutionItem, raw_item)
            if item.schema_id != EXPECTED_SCHEMA_ID:
                raise XueqiuProtocolError("fork_result_invalid")
            stock = _decode_stock(item.fields)
            if stock.symbol in seen:
                raise XueqiuProtocolError("fork_result_invalid")
            seen.add(stock.symbol)
            projected.append(stock)
    except XueqiuProtocolError:
        return _failure_value("backend_contract_violation")
    return {
        "backend": _backend_value(),
        "items": [_stock_value(item) for item in projected],
        "operation": EXPECTED_OPERATION,
        "protocol": PROTOCOL_VERSION,
        "schema": EXPECTED_SCHEMA_ID,
        "source": EXPECTED_SOURCE,
        "truncated": success.truncated,
    }


def _decode_success(value: Mapping[str, object], *, limit: int) -> XueqiuProjection:
    _validate_identity(value)
    _decode_backend(value["backend"])
    if value["schema"] != EXPECTED_SCHEMA_ID or type(value["truncated"]) is not bool:
        raise XueqiuProtocolError("worker_response_invalid")
    items = value["items"]
    if not isinstance(items, list) or len(items) > limit:
        raise XueqiuProtocolError("worker_response_invalid")
    selected = tuple(_decode_stock(item) for item in items)
    if len({item.symbol for item in selected}) != len(selected):
        raise XueqiuProtocolError("worker_response_invalid")
    return XueqiuProjection(selected, value["truncated"])


def _decode_failure(value: Mapping[str, object]) -> ForkExecutionFailure:
    _validate_identity(value)
    _decode_backend(value["backend"])
    error = value["error"]
    if not isinstance(error, dict) or set(error) != _ERROR_FIELDS:
        raise XueqiuProtocolError("worker_response_invalid")
    code = error["code"]
    if type(code) is not str or code not in _ERROR_CODES:
        raise XueqiuProtocolError("worker_response_invalid")
    return ForkExecutionFailure(cast(WorkerErrorCode, code))


def _decode_stock(value: object) -> XueqiuStockProjection:
    if (
        not isinstance(value, Mapping)
        or set(value) != _ITEM_FIELDS
        or any(type(key) is not str for key in value)
    ):
        raise XueqiuProtocolError("worker_response_invalid")
    return XueqiuStockProjection(
        _required_text(value["symbol"], MAX_SYMBOL_CHARACTERS),
        _required_text(value["name"], MAX_NAME_CHARACTERS),
        _required_text(value["exchange"], MAX_EXCHANGE_CHARACTERS),
    )


def _stock_value(stock: XueqiuStockProjection) -> dict[str, str]:
    return {"exchange": stock.exchange, "name": stock.name, "symbol": stock.symbol}


def _valid_execution_identity(value: _ExecutionSuccess | _ExecutionFailure) -> bool:
    return bool(
        type(value.protocol_version) is str
        and value.protocol_version == PROTOCOL_VERSION
        and type(value.source) is str
        and value.source == EXPECTED_SOURCE
        and type(value.operation) is str
        and value.operation == EXPECTED_OPERATION
        and type(value.backend_id) is str
        and value.backend_id == EXPECTED_BACKEND_ID
        and type(value.backend_version) is str
        and value.backend_version == EXPECTED_BACKEND_VERSION
    )


def _validate_identity(value: Mapping[str, object]) -> None:
    if (
        value.get("protocol") != PROTOCOL_VERSION
        or value.get("source") != EXPECTED_SOURCE
        or value.get("operation") != EXPECTED_OPERATION
    ):
        raise XueqiuProtocolError("worker_response_invalid")


def _backend_value() -> dict[str, str]:
    return {"id": EXPECTED_BACKEND_ID, "version": EXPECTED_BACKEND_VERSION}


def _decode_backend(value: object) -> None:
    if (
        not isinstance(value, Mapping)
        or set(value) != _BACKEND_FIELDS
        or value.get("id") != EXPECTED_BACKEND_ID
        or value.get("version") != EXPECTED_BACKEND_VERSION
    ):
        raise XueqiuProtocolError("worker_response_invalid")


def _failure_value(error_code: WorkerErrorCode) -> dict[str, object]:
    return {
        "backend": _backend_value(),
        "error": {"code": error_code},
        "operation": EXPECTED_OPERATION,
        "protocol": PROTOCOL_VERSION,
        "source": EXPECTED_SOURCE,
    }


def _valid_query(value: object) -> bool:
    return bool(
        type(value) is str
        and value == value.strip()
        and 1 <= len(value) <= MAX_QUERY_CHARACTERS
        and not _contains_invalid_scalar(value)
    )


def _valid_deadline(value: object) -> bool:
    if type(value) not in {int, float}:
        return False
    numeric = cast(int | float, value)
    return bool(
        math.isfinite(float(numeric)) and 0 < float(numeric) < (1 << 64) / 1_000_000_000
    )


def _valid_cookie_text(value: str) -> bool:
    if (
        not 1 <= len(value) <= MAX_COOKIE_BYTES
        or any(ord(character) < 32 or ord(character) > 126 for character in value)
        or value[0] == " "
        or value[-1] == " "
    ):
        return False
    names: list[tuple[int, int]] = []
    token_seen = False
    start = 0
    while start < len(value):
        raw_end = value.find(";", start)
        has_delimiter = raw_end >= 0
        if not has_delimiter:
            raw_end = len(value)
        end = raw_end
        while start < end and value[start] == " ":
            start += 1
        while end > start and value[end - 1] == " ":
            end -= 1
        separator = value.find("=", start, end)
        if (
            separator < 0
            or not start < separator
            or separator + 1 >= end
            or separator - start > 64
        ):
            return False
        if any(
            not _cookie_name_byte(ord(value[index]))
            for index in range(start, separator)
        ):
            return False
        if any(value[index] in {" ", ",", ";"} for index in range(separator + 1, end)):
            return False
        if any(
            _same_text(value, start, separator, old_start, old_end)
            for old_start, old_end in names
        ):
            return False
        names.append((start, separator))
        token_seen = token_seen or _matches_text(value, start, separator, "xq_a_token")
        start = raw_end + 1
        if has_delimiter and start == len(value):
            return False
    return token_seen


def _valid_cookie_bytes(value: bytearray) -> bool:
    if not 1 <= len(value) <= MAX_COOKIE_BYTES:
        return False
    if (
        any(byte < 32 or byte > 126 for byte in value)
        or value[0] == 32
        or value[-1] == 32
    ):
        return False
    names: list[tuple[int, int]] = []
    token_seen = False
    start = 0
    while start < len(value):
        raw_end = value.find(b";", start)
        has_delimiter = raw_end >= 0
        if not has_delimiter:
            raw_end = len(value)
        end = raw_end
        while start < end and value[start] == 32:
            start += 1
        while end > start and value[end - 1] == 32:
            end -= 1
        separator = value.find(b"=", start, end)
        if (
            separator < 0
            or not start < separator
            or separator + 1 >= end
            or separator - start > 64
        ):
            return False
        if any(
            not _cookie_name_byte(value[index]) for index in range(start, separator)
        ):
            return False
        if any(value[index] in {32, 44, 59} for index in range(separator + 1, end)):
            return False
        if any(
            _same_bytes(value, start, separator, old_start, old_end)
            for old_start, old_end in names
        ):
            return False
        names.append((start, separator))
        token_seen = token_seen or _matches_ascii(
            value, start, separator, b"xq_a_token"
        )
        start = raw_end + 1
        if has_delimiter and start == len(value):
            return False
    return token_seen


def _cookie_name_byte(value: int) -> bool:
    return 48 <= value <= 57 or 65 <= value <= 90 or 97 <= value <= 122 or value == 95


def _same_bytes(
    value: bytearray,
    first_start: int,
    first_end: int,
    second_start: int,
    second_end: int,
) -> bool:
    if first_end - first_start != second_end - second_start:
        return False
    return all(
        value[first_start + offset] == value[second_start + offset]
        for offset in range(first_end - first_start)
    )


def _same_text(
    value: str,
    first_start: int,
    first_end: int,
    second_start: int,
    second_end: int,
) -> bool:
    if first_end - first_start != second_end - second_start:
        return False
    return all(
        value[first_start + offset] == value[second_start + offset]
        for offset in range(first_end - first_start)
    )


def _matches_ascii(value: bytearray, start: int, end: int, expected: bytes) -> bool:
    return end - start == len(expected) and all(
        value[start + offset] == expected[offset] for offset in range(len(expected))
    )


def _matches_text(value: str, start: int, end: int, expected: str) -> bool:
    return end - start == len(expected) and all(
        value[start + offset] == expected[offset] for offset in range(len(expected))
    )


def _valid_stock(symbol: object, name: object, exchange: object) -> bool:
    if (
        type(symbol) is not str
        or not symbol.isascii()
        or symbol != symbol.strip()
        or not 1 <= len(symbol) <= MAX_SYMBOL_CHARACTERS
        or _contains_invalid_scalar(symbol)
        or type(name) is not str
        or name != name.strip()
        or not 1 <= len(name) <= MAX_NAME_CHARACTERS
        or _contains_invalid_scalar(name)
        or type(exchange) is not str
        or exchange != exchange.strip()
        or not exchange.isascii()
        or _EXCHANGE.fullmatch(exchange) is None
    ):
        return False
    mainland = _MAINLAND_SYMBOL.fullmatch(symbol)
    if mainland is not None:
        prefix = mainland.group(1)
        return exchange in {prefix, f"{prefix}A"}
    qualified = _QUALIFIED_SYMBOL.fullmatch(symbol)
    return qualified is not None and qualified.group(1) == exchange


def _required_text(value: object, maximum: int) -> str:
    if (
        type(value) is not str
        or value != value.strip()
        or not 1 <= len(value) <= maximum
        or _contains_invalid_scalar(value)
    ):
        raise XueqiuProtocolError("worker_response_invalid")
    return value


def _contains_invalid_scalar(value: str) -> bool:
    return any(
        character == "\x00" or 0xD800 <= ord(character) <= 0xDFFF for character in value
    )


def _read_exact_into(stream: _BinaryInput, target: bytearray) -> None:
    view = memoryview(target)
    offset = 0
    try:
        while offset < len(target):
            count = stream.readinto(view[offset:])
            if type(count) is not int or count <= 0 or count > len(target) - offset:
                raise XueqiuProtocolError("worker_request_invalid")
            offset += count
    finally:
        view.release()


def _read_some_into(stream: _BinaryInput, target: bytearray) -> int:
    count = stream.readinto(target)
    if type(count) is not int or count < 0 or count > len(target):
        raise XueqiuProtocolError("worker_request_invalid")
    return count


def _encode_frame(value: Mapping[str, object], maximum: int) -> bytes:
    try:
        payload = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8", errors="strict")
    except (TypeError, UnicodeError, ValueError, RecursionError):
        raise XueqiuProtocolError("worker_frame_invalid") from None
    if not 0 < len(payload) <= maximum:
        raise XueqiuProtocolError("worker_frame_invalid")
    return len(payload).to_bytes(_LENGTH_BYTES, "big") + payload


def _decode_frame(raw: bytes | bytearray, maximum: int) -> object:
    if not isinstance(raw, bytes | bytearray) or len(raw) < _LENGTH_BYTES + 1:
        raise XueqiuProtocolError("worker_frame_invalid")
    length = int.from_bytes(raw[:_LENGTH_BYTES], "big")
    if not 0 < length <= maximum or len(raw) != _LENGTH_BYTES + length:
        raise XueqiuProtocolError("worker_frame_invalid")
    return _load_json(raw[_LENGTH_BYTES:], maximum=maximum)


def _load_json(raw: bytes | bytearray, *, maximum: int) -> object:
    if not 0 < len(raw) <= maximum:
        raise XueqiuProtocolError("worker_json_invalid")
    try:
        value = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (XueqiuProtocolError, UnicodeError, ValueError, RecursionError):
        raise XueqiuProtocolError("worker_json_invalid") from None
    _validate_json_shape(value)
    return value


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise XueqiuProtocolError("worker_json_invalid")
        value[key] = item
    return value


def _reject_constant(_: str) -> object:
    raise XueqiuProtocolError("worker_json_invalid")


def _validate_json_shape(value: object) -> None:
    pending: list[tuple[object, int]] = [(value, 1)]
    nodes = 0
    while pending:
        current, depth = pending.pop()
        nodes += 1
        if nodes > MAX_JSON_NODES or depth > MAX_JSON_DEPTH:
            raise XueqiuProtocolError("worker_json_invalid")
        if current is None or type(current) in {bool, int}:
            continue
        if type(current) is float:
            if not math.isfinite(current):
                raise XueqiuProtocolError("worker_json_invalid")
            continue
        if type(current) is str:
            if len(
                current.encode("utf-8", errors="strict")
            ) > MAX_JSON_STRING_BYTES or _contains_invalid_scalar(current):
                raise XueqiuProtocolError("worker_json_invalid")
            continue
        if isinstance(current, list):
            pending.extend((item, depth + 1) for item in current)
            continue
        if isinstance(current, dict):
            pending.extend((item, depth + 1) for item in current.values())
            continue
        raise XueqiuProtocolError("worker_json_invalid")


def _worker_checkpoint(deadline: float) -> None:
    if _CANCELLATION_REQUESTED.is_set():
        raise _WorkerCancellationRequested
    if time.monotonic() >= deadline:
        raise _DeadlineExpired


def _request_worker_cancellation(_signum: int, _frame: object) -> None:
    _CANCELLATION_REQUESTED.set()


def _close_session(session: object | None) -> None:
    close = getattr(session, "close", None)
    if callable(close):
        try:
            close()
        except Exception:
            pass


def _zero(value: bytearray) -> None:
    value[:] = b"\x00" * len(value)


def _main() -> int:
    request: WorkerRequest | None = None
    try:
        signal.signal(signal.SIGTERM, _request_worker_cancellation)
        request = _read_request(cast(_BinaryInput, sys.stdin.buffer))
        value = _execute_request(request)
        try:
            output = _encode_frame(value, MAX_OUTPUT_BYTES)
        except XueqiuProtocolError:
            output = _encode_frame(
                _failure_value("backend_contract_violation"), MAX_OUTPUT_BYTES
            )
        sys.stdout.buffer.write(output)
        sys.stdout.buffer.flush()
        return 0
    except Exception:
        return 1
    finally:
        if request is not None:
            request.close()


if __name__ == "__main__":
    raise SystemExit(_main())


__all__ = [
    "EXPECTED_BACKEND_ID",
    "EXPECTED_BACKEND_VERSION",
    "EXPECTED_OPERATION",
    "EXPECTED_SCHEMA_ID",
    "ForkExecutionFailure",
    "MAX_OUTPUT_BYTES",
    "WorkerErrorCode",
    "WorkerRequest",
    "WorkerResponse",
    "XueqiuProjection",
    "XueqiuProtocolError",
    "XueqiuStockProjection",
    "decode_response",
    "encode_request",
]
