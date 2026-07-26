from __future__ import annotations

import base64
import os
import stat
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest

from hermes_reach.connector.audit import (
    ReceiptEvidenceError,
    ReceiptEvidenceLedger,
    verify_receipt,
)
from hermes_reach.connector.authority import BoundReceiptIssuer
from hermes_reach.connector.errors import ConnectorError, ConnectorErrorCode
from hermes_reach.connector.identity import DevicePrivateIdentity
from hermes_reach.connector.limits import AUDIT_RETENTION_SECONDS
from hermes_reach.connector.protocol import (
    OperationResultItemV1,
    OperationResultV1,
    PublicBackendIdentity,
    SignedReceipt,
    SignedRequest,
    create_signed_request,
    protect_operation_call,
)
from hermes_reach.connector.store import ClaimResult
from hermes_reach.contracts import validate_read

NOW = 1_800_000_000
CANARY = "QUERY_CANARY=TOKEN_CANARY"
BACKEND = PublicBackendIdentity("reach-bounded-executor-v1", "1")
RESULT = OperationResultV1((OperationResultItemV1("content", "result"),), False)
PROTECTED = protect_operation_call(
    validate_read(
        {
            "source": "web",
            "operation": "read.url",
            "target": {"url": f"https://example.com/private?{CANARY}"},
        }
    )
)


def _canonical_id(value: int) -> str:
    return base64.b32encode(value.to_bytes(16, "big")).decode().rstrip("=").lower()


def _identity(seed: int) -> DevicePrivateIdentity:
    return DevicePrivateIdentity._from_seed_for_testing(bytes([seed]) * 32)


class _IdFactory:
    def __init__(self, start: int = 10_000) -> None:
        self._value = start

    def __call__(self) -> str:
        self._value += 1
        return _canonical_id(self._value)


def _request(
    connector: DevicePrivateIdentity,
    vps: DevicePrivateIdentity,
    *,
    slot: int,
    issued_at: int = NOW,
) -> SignedRequest:
    return create_signed_request(
        signer=vps,
        message_id=_canonical_id(1_000 + slot),
        request_id=_canonical_id(2_000 + slot),
        trace_id=f"{slot:032x}",
        audience_key_id=connector.public_identity.key_id,
        grant_id=_canonical_id(3_000),
        grant_revision=1,
        policy_revision=1,
        source="web",
        operation="read.url",
        issued_at=issued_at,
        deadline=issued_at + 60,
        protected_payload=PROTECTED,
    )


def _success_receipt(
    connector: DevicePrivateIdentity,
    request: SignedRequest,
    *,
    sequence: int = 1,
    remaining: int = 9,
    ended_at: int | None = None,
    factory: _IdFactory | None = None,
) -> SignedReceipt:
    effective_end = request.issued_at + 2 if ended_at is None else ended_at
    issuer = BoundReceiptIssuer(
        connector,
        request,
        ClaimResult(True, None, sequence, remaining),
        started_at=request.issued_at + 1,
        id_factory=_IdFactory() if factory is None else factory,
    )
    return issuer.issue(
        ended_at=effective_end,
        expires_at=effective_end + 120,
        backend=BACKEND,
        result=RESULT,
    )


def _assert_connector_error(
    error: pytest.ExceptionInfo[ConnectorError], code: ConnectorErrorCode
) -> None:
    assert error.value.code == code.value


def test_bound_receipt_issuer_signs_exact_success_once() -> None:
    connector = _identity(20)
    vps = _identity(21)
    request = _request(connector, vps, slot=1)
    issuer = BoundReceiptIssuer(
        connector,
        request,
        ClaimResult(True, None, 1, 9),
        started_at=NOW + 1,
        id_factory=_IdFactory(),
    )

    receipt = issuer.issue(
        ended_at=NOW + 2,
        expires_at=NOW + 120,
        backend=BACKEND,
        result=RESULT,
    )

    assert receipt.decision == "allow"
    assert receipt.outcome == "ok"
    assert receipt.failure is None
    assert receipt.usage is not None
    assert (receipt.usage.sequence, receipt.usage.remaining) == (1, 9)
    assert (
        verify_receipt(
            receipt,
            pinned_connector=connector.public_identity,
            request=request,
            now=NOW + 3,
        )
        is receipt
    )
    with pytest.raises(ValueError, match="already has a receipt"):
        issuer.issue(
            ended_at=NOW + 3,
            expires_at=NOW + 120,
            backend=BACKEND,
            result=RESULT,
        )


def test_denied_receipt_cannot_replace_authority_cause() -> None:
    connector = _identity(20)
    request = _request(connector, _identity(21), slot=2)
    issuer = BoundReceiptIssuer(
        connector,
        request,
        ClaimResult(False, ConnectorErrorCode.GRANT_REVOKED, None, None),
        started_at=NOW + 1,
        id_factory=_IdFactory(),
    )

    with pytest.raises(ValueError, match="replace"):
        issuer.issue(
            ended_at=NOW + 2,
            expires_at=NOW + 120,
            failure_code=ConnectorErrorCode.GRANT_EXPIRED,
        )
    receipt = issuer.issue(ended_at=NOW + 2, expires_at=NOW + 120)
    assert receipt.decision == "deny"
    assert receipt.outcome == "error"
    assert receipt.failure is not None
    assert receipt.failure.cause_code is ConnectorErrorCode.GRANT_REVOKED
    assert receipt.failure.failure_class == "authority"
    assert receipt.usage is None
    assert receipt.backend is None


def test_accepted_execution_failure_keeps_spent_usage_in_receipt() -> None:
    connector = _identity(20)
    request = _request(connector, _identity(21), slot=3)
    issuer = BoundReceiptIssuer(
        connector,
        request,
        ClaimResult(True, None, 4, 6),
        started_at=NOW + 1,
        id_factory=_IdFactory(),
    )

    receipt = issuer.issue(
        ended_at=NOW + 2,
        expires_at=NOW + 120,
        failure_code=ConnectorErrorCode.BACKEND_UNBOUND,
    )
    assert receipt.decision == "allow"
    assert receipt.outcome == "error"
    assert receipt.failure is not None
    assert receipt.failure.cause_code is ConnectorErrorCode.BACKEND_UNBOUND
    assert receipt.usage is not None
    assert receipt.usage.sequence == 4


def test_receipt_verifier_distinguishes_invalid_context_and_expiry() -> None:
    connector = _identity(20)
    vps = _identity(21)
    request = _request(connector, vps, slot=4)
    receipt = _success_receipt(connector, request)

    tampered = replace(
        receipt, signature=bytes([receipt.signature[0] ^ 1]) + receipt.signature[1:]
    )
    with pytest.raises(ConnectorError) as invalid:
        verify_receipt(
            tampered,
            pinned_connector=connector.public_identity,
            request=request,
            now=NOW + 3,
        )
    _assert_connector_error(invalid, ConnectorErrorCode.RECEIPT_INVALID)

    substituted_request = _request(connector, vps, slot=5)
    with pytest.raises(ConnectorError) as context:
        verify_receipt(
            receipt,
            pinned_connector=connector.public_identity,
            request=substituted_request,
            now=NOW + 3,
        )
    _assert_connector_error(context, ConnectorErrorCode.RECEIPT_CONTEXT_MISMATCH)

    with pytest.raises(ConnectorError) as expired:
        verify_receipt(
            receipt,
            pinned_connector=connector.public_identity,
            request=request,
            now=request.deadline,
        )
    _assert_connector_error(expired, ConnectorErrorCode.RECEIPT_EXPIRED)

    other_connector = _identity(22)
    with pytest.raises(ConnectorError) as wrong_key:
        verify_receipt(
            receipt,
            pinned_connector=other_connector.public_identity,
            request=request,
            now=NOW + 3,
        )
    _assert_connector_error(wrong_key, ConnectorErrorCode.RECEIPT_INVALID)


def test_verified_evidence_is_owner_only_replay_protected_and_payload_free(
    tmp_path: Path,
) -> None:
    connector = _identity(20)
    vps = _identity(21)
    request = _request(connector, vps, slot=6)
    receipt = _success_receipt(connector, request)
    path = tmp_path / "evidence" / "vps-receipts.jsonl"
    ledger = ReceiptEvidenceLedger(path, connector.public_identity, role="vps")

    record = ledger.append_verified(receipt, request=request, now=NOW + 3)
    assert ledger.records() == (record,)
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    for child in path.parent.iterdir():
        assert stat.S_IMODE(child.stat().st_mode) == 0o600
        assert child.stat().st_nlink == 1
    raw = path.read_bytes()
    assert CANARY.encode() not in raw
    assert b"https://" not in raw
    assert b"private" not in raw

    with pytest.raises(ConnectorError) as replay:
        ledger.append_verified(receipt, request=request, now=NOW + 4)
    _assert_connector_error(replay, ConnectorErrorCode.RECEIPT_REPLAYED)


def test_evidence_chain_detects_tampering_and_role_substitution(tmp_path: Path) -> None:
    connector = _identity(20)
    vps = _identity(21)
    factory = _IdFactory()
    path = tmp_path / "evidence" / "connector-receipts.jsonl"
    ledger = ReceiptEvidenceLedger(path, connector.public_identity, role="connector")
    request_one = _request(connector, vps, slot=7)
    request_two = _request(connector, vps, slot=8, issued_at=NOW + 10)
    ledger.append_verified(
        _success_receipt(connector, request_one, factory=factory),
        request=request_one,
        now=NOW + 3,
    )
    ledger.append_verified(
        _success_receipt(connector, request_two, sequence=2, factory=factory),
        request=request_two,
        now=NOW + 13,
    )
    assert len(ledger.records()) == 2

    copied = tmp_path / "evidence-copy" / "vps.jsonl"
    copied.parent.mkdir(mode=0o700)
    copied.write_bytes(path.read_bytes())
    copied.chmod(0o600)
    with pytest.raises(ReceiptEvidenceError, match="hash chain"):
        ReceiptEvidenceLedger(copied, connector.public_identity, role="vps").records()

    payload = bytearray(path.read_bytes())
    payload[-3] = ord("0") if payload[-3] != ord("0") else ord("1")
    path.write_bytes(payload)
    with pytest.raises(ReceiptEvidenceError):
        ledger.records()


def test_evidence_prune_rechains_retained_receipts(tmp_path: Path) -> None:
    connector = _identity(20)
    vps = _identity(21)
    factory = _IdFactory()
    path = tmp_path / "evidence" / "receipts.jsonl"
    ledger = ReceiptEvidenceLedger(path, connector.public_identity, role="vps")
    old_request = _request(connector, vps, slot=9, issued_at=NOW)
    new_time = NOW + AUDIT_RETENTION_SECONDS + 10
    new_request = _request(connector, vps, slot=10, issued_at=new_time)
    ledger.append_verified(
        _success_receipt(connector, old_request, factory=factory),
        request=old_request,
        now=NOW + 3,
    )
    new_receipt = _success_receipt(connector, new_request, sequence=2, factory=factory)
    ledger.append_verified(new_receipt, request=new_request, now=new_time + 3)

    assert ledger.prune(now=new_time + 3) == 1
    records = ledger.records()
    assert len(records) == 1
    assert records[0].receipt.receipt_id == new_receipt.receipt_id
    assert records[0].previous_hash == ""


def test_concurrent_evidence_appends_preserve_one_complete_chain(
    tmp_path: Path,
) -> None:
    connector = _identity(20)
    vps = _identity(21)
    factory = _IdFactory()
    path = tmp_path / "evidence" / "receipts.jsonl"
    ledger = ReceiptEvidenceLedger(path, connector.public_identity, role="vps")
    pairs = []
    for slot in range(20, 32):
        request = _request(connector, vps, slot=slot)
        pairs.append((request, _success_receipt(connector, request, factory=factory)))

    with ThreadPoolExecutor(max_workers=8) as executor:
        records = list(
            executor.map(
                lambda pair: ledger.append_verified(
                    pair[1], request=pair[0], now=NOW + 3
                ),
                pairs,
            )
        )

    loaded = ledger.records()
    assert len(records) == len(pairs)
    assert len(loaded) == len(pairs)
    assert len({record.record_hash for record in loaded}) == len(pairs)


@pytest.mark.parametrize("target", ["ledger", "lock"])
@pytest.mark.parametrize("kind", ["symlink", "hardlink", "wrong-mode", "fifo"])
def test_unsafe_evidence_or_lock_file_fails_closed(
    tmp_path: Path,
    target: str,
    kind: str,
) -> None:
    connector = _identity(20)
    vps = _identity(21)
    request = _request(connector, vps, slot=11)
    receipt = _success_receipt(connector, request)
    path = tmp_path / "evidence" / "receipts.jsonl"
    ledger = ReceiptEvidenceLedger(path, connector.public_identity, role="vps")
    ledger.append_verified(receipt, request=request, now=NOW + 3)
    unsafe = path if target == "ledger" else path.parent / f".{path.name}.lock"

    if kind == "wrong-mode":
        unsafe.chmod(0o640)
    elif kind == "hardlink":
        os.link(unsafe, unsafe.with_name(f"{unsafe.name}.alias"))
    else:
        original = unsafe.with_name(f"{unsafe.name}.original")
        unsafe.rename(original)
        if kind == "symlink":
            unsafe.symlink_to(original.name)
        else:
            os.mkfifo(unsafe, 0o600)

    with pytest.raises(ReceiptEvidenceError):
        ledger.records()
