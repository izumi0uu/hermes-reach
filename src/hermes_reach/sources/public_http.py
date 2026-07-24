"""SSRF-resistant public HTTP transport with DNS-pinned connections."""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
import ssl
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final, Protocol
from urllib.parse import quote, urljoin, urlsplit, urlunsplit

import httpcore

from ..runtime.adapters import FailureClass

MAX_URL_CHARACTERS: Final = 8192
MAX_RESPONSE_BYTES: Final = 1_048_576
MAX_REDIRECTS: Final = 3
DNS_TIMEOUT_SECONDS: Final = 3.0
CONNECT_TIMEOUT_SECONDS: Final = 5.0
READ_TIMEOUT_SECONDS: Final = 5.0
_REDIRECT_STATUSES: Final = frozenset({301, 302, 303, 307, 308})
_CONTROL_CHARACTER: Final = re.compile(r"[\x00-\x1f\x7f]")
_HEADERS: Final = (
    (b"Accept", b"text/html,text/plain,application/xml,text/xml,application/json"),
    (b"User-Agent", b"hermes-reach/0.1"),
    (b"Accept-Encoding", b"identity"),
    (b"Connection", b"close"),
)


class HttpFailure(Exception):
    """A closed transport failure that never contains request or response data."""

    def __init__(self, failure_class: FailureClass, code: str) -> None:
        super().__init__(code)
        self.failure_class = failure_class
        self.code = code


@dataclass(frozen=True, slots=True)
class HttpResponse:
    """A bounded response retained only for the current adapter call."""

    status: int
    content_type: str
    body: bytes
    public_url: str


class PublicHttpClient(Protocol):
    """The only HTTP capability visible to credential-free source adapters."""

    async def get(self, url: str) -> HttpResponse: ...


Resolver = Callable[[str, int], Awaitable[tuple[str, ...]]]
BackendFactory = Callable[[], httpcore.AsyncNetworkBackend]


@dataclass(frozen=True, slots=True)
class _NormalizedUrl:
    scheme: str
    host: str
    port: int
    target: str
    request_url: str
    public_url: str


@dataclass(frozen=True, slots=True)
class _HopResponse:
    status: int
    headers: Mapping[bytes, bytes]
    body: bytes


class PinnedNetworkBackend(httpcore.AsyncNetworkBackend):
    """Connect to one validated IP while preserving the HTTP/TLS origin."""

    def __init__(
        self,
        origin_host: str,
        origin_port: int,
        pinned_ip: str,
        backend: httpcore.AsyncNetworkBackend,
    ) -> None:
        self._origin_host = origin_host
        self._origin_port = origin_port
        self._pinned_ip = pinned_ip
        self._backend = backend

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        if host != self._origin_host or port != self._origin_port:
            raise HttpFailure("policy", "unexpected_connection_origin")
        return await self._backend.connect_tcp(
            self._pinned_ip,
            port,
            timeout=timeout,
            local_address=local_address,
            socket_options=socket_options,
        )

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        del path, timeout, socket_options
        raise HttpFailure("policy", "unix_socket_denied")

    async def sleep(self, seconds: float) -> None:
        await self._backend.sleep(seconds)


class PublicHttpTransport:
    """Fetch bounded public documents without proxy or DNS-rebinding paths."""

    def __init__(
        self,
        resolver: Resolver | None = None,
        backend_factory: BackendFactory | None = None,
        ssl_context: ssl.SSLContext | None = None,
        maximum_bytes: int = MAX_RESPONSE_BYTES,
        maximum_redirects: int = MAX_REDIRECTS,
    ) -> None:
        self._resolver = resolver or _resolve_public_addresses
        self._backend_factory = backend_factory or httpcore.AnyIOBackend
        self._ssl_context = ssl_context
        self._maximum_bytes = maximum_bytes
        self._maximum_redirects = maximum_redirects

    async def get(self, url: str) -> HttpResponse:
        """Fetch one public URL, revalidating and pinning every redirect hop."""

        current = _normalize_url(url)
        for redirect_count in range(self._maximum_redirects + 1):
            addresses = await self._resolve(current.host, current.port)
            pinned_ip = _select_public_address(addresses)
            hop = await self._request_hop(current, pinned_ip)
            if hop.status in _REDIRECT_STATUSES:
                if redirect_count >= self._maximum_redirects:
                    raise HttpFailure("permanent", "redirect_limit_exceeded")
                location = hop.headers.get(b"location")
                if location is None:
                    raise HttpFailure("permanent", "redirect_location_missing")
                redirect_url = _decode_location(location)
                next_url = _normalize_url(urljoin(current.request_url, redirect_url))
                if current.scheme == "https" and next_url.scheme != "https":
                    raise HttpFailure("policy", "redirect_downgrade_denied")
                current = next_url
                continue
            _raise_for_status(hop.status)
            return HttpResponse(
                hop.status,
                _header_text(hop.headers.get(b"content-type", b"")),
                hop.body,
                current.public_url,
            )
        raise HttpFailure("permanent", "redirect_limit_exceeded")

    async def _resolve(self, host: str, port: int) -> tuple[str, ...]:
        try:
            return await asyncio.wait_for(
                self._resolver(host, port), timeout=DNS_TIMEOUT_SECONDS
            )
        except asyncio.CancelledError:
            raise
        except HttpFailure:
            raise
        except TimeoutError:
            raise HttpFailure("transient", "dns_timeout") from None
        except Exception:
            raise HttpFailure("transient", "dns_resolution_failed") from None

    async def _request_hop(self, url: _NormalizedUrl, pinned_ip: str) -> _HopResponse:
        backend = PinnedNetworkBackend(
            url.host,
            url.port,
            pinned_ip,
            self._backend_factory(),
        )
        pool = httpcore.AsyncConnectionPool(
            ssl_context=self._ssl_context,
            proxy=None,
            max_connections=1,
            max_keepalive_connections=0,
            keepalive_expiry=0.0,
            http1=True,
            http2=False,
            retries=0,
            network_backend=backend,
        )
        timeout = {
            "pool": CONNECT_TIMEOUT_SECONDS,
            "connect": CONNECT_TIMEOUT_SECONDS,
            "read": READ_TIMEOUT_SECONDS,
            "write": READ_TIMEOUT_SECONDS,
        }
        request_failed = False
        try:
            async with pool.stream(
                "GET",
                httpcore.URL(
                    scheme=url.scheme,
                    host=url.host,
                    port=url.port,
                    target=url.target,
                ),
                headers=_HEADERS,
                extensions={"timeout": timeout},
            ) as response:
                headers = _response_headers(response.headers)
                _validate_response_headers(headers, self._maximum_bytes)
                if response.status in _REDIRECT_STATUSES:
                    return _HopResponse(response.status, headers, b"")
                body = bytearray()
                async for chunk in response.aiter_stream():
                    if len(body) + len(chunk) > self._maximum_bytes:
                        raise HttpFailure("permanent", "response_too_large")
                    body.extend(chunk)
                return _HopResponse(response.status, headers, bytes(body))
        except asyncio.CancelledError:
            request_failed = True
            raise
        except HttpFailure:
            request_failed = True
            raise
        except (TimeoutError, httpcore.TimeoutException):
            request_failed = True
            raise HttpFailure("transient", "http_timeout") from None
        except (httpcore.NetworkError, httpcore.ProtocolError):
            request_failed = True
            raise HttpFailure("transient", "http_transport_failed") from None
        except Exception:
            request_failed = True
            raise HttpFailure("permanent", "http_exchange_failed") from None
        finally:
            try:
                await pool.aclose()
            except asyncio.CancelledError:
                raise
            except Exception:
                if not request_failed:
                    raise HttpFailure("transient", "http_cleanup_failed") from None


async def _resolve_public_addresses(host: str, port: int) -> tuple[str, ...]:
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        return (str(literal),)
    try:
        loop = asyncio.get_running_loop()
        records = await asyncio.wait_for(
            loop.getaddrinfo(
                host,
                port,
                family=socket.AF_UNSPEC,
                type=socket.SOCK_STREAM,
                proto=socket.IPPROTO_TCP,
            ),
            timeout=DNS_TIMEOUT_SECONDS,
        )
    except TimeoutError:
        raise HttpFailure("transient", "dns_timeout") from None
    except socket.gaierror as error:
        missing = error.errno == getattr(socket, "EAI_NONAME", None)
        failure: FailureClass = "not_found" if missing else "transient"
        raise HttpFailure(failure, "dns_resolution_failed") from None
    addresses = tuple(record[4][0] for record in records)
    if not addresses:
        raise HttpFailure("not_found", "dns_answer_empty")
    return addresses


def _normalize_url(value: str) -> _NormalizedUrl:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > MAX_URL_CHARACTERS
        or value != value.strip()
        or _CONTROL_CHARACTER.search(value)
        or "#" in value
    ):
        raise HttpFailure("policy", "url_invalid")
    try:
        parsed = urlsplit(value)
        scheme = parsed.scheme.lower()
        if scheme not in {"http", "https"}:
            raise HttpFailure("policy", "url_scheme_denied")
        if parsed.username is not None or parsed.password is not None:
            raise HttpFailure("policy", "url_userinfo_denied")
        hostname = parsed.hostname
        if hostname is None:
            raise HttpFailure("policy", "url_host_missing")
        host = hostname.rstrip(".").encode("idna").decode("ascii").lower()
        if not host or len(host) > 253:
            raise HttpFailure("policy", "url_host_invalid")
        if host == "localhost" or host.endswith((".localhost", ".local")):
            raise HttpFailure("policy", "non_public_address_denied")
        try:
            literal_address = ipaddress.ip_address(host)
        except ValueError:
            literal_address = None
        if literal_address is not None and not _is_public_address(literal_address):
            raise HttpFailure("policy", "non_public_address_denied")
        port = parsed.port or (443 if scheme == "https" else 80)
        if port != (443 if scheme == "https" else 80):
            raise HttpFailure("policy", "url_port_denied")
    except HttpFailure:
        raise
    except (UnicodeError, ValueError):
        raise HttpFailure("policy", "url_invalid") from None

    path = quote(parsed.path or "/", safe="/%:@!$&'()*+,;=-._~")
    query = quote(parsed.query, safe="=&;%:@!$'()*+,-._~/?")
    target = path if not query else f"{path}?{query}"
    display_host = f"[{host}]" if ":" in host else host
    request_url = urlunsplit((scheme, display_host, path, query, ""))
    public_url = urlunsplit((scheme, display_host, path, "", ""))
    if len(request_url) > MAX_URL_CHARACTERS:
        raise HttpFailure("policy", "url_invalid")
    return _NormalizedUrl(scheme, host, port, target, request_url, public_url)


def _select_public_address(addresses: tuple[str, ...]) -> str:
    if not addresses:
        raise HttpFailure("not_found", "dns_answer_empty")
    parsed: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for value in addresses:
        try:
            address = ipaddress.ip_address(value)
        except ValueError:
            raise HttpFailure("policy", "dns_answer_invalid") from None
        if not _is_public_address(address):
            raise HttpFailure("policy", "non_public_address_denied")
        parsed.append(address)
    return str(sorted(parsed, key=lambda item: (item.version, item.packed))[0])


def _is_public_address(
    address: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> bool:
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return _is_public_address(address.ipv4_mapped)
    return bool(
        address.is_global
        and not address.is_private
        and not address.is_loopback
        and not address.is_link_local
        and not address.is_reserved
        and not address.is_multicast
        and not address.is_unspecified
    )


def _response_headers(values: list[tuple[bytes, bytes]]) -> Mapping[bytes, bytes]:
    headers: dict[bytes, bytes] = {}
    for name, value in values:
        key = name.lower()
        allowed = {
            b"content-length",
            b"content-encoding",
            b"content-type",
            b"location",
        }
        if key in allowed:
            headers.setdefault(key, value)
    return MappingProxyType(headers)


def _validate_response_headers(headers: Mapping[bytes, bytes], maximum: int) -> None:
    encoding = headers.get(b"content-encoding", b"identity").strip().lower()
    if encoding not in {b"", b"identity"}:
        raise HttpFailure("permanent", "content_encoding_unsupported")
    content_length = headers.get(b"content-length")
    if content_length is None:
        return
    try:
        length = int(content_length)
    except ValueError:
        raise HttpFailure("permanent", "content_length_invalid") from None
    if length < 0 or length > maximum:
        raise HttpFailure("permanent", "response_too_large")


def _decode_location(value: bytes) -> str:
    if len(value) > MAX_URL_CHARACTERS:
        raise HttpFailure("policy", "redirect_location_invalid")
    try:
        location = value.decode("latin-1")
    except UnicodeError:
        raise HttpFailure("policy", "redirect_location_invalid") from None
    if not location or _CONTROL_CHARACTER.search(location):
        raise HttpFailure("policy", "redirect_location_invalid")
    return location


def _header_text(value: bytes) -> str:
    try:
        return value.decode("ascii").lower()
    except UnicodeError:
        raise HttpFailure("permanent", "content_type_invalid") from None


def _raise_for_status(status: int) -> None:
    if 200 <= status < 300:
        return
    if status in {404, 410}:
        raise HttpFailure("not_found", "http_not_found")
    if status in {408, 425} or 500 <= status < 600:
        raise HttpFailure("transient", "http_temporary_failure")
    if status == 401:
        raise HttpFailure("authentication", "http_authentication_required")
    if status == 403:
        raise HttpFailure("authorization", "http_access_denied")
    if status == 429:
        raise HttpFailure("rate_limit", "http_rate_limited")
    if status in {400, 422}:
        raise HttpFailure("invalid_input", "http_request_rejected")
    raise HttpFailure("permanent", "http_status_unsupported")
