from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path

import pytest

from hermes_reach.connector.errors import ConnectorError, ConnectorErrorCode
from hermes_reach.connector.secrets import (
    CapabilityId,
    SecretBindingCatalog,
    SecretMaterial,
)
from hermes_reach.sources.xueqiu_activation import activate_xueqiu_binding

_PROJECT_ID = "12345678-1234-4234-8234-123456789abc"
_SELECTOR = "XUEQIU_COOKIE"
_BWS_SHA256 = "a" * 64
_TOKEN_CANARY = "TOKEN_CANARY"


class _Provider:
    def __init__(self) -> None:
        self.calls = 0

    async def resolve(
        self,
        capability_id: CapabilityId,
        *,
        deadline: float,
        require_fresh: bool,
    ) -> SecretMaterial:
        del capability_id, deadline, require_fresh
        self.calls += 1
        raise AssertionError("activation must not resolve a secret")


class _ProviderFactory:
    def __init__(self) -> None:
        self.calls = 0
        self.binding = None
        self.environment: Mapping[str, str] | None = None
        self.provider = _Provider()

    def __call__(
        self,
        catalog: SecretBindingCatalog,
        *,
        environment: Mapping[str, str] | None = None,
    ) -> _Provider:
        self.calls += 1
        bindings = catalog.active_bindings()
        assert len(bindings) == 1
        self.binding = bindings[0]
        self.environment = environment
        return self.provider


def _manifest_value(tmp_path: Path) -> dict[str, object]:
    capability = CapabilityId.new(lambda size: b"\x06" * size).for_grant()
    return {
        "bws_sha256": _BWS_SHA256,
        "capability_id": capability,
        "profile_home": str(tmp_path / "bitwarden-profile"),
        "project_id": _PROJECT_ID,
        "protocol_version": "v1",
        "selector": _SELECTOR,
        "server_url": "",
    }


def _write_manifest(path: Path, value: dict[str, object]) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=True, separators=(",", ":")),
        encoding="utf-8",
    )
    path.chmod(0o600)


def test_owner_manifest_activates_one_opaque_scope_without_secret_resolution(
    tmp_path: Path,
) -> None:
    path = tmp_path / "xueqiu-binding.json"
    value = _manifest_value(tmp_path)
    _write_manifest(path, value)
    factory = _ProviderFactory()
    environment = {"BWS_ACCESS_TOKEN": _TOKEN_CANARY}

    activation = activate_xueqiu_binding(
        path,
        environment=environment,
        provider_factory=factory,
    )

    assert activation.scope.source == "xueqiu"
    assert activation.scope.operation == "search.stocks"
    assert activation.scope.data_scope == "public"
    assert activation.scope.capability_id == value["capability_id"]
    assert activation.bws_sha256 == _BWS_SHA256
    assert (
        activation.composition.required_scope("xueqiu", "search.stocks")
        == activation.scope
    )
    assert factory.calls == 1
    assert factory.provider.calls == 0
    assert factory.environment is environment
    assert factory.binding is not None
    assert factory.binding.project_id == _PROJECT_ID
    assert factory.binding.selector == _SELECTOR
    assert factory.binding.server_url == ""
    rendered = repr(activation)
    assert _PROJECT_ID not in rendered
    assert _SELECTOR not in rendered
    assert _TOKEN_CANARY not in rendered
    assert str(path) not in rendered


@pytest.mark.parametrize("mode", [0o700, 0o644, 0o640, 0o606, 0o400])
def test_manifest_rejects_non_owner_only_modes_before_provider_creation(
    tmp_path: Path,
    mode: int,
) -> None:
    path = tmp_path / "xueqiu-binding.json"
    _write_manifest(path, _manifest_value(tmp_path))
    path.chmod(mode)
    factory = _ProviderFactory()

    with pytest.raises(ConnectorError) as caught:
        activate_xueqiu_binding(path, provider_factory=factory)

    assert caught.value.code == ConnectorErrorCode.SECRET_UNAVAILABLE.value
    assert factory.calls == 0


@pytest.mark.parametrize("link_kind", ["symbolic", "hard"])
def test_manifest_rejects_links_before_provider_creation(
    tmp_path: Path,
    link_kind: str,
) -> None:
    target = tmp_path / "target.json"
    selected = tmp_path / "selected.json"
    _write_manifest(target, _manifest_value(tmp_path))
    if link_kind == "symbolic":
        selected.symlink_to(target)
    else:
        os.link(target, selected)
    factory = _ProviderFactory()

    with pytest.raises(ConnectorError) as caught:
        activate_xueqiu_binding(selected, provider_factory=factory)

    assert caught.value.code == ConnectorErrorCode.SECRET_UNAVAILABLE.value
    assert factory.calls == 0


def test_manifest_rejects_duplicate_keys_and_unknown_fields(
    tmp_path: Path,
) -> None:
    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text(
        '{"protocol_version":"v1","protocol_version":"v1"}',
        encoding="utf-8",
    )
    duplicate.chmod(0o600)
    unknown = tmp_path / "unknown.json"
    value = {**_manifest_value(tmp_path), "provider": "bitwarden"}
    _write_manifest(unknown, value)

    for path in (duplicate, unknown):
        factory = _ProviderFactory()
        with pytest.raises(ConnectorError) as caught:
            activate_xueqiu_binding(path, provider_factory=factory)
        assert caught.value.code == ConnectorErrorCode.SECRET_UNAVAILABLE.value
        assert factory.calls == 0


def test_manifest_rejects_relative_paths_without_reading_them(tmp_path: Path) -> None:
    path = tmp_path / "relative.json"
    _write_manifest(path, _manifest_value(tmp_path))
    factory = _ProviderFactory()

    with pytest.raises(ConnectorError) as caught:
        activate_xueqiu_binding(Path("relative.json"), provider_factory=factory)

    assert caught.value.code == ConnectorErrorCode.SECRET_UNAVAILABLE.value
    assert factory.calls == 0
