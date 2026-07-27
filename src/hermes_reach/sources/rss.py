"""One-shot RSS and Atom parsing with no subscription or content state."""

from __future__ import annotations

import asyncio
import codecs
import os
import re
import signal
import sys
from collections.abc import Mapping
from typing import Final

from ..runtime.adapters import AdapterResult, FailureClass, RawItem
from ..runtime.policy import AuthorizedCall
from .documents import (
    DocumentError,
    decode_document,
    extract_visible_html,
    normalize_whitespace,
    sanitize_result_url,
)
from .public_http import HttpFailure, PublicHttpClient
from .rss_worker import (
    MAX_ENTRIES,
    MAX_OUTPUT_BYTES,
    EntryProjection,
    FeedparserProjection,
    FeedparserProtocolError,
    FeedProjection,
    decode_response,
    encode_request,
)

_UNSAFE_DECLARATION: Final = re.compile(r"<!\s*(?:DOCTYPE|ENTITY)\b", re.I)
_XML_ENCODING: Final = re.compile(
    rb"<\?xml[^>]*encoding\s*=\s*['\"]([A-Za-z0-9._-]+)['\"]", re.I
)
_TEXT_XML_ENCODING: Final = re.compile(
    r"<\?xml[^>]*encoding\s*=\s*['\"]([A-Za-z0-9._-]+)['\"]", re.I
)
_CONTENT_TYPE_ENCODING: Final = re.compile(
    r"charset\s*=\s*['\"]?([A-Za-z0-9._-]+)", re.I
)
_SUPPORTED_XML_ENCODINGS: Final = frozenset(
    {
        "utf-8",
        "utf8",
        "utf-16",
        "utf-16-le",
        "utf-16-be",
        "utf-16le",
        "utf-16be",
        "utf-32",
        "utf-32-le",
        "utf-32-be",
        "utf-32le",
        "utf-32be",
    }
)
_WORKER_MODULE: Final = "hermes_reach.sources.rss_worker"


class FeedError(Exception):
    """A safe feed parser error without XML or URL content."""


class FeedparserWorkerError(Exception):
    """A classified parser worker failure without process or feed details."""

    def __init__(self, failure_class: FailureClass) -> None:
        super().__init__("feedparser_worker_failed")
        self.failure_class = failure_class


class FeedparserWorker:
    """Run feedparser in a fixed isolated process that can be hard-cancelled."""

    async def parse(
        self,
        body: bytes,
        *,
        content_type: str,
        content_location: str,
        max_entries: int,
    ) -> FeedparserProjection:
        try:
            request = encode_request(
                body,
                content_type=content_type,
                content_location=content_location,
                max_entries=max_entries,
            )
        except FeedparserProtocolError:
            raise FeedparserWorkerError("permanent") from None

        process: asyncio.subprocess.Process | None = None
        try:
            try:
                process = await asyncio.create_subprocess_exec(
                    sys.executable,
                    "-I",
                    "-m",
                    _WORKER_MODULE,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                    cwd="/",
                    env={},
                    close_fds=True,
                    start_new_session=True,
                )
            except asyncio.CancelledError:
                raise
            except (OSError, ValueError):
                raise FeedparserWorkerError("transient") from None
            output = await _exchange_bounded(process, request)
            if process.returncode != 0:
                raise FeedparserWorkerError("permanent")
            try:
                return decode_response(output)
            except FeedparserProtocolError:
                raise FeedparserWorkerError("permanent") from None
        except asyncio.CancelledError:
            raise
        except FeedparserWorkerError:
            raise
        except (BrokenPipeError, ChildProcessError, ConnectionError, OSError):
            raise FeedparserWorkerError("transient") from None
        except Exception:
            raise FeedparserWorkerError("transient") from None
        finally:
            if process is not None:
                await _kill_process_group(process)


class RssAdapter:
    def __init__(self, client: PublicHttpClient) -> None:
        self._client = client
        self._worker = FeedparserWorker()

    async def execute(self, authorized: AuthorizedCall) -> AdapterResult:
        target = authorized.call.target
        if target is None or "url" not in target:
            return AdapterResult(failure_class="invalid_input")
        try:
            response = await self._client.get(target["url"])
            _preflight_xml(response.body, response.content_type)
            content_location = sanitize_result_url(
                response.public_url, response.public_url
            )
            if content_location is None:
                raise FeedError("feed_location_invalid")
            maximum_items = authorized.operation.runtime.maximum_items
            limit = (
                _integer_option(
                    authorized.call.options,
                    "limit",
                    maximum_items,
                )
                if authorized.operation.name == "browse.entries"
                else 1
            )
            parsed = await self._worker.parse(
                response.body,
                content_type=response.content_type,
                content_location=content_location,
                max_entries=min(limit, maximum_items + 1, MAX_ENTRIES),
            )
            if authorized.operation.name == "read.feed":
                item = _feed_item(parsed.feed, content_location)
                return AdapterResult(
                    (item,),
                    partial_failure_class="permanent" if parsed.bozo else None,
                )
            if authorized.operation.name == "browse.entries":
                projected = tuple(
                    _entry_item(entry, content_location) for entry in parsed.entries
                )
                items = tuple(item for item in projected if item is not None)
                dropped_entries = len(items) != len(parsed.entries)
                if not items and (parsed.bozo or parsed.entries):
                    raise FeedError("feed_entries_unusable")
                return AdapterResult(
                    items,
                    partial_failure_class=(
                        "permanent" if parsed.bozo or dropped_entries else None
                    ),
                )
            return AdapterResult(failure_class="invalid_input")
        except asyncio.CancelledError:
            raise
        except HttpFailure as error:
            return AdapterResult(failure_class=error.failure_class)
        except FeedparserWorkerError as error:
            return AdapterResult(failure_class=error.failure_class)
        except (DocumentError, FeedError):
            return AdapterResult(failure_class="permanent")
        except Exception:
            return AdapterResult(failure_class="transient")


def _preflight_xml(body: bytes, content_type: str) -> None:
    detected = _detected_encoding(body)
    declared = (
        detected or _declared_encoding(body) or _content_type_encoding(content_type)
    )
    effective_type = content_type
    if declared is not None:
        selected_encoding = _canonical_encoding(declared)
        if selected_encoding is None:
            raise FeedError("xml_encoding_unsupported")
        effective_type = f"text/xml; charset={selected_encoding}"
    text = decode_document(body, effective_type)
    text_declaration = _text_declared_encoding(text)
    if text_declaration is not None:
        declared_encoding = _canonical_encoding(text_declaration)
        if declared_encoding is None:
            raise FeedError("xml_encoding_unsupported")
        if detected is not None and not _encodings_match(
            _canonical_encoding(detected), declared_encoding
        ):
            raise FeedError("xml_encoding_mismatch")
    if _UNSAFE_DECLARATION.search(text):
        raise FeedError("xml_declaration_denied")


def _declared_encoding(body: bytes) -> str | None:
    if body.startswith(
        (
            codecs.BOM_UTF8,
            codecs.BOM_UTF16_BE,
            codecs.BOM_UTF16_LE,
            codecs.BOM_UTF32_BE,
            codecs.BOM_UTF32_LE,
        )
    ):
        return None
    match = _XML_ENCODING.search(body[:256])
    return match.group(1).decode("ascii") if match else None


def _detected_encoding(body: bytes) -> str | None:
    if body.startswith(codecs.BOM_UTF32_BE):
        return "utf-32-be"
    if body.startswith(codecs.BOM_UTF32_LE):
        return "utf-32-le"
    if body.startswith(codecs.BOM_UTF16_BE):
        return "utf-16-be"
    if body.startswith(codecs.BOM_UTF16_LE):
        return "utf-16-le"
    if body.startswith(codecs.BOM_UTF8):
        return "utf-8"
    if body.startswith(b"\x00\x00\x00<"):
        return "utf-32-be"
    if body.startswith(b"<\x00\x00\x00"):
        return "utf-32-le"
    if body.startswith(b"\x00<"):
        return "utf-16-be"
    if body.startswith(b"<\x00"):
        return "utf-16-le"
    return None


def _content_type_encoding(content_type: str) -> str | None:
    match = _CONTENT_TYPE_ENCODING.search(content_type)
    return match.group(1) if match else None


def _text_declared_encoding(text: str) -> str | None:
    match = _TEXT_XML_ENCODING.search(text[:256])
    return match.group(1) if match else None


def _canonical_encoding(value: str) -> str | None:
    normalized = value.lower().replace("_", "-")
    aliases = {
        "utf8": "utf-8",
        "utf16": "utf-16",
        "utf16le": "utf-16-le",
        "utf16be": "utf-16-be",
        "utf-16le": "utf-16-le",
        "utf-16be": "utf-16-be",
        "utf32": "utf-32",
        "utf32le": "utf-32-le",
        "utf32be": "utf-32-be",
        "utf-32le": "utf-32-le",
        "utf-32be": "utf-32-be",
    }
    canonical = aliases.get(normalized, normalized)
    return canonical if canonical in _SUPPORTED_XML_ENCODINGS else None


def _encodings_match(actual: str | None, declared: str) -> bool:
    if actual is None:
        return False
    if actual == declared:
        return True
    return declared in {"utf-16", "utf-32"} and actual.startswith(f"{declared}-")


def _feed_item(feed: FeedProjection | None, base_url: str) -> RawItem:
    if feed is None:
        raise FeedError("feed_metadata_missing")
    title = _optional_text(feed.title)
    text = _content_text(feed.text or title or "")
    if not text:
        raise FeedError("feed_metadata_missing")
    return RawItem(
        text=text,
        kind="content",
        title=title,
        url=sanitize_result_url(feed.url, base_url),
    )


def _entry_item(entry: EntryProjection, base_url: str) -> RawItem | None:
    title = _optional_text(entry.title)
    text = _content_text(entry.text or title or "")
    if not text:
        return None
    return RawItem(
        text=text,
        native_id=_optional_text(entry.native_id),
        kind="entry",
        title=title,
        url=sanitize_result_url(entry.url, base_url),
        author=_optional_text(entry.author),
        published_at=_optional_text(entry.published_at),
    )


def _content_text(value: str) -> str:
    if "<" in value and ">" in value:
        text, _ = extract_visible_html(value)
        return text
    return normalize_whitespace(value)


def _optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    return normalize_whitespace(value) or None


def _integer_option(options: Mapping[str, object], name: str, default: int) -> int:
    value = options.get(name, default)
    return value if isinstance(value, int) and not isinstance(value, bool) else default


async def _exchange_bounded(
    process: asyncio.subprocess.Process, request: bytes
) -> bytes:
    writer = process.stdin
    reader = process.stdout
    if writer is None or reader is None:
        raise FeedparserWorkerError("transient")
    writer.write(request)
    await writer.drain()
    writer.close()

    output = bytearray()
    try:
        while len(output) <= MAX_OUTPUT_BYTES:
            chunk = await reader.read(min(8192, MAX_OUTPUT_BYTES + 1 - len(output)))
            if not chunk:
                break
            output.extend(chunk)
            if len(output) > MAX_OUTPUT_BYTES:
                raise FeedparserWorkerError("permanent")
        await process.wait()
        return bytes(output)
    finally:
        output[:] = b"\x00" * len(output)


async def _kill_process_group(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
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
    try:
        await process.wait()
    except (BrokenPipeError, ChildProcessError, ConnectionError, OSError):
        pass
