"""Closed Agent-Reach Exa execution inside an isolated worker."""

from __future__ import annotations

import ipaddress
import json
import math
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import BinaryIO, Final, Literal, Protocol, TypeAlias, cast
from urllib.parse import urlsplit

from ..agent_reach_bridge import (
    AgentReachExecutionApi,
    validate_agent_reach_execution_contract,
)
from .exa_artifacts import ExaArtifactAttestation

WorkerOperation = Literal["search.web", "search.code"]
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
EXPECTED_SOURCE: Final = "exa"
EXPECTED_BACKEND_ID: Final = "exa-mcporter"
EXPECTED_BACKEND_VERSION: Final = "0.12.3+exa-web.v1"
EXPECTED_CODE_BACKEND_VERSION: Final = "0.12.3+exa-code.v1"
RESULT_SCHEMA: Final = "exa.search.result.v1"
CODE_RESULT_SCHEMA: Final = "exa.code.result.v1"

MAX_REQUEST_BYTES: Final = 64 * 1024
MAX_OUTPUT_BYTES: Final = 512 * 1024
MAX_QUERY_CHARACTERS: Final = 4_096
MAX_LIMIT: Final = 50
MAX_ITEMS: Final = 20
MAX_TEXT_CHARACTERS: Final = 16_000
MAX_TITLE_CHARACTERS: Final = 4_096
MAX_URL_CHARACTERS: Final = 8_192
MAX_AUTHOR_CHARACTERS: Final = 2_048
MAX_PUBLISHED_CHARACTERS: Final = 512
MAX_JSON_DEPTH: Final = 10
MAX_JSON_NODES: Final = 512
MAX_JSON_STRING_BYTES: Final = MAX_TEXT_CHARACTERS * 4
_LENGTH_BYTES: Final = 4

_REQUEST_FIELDS: Final = frozenset(
    {"artifacts", "limit", "operation", "protocol", "query"}
)
_ARTIFACT_FIELDS: Final = frozenset(
    {
        "config_path",
        "config_sha256",
        "mcporter_cli",
        "mcporter_root",
        "mcporter_tree_sha256",
        "node_executable",
        "node_sha256",
    }
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
_WEB_ITEM_FIELDS: Final = frozenset({"author", "published_at", "text", "title", "url"})
_CODE_ITEM_FIELDS: Final = frozenset({"text", "title", "url"})
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


class ExaProtocolError(ValueError):
    """The closed worker input, output, or fork contract was violated."""


@dataclass(frozen=True, slots=True)
class _OperationContract:
    backend_version: str
    result_schema: str
    item_fields: frozenset[str]


_OPERATION_CONTRACTS: Final = MappingProxyType(
    {
        "search.web": _OperationContract(
            EXPECTED_BACKEND_VERSION,
            RESULT_SCHEMA,
            _WEB_ITEM_FIELDS,
        ),
        "search.code": _OperationContract(
            EXPECTED_CODE_BACKEND_VERSION,
            CODE_RESULT_SCHEMA,
            _CODE_ITEM_FIELDS,
        ),
    }
)


@dataclass(frozen=True, slots=True, repr=False)
class WorkerRequest:
    operation: WorkerOperation
    query: str
    limit: int
    artifacts: ExaArtifactAttestation

    def __repr__(self) -> str:
        return f"WorkerRequest(operation={self.operation!r}, payload=<redacted>)"


@dataclass(frozen=True, slots=True, repr=False)
class ExaResultProjection:
    """One independently validated result returned by the fork."""

    text: str
    title: str
    url: str
    author: str | None
    published_at: str | None

    def __repr__(self) -> str:
        return "ExaResultProjection(<redacted>)"


@dataclass(frozen=True, slots=True, repr=False)
class ExaCodeResultProjection:
    """One independently validated Code result returned by the fork."""

    text: str
    title: str
    url: str

    def __repr__(self) -> str:
        return "ExaCodeResultProjection(<redacted>)"


ExaItemProjection: TypeAlias = ExaResultProjection | ExaCodeResultProjection


@dataclass(frozen=True, slots=True, repr=False)
class ExaProjection:
    """A complete validated Exa worker result for one closed operation."""

    operation: WorkerOperation
    items: tuple[ExaItemProjection, ...]
    truncated: bool

    def __repr__(self) -> str:
        return f"ExaProjection(operation={self.operation!r}, result=<redacted>)"


@dataclass(frozen=True, slots=True)
class ForkExecutionFailure:
    """A validated fork failure containing no provider or request text."""

    operation: WorkerOperation
    error_code: WorkerErrorCode


WorkerResponse: TypeAlias = ExaProjection | ForkExecutionFailure


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
    validator = cast(
        Callable[..., AgentReachExecutionApi],
        validate_agent_reach_execution_contract,
    )
    return validator(runtime_module="exa")


def encode_request(
    query: str,
    limit: int,
    artifacts: ExaArtifactAttestation,
    *,
    operation: str = "search.web",
) -> bytes:
    """Encode one fixed Exa request for the isolated worker."""

    if type(artifacts) is not ExaArtifactAttestation:
        raise ExaProtocolError("worker_request_invalid")
    request = _validated_request(
        {
            "artifacts": artifacts.frame_fields(),
            "limit": limit,
            "operation": operation,
            "protocol": PROTOCOL_VERSION,
            "query": query,
        }
    )
    return _encode_frame(_request_value(request), MAX_REQUEST_BYTES)


def decode_response(
    raw: bytes,
    *,
    limit: int,
    operation: str = "search.web",
) -> WorkerResponse:
    """Independently validate the complete bounded worker response."""

    contract_operation = _operation(operation, request=False)
    maximum_items = min(_bounded_limit(limit), MAX_ITEMS)
    value = _decode_frame(raw, MAX_OUTPUT_BYTES)
    if not isinstance(value, dict):
        raise ExaProtocolError("worker_response_invalid")
    if set(value) == _SUCCESS_FIELDS:
        return _decode_success(
            value,
            operation=contract_operation,
            maximum_items=maximum_items,
        )
    if set(value) == _FAILURE_FIELDS:
        return _decode_failure(value, operation=contract_operation)
    raise ExaProtocolError("worker_response_invalid")


def _read_request(stream: BinaryIO) -> WorkerRequest:
    header = stream.read(_LENGTH_BYTES)
    if len(header) != _LENGTH_BYTES:
        raise ExaProtocolError("worker_request_invalid")
    length = int.from_bytes(header, "big")
    if not 0 < length <= MAX_REQUEST_BYTES:
        raise ExaProtocolError("worker_request_invalid")
    payload = stream.read(length)
    if len(payload) != length or stream.read(1):
        raise ExaProtocolError("worker_request_invalid")
    return _validated_request(_load_json(payload, maximum=MAX_REQUEST_BYTES))


def _validated_request(value: object) -> WorkerRequest:
    if not isinstance(value, dict) or set(value) != _REQUEST_FIELDS:
        raise ExaProtocolError("worker_request_invalid")
    if value.get("protocol") != PROTOCOL_VERSION:
        raise ExaProtocolError("worker_request_invalid")
    operation = _operation(value.get("operation"), request=True)
    query = _bounded_text(value.get("query"), MAX_QUERY_CHARACTERS)
    limit = _bounded_limit(value.get("limit"))
    artifacts = _decode_artifacts(value.get("artifacts"))
    return WorkerRequest(operation, query, limit, artifacts)


def _decode_artifacts(value: object) -> ExaArtifactAttestation:
    if not isinstance(value, dict) or set(value) != _ARTIFACT_FIELDS:
        raise ExaProtocolError("worker_request_invalid")
    if any(
        type(key) is not str or type(item) is not str for key, item in value.items()
    ):
        raise ExaProtocolError("worker_request_invalid")
    try:
        return ExaArtifactAttestation(
            node_executable=_absolute_path(value["node_executable"]),
            node_sha256=cast(str, value["node_sha256"]),
            mcporter_root=_absolute_path(value["mcporter_root"]),
            mcporter_cli=_absolute_path(value["mcporter_cli"]),
            mcporter_tree_sha256=cast(str, value["mcporter_tree_sha256"]),
            config_path=_absolute_path(value["config_path"]),
            config_sha256=cast(str, value["config_sha256"]),
        )
    except (TypeError, ValueError):
        raise ExaProtocolError("worker_request_invalid") from None


def _absolute_path(value: object) -> Path:
    if type(value) is not str:
        raise ExaProtocolError("worker_request_invalid")
    return Path(value)


def _request_value(request: WorkerRequest) -> dict[str, object]:
    return {
        "artifacts": request.artifacts.frame_fields(),
        "limit": request.limit,
        "operation": request.operation,
        "protocol": PROTOCOL_VERSION,
        "query": request.query,
    }


def _execute_request(
    request: WorkerRequest,
    *,
    execution_api_provider: ExecutionApiProvider | None = None,
) -> Mapping[str, object]:
    provider = execution_api_provider or _load_execution_api
    try:
        api = provider()
    except Exception:
        return _failure_value("backend_contract_violation", request.operation)

    try:
        request_factory = cast(Callable[..., object], api.execution_request_type)
        network_factory = cast(Callable[..., object], api.network_access_type)
        limits_factory = cast(Callable[..., object], api.execution_limits_type)
        context_factory = cast(Callable[..., object], api.execution_context_type)
        artifacts_type = getattr(api, "mcporter_artifacts_type", None)
        if not isinstance(artifacts_type, type):
            return _failure_value("backend_contract_violation", request.operation)
        artifacts_factory = cast(Callable[..., object], artifacts_type)

        execution_request = request_factory(
            PROTOCOL_VERSION,
            EXPECTED_SOURCE,
            request.operation,
            {"query": request.query, "limit": request.limit},
        )
        network_access = network_factory()
        artifact_capability = artifacts_factory(**request.artifacts.frame_fields())
        maximum_items = min(request.limit, MAX_ITEMS)
        limits = limits_factory(
            maximum_items=maximum_items,
            maximum_text_characters=MAX_TEXT_CHARACTERS,
        )
        context = context_factory(
            (network_access, artifact_capability),
            limits=limits,
        )
        result = api.execute(execution_request, context)

        if type(result) is api.execution_success_type:
            return _success_value(
                cast(_ExecutionSuccess, result),
                api=api,
                operation=request.operation,
                maximum_items=maximum_items,
            )
        if type(result) is api.execution_failure_type:
            failure = cast(_ExecutionFailure, result)
            code = failure.error_code
            if (
                _valid_execution_identity(failure, request.operation)
                and type(code) is str
                and code in _ERROR_CODES
            ):
                return _failure_value(
                    cast(WorkerErrorCode, code),
                    request.operation,
                )
    except Exception:
        return _failure_value("backend_contract_violation", request.operation)
    return _failure_value("backend_contract_violation", request.operation)


def _success_value(
    success: _ExecutionSuccess,
    *,
    api: AgentReachExecutionApi,
    operation: WorkerOperation,
    maximum_items: int,
) -> Mapping[str, object]:
    contract = _OPERATION_CONTRACTS[operation]
    items = success.items
    if (
        not _valid_execution_identity(success, operation)
        or success.partial_error_code is not None
        or type(success.truncated) is not bool
        or type(items) is not tuple
        or len(items) > maximum_items
    ):
        return _failure_value("backend_contract_violation", operation)
    projected: list[dict[str, object]] = []
    for raw_item in items:
        if type(raw_item) is not api.execution_item_type:
            return _failure_value("backend_contract_violation", operation)
        item = cast(_ExecutionItem, raw_item)
        if item.schema_id != contract.result_schema:
            return _failure_value("backend_contract_violation", operation)
        try:
            projected.append(
                _item_value(_decode_item(item.fields, operation=operation))
            )
        except ExaProtocolError:
            return _failure_value("backend_contract_violation", operation)
    return {
        "backend": _backend_value(operation),
        "items": projected,
        "operation": operation,
        "partial": None,
        "protocol": PROTOCOL_VERSION,
        "schema": contract.result_schema,
        "source": EXPECTED_SOURCE,
        "truncated": success.truncated,
    }


def _valid_execution_identity(
    value: _ExecutionSuccess | _ExecutionFailure,
    operation: WorkerOperation,
) -> bool:
    return bool(
        value.protocol_version == PROTOCOL_VERSION
        and value.source == EXPECTED_SOURCE
        and value.operation == operation
        and value.backend_id == EXPECTED_BACKEND_ID
        and value.backend_version == _OPERATION_CONTRACTS[operation].backend_version
    )


def _failure_value(
    error_code: WorkerErrorCode,
    operation: WorkerOperation = "search.web",
) -> dict[str, object]:
    return {
        "backend": _backend_value(operation),
        "error": {"code": error_code},
        "operation": operation,
        "protocol": PROTOCOL_VERSION,
        "source": EXPECTED_SOURCE,
    }


def _backend_value(operation: WorkerOperation) -> dict[str, str]:
    return {
        "id": EXPECTED_BACKEND_ID,
        "version": _OPERATION_CONTRACTS[operation].backend_version,
    }


def _decode_success(
    value: Mapping[str, object],
    *,
    operation: WorkerOperation,
    maximum_items: int,
) -> ExaProjection:
    contract = _OPERATION_CONTRACTS[operation]
    _validate_response_identity(value, operation)
    _decode_backend(value["backend"], operation)
    items = value["items"]
    truncated = value["truncated"]
    if (
        value["schema"] != contract.result_schema
        or value["partial"] is not None
        or type(truncated) is not bool
        or not isinstance(items, list)
        or len(items) > maximum_items
    ):
        raise ExaProtocolError("worker_response_invalid")
    return ExaProjection(
        operation,
        tuple(_decode_item(item, operation=operation) for item in items),
        truncated,
    )


def _decode_failure(
    value: Mapping[str, object],
    *,
    operation: WorkerOperation,
) -> ForkExecutionFailure:
    _validate_response_identity(value, operation)
    _decode_backend(value["backend"], operation)
    error = value["error"]
    if not isinstance(error, dict) or set(error) != _ERROR_FIELDS:
        raise ExaProtocolError("worker_response_invalid")
    code = error["code"]
    if type(code) is not str or code not in _ERROR_CODES:
        raise ExaProtocolError("worker_response_invalid")
    return ForkExecutionFailure(operation, cast(WorkerErrorCode, code))


def _validate_response_identity(
    value: Mapping[str, object],
    operation: WorkerOperation,
) -> None:
    if (
        value["protocol"] != PROTOCOL_VERSION
        or value["source"] != EXPECTED_SOURCE
        or value["operation"] != operation
    ):
        raise ExaProtocolError("worker_response_invalid")


def _decode_backend(value: object, operation: WorkerOperation) -> None:
    if (
        not isinstance(value, dict)
        or set(value) != _BACKEND_FIELDS
        or value["id"] != EXPECTED_BACKEND_ID
        or value["version"] != _OPERATION_CONTRACTS[operation].backend_version
    ):
        raise ExaProtocolError("worker_response_invalid")


def _decode_item(
    value: object,
    *,
    operation: WorkerOperation,
) -> ExaItemProjection:
    contract = _OPERATION_CONTRACTS[operation]
    if (
        not isinstance(value, Mapping)
        or set(value) != contract.item_fields
        or any(type(key) is not str for key in value)
    ):
        raise ExaProtocolError("worker_response_invalid")
    text = _normalized_text(value["text"], MAX_TEXT_CHARACTERS)
    title = _label_text(value["title"], MAX_TITLE_CHARACTERS)
    url = _label_text(value["url"], MAX_URL_CHARACTERS)
    if not _valid_public_url(url):
        raise ExaProtocolError("worker_response_invalid")
    if operation == "search.code":
        return ExaCodeResultProjection(text, title, url)
    return ExaResultProjection(
        text,
        title,
        url,
        _optional_label_text(value["author"], MAX_AUTHOR_CHARACTERS),
        _optional_label_text(value["published_at"], MAX_PUBLISHED_CHARACTERS),
    )


def _item_value(item: ExaItemProjection) -> dict[str, object]:
    if type(item) is ExaCodeResultProjection:
        return {"text": item.text, "title": item.title, "url": item.url}
    if type(item) is ExaResultProjection:
        return {
            "author": item.author,
            "published_at": item.published_at,
            "text": item.text,
            "title": item.title,
            "url": item.url,
        }
    raise ExaProtocolError("worker_response_invalid")


def _required_text(value: object, maximum: int) -> str:
    if (
        type(value) is not str
        or not 0 < len(value) <= maximum
        or value != value.strip()
        or _contains_invalid_scalar(value)
    ):
        raise ExaProtocolError("worker_response_invalid")
    return value


def _normalized_text(value: object, maximum: int) -> str:
    text = _required_text(value, maximum)
    if text != " ".join(text.split()):
        raise ExaProtocolError("worker_response_invalid")
    return text


def _label_text(value: object, maximum: int) -> str:
    text = _required_text(value, maximum)
    if _contains_control(text):
        raise ExaProtocolError("worker_response_invalid")
    return text


def _optional_label_text(value: object, maximum: int) -> str | None:
    if value is None:
        return None
    return _label_text(value, maximum)


def _bounded_text(value: object, maximum: int) -> str:
    if (
        type(value) is not str
        or not 0 < len(value) <= maximum
        or value != value.strip()
        or _contains_invalid_scalar(value)
    ):
        raise ExaProtocolError("worker_request_invalid")
    return value


def _bounded_limit(value: object) -> int:
    if type(value) is not int or not 1 <= value <= MAX_LIMIT:
        raise ExaProtocolError("worker_request_invalid")
    return value


def _operation(value: object, *, request: bool) -> WorkerOperation:
    if type(value) is not str or value not in _OPERATION_CONTRACTS:
        raise ExaProtocolError(
            "worker_request_invalid" if request else "worker_response_invalid"
        )
    return cast(WorkerOperation, value)


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
        raise ExaProtocolError("worker_frame_invalid") from None
    if not 0 < len(payload) <= maximum:
        raise ExaProtocolError("worker_frame_invalid")
    return len(payload).to_bytes(_LENGTH_BYTES, "big") + payload


def _decode_frame(raw: bytes, maximum: int) -> object:
    if type(raw) is not bytes or len(raw) < _LENGTH_BYTES + 1:
        raise ExaProtocolError("worker_frame_invalid")
    length = int.from_bytes(raw[:_LENGTH_BYTES], "big")
    if not 0 < length <= maximum or len(raw) != _LENGTH_BYTES + length:
        raise ExaProtocolError("worker_frame_invalid")
    return _load_json(raw[_LENGTH_BYTES:], maximum=maximum)


def _load_json(raw: bytes, *, maximum: int) -> object:
    if not 0 < len(raw) <= maximum:
        raise ExaProtocolError("worker_json_invalid")
    try:
        value = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (ExaProtocolError, UnicodeError, ValueError, RecursionError):
        raise ExaProtocolError("worker_json_invalid") from None
    _validate_json_shape(value)
    return value


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ExaProtocolError("worker_json_invalid")
        value[key] = item
    return value


def _reject_constant(_: str) -> object:
    raise ExaProtocolError("worker_json_invalid")


def _validate_json_shape(value: object) -> None:
    pending: list[tuple[object, int]] = [(value, 1)]
    nodes = 0
    while pending:
        current, depth = pending.pop()
        nodes += 1
        if nodes > MAX_JSON_NODES or depth > MAX_JSON_DEPTH:
            raise ExaProtocolError("worker_json_invalid")
        if current is None or type(current) in {bool, int}:
            continue
        if type(current) is float:
            if not math.isfinite(current):
                raise ExaProtocolError("worker_json_invalid")
            continue
        if type(current) is str:
            if len(
                current.encode("utf-8", errors="strict")
            ) > MAX_JSON_STRING_BYTES or _contains_invalid_scalar(current):
                raise ExaProtocolError("worker_json_invalid")
            continue
        if isinstance(current, list):
            pending.extend((item, depth + 1) for item in current)
            continue
        if isinstance(current, dict):
            pending.extend((item, depth + 1) for item in current.values())
            continue
        raise ExaProtocolError("worker_json_invalid")


def _contains_invalid_scalar(value: str) -> bool:
    return any(
        character == "\x00" or 0xD800 <= ord(character) <= 0xDFFF for character in value
    )


def _contains_control(value: str) -> bool:
    return any(ord(character) < 32 or ord(character) == 127 for character in value)


def _main() -> int:
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
    except ExaProtocolError:
        try:
            output = _encode_frame(
                _failure_value("backend_contract_violation", request.operation),
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
    "CODE_RESULT_SCHEMA",
    "EXPECTED_CODE_BACKEND_VERSION",
    "ExaCodeResultProjection",
    "ExaProjection",
    "ExaProtocolError",
    "ExaResultProjection",
    "ForkExecutionFailure",
    "MAX_LIMIT",
    "MAX_OUTPUT_BYTES",
    "WorkerErrorCode",
    "WorkerOperation",
    "WorkerRequest",
    "decode_response",
    "encode_request",
]
