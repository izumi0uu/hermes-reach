"""Hermes plugin registration without importing Hermes implementation modules."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Final

from .agent_reach_bridge import load_agent_reach_catalog
from .cli import reach_command, register_cli
from .schemas import (
    REACH_BROWSE,
    REACH_READ,
    REACH_SEARCH,
    REACH_STATUS,
    REACH_TRANSCRIBE,
)
from .tools import (
    reach_browse,
    reach_read,
    reach_search,
    reach_status,
    reach_transcribe,
)

ToolHandler = Callable[[dict[str, object]], str | Awaitable[str]]

_TOOLS: Final[tuple[tuple[str, dict[str, object], ToolHandler, bool], ...]] = (
    ("reach_search", REACH_SEARCH, reach_search, True),
    ("reach_read", REACH_READ, reach_read, True),
    ("reach_browse", REACH_BROWSE, reach_browse, True),
    ("reach_transcribe", REACH_TRANSCRIBE, reach_transcribe, True),
    ("reach_status", REACH_STATUS, reach_status, False),
)


def register(ctx: Any) -> None:
    """Register the fixed public tool and operator CLI surface."""

    load_agent_reach_catalog()
    for name, schema, handler, is_async in _TOOLS:
        description = schema["description"]
        ctx.register_tool(
            name=name,
            toolset="reach",
            schema=schema,
            handler=handler,
            is_async=is_async,
            description=description if isinstance(description, str) else "",
        )
    ctx.register_cli_command(
        name="reach",
        help="Inspect and configure Hermes Reach capabilities",
        setup_fn=register_cli,
        handler_fn=reach_command,
        description="Read-only source capability discovery and future setup namespace.",
    )
