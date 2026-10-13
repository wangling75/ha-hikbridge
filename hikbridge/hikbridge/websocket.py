"""Minimal RFC 6455 server-side WebSocket, enough for the talk channel.

http.server has no WebSocket support, and pulling in a dependency for one
endpoint is not worth it. Only what the browser needs is implemented:
handshake, masked client frames, fragmentation, ping/pong and close.
"""
from __future__ import annotations

import base64
import hashlib
import struct

_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

OP_CONT = 0x0
OP_TEXT = 0x1
OP_BINARY = 0x2
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA


class WebSocketError(Exception):
    """Protocol error or peer went away."""


def accept_key(client_key: str) -> str:
    digest = hashlib.sha1((client_key + _GUID).encode("ascii")).digest()
    return base64.b64encode(digest).decode("ascii")


def _unmask(payload: bytes, mask: bytes) -> bytes:
    if not payload:
        return b""
    out = bytearray(payload)
    for i in range(len(out)):
        out[i] ^= mask[i & 3]
    return bytes(out)


class WebSocket:
    """A server-side WebSocket built on a BaseHTTPRequestHandler."""

    def __init__(self, handler) -> None:
        self._handler = handler
        self._rfile = handler.rfile
        self._wfile = handler.wfile
        self._closed = False

    # -- handshake ---------------------------------------------------------
    def handshake(self) -> None:
        key = self._handler.headers.get("Sec-WebSocket-Key")
        if not key:
            raise WebSocketError("missing Sec-WebSocket-Key")
        upgrade = (self._handler.headers.get("Upgrade") or "").lower()
        if upgrade != "websocket":
            raise WebSocketError("not a websocket upgrade")
        self._handler.send_response(101, "Switching Protocols")
        self._handler.send_header("Upgrade", "websocket")
        self._handler.send_header("Connection", "Upgrade")
        self._handler.send_header("Sec-WebSocket-Accept", accept_key(key))
        self._handler.end_headers()
        self._wfile.flush()

    # -- low level ---------------------------------------------------------
    def _read_exact(self, count: int) -> bytes:
        data = self._rfile.read(count)
        if not data or len(data) < count:
            raise WebSocketError("connection closed")
        return data

    def _send_frame(self, payload: bytes, opcode: int) -> None:
        header = bytearray()
        header.append(0x80 | opcode)
        length = len(payload)
        if length < 126:
            header.append(length)
        elif length < 65536:
            header.append(126)
            header += struct.pack("!H", length)
        else:
            header.append(127)
            header += struct.pack("!Q", length)
        self._wfile.write(bytes(header) + payload)
        self._wfile.flush()

    # -- public API --------------------------------------------------------
    def recv(self) -> tuple[int, bytes]:
        """Return the next complete (opcode, payload) message."""
        fragments = bytearray()
        frag_opcode = None
        while True:
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
                payload = _unmask(payload, mask)

            if opcode == OP_CLOSE:
                raise WebSocketError("client closed")
            if opcode == OP_PING:
                self._send_frame(payload, OP_PONG)
                continue
            if opcode == OP_PONG:
                continue
            if opcode == OP_CONT:
                if frag_opcode is None:
                    raise WebSocketError("unexpected continuation frame")
                fragments += payload
                if fin:
                    return frag_opcode, bytes(fragments)
                continue
            # text or binary
            if fin:
                return opcode, payload
            frag_opcode = opcode
            fragments = bytearray(payload)

    def send_text(self, text: str) -> None:
        self._send_frame(text.encode("utf-8"), OP_TEXT)

    def send_binary(self, payload: bytes) -> None:
        self._send_frame(payload, OP_BINARY)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._send_frame(b"", OP_CLOSE)
        except Exception:  # noqa: BLE001 - peer may already be gone
            pass
        try:
            self._handler.close_connection = True
        except Exception:  # noqa: BLE001
            pass
