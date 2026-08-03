from __future__ import annotations

import asyncio
import base64
import time
from collections.abc import Mapping
from pathlib import Path

import pytest

from hermes_reach.connector.authority import AuthorizedExecution
from hermes_reach.connector.errors import ConnectorError, ConnectorErrorCode
from hermes_reach.connector.execution import (
    ConnectorExecutionComposition,
    ConnectorExecutorBinding,
    SecretExecutionPlan,
)
from hermes_reach.connector.identity import DevicePrivateIdentity
from hermes_reach.connector.protocol import (
    GrantScope,
    OperationResultItemV1,
    OperationResultV1,
    PublicBackendIdentity,
    create_signed_request,
    protect_operation_call,
)
from hermes_reach.connector.secrets import (
    BitwardenSecretBinding,
    CapabilityId,
    SecretBindingCatalog,
    SecretMaterial,
)
from hermes_reach.connector.store import ClaimResult
from hermes_reach.contracts import validate_read

NOW = 1_800_000_000
BACKEND = PublicBackendIdentity("reach-bounded-executor-v1", "1")


def _id(value: int) -> str:
    return base64.b32encode(value.to_bytes(16, "big")).decode().rstrip("=").lower()


def _execution(scope: GrantScope | None = None) -> AuthorizedExecution:
    effective_scope = scope or GrantScope("web", "read.url", "public")
    connector = DevicePrivateIdentity._from_seed_for_testing(bytes([10]) * 32)
    vps = DevicePrivateIdentity._from_seed_for_testing(bytes([11]) * 32)
    protected = protect_operation_call(
        validate_read(
            {
                "source": "web",
                "operation": "read.url",
                "target": {"url": "https://example.com/execution-canary"},
            }
        )
    )
    request = create_signed_request(
        vps,
        message_id=_id(1),
        request_id=_id(2),
        trace_id="1" * 32,
        audience_key_id=connector.public_identity.key_id,
        grant_id=_id(3),
        grant_revision=1,
        policy_revision=1,
        source="web",
        operation="read.url",
        issued_at=NOW,
        deadline=NOW + 30,
        protected_payload=protected,
    )
    return AuthorizedExecution(
        request,
        protected,
        effective_scope,
        ClaimResult(True, None, 1, 4, "a" * 64),
    )


class _Executor:
    def __init__(self, result: object | None = None) -> None:
        self.calls = 0
        self.environments: list[Mapping[str, str]] = []
        self._result = result

    async def execute(
        self, execution: AuthorizedExecution, environment: Mapping[str, str]
    ) -> OperationResultV1:
        self.calls += 1
        assert execution.operation_call().source.name == "web"
        self.environments.append(environment)
        if self._result is not None:
            return self._result  # type: ignore[return-value]
        return OperationResultV1(
            (OperationResultItemV1("content", "bounded fixture result"),), False
        )


class _SecretProvider:
    def __init__(self) -> None:
        self.calls = 0

    async def resolve(
        self,
        capability_id: CapabilityId,
        *,
        deadline: float,
        require_fresh: bool,
    ) -> SecretMaterial:
        self.calls += 1
        assert isinstance(capability_id, CapabilityId)
        assert deadline > time.monotonic()
        assert require_fresh is True
        return SecretMaterial(bytearray(b"fixture-secret"))


def _binding(
    executor: _Executor,
    *,
    scope: GrantScope | None = None,
    secret: SecretExecutionPlan | None = None,
    cleanup=None,  # type: ignore[no-untyped-def]
) -> ConnectorExecutorBinding:
    return ConnectorExecutorBinding(
        scope or GrantScope("web", "read.url", "public"),
        BACKEND,
        executor,
        cleanup,
        secret,
    )


def _assert_code(
    error: pytest.ExceptionInfo[ConnectorError], code: ConnectorErrorCode
) -> None:
    assert error.value.code == code.value


def test_empty_composition_is_redacted_and_fails_one_use_closed() -> None:
    composition = ConnectorExecutionComposition()
    execution = _execution()
    prepared = composition.prepare(execution, deadline=time.monotonic() + 1)

    assert repr(composition) == "ConnectorExecutionComposition(count=0)"
    assert repr(prepared) == "PreparedConnectorExecution(<redacted>)"
    assert prepared.backend is None
    with pytest.raises(ConnectorError) as unbound:
        asyncio.run(prepared.execute())
    _assert_code(unbound, ConnectorErrorCode.BACKEND_UNBOUND)
    with pytest.raises(ConnectorError) as replayed:
        asyncio.run(prepared.execute())
    _assert_code(replayed, ConnectorErrorCode.BACKEND_UNBOUND)


def test_composition_rejects_duplicates_and_scope_capability_mismatch() -> None:
    executor = _Executor()
    binding = _binding(executor)
    with pytest.raises(ValueError, match="duplicate"):
        ConnectorExecutionComposition((binding, binding))

    capability = CapabilityId.new(lambda size: bytes([7]) * size)
    secret_scope = GrantScope("web", "read.url", "public", capability.for_grant())
    with pytest.raises(ValueError, match="capability"):
        _binding(executor, scope=secret_scope)


def test_composition_combines_only_closed_nonoverlapping_registries() -> None:
    executor = _Executor()
    web = ConnectorExecutionComposition((_binding(executor),))
    rss_scope = GrantScope("rss", "read.feed", "public")
    rss = ConnectorExecutionComposition((_binding(executor, scope=rss_scope),))

    combined = ConnectorExecutionComposition.combine((web, rss))

    assert combined.required_scope("web", "read.url") == GrantScope(
        "web", "read.url", "public"
    )
    assert combined.required_scope("rss", "read.feed") == rss_scope
    with pytest.raises(ValueError, match="duplicate"):
        ConnectorExecutionComposition.combine((web, web))
    with pytest.raises(ValueError, match="invalid"):
        ConnectorExecutionComposition.combine((web, object()))  # type: ignore[arg-type]


def test_exact_execution_returns_only_bounded_result_and_is_one_use() -> None:
    executor = _Executor()
    composition = ConnectorExecutionComposition((_binding(executor),))
    execution = _execution()
    prepared = composition.prepare(execution, deadline=time.monotonic() + 1)

    result = asyncio.run(prepared.execute())

    assert result.items[0].text == "bounded fixture result"
    assert prepared.backend == BACKEND
    assert executor.calls == 1
    assert dict(executor.environments[0]) == {}
    with pytest.raises(TypeError):
        executor.environments[0]["FORBIDDEN"] = "value"  # type: ignore[index]
    with pytest.raises(ConnectorError) as replayed:
        asyncio.run(prepared.execute())
    _assert_code(replayed, ConnectorErrorCode.BACKEND_UNBOUND)
    assert executor.calls == 1


def test_over_budget_executor_result_is_rejected() -> None:
    oversized = OperationResultV1(
        tuple(OperationResultItemV1("content", str(index)) for index in range(21)),
        False,
    )
    prepared = ConnectorExecutionComposition((_binding(_Executor(oversized)),)).prepare(
        _execution(), deadline=time.monotonic() + 1
    )

    with pytest.raises(Exception, match="exceeds the operation bounds"):
        asyncio.run(prepared.execute())


def test_executor_failure_and_invalid_result_invoke_exact_cleanup() -> None:
    async def exercise() -> tuple[int, int]:
        cleanup_calls = 0

        async def cleanup() -> None:
            nonlocal cleanup_calls
            cleanup_calls += 1

        class _FailingExecutor(_Executor):
            async def execute(
                self,
                execution: AuthorizedExecution,
                environment: Mapping[str, str],
            ) -> OperationResultV1:
                del execution, environment
                self.calls += 1
                raise RuntimeError("private executor detail")

        failing = _FailingExecutor()
        failed = ConnectorExecutionComposition(
            (_binding(failing, cleanup=cleanup),)
        ).prepare(_execution(), deadline=asyncio.get_running_loop().time() + 1)
        with pytest.raises(ConnectorError) as caught:
            await failed.execute()
        _assert_code(caught, ConnectorErrorCode.CONNECTOR_STATE_INVALID)
        assert "private executor detail" not in str(caught.value)

        invalid = ConnectorExecutionComposition(
            (_binding(_Executor(object()), cleanup=cleanup),)
        ).prepare(_execution(), deadline=asyncio.get_running_loop().time() + 1)
        with pytest.raises(Exception, match="exceeds the operation bounds"):
            await invalid.execute()
        return cleanup_calls, failing.calls

    assert asyncio.run(exercise()) == (2, 1)


def test_secret_plan_exposes_one_entry_only_for_executor_call(tmp_path: Path) -> None:
    capability = CapabilityId.new(lambda size: bytes([8]) * size)
    scope = GrantScope("web", "read.url", "public", capability.for_grant())
    local_binding = BitwardenSecretBinding(
        capability,
        "web",
        "read.url",
        "12345678-1234-4234-8234-123456789abc",
        "FIXTURE_SELECTOR",
        "FIXTURE_TARGET",
        tmp_path,
        "a" * 64,
    )
    provider = _SecretProvider()
    executor = _Executor()
    secret = SecretExecutionPlan(
        SecretBindingCatalog((local_binding,)), provider, "FIXTURE_TARGET"
    )
    prepared = ConnectorExecutionComposition(
        (_binding(executor, scope=scope, secret=secret),)
    ).prepare(_execution(scope), deadline=time.monotonic() + 1)

    asyncio.run(prepared.execute())

    assert provider.calls == 1
    assert executor.calls == 1
    assert dict(executor.environments[0]) == {}


def test_cancellation_invokes_exact_cleanup_once() -> None:
    async def exercise() -> tuple[int, int]:
        started = asyncio.Event()
        released = asyncio.Event()
        cleanup_calls = 0

        class _BlockingExecutor(_Executor):
            async def execute(
                self,
                execution: AuthorizedExecution,
                environment: Mapping[str, str],
            ) -> OperationResultV1:
                del execution, environment
                self.calls += 1
                started.set()
                await released.wait()
                return OperationResultV1((), False)

        async def cleanup() -> None:
            nonlocal cleanup_calls
            cleanup_calls += 1
            released.set()

        executor = _BlockingExecutor()
        prepared = ConnectorExecutionComposition(
            (_binding(executor, cleanup=cleanup),)
        ).prepare(_execution(), deadline=asyncio.get_running_loop().time() + 10)
        task = asyncio.create_task(prepared.execute())
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return executor.calls, cleanup_calls

    assert asyncio.run(exercise()) == (1, 1)
