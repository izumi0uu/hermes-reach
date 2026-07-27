from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import signal
from collections.abc import Callable
from pathlib import Path

import pytest

from hermes_reach.bootstrap import DEFAULT_RUNTIME
from hermes_reach.connector.authority import AuthorizedExecution
from hermes_reach.connector.errors import ConnectorError, ConnectorErrorCode
from hermes_reach.connector.identity import DevicePrivateIdentity
from hermes_reach.connector.protocol import (
    GrantScope,
    create_signed_request,
    protect_operation_call,
)
from hermes_reach.connector.store import ClaimResult
from hermes_reach.contracts import (
    OperationCall,
    ReachValidationError,
    reddit_post_id_from_url,
    validate_read,
)
from hermes_reach.sources.reddit import (
    OpenCliRedditReadExecutor,
    OpenCliSubprocess,
    attest_opencli_executable,
    build_reddit_opencli_execution_composition,
)

NOW = 1_800_000_000
POST_ID = "abc123"
POST_URL = f"https://www.reddit.com/r/python/comments/{POST_ID}/fixture_post"
EXPECTED_ARGV = (
    "reddit",
    "read",
    POST_ID,
    "--sort",
    "best",
    "--limit",
    "3",
    "--depth",
    "2",
    "--replies",
    "2",
    "--max-length",
    "800",
    "-f",
    "yaml",
)
FIXTURE_OUTPUT = """\
- type: POST
  author: alice
  score: 42
  text: |-
    Fixture title
    Fixture post body
  post_hint: self
  url_overridden_by_dest: ""
  preview_image_url: ""
  gallery_urls: []
- type: L0
  author: bob
  score: 7
  text: First reply
  post_hint: ""
  url_overridden_by_dest: ""
  preview_image_url: ""
  gallery_urls: []
- type: ""
  author: ""
  score: ""
  text: Continued reply text
  post_hint: ""
  url_overridden_by_dest: ""
  preview_image_url: ""
  gallery_urls: []
"""


def _id(value: int) -> str:
    return base64.b32encode(value.to_bytes(16, "big")).decode().rstrip("=").lower()


def _reddit_call(url: str = POST_URL) -> OperationCall:
    return validate_read(
        {
            "source": "reddit",
            "operation": "read.post",
            "target": {"url": url},
        }
    )


def _execution(
    call: OperationCall | None = None, *, deadline: int = NOW + 30
) -> AuthorizedExecution:
    selected = call or _reddit_call()
    connector = DevicePrivateIdentity._from_seed_for_testing(bytes([40]) * 32)
    vps = DevicePrivateIdentity._from_seed_for_testing(bytes([41]) * 32)
    protected = protect_operation_call(selected)
    request = create_signed_request(
        vps,
        message_id=_id(1),
        request_id=_id(2),
        trace_id="a" * 32,
        audience_key_id=connector.public_identity.key_id,
        grant_id=_id(3),
        grant_revision=1,
        policy_revision=1,
        source=selected.source.name,
        operation=selected.operation.name,
        issued_at=NOW,
        deadline=deadline,
        protected_payload=protected,
    )
    scope = GrantScope(
        selected.source.name,
        selected.operation.name,
        selected.operation.runtime.data_scope,
    )
    return AuthorizedExecution(
        request,
        protected,
        scope,
        ClaimResult(True, None, 1, 4, "b" * 64),
    )


class _FixtureProcess:
    def __init__(self, output: str = FIXTURE_OUTPUT) -> None:
        self.output = output
        self.calls: list[tuple[tuple[str, ...], float]] = []

    async def run(self, argv: tuple[str, ...], *, deadline: float) -> str:
        self.calls.append((argv, deadline))
        return self.output


class _FailingFixtureProcess(_FixtureProcess):
    async def run(self, argv: tuple[str, ...], *, deadline: float) -> str:
        self.calls.append((argv, deadline))
        raise RuntimeError("PROVIDER_OUTPUT_CANARY")


def _assert_code(
    caught: pytest.ExceptionInfo[ConnectorError], code: ConnectorErrorCode
) -> None:
    assert caught.value.code == code.value


@pytest.mark.parametrize(
    ("url", "post_id"),
    [
        ("https://reddit.com/r/python/comments/ABC123", "abc123"),
        (POST_URL, POST_ID),
        (
            "https://old.reddit.com/r/a_b/comments/z9/slug_with-hyphen/",
            "z9",
        ),
    ],
)
def test_reddit_post_contract_accepts_only_canonical_https_urls(
    url: str, post_id: str
) -> None:
    call = _reddit_call(url)

    assert call.target == {"url": url}
    assert call.options == {}
    assert reddit_post_id_from_url(url) == post_id


@pytest.mark.parametrize(
    "url",
    [
        "abc123",
        "http://www.reddit.com/r/python/comments/abc123/post",
        "https://example.com/r/python/comments/abc123/post",
        "https://reddit.com.example/r/python/comments/abc123/post",
        "https://user@reddit.com/r/python/comments/abc123/post",
        "https://reddit.com:443/r/python/comments/abc123/post",
        "https://reddit.com/r/python/comments/abc123/post?sort=new",
        "https://reddit.com/r/python/comments/abc123/post#comments",
        "https://reddit.com/r/python/comments/abc123/post/extra",
        "https://reddit.com/r/python//comments/abc123/post",
        "https://reddit.com/r/python/comments//abc123/post",
        "https://reddit.com/r/python/comments/abc123//post",
        "https://reddit.com/u/alice/comments/abc123/post",
        "https://reddit.com/r/python/comment/abc123/post",
    ],
)
def test_reddit_post_contract_rejects_near_miss_urls(url: str) -> None:
    with pytest.raises(ReachValidationError):
        _reddit_call(url)
    assert reddit_post_id_from_url(url) is None


def test_reddit_post_contract_rejects_native_ids_and_other_operations() -> None:
    with pytest.raises(ReachValidationError):
        validate_read(
            {
                "source": "reddit",
                "operation": "read.post",
                "target": {"native_id": POST_ID},
            }
        )

    subreddit = validate_read(
        {
            "source": "reddit",
            "operation": "read.subreddit",
            "target": {"native_id": "python"},
        }
    )
    assert subreddit.operation.implementation_state == "planned"


def test_executor_derives_one_post_id_and_maps_closed_yaml_rows() -> None:
    process = _FixtureProcess()
    executor = OpenCliRedditReadExecutor(
        process,
        wall_clock=lambda: float(NOW),
        monotonic_clock=lambda: 100.0,
    )

    result = asyncio.run(executor.execute(_execution(), {}))

    assert process.calls == [(EXPECTED_ARGV, 130.0)]
    assert result.truncated is False
    assert [(item.kind, item.text, item.author) for item in result.items] == [
        ("content", "Fixture title Fixture post body", "alice"),
        ("reply", "First reply", "bob"),
        ("reply", "Continued reply text", None),
    ]
    assert result.items[0].native_id == POST_ID
    assert result.items[0].title == "Fixture title"
    assert result.items[0].url == f"https://www.reddit.com/comments/{POST_ID}"
    assert all(item.media is None for item in result.items)


def test_executor_rejects_secret_environment_wrong_operation_and_expired_call() -> None:
    process = _FixtureProcess()
    executor = OpenCliRedditReadExecutor(
        process,
        wall_clock=lambda: float(NOW),
        monotonic_clock=lambda: 100.0,
    )
    web_call = validate_read(
        {
            "source": "web",
            "operation": "read.url",
            "target": {"url": "https://example.com"},
        }
    )

    with pytest.raises(ConnectorError) as secret:
        asyncio.run(executor.execute(_execution(), {"SESSION": "private"}))
    _assert_code(secret, ConnectorErrorCode.CONNECTOR_STATE_INVALID)
    with pytest.raises(ConnectorError) as operation:
        asyncio.run(executor.execute(_execution(web_call), {}))
    _assert_code(operation, ConnectorErrorCode.CONNECTOR_STATE_INVALID)
    expired_executor = OpenCliRedditReadExecutor(
        process,
        wall_clock=lambda: float(NOW + 1),
        monotonic_clock=lambda: 100.0,
    )
    with pytest.raises(ConnectorError) as deadline:
        asyncio.run(expired_executor.execute(_execution(deadline=NOW + 1), {}))
    _assert_code(deadline, ConnectorErrorCode.CONNECTOR_DEADLINE_EXCEEDED)
    assert process.calls == []


@pytest.mark.parametrize(
    "output",
    [
        "PROVIDER_OUTPUT_CANARY: [",
        "[]",
        FIXTURE_OUTPUT.replace("type: POST", "type: L0", 1),
        FIXTURE_OUTPUT.replace("type: L0", "type: POST", 1),
        FIXTURE_OUTPUT.replace("type: L0", "type: Lone", 1),
        FIXTURE_OUTPUT.replace("  gallery_urls: []\n", "", 1),
        FIXTURE_OUTPUT + "  unknown_field: rejected\n",
        "!!python/object/apply:os.system ['PROVIDER_OUTPUT_CANARY']",
    ],
)
def test_executor_rejects_yaml_drift_without_exposing_output(output: str) -> None:
    executor = OpenCliRedditReadExecutor(
        _FixtureProcess(output),
        wall_clock=lambda: float(NOW),
        monotonic_clock=lambda: 100.0,
    )

    with pytest.raises(ConnectorError) as rejected:
        asyncio.run(executor.execute(_execution(), {}))

    _assert_code(rejected, ConnectorErrorCode.CONNECTOR_STATE_INVALID)
    assert "PROVIDER_OUTPUT_CANARY" not in str(rejected.value)


def test_executor_rejects_oversized_yaml_before_parsing() -> None:
    executor = OpenCliRedditReadExecutor(
        _FixtureProcess("PROVIDER_OUTPUT_CANARY" * 4_000),
        wall_clock=lambda: float(NOW),
        monotonic_clock=lambda: 100.0,
    )

    with pytest.raises(ConnectorError) as rejected:
        asyncio.run(executor.execute(_execution(), {}))

    _assert_code(rejected, ConnectorErrorCode.CONNECTOR_STATE_INVALID)
    assert "PROVIDER_OUTPUT_CANARY" not in str(rejected.value)


def test_executor_redacts_unexpected_process_boundary_failure() -> None:
    process = _FailingFixtureProcess()
    executor = OpenCliRedditReadExecutor(
        process,
        wall_clock=lambda: float(NOW),
        monotonic_clock=lambda: 100.0,
    )

    with pytest.raises(ConnectorError) as rejected:
        asyncio.run(executor.execute(_execution(), {}))

    _assert_code(rejected, ConnectorErrorCode.CONNECTOR_STATE_INVALID)
    assert "PROVIDER_OUTPUT_CANARY" not in str(rejected.value)
    assert len(process.calls) == 1


def test_executor_result_stays_within_operation_character_budget() -> None:
    post = FIXTURE_OUTPUT.split("- type: L0", maxsplit=1)[0]
    reply = """\
- type: L0
  author: {}
  score: 1
  text: {}
  post_hint: ""
  url_overridden_by_dest: ""
  preview_image_url: ""
  gallery_urls: []
""".format("a" * 80, "x" * 1_500)
    executor = OpenCliRedditReadExecutor(
        _FixtureProcess(post + reply * 13),
        wall_clock=lambda: float(NOW),
        monotonic_clock=lambda: 100.0,
    )

    result = asyncio.run(executor.execute(_execution(), {}))

    assert len(result.items) == 14
    assert result.character_count() <= 16_000
    assert all(len(item.text) <= 1_000 for item in result.items)
    assert all(item.author is None or len(item.author) <= 64 for item in result.items)


class _BytesReader:
    def __init__(self, output: bytes) -> None:
        self._output = output

    async def read(self, maximum: int) -> bytes:
        chunk = self._output[:maximum]
        self._output = self._output[maximum:]
        return chunk


class _NeverReader:
    def __init__(self) -> None:
        self.entered = asyncio.Event()

    async def read(self, _: int) -> bytes:
        self.entered.set()
        await asyncio.Event().wait()
        return b""


class _SubprocessFixture:
    def __init__(
        self,
        output: bytes = FIXTURE_OUTPUT.encode(),
        *,
        exit_code: int = 0,
        reader: _BytesReader | _NeverReader | None = None,
    ) -> None:
        self.stdout = reader or _BytesReader(output)
        self.returncode: int | None = None
        self.pid = 8765
        self._exit_code = exit_code
        self.direct_kills = 0

    async def wait(self) -> int:
        self.returncode = self._exit_code
        return self._exit_code

    def kill(self) -> None:
        self.direct_kills += 1


def _subprocess_runner(
    *, clock: Callable[[], float] = lambda: 10.0
) -> OpenCliSubprocess:
    return OpenCliSubprocess(
        Path("/usr/local/bin/opencli"),
        environment={
            "HOME": "/tmp/opencli-home",
            "PATH": "/usr/local/bin:/usr/bin",
            "LANG": "C.UTF-8",
            "TMPDIR": "/tmp/opencli",
            "TZ": "UTC",
            "AWS_SECRET_ACCESS_KEY": "AMBIENT_CANARY",
            "HTTPS_PROXY": "http://proxy-canary",
        },
        clock=clock,
    )


def _opencli_executable(tmp_path: Path, content: bytes = b"fixture-opencli") -> Path:
    executable = tmp_path / "opencli"
    executable.write_bytes(content)
    executable.chmod(0o700)
    return executable


def test_production_composition_attests_canonical_opencli_executable(
    tmp_path: Path,
) -> None:
    executable = _opencli_executable(tmp_path)
    selected = tmp_path / "selected-opencli"
    selected.symlink_to(executable)

    attestation = attest_opencli_executable(selected)
    composition = build_reddit_opencli_execution_composition(
        selected,
        environment={"HOME": str(tmp_path), "PATH": "/usr/bin"},
    )

    assert attestation.canonical_path == executable.resolve()
    assert attestation.sha256 == hashlib.sha256(b"fixture-opencli").hexdigest()
    assert composition.required_scope("reddit", "read.post") == GrantScope(
        "reddit", "read.post", "public"
    )
    assert repr(composition) == "ConnectorExecutionComposition(count=1)"


@pytest.mark.parametrize("unsafe", ["relative", "missing", "broken", "directory"])
def test_opencli_attestation_rejects_unresolved_or_non_regular_paths(
    tmp_path: Path, unsafe: str
) -> None:
    if unsafe == "relative":
        candidate = Path("opencli")
    elif unsafe == "missing":
        candidate = tmp_path / "missing"
    elif unsafe == "broken":
        candidate = tmp_path / "broken"
        candidate.symlink_to(tmp_path / "missing-target")
    else:
        candidate = tmp_path / "directory"
        candidate.mkdir()

    with pytest.raises(ConnectorError) as rejected:
        attest_opencli_executable(candidate)

    _assert_code(rejected, ConnectorErrorCode.CONNECTOR_STATE_INVALID)


def test_opencli_attestation_rejects_terminal_control_characters(
    tmp_path: Path,
) -> None:
    executable = tmp_path / "opencli\nforged-scope"
    executable.write_bytes(b"fixture-opencli")
    executable.chmod(0o700)

    with pytest.raises(ConnectorError) as rejected:
        attest_opencli_executable(executable)

    _assert_code(rejected, ConnectorErrorCode.CONNECTOR_STATE_INVALID)


@pytest.mark.parametrize("mode", [0o600, 0o001, 0o720, 0o702])
def test_opencli_attestation_rejects_non_executable_or_writable_modes(
    tmp_path: Path, mode: int
) -> None:
    executable = _opencli_executable(tmp_path)
    executable.chmod(mode)

    with pytest.raises(ConnectorError) as rejected:
        attest_opencli_executable(executable)

    _assert_code(rejected, ConnectorErrorCode.CONNECTOR_STATE_INVALID)


def test_opencli_attestation_rejects_empty_and_multiply_linked_files(
    tmp_path: Path,
) -> None:
    empty = _opencli_executable(tmp_path, b"")
    with pytest.raises(ConnectorError):
        attest_opencli_executable(empty)

    empty.unlink()
    executable = _opencli_executable(tmp_path)
    os.link(executable, tmp_path / "second-link")
    with pytest.raises(ConnectorError) as linked:
        attest_opencli_executable(executable)
    _assert_code(linked, ConnectorErrorCode.CONNECTOR_STATE_INVALID)


def test_attested_subprocess_revalidates_digest_before_zero_spawns(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    executable = _opencli_executable(tmp_path)
    attestation = attest_opencli_executable(executable)
    executable.write_bytes(b"drifted-opencli")
    executable.chmod(0o700)
    spawns = 0

    async def create(*_: str, **__: object) -> _SubprocessFixture:
        nonlocal spawns
        spawns += 1
        return _SubprocessFixture()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    runner = OpenCliSubprocess(
        attestation.canonical_path,
        environment={"HOME": str(tmp_path), "PATH": "/usr/bin"},
        expected_sha256=attestation.sha256,
        clock=lambda: 10.0,
    )

    with pytest.raises(ConnectorError) as drifted:
        asyncio.run(runner.run(EXPECTED_ARGV, deadline=50.0))

    _assert_code(drifted, ConnectorErrorCode.CONNECTOR_STATE_INVALID)
    assert spawns == 0


def test_subprocess_uses_fixed_exec_and_allowlisted_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _SubprocessFixture()
    captured: dict[str, object] = {}

    async def create(*args: str, **kwargs: object) -> _SubprocessFixture:
        captured["args"] = args
        captured["kwargs"] = kwargs
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    output = asyncio.run(_subprocess_runner().run(EXPECTED_ARGV, deadline=50.0))

    assert output == FIXTURE_OUTPUT
    assert captured["args"] == ("/usr/local/bin/opencli", *EXPECTED_ARGV)
    kwargs = captured["kwargs"]
    assert kwargs["stdin"] is asyncio.subprocess.DEVNULL
    assert kwargs["stdout"] is asyncio.subprocess.PIPE
    assert kwargs["stderr"] is asyncio.subprocess.DEVNULL
    assert kwargs["cwd"] == "/tmp/opencli-home"
    assert kwargs["close_fds"] is True
    assert kwargs["start_new_session"] is True
    assert "shell" not in kwargs
    assert kwargs["env"] == {
        "HOME": "/tmp/opencli-home",
        "PATH": "/usr/local/bin:/usr/bin",
        "NO_COLOR": "1",
        "LANG": "C.UTF-8",
        "TMPDIR": "/tmp/opencli",
        "TZ": "UTC",
    }


@pytest.mark.parametrize(
    "environment",
    [
        {"HOME": "relative", "PATH": "/usr/bin"},
        {"HOME": "/tmp/opencli\nhome", "PATH": "/usr/bin"},
        {"HOME": "/tmp/opencli-home", "PATH": "bin:/usr/bin"},
        {"HOME": "/tmp/opencli-home", "PATH": "/usr/bin:\x1b[31m/bin"},
    ],
)
def test_subprocess_rejects_ambient_relative_or_controlled_paths(
    environment: dict[str, str],
) -> None:
    with pytest.raises(ValueError):
        OpenCliSubprocess(Path("/usr/local/bin/opencli"), environment=environment)


def test_subprocess_rejects_non_read_argv_before_process_creation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    async def create(*_: str, **__: object) -> _SubprocessFixture:
        nonlocal calls
        calls += 1
        return _SubprocessFixture()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    runner = _subprocess_runner()
    unsafe_arguments = (
        ("reddit", "upvote", POST_ID),
        (*EXPECTED_ARGV, "--extra"),
        (*EXPECTED_ARGV[:2], "ABC123", *EXPECTED_ARGV[3:]),
        (*EXPECTED_ARGV[:2], "bad/value", *EXPECTED_ARGV[3:]),
    )
    for argv in unsafe_arguments:
        with pytest.raises(ConnectorError) as rejected:
            asyncio.run(runner.run(argv, deadline=50.0))
        _assert_code(rejected, ConnectorErrorCode.CONNECTOR_STATE_INVALID)
    assert calls == 0


def test_subprocess_deadline_includes_process_creation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered = asyncio.Event()

    async def create(*_: str, **__: object) -> _SubprocessFixture:
        entered.set()
        await asyncio.Event().wait()
        return _SubprocessFixture()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)

    async def exercise() -> None:
        with pytest.raises(ConnectorError) as expired:
            await _subprocess_runner().run(EXPECTED_ARGV, deadline=10.001)
        _assert_code(expired, ConnectorErrorCode.CONNECTOR_DEADLINE_EXCEEDED)
        assert entered.is_set()

    asyncio.run(exercise())


def test_subprocess_cleanup_falls_back_when_process_group_signal_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    never = _NeverReader()
    process = _SubprocessFixture(reader=never)

    async def create(*_: str, **__: object) -> _SubprocessFixture:
        return process

    def fail_killpg(*_: object) -> None:
        raise OSError("PROCESS_ERROR_CANARY")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    monkeypatch.setattr(os, "killpg", fail_killpg)

    async def exercise() -> None:
        task = asyncio.create_task(
            _subprocess_runner().run(EXPECTED_ARGV, deadline=50.0)
        )
        await never.entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(exercise())
    assert process.direct_kills == 1


@pytest.mark.parametrize("terminal", ["nonzero", "oversize", "timeout", "cancel"])
def test_subprocess_redacts_failures_and_cleans_running_process_group(
    monkeypatch: pytest.MonkeyPatch, terminal: str
) -> None:
    never = _NeverReader()
    process = _SubprocessFixture(
        b"PROVIDER_OUTPUT_CANARY" * 4_000 if terminal == "oversize" else b"output",
        exit_code=7 if terminal == "nonzero" else 0,
        reader=never if terminal in {"timeout", "cancel"} else None,
    )
    killed: list[tuple[int, signal.Signals]] = []

    async def create(*_: str, **__: object) -> _SubprocessFixture:
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    monkeypatch.setattr(
        os,
        "killpg",
        lambda pid, requested_signal: killed.append((pid, requested_signal)),
    )
    runner = _subprocess_runner()

    async def exercise() -> None:
        task = asyncio.create_task(
            runner.run(
                EXPECTED_ARGV,
                deadline=10.001 if terminal == "timeout" else 50.0,
            )
        )
        if terminal == "cancel":
            await never.entered.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            return
        with pytest.raises(ConnectorError) as failed:
            await task
        expected_code = (
            ConnectorErrorCode.CONNECTOR_DEADLINE_EXCEEDED
            if terminal == "timeout"
            else ConnectorErrorCode.CONNECTOR_STATE_INVALID
        )
        _assert_code(failed, expected_code)
        assert "PROVIDER_OUTPUT_CANARY" not in str(failed.value)

    asyncio.run(exercise())
    expected_kill = [] if terminal == "nonzero" else [(process.pid, signal.SIGKILL)]
    assert killed == expected_kill
    assert process.direct_kills == 0


def test_default_runtime_does_not_bind_or_execute_reddit() -> None:
    availability = DEFAULT_RUNTIME.operation_availability("reddit", "read.post")

    assert availability.state == "unavailable"
    assert asyncio.run(DEFAULT_RUNTIME.dispatch(_reddit_call())) is None
