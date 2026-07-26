from __future__ import annotations

import asyncio
import errno
import socket
import ssl
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from websockets.asyncio.client import connect as websocket_connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

from hermes_reach.connector.errors import ConnectorError, ConnectorErrorCode
from hermes_reach.connector.identity import DevicePrivateIdentity
from hermes_reach.connector.protocol import (
    ErrorFrame,
    GrantScope,
    OperationInvocationV1,
    PairingChallenge,
    PairingInit,
    create_pairing_challenge,
    create_pairing_init,
    create_signed_request,
    encode_record,
    protect_operation_call,
    record_digest,
)
from hermes_reach.connector.tls import (
    ConnectorTLSStore,
    build_pinned_client_context,
)
from hermes_reach.connector.transport import (
    WSS_SUBPROTOCOL,
    PairingExchange,
    PairingWssClient,
    PinnedWssClient,
    WssConnection,
    WssEndpoint,
    WssServer,
    _exchange_record,
    _serve_connection,
)
from hermes_reach.contracts import validate_read

_NOW = int(time.time())
_VPS_SEED = bytes(range(32))
_CONNECTOR_SEED = bytes(range(32, 64))
_OTHER_CONNECTOR_SEED = bytes(range(64, 96))
_PAIRING_MESSAGE_ID = "caireeyuculbogazdinryhi6d4"
_PAIRING_ID = "aaaqeayeaudaocajbifqydiob4"
_REQUEST_MESSAGE_ID = "oj2xe43uov3ho6dzpj5xy7l6p4"
_REQUEST_ID = "mfyha3dspb2hq5dimvwgy3zao4"
_GRANT_ID = "ibaueq2eivdeoscjjjfuytkoj4"
_ERROR_MESSAGE_ID = "x3ahb4hq6dypb4hq6dypb4irce"


def _identity(seed: bytes) -> DevicePrivateIdentity:
    return DevicePrivateIdentity._from_seed_for_testing(seed)


def _assert_code(error: pytest.ExceptionInfo[ConnectorError], code: str) -> None:
    assert error.value.code == code


def _require_loopback_bind() -> None:
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind(("127.0.0.1", 0))
    except OSError as error:
        if error.errno in {errno.EACCES, errno.EPERM}:
            pytest.skip("The execution sandbox forbids loopback listener binds.")
        raise
    finally:
        probe.close()


def _pairing_init() -> PairingInit:
    return create_pairing_init(
        signer=_identity(_VPS_SEED),
        message_id=_PAIRING_MESSAGE_ID,
        pairing_id=_PAIRING_ID,
        device_label="reach-vps-1",
        endpoint_digest="11" * 32,
        vps_nonce=bytes(range(32)),
        requested_scopes=(
            GrantScope(
                source="web",
                operation="read.url",
                data_scope="public",
                capability_id=None,
            ),
        ),
        grant_expires_at=_NOW + 60,
        grant_max_uses=1,
        issued_at=_NOW,
        deadline=_NOW + 30,
    )


def _invocation() -> OperationInvocationV1:
    connector = _identity(_CONNECTOR_SEED)
    payload = protect_operation_call(
        validate_read(
            {
                "source": "web",
                "operation": "read.url",
                "target": {"url": "https://example.com/article"},
            }
        )
    )
    request = create_signed_request(
        signer=_identity(_VPS_SEED),
        message_id=_REQUEST_MESSAGE_ID,
        request_id=_REQUEST_ID,
        trace_id="0" * 32,
        audience_key_id=connector.public_identity.key_id,
        grant_id=_GRANT_ID,
        grant_revision=1,
        policy_revision=1,
        source="web",
        operation="read.url",
        issued_at=_NOW,
        deadline=_NOW + 30,
        protected_payload=payload,
    )
    return OperationInvocationV1(request.message_id, request, payload)


class _FakeConnection:
    def __init__(
        self,
        *,
        response: str | bytes,
        subprotocol: str | None = str(WSS_SUBPROTOCOL),
        peer_certificate_der: bytes = b"fixture-leaf",
        wait_for_receive: bool = False,
    ) -> None:
        self.subprotocol = subprotocol
        self.peer_certificate_der = peer_certificate_der
        self._response = response
        self._wait_for_receive = wait_for_receive
        self.receive_started = asyncio.Event()
        self.release_receive = asyncio.Event()
        self.sent: list[str] = []
        self.close_codes: list[int] = []

    async def send_text(self, value: str) -> None:
        self.sent.append(value)

    async def receive(self) -> str | bytes:
        self.receive_started.set()
        if self._wait_for_receive:
            await self.release_receive.wait()
        return self._response

    async def close(self, code: int) -> None:
        self.close_codes.append(code)


class _FakeDialer:
    def __init__(self, connection: _FakeConnection) -> None:
        self.connection = connection
        self.contexts: list[ssl.SSLContext] = []
        self.endpoints: list[WssEndpoint] = []

    @asynccontextmanager
    async def open(
        self, endpoint: WssEndpoint, context: ssl.SSLContext
    ) -> AsyncIterator[WssConnection]:
        self.endpoints.append(endpoint)
        self.contexts.append(context)
        yield self.connection


class _FakeServerConnection:
    def __init__(
        self,
        message: str | bytes,
        *,
        subprotocol: str | None = str(WSS_SUBPROTOCOL),
    ) -> None:
        self.subprotocol = subprotocol
        self._message = message
        self.sent: list[str] = []
        self.close_calls: list[tuple[int, str]] = []

    async def recv(self) -> str | bytes:
        return self._message

    async def send(self, value: str) -> None:
        self.sent.append(value)

    async def close(self, *, code: int, reason: str) -> None:
        self.close_calls.append((code, reason))


class _FakeServer:
    def __init__(self) -> None:
        self.close_calls: list[bool] = []
        self.closed = asyncio.Event()

    def close(self, *, close_connections: bool) -> None:
        self.close_calls.append(close_connections)

    async def wait_closed(self) -> None:
        self.closed.set()


def _error_response() -> str:
    return encode_record(
        ErrorFrame(_ERROR_MESSAGE_ID, ConnectorErrorCode.CONNECTOR_PROTOCOL_MISMATCH)
    ).decode("ascii")


def test_wss_endpoint_requires_canonical_private_numeric_wss_uri() -> None:
    endpoint = WssEndpoint.parse("wss://127.0.0.1:8765")
    assert endpoint.host == "127.0.0.1"
    assert endpoint.port == 8765
    assert endpoint.uri == "wss://127.0.0.1:8765"
    assert WssEndpoint.parse("wss://[::1]:8765/").uri == "wss://[::1]:8765"

    for value in (
        "ws://127.0.0.1:8765",
        "wss://localhost:8765",
        "wss://8.8.8.8:8765",
        "wss://127.0.0.1",
        "wss://user@127.0.0.1:8765",
        "wss://127.0.0.1:8765/path",
        "wss://127.0.0.1:8765?query=1",
        "wss://127.0.0.1:8765#fragment",
        "wss://127.0.0.1:0001",
    ):
        with pytest.raises(ConnectorError) as caught:
            WssEndpoint.parse(value)
        _assert_code(caught, ConnectorErrorCode.CONNECTOR_TLS_FAILED.value)


def test_pairing_transport_is_unpinned_and_rejects_operation_input() -> None:
    connection = _FakeConnection(response=_error_response())
    dialer = _FakeDialer(connection)
    client = PairingWssClient(WssEndpoint.parse("wss://127.0.0.1:8765"), dialer=dialer)

    with pytest.raises(TypeError):
        asyncio.run(client.exchange(_invocation(), deadline=time.monotonic() + 1))
    assert dialer.contexts == []

    with pytest.raises(ConnectorError) as caught:
        asyncio.run(client.exchange(_pairing_init(), deadline=time.monotonic() + 1))
    _assert_code(caught, ConnectorErrorCode.CONNECTOR_PROTOCOL_MISMATCH.value)
    assert dialer.contexts[0].verify_mode == ssl.CERT_NONE
    assert not dialer.contexts[0].check_hostname
    assert connection.close_codes == [1000]


def test_pairing_poll_accepts_only_the_byte_identical_pending_challenge(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connector = _identity(_CONNECTOR_SEED)
    authority = ConnectorTLSStore(tmp_path / "connector", _platform="linux").initialize(
        connector, now=_NOW
    )
    pairing_init = _pairing_init()
    challenge = create_pairing_challenge(
        signer=connector,
        message_id=_REQUEST_MESSAGE_ID,
        pairing_id=pairing_init.pairing_id,
        init_digest=record_digest(pairing_init),
        vps_key_id=pairing_init.vps_key_id,
        connector_nonce=bytes(range(32, 64)),
        tls_ca_der=authority.der,
        tls_leaf_fingerprint="22" * 32,
        issued_at=_NOW,
        deadline=pairing_init.deadline,
    )
    connection = _FakeConnection(response=encode_record(challenge).decode("ascii"))
    client = PairingWssClient(
        WssEndpoint.parse("wss://127.0.0.1:8765"),
        dialer=_FakeDialer(connection),
        wall_clock=lambda: _NOW,
    )
    monkeypatch.setattr(
        "hermes_reach.connector.transport.verify_connector_leaf_der",
        lambda *_args, **_kwargs: "22" * 32,
    )

    result = asyncio.run(
        client.poll(
            pairing_init,
            PairingExchange(challenge, authority, "22" * 32),
            deadline=time.monotonic() + 1,
        )
    )

    assert result is None
    assert connection.sent == [encode_record(pairing_init).decode("ascii")]


def test_pinned_transport_validates_leaf_before_sending_and_rejects_pairing_input(
    tmp_path: Path,
) -> None:
    connector = _identity(_CONNECTOR_SEED)
    authority = ConnectorTLSStore(tmp_path / "connector", _platform="linux").initialize(
        connector, now=_NOW
    )
    connection = _FakeConnection(
        response=_error_response(), peer_certificate_der=b"bad"
    )
    dialer = _FakeDialer(connection)
    client = PinnedWssClient(
        WssEndpoint.parse("wss://127.0.0.1:8765"),
        authority,
        dialer=dialer,
        wall_clock=lambda: _NOW,
    )

    with pytest.raises(TypeError):
        asyncio.run(client.exchange(_pairing_init(), deadline=time.monotonic() + 1))
    assert dialer.contexts == []

    with pytest.raises(ConnectorError) as caught:
        asyncio.run(client.exchange(_invocation(), deadline=time.monotonic() + 1))
    _assert_code(caught, ConnectorErrorCode.CONNECTOR_TLS_FAILED.value)
    assert dialer.contexts[0].verify_mode == ssl.CERT_REQUIRED
    assert connection.sent == []
    assert connection.close_codes == [1002]


def test_exchange_rejects_subprotocol_and_binary_frames_with_protocol_close() -> None:
    endpoint = WssEndpoint.parse("wss://127.0.0.1:8765")
    context = ssl.create_default_context()
    request = _pairing_init()
    wrong_subprotocol = _FakeConnection(response=_error_response(), subprotocol=None)

    with pytest.raises(ConnectorError) as subprotocol_error:
        asyncio.run(
            _exchange_record(
                _FakeDialer(wrong_subprotocol),
                endpoint,
                context,
                request,
                deadline=time.monotonic() + 1,
            )
        )
    _assert_code(
        subprotocol_error, ConnectorErrorCode.CONNECTOR_PROTOCOL_MISMATCH.value
    )
    assert wrong_subprotocol.sent == []
    assert wrong_subprotocol.close_codes == [1002]

    binary = _FakeConnection(response=b"not-json")
    with pytest.raises(ConnectorError) as binary_error:
        asyncio.run(
            _exchange_record(
                _FakeDialer(binary),
                endpoint,
                context,
                request,
                deadline=time.monotonic() + 1,
            )
        )
    _assert_code(binary_error, ConnectorErrorCode.CONNECTOR_PROTOCOL_MISMATCH.value)
    assert len(binary.sent) == 1
    assert binary.close_codes == [1002]


def test_exchange_rejects_an_expired_deadline_before_dialing() -> None:
    async def exchange() -> None:
        connection = _FakeConnection(response=_error_response())
        dialer = _FakeDialer(connection)
        with pytest.raises(ConnectorError) as caught:
            await _exchange_record(
                dialer,
                WssEndpoint.parse("wss://127.0.0.1:8765"),
                ssl.create_default_context(),
                _pairing_init(),
                deadline=asyncio.get_running_loop().time() - 0.01,
            )
        _assert_code(caught, ConnectorErrorCode.CONNECTOR_DEADLINE_EXCEEDED.value)
        assert dialer.contexts == []

    asyncio.run(exchange())


def test_exchange_cancellation_closes_the_connection_and_reraises() -> None:
    async def exchange() -> None:
        connection = _FakeConnection(response=_error_response(), wait_for_receive=True)
        task = asyncio.create_task(
            _exchange_record(
                _FakeDialer(connection),
                WssEndpoint.parse("wss://127.0.0.1:8765"),
                ssl.create_default_context(),
                _pairing_init(),
                deadline=asyncio.get_running_loop().time() + 5,
            )
        )
        await connection.receive_started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert connection.close_codes == [1008]

    asyncio.run(exchange())


def test_server_record_timeout_and_binary_frame_use_explicit_closes() -> None:
    async def exchange() -> None:
        async def response(record: object) -> ErrorFrame:
            del record
            return ErrorFrame(
                _ERROR_MESSAGE_ID, ConnectorErrorCode.CONNECTOR_PROTOCOL_MISMATCH
            )

        wrong_subprotocol = _FakeServerConnection(
            encode_record(_pairing_init()).decode("ascii"),
            subprotocol=None,
        )
        await _serve_connection(
            wrong_subprotocol,
            response,
            request_timeout=1,
            message_idle_timeout=1,
        )
        assert wrong_subprotocol.sent == []
        assert wrong_subprotocol.close_calls == [(1002, "")]

        binary = _FakeServerConnection(b"unexpected-binary")
        await _serve_connection(
            binary,
            response,
            request_timeout=1,
            message_idle_timeout=1,
        )
        assert binary.sent == []
        assert binary.close_calls == [(1003, "")]

        waiting = asyncio.Event()

        async def blocked_response(record: object) -> ErrorFrame:
            del record
            await waiting.wait()
            return ErrorFrame(
                _ERROR_MESSAGE_ID, ConnectorErrorCode.CONNECTOR_PROTOCOL_MISMATCH
            )

        timed_out = _FakeServerConnection(
            encode_record(_pairing_init()).decode("ascii")
        )
        await _serve_connection(
            timed_out,
            blocked_response,
            request_timeout=0.01,
            message_idle_timeout=1,
        )
        assert timed_out.close_calls == [(1008, "")]

    asyncio.run(exchange())


def test_wss_server_close_stops_connections_before_disposing_tls_material(
    tmp_path: Path,
) -> None:
    async def close_server() -> None:
        connector = _identity(_CONNECTOR_SEED)
        store = ConnectorTLSStore(tmp_path / "connector", _platform="linux")
        store.initialize(connector, now=_NOW)
        material = store.create_unlock_material(
            connector, bind_host="127.0.0.1", now=_NOW
        )
        listener = _FakeServer()
        server = WssServer(
            listener,  # type: ignore[arg-type]
            material,
            host="127.0.0.1",
            port=8765,
            wall_clock=lambda: _NOW,
        )
        await server.close()
        assert listener.close_calls == [True]
        assert listener.closed.is_set()
        with pytest.raises(ConnectorError) as caught:
            _ = material.server_context
        _assert_code(caught, ConnectorErrorCode.CONNECTOR_KEY_LOCKED.value)

    asyncio.run(close_server())


def test_pinned_wss_round_trip_only_trusts_the_connector_ca(tmp_path: Path) -> None:
    _require_loopback_bind()

    async def exchange() -> None:
        connector = _identity(_CONNECTOR_SEED)
        store = ConnectorTLSStore(tmp_path / "connector", _platform="linux")
        authority = store.initialize(connector, now=_NOW)
        material = store.create_unlock_material(
            connector, bind_host="127.0.0.1", now=_NOW
        )

        async def handler(record: object) -> ErrorFrame:
            assert isinstance(record, OperationInvocationV1)
            return ErrorFrame(
                _ERROR_MESSAGE_ID, ConnectorErrorCode.CONNECTOR_PROTOCOL_MISMATCH
            )

        server = await WssServer.start(
            bind_host="127.0.0.1",
            port=0,
            material=material,
            handler=handler,
            wall_clock=lambda: _NOW,
        )
        try:
            response = await PinnedWssClient(
                server.endpoint,
                authority,
                wall_clock=lambda: _NOW,
            ).exchange(_invocation(), deadline=asyncio.get_running_loop().time() + 5)
            assert isinstance(response, ErrorFrame)
            assert response.code is ConnectorErrorCode.CONNECTOR_PROTOCOL_MISMATCH
        finally:
            await server.close()

    asyncio.run(exchange())


def test_pinned_wss_rejects_a_different_public_ca(tmp_path: Path) -> None:
    _require_loopback_bind()

    async def exchange() -> None:
        expected_connector = _identity(_CONNECTOR_SEED)
        expected_store = ConnectorTLSStore(tmp_path / "expected", _platform="linux")
        expected_authority = expected_store.initialize(expected_connector, now=_NOW)
        substituted_connector = _identity(_OTHER_CONNECTOR_SEED)
        substituted_store = ConnectorTLSStore(
            tmp_path / "substituted", _platform="linux"
        )
        substituted_store.initialize(substituted_connector, now=_NOW)
        material = substituted_store.create_unlock_material(
            substituted_connector, bind_host="127.0.0.1", now=_NOW
        )

        async def handler(record: object) -> ErrorFrame:
            del record
            return ErrorFrame(
                _ERROR_MESSAGE_ID, ConnectorErrorCode.CONNECTOR_PROTOCOL_MISMATCH
            )

        server = await WssServer.start(
            bind_host="127.0.0.1",
            port=0,
            material=material,
            handler=handler,
            wall_clock=lambda: _NOW,
        )
        try:
            with pytest.raises(ConnectorError) as caught:
                await PinnedWssClient(
                    server.endpoint,
                    expected_authority,
                    wall_clock=lambda: _NOW,
                ).exchange(
                    _invocation(),
                    deadline=asyncio.get_running_loop().time() + 5,
                )
            _assert_code(caught, ConnectorErrorCode.CONNECTOR_TLS_FAILED.value)
        finally:
            await server.close()

    asyncio.run(exchange())


def test_wss_server_enforces_subprotocol_binary_and_idle_limits(tmp_path: Path) -> None:
    _require_loopback_bind()

    async def exchange() -> None:
        connector = _identity(_CONNECTOR_SEED)
        store = ConnectorTLSStore(tmp_path / "connector", _platform="linux")
        authority = store.initialize(connector, now=_NOW)
        material = store.create_unlock_material(
            connector, bind_host="127.0.0.1", now=_NOW
        )

        async def handler(record: object) -> ErrorFrame:
            del record
            return ErrorFrame(
                _ERROR_MESSAGE_ID, ConnectorErrorCode.CONNECTOR_PROTOCOL_MISMATCH
            )

        server = await WssServer.start(
            bind_host="127.0.0.1",
            port=0,
            material=material,
            handler=handler,
            wall_clock=lambda: _NOW,
            message_idle_timeout=0.05,
        )
        context = build_pinned_client_context(authority)
        try:
            async with websocket_connect(
                server.endpoint.uri,
                ssl=context,
                server_hostname=None,
                proxy=None,
                subprotocols=(WSS_SUBPROTOCOL,),
                compression=None,
            ) as binary_connection:
                await binary_connection.send(b"unexpected-binary")
                with pytest.raises(ConnectionClosed) as binary_closed:
                    await binary_connection.recv()
                assert binary_closed.value.rcvd is not None
                assert binary_closed.value.rcvd.code == 1003

            async with websocket_connect(
                server.endpoint.uri,
                ssl=context,
                server_hostname=None,
                proxy=None,
                subprotocols=(WSS_SUBPROTOCOL,),
                compression=None,
            ) as idle_connection:
                with pytest.raises(ConnectionClosed) as idle_closed:
                    await idle_connection.recv()
                assert idle_closed.value.rcvd is not None
                assert idle_closed.value.rcvd.code == 1008

            with pytest.raises(InvalidStatus) as wrong_protocol_rejected:
                async with websocket_connect(
                    server.endpoint.uri,
                    ssl=context,
                    server_hostname=None,
                    proxy=None,
                    subprotocols=("wrong-protocol",),
                    compression=None,
                ):
                    pass
            assert wrong_protocol_rejected.value.response.status_code == 400
        finally:
            await server.close()

    asyncio.run(exchange())


def test_pairing_rejects_initial_certificate_substitution_without_a_pin(
    tmp_path: Path,
) -> None:
    _require_loopback_bind()

    async def exchange() -> None:
        connector = _identity(_CONNECTOR_SEED)
        trusted_store = ConnectorTLSStore(tmp_path / "trusted", _platform="linux")
        trusted_authority = trusted_store.initialize(connector, now=_NOW)
        trusted_material = trusted_store.create_unlock_material(
            connector, bind_host="127.0.0.1", now=_NOW
        )
        substituted_connector = _identity(_OTHER_CONNECTOR_SEED)
        substituted_store = ConnectorTLSStore(
            tmp_path / "substituted", _platform="linux"
        )
        substituted_store.initialize(substituted_connector, now=_NOW)
        substituted_material = substituted_store.create_unlock_material(
            substituted_connector, bind_host="127.0.0.1", now=_NOW
        )

        async def handler(record: object) -> PairingChallenge:
            assert isinstance(record, PairingInit)
            return create_pairing_challenge(
                signer=connector,
                message_id=_REQUEST_MESSAGE_ID,
                pairing_id=record.pairing_id,
                init_digest=record_digest(record),
                vps_key_id=record.vps_key_id,
                connector_nonce=bytes(range(32, 64)),
                tls_ca_der=trusted_authority.der,
                tls_leaf_fingerprint=trusted_material.leaf_fingerprint,
                issued_at=_NOW + 1,
                deadline=record.deadline,
            )

        server = await WssServer.start(
            bind_host="127.0.0.1",
            port=0,
            material=substituted_material,
            handler=handler,
            wall_clock=lambda: _NOW,
        )
        try:
            with pytest.raises(ConnectorError) as caught:
                await PairingWssClient(
                    server.endpoint, wall_clock=lambda: _NOW + 1
                ).exchange(
                    _pairing_init(), deadline=asyncio.get_running_loop().time() + 5
                )
            _assert_code(caught, ConnectorErrorCode.CONNECTOR_TLS_FAILED.value)
        finally:
            trusted_material.close()
            await server.close()

    asyncio.run(exchange())
