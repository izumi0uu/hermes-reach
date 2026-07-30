from __future__ import annotations

import asyncio
import importlib
import io
import json
import signal
import subprocess
import sys
from collections.abc import Callable
from dataclasses import replace
from types import MappingProxyType
from typing import cast

import pytest

import hermes_reach.sources.rss as rss
import hermes_reach.sources.rss_worker as rss_worker
from hermes_reach.agent_reach_bridge import (
    AgentReachBridgeError,
    load_agent_reach_catalog,
    validate_agent_reach_execution_contract,
)
from hermes_reach.sources.rss import FeedparserWorker, FeedparserWorkerError
from hermes_reach.sources.rss_worker import (
    MAX_OUTPUT_BYTES,
    FeedparserProjection,
    ForkExecutionFailure,
    WorkerOperation,
    decode_response,
    encode_request,
)

FEED_URL = "https://example.com/feed.xml"
ATOM = b"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Example feed</title><subtitle>Feed body</subtitle>
  <entry><id>https://example.com/entry-1?private=yes#fragment</id>
    <title>Entry title</title>
    <content type="html">&lt;p&gt;Preferred body&lt;/p&gt;</content>
    <summary>Fallback body</summary><author><name>Alice</name></author>
    <link href="/entry?tracking=yes" />
    <updated>2026-07-27T01:02:03Z</updated>
  </entry>
</feed>"""


def _backend() -> dict[str, str]:
    return {"id": "feedparser", "version": "6.0.12"}


def _success_value(
    operation: WorkerOperation = "browse.entries",
    *,
    items: list[dict[str, object]] | None = None,
    partial: str | None = None,
    truncated: bool = False,
) -> dict[str, object]:
    return {
        "backend": _backend(),
        "items": [] if items is None else items,
        "operation": operation,
        "partial": partial,
        "protocol": "v1",
        "schema": "rss.feed.v1" if operation == "read.feed" else "rss.entry.v1",
        "source": "rss",
        "truncated": truncated,
    }


def _failure_value(
    code: str,
    operation: WorkerOperation = "browse.entries",
) -> dict[str, object]:
    return {
        "backend": _backend(),
        "error": {"code": code},
        "operation": operation,
        "protocol": "v1",
        "source": "rss",
    }


def _encoded(value: object) -> bytes:
    return json.dumps(value, separators=(",", ":"), sort_keys=True).encode()


def _entry_fields(**overrides: object) -> dict[str, object]:
    fields: dict[str, object] = {
        "author": "Alice",
        "native_id": "https://example.com/entry-1",
        "published_at": "2026-07-27T01:02:03Z",
        "text": "<p>Preferred body</p>",
        "title": "Entry title",
        "url": "https://example.com/entry",
    }
    fields.update(overrides)
    return fields


def _feed_fields(**overrides: object) -> dict[str, object]:
    fields: dict[str, object] = {
        "text": "Feed body",
        "title": "Example feed",
        "url": "https://example.com/feed",
    }
    fields.update(overrides)
    return fields


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


class _BrokenWriter(_Writer):
    async def drain(self) -> None:
        raise BrokenPipeError("private pipe details")


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
        writer: _Writer | None = None,
    ) -> None:
        self.pid = 2_147_483_647
        self.returncode: int | None = None
        self._wait_returncode = returncode
        self.stdin = _Writer() if writer is None else writer
        self.stdout = _Reader(output) if reader is None else reader
        self.direct_kills = 0
        self.waits = 0

    async def wait(self) -> int:
        self.waits += 1
        if self.returncode is None:
            self.returncode = self._wait_returncode
        return self.returncode

    def kill(self) -> None:
        self.direct_kills += 1


@pytest.mark.parametrize(
    ("operation", "max_entries", "expected_arguments", "schema", "fields"),
    [
        ("read.feed", 1, {}, "rss.feed.v1", _feed_fields()),
        (
            "browse.entries",
            7,
            {"max_entries": 7},
            "rss.entry.v1",
            _entry_fields(),
        ),
    ],
)
def test_worker_constructs_closed_fork_request_document_limits_and_context(
    operation: WorkerOperation,
    max_entries: int,
    expected_arguments: dict[str, int],
    schema: str,
    fields: dict[str, object],
) -> None:
    execution = importlib.import_module("agent_reach.execution.v1")
    captured: dict[str, object] = {}

    def execute(request: object, context: object) -> object:
        captured["request"] = request
        captured["context"] = context
        item = execution.ExecutionItemV1(schema, fields)
        return execution.ExecutionSuccessV1(
            "v1",
            "rss",
            operation,
            "feedparser",
            "6.0.12",
            (item,),
        )

    execution_api = replace(
        validate_agent_reach_execution_contract(),
        execute=execute,
    )
    framed = encode_request(
        ATOM,
        operation=operation,
        content_type="",
        content_location=FEED_URL,
        max_entries=max_entries,
    )
    request = rss_worker._read_request(io.BytesIO(framed))

    response = rss_worker._execute_request(
        request,
        execution_api_provider=lambda: execution_api,
    )
    decoded = decode_response(
        rss_worker._response_bytes(response),
        operation=operation,
        max_entries=max_entries,
    )

    assert isinstance(decoded, FeedparserProjection)
    execution_request = captured["request"]
    assert execution_request.protocol_version == "v1"
    assert execution_request.source == "rss"
    assert execution_request.operation == operation
    assert dict(execution_request.arguments) == expected_arguments
    context = captured["context"]
    assert len(context.host_capabilities) == 1
    document = context.host_capabilities[0]
    assert document.body == ATOM
    assert document.content_type == ""
    assert document.content_location == FEED_URL
    assert context.limits.maximum_items == max_entries
    assert context.limits.maximum_text_characters == 16_000


def test_worker_converts_fork_contract_drift_to_a_closed_failure() -> None:
    execution_api = replace(
        validate_agent_reach_execution_contract(),
        execute=lambda *_: object(),
    )
    request = rss_worker._read_request(
        io.BytesIO(
            encode_request(
                ATOM,
                operation="read.feed",
                content_type="application/atom+xml",
                content_location=FEED_URL,
                max_entries=1,
            )
        )
    )

    decoded = decode_response(
        rss_worker._response_bytes(
            rss_worker._execute_request(
                request,
                execution_api_provider=lambda: execution_api,
            )
        ),
        operation="read.feed",
        max_entries=1,
    )

    assert decoded == ForkExecutionFailure("read.feed", "backend_contract_violation")


def test_registration_then_worker_drift_revalidates_and_never_calls_execute() -> None:
    load_agent_reach_catalog()
    execute_calls = 0
    validation_calls = 0

    def execute_forbidden(*_: object) -> object:
        nonlocal execute_calls
        execute_calls += 1
        raise AssertionError("fork execute must remain unreachable")

    drifted_api = replace(
        validate_agent_reach_execution_contract(),
        execute=execute_forbidden,
    )

    def drifted_provider() -> object:
        nonlocal validation_calls
        validation_calls += 1
        assert drifted_api.execute is execute_forbidden
        raise AgentReachBridgeError("PRIVATE CONTRACT DRIFT")

    request = rss_worker._read_request(
        io.BytesIO(
            encode_request(
                ATOM,
                operation="read.feed",
                content_type="application/atom+xml",
                content_location=FEED_URL,
                max_entries=1,
            )
        )
    )

    for _ in range(2):
        raw = rss_worker._response_bytes(
            rss_worker._execute_request(
                request,
                execution_api_provider=cast(
                    rss_worker.ExecutionApiProvider,
                    drifted_provider,
                ),
            )
        )
        assert decode_response(
            raw,
            operation="read.feed",
            max_entries=1,
        ) == ForkExecutionFailure("read.feed", "backend_contract_violation")
        assert b"PRIVATE" not in raw

    assert validation_calls == 2
    assert execute_calls == 0


def test_default_worker_handshake_revalidates_runtime_module_every_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    validation_calls = 0

    def reject_runtime_drift(*, runtime_module: str | None = None) -> object:
        nonlocal validation_calls
        validation_calls += 1
        assert runtime_module == "rss"
        raise AgentReachBridgeError("PRIVATE RUNTIME DRIFT")

    monkeypatch.setattr(
        rss_worker,
        "validate_agent_reach_execution_contract",
        reject_runtime_drift,
    )
    request = rss_worker._read_request(
        io.BytesIO(
            encode_request(
                ATOM,
                operation="read.feed",
                content_type="application/atom+xml",
                content_location=FEED_URL,
                max_entries=1,
            )
        )
    )

    for _ in range(2):
        raw = rss_worker._response_bytes(rss_worker._execute_request(request))
        assert decode_response(
            raw,
            operation="read.feed",
            max_entries=1,
        ) == ForkExecutionFailure("read.feed", "backend_contract_violation")
        assert b"PRIVATE" not in raw

    assert validation_calls == 2


def test_worker_rejects_open_fork_fields_before_writing_stdout() -> None:
    execution = importlib.import_module("agent_reach.execution.v1")
    item = execution.ExecutionItemV1("rss.entry.v1", _entry_fields())
    success = execution.ExecutionSuccessV1(
        "v1",
        "rss",
        "browse.entries",
        "feedparser",
        "6.0.12",
        (item,),
    )
    object.__setattr__(
        item,
        "fields",
        MappingProxyType(_entry_fields(private="OUTPUT_CANARY")),
    )
    execution_api = replace(
        validate_agent_reach_execution_contract(),
        execute=lambda *_: success,
    )
    request = rss_worker._read_request(
        io.BytesIO(
            encode_request(
                ATOM,
                operation="browse.entries",
                content_type="application/atom+xml",
                content_location=FEED_URL,
                max_entries=2,
            )
        )
    )

    raw = rss_worker._response_bytes(
        rss_worker._execute_request(
            request,
            execution_api_provider=lambda: execution_api,
        )
    )

    assert b"OUTPUT_CANARY" not in raw
    assert decode_response(
        raw,
        operation="browse.entries",
        max_entries=2,
    ) == ForkExecutionFailure("browse.entries", "backend_contract_violation")


def test_real_isolated_worker_executes_fork_projection() -> None:
    result = asyncio.run(
        FeedparserWorker().parse(
            ATOM,
            operation="browse.entries",
            content_type="application/atom+xml",
            content_location=FEED_URL,
            max_entries=2,
        )
    )

    assert isinstance(result, FeedparserProjection)
    assert result.operation == "browse.entries"
    assert result.partial_error_code is None
    assert result.truncated is False
    assert [entry.text for entry in result.entries] == ["<p>Preferred body</p>"]
    assert result.entries[0].native_id == "https://example.com/entry-1"
    assert result.entries[0].url == "https://example.com/entry"


def test_isolated_worker_module_is_not_preloaded_by_package_entry_point() -> None:
    completed = subprocess.run(
        [sys.executable, "-I", "-m", "hermes_reach.sources.rss_worker"],
        input=b"",
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 1
    assert completed.stdout == b""
    assert completed.stderr == b""


def test_worker_uses_fixed_isolated_argv_environment_and_framing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _Process(_encoded(_success_value()))
    captured: dict[str, object] = {}

    async def create(*args: str, **kwargs: object) -> _Process:
        captured["args"] = args
        captured["kwargs"] = kwargs
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)

    result = asyncio.run(
        FeedparserWorker().parse(
            ATOM,
            operation="browse.entries",
            content_type="application/atom+xml",
            content_location=FEED_URL,
            max_entries=2,
        )
    )

    assert result.entries == ()
    assert captured["args"] == (
        sys.executable,
        "-I",
        "-m",
        "hermes_reach.sources.rss_worker",
    )
    kwargs = cast(dict[str, object], captured["kwargs"])
    assert kwargs["stdin"] is asyncio.subprocess.PIPE
    assert kwargs["stdout"] is asyncio.subprocess.PIPE
    assert kwargs["stderr"] is asyncio.subprocess.DEVNULL
    assert kwargs["cwd"] == "/"
    assert kwargs["env"] == {}
    assert kwargs["close_fds"] is True
    assert kwargs["start_new_session"] is True
    assert "shell" not in kwargs

    metadata_length = int.from_bytes(process.stdin.value[:4], "big")
    metadata = json.loads(process.stdin.value[4 : 4 + metadata_length])
    assert metadata == {
        "content_location": FEED_URL,
        "content_type": "application/atom+xml",
        "max_entries": 2,
        "operation": "browse.entries",
        "version": 1,
    }
    assert process.stdin.value[4 + metadata_length :] == ATOM
    assert process.stdin.closed


@pytest.mark.parametrize("terminal", ["timeout", "cancel"])
def test_timeout_and_cancellation_kill_and_reap_worker_process_group(
    monkeypatch: pytest.MonkeyPatch, terminal: str
) -> None:
    never = _NeverReader()
    process = _Process(reader=never)
    killed: list[tuple[int, signal.Signals]] = []

    async def create(*_: str, **__: object) -> _Process:
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    monkeypatch.setattr(
        rss.os,
        "killpg",
        lambda pid, requested_signal: killed.append((pid, requested_signal)),
    )

    async def exercise() -> None:
        task = asyncio.create_task(
            FeedparserWorker().parse(
                ATOM,
                operation="browse.entries",
                content_type="application/atom+xml",
                content_location=FEED_URL,
                max_entries=2,
            )
        )
        await never.entered.wait()
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


def test_worker_cleanup_falls_back_to_direct_kill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    never = _NeverReader()
    process = _Process(reader=never)

    async def create(*_: str, **__: object) -> _Process:
        return process

    def fail_group_kill(*_: object) -> None:
        raise OSError("process group unavailable")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    monkeypatch.setattr(rss.os, "killpg", fail_group_kill)

    async def exercise() -> None:
        task = asyncio.create_task(
            FeedparserWorker().parse(
                ATOM,
                operation="browse.entries",
                content_type="application/atom+xml",
                content_location=FEED_URL,
                max_entries=2,
            )
        )
        await never.entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(exercise())
    assert process.direct_kills == 1
    assert process.waits == 1


def test_process_group_cleanup_kills_and_reaps_a_real_blocked_child() -> None:
    async def exercise() -> None:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-I",
            "-c",
            "import time; time.sleep(60)",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            cwd="/",
            env={},
            close_fds=True,
            start_new_session=True,
        )
        try:
            await rss._cleanup_process_group(process, terminate_group=True)
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()

        assert process.returncode == -signal.SIGKILL

    asyncio.run(exercise())


@pytest.mark.parametrize("terminal", ["oversize", "nonzero", "malformed"])
def test_worker_bounds_and_redacts_terminal_failures(
    monkeypatch: pytest.MonkeyPatch, terminal: str
) -> None:
    if terminal == "oversize":
        output = b"OUTPUT_CANARY" * (MAX_OUTPUT_BYTES // 13 + 1)
    elif terminal == "malformed":
        output = b'{"private":"OUTPUT_CANARY"}'
    else:
        output = _encoded(_success_value())
    process = _Process(output, returncode=7 if terminal == "nonzero" else 0)
    killed: list[tuple[int, signal.Signals]] = []

    async def create(*_: str, **__: object) -> _Process:
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    monkeypatch.setattr(
        rss.os,
        "killpg",
        lambda pid, requested_signal: killed.append((pid, requested_signal)),
    )

    with pytest.raises(FeedparserWorkerError) as failed:
        asyncio.run(
            FeedparserWorker().parse(
                ATOM,
                operation="browse.entries",
                content_type="application/atom+xml",
                content_location=FEED_URL,
                max_entries=2,
            )
        )

    assert failed.value.failure_class == "permanent"
    assert "OUTPUT_CANARY" not in str(failed.value)
    assert killed == [(process.pid, signal.SIGKILL)]


@pytest.mark.parametrize(
    ("factory", "expected"),
    [
        (lambda: OSError("private launch"), "transient"),
        (lambda: ValueError("private configuration"), "permanent"),
    ],
)
def test_worker_classifies_launch_failures_without_leaking_details(
    monkeypatch: pytest.MonkeyPatch,
    factory: Callable[[], Exception],
    expected: str,
) -> None:
    async def create(*_: str, **__: object) -> _Process:
        raise factory()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)

    with pytest.raises(FeedparserWorkerError) as failed:
        asyncio.run(
            FeedparserWorker().parse(
                ATOM,
                operation="read.feed",
                content_type="application/atom+xml",
                content_location=FEED_URL,
                max_entries=1,
            )
        )

    assert failed.value.failure_class == expected
    assert "private" not in str(failed.value)


def test_worker_classifies_pipe_os_failures_as_transient(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _Process(writer=_BrokenWriter())

    async def create(*_: str, **__: object) -> _Process:
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)

    with pytest.raises(FeedparserWorkerError) as failed:
        asyncio.run(
            FeedparserWorker().parse(
                ATOM,
                operation="read.feed",
                content_type="application/atom+xml",
                content_location=FEED_URL,
                max_entries=1,
            )
        )

    assert failed.value.failure_class == "transient"


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("deadline_exceeded", "transient"),
        ("cancelled", "transient"),
        ("backend_unavailable", "permanent"),
        ("backend_incompatible", "permanent"),
        ("permanent", "permanent"),
        ("backend_contract_violation", "permanent"),
    ],
)
def test_worker_maps_closed_fork_failures(
    monkeypatch: pytest.MonkeyPatch,
    code: str,
    expected: str,
) -> None:
    process = _Process(_encoded(_failure_value(code)))

    async def create(*_: str, **__: object) -> _Process:
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)

    with pytest.raises(FeedparserWorkerError) as failed:
        asyncio.run(
            FeedparserWorker().parse(
                ATOM,
                operation="browse.entries",
                content_type="application/atom+xml",
                content_location=FEED_URL,
                max_entries=2,
            )
        )

    assert failed.value.failure_class == expected


def test_response_decoder_accepts_only_closed_correlated_success_and_failure() -> None:
    success = decode_response(
        _encoded(
            _success_value(
                items=[_entry_fields()],
                partial="permanent",
                truncated=True,
            )
        ),
        operation="browse.entries",
        max_entries=2,
    )
    failure = decode_response(
        _encoded(_failure_value("cancelled")),
        operation="browse.entries",
        max_entries=2,
    )

    assert isinstance(success, FeedparserProjection)
    assert success.partial_error_code == "permanent"
    assert success.truncated is True
    assert success.entries[0].native_id == "https://example.com/entry-1"
    assert failure == ForkExecutionFailure("browse.entries", "cancelled")


def _mutated_responses() -> list[dict[str, object]]:
    values: list[dict[str, object]] = []
    mutations: tuple[tuple[str, object], ...] = (
        ("protocol", "v2"),
        ("source", "web"),
        ("operation", "read.feed"),
        ("schema", "rss.unknown.v1"),
        ("partial", "cancelled"),
        ("truncated", 1),
    )
    for name, replacement in mutations:
        value = _success_value(items=[_entry_fields()])
        value[name] = replacement
        values.append(value)
    wrong_backend = _success_value(items=[_entry_fields()])
    wrong_backend["backend"] = {"id": "other", "version": "6.0.12"}
    values.append(wrong_backend)
    wrong_version = _success_value(items=[_entry_fields()])
    wrong_version["backend"] = {"id": "feedparser", "version": "6.0.13"}
    values.append(wrong_version)
    extra_top_level = _success_value(items=[_entry_fields()])
    extra_top_level["private"] = "OUTPUT_CANARY"
    values.append(extra_top_level)
    extra_item = _success_value(items=[_entry_fields(private="OUTPUT_CANARY")])
    values.append(extra_item)
    long_text = _success_value(items=[_entry_fields(text="x" * 16_001)])
    values.append(long_text)
    too_many = _success_value(items=[_entry_fields(), _entry_fields(), _entry_fields()])
    values.append(too_many)
    empty_partial = _success_value(partial="permanent")
    values.append(empty_partial)
    return values


@pytest.mark.parametrize("value", _mutated_responses())
def test_response_decoder_rejects_identity_backend_schema_bounds_and_item_drift(
    value: dict[str, object],
) -> None:
    with pytest.raises(rss_worker.FeedparserProtocolError):
        decode_response(
            _encoded(value),
            operation="browse.entries",
            max_entries=2,
        )


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"{}",
        b'{"backend":{"id":"feedparser","id":"other","version":"6.0.12"},'
        b'"error":{"code":"permanent"},"operation":"browse.entries",'
        b'"protocol":"v1","source":"rss"}',
        b'{"backend":{"id":"feedparser","version":"6.0.12"},'
        b'"error":{"code":"private"},"operation":"browse.entries",'
        b'"protocol":"v1","source":"rss"}',
        b'{"backend":{"id":"feedparser","version":"6.0.12"},'
        b'"items":[],"operation":"browse.entries","partial":null,'
        b'"protocol":"v1","schema":"rss.entry.v1","source":"rss",'
        b'"truncated":NaN}',
        b"X" * (MAX_OUTPUT_BYTES + 1),
    ],
)
def test_response_decoder_rejects_open_duplicate_or_oversize_protocol(
    raw: bytes,
) -> None:
    with pytest.raises(rss_worker.FeedparserProtocolError):
        decode_response(raw, operation="browse.entries", max_entries=2)


def test_response_decoder_requires_one_untruncated_feed_item() -> None:
    for value in (
        _success_value("read.feed", items=[]),
        _success_value("read.feed", items=[_feed_fields()], truncated=True),
        _success_value("read.feed", items=[_entry_fields()]),
    ):
        with pytest.raises(rss_worker.FeedparserProtocolError):
            decode_response(_encoded(value), operation="read.feed", max_entries=1)


def test_request_encoder_closes_operation_limits_and_metadata() -> None:
    empty_type = encode_request(
        ATOM,
        operation="read.feed",
        content_type="",
        content_location=FEED_URL,
        max_entries=1,
    )
    assert rss_worker._read_request(io.BytesIO(empty_type)).content_type == ""

    invalid: tuple[Callable[[], bytes], ...] = (
        lambda: encode_request(
            cast(bytes, FEED_URL),
            operation="read.feed",
            content_type="application/rss+xml",
            content_location=FEED_URL,
            max_entries=1,
        ),
        lambda: encode_request(
            ATOM,
            operation="read.feed",
            content_type="application/rss+xml\nproxy: enabled",
            content_location=FEED_URL,
            max_entries=1,
        ),
        lambda: encode_request(
            ATOM,
            operation="read.feed",
            content_type=" application/rss+xml ",
            content_location=FEED_URL,
            max_entries=1,
        ),
        lambda: encode_request(
            ATOM,
            operation="read.feed",
            content_type="application/rss+xml",
            content_location="file:///tmp/private-feed",
            max_entries=1,
        ),
        lambda: encode_request(
            ATOM,
            operation="read.feed",
            content_type="application/rss+xml",
            content_location="https://public_feed.example.com/feed.xml",
            max_entries=1,
        ),
        lambda: encode_request(
            ATOM,
            operation="read.feed",
            content_type="application/rss+xml",
            content_location=FEED_URL,
            max_entries=2,
        ),
        lambda: encode_request(
            ATOM,
            operation="browse.entries",
            content_type="application/rss+xml",
            content_location=FEED_URL,
            max_entries=0,
        ),
        lambda: encode_request(
            ATOM,
            operation="browse.entries",
            content_type="application/rss+xml",
            content_location=FEED_URL,
            max_entries=22,
        ),
    )
    for invoke in invalid:
        with pytest.raises(rss_worker.FeedparserProtocolError):
            invoke()
