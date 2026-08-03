"""Trusted-device activation for the capability-bound Xueqiu executor."""

from __future__ import annotations

import json
import os
import re
import stat
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Protocol, cast

from ..connector.errors import ConnectorError, ConnectorErrorCode
from ..connector.execution import ConnectorExecutionComposition
from ..connector.limits import SUPPORTED_CONNECTOR_PLATFORMS
from ..connector.protocol import GrantScope
from ..connector.secrets import (
    BitwardenSecretBinding,
    BitwardenSecretProvider,
    CapabilityId,
    SecretBindingCatalog,
    SecretProvider,
)
from .xueqiu import (
    XUEQIU_COOKIE_INJECTION_TARGET,
    xueqiu_execution_composition,
    xueqiu_scope,
)

_MANIFEST_PROTOCOL: Final = "v1"
_MAX_MANIFEST_BYTES: Final = 16 * 1024
_SHA256: Final = re.compile(r"[0-9a-f]{64}")
_MANIFEST_FIELDS: Final = frozenset(
    {
        "bws_sha256",
        "capability_id",
        "profile_home",
        "project_id",
        "protocol_version",
        "selector",
        "server_url",
    }
)


class _ProviderFactory(Protocol):
    def __call__(
        self,
        catalog: SecretBindingCatalog,
        *,
        environment: Mapping[str, str] | None = None,
    ) -> SecretProvider: ...


@dataclass(frozen=True, slots=True, repr=False)
class XueqiuActivation:
    """Closed activation result safe to pass into Connector composition."""

    scope: GrantScope
    composition: ConnectorExecutionComposition = field(repr=False)
    bws_sha256: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.scope, GrantScope)
            or not isinstance(self.composition, ConnectorExecutionComposition)
            or self.composition.required_scope(self.scope.source, self.scope.operation)
            != self.scope
            or type(self.bws_sha256) is not str
            or _SHA256.fullmatch(self.bws_sha256) is None
        ):
            raise ValueError("The Xueqiu activation is invalid.")

    def __repr__(self) -> str:
        return "XueqiuActivation(<redacted>)"


def activate_xueqiu_binding(
    manifest_path: Path,
    *,
    environment: Mapping[str, str] | None = None,
    provider_factory: _ProviderFactory = BitwardenSecretProvider,
    _platform: str | None = None,
) -> XueqiuActivation:
    """Validate one owner-only manifest and compose its exact executor."""

    _require_secure_platform(sys.platform if _platform is None else _platform)
    try:
        value = _read_manifest(manifest_path)
        capability = CapabilityId.parse(_string(value, "capability_id"))
        bws_sha256 = _string(value, "bws_sha256")
        binding = BitwardenSecretBinding(
            capability_id=capability,
            source="xueqiu",
            operation="search.stocks",
            project_id=_string(value, "project_id"),
            selector=_string(value, "selector"),
            injection_target=XUEQIU_COOKIE_INJECTION_TARGET,
            profile_home=Path(_string(value, "profile_home")),
            bws_sha256=bws_sha256,
            server_url=_string(value, "server_url", allow_empty=True),
        )
        catalog = SecretBindingCatalog((binding,))
        provider = provider_factory(catalog, environment=environment)
        composition = xueqiu_execution_composition(
            capability,
            catalog,
            provider,
        )
        return XueqiuActivation(
            xueqiu_scope(capability),
            composition,
            bws_sha256,
        )
    except ConnectorError:
        raise
    except Exception:
        raise ConnectorError(ConnectorErrorCode.SECRET_UNAVAILABLE) from None


def _read_manifest(path: Path) -> dict[str, object]:
    body = _read_owner_file(path)
    try:
        value = json.loads(
            body.decode("utf-8", errors="strict"),
            object_pairs_hook=_object_without_duplicates,
            parse_constant=lambda _: _raise_invalid_manifest(),
        )
    except (UnicodeError, ValueError, RecursionError):
        raise ValueError("invalid Xueqiu binding manifest") from None
    if (
        type(value) is not dict
        or set(value) != _MANIFEST_FIELDS
        or value.get("protocol_version") != _MANIFEST_PROTOCOL
    ):
        raise ValueError("invalid Xueqiu binding manifest")
    return cast(dict[str, object], value)


def _read_owner_file(path: Path) -> bytes:
    if not isinstance(path, Path) or not path.is_absolute() or ".." in path.parts:
        raise ValueError("invalid Xueqiu binding manifest")
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
    descriptor = -1
    try:
        descriptor = os.open(path, flags)
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_uid != os.geteuid()
            or stat.S_IMODE(before.st_mode) != 0o600
            or not 0 < before.st_size <= _MAX_MANIFEST_BYTES
        ):
            raise ValueError("invalid Xueqiu binding manifest")
        chunks = bytearray()
        remaining = before.st_size
        while remaining:
            chunk = os.read(descriptor, min(4096, remaining))
            if not chunk:
                raise ValueError("invalid Xueqiu binding manifest")
            chunks.extend(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise ValueError("invalid Xueqiu binding manifest")
        after = os.fstat(descriptor)
        if _stable_metadata(before) != _stable_metadata(after):
            raise ValueError("invalid Xueqiu binding manifest")
        return bytes(chunks)
    except ValueError:
        raise
    except OSError:
        raise ValueError("invalid Xueqiu binding manifest") from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _object_without_duplicates(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if type(key) is not str or key in value:
            raise ValueError("invalid Xueqiu binding manifest")
        value[key] = item
    return value


def _require_secure_platform(platform: str) -> None:
    if (
        platform not in SUPPORTED_CONNECTOR_PLATFORMS
        or not hasattr(os, "O_CLOEXEC")
        or not hasattr(os, "O_NOFOLLOW")
        or not hasattr(os, "geteuid")
    ):
        raise ConnectorError(ConnectorErrorCode.UNSUPPORTED_PLATFORM)


def _raise_invalid_manifest() -> None:
    raise ValueError("invalid Xueqiu binding manifest")


def _string(
    value: Mapping[str, object],
    field_name: str,
    *,
    allow_empty: bool = False,
) -> str:
    selected = value.get(field_name)
    if (
        type(selected) is not str
        or "\x00" in selected
        or (not selected and not allow_empty)
        or (selected and not selected.isprintable())
    ):
        raise ValueError("invalid Xueqiu binding manifest")
    return selected


def _stable_metadata(value: os.stat_result) -> tuple[int, ...]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_nlink,
        value.st_uid,
        value.st_gid,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


__all__ = ["XueqiuActivation", "activate_xueqiu_binding"]
