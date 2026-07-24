"""Closed request validation and redacted v1 response construction."""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final

from .catalog import (
    CATALOG_VERSION,
    PROTOCOL_VERSION,
    OperationSpec,
    OptionSpec,
    SourceSpec,
    ToolFamily,
    get_operation,
    get_source,
)

MAX_QUERY_LENGTH: Final = 4096
_IDENTIFIER: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")


class ReachValidationError(Exception):
    """A safe domain error that is serialised without user input or exceptions."""

    def __init__(
        self,
        code: str,
        message: str,
        remediation: str,
        details: Mapping[str, object] | None = None,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.message = message
        self.remediation = remediation
        self.details = dict(details or {})


@dataclass(frozen=True, slots=True)
class OperationCall:
    """A validated source-operation request held only for the current call."""

    source: SourceSpec
    operation: OperationSpec
    options: Mapping[str, object]
    target: Mapping[str, str] | None = None
    query: str | None = None


@dataclass(frozen=True, slots=True)
class StatusRequest:
    """A validated safe status filter."""

    sources: tuple[SourceSpec, ...]
    include_planned: bool


def new_trace_id() -> str:
    """Create an opaque request correlation ID without embedding input data."""

    return uuid.uuid4().hex


def json_result(payload: Mapping[str, object]) -> str:
    """Serialize every tool result consistently for Hermes handlers."""

    return json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def error_response(error: ReachValidationError, trace_id: str) -> dict[str, object]:
    """Build a public, redacted error envelope."""

    return {
        "protocol_version": PROTOCOL_VERSION,
        "trace_id": trace_id,
        "outcome": "error",
        "groups": [],
        "error": {
            "code": error.code,
            "message": error.message,
            "remediation": error.remediation,
            "details": dict(error.details),
        },
    }


def internal_error_response(trace_id: str) -> dict[str, object]:
    """Hide unexpected exceptions while retaining a useful correlation ID."""

    return error_response(
        ReachValidationError(
            "internal_error",
            "The request could not be processed.",
            "Retry later or inspect the trace ID through operator diagnostics.",
        ),
        trace_id,
    )


def planned_group(call: OperationCall) -> dict[str, object]:
    """Represent a known but intentionally unimplemented operation."""

    return {
        "source": call.source.name,
        "operation": call.operation.name,
        "availability": "unavailable",
        "provenance": {
            "catalog_version": CATALOG_VERSION,
            "owner": "foundation",
            "implementation_state": call.operation.implementation_state,
        },
        "items": [],
        "native_order": "backend",
        "truncated": False,
        "continuation": None,
        "attempts": [],
        "error": {
            "code": "capability_unavailable",
            "message": "This source operation is planned but not implemented.",
            "remediation": call.operation.unavailable_reason,
        },
    }


def planned_response(
    calls: tuple[OperationCall, ...], trace_id: str
) -> dict[str, object]:
    """Return ordered source groups without contacting a backend."""

    return {
        "protocol_version": PROTOCOL_VERSION,
        "trace_id": trace_id,
        "outcome": "error",
        "groups": [planned_group(call) for call in calls],
        "error": {
            "code": "capability_unavailable",
            "message": "No requested source operation has an installed adapter.",
            "remediation": "Inspect reach_status for the release state and setup path.",
            "details": {},
        },
    }


def success_response(trace_id: str, data: Mapping[str, object]) -> dict[str, object]:
    """Build a local discovery response with no result content."""

    return {
        "protocol_version": PROTOCOL_VERSION,
        "trace_id": trace_id,
        "outcome": "ok",
        "groups": [],
        "data": dict(data),
    }


def validate_search(args: object) -> tuple[OperationCall, ...]:
    """Validate bounded multi-source search without interpreting omission as all."""

    request = _request_object(args, {"protocol_version", "requests"})
    _validate_protocol_version(request)
    requests = request.get("requests")
    if not isinstance(requests, list) or not 1 <= len(requests) <= 5:
        raise _invalid("requests", "Provide one to five explicit source requests.")

    calls: list[OperationCall] = []
    sources: set[str] = set()
    for value in requests:
        item = _object(value, "requests")
        _reject_unknown(item, {"source", "operation", "query", "options"})
        source = _source(item.get("source"))
        if source.name in sources:
            raise _invalid("requests", "Each search source may appear only once.")
        sources.add(source.name)
        operation = _operation(source, item.get("operation"), "search")
        query = _text(item.get("query"), "query", MAX_QUERY_LENGTH)
        options = _options(operation, item.get("options", {}))
        calls.append(OperationCall(source, operation, options, query=query))
    return tuple(calls)


def validate_read(args: object) -> OperationCall:
    """Validate a single content retrieval request."""

    return _single_call(args, "read")


def validate_browse(args: object) -> OperationCall:
    """Validate a single source-native collection request."""

    return _single_call(args, "browse")


def validate_transcribe(args: object) -> OperationCall:
    """Validate a single media transcription request without opening media."""

    return _single_call(args, "transcribe")


def validate_status(args: object) -> StatusRequest:
    """Validate local-only source status filters."""

    request = _request_object(args, {"protocol_version", "sources", "include_planned"})
    _validate_protocol_version(request)
    include_planned = request.get("include_planned", True)
    if not isinstance(include_planned, bool):
        raise _invalid("include_planned", "Use a boolean value.")

    raw_sources = request.get("sources")
    if raw_sources is None:
        from .catalog import SOURCE_CATALOG

        return StatusRequest(SOURCE_CATALOG, include_planned)
    if not isinstance(raw_sources, list) or not raw_sources:
        raise _invalid(
            "sources", "Provide a non-empty source list when filtering status."
        )

    sources: list[SourceSpec] = []
    seen: set[str] = set()
    for raw_source in raw_sources:
        source = _source(raw_source)
        if source.name in seen:
            raise _invalid("sources", "Status sources must be distinct.")
        seen.add(source.name)
        sources.append(source)
    return StatusRequest(tuple(sources), include_planned)


def _single_call(args: object, tool: ToolFamily) -> OperationCall:
    allowed = {"protocol_version", "source", "operation", "options", "target"}
    request = _request_object(args, allowed)
    _validate_protocol_version(request)
    source = _source(request.get("source"))
    operation = _operation(source, request.get("operation"), tool)
    options = _options(operation, request.get("options", {}))
    target = _target(request.get("target"), operation)
    return OperationCall(source, operation, options, target=target)


def _request_object(args: object, allowed: set[str]) -> Mapping[str, object]:
    request = _object(args, "request")
    _reject_unknown(request, allowed)
    return request


def _object(value: object, field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise _invalid(field, "Use a JSON object.")
    return value


def _reject_unknown(value: Mapping[str, object], allowed: set[str]) -> None:
    if set(value).difference(allowed):
        raise ReachValidationError(
            "invalid_argument",
            "The request contains an unsupported field.",
            "Consult the tool schema and remove unsupported fields.",
        )


def _validate_protocol_version(request: Mapping[str, object]) -> None:
    version = request.get("protocol_version", PROTOCOL_VERSION)
    if version != PROTOCOL_VERSION:
        raise ReachValidationError(
            "unsupported_protocol_version",
            "The request protocol version is not supported.",
            "Use protocol_version v1.",
        )


def _source(value: object) -> SourceSpec:
    source_name = _text(value, "source", 64)
    source = get_source(source_name)
    if source is None:
        raise ReachValidationError(
            "unsupported_source",
            "The requested source is not in the capability catalog.",
            "Inspect reach_status or reach sources for supported source IDs.",
        )
    return source


def _operation(source: SourceSpec, value: object, tool: ToolFamily) -> OperationSpec:
    operation_name = _text(value, "operation", 128)
    operation = get_operation(source, operation_name)
    if operation is None or operation.tool != tool:
        raise ReachValidationError(
            "unsupported_operation",
            "The requested operation is not supported by this tool and source.",
            "Inspect reach_status for source-operation availability.",
        )
    return operation


def _options(operation: OperationSpec, value: object) -> Mapping[str, object]:
    options = _object(value, "options")
    option_specs = {option.name: option for option in operation.options}
    _reject_unknown(options, set(option_specs))
    missing = {
        option.name
        for option in operation.options
        if option.required and option.name not in options
    }
    if missing:
        raise _invalid("options", "Provide every required operation option.")
    for name, option_value in options.items():
        _validate_option(option_specs[name], option_value)
    return MappingProxyType(dict(options))


def operation_options_are_valid(operation: OperationSpec, value: object) -> bool:
    """Check whether options still match their canonical catalog operation."""

    try:
        _options(operation, value)
    except ReachValidationError:
        return False
    return True


def operation_call_is_valid(call: OperationCall) -> bool:
    """Revalidate a complete call before any adapter can observe it."""

    try:
        if dict(_options(call.operation, call.options)) != dict(call.options):
            return False
        if call.operation.tool == "search":
            return call.target is None and call.query == _text(
                call.query, "query", MAX_QUERY_LENGTH
            )
        if call.query is not None:
            return False
        expected = _target(call.target, call.operation)
        if expected is None:
            return call.target is None
        return call.target is not None and dict(expected) == dict(call.target)
    except ReachValidationError:
        return False


def _validate_option(spec: OptionSpec, value: object) -> None:
    if spec.kind == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            raise _invalid("options", "An option has an invalid value type.")
        if spec.minimum is not None and value < spec.minimum:
            raise _invalid("options", "An option is below its allowed range.")
        if spec.maximum is not None and value > spec.maximum:
            raise _invalid("options", "An option exceeds its allowed range.")
    elif spec.kind == "boolean":
        if not isinstance(value, bool):
            raise _invalid("options", "An option has an invalid value type.")
    elif spec.kind == "string":
        normalized = _text(value, "options", spec.maximum or 256)
        _validate_string_format(normalized, spec.string_format, "options")
    else:
        raise ReachValidationError(
            "internal_error",
            "The catalog option definition is invalid.",
            "Contact the operator with the trace ID.",
        )


def _target(value: object, operation: OperationSpec) -> Mapping[str, str] | None:
    if not operation.targets:
        if value is not None:
            raise _invalid("target", "This operation does not accept a target.")
        return None
    target = _object(value, "target")
    target_specs = {spec.kind: spec for spec in operation.targets}
    _reject_unknown(target, set(target_specs))
    if len(target) != 1:
        raise _invalid("target", "Provide exactly one supported target type.")
    name, raw_value = next(iter(target.items()))
    spec = next((item for item in operation.targets if item.kind == name), None)
    if spec is None:
        raise _invalid("target", "Use a target kind owned by this operation.")
    target_value = _text(raw_value, "target", spec.maximum)
    if name == "url" and not target_value.startswith(("https://", "http://")):
        raise ReachValidationError(
            "invalid_target",
            "The URL target must use http or https.",
            "Provide a public http(s) URL or another supported target type.",
        )
    _validate_string_format(target_value, spec.string_format, "target")
    return MappingProxyType({name: target_value})


def _validate_string_format(value: str, string_format: str, field: str) -> None:
    if string_format == "text":
        return
    if string_format == "identifier" and _IDENTIFIER.fullmatch(value):
        return
    if string_format == "positive_integer" and value.isascii() and value.isdigit():
        if int(value) > 0:
            return
    raise _invalid(field, "Use the documented closed value format.")


def _text(value: object, field: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise _invalid(field, "Provide a non-empty value within the documented limit.")
    return value.strip()


def _invalid(field: str, remediation: str) -> ReachValidationError:
    return ReachValidationError(
        "invalid_argument",
        "The request does not match the tool contract.",
        remediation,
        {"field": field},
    )
