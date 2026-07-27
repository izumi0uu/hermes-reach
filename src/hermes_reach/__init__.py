"""Hermes Reach entry-point module."""

from __future__ import annotations

from typing import Any


def register(ctx: Any) -> None:
    """Load the plugin implementation only when Hermes registers it."""

    from .plugin import register as register_plugin

    register_plugin(ctx)


__all__ = ["register"]
