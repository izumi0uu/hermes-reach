from __future__ import annotations

import asyncio
import io
import signal
import sys
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest

import hermes_reach.sources.exa as exa
import hermes_reach.sources.exa_worker as worker
from hermes_reach.contracts import validate_search
from hermes_reach.runtime.adapters import AdapterResult
from hermes_reach.runtime.policy import AuthorizedCall, ReadOnlyPolicy
from hermes_reach.runtime.responses import runner_group
from hermes_reach.runtime.runner import BoundedRunner
from hermes_reach.sources.exa import (
    ExaAdapter,
    ExaWorker,
    ExaWorkerError,
    production_exa_binding,
)
from hermes_reach.sources.exa_artifacts import ExaArtifactAttestation
from hermes_reach.sources.exa_worker import ExaProjection, ExaResultProjection

QUERY = "private Exa query"


def _artifacts() -> ExaArtifactAttestation:
    return ExaArtifactAttestation(
        Path("/opt/hermes-reach/exa/bin/node"),
        "a" * 64,
        Path("/opt/hermes-reach/exa/mcporter"),
        Path("/opt/hermes-reach/exa/mcporter/dist/cli.js"),
        "b" * 64,
        Path("/opt/hermes-reach/exa/config.json"),
        "c" * 64,
    )


def _authorized(*, limit: int | None = None) -> AuthorizedCall:
    request: dict[str, object] = {
        "source": "exa",
        "operation": "search.web",
        "query": QUERY,
    }
    if limit is not None:
        request["options"] = {"limit": limit}
    return ReadOnlyPolicy().authorize(validate_search({"requests": [request]})[0])


def _projection(*, truncated: bool = False) -> ExaProjection:
    return ExaProjection(
        "search.web",
        (
            ExaResultProjection(
                "Result body",
                "Result title",
                "https://example.com/result?from=exa",
                "Author",
                "2026-07-31",
            ),
        ),
        truncated,
    )


class _FixtureWorker:
    def __init__(self, response: ExaProjection | BaseException) -> None:
        self.response = response
        self.calls: list[tuple[str, int]] = []

    async def execute(self, query: str, limit: int) -> ExaProjection:
        self.calls.append((query, limit))
        if isinstance(self.response, BaseException):
            raise self.response
        return self.response


def _success_value(
    *,
    items: list[dict[str, object]] | None = None,
    truncated: bool = False,
) -> dict[str, object]:
    return {
        "backend": {"id": "exa-mcporter", "version": "0.12.3+exa-web.v1"},
        "items": (
            [
                {
                    "author": "Author",
                    "published_at": "2026-07-31",
                    "text": "Result body",
                    "title": "Result title",
                    "url": "https://example.com/result?from=exa",
                }
            ]
            if items is None
            else items
        ),
        "operation": "search.web",
        "partial": None,
        "protocol": "v1",
        "schema": "exa.search.result.v1",
        "source": "exa",
        "truncated": truncated,
    }


def _failure_value(code: str) -> dict[str, object]:
    return {
        "backend": {"id": "exa-mcporter", "version": "0.12.3+exa-web.v1"},
        "error": {"code": code},
        "operation": "search.web",
        "protocol": "v1",
        "source": "exa",
    }


def _framed(value: Mapping[str, object]) -> bytes:
    return worker._encode_frame(value, worker.MAX_OUTPUT_BYTES)


class _Writer:
    def __init__(self) -> None:
        self.value = b""
        self.closed = False

    def write(self, value: bytes | bytearray) -> None:
        self.value += bytes(value)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True


class _Reader:
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


class _Process:
    def __init__(
        self,
        output: bytes = b"",
        *,
        returncode: int = 0,
        reader: _Reader | _NeverReader | None = None,
    ) -> None:
        self.pid = 9753
        self.returncode: int | None = None
        self._wait_returncode = returncode
        self.stdin = _Writer()
        self.stdout = _Reader(output) if reader is None else reader
        self.waits = 0
        self.direct_kills = 0

    async def wait(self) -> int:
        self.waits += 1
        if self.returncode is None:
            self.returncode = self._wait_returncode
        return self.returncode

    def kill(self) -> None:
        self.direct_kills += 1


def test_adapter_maps_closed_projection_and_uses_default_or_explicit_limit() -> None:
    default_worker = _FixtureWorker(_projection(truncated=True))
    limited_worker = _FixtureWorker(_projection())

    default_result = asyncio.run(
        ExaAdapter(_artifacts(), default_worker).execute(_authorized())
    )
    limited_result = asyncio.run(
        ExaAdapter(_artifacts(), limited_worker).execute(_authorized(limit=7))
    )

    assert default_result == AdapterResult(
        (
            exa.RawItem(
                text="Result body",
                kind="result",
                title="Result title",
                url="https://example.com/result?from=exa",
                author="Author",
                published_at="2026-07-31",
            ),
        ),
        truncated=True,
    )
    assert limited_result.is_success
    assert default_worker.calls == [(QUERY, 20)]
    assert limited_worker.calls == [(QUERY, 7)]


def test_adapter_rejects_forged_call_before_worker_observes_query() -> None:
    fixture = _FixtureWorker(_projection())
    authorized = _authorized()
    forged = replace(
        authorized,
        call=replace(authorized.call, query=" forged"),
    )

    result = asyncio.run(ExaAdapter(_artifacts(), fixture).execute(forged))

    assert result.failure_class == "invalid_input"
    assert fixture.calls == []


@pytest.mark.parametrize("limit", [0, worker.MAX_LIMIT + 1])
def test_adapter_rejects_out_of_range_integer_limit_before_worker_invocation(
    monkeypatch: pytest.MonkeyPatch,
    limit: int,
) -> None:
    fixture = _FixtureWorker(_projection())
    authorized = _authorized()
    forged = replace(
        authorized,
        call=replace(authorized.call, options={"limit": limit}),
    )
    monkeypatch.setattr(exa, "operation_call_is_valid", lambda _: True)

    result = asyncio.run(ExaAdapter(_artifacts(), fixture).execute(forged))

    assert exa.MAX_LIMIT == worker.MAX_LIMIT
    assert result == AdapterResult(failure_class="invalid_input")
    assert fixture.calls == []


@pytest.mark.parametrize(
    "failure_class",
    [
        "invalid_input",
        "not_found",
        "setup_required",
        "authentication",
        "authorization",
        "rate_limit",
        "transient",
        "permanent",
    ],
)
def test_adapter_preserves_only_closed_worker_failure_class(failure_class: str) -> None:
    fixture = _FixtureWorker(ExaWorkerError(cast(exa.FailureClass, failure_class)))

    result = asyncio.run(ExaAdapter(_artifacts(), fixture).execute(_authorized()))

    assert result.failure_class == failure_class
    assert QUERY not in repr(result)


def test_adapter_propagates_cancellation() -> None:
    fixture = _FixtureWorker(asyncio.CancelledError())

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(ExaAdapter(_artifacts(), fixture).execute(_authorized()))


def test_binding_has_exact_identity_and_binding_owned_single_attempt() -> None:
    fixture = _FixtureWorker(ExaWorkerError("transient"))
    binding = production_exa_binding(_artifacts(), worker=fixture)

    result = asyncio.run(BoundedRunner().run(_authorized(), (binding,)))

    assert binding.source == "exa"
    assert binding.operation == "search.web"
    assert binding.backend_id == "exa-mcporter"
    assert binding.backend_version == "0.12.3+exa-web.v1"
    assert binding.required_scope == "public"
    assert binding.retry_owner == "binding"
    assert fixture.calls == [(QUERY, 20)]
    assert result.failure_class == "transient"
    assert len(result.attempts) == 1


def test_artifact_drift_reports_setup_required_without_retry() -> None:
    fixture = _FixtureWorker(ExaWorkerError("setup_required"))
    binding = production_exa_binding(_artifacts(), worker=fixture)
    authorized = _authorized()

    result = asyncio.run(BoundedRunner().run(authorized, (binding,)))
    group, outcome = runner_group(authorized.call, result)

    assert outcome == "error"
    assert group["availability"] == "setup_required"
    assert group["error"] == {
        "code": "setup_required",
        "message": "The source requires an unavailable configured capability.",
        "remediation": "Complete operator setup for this exact source operation.",
    }
    assert len(cast(list[object], group["attempts"])) == 1
    assert cast(list[dict[str, object]], group["attempts"])[0]["outcome"] == (
        "setup_required"
    )
    assert fixture.calls == [(QUERY, 20)]


def test_parent_uses_fixed_argv_isolated_environment_and_cleans_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _Process(_framed(_success_value(items=[], truncated=True)))
    captured: dict[str, object] = {}

    async def create(*args: str, **kwargs: object) -> _Process:
        captured["args"] = args
        captured["kwargs"] = kwargs
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)

    result = asyncio.run(ExaWorker(_artifacts()).execute(QUERY, 50))

    assert result == ExaProjection("search.web", (), True)
    assert captured["args"] == (
        sys.executable,
        "-I",
        "-m",
        "hermes_reach.sources.exa_worker",
    )
    assert QUERY not in cast(tuple[str, ...], captured["args"])
    kwargs = cast(dict[str, object], captured["kwargs"])
    assert kwargs["stdin"] is asyncio.subprocess.PIPE
    assert kwargs["stdout"] is asyncio.subprocess.PIPE
    assert kwargs["stderr"] is asyncio.subprocess.DEVNULL
    assert kwargs["close_fds"] is True
    assert kwargs["start_new_session"] is True
    assert "shell" not in kwargs
    environment = cast(dict[str, str], kwargs["env"])
    assert set(environment) == {
        "HOME",
        "XDG_CONFIG_HOME",
        "XDG_CACHE_HOME",
        "XDG_DATA_HOME",
        "TMPDIR",
        "PYTHONUTF8",
        "PYTHONNOUSERSITE",
        "LANG",
        "LC_ALL",
        "TZ",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "no_proxy",
    }
    assert all(QUERY not in value for value in environment.values())
    assert "PATH" not in environment
    assert "EXA_API_KEY" not in environment
    assert "NODE_OPTIONS" not in environment
    assert environment["HTTPS_PROXY"] == ""
    cwd = Path(cast(str, kwargs["cwd"]))
    assert cwd == Path(environment["HOME"]).parent
    assert QUERY not in str(cwd)
    assert cwd.exists() is False
    request = worker._read_request(io.BytesIO(process.stdin.value))
    assert request.query == QUERY
    assert request.limit == 50
    assert request.artifacts == _artifacts()
    assert process.stdin.closed


@pytest.mark.parametrize("executable", ["", "python"])
def test_parent_rejects_non_absolute_python_before_worker_spawn(
    monkeypatch: pytest.MonkeyPatch,
    executable: str,
) -> None:
    spawns = 0

    async def unexpected_spawn(*_: str, **__: object) -> _Process:
        nonlocal spawns
        spawns += 1
        raise AssertionError("invalid interpreter path reached worker spawn")

    monkeypatch.setattr(exa.sys, "executable", executable)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", unexpected_spawn)

    with pytest.raises(ExaWorkerError) as raised:
        asyncio.run(ExaWorker(_artifacts()).execute(QUERY, 1))

    assert raised.value.failure_class == "permanent"
    assert QUERY not in repr(raised.value)
    assert spawns == 0


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("unsupported_protocol_version", "permanent"),
        ("invalid_request", "permanent"),
        ("unsupported_source", "permanent"),
        ("unsupported_operation", "permanent"),
        ("host_capability_missing", "permanent"),
        ("backend_unavailable", "setup_required"),
        ("backend_incompatible", "setup_required"),
        ("backend_contract_violation", "permanent"),
        ("invalid_input", "invalid_input"),
        ("not_found", "not_found"),
        ("authentication", "authentication"),
        ("authorization", "authorization"),
        ("rate_limit", "rate_limit"),
        ("transient", "transient"),
        ("deadline_exceeded", "transient"),
        ("cancelled", "transient"),
        ("permanent", "permanent"),
    ],
)
def test_parent_maps_closed_fork_failures_without_exposing_code(
    monkeypatch: pytest.MonkeyPatch,
    code: str,
    expected: str,
) -> None:
    process = _Process(_framed(_failure_value(code)))

    async def create(*_: str, **__: object) -> _Process:
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)

    with pytest.raises(ExaWorkerError) as raised:
        asyncio.run(ExaWorker(_artifacts()).execute(QUERY, 1))

    assert raised.value.failure_class == expected
    assert code not in str(raised.value)
    assert QUERY not in str(raised.value)


@pytest.mark.parametrize("terminal", ["timeout", "cancel"])
def test_timeout_and_cancellation_kill_reap_then_clean_worker(
    monkeypatch: pytest.MonkeyPatch,
    terminal: str,
) -> None:
    reader = _NeverReader()
    process = _Process(reader=reader)
    killed: list[tuple[int, signal.Signals]] = []
    cwd: Path | None = None

    async def create(*_: str, **kwargs: object) -> _Process:
        nonlocal cwd
        cwd = Path(cast(str, kwargs["cwd"]))
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    monkeypatch.setattr(
        exa.os,
        "killpg",
        lambda pid, requested: killed.append((pid, requested)),
    )

    async def exercise() -> None:
        task = asyncio.create_task(ExaWorker(_artifacts()).execute(QUERY, 1))
        await reader.entered.wait()
        if terminal == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(task, timeout=0.001)

    asyncio.run(exercise())

    assert killed == [(process.pid, signal.SIGKILL)]
    assert process.waits == 1
    assert process.direct_kills == 0
    assert cwd is not None
    assert cwd.exists() is False


@pytest.mark.parametrize(
    ("output", "returncode", "failure_class"),
    [
        (b"invalid-frame", 0, "permanent"),
        (b"", 1, "transient"),
        (b"x" * (worker.MAX_OUTPUT_BYTES + 5), 0, "permanent"),
    ],
)
def test_invalid_terminal_output_kills_completed_group_and_is_redacted(
    monkeypatch: pytest.MonkeyPatch,
    output: bytes,
    returncode: int,
    failure_class: str,
) -> None:
    process = _Process(output, returncode=returncode)
    killed: list[tuple[int, signal.Signals]] = []

    async def create(*_: str, **__: object) -> _Process:
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    monkeypatch.setattr(
        exa.os,
        "killpg",
        lambda pid, requested: killed.append((pid, requested)),
    )

    with pytest.raises(ExaWorkerError) as raised:
        asyncio.run(ExaWorker(_artifacts()).execute(QUERY, 1))

    assert raised.value.failure_class == failure_class
    assert killed == [(process.pid, signal.SIGKILL)]
    assert process.waits == 1
    assert QUERY not in str(raised.value)


def test_process_cleanup_falls_back_to_direct_kill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _Process()

    def unavailable(*_: object) -> None:
        raise OSError("private process detail")

    monkeypatch.setattr(exa.os, "killpg", unavailable)

    asyncio.run(exa._cleanup_process_group(process, terminate_group=True))  # type: ignore[arg-type]

    assert process.direct_kills == 1
    assert process.waits == 1


def test_private_state_is_removed_when_process_cleanup_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _Process(_framed(_success_value(items=[])))
    cwd: Path | None = None

    async def create(*_: str, **kwargs: object) -> _Process:
        nonlocal cwd
        cwd = Path(cast(str, kwargs["cwd"]))
        return process

    async def fail_cleanup(*_: object, **__: object) -> None:
        raise RuntimeError("private cleanup detail")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    monkeypatch.setattr(exa, "_cleanup_process_group", fail_cleanup)

    with pytest.raises(RuntimeError, match="private cleanup detail"):
        asyncio.run(ExaWorker(_artifacts()).execute(QUERY, 1))

    assert cwd is not None
    assert cwd.exists() is False
