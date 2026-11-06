#!/usr/bin/env python3
"""Local HTTP API server for capture index search, detail, import, rebuild, and retention (DEV-010 / DEV-011)."""

from __future__ import annotations

import json
import os
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from index_store import CaptureIndexStore

API_HOST = os.environ.get("CONTROL_API_LISTEN_HOST", "0.0.0.0")
API_PORT = int(os.environ.get("CONTROL_API_PORT", "18480"))

PERIODIC_WAKE_EVENT = threading.Event()
LAST_PERIODIC_RESET_LOCK = threading.Lock()
LAST_PERIODIC_TS = time.monotonic()


def _reset_periodic_timer() -> None:
    global LAST_PERIODIC_TS
    with LAST_PERIODIC_RESET_LOCK:
        LAST_PERIODIC_TS = time.monotonic()
    PERIODIC_WAKE_EVENT.set()


def _first_param(qs: dict[str, list[str]], key: str) -> str | None:
    vals = qs.get(key)
    if not vals:
        return None
    val = vals[0].strip()
    return val if val != "" else None


def _parse_bool(val: Any) -> bool:
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float)):
        return bool(val)
    s = str(val).strip().lower()
    if s in ("1", "true", "yes", "on"):
        return True
    if s in ("0", "false", "no", "off"):
        return False
    raise ValueError(f"Invalid boolean value: {val}")


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

    def _read_json_body(self) -> dict[str, Any]:
        cl = int(self.headers.get("Content-Length", "0") or "0")
        if cl <= 0:
            return {}
        raw = self.rfile.read(cl).decode("utf-8").strip()
        if not raw:
            return {}
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            raise ValueError("Request JSON body must be an object")
        return parsed

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

            if path == "/api/v1/retention/status":
                runs_limit_str = _first_param(qs, "runs_limit")
                runs_limit = (
                    int(runs_limit_str) if runs_limit_str is not None else 20
                )
                self._send_json(
                    200, self.store.get_retention_status(runs_limit=runs_limit)
                )
                return

            if path == "/api/v1/retention/policy":
                self._send_json(200, self.store.get_retention_policy())
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

    def _handle_policy_update(self) -> None:
        body = self._read_json_body()
        min_ret = (
            int(body["min_retention_seconds"])
            if "min_retention_seconds" in body
            and body["min_retention_seconds"] is not None
            else None
        )
        soft_lim = (
            int(body["soft_limit_bytes"])
            if "soft_limit_bytes" in body
            and body["soft_limit_bytes"] is not None
            else None
        )
        hard_lim = (
            int(body["hard_limit_bytes"])
            if "hard_limit_bytes" in body
            and body["hard_limit_bytes"] is not None
            else None
        )
        interval = (
            float(body["periodic_interval_seconds"])
            if "periodic_interval_seconds" in body
            and body["periodic_interval_seconds"] is not None
            else None
        )
        enabled = (
            _parse_bool(body["periodic_enabled"])
            if "periodic_enabled" in body
            and body["periodic_enabled"] is not None
            else None
        )
        updated = self.store.update_retention_policy(
            min_retention_seconds=min_ret,
            soft_limit_bytes=soft_lim,
            hard_limit_bytes=hard_lim,
            periodic_interval_seconds=interval,
            periodic_enabled=enabled,
        )
        _reset_periodic_timer()
        self._send_json(200, updated)

    def do_PUT(self) -> None:
        split = urlsplit(self.path)
        path = split.path.rstrip("/") or "/"
        try:
            if path == "/api/v1/retention/policy":
                self._handle_policy_update()
                return
            self._send_json(404, {"error": "not_found", "path": path})
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
        qs = parse_qs(split.query, keep_blank_values=False)

        try:
            if path == "/api/v1/retention/policy":
                self._handle_policy_update()
                return

            body = self._read_json_body()

            if path == "/api/v1/retention/run":
                dry_run_q = _first_param(qs, "dry_run")
                if dry_run_q is not None:
                    dry_run = _parse_bool(dry_run_q)
                elif "dry_run" in body and body["dry_run"] is not None:
                    dry_run = _parse_bool(body["dry_run"])
                else:
                    dry_run = False

                now_param = _first_param(qs, "now") or body.get("now")
                trigger_source = (
                    _first_param(qs, "trigger_source")
                    or body.get("trigger_source")
                    or ("dry_run" if dry_run else "manual")
                )
                res = self.store.run_retention(
                    dry_run=dry_run,
                    now=str(now_param) if now_param else None,
                    trigger_source=str(trigger_source),
                )
                self._send_json(200, res)
                return

            if path == "/api/v1/index/import":
                res = self.store.import_all_captures()
                self._send_json(200, res)
                return

            if path == "/api/v1/index/rebuild":
                res = self.store.rebuild_index()
                self._send_json(200, res)
                return

            self._send_json(404, {"error": "not_found", "path": path})
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

    def log_message(self, fmt: str, *args: object) -> None:
        print(f"[control-api] {self.address_string()} - {fmt % args}", flush=True)


def _periodic_retention_worker(store: CaptureIndexStore) -> None:
    global LAST_PERIODIC_TS
    while True:
        try:
            PERIODIC_WAKE_EVENT.clear()
            policy = store.get_retention_policy()
            if not policy.get("periodic_enabled", True):
                PERIODIC_WAKE_EVENT.wait(timeout=1.0)
                with LAST_PERIODIC_RESET_LOCK:
                    LAST_PERIODIC_TS = time.monotonic()
                continue

            interval = max(
                0.2, float(policy.get("periodic_interval_seconds", 60.0))
            )
            with LAST_PERIODIC_RESET_LOCK:
                elapsed = time.monotonic() - LAST_PERIODIC_TS

            remaining = interval - elapsed
            if remaining > 0:
                PERIODIC_WAKE_EVENT.wait(timeout=min(remaining, 0.5))
                continue

            with LAST_PERIODIC_RESET_LOCK:
                LAST_PERIODIC_TS = time.monotonic()

            store.run_retention(dry_run=False, trigger_source="periodic")
        except Exception as exc:
            print(
                f"[control-api] periodic retention warning: {exc}",
                flush=True,
            )
            time.sleep(1.0)


def main() -> None:
    signal.signal(signal.SIGTERM, lambda _signum, _frame: sys.exit(0))
    signal.signal(signal.SIGINT, lambda _signum, _frame: sys.exit(0))

    store = CaptureIndexStore()
    try:
        store.import_all_captures()
    except Exception as exc:
        print(f"[control-api] startup import warning: {exc}", flush=True)

    worker = threading.Thread(
        target=_periodic_retention_worker,
        args=(store,),
        name="periodic-retention",
        daemon=True,
    )
    worker.start()

    server = ThreadingHTTPServer((API_HOST, API_PORT), CaptureApiHandler)
    print(f"[control-api] listening on {API_HOST}:{API_PORT}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
