"""One-shot child process for an isolated Hermes Bitwarden fetch."""

from __future__ import annotations

import hashlib
import importlib
import inspect
import os
import re
import sys
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from importlib.machinery import PathFinder
from importlib.metadata import distribution, version
from pathlib import Path
from types import ModuleType
from typing import Final, Protocol, cast

from .limits import MAX_SECRET_BYTES, MAX_SECRET_HELPER_INPUT_BYTES
from .protocol import load_canonical_json
from .secrets import _validate_server_url

_HELPER_VERSION: Final = 1
_SUCCESS: Final = b"\x00"
_UNAVAILABLE: Final = b"\x01"
_EXPECTED_HERMES_VERSION: Final = "0.19.0"
_EXPECTED_BWS_VERSION: Final = "2.0.0"
_EXPECTED_SOURCE_HASHES: Final[Mapping[str, str]] = {
    "base": "37a7a9683ab57f3a94a535946491401686a7ad8cb54d957c65b3dfaebe2a6690",
    "bitwarden": "1b04de42849482af965e0715cdfb7b9dc73c83f9b701cacb33f3048e97097490",
    "cache": "012f5a069c5ef1e7b58ace4adb77aa1d64241ea81b6304132c00668879f99f55",
}
_HERMES_SOURCE_PATHS: Final[Mapping[str, str]] = {
    "base": "agent/secret_sources/base.py",
    "bitwarden": "agent/secret_sources/bitwarden.py",
    "cache": "agent/secret_sources/_cache.py",
}
_ENV_NAME: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}\Z")
_ALLOWED_ENVIRONMENT: Final = frozenset(
    {
        "BWS_ACCESS_TOKEN",
        "HOME",
        "HERMES_HOME",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "NO_COLOR",
        "PATH",
        "PYTHONNOUSERSITE",
        "PYTHONUTF8",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "TZ",
        "XDG_CACHE_HOME",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
    }
)


class _FetchSource(Protocol):
    api_version: int
    name: str
    shape: str

    def fetch(self, cfg: dict[str, object], home_path: Path) -> object: ...

    def config_schema(self) -> dict[str, dict[str, object]]: ...


class _FetchResult(Protocol):
    secrets: dict[str, str]
    error: str | None
    error_kind: object | None
    applied: list[str]
    skipped: list[str]
    warnings: list[str]
    binary_path: Path | None


@dataclass(frozen=True, slots=True)
class _HermesRuntime:
    base: ModuleType
    bitwarden: ModuleType
    cache: ModuleType
    source_type: type[_FetchSource]
    fetch_result_type: type[object]


@dataclass(frozen=True, slots=True, repr=False)
class _HelperRequest:
    profile_home: Path
    project_id: str
    selector: str
    server_url: str

    def __repr__(self) -> str:
        return "_HelperRequest(<redacted>)"


def _parse_request(raw: bytes) -> _HelperRequest:
    value = load_canonical_json(raw, max_bytes=MAX_SECRET_HELPER_INPUT_BYTES)
    if not isinstance(value, dict) or set(value) != {
        "profile_home",
        "project_id",
        "selector",
        "server_url",
        "version",
    }:
        raise ValueError("The Bitwarden helper request is invalid.")
    helper_version = value.get("version")
    if type(helper_version) is not int or helper_version != _HELPER_VERSION:
        raise ValueError("The Bitwarden helper request is invalid.")
    profile_value = value.get("profile_home")
    project_id = value.get("project_id")
    selector = value.get("selector")
    server_url = value.get("server_url")
    if (
        type(profile_value) is not str
        or type(project_id) is not str
        or type(selector) is not str
        or type(server_url) is not str
    ):
        raise ValueError("The Bitwarden helper request is invalid.")
    profile_home = Path(profile_value)
    if not profile_home.is_absolute() or _ENV_NAME.fullmatch(selector) is None:
        raise ValueError("The Bitwarden helper request is invalid.")
    try:
        project = uuid.UUID(project_id)
        _validate_server_url(server_url)
    except ValueError:
        raise ValueError("The Bitwarden helper request is invalid.") from None
    if str(project) != project_id:
        raise ValueError("The Bitwarden helper request is invalid.")
    return _HelperRequest(profile_home, project_id, selector, server_url)


def _assert_compatibility() -> None:
    if version("hermes-agent") != _EXPECTED_HERMES_VERSION:
        raise RuntimeError("The Hermes Bitwarden contract is incompatible.")
    installed = distribution("hermes-agent")
    source_paths: dict[str, Path] = {}
    for name, relative_path in _HERMES_SOURCE_PATHS.items():
        source_path = Path(str(installed.locate_file(relative_path))).resolve()
        try:
            with source_path.open("rb") as source:
                digest = hashlib.file_digest(source, "sha256").hexdigest()
        except OSError:
            raise RuntimeError(
                "The Hermes Bitwarden contract is incompatible."
            ) from None
        if digest != _EXPECTED_SOURCE_HASHES[name]:
            raise RuntimeError("The Hermes Bitwarden contract is incompatible.")
        source_paths[name] = source_path

    _assert_import_resolution(source_paths)
    runtime = _load_hermes_runtime()
    _assert_loaded_sources(runtime, source_paths)
    if (
        getattr(runtime.base, "SECRET_SOURCE_API_VERSION", None) != 1
        or runtime.source_type.api_version != 1
        or runtime.source_type.name != "bitwarden"
        or runtime.source_type.shape != "bulk"
        or getattr(runtime.bitwarden, "_BWS_VERSION", None) != _EXPECTED_BWS_VERSION
        or getattr(runtime.bitwarden, "_DISK_CACHE_BASENAME", None) != "bws_cache.json"
    ):
        raise RuntimeError("The Hermes Bitwarden contract is incompatible.")
    signature = inspect.signature(runtime.source_type.fetch)
    if tuple(signature.parameters) != ("self", "cfg", "home_path"):
        raise RuntimeError("The Hermes Bitwarden contract is incompatible.")
    schema = runtime.source_type().config_schema()
    if (
        not isinstance(schema, dict)
        or schema.get("cache_ttl_seconds", {}).get("default") != 300
        or schema.get("auto_install", {}).get("default") is not True
    ):
        raise RuntimeError("The Hermes Bitwarden contract is incompatible.")


def _load_hermes_runtime() -> _HermesRuntime:
    base = importlib.import_module("agent.secret_sources.base")
    cache = importlib.import_module("agent.secret_sources._cache")
    bitwarden = importlib.import_module("agent.secret_sources.bitwarden")
    source_type = getattr(bitwarden, "BitwardenSource", None)
    fetch_result_type = getattr(base, "FetchResult", None)
    if not isinstance(source_type, type) or not isinstance(fetch_result_type, type):
        raise RuntimeError("The Hermes Bitwarden contract is incompatible.")
    return _HermesRuntime(
        base,
        bitwarden,
        cache,
        cast(type[_FetchSource], source_type),
        cast(type[object], fetch_result_type),
    )


def _assert_import_resolution(source_paths: Mapping[str, Path]) -> None:
    base_path = source_paths.get("base")
    if base_path is None:
        raise RuntimeError("The Hermes Bitwarden contract is incompatible.")
    secret_sources_directory = base_path.parent
    agent_directory = secret_sources_directory.parent

    agent_locations = _require_spec_path(
        "agent",
        None,
        agent_directory / "__init__.py",
    )
    secret_source_locations = _require_spec_path(
        "agent.secret_sources",
        agent_locations,
        secret_sources_directory / "__init__.py",
    )
    for name, module_name in (
        ("base", "agent.secret_sources.base"),
        ("cache", "agent.secret_sources._cache"),
        ("bitwarden", "agent.secret_sources.bitwarden"),
    ):
        expected = source_paths.get(name)
        if expected is None:
            raise RuntimeError("The Hermes Bitwarden contract is incompatible.")
        _require_spec_path(module_name, secret_source_locations, expected)


def _require_spec_path(
    module_name: str,
    search_path: Sequence[str] | None,
    expected: Path,
) -> tuple[str, ...]:
    try:
        spec = PathFinder.find_spec(module_name, search_path)
        origin = Path(str(spec.origin)).resolve(strict=True) if spec else None
        expected_path = expected.resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        raise RuntimeError("The Hermes Bitwarden contract is incompatible.") from None
    if spec is None or origin != expected_path:
        raise RuntimeError("The Hermes Bitwarden contract is incompatible.")
    return tuple(spec.submodule_search_locations or ())


def _assert_loaded_sources(
    runtime: _HermesRuntime, source_paths: Mapping[str, Path]
) -> None:
    expected_modules: dict[str, tuple[object, Path]] = {
        "base": (runtime.base, source_paths["base"]),
        "bitwarden": (runtime.bitwarden, source_paths["bitwarden"]),
        "cache": (runtime.cache, source_paths["cache"]),
    }
    for module, expected in expected_modules.values():
        module_path = getattr(module, "__file__", None)
        if type(module_path) is not str:
            raise RuntimeError("The Hermes Bitwarden contract is incompatible.")
        try:
            actual = Path(module_path).resolve(strict=True)
            expected_path = expected.resolve(strict=True)
        except (OSError, TypeError, ValueError):
            raise RuntimeError(
                "The Hermes Bitwarden contract is incompatible."
            ) from None
        if actual != expected_path:
            raise RuntimeError("The Hermes Bitwarden contract is incompatible.")


def _fetch_selected_secret(
    request: _HelperRequest,
    *,
    source_factory: Callable[[], _FetchSource] | None = None,
    compatibility_check: Callable[[], None] = _assert_compatibility,
) -> bytearray:
    compatibility_check()
    runtime = _load_hermes_runtime()
    expected_home = str(request.profile_home)
    if (
        os.environ.get("HERMES_HOME") != expected_home
        or os.environ.get("HOME") != expected_home
        or os.environ.get("BWS_SERVER_URL") is not None
    ):
        raise RuntimeError("The helper environment is incompatible.")
    before = dict(os.environ)
    result: _FetchResult | None = None
    try:
        source = runtime.source_type() if source_factory is None else source_factory()
        fetched = source.fetch(
            {
                "access_token_env": "BWS_ACCESS_TOKEN",
                "auto_install": False,
                "cache_ttl_seconds": 0,
                "enabled": True,
                "encrypted_cache": {
                    "enabled": False,
                    "max_stale_seconds": 0,
                },
                "project_id": request.project_id,
                "server_url": request.server_url,
            },
            request.profile_home,
        )
        expected_binary = request.profile_home / "bin" / "bws"
        if type(fetched) is not runtime.fetch_result_type:
            raise RuntimeError("The Hermes Bitwarden fetch failed.")
        result = cast(_FetchResult, fetched)
        if (
            result.error is not None
            or result.error_kind is not None
            or result.applied
            or result.skipped
            or result.warnings
            or result.binary_path != expected_binary
            or not isinstance(result.secrets, dict)
            or os.environ != before
        ):
            raise RuntimeError("The Hermes Bitwarden fetch failed.")
        selected = result.secrets.get(request.selector)
        if type(selected) is not str:
            raise RuntimeError("The Bitwarden binding is unavailable.")
        material = bytearray(selected, "utf-8", errors="strict")
        if not material or len(material) > MAX_SECRET_BYTES or 0 in material:
            material[:] = b"\x00" * len(material)
            raise RuntimeError("The Bitwarden binding is unavailable.")
        return material
    finally:
        if result is not None and isinstance(result.secrets, dict):
            result.secrets.clear()


def _sanitize_environment() -> None:
    for name in tuple(os.environ):
        if name not in _ALLOWED_ENVIRONMENT:
            os.environ.pop(name, None)


def main() -> int:
    material: bytearray | None = None
    try:
        _sanitize_environment()
        raw = sys.stdin.buffer.read(MAX_SECRET_HELPER_INPUT_BYTES + 1)
        if not raw or len(raw) > MAX_SECRET_HELPER_INPUT_BYTES:
            raise ValueError("The Bitwarden helper request is invalid.")
        request = _parse_request(raw)
        material = _fetch_selected_secret(request)
        sys.stdout.buffer.write(_SUCCESS)
        sys.stdout.buffer.write(len(material).to_bytes(4, "big"))
        sys.stdout.buffer.write(memoryview(material))
        sys.stdout.buffer.flush()
        return 0
    except BaseException:
        try:
            sys.stdout.buffer.write(_UNAVAILABLE)
            sys.stdout.buffer.flush()
        except BaseException:
            pass
        return 1
    finally:
        if material is not None:
            material[:] = b"\x00" * len(material)


if __name__ == "__main__":
    raise SystemExit(main())
