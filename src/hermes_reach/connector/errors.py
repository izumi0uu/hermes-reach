"""Closed, public-safe Connector error taxonomy."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Final


class ConnectorErrorCategory(str, Enum):
    """Stable Connector failure boundaries."""

    SETUP = "setup"
    TRANSPORT = "transport"
    AUTHORITY = "authority"
    RECEIPT = "receipt"
    SECRET = "secret"
    MODEL = "model"
    FILE = "file"
    BACKEND = "backend"


class ConnectorErrorCode(str, Enum):
    """Closed codes that may cross the Connector trust boundary."""

    UNSUPPORTED_PLATFORM = "unsupported_platform"
    CONNECTOR_NOT_INITIALIZED = "connector_not_initialized"
    CONNECTOR_KEY_LOCKED = "connector_key_locked"
    INTERACTIVE_UNLOCK_REQUIRED = "interactive_unlock_required"
    CONNECTOR_NOT_PAIRED = "connector_not_paired"
    GRANT_REQUIRED = "grant_required"
    CONNECTOR_STATE_INVALID = "connector_state_invalid"
    CONNECTOR_SCHEMA_INCOMPATIBLE = "connector_schema_incompatible"
    CONNECTOR_SERVICE_RUNNING = "connector_service_running"

    CONNECTOR_OFFLINE = "connector_offline"
    CONNECTOR_TLS_FAILED = "connector_tls_failed"
    CONNECTOR_PROTOCOL_MISMATCH = "connector_protocol_mismatch"
    CONNECTOR_DEADLINE_EXCEEDED = "connector_deadline_exceeded"

    DEVICE_REVOKED = "device_revoked"
    GRANT_REVOKED = "grant_revoked"
    GRANT_EXPIRED = "grant_expired"
    GRANT_SUPERSEDED = "grant_superseded"
    GRANT_SCOPE_DENIED = "grant_scope_denied"
    GRANT_LIMIT_EXHAUSTED = "grant_limit_exhausted"
    POLICY_REVISION_STALE = "policy_revision_stale"
    BACKEND_UNBOUND = "backend_unbound"
    REQUEST_REPLAYED = "request_replayed"

    RECEIPT_INVALID = "receipt_invalid"
    RECEIPT_CONTEXT_MISMATCH = "receipt_context_mismatch"
    RECEIPT_EXPIRED = "receipt_expired"
    RECEIPT_REPLAYED = "receipt_replayed"

    SECRET_UNAVAILABLE = "secret_unavailable"
    SECRET_BINDING_DENIED = "secret_binding_denied"

    MODEL_POLICY_DENIED = "model_policy_denied"

    FILE_GRANT_INVALID = "file_grant_invalid"
    FILE_CHANGED = "file_changed"

    BACKEND_INVALID_INPUT = "backend_invalid_input"
    BACKEND_NOT_FOUND = "backend_not_found"
    BACKEND_AUTHENTICATION_REQUIRED = "backend_authentication_required"
    BACKEND_AUTHORIZATION_DENIED = "backend_authorization_denied"
    BACKEND_UNAVAILABLE = "backend_unavailable"
    BACKEND_INCOMPATIBLE = "backend_incompatible"
    BACKEND_DEADLINE_EXCEEDED = "backend_deadline_exceeded"
    BACKEND_RATE_LIMITED = "backend_rate_limited"
    BACKEND_TRANSIENT = "backend_transient"
    BACKEND_PERMANENT = "backend_permanent"
    BACKEND_CONTRACT_VIOLATION = "backend_contract_violation"


@dataclass(frozen=True, slots=True)
class _ErrorDefinition:
    category: ConnectorErrorCategory
    message: str
    remediation: str


_DEFINITIONS: Final[dict[ConnectorErrorCode, _ErrorDefinition]] = {
    ConnectorErrorCode.UNSUPPORTED_PLATFORM: _ErrorDefinition(
        ConnectorErrorCategory.SETUP,
        "This platform cannot provide the required Connector guarantees.",
        "Use a supported Connector or VPS platform.",
    ),
    ConnectorErrorCode.CONNECTOR_NOT_INITIALIZED: _ErrorDefinition(
        ConnectorErrorCategory.SETUP,
        "The Connector identity is not initialized.",
        "Initialize this Connector role from the operator CLI.",
    ),
    ConnectorErrorCode.CONNECTOR_KEY_LOCKED: _ErrorDefinition(
        ConnectorErrorCategory.SETUP,
        "The Connector is locked.",
        "Unlock the foreground Connector from its controlling terminal.",
    ),
    ConnectorErrorCode.INTERACTIVE_UNLOCK_REQUIRED: _ErrorDefinition(
        ConnectorErrorCategory.SETUP,
        "Interactive Connector unlock is required.",
        "Use the original controlling terminal for the foreground service.",
    ),
    ConnectorErrorCode.CONNECTOR_NOT_PAIRED: _ErrorDefinition(
        ConnectorErrorCategory.SETUP,
        "This device is not paired with the Connector.",
        "Complete operator-confirmed device pairing.",
    ),
    ConnectorErrorCode.GRANT_REQUIRED: _ErrorDefinition(
        ConnectorErrorCategory.SETUP,
        "A current Connector grant is required.",
        "Ask the Connector owner to approve an exact operation grant.",
    ),
    ConnectorErrorCode.CONNECTOR_STATE_INVALID: _ErrorDefinition(
        ConnectorErrorCategory.SETUP,
        "The Connector authority state could not be verified.",
        "Lock the Connector and inspect its owner-controlled state.",
    ),
    ConnectorErrorCode.CONNECTOR_SCHEMA_INCOMPATIBLE: _ErrorDefinition(
        ConnectorErrorCategory.SETUP,
        "The Connector authority schema is incompatible.",
        "Use a compatible Reach version without replacing live authority state.",
    ),
    ConnectorErrorCode.CONNECTOR_SERVICE_RUNNING: _ErrorDefinition(
        ConnectorErrorCategory.SETUP,
        "Another Connector service owns the authority writer lease.",
        "Use the running foreground service or stop it before offline changes.",
    ),
    ConnectorErrorCode.CONNECTOR_OFFLINE: _ErrorDefinition(
        ConnectorErrorCategory.TRANSPORT,
        "The Connector is offline.",
        "Start and unlock the trusted Connector, then retry.",
    ),
    ConnectorErrorCode.CONNECTOR_TLS_FAILED: _ErrorDefinition(
        ConnectorErrorCategory.TRANSPORT,
        "The Connector secure transport could not be verified.",
        "Inspect the pinned Connector identity and private-network endpoint.",
    ),
    ConnectorErrorCode.CONNECTOR_PROTOCOL_MISMATCH: _ErrorDefinition(
        ConnectorErrorCategory.TRANSPORT,
        "The Connector protocol is incompatible.",
        "Use compatible Reach Connector versions on both devices.",
    ),
    ConnectorErrorCode.CONNECTOR_DEADLINE_EXCEEDED: _ErrorDefinition(
        ConnectorErrorCategory.TRANSPORT,
        "The Connector request deadline was exceeded.",
        "Retry with the same approved scope and a new request identifier.",
    ),
    ConnectorErrorCode.DEVICE_REVOKED: _ErrorDefinition(
        ConnectorErrorCategory.AUTHORITY,
        "This device has been revoked.",
        "Re-pair the device only after the Connector owner verifies it.",
    ),
    ConnectorErrorCode.GRANT_REVOKED: _ErrorDefinition(
        ConnectorErrorCategory.AUTHORITY,
        "The Connector grant has been revoked.",
        "Ask the Connector owner to approve a new grant if appropriate.",
    ),
    ConnectorErrorCode.GRANT_EXPIRED: _ErrorDefinition(
        ConnectorErrorCategory.AUTHORITY,
        "The Connector grant has expired.",
        "Ask the Connector owner to approve a renewed grant.",
    ),
    ConnectorErrorCode.GRANT_SUPERSEDED: _ErrorDefinition(
        ConnectorErrorCategory.AUTHORITY,
        "The Connector grant revision is no longer current.",
        "Use the current approved grant revision.",
    ),
    ConnectorErrorCode.GRANT_SCOPE_DENIED: _ErrorDefinition(
        ConnectorErrorCategory.AUTHORITY,
        "The Connector grant does not allow this operation.",
        "Use an operation within the approved scope.",
    ),
    ConnectorErrorCode.GRANT_LIMIT_EXHAUSTED: _ErrorDefinition(
        ConnectorErrorCategory.AUTHORITY,
        "The Connector grant request limit is exhausted.",
        "Ask the Connector owner to approve a renewed grant.",
    ),
    ConnectorErrorCode.POLICY_REVISION_STALE: _ErrorDefinition(
        ConnectorErrorCategory.AUTHORITY,
        "The Connector policy revision is no longer current.",
        "Refresh the approved grant under the current policy.",
    ),
    ConnectorErrorCode.BACKEND_UNBOUND: _ErrorDefinition(
        ConnectorErrorCategory.AUTHORITY,
        "No approved Connector backend is bound to this operation.",
        "Configure an exact reviewed backend binding on the Connector.",
    ),
    ConnectorErrorCode.REQUEST_REPLAYED: _ErrorDefinition(
        ConnectorErrorCategory.AUTHORITY,
        "The Connector request identifier was already claimed.",
        "Retry only with a new request identifier.",
    ),
    ConnectorErrorCode.RECEIPT_INVALID: _ErrorDefinition(
        ConnectorErrorCategory.RECEIPT,
        "The Connector receipt could not be verified.",
        "Discard the result and inspect Connector compatibility.",
    ),
    ConnectorErrorCode.RECEIPT_CONTEXT_MISMATCH: _ErrorDefinition(
        ConnectorErrorCategory.RECEIPT,
        "The Connector receipt does not match this request.",
        "Discard the result and retry with a new request identifier.",
    ),
    ConnectorErrorCode.RECEIPT_EXPIRED: _ErrorDefinition(
        ConnectorErrorCategory.RECEIPT,
        "The Connector receipt has expired.",
        "Discard the result and issue a new authorized request.",
    ),
    ConnectorErrorCode.RECEIPT_REPLAYED: _ErrorDefinition(
        ConnectorErrorCategory.RECEIPT,
        "The Connector receipt was already recorded.",
        "Discard the duplicate receipt and inspect the original evidence.",
    ),
    ConnectorErrorCode.SECRET_UNAVAILABLE: _ErrorDefinition(
        ConnectorErrorCategory.SECRET,
        "The approved Connector secret is unavailable.",
        "Inspect the trusted Connector provider configuration.",
    ),
    ConnectorErrorCode.SECRET_BINDING_DENIED: _ErrorDefinition(
        ConnectorErrorCategory.SECRET,
        "The Connector secret binding is not approved.",
        "Configure the exact opaque binding on the trusted Connector.",
    ),
    ConnectorErrorCode.MODEL_POLICY_DENIED: _ErrorDefinition(
        ConnectorErrorCategory.MODEL,
        "Connector model policy denied this operation.",
        "Ask the Connector owner to review the exact model policy.",
    ),
    ConnectorErrorCode.FILE_GRANT_INVALID: _ErrorDefinition(
        ConnectorErrorCategory.FILE,
        "The Connector file grant is invalid.",
        "Ask the Connector owner to approve a new single-file grant.",
    ),
    ConnectorErrorCode.FILE_CHANGED: _ErrorDefinition(
        ConnectorErrorCategory.FILE,
        "The Connector-local file changed after approval.",
        "Review the file and approve a new single-file grant.",
    ),
    ConnectorErrorCode.BACKEND_INVALID_INPUT: _ErrorDefinition(
        ConnectorErrorCategory.BACKEND,
        "The reviewed backend rejected the closed operation input.",
        "Use the documented source-operation request shape.",
    ),
    ConnectorErrorCode.BACKEND_NOT_FOUND: _ErrorDefinition(
        ConnectorErrorCategory.BACKEND,
        "The reviewed backend found no matching resource.",
        "Check the resource identity before retrying.",
    ),
    ConnectorErrorCode.BACKEND_AUTHENTICATION_REQUIRED: _ErrorDefinition(
        ConnectorErrorCategory.BACKEND,
        "The trusted-device platform session requires authentication.",
        "Sign in on the trusted Connector device, then retry.",
    ),
    ConnectorErrorCode.BACKEND_AUTHORIZATION_DENIED: _ErrorDefinition(
        ConnectorErrorCategory.BACKEND,
        "The trusted-device platform session cannot access this resource.",
        "Use a resource visible to the approved platform account.",
    ),
    ConnectorErrorCode.BACKEND_UNAVAILABLE: _ErrorDefinition(
        ConnectorErrorCategory.BACKEND,
        "The reviewed backend is temporarily unavailable.",
        "Restore the trusted-device backend and retry.",
    ),
    ConnectorErrorCode.BACKEND_INCOMPATIBLE: _ErrorDefinition(
        ConnectorErrorCategory.BACKEND,
        "The reviewed backend artifact or session is incompatible.",
        "Restore the exact approved backend closure before retrying.",
    ),
    ConnectorErrorCode.BACKEND_DEADLINE_EXCEEDED: _ErrorDefinition(
        ConnectorErrorCategory.BACKEND,
        "The reviewed backend exceeded its bounded execution deadline.",
        "Retry the same approved operation after the backend recovers.",
    ),
    ConnectorErrorCode.BACKEND_RATE_LIMITED: _ErrorDefinition(
        ConnectorErrorCategory.BACKEND,
        "The platform rate-limited the reviewed backend.",
        "Wait before retrying; do not bypass the platform limit.",
    ),
    ConnectorErrorCode.BACKEND_TRANSIENT: _ErrorDefinition(
        ConnectorErrorCategory.BACKEND,
        "The reviewed backend reported a transient failure.",
        "Retry the same approved operation within its bounded policy.",
    ),
    ConnectorErrorCode.BACKEND_PERMANENT: _ErrorDefinition(
        ConnectorErrorCategory.BACKEND,
        "The reviewed backend could not complete this operation.",
        "Inspect the trusted-device setup before retrying.",
    ),
    ConnectorErrorCode.BACKEND_CONTRACT_VIOLATION: _ErrorDefinition(
        ConnectorErrorCategory.BACKEND,
        "The reviewed backend violated its closed result contract.",
        "Stop using the backend until its pinned integration is reviewed.",
    ),
}


class ConnectorError(Exception):
    """A closed Connector failure that retains no untrusted context."""

    def __init__(
        self,
        code: ConnectorErrorCode,
        *,
        unsafe_context: object | None = None,
    ) -> None:
        if not isinstance(code, ConnectorErrorCode):
            raise ValueError("The Connector error code is invalid.") from None
        definition = _DEFINITIONS[code]
        del unsafe_context
        super().__init__(definition.message)
        self.code = code.value
        self.message = definition.message
        self.remediation = definition.remediation

    def __str__(self) -> str:
        return f"{self.code}: {self.message} {self.remediation}"

    def __repr__(self) -> str:
        return (
            f"ConnectorError(code={self.code!r}, message={self.message!r}, "
            f"remediation={self.remediation!r})"
        )

    def as_data(self) -> dict[str, str]:
        """Return the exact public-safe error object."""

        return {
            "code": self.code,
            "message": self.message,
            "remediation": self.remediation,
        }


def codes_for_category(
    category: ConnectorErrorCategory | str,
) -> tuple[ConnectorErrorCode, ...]:
    """Enumerate a category without accepting unknown values."""

    normalized = ConnectorErrorCategory(category)
    return tuple(
        code for code in ConnectorErrorCode if _DEFINITIONS[code].category is normalized
    )


def category_for_code(code: ConnectorErrorCode) -> ConnectorErrorCategory:
    """Return the closed failure category for one stable code."""

    if not isinstance(code, ConnectorErrorCode):
        raise ValueError("The Connector error code is invalid.") from None
    return _DEFINITIONS[code].category


if set(_DEFINITIONS) != set(ConnectorErrorCode):
    raise RuntimeError("Connector error definitions do not match the closed taxonomy")
