"""Stable, side-effect-free Reach Connector contracts."""

from .errors import (
    ConnectorError,
    ConnectorErrorCategory,
    ConnectorErrorCode,
    category_for_code,
    codes_for_category,
)

__all__ = [
    "ConnectorError",
    "ConnectorErrorCategory",
    "ConnectorErrorCode",
    "category_for_code",
    "codes_for_category",
]
