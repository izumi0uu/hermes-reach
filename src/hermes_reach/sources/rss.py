"""One-shot RSS and Atom parsing with no subscription or content state."""

from __future__ import annotations

import codecs
import re
import xml.etree.ElementTree as ET
from collections.abc import Mapping
from typing import Final

from ..runtime.adapters import AdapterResult, RawItem
from ..runtime.policy import AuthorizedCall
from .documents import (
    DocumentError,
    decode_document,
    extract_visible_html,
    normalize_whitespace,
    sanitize_result_url,
)
from .public_http import HttpFailure, PublicHttpClient

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


class FeedError(Exception):
    """A safe feed parser error without XML or URL content."""


class RssAdapter:
    def __init__(self, client: PublicHttpClient) -> None:
        self._client = client

    async def execute(self, authorized: AuthorizedCall) -> AdapterResult:
        target = authorized.call.target
        if target is None or "url" not in target:
            return AdapterResult(failure_class="invalid_input")
        try:
            response = await self._client.get(target["url"])
            root = _parse_xml(response.body, response.content_type)
            if authorized.operation.name == "read.feed":
                item = _feed_item(root, response.public_url)
                return AdapterResult((item,))
            if authorized.operation.name == "browse.entries":
                limit = _integer_option(
                    authorized.call.options,
                    "limit",
                    authorized.operation.runtime.maximum_items,
                )
                return AdapterResult(
                    tuple(_entry_items(root, response.public_url)[:limit])
                )
            return AdapterResult(failure_class="invalid_input")
        except HttpFailure as error:
            return AdapterResult(failure_class=error.failure_class)
        except (DocumentError, FeedError, ET.ParseError):
            return AdapterResult(failure_class="permanent")
        except Exception:
            return AdapterResult(failure_class="transient")


def _parse_xml(body: bytes, content_type: str) -> ET.Element:
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
    return ET.fromstring(text)


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


def _feed_item(root: ET.Element, base_url: str) -> RawItem:
    container = _feed_container(root)
    title = _child_text(container, "title")
    description = _first_text(container, ("description", "subtitle"))
    text = _content_text(description or title)
    if not text:
        raise FeedError("feed_metadata_missing")
    return RawItem(
        text=text,
        kind="content",
        title=title or None,
        url=_feed_link(container, base_url),
    )


def _entry_items(root: ET.Element, base_url: str) -> list[RawItem]:
    container = (
        root if _local_name(root.tag).lower() == "rdf" else _feed_container(root)
    )
    entries = [
        child for child in container if _local_name(child.tag) in {"item", "entry"}
    ]
    return [_entry_item(entry, base_url) for entry in entries]


def _entry_item(entry: ET.Element, base_url: str) -> RawItem:
    title = _child_text(entry, "title")
    content = _first_text(entry, ("description", "summary", "content", "encoded"))
    text = _content_text(content or title)
    native_id = _first_text(entry, ("guid", "id")) or None
    author = _first_text(entry, ("author", "creator")) or None
    published = _first_text(entry, ("published", "updated", "pubDate", "date"))
    return RawItem(
        text=text,
        native_id=native_id,
        kind="entry",
        title=title or None,
        url=_entry_link(entry, base_url),
        author=author,
        published_at=published or None,
    )


def _feed_container(root: ET.Element) -> ET.Element:
    root_name = _local_name(root.tag).lower()
    if root_name == "feed":
        return root
    if root_name in {"rss", "rdf"}:
        channel = next(
            (child for child in root if _local_name(child.tag) == "channel"), None
        )
        return channel if channel is not None else root
    raise FeedError("feed_root_unsupported")


def _feed_link(container: ET.Element, base_url: str) -> str | None:
    return _entry_link(container, base_url)


def _entry_link(element: ET.Element, base_url: str) -> str | None:
    for child in element:
        if _local_name(child.tag) != "link":
            continue
        href = child.attrib.get("href")
        rel = child.attrib.get("rel", "alternate")
        if href and rel in {"", "alternate"}:
            return sanitize_result_url(href, base_url)
        if child.text and child.text.strip():
            return sanitize_result_url(child.text, base_url)
    return None


def _first_text(element: ET.Element, names: tuple[str, ...]) -> str:
    for name in names:
        value = _child_text(element, name)
        if value:
            return value
    return ""


def _child_text(element: ET.Element, name: str) -> str:
    for child in element:
        if _local_name(child.tag) == name:
            return normalize_whitespace(" ".join(child.itertext()))
    return ""


def _content_text(value: str) -> str:
    if "<" in value and ">" in value:
        text, _ = extract_visible_html(value)
        return text
    return normalize_whitespace(value)


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _integer_option(options: Mapping[str, object], name: str, default: int) -> int:
    value = options.get(name, default)
    return value if isinstance(value, int) and not isinstance(value, bool) else default
