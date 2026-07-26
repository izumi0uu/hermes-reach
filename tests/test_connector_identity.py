from __future__ import annotations

import getpass
import inspect
import io
import json
import os
import stat
import warnings
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.rsa import generate_private_key

import hermes_reach.connector.identity as identity_module
from hermes_reach.connector.errors import ConnectorError, ConnectorErrorCode
from hermes_reach.connector.identity import (
    ConnectorKeyStore,
    DevicePrivateIdentity,
    DevicePublicIdentity,
    DeviceRole,
    RotationStatementV1,
    SignatureDomain,
    SignedRotationV1,
    TtyPassphraseReader,
    VpsKeyStore,
    create_signed_rotation,
    domain_separated_bytes,
    verify_signed_rotation,
)

RFC8032_SEED = bytes.fromhex(
    "9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60"
)
RFC8032_PUBLIC = bytes.fromhex(
    "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a"
)
RFC8032_WIRE = "11qYAYKxCrfVS_7TyWQHOg7hcvPapiMlrwIaaPcHURo"
RFC8032_KEY_ID = "eh7ddx5bksrgcytl7bkai36se4nxx3kl"
RFC8032_FINGERPRINT = (
    "sha256:21fe-31df-a154-a261-626b-f854-046f-d227-"
    "1b7b-ed4b-6abe-45aa-5887-7ef4-7f97-21b9"
)
VECTOR_PAYLOAD = b'{"sequence":1}'
VECTOR_SIGNED_BYTES = b"hermes-reach:connector:v1:request\x00" + VECTOR_PAYLOAD
VECTOR_SIGNATURE = bytes.fromhex(
    "6972ecc06b137d9cbd3cb8ad4d881bc9e7895d792a5ec2c87ae63851b772e60a"
    "e619f992af3f2cbf3f8e22a36f822120bbee5e992aa7cacf5629f492b0bfed01"
)
PASSPHRASE = b"PASSPHRASE_CANARY-correct horse battery staple"


class _FixedPrompt:
    def __init__(self, *values: bytes) -> None:
        self._values = iter(values)
        self.calls = 0

    def __call__(self, *, prompt: str, stream: object) -> str:
        assert "passphrase" in prompt.lower()
        assert stream is not None
        self.calls += 1
        return next(self._values).decode("utf-8")


class _StructuralReader:
    def read(self, prompt: str) -> bytes:
        del prompt
        return PASSPHRASE


class _Terminal:
    def __init__(self, *, interactive: bool) -> None:
        self._interactive = interactive

    def isatty(self) -> bool:
        return self._interactive


class _CapturedTerminal(io.StringIO):
    def __init__(self, descriptor: int) -> None:
        super().__init__()
        self._descriptor = descriptor

    def fileno(self) -> int:
        return self._descriptor


class _ConfirmationTerminal(io.TextIOBase):
    def __init__(self, value: str) -> None:
        self._input = io.StringIO(value)
        self.output = io.StringIO()

    def isatty(self) -> bool:
        return True

    def write(self, value: str) -> int:
        return self.output.write(value)

    def flush(self) -> None:
        return None

    def readline(self, size: int = -1) -> str:
        return self._input.readline(size)


def _fixed_tty_reader(*values: bytes) -> TtyPassphraseReader:
    return TtyPassphraseReader._from_test_terminal(
        _Terminal(interactive=True),
        prompt=_FixedPrompt(*values),
    )


def _identity(seed: bytes = RFC8032_SEED) -> DevicePrivateIdentity:
    return DevicePrivateIdentity._from_seed_for_testing(seed)


def _assert_code(error: pytest.ExceptionInfo[ConnectorError], code: str) -> None:
    assert error.value.code == code
    assert "CANARY" not in str(error.value)
    assert "CANARY" not in repr(error.value)


def _only_file(directory: Path) -> Path:
    files = tuple(path for path in directory.iterdir() if path.is_file())
    assert len(files) == 1
    return files[0]


def test_rfc8032_identity_vector_pins_every_public_representation() -> None:
    identity = _identity()
    public = identity.public_identity

    assert public.raw_public_key == RFC8032_PUBLIC
    assert public.wire_public_key == RFC8032_WIRE
    assert public.key_id == RFC8032_KEY_ID
    assert public.fingerprint == RFC8032_FINGERPRINT
    assert len(public.wire_public_key) == 43
    assert len(public.key_id) == 32
    assert DevicePublicIdentity.from_wire(RFC8032_WIRE) == public


def test_signature_vector_pins_domain_separator_and_signature_bytes() -> None:
    identity = _identity()

    assert (
        domain_separated_bytes(SignatureDomain.REQUEST, VECTOR_PAYLOAD)
        == VECTOR_SIGNED_BYTES
    )
    signature = identity.sign(SignatureDomain.REQUEST, VECTOR_PAYLOAD)
    assert signature == VECTOR_SIGNATURE
    assert identity.public_identity.verify(
        SignatureDomain.REQUEST, VECTOR_PAYLOAD, signature
    )


def test_signatures_reject_wrong_domain_payload_signature_and_key() -> None:
    identity = _identity()
    signature = identity.sign(SignatureDomain.REQUEST, VECTOR_PAYLOAD)
    other = _identity(bytes(range(32)))

    assert not identity.public_identity.verify(
        SignatureDomain.RECEIPT, VECTOR_PAYLOAD, signature
    )
    assert not identity.public_identity.verify(
        SignatureDomain.REQUEST, VECTOR_PAYLOAD + b" ", signature
    )
    assert not identity.public_identity.verify(
        SignatureDomain.REQUEST, VECTOR_PAYLOAD, signature[:-1] + b"\x00"
    )
    assert not other.public_identity.verify(
        SignatureDomain.REQUEST, VECTOR_PAYLOAD, signature
    )
    assert not identity.public_identity.verify(
        SignatureDomain.REQUEST, VECTOR_PAYLOAD, b"short"
    )


@pytest.mark.parametrize(
    "wire",
    [
        RFC8032_WIRE + "=",
        RFC8032_WIRE[:-1],
        RFC8032_WIRE + "A",
        "+" + RFC8032_WIRE[1:],
        "/" + RFC8032_WIRE[1:],
        "!" + RFC8032_WIRE[1:],
        RFC8032_WIRE[:-1] + "p",  # Same decoded bytes, nonzero unused low bits.
        RFC8032_WIRE[:-1] + "\n",
        "\N{LATIN SMALL LETTER E WITH ACUTE}" + RFC8032_WIRE[1:],
    ],
)
def test_public_identity_rejects_noncanonical_base64url(wire: str) -> None:
    with pytest.raises(ValueError, match="public identity encoding"):
        DevicePublicIdentity.from_wire(wire)


@pytest.mark.parametrize("raw", [b"", b"x" * 31, b"x" * 33])
def test_public_identity_requires_exactly_32_raw_bytes(raw: bytes) -> None:
    with pytest.raises(ValueError, match="exactly 32 bytes"):
        DevicePublicIdentity(raw)


def test_private_identity_repr_and_json_never_expose_private_material() -> None:
    identity = _identity()
    displays = (str(identity), repr(identity))

    assert all(RFC8032_SEED.hex() not in display for display in displays)
    assert all("redacted" in display.lower() for display in displays)
    with pytest.raises(TypeError):
        json.dumps(identity)
    assert not hasattr(identity, "private_bytes")


def test_tty_reader_uses_only_supplied_terminal_and_redacts_result() -> None:
    terminal = _Terminal(interactive=True)
    seen: dict[str, object] = {}

    def prompt(*, prompt: str, stream: object) -> str:
        seen.update(prompt=prompt, stream=stream)
        return PASSPHRASE.decode()

    secret = TtyPassphraseReader._from_test_terminal(terminal, prompt=prompt).read(
        "Passphrase: "
    )

    assert seen == {"prompt": "Passphrase: ", "stream": terminal}
    assert PASSPHRASE.decode() not in repr(secret)
    with pytest.raises(TypeError):
        json.dumps(secret)


def test_tty_reader_rejects_pipe_without_calling_prompt() -> None:
    called = False

    def prompt(**_: object) -> str:
        nonlocal called
        called = True
        return PASSPHRASE.decode()

    reader = TtyPassphraseReader._from_test_terminal(
        _Terminal(interactive=False), prompt=prompt
    )
    with pytest.raises(ConnectorError) as caught:
        reader.read("Passphrase: ")
    _assert_code(caught, ConnectorErrorCode.INTERACTIVE_UNLOCK_REQUIRED.value)
    assert not called


@pytest.mark.parametrize(
    ("value", "accepted"),
    [
        ("approve\n", True),
        ("approve", False),
        ("approve \n", False),
        ("Approve\n", False),
        ("deny\n", False),
    ],
)
def test_tty_confirmation_requires_one_exact_line(value: str, accepted: bool) -> None:
    terminal = _ConfirmationTerminal(value)
    reader = TtyPassphraseReader._from_test_terminal(
        terminal, prompt=_FixedPrompt(PASSPHRASE)
    )

    assert reader._confirm("Confirm: ", "approve") is accepted
    assert terminal.output.getvalue() == "Confirm: "


def test_tty_confirmation_rejects_noninteractive_input() -> None:
    reader = TtyPassphraseReader._from_test_terminal(
        _Terminal(interactive=False), prompt=_FixedPrompt(PASSPHRASE)
    )

    with pytest.raises(ConnectorError) as caught:
        reader._confirm("Confirm: ", "approve")

    _assert_code(caught, ConnectorErrorCode.INTERACTIVE_UNLOCK_REQUIRED.value)


def test_tty_command_and_output_stay_on_the_captured_terminal() -> None:
    terminal = _ConfirmationTerminal("status\n")
    reader = TtyPassphraseReader._from_test_terminal(
        terminal, prompt=_FixedPrompt(PASSPHRASE)
    )

    assert reader._read_command("Connector> ") == "status\n"
    reader._write("Connector locked.\n")
    assert terminal.output.getvalue() == "Connector> Connector locked.\n"


def test_tty_reader_rejects_getpass_fallback_warning() -> None:
    def prompt(**_: object) -> str:
        warnings.warn("fallback", category=getpass.GetPassWarning, stacklevel=2)
        return PASSPHRASE.decode()

    reader = TtyPassphraseReader._from_test_terminal(
        _Terminal(interactive=True), prompt=prompt
    )
    with pytest.raises(ConnectorError) as caught:
        reader.read("Passphrase: ")
    _assert_code(caught, ConnectorErrorCode.INTERACTIVE_UNLOCK_REQUIRED.value)


def test_production_tty_reader_captures_and_revalidates_dev_tty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    descriptor = 71
    terminal = _CapturedTerminal(descriptor)
    status = SimpleNamespace(
        st_mode=stat.S_IFCHR | 0o600,
        st_dev=11,
        st_ino=12,
        st_rdev=13,
    )
    opened: list[tuple[str, int]] = []
    closed: list[int] = []

    def open_terminal(path: str, flags: int) -> int:
        opened.append((path, flags))
        return descriptor

    def prompt(*, prompt: str, stream: object) -> str:
        assert prompt == "Passphrase: "
        assert stream is terminal
        return PASSPHRASE.decode()

    monkeypatch.setattr(identity_module.os, "open", open_terminal)
    monkeypatch.setattr(identity_module.os, "fdopen", lambda *_a, **_kw: terminal)
    monkeypatch.setattr(identity_module.os, "fstat", lambda _fd: status)
    monkeypatch.setattr(identity_module.os, "isatty", lambda _fd: True)
    monkeypatch.setattr(identity_module.os, "tcgetpgrp", lambda _fd: 41)
    monkeypatch.setattr(identity_module.os, "getpgrp", lambda: 41)
    monkeypatch.setattr(identity_module.os, "close", closed.append)
    monkeypatch.setattr(identity_module.getpass, "getpass", prompt)

    reader = TtyPassphraseReader()
    secret = reader.read("Passphrase: ")
    reader.close()

    assert repr(secret) == "KeyPassphrase(<redacted>)"
    assert [path for path, _flags in opened] == ["/dev/tty"] * 3
    for _path, flags in opened:
        assert flags & os.O_NOFOLLOW
        assert flags & os.O_NOCTTY
        assert flags & os.O_CLOEXEC
        assert flags & os.O_RDWR
    assert closed == [descriptor, descriptor]


@pytest.mark.parametrize(
    ("mode", "terminal_process_group"),
    [(stat.S_IFREG | 0o600, 41), (stat.S_IFCHR | 0o600, 42)],
)
def test_production_tty_reader_rejects_non_character_or_background_terminal(
    monkeypatch: pytest.MonkeyPatch,
    mode: int,
    terminal_process_group: int,
) -> None:
    descriptor = 72
    status = SimpleNamespace(st_mode=mode, st_dev=11, st_ino=12, st_rdev=13)
    closed: list[int] = []
    monkeypatch.setattr(identity_module.os, "open", lambda *_a, **_kw: descriptor)
    monkeypatch.setattr(identity_module.os, "fstat", lambda _fd: status)
    monkeypatch.setattr(identity_module.os, "isatty", lambda _fd: True)
    monkeypatch.setattr(
        identity_module.os, "tcgetpgrp", lambda _fd: terminal_process_group
    )
    monkeypatch.setattr(identity_module.os, "getpgrp", lambda: 41)
    monkeypatch.setattr(identity_module.os, "close", closed.append)

    with pytest.raises(ConnectorError) as caught:
        TtyPassphraseReader()

    _assert_code(caught, ConnectorErrorCode.INTERACTIVE_UNLOCK_REQUIRED.value)
    assert closed == [descriptor]


def test_production_tty_reader_rejects_changed_controlling_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    descriptor = 73
    terminal = _CapturedTerminal(descriptor)
    original = SimpleNamespace(
        st_mode=stat.S_IFCHR | 0o600,
        st_dev=11,
        st_ino=12,
        st_rdev=13,
    )
    replacement = SimpleNamespace(
        st_mode=stat.S_IFCHR | 0o600,
        st_dev=11,
        st_ino=14,
        st_rdev=15,
    )
    statuses = iter((original, original, replacement))
    prompted = False

    def prompt(**_: object) -> str:
        nonlocal prompted
        prompted = True
        return PASSPHRASE.decode()

    monkeypatch.setattr(identity_module.os, "open", lambda *_a, **_kw: descriptor)
    monkeypatch.setattr(identity_module.os, "fdopen", lambda *_a, **_kw: terminal)
    monkeypatch.setattr(identity_module.os, "fstat", lambda _fd: next(statuses))
    monkeypatch.setattr(identity_module.os, "isatty", lambda _fd: True)
    monkeypatch.setattr(identity_module.os, "tcgetpgrp", lambda _fd: 41)
    monkeypatch.setattr(identity_module.os, "getpgrp", lambda: 41)
    monkeypatch.setattr(identity_module.os, "close", lambda _fd: None)
    monkeypatch.setattr(identity_module.getpass, "getpass", prompt)

    reader = TtyPassphraseReader()
    with pytest.raises(ConnectorError) as caught:
        reader.read("Passphrase: ")
    reader.close()

    _assert_code(caught, ConnectorErrorCode.INTERACTIVE_UNLOCK_REQUIRED.value)
    assert not prompted


def test_connector_store_has_no_direct_passphrase_or_ambient_input_api() -> None:
    assert set(inspect.signature(TtyPassphraseReader.__init__).parameters) == {"self"}
    assert type(_fixed_tty_reader(PASSPHRASE)) is not TtyPassphraseReader
    for method in (
        ConnectorKeyStore.__init__,
        ConnectorKeyStore.initialize_from_tty,
        ConnectorKeyStore.unlock_from_tty,
    ):
        names = set(inspect.signature(method).parameters)
        assert not names & {
            "passphrase",
            "password",
            "argv",
            "env",
            "config",
            "stdin",
            "ipc",
        }


def test_connector_store_rejects_structural_or_raw_passphrase_readers(
    tmp_path: Path,
) -> None:
    state = tmp_path / "connector"
    store = ConnectorKeyStore(state, _platform="linux")

    with pytest.raises(ConnectorError) as caught:
        store.initialize_from_tty(_StructuralReader())  # type: ignore[arg-type]

    _assert_code(caught, ConnectorErrorCode.INTERACTIVE_UNLOCK_REQUIRED.value)
    assert not state.exists()


def test_connector_store_rejects_test_reader_at_production_boundaries(
    tmp_path: Path,
) -> None:
    state = tmp_path / "connector"
    store = ConnectorKeyStore(state, _platform="linux")
    initialize_prompt = _FixedPrompt(PASSPHRASE)
    injected_initialize = TtyPassphraseReader._from_test_terminal(
        _Terminal(interactive=True), prompt=initialize_prompt
    )

    with pytest.raises(ConnectorError) as initialize_error:
        store.initialize_from_tty(injected_initialize)

    _assert_code(initialize_error, ConnectorErrorCode.INTERACTIVE_UNLOCK_REQUIRED.value)
    assert initialize_prompt.calls == 0
    assert not state.exists()

    store._initialize_from_tty_for_testing(_fixed_tty_reader(PASSPHRASE))
    unlock_prompt = _FixedPrompt(PASSPHRASE)
    injected_unlock = TtyPassphraseReader._from_test_terminal(
        _Terminal(interactive=True), prompt=unlock_prompt
    )

    with pytest.raises(ConnectorError) as unlock_error:
        store.unlock_from_tty(injected_unlock)

    _assert_code(unlock_error, ConnectorErrorCode.INTERACTIVE_UNLOCK_REQUIRED.value)
    assert unlock_prompt.calls == 0


def test_connector_store_creates_encrypted_owner_only_pkcs8(tmp_path: Path) -> None:
    state = tmp_path / "connector"
    store = ConnectorKeyStore(state, _platform="linux")

    public = store._initialize_from_tty_for_testing(_fixed_tty_reader(PASSPHRASE))
    key_path = _only_file(state)
    payload = key_path.read_bytes()

    assert stat.S_IMODE(state.stat().st_mode) == 0o700
    assert stat.S_IMODE(key_path.stat().st_mode) == 0o600
    assert key_path.stat().st_uid == os.geteuid()
    assert key_path.stat().st_nlink == 1
    assert payload.startswith(b"-----BEGIN ENCRYPTED PRIVATE KEY-----\n")
    with pytest.raises((TypeError, ValueError)):
        serialization.load_pem_private_key(payload, password=None)
    loaded = serialization.load_pem_private_key(payload, password=PASSPHRASE)
    assert isinstance(loaded, Ed25519PrivateKey)
    assert (
        store._unlock_from_tty_for_testing(
            _fixed_tty_reader(PASSPHRASE)
        ).public_identity
        == public
    )


def test_connector_unlock_terminates_closed_after_exactly_three_failures(
    tmp_path: Path,
) -> None:
    store = ConnectorKeyStore(tmp_path / "connector", _platform="darwin")
    store._initialize_from_tty_for_testing(_fixed_tty_reader(PASSPHRASE))
    prompt = _FixedPrompt(b"wrong-1", b"wrong-2", b"wrong-3", PASSPHRASE)
    reader = TtyPassphraseReader._from_test_terminal(
        _Terminal(interactive=True), prompt=prompt
    )

    with pytest.raises(ConnectorError) as caught:
        store._unlock_from_tty_for_testing(reader)

    _assert_code(caught, ConnectorErrorCode.CONNECTOR_KEY_LOCKED.value)
    assert prompt.calls == 3
    with pytest.raises(ValueError, match="exactly three"):
        store._unlock_from_tty_for_testing(_fixed_tty_reader(PASSPHRASE), attempts=4)


def test_connector_unlock_does_not_retry_a_noninteractive_reader(
    tmp_path: Path,
) -> None:
    store = ConnectorKeyStore(tmp_path / "connector", _platform="linux")
    store._initialize_from_tty_for_testing(_fixed_tty_reader(PASSPHRASE))
    reader = TtyPassphraseReader._from_test_terminal(
        _Terminal(interactive=False), prompt=_FixedPrompt(PASSPHRASE)
    )

    with pytest.raises(ConnectorError) as caught:
        store._unlock_from_tty_for_testing(reader)

    _assert_code(caught, ConnectorErrorCode.INTERACTIVE_UNLOCK_REQUIRED.value)


def test_vps_store_creates_unencrypted_owner_only_pkcs8_and_loads(
    tmp_path: Path,
) -> None:
    state = tmp_path / "vps"
    store = VpsKeyStore(state, _platform="linux")

    public = store.initialize()
    key_path = _only_file(state)
    payload = key_path.read_bytes()

    assert stat.S_IMODE(state.stat().st_mode) == 0o700
    assert stat.S_IMODE(key_path.stat().st_mode) == 0o600
    assert key_path.stat().st_uid == os.geteuid()
    assert key_path.stat().st_nlink == 1
    assert payload.startswith(b"-----BEGIN PRIVATE KEY-----\n")
    loaded = serialization.load_pem_private_key(payload, password=None)
    assert isinstance(loaded, Ed25519PrivateKey)
    assert store.load().public_identity == public


@pytest.mark.parametrize(
    ("store_type", "platform"),
    [(ConnectorKeyStore, "win32"), (VpsKeyStore, "darwin"), (VpsKeyStore, "win32")],
)
def test_key_stores_reject_unsupported_platforms_without_creating_state(
    tmp_path: Path,
    store_type: type[ConnectorKeyStore] | type[VpsKeyStore],
    platform: str,
) -> None:
    state = tmp_path / "unsupported"
    store = store_type(state, _platform=platform)

    with pytest.raises(ConnectorError) as caught:
        if isinstance(store, ConnectorKeyStore):
            store._initialize_from_tty_for_testing(_fixed_tty_reader(PASSPHRASE))
        else:
            store.initialize()

    _assert_code(caught, ConnectorErrorCode.UNSUPPORTED_PLATFORM.value)
    assert not state.exists()


def test_key_store_refuses_collision_without_overwrite_or_temp_residue(
    tmp_path: Path,
) -> None:
    state = tmp_path / "vps"
    store = VpsKeyStore(state, _platform="linux")
    public = store.initialize()
    key_path = _only_file(state)
    original = key_path.read_bytes()

    with pytest.raises(ConnectorError) as caught:
        store.initialize()

    _assert_code(caught, ConnectorErrorCode.CONNECTOR_NOT_INITIALIZED.value)
    assert key_path.read_bytes() == original
    assert store.load().public_identity == public
    assert tuple(state.iterdir()) == (key_path,)


def test_missing_known_vps_key_fails_without_silent_regeneration(
    tmp_path: Path,
) -> None:
    state = tmp_path / "vps"
    store = VpsKeyStore(state, _platform="linux")
    store.initialize()
    _only_file(state).unlink()

    with pytest.raises(ConnectorError) as caught:
        store.load()

    _assert_code(caught, ConnectorErrorCode.CONNECTOR_NOT_INITIALIZED.value)
    assert tuple(state.iterdir()) == ()


def test_store_rejects_symlink_state_directory(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir(mode=0o700)
    alias = tmp_path / "STATE_PATH_CANARY"
    alias.symlink_to(target, target_is_directory=True)

    with pytest.raises(ConnectorError) as caught:
        VpsKeyStore(alias, _platform="linux").initialize()

    _assert_code(caught, ConnectorErrorCode.CONNECTOR_NOT_INITIALIZED.value)
    assert tuple(target.iterdir()) == ()


def test_store_rejects_permissive_state_directory(tmp_path: Path) -> None:
    state = tmp_path / "STATE_PATH_CANARY"
    state.mkdir(mode=0o700)
    state.chmod(0o750)

    with pytest.raises(ConnectorError) as caught:
        VpsKeyStore(state, _platform="linux").initialize()

    _assert_code(caught, ConnectorErrorCode.CONNECTOR_NOT_INITIALIZED.value)
    assert tuple(state.iterdir()) == ()


def test_store_rejects_wrong_owner_state_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "STATE_PATH_CANARY"
    state.mkdir(mode=0o700)
    monkeypatch.setattr(
        "hermes_reach.connector.identity.os.geteuid", lambda: os.getuid() + 1
    )

    with pytest.raises(ConnectorError) as caught:
        VpsKeyStore(state, _platform="linux").initialize()

    _assert_code(caught, ConnectorErrorCode.CONNECTOR_NOT_INITIALIZED.value)
    assert tuple(state.iterdir()) == ()


def test_store_rejects_symlink_key(tmp_path: Path) -> None:
    state = tmp_path / "vps"
    state.mkdir(mode=0o700)
    outside = tmp_path / "KEY_PATH_CANARY"
    outside.write_bytes(b"not a key")
    outside.chmod(0o600)
    (state / "vps-identity.pem").symlink_to(outside)

    with pytest.raises(ConnectorError) as caught:
        VpsKeyStore(state, _platform="linux").load()

    _assert_code(caught, ConnectorErrorCode.CONNECTOR_NOT_INITIALIZED.value)


@pytest.mark.parametrize("mode", [0o400, 0o640, 0o660, 0o604])
def test_store_rejects_any_key_mode_other_than_0600(tmp_path: Path, mode: int) -> None:
    state = tmp_path / f"vps-{mode:o}"
    store = VpsKeyStore(state, _platform="linux")
    store.initialize()
    _only_file(state).chmod(mode)

    with pytest.raises(ConnectorError) as caught:
        store.load()

    _assert_code(caught, ConnectorErrorCode.CONNECTOR_NOT_INITIALIZED.value)


def test_store_rejects_hardlinked_key(tmp_path: Path) -> None:
    state = tmp_path / "vps"
    store = VpsKeyStore(state, _platform="linux")
    store.initialize()
    key_path = _only_file(state)
    os.link(key_path, tmp_path / "KEY_PATH_CANARY")

    with pytest.raises(ConnectorError) as caught:
        store.load()

    _assert_code(caught, ConnectorErrorCode.CONNECTOR_NOT_INITIALIZED.value)


def test_store_rejects_fifo_without_blocking(tmp_path: Path) -> None:
    state = tmp_path / "vps"
    state.mkdir(mode=0o700)
    fifo = state / "vps-identity.pem"
    os.mkfifo(fifo, mode=0o600)

    with pytest.raises(ConnectorError) as caught:
        VpsKeyStore(state, _platform="linux").load()

    _assert_code(caught, ConnectorErrorCode.CONNECTOR_NOT_INITIALIZED.value)


def test_store_rejects_oversized_key_file(tmp_path: Path) -> None:
    state = tmp_path / "vps"
    state.mkdir(mode=0o700)
    key_path = state / "vps-identity.pem"
    key_path.write_bytes(b"X" * (16 * 1024 + 1))
    key_path.chmod(0o600)

    with pytest.raises(ConnectorError) as caught:
        VpsKeyStore(state, _platform="linux").load()

    _assert_code(caught, ConnectorErrorCode.CONNECTOR_NOT_INITIALIZED.value)


def test_stores_require_ed25519_keys_even_with_valid_pkcs8_headers(
    tmp_path: Path,
) -> None:
    rsa_key = generate_private_key(public_exponent=65537, key_size=2048)

    vps_state = tmp_path / "vps"
    vps_state.mkdir(mode=0o700)
    vps_key = vps_state / "vps-identity.pem"
    vps_key.write_bytes(
        rsa_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    vps_key.chmod(0o600)
    with pytest.raises(ConnectorError) as vps_error:
        VpsKeyStore(vps_state, _platform="linux").load()
    _assert_code(vps_error, ConnectorErrorCode.CONNECTOR_NOT_INITIALIZED.value)

    connector_state = tmp_path / "connector"
    connector_state.mkdir(mode=0o700)
    connector_key = connector_state / "connector-identity.pem"
    connector_key.write_bytes(
        rsa_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.BestAvailableEncryption(PASSPHRASE),
        )
    )
    connector_key.chmod(0o600)
    prompt = _FixedPrompt(PASSPHRASE, PASSPHRASE, PASSPHRASE)
    reader = TtyPassphraseReader._from_test_terminal(
        _Terminal(interactive=True), prompt=prompt
    )
    with pytest.raises(ConnectorError) as connector_error:
        ConnectorKeyStore(
            connector_state, _platform="linux"
        )._unlock_from_tty_for_testing(reader)
    _assert_code(connector_error, ConnectorErrorCode.CONNECTOR_KEY_LOCKED.value)
    assert prompt.calls == 3


def test_stores_reject_valid_ed25519_pkcs8_from_the_opposite_role(
    tmp_path: Path,
) -> None:
    private_key = Ed25519PrivateKey.generate()

    vps_state = tmp_path / "vps"
    vps_state.mkdir(mode=0o700)
    vps_key = vps_state / "vps-identity.pem"
    vps_key.write_bytes(
        private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.BestAvailableEncryption(PASSPHRASE),
        )
    )
    vps_key.chmod(0o600)
    with pytest.raises(ConnectorError) as vps_error:
        VpsKeyStore(vps_state, _platform="linux").load()
    _assert_code(vps_error, ConnectorErrorCode.CONNECTOR_NOT_INITIALIZED.value)

    connector_state = tmp_path / "connector"
    connector_state.mkdir(mode=0o700)
    connector_key = connector_state / "connector-identity.pem"
    connector_key.write_bytes(
        private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    connector_key.chmod(0o600)
    prompt = _FixedPrompt(PASSPHRASE, PASSPHRASE, PASSPHRASE)
    reader = TtyPassphraseReader._from_test_terminal(
        _Terminal(interactive=True), prompt=prompt
    )
    with pytest.raises(ConnectorError) as connector_error:
        ConnectorKeyStore(
            connector_state, _platform="linux"
        )._unlock_from_tty_for_testing(reader)
    _assert_code(connector_error, ConnectorErrorCode.CONNECTOR_KEY_LOCKED.value)
    assert prompt.calls == 0


def test_connector_store_maps_malformed_encrypted_pem_to_locked(
    tmp_path: Path,
) -> None:
    state = tmp_path / "connector"
    state.mkdir(mode=0o700)
    key_path = state / "connector-identity.pem"
    key_path.write_bytes(
        b"-----BEGIN ENCRYPTED PRIVATE KEY-----\nMALFORMED_KEY_CANARY\n"
    )
    key_path.chmod(0o600)
    prompt = _FixedPrompt(PASSPHRASE, PASSPHRASE, PASSPHRASE)
    reader = TtyPassphraseReader._from_test_terminal(
        _Terminal(interactive=True), prompt=prompt
    )

    with pytest.raises(ConnectorError) as caught:
        ConnectorKeyStore(state, _platform="linux")._unlock_from_tty_for_testing(reader)

    _assert_code(caught, ConnectorErrorCode.CONNECTOR_KEY_LOCKED.value)
    assert prompt.calls == 3


def _rotation() -> tuple[
    DevicePrivateIdentity, DevicePrivateIdentity, SignedRotationV1
]:
    old = _identity()
    new = _identity(bytes(range(32)))
    rotation = create_signed_rotation(
        old,
        new,
        role=DeviceRole.CONNECTOR,
        sequence=4,
        issued_at=1_000,
        expires_at=1_300,
    )
    return old, new, rotation


def _verify_rotation(
    rotation: SignedRotationV1,
    *,
    old: DevicePrivateIdentity,
    role: DeviceRole = DeviceRole.CONNECTOR,
    sequence: int = 3,
    now: int = 1_100,
) -> DevicePublicIdentity | None:
    return verify_signed_rotation(
        rotation,
        pinned_identity=old.public_identity,
        expected_role=role,
        current_sequence=sequence,
        now=datetime.fromtimestamp(now, tz=UTC),
    )


def test_rotation_statement_is_canonical_and_proves_old_and_new_keys() -> None:
    old, new, rotation = _rotation()
    expected = (
        b'{"expires_at":1300,"issued_at":1000,"new_key_id":"'
        + new.public_identity.key_id.encode()
        + b'","new_public_key":"'
        + new.public_identity.wire_public_key.encode()
        + b'","old_key_id":"'
        + old.public_identity.key_id.encode()
        + b'","role":"connector","sequence":4,"version":"v1"}'
    )

    assert rotation.statement.canonical_bytes() == expected
    assert old.public_identity.verify(
        SignatureDomain.KEY_ROTATION, expected, rotation.old_signature
    )
    assert new.public_identity.verify(
        SignatureDomain.KEY_ROTATION_PROOF, expected, rotation.new_key_proof
    )
    assert _verify_rotation(rotation, old=old) == new.public_identity


@pytest.mark.parametrize(
    "mutation",
    [
        lambda signed: replace(signed, old_signature=b"\x00" * 64),
        lambda signed: replace(signed, new_key_proof=b"\x00" * 64),
        lambda signed: replace(
            signed, statement=replace(signed.statement, old_key_id="a" * 32)
        ),
    ],
)
def test_rotation_rejects_tampering_and_missing_either_key_proof(
    mutation: Callable[[SignedRotationV1], SignedRotationV1],
) -> None:
    old, _, rotation = _rotation()
    assert _verify_rotation(mutation(rotation), old=old) is None


def test_rotation_statement_rejects_mismatched_key_material_and_version() -> None:
    _, _, rotation = _rotation()

    with pytest.raises(ValueError, match="rotation identity"):
        replace(rotation.statement, new_key_id="a" * 32)
    with pytest.raises(ValueError, match="rotation identity"):
        replace(rotation.statement, new_public_key=RFC8032_WIRE)
    with pytest.raises(ValueError, match="version or role"):
        replace(rotation.statement, version="v2")


def test_rotation_rejects_wrong_pin_role_sequence_and_time_bounds() -> None:
    old, new, rotation = _rotation()

    assert _verify_rotation(rotation, old=new) is None
    assert _verify_rotation(rotation, old=old, role=DeviceRole.VPS) is None
    assert _verify_rotation(rotation, old=old, sequence=2) is None
    assert _verify_rotation(rotation, old=old, sequence=4) is None
    assert _verify_rotation(rotation, old=old, now=999) is None
    assert _verify_rotation(rotation, old=old, now=1_300) is None
    assert (
        verify_signed_rotation(
            rotation,
            pinned_identity=old.public_identity,
            expected_role=DeviceRole.CONNECTOR,
            current_sequence=3,
            now=datetime.fromtimestamp(1_100),
        )
        is None
    )


@pytest.mark.parametrize(
    ("sequence", "issued_at", "expires_at"),
    [(0, 1_000, 1_300), (-1, 1_000, 1_300), (4, -1, 1_300), (4, 1_000, 1_000)],
)
def test_rotation_creation_rejects_invalid_bounds(
    sequence: int, issued_at: int, expires_at: int
) -> None:
    old, new, _ = _rotation()
    with pytest.raises(ValueError, match="rotation bounds"):
        create_signed_rotation(
            old,
            new,
            role=DeviceRole.CONNECTOR,
            sequence=sequence,
            issued_at=issued_at,
            expires_at=expires_at,
        )


def test_rotation_rejects_same_key_and_offers_no_recovery_bypass() -> None:
    old = _identity()
    with pytest.raises(ValueError):
        create_signed_rotation(
            old,
            old,
            role=DeviceRole.CONNECTOR,
            sequence=1,
            issued_at=1_000,
            expires_at=1_300,
        )

    parameters = set(inspect.signature(verify_signed_rotation).parameters)
    assert not parameters & {"recover", "recovery", "skip_old_proof", "force"}


def test_rotation_dataclasses_do_not_accept_open_ended_role_or_version() -> None:
    _, _, rotation = _rotation()
    with pytest.raises(ValueError, match="version or role"):
        RotationStatementV1(
            role=DeviceRole.CONNECTOR,
            old_key_id=rotation.statement.old_key_id,
            new_key_id=rotation.statement.new_key_id,
            new_public_key=rotation.statement.new_public_key,
            sequence=rotation.statement.sequence,
            issued_at=rotation.statement.issued_at,
            expires_at=rotation.statement.expires_at,
            version="recovery",
        )
