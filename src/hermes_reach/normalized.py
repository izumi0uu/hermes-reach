"""Shared character accounting for normalized local and Connector results."""

from __future__ import annotations

from typing import Final

NORMALIZED_MEDIA_VERSION: Final = "v1"
# Keep signed wire values exactly representable in interoperable JSON runtimes.
MAX_NORMALIZED_INTEGER: Final = (1 << 53) - 1


def media_metadata_characters(
    *,
    coverage: str,
    duration_seconds: int | None,
    view_count: int | None,
    comment_count: int | None,
    subtitle_language: str | None,
    subtitle_origin: str | None,
) -> int:
    """Count the exact scalar contribution of closed media metadata."""

    return sum(
        len(value)
        for value in (
            NORMALIZED_MEDIA_VERSION,
            coverage,
            subtitle_language,
            subtitle_origin,
            None if duration_seconds is None else str(duration_seconds),
            None if view_count is None else str(view_count),
            None if comment_count is None else str(comment_count),
        )
        if value is not None
    )


def normalized_item_characters(
    *,
    kind: str,
    text: str,
    native_id: str | None,
    title: str | None,
    url: str | None,
    author: str | None,
    published_at: str | None,
    media_characters: int,
) -> int:
    """Count one normalized item using the shared runner/wire budget."""

    return (
        sum(
            len(value)
            for value in (kind, text, native_id, title, url, author, published_at)
            if value is not None
        )
        + media_characters
    )
