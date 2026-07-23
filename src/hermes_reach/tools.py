"""Hermes tool handlers that validate requests and never execute a backend."""

from __future__ import annotations

from collections.abc import Callable

from .contracts import (
    OperationCall,
    ReachValidationError,
    error_response,
    internal_error_response,
    json_result,
    new_trace_id,
    planned_response,
    success_response,
    validate_browse,
    validate_read,
    validate_search,
    validate_status,
    validate_transcribe,
)
from .status import status_data

Validator = Callable[[object], OperationCall | tuple[OperationCall, ...]]


def reach_search(args: dict[str, object], **kwargs: object) -> str:
    """Validate bounded explicit-source search and return planned groups."""

    del kwargs
    return _planned_handler(args, validate_search)


def reach_read(args: dict[str, object], **kwargs: object) -> str:
    """Validate a content-read request and return a planned group."""

    del kwargs
    return _planned_handler(args, validate_read)


def reach_browse(args: dict[str, object], **kwargs: object) -> str:
    """Validate source-native browsing and return a planned group."""

    del kwargs
    return _planned_handler(args, validate_browse)


def reach_transcribe(args: dict[str, object], **kwargs: object) -> str:
    """Validate transcription input without touching the media target."""

    del kwargs
    return _planned_handler(args, validate_transcribe)


def reach_status(args: dict[str, object], **kwargs: object) -> str:
    """Return local catalog status without a health probe."""

    del kwargs
    trace_id = new_trace_id()
    try:
        request = validate_status(args)
        return json_result(
            success_response(
                trace_id, status_data(request.sources, request.include_planned)
            )
        )
    except ReachValidationError as error:
        return json_result(error_response(error, trace_id))
    except Exception:
        return json_result(internal_error_response(trace_id))


def _planned_handler(args: object, validator: Validator) -> str:
    trace_id = new_trace_id()
    try:
        validated = validator(args)
        if isinstance(validated, tuple):
            calls = validated
        else:
            calls = (validated,)
        return json_result(planned_response(calls, trace_id))
    except ReachValidationError as error:
        return json_result(error_response(error, trace_id))
    except Exception:
        return json_result(internal_error_response(trace_id))
