"""Local source-operation availability without active health probes."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Availability = Literal["available", "setup_required", "degraded", "unavailable"]


@dataclass(frozen=True, slots=True)
class AvailabilityRecord:
    """A safe local projection for one source-operation."""

    state: Availability
    reason: str
    backend_id: str | None = None
    backend_version: str | None = None
