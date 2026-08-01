from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import sys
import time
from collections.abc import Mapping
from dataclasses import fields
from pathlib import Path
from typing import cast

import pytest

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
    validate_browse,
    validate_read,
    validate_search,
)
from hermes_reach.sources.opencli_social import (
    OpenCliSessionAttestation,
    OpenCliSocialExecutor,
    attest_opencli_social_session,
    opencli_social_execution_composition,
    opencli_social_scopes,
)
from hermes_reach.sources.opencli_social_worker import (
    ForkExecutionFailure,
    OpenCliSocialProjection,
    SocialItemProjection,
    WorkerErrorCode,
    WorkerOperation,
    WorkerResponse,
    WorkerSource,
)

NOW = 1_800_000_000
POST_URL = "https://www.reddit.com/r/python/comments/abc123/fixture_post"
_DIGEST = "a" * 64


def _id(value: int) -> str:
    return base64.b32encode(value.to_bytes(16, "big")).decode().rstrip("=").lower()


def _attestation() -> OpenCliSessionAttestation:
    return OpenCliSessionAttestation(
        Path("/opt/hermes-reach/opencli/bin/node"),
        _DIGEST,
        Path("/opt/hermes-reach/opencli"),
        Path(
            "/opt/hermes-reach/opencli/node_modules/@jackwener/opencli/dist/src/main.js"
        ),
        "b" * 64,
        Path("/Users/operator/opencli-session"),
    )


def _attestable_closure(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    node = tmp_path / "node"
    node.write_bytes(b"#!/bin/sh\nexit 0\n")
    node.chmod(0o700)
    root = tmp_path / "opencli"
    package_root = root / "node_modules" / "@jackwener" / "opencli"
    cli = package_root / "dist" / "src" / "main.js"
    cli.parent.mkdir(parents=True)
    cli.write_bytes(b"export {};\n")
    package = {
        "name": "@jackwener/opencli",
        "version": "1.8.6-hermes.1",
        "bin": {"opencli": "dist/src/main.js"},
    }
    (package_root / "package.json").write_text(
        json.dumps(package, separators=(",", ":")), encoding="utf-8"
    )
    for path in root.rglob("*"):
        path.chmod(0o700 if path.is_dir() else 0o600)
    root.chmod(0o700)
    session_home = tmp_path / "session"
    session_home.mkdir(mode=0o700)
    return node, root, cli, session_home


def _search(source: str, operation: str) -> OperationCall:
    return validate_search(
        {
            "requests": [
                {
                    "source": source,
                    "operation": operation,
                    "query": "private query canary",
                    "options": {"limit": 7},
                }
            ]
        }
    )[0]


def _read(source: str, operation: str, target: Mapping[str, str]) -> OperationCall:
    return validate_read(
        {"source": source, "operation": operation, "target": dict(target)}
    )


def _browse(
    source: str,
    operation: str,
    *,
    target: Mapping[str, str] | None = None,
) -> OperationCall:
    payload: dict[str, object] = {
        "source": source,
        "operation": operation,
        "options": {"limit": 7},
    }
    if target is not None:
        payload["target"] = dict(target)
    return validate_browse(payload)


SOCIAL_CALLS = (
    (_search("reddit", "search.posts"), {"query": "private query canary", "limit": 7}),
    (_read("reddit", "read.post", {"url": POST_URL}), {"url": POST_URL}),
    (
        _browse("reddit", "browse.subreddit", target={"native_id": "Python_3"}),
        {"subreddit": "Python_3", "limit": 7},
    ),
    (_browse("reddit", "browse.hot"), {"limit": 7}),
    (_browse("reddit", "browse.popular"), {"limit": 7}),
    (_browse("reddit", "browse.all"), {"limit": 7}),
    (
        _read("reddit", "read.subreddit", {"native_id": "Python_3"}),
        {"subreddit": "Python_3"},
    ),
    (_search("facebook", "search"), {"query": "private query canary", "limit": 7}),
    (
        _read("facebook", "read.profile", {"native_id": "open.ai-profile"}),
        {"username": "open.ai-profile"},
    ),
    (_browse("facebook", "browse.feed"), {"limit": 7}),
    (_browse("facebook", "browse.groups"), {"limit": 7}),
    (
        _search("instagram", "search.users"),
        {"query": "private query canary", "limit": 7},
    ),
    (
        _read("instagram", "read.profile", {"native_id": "openai.dev"}),
        {"username": "openai.dev"},
    ),
    (
        _browse("instagram", "browse.user_posts", target={"native_id": "openai.dev"}),
        {"username": "openai.dev", "limit": 7},
    ),
    (_browse("instagram", "browse.explore"), {"limit": 7}),
)


def _execution(call: OperationCall, *, deadline: int = NOW + 20) -> AuthorizedExecution:
    connector = DevicePrivateIdentity._from_seed_for_testing(bytes([60]) * 32)
    vps = DevicePrivateIdentity._from_seed_for_testing(bytes([61]) * 32)
    protected = protect_operation_call(call)
    request = create_signed_request(
        vps,
        message_id=_id(1),
        request_id=_id(2),
        trace_id="a" * 32,
        audience_key_id=connector.public_identity.key_id,
        grant_id=_id(3),
        grant_revision=1,
        policy_revision=1,
        source=call.source.name,
        operation=call.operation.name,
        issued_at=NOW,
        deadline=deadline,
        protected_payload=protected,
    )
    return AuthorizedExecution(
        request,
        protected,
        GrantScope(
            call.source.name,
            call.operation.name,
            call.operation.runtime.data_scope,
        ),
        ClaimResult(True, None, 1, 4, "c" * 64),
    )


class _Worker:
    def __init__(self, responses: list[WorkerResponse] | None = None) -> None:
        self.responses = list(responses or [])
        self.calls: list[tuple[str, str, dict[str, object], float]] = []

    async def execute(
        self,
        source: str,
        operation: str,
        arguments: Mapping[str, object],
        *,
        deadline: float,
    ) -> WorkerResponse:
        self.calls.append((source, operation, dict(arguments), deadline))
        if self.responses:
            return self.responses.pop(0)
        return OpenCliSocialProjection(
            cast(WorkerSource, source),
            cast(WorkerOperation, operation),
            (),
            False,
        )


@pytest.mark.parametrize(("call", "expected_arguments"), SOCIAL_CALLS)
def test_executor_maps_all_fifteen_catalog_operations_to_closed_worker_requests(
    call: OperationCall,
    expected_arguments: dict[str, object],
) -> None:
    worker = _Worker()
    executor = OpenCliSocialExecutor(
        _attestation(),
        worker,
        wall_clock=lambda: float(NOW),
        monotonic_clock=lambda: 100.0,
    )

    result = asyncio.run(executor.execute(_execution(call), {}))

    assert result.items == ()
    assert worker.calls == [
        (call.source.name, call.operation.name, expected_arguments, 120.0)
    ]


def test_attestation_has_exact_redacted_opencli_session_shape() -> None:
    attestation = _attestation()

    assert tuple(field.name for field in fields(attestation)) == (
        "node_executable",
        "node_sha256",
        "opencli_root",
        "opencli_cli",
        "opencli_tree_sha256",
        "session_home",
    )
    assert repr(attestation) == "OpenCliSessionAttestation(<redacted>)"
    assert "operator" not in repr(attestation)
    assert set(attestation.frame_fields()) == {
        "node_executable",
        "node_sha256",
        "opencli_root",
        "opencli_cli",
        "opencli_tree_sha256",
        "session_home",
    }


def test_startup_attestation_hashes_the_exact_node_and_complete_opencli_tree(
    tmp_path: Path,
) -> None:
    node, root, cli, session_home = _attestable_closure(tmp_path)

    attestation = attest_opencli_social_session(node, root, cli, session_home)

    assert attestation.node_executable == node
    assert attestation.node_sha256 == hashlib.sha256(node.read_bytes()).hexdigest()
    assert attestation.opencli_root == root
    assert attestation.opencli_cli == cli
    assert len(attestation.opencli_tree_sha256) == 64
    assert attestation.session_home == session_home
    cli.write_bytes(b"export const changed = true;\n")
    cli.chmod(0o600)
    changed = attest_opencli_social_session(node, root, cli, session_home)
    assert changed.opencli_tree_sha256 != attestation.opencli_tree_sha256


def test_real_isolated_worker_executes_the_attested_closure_through_the_pinned_fork(
    tmp_path: Path,
) -> None:
    node, root, cli, session_home = _attestable_closure(tmp_path)
    output = """\
- type: POST
  author: alice
  score: 12
  text: Fixture Reddit post
  post_hint: self
  url_overridden_by_dest: ""
  preview_image_url: ""
  gallery_urls: []
"""
    expected_argv = (
        "reddit",
        "read",
        "abc123",
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
        "--format",
        "yaml",
    )
    node.write_text(
        f"#!{sys.executable}\n"
        "import sys\n"
        f"if tuple(sys.argv[2:]) != {expected_argv!r}:\n"
        "    raise SystemExit(7)\n"
        f"sys.stdout.write({output!r})\n",
        encoding="utf-8",
    )
    node.chmod(0o700)
    attestation = attest_opencli_social_session(node, root, cli, session_home)
    call = _read("reddit", "read.post", {"url": POST_URL})
    executor = OpenCliSocialExecutor(
        attestation,
        wall_clock=lambda: float(NOW),
        monotonic_clock=time.monotonic,
    )

    result = asyncio.run(
        executor.execute(
            _execution(call, deadline=NOW + 20),
            {},
        )
    )

    assert [(item.kind, item.text) for item in result.items] == [
        ("content", "Fixture Reddit post | score: 12 | media: self")
    ]
    assert result.items[0].native_id == "abc123"


@pytest.mark.parametrize("termination", ["cancel", "deadline"])
def test_isolated_worker_reaps_separate_node_session_on_termination(
    tmp_path: Path,
    termination: str,
) -> None:
    node, root, cli, session_home = _attestable_closure(tmp_path)
    pid_path = tmp_path / "node.pid"
    stop_path = tmp_path / "node.stop"
    node.write_text(
        f"#!{sys.executable}\n"
        "import json\n"
        "import os\n"
        "import time\n"
        "from pathlib import Path\n"
        f"pid_path = Path({str(pid_path)!r})\n"
        "pending_path = pid_path.with_suffix('.pending')\n"
        "pending_path.write_text(json.dumps({\n"
        "    'pid': os.getpid(),\n"
        "    'ppid': os.getppid(),\n"
        "    'pgid': os.getpgid(0),\n"
        "    'sid': os.getsid(0),\n"
        "}), encoding='ascii')\n"
        "pending_path.replace(pid_path)\n"
        f"while not Path({str(stop_path)!r}).exists():\n"
        "    time.sleep(0.05)\n",
        encoding="utf-8",
    )
    node.chmod(0o700)
    attestation = attest_opencli_social_session(node, root, cli, session_home)
    call = _read("reddit", "read.post", {"url": POST_URL})
    executor = OpenCliSocialExecutor(
        attestation,
        wall_clock=lambda: float(NOW),
        monotonic_clock=time.monotonic,
    )

    async def exercise() -> None:
        operation_deadline = NOW + (2 if termination == "deadline" else 20)
        task = asyncio.create_task(
            executor.execute(_execution(call, deadline=operation_deadline), {})
        )
        try:
            for _ in range(300):
                if pid_path.is_file():
                    break
                await asyncio.sleep(0.01)
            assert pid_path.is_file()
            identity = json.loads(pid_path.read_text(encoding="ascii"))
            assert set(identity) == {"pid", "ppid", "pgid", "sid"}
            assert all(type(value) is int for value in identity.values())
            node_pid = cast(int, identity["pid"])
            worker_pid = cast(int, identity["ppid"])
            assert identity == {
                "pid": node_pid,
                "ppid": worker_pid,
                "pgid": node_pid,
                "sid": node_pid,
            }
            assert os.getpgid(worker_pid) == worker_pid
            assert os.getsid(worker_pid) == worker_pid

            if termination == "cancel":
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(task, timeout=10)
            else:
                with pytest.raises(ConnectorError) as caught:
                    await asyncio.wait_for(task, timeout=10)
                assert caught.value.code == (
                    ConnectorErrorCode.BACKEND_DEADLINE_EXCEEDED.value
                )

            for _ in range(100):
                try:
                    os.kill(node_pid, 0)
                except ProcessLookupError:
                    break
                await asyncio.sleep(0.01)
            else:
                pytest.fail(
                    "The isolated OpenCLI Node process survived worker cleanup."
                )
        finally:
            stop_path.touch()
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    asyncio.run(exercise())


@pytest.mark.parametrize("invalid_kind", ["package", "mode", "entrypoint", "overlap"])
def test_startup_attestation_rejects_incompatible_closures_without_path_leakage(
    tmp_path: Path,
    invalid_kind: str,
) -> None:
    node, root, cli, session_home = _attestable_closure(tmp_path)
    if invalid_kind == "package":
        package_json = cli.parents[2] / "package.json"
        package_json.write_text(
            json.dumps(
                {
                    "name": "@jackwener/opencli",
                    "version": "1.8.6",
                    "bin": {"opencli": "dist/src/main.js"},
                }
            ),
            encoding="utf-8",
        )
        package_json.chmod(0o600)
    elif invalid_kind == "mode":
        cli.chmod(0o622)
    elif invalid_kind == "entrypoint":
        other = root / "main.js"
        other.write_bytes(b"export {};\n")
        other.chmod(0o600)
        cli = other
    else:
        session_home = root

    with pytest.raises(ConnectorError) as caught:
        attest_opencli_social_session(node, root, cli, session_home)

    assert caught.value.code == ConnectorErrorCode.BACKEND_INCOMPATIBLE.value
    assert str(tmp_path) not in str(caught.value)


@pytest.mark.parametrize(
    "mutation",
    [
        {"node_executable": Path("relative/node")},
        {"node_sha256": "A" * 64},
        {"opencli_cli": Path("/opt/hermes-reach/other/cli.js")},
        {"opencli_tree_sha256": "short"},
        {"session_home": Path("relative/session")},
    ],
)
def test_attestation_rejects_path_or_digest_authority_drift(
    mutation: dict[str, object],
) -> None:
    values: dict[str, object] = {
        "node_executable": Path("/opt/hermes-reach/opencli/bin/node"),
        "node_sha256": _DIGEST,
        "opencli_root": Path("/opt/hermes-reach/opencli"),
        "opencli_cli": Path(
            "/opt/hermes-reach/opencli/node_modules/@jackwener/opencli/dist/cli.js"
        ),
        "opencli_tree_sha256": "b" * 64,
        "session_home": Path("/Users/operator/opencli-session"),
    }
    values.update(mutation)

    with pytest.raises(ValueError, match="attestation is invalid"):
        OpenCliSessionAttestation(**values)  # type: ignore[arg-type]


def test_composition_binds_exact_backend_and_all_public_account_scopes() -> None:
    composition = opencli_social_execution_composition(_attestation())

    assert repr(composition) == "ConnectorExecutionComposition(count=15)"
    assert composition.required_scope("reddit", "read.post") == GrantScope(
        "reddit", "read.post", "public"
    )
    assert composition.required_scope("facebook", "browse.feed") == GrantScope(
        "facebook", "browse.feed", "account_visible"
    )
    assert composition.required_scope("instagram", "browse.explore") == GrantScope(
        "instagram", "browse.explore", "account_visible"
    )
    assert composition.required_scope("twitter", "search.posts") is None
    scopes = opencli_social_scopes()
    assert len(scopes) == 15
    assert all(
        composition.required_scope(scope.source, scope.operation) == scope
        for scope in scopes
    )


@pytest.mark.parametrize(
    "error_code",
    ["backend_unavailable", "deadline_exceeded", "transient"],
)
def test_executor_retries_eligible_failure_once_with_same_absolute_deadline(
    error_code: WorkerErrorCode,
) -> None:
    call = SOCIAL_CALLS[0][0]
    worker = _Worker(
        [
            ForkExecutionFailure("reddit", "search.posts", error_code),
            OpenCliSocialProjection("reddit", "search.posts", (), False),
        ]
    )
    times = iter((100.0, 101.0))
    executor = OpenCliSocialExecutor(
        _attestation(),
        worker,
        wall_clock=lambda: float(NOW),
        monotonic_clock=lambda: next(times),
    )

    result = asyncio.run(executor.execute(_execution(call), {}))

    assert result.items == ()
    assert len(worker.calls) == 2
    assert worker.calls[0][3] == worker.calls[1][3] == 120.0


@pytest.mark.parametrize(
    ("error_code", "connector_code"),
    [
        ("invalid_input", ConnectorErrorCode.BACKEND_INVALID_INPUT),
        ("not_found", ConnectorErrorCode.BACKEND_NOT_FOUND),
        ("authentication", ConnectorErrorCode.BACKEND_AUTHENTICATION_REQUIRED),
        ("authorization", ConnectorErrorCode.BACKEND_AUTHORIZATION_DENIED),
        ("backend_incompatible", ConnectorErrorCode.BACKEND_INCOMPATIBLE),
        ("rate_limit", ConnectorErrorCode.BACKEND_RATE_LIMITED),
        ("permanent", ConnectorErrorCode.BACKEND_PERMANENT),
        (
            "backend_contract_violation",
            ConnectorErrorCode.BACKEND_CONTRACT_VIOLATION,
        ),
    ],
)
def test_executor_maps_non_retryable_fork_failures_without_payload_leakage(
    error_code: WorkerErrorCode,
    connector_code: ConnectorErrorCode,
) -> None:
    call = SOCIAL_CALLS[0][0]
    worker = _Worker([ForkExecutionFailure("reddit", "search.posts", error_code)])
    executor = OpenCliSocialExecutor(
        _attestation(),
        worker,
        wall_clock=lambda: float(NOW),
        monotonic_clock=lambda: 100.0,
    )

    with pytest.raises(ConnectorError) as caught:
        asyncio.run(executor.execute(_execution(call), {}))

    assert caught.value.code == connector_code.value
    assert len(worker.calls) == 1
    rendered = f"{caught.value!r} {caught.value}"
    assert "private query canary" not in rendered
    assert "/Users/operator" not in rendered


def test_executor_projects_only_normalized_public_result_fields() -> None:
    call = SOCIAL_CALLS[8][0]
    projection = OpenCliSocialProjection(
        "facebook",
        "read.profile",
        (
            SocialItemProjection(
                "profile",
                "friends: 12 | followers: 34",
                native_id="open.ai-profile",
                title="Open AI",
                url="https://www.facebook.com/open.ai-profile",
            ),
        ),
        True,
    )
    executor = OpenCliSocialExecutor(
        _attestation(),
        _Worker([projection]),
        wall_clock=lambda: float(NOW),
        monotonic_clock=lambda: 100.0,
    )

    result = asyncio.run(executor.execute(_execution(call), {}))

    assert result.truncated is True
    assert result.items[0].kind == "profile"
    assert result.items[0].text == "friends: 12 | followers: 34"
    assert result.items[0].native_id == "open.ai-profile"
    assert result.items[0].title == "Open AI"


def test_executor_rejects_nonempty_environment_before_worker_handoff() -> None:
    call = SOCIAL_CALLS[0][0]
    worker = _Worker()
    executor = OpenCliSocialExecutor(
        _attestation(),
        worker,
        wall_clock=lambda: float(NOW),
        monotonic_clock=lambda: 100.0,
    )

    with pytest.raises(ConnectorError) as caught:
        asyncio.run(executor.execute(_execution(call), {"COOKIE": "secret"}))

    assert caught.value.code == ConnectorErrorCode.CONNECTOR_STATE_INVALID.value
    assert worker.calls == []
