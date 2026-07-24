"""Typed adapter registration without source-specific execution code."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Final, Literal

from ..catalog import DataScope, get_operation, get_source
from .availability import Availability, AvailabilityRecord
from .policy import AuthorizedCall, scope_includes

FailureClass = Literal[
    "transient",
    "invalid_input",
    "not_found",
    "authentication",
    "authorization",
    "policy",
    "rate_limit",
    "permanent",
]
_FAILURE_CLASSES: frozenset[str] = frozenset(
    {
        "transient",
        "invalid_input",
        "not_found",
        "authentication",
        "authorization",
        "policy",
        "rate_limit",
        "permanent",
    }
)
ItemKind = Literal["content", "entry", "topic", "reply", "profile", "result"]
_ITEM_KINDS: Final[frozenset[str]] = frozenset(
    {"content", "entry", "topic", "reply", "profile", "result"}
)


@dataclass(frozen=True, slots=True)
class RawItem:
    """A typed adapter item before response bounds are applied."""

    text: str
    native_id: str | None = None
    kind: ItemKind = "content"
    title: str | None = None
    url: str | None = None
    author: str | None = None
    published_at: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in _ITEM_KINDS:
            raise ValueError("Adapter items must use a known item kind.")


@dataclass(frozen=True, slots=True)
class AdapterResult:
    """A successful or classified failed adapter attempt."""

    items: tuple[RawItem, ...] = ()
    failure_class: FailureClass | None = None
    partial_failure_class: FailureClass | None = None

    def __post_init__(self) -> None:
        if self.failure_class is not None and self.items:
            raise ValueError("Failed adapter results cannot contain raw items.")
        if self.failure_class is not None and self.partial_failure_class is not None:
            raise ValueError("A result cannot be both failed and partial.")
        if self.partial_failure_class is not None and not self.items:
            raise ValueError("Partial adapter results must contain usable items.")
        if (
            self.failure_class is not None
            and self.failure_class not in _FAILURE_CLASSES
        ):
            raise ValueError("Adapter results must use a known failure classification.")
        if (
            self.partial_failure_class is not None
            and self.partial_failure_class not in _FAILURE_CLASSES
        ):
            raise ValueError("Partial results must use a known failure classification.")

    @property
    def is_success(self) -> bool:
        """Return whether the adapter produced a successful result."""

        return self.failure_class is None


AdapterCallable = Callable[[AuthorizedCall], Awaitable[AdapterResult]]
CancelCallable = Callable[[], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class AdapterBinding:
    """One deterministic candidate for a catalog operation."""

    source: str
    operation: str
    backend_id: str
    backend_version: str
    priority: int
    required_scope: DataScope
    equivalence_group: str
    execute: AdapterCallable
    cancel: CancelCallable | None = None


class AdapterRegistry:
    """Own adapter binding ownership and deterministic candidate order."""

    def __init__(self) -> None:
        self._bindings: dict[tuple[str, str], list[AdapterBinding]] = {}
        self._states: dict[tuple[str, str], AvailabilityRecord] = {}

    def register(self, binding: AdapterBinding) -> None:
        """Register a binding only for an existing catalog operation."""

        source = get_source(binding.source)
        operation = (
            get_operation(source, binding.operation) if source is not None else None
        )
        if operation is None:
            raise ValueError("Adapter bindings must reference a catalog operation.")
        if not scope_includes(operation.runtime.data_scope, binding.required_scope):
            raise ValueError("Adapter bindings cannot widen a catalog data scope.")
        key = (binding.source, binding.operation)
        bindings = self._bindings.setdefault(key, [])
        if any(existing.backend_id == binding.backend_id for existing in bindings):
            raise ValueError("A backend may bind a catalog operation only once.")
        bindings.append(binding)
        bindings.sort(key=lambda item: (item.priority, item.backend_id))
        self._states.pop(key, None)

    def mark(
        self,
        source_name: str,
        operation_name: str,
        state: Availability,
        reason: str,
    ) -> None:
        """Record an explicit non-executable operation state."""

        source = get_source(source_name)
        operation = (
            get_operation(source, operation_name) if source is not None else None
        )
        if operation is None:
            raise ValueError("Availability must reference a catalog operation.")
        if state == "available":
            raise ValueError("Available operations require an executable binding.")
        key = (source_name, operation_name)
        if self._bindings.get(key):
            raise ValueError("A bound operation cannot have a non-executable state.")
        self._states[key] = AvailabilityRecord(state, reason)

    def candidates(self, call: AuthorizedCall) -> tuple[AdapterBinding, ...]:
        """Return stable candidates without evaluating availability or health."""

        return tuple(
            self._bindings.get((call.call.source.name, call.operation.name), ())
        )

    def has_binding(self, source: str, operation: str) -> bool:
        """Return whether a catalog operation has any registered backend."""

        return bool(self._bindings.get((source, operation)))

    def availability(self, source_name: str, operation_name: str) -> AvailabilityRecord:
        """Return local state without probing a backend or environment."""

        key = (source_name, operation_name)
        bindings = self._bindings.get(key, ())
        if bindings:
            binding = bindings[0]
            return AvailabilityRecord(
                "available",
                "A registered read-only adapter is executable on request.",
                binding.backend_id,
                binding.backend_version,
            )
        explicit = self._states.get(key)
        if explicit is not None:
            return explicit
        source = get_source(source_name)
        operation = (
            get_operation(source, operation_name) if source is not None else None
        )
        if operation is None:
            return AvailabilityRecord(
                "unavailable", "The source operation is not in the catalog."
            )
        if operation.implementation_state == "planned":
            return AvailabilityRecord("unavailable", operation.unavailable_reason)
        return AvailabilityRecord(
            "unavailable", "The implemented operation has no registered adapter."
        )
