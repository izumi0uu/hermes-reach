"""Verified, owner-only receipt evidence with a tamper-evident hash chain."""

from __future__ import annotations

import base64
import binascii
import fcntl
import hashlib
import os
import secrets
import stat
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Final, Literal

from .errors import ConnectorError, ConnectorErrorCode
from .identity import DevicePublicIdentity, _open_state_directory
from .limits import AUDIT_RETENTION_SECONDS, MAX_FRAME_BYTES, MAX_TIMESTAMP_SECONDS
from .protocol import (
    ProtocolValidationError,
    ReceiptContextMismatchError,
    ReceiptExpiredError,
    SignedReceipt,
    SignedRequest,
    canonical_json_bytes,
    encode_record,
    load_canonical_json,
    parse_record,
    record_digest,
    verify_record,
    verify_signed_receipt,
)

EvidenceRole = Literal["connector", "vps"]

_EVIDENCE_VERSION: Final = 1
_MAX_LEDGER_BYTES: Final = 64 * 1024 * 1024
_MAX_EVIDENCE_RECORDS: Final = 100_000
_HASH_DOMAIN: Final = b"hermes-reach:connector:receipt-evidence:v1\x00"
_ZERO_HASH: Final = ""


class ReceiptEvidenceError(ValueError):
    """A local receipt-evidence invariant failed before data could be trusted."""


@dataclass(frozen=True, slots=True)
class ReceiptEvidenceRecord:
    role: EvidenceRole
    recorded_at: int
    receipt: SignedReceipt
    receipt_digest: str
    previous_hash: str
    record_hash: str


def verify_receipt(
    receipt: SignedReceipt,
    *,
    pinned_connector: DevicePublicIdentity,
    request: SignedRequest,
    now: int,
) -> SignedReceipt:
    """Map exact protocol verification failures to closed receipt outcomes."""

    try:
        verify_signed_receipt(
            receipt,
            pinned_connector=pinned_connector,
            request=request,
            now=now,
        )
    except ReceiptContextMismatchError:
        raise ConnectorError(ConnectorErrorCode.RECEIPT_CONTEXT_MISMATCH) from None
    except ReceiptExpiredError:
        raise ConnectorError(ConnectorErrorCode.RECEIPT_EXPIRED) from None
    except (ProtocolValidationError, TypeError, ValueError):
        raise ConnectorError(ConnectorErrorCode.RECEIPT_INVALID) from None
    return receipt


class ReceiptEvidenceLedger:
    """Persist only receipts verified against their exact originating request."""

    def __init__(
        self,
        path: Path,
        pinned_connector: DevicePublicIdentity,
        *,
        role: EvidenceRole,
    ) -> None:
        if not path.is_absolute() or not path.name or path.name in {".", ".."}:
            raise ValueError("Receipt evidence requires an absolute file path.")
        if not isinstance(pinned_connector, DevicePublicIdentity):
            raise TypeError("Receipt evidence requires a pinned Connector identity.")
        if role not in {"connector", "vps"}:
            raise ValueError("Receipt evidence role is invalid.")
        self._directory = path.parent
        self._filename = path.name
        self._lock_filename = f".{path.name}.lock"
        self._pinned_connector = pinned_connector
        self._role = role
        self._mutex = threading.RLock()

    def append_verified(
        self,
        receipt: SignedReceipt,
        *,
        request: SignedRequest,
        now: int,
    ) -> ReceiptEvidenceRecord:
        """Verify exact context before durably appending one receipt."""

        verify_receipt(
            receipt,
            pinned_connector=self._pinned_connector,
            request=request,
            now=now,
        )
        with self._locked_directory(exclusive=True) as directory_descriptor:
            records = self._read_records(directory_descriptor)
            digest = record_digest(receipt)
            if any(
                record.receipt.receipt_id == receipt.receipt_id
                or record.receipt_digest == digest
                for record in records
            ):
                raise ConnectorError(ConnectorErrorCode.RECEIPT_REPLAYED)
            if len(records) >= _MAX_EVIDENCE_RECORDS:
                raise ReceiptEvidenceError("The receipt evidence ledger is full.")
            if (
                type(now) is not int
                or not 0 <= now <= MAX_TIMESTAMP_SECONDS
                or (records and now < records[-1].recorded_at)
            ):
                raise ReceiptEvidenceError("The receipt evidence time is invalid.")
            previous_hash = records[-1].record_hash if records else _ZERO_HASH
            record = ReceiptEvidenceRecord(
                self._role,
                now,
                receipt,
                digest,
                previous_hash,
                "",
            )
            record = replace(record, record_hash=_evidence_hash(record))
            self._append_record(directory_descriptor, record)
            return record

    def records(self) -> tuple[ReceiptEvidenceRecord, ...]:
        with self._locked_directory(exclusive=False) as directory_descriptor:
            return self._read_records(directory_descriptor)

    def prune(self, *, now: int) -> int:
        if type(now) is not int or not 0 <= now <= MAX_TIMESTAMP_SECONDS:
            raise ValueError("Receipt evidence prune time is invalid.")
        cutoff = max(0, now - AUDIT_RETENTION_SECONDS)
        with self._locked_directory(exclusive=True) as directory_descriptor:
            records = self._read_records(directory_descriptor)
            retained = tuple(
                record for record in records if record.recorded_at >= cutoff
            )
            removed = len(records) - len(retained)
            if removed:
                self._rewrite(directory_descriptor, _rechain(retained))
            return removed

    @contextmanager
    def _locked_directory(self, *, exclusive: bool) -> Iterator[int]:
        with self._mutex:
            directory_descriptor = -1
            lock_descriptor = -1
            try:
                directory_descriptor = _open_state_directory(
                    self._directory, create=True
                )
                lock_descriptor = _open_owner_file(
                    directory_descriptor, self._lock_filename, create=True
                )
                fcntl.flock(
                    lock_descriptor, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
                )
                yield directory_descriptor
            except (ConnectorError, ReceiptEvidenceError):
                raise
            except (OSError, ValueError):
                raise ReceiptEvidenceError(
                    "The receipt evidence storage could not be verified."
                ) from None
            finally:
                if lock_descriptor >= 0:
                    os.close(lock_descriptor)
                if directory_descriptor >= 0:
                    os.close(directory_descriptor)

    def _read_records(
        self, directory_descriptor: int
    ) -> tuple[ReceiptEvidenceRecord, ...]:
        descriptor = -1
        try:
            descriptor = _open_owner_file(
                directory_descriptor, self._filename, create=False, writable=False
            )
        except FileNotFoundError:
            return ()
        try:
            metadata = os.fstat(descriptor)
            if metadata.st_size > _MAX_LEDGER_BYTES:
                raise ReceiptEvidenceError("The receipt evidence ledger is oversized.")
            payload = _read_bounded(descriptor, _MAX_LEDGER_BYTES + 1)
            if len(payload) > _MAX_LEDGER_BYTES:
                raise ReceiptEvidenceError("The receipt evidence ledger is oversized.")
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        try:
            lines = payload.splitlines()
            if len(lines) > _MAX_EVIDENCE_RECORDS:
                raise ReceiptEvidenceError("The receipt evidence ledger is oversized.")
            records = tuple(_parse_evidence(line) for line in lines)
        except ReceiptEvidenceError:
            raise
        except (UnicodeError, ValueError):
            raise ReceiptEvidenceError(
                "The receipt evidence ledger is malformed."
            ) from None
        previous_hash = _ZERO_HASH
        seen_receipt_ids: set[str] = set()
        seen_digests: set[str] = set()
        previous_time = 0
        for record in records:
            try:
                verify_record(record.receipt, self._pinned_connector)
            except ProtocolValidationError:
                raise ReceiptEvidenceError(
                    "The receipt evidence signature is invalid."
                ) from None
            if (
                record.role != self._role
                or record.previous_hash != previous_hash
                or record.record_hash != _evidence_hash(record)
                or record.receipt.receipt_id in seen_receipt_ids
                or record.receipt_digest in seen_digests
                or record.recorded_at < previous_time
            ):
                raise ReceiptEvidenceError(
                    "The receipt evidence hash chain is invalid."
                )
            previous_hash = record.record_hash
            seen_receipt_ids.add(record.receipt.receipt_id)
            seen_digests.add(record.receipt_digest)
            previous_time = record.recorded_at
        return records

    def _append_record(
        self, directory_descriptor: int, record: ReceiptEvidenceRecord
    ) -> None:
        payload = _serialize_evidence(record) + b"\n"
        descriptor = _open_owner_file(
            directory_descriptor, self._filename, create=True, append=True
        )
        try:
            metadata = os.fstat(descriptor)
            if metadata.st_size + len(payload) > _MAX_LEDGER_BYTES:
                raise ReceiptEvidenceError("The receipt evidence ledger is full.")
            _write_all(descriptor, payload)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _rewrite(
        self,
        directory_descriptor: int,
        records: tuple[ReceiptEvidenceRecord, ...],
    ) -> None:
        temporary_name = f".{self._filename}.{secrets.token_hex(12)}"
        descriptor = -1
        try:
            descriptor = os.open(
                temporary_name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600,
                dir_fd=directory_descriptor,
            )
            os.fchmod(descriptor, 0o600)
            for record in records:
                _write_all(descriptor, _serialize_evidence(record) + b"\n")
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = -1
            os.replace(
                temporary_name,
                self._filename,
                src_dir_fd=directory_descriptor,
                dst_dir_fd=directory_descriptor,
            )
            os.fsync(directory_descriptor)
        except BaseException:
            try:
                os.unlink(temporary_name, dir_fd=directory_descriptor)
            except OSError:
                pass
            raise
        finally:
            if descriptor >= 0:
                os.close(descriptor)


def _evidence_mapping(record: ReceiptEvidenceRecord) -> dict[str, object]:
    return {
        "previous_hash": record.previous_hash,
        "receipt": base64.b64encode(encode_record(record.receipt)).decode("ascii"),
        "receipt_digest": record.receipt_digest,
        "record_hash": record.record_hash,
        "recorded_at": record.recorded_at,
        "role": record.role,
        "version": _EVIDENCE_VERSION,
    }


def _serialize_evidence(record: ReceiptEvidenceRecord) -> bytes:
    return canonical_json_bytes(_evidence_mapping(record))


def _evidence_hash(record: ReceiptEvidenceRecord) -> str:
    payload = canonical_json_bytes(_evidence_mapping(replace(record, record_hash="")))
    return hashlib.sha256(_HASH_DOMAIN + payload).hexdigest()


def _parse_evidence(raw: bytes) -> ReceiptEvidenceRecord:
    try:
        value = load_canonical_json(raw, max_bytes=MAX_FRAME_BYTES)
        if not isinstance(value, dict) or set(value) != {
            "previous_hash",
            "receipt",
            "receipt_digest",
            "record_hash",
            "recorded_at",
            "role",
            "version",
        }:
            raise ReceiptEvidenceError("The receipt evidence schema is invalid.")
        if value["version"] != _EVIDENCE_VERSION:
            raise ReceiptEvidenceError("The receipt evidence version is invalid.")
        encoded_receipt = _string(value, "receipt")
        receipt_payload = base64.b64decode(encoded_receipt, validate=True)
        parsed = parse_record(receipt_payload)
        if not isinstance(parsed, SignedReceipt):
            raise ReceiptEvidenceError("The evidence does not contain a receipt.")
        digest = _string(value, "receipt_digest")
        if digest != record_digest(parsed):
            raise ReceiptEvidenceError("The receipt evidence digest is invalid.")
        return ReceiptEvidenceRecord(
            role=_role(value["role"]),
            recorded_at=_integer(value, "recorded_at"),
            receipt=parsed,
            receipt_digest=digest,
            previous_hash=_hash(value, "previous_hash", allow_empty=True),
            record_hash=_hash(value, "record_hash"),
        )
    except ReceiptEvidenceError:
        raise
    except (binascii.Error, KeyError, ProtocolValidationError, ValueError):
        raise ReceiptEvidenceError("The receipt evidence values are invalid.") from None


def _rechain(
    records: tuple[ReceiptEvidenceRecord, ...],
) -> tuple[ReceiptEvidenceRecord, ...]:
    previous_hash = _ZERO_HASH
    result: list[ReceiptEvidenceRecord] = []
    for old in records:
        record = replace(old, previous_hash=previous_hash, record_hash="")
        record = replace(record, record_hash=_evidence_hash(record))
        result.append(record)
        previous_hash = record.record_hash
    return tuple(result)


def _open_owner_file(
    directory_descriptor: int,
    filename: str,
    *,
    create: bool,
    writable: bool = True,
    append: bool = False,
) -> int:
    flags = os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
    flags |= os.O_WRONLY if writable else os.O_RDONLY
    if append:
        flags |= os.O_APPEND
    if create:
        try:
            descriptor = os.open(
                filename,
                flags | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=directory_descriptor,
            )
        except FileExistsError:
            descriptor = os.open(filename, flags, dir_fd=directory_descriptor)
    else:
        descriptor = os.open(filename, flags, dir_fd=directory_descriptor)
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_nlink != 1
        ):
            raise OSError("unsafe receipt evidence file")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _read_bounded(descriptor: int, maximum: int) -> bytes:
    chunks: list[bytes] = []
    remaining = maximum
    while remaining:
        chunk = os.read(descriptor, min(remaining, 65_536))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _write_all(descriptor: int, payload: bytes) -> None:
    offset = 0
    while offset < len(payload):
        written = os.write(descriptor, payload[offset:])
        if written <= 0:
            raise OSError("receipt evidence write failed")
        offset += written


def _string(value: Mapping[str, object], name: str) -> str:
    item = value[name]
    if type(item) is not str or not item:
        raise ReceiptEvidenceError("The receipt evidence text is invalid.")
    return item


def _integer(value: Mapping[str, object], name: str) -> int:
    item = value[name]
    if type(item) is not int or not 0 <= item <= MAX_TIMESTAMP_SECONDS:
        raise ReceiptEvidenceError("The receipt evidence timestamp is invalid.")
    return item


def _hash(value: Mapping[str, object], name: str, *, allow_empty: bool = False) -> str:
    item = value[name]
    if type(item) is not str:
        raise ReceiptEvidenceError("The receipt evidence hash is invalid.")
    if item == "" and allow_empty:
        return item
    if (
        len(item) != 64
        or not item.isascii()
        or any(character not in "0123456789abcdef" for character in item)
    ):
        raise ReceiptEvidenceError("The receipt evidence hash is invalid.")
    return item


def _role(value: object) -> EvidenceRole:
    if value == "connector":
        return "connector"
    if value == "vps":
        return "vps"
    raise ReceiptEvidenceError("The receipt evidence role is invalid.")
