"""Foreground locked Connector service and TTY-gated pairing authority."""

from __future__ import annotations

import asyncio
import base64
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .authority import GrantAuthority
from .errors import ConnectorError, ConnectorErrorCode
from .identity import (
    ConnectorKeyStore,
    DevicePrivateIdentity,
    DevicePublicIdentity,
    TtyPassphraseReader,
    _require_test_tty_reader,
    _require_tty_reader,
)
from .media_policy import FileGrantProposal, ModelPolicy, ProcessLocalFileGrants
from .protocol import (
    ErrorFrame,
    FileGrant,
    GrantClaims,
    PairingComplete,
    PairingInit,
    ProtocolValidationError,
    SignedGrant,
    WireRecord,
    create_pairing_challenge,
    create_pairing_complete,
    create_pairing_resolution,
    create_signed_grant,
    encode_record,
    pairing_sas,
    pairing_transcript_hash,
    record_digest,
)
from .store import AuthorityStore, PairingState, StoreWriterLease
from .tls import ConnectorCACertificate, ConnectorTLSStore, EphemeralTLSMaterial
from .transport import ServerFrameHandler, WssEndpoint, WssServer


class WssServerStarter(Protocol):
    async def __call__(
        self,
        *,
        bind_host: str,
        port: int,
        material: EphemeralTLSMaterial,
        handler: ServerFrameHandler,
        wall_clock: Callable[[], int] | None = None,
    ) -> WssServer: ...


@dataclass(frozen=True, slots=True)
class PairingDisplay:
    """The complete, safe-to-render approval context from one pending pairing."""

    pairing_id: str
    device_label: str
    device_fingerprint: str
    sas: str
    scopes: tuple[tuple[str, str, str], ...]
    expires_at: int
    max_uses: int


@dataclass(frozen=True, slots=True)
class _FileGrantContext:
    device_label: str
    subject_key_id: str
    grant_revision: int
    policy_revision: int


_INITIAL_POLICY_DIGEST = ModelPolicy.default_deny(1).digest()


class ConnectorService:
    """Own one locked foreground lease; no listener exists before TTY unlock."""

    def __init__(
        self,
        *,
        key_store: ConnectorKeyStore,
        tls_store: ConnectorTLSStore,
        store: AuthorityStore,
        authority: GrantAuthority,
        tty_reader: TtyPassphraseReader,
        bind_host: str,
        port: int,
        clock: Callable[[], int] | None = None,
        id_factory: Callable[[], str] | None = None,
        server_starter: WssServerStarter | None = None,
        owned_lease: StoreWriterLease | None = None,
        model_policy: ModelPolicy | None = None,
        file_grants: ProcessLocalFileGrants | None = None,
    ) -> None:
        _require_tty_reader(tty_reader)
        self._initialize(
            key_store=key_store,
            tls_store=tls_store,
            store=store,
            authority=authority,
            tty_reader=tty_reader,
            bind_host=bind_host,
            port=port,
            clock=clock,
            id_factory=id_factory,
            server_starter=server_starter,
            owned_lease=owned_lease,
            model_policy=model_policy,
            file_grants=file_grants,
            test_reader=False,
        )

    @classmethod
    def _from_test_dependencies(
        cls,
        *,
        key_store: ConnectorKeyStore,
        tls_store: ConnectorTLSStore,
        store: AuthorityStore,
        authority: GrantAuthority,
        tty_reader: TtyPassphraseReader,
        bind_host: str,
        port: int,
        clock: Callable[[], int] | None = None,
        id_factory: Callable[[], str] | None = None,
        server_starter: WssServerStarter | None = None,
        owned_lease: StoreWriterLease | None = None,
        model_policy: ModelPolicy | None = None,
        file_grants: ProcessLocalFileGrants | None = None,
    ) -> ConnectorService:
        """Construct a service with a hermetic reader only from private tests."""

        _require_test_tty_reader(tty_reader)
        service = object.__new__(cls)
        service._initialize(
            key_store=key_store,
            tls_store=tls_store,
            store=store,
            authority=authority,
            tty_reader=tty_reader,
            bind_host=bind_host,
            port=port,
            clock=clock,
            id_factory=id_factory,
            server_starter=server_starter,
            owned_lease=owned_lease,
            model_policy=model_policy,
            file_grants=file_grants,
            test_reader=True,
        )
        return service

    def _initialize(
        self,
        *,
        key_store: ConnectorKeyStore,
        tls_store: ConnectorTLSStore,
        store: AuthorityStore,
        authority: GrantAuthority,
        tty_reader: TtyPassphraseReader,
        bind_host: str,
        port: int,
        clock: Callable[[], int] | None,
        id_factory: Callable[[], str] | None,
        server_starter: WssServerStarter | None,
        owned_lease: StoreWriterLease | None,
        model_policy: ModelPolicy | None,
        file_grants: ProcessLocalFileGrants | None,
        test_reader: bool,
    ) -> None:
        effective_clock = _wall_timestamp if clock is None else clock
        effective_id_factory = _new_id if id_factory is None else id_factory
        effective_server_starter = (
            WssServer.start if server_starter is None else server_starter
        )
        if (
            not isinstance(key_store, ConnectorKeyStore)
            or not isinstance(tls_store, ConnectorTLSStore)
            or not isinstance(store, AuthorityStore)
            or not isinstance(authority, GrantAuthority)
            or type(bind_host) is not str
            or type(port) is not int
            or not callable(effective_clock)
            or not callable(effective_id_factory)
            or not callable(effective_server_starter)
            or (owned_lease is not None and type(owned_lease) is not StoreWriterLease)
        ):
            raise TypeError("The Connector service dependencies are invalid.")
        current_policy_revision = store.current_policy_revision()
        effective_model_policy = (
            ModelPolicy.default_deny(current_policy_revision)
            if model_policy is None
            else model_policy
        )
        effective_file_grants = (
            ProcessLocalFileGrants(
                clock=effective_clock, id_factory=effective_id_factory
            )
            if file_grants is None
            else file_grants
        )
        if not isinstance(effective_model_policy, ModelPolicy) or not isinstance(
            effective_file_grants, ProcessLocalFileGrants
        ):
            raise TypeError("The Connector service dependencies are invalid.")
        if (
            effective_model_policy.revision != current_policy_revision
            or effective_model_policy.digest() != store.current_policy_digest()
        ):
            raise ConnectorError(ConnectorErrorCode.CONNECTOR_STATE_INVALID)
        if (
            store.connector_identity
            != tls_store.load(
                store.connector_identity, now=effective_clock()
            ).connector_identity
        ):
            raise ConnectorError(ConnectorErrorCode.CONNECTOR_STATE_INVALID)
        self._key_store = key_store
        self._tls_store = tls_store
        self._store = store
        self._authority = authority
        self._tty_reader = tty_reader
        self._bind_host = bind_host
        self._port = port
        self._clock = effective_clock
        self._id_factory = effective_id_factory
        self._server_starter = effective_server_starter
        self._owned_lease = owned_lease
        self._model_policy = effective_model_policy
        self._file_grants = effective_file_grants
        self._test_reader = test_reader
        self._lifecycle_lock = asyncio.Lock()
        self._server: WssServer | None = None
        self._signer: DevicePrivateIdentity | None = None
        self._authority_certificate: ConnectorCACertificate | None = None
        self._material: EphemeralTLSMaterial | None = None

    @classmethod
    def initialize_state_directory(
        cls,
        state_directory: Path,
        *,
        tty_reader: TtyPassphraseReader,
        clock: Callable[[], int] | None = None,
    ) -> None:
        """Create one complete Connector profile or preserve a failed partial state.

        A later init never overwrites an identity, CA, or authority database. The
        owner must inspect and intentionally recover any partially created state.
        """

        _require_tty_reader(tty_reader)
        cls._initialize_state_directory(
            state_directory,
            tty_reader=tty_reader,
            clock=clock,
            test_reader=False,
        )

    @classmethod
    def _initialize_state_directory_for_testing(
        cls,
        state_directory: Path,
        *,
        tty_reader: TtyPassphraseReader,
        clock: Callable[[], int] | None = None,
    ) -> None:
        """Initialize state through the explicit private test-reader boundary."""

        _require_test_tty_reader(tty_reader)
        cls._initialize_state_directory(
            state_directory,
            tty_reader=tty_reader,
            clock=clock,
            test_reader=True,
        )

    @staticmethod
    def _initialize_state_directory(
        state_directory: Path,
        *,
        tty_reader: TtyPassphraseReader,
        clock: Callable[[], int] | None,
        test_reader: bool,
    ) -> None:
        if not isinstance(state_directory, Path):
            raise TypeError("The Connector initialization inputs are invalid.")
        effective_clock = _wall_timestamp if clock is None else clock
        if not callable(effective_clock):
            raise TypeError("The Connector initialization inputs are invalid.")
        now = effective_clock()
        lease = StoreWriterLease(state_directory)
        signer: DevicePrivateIdentity | None = None
        try:
            _require_initialization_directory(state_directory)
            key_store = ConnectorKeyStore(state_directory)
            if test_reader:
                key_store._initialize_from_tty_for_testing(tty_reader)
                signer = key_store._unlock_from_tty_for_testing(tty_reader)
            else:
                key_store.initialize_from_tty(tty_reader)
                signer = key_store.unlock_from_tty(tty_reader)
            tls_store = ConnectorTLSStore(state_directory)
            identity = signer.public_identity
            tls_store.initialize(signer, now=now)
            AuthorityStore.initialize(
                state_directory,
                identity,
                lease,
                initial_policy_digest=_INITIAL_POLICY_DIGEST,
                now=now,
            )
        finally:
            signer = None
            lease.close()

    @classmethod
    def open_state_directory(
        cls,
        state_directory: Path,
        *,
        tty_reader: TtyPassphraseReader,
        bind_host: str,
        port: int,
        clock: Callable[[], int] | None = None,
        id_factory: Callable[[], str] | None = None,
        server_starter: WssServerStarter | None = None,
    ) -> ConnectorService:
        """Open verified public state for a locked foreground Connector service."""

        _require_tty_reader(tty_reader)
        return cls._open_state_directory(
            state_directory,
            tty_reader=tty_reader,
            bind_host=bind_host,
            port=port,
            clock=clock,
            id_factory=id_factory,
            server_starter=server_starter,
            test_reader=False,
        )

    @classmethod
    def _open_state_directory_for_testing(
        cls,
        state_directory: Path,
        *,
        tty_reader: TtyPassphraseReader,
        bind_host: str,
        port: int,
        clock: Callable[[], int] | None = None,
        id_factory: Callable[[], str] | None = None,
        server_starter: WssServerStarter | None = None,
    ) -> ConnectorService:
        """Open state through the explicit private test-reader boundary."""

        _require_test_tty_reader(tty_reader)
        return cls._open_state_directory(
            state_directory,
            tty_reader=tty_reader,
            bind_host=bind_host,
            port=port,
            clock=clock,
            id_factory=id_factory,
            server_starter=server_starter,
            test_reader=True,
        )

    @classmethod
    def _open_state_directory(
        cls,
        state_directory: Path,
        *,
        tty_reader: TtyPassphraseReader,
        bind_host: str,
        port: int,
        clock: Callable[[], int] | None,
        id_factory: Callable[[], str] | None,
        server_starter: WssServerStarter | None,
        test_reader: bool,
    ) -> ConnectorService:
        if not isinstance(state_directory, Path):
            raise TypeError("The Connector service state inputs are invalid.")
        effective_clock = _wall_timestamp if clock is None else clock
        if not callable(effective_clock):
            raise TypeError("The Connector service state inputs are invalid.")
        lease = StoreWriterLease(state_directory, create=False)
        try:
            tls_store = ConnectorTLSStore(state_directory)
            identity = tls_store.load_public_identity(now=effective_clock())
            store = AuthorityStore.open(state_directory, identity, lease)
            if test_reader:
                return cls._from_test_dependencies(
                    key_store=ConnectorKeyStore(state_directory),
                    tls_store=tls_store,
                    store=store,
                    authority=GrantAuthority(store, clock=effective_clock),
                    tty_reader=tty_reader,
                    bind_host=bind_host,
                    port=port,
                    clock=effective_clock,
                    id_factory=id_factory,
                    server_starter=server_starter,
                    owned_lease=lease,
                    model_policy=None,
                    file_grants=None,
                )
            return cls(
                key_store=ConnectorKeyStore(state_directory),
                tls_store=tls_store,
                store=store,
                authority=GrantAuthority(store, clock=effective_clock),
                tty_reader=tty_reader,
                bind_host=bind_host,
                port=port,
                clock=effective_clock,
                id_factory=id_factory,
                server_starter=server_starter,
                owned_lease=lease,
                model_policy=None,
                file_grants=None,
            )
        except BaseException:
            lease.close()
            raise

    @property
    def is_locked(self) -> bool:
        return self._server is None

    @property
    def endpoint(self) -> WssEndpoint | None:
        """Expose the listener only while the service owns an active unlock lease."""

        server = self._server
        return None if server is None else server.endpoint

    async def unlock(self) -> None:
        """Unlock through the captured original TTY, then bind the WSS listener."""

        async with self._lifecycle_lock:
            if self._server is not None:
                raise ConnectorError(ConnectorErrorCode.CONNECTOR_KEY_LOCKED)
            if self._test_reader:
                signer = self._key_store._unlock_from_tty_for_testing(self._tty_reader)
            else:
                signer = self._key_store.unlock_from_tty(self._tty_reader)
            if signer.public_identity != self._store.connector_identity:
                raise ConnectorError(ConnectorErrorCode.CONNECTOR_STATE_INVALID)
            now = self._clock()
            certificate = self._tls_store.load(signer.public_identity, now=now)
            material = self._tls_store.create_unlock_material(
                signer, bind_host=self._bind_host, now=now
            )
            self._signer = signer
            self._authority_certificate = certificate
            self._material = material
            self._authority._activate_from_service(signer)
            try:
                self._server = await self._server_starter(
                    bind_host=self._bind_host,
                    port=self._port,
                    material=material,
                    handler=self._handle_record,
                    wall_clock=self._clock,
                )
            except BaseException:
                self._authority.lock()
                self._signer = None
                self._authority_certificate = None
                self._material = None
                material.close()
                raise

    async def lock(self) -> None:
        """Close connections first, then discard the signer and TLS lease."""

        async with self._lifecycle_lock:
            server = self._server
            material = self._material
            self._server = None
            if server is not None:
                await server.close()
            if material is not None:
                material.close()
            self._authority.lock()
            self._signer = None
            self._authority_certificate = None
            self._material = None

    async def close(self) -> None:
        try:
            await self.lock()
        finally:
            self._file_grants.clear()
            self._tty_reader.close()
            if self._owned_lease is not None:
                self._owned_lease.close()

    async def serve_foreground(self) -> None:
        """Run the closed owner command loop on the original controlling TTY."""

        try:
            while True:
                command = await asyncio.to_thread(
                    self._tty_reader._read_command, "Connector> "
                )
                try:
                    output, should_exit = await self._run_foreground_command(command)
                except ConnectorError as error:
                    output = f"{error.code}: {error.message}"
                    should_exit = (
                        command.strip() == "unlock"
                        and error.code == ConnectorErrorCode.CONNECTOR_KEY_LOCKED.value
                    )
                except ProtocolValidationError:
                    output = "Invalid Connector command."
                    should_exit = False
                self._tty_reader._write(f"{output}\n")
                if should_exit:
                    return
        finally:
            await self.close()

    async def _run_foreground_command(self, command: str) -> tuple[str, bool]:
        parts = command.strip().split()
        if not parts:
            return "", False
        if parts == ["status"]:
            status = "Connector locked." if self.is_locked else "Connector unlocked."
            return status, False
        if parts == ["unlock"]:
            await self.unlock()
            return "Connector unlocked.", False
        if parts == ["lock"]:
            await self.lock()
            return "Connector locked.", False
        if parts == ["pending"]:
            return _pending_pairings_output(self.pending_pairings()), False
        if len(parts) == 2 and parts[0] == "approve":
            self.approve_pairing(parts[1])
            return "Pairing approved.", False
        if len(parts) == 2 and parts[0] == "deny":
            self.deny_pairing(parts[1])
            return "Pairing denied.", False
        if parts == ["exit"]:
            return "Connector stopped.", True
        return "Invalid Connector command.", False

    def pending_pairing(self, pairing_id: str) -> PairingDisplay | None:
        """Return approval-safe metadata for a pending request on the local TTY."""

        state = self._store.pairing_state_by_id(pairing_id, now=self._clock())
        if state is None or state.decision != "pending":
            return None
        return _pairing_display(state)

    def pending_pairings(self) -> tuple[PairingDisplay, ...]:
        """Return the complete bounded local queue for owner review."""

        return tuple(
            _pairing_display(state)
            for state in self._store.pending_pairings(now=self._clock())
        )

    def approve_pairing(self, pairing_id: str) -> None:
        """Consume one pending pairing only after exact original-TTY confirmation."""

        signer = self._require_unlocked_signer()
        state = self._store.pairing_state_by_id(pairing_id, now=self._clock())
        if state is None or state.decision != "pending":
            raise ConnectorError(ConnectorErrorCode.REQUEST_REPLAYED)
        display = _pairing_display(state)
        if not self._tty_reader._confirm(_approval_prompt(display), "approve"):
            raise ConnectorError(ConnectorErrorCode.INTERACTIVE_UNLOCK_REQUIRED)
        now = self._clock()
        grant = _initial_grant(
            signer,
            state,
            policy_revision=self._store.current_policy_revision(),
            message_id=self._id_factory(),
            grant_id=self._id_factory(),
            now=now,
        )
        self._store.approve_pairing(
            pairing_id,
            device_id=self._id_factory(),
            transcript_digest=_transcript_digest(state),
            grant=grant,
            now=now,
        )

    def deny_pairing(self, pairing_id: str) -> None:
        """Deny one pending pairing only after exact original-TTY confirmation."""

        self._require_unlocked_signer()
        state = self._store.pairing_state_by_id(pairing_id, now=self._clock())
        if state is None or state.decision != "pending":
            raise ConnectorError(ConnectorErrorCode.REQUEST_REPLAYED)
        if not self._tty_reader._confirm(
            f"Deny {state.init.device_label} {state.init.pairing_id} (type deny): ",
            "deny",
        ):
            raise ConnectorError(ConnectorErrorCode.INTERACTIVE_UNLOCK_REQUIRED)
        self._store.deny_pairing(pairing_id, now=self._clock())

    def approve_local_file(
        self,
        path: Path,
        *,
        grant_id: str,
        source: str,
        operation: str,
        expires_at: int | None = None,
    ) -> FileGrant:
        """Display and approve one process-local transcription file on the TTY."""

        signer = self._require_unlocked_signer()
        context = self._file_grant_context(
            grant_id=grant_id,
            source=source,
            operation=operation,
            now=self._clock(),
        )
        row = self._model_policy.require_row(
            source=source,
            operation=operation,
            media_source_class="connector_local_file",
        )
        proposal = self._file_grants.propose(
            path,
            subject_key_id=context.subject_key_id,
            source=source,
            operation=operation,
            grant_revision=context.grant_revision,
            policy_revision=context.policy_revision,
            expires_at=expires_at,
            maximum_bytes=row.maximum_source_bytes,
        )
        prompt = _file_approval_prompt(context.device_label, proposal)
        if not self._tty_reader._confirm(prompt, "approve"):
            self._file_grants.discard(proposal)
            raise ConnectorError(ConnectorErrorCode.INTERACTIVE_UNLOCK_REQUIRED)
        try:
            current = self._file_grant_context(
                grant_id=grant_id,
                source=source,
                operation=operation,
                now=self._clock(),
            )
            signer_is_current = self._require_unlocked_signer() is signer
        except BaseException:
            self._file_grants.discard(proposal)
            raise
        if current != context or not signer_is_current:
            self._file_grants.discard(proposal)
            raise ConnectorError(ConnectorErrorCode.FILE_GRANT_INVALID)
        return self._file_grants.approve(
            proposal,
            signer=signer,
            message_id=self._id_factory(),
        )

    def _file_grant_context(
        self,
        *,
        grant_id: str,
        source: str,
        operation: str,
        now: int,
    ) -> _FileGrantContext:
        policy_revision = self._store.current_policy_revision()
        if policy_revision != self._model_policy.revision:
            raise ConnectorError(ConnectorErrorCode.MODEL_POLICY_DENIED)
        grants = tuple(
            grant
            for grant in self._store.inspect_grants()
            if grant.grant_id == grant_id
            and grant.superseded_at is None
            and grant.revoked_at is None
            and grant.not_before <= now < grant.expires_at
            and grant.policy_revision == policy_revision
            and any(
                scope.source == source and scope.operation == operation
                for scope in grant.scopes
            )
        )
        if len(grants) != 1:
            raise ConnectorError(ConnectorErrorCode.FILE_GRANT_INVALID)
        grant = grants[0]
        devices = tuple(
            device
            for device in self._store.inspect_devices()
            if device.device_id == grant.device_id
            and device.key_id == grant.subject_key_id
            and device.revoked_at is None
        )
        if len(devices) != 1:
            raise ConnectorError(ConnectorErrorCode.FILE_GRANT_INVALID)
        return _FileGrantContext(
            device_label=devices[0].label,
            subject_key_id=grant.subject_key_id,
            grant_revision=grant.revision,
            policy_revision=grant.policy_revision,
        )

    async def _handle_record(self, record: WireRecord) -> WireRecord:
        if not isinstance(record, PairingInit):
            return ErrorFrame(
                self._id_factory(), ConnectorErrorCode.CONNECTOR_PROTOCOL_MISMATCH
            )
        signer = self._signer
        certificate = self._authority_certificate
        material = self._material
        if (
            signer is None
            or certificate is None
            or material is None
            or self._server is None
        ):
            return ErrorFrame(
                self._id_factory(), ConnectorErrorCode.CONNECTOR_KEY_LOCKED
            )
        try:
            now = self._clock()
            state = self._store.pairing_state(record, now=now)
            if state is None:
                challenge = create_pairing_challenge(
                    signer=signer,
                    message_id=self._id_factory(),
                    pairing_id=record.pairing_id,
                    init_digest=record_digest(record),
                    vps_key_id=record.vps_key_id,
                    connector_nonce=secrets.token_bytes(32),
                    tls_ca_der=certificate.der,
                    tls_leaf_fingerprint=material.leaf_fingerprint,
                    issued_at=now,
                    deadline=record.deadline,
                )
                self._store.begin_pairing(record, challenge, now=now)
                return challenge
            if state.decision == "pending":
                return state.challenge
            if state.decision != "approved" or state.signed_grant is None:
                return ErrorFrame(
                    self._id_factory(), ConnectorErrorCode.CONNECTOR_NOT_PAIRED
                )
            complete = _pairing_complete(signer, state, self._id_factory())
            return create_pairing_resolution(
                message_id=self._id_factory(),
                pairing_id=record.pairing_id,
                signed_grant=state.signed_grant,
                pairing_complete=complete,
            )
        except (ConnectorError, ProtocolValidationError):
            return ErrorFrame(
                self._id_factory(), ConnectorErrorCode.CONNECTOR_PROTOCOL_MISMATCH
            )

    def _require_unlocked_signer(self) -> DevicePrivateIdentity:
        signer = self._signer
        if signer is None or self._server is None:
            raise ConnectorError(ConnectorErrorCode.CONNECTOR_KEY_LOCKED)
        return signer


def _require_initialization_directory(state_directory: Path) -> None:
    """Allow only the just-created persistent writer lock before first init."""

    try:
        if len(tuple(state_directory.iterdir())) != 1:
            raise ValueError("Connector state already exists")
    except (OSError, ValueError):
        raise ConnectorError(ConnectorErrorCode.CONNECTOR_STATE_INVALID) from None


def _pairing_display(state: PairingState) -> PairingDisplay:
    init = state.init
    device_identity = DevicePublicIdentity.from_wire(init.vps_public_key)
    transcript_hash = pairing_transcript_hash(
        encode_record(init),
        encode_record(state.challenge),
        observed_tls_leaf_fingerprint=state.challenge.tls_leaf_fingerprint,
    )
    return PairingDisplay(
        pairing_id=init.pairing_id,
        device_label=init.device_label,
        device_fingerprint=device_identity.fingerprint,
        sas=pairing_sas(transcript_hash),
        scopes=tuple(
            (scope.source, scope.operation, scope.data_scope)
            for scope in init.requested_scopes
        ),
        expires_at=init.grant_expires_at,
        max_uses=init.grant_max_uses,
    )


def _approval_prompt(display: PairingDisplay) -> str:
    scopes = ", ".join(
        f"{source}:{operation}:{data_scope}"
        for source, operation, data_scope in display.scopes
    )
    return (
        "Approve pairing\n"
        f"device: {display.device_label}\n"
        f"fingerprint: {display.device_fingerprint}\n"
        f"SAS: {display.sas}\n"
        f"scopes: {scopes}\n"
        f"expires_at: {display.expires_at}\n"
        f"max_uses: {display.max_uses}\n"
        "Type approve: "
    )


def _file_approval_prompt(device_label: str, proposal: FileGrantProposal) -> str:
    return (
        "Approve Connector-local transcription file\n"
        f"device: {device_label}\n"
        f"basename: {proposal.basename}\n"
        f"digest: {proposal.digest}\n"
        f"size: {proposal.size}\n"
        f"operation: {proposal.source}:{proposal.operation}\n"
        f"expires_at: {proposal.expires_at}\n"
        "Type approve: "
    )


def _pending_pairings_output(pairings: tuple[PairingDisplay, ...]) -> str:
    if not pairings:
        return "No pending pairings."
    return "\n".join(
        "\n".join(
            (
                f"pairing_id: {display.pairing_id}",
                f"device: {display.device_label}",
                f"fingerprint: {display.device_fingerprint}",
                f"SAS: {display.sas}",
                "scopes: "
                + ", ".join(
                    f"{source}:{operation}:{data_scope}"
                    for source, operation, data_scope in display.scopes
                ),
                f"expires_at: {display.expires_at}",
                f"max_uses: {display.max_uses}",
            )
        )
        for display in pairings
    )


def _initial_grant(
    signer: DevicePrivateIdentity,
    state: PairingState,
    *,
    policy_revision: int,
    message_id: str,
    grant_id: str,
    now: int,
) -> SignedGrant:
    init = state.init
    claims = GrantClaims(
        grant_id=grant_id,
        revision=1,
        issuer_key_id=signer.public_identity.key_id,
        subject_key_id=init.vps_key_id,
        issued_at=now,
        not_before=now,
        expires_at=init.grant_expires_at,
        policy_revision=policy_revision,
        max_uses=init.grant_max_uses,
        scopes=init.requested_scopes,
    )
    return create_signed_grant(signer, message_id=message_id, claims=claims)


def _pairing_complete(
    signer: DevicePrivateIdentity, state: PairingState, message_id: str
) -> PairingComplete:
    if (
        state.decision != "approved"
        or state.decided_at is None
        or state.transcript_digest is None
        or state.signed_grant is None
    ):
        raise ConnectorError(ConnectorErrorCode.CONNECTOR_STATE_INVALID)
    return create_pairing_complete(
        signer=signer,
        message_id=message_id,
        pairing_id=state.init.pairing_id,
        transcript_digest=state.transcript_digest,
        vps_key_id=state.init.vps_key_id,
        signed_grant_digest=record_digest(state.signed_grant),
        completed_at=state.decided_at,
    )


def _transcript_digest(state: PairingState) -> str:
    return pairing_transcript_hash(
        encode_record(state.init),
        encode_record(state.challenge),
        observed_tls_leaf_fingerprint=state.challenge.tls_leaf_fingerprint,
    ).hex()


def _new_id() -> str:
    return base64.b32encode(secrets.token_bytes(16)).decode("ascii").rstrip("=").lower()


def _wall_timestamp() -> int:
    return int(time.time())
