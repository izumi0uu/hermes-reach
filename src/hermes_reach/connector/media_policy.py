"""Default-deny Connector model policy and process-local file grants."""

from __future__ import annotations

import base64
import hashlib
import os
import re
import stat
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO, Final, Literal, Never, Protocol, cast

from ..catalog import get_operation, get_source
from .authority import AuthorizedExecution
from .errors import ConnectorError, ConnectorErrorCode
from .identity import DevicePrivateIdentity, DevicePublicIdentity
from .limits import (
    DEFAULT_FILE_GRANT_TTL_SECONDS,
    ID_BASE32_LENGTH,
    ID_ENTROPY_BYTES,
    KEY_ID_BASE32_LENGTH,
    MAX_FILE_BYTES,
    MAX_FILE_GRANT_TTL_SECONDS,
)
from .protocol import (
    FileGrant,
    OperationResultV1,
    ProtocolValidationError,
    PublicBackendIdentity,
    canonical_json_bytes,
    create_file_grant,
    verify_file_grant,
)

MediaSourceClass = Literal["public_http", "reach_resource_ref", "connector_local_file"]
CostUnit = Literal["request", "media_minute", "media_hour", "chunk"]

_MEDIA_SOURCE_CLASSES: Final = frozenset(
    {"public_http", "reach_resource_ref", "connector_local_file"}
)
_COST_UNITS: Final = frozenset({"request", "media_minute", "media_hour", "chunk"})
_METADATA_ID: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}\Z")
_KEY_ID: Final = re.compile(rf"[a-z2-7]{{{KEY_ID_BASE32_LENGTH}}}\Z")
_OPAQUE_ID: Final = re.compile(rf"[a-z2-7]{{{ID_BASE32_LENGTH}}}\Z")
_SHA256: Final = re.compile(r"[0-9a-f]{64}\Z")
_MAX_POLICY_ROWS: Final = 128
_MAX_MODEL_FALLBACKS: Final = 8
_MAX_POLICY_COUNTER: Final = (1 << 53) - 1
_HASH_CHUNK_BYTES: Final = 1024 * 1024

WallClock = Callable[[], int]
IdFactory = Callable[[], str]


@dataclass(frozen=True, slots=True)
class ModelIdentity:
    """One exact provider/model identity from trusted local policy."""

    provider: str
    model: str

    def __post_init__(self) -> None:
        if (
            type(self.provider) is not str
            or type(self.model) is not str
            or _METADATA_ID.fullmatch(self.provider) is None
            or _METADATA_ID.fullmatch(self.model) is None
        ):
            raise ValueError("The model identity is invalid.")


@dataclass(frozen=True, slots=True)
class ModelCleanupPolicy:
    """Exact optional cleanup consent; omission means cleanup is forbidden."""

    allowed: bool
    model: ModelIdentity | None = None

    def __post_init__(self) -> None:
        if type(self.allowed) is not bool or self.allowed != (self.model is not None):
            raise ValueError("The model cleanup policy is invalid.")


@dataclass(frozen=True, slots=True)
class ModelCostPolicy:
    """Integer-only cost ceiling metadata for one policy row."""

    unit: CostUnit
    ceiling_microunits: int

    def __post_init__(self) -> None:
        if (
            type(self.unit) is not str
            or self.unit not in _COST_UNITS
            or type(self.ceiling_microunits) is not int
            or not 0 < self.ceiling_microunits <= _MAX_POLICY_COUNTER
        ):
            raise ValueError("The model cost policy is invalid.")


@dataclass(frozen=True, slots=True)
class ModelPolicyRow:
    """One immutable exact transcription consent row."""

    source: str
    operation: str
    media_source_class: MediaSourceClass
    primary: ModelIdentity
    maximum_source_bytes: int
    maximum_duration_seconds: int
    maximum_chunks: int
    fallbacks: tuple[ModelIdentity, ...]
    cleanup: ModelCleanupPolicy
    cost: ModelCostPolicy

    def __post_init__(self) -> None:
        source = get_source(self.source)
        operation = (
            get_operation(source, self.operation) if source is not None else None
        )
        if operation is None or operation.tool != "transcribe":
            raise ValueError("The model policy source-operation is invalid.")
        if (
            type(self.media_source_class) is not str
            or self.media_source_class not in _MEDIA_SOURCE_CLASSES
        ):
            raise ValueError("The model policy media source is invalid.")
        if not isinstance(self.primary, ModelIdentity):
            raise ValueError("The model policy primary identity is invalid.")
        if (
            type(self.maximum_source_bytes) is not int
            or not 0 < self.maximum_source_bytes <= MAX_FILE_BYTES
            or type(self.maximum_duration_seconds) is not int
            or not 0 < self.maximum_duration_seconds <= _MAX_POLICY_COUNTER
            or type(self.maximum_chunks) is not int
            or not 0 < self.maximum_chunks <= _MAX_POLICY_COUNTER
            or type(self.fallbacks) is not tuple
            or len(self.fallbacks) > _MAX_MODEL_FALLBACKS
            or not all(isinstance(item, ModelIdentity) for item in self.fallbacks)
            or not isinstance(self.cleanup, ModelCleanupPolicy)
            or not isinstance(self.cost, ModelCostPolicy)
        ):
            raise ValueError("The model policy bounds are invalid.")
        models = (self.primary, *self.fallbacks)
        if len(set(models)) != len(models):
            raise ValueError("The model fallback order contains a duplicate.")

    @property
    def models_in_order(self) -> tuple[ModelIdentity, ...]:
        return (self.primary, *self.fallbacks)


@dataclass(frozen=True, slots=True)
class ModelWorkload:
    """Trusted media measurements checked against an exact policy row."""

    source_bytes: int
    duration_seconds: int
    chunk_count: int

    def __post_init__(self) -> None:
        for value in (self.source_bytes, self.duration_seconds, self.chunk_count):
            if type(value) is not int or not 0 < value <= _MAX_POLICY_COUNTER:
                raise ValueError("The model workload bounds are invalid.")


@dataclass(frozen=True, slots=True)
class ModelProvenance:
    """Closed safe metadata derived only from trusted policy and composition."""

    policy_revision: int
    provider: str
    model: str
    fallback_index: int
    cleanup_provider: str | None
    cleanup_model: str | None
    cost_unit: CostUnit
    cost_ceiling_microunits: int


@dataclass(frozen=True, slots=True)
class ModelExecutionPlan:
    """An exact policy decision with no provider choice from the request."""

    policy_revision: int
    row: ModelPolicyRow
    workload: ModelWorkload
    selected_model: ModelIdentity
    fallback_index: int

    @property
    def provenance(self) -> ModelProvenance:
        cleanup = self.row.cleanup.model
        return ModelProvenance(
            policy_revision=self.policy_revision,
            provider=self.selected_model.provider,
            model=self.selected_model.model,
            fallback_index=self.fallback_index,
            cleanup_provider=None if cleanup is None else cleanup.provider,
            cleanup_model=None if cleanup is None else cleanup.model,
            cost_unit=self.row.cost.unit,
            cost_ceiling_microunits=self.row.cost.ceiling_microunits,
        )


@dataclass(frozen=True, slots=True)
class ModelPolicy:
    """One immutable revision; an empty row set authorizes nothing."""

    revision: int
    rows: tuple[ModelPolicyRow, ...] = ()

    def __post_init__(self) -> None:
        if (
            type(self.revision) is not int
            or self.revision <= 0
            or type(self.rows) is not tuple
            or len(self.rows) > _MAX_POLICY_ROWS
            or not all(isinstance(row, ModelPolicyRow) for row in self.rows)
        ):
            raise ValueError("The model policy revision is invalid.")
        keys = tuple(
            (row.source, row.operation, row.media_source_class) for row in self.rows
        )
        if len(set(keys)) != len(keys):
            raise ValueError("The model policy contains a duplicate exact row.")

    @classmethod
    def default_deny(cls, revision: int) -> ModelPolicy:
        return cls(revision=revision)

    def digest(self) -> str:
        """Hash the complete canonical policy without persisting model state."""

        rows = []
        for row in sorted(
            self.rows,
            key=lambda item: (
                item.source,
                item.operation,
                item.media_source_class,
            ),
        ):
            cleanup = row.cleanup.model
            rows.append(
                {
                    "cleanup": (
                        None
                        if cleanup is None
                        else {"model": cleanup.model, "provider": cleanup.provider}
                    ),
                    "cost": {
                        "ceiling_microunits": row.cost.ceiling_microunits,
                        "unit": row.cost.unit,
                    },
                    "fallbacks": [
                        {"model": item.model, "provider": item.provider}
                        for item in row.fallbacks
                    ],
                    "maximum_chunks": row.maximum_chunks,
                    "maximum_duration_seconds": row.maximum_duration_seconds,
                    "maximum_source_bytes": row.maximum_source_bytes,
                    "media_source_class": row.media_source_class,
                    "operation": row.operation,
                    "primary": {
                        "model": row.primary.model,
                        "provider": row.primary.provider,
                    },
                    "source": row.source,
                }
            )
        payload = {"revision": self.revision, "rows": rows, "version": "v1"}
        return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()

    def require_row(
        self,
        *,
        source: str,
        operation: str,
        media_source_class: MediaSourceClass,
    ) -> ModelPolicyRow:
        """Select only by catalog operation and trusted media classification."""

        for row in self.rows:
            if (
                row.source == source
                and row.operation == operation
                and row.media_source_class == media_source_class
            ):
                return row
        raise ConnectorError(ConnectorErrorCode.MODEL_POLICY_DENIED)

    def authorize(
        self,
        execution: AuthorizedExecution,
        *,
        media_source_class: MediaSourceClass,
        workload: ModelWorkload,
    ) -> ModelPolicyRow:
        """Apply revision and all media bounds after live authority accepted."""

        if not isinstance(execution, AuthorizedExecution) or not isinstance(
            workload, ModelWorkload
        ):
            raise TypeError("The model authorization inputs are invalid.")
        if execution.request.policy_revision != self.revision:
            raise ConnectorError(ConnectorErrorCode.MODEL_POLICY_DENIED)
        if execution.policy_digest != self.digest():
            raise ConnectorError(ConnectorErrorCode.MODEL_POLICY_DENIED)
        row = self.require_row(
            source=execution.request.source,
            operation=execution.request.operation,
            media_source_class=media_source_class,
        )
        if (
            workload.source_bytes > row.maximum_source_bytes
            or workload.duration_seconds > row.maximum_duration_seconds
            or workload.chunk_count > row.maximum_chunks
        ):
            raise ConnectorError(ConnectorErrorCode.MODEL_POLICY_DENIED)
        return row


class ConnectorModelExecutor(Protocol):
    """Exact injected executor interface; production provides no implementation."""

    async def execute(self, request: ModelExecutionRequest) -> OperationResultV1: ...


@dataclass(frozen=True, slots=True)
class ModelExecutorBinding:
    """One exact composition-owned model binding."""

    source: str
    operation: str
    model: ModelIdentity
    backend: PublicBackendIdentity
    executor: ConnectorModelExecutor = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        source = get_source(self.source)
        operation = (
            get_operation(source, self.operation) if source is not None else None
        )
        if (
            operation is None
            or operation.tool != "transcribe"
            or not isinstance(self.model, ModelIdentity)
            or not isinstance(self.backend, PublicBackendIdentity)
            or not callable(getattr(self.executor, "execute", None))
        ):
            raise ValueError("The model executor binding is invalid.")


@dataclass(frozen=True, slots=True, repr=False)
class FileGrantProposal:
    """Approval-safe metadata; its trusted path remains in the registry only."""

    file_grant_id: str
    subject_key_id: str
    basename: str
    digest: str
    size: int
    source: str
    operation: str
    grant_revision: int
    policy_revision: int
    issued_at: int
    expires_at: int

    def __post_init__(self) -> None:
        _validate_file_context(
            subject_key_id=self.subject_key_id,
            source=self.source,
            operation=self.operation,
            grant_revision=self.grant_revision,
            policy_revision=self.policy_revision,
        )
        if (
            type(self.file_grant_id) is not str
            or _OPAQUE_ID.fullmatch(self.file_grant_id) is None
            or type(self.basename) is not str
            or not self.basename
            or len(self.basename) > 255
            or self.basename in {".", ".."}
            or not all(0x20 <= ord(character) <= 0x7E for character in self.basename)
            or "/" in self.basename
            or type(self.digest) is not str
            or _SHA256.fullmatch(self.digest) is None
            or type(self.size) is not int
            or not 0 < self.size <= MAX_FILE_BYTES
            or type(self.issued_at) is not int
            or type(self.expires_at) is not int
            or not self.issued_at < self.expires_at
            or self.expires_at - self.issued_at > MAX_FILE_GRANT_TTL_SECONDS
        ):
            raise ValueError("The file proposal bounds are invalid.")

    def __repr__(self) -> str:
        return "FileGrantProposal(<redacted>)"


@dataclass(frozen=True, slots=True, repr=False)
class _FileSnapshot:
    path: Path = field(repr=False)
    device: int
    inode: int
    size: int
    modified_ns: int
    changed_ns: int
    digest: str


@dataclass(frozen=True, slots=True, repr=False)
class _PendingFile:
    proposal: FileGrantProposal
    snapshot: _FileSnapshot = field(repr=False)


@dataclass(frozen=True, slots=True, repr=False)
class _GrantedMapping:
    grant: FileGrant
    connector_identity: DevicePublicIdentity
    snapshot: _FileSnapshot = field(repr=False)


class VerifiedLocalFile:
    """A path-free handle to the exact descriptor revalidated before execution."""

    __slots__ = ("_stream", "digest", "size")

    def __init__(self, stream: BinaryIO, *, digest: str, size: int) -> None:
        self._stream = stream
        self.digest = digest
        self.size = size

    @property
    def closed(self) -> bool:
        return self._stream.closed

    def read(self, size: int = -1) -> bytes:
        if type(size) is not int or size < -1:
            raise ValueError("The file read bound is invalid.")
        return self._stream.read(size)

    def rewind(self) -> None:
        self._stream.seek(0)

    def close(self) -> None:
        self._stream.close()

    def __enter__(self) -> VerifiedLocalFile:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def __repr__(self) -> str:
        return "VerifiedLocalFile(<redacted>)"

    def __reduce__(self) -> Never:
        raise TypeError("Verified local files cannot be serialized.")

    def __copy__(self) -> Never:
        raise TypeError("Verified local files cannot be copied.")

    def __deepcopy__(self, _: object) -> Never:
        raise TypeError("Verified local files cannot be copied.")


class ProcessLocalFileGrants:
    """Single-process proposal and one-use file-grant registry."""

    def __init__(
        self,
        *,
        clock: WallClock = lambda: int(time.time()),
        id_factory: IdFactory = lambda: _new_id(),
    ) -> None:
        if not callable(clock) or not callable(id_factory):
            raise TypeError("The file-grant registry dependencies are invalid.")
        self._clock = clock
        self._id_factory = id_factory
        self._pending: dict[str, _PendingFile] = {}
        self._granted: dict[str, _GrantedMapping] = {}
        self._mutex = threading.RLock()
        self._generation = 0
        self._closed = False

    @property
    def pending_count(self) -> int:
        with self._mutex:
            return len(self._pending)

    @property
    def active_count(self) -> int:
        with self._mutex:
            return len(self._granted)

    def propose(
        self,
        path: Path,
        *,
        subject_key_id: str,
        source: str,
        operation: str,
        grant_revision: int,
        policy_revision: int,
        expires_at: int | None = None,
        maximum_bytes: int = MAX_FILE_BYTES,
    ) -> FileGrantProposal:
        """Hash one local descriptor and retain its raw path only in memory."""

        _validate_file_context(
            subject_key_id=subject_key_id,
            source=source,
            operation=operation,
            grant_revision=grant_revision,
            policy_revision=policy_revision,
        )
        with self._mutex:
            if self._closed:
                raise ConnectorError(ConnectorErrorCode.FILE_GRANT_INVALID)
            generation = self._generation
        if type(maximum_bytes) is not int or not 0 < maximum_bytes <= MAX_FILE_BYTES:
            raise ConnectorError(ConnectorErrorCode.FILE_GRANT_INVALID)
        now = _timestamp(self._clock)
        effective_expiry = (
            now + DEFAULT_FILE_GRANT_TTL_SECONDS if expires_at is None else expires_at
        )
        if (
            type(effective_expiry) is not int
            or not now < effective_expiry
            or effective_expiry - now > MAX_FILE_GRANT_TTL_SECONDS
        ):
            raise ConnectorError(ConnectorErrorCode.FILE_GRANT_INVALID)
        snapshot, stream = _open_hashed_file(path)
        stream.close()
        if snapshot.size > maximum_bytes:
            raise ConnectorError(ConnectorErrorCode.FILE_GRANT_INVALID)
        file_grant_id = self._id_factory()
        if (
            type(file_grant_id) is not str
            or _OPAQUE_ID.fullmatch(file_grant_id) is None
        ):
            raise ConnectorError(ConnectorErrorCode.FILE_GRANT_INVALID)
        try:
            proposal = FileGrantProposal(
                file_grant_id=file_grant_id,
                subject_key_id=subject_key_id,
                basename=snapshot.path.name,
                digest=snapshot.digest,
                size=snapshot.size,
                source=source,
                operation=operation,
                grant_revision=grant_revision,
                policy_revision=policy_revision,
                issued_at=now,
                expires_at=effective_expiry,
            )
        except (TypeError, ValueError):
            raise ConnectorError(ConnectorErrorCode.FILE_GRANT_INVALID) from None
        with self._mutex:
            if (
                self._closed
                or generation != self._generation
                or file_grant_id in self._pending
                or file_grant_id in self._granted
            ):
                raise ConnectorError(ConnectorErrorCode.FILE_GRANT_INVALID)
            self._pending[file_grant_id] = _PendingFile(proposal, snapshot)
        return proposal

    def discard(self, proposal: FileGrantProposal) -> None:
        if not isinstance(proposal, FileGrantProposal):
            raise TypeError("The file proposal is invalid.")
        with self._mutex:
            current = self._pending.get(proposal.file_grant_id)
            if current is None or current.proposal != proposal:
                raise ConnectorError(ConnectorErrorCode.FILE_GRANT_INVALID)
            del self._pending[proposal.file_grant_id]

    def approve(
        self,
        proposal: FileGrantProposal,
        *,
        signer: DevicePrivateIdentity,
        message_id: str,
    ) -> FileGrant:
        """Consume one proposal, revalidate it, then mint the signed grant."""

        if not isinstance(proposal, FileGrantProposal) or not isinstance(
            signer, DevicePrivateIdentity
        ):
            raise TypeError("The file approval inputs are invalid.")
        with self._mutex:
            if self._closed:
                raise ConnectorError(ConnectorErrorCode.FILE_GRANT_INVALID)
            generation = self._generation
            pending = self._pending.pop(proposal.file_grant_id, None)
        if pending is None or pending.proposal != proposal:
            raise ConnectorError(ConnectorErrorCode.FILE_GRANT_INVALID)
        now = _timestamp(self._clock)
        if now >= proposal.expires_at:
            raise ConnectorError(ConnectorErrorCode.FILE_GRANT_INVALID)
        current, stream = _open_hashed_file(pending.snapshot.path, changed=True)
        stream.close()
        if not _same_snapshot(pending.snapshot, current):
            raise ConnectorError(ConnectorErrorCode.FILE_CHANGED)
        try:
            grant = create_file_grant(
                signer,
                message_id=message_id,
                file_grant_id=proposal.file_grant_id,
                subject_key_id=proposal.subject_key_id,
                digest=proposal.digest,
                size=proposal.size,
                source=proposal.source,
                operation=proposal.operation,
                grant_revision=proposal.grant_revision,
                policy_revision=proposal.policy_revision,
                issued_at=proposal.issued_at,
                expires_at=proposal.expires_at,
            )
            verify_file_grant(
                grant,
                pinned_connector=signer.public_identity,
                expected_subject_key_id=proposal.subject_key_id,
                expected_grant_revision=proposal.grant_revision,
                expected_policy_revision=proposal.policy_revision,
                expected_source=proposal.source,
                expected_operation=proposal.operation,
                now=now,
            )
        except (ProtocolValidationError, TypeError, ValueError):
            raise ConnectorError(ConnectorErrorCode.FILE_GRANT_INVALID) from None
        mapping = _GrantedMapping(grant, signer.public_identity, current)
        with self._mutex:
            if (
                self._closed
                or generation != self._generation
                or grant.file_grant_id in self._granted
            ):
                raise ConnectorError(ConnectorErrorCode.FILE_GRANT_INVALID)
            self._granted[grant.file_grant_id] = mapping
        return grant

    def consume(
        self,
        file_grant_id: str,
        execution: AuthorizedExecution,
    ) -> VerifiedLocalFile:
        """Spend one mapping before revalidation and return that exact descriptor."""

        if (
            type(file_grant_id) is not str
            or _OPAQUE_ID.fullmatch(file_grant_id) is None
            or not isinstance(execution, AuthorizedExecution)
        ):
            raise ConnectorError(ConnectorErrorCode.FILE_GRANT_INVALID)
        with self._mutex:
            if self._closed:
                raise ConnectorError(ConnectorErrorCode.FILE_GRANT_INVALID)
            mapping = self._granted.pop(file_grant_id, None)
        if mapping is None or mapping.grant.file_grant_id != file_grant_id:
            raise ConnectorError(ConnectorErrorCode.FILE_GRANT_INVALID)
        grant = mapping.grant
        try:
            verify_file_grant(
                grant,
                pinned_connector=mapping.connector_identity,
                expected_subject_key_id=execution.request.subject_key_id,
                expected_grant_revision=execution.request.grant_revision,
                expected_policy_revision=execution.request.policy_revision,
                expected_source=execution.request.source,
                expected_operation=execution.request.operation,
                now=_timestamp(self._clock),
            )
        except (ProtocolValidationError, TypeError, ValueError):
            raise ConnectorError(ConnectorErrorCode.FILE_GRANT_INVALID) from None
        current, stream = _open_hashed_file(
            mapping.snapshot.path, changed=True, immutable=True
        )
        if not _same_snapshot(mapping.snapshot, current):
            stream.close()
            raise ConnectorError(ConnectorErrorCode.FILE_CHANGED)
        return VerifiedLocalFile(stream, digest=current.digest, size=current.size)

    def clear(self) -> None:
        """Invalidate every path mapping on process shutdown or replacement."""

        with self._mutex:
            self._closed = True
            self._generation += 1
            self._pending.clear()
            self._granted.clear()

    def __repr__(self) -> str:
        return "ProcessLocalFileGrants(<redacted>)"


class ModelExecutionRequest:
    """Path-free input provided only to one injected exact executor."""

    __slots__ = ("authorized_execution", "local_file", "plan")

    def __init__(
        self,
        authorized_execution: AuthorizedExecution,
        plan: ModelExecutionPlan,
        local_file: VerifiedLocalFile | None,
    ) -> None:
        self.authorized_execution = authorized_execution
        self.plan = plan
        self.local_file = local_file

    def __repr__(self) -> str:
        return "ModelExecutionRequest(<redacted>)"


class PreparedModelExecution:
    """One-use runner for an exact injected binding and optional local file."""

    __slots__ = ("_binding", "_executed", "_mutex", "_request")

    def __init__(
        self, binding: ModelExecutorBinding, request: ModelExecutionRequest
    ) -> None:
        self._binding = binding
        self._request = request
        self._executed = False
        self._mutex = threading.Lock()

    @property
    def backend(self) -> PublicBackendIdentity:
        return self._binding.backend

    @property
    def provenance(self) -> ModelProvenance:
        return self._request.plan.provenance

    async def execute(self) -> OperationResultV1:
        with self._mutex:
            if self._executed:
                raise ConnectorError(ConnectorErrorCode.BACKEND_UNBOUND)
            self._executed = True
        local_file = self._request.local_file
        try:
            result = await self._binding.executor.execute(self._request)
            source = get_source(self._binding.source)
            operation = (
                get_operation(source, self._binding.operation)
                if source is not None
                else None
            )
            if (
                operation is None
                or not isinstance(result, OperationResultV1)
                or len(result.items) > operation.runtime.maximum_items
                or result.character_count() > operation.runtime.maximum_characters
            ):
                raise ProtocolValidationError(
                    "The model executor result exceeds the operation bounds."
                )
            return result
        finally:
            if local_file is not None:
                local_file.close()

    def __repr__(self) -> str:
        return "PreparedModelExecution(<redacted>)"


def prepare_model_execution(
    execution: AuthorizedExecution,
    *,
    policy: ModelPolicy,
    media_source_class: MediaSourceClass,
    workload: ModelWorkload,
    bindings: tuple[ModelExecutorBinding, ...],
    file_grants: ProcessLocalFileGrants | None = None,
    file_grant_id: str | None = None,
) -> PreparedModelExecution:
    """Resolve policy and exact binding before touching a local file mapping."""

    if (
        not isinstance(policy, ModelPolicy)
        or type(bindings) is not tuple
        or not all(isinstance(binding, ModelExecutorBinding) for binding in bindings)
    ):
        raise TypeError("The model execution composition is invalid.")
    row = policy.authorize(
        execution,
        media_source_class=media_source_class,
        workload=workload,
    )
    binding_keys = tuple(
        (binding.source, binding.operation, binding.model) for binding in bindings
    )
    if len(set(binding_keys)) != len(binding_keys):
        raise ValueError("The model executor composition contains a duplicate.")
    selected: ModelExecutorBinding | None = None
    fallback_index = 0
    for index, model in enumerate(row.models_in_order):
        selected = next(
            (
                binding
                for binding in bindings
                if binding.source == execution.request.source
                and binding.operation == execution.request.operation
                and binding.model == model
            ),
            None,
        )
        if selected is not None:
            fallback_index = index
            break
    if selected is None:
        raise ConnectorError(ConnectorErrorCode.BACKEND_UNBOUND)
    local_file: VerifiedLocalFile | None = None
    if media_source_class == "connector_local_file":
        if file_grants is None or file_grant_id is None:
            raise ConnectorError(ConnectorErrorCode.FILE_GRANT_INVALID)
        local_file = file_grants.consume(file_grant_id, execution)
    elif file_grants is not None or file_grant_id is not None:
        raise ConnectorError(ConnectorErrorCode.FILE_GRANT_INVALID)
    plan = ModelExecutionPlan(
        policy_revision=policy.revision,
        row=row,
        workload=workload,
        selected_model=selected.model,
        fallback_index=fallback_index,
    )
    return PreparedModelExecution(
        selected, ModelExecutionRequest(execution, plan, local_file)
    )


def _validate_file_context(
    *,
    subject_key_id: object,
    source: object,
    operation: object,
    grant_revision: object,
    policy_revision: object,
) -> None:
    catalog_source = get_source(source) if type(source) is str else None
    catalog_operation = (
        get_operation(catalog_source, operation)
        if catalog_source is not None and type(operation) is str
        else None
    )
    if (
        type(subject_key_id) is not str
        or _KEY_ID.fullmatch(subject_key_id) is None
        or catalog_operation is None
        or catalog_operation.tool != "transcribe"
        or type(grant_revision) is not int
        or grant_revision <= 0
        or type(policy_revision) is not int
        or policy_revision <= 0
    ):
        raise ConnectorError(ConnectorErrorCode.FILE_GRANT_INVALID)


def _open_hashed_file(
    path: Path, *, changed: bool = False, immutable: bool = False
) -> tuple[_FileSnapshot, BinaryIO]:
    stream: BinaryIO | None = None
    snapshot_stream: BinaryIO | None = None
    try:
        stream = _open_regular_no_follow(path)
        if immutable:
            snapshot_stream = cast(
                BinaryIO, tempfile.TemporaryFile(mode="w+b", buffering=0)
            )
        before = os.fstat(stream.fileno())
        _require_regular_file(before)
        digest = hashlib.sha256()
        while True:
            chunk = stream.read(_HASH_CHUNK_BYTES)
            if not chunk:
                break
            digest.update(chunk)
            if snapshot_stream is not None:
                snapshot_stream.write(chunk)
        after = os.fstat(stream.fileno())
        if not _stable_stat(before, after):
            raise OSError("file changed while hashing")
        file_snapshot = _FileSnapshot(
            path=path,
            device=after.st_dev,
            inode=after.st_ino,
            size=after.st_size,
            modified_ns=after.st_mtime_ns,
            changed_ns=after.st_ctime_ns,
            digest=digest.hexdigest(),
        )
        if snapshot_stream is not None:
            if os.fstat(snapshot_stream.fileno()).st_size != after.st_size:
                raise OSError("immutable file snapshot is incomplete")
            snapshot_stream.seek(0)
            stream.close()
            stream = None
            result_stream = snapshot_stream
            snapshot_stream = None
        else:
            stream.seek(0)
            result_stream = stream
            stream = None
        return file_snapshot, result_stream
    except ConnectorError:
        if stream is not None:
            stream.close()
        if snapshot_stream is not None:
            snapshot_stream.close()
        raise
    except (OSError, TypeError, ValueError):
        if stream is not None:
            stream.close()
        if snapshot_stream is not None:
            snapshot_stream.close()
        code = (
            ConnectorErrorCode.FILE_CHANGED
            if changed
            else ConnectorErrorCode.FILE_GRANT_INVALID
        )
        raise ConnectorError(code) from None


def _open_regular_no_follow(path: Path) -> BinaryIO:
    if not isinstance(path, Path):
        raise TypeError("The Connector-local file path is invalid.")
    raw = os.fspath(path)
    if (
        not path.is_absolute()
        or raw.startswith("//")
        or "\x00" in raw
        or path.name in {"", ".", ".."}
        or any(part in {"", ".", ".."} for part in path.parts[1:])
    ):
        raise ValueError("The Connector-local file path is invalid.")
    required_flags = ("O_CLOEXEC", "O_DIRECTORY", "O_NOFOLLOW", "O_NONBLOCK")
    if any(not hasattr(os, name) for name in required_flags):
        raise OSError("no-follow file opens are unsupported")
    close_on_exec = os.O_CLOEXEC
    no_follow = os.O_NOFOLLOW
    directory = os.O_DIRECTORY
    non_block = os.O_NONBLOCK
    directory_fd = os.open("/", os.O_RDONLY | close_on_exec | directory | no_follow)
    file_fd: int | None = None
    try:
        components = path.parts[1:]
        for component in components[:-1]:
            next_fd = os.open(
                component,
                os.O_RDONLY | close_on_exec | directory | no_follow,
                dir_fd=directory_fd,
            )
            os.close(directory_fd)
            directory_fd = next_fd
        file_fd = os.open(
            components[-1],
            os.O_RDONLY | close_on_exec | no_follow | non_block,
            dir_fd=directory_fd,
        )
        stream = cast(BinaryIO, os.fdopen(file_fd, "rb", buffering=0))
        file_fd = None
        return stream
    finally:
        if file_fd is not None:
            os.close(file_fd)
        os.close(directory_fd)


def _require_regular_file(value: os.stat_result) -> None:
    if (
        not stat.S_ISREG(value.st_mode)
        or value.st_size <= 0
        or value.st_size > MAX_FILE_BYTES
    ):
        raise OSError("not a bounded regular file")


def _stable_stat(before: os.stat_result, after: os.stat_result) -> bool:
    return (
        stat.S_ISREG(after.st_mode)
        and before.st_dev == after.st_dev
        and before.st_ino == after.st_ino
        and before.st_size == after.st_size
        and before.st_mtime_ns == after.st_mtime_ns
        and before.st_ctime_ns == after.st_ctime_ns
    )


def _same_snapshot(expected: _FileSnapshot, actual: _FileSnapshot) -> bool:
    return (
        expected.device == actual.device
        and expected.inode == actual.inode
        and expected.size == actual.size
        and expected.modified_ns == actual.modified_ns
        and expected.changed_ns == actual.changed_ns
        and expected.digest == actual.digest
        and _SHA256.fullmatch(actual.digest) is not None
    )


def _timestamp(clock: WallClock) -> int:
    value = clock()
    if type(value) is not int or value < 0:
        raise ConnectorError(ConnectorErrorCode.FILE_GRANT_INVALID)
    return value


def _new_id() -> str:
    return base64.b32encode(os.urandom(ID_ENTROPY_BYTES)).decode().rstrip("=").lower()


__all__ = [
    "ConnectorModelExecutor",
    "FileGrantProposal",
    "ModelCleanupPolicy",
    "ModelCostPolicy",
    "ModelExecutionPlan",
    "ModelExecutionRequest",
    "ModelExecutorBinding",
    "ModelIdentity",
    "ModelPolicy",
    "ModelPolicyRow",
    "ModelProvenance",
    "ModelWorkload",
    "PreparedModelExecution",
    "ProcessLocalFileGrants",
    "VerifiedLocalFile",
    "prepare_model_execution",
]
