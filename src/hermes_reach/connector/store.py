"""Transactional SQLite authority state for the trusted Connector."""

from __future__ import annotations

import base64
import binascii
import fcntl
import os
import re
import sqlite3
import stat
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal, Self, cast

from .errors import ConnectorError, ConnectorErrorCode
from .identity import DevicePublicIdentity, _open_state_directory
from .limits import (
    AUDIT_RETENTION_SECONDS,
    CONNECTOR_STORAGE_SCHEMA_VERSION,
    MAX_CLOCK_SKEW_SECONDS,
    SUPPORTED_CONNECTOR_PLATFORMS,
)
from .protocol import (
    GrantScope,
    PairingChallenge,
    PairingInit,
    ProtocolValidationError,
    SignedGrant,
    SignedRequest,
    encode_record,
    pairing_transcript_hash,
    parse_record,
    record_digest,
    verify_pairing_challenge,
    verify_pairing_init,
    verify_record,
    verify_signed_grant,
)

_DATABASE_FILE: Final = "connector-authority.sqlite3"
_WRITER_LOCK_FILE: Final = "connector-authority.lock"
_APPLICATION_ID: Final = 0x48524348
_BUSY_TIMEOUT_MS: Final = 5_000
_HEX_64: Final = re.compile(r"[0-9a-f]{64}")
_ID_LENGTH: Final = 26

ClaimDecision = Literal["allow", "deny"]
PairingDecision = Literal["pending", "approved", "denied", "expired"]


@dataclass(frozen=True, slots=True)
class ClaimResult:
    """The durable result of one request claim transaction."""

    accepted: bool
    cause_code: ConnectorErrorCode | None
    use_sequence: int | None
    remaining_uses: int | None


@dataclass(frozen=True, slots=True)
class DeviceInspection:
    """Public-safe paired-device state."""

    device_id: str
    label: str
    key_id: str
    fingerprint: str
    paired_at: int
    revoked_at: int | None


@dataclass(frozen=True, slots=True)
class GrantScopeInspection:
    source: str
    operation: str
    data_scope: str


@dataclass(frozen=True, slots=True)
class GrantInspection:
    """Public-safe immutable grant revision and its local counter."""

    grant_id: str
    revision: int
    device_id: str
    subject_key_id: str
    policy_revision: int
    issued_at: int
    not_before: int
    expires_at: int
    max_uses: int
    used_count: int
    revoked_at: int | None
    superseded_at: int | None
    scopes: tuple[GrantScopeInspection, ...]


@dataclass(frozen=True, slots=True)
class CleanupResult:
    expired_pairings: int
    removed_pairings: int
    removed_request_claims: int


@dataclass(frozen=True, slots=True)
class PairingState:
    """Durable pairing state recoverable only by its exact signed init record."""

    init: PairingInit
    challenge: PairingChallenge
    decision: PairingDecision
    decided_at: int | None
    transcript_digest: str | None
    signed_grant: SignedGrant | None


class StoreWriterLease:
    """One persistent-inode process lock for all authority mutations."""

    def __init__(
        self,
        state_directory: Path,
        *,
        create: bool = True,
        _platform: str | None = None,
    ) -> None:
        platform = sys.platform if _platform is None else _platform
        if type(create) is not bool:
            raise TypeError("The authority writer lease creation mode is invalid.")
        if platform not in SUPPORTED_CONNECTOR_PLATFORMS:
            raise ConnectorError(ConnectorErrorCode.UNSUPPORTED_PLATFORM)
        descriptor = -1
        directory_descriptor = -1
        try:
            directory_descriptor = _open_state_directory(state_directory, create=create)
            descriptor = _open_owner_file(
                directory_descriptor,
                _WRITER_LOCK_FILE,
                create=create,
            )
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ConnectorError(
                    ConnectorErrorCode.CONNECTOR_SERVICE_RUNNING
                ) from None
        except ConnectorError:
            if descriptor >= 0:
                os.close(descriptor)
            raise
        except (OSError, ValueError):
            if descriptor >= 0:
                os.close(descriptor)
            raise ConnectorError(ConnectorErrorCode.CONNECTOR_STATE_INVALID) from None
        finally:
            if directory_descriptor >= 0:
                os.close(directory_descriptor)
        self._state_directory = state_directory
        self._descriptor = descriptor
        metadata = os.fstat(descriptor)
        self._device = metadata.st_dev
        self._inode = metadata.st_ino
        self._owner_pid = os.getpid()
        self._closed = False

    def close(self) -> None:
        """Release the inode lock; the lock file intentionally remains."""

        if getattr(self, "_closed", True):
            return
        self._closed = True
        os.close(self._descriptor)

    def _assert_active_for(self, state_directory: Path) -> None:
        if (
            type(self) is not StoreWriterLease
            or self._closed
            or self._owner_pid != os.getpid()
            or self._state_directory != state_directory
        ):
            raise ConnectorError(ConnectorErrorCode.CONNECTOR_STATE_INVALID)
        try:
            _validate_open_file(self._descriptor)
            directory_descriptor = _open_state_directory(
                self._state_directory, create=False
            )
            try:
                named_descriptor = _open_owner_file(
                    directory_descriptor, _WRITER_LOCK_FILE, create=False
                )
                try:
                    metadata = os.fstat(named_descriptor)
                    if (
                        metadata.st_dev != self._device
                        or metadata.st_ino != self._inode
                    ):
                        raise OSError("authority writer lock inode changed")
                finally:
                    os.close(named_descriptor)
            finally:
                os.close(directory_descriptor)
        except OSError:
            raise ConnectorError(ConnectorErrorCode.CONNECTOR_STATE_INVALID) from None

    def __enter__(self) -> Self:
        self._assert_active_for(self._state_directory)
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def __del__(self) -> None:
        self.close()


class AuthorityStore:
    """Persistent grant, replay, quota, revocation, and policy authority."""

    def __init__(
        self,
        state_directory: Path,
        connector_identity: DevicePublicIdentity,
        writer_lease: StoreWriterLease,
    ) -> None:
        if not isinstance(connector_identity, DevicePublicIdentity):
            raise TypeError("AuthorityStore requires a Connector public identity.")
        writer_lease._assert_active_for(state_directory)
        self._state_directory = state_directory
        self._database_path = state_directory / _DATABASE_FILE
        self._connector_identity = connector_identity
        self._writer_lease = writer_lease

    @classmethod
    def initialize(
        cls,
        state_directory: Path,
        connector_identity: DevicePublicIdentity,
        writer_lease: StoreWriterLease,
        *,
        initial_policy_digest: str,
        now: int,
    ) -> Self:
        """Create schema v1 exactly once beneath an owner-only directory."""

        _require_digest(initial_policy_digest)
        _require_timestamp(now)
        writer_lease._assert_active_for(state_directory)
        directory_descriptor = -1
        database_descriptor = -1
        try:
            directory_descriptor = _open_state_directory(state_directory, create=False)
            database_descriptor = _create_owner_file(
                directory_descriptor, _DATABASE_FILE
            )
        except (FileExistsError, OSError, ValueError):
            raise ConnectorError(ConnectorErrorCode.CONNECTOR_STATE_INVALID) from None
        finally:
            if database_descriptor >= 0:
                os.close(database_descriptor)
            if directory_descriptor >= 0:
                os.close(directory_descriptor)

        store = cls(state_directory, connector_identity, writer_lease)
        try:
            with store._connection(validate_schema=False) as connection:
                connection.execute("BEGIN EXCLUSIVE")
                try:
                    for statement in _SCHEMA_STATEMENTS:
                        connection.execute(statement)
                    connection.execute(
                        "INSERT INTO schema_meta "
                        "(singleton, schema_version, connector_key_id, "
                        "migration_state, migrated_at) VALUES (1, ?, ?, 'ready', ?)",
                        (
                            CONNECTOR_STORAGE_SCHEMA_VERSION,
                            connector_identity.key_id,
                            now,
                        ),
                    )
                    connection.execute(
                        "INSERT INTO policy_revisions "
                        "(revision, policy_digest, created_at, replaced_at) "
                        "VALUES (1, ?, ?, NULL)",
                        (initial_policy_digest, now),
                    )
                    connection.execute(f"PRAGMA application_id={_APPLICATION_ID}")
                    connection.execute(
                        f"PRAGMA user_version={CONNECTOR_STORAGE_SCHEMA_VERSION}"
                    )
                    connection.execute("COMMIT")
                except BaseException:
                    connection.execute("ROLLBACK")
                    raise
        except ConnectorError:
            raise
        except sqlite3.Error:
            raise ConnectorError(ConnectorErrorCode.CONNECTOR_STATE_INVALID) from None
        return cls.open(state_directory, connector_identity, writer_lease)

    @classmethod
    def open(
        cls,
        state_directory: Path,
        connector_identity: DevicePublicIdentity,
        writer_lease: StoreWriterLease,
    ) -> Self:
        """Open only the exact schema after a read-only compatibility check."""

        writer_lease._assert_active_for(state_directory)
        database_path = state_directory / _DATABASE_FILE
        _validate_state_files(state_directory)
        _check_schema_compatibility(database_path, connector_identity.key_id)
        store = cls(state_directory, connector_identity, writer_lease)
        with store._connection() as connection:
            store._validate_stored_state(connection)
        return store

    @property
    def connector_identity(self) -> DevicePublicIdentity:
        """Return the immutable Connector identity bound to this database."""

        return self._connector_identity

    def active_device_identity(self, key_id: str) -> DevicePublicIdentity | None:
        """Resolve only a currently active paired key for signature prevalidation."""

        try:
            with self._connection() as connection:
                row = connection.execute(
                    "SELECT public_key FROM device_keys "
                    "WHERE key_id=? AND retired_at IS NULL",
                    (key_id,),
                ).fetchone()
                if row is None:
                    return None
                return _stored_public_identity(key_id, _text_value(row["public_key"]))
        except ConnectorError:
            raise
        except sqlite3.Error:
            raise ConnectorError(ConnectorErrorCode.CONNECTOR_STATE_INVALID) from None

    def begin_pairing(
        self, record: PairingInit, challenge: PairingChallenge, *, now: int
    ) -> None:
        """Persist an exact challenge before exposing a pending pairing."""

        identity = verify_pairing_init(record, now=now)
        init_payload = encode_record(record)
        init_digest = record_digest(record)
        challenge_payload = encode_record(challenge)
        try:
            connector_identity = verify_pairing_challenge(
                challenge,
                expected_pairing_id=record.pairing_id,
                expected_vps_key_id=record.vps_key_id,
                expected_init_digest=init_digest,
                observed_tls_leaf_fingerprint=challenge.tls_leaf_fingerprint,
                now=now,
            )
        except ProtocolValidationError:
            raise ConnectorError(ConnectorErrorCode.CONNECTOR_STATE_INVALID) from None
        if connector_identity != self._connector_identity:
            raise ConnectorError(ConnectorErrorCode.CONNECTOR_STATE_INVALID)
        try:
            with self._connection() as connection, _transaction(connection):
                connection.execute(
                    "UPDATE pairings SET decision='expired', decided_at=? "
                    "WHERE decision='pending' AND deadline<=?",
                    (now, now),
                )
                pending = connection.execute(
                    "SELECT COUNT(*) FROM pairings WHERE decision='pending'"
                ).fetchone()
                if pending is None or _integer_value(pending[0]) >= 3:
                    raise ConnectorError(ConnectorErrorCode.CONNECTOR_STATE_INVALID)
                connection.execute(
                    "INSERT INTO pairings "
                    "(pairing_id, vps_key_id, vps_public_key, init_record, "
                    "init_digest, challenge_record, transcript_digest, deadline, "
                    "decision, decided_at, device_id, grant_id, grant_revision) "
                    "VALUES (?, ?, ?, ?, ?, ?, NULL, ?, 'pending', NULL, NULL, "
                    "NULL, NULL)",
                    (
                        record.pairing_id,
                        identity.key_id,
                        identity.wire_public_key,
                        init_payload,
                        init_digest,
                        challenge_payload,
                        record.deadline,
                    ),
                )
        except ConnectorError:
            raise
        except sqlite3.IntegrityError:
            raise ConnectorError(ConnectorErrorCode.REQUEST_REPLAYED) from None
        except sqlite3.Error:
            raise ConnectorError(ConnectorErrorCode.CONNECTOR_STATE_INVALID) from None

    def pairing_state(self, record: PairingInit, *, now: int) -> PairingState | None:
        """Return only state bound to the byte-exact signed pairing request."""

        verify_pairing_init(record, now=now)
        digest = record_digest(record)
        try:
            with self._connection() as connection, _transaction(connection):
                connection.execute(
                    "UPDATE pairings SET decision='expired', decided_at=? "
                    "WHERE decision='pending' AND deadline<=?",
                    (now, now),
                )
                row = connection.execute(
                    "SELECT * FROM pairings WHERE pairing_id=?", (record.pairing_id,)
                ).fetchone()
                if row is None:
                    return None
                state = self._pairing_state_from_row(connection, row)
                init = state.init
                if (
                    digest != _text_value(row["init_digest"])
                    or init != record
                    or init.vps_key_id != record.vps_key_id
                ):
                    raise ConnectorError(ConnectorErrorCode.REQUEST_REPLAYED)
                return state
        except ConnectorError:
            raise
        except (ProtocolValidationError, sqlite3.Error):
            raise ConnectorError(ConnectorErrorCode.CONNECTOR_STATE_INVALID) from None

    def pairing_state_by_id(self, pairing_id: str, *, now: int) -> PairingState | None:
        """Load one local operator-visible pairing without accepting remote input."""

        _require_id(pairing_id)
        _require_timestamp(now)
        try:
            with self._connection() as connection, _transaction(connection):
                connection.execute(
                    "UPDATE pairings SET decision='expired', decided_at=? "
                    "WHERE decision='pending' AND deadline<=?",
                    (now, now),
                )
                row = connection.execute(
                    "SELECT * FROM pairings WHERE pairing_id=?", (pairing_id,)
                ).fetchone()
                if row is None:
                    return None
                return self._pairing_state_from_row(connection, row)
        except ConnectorError:
            raise
        except (ProtocolValidationError, sqlite3.Error):
            raise ConnectorError(ConnectorErrorCode.CONNECTOR_STATE_INVALID) from None

    def pending_pairings(self, *, now: int) -> tuple[PairingState, ...]:
        """Load the bounded local approval queue in deterministic order."""

        _require_timestamp(now)
        try:
            with self._connection() as connection, _transaction(connection):
                connection.execute(
                    "UPDATE pairings SET decision='expired', decided_at=? "
                    "WHERE decision='pending' AND deadline<=?",
                    (now, now),
                )
                rows = connection.execute(
                    "SELECT * FROM pairings WHERE decision='pending' "
                    "ORDER BY pairing_id"
                ).fetchall()
                return tuple(
                    self._pairing_state_from_row(connection, row) for row in rows
                )
        except ConnectorError:
            raise
        except (ProtocolValidationError, sqlite3.Error):
            raise ConnectorError(ConnectorErrorCode.CONNECTOR_STATE_INVALID) from None

    def current_policy_revision(self) -> int:
        """Return the single live policy revision for an owner-approved grant."""

        try:
            with self._connection() as connection:
                return _current_policy_revision(connection)
        except ConnectorError:
            raise
        except sqlite3.Error:
            raise ConnectorError(ConnectorErrorCode.CONNECTOR_STATE_INVALID) from None

    def approve_pairing(
        self,
        pairing_id: str,
        *,
        device_id: str,
        transcript_digest: str,
        grant: SignedGrant,
        now: int,
    ) -> GrantInspection:
        """Atomically consume a pairing and create its device and first grant."""

        _require_id(pairing_id)
        _require_id(device_id)
        _require_digest(transcript_digest)
        _require_timestamp(now)
        expired = False
        inspection: GrantInspection | None = None
        try:
            with self._connection() as connection, _transaction(connection):
                row = connection.execute(
                    "SELECT * FROM pairings WHERE pairing_id=?",
                    (pairing_id,),
                ).fetchone()
                if row is None or _text_value(row["decision"]) != "pending":
                    raise ConnectorError(ConnectorErrorCode.REQUEST_REPLAYED)
                if now >= _integer_value(row["deadline"]):
                    connection.execute(
                        "UPDATE pairings SET decision='expired', decided_at=? "
                        "WHERE pairing_id=? AND decision='pending'",
                        (now, pairing_id),
                    )
                    expired = True
                else:
                    init = _stored_pairing_init(row)
                    challenge = _stored_pairing_challenge(row, self._connector_identity)
                    expected_transcript_digest = pairing_transcript_hash(
                        encode_record(init),
                        encode_record(challenge),
                        observed_tls_leaf_fingerprint=challenge.tls_leaf_fingerprint,
                    ).hex()
                    if transcript_digest != expected_transcript_digest:
                        raise ProtocolValidationError(
                            "The approved pairing transcript does not match."
                        )
                    verify_signed_grant(
                        grant,
                        pinned_connector=self._connector_identity,
                        expected_subject_key_id=init.vps_key_id,
                        now=now,
                    )
                    current_policy = _current_policy_revision(connection)
                    claims = grant.claims
                    if (
                        claims.revision != 1
                        or claims.policy_revision != current_policy
                        or claims.scopes != init.requested_scopes
                        or claims.expires_at != init.grant_expires_at
                        or claims.max_uses != init.grant_max_uses
                    ):
                        raise ProtocolValidationError(
                            "The initial grant does not match the approved pairing."
                        )
                    connection.execute(
                        "INSERT INTO devices "
                        "(device_id, label, paired_at, revoked_at) "
                        "VALUES (?, ?, ?, NULL)",
                        (device_id, init.device_label, now),
                    )
                    connection.execute(
                        "INSERT INTO device_keys "
                        "(device_id, sequence, key_id, public_key, activated_at, "
                        "retired_at) "
                        "VALUES (?, 0, ?, ?, ?, NULL)",
                        (device_id, init.vps_key_id, init.vps_public_key, now),
                    )
                    _insert_grant(connection, grant, device_id=device_id)
                    updated = connection.execute(
                        "UPDATE pairings SET decision='approved', decided_at=?, "
                        "transcript_digest=?, device_id=?, grant_id=?, "
                        "grant_revision=? WHERE pairing_id=? AND decision='pending'",
                        (
                            now,
                            transcript_digest,
                            device_id,
                            claims.grant_id,
                            claims.revision,
                            pairing_id,
                        ),
                    )
                    if updated.rowcount != 1:
                        raise ConnectorError(ConnectorErrorCode.REQUEST_REPLAYED)
                    inspection = self._grant_inspection(
                        connection, claims.grant_id, claims.revision
                    )
            if expired:
                raise ConnectorError(ConnectorErrorCode.GRANT_EXPIRED)
            if inspection is None:
                raise ConnectorError(ConnectorErrorCode.CONNECTOR_STATE_INVALID)
            return inspection
        except (ConnectorError, ProtocolValidationError):
            raise
        except sqlite3.Error:
            raise ConnectorError(ConnectorErrorCode.CONNECTOR_STATE_INVALID) from None

    def deny_pairing(self, pairing_id: str, *, now: int) -> None:
        _require_id(pairing_id)
        _require_timestamp(now)
        try:
            with self._connection() as connection, _transaction(connection):
                updated = connection.execute(
                    "UPDATE pairings SET decision='denied', decided_at=? "
                    "WHERE pairing_id=? AND decision='pending' AND deadline>?",
                    (now, pairing_id, now),
                )
                if updated.rowcount != 1:
                    raise ConnectorError(ConnectorErrorCode.REQUEST_REPLAYED)
        except ConnectorError:
            raise
        except sqlite3.Error:
            raise ConnectorError(ConnectorErrorCode.CONNECTOR_STATE_INVALID) from None

    def replace_grant(self, grant: SignedGrant, *, now: int) -> GrantInspection:
        """Supersede the current revision and start the approved revision at zero."""

        _require_timestamp(now)
        verify_signed_grant(
            grant,
            pinned_connector=self._connector_identity,
            expected_subject_key_id=grant.claims.subject_key_id,
            now=now,
        )
        try:
            with self._connection() as connection, _transaction(connection):
                claims = grant.claims
                lineage = connection.execute(
                    "SELECT gl.device_id, gl.revoked_at, "
                    "d.revoked_at AS device_revoked_at FROM grant_lineages AS gl "
                    "JOIN devices AS d USING (device_id) WHERE gl.grant_id=?",
                    (claims.grant_id,),
                ).fetchone()
                if lineage is None:
                    raise ConnectorError(ConnectorErrorCode.GRANT_REQUIRED)
                if lineage["revoked_at"] is not None:
                    raise ConnectorError(ConnectorErrorCode.GRANT_REVOKED)
                if lineage["device_revoked_at"] is not None:
                    raise ConnectorError(ConnectorErrorCode.DEVICE_REVOKED)
                current = connection.execute(
                    "SELECT * FROM grant_revisions "
                    "WHERE grant_id=? AND superseded_at IS NULL",
                    (claims.grant_id,),
                ).fetchone()
                if current is None:
                    raise ConnectorError(ConnectorErrorCode.CONNECTOR_STATE_INVALID)
                old_grant = self._load_stored_grant(connection, current)
                if (
                    claims.revision != old_grant.claims.revision + 1
                    or claims.subject_key_id != old_grant.claims.subject_key_id
                    or claims.policy_revision != _current_policy_revision(connection)
                ):
                    raise ProtocolValidationError(
                        "The replacement grant continuity is invalid."
                    )
                updated = connection.execute(
                    "UPDATE grant_revisions SET superseded_at=? "
                    "WHERE grant_id=? AND revision=? AND superseded_at IS NULL",
                    (now, claims.grant_id, old_grant.claims.revision),
                )
                if updated.rowcount != 1:
                    raise ConnectorError(ConnectorErrorCode.GRANT_SUPERSEDED)
                _insert_grant(
                    connection,
                    grant,
                    device_id=_text_value(lineage["device_id"]),
                    create_lineage=False,
                )
                return self._grant_inspection(
                    connection, claims.grant_id, claims.revision
                )
        except (ConnectorError, ProtocolValidationError):
            raise
        except sqlite3.Error:
            raise ConnectorError(ConnectorErrorCode.CONNECTOR_STATE_INVALID) from None

    def revoke_device(self, device_id: str, *, now: int) -> None:
        _require_id(device_id)
        self._revoke("devices", "device_id", device_id, now=now)

    def revoke_grant(self, grant_id: str, *, now: int) -> None:
        _require_id(grant_id)
        self._revoke("grant_lineages", "grant_id", grant_id, now=now)

    def advance_policy_revision(self, policy_digest: str, *, now: int) -> int:
        _require_digest(policy_digest)
        _require_timestamp(now)
        try:
            with self._connection() as connection, _transaction(connection):
                current = _current_policy_revision(connection)
                updated = connection.execute(
                    "UPDATE policy_revisions SET replaced_at=? "
                    "WHERE revision=? AND replaced_at IS NULL",
                    (now, current),
                )
                if updated.rowcount != 1:
                    raise ConnectorError(ConnectorErrorCode.CONNECTOR_STATE_INVALID)
                revision = current + 1
                connection.execute(
                    "INSERT INTO policy_revisions "
                    "(revision, policy_digest, created_at, replaced_at) "
                    "VALUES (?, ?, ?, NULL)",
                    (revision, policy_digest, now),
                )
                return revision
        except ConnectorError:
            raise
        except sqlite3.Error:
            raise ConnectorError(ConnectorErrorCode.CONNECTOR_STATE_INVALID) from None

    def claim(
        self,
        request: SignedRequest,
        *,
        required_scope: GrantScope,
        now: int,
    ) -> ClaimResult:
        """Claim a unique request and conditionally spend exactly one grant use."""

        if not isinstance(request, SignedRequest) or not isinstance(
            required_scope, GrantScope
        ):
            raise TypeError("Authority claims require signed request and scope values.")
        _require_timestamp(now)
        try:
            with self._connection() as connection, _transaction(connection):
                device = self._authenticated_device(connection, request)
                if device is None:
                    return ClaimResult(
                        False, ConnectorErrorCode.CONNECTOR_NOT_PAIRED, None, None
                    )
                replay = connection.execute(
                    "SELECT 1 FROM request_claims WHERE request_id=?",
                    (request.request_id,),
                ).fetchone()
                if replay is not None:
                    return ClaimResult(
                        False, ConnectorErrorCode.REQUEST_REPLAYED, None, None
                    )
                device_id = _text_value(device["device_id"])
                if device["revoked_at"] is not None:
                    return self._deny_claim(
                        connection,
                        request,
                        device_id,
                        ConnectorErrorCode.DEVICE_REVOKED,
                        now,
                    )
                if (
                    now + MAX_CLOCK_SKEW_SECONDS < request.issued_at
                    or now >= request.deadline
                ):
                    return self._deny_claim(
                        connection,
                        request,
                        device_id,
                        ConnectorErrorCode.CONNECTOR_DEADLINE_EXCEEDED,
                        now,
                    )
                lineage = connection.execute(
                    "SELECT device_id, revoked_at FROM grant_lineages WHERE grant_id=?",
                    (request.grant_id,),
                ).fetchone()
                if lineage is None or _text_value(lineage["device_id"]) != device_id:
                    return self._deny_claim(
                        connection,
                        request,
                        device_id,
                        ConnectorErrorCode.GRANT_REQUIRED,
                        now,
                    )
                if lineage["revoked_at"] is not None:
                    return self._deny_claim(
                        connection,
                        request,
                        device_id,
                        ConnectorErrorCode.GRANT_REVOKED,
                        now,
                    )
                current = connection.execute(
                    "SELECT * FROM grant_revisions "
                    "WHERE grant_id=? AND superseded_at IS NULL",
                    (request.grant_id,),
                ).fetchone()
                if current is None:
                    raise ConnectorError(ConnectorErrorCode.CONNECTOR_STATE_INVALID)
                if _integer_value(current["revision"]) != request.grant_revision:
                    return self._deny_claim(
                        connection,
                        request,
                        device_id,
                        ConnectorErrorCode.GRANT_SUPERSEDED,
                        now,
                    )
                grant = self._load_stored_grant(connection, current)
                claims = grant.claims
                if claims.subject_key_id != request.subject_key_id:
                    return self._deny_claim(
                        connection,
                        request,
                        device_id,
                        ConnectorErrorCode.GRANT_REQUIRED,
                        now,
                    )
                if now < claims.not_before or now >= claims.expires_at:
                    return self._deny_claim(
                        connection,
                        request,
                        device_id,
                        ConnectorErrorCode.GRANT_EXPIRED,
                        now,
                    )
                current_policy = _current_policy_revision(connection)
                if (
                    request.policy_revision != current_policy
                    or claims.policy_revision != current_policy
                ):
                    return self._deny_claim(
                        connection,
                        request,
                        device_id,
                        ConnectorErrorCode.POLICY_REVISION_STALE,
                        now,
                    )
                if (
                    request.source != required_scope.source
                    or request.operation != required_scope.operation
                    or required_scope not in claims.scopes
                ):
                    return self._deny_claim(
                        connection,
                        request,
                        device_id,
                        ConnectorErrorCode.GRANT_SCOPE_DENIED,
                        now,
                    )
                updated = connection.execute(
                    "UPDATE grant_revisions SET used_count=used_count+1 "
                    "WHERE grant_id=? AND revision=? AND superseded_at IS NULL "
                    "AND used_count<max_uses "
                    "AND EXISTS (SELECT 1 FROM grant_lineages "
                    "WHERE grant_id=? AND revoked_at IS NULL) "
                    "AND EXISTS (SELECT 1 FROM policy_revisions "
                    "WHERE revision=? AND replaced_at IS NULL) "
                    "AND EXISTS (SELECT 1 FROM grant_scopes WHERE grant_id=? "
                    "AND revision=? AND source=? AND operation=? "
                    "AND data_scope=? AND capability_id IS ?)",
                    (
                        request.grant_id,
                        request.grant_revision,
                        request.grant_id,
                        request.policy_revision,
                        request.grant_id,
                        request.grant_revision,
                        required_scope.source,
                        required_scope.operation,
                        required_scope.data_scope,
                        required_scope.capability_id,
                    ),
                )
                if updated.rowcount != 1:
                    return self._deny_claim(
                        connection,
                        request,
                        device_id,
                        ConnectorErrorCode.GRANT_LIMIT_EXHAUSTED,
                        now,
                    )
                counter = connection.execute(
                    "SELECT used_count, max_uses FROM grant_revisions "
                    "WHERE grant_id=? AND revision=?",
                    (request.grant_id, request.grant_revision),
                ).fetchone()
                if counter is None:
                    raise ConnectorError(ConnectorErrorCode.CONNECTOR_STATE_INVALID)
                used_count = _integer_value(counter["used_count"])
                max_uses = _integer_value(counter["max_uses"])
                connection.execute(
                    _INSERT_CLAIM,
                    _claim_values(
                        request,
                        device_id,
                        "allow",
                        None,
                        now,
                        used_count,
                        max_uses - used_count,
                    ),
                )
                return ClaimResult(True, None, used_count, max_uses - used_count)
        except (ConnectorError, ProtocolValidationError):
            raise
        except sqlite3.Error:
            raise ConnectorError(ConnectorErrorCode.CONNECTOR_STATE_INVALID) from None

    def deny_authenticated_request(
        self,
        request: SignedRequest,
        code: ConnectorErrorCode,
        *,
        now: int,
    ) -> ClaimResult:
        """Replay-record one pre-authority denial for a verified paired request."""

        if not isinstance(request, SignedRequest) or not isinstance(
            code, ConnectorErrorCode
        ):
            raise TypeError("Authenticated denial requires closed request values.")
        _require_timestamp(now)
        try:
            with self._connection() as connection, _transaction(connection):
                device = self._authenticated_device(connection, request)
                if device is None:
                    return ClaimResult(
                        False, ConnectorErrorCode.CONNECTOR_NOT_PAIRED, None, None
                    )
                replay = connection.execute(
                    "SELECT 1 FROM request_claims WHERE request_id=?",
                    (request.request_id,),
                ).fetchone()
                if replay is not None:
                    return ClaimResult(
                        False, ConnectorErrorCode.REQUEST_REPLAYED, None, None
                    )
                return self._deny_claim(
                    connection,
                    request,
                    _text_value(device["device_id"]),
                    code,
                    now,
                )
        except (ConnectorError, ProtocolValidationError):
            raise
        except sqlite3.Error:
            raise ConnectorError(ConnectorErrorCode.CONNECTOR_STATE_INVALID) from None

    def complete_claim(self, request_id: str, receipt_digest: str, *, now: int) -> None:
        """Attach only receipt evidence metadata; operation results are never stored."""

        _require_id(request_id)
        _require_digest(receipt_digest)
        _require_timestamp(now)
        try:
            with self._connection() as connection, _transaction(connection):
                updated = connection.execute(
                    "UPDATE request_claims SET completed_at=?, receipt_digest=? "
                    "WHERE request_id=? AND decision='allow' AND completed_at IS NULL "
                    "AND claimed_at<=?",
                    (now, receipt_digest, request_id, now),
                )
                if updated.rowcount != 1:
                    raise ConnectorError(ConnectorErrorCode.CONNECTOR_STATE_INVALID)
        except ConnectorError:
            raise
        except sqlite3.Error:
            raise ConnectorError(ConnectorErrorCode.CONNECTOR_STATE_INVALID) from None

    def inspect_devices(self) -> tuple[DeviceInspection, ...]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT d.device_id, d.label, d.paired_at, d.revoked_at, "
                "k.key_id, k.public_key FROM devices AS d "
                "JOIN device_keys AS k USING (device_id) "
                "WHERE k.retired_at IS NULL ORDER BY d.device_id"
            ).fetchall()
            result: list[DeviceInspection] = []
            for row in rows:
                public = _stored_public_identity(
                    _text_value(row["key_id"]), _text_value(row["public_key"])
                )
                result.append(
                    DeviceInspection(
                        device_id=_text_value(row["device_id"]),
                        label=_text_value(row["label"]),
                        key_id=public.key_id,
                        fingerprint=public.fingerprint,
                        paired_at=_integer_value(row["paired_at"]),
                        revoked_at=_optional_integer(row["revoked_at"]),
                    )
                )
            return tuple(result)

    def inspect_grants(self) -> tuple[GrantInspection, ...]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT grant_id, revision FROM grant_revisions "
                "ORDER BY grant_id, revision"
            ).fetchall()
            return tuple(
                self._grant_inspection(
                    connection,
                    _text_value(row["grant_id"]),
                    _integer_value(row["revision"]),
                )
                for row in rows
            )

    def cleanup(self, *, now: int) -> CleanupResult:
        _require_timestamp(now)
        cutoff = max(0, now - AUDIT_RETENTION_SECONDS)
        try:
            with self._connection() as connection, _transaction(connection):
                expired = connection.execute(
                    "UPDATE pairings SET decision='expired', decided_at=? "
                    "WHERE decision='pending' AND deadline<=?",
                    (now, now),
                ).rowcount
                removed_pairings = connection.execute(
                    "DELETE FROM pairings WHERE decision!='pending' AND decided_at<?",
                    (cutoff,),
                ).rowcount
                removed = connection.execute(
                    "DELETE FROM request_claims WHERE claimed_at<?",
                    (cutoff,),
                ).rowcount
                return CleanupResult(expired, removed_pairings, removed)
        except sqlite3.Error:
            raise ConnectorError(ConnectorErrorCode.CONNECTOR_STATE_INVALID) from None

    def _revoke(self, table: str, key: str, value: str, *, now: int) -> None:
        _require_timestamp(now)
        if (table, key) not in {
            ("devices", "device_id"),
            ("grant_lineages", "grant_id"),
        }:
            raise AssertionError("The revocation target is closed.")
        try:
            with self._connection() as connection, _transaction(connection):
                existing = connection.execute(
                    f"SELECT revoked_at FROM {table} WHERE {key}=?", (value,)
                ).fetchone()
                if existing is None:
                    raise ConnectorError(ConnectorErrorCode.CONNECTOR_STATE_INVALID)
                if existing["revoked_at"] is None:
                    connection.execute(
                        f"UPDATE {table} SET revoked_at=? WHERE {key}=?",
                        (now, value),
                    )
        except ConnectorError:
            raise
        except sqlite3.Error:
            raise ConnectorError(ConnectorErrorCode.CONNECTOR_STATE_INVALID) from None

    def _deny_claim(
        self,
        connection: sqlite3.Connection,
        request: SignedRequest,
        device_id: str,
        code: ConnectorErrorCode,
        now: int,
    ) -> ClaimResult:
        connection.execute(
            _INSERT_CLAIM,
            _claim_values(request, device_id, "deny", code.value, now, None, None),
        )
        return ClaimResult(False, code, None, None)

    def _authenticated_device(
        self, connection: sqlite3.Connection, request: SignedRequest
    ) -> sqlite3.Row | None:
        device = connection.execute(
            "SELECT d.device_id, d.revoked_at, k.public_key "
            "FROM device_keys AS k JOIN devices AS d USING (device_id) "
            "WHERE k.key_id=? AND k.retired_at IS NULL",
            (request.subject_key_id,),
        ).fetchone()
        if device is None:
            return None
        public_identity = _stored_public_identity(
            request.subject_key_id, _text_value(device["public_key"])
        )
        verify_record(request, public_identity)
        if request.audience_key_id != self._connector_identity.key_id:
            raise ProtocolValidationError("The signed request audience does not match.")
        return cast(sqlite3.Row, device)

    def _pairing_state_from_row(
        self, connection: sqlite3.Connection, row: sqlite3.Row
    ) -> PairingState:
        init = _stored_pairing_init(row)
        challenge = _stored_pairing_challenge(row, self._connector_identity)
        decision = cast(PairingDecision, _text_value(row["decision"]))
        if decision not in {"pending", "approved", "denied", "expired"}:
            raise ConnectorError(ConnectorErrorCode.CONNECTOR_STATE_INVALID)
        transcript_digest = _optional_text(row["transcript_digest"])
        decided_at = _optional_integer(row["decided_at"])
        signed_grant: SignedGrant | None = None
        if decision == "approved":
            grant_id = _text_value(row["grant_id"])
            grant_revision = _integer_value(row["grant_revision"])
            grant_row = connection.execute(
                "SELECT gr.*, gl.device_id FROM grant_revisions AS gr "
                "JOIN grant_lineages AS gl USING (grant_id) "
                "WHERE gr.grant_id=? AND gr.revision=?",
                (grant_id, grant_revision),
            ).fetchone()
            if grant_row is None or transcript_digest is None:
                raise ConnectorError(ConnectorErrorCode.CONNECTOR_STATE_INVALID)
            signed_grant = self._load_stored_grant(connection, grant_row)
        elif transcript_digest is not None:
            raise ConnectorError(ConnectorErrorCode.CONNECTOR_STATE_INVALID)
        if (decision == "pending") != (decided_at is None):
            raise ConnectorError(ConnectorErrorCode.CONNECTOR_STATE_INVALID)
        return PairingState(
            init,
            challenge,
            decision,
            decided_at,
            transcript_digest,
            signed_grant,
        )

    def _grant_inspection(
        self, connection: sqlite3.Connection, grant_id: str, revision: int
    ) -> GrantInspection:
        row = connection.execute(
            "SELECT gr.*, gl.device_id, gl.revoked_at FROM grant_revisions AS gr "
            "JOIN grant_lineages AS gl USING (grant_id) "
            "WHERE gr.grant_id=? AND gr.revision=?",
            (grant_id, revision),
        ).fetchone()
        if row is None:
            raise ConnectorError(ConnectorErrorCode.CONNECTOR_STATE_INVALID)
        grant = self._load_stored_grant(connection, row)
        claims = grant.claims
        return GrantInspection(
            grant_id=claims.grant_id,
            revision=claims.revision,
            device_id=_text_value(row["device_id"]),
            subject_key_id=claims.subject_key_id,
            policy_revision=claims.policy_revision,
            issued_at=claims.issued_at,
            not_before=claims.not_before,
            expires_at=claims.expires_at,
            max_uses=claims.max_uses,
            used_count=_integer_value(row["used_count"]),
            revoked_at=_optional_integer(row["revoked_at"]),
            superseded_at=_optional_integer(row["superseded_at"]),
            scopes=tuple(
                GrantScopeInspection(scope.source, scope.operation, scope.data_scope)
                for scope in claims.scopes
            ),
        )

    def _load_stored_grant(
        self, connection: sqlite3.Connection, row: sqlite3.Row
    ) -> SignedGrant:
        try:
            payload = _blob_value(row["signed_record"])
            parsed = parse_record(payload)
            if not isinstance(parsed, SignedGrant):
                raise ProtocolValidationError("Stored authority is not a signed grant.")
            verify_record(parsed, self._connector_identity)
            claims = parsed.claims
            projected = (
                _text_value(row["grant_id"]),
                _integer_value(row["revision"]),
                _text_value(row["record_digest"]),
                _text_value(row["issuer_key_id"]),
                _text_value(row["subject_key_id"]),
                _integer_value(row["issued_at"]),
                _integer_value(row["not_before"]),
                _integer_value(row["expires_at"]),
                _integer_value(row["policy_revision"]),
                _integer_value(row["max_uses"]),
            )
            expected = (
                claims.grant_id,
                claims.revision,
                record_digest(parsed),
                claims.issuer_key_id,
                claims.subject_key_id,
                claims.issued_at,
                claims.not_before,
                claims.expires_at,
                claims.policy_revision,
                claims.max_uses,
            )
            scope_rows = connection.execute(
                "SELECT source, operation, data_scope, capability_id "
                "FROM grant_scopes WHERE grant_id=? AND revision=? "
                "ORDER BY source, operation",
                (claims.grant_id, claims.revision),
            ).fetchall()
            stored_scopes = tuple(
                GrantScope(
                    source=_text_value(scope_row["source"]),
                    operation=_text_value(scope_row["operation"]),
                    data_scope=_data_scope(scope_row["data_scope"]),
                    capability_id=_optional_text(scope_row["capability_id"]),
                )
                for scope_row in scope_rows
            )
            if projected != expected or stored_scopes != claims.scopes:
                raise ProtocolValidationError("Stored grant projection drifted.")
            return parsed
        except (KeyError, ProtocolValidationError, TypeError, ValueError):
            raise ConnectorError(ConnectorErrorCode.CONNECTOR_STATE_INVALID) from None

    def _validate_stored_state(self, connection: sqlite3.Connection) -> None:
        check = connection.execute("PRAGMA quick_check").fetchone()
        if check is None or check[0] != "ok":
            raise ConnectorError(ConnectorErrorCode.CONNECTOR_STATE_INVALID)
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise ConnectorError(ConnectorErrorCode.CONNECTOR_STATE_INVALID)
        _current_policy_revision(connection)
        pairings = connection.execute("SELECT * FROM pairings").fetchall()
        for row in pairings:
            state = self._pairing_state_from_row(connection, row)
            if state.decision == "approved":
                if state.transcript_digest is None:
                    raise ConnectorError(ConnectorErrorCode.CONNECTOR_STATE_INVALID)
                expected_digest = pairing_transcript_hash(
                    encode_record(state.init),
                    encode_record(state.challenge),
                    observed_tls_leaf_fingerprint=state.challenge.tls_leaf_fingerprint,
                ).hex()
                if state.transcript_digest != expected_digest:
                    raise ConnectorError(ConnectorErrorCode.CONNECTOR_STATE_INVALID)
            elif state.transcript_digest is not None:
                raise ConnectorError(ConnectorErrorCode.CONNECTOR_STATE_INVALID)
        devices = connection.execute(
            "SELECT d.device_id, COUNT(k.sequence) AS key_count, "
            "SUM(CASE WHEN k.retired_at IS NULL THEN 1 ELSE 0 END) AS active_count "
            "FROM devices AS d LEFT JOIN device_keys AS k USING (device_id) "
            "GROUP BY d.device_id"
        ).fetchall()
        for row in devices:
            if (
                _integer_value(row["key_count"]) < 1
                or _integer_value(row["active_count"]) != 1
            ):
                raise ConnectorError(ConnectorErrorCode.CONNECTOR_STATE_INVALID)
        keys = connection.execute(
            "SELECT key_id, public_key FROM device_keys"
        ).fetchall()
        for row in keys:
            _stored_public_identity(
                _text_value(row["key_id"]), _text_value(row["public_key"])
            )
        lineages = connection.execute(
            "SELECT gl.grant_id, COUNT(gr.revision) AS revision_count, "
            "SUM(CASE WHEN gr.superseded_at IS NULL THEN 1 ELSE 0 END) "
            "AS current_count FROM grant_lineages AS gl "
            "LEFT JOIN grant_revisions AS gr USING (grant_id) GROUP BY gl.grant_id"
        ).fetchall()
        for row in lineages:
            if (
                _integer_value(row["revision_count"]) < 1
                or _integer_value(row["current_count"]) != 1
            ):
                raise ConnectorError(ConnectorErrorCode.CONNECTOR_STATE_INVALID)
        grants = connection.execute(
            "SELECT gr.*, gl.device_id FROM grant_revisions AS gr "
            "JOIN grant_lineages AS gl USING (grant_id)"
        ).fetchall()
        for row in grants:
            grant = self._load_stored_grant(connection, row)
            subject = connection.execute(
                "SELECT 1 FROM device_keys WHERE device_id=? AND key_id=?",
                (_text_value(row["device_id"]), grant.claims.subject_key_id),
            ).fetchone()
            if subject is None:
                raise ConnectorError(ConnectorErrorCode.CONNECTOR_STATE_INVALID)

    @contextmanager
    def _connection(
        self, *, validate_schema: bool = True
    ) -> Iterator[sqlite3.Connection]:
        self._writer_lease._assert_active_for(self._state_directory)
        connection: sqlite3.Connection | None = None
        try:
            _validate_state_files(self._state_directory)
            connection = sqlite3.connect(
                self._database_path,
                timeout=_BUSY_TIMEOUT_MS / 1000,
                isolation_level=None,
            )
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA trusted_schema=OFF")
            connection.execute("PRAGMA foreign_keys=ON")
            foreign_keys = connection.execute("PRAGMA foreign_keys").fetchone()
            journal = connection.execute("PRAGMA journal_mode=WAL").fetchone()
            connection.execute("PRAGMA synchronous=FULL")
            synchronous = connection.execute("PRAGMA synchronous").fetchone()
            connection.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
            if (
                foreign_keys is None
                or foreign_keys[0] != 1
                or journal is None
                or str(journal[0]).lower() != "wal"
                or synchronous is None
                or synchronous[0] != 2
            ):
                raise ConnectorError(ConnectorErrorCode.CONNECTOR_STATE_INVALID)
            _validate_state_files(self._state_directory)
            if validate_schema:
                _check_connection_schema(connection, self._connector_identity.key_id)
        except ConnectorError:
            if connection is not None:
                connection.close()
            raise
        except (OSError, sqlite3.Error, ValueError):
            if connection is not None:
                connection.close()
            raise ConnectorError(ConnectorErrorCode.CONNECTOR_STATE_INVALID) from None
        try:
            yield connection
        finally:
            connection.close()


@contextmanager
def _transaction(connection: sqlite3.Connection) -> Iterator[None]:
    connection.execute("BEGIN IMMEDIATE")
    try:
        yield
        connection.execute("COMMIT")
    except BaseException:
        if connection.in_transaction:
            try:
                connection.execute("ROLLBACK")
            except sqlite3.Error:
                pass
        raise


def _insert_grant(
    connection: sqlite3.Connection,
    grant: SignedGrant,
    *,
    device_id: str,
    create_lineage: bool = True,
) -> None:
    claims = grant.claims
    if create_lineage:
        connection.execute(
            "INSERT INTO grant_lineages "
            "(grant_id, device_id, created_at, revoked_at) VALUES (?, ?, ?, NULL)",
            (claims.grant_id, device_id, claims.issued_at),
        )
    connection.execute(
        "INSERT INTO grant_revisions "
        "(grant_id, revision, signed_record, record_digest, issuer_key_id, "
        "subject_key_id, issued_at, not_before, expires_at, policy_revision, "
        "max_uses, used_count, superseded_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, NULL)",
        (
            claims.grant_id,
            claims.revision,
            encode_record(grant),
            record_digest(grant),
            claims.issuer_key_id,
            claims.subject_key_id,
            claims.issued_at,
            claims.not_before,
            claims.expires_at,
            claims.policy_revision,
            claims.max_uses,
        ),
    )
    connection.executemany(
        "INSERT INTO grant_scopes "
        "(grant_id, revision, source, operation, data_scope, capability_id) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            (
                claims.grant_id,
                claims.revision,
                scope.source,
                scope.operation,
                scope.data_scope,
                scope.capability_id,
            )
            for scope in claims.scopes
        ),
    )


def _stored_pairing_init(row: sqlite3.Row) -> PairingInit:
    try:
        parsed = parse_record(_blob_value(row["init_record"]))
        if not isinstance(parsed, PairingInit):
            raise ProtocolValidationError("Stored pairing type is invalid.")
        if (
            record_digest(parsed) != _text_value(row["init_digest"])
            or parsed.pairing_id != _text_value(row["pairing_id"])
            or parsed.vps_key_id != _text_value(row["vps_key_id"])
            or parsed.vps_public_key != _text_value(row["vps_public_key"])
            or parsed.deadline != _integer_value(row["deadline"])
        ):
            raise ProtocolValidationError("Stored pairing projection drifted.")
        verify_record(parsed, DevicePublicIdentity.from_wire(parsed.vps_public_key))
        return parsed
    except (ProtocolValidationError, TypeError, ValueError):
        raise ConnectorError(ConnectorErrorCode.CONNECTOR_STATE_INVALID) from None


def _stored_pairing_challenge(
    row: sqlite3.Row, connector_identity: DevicePublicIdentity
) -> PairingChallenge:
    try:
        init = _stored_pairing_init(row)
        parsed = parse_record(_blob_value(row["challenge_record"]))
        if not isinstance(parsed, PairingChallenge):
            raise ProtocolValidationError("Stored pairing challenge type is invalid.")
        identity = verify_pairing_challenge(
            parsed,
            expected_pairing_id=init.pairing_id,
            expected_vps_key_id=init.vps_key_id,
            expected_init_digest=record_digest(init),
            observed_tls_leaf_fingerprint=parsed.tls_leaf_fingerprint,
            now=parsed.issued_at,
        )
        if identity != connector_identity or parsed.deadline != init.deadline:
            raise ProtocolValidationError("Stored pairing challenge drifted.")
        return parsed
    except (ProtocolValidationError, TypeError, ValueError):
        raise ConnectorError(ConnectorErrorCode.CONNECTOR_STATE_INVALID) from None


def _stored_public_identity(key_id: str, public_key: str) -> DevicePublicIdentity:
    try:
        identity = DevicePublicIdentity.from_wire(public_key)
    except ValueError:
        raise ConnectorError(ConnectorErrorCode.CONNECTOR_STATE_INVALID) from None
    if identity.key_id != key_id:
        raise ConnectorError(ConnectorErrorCode.CONNECTOR_STATE_INVALID)
    return identity


def _current_policy_revision(connection: sqlite3.Connection) -> int:
    rows = connection.execute(
        "SELECT revision FROM policy_revisions WHERE replaced_at IS NULL"
    ).fetchall()
    if len(rows) != 1:
        raise ConnectorError(ConnectorErrorCode.CONNECTOR_STATE_INVALID)
    return _integer_value(rows[0]["revision"])


def _claim_values(
    request: SignedRequest,
    device_id: str,
    decision: ClaimDecision,
    cause_code: str | None,
    claimed_at: int,
    use_sequence: int | None,
    remaining_uses: int | None,
) -> tuple[object, ...]:
    return (
        request.request_id,
        request.trace_id,
        device_id,
        request.subject_key_id,
        request.grant_id,
        request.grant_revision,
        request.policy_revision,
        request.source,
        request.operation,
        request.payload_digest,
        decision,
        cause_code,
        claimed_at,
        use_sequence,
        remaining_uses,
    )


_INSERT_CLAIM: Final = (
    "INSERT INTO request_claims "
    "(request_id, trace_id, device_id, subject_key_id, grant_id, grant_revision, "
    "policy_revision, source, operation, payload_digest, decision, cause_code, "
    "claimed_at, use_sequence, remaining_uses, completed_at, receipt_digest) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL)"
)


def _check_schema_compatibility(database_path: Path, connector_key_id: str) -> None:
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(
            f"{database_path.as_uri()}?mode=ro",
            uri=True,
            timeout=_BUSY_TIMEOUT_MS / 1000,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        application = connection.execute("PRAGMA application_id").fetchone()
        version = connection.execute("PRAGMA user_version").fetchone()
        if application is None or application[0] != _APPLICATION_ID:
            raise ConnectorError(ConnectorErrorCode.CONNECTOR_STATE_INVALID)
        if version is None or type(version[0]) is not int:
            raise ConnectorError(ConnectorErrorCode.CONNECTOR_STATE_INVALID)
        if version[0] > CONNECTOR_STORAGE_SCHEMA_VERSION:
            raise ConnectorError(ConnectorErrorCode.CONNECTOR_SCHEMA_INCOMPATIBLE)
        if version[0] != CONNECTOR_STORAGE_SCHEMA_VERSION:
            raise ConnectorError(ConnectorErrorCode.CONNECTOR_STATE_INVALID)
        _check_connection_schema(connection, connector_key_id)
    except ConnectorError:
        raise
    except (OSError, sqlite3.Error, ValueError):
        raise ConnectorError(ConnectorErrorCode.CONNECTOR_STATE_INVALID) from None
    finally:
        if connection is not None:
            connection.close()


def _check_connection_schema(
    connection: sqlite3.Connection, connector_key_id: str
) -> None:
    row = connection.execute(
        "SELECT schema_version, connector_key_id, migration_state "
        "FROM schema_meta WHERE singleton=1"
    ).fetchone()
    if row is None:
        raise ConnectorError(ConnectorErrorCode.CONNECTOR_STATE_INVALID)
    schema_version = _integer_value(row["schema_version"])
    if schema_version > CONNECTOR_STORAGE_SCHEMA_VERSION:
        raise ConnectorError(ConnectorErrorCode.CONNECTOR_SCHEMA_INCOMPATIBLE)
    if (
        schema_version != CONNECTOR_STORAGE_SCHEMA_VERSION
        or _text_value(row["connector_key_id"]) != connector_key_id
        or _text_value(row["migration_state"]) != "ready"
    ):
        raise ConnectorError(ConnectorErrorCode.CONNECTOR_STATE_INVALID)


def _create_owner_file(directory_descriptor: int, filename: str) -> int:
    flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
    descriptor = os.open(filename, flags, 0o600, dir_fd=directory_descriptor)
    os.fchmod(descriptor, 0o600)
    _validate_open_file(descriptor)
    return descriptor


def _open_owner_file(directory_descriptor: int, filename: str, *, create: bool) -> int:
    if create:
        try:
            return _create_owner_file(directory_descriptor, filename)
        except FileExistsError:
            pass
    flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
    descriptor = os.open(filename, flags, dir_fd=directory_descriptor)
    try:
        _validate_open_file(descriptor)
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _validate_open_file(descriptor: int) -> None:
    metadata = os.fstat(descriptor)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_nlink != 1
    ):
        raise OSError("unsafe authority state file")


def _validate_named_owner_file(state_directory: Path, filename: str) -> None:
    directory_descriptor = -1
    descriptor = -1
    try:
        directory_descriptor = _open_state_directory(state_directory, create=False)
        descriptor = _open_owner_file(directory_descriptor, filename, create=False)
    except (OSError, ValueError):
        raise ConnectorError(ConnectorErrorCode.CONNECTOR_STATE_INVALID) from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if directory_descriptor >= 0:
            os.close(directory_descriptor)


def _validate_state_files(state_directory: Path) -> None:
    _validate_named_owner_file(state_directory, _DATABASE_FILE)
    _validate_named_owner_file(state_directory, _WRITER_LOCK_FILE)
    for suffix in ("-wal", "-shm"):
        _validate_optional_named_owner_file(
            state_directory, f"{_DATABASE_FILE}{suffix}"
        )


def _validate_optional_named_owner_file(state_directory: Path, filename: str) -> None:
    directory_descriptor = -1
    descriptor = -1
    try:
        directory_descriptor = _open_state_directory(state_directory, create=False)
        try:
            descriptor = _open_owner_file(directory_descriptor, filename, create=False)
        except FileNotFoundError:
            return
    except (OSError, ValueError):
        raise ConnectorError(ConnectorErrorCode.CONNECTOR_STATE_INVALID) from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if directory_descriptor >= 0:
            os.close(directory_descriptor)


def _require_id(value: object) -> None:
    if type(value) is not str or len(value) != _ID_LENGTH:
        raise ProtocolValidationError("The authority identifier is invalid.")
    try:
        decoded = base64.b32decode(value.upper() + "======", casefold=False)
    except (ValueError, binascii.Error):
        raise ProtocolValidationError("The authority identifier is invalid.") from None
    if (
        len(decoded) != 16
        or base64.b32encode(decoded).decode("ascii").rstrip("=").lower() != value
    ):
        raise ProtocolValidationError("The authority identifier is invalid.")


def _require_digest(value: object) -> None:
    if type(value) is not str or _HEX_64.fullmatch(value) is None:
        raise ProtocolValidationError("The authority digest is invalid.")


def _require_timestamp(value: object) -> None:
    if type(value) is not int or not 0 <= value <= 253_402_300_799:
        raise ProtocolValidationError("The authority timestamp is invalid.")


def _text_value(value: object) -> str:
    if type(value) is not str:
        raise ConnectorError(ConnectorErrorCode.CONNECTOR_STATE_INVALID)
    return value


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    return _text_value(value)


def _integer_value(value: object) -> int:
    if type(value) is not int:
        raise ConnectorError(ConnectorErrorCode.CONNECTOR_STATE_INVALID)
    return value


def _optional_integer(value: object) -> int | None:
    if value is None:
        return None
    return _integer_value(value)


def _blob_value(value: object) -> bytes:
    if type(value) is not bytes:
        raise ConnectorError(ConnectorErrorCode.CONNECTOR_STATE_INVALID)
    return value


def _data_scope(value: object) -> Literal["public", "account_visible"]:
    if value == "public":
        return "public"
    if value == "account_visible":
        return "account_visible"
    raise ConnectorError(ConnectorErrorCode.CONNECTOR_STATE_INVALID)


# This is the unreleased v1 baseline, including durable pairing challenges.
# After the first release, any shape change must bump the version and migrate.
_SCHEMA_STATEMENTS: Final[tuple[str, ...]] = (
    "CREATE TABLE schema_meta (singleton INTEGER PRIMARY KEY CHECK(singleton=1), "
    "schema_version INTEGER NOT NULL, connector_key_id TEXT NOT NULL, "
    "migration_state TEXT NOT NULL CHECK(migration_state='ready'), "
    "migrated_at INTEGER NOT NULL)",
    "CREATE TABLE devices (device_id TEXT PRIMARY KEY, label TEXT NOT NULL, "
    "paired_at INTEGER NOT NULL, revoked_at INTEGER)",
    "CREATE TABLE device_keys (device_id TEXT NOT NULL REFERENCES devices(device_id), "
    "sequence INTEGER NOT NULL CHECK(sequence>=0), key_id TEXT NOT NULL UNIQUE, "
    "public_key TEXT NOT NULL UNIQUE, activated_at INTEGER NOT NULL, "
    "retired_at INTEGER, "
    "PRIMARY KEY(device_id, sequence))",
    "CREATE UNIQUE INDEX one_active_device_key ON device_keys(device_id) "
    "WHERE retired_at IS NULL",
    "CREATE TABLE pairings (pairing_id TEXT PRIMARY KEY, vps_key_id TEXT NOT NULL, "
    "vps_public_key TEXT NOT NULL, init_record BLOB NOT NULL, "
    "init_digest TEXT NOT NULL UNIQUE, challenge_record BLOB NOT NULL, "
    "transcript_digest TEXT, deadline INTEGER NOT NULL, decision TEXT NOT NULL "
    "CHECK(decision IN ('pending','approved','denied','expired')), decided_at INTEGER, "
    "device_id TEXT REFERENCES devices(device_id), grant_id TEXT, "
    "grant_revision INTEGER)",
    "CREATE UNIQUE INDEX one_pending_pairing_per_device ON pairings(vps_key_id) "
    "WHERE decision='pending'",
    "CREATE TABLE policy_revisions (revision INTEGER PRIMARY KEY CHECK(revision>0), "
    "policy_digest TEXT NOT NULL UNIQUE, created_at INTEGER NOT NULL, "
    "replaced_at INTEGER)",
    "CREATE UNIQUE INDEX one_current_policy ON policy_revisions((1)) "
    "WHERE replaced_at IS NULL",
    "CREATE TABLE grant_lineages (grant_id TEXT PRIMARY KEY, device_id TEXT NOT NULL "
    "REFERENCES devices(device_id), created_at INTEGER NOT NULL, revoked_at INTEGER)",
    "CREATE TABLE grant_revisions (grant_id TEXT NOT NULL "
    "REFERENCES grant_lineages(grant_id), "
    "revision INTEGER NOT NULL CHECK(revision>0), signed_record BLOB NOT NULL, "
    "record_digest TEXT NOT NULL UNIQUE, issuer_key_id TEXT NOT NULL, "
    "subject_key_id TEXT NOT NULL, issued_at INTEGER NOT NULL, "
    "not_before INTEGER NOT NULL, "
    "expires_at INTEGER NOT NULL, policy_revision INTEGER NOT NULL "
    "REFERENCES policy_revisions(revision), max_uses INTEGER NOT NULL "
    "CHECK(max_uses>0), "
    "used_count INTEGER NOT NULL DEFAULT 0 "
    "CHECK(used_count>=0 AND used_count<=max_uses), "
    "superseded_at INTEGER, PRIMARY KEY(grant_id, revision))",
    "CREATE UNIQUE INDEX one_current_grant_revision ON grant_revisions(grant_id) "
    "WHERE superseded_at IS NULL",
    "CREATE TABLE grant_scopes (grant_id TEXT NOT NULL, revision INTEGER NOT NULL, "
    "source TEXT NOT NULL, operation TEXT NOT NULL, data_scope TEXT NOT NULL "
    "CHECK(data_scope IN ('public','account_visible')), capability_id TEXT, "
    "PRIMARY KEY(grant_id, revision, source, operation), "
    "FOREIGN KEY(grant_id, revision) "
    "REFERENCES grant_revisions(grant_id, revision))",
    "CREATE TABLE request_claims (request_id TEXT PRIMARY KEY, trace_id TEXT NOT NULL, "
    "device_id TEXT NOT NULL REFERENCES devices(device_id), "
    "subject_key_id TEXT NOT NULL, grant_id TEXT NOT NULL, "
    "grant_revision INTEGER NOT NULL, policy_revision INTEGER NOT NULL, "
    "source TEXT NOT NULL, operation TEXT NOT NULL, payload_digest TEXT NOT NULL, "
    "decision TEXT NOT NULL CHECK(decision IN ('allow','deny')), cause_code TEXT, "
    "claimed_at INTEGER NOT NULL, use_sequence INTEGER, remaining_uses INTEGER, "
    "completed_at INTEGER, receipt_digest TEXT)",
    "CREATE TRIGGER device_revocation_monotonic BEFORE UPDATE OF revoked_at ON devices "
    "WHEN OLD.revoked_at IS NOT NULL AND NEW.revoked_at IS NOT OLD.revoked_at "
    "BEGIN SELECT RAISE(ABORT, 'device revocation is immutable'); END",
    "CREATE TRIGGER grant_revocation_monotonic BEFORE UPDATE OF revoked_at "
    "ON grant_lineages "
    "WHEN OLD.revoked_at IS NOT NULL AND NEW.revoked_at IS NOT OLD.revoked_at "
    "BEGIN SELECT RAISE(ABORT, 'grant revocation is immutable'); END",
    "CREATE TRIGGER policy_replacement_monotonic BEFORE UPDATE OF replaced_at "
    "ON policy_revisions WHEN OLD.replaced_at IS NOT NULL "
    "AND NEW.replaced_at IS NOT OLD.replaced_at "
    "BEGIN SELECT RAISE(ABORT, 'policy replacement is immutable'); END",
    "CREATE TRIGGER grant_revision_static BEFORE UPDATE ON grant_revisions WHEN "
    "NEW.signed_record IS NOT OLD.signed_record "
    "OR NEW.record_digest IS NOT OLD.record_digest "
    "OR NEW.issuer_key_id IS NOT OLD.issuer_key_id "
    "OR NEW.subject_key_id IS NOT OLD.subject_key_id "
    "OR NEW.issued_at IS NOT OLD.issued_at OR NEW.not_before IS NOT OLD.not_before "
    "OR NEW.expires_at IS NOT OLD.expires_at "
    "OR NEW.policy_revision IS NOT OLD.policy_revision "
    "OR NEW.max_uses IS NOT OLD.max_uses BEGIN SELECT RAISE(ABORT, "
    "'signed grant projection is immutable'); END",
    "CREATE TRIGGER grant_counter_monotonic BEFORE UPDATE OF used_count "
    "ON grant_revisions "
    "WHEN NEW.used_count<OLD.used_count OR NEW.used_count>OLD.used_count+1 "
    "BEGIN SELECT RAISE(ABORT, 'grant counter is monotonic'); END",
    "CREATE TRIGGER grant_supersession_monotonic BEFORE UPDATE OF superseded_at "
    "ON grant_revisions WHEN OLD.superseded_at IS NOT NULL "
    "AND NEW.superseded_at IS NOT OLD.superseded_at "
    "BEGIN SELECT RAISE(ABORT, 'grant supersession is immutable'); END",
    "CREATE TRIGGER grant_scope_no_update BEFORE UPDATE ON grant_scopes "
    "BEGIN SELECT RAISE(ABORT, 'grant scopes are immutable'); END",
    "CREATE TRIGGER grant_scope_no_delete BEFORE DELETE ON grant_scopes "
    "BEGIN SELECT RAISE(ABORT, 'grant scopes are immutable'); END",
)
