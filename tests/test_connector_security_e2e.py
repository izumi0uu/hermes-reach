from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import shutil
import socket
import sqlite3
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from hermes_reach.audit import AuditEvent, AuditLedger
from hermes_reach.connector.audit import ReceiptEvidenceLedger, verify_response
from hermes_reach.connector.authority import GrantAuthority
from hermes_reach.connector.client import (
    ConnectorClient,
    ConnectorSnapshotStore,
    PairedVpsProfile,
    PairingDisplay,
    PairingExchange,
    VpsPairingOrchestrator,
    VpsProfileStore,
)
from hermes_reach.connector.errors import ConnectorError, ConnectorErrorCode
from hermes_reach.connector.identity import (
    DevicePrivateIdentity,
    DevicePublicIdentity,
    VpsKeyStore,
)
from hermes_reach.connector.limits import CONNECTOR_STORAGE_SCHEMA_VERSION
from hermes_reach.connector.protocol import (
    GrantClaims,
    GrantScope,
    OperationInvocationV1,
    OperationResponseV1,
    OperationResultItemV1,
    OperationResultV1,
    PairingInit,
    PairingResolution,
    ProtocolValidationError,
    PublicBackendIdentity,
    SignedRequest,
    canonical_json_bytes,
    create_pairing_challenge,
    create_pairing_complete,
    create_pairing_resolution,
    create_signed_grant,
    create_signed_request,
    encode_record,
    load_canonical_json,
    pairing_transcript_hash,
    parse_record,
    protect_operation_call,
    record_digest,
    verify_signed_grant,
)
from hermes_reach.connector.secrets import BitwardenSecretBinding, CapabilityId
from hermes_reach.connector.store import AuthorityStore, StoreWriterLease
from hermes_reach.connector.tls import ConnectorTLSStore, verify_connector_ca_der
from hermes_reach.connector.transport import WssEndpoint
from hermes_reach.contracts import OperationCall, ReachValidationError, validate_read
from hermes_reach.sources.registry import build_alpha1_registry

NOW = 1_750_000_000
POLICY_DIGEST = hashlib.sha256(b"security-e2e-policy-v1").hexdigest()
SCOPE = GrantScope("web", "read.url", "public")
LEAF_FINGERPRINT = hashlib.sha256(b"fixture-leaf").hexdigest()
BACKEND = PublicBackendIdentity("reach-bounded-executor-v1", "1")


def _canonical_id(value: int) -> str:
    return base64.b32encode(value.to_bytes(16, "big")).decode().rstrip("=").lower()


class _IdFactory:
    def __init__(self, start: int = 100) -> None:
        self._value = start

    def __call__(self) -> str:
        self._value += 1
        return _canonical_id(self._value)


class _TrustedPairingTransport:
    def __init__(
        self,
        connector: DevicePrivateIdentity,
        ca_der: bytes,
        store: AuthorityStore,
        ids: _IdFactory,
    ) -> None:
        self._connector = connector
        self._ca_der = ca_der
        self._store = store
        self._ids = ids
        self._resolution: PairingResolution | None = None

    async def exchange(
        self, pairing_init: PairingInit, *, deadline: float
    ) -> PairingExchange:
        assert deadline > 0
        challenge = create_pairing_challenge(
            self._connector,
            message_id=self._ids(),
            pairing_id=pairing_init.pairing_id,
            init_digest=record_digest(pairing_init),
            vps_key_id=pairing_init.vps_key_id,
            connector_nonce=bytes(range(32)),
            tls_ca_der=self._ca_der,
            tls_leaf_fingerprint=LEAF_FINGERPRINT,
            issued_at=NOW,
            deadline=pairing_init.deadline,
        )
        grant = create_signed_grant(
            self._connector,
            message_id=self._ids(),
            claims=GrantClaims(
                grant_id=self._ids(),
                revision=1,
                issuer_key_id=self._connector.public_identity.key_id,
                subject_key_id=pairing_init.vps_key_id,
                issued_at=NOW,
                not_before=NOW,
                expires_at=pairing_init.grant_expires_at,
                policy_revision=1,
                max_uses=pairing_init.grant_max_uses,
                scopes=pairing_init.requested_scopes,
            ),
        )
        transcript = pairing_transcript_hash(
            encode_record(pairing_init),
            encode_record(challenge),
            observed_tls_leaf_fingerprint=LEAF_FINGERPRINT,
        )
        complete = create_pairing_complete(
            self._connector,
            message_id=self._ids(),
            pairing_id=pairing_init.pairing_id,
            transcript_digest=transcript.hex(),
            vps_key_id=pairing_init.vps_key_id,
            signed_grant_digest=record_digest(grant),
            completed_at=NOW,
        )
        self._store.begin_pairing(pairing_init, challenge, now=NOW)
        self._store.approve_pairing(
            pairing_init.pairing_id,
            device_id=self._ids(),
            transcript_digest=transcript.hex(),
            grant=grant,
            now=NOW,
        )
        self._resolution = create_pairing_resolution(
            message_id=self._ids(),
            pairing_id=pairing_init.pairing_id,
            signed_grant=grant,
            pairing_complete=complete,
        )
        return PairingExchange(
            challenge,
            verify_connector_ca_der(
                self._ca_der, self._connector.public_identity, now=NOW
            ),
            LEAF_FINGERPRINT,
        )

    async def poll(
        self,
        pairing_init: PairingInit,
        exchange: PairingExchange,
        *,
        deadline: float,
    ) -> PairingResolution | None:
        del pairing_init, exchange
        assert deadline > 0
        assert self._resolution is not None
        return self._resolution


class _AuthorityTransport:
    def __init__(self, authority: GrantAuthority, clock: Callable[[], int]) -> None:
        self._authority = authority
        self._clock = clock
        self.invocations: list[OperationInvocationV1] = []
        self.responses: list[OperationResponseV1] = []
        self.execution_calls = 0

    async def exchange(
        self, invocation: object, *, deadline: float
    ) -> OperationResponseV1:
        assert isinstance(invocation, OperationInvocationV1)
        assert deadline > 0
        self.invocations.append(invocation)

        def handoff(_: object) -> None:
            self.execution_calls += 1

        request = invocation.signed_request
        required_scope = GrantScope(request.source, request.operation, "public")
        now = max(request.issued_at, self._clock())
        decision = self._authority.authorize_and_handoff(
            request,
            invocation.protected_payload,
            required_scope,
            now=now,
            handoff=handoff,
        )
        assert decision.receipt_issuer is not None
        result = (
            OperationResultV1(
                (OperationResultItemV1("content", "fixture normalized result"),),
                False,
            )
            if decision.accepted
            else None
        )
        receipt = decision.receipt_issuer.issue(
            ended_at=request.issued_at,
            expires_at=request.issued_at + 120,
            failure_code=decision.claim.cause_code,
            backend=BACKEND if decision.accepted else None,
            result=result,
        )
        response = OperationResponseV1(receipt.message_id, receipt, result)
        self.responses.append(response)
        return response


class _SubstitutionTransport:
    def __init__(self, response: OperationResponseV1) -> None:
        self._response = response

    async def exchange(
        self, invocation: object, *, deadline: float
    ) -> OperationResponseV1:
        del invocation
        assert deadline > 0
        return self._response


@dataclass
class _SecurityHarness:
    trusted_state: Path
    vps_state: Path
    connector: DevicePrivateIdentity
    profile: PairedVpsProfile
    lease: StoreWriterLease
    store: AuthorityStore
    authority: GrantAuthority
    transport: _AuthorityTransport
    ids: _IdFactory


@pytest.fixture(autouse=True)
def _deny_real_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def denied(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise AssertionError("Connector security acceptance must remain offline")

    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(socket, "getaddrinfo", denied)
    monkeypatch.setattr(asyncio, "open_connection", denied)
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket.socket, "connect_ex", denied)


@pytest.fixture
def security_harness(tmp_path: Path) -> _SecurityHarness:
    connector = DevicePrivateIdentity._from_seed_for_testing(bytes(range(32)))
    trusted_state = tmp_path / "trusted-authority"
    lease = StoreWriterLease(trusted_state)
    store = AuthorityStore.initialize(
        trusted_state,
        connector.public_identity,
        lease,
        initial_policy_digest=POLICY_DIGEST,
        now=NOW,
    )
    ca = ConnectorTLSStore(tmp_path / "trusted-tls", _platform="linux").initialize(
        connector, now=NOW
    )
    vps_state = tmp_path / "vps-state"
    key_store = VpsKeyStore(vps_state, _platform="linux")
    key_store.initialize()
    ids = _IdFactory()
    pairing = _TrustedPairingTransport(connector, ca.der, store, ids)
    displays: list[PairingDisplay] = []

    async def no_wait(_: float) -> None:
        return None

    orchestrator = VpsPairingOrchestrator(
        key_store,
        VpsProfileStore(vps_state),
        client_factory=lambda endpoint: pairing,
        wall_clock=lambda: NOW,
        monotonic_clock=lambda: 10.0,
        sleep=no_wait,
        id_factory=ids,
        nonce_factory=lambda size: bytes([7]) * size,
    )
    profile = asyncio.run(
        orchestrator.pair(
            WssEndpoint.parse("wss://127.0.0.1:8765"),
            device_label="security-e2e-vps",
            requested_scopes=(SCOPE,),
            grant_expires_at=NOW + 3_600,
            grant_max_uses=6,
            display=displays.append,
        )
    )
    assert displays[0].scopes == (("web", "read.url", "public", None),)

    def authority_clock() -> int:
        return NOW + 10

    authority = GrantAuthority(store, id_factory=ids, clock=authority_clock)
    authority._activate_from_service(connector)
    value = _SecurityHarness(
        trusted_state,
        vps_state,
        connector,
        profile,
        lease,
        store,
        authority,
        _AuthorityTransport(authority, authority_clock),
        ids,
    )
    try:
        yield value
    finally:
        lease.close()


def _client(
    state: Path,
    profile: PairedVpsProfile,
    identity: DevicePrivateIdentity,
    transport: object,
    ids: _IdFactory,
) -> ConnectorClient:
    return ConnectorClient(
        profile,
        identity,
        transport,
        ReceiptEvidenceLedger(
            state / "receipts.jsonl", profile.connector_identity, role="vps"
        ),
        ConnectorSnapshotStore(state),
        wall_clock=lambda: NOW + 10,
        monotonic_clock=lambda: 20.0,
        id_factory=ids,
    )


def _read_call(canary: str = "article") -> OperationCall:
    return validate_read(
        {
            "source": "web",
            "operation": "read.url",
            "target": {"url": f"https://example.com/{canary}?query={canary}"},
        }
    )


def _state_bytes(directory: Path) -> bytes:
    return b"".join(
        path.read_bytes()
        for path in sorted(directory.rglob("*"))
        if path.is_file() and not path.is_symlink()
    )


def _assert_connector_error(
    error: pytest.ExceptionInfo[ConnectorError], code: ConnectorErrorCode
) -> None:
    assert error.value.code == code.value


def test_compromised_vps_copy_is_bounded_then_revocation_denies_next_request(
    security_harness: _SecurityHarness, tmp_path: Path
) -> None:
    harness = security_harness
    original_identity = VpsKeyStore(harness.vps_state, _platform="linux").load()
    original_client = _client(
        harness.vps_state,
        harness.profile,
        original_identity,
        harness.transport,
        harness.ids,
    )
    first = asyncio.run(
        original_client.execute(_read_call("VPS_QUERY_CANARY"), trace_id="1" * 32)
    )
    assert first.receipt.usage is not None
    assert first.receipt.usage.sequence == 1

    stolen_state = tmp_path / "copied-vps-state"
    shutil.copytree(harness.vps_state, stolen_state)
    stolen_identity = VpsKeyStore(stolen_state, _platform="linux").load()
    stolen_profile = VpsProfileStore(stolen_state).load()
    assert isinstance(stolen_profile, PairedVpsProfile)
    assert stolen_identity.public_identity == original_identity.public_identity
    assert stolen_profile.current_grant == harness.profile.current_grant

    stolen_client = _client(
        stolen_state,
        stolen_profile,
        stolen_identity,
        harness.transport,
        harness.ids,
    )
    residual = asyncio.run(
        stolen_client.execute(_read_call("residual"), trace_id="2" * 32)
    )
    assert residual.receipt.usage is not None
    assert residual.receipt.usage.sequence == 2
    assert harness.transport.execution_calls == 2

    widened_call = validate_read(
        {
            "source": "github",
            "operation": "read.repository",
            "target": {"native_id": "openai/hermes-reach"},
        }
    )
    widened_payload = protect_operation_call(widened_call)
    widened_request = create_signed_request(
        stolen_identity,
        message_id=harness.ids(),
        request_id=harness.ids(),
        trace_id="3" * 32,
        audience_key_id=harness.connector.public_identity.key_id,
        grant_id=stolen_profile.signed_grant.claims.grant_id,
        grant_revision=1,
        policy_revision=1,
        source="github",
        operation="read.repository",
        issued_at=NOW + 10,
        deadline=NOW + 30,
        protected_payload=widened_payload,
    )
    widened_response = asyncio.run(
        harness.transport.exchange(
            OperationInvocationV1(
                widened_request.message_id, widened_request, widened_payload
            ),
            deadline=30.0,
        )
    )
    assert widened_response.receipt.failure is not None
    assert (
        widened_response.receipt.failure.cause_code
        is ConnectorErrorCode.GRANT_SCOPE_DENIED
    )
    assert harness.transport.execution_calls == 2

    accepted_invocation = harness.transport.invocations[0]
    replay = asyncio.run(harness.transport.exchange(accepted_invocation, deadline=30.0))
    verify_response(
        replay,
        pinned_connector=harness.connector.public_identity,
        request=accepted_invocation.signed_request,
        now=NOW + 10,
    )
    assert replay.receipt.failure is not None
    assert replay.receipt.failure.cause_code is ConnectorErrorCode.REQUEST_REPLAYED
    assert harness.transport.execution_calls == 2

    evidence = ReceiptEvidenceLedger(
        stolen_state / "receipts.jsonl",
        stolen_profile.connector_identity,
        role="vps",
    )
    evidence_count = len(evidence.records())
    substituted_client = _client(
        stolen_state,
        stolen_profile,
        stolen_identity,
        _SubstitutionTransport(first),
        harness.ids,
    )
    with pytest.raises(ConnectorError) as substitution:
        asyncio.run(
            substituted_client.execute(_read_call("substituted"), trace_id="4" * 32)
        )
    _assert_connector_error(substitution, ConnectorErrorCode.RECEIPT_CONTEXT_MISMATCH)
    assert len(evidence.records()) == evidence_count

    harness.authority.revoke_grant(
        stolen_profile.signed_grant.claims.grant_id, now=NOW + 11
    )
    with pytest.raises(ConnectorError) as revoked:
        asyncio.run(
            stolen_client.execute(_read_call("after-revoke"), trace_id="5" * 32)
        )
    _assert_connector_error(revoked, ConnectorErrorCode.GRANT_REVOKED)
    assert harness.transport.execution_calls == 2
    assert harness.store.inspect_grants()[0].used_count == 2

    copied = _state_bytes(stolen_state)
    forbidden = (
        b"VPS_QUERY_CANARY",
        b"BSM_PROJECT_CANARY",
        b"SELECTOR_CANARY",
        b"TRUSTED_PATH_CANARY",
        str(harness.trusted_state).encode(),
        harness.connector._private_bytes(None),
    )
    assert all(value not in copied for value in forbidden)


def test_forged_widened_grant_is_rejected(
    security_harness: _SecurityHarness,
) -> None:
    harness = security_harness
    stolen_identity = VpsKeyStore(harness.vps_state, _platform="linux").load()
    widened_claims = replace(
        harness.profile.signed_grant.claims,
        scopes=(GrantScope("github", "read.repository", "public"), SCOPE),
    )
    forged_grant = replace(harness.profile.signed_grant, claims=widened_claims)

    with pytest.raises(ProtocolValidationError):
        verify_signed_grant(
            forged_grant,
            pinned_connector=harness.connector.public_identity,
            expected_subject_key_id=stolen_identity.public_identity.key_id,
            now=NOW + 10,
        )


def test_secret_provider_and_local_path_fields_are_rejected() -> None:
    rejected_inputs = (
        {
            "source": "web",
            "operation": "read.url",
            "provider": "bitwarden:BSM_PROJECT_CANARY:SELECTOR_CANARY",
            "target": {"url": "https://example.com/article"},
        },
        {
            "source": "web",
            "operation": "read.url",
            "target": {"path": "/trusted/TRUSTED_PATH_CANARY/media.wav"},
        },
    )

    for payload in rejected_inputs:
        with pytest.raises(ReachValidationError):
            validate_read(payload)


def test_local_public_registry_remains_available() -> None:
    local_registry = build_alpha1_registry()
    local_rss = local_registry.availability("rss", "read.feed")

    assert local_rss.state == "available"
    assert local_rss.backend_id == "feedparser"


def test_unknown_protocol_schema_key_and_provider_versions_fail_closed(
    security_harness: _SecurityHarness, tmp_path: Path
) -> None:
    harness = security_harness
    encoded = load_canonical_json(encode_record(harness.profile.signed_grant))
    assert isinstance(encoded, dict)
    encoded["protocol"] = "reach-connector/v2"
    with pytest.raises(ProtocolValidationError):
        parse_record(canonical_json_bytes(encoded))

    with pytest.raises(ValueError, match="public identity encoding"):
        DevicePublicIdentity.from_wire(
            "v2:" + harness.connector.public_identity.wire_public_key
        )

    drifted_state = tmp_path / "future-vps-state"
    shutil.copytree(harness.vps_state, drifted_state)
    profile_path = drifted_state / "vps-profile.json"
    profile_payload = load_canonical_json(profile_path.read_bytes())
    assert isinstance(profile_payload, dict)
    profile_payload["version"] = 2
    profile_path.write_bytes(canonical_json_bytes(profile_payload))
    os.chmod(profile_path, 0o600)
    with pytest.raises(ConnectorError) as schema:
        VpsProfileStore(drifted_state).load()
    _assert_connector_error(schema, ConnectorErrorCode.CONNECTOR_SCHEMA_INCOMPATIBLE)

    with pytest.raises(ValueError, match="secret provider binding"):
        BitwardenSecretBinding(
            capability_id=CapabilityId.new(lambda size: b"\x01" * size),
            source="web",
            operation="read.url",
            project_id="00000000-0000-4000-8000-000000000001",
            selector="SELECTED_KEY",
            injection_target="SCOPED_KEY",
            profile_home=(tmp_path / "connector-profile").resolve(),
            bws_sha256="0" * 64,
            provider="future-provider",
        )


def test_audit_ledger_append_has_no_implicit_export(
    tmp_path: Path,
) -> None:
    ledger = AuditLedger(tmp_path / "audit" / "ledger.jsonl")
    ledger.append(
        AuditEvent(
            trace_id="6" * 32,
            device_id="local",
            source="web",
            operation="read.url",
            backend_id="fixture-backend-v1",
            backend_version="1",
            catalog_version="v1",
            policy_revision="1",
            grant_revision=None,
            occurred_at=datetime.fromtimestamp(NOW, tz=UTC),
            duration_ms=1,
            attempt_count=1,
            failure_class=None,
            result_count=1,
            truncated=False,
            outcome="ok",
        )
    )

    records = ledger.records()
    assert len(records) == 1
    assert records[0].event.trace_id == "6" * 32


def test_live_revocation_and_supersession_survive_backup_and_rollback_simulation(
    security_harness: _SecurityHarness, tmp_path: Path
) -> None:
    harness = security_harness
    database = harness.trusted_state / "connector-authority.sqlite3"
    backup = harness.trusted_state / "connector-authority.sqlite3.rollback"
    with (
        closing(sqlite3.connect(database)) as source,
        closing(sqlite3.connect(backup)) as target,
    ):
        source.backup(target)
        target.commit()
    os.chmod(backup, 0o600)

    claims = harness.profile.signed_grant.claims
    replacement = create_signed_grant(
        harness.connector,
        message_id=harness.ids(),
        claims=replace(
            claims,
            revision=2,
            issued_at=NOW + 10,
            not_before=NOW + 10,
        ),
    )
    harness.authority.replace_grant(replacement, now=NOW + 10)
    harness.authority.revoke_grant(claims.grant_id, now=NOW + 11)
    harness.lease.close()

    with closing(sqlite3.connect(backup)) as stale:
        row = stale.execute(
            "SELECT revoked_at FROM grant_lineages WHERE grant_id=?",
            (claims.grant_id,),
        ).fetchone()
    assert row == (None,)

    live_lease = StoreWriterLease(harness.trusted_state)
    try:
        live = AuthorityStore.open(
            harness.trusted_state, harness.connector.public_identity, live_lease
        )
        revisions = live.inspect_grants()
        assert revisions[0].superseded_at == NOW + 10
        assert revisions[1].superseded_at is None
        assert all(revision.revoked_at == NOW + 11 for revision in revisions)

        stolen_identity = VpsKeyStore(harness.vps_state, _platform="linux").load()
        for slot, revision in ((70, 1), (71, 2)):
            payload = protect_operation_call(_read_call(f"rollback-{revision}"))
            request: SignedRequest = create_signed_request(
                stolen_identity,
                message_id=_canonical_id(8_000 + slot),
                request_id=_canonical_id(9_000 + slot),
                trace_id=f"{slot:032x}",
                audience_key_id=harness.connector.public_identity.key_id,
                grant_id=claims.grant_id,
                grant_revision=revision,
                policy_revision=1,
                source="web",
                operation="read.url",
                issued_at=NOW + 12,
                deadline=NOW + 30,
                protected_payload=payload,
            )
            denied = live.claim(request, required_scope=SCOPE, now=NOW + 12)
            assert not denied.accepted
            assert denied.cause_code is ConnectorErrorCode.GRANT_REVOKED
    finally:
        live_lease.close()

    future_state = tmp_path / "future-authority"
    shutil.copytree(harness.trusted_state, future_state)
    future_database = future_state / "connector-authority.sqlite3"
    with closing(sqlite3.connect(future_database)) as connection:
        connection.execute(
            f"PRAGMA user_version={CONNECTOR_STORAGE_SCHEMA_VERSION + 1}"
        )
        connection.commit()
    content_before = future_database.read_bytes()
    modified_before = future_database.stat().st_mtime_ns
    future_lease = StoreWriterLease(future_state)
    try:
        with pytest.raises(ConnectorError) as incompatible:
            AuthorityStore.open(
                future_state, harness.connector.public_identity, future_lease
            )
        _assert_connector_error(
            incompatible, ConnectorErrorCode.CONNECTOR_SCHEMA_INCOMPATIBLE
        )
    finally:
        future_lease.close()
    assert future_database.read_bytes() == content_before
    assert future_database.stat().st_mtime_ns == modified_before
