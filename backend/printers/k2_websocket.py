"""Creality K2 CFS reader over the proprietary WebSocket (terv.md 13. fejezet).

The K2 exposes a JSON WebSocket on port ``9999`` that Creality Print and the
built-in LAN dashboard use. It carries live telemetry and -- unlike Moonraker --
the Creality Filament System (CFS) slot data. The protocol is **unofficial and
reverse-engineered** (see the community projects ``k2-websocket-re`` /
``Creality-K2-websocket``); it is not documented by Creality and may change with
firmware.

The exchange this module performs is deliberately minimal:

1. connect to ``ws://<host>:9999``;
2. send ``{"method": "get", "params": {"boxsInfo": 1}}``;
3. read frames until one contains ``boxsInfo`` (the initial connection dump and
   periodic telemetry frames are skipped);
4. parse the CFS boxes into :class:`~printers.base.CfsSlot` rows.

Only the standard library is used (``socket`` + RFC 6455 framing), so the
project gains no dependency. When the printer refuses the connection, closes the
socket or answers with something unparseable, a
:class:`~printers.base.PrinterBackendError` is raised -- the reader never
fabricates slots.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import struct
from collections.abc import Callable
from typing import Any

from .base import CfsSlot, PrinterBackendError, PrinterConnectionError

__all__ = [
    "K2_WEBSOCKET_PORT",
    "WebSocketError",
    "parse_boxs_info",
    "read_cfs_slots",
]

#: Creality's proprietary WebSocket port.
K2_WEBSOCKET_PORT = 9999

#: Maximum frames read while waiting for the ``boxsInfo`` reply.
MAX_FRAMES = 200

#: RFC 6455 handshake GUID.
_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

_OP_CONTINUATION = 0x0
_OP_TEXT = 0x1
_OP_BINARY = 0x2
_OP_CLOSE = 0x8
_OP_PING = 0x9
_OP_PONG = 0xA


class WebSocketError(PrinterBackendError):
    """The WebSocket handshake or framing failed."""


class _WebSocketClient:
    """Minimal RFC 6455 client: text frames, ping/pong and close only.

    This is intentionally not a general-purpose WebSocket stack -- the CFS read
    is a single request/response exchange. ``socket_factory`` is injectable so
    tests exercise the framing without a network.
    """

    def __init__(self, sock: Any, *, timeout: float = 5.0) -> None:
        self._sock = sock
        self._timeout = float(timeout)
        self._buffer = bytearray()

    @classmethod
    def connect(
        cls,
        host: str,
        port: int,
        *,
        path: str = "/",
        timeout: float = 5.0,
        socket_factory: Callable[..., Any] | None = None,
    ) -> _WebSocketClient:
        """Open a WebSocket connection and complete the handshake."""
        if not host:
            raise PrinterConnectionError("K2 WebSocket host is not configured")
        factory = socket_factory or socket.create_connection
        try:
            sock = factory((host, port), timeout=timeout)
            sock.settimeout(timeout)
        except OSError as exc:
            raise PrinterConnectionError(
                f"Cannot reach the K2 WebSocket at {host}:{port}: {exc}"
            ) from exc

        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        try:
            sock.sendall(request.encode("ascii"))
            response, leftover = cls._read_http_response(sock)
        except OSError as exc:
            _close_quietly(sock)
            raise PrinterConnectionError(f"K2 WebSocket handshake failed: {exc}") from exc

        first_line = response.split(b"\r\n", 1)[0]
        if b" 101" not in first_line:
            _close_quietly(sock)
            raise WebSocketError(f"K2 WebSocket handshake rejected: {first_line!r}")
        headers = cls._parse_headers(response)
        expected = base64.b64encode(
            hashlib.sha1(f"{key}{_WS_GUID}".encode("ascii")).digest()
        ).decode("ascii")
        if headers.get("sec-websocket-accept", "") != expected:
            _close_quietly(sock)
            raise WebSocketError("K2 WebSocket handshake returned an invalid accept key")
        client = cls(sock, timeout=timeout)
        # The printer may already have sent frames in the same TCP segment as
        # the handshake response; keep those bytes instead of dropping them.
        client._buffer.extend(leftover)
        return client

    # -- public API ---------------------------------------------------------

    def send_json(self, payload: dict[str, Any]) -> None:
        self.send_text(json.dumps(payload, separators=(",", ":")))

    def send_text(self, text: str) -> None:
        self._send_frame(_OP_TEXT, text.encode("utf-8"))

    def recv_json(self) -> Any:
        """Read one complete text message and decode it as JSON."""
        text = self.recv_text()
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise WebSocketError("K2 WebSocket sent invalid JSON") from exc

    def recv_text(self) -> str:
        """Read one complete text message (handling ping/pong and continuation)."""
        message = bytearray()
        while True:
            fin, opcode, payload = self._read_frame()
            if opcode == _OP_CLOSE:
                raise WebSocketError("K2 WebSocket closed by the printer")
            if opcode == _OP_PING:
                self._send_frame(_OP_PONG, payload)
                continue
            if opcode == _OP_PONG:
                continue
            if opcode in (_OP_TEXT, _OP_CONTINUATION):
                message.extend(payload)
                if fin:
                    try:
                        return message.decode("utf-8")
                    except UnicodeDecodeError as exc:
                        raise WebSocketError("K2 WebSocket sent invalid UTF-8") from exc
                continue
            # Binary or unknown frame: drop it once complete.
            if fin:
                message.clear()

    def close(self) -> None:
        try:
            self._send_frame(_OP_CLOSE, b"")
        except OSError:
            pass
        finally:
            _close_quietly(self._sock)

    # -- framing ------------------------------------------------------------

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        header = bytearray()
        header.append(0x80 | opcode)  # FIN + opcode
        length = len(payload)
        if length < 126:
            header.append(0x80 | length)  # MASK + length
        elif length < 65536:
            header.append(0x80 | 126)
            header.extend(struct.pack("!H", length))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack("!Q", length))
        mask = os.urandom(4)
        header.extend(mask)
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        self._sock.sendall(bytes(header) + masked)

    def _read_frame(self) -> tuple[bool, int, bytes]:
        first, second = self._read_exact(2)
        fin = bool(first & 0x80)
        opcode = first & 0x0F
        masked = bool(second & 0x80)
        length = second & 0x7F
        if length == 126:
            length = struct.unpack("!H", self._read_exact(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self._read_exact(8))[0]
        mask = self._read_exact(4) if masked else b""
        payload = self._read_exact(length) if length else b""
        if masked:
            payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        return fin, opcode, payload

    def _read_exact(self, count: int) -> bytes:
        while len(self._buffer) < count:
            try:
                chunk = self._sock.recv(4096)
            except OSError as exc:
                raise PrinterConnectionError(f"K2 WebSocket read failed: {exc}") from exc
            if not chunk:
                raise PrinterConnectionError("K2 WebSocket connection closed unexpectedly")
            self._buffer.extend(chunk)
        data = bytes(self._buffer[:count])
        del self._buffer[:count]
        return data

    @staticmethod
    def _read_http_response(sock: Any) -> tuple[bytes, bytes]:
        """Return ``(headers, leftover)`` from the handshake response.

        ``leftover`` is whatever followed the ``\\r\\n\\r\\n`` terminator in the
        same read -- the printer may already have pushed a telemetry frame, and
        those bytes must be fed back into the frame parser, not discarded.
        """
        response = bytearray()
        while b"\r\n\r\n" not in response:
            chunk = sock.recv(4096)
            if not chunk:
                raise WebSocketError("K2 WebSocket closed during the handshake")
            response.extend(chunk)
            if len(response) > 65536:
                raise WebSocketError("K2 WebSocket handshake response is too large")
        marker = response.index(b"\r\n\r\n") + 4
        return bytes(response[:marker]), bytes(response[marker:])

    @staticmethod
    def _parse_headers(response: bytes) -> dict[str, str]:
        headers: dict[str, str] = {}
        for line in response.split(b"\r\n")[1:]:
            if b":" not in line:
                continue
            name, _, value = line.partition(b":")
            headers[name.decode("ascii", "replace").strip().lower()] = value.decode(
                "ascii", "replace"
            ).strip()
        return headers


def parse_boxs_info(payload: Any) -> list[CfsSlot]:
    """Parse a ``boxsInfo`` payload into CFS slots.

    Only CFS boxes (``type == 0``) are returned; the spool holder (``type == 1``,
    ``id == 0``) is skipped. ``index`` is a flat 0-based slot number across the
    boxes (``(box_id - 1) * 4 + material_id``) because :class:`CfsSlot` carries
    no box id; a material with ``state == 0`` is treated as empty.
    """
    if not isinstance(payload, dict):
        raise WebSocketError("K2 CFS payload is not an object")
    boxes = payload.get("materialBoxs")
    if not isinstance(boxes, list):
        raise WebSocketError("K2 CFS payload has no materialBoxs list")

    slots: list[CfsSlot] = []
    for box in boxes:
        if not isinstance(box, dict):
            continue
        if int(box.get("type") or 0) != 0:
            continue  # 1 = spool holder
        box_id = int(box.get("id") or 0)
        if box_id <= 0:
            continue
        materials = box.get("materials")
        if not isinstance(materials, list):
            continue
        for material in materials:
            if not isinstance(material, dict):
                continue
            material_id = int(material.get("id") or 0)
            state = int(material.get("state") or 0)
            slots.append(
                CfsSlot(
                    index=(box_id - 1) * 4 + material_id,
                    material=str(material.get("type") or ""),
                    brand=str(material.get("vendor") or ""),
                    name=str(material.get("name") or ""),
                    color=str(material.get("color") or ""),
                    state=str(state),
                    empty=state == 0,
                )
            )
    slots.sort(key=lambda slot: slot.index)
    return slots


def read_cfs_slots(
    host: str,
    *,
    port: int = K2_WEBSOCKET_PORT,
    timeout: float = 5.0,
    connect: Callable[..., _WebSocketClient] | None = None,
) -> list[CfsSlot]:
    """Read the CFS slots from ``host`` over the proprietary WebSocket.

    ``connect`` is injectable for tests. Raises
    :class:`~printers.base.PrinterConnectionError` when the printer is
    unreachable and :class:`WebSocketError` when the reply is malformed.
    """
    connector = connect or _WebSocketClient.connect
    client = connector(host, port, timeout=timeout)
    try:
        client.send_json({"method": "get", "params": {"boxsInfo": 1}})
        for _ in range(MAX_FRAMES):
            message = client.recv_json()
            if isinstance(message, dict) and "boxsInfo" in message:
                return parse_boxs_info(message["boxsInfo"])
        raise WebSocketError("K2 CFS WebSocket did not return boxsInfo")
    finally:
        client.close()


def _close_quietly(sock: Any) -> None:
    try:
        sock.close()
    except OSError:
        pass
