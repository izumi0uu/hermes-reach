from __future__ import annotations

import asyncio
import io
import json
import signal
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from agent_reach.execution import v1 as execution

import hermes_reach.sources.bilibili as bilibili
import hermes_reach.sources.bilibili_worker as worker
from hermes_reach.sources.bilibili import BilibiliWorker, BilibiliWorkerError


def _item(
    *,
    text: str = "Description",
    bvid: str = "BV1xx411c7mD",
) -> execution.ExecutionItemV1:
    return execution.ExecutionItemV1(
        "bilibili.video.v1",
        {
            "text": text,
            "native_id": bvid,
            "title": "Video title",
            "url": f"https://www.bilibili.com/video/{bvid}",
            "author": "Author",
            "duration_seconds": 125,
            "view_count": 99,
        },
    )


def _success(
    operation: worker.WorkerOperation,
    *,
    items: tuple[execution.ExecutionItemV1, ...] | None = None,
    truncated: bool = False,
) -> execution.ExecutionSuccessV1:
    selected = (_item(),) if items is None else items
    return execution.ExecutionSuccessV1(
        "v1",
        "bilibili",
        operation,
        "bili-cli",
        "0.6.2",
        selected,
        truncated,
        None,
    )


def _api(execute: Callable[[object, object], object]) -> SimpleNamespace:
    return SimpleNamespace(
        execution_request_type=execution.ExecutionRequestV1,
        network_access_type=execution.NetworkAccessV1,
        execution_limits_type=execution.ExecutionLimitsV1,
        execution_context_type=execution.ExecutionContextV1,
        execution_item_type=execution.ExecutionItemV1,
        execution_success_type=execution.ExecutionSuccessV1,
        execution_failure_type=execution.ExecutionFailureV1,
        execute=execute,
    )


def _success_value(
    operation: worker.WorkerOperation,
    *,
    items: list[dict[str, object]] | None = None,
    truncated: bool = False,
) -> dict[str, object]:
    return {
        "backend": {"id": "bili-cli", "version": "0.6.2"},
        "items": [_item_value()] if items is None else items,
        "operation": operation,
        "partial": None,
        "protocol": "v1",
        "schema": "bilibili.video.v1",
        "source": "bilibili",
        "truncated": truncated,
    }


def _item_value(
    *,
    text: str = "Description",
    bvid: str = "BV1xx411c7mD",
) -> dict[str, object]:
    return {
        "author": "Author",
        "duration_seconds": 125,
        "native_id": bvid,
        "text": text,
        "title": "Video title",
        "url": f"https://www.bilibili.com/video/{bvid}",
        "view_count": 99,
    }


def _framed(value: dict[str, object]) -> bytes:
    return worker._encode_frame(value, worker.MAX_OUTPUT_BYTES)


class _Writer:
    def __init__(self) -> None:
        self.value = b""
        self.closed = False

    def write(self, value: bytes) -> None:
        self.value += value

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
        self.pid = 2468
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


@pytest.mark.parametrize(
    ("worker_request", "expected_arguments", "expected_limit"),
    [
        (
            worker.WorkerRequest("search.videos", query="-danger", limit=3),
            {"query": "-danger", "limit": 3},
            3,
        ),
        (
            worker.WorkerRequest(
                "read.video", url="https://www.bilibili.com/video/BV1xx411c7mD"
            ),
            {"url": "https://www.bilibili.com/video/BV1xx411c7mD"},
            1,
        ),
        (worker.WorkerRequest("browse.hot", limit=4), {"limit": 4}, 4),
        (worker.WorkerRequest("browse.rank", limit=5), {"limit": 5}, 5),
    ],
)
def test_worker_executes_all_operations_through_closed_fork_api(
    worker_request: worker.WorkerRequest,
    expected_arguments: dict[str, object],
    expected_limit: int,
) -> None:
    calls: list[tuple[object, object]] = []

    def execute(execution_request: object, context: object) -> object:
        calls.append((execution_request, context))
        operation = cast(execution.ExecutionRequestV1, execution_request).operation
        items = (_item(),) if operation == "read.video" else ()
        return _success(cast(worker.WorkerOperation, operation), items=items)

    value = worker._execute_request(
        worker_request, execution_api_provider=lambda: _api(execute)
    )

    assert value["items"] == (
        [] if worker_request.operation != "read.video" else [_item_value()]
    )
    assert len(calls) == 1
    execution_request = cast(execution.ExecutionRequestV1, calls[0][0])
    context = cast(execution.ExecutionContextV1, calls[0][1])
    assert execution_request.protocol_version == "v1"
    assert execution_request.source == "bilibili"
    assert execution_request.operation == worker_request.operation
    assert dict(execution_request.arguments) == expected_arguments
    assert len(context.host_capabilities) == 1
    assert type(context.host_capabilities[0]) is execution.NetworkAccessV1
    assert context.limits == execution.ExecutionLimitsV1(
        maximum_items=expected_limit,
        maximum_text_characters=worker.MAX_TEXT_CHARACTERS,
    )


def test_worker_uses_bilibili_runtime_integrity_handshake(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requested: list[object] = []
    sentinel = cast(worker.AgentReachExecutionApi, object())

    def validate(**kwargs: object) -> worker.AgentReachExecutionApi:
        requested.append(kwargs)
        return sentinel

    monkeypatch.setattr(worker, "validate_agent_reach_execution_contract", validate)

    assert worker._load_execution_api() is sentinel
    assert requested == [{"runtime_module": "bilibili"}]


@pytest.mark.parametrize(
    "result",
    [
        object(),
        SimpleNamespace(
            protocol_version="v1",
            source="bilibili",
            operation="browse.hot",
            backend_id="bili-cli",
            backend_version="0.6.2",
            items=(),
            truncated=False,
            partial_error_code=None,
        ),
    ],
)
def test_worker_fails_closed_on_execution_type_or_provider_drift(
    result: object,
) -> None:
    value = worker._execute_request(
        worker.WorkerRequest("browse.hot", limit=1),
        execution_api_provider=lambda: _api(lambda *_: result),
    )
    assert value == worker._failure_value("browse.hot", "backend_contract_violation")

    provider_failure = worker._execute_request(
        worker.WorkerRequest("browse.hot", limit=1),
        execution_api_provider=lambda: (_ for _ in ()).throw(RuntimeError("private")),
    )
    assert provider_failure == worker._failure_value(
        "browse.hot", "backend_contract_violation"
    )


def test_worker_serializes_only_closed_fork_failure() -> None:
    def execute(_: object, __: object) -> execution.ExecutionFailureV1:
        return execution.ExecutionFailureV1(
            "v1",
            "bilibili",
            "browse.hot",
            "bili-cli",
            "0.6.2",
            "rate_limit",
        )

    value = worker._execute_request(
        worker.WorkerRequest("browse.hot", limit=1),
        execution_api_provider=lambda: _api(execute),
    )

    assert value == worker._failure_value("browse.hot", "rate_limit")
    assert "message" not in json.dumps(value)


def test_worker_rejects_unknown_fields_and_trailing_request_data() -> None:
    payload = json.dumps(
        {
            "protocol_version": "v1",
            "operation": "browse.hot",
            "limit": 1,
            "command": "login",
        }
    ).encode()
    framed = len(payload).to_bytes(4, "big") + payload

    with pytest.raises(worker.BilibiliProtocolError):
        worker._read_request(io.BytesIO(framed))
    with pytest.raises(worker.BilibiliProtocolError):
        worker._read_request(
            io.BytesIO(worker.encode_request("browse.hot", limit=1) + b"x")
        )


def test_frames_keep_bounded_cjk_as_utf8() -> None:
    payload = {"text": "中文边界"}
    expected = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    ascii_expansion = json.dumps(
        payload,
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")

    framed = worker._encode_frame(payload, len(expected))

    assert framed[4:] == expected
    assert worker._decode_frame(framed, len(expected)) == payload
    assert len(expected) < len(ascii_expansion)


@pytest.mark.parametrize(
    "url",
    [
        "http://www.bilibili.com/video/BV1xx411c7mD",
        "https://evil.test/video/BV1xx411c7mD",
        "https://www.bilibili.com/video/BV1xx411c7mD?x=1",
        "https://www.bilibili.com/video/BV1xx411c7mD#fragment",
        "https://www.bilibili.com/video/BV1xx411c7mD/extra",
        "https://www.bilibili.com/video/NOTABVID12",
        "https://www.bilibili.com\uff0fvideo/BV1xx411c7mD",
    ],
)
def test_read_video_rejects_non_canonical_urls(url: str) -> None:
    with pytest.raises(worker.BilibiliProtocolError):
        worker.encode_request("read.video", url=url)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: {**value, "schema": "future.video.v2"},
        lambda value: {**value, "source": "rss"},
        lambda value: {**value, "operation": "browse.rank"},
        lambda value: {**value, "partial": "permanent"},
        lambda value: {**value, "unknown": True},
        lambda value: {**value, "items": [{**_item_value(), "unknown": True}]},
        lambda value: {
            **value,
            "items": [{**_item_value(), "url": "https://evil.test/video"}],
        },
        lambda value: {
            **value,
            "items": [{**_item_value(), "duration_seconds": True}],
        },
    ],
)
def test_parent_decoder_revalidates_complete_closed_fork_result(
    mutation: Callable[[dict[str, object]], dict[str, object]],
) -> None:
    raw = _framed(mutation(_success_value("browse.hot")))

    with pytest.raises(worker.BilibiliProtocolError):
        worker.decode_response(raw, operation="browse.hot", limit=1)


def test_parent_decoder_preserves_truncation_and_typed_items() -> None:
    decoded = worker.decode_response(
        _framed(_success_value("search.videos", truncated=True)),
        operation="search.videos",
        limit=1,
    )

    assert decoded == worker.BilibiliProjection(
        "search.videos",
        (
            worker.BilibiliVideoProjection(
                "Description",
                "BV1xx411c7mD",
                "Video title",
                "https://www.bilibili.com/video/BV1xx411c7mD",
                "Author",
                125,
                99,
            ),
        ),
        True,
    )


def test_parent_decoder_accepts_fork_text_truncated_at_a_space() -> None:
    text = "x " * (worker.MAX_TEXT_CHARACTERS // 2)

    decoded = worker.decode_response(
        _framed(
            _success_value(
                "search.videos",
                items=[_item_value(text=text)],
                truncated=True,
            )
        ),
        operation="search.videos",
        limit=1,
    )

    assert isinstance(decoded, worker.BilibiliProjection)
    assert decoded.items[0].text == text


def test_worker_preserves_zero_item_truncation_allowed_by_fork_contract() -> None:
    value = worker._execute_request(
        worker.WorkerRequest("search.videos", query="query", limit=1),
        execution_api_provider=lambda: _api(
            lambda *_: _success("search.videos", items=(), truncated=True)
        ),
    )

    assert worker.decode_response(
        _framed(cast(dict[str, object], value)),
        operation="search.videos",
        limit=1,
    ) == worker.BilibiliProjection(
        "search.videos",
        (),
        True,
    )


def test_real_worker_module_rejects_empty_input_without_output() -> None:
    completed = subprocess.run(
        [sys.executable, "-I", "-m", "hermes_reach.sources.bilibili_worker"],
        input=b"",
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 1
    assert completed.stdout == b""
    assert completed.stderr == b""


def test_parent_uses_fixed_argv_isolated_environment_and_cleans_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _Process(_framed(_success_value("search.videos", items=[])))
    captured: dict[str, object] = {}

    async def create(*args: str, **kwargs: object) -> _Process:
        captured["args"] = args
        captured["kwargs"] = kwargs
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)

    result = asyncio.run(
        BilibiliWorker().execute("search.videos", query="secret", limit=2)
    )

    assert result == worker.BilibiliProjection("search.videos", (), False)
    assert captured["args"] == (
        sys.executable,
        "-I",
        "-m",
        "hermes_reach.sources.bilibili_worker",
    )
    assert "secret" not in cast(tuple[str, ...], captured["args"])
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
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "no_proxy",
    }
    assert environment["HTTP_PROXY"] == ""
    assert environment["NO_PROXY"] == "*"
    assert "OUTPUT" not in environment
    cwd = Path(cast(str, kwargs["cwd"]))
    assert cwd == Path(environment["HOME"]).parent
    assert cwd.exists() is False
    request = worker._read_request(io.BytesIO(process.stdin.value))
    assert request == worker.WorkerRequest("search.videos", query="secret", limit=2)
    assert process.stdin.closed


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("unsupported_protocol_version", "permanent"),
        ("invalid_request", "permanent"),
        ("unsupported_source", "permanent"),
        ("unsupported_operation", "permanent"),
        ("host_capability_missing", "permanent"),
        ("invalid_input", "invalid_input"),
        ("not_found", "not_found"),
        ("authentication", "authentication"),
        ("authorization", "authorization"),
        ("rate_limit", "rate_limit"),
        ("transient", "transient"),
        ("deadline_exceeded", "transient"),
        ("cancelled", "transient"),
        ("backend_unavailable", "transient"),
        ("backend_incompatible", "transient"),
        ("permanent", "permanent"),
        ("backend_contract_violation", "transient"),
    ],
)
def test_parent_maps_closed_fork_failures(
    monkeypatch: pytest.MonkeyPatch,
    code: str,
    expected: str,
) -> None:
    value = worker._failure_value("browse.hot", cast(worker.WorkerErrorCode, code))
    process = _Process(_framed(value))

    async def create(*_: str, **__: object) -> _Process:
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)

    with pytest.raises(BilibiliWorkerError) as raised:
        asyncio.run(BilibiliWorker().execute("browse.hot", limit=1))

    assert raised.value.failure_class == expected
    assert code not in str(raised.value)


def test_parent_preserves_transient_fallback_for_unexpected_worker_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def create(*_: str, **__: object) -> _Process:
        raise RuntimeError("sensitive unexpected failure")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)

    with pytest.raises(BilibiliWorkerError) as raised:
        asyncio.run(BilibiliWorker().execute("browse.hot", limit=1))

    assert raised.value.failure_class == "transient"
    assert "sensitive" not in str(raised.value)


@pytest.mark.parametrize("terminal", ["timeout", "cancel"])
def test_timeout_and_cancellation_kill_reap_then_clean_worker(
    monkeypatch: pytest.MonkeyPatch, terminal: str
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
        bilibili.os,
        "killpg",
        lambda pid, requested: killed.append((pid, requested)),
    )

    async def exercise() -> None:
        task = asyncio.create_task(BilibiliWorker().execute("browse.rank", limit=2))
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


def test_parent_bounds_stdout_and_discards_process_failure_details(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _Process(b"x" * (worker.MAX_OUTPUT_BYTES + 5))

    async def create(*_: str, **__: object) -> _Process:
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)

    with pytest.raises(BilibiliWorkerError) as raised:
        asyncio.run(BilibiliWorker().execute("browse.hot", limit=1))

    assert raised.value.failure_class == "permanent"
    assert "x" not in str(raised.value)


@pytest.mark.parametrize(
    ("output", "returncode", "failure_class"),
    [
        (b"invalid-frame", 0, "permanent"),
        (b"", 1, "transient"),
    ],
)
def test_invalid_terminal_output_kills_completed_process_group(
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
        bilibili.os,
        "killpg",
        lambda pid, requested: killed.append((pid, requested)),
    )

    with pytest.raises(BilibiliWorkerError) as raised:
        asyncio.run(BilibiliWorker().execute("browse.hot", limit=1))

    assert raised.value.failure_class == failure_class
    assert killed == [(process.pid, signal.SIGKILL)]
    assert process.waits == 1
