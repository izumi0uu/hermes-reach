from __future__ import annotations

import asyncio
import base64
import errno
import hashlib
import io
import socket
import stat
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import pytest

from hermes_reach.connector.audit import ReceiptEvidenceLedger
from hermes_reach.connector.authority import AuthorizedExecution, GrantAuthority
from hermes_reach.connector.client import (
    ConnectorClient,
    ConnectorSnapshotStore,
    PairedVpsProfile,
)
from hermes_reach.connector.execution import (
    ConnectorExecutionComposition,
    ConnectorExecutorBinding,
)
from hermes_reach.connector.identity import (
    ConnectorKeyStore,
    DevicePrivateIdentity,
    TtyPassphraseReader,
)
from hermes_reach.connector.media_policy import ModelPolicy
from hermes_reach.connector.protocol import (
    GrantClaims,
    GrantScope,
    OperationResultItemV1,
    OperationResultV1,
    PairingResolution,
    PublicBackendIdentity,
    create_pairing_challenge,
    create_pairing_complete,
    create_pairing_init,
    create_pairing_resolution,
    create_signed_grant,
    encode_record,
    pairing_transcript_hash,
    record_digest,
)
from hermes_reach.connector.service import ConnectorService
from hermes_reach.connector.store import AuthorityStore, StoreWriterLease
from hermes_reach.connector.tls import ConnectorTLSStore
from hermes_reach.connector.transport import PinnedWssClient
from hermes_reach.contracts import OperationCall, validate_read
from hermes_reach.runtime.adapters import AdapterRegistry
from hermes_reach.runtime.availability import AvailabilityRecord
from hermes_reach.runtime.dispatcher import RuntimeDispatcher
from hermes_reach.sources.connector import connector_bindings

PASSPHRASE = "bridge-e2e-passphrase"
SCOPE = GrantScope("web", "read.url", "public")
BACKEND = PublicBackendIdentity("reach-bounded-executor-v1", "1")
CANARY = "BRIDGE_QUERY_CANARY"


def _id(value: int) -> str:
    return base64.b32encode(value.to_bytes(16, "big")).decode().rstrip("=").lower()


class _Ids:
    def __init__(self, value: int = 10_000) -> None:
        self._value = value

    def __call__(self) -> str:
        self._value += 1
        return _id(self._value)


class _Terminal(io.TextIOBase):
    def isatty(self) -> bool:
        return True


def _reader(*passphrases: str) -> TtyPassphraseReader:
    values = iter(passphrases)

    def prompt(*, prompt: str, stream: object) -> str:
        del prompt, stream
        return next(values)

    return TtyPassphraseReader._from_test_terminal(_Terminal(), prompt=prompt)


class _FixtureExecutor:
    def __init__(self) -> None:
        self.calls = 0

    async def execute(
        self,
        execution: AuthorizedExecution,
        environment: Mapping[str, str],
    ) -> OperationResultV1:
        self.calls += 1
        assert execution.operation_call().source.name == "web"
        assert dict(environment) == {}
        return OperationResultV1(
            (OperationResultItemV1("content", "real WSS fixture result"),), False
        )


class _BlockingExecutor:
    def __init__(self) -> None:
        self.calls = 0
        self.cleanup_calls = 0
        self.started = asyncio.Event()
        self.cleaned = asyncio.Event()

    async def execute(
        self,
        execution: AuthorizedExecution,
        environment: Mapping[str, str],
    ) -> OperationResultV1:
        del execution, environment
        self.calls += 1
        self.started.set()
        await asyncio.Event().wait()
        return OperationResultV1((), False)

    async def cleanup(self) -> None:
        self.cleanup_calls += 1
        self.cleaned.set()


@dataclass
class _BridgeHarness:
    service: ConnectorService
    client: ConnectorClient
    store: AuthorityStore
    lease: StoreWriterLease
    receipt_ledger: ReceiptEvidenceLedger
    snapshot_store: ConnectorSnapshotStore
    vps_state: Path


async def _open_bridge(
    tmp_path: Path, executor: _FixtureExecutor | _BlockingExecutor
) -> _BridgeHarness:
    now = int(time.time())
    ids = _Ids()
    trusted_state = tmp_path / "connector"
    vps_state = tmp_path / "vps"
    vps_state.mkdir(mode=0o700)
    key_store = ConnectorKeyStore(trusted_state, _platform="linux")
    connector_public = key_store._initialize_from_tty_for_testing(_reader(PASSPHRASE))
    signer = key_store._unlock_from_tty_for_testing(_reader(PASSPHRASE))
    tls_store = ConnectorTLSStore(trusted_state, _platform="linux")
    certificate = tls_store.initialize(signer, now=now)
    lease = StoreWriterLease(trusted_state)
    store = AuthorityStore.initialize(
        trusted_state,
        connector_public,
        lease,
        initial_policy_digest=ModelPolicy.default_deny(1).digest(),
        now=now,
    )
    vps = DevicePrivateIdentity._from_seed_for_testing(bytes([31]) * 32)
    pairing = create_pairing_init(
        vps,
        message_id=ids(),
        pairing_id=ids(),
        device_label="bridge-e2e-vps",
        endpoint_digest=hashlib.sha256(b"loopback-bridge").hexdigest(),
        vps_nonce=bytes(range(32)),
        requested_scopes=(SCOPE,),
        grant_expires_at=now + 3_600,
        grant_max_uses=4,
        issued_at=now,
        deadline=now + 300,
    )
    leaf_fingerprint = "ab" * 32
    challenge = create_pairing_challenge(
        signer,
        message_id=ids(),
        pairing_id=pairing.pairing_id,
        init_digest=record_digest(pairing),
        vps_key_id=pairing.vps_key_id,
        connector_nonce=bytes(range(32, 64)),
        tls_ca_der=certificate.der,
        tls_leaf_fingerprint=leaf_fingerprint,
        issued_at=now,
        deadline=pairing.deadline,
    )
    grant = create_signed_grant(
        signer,
        message_id=ids(),
        claims=GrantClaims(
            grant_id=ids(),
            revision=1,
            issuer_key_id=connector_public.key_id,
            subject_key_id=vps.public_identity.key_id,
            issued_at=now,
            not_before=now,
            expires_at=now + 3_600,
            policy_revision=1,
            max_uses=4,
            scopes=(SCOPE,),
        ),
    )
    transcript = pairing_transcript_hash(
        encode_record(pairing),
        encode_record(challenge),
        observed_tls_leaf_fingerprint=leaf_fingerprint,
    )
    complete = create_pairing_complete(
        signer,
        message_id=ids(),
        pairing_id=pairing.pairing_id,
        transcript_digest=transcript.hex(),
        vps_key_id=vps.public_identity.key_id,
        signed_grant_digest=record_digest(grant),
        completed_at=now,
    )
    store.begin_pairing(pairing, challenge, now=now)
    store.approve_pairing(
        pairing.pairing_id,
        device_id=ids(),
        transcript_digest=transcript.hex(),
        grant=grant,
        now=now,
    )
    resolution = create_pairing_resolution(
        message_id=ids(),
        pairing_id=pairing.pairing_id,
        signed_grant=grant,
        pairing_complete=complete,
    )
    assert isinstance(resolution, PairingResolution)
    binding = ConnectorExecutorBinding(
        SCOPE,
        BACKEND,
        executor,
        getattr(executor, "cleanup", None),
    )
    service = ConnectorService._from_test_dependencies(
        key_store=key_store,
        tls_store=tls_store,
        store=store,
        authority=GrantAuthority(store, id_factory=ids),
        tty_reader=_reader(PASSPHRASE),
        bind_host="127.0.0.1",
        port=0,
        id_factory=ids,
        execution_composition=ConnectorExecutionComposition((binding,)),
    )
    await service.unlock()
    endpoint = service.endpoint
    assert endpoint is not None
    profile = PairedVpsProfile(
        endpoint,
        vps.public_identity.key_id,
        connector_public,
        certificate.der,
        resolution.signed_grant,
        resolution.pairing_complete,
    )
    receipt_ledger = ReceiptEvidenceLedger(
        vps_state / "receipts.jsonl", connector_public, role="vps"
    )
    snapshot_store = ConnectorSnapshotStore(vps_state)
    client = ConnectorClient(
        profile,
        vps,
        PinnedWssClient(endpoint, profile.authority()),
        receipt_ledger,
        snapshot_store,
        id_factory=ids,
    )
    return _BridgeHarness(
        service,
        client,
        store,
        lease,
        receipt_ledger,
        snapshot_store,
        vps_state,
    )


def _available(_: str, __: str) -> AvailabilityRecord:
    return AvailabilityRecord("available", "Fixture Connector is available.")


def _require_loopback_bind() -> None:
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind(("127.0.0.1", 0))
    except OSError as error:
        if error.errno in {errno.EACCES, errno.EPERM}:
            pytest.skip("The execution sandbox forbids loopback listener binds.")
        raise
    finally:
        probe.close()


def _call() -> OperationCall:
    return validate_read(
        {
            "source": "web",
            "operation": "read.url",
            "target": {"url": f"https://example.com/article?query={CANARY}"},
        }
    )


def test_real_wss_bridge_executes_and_verifies_one_exact_operation(
    tmp_path: Path,
) -> None:
    _require_loopback_bind()

    async def exercise() -> None:
        executor = _FixtureExecutor()
        harness = await _open_bridge(tmp_path, executor)
        registry = AdapterRegistry()
        for binding in connector_bindings(
            harness.client, _available, (("web", "read.url"),)
        ):
            registry.register(binding)
        try:
            result = await RuntimeDispatcher(registry).dispatch(
                _call(), trace_id="c" * 32
            )

            assert result is not None
            assert result.items[0].text == "real WSS fixture result"
            assert result.selected_backend_id == BACKEND.backend_id
            assert executor.calls == 1
            assert harness.store.inspect_grants()[0].used_count == 1
            records = harness.receipt_ledger.records()
            assert len(records) == 1
            assert records[0].receipt.trace_id == "c" * 32
            snapshot = harness.snapshot_store.load()
            assert snapshot is not None
            assert snapshot.state == "authenticated"
            assert snapshot.scopes == (("web", "read.url"),)
            stored = b"".join(
                path.read_bytes()
                for path in harness.vps_state.iterdir()
                if path.is_file()
            )
            assert CANARY.encode() not in stored
            assert b"https://" not in stored
            assert all(
                stat.S_IMODE(path.stat().st_mode) == 0o600
                for path in harness.vps_state.iterdir()
                if path.is_file()
            )
        finally:
            await harness.service.close()
            harness.lease.close()

    asyncio.run(exercise())


def test_service_lock_cancels_real_wss_executor_and_awaits_cleanup(
    tmp_path: Path,
) -> None:
    _require_loopback_bind()

    async def exercise() -> None:
        executor = _BlockingExecutor()
        harness = await _open_bridge(tmp_path, executor)
        registry = AdapterRegistry()
        for binding in connector_bindings(
            harness.client, _available, (("web", "read.url"),)
        ):
            registry.register(binding)
        try:
            dispatch = asyncio.create_task(
                RuntimeDispatcher(registry).dispatch(_call(), trace_id="d" * 32)
            )
            await asyncio.wait_for(executor.started.wait(), timeout=5)

            await asyncio.wait_for(harness.service.lock(), timeout=5)
            result = await asyncio.wait_for(dispatch, timeout=5)

            assert result is not None
            assert result.failure_class == "transient"
            assert executor.calls == 1
            assert executor.cleanup_calls == 1
            assert harness.service.is_locked
            assert harness.receipt_ledger.records() == ()
        finally:
            await harness.service.close()
            harness.lease.close()

    asyncio.run(exercise())


def test_client_cancellation_closes_wss_and_cancels_remote_executor(
    tmp_path: Path,
) -> None:
    _require_loopback_bind()

    async def exercise() -> None:
        executor = _BlockingExecutor()
        harness = await _open_bridge(tmp_path, executor)
        registry = AdapterRegistry()
        for binding in connector_bindings(
            harness.client, _available, (("web", "read.url"),)
        ):
            registry.register(binding)
        try:
            dispatch = asyncio.create_task(
                RuntimeDispatcher(registry).dispatch(_call(), trace_id="e" * 32)
            )
            await asyncio.wait_for(executor.started.wait(), timeout=5)

            dispatch.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(dispatch, timeout=5)
            await asyncio.wait_for(executor.cleaned.wait(), timeout=5)

            assert executor.calls == 1
            assert executor.cleanup_calls == 1
            assert harness.store.inspect_grants()[0].used_count == 1
            assert harness.receipt_ledger.records() == ()
        finally:
            await harness.service.close()
            harness.lease.close()

    asyncio.run(exercise())
