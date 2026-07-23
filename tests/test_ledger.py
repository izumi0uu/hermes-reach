from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from hermes_reach.audit.ledger import AuditEvent, AuditLedger, AuditLedgerError


def _event(occurred_at: datetime) -> AuditEvent:
    return AuditEvent(
        trace_id="a" * 32,
        device_id="device-1",
        source="github",
        operation="search.repositories",
        backend_id="backend-1",
        backend_version="1.0",
        catalog_version="v1",
        policy_revision="v1",
        grant_revision="grant-1",
        occurred_at=occurred_at,
        duration_ms=12,
        attempt_count=1,
        failure_class=None,
        result_count=1,
        truncated=False,
        outcome="ok",
    )


def test_ledger_hash_chain_permissions_and_tamper_detection(tmp_path: Path) -> None:
    path = tmp_path / "audit" / "ledger.jsonl"
    ledger = AuditLedger(path)
    now = datetime(2026, 7, 23, tzinfo=UTC)
    first = ledger.append(_event(now))
    second = ledger.append(replace(_event(now), trace_id="b" * 32))

    assert second.previous_hash == first.record_hash
    assert ledger.records() == (first, second)
    assert path.stat().st_mode & 0o777 == 0o600

    path.write_text(
        path.read_text(encoding="utf-8").replace("backend-1", "tampered"),
        encoding="utf-8",
    )
    with pytest.raises(AuditLedgerError, match="hash chain"):
        ledger.records()


def test_prune_rechains_only_retained_metadata(tmp_path: Path) -> None:
    path = tmp_path / "ledger.jsonl"
    ledger = AuditLedger(path, retention_days=30)
    now = datetime(2026, 7, 23, tzinfo=UTC)
    ledger.append(_event(now - timedelta(days=31)))
    current = ledger.append(replace(_event(now), trace_id="b" * 32))

    assert ledger.prune(now) == 1
    retained = ledger.records()
    assert len(retained) == 1
    assert retained[0].event == current.event
    assert retained[0].previous_hash == ""


def test_ledger_rejects_untrusted_payload_like_metadata_and_invalid_events(
    tmp_path: Path,
) -> None:
    path = tmp_path / "ledger.jsonl"
    ledger = AuditLedger(path)
    valid = _event(datetime(2026, 7, 23, tzinfo=UTC))
    ledger.append(valid)
    probes = (
        "private-query-value",
        "content-body",
        "https://example.test/path?secret=value",
        "resource_ref-value",
        "password-value",
    )

    for probe in probes:
        with pytest.raises(AuditLedgerError, match="unsafe"):
            ledger.append(replace(valid, trace_id=probe))
    with pytest.raises(AuditLedgerError, match="catalog operation"):
        ledger.append(replace(valid, operation="read.unknown"))
    with pytest.raises(AuditLedgerError, match="only AuditEvent"):
        ledger.append({})  # type: ignore[arg-type]

    serialized = path.read_text(encoding="utf-8")
    assert all(probe not in serialized for probe in probes)
