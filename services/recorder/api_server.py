#!/usr/bin/env python3
"""Local HTTP API server for capture index search, detail, import, and rebuild (DEV-010)."""

from __future__ import annotations

import json
import os
import signal
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from index_store import CaptureIndexStore

API_HOST = os.environ.get("CONTROL_API_LISTEN_HOST", "0.0.0.0")
API_PORT = int(os.environ.get("CONTROL_API_PORT", "18480"))


def _first_param(qs: dict[str, list[str]], key: str) -> str | None:
    vals = qs.get(key)
    if not vals:
        return None
    val = vals[0].strip()
    return val if val != "" else None


class CaptureApiHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    store = CaptureIndexStore()

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode(
            "utf-8"
        )
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        split = urlsplit(self.path)
        path = split.path.rstrip("/") or "/"
        qs = parse_qs(split.query, keep_blank_values=False)

        try:
            if path == "/api/v1/health":
                self._send_json(200, {"status": "ok"})
                return

            if path == "/api/v1/index/status":
                self._send_json(200, self.store.get_index_status())
                return

            if path == "/api/v1/captures":
                limit_str = _first_param(qs, "limit")
                offset_str = _first_param(qs, "offset")
                resp_status_str = _first_param(qs, "response_status")

                limit = int(limit_str) if limit_str is not None else 50
                offset = int(offset_str) if offset_str is not None else 0
                resp_status = (
                    int(resp_status_str) if resp_status_str is not None else None
                )
                sort = _first_param(qs, "sort") or "desc"
                from_time = _first_param(qs, "from_time") or _first_param(
                    qs, "since"
                )
                to_time = _first_param(qs, "to_time") or _first_param(
                    qs, "until"
                )
                host = _first_param(qs, "host")
                authority = _first_param(qs, "authority")
                method = _first_param(qs, "method")
                req_path = _first_param(qs, "path")
                path_prefix = _first_param(qs, "path_prefix")
                capture_status = _first_param(qs, "capture_status") or _first_param(
                    qs, "status"
                )

                result = self.store.query_captures(
                    limit=limit,
                    offset=offset,
                    sort=sort,
                    from_time=from_time,
                    to_time=to_time,
                    host=host,
                    authority=authority,
                    method=method,
                    path=req_path,
                    path_prefix=path_prefix,
                    capture_status=capture_status,
                    response_status=resp_status,
                )
                self._send_json(200, result)
                return

            if path.startswith("/api/v1/captures/"):
                capture_id = unquote(path[len("/api/v1/captures/") :])
                try:
                    detail = self.store.get_capture_detail(capture_id)
                    self._send_json(200, detail)
                except FileNotFoundError as exc:
                    self._send_json(
                        404,
                        {
                            "error": "capture_not_found",
                            "capture_id": capture_id,
                            "message": str(exc),
                        },
                    )
                return

            self._send_json(
                404,
                {"error": "not_found", "path": path},
            )
        except ValueError as exc:
            self._send_json(
                400,
                {"error": "invalid_parameter", "message": str(exc)},
            )
        except Exception as exc:
            self._send_json(
                500,
                {"error": "internal_error", "message": str(exc)},
            )

    def do_POST(self) -> None:
        split = urlsplit(self.path)
        path = split.path.rstrip("/") or "/"
        cl = int(self.headers.get("Content-Length", "0") or "0")
        if cl > 0:
            _ = self.rfile.read(cl)

        try:
            if path == "/api/v1/index/import":
                res = self.store.import_all_captures()
                self._send_json(200, res)
                return

            if path == "/api/v1/index/rebuild":
                res = self.store.rebuild_index()
                self._send_json(200, res)
                return

            self._send_json(404, {"error": "not_found", "path": path})
        except Exception as exc:
            self._send_json(
                500,
                {"error": "internal_error", "message": str(exc)},
            )

    def log_message(self, fmt: str, *args: object) -> None:
        print(f"[control-api] {self.address_string()} - {fmt % args}", flush=True)


def main() -> None:
    signal.signal(signal.SIGTERM, lambda _signum, _frame: sys.exit(0))
    signal.signal(signal.SIGINT, lambda _signum, _frame: sys.exit(0))

    store = CaptureIndexStore()
    try:
        store.import_all_captures()
    except Exception as exc:
        print(f"[control-api] startup import warning: {exc}", flush=True)

    server = ThreadingHTTPServer((API_HOST, API_PORT), CaptureApiHandler)
    print(f"[control-api] listening on {API_HOST}:{API_PORT}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
