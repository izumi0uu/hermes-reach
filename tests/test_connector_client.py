from __future__ import annotations

import asyncio
import base64
import os
import stat
import time
from dataclasses import replace
from pathlib import Path

import pytest

from hermes_reach.connector.audit import ReceiptEvidenceLedger
from hermes_reach.connector.client import (
    ConnectorAvailabilityResolver,
    ConnectorClient,
    ConnectorSnapshot,
    ConnectorSnapshotStore,
    PairedVpsProfile,
    PairingDisplay,
    PairingExchange,
    PairingWssClient,
    VpsPairingOrchestrator,
    VpsProfileStore,
    _endpoint_digest,
)
from hermes_reach.connector.errors import ConnectorError, ConnectorErrorCode
from hermes_reach.connector.identity import DevicePrivateIdentity, VpsKeyStore
from hermes_reach.connector.limits import (
    MAX_CLOCK_SKEW_SECONDS,
    PAIRING_TTL_SECONDS,
)
from hermes_reach.connector.protocol import (
    ErrorFrame,
    GrantClaims,
    GrantScope,
    OperationInvocationV1,
    OperationResponseV1,
    OperationResultItemV1,
    OperationResultV1,
    PairingInit,
    PairingResolution,
    PublicBackendIdentity,
    ReceiptFailure,
    ReceiptUsage,
    canonical_json_bytes,
    create_pairing_challenge,
    create_pairing_complete,
    create_pairing_resolution,
    create_signed_grant,
    create_signed_receipt,
    encode_record,
    load_canonical_json,
    pairing_sas,
    pairing_transcript_hash,
    record_digest,
)
from hermes_reach.connector.tls import ConnectorTLSStore, verify_connector_ca_der
from hermes_reach.connector.transport import ConnectorDeliveryError, WssEndpoint
from hermes_reach.contracts import validate_read

NOW = int(time.time())
LEAF_FINGERPRINT = "ab" * 32
TRACE_ID = "1" * 32
CANARY = "QUERY_CANARY=TOKEN_CANARY"


def _canonical_id(value: int) -> str:
    return base64.b32encode(value.to_bytes(16, "big")).decode().rstrip("=").lower()


class _IdFactory:
    def __init__(self, start: int = 100) -> None:
        self._value = start

    def __call__(self) -> str:
        self._value += 1
        return _canonical_id(self._value)


class _PairingClient:
    def __init__(
        self,
        connector: DevicePrivateIdentity,
        ca_der: bytes,
        ids: _IdFactory,
        *,
        pending_polls: int = 0,
    ) -> None:
        self._connector = connector
        self._ca_der = ca_der
        self._ids = ids
        self._pending_polls = pending_polls
        self.exchange_init: PairingInit | None = None
        self.poll_inits: list[PairingInit] = []
        self.exchange_result: PairingExchange | None = None

    async def exchange(
        self, pairing_init: PairingInit, *, deadline: float
    ) -> PairingExchange:
        assert deadline > 0
        self.exchange_init = pairing_init
        challenge = create_pairing_challenge(
            self._connector,
            message_id=self._ids(),
            pairing_id=pairing_init.pairing_id,
            init_digest=record_digest(pairing_init),
            vps_key_id=pairing_init.vps_key_id,
            connector_nonce=bytes(range(32)),
            tls_ca_der=self._ca_der,
            tls_leaf_fingerprint=LEAF_FINGERPRINT,
            issued_at=pairing_init.issued_at,
            deadline=pairing_init.deadline,
        )
        self.exchange_result = PairingExchange(
            challenge,
            verify_connector_ca_der(
                self._ca_der,
                self._connector.public_identity,
                now=pairing_init.issued_at,
            ),
            LEAF_FINGERPRINT,
        )
        return self.exchange_result

    async def poll(
        self,
        pairing_init: PairingInit,
        exchange: PairingExchange,
        *,
        deadline: float,
    ) -> PairingResolution | None:
        assert deadline > 0
        self.poll_inits.append(pairing_init)
        if len(self.poll_inits) <= self._pending_polls:
            return None
        grant = create_signed_grant(
            self._connector,
            message_id=self._ids(),
            claims=GrantClaims(
                grant_id=self._ids(),
                revision=1,
                issuer_key_id=self._connector.public_identity.key_id,
                subject_key_id=pairing_init.vps_key_id,
                issued_at=pairing_init.issued_at,
                not_before=pairing_init.issued_at,
                expires_at=pairing_init.grant_expires_at,
                policy_revision=1,
                max_uses=pairing_init.grant_max_uses,
                scopes=pairing_init.requested_scopes,
            ),
        )
        transcript = pairing_transcript_hash(
            encode_record(pairing_init),
            encode_record(exchange.challenge),
            observed_tls_leaf_fingerprint=exchange.observed_tls_leaf_fingerprint,
        )
        complete = create_pairing_complete(
            self._connector,
            message_id=self._ids(),
            pairing_id=pairing_init.pairing_id,
            transcript_digest=transcript.hex(),
            vps_key_id=pairing_init.vps_key_id,
            signed_grant_digest=record_digest(grant),
            completed_at=pairing_init.issued_at,
        )
        return create_pairing_resolution(
            message_id=self._ids(),
            pairing_id=pairing_init.pairing_id,
            signed_grant=grant,
            pairing_complete=complete,
        )


class _ReceiptTransport:
    def __init__(
        self,
        connector: DevicePrivateIdentity,
        ids: _IdFactory,
        *,
        tamper: bool = False,
    ) -> None:
        self._connector = connector
        self._ids = ids
        self._tamper = tamper
        self.requests = []

    async def exchange(
        self, invocation: object, *, deadline: float
    ) -> OperationResponseV1:
        assert isinstance(invocation, OperationInvocationV1)
        assert deadline > 0
        self.requests.append(invocation)
        request = invocation.signed_request
        result = OperationResultV1(
            (OperationResultItemV1("content", "normalized result"),),
            False,
        )
        receipt = create_signed_receipt(
            self._connector,
            message_id=self._ids(),
            receipt_id=self._ids(),
            request=request,
            decision="allow",
            failure=None,
            usage=ReceiptUsage(1, 9),
            backend=PublicBackendIdentity("reach-bounded-executor-v1", "1"),
            started_at=request.issued_at,
            ended_at=request.issued_at + 1,
            expires_at=request.issued_at + 120,
            result=result,
            outcome="ok",
        )
        if self._tamper:
            receipt = replace(
                receipt,
                signature=bytes([receipt.signature[0] ^ 1]) + receipt.signature[1:],
            )
        return OperationResponseV1(receipt.message_id, receipt, result)


class _ResultTamperingTransport(_ReceiptTransport):
    async def exchange(
        self, invocation: object, *, deadline: float
    ) -> OperationResponseV1:
        response = await super().exchange(invocation, deadline=deadline)
        tampered = OperationResultV1(
            (OperationResultItemV1("content", "tampered after receipt creation"),),
            False,
        )
        object.__setattr__(response, "result", tampered)
        return response


class _OfflineTransport:
    def __init__(self) -> None:
        self.calls = 0

    async def exchange(
        self, request: object, *, deadline: float
    ) -> OperationResponseV1:
        del request, deadline
        self.calls += 1
        raise ConnectorError(ConnectorErrorCode.CONNECTOR_OFFLINE)


class _ClosedErrorTransport:
    def __init__(self, code: ConnectorErrorCode) -> None:
        self._code = code
        self.calls = 0

    async def exchange(
        self, request: object, *, deadline: float
    ) -> OperationResponseV1:
        del request, deadline
        self.calls += 1
        raise ConnectorError(self._code)


class _DeadlineExhaustingTransport:
    def __init__(self) -> None:
        self.calls = 0

    async def exchange(
        self, request: object, *, deadline: float
    ) -> OperationResponseV1:
        del request, deadline
        self.calls += 1
        raise ConnectorDeliveryError(
            ConnectorErrorCode.CONNECTOR_DEADLINE_EXCEEDED,
            "delivery_unknown",
        )


class _RetryingReceiptTransport(_ReceiptTransport):
    def __init__(self, connector: DevicePrivateIdentity, ids: _IdFactory) -> None:
        super().__init__(connector, ids)
        self.attempts: list[OperationInvocationV1] = []
        self.deadlines: list[float] = []

    async def exchange(
        self, invocation: object, *, deadline: float
    ) -> OperationResponseV1:
        assert isinstance(invocation, OperationInvocationV1)
        self.attempts.append(invocation)
        self.deadlines.append(deadline)
        if len(self.attempts) == 1:
            raise ConnectorDeliveryError(
                ConnectorErrorCode.CONNECTOR_OFFLINE, "not_sent"
            )
        return await super().exchange(invocation, deadline=deadline)


class _LostResponseTransport:
    def __init__(self, connector: DevicePrivateIdentity, ids: _IdFactory) -> None:
        self._connector = connector
        self._ids = ids
        self.attempts: list[OperationInvocationV1] = []
        self.deadlines: list[float] = []

    async def exchange(
        self, invocation: object, *, deadline: float
    ) -> OperationResponseV1:
        assert isinstance(invocation, OperationInvocationV1)
        self.attempts.append(invocation)
        self.deadlines.append(deadline)
        attempt = len(self.attempts)
        if attempt == 1:
            raise ConnectorDeliveryError(
                ConnectorErrorCode.CONNECTOR_OFFLINE, "delivery_unknown"
            )
        request = invocation.signed_request
        if attempt > 2:
            result = OperationResultV1(
                (OperationResultItemV1("content", "recovered new request"),),
                False,
            )
            receipt = create_signed_receipt(
                self._connector,
                message_id=self._ids(),
                receipt_id=self._ids(),
                request=request,
                decision="allow",
                failure=None,
                usage=ReceiptUsage(2, 8),
                backend=PublicBackendIdentity("reach-bounded-executor-v1", "1"),
                started_at=request.issued_at,
                ended_at=request.issued_at + 1,
                expires_at=request.issued_at + 120,
                result=result,
                outcome="ok",
            )
            return OperationResponseV1(receipt.message_id, receipt, result)
        receipt = create_signed_receipt(
            self._connector,
            message_id=self._ids(),
            receipt_id=self._ids(),
            request=request,
            decision="deny",
            failure=ReceiptFailure("authority", ConnectorErrorCode.REQUEST_REPLAYED),
            usage=None,
            backend=None,
            started_at=request.issued_at,
            ended_at=request.issued_at + 1,
            expires_at=request.issued_at + 120,
            result=None,
            outcome="error",
        )
        return OperationResponseV1(receipt.message_id, receipt, None)


class _AcceptedFailureTransport:
    def __init__(
        self,
        connector: DevicePrivateIdentity,
        ids: _IdFactory,
        failure: ReceiptFailure,
    ) -> None:
        self._connector = connector
        self._ids = ids
        self._failure = failure
        self.calls = 0

    async def exchange(
        self, invocation: object, *, deadline: float
    ) -> OperationResponseV1:
        assert isinstance(invocation, OperationInvocationV1)
        assert deadline > 0
        self.calls += 1
        request = invocation.signed_request
        receipt = create_signed_receipt(
            self._connector,
            message_id=self._ids(),
            receipt_id=self._ids(),
            request=request,
            decision="allow",
            failure=self._failure,
            usage=ReceiptUsage(1, 9),
            backend=None,
            started_at=request.issued_at,
            ended_at=request.issued_at + 1,
            expires_at=request.issued_at + 120,
            result=None,
            outcome="error",
        )
        return OperationResponseV1(receipt.message_id, receipt, None)


class _UnsignedTransport:
    async def exchange(self, request: object, *, deadline: float) -> ErrorFrame:
        del request, deadline
        return ErrorFrame(_canonical_id(1900), ConnectorErrorCode.GRANT_REVOKED)


class _InterruptedPairingClient(_PairingClient):
    async def poll(
        self,
        pairing_init: PairingInit,
        exchange: PairingExchange,
        *,
        deadline: float,
    ) -> PairingResolution | None:
        del pairing_init, exchange, deadline
        raise ConnectorError(ConnectorErrorCode.CONNECTOR_OFFLINE)


def _scope() -> GrantScope:
    return GrantScope("web", "read.url", "public")


def _paired_fixture(
    tmp_path: Path,
    *,
    pending_polls: int = 0,
    requested_scopes: tuple[GrantScope, ...] | None = None,
) -> tuple[
    PairedVpsProfile,
    DevicePrivateIdentity,
    DevicePrivateIdentity,
    VpsProfileStore,
    list[PairingDisplay],
    _PairingClient,
]:
    state_directory = tmp_path / "vps"
    key_store = VpsKeyStore(state_directory, _platform="linux")
    key_store.initialize()
    vps_identity = key_store.load()
    connector = DevicePrivateIdentity._from_seed_for_testing(bytes(range(32)))
    ca = ConnectorTLSStore(tmp_path / "connector", _platform="linux").initialize(
        connector, now=NOW
    )
    ids = _IdFactory()
    pairing_client = _PairingClient(connector, ca.der, ids, pending_polls=pending_polls)
    profile_store = VpsProfileStore(state_directory)
    displays: list[PairingDisplay] = []
    effective_scopes = (_scope(),) if requested_scopes is None else requested_scopes

    async def no_wait(_: float) -> None:
        return None

    orchestrator = VpsPairingOrchestrator(
        key_store,
        profile_store,
        client_factory=lambda endpoint: pairing_client,
        wall_clock=lambda: NOW,
        monotonic_clock=lambda: 10.0,
        sleep=no_wait,
        id_factory=ids,
        nonce_factory=lambda size: bytes([7]) * size,
    )
    profile = asyncio.run(
        orchestrator.pair(
            WssEndpoint.parse("wss://127.0.0.1:8765"),
            device_label="reach-vps-1",
            requested_scopes=effective_scopes,
            grant_expires_at=NOW + 3600,
            grant_max_uses=10,
            display=displays.append,
        )
    )
    return (
        profile,
        vps_identity,
        connector,
        profile_store,
        displays,
        pairing_client,
    )


def _assert_code(error: pytest.ExceptionInfo[ConnectorError], code: str) -> None:
    assert error.value.code == code


def test_pairing_persists_exact_records_displays_sas_and_commits_atomically(
    tmp_path: Path,
) -> None:
    profile, vps, connector, store, displays, pairing_client = _paired_fixture(
        tmp_path, pending_polls=1
    )

    assert store.load() == profile
    assert profile.vps_key_id == vps.public_identity.key_id
    assert profile.connector_identity == connector.public_identity
    assert profile.current_grant.claims.scopes == (_scope(),)
    assert len(displays) == 1
    assert displays[0].connector_fingerprint == connector.public_identity.fingerprint
    assert displays[0].scopes == (("web", "read.url", "public"),)
    assert displays[0].grant_expires_at == NOW + 3600
    assert displays[0].grant_max_uses == 10
    assert pairing_client.exchange_init is not None
    assert pairing_client.exchange_result is not None
    assert all(
        encode_record(value) == encode_record(pairing_client.exchange_init)
        for value in pairing_client.poll_inits
    )
    expected_sas = pairing_sas(
        pairing_transcript_hash(
            encode_record(pairing_client.exchange_init),
            encode_record(pairing_client.exchange_result.challenge),
            observed_tls_leaf_fingerprint=LEAF_FINGERPRINT,
        )
    )
    assert displays[0].sas == expected_sas

    path = tmp_path / "vps" / "vps-profile.json"
    raw = path.read_bytes()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert path.stat().st_nlink == 1
    assert raw == canonical_json_bytes(load_canonical_json(raw))
    for forbidden in (b"query", b"target", b"credential", b"provider", b"path"):
        assert forbidden not in raw.lower()


def test_profile_store_rejects_noncanonical_unknown_and_symlink_state(
    tmp_path: Path,
) -> None:
    profile, _, _, store, _, _ = _paired_fixture(tmp_path)
    assert store.load() == profile
    path = tmp_path / "vps" / "vps-profile.json"
    original = path.read_bytes()
    mapping = load_canonical_json(original)
    assert isinstance(mapping, dict)
    mapping["unexpected"] = True
    path.write_bytes(canonical_json_bytes(mapping))
    os.chmod(path, 0o600)
    with pytest.raises(ConnectorError) as unknown:
        store.load()
    _assert_code(unknown, ConnectorErrorCode.CONNECTOR_STATE_INVALID.value)

    path.unlink()
    target = tmp_path / "outside.json"
    target.write_bytes(original)
    path.symlink_to(target)
    with pytest.raises(ConnectorError) as symlink:
        store.load()
    _assert_code(symlink, ConnectorErrorCode.CONNECTOR_STATE_INVALID.value)


def test_pairing_resume_keeps_original_expiry_limit_and_exact_init(
    tmp_path: Path,
) -> None:
    state_directory = tmp_path / "vps"
    key_store = VpsKeyStore(state_directory, _platform="linux")
    key_store.initialize()
    connector = DevicePrivateIdentity._from_seed_for_testing(bytes(range(32)))
    ca = ConnectorTLSStore(tmp_path / "connector", _platform="linux").initialize(
        connector, now=NOW
    )
    ids = _IdFactory(2000)
    interrupted = _InterruptedPairingClient(connector, ca.der, ids)
    store = VpsProfileStore(state_directory)
    first = VpsPairingOrchestrator(
        key_store,
        store,
        client_factory=lambda endpoint: interrupted,
        wall_clock=lambda: NOW,
        monotonic_clock=lambda: 1.0,
        id_factory=ids,
        nonce_factory=lambda size: bytes([9]) * size,
    )
    endpoint = WssEndpoint.parse("wss://127.0.0.1:8765")
    with pytest.raises(ConnectorError) as caught:
        asyncio.run(
            first.pair(
                endpoint,
                device_label="resume-vps",
                requested_scopes=(_scope(),),
                grant_expires_at=NOW + 1800,
                grant_max_uses=7,
                display=lambda value: None,
            )
        )
    _assert_code(caught, ConnectorErrorCode.CONNECTOR_OFFLINE.value)
    pending_init = interrupted.exchange_init
    assert pending_init is not None

    resumed_client = _PairingClient(connector, ca.der, ids)
    displays: list[PairingDisplay] = []
    resumed = VpsPairingOrchestrator(
        key_store,
        store,
        client_factory=lambda value: resumed_client,
        wall_clock=lambda: NOW,
        monotonic_clock=lambda: 2.0,
        id_factory=ids,
    )
    profile = asyncio.run(
        resumed.pair(
            endpoint,
            device_label="resume-vps",
            requested_scopes=(_scope(),),
            grant_expires_at=NOW + 7200,
            grant_max_uses=99,
            display=displays.append,
        )
    )

    assert resumed_client.exchange_init is None
    assert encode_record(resumed_client.poll_inits[0]) == encode_record(pending_init)
    assert displays[0].grant_expires_at == NOW + 1800
    assert displays[0].grant_max_uses == 7
    assert profile.signed_grant.claims.expires_at == NOW + 1800
    assert profile.signed_grant.claims.max_uses == 7


def test_expired_pending_pairing_is_replaced_with_a_fresh_signed_init(
    tmp_path: Path,
) -> None:
    state_directory = tmp_path / "vps"
    key_store = VpsKeyStore(state_directory, _platform="linux")
    key_store.initialize()
    connector = DevicePrivateIdentity._from_seed_for_testing(bytes(range(32)))
    ca = ConnectorTLSStore(tmp_path / "connector", _platform="linux").initialize(
        connector, now=NOW
    )
    ids = _IdFactory(2400)
    interrupted = _InterruptedPairingClient(connector, ca.der, ids)
    store = VpsProfileStore(state_directory)
    first = VpsPairingOrchestrator(
        key_store,
        store,
        client_factory=lambda endpoint: interrupted,
        wall_clock=lambda: NOW,
        monotonic_clock=lambda: 1.0,
        id_factory=ids,
        nonce_factory=lambda size: bytes([10]) * size,
    )
    endpoint = WssEndpoint.parse("wss://127.0.0.1:8765")
    with pytest.raises(ConnectorError):
        asyncio.run(
            first.pair(
                endpoint,
                device_label="expired-vps",
                requested_scopes=(_scope(),),
                grant_expires_at=NOW + 1800,
                grant_max_uses=7,
                display=lambda value: None,
            )
        )
    expired_init = interrupted.exchange_init
    assert expired_init is not None

    replacement_client = _PairingClient(connector, ca.der, ids)
    replacement_now = NOW + PAIRING_TTL_SECONDS
    replacement = VpsPairingOrchestrator(
        key_store,
        store,
        client_factory=lambda value: replacement_client,
        wall_clock=lambda: replacement_now,
        monotonic_clock=lambda: 2.0,
        id_factory=ids,
        nonce_factory=lambda size: bytes([11]) * size,
    )
    profile = asyncio.run(
        replacement.pair(
            endpoint,
            device_label="expired-vps",
            requested_scopes=(_scope(),),
            grant_expires_at=replacement_now + 1800,
            grant_max_uses=7,
            display=lambda value: None,
        )
    )

    fresh_init = replacement_client.exchange_init
    assert fresh_init is not None
    assert fresh_init.pairing_id != expired_init.pairing_id
    assert fresh_init.vps_nonce != expired_init.vps_nonce
    assert fresh_init.issued_at == replacement_now
    assert profile.signed_grant.claims.expires_at == replacement_now + 1800


def test_invalid_pairing_display_input_fails_closed_before_transport(
    tmp_path: Path,
) -> None:
    state_directory = tmp_path / "vps"
    key_store = VpsKeyStore(state_directory, _platform="linux")
    key_store.initialize()
    transport_created = False

    def client_factory(endpoint: WssEndpoint) -> _PairingClient:
        nonlocal transport_created
        del endpoint
        transport_created = True
        raise AssertionError("invalid pairing must not construct transport")

    store = VpsProfileStore(state_directory)
    orchestrator = VpsPairingOrchestrator(
        key_store,
        store,
        client_factory=client_factory,
        wall_clock=lambda: NOW,
        id_factory=_IdFactory(2600),
    )

    with pytest.raises(ConnectorError) as caught:
        asyncio.run(
            orchestrator.pair(
                WssEndpoint.parse("wss://127.0.0.1:8765"),
                device_label="unsafe\nlabel",
                requested_scopes=(_scope(),),
                grant_expires_at=NOW + 1800,
                grant_max_uses=7,
                display=lambda value: None,
            )
        )

    _assert_code(caught, ConnectorErrorCode.CONNECTOR_STATE_INVALID.value)
    assert not transport_created
    assert store.load() is None


def test_connector_client_verifies_before_evidence_and_updates_snapshot(
    tmp_path: Path,
) -> None:
    profile, vps, connector, profile_store, _, _ = _paired_fixture(
        tmp_path,
        requested_scopes=(
            GrantScope("rss", "read.feed", "public"),
            _scope(),
        ),
    )
    receipt_path = tmp_path / "vps" / "receipts.jsonl"
    ledger = ReceiptEvidenceLedger(receipt_path, connector.public_identity, role="vps")
    snapshots = ConnectorSnapshotStore(tmp_path / "vps")
    transport = _ReceiptTransport(connector, _IdFactory(500))
    client = ConnectorClient(
        profile,
        vps,
        transport,
        ledger,
        snapshots,
        wall_clock=lambda: NOW + 2,
        monotonic_clock=lambda: 20.0,
        id_factory=_IdFactory(700),
    )
    call = validate_read(
        {
            "source": "web",
            "operation": "read.url",
            "target": {"url": f"https://example.com/private?{CANARY}"},
        }
    )

    response = asyncio.run(client.execute(call, trace_id=TRACE_ID))

    assert response.receipt.outcome == "ok"
    assert response.result is not None
    assert len(transport.requests) == 1
    assert len(ledger.records()) == 1
    snapshot = snapshots.load()
    assert snapshot is not None
    assert snapshot.state == "authenticated"
    assert snapshot.scopes == (("web", "read.url"),)
    availability = ConnectorAvailabilityResolver(
        profile_store, snapshots, clock=lambda: NOW + 3
    ).resolve("web", "read.url")
    assert availability.state == "available"
    assert availability.snapshot_at == NOW + 2
    other_availability = ConnectorAvailabilityResolver(
        profile_store, snapshots, clock=lambda: NOW + 3
    ).resolve("rss", "read.feed")
    assert other_availability.state == "degraded"
    assert other_availability.cause_code == ConnectorErrorCode.CONNECTOR_OFFLINE.value
    for path in (receipt_path, tmp_path / "vps" / "vps-connector-snapshot.json"):
        raw = path.read_bytes()
        assert CANARY.encode() not in raw
        assert b"https://" not in raw
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_connector_client_retries_not_sent_failure_with_identical_invocation(
    tmp_path: Path,
) -> None:
    profile, vps, connector, _, _, _ = _paired_fixture(tmp_path)
    transport = _RetryingReceiptTransport(connector, _IdFactory(750))
    client = ConnectorClient(
        profile,
        vps,
        transport,
        ReceiptEvidenceLedger(
            tmp_path / "vps" / "receipts.jsonl",
            connector.public_identity,
            role="vps",
        ),
        ConnectorSnapshotStore(tmp_path / "vps"),
        wall_clock=lambda: NOW + 2,
        monotonic_clock=lambda: 20.0,
        id_factory=_IdFactory(800),
    )
    call = validate_read(
        {
            "source": "web",
            "operation": "read.url",
            "target": {"url": "https://example.com/retry-canary"},
        }
    )

    response = asyncio.run(client.execute(call, trace_id=TRACE_ID))

    assert response.receipt.outcome == "ok"
    assert len(transport.attempts) == 2
    assert transport.attempts[0] is transport.attempts[1]
    assert encode_record(transport.attempts[0]) == encode_record(transport.attempts[1])
    assert transport.deadlines == [50.0, 50.0]
    request = transport.attempts[0].signed_request
    assert request.trace_id == TRACE_ID
    assert request.request_id == transport.attempts[1].signed_request.request_id


def test_connector_client_records_signed_replay_after_ambiguous_response_loss(
    tmp_path: Path,
) -> None:
    profile, vps, connector, _, _, _ = _paired_fixture(tmp_path)
    transport = _LostResponseTransport(connector, _IdFactory(850))
    ledger = ReceiptEvidenceLedger(
        tmp_path / "vps" / "receipts.jsonl",
        connector.public_identity,
        role="vps",
    )
    snapshots = ConnectorSnapshotStore(tmp_path / "vps")
    client = ConnectorClient(
        profile,
        vps,
        transport,
        ledger,
        snapshots,
        wall_clock=lambda: NOW + 2,
        monotonic_clock=lambda: 20.0,
        id_factory=_IdFactory(900),
    )
    call = validate_read(
        {
            "source": "web",
            "operation": "read.url",
            "target": {"url": "https://example.com/lost-response-canary"},
        }
    )

    with pytest.raises(ConnectorError) as replayed:
        asyncio.run(client.execute(call, trace_id=TRACE_ID))

    _assert_code(replayed, ConnectorErrorCode.REQUEST_REPLAYED.value)
    assert len(transport.attempts) == 2
    assert transport.attempts[0] is transport.attempts[1]
    assert encode_record(transport.attempts[0]) == encode_record(transport.attempts[1])
    assert transport.deadlines == [50.0, 50.0]
    assert len(ledger.records()) == 1
    snapshot = snapshots.load()
    assert snapshot is not None
    assert snapshot.state == "disconnected"
    assert snapshot.cause_code is ConnectorErrorCode.REQUEST_REPLAYED
    assert snapshot.scopes == (("web", "read.url"),)
    replay_availability = ConnectorAvailabilityResolver(
        VpsProfileStore(tmp_path / "vps"), snapshots, clock=lambda: NOW + 3
    ).resolve("web", "read.url")
    assert replay_availability.state == "degraded"
    assert replay_availability.cause_code == ConnectorErrorCode.CONNECTOR_OFFLINE.value

    recovered = asyncio.run(client.execute(call, trace_id="2" * 32))

    assert recovered.result is not None
    assert recovered.result.items[0].text == "recovered new request"
    assert len(transport.attempts) == 3
    assert transport.attempts[2] is not transport.attempts[0]
    assert (
        transport.attempts[2].signed_request.request_id
        != transport.attempts[0].signed_request.request_id
    )
    assert len(ledger.records()) == 2
    availability = ConnectorAvailabilityResolver(
        VpsProfileStore(tmp_path / "vps"), snapshots, clock=lambda: NOW + 3
    ).resolve("web", "read.url")
    assert availability.state == "available"


@pytest.mark.parametrize(
    ("failure_class", "code"),
    (
        ("authority", ConnectorErrorCode.BACKEND_UNBOUND),
        ("secret", ConnectorErrorCode.SECRET_UNAVAILABLE),
        ("transport", ConnectorErrorCode.CONNECTOR_DEADLINE_EXCEEDED),
    ),
)
def test_accepted_remediable_failure_blocks_exact_operation_until_snapshot_ttl(
    tmp_path: Path, failure_class: str, code: ConnectorErrorCode
) -> None:
    profile, vps, connector, profile_store, _, _ = _paired_fixture(
        tmp_path,
        requested_scopes=(
            GrantScope("rss", "read.feed", "public"),
            _scope(),
        ),
    )
    snapshots = ConnectorSnapshotStore(tmp_path / "vps")
    transport = _AcceptedFailureTransport(
        connector,
        _IdFactory(925),
        ReceiptFailure(failure_class, code),
    )
    client = ConnectorClient(
        profile,
        vps,
        transport,
        ReceiptEvidenceLedger(
            tmp_path / "vps" / "receipts.jsonl",
            connector.public_identity,
            role="vps",
        ),
        snapshots,
        wall_clock=lambda: NOW + 2,
        monotonic_clock=lambda: 20.0,
        id_factory=_IdFactory(940),
    )

    with pytest.raises(ConnectorError) as caught:
        asyncio.run(
            client.execute(
                validate_read(
                    {
                        "source": "web",
                        "operation": "read.url",
                        "target": {"url": "https://example.com/unbound"},
                    }
                ),
                trace_id=TRACE_ID,
            )
        )

    _assert_code(caught, code.value)
    assert transport.calls == 1
    snapshot = snapshots.load()
    assert snapshot is not None
    assert snapshot.state == "unavailable"
    assert snapshot.cause_code is code
    assert snapshot.scopes == (("web", "read.url"),)
    availability = ConnectorAvailabilityResolver(
        profile_store, snapshots, clock=lambda: NOW + 3
    ).resolve("web", "read.url")
    assert availability.state == "unavailable"
    assert availability.cause_code == code.value
    other_availability = ConnectorAvailabilityResolver(
        profile_store, snapshots, clock=lambda: NOW + 3
    ).resolve("rss", "read.feed")
    assert other_availability.state == "degraded"

    retry_availability = ConnectorAvailabilityResolver(
        profile_store, snapshots, clock=lambda: NOW + 63
    ).resolve("web", "read.url")
    assert retry_availability.state == "degraded"
    assert retry_availability.cause_code == ConnectorErrorCode.CONNECTOR_OFFLINE.value


@pytest.mark.parametrize(
    ("code", "snapshot_state", "availability_state"),
    (
        (ConnectorErrorCode.BACKEND_INVALID_INPUT, "authenticated", "available"),
        (ConnectorErrorCode.BACKEND_NOT_FOUND, "authenticated", "available"),
        (ConnectorErrorCode.BACKEND_UNAVAILABLE, "disconnected", "degraded"),
        (ConnectorErrorCode.BACKEND_DEADLINE_EXCEEDED, "disconnected", "degraded"),
        (ConnectorErrorCode.BACKEND_RATE_LIMITED, "disconnected", "degraded"),
        (ConnectorErrorCode.BACKEND_TRANSIENT, "disconnected", "degraded"),
        (
            ConnectorErrorCode.BACKEND_AUTHENTICATION_REQUIRED,
            "unavailable",
            "unavailable",
        ),
        (ConnectorErrorCode.BACKEND_INCOMPATIBLE, "unavailable", "unavailable"),
        (
            ConnectorErrorCode.BACKEND_CONTRACT_VIOLATION,
            "unavailable",
            "unavailable",
        ),
    ),
)
def test_backend_failure_snapshot_distinguishes_request_and_capability_state(
    tmp_path: Path,
    code: ConnectorErrorCode,
    snapshot_state: str,
    availability_state: str,
) -> None:
    profile, vps, connector, profile_store, _, _ = _paired_fixture(tmp_path)
    snapshots = ConnectorSnapshotStore(tmp_path / "vps")
    client = ConnectorClient(
        profile,
        vps,
        _AcceptedFailureTransport(
            connector,
            _IdFactory(955),
            ReceiptFailure("backend", code),
        ),
        ReceiptEvidenceLedger(
            tmp_path / "vps" / "receipts.jsonl",
            connector.public_identity,
            role="vps",
        ),
        snapshots,
        wall_clock=lambda: NOW + 2,
        monotonic_clock=lambda: 20.0,
        id_factory=_IdFactory(970),
    )
    call = validate_read(
        {
            "source": "web",
            "operation": "read.url",
            "target": {"url": "https://example.com/backend-state"},
        }
    )

    with pytest.raises(ConnectorError) as caught:
        asyncio.run(client.execute(call, trace_id=TRACE_ID))

    _assert_code(caught, code.value)
    snapshot = snapshots.load()
    assert snapshot is not None
    assert snapshot.state == snapshot_state
    if snapshot_state == "authenticated":
        assert snapshot.cause_code is None
    else:
        assert snapshot.cause_code is code
    availability = ConnectorAvailabilityResolver(
        profile_store, snapshots, clock=lambda: NOW + 3
    ).resolve("web", "read.url")
    assert availability.state == availability_state
    if availability_state == "available":
        assert availability.cause_code is None
    elif availability_state == "degraded":
        assert availability.cause_code == ConnectorErrorCode.CONNECTOR_OFFLINE.value
    else:
        assert availability.cause_code == code.value


@pytest.mark.parametrize(
    "code",
    (
        ConnectorErrorCode.CONNECTOR_TLS_FAILED,
        ConnectorErrorCode.CONNECTOR_PROTOCOL_MISMATCH,
    ),
)
def test_connector_client_never_retries_tls_or_protocol_failure(
    tmp_path: Path, code: ConnectorErrorCode
) -> None:
    profile, vps, connector, _, _, _ = _paired_fixture(tmp_path)
    transport = _ClosedErrorTransport(code)
    client = ConnectorClient(
        profile,
        vps,
        transport,
        ReceiptEvidenceLedger(
            tmp_path / "vps" / "receipts.jsonl",
            connector.public_identity,
            role="vps",
        ),
        ConnectorSnapshotStore(tmp_path / "vps"),
        wall_clock=lambda: NOW + 2,
        monotonic_clock=lambda: 20.0,
        id_factory=_IdFactory(950),
    )

    with pytest.raises(ConnectorError) as caught:
        asyncio.run(
            client.execute(
                validate_read(
                    {
                        "source": "web",
                        "operation": "read.url",
                        "target": {"url": "https://example.com/no-retry"},
                    }
                ),
                trace_id=TRACE_ID,
            )
        )

    _assert_code(caught, code.value)
    assert transport.calls == 1


def test_connector_client_does_not_retry_after_original_deadline_expires(
    tmp_path: Path,
) -> None:
    profile, vps, connector, _, _, _ = _paired_fixture(tmp_path)
    transport = _DeadlineExhaustingTransport()
    monotonic_times = (20.0, 50.0)
    monotonic_reads = 0

    def monotonic_clock() -> float:
        nonlocal monotonic_reads
        value = monotonic_times[min(monotonic_reads, len(monotonic_times) - 1)]
        monotonic_reads += 1
        return value

    client = ConnectorClient(
        profile,
        vps,
        transport,
        ReceiptEvidenceLedger(
            tmp_path / "vps" / "receipts.jsonl",
            connector.public_identity,
            role="vps",
        ),
        ConnectorSnapshotStore(tmp_path / "vps"),
        wall_clock=lambda: NOW + 2,
        monotonic_clock=monotonic_clock,
        id_factory=_IdFactory(975),
    )

    with pytest.raises(ConnectorError) as caught:
        asyncio.run(
            client.execute(
                validate_read(
                    {
                        "source": "web",
                        "operation": "read.url",
                        "target": {"url": "https://example.com/deadline"},
                    }
                ),
                trace_id=TRACE_ID,
            )
        )

    _assert_code(caught, ConnectorErrorCode.CONNECTOR_DEADLINE_EXCEEDED.value)
    assert transport.calls == 1


def test_tampered_receipt_creates_no_evidence_or_snapshot(tmp_path: Path) -> None:
    profile, vps, connector, _, _, _ = _paired_fixture(tmp_path)
    ledger = ReceiptEvidenceLedger(
        tmp_path / "vps" / "receipts.jsonl",
        connector.public_identity,
        role="vps",
    )
    snapshots = ConnectorSnapshotStore(tmp_path / "vps")
    transport = _ReceiptTransport(connector, _IdFactory(900), tamper=True)
    client = ConnectorClient(
        profile,
        vps,
        transport,
        ledger,
        snapshots,
        wall_clock=lambda: NOW + 2,
        id_factory=_IdFactory(1000),
    )

    with pytest.raises(ConnectorError) as caught:
        asyncio.run(
            client.execute(
                validate_read(
                    {
                        "source": "web",
                        "operation": "read.url",
                        "target": {"url": "https://example.com/article"},
                    }
                ),
                trace_id=TRACE_ID,
            )
        )
    _assert_code(caught, ConnectorErrorCode.RECEIPT_INVALID.value)
    assert len(transport.requests) == 1
    assert ledger.records() == ()
    assert snapshots.load() is None


def test_tampered_result_creates_no_evidence_or_snapshot(
    tmp_path: Path,
) -> None:
    profile, vps, connector, _, _, _ = _paired_fixture(tmp_path)
    ledger = ReceiptEvidenceLedger(
        tmp_path / "vps" / "receipts.jsonl",
        connector.public_identity,
        role="vps",
    )
    snapshots = ConnectorSnapshotStore(tmp_path / "vps")
    client = ConnectorClient(
        profile,
        vps,
        _ResultTamperingTransport(connector, _IdFactory(920)),
        ledger,
        snapshots,
        wall_clock=lambda: NOW + 2,
        id_factory=_IdFactory(1020),
    )
    call = validate_read(
        {
            "source": "web",
            "operation": "read.url",
            "target": {"url": "https://example.com/article"},
        }
    )

    with pytest.raises(ConnectorError) as caught:
        asyncio.run(client.execute(call, trace_id=TRACE_ID))

    _assert_code(caught, ConnectorErrorCode.RECEIPT_INVALID.value)
    assert ledger.records() == ()
    assert snapshots.load() is None


def test_evidence_failure_and_snapshot_failure_stay_closed(tmp_path: Path) -> None:
    profile, vps, connector, _, _, _ = _paired_fixture(tmp_path)
    receipt_path = tmp_path / "vps" / "receipts.jsonl"
    receipt_path.write_bytes(b"malformed\n")
    os.chmod(receipt_path, 0o600)
    snapshot_path = tmp_path / "vps" / "vps-connector-snapshot.json"
    snapshot_path.symlink_to(tmp_path / "outside-snapshot.json")
    client = ConnectorClient(
        profile,
        vps,
        _ReceiptTransport(connector, _IdFactory(1100)),
        ReceiptEvidenceLedger(
            receipt_path,
            connector.public_identity,
            role="vps",
        ),
        ConnectorSnapshotStore(tmp_path / "vps"),
        wall_clock=lambda: NOW + 2,
        id_factory=_IdFactory(1150),
    )
    call = validate_read(
        {
            "source": "web",
            "operation": "read.url",
            "target": {"url": "https://example.com/article"},
        }
    )

    with pytest.raises(ConnectorError) as caught:
        asyncio.run(client.execute(call, trace_id=TRACE_ID))
    _assert_code(caught, ConnectorErrorCode.CONNECTOR_STATE_INVALID.value)


def test_offline_dispatch_overrides_cached_health_with_degraded_snapshot(
    tmp_path: Path,
) -> None:
    profile, vps, connector, profile_store, _, _ = _paired_fixture(tmp_path)
    snapshots = ConnectorSnapshotStore(tmp_path / "vps")
    ledger = ReceiptEvidenceLedger(
        tmp_path / "vps" / "receipts.jsonl",
        connector.public_identity,
        role="vps",
    )
    success_client = ConnectorClient(
        profile,
        vps,
        _ReceiptTransport(connector, _IdFactory(1200)),
        ledger,
        snapshots,
        wall_clock=lambda: NOW + 2,
        id_factory=_IdFactory(1300),
    )
    call = validate_read(
        {
            "source": "web",
            "operation": "read.url",
            "target": {"url": "https://example.com/article"},
        }
    )
    asyncio.run(success_client.execute(call, trace_id=TRACE_ID))
    assert (
        ConnectorAvailabilityResolver(profile_store, snapshots, clock=lambda: NOW + 3)
        .resolve("web", "read.url")
        .state
        == "available"
    )

    offline = _OfflineTransport()
    offline_client = ConnectorClient(
        profile,
        vps,
        offline,
        ledger,
        snapshots,
        wall_clock=lambda: NOW + 4,
        id_factory=_IdFactory(1400),
    )
    with pytest.raises(ConnectorError) as caught:
        asyncio.run(offline_client.execute(call, trace_id="2" * 32))
    _assert_code(caught, ConnectorErrorCode.CONNECTOR_OFFLINE.value)
    assert offline.calls == 2
    availability = ConnectorAvailabilityResolver(
        profile_store, snapshots, clock=lambda: NOW + 4
    ).resolve("web", "read.url")
    assert availability.state == "degraded"
    assert availability.cause_code == ConnectorErrorCode.CONNECTOR_OFFLINE.value


def test_unsigned_error_frame_is_never_trusted_as_authority(tmp_path: Path) -> None:
    profile, vps, connector, _, _, _ = _paired_fixture(tmp_path)
    snapshots = ConnectorSnapshotStore(tmp_path / "vps")
    client = ConnectorClient(
        profile,
        vps,
        _UnsignedTransport(),
        ReceiptEvidenceLedger(
            tmp_path / "vps" / "receipts.jsonl",
            connector.public_identity,
            role="vps",
        ),
        snapshots,
        wall_clock=lambda: NOW + 2,
        id_factory=_IdFactory(2100),
    )
    call = validate_read(
        {
            "source": "web",
            "operation": "read.url",
            "target": {"url": "https://example.com/article"},
        }
    )

    with pytest.raises(ConnectorError) as caught:
        asyncio.run(client.execute(call, trace_id=TRACE_ID))
    _assert_code(caught, ConnectorErrorCode.CONNECTOR_PROTOCOL_MISMATCH.value)
    snapshot = snapshots.load()
    assert snapshot is not None
    assert snapshot.state == "unavailable"
    assert snapshot.cause_code is ConnectorErrorCode.CONNECTOR_PROTOCOL_MISMATCH


def test_local_grant_scope_denial_happens_before_transport(tmp_path: Path) -> None:
    profile, vps, connector, _, _, _ = _paired_fixture(tmp_path)
    transport = _OfflineTransport()
    client = ConnectorClient(
        profile,
        vps,
        transport,
        ReceiptEvidenceLedger(
            tmp_path / "vps" / "receipts.jsonl",
            connector.public_identity,
            role="vps",
        ),
        ConnectorSnapshotStore(tmp_path / "vps"),
        wall_clock=lambda: NOW + 2,
        id_factory=_IdFactory(1500),
    )
    call = validate_read(
        {
            "source": "v2ex",
            "operation": "read.topic",
            "target": {"native_id": "123"},
        }
    )

    with pytest.raises(ConnectorError) as caught:
        asyncio.run(client.execute(call, trace_id=TRACE_ID))
    _assert_code(caught, ConnectorErrorCode.GRANT_SCOPE_DENIED.value)
    assert transport.calls == 0


def test_availability_snapshot_is_local_and_expires_after_sixty_seconds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile, _, _, profile_store, _, _ = _paired_fixture(tmp_path)
    snapshots = ConnectorSnapshotStore(tmp_path / "vps")
    snapshots.save(
        ConnectorSnapshot(
            connector_key_id=profile.connector_identity.key_id,
            grant_id=profile.signed_grant.claims.grant_id,
            grant_revision=profile.signed_grant.claims.revision,
            observed_at=NOW,
            state="authenticated",
            cause_code=None,
            scopes=(("web", "read.url"),),
        )
    )

    def forbidden_network(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise AssertionError("status must not use network")

    monkeypatch.setattr("socket.socket", forbidden_network)
    resolver = ConnectorAvailabilityResolver(
        profile_store, snapshots, clock=lambda: NOW + 61
    )
    availability = resolver.resolve("web", "read.url")
    assert availability.state == "degraded"
    assert availability.cause_code == ConnectorErrorCode.CONNECTOR_OFFLINE.value
    assert availability.snapshot_at == NOW


def test_availability_rejects_failed_snapshot_beyond_clock_skew(
    tmp_path: Path,
) -> None:
    profile, _, _, profile_store, _, _ = _paired_fixture(tmp_path)
    snapshots = ConnectorSnapshotStore(tmp_path / "vps")
    observed_at = NOW + MAX_CLOCK_SKEW_SECONDS + 1
    snapshots.save(
        ConnectorSnapshot(
            connector_key_id=profile.connector_identity.key_id,
            grant_id=profile.signed_grant.claims.grant_id,
            grant_revision=profile.signed_grant.claims.revision,
            observed_at=observed_at,
            state="unavailable",
            cause_code=ConnectorErrorCode.BACKEND_UNBOUND,
            scopes=(("web", "read.url"),),
        )
    )

    availability = ConnectorAvailabilityResolver(
        profile_store, snapshots, clock=lambda: NOW
    ).resolve("web", "read.url")

    assert availability.state == "unavailable"
    assert (
        availability.cause_code
        == ConnectorErrorCode.CONNECTOR_SCHEMA_INCOMPATIBLE.value
    )
    assert availability.snapshot_at == observed_at


def test_snapshot_rejects_hardlink_and_resolver_closes_malformed_state(
    tmp_path: Path,
) -> None:
    profile, _, _, profile_store, _, _ = _paired_fixture(tmp_path)
    snapshots = ConnectorSnapshotStore(tmp_path / "vps")
    snapshot = ConnectorSnapshot(
        connector_key_id=profile.connector_identity.key_id,
        grant_id=profile.signed_grant.claims.grant_id,
        grant_revision=profile.signed_grant.claims.revision,
        observed_at=NOW,
        state="authenticated",
        cause_code=None,
        scopes=(("web", "read.url"),),
    )
    snapshots.save(snapshot)
    snapshot_path = tmp_path / "vps" / "vps-connector-snapshot.json"
    newer_mapping = load_canonical_json(snapshot_path.read_bytes())
    assert isinstance(newer_mapping, dict)
    os.link(snapshot_path, tmp_path / "snapshot-hardlink.json")

    with pytest.raises(ConnectorError) as hardlink:
        snapshots.save(snapshot)
    _assert_code(hardlink, ConnectorErrorCode.CONNECTOR_STATE_INVALID.value)

    snapshot_path.unlink()
    newer_mapping["version"] = 2
    snapshot_path.write_bytes(canonical_json_bytes(newer_mapping))
    os.chmod(snapshot_path, 0o600)
    availability = ConnectorAvailabilityResolver(
        profile_store, snapshots, clock=lambda: NOW
    ).resolve("web", "read.url")
    assert availability.state == "unavailable"
    assert (
        availability.cause_code
        == ConnectorErrorCode.CONNECTOR_SCHEMA_INCOMPATIBLE.value
    )


def test_public_client_module_does_not_register_or_construct_live_binding() -> None:
    import hermes_reach.connector.client as client_module

    assert not hasattr(client_module, "AdapterBinding")
    assert not hasattr(client_module, "build_alpha1_registry")
    assert PairingWssClient.__module__ == "hermes_reach.connector.transport"
    assert _endpoint_digest(WssEndpoint.parse("wss://127.0.0.1:8765")) == (
        "f0e87970665f19d854ff87948fba341092d57c481ad74958bd270bf917ac4bad"
    )
