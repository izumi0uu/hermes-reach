"""Isolated invocation of the pinned Agent-Reach YouTube runtime."""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date
from typing import BinaryIO, Final, Literal, Protocol, cast
from urllib.parse import parse_qs, urlsplit

from ..agent_reach_bridge import (
    YTDLP_VERSION,
    AgentReachExecutionApi,
    validate_agent_reach_execution_contract,
)

WorkerOperation = Literal["search.videos", "read.video", "read.subtitles"]

PROTOCOL_VERSION: Final = "v1"
MAX_REQUEST_BYTES: Final = 32 * 1024
MAX_OUTPUT_BYTES: Final = 512 * 1024
MAX_JSON_DEPTH: Final = 10
MAX_JSON_ITEMS: Final = 64
MAX_JSON_NODES: Final = 1024
MAX_STRING_BYTES: Final = 256 * 1024
MAX_QUERY_CHARACTERS: Final = 4096
MAX_URL_CHARACTERS: Final = 128
MAX_LANGUAGE_CHARACTERS: Final = 32
MAX_LIMIT: Final = 50
MAX_TEXT_CHARACTERS: Final = 16_000
MAX_TITLE_CHARACTERS: Final = 4_096
MAX_AUTHOR_CHARACTERS: Final = 1_024
MAX_SUBTITLE_TEXT_BYTES: Final = 256 * 1024
MAX_NORMALIZED_INTEGER: Final = (1 << 53) - 1
EXPECTED_SOURCE: Final = "youtube"
EXPECTED_BACKEND_ID: Final = "yt-dlp"
EXPECTED_BACKEND_VERSION: Final = YTDLP_VERSION
VIDEO_SCHEMA: Final = "youtube.video.v1"
SUBTITLE_SCHEMA: Final = "youtube.subtitle.v1"
_VIDEO_ID: Final = re.compile(r"[A-Za-z0-9_-]{11}")
_LANGUAGE: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,31}")
_ERROR_CODES: Final = frozenset(
    {
        "setup_required",
        "not_found",
        "authentication",
        "authorization",
        "rate_limit",
        "transient",
        "permanent",
    }
)
_FORK_ERROR_CODES: Final = frozenset(
    {
        "unsupported_protocol_version",
        "invalid_request",
        "unsupported_source",
        "unsupported_operation",
        "host_capability_missing",
        "backend_unavailable",
        "backend_incompatible",
        "deadline_exceeded",
        "cancelled",
        "invalid_input",
        "not_found",
        "authentication",
        "authorization",
        "rate_limit",
        "transient",
        "permanent",
        "backend_contract_violation",
    }
)
_FORK_VIDEO_ITEM_FIELDS: Final = (
    "text",
    "native_id",
    "title",
    "url",
    "author",
    "published_at",
    "duration_seconds",
    "view_count",
    "comment_count",
)
_FORK_SUBTITLE_ITEM_FIELDS: Final = (
    "text",
    "native_id",
    "title",
    "url",
    "language",
    "origin",
)


class YouTubeProtocolError(Exception):
    """A request, backend projection, framing, or bounds failure."""


@dataclass(frozen=True, slots=True)
class WorkerRequest:
    operation: WorkerOperation
    query: str | None = None
    url: str | None = None
    limit: int | None = None
    language: str | None = None


class _ExecutionItem(Protocol):
    schema_id: object
    fields: object


class _ExecutionSuccess(Protocol):
    protocol_version: object
    source: object
    operation: object
    backend_id: object
    backend_version: object
    items: object
    truncated: object
    partial_error_code: object


class _ExecutionFailure(Protocol):
    protocol_version: object
    source: object
    operation: object
    backend_id: object
    backend_version: object
    error_code: object


ExecutionApiProvider = Callable[[], AgentReachExecutionApi]


def _load_execution_api() -> AgentReachExecutionApi:
    return validate_agent_reach_execution_contract(runtime_module="youtube")


def encode_request(
    operation: WorkerOperation,
    *,
    query: str | None = None,
    url: str | None = None,
    limit: int | None = None,
    language: str | None = None,
) -> bytes:
    """Encode one closed request without placing its values in process argv."""

    value: dict[str, object] = {
        "protocol_version": PROTOCOL_VERSION,
        "operation": operation,
    }
    if query is not None:
        value["query"] = query
    if url is not None:
        value["url"] = url
    if limit is not None:
        value["limit"] = limit
    if language is not None:
        value["language"] = language
    request = _validated_request(value)
    return _encode_frame(_request_data(request), MAX_REQUEST_BYTES)


def decode_response(value: bytes) -> Mapping[str, object]:
    """Decode and revalidate the worker's closed result projection."""

    return _validated_backend_envelope(_decode_frame(value, MAX_OUTPUT_BYTES))


def _read_request(stream: BinaryIO) -> WorkerRequest:
    header = stream.read(4)
    if len(header) != 4:
        raise YouTubeProtocolError("worker_request_invalid")
    length = int.from_bytes(header, "big")
    if not 0 < length <= MAX_REQUEST_BYTES:
        raise YouTubeProtocolError("worker_request_invalid")
    payload = stream.read(length)
    if len(payload) != length or stream.read(1):
        raise YouTubeProtocolError("worker_request_invalid")
    return _validated_request(_load_json(payload, MAX_REQUEST_BYTES))


def _validated_request(value: object) -> WorkerRequest:
    if not isinstance(value, dict):
        raise YouTubeProtocolError("worker_request_invalid")
    operation = value.get("operation")
    if value.get("protocol_version") != PROTOCOL_VERSION or operation not in {
        "search.videos",
        "read.video",
        "read.subtitles",
    }:
        raise YouTubeProtocolError("worker_request_invalid")
    if operation == "search.videos":
        if set(value) != {"protocol_version", "operation", "query", "limit"}:
            raise YouTubeProtocolError("worker_request_invalid")
        return WorkerRequest(
            "search.videos",
            query=_bounded_request_text(value.get("query"), MAX_QUERY_CHARACTERS),
            limit=_bounded_limit(value.get("limit")),
        )
    allowed = {"protocol_version", "operation", "url"}
    if operation == "read.subtitles":
        allowed.add("language")
    if set(value) not in ({"protocol_version", "operation", "url"}, allowed):
        raise YouTubeProtocolError("worker_request_invalid")
    url = _bounded_request_text(value.get("url"), MAX_URL_CHARACTERS)
    if not _valid_video_url(url):
        raise YouTubeProtocolError("worker_request_invalid")
    language = value.get("language")
    if language is not None and (
        not isinstance(language, str) or _LANGUAGE.fullmatch(language) is None
    ):
        raise YouTubeProtocolError("worker_request_invalid")
    return WorkerRequest(cast(WorkerOperation, operation), url=url, language=language)


def _request_data(request: WorkerRequest) -> Mapping[str, object]:
    value: dict[str, object] = {
        "protocol_version": PROTOCOL_VERSION,
        "operation": request.operation,
    }
    if request.query is not None:
        value["query"] = request.query
    if request.url is not None:
        value["url"] = request.url
    if request.limit is not None:
        value["limit"] = request.limit
    if request.language is not None:
        value["language"] = request.language
    return value


def _execute_request(
    request: WorkerRequest,
    *,
    execution_api_provider: ExecutionApiProvider | None = None,
) -> Mapping[str, object]:
    provider = (
        execution_api_provider
        if execution_api_provider is not None
        else _load_execution_api
    )
    try:
        api = provider()
    except Exception:
        return _error_response(request.operation, "setup_required")

    try:
        arguments, expected_id, maximum_items = _execution_arguments(request)
        request_factory = cast(Callable[..., object], api.execution_request_type)
        network_factory = cast(Callable[..., object], api.network_access_type)
        limits_factory = cast(Callable[..., object], api.execution_limits_type)
        context_factory = cast(Callable[..., object], api.execution_context_type)
        execution_request = request_factory(
            PROTOCOL_VERSION,
            EXPECTED_SOURCE,
            request.operation,
            arguments,
        )
        network_access = network_factory()
        limits = limits_factory(
            maximum_items=maximum_items,
            maximum_text_characters=MAX_TEXT_CHARACTERS,
        )
        host_capabilities: tuple[object, ...] = (network_access,)
        if request.operation == "read.subtitles":
            private_workspace_factory = cast(
                Callable[..., object],
                api.private_workspace_type,
            )
            host_capabilities = (
                network_access,
                private_workspace_factory(),
            )
        context = context_factory(host_capabilities, limits=limits)
        result = api.execute(execution_request, context)

        if type(result) is api.execution_success_type:
            return _fork_success_response(
                request,
                cast(_ExecutionSuccess, result),
                api,
                expected_id=expected_id,
            )

        if type(result) is api.execution_failure_type:
            failure = cast(_ExecutionFailure, result)
            error_code = failure.error_code
            if (
                _exact_text(failure.protocol_version, PROTOCOL_VERSION)
                and _exact_text(failure.source, EXPECTED_SOURCE)
                and _exact_text(failure.operation, request.operation)
                and _exact_text(failure.backend_id, EXPECTED_BACKEND_ID)
                and _exact_text(
                    failure.backend_version,
                    EXPECTED_BACKEND_VERSION,
                )
                and type(error_code) is str
                and error_code in _FORK_ERROR_CODES
            ):
                return _error_response(
                    request.operation,
                    _translate_fork_error(error_code),
                )
    except Exception:
        return _error_response(request.operation, "permanent")
    return _error_response(request.operation, "permanent")


def _execution_arguments(
    request: WorkerRequest,
) -> tuple[dict[str, object], str | None, int]:
    if request.operation == "search.videos":
        if request.query is None or request.limit is None:
            raise YouTubeProtocolError("worker_request_invalid")
        return {"query": request.query, "limit": request.limit}, None, request.limit
    if request.url is None:
        raise YouTubeProtocolError("worker_request_invalid")
    expected_id = _video_id_from_url(request.url)
    arguments: dict[str, object] = {"url": _canonical_url(expected_id)}
    if request.operation == "read.subtitles":
        arguments["language"] = request.language
    return arguments, expected_id, 1


def _fork_success_response(
    request: WorkerRequest,
    success: _ExecutionSuccess,
    api: AgentReachExecutionApi,
    *,
    expected_id: str | None,
) -> Mapping[str, object]:
    items = success.items
    if (
        not _exact_text(success.protocol_version, PROTOCOL_VERSION)
        or not _exact_text(success.source, EXPECTED_SOURCE)
        or not _exact_text(success.operation, request.operation)
        or not _exact_text(success.backend_id, EXPECTED_BACKEND_ID)
        or not _exact_text(success.backend_version, EXPECTED_BACKEND_VERSION)
        or success.partial_error_code is not None
        or type(success.truncated) is not bool
        or type(items) is not tuple
        or any(type(item) is not api.execution_item_type for item in items)
    ):
        raise YouTubeProtocolError("backend_data_invalid")

    if request.operation == "search.videos":
        if request.limit is None or len(items) > request.limit:
            raise YouTubeProtocolError("backend_data_invalid")
        projected = [_project_fork_search_item(item) for item in items]
        identities = tuple(item["id"] for item in projected)
        if len(set(identities)) != len(identities):
            raise YouTubeProtocolError("backend_identity_invalid")
        return _success_response(request.operation, projected)

    if len(items) != 1 or expected_id is None:
        raise YouTubeProtocolError("backend_data_invalid")
    item = cast(_ExecutionItem, items[0])
    if request.operation == "read.video":
        if not _exact_text(item.schema_id, VIDEO_SCHEMA):
            raise YouTubeProtocolError("backend_data_invalid")
        projected_video = _project_fork_video_fields(
            item.fields,
            expected_id=expected_id,
        )
        return _success_response(
            request.operation,
            {"item": projected_video, "truncated": success.truncated},
        )
    if not _exact_text(item.schema_id, SUBTITLE_SCHEMA):
        raise YouTubeProtocolError("backend_data_invalid")
    projected_subtitle = _project_fork_subtitle_fields(
        item.fields,
        expected_id=expected_id,
        truncated=success.truncated,
    )
    return _success_response(request.operation, projected_subtitle)


def _project_fork_search_item(value: object) -> dict[str, object]:
    item = cast(_ExecutionItem, value)
    if not _exact_text(item.schema_id, VIDEO_SCHEMA):
        raise YouTubeProtocolError("backend_data_invalid")
    projected = _project_fork_video_fields(item.fields, expected_id=None)
    return {
        "id": projected["native_id"],
        "title": projected["title"],
        "description": projected["text"],
        "uploader": projected["author"],
        "duration_seconds": projected["duration_seconds"],
        "view_count": projected["view_count"],
        "comment_count": projected["comment_count"],
        "upload_date": projected["published_at"],
        "url": projected["url"],
    }


def _translate_fork_error(error_code: str) -> str:
    if error_code in {"backend_unavailable", "backend_incompatible"}:
        return "setup_required"
    if error_code in {
        "not_found",
        "authentication",
        "authorization",
        "rate_limit",
        "transient",
        "permanent",
    }:
        return error_code
    return "permanent"


def _project_fork_video_fields(
    value: object,
    *,
    expected_id: str | None,
) -> dict[str, object]:
    item = _copied_closed_mapping(value, _FORK_VIDEO_ITEM_FIELDS)
    native_id = _fork_video_id(item["native_id"])
    url = item["url"]
    if (
        (expected_id is not None and native_id != expected_id)
        or type(url) is not str
        or url != _canonical_url(native_id)
    ):
        raise YouTubeProtocolError("backend_identity_invalid")
    return {
        "text": _fork_required_text(
            item["text"],
            maximum_characters=MAX_TEXT_CHARACTERS,
        ),
        "native_id": native_id,
        "title": _fork_required_text(
            item["title"],
            maximum_characters=MAX_TITLE_CHARACTERS,
            maximum_bytes=1024,
        ),
        "url": _canonical_url(native_id),
        "author": _fork_optional_text(
            item["author"],
            maximum_characters=MAX_AUTHOR_CHARACTERS,
            maximum_bytes=1024,
        ),
        "published_at": _fork_published_at(item["published_at"]),
        "duration_seconds": _fork_optional_integer(item["duration_seconds"]),
        "view_count": _fork_optional_integer(item["view_count"]),
        "comment_count": _fork_optional_integer(item["comment_count"]),
    }


def _project_fork_subtitle_fields(
    value: object,
    *,
    expected_id: str,
    truncated: bool,
) -> dict[str, object]:
    item = _copied_closed_mapping(value, _FORK_SUBTITLE_ITEM_FIELDS)
    native_id = _fork_video_id(item["native_id"])
    url = item["url"]
    language = item["language"]
    origin = item["origin"]
    text = _fork_required_text(
        item["text"],
        maximum_characters=MAX_TEXT_CHARACTERS,
    )
    if (
        native_id != expected_id
        or type(url) is not str
        or url != _canonical_url(native_id)
        or type(language) is not str
        or _LANGUAGE.fullmatch(language) is None
        or origin not in {"manual", "automatic"}
        or len(text.encode("utf-8", errors="strict")) > MAX_SUBTITLE_TEXT_BYTES
        or not text.lstrip("\ufeff\r\n ").startswith("WEBVTT")
    ):
        raise YouTubeProtocolError("backend_subtitle_invalid")
    return {
        "id": native_id,
        "title": _fork_required_text(
            item["title"],
            maximum_characters=MAX_TITLE_CHARACTERS,
            maximum_bytes=1024,
        ),
        "language": language,
        "origin": origin,
        "text": text,
        "truncated": truncated,
        "url": url,
    }


def _success_response(operation: WorkerOperation, data: object) -> Mapping[str, object]:
    return _validated_backend_envelope(
        {
            "protocol_version": PROTOCOL_VERSION,
            "operation": operation,
            "ok": True,
            "data": data,
        }
    )


def _error_response(operation: WorkerOperation, code: str) -> Mapping[str, object]:
    return _validated_backend_envelope(
        {
            "protocol_version": PROTOCOL_VERSION,
            "operation": operation,
            "ok": False,
            "error": {"code": code},
        }
    )


def _validated_backend_envelope(value: object) -> Mapping[str, object]:
    if not _json_within_bounds(value) or not isinstance(value, dict):
        raise YouTubeProtocolError("backend_envelope_invalid")
    operation = value.get("operation")
    if (
        value.get("protocol_version") != PROTOCOL_VERSION
        or operation not in {"search.videos", "read.video", "read.subtitles"}
        or type(value.get("ok")) is not bool
    ):
        raise YouTubeProtocolError("backend_envelope_invalid")
    if value["ok"] is False:
        if set(value) != {"protocol_version", "operation", "ok", "error"}:
            raise YouTubeProtocolError("backend_envelope_invalid")
        error = value.get("error")
        if (
            not isinstance(error, dict)
            or set(error) != {"code"}
            or error.get("code") not in _ERROR_CODES
        ):
            raise YouTubeProtocolError("backend_envelope_invalid")
        return cast(Mapping[str, object], value)
    if set(value) != {"protocol_version", "operation", "ok", "data"}:
        raise YouTubeProtocolError("backend_envelope_invalid")
    data = value.get("data")
    if operation == "search.videos":
        if not isinstance(data, list) or len(data) > MAX_LIMIT:
            raise YouTubeProtocolError("backend_data_invalid")
        for item in data:
            _validated_video_data(item)
    elif operation == "read.video":
        _validated_fork_video_data(data)
    else:
        _validated_subtitle_data(data)
    return cast(Mapping[str, object], value)


def _validated_video_data(value: object) -> Mapping[str, object]:
    item = _closed_mapping(
        value,
        {
            "id",
            "title",
            "description",
            "uploader",
            "duration_seconds",
            "view_count",
            "comment_count",
            "upload_date",
            "url",
        },
    )
    video_id = _video_id(item.get("id"))
    if item.get("url") != _canonical_url(video_id):
        raise YouTubeProtocolError("backend_identity_invalid")
    _fork_required_text(
        item.get("title"),
        maximum_characters=MAX_TITLE_CHARACTERS,
        maximum_bytes=1024,
    )
    _fork_optional_text(
        item.get("description"),
        maximum_characters=MAX_TEXT_CHARACTERS,
    )
    _fork_optional_text(
        item.get("uploader"),
        maximum_characters=MAX_AUTHOR_CHARACTERS,
        maximum_bytes=1024,
    )
    for name in ("duration_seconds", "view_count", "comment_count"):
        _fork_optional_integer(item.get(name))
    _fork_published_at(item.get("upload_date"))
    return item


def _validated_fork_video_data(value: object) -> Mapping[str, object]:
    result = _closed_mapping(value, {"item", "truncated"})
    if type(result.get("truncated")) is not bool:
        raise YouTubeProtocolError("backend_data_invalid")
    item = _copied_closed_mapping(result.get("item"), _FORK_VIDEO_ITEM_FIELDS)
    expected_id = _fork_video_id(item["native_id"])
    _project_fork_video_fields(item, expected_id=expected_id)
    return result


def _validated_subtitle_data(value: object) -> Mapping[str, object]:
    item = _closed_mapping(
        value,
        {"id", "title", "language", "origin", "text", "truncated", "url"},
    )
    video_id = _video_id(item.get("id"))
    if item.get("url") != _canonical_url(video_id):
        raise YouTubeProtocolError("backend_identity_invalid")
    _fork_required_text(
        item.get("title"),
        maximum_characters=MAX_TITLE_CHARACTERS,
        maximum_bytes=1024,
    )
    language = item.get("language")
    if not isinstance(language, str) or _LANGUAGE.fullmatch(language) is None:
        raise YouTubeProtocolError("backend_subtitle_language_invalid")
    if item.get("origin") not in {"manual", "automatic"}:
        raise YouTubeProtocolError("backend_subtitle_origin_invalid")
    text = item.get("text")
    if (
        not isinstance(text, str)
        or len(text.encode("utf-8", errors="strict")) > MAX_SUBTITLE_TEXT_BYTES
        or not text.lstrip("\ufeff\r\n ").startswith("WEBVTT")
        or type(item.get("truncated")) is not bool
    ):
        raise YouTubeProtocolError("backend_subtitle_invalid")
    return item


def _encode_frame(value: Mapping[str, object], maximum: int) -> bytes:
    try:
        payload = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8", errors="strict")
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise YouTubeProtocolError("worker_frame_invalid") from None
    if not 0 < len(payload) <= maximum:
        raise YouTubeProtocolError("worker_frame_invalid")
    return len(payload).to_bytes(4, "big") + payload


def _decode_frame(value: bytes, maximum: int) -> object:
    if len(value) < 5:
        raise YouTubeProtocolError("worker_frame_invalid")
    length = int.from_bytes(value[:4], "big")
    if not 0 < length <= maximum or len(value) != length + 4:
        raise YouTubeProtocolError("worker_frame_invalid")
    return _load_json(value[4:], maximum)


def _load_json(value: bytes, maximum: int) -> object:
    if not 0 < len(value) <= maximum:
        raise YouTubeProtocolError("worker_json_invalid")
    try:
        return json.loads(
            value.decode("utf-8", errors="strict"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (
        YouTubeProtocolError,
        UnicodeError,
        json.JSONDecodeError,
        RecursionError,
    ):
        raise YouTubeProtocolError("worker_json_invalid") from None


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise YouTubeProtocolError("worker_json_invalid")
        result[key] = value
    return result


def _reject_constant(_: str) -> object:
    raise YouTubeProtocolError("worker_json_invalid")


def _json_within_bounds(value: object) -> bool:
    budget = [MAX_JSON_NODES]

    def bounded(item: object, depth: int) -> bool:
        budget[0] -= 1
        if budget[0] < 0 or depth > MAX_JSON_DEPTH:
            return False
        if item is None or type(item) in {bool, int}:
            return not isinstance(item, int) or -MAX_NORMALIZED_INTEGER <= item <= (
                MAX_NORMALIZED_INTEGER
            )
        if isinstance(item, str):
            return len(item.encode("utf-8", errors="strict")) <= MAX_STRING_BYTES
        if isinstance(item, list):
            return len(item) <= MAX_JSON_ITEMS and all(
                bounded(child, depth + 1) for child in item
            )
        if isinstance(item, dict):
            return len(item) <= MAX_JSON_ITEMS and all(
                isinstance(key, str) and len(key) <= 64 and bounded(child, depth + 1)
                for key, child in item.items()
            )
        return False

    try:
        return bounded(value, 0)
    except UnicodeError:
        return False


def _mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise YouTubeProtocolError("backend_data_invalid")
    return cast(Mapping[str, object], value)


def _closed_mapping(value: object, fields: set[str]) -> Mapping[str, object]:
    item = _mapping(value)
    if set(item) != fields:
        raise YouTubeProtocolError("backend_data_invalid")
    return item


def _copied_closed_mapping(
    value: object,
    fields: tuple[str, ...],
) -> dict[str, object]:
    try:
        if not isinstance(value, Mapping):
            raise YouTubeProtocolError("backend_data_invalid")
        names = tuple(value)
        if (
            len(names) != len(fields)
            or set(names) != set(fields)
            or any(type(name) is not str for name in names)
        ):
            raise YouTubeProtocolError("backend_data_invalid")
        return {name: value[name] for name in fields}
    except YouTubeProtocolError:
        raise
    except Exception:
        raise YouTubeProtocolError("backend_data_invalid") from None


def _bounded_request_text(value: object, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise YouTubeProtocolError("worker_request_invalid")
    return value.strip()


def _bounded_limit(value: object) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= MAX_LIMIT
    ):
        raise YouTubeProtocolError("worker_request_invalid")
    return value


def _fork_optional_integer(value: object) -> int | None:
    if value is None:
        return None
    if type(value) is not int or not 0 <= value <= MAX_NORMALIZED_INTEGER:
        raise YouTubeProtocolError("backend_integer_invalid")
    return value


def _fork_optional_text(
    value: object,
    *,
    maximum_characters: int,
    maximum_bytes: int | None = None,
) -> str | None:
    if value is None:
        return None
    if (
        type(value) is not str
        or not 0 < len(value) <= maximum_characters
        or _contains_invalid_scalar(value)
    ):
        raise YouTubeProtocolError("backend_text_invalid")
    try:
        encoded_size = len(value.encode("utf-8", errors="strict"))
    except UnicodeError:
        raise YouTubeProtocolError("backend_text_invalid") from None
    if maximum_bytes is not None and encoded_size > maximum_bytes:
        raise YouTubeProtocolError("backend_text_invalid")
    return value


def _fork_required_text(
    value: object,
    *,
    maximum_characters: int,
    maximum_bytes: int | None = None,
) -> str:
    text = _fork_optional_text(
        value,
        maximum_characters=maximum_characters,
        maximum_bytes=maximum_bytes,
    )
    if text is None:
        raise YouTubeProtocolError("backend_text_invalid")
    return text


def _fork_published_at(value: object) -> str | None:
    if value is None:
        return None
    if type(value) is not str or len(value) != 10 or not value.isascii():
        raise YouTubeProtocolError("backend_date_invalid")
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        raise YouTubeProtocolError("backend_date_invalid") from None
    if parsed.year < 1970 or parsed.isoformat() != value:
        raise YouTubeProtocolError("backend_date_invalid")
    return value


def _contains_invalid_scalar(value: str) -> bool:
    return any(
        character == "\x00" or 0xD800 <= ord(character) <= 0xDFFF for character in value
    )


def _video_id(value: object) -> str:
    if not isinstance(value, str) or _VIDEO_ID.fullmatch(value) is None:
        raise YouTubeProtocolError("backend_identity_invalid")
    return value


def _fork_video_id(value: object) -> str:
    if type(value) is not str or _VIDEO_ID.fullmatch(value) is None:
        raise YouTubeProtocolError("backend_identity_invalid")
    return value


def _video_id_from_url(value: str) -> str:
    parsed = urlsplit(value)
    query = parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True)
    return _video_id(query["v"][0])


def _valid_video_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        query = parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True)
    except (KeyError, ValueError):
        return False
    return bool(
        parsed.scheme == "https"
        and parsed.netloc == "www.youtube.com"
        and parsed.path == "/watch"
        and not parsed.fragment
        and set(query) == {"v"}
        and len(query["v"]) == 1
        and _VIDEO_ID.fullmatch(query["v"][0])
    )


def _canonical_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


def _exact_text(value: object, expected: str) -> bool:
    return type(value) is str and value == expected


def main() -> int:
    try:
        request = _read_request(sys.stdin.buffer)
    except (Exception, KeyboardInterrupt, SystemExit):
        return 1
    response = _execute_request(request)
    try:
        frame = _encode_frame(response, MAX_OUTPUT_BYTES)
    except YouTubeProtocolError:
        frame = _encode_frame(
            _error_response(request.operation, "permanent"), MAX_OUTPUT_BYTES
        )
    try:
        sys.stdout.buffer.write(frame)
        sys.stdout.buffer.flush()
    except (BrokenPipeError, OSError):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
