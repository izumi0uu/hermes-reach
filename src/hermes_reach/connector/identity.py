"""Reach-owned Ed25519 identities and owner-only key stores."""

from __future__ import annotations

import base64
import binascii
import getpass
import hashlib
import io
import json
import os
import secrets
import stat
import sys
import warnings
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Final, Never, Self, SupportsIndex

from cryptography import x509
from cryptography.exceptions import InvalidSignature, UnsupportedAlgorithm
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from .errors import ConnectorError, ConnectorErrorCode
from .limits import (
    KEY_ID_BASE32_LENGTH,
    MAX_TIMESTAMP_SECONDS,
    MIN_TIMESTAMP_SECONDS,
    SUPPORTED_CONNECTOR_PLATFORMS,
    SUPPORTED_VPS_PLATFORMS,
)

_DOMAIN_PREFIX: Final = b"hermes-reach:connector:v1:"
_MAX_KEY_FILE_BYTES: Final = 16 * 1024
_CONNECTOR_KEY_FILE: Final = "connector-identity.pem"
_VPS_KEY_FILE: Final = "vps-identity.pem"
_ENCRYPTED_PEM_HEADER: Final = b"-----BEGIN ENCRYPTED PRIVATE KEY-----\n"
_PLAIN_PEM_HEADER: Final = b"-----BEGIN PRIVATE KEY-----\n"
_MAX_TTY_COMMAND_CHARS: Final = 512


class DeviceRole(str, Enum):
    """Closed device roles with intentionally different storage policies."""

    CONNECTOR = "connector"
    VPS = "vps"


class SignatureDomain(str, Enum):
    """Record domains that cannot substitute signatures for one another."""

    PAIRING_INIT = "pairing-init"
    PAIRING_CHALLENGE = "pairing-challenge"
    PAIRING_COMPLETE = "pairing-complete"
    GRANT = "grant"
    REQUEST = "request"
    RECEIPT = "receipt"
    FILE_GRANT = "file-grant"
    KEY_ROTATION = "key-rotation"
    KEY_ROTATION_PROOF = "key-rotation-proof"


def domain_separated_bytes(domain: SignatureDomain, payload: bytes) -> bytes:
    """Return the exact bytes covered by an identity signature."""

    if not isinstance(domain, SignatureDomain) or not isinstance(payload, bytes):
        raise TypeError("Signatures require a closed domain and bytes payload.")
    return _DOMAIN_PREFIX + domain.value.encode("ascii") + b"\x00" + payload


@dataclass(frozen=True, slots=True)
class DevicePublicIdentity:
    """A canonical Ed25519 public identity safe to serialize on the wire."""

    raw_public_key: bytes

    def __post_init__(self) -> None:
        raw = bytes(self.raw_public_key)
        if len(raw) != 32:
            raise ValueError("Ed25519 public identities contain exactly 32 bytes.")
        object.__setattr__(self, "raw_public_key", raw)

    @classmethod
    def from_wire(cls, value: str) -> Self:
        """Decode only canonical unpadded base64url public keys."""

        if not isinstance(value, str) or len(value) != 43 or "=" in value:
            raise ValueError("The public identity encoding is invalid.")
        try:
            raw = base64.urlsafe_b64decode(value + "=")
        except (ValueError, binascii.Error) as error:
            raise ValueError("The public identity encoding is invalid.") from error
        identity = cls(raw)
        if identity.wire_public_key != value:
            raise ValueError("The public identity encoding is non-canonical.")
        return identity

    @property
    def wire_public_key(self) -> str:
        """Return canonical unpadded base64url public-key bytes."""

        return base64.urlsafe_b64encode(self.raw_public_key).decode("ascii").rstrip("=")

    @property
    def key_id(self) -> str:
        """Return the lowercase base32 identifier derived from 160 digest bits."""

        digest = hashlib.sha256(self.raw_public_key).digest()[:20]
        value = base64.b32encode(digest).decode("ascii").rstrip("=").lower()
        if len(value) != KEY_ID_BASE32_LENGTH:
            raise RuntimeError("The key identifier invariant is invalid.")
        return value

    @property
    def fingerprint(self) -> str:
        """Return the complete SHA-256 digest in fixed human-checkable groups."""

        digest = hashlib.sha256(self.raw_public_key).hexdigest()
        return "sha256:" + "-".join(
            digest[index : index + 4] for index in range(0, len(digest), 4)
        )

    def verify(
        self,
        domain: SignatureDomain,
        payload: bytes,
        signature: bytes,
    ) -> bool:
        """Verify an exact-domain signature without exposing backend exceptions."""

        if not isinstance(signature, bytes) or len(signature) != 64:
            return False
        try:
            public_key = Ed25519PublicKey.from_public_bytes(self.raw_public_key)
            public_key.verify(signature, domain_separated_bytes(domain, payload))
        except (InvalidSignature, TypeError, ValueError):
            return False
        return True


class DevicePrivateIdentity:
    """A non-serializable Ed25519 signer with a constant redacted display."""

    __slots__ = ("_private_key",)

    def __init__(self, private_key: Ed25519PrivateKey) -> None:
        if not isinstance(private_key, Ed25519PrivateKey):
            raise TypeError("Device identities require an Ed25519 private key.")
        self._private_key = private_key

    @classmethod
    def generate(cls) -> Self:
        """Generate a fresh device identity."""

        return cls(Ed25519PrivateKey.generate())

    @classmethod
    def _from_seed_for_testing(cls, seed: bytes) -> Self:
        if len(seed) != 32:
            raise ValueError("Ed25519 seeds contain exactly 32 bytes.")
        return cls(Ed25519PrivateKey.from_private_bytes(seed))

    @property
    def public_identity(self) -> DevicePublicIdentity:
        """Return the corresponding public identity."""

        raw = self._private_key.public_key().public_bytes(
            serialization.Encoding.Raw,
            serialization.PublicFormat.Raw,
        )
        return DevicePublicIdentity(raw)

    def sign(self, domain: SignatureDomain, payload: bytes) -> bytes:
        """Sign one immutable payload in its exact record domain."""

        return self._private_key.sign(domain_separated_bytes(domain, payload))

    def _private_bytes(self, password: bytes | None) -> bytes:
        encryption: serialization.KeySerializationEncryption
        if password is None:
            encryption = serialization.NoEncryption()
        else:
            if not password:
                raise ValueError("Connector key passphrases cannot be empty.")
            encryption = serialization.BestAvailableEncryption(password)
        return self._private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            encryption,
        )

    def _sign_certificate(self, builder: x509.CertificateBuilder) -> x509.Certificate:
        """Sign an internal TLS certificate without exposing the private key."""

        if not isinstance(builder, x509.CertificateBuilder):
            raise TypeError("Certificate signing requires an X.509 builder.")
        return builder.sign(private_key=self._private_key, algorithm=None)

    def __repr__(self) -> str:
        return "DevicePrivateIdentity(<redacted>)"

    __str__ = __repr__


class _KeyPassphrase:
    """Short-lived passphrase storage with a constant redacted display."""

    __slots__ = ("_buffer", "_closed")

    def __init__(self, value: str | bytes) -> None:
        self._buffer = bytearray()
        self._closed = True
        if isinstance(value, str):
            self._buffer = bytearray(value, "utf-8")
        elif isinstance(value, bytes):
            self._buffer = bytearray(value)
        else:
            raise TypeError("Key passphrases must be text or bytes.")
        if not self._buffer:
            raise ValueError("Key passphrases cannot be empty.")
        self._closed = False

    def _as_bytes(self) -> bytes:
        if self._closed:
            raise ValueError("The key passphrase is no longer available.")
        return bytes(self._buffer)

    def close(self) -> None:
        if self._closed:
            return
        for index in range(len(self._buffer)):
            self._buffer[index] = 0
        self._buffer.clear()
        self._closed = True

    def __repr__(self) -> str:
        return "KeyPassphrase(<redacted>)"

    __str__ = __repr__

    def __reduce_ex__(self, protocol: SupportsIndex) -> Never:
        del protocol
        raise TypeError("Key passphrases cannot be serialized.")

    def __del__(self) -> None:
        self.close()


class TtyPassphraseReader:
    """Capture and retain the foreground process's original controlling TTY."""

    _terminal: object
    _terminal_identity: tuple[int, int, int] | None
    _prompt: Callable[..., str]
    _test_only: bool
    _closed: bool

    def __init__(self) -> None:
        terminal, identity = _open_controlling_terminal()
        self._terminal = terminal
        self._terminal_identity = identity
        self._prompt = getpass.getpass
        self._test_only = False
        self._closed = False

    @classmethod
    def _from_test_terminal(
        cls,
        terminal: object,
        *,
        prompt: Callable[..., str],
    ) -> TtyPassphraseReader:
        """Build the distinct private reader accepted only by test seams."""

        del cls
        return _TestTtyPassphraseReader(terminal, prompt=prompt)

    def read(self, prompt: str) -> _KeyPassphrase:
        """Reject non-TTY input and getpass's insecure stdin fallback."""

        self._validate_terminal()
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", getpass.GetPassWarning)
                value = self._prompt(prompt=prompt, stream=self._terminal)
        except (EOFError, getpass.GetPassWarning, OSError, TypeError):
            raise ConnectorError(
                ConnectorErrorCode.INTERACTIVE_UNLOCK_REQUIRED
            ) from None
        self._validate_terminal()
        if not isinstance(value, str) or not value:
            raise ConnectorError(ConnectorErrorCode.CONNECTOR_KEY_LOCKED)
        return _KeyPassphrase(value)

    def close(self) -> None:
        """Release the captured descriptor without changing terminal state."""

        if getattr(self, "_closed", True):
            return
        self._closed = True
        if not self._test_only and isinstance(self._terminal, io.TextIOBase):
            try:
                self._terminal.close()
            except OSError:
                pass

    def _confirm(self, prompt: str, expected: str) -> bool:
        """Read one explicit non-secret confirmation from the captured TTY."""

        if type(prompt) is not str or type(expected) is not str or not expected:
            raise ValueError("TTY confirmation parameters are invalid.")
        self._validate_terminal()
        terminal = self._terminal
        if not isinstance(terminal, io.TextIOBase):
            raise ConnectorError(ConnectorErrorCode.INTERACTIVE_UNLOCK_REQUIRED)
        try:
            terminal.write(prompt)
            terminal.flush()
            value = terminal.readline(len(expected) + 2)
        except (OSError, TypeError, ValueError):
            raise ConnectorError(
                ConnectorErrorCode.INTERACTIVE_UNLOCK_REQUIRED
            ) from None
        self._validate_terminal()
        return value == f"{expected}\n"

    def _read_command(self, prompt: str) -> str:
        """Read one bounded non-secret command from the captured TTY."""

        if type(prompt) is not str:
            raise ValueError("TTY command prompt parameters are invalid.")
        self._validate_terminal()
        terminal = self._terminal
        if not isinstance(terminal, io.TextIOBase):
            raise ConnectorError(ConnectorErrorCode.INTERACTIVE_UNLOCK_REQUIRED)
        try:
            terminal.write(prompt)
            terminal.flush()
            value = terminal.readline(_MAX_TTY_COMMAND_CHARS + 1)
        except (OSError, TypeError, ValueError):
            raise ConnectorError(
                ConnectorErrorCode.INTERACTIVE_UNLOCK_REQUIRED
            ) from None
        self._validate_terminal()
        if not isinstance(value, str) or not value:
            raise ConnectorError(ConnectorErrorCode.INTERACTIVE_UNLOCK_REQUIRED)
        return value

    def _write(self, value: str) -> None:
        """Render non-secret service output on the captured original TTY."""

        if type(value) is not str:
            raise ValueError("TTY output must be text.")
        self._validate_terminal()
        terminal = self._terminal
        if not isinstance(terminal, io.TextIOBase):
            raise ConnectorError(ConnectorErrorCode.INTERACTIVE_UNLOCK_REQUIRED)
        try:
            terminal.write(value)
            terminal.flush()
        except (OSError, TypeError, ValueError):
            raise ConnectorError(
                ConnectorErrorCode.INTERACTIVE_UNLOCK_REQUIRED
            ) from None
        self._validate_terminal()

    def _validate_terminal(self) -> None:
        if self._closed:
            raise ConnectorError(ConnectorErrorCode.INTERACTIVE_UNLOCK_REQUIRED)
        if self._test_only:
            isatty = getattr(self._terminal, "isatty", None)
            if not callable(isatty) or not isatty():
                raise ConnectorError(ConnectorErrorCode.INTERACTIVE_UNLOCK_REQUIRED)
            return
        try:
            if not isinstance(self._terminal, io.TextIOBase):
                raise TypeError("The terminal handle is invalid.")
            descriptor = self._terminal.fileno()
            identity = _terminal_identity(descriptor)
        except (AttributeError, OSError, TypeError, ValueError):
            raise ConnectorError(
                ConnectorErrorCode.INTERACTIVE_UNLOCK_REQUIRED
            ) from None
        if identity != self._terminal_identity:
            raise ConnectorError(ConnectorErrorCode.INTERACTIVE_UNLOCK_REQUIRED)
        probe = -1
        try:
            probe = os.open("/dev/tty", _terminal_open_flags())
            if _terminal_identity(probe) != self._terminal_identity:
                raise ConnectorError(ConnectorErrorCode.INTERACTIVE_UNLOCK_REQUIRED)
        except ConnectorError:
            raise
        except (OSError, TypeError, ValueError):
            raise ConnectorError(
                ConnectorErrorCode.INTERACTIVE_UNLOCK_REQUIRED
            ) from None
        finally:
            if probe >= 0:
                os.close(probe)

    def __del__(self) -> None:
        self.close()


class _TestTtyPassphraseReader(TtyPassphraseReader):
    """Hermetic reader type that production key and service APIs reject."""

    def __init__(self, terminal: object, *, prompt: Callable[..., str]) -> None:
        self._terminal = terminal
        self._terminal_identity = None
        self._prompt = prompt
        self._test_only = True
        self._closed = False


class ConnectorKeyStore:
    """Passphrase-encrypted Connector identity beneath an owner-only directory."""

    def __init__(self, state_directory: Path, *, _platform: str | None = None) -> None:
        self._state_directory = state_directory
        self._platform = sys.platform if _platform is None else _platform

    def initialize_from_tty(self, reader: TtyPassphraseReader) -> DevicePublicIdentity:
        """Create a Connector identity without accepting a passphrase DTO."""

        self._ensure_platform()
        _require_tty_reader(reader)
        return self._initialize_from_tty(reader)

    def _initialize_from_tty_for_testing(
        self, reader: TtyPassphraseReader
    ) -> DevicePublicIdentity:
        """Exercise encrypted-key creation through the private test reader seam."""

        self._ensure_platform()
        _require_test_tty_reader(reader)
        return self._initialize_from_tty(reader)

    def _initialize_from_tty(self, reader: TtyPassphraseReader) -> DevicePublicIdentity:
        passphrase = _read_passphrase(reader, "New Connector passphrase: ")
        try:
            identity = DevicePrivateIdentity.generate()
            payload = identity._private_bytes(passphrase._as_bytes())
        finally:
            passphrase.close()
        _create_key_file(self._state_directory, _CONNECTOR_KEY_FILE, payload)
        return identity.public_identity

    def unlock_from_tty(
        self,
        reader: TtyPassphraseReader,
        *,
        attempts: int = 3,
    ) -> DevicePrivateIdentity:
        """Unlock only through the foreground TTY, with a fixed attempt cap."""

        self._ensure_platform()
        _require_tty_reader(reader)
        return self._unlock_from_tty(reader, attempts=attempts)

    def _unlock_from_tty_for_testing(
        self,
        reader: TtyPassphraseReader,
        *,
        attempts: int = 3,
    ) -> DevicePrivateIdentity:
        """Exercise encrypted-key unlock through the private test reader seam."""

        self._ensure_platform()
        _require_test_tty_reader(reader)
        return self._unlock_from_tty(reader, attempts=attempts)

    def _unlock_from_tty(
        self,
        reader: TtyPassphraseReader,
        *,
        attempts: int,
    ) -> DevicePrivateIdentity:
        if attempts != 3:
            raise ValueError("Connector unlock uses exactly three attempts.")
        payload = _read_key_file(self._state_directory, _CONNECTOR_KEY_FILE)
        if not payload.startswith(_ENCRYPTED_PEM_HEADER):
            raise ConnectorError(ConnectorErrorCode.CONNECTOR_KEY_LOCKED)
        for _ in range(attempts):
            try:
                passphrase = _read_passphrase(reader, "Connector passphrase: ")
                try:
                    return _load_private_identity(
                        payload, password=passphrase._as_bytes()
                    )
                finally:
                    passphrase.close()
            except ConnectorError as error:
                if error.code == ConnectorErrorCode.INTERACTIVE_UNLOCK_REQUIRED.value:
                    raise
            except (TypeError, UnsupportedAlgorithm, ValueError):
                pass
        raise ConnectorError(ConnectorErrorCode.CONNECTOR_KEY_LOCKED)

    def _ensure_platform(self) -> None:
        if self._platform not in SUPPORTED_CONNECTOR_PLATFORMS:
            raise ConnectorError(ConnectorErrorCode.UNSUPPORTED_PLATFORM)


class VpsKeyStore:
    """Unattended VPS identity with owner-only, unencrypted PKCS#8 storage."""

    def __init__(self, state_directory: Path, *, _platform: str | None = None) -> None:
        self._state_directory = state_directory
        self._platform = sys.platform if _platform is None else _platform

    def initialize(self) -> DevicePublicIdentity:
        """Create an unattended VPS identity once."""

        self._ensure_platform()
        identity = DevicePrivateIdentity.generate()
        _create_key_file(
            self._state_directory,
            _VPS_KEY_FILE,
            identity._private_bytes(None),
        )
        return identity.public_identity

    def load(self) -> DevicePrivateIdentity:
        """Load the existing VPS identity without silently regenerating it."""

        self._ensure_platform()
        payload = _read_key_file(self._state_directory, _VPS_KEY_FILE)
        if not payload.startswith(_PLAIN_PEM_HEADER):
            raise ConnectorError(ConnectorErrorCode.CONNECTOR_NOT_INITIALIZED)
        try:
            return _load_private_identity(payload, password=None)
        except (TypeError, UnsupportedAlgorithm, ValueError):
            raise ConnectorError(ConnectorErrorCode.CONNECTOR_NOT_INITIALIZED) from None

    def _ensure_platform(self) -> None:
        if self._platform not in SUPPORTED_VPS_PLATFORMS:
            raise ConnectorError(ConnectorErrorCode.UNSUPPORTED_PLATFORM)


@dataclass(frozen=True, slots=True)
class RotationStatementV1:
    """Canonical continuity claim from one device key to its replacement."""

    role: DeviceRole
    old_key_id: str
    new_key_id: str
    new_public_key: str
    sequence: int
    issued_at: int
    expires_at: int
    version: str = "v1"

    def __post_init__(self) -> None:
        if self.version != "v1" or not isinstance(self.role, DeviceRole):
            raise ValueError("The key rotation version or role is invalid.")
        if not _is_canonical_key_id(self.old_key_id) or not _is_canonical_key_id(
            self.new_key_id
        ):
            raise ValueError("The key rotation identity is invalid.")
        try:
            new_identity = DevicePublicIdentity.from_wire(self.new_public_key)
        except ValueError as error:
            raise ValueError("The key rotation identity is invalid.") from error
        if new_identity.key_id != self.new_key_id or self.old_key_id == self.new_key_id:
            raise ValueError("The key rotation identity is invalid.")
        if (
            type(self.sequence) is not int
            or self.sequence <= 0
            or type(self.issued_at) is not int
            or type(self.expires_at) is not int
            or not MIN_TIMESTAMP_SECONDS <= self.issued_at <= MAX_TIMESTAMP_SECONDS
            or not MIN_TIMESTAMP_SECONDS <= self.expires_at <= MAX_TIMESTAMP_SECONDS
            or self.expires_at <= self.issued_at
        ):
            raise ValueError("The key rotation bounds are invalid.")

    def canonical_bytes(self) -> bytes:
        """Return the deterministic closed statement encoding."""

        return json.dumps(
            {
                "expires_at": self.expires_at,
                "issued_at": self.issued_at,
                "new_key_id": self.new_key_id,
                "new_public_key": self.new_public_key,
                "old_key_id": self.old_key_id,
                "role": self.role.value,
                "sequence": self.sequence,
                "version": self.version,
            },
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")


@dataclass(frozen=True, slots=True)
class SignedRotationV1:
    """Old-key continuity signature plus proof of the replacement key."""

    statement: RotationStatementV1
    old_signature: bytes
    new_key_proof: bytes


def create_signed_rotation(
    old_identity: DevicePrivateIdentity,
    new_identity: DevicePrivateIdentity,
    *,
    role: DeviceRole,
    sequence: int,
    issued_at: int,
    expires_at: int,
) -> SignedRotationV1:
    """Create a rotation that proves control of both device keys."""

    if (
        not isinstance(old_identity, DevicePrivateIdentity)
        or not isinstance(new_identity, DevicePrivateIdentity)
        or not isinstance(role, DeviceRole)
        or type(sequence) is not int
        or type(issued_at) is not int
        or type(expires_at) is not int
    ):
        raise TypeError("The key rotation inputs are invalid.")
    if sequence <= 0 or issued_at < 0 or expires_at <= issued_at:
        raise ValueError("The key rotation bounds are invalid.")
    statement = RotationStatementV1(
        role=role,
        old_key_id=old_identity.public_identity.key_id,
        new_key_id=new_identity.public_identity.key_id,
        new_public_key=new_identity.public_identity.wire_public_key,
        sequence=sequence,
        issued_at=issued_at,
        expires_at=expires_at,
    )
    payload = statement.canonical_bytes()
    return SignedRotationV1(
        statement,
        old_identity.sign(SignatureDomain.KEY_ROTATION, payload),
        new_identity.sign(SignatureDomain.KEY_ROTATION_PROOF, payload),
    )


def verify_signed_rotation(
    rotation: SignedRotationV1,
    *,
    pinned_identity: DevicePublicIdentity,
    expected_role: DeviceRole,
    current_sequence: int,
    now: datetime,
) -> DevicePublicIdentity | None:
    """Return the proven replacement identity or fail without a recovery path."""

    if (
        not isinstance(rotation, SignedRotationV1)
        or not isinstance(pinned_identity, DevicePublicIdentity)
        or not isinstance(expected_role, DeviceRole)
        or type(current_sequence) is not int
        or current_sequence < 0
        or not isinstance(now, datetime)
        or now.tzinfo is None
        or not isinstance(rotation.statement, RotationStatementV1)
        or not isinstance(rotation.old_signature, bytes)
        or not isinstance(rotation.new_key_proof, bytes)
    ):
        return None
    statement = rotation.statement
    try:
        now_seconds = int(now.astimezone(UTC).timestamp())
    except (OSError, OverflowError, ValueError):
        return None
    if (
        statement.version != "v1"
        or statement.role != expected_role
        or statement.old_key_id != pinned_identity.key_id
        or statement.sequence != current_sequence + 1
        or not statement.issued_at <= now_seconds < statement.expires_at
    ):
        return None
    try:
        new_identity = DevicePublicIdentity.from_wire(statement.new_public_key)
    except ValueError:
        return None
    if new_identity.key_id != statement.new_key_id:
        return None
    payload = statement.canonical_bytes()
    if not pinned_identity.verify(
        SignatureDomain.KEY_ROTATION, payload, rotation.old_signature
    ):
        return None
    if not new_identity.verify(
        SignatureDomain.KEY_ROTATION_PROOF, payload, rotation.new_key_proof
    ):
        return None
    return new_identity


def _load_private_identity(
    payload: bytes,
    *,
    password: bytes | None,
) -> DevicePrivateIdentity:
    key = serialization.load_pem_private_key(payload, password=password)
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError("The private key type is invalid.")
    return DevicePrivateIdentity(key)


def _read_passphrase(reader: TtyPassphraseReader, prompt: str) -> _KeyPassphrase:
    return reader.read(prompt)


def _require_tty_reader(reader: object) -> None:
    if type(reader) is not TtyPassphraseReader:
        raise ConnectorError(ConnectorErrorCode.INTERACTIVE_UNLOCK_REQUIRED)


def _require_test_tty_reader(reader: object) -> None:
    if type(reader) is not _TestTtyPassphraseReader:
        raise ConnectorError(ConnectorErrorCode.INTERACTIVE_UNLOCK_REQUIRED)


def _terminal_open_flags() -> int:
    if sys.platform not in SUPPORTED_CONNECTOR_PLATFORMS:
        raise ConnectorError(ConnectorErrorCode.UNSUPPORTED_PLATFORM)
    return os.O_RDWR | os.O_CLOEXEC | os.O_NOCTTY | os.O_NOFOLLOW


def _terminal_identity(descriptor: int) -> tuple[int, int, int]:
    status = os.fstat(descriptor)
    if (
        not stat.S_ISCHR(status.st_mode)
        or not os.isatty(descriptor)
        or os.tcgetpgrp(descriptor) != os.getpgrp()
    ):
        raise ConnectorError(ConnectorErrorCode.INTERACTIVE_UNLOCK_REQUIRED)
    return status.st_dev, status.st_ino, status.st_rdev


def _open_controlling_terminal() -> tuple[object, tuple[int, int, int]]:
    descriptor = -1
    try:
        descriptor = os.open("/dev/tty", _terminal_open_flags())
        identity = _terminal_identity(descriptor)
        terminal = os.fdopen(
            descriptor,
            "r+",
            buffering=1,
            encoding="utf-8",
            errors="strict",
            newline=None,
        )
        descriptor = -1
        return terminal, identity
    except ConnectorError:
        raise
    except (OSError, TypeError, ValueError):
        raise ConnectorError(ConnectorErrorCode.INTERACTIVE_UNLOCK_REQUIRED) from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _is_canonical_key_id(value: object) -> bool:
    if not isinstance(value, str) or len(value) != KEY_ID_BASE32_LENGTH:
        return False
    try:
        raw = base64.b32decode(value.upper(), casefold=False)
    except (ValueError, binascii.Error):
        return False
    return (
        len(raw) == 20
        and base64.b32encode(raw).decode("ascii").rstrip("=").lower() == value
    )


def _create_key_file(state_directory: Path, filename: str, payload: bytes) -> None:
    try:
        directory_fd = _open_state_directory(state_directory, create=True)
    except (FileNotFoundError, OSError, ValueError):
        raise ConnectorError(ConnectorErrorCode.CONNECTOR_NOT_INITIALIZED) from None
    temporary_name = f".{filename}.{secrets.token_hex(8)}.tmp"
    descriptor = -1
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
        descriptor = os.open(temporary_name, flags, 0o600, dir_fd=directory_fd)
        os.fchmod(descriptor, 0o600)
        _write_all(descriptor, payload)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.link(
            temporary_name,
            filename,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
            follow_symlinks=False,
        )
        os.unlink(temporary_name, dir_fd=directory_fd)
        os.fsync(directory_fd)
    except (FileExistsError, OSError):
        try:
            os.unlink(temporary_name, dir_fd=directory_fd)
        except OSError:
            pass
        raise ConnectorError(ConnectorErrorCode.CONNECTOR_NOT_INITIALIZED) from None
    finally:
        if descriptor != -1:
            os.close(descriptor)
        os.close(directory_fd)


def _read_key_file(state_directory: Path, filename: str) -> bytes:
    try:
        directory_fd = _open_state_directory(state_directory, create=False)
    except (FileNotFoundError, OSError, ValueError):
        raise ConnectorError(ConnectorErrorCode.CONNECTOR_NOT_INITIALIZED) from None
    descriptor = -1
    try:
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
        descriptor = os.open(filename, flags, dir_fd=directory_fd)
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_nlink != 1
            or not 0 < metadata.st_size <= _MAX_KEY_FILE_BYTES
        ):
            raise OSError("unsafe key file")
        payload = _read_bounded(descriptor, metadata.st_size)
        if len(payload) != metadata.st_size:
            raise OSError("short key read")
        return payload
    except (FileNotFoundError, OSError):
        raise ConnectorError(ConnectorErrorCode.CONNECTOR_NOT_INITIALIZED) from None
    finally:
        if descriptor != -1:
            os.close(descriptor)
        os.close(directory_fd)


def _open_state_directory(path: Path, *, create: bool) -> int:
    if not path.is_absolute() or not path.parts or path.parts[0] != os.sep:
        raise ValueError("The Connector state directory must be absolute.")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    descriptor = os.open(os.sep, flags)
    try:
        parts = path.parts[1:]
        for index, part in enumerate(parts):
            if not part or part in {".", ".."}:
                raise ValueError("The Connector state directory is invalid.")
            is_final = index == len(parts) - 1
            if is_final and create:
                try:
                    os.mkdir(part, 0o700, dir_fd=descriptor)
                except FileExistsError:
                    pass
            next_descriptor = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o700
        ):
            raise OSError("unsafe state directory")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _write_all(descriptor: int, payload: bytes) -> None:
    offset = 0
    while offset < len(payload):
        written = os.write(descriptor, payload[offset:])
        if written <= 0:
            raise OSError("key write failed")
        offset += written


def _read_bounded(descriptor: int, expected_size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = expected_size
    while remaining:
        chunk = os.read(descriptor, min(remaining, 4096))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)
