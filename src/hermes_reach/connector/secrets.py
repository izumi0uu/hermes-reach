"""Connector-only secret bindings and isolated Bitwarden resolution."""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import math
import os
import re
import signal
import stat
import subprocess
import sys
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final, Literal, Protocol, SupportsIndex, TypeVar
from urllib.parse import urlsplit

import yaml

from .authority import AuthorizedExecution
from .errors import ConnectorError, ConnectorErrorCode
from .identity import _open_state_directory
from .limits import (
    DEFAULT_SECRET_HELPER_TIMEOUT_SECONDS,
    ID_BASE32_LENGTH,
    ID_ENTROPY_BYTES,
    MAX_BWS_BINARY_BYTES,
    MAX_SECRET_BINDINGS,
    MAX_SECRET_BYTES,
    MAX_SECRET_HELPER_INPUT_BYTES,
)
from .protocol import canonical_json_bytes

_BWS_ACCESS_TOKEN_ENV: Final = "BWS_ACCESS_TOKEN"
_BWS_VERSION: Final = "2.0.0"
_HELPER_MODULE: Final = "hermes_reach.connector.bitwarden_helper"
_HELPER_RESPONSE_HEADER_BYTES: Final = 5
_HELPER_SUCCESS: Final = 0
_PROFILE_CONFIG_MAX_BYTES: Final = 256 * 1024
_BWS_CACHE_FILENAMES: Final = frozenset({"bws_cache.json", "bws_cache.enc.json"})
_ENV_NAME: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}\Z")
_SHA256: Final = re.compile(r"[0-9a-f]{64}\Z")

SecretExecutionResult = TypeVar("SecretExecutionResult")
EntropySource = Callable[[int], bytes]
MonotonicClock = Callable[[], float]
VersionReader = Callable[[Path, Mapping[str, str]], str]


def _monotonic() -> float:
    return time.monotonic()


@dataclass(frozen=True, slots=True, repr=False)
class CapabilityId:
    """A random wire-visible binding identifier with no provider semantics."""

    _value: str = field(repr=False)

    def __post_init__(self) -> None:
        if not _is_capability_id(self._value):
            raise ValueError("The opaque capability identifier is invalid.")

    @classmethod
    def new(cls, entropy: EntropySource = os.urandom) -> CapabilityId:
        raw = entropy(ID_ENTROPY_BYTES)
        if type(raw) is not bytes or len(raw) != ID_ENTROPY_BYTES:
            raise ValueError("Capability entropy is invalid.")
        return cls(base64.b32encode(raw).decode("ascii").rstrip("=").lower())

    @classmethod
    def parse(cls, value: object) -> CapabilityId:
        if type(value) is not str:
            raise ValueError("The opaque capability identifier is invalid.")
        return cls(value)

    def for_grant(self) -> str:
        """Return the opaque value only for an exact signed grant scope."""

        return self._value

    def __repr__(self) -> str:
        return "CapabilityId(<opaque>)"

    __str__ = __repr__


@dataclass(frozen=True, slots=True, repr=False)
class BitwardenSecretBinding:
    """Closed Connector-local metadata for one exact operation binding."""

    capability_id: CapabilityId
    source: str
    operation: str
    project_id: str
    selector: str
    injection_target: str
    profile_home: Path
    bws_sha256: str
    server_url: str = ""
    revoked_at: int | None = None
    provider: Literal["bitwarden"] = "bitwarden"

    def __post_init__(self) -> None:
        if not isinstance(self.capability_id, CapabilityId):
            raise ValueError("The secret capability binding is invalid.")
        _validate_source_operation(self.source, self.operation)
        if self.provider != "bitwarden":
            raise ValueError("The secret provider binding is invalid.")
        try:
            project = uuid.UUID(self.project_id)
        except (AttributeError, ValueError):
            raise ValueError("The Bitwarden project binding is invalid.") from None
        if str(project) != self.project_id:
            raise ValueError("The Bitwarden project binding is invalid.")
        if (
            _ENV_NAME.fullmatch(self.selector) is None
            or _ENV_NAME.fullmatch(self.injection_target) is None
            or self.selector == _BWS_ACCESS_TOKEN_ENV
            or self.injection_target == _BWS_ACCESS_TOKEN_ENV
        ):
            raise ValueError("The secret selector binding is invalid.")
        if (
            not isinstance(self.profile_home, Path)
            or not self.profile_home.is_absolute()
            or _SHA256.fullmatch(self.bws_sha256) is None
        ):
            raise ValueError("The Bitwarden profile binding is invalid.")
        _validate_server_url(self.server_url)
        if self.revoked_at is not None and (
            type(self.revoked_at) is not int or self.revoked_at < 0
        ):
            raise ValueError("The secret binding revocation is invalid.")

    @property
    def bws_executable(self) -> Path:
        return self.profile_home / "bin" / "bws"

    def __repr__(self) -> str:
        return (
            "BitwardenSecretBinding("
            f"source={self.source!r}, operation={self.operation!r}, "
            "provider='bitwarden', configuration=<redacted>)"
        )


class SecretBindingCatalog:
    """Immutable exact binding map injected only into a trusted Connector."""

    __slots__ = ("_bindings",)

    def __init__(self, bindings: Sequence[BitwardenSecretBinding]) -> None:
        if (
            isinstance(bindings, str | bytes | bytearray)
            or len(bindings) > MAX_SECRET_BINDINGS
        ):
            raise ValueError("The secret binding catalog is invalid.")
        by_capability: dict[CapabilityId, BitwardenSecretBinding] = {}
        operations: set[tuple[str, str]] = set()
        for binding in bindings:
            if not isinstance(binding, BitwardenSecretBinding):
                raise ValueError("The secret binding catalog is invalid.")
            operation = (binding.source, binding.operation)
            if binding.capability_id in by_capability or operation in operations:
                raise ValueError("The secret binding catalog contains duplicates.")
            by_capability[binding.capability_id] = binding
            operations.add(operation)
        self._bindings = MappingProxyType(by_capability)

    def require_active(
        self,
        capability_id: CapabilityId,
        *,
        source: str | None = None,
        operation: str | None = None,
    ) -> BitwardenSecretBinding:
        """Resolve one active local binding without exposing its configuration."""

        if not isinstance(capability_id, CapabilityId):
            raise ConnectorError(ConnectorErrorCode.SECRET_BINDING_DENIED)
        binding = self._bindings.get(capability_id)
        if (
            binding is None
            or binding.revoked_at is not None
            or (source is not None and binding.source != source)
            or (operation is not None and binding.operation != operation)
        ):
            raise ConnectorError(ConnectorErrorCode.SECRET_BINDING_DENIED)
        return binding

    def active_bindings(self) -> tuple[BitwardenSecretBinding, ...]:
        """Return local configuration only to explicit provider composition."""

        return tuple(
            binding for binding in self._bindings.values() if binding.revoked_at is None
        )

    def __repr__(self) -> str:
        return f"SecretBindingCatalog(count={len(self._bindings)}, <redacted>)"


class SecretMaterial:
    """One non-copyable mutable secret buffer with an explicit lifetime."""

    __slots__ = ("_buffer", "_closed")

    def __init__(self, value: bytearray) -> None:
        if type(value) is not bytearray:
            raise ConnectorError(ConnectorErrorCode.SECRET_UNAVAILABLE)
        try:
            if not value or len(value) > MAX_SECRET_BYTES or 0 in value:
                raise ValueError("invalid secret material")
            value.decode("utf-8", errors="strict")
            self._buffer = bytearray(value)
        except (UnicodeError, ValueError):
            value[:] = b"\x00" * len(value)
            raise ConnectorError(ConnectorErrorCode.SECRET_UNAVAILABLE) from None
        value[:] = b"\x00" * len(value)
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    def close(self) -> None:
        if self._closed:
            return
        self._buffer[:] = b"\x00" * len(self._buffer)
        self._closed = True

    def _decode_for_executor(self) -> str:
        if self._closed:
            raise ConnectorError(ConnectorErrorCode.SECRET_UNAVAILABLE)
        try:
            return self._buffer.decode("utf-8", errors="strict")
        except UnicodeError:
            raise ConnectorError(ConnectorErrorCode.SECRET_UNAVAILABLE) from None

    def __enter__(self) -> SecretMaterial:
        if self._closed:
            raise ConnectorError(ConnectorErrorCode.SECRET_UNAVAILABLE)
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def __copy__(self) -> SecretMaterial:
        raise TypeError("Secret material cannot be copied.")

    def __deepcopy__(self, memo: object) -> SecretMaterial:
        del memo
        raise TypeError("Secret material cannot be copied.")

    def __reduce__(self) -> str | tuple[Any, ...]:
        raise TypeError("Secret material cannot be serialized.")

    def __reduce_ex__(self, protocol: SupportsIndex) -> str | tuple[Any, ...]:
        del protocol
        raise TypeError("Secret material cannot be serialized.")

    def __getstate__(self) -> object:
        raise TypeError("Secret material cannot be serialized.")

    def __repr__(self) -> str:
        return "SecretMaterial(<redacted>)"

    __str__ = __repr__

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass


class SecretProvider(Protocol):
    async def resolve(
        self,
        capability_id: CapabilityId,
        *,
        deadline: float,
        require_fresh: bool,
    ) -> SecretMaterial: ...


class BitwardenHelperLauncher(Protocol):
    async def fetch(
        self,
        binding: BitwardenSecretBinding,
        *,
        access_token: str,
        deadline: float,
    ) -> bytearray: ...


class SubprocessBitwardenHelper:
    """Launch one killable helper with no ambient provider configuration."""

    __slots__ = ("_clock", "_environment", "_python")

    def __init__(
        self,
        *,
        environment: Mapping[str, str] | None = None,
        clock: MonotonicClock = _monotonic,
        python_executable: Path | None = None,
    ) -> None:
        self._clock = clock
        self._environment = os.environ if environment is None else environment
        self._python = (
            Path(sys.executable) if python_executable is None else python_executable
        )
        if not callable(self._clock) or not self._python.is_absolute():
            raise TypeError("The Bitwarden helper launcher is invalid.")

    async def fetch(
        self,
        binding: BitwardenSecretBinding,
        *,
        access_token: str,
        deadline: float,
    ) -> bytearray:
        if not isinstance(binding, BitwardenSecretBinding):
            raise ConnectorError(ConnectorErrorCode.SECRET_UNAVAILABLE)
        timeout = _remaining_timeout(deadline, self._clock())
        request = canonical_json_bytes(
            {
                "profile_home": str(binding.profile_home),
                "project_id": binding.project_id,
                "selector": binding.selector,
                "server_url": binding.server_url,
                "version": 1,
            }
        )
        if len(request) > MAX_SECRET_HELPER_INPUT_BYTES:
            raise ConnectorError(ConnectorErrorCode.SECRET_UNAVAILABLE)
        environment = _minimal_helper_environment(
            binding, access_token=access_token, parent=self._environment
        )
        try:
            process = await asyncio.create_subprocess_exec(
                str(self._python),
                "-I",
                "-m",
                _HELPER_MODULE,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                cwd=binding.profile_home,
                env=environment,
                close_fds=True,
                start_new_session=True,
            )
        except (OSError, ValueError):
            raise ConnectorError(ConnectorErrorCode.SECRET_UNAVAILABLE) from None
        try:
            stdout = await asyncio.wait_for(
                _communicate_bounded(process, request), timeout=timeout
            )
            if process.returncode != 0:
                raise ConnectorError(ConnectorErrorCode.SECRET_UNAVAILABLE)
            return _parse_helper_response(stdout)
        except TimeoutError:
            raise ConnectorError(ConnectorErrorCode.SECRET_UNAVAILABLE) from None
        except asyncio.CancelledError:
            raise
        except ConnectorError:
            raise
        except (BrokenPipeError, ConnectionError, OSError):
            raise ConnectorError(ConnectorErrorCode.SECRET_UNAVAILABLE) from None
        finally:
            await _kill_process_group(process)

    def __repr__(self) -> str:
        return "SubprocessBitwardenHelper(<redacted>)"


class BitwardenSecretProvider:
    """Resolve only pre-approved bindings through a one-fetch child process."""

    __slots__ = (
        "_catalog",
        "_clock",
        "_environment",
        "_launcher",
        "_version_reader",
    )

    def __init__(
        self,
        catalog: SecretBindingCatalog,
        *,
        launcher: BitwardenHelperLauncher | None = None,
        environment: Mapping[str, str] | None = None,
        clock: MonotonicClock = _monotonic,
        version_reader: VersionReader = lambda path, env: _read_bws_version(path, env),
    ) -> None:
        if not isinstance(catalog, SecretBindingCatalog):
            raise TypeError("The Bitwarden provider catalog is invalid.")
        effective_environment = os.environ if environment is None else environment
        effective_launcher = (
            SubprocessBitwardenHelper(
                environment=effective_environment,
                clock=clock,
            )
            if launcher is None
            else launcher
        )
        if (
            not callable(clock)
            or not callable(version_reader)
            or not hasattr(effective_launcher, "fetch")
        ):
            raise TypeError("The Bitwarden provider dependencies are invalid.")
        self._catalog = catalog
        self._environment = effective_environment
        self._clock = clock
        self._launcher = effective_launcher
        self._version_reader = version_reader
        probed: set[Path] = set()
        for binding in catalog.active_bindings():
            _validate_binding_runtime(binding, effective_environment)
            if binding.bws_executable not in probed:
                version = version_reader(
                    binding.bws_executable,
                    _minimal_bws_environment(binding, effective_environment),
                )
                if version != _BWS_VERSION:
                    raise ConnectorError(ConnectorErrorCode.SECRET_UNAVAILABLE)
                probed.add(binding.bws_executable)

    async def resolve(
        self,
        capability_id: CapabilityId,
        *,
        deadline: float,
        require_fresh: bool,
    ) -> SecretMaterial:
        if require_fresh is not True:
            raise ConnectorError(ConnectorErrorCode.SECRET_UNAVAILABLE)
        binding = self._catalog.require_active(capability_id)
        _remaining_timeout(deadline, self._clock())
        _validate_binding_runtime(binding, self._environment)
        if (
            binding.selector in self._environment
            or binding.injection_target in self._environment
        ):
            raise ConnectorError(ConnectorErrorCode.SECRET_UNAVAILABLE)
        token = self._environment.get(_BWS_ACCESS_TOKEN_ENV, "")
        if type(token) is not str or not token or len(token) > 4096 or "\x00" in token:
            raise ConnectorError(ConnectorErrorCode.SECRET_UNAVAILABLE)
        try:
            value = await self._launcher.fetch(
                binding,
                access_token=token,
                deadline=deadline,
            )
            return SecretMaterial(value)
        except asyncio.CancelledError:
            raise
        except ConnectorError:
            raise
        except Exception:
            raise ConnectorError(ConnectorErrorCode.SECRET_UNAVAILABLE) from None

    def __repr__(self) -> str:
        return "BitwardenSecretProvider(<redacted>)"


def prepare_secret_execution(
    execution: AuthorizedExecution,
    *,
    catalog: SecretBindingCatalog,
    provider: SecretProvider,
    injection_target: str,
    deadline: float,
    execute: Callable[[Mapping[str, str]], Awaitable[SecretExecutionResult]],
) -> Awaitable[SecretExecutionResult]:
    """Validate a local binding during authority handoff, before provider access."""

    if not isinstance(execution, AuthorizedExecution) or not callable(execute):
        raise TypeError("The scoped secret execution boundary is invalid.")
    capability_value = execution.required_scope.capability_id
    try:
        capability_id = CapabilityId.parse(capability_value)
    except ValueError:
        raise ConnectorError(ConnectorErrorCode.SECRET_BINDING_DENIED) from None
    binding = catalog.require_active(
        capability_id,
        source=execution.request.source,
        operation=execution.request.operation,
    )
    if injection_target != binding.injection_target:
        raise ConnectorError(ConnectorErrorCode.SECRET_BINDING_DENIED)
    return _resolve_and_execute(
        provider,
        capability_id,
        injection_target=injection_target,
        deadline=deadline,
        execute=execute,
    )


async def execute_with_secret(
    material: SecretMaterial,
    *,
    injection_target: str,
    execute: Callable[[Mapping[str, str]], Awaitable[SecretExecutionResult]],
) -> SecretExecutionResult:
    """Expose one value only through a fresh one-entry executor mapping."""

    if (
        not isinstance(material, SecretMaterial)
        or _ENV_NAME.fullmatch(injection_target) is None
        or not callable(execute)
    ):
        if isinstance(material, SecretMaterial):
            material.close()
        raise ConnectorError(ConnectorErrorCode.SECRET_UNAVAILABLE)
    environment: dict[str, str] = {}
    try:
        environment[injection_target] = material._decode_for_executor()
        try:
            return await execute(MappingProxyType(environment))
        except asyncio.CancelledError:
            raise
        except ConnectorError:
            raise
        except Exception:
            raise ConnectorError(ConnectorErrorCode.SECRET_UNAVAILABLE) from None
    finally:
        environment.clear()
        material.close()


async def _resolve_and_execute(
    provider: SecretProvider,
    capability_id: CapabilityId,
    *,
    injection_target: str,
    deadline: float,
    execute: Callable[[Mapping[str, str]], Awaitable[SecretExecutionResult]],
) -> SecretExecutionResult:
    material = await provider.resolve(
        capability_id,
        deadline=deadline,
        require_fresh=True,
    )
    return await execute_with_secret(
        material,
        injection_target=injection_target,
        execute=execute,
    )


def _validate_source_operation(source: object, operation: object) -> None:
    from ..catalog import get_operation, get_source

    if type(source) is not str or type(operation) is not str:
        raise ValueError("The secret source-operation binding is invalid.")
    source_spec = get_source(source)
    operation_spec = (
        get_operation(source_spec, operation) if source_spec is not None else None
    )
    if operation_spec is None or operation_spec.tool == "status":
        raise ValueError("The secret source-operation binding is invalid.")


def _is_capability_id(value: object) -> bool:
    if type(value) is not str or len(value) != ID_BASE32_LENGTH:
        return False
    padding = "=" * (-len(value) % 8)
    try:
        decoded = base64.b32decode(value.upper() + padding, casefold=False)
    except (ValueError, binascii.Error):
        return False
    return (
        len(decoded) == ID_ENTROPY_BYTES
        and any(decoded)
        and base64.b32encode(decoded).decode("ascii").rstrip("=").lower() == value
    )


def _validate_server_url(value: object) -> None:
    if value == "":
        return
    if type(value) is not str or len(value) > 2048 or not value.isascii():
        raise ValueError("The Bitwarden server binding is invalid.")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise ValueError("The Bitwarden server binding is invalid.") from None
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
        or port not in {None, 443}
    ):
        raise ValueError("The Bitwarden server binding is invalid.")


def _remaining_timeout(deadline: object, now: object) -> float:
    if (
        isinstance(deadline, bool)
        or not isinstance(deadline, int | float)
        or isinstance(now, bool)
        or not isinstance(now, int | float)
    ):
        raise ConnectorError(ConnectorErrorCode.SECRET_UNAVAILABLE)
    try:
        deadline_value = float(deadline)
        now_value = float(now)
    except (OverflowError, ValueError):
        raise ConnectorError(ConnectorErrorCode.SECRET_UNAVAILABLE) from None
    if not math.isfinite(deadline_value) or not math.isfinite(now_value):
        raise ConnectorError(ConnectorErrorCode.SECRET_UNAVAILABLE)
    remaining = deadline_value - now_value
    if remaining <= 0:
        raise ConnectorError(ConnectorErrorCode.SECRET_UNAVAILABLE)
    return min(remaining, float(DEFAULT_SECRET_HELPER_TIMEOUT_SECONDS))


def _validate_binding_runtime(
    binding: BitwardenSecretBinding, environment: Mapping[str, str]
) -> None:
    _validate_profile(binding.profile_home)
    _validate_profile_secret_sources_disabled(binding.profile_home)
    _reject_legacy_caches(binding.profile_home)
    _validate_bws_binary(binding)
    if binding.selector in environment or binding.injection_target in environment:
        raise ConnectorError(ConnectorErrorCode.SECRET_UNAVAILABLE)


def _validate_profile(profile_home: Path) -> None:
    descriptor = -1
    try:
        descriptor = _open_state_directory(profile_home, create=False)
    except (FileNotFoundError, OSError, ValueError):
        raise ConnectorError(ConnectorErrorCode.SECRET_UNAVAILABLE) from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _validate_profile_secret_sources_disabled(profile_home: Path) -> None:
    raw = _read_profile_file(profile_home, "config.yaml", _PROFILE_CONFIG_MAX_BYTES)
    if raw is None:
        return
    try:
        loaded = yaml.safe_load(raw.decode("utf-8", errors="strict"))
    except (UnicodeError, yaml.YAMLError):
        raise ConnectorError(ConnectorErrorCode.SECRET_UNAVAILABLE) from None
    if loaded is None:
        return
    if not isinstance(loaded, dict):
        raise ConnectorError(ConnectorErrorCode.SECRET_UNAVAILABLE)
    secrets_config = loaded.get("secrets")
    if secrets_config is None:
        return
    if not isinstance(secrets_config, dict):
        raise ConnectorError(ConnectorErrorCode.SECRET_UNAVAILABLE)
    for source_config in secrets_config.values():
        if not isinstance(source_config, dict):
            raise ConnectorError(ConnectorErrorCode.SECRET_UNAVAILABLE)
        enabled = source_config.get("enabled", False)
        if type(enabled) is not bool or enabled:
            raise ConnectorError(ConnectorErrorCode.SECRET_UNAVAILABLE)


def _read_profile_file(profile_home: Path, filename: str, maximum: int) -> bytes | None:
    directory_descriptor = -1
    descriptor = -1
    try:
        directory_descriptor = _open_state_directory(profile_home, create=False)
        try:
            descriptor = os.open(
                filename,
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                dir_fd=directory_descriptor,
            )
        except FileNotFoundError:
            return None
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_nlink != 1
            or not 0 <= metadata.st_size <= maximum
        ):
            raise OSError("unsafe profile configuration")
        chunks: list[bytes] = []
        remaining = metadata.st_size
        while remaining:
            chunk = os.read(descriptor, min(4096, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        if len(payload) != metadata.st_size:
            raise OSError("short profile configuration read")
        return payload
    except (OSError, ValueError):
        raise ConnectorError(ConnectorErrorCode.SECRET_UNAVAILABLE) from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if directory_descriptor >= 0:
            os.close(directory_descriptor)


def _reject_legacy_caches(profile_home: Path) -> None:
    for filename in _BWS_CACHE_FILENAMES:
        try:
            os.lstat(profile_home / "cache" / filename)
        except FileNotFoundError:
            continue
        except OSError:
            raise ConnectorError(ConnectorErrorCode.SECRET_UNAVAILABLE) from None
        raise ConnectorError(ConnectorErrorCode.SECRET_UNAVAILABLE)


def _validate_bws_binary(binding: BitwardenSecretBinding) -> None:
    directory_descriptor = -1
    descriptor = -1
    try:
        directory_descriptor = _open_state_directory(
            binding.bws_executable.parent, create=False
        )
        descriptor = os.open(
            binding.bws_executable.name,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
            dir_fd=directory_descriptor,
        )
        metadata = os.fstat(descriptor)
        mode = stat.S_IMODE(metadata.st_mode)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_nlink != 1
            or not 0 < metadata.st_size <= MAX_BWS_BINARY_BYTES
            or mode & 0o077
            or not mode & stat.S_IXUSR
        ):
            raise OSError("unsafe bws executable")
        with os.fdopen(os.dup(descriptor), "rb") as stream:
            digest = hashlib.file_digest(stream, "sha256")
        if digest.hexdigest() != binding.bws_sha256:
            raise OSError("bws executable digest mismatch")
    except (OSError, ValueError):
        raise ConnectorError(ConnectorErrorCode.SECRET_UNAVAILABLE) from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if directory_descriptor >= 0:
            os.close(directory_descriptor)


def _minimal_bws_environment(
    binding: BitwardenSecretBinding, parent: Mapping[str, str]
) -> dict[str, str]:
    environment = {
        "HOME": str(binding.profile_home),
        "HERMES_HOME": str(binding.profile_home),
        "NO_COLOR": "1",
        "PATH": str(binding.profile_home / "bin"),
    }
    for name in ("LANG", "LC_ALL", "SSL_CERT_DIR", "SSL_CERT_FILE", "TZ"):
        value = parent.get(name)
        if type(value) is str and value and "\x00" not in value:
            environment[name] = value
    return environment


def _minimal_helper_environment(
    binding: BitwardenSecretBinding,
    *,
    access_token: str,
    parent: Mapping[str, str],
) -> dict[str, str]:
    environment = _minimal_bws_environment(binding, parent)
    environment.update(
        {
            _BWS_ACCESS_TOKEN_ENV: access_token,
            "PYTHONNOUSERSITE": "1",
            "PYTHONUTF8": "1",
            "XDG_CACHE_HOME": str(binding.profile_home / ".cache-disabled"),
            "XDG_CONFIG_HOME": str(binding.profile_home / ".config-isolated"),
            "XDG_DATA_HOME": str(binding.profile_home / ".data-isolated"),
        }
    )
    return environment


def _read_bws_version(path: Path, environment: Mapping[str, str]) -> str:
    try:
        completed = subprocess.run(
            [str(path), "--version"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=dict(environment),
            cwd=path.parent.parent,
            timeout=5,
            check=False,
            close_fds=True,
            text=True,
        )
    except (OSError, subprocess.SubprocessError):
        raise ConnectorError(ConnectorErrorCode.SECRET_UNAVAILABLE) from None
    if completed.returncode != 0:
        raise ConnectorError(ConnectorErrorCode.SECRET_UNAVAILABLE)
    output = completed.stdout.strip().split()
    return _BWS_VERSION if _BWS_VERSION in output else ""


def _parse_helper_response(raw: bytearray) -> bytearray:
    try:
        if (
            type(raw) is not bytearray
            or len(raw) < _HELPER_RESPONSE_HEADER_BYTES
            or len(raw) > _HELPER_RESPONSE_HEADER_BYTES + MAX_SECRET_BYTES
            or raw[0] != _HELPER_SUCCESS
        ):
            raise ConnectorError(ConnectorErrorCode.SECRET_UNAVAILABLE)
        length = int.from_bytes(memoryview(raw)[1:5], "big")
        value = bytearray(memoryview(raw)[5:])
        if length != len(value) or not 0 < length <= MAX_SECRET_BYTES:
            value[:] = b"\x00" * len(value)
            raise ConnectorError(ConnectorErrorCode.SECRET_UNAVAILABLE)
        return value
    finally:
        raw[:] = b"\x00" * len(raw)


async def _communicate_bounded(
    process: asyncio.subprocess.Process, request: bytes
) -> bytearray:
    writer = process.stdin
    reader = process.stdout
    if writer is None or reader is None:
        raise ConnectorError(ConnectorErrorCode.SECRET_UNAVAILABLE)
    writer.write(request)
    await writer.drain()
    writer.close()

    maximum = _HELPER_RESPONSE_HEADER_BYTES + MAX_SECRET_BYTES
    response = bytearray()
    try:
        while len(response) <= maximum:
            chunk = await reader.read(min(8192, maximum + 1 - len(response)))
            if not chunk:
                break
            response.extend(chunk)
            if len(response) > maximum:
                raise ConnectorError(ConnectorErrorCode.SECRET_UNAVAILABLE)
        await process.wait()
        return response
    except BaseException:
        response[:] = b"\x00" * len(response)
        raise


async def _kill_process_group(process: asyncio.subprocess.Process) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except OSError:
        if process.returncode is None:
            process.kill()
    try:
        await process.wait()
    except (BrokenPipeError, ChildProcessError, ConnectionError, OSError):
        pass


__all__ = [
    "BitwardenSecretBinding",
    "BitwardenSecretProvider",
    "CapabilityId",
    "SecretBindingCatalog",
    "SecretMaterial",
    "SecretProvider",
    "SubprocessBitwardenHelper",
    "execute_with_secret",
    "prepare_secret_execution",
]
