"""Fixed, framed invocation of the pinned ``bili-cli`` Click entry point."""

from __future__ import annotations

import io
import json
import re
import sys
from collections.abc import Callable, Mapping
from contextlib import redirect_stdout
from dataclasses import dataclass
from importlib import import_module
from importlib.metadata import version
from typing import BinaryIO, Final, Literal, cast
from urllib.parse import urlsplit

from ..agent_reach_bridge import (
    BILIBILI_CLI_DISTRIBUTION,
    BILIBILI_CLI_VERSION,
)

WorkerOperation = Literal["search.videos", "read.video", "browse.hot", "browse.rank"]

PROTOCOL_VERSION: Final = "v1"
MAX_REQUEST_BYTES: Final = 32 * 1024
MAX_OUTPUT_BYTES: Final = 512 * 1024
MAX_JSON_DEPTH: Final = 12
MAX_JSON_ITEMS: Final = 64
MAX_STRING_BYTES: Final = 64 * 1024
MAX_QUERY_CHARACTERS: Final = 4096
MAX_URL_CHARACTERS: Final = 128
MAX_LIMIT: Final = 50
_BVID: Final = re.compile(r"BV[A-Za-z0-9]{10}")


class BilibiliProtocolError(Exception):
    """A redacted worker protocol or backend-envelope failure."""


@dataclass(frozen=True, slots=True)
class WorkerRequest:
    operation: WorkerOperation
    query: str | None = None
    url: str | None = None
    limit: int | None = None


class _BoundedTextSink(io.StringIO):
    def __init__(self, maximum_bytes: int = MAX_OUTPUT_BYTES) -> None:
        super().__init__()
        self._maximum_bytes = maximum_bytes
        self._size = 0

    def write(self, value: str) -> int:
        if not isinstance(value, str):
            raise TypeError("worker_output_invalid")
        size = len(value.encode("utf-8", errors="strict"))
        if self._size + size > self._maximum_bytes:
            raise BilibiliProtocolError("worker_output_too_large")
        self._size += size
        return super().write(value)


def encode_request(
    operation: WorkerOperation,
    *,
    query: str | None = None,
    url: str | None = None,
    limit: int | None = None,
) -> bytes:
    """Encode one closed operation request for the isolated worker."""

    request = _validated_request(
        {
            "protocol_version": PROTOCOL_VERSION,
            "operation": operation,
            **({"query": query} if query is not None else {}),
            **({"url": url} if url is not None else {}),
            **({"limit": limit} if limit is not None else {}),
        }
    )
    payload: dict[str, object] = {
        "protocol_version": PROTOCOL_VERSION,
        "operation": request.operation,
    }
    if request.query is not None:
        payload["query"] = request.query
    if request.url is not None:
        payload["url"] = request.url
    if request.limit is not None:
        payload["limit"] = request.limit
    return _encode_frame(payload, MAX_REQUEST_BYTES)


def decode_response(value: bytes) -> Mapping[str, object]:
    """Decode and revalidate the worker's bounded upstream envelope."""

    loaded = _decode_frame(value, MAX_OUTPUT_BYTES)
    return _validated_backend_envelope(loaded)


def _read_request(stream: BinaryIO) -> WorkerRequest:
    header = stream.read(4)
    if len(header) != 4:
        raise BilibiliProtocolError("worker_request_invalid")
    length = int.from_bytes(header, "big")
    if not 0 < length <= MAX_REQUEST_BYTES:
        raise BilibiliProtocolError("worker_request_invalid")
    payload = stream.read(length)
    if len(payload) != length or stream.read(1):
        raise BilibiliProtocolError("worker_request_invalid")
    return _validated_request(_load_json(payload))


def _validated_request(value: object) -> WorkerRequest:
    if not isinstance(value, dict):
        raise BilibiliProtocolError("worker_request_invalid")
    operation = value.get("operation")
    if value.get("protocol_version") != PROTOCOL_VERSION or operation not in {
        "search.videos",
        "read.video",
        "browse.hot",
        "browse.rank",
    }:
        raise BilibiliProtocolError("worker_request_invalid")
    if operation == "search.videos":
        if set(value) != {"protocol_version", "operation", "query", "limit"}:
            raise BilibiliProtocolError("worker_request_invalid")
        query = _bounded_text(value.get("query"), MAX_QUERY_CHARACTERS)
        limit = _bounded_limit(value.get("limit"))
        return WorkerRequest(cast(WorkerOperation, operation), query=query, limit=limit)
    if operation == "read.video":
        if set(value) != {"protocol_version", "operation", "url"}:
            raise BilibiliProtocolError("worker_request_invalid")
        url = _bounded_text(value.get("url"), MAX_URL_CHARACTERS)
        if not _valid_video_url(url):
            raise BilibiliProtocolError("worker_request_invalid")
        return WorkerRequest(cast(WorkerOperation, operation), url=url)
    if set(value) != {"protocol_version", "operation", "limit"}:
        raise BilibiliProtocolError("worker_request_invalid")
    return WorkerRequest(
        cast(WorkerOperation, operation), limit=_bounded_limit(value.get("limit"))
    )


def _argv(request: WorkerRequest) -> tuple[str, ...]:
    if request.operation == "search.videos":
        if request.query is None or request.limit is None:
            raise BilibiliProtocolError("worker_request_invalid")
        return (
            "search",
            "--type",
            "video",
            "--max",
            str(request.limit),
            "--json",
            "--",
            request.query,
        )
    if request.operation == "read.video":
        if request.url is None:
            raise BilibiliProtocolError("worker_request_invalid")
        return ("video", request.url, "--json")
    if request.limit is None:
        raise BilibiliProtocolError("worker_request_invalid")
    if request.operation == "browse.hot":
        return ("hot", "--max", str(request.limit), "--json")
    if request.operation == "browse.rank":
        return ("rank", "--max", str(request.limit), "--json")
    raise BilibiliProtocolError("worker_request_invalid")


def _invoke_cli(
    request: WorkerRequest,
    module_loader: Callable[[str], object] = import_module,
    version_reader: Callable[[str], str] = version,
) -> Mapping[str, object]:
    if version_reader(BILIBILI_CLI_DISTRIBUTION) != BILIBILI_CLI_VERSION:
        raise BilibiliProtocolError("backend_version_invalid")
    module = module_loader("bili_cli.cli")
    cli = getattr(module, "cli", None)
    main = getattr(cli, "main", None)
    if not callable(main):
        raise BilibiliProtocolError("backend_entry_point_invalid")

    sink = _BoundedTextSink()
    exited = False
    with redirect_stdout(sink):
        try:
            cast(Callable[..., object], main)(
                args=list(_argv(request)),
                prog_name="bili",
                standalone_mode=False,
            )
        except SystemExit as error:
            if error.code != 1:
                raise BilibiliProtocolError("backend_invocation_invalid") from None
            exited = True
    envelope = _validated_backend_envelope(
        _load_json(sink.getvalue().encode("utf-8", errors="strict"))
    )
    if (envelope["ok"] is False) != exited:
        raise BilibiliProtocolError("backend_exit_invalid")
    return envelope


def _validated_backend_envelope(value: object) -> Mapping[str, object]:
    if not _json_within_bounds(value) or not isinstance(value, dict):
        raise BilibiliProtocolError("backend_envelope_invalid")
    if value.get("schema_version") != "1" or type(value.get("ok")) is not bool:
        raise BilibiliProtocolError("backend_envelope_invalid")
    if value["ok"] is True:
        if set(value) != {"ok", "schema_version", "data"}:
            raise BilibiliProtocolError("backend_envelope_invalid")
    else:
        if set(value) != {"ok", "schema_version", "error"}:
            raise BilibiliProtocolError("backend_envelope_invalid")
        error = value.get("error")
        if (
            not isinstance(error, dict)
            or not {"code", "message"} <= set(error) <= {"code", "message", "details"}
            or not isinstance(error.get("code"), str)
            or not isinstance(error.get("message"), str)
        ):
            raise BilibiliProtocolError("backend_envelope_invalid")
    return cast(Mapping[str, object], value)


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
        raise BilibiliProtocolError("worker_frame_invalid") from None
    if not 0 < len(payload) <= maximum:
        raise BilibiliProtocolError("worker_frame_invalid")
    return len(payload).to_bytes(4, "big") + payload


def _decode_frame(value: bytes, maximum: int) -> object:
    if len(value) < 5:
        raise BilibiliProtocolError("worker_frame_invalid")
    length = int.from_bytes(value[:4], "big")
    if not 0 < length <= maximum or len(value) != length + 4:
        raise BilibiliProtocolError("worker_frame_invalid")
    return _load_json(value[4:])


def _load_json(value: bytes) -> object:
    if not 0 < len(value) <= MAX_OUTPUT_BYTES:
        raise BilibiliProtocolError("worker_json_invalid")
    try:
        return json.loads(
            value.decode("utf-8", errors="strict"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (
        BilibiliProtocolError,
        UnicodeError,
        json.JSONDecodeError,
        RecursionError,
    ):
        raise BilibiliProtocolError("worker_json_invalid") from None


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise BilibiliProtocolError("worker_json_invalid")
        result[key] = value
    return result


def _reject_constant(_: str) -> object:
    raise BilibiliProtocolError("worker_json_invalid")


def _json_within_bounds(value: object, depth: int = 0) -> bool:
    if depth > MAX_JSON_DEPTH:
        return False
    if value is None or type(value) in {bool, int}:
        return not isinstance(value, int) or -(1 << 53) < value < (1 << 53)
    if isinstance(value, str):
        return len(value.encode("utf-8", errors="strict")) <= MAX_STRING_BYTES
    if isinstance(value, list):
        return len(value) <= MAX_JSON_ITEMS and all(
            _json_within_bounds(item, depth + 1) for item in value
        )
    if isinstance(value, dict):
        return len(value) <= MAX_JSON_ITEMS and all(
            isinstance(key, str)
            and len(key) <= 64
            and _json_within_bounds(item, depth + 1)
            for key, item in value.items()
        )
    return False


def _bounded_text(value: object, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise BilibiliProtocolError("worker_request_invalid")
    return value.strip()


def _bounded_limit(value: object) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= MAX_LIMIT
    ):
        raise BilibiliProtocolError("worker_request_invalid")
    return value


def _valid_video_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    parts = parsed.path.split("/")
    return bool(
        parsed.scheme == "https"
        and parsed.netloc == "www.bilibili.com"
        and not parsed.query
        and not parsed.fragment
        and len(parts) == 3
        and parts[1] == "video"
        and _BVID.fullmatch(parts[2])
    )


def main() -> int:
    try:
        request = _read_request(sys.stdin.buffer)
        response = _invoke_cli(request)
        sys.stdout.buffer.write(_encode_frame(response, MAX_OUTPUT_BYTES))
        sys.stdout.buffer.flush()
        return 0
    except (Exception, KeyboardInterrupt, SystemExit):
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
