"""Typed adapter registration without source-specific execution code."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal

from ..catalog import DataScope, get_operation, get_source
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


@dataclass(frozen=True, slots=True)
class RawItem:
    """A typed adapter item before response bounds are applied."""

    text: str
    native_id: str | None = None


@dataclass(frozen=True, slots=True)
class AdapterResult:
    """A successful or classified failed adapter attempt."""

    items: tuple[RawItem, ...] = ()
    failure_class: FailureClass | None = None

    def __post_init__(self) -> None:
        if self.failure_class is not None and self.items:
            raise ValueError("Failed adapter results cannot contain raw items.")
        if (
            self.failure_class is not None
            and self.failure_class not in _FAILURE_CLASSES
        ):
            raise ValueError("Adapter results must use a known failure classification.")

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

    def candidates(self, call: AuthorizedCall) -> tuple[AdapterBinding, ...]:
        """Return stable candidates without evaluating availability or health."""

        return tuple(
            self._bindings.get((call.call.source.name, call.operation.name), ())
        )

    def has_binding(self, source: str, operation: str) -> bool:
        """Return whether a catalog operation has any registered backend."""

        return bool(self._bindings.get((source, operation)))
