"""Closed-schema, hash-chained local metadata ledger for Reach decisions."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Sequence
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, Protocol

from ..catalog import get_operation, get_source

AuditOutcome = Literal["ok", "error"]


class AuditLedgerError(ValueError):
    """A local ledger invariant failed before data could be trusted."""


@dataclass(frozen=True, slots=True)
class AuditEvent:
    """The complete allowlist of metadata permitted in a Reach audit event."""

    trace_id: str
    device_id: str
    source: str
    operation: str
    backend_id: str
    backend_version: str
    catalog_version: str
    policy_revision: str
    grant_revision: str | None
    occurred_at: datetime
    duration_ms: int
    attempt_count: int
    failure_class: str | None
    result_count: int
    truncated: bool
    outcome: AuditOutcome


@dataclass(frozen=True, slots=True)
class AuditRecord:
    """A verified event with its predecessor link and canonical hash."""

    event: AuditEvent
    previous_hash: str
    record_hash: str


class ImmutableAuditExporter(Protocol):
    """Future operator-owned immutable sinks implement this interface."""

    def export(self, records: Sequence[AuditRecord]) -> None:
        """Export already-redacted and verified ledger records."""


class AuditLedger:
    """Append and verify only closed, redacted metadata records."""

    def __init__(self, path: Path, retention_days: int = 30) -> None:
        if retention_days <= 0:
            raise ValueError("Audit retention must be positive.")
        self._path = path
        self._retention_days = retention_days

    def append(self, event: AuditEvent) -> AuditRecord:
        """Validate and append one metadata-only event with a hash link."""

        self._validate_event(event)
        records = self.records()
        previous_hash = records[-1].record_hash if records else ""
        record = AuditRecord(event, previous_hash, "")
        record = replace(record, record_hash=_record_hash(record))
        self._ensure_parent()
        descriptor = os.open(self._path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "a", encoding="utf-8") as ledger_file:
                descriptor = -1
                ledger_file.write(_serialize_record(record))
                ledger_file.write("\n")
        finally:
            if descriptor != -1:
                os.close(descriptor)
        return record

    def records(self) -> tuple[AuditRecord, ...]:
        """Load and verify the complete current retention window."""

        if not self._path.exists():
            return ()
        try:
            lines = self._path.read_text(encoding="utf-8").splitlines()
        except OSError as error:
            raise AuditLedgerError("The audit ledger cannot be read.") from error
        records = tuple(_parse_record(line) for line in lines)
        previous_hash = ""
        for record in records:
            self._validate_event(record.event)
            if (
                record.previous_hash != previous_hash
                or record.record_hash != _record_hash(record)
            ):
                raise AuditLedgerError("The audit ledger hash chain is invalid.")
            previous_hash = record.record_hash
        return records

    def prune(self, now: datetime) -> int:
        """Retain the configured window and restart its local hash chain."""

        if now.tzinfo is None:
            raise ValueError("Audit prune time must include a timezone.")
        records = self.records()
        cutoff = now.astimezone(UTC) - timedelta(days=self._retention_days)
        retained = tuple(
            record for record in records if record.event.occurred_at >= cutoff
        )
        removed = len(records) - len(retained)
        if removed:
            self._rewrite(_rechain(retained))
        return removed

    def _ensure_parent(self) -> None:
        self._path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)

    def _rewrite(self, records: tuple[AuditRecord, ...]) -> None:
        self._ensure_parent()
        descriptor, temporary_path = tempfile.mkstemp(
            dir=self._path.parent, prefix=f".{self._path.name}.", text=True
        )
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as ledger_file:
                descriptor = -1
                for record in records:
                    ledger_file.write(_serialize_record(record))
                    ledger_file.write("\n")
            os.replace(temporary_path, self._path)
            os.chmod(self._path, 0o600)
        except Exception:
            Path(temporary_path).unlink(missing_ok=True)
            raise
        finally:
            if descriptor != -1:
                os.close(descriptor)

    def _validate_event(self, event: AuditEvent) -> None:
        if not isinstance(event, AuditEvent):
            raise AuditLedgerError("The audit ledger accepts only AuditEvent values.")
        source = get_source(event.source)
        if source is None or get_operation(source, event.operation) is None:
            raise AuditLedgerError("Audit events must reference a catalog operation.")
        if event.occurred_at.tzinfo is None:
            raise AuditLedgerError("Audit event timestamps must include a timezone.")
        if any(
            value < 0
            for value in (event.duration_ms, event.attempt_count, event.result_count)
        ):
            raise AuditLedgerError("Audit event counters cannot be negative.")
        if event.outcome not in {"ok", "error"}:
            raise AuditLedgerError("Audit events must have a stable outcome.")
        identifiers = (
            event.trace_id,
            event.device_id,
            event.backend_id,
            event.backend_version,
            event.catalog_version,
            event.policy_revision,
        )
        if not all(_is_safe_identifier(value) for value in identifiers):
            raise AuditLedgerError("Audit metadata contains an unsafe identifier.")
        if event.grant_revision is not None and not _is_safe_identifier(
            event.grant_revision
        ):
            raise AuditLedgerError("Audit metadata contains an unsafe grant revision.")
        if event.failure_class is not None and not _is_safe_identifier(
            event.failure_class
        ):
            raise AuditLedgerError("Audit metadata contains an unsafe failure class.")


def _serialize_record(record: AuditRecord) -> str:
    return json.dumps(
        _record_mapping(record),
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


def _record_mapping(record: AuditRecord) -> dict[str, object]:
    event = asdict(record.event)
    occurred_at = event["occurred_at"]
    if not isinstance(occurred_at, datetime):
        raise AuditLedgerError("Audit timestamps must be datetimes.")
    event["occurred_at"] = occurred_at.astimezone(UTC).isoformat()
    return {
        "event": event,
        "previous_hash": record.previous_hash,
        "record_hash": record.record_hash,
    }


def _record_hash(record: AuditRecord) -> str:
    payload = _record_mapping(replace(record, record_hash=""))
    encoded = json.dumps(
        payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _parse_record(line: str) -> AuditRecord:
    try:
        raw = json.loads(line)
    except json.JSONDecodeError as error:
        raise AuditLedgerError("The audit ledger contains malformed JSON.") from error
    if not isinstance(raw, dict) or set(raw) != {
        "event",
        "previous_hash",
        "record_hash",
    }:
        raise AuditLedgerError("The audit ledger record schema is invalid.")
    event_raw = raw["event"]
    if not isinstance(event_raw, dict) or set(event_raw) != set(
        AuditEvent.__dataclass_fields__
    ):
        raise AuditLedgerError("The audit event schema is invalid.")
    occurred_at = event_raw.get("occurred_at")
    if not isinstance(occurred_at, str):
        raise AuditLedgerError("The audit event timestamp is invalid.")
    try:
        event = AuditEvent(
            trace_id=_string(event_raw, "trace_id"),
            device_id=_string(event_raw, "device_id"),
            source=_string(event_raw, "source"),
            operation=_string(event_raw, "operation"),
            backend_id=_string(event_raw, "backend_id"),
            backend_version=_string(event_raw, "backend_version"),
            catalog_version=_string(event_raw, "catalog_version"),
            policy_revision=_string(event_raw, "policy_revision"),
            grant_revision=_optional_string(event_raw, "grant_revision"),
            occurred_at=datetime.fromisoformat(occurred_at),
            duration_ms=_integer(event_raw, "duration_ms"),
            attempt_count=_integer(event_raw, "attempt_count"),
            failure_class=_optional_string(event_raw, "failure_class"),
            result_count=_integer(event_raw, "result_count"),
            truncated=_boolean(event_raw, "truncated"),
            outcome=_outcome(event_raw),
        )
        previous_hash = _string(raw, "previous_hash", allow_empty=True)
        record_hash = _string(raw, "record_hash")
    except (TypeError, ValueError) as error:
        raise AuditLedgerError("The audit ledger record values are invalid.") from error
    return AuditRecord(event, previous_hash, record_hash)


def _rechain(records: tuple[AuditRecord, ...]) -> tuple[AuditRecord, ...]:
    previous_hash = ""
    rechained: list[AuditRecord] = []
    for existing in records:
        record = AuditRecord(existing.event, previous_hash, "")
        record = replace(record, record_hash=_record_hash(record))
        rechained.append(record)
        previous_hash = record.record_hash
    return tuple(rechained)


def _is_safe_identifier(value: str) -> bool:
    if not value.isascii():
        return False
    forbidden_markers = (
        "http://",
        "https://",
        "query",
        "content",
        "secret",
        "token",
        "password",
        "credential",
        "cookie",
        "resource_ref",
        "continuation",
        "/",
        "\\",
        "?",
        "&",
        "=",
    )
    if any(marker in value.lower() for marker in forbidden_markers):
        return False
    normalized = (
        value.replace("-", "").replace("_", "").replace(".", "").replace(":", "")
    )
    return bool(value) and len(value) <= 128 and normalized.isalnum()


def _string(value: dict[object, object], name: str, allow_empty: bool = False) -> str:
    item = value[name]
    if not isinstance(item, str) or (not allow_empty and not item):
        raise ValueError(name)
    return item


def _optional_string(value: dict[object, object], name: str) -> str | None:
    item = value[name]
    if item is not None and not isinstance(item, str):
        raise ValueError(name)
    return item


def _integer(value: dict[object, object], name: str) -> int:
    item = value[name]
    if isinstance(item, bool) or not isinstance(item, int):
        raise ValueError(name)
    return item


def _boolean(value: dict[object, object], name: str) -> bool:
    item = value[name]
    if not isinstance(item, bool):
        raise ValueError(name)
    return item


def _outcome(value: dict[object, object]) -> AuditOutcome:
    outcome = _string(value, "outcome")
    if outcome == "ok":
        return "ok"
    if outcome == "error":
        return "error"
    raise ValueError("outcome")
