from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import os
import pickle
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import pytest

import hermes_reach.connector.media_policy as media_policy_module
from hermes_reach.connector.authority import AuthorizedExecution, GrantAuthority
from hermes_reach.connector.errors import ConnectorError, ConnectorErrorCode
from hermes_reach.connector.identity import DevicePrivateIdentity
from hermes_reach.connector.media_policy import (
    FileGrantProposal,
    ModelCleanupPolicy,
    ModelCostPolicy,
    ModelExecutionRequest,
    ModelExecutorBinding,
    ModelIdentity,
    ModelPolicy,
    ModelPolicyRow,
    ModelWorkload,
    ProcessLocalFileGrants,
    VerifiedLocalFile,
    prepare_model_execution,
)
from hermes_reach.connector.protocol import (
    GrantClaims,
    GrantScope,
    OperationResultItemV1,
    OperationResultMediaV1,
    OperationResultV1,
    PairingChallenge,
    PairingInit,
    ProtocolValidationError,
    PublicBackendIdentity,
    SignedRequest,
    create_pairing_challenge,
    create_pairing_init,
    create_signed_grant,
    create_signed_request,
    encode_record,
    pairing_transcript_hash,
    protect_operation_call,
    record_digest,
)
from hermes_reach.connector.store import AuthorityStore, ClaimResult, StoreWriterLease
from hermes_reach.contracts import validate_transcribe

NOW = 1_800_000_000
SOURCE = "youtube"
OPERATION = "transcribe.video"
PRIMARY = ModelIdentity("fixture-provider", "fixture-transcriber-v1")
FALLBACK = ModelIdentity("fixture-fallback", "fixture-transcriber-v2")
CLEANUP = ModelIdentity("fixture-cleanup", "fixture-cleaner-v1")
BACKEND = PublicBackendIdentity("reach-bounded-executor-v1", "1")
WORKLOAD = ModelWorkload(source_bytes=12, duration_seconds=30, chunk_count=2)


def _id(value: int) -> str:
    return base64.b32encode(value.to_bytes(16, "big")).decode().rstrip("=").lower()


def _identity(seed: int) -> DevicePrivateIdentity:
    return DevicePrivateIdentity._from_seed_for_testing(bytes([seed]) * 32)


class _Clock:
    def __init__(self, value: int = NOW) -> None:
        self.value = value

    def __call__(self) -> int:
        return self.value


class _Ids:
    def __init__(self, value: int = 10_000) -> None:
        self.value = value

    def __call__(self) -> str:
        self.value += 1
        return _id(self.value)


class _DeterministicExecutor:
    def __init__(self, expected_file: bytes | None = None) -> None:
        self.expected_file = expected_file
        self.calls = 0

    async def execute(self, request: ModelExecutionRequest) -> OperationResultV1:
        self.calls += 1
        if self.expected_file is None:
            assert request.local_file is None
        else:
            assert request.local_file is not None
            assert request.local_file.read() == self.expected_file
        return OperationResultV1(
            items=(
                OperationResultItemV1(
                    kind="content",
                    text="fixed normalized transcript",
                    media=OperationResultMediaV1(
                        coverage="complete", duration_seconds=30
                    ),
                ),
            ),
            truncated=False,
        )


def _row(
    *,
    media_source_class: str = "connector_local_file",
    maximum_source_bytes: int = 1024,
    maximum_duration_seconds: int = 60,
    maximum_chunks: int = 4,
) -> ModelPolicyRow:
    return ModelPolicyRow(
        source=SOURCE,
        operation=OPERATION,
        media_source_class=media_source_class,  # type: ignore[arg-type]
        primary=PRIMARY,
        maximum_source_bytes=maximum_source_bytes,
        maximum_duration_seconds=maximum_duration_seconds,
        maximum_chunks=maximum_chunks,
        fallbacks=(FALLBACK,),
        cleanup=ModelCleanupPolicy(True, CLEANUP),
        cost=ModelCostPolicy("media_minute", 50_000),
    )


def _policy(
    *,
    revision: int = 1,
    media_source_class: str = "connector_local_file",
) -> ModelPolicy:
    return ModelPolicy(revision, (_row(media_source_class=media_source_class),))


def _protected(source: str = SOURCE, operation: str = OPERATION):  # type: ignore[no-untyped-def]
    target = {
        "url": (
            "https://www.youtube.com/watch?v=abcdefghijk"
            if source == "youtube"
            else "https://example.com/media"
        )
    }
    return protect_operation_call(
        validate_transcribe(
            {"source": source, "operation": operation, "target": target}
        )
    )


def _request(
    *,
    connector: DevicePrivateIdentity,
    vps: DevicePrivateIdentity,
    source: str = SOURCE,
    operation: str = OPERATION,
    grant_revision: int = 1,
    policy_revision: int = 1,
    slot: int = 1,
) -> tuple[SignedRequest, object]:
    protected = _protected(source, operation)
    request = create_signed_request(
        vps,
        message_id=_id(100 + slot),
        request_id=_id(200 + slot),
        trace_id=f"{slot:032x}",
        audience_key_id=connector.public_identity.key_id,
        grant_id=_id(300),
        grant_revision=grant_revision,
        policy_revision=policy_revision,
        source=source,
        operation=operation,
        issued_at=NOW,
        deadline=NOW + 60,
        protected_payload=protected,
    )
    return request, protected


def _execution(
    *,
    connector: DevicePrivateIdentity | None = None,
    vps: DevicePrivateIdentity | None = None,
    source: str = SOURCE,
    operation: str = OPERATION,
    grant_revision: int = 1,
    policy_revision: int = 1,
    slot: int = 1,
    policy: ModelPolicy | None = None,
) -> AuthorizedExecution:
    connector = _identity(10) if connector is None else connector
    vps = _identity(11) if vps is None else vps
    request, protected = _request(
        connector=connector,
        vps=vps,
        source=source,
        operation=operation,
        grant_revision=grant_revision,
        policy_revision=policy_revision,
        slot=slot,
    )
    scope = GrantScope(source, operation, "public")
    effective_policy = _policy(revision=policy_revision) if policy is None else policy
    return AuthorizedExecution(
        request,
        protected,  # type: ignore[arg-type]
        scope,
        ClaimResult(True, None, 1, 9, effective_policy.digest()),
    )


def _binding(
    executor: _DeterministicExecutor,
    *,
    model: ModelIdentity = PRIMARY,
) -> ModelExecutorBinding:
    return ModelExecutorBinding(SOURCE, OPERATION, model, BACKEND, executor)


def _proposal(
    registry: ProcessLocalFileGrants,
    path: Path,
    execution: AuthorizedExecution,
    *,
    expires_at: int | None = None,
) -> FileGrantProposal:
    return registry.propose(
        path,
        subject_key_id=execution.request.subject_key_id,
        source=execution.request.source,
        operation=execution.request.operation,
        grant_revision=execution.request.grant_revision,
        policy_revision=execution.request.policy_revision,
        expires_at=expires_at,
    )


def _approve(
    registry: ProcessLocalFileGrants,
    path: Path,
    execution: AuthorizedExecution,
    connector: DevicePrivateIdentity,
    *,
    expires_at: int | None = None,
) -> FileGrantProposal:
    proposal = _proposal(registry, path, execution, expires_at=expires_at)
    registry.approve(proposal, signer=connector, message_id=_id(400))
    return proposal


def _assert_code(
    caught: pytest.ExceptionInfo[ConnectorError], code: ConnectorErrorCode
) -> None:
    assert caught.value.code == code.value


def test_empty_policy_denies_even_when_exact_executor_and_key_shaped_state_exist() -> (
    None
):
    empty_policy = ModelPolicy.default_deny(1)
    execution = _execution(policy=empty_policy)
    executor = _DeterministicExecutor()

    with pytest.raises(ConnectorError) as denied:
        prepare_model_execution(
            execution,
            policy=empty_policy,
            media_source_class="public_http",
            workload=WORKLOAD,
            bindings=(_binding(executor),),
        )

    _assert_code(denied, ConnectorErrorCode.MODEL_POLICY_DENIED)
    assert executor.calls == 0


def test_policy_derives_primary_fallback_cleanup_cost_and_provenance_exactly() -> None:
    primary_executor = _DeterministicExecutor()
    fallback_executor = _DeterministicExecutor()
    policy = _policy(media_source_class="public_http")
    execution = _execution(policy=policy)

    prepared = prepare_model_execution(
        execution,
        policy=policy,
        media_source_class="public_http",
        workload=WORKLOAD,
        bindings=(
            _binding(fallback_executor, model=FALLBACK),
            _binding(primary_executor),
        ),
    )
    assert prepared.provenance.provider == PRIMARY.provider
    assert prepared.provenance.model == PRIMARY.model
    assert prepared.provenance.fallback_index == 0
    assert prepared.provenance.cleanup_provider == CLEANUP.provider
    assert prepared.provenance.cleanup_model == CLEANUP.model
    assert prepared.provenance.cost_unit == "media_minute"
    assert prepared.provenance.cost_ceiling_microunits == 50_000
    assert (
        asyncio.run(prepared.execute()).items[0].text == "fixed normalized transcript"
    )
    assert primary_executor.calls == 1
    assert fallback_executor.calls == 0

    fallback_only = prepare_model_execution(
        _execution(slot=2, policy=policy),
        policy=policy,
        media_source_class="public_http",
        workload=WORKLOAD,
        bindings=(_binding(fallback_executor, model=FALLBACK),),
    )
    assert fallback_only.provenance.fallback_index == 1
    assert fallback_only.provenance.model == FALLBACK.model


@pytest.mark.parametrize(
    ("policy", "media_source_class", "workload"),
    (
        (_policy(revision=2), "connector_local_file", WORKLOAD),
        (_policy(), "public_http", WORKLOAD),
        (_policy(), "connector_local_file", ModelWorkload(1025, 30, 2)),
        (_policy(), "connector_local_file", ModelWorkload(12, 61, 2)),
        (_policy(), "connector_local_file", ModelWorkload(12, 30, 5)),
    ),
)
def test_policy_denies_revision_source_size_duration_and_chunk_widening(
    policy: ModelPolicy,
    media_source_class: str,
    workload: ModelWorkload,
) -> None:
    with pytest.raises(ConnectorError) as denied:
        policy.authorize(
            _execution(),
            media_source_class=media_source_class,  # type: ignore[arg-type]
            workload=workload,
        )
    _assert_code(denied, ConnectorErrorCode.MODEL_POLICY_DENIED)


def test_policy_is_immutable_unique_and_complete_digest_bound() -> None:
    first = _policy()
    second = ModelPolicy(1, tuple(reversed(first.rows)))
    assert first.digest() == second.digest()
    assert first.digest() != ModelPolicy(2, first.rows).digest()
    with pytest.raises(FrozenInstanceError):
        first.revision = 2  # type: ignore[misc]
    with pytest.raises(ValueError):
        ModelPolicy(1, (first.rows[0], first.rows[0]))
    with pytest.raises(ValueError):
        ModelPolicyRow(
            source=SOURCE,
            operation=OPERATION,
            media_source_class="connector_local_file",
            primary=PRIMARY,
            maximum_source_bytes=1024,
            maximum_duration_seconds=60,
            maximum_chunks=4,
            fallbacks=(PRIMARY,),
            cleanup=ModelCleanupPolicy(False),
            cost=ModelCostPolicy("request", 1),
        )
    with pytest.raises(ValueError):
        ModelCleanupPolicy(False, CLEANUP)


def test_same_revision_policy_substitution_denies_before_binding_selection() -> None:
    authorized = _policy(media_source_class="public_http")
    substituted_model = ModelIdentity("substituted-provider", "other-model-v1")
    substituted = ModelPolicy(
        authorized.revision,
        (
            replace(
                authorized.rows[0],
                primary=substituted_model,
                fallbacks=(),
            ),
        ),
    )
    executor = _DeterministicExecutor()

    with pytest.raises(ConnectorError) as denied:
        prepare_model_execution(
            _execution(policy=authorized),
            policy=substituted,
            media_source_class="public_http",
            workload=WORKLOAD,
            bindings=(_binding(executor, model=substituted_model),),
        )

    _assert_code(denied, ConnectorErrorCode.MODEL_POLICY_DENIED)
    assert executor.calls == 0


def test_file_grant_is_path_free_single_use_and_restart_local(
    tmp_path: Path,
) -> None:
    connector = _identity(10)
    execution = _execution(connector=connector)
    clock = _Clock()
    registry = ProcessLocalFileGrants(clock=clock, id_factory=_Ids())
    canary_directory = tmp_path / "SECRET_PATH_CANARY"
    canary_directory.mkdir()
    media = canary_directory / "episode.wav"
    payload = b"fixture media"
    media.write_bytes(payload)

    proposal = _proposal(registry, media, execution)
    assert proposal.expires_at == NOW + 600
    assert proposal.basename == "episode.wav"
    assert "SECRET_PATH_CANARY" not in repr(proposal)
    grant = registry.approve(proposal, signer=connector, message_id=_id(400))
    encoded = encode_record(grant)
    assert b"SECRET_PATH_CANARY" not in encoded
    assert b"episode.wav" not in encoded
    assert registry.pending_count == 0
    assert registry.active_count == 1

    local_file = registry.consume(grant.file_grant_id, execution)
    assert isinstance(local_file, VerifiedLocalFile)
    assert "SECRET_PATH_CANARY" not in repr(local_file)
    assert local_file.read() == payload
    local_file.close()
    assert registry.active_count == 0
    with pytest.raises(ConnectorError) as replayed:
        registry.consume(grant.file_grant_id, execution)
    _assert_code(replayed, ConnectorErrorCode.FILE_GRANT_INVALID)

    restarted = ProcessLocalFileGrants(clock=clock, id_factory=_Ids(20_000))
    with pytest.raises(ConnectorError) as after_restart:
        restarted.consume(grant.file_grant_id, execution)
    _assert_code(after_restart, ConnectorErrorCode.FILE_GRANT_INVALID)


def test_backend_unbound_and_policy_denial_do_not_touch_file_or_provider_seams(
    tmp_path: Path,
) -> None:
    connector = _identity(10)
    execution = _execution(connector=connector)
    media = tmp_path / "media.wav"
    media.write_bytes(b"fixture media")
    registry = ProcessLocalFileGrants(clock=_Clock(), id_factory=_Ids())
    proposal = _approve(registry, media, execution, connector)

    with pytest.raises(ConnectorError) as unbound:
        prepare_model_execution(
            execution,
            policy=_policy(),
            media_source_class="connector_local_file",
            workload=WORKLOAD,
            bindings=(),
            file_grants=registry,
            file_grant_id=proposal.file_grant_id,
        )
    _assert_code(unbound, ConnectorErrorCode.BACKEND_UNBOUND)
    assert registry.active_count == 1

    with pytest.raises(ConnectorError) as denied:
        prepare_model_execution(
            execution,
            policy=ModelPolicy.default_deny(1),
            media_source_class="connector_local_file",
            workload=WORKLOAD,
            bindings=(_binding(_DeterministicExecutor()),),
            file_grants=registry,
            file_grant_id=proposal.file_grant_id,
        )
    _assert_code(denied, ConnectorErrorCode.MODEL_POLICY_DENIED)
    assert registry.active_count == 1


def test_file_changes_invalidate_proposal_or_one_use_grant(tmp_path: Path) -> None:
    connector = _identity(10)
    execution = _execution(connector=connector)
    registry = ProcessLocalFileGrants(clock=_Clock(), id_factory=_Ids())
    media = tmp_path / "media.wav"
    media.write_bytes(b"first-content")
    proposal = _proposal(registry, media, execution)
    media.write_bytes(b"other-content")

    with pytest.raises(ConnectorError) as changed_before_approval:
        registry.approve(proposal, signer=connector, message_id=_id(400))
    _assert_code(changed_before_approval, ConnectorErrorCode.FILE_CHANGED)
    assert registry.pending_count == 0

    media.write_bytes(b"first-content")
    approved = _approve(registry, media, execution, connector)
    media.write_bytes(b"other-content")
    with pytest.raises(ConnectorError) as changed_after_approval:
        registry.consume(approved.file_grant_id, execution)
    _assert_code(changed_after_approval, ConnectorErrorCode.FILE_CHANGED)
    assert registry.active_count == 0


def test_file_inode_replacement_and_size_change_fail_closed(tmp_path: Path) -> None:
    connector = _identity(10)
    execution = _execution(connector=connector)
    registry = ProcessLocalFileGrants(clock=_Clock(), id_factory=_Ids())
    media = tmp_path / "media.wav"
    media.write_bytes(b"original")
    inode_grant = _approve(registry, media, execution, connector)
    replacement = tmp_path / "replacement.wav"
    replacement.write_bytes(b"original")
    replacement.replace(media)
    with pytest.raises(ConnectorError) as inode_changed:
        registry.consume(inode_grant.file_grant_id, execution)
    _assert_code(inode_changed, ConnectorErrorCode.FILE_CHANGED)

    media.write_bytes(b"original")
    size_grant = _approve(registry, media, execution, connector)
    media.write_bytes(b"original-longer")
    with pytest.raises(ConnectorError) as size_changed:
        registry.consume(size_grant.file_grant_id, execution)
    _assert_code(size_changed, ConnectorErrorCode.FILE_CHANGED)


def test_consumed_file_is_an_immutable_snapshot_of_the_verified_descriptor(
    tmp_path: Path,
) -> None:
    connector = _identity(10)
    execution = _execution(connector=connector)
    registry = ProcessLocalFileGrants(clock=_Clock(), id_factory=_Ids())
    media = tmp_path / "media.wav"
    approved_content = b"approved fixture media"
    media.write_bytes(approved_content)
    proposal = _approve(registry, media, execution, connector)

    local_file = registry.consume(proposal.file_grant_id, execution)
    media.write_bytes(b"mutated after consume and before executor read")

    assert local_file.size == len(approved_content)
    assert local_file.digest == hashlib.sha256(approved_content).hexdigest()
    assert local_file.read() == approved_content
    local_file.close()


def test_file_paths_reject_traversal_symlinks_special_and_empty_files(
    tmp_path: Path,
) -> None:
    execution = _execution()
    registry = ProcessLocalFileGrants(clock=_Clock(), id_factory=_Ids())
    real_directory = tmp_path / "real"
    real_directory.mkdir()
    media = real_directory / "media.wav"
    media.write_bytes(b"media")
    final_symlink = tmp_path / "final-link.wav"
    final_symlink.symlink_to(media)
    directory_symlink = tmp_path / "directory-link"
    directory_symlink.symlink_to(real_directory, target_is_directory=True)
    empty = tmp_path / "empty.wav"
    empty.touch()
    fifo = tmp_path / "media.fifo"
    os.mkfifo(fifo)

    invalid_paths = (
        Path("relative.wav"),
        Path("https://example.com/media.wav"),
        tmp_path / ".." / tmp_path.name / "real" / "media.wav",
        final_symlink,
        directory_symlink / "media.wav",
        real_directory,
        empty,
        fifo,
    )
    for invalid in invalid_paths:
        with pytest.raises(ConnectorError) as denied:
            _proposal(registry, invalid, execution)
        _assert_code(denied, ConnectorErrorCode.FILE_GRANT_INVALID)
    assert registry.pending_count == 0


@pytest.mark.parametrize(
    "basename",
    (
        "terminal\x7fspoof.wav",
        "terminal\x85spoof.wav",
        "terminal\u202espoof.wav",
        "terminal\nspoof.wav",
        "media-\u5a92\u4f53.wav",
    ),
)
def test_file_approval_rejects_terminal_unsafe_basenames(
    tmp_path: Path, basename: str
) -> None:
    execution = _execution()
    registry = ProcessLocalFileGrants(clock=_Clock(), id_factory=_Ids())
    media = tmp_path / basename
    media.write_bytes(b"fixture media")

    with pytest.raises(ConnectorError) as denied:
        _proposal(registry, media, execution)

    _assert_code(denied, ConnectorErrorCode.FILE_GRANT_INVALID)
    assert registry.pending_count == 0


def test_file_grant_rejects_expiry_context_mismatch_and_replay(tmp_path: Path) -> None:
    connector = _identity(10)
    vps = _identity(11)
    media = tmp_path / "media.wav"
    media.write_bytes(b"fixture media")

    cases = (
        _execution(connector=connector, vps=_identity(12), slot=2),
        _execution(connector=connector, vps=vps, grant_revision=2, slot=3),
        _execution(connector=connector, vps=vps, policy_revision=2, slot=4),
    )
    for index, wrong_execution in enumerate(cases):
        clock = _Clock()
        registry = ProcessLocalFileGrants(clock=clock, id_factory=_Ids(30_000 + index))
        correct = _execution(connector=connector, vps=vps, slot=10 + index)
        proposal = _approve(registry, media, correct, connector)
        with pytest.raises(ConnectorError) as denied:
            registry.consume(proposal.file_grant_id, wrong_execution)
        _assert_code(denied, ConnectorErrorCode.FILE_GRANT_INVALID)
        assert registry.active_count == 0

    clock = _Clock()
    registry = ProcessLocalFileGrants(clock=clock, id_factory=_Ids(40_000))
    correct = _execution(connector=connector, vps=vps, slot=20)
    proposal = _approve(registry, media, correct, connector, expires_at=NOW + 1)
    clock.value = NOW + 1
    with pytest.raises(ConnectorError) as expired:
        registry.consume(proposal.file_grant_id, correct)
    _assert_code(expired, ConnectorErrorCode.FILE_GRANT_INVALID)
    assert registry.active_count == 0


def test_file_grant_ttl_bounds_and_descriptor_handle_are_nonserializable(
    tmp_path: Path,
) -> None:
    connector = _identity(10)
    execution = _execution(connector=connector)
    media = tmp_path / "media.wav"
    media.write_bytes(b"fixture media")
    registry = ProcessLocalFileGrants(clock=_Clock(), id_factory=_Ids())

    with pytest.raises(ConnectorError) as too_long:
        _proposal(registry, media, execution, expires_at=NOW + 1801)
    _assert_code(too_long, ConnectorErrorCode.FILE_GRANT_INVALID)
    proposal = _approve(registry, media, execution, connector)
    local_file = registry.consume(proposal.file_grant_id, execution)
    with pytest.raises(TypeError):
        pickle.dumps(local_file)
    with pytest.raises(TypeError):
        copy.copy(local_file)
    with pytest.raises(TypeError):
        copy.deepcopy(local_file)
    local_file.close()


def test_clear_is_a_terminal_barrier_against_inflight_propose_and_approve(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    connector = _identity(10)
    execution = _execution(connector=connector)
    media = tmp_path / "media.wav"
    media.write_bytes(b"fixture media")
    original = media_policy_module._open_hashed_file

    propose_registry = ProcessLocalFileGrants(clock=_Clock(), id_factory=_Ids())
    propose_hashed = threading.Event()
    release_propose = threading.Event()

    def blocked_propose(*args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        result = original(*args, **kwargs)  # type: ignore[arg-type]
        propose_hashed.set()
        assert release_propose.wait(5)
        return result

    monkeypatch.setattr(media_policy_module, "_open_hashed_file", blocked_propose)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(_proposal, propose_registry, media, execution)
        assert propose_hashed.wait(5)
        propose_registry.clear()
        release_propose.set()
        with pytest.raises(ConnectorError) as closed_propose:
            future.result(timeout=5)
    _assert_code(closed_propose, ConnectorErrorCode.FILE_GRANT_INVALID)
    assert propose_registry.pending_count == 0

    monkeypatch.setattr(media_policy_module, "_open_hashed_file", original)
    approve_registry = ProcessLocalFileGrants(clock=_Clock(), id_factory=_Ids(20_000))
    proposal = _proposal(approve_registry, media, execution)
    approve_hashed = threading.Event()
    release_approve = threading.Event()

    def blocked_approve(*args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        result = original(*args, **kwargs)  # type: ignore[arg-type]
        approve_hashed.set()
        assert release_approve.wait(5)
        return result

    monkeypatch.setattr(media_policy_module, "_open_hashed_file", blocked_approve)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            approve_registry.approve,
            proposal,
            signer=connector,
            message_id=_id(401),
        )
        assert approve_hashed.wait(5)
        approve_registry.clear()
        release_approve.set()
        with pytest.raises(ConnectorError) as closed_approve:
            future.result(timeout=5)
    _assert_code(closed_approve, ConnectorErrorCode.FILE_GRANT_INVALID)
    assert approve_registry.pending_count == 0
    assert approve_registry.active_count == 0


def _challenge(
    connector: DevicePrivateIdentity, pairing: PairingInit
) -> PairingChallenge:
    return create_pairing_challenge(
        signer=connector,
        message_id=_id(600),
        pairing_id=pairing.pairing_id,
        init_digest=record_digest(pairing),
        vps_key_id=pairing.vps_key_id,
        connector_nonce=bytes(range(32, 64)),
        tls_ca_der=b"media-policy-test-ca",
        tls_leaf_fingerprint=hashlib.sha256(b"media-policy-leaf").hexdigest(),
        issued_at=NOW + 1,
        deadline=pairing.deadline,
    )


def test_deterministic_file_executor_runs_after_complete_authority_claim(
    tmp_path: Path,
) -> None:
    connector = _identity(10)
    vps = _identity(11)
    scope = GrantScope(SOURCE, OPERATION, "public")
    policy = _policy()
    state = tmp_path / "state"
    lease = StoreWriterLease(state)
    store = AuthorityStore.initialize(
        state,
        connector.public_identity,
        lease,
        initial_policy_digest=policy.digest(),
        now=NOW,
    )
    pairing = create_pairing_init(
        vps,
        message_id=_id(601),
        pairing_id=_id(602),
        device_label="media-policy-vps",
        endpoint_digest=hashlib.sha256(b"endpoint").hexdigest(),
        vps_nonce=bytes(range(32)),
        requested_scopes=(scope,),
        grant_expires_at=NOW + 3600,
        grant_max_uses=5,
        issued_at=NOW,
        deadline=NOW + 300,
    )
    challenge = _challenge(connector, pairing)
    grant_id = _id(603)
    grant = create_signed_grant(
        connector,
        message_id=_id(604),
        claims=GrantClaims(
            grant_id=grant_id,
            revision=1,
            issuer_key_id=connector.public_identity.key_id,
            subject_key_id=vps.public_identity.key_id,
            issued_at=NOW + 1,
            not_before=NOW + 1,
            expires_at=NOW + 3600,
            policy_revision=1,
            max_uses=5,
            scopes=(scope,),
        ),
    )
    store.begin_pairing(pairing, challenge, now=NOW)
    store.approve_pairing(
        pairing.pairing_id,
        device_id=_id(605),
        transcript_digest=pairing_transcript_hash(
            encode_record(pairing),
            encode_record(challenge),
            observed_tls_leaf_fingerprint=challenge.tls_leaf_fingerprint,
        ).hex(),
        grant=grant,
        now=NOW + 2,
    )
    authority_clock = _Clock(NOW + 11)
    authority = GrantAuthority(store, id_factory=_Ids(50_000), clock=authority_clock)
    authority._activate_from_service(connector)
    protected = _protected()
    request = create_signed_request(
        vps,
        message_id=_id(606),
        request_id=_id(607),
        trace_id="6" * 32,
        audience_key_id=connector.public_identity.key_id,
        grant_id=grant_id,
        grant_revision=1,
        policy_revision=1,
        source=SOURCE,
        operation=OPERATION,
        issued_at=NOW + 10,
        deadline=NOW + 60,
        protected_payload=protected,
    )
    media = tmp_path / "fixture.wav"
    payload = b"fixture media"
    media.write_bytes(payload)
    file_registry = ProcessLocalFileGrants(
        clock=authority_clock, id_factory=_Ids(60_000)
    )
    pre_execution = AuthorizedExecution(
        request,
        protected,
        scope,
        ClaimResult(True, None, 1, 4, policy.digest()),
    )
    proposal = _approve(file_registry, media, pre_execution, connector)
    executor = _DeterministicExecutor(payload)

    try:
        decision = authority.authorize_and_handoff(
            request,
            protected,
            scope,
            now=NOW + 11,
            handoff=lambda accepted: prepare_model_execution(
                accepted,
                policy=policy,
                media_source_class="connector_local_file",
                workload=WORKLOAD,
                bindings=(_binding(executor),),
                file_grants=file_registry,
                file_grant_id=proposal.file_grant_id,
            ),
        )
        assert decision.accepted
        assert decision.claim.use_sequence == 1
        assert decision.handoff_result is not None
        result = asyncio.run(decision.handoff_result.execute())
        assert result.items[0].text == "fixed normalized transcript"
        assert executor.calls == 1
        assert file_registry.active_count == 0
        assert store.inspect_grants()[0].used_count == 1
    finally:
        lease.close()


def test_production_module_contains_no_fixture_or_live_backend_implementation() -> None:
    module = Path("src/hermes_reach/connector/media_policy.py").read_text(
        encoding="utf-8"
    )
    forbidden_words = (
        "openai",
        "whisper",
        "groq",
        "exa",
        "agent_reach",
        "subprocess",
        "requests.",
        "httpx",
    )
    assert "class deterministic" not in module.lower()
    assert all(
        re.search(rf"\b{re.escape(value)}\b", module, re.IGNORECASE) is None
        for value in forbidden_words
    )
    assert "class ConnectorModelExecutor(Protocol)" in module


def test_malformed_executor_result_is_rejected_and_file_is_closed(
    tmp_path: Path,
) -> None:
    class _MalformedExecutor:
        async def execute(self, request: ModelExecutionRequest) -> OperationResultV1:
            assert request.local_file is not None
            return OperationResultV1(
                tuple(
                    OperationResultItemV1(kind="content", text="x") for _ in range(21)
                ),
                False,
            )

    connector = _identity(10)
    execution = _execution(connector=connector)
    media = tmp_path / "media.wav"
    media.write_bytes(b"fixture media")
    registry = ProcessLocalFileGrants(clock=_Clock(), id_factory=_Ids())
    proposal = _approve(registry, media, execution, connector)
    binding = ModelExecutorBinding(
        SOURCE, OPERATION, PRIMARY, BACKEND, _MalformedExecutor()
    )
    prepared = prepare_model_execution(
        execution,
        policy=_policy(),
        media_source_class="connector_local_file",
        workload=WORKLOAD,
        bindings=(binding,),
        file_grants=registry,
        file_grant_id=proposal.file_grant_id,
    )
    with pytest.raises(ProtocolValidationError):
        asyncio.run(prepared.execute())
    with pytest.raises(ConnectorError) as second_run:
        asyncio.run(prepared.execute())
    _assert_code(second_run, ConnectorErrorCode.BACKEND_UNBOUND)
