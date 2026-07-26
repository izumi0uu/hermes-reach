from __future__ import annotations

import base64
import hashlib
import os
import sqlite3
import stat
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import pytest

from hermes_reach.connector.errors import ConnectorError, ConnectorErrorCode
from hermes_reach.connector.identity import DevicePrivateIdentity
from hermes_reach.connector.limits import (
    AUDIT_RETENTION_SECONDS,
    CONNECTOR_STORAGE_SCHEMA_VERSION,
)
from hermes_reach.connector.protocol import (
    GrantClaims,
    GrantScope,
    PairingChallenge,
    PairingInit,
    ProtocolValidationError,
    SignedGrant,
    SignedRequest,
    create_pairing_challenge,
    create_pairing_init,
    create_signed_grant,
    create_signed_request,
    encode_record,
    pairing_transcript_hash,
    protect_operation_call,
    record_digest,
)
from hermes_reach.connector.store import AuthorityStore, StoreWriterLease
from hermes_reach.contracts import validate_read

NOW = 1_800_000_000
DATABASE_NAME = "connector-authority.sqlite3"
LOCK_NAME = "connector-authority.lock"
INITIAL_POLICY_DIGEST = hashlib.sha256(b"initial-policy").hexdigest()
CAPABILITY_ID = "aaaaaaaaaaaaaaaaaaaaaaaafi"
SCOPE = GrantScope("web", "read.url", "public", CAPABILITY_ID)
PROTECTED_OPERATION = protect_operation_call(
    validate_read(
        {
            "source": "web",
            "operation": "read.url",
            "target": {"url": "https://example.com/article"},
        }
    )
)


def _canonical_id(value: int) -> str:
    return base64.b32encode(value.to_bytes(16, "big")).decode().rstrip("=").lower()


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _identity(seed: int) -> DevicePrivateIdentity:
    return DevicePrivateIdentity._from_seed_for_testing(bytes([seed]) * 32)


def _pairing(
    vps: DevicePrivateIdentity,
    *,
    slot: int,
    max_uses: int,
    deadline: int = NOW + 300,
    expires_at: int = NOW + 3_600,
) -> PairingInit:
    return create_pairing_init(
        signer=vps,
        message_id=_canonical_id(1_000 + slot),
        pairing_id=_canonical_id(2_000 + slot),
        device_label=f"reach-vps-{slot}",
        endpoint_digest=_digest(f"endpoint-{slot}"),
        vps_nonce=slot.to_bytes(32, "big"),
        requested_scopes=(SCOPE,),
        grant_expires_at=expires_at,
        grant_max_uses=max_uses,
        issued_at=NOW,
        deadline=deadline,
    )


def _challenge(
    connector: DevicePrivateIdentity, pairing: PairingInit, *, slot: int
) -> PairingChallenge:
    return create_pairing_challenge(
        signer=connector,
        message_id=_canonical_id(30_000 + slot),
        pairing_id=pairing.pairing_id,
        init_digest=record_digest(pairing),
        vps_key_id=pairing.vps_key_id,
        connector_nonce=(30_000 + slot).to_bytes(32, "big"),
        tls_ca_der=f"test-ca-{slot}".encode("ascii"),
        tls_leaf_fingerprint=_digest(f"leaf-{slot}"),
        issued_at=NOW + 1,
        deadline=pairing.deadline,
    )


def _grant(
    connector: DevicePrivateIdentity,
    vps: DevicePrivateIdentity,
    *,
    slot: int,
    grant_id: str,
    revision: int,
    policy_revision: int,
    max_uses: int,
    issued_at: int,
    expires_at: int = NOW + 3_600,
) -> SignedGrant:
    return create_signed_grant(
        signer=connector,
        message_id=_canonical_id(3_000 + slot),
        claims=GrantClaims(
            grant_id=grant_id,
            revision=revision,
            issuer_key_id=connector.public_identity.key_id,
            subject_key_id=vps.public_identity.key_id,
            issued_at=issued_at,
            not_before=issued_at,
            expires_at=expires_at,
            policy_revision=policy_revision,
            max_uses=max_uses,
            scopes=(SCOPE,),
        ),
    )


def _request(
    connector: DevicePrivateIdentity,
    vps: DevicePrivateIdentity,
    *,
    slot: int,
    grant_id: str,
    revision: int = 1,
    policy_revision: int = 1,
    issued_at: int = NOW + 10,
) -> SignedRequest:
    return create_signed_request(
        signer=vps,
        message_id=_canonical_id(10_000 + slot),
        request_id=_canonical_id(20_000 + slot),
        trace_id=f"{slot:032x}",
        audience_key_id=connector.public_identity.key_id,
        grant_id=grant_id,
        grant_revision=revision,
        policy_revision=policy_revision,
        source=SCOPE.source,
        operation=SCOPE.operation,
        issued_at=issued_at,
        deadline=issued_at + 60,
        protected_payload=PROTECTED_OPERATION,
    )


def _assert_error(
    error: pytest.ExceptionInfo[ConnectorError], code: ConnectorErrorCode
) -> None:
    assert error.value.code == code.value


@dataclass
class StoreHarness:
    state_directory: Path
    connector: DevicePrivateIdentity
    vps: DevicePrivateIdentity
    lease: StoreWriterLease
    store: AuthorityStore


@pytest.fixture
def harness(tmp_path: Path) -> StoreHarness:
    state_directory = tmp_path / "connector-state"
    connector = _identity(1)
    vps = _identity(2)
    lease = StoreWriterLease(state_directory)
    store = AuthorityStore.initialize(
        state_directory,
        connector.public_identity,
        lease,
        initial_policy_digest=INITIAL_POLICY_DIGEST,
        now=NOW,
    )
    value = StoreHarness(state_directory, connector, vps, lease, store)
    try:
        yield value
    finally:
        lease.close()


def _approve_first_grant(
    harness: StoreHarness,
    *,
    slot: int = 1,
    max_uses: int = 5,
) -> tuple[str, SignedGrant]:
    pairing = _pairing(harness.vps, slot=slot, max_uses=max_uses)
    grant_id = _canonical_id(4_000 + slot)
    grant = _grant(
        harness.connector,
        harness.vps,
        slot=slot,
        grant_id=grant_id,
        revision=1,
        policy_revision=1,
        max_uses=max_uses,
        issued_at=NOW + 1,
    )
    challenge = _challenge(harness.connector, pairing, slot=slot)
    harness.store.begin_pairing(pairing, challenge, now=NOW)
    harness.store.approve_pairing(
        pairing.pairing_id,
        device_id=_canonical_id(5_000 + slot),
        transcript_digest=pairing_transcript_hash(
            encode_record(pairing),
            encode_record(challenge),
            observed_tls_leaf_fingerprint=challenge.tls_leaf_fingerprint,
        ).hex(),
        grant=grant,
        now=NOW + 2,
    )
    return grant_id, grant


def _database(state_directory: Path) -> Path:
    return state_directory / DATABASE_NAME


def _raw_connection(state_directory: Path) -> sqlite3.Connection:
    return sqlite3.connect(_database(state_directory), isolation_level=None)


def test_initialize_uses_owner_only_files_and_required_pragmas(
    harness: StoreHarness,
) -> None:
    assert stat.S_IMODE(harness.state_directory.stat().st_mode) == 0o700

    with harness.store._connection() as connection:
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert connection.execute("PRAGMA synchronous").fetchone()[0] == 2
        assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == 5_000
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
        schema_meta = connection.execute(
            "SELECT schema_version, migration_state FROM schema_meta WHERE singleton=1"
        ).fetchone()
        assert schema_meta is not None
        assert tuple(schema_meta) == (1, "ready")
        pairing_columns = {
            row["name"]: (row["type"], row["notnull"])
            for row in connection.execute("PRAGMA table_info(pairings)")
        }
        assert pairing_columns["challenge_record"] == ("BLOB", 1)
        for path in harness.state_directory.iterdir():
            metadata = path.stat(follow_symlinks=False)
            assert stat.S_ISREG(metadata.st_mode)
            assert stat.S_IMODE(metadata.st_mode) == 0o600
            assert metadata.st_nlink == 1

    with pytest.raises(ConnectorError) as error:
        StoreWriterLease(harness.state_directory)
    _assert_error(error, ConnectorErrorCode.CONNECTOR_SERVICE_RUNNING)


def test_writer_lease_rejects_a_second_process(harness: StoreHarness) -> None:
    program = "\n".join(
        (
            "import sys",
            "from pathlib import Path",
            "from hermes_reach.connector.errors import ConnectorError",
            "from hermes_reach.connector.store import StoreWriterLease",
            "try:",
            "    StoreWriterLease(Path(sys.argv[1]))",
            "except ConnectorError as error:",
            "    print(error.code)",
            "else:",
            "    print('unexpected-success')",
        )
    )
    result = subprocess.run(
        [sys.executable, "-c", program, str(harness.state_directory)],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.stdout.strip() == ConnectorErrorCode.CONNECTOR_SERVICE_RUNNING.value


def test_pairing_approval_atomically_creates_device_and_first_grant(
    harness: StoreHarness,
) -> None:
    grant_id, _ = _approve_first_grant(harness)

    devices = harness.store.inspect_devices()
    grants = harness.store.inspect_grants()
    assert len(devices) == 1
    assert devices[0].key_id == harness.vps.public_identity.key_id
    assert len(grants) == 1
    assert grants[0].grant_id == grant_id
    assert grants[0].revision == 1
    assert grants[0].used_count == 0
    assert grants[0].scopes[0].source == SCOPE.source
    assert not hasattr(grants[0].scopes[0], "capability_id")

    pairing = _pairing(harness.vps, slot=2, max_uses=3)
    colliding_grant = _grant(
        harness.connector,
        harness.vps,
        slot=2,
        grant_id=_canonical_id(4_002),
        revision=1,
        policy_revision=1,
        max_uses=3,
        issued_at=NOW + 1,
    )
    challenge = _challenge(harness.connector, pairing, slot=2)
    harness.store.begin_pairing(pairing, challenge, now=NOW)
    with pytest.raises(ConnectorError) as error:
        harness.store.approve_pairing(
            pairing.pairing_id,
            device_id=_canonical_id(5_002),
            transcript_digest=pairing_transcript_hash(
                encode_record(pairing),
                encode_record(challenge),
                observed_tls_leaf_fingerprint=challenge.tls_leaf_fingerprint,
            ).hex(),
            grant=colliding_grant,
            now=NOW + 2,
        )
    _assert_error(error, ConnectorErrorCode.CONNECTOR_STATE_INVALID)

    harness.store.deny_pairing(pairing.pairing_id, now=NOW + 3)
    assert len(harness.store.inspect_devices()) == 1
    assert len(harness.store.inspect_grants()) == 1


def test_pairing_state_persists_the_exact_challenge_and_verified_resolution(
    harness: StoreHarness,
) -> None:
    pairing = _pairing(harness.vps, slot=120, max_uses=2)
    challenge = _challenge(harness.connector, pairing, slot=120)
    grant = _grant(
        harness.connector,
        harness.vps,
        slot=120,
        grant_id=_canonical_id(4_120),
        revision=1,
        policy_revision=1,
        max_uses=2,
        issued_at=NOW + 1,
    )
    harness.store.begin_pairing(pairing, challenge, now=NOW)

    pending = harness.store.pairing_state(pairing, now=NOW + 2)
    assert pending is not None
    assert pending.decision == "pending"
    assert pending.challenge == challenge
    assert pending.signed_grant is None

    with pytest.raises(ProtocolValidationError):
        harness.store.approve_pairing(
            pairing.pairing_id,
            device_id=_canonical_id(5_120),
            transcript_digest=_digest("wrong-transcript"),
            grant=grant,
            now=NOW + 2,
        )
    assert harness.store.pairing_state(pairing, now=NOW + 2) == pending

    transcript_digest = pairing_transcript_hash(
        encode_record(pairing),
        encode_record(challenge),
        observed_tls_leaf_fingerprint=challenge.tls_leaf_fingerprint,
    ).hex()
    harness.store.approve_pairing(
        pairing.pairing_id,
        device_id=_canonical_id(5_120),
        transcript_digest=transcript_digest,
        grant=grant,
        now=NOW + 2,
    )
    approved = harness.store.pairing_state(pairing, now=NOW + 3)
    assert approved is not None
    assert approved.decision == "approved"
    assert approved.challenge == challenge
    assert approved.transcript_digest == transcript_digest
    assert approved.signed_grant == grant


def test_expired_pairing_decision_commits_before_rejection(
    harness: StoreHarness,
) -> None:
    pairing = _pairing(
        harness.vps,
        slot=3,
        max_uses=2,
        deadline=NOW + 10,
        expires_at=NOW + 1_000,
    )
    grant = _grant(
        harness.connector,
        harness.vps,
        slot=3,
        grant_id=_canonical_id(4_003),
        revision=1,
        policy_revision=1,
        max_uses=2,
        issued_at=NOW + 1,
        expires_at=NOW + 1_000,
    )
    challenge = _challenge(harness.connector, pairing, slot=3)
    harness.store.begin_pairing(pairing, challenge, now=NOW)

    with pytest.raises(ConnectorError) as expired:
        harness.store.approve_pairing(
            pairing.pairing_id,
            device_id=_canonical_id(5_003),
            transcript_digest=pairing_transcript_hash(
                encode_record(pairing),
                encode_record(challenge),
                observed_tls_leaf_fingerprint=challenge.tls_leaf_fingerprint,
            ).hex(),
            grant=grant,
            now=NOW + 10,
        )
    _assert_error(expired, ConnectorErrorCode.GRANT_EXPIRED)

    with pytest.raises(ConnectorError) as replay:
        harness.store.approve_pairing(
            pairing.pairing_id,
            device_id=_canonical_id(5_003),
            transcript_digest=pairing_transcript_hash(
                encode_record(pairing),
                encode_record(challenge),
                observed_tls_leaf_fingerprint=challenge.tls_leaf_fingerprint,
            ).hex(),
            grant=grant,
            now=NOW + 10,
        )
    _assert_error(replay, ConnectorErrorCode.REQUEST_REPLAYED)
    assert harness.store.inspect_devices() == ()


def test_concurrent_claims_never_overspend_grant(harness: StoreHarness) -> None:
    max_uses = 8
    grant_id, _ = _approve_first_grant(harness, max_uses=max_uses)
    requests = [
        _request(harness.connector, harness.vps, slot=slot, grant_id=grant_id)
        for slot in range(1, 25)
    ]

    with ThreadPoolExecutor(max_workers=12) as executor:
        results = list(
            executor.map(
                lambda request: harness.store.claim(
                    request, required_scope=SCOPE, now=NOW + 11
                ),
                requests,
            )
        )

    accepted = [result for result in results if result.accepted]
    denied = [result for result in results if not result.accepted]
    assert len(accepted) == max_uses
    assert sorted(result.use_sequence for result in accepted) == list(
        range(1, max_uses + 1)
    )
    assert {result.cause_code for result in denied} == {
        ConnectorErrorCode.GRANT_LIMIT_EXHAUSTED
    }
    assert harness.store.inspect_grants()[0].used_count == max_uses


def test_concurrent_duplicate_request_is_accepted_once(harness: StoreHarness) -> None:
    grant_id, _ = _approve_first_grant(harness, max_uses=10)
    request = _request(harness.connector, harness.vps, slot=40, grant_id=grant_id)

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(
            executor.map(
                lambda _: harness.store.claim(
                    request, required_scope=SCOPE, now=NOW + 11
                ),
                range(12),
            )
        )

    assert sum(result.accepted for result in results) == 1
    assert (
        sum(
            result.cause_code is ConnectorErrorCode.REQUEST_REPLAYED
            for result in results
        )
        == 11
    )
    assert harness.store.inspect_grants()[0].used_count == 1


def test_replay_and_revocation_survive_store_restart(harness: StoreHarness) -> None:
    grant_id, _ = _approve_first_grant(harness)
    request = _request(harness.connector, harness.vps, slot=50, grant_id=grant_id)
    assert harness.store.claim(request, required_scope=SCOPE, now=NOW + 11).accepted

    harness.lease.close()
    lease = StoreWriterLease(harness.state_directory)
    try:
        reopened = AuthorityStore.open(
            harness.state_directory, harness.connector.public_identity, lease
        )
        replay = reopened.claim(request, required_scope=SCOPE, now=NOW + 12)
        assert replay.cause_code is ConnectorErrorCode.REQUEST_REPLAYED
        reopened.revoke_grant(grant_id, now=NOW + 13)
    finally:
        lease.close()

    lease = StoreWriterLease(harness.state_directory)
    try:
        reopened = AuthorityStore.open(
            harness.state_directory, harness.connector.public_identity, lease
        )
        denied = reopened.claim(
            _request(harness.connector, harness.vps, slot=51, grant_id=grant_id),
            required_scope=SCOPE,
            now=NOW + 14,
        )
        assert denied.cause_code is ConnectorErrorCode.GRANT_REVOKED
        assert reopened.inspect_grants()[0].used_count == 1
    finally:
        lease.close()


def test_grant_replacement_is_atomic_and_starts_a_fresh_counter(
    harness: StoreHarness,
) -> None:
    grant_id, _ = _approve_first_grant(harness, max_uses=4)
    for slot in (60, 61):
        assert harness.store.claim(
            _request(harness.connector, harness.vps, slot=slot, grant_id=grant_id),
            required_scope=SCOPE,
            now=NOW + 11,
        ).accepted

    replacement = _grant(
        harness.connector,
        harness.vps,
        slot=62,
        grant_id=grant_id,
        revision=2,
        policy_revision=1,
        max_uses=2,
        issued_at=NOW + 20,
    )
    current = harness.store.replace_grant(replacement, now=NOW + 20)
    assert current.revision == 2
    assert current.used_count == 0

    revisions = harness.store.inspect_grants()
    assert [(row.revision, row.used_count) for row in revisions] == [(1, 2), (2, 0)]
    assert revisions[0].superseded_at == NOW + 20

    old = harness.store.claim(
        _request(
            harness.connector,
            harness.vps,
            slot=63,
            grant_id=grant_id,
            revision=1,
            issued_at=NOW + 21,
        ),
        required_scope=SCOPE,
        now=NOW + 21,
    )
    assert old.cause_code is ConnectorErrorCode.GRANT_SUPERSEDED

    fresh = harness.store.claim(
        _request(
            harness.connector,
            harness.vps,
            slot=64,
            grant_id=grant_id,
            revision=2,
            issued_at=NOW + 21,
        ),
        required_scope=SCOPE,
        now=NOW + 21,
    )
    assert fresh.accepted
    assert fresh.use_sequence == 1
    assert fresh.remaining_uses == 1


def test_stale_policy_denies_old_grant_and_replacement_requires_current_policy(
    harness: StoreHarness,
) -> None:
    grant_id, _ = _approve_first_grant(harness)
    assert harness.store.advance_policy_revision(_digest("policy-2"), now=NOW + 20) == 2

    stale = harness.store.claim(
        _request(harness.connector, harness.vps, slot=70, grant_id=grant_id),
        required_scope=SCOPE,
        now=NOW + 21,
    )
    assert stale.cause_code is ConnectorErrorCode.POLICY_REVISION_STALE

    old_policy_replacement = _grant(
        harness.connector,
        harness.vps,
        slot=71,
        grant_id=grant_id,
        revision=2,
        policy_revision=1,
        max_uses=5,
        issued_at=NOW + 21,
    )
    with pytest.raises(ProtocolValidationError):
        harness.store.replace_grant(old_policy_replacement, now=NOW + 21)

    replacement = _grant(
        harness.connector,
        harness.vps,
        slot=72,
        grant_id=grant_id,
        revision=2,
        policy_revision=2,
        max_uses=5,
        issued_at=NOW + 21,
    )
    harness.store.replace_grant(replacement, now=NOW + 21)
    accepted = harness.store.claim(
        _request(
            harness.connector,
            harness.vps,
            slot=73,
            grant_id=grant_id,
            revision=2,
            policy_revision=2,
            issued_at=NOW + 22,
        ),
        required_scope=SCOPE,
        now=NOW + 22,
    )
    assert accepted.accepted


def test_active_rotated_key_cannot_inherit_grant_bound_to_old_subject(
    harness: StoreHarness,
) -> None:
    grant_id, _ = _approve_first_grant(harness)
    rotated_vps = _identity(4)
    device_id = harness.store.inspect_devices()[0].device_id
    with _raw_connection(harness.state_directory) as connection:
        connection.execute(
            "UPDATE device_keys SET retired_at=? WHERE device_id=?",
            (NOW + 20, device_id),
        )
        connection.execute(
            "INSERT INTO device_keys "
            "(device_id, sequence, key_id, public_key, activated_at, retired_at) "
            "VALUES (?, 1, ?, ?, ?, NULL)",
            (
                device_id,
                rotated_vps.public_identity.key_id,
                rotated_vps.public_identity.wire_public_key,
                NOW + 20,
            ),
        )

    denied = harness.store.claim(
        _request(
            harness.connector,
            rotated_vps,
            slot=75,
            grant_id=grant_id,
            issued_at=NOW + 21,
        ),
        required_scope=SCOPE,
        now=NOW + 21,
    )
    assert denied.cause_code is ConnectorErrorCode.GRANT_REQUIRED
    assert harness.store.inspect_grants()[0].used_count == 0


def test_claim_insert_failure_rolls_back_usage_increment(harness: StoreHarness) -> None:
    grant_id, _ = _approve_first_grant(harness)
    with _raw_connection(harness.state_directory) as connection:
        connection.execute(
            "CREATE TRIGGER abort_request_claim BEFORE INSERT ON request_claims "
            "BEGIN SELECT RAISE(ABORT, 'injected claim failure'); END"
        )

    request = _request(harness.connector, harness.vps, slot=80, grant_id=grant_id)
    with pytest.raises(ConnectorError) as error:
        harness.store.claim(request, required_scope=SCOPE, now=NOW + 11)
    _assert_error(error, ConnectorErrorCode.CONNECTOR_STATE_INVALID)
    assert harness.store.inspect_grants()[0].used_count == 0

    with _raw_connection(harness.state_directory) as connection:
        connection.execute("DROP TRIGGER abort_request_claim")
    result = harness.store.claim(request, required_scope=SCOPE, now=NOW + 11)
    assert result.accepted
    assert result.use_sequence == 1


def test_committed_claim_can_remain_incomplete_but_never_reexecutes(
    harness: StoreHarness,
) -> None:
    grant_id, _ = _approve_first_grant(harness)
    request = _request(harness.connector, harness.vps, slot=90, grant_id=grant_id)
    assert harness.store.claim(request, required_scope=SCOPE, now=NOW + 11).accepted

    harness.lease.close()
    lease = StoreWriterLease(harness.state_directory)
    try:
        reopened = AuthorityStore.open(
            harness.state_directory, harness.connector.public_identity, lease
        )
        replay = reopened.claim(request, required_scope=SCOPE, now=NOW + 12)
        assert replay.cause_code is ConnectorErrorCode.REQUEST_REPLAYED
        with _raw_connection(harness.state_directory) as connection:
            columns = {
                row[1]
                for row in connection.execute("PRAGMA table_info(request_claims)")
            }
            row = connection.execute(
                "SELECT completed_at, receipt_digest FROM request_claims "
                "WHERE request_id=?",
                (request.request_id,),
            ).fetchone()
        assert "result" not in columns
        assert "result_payload" not in columns
        assert row == (None, None)
        assert reopened.inspect_grants()[0].used_count == 1
    finally:
        lease.close()


@pytest.mark.parametrize("corruption", ["signed_record", "projection"])
def test_signed_grant_or_database_projection_corruption_fails_closed(
    harness: StoreHarness,
    corruption: str,
) -> None:
    _approve_first_grant(harness)
    harness.lease.close()
    with _raw_connection(harness.state_directory) as connection:
        connection.execute("DROP TRIGGER grant_revision_static")
        if corruption == "signed_record":
            connection.execute("UPDATE grant_revisions SET signed_record=x'00'")
        else:
            connection.execute("UPDATE grant_revisions SET max_uses=max_uses+1")

    lease = StoreWriterLease(harness.state_directory)
    try:
        with pytest.raises(ConnectorError) as error:
            AuthorityStore.open(
                harness.state_directory, harness.connector.public_identity, lease
            )
        _assert_error(error, ConnectorErrorCode.CONNECTOR_STATE_INVALID)
    finally:
        lease.close()


def test_future_schema_probe_is_read_only_and_preserves_database(
    harness: StoreHarness,
) -> None:
    harness.lease.close()
    future = CONNECTOR_STORAGE_SCHEMA_VERSION + 1
    with _raw_connection(harness.state_directory) as connection:
        connection.execute(f"PRAGMA user_version={future}")
    database = _database(harness.state_directory)
    content_before = database.read_bytes()
    modified_before = database.stat().st_mtime_ns

    lease = StoreWriterLease(harness.state_directory)
    try:
        with pytest.raises(ConnectorError) as error:
            AuthorityStore.open(
                harness.state_directory, harness.connector.public_identity, lease
            )
        _assert_error(error, ConnectorErrorCode.CONNECTOR_SCHEMA_INCOMPATIBLE)
    finally:
        lease.close()

    assert database.read_bytes() == content_before
    assert database.stat().st_mtime_ns == modified_before


def test_unrelated_future_sqlite_database_is_not_misclassified_as_reach_schema(
    harness: StoreHarness,
) -> None:
    harness.lease.close()
    with _raw_connection(harness.state_directory) as connection:
        connection.execute("PRAGMA application_id=0")
        connection.execute(
            f"PRAGMA user_version={CONNECTOR_STORAGE_SCHEMA_VERSION + 1}"
        )

    lease = StoreWriterLease(harness.state_directory)
    try:
        with pytest.raises(ConnectorError) as error:
            AuthorityStore.open(
                harness.state_directory, harness.connector.public_identity, lease
            )
        _assert_error(error, ConnectorErrorCode.CONNECTOR_STATE_INVALID)
    finally:
        lease.close()


def _make_unsafe(path: Path, kind: str) -> None:
    if kind == "wrong-mode":
        path.chmod(0o640)
        return
    if kind == "hardlink":
        os.link(path, path.with_name(f"{path.name}.alias"))
        return
    target = path.with_name(f"{path.name}.target")
    path.rename(target)
    if kind == "symlink":
        path.symlink_to(target.name)
    elif kind == "fifo":
        os.mkfifo(path, 0o600)
    else:
        raise AssertionError("unknown unsafe-file fixture")


@pytest.mark.parametrize("filename", [DATABASE_NAME, LOCK_NAME])
@pytest.mark.parametrize("kind", ["symlink", "hardlink", "wrong-mode", "fifo"])
def test_unsafe_database_and_lock_files_are_rejected(
    tmp_path: Path,
    filename: str,
    kind: str,
) -> None:
    state_directory = tmp_path / "state"
    connector = _identity(3)
    lease = StoreWriterLease(state_directory)
    AuthorityStore.initialize(
        state_directory,
        connector.public_identity,
        lease,
        initial_policy_digest=INITIAL_POLICY_DIGEST,
        now=NOW,
    )
    lease.close()
    _make_unsafe(state_directory / filename, kind)

    if filename == LOCK_NAME:
        with pytest.raises(ConnectorError) as error:
            StoreWriterLease(state_directory)
        _assert_error(error, ConnectorErrorCode.CONNECTOR_STATE_INVALID)
        return

    lease = StoreWriterLease(state_directory)
    try:
        with pytest.raises(ConnectorError) as error:
            AuthorityStore.open(state_directory, connector.public_identity, lease)
        _assert_error(error, ConnectorErrorCode.CONNECTOR_STATE_INVALID)
    finally:
        lease.close()


@pytest.mark.parametrize("suffix", ["-wal", "-shm"])
@pytest.mark.parametrize("kind", ["symlink", "hardlink", "wrong-mode", "fifo"])
def test_unsafe_sqlite_sidecar_is_rejected_before_sqlite_open(
    harness: StoreHarness,
    suffix: str,
    kind: str,
) -> None:
    sidecar = harness.state_directory / f"{DATABASE_NAME}{suffix}"
    if sidecar.exists() or sidecar.is_symlink():
        sidecar.unlink()
    sidecar.touch(mode=0o600)
    _make_unsafe(sidecar, kind)

    with pytest.raises(ConnectorError) as error:
        harness.store.inspect_devices()
    _assert_error(error, ConnectorErrorCode.CONNECTOR_STATE_INVALID)


def test_replacing_named_writer_lock_inode_invalidates_live_lease(
    harness: StoreHarness,
) -> None:
    lock_path = harness.state_directory / LOCK_NAME
    lock_path.rename(harness.state_directory / f"{LOCK_NAME}.old")
    lock_path.touch(mode=0o600)

    with pytest.raises(ConnectorError) as error:
        harness.store.inspect_devices()
    _assert_error(error, ConnectorErrorCode.CONNECTOR_STATE_INVALID)


def test_revocation_counter_and_supersession_cannot_roll_back(
    harness: StoreHarness,
) -> None:
    grant_id, _ = _approve_first_grant(harness, max_uses=3)
    assert harness.store.claim(
        _request(harness.connector, harness.vps, slot=100, grant_id=grant_id),
        required_scope=SCOPE,
        now=NOW + 11,
    ).accepted
    replacement = _grant(
        harness.connector,
        harness.vps,
        slot=101,
        grant_id=grant_id,
        revision=2,
        policy_revision=1,
        max_uses=3,
        issued_at=NOW + 20,
    )
    harness.store.replace_grant(replacement, now=NOW + 20)
    assert harness.store.claim(
        _request(
            harness.connector,
            harness.vps,
            slot=102,
            grant_id=grant_id,
            revision=2,
            issued_at=NOW + 21,
        ),
        required_scope=SCOPE,
        now=NOW + 21,
    ).accepted
    device_id = harness.store.inspect_devices()[0].device_id
    harness.store.revoke_device(device_id, now=NOW + 22)
    harness.store.revoke_grant(grant_id, now=NOW + 22)

    with _raw_connection(harness.state_directory) as connection:
        statements = (
            ("UPDATE devices SET revoked_at=NULL WHERE device_id=?", (device_id,)),
            (
                "UPDATE grant_lineages SET revoked_at=NULL WHERE grant_id=?",
                (grant_id,),
            ),
            (
                "UPDATE grant_revisions SET used_count=0 "
                "WHERE grant_id=? AND revision=2",
                (grant_id,),
            ),
            (
                "UPDATE grant_revisions SET superseded_at=NULL "
                "WHERE grant_id=? AND revision=1",
                (grant_id,),
            ),
        )
        for statement, parameters in statements:
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(statement, parameters)

    devices = harness.store.inspect_devices()
    revisions = harness.store.inspect_grants()
    assert devices[0].revoked_at == NOW + 22
    assert revisions[0].superseded_at == NOW + 20
    assert revisions[1].used_count == 1
    assert all(revision.revoked_at == NOW + 22 for revision in revisions)


def test_retention_cleanup_removes_expired_pairings_and_all_old_claims(
    harness: StoreHarness,
) -> None:
    grant_id, _ = _approve_first_grant(harness)
    unknown_grant_request = _request(
        harness.connector,
        harness.vps,
        slot=110,
        grant_id=_canonical_id(9_999),
    )
    denied = harness.store.claim(
        unknown_grant_request,
        required_scope=SCOPE,
        now=NOW + 11,
    )
    assert denied.cause_code is ConnectorErrorCode.GRANT_REQUIRED
    assert harness.store.claim(
        _request(harness.connector, harness.vps, slot=111, grant_id=grant_id),
        required_scope=SCOPE,
        now=NOW + 11,
    ).accepted

    pending = _pairing(
        _identity(5),
        slot=112,
        max_uses=1,
        deadline=NOW + 30,
        expires_at=NOW + 300,
    )
    harness.store.begin_pairing(
        pending, _challenge(harness.connector, pending, slot=112), now=NOW
    )
    first = harness.store.cleanup(now=NOW + 30)
    assert first.expired_pairings == 1
    assert first.removed_pairings == 0

    final = harness.store.cleanup(now=NOW + AUDIT_RETENTION_SECONDS + 31)
    assert final.expired_pairings == 0
    assert final.removed_pairings == 2
    assert final.removed_request_claims == 2
    with _raw_connection(harness.state_directory) as connection:
        assert connection.execute("SELECT COUNT(*) FROM request_claims").fetchone() == (
            0,
        )
