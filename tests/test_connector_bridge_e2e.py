from __future__ import annotations

import asyncio
import base64
import errno
import hashlib
import io
import json
import socket
import stat
import sys
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import pytest

from hermes_reach.bootstrap import build_vps_runtime
from hermes_reach.connector.audit import ReceiptEvidenceLedger
from hermes_reach.connector.authority import AuthorizedExecution, GrantAuthority
from hermes_reach.connector.client import (
    ConnectorClient,
    ConnectorSnapshotStore,
    PairedVpsProfile,
    PendingVpsProfile,
    VpsProfileStore,
    _endpoint_digest,
)
from hermes_reach.connector.execution import (
    ConnectorExecutionComposition,
    ConnectorExecutorBinding,
)
from hermes_reach.connector.identity import (
    ConnectorKeyStore,
    DevicePrivateIdentity,
    TtyPassphraseReader,
    VpsKeyStore,
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
from hermes_reach.contracts import OperationCall, validate_browse, validate_read
from hermes_reach.runtime.adapters import AdapterRegistry
from hermes_reach.runtime.availability import AvailabilityRecord
from hermes_reach.runtime.dispatcher import RuntimeDispatcher
from hermes_reach.sources.connector import connector_bindings
from hermes_reach.sources.opencli_social import (
    OpenCliSessionAttestation,
    attest_opencli_social_session,
    opencli_social_execution_composition,
)

PASSPHRASE = "bridge-e2e-passphrase"
SCOPE = GrantScope("rss", "read.feed", "public")
REDDIT_SCOPE = GrantScope("reddit", "read.post", "public")
INSTAGRAM_EXPLORE_SCOPE = GrantScope("instagram", "browse.explore", "account_visible")
BACKEND = PublicBackendIdentity("reach-bounded-executor-v1", "1")
SOCIAL_BACKEND = PublicBackendIdentity("opencli", "1.8.6-hermes.1")
CANARY = "BRIDGE_QUERY_CANARY"
REDDIT_POST_ID = "abc123"
INSTAGRAM_AUTHOR_CANARY = "explore_canary"
INSTAGRAM_RESULT_CANARY = "EXPLORE_RESULT_CANARY"
NODE_PATH_CANARY = "OPENCLI_NODE_PATH_CANARY"
OPENCLI_ROOT_PATH_CANARY = "OPENCLI_ROOT_PATH_CANARY"
SESSION_PATH_CANARY = "OPENCLI_SESSION_PATH_CANARY"
REDDIT_OUTPUT = """\
- type: POST
  author: alice
  score: 12
  text: Fixture Reddit post
  post_hint: self
  url_overridden_by_dest: ""
  preview_image_url: ""
  gallery_urls: []
- type: L0
  author: bob
  score: 3
  text: Fixture Reddit reply
  post_hint: ""
  url_overridden_by_dest: ""
  preview_image_url: ""
  gallery_urls: []
"""
INSTAGRAM_EXPLORE_OUTPUT = f"""\
- rank: 1
  user: {INSTAGRAM_AUTHOR_CANARY}
  caption: {INSTAGRAM_RESULT_CANARY}
  likes: 50
  comments: 4
  type: image
"""


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
        call = execution.operation_call()
        assert (call.source.name, call.operation.name) == ("rss", "read.feed")
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
    trusted_state: Path
    vps_state: Path


async def _open_bridge(
    tmp_path: Path,
    executor: _FixtureExecutor | _BlockingExecutor | None,
    *,
    scope: GrantScope = SCOPE,
    execution_composition: ConnectorExecutionComposition | None = None,
    persist_vps_profile: bool = False,
) -> _BridgeHarness:
    now = int(time.time())
    ids = _Ids()
    trusted_state = tmp_path / "connector"
    vps_state = tmp_path / "vps"
    if persist_vps_profile:
        vps_key_store = VpsKeyStore(vps_state, _platform="linux")
        vps_key_store.initialize()
        vps = vps_key_store.load()
    else:
        vps_state.mkdir(mode=0o700)
        vps = DevicePrivateIdentity._from_seed_for_testing(bytes([31]) * 32)
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
    if execution_composition is None:
        assert executor is not None
        binding = ConnectorExecutorBinding(
            scope,
            BACKEND,
            executor,
            getattr(executor, "cleanup", None),
        )
        execution_composition = ConnectorExecutionComposition((binding,))
    service = ConnectorService._from_test_dependencies(
        key_store=key_store,
        tls_store=tls_store,
        store=store,
        authority=GrantAuthority(store, id_factory=ids),
        tty_reader=_reader(PASSPHRASE),
        bind_host="127.0.0.1",
        port=0,
        id_factory=ids,
        execution_composition=execution_composition,
    )
    await service.unlock()
    endpoint = service.endpoint
    assert endpoint is not None
    pairing = create_pairing_init(
        vps,
        message_id=ids(),
        pairing_id=ids(),
        device_label="bridge-e2e-vps",
        endpoint_digest=(
            _endpoint_digest(endpoint)
            if persist_vps_profile
            else hashlib.sha256(b"loopback-bridge").hexdigest()
        ),
        vps_nonce=bytes(range(32)),
        requested_scopes=(scope,),
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
            scopes=(scope,),
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
    if persist_vps_profile:
        pending = PendingVpsProfile(
            endpoint,
            vps.public_identity.key_id,
            pairing,
            challenge,
            leaf_fingerprint,
        )
        profile_store = VpsProfileStore(vps_state)
        profile_store.save_pending(pending)
        profile = profile_store.commit_paired(pending, resolution, now=now)
    else:
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
        trusted_state,
        vps_state,
    )


def _available(source: str, operation: str) -> AvailabilityRecord:
    del source, operation
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
            "source": "rss",
            "operation": "read.feed",
            "target": {"url": f"https://example.com/article?query={CANARY}"},
        }
    )


def _reddit_call() -> OperationCall:
    return validate_read(
        {
            "source": "reddit",
            "operation": "read.post",
            "target": {
                "url": (
                    "https://www.reddit.com/r/python/comments/"
                    f"{REDDIT_POST_ID}/fixture_post"
                )
            },
        }
    )


def _instagram_explore_call() -> OperationCall:
    return validate_browse(
        {
            "source": "instagram",
            "operation": "browse.explore",
            "options": {"limit": 1},
        }
    )


def _opencli_social_fixture(
    tmp_path: Path,
    *,
    expected_argv: tuple[str, ...],
    output: str,
) -> OpenCliSessionAttestation:
    node = tmp_path / NODE_PATH_CANARY
    node.write_text(
        f"#!{sys.executable}\n"
        "import sys\n"
        "if not sys.argv[1].endswith(\n"
        "    '/node_modules/@jackwener/opencli/dist/src/main.js'\n"
        "):\n"
        "    raise SystemExit(6)\n"
        f"if tuple(sys.argv[2:]) != {expected_argv!r}:\n"
        "    raise SystemExit(7)\n"
        f"sys.stdout.write({output!r})\n",
        encoding="utf-8",
    )
    node.chmod(0o700)
    root = tmp_path / OPENCLI_ROOT_PATH_CANARY
    package_root = root / "node_modules" / "@jackwener" / "opencli"
    cli = package_root / "dist" / "src" / "main.js"
    cli.parent.mkdir(parents=True)
    cli.write_bytes(b"export {};\n")
    (package_root / "package.json").write_text(
        json.dumps(
            {
                "name": "@jackwener/opencli",
                "version": "1.8.6-hermes.1",
                "bin": {"opencli": "dist/src/main.js"},
            },
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    for path in root.rglob("*"):
        path.chmod(0o700 if path.is_dir() else 0o600)
    root.chmod(0o700)
    session_home = tmp_path / SESSION_PATH_CANARY
    session_home.mkdir(mode=0o700)
    return attest_opencli_social_session(node, root, cli, session_home)


def _private_attestation_markers(
    attestation: OpenCliSessionAttestation,
) -> tuple[bytes, ...]:
    return tuple(
        value.encode()
        for value in (
            str(attestation.node_executable),
            attestation.node_sha256,
            str(attestation.opencli_root),
            str(attestation.opencli_cli),
            attestation.opencli_tree_sha256,
            str(attestation.session_home),
            attestation.node_executable.name,
            attestation.opencli_root.name,
            attestation.session_home.name,
        )
    ) + (
        bytes.fromhex(attestation.node_sha256),
        bytes.fromhex(attestation.opencli_tree_sha256),
    )


def _persisted_state_bytes(harness: _BridgeHarness) -> bytes:
    return b"".join(
        path.read_bytes()
        for state_directory in (harness.trusted_state, harness.vps_state)
        for path in state_directory.rglob("*")
        if path.is_file()
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
            harness.client, _available, (("rss", "read.feed"),)
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
            assert snapshot.scopes == (("rss", "read.feed"),)
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


def test_production_social_composition_and_vps_runtime_cross_real_wss(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _require_loopback_bind()
    attestation = _opencli_social_fixture(
        tmp_path,
        expected_argv=(
            "reddit",
            "read",
            REDDIT_POST_ID,
            "--sort",
            "best",
            "--limit",
            "3",
            "--depth",
            "2",
            "--replies",
            "2",
            "--max-length",
            "800",
            "--format",
            "yaml",
        ),
        output=REDDIT_OUTPUT,
    )
    private_markers = _private_attestation_markers(attestation)
    composition = opencli_social_execution_composition(attestation)

    async def exercise() -> None:
        harness = await _open_bridge(
            tmp_path,
            None,
            scope=REDDIT_SCOPE,
            execution_composition=composition,
            persist_vps_profile=True,
        )
        monkeypatch.setattr(VpsKeyStore, "_ensure_platform", lambda self: None)
        runtime = build_vps_runtime(harness.vps_state)
        try:
            before = runtime.operation_availability("reddit", "read.post")
            assert before.state == "degraded"

            result = await runtime.dispatch(_reddit_call(), trace_id="f" * 32)

            assert result is not None
            assert [(item.kind, item.text) for item in result.items] == [
                ("content", "Fixture Reddit post | score: 12 | media: self"),
                ("reply", "Fixture Reddit reply | score: 3"),
            ]
            assert result.items[0].native_id == REDDIT_POST_ID
            assert result.selected_backend_id == SOCIAL_BACKEND.backend_id
            grant = harness.store.inspect_grants()[0]
            assert grant.used_count == 1
            assert [
                (scope.source, scope.operation, scope.data_scope)
                for scope in grant.scopes
            ] == [("reddit", "read.post", "public")]
            records = harness.receipt_ledger.records()
            assert len(records) == 1
            receipt = records[0].receipt
            assert receipt.trace_id == "f" * 32
            assert (receipt.source, receipt.operation) == ("reddit", "read.post")
            assert receipt.backend == SOCIAL_BACKEND
            assert receipt.result_count == 2
            snapshot = harness.snapshot_store.load()
            assert snapshot is not None
            assert snapshot.state == "authenticated"
            assert snapshot.scopes == (("reddit", "read.post"),)
            after = runtime.operation_availability("reddit", "read.post")
            assert after.state == "available"
            assert after.backend_id == SOCIAL_BACKEND.backend_id
            stored = _persisted_state_bytes(harness)
            assert all(value not in stored for value in private_markers)
            assert REDDIT_POST_ID.encode() not in stored
            assert b"Fixture Reddit post" not in stored
        finally:
            await harness.service.close()
            harness.lease.close()

    asyncio.run(exercise())


def test_account_visible_instagram_explore_crosses_real_wss_without_persistence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _require_loopback_bind()
    attestation = _opencli_social_fixture(
        tmp_path,
        expected_argv=(
            "instagram",
            "explore",
            "--limit",
            "1",
            "--format",
            "yaml",
        ),
        output=INSTAGRAM_EXPLORE_OUTPUT,
    )
    private_markers = _private_attestation_markers(attestation)
    composition = opencli_social_execution_composition(attestation)
    assert (
        composition.required_scope("instagram", "browse.explore")
        == INSTAGRAM_EXPLORE_SCOPE
    )

    async def exercise() -> None:
        harness = await _open_bridge(
            tmp_path,
            None,
            scope=INSTAGRAM_EXPLORE_SCOPE,
            execution_composition=composition,
            persist_vps_profile=True,
        )
        monkeypatch.setattr(VpsKeyStore, "_ensure_platform", lambda self: None)
        runtime = build_vps_runtime(harness.vps_state)
        try:
            before = runtime.operation_availability("instagram", "browse.explore")
            assert before.state == "degraded"

            call = _instagram_explore_call()
            assert call.operation.runtime.data_scope == "account_visible"
            result = await runtime.dispatch(
                call,
                effective_scope="account_visible",
                trace_id="a" * 32,
            )

            assert result is not None
            assert [(item.kind, item.text) for item in result.items] == [
                (
                    "entry",
                    (
                        f"{INSTAGRAM_RESULT_CANARY} | reactions: 50 | "
                        "comments: 4 | media: image"
                    ),
                )
            ]
            assert result.items[0].native_id == "1"
            assert result.items[0].author == INSTAGRAM_AUTHOR_CANARY
            assert result.items[0].published_at is None
            assert result.selected_backend_id == SOCIAL_BACKEND.backend_id

            grant = harness.store.inspect_grants()[0]
            assert grant.used_count == 1
            assert [
                (scope.source, scope.operation, scope.data_scope)
                for scope in grant.scopes
            ] == [("instagram", "browse.explore", "account_visible")]
            records = harness.receipt_ledger.records()
            assert len(records) == 1
            receipt = records[0].receipt
            assert receipt.trace_id == "a" * 32
            assert (receipt.source, receipt.operation) == (
                "instagram",
                "browse.explore",
            )
            assert receipt.backend == SOCIAL_BACKEND
            assert receipt.result_count == 1
            snapshot = harness.snapshot_store.load()
            assert snapshot is not None
            assert snapshot.state == "authenticated"
            assert snapshot.scopes == (("instagram", "browse.explore"),)
            after = runtime.operation_availability("instagram", "browse.explore")
            assert after.state == "available"
            assert after.backend_id == SOCIAL_BACKEND.backend_id

            stored = _persisted_state_bytes(harness)
            assert all(value not in stored for value in private_markers)
            assert INSTAGRAM_RESULT_CANARY.encode() not in stored
            assert INSTAGRAM_AUTHOR_CANARY.encode() not in stored
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
            harness.client, _available, (("rss", "read.feed"),)
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
            harness.client, _available, (("rss", "read.feed"),)
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
