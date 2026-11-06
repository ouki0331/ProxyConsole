#!/usr/bin/env python3
"""Identifiable HTTP & WebSocket echo target for DEV-001 / DEV-002 verification."""

import base64
import hashlib
import json
import os
import signal
import struct
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qsl, urlsplit

TARGET_ID = os.environ.get("TARGET_ID", "echo-unknown")
LISTEN_HOST = os.environ.get("LISTEN_HOST", "0.0.0.0")
LISTEN_PORT = int(os.environ.get("LISTEN_PORT", "8080"))
MAX_INLINE_BODY = int(os.environ.get("MAX_INLINE_BODY", "65536"))
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def _read_ws_frame(rfile) -> tuple[int, bytes] | None:
    header = rfile.read(2)
    if len(header) < 2:
        return None
    b1, b2 = header[0], header[1]
    opcode = b1 & 0x0F
    masked = (b2 & 0x80) != 0
    length = b2 & 0x7F
    if length == 126:
        ext = rfile.read(2)
        if len(ext) < 2:
            return None
        length = struct.unpack("!H", ext)[0]
    elif length == 127:
        ext = rfile.read(8)
        if len(ext) < 8:
            return None
        length = struct.unpack("!Q", ext)[0]
    mask_key = rfile.read(4) if masked else b""
    payload = rfile.read(length) if length > 0 else b""
    if masked and mask_key:
        payload = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))
    return opcode, payload


def _build_ws_frame(opcode: int, payload: bytes) -> bytes:
    first_byte = 0x80 | (opcode & 0x0F)
    length = len(payload)
    if length < 126:
        header = bytes([first_byte, length])
    elif length < 65536:
        header = bytes([first_byte, 126]) + struct.unpack_from("!2s", struct.pack("!H", length))[0]
    else:
        header = bytes([first_byte, 127]) + struct.pack("!Q", length)
    return header + payload


class EchoRequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _handle_websocket(self, raw_path: str, raw_query: str) -> None:
        ws_key = self.headers.get("Sec-WebSocket-Key", "").strip()
        accept_raw = hashlib.sha1((ws_key + WS_GUID).encode("ascii")).digest()
        accept_val = base64.b64encode(accept_raw).decode("ascii")

        self.send_response(101, "Switching Protocols")
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept_val)
        self.send_header("X-Echo-Target", TARGET_ID)
        ws_proto = self.headers.get("Sec-WebSocket-Protocol")
        if ws_proto:
            self.send_header("Sec-WebSocket-Protocol", ws_proto.split(",")[0].strip())
        self.end_headers()
        self.wfile.flush()

        # Echo frames until client closes or sends close frame
        while True:
            frame = _read_ws_frame(self.rfile)
            if frame is None:
                break
            opcode, payload = frame
            if opcode == 0x8:  # Close frame
                self.wfile.write(_build_ws_frame(0x8, b""))
                self.wfile.flush()
                break
            if opcode in (0x1, 0x2):  # Text or Binary frame
                msg_text = payload.decode("utf-8", errors="replace")
                reply = json.dumps(
                    {
                        "target": TARGET_ID,
                        "ws_echo": msg_text,
                        "path": raw_path,
                        "raw_query": raw_query,
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
                self.wfile.write(_build_ws_frame(0x1, reply))
                self.wfile.flush()

    def _read_body_stream(self) -> tuple[int, str, bytes]:
        hasher = hashlib.sha256()
        total_size = 0
        inline_buf = bytearray()

        transfer_encoding = (self.headers.get("Transfer-Encoding") or "").lower()
        if "chunked" in transfer_encoding:
            while True:
                line = self.rfile.readline()
                if not line:
                    break
                chunk_len_str = line.split(b";", 1)[0].strip()
                if not chunk_len_str:
                    continue
                chunk_len = int(chunk_len_str, 16)
                if chunk_len == 0:
                    self.rfile.readline()
                    break
                remaining = chunk_len
                while remaining > 0:
                    piece = self.rfile.read(min(65536, remaining))
                    if not piece:
                        break
                    total_size += len(piece)
                    hasher.update(piece)
                    if len(inline_buf) < MAX_INLINE_BODY:
                        take = min(MAX_INLINE_BODY - len(inline_buf), len(piece))
                        inline_buf.extend(piece[:take])
                    remaining -= len(piece)
                self.rfile.readline()
        else:
            content_length_raw = self.headers.get("Content-Length", "0")
            try:
                content_length = max(0, int(content_length_raw))
            except ValueError:
                content_length = 0
            remaining = content_length
            while remaining > 0:
                piece = self.rfile.read(min(65536, remaining))
                if not piece:
                    break
                total_size += len(piece)
                hasher.update(piece)
                if len(inline_buf) < MAX_INLINE_BODY:
                    take = min(MAX_INLINE_BODY - len(inline_buf), len(piece))
                    inline_buf.extend(piece[:take])
                remaining -= len(piece)

        return total_size, hasher.hexdigest(), bytes(inline_buf)

    def _handle_any(self) -> None:
        split_result = urlsplit(self.path)
        raw_path = split_result.path or "/"
        raw_query = split_result.query

        if (self.headers.get("Upgrade") or "").lower() == "websocket":
            self._handle_websocket(raw_path, raw_query)
            return

        query_params = [
            [k, v] for k, v in parse_qsl(raw_query, keep_blank_values=True)
        ]

        body_size, body_sha256, inline_bytes = self._read_body_stream()
        is_inline_complete = body_size <= MAX_INLINE_BODY

        body_text = (
            inline_bytes.decode("utf-8", errors="replace")
            if is_inline_complete
            else ""
        )

        body_json = None
        form_params = None
        content_type = (self.headers.get("Content-Type") or "").lower()

        if is_inline_complete and body_size > 0:
            try:
                body_json = json.loads(body_text)
            except ValueError:
                body_json = None

            if "application/x-www-form-urlencoded" in content_type:
                form_params = [
                    [k, v]
                    for k, v in parse_qsl(body_text, keep_blank_values=True)
                ]

        ordered_headers = [[k, v] for k, v in self.headers.items()]

        payload = {
            "target": TARGET_ID,
            "method": self.command,
            "path": raw_path,
            "raw_query": raw_query,
            "query_params": query_params,
            "http_version": self.request_version,
            "host": self.headers.get("Host", ""),
            "headers": ordered_headers,
            "body_text": body_text,
            "body_json": body_json,
            "form_params": form_params,
            "body_size": body_size,
            "body_sha256": body_sha256,
            "body_inline_truncated": not is_inline_complete,
        }

        response_bytes = (
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
        ).encode("utf-8")

        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(response_bytes)))
        self.send_header("X-Echo-Target", TARGET_ID)
        self.end_headers()
        self.wfile.write(response_bytes)

    def do_GET(self) -> None:
        self._handle_any()

    def do_POST(self) -> None:
        self._handle_any()

    def do_PUT(self) -> None:
        self._handle_any()

    def do_PATCH(self) -> None:
        self._handle_any()

    def do_DELETE(self) -> None:
        self._handle_any()

    def log_message(self, fmt: str, *args: object) -> None:
        print(f"[{TARGET_ID}] {self.address_string()} - {fmt % args}", flush=True)


def main() -> None:
    signal.signal(signal.SIGTERM, lambda _signum, _frame: sys.exit(0))
    signal.signal(signal.SIGINT, lambda _signum, _frame: sys.exit(0))
    server = ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), EchoRequestHandler)
    print(f"[{TARGET_ID}] listening on {LISTEN_HOST}:{LISTEN_PORT}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
