"""Closed feedparser worker protocol for already-fetched RSS and Atom bytes."""

from __future__ import annotations

import importlib
import io
import ipaddress
import json
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import BinaryIO, Final, cast
from urllib.parse import urlsplit

from ..agent_reach_bridge import FEEDPARSER_VERSION

PROTOCOL_VERSION: Final = 1
MAX_FEED_BYTES: Final = 1_048_576
MAX_METADATA_BYTES: Final = 16_384
MAX_OUTPUT_BYTES: Final = 1_048_576
# One extra projection preserves the runner's existing overflow/truncation signal.
MAX_ENTRIES: Final = 21
MAX_CONTENT_TYPE_CHARACTERS: Final = 512
MAX_CONTENT_LOCATION_CHARACTERS: Final = 8192
MAX_TEXT_CHARACTERS: Final = 16_000
MAX_TITLE_CHARACTERS: Final = 4096
MAX_URL_CHARACTERS: Final = 8192
MAX_NATIVE_ID_CHARACTERS: Final = 512
MAX_AUTHOR_CHARACTERS: Final = 2048
MAX_PUBLISHED_CHARACTERS: Final = 512
_METADATA_LENGTH_BYTES: Final = 4
_REQUEST_FIELDS: Final = frozenset(
    {"content_location", "content_type", "max_entries", "version"}
)
_RESPONSE_FIELDS: Final = frozenset({"bozo", "entries", "feed", "version"})
_FEED_FIELDS: Final = frozenset({"text", "title", "url"})
_ENTRY_FIELDS: Final = frozenset(
    {"author", "native_id", "published_at", "text", "title", "url"}
)


class FeedparserProtocolError(ValueError):
    """The closed worker input or output contract was violated."""


@dataclass(frozen=True, slots=True)
class FeedProjection:
    """Closed feed-level fields selected from feedparser output."""

    text: str | None
    title: str | None
    url: str | None


@dataclass(frozen=True, slots=True)
class EntryProjection:
    """Closed entry fields selected from feedparser output."""

    text: str | None
    native_id: str | None
    title: str | None
    url: str | None
    author: str | None
    published_at: str | None


@dataclass(frozen=True, slots=True)
class FeedparserProjection:
    """Validated result returned across the parser process boundary."""

    feed: FeedProjection | None
    entries: tuple[EntryProjection, ...]
    bozo: bool


@dataclass(frozen=True, slots=True)
class _WorkerRequest:
    content_type: str
    content_location: str
    max_entries: int
    body: bytes


def encode_request(
    body: bytes,
    *,
    content_type: str,
    content_location: str,
    max_entries: int,
) -> bytes:
    """Frame one bounded byte-only parser request for stdin."""

    if type(body) is not bytes or not 0 < len(body) <= MAX_FEED_BYTES:
        raise FeedparserProtocolError("feed_body_invalid")
    metadata = _validated_metadata(
        {
            "content_location": content_location,
            "content_type": content_type,
            "max_entries": max_entries,
            "version": PROTOCOL_VERSION,
        }
    )
    raw_metadata = json.dumps(
        metadata,
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    if not 0 < len(raw_metadata) <= MAX_METADATA_BYTES:
        raise FeedparserProtocolError("feed_metadata_invalid")
    return (
        len(raw_metadata).to_bytes(_METADATA_LENGTH_BYTES, "big") + raw_metadata + body
    )


def decode_response(raw: bytes) -> FeedparserProjection:
    """Validate the complete, bounded JSON response before parent use."""

    if type(raw) is not bytes or not 0 < len(raw) <= MAX_OUTPUT_BYTES:
        raise FeedparserProtocolError("feed_response_invalid")
    try:
        value = _load_json(raw.decode("utf-8", errors="strict"))
    except (
        UnicodeError,
        json.JSONDecodeError,
        RecursionError,
        FeedparserProtocolError,
    ):
        raise FeedparserProtocolError("feed_response_invalid") from None
    if not isinstance(value, dict) or set(value) != _RESPONSE_FIELDS:
        raise FeedparserProtocolError("feed_response_invalid")
    response = cast(dict[str, object], value)
    if (
        type(response["version"]) is not int
        or response["version"] != PROTOCOL_VERSION
        or type(response["bozo"]) is not bool
    ):
        raise FeedparserProtocolError("feed_response_invalid")

    feed_value = response["feed"]
    feed = None if feed_value is None else _decode_feed(feed_value)
    entries_value = response["entries"]
    if not isinstance(entries_value, list) or len(entries_value) > MAX_ENTRIES:
        raise FeedparserProtocolError("feed_response_invalid")
    entries = tuple(_decode_entry(value) for value in entries_value)
    return FeedparserProjection(feed, entries, response["bozo"])


def _validated_metadata(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != _REQUEST_FIELDS:
        raise FeedparserProtocolError("feed_metadata_invalid")
    metadata = cast(dict[str, object], value)
    content_type = metadata["content_type"]
    content_location = metadata["content_location"]
    max_entries = metadata["max_entries"]
    if (
        type(metadata["version"]) is not int
        or metadata["version"] != PROTOCOL_VERSION
        or type(content_type) is not str
        or len(content_type) > MAX_CONTENT_TYPE_CHARACTERS
        or not content_type.isascii()
        or _contains_control(content_type)
        or not _valid_content_location(content_location)
        or type(max_entries) is not int
        or not 1 <= max_entries <= MAX_ENTRIES
    ):
        raise FeedparserProtocolError("feed_metadata_invalid")
    return {
        "content_location": content_location,
        "content_type": content_type,
        "max_entries": max_entries,
        "version": PROTOCOL_VERSION,
    }


def _valid_content_location(value: object) -> bool:
    if (
        type(value) is not str
        or not 0 < len(value) <= MAX_CONTENT_LOCATION_CHARACTERS
        or not value.isascii()
        or value != value.strip()
        or _contains_control(value)
    ):
        return False
    try:
        parsed = urlsplit(value)
        scheme = parsed.scheme.lower()
        hostname = parsed.hostname
        if (
            scheme not in {"http", "https"}
            or parsed.username is not None
            or parsed.password is not None
            or hostname is None
            or parsed.query
            or parsed.fragment
        ):
            return False
        host = hostname.rstrip(".").encode("idna").decode("ascii").lower()
        if not host or host == "localhost" or host.endswith((".localhost", ".local")):
            return False
        port = parsed.port or (443 if scheme == "https" else 80)
        if port != (443 if scheme == "https" else 80):
            return False
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            return True
        return _is_global_address(address)
    except (UnicodeError, ValueError):
        return False


def _read_request(stream: BinaryIO) -> _WorkerRequest:
    header = stream.read(_METADATA_LENGTH_BYTES)
    if len(header) != _METADATA_LENGTH_BYTES:
        raise FeedparserProtocolError("feed_request_invalid")
    metadata_length = int.from_bytes(header, "big")
    if not 0 < metadata_length <= MAX_METADATA_BYTES:
        raise FeedparserProtocolError("feed_request_invalid")
    raw_metadata = stream.read(metadata_length)
    if len(raw_metadata) != metadata_length:
        raise FeedparserProtocolError("feed_request_invalid")
    try:
        loaded = _load_json(raw_metadata.decode("ascii", errors="strict"))
    except (
        UnicodeError,
        json.JSONDecodeError,
        RecursionError,
        FeedparserProtocolError,
    ):
        raise FeedparserProtocolError("feed_request_invalid") from None
    metadata = _validated_metadata(loaded)
    body = stream.read(MAX_FEED_BYTES + 1)
    if not 0 < len(body) <= MAX_FEED_BYTES:
        raise FeedparserProtocolError("feed_body_invalid")
    return _WorkerRequest(
        cast(str, metadata["content_type"]),
        cast(str, metadata["content_location"]),
        cast(int, metadata["max_entries"]),
        body,
    )


def _parse_feed(request: _WorkerRequest) -> FeedparserProjection:
    try:
        feedparser = importlib.import_module("feedparser")
    except ImportError:
        raise RuntimeError("feedparser_backend_unavailable") from None
    if getattr(feedparser, "__version__", None) != FEEDPARSER_VERSION:
        raise RuntimeError("feedparser_backend_incompatible")
    parser = cast(Callable[..., object], getattr(feedparser, "parse", None))
    if not callable(parser):
        raise RuntimeError("feedparser_backend_incompatible")

    # Feedparser tries to open even a bytes object as a filesystem path first.
    # BytesIO makes the byte-only, no-network/no-file ownership boundary explicit.
    parsed_value = parser(
        io.BytesIO(request.body),
        response_headers={
            "content-location": request.content_location,
            "content-type": request.content_type,
        },
        resolve_relative_uris=True,
        sanitize_html=True,
    )
    parsed = _as_mapping(parsed_value)
    if parsed is None:
        raise FeedparserProtocolError("feedparser_result_invalid")
    feed_value = _as_mapping(parsed.get("feed"))
    feed = _project_feed(feed_value) if feed_value is not None else None
    entries_value = parsed.get("entries")
    if not isinstance(entries_value, list):
        raise FeedparserProtocolError("feedparser_result_invalid")
    entries: list[EntryProjection] = []
    for value in entries_value[: request.max_entries]:
        entry = _as_mapping(value)
        if entry is None:
            raise FeedparserProtocolError("feedparser_result_invalid")
        entries.append(_project_entry(entry))
    bozo = parsed.get("bozo", False)
    if type(bozo) not in {bool, int}:
        raise FeedparserProtocolError("feedparser_result_invalid")
    return FeedparserProjection(feed, tuple(entries), bool(bozo))


def _project_feed(feed: Mapping[str, object]) -> FeedProjection:
    title = _source_string(feed.get("title"), MAX_TITLE_CHARACTERS)
    return FeedProjection(
        _first_string(
            feed,
            ("subtitle", "description", "title"),
            MAX_TEXT_CHARACTERS,
        ),
        title,
        _source_string(feed.get("link"), MAX_URL_CHARACTERS),
    )


def _project_entry(entry: Mapping[str, object]) -> EntryProjection:
    title = _source_string(entry.get("title"), MAX_TITLE_CHARACTERS)
    text = _content_value(entry)
    if text is None:
        text = _first_string(
            entry,
            ("summary", "description", "title"),
            MAX_TEXT_CHARACTERS,
        )
    author = _source_string(entry.get("author"), MAX_AUTHOR_CHARACTERS)
    if author is None:
        detail = _as_mapping(entry.get("author_detail"))
        if detail is not None:
            author = _source_string(detail.get("name"), MAX_AUTHOR_CHARACTERS)
    return EntryProjection(
        text,
        _first_string(entry, ("id", "guid"), MAX_NATIVE_ID_CHARACTERS),
        title,
        _source_string(entry.get("link"), MAX_URL_CHARACTERS),
        author,
        _first_string(
            entry,
            ("published", "updated"),
            MAX_PUBLISHED_CHARACTERS,
        ),
    )


def _content_value(entry: Mapping[str, object]) -> str | None:
    content = entry.get("content")
    if not isinstance(content, list):
        return None
    for value in content:
        mapping = _as_mapping(value)
        if mapping is None:
            continue
        selected = _source_string(mapping.get("value"), MAX_TEXT_CHARACTERS)
        if selected is not None:
            return selected
    return None


def _first_string(
    value: Mapping[str, object], names: tuple[str, ...], maximum: int
) -> str | None:
    for name in names:
        selected = _source_string(value.get(name), maximum)
        if selected is not None:
            return selected
    return None


def _source_string(value: object, maximum: int) -> str | None:
    if type(value) is not str or not value.strip() or _contains_invalid_scalar(value):
        return None
    return value[:maximum]


def _as_mapping(value: object) -> Mapping[str, object] | None:
    return cast(Mapping[str, object], value) if isinstance(value, Mapping) else None


def _response_bytes(result: FeedparserProjection) -> bytes:
    feed = result.feed
    value = {
        "bozo": result.bozo,
        "entries": [
            {
                "author": entry.author,
                "native_id": entry.native_id,
                "published_at": entry.published_at,
                "text": entry.text,
                "title": entry.title,
                "url": entry.url,
            }
            for entry in result.entries
        ],
        "feed": (
            None
            if feed is None
            else {"text": feed.text, "title": feed.title, "url": feed.url}
        ),
        "version": PROTOCOL_VERSION,
    }
    raw = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    if not 0 < len(raw) <= MAX_OUTPUT_BYTES:
        raise FeedparserProtocolError("feed_response_too_large")
    return raw


def _decode_feed(value: object) -> FeedProjection:
    if not isinstance(value, dict) or set(value) != _FEED_FIELDS:
        raise FeedparserProtocolError("feed_response_invalid")
    feed = cast(dict[str, object], value)
    return FeedProjection(
        _decode_string(feed["text"], MAX_TEXT_CHARACTERS),
        _decode_string(feed["title"], MAX_TITLE_CHARACTERS),
        _decode_string(feed["url"], MAX_URL_CHARACTERS),
    )


def _decode_entry(value: object) -> EntryProjection:
    if not isinstance(value, dict) or set(value) != _ENTRY_FIELDS:
        raise FeedparserProtocolError("feed_response_invalid")
    entry = cast(dict[str, object], value)
    return EntryProjection(
        _decode_string(entry["text"], MAX_TEXT_CHARACTERS),
        _decode_string(entry["native_id"], MAX_NATIVE_ID_CHARACTERS),
        _decode_string(entry["title"], MAX_TITLE_CHARACTERS),
        _decode_string(entry["url"], MAX_URL_CHARACTERS),
        _decode_string(entry["author"], MAX_AUTHOR_CHARACTERS),
        _decode_string(entry["published_at"], MAX_PUBLISHED_CHARACTERS),
    )


def _decode_string(value: object, maximum: int) -> str | None:
    if value is None:
        return None
    if (
        type(value) is not str
        or not 0 < len(value) <= maximum
        or _contains_invalid_scalar(value)
    ):
        raise FeedparserProtocolError("feed_response_invalid")
    return value


def _contains_control(value: str) -> bool:
    return any(ord(character) < 32 or ord(character) == 127 for character in value)


def _contains_invalid_scalar(value: str) -> bool:
    return any(
        character == "\x00" or 0xD800 <= ord(character) <= 0xDFFF for character in value
    )


def _load_json(raw: str) -> object:
    return json.loads(raw, object_pairs_hook=_unique_object)


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for name, item in pairs:
        if name in value:
            raise FeedparserProtocolError("feed_json_duplicate_key")
        value[name] = item
    return value


def _is_global_address(
    address: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> bool:
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return _is_global_address(address.ipv4_mapped)
    return bool(
        address.is_global
        and not address.is_private
        and not address.is_loopback
        and not address.is_link_local
        and not address.is_reserved
        and not address.is_multicast
        and not address.is_unspecified
    )


def _main() -> int:
    try:
        request = _read_request(sys.stdin.buffer)
        output = _response_bytes(_parse_feed(request))
        sys.stdout.buffer.write(output)
        sys.stdout.buffer.flush()
        return 0
    except Exception:
        return 1


if __name__ == "__main__":
    raise SystemExit(_main())
