"""Shared cleanup semantics for isolated source workers."""

from __future__ import annotations

import sys
from collections.abc import Awaitable
from tempfile import TemporaryDirectory


async def cleanup_worker_resources(
    process_cleanup: Awaitable[None] | None,
    temporary: TemporaryDirectory[str] | None,
) -> None:
    """Clean both resources without replacing an exception already in flight."""

    active_exception = sys.exception()
    cleanup_error: Exception | None = None

    if process_cleanup is not None:
        try:
            await process_cleanup
        except Exception as error:
            cleanup_error = error

    if temporary is not None:
        try:
            temporary.cleanup()
        except Exception as error:
            if cleanup_error is None:
                cleanup_error = error

    if active_exception is None and cleanup_error is not None:
        raise cleanup_error


__all__ = ["cleanup_worker_resources"]
