from __future__ import annotations

import asyncio
import builtins
import io
import json
import signal
import socket
import subprocess
import sys
from typing import cast

import feedparser.http
import pytest

import hermes_reach.sources.rss as rss
import hermes_reach.sources.rss_worker as rss_worker
from hermes_reach.sources.rss import FeedparserWorker, FeedparserWorkerError
from hermes_reach.sources.rss_worker import (
    MAX_OUTPUT_BYTES,
    FeedparserProjection,
    decode_response,
    encode_request,
)

FEED_URL = "https://example.com/feed.xml"
ATOM = b"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Example feed</title><subtitle>Feed body</subtitle>
  <entry><id>entry-1</id><title>Entry title</title>
    <content type="html">&lt;p&gt;Preferred body&lt;/p&gt;</content>
    <summary>Fallback body</summary><author><name>Alice</name></author>
    <link href="/entry?tracking=yes" />
    <updated>2026-07-27T01:02:03Z</updated>
  </entry>
</feed>"""


def _closed_output() -> bytes:
    return json.dumps(
        {
            "bozo": False,
            "entries": [],
            "feed": None,
            "version": 1,
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode()


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
        self.pid = 7890
        self.returncode: int | None = None
        self._wait_returncode = returncode
        self.stdin = _Writer()
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


def test_feedparser_receives_an_in_memory_byte_stream_without_fetch_or_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    framed = encode_request(
        ATOM,
        content_type="application/atom+xml",
        content_location=FEED_URL,
        max_entries=2,
    )
    request = rss_worker._read_request(io.BytesIO(framed))

    def unexpected_io(*_: object, **__: object) -> object:
        raise AssertionError("feedparser attempted external I/O")

    monkeypatch.setattr(builtins, "open", unexpected_io)
    monkeypatch.setattr(feedparser.http, "get", unexpected_io)
    monkeypatch.setattr(socket, "create_connection", unexpected_io)

    result = rss_worker._parse_feed(request)

    assert result.entries[0].text == "<p>Preferred body</p>"
    assert result.entries[0].url == "https://example.com/entry?tracking=yes"


def test_real_isolated_worker_parses_closed_projection() -> None:
    result = asyncio.run(
        FeedparserWorker().parse(
            ATOM,
            content_type="application/atom+xml",
            content_location=FEED_URL,
            max_entries=2,
        )
    )

    assert isinstance(result, FeedparserProjection)
    assert result.bozo is False
    assert result.feed is not None
    assert result.feed.text == "Feed body"
    assert [entry.text for entry in result.entries] == ["<p>Preferred body</p>"]


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
    process = _Process(_closed_output())
    captured: dict[str, object] = {}

    async def create(*args: str, **kwargs: object) -> _Process:
        captured["args"] = args
        captured["kwargs"] = kwargs
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)

    result = asyncio.run(
        FeedparserWorker().parse(
            ATOM,
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
            await rss._kill_process_group(process)
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
        output = _closed_output()
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
                content_type="application/atom+xml",
                content_location=FEED_URL,
                max_entries=2,
            )
        )

    assert failed.value.failure_class == "permanent"
    assert "OUTPUT_CANARY" not in str(failed.value)
    expected_kill = [(process.pid, signal.SIGKILL)] if terminal == "oversize" else []
    assert killed == expected_kill


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"{}",
        b'{"bozo":false,"entries":[],"feed":null,"version":true}',
        b'{"bozo":false,"bozo":true,"entries":[],"feed":null,"version":1}',
        b'{"bozo":false,"entries":[],"extra":null,"feed":null,"version":1}',
        b"X" * (MAX_OUTPUT_BYTES + 1),
    ],
)
def test_response_decoder_rejects_open_or_oversize_protocol(raw: bytes) -> None:
    with pytest.raises(rss_worker.FeedparserProtocolError):
        decode_response(raw)


def test_request_encoder_rejects_fetchable_strings_and_unsafe_metadata() -> None:
    with pytest.raises(rss_worker.FeedparserProtocolError):
        encode_request(
            cast(bytes, FEED_URL),
            content_type="application/rss+xml",
            content_location=FEED_URL,
            max_entries=2,
        )
    with pytest.raises(rss_worker.FeedparserProtocolError):
        encode_request(
            ATOM,
            content_type="application/rss+xml\nproxy: enabled",
            content_location=FEED_URL,
            max_entries=2,
        )
    with pytest.raises(rss_worker.FeedparserProtocolError):
        encode_request(
            ATOM,
            content_type="application/rss+xml",
            content_location="file:///tmp/private-feed",
            max_entries=2,
        )
