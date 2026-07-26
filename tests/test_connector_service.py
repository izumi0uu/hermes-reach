from __future__ import annotations

import asyncio
import base64
import hashlib
import io
from collections.abc import Callable
from pathlib import Path
from typing import Never

import pytest

from hermes_reach.connector.authority import GrantAuthority
from hermes_reach.connector.errors import ConnectorError, ConnectorErrorCode
from hermes_reach.connector.identity import (
    ConnectorKeyStore,
    DevicePrivateIdentity,
    TtyPassphraseReader,
)
from hermes_reach.connector.media_policy import (
    ModelCleanupPolicy,
    ModelCostPolicy,
    ModelIdentity,
    ModelPolicy,
    ModelPolicyRow,
    ProcessLocalFileGrants,
)
from hermes_reach.connector.protocol import (
    ErrorFrame,
    FileGrant,
    GrantScope,
    PairingChallenge,
    PairingInit,
    PairingResolution,
    create_pairing_init,
    encode_record,
    verify_pairing_resolution,
)
from hermes_reach.connector.service import ConnectorService
from hermes_reach.connector.store import AuthorityStore, StoreWriterLease
from hermes_reach.connector.tls import ConnectorTLSStore, EphemeralTLSMaterial
from hermes_reach.connector.transport import ServerFrameHandler, WssEndpoint

NOW = 1_800_000_000
PASSPHRASE = "service-test-passphrase"
SCOPE = GrantScope("web", "read.url", "public")
TRANSCRIBE_SCOPE = GrantScope("youtube", "transcribe.video", "public")


def _id(value: int) -> str:
    return base64.b32encode(value.to_bytes(16, "big")).decode().rstrip("=").lower()


class _InteractiveTerminal(io.TextIOBase):
    def __init__(self, confirmation: str) -> None:
        self._input = io.StringIO(confirmation)
        self.output = io.StringIO()

    def isatty(self) -> bool:
        return True

    def write(self, value: str) -> int:
        return self.output.write(value)

    def flush(self) -> None:
        return None

    def readline(self, size: int = -1) -> str:
        return self._input.readline(size)


def _reader(*passphrases: str, confirmation: str = "") -> TtyPassphraseReader:
    values = iter(passphrases)
    terminal = _InteractiveTerminal(confirmation)

    def prompt(*, prompt: str, stream: object) -> str:
        del prompt, stream
        return next(values)

    return TtyPassphraseReader._from_test_terminal(terminal, prompt=prompt)


class _IdFactory:
    def __init__(self) -> None:
        self._value = 40_000

    def __call__(self) -> str:
        self._value += 1
        return _id(self._value)


class _FakeWssServer:
    def __init__(self, material: EphemeralTLSMaterial) -> None:
        self._material = material
        self.closed = False

    @property
    def endpoint(self) -> WssEndpoint:
        return WssEndpoint.parse("wss://127.0.0.1:8765")

    async def close(self) -> None:
        self.closed = True


class _ServerStarter:
    def __init__(self) -> None:
        self.calls = 0
        self.handler: ServerFrameHandler | None = None
        self.server: _FakeWssServer | None = None

    async def __call__(
        self,
        *,
        bind_host: str,
        port: int,
        material: EphemeralTLSMaterial,
        handler: ServerFrameHandler,
        wall_clock: Callable[[], int] | None = None,
    ) -> _FakeWssServer:
        assert bind_host == "127.0.0.1"
        assert port == 8765
        assert wall_clock is not None
        self.calls += 1
        self.handler = handler
        self.server = _FakeWssServer(material)
        return self.server


def _pairing(vps: DevicePrivateIdentity) -> PairingInit:
    return create_pairing_init(
        signer=vps,
        message_id=_id(1),
        pairing_id=_id(2),
        device_label="vps-production",
        endpoint_digest=hashlib.sha256(b"endpoint").hexdigest(),
        vps_nonce=bytes(range(32)),
        requested_scopes=(SCOPE,),
        grant_expires_at=NOW + 3_600,
        grant_max_uses=2,
        issued_at=NOW,
        deadline=NOW + 300,
    )


def _transcribe_pairing(vps: DevicePrivateIdentity) -> PairingInit:
    return create_pairing_init(
        signer=vps,
        message_id=_id(11),
        pairing_id=_id(12),
        device_label="vps-media",
        endpoint_digest=hashlib.sha256(b"media-endpoint").hexdigest(),
        vps_nonce=bytes(range(32)),
        requested_scopes=(TRANSCRIBE_SCOPE,),
        grant_expires_at=NOW + 3_600,
        grant_max_uses=2,
        issued_at=NOW,
        deadline=NOW + 300,
    )


def _media_policy() -> ModelPolicy:
    return ModelPolicy(
        1,
        (
            ModelPolicyRow(
                source="youtube",
                operation="transcribe.video",
                media_source_class="connector_local_file",
                primary=ModelIdentity("fixture-provider", "fixture-model-v1"),
                maximum_source_bytes=1024,
                maximum_duration_seconds=60,
                maximum_chunks=4,
                fallbacks=(),
                cleanup=ModelCleanupPolicy(False),
                cost=ModelCostPolicy("media_minute", 50_000),
            ),
        ),
    )


def _assert_code(
    error: pytest.ExceptionInfo[ConnectorError], code: ConnectorErrorCode
) -> None:
    assert error.value.code == code.value


def test_foreground_service_starts_locked_then_tty_approves_one_pairing(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        state = tmp_path / "connector"
        key_store = ConnectorKeyStore(state, _platform="linux")
        connector_public = key_store._initialize_from_tty_for_testing(
            _reader(PASSPHRASE)
        )
        signer = key_store._unlock_from_tty_for_testing(_reader(PASSPHRASE))
        tls_store = ConnectorTLSStore(state, _platform="linux")
        tls_store.initialize(signer, now=NOW)
        lease = StoreWriterLease(state)
        store = AuthorityStore.initialize(
            state,
            connector_public,
            lease,
            initial_policy_digest=ModelPolicy.default_deny(1).digest(),
            now=NOW,
        )
        authority = GrantAuthority(store, clock=lambda: NOW)
        starter = _ServerStarter()
        approval_reader = _reader(PASSPHRASE, confirmation="approve\n")
        injected_reader = _reader(PASSPHRASE)
        with pytest.raises(ConnectorError) as rejected_injection:
            ConnectorService(
                key_store=key_store,
                tls_store=tls_store,
                store=store,
                authority=authority,
                tty_reader=injected_reader,
                bind_host="127.0.0.1",
                port=8765,
                clock=lambda: NOW,
                id_factory=_IdFactory(),
                server_starter=starter,  # type: ignore[arg-type]
            )
        _assert_code(rejected_injection, ConnectorErrorCode.INTERACTIVE_UNLOCK_REQUIRED)
        assert starter.calls == 0

        service = ConnectorService._from_test_dependencies(
            key_store=key_store,
            tls_store=tls_store,
            store=store,
            authority=authority,
            tty_reader=approval_reader,
            bind_host="127.0.0.1",
            port=8765,
            clock=lambda: NOW,
            id_factory=_IdFactory(),
            server_starter=starter,  # type: ignore[arg-type]
        )
        try:
            assert service.is_locked
            assert service.endpoint is None
            assert not authority.is_unlocked
            assert starter.calls == 0

            await service.unlock()
            assert not service.is_locked
            assert service.endpoint == WssEndpoint.parse("wss://127.0.0.1:8765")
            assert authority.is_unlocked
            assert starter.handler is not None

            pairing = _pairing(
                DevicePrivateIdentity._from_seed_for_testing(bytes(range(32)))
            )
            first = await starter.handler(pairing)
            assert isinstance(first, PairingChallenge)
            assert await starter.handler(pairing) == first
            conflicting = await starter.handler(
                _pairing(
                    DevicePrivateIdentity._from_seed_for_testing(bytes(range(1, 33)))
                )
            )
            assert isinstance(conflicting, ErrorFrame)
            assert conflicting.code is ConnectorErrorCode.CONNECTOR_PROTOCOL_MISMATCH
            display = service.pending_pairing(pairing.pairing_id)
            assert display is not None
            assert display.device_label == pairing.device_label
            assert display.device_fingerprint.startswith("sha256:")
            assert display.scopes == (("web", "read.url", "public"),)
            assert service.pending_pairings() == (display,)

            service.approve_pairing(pairing.pairing_id)
            terminal = approval_reader._terminal
            assert isinstance(terminal, _InteractiveTerminal)
            approval_text = terminal.output.getvalue()
            assert pairing.device_label in approval_text
            assert display.device_fingerprint in approval_text
            assert display.sas in approval_text
            assert "web:read.url:public" in approval_text
            assert str(display.expires_at) in approval_text
            assert str(display.max_uses) in approval_text
            resolved = await starter.handler(pairing)
            assert isinstance(resolved, PairingResolution)
            assert (
                verify_pairing_resolution(
                    resolved,
                    pairing_init=pairing,
                    pairing_challenge=first,
                    observed_tls_leaf_fingerprint=first.tls_leaf_fingerprint,
                    now=NOW,
                )
                == resolved.signed_grant
            )

            await service.lock()
            assert service.is_locked
            assert service.endpoint is None
            assert starter.server is not None and starter.server.closed
            assert not authority.is_unlocked
            with pytest.raises(ConnectorError) as material_closed:
                _ = starter.server._material.server_context
            _assert_code(material_closed, ConnectorErrorCode.CONNECTOR_KEY_LOCKED)
        finally:
            await service.close()
            lease.close()

    asyncio.run(exercise())


def test_pairing_approval_requires_the_exact_original_tty_confirmation(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        state = tmp_path / "connector"
        key_store = ConnectorKeyStore(state, _platform="linux")
        connector_public = key_store._initialize_from_tty_for_testing(
            _reader(PASSPHRASE)
        )
        signer = key_store._unlock_from_tty_for_testing(_reader(PASSPHRASE))
        tls_store = ConnectorTLSStore(state, _platform="linux")
        tls_store.initialize(signer, now=NOW)
        lease = StoreWriterLease(state)
        store = AuthorityStore.initialize(
            state,
            connector_public,
            lease,
            initial_policy_digest=ModelPolicy.default_deny(1).digest(),
            now=NOW,
        )
        authority = GrantAuthority(store, clock=lambda: NOW)
        starter = _ServerStarter()
        service = ConnectorService._from_test_dependencies(
            key_store=key_store,
            tls_store=tls_store,
            store=store,
            authority=authority,
            tty_reader=_reader(PASSPHRASE, confirmation="yes\n"),
            bind_host="127.0.0.1",
            port=8765,
            clock=lambda: NOW,
            id_factory=_IdFactory(),
            server_starter=starter,  # type: ignore[arg-type]
        )
        try:
            await service.unlock()
            assert starter.handler is not None
            pairing = _pairing(
                DevicePrivateIdentity._from_seed_for_testing(bytes(range(32)))
            )
            assert isinstance(await starter.handler(pairing), PairingChallenge)
            with pytest.raises(ConnectorError) as rejected:
                service.approve_pairing(pairing.pairing_id)
            _assert_code(rejected, ConnectorErrorCode.INTERACTIVE_UNLOCK_REQUIRED)
            assert service.pending_pairing(pairing.pairing_id) is not None
            assert store.inspect_devices() == ()
        finally:
            await service.close()
            lease.close()

    asyncio.run(exercise())


def test_state_initialization_opens_a_locked_foreground_service(tmp_path: Path) -> None:
    async def exercise() -> None:
        state = tmp_path / "connector"
        ConnectorService._initialize_state_directory_for_testing(
            state,
            tty_reader=_reader(PASSPHRASE, PASSPHRASE),
            clock=lambda: NOW,
        )
        starter = _ServerStarter()
        with pytest.raises(ConnectorError) as rejected_injection:
            ConnectorService.open_state_directory(
                state,
                tty_reader=_reader(PASSPHRASE),
                bind_host="127.0.0.1",
                port=8765,
                clock=lambda: NOW,
                server_starter=starter,  # type: ignore[arg-type]
            )
        _assert_code(rejected_injection, ConnectorErrorCode.INTERACTIVE_UNLOCK_REQUIRED)
        assert starter.calls == 0

        service = ConnectorService._open_state_directory_for_testing(
            state,
            tty_reader=_reader(PASSPHRASE),
            bind_host="127.0.0.1",
            port=8765,
            clock=lambda: NOW,
            id_factory=_IdFactory(),
            server_starter=starter,  # type: ignore[arg-type]
        )
        try:
            assert service.is_locked
            assert service.endpoint is None
            assert starter.calls == 0
            await service.unlock()
            assert not service.is_locked
            assert starter.calls == 1
        finally:
            await service.close()

    asyncio.run(exercise())


def test_opening_a_missing_state_does_not_create_connector_files(
    tmp_path: Path,
) -> None:
    state = tmp_path / "connector"

    with pytest.raises(ConnectorError) as missing:
        ConnectorService._open_state_directory_for_testing(
            state,
            tty_reader=_reader(PASSPHRASE),
            bind_host="127.0.0.1",
            port=8765,
            clock=lambda: NOW,
        )

    _assert_code(missing, ConnectorErrorCode.CONNECTOR_STATE_INVALID)
    assert not state.exists()


def test_foreground_loop_uses_only_its_captured_tty_and_closes_on_exit(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        state = tmp_path / "connector"
        key_store = ConnectorKeyStore(state, _platform="linux")
        connector_public = key_store._initialize_from_tty_for_testing(
            _reader(PASSPHRASE)
        )
        signer = key_store._unlock_from_tty_for_testing(_reader(PASSPHRASE))
        tls_store = ConnectorTLSStore(state, _platform="linux")
        tls_store.initialize(signer, now=NOW)
        lease = StoreWriterLease(state)
        store = AuthorityStore.initialize(
            state,
            connector_public,
            lease,
            initial_policy_digest=ModelPolicy.default_deny(1).digest(),
            now=NOW,
        )
        starter = _ServerStarter()
        reader = _reader(
            PASSPHRASE,
            confirmation="status\nunlock\nlock\nexit\n",
        )
        service = ConnectorService._from_test_dependencies(
            key_store=key_store,
            tls_store=tls_store,
            store=store,
            authority=GrantAuthority(store, clock=lambda: NOW),
            tty_reader=reader,
            bind_host="127.0.0.1",
            port=8765,
            clock=lambda: NOW,
            id_factory=_IdFactory(),
            server_starter=starter,  # type: ignore[arg-type]
        )
        try:
            await service.serve_foreground()
            assert service.is_locked
            assert starter.server is not None and starter.server.closed
            terminal = reader._terminal
            assert isinstance(terminal, _InteractiveTerminal)
            output = terminal.output.getvalue()
            assert "Connector locked." in output
            assert "Connector unlocked." in output
            assert "Connector stopped." in output
        finally:
            lease.close()

    asyncio.run(exercise())


def test_original_tty_approves_path_free_process_local_file_grant(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        state = tmp_path / "connector"
        key_store = ConnectorKeyStore(state, _platform="linux")
        connector_public = key_store._initialize_from_tty_for_testing(
            _reader(PASSPHRASE)
        )
        signer = key_store._unlock_from_tty_for_testing(_reader(PASSPHRASE))
        tls_store = ConnectorTLSStore(state, _platform="linux")
        tls_store.initialize(signer, now=NOW)
        lease = StoreWriterLease(state)
        policy = _media_policy()
        store = AuthorityStore.initialize(
            state,
            connector_public,
            lease,
            initial_policy_digest=policy.digest(),
            now=NOW,
        )
        authority = GrantAuthority(store, clock=lambda: NOW)
        starter = _ServerStarter()
        reader = _reader(PASSPHRASE, confirmation="approve\napprove\n")
        file_grants = ProcessLocalFileGrants(clock=lambda: NOW, id_factory=_IdFactory())
        service = ConnectorService._from_test_dependencies(
            key_store=key_store,
            tls_store=tls_store,
            store=store,
            authority=authority,
            tty_reader=reader,
            bind_host="127.0.0.1",
            port=8765,
            clock=lambda: NOW,
            id_factory=_IdFactory(),
            server_starter=starter,  # type: ignore[arg-type]
            model_policy=policy,
            file_grants=file_grants,
        )
        try:
            await service.unlock()
            assert starter.handler is not None
            pairing = _transcribe_pairing(
                DevicePrivateIdentity._from_seed_for_testing(bytes(range(32)))
            )
            assert isinstance(await starter.handler(pairing), PairingChallenge)
            service.approve_pairing(pairing.pairing_id)
            grant_inspection = store.inspect_grants()[0]

            private_directory = tmp_path / "SECRET_PARENT_PATH_CANARY"
            private_directory.mkdir()
            media = private_directory / "episode.wav"
            media.write_bytes(b"fixture media")
            file_grant = service.approve_local_file(
                media,
                grant_id=grant_inspection.grant_id,
                source="youtube",
                operation="transcribe.video",
            )

            assert isinstance(file_grant, FileGrant)
            assert file_grant.subject_key_id == pairing.vps_key_id
            assert file_grant.grant_revision == grant_inspection.revision
            assert file_grant.policy_revision == policy.revision
            assert file_grants.active_count == 1
            terminal = reader._terminal
            assert isinstance(terminal, _InteractiveTerminal)
            output = terminal.output.getvalue()
            assert pairing.device_label in output
            assert media.name in output
            assert file_grant.digest in output
            assert str(file_grant.size) in output
            assert "youtube:transcribe.video" in output
            assert str(file_grant.expires_at) in output
            assert "SECRET_PARENT_PATH_CANARY" not in output
            assert b"SECRET_PARENT_PATH_CANARY" not in encode_record(file_grant)
            assert b"episode.wav" not in encode_record(file_grant)
            database = state / "connector-authority.sqlite3"
            assert b"SECRET_PARENT_PATH_CANARY" not in database.read_bytes()
        finally:
            await service.close()
            assert file_grants.active_count == 0
            lease.close()

    asyncio.run(exercise())


def test_service_rejects_model_rows_not_bound_to_authority_policy_digest(
    tmp_path: Path,
) -> None:
    state = tmp_path / "connector"
    key_store = ConnectorKeyStore(state, _platform="linux")
    connector_public = key_store._initialize_from_tty_for_testing(_reader(PASSPHRASE))
    signer = key_store._unlock_from_tty_for_testing(_reader(PASSPHRASE))
    tls_store = ConnectorTLSStore(state, _platform="linux")
    tls_store.initialize(signer, now=NOW)
    lease = StoreWriterLease(state)
    store = AuthorityStore.initialize(
        state,
        connector_public,
        lease,
        initial_policy_digest=ModelPolicy.default_deny(1).digest(),
        now=NOW,
    )
    reader = _reader(PASSPHRASE)
    try:
        with pytest.raises(ConnectorError) as mismatch:
            ConnectorService._from_test_dependencies(
                key_store=key_store,
                tls_store=tls_store,
                store=store,
                authority=GrantAuthority(store, clock=lambda: NOW),
                tty_reader=reader,
                bind_host="127.0.0.1",
                port=8765,
                clock=lambda: NOW,
                model_policy=_media_policy(),
            )
        _assert_code(mismatch, ConnectorErrorCode.CONNECTOR_STATE_INVALID)
    finally:
        reader.close()
        lease.close()


def test_failed_state_initialization_preserves_partial_state_and_refuses_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "connector"

    def fail_ca(
        self: ConnectorTLSStore, signer: DevicePrivateIdentity, *, now: int
    ) -> Never:
        del self, signer, now
        raise ConnectorError(ConnectorErrorCode.CONNECTOR_STATE_INVALID)

    monkeypatch.setattr(ConnectorTLSStore, "initialize", fail_ca)
    with pytest.raises(ConnectorError) as failed:
        ConnectorService._initialize_state_directory_for_testing(
            state,
            tty_reader=_reader(PASSPHRASE, PASSPHRASE),
            clock=lambda: NOW,
        )
    _assert_code(failed, ConnectorErrorCode.CONNECTOR_STATE_INVALID)
    key_path = state / "connector-identity.pem"
    original_key = key_path.read_bytes()

    with pytest.raises(ConnectorError) as retry:
        ConnectorService._initialize_state_directory_for_testing(
            state,
            tty_reader=_reader(),
            clock=lambda: NOW,
        )
    _assert_code(retry, ConnectorErrorCode.CONNECTOR_STATE_INVALID)
    assert key_path.read_bytes() == original_key
