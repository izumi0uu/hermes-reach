from __future__ import annotations

import asyncio
import io
import json
import signal
import subprocess
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from agent_reach.execution import v1 as execution

import hermes_reach.sources.youtube as youtube
import hermes_reach.sources.youtube_worker as worker
from hermes_reach.sources.youtube import YouTubeWorker, YouTubeWorkerError

VIDEO_ID = "dQw4w9WgXcQ"
VIDEO_URL = f"https://www.youtube.com/watch?v={VIDEO_ID}"


def _fork_item_value(
    *,
    text: str = "Description",
    video_id: str = VIDEO_ID,
) -> dict[str, object]:
    return {
        "text": text,
        "native_id": video_id,
        "title": "Video title",
        "url": f"https://www.youtube.com/watch?v={video_id}",
        "author": "Channel",
        "published_at": "2009-10-25",
        "duration_seconds": 213,
        "view_count": 42,
        "comment_count": 7,
    }


def _projected_video() -> dict[str, object]:
    return {
        "id": VIDEO_ID,
        "title": "Video title",
        "description": "Description",
        "uploader": "Channel",
        "duration_seconds": 213,
        "view_count": 42,
        "comment_count": 7,
        "upload_date": "2009-10-25",
        "url": VIDEO_URL,
    }


def _video_item(
    *,
    text: str = "Description",
    video_id: str = VIDEO_ID,
) -> execution.ExecutionItemV1:
    return execution.ExecutionItemV1(
        "youtube.video.v1",
        _fork_item_value(text=text, video_id=video_id),
    )


def _subtitle_item_value(
    *,
    text: str = "WEBVTT\n\n00:00.000 --> 00:01.000\nHello",
    video_id: str = VIDEO_ID,
) -> dict[str, object]:
    return {
        "text": text,
        "native_id": video_id,
        "title": "Video title",
        "url": f"https://www.youtube.com/watch?v={video_id}",
        "language": "en",
        "origin": "manual",
    }


def _subtitle_item(
    *,
    text: str = "WEBVTT\n\n00:00.000 --> 00:01.000\nHello",
    video_id: str = VIDEO_ID,
) -> execution.ExecutionItemV1:
    return execution.ExecutionItemV1(
        "youtube.subtitle.v1",
        _subtitle_item_value(text=text, video_id=video_id),
    )


def _success(
    operation: worker.WorkerOperation,
    items: tuple[execution.ExecutionItemV1, ...],
    *,
    truncated: bool = False,
) -> execution.ExecutionSuccessV1:
    return execution.ExecutionSuccessV1(
        "v1",
        "youtube",
        operation,
        "yt-dlp",
        "2026.7.4",
        items,
        truncated,
        None,
    )


def _failure(
    operation: worker.WorkerOperation,
    code: str,
) -> execution.ExecutionFailureV1:
    return execution.ExecutionFailureV1(
        "v1",
        "youtube",
        operation,
        "yt-dlp",
        "2026.7.4",
        cast(execution.ExecutionErrorCodeV1, code),
    )


def _api(execute: Callable[[object, object], object]) -> SimpleNamespace:
    return SimpleNamespace(
        execution_request_type=execution.ExecutionRequestV1,
        network_access_type=execution.NetworkAccessV1,
        private_workspace_type=execution.PrivateWorkspaceV1,
        execution_limits_type=execution.ExecutionLimitsV1,
        execution_context_type=execution.ExecutionContextV1,
        execution_item_type=execution.ExecutionItemV1,
        execution_success_type=execution.ExecutionSuccessV1,
        execution_failure_type=execution.ExecutionFailureV1,
        execute=execute,
    )


def test_search_uses_closed_fork_request_and_network_capability() -> None:
    calls: list[tuple[object, object]] = []

    def execute(request: object, context: object) -> object:
        calls.append((request, context))
        return _success("search.videos", (_video_item(),), truncated=True)

    value = worker._execute_request(
        worker.WorkerRequest("search.videos", query="private query", limit=2),
        execution_api_provider=lambda: _api(execute),
    )

    assert value == {
        "protocol_version": "v1",
        "operation": "search.videos",
        "ok": True,
        "data": [_projected_video()],
    }
    request = cast(execution.ExecutionRequestV1, calls[0][0])
    context = cast(execution.ExecutionContextV1, calls[0][1])
    assert request == execution.ExecutionRequestV1(
        "v1",
        "youtube",
        "search.videos",
        {"query": "private query", "limit": 2},
    )
    assert tuple(type(value) for value in context.host_capabilities) == (
        execution.NetworkAccessV1,
    )
    assert context.limits == execution.ExecutionLimitsV1(
        maximum_items=2,
        maximum_text_characters=worker.MAX_TEXT_CHARACTERS,
    )


def test_read_video_uses_closed_fork_api_and_canonicalizes_encoded_id() -> None:
    encoded_url = "https://www.youtube.com/watch?v=dQw4w9WgXc%51"
    request = worker._read_request(
        io.BytesIO(worker.encode_request("read.video", url=encoded_url))
    )
    calls: list[tuple[object, object]] = []

    def execute(execution_request: object, context: object) -> object:
        calls.append((execution_request, context))
        return _success("read.video", (_video_item(),), truncated=True)

    value = worker._execute_request(
        request,
        execution_api_provider=lambda: _api(execute),
    )

    assert value == {
        "protocol_version": "v1",
        "operation": "read.video",
        "ok": True,
        "data": {"item": _fork_item_value(), "truncated": True},
    }
    execution_request = cast(execution.ExecutionRequestV1, calls[0][0])
    context = cast(execution.ExecutionContextV1, calls[0][1])
    assert execution_request == execution.ExecutionRequestV1(
        "v1",
        "youtube",
        "read.video",
        {"url": VIDEO_URL},
    )
    assert tuple(type(value) for value in context.host_capabilities) == (
        execution.NetworkAccessV1,
    )


def test_subtitles_use_network_then_private_workspace_capabilities() -> None:
    calls: list[tuple[object, object]] = []

    def execute(request: object, context: object) -> object:
        calls.append((request, context))
        return _success("read.subtitles", (_subtitle_item(),), truncated=True)

    value = worker._execute_request(
        worker.WorkerRequest("read.subtitles", url=VIDEO_URL, language="en"),
        execution_api_provider=lambda: _api(execute),
    )

    assert value == {
        "protocol_version": "v1",
        "operation": "read.subtitles",
        "ok": True,
        "data": {
            "id": VIDEO_ID,
            "title": "Video title",
            "language": "en",
            "origin": "manual",
            "text": "WEBVTT\n\n00:00.000 --> 00:01.000\nHello",
            "truncated": True,
            "url": VIDEO_URL,
        },
    }
    execution_request = cast(execution.ExecutionRequestV1, calls[0][0])
    context = cast(execution.ExecutionContextV1, calls[0][1])
    assert execution_request == execution.ExecutionRequestV1(
        "v1",
        "youtube",
        "read.subtitles",
        {"url": VIDEO_URL, "language": "en"},
    )
    assert tuple(type(value) for value in context.host_capabilities) == (
        execution.NetworkAccessV1,
        execution.PrivateWorkspaceV1,
    )
    assert context.limits == execution.ExecutionLimitsV1(
        maximum_items=1,
        maximum_text_characters=worker.MAX_TEXT_CHARACTERS,
    )


def test_worker_uses_youtube_runtime_integrity_handshake(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requested: list[object] = []
    sentinel = cast(worker.AgentReachExecutionApi, object())

    def validate(**kwargs: object) -> worker.AgentReachExecutionApi:
        requested.append(kwargs)
        return sentinel

    monkeypatch.setattr(worker, "validate_agent_reach_execution_contract", validate)

    assert worker._load_execution_api() is sentinel
    assert requested == [{"runtime_module": "youtube"}]


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("unsupported_protocol_version", "permanent"),
        ("invalid_request", "permanent"),
        ("unsupported_source", "permanent"),
        ("unsupported_operation", "permanent"),
        ("host_capability_missing", "permanent"),
        ("invalid_input", "permanent"),
        ("backend_unavailable", "setup_required"),
        ("backend_incompatible", "setup_required"),
        ("deadline_exceeded", "permanent"),
        ("cancelled", "permanent"),
        ("not_found", "not_found"),
        ("authentication", "authentication"),
        ("authorization", "authorization"),
        ("rate_limit", "rate_limit"),
        ("transient", "transient"),
        ("permanent", "permanent"),
        ("backend_contract_violation", "permanent"),
    ],
)
@pytest.mark.parametrize(
    "worker_request",
    [
        worker.WorkerRequest("search.videos", query="query", limit=1),
        worker.WorkerRequest("read.video", url=VIDEO_URL),
        worker.WorkerRequest("read.subtitles", url=VIDEO_URL),
    ],
)
def test_all_operations_freeze_fork_error_mapping(
    worker_request: worker.WorkerRequest,
    code: str,
    expected: str,
) -> None:
    value = worker._execute_request(
        worker_request,
        execution_api_provider=lambda: _api(
            lambda *_: _failure(worker_request.operation, code)
        ),
    )

    assert value == {
        "protocol_version": "v1",
        "operation": worker_request.operation,
        "ok": False,
        "error": {"code": expected},
    }


def test_all_operations_redact_integrity_and_execution_failures() -> None:
    secret = "query=private /secret/path"
    failures: list[Mapping[str, object]] = []
    for request in (
        worker.WorkerRequest("search.videos", query="private", limit=1),
        worker.WorkerRequest("read.video", url=VIDEO_URL),
        worker.WorkerRequest("read.subtitles", url=VIDEO_URL),
    ):
        failures.append(
            worker._execute_request(
                request,
                execution_api_provider=lambda: (_ for _ in ()).throw(
                    RuntimeError(secret)
                ),
            )
        )
        failures.append(
            worker._execute_request(
                request,
                execution_api_provider=lambda: _api(
                    lambda *_: (_ for _ in ()).throw(RuntimeError(secret))
                ),
            )
        )

    assert [failure["error"] for failure in failures] == [
        {"code": "setup_required"},
        {"code": "permanent"},
    ] * 3
    assert secret not in json.dumps(failures)


def test_worker_rejects_fork_type_identity_schema_and_partial_drift() -> None:
    wrong_identity = _success(
        "read.video",
        (_video_item(video_id="aaaaaaaaaaa"),),
    )
    wrong_schema = _success("read.video", (_video_item(),))
    object.__setattr__(wrong_schema.items[0], "schema_id", "youtube.subtitle.v1")
    partial = _success("read.video", (_video_item(),))
    object.__setattr__(partial, "partial_error_code", "transient")

    for result in (object(), wrong_identity, wrong_schema, partial):
        value = worker._execute_request(
            worker.WorkerRequest("read.video", url=VIDEO_URL),
            execution_api_provider=lambda result=result: _api(lambda *_: result),
        )
        assert value["error"] == {"code": "permanent"}


def test_search_rejects_duplicate_identity_and_limit_drift() -> None:
    duplicate = _success("search.videos", (_video_item(), _video_item()))
    too_many = _success(
        "search.videos",
        (_video_item(), _video_item(video_id="aaaaaaaaaaa")),
    )

    for request, result in (
        (worker.WorkerRequest("search.videos", query="query", limit=2), duplicate),
        (worker.WorkerRequest("search.videos", query="query", limit=1), too_many),
    ):
        value = worker._execute_request(
            request,
            execution_api_provider=lambda result=result: _api(lambda *_: result),
        )
        assert value["error"] == {"code": "permanent"}


@pytest.mark.parametrize(
    "fields",
    [
        _subtitle_item_value(video_id="aaaaaaaaaaa"),
        {**_subtitle_item_value(), "origin": "provider"},
        {**_subtitle_item_value(), "text": "not vtt"},
    ],
)
def test_subtitle_revalidates_identity_origin_and_vtt(
    fields: dict[str, object],
) -> None:
    item = _subtitle_item()
    object.__setattr__(item, "fields", fields)
    value = worker._execute_request(
        worker.WorkerRequest("read.subtitles", url=VIDEO_URL),
        execution_api_provider=lambda: _api(
            lambda *_: _success(
                "read.subtitles",
                (item,),
            )
        ),
    )

    assert value["error"] == {"code": "permanent"}


def test_read_video_preserves_unicode_text_limit_and_truncation() -> None:
    text = chr(0x1F600) * worker.MAX_TEXT_CHARACTERS
    value = worker._execute_request(
        worker.WorkerRequest("read.video", url=VIDEO_URL),
        execution_api_provider=lambda: _api(
            lambda *_: _success(
                "read.video",
                (_video_item(text=text),),
                truncated=True,
            )
        ),
    )
    decoded = worker.decode_response(
        worker._encode_frame(value, worker.MAX_OUTPUT_BYTES)
    )

    data = cast(dict[str, object], decoded["data"])
    item = cast(dict[str, object], data["item"])
    assert item["text"] == text
    assert len(cast(str, item["text"])) == worker.MAX_TEXT_CHARACTERS
    assert data["truncated"] is True


def test_request_frames_reject_authority_fields_duplicate_keys_and_constants() -> None:
    payload = json.dumps(
        {
            "protocol_version": "v1",
            "operation": "read.video",
            "url": VIDEO_URL,
            "proxy": "http://private",
        }
    ).encode()
    with pytest.raises(worker.YouTubeProtocolError):
        worker._read_request(io.BytesIO(len(payload).to_bytes(4, "big") + payload))

    duplicate = (
        b'{"protocol_version":"v1","protocol_version":"v1",'
        b'"operation":"read.video","ok":true,"data":{}}'
    )
    with pytest.raises(worker.YouTubeProtocolError):
        worker.decode_response(len(duplicate).to_bytes(4, "big") + duplicate)

    invalid_number = (
        b'{"protocol_version":"v1","operation":"read.video","ok":true,"data":NaN}'
    )
    with pytest.raises(worker.YouTubeProtocolError):
        worker.decode_response(len(invalid_number).to_bytes(4, "big") + invalid_number)


@pytest.mark.parametrize(
    "data",
    [
        {"item": _fork_item_value(), "truncated": False, "unknown": True},
        {"truncated": False},
        {"item": _fork_item_value(), "truncated": 1},
        {"item": {**_fork_item_value(), "unknown": True}, "truncated": False},
        {
            "item": {**_fork_item_value(), "text": "value\x00hidden"},
            "truncated": False,
        },
        {
            "item": {
                **_fork_item_value(),
                "text": "x" * (worker.MAX_TEXT_CHARACTERS + 1),
            },
            "truncated": False,
        },
        {
            "item": {**_fork_item_value(), "native_id": "invalid"},
            "truncated": False,
        },
        {
            "item": {**_fork_item_value(), "published_at": "2026-02-31"},
            "truncated": False,
        },
        {
            "item": {**_fork_item_value(), "duration_seconds": True},
            "truncated": False,
        },
    ],
)
def test_parent_decoder_revalidates_closed_read_video_frame(data: object) -> None:
    frame = worker._encode_frame(
        {
            "protocol_version": "v1",
            "operation": "read.video",
            "ok": True,
            "data": data,
        },
        worker.MAX_OUTPUT_BYTES,
    )

    with pytest.raises(worker.YouTubeProtocolError):
        worker.decode_response(frame)


def test_parent_decoder_rejects_fork_error_details() -> None:
    frame = worker._encode_frame(
        {
            "protocol_version": "v1",
            "operation": "read.video",
            "ok": False,
            "error": {"code": "not_found", "message": "private"},
        },
        worker.MAX_OUTPUT_BYTES,
    )

    with pytest.raises(worker.YouTubeProtocolError):
        worker.decode_response(frame)


def test_worker_json_bounds_cover_depth_items_nodes_and_strings() -> None:
    deep: object = None
    for _ in range(worker.MAX_JSON_DEPTH + 2):
        deep = {"next": deep}

    assert worker._json_within_bounds(deep) is False
    assert worker._json_within_bounds([None] * (worker.MAX_JSON_ITEMS + 1)) is False
    assert worker._json_within_bounds([None] * worker.MAX_JSON_NODES) is False
    assert worker._json_within_bounds("x" * (worker.MAX_STRING_BYTES + 1)) is False


@pytest.mark.parametrize(
    "url",
    [
        "http://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "https://youtube.com/watch?v=dQw4w9WgXcQ",
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=private",
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ#fragment",
        "https://www.youtube.com/watch?v=invalid",
    ],
)
def test_worker_rejects_noncanonical_video_urls(url: str) -> None:
    with pytest.raises(worker.YouTubeProtocolError):
        worker.encode_request("read.video", url=url)


def test_real_worker_module_rejects_empty_input_without_output() -> None:
    completed = subprocess.run(
        [sys.executable, "-I", "-m", "hermes_reach.sources.youtube_worker"],
        input=b"",
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 1
    assert completed.stdout == b""
    assert completed.stderr == b""


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
        self.pid = 8642
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


def _framed_response(operation: worker.WorkerOperation, data: object) -> bytes:
    return worker._encode_frame(
        worker._success_response(operation, data), worker.MAX_OUTPUT_BYTES
    )


def test_parent_uses_fixed_argv_private_environment_and_cleans_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _Process(_framed_response("search.videos", []))
    captured: dict[str, object] = {}

    async def create(*args: str, **kwargs: object) -> _Process:
        captured["args"] = args
        captured["kwargs"] = kwargs
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)

    result = asyncio.run(
        YouTubeWorker().execute("search.videos", query="private query", limit=2)
    )

    assert result["data"] == []
    assert captured["args"] == (
        sys.executable,
        "-I",
        "-m",
        "hermes_reach.sources.youtube_worker",
    )
    assert "private query" not in cast(tuple[str, ...], captured["args"])
    kwargs = cast(dict[str, object], captured["kwargs"])
    environment = cast(dict[str, str], kwargs["env"])
    assert "PATH" not in environment
    assert "private query" not in str(environment)
    assert environment["YTDLP_NO_PLUGINS"] == "1"
    assert environment["HTTP_PROXY"] == ""
    assert environment["NO_PROXY"] == "*"
    assert Path(environment["DENO_DIR"]).exists() is False
    request = worker._read_request(io.BytesIO(process.stdin.value))
    assert request == worker.WorkerRequest(
        "search.videos", query="private query", limit=2
    )
    assert process.stdin.closed


@pytest.mark.parametrize("terminal", ["timeout", "cancel"])
def test_timeout_and_cancellation_kill_reap_then_remove_private_state(
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
        youtube.os,
        "killpg",
        lambda pid, requested: killed.append((pid, requested)),
    )

    async def exercise() -> None:
        task = asyncio.create_task(YouTubeWorker().execute("read.video", url=VIDEO_URL))
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
def test_invalid_terminal_paths_kill_and_reap_the_process_group(
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
        youtube.os,
        "killpg",
        lambda pid, requested: killed.append((pid, requested)),
    )

    with pytest.raises(YouTubeWorkerError) as raised:
        asyncio.run(YouTubeWorker().execute("read.video", url=VIDEO_URL))

    assert raised.value.failure_class == failure_class
    assert killed == [(process.pid, signal.SIGKILL)]
    assert process.waits == 1
