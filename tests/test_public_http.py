from __future__ import annotations

import asyncio
import os
import ssl
from collections.abc import Iterable

import httpcore
import pytest

import hermes_reach.sources.public_http as public_http
from hermes_reach.sources.public_http import HttpFailure, PublicHttpTransport


class RecordingStream(httpcore.AsyncMockStream):
    def __init__(self, buffer: list[bytes]) -> None:
        super().__init__(buffer)
        self.writes: list[bytes] = []
        self.tls_hosts: list[str | None] = []
        self.closed = False

    async def write(self, buffer: bytes, timeout: float | None = None) -> None:
        del timeout
        self.writes.append(buffer)

    async def start_tls(
        self,
        ssl_context: ssl.SSLContext,
        server_hostname: str | None = None,
        timeout: float | None = None,
    ) -> httpcore.AsyncNetworkStream:
        del ssl_context, timeout
        self.tls_hosts.append(server_hostname)
        return self

    async def aclose(self) -> None:
        self.closed = True
        await super().aclose()


class RecordingBackend(httpcore.AsyncNetworkBackend):
    def __init__(self, response: bytes) -> None:
        self._response = response
        self.connects: list[tuple[str, int]] = []
        self.streams: list[RecordingStream] = []

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        del timeout, local_address, socket_options
        self.connects.append((host, port))
        stream = RecordingStream([self._response])
        self.streams.append(stream)
        return stream

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        del path, timeout, socket_options
        raise AssertionError("unix sockets are forbidden")

    async def sleep(self, seconds: float) -> None:
        del seconds


class BackendScript:
    def __init__(self, *responses: bytes) -> None:
        self._responses = list(responses)
        self.backends: list[RecordingBackend] = []

    def __call__(self) -> httpcore.AsyncNetworkBackend:
        backend = RecordingBackend(self._responses.pop(0))
        self.backends.append(backend)
        return backend


def _response(
    body: bytes = b"ok",
    *,
    status: int = 200,
    headers: tuple[tuple[bytes, bytes], ...] = (),
) -> bytes:
    lines = [
        f"HTTP/1.1 {status} Test".encode(),
        f"Content-Length: {len(body)}".encode(),
        b"Connection: close",
    ]
    lines.extend(name + b": " + value for name, value in headers)
    return b"\r\n".join(lines) + b"\r\n\r\n" + body


def _ssl_context() -> ssl.SSLContext:
    return ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)


def test_transport_pins_ip_but_preserves_host_and_tls_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolutions: list[tuple[str, int]] = []

    async def resolver(host: str, port: int) -> tuple[str, ...]:
        resolutions.append((host, port))
        return ("1.1.1.1",)

    monkeypatch.setattr(
        os,
        "getenv",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("ambient proxy reads are forbidden")
        ),
    )
    script = BackendScript(
        _response(headers=((b"Content-Type", b"text/plain; charset=utf-8"),))
    )
    transport = PublicHttpTransport(
        resolver=resolver,
        backend_factory=script,
        ssl_context=_ssl_context(),
    )

    result = asyncio.run(
        transport.get("https://example.com/private/path?private-query=value")
    )

    backend = script.backends[0]
    stream = backend.streams[0]
    request_bytes = b"".join(stream.writes)
    assert resolutions == [("example.com", 443)]
    assert backend.connects == [("1.1.1.1", 443)]
    assert stream.tls_hosts == ["example.com"]
    assert b"Host: example.com" in request_bytes
    assert b"GET /private/path?private-query=value HTTP/1.1" in request_bytes
    assert result.public_url == "https://example.com/private/path"
    assert result.body == b"ok"
    assert stream.closed is True


@pytest.mark.parametrize(
    "url",
    [
        "file:///private",
        "https://user:password@example.com/",
        "https://example.com:8443/",
        "https://example.com/#fragment",
        "https://127.0.0.1/",
        "https://[::1]/",
        "https://service.local/",
    ],
)
def test_transport_rejects_unsafe_url_shapes_before_resolution(url: str) -> None:
    async def resolver(host: str, port: int) -> tuple[str, ...]:
        del host, port
        raise AssertionError("unsafe URLs must not resolve")

    transport = PublicHttpTransport(resolver=resolver)

    with pytest.raises(HttpFailure) as exc_info:
        asyncio.run(transport.get(url))

    assert exc_info.value.failure_class == "policy"


@pytest.mark.parametrize(
    "answers",
    [
        ("127.0.0.1",),
        ("10.0.0.1",),
        ("169.254.1.1",),
        ("::1",),
        ("::ffff:127.0.0.1",),
        ("1.1.1.1", "127.0.0.1"),
        ("224.0.0.1",),
    ],
)
def test_transport_rejects_any_non_public_dns_answer(answers: tuple[str, ...]) -> None:
    async def resolver(host: str, port: int) -> tuple[str, ...]:
        del host, port
        return answers

    backend_called = False

    def backend_factory() -> httpcore.AsyncNetworkBackend:
        nonlocal backend_called
        backend_called = True
        return RecordingBackend(_response())

    transport = PublicHttpTransport(resolver, backend_factory)

    with pytest.raises(HttpFailure) as exc_info:
        asyncio.run(transport.get("https://example.com/"))

    assert exc_info.value.failure_class == "policy"
    assert backend_called is False


def test_transport_revalidates_each_redirect_and_denies_downgrade() -> None:
    async def resolver(host: str, port: int) -> tuple[str, ...]:
        del host, port
        return ("1.1.1.1",)

    script = BackendScript(
        _response(
            b"",
            status=302,
            headers=((b"Location", b"http://example.org/next"),),
        )
    )
    transport = PublicHttpTransport(resolver, script, ssl_context=_ssl_context())

    with pytest.raises(HttpFailure) as exc_info:
        asyncio.run(transport.get("https://example.com/start"))

    assert exc_info.value.code == "redirect_downgrade_denied"
    assert len(script.backends) == 1


def test_transport_revalidates_redirect_dns_before_connecting() -> None:
    resolutions: list[tuple[str, int]] = []

    async def resolver(host: str, port: int) -> tuple[str, ...]:
        resolutions.append((host, port))
        if host == "example.com":
            return ("1.1.1.1",)
        return ("1.0.0.1", "127.0.0.1")

    script = BackendScript(
        _response(
            b"",
            status=302,
            headers=((b"Location", b"https://example.org/next"),),
        )
    )
    transport = PublicHttpTransport(resolver, script, ssl_context=_ssl_context())

    with pytest.raises(HttpFailure) as exc_info:
        asyncio.run(transport.get("https://example.com/start"))

    assert exc_info.value.code == "non_public_address_denied"
    assert resolutions == [("example.com", 443), ("example.org", 443)]
    assert len(script.backends) == 1
    assert script.backends[0].streams[0].closed is True


def test_transport_bounds_injected_dns_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def resolver(host: str, port: int) -> tuple[str, ...]:
        del host, port
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    monkeypatch.setattr(public_http, "DNS_TIMEOUT_SECONDS", 0.001)
    transport = PublicHttpTransport(resolver=resolver)

    with pytest.raises(HttpFailure) as exc_info:
        asyncio.run(transport.get("https://example.com/"))

    assert exc_info.value.failure_class == "transient"
    assert exc_info.value.code == "dns_timeout"


def test_transport_enforces_declared_and_streamed_byte_limits() -> None:
    async def resolver(host: str, port: int) -> tuple[str, ...]:
        del host, port
        return ("1.1.1.1",)

    script = BackendScript(_response(b"12345"))
    transport = PublicHttpTransport(
        resolver,
        script,
        ssl_context=_ssl_context(),
        maximum_bytes=4,
    )

    with pytest.raises(HttpFailure) as exc_info:
        asyncio.run(transport.get("https://example.com/"))

    assert exc_info.value.code == "response_too_large"

    chunked = (
        b"HTTP/1.1 200 OK\r\n"
        b"Transfer-Encoding: chunked\r\n"
        b"Connection: close\r\n\r\n"
        b"5\r\n12345\r\n0\r\n\r\n"
    )
    streamed = BackendScript(chunked)
    streamed_transport = PublicHttpTransport(
        resolver,
        streamed,
        ssl_context=_ssl_context(),
        maximum_bytes=4,
    )

    with pytest.raises(HttpFailure) as streamed_exc:
        asyncio.run(streamed_transport.get("https://example.com/"))

    assert streamed_exc.value.code == "response_too_large"


class BlockingStream(RecordingStream):
    def __init__(self) -> None:
        super().__init__([])
        self.read_started = asyncio.Event()

    async def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        del max_bytes, timeout
        self.read_started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


class BlockingBackend(RecordingBackend):
    def __init__(self) -> None:
        super().__init__(b"")
        self.blocking_stream = BlockingStream()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        del timeout, local_address, socket_options
        self.connects.append((host, port))
        self.streams.append(self.blocking_stream)
        return self.blocking_stream


def test_transport_propagates_cancellation_after_closing_stream() -> None:
    async def resolver(host: str, port: int) -> tuple[str, ...]:
        del host, port
        return ("1.1.1.1",)

    backend = BlockingBackend()
    transport = PublicHttpTransport(
        resolver,
        lambda: backend,
        ssl_context=_ssl_context(),
    )

    async def cancel_during_read() -> None:
        task = asyncio.create_task(transport.get("https://example.com/"))
        await backend.blocking_stream.read_started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(cancel_during_read())

    assert backend.blocking_stream.closed is True
