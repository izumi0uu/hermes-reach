"""Connector identity CA and per-unlock ephemeral TLS material."""

from __future__ import annotations

import ipaddress
import os
import ssl
import sys
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final

from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.x509.oid import (
    ExtendedKeyUsageOID,
    ExtensionOID,
    NameOID,
    SignatureAlgorithmOID,
)

from .errors import ConnectorError, ConnectorErrorCode
from .identity import (
    DevicePrivateIdentity,
    DevicePublicIdentity,
    _create_key_file,
    _read_key_file,
)
from .limits import (
    MAX_TIMESTAMP_SECONDS,
    MAX_TLS_CA_DER_BYTES,
    SUPPORTED_CONNECTOR_PLATFORMS,
)

_CA_CERTIFICATE_FILE: Final = "connector-ca.pem"
_CA_COMMON_NAME_PREFIX: Final = "Hermes Reach Connector CA "
_LEAF_COMMON_NAME_PREFIX: Final = "Hermes Reach Connector TLS "
_CA_VALIDITY_SECONDS: Final = 10 * 365 * 24 * 60 * 60
_LEAF_VALIDITY_SECONDS: Final = 8 * 60 * 60
_CLOCK_SKEW_SECONDS: Final = 30
_ALPN_PROTOCOLS: Final = ["http/1.1"]
_MAX_CERTIFICATE_CHAIN_BYTES: Final = 32 * 1024
_MAX_TLS_PRIVATE_KEY_BYTES: Final = 4 * 1024


@dataclass(frozen=True, slots=True)
class ConnectorCACertificate:
    """Verified public identity CA material safe to pin on a VPS."""

    certificate: x509.Certificate
    pem: bytes
    fingerprint: str
    connector_identity: DevicePublicIdentity

    @property
    def der(self) -> bytes:
        """Return the canonical DER bytes carried by the signed pairing flow."""

        return self.certificate.public_bytes(serialization.Encoding.DER)


class EphemeralTLSMaterial:
    """One unlock-scoped server context whose leaf private key is never named."""

    __slots__ = (
        "_closed",
        "_context",
        "_expires_at",
        "_leaf_fingerprint",
    )

    def __init__(
        self,
        context: ssl.SSLContext,
        *,
        expires_at: int,
        leaf_fingerprint: str,
    ) -> None:
        self._context: ssl.SSLContext | None = context
        self._expires_at = expires_at
        self._leaf_fingerprint = leaf_fingerprint
        self._closed = False

    @property
    def expires_at(self) -> int:
        return self._expires_at

    @property
    def leaf_fingerprint(self) -> str:
        return self._leaf_fingerprint

    @property
    def server_context(self) -> ssl.SSLContext:
        if self._closed or self._context is None:
            raise ConnectorError(ConnectorErrorCode.CONNECTOR_KEY_LOCKED)
        return self._context

    def close(self) -> None:
        self._closed = True
        self._context = None

    def __enter__(self) -> EphemeralTLSMaterial:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def __repr__(self) -> str:
        return "EphemeralTLSMaterial(<redacted>)"


class ConnectorTLSStore:
    """Persist only the public identity CA and mint leaf state after unlock."""

    def __init__(self, state_directory: Path, *, _platform: str | None = None) -> None:
        self._state_directory = state_directory
        self._platform = sys.platform if _platform is None else _platform

    def initialize(
        self, signer: DevicePrivateIdentity, *, now: int | None = None
    ) -> ConnectorCACertificate:
        self._ensure_platform()
        if not isinstance(signer, DevicePrivateIdentity):
            raise TypeError("Connector CA initialization requires its identity signer.")
        timestamp = _timestamp_now() if now is None else _require_timestamp(now)
        certificate = _create_ca_certificate(signer, now=timestamp)
        pem = certificate.public_bytes(serialization.Encoding.PEM)
        _create_key_file(self._state_directory, _CA_CERTIFICATE_FILE, pem)
        return _verified_ca(certificate, pem, signer.public_identity, now=timestamp)

    def load_public_identity(self, *, now: int | None = None) -> DevicePublicIdentity:
        """Recover only the public Connector identity from its verified CA."""

        self._ensure_platform()
        timestamp = _timestamp_now() if now is None else _require_timestamp(now)
        try:
            payload = _read_key_file(self._state_directory, _CA_CERTIFICATE_FILE)
            certificate = x509.load_pem_x509_certificate(payload)
            public_key = certificate.public_key()
            if not isinstance(public_key, Ed25519PublicKey):
                raise ValueError("invalid CA public key")
            identity = DevicePublicIdentity(
                public_key.public_bytes(
                    serialization.Encoding.Raw,
                    serialization.PublicFormat.Raw,
                )
            )
            _verified_ca(certificate, payload, identity, now=timestamp)
            return identity
        except ConnectorError:
            raise
        except (TypeError, ValueError):
            raise ConnectorError(ConnectorErrorCode.CONNECTOR_TLS_FAILED) from None

    def load(
        self, expected_identity: DevicePublicIdentity, *, now: int | None = None
    ) -> ConnectorCACertificate:
        self._ensure_platform()
        if not isinstance(expected_identity, DevicePublicIdentity):
            raise TypeError("Connector CA loading requires the pinned identity.")
        timestamp = _timestamp_now() if now is None else _require_timestamp(now)
        try:
            payload = _read_key_file(self._state_directory, _CA_CERTIFICATE_FILE)
        except ConnectorError:
            raise ConnectorError(ConnectorErrorCode.CONNECTOR_STATE_INVALID) from None
        try:
            certificate = x509.load_pem_x509_certificate(payload)
            if certificate.public_bytes(serialization.Encoding.PEM) != payload:
                raise ValueError("non-canonical CA certificate")
            return _verified_ca(certificate, payload, expected_identity, now=timestamp)
        except ConnectorError:
            raise
        except (TypeError, ValueError):
            raise ConnectorError(ConnectorErrorCode.CONNECTOR_TLS_FAILED) from None

    def create_unlock_material(
        self,
        signer: DevicePrivateIdentity,
        *,
        bind_host: str,
        now: int | None = None,
    ) -> EphemeralTLSMaterial:
        """Create a fresh leaf and load it through anonymous POSIX file handles."""

        self._ensure_platform()
        if not isinstance(signer, DevicePrivateIdentity):
            raise TypeError("TLS unlock material requires the Connector signer.")
        timestamp = _timestamp_now() if now is None else _require_timestamp(now)
        address = validate_private_bind_host(bind_host)
        authority = self.load(signer.public_identity, now=timestamp)
        leaf_key = Ed25519PrivateKey.generate()
        leaf = _create_leaf_certificate(
            signer,
            authority.certificate,
            leaf_key,
            address=address,
            now=timestamp,
        )
        certificate_chain = (
            leaf.public_bytes(serialization.Encoding.PEM) + authority.pem
        )
        private_key = bytearray(
            leaf_key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
        try:
            context = _server_context(certificate_chain, private_key)
        finally:
            _wipe(private_key)
        return EphemeralTLSMaterial(
            context,
            expires_at=timestamp + _LEAF_VALIDITY_SECONDS,
            leaf_fingerprint=leaf.fingerprint(hashes.SHA256()).hex(),
        )

    def _ensure_platform(self) -> None:
        if self._platform not in SUPPORTED_CONNECTOR_PLATFORMS:
            raise ConnectorError(ConnectorErrorCode.UNSUPPORTED_PLATFORM)


def build_pinned_client_context(authority: ConnectorCACertificate) -> ssl.SSLContext:
    """Trust only the pinned Connector identity CA, never ambient public roots."""

    if not isinstance(authority, ConnectorCACertificate):
        raise TypeError("Pinned TLS requires a verified Connector CA.")
    verified = _verified_ca(
        authority.certificate,
        authority.pem,
        authority.connector_identity,
        now=_timestamp_now(),
    )
    if verified.fingerprint != authority.fingerprint:
        raise ConnectorError(ConnectorErrorCode.CONNECTOR_TLS_FAILED)
    context = _base_client_context()
    context.verify_mode = ssl.CERT_REQUIRED
    context.load_verify_locations(cadata=authority.pem.decode("ascii"))
    if hasattr(ssl, "VERIFY_X509_STRICT"):
        context.verify_flags |= ssl.VERIFY_X509_STRICT
    return context


def build_initial_pairing_client_context() -> ssl.SSLContext:
    """Create the pairing-only unpinned context; it authorizes no operation."""

    context = _base_client_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


def verify_connector_ca_der(
    payload: bytes,
    expected_identity: DevicePublicIdentity,
    *,
    now: int | None = None,
) -> ConnectorCACertificate:
    """Verify signed pairing CA bytes before they become a persistent pin."""

    if (
        type(payload) is not bytes
        or not 0 < len(payload) <= MAX_TLS_CA_DER_BYTES
        or not isinstance(expected_identity, DevicePublicIdentity)
    ):
        raise ConnectorError(ConnectorErrorCode.CONNECTOR_TLS_FAILED)
    timestamp = _timestamp_now() if now is None else _require_timestamp(now)
    try:
        certificate = x509.load_der_x509_certificate(payload)
        if certificate.public_bytes(serialization.Encoding.DER) != payload:
            raise ValueError("non-canonical CA DER")
        pem = certificate.public_bytes(serialization.Encoding.PEM)
        return _verified_ca(certificate, pem, expected_identity, now=timestamp)
    except ConnectorError:
        raise
    except (TypeError, ValueError):
        raise ConnectorError(ConnectorErrorCode.CONNECTOR_TLS_FAILED) from None


def verify_connector_leaf_der(
    payload: bytes,
    authority: ConnectorCACertificate,
    *,
    endpoint_host: str,
    now: int | None = None,
) -> str:
    """Verify the peer leaf against the CA and exact numeric endpoint."""

    if type(payload) is not bytes or not 0 < len(payload) <= MAX_TLS_CA_DER_BYTES:
        raise ConnectorError(ConnectorErrorCode.CONNECTOR_TLS_FAILED)
    if not isinstance(authority, ConnectorCACertificate):
        raise TypeError("Connector leaf verification requires a verified CA.")
    timestamp = _timestamp_now() if now is None else _require_timestamp(now)
    address = validate_private_bind_host(endpoint_host)
    verified_authority = _verified_ca(
        authority.certificate,
        authority.pem,
        authority.connector_identity,
        now=timestamp,
    )
    try:
        certificate = x509.load_der_x509_certificate(payload)
        if certificate.public_bytes(serialization.Encoding.DER) != payload:
            raise ValueError("non-canonical leaf DER")
        public_key = certificate.public_key()
        authority_public_key = verified_authority.certificate.public_key()
        if not isinstance(public_key, Ed25519PublicKey) or not isinstance(
            authority_public_key, Ed25519PublicKey
        ):
            raise ValueError("invalid leaf key")
        extensions = {extension.oid: extension for extension in certificate.extensions}
        if set(extensions) != {
            ExtensionOID.BASIC_CONSTRAINTS,
            ExtensionOID.KEY_USAGE,
            ExtensionOID.EXTENDED_KEY_USAGE,
            ExtensionOID.SUBJECT_ALTERNATIVE_NAME,
            ExtensionOID.SUBJECT_KEY_IDENTIFIER,
            ExtensionOID.AUTHORITY_KEY_IDENTIFIER,
        }:
            raise ValueError("invalid leaf extensions")
        basic_extension = extensions[ExtensionOID.BASIC_CONSTRAINTS]
        usage_extension = extensions[ExtensionOID.KEY_USAGE]
        extended_usage_extension = extensions[ExtensionOID.EXTENDED_KEY_USAGE]
        san_extension = extensions[ExtensionOID.SUBJECT_ALTERNATIVE_NAME]
        subject_key_extension = extensions[ExtensionOID.SUBJECT_KEY_IDENTIFIER]
        authority_key_extension = extensions[ExtensionOID.AUTHORITY_KEY_IDENTIFIER]
        basic = basic_extension.value
        usage = usage_extension.value
        extended_usage = extended_usage_extension.value
        san = san_extension.value
        subject_key = subject_key_extension.value
        authority_key = authority_key_extension.value
        if (
            not isinstance(basic, x509.BasicConstraints)
            or not isinstance(usage, x509.KeyUsage)
            or not isinstance(extended_usage, x509.ExtendedKeyUsage)
            or not isinstance(san, x509.SubjectAlternativeName)
            or not isinstance(subject_key, x509.SubjectKeyIdentifier)
            or not isinstance(authority_key, x509.AuthorityKeyIdentifier)
        ):
            raise ValueError("invalid leaf extension values")
        expected_subject = x509.Name(
            [
                x509.NameAttribute(
                    NameOID.COMMON_NAME,
                    f"{_LEAF_COMMON_NAME_PREFIX}{authority.connector_identity.key_id}",
                )
            ]
        )
        expected_subject_key = x509.SubjectKeyIdentifier.from_public_key(public_key)
        authority_subject_key = (
            verified_authority.certificate.extensions.get_extension_for_class(
                x509.SubjectKeyIdentifier
            ).value
        )
        validity_seconds = (
            certificate.not_valid_after_utc - certificate.not_valid_before_utc
        ).total_seconds()
        if (
            certificate.version is not x509.Version.v3
            or certificate.signature_algorithm_oid != SignatureAlgorithmOID.ED25519
            or certificate.signature_hash_algorithm is not None
            or certificate.subject != expected_subject
            or certificate.issuer != verified_authority.certificate.subject
            or not basic_extension.critical
            or not usage_extension.critical
            or not extended_usage_extension.critical
            or san_extension.critical
            or subject_key_extension.critical
            or authority_key_extension.critical
            or basic.ca
            or basic.path_length is not None
            or not usage.digital_signature
            or usage.content_commitment
            or usage.key_encipherment
            or usage.data_encipherment
            or usage.key_agreement
            or usage.key_cert_sign
            or usage.crl_sign
            or tuple(extended_usage) != (ExtendedKeyUsageOID.SERVER_AUTH,)
            or san != x509.SubjectAlternativeName([x509.IPAddress(address)])
            or subject_key.digest != expected_subject_key.digest
            or authority_key.key_identifier != authority_subject_key.digest
            or authority_key.authority_cert_issuer is not None
            or authority_key.authority_cert_serial_number is not None
            or not 0 < validity_seconds <= _LEAF_VALIDITY_SECONDS + _CLOCK_SKEW_SECONDS
            or not certificate.not_valid_before_utc.timestamp()
            <= timestamp
            < certificate.not_valid_after_utc.timestamp()
        ):
            raise ValueError("invalid leaf constraints")
        authority_public_key.verify(
            certificate.signature, certificate.tbs_certificate_bytes
        )
        return certificate.fingerprint(hashes.SHA256()).hex()
    except (InvalidSignature, TypeError, ValueError, x509.ExtensionNotFound):
        raise ConnectorError(ConnectorErrorCode.CONNECTOR_TLS_FAILED) from None


def validate_private_bind_host(
    host: str,
) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    """Accept only explicit loopback, RFC1918, CGNAT, or IPv6 ULA literals."""

    if type(host) is not str or not host or "%" in host:
        raise ConnectorError(ConnectorErrorCode.CONNECTOR_TLS_FAILED)
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        raise ConnectorError(ConnectorErrorCode.CONNECTOR_TLS_FAILED) from None
    if (
        address.is_unspecified
        or address.is_multicast
        or address.is_link_local
        or (address.is_reserved and not address.is_loopback)
    ):
        raise ConnectorError(ConnectorErrorCode.CONNECTOR_TLS_FAILED)
    networks = (
        ipaddress.ip_network("127.0.0.0/8"),
        ipaddress.ip_network("10.0.0.0/8"),
        ipaddress.ip_network("172.16.0.0/12"),
        ipaddress.ip_network("192.168.0.0/16"),
        ipaddress.ip_network("100.64.0.0/10"),
        ipaddress.ip_network("::1/128"),
        ipaddress.ip_network("fc00::/7"),
    )
    if not any(
        address in network for network in networks if address.version == network.version
    ):
        raise ConnectorError(ConnectorErrorCode.CONNECTOR_TLS_FAILED)
    return address


def _create_ca_certificate(
    signer: DevicePrivateIdentity, *, now: int
) -> x509.Certificate:
    public_key = Ed25519PublicKey.from_public_bytes(
        signer.public_identity.raw_public_key
    )
    name = x509.Name(
        [
            x509.NameAttribute(
                NameOID.COMMON_NAME,
                f"{_CA_COMMON_NAME_PREFIX}{signer.public_identity.key_id}",
            )
        ]
    )
    moment = datetime.fromtimestamp(now, UTC)
    builder = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(public_key)
        .serial_number(x509.random_serial_number())
        .not_valid_before(moment - timedelta(seconds=_CLOCK_SKEW_SECONDS))
        .not_valid_after(moment + timedelta(seconds=_CA_VALIDITY_SECONDS))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(public_key), critical=False
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(public_key),
            critical=False,
        )
    )
    return signer._sign_certificate(builder)


def _create_leaf_certificate(
    signer: DevicePrivateIdentity,
    authority: x509.Certificate,
    leaf_key: Ed25519PrivateKey,
    *,
    address: ipaddress.IPv4Address | ipaddress.IPv6Address,
    now: int,
) -> x509.Certificate:
    moment = datetime.fromtimestamp(now, UTC)
    authority_public_key = authority.public_key()
    if not isinstance(authority_public_key, Ed25519PublicKey):
        raise ConnectorError(ConnectorErrorCode.CONNECTOR_TLS_FAILED)
    subject = x509.Name(
        [
            x509.NameAttribute(
                NameOID.COMMON_NAME,
                f"{_LEAF_COMMON_NAME_PREFIX}{signer.public_identity.key_id}",
            )
        ]
    )
    builder = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(authority.subject)
        .public_key(leaf_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(moment - timedelta(seconds=_CLOCK_SKEW_SECONDS))
        .not_valid_after(moment + timedelta(seconds=_LEAF_VALIDITY_SECONDS))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=True
        )
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(address)]), critical=False
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(leaf_key.public_key()),
            critical=False,
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(authority_public_key),
            critical=False,
        )
    )
    return signer._sign_certificate(builder)


def _verified_ca(
    certificate: x509.Certificate,
    pem: bytes,
    expected_identity: DevicePublicIdentity,
    *,
    now: int,
) -> ConnectorCACertificate:
    try:
        public_key = certificate.public_key()
        if not isinstance(public_key, Ed25519PublicKey):
            raise ValueError("invalid CA key")
        raw = public_key.public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
        identity = DevicePublicIdentity(raw)
        extensions = {extension.oid: extension for extension in certificate.extensions}
        if set(extensions) != {
            ExtensionOID.BASIC_CONSTRAINTS,
            ExtensionOID.KEY_USAGE,
            ExtensionOID.SUBJECT_KEY_IDENTIFIER,
            ExtensionOID.AUTHORITY_KEY_IDENTIFIER,
        }:
            raise ValueError("invalid CA extensions")
        basic_extension = extensions[ExtensionOID.BASIC_CONSTRAINTS]
        usage_extension = extensions[ExtensionOID.KEY_USAGE]
        subject_key_extension = extensions[ExtensionOID.SUBJECT_KEY_IDENTIFIER]
        authority_key_extension = extensions[ExtensionOID.AUTHORITY_KEY_IDENTIFIER]
        basic = basic_extension.value
        usage = usage_extension.value
        subject_key = subject_key_extension.value
        authority_key = authority_key_extension.value
        if (
            not isinstance(basic, x509.BasicConstraints)
            or not isinstance(usage, x509.KeyUsage)
            or not isinstance(subject_key, x509.SubjectKeyIdentifier)
            or not isinstance(authority_key, x509.AuthorityKeyIdentifier)
        ):
            raise ValueError("invalid CA extension values")
        expected_subject_key = x509.SubjectKeyIdentifier.from_public_key(public_key)
        expected_name = x509.Name(
            [
                x509.NameAttribute(
                    NameOID.COMMON_NAME,
                    f"{_CA_COMMON_NAME_PREFIX}{expected_identity.key_id}",
                )
            ]
        )
        validity_seconds = (
            certificate.not_valid_after_utc - certificate.not_valid_before_utc
        ).total_seconds()
        if (
            pem != certificate.public_bytes(serialization.Encoding.PEM)
            or identity != expected_identity
            or certificate.version is not x509.Version.v3
            or certificate.signature_algorithm_oid != SignatureAlgorithmOID.ED25519
            or certificate.signature_hash_algorithm is not None
            or certificate.subject != expected_name
            or certificate.issuer != expected_name
            or not basic_extension.critical
            or not usage_extension.critical
            or subject_key_extension.critical
            or authority_key_extension.critical
            or not basic.ca
            or basic.path_length != 0
            or not usage.digital_signature
            or usage.content_commitment
            or usage.key_encipherment
            or usage.data_encipherment
            or usage.key_agreement
            or not usage.key_cert_sign
            or not usage.crl_sign
            or subject_key.digest != expected_subject_key.digest
            or authority_key.key_identifier != subject_key.digest
            or authority_key.authority_cert_issuer is not None
            or authority_key.authority_cert_serial_number is not None
            or not 0 < validity_seconds <= _CA_VALIDITY_SECONDS + _CLOCK_SKEW_SECONDS
            or not certificate.not_valid_before_utc.timestamp()
            <= now
            < certificate.not_valid_after_utc.timestamp()
        ):
            raise ValueError("invalid CA constraints")
        public_key.verify(certificate.signature, certificate.tbs_certificate_bytes)
    except (InvalidSignature, TypeError, ValueError, x509.ExtensionNotFound):
        raise ConnectorError(ConnectorErrorCode.CONNECTOR_TLS_FAILED) from None
    return ConnectorCACertificate(
        certificate,
        pem,
        certificate.fingerprint(hashes.SHA256()).hex(),
        expected_identity,
    )


def _server_context(certificate_chain: bytes, private_key: bytearray) -> ssl.SSLContext:
    if (
        type(certificate_chain) is not bytes
        or not 0 < len(certificate_chain) <= _MAX_CERTIFICATE_CHAIN_BYTES
        or type(private_key) is not bytearray
        or not 0 < len(private_key) <= _MAX_TLS_PRIVATE_KEY_BYTES
    ):
        raise ConnectorError(ConnectorErrorCode.CONNECTOR_TLS_FAILED)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    _harden_context(context)
    if hasattr(context, "num_tickets"):
        context.num_tickets = 0
    read_descriptors: list[int] = []
    write_descriptors: list[int] = []
    writer_threads: list[threading.Thread] = []
    writer_failed: list[bool] = []
    load_failed = False
    try:
        certificate_read, certificate_write = os.pipe()
        read_descriptors.append(certificate_read)
        write_descriptors.append(certificate_write)
        key_read, key_write = os.pipe()
        read_descriptors.append(key_read)
        write_descriptors.append(key_write)
        writer_threads.append(
            _start_pipe_writer(certificate_write, certificate_chain, writer_failed)
        )
        write_descriptors.remove(certificate_write)
        writer_threads.append(_start_pipe_writer(key_write, private_key, writer_failed))
        write_descriptors.remove(key_write)
        context.load_cert_chain(
            f"/dev/fd/{certificate_read}",
            f"/dev/fd/{key_read}",
        )
    except (OSError, RuntimeError, ssl.SSLError, ValueError):
        load_failed = True
    finally:
        for descriptor in write_descriptors:
            try:
                os.close(descriptor)
            except OSError:
                pass
        for descriptor in read_descriptors:
            try:
                os.close(descriptor)
            except OSError:
                pass
        for thread in writer_threads:
            thread.join()
    if load_failed or writer_failed:
        raise ConnectorError(ConnectorErrorCode.CONNECTOR_TLS_FAILED) from None
    return context


def _start_pipe_writer(
    descriptor: int,
    payload: bytes | bytearray,
    failures: list[bool],
) -> threading.Thread:
    def write_payload() -> None:
        try:
            view = memoryview(payload)
            offset = 0
            while offset < len(view):
                written = os.write(descriptor, view[offset:])
                if written <= 0:
                    raise OSError("TLS pipe write failed")
                offset += written
        except OSError:
            failures.append(True)
        finally:
            try:
                os.close(descriptor)
            except OSError:
                pass

    thread = threading.Thread(target=write_payload, daemon=True)
    try:
        thread.start()
    except RuntimeError:
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise
    return thread


def _wipe(buffer: bytearray) -> None:
    for index in range(len(buffer)):
        buffer[index] = 0
    buffer.clear()


def _base_client_context() -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    _harden_context(context)
    return context


def _harden_context(context: ssl.SSLContext) -> None:
    if not ssl.HAS_TLSv1_3:
        raise ConnectorError(ConnectorErrorCode.CONNECTOR_TLS_FAILED)
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    context.options |= ssl.OP_NO_COMPRESSION | ssl.OP_NO_TICKET
    context.set_alpn_protocols(_ALPN_PROTOCOLS)


def _timestamp_now() -> int:
    return _require_timestamp(int(datetime.now(UTC).timestamp()))


def _require_timestamp(value: object) -> int:
    if type(value) is not int or not 0 <= value <= MAX_TIMESTAMP_SECONDS:
        raise ConnectorError(ConnectorErrorCode.CONNECTOR_TLS_FAILED)
    return value
