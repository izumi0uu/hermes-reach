"""Live Connector authority ordering and bound signed-receipt issuance."""

from __future__ import annotations

import base64
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Generic, TypeVar

from ..contracts import OperationCall
from .errors import ConnectorErrorCode, category_for_code
from .identity import DevicePrivateIdentity
from .protocol import (
    GrantScope,
    ProtectedOperationPayload,
    ProtocolValidationError,
    PublicBackendIdentity,
    ReceiptFailure,
    ReceiptUsage,
    SignedGrant,
    SignedReceipt,
    SignedRequest,
    create_signed_receipt,
    verify_signed_receipt,
    verify_signed_request,
)
from .store import AuthorityStore, ClaimResult, GrantInspection

HandoffResult = TypeVar("HandoffResult")
ProtocolIdFactory = Callable[[], str]
WallClock = Callable[[], int]


class UnauthenticatedRequestError(ValueError):
    """Traffic that must receive only an unsigned generic rejection."""


class AuthorizedExecution:
    """One quota-spent execution handoff with protected payload kept private."""

    __slots__ = (
        "_protected_payload",
        "remaining_uses",
        "request",
        "required_scope",
        "use_sequence",
    )

    def __init__(
        self,
        request: SignedRequest,
        protected_payload: ProtectedOperationPayload,
        required_scope: GrantScope,
        claim: ClaimResult,
    ) -> None:
        if (
            not claim.accepted
            or claim.use_sequence is None
            or claim.remaining_uses is None
        ):
            raise ValueError("Authorized execution requires a spent grant use.")
        self.request = request
        self.required_scope = required_scope
        self.use_sequence = claim.use_sequence
        self.remaining_uses = claim.remaining_uses
        self._protected_payload = protected_payload

    def operation_call(self) -> OperationCall:
        """Recover the validated call only at the exact executor handoff."""

        return self._protected_payload.to_operation_call()

    def __repr__(self) -> str:
        return (
            "AuthorizedExecution("
            f"request_id={self.request.request_id!r}, "
            f"source={self.request.source!r}, operation={self.request.operation!r}, "
            f"use_sequence={self.use_sequence!r}, protected_payload=<redacted>)"
        )


@dataclass(frozen=True, slots=True)
class AuthorityDecision(Generic[HandoffResult]):
    """Closed result of the live authority transaction and optional handoff."""

    claim: ClaimResult
    receipt_issuer: BoundReceiptIssuer | None = field(repr=False)
    handoff_result: HandoffResult | None = field(default=None, repr=False)

    @property
    def accepted(self) -> bool:
        return self.claim.accepted


class BoundReceiptIssuer:
    """A signer restricted to one exact authenticated request and decision."""

    __slots__ = (
        "_claim",
        "_id_factory",
        "_issued",
        "_mutex",
        "_request",
        "_signer",
        "_started_at",
    )

    def __init__(
        self,
        signer: DevicePrivateIdentity,
        request: SignedRequest,
        claim: ClaimResult,
        *,
        started_at: int,
        id_factory: ProtocolIdFactory,
    ) -> None:
        self._signer = signer
        self._request = request
        self._claim = claim
        self._started_at = started_at
        self._id_factory = id_factory
        self._issued = False
        self._mutex = threading.Lock()

    def issue(
        self,
        *,
        ended_at: int,
        expires_at: int,
        failure_code: ConnectorErrorCode | None = None,
        backend: PublicBackendIdentity | None = None,
        result_count: int = 0,
        truncated: bool = False,
    ) -> SignedReceipt:
        """Sign one closed receipt matching the already committed decision."""

        with self._mutex:
            if self._issued:
                raise ValueError("This authority decision already has a receipt.")
            claim = self._claim
            if claim.accepted:
                if claim.use_sequence is None or claim.remaining_uses is None:
                    raise ValueError("Accepted receipt accounting is unavailable.")
                usage = ReceiptUsage(claim.use_sequence, claim.remaining_uses)
                decision = "allow"
            else:
                if claim.cause_code is None:
                    raise ValueError("Denied receipt cause is unavailable.")
                if failure_code is not None and failure_code is not claim.cause_code:
                    raise ValueError(
                        "Denied receipts cannot replace the authority cause."
                    )
                failure_code = claim.cause_code
                usage = None
                decision = "deny"
                backend = None
                result_count = 0
                truncated = False
            if failure_code is None:
                if backend is None:
                    raise ValueError("Successful receipts require an approved backend.")
                failure = None
                outcome = "ok"
            else:
                failure = ReceiptFailure(
                    category_for_code(failure_code).value, failure_code
                )
                outcome = "error"
            receipt = create_signed_receipt(
                signer=self._signer,
                message_id=self._id_factory(),
                receipt_id=self._id_factory(),
                request=self._request,
                decision=decision,
                failure=failure,
                usage=usage,
                backend=backend,
                started_at=self._started_at,
                ended_at=ended_at,
                expires_at=expires_at,
                result_count=result_count,
                truncated=truncated,
                outcome=outcome,
            )
            verify_signed_receipt(
                receipt,
                pinned_connector=self._signer.public_identity,
                request=self._request,
                now=ended_at,
            )
            self._issued = True
            return receipt

    def __repr__(self) -> str:
        return "BoundReceiptIssuer(<redacted>)"


class GrantAuthority:
    """Serialize mutable authority state through executor handoff."""

    def __init__(
        self,
        store: AuthorityStore,
        *,
        id_factory: ProtocolIdFactory = lambda: _random_protocol_id(),
        clock: WallClock = lambda: int(time.time()),
    ) -> None:
        if not isinstance(store, AuthorityStore):
            raise TypeError("GrantAuthority requires a production authority store.")
        self._store = store
        self._id_factory = id_factory
        self._clock = clock
        self._mutex = threading.RLock()
        self._signer: DevicePrivateIdentity | None = None

    @property
    def is_unlocked(self) -> bool:
        with self._mutex:
            return self._signer is not None

    def _activate_from_service(self, signer: DevicePrivateIdentity) -> None:
        """Activate only after the foreground service completes TTY unlock."""

        if not isinstance(signer, DevicePrivateIdentity):
            raise TypeError("Authority activation requires a Connector signer.")
        if signer.public_identity != self._store.connector_identity:
            raise ValueError("The unlocked signer does not match authority state.")
        with self._mutex:
            self._signer = signer

    def lock(self) -> None:
        """Block new claims after any earlier executor handoff has completed."""

        with self._mutex:
            self._signer = None

    def authorize_and_handoff(
        self,
        request: SignedRequest,
        protected_payload: ProtectedOperationPayload,
        required_scope: GrantScope,
        *,
        now: int,
        handoff: Callable[[AuthorizedExecution], HandoffResult],
    ) -> AuthorityDecision[HandoffResult]:
        """Verify immutable input, commit live authority, then start execution."""

        if (
            not isinstance(request, SignedRequest)
            or not isinstance(protected_payload, ProtectedOperationPayload)
            or not isinstance(required_scope, GrantScope)
        ):
            raise ProtocolValidationError("The authority request type is invalid.")
        if not callable(handoff):
            raise TypeError("Authority executor handoff must be callable.")
        identity = self._store.active_device_identity(request.subject_key_id)
        if identity is None:
            raise UnauthenticatedRequestError(
                "The Connector request could not be authenticated."
            )
        verify_signed_request(
            request,
            pinned_vps=identity,
            expected_connector_key_id=self._store.connector_identity.key_id,
            protected_payload=protected_payload,
            now=now,
        )
        with self._mutex:
            claim_time = max(now, self._clock())
            signer = self._signer
            if signer is None:
                claim = self._store.deny_authenticated_request(
                    request,
                    ConnectorErrorCode.CONNECTOR_KEY_LOCKED,
                    now=claim_time,
                )
                return AuthorityDecision(claim, None)
            claim = self._store.claim(
                request, required_scope=required_scope, now=claim_time
            )
            issuer = BoundReceiptIssuer(
                signer,
                request,
                claim,
                started_at=now,
                id_factory=self._id_factory,
            )
            if not claim.accepted:
                return AuthorityDecision(claim, issuer)
            execution = AuthorizedExecution(
                request, protected_payload, required_scope, claim
            )
            return AuthorityDecision(claim, issuer, handoff(execution))

    def replace_grant(self, grant: SignedGrant, *, now: int) -> GrantInspection:
        with self._mutex:
            return self._store.replace_grant(grant, now=now)

    def revoke_device(self, device_id: str, *, now: int) -> None:
        with self._mutex:
            self._store.revoke_device(device_id, now=now)

    def revoke_grant(self, grant_id: str, *, now: int) -> None:
        with self._mutex:
            self._store.revoke_grant(grant_id, now=now)

    def advance_policy_revision(self, policy_digest: str, *, now: int) -> int:
        with self._mutex:
            return self._store.advance_policy_revision(policy_digest, now=now)


def _random_protocol_id() -> str:
    return base64.b32encode(secrets.token_bytes(16)).decode().rstrip("=").lower()
