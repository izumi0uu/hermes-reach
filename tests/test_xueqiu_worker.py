from __future__ import annotations

import io
import json
import time
from collections.abc import Callable, Mapping
from types import SimpleNamespace
from typing import cast

import pytest

import hermes_reach.sources.xueqiu_worker as worker

COOKIE_CANARY = "xq_a_token=secret-cookie-canary; u=fixture"


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


class _Session:
    observed: bytes = b""

    def __init__(self, cookie_header: bytearray) -> None:
        type(self).observed = bytes(cookie_header)
        self.cookie_header = bytearray(cookie_header)
        cookie_header[:] = b"\x00" * len(cookie_header)

    def close(self) -> None:
        self.cookie_header[:] = b"\x00" * len(self.cookie_header)


class _Limits:
    def __init__(self, *, maximum_items: int, maximum_text_characters: int) -> None:
        self.maximum_items = maximum_items
        self.maximum_text_characters = maximum_text_characters


class _Context:
    def __init__(
        self,
        host_capabilities: tuple[object, ...],
        *,
        checkpoint: Callable[[], None],
        limits: _Limits,
    ) -> None:
        self.host_capabilities = host_capabilities
        self.checkpoint = checkpoint
        self.limits = limits


class _Item:
    def __init__(self, fields: Mapping[str, object]) -> None:
        self.schema_id = "xueqiu.stock.v1"
        self.fields = dict(fields)


class _Success:
    def __init__(self, items: tuple[_Item, ...], *, truncated: bool = False) -> None:
        self.protocol_version = "v1"
        self.source = "xueqiu"
        self.operation = "search.stocks"
        self.backend_id = "xueqiu-api"
        self.backend_version = "1.5.0+search.v1"
        self.items = items
        self.truncated = truncated
        self.partial_error_code = None


class _Failure:
    def __init__(self, error_code: str) -> None:
        self.protocol_version = "v1"
        self.source = "xueqiu"
        self.operation = "search.stocks"
        self.backend_id = "xueqiu-api"
        self.backend_version = "1.5.0+search.v1"
        self.error_code = error_code


def _api(execute: Callable[[object, object], object]) -> SimpleNamespace:
    return SimpleNamespace(
        execution_request_type=_ExecutionRequest,
        xueqiu_session_type=_Session,
        execution_limits_type=_Limits,
        execution_context_type=_Context,
        execution_item_type=_Item,
        execution_success_type=_Success,
        execution_failure_type=_Failure,
        execute=execute,
    )


def _provider(value: SimpleNamespace) -> worker.ExecutionApiProvider:
    return cast(worker.ExecutionApiProvider, lambda: value)


def _request(
    cookie: str = COOKIE_CANARY,
    *,
    deadline: float | None = None,
) -> worker.WorkerRequest:
    frame = worker.encode_request(
        "600519",
        2,
        cookie,
        deadline=time.monotonic() + 60 if deadline is None else deadline,
    )
    try:
        return worker._read_request(io.BytesIO(frame))
    finally:
        frame[:] = b"\x00" * len(frame)


def _success_value(
    *,
    items: list[dict[str, str]] | None = None,
    truncated: bool = False,
) -> dict[str, object]:
    return {
        "backend": {"id": "xueqiu-api", "version": "1.5.0+search.v1"},
        "items": items
        if items is not None
        else [{"exchange": "SH", "name": "Kweichow Moutai", "symbol": "SH600519"}],
        "operation": "search.stocks",
        "protocol": "v1",
        "schema": "xueqiu.stock.v1",
        "source": "xueqiu",
        "truncated": truncated,
    }


def _framed(value: Mapping[str, object]) -> bytes:
    return worker._encode_frame(value, worker.MAX_OUTPUT_BYTES)


def test_worker_calls_only_the_xueqiu_runtime_and_clears_every_cookie_buffer() -> None:
    request = _request()
    session: _Session | None = None

    def execute(execution_request: object, context: object) -> object:
        nonlocal session
        typed_request = cast(_ExecutionRequest, execution_request)
        typed_context = cast(_Context, context)
        assert typed_request.source == "xueqiu"
        assert typed_request.operation == "search.stocks"
        assert typed_request.arguments == {"query": "600519", "limit": 2}
        assert typed_context.limits.maximum_items == 2
        assert len(typed_context.host_capabilities) == 1
        session = cast(_Session, typed_context.host_capabilities[0])
        typed_context.checkpoint()
        return _Success(
            (
                _Item(
                    {
                        "symbol": "SH600519",
                        "name": "Kweichow Moutai",
                        "exchange": "SH",
                    }
                ),
            )
        )

    value = worker._execute_request(
        request, execution_api_provider=_provider(_api(execute))
    )

    assert _Session.observed == COOKIE_CANARY.encode()
    assert not any(request.cookie_header)
    assert session is not None and not any(session.cookie_header)
    encoded = json.dumps(value, sort_keys=True)
    assert "secret-cookie-canary" not in encoded
    response = worker.decode_response(_framed(value), limit=2)
    assert isinstance(response, worker.XueqiuProjection)
    assert response.items == (
        worker.XueqiuStockProjection("SH600519", "Kweichow Moutai", "SH"),
    )


class _CapturingInput:
    def __init__(self, value: bytes) -> None:
        self._value = value
        self._offset = 0
        self.buffers: list[bytearray] = []

    def readinto(self, buffer: bytearray | memoryview) -> int:
        target = buffer.obj if isinstance(buffer, memoryview) else buffer
        if isinstance(target, bytearray):
            self.buffers.append(target)
        selected = self._value[self._offset : self._offset + len(buffer)]
        buffer[: len(selected)] = selected
        self._offset += len(selected)
        return len(selected)


def test_secret_frame_is_mutable_redacted_and_zeroed_after_read() -> None:
    frame = worker.encode_request(
        "stock query",
        3,
        COOKIE_CANARY,
        deadline=time.monotonic() + 60,
    )
    assert COOKIE_CANARY.encode() in frame
    stream = _CapturingInput(bytes(frame))

    request = worker._read_request(stream)

    assert repr(request) == "WorkerRequest(<redacted>)"
    assert "canary" not in repr(request)
    assert stream.buffers
    assert all(not any(buffer) for buffer in stream.buffers)
    request.close()
    assert not any(request.cookie_header)
    frame[:] = b"\x00" * len(frame)
    assert not any(frame)


def test_secret_frame_decode_avoids_secret_slices_and_immutable_copies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    query = "stock query"
    frame = worker.encode_request(
        query,
        3,
        COOKIE_CANARY,
        deadline=time.monotonic() + 60,
    )
    payload_length = int.from_bytes(frame[:4], "big")
    cookie_start = worker._REQUEST_FIXED_BYTES + len(query.encode())
    cookie_length = len(COOKIE_CANARY.encode())

    def intersects_cookie(key: slice) -> bool:
        return any(
            cookie_start <= index < payload_length
            for index in range(*key.indices(payload_length))
        )

    class RejectSecretSlice(bytearray):
        def __getitem__(self, key: int | slice) -> int | bytearray:
            if (
                isinstance(key, slice)
                and len(self) == payload_length
                and intersects_cookie(key)
            ):
                raise AssertionError("a payload slice intersected the Cookie bytes")
            return super().__getitem__(key)

    real_bytes = bytes

    def reject_immutable_cookie_copy(value: bytearray | memoryview) -> bytes:
        if (
            isinstance(value, memoryview)
            and isinstance(value.obj, RejectSecretSlice)
            and len(value.obj) in {payload_length, cookie_length}
        ):
            raise AssertionError("secret-buffer bytes were materialized as bytes")
        return real_bytes(value)

    probe_payload = RejectSecretSlice(payload_length)
    with (
        memoryview(frame) as frame_view,
        frame_view[4:] as payload_source,
        memoryview(probe_payload) as probe_view,
    ):
        probe_view[:] = payload_source
    for secret_slice in (
        slice(cookie_start, None),
        slice(cookie_start - 1, cookie_start + 1),
        slice(cookie_start, cookie_start + 1),
        slice(payload_length - 1, payload_length),
        slice(payload_length - 1, cookie_start - 1, -1),
        slice(cookie_start, payload_length, 2),
    ):
        with pytest.raises(AssertionError, match="intersected the Cookie"):
            probe_payload[secret_slice]
    with (
        memoryview(probe_payload) as probe_view,
        probe_view[cookie_start:] as cookie_view,
        probe_view[cookie_start : cookie_start + 1] as cookie_byte_view,
    ):
        with pytest.raises(AssertionError, match="materialized as bytes"):
            reject_immutable_cookie_copy(cookie_view)
        with pytest.raises(AssertionError, match="materialized as bytes"):
            reject_immutable_cookie_copy(cookie_byte_view)
    probe_payload[:] = b"\x00" * len(probe_payload)

    monkeypatch.setattr(worker, "bytearray", RejectSecretSlice, raising=False)
    monkeypatch.setattr(worker, "bytes", reject_immutable_cookie_copy, raising=False)
    request: worker.WorkerRequest | None = None
    try:
        request = worker._read_request(io.BytesIO(frame))
        assert request.cookie_header == COOKIE_CANARY.encode()
    finally:
        if request is not None:
            request.close()
        frame[:] = b"\x00" * len(frame)

    assert request is not None
    assert not any(request.cookie_header)
    assert not any(frame)


@pytest.mark.parametrize(
    "cookie",
    [
        "missing_token=value",
        "xq_a_token=one; xq_a_token=two",
        "xq_a_token=with space",
        "xq_a_token=with,comma",
        "xq_a_token=value\r\nInjected: yes",
        "xq_a_token=",
    ],
)
def test_cookie_contract_rejects_malformed_or_ambiguous_headers(cookie: str) -> None:
    with pytest.raises(worker.XueqiuProtocolError):
        worker.encode_request(
            "600519",
            1,
            cookie,
            deadline=time.monotonic() + 60,
        )


@pytest.mark.parametrize(
    "item",
    [
        {"exchange": "SZ", "name": "Mismatch", "symbol": "SH600519"},
        {"exchange": "NASDAQ", "name": "Apple", "symbol": "AAPL"},
        {"exchange": "SH", "name": "", "symbol": "SH600519"},
        {
            "exchange": "SH",
            "name": "Moutai",
            "symbol": "SH600519",
            "cookie": "forbidden",
        },
    ],
)
def test_parent_decoder_rejects_tampered_stock_projection(
    item: dict[str, str],
) -> None:
    with pytest.raises(worker.XueqiuProtocolError):
        worker.decode_response(_framed(_success_value(items=[item])), limit=2)


def test_fork_drift_is_redacted_and_cookie_is_cleared() -> None:
    request = _request()

    def execute(_request: object, _context: object) -> object:
        return _Success(
            (
                _Item(
                    {
                        "symbol": "SH600519",
                        "name": "Moutai",
                        "exchange": "SZ",
                    }
                ),
            )
        )

    value = worker._execute_request(
        request, execution_api_provider=_provider(_api(execute))
    )

    assert value == worker._failure_value("backend_contract_violation")
    assert not any(request.cookie_header)
    assert "canary" not in json.dumps(value)


def test_cancellation_checkpoint_returns_closed_failure_and_clears_cookie() -> None:
    request = _request()
    worker._CANCELLATION_REQUESTED.set()
    try:
        value = worker._execute_request(
            request,
            execution_api_provider=_provider(
                _api(lambda _request, _context: pytest.fail("executed after cancel"))
            ),
        )
    finally:
        worker._CANCELLATION_REQUESTED.clear()

    assert value == worker._failure_value("cancelled")
    assert not any(request.cookie_header)


def test_expired_deadline_returns_closed_failure_and_clears_cookie() -> None:
    request = _request(deadline=time.monotonic() - 1)

    value = worker._execute_request(
        request,
        execution_api_provider=_provider(
            _api(lambda _request, _context: pytest.fail("executed after deadline"))
        ),
    )

    assert value == worker._failure_value("deadline_exceeded")
    assert not any(request.cookie_header)


@pytest.mark.parametrize(
    "cookie",
    (
        "xq_a_token=value",
        "xq_a_token=value; u=1",
        " xq_a_token=value",
        "xq_a_token=value;",
        "xq_a_token=one; xq_a_token=two",
        "u=1",
        "xq_a_token=",
        "=value",
        "xq_a_token=with space",
    ),
)
def test_cookie_validators_agree_on_ascii_input(cookie: str) -> None:
    assert worker._valid_cookie_text(cookie) == worker._valid_cookie_bytes(
        bytearray(cookie.encode("ascii"))
    )


def test_runtime_loader_selects_only_the_xueqiu_module(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[str | None] = []
    marker = cast(worker.AgentReachExecutionApi, object())

    def validate(*, runtime_module: str | None = None) -> worker.AgentReachExecutionApi:
        observed.append(runtime_module)
        return marker

    monkeypatch.setattr(worker, "validate_agent_reach_execution_contract", validate)

    assert worker._load_execution_api() is marker
    assert observed == ["xueqiu"]
