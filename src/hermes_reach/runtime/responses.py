"""Map bounded runtime results into stable redacted v1 envelopes."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final, Literal

from ..catalog import CATALOG_VERSION, PROTOCOL_VERSION
from ..contracts import OperationCall
from .adapters import FailureClass, RawItem
from .availability import AvailabilityRecord
from .runner import AttemptProvenance, RunnerResult

GroupOutcome = Literal["ok", "partial", "error"]

_FAILURES: Final[dict[FailureClass, tuple[str, str, str]]] = {
    "transient": (
        "source_temporarily_unavailable",
        "The source could not complete the request.",
        "Retry later or inspect local source status.",
    ),
    "invalid_input": (
        "source_rejected_request",
        "The source rejected the normalized request.",
        "Review the operation input and retry.",
    ),
    "not_found": (
        "resource_not_found",
        "The requested source resource was not found.",
        "Verify the source-native identifier or URL.",
    ),
    "authentication": (
        "setup_required",
        "The source requires an unavailable configured capability.",
        "Complete operator setup for this exact source operation.",
    ),
    "authorization": (
        "source_access_denied",
        "The source denied this read request.",
        "Inspect the configured source scope and operator policy.",
    ),
    "policy": (
        "policy_denied",
        "Reach policy denied this source request.",
        "Use a catalog-owned public target and operation.",
    ),
    "rate_limit": (
        "source_rate_limited",
        "The source rate-limited this request.",
        "Retry after the source limit resets.",
    ),
    "permanent": (
        "source_response_invalid",
        "The source returned an unsupported response.",
        "Inspect source compatibility through operator diagnostics.",
    ),
}


def unavailable_group(
    call: OperationCall, availability: AvailabilityRecord
) -> tuple[dict[str, object], GroupOutcome]:
    """Build a non-executable group without request-bearing details."""

    code = (
        "setup_required"
        if availability.state == "setup_required"
        else "capability_unavailable"
    )
    message = (
        "This source operation requires operator setup."
        if availability.state == "setup_required"
        else "This source operation is not executable in this release."
    )
    return (
        _group_base(
            call,
            availability.state,
            [],
            False,
            [],
            {"code": code, "message": message, "remediation": availability.reason},
        ),
        "error",
    )


def runner_group(
    call: OperationCall, result: RunnerResult
) -> tuple[dict[str, object], GroupOutcome]:
    """Map one bounded runner result without exposing adapter details."""

    attempts = [_attempt_data(attempt) for attempt in result.attempts]
    provenance: dict[str, object] = {
        "catalog_version": CATALOG_VERSION,
        "owner": "adapter",
        "implementation_state": call.operation.implementation_state,
    }
    if result.selected_backend_id is not None:
        provenance["backend_id"] = result.selected_backend_id
    if result.selected_backend_version is not None:
        provenance["backend_version"] = result.selected_backend_version

    if result.failure_class is not None:
        availability = _failure_availability(result.failure_class)
        group = _group_base(
            call,
            availability,
            [],
            result.truncated,
            attempts,
            _error_data(result.failure_class),
            provenance,
        )
        return group, "error"

    items = [_item_data(item) for item in result.items]
    if result.partial_failure_class is not None:
        error = _error_data(result.partial_failure_class)
        error["code"] = "partial_source_result"
        error["message"] = "The source returned usable but incomplete data."
        group = _group_base(
            call,
            "degraded",
            items,
            result.truncated,
            attempts,
            error,
            provenance,
        )
        return group, "partial"
    return (
        _group_base(
            call,
            "available",
            items,
            result.truncated,
            attempts,
            None,
            provenance,
        ),
        "ok",
    )


def execution_response(
    groups: Sequence[tuple[dict[str, object], GroupOutcome]], trace_id: str
) -> dict[str, object]:
    """Aggregate ordered groups into one v1 response."""

    outcomes = [outcome for _, outcome in groups]
    if outcomes and all(outcome == "ok" for outcome in outcomes):
        outcome = "ok"
    elif any(outcome in {"ok", "partial"} for outcome in outcomes):
        outcome = "partial"
    else:
        outcome = "error"
    response: dict[str, object] = {
        "protocol_version": PROTOCOL_VERSION,
        "trace_id": trace_id,
        "outcome": outcome,
        "groups": [group for group, _ in groups],
    }
    if outcome == "error":
        response["error"] = {
            "code": "all_sources_failed",
            "message": "No requested source operation completed successfully.",
            "remediation": "Inspect each source group for a safe remediation.",
            "details": {},
        }
    return response


def internal_failure_group(
    call: OperationCall,
) -> tuple[dict[str, object], GroupOutcome]:
    """Contain unexpected execution exceptions before Hermes can log them."""

    result = RunnerResult((), False, (), failure_class="permanent")
    return runner_group(call, result)


def _group_base(
    call: OperationCall,
    availability: str,
    items: list[dict[str, object]],
    truncated: bool,
    attempts: list[dict[str, object]],
    error: dict[str, object] | None,
    provenance: dict[str, object] | None = None,
) -> dict[str, object]:
    group: dict[str, object] = {
        "source": call.source.name,
        "operation": call.operation.name,
        "availability": availability,
        "provenance": provenance
        or {
            "catalog_version": CATALOG_VERSION,
            "owner": "foundation",
            "implementation_state": call.operation.implementation_state,
        },
        "items": items,
        "native_order": "backend",
        "truncated": truncated,
        "continuation": None,
        "attempts": attempts,
    }
    if error is not None:
        group["error"] = error
    return group


def _item_data(item: RawItem) -> dict[str, object]:
    data: dict[str, object] = {"kind": item.kind, "text": item.text}
    for name in ("native_id", "title", "url", "author", "published_at"):
        value = getattr(item, name)
        if value is not None:
            data[name] = value
    return data


def _attempt_data(attempt: AttemptProvenance) -> dict[str, object]:
    return {
        "backend_id": attempt.backend_id,
        "backend_version": attempt.backend_version,
        "duration_ms": attempt.duration_ms,
        "outcome": attempt.outcome,
    }


def _failure_availability(failure: FailureClass) -> str:
    if failure in {"not_found", "invalid_input"}:
        return "available"
    if failure == "authentication":
        return "setup_required"
    if failure in {"authorization", "policy"}:
        return "unavailable"
    return "degraded"


def _error_data(failure: FailureClass) -> dict[str, object]:
    code, message, remediation = _FAILURES[failure]
    return {"code": code, "message": message, "remediation": remediation}
