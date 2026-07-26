"""Bounded WSS-only Connector transport with separate pairing and pinned clients."""

from __future__ import annotations

import asyncio
import socket
import ssl
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from typing import Final, Protocol
from urllib.parse import urlsplit

from websockets.asyncio.client import ClientConnection
from websockets.asyncio.client import connect as websocket_connect
from websockets.asyncio.server import Server, ServerConnection
from websockets.asyncio.server import serve as websocket_serve
from websockets.exceptions import (
    ConnectionClosed,
    InvalidHandshake,
    InvalidStatus,
    InvalidURI,
    NegotiationError,
    PayloadTooBig,
)
from websockets.typing import Subprotocol

from .errors import ConnectorError, ConnectorErrorCode
from .limits import MAX_FRAME_BYTES
from .protocol import (
    ErrorFrame,
    OperationInvocationV1,
    OperationResponseV1,
    PairingChallenge,
    PairingInit,
    PairingResolution,
    ProtocolValidationError,
    WireRecord,
    encode_record,
    pairing_ca_der,
    parse_record,
    record_digest,
    verify_pairing_challenge,
    verify_pairing_resolution,
)
from .tls import (
    ConnectorCACertificate,
    EphemeralTLSMaterial,
    build_initial_pairing_client_context,
    build_pinned_client_context,
    validate_private_bind_host,
    verify_connector_ca_der,
    verify_connector_leaf_der,
)

WSS_SUBPROTOCOL: Final[Subprotocol] = Subprotocol("reach-connector.v1")
_OPEN_TIMEOUT_SECONDS: Final = 5.0
_TLS_HANDSHAKE_TIMEOUT_SECONDS: Final = 5.0
_PING_INTERVAL_SECONDS: Final = 20.0
_PING_TIMEOUT_SECONDS: Final = 10.0
_CLOSE_TIMEOUT_SECONDS: Final = 3.0
_MESSAGE_IDLE_TIMEOUT_SECONDS: Final = 30.0
_REQUEST_TIMEOUT_SECONDS: Final = 60.0
_WRITE_LIMIT: Final = (32 * 1024, 8 * 1024)
_MAX_QUEUE: Final = (1, 0)

_CLOSE_NORMAL: Final = 1000
_CLOSE_PROTOCOL: Final = 1002
_CLOSE_BINARY: Final = 1003
_CLOSE_POLICY: Final = 1008
_CLOSE_OVERSIZE: Final = 1009
_CLOSE_INTERNAL: Final = 1011


@dataclass(frozen=True, slots=True)
class WssEndpoint:
    """Canonical private numeric WSS endpoint without discovery or URL metadata."""

    host: str
    port: int
    uri: str

    @classmethod
    def parse(cls, value: str) -> WssEndpoint:
        if type(value) is not str or not value.startswith("wss://"):
            raise ConnectorError(ConnectorErrorCode.CONNECTOR_TLS_FAILED)
        try:
            parsed = urlsplit(value)
            port = parsed.port
        except ValueError:
            raise ConnectorError(ConnectorErrorCode.CONNECTOR_TLS_FAILED) from None
        if (
            parsed.scheme != "wss"
            or parsed.username is not None
            or parsed.password is not None
            or parsed.hostname is None
            or port is None
            or not 1 <= port <= 65535
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ConnectorError(ConnectorErrorCode.CONNECTOR_TLS_FAILED)
        address = validate_private_bind_host(parsed.hostname)
        host = str(address)
        authority = f"[{host}]" if address.version == 6 else host
        uri = f"wss://{authority}:{port}"
        if value not in {uri, f"{uri}/"}:
            raise ConnectorError(ConnectorErrorCode.CONNECTOR_TLS_FAILED)
        return cls(host, port, uri)


class ConnectorTransport(Protocol):
    """Injectable pinned operation exchange used by the future VPS client."""

    async def exchange(
        self, invocation: OperationInvocationV1, *, deadline: float
    ) -> OperationResponseV1 | ErrorFrame: ...


class WssConnection(Protocol):
    @property
    def subprotocol(self) -> str | None: ...

    @property
    def peer_certificate_der(self) -> bytes: ...

    async def send_text(self, value: str) -> None: ...

    async def receive(self) -> str | bytes: ...

    async def close(self, code: int) -> None: ...


class WssDialer(Protocol):
    def open(
        self, endpoint: WssEndpoint, context: ssl.SSLContext
    ) -> AbstractAsyncContextManager[WssConnection]: ...


class _ClientConnectionAdapter:
    __slots__ = ("_connection",)

    def __init__(self, connection: ClientConnection) -> None:
        self._connection = connection

    @property
    def subprotocol(self) -> str | None:
        return self._connection.subprotocol

    @property
    def peer_certificate_der(self) -> bytes:
        ssl_object = self._connection.transport.get_extra_info("ssl_object")
        if not isinstance(ssl_object, ssl.SSLObject | ssl.SSLSocket):
            raise ConnectorError(ConnectorErrorCode.CONNECTOR_TLS_FAILED)
        payload = ssl_object.getpeercert(binary_form=True)
        if not isinstance(payload, bytes) or not payload:
            raise ConnectorError(ConnectorErrorCode.CONNECTOR_TLS_FAILED)
        return payload

    async def send_text(self, value: str) -> None:
        await self._connection.send(value)

    async def receive(self) -> str | bytes:
        return await self._connection.recv()

    async def close(self, code: int) -> None:
        await self._connection.close(code=code, reason="")


class WebsocketsDialer:
    """Direct numeric-socket dialer that cannot use proxies or follow redirects."""

    @asynccontextmanager
    async def open(
        self, endpoint: WssEndpoint, context: ssl.SSLContext
    ) -> AsyncIterator[WssConnection]:
        family = socket.AF_INET6 if ":" in endpoint.host else socket.AF_INET
        connection_socket = socket.socket(family, socket.SOCK_STREAM)
        connection_socket.setblocking(False)
        try:
            loop = asyncio.get_running_loop()
            await loop.sock_connect(connection_socket, (endpoint.host, endpoint.port))
            connector = websocket_connect(
                endpoint.uri,
                ssl=context,
                server_hostname=endpoint.host,
                sock=connection_socket,
                proxy=None,
                subprotocols=(WSS_SUBPROTOCOL,),
                compression=None,
                user_agent_header=None,
                open_timeout=_OPEN_TIMEOUT_SECONDS,
                ping_interval=_PING_INTERVAL_SECONDS,
                ping_timeout=_PING_TIMEOUT_SECONDS,
                close_timeout=_CLOSE_TIMEOUT_SECONDS,
                max_size=MAX_FRAME_BYTES,
                max_queue=_MAX_QUEUE,
                write_limit=_WRITE_LIMIT,
                ssl_handshake_timeout=_TLS_HANDSHAKE_TIMEOUT_SECONDS,
            )
            async with connector as connection:
                yield _ClientConnectionAdapter(connection)
        finally:
            connection_socket.close()


@dataclass(frozen=True, slots=True)
class PairingExchange:
    """Unpersisted pairing evidence; pinning waits for PairingComplete."""

    challenge: PairingChallenge
    authority: ConnectorCACertificate
    observed_tls_leaf_fingerprint: str


class PairingWssClient:
    """Pairing-only client whose unverified TLS context cannot send operations."""

    def __init__(
        self,
        endpoint: WssEndpoint,
        *,
        dialer: WssDialer | None = None,
        wall_clock: Callable[[], int] | None = None,
    ) -> None:
        self._endpoint = endpoint
        self._dialer: WssDialer = WebsocketsDialer() if dialer is None else dialer
        self._wall_clock = _wall_timestamp if wall_clock is None else wall_clock

    async def exchange(
        self, pairing_init: PairingInit, *, deadline: float
    ) -> PairingExchange:
        if not isinstance(pairing_init, PairingInit):
            raise TypeError("The pairing transport accepts only PairingInit.")
        context = build_initial_pairing_client_context()
        response, leaf_der = await _exchange_record(
            self._dialer,
            self._endpoint,
            context,
            pairing_init,
            deadline=deadline,
        )
        if not isinstance(response, PairingChallenge):
            raise ConnectorError(ConnectorErrorCode.CONNECTOR_PROTOCOL_MISMATCH)
        now = self._wall_clock()
        try:
            connector_identity = verify_pairing_challenge(
                response,
                expected_pairing_id=pairing_init.pairing_id,
                expected_vps_key_id=pairing_init.vps_key_id,
                expected_init_digest=record_digest(pairing_init),
                observed_tls_leaf_fingerprint=response.tls_leaf_fingerprint,
                now=now,
            )
            authority = verify_connector_ca_der(
                pairing_ca_der(response), connector_identity, now=now
            )
            observed_leaf_fingerprint = verify_connector_leaf_der(
                leaf_der,
                authority,
                endpoint_host=self._endpoint.host,
                now=now,
            )
            if observed_leaf_fingerprint != response.tls_leaf_fingerprint:
                raise ConnectorError(ConnectorErrorCode.CONNECTOR_TLS_FAILED)
        except ProtocolValidationError:
            raise ConnectorError(
                ConnectorErrorCode.CONNECTOR_PROTOCOL_MISMATCH
            ) from None
        return PairingExchange(response, authority, observed_leaf_fingerprint)

    async def resolve(
        self,
        pairing_init: PairingInit,
        exchange: PairingExchange,
        *,
        deadline: float,
    ) -> PairingResolution:
        """Retrieve an approved result, rejecting a still-pending exchange."""

        resolution = await self.poll(
            pairing_init,
            exchange,
            deadline=deadline,
        )
        if resolution is None:
            raise ConnectorError(ConnectorErrorCode.CONNECTOR_PROTOCOL_MISMATCH)
        return resolution

    async def poll(
        self,
        pairing_init: PairingInit,
        exchange: PairingExchange,
        *,
        deadline: float,
    ) -> PairingResolution | None:
        """Poll the exact pending pairing without recreating or extending it."""

        if not isinstance(pairing_init, PairingInit) or not isinstance(
            exchange, PairingExchange
        ):
            raise TypeError(
                "Pairing resolution requires the original pairing exchange."
            )
        context = build_pinned_client_context(exchange.authority)
        response, leaf_der = await _exchange_record(
            self._dialer,
            self._endpoint,
            context,
            pairing_init,
            deadline=deadline,
            peer_validator=lambda payload: verify_connector_leaf_der(
                payload,
                exchange.authority,
                endpoint_host=self._endpoint.host,
                now=self._wall_clock(),
            ),
        )
        now = self._wall_clock()
        leaf_fingerprint = verify_connector_leaf_der(
            leaf_der,
            exchange.authority,
            endpoint_host=self._endpoint.host,
            now=now,
        )
        if leaf_fingerprint != exchange.observed_tls_leaf_fingerprint:
            raise ConnectorError(ConnectorErrorCode.CONNECTOR_TLS_FAILED)
        if isinstance(response, PairingChallenge):
            if response != exchange.challenge:
                raise ConnectorError(ConnectorErrorCode.CONNECTOR_PROTOCOL_MISMATCH)
            return None
        if isinstance(response, ErrorFrame):
            if response.code is ConnectorErrorCode.CONNECTOR_NOT_PAIRED:
                raise ConnectorError(ConnectorErrorCode.CONNECTOR_NOT_PAIRED)
            raise ConnectorError(ConnectorErrorCode.CONNECTOR_PROTOCOL_MISMATCH)
        if not isinstance(response, PairingResolution):
            raise ConnectorError(ConnectorErrorCode.CONNECTOR_PROTOCOL_MISMATCH)
        try:
            verify_pairing_resolution(
                response,
                pairing_init=pairing_init,
                pairing_challenge=exchange.challenge,
                observed_tls_leaf_fingerprint=leaf_fingerprint,
                now=now,
            )
        except ProtocolValidationError:
            raise ConnectorError(
                ConnectorErrorCode.CONNECTOR_PROTOCOL_MISMATCH
            ) from None
        return response


class PinnedWssClient:
    """Operation-only client that validates both the pinned CA and exact leaf."""

    def __init__(
        self,
        endpoint: WssEndpoint,
        authority: ConnectorCACertificate,
        *,
        dialer: WssDialer | None = None,
        wall_clock: Callable[[], int] | None = None,
    ) -> None:
        self._endpoint = endpoint
        self._authority = authority
        self._dialer: WssDialer = WebsocketsDialer() if dialer is None else dialer
        self._wall_clock = _wall_timestamp if wall_clock is None else wall_clock

    async def exchange(
        self, invocation: OperationInvocationV1, *, deadline: float
    ) -> OperationResponseV1 | ErrorFrame:
        if not isinstance(invocation, OperationInvocationV1):
            raise TypeError("The pinned transport accepts only an invocation.")
        context = build_pinned_client_context(self._authority)
        response, leaf_der = await _exchange_record(
            self._dialer,
            self._endpoint,
            context,
            invocation,
            deadline=deadline,
            peer_validator=lambda payload: verify_connector_leaf_der(
                payload,
                self._authority,
                endpoint_host=self._endpoint.host,
                now=self._wall_clock(),
            ),
        )
        if not isinstance(response, OperationResponseV1 | ErrorFrame):
            raise ConnectorError(ConnectorErrorCode.CONNECTOR_PROTOCOL_MISMATCH)
        return response


async def _exchange_record(
    dialer: WssDialer,
    endpoint: WssEndpoint,
    context: ssl.SSLContext,
    request: WireRecord,
    *,
    deadline: float,
    peer_validator: Callable[[bytes], object] | None = None,
) -> tuple[WireRecord, bytes]:
    if (
        type(deadline) not in {int, float}
        or deadline <= asyncio.get_running_loop().time()
    ):
        raise ConnectorError(ConnectorErrorCode.CONNECTOR_DEADLINE_EXCEEDED)
    encoded = encode_record(request)
    try:
        async with asyncio.timeout_at(float(deadline)):
            async with dialer.open(endpoint, context) as connection:
                try:
                    if connection.subprotocol != WSS_SUBPROTOCOL:
                        raise ConnectorError(
                            ConnectorErrorCode.CONNECTOR_PROTOCOL_MISMATCH
                        )
                    leaf_der = connection.peer_certificate_der
                    if peer_validator is not None:
                        peer_validator(leaf_der)
                    await connection.send_text(encoded.decode("ascii"))
                    message = await connection.receive()
                    response = _parse_text_message(message)
                    await connection.close(_CLOSE_NORMAL)
                    return response, leaf_der
                except asyncio.CancelledError:
                    await connection.close(_CLOSE_POLICY)
                    raise
                except ConnectorError:
                    await connection.close(_CLOSE_PROTOCOL)
                    raise
    except asyncio.CancelledError:
        raise
    except ConnectorError:
        raise
    except (ssl.SSLCertVerificationError, ssl.SSLError):
        raise ConnectorError(ConnectorErrorCode.CONNECTOR_TLS_FAILED) from None
    except TimeoutError:
        raise ConnectorError(ConnectorErrorCode.CONNECTOR_DEADLINE_EXCEEDED) from None
    except (
        InvalidURI,
        InvalidHandshake,
        InvalidStatus,
        NegotiationError,
        PayloadTooBig,
    ):
        raise ConnectorError(ConnectorErrorCode.CONNECTOR_PROTOCOL_MISMATCH) from None
    except (ConnectionClosed, OSError):
        raise ConnectorError(ConnectorErrorCode.CONNECTOR_OFFLINE) from None
    except Exception:
        raise ConnectorError(ConnectorErrorCode.CONNECTOR_OFFLINE) from None


def _parse_text_message(message: str | bytes) -> WireRecord:
    if isinstance(message, bytes):
        raise ConnectorError(ConnectorErrorCode.CONNECTOR_PROTOCOL_MISMATCH)
    if not isinstance(message, str):
        raise ConnectorError(ConnectorErrorCode.CONNECTOR_PROTOCOL_MISMATCH)
    try:
        encoded = message.encode("ascii")
        return parse_record(encoded)
    except (UnicodeEncodeError, ProtocolValidationError):
        raise ConnectorError(ConnectorErrorCode.CONNECTOR_PROTOCOL_MISMATCH) from None


ServerFrameHandler = Callable[[WireRecord], Awaitable[WireRecord]]


class WssServer:
    """One-record-per-connection WSS listener owned by one unlock lease."""

    def __init__(
        self,
        server: Server,
        material: EphemeralTLSMaterial,
        *,
        host: str,
        port: int,
        wall_clock: Callable[[], int],
    ) -> None:
        self._server = server
        self._material = material
        self._host = host
        self._port = port
        self._wall_clock = wall_clock
        self._closed = False
        self._close_lock = asyncio.Lock()
        self._expiry_task = asyncio.create_task(self._close_at_expiry())

    @classmethod
    async def start(
        cls,
        *,
        bind_host: str,
        port: int,
        material: EphemeralTLSMaterial,
        handler: ServerFrameHandler,
        wall_clock: Callable[[], int] | None = None,
        request_timeout: float = _REQUEST_TIMEOUT_SECONDS,
        message_idle_timeout: float = _MESSAGE_IDLE_TIMEOUT_SECONDS,
    ) -> WssServer:
        address = validate_private_bind_host(bind_host)
        if (
            type(port) is not int
            or not 0 <= port <= 65535
            or (port == 0 and not address.is_loopback)
            or not isinstance(material, EphemeralTLSMaterial)
            or not callable(handler)
            or type(request_timeout) not in {int, float}
            or request_timeout <= 0
            or type(message_idle_timeout) not in {int, float}
            or message_idle_timeout <= 0
        ):
            raise ConnectorError(ConnectorErrorCode.CONNECTOR_TLS_FAILED)
        clock = _wall_timestamp if wall_clock is None else wall_clock
        if clock() >= material.expires_at:
            material.close()
            raise ConnectorError(ConnectorErrorCode.CONNECTOR_TLS_FAILED)

        async def connection_handler(connection: ServerConnection) -> None:
            await _serve_connection(
                connection,
                handler,
                request_timeout=float(request_timeout),
                message_idle_timeout=float(message_idle_timeout),
            )

        try:
            server = await websocket_serve(
                connection_handler,
                str(address),
                port,
                ssl=material.server_context,
                subprotocols=(WSS_SUBPROTOCOL,),
                compression=None,
                origins=(None,),
                server_header=None,
                open_timeout=_OPEN_TIMEOUT_SECONDS,
                ping_interval=_PING_INTERVAL_SECONDS,
                ping_timeout=_PING_TIMEOUT_SECONDS,
                close_timeout=_CLOSE_TIMEOUT_SECONDS,
                max_size=MAX_FRAME_BYTES,
                max_queue=_MAX_QUEUE,
                write_limit=_WRITE_LIMIT,
                ssl_handshake_timeout=_TLS_HANDSHAKE_TIMEOUT_SECONDS,
            )
        except (OSError, ssl.SSLError, ValueError):
            material.close()
            raise ConnectorError(ConnectorErrorCode.CONNECTOR_OFFLINE) from None
        sockets = tuple(server.sockets)
        if not sockets:
            server.close(close_connections=True)
            await server.wait_closed()
            material.close()
            raise ConnectorError(ConnectorErrorCode.CONNECTOR_OFFLINE)
        bound_port = int(sockets[0].getsockname()[1])
        return cls(
            server,
            material,
            host=str(address),
            port=bound_port,
            wall_clock=clock,
        )

    @property
    def endpoint(self) -> WssEndpoint:
        authority = f"[{self._host}]" if ":" in self._host else self._host
        return WssEndpoint.parse(f"wss://{authority}:{self._port}")

    async def close(self) -> None:
        async with self._close_lock:
            if self._closed:
                return
            self._closed = True
            expiry_task = self._expiry_task
            if expiry_task is not asyncio.current_task():
                expiry_task.cancel()
            self._server.close(close_connections=True)
            await self._server.wait_closed()
            self._material.close()
            if expiry_task is not asyncio.current_task():
                await asyncio.gather(expiry_task, return_exceptions=True)

    async def _close_at_expiry(self) -> None:
        delay = max(0, self._material.expires_at - self._wall_clock())
        await asyncio.sleep(delay)
        await self.close()


async def _serve_connection(
    connection: ServerConnection,
    handler: ServerFrameHandler,
    *,
    request_timeout: float,
    message_idle_timeout: float,
) -> None:
    try:
        if connection.subprotocol != WSS_SUBPROTOCOL:
            await connection.close(code=_CLOSE_PROTOCOL, reason="")
            return
        async with asyncio.timeout(message_idle_timeout):
            message = await connection.recv()
        if isinstance(message, bytes):
            await connection.close(code=_CLOSE_BINARY, reason="")
            return
        try:
            request = _parse_text_message(message)
        except ConnectorError:
            await connection.close(code=_CLOSE_PROTOCOL, reason="")
            return
        async with asyncio.timeout(request_timeout):
            response = await handler(request)
        encoded = encode_record(response)
        await connection.send(encoded.decode("ascii"))
        await connection.close(code=_CLOSE_NORMAL, reason="")
    except asyncio.CancelledError:
        await connection.close(code=_CLOSE_POLICY, reason="")
        raise
    except TimeoutError:
        await connection.close(code=_CLOSE_POLICY, reason="")
    except PayloadTooBig:
        await connection.close(code=_CLOSE_OVERSIZE, reason="")
    except (ConnectionClosed, OSError):
        return
    except Exception:
        await connection.close(code=_CLOSE_INTERNAL, reason="")


def _wall_timestamp() -> int:
    return int(time.time())
