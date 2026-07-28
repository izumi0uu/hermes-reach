from __future__ import annotations

import asyncio
import io
import json
import os
import signal
import subprocess
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

import hermes_reach.sources.youtube as youtube
import hermes_reach.sources.youtube_worker as worker
from hermes_reach.sources.youtube import YouTubeWorker, YouTubeWorkerError

VIDEO_ID = "dQw4w9WgXcQ"
VIDEO_URL = f"https://www.youtube.com/watch?v={VIDEO_ID}"
VERSIONS = {"yt-dlp": "2026.7.4", "yt-dlp-ejs": "0.8.0", "deno": "2.8.3"}


def _backend_video() -> dict[str, object]:
    return {
        "id": VIDEO_ID,
        "title": " Video   title ",
        "description": "Description",
        "uploader": "Channel",
        "duration": 213,
        "view_count": 42,
        "comment_count": 7,
        "upload_date": "20091025",
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


def _executable(tmp_path: Path) -> str:
    scripts = tmp_path / "bin"
    scripts.mkdir()
    executable = scripts / "python"
    executable.write_bytes(b"")
    deno = scripts / "deno"
    deno.write_bytes(b"deno")
    deno.chmod(0o700)
    return str(executable)


class FakeDownloader:
    instances: list[FakeDownloader] = []
    response: object = _backend_video()

    def __init__(self, params: dict[str, object]) -> None:
        self.params = params
        self.calls: list[tuple[str, bool, str]] = []
        self.closed = False
        self.instances.append(self)

    def extract_info(self, target: str, *, download: bool, ie_key: str) -> object:
        self.calls.append((target, download, ie_key))
        return self.response

    def close(self) -> None:
        self.closed = True


@pytest.fixture(autouse=True)
def _reset_fake_downloader() -> Iterator[None]:
    FakeDownloader.instances.clear()
    FakeDownloader.response = _backend_video()
    yield
    FakeDownloader.instances.clear()
    FakeDownloader.response = _backend_video()


def _modules(plugin_dirs: SimpleNamespace) -> object:
    def load(name: str) -> object:
        if name == "yt_dlp":
            return SimpleNamespace(YoutubeDL=FakeDownloader)
        if name == "yt_dlp_ejs":
            return SimpleNamespace(version="0.8.0")
        if name == "yt_dlp.globals":
            return SimpleNamespace(plugin_dirs=plugin_dirs)
        raise AssertionError(name)

    return load


def _version(name: str) -> str:
    return VERSIONS[name]


@pytest.mark.parametrize(
    ("worker_request", "response", "expected_call"),
    [
        (
            worker.WorkerRequest("search.videos", query="private query", limit=2),
            {"entries": [_backend_video()]},
            ("ytsearch2:private query", False, "YoutubeSearch"),
        ),
        (
            worker.WorkerRequest("read.video", url=VIDEO_URL),
            _backend_video(),
            (VIDEO_URL, False, "Youtube"),
        ),
    ],
)
def test_worker_uses_exact_structured_routes_and_closed_options(
    tmp_path: Path,
    worker_request: worker.WorkerRequest,
    response: object,
    expected_call: tuple[str, bool, str],
) -> None:
    FakeDownloader.response = response
    plugin_dirs = SimpleNamespace(value=["ambient"])

    result = worker._invoke_backend(
        worker_request,
        module_loader=cast(Callable[[str], object], _modules(plugin_dirs)),
        version_reader=_version,
        executable=_executable(tmp_path),
        root=tmp_path,
    )

    instance = FakeDownloader.instances[0]
    assert instance.calls == [expected_call]
    assert instance.closed is True
    assert result == (
        [_projected_video()]
        if worker_request.operation == "search.videos"
        else _projected_video()
    )
    assert plugin_dirs.value == []
    assert instance.params["cachedir"] is False
    assert instance.params["proxy"] == ""
    assert instance.params["cookiefile"] is None
    assert instance.params["cookiesfrombrowser"] is None
    assert instance.params["usenetrc"] is False
    assert instance.params["netrc_cmd"] is None
    assert instance.params["remote_components"] == []
    assert instance.params["postprocessors"] == []
    assert instance.params["js_runtimes"] == {
        "deno": {"path": str(tmp_path / "bin" / "deno")}
    }


def test_subtitle_route_prefers_manual_vtt_and_removes_the_file(
    tmp_path: Path,
) -> None:
    subtitle = tmp_path / f"{VIDEO_ID}.en.vtt"
    subtitle.write_text("WEBVTT\n\n00:00.000 --> 00:01.000\nHello", encoding="utf-8")
    FakeDownloader.response = {
        **_backend_video(),
        "requested_subtitles": {"en": {"ext": "vtt", "filepath": str(subtitle)}},
        "subtitles": {"en": [{"ext": "vtt", "url": "private"}]},
        "automatic_captions": {"en": [{"ext": "vtt", "url": "private"}]},
    }
    plugin_dirs = SimpleNamespace(value=["ambient"])

    result = worker._invoke_backend(
        worker.WorkerRequest("read.subtitles", url=VIDEO_URL, language="en"),
        module_loader=cast(Callable[[str], object], _modules(plugin_dirs)),
        version_reader=_version,
        executable=_executable(tmp_path),
        root=tmp_path,
    )

    instance = FakeDownloader.instances[0]
    assert instance.calls == [(VIDEO_URL, True, "Youtube")]
    assert instance.params["writesubtitles"] is True
    assert instance.params["writeautomaticsub"] is True
    assert instance.params["subtitleslangs"] == ["en"]
    assert instance.params["subtitlesformat"] == "vtt"
    assert instance.params["skip_download"] is True
    assert instance.params["max_filesize"] == worker.MAX_SUBTITLE_FILE_BYTES
    assert result == {
        "id": VIDEO_ID,
        "title": "Video title",
        "language": "en",
        "origin": "manual",
        "text": "WEBVTT\n\n00:00.000 --> 00:01.000\nHello",
        "truncated": False,
        "url": VIDEO_URL,
    }
    assert subtitle.exists() is False


def test_subtitle_route_uses_default_order_automatic_origin_and_truncation(
    tmp_path: Path,
) -> None:
    subtitle = tmp_path / f"{VIDEO_ID}.zh-Hans.vtt"
    subtitle.write_text(
        "WEBVTT\n\n" + ("line\n" * (worker.MAX_SUBTITLE_TEXT_BYTES // 5)),
        encoding="utf-8",
    )
    FakeDownloader.response = {
        **_backend_video(),
        "requested_subtitles": {"zh-Hans": {"ext": "vtt", "filepath": str(subtitle)}},
        "subtitles": {},
        "automatic_captions": {"zh-Hans": [{"ext": "vtt"}]},
    }
    plugin_dirs = SimpleNamespace(value=[])

    result = cast(
        dict[str, object],
        worker._invoke_backend(
            worker.WorkerRequest("read.subtitles", url=VIDEO_URL),
            module_loader=cast(Callable[[str], object], _modules(plugin_dirs)),
            version_reader=_version,
            executable=_executable(tmp_path),
            root=tmp_path,
        ),
    )

    instance = FakeDownloader.instances[0]
    assert instance.params["subtitleslangs"] == ["zh-Hans", "zh", "en"]
    assert result["language"] == "zh-Hans"
    assert result["origin"] == "automatic"
    assert result["truncated"] is True
    assert len(cast(str, result["text"]).encode("utf-8")) <= (
        worker.MAX_SUBTITLE_TEXT_BYTES
    )


def test_subtitle_projection_rejects_paths_outside_private_root(
    tmp_path: Path,
) -> None:
    root = tmp_path / "private"
    root.mkdir()
    outside = tmp_path / "outside.vtt"
    outside.write_text("WEBVTT\n", encoding="utf-8")
    data = {
        **_backend_video(),
        "requested_subtitles": {"en": {"ext": "vtt", "filepath": str(outside)}},
        "subtitles": {"en": [{"ext": "vtt"}]},
        "automatic_captions": {},
    }

    with pytest.raises(worker.YouTubeProtocolError):
        worker._project_subtitles(
            data,
            root,
            "en",
            expected_id=VIDEO_ID,
        )

    assert outside.exists() is True


def test_subtitle_projection_accepts_resolved_private_root_prefix(
    tmp_path: Path,
) -> None:
    root = tmp_path / "private"
    root.mkdir()
    alias = tmp_path / "private-alias"
    alias.symlink_to(root, target_is_directory=True)
    subtitle = root / f"{VIDEO_ID}.en.vtt"
    subtitle.write_text("WEBVTT\n\n00:00.000 --> 00:01.000\nHello", encoding="utf-8")

    text, truncated = worker._read_subtitle_file(
        alias / subtitle.name,
        alias,
    )

    assert text.endswith("Hello")
    assert truncated is False
    assert subtitle.exists() is False


def test_subtitle_projection_rejects_final_symlink(tmp_path: Path) -> None:
    root = tmp_path / "private"
    root.mkdir()
    target = root / "target.vtt"
    target.write_text("WEBVTT\n", encoding="utf-8")
    subtitle = root / f"{VIDEO_ID}.en.vtt"
    subtitle.symlink_to(target)

    with pytest.raises(worker.YouTubeProtocolError):
        worker._read_subtitle_file(subtitle, root)

    assert subtitle.is_symlink()
    assert target.exists()


def test_no_subtitles_and_dependency_drift_return_closed_errors(tmp_path: Path) -> None:
    FakeDownloader.response = {**_backend_video(), "requested_subtitles": None}
    plugin_dirs = SimpleNamespace(value=[])
    missing = worker._execute_request(
        worker.WorkerRequest("read.subtitles", url=VIDEO_URL),
        cast(Callable[[str], object], _modules(plugin_dirs)),
        _version,
        _executable(tmp_path),
        tmp_path,
    )
    drifted = worker._execute_request(
        worker.WorkerRequest("read.video", url=VIDEO_URL),
        cast(Callable[[str], object], _modules(plugin_dirs)),
        lambda name: "0" if name == "yt-dlp-ejs" else VERSIONS[name],
        str(tmp_path / "bin" / "python"),
        tmp_path,
    )

    assert missing["error"] == {"code": "not_found"}
    assert drifted["error"] == {"code": "setup_required"}


def test_backend_projection_rejects_identity_date_and_progress_drift() -> None:
    with pytest.raises(worker.YouTubeProtocolError):
        worker._project_video(
            {**_backend_video(), "id": "aaaaaaaaaaa"},
            expected_id=VIDEO_ID,
        )
    with pytest.raises(worker.YouTubeProtocolError):
        worker._project_video(
            {**_backend_video(), "upload_date": "20260231"},
            expected_id=VIDEO_ID,
        )
    with pytest.raises(worker.YouTubeProtocolError):
        worker._bounded_progress(
            {"downloaded_bytes": worker.MAX_SUBTITLE_FILE_BYTES + 1}
        )


def test_backend_projection_normalizes_integral_float_duration() -> None:
    assert (
        worker._project_video(
            {**_backend_video(), "duration": 300.0},
            expected_id=VIDEO_ID,
        )["duration_seconds"]
        == 300
    )

    with pytest.raises(worker.YouTubeProtocolError):
        worker._project_video(
            {**_backend_video(), "duration": 300.5},
            expected_id=VIDEO_ID,
        )


def test_success_projection_failure_returns_closed_permanent_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        worker,
        "_invoke_backend",
        lambda *_args, **_kwargs: {"unexpected": True},
    )

    response = worker._execute_request(
        worker.WorkerRequest("read.video", url=VIDEO_URL)
    )

    assert response == {
        "protocol_version": "v1",
        "operation": "read.video",
        "ok": False,
        "error": {"code": "permanent"},
    }


@pytest.mark.skipif(os.name == "nt", reason="POSIX executable shape checks")
@pytest.mark.parametrize(
    "mode", ["missing", "directory", "symlink", "hardlink", "not_executable"]
)
def test_deno_gate_rejects_unexpected_executable_shapes(
    tmp_path: Path,
    mode: str,
) -> None:
    scripts = tmp_path / "bin"
    scripts.mkdir()
    python = scripts / "python"
    python.write_bytes(b"")
    deno = scripts / "deno"
    if mode == "directory":
        deno.mkdir()
    elif mode == "symlink":
        target = tmp_path / "real-deno"
        target.write_bytes(b"deno")
        target.chmod(0o700)
        deno.symlink_to(target)
    elif mode == "hardlink":
        target = tmp_path / "real-deno"
        target.write_bytes(b"deno")
        target.chmod(0o700)
        os.link(target, deno)
    elif mode == "not_executable":
        deno.write_bytes(b"deno")
        deno.chmod(0o600)

    with pytest.raises(worker.YouTubeSetupError):
        worker._deno_executable(str(python))


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
