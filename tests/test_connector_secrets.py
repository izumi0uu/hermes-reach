from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import os
import pickle
import signal
from collections.abc import Mapping
from pathlib import Path

import pytest

from hermes_reach.connector.authority import AuthorizedExecution
from hermes_reach.connector.errors import ConnectorError, ConnectorErrorCode
from hermes_reach.connector.identity import DevicePrivateIdentity
from hermes_reach.connector.limits import MAX_SECRET_BYTES
from hermes_reach.connector.protocol import (
    GrantScope,
    create_signed_request,
    protect_operation_call,
)
from hermes_reach.connector.secrets import (
    BitwardenSecretBinding,
    BitwardenSecretProvider,
    CapabilityId,
    SecretBindingCatalog,
    SecretMaterial,
    SubprocessBitwardenHelper,
    execute_with_secret,
    prepare_secret_execution,
)
from hermes_reach.connector.store import ClaimResult
from hermes_reach.contracts import validate_read

NOW = 1_800_000_000
PROJECT_ID = "12345678-1234-5678-9234-567812345678"


def _capability(slot: int) -> CapabilityId:
    return CapabilityId.new(lambda size: slot.to_bytes(size, "big"))


def _binding(
    tmp_path: Path,
    *,
    slot: int = 1,
    source: str = "web",
    operation: str = "read.url",
    selector: str = "UPSTREAM_KEY",
    injection_target: str = "EXECUTOR_KEY",
    revoked_at: int | None = None,
) -> BitwardenSecretBinding:
    profile = tmp_path / f"profile-{slot}"
    profile.mkdir(mode=0o700)
    profile.chmod(0o700)
    binary_directory = profile / "bin"
    binary_directory.mkdir(mode=0o700)
    binary_directory.chmod(0o700)
    binary = binary_directory / "bws"
    binary.write_bytes(b"#!/bin/sh\necho 'bws 2.0.0'\n")
    binary.chmod(0o700)
    return BitwardenSecretBinding(
        capability_id=_capability(slot),
        source=source,
        operation=operation,
        project_id=PROJECT_ID,
        selector=selector,
        injection_target=injection_target,
        profile_home=profile,
        bws_sha256=hashlib.sha256(binary.read_bytes()).hexdigest(),
        revoked_at=revoked_at,
    )


def _assert_code(error: pytest.ExceptionInfo[ConnectorError], code: str) -> None:
    assert error.value.code == code


def _execution(binding: BitwardenSecretBinding) -> AuthorizedExecution:
    vps = DevicePrivateIdentity._from_seed_for_testing(b"\x61" * 32)
    connector = DevicePrivateIdentity._from_seed_for_testing(b"\x62" * 32)
    call = validate_read(
        {
            "source": binding.source,
            "operation": binding.operation,
            "target": {"url": "https://example.com/private-query-canary"},
        }
    )
    protected = protect_operation_call(call)
    scope = GrantScope(
        binding.source,
        binding.operation,
        call.operation.runtime.data_scope,
        binding.capability_id.for_grant(),
    )
    request = create_signed_request(
        signer=vps,
        message_id=_capability(80).for_grant(),
        request_id=_capability(81).for_grant(),
        trace_id="1" * 32,
        audience_key_id=connector.public_identity.key_id,
        grant_id=_capability(82).for_grant(),
        grant_revision=1,
        policy_revision=1,
        source=binding.source,
        operation=binding.operation,
        issued_at=NOW,
        deadline=NOW + 60,
        protected_payload=protected,
    )
    return AuthorizedExecution(
        request,
        protected,
        scope,
        ClaimResult(True, None, 1, 9, "0" * 64),
    )


def test_capability_ids_are_canonical_opaque_and_grant_compatible() -> None:
    capability = _capability(1)
    assert len(capability.for_grant()) == 26
    assert CapabilityId.parse(capability.for_grant()) == capability
    assert repr(capability) == "CapabilityId(<opaque>)"
    assert capability.for_grant() not in repr(capability)
    assert GrantScope("web", "read.url", "public", capability.for_grant())

    for value in (None, "", "a" * 26, capability.for_grant().upper()):
        with pytest.raises(ValueError):
            CapabilityId.parse(value)
    with pytest.raises(ValueError):
        CapabilityId.new(lambda _: b"short")


def test_binding_catalog_is_closed_redacted_and_exact(tmp_path: Path) -> None:
    binding = _binding(tmp_path)
    catalog = SecretBindingCatalog((binding,))
    rendered = repr(binding) + repr(catalog)
    for forbidden in (
        binding.capability_id.for_grant(),
        binding.project_id,
        binding.selector,
        binding.injection_target,
        str(binding.profile_home),
        binding.bws_sha256,
    ):
        assert forbidden not in rendered
    assert (
        catalog.require_active(
            binding.capability_id,
            source=binding.source,
            operation=binding.operation,
        )
        is binding
    )

    with pytest.raises(ValueError):
        SecretBindingCatalog((binding, binding))
    with pytest.raises(ConnectorError) as wrong_operation:
        catalog.require_active(
            binding.capability_id,
            source=binding.source,
            operation="read.topic",
        )
    _assert_code(wrong_operation, ConnectorErrorCode.SECRET_BINDING_DENIED.value)


def test_revoked_and_unknown_bindings_fail_closed(tmp_path: Path) -> None:
    revoked = _binding(tmp_path, revoked_at=NOW)
    catalog = SecretBindingCatalog((revoked,))
    for capability in (revoked.capability_id, _capability(99)):
        with pytest.raises(ConnectorError) as denied:
            catalog.require_active(capability)
        _assert_code(denied, ConnectorErrorCode.SECRET_BINDING_DENIED.value)


def test_binding_validation_rejects_provider_metadata_ambiguity(tmp_path: Path) -> None:
    binding = _binding(tmp_path)
    fields = {
        "capability_id": binding.capability_id,
        "source": binding.source,
        "operation": binding.operation,
        "project_id": binding.project_id,
        "selector": binding.selector,
        "injection_target": binding.injection_target,
        "profile_home": binding.profile_home,
        "bws_sha256": binding.bws_sha256,
    }
    invalid = (
        {"project_id": "not-a-project"},
        {"selector": "BWS_ACCESS_TOKEN"},
        {"injection_target": "not-an-env-name"},
        {"profile_home": Path("relative-profile")},
        {"bws_sha256": "A" * 64},
        {"server_url": "http://vault.example"},
    )
    for override in invalid:
        with pytest.raises(ValueError):
            BitwardenSecretBinding(**(fields | override))


def test_secret_material_is_redacted_nonserializable_and_zeroed() -> None:
    source = bytearray(b"SECRET_VALUE_CANARY")
    material = SecretMaterial(source)
    assert set(source) == {0}
    assert repr(material) == "SecretMaterial(<redacted>)"
    assert str(material) == "SecretMaterial(<redacted>)"
    for serialize in (
        lambda: copy.copy(material),
        lambda: copy.deepcopy(material),
        lambda: pickle.dumps(material),
        lambda: json.dumps(material),
    ):
        with pytest.raises(TypeError):
            serialize()
    material.close()
    assert material.closed
    assert set(material._buffer) == {0}
    material.close()

    for malformed in (bytearray(b"contains\x00nul"), bytearray(b"\xff")):
        with pytest.raises(ConnectorError) as unavailable:
            SecretMaterial(malformed)
        _assert_code(unavailable, ConnectorErrorCode.SECRET_UNAVAILABLE.value)
        assert malformed and set(malformed) == {0}


def test_scoped_execution_injects_one_value_and_clears_on_success() -> None:
    material = SecretMaterial(bytearray(b"EXECUTOR_SECRET_CANARY"))
    seen: list[Mapping[str, str]] = []

    async def execute(environment: Mapping[str, str]) -> str:
        assert dict(environment) == {"EXECUTOR_KEY": "EXECUTOR_SECRET_CANARY"}
        assert environment is not os.environ
        seen.append(environment)
        return "ok"

    assert (
        asyncio.run(
            execute_with_secret(
                material,
                injection_target="EXECUTOR_KEY",
                execute=execute,
            )
        )
        == "ok"
    )
    assert material.closed
    assert dict(seen[0]) == {}


@pytest.mark.parametrize("failure", ["error", "cancel"])
def test_scoped_execution_clears_on_error_and_cancellation(failure: str) -> None:
    material = SecretMaterial(bytearray(b"EPHEMERAL_SECRET"))

    async def execute(_: Mapping[str, str]) -> None:
        if failure == "cancel":
            raise asyncio.CancelledError
        raise RuntimeError("unsafe provider stderr canary")

    expected = asyncio.CancelledError if failure == "cancel" else ConnectorError
    with pytest.raises(expected) as raised:
        asyncio.run(
            execute_with_secret(
                material,
                injection_target="EXECUTOR_KEY",
                execute=execute,
            )
        )
    if failure == "error":
        assert isinstance(raised.value, ConnectorError)
        assert raised.value.code == ConnectorErrorCode.SECRET_UNAVAILABLE.value
        assert "unsafe provider stderr canary" not in str(raised.value)
    assert material.closed
    assert set(material._buffer) == {0}


class _Launcher:
    def __init__(self, value: bytes = b"RESOLVED_SECRET") -> None:
        self.calls = 0
        self.value = value
        self.tokens: list[str] = []

    async def fetch(
        self,
        binding: BitwardenSecretBinding,
        *,
        access_token: str,
        deadline: float,
    ) -> bytearray:
        assert binding.provider == "bitwarden"
        assert deadline == 50.0
        self.calls += 1
        self.tokens.append(access_token)
        return bytearray(self.value)


def _provider(
    catalog: SecretBindingCatalog,
    launcher: _Launcher,
    *,
    environment: Mapping[str, str] | None = None,
    version: str = "2.0.0",
) -> BitwardenSecretProvider:
    return BitwardenSecretProvider(
        catalog,
        launcher=launcher,
        environment={"BWS_ACCESS_TOKEN": "BOOTSTRAP_TOKEN"}
        if environment is None
        else environment,
        clock=lambda: 10.0,
        version_reader=lambda _path, _environment: version,
    )


def test_provider_resolves_only_an_exact_fresh_binding(tmp_path: Path) -> None:
    binding = _binding(tmp_path)
    launcher = _Launcher()
    provider = _provider(SecretBindingCatalog((binding,)), launcher)

    with pytest.raises(ConnectorError) as stale:
        asyncio.run(
            provider.resolve(
                binding.capability_id,
                deadline=50.0,
                require_fresh=False,
            )
        )
    _assert_code(stale, ConnectorErrorCode.SECRET_UNAVAILABLE.value)
    assert launcher.calls == 0

    material = asyncio.run(
        provider.resolve(
            binding.capability_id,
            deadline=50.0,
            require_fresh=True,
        )
    )
    assert launcher.calls == 1
    assert launcher.tokens == ["BOOTSTRAP_TOKEN"]
    assert "BOOTSTRAP_TOKEN" not in repr(provider)
    material.close()


@pytest.mark.parametrize(
    "deadline",
    [True, float("nan"), float("inf"), 10**400, 10.0],
)
def test_provider_rejects_invalid_or_expired_deadlines_before_fetch(
    tmp_path: Path,
    deadline: object,
) -> None:
    binding = _binding(tmp_path)
    launcher = _Launcher()
    provider = _provider(SecretBindingCatalog((binding,)), launcher)

    with pytest.raises(ConnectorError) as unavailable:
        asyncio.run(
            provider.resolve(
                binding.capability_id,
                deadline=deadline,  # type: ignore[arg-type]
                require_fresh=True,
            )
        )
    _assert_code(unavailable, ConnectorErrorCode.SECRET_UNAVAILABLE.value)
    assert launcher.calls == 0


def test_unknown_binding_never_reads_token_or_launches(tmp_path: Path) -> None:
    binding = _binding(tmp_path)
    launcher = _Launcher()

    class _TokenTrap(dict[str, str]):
        def get(self, key: str, default: str | None = None) -> str:  # type: ignore[override]
            if key == "BWS_ACCESS_TOKEN":
                pytest.fail("unknown binding read the bootstrap token")
            return super().get(key, default)  # type: ignore[return-value]

    provider = _provider(
        SecretBindingCatalog((binding,)), launcher, environment=_TokenTrap()
    )
    with pytest.raises(ConnectorError) as denied:
        asyncio.run(
            provider.resolve(_capability(99), deadline=50.0, require_fresh=True)
        )
    _assert_code(denied, ConnectorErrorCode.SECRET_BINDING_DENIED.value)
    assert launcher.calls == 0


def test_profile_cache_plaintext_env_and_binary_drift_fail_before_fetch(
    tmp_path: Path,
) -> None:
    binding = _binding(tmp_path)
    catalog = SecretBindingCatalog((binding,))
    launcher = _Launcher()

    for environment in (
        {"BWS_ACCESS_TOKEN": "token", binding.selector: "plaintext"},
        {"BWS_ACCESS_TOKEN": "token", binding.injection_target: "plaintext"},
    ):
        with pytest.raises(ConnectorError) as ambient:
            _provider(catalog, launcher, environment=environment)
        _assert_code(ambient, ConnectorErrorCode.SECRET_UNAVAILABLE.value)

    cache = binding.profile_home / "cache"
    cache.mkdir(mode=0o700)
    for cache_name in ("bws_cache.json", "bws_cache.enc.json"):
        (cache / cache_name).write_text("SECRET_CACHE_CANARY")
        with pytest.raises(ConnectorError) as cached:
            _provider(catalog, launcher)
        _assert_code(cached, ConnectorErrorCode.SECRET_UNAVAILABLE.value)
        (cache / cache_name).unlink()

    binding.bws_executable.write_bytes(b"changed")
    binding.bws_executable.chmod(0o700)
    with pytest.raises(ConnectorError) as changed:
        _provider(catalog, launcher)
    _assert_code(changed, ConnectorErrorCode.SECRET_UNAVAILABLE.value)
    assert launcher.calls == 0


def test_profile_cache_directory_symlink_fails_closed(tmp_path: Path) -> None:
    binding = _binding(tmp_path)
    actual_cache = binding.profile_home / "actual-cache"
    actual_cache.mkdir(mode=0o700)
    (binding.profile_home / "cache").symlink_to(actual_cache, target_is_directory=True)

    with pytest.raises(ConnectorError) as unsafe:
        _provider(SecretBindingCatalog((binding,)), _Launcher())

    _assert_code(unsafe, ConnectorErrorCode.SECRET_UNAVAILABLE.value)


def test_bws_binary_parent_symlink_fails_closed(tmp_path: Path) -> None:
    binding = _binding(tmp_path)
    binary_directory = binding.bws_executable.parent
    actual_directory = binding.profile_home / "actual-bin"
    binary_directory.rename(actual_directory)
    binary_directory.symlink_to(actual_directory, target_is_directory=True)

    with pytest.raises(ConnectorError) as unsafe:
        _provider(SecretBindingCatalog((binding,)), _Launcher())
    _assert_code(unsafe, ConnectorErrorCode.SECRET_UNAVAILABLE.value)


def test_profile_global_secret_sources_and_bws_version_fail_closed(
    tmp_path: Path,
) -> None:
    binding = _binding(tmp_path)
    config = binding.profile_home / "config.yaml"
    config.write_text("secrets:\n  bitwarden:\n    enabled: true\n")
    config.chmod(0o600)
    with pytest.raises(ConnectorError) as enabled:
        _provider(SecretBindingCatalog((binding,)), _Launcher())
    _assert_code(enabled, ConnectorErrorCode.SECRET_UNAVAILABLE.value)

    config.write_text("secrets:\n  bitwarden:\n    enabled: false\n")
    config.chmod(0o600)
    with pytest.raises(ConnectorError) as version:
        _provider(SecretBindingCatalog((binding,)), _Launcher(), version="2.0.1")
    _assert_code(version, ConnectorErrorCode.SECRET_UNAVAILABLE.value)


class _ProviderSpy:
    def __init__(self, values: list[bytes] | None = None) -> None:
        self.calls = 0
        self.values = list(values or [b"SCOPED_VALUE"])

    async def resolve(
        self,
        capability_id: CapabilityId,
        *,
        deadline: float,
        require_fresh: bool,
    ) -> SecretMaterial:
        assert isinstance(capability_id, CapabilityId)
        assert deadline == 50.0
        assert require_fresh
        self.calls += 1
        return SecretMaterial(bytearray(self.values.pop(0)))


def test_authorized_handoff_checks_binding_before_provider(tmp_path: Path) -> None:
    binding = _binding(tmp_path)
    execution = _execution(binding)
    provider = _ProviderSpy()
    catalog = SecretBindingCatalog((binding,))

    async def execute(environment: Mapping[str, str]) -> str:
        assert dict(environment) == {binding.injection_target: "SCOPED_VALUE"}
        return "bounded-result"

    prepared = prepare_secret_execution(
        execution,
        catalog=catalog,
        provider=provider,
        injection_target=binding.injection_target,
        deadline=50.0,
        execute=execute,
    )
    assert provider.calls == 0
    assert asyncio.run(prepared) == "bounded-result"
    assert provider.calls == 1

    for target, denied_catalog in (
        ("WRONG_TARGET", catalog),
        (
            binding.injection_target,
            SecretBindingCatalog(
                (
                    _binding(
                        tmp_path,
                        slot=2,
                        selector="OTHER_SELECTOR",
                        injection_target="OTHER_TARGET",
                    ),
                )
            ),
        ),
    ):
        with pytest.raises(ConnectorError) as denied:
            prepare_secret_execution(
                execution,
                catalog=denied_catalog,
                provider=provider,
                injection_target=target,
                deadline=50.0,
                execute=execute,
            )
        _assert_code(denied, ConnectorErrorCode.SECRET_BINDING_DENIED.value)
    assert provider.calls == 1


def test_concurrent_scoped_executions_never_share_mappings(
    tmp_path: Path,
) -> None:
    binding = _binding(tmp_path)
    execution = _execution(binding)
    provider = _ProviderSpy([b"VALUE_ONE", b"VALUE_TWO"])
    catalog = SecretBindingCatalog((binding,))
    barrier = asyncio.Barrier(2)
    seen: list[Mapping[str, str]] = []

    async def execute(environment: Mapping[str, str]) -> str:
        seen.append(environment)
        value = environment[binding.injection_target]
        await barrier.wait()
        assert environment[binding.injection_target] == value
        return value

    calls = [
        prepare_secret_execution(
            execution,
            catalog=catalog,
            provider=provider,
            injection_target=binding.injection_target,
            deadline=50.0,
            execute=execute,
        )
        for _ in range(2)
    ]

    async def gather_calls() -> list[str]:
        return list(await asyncio.gather(*calls))

    assert set(asyncio.run(gather_calls())) == {"VALUE_ONE", "VALUE_TWO"}
    assert provider.calls == 2
    assert seen[0] is not seen[1]
    assert all(dict(environment) == {} for environment in seen)


class _FakeProcess:
    def __init__(self, response: bytes, *, returncode: int = 0) -> None:
        self.response = response
        self.returncode: int | None = None
        self._wait_returncode = returncode
        self.pid = 9876
        self.input: bytes | None = None
        self.stdin = _FakeWriter(self)
        self.stdout = _FakeReader(response)

    async def communicate(self, value: bytes | None = None) -> tuple[bytes, bytes]:
        self.input = value
        return self.response, b""

    async def wait(self) -> int:
        if self.returncode is None:
            self.returncode = self._wait_returncode
        return self.returncode


class _FakeWriter:
    def __init__(self, process: _FakeProcess) -> None:
        self._process = process

    def write(self, value: bytes) -> None:
        self._process.input = value

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        return None


class _FakeReader:
    def __init__(self, value: bytes) -> None:
        self._value = value

    async def read(self, maximum: int) -> bytes:
        value = self._value[:maximum]
        self._value = self._value[maximum:]
        return value


class _NeverReader:
    def __init__(self) -> None:
        self.entered = asyncio.Event()

    async def read(self, _: int) -> bytes:
        self.entered.set()
        await asyncio.Event().wait()
        return b""


def test_helper_launcher_uses_fixed_argv_and_minimal_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binding = _binding(tmp_path)
    process = _FakeProcess(b"\x00\x00\x00\x00\x06SECRET")
    captured: dict[str, object] = {}
    killed: list[tuple[int, signal.Signals]] = []

    async def create(*args: str, **kwargs: object) -> _FakeProcess:
        captured["args"] = args
        captured["kwargs"] = kwargs
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    monkeypatch.setattr(
        os,
        "killpg",
        lambda pid, requested_signal: killed.append((pid, requested_signal)),
    )
    launcher = SubprocessBitwardenHelper(
        environment={
            "BWS_ACCESS_TOKEN": "parent-token",
            "AWS_SECRET_ACCESS_KEY": "AMBIENT_CANARY",
            "HTTPS_PROXY": "http://proxy-canary",
            "LANG": "C.UTF-8",
        },
        clock=lambda: 10.0,
        python_executable=Path("/usr/bin/python3"),
    )
    value = asyncio.run(
        launcher.fetch(
            binding,
            access_token="BOOTSTRAP_CANARY",
            deadline=50.0,
        )
    )
    assert value == bytearray(b"SECRET")
    assert captured["args"] == (
        "/usr/bin/python3",
        "-I",
        "-m",
        "hermes_reach.connector.bitwarden_helper",
    )
    kwargs = captured["kwargs"]
    environment = kwargs["env"]
    assert isinstance(environment, dict)
    assert environment["BWS_ACCESS_TOKEN"] == "BOOTSTRAP_CANARY"
    assert "AWS_SECRET_ACCESS_KEY" not in environment
    assert "HTTPS_PROXY" not in environment
    argv = " ".join(captured["args"])
    assert binding.project_id not in argv
    assert binding.selector not in argv
    assert kwargs["stderr"] is asyncio.subprocess.DEVNULL
    assert kwargs["close_fds"] is True
    assert kwargs["start_new_session"] is True
    assert process.input is not None
    assert b"BOOTSTRAP_CANARY" not in process.input
    assert killed == []


@pytest.mark.parametrize(
    "terminal",
    ["timeout", "cancel", "oversize", "nonzero", "malformed", "success"],
)
def test_helper_launcher_signals_only_running_process_groups(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    terminal: str,
) -> None:
    binding = _binding(tmp_path)
    if terminal == "oversize":
        response = b"X" * (MAX_SECRET_BYTES + 6)
    elif terminal == "malformed":
        response = b"\x00\x00\x00\x00\x06BAD"
    elif terminal in {"nonzero", "success"}:
        response = b"\x00\x00\x00\x00\x06SECRET"
    else:
        response = b""
    process = _FakeProcess(response, returncode=7 if terminal == "nonzero" else 0)
    never_reader = _NeverReader()
    if terminal in {"timeout", "cancel"}:
        process.stdout = never_reader
    killed: list[tuple[int, signal.Signals]] = []

    async def create(*_: str, **__: object) -> _FakeProcess:
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    monkeypatch.setattr(
        os,
        "killpg",
        lambda pid, requested_signal: killed.append((pid, requested_signal)),
    )
    launcher = SubprocessBitwardenHelper(
        environment={"BWS_ACCESS_TOKEN": "parent-token"},
        clock=lambda: 10.0,
        python_executable=Path("/usr/bin/python3"),
    )

    async def exercise() -> None:
        task = asyncio.create_task(
            launcher.fetch(
                binding,
                access_token="BOOTSTRAP_CANARY",
                deadline=10.01 if terminal == "timeout" else 50.0,
            )
        )
        if terminal == "cancel":
            await never_reader.entered.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            return
        if terminal == "success":
            assert await task == bytearray(b"SECRET")
            return
        with pytest.raises(ConnectorError) as unavailable:
            await task
        _assert_code(unavailable, ConnectorErrorCode.SECRET_UNAVAILABLE.value)

    asyncio.run(exercise())
    expected = (
        [(process.pid, signal.SIGKILL)]
        if terminal in {"timeout", "cancel", "oversize"}
        else []
    )
    assert killed == expected


def test_secret_module_has_no_public_live_backend_registration() -> None:
    from hermes_reach.connector import secrets

    source = Path(secrets.__file__).read_text(encoding="utf-8")
    for forbidden in (
        "build_alpha1_registry",
        "AdapterBinding(",
        "os.environ.update",
        "shell=True",
        "apply_all(",
        "apply_bitwarden_secrets(",
    ):
        assert forbidden not in source
    assert "SecretMaterial(<redacted>)" in source
