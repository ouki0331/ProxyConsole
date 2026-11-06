#!/usr/bin/env python3
"""Minimal identifiable HTTP echo target for DEV-001 proxy chain verification."""

import json
import os
import signal
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qsl, urlsplit

TARGET_ID = os.environ.get("TARGET_ID", "echo-unknown")
LISTEN_HOST = os.environ.get("LISTEN_HOST", "0.0.0.0")
LISTEN_PORT = int(os.environ.get("LISTEN_PORT", "8080"))


class EchoRequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _handle_any(self) -> None:
        split_result = urlsplit(self.path)
        raw_path = split_result.path or "/"
        raw_query = split_result.query
        query_params = [
            [k, v] for k, v in parse_qsl(raw_query, keep_blank_values=True)
        ]

        content_length_raw = self.headers.get("Content-Length", "0")
        try:
            content_length = max(0, int(content_length_raw))
        except ValueError:
            content_length = 0

        raw_body = self.rfile.read(content_length) if content_length > 0 else b""
        body_text = raw_body.decode("utf-8", errors="replace")

        body_json = None
        if raw_body:
            try:
                body_json = json.loads(body_text)
            except ValueError:
                body_json = None

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
            "body_size": len(raw_body),
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
