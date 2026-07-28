from __future__ import annotations

import asyncio
import base64
import time
from pathlib import Path

import pytest

from hermes_reach import bootstrap
from hermes_reach.bootstrap import (
    DEFAULT_RUNTIME,
    VPS_STATE_DIRECTORY_ENVIRONMENT,
    build_vps_runtime,
    runtime_from_environment,
)
from hermes_reach.connector import client as connector_client
from hermes_reach.connector import identity as connector_identity
from hermes_reach.connector.client import VpsPairingOrchestrator, VpsProfileStore
from hermes_reach.connector.identity import DevicePrivateIdentity, VpsKeyStore
from hermes_reach.connector.protocol import (
    GrantClaims,
    GrantScope,
    PairingChallenge,
    PairingInit,
    PairingResolution,
    create_pairing_challenge,
    create_pairing_complete,
    create_pairing_resolution,
    create_signed_grant,
    encode_record,
    pairing_transcript_hash,
    record_digest,
)
from hermes_reach.connector.tls import ConnectorTLSStore, verify_connector_ca_der
from hermes_reach.connector.transport import PairingExchange, WssEndpoint

_LEAF_FINGERPRINT = "ab" * 32


def _id(value: int) -> str:
    return base64.b32encode(value.to_bytes(16, "big")).decode().rstrip("=").lower()


class _Ids:
    def __init__(self) -> None:
        self._value = 90_000

    def __call__(self) -> str:
        self._value += 1
        return _id(self._value)


class _PairingClient:
    def __init__(
        self,
        connector: DevicePrivateIdentity,
        ca_der: bytes,
        ids: _Ids,
    ) -> None:
        self._connector = connector
        self._ca_der = ca_der
        self._ids = ids

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
            tls_leaf_fingerprint=_LEAF_FINGERPRINT,
            issued_at=pairing_init.issued_at,
            deadline=pairing_init.deadline,
        )
        return PairingExchange(
            challenge,
            verify_connector_ca_der(
                self._ca_der,
                self._connector.public_identity,
                now=pairing_init.issued_at,
            ),
            _LEAF_FINGERPRINT,
        )

    async def poll(
        self,
        pairing_init: PairingInit,
        exchange: PairingExchange,
        *,
        deadline: float,
    ) -> PairingResolution | None:
        assert deadline > 0
        challenge = exchange.challenge
        assert isinstance(challenge, PairingChallenge)
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
            encode_record(challenge),
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


def _paired_state(tmp_path: Path, scopes: tuple[GrantScope, ...]) -> Path:
    now = int(time.time())
    state_directory = tmp_path / "vps"
    key_store = VpsKeyStore(state_directory, _platform="linux")
    key_store.initialize()
    connector = DevicePrivateIdentity._from_seed_for_testing(bytes(range(32)))
    ca = ConnectorTLSStore(tmp_path / "connector", _platform="linux").initialize(
        connector, now=now
    )
    ids = _Ids()
    pairing_client = _PairingClient(connector, ca.der, ids)
    orchestrator = VpsPairingOrchestrator(
        key_store,
        VpsProfileStore(state_directory),
        client_factory=lambda endpoint: pairing_client,
        wall_clock=lambda: now,
        monotonic_clock=lambda: 10.0,
        sleep=lambda delay: asyncio.sleep(0),
        id_factory=ids,
        nonce_factory=lambda size: bytes([7]) * size,
    )
    asyncio.run(
        orchestrator.pair(
            WssEndpoint.parse("wss://127.0.0.1:8765"),
            device_label="activation-vps",
            requested_scopes=scopes,
            grant_expires_at=now + 3_600,
            grant_max_uses=10,
            display=lambda value: None,
        )
    )
    return state_directory


def test_absent_environment_pointer_returns_default_without_factory_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(_: Path) -> object:
        raise AssertionError("absent configuration constructed Connector state")

    monkeypatch.setattr(bootstrap, "build_vps_runtime", forbidden)

    assert runtime_from_environment({}) is DEFAULT_RUNTIME


def test_invalid_configured_state_preserves_alpha1_and_creates_nothing(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "missing"

    runtime = runtime_from_environment({VPS_STATE_DIRECTORY_ENVIRONMENT: str(missing)})

    assert runtime.operation_availability("rss", "read.feed").state == "available"
    assert runtime.operation_availability("reddit", "read.post").state == "unavailable"
    assert not missing.exists()


@pytest.mark.parametrize("state", ["unpaired", "malformed"])
def test_unpaired_or_malformed_configured_state_closes_only_reddit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, state: str
) -> None:
    state_directory = tmp_path / state
    VpsKeyStore(state_directory, _platform="linux").initialize()
    if state == "malformed":
        profile = state_directory / "vps-profile.json"
        profile.write_text('{"state":"paired"}', encoding="utf-8")
        profile.chmod(0o600)

    monkeypatch.setattr(connector_identity.sys, "platform", "linux")
    runtime = build_vps_runtime(state_directory)

    assert runtime.operation_availability("rss", "read.feed").state == "available"
    assert runtime.operation_availability("reddit", "read.post").state == "unavailable"
    assert not (state_directory / "receipts.jsonl").exists()
    assert not (state_directory / "vps-connector-snapshot.json").exists()


def test_wrong_vps_key_closes_only_reddit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    state_directory = _paired_state(
        tmp_path, (GrantScope("reddit", "read.post", "public"),)
    )
    replacement_directory = tmp_path / "replacement"
    VpsKeyStore(replacement_directory, _platform="linux").initialize()
    (state_directory / "vps-identity.pem").write_bytes(
        (replacement_directory / "vps-identity.pem").read_bytes()
    )

    monkeypatch.setattr(connector_identity.sys, "platform", "linux")
    runtime = build_vps_runtime(state_directory)

    assert runtime.operation_availability("rss", "read.feed").state == "available"
    assert runtime.operation_availability("reddit", "read.post").state == "unavailable"
    assert not (state_directory / "receipts.jsonl").exists()
    assert not (state_directory / "vps-connector-snapshot.json").exists()


def test_expired_grant_closes_only_reddit_without_startup_dial(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    state_directory = _paired_state(
        tmp_path, (GrantScope("reddit", "read.post", "public"),)
    )
    profile = VpsProfileStore(state_directory).load()
    assert profile is not None
    expires_at = profile.current_grant.claims.expires_at

    monkeypatch.setattr(connector_identity.sys, "platform", "linux")
    monkeypatch.setattr(connector_client.time, "time", lambda: expires_at + 1)
    runtime = build_vps_runtime(state_directory)

    assert runtime.operation_availability("rss", "read.feed").state == "available"
    reddit = runtime.operation_availability("reddit", "read.post")
    assert reddit.state == "unavailable"
    assert reddit.cause_code == "grant_expired"
    assert not (state_directory / "receipts.jsonl").exists()
    assert not (state_directory / "vps-connector-snapshot.json").exists()


def test_verified_reddit_pairing_builds_one_degraded_connector_adapter(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    state_directory = _paired_state(
        tmp_path, (GrantScope("reddit", "read.post", "public"),)
    )

    monkeypatch.setattr(connector_identity.sys, "platform", "linux")
    runtime = build_vps_runtime(state_directory)

    assert runtime.operation_availability("rss", "read.feed").state == "available"
    reddit = runtime.operation_availability("reddit", "read.post")
    assert reddit.state == "degraded"
    assert reddit.cause_code == "connector_offline"
    assert not (state_directory / "receipts.jsonl").exists()
    assert not (state_directory / "vps-connector-snapshot.json").exists()


def test_verified_wrong_scope_pairing_is_setup_required_without_startup_dial(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    state_directory = _paired_state(
        tmp_path, (GrantScope("web", "read.url", "public"),)
    )

    monkeypatch.setattr(connector_identity.sys, "platform", "linux")
    runtime = build_vps_runtime(state_directory)

    reddit = runtime.operation_availability("reddit", "read.post")
    assert reddit.state == "setup_required"
    assert reddit.cause_code == "grant_scope_denied"
    assert not (state_directory / "receipts.jsonl").exists()
    assert not (state_directory / "vps-connector-snapshot.json").exists()
