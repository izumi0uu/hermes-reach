"""Isolated structured invocation of the pinned ``yt-dlp`` backend."""

from __future__ import annotations

import json
import os
import re
import stat
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date
from importlib import import_module
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import BinaryIO, Final, Literal, cast
from urllib.parse import parse_qs, urlsplit

from ..agent_reach_bridge import (
    DENO_DISTRIBUTION,
    DENO_VERSION,
    YTDLP_DISTRIBUTION,
    YTDLP_EJS_DISTRIBUTION,
    YTDLP_EJS_VERSION,
    YTDLP_VERSION,
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
MAX_SUBTITLE_FILE_BYTES: Final = 512 * 1024
MAX_SUBTITLE_TEXT_BYTES: Final = 256 * 1024
MAX_NORMALIZED_INTEGER: Final = (1 << 53) - 1
DEFAULT_SUBTITLE_LANGUAGES: Final = ("zh-Hans", "zh", "en")
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
_FIXED_USER_AGENT: Final = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)


class YouTubeProtocolError(Exception):
    """A request, backend projection, framing, or bounds failure."""


class YouTubeSetupError(Exception):
    """The exact local yt-dlp execution closure is unavailable."""


class YouTubeNotFoundError(Exception):
    """The reviewed backend produced no requested resource."""


@dataclass(frozen=True, slots=True)
class WorkerRequest:
    operation: WorkerOperation
    query: str | None = None
    url: str | None = None
    limit: int | None = None
    language: str | None = None


class _NullLogger:
    def debug(self, _message: object) -> None:
        return None

    def warning(self, _message: object) -> None:
        return None

    def error(self, _message: object) -> None:
        return None


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


def _invoke_backend(
    request: WorkerRequest,
    *,
    module_loader: Callable[[str], object] = import_module,
    version_reader: Callable[[str], str] = version,
    executable: str = sys.executable,
    root: Path | None = None,
) -> object:
    _require_versions(version_reader)
    deno = _deno_executable(executable)
    private_root = (root if root is not None else Path.cwd()).absolute()
    os.environ["YTDLP_NO_PLUGINS"] = "1"
    yt_dlp = module_loader("yt_dlp")
    yt_dlp_ejs = module_loader("yt_dlp_ejs")
    if getattr(yt_dlp_ejs, "version", None) != YTDLP_EJS_VERSION:
        raise YouTubeSetupError("backend_ejs_invalid")
    globals_module = module_loader("yt_dlp.globals")
    plugin_dirs = getattr(globals_module, "plugin_dirs", None)
    if plugin_dirs is None or not hasattr(plugin_dirs, "value"):
        raise YouTubeSetupError("backend_plugins_invalid")
    plugin_dirs.value = []
    youtube_dl = getattr(yt_dlp, "YoutubeDL", None)
    if not callable(youtube_dl):
        raise YouTubeSetupError("backend_entry_point_invalid")

    params = _common_options(deno)
    if request.operation == "read.subtitles":
        languages = (
            [request.language] if request.language else list(DEFAULT_SUBTITLE_LANGUAGES)
        )
        params.update(
            {
                "writesubtitles": True,
                "writeautomaticsub": True,
                "subtitleslangs": languages,
                "subtitlesformat": "vtt",
                "skip_download": True,
                "paths": {
                    "home": str(private_root),
                    "temp": str(private_root),
                    "subtitle": str(private_root),
                },
                "outtmpl": {
                    "default": str(private_root / "%(id)s.%(ext)s"),
                    "subtitle": str(private_root / "%(id)s.%(ext)s"),
                },
                "overwrites": True,
                "nopart": True,
                "max_filesize": MAX_SUBTITLE_FILE_BYTES,
                "buffersize": 16 * 1024,
                "http_chunk_size": 16 * 1024,
                "noresizebuffer": True,
                "progress_hooks": [_bounded_progress],
            }
        )
    downloader = cast(Callable[[dict[str, object]], object], youtube_dl)(params)
    try:
        extract_info = getattr(downloader, "extract_info", None)
        if not callable(extract_info):
            raise YouTubeSetupError("backend_entry_point_invalid")
        if request.operation == "search.videos":
            if request.query is None or request.limit is None:
                raise YouTubeProtocolError("worker_request_invalid")
            result = cast(Callable[..., object], extract_info)(
                f"ytsearch{request.limit}:{request.query}",
                download=False,
                ie_key="YoutubeSearch",
            )
            return _project_search(result, request.limit)
        if request.url is None:
            raise YouTubeProtocolError("worker_request_invalid")
        if request.operation == "read.video":
            result = cast(Callable[..., object], extract_info)(
                request.url,
                download=False,
                ie_key="Youtube",
            )
            return _project_video(result, expected_id=_video_id_from_url(request.url))
        result = cast(Callable[..., object], extract_info)(
            request.url,
            download=True,
            ie_key="Youtube",
        )
        return _project_subtitles(
            result,
            private_root,
            request.language,
            expected_id=_video_id_from_url(request.url),
        )
    finally:
        close = getattr(downloader, "close", None)
        if callable(close):
            cast(Callable[[], object], close)()


def _common_options(deno: Path) -> dict[str, object]:
    return {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "logger": _NullLogger(),
        "ignoreerrors": False,
        "cachedir": False,
        "proxy": "",
        "cookiefile": None,
        "cookiesfrombrowser": None,
        "usenetrc": False,
        "netrc_cmd": None,
        "username": None,
        "password": None,
        "http_headers": {"User-Agent": _FIXED_USER_AGENT},
        "mark_watched": False,
        "noplaylist": True,
        "postprocessors": [],
        "js_runtimes": {"deno": {"path": str(deno)}},
        "remote_components": [],
        "retries": 1,
        "fragment_retries": 1,
        "extractor_retries": 1,
        "socket_timeout": 10,
        "concurrent_fragment_downloads": 1,
    }


def _require_versions(version_reader: Callable[[str], str]) -> None:
    expected = {
        YTDLP_DISTRIBUTION: YTDLP_VERSION,
        YTDLP_EJS_DISTRIBUTION: YTDLP_EJS_VERSION,
        DENO_DISTRIBUTION: DENO_VERSION,
    }
    try:
        installed = {name: version_reader(name) for name in expected}
    except (PackageNotFoundError, ValueError):
        raise YouTubeSetupError("backend_dependency_missing") from None
    if installed != expected:
        raise YouTubeSetupError("backend_dependency_version_invalid")


def _deno_executable(executable: str) -> Path:
    scripts = Path(executable).absolute().parent
    candidate = scripts / ("deno.exe" if os.name == "nt" else "deno")
    try:
        details = candidate.lstat()
    except OSError:
        raise YouTubeSetupError("backend_deno_missing") from None
    if (
        not stat.S_ISREG(details.st_mode)
        or details.st_nlink != 1
        or not os.access(candidate, os.X_OK)
    ):
        raise YouTubeSetupError("backend_deno_invalid")
    return candidate


def _bounded_progress(status_value: object) -> None:
    if not isinstance(status_value, Mapping):
        raise YouTubeProtocolError("backend_progress_invalid")
    downloaded = status_value.get("downloaded_bytes")
    if downloaded is not None and (
        isinstance(downloaded, bool)
        or not isinstance(downloaded, int)
        or downloaded < 0
        or downloaded > MAX_SUBTITLE_FILE_BYTES
    ):
        raise YouTubeProtocolError("backend_subtitle_too_large")


def _project_search(value: object, limit: int) -> list[dict[str, object]]:
    info = _mapping(value)
    entries = info.get("entries")
    if not isinstance(entries, list) or len(entries) > limit:
        raise YouTubeProtocolError("backend_search_invalid")
    return [
        _project_video(entry, expected_id=None, search_result=True) for entry in entries
    ]


def _project_video(
    value: object,
    *,
    expected_id: str | None,
    search_result: bool = False,
) -> dict[str, object]:
    info = _mapping(value)
    video_id = _video_id(info.get("id"))
    if expected_id is not None and video_id != expected_id:
        raise YouTubeProtocolError("backend_identity_invalid")
    title = _required_projected_text(info.get("title"), 1024)
    description = _optional_projected_text(
        info.get("description"), 4096 if search_result else 64 * 1024
    )
    uploader_value = info.get("uploader")
    if uploader_value is None:
        uploader_value = info.get("channel")
    return {
        "id": video_id,
        "title": title,
        "description": description,
        "uploader": _optional_projected_text(uploader_value, 1024),
        "duration_seconds": _optional_integer(info.get("duration")),
        "view_count": _optional_integer(info.get("view_count")),
        "comment_count": _optional_integer(info.get("comment_count")),
        "upload_date": _upload_date(info.get("upload_date")),
        "url": _canonical_url(video_id),
    }


def _project_subtitles(
    value: object,
    root: Path,
    language: str | None,
    *,
    expected_id: str,
) -> dict[str, object]:
    info = _mapping(value)
    video_id = _video_id(info.get("id"))
    if video_id != expected_id:
        raise YouTubeProtocolError("backend_identity_invalid")
    requested_value = info.get("requested_subtitles")
    if not isinstance(requested_value, Mapping) or not requested_value:
        raise YouTubeNotFoundError("backend_subtitle_missing")
    requested = _mapping(requested_value)
    selected_language = _select_language(requested, language)
    selected = _mapping(requested.get(selected_language))
    if selected.get("ext") != "vtt":
        raise YouTubeProtocolError("backend_subtitle_format_invalid")
    filepath = selected.get("filepath")
    if not isinstance(filepath, str):
        raise YouTubeProtocolError("backend_subtitle_path_invalid")
    manual = info.get("subtitles")
    automatic = info.get("automatic_captions")
    if isinstance(manual, Mapping) and selected_language in manual:
        origin = "manual"
    elif isinstance(automatic, Mapping) and selected_language in automatic:
        origin = "automatic"
    else:
        raise YouTubeProtocolError("backend_subtitle_origin_invalid")
    text, truncated = _read_subtitle_file(Path(filepath), root)
    return {
        "id": video_id,
        "title": _required_projected_text(info.get("title"), 1024),
        "language": selected_language,
        "origin": origin,
        "text": text,
        "truncated": truncated,
        "url": _canonical_url(video_id),
    }


def _select_language(requested: Mapping[str, object], language: str | None) -> str:
    candidates = (language,) if language is not None else DEFAULT_SUBTITLE_LANGUAGES
    for candidate in candidates:
        if candidate in requested and candidate != "live_chat":
            return candidate
    raise YouTubeNotFoundError("backend_subtitle_missing")


def _read_subtitle_file(path: Path, root: Path) -> tuple[str, bool]:
    root = root.absolute()
    candidate = path.absolute()
    try:
        details = candidate.lstat()
        resolved = candidate.resolve(strict=True)
    except OSError:
        raise YouTubeProtocolError("backend_subtitle_path_invalid") from None
    if (
        not stat.S_ISREG(details.st_mode)
        or details.st_nlink != 1
        or resolved != candidate
        or not resolved.is_relative_to(root)
        or details.st_size > MAX_SUBTITLE_FILE_BYTES
    ):
        raise YouTubeProtocolError("backend_subtitle_path_invalid")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = -1
    try:
        descriptor = os.open(resolved, flags)
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or (opened.st_dev, opened.st_ino) != (details.st_dev, details.st_ino)
        ):
            raise YouTubeProtocolError("backend_subtitle_path_invalid")
        output = bytearray()
        while len(output) <= MAX_SUBTITLE_FILE_BYTES:
            chunk = os.read(
                descriptor,
                min(64 * 1024, MAX_SUBTITLE_FILE_BYTES + 1 - len(output)),
            )
            if not chunk:
                break
            output.extend(chunk)
        data = bytes(output)
    except OSError:
        raise YouTubeProtocolError("backend_subtitle_read_invalid") from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            candidate.unlink()
        except OSError:
            pass
    if len(data) > MAX_SUBTITLE_FILE_BYTES:
        raise YouTubeProtocolError("backend_subtitle_too_large")
    try:
        text = data.decode("utf-8", errors="strict")
    except UnicodeError:
        raise YouTubeProtocolError("backend_subtitle_encoding_invalid") from None
    if not text.lstrip("\ufeff\r\n ").startswith("WEBVTT"):
        raise YouTubeProtocolError("backend_subtitle_format_invalid")
    bounded, truncated = _truncate_utf8(text, MAX_SUBTITLE_TEXT_BYTES)
    return bounded, truncated


def _execute_request(
    request: WorkerRequest,
    module_loader: Callable[[str], object] = import_module,
    version_reader: Callable[[str], str] = version,
    executable: str = sys.executable,
    root: Path | None = None,
) -> Mapping[str, object]:
    try:
        data = _invoke_backend(
            request,
            module_loader=module_loader,
            version_reader=version_reader,
            executable=executable,
            root=root,
        )
    except YouTubeSetupError:
        return _error_response(request.operation, "setup_required")
    except YouTubeNotFoundError:
        return _error_response(request.operation, "not_found")
    except YouTubeProtocolError:
        return _error_response(request.operation, "permanent")
    except Exception as error:
        return _error_response(request.operation, _backend_error_code(error))
    return _success_response(request.operation, data)


def _backend_error_code(error: Exception) -> str:
    for current in _exception_chain(error):
        name = type(current).__name__
        status_code = getattr(current, "status", None)
        if name == "HTTPError" and isinstance(status_code, int):
            if status_code == 404:
                return "not_found"
            if status_code == 429:
                return "rate_limit"
            if status_code == 401:
                return "authentication"
            if status_code == 403:
                return "authorization"
        if name == "GeoRestrictedError":
            return "authorization"
        if name in {
            "TransportError",
            "IncompleteRead",
            "TimeoutError",
            "ConnectionError",
            "SSLError",
            "SocketError",
        } or isinstance(current, OSError):
            return "transient"
    return "permanent"


def _exception_chain(error: Exception) -> tuple[BaseException, ...]:
    pending: list[BaseException] = [error]
    seen: set[int] = set()
    result: list[BaseException] = []
    while pending and len(result) < 16:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        result.append(current)
        if current.__cause__ is not None:
            pending.append(current.__cause__)
        if current.__context__ is not None:
            pending.append(current.__context__)
        exc_info = getattr(current, "exc_info", None)
        if (
            isinstance(exc_info, tuple)
            and len(exc_info) == 3
            and isinstance(exc_info[1], BaseException)
        ):
            pending.append(exc_info[1])
    return tuple(result)


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
        _validated_video_data(data)
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
    _required_projected_text(item.get("title"), 1024)
    _optional_projected_text(item.get("description"), 64 * 1024)
    _optional_projected_text(item.get("uploader"), 1024)
    for name in ("duration_seconds", "view_count", "comment_count"):
        _optional_integer(item.get(name))
    _upload_date(item.get("upload_date"))
    return item


def _validated_subtitle_data(value: object) -> Mapping[str, object]:
    item = _closed_mapping(
        value,
        {"id", "title", "language", "origin", "text", "truncated", "url"},
    )
    video_id = _video_id(item.get("id"))
    if item.get("url") != _canonical_url(video_id):
        raise YouTubeProtocolError("backend_identity_invalid")
    _required_projected_text(item.get("title"), 1024)
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


def _required_projected_text(value: object, maximum_bytes: int) -> str:
    text = _optional_projected_text(value, maximum_bytes)
    if text is None:
        raise YouTubeProtocolError("backend_text_invalid")
    return text


def _optional_projected_text(value: object, maximum_bytes: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise YouTubeProtocolError("backend_text_invalid")
    normalized = " ".join(value.split())
    if not normalized:
        return None
    return _truncate_utf8(normalized, maximum_bytes)[0]


def _truncate_utf8(value: str, maximum_bytes: int) -> tuple[str, bool]:
    encoded = value.encode("utf-8", errors="strict")
    if len(encoded) <= maximum_bytes:
        return value, False
    end = maximum_bytes
    while end > 0:
        try:
            return encoded[:end].decode("utf-8", errors="strict"), True
        except UnicodeDecodeError:
            end -= 1
    return "", True


def _optional_integer(value: object) -> int | None:
    if value is None:
        return None
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 0 <= value <= MAX_NORMALIZED_INTEGER
    ):
        raise YouTubeProtocolError("backend_integer_invalid")
    return value


def _upload_date(value: object) -> str | None:
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or len(value) != 8
        or not value.isascii()
        or not value.isdigit()
    ):
        raise YouTubeProtocolError("backend_date_invalid")
    year, month, day = int(value[:4]), int(value[4:6]), int(value[6:])
    try:
        parsed = date(year, month, day)
    except ValueError:
        raise YouTubeProtocolError("backend_date_invalid") from None
    if parsed.year < 1970:
        raise YouTubeProtocolError("backend_date_invalid")
    return parsed.isoformat()


def _video_id(value: object) -> str:
    if not isinstance(value, str) or _VIDEO_ID.fullmatch(value) is None:
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
