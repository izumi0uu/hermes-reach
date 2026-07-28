from __future__ import annotations

import asyncio
import io
import json
import signal
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import bili_cli.client
import pytest

import hermes_reach.sources.bilibili as bilibili
import hermes_reach.sources.bilibili_worker as worker
from hermes_reach.sources.bilibili import BilibiliWorker, BilibiliWorkerError


def _success_envelope(data: object) -> dict[str, object]:
    return {"ok": True, "schema_version": "1", "data": data}


def _framed_response(data: object) -> bytes:
    return worker._encode_frame(_success_envelope(data), worker.MAX_OUTPUT_BYTES)


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
    ("worker_request", "expected"),
    [
        (
            worker.WorkerRequest("search.videos", query="-danger", limit=3),
            (
                "search",
                "--type",
                "video",
                "--max",
                "3",
                "--json",
                "--",
                "-danger",
            ),
        ),
        (
            worker.WorkerRequest(
                "read.video", url="https://www.bilibili.com/video/BV1xx411c7mD"
            ),
            (
                "video",
                "https://www.bilibili.com/video/BV1xx411c7mD",
                "--json",
            ),
        ),
        (worker.WorkerRequest("browse.hot", limit=4), ("hot", "--max", "4", "--json")),
        (
            worker.WorkerRequest("browse.rank", limit=5),
            ("rank", "--max", "5", "--json"),
        ),
    ],
)
def test_worker_argv_is_exhaustive_and_keeps_input_positional(
    worker_request: worker.WorkerRequest, expected: tuple[str, ...]
) -> None:
    assert worker._argv(worker_request) == expected


def test_worker_invokes_exact_click_entry_point_and_accepts_structured_error() -> None:
    calls: list[dict[str, object]] = []

    class FakeCli:
        def main(self, **kwargs: object) -> None:
            calls.append(kwargs)
            print(
                json.dumps(
                    {
                        "ok": False,
                        "schema_version": "1",
                        "error": {"code": "not_found", "message": "private"},
                    }
                )
            )
            raise SystemExit(1)

    result = worker._invoke_cli(
        worker.WorkerRequest("search.videos", query="-danger", limit=2),
        lambda _: SimpleNamespace(cli=FakeCli()),
        lambda _: "0.6.2",
    )

    assert result["ok"] is False
    assert calls == [
        {
            "args": [
                "search",
                "--type",
                "video",
                "--max",
                "2",
                "--json",
                "--",
                "-danger",
            ],
            "prog_name": "bili",
            "standalone_mode": False,
        }
    ]


def test_real_click_parser_keeps_leading_hyphen_query_positional(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, int]] = []

    async def search_video(keyword: str, page: int = 1) -> list[dict[str, object]]:
        calls.append((keyword, page))
        return [
            {
                "bvid": "BV1xx411c7mD",
                "title": "Result",
                "author": "Author",
                "play": 1,
                "duration": "00:10",
            }
        ]

    monkeypatch.setattr(bili_cli.client, "search_video", search_video)

    result = worker._invoke_cli(
        worker.WorkerRequest("search.videos", query="-danger", limit=1)
    )

    assert calls == [("-danger", 1)]
    assert result["ok"] is True
    assert cast(list[dict[str, object]], result["data"])[0]["bvid"] == ("BV1xx411c7mD")


@pytest.mark.parametrize("failure", ["wrong_exit", "bad_json", "overflow"])
def test_worker_fails_closed_on_click_or_output_drift(failure: str) -> None:
    class FakeCli:
        def main(self, **_: object) -> None:
            if failure == "wrong_exit":
                raise SystemExit(2)
            if failure == "bad_json":
                print("not-json")
                return
            print("x" * (worker.MAX_OUTPUT_BYTES + 1))

    with pytest.raises(worker.BilibiliProtocolError):
        worker._invoke_cli(
            worker.WorkerRequest("browse.hot", limit=1),
            lambda _: SimpleNamespace(cli=FakeCli()),
            lambda _: "0.6.2",
        )


def test_worker_rejects_unknown_fields_trailing_data_and_version_drift() -> None:
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
    with pytest.raises(worker.BilibiliProtocolError):
        worker._invoke_cli(
            worker.WorkerRequest("browse.hot", limit=1),
            lambda _: SimpleNamespace(cli=object()),
            lambda _: "0.6.1",
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
    ],
)
def test_read_video_rejects_non_canonical_urls(url: str) -> None:
    with pytest.raises(worker.BilibiliProtocolError):
        worker.encode_request("read.video", url=url)


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
    process = _Process(_framed_response([]))
    captured: dict[str, object] = {}

    async def create(*args: str, **kwargs: object) -> _Process:
        captured["args"] = args
        captured["kwargs"] = kwargs
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)

    result = asyncio.run(
        BilibiliWorker().execute("search.videos", query="secret", limit=2)
    )

    assert result["data"] == []
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
