"""Trusted-Connector integration for the fork-owned OpenCLI social runtime."""

from __future__ import annotations

import asyncio
import hashlib
import heapq
import json
import os
import re
import signal
import stat
import sys
import tempfile
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Protocol, cast

from ..catalog import get_operation, get_source
from ..connector.authority import AuthorizedExecution
from ..connector.errors import ConnectorError, ConnectorErrorCode
from ..connector.execution import (
    ConnectorExecutionComposition,
    ConnectorExecutorBinding,
    ExecutorEnvironment,
)
from ..connector.protocol import (
    GrantScope,
    OperationResultItemV1,
    OperationResultV1,
    ProtocolValidationError,
)
from ..contracts import OperationCall, operation_call_is_valid
from ._worker_cleanup import cleanup_worker_resources
from .opencli_social_contract import (
    OPENCLI_SOCIAL_BACKEND,
    OPENCLI_SOCIAL_SCOPE_BY_OPERATION,
    OPENCLI_SOCIAL_SCOPES,
)
from .opencli_social_worker import (
    EXPECTED_BACKEND_VERSION,
    MAX_OUTPUT_BYTES,
    ForkExecutionFailure,
    OpenCliSocialProjection,
    OpenCliSocialProtocolError,
    WorkerErrorCode,
    WorkerOperation,
    WorkerResponse,
    WorkerSource,
    decode_response,
    encode_request,
)

_WORKER_MODULE: Final = "hermes_reach.sources.opencli_social_worker"
_SHA256: Final = re.compile(r"[0-9a-f]{64}")
_PACKAGE_NAME: Final = "@jackwener/opencli"
_PACKAGE_CLI: Final = Path("node_modules/@jackwener/opencli/dist/src/main.js")
_MAX_NODE_BYTES: Final = 256 * 1_024 * 1_024
_MAX_TREE_BYTES: Final = 768 * 1_024 * 1_024
_MAX_TREE_ENTRIES: Final = 30_000
_MAX_TREE_DEPTH: Final = 96
_MAX_PATH_BYTES: Final = 4_096
_MAX_PACKAGE_JSON_BYTES: Final = 64 * 1_024
_TREE_SORT_CHUNK_ENTRIES: Final = 256
_HASH_CHUNK_BYTES: Final = 64 * 1_024
_WORKER_GRACEFUL_SHUTDOWN_SECONDS: Final = 5.0
_FORBIDDEN_TREE_MODE_BITS: Final = 0o7022
_PROXY_VARIABLES: Final = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "no_proxy",
)
_RETRYABLE_ERRORS: Final[frozenset[str]] = frozenset(
    {"backend_unavailable", "deadline_exceeded", "transient"}
)
_ERROR_CODE_MAP: Final[dict[str, ConnectorErrorCode]] = {
    "unsupported_protocol_version": ConnectorErrorCode.BACKEND_CONTRACT_VIOLATION,
    "invalid_request": ConnectorErrorCode.BACKEND_CONTRACT_VIOLATION,
    "unsupported_source": ConnectorErrorCode.BACKEND_CONTRACT_VIOLATION,
    "unsupported_operation": ConnectorErrorCode.BACKEND_CONTRACT_VIOLATION,
    "host_capability_missing": ConnectorErrorCode.BACKEND_CONTRACT_VIOLATION,
    "backend_unavailable": ConnectorErrorCode.BACKEND_UNAVAILABLE,
    "backend_incompatible": ConnectorErrorCode.BACKEND_INCOMPATIBLE,
    "deadline_exceeded": ConnectorErrorCode.BACKEND_DEADLINE_EXCEEDED,
    "cancelled": ConnectorErrorCode.BACKEND_DEADLINE_EXCEEDED,
    "invalid_input": ConnectorErrorCode.BACKEND_INVALID_INPUT,
    "not_found": ConnectorErrorCode.BACKEND_NOT_FOUND,
    "authentication": ConnectorErrorCode.BACKEND_AUTHENTICATION_REQUIRED,
    "authorization": ConnectorErrorCode.BACKEND_AUTHORIZATION_DENIED,
    "rate_limit": ConnectorErrorCode.BACKEND_RATE_LIMITED,
    "transient": ConnectorErrorCode.BACKEND_TRANSIENT,
    "permanent": ConnectorErrorCode.BACKEND_PERMANENT,
    "backend_contract_violation": ConnectorErrorCode.BACKEND_CONTRACT_VIOLATION,
}


@dataclass(frozen=True, slots=True, repr=False)
class OpenCliSessionAttestation:
    """Process-local identity for the exact OpenCLI closure and live session."""

    node_executable: Path
    node_sha256: str
    opencli_root: Path
    opencli_cli: Path
    opencli_tree_sha256: str
    session_home: Path

    def __post_init__(self) -> None:
        paths = (
            self.node_executable,
            self.opencli_root,
            self.opencli_cli,
            self.session_home,
        )
        if (
            any(not _valid_attested_path(path) for path in paths)
            or _SHA256.fullmatch(self.node_sha256) is None
            or _SHA256.fullmatch(self.opencli_tree_sha256) is None
            or self.opencli_cli == self.opencli_root
            or not self.opencli_cli.is_relative_to(self.opencli_root)
        ):
            raise ValueError("The OpenCLI session attestation is invalid.")

    def frame_fields(self) -> dict[str, str]:
        """Return the exact private worker capability without probing paths."""

        return {
            "node_executable": str(self.node_executable),
            "node_sha256": self.node_sha256,
            "opencli_root": str(self.opencli_root),
            "opencli_cli": str(self.opencli_cli),
            "opencli_tree_sha256": self.opencli_tree_sha256,
            "session_home": str(self.session_home),
        }

    def __repr__(self) -> str:
        return "OpenCliSessionAttestation(<redacted>)"


class _OpenCliAttestationError(Exception):
    pass


def attest_opencli_social_session(
    node_executable: Path,
    opencli_root: Path,
    opencli_cli: Path,
    session_home: Path,
) -> OpenCliSessionAttestation:
    """Attest one exact local OpenCLI closure before Connector activation."""

    try:
        node = _canonical_artifact_path(node_executable, kind="file")
        root = _canonical_artifact_path(opencli_root, kind="directory")
        cli = _canonical_artifact_path(opencli_cli, kind="file")
        home = _canonical_artifact_path(session_home, kind="directory")
        if cli != root / _PACKAGE_CLI or _paths_overlap(root, home):
            raise _OpenCliAttestationError
        node_sha256 = _regular_file_sha256(
            node,
            maximum_bytes=_MAX_NODE_BYTES,
            executable=True,
        )
        opencli_tree_sha256 = _tree_sha256(root)
        _validate_package_identity(root, cli)
        return OpenCliSessionAttestation(
            node,
            node_sha256,
            root,
            cli,
            opencli_tree_sha256,
            home,
        )
    except _OpenCliAttestationError:
        raise ConnectorError(ConnectorErrorCode.BACKEND_INCOMPATIBLE) from None


def opencli_social_scopes() -> tuple[GrantScope, ...]:
    """Return the immutable ordered grants enabled by the social composition."""

    return OPENCLI_SOCIAL_SCOPES


class _WorkerClient(Protocol):
    async def execute(
        self,
        source: str,
        operation: str,
        arguments: Mapping[str, object],
        *,
        deadline: float,
    ) -> WorkerResponse: ...


class _IsolatedOpenCliSocialWorker:
    """Supervise one fixed isolated Agent-Reach execution attempt."""

    __slots__ = ("_attestation",)

    def __init__(self, attestation: OpenCliSessionAttestation) -> None:
        self._attestation = attestation

    async def execute(
        self,
        source: str,
        operation: str,
        arguments: Mapping[str, object],
        *,
        deadline: float,
    ) -> WorkerResponse:
        try:
            request = bytearray(
                encode_request(
                    source,
                    operation,
                    arguments,
                    self._attestation.frame_fields(),
                    deadline=deadline,
                )
            )
        except OpenCliSocialProtocolError:
            return _fork_failure(source, operation, "backend_contract_violation")

        process: asyncio.subprocess.Process | None = None
        temporary: tempfile.TemporaryDirectory[str] | None = None
        response_validated = False
        output = bytearray()
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return _fork_failure(source, operation, "deadline_exceeded")
            if not os.path.isabs(sys.executable):
                return _fork_failure(source, operation, "backend_incompatible")
            temporary = tempfile.TemporaryDirectory(
                prefix="hermes-reach-opencli-social-"
            )
            root = Path(temporary.name)
            environment = _isolated_environment(root)
            try:
                process = await asyncio.create_subprocess_exec(
                    sys.executable,
                    "-I",
                    "-m",
                    _WORKER_MODULE,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                    cwd=str(root),
                    env=environment,
                    close_fds=True,
                    start_new_session=True,
                )
            except asyncio.CancelledError:
                raise
            except (OSError, ValueError):
                return _fork_failure(source, operation, "backend_unavailable")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return _fork_failure(source, operation, "deadline_exceeded")
            try:
                async with asyncio.timeout(remaining):
                    output = await _exchange_bounded(process, request)
            except TimeoutError:
                return _fork_failure(source, operation, "deadline_exceeded")
            if process.returncode != 0:
                return _fork_failure(source, operation, "transient")
            try:
                response = decode_response(
                    output,
                    source=source,
                    operation=operation,
                    arguments=arguments,
                )
            except OpenCliSocialProtocolError:
                return _fork_failure(source, operation, "backend_contract_violation")
            response_validated = True
            return response
        except asyncio.CancelledError:
            raise
        except (BrokenPipeError, ChildProcessError, ConnectionError, OSError):
            return _fork_failure(source, operation, "transient")
        except Exception:
            return _fork_failure(source, operation, "backend_contract_violation")
        finally:
            request[:] = b"\x00" * len(request)
            output[:] = b"\x00" * len(output)
            await cleanup_worker_resources(
                (
                    _cleanup_process_group(
                        process,
                        terminate_group=not response_validated,
                    )
                    if process is not None
                    else None
                ),
                temporary,
            )


class OpenCliSocialExecutor:
    """Map one authorized social operation into the isolated fork runtime."""

    __slots__ = ("_attestation", "_monotonic_clock", "_wall_clock", "_worker")

    def __init__(
        self,
        attestation: OpenCliSessionAttestation,
        worker: _WorkerClient | None = None,
        *,
        wall_clock: Callable[[], float] = time.time,
        monotonic_clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if (
            type(attestation) is not OpenCliSessionAttestation
            or (worker is not None and not callable(getattr(worker, "execute", None)))
            or not callable(wall_clock)
            or not callable(monotonic_clock)
        ):
            raise TypeError("The OpenCLI social executor configuration is invalid.")
        self._attestation = attestation
        self._worker = (
            worker if worker is not None else _IsolatedOpenCliSocialWorker(attestation)
        )
        self._wall_clock = wall_clock
        self._monotonic_clock = monotonic_clock

    async def execute(
        self,
        execution: AuthorizedExecution,
        environment: ExecutorEnvironment,
    ) -> OperationResultV1:
        if not isinstance(execution, AuthorizedExecution) or not _empty_environment(
            environment
        ):
            raise ConnectorError(ConnectorErrorCode.CONNECTOR_STATE_INVALID)
        call = execution.operation_call()
        key = (execution.request.source, execution.request.operation)
        expected_scope = OPENCLI_SOCIAL_SCOPE_BY_OPERATION.get(key)
        if (
            expected_scope is None
            or execution.required_scope != expected_scope
            or call.source.name != key[0]
            or call.operation.name != key[1]
            or not operation_call_is_valid(call)
        ):
            raise ConnectorError(ConnectorErrorCode.BACKEND_INVALID_INPUT)
        try:
            arguments = _arguments_from_call(call)
        except (TypeError, ValueError):
            raise ConnectorError(ConnectorErrorCode.BACKEND_INVALID_INPUT) from None
        remaining = execution.request.deadline - float(self._wall_clock())
        if remaining <= 0:
            raise ConnectorError(ConnectorErrorCode.CONNECTOR_DEADLINE_EXCEEDED)
        deadline = float(self._monotonic_clock()) + remaining

        for attempt in range(2):
            try:
                response = await self._worker.execute(
                    key[0],
                    key[1],
                    arguments,
                    deadline=deadline,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                raise ConnectorError(
                    ConnectorErrorCode.BACKEND_CONTRACT_VIOLATION
                ) from None
            if isinstance(response, OpenCliSocialProjection):
                return _operation_result(response, key=key)
            if not isinstance(response, ForkExecutionFailure):
                raise ConnectorError(ConnectorErrorCode.BACKEND_CONTRACT_VIOLATION)
            if (
                response.source != key[0]
                or response.operation != key[1]
                or response.error_code not in _ERROR_CODE_MAP
            ):
                raise ConnectorError(ConnectorErrorCode.BACKEND_CONTRACT_VIOLATION)
            if (
                attempt == 0
                and response.error_code in _RETRYABLE_ERRORS
                and float(self._monotonic_clock()) < deadline
            ):
                continue
            raise ConnectorError(_ERROR_CODE_MAP[response.error_code])
        raise ConnectorError(ConnectorErrorCode.BACKEND_CONTRACT_VIOLATION)

    def __repr__(self) -> str:
        return "OpenCliSocialExecutor(<redacted>)"


def opencli_social_execution_composition(
    attestation: OpenCliSessionAttestation,
) -> ConnectorExecutionComposition:
    """Compose all and only the 15 exact social Connector bindings."""

    if type(attestation) is not OpenCliSessionAttestation:
        raise TypeError("The OpenCLI social composition is invalid.")
    executor = OpenCliSocialExecutor(attestation)
    return ConnectorExecutionComposition(
        tuple(
            ConnectorExecutorBinding(
                required_scope=scope,
                backend=OPENCLI_SOCIAL_BACKEND,
                executor=executor,
            )
            for scope in OPENCLI_SOCIAL_SCOPES
        )
    )


def _arguments_from_call(call: OperationCall) -> dict[str, object]:
    key = (call.source.name, call.operation.name)
    default_limit = call.operation.runtime.maximum_items
    if key in {
        ("reddit", "search.posts"),
        ("facebook", "search"),
        ("instagram", "search.users"),
    }:
        if type(call.query) is not str or call.target is not None:
            raise ValueError("invalid social call")
        return {"query": call.query, "limit": _limit(call.options, default_limit)}
    if key == ("reddit", "read.post"):
        return {"url": _target(call, "url")}
    if key == ("reddit", "browse.subreddit"):
        return {
            "subreddit": _target(call, "native_id"),
            "limit": _limit(call.options, default_limit),
        }
    if key == ("reddit", "read.subreddit"):
        return {"subreddit": _target(call, "native_id")}
    if key in {
        ("reddit", "browse.hot"),
        ("reddit", "browse.popular"),
        ("reddit", "browse.all"),
        ("facebook", "browse.feed"),
        ("facebook", "browse.groups"),
        ("instagram", "browse.explore"),
    }:
        if call.target is not None:
            raise ValueError("invalid social call")
        return {"limit": _limit(call.options, default_limit)}
    if key in {
        ("facebook", "read.profile"),
        ("instagram", "read.profile"),
    }:
        return {"username": _target(call, "native_id")}
    if key == ("instagram", "browse.user_posts"):
        return {
            "username": _target(call, "native_id"),
            "limit": _limit(call.options, default_limit),
        }
    raise ValueError("invalid social call")


def _target(call: OperationCall, name: str) -> str:
    target = call.target
    if target is None or set(target) != {name}:
        raise ValueError("invalid social call")
    value = target[name]
    if type(value) is not str:
        raise ValueError("invalid social call")
    return value


def _limit(options: Mapping[str, object], default: int) -> int:
    if not set(options).issubset({"limit"}):
        raise ValueError("invalid social call")
    value = options.get("limit", default)
    if type(value) is not int or not 1 <= value <= 50:
        raise ValueError("invalid social call")
    return value


def _operation_result(
    projection: OpenCliSocialProjection,
    *,
    key: tuple[str, str],
) -> OperationResultV1:
    if projection.source != key[0] or projection.operation != key[1]:
        raise ConnectorError(ConnectorErrorCode.BACKEND_CONTRACT_VIOLATION)
    try:
        result = OperationResultV1(
            tuple(
                OperationResultItemV1(
                    item.kind,
                    item.text,
                    native_id=item.native_id,
                    title=item.title,
                    url=item.url,
                    author=item.author,
                    published_at=item.published_at,
                )
                for item in projection.items
            ),
            projection.truncated,
        )
        source = get_source(key[0])
        operation = get_operation(source, key[1]) if source is not None else None
        if (
            operation is None
            or len(result.items) > operation.runtime.maximum_items
            or result.character_count() > operation.runtime.maximum_characters
        ):
            raise ProtocolValidationError("The social result exceeds its bounds.")
        return result
    except ProtocolValidationError:
        raise ConnectorError(ConnectorErrorCode.BACKEND_CONTRACT_VIOLATION) from None


def _fork_failure(
    source: str,
    operation: str,
    code: WorkerErrorCode,
) -> ForkExecutionFailure:
    if (source, operation) not in OPENCLI_SOCIAL_SCOPE_BY_OPERATION:
        source, operation = "reddit", "read.post"
        code = "backend_contract_violation"
    return ForkExecutionFailure(
        cast(WorkerSource, source),
        cast(WorkerOperation, operation),
        code,
    )


def _canonical_artifact_path(value: object, *, kind: str) -> Path:
    if (
        not isinstance(value, Path)
        or not _valid_attested_path(value)
        or kind not in {"file", "directory"}
    ):
        raise _OpenCliAttestationError
    try:
        resolved = value.resolve(strict=True)
        metadata = value.lstat()
    except OSError:
        raise _OpenCliAttestationError from None
    if (
        resolved != value
        or metadata.st_uid != os.getuid()
        or metadata.st_mode & 0o022
        or (kind == "file" and not stat.S_ISREG(metadata.st_mode))
        or (kind == "directory" and not stat.S_ISDIR(metadata.st_mode))
    ):
        raise _OpenCliAttestationError
    return value


def _paths_overlap(first: Path, second: Path) -> bool:
    return (
        first == second or first.is_relative_to(second) or second.is_relative_to(first)
    )


def _regular_file_sha256(
    path: Path,
    *,
    maximum_bytes: int,
    executable: bool,
) -> str:
    try:
        metadata = path.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or metadata.st_nlink != 1
            or metadata.st_size <= 0
            or metadata.st_size > maximum_bytes
            or metadata.st_mode & 0o022
            or (executable and not metadata.st_mode & stat.S_IXUSR)
        ):
            raise _OpenCliAttestationError
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            observed = os.fstat(stream.fileno())
            if _file_identity(observed) != _file_identity(metadata):
                raise _OpenCliAttestationError
            remaining = metadata.st_size
            while remaining:
                chunk = stream.read(min(_HASH_CHUNK_BYTES, remaining))
                if not chunk:
                    raise _OpenCliAttestationError
                digest.update(chunk)
                remaining -= len(chunk)
            if stream.read(1):
                raise _OpenCliAttestationError
            if _file_identity(os.fstat(stream.fileno())) != _file_identity(observed):
                raise _OpenCliAttestationError
        return digest.hexdigest()
    except _OpenCliAttestationError:
        raise
    except OSError:
        raise _OpenCliAttestationError from None


def _file_identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_uid,
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _tree_entry(root: Path, path: Path) -> tuple[str, Path]:
    try:
        relative = path.relative_to(root)
        name = "." if relative == Path(".") else relative.as_posix()
        encoded = name.encode("utf-8", errors="strict")
    except (UnicodeError, ValueError):
        raise _OpenCliAttestationError from None
    if (
        not encoded
        or len(encoded) > _MAX_PATH_BYTES
        or len(relative.parts) > _MAX_TREE_DEPTH
    ):
        raise _OpenCliAttestationError
    return name, path


def _sorted_tree_entries(root: Path) -> Iterator[tuple[str, Path]]:
    chunks: list[list[tuple[str, Path]]] = []
    chunk = [_tree_entry(root, root)]
    directories = [root]
    entry_count = 1
    try:
        while directories:
            directory = directories.pop()
            with os.scandir(directory) as iterator:
                for entry in iterator:
                    if entry_count >= _MAX_TREE_ENTRIES:
                        raise _OpenCliAttestationError
                    path = Path(entry.path)
                    chunk.append(_tree_entry(root, path))
                    entry_count += 1
                    if entry.is_dir(follow_symlinks=False):
                        directories.append(path)
                    if len(chunk) >= _TREE_SORT_CHUNK_ENTRIES:
                        chunk.sort(key=lambda item: item[0])
                        chunks.append(chunk)
                        chunk = []
        if chunk:
            chunk.sort(key=lambda item: item[0])
            chunks.append(chunk)
    except _OpenCliAttestationError:
        raise
    except OSError:
        raise _OpenCliAttestationError from None
    yield from heapq.merge(*chunks, key=lambda item: item[0])


def _tree_sha256(root: Path) -> str:
    digest = hashlib.sha256(b"agent-reach-opencli-tree-v1\0")
    total_bytes = 0
    for name, path in _sorted_tree_entries(root):
        try:
            encoded = name.encode("utf-8", errors="strict")
            metadata = path.lstat()
        except (UnicodeError, OSError):
            raise _OpenCliAttestationError from None
        if (
            metadata.st_uid != os.getuid()
            or metadata.st_mode & _FORBIDDEN_TREE_MODE_BITS
        ):
            raise _OpenCliAttestationError
        digest.update(b"D" if stat.S_ISDIR(metadata.st_mode) else b"F")
        digest.update(len(encoded).to_bytes(4, "big"))
        digest.update(encoded)
        digest.update((metadata.st_mode & 0o777).to_bytes(2, "big"))
        if stat.S_ISDIR(metadata.st_mode):
            continue
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise _OpenCliAttestationError
        total_bytes += metadata.st_size
        if total_bytes > _MAX_TREE_BYTES:
            raise _OpenCliAttestationError
        digest.update(metadata.st_size.to_bytes(8, "big"))
        _update_digest_from_file(digest, path, metadata)
    return digest.hexdigest()


def _update_digest_from_file(
    digest: object,
    path: Path,
    metadata: os.stat_result,
) -> None:
    updater = getattr(digest, "update", None)
    if not callable(updater):
        raise _OpenCliAttestationError
    try:
        with path.open("rb") as stream:
            observed = os.fstat(stream.fileno())
            if _file_identity(observed) != _file_identity(metadata):
                raise _OpenCliAttestationError
            remaining = metadata.st_size
            while remaining:
                chunk = stream.read(min(_HASH_CHUNK_BYTES, remaining))
                if not chunk:
                    raise _OpenCliAttestationError
                updater(chunk)
                remaining -= len(chunk)
            if stream.read(1):
                raise _OpenCliAttestationError
            if _file_identity(os.fstat(stream.fileno())) != _file_identity(observed):
                raise _OpenCliAttestationError
    except _OpenCliAttestationError:
        raise
    except OSError:
        raise _OpenCliAttestationError from None


def _validate_package_identity(root: Path, cli: Path) -> None:
    package_json = root / _PACKAGE_CLI.parents[2] / "package.json"
    try:
        metadata = package_json.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or metadata.st_nlink != 1
            or not 0 < metadata.st_size <= _MAX_PACKAGE_JSON_BYTES
            or metadata.st_mode & 0o022
        ):
            raise _OpenCliAttestationError
        raw = bytearray()
        with package_json.open("rb") as stream:
            observed = os.fstat(stream.fileno())
            if _file_identity(observed) != _file_identity(metadata):
                raise _OpenCliAttestationError
            remaining = metadata.st_size
            while remaining:
                chunk = stream.read(min(_HASH_CHUNK_BYTES, remaining))
                if not chunk:
                    raise _OpenCliAttestationError
                raw.extend(chunk)
                remaining -= len(chunk)
            if stream.read(1):
                raise _OpenCliAttestationError
            if _file_identity(os.fstat(stream.fileno())) != _file_identity(observed):
                raise _OpenCliAttestationError
        value = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=_reject_duplicate_json_pairs,
        )
    except _OpenCliAttestationError:
        raise
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
        raise _OpenCliAttestationError from None
    if not isinstance(value, Mapping):
        raise _OpenCliAttestationError
    package = cast(Mapping[str, object], value)
    binary = package.get("bin")
    if (
        package.get("name") != _PACKAGE_NAME
        or package.get("version") != EXPECTED_BACKEND_VERSION
        or not isinstance(binary, Mapping)
        or set(binary) != {"opencli"}
        or binary.get("opencli") != "dist/src/main.js"
        or cli != root / _PACKAGE_CLI
    ):
        raise _OpenCliAttestationError


def _reject_duplicate_json_pairs(
    pairs: Sequence[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate json key")
        result[key] = value
    return result


def _valid_attested_path(value: object) -> bool:
    if (
        not isinstance(value, Path)
        or not value.is_absolute()
        or value == Path("/")
        or ".." in value.parts
    ):
        return False
    rendered = str(value)
    return bool(
        0 < len(rendered) <= 8_192 and rendered.isprintable() and "\x00" not in rendered
    )


def _empty_environment(environment: object) -> bool:
    try:
        return isinstance(environment, Mapping) and len(environment) == 0
    except Exception:
        return False


def _isolated_environment(root: Path) -> dict[str, str]:
    directories = {
        "HOME": root / "home",
        "XDG_CONFIG_HOME": root / "config",
        "XDG_CACHE_HOME": root / "cache",
        "XDG_DATA_HOME": root / "data",
        "TMPDIR": root / "tmp",
    }
    for directory in directories.values():
        directory.mkdir(mode=0o700)
    environment = {
        **{name: str(path) for name, path in directories.items()},
        "PYTHONUTF8": "1",
        "PYTHONNOUSERSITE": "1",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "TZ": "UTC",
    }
    environment.update({name: "" for name in _PROXY_VARIABLES})
    return environment


async def _exchange_bounded(
    process: asyncio.subprocess.Process,
    request: bytearray,
) -> bytearray:
    writer = process.stdin
    reader = process.stdout
    if writer is None or reader is None:
        raise OSError("worker transport unavailable")
    try:
        writer.write(request)
        await writer.drain()
    finally:
        writer.close()
    output = bytearray()
    while len(output) <= MAX_OUTPUT_BYTES + 4:
        chunk = await reader.read(min(8_192, MAX_OUTPUT_BYTES + 5 - len(output)))
        if not chunk:
            break
        output.extend(chunk)
        if len(output) > MAX_OUTPUT_BYTES + 4:
            output[:] = b"\x00" * len(output)
            raise OpenCliSocialProtocolError("worker_response_invalid")
    await process.wait()
    return output


async def _cleanup_process_group(
    process: asyncio.subprocess.Process,
    *,
    terminate_group: bool,
) -> None:
    if process.returncode is not None and not terminate_group:
        return
    if terminate_group:
        if process.returncode is None:
            try:
                process.terminate()
            except ProcessLookupError:
                pass
            except OSError:
                pass
            try:
                async with asyncio.timeout(_WORKER_GRACEFUL_SHUTDOWN_SECONDS):
                    await process.wait()
            except TimeoutError:
                pass
            except (BrokenPipeError, ChildProcessError, ConnectionError, OSError):
                pass
        if process.returncode is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError:
            if process.returncode is None:
                try:
                    process.kill()
                except OSError:
                    pass
    if process.returncode is not None:
        return
    try:
        await process.wait()
    except (BrokenPipeError, ChildProcessError, ConnectionError, OSError):
        pass


__all__ = [
    "OpenCliSessionAttestation",
    "OpenCliSocialExecutor",
    "attest_opencli_social_session",
    "opencli_social_scopes",
    "opencli_social_execution_composition",
]
