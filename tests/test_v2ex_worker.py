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

import hermes_reach.sources.v2ex as v2ex
import hermes_reach.sources.v2ex_worker as worker
from hermes_reach.sources.v2ex import V2exWorker, V2exWorkerError


class _ExecutionRequest:
    def __init__(
        self,
        protocol_version: str,
        source: str,
        operation: str,
        arguments: Mapping[str, object],
    ) -> None:
        self.protocol_version = protocol_version
        self.source = source
        self.operation = operation
        self.arguments = dict(arguments)


class _NetworkAccess:
    pass


class _ExecutionLimits:
    def __init__(self, *, maximum_items: int, maximum_text_characters: int) -> None:
        self.maximum_items = maximum_items
        self.maximum_text_characters = maximum_text_characters


class _ExecutionContext:
    def __init__(
        self,
        host_capabilities: tuple[object, ...],
        *,
        limits: _ExecutionLimits,
    ) -> None:
        self.host_capabilities = host_capabilities
        self.limits = limits


class _ExecutionItem:
    def __init__(self, schema_id: str, fields: Mapping[str, object]) -> None:
        self.schema_id = schema_id
        self.fields = dict(fields)


class _ExecutionSuccess:
    def __init__(
        self,
        operation: str,
        items: tuple[_ExecutionItem, ...],
        *,
        truncated: bool = False,
        partial_error_code: str | None = None,
    ) -> None:
        self.protocol_version = "v1"
        self.source = "v2ex"
        self.operation = operation
        self.backend_id = "v2ex-public-api"
        self.backend_version = "legacy-json-2026-07-31"
        self.items = items
        self.truncated = truncated
        self.partial_error_code = partial_error_code


class _ExecutionFailure:
    def __init__(self, operation: str, error_code: str) -> None:
        self.protocol_version = "v1"
        self.source = "v2ex"
        self.operation = operation
        self.backend_id = "v2ex-public-api"
        self.backend_version = "legacy-json-2026-07-31"
        self.error_code = error_code


def _api(execute: Callable[[object, object], object]) -> SimpleNamespace:
    return SimpleNamespace(
        execution_request_type=_ExecutionRequest,
        network_access_type=_NetworkAccess,
        execution_limits_type=_ExecutionLimits,
        execution_context_type=_ExecutionContext,
        execution_item_type=_ExecutionItem,
        execution_success_type=_ExecutionSuccess,
        execution_failure_type=_ExecutionFailure,
        execute=execute,
    )


def _topic_fields(
    *,
    native_id: str = "42",
    node: str = "python",
    url: str | None = None,
) -> dict[str, object]:
    return {
        "author": "alice",
        "native_id": native_id,
        "node": node,
        "published_at": "2023-11-14T22:13:20+00:00",
        "text": "Topic body",
        "title": "Topic title",
        "url": url if url is not None else f"https://www.v2ex.com/t/{native_id}",
    }


def _reply_fields(
    *,
    native_id: str = "7",
    topic_id: str = "42",
    url: str | None = None,
) -> dict[str, object]:
    return {
        "author": "bob",
        "native_id": native_id,
        "published_at": None,
        "text": "Reply body",
        "url": (
            url
            if url is not None
            else f"https://www.v2ex.com/t/{topic_id}#reply{native_id}"
        ),
    }


def _profile_fields(
    *,
    username: str = "Alice",
    url: str | None = None,
) -> dict[str, object]:
    return {
        "native_id": "9",
        "published_at": None,
        "text": "Bio Shanghai",
        "title": username,
        "url": url if url is not None else f"https://www.v2ex.com/member/{username}",
    }


def _item(schema: str, fields: Mapping[str, object]) -> _ExecutionItem:
    return _ExecutionItem(schema, fields)


def _frame_item(schema: str, fields: Mapping[str, object]) -> dict[str, object]:
    return {"fields": dict(fields), "schema": schema}


def _success_value(
    operation: worker.WorkerOperation,
    *,
    items: list[dict[str, object]] | None = None,
    truncated: bool = False,
    partial: str | None = None,
) -> dict[str, object]:
    if items is None:
        if operation in {"browse.hot", "browse.node_topics"}:
            items = [_frame_item("v2ex.topic.v1", _topic_fields())]
        elif operation == "read.topic":
            items = [
                _frame_item("v2ex.topic.v1", _topic_fields()),
                _frame_item("v2ex.reply.v1", _reply_fields()),
            ]
        else:
            items = [_frame_item("v2ex.profile.v1", _profile_fields())]
    return {
        "backend": {
            "id": "v2ex-public-api",
            "version": "legacy-json-2026-07-31",
        },
        "items": items,
        "operation": operation,
        "partial": partial,
        "protocol": "v1",
        "source": "v2ex",
        "truncated": truncated,
    }


def _framed(value: Mapping[str, object]) -> bytes:
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


@pytest.mark.parametrize(
    ("worker_request", "expected_arguments", "expected_limit"),
    [
        (worker.WorkerRequest("browse.hot", limit=3), {"limit": 3}, 3),
        (
            worker.WorkerRequest(
                "browse.node_topics",
                node="python",
                page=4,
                limit=5,
            ),
            {"node": "python", "page": 4, "limit": 5},
            5,
        ),
        (
            worker.WorkerRequest("read.topic", topic_id="0042"),
            {"topic_id": "0042"},
            21,
        ),
        (
            worker.WorkerRequest("read.user", username="alice"),
            {"username": "alice"},
            1,
        ),
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
        request = cast(_ExecutionRequest, execution_request)
        if request.operation == "read.topic":
            items = (
                _item("v2ex.topic.v1", _topic_fields()),
                _item("v2ex.reply.v1", _reply_fields()),
            )
        elif request.operation == "read.user":
            items = (_item("v2ex.profile.v1", _profile_fields()),)
        elif request.operation == "browse.node_topics":
            items = (_item("v2ex.topic.v1", _topic_fields()),)
        else:
            items = ()
        return _ExecutionSuccess(request.operation, items)

    value = worker._execute_request(
        worker_request,
        execution_api_provider=lambda: _api(execute),  # type: ignore[arg-type]
    )

    assert value["operation"] == worker_request.operation
    assert len(calls) == 1
    execution_request = cast(_ExecutionRequest, calls[0][0])
    context = cast(_ExecutionContext, calls[0][1])
    assert execution_request.protocol_version == "v1"
    assert execution_request.source == "v2ex"
    assert execution_request.operation == worker_request.operation
    assert execution_request.arguments == expected_arguments
    assert len(context.host_capabilities) == 1
    assert type(context.host_capabilities[0]) is _NetworkAccess
    assert context.limits.maximum_items == expected_limit
    assert context.limits.maximum_text_characters == worker.MAX_TEXT_CHARACTERS


def test_worker_uses_v2ex_runtime_integrity_handshake(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requested: list[object] = []
    sentinel = cast(worker.AgentReachExecutionApi, object())

    def validate(**kwargs: object) -> worker.AgentReachExecutionApi:
        requested.append(kwargs)
        return sentinel

    monkeypatch.setattr(worker, "validate_agent_reach_execution_contract", validate)

    assert worker._load_execution_api() is sentinel
    assert requested == [{"runtime_module": "v2ex"}]


@pytest.mark.parametrize(
    "result",
    [
        object(),
        SimpleNamespace(
            protocol_version="v1",
            source="v2ex",
            operation="browse.hot",
            backend_id="v2ex-public-api",
            backend_version="legacy-json-2026-07-31",
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
        execution_api_provider=lambda: _api(lambda *_: result),  # type: ignore[arg-type]
    )
    assert value == worker._failure_value("browse.hot", "backend_contract_violation")

    provider_failure = worker._execute_request(
        worker.WorkerRequest("browse.hot", limit=1),
        execution_api_provider=lambda: (_ for _ in ()).throw(RuntimeError("private")),
    )
    assert provider_failure == worker._failure_value(
        "browse.hot", "backend_contract_violation"
    )


def test_worker_revalidates_item_identity_and_partial_sequence_before_stdout() -> None:
    malformed = worker._execute_request(
        worker.WorkerRequest("read.topic", topic_id="42"),
        execution_api_provider=lambda: _api(  # type: ignore[arg-type]
            lambda *_: _ExecutionSuccess(
                "read.topic",
                (
                    _item("v2ex.topic.v1", _topic_fields()),
                    _item("v2ex.reply.v1", _reply_fields(topic_id="41")),
                ),
            )
        ),
    )
    invalid_partial = worker._execute_request(
        worker.WorkerRequest("read.topic", topic_id="42"),
        execution_api_provider=lambda: _api(  # type: ignore[arg-type]
            lambda *_: _ExecutionSuccess(
                "read.topic",
                (
                    _item("v2ex.topic.v1", _topic_fields()),
                    _item("v2ex.reply.v1", _reply_fields()),
                ),
                partial_error_code="transient",
            )
        ),
    )

    assert malformed == worker._failure_value(
        "read.topic", "backend_contract_violation"
    )
    assert invalid_partial == worker._failure_value(
        "read.topic", "backend_contract_violation"
    )


def test_worker_preserves_topic_only_partial_after_complete_validation() -> None:
    value = worker._execute_request(
        worker.WorkerRequest("read.topic", topic_id="0042"),
        execution_api_provider=lambda: _api(  # type: ignore[arg-type]
            lambda *_: _ExecutionSuccess(
                "read.topic",
                (_item("v2ex.topic.v1", _topic_fields()),),
                truncated=True,
                partial_error_code="transient",
            )
        ),
    )

    decoded = worker.decode_response(
        _framed(value),
        operation="read.topic",
        topic_id="0042",
    )

    assert decoded == worker.V2exProjection(
        "read.topic",
        (
            worker.V2exTopicProjection(
                "Topic body",
                "42",
                "Topic title",
                "https://www.v2ex.com/t/42",
                "alice",
                "2023-11-14T22:13:20+00:00",
                "python",
            ),
        ),
        True,
        "transient",
    )


def test_worker_serializes_only_closed_fork_failure() -> None:
    value = worker._execute_request(
        worker.WorkerRequest("browse.hot", limit=1),
        execution_api_provider=lambda: _api(  # type: ignore[arg-type]
            lambda *_: _ExecutionFailure("browse.hot", "rate_limit")
        ),
    )

    assert value == worker._failure_value("browse.hot", "rate_limit")
    assert "message" not in json.dumps(value)


@pytest.mark.parametrize(
    "encode",
    [
        lambda: worker.encode_request("browse.hot"),
        lambda: worker.encode_request("browse.hot", limit=True),
        lambda: worker.encode_request(
            "browse.node_topics", node="bad node", page=1, limit=1
        ),
        lambda: worker.encode_request(
            "browse.node_topics", node="python", page=0, limit=1
        ),
        lambda: worker.encode_request("read.topic", topic_id="0"),
        lambda: worker.encode_request("read.topic", topic_id="+42"),
        lambda: worker.encode_request("read.user", username="bad/user"),
        lambda: worker.encode_request("read.user", username="alice", limit=1),
    ],
)
def test_request_encoder_rejects_missing_extra_and_out_of_range_values(
    encode: Callable[[], bytes],
) -> None:
    with pytest.raises(worker.V2exProtocolError):
        encode()


def test_worker_rejects_unknown_fields_and_trailing_request_data() -> None:
    payload = json.dumps(
        {
            "protocol_version": "v1",
            "operation": "browse.hot",
            "limit": 1,
            "endpoint": "private",
        }
    ).encode()
    framed = len(payload).to_bytes(4, "big") + payload

    with pytest.raises(worker.V2exProtocolError):
        worker._read_request(io.BytesIO(framed))
    with pytest.raises(worker.V2exProtocolError):
        worker._read_request(
            io.BytesIO(worker.encode_request("browse.hot", limit=1) + b"x")
        )


def test_frames_keep_bounded_cjk_as_utf8_and_reject_duplicate_keys() -> None:
    payload = {"text": "中文边界"}
    expected = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")

    framed = worker._encode_frame(payload, len(expected))

    assert framed[4:] == expected
    assert worker._decode_frame(framed, len(expected)) == payload
    duplicate = b'{"protocol_version":"v1","protocol_version":"v1"}'
    with pytest.raises(worker.V2exProtocolError):
        worker._load_json(duplicate, maximum=len(duplicate))


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: {**value, "source": "rss"},
        lambda value: {**value, "operation": "browse.node_topics"},
        lambda value: {**value, "partial": "transient"},
        lambda value: {**value, "unknown": True},
        lambda value: {
            **value,
            "backend": {"id": "other", "version": "legacy-json-2026-07-31"},
        },
        lambda value: {
            **value,
            "items": [
                _frame_item("future.topic.v2", _topic_fields()),
            ],
        },
        lambda value: {
            **value,
            "items": [
                _frame_item(
                    "v2ex.topic.v1",
                    {**_topic_fields(), "unknown": True},
                )
            ],
        },
        lambda value: {
            **value,
            "items": [
                _frame_item(
                    "v2ex.topic.v1",
                    _topic_fields(url="https://evil.test/t/42"),
                )
            ],
        },
        lambda value: {
            **value,
            "items": [
                _frame_item(
                    "v2ex.topic.v1",
                    {**_topic_fields(), "native_id": 42},
                )
            ],
        },
        lambda value: {
            **value,
            "items": [
                _frame_item(
                    "v2ex.topic.v1",
                    {**_topic_fields(), "author": "bad author"},
                )
            ],
        },
        lambda value: {
            **value,
            "items": [
                _frame_item(
                    "v2ex.topic.v1",
                    {**_topic_fields(), "published_at": "2023-11-14T22:13:20Z"},
                )
            ],
        },
        lambda value: {
            **value,
            "items": [
                _frame_item(
                    "v2ex.topic.v1",
                    {**_topic_fields(), "text": "not  normalized"},
                )
            ],
        },
    ],
)
def test_parent_decoder_revalidates_complete_closed_result(
    mutation: Callable[[dict[str, object]], dict[str, object]],
) -> None:
    with pytest.raises(worker.V2exProtocolError):
        worker.decode_response(
            _framed(mutation(_success_value("browse.hot"))),
            operation="browse.hot",
            limit=1,
        )


def test_parent_decoder_correlates_node_page_limit_and_unique_topics() -> None:
    valid = _success_value(
        "browse.node_topics",
        items=[
            _frame_item("v2ex.topic.v1", _topic_fields(node="Python")),
        ],
        truncated=True,
    )

    decoded = worker.decode_response(
        _framed(valid),
        operation="browse.node_topics",
        node="python",
        page=3,
        limit=1,
    )

    assert decoded == worker.V2exProjection(
        "browse.node_topics",
        (
            worker.V2exTopicProjection(
                "Topic body",
                "42",
                "Topic title",
                "https://www.v2ex.com/t/42",
                "alice",
                "2023-11-14T22:13:20+00:00",
                "Python",
            ),
        ),
        True,
        None,
    )
    for items in (
        [
            _frame_item("v2ex.topic.v1", _topic_fields(node="go")),
        ],
        [
            _frame_item("v2ex.topic.v1", _topic_fields()),
            _frame_item("v2ex.topic.v1", _topic_fields()),
        ],
        [
            _frame_item("v2ex.topic.v1", _topic_fields(native_id="1")),
            _frame_item("v2ex.topic.v1", _topic_fields(native_id="2")),
        ],
    ):
        with pytest.raises(worker.V2exProtocolError):
            worker.decode_response(
                _framed(_success_value("browse.node_topics", items=items)),
                operation="browse.node_topics",
                node="python",
                page=3,
                limit=1,
            )


def test_parent_decoder_correlates_canonical_topic_and_reply_order() -> None:
    decoded = worker.decode_response(
        _framed(_success_value("read.topic")),
        operation="read.topic",
        topic_id="0042",
    )

    assert isinstance(decoded, worker.V2exProjection)
    assert [item.schema_id for item in decoded.items] == [
        "v2ex.topic.v1",
        "v2ex.reply.v1",
    ]
    assert [item.native_id for item in decoded.items] == ["42", "7"]

    invalid_items = (
        [_frame_item("v2ex.topic.v1", _topic_fields(native_id="41"))],
        [
            _frame_item("v2ex.reply.v1", _reply_fields()),
            _frame_item("v2ex.topic.v1", _topic_fields()),
        ],
        [
            _frame_item("v2ex.topic.v1", _topic_fields()),
            _frame_item("v2ex.reply.v1", _reply_fields(topic_id="41")),
        ],
        [
            _frame_item("v2ex.topic.v1", _topic_fields()),
            _frame_item("v2ex.reply.v1", _reply_fields()),
            _frame_item("v2ex.reply.v1", _reply_fields()),
        ],
    )
    for items in invalid_items:
        with pytest.raises(worker.V2exProtocolError):
            worker.decode_response(
                _framed(_success_value("read.topic", items=items)),
                operation="read.topic",
                topic_id="42",
            )


def test_parent_decoder_accepts_only_topic_only_closed_partial() -> None:
    value = _success_value(
        "read.topic",
        items=[_frame_item("v2ex.topic.v1", _topic_fields())],
        partial="backend_contract_violation",
    )

    decoded = worker.decode_response(
        _framed(value),
        operation="read.topic",
        topic_id="42",
    )

    assert isinstance(decoded, worker.V2exProjection)
    assert decoded.partial_error_code == "backend_contract_violation"
    assert len(decoded.items) == 1

    for invalid in (
        _success_value("browse.hot", partial="transient"),
        _success_value("read.topic", partial="invalid_input"),
        _success_value("read.topic", partial="transient"),
    ):
        kwargs: dict[str, object] = (
            {"operation": "browse.hot", "limit": 1}
            if invalid["operation"] == "browse.hot"
            else {"operation": "read.topic", "topic_id": "42"}
        )
        with pytest.raises(worker.V2exProtocolError):
            worker.decode_response(_framed(invalid), **kwargs)  # type: ignore[arg-type]


def test_parent_decoder_correlates_profile_case_insensitively() -> None:
    decoded = worker.decode_response(
        _framed(_success_value("read.user")),
        operation="read.user",
        username="alice",
    )

    assert isinstance(decoded, worker.V2exProjection)
    assert decoded.items[0].schema_id == "v2ex.profile.v1"

    with pytest.raises(worker.V2exProtocolError):
        worker.decode_response(
            _framed(_success_value("read.user")),
            operation="read.user",
            username="bob",
        )


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: {**value, "source": "rss"},
        lambda value: {**value, "operation": "read.user"},
        lambda value: {**value, "error": {"code": "future_error"}},
        lambda value: {**value, "error": {"code": "transient", "detail": "x"}},
        lambda value: {**value, "unknown": True},
    ],
)
def test_parent_decoder_revalidates_closed_failure_envelope(
    mutation: Callable[[dict[str, object]], dict[str, object]],
) -> None:
    with pytest.raises(worker.V2exProtocolError):
        worker.decode_response(
            _framed(
                mutation(
                    worker._failure_value(
                        "browse.hot",
                        "transient",
                    )
                )
            ),
            operation="browse.hot",
            limit=1,
        )


def test_real_worker_module_rejects_empty_input_without_output() -> None:
    completed = subprocess.run(
        [sys.executable, "-I", "-m", "hermes_reach.sources.v2ex_worker"],
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
    process = _Process(_framed(_success_value("browse.node_topics", items=[])))
    captured: dict[str, object] = {}

    async def create(*args: str, **kwargs: object) -> _Process:
        captured["args"] = args
        captured["kwargs"] = kwargs
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)

    result = asyncio.run(
        V2exWorker().execute(
            "browse.node_topics",
            node="private-node",
            page=3,
            limit=2,
        )
    )

    assert result == worker.V2exProjection("browse.node_topics", (), False, None)
    assert captured["args"] == (
        sys.executable,
        "-I",
        "-m",
        "hermes_reach.sources.v2ex_worker",
    )
    assert "private-node" not in cast(tuple[str, ...], captured["args"])
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
    assert "PATH" not in environment
    cwd = Path(cast(str, kwargs["cwd"]))
    assert cwd == Path(environment["HOME"]).parent
    assert cwd.exists() is False
    request = worker._read_request(io.BytesIO(process.stdin.value))
    assert request == worker.WorkerRequest(
        "browse.node_topics",
        node="private-node",
        page=3,
        limit=2,
    )
    assert process.stdin.closed


def test_parent_rejects_non_absolute_python_before_process_or_state_creation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spawned = False
    temporary_created = False

    async def create(*_: str, **__: object) -> _Process:
        nonlocal spawned
        spawned = True
        return _Process()

    class ForbiddenTemporaryDirectory:
        def __init__(self, *_: object, **__: object) -> None:
            nonlocal temporary_created
            temporary_created = True

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    monkeypatch.setattr(v2ex.sys, "executable", "python")
    monkeypatch.setattr(
        v2ex.tempfile, "TemporaryDirectory", ForbiddenTemporaryDirectory
    )

    with pytest.raises(V2exWorkerError) as raised:
        asyncio.run(V2exWorker().execute("browse.hot", limit=1))

    assert raised.value.failure_class == "permanent"
    assert spawned is False
    assert temporary_created is False


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
        ("backend_unavailable", "permanent"),
        ("backend_incompatible", "permanent"),
        ("permanent", "permanent"),
        ("backend_contract_violation", "permanent"),
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
    monkeypatch.setattr(v2ex.os, "killpg", lambda *_: None)

    with pytest.raises(V2exWorkerError) as raised:
        asyncio.run(V2exWorker().execute("browse.hot", limit=1))

    assert raised.value.failure_class == expected
    assert code not in str(raised.value)


def test_parent_preserves_transient_fallback_for_unexpected_worker_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def create(*_: str, **__: object) -> _Process:
        raise RuntimeError("sensitive unexpected failure")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)

    with pytest.raises(V2exWorkerError) as raised:
        asyncio.run(V2exWorker().execute("browse.hot", limit=1))

    assert raised.value.failure_class == "transient"
    assert "sensitive" not in str(raised.value)


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
        v2ex.os,
        "killpg",
        lambda pid, requested: killed.append((pid, requested)),
    )

    async def exercise() -> None:
        task = asyncio.create_task(V2exWorker().execute("browse.hot", limit=2))
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
    canary = b"private-output-canary"
    process = _Process(
        (canary * ((worker.MAX_OUTPUT_BYTES // len(canary)) + 2))[
            : worker.MAX_OUTPUT_BYTES + 5
        ]
    )

    async def create(*_: str, **__: object) -> _Process:
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    monkeypatch.setattr(v2ex.os, "killpg", lambda *_: None)

    with pytest.raises(V2exWorkerError) as raised:
        asyncio.run(V2exWorker().execute("browse.hot", limit=1))

    assert raised.value.failure_class == "permanent"
    assert canary.decode() not in str(raised.value)


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
        v2ex.os,
        "killpg",
        lambda pid, requested: killed.append((pid, requested)),
    )

    with pytest.raises(V2exWorkerError) as raised:
        asyncio.run(V2exWorker().execute("browse.hot", limit=1))

    assert raised.value.failure_class == failure_class
    assert killed == [(process.pid, signal.SIGKILL)]
    assert process.waits == 1


def test_hermes_v2ex_sources_contain_no_platform_api_route_or_native_parser() -> None:
    source_root = Path(v2ex.__file__).parent
    combined = "\n".join(
        (source_root / name).read_text() for name in ("v2ex.py", "v2ex_worker.py")
    )

    assert "/api/" not in combined
    assert "topics/show" not in combined
    assert "replies/show" not in combined
    assert "members/show" not in combined
