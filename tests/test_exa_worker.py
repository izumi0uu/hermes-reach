from __future__ import annotations

import io
import json
import subprocess
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

import hermes_reach.sources.exa_worker as worker
from hermes_reach.sources.exa_artifacts import ExaArtifactAttestation

QUERY = "private Exa query"
_DIGEST = "a" * 64


def _artifacts() -> ExaArtifactAttestation:
    return ExaArtifactAttestation(
        Path("/opt/hermes-reach/exa/bin/node"),
        _DIGEST,
        Path("/opt/hermes-reach/exa/mcporter"),
        Path("/opt/hermes-reach/exa/mcporter/dist/cli.js"),
        "b" * 64,
        Path("/opt/hermes-reach/exa/config.json"),
        "c" * 64,
    )


@dataclass(frozen=True)
class _ExecutionRequest:
    protocol_version: str
    source: str
    operation: str
    arguments: Mapping[str, object]


@dataclass(frozen=True)
class _NetworkAccess:
    pass


@dataclass(frozen=True)
class _McporterArtifacts:
    node_executable: str
    node_sha256: str
    mcporter_root: str
    mcporter_cli: str
    mcporter_tree_sha256: str
    config_path: str
    config_sha256: str


@dataclass(frozen=True)
class _Limits:
    maximum_items: int
    maximum_text_characters: int


@dataclass(frozen=True)
class _Context:
    host_capabilities: tuple[object, ...]
    limits: _Limits

    def __init__(
        self, host_capabilities: tuple[object, ...], *, limits: _Limits
    ) -> None:
        object.__setattr__(self, "host_capabilities", host_capabilities)
        object.__setattr__(self, "limits", limits)


@dataclass(frozen=True)
class _Item:
    schema_id: object
    fields: object


@dataclass(frozen=True)
class _Success:
    protocol_version: object = "v1"
    source: object = "exa"
    operation: object = "search.web"
    backend_id: object = "exa-mcporter"
    backend_version: object = "0.12.3+exa-web.v1"
    items: object = ()
    truncated: object = False
    partial_error_code: object = None


@dataclass(frozen=True)
class _Failure:
    protocol_version: object = "v1"
    source: object = "exa"
    operation: object = "search.web"
    backend_id: object = "exa-mcporter"
    backend_version: object = "0.12.3+exa-web.v1"
    error_code: object = "transient"


def _api(execute: Callable[[object, object], object]) -> SimpleNamespace:
    return SimpleNamespace(
        execution_request_type=_ExecutionRequest,
        network_access_type=_NetworkAccess,
        mcporter_artifacts_type=_McporterArtifacts,
        execution_limits_type=_Limits,
        execution_context_type=_Context,
        execution_item_type=_Item,
        execution_success_type=_Success,
        execution_failure_type=_Failure,
        execute=execute,
    )


def _item(
    operation: worker.WorkerOperation = "search.web",
    **overrides: object,
) -> _Item:
    if operation == "search.code":
        return _Item(
            "exa.code.result.v1",
            {
                "text": "def retry(): pass",
                "title": "Retry implementation",
                "url": "https://github.com/example/repository/blob/main/retry.py",
                **overrides,
            },
        )
    return _Item(
        "exa.search.result.v1",
        {
            "text": "Result body",
            "title": "Result title",
            "url": "https://example.com/result?from=exa",
            "author": "Author",
            "published_at": "2026-07-31",
            **overrides,
        },
    )


def _success_value(
    operation: worker.WorkerOperation = "search.web",
    **overrides: object,
) -> dict[str, object]:
    code = operation == "search.code"
    return {
        "backend": {
            "id": "exa-mcporter",
            "version": ("0.12.3+exa-code.v1" if code else "0.12.3+exa-web.v1"),
        },
        "items": (
            [
                {
                    "text": "def retry(): pass",
                    "title": "Retry implementation",
                    "url": "https://github.com/example/repository/blob/main/retry.py",
                }
            ]
            if code
            else [
                {
                    "author": "Author",
                    "published_at": "2026-07-31",
                    "text": "Result body",
                    "title": "Result title",
                    "url": "https://example.com/result?from=exa",
                }
            ]
        ),
        "operation": operation,
        "partial": None,
        "protocol": "v1",
        "schema": "exa.code.result.v1" if code else "exa.search.result.v1",
        "source": "exa",
        "truncated": False,
        **overrides,
    }


def _framed(value: Mapping[str, object]) -> bytes:
    return worker._encode_frame(value, worker.MAX_OUTPUT_BYTES)


def _provider(value: SimpleNamespace) -> worker.ExecutionApiProvider:
    return cast(worker.ExecutionApiProvider, lambda: value)


def test_request_frame_is_closed_and_round_trips_artifact_identity() -> None:
    raw = worker.encode_request(QUERY, 50, _artifacts())

    request = worker._read_request(io.BytesIO(raw))

    assert request == worker.WorkerRequest("search.web", QUERY, 50, _artifacts())
    assert QUERY not in repr(request)
    assert "/opt/hermes-reach" not in repr(request)
    payload = json.loads(raw[4:])
    assert set(payload) == {"artifacts", "limit", "operation", "protocol", "query"}
    assert set(payload["artifacts"]) == {
        "config_path",
        "config_sha256",
        "mcporter_cli",
        "mcporter_root",
        "mcporter_tree_sha256",
        "node_executable",
        "node_sha256",
    }


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: {**value, "command": "forbidden"},
        lambda value: {**value, "operation": "search.future"},
        lambda value: {**value, "query": " query"},
        lambda value: {**value, "limit": True},
        lambda value: {
            **value,
            "artifacts": {**cast(dict[str, object], value["artifacts"]), "path": "/x"},
        },
    ],
)
def test_worker_request_rejects_every_authority_or_shape_drift(
    mutation: Callable[[dict[str, object]], dict[str, object]],
) -> None:
    value: dict[str, object] = {
        "artifacts": _artifacts().frame_fields(),
        "limit": 1,
        "operation": "search.web",
        "protocol": "v1",
        "query": QUERY,
    }

    with pytest.raises(worker.ExaProtocolError):
        worker._validated_request(mutation(value))


def test_worker_builds_exact_typed_fork_request_context_and_projection() -> None:
    calls: list[tuple[object, object]] = []

    def execute(request: object, context: object) -> object:
        calls.append((request, context))
        return _Success(items=(_item(),), truncated=True)

    value = worker._execute_request(
        worker.WorkerRequest("search.web", QUERY, 50, _artifacts()),
        execution_api_provider=_provider(_api(execute)),
    )

    assert len(calls) == 1
    request = cast(_ExecutionRequest, calls[0][0])
    context = cast(_Context, calls[0][1])
    assert request == _ExecutionRequest(
        "v1",
        "exa",
        "search.web",
        {"query": QUERY, "limit": 50},
    )
    assert context.host_capabilities == (
        _NetworkAccess(),
        _McporterArtifacts(**_artifacts().frame_fields()),
    )
    assert context.limits == _Limits(20, 16_000)
    assert worker.decode_response(
        _framed(cast(Mapping[str, object], value)),
        limit=50,
    ) == worker.ExaProjection(
        "search.web",
        (
            worker.ExaResultProjection(
                "Result body",
                "Result title",
                "https://example.com/result?from=exa",
                "Author",
                "2026-07-31",
            ),
        ),
        True,
    )
    assert QUERY not in repr(value)


def test_code_worker_uses_independent_request_identity_schema_and_projection() -> None:
    calls: list[tuple[object, object]] = []

    def execute(request: object, context: object) -> object:
        calls.append((request, context))
        return _Success(
            operation="search.code",
            backend_version="0.12.3+exa-code.v1",
            items=(_item("search.code"),),
        )

    request = worker.WorkerRequest("search.code", QUERY, 4, _artifacts())
    value = worker._execute_request(
        request,
        execution_api_provider=_provider(_api(execute)),
    )

    assert len(calls) == 1
    execution_request = cast(_ExecutionRequest, calls[0][0])
    context = cast(_Context, calls[0][1])
    assert execution_request == _ExecutionRequest(
        "v1",
        "exa",
        "search.code",
        {"query": QUERY, "limit": 4},
    )
    assert context.host_capabilities == (
        _NetworkAccess(),
        _McporterArtifacts(**_artifacts().frame_fields()),
    )
    assert worker.decode_response(
        _framed(cast(Mapping[str, object], value)),
        operation="search.code",
        limit=4,
    ) == worker.ExaProjection(
        "search.code",
        (
            worker.ExaCodeResultProjection(
                "def retry(): pass",
                "Retry implementation",
                "https://github.com/example/repository/blob/main/retry.py",
            ),
        ),
        False,
    )


@pytest.mark.parametrize(
    ("expected_operation", "value"),
    [
        ("search.web", _success_value("search.code")),
        ("search.code", _success_value("search.web")),
        (
            "search.code",
            {
                **_success_value("search.code"),
                "schema": "exa.search.result.v1",
            },
        ),
    ],
)
def test_parent_decoder_rejects_web_code_identity_or_schema_substitution(
    expected_operation: worker.WorkerOperation,
    value: Mapping[str, object],
) -> None:
    with pytest.raises(worker.ExaProtocolError):
        worker.decode_response(
            _framed(value),
            operation=expected_operation,
            limit=4,
        )


def test_worker_preserves_only_closed_fork_failure_code() -> None:
    value = worker._execute_request(
        worker.WorkerRequest("search.web", QUERY, 1, _artifacts()),
        execution_api_provider=_provider(
            _api(lambda *_: _Failure(error_code="rate_limit"))
        ),
    )

    assert worker.decode_response(
        _framed(cast(Mapping[str, object], value)),
        limit=1,
    ) == worker.ForkExecutionFailure("search.web", "rate_limit")
    assert QUERY not in repr(value)


@pytest.mark.parametrize(
    "result",
    [
        _Success(source="other"),
        _Success(backend_version="future"),
        _Success(partial_error_code="transient"),
        _Success(items=(_Item("other.schema", {}),)),
        _Success(items=(_item(extra="field"),)),
        _Success(items=(_item(text="body  gap"),)),
        _Success(items=(_item(title="title\x01detail"),)),
        _Success(items=(_item(author="author\x7fdetail"),)),
        _Success(items=(_item(published_at="date\ndetail"),)),
        _Success(items=(_item(url="https://example.com/résultat"),)),
        _Failure(error_code="future"),
        object(),
    ],
)
def test_worker_converts_fork_contract_drift_to_closed_failure(result: object) -> None:
    value = worker._execute_request(
        worker.WorkerRequest("search.web", QUERY, 1, _artifacts()),
        execution_api_provider=_provider(_api(lambda *_: result)),
    )

    assert value == worker._failure_value("backend_contract_violation")
    assert QUERY not in repr(value)


@pytest.mark.parametrize("artifacts_type", [None, object()])
def test_worker_rejects_invalid_fork_artifact_capability_type(
    artifacts_type: object,
) -> None:
    api = _api(lambda *_: _Success())
    api.mcporter_artifacts_type = artifacts_type

    value = worker._execute_request(
        worker.WorkerRequest("search.web", QUERY, 1, _artifacts()),
        execution_api_provider=_provider(api),
    )

    assert value == worker._failure_value("backend_contract_violation")
    assert QUERY not in repr(value)


def test_worker_rejects_missing_fork_artifact_capability_type() -> None:
    api = _api(lambda *_: _Success())
    del api.mcporter_artifacts_type

    value = worker._execute_request(
        worker.WorkerRequest("search.web", QUERY, 1, _artifacts()),
        execution_api_provider=_provider(api),
    )

    assert value == worker._failure_value("backend_contract_violation")
    assert QUERY not in repr(value)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: {**value, "query": QUERY},
        lambda value: {**value, "source": "other"},
        lambda value: {**value, "schema": "future"},
        lambda value: {**value, "truncated": 1},
        lambda value: {**value, "items": [*cast(list[object], value["items"])] * 2},
        lambda value: {
            **value,
            "items": [
                {
                    **cast(list[dict[str, object]], value["items"])[0],
                    "url": "http://127.0.0.1/private",
                }
            ],
        },
        lambda value: {
            **value,
            "items": [
                {
                    **cast(list[dict[str, object]], value["items"])[0],
                    "text": "value\x00hidden",
                }
            ],
        },
    ],
)
def test_parent_decoder_rejects_identity_shape_bounds_and_unsafe_urls(
    mutation: Callable[[dict[str, object]], dict[str, object]],
) -> None:
    with pytest.raises(worker.ExaProtocolError):
        worker.decode_response(_framed(mutation(_success_value())), limit=1)


def test_parent_decoder_accepts_empty_and_nullable_closed_results() -> None:
    empty = worker.decode_response(
        _framed(_success_value(items=[], truncated=True)),
        limit=1,
    )
    nullable = _success_value()
    item = cast(list[dict[str, object]], nullable["items"])[0]
    item["author"] = None
    item["published_at"] = None

    assert empty == worker.ExaProjection("search.web", (), True)
    assert worker.decode_response(_framed(nullable), limit=1).items[0] == (
        worker.ExaResultProjection(
            "Result body",
            "Result title",
            "https://example.com/result?from=exa",
            None,
            None,
        )
    )


def test_parent_decoder_accepts_exact_fork_scalar_and_multibyte_bounds() -> None:
    value = _success_value()
    item = cast(list[dict[str, object]], value["items"])[0]
    url_prefix = "https://example.com/"
    item.update(
        {
            "text": "界" * worker.MAX_TEXT_CHARACTERS,
            "title": "t" * worker.MAX_TITLE_CHARACTERS,
            "url": url_prefix + "u" * (worker.MAX_URL_CHARACTERS - len(url_prefix)),
            "author": "a" * worker.MAX_AUTHOR_CHARACTERS,
            "published_at": "p" * worker.MAX_PUBLISHED_CHARACTERS,
        }
    )

    response = worker.decode_response(_framed(value), limit=1)

    projected = response.items[0]
    assert len(projected.text) == worker.MAX_TEXT_CHARACTERS
    assert len(projected.title) == worker.MAX_TITLE_CHARACTERS
    assert len(projected.url) == worker.MAX_URL_CHARACTERS
    assert projected.author is not None
    assert len(projected.author) == worker.MAX_AUTHOR_CHARACTERS
    assert projected.published_at is not None
    assert len(projected.published_at) == worker.MAX_PUBLISHED_CHARACTERS


def test_duplicate_json_keys_and_trailing_frame_bytes_fail_closed() -> None:
    duplicate = b'{"protocol":"v1","protocol":"v1"}'
    framed = len(duplicate).to_bytes(4, "big") + duplicate

    with pytest.raises(worker.ExaProtocolError):
        worker._read_request(io.BytesIO(framed))
    with pytest.raises(worker.ExaProtocolError):
        worker.decode_response(_framed(_success_value()) + b"x", limit=1)


@pytest.mark.parametrize(
    "selected",
    [
        {"oversized": QUERY + "x" * worker.MAX_OUTPUT_BYTES},
        {"invalid_unicode": QUERY + "\ud800"},
    ],
)
def test_main_frames_unencodable_selected_result_as_closed_permanent_failure(
    monkeypatch: pytest.MonkeyPatch,
    selected: Mapping[str, object],
) -> None:
    stdout = io.BytesIO()
    monkeypatch.setattr(
        worker.sys,
        "stdin",
        SimpleNamespace(
            buffer=io.BytesIO(worker.encode_request(QUERY, 1, _artifacts()))
        ),
    )
    monkeypatch.setattr(worker.sys, "stdout", SimpleNamespace(buffer=stdout))
    monkeypatch.setattr(worker, "_execute_request", lambda _: selected)

    assert worker.MAX_OUTPUT_BYTES == 524_288
    assert worker._main() == 0
    assert len(stdout.getvalue()) <= worker.MAX_OUTPUT_BYTES + 4
    assert worker.decode_response(stdout.getvalue(), limit=1) == (
        worker.ForkExecutionFailure("search.web", "backend_contract_violation")
    )
    assert QUERY.encode() not in stdout.getvalue()


def test_code_main_overflow_preserves_code_failure_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stdout = io.BytesIO()
    request = worker.encode_request(
        QUERY,
        1,
        _artifacts(),
        operation="search.code",
    )
    monkeypatch.setattr(
        worker.sys,
        "stdin",
        SimpleNamespace(buffer=io.BytesIO(request)),
    )
    monkeypatch.setattr(worker.sys, "stdout", SimpleNamespace(buffer=stdout))
    monkeypatch.setattr(
        worker,
        "_execute_request",
        lambda _: {"oversized": QUERY + "x" * worker.MAX_OUTPUT_BYTES},
    )

    assert worker._main() == 0
    assert worker.decode_response(
        stdout.getvalue(),
        operation="search.code",
        limit=1,
    ) == worker.ForkExecutionFailure(
        "search.code",
        "backend_contract_violation",
    )
    assert QUERY.encode() not in stdout.getvalue()


def test_main_preserves_nonzero_exit_for_execution_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stdout = io.BytesIO()
    monkeypatch.setattr(
        worker.sys,
        "stdin",
        SimpleNamespace(
            buffer=io.BytesIO(worker.encode_request(QUERY, 1, _artifacts()))
        ),
    )
    monkeypatch.setattr(worker.sys, "stdout", SimpleNamespace(buffer=stdout))

    def fail_execution(_: worker.WorkerRequest) -> Mapping[str, object]:
        raise RuntimeError("private execution failure")

    monkeypatch.setattr(worker, "_execute_request", fail_execution)

    assert worker._main() == 1
    assert stdout.getvalue() == b""


@pytest.mark.parametrize("failure_stage", ["write", "flush"])
def test_main_preserves_nonzero_exit_for_output_failure(
    monkeypatch: pytest.MonkeyPatch,
    failure_stage: str,
) -> None:
    class FailingOutput(io.BytesIO):
        def write(self, value: bytes, /) -> int:
            if failure_stage == "write":
                raise OSError("private write failure")
            return super().write(value)

        def flush(self) -> None:
            if failure_stage == "flush":
                raise OSError("private flush failure")
            super().flush()

    monkeypatch.setattr(
        worker.sys,
        "stdin",
        SimpleNamespace(
            buffer=io.BytesIO(worker.encode_request(QUERY, 1, _artifacts()))
        ),
    )
    monkeypatch.setattr(
        worker.sys,
        "stdout",
        SimpleNamespace(buffer=FailingOutput()),
    )
    monkeypatch.setattr(worker, "_execute_request", lambda _: _success_value())

    assert worker._main() == 1


def test_runtime_loader_requests_only_the_exa_module(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sentinel = cast(worker.AgentReachExecutionApi, object())
    calls: list[dict[str, object]] = []

    def validate(**kwargs: object) -> worker.AgentReachExecutionApi:
        calls.append(kwargs)
        return sentinel

    monkeypatch.setattr(worker, "validate_agent_reach_execution_contract", validate)

    assert worker._load_execution_api() is sentinel
    assert calls == [{"runtime_module": "exa"}]


def test_real_worker_module_rejects_empty_input_without_output() -> None:
    completed = subprocess.run(
        [sys.executable, "-I", "-m", "hermes_reach.sources.exa_worker"],
        input=b"",
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 1
    assert completed.stdout == b""
    assert completed.stderr == b""
