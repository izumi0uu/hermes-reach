from __future__ import annotations

import asyncio
from dataclasses import asdict, replace

from hermes_reach.catalog import OperationRuntimeSpec, get_operation, get_source
from hermes_reach.contracts import OperationCall
from hermes_reach.runtime.adapters import (
    AdapterBinding,
    AdapterResult,
    MediaMetadata,
    RawItem,
)
from hermes_reach.runtime.policy import AuthorizedCall
from hermes_reach.runtime.runner import BoundedRunner


def _authorized(
    source_name: str = "github",
    operation_name: str = "search.repositories",
    *,
    effective_scope: str = "public",
    runtime: OperationRuntimeSpec | None = None,
) -> AuthorizedCall:
    source = get_source(source_name)
    assert source is not None
    operation = get_operation(source, operation_name)
    assert operation is not None
    if runtime is not None:
        operation = replace(operation, runtime=runtime)
    return AuthorizedCall(
        OperationCall(source, operation, {}, query="not-recorded"),
        effective_scope,  # type: ignore[arg-type]
        "policy-v1",
    )


def _binding(
    execute: object,
    *,
    backend_id: str = "primary",
    source: str = "github",
    operation: str = "search.repositories",
    required_scope: str = "public",
    equivalence_group: str = "same-operation",
    cancel: object | None = None,
    retry_owner: str = "runner",
) -> AdapterBinding:
    return AdapterBinding(
        source=source,
        operation=operation,
        backend_id=backend_id,
        backend_version="1.0",
        priority=10,
        required_scope=required_scope,  # type: ignore[arg-type]
        equivalence_group=equivalence_group,
        execute=execute,  # type: ignore[arg-type]
        cancel=cancel,  # type: ignore[arg-type]
        retry_owner=retry_owner,  # type: ignore[arg-type]
    )


def test_runner_bounds_items_and_characters_without_retaining_excess() -> None:
    async def execute(_: AuthorizedCall) -> AdapterResult:
        return AdapterResult(
            tuple(RawItem("x" * 16_001, f"id-{index}") for index in range(21))
        )

    result = asyncio.run(BoundedRunner().run(_authorized(), (_binding(execute),)))

    assert len(result.items) == 1
    item = result.items[0]
    assert (
        sum(
            len(value)
            for value in (item.kind, item.text, item.native_id)
            if value is not None
        )
        == 16_000
    )
    assert result.truncated is True
    assert [attempt.outcome for attempt in result.attempts] == ["success"]
    assert result.selected_backend_id == "primary"
    assert result.selected_backend_version == "1.0"


def test_runner_retries_once_after_a_transient_failure() -> None:
    invocations: list[str] = []

    async def execute(_: AuthorizedCall) -> AdapterResult:
        invocations.append("attempt")
        if len(invocations) == 1:
            return AdapterResult(failure_class="transient")
        return AdapterResult((RawItem("ok"),))

    result = asyncio.run(BoundedRunner().run(_authorized(), (_binding(execute),)))

    assert invocations == ["attempt", "attempt"]
    assert [attempt.outcome for attempt in result.attempts] == ["transient", "success"]
    assert result.items == (RawItem("ok"),)


def test_binding_owned_retry_prevents_outer_retry_and_fallback() -> None:
    invocations: list[str] = []

    async def transient(_: AuthorizedCall) -> AdapterResult:
        invocations.append("connector")
        return AdapterResult(failure_class="transient")

    async def fallback(_: AuthorizedCall) -> AdapterResult:
        invocations.append("fallback")
        return AdapterResult((RawItem("not-reached"),))

    result = asyncio.run(
        BoundedRunner().run(
            _authorized(),
            (
                _binding(transient, retry_owner="binding"),
                _binding(fallback, backend_id="fallback"),
            ),
        )
    )

    assert invocations == ["connector"]
    assert result.failure_class == "transient"
    assert [attempt.outcome for attempt in result.attempts] == ["transient"]


def test_non_retryable_failures_do_not_retry_or_fall_back() -> None:
    invocations: list[str] = []

    async def primary(_: AuthorizedCall) -> AdapterResult:
        invocations.append("primary")
        return AdapterResult(failure_class="authorization")

    async def fallback(_: AuthorizedCall) -> AdapterResult:
        invocations.append("fallback")
        return AdapterResult((RawItem("not-reached"),))

    result = asyncio.run(
        BoundedRunner().run(
            _authorized(),
            (_binding(primary), _binding(fallback, backend_id="fallback")),
        )
    )

    assert invocations == ["primary"]
    assert result.failure_class == "authorization"


def test_fallback_requires_same_operation_equivalence_and_non_broader_scope() -> None:
    invocations: list[str] = []

    async def transient(_: AuthorizedCall) -> AdapterResult:
        invocations.append("primary")
        return AdapterResult(failure_class="transient")

    async def successful(_: AuthorizedCall) -> AdapterResult:
        invocations.append("fallback")
        return AdapterResult((RawItem("fallback"),))

    call = _authorized("twitter", "browse.home", effective_scope="account_visible")
    allowed = _binding(
        successful,
        backend_id="public-fallback",
        source="twitter",
        operation="browse.home",
        required_scope="public",
    )
    primary = _binding(
        transient,
        source="twitter",
        operation="browse.home",
        required_scope="account_visible",
    )
    result = asyncio.run(BoundedRunner().run(call, (primary, allowed)))

    assert invocations == ["primary", "primary", "fallback"]
    assert result.items == (RawItem("fallback"),)

    invocations.clear()
    broader = replace(allowed, backend_id="broader", required_scope="account_visible")
    public_primary = replace(primary, required_scope="public")
    rejected = asyncio.run(BoundedRunner().run(call, (public_primary, broader)))

    assert invocations == ["primary", "primary"]
    assert rejected.failure_class == "transient"


def test_timeout_cancels_and_provenance_contains_no_raw_output() -> None:
    cancelled: list[bool] = []

    async def execute(_: AuthorizedCall) -> AdapterResult:
        await asyncio.Event().wait()
        return AdapterResult((RawItem("private-body"),))

    async def cancel() -> None:
        cancelled.append(True)

    runtime = OperationRuntimeSpec(
        attempt_timeout_seconds=0,  # type: ignore[arg-type]
        total_timeout_seconds=30,
    )
    result = asyncio.run(
        BoundedRunner().run(
            _authorized(runtime=runtime), (_binding(execute, cancel=cancel),)
        )
    )

    assert cancelled == [True, True]
    assert [attempt.outcome for attempt in result.attempts] == ["timeout", "timeout"]
    assert "private-body" not in str([asdict(attempt) for attempt in result.attempts])


def test_runner_treats_malformed_adapter_outcomes_as_transient() -> None:
    async def execute(_: AuthorizedCall) -> object:
        return "not-an-adapter-result"

    result = asyncio.run(BoundedRunner().run(_authorized(), (_binding(execute),)))

    assert result.failure_class == "transient"
    assert [attempt.outcome for attempt in result.attempts] == [
        "transient",
        "transient",
    ]


def test_runner_preserves_closed_item_fields_and_partial_state() -> None:
    async def execute(_: AuthorizedCall) -> AdapterResult:
        return AdapterResult(
            (
                RawItem(
                    "body",
                    "native",
                    kind="entry",
                    title="title",
                    url="https://example.com/item",
                    author="author",
                    published_at="2026-07-24T00:00:00+00:00",
                ),
            ),
            partial_failure_class="transient",
        )

    result = asyncio.run(BoundedRunner().run(_authorized(), (_binding(execute),)))

    assert result.partial_failure_class == "transient"
    assert result.selected_backend_id == "primary"
    assert result.items[0].kind == "entry"
    assert [attempt.outcome for attempt in result.attempts] == ["partial"]


def test_runner_budgets_closed_media_metadata_with_item_characters() -> None:
    async def execute(_: AuthorizedCall) -> AdapterResult:
        return AdapterResult(
            (
                RawItem(
                    "body",
                    kind="result",
                    media=MediaMetadata(
                        duration_seconds=3600,
                        view_count=2_000_000,
                        subtitle_language="en",
                        subtitle_origin="manual",
                        coverage="complete",
                    ),
                ),
            )
        )

    runtime = OperationRuntimeSpec(maximum_characters=12)
    result = asyncio.run(
        BoundedRunner().run(_authorized(runtime=runtime), (_binding(execute),))
    )

    assert result.truncated is True
    assert result.items[0].media is None
    assert result.items[0].text == "body"
