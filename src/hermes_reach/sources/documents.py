"""Deterministic, bounded helpers shared by document source adapters."""

from __future__ import annotations

import codecs
import ipaddress
import re
from html.parser import HTMLParser
from typing import Final
from urllib.parse import quote, urljoin, urlsplit, urlunsplit

MAX_DECODED_CHARACTERS: Final = 250_000
_CHARSET = re.compile(r"charset\s*=\s*['\"]?([A-Za-z0-9._-]+)", re.IGNORECASE)
_IGNORED_HTML: Final = frozenset(
    {"script", "style", "noscript", "template", "svg", "canvas"}
)
_BLOCK_HTML: Final = frozenset(
    {
        "address",
        "article",
        "aside",
        "blockquote",
        "br",
        "div",
        "footer",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "li",
        "main",
        "nav",
        "p",
        "section",
        "td",
        "th",
        "tr",
    }
)


class DocumentError(Exception):
    """A safe parser error without source bytes or URLs."""


class _VisibleTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._ignored_depth = 0
        self._title_depth = 0
        self._chunks: list[str] = []
        self._title_chunks: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        normalized = tag.lower()
        if normalized in _IGNORED_HTML:
            self._ignored_depth += 1
            return
        if self._ignored_depth:
            return
        if normalized == "title":
            self._title_depth += 1
        if normalized in _BLOCK_HTML:
            self._chunks.append(" ")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        if not self._ignored_depth and tag.lower() in _BLOCK_HTML:
            self._chunks.append(" ")

    def handle_endtag(self, tag: str) -> None:
        normalized = tag.lower()
        if normalized in _IGNORED_HTML:
            if self._ignored_depth:
                self._ignored_depth -= 1
            return
        if self._ignored_depth:
            return
        if normalized == "title" and self._title_depth:
            self._title_depth -= 1
        if normalized in _BLOCK_HTML:
            self._chunks.append(" ")

    def handle_data(self, data: str) -> None:
        if self._ignored_depth:
            return
        self._chunks.append(data)
        if self._title_depth:
            self._title_chunks.append(data)

    def result(self) -> tuple[str, str | None]:
        text = normalize_whitespace(" ".join(self._chunks))
        title = normalize_whitespace(" ".join(self._title_chunks)) or None
        return text, title


def decode_document(body: bytes, content_type: str) -> str:
    """Decode a bounded textual body using an explicit or UTF default charset."""

    match = _CHARSET.search(content_type)
    charset = match.group(1).lower() if match else "utf-8"
    if body.startswith(codecs.BOM_UTF8):
        charset = "utf-8-sig"
    elif body.startswith((codecs.BOM_UTF32_BE, codecs.BOM_UTF32_LE)):
        charset = "utf-32"
    elif body.startswith((codecs.BOM_UTF16_BE, codecs.BOM_UTF16_LE)):
        charset = "utf-16"
    try:
        codec = codecs.lookup(charset)
        text = codec.decode(body, "strict")[0]
    except (LookupError, UnicodeError) as error:
        raise DocumentError("document_decode_failed") from error
    if len(text) > MAX_DECODED_CHARACTERS:
        raise DocumentError("document_character_limit")
    return text


def extract_visible_html(value: str) -> tuple[str, str | None]:
    """Extract visible text and title without building a browser DOM."""

    parser = _VisibleTextParser()
    try:
        parser.feed(value)
        parser.close()
    except Exception as error:
        raise DocumentError("html_parse_failed") from error
    return parser.result()


def normalize_whitespace(value: str) -> str:
    return " ".join(value.split())


def sanitize_result_url(value: str | None, base_url: str) -> str | None:
    """Return safe display metadata only; this function never fetches a URL."""

    if not value:
        return None
    try:
        parsed = urlsplit(urljoin(base_url, value.strip()))
        scheme = parsed.scheme.lower()
        if scheme not in {"http", "https"}:
            return None
        if parsed.username is not None or parsed.password is not None:
            return None
        hostname = parsed.hostname
        if hostname is None:
            return None
        host = hostname.rstrip(".").encode("idna").decode("ascii").lower()
        if host == "localhost" or host.endswith((".localhost", ".local")):
            return None
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
        if address is not None and not address.is_global:
            return None
        port = parsed.port or (443 if scheme == "https" else 80)
        if port != (443 if scheme == "https" else 80):
            return None
        path = quote(parsed.path or "/", safe="/%:@!$&'()*+,;=-._~")
        display_host = f"[{host}]" if ":" in host else host
        return urlunsplit((scheme, display_host, path, "", ""))
    except (UnicodeError, ValueError):
        return None
