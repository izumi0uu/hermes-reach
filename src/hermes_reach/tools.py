"""Hermes tool handlers for validated, bounded read-only execution."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from .bootstrap import DEFAULT_RUNTIME
from .contracts import (
    OperationCall,
    ReachValidationError,
    error_response,
    internal_error_response,
    json_result,
    new_trace_id,
    success_response,
    validate_browse,
    validate_read,
    validate_search,
    validate_status,
    validate_transcribe,
)
from .runtime.dispatcher import RuntimeDispatcher
from .runtime.responses import (
    GroupOutcome,
    execution_response,
    internal_failure_group,
    runner_group,
    unavailable_group,
)
from .status import status_data

Validator = Callable[[object], OperationCall | tuple[OperationCall, ...]]
_RUNTIME: RuntimeDispatcher = DEFAULT_RUNTIME


def _set_runtime(runtime: RuntimeDispatcher) -> None:
    """Install the process runtime selected once during plugin registration."""

    if not isinstance(runtime, RuntimeDispatcher):
        raise TypeError("The Reach tool runtime is invalid.")
    global _RUNTIME
    _RUNTIME = runtime


async def reach_search(args: dict[str, object], **kwargs: object) -> str:
    """Validate and execute bounded explicit-source search."""

    del kwargs
    return await _execution_handler(args, validate_search)


async def reach_read(args: dict[str, object], **kwargs: object) -> str:
    """Validate and execute a content-read request."""

    del kwargs
    return await _execution_handler(args, validate_read)


async def reach_browse(args: dict[str, object], **kwargs: object) -> str:
    """Validate and execute source-native browsing."""

    del kwargs
    return await _execution_handler(args, validate_browse)


async def reach_transcribe(args: dict[str, object], **kwargs: object) -> str:
    """Validate and execute a registered transcription adapter."""

    del kwargs
    return await _execution_handler(args, validate_transcribe)


def reach_status(args: dict[str, object], **kwargs: object) -> str:
    """Return local catalog status without a health probe."""

    del kwargs
    trace_id = new_trace_id()
    try:
        request = validate_status(args)
        return json_result(
            success_response(
                trace_id,
                status_data(
                    request.sources,
                    request.include_planned,
                    _RUNTIME.operation_availability,
                ),
            )
        )
    except ReachValidationError as error:
        return json_result(error_response(error, trace_id))
    except Exception:
        return json_result(internal_error_response(trace_id))


async def _execution_handler(args: object, validator: Validator) -> str:
    trace_id = new_trace_id()
    try:
        validated = validator(args)
        if isinstance(validated, tuple):
            calls = validated
        else:
            calls = (validated,)
        groups = await asyncio.gather(
            *(_dispatch_group(call, trace_id=trace_id) for call in calls)
        )
        return json_result(execution_response(groups, trace_id))
    except ReachValidationError as error:
        return json_result(error_response(error, trace_id))
    except Exception:
        return json_result(internal_error_response(trace_id))


async def _dispatch_group(
    call: OperationCall,
    *,
    trace_id: str,
) -> tuple[dict[str, object], GroupOutcome]:
    availability = _RUNTIME.operation_availability(
        call.source.name, call.operation.name
    )
    if availability.state not in {"available", "degraded"}:
        return unavailable_group(call, availability)
    try:
        result = await _RUNTIME.dispatch(call, trace_id=trace_id)
        if result is None:
            refreshed = _RUNTIME.operation_availability(
                call.source.name, call.operation.name
            )
            return unavailable_group(call, refreshed)
        return runner_group(call, result)
    except Exception:
        return internal_failure_group(call)
