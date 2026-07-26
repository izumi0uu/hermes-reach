"""Bounded async execution and deterministic fallback for injected adapters."""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

from ..normalized import normalized_item_characters
from .adapters import AdapterBinding, AdapterResult, FailureClass, RawItem
from .policy import AuthorizedCall, scope_includes

_NON_RETRYABLE_FAILURES: Final[frozenset[FailureClass]] = frozenset(
    {
        "invalid_input",
        "not_found",
        "authentication",
        "authorization",
        "policy",
        "rate_limit",
        "permanent",
    }
)


@dataclass(frozen=True, slots=True)
class AttemptProvenance:
    """Safe execution facts with no adapter output or request values."""

    backend_id: str
    backend_version: str
    duration_ms: int
    outcome: str


@dataclass(frozen=True, slots=True)
class RunnerResult:
    """Bounded normalized items and redacted execution provenance."""

    items: tuple[RawItem, ...]
    truncated: bool
    attempts: tuple[AttemptProvenance, ...]
    failure_class: FailureClass | None = None
    partial_failure_class: FailureClass | None = None
    selected_backend_id: str | None = None
    selected_backend_version: str | None = None


class BoundedRunner:
    """Run only semantically equivalent authorized bindings within budgets."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock

    async def run(
        self, call: AuthorizedCall, bindings: tuple[AdapterBinding, ...]
    ) -> RunnerResult:
        """Execute one binding, retrying only one transient failure."""

        if not bindings:
            return RunnerResult((), False, (), "permanent")

        primary = bindings[0]
        start = self._clock()
        attempts: list[AttemptProvenance] = []
        result = await self._attempt(call, primary, start, attempts)
        if result.is_success:
            return self._bounded(result, call, primary, attempts)
        if result.failure_class in _NON_RETRYABLE_FAILURES:
            return RunnerResult((), False, tuple(attempts), result.failure_class)
        if primary.retry_owner == "binding":
            return RunnerResult((), False, tuple(attempts), result.failure_class)

        result = await self._attempt(call, primary, start, attempts)
        if result.is_success:
            return self._bounded(result, call, primary, attempts)
        if result.failure_class in _NON_RETRYABLE_FAILURES:
            return RunnerResult((), False, tuple(attempts), result.failure_class)

        for fallback in bindings[1:]:
            if not self._is_allowed_fallback(primary, fallback, call):
                continue
            result = await self._attempt(call, fallback, start, attempts)
            if result.is_success:
                return self._bounded(result, call, fallback, attempts)
            if result.failure_class in _NON_RETRYABLE_FAILURES:
                return RunnerResult((), False, tuple(attempts), result.failure_class)
        return RunnerResult((), False, tuple(attempts), "transient")

    async def _attempt(
        self,
        call: AuthorizedCall,
        binding: AdapterBinding,
        start: float,
        attempts: list[AttemptProvenance],
    ) -> AdapterResult:
        elapsed = self._clock() - start
        remaining = call.operation.runtime.total_timeout_seconds - elapsed
        if remaining <= 0:
            attempts.append(self._attempt_record(binding, None, "timeout"))
            return AdapterResult(failure_class="transient")
        timeout = min(call.operation.runtime.attempt_timeout_seconds, remaining)
        attempt_start = self._clock()
        try:
            result = await asyncio.wait_for(binding.execute(call), timeout=timeout)
        except TimeoutError:
            await self._cancel(binding)
            attempts.append(self._attempt_record(binding, attempt_start, "timeout"))
            return AdapterResult(failure_class="transient")
        except asyncio.CancelledError:
            await self._cancel(binding)
            raise
        except Exception:
            attempts.append(self._attempt_record(binding, attempt_start, "transient"))
            return AdapterResult(failure_class="transient")
        if not isinstance(result, AdapterResult):
            attempts.append(self._attempt_record(binding, attempt_start, "transient"))
            return AdapterResult(failure_class="transient")
        outcome = (
            "partial"
            if result.partial_failure_class is not None
            else "success"
            if result.is_success
            else result.failure_class or "permanent"
        )
        attempts.append(self._attempt_record(binding, attempt_start, outcome))
        return result

    def _attempt_record(
        self, binding: AdapterBinding, started_at: float | None, outcome: str
    ) -> AttemptProvenance:
        duration_ms = (
            0
            if started_at is None
            else max(0, int((self._clock() - started_at) * 1000))
        )
        return AttemptProvenance(
            backend_id=binding.backend_id,
            backend_version=binding.backend_version,
            duration_ms=duration_ms,
            outcome=outcome,
        )

    async def _cancel(self, binding: AdapterBinding) -> None:
        if binding.cancel is not None:
            with contextlib.suppress(Exception):
                await binding.cancel()

    def _is_allowed_fallback(
        self,
        primary: AdapterBinding,
        fallback: AdapterBinding,
        call: AuthorizedCall,
    ) -> bool:
        return (
            fallback.source == primary.source
            and fallback.operation == primary.operation
            and fallback.equivalence_group == primary.equivalence_group
            and scope_includes(primary.required_scope, fallback.required_scope)
            and scope_includes(call.effective_scope, fallback.required_scope)
        )

    def _bounded(
        self,
        result: AdapterResult,
        call: AuthorizedCall,
        binding: AdapterBinding,
        attempts: list[AttemptProvenance],
    ) -> RunnerResult:
        limit = call.operation.runtime.maximum_items
        character_limit = call.operation.runtime.maximum_characters
        selected = result.items[:limit]
        normalized: list[RawItem] = []
        used_characters = 0
        truncated = result.truncated or len(result.items) > len(selected)
        for item in selected:
            remaining = character_limit - used_characters
            if remaining < len(item.kind):
                truncated = True
                break
            bounded, item_truncated = self._bounded_item(item, remaining)
            normalized.append(bounded)
            used_characters += self._item_characters(bounded)
            truncated = truncated or item_truncated
        return RunnerResult(
            tuple(normalized),
            truncated,
            tuple(attempts),
            partial_failure_class=result.partial_failure_class,
            selected_backend_id=binding.backend_id,
            selected_backend_version=binding.backend_version,
        )

    def _bounded_item(self, item: RawItem, remaining: int) -> tuple[RawItem, bool]:
        truncated = False
        kind = item.kind
        remaining = max(0, remaining - len(kind))

        def identity(value: str | None, maximum: int) -> str | None:
            nonlocal remaining, truncated
            if value is None:
                return None
            if len(value) > maximum or len(value) > remaining:
                truncated = True
                return None
            remaining -= len(value)
            return value

        def display(value: str | None, maximum: int) -> str | None:
            nonlocal remaining, truncated
            if value is None or remaining <= 0:
                if value:
                    truncated = True
                return None
            bounded = value[: min(maximum, remaining)]
            if bounded != value:
                truncated = True
            remaining -= len(bounded)
            return bounded

        native_id = identity(item.native_id, 512)
        url = identity(item.url, 4096)
        title = display(item.title, 512)
        author = display(item.author, 256)
        published_at = display(item.published_at, 128)
        media = item.media
        if media is not None:
            media_characters = media.character_count()
            if media_characters > remaining:
                media = None
                truncated = True
            else:
                remaining -= media_characters
        text = item.text[:remaining]
        if text != item.text:
            truncated = True
        return (
            RawItem(
                text=text,
                native_id=native_id,
                kind=kind,
                title=title,
                url=url,
                author=author,
                published_at=published_at,
                media=media,
            ),
            truncated,
        )

    @staticmethod
    def _item_characters(item: RawItem) -> int:
        return normalized_item_characters(
            kind=item.kind,
            text=item.text,
            native_id=item.native_id,
            title=item.title,
            url=item.url,
            author=item.author,
            published_at=item.published_at,
            media_characters=(
                0 if item.media is None else item.media.character_count()
            ),
        )
