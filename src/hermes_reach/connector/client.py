"""VPS pairing, signed request dispatch, and local Connector snapshots."""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import os
import secrets
import stat
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal, Protocol, TypeVar, cast

from ..catalog import get_operation, get_source
from ..contracts import OperationCall
from ..runtime.availability import AvailabilityRecord
from .audit import ReceiptEvidenceError, ReceiptEvidenceLedger, verify_response
from .errors import ConnectorError, ConnectorErrorCode
from .identity import (
    DevicePrivateIdentity,
    DevicePublicIdentity,
    VpsKeyStore,
    _open_state_directory,
)
from .limits import (
    ID_BASE32_LENGTH,
    ID_ENTROPY_BYTES,
    KEY_ID_BASE32_LENGTH,
    MAX_CLOCK_SKEW_SECONDS,
    MAX_FRAME_BYTES,
    MAX_REQUEST_TTL_SECONDS,
    MAX_TIMESTAMP_SECONDS,
    PAIRING_TTL_SECONDS,
)
from .protocol import (
    ErrorFrame,
    GrantScope,
    OperationInvocationV1,
    OperationResponseV1,
    PairingChallenge,
    PairingComplete,
    PairingInit,
    PairingResolution,
    ProtocolValidationError,
    SignedGrant,
    SignedReceipt,
    canonical_json_bytes,
    create_pairing_init,
    create_signed_request,
    encode_record,
    load_canonical_json,
    pairing_ca_der,
    pairing_sas,
    pairing_transcript_hash,
    parse_record,
    protect_operation_call,
    record_digest,
    verify_pairing_resolution,
    verify_record,
)
from .tls import ConnectorCACertificate, verify_connector_ca_der
from .transport import (
    ConnectorDeliveryError,
    ConnectorTransport,
    PairingExchange,
    PairingWssClient,
    WssEndpoint,
)

_PROFILE_VERSION: Final = 1
_SNAPSHOT_VERSION: Final = 1
_PROFILE_FILENAME: Final = "vps-profile.json"
_SNAPSHOT_FILENAME: Final = "vps-connector-snapshot.json"
_MAX_PROFILE_BYTES: Final = MAX_FRAME_BYTES
_MAX_SNAPSHOT_BYTES: Final = 64 * 1024
DEFAULT_SNAPSHOT_TTL_SECONDS: Final = 60
_PAIRING_POLL_INTERVAL_SECONDS: Final = 1.0
_ENDPOINT_DIGEST_DOMAIN: Final = b"hermes-reach:connector:endpoint:v1\x00"

SnapshotState = Literal["authenticated", "disconnected", "unavailable"]
_RecordT = TypeVar(
    "_RecordT", PairingInit, PairingChallenge, PairingComplete, SignedGrant
)


class PairingClient(Protocol):
    """Injectable pairing-only transport used by the VPS orchestrator."""

    async def exchange(
        self, pairing_init: PairingInit, *, deadline: float
    ) -> PairingExchange: ...

    async def poll(
        self,
        pairing_init: PairingInit,
        exchange: PairingExchange,
        *,
        deadline: float,
    ) -> PairingResolution | None: ...


@dataclass(frozen=True, slots=True)
class PendingVpsProfile:
    """Exact pairing state retained until a verified resolution is committed."""

    endpoint: WssEndpoint
    vps_key_id: str
    pairing_init: PairingInit
    pairing_challenge: PairingChallenge | None = None
    observed_tls_leaf_fingerprint: str | None = None

    @property
    def version(self) -> int:
        return _PROFILE_VERSION


@dataclass(frozen=True, slots=True)
class PairedVpsProfile:
    """Pinned Connector identity, CA, and current signed grant on one VPS."""

    endpoint: WssEndpoint
    vps_key_id: str
    connector_identity: DevicePublicIdentity
    connector_ca_der: bytes
    signed_grant: SignedGrant
    pairing_complete: PairingComplete

    @property
    def version(self) -> int:
        return _PROFILE_VERSION

    @property
    def current_grant(self) -> SignedGrant:
        return self.signed_grant

    def authority(self, *, now: int | None = None) -> ConnectorCACertificate:
        """Re-verify the persisted CA against the pinned Connector identity."""

        return verify_connector_ca_der(
            self.connector_ca_der,
            self.connector_identity,
            now=now,
        )


VpsProfile = PendingVpsProfile | PairedVpsProfile


@dataclass(frozen=True, slots=True)
class PairingDisplay:
    """Safe values independently displayed on the VPS before approval."""

    pairing_id: str
    connector_key_id: str
    connector_fingerprint: str
    sas: str
    deadline: int
    scopes: tuple[tuple[str, str, str, str | None], ...]
    grant_expires_at: int
    grant_max_uses: int

    def as_data(self) -> dict[str, object]:
        return {
            "connector_fingerprint": self.connector_fingerprint,
            "connector_key_id": self.connector_key_id,
            "deadline": self.deadline,
            "grant_expires_at": self.grant_expires_at,
            "grant_max_uses": self.grant_max_uses,
            "pairing_id": self.pairing_id,
            "sas": self.sas,
            "scopes": [
                {
                    "data_scope": data_scope,
                    "operation": operation,
                    "source": source,
                    "capability_id": capability_id,
                }
                for source, operation, data_scope, capability_id in self.scopes
            ],
        }


@dataclass(frozen=True, slots=True)
class ConnectorSnapshot:
    """Closed local projection of the last authenticated Connector result."""

    connector_key_id: str
    grant_id: str
    grant_revision: int
    observed_at: int
    state: SnapshotState
    cause_code: ConnectorErrorCode | None
    scopes: tuple[tuple[str, str], ...]

    @property
    def version(self) -> int:
        return _SNAPSHOT_VERSION


class VpsProfileStore:
    """Owner-only canonical and atomic VPS pairing profile persistence."""

    def __init__(self, state_directory: Path) -> None:
        if not isinstance(state_directory, Path) or not state_directory.is_absolute():
            raise ValueError("The VPS profile state directory must be absolute.")
        self._state_directory = state_directory

    def load(self) -> VpsProfile | None:
        payload = _read_owner_file(
            self._state_directory,
            _PROFILE_FILENAME,
            maximum=_MAX_PROFILE_BYTES,
        )
        if payload is None:
            return None
        try:
            return _parse_profile(payload)
        except ConnectorError:
            raise
        except (TypeError, ValueError):
            raise ConnectorError(ConnectorErrorCode.CONNECTOR_STATE_INVALID) from None

    def save_pending(self, profile: PendingVpsProfile) -> None:
        """Create pending state or add the one exact verified challenge."""

        _validate_pending_profile(profile)
        current = self.load()
        if isinstance(current, PairedVpsProfile):
            raise ConnectorError(ConnectorErrorCode.CONNECTOR_STATE_INVALID)
        if isinstance(current, PendingVpsProfile):
            same_request = (
                current.endpoint == profile.endpoint
                and current.vps_key_id == profile.vps_key_id
                and encode_record(current.pairing_init)
                == encode_record(profile.pairing_init)
            )
            allowed_challenge_fill = (
                same_request
                and current.pairing_challenge is None
                and current.observed_tls_leaf_fingerprint is None
                and profile.pairing_challenge is not None
                and profile.observed_tls_leaf_fingerprint is not None
            )
            expired_replacement = (
                current.pairing_init.deadline <= profile.pairing_init.issued_at
                and profile.pairing_challenge is None
                and profile.observed_tls_leaf_fingerprint is None
            )
            if (
                current != profile
                and not allowed_challenge_fill
                and not expired_replacement
            ):
                raise ConnectorError(ConnectorErrorCode.CONNECTOR_STATE_INVALID)
        _atomic_write_owner_file(
            self._state_directory,
            _PROFILE_FILENAME,
            canonical_json_bytes(_profile_mapping(profile)),
            maximum=_MAX_PROFILE_BYTES,
        )

    def commit_paired(
        self,
        pending: PendingVpsProfile,
        resolution: PairingResolution,
        *,
        now: int,
    ) -> PairedVpsProfile:
        """Atomically replace the exact pending exchange after full verification."""

        _require_timestamp(now)
        current = self.load()
        if current != pending or pending.pairing_challenge is None:
            raise ConnectorError(ConnectorErrorCode.CONNECTOR_STATE_INVALID)
        leaf_fingerprint = pending.observed_tls_leaf_fingerprint
        if leaf_fingerprint is None:
            raise ConnectorError(ConnectorErrorCode.CONNECTOR_STATE_INVALID)
        try:
            signed_grant = verify_pairing_resolution(
                resolution,
                pairing_init=pending.pairing_init,
                pairing_challenge=pending.pairing_challenge,
                observed_tls_leaf_fingerprint=leaf_fingerprint,
                now=now,
            )
            connector_identity = DevicePublicIdentity.from_wire(
                pending.pairing_challenge.connector_public_key
            )
            ca_der = pairing_ca_der(pending.pairing_challenge)
            verify_connector_ca_der(ca_der, connector_identity, now=now)
            paired = PairedVpsProfile(
                endpoint=pending.endpoint,
                vps_key_id=pending.vps_key_id,
                connector_identity=connector_identity,
                connector_ca_der=ca_der,
                signed_grant=signed_grant,
                pairing_complete=resolution.pairing_complete,
            )
            _validate_paired_profile(paired, now=now)
        except ConnectorError:
            raise
        except (ProtocolValidationError, TypeError, ValueError):
            raise ConnectorError(
                ConnectorErrorCode.CONNECTOR_PROTOCOL_MISMATCH
            ) from None
        _atomic_write_owner_file(
            self._state_directory,
            _PROFILE_FILENAME,
            canonical_json_bytes(_profile_mapping(paired)),
            maximum=_MAX_PROFILE_BYTES,
        )
        return paired


class VpsPairingOrchestrator:
    """Create, display, poll, verify, and durably commit one VPS pairing."""

    def __init__(
        self,
        key_store: VpsKeyStore,
        profile_store: VpsProfileStore,
        *,
        client_factory: Callable[[WssEndpoint], PairingClient] | None = None,
        wall_clock: Callable[[], int] | None = None,
        monotonic_clock: Callable[[], float] | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        id_factory: Callable[[], str] | None = None,
        nonce_factory: Callable[[int], bytes] | None = None,
    ) -> None:
        if not isinstance(key_store, VpsKeyStore) or not isinstance(
            profile_store, VpsProfileStore
        ):
            raise TypeError("VPS pairing requires key and profile stores.")
        self._key_store = key_store
        self._profile_store = profile_store
        self._client_factory = (
            (lambda endpoint: PairingWssClient(endpoint))
            if client_factory is None
            else client_factory
        )
        self._wall_clock = _wall_timestamp if wall_clock is None else wall_clock
        self._monotonic_clock = (
            time.monotonic if monotonic_clock is None else monotonic_clock
        )
        self._sleep = asyncio.sleep if sleep is None else sleep
        self._id_factory = _random_protocol_id if id_factory is None else id_factory
        self._nonce_factory = (
            secrets.token_bytes if nonce_factory is None else nonce_factory
        )

    async def pair(
        self,
        endpoint: WssEndpoint,
        *,
        device_label: str,
        requested_scopes: tuple[GrantScope, ...],
        grant_expires_at: int,
        grant_max_uses: int,
        display: Callable[[PairingDisplay], None],
    ) -> PairedVpsProfile:
        if not isinstance(endpoint, WssEndpoint) or not callable(display):
            raise TypeError("VPS pairing requires an endpoint and display callback.")
        identity = self._key_store.load()
        now = _clock_value(self._wall_clock)
        current = self._profile_store.load()
        if current is None or (
            isinstance(current, PendingVpsProfile)
            and current.pairing_init.deadline <= now
        ):
            try:
                pairing_init = create_pairing_init(
                    identity,
                    message_id=self._id_factory(),
                    pairing_id=self._id_factory(),
                    device_label=device_label,
                    vps_nonce=self._nonce_factory(32),
                    endpoint_digest=_endpoint_digest(endpoint),
                    requested_scopes=requested_scopes,
                    grant_expires_at=grant_expires_at,
                    grant_max_uses=grant_max_uses,
                    issued_at=now,
                    deadline=now + PAIRING_TTL_SECONDS,
                )
            except (ProtocolValidationError, TypeError, ValueError):
                raise ConnectorError(
                    ConnectorErrorCode.CONNECTOR_STATE_INVALID
                ) from None
            pending = PendingVpsProfile(
                endpoint, identity.public_identity.key_id, pairing_init
            )
            self._profile_store.save_pending(pending)
        elif isinstance(current, PendingVpsProfile):
            pending = current
            if not _matches_pairing_request(
                pending,
                endpoint=endpoint,
                identity=identity,
                device_label=device_label,
                requested_scopes=requested_scopes,
            ):
                raise ConnectorError(ConnectorErrorCode.CONNECTOR_STATE_INVALID)
        else:
            raise ConnectorError(ConnectorErrorCode.CONNECTOR_STATE_INVALID)

        client = self._client_factory(endpoint)
        exchange = _pending_exchange(pending, now=now)
        if exchange is None:
            exchange = await client.exchange(
                pending.pairing_init,
                deadline=self._transport_deadline(pending.pairing_init.deadline),
            )
            pending = PendingVpsProfile(
                endpoint=pending.endpoint,
                vps_key_id=pending.vps_key_id,
                pairing_init=pending.pairing_init,
                pairing_challenge=exchange.challenge,
                observed_tls_leaf_fingerprint=exchange.observed_tls_leaf_fingerprint,
            )
            self._profile_store.save_pending(pending)

        display(_pairing_display(pending))
        while True:
            now = _clock_value(self._wall_clock)
            if now >= exchange.challenge.deadline:
                raise ConnectorError(ConnectorErrorCode.CONNECTOR_DEADLINE_EXCEEDED)
            resolution = await client.poll(
                pending.pairing_init,
                exchange,
                deadline=self._transport_deadline(exchange.challenge.deadline),
            )
            if resolution is not None:
                return self._profile_store.commit_paired(
                    pending,
                    resolution,
                    now=_clock_value(self._wall_clock),
                )
            remaining = exchange.challenge.deadline - _clock_value(self._wall_clock)
            if remaining <= 0:
                raise ConnectorError(ConnectorErrorCode.CONNECTOR_DEADLINE_EXCEEDED)
            await self._sleep(min(_PAIRING_POLL_INTERVAL_SECONDS, float(remaining)))

    def _transport_deadline(self, wall_deadline: int) -> float:
        remaining = wall_deadline - _clock_value(self._wall_clock)
        if remaining <= 0:
            raise ConnectorError(ConnectorErrorCode.CONNECTOR_DEADLINE_EXCEEDED)
        return float(self._monotonic_clock()) + remaining


class ConnectorSnapshotStore:
    """Owner-only canonical snapshot store; reads never probe the Connector."""

    def __init__(self, state_directory: Path) -> None:
        if not isinstance(state_directory, Path) or not state_directory.is_absolute():
            raise ValueError("The Connector snapshot state directory must be absolute.")
        self._state_directory = state_directory

    def load(self) -> ConnectorSnapshot | None:
        payload = _read_owner_file(
            self._state_directory,
            _SNAPSHOT_FILENAME,
            maximum=_MAX_SNAPSHOT_BYTES,
        )
        if payload is None:
            return None
        try:
            return _parse_snapshot(payload)
        except ConnectorError:
            raise
        except (TypeError, ValueError):
            raise ConnectorError(ConnectorErrorCode.CONNECTOR_STATE_INVALID) from None

    def save(self, snapshot: ConnectorSnapshot) -> None:
        _validate_snapshot(snapshot)
        _atomic_write_owner_file(
            self._state_directory,
            _SNAPSHOT_FILENAME,
            canonical_json_bytes(_snapshot_mapping(snapshot)),
            maximum=_MAX_SNAPSHOT_BYTES,
        )


class ConnectorAvailabilityResolver:
    """Resolve a future Connector binding solely from verified local files."""

    def __init__(
        self,
        profile_store: VpsProfileStore,
        snapshot_store: ConnectorSnapshotStore,
        *,
        clock: Callable[[], int] | None = None,
        ttl_seconds: int = DEFAULT_SNAPSHOT_TTL_SECONDS,
    ) -> None:
        if not isinstance(profile_store, VpsProfileStore) or not isinstance(
            snapshot_store, ConnectorSnapshotStore
        ):
            raise TypeError("Connector availability requires local stores.")
        if type(ttl_seconds) is not int or not 1 <= ttl_seconds <= 300:
            raise ValueError("The Connector snapshot TTL is invalid.")
        self._profile_store = profile_store
        self._snapshot_store = snapshot_store
        self._clock = _wall_timestamp if clock is None else clock
        self._ttl_seconds = ttl_seconds

    def __call__(self, source: str, operation: str) -> AvailabilityRecord:
        return self.resolve(source, operation)

    def resolve(self, source: str, operation: str) -> AvailabilityRecord:
        _require_catalog_operation(source, operation)
        now = _clock_value(self._clock)
        try:
            profile = self._profile_store.load()
        except ConnectorError as error:
            return AvailabilityRecord(
                "unavailable",
                "The local Connector profile could not be verified.",
                cause_code=error.code,
            )
        if not isinstance(profile, PairedVpsProfile):
            return AvailabilityRecord(
                "setup_required",
                "Pair this VPS with a trusted Connector.",
                cause_code=ConnectorErrorCode.CONNECTOR_NOT_PAIRED.value,
            )
        grant_error = _grant_error(profile, source, operation, now=now)
        if grant_error is not None:
            state: Literal["setup_required", "unavailable"] = (
                "setup_required"
                if grant_error is ConnectorErrorCode.GRANT_SCOPE_DENIED
                else "unavailable"
            )
            return AvailabilityRecord(
                state,
                "No current Connector grant authorizes this operation.",
                cause_code=grant_error.value,
            )
        try:
            snapshot = self._snapshot_store.load()
        except ConnectorError as error:
            return AvailabilityRecord(
                "unavailable",
                "The local Connector snapshot could not be verified.",
                cause_code=error.code,
            )
        if snapshot is None or not _snapshot_matches_profile(snapshot, profile):
            return AvailabilityRecord(
                "degraded",
                "The Connector has no recent authenticated snapshot.",
                cause_code=ConnectorErrorCode.CONNECTOR_OFFLINE.value,
            )
        if snapshot.observed_at > now + MAX_CLOCK_SKEW_SECONDS:
            return AvailabilityRecord(
                "unavailable",
                "The Connector snapshot is incompatible.",
                cause_code=ConnectorErrorCode.CONNECTOR_SCHEMA_INCOMPATIBLE.value,
                snapshot_at=snapshot.observed_at,
            )
        if (source, operation) not in snapshot.scopes:
            return AvailabilityRecord(
                "degraded",
                "The exact Connector operation has no recent snapshot.",
                cause_code=ConnectorErrorCode.CONNECTOR_OFFLINE.value,
                snapshot_at=snapshot.observed_at,
            )
        if now - snapshot.observed_at > self._ttl_seconds:
            return AvailabilityRecord(
                "degraded",
                "The Connector is offline or its snapshot is stale.",
                cause_code=ConnectorErrorCode.CONNECTOR_OFFLINE.value,
                snapshot_at=snapshot.observed_at,
            )
        if snapshot.state == "disconnected":
            cause = snapshot.cause_code or ConnectorErrorCode.CONNECTOR_OFFLINE
            return AvailabilityRecord(
                "degraded",
                "The Connector is offline or its snapshot is stale.",
                cause_code=cause.value,
                snapshot_at=snapshot.observed_at,
            )
        if snapshot.state == "unavailable":
            cause = snapshot.cause_code or ConnectorErrorCode.CONNECTOR_STATE_INVALID
            return AvailabilityRecord(
                "unavailable",
                "The Connector is not authorized for this operation.",
                cause_code=cause.value,
                snapshot_at=snapshot.observed_at,
            )
        return AvailabilityRecord(
            "available",
            "A recent authenticated Connector snapshot is available.",
            snapshot_at=snapshot.observed_at,
        )


class ConnectorClient:
    """Fail fast locally, sign one request, and trust only a verified receipt."""

    def __init__(
        self,
        profile: PairedVpsProfile,
        vps_identity: DevicePrivateIdentity,
        transport: ConnectorTransport,
        evidence_ledger: ReceiptEvidenceLedger,
        snapshot_store: ConnectorSnapshotStore,
        *,
        wall_clock: Callable[[], int] | None = None,
        monotonic_clock: Callable[[], float] | None = None,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        if not isinstance(profile, PairedVpsProfile) or not isinstance(
            vps_identity, DevicePrivateIdentity
        ):
            raise TypeError("Connector requests require a paired VPS identity.")
        if not isinstance(evidence_ledger, ReceiptEvidenceLedger) or not isinstance(
            snapshot_store, ConnectorSnapshotStore
        ):
            raise TypeError("Connector requests require evidence and snapshot stores.")
        if vps_identity.public_identity.key_id != profile.vps_key_id:
            raise ConnectorError(ConnectorErrorCode.CONNECTOR_STATE_INVALID)
        _validate_paired_profile(profile)
        self._profile = profile
        self._identity = vps_identity
        self._transport = transport
        self._evidence_ledger = evidence_ledger
        self._snapshot_store = snapshot_store
        self._wall_clock = _wall_timestamp if wall_clock is None else wall_clock
        self._monotonic_clock = (
            time.monotonic if monotonic_clock is None else monotonic_clock
        )
        self._id_factory = _random_protocol_id if id_factory is None else id_factory

    async def execute(
        self, call: OperationCall, *, trace_id: str
    ) -> OperationResponseV1:
        if not isinstance(call, OperationCall):
            raise TypeError("Connector requests require a validated operation call.")
        now = _clock_value(self._wall_clock)
        grant_error = _grant_error(
            self._profile,
            call.source.name,
            call.operation.name,
            now=now,
        )
        if grant_error is not None:
            raise ConnectorError(grant_error)
        grant = self._profile.signed_grant.claims
        protected = protect_operation_call(call)
        ttl = min(MAX_REQUEST_TTL_SECONDS, call.operation.runtime.total_timeout_seconds)
        deadline = min(now + ttl, grant.expires_at)
        request = create_signed_request(
            self._identity,
            message_id=self._id_factory(),
            request_id=self._id_factory(),
            trace_id=trace_id,
            audience_key_id=self._profile.connector_identity.key_id,
            grant_id=grant.grant_id,
            grant_revision=grant.revision,
            policy_revision=grant.policy_revision,
            source=call.source.name,
            operation=call.operation.name,
            issued_at=now,
            deadline=deadline,
            protected_payload=protected,
        )
        invocation = OperationInvocationV1(request.message_id, request, protected)
        transport_deadline = float(self._monotonic_clock()) + (deadline - now)
        try:
            response = await self._exchange_with_retry(
                invocation, deadline=transport_deadline
            )
        except ConnectorError as error:
            code = _transport_error_code(error)
            _save_snapshot_after_error(
                self._snapshot_store,
                _snapshot_for_transport_error(
                    self._profile,
                    _clock_value(self._wall_clock),
                    code,
                    source=call.source.name,
                    operation=call.operation.name,
                ),
            )
            if code.value != error.code:
                raise ConnectorError(code) from None
            raise
        except Exception:
            _save_snapshot_after_error(
                self._snapshot_store,
                _snapshot(
                    self._profile,
                    _clock_value(self._wall_clock),
                    "disconnected",
                    ConnectorErrorCode.CONNECTOR_OFFLINE,
                    source=call.source.name,
                    operation=call.operation.name,
                ),
            )
            raise ConnectorError(ConnectorErrorCode.CONNECTOR_OFFLINE) from None
        if isinstance(response, ErrorFrame):
            _save_snapshot_after_error(
                self._snapshot_store,
                _snapshot(
                    self._profile,
                    _clock_value(self._wall_clock),
                    "unavailable",
                    ConnectorErrorCode.CONNECTOR_PROTOCOL_MISMATCH,
                    source=call.source.name,
                    operation=call.operation.name,
                ),
            )
            raise ConnectorError(ConnectorErrorCode.CONNECTOR_PROTOCOL_MISMATCH)
        if not isinstance(response, OperationResponseV1):
            _save_snapshot_after_error(
                self._snapshot_store,
                _snapshot(
                    self._profile,
                    _clock_value(self._wall_clock),
                    "unavailable",
                    ConnectorErrorCode.CONNECTOR_PROTOCOL_MISMATCH,
                    source=call.source.name,
                    operation=call.operation.name,
                ),
            )
            raise ConnectorError(ConnectorErrorCode.CONNECTOR_PROTOCOL_MISMATCH)
        observed_at = _clock_value(self._wall_clock)
        try:
            verify_response(
                response,
                pinned_connector=self._profile.connector_identity,
                request=request,
                now=observed_at,
            )
            self._evidence_ledger.append_verified(
                response.receipt,
                request=request,
                now=observed_at,
            )
        except ConnectorError:
            raise
        except ReceiptEvidenceError:
            _save_snapshot_after_error(
                self._snapshot_store,
                _snapshot(
                    self._profile,
                    _clock_value(self._wall_clock),
                    "unavailable",
                    ConnectorErrorCode.CONNECTOR_STATE_INVALID,
                    source=request.source,
                    operation=request.operation,
                ),
            )
            raise ConnectorError(ConnectorErrorCode.CONNECTOR_STATE_INVALID) from None
        receipt = response.receipt
        snapshot = _snapshot_from_receipt(self._profile, receipt, observed_at)
        self._snapshot_store.save(snapshot)
        if receipt.failure is not None:
            raise ConnectorError(receipt.failure.cause_code)
        return response

    async def _exchange_with_retry(
        self, invocation: OperationInvocationV1, *, deadline: float
    ) -> OperationResponseV1 | ErrorFrame:
        for attempt in range(2):
            try:
                return await self._transport.exchange(invocation, deadline=deadline)
            except ConnectorError as error:
                if attempt == 0 and self._retry_eligible(error, deadline=deadline):
                    continue
                raise
        raise AssertionError("The Connector retry loop did not terminate.")

    def _retry_eligible(self, error: ConnectorError, *, deadline: float) -> bool:
        if self._monotonic_clock() >= deadline:
            return False
        code = ConnectorErrorCode(error.code)
        if code not in {
            ConnectorErrorCode.CONNECTOR_OFFLINE,
            ConnectorErrorCode.CONNECTOR_DEADLINE_EXCEEDED,
        }:
            return False
        if isinstance(error, ConnectorDeliveryError):
            return error.delivery_state in {"not_sent", "delivery_unknown"}
        return True


def _endpoint_digest(endpoint: WssEndpoint) -> str:
    return hashlib.sha256(
        _ENDPOINT_DIGEST_DOMAIN + endpoint.uri.encode("ascii")
    ).hexdigest()


def _matches_pairing_request(
    pending: PendingVpsProfile,
    *,
    endpoint: WssEndpoint,
    identity: DevicePrivateIdentity,
    device_label: str,
    requested_scopes: tuple[GrantScope, ...],
) -> bool:
    pairing_init = pending.pairing_init
    return (
        pending.endpoint == endpoint
        and pending.vps_key_id == identity.public_identity.key_id
        and pairing_init.device_label == device_label
        and pairing_init.requested_scopes == requested_scopes
    )


def _pending_exchange(
    pending: PendingVpsProfile, *, now: int
) -> PairingExchange | None:
    challenge = pending.pairing_challenge
    leaf_fingerprint = pending.observed_tls_leaf_fingerprint
    if challenge is None and leaf_fingerprint is None:
        return None
    if challenge is None or leaf_fingerprint is None:
        raise ConnectorError(ConnectorErrorCode.CONNECTOR_STATE_INVALID)
    try:
        connector_identity = DevicePublicIdentity.from_wire(
            challenge.connector_public_key
        )
        authority = verify_connector_ca_der(
            pairing_ca_der(challenge), connector_identity, now=now
        )
    except (ConnectorError, ProtocolValidationError, TypeError, ValueError):
        raise ConnectorError(ConnectorErrorCode.CONNECTOR_STATE_INVALID) from None
    return PairingExchange(challenge, authority, leaf_fingerprint)


def _pairing_display(pending: PendingVpsProfile) -> PairingDisplay:
    challenge = pending.pairing_challenge
    leaf_fingerprint = pending.observed_tls_leaf_fingerprint
    if challenge is None or leaf_fingerprint is None:
        raise ConnectorError(ConnectorErrorCode.CONNECTOR_STATE_INVALID)
    connector_identity = DevicePublicIdentity.from_wire(challenge.connector_public_key)
    transcript = pairing_transcript_hash(
        encode_record(pending.pairing_init),
        encode_record(challenge),
        observed_tls_leaf_fingerprint=leaf_fingerprint,
    )
    return PairingDisplay(
        pairing_id=pending.pairing_init.pairing_id,
        connector_key_id=connector_identity.key_id,
        connector_fingerprint=connector_identity.fingerprint,
        sas=pairing_sas(transcript),
        deadline=challenge.deadline,
        scopes=tuple(
            (
                scope.source,
                scope.operation,
                scope.data_scope,
                scope.capability_id,
            )
            for scope in pending.pairing_init.requested_scopes
        ),
        grant_expires_at=pending.pairing_init.grant_expires_at,
        grant_max_uses=pending.pairing_init.grant_max_uses,
    )


def _grant_error(
    profile: PairedVpsProfile, source: str, operation: str, *, now: int
) -> ConnectorErrorCode | None:
    _require_timestamp(now)
    grant = profile.signed_grant
    try:
        verify_record(grant, profile.connector_identity)
    except (ProtocolValidationError, TypeError, ValueError):
        return ConnectorErrorCode.CONNECTOR_STATE_INVALID
    claims = grant.claims
    if (
        claims.issuer_key_id != profile.connector_identity.key_id
        or claims.subject_key_id != profile.vps_key_id
    ):
        return ConnectorErrorCode.CONNECTOR_STATE_INVALID
    if now >= claims.expires_at:
        return ConnectorErrorCode.GRANT_EXPIRED
    if now < claims.not_before:
        return ConnectorErrorCode.GRANT_REQUIRED
    if not any(
        scope.source == source
        and scope.operation == operation
        and scope.data_scope == _operation_data_scope(source, operation)
        for scope in claims.scopes
    ):
        return ConnectorErrorCode.GRANT_SCOPE_DENIED
    return None


def _operation_data_scope(source: str, operation: str) -> str:
    source_spec = get_source(source)
    operation_spec = (
        get_operation(source_spec, operation) if source_spec is not None else None
    )
    if operation_spec is None:
        raise ConnectorError(ConnectorErrorCode.CONNECTOR_STATE_INVALID)
    return operation_spec.runtime.data_scope


def _snapshot(
    profile: PairedVpsProfile,
    observed_at: int,
    state: SnapshotState,
    cause_code: ConnectorErrorCode | None,
    *,
    source: str,
    operation: str,
) -> ConnectorSnapshot:
    claims = profile.signed_grant.claims
    # This is one latest exact-operation projection, not a mutable grant-scope
    # table; a later operation deliberately replaces an earlier advisory state.
    return ConnectorSnapshot(
        connector_key_id=profile.connector_identity.key_id,
        grant_id=claims.grant_id,
        grant_revision=claims.revision,
        observed_at=observed_at,
        state=state,
        cause_code=cause_code,
        scopes=((source, operation),),
    )


def _snapshot_for_transport_error(
    profile: PairedVpsProfile,
    observed_at: int,
    code: ConnectorErrorCode,
    *,
    source: str,
    operation: str,
) -> ConnectorSnapshot:
    if code in {
        ConnectorErrorCode.CONNECTOR_TLS_FAILED,
        ConnectorErrorCode.CONNECTOR_PROTOCOL_MISMATCH,
    }:
        return _snapshot(
            profile,
            observed_at,
            "unavailable",
            code,
            source=source,
            operation=operation,
        )
    return _snapshot(
        profile,
        observed_at,
        "disconnected",
        ConnectorErrorCode.CONNECTOR_OFFLINE,
        source=source,
        operation=operation,
    )


def _transport_error_code(error: ConnectorError) -> ConnectorErrorCode:
    code = ConnectorErrorCode(error.code)
    if code in {
        ConnectorErrorCode.CONNECTOR_OFFLINE,
        ConnectorErrorCode.CONNECTOR_TLS_FAILED,
        ConnectorErrorCode.CONNECTOR_PROTOCOL_MISMATCH,
        ConnectorErrorCode.CONNECTOR_DEADLINE_EXCEEDED,
    }:
        return code
    return ConnectorErrorCode.CONNECTOR_PROTOCOL_MISMATCH


def _snapshot_from_receipt(
    profile: PairedVpsProfile, receipt: SignedReceipt, observed_at: int
) -> ConnectorSnapshot:
    failure = receipt.failure
    if failure is None:
        return _snapshot(
            profile,
            observed_at,
            "authenticated",
            None,
            source=receipt.source,
            operation=receipt.operation,
        )
    if (
        receipt.decision == "deny"
        and failure.cause_code is ConnectorErrorCode.REQUEST_REPLAYED
    ):
        return _snapshot(
            profile,
            observed_at,
            "disconnected",
            ConnectorErrorCode.REQUEST_REPLAYED,
            source=receipt.source,
            operation=receipt.operation,
        )
    if failure.cause_code in {
        ConnectorErrorCode.BACKEND_INVALID_INPUT,
        ConnectorErrorCode.BACKEND_NOT_FOUND,
    }:
        return _snapshot(
            profile,
            observed_at,
            "authenticated",
            None,
            source=receipt.source,
            operation=receipt.operation,
        )
    if failure.cause_code in {
        ConnectorErrorCode.BACKEND_UNAVAILABLE,
        ConnectorErrorCode.BACKEND_DEADLINE_EXCEEDED,
        ConnectorErrorCode.BACKEND_RATE_LIMITED,
        ConnectorErrorCode.BACKEND_TRANSIENT,
    }:
        return _snapshot(
            profile,
            observed_at,
            "disconnected",
            failure.cause_code,
            source=receipt.source,
            operation=receipt.operation,
        )
    return _snapshot(
        profile,
        observed_at,
        "unavailable",
        failure.cause_code,
        source=receipt.source,
        operation=receipt.operation,
    )


def _save_snapshot_after_error(
    store: ConnectorSnapshotStore, snapshot: ConnectorSnapshot
) -> None:
    """Keep the original closed dispatch error if local projection also fails."""

    try:
        store.save(snapshot)
    except ConnectorError:
        pass


def _snapshot_matches_profile(
    snapshot: ConnectorSnapshot, profile: PairedVpsProfile
) -> bool:
    claims = profile.signed_grant.claims
    return (
        snapshot.connector_key_id == profile.connector_identity.key_id
        and snapshot.grant_id == claims.grant_id
        and snapshot.grant_revision == claims.revision
    )


def _validate_pending_profile(profile: PendingVpsProfile) -> None:
    if not isinstance(profile, PendingVpsProfile):
        raise TypeError("The pending VPS profile type is invalid.")
    pairing_init = profile.pairing_init
    if (
        pairing_init.vps_key_id != profile.vps_key_id
        or pairing_init.endpoint_digest != _endpoint_digest(profile.endpoint)
    ):
        raise ConnectorError(ConnectorErrorCode.CONNECTOR_STATE_INVALID)
    try:
        vps_identity = DevicePublicIdentity.from_wire(pairing_init.vps_public_key)
        verify_record(pairing_init, vps_identity)
    except (ProtocolValidationError, TypeError, ValueError):
        raise ConnectorError(ConnectorErrorCode.CONNECTOR_STATE_INVALID) from None
    challenge = profile.pairing_challenge
    leaf_fingerprint = profile.observed_tls_leaf_fingerprint
    if challenge is None and leaf_fingerprint is None:
        return
    if challenge is None or leaf_fingerprint is None:
        raise ConnectorError(ConnectorErrorCode.CONNECTOR_STATE_INVALID)
    try:
        connector_identity = DevicePublicIdentity.from_wire(
            challenge.connector_public_key
        )
        verify_record(challenge, connector_identity)
    except (ProtocolValidationError, TypeError, ValueError):
        raise ConnectorError(ConnectorErrorCode.CONNECTOR_STATE_INVALID) from None
    if (
        challenge.pairing_id != pairing_init.pairing_id
        or challenge.vps_key_id != profile.vps_key_id
        or challenge.init_digest != record_digest(pairing_init)
        or challenge.tls_leaf_fingerprint != leaf_fingerprint
    ):
        raise ConnectorError(ConnectorErrorCode.CONNECTOR_STATE_INVALID)


def _validate_paired_profile(
    profile: PairedVpsProfile, *, now: int | None = None
) -> None:
    if not isinstance(profile, PairedVpsProfile):
        raise TypeError("The paired VPS profile type is invalid.")
    grant = profile.signed_grant
    complete = profile.pairing_complete
    try:
        verify_record(grant, profile.connector_identity)
        verify_record(complete, profile.connector_identity)
        profile.authority(now=now)
    except (ConnectorError, ProtocolValidationError, TypeError, ValueError):
        raise ConnectorError(ConnectorErrorCode.CONNECTOR_STATE_INVALID) from None
    if (
        grant.claims.issuer_key_id != profile.connector_identity.key_id
        or grant.claims.subject_key_id != profile.vps_key_id
        or complete.connector_key_id != profile.connector_identity.key_id
        or complete.vps_key_id != profile.vps_key_id
        or complete.signed_grant_digest != record_digest(grant)
    ):
        raise ConnectorError(ConnectorErrorCode.CONNECTOR_STATE_INVALID)


def _profile_mapping(profile: VpsProfile) -> dict[str, object]:
    if isinstance(profile, PendingVpsProfile):
        return {
            "endpoint": profile.endpoint.uri,
            "observed_tls_leaf_fingerprint": profile.observed_tls_leaf_fingerprint,
            "pairing_challenge": _optional_record_blob(profile.pairing_challenge),
            "pairing_init": _record_blob(profile.pairing_init),
            "state": "pending",
            "version": _PROFILE_VERSION,
            "vps_key_id": profile.vps_key_id,
        }
    return {
        "connector_ca_der": _encode_blob(profile.connector_ca_der),
        "connector_public_key": profile.connector_identity.wire_public_key,
        "endpoint": profile.endpoint.uri,
        "pairing_complete": _record_blob(profile.pairing_complete),
        "signed_grant": _record_blob(profile.signed_grant),
        "state": "paired",
        "version": _PROFILE_VERSION,
        "vps_key_id": profile.vps_key_id,
    }


def _parse_profile(payload: bytes) -> VpsProfile:
    mapping = _mapping(load_canonical_json(payload, max_bytes=_MAX_PROFILE_BYTES))
    state = _string(mapping, "state")
    if state == "pending":
        _closed(
            mapping,
            {
                "endpoint",
                "observed_tls_leaf_fingerprint",
                "pairing_challenge",
                "pairing_init",
                "state",
                "version",
                "vps_key_id",
            },
        )
        _require_version(mapping, _PROFILE_VERSION)
        pairing_init = _parse_record_blob(mapping.get("pairing_init"), PairingInit)
        challenge_value = mapping.get("pairing_challenge")
        challenge = (
            None
            if challenge_value is None
            else _parse_record_blob(challenge_value, PairingChallenge)
        )
        leaf_value = mapping.get("observed_tls_leaf_fingerprint")
        leaf = None if leaf_value is None else _string_value(leaf_value)
        profile = PendingVpsProfile(
            endpoint=WssEndpoint.parse(_string(mapping, "endpoint")),
            vps_key_id=_string(mapping, "vps_key_id"),
            pairing_init=pairing_init,
            pairing_challenge=challenge,
            observed_tls_leaf_fingerprint=leaf,
        )
        _validate_pending_profile(profile)
        return profile
    if state == "paired":
        _closed(
            mapping,
            {
                "connector_ca_der",
                "connector_public_key",
                "endpoint",
                "pairing_complete",
                "signed_grant",
                "state",
                "version",
                "vps_key_id",
            },
        )
        _require_version(mapping, _PROFILE_VERSION)
        paired_profile = PairedVpsProfile(
            endpoint=WssEndpoint.parse(_string(mapping, "endpoint")),
            vps_key_id=_string(mapping, "vps_key_id"),
            connector_identity=DevicePublicIdentity.from_wire(
                _string(mapping, "connector_public_key")
            ),
            connector_ca_der=_decode_blob(
                mapping.get("connector_ca_der"), maximum=MAX_FRAME_BYTES
            ),
            signed_grant=_parse_record_blob(mapping.get("signed_grant"), SignedGrant),
            pairing_complete=_parse_record_blob(
                mapping.get("pairing_complete"), PairingComplete
            ),
        )
        _validate_paired_profile(paired_profile)
        return paired_profile
    raise ConnectorError(ConnectorErrorCode.CONNECTOR_SCHEMA_INCOMPATIBLE)


def _snapshot_mapping(snapshot: ConnectorSnapshot) -> dict[str, object]:
    return {
        "cause_code": (
            None if snapshot.cause_code is None else snapshot.cause_code.value
        ),
        "connector_key_id": snapshot.connector_key_id,
        "grant_id": snapshot.grant_id,
        "grant_revision": snapshot.grant_revision,
        "observed_at": snapshot.observed_at,
        "scopes": [
            {"operation": operation, "source": source}
            for source, operation in snapshot.scopes
        ],
        "state": snapshot.state,
        "version": _SNAPSHOT_VERSION,
    }


def _parse_snapshot(payload: bytes) -> ConnectorSnapshot:
    mapping = _mapping(load_canonical_json(payload, max_bytes=_MAX_SNAPSHOT_BYTES))
    _closed(
        mapping,
        {
            "cause_code",
            "connector_key_id",
            "grant_id",
            "grant_revision",
            "observed_at",
            "scopes",
            "state",
            "version",
        },
    )
    _require_version(mapping, _SNAPSHOT_VERSION)
    state_value = _string(mapping, "state")
    if state_value not in {"authenticated", "disconnected", "unavailable"}:
        raise ValueError("invalid snapshot state")
    cause_value = mapping.get("cause_code")
    cause = (
        None if cause_value is None else ConnectorErrorCode(_string_value(cause_value))
    )
    scopes_value = mapping.get("scopes")
    if type(scopes_value) is not list:
        raise ValueError("invalid snapshot scopes")
    scopes: list[tuple[str, str]] = []
    for value in cast(list[object], scopes_value):
        scope = _mapping(value)
        _closed(scope, {"operation", "source"})
        scopes.append((_string(scope, "source"), _string(scope, "operation")))
    snapshot = ConnectorSnapshot(
        connector_key_id=_string(mapping, "connector_key_id"),
        grant_id=_string(mapping, "grant_id"),
        grant_revision=_integer(mapping, "grant_revision"),
        observed_at=_integer(mapping, "observed_at"),
        state=cast(SnapshotState, state_value),
        cause_code=cause,
        scopes=tuple(scopes),
    )
    _validate_snapshot(snapshot)
    return snapshot


def _validate_snapshot(snapshot: ConnectorSnapshot) -> None:
    if not isinstance(snapshot, ConnectorSnapshot):
        raise TypeError("The Connector snapshot type is invalid.")
    _require_timestamp(snapshot.observed_at)
    if not _is_canonical_base32(snapshot.connector_key_id, expected_bytes=20):
        raise ValueError("The Connector snapshot key identifier is invalid.")
    if not _is_canonical_base32(snapshot.grant_id, expected_bytes=16):
        raise ValueError("The Connector snapshot grant identifier is invalid.")
    if type(snapshot.grant_revision) is not int or snapshot.grant_revision <= 0:
        raise ValueError("The Connector snapshot grant revision is invalid.")
    if snapshot.state not in {"authenticated", "disconnected", "unavailable"}:
        raise ValueError("The Connector snapshot state is invalid.")
    if snapshot.cause_code is not None and not isinstance(
        snapshot.cause_code, ConnectorErrorCode
    ):
        raise ValueError("The Connector snapshot cause is invalid.")
    if snapshot.state == "authenticated" and snapshot.cause_code is not None:
        raise ValueError("An authenticated snapshot cannot have a cause code.")
    if snapshot.state != "authenticated" and snapshot.cause_code is None:
        raise ValueError("A failed Connector snapshot requires a cause code.")
    if not snapshot.scopes or snapshot.scopes != tuple(sorted(set(snapshot.scopes))):
        raise ValueError("The Connector snapshot scopes are invalid.")
    for source, operation in snapshot.scopes:
        _require_catalog_operation(source, operation)


def _require_catalog_operation(source: str, operation: str) -> None:
    if type(source) is not str or type(operation) is not str:
        raise ValueError("The Connector source-operation is invalid.")
    source_spec = get_source(source)
    operation_spec = (
        None if source_spec is None else get_operation(source_spec, operation)
    )
    if operation_spec is None or operation_spec.tool == "status":
        raise ValueError("The Connector source-operation is invalid.")


def _record_blob(
    record: PairingInit | PairingChallenge | PairingComplete | SignedGrant,
) -> str:
    return _encode_blob(encode_record(record))


def _optional_record_blob(
    record: PairingChallenge | None,
) -> str | None:
    return None if record is None else _record_blob(record)


def _parse_record_blob(value: object, record_type: type[_RecordT]) -> _RecordT:
    record = parse_record(_decode_blob(value, maximum=MAX_FRAME_BYTES))
    if not isinstance(record, record_type):
        raise ValueError("The persisted Connector record type is invalid.")
    return record


def _encode_blob(payload: bytes) -> str:
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _decode_blob(value: object, *, maximum: int) -> bytes:
    encoded = _string_value(value)
    if not encoded or "=" in encoded or len(encoded) > 4 * maximum:
        raise ValueError("The persisted Connector payload is invalid.")
    try:
        decoded = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
    except (ValueError, binascii.Error):
        raise ValueError("The persisted Connector payload is invalid.") from None
    if not decoded or len(decoded) > maximum or _encode_blob(decoded) != encoded:
        raise ValueError("The persisted Connector payload is invalid.")
    return decoded


def _is_canonical_base32(value: object, *, expected_bytes: int) -> bool:
    expected_length = ID_BASE32_LENGTH if expected_bytes == 16 else KEY_ID_BASE32_LENGTH
    if type(value) is not str or len(value) != expected_length or "=" in value:
        return False
    try:
        decoded = base64.b32decode(
            value.upper() + "=" * (-len(value) % 8), casefold=False
        )
    except (ValueError, binascii.Error):
        return False
    return (
        len(decoded) == expected_bytes
        and base64.b32encode(decoded).decode("ascii").rstrip("=").lower() == value
    )


def _mapping(value: object) -> dict[str, object]:
    if type(value) is not dict:
        raise ValueError("The persisted Connector object is invalid.")
    mapping = cast(dict[object, object], value)
    if not all(type(key) is str for key in mapping):
        raise ValueError("The persisted Connector object is invalid.")
    return cast(dict[str, object], mapping)


def _closed(mapping: Mapping[str, object], fields: set[str]) -> None:
    if set(mapping) != fields:
        raise ValueError("The persisted Connector schema is invalid.")


def _string(mapping: Mapping[str, object], field: str) -> str:
    return _string_value(mapping.get(field))


def _string_value(value: object) -> str:
    if type(value) is not str:
        raise ValueError("The persisted Connector string is invalid.")
    return value


def _integer(mapping: Mapping[str, object], field: str) -> int:
    value = mapping.get(field)
    if type(value) is not int:
        raise ValueError("The persisted Connector integer is invalid.")
    return value


def _require_version(mapping: Mapping[str, object], expected: int) -> None:
    version = _integer(mapping, "version")
    if version != expected:
        raise ConnectorError(ConnectorErrorCode.CONNECTOR_SCHEMA_INCOMPATIBLE)


def _require_timestamp(value: object) -> None:
    if type(value) is not int or not 0 <= value <= MAX_TIMESTAMP_SECONDS:
        raise ValueError("The Connector timestamp is invalid.")


def _clock_value(clock: Callable[[], int]) -> int:
    value = clock()
    _require_timestamp(value)
    return value


def _random_protocol_id() -> str:
    return (
        base64.b32encode(secrets.token_bytes(ID_ENTROPY_BYTES))
        .decode("ascii")
        .rstrip("=")
        .lower()
    )


def _wall_timestamp() -> int:
    return int(time.time())


def _read_owner_file(
    state_directory: Path, filename: str, *, maximum: int
) -> bytes | None:
    directory_descriptor = -1
    descriptor = -1
    try:
        directory_descriptor = _open_state_directory(state_directory, create=False)
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        raise ConnectorError(ConnectorErrorCode.CONNECTOR_STATE_INVALID) from None
    try:
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
        try:
            descriptor = os.open(filename, flags, dir_fd=directory_descriptor)
        except FileNotFoundError:
            return None
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_nlink != 1
            or not 0 < metadata.st_size <= maximum
        ):
            raise OSError("unsafe Connector state file")
        chunks: list[bytes] = []
        remaining = metadata.st_size
        while remaining:
            chunk = os.read(descriptor, min(remaining, 4096))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        if len(payload) != metadata.st_size:
            raise OSError("short Connector state read")
        return payload
    except (OSError, ValueError):
        raise ConnectorError(ConnectorErrorCode.CONNECTOR_STATE_INVALID) from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if directory_descriptor >= 0:
            os.close(directory_descriptor)


def _atomic_write_owner_file(
    state_directory: Path,
    filename: str,
    payload: bytes,
    *,
    maximum: int,
) -> None:
    if not payload or len(payload) > maximum:
        raise ConnectorError(ConnectorErrorCode.CONNECTOR_STATE_INVALID)
    directory_descriptor = -1
    descriptor = -1
    temporary_name = f".{filename}.{secrets.token_hex(12)}.tmp"
    try:
        directory_descriptor = _open_state_directory(state_directory, create=True)
        _validate_existing_owner_file(directory_descriptor, filename)
        descriptor = os.open(
            temporary_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
            dir_fd=directory_descriptor,
        )
        os.fchmod(descriptor, 0o600)
        offset = 0
        while offset < len(payload):
            written = os.write(descriptor, payload[offset:])
            if written <= 0:
                raise OSError("Connector state write failed")
            offset += written
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.replace(
            temporary_name,
            filename,
            src_dir_fd=directory_descriptor,
            dst_dir_fd=directory_descriptor,
        )
        os.fsync(directory_descriptor)
    except ConnectorError:
        raise
    except (OSError, ValueError):
        try:
            if directory_descriptor >= 0:
                os.unlink(temporary_name, dir_fd=directory_descriptor)
        except OSError:
            pass
        raise ConnectorError(ConnectorErrorCode.CONNECTOR_STATE_INVALID) from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if directory_descriptor >= 0:
            os.close(directory_descriptor)


def _validate_existing_owner_file(directory_descriptor: int, filename: str) -> None:
    descriptor = -1
    try:
        try:
            descriptor = os.open(
                filename,
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                dir_fd=directory_descriptor,
            )
        except FileNotFoundError:
            return
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_nlink != 1
        ):
            raise OSError("unsafe existing Connector state file")
    finally:
        if descriptor >= 0:
            os.close(descriptor)
