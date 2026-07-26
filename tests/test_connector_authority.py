from __future__ import annotations

import base64
import hashlib
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import pytest

from hermes_reach.connector.audit import verify_receipt
from hermes_reach.connector.authority import (
    AuthorizedExecution,
    GrantAuthority,
    UnauthenticatedRequestError,
)
from hermes_reach.connector.errors import ConnectorErrorCode
from hermes_reach.connector.identity import DevicePrivateIdentity, DevicePublicIdentity
from hermes_reach.connector.limits import MAX_CLOCK_SKEW_SECONDS
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
POLICY_DIGEST = hashlib.sha256(b"policy-1").hexdigest()
SCOPE = GrantScope("web", "read.url", "public")
PROTECTED = protect_operation_call(
    validate_read(
        {
            "source": "web",
            "operation": "read.url",
            "target": {"url": "https://example.com/authority-canary"},
        }
    )
)
OTHER_PROTECTED = protect_operation_call(
    validate_read(
        {
            "source": "web",
            "operation": "read.url",
            "target": {"url": "https://example.com/substituted"},
        }
    )
)


def _canonical_id(value: int) -> str:
    return base64.b32encode(value.to_bytes(16, "big")).decode().rstrip("=").lower()


def _identity(seed: int) -> DevicePrivateIdentity:
    return DevicePrivateIdentity._from_seed_for_testing(bytes([seed]) * 32)


def _challenge(
    connector: DevicePrivateIdentity, pairing: PairingInit
) -> PairingChallenge:
    return create_pairing_challenge(
        signer=connector,
        message_id=_canonical_id(49_999),
        pairing_id=pairing.pairing_id,
        init_digest=record_digest(pairing),
        vps_key_id=pairing.vps_key_id,
        connector_nonce=bytes(range(32, 64)),
        tls_ca_der=b"authority-test-ca",
        tls_leaf_fingerprint=hashlib.sha256(b"authority-test-leaf").hexdigest(),
        issued_at=NOW + 1,
        deadline=pairing.deadline,
    )


class _IdFactory:
    def __init__(self) -> None:
        self._value = 50_000

    def __call__(self) -> str:
        self._value += 1
        return _canonical_id(self._value)


class _Clock:
    def __init__(self, value: int) -> None:
        self.value = value

    def __call__(self) -> int:
        return self.value


@dataclass
class AuthorityHarness:
    state_directory: Path
    connector: DevicePrivateIdentity
    vps: DevicePrivateIdentity
    lease: StoreWriterLease
    store: AuthorityStore
    authority: GrantAuthority
    grant_id: str
    clock: _Clock


@pytest.fixture
def harness(tmp_path: Path) -> AuthorityHarness:
    state_directory = tmp_path / "state"
    connector = _identity(10)
    vps = _identity(11)
    lease = StoreWriterLease(state_directory)
    store = AuthorityStore.initialize(
        state_directory,
        connector.public_identity,
        lease,
        initial_policy_digest=POLICY_DIGEST,
        now=NOW,
    )
    pairing = create_pairing_init(
        signer=vps,
        message_id=_canonical_id(1),
        pairing_id=_canonical_id(2),
        device_label="authority-vps",
        endpoint_digest=hashlib.sha256(b"endpoint").hexdigest(),
        vps_nonce=bytes(range(32)),
        requested_scopes=(SCOPE,),
        grant_expires_at=NOW + 3_600,
        grant_max_uses=20,
        issued_at=NOW,
        deadline=NOW + 300,
    )
    grant_id = _canonical_id(3)
    grant = _grant(connector, vps, grant_id=grant_id, revision=1, policy_revision=1)
    challenge = _challenge(connector, pairing)
    store.begin_pairing(pairing, challenge, now=NOW)
    store.approve_pairing(
        pairing.pairing_id,
        device_id=_canonical_id(4),
        transcript_digest=pairing_transcript_hash(
            encode_record(pairing),
            encode_record(challenge),
            observed_tls_leaf_fingerprint=challenge.tls_leaf_fingerprint,
        ).hex(),
        grant=grant,
        now=NOW + 2,
    )
    clock = _Clock(NOW)
    authority = GrantAuthority(store, id_factory=_IdFactory(), clock=clock)
    authority._activate_from_service(connector)
    value = AuthorityHarness(
        state_directory, connector, vps, lease, store, authority, grant_id, clock
    )
    try:
        yield value
    finally:
        lease.close()


def _grant(
    connector: DevicePrivateIdentity,
    vps: DevicePrivateIdentity,
    *,
    grant_id: str,
    revision: int,
    policy_revision: int,
    issued_at: int = NOW + 1,
) -> SignedGrant:
    return create_signed_grant(
        signer=connector,
        message_id=_canonical_id(100 + revision + policy_revision),
        claims=GrantClaims(
            grant_id=grant_id,
            revision=revision,
            issuer_key_id=connector.public_identity.key_id,
            subject_key_id=vps.public_identity.key_id,
            issued_at=issued_at,
            not_before=issued_at,
            expires_at=NOW + 3_600,
            policy_revision=policy_revision,
            max_uses=20,
            scopes=(SCOPE,),
        ),
    )


def _request(
    harness: AuthorityHarness,
    slot: int,
    *,
    signer: DevicePrivateIdentity | None = None,
    revision: int = 1,
    policy_revision: int = 1,
    issued_at: int = NOW + 10,
) -> SignedRequest:
    return create_signed_request(
        signer=harness.vps if signer is None else signer,
        message_id=_canonical_id(1_000 + slot),
        request_id=_canonical_id(2_000 + slot),
        trace_id=f"{slot:032x}",
        audience_key_id=harness.connector.public_identity.key_id,
        grant_id=harness.grant_id,
        grant_revision=revision,
        policy_revision=policy_revision,
        source=SCOPE.source,
        operation=SCOPE.operation,
        issued_at=issued_at,
        deadline=issued_at + 60,
        protected_payload=PROTECTED,
    )


def test_authority_commits_claim_before_redacted_executor_handoff(
    harness: AuthorityHarness,
) -> None:
    seen: list[AuthorizedExecution] = []

    def handoff(execution: AuthorizedExecution) -> str:
        seen.append(execution)
        assert "authority-canary" not in repr(execution)
        assert execution.operation_call().target == {
            "url": "https://example.com/authority-canary"
        }
        return "started"

    decision = harness.authority.authorize_and_handoff(
        _request(harness, 1),
        PROTECTED,
        SCOPE,
        now=NOW + 11,
        handoff=handoff,
    )

    assert decision.accepted
    assert decision.handoff_result == "started"
    assert decision.claim.use_sequence == 1
    assert decision.receipt_issuer is not None
    assert len(seen) == 1
    assert harness.store.inspect_grants()[0].used_count == 1


@pytest.mark.parametrize(
    ("connector_now", "request_issued_at", "slot"),
    (
        (NOW + 1, NOW + 1 + MAX_CLOCK_SKEW_SECONDS, 31),
        (NOW + 1 + MAX_CLOCK_SKEW_SECONDS, NOW + 1, 32),
    ),
)
def test_receipt_times_cover_allowed_vps_clock_skew(
    harness: AuthorityHarness,
    connector_now: int,
    request_issued_at: int,
    slot: int,
) -> None:
    request = _request(harness, slot, issued_at=request_issued_at)
    decision = harness.authority.authorize_and_handoff(
        request,
        PROTECTED,
        SCOPE,
        now=connector_now,
        handoff=lambda execution: execution,
    )
    assert decision.accepted
    assert decision.receipt_issuer is not None
    ended_at = max(connector_now, request_issued_at)

    receipt = decision.receipt_issuer.issue(
        ended_at=ended_at,
        expires_at=ended_at + 120,
        failure_code=ConnectorErrorCode.BACKEND_UNBOUND,
    )

    assert receipt.started_at == max(connector_now, request_issued_at)
    assert (
        verify_receipt(
            receipt,
            pinned_connector=harness.connector.public_identity,
            request=request,
            now=request_issued_at,
        )
        is receipt
    )


def test_unpaired_or_payload_substituted_request_never_reaches_authority(
    harness: AuthorityHarness,
) -> None:
    calls = 0

    def handoff(_: AuthorizedExecution) -> None:
        nonlocal calls
        calls += 1

    stranger = _identity(12)
    with pytest.raises(UnauthenticatedRequestError):
        harness.authority.authorize_and_handoff(
            _request(harness, 2, signer=stranger),
            PROTECTED,
            SCOPE,
            now=NOW + 11,
            handoff=handoff,
        )
    with pytest.raises(ProtocolValidationError):
        harness.authority.authorize_and_handoff(
            _request(harness, 3),
            OTHER_PROTECTED,
            SCOPE,
            now=NOW + 11,
            handoff=handoff,
        )
    assert calls == 0
    assert harness.store.inspect_grants()[0].used_count == 0

    with pytest.raises(ProtocolValidationError):
        harness.authority.authorize_and_handoff(
            cast(SignedRequest, object()),
            PROTECTED,
            SCOPE,
            now=NOW + 11,
            handoff=handoff,
        )


def test_locked_authority_records_denial_without_exposing_a_signer(
    harness: AuthorityHarness,
) -> None:
    harness.authority.lock()
    request = _request(harness, 4)
    decision = harness.authority.authorize_and_handoff(
        request,
        PROTECTED,
        SCOPE,
        now=NOW + 11,
        handoff=lambda _: pytest.fail("locked authority invoked the executor"),
    )
    assert not decision.accepted
    assert decision.claim.cause_code is ConnectorErrorCode.CONNECTOR_KEY_LOCKED
    assert decision.receipt_issuer is None
    assert harness.store.inspect_grants()[0].used_count == 0

    harness.authority._activate_from_service(harness.connector)
    replay = harness.authority.authorize_and_handoff(
        request,
        PROTECTED,
        SCOPE,
        now=NOW + 12,
        handoff=lambda _: pytest.fail("replayed request invoked the executor"),
    )
    assert replay.claim.cause_code is ConnectorErrorCode.REQUEST_REPLAYED
    assert replay.receipt_issuer is not None


def test_request_that_expires_while_waiting_for_mutex_never_reaches_handoff(
    harness: AuthorityHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request(harness, 5)
    identity_loaded = threading.Event()
    original = harness.store.active_device_identity

    def observed_lookup(key_id: str) -> DevicePublicIdentity | None:
        identity = original(key_id)
        identity_loaded.set()
        return identity

    monkeypatch.setattr(harness.store, "active_device_identity", observed_lookup)
    executor = ThreadPoolExecutor(max_workers=1)
    harness.authority._mutex.acquire()
    released = False
    try:
        future = executor.submit(
            harness.authority.authorize_and_handoff,
            request,
            PROTECTED,
            SCOPE,
            now=NOW + 11,
            handoff=lambda _: pytest.fail(
                "expired queued request invoked the executor"
            ),
        )
        assert identity_loaded.wait(5)
        harness.clock.value = request.deadline
        harness.authority._mutex.release()
        released = True
        decision = future.result(timeout=5)
    finally:
        if not released:
            harness.authority._mutex.release()
        executor.shutdown(wait=True, cancel_futures=True)

    assert not decision.accepted
    assert decision.claim.cause_code is ConnectorErrorCode.CONNECTOR_DEADLINE_EXCEEDED
    assert decision.receipt_issuer is not None
    receipt = decision.receipt_issuer.issue(
        ended_at=request.deadline,
        expires_at=request.deadline + 120,
    )
    assert receipt.decision == "deny"
    assert (
        verify_receipt(
            receipt,
            pinned_connector=harness.connector.public_identity,
            request=request,
            now=request.deadline,
        )
        is receipt
    )
    assert harness.store.inspect_grants()[0].used_count == 0


Mutation = str


def _apply_mutation(harness: AuthorityHarness, mutation: Mutation) -> None:
    if mutation == "lock":
        harness.authority.lock()
    elif mutation == "revoke":
        harness.authority.revoke_grant(harness.grant_id, now=NOW + 20)
    elif mutation == "replace":
        harness.authority.replace_grant(
            _grant(
                harness.connector,
                harness.vps,
                grant_id=harness.grant_id,
                revision=2,
                policy_revision=1,
                issued_at=NOW + 20,
            ),
            now=NOW + 20,
        )
    elif mutation == "policy":
        harness.authority.advance_policy_revision(
            hashlib.sha256(b"policy-2").hexdigest(), now=NOW + 20
        )
    else:
        raise AssertionError("unknown mutation")


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        ("lock", ConnectorErrorCode.CONNECTOR_KEY_LOCKED),
        ("revoke", ConnectorErrorCode.GRANT_REVOKED),
        ("replace", ConnectorErrorCode.GRANT_SUPERSEDED),
        ("policy", ConnectorErrorCode.POLICY_REVISION_STALE),
    ],
)
def test_mutation_that_commits_first_prevents_executor_handoff(
    harness: AuthorityHarness,
    mutation: Mutation,
    expected: ConnectorErrorCode,
) -> None:
    _apply_mutation(harness, mutation)
    calls = 0

    def handoff(_: AuthorizedExecution) -> None:
        nonlocal calls
        calls += 1

    decision = harness.authority.authorize_and_handoff(
        _request(harness, 10),
        PROTECTED,
        SCOPE,
        now=NOW + 21,
        handoff=handoff,
    )
    assert not decision.accepted
    assert decision.claim.cause_code is expected
    assert calls == 0
    assert harness.store.inspect_grants()[0].used_count == 0


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        ("lock", ConnectorErrorCode.CONNECTOR_KEY_LOCKED),
        ("revoke", ConnectorErrorCode.GRANT_REVOKED),
        ("replace", ConnectorErrorCode.GRANT_SUPERSEDED),
        ("policy", ConnectorErrorCode.POLICY_REVISION_STALE),
    ],
)
def test_claim_handoff_commits_before_mutation_and_next_request_is_denied(
    harness: AuthorityHarness,
    mutation: Mutation,
    expected: ConnectorErrorCode,
) -> None:
    entered_handoff = threading.Event()
    release_handoff = threading.Event()
    mutation_done = threading.Event()

    def handoff(execution: AuthorizedExecution) -> int:
        entered_handoff.set()
        assert release_handoff.wait(5)
        return execution.use_sequence

    def mutate() -> None:
        _apply_mutation(harness, mutation)
        mutation_done.set()

    with ThreadPoolExecutor(max_workers=2) as executor:
        decision_future = executor.submit(
            harness.authority.authorize_and_handoff,
            _request(harness, 20),
            PROTECTED,
            SCOPE,
            now=NOW + 11,
            handoff=handoff,
        )
        assert entered_handoff.wait(5)
        mutation_future = executor.submit(mutate)
        assert not mutation_done.wait(0.1)
        release_handoff.set()
        decision = decision_future.result(timeout=5)
        mutation_future.result(timeout=5)

    assert decision.accepted
    assert decision.handoff_result == 1
    assert mutation_done.is_set()

    following = harness.authority.authorize_and_handoff(
        _request(harness, 21, issued_at=NOW + 21),
        PROTECTED,
        SCOPE,
        now=NOW + 21,
        handoff=lambda _: pytest.fail("post-mutation request invoked the executor"),
    )
    assert following.claim.cause_code is expected
