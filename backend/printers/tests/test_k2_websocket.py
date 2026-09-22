"""Tests for the K2 CFS WebSocket reader -- no network, sockets are faked.

Two layers are covered: the minimal RFC 6455 framing (handshake + masked text
frames + ping/pong/close) and the CFS parsing/request flow. Nothing here opens a
real socket.
"""

from __future__ import annotations

import base64
import hashlib

import pytest

from printers.base import PrinterConnectionError
from printers.k2_websocket import (
    MAX_FRAMES,
    WebSocketError,
    _WebSocketClient,
    parse_boxs_info,
    read_cfs_slots,
)

_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

CFS_PAYLOAD = {
    "materialBoxs": [
        {  # spool holder -- must be skipped
            "id": 0,
            "type": 1,
            "state": 0,
            "materials": [{"id": 0, "state": 1, "type": "PLA"}],
        },
        {
            "id": 1,
            "type": 0,
            "state": 1,
            "temp": 30,
            "humidity": 40,
            "materials": [
                {
                    "id": 0,
                    "vendor": "Creality",
                    "type": "PLA",
                    "name": "Hyper PLA",
                    "color": "#0ffffff",
                    "state": 2,
                    "percent": 100,
                },
                {"id": 1, "state": 0},
                {"id": 2, "vendor": "eSun", "type": "PETG", "name": "PETG", "state": 1},
                {"id": 3, "state": 0},
            ],
        },
        {
            "id": 2,
            "type": 0,
            "state": 1,
            "materials": [
                {"id": 0, "vendor": "X", "type": "ABS", "name": "ABS", "state": 1},
            ],
        },
    ]
}


# ---------------------------------------------------------------------------
# Fake sockets / clients
# ---------------------------------------------------------------------------


class FakeSocket:
    def __init__(self, chunks: list[bytes] | None = None) -> None:
        self._chunks = list(chunks or [])
        self.sent = bytearray()
        self.closed = False
        self.timeout = None

    def settimeout(self, value):
        self.timeout = value

    def sendall(self, data: bytes) -> None:
        self.sent.extend(data)

    def recv(self, n: int) -> bytes:
        if not self._chunks:
            return b""
        chunk = self._chunks[0]
        if len(chunk) <= n:
            self._chunks.pop(0)
            return chunk
        self._chunks[0] = chunk[n:]
        return chunk[:n]

    def close(self) -> None:
        self.closed = True


class HandshakeSocket(FakeSocket):
    """Returns a handshake response computed from the client's request."""

    def __init__(self, response_builder) -> None:
        super().__init__()
        self._response_builder = response_builder
        self._handshake_done = False

    def recv(self, n: int) -> bytes:
        if not self._handshake_done:
            self._handshake_done = True
            return self._response_builder(bytes(self.sent))
        return super().recv(n)


def _server_frame(text: str, opcode: int = 0x1) -> bytes:
    payload = text.encode()
    return bytes([0x80 | opcode, len(payload)]) + payload


def _header(request: bytes, name: str) -> str:
    for line in request.decode("ascii").split("\r\n"):
        key, _, value = line.partition(":")
        if key.strip().lower() == name.lower():
            return value.strip()
    raise AssertionError(f"header {name!r} not found in {request!r}")


class FakeWsClient:
    def __init__(self, messages: list[object]) -> None:
        self._messages = list(messages)
        self.sent: list[object] = []
        self.closed = False

    def send_json(self, payload) -> None:
        self.sent.append(payload)

    def recv_json(self):
        if not self._messages:
            raise AssertionError("the reader asked for more messages than scripted")
        return self._messages.pop(0)

    def close(self) -> None:
        self.closed = True


# ---------------------------------------------------------------------------
# RFC 6455 framing
# ---------------------------------------------------------------------------


def test_connect_completes_a_valid_handshake():
    def build(request: bytes) -> bytes:
        key = _header(request, "sec-websocket-key")
        accept = base64.b64encode(hashlib.sha1(f"{key}{_WS_GUID}".encode()).digest()).decode()
        return (
            "HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\n"
            f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
        ).encode()

    sock = HandshakeSocket(build)
    client = _WebSocketClient.connect(
        "k2.local", 9999, socket_factory=lambda addr, timeout=None: sock
    )

    assert isinstance(client, _WebSocketClient)
    assert b"GET / HTTP/1.1" in sock.sent
    assert b"Sec-WebSocket-Version: 13" in sock.sent
    assert sock.closed is False


def test_connect_keeps_frames_sent_with_the_handshake():
    def build(request: bytes) -> bytes:
        key = _header(request, "sec-websocket-key")
        accept = base64.b64encode(hashlib.sha1(f"{key}{_WS_GUID}".encode()).digest()).decode()
        handshake = (
            f"HTTP/1.1 101 Switching Protocols\r\nSec-WebSocket-Accept: {accept}\r\n\r\n"
        ).encode()
        # The printer pushes a frame in the same TCP segment as the handshake.
        return handshake + _server_frame("early")

    sock = HandshakeSocket(build)
    client = _WebSocketClient.connect(
        "k2.local", 9999, socket_factory=lambda addr, timeout=None: sock
    )

    assert client.recv_text() == "early"


def test_connect_rejects_a_non_101_response():
    sock = HandshakeSocket(lambda request: b"HTTP/1.1 403 Forbidden\r\n\r\n")
    with pytest.raises(WebSocketError):
        _WebSocketClient.connect("k2.local", 9999, socket_factory=lambda addr, timeout=None: sock)
    assert sock.closed is True


def test_connect_rejects_an_invalid_accept_key():
    sock = HandshakeSocket(
        lambda request: b"HTTP/1.1 101 Switching Protocols\r\nSec-WebSocket-Accept: wrong\r\n\r\n"
    )
    with pytest.raises(WebSocketError):
        _WebSocketClient.connect("k2.local", 9999, socket_factory=lambda addr, timeout=None: sock)


def test_send_text_produces_a_masked_text_frame():
    sock = FakeSocket()
    client = _WebSocketClient(sock)

    client.send_text("hi")

    frame = bytes(sock.sent)
    assert frame[0] == 0x81  # FIN + text
    assert frame[1] & 0x80  # MASK bit set
    assert (frame[1] & 0x7F) == 2
    mask = frame[2:6]
    payload = frame[6:8]
    unmasked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
    assert unmasked == b"hi"


def test_recv_text_returns_the_payload():
    client = _WebSocketClient(FakeSocket([_server_frame("hello")]))
    assert client.recv_text() == "hello"


def test_recv_text_answers_ping_then_reads_the_message():
    sock = FakeSocket([_server_frame("ping", opcode=0x9), _server_frame("hi")])
    client = _WebSocketClient(sock)

    assert client.recv_text() == "hi"
    assert sock.sent[0] == 0x8A  # FIN + pong


def test_recv_text_raises_on_a_close_frame():
    client = _WebSocketClient(FakeSocket([_server_frame("", opcode=0x8)]))
    with pytest.raises(WebSocketError):
        client.recv_text()


def test_recv_raises_connection_error_on_eof():
    client = _WebSocketClient(FakeSocket([]))
    with pytest.raises(PrinterConnectionError):
        client.recv_text()


# ---------------------------------------------------------------------------
# CFS parsing
# ---------------------------------------------------------------------------


def test_parse_boxs_info_maps_boxes_and_skips_the_spool_holder():
    slots = parse_boxs_info(CFS_PAYLOAD)

    assert [slot.index for slot in slots] == [0, 1, 2, 3, 4]
    assert slots[0].material == "PLA"
    assert slots[0].brand == "Creality"
    assert slots[0].name == "Hyper PLA"
    assert slots[0].empty is False
    assert slots[1].empty is True
    assert slots[4].index == 4
    assert slots[4].material == "ABS"


def test_parse_boxs_info_rejects_invalid_payloads():
    with pytest.raises(WebSocketError):
        parse_boxs_info("not an object")
    with pytest.raises(WebSocketError):
        parse_boxs_info({})


# ---------------------------------------------------------------------------
# read_cfs_slots
# ---------------------------------------------------------------------------


def test_read_cfs_slots_skips_telemetry_until_boxs_info():
    client = FakeWsClient([{"nozzleTemp": "30"}, {"boxsInfo": CFS_PAYLOAD}])

    slots = read_cfs_slots("k2.local", connect=lambda host, port, *, timeout: client)

    assert client.sent == [{"method": "get", "params": {"boxsInfo": 1}}]
    assert len(slots) == 5
    assert client.closed is True


def test_read_cfs_slots_raises_when_boxs_info_never_arrives():
    client = FakeWsClient([{"telemetry": index} for index in range(MAX_FRAMES)])

    with pytest.raises(WebSocketError):
        read_cfs_slots("k2.local", connect=lambda host, port, *, timeout: client)

    assert client.closed is True


def test_read_cfs_slots_closes_on_a_malformed_reply():
    client = FakeWsClient([{"boxsInfo": "nope"}])

    with pytest.raises(WebSocketError):
        read_cfs_slots("k2.local", connect=lambda host, port, *, timeout: client)

    assert client.closed is True
