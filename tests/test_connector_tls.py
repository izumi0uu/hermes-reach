from __future__ import annotations

import ipaddress
import os
import ssl
import stat
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.x509.oid import ExtendedKeyUsageOID, ExtensionOID, NameOID

import hermes_reach.connector.identity as identity_module
import hermes_reach.connector.tls as tls_module
from hermes_reach.connector.errors import ConnectorError, ConnectorErrorCode
from hermes_reach.connector.identity import DevicePrivateIdentity
from hermes_reach.connector.tls import (
    ConnectorTLSStore,
    build_initial_pairing_client_context,
    build_pinned_client_context,
    validate_private_bind_host,
    verify_connector_ca_der,
    verify_connector_leaf_der,
)

NOW = 1_800_000_000
SEED = bytes(range(32))
OTHER_SEED = bytes(reversed(range(32)))


def _identity(seed: bytes = SEED) -> DevicePrivateIdentity:
    return DevicePrivateIdentity._from_seed_for_testing(seed)


def _assert_code(error: pytest.ExceptionInfo[ConnectorError], code: str) -> None:
    assert error.value.code == code
    assert "CANARY" not in str(error.value)
    assert "CANARY" not in repr(error.value)


def _ca_path(state: Path) -> Path:
    path = state / "connector-ca.pem"
    assert path.is_file()
    return path


def test_connector_ca_is_identity_signed_canonical_and_owner_only(
    tmp_path: Path,
) -> None:
    state = tmp_path / "connector"
    signer = _identity()

    authority = ConnectorTLSStore(state, _platform="linux").initialize(signer, now=NOW)
    certificate = authority.certificate
    public_key = certificate.public_key()
    ca_path = _ca_path(state)

    assert stat.S_IMODE(state.stat().st_mode) == 0o700
    assert stat.S_IMODE(ca_path.stat().st_mode) == 0o600
    assert ca_path.stat().st_uid == os.geteuid()
    assert ca_path.stat().st_nlink == 1
    assert ca_path.read_bytes() == authority.pem
    assert authority.der == certificate.public_bytes(serialization.Encoding.DER)
    assert authority.fingerprint == certificate.fingerprint(hashes.SHA256()).hex()
    assert authority.connector_identity == signer.public_identity
    assert certificate.subject == certificate.issuer
    assert certificate.subject == x509.Name(
        [
            x509.NameAttribute(
                NameOID.COMMON_NAME,
                f"Hermes Reach Connector CA {signer.public_identity.key_id}",
            )
        ]
    )
    public_key.verify(certificate.signature, certificate.tbs_certificate_bytes)

    extensions = {extension.oid: extension for extension in certificate.extensions}
    assert set(extensions) == {
        ExtensionOID.BASIC_CONSTRAINTS,
        ExtensionOID.KEY_USAGE,
        ExtensionOID.SUBJECT_KEY_IDENTIFIER,
        ExtensionOID.AUTHORITY_KEY_IDENTIFIER,
    }
    basic = extensions[ExtensionOID.BASIC_CONSTRAINTS]
    usage = extensions[ExtensionOID.KEY_USAGE]
    subject_key = extensions[ExtensionOID.SUBJECT_KEY_IDENTIFIER]
    authority_key = extensions[ExtensionOID.AUTHORITY_KEY_IDENTIFIER]
    assert basic.critical and basic.value == x509.BasicConstraints(True, 0)
    assert usage.critical
    assert usage.value.digital_signature
    assert usage.value.key_cert_sign
    assert usage.value.crl_sign
    assert not usage.value.content_commitment
    assert not usage.value.key_encipherment
    assert not usage.value.data_encipherment
    assert not usage.value.key_agreement
    assert not subject_key.critical
    assert not authority_key.critical
    assert authority_key.value.key_identifier == subject_key.value.digest


def test_connector_ca_load_rejects_wrong_identity_tamper_expiry_and_constraints(
    tmp_path: Path,
) -> None:
    state = tmp_path / "connector"
    private_key = Ed25519PrivateKey.from_private_bytes(SEED)
    signer = DevicePrivateIdentity(private_key)
    store = ConnectorTLSStore(state, _platform="linux")
    authority = store.initialize(signer, now=NOW)

    with pytest.raises(ConnectorError) as wrong_identity:
        store.load(_identity(OTHER_SEED).public_identity, now=NOW)
    _assert_code(wrong_identity, ConnectorErrorCode.CONNECTOR_TLS_FAILED.value)

    ca_path = _ca_path(state)
    ca_path.write_bytes(authority.pem + b"\n")
    with pytest.raises(ConnectorError) as noncanonical:
        store.load(signer.public_identity, now=NOW)
    _assert_code(noncanonical, ConnectorErrorCode.CONNECTOR_TLS_FAILED.value)

    ca_path.write_bytes(authority.pem)
    with pytest.raises(ConnectorError) as expired:
        store.load(signer.public_identity, now=NOW + 11 * 365 * 24 * 60 * 60)
    _assert_code(expired, ConnectorErrorCode.CONNECTOR_TLS_FAILED.value)

    moment = datetime.fromtimestamp(NOW, UTC)
    invalid = (
        x509.CertificateBuilder()
        .subject_name(authority.certificate.subject)
        .issuer_name(authority.certificate.issuer)
        .public_key(private_key.public_key())
        .serial_number(7)
        .not_valid_before(moment - timedelta(seconds=30))
        .not_valid_after(moment + timedelta(days=365))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(private_key, algorithm=None)
    )
    ca_path.write_bytes(invalid.public_bytes(serialization.Encoding.PEM))
    with pytest.raises(ConnectorError) as invalid_constraints:
        store.load(signer.public_identity, now=NOW)
    _assert_code(invalid_constraints, ConnectorErrorCode.CONNECTOR_TLS_FAILED.value)


@pytest.mark.parametrize("mode", [0o400, 0o640, 0o660, 0o604])
def test_connector_ca_rejects_any_mode_other_than_0600(
    tmp_path: Path, mode: int
) -> None:
    state = tmp_path / f"connector-{mode:o}"
    signer = _identity()
    store = ConnectorTLSStore(state, _platform="linux")
    store.initialize(signer, now=NOW)
    _ca_path(state).chmod(mode)

    with pytest.raises(ConnectorError) as caught:
        store.load(signer.public_identity, now=NOW)

    _assert_code(caught, ConnectorErrorCode.CONNECTOR_STATE_INVALID.value)


def test_connector_ca_rejects_symlink_hardlink_wrong_owner_and_fifo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    signer = _identity()

    target_state = tmp_path / "target"
    authority = ConnectorTLSStore(target_state, _platform="linux").initialize(
        signer, now=NOW
    )
    symlink_state = tmp_path / "symlink-state"
    symlink_state.mkdir(mode=0o700)
    outside = tmp_path / "CA_PATH_CANARY"
    outside.write_bytes(authority.pem)
    outside.chmod(0o600)
    (symlink_state / "connector-ca.pem").symlink_to(outside)
    with pytest.raises(ConnectorError) as symlink_error:
        ConnectorTLSStore(symlink_state, _platform="linux").load(
            signer.public_identity, now=NOW
        )
    _assert_code(symlink_error, ConnectorErrorCode.CONNECTOR_STATE_INVALID.value)

    hardlink_state = tmp_path / "hardlink-state"
    hardlink_store = ConnectorTLSStore(hardlink_state, _platform="linux")
    hardlink_store.initialize(signer, now=NOW)
    os.link(_ca_path(hardlink_state), tmp_path / "CA_HARDLINK_CANARY")
    with pytest.raises(ConnectorError) as hardlink_error:
        hardlink_store.load(signer.public_identity, now=NOW)
    _assert_code(hardlink_error, ConnectorErrorCode.CONNECTOR_STATE_INVALID.value)

    fifo_state = tmp_path / "fifo-state"
    fifo_state.mkdir(mode=0o700)
    os.mkfifo(fifo_state / "connector-ca.pem", mode=0o600)
    with pytest.raises(ConnectorError) as fifo_error:
        ConnectorTLSStore(fifo_state, _platform="linux").load(
            signer.public_identity, now=NOW
        )
    _assert_code(fifo_error, ConnectorErrorCode.CONNECTOR_STATE_INVALID.value)

    owner_state = tmp_path / "owner-state"
    owner_store = ConnectorTLSStore(owner_state, _platform="linux")
    owner_store.initialize(signer, now=NOW)
    monkeypatch.setattr(identity_module.os, "geteuid", lambda: os.getuid() + 1)
    with pytest.raises(ConnectorError) as owner_error:
        owner_store.load(signer.public_identity, now=NOW)
    _assert_code(owner_error, ConnectorErrorCode.CONNECTOR_STATE_INVALID.value)


def test_each_unlock_uses_a_fresh_endpoint_bound_leaf_and_anonymous_pipes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "connector"
    signer = _identity()
    store = ConnectorTLSStore(state, _platform="linux")
    authority = store.initialize(signer, now=NOW)
    real_pipe = os.pipe
    pipe_calls = 0
    captured_chains: list[bytes] = []
    captured_keys: list[bytes] = []
    real_server_context = tls_module._server_context

    def counted_pipe() -> tuple[int, int]:
        nonlocal pipe_calls
        pipe_calls += 1
        return real_pipe()

    def capture_context(chain: bytes, key: bytearray) -> ssl.SSLContext:
        captured_chains.append(chain)
        captured_keys.append(bytes(key))
        return real_server_context(chain, key)

    monkeypatch.setattr(tls_module.os, "pipe", counted_pipe)
    monkeypatch.setattr(tls_module, "_server_context", capture_context)

    first = store.create_unlock_material(signer, bind_host="127.0.0.1", now=NOW)
    second = store.create_unlock_material(signer, bind_host="127.0.0.1", now=NOW)

    assert pipe_calls == 4
    assert first.leaf_fingerprint != second.leaf_fingerprint
    assert first.expires_at == second.expires_at == NOW + 8 * 60 * 60
    assert tuple(path.name for path in state.iterdir()) == ("connector-ca.pem",)
    assert all(
        key.startswith(b"-----BEGIN PRIVATE KEY-----\n") for key in captured_keys
    )
    assert all(key not in _ca_path(state).read_bytes() for key in captured_keys)

    first_chain = x509.load_pem_x509_certificates(captured_chains[0])
    assert len(first_chain) == 2
    leaf, chained_ca = first_chain
    assert chained_ca == authority.certificate
    assert leaf.issuer == authority.certificate.subject
    assert leaf.subject == x509.Name(
        [
            x509.NameAttribute(
                NameOID.COMMON_NAME,
                f"Hermes Reach Connector TLS {signer.public_identity.key_id}",
            )
        ]
    )
    assert leaf.extensions.get_extension_for_class(x509.BasicConstraints).value == (
        x509.BasicConstraints(ca=False, path_length=None)
    )
    assert leaf.extensions.get_extension_for_class(
        x509.ExtendedKeyUsage
    ).value == x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH])
    assert leaf.extensions.get_extension_for_class(
        x509.SubjectAlternativeName
    ).value.get_values_for_type(x509.IPAddress) == [ipaddress.ip_address("127.0.0.1")]
    authority_public_key = authority.certificate.public_key()
    authority_public_key.verify(leaf.signature, leaf.tbs_certificate_bytes)
    assert first.leaf_fingerprint == leaf.fingerprint(hashes.SHA256()).hex()
    verified_authority = verify_connector_ca_der(
        authority.der, signer.public_identity, now=NOW
    )
    assert verified_authority.fingerprint == authority.fingerprint
    assert (
        verify_connector_leaf_der(
            leaf.public_bytes(serialization.Encoding.DER),
            verified_authority,
            endpoint_host="127.0.0.1",
            now=NOW,
        )
        == first.leaf_fingerprint
    )
    with pytest.raises(ConnectorError) as wrong_endpoint:
        verify_connector_leaf_der(
            leaf.public_bytes(serialization.Encoding.DER),
            verified_authority,
            endpoint_host="127.0.0.2",
            now=NOW,
        )
    _assert_code(wrong_endpoint, ConnectorErrorCode.CONNECTOR_TLS_FAILED.value)
    with pytest.raises(ConnectorError) as tampered_ca:
        verify_connector_ca_der(
            authority.der[:-1] + bytes([authority.der[-1] ^ 1]),
            signer.public_identity,
            now=NOW,
        )
    _assert_code(tampered_ca, ConnectorErrorCode.CONNECTOR_TLS_FAILED.value)

    with pytest.raises(AttributeError):
        first.expires_at = NOW  # type: ignore[misc]
    with pytest.raises(AttributeError):
        first.leaf_fingerprint = "00" * 32  # type: ignore[misc]


def test_tls_contexts_require_tls13_disable_tickets_and_separate_pairing_trust(
    tmp_path: Path,
) -> None:
    signer = _identity()
    store = ConnectorTLSStore(tmp_path / "connector", _platform="linux")
    authority = store.initialize(signer, now=int(time.time()))
    material = store.create_unlock_material(
        signer, bind_host="127.0.0.1", now=int(time.time())
    )
    server = material.server_context
    pinned = build_pinned_client_context(authority)
    pairing = build_initial_pairing_client_context()

    for context in (server, pinned, pairing):
        assert context.minimum_version == ssl.TLSVersion.TLSv1_3
        assert context.options & ssl.OP_NO_COMPRESSION
        assert context.options & ssl.OP_NO_TICKET
    assert getattr(server, "num_tickets", 0) == 0
    assert pinned.verify_mode == ssl.CERT_REQUIRED
    assert not pinned.check_hostname
    assert authority.der in pinned.get_ca_certs(binary_form=True)
    assert len(pinned.get_ca_certs(binary_form=True)) == 1
    assert pairing.verify_mode == ssl.CERT_NONE
    assert not pairing.check_hostname
    assert pairing.get_ca_certs(binary_form=True) == []

    with pytest.raises(ConnectorError) as forged:
        build_pinned_client_context(replace(authority, fingerprint="00" * 32))
    _assert_code(forged, ConnectorErrorCode.CONNECTOR_TLS_FAILED.value)

    material.close()
    with pytest.raises(ConnectorError) as closed:
        _ = material.server_context
    _assert_code(closed, ConnectorErrorCode.CONNECTOR_KEY_LOCKED.value)


@pytest.mark.parametrize(
    "host",
    [
        "127.0.0.1",
        "127.255.255.254",
        "10.0.0.1",
        "172.16.0.1",
        "172.31.255.254",
        "192.168.1.1",
        "100.64.0.1",
        "100.127.255.254",
        "::1",
        "fc00::1",
        "fdff:ffff::1",
    ],
)
def test_private_bind_host_accepts_only_explicit_supported_ranges(host: str) -> None:
    assert str(validate_private_bind_host(host)) == host


@pytest.mark.parametrize(
    "host",
    [
        "",
        "0.0.0.0",
        "::",
        "8.8.8.8",
        "100.63.255.255",
        "100.128.0.1",
        "169.254.1.1",
        "224.0.0.1",
        "255.255.255.255",
        "2001:4860:4860::8888",
        "fe80::1",
        "fe80::1%en0",
        "ff02::1",
        "localhost",
        "[::1]",
        " 127.0.0.1",
    ],
)
def test_private_bind_host_rejects_wildcard_public_ambiguous_or_names(
    host: str,
) -> None:
    with pytest.raises(ConnectorError) as caught:
        validate_private_bind_host(host)
    _assert_code(caught, ConnectorErrorCode.CONNECTOR_TLS_FAILED.value)


def test_tls_store_rejects_unsupported_platform_without_state(tmp_path: Path) -> None:
    state = tmp_path / "unsupported"
    with pytest.raises(ConnectorError) as caught:
        ConnectorTLSStore(state, _platform="win32").initialize(_identity(), now=NOW)
    _assert_code(caught, ConnectorErrorCode.UNSUPPORTED_PLATFORM.value)
    assert not state.exists()
