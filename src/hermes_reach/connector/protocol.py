"""Canonical, closed records for the transport-neutral Connector v1 protocol."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import Final, Never, TypeAlias, cast
from urllib.parse import urlsplit

from ..catalog import DataScope, OperationSpec, get_operation, get_source
from ..contracts import OperationCall, operation_call_is_valid
from ..normalized import (
    MAX_NORMALIZED_INTEGER,
    NORMALIZED_MEDIA_VERSION,
    media_metadata_characters,
    normalized_item_characters,
)
from .errors import (
    ConnectorErrorCategory,
    ConnectorErrorCode,
    codes_for_category,
)
from .identity import (
    DevicePrivateIdentity,
    DevicePublicIdentity,
    SignatureDomain,
)
from .limits import (
    CONNECTOR_PROTOCOL_VERSION,
    DEFAULT_FILE_GRANT_TTL_SECONDS,
    DEVICE_NONCE_BYTES,
    ID_BASE32_LENGTH,
    KEY_ID_BASE32_LENGTH,
    MAX_CLOCK_SKEW_SECONDS,
    MAX_DEVICE_LABEL_LENGTH,
    MAX_FILE_BYTES,
    MAX_FILE_GRANT_TTL_SECONDS,
    MAX_FRAME_BYTES,
    MAX_GRANT_SCOPES,
    MAX_GRANT_TTL_SECONDS,
    MAX_GRANT_USES,
    MAX_JSON_CONTAINER_ITEMS,
    MAX_JSON_DEPTH,
    MAX_OPERATION_RESULT_BYTES,
    MAX_PROTECTED_OPERATION_BYTES,
    MAX_RECEIPT_TTL_SECONDS,
    MAX_REQUEST_TTL_SECONDS,
    MAX_TIMESTAMP_SECONDS,
    MAX_TLS_CA_DER_BYTES,
    MIN_TIMESTAMP_SECONDS,
    PAIRING_SAS_LENGTH,
    PAIRING_TTL_SECONDS,
)

JSONScalar: TypeAlias = None | bool | int | str
JSONValue: TypeAlias = JSONScalar | list["JSONValue"] | dict[str, "JSONValue"]

_SIGNATURE_ALGORITHM: Final = "ed25519-v1"
_PAYLOAD_DIGEST_ALGORITHM: Final = "sha256-v1"
_OPERATION_PAYLOAD_DOMAIN: Final = b"hermes-reach:connector:v1:operation-call\x00"
_OPERATION_RESULT_DOMAIN: Final = b"hermes-reach:connector:v1:operation-result\x00"
_RECORD_DIGEST_DOMAIN: Final = b"hermes-reach:connector:v1:record-digest\x00"
_TRANSCRIPT_DOMAIN: Final = b"hermes-reach:connector:v1:pairing-transcript\x00"
_CROCKFORD_ALPHABET: Final = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_HEX_32: Final = re.compile(r"[0-9a-f]{32}")
_HEX_64: Final = re.compile(r"[0-9a-f]{64}")
_METADATA_ID: Final = re.compile(r"[a-z0-9][a-z0-9._-]{0,127}")
_BACKEND_VERSION: Final = re.compile(r"[a-z0-9][a-z0-9._+-]{0,127}")
_FAILURE_CLASSES: Final = frozenset(
    category.value for category in ConnectorErrorCategory
)
_DECISIONS: Final = frozenset({"allow", "deny"})
_OUTCOMES: Final = frozenset({"ok", "error"})
_SAFE_BACKENDS: Final = frozenset(
    {
        ("reach-bounded-executor-v1", "1"),
        ("opencli", "1.8.6-hermes.1"),
        ("xueqiu-api", "1.5.0+search.v1"),
    }
)
_ITEM_KINDS: Final = frozenset(
    {"content", "entry", "topic", "reply", "profile", "result"}
)
_MEDIA_COVERAGE: Final = frozenset({"complete", "partial", "unknown"})
_SUBTITLE_ORIGINS: Final = frozenset({"manual", "automatic"})
_LANGUAGE_TAG: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,31}\Z")


class ProtocolValidationError(ValueError):
    """A public-safe protocol validation failure with no untrusted context."""


class ReceiptContextMismatchError(ProtocolValidationError):
    """A validly signed receipt belongs to a different request context."""


class ReceiptExpiredError(ProtocolValidationError):
    """A receipt or successful delivery is outside its trusted time window."""


def _reject_float(_: str) -> Never:
    raise ProtocolValidationError("Connector JSON does not permit floating values.")


def _reject_constant(_: str) -> Never:
    raise ProtocolValidationError("Connector JSON contains an unsupported constant.")


def _object_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ProtocolValidationError("Connector JSON contains a duplicate key.")
        result[key] = value
    return result


def _validate_json_value(value: object, *, depth: int = 0) -> None:
    if depth > MAX_JSON_DEPTH:
        raise ProtocolValidationError("Connector JSON exceeds the nesting limit.")
    if value is None or type(value) in {bool, int}:
        return
    if type(value) is str:
        if any(
            ord(character) < 0x20 or 0xD800 <= ord(character) <= 0xDFFF
            for character in value
        ):
            raise ProtocolValidationError("Connector JSON contains invalid Unicode.")
        return
    if type(value) is list:
        sequence = cast(list[object], value)
        if len(sequence) > MAX_JSON_CONTAINER_ITEMS:
            raise ProtocolValidationError("Connector JSON contains too many values.")
        for item in sequence:
            _validate_json_value(item, depth=depth + 1)
        return
    if type(value) is dict:
        mapping = cast(dict[object, object], value)
        if len(mapping) > MAX_JSON_CONTAINER_ITEMS:
            raise ProtocolValidationError("Connector JSON contains too many fields.")
        for key, item in mapping.items():
            if type(key) is not str:
                raise ProtocolValidationError(
                    "Connector JSON object keys must be strings."
                )
            _validate_json_value(key, depth=depth + 1)
            _validate_json_value(item, depth=depth + 1)
        return
    raise ProtocolValidationError("Connector JSON contains an unsupported value type.")


def canonical_json_bytes(value: object) -> bytes:
    """Serialize only the approved exact JSON value model."""

    _validate_json_value(value)
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, UnicodeError, ValueError):
        raise ProtocolValidationError("Connector JSON cannot be serialized.") from None
    if not encoded or len(encoded) > MAX_FRAME_BYTES:
        raise ProtocolValidationError("Connector JSON exceeds the frame limit.")
    return encoded


def load_canonical_json(raw: bytes, *, max_bytes: int = MAX_FRAME_BYTES) -> JSONValue:
    """Parse canonical UTF-8 JSON with duplicate and alternate-form rejection."""

    if (
        type(raw) is not bytes
        or type(max_bytes) is not int
        or not 0 < len(raw) <= max_bytes <= MAX_FRAME_BYTES
    ):
        raise ProtocolValidationError("The Connector frame size is invalid.")
    if raw.startswith(b"\xef\xbb\xbf"):
        raise ProtocolValidationError("Connector JSON must not contain a BOM.")
    try:
        text = raw.decode("utf-8", errors="strict")
        value: object = json.loads(
            text,
            object_pairs_hook=_object_pairs,
            parse_float=_reject_float,
            parse_constant=_reject_constant,
        )
    except ProtocolValidationError:
        raise
    except (UnicodeError, json.JSONDecodeError, ValueError):
        raise ProtocolValidationError("The Connector frame is malformed.") from None
    _validate_json_value(value)
    if canonical_json_bytes(value) != raw:
        raise ProtocolValidationError("The Connector frame is not canonical.")
    return cast(JSONValue, value)


@dataclass(frozen=True, slots=True)
class GrantScope:
    """One exact catalog source-operation and its maximum approved data scope."""

    source: str
    operation: str
    data_scope: DataScope
    capability_id: str | None = None

    def __post_init__(self) -> None:
        operation = _catalog_operation(self.source, self.operation)
        if operation.runtime.data_scope != self.data_scope:
            raise ProtocolValidationError("The grant data scope is invalid.")
        if self.capability_id is not None and not _is_canonical_id(self.capability_id):
            raise ProtocolValidationError("The capability identifier is invalid.")


@dataclass(frozen=True, slots=True)
class GrantClaims:
    """Immutable inspectable authority claims signed by the Connector."""

    grant_id: str
    revision: int
    issuer_key_id: str
    subject_key_id: str
    issued_at: int
    not_before: int
    expires_at: int
    policy_revision: int
    max_uses: int
    scopes: tuple[GrantScope, ...]
    signature_algorithm: str = _SIGNATURE_ALGORITHM

    def __post_init__(self) -> None:
        _require_id(self.grant_id)
        _require_key_id(self.issuer_key_id)
        _require_key_id(self.subject_key_id)
        _require_positive_int(self.revision, "grant revision")
        _require_positive_int(self.policy_revision, "policy revision")
        _require_timestamp(self.issued_at)
        _require_timestamp(self.not_before)
        _require_timestamp(self.expires_at)
        if (
            self.signature_algorithm != _SIGNATURE_ALGORITHM
            or self.issuer_key_id == self.subject_key_id
            or self.issued_at > self.not_before
            or self.not_before >= self.expires_at
            or self.expires_at - self.issued_at > MAX_GRANT_TTL_SECONDS
            or type(self.max_uses) is not int
            or not 1 <= self.max_uses <= MAX_GRANT_USES
        ):
            raise ProtocolValidationError("The signed grant bounds are invalid.")
        _require_scopes(self.scopes)


@dataclass(frozen=True, slots=True)
class PairingInit:
    message_id: str
    pairing_id: str
    vps_public_key: str
    vps_key_id: str
    device_label: str
    vps_nonce: str
    endpoint_digest: str
    requested_scopes: tuple[GrantScope, ...]
    grant_expires_at: int
    grant_max_uses: int
    issued_at: int
    deadline: int
    signature: bytes
    signature_algorithm: str = _SIGNATURE_ALGORITHM

    def __post_init__(self) -> None:
        _require_id(self.message_id)
        _require_id(self.pairing_id)
        public_identity = _require_public_key(self.vps_public_key)
        _require_key_id(self.vps_key_id)
        if public_identity.key_id != self.vps_key_id:
            raise ProtocolValidationError("The pairing device identity is invalid.")
        require_printable_metadata(self.device_label, MAX_DEVICE_LABEL_LENGTH)
        _require_nonce(self.vps_nonce)
        _require_digest(self.endpoint_digest)
        _require_scopes(self.requested_scopes)
        _require_timestamp(self.issued_at)
        _require_timestamp(self.deadline)
        _require_timestamp(self.grant_expires_at)
        if (
            self.signature_algorithm != _SIGNATURE_ALGORITHM
            or not self.issued_at < self.deadline
            or self.deadline - self.issued_at > PAIRING_TTL_SECONDS
            or not self.issued_at < self.grant_expires_at
            or self.grant_expires_at - self.issued_at > MAX_GRANT_TTL_SECONDS
            or type(self.grant_max_uses) is not int
            or not 1 <= self.grant_max_uses <= MAX_GRANT_USES
        ):
            raise ProtocolValidationError("The pairing request bounds are invalid.")
        _require_signature(self.signature)


@dataclass(frozen=True, slots=True)
class PairingChallenge:
    message_id: str
    pairing_id: str
    init_digest: str
    vps_key_id: str
    connector_public_key: str
    connector_key_id: str
    connector_nonce: str
    tls_ca_der: str
    tls_ca_fingerprint: str
    tls_leaf_fingerprint: str
    issued_at: int
    deadline: int
    signature: bytes
    signature_algorithm: str = _SIGNATURE_ALGORITHM

    def __post_init__(self) -> None:
        _require_id(self.message_id)
        _require_id(self.pairing_id)
        _require_digest(self.init_digest)
        _require_key_id(self.vps_key_id)
        public_identity = _require_public_key(self.connector_public_key)
        _require_key_id(self.connector_key_id)
        if public_identity.key_id != self.connector_key_id:
            raise ProtocolValidationError("The pairing Connector identity is invalid.")
        _require_nonce(self.connector_nonce)
        ca_der = _decode_bounded_base64url(
            self.tls_ca_der, maximum=MAX_TLS_CA_DER_BYTES
        )
        _require_digest(self.tls_ca_fingerprint)
        _require_digest(self.tls_leaf_fingerprint)
        _require_timestamp(self.issued_at)
        _require_timestamp(self.deadline)
        if (
            self.signature_algorithm != _SIGNATURE_ALGORITHM
            or self.connector_key_id == self.vps_key_id
            or hashlib.sha256(ca_der).hexdigest() != self.tls_ca_fingerprint
            or not self.issued_at < self.deadline
            or self.deadline - self.issued_at > PAIRING_TTL_SECONDS
        ):
            raise ProtocolValidationError("The pairing challenge bounds are invalid.")
        _require_signature(self.signature)


@dataclass(frozen=True, slots=True)
class PairingComplete:
    message_id: str
    pairing_id: str
    transcript_digest: str
    connector_key_id: str
    vps_key_id: str
    signed_grant_digest: str
    completed_at: int
    signature: bytes
    signature_algorithm: str = _SIGNATURE_ALGORITHM

    def __post_init__(self) -> None:
        _require_id(self.message_id)
        _require_id(self.pairing_id)
        _require_digest(self.transcript_digest)
        _require_key_id(self.connector_key_id)
        _require_key_id(self.vps_key_id)
        _require_digest(self.signed_grant_digest)
        _require_timestamp(self.completed_at)
        if (
            self.signature_algorithm != _SIGNATURE_ALGORITHM
            or self.connector_key_id == self.vps_key_id
        ):
            raise ProtocolValidationError(
                "The pairing completion algorithm is invalid."
            )
        _require_signature(self.signature)


@dataclass(frozen=True, slots=True)
class SignedGrant:
    message_id: str
    claims: GrantClaims
    signature: bytes

    def __post_init__(self) -> None:
        _require_id(self.message_id)
        if not isinstance(self.claims, GrantClaims):
            raise ProtocolValidationError("The signed grant claims are invalid.")
        _require_signature(self.signature)


@dataclass(frozen=True, slots=True)
class PairingResolution:
    """Unsigned envelope carrying the exact independently signed pairing result."""

    message_id: str
    pairing_id: str
    signed_grant: SignedGrant
    pairing_complete: PairingComplete

    def __post_init__(self) -> None:
        _require_id(self.message_id)
        _require_id(self.pairing_id)
        if not isinstance(self.signed_grant, SignedGrant) or not isinstance(
            self.pairing_complete, PairingComplete
        ):
            raise ProtocolValidationError("The pairing resolution records are invalid.")
        grant = self.signed_grant.claims
        complete = self.pairing_complete
        if (
            complete.pairing_id != self.pairing_id
            or complete.signed_grant_digest != record_digest(self.signed_grant)
            or complete.connector_key_id != grant.issuer_key_id
            or complete.vps_key_id != grant.subject_key_id
        ):
            raise ProtocolValidationError("The pairing resolution context is invalid.")


@dataclass(frozen=True, slots=True)
class SignedRequest:
    message_id: str
    request_id: str
    trace_id: str
    audience_key_id: str
    subject_key_id: str
    grant_id: str
    grant_revision: int
    policy_revision: int
    source: str
    operation: str
    issued_at: int
    deadline: int
    payload_digest: str
    signature: bytes
    payload_digest_algorithm: str = _PAYLOAD_DIGEST_ALGORITHM
    signature_algorithm: str = _SIGNATURE_ALGORITHM

    def __post_init__(self) -> None:
        _require_id(self.message_id)
        _require_id(self.request_id)
        if type(self.trace_id) is not str or _HEX_32.fullmatch(self.trace_id) is None:
            raise ProtocolValidationError("The Reach trace identifier is invalid.")
        _require_key_id(self.audience_key_id)
        _require_key_id(self.subject_key_id)
        _require_id(self.grant_id)
        _require_positive_int(self.grant_revision, "grant revision")
        _require_positive_int(self.policy_revision, "policy revision")
        _catalog_operation(self.source, self.operation)
        _require_timestamp(self.issued_at)
        _require_timestamp(self.deadline)
        _require_digest(self.payload_digest)
        if (
            self.payload_digest_algorithm != _PAYLOAD_DIGEST_ALGORITHM
            or self.signature_algorithm != _SIGNATURE_ALGORITHM
            or self.audience_key_id == self.subject_key_id
            or not self.issued_at < self.deadline
            or self.deadline - self.issued_at > MAX_REQUEST_TTL_SECONDS
        ):
            raise ProtocolValidationError("The signed request bounds are invalid.")
        _require_signature(self.signature)


@dataclass(frozen=True, slots=True)
class ReceiptFailure:
    failure_class: str
    cause_code: ConnectorErrorCode

    def __post_init__(self) -> None:
        if (
            self.failure_class not in _FAILURE_CLASSES
            or not isinstance(self.cause_code, ConnectorErrorCode)
            or self.cause_code
            not in codes_for_category(ConnectorErrorCategory(self.failure_class))
        ):
            raise ProtocolValidationError("The receipt failure is invalid.")


@dataclass(frozen=True, slots=True)
class ReceiptUsage:
    sequence: int
    remaining: int

    def __post_init__(self) -> None:
        _require_positive_int(self.sequence, "use sequence")
        if type(self.remaining) is not int or not 0 <= self.remaining <= MAX_GRANT_USES:
            raise ProtocolValidationError("The receipt usage is invalid.")


@dataclass(frozen=True, slots=True)
class PublicBackendIdentity:
    backend_id: str
    backend_version: str

    def __post_init__(self) -> None:
        if (
            type(self.backend_id) is not str
            or type(self.backend_version) is not str
            or _METADATA_ID.fullmatch(self.backend_id) is None
            or _BACKEND_VERSION.fullmatch(self.backend_version) is None
            or (self.backend_id, self.backend_version) not in _SAFE_BACKENDS
        ):
            raise ProtocolValidationError(
                "The public backend identity is not approved."
            )


@dataclass(frozen=True, slots=True)
class SignedReceipt:
    message_id: str
    receipt_id: str
    request_id: str
    trace_id: str
    issuer_key_id: str
    subject_key_id: str
    grant_id: str
    grant_revision: int
    policy_revision: int
    source: str
    operation: str
    decision: str
    failure: ReceiptFailure | None
    usage: ReceiptUsage | None
    backend: PublicBackendIdentity | None
    started_at: int
    ended_at: int
    expires_at: int
    result_count: int
    truncated: bool
    result_digest: str | None
    outcome: str
    payload_digest: str
    signature: bytes
    payload_digest_algorithm: str = _PAYLOAD_DIGEST_ALGORITHM
    result_digest_algorithm: str = _PAYLOAD_DIGEST_ALGORITHM
    signature_algorithm: str = _SIGNATURE_ALGORITHM

    def __post_init__(self) -> None:
        _require_id(self.message_id)
        _require_id(self.receipt_id)
        _require_id(self.request_id)
        if type(self.trace_id) is not str or _HEX_32.fullmatch(self.trace_id) is None:
            raise ProtocolValidationError("The receipt trace identifier is invalid.")
        _require_key_id(self.issuer_key_id)
        _require_key_id(self.subject_key_id)
        _require_id(self.grant_id)
        _require_positive_int(self.grant_revision, "grant revision")
        _require_positive_int(self.policy_revision, "policy revision")
        operation = _catalog_operation(self.source, self.operation)
        _require_timestamp(self.started_at)
        _require_timestamp(self.ended_at)
        _require_timestamp(self.expires_at)
        _require_digest(self.payload_digest)
        if self.result_digest is not None:
            _require_digest(self.result_digest)
        if (
            self.decision not in _DECISIONS
            or self.outcome not in _OUTCOMES
            or self.payload_digest_algorithm != _PAYLOAD_DIGEST_ALGORITHM
            or self.result_digest_algorithm != _PAYLOAD_DIGEST_ALGORITHM
            or self.signature_algorithm != _SIGNATURE_ALGORITHM
            or self.issuer_key_id == self.subject_key_id
            or not self.started_at <= self.ended_at < self.expires_at
            or self.expires_at - self.ended_at > MAX_RECEIPT_TTL_SECONDS
            or type(self.result_count) is not int
            or not 0 <= self.result_count <= operation.runtime.maximum_items
            or type(self.truncated) is not bool
            or (
                self.failure is not None
                and not isinstance(self.failure, ReceiptFailure)
            )
            or (self.usage is not None and not isinstance(self.usage, ReceiptUsage))
            or (
                self.backend is not None
                and not isinstance(self.backend, PublicBackendIdentity)
            )
        ):
            raise ProtocolValidationError("The signed receipt bounds are invalid.")
        if self.decision == "deny" and (
            self.outcome != "error"
            or self.failure is None
            or self.usage is not None
            or self.backend is not None
            or self.result_count != 0
            or self.truncated
        ):
            raise ProtocolValidationError(
                "A denied receipt has invalid result metadata."
            )
        if self.decision == "allow" and self.usage is None:
            raise ProtocolValidationError(
                "An accepted receipt requires use accounting."
            )
        if (self.outcome == "ok") != (self.failure is None):
            raise ProtocolValidationError("The receipt outcome and failure disagree.")
        if (self.outcome == "ok") != (self.result_digest is not None):
            raise ProtocolValidationError(
                "The receipt outcome and result digest disagree."
            )
        if self.outcome == "error" and (self.result_count != 0 or self.truncated):
            raise ProtocolValidationError("A failed receipt cannot describe a result.")
        if self.outcome == "ok" and self.backend is None:
            raise ProtocolValidationError(
                "A successful receipt requires an approved backend identity."
            )
        _require_signature(self.signature)


@dataclass(frozen=True, slots=True)
class FileGrant:
    message_id: str
    file_grant_id: str
    issuer_key_id: str
    subject_key_id: str
    digest: str
    size: int
    source: str
    operation: str
    grant_revision: int
    policy_revision: int
    issued_at: int
    expires_at: int
    signature: bytes
    single_use: bool = True
    signature_algorithm: str = _SIGNATURE_ALGORITHM

    def __post_init__(self) -> None:
        _require_id(self.message_id)
        _require_id(self.file_grant_id)
        _require_key_id(self.issuer_key_id)
        _require_key_id(self.subject_key_id)
        _require_digest(self.digest)
        operation = _catalog_operation(self.source, self.operation)
        if operation.tool != "transcribe":
            raise ProtocolValidationError("File grants are transcription-only.")
        _require_positive_int(self.grant_revision, "grant revision")
        _require_positive_int(self.policy_revision, "policy revision")
        _require_timestamp(self.issued_at)
        _require_timestamp(self.expires_at)
        if (
            type(self.size) is not int
            or not 0 < self.size <= MAX_FILE_BYTES
            or self.issuer_key_id == self.subject_key_id
            or self.single_use is not True
            or self.signature_algorithm != _SIGNATURE_ALGORITHM
            or not self.issued_at < self.expires_at
            or self.expires_at - self.issued_at > MAX_FILE_GRANT_TTL_SECONDS
        ):
            raise ProtocolValidationError("The file grant bounds are invalid.")
        _require_signature(self.signature)


@dataclass(frozen=True, slots=True)
class ErrorFrame:
    message_id: str
    code: ConnectorErrorCode

    def __post_init__(self) -> None:
        _require_id(self.message_id)
        if not isinstance(self.code, ConnectorErrorCode):
            raise ProtocolValidationError("The Connector error frame is invalid.")


@dataclass(frozen=True, slots=True)
class OperationResultMediaV1:
    coverage: str
    duration_seconds: int | None = None
    view_count: int | None = None
    comment_count: int | None = None
    subtitle_language: str | None = None
    subtitle_origin: str | None = None
    version: str = NORMALIZED_MEDIA_VERSION

    def __post_init__(self) -> None:
        if (
            self.version != NORMALIZED_MEDIA_VERSION
            or self.coverage not in _MEDIA_COVERAGE
            or (
                self.subtitle_language is not None
                and (
                    type(self.subtitle_language) is not str
                    or _LANGUAGE_TAG.fullmatch(self.subtitle_language) is None
                )
            )
            or (
                self.subtitle_origin is not None
                and self.subtitle_origin not in _SUBTITLE_ORIGINS
            )
        ):
            raise ProtocolValidationError("The operation result media is invalid.")
        for value in (self.duration_seconds, self.view_count, self.comment_count):
            if value is not None and (
                type(value) is not int or not 0 <= value <= MAX_NORMALIZED_INTEGER
            ):
                raise ProtocolValidationError(
                    "The operation result media count is invalid."
                )

    def character_count(self) -> int:
        return media_metadata_characters(
            coverage=self.coverage,
            duration_seconds=self.duration_seconds,
            view_count=self.view_count,
            comment_count=self.comment_count,
            subtitle_language=self.subtitle_language,
            subtitle_origin=self.subtitle_origin,
        )


@dataclass(frozen=True, slots=True)
class OperationResultItemV1:
    kind: str
    text: str
    native_id: str | None = None
    title: str | None = None
    url: str | None = None
    author: str | None = None
    published_at: str | None = None
    media: OperationResultMediaV1 | None = None

    def __post_init__(self) -> None:
        if self.kind not in _ITEM_KINDS:
            raise ProtocolValidationError("The operation result item kind is invalid.")
        _require_result_string(self.text, maximum=None, nullable=False)
        _require_result_string(self.native_id, maximum=512, nullable=True)
        _require_result_string(self.title, maximum=512, nullable=True)
        _require_result_string(self.author, maximum=256, nullable=True)
        _require_result_string(self.published_at, maximum=128, nullable=True)
        _require_result_url(self.url)
        if self.media is not None and not isinstance(
            self.media, OperationResultMediaV1
        ):
            raise ProtocolValidationError("The operation result media is invalid.")

    def character_count(self) -> int:
        return normalized_item_characters(
            kind=self.kind,
            text=self.text,
            native_id=self.native_id,
            title=self.title,
            url=self.url,
            author=self.author,
            published_at=self.published_at,
            media_characters=(
                0 if self.media is None else self.media.character_count()
            ),
        )


@dataclass(frozen=True, slots=True)
class OperationResultV1:
    items: tuple[OperationResultItemV1, ...]
    truncated: bool
    version: str = "v1"

    def __post_init__(self) -> None:
        if (
            self.version != "v1"
            or type(self.items) is not tuple
            or len(self.items) > MAX_JSON_CONTAINER_ITEMS
            or not all(isinstance(item, OperationResultItemV1) for item in self.items)
            or type(self.truncated) is not bool
        ):
            raise ProtocolValidationError("The operation result is invalid.")

    def character_count(self) -> int:
        return sum(item.character_count() for item in self.items)


class ProtectedOperationPayload:
    """Canonical operation bytes whose representation never exposes request data."""

    __slots__ = ("_call", "_canonical")

    def __init__(self, call: OperationCall, canonical: bytes) -> None:
        self._call = call
        self._canonical = canonical

    def transport_bytes(self) -> bytes:
        """Return the protected bytes only for the authenticated transport body."""

        return self._canonical

    def to_operation_call(self) -> OperationCall:
        """Return the revalidated call for the trusted Connector execution path."""

        return self._call

    def __repr__(self) -> str:
        return "ProtectedOperationPayload(<redacted>)"

    __str__ = __repr__


@dataclass(frozen=True, slots=True)
class OperationInvocationV1:
    message_id: str
    signed_request: SignedRequest
    protected_payload: ProtectedOperationPayload

    def __post_init__(self) -> None:
        _require_id(self.message_id)
        if not isinstance(self.signed_request, SignedRequest) or not isinstance(
            self.protected_payload, ProtectedOperationPayload
        ):
            raise ProtocolValidationError("The operation invocation is invalid.")
        call = self.protected_payload.to_operation_call()
        if (
            self.message_id != self.signed_request.message_id
            or len(self.protected_payload.transport_bytes())
            > MAX_PROTECTED_OPERATION_BYTES
            or self.signed_request.payload_digest
            != operation_payload_digest(self.protected_payload)
            or self.signed_request.source != call.source.name
            or self.signed_request.operation != call.operation.name
        ):
            raise ProtocolValidationError(
                "The operation invocation context does not match."
            )


@dataclass(frozen=True, slots=True)
class OperationResponseV1:
    message_id: str
    receipt: SignedReceipt
    result: OperationResultV1 | None

    def __post_init__(self) -> None:
        _require_id(self.message_id)
        if not isinstance(self.receipt, SignedReceipt) or (
            self.result is not None and not isinstance(self.result, OperationResultV1)
        ):
            raise ProtocolValidationError("The operation response is invalid.")
        if self.message_id != self.receipt.message_id:
            raise ProtocolValidationError(
                "The operation response context does not match."
            )
        _validate_response_result(self.receipt, self.result)


SignedRecord: TypeAlias = (
    PairingInit
    | PairingChallenge
    | PairingComplete
    | SignedGrant
    | SignedRequest
    | SignedReceipt
    | FileGrant
)
WireRecord: TypeAlias = (
    SignedRecord
    | PairingResolution
    | OperationInvocationV1
    | OperationResponseV1
    | ErrorFrame
)


def _catalog_operation(source_name: object, operation_name: object) -> OperationSpec:
    if type(source_name) is not str or type(operation_name) is not str:
        raise ProtocolValidationError("The source-operation is invalid.")
    source = get_source(source_name)
    operation = get_operation(source, operation_name) if source is not None else None
    if operation is None or operation.tool == "status":
        raise ProtocolValidationError("The source-operation is not in the catalog.")
    return operation


def _require_timestamp(value: object) -> None:
    if (
        type(value) is not int
        or not MIN_TIMESTAMP_SECONDS <= value <= MAX_TIMESTAMP_SECONDS
    ):
        raise ProtocolValidationError("A Connector timestamp is invalid.")


def _require_positive_int(value: object, name: str) -> None:
    del name
    if type(value) is not int or value <= 0:
        raise ProtocolValidationError("A positive Connector counter is invalid.")


def require_printable_metadata(value: object, maximum: int) -> None:
    """Require bounded printable ASCII before metadata reaches a terminal."""

    if (
        type(value) is not str
        or not 0 < len(value) <= maximum
        or value.strip() != value
        or not value.isascii()
        or any(ord(character) < 0x20 or ord(character) > 0x7E for character in value)
    ):
        raise ProtocolValidationError("Printable Connector metadata is invalid.")


def _decode_base32(value: str, expected_bytes: int) -> bytes | None:
    padding = "=" * (-len(value) % 8)
    try:
        decoded = base64.b32decode(value.upper() + padding, casefold=False)
    except (ValueError, binascii.Error):
        return None
    canonical = base64.b32encode(decoded).decode("ascii").rstrip("=").lower()
    return decoded if len(decoded) == expected_bytes and canonical == value else None


def _is_canonical_id(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == ID_BASE32_LENGTH
        and _decode_base32(value, 16) is not None
    )


def _require_id(value: object) -> None:
    if not _is_canonical_id(value):
        raise ProtocolValidationError("A Connector identifier is invalid.")


def _require_key_id(value: object) -> None:
    if (
        type(value) is not str
        or len(value) != KEY_ID_BASE32_LENGTH
        or _decode_base32(value, 20) is None
    ):
        raise ProtocolValidationError("A Connector key identifier is invalid.")


def _encode_base64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_base64url(value: object, expected_bytes: int) -> bytes:
    if type(value) is not str or "=" in value:
        raise ProtocolValidationError("A Connector binary encoding is invalid.")
    try:
        decoded = base64.b64decode(
            value + "=" * (-len(value) % 4), altchars=b"-_", validate=True
        )
    except (ValueError, binascii.Error):
        raise ProtocolValidationError(
            "A Connector binary encoding is invalid."
        ) from None
    if len(decoded) != expected_bytes or _encode_base64url(decoded) != value:
        raise ProtocolValidationError("A Connector binary encoding is non-canonical.")
    return decoded


def _decode_bounded_base64url(value: object, *, maximum: int) -> bytes:
    if type(value) is not str or not value or "=" in value:
        raise ProtocolValidationError("A Connector binary encoding is invalid.")
    try:
        decoded = base64.b64decode(
            value + "=" * (-len(value) % 4), altchars=b"-_", validate=True
        )
    except (ValueError, binascii.Error):
        raise ProtocolValidationError(
            "A Connector binary encoding is invalid."
        ) from None
    if not 0 < len(decoded) <= maximum or _encode_base64url(decoded) != value:
        raise ProtocolValidationError("A Connector binary encoding is non-canonical.")
    return decoded


def _require_public_key(value: object) -> DevicePublicIdentity:
    if type(value) is not str:
        raise ProtocolValidationError("The Connector public key is invalid.")
    try:
        return DevicePublicIdentity.from_wire(value)
    except ValueError:
        raise ProtocolValidationError("The Connector public key is invalid.") from None


def _require_nonce(value: object) -> None:
    _decode_base64url(value, DEVICE_NONCE_BYTES)


def _require_digest(value: object) -> None:
    if type(value) is not str or _HEX_64.fullmatch(value) is None:
        raise ProtocolValidationError("A Connector digest is invalid.")


def _require_result_string(
    value: object, *, maximum: int | None, nullable: bool
) -> None:
    if value is None and nullable:
        return
    if type(value) is not str or (maximum is not None and len(value) > maximum):
        raise ProtocolValidationError("An operation result string is invalid.")
    _validate_json_value(value)


def _require_result_url(value: object) -> None:
    if value is None:
        return
    _require_result_string(value, maximum=4096, nullable=False)
    try:
        parsed = urlsplit(cast(str, value))
        port = parsed.port
    except ValueError:
        raise ProtocolValidationError("The operation result URL is invalid.") from None
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or (port is not None and not 1 <= port <= 65535)
    ):
        raise ProtocolValidationError("The operation result URL is invalid.")


def _require_signature(value: object) -> None:
    if type(value) is not bytes or len(value) != 64:
        raise ProtocolValidationError("The Connector signature is invalid.")


def _scope_key(scope: GrantScope) -> tuple[str, str]:
    return scope.source, scope.operation


def _require_scopes(scopes: object) -> None:
    if (
        type(scopes) is not tuple
        or not 1 <= len(scopes) <= MAX_GRANT_SCOPES
        or not all(isinstance(scope, GrantScope) for scope in scopes)
    ):
        raise ProtocolValidationError("The Connector grant scopes are invalid.")
    typed_scopes = cast(tuple[GrantScope, ...], scopes)
    keys = tuple(_scope_key(scope) for scope in typed_scopes)
    if keys != tuple(sorted(keys)) or len(set(keys)) != len(keys):
        raise ProtocolValidationError(
            "Connector grant scopes must be sorted and unique."
        )


def _scope_mapping(scope: GrantScope) -> dict[str, JSONValue]:
    return {
        "capability_id": scope.capability_id,
        "data_scope": scope.data_scope,
        "operation": scope.operation,
        "source": scope.source,
    }


def _grant_claims_mapping(claims: GrantClaims) -> dict[str, JSONValue]:
    return {
        "expires_at": claims.expires_at,
        "grant_id": claims.grant_id,
        "issued_at": claims.issued_at,
        "issuer_key_id": claims.issuer_key_id,
        "max_uses": claims.max_uses,
        "not_before": claims.not_before,
        "policy_revision": claims.policy_revision,
        "revision": claims.revision,
        "scopes": [_scope_mapping(scope) for scope in claims.scopes],
        "signature_algorithm": claims.signature_algorithm,
        "subject_key_id": claims.subject_key_id,
    }


def _result_media_mapping(media: OperationResultMediaV1) -> dict[str, JSONValue]:
    return {
        "comment_count": media.comment_count,
        "coverage": media.coverage,
        "duration_seconds": media.duration_seconds,
        "subtitle_language": media.subtitle_language,
        "subtitle_origin": media.subtitle_origin,
        "version": media.version,
        "view_count": media.view_count,
    }


def _result_item_mapping(item: OperationResultItemV1) -> dict[str, JSONValue]:
    return {
        "author": item.author,
        "kind": item.kind,
        "media": None if item.media is None else _result_media_mapping(item.media),
        "native_id": item.native_id,
        "published_at": item.published_at,
        "text": item.text,
        "title": item.title,
        "url": item.url,
    }


def operation_result_mapping(result: OperationResultV1) -> dict[str, JSONValue]:
    """Return the complete nullable v1 result projection used on the wire."""

    if not isinstance(result, OperationResultV1):
        raise TypeError("Operation result mapping requires a v1 result.")
    return {
        "items": [_result_item_mapping(item) for item in result.items],
        "truncated": result.truncated,
        "version": result.version,
    }


def canonical_operation_result_bytes(result: OperationResultV1) -> bytes:
    """Encode one result within its stricter response-body budget."""

    encoded = canonical_json_bytes(operation_result_mapping(result))
    if len(encoded) > MAX_OPERATION_RESULT_BYTES:
        raise ProtocolValidationError("The operation result exceeds its byte limit.")
    return encoded


def operation_result_digest(result: OperationResultV1) -> str:
    """Bind canonical result bytes into the signed receipt."""

    return hashlib.sha256(
        _OPERATION_RESULT_DOMAIN + canonical_operation_result_bytes(result)
    ).hexdigest()


def _validate_response_result(
    receipt: SignedReceipt, result: OperationResultV1 | None
) -> None:
    if receipt.outcome == "error":
        if result is not None or receipt.result_digest is not None:
            raise ProtocolValidationError("A failed response cannot carry a result.")
        return
    if result is None or receipt.result_digest is None:
        raise ProtocolValidationError("A successful response requires a result.")
    operation = _catalog_operation(receipt.source, receipt.operation)
    if (
        len(result.items) > operation.runtime.maximum_items
        or result.character_count() > operation.runtime.maximum_characters
        or receipt.result_count != len(result.items)
        or receipt.truncated is not result.truncated
        or receipt.result_digest != operation_result_digest(result)
    ):
        raise ProtocolValidationError(
            "The operation result does not match its signed receipt."
        )


def _claims_mapping(record: SignedRecord) -> dict[str, JSONValue]:
    if isinstance(record, PairingInit):
        return {
            "deadline": record.deadline,
            "device_label": record.device_label,
            "endpoint_digest": record.endpoint_digest,
            "grant_expires_at": record.grant_expires_at,
            "grant_max_uses": record.grant_max_uses,
            "issued_at": record.issued_at,
            "pairing_id": record.pairing_id,
            "requested_scopes": [
                _scope_mapping(scope) for scope in record.requested_scopes
            ],
            "signature_algorithm": record.signature_algorithm,
            "vps_key_id": record.vps_key_id,
            "vps_nonce": record.vps_nonce,
            "vps_public_key": record.vps_public_key,
        }
    if isinstance(record, PairingChallenge):
        return {
            "connector_key_id": record.connector_key_id,
            "connector_nonce": record.connector_nonce,
            "connector_public_key": record.connector_public_key,
            "deadline": record.deadline,
            "init_digest": record.init_digest,
            "issued_at": record.issued_at,
            "pairing_id": record.pairing_id,
            "signature_algorithm": record.signature_algorithm,
            "tls_ca_der": record.tls_ca_der,
            "tls_ca_fingerprint": record.tls_ca_fingerprint,
            "tls_leaf_fingerprint": record.tls_leaf_fingerprint,
            "vps_key_id": record.vps_key_id,
        }
    if isinstance(record, PairingComplete):
        return {
            "completed_at": record.completed_at,
            "connector_key_id": record.connector_key_id,
            "pairing_id": record.pairing_id,
            "signature_algorithm": record.signature_algorithm,
            "signed_grant_digest": record.signed_grant_digest,
            "transcript_digest": record.transcript_digest,
            "vps_key_id": record.vps_key_id,
        }
    if isinstance(record, SignedGrant):
        return _grant_claims_mapping(record.claims)
    if isinstance(record, SignedRequest):
        return {
            "audience_key_id": record.audience_key_id,
            "deadline": record.deadline,
            "grant_id": record.grant_id,
            "grant_revision": record.grant_revision,
            "issued_at": record.issued_at,
            "operation": record.operation,
            "payload_digest": record.payload_digest,
            "payload_digest_algorithm": record.payload_digest_algorithm,
            "policy_revision": record.policy_revision,
            "request_id": record.request_id,
            "signature_algorithm": record.signature_algorithm,
            "source": record.source,
            "subject_key_id": record.subject_key_id,
            "trace_id": record.trace_id,
        }
    if isinstance(record, SignedReceipt):
        failure: JSONValue = None
        if record.failure is not None:
            failure = {
                "cause_code": record.failure.cause_code.value,
                "failure_class": record.failure.failure_class,
            }
        usage: JSONValue = None
        if record.usage is not None:
            usage = {
                "remaining": record.usage.remaining,
                "sequence": record.usage.sequence,
            }
        backend: JSONValue = None
        if record.backend is not None:
            backend = {
                "backend_id": record.backend.backend_id,
                "backend_version": record.backend.backend_version,
            }
        return {
            "backend": backend,
            "decision": record.decision,
            "ended_at": record.ended_at,
            "expires_at": record.expires_at,
            "failure": failure,
            "grant_id": record.grant_id,
            "grant_revision": record.grant_revision,
            "issuer_key_id": record.issuer_key_id,
            "operation": record.operation,
            "outcome": record.outcome,
            "payload_digest": record.payload_digest,
            "payload_digest_algorithm": record.payload_digest_algorithm,
            "policy_revision": record.policy_revision,
            "receipt_id": record.receipt_id,
            "request_id": record.request_id,
            "result_count": record.result_count,
            "result_digest": record.result_digest,
            "result_digest_algorithm": record.result_digest_algorithm,
            "signature_algorithm": record.signature_algorithm,
            "source": record.source,
            "started_at": record.started_at,
            "subject_key_id": record.subject_key_id,
            "trace_id": record.trace_id,
            "truncated": record.truncated,
            "usage": usage,
        }
    if isinstance(record, FileGrant):
        return {
            "digest": record.digest,
            "expires_at": record.expires_at,
            "file_grant_id": record.file_grant_id,
            "grant_revision": record.grant_revision,
            "issued_at": record.issued_at,
            "issuer_key_id": record.issuer_key_id,
            "operation": record.operation,
            "policy_revision": record.policy_revision,
            "signature_algorithm": record.signature_algorithm,
            "single_use": record.single_use,
            "size": record.size,
            "source": record.source,
            "subject_key_id": record.subject_key_id,
        }
    raise TypeError("Unsupported Connector record type.")


def _record_type(record: WireRecord) -> str:
    if isinstance(record, PairingInit):
        return "pairing-init"
    if isinstance(record, PairingChallenge):
        return "pairing-challenge"
    if isinstance(record, PairingComplete):
        return "pairing-complete"
    if isinstance(record, SignedGrant):
        return "signed-grant"
    if isinstance(record, PairingResolution):
        return "pairing-resolution"
    if isinstance(record, SignedRequest):
        return "operation-request"
    if isinstance(record, SignedReceipt):
        return "operation-receipt"
    if isinstance(record, OperationInvocationV1):
        return "operation-invocation"
    if isinstance(record, OperationResponseV1):
        return "operation-response"
    if isinstance(record, FileGrant):
        return "file-grant"
    if isinstance(record, ErrorFrame):
        return "error"
    raise TypeError("Unsupported Connector record type.")


def _signature_domain(record: SignedRecord) -> SignatureDomain:
    if isinstance(record, PairingInit):
        return SignatureDomain.PAIRING_INIT
    if isinstance(record, PairingChallenge):
        return SignatureDomain.PAIRING_CHALLENGE
    if isinstance(record, PairingComplete):
        return SignatureDomain.PAIRING_COMPLETE
    if isinstance(record, SignedGrant):
        return SignatureDomain.GRANT
    if isinstance(record, SignedRequest):
        return SignatureDomain.REQUEST
    if isinstance(record, SignedReceipt):
        return SignatureDomain.RECEIPT
    if isinstance(record, FileGrant):
        return SignatureDomain.FILE_GRANT
    raise TypeError("Unsupported signed Connector record type.")


def _unsigned_frame(record: SignedRecord) -> dict[str, JSONValue]:
    return {
        "body": {"claims": _claims_mapping(record)},
        "message_id": record.message_id,
        "protocol": CONNECTOR_PROTOCOL_VERSION,
        "type": _record_type(record),
    }


def record_signing_bytes(record: SignedRecord) -> bytes:
    """Return the canonical outer frame with only the signature omitted."""

    return canonical_json_bytes(_unsigned_frame(record))


def encode_record(record: WireRecord) -> bytes:
    """Encode one closed v1 record as canonical JSON."""

    if isinstance(record, ErrorFrame):
        frame: dict[str, JSONValue] = {
            "body": {"code": record.code.value},
            "message_id": record.message_id,
            "protocol": CONNECTOR_PROTOCOL_VERSION,
            "type": _record_type(record),
        }
    elif isinstance(record, PairingResolution):
        frame = {
            "body": {
                "pairing_complete": _encode_base64url(
                    encode_record(record.pairing_complete)
                ),
                "signed_grant": _encode_base64url(encode_record(record.signed_grant)),
            },
            "message_id": record.message_id,
            "protocol": CONNECTOR_PROTOCOL_VERSION,
            "type": _record_type(record),
        }
    elif isinstance(record, OperationInvocationV1):
        protected_mapping = _mapping(
            load_canonical_json(
                record.protected_payload.transport_bytes(),
                max_bytes=MAX_PROTECTED_OPERATION_BYTES,
            )
        )
        frame = {
            "body": {
                "protected_payload": cast(dict[str, JSONValue], protected_mapping),
                "signed_request": _encode_base64url(
                    encode_record(record.signed_request)
                ),
            },
            "message_id": record.message_id,
            "protocol": CONNECTOR_PROTOCOL_VERSION,
            "type": _record_type(record),
        }
    elif isinstance(record, OperationResponseV1):
        frame = {
            "body": {
                "receipt": _encode_base64url(encode_record(record.receipt)),
                "result": (
                    None
                    if record.result is None
                    else operation_result_mapping(record.result)
                ),
            },
            "message_id": record.message_id,
            "protocol": CONNECTOR_PROTOCOL_VERSION,
            "type": _record_type(record),
        }
    else:
        frame = _unsigned_frame(record)
        body = cast(dict[str, JSONValue], frame["body"])
        body["signature"] = _encode_base64url(record.signature)
    return canonical_json_bytes(frame)


def _sign(record: SignedRecord, signer: DevicePrivateIdentity) -> SignedRecord:
    if not isinstance(signer, DevicePrivateIdentity):
        raise TypeError("Connector records require a device signer.")
    signature = signer.sign(_signature_domain(record), record_signing_bytes(record))
    return replace(record, signature=signature)


def record_digest(record: WireRecord) -> str:
    """Return the domain-separated digest used to bind complete signed records."""

    return hashlib.sha256(_RECORD_DIGEST_DOMAIN + encode_record(record)).hexdigest()


def create_pairing_init(
    signer: DevicePrivateIdentity,
    *,
    message_id: str,
    pairing_id: str,
    device_label: str,
    vps_nonce: bytes,
    endpoint_digest: str,
    requested_scopes: tuple[GrantScope, ...],
    grant_expires_at: int,
    grant_max_uses: int,
    issued_at: int,
    deadline: int,
) -> PairingInit:
    public = signer.public_identity
    unsigned = PairingInit(
        message_id=message_id,
        pairing_id=pairing_id,
        vps_public_key=public.wire_public_key,
        vps_key_id=public.key_id,
        device_label=device_label,
        vps_nonce=_encode_nonce(vps_nonce),
        endpoint_digest=endpoint_digest,
        requested_scopes=requested_scopes,
        grant_expires_at=grant_expires_at,
        grant_max_uses=grant_max_uses,
        issued_at=issued_at,
        deadline=deadline,
        signature=b"\x00" * 64,
    )
    return cast(PairingInit, _sign(unsigned, signer))


def create_pairing_resolution(
    *,
    message_id: str,
    pairing_id: str,
    signed_grant: SignedGrant,
    pairing_complete: PairingComplete,
) -> PairingResolution:
    """Wrap paired-device grant records without creating a third signature."""

    return PairingResolution(
        message_id=message_id,
        pairing_id=pairing_id,
        signed_grant=signed_grant,
        pairing_complete=pairing_complete,
    )


def create_pairing_challenge(
    signer: DevicePrivateIdentity,
    *,
    message_id: str,
    pairing_id: str,
    init_digest: str,
    vps_key_id: str,
    connector_nonce: bytes,
    tls_ca_der: bytes,
    tls_leaf_fingerprint: str,
    issued_at: int,
    deadline: int,
) -> PairingChallenge:
    public = signer.public_identity
    if type(tls_ca_der) is not bytes:
        raise TypeError("The pairing CA certificate must be DER bytes.")
    unsigned = PairingChallenge(
        message_id=message_id,
        pairing_id=pairing_id,
        init_digest=init_digest,
        vps_key_id=vps_key_id,
        connector_public_key=public.wire_public_key,
        connector_key_id=public.key_id,
        connector_nonce=_encode_nonce(connector_nonce),
        tls_ca_der=_encode_base64url(tls_ca_der),
        tls_ca_fingerprint=hashlib.sha256(tls_ca_der).hexdigest(),
        tls_leaf_fingerprint=tls_leaf_fingerprint,
        issued_at=issued_at,
        deadline=deadline,
        signature=b"\x00" * 64,
    )
    return cast(PairingChallenge, _sign(unsigned, signer))


def create_pairing_complete(
    signer: DevicePrivateIdentity,
    *,
    message_id: str,
    pairing_id: str,
    transcript_digest: str,
    vps_key_id: str,
    signed_grant_digest: str,
    completed_at: int,
) -> PairingComplete:
    unsigned = PairingComplete(
        message_id=message_id,
        pairing_id=pairing_id,
        transcript_digest=transcript_digest,
        connector_key_id=signer.public_identity.key_id,
        vps_key_id=vps_key_id,
        signed_grant_digest=signed_grant_digest,
        completed_at=completed_at,
        signature=b"\x00" * 64,
    )
    return cast(PairingComplete, _sign(unsigned, signer))


def create_signed_grant(
    signer: DevicePrivateIdentity, *, message_id: str, claims: GrantClaims
) -> SignedGrant:
    if signer.public_identity.key_id != claims.issuer_key_id:
        raise ProtocolValidationError("The grant issuer does not match the signer.")
    unsigned = SignedGrant(message_id, claims, b"\x00" * 64)
    return cast(SignedGrant, _sign(unsigned, signer))


def create_signed_request(
    signer: DevicePrivateIdentity,
    *,
    message_id: str,
    request_id: str,
    trace_id: str,
    audience_key_id: str,
    grant_id: str,
    grant_revision: int,
    policy_revision: int,
    source: str,
    operation: str,
    issued_at: int,
    deadline: int,
    protected_payload: ProtectedOperationPayload,
) -> SignedRequest:
    if not isinstance(protected_payload, ProtectedOperationPayload):
        raise TypeError("A signed request requires a protected operation payload.")
    unsigned = SignedRequest(
        message_id=message_id,
        request_id=request_id,
        trace_id=trace_id,
        audience_key_id=audience_key_id,
        subject_key_id=signer.public_identity.key_id,
        grant_id=grant_id,
        grant_revision=grant_revision,
        policy_revision=policy_revision,
        source=source,
        operation=operation,
        issued_at=issued_at,
        deadline=deadline,
        payload_digest=operation_payload_digest(protected_payload),
        signature=b"\x00" * 64,
    )
    call = protected_payload.to_operation_call()
    if call.source.name != source or call.operation.name != operation:
        raise ProtocolValidationError("The protected operation context is invalid.")
    return cast(SignedRequest, _sign(unsigned, signer))


def create_signed_receipt(
    signer: DevicePrivateIdentity,
    *,
    message_id: str,
    receipt_id: str,
    request: SignedRequest,
    decision: str,
    failure: ReceiptFailure | None,
    usage: ReceiptUsage | None,
    backend: PublicBackendIdentity | None,
    started_at: int,
    ended_at: int,
    expires_at: int,
    result: OperationResultV1 | None,
    outcome: str,
) -> SignedReceipt:
    if not isinstance(request, SignedRequest):
        raise TypeError("A receipt requires a signed operation request.")
    if request.audience_key_id != signer.public_identity.key_id:
        raise ProtocolValidationError(
            "The receipt signer does not match the request audience."
        )
    if result is not None and not isinstance(result, OperationResultV1):
        raise TypeError("A receipt result must use the v1 result contract.")
    unsigned = SignedReceipt(
        message_id=message_id,
        receipt_id=receipt_id,
        request_id=request.request_id,
        trace_id=request.trace_id,
        issuer_key_id=signer.public_identity.key_id,
        subject_key_id=request.subject_key_id,
        grant_id=request.grant_id,
        grant_revision=request.grant_revision,
        policy_revision=request.policy_revision,
        source=request.source,
        operation=request.operation,
        decision=decision,
        failure=failure,
        usage=usage,
        backend=backend,
        started_at=started_at,
        ended_at=ended_at,
        expires_at=expires_at,
        result_count=0 if result is None else len(result.items),
        truncated=False if result is None else result.truncated,
        result_digest=None if result is None else operation_result_digest(result),
        outcome=outcome,
        payload_digest=request.payload_digest,
        signature=b"\x00" * 64,
    )
    return cast(SignedReceipt, _sign(unsigned, signer))


def create_file_grant(
    signer: DevicePrivateIdentity,
    *,
    message_id: str,
    file_grant_id: str,
    subject_key_id: str,
    digest: str,
    size: int,
    source: str,
    operation: str,
    grant_revision: int,
    policy_revision: int,
    issued_at: int,
    expires_at: int | None = None,
) -> FileGrant:
    effective_expiry = (
        issued_at + DEFAULT_FILE_GRANT_TTL_SECONDS if expires_at is None else expires_at
    )
    unsigned = FileGrant(
        message_id=message_id,
        file_grant_id=file_grant_id,
        issuer_key_id=signer.public_identity.key_id,
        subject_key_id=subject_key_id,
        digest=digest,
        size=size,
        source=source,
        operation=operation,
        grant_revision=grant_revision,
        policy_revision=policy_revision,
        issued_at=issued_at,
        expires_at=effective_expiry,
        signature=b"\x00" * 64,
    )
    return cast(FileGrant, _sign(unsigned, signer))


def _encode_nonce(value: bytes) -> str:
    if type(value) is not bytes or len(value) != DEVICE_NONCE_BYTES:
        raise ProtocolValidationError("The pairing nonce is invalid.")
    return _encode_base64url(value)


def protect_operation_call(call: OperationCall) -> ProtectedOperationPayload:
    """Project a validated call into the only protected request payload schema."""

    if not isinstance(call, OperationCall) or not operation_call_is_valid(call):
        raise ProtocolValidationError("The protected operation call is invalid.")
    source = get_source(call.source.name)
    operation = (
        get_operation(source, call.operation.name) if source is not None else None
    )
    if source is not call.source or operation is not call.operation:
        raise ProtocolValidationError("The operation call is not catalog-owned.")
    target: dict[str, JSONValue] | None = None
    if call.target is not None:
        if "local_file" in call.target:
            raise ProtocolValidationError("Connector-local files require a file grant.")
        target = {key: value for key, value in call.target.items()}
    options: dict[str, JSONValue] = {}
    for key, value in call.options.items():
        if type(key) is not str or type(value) not in {bool, int, str}:
            raise ProtocolValidationError(
                "The protected operation options are invalid."
            )
        options[key] = cast(JSONScalar, value)
    mapping: dict[str, JSONValue] = {
        "operation": call.operation.name,
        "options": options,
        "query": call.query,
        "source": call.source.name,
        "target": target,
    }
    return ProtectedOperationPayload(call, canonical_json_bytes(mapping))


def parse_protected_operation(raw: bytes) -> ProtectedOperationPayload:
    """Parse and revalidate the canonical protected operation payload."""

    mapping = _mapping(load_canonical_json(raw))
    _closed(mapping, {"operation", "options", "query", "source", "target"})
    source_name = _string(mapping, "source")
    operation_name = _string(mapping, "operation")
    source = get_source(source_name)
    operation = get_operation(source, operation_name) if source is not None else None
    if source is None or operation is None:
        raise ProtocolValidationError("The protected source-operation is invalid.")
    raw_options = _mapping(mapping.get("options"))
    options: dict[str, object] = dict(raw_options)
    raw_query = mapping.get("query")
    query = None if raw_query is None else _exact_string(raw_query)
    raw_target = mapping.get("target")
    target: dict[str, str] | None = None
    if raw_target is not None:
        target_mapping = _mapping(raw_target)
        if "local_file" in target_mapping:
            raise ProtocolValidationError("Connector-local files require a file grant.")
        target = {key: _exact_string(value) for key, value in target_mapping.items()}
    call = OperationCall(
        source,
        operation,
        MappingProxyType(options),
        target=None if target is None else MappingProxyType(target),
        query=query,
    )
    if not operation_call_is_valid(call):
        raise ProtocolValidationError("The protected operation call is invalid.")
    return ProtectedOperationPayload(call, raw)


def operation_payload_digest(payload: ProtectedOperationPayload) -> str:
    """Digest protected operation bytes without exposing their content."""

    if not isinstance(payload, ProtectedOperationPayload):
        raise TypeError("Operation digests require a protected payload.")
    return hashlib.sha256(
        _OPERATION_PAYLOAD_DOMAIN + payload.transport_bytes()
    ).hexdigest()


def pairing_ca_der(challenge: PairingChallenge) -> bytes:
    """Return the bounded canonical CA DER protected by a signed challenge."""

    if not isinstance(challenge, PairingChallenge):
        raise ProtocolValidationError("The pairing challenge type is invalid.")
    return _decode_bounded_base64url(challenge.tls_ca_der, maximum=MAX_TLS_CA_DER_BYTES)


def pairing_transcript_hash(
    init_bytes: bytes,
    challenge_bytes: bytes,
    *,
    observed_tls_leaf_fingerprint: str,
) -> bytes:
    """Hash pairing records plus the leaf independently observed by this peer."""

    init = parse_record(init_bytes)
    challenge = parse_record(challenge_bytes)
    if not isinstance(init, PairingInit) or not isinstance(challenge, PairingChallenge):
        raise ProtocolValidationError(
            "The pairing transcript record types are invalid."
        )
    if len(init_bytes) > 0xFFFFFFFF or len(challenge_bytes) > 0xFFFFFFFF:
        raise ProtocolValidationError("The pairing transcript is oversized.")
    _require_digest(observed_tls_leaf_fingerprint)
    channel_binding = bytes.fromhex(observed_tls_leaf_fingerprint)
    payload = (
        _TRANSCRIPT_DOMAIN
        + len(init_bytes).to_bytes(4, "big")
        + init_bytes
        + len(challenge_bytes).to_bytes(4, "big")
        + challenge_bytes
        + len(channel_binding).to_bytes(4, "big")
        + channel_binding
    )
    return hashlib.sha256(payload).digest()


def pairing_sas(transcript_hash: bytes) -> str:
    """Format the first 50 transcript bits as two Crockford-base32 groups."""

    if type(transcript_hash) is not bytes or len(transcript_hash) != 32:
        raise ProtocolValidationError("The pairing transcript digest is invalid.")
    value = int.from_bytes(transcript_hash[:7], "big") >> 6
    characters = ["0"] * PAIRING_SAS_LENGTH
    for index in range(PAIRING_SAS_LENGTH - 1, -1, -1):
        characters[index] = _CROCKFORD_ALPHABET[value & 31]
        value >>= 5
    compact = "".join(characters)
    return f"{compact[:5]}-{compact[5:]}"


def _mapping(value: object) -> dict[str, object]:
    if type(value) is not dict:
        raise ProtocolValidationError("A Connector object is invalid.")
    mapping = cast(dict[object, object], value)
    if not all(type(key) is str for key in mapping):
        raise ProtocolValidationError("A Connector object key is invalid.")
    return cast(dict[str, object], mapping)


def _closed(mapping: Mapping[str, object], fields: set[str]) -> None:
    if set(mapping) != fields:
        raise ProtocolValidationError("The Connector record schema is invalid.")


def _exact_string(value: object) -> str:
    if type(value) is not str:
        raise ProtocolValidationError("A Connector string field is invalid.")
    return value


def _string(mapping: Mapping[str, object], field: str) -> str:
    return _exact_string(mapping.get(field))


def _integer(mapping: Mapping[str, object], field: str) -> int:
    value = mapping.get(field)
    if type(value) is not int:
        raise ProtocolValidationError("A Connector integer field is invalid.")
    return value


def _boolean(mapping: Mapping[str, object], field: str) -> bool:
    value = mapping.get(field)
    if type(value) is not bool:
        raise ProtocolValidationError("A Connector boolean field is invalid.")
    return value


def _parse_scope(value: object) -> GrantScope:
    mapping = _mapping(value)
    _closed(mapping, {"capability_id", "data_scope", "operation", "source"})
    capability = mapping.get("capability_id")
    capability_id = None if capability is None else _exact_string(capability)
    data_scope = _string(mapping, "data_scope")
    if data_scope not in {"public", "account_visible"}:
        raise ProtocolValidationError("The grant data scope is invalid.")
    return GrantScope(
        source=_string(mapping, "source"),
        operation=_string(mapping, "operation"),
        data_scope=cast(DataScope, data_scope),
        capability_id=capability_id,
    )


def _parse_scopes(value: object) -> tuple[GrantScope, ...]:
    if type(value) is not list:
        raise ProtocolValidationError("The Connector grant scopes are invalid.")
    return tuple(_parse_scope(item) for item in cast(list[object], value))


def _parse_grant_claims(mapping: dict[str, object]) -> GrantClaims:
    _closed(
        mapping,
        {
            "expires_at",
            "grant_id",
            "issued_at",
            "issuer_key_id",
            "max_uses",
            "not_before",
            "policy_revision",
            "revision",
            "scopes",
            "signature_algorithm",
            "subject_key_id",
        },
    )
    return GrantClaims(
        grant_id=_string(mapping, "grant_id"),
        revision=_integer(mapping, "revision"),
        issuer_key_id=_string(mapping, "issuer_key_id"),
        subject_key_id=_string(mapping, "subject_key_id"),
        issued_at=_integer(mapping, "issued_at"),
        not_before=_integer(mapping, "not_before"),
        expires_at=_integer(mapping, "expires_at"),
        policy_revision=_integer(mapping, "policy_revision"),
        max_uses=_integer(mapping, "max_uses"),
        scopes=_parse_scopes(mapping.get("scopes")),
        signature_algorithm=_string(mapping, "signature_algorithm"),
    )


def _signed_parts(
    frame: dict[str, object], expected_type: str
) -> tuple[str, dict[str, object], bytes]:
    _closed(frame, {"body", "message_id", "protocol", "type"})
    if (
        _string(frame, "protocol") != CONNECTOR_PROTOCOL_VERSION
        or _string(frame, "type") != expected_type
    ):
        raise ProtocolValidationError(
            "The Connector protocol or record type is invalid."
        )
    message_id = _string(frame, "message_id")
    body = _mapping(frame.get("body"))
    _closed(body, {"claims", "signature"})
    claims = _mapping(body.get("claims"))
    signature = _decode_base64url(body.get("signature"), 64)
    return message_id, claims, signature


def parse_record(raw: bytes) -> WireRecord:
    """Parse one canonical frame into its exact closed record type."""

    frame = _mapping(load_canonical_json(raw))
    record_type = _string(frame, "type")
    if record_type == "error":
        _closed(frame, {"body", "message_id", "protocol", "type"})
        if _string(frame, "protocol") != CONNECTOR_PROTOCOL_VERSION:
            raise ProtocolValidationError("The Connector protocol is invalid.")
        body = _mapping(frame.get("body"))
        _closed(body, {"code"})
        try:
            code = ConnectorErrorCode(_string(body, "code"))
        except ValueError:
            raise ProtocolValidationError(
                "The Connector error code is invalid."
            ) from None
        return ErrorFrame(_string(frame, "message_id"), code)
    parsers: dict[str, Callable[[dict[str, object]], WireRecord]] = {
        "pairing-init": _parse_pairing_init,
        "pairing-challenge": _parse_pairing_challenge,
        "pairing-complete": _parse_pairing_complete,
        "signed-grant": _parse_signed_grant,
        "pairing-resolution": _parse_pairing_resolution,
        "operation-request": _parse_signed_request,
        "operation-receipt": _parse_signed_receipt,
        "operation-invocation": _parse_operation_invocation,
        "operation-response": _parse_operation_response,
        "file-grant": _parse_file_grant,
    }
    parser = parsers.get(record_type)
    if parser is None:
        raise ProtocolValidationError("The Connector record type is unsupported.")
    return parser(frame)


def _parse_pairing_init(frame: dict[str, object]) -> PairingInit:
    message_id, claims, signature = _signed_parts(frame, "pairing-init")
    _closed(
        claims,
        {
            "deadline",
            "device_label",
            "endpoint_digest",
            "grant_expires_at",
            "grant_max_uses",
            "issued_at",
            "pairing_id",
            "requested_scopes",
            "signature_algorithm",
            "vps_key_id",
            "vps_nonce",
            "vps_public_key",
        },
    )
    return PairingInit(
        message_id=message_id,
        pairing_id=_string(claims, "pairing_id"),
        vps_public_key=_string(claims, "vps_public_key"),
        vps_key_id=_string(claims, "vps_key_id"),
        device_label=_string(claims, "device_label"),
        vps_nonce=_string(claims, "vps_nonce"),
        endpoint_digest=_string(claims, "endpoint_digest"),
        requested_scopes=_parse_scopes(claims.get("requested_scopes")),
        grant_expires_at=_integer(claims, "grant_expires_at"),
        grant_max_uses=_integer(claims, "grant_max_uses"),
        issued_at=_integer(claims, "issued_at"),
        deadline=_integer(claims, "deadline"),
        signature=signature,
        signature_algorithm=_string(claims, "signature_algorithm"),
    )


def _parse_pairing_challenge(frame: dict[str, object]) -> PairingChallenge:
    message_id, claims, signature = _signed_parts(frame, "pairing-challenge")
    _closed(
        claims,
        {
            "connector_key_id",
            "connector_nonce",
            "connector_public_key",
            "deadline",
            "init_digest",
            "issued_at",
            "pairing_id",
            "signature_algorithm",
            "tls_ca_der",
            "tls_ca_fingerprint",
            "tls_leaf_fingerprint",
            "vps_key_id",
        },
    )
    return PairingChallenge(
        message_id=message_id,
        pairing_id=_string(claims, "pairing_id"),
        init_digest=_string(claims, "init_digest"),
        vps_key_id=_string(claims, "vps_key_id"),
        connector_public_key=_string(claims, "connector_public_key"),
        connector_key_id=_string(claims, "connector_key_id"),
        connector_nonce=_string(claims, "connector_nonce"),
        tls_ca_der=_string(claims, "tls_ca_der"),
        tls_ca_fingerprint=_string(claims, "tls_ca_fingerprint"),
        tls_leaf_fingerprint=_string(claims, "tls_leaf_fingerprint"),
        issued_at=_integer(claims, "issued_at"),
        deadline=_integer(claims, "deadline"),
        signature=signature,
        signature_algorithm=_string(claims, "signature_algorithm"),
    )


def _parse_pairing_complete(frame: dict[str, object]) -> PairingComplete:
    message_id, claims, signature = _signed_parts(frame, "pairing-complete")
    _closed(
        claims,
        {
            "completed_at",
            "connector_key_id",
            "pairing_id",
            "signature_algorithm",
            "signed_grant_digest",
            "transcript_digest",
            "vps_key_id",
        },
    )
    return PairingComplete(
        message_id=message_id,
        pairing_id=_string(claims, "pairing_id"),
        transcript_digest=_string(claims, "transcript_digest"),
        connector_key_id=_string(claims, "connector_key_id"),
        vps_key_id=_string(claims, "vps_key_id"),
        signed_grant_digest=_string(claims, "signed_grant_digest"),
        completed_at=_integer(claims, "completed_at"),
        signature=signature,
        signature_algorithm=_string(claims, "signature_algorithm"),
    )


def _parse_signed_grant(frame: dict[str, object]) -> SignedGrant:
    message_id, claims, signature = _signed_parts(frame, "signed-grant")
    return SignedGrant(message_id, _parse_grant_claims(claims), signature)


def _parse_pairing_resolution(frame: dict[str, object]) -> PairingResolution:
    _closed(frame, {"body", "message_id", "protocol", "type"})
    if (
        _string(frame, "protocol") != CONNECTOR_PROTOCOL_VERSION
        or _string(frame, "type") != "pairing-resolution"
    ):
        raise ProtocolValidationError("The Connector protocol is invalid.")
    body = _mapping(frame.get("body"))
    _closed(body, {"pairing_complete", "signed_grant"})
    signed_grant = parse_record(
        _decode_bounded_base64url(
            _string(body, "signed_grant"), maximum=MAX_FRAME_BYTES
        )
    )
    pairing_complete = parse_record(
        _decode_bounded_base64url(
            _string(body, "pairing_complete"), maximum=MAX_FRAME_BYTES
        )
    )
    if not isinstance(signed_grant, SignedGrant) or not isinstance(
        pairing_complete, PairingComplete
    ):
        raise ProtocolValidationError("The pairing resolution records are invalid.")
    return PairingResolution(
        message_id=_string(frame, "message_id"),
        pairing_id=pairing_complete.pairing_id,
        signed_grant=signed_grant,
        pairing_complete=pairing_complete,
    )


def _parse_operation_invocation(frame: dict[str, object]) -> OperationInvocationV1:
    _closed(frame, {"body", "message_id", "protocol", "type"})
    if (
        _string(frame, "protocol") != CONNECTOR_PROTOCOL_VERSION
        or _string(frame, "type") != "operation-invocation"
    ):
        raise ProtocolValidationError("The Connector protocol is invalid.")
    body = _mapping(frame.get("body"))
    _closed(body, {"protected_payload", "signed_request"})
    request = parse_record(
        _decode_bounded_base64url(
            _string(body, "signed_request"), maximum=MAX_FRAME_BYTES
        )
    )
    if not isinstance(request, SignedRequest):
        raise ProtocolValidationError("The operation invocation request is invalid.")
    protected_bytes = canonical_json_bytes(_mapping(body.get("protected_payload")))
    if len(protected_bytes) > MAX_PROTECTED_OPERATION_BYTES:
        raise ProtocolValidationError("The protected operation is oversized.")
    return OperationInvocationV1(
        message_id=_string(frame, "message_id"),
        signed_request=request,
        protected_payload=parse_protected_operation(protected_bytes),
    )


def _parse_result_media(value: object) -> OperationResultMediaV1 | None:
    if value is None:
        return None
    mapping = _mapping(value)
    _closed(
        mapping,
        {
            "comment_count",
            "coverage",
            "duration_seconds",
            "subtitle_language",
            "subtitle_origin",
            "version",
            "view_count",
        },
    )

    def optional_integer(field: str) -> int | None:
        item = mapping.get(field)
        return None if item is None else _integer(mapping, field)

    def optional_string(field: str) -> str | None:
        item = mapping.get(field)
        return None if item is None else _string(mapping, field)

    return OperationResultMediaV1(
        coverage=_string(mapping, "coverage"),
        duration_seconds=optional_integer("duration_seconds"),
        view_count=optional_integer("view_count"),
        comment_count=optional_integer("comment_count"),
        subtitle_language=optional_string("subtitle_language"),
        subtitle_origin=optional_string("subtitle_origin"),
        version=_string(mapping, "version"),
    )


def _parse_result_item(value: object) -> OperationResultItemV1:
    mapping = _mapping(value)
    _closed(
        mapping,
        {
            "author",
            "kind",
            "media",
            "native_id",
            "published_at",
            "text",
            "title",
            "url",
        },
    )

    def optional_string(field: str) -> str | None:
        item = mapping.get(field)
        return None if item is None else _string(mapping, field)

    return OperationResultItemV1(
        kind=_string(mapping, "kind"),
        text=_string(mapping, "text"),
        native_id=optional_string("native_id"),
        title=optional_string("title"),
        url=optional_string("url"),
        author=optional_string("author"),
        published_at=optional_string("published_at"),
        media=_parse_result_media(mapping.get("media")),
    )


def _parse_operation_result(value: object) -> OperationResultV1:
    mapping = _mapping(value)
    _closed(mapping, {"items", "truncated", "version"})
    raw_items = mapping.get("items")
    if type(raw_items) is not list:
        raise ProtocolValidationError("The operation result items are invalid.")
    items = cast(list[object], raw_items)
    return OperationResultV1(
        items=tuple(_parse_result_item(item) for item in items),
        truncated=_boolean(mapping, "truncated"),
        version=_string(mapping, "version"),
    )


def _parse_operation_response(frame: dict[str, object]) -> OperationResponseV1:
    _closed(frame, {"body", "message_id", "protocol", "type"})
    if (
        _string(frame, "protocol") != CONNECTOR_PROTOCOL_VERSION
        or _string(frame, "type") != "operation-response"
    ):
        raise ProtocolValidationError("The Connector protocol is invalid.")
    body = _mapping(frame.get("body"))
    _closed(body, {"receipt", "result"})
    receipt = parse_record(
        _decode_bounded_base64url(_string(body, "receipt"), maximum=MAX_FRAME_BYTES)
    )
    if not isinstance(receipt, SignedReceipt):
        raise ProtocolValidationError("The operation response receipt is invalid.")
    raw_result = body.get("result")
    result = None if raw_result is None else _parse_operation_result(raw_result)
    if result is not None:
        # Enforce MAX_OPERATION_RESULT_BYTES while parsing; bytes are not reused.
        canonical_operation_result_bytes(result)
    return OperationResponseV1(
        message_id=_string(frame, "message_id"),
        receipt=receipt,
        result=result,
    )


def _parse_signed_request(frame: dict[str, object]) -> SignedRequest:
    message_id, claims, signature = _signed_parts(frame, "operation-request")
    _closed(
        claims,
        {
            "audience_key_id",
            "deadline",
            "grant_id",
            "grant_revision",
            "issued_at",
            "operation",
            "payload_digest",
            "payload_digest_algorithm",
            "policy_revision",
            "request_id",
            "signature_algorithm",
            "source",
            "subject_key_id",
            "trace_id",
        },
    )
    return SignedRequest(
        message_id=message_id,
        request_id=_string(claims, "request_id"),
        trace_id=_string(claims, "trace_id"),
        audience_key_id=_string(claims, "audience_key_id"),
        subject_key_id=_string(claims, "subject_key_id"),
        grant_id=_string(claims, "grant_id"),
        grant_revision=_integer(claims, "grant_revision"),
        policy_revision=_integer(claims, "policy_revision"),
        source=_string(claims, "source"),
        operation=_string(claims, "operation"),
        issued_at=_integer(claims, "issued_at"),
        deadline=_integer(claims, "deadline"),
        payload_digest=_string(claims, "payload_digest"),
        signature=signature,
        payload_digest_algorithm=_string(claims, "payload_digest_algorithm"),
        signature_algorithm=_string(claims, "signature_algorithm"),
    )


def _parse_receipt_failure(value: object) -> ReceiptFailure | None:
    if value is None:
        return None
    mapping = _mapping(value)
    _closed(mapping, {"cause_code", "failure_class"})
    try:
        code = ConnectorErrorCode(_string(mapping, "cause_code"))
    except ValueError:
        raise ProtocolValidationError("The receipt failure code is invalid.") from None
    return ReceiptFailure(_string(mapping, "failure_class"), code)


def _parse_receipt_usage(value: object) -> ReceiptUsage | None:
    if value is None:
        return None
    mapping = _mapping(value)
    _closed(mapping, {"remaining", "sequence"})
    return ReceiptUsage(
        sequence=_integer(mapping, "sequence"),
        remaining=_integer(mapping, "remaining"),
    )


def _parse_backend(value: object) -> PublicBackendIdentity | None:
    if value is None:
        return None
    mapping = _mapping(value)
    _closed(mapping, {"backend_id", "backend_version"})
    return PublicBackendIdentity(
        _string(mapping, "backend_id"), _string(mapping, "backend_version")
    )


def _parse_signed_receipt(frame: dict[str, object]) -> SignedReceipt:
    message_id, claims, signature = _signed_parts(frame, "operation-receipt")
    _closed(
        claims,
        {
            "backend",
            "decision",
            "ended_at",
            "expires_at",
            "failure",
            "grant_id",
            "grant_revision",
            "issuer_key_id",
            "operation",
            "outcome",
            "payload_digest",
            "payload_digest_algorithm",
            "policy_revision",
            "receipt_id",
            "request_id",
            "result_count",
            "result_digest",
            "result_digest_algorithm",
            "signature_algorithm",
            "source",
            "started_at",
            "subject_key_id",
            "trace_id",
            "truncated",
            "usage",
        },
    )
    return SignedReceipt(
        message_id=message_id,
        receipt_id=_string(claims, "receipt_id"),
        request_id=_string(claims, "request_id"),
        trace_id=_string(claims, "trace_id"),
        issuer_key_id=_string(claims, "issuer_key_id"),
        subject_key_id=_string(claims, "subject_key_id"),
        grant_id=_string(claims, "grant_id"),
        grant_revision=_integer(claims, "grant_revision"),
        policy_revision=_integer(claims, "policy_revision"),
        source=_string(claims, "source"),
        operation=_string(claims, "operation"),
        decision=_string(claims, "decision"),
        failure=_parse_receipt_failure(claims.get("failure")),
        usage=_parse_receipt_usage(claims.get("usage")),
        backend=_parse_backend(claims.get("backend")),
        started_at=_integer(claims, "started_at"),
        ended_at=_integer(claims, "ended_at"),
        expires_at=_integer(claims, "expires_at"),
        result_count=_integer(claims, "result_count"),
        truncated=_boolean(claims, "truncated"),
        result_digest=(
            None
            if claims.get("result_digest") is None
            else _string(claims, "result_digest")
        ),
        outcome=_string(claims, "outcome"),
        payload_digest=_string(claims, "payload_digest"),
        signature=signature,
        payload_digest_algorithm=_string(claims, "payload_digest_algorithm"),
        result_digest_algorithm=_string(claims, "result_digest_algorithm"),
        signature_algorithm=_string(claims, "signature_algorithm"),
    )


def _parse_file_grant(frame: dict[str, object]) -> FileGrant:
    message_id, claims, signature = _signed_parts(frame, "file-grant")
    _closed(
        claims,
        {
            "digest",
            "expires_at",
            "file_grant_id",
            "grant_revision",
            "issued_at",
            "issuer_key_id",
            "operation",
            "policy_revision",
            "signature_algorithm",
            "single_use",
            "size",
            "source",
            "subject_key_id",
        },
    )
    return FileGrant(
        message_id=message_id,
        file_grant_id=_string(claims, "file_grant_id"),
        issuer_key_id=_string(claims, "issuer_key_id"),
        subject_key_id=_string(claims, "subject_key_id"),
        digest=_string(claims, "digest"),
        size=_integer(claims, "size"),
        source=_string(claims, "source"),
        operation=_string(claims, "operation"),
        grant_revision=_integer(claims, "grant_revision"),
        policy_revision=_integer(claims, "policy_revision"),
        issued_at=_integer(claims, "issued_at"),
        expires_at=_integer(claims, "expires_at"),
        signature=signature,
        single_use=_boolean(claims, "single_use"),
        signature_algorithm=_string(claims, "signature_algorithm"),
    )


def verify_record(record: SignedRecord, verifier: DevicePublicIdentity) -> None:
    """Verify one record signature against the exact expected device identity."""

    if not isinstance(verifier, DevicePublicIdentity) or not isinstance(
        record,
        PairingInit
        | PairingChallenge
        | PairingComplete
        | SignedGrant
        | SignedRequest
        | SignedReceipt
        | FileGrant,
    ):
        raise ProtocolValidationError("The Connector signature context is invalid.")
    if not verifier.verify(
        _signature_domain(record), record_signing_bytes(record), record.signature
    ):
        raise ProtocolValidationError("The Connector signature could not be verified.")


def _require_active(
    *, issued_at: int, expires_at: int, now: int, allow_clock_skew: bool
) -> None:
    _require_timestamp(now)
    skew = MAX_CLOCK_SKEW_SECONDS if allow_clock_skew else 0
    if now + skew < issued_at or now >= expires_at:
        raise ProtocolValidationError(
            "The Connector record is outside its time window."
        )


def verify_pairing_init(record: PairingInit, *, now: int) -> DevicePublicIdentity:
    """Verify a self-signed bounded pairing request and return its VPS identity."""

    if not isinstance(record, PairingInit):
        raise ProtocolValidationError("The pairing request type is invalid.")
    identity = _require_public_key(record.vps_public_key)
    verify_record(record, identity)
    _require_active(
        issued_at=record.issued_at,
        expires_at=record.deadline,
        now=now,
        allow_clock_skew=True,
    )
    return identity


def verify_pairing_challenge(
    record: PairingChallenge,
    *,
    expected_pairing_id: str,
    expected_vps_key_id: str,
    expected_init_digest: str,
    observed_tls_leaf_fingerprint: str,
    now: int,
) -> DevicePublicIdentity:
    """Verify the self-authenticating initial Connector challenge and context."""

    if not isinstance(record, PairingChallenge):
        raise ProtocolValidationError("The pairing challenge type is invalid.")
    identity = _require_public_key(record.connector_public_key)
    verify_record(record, identity)
    _require_digest(observed_tls_leaf_fingerprint)
    if (
        record.pairing_id != expected_pairing_id
        or record.vps_key_id != expected_vps_key_id
        or record.init_digest != expected_init_digest
        or record.tls_leaf_fingerprint != observed_tls_leaf_fingerprint
    ):
        raise ProtocolValidationError("The pairing challenge context does not match.")
    _require_active(
        issued_at=record.issued_at,
        expires_at=record.deadline,
        now=now,
        allow_clock_skew=True,
    )
    return identity


def verify_pairing_complete(
    record: PairingComplete,
    *,
    pinned_connector: DevicePublicIdentity,
    expected_pairing_id: str,
    expected_vps_key_id: str,
    expected_transcript_digest: str,
    expected_grant_digest: str,
    expected_deadline: int,
    now: int,
) -> None:
    """Verify pairing success independently from operation receipt evidence."""

    if not isinstance(record, PairingComplete):
        raise ProtocolValidationError("The pairing completion type is invalid.")
    verify_record(record, pinned_connector)
    _require_timestamp(expected_deadline)
    _require_timestamp(now)
    if (
        record.connector_key_id != pinned_connector.key_id
        or record.pairing_id != expected_pairing_id
        or record.vps_key_id != expected_vps_key_id
        or record.transcript_digest != expected_transcript_digest
        or record.signed_grant_digest != expected_grant_digest
        or record.completed_at > expected_deadline
    ):
        raise ProtocolValidationError("The pairing completion context does not match.")
    if now >= expected_deadline or now + MAX_CLOCK_SKEW_SECONDS < record.completed_at:
        raise ProtocolValidationError(
            "The pairing completion is outside its time window."
        )


def verify_pairing_resolution(
    record: PairingResolution,
    *,
    pairing_init: PairingInit,
    pairing_challenge: PairingChallenge,
    observed_tls_leaf_fingerprint: str,
    now: int,
) -> SignedGrant:
    """Verify an approval result against the exact previously displayed SAS."""

    if not isinstance(record, PairingResolution):
        raise ProtocolValidationError("The pairing resolution type is invalid.")
    vps_identity = verify_pairing_init(pairing_init, now=now)
    connector_identity = verify_pairing_challenge(
        pairing_challenge,
        expected_pairing_id=pairing_init.pairing_id,
        expected_vps_key_id=pairing_init.vps_key_id,
        expected_init_digest=record_digest(pairing_init),
        observed_tls_leaf_fingerprint=observed_tls_leaf_fingerprint,
        now=now,
    )
    signed_grant = record.signed_grant
    verify_signed_grant(
        signed_grant,
        pinned_connector=connector_identity,
        expected_subject_key_id=vps_identity.key_id,
        now=now,
    )
    claims = signed_grant.claims
    if (
        claims.revision != 1
        or claims.expires_at != pairing_init.grant_expires_at
        or claims.max_uses != pairing_init.grant_max_uses
        or claims.scopes != pairing_init.requested_scopes
    ):
        raise ProtocolValidationError(
            "The initial grant does not match the displayed pairing request."
        )
    transcript_digest = pairing_transcript_hash(
        encode_record(pairing_init),
        encode_record(pairing_challenge),
        observed_tls_leaf_fingerprint=observed_tls_leaf_fingerprint,
    ).hex()
    verify_pairing_complete(
        record.pairing_complete,
        pinned_connector=connector_identity,
        expected_pairing_id=pairing_init.pairing_id,
        expected_vps_key_id=vps_identity.key_id,
        expected_transcript_digest=transcript_digest,
        expected_grant_digest=record_digest(signed_grant),
        expected_deadline=pairing_challenge.deadline,
        now=now,
    )
    return signed_grant


def verify_signed_grant(
    record: SignedGrant,
    *,
    pinned_connector: DevicePublicIdentity,
    expected_subject_key_id: str,
    now: int,
) -> None:
    """Verify issuer, subject, active window, and signature of a local grant copy."""

    if not isinstance(record, SignedGrant):
        raise ProtocolValidationError("The signed grant type is invalid.")
    verify_record(record, pinned_connector)
    claims = record.claims
    if (
        claims.issuer_key_id != pinned_connector.key_id
        or claims.subject_key_id != expected_subject_key_id
    ):
        raise ProtocolValidationError(
            "The signed grant identity context does not match."
        )
    _require_active(
        issued_at=claims.not_before,
        expires_at=claims.expires_at,
        now=now,
        allow_clock_skew=False,
    )


def verify_signed_request(
    record: SignedRequest,
    *,
    pinned_vps: DevicePublicIdentity,
    expected_connector_key_id: str,
    protected_payload: ProtectedOperationPayload,
    now: int,
) -> None:
    """Verify exact audience, signer, payload digest, operation, and deadline."""

    if not isinstance(record, SignedRequest):
        raise ProtocolValidationError("The signed request type is invalid.")
    verify_record(record, pinned_vps)
    call = protected_payload.to_operation_call()
    if (
        record.subject_key_id != pinned_vps.key_id
        or record.audience_key_id != expected_connector_key_id
        or record.payload_digest != operation_payload_digest(protected_payload)
        or record.source != call.source.name
        or record.operation != call.operation.name
    ):
        raise ProtocolValidationError("The signed request context does not match.")
    _require_active(
        issued_at=record.issued_at,
        expires_at=record.deadline,
        now=now,
        allow_clock_skew=True,
    )


def verify_signed_receipt(
    record: SignedReceipt,
    *,
    pinned_connector: DevicePublicIdentity,
    request: SignedRequest,
    now: int,
) -> None:
    """Verify a receipt against the complete exact request context before trust."""

    if not isinstance(record, SignedReceipt) or not isinstance(request, SignedRequest):
        raise ProtocolValidationError("The signed receipt type is invalid.")
    verify_record(record, pinned_connector)
    if (
        record.issuer_key_id != pinned_connector.key_id
        or request.audience_key_id != pinned_connector.key_id
        or record.request_id != request.request_id
        or record.trace_id != request.trace_id
        or record.subject_key_id != request.subject_key_id
        or record.grant_id != request.grant_id
        or record.grant_revision != request.grant_revision
        or record.policy_revision != request.policy_revision
        or record.source != request.source
        or record.operation != request.operation
        or record.payload_digest != request.payload_digest
        or not request.issued_at <= record.started_at <= request.deadline
        or (record.outcome == "ok" and record.ended_at >= request.deadline)
    ):
        raise ReceiptContextMismatchError("The signed receipt context does not match.")
    if record.outcome == "ok" and now >= request.deadline:
        raise ReceiptExpiredError("The successful receipt arrived after its deadline.")
    try:
        _require_active(
            issued_at=record.ended_at,
            expires_at=record.expires_at,
            now=now,
            allow_clock_skew=True,
        )
    except ProtocolValidationError:
        raise ReceiptExpiredError(
            "The Connector receipt is outside its time window."
        ) from None


def verify_operation_response(
    response: OperationResponseV1,
    *,
    pinned_connector: DevicePublicIdentity,
    request: SignedRequest,
    now: int,
) -> None:
    """Verify the complete receipt-bound response before evidence or projection."""

    if not isinstance(response, OperationResponseV1):
        raise ProtocolValidationError("The operation response type is invalid.")
    verify_signed_receipt(
        response.receipt,
        pinned_connector=pinned_connector,
        request=request,
        now=now,
    )
    _validate_response_result(response.receipt, response.result)


def verify_file_grant(
    record: FileGrant,
    *,
    pinned_connector: DevicePublicIdentity,
    expected_subject_key_id: str,
    expected_grant_revision: int,
    expected_policy_revision: int,
    expected_source: str,
    expected_operation: str,
    now: int,
) -> None:
    """Verify a single-use file grant without resolving a trusted-device path."""

    if not isinstance(record, FileGrant):
        raise ProtocolValidationError("The file grant type is invalid.")
    verify_record(record, pinned_connector)
    if (
        record.issuer_key_id != pinned_connector.key_id
        or record.subject_key_id != expected_subject_key_id
        or record.grant_revision != expected_grant_revision
        or record.policy_revision != expected_policy_revision
        or record.source != expected_source
        or record.operation != expected_operation
    ):
        raise ProtocolValidationError("The file grant context does not match.")
    _require_active(
        issued_at=record.issued_at,
        expires_at=record.expires_at,
        now=now,
        allow_clock_skew=False,
    )
