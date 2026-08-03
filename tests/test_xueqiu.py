from __future__ import annotations

import asyncio
import base64
import signal
import time
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import cast

import pytest

import hermes_reach.sources.xueqiu as xueqiu
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
from hermes_reach.contracts import OperationCall, validate_search
from hermes_reach.sources.xueqiu_worker import (
    ForkExecutionFailure,
    WorkerResponse,
    XueqiuProjection,
    XueqiuStockProjection,
)

NOW = 1_800_000_000
COOKIE_CANARY = "xq_a_token=connector-secret-canary; u=fixture"


def _id(value: int) -> str:
    return base64.b32encode(value.to_bytes(16, "big")).decode().rstrip("=").lower()


def _capability(value: int = 70) -> CapabilityId:
    return CapabilityId.new(lambda size: bytes([value]) * size)


def _call(*, limit: int = 2) -> OperationCall:
    return validate_search(
        {
            "requests": [
                {
                    "source": "xueqiu",
                    "operation": "search.stocks",
                    "query": "600519",
                    "options": {"limit": limit},
                }
            ]
        }
    )[0]


def _execution(call: OperationCall, scope: GrantScope) -> AuthorizedExecution:
    connector = DevicePrivateIdentity._from_seed_for_testing(bytes([71]) * 32)
    vps = DevicePrivateIdentity._from_seed_for_testing(bytes([72]) * 32)
    protected = protect_operation_call(call)
    request = create_signed_request(
        vps,
        message_id=_id(1),
        request_id=_id(2),
        trace_id="d" * 32,
        audience_key_id=connector.public_identity.key_id,
        grant_id=_id(3),
        grant_revision=1,
        policy_revision=1,
        source=call.source.name,
        operation=call.operation.name,
        issued_at=NOW,
        deadline=NOW + 20,
        protected_payload=protected,
    )
    return AuthorizedExecution(
        request,
        protected,
        scope,
        ClaimResult(True, None, 1, 4, "e" * 64),
    )


class _Worker:
    def __init__(self, response: WorkerResponse | None = None) -> None:
        self.response = response or XueqiuProjection((), False)
        self.calls: list[tuple[str, int, float]] = []
        self.cookie_seen = False

    async def execute(
        self,
        query: str,
        limit: int,
        cookie_header: str,
        *,
        deadline: float,
    ) -> WorkerResponse:
        self.calls.append((query, limit, deadline))
        self.cookie_seen = cookie_header == COOKIE_CANARY
        return self.response


class _SecretProvider:
    def __init__(self) -> None:
        self.calls = 0
        self.material: SecretMaterial | None = None

    async def resolve(
        self,
        capability_id: CapabilityId,
        *,
        deadline: float,
        require_fresh: bool,
    ) -> SecretMaterial:
        assert isinstance(capability_id, CapabilityId)
        assert deadline > time.monotonic()
        assert require_fresh is True
        self.calls += 1
        source = bytearray(COOKIE_CANARY.encode())
        self.material = SecretMaterial(source)
        assert not any(source)
        return self.material


class _ObservedEnvironment(Mapping[str, str]):
    def __init__(self) -> None:
        self.iterations = 0
        self.length_reads = 0
        self.value_reads = 0

    def __getitem__(self, key: str) -> str:
        self.value_reads += 1
        if key != xueqiu.XUEQIU_COOKIE_INJECTION_TARGET:
            raise KeyError(key)
        return COOKIE_CANARY

    def __iter__(self) -> Iterator[str]:
        self.iterations += 1
        yield xueqiu.XUEQIU_COOKIE_INJECTION_TARGET

    def __len__(self) -> int:
        self.length_reads += 1
        return 1

    @property
    def observations(self) -> tuple[int, int, int]:
        return self.iterations, self.length_reads, self.value_reads


def _binding(
    tmp_path: Path,
    capability: CapabilityId,
) -> BitwardenSecretBinding:
    return BitwardenSecretBinding(
        capability,
        "xueqiu",
        "search.stocks",
        "12345678-1234-4234-8234-123456789abc",
        "XUEQIU_COOKIE",
        xueqiu.XUEQIU_COOKIE_INJECTION_TARGET,
        tmp_path,
        "a" * 64,
    )


def _secret_composition(
    tmp_path: Path,
    worker: _Worker,
) -> tuple[
    ConnectorExecutionComposition,
    GrantScope,
    _SecretProvider,
    BitwardenSecretBinding,
]:
    capability = _capability()
    scope = xueqiu.xueqiu_scope(capability)
    binding = _binding(tmp_path, capability)
    catalog = SecretBindingCatalog((binding,))
    provider = _SecretProvider()
    executor = xueqiu.XueqiuExecutor(
        scope,
        worker,
        wall_clock=lambda: float(NOW),
        monotonic_clock=lambda: 100.0,
    )
    composition = ConnectorExecutionComposition(
        (
            ConnectorExecutorBinding(
                scope,
                xueqiu.XUEQIU_BACKEND,
                executor,
                secret=SecretExecutionPlan(
                    catalog,
                    provider,
                    xueqiu.XUEQIU_COOKIE_INJECTION_TARGET,
                ),
            ),
        )
    )
    return composition, scope, provider, binding


def test_authorized_secret_is_resolved_once_and_only_for_the_worker_frame(
    tmp_path: Path,
) -> None:
    worker = _Worker(
        XueqiuProjection(
            (
                XueqiuStockProjection("SH600519", "Kweichow Moutai", "SH"),
                XueqiuStockProjection("NASDAQ:AAPL", "Apple", "NASDAQ"),
            ),
            False,
        )
    )
    composition, scope, provider, binding = _secret_composition(tmp_path, worker)
    prepared = composition.prepare(
        _execution(_call(), scope),
        deadline=time.monotonic() + 30,
    )

    result = asyncio.run(prepared.execute())

    assert provider.calls == 1
    assert provider.material is not None and provider.material.closed is True
    assert worker.cookie_seen is True
    assert [(item.kind, item.native_id, item.text) for item in result.items] == [
        ("result", "SH600519", "Kweichow Moutai | Symbol: SH600519 | Exchange: SH"),
        (
            "result",
            "NASDAQ:AAPL",
            "Apple | Symbol: NASDAQ:AAPL | Exchange: NASDAQ",
        ),
    ]
    assert COOKIE_CANARY not in repr(composition)
    assert COOKIE_CANARY not in repr(prepared)
    assert COOKIE_CANARY not in repr(binding)
    assert COOKIE_CANARY not in repr(result)


def test_executor_caps_limit_at_runtime_bound_and_marks_truncation() -> None:
    capability = _capability()
    scope = xueqiu.xueqiu_scope(capability)
    worker = _Worker(
        XueqiuProjection(
            tuple(
                XueqiuStockProjection(
                    f"SH{600_000 + index:06d}",
                    f"Stock {index}",
                    "SH",
                )
                for index in range(20)
            ),
            False,
        )
    )
    executor = xueqiu.XueqiuExecutor(
        scope,
        worker,
        wall_clock=lambda: float(NOW),
        monotonic_clock=lambda: 100.0,
    )

    result = asyncio.run(
        executor.execute(
            _execution(_call(limit=50), scope),
            {xueqiu.XUEQIU_COOKIE_INJECTION_TARGET: COOKIE_CANARY},
        )
    )

    assert worker.calls == [("600519", 20, 120.0)]
    assert len(result.items) == 20
    assert result.truncated is True


def test_invalid_execution_is_rejected_before_cookie_environment_access() -> None:
    capability = _capability()
    scope = xueqiu.xueqiu_scope(capability)
    worker = _Worker()
    executor = xueqiu.XueqiuExecutor(
        scope,
        worker,
        wall_clock=lambda: float(NOW),
        monotonic_clock=lambda: 100.0,
    )
    environment = _ObservedEnvironment()

    with pytest.raises(ConnectorError) as caught:
        asyncio.run(executor.execute(object(), environment))  # type: ignore[arg-type]

    assert caught.value.code == ConnectorErrorCode.CONNECTOR_STATE_INVALID.value
    assert environment.observations == (0, 0, 0)
    assert worker.calls == []


def test_invalid_call_is_rejected_before_cookie_environment_access() -> None:
    capability = _capability()
    scope = xueqiu.xueqiu_scope(capability)
    worker = _Worker()
    executor = xueqiu.XueqiuExecutor(
        scope,
        worker,
        wall_clock=lambda: float(NOW),
        monotonic_clock=lambda: 100.0,
    )
    environment = _ObservedEnvironment()
    foreign_call = validate_search(
        {
            "requests": [
                {
                    "source": "twitter",
                    "operation": "search.posts",
                    "query": "foreign-call-canary",
                    "options": {"limit": 2},
                }
            ]
        }
    )[0]

    with pytest.raises(ConnectorError) as caught:
        asyncio.run(executor.execute(_execution(foreign_call, scope), environment))

    assert caught.value.code == ConnectorErrorCode.BACKEND_INVALID_INPUT.value
    assert environment.observations == (0, 0, 0)
    assert worker.calls == []


def test_xueqiu_composition_binds_one_exact_opaque_scope(tmp_path: Path) -> None:
    capability = _capability()
    binding = _binding(tmp_path, capability)
    catalog = SecretBindingCatalog((binding,))
    provider = _SecretProvider()

    composition = xueqiu.xueqiu_execution_composition(
        capability,
        catalog,
        provider,
    )

    expected = xueqiu.xueqiu_scope(capability)
    assert composition.required_scope("xueqiu", "search.stocks") == expected
    assert expected.capability_id == capability.for_grant()
    assert repr(composition) == "ConnectorExecutionComposition(count=1)"
    assert "bitwarden" not in repr(composition).lower()


def test_wrong_injection_target_is_rejected_without_provider_access(
    tmp_path: Path,
) -> None:
    capability = _capability()
    binding = BitwardenSecretBinding(
        capability,
        "xueqiu",
        "search.stocks",
        "12345678-1234-4234-8234-123456789abc",
        "XUEQIU_COOKIE",
        "WRONG_TARGET",
        tmp_path,
        "a" * 64,
    )
    provider = _SecretProvider()

    with pytest.raises(ValueError, match="secret composition"):
        xueqiu.xueqiu_execution_composition(
            capability,
            SecretBindingCatalog((binding,)),
            provider,
        )

    assert provider.calls == 0


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        ("authentication", ConnectorErrorCode.BACKEND_AUTHENTICATION_REQUIRED),
        ("authorization", ConnectorErrorCode.BACKEND_AUTHORIZATION_DENIED),
        ("rate_limit", ConnectorErrorCode.BACKEND_RATE_LIMITED),
        ("not_found", ConnectorErrorCode.BACKEND_NOT_FOUND),
        ("transient", ConnectorErrorCode.BACKEND_TRANSIENT),
        ("backend_contract_violation", ConnectorErrorCode.BACKEND_CONTRACT_VIOLATION),
    ],
)
def test_executor_maps_only_closed_fork_failures(
    failure: str,
    expected: ConnectorErrorCode,
) -> None:
    capability = _capability()
    scope = xueqiu.xueqiu_scope(capability)
    executor = xueqiu.XueqiuExecutor(
        scope,
        _Worker(ForkExecutionFailure(failure)),  # type: ignore[arg-type]
        wall_clock=lambda: float(NOW),
        monotonic_clock=lambda: 100.0,
    )

    with pytest.raises(ConnectorError) as caught:
        asyncio.run(
            executor.execute(
                _execution(_call(), scope),
                {xueqiu.XUEQIU_COOKIE_INJECTION_TARGET: COOKIE_CANARY},
            )
        )

    assert caught.value.code == expected.value
    assert COOKIE_CANARY not in str(caught.value)


def test_executor_rejects_extra_environment_before_worker_execution() -> None:
    capability = _capability()
    scope = xueqiu.xueqiu_scope(capability)
    worker = _Worker()
    executor = xueqiu.XueqiuExecutor(
        scope,
        worker,
        wall_clock=lambda: float(NOW),
        monotonic_clock=lambda: 100.0,
    )

    with pytest.raises(ConnectorError) as caught:
        asyncio.run(
            executor.execute(
                _execution(_call(), scope),
                {
                    xueqiu.XUEQIU_COOKIE_INJECTION_TARGET: COOKIE_CANARY,
                    "EXTRA": "forbidden",
                },
            )
        )

    assert caught.value.code == ConnectorErrorCode.CONNECTOR_STATE_INVALID.value
    assert worker.calls == []


def test_cancellation_closes_secret_material_and_does_not_return_a_result(
    tmp_path: Path,
) -> None:
    async def exercise() -> tuple[int, bool]:
        started = asyncio.Event()

        class _BlockingWorker(_Worker):
            async def execute(
                self,
                query: str,
                limit: int,
                cookie_header: str,
                *,
                deadline: float,
            ) -> WorkerResponse:
                del query, limit, deadline
                self.cookie_seen = cookie_header == COOKIE_CANARY
                started.set()
                await asyncio.Event().wait()
                raise AssertionError("unreachable")

        worker = _BlockingWorker()
        composition, scope, provider, _ = _secret_composition(tmp_path, worker)
        prepared = composition.prepare(
            _execution(_call(), scope),
            deadline=asyncio.get_running_loop().time() + 30,
        )
        task = asyncio.create_task(prepared.execute())
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert worker.cookie_seen is True
        assert provider.material is not None
        return provider.calls, provider.material.closed

    assert asyncio.run(exercise()) == (1, True)


def test_isolated_worker_cancellation_zeroes_frame_and_reaps_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def exercise() -> tuple[bool, bool, bool]:
        started = asyncio.Event()

        class _Writer:
            def __init__(self) -> None:
                self.request: bytearray | None = None
                self.canary_seen = False
                self.closed = False

            def write(self, value: bytearray) -> None:
                self.request = value
                self.canary_seen = COOKIE_CANARY.encode() in value

            async def drain(self) -> None:
                started.set()
                await asyncio.Event().wait()

            def close(self) -> None:
                self.closed = True

        class _Reader:
            async def read(self, maximum: int) -> bytes:
                del maximum
                await asyncio.Event().wait()
                return b""

        class _Process:
            def __init__(self) -> None:
                self.pid = 4242
                self.returncode: int | None = None
                self.stdin = _Writer()
                self.stdout = _Reader()
                self.terminated = False

            def terminate(self) -> None:
                self.terminated = True
                self.returncode = 0

            def kill(self) -> None:
                self.returncode = -9

            async def wait(self) -> int:
                if self.returncode is None:
                    self.returncode = 0
                return self.returncode

        process = _Process()
        launches: list[tuple[tuple[object, ...], dict[str, object]]] = []

        async def launch(*args: object, **kwargs: object) -> _Process:
            launches.append((args, kwargs))
            return process

        monkeypatch.setattr(asyncio, "create_subprocess_exec", launch)
        task = asyncio.create_task(
            xueqiu._IsolatedXueqiuWorker().execute(
                "600519",
                2,
                COOKIE_CANARY,
                deadline=time.monotonic() + 30,
            )
        )
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert launches[0][0][1:] == (
            "-I",
            "-m",
            "hermes_reach.sources.xueqiu_worker",
        )
        environment = launches[0][1]["env"]
        assert isinstance(environment, dict)
        assert COOKIE_CANARY not in repr(environment)
        assert process.stdin.request is not None
        return (
            process.stdin.canary_seen,
            not any(process.stdin.request),
            process.stdin.closed and process.terminated,
        )

    assert asyncio.run(exercise()) == (True, True, True)


def test_worker_cleanup_escalates_when_sigterm_is_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Process:
        def __init__(self) -> None:
            self.pid = 4242
            self.returncode: int | None = None
            self.terminated = False

        def terminate(self) -> None:
            self.terminated = True

        def kill(self) -> None:
            raise AssertionError("process-group escalation must be used")

        async def wait(self) -> int:
            while self.returncode is None:
                await asyncio.sleep(0)
            return self.returncode

    process = _Process()
    signals: list[tuple[int, int]] = []

    def killpg(pid: int, selected_signal: int) -> None:
        signals.append((pid, selected_signal))
        process.returncode = -selected_signal

    monkeypatch.setattr(xueqiu, "_WORKER_GRACEFUL_SHUTDOWN_SECONDS", 0.001)
    monkeypatch.setattr(xueqiu.os, "killpg", killpg)

    asyncio.run(
        xueqiu._cleanup_process_group(
            cast(asyncio.subprocess.Process, process),
            terminate_group=True,
        )
    )

    assert process.terminated is True
    assert signals == [(process.pid, signal.SIGKILL)]
    assert process.returncode == -signal.SIGKILL


def test_parent_normalization_bounds_long_names_and_marks_truncation() -> None:
    projection = XueqiuProjection(
        (XueqiuStockProjection("SH600519", "N" * 4_096, "SH"),),
        False,
    )

    result = xueqiu._operation_result(projection)

    assert result.truncated is True
    assert result.items[0].text.startswith("N" * 512 + " | Symbol:")
    assert len(result.items[0].text) < 600
