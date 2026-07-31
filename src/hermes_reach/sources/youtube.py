"""Production binding for the closed Agent-Reach YouTube runtime."""

from __future__ import annotations

import asyncio
import os
import re
import signal
import sys
import tempfile
from collections.abc import Mapping
from datetime import date
from pathlib import Path
from typing import Final, Protocol, cast

from ..agent_reach_bridge import YTDLP_VERSION
from ..normalized import MAX_NORMALIZED_INTEGER
from ..runtime.adapters import (
    AdapterResult,
    FailureClass,
    MediaMetadata,
    RawItem,
    SubtitleOrigin,
)
from ._worker_cleanup import cleanup_worker_resources
from .documents import normalize_whitespace
from .media import (
    YOUTUBE_OPERATIONS,
    AuditedYouTubeBackend,
    MediaBackendAttestation,
)
from .youtube_worker import (
    MAX_OUTPUT_BYTES,
    WorkerOperation,
    YouTubeProtocolError,
    decode_response,
    encode_request,
)

_WORKER_MODULE: Final = "hermes_reach.sources.youtube_worker"
_VIDEO_ID: Final = re.compile(r"[A-Za-z0-9_-]{11}")
_FORK_VIDEO_RESULT_FIELDS: Final = ("item", "truncated")
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
_MAX_FORK_TEXT_CHARACTERS: Final = 16_000
_MAX_FORK_TITLE_CHARACTERS: Final = 4_096
_MAX_FORK_TITLE_BYTES: Final = 1_024
_MAX_FORK_AUTHOR_CHARACTERS: Final = 1_024
_MAX_FORK_AUTHOR_BYTES: Final = 1_024
_ERROR_CLASSES: Final[Mapping[str, FailureClass]] = {
    "setup_required": "permanent",
    "not_found": "not_found",
    "authentication": "authentication",
    "authorization": "authorization",
    "rate_limit": "rate_limit",
    "transient": "transient",
    "permanent": "permanent",
}


class YouTubeWorkerError(Exception):
    """A classified worker failure containing no request or backend text."""

    def __init__(self, failure_class: FailureClass) -> None:
        super().__init__("youtube_worker_failed")
        self.failure_class = failure_class


class YouTubeWorkerClient(Protocol):
    async def execute(
        self,
        operation: WorkerOperation,
        *,
        query: str | None = None,
        url: str | None = None,
        limit: int | None = None,
        language: str | None = None,
    ) -> Mapping[str, object]: ...


class YouTubeWorker:
    """Supervise one fixed isolated Agent-Reach YouTube worker invocation."""

    async def execute(
        self,
        operation: WorkerOperation,
        *,
        query: str | None = None,
        url: str | None = None,
        limit: int | None = None,
        language: str | None = None,
    ) -> Mapping[str, object]:
        try:
            request = encode_request(
                operation,
                query=query,
                url=url,
                limit=limit,
                language=language,
            )
        except YouTubeProtocolError:
            raise YouTubeWorkerError("permanent") from None
        if not os.path.isabs(sys.executable):
            raise YouTubeWorkerError("permanent")

        process: asyncio.subprocess.Process | None = None
        temporary: tempfile.TemporaryDirectory[str] | None = None
        response_validated = False
        try:
            temporary = tempfile.TemporaryDirectory(prefix="hermes-reach-youtube-")
            root = Path(temporary.name)
            environment = _isolated_environment(root)
            try:
                process = await asyncio.create_subprocess_exec(
                    sys.executable,
                    "-I",
                    "-m",
                    _WORKER_MODULE,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                    cwd=str(root),
                    env=environment,
                    close_fds=True,
                    start_new_session=True,
                )
            except asyncio.CancelledError:
                raise
            except (OSError, ValueError):
                raise YouTubeWorkerError("transient") from None
            output = await _exchange_bounded(process, request)
            if process.returncode != 0:
                raise YouTubeWorkerError("transient")
            try:
                response = decode_response(output)
            except YouTubeProtocolError:
                raise YouTubeWorkerError("permanent") from None
            if response.get("operation") != operation:
                raise YouTubeWorkerError("permanent")
            response_validated = True
            return response
        except asyncio.CancelledError:
            raise
        except YouTubeWorkerError:
            raise
        except (BrokenPipeError, ChildProcessError, ConnectionError, OSError):
            raise YouTubeWorkerError("transient") from None
        except Exception:
            raise YouTubeWorkerError("transient") from None
        finally:
            await cleanup_worker_resources(
                (
                    _cleanup_process_group(
                        process,
                        terminate_group=not response_validated,
                    )
                    if process is not None
                    else None
                ),
                temporary,
            )


class ProductionYouTubeClient:
    """Project the worker's closed v1 envelope into media adapter types."""

    def __init__(self, worker: YouTubeWorkerClient | None = None) -> None:
        self._worker = worker if worker is not None else YouTubeWorker()

    async def search_videos(self, query: str, limit: int) -> AdapterResult:
        return await self._execute("search.videos", query=query, limit=limit)

    async def read_video(self, video_url: str) -> AdapterResult:
        return await self._execute("read.video", url=video_url)

    async def read_subtitles(
        self, video_url: str, language: str | None
    ) -> AdapterResult:
        return await self._execute("read.subtitles", url=video_url, language=language)

    async def _execute(
        self,
        operation: WorkerOperation,
        *,
        query: str | None = None,
        url: str | None = None,
        limit: int | None = None,
        language: str | None = None,
    ) -> AdapterResult:
        try:
            envelope = await self._worker.execute(
                operation,
                query=query,
                url=url,
                limit=limit,
                language=language,
            )
            return _project_envelope(
                operation,
                envelope,
                limit,
                requested_url=url,
            )
        except asyncio.CancelledError:
            raise
        except YouTubeWorkerError as error:
            return AdapterResult(failure_class=error.failure_class)
        except (TypeError, ValueError, YouTubeProtocolError):
            return AdapterResult(failure_class="permanent")
        except Exception:
            return AdapterResult(failure_class="transient")


def production_youtube_backend() -> AuditedYouTubeBackend:
    """Construct the reviewed default bundle without probing local state."""

    return AuditedYouTubeBackend(
        ProductionYouTubeClient(),
        MediaBackendAttestation(
            provider_id="yt-dlp",
            provider_version=YTDLP_VERSION,
            operations=YOUTUBE_OPERATIONS,
            logs_queries=False,
            persists_content=False,
            hidden_model_processing=False,
            runtime_dependency_install=False,
            reads_ambient_configuration=False,
            imports_credentials=False,
            imports_cookies=False,
            uses_proxy=False,
            uses_browser=False,
            uses_shell=False,
            delegates_to_ytdlp=False,
        ),
    )


def _project_envelope(
    operation: WorkerOperation,
    envelope: Mapping[str, object],
    limit: int | None,
    *,
    requested_url: str | None = None,
) -> AdapterResult:
    if (
        envelope.get("protocol_version") != "v1"
        or envelope.get("operation") != operation
        or type(envelope.get("ok")) is not bool
    ):
        raise YouTubeProtocolError("backend_envelope_invalid")
    if envelope.get("ok") is False:
        if set(envelope) != {"protocol_version", "operation", "ok", "error"}:
            raise YouTubeProtocolError("backend_envelope_invalid")
        error = envelope.get("error")
        if not isinstance(error, Mapping) or set(error) != {"code"}:
            raise YouTubeProtocolError("backend_error_invalid")
        code = error.get("code")
        failure_class = _ERROR_CLASSES.get(code) if isinstance(code, str) else None
        return AdapterResult(failure_class=failure_class or "permanent")
    if envelope.get("ok") is not True:
        raise YouTubeProtocolError("backend_envelope_invalid")
    if set(envelope) != {"protocol_version", "operation", "ok", "data"}:
        raise YouTubeProtocolError("backend_envelope_invalid")
    data = envelope.get("data")
    if operation == "search.videos":
        if limit is None or not isinstance(data, list) or len(data) > limit:
            raise YouTubeProtocolError("backend_data_invalid")
        return AdapterResult(tuple(_video_item(item, search=True) for item in data))
    if operation == "read.video":
        return _fork_video_result(data, requested_url=requested_url)
    if operation == "read.subtitles":
        item, truncated = _subtitle_item(data, requested_url=requested_url)
        return AdapterResult((item,), truncated=truncated)
    raise YouTubeProtocolError("backend_operation_invalid")


def _video_item(value: object, *, search: bool) -> RawItem:
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
    video_id, video_url = _video_identity(item)
    title = _required_text(item.get("title"))
    description = _optional_text(item.get("description"))
    return RawItem(
        text=description or title,
        native_id=video_id,
        kind="result" if search else "content",
        title=title,
        url=video_url,
        author=_optional_text(item.get("uploader")),
        published_at=_optional_text(item.get("upload_date")),
        media=MediaMetadata(
            duration_seconds=_optional_integer(item.get("duration_seconds")),
            view_count=_optional_integer(item.get("view_count")),
            comment_count=_optional_integer(item.get("comment_count")),
            coverage="partial" if search else "complete",
        ),
    )


def _fork_video_result(
    value: object,
    *,
    requested_url: str | None,
) -> AdapterResult:
    result = _copied_closed_mapping(value, _FORK_VIDEO_RESULT_FIELDS)
    truncated = result["truncated"]
    if type(truncated) is not bool:
        raise YouTubeProtocolError("backend_data_invalid")

    item = _copied_closed_mapping(result["item"], _FORK_VIDEO_ITEM_FIELDS)
    video_id = _fork_required_text(item["native_id"], maximum_characters=512)
    video_url = item["url"]
    expected_url = f"https://www.youtube.com/watch?v={video_id}"
    if (
        _VIDEO_ID.fullmatch(video_id) is None
        or type(video_url) is not str
        or video_url != expected_url
        or requested_url != expected_url
    ):
        raise YouTubeProtocolError("backend_identity_invalid")

    return AdapterResult(
        (
            RawItem(
                text=_fork_required_text(
                    item["text"],
                    maximum_characters=_MAX_FORK_TEXT_CHARACTERS,
                ),
                native_id=video_id,
                kind="content",
                title=_fork_required_text(
                    item["title"],
                    maximum_characters=_MAX_FORK_TITLE_CHARACTERS,
                    maximum_bytes=_MAX_FORK_TITLE_BYTES,
                ),
                url=video_url,
                author=_fork_optional_text(
                    item["author"],
                    maximum_characters=_MAX_FORK_AUTHOR_CHARACTERS,
                    maximum_bytes=_MAX_FORK_AUTHOR_BYTES,
                ),
                published_at=_fork_published_at(item["published_at"]),
                media=MediaMetadata(
                    duration_seconds=_fork_optional_integer(item["duration_seconds"]),
                    view_count=_fork_optional_integer(item["view_count"]),
                    comment_count=_fork_optional_integer(item["comment_count"]),
                    coverage="complete",
                ),
            ),
        ),
        truncated=truncated,
    )


def _subtitle_item(
    value: object,
    *,
    requested_url: str | None,
) -> tuple[RawItem, bool]:
    item = _closed_mapping(
        value,
        {"id", "title", "language", "origin", "text", "truncated", "url"},
    )
    truncated = item.get("truncated")
    origin = item.get("origin")
    if type(truncated) is not bool or origin not in {"manual", "automatic"}:
        raise YouTubeProtocolError("backend_subtitle_invalid")
    video_id, video_url = _video_identity(item)
    if requested_url != video_url:
        raise YouTubeProtocolError("backend_identity_invalid")
    return (
        RawItem(
            text=_required_raw_text(item.get("text")),
            native_id=video_id,
            kind="content",
            title=_required_text(item.get("title")),
            url=video_url,
            media=MediaMetadata(
                subtitle_language=_required_text(item.get("language")),
                subtitle_origin=cast(SubtitleOrigin, origin),
                coverage="complete",
            ),
        ),
        truncated,
    )


def _isolated_environment(root: Path) -> dict[str, str]:
    directories = {
        "HOME": root / "home",
        "XDG_CONFIG_HOME": root / "config",
        "XDG_CACHE_HOME": root / "cache",
        "XDG_DATA_HOME": root / "data",
        "DENO_DIR": root / "deno",
        "TMPDIR": root / "tmp",
    }
    for directory in directories.values():
        directory.mkdir(mode=0o700)
    return {
        **{name: str(path) for name, path in directories.items()},
        "PYTHONUTF8": "1",
        "PYTHONNOUSERSITE": "1",
        "YTDLP_NO_PLUGINS": "1",
        "LANG": "C",
        "LC_ALL": "C",
        "HTTP_PROXY": "",
        "HTTPS_PROXY": "",
        "ALL_PROXY": "",
        "NO_PROXY": "*",
        "http_proxy": "",
        "https_proxy": "",
        "all_proxy": "",
        "no_proxy": "*",
    }


async def _exchange_bounded(
    process: asyncio.subprocess.Process, request: bytes
) -> bytes:
    writer = process.stdin
    reader = process.stdout
    if writer is None or reader is None:
        raise YouTubeWorkerError("transient")
    writer.write(request)
    await writer.drain()
    writer.close()

    output = bytearray()
    try:
        while len(output) <= MAX_OUTPUT_BYTES + 4:
            chunk = await reader.read(min(8192, MAX_OUTPUT_BYTES + 5 - len(output)))
            if not chunk:
                break
            output.extend(chunk)
            if len(output) > MAX_OUTPUT_BYTES + 4:
                raise YouTubeWorkerError("permanent")
        await process.wait()
        return bytes(output)
    finally:
        output[:] = b"\x00" * len(output)


async def _cleanup_process_group(
    process: asyncio.subprocess.Process, *, terminate_group: bool
) -> None:
    if process.returncode is not None and not terminate_group:
        return
    if terminate_group:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError:
            if process.returncode is None:
                try:
                    process.kill()
                except OSError:
                    pass
    if process.returncode is not None:
        return
    try:
        await process.wait()
    except (BrokenPipeError, ChildProcessError, ConnectionError, OSError):
        pass


def _closed_mapping(value: object, fields: set[str]) -> Mapping[str, object]:
    if (
        not isinstance(value, Mapping)
        or set(value) != fields
        or not all(isinstance(key, str) for key in value)
    ):
        raise YouTubeProtocolError("backend_data_invalid")
    return cast(Mapping[str, object], value)


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


def _video_identity(item: Mapping[str, object]) -> tuple[str, str]:
    video_id = _required_text(item.get("id"))
    expected_url = f"https://www.youtube.com/watch?v={video_id}"
    if _VIDEO_ID.fullmatch(video_id) is None or item.get("url") != expected_url:
        raise YouTubeProtocolError("backend_identity_invalid")
    return video_id, expected_url


def _required_text(value: object) -> str:
    text = _optional_text(value)
    if text is None:
        raise YouTubeProtocolError("backend_text_invalid")
    return text


def _required_raw_text(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise YouTubeProtocolError("backend_text_invalid")
    return value


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise YouTubeProtocolError("backend_text_invalid")
    return normalize_whitespace(value) or None


def _optional_integer(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
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


def _fork_optional_integer(value: object) -> int | None:
    if value is None:
        return None
    if type(value) is not int or not 0 <= value <= MAX_NORMALIZED_INTEGER:
        raise YouTubeProtocolError("backend_integer_invalid")
    return value


def _contains_invalid_scalar(value: str) -> bool:
    return any(
        character == "\x00" or 0xD800 <= ord(character) <= 0xDFFF for character in value
    )
