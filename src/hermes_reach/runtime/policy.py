"""Deny-first authorization for catalog-owned read-only operations."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final, Literal

from ..catalog import DataScope, OperationSpec, get_operation, get_source
from ..contracts import (
    OperationCall,
    ReachValidationError,
    operation_call_is_valid,
    operation_options_are_valid,
)

EffectiveScope = Literal["public", "account_visible"]
_SCOPE_RANK: Final[dict[EffectiveScope, int]] = {
    "public": 0,
    "account_visible": 1,
}
_READ_ONLY_TOOLS: Final[frozenset[str]] = frozenset(
    {"search", "read", "browse", "transcribe"}
)
_TRACE_ID: Final = re.compile(r"[0-9a-f]{32}")


class RuntimePolicyError(ReachValidationError):
    """A stable policy denial that never embeds request values."""


@dataclass(frozen=True, slots=True)
class AuthorizedCall:
    """A canonical operation that has passed runtime policy checks."""

    call: OperationCall
    effective_scope: EffectiveScope
    policy_revision: str
    trace_id: str | None = None

    @property
    def operation(self) -> OperationSpec:
        """Return the policy-authorized catalog operation."""

        return self.call.operation


class ReadOnlyPolicy:
    """Authorize only current immutable catalog rows at a bounded scope."""

    def __init__(self, revision: str = "v1") -> None:
        self._revision = revision

    def authorize(
        self,
        call: OperationCall,
        effective_scope: EffectiveScope = "public",
        *,
        trace_id: str | None = None,
    ) -> AuthorizedCall:
        """Reject forged, mutating, or scope-expanding operation calls."""

        source = get_source(call.source.name)
        if source is None or source != call.source:
            raise self._denied("The source is not authorized by the catalog.")
        operation = get_operation(source, call.operation.name)
        if operation is None or operation != call.operation:
            raise self._denied("The operation is not authorized by the catalog.")
        if operation.tool not in _READ_ONLY_TOOLS:
            raise self._denied("The operation is outside the read-only runtime.")
        if not operation_options_are_valid(operation, call.options):
            raise self._denied(
                "The operation options are not authorized by the catalog."
            )
        if not operation_call_is_valid(call):
            raise self._denied("The operation input is not authorized by the catalog.")
        if not scope_includes(effective_scope, operation.runtime.data_scope):
            raise self._denied("The operation requires an operator-granted scope.")
        if trace_id is not None and (
            type(trace_id) is not str or _TRACE_ID.fullmatch(trace_id) is None
        ):
            raise self._denied("The execution trace context is invalid.")
        return AuthorizedCall(call, effective_scope, self._revision, trace_id)

    def _denied(self, remediation: str) -> RuntimePolicyError:
        return RuntimePolicyError(
            "policy_denied",
            "The requested operation is denied by Reach policy.",
            remediation,
        )


def scope_includes(available: EffectiveScope, required: DataScope) -> bool:
    """Return whether a granted scope is at least as broad as a requirement."""

    return _SCOPE_RANK[available] >= _SCOPE_RANK[required]
