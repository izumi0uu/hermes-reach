"""Public JSON schemas shown to Hermes agents for the five Reach tools."""

from __future__ import annotations

from typing import Final

_PROTOCOL_VERSION: Final[dict[str, object]] = {
    "type": "string",
    "enum": ["v1"],
    "description": "Optional protocol version; defaults to v1.",
}

_SOURCE: Final[dict[str, object]] = {
    "type": "string",
    "description": "Canonical source ID from reach_status.",
}

_OPERATION: Final[dict[str, object]] = {
    "type": "string",
    "description": "Registered operation ID for the selected source and tool.",
}

_OPTIONS: Final[dict[str, object]] = {
    "type": "object",
    "description": "Closed source-operation options validated against the catalog.",
}

_TARGET_PROPERTIES: Final[dict[str, object]] = {
    "url": {"type": "string"},
    "native_id": {"type": "string"},
    "resource_ref": {"type": "string"},
}

_TARGET_ONE_OF: Final[list[dict[str, list[str]]]] = [
    {"required": ["url"]},
    {"required": ["native_id"]},
    {"required": ["resource_ref"]},
]

_TARGET: Final[dict[str, object]] = {
    "type": "object",
    "additionalProperties": False,
    "properties": _TARGET_PROPERTIES,
    "oneOf": _TARGET_ONE_OF,
}

_TRANSCRIBE_TARGET: Final[dict[str, object]] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {**_TARGET_PROPERTIES, "local_file": {"type": "string"}},
    "oneOf": [*_TARGET_ONE_OF, {"required": ["local_file"]}],
}

REACH_SEARCH: Final[dict[str, object]] = {
    "name": "reach_search",
    "description": (
        "Search one to five explicit registered sources without global ranking."
    ),
    "parameters": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "protocol_version": _PROTOCOL_VERSION,
            "requests": {
                "type": "array",
                "minItems": 1,
                "maxItems": 5,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "source": _SOURCE,
                        "operation": _OPERATION,
                        "query": {"type": "string", "minLength": 1, "maxLength": 4096},
                        "options": _OPTIONS,
                    },
                    "required": ["source", "operation", "query"],
                },
            },
        },
        "required": ["requests"],
    },
}


def _single_schema(
    name: str, description: str, target: dict[str, object] | None
) -> dict[str, object]:
    properties: dict[str, object] = {
        "protocol_version": _PROTOCOL_VERSION,
        "source": _SOURCE,
        "operation": _OPERATION,
        "options": _OPTIONS,
    }
    required = ["source", "operation"]
    if target is not None:
        properties["target"] = target
        required.append("target")
    return {
        "name": name,
        "description": description,
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": properties,
            "required": required,
        },
    }


REACH_READ: Final[dict[str, object]] = _single_schema(
    "reach_read",
    "Read a registered source resource by its explicit routing target.",
    _TARGET,
)
REACH_BROWSE: Final[dict[str, object]] = _single_schema(
    "reach_browse", "Browse a registered source-native collection.", None
)
REACH_TRANSCRIBE: Final[dict[str, object]] = _single_schema(
    "reach_transcribe",
    "Transcribe explicit media through a registered source operation.",
    _TRANSCRIBE_TARGET,
)
REACH_STATUS: Final[dict[str, object]] = {
    "name": "reach_status",
    "description": (
        "Inspect local Reach catalog availability without running source backends."
    ),
    "parameters": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "protocol_version": _PROTOCOL_VERSION,
            "sources": {"type": "array", "items": _SOURCE, "minItems": 1},
            "include_planned": {"type": "boolean", "default": True},
        },
    },
}

ALL_TOOL_SCHEMAS: Final[tuple[dict[str, object], ...]] = (
    REACH_SEARCH,
    REACH_READ,
    REACH_BROWSE,
    REACH_TRANSCRIBE,
    REACH_STATUS,
)
