#!/usr/bin/env python3
"""Mitmproxy capture addon implementing HTTP Capture Contract v0 for DEV-002."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import time
from typing import Any
import uuid

from mitmproxy import http

from parser import build_derived_views

SCHEMA_VERSION = "v0"


class _StreamState:
    def __init__(self, max_body_bytes: int, overflow_mode: str) -> None:
        self.max_body_bytes = max_body_bytes
        self.overflow_mode = overflow_mode
        self.total_size = 0
        self.hasher = hashlib.sha256()
        self.captured_buf = bytearray()
        self.exceeded_limit = False

    def feed(self, chunk: bytes) -> bytes:
        if not chunk:
            return chunk
        self.total_size += len(chunk)
        self.hasher.update(chunk)
        if self.total_size > self.max_body_bytes:
            self.exceeded_limit = True
        if self.overflow_mode == "truncated" and len(self.captured_buf) < self.max_body_bytes:
            take = min(self.max_body_bytes - len(self.captured_buf), len(chunk))
            self.captured_buf.extend(chunk[:take])
        return chunk


class CaptureRecorderAddon:
    def __init__(self) -> None:
        self.storage_dir = Path(
            os.environ.get("CAPTURE_STORAGE_DIR", "/var/lib/proxyconsole")
        )
        self.captures_dir = self.storage_dir / "captures"
        self.blobs_dir = self.storage_dir / "blobs"
        self.max_body_bytes = int(
            os.environ.get("CAPTURE_MAX_BODY_BYTES", str(1024 * 1024))
        )
        self.overflow_mode = os.environ.get("CAPTURE_OVERFLOW_MODE", "truncated")
        self.upstream_target = os.environ.get(
            "UPSTREAM_TARGET", "http://router:8080"
        )
        self._seq = 0
        self._stream_states: dict[str, _StreamState] = {}

        self.captures_dir.mkdir(parents=True, exist_ok=True)
        self.blobs_dir.mkdir(parents=True, exist_ok=True)

    def _next_capture_id(self) -> str:
        self._seq += 1
        now_str = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        short_uid = uuid.uuid4().hex[:8]
        return f"cap-{now_str}-{self._seq:04d}-{short_uid}"

    def _resolve_route_id(self, authority: str) -> str:
        host_only = authority.split(":", 1)[0].strip().lower()
        if host_only == "app-a.local.test":
            return "route:app-a"
        if host_only == "app-b.local.test":
            return "route:app-b"
        return f"route:unmatched:{host_only or 'empty'}"

    def requestheaders(self, flow: http.HTTPFlow) -> None:
        try:
            flow.metadata["pc_start_monotonic"] = time.monotonic()
            flow.metadata["pc_capture_id"] = self._next_capture_id()

            # Check if this request is a large upload or chunked transfer that should stream
            cl_header = flow.request.headers.get("Content-Length")
            te_header = (flow.request.headers.get("Transfer-Encoding") or "").lower()
            should_stream = False
            if cl_header is not None:
                try:
                    if int(cl_header) > self.max_body_bytes:
                        should_stream = True
                except ValueError:
                    pass
            elif "chunked" in te_header:
                should_stream = True

            if should_stream:
                state = _StreamState(self.max_body_bytes, self.overflow_mode)
                self._stream_states[flow.id] = state
                flow.request.stream = state.feed
        except Exception as exc:
            print(f"[recorder] requestheaders warning: {exc}", flush=True)

    def response(self, flow: http.HTTPFlow) -> None:
        self._finalize_flow(flow, proxy_error=None)

    def error(self, flow: http.HTTPFlow) -> None:
        err_msg = str(flow.error) if flow.error else "unknown_proxy_error"
        self._finalize_flow(flow, proxy_error=err_msg)

    def _finalize_flow(self, flow: http.HTTPFlow, proxy_error: str | None) -> None:
        try:
            capture_id = flow.metadata.get("pc_capture_id") or self._next_capture_id()
            start_mono = flow.metadata.get("pc_start_monotonic", time.monotonic())
            duration_ms = round((time.monotonic() - start_mono) * 1000.0, 3)

            req = flow.request
            resp = flow.response

            ordered_headers = [
                [k.decode("latin-1"), v.decode("latin-1")]
                for k, v in req.headers.fields
            ]
            ordered_trailers = (
                [
                    [k.decode("latin-1"), v.decode("latin-1")]
                    for k, v in req.trailers.fields
                ]
                if req.trailers
                else []
            )

            authority = req.headers.get("Host") or req.host_header or ""
            scheme = req.headers.get("X-Forwarded-Proto") or req.scheme or "http"
            raw_target = req.path or "/"
            if "?" in raw_target:
                raw_path, raw_query = raw_target.split("?", 1)
            else:
                raw_path, raw_query = raw_target, ""

            upgrade_hdr = (req.headers.get("Upgrade") or "").lower()
            is_websocket = upgrade_hdr == "websocket" or (
                resp is not None and resp.status_code == 101
            )

            stream_state = self._stream_states.pop(flow.id, None)
            content_type = req.headers.get("Content-Type")
            content_encoding = req.headers.get("Content-Encoding")

            blob_ref: str | None = None
            raw_body_for_views: bytes | None = None

            if is_websocket:
                body_size = 0
                captured_size = 0
                body_sha256 = hashlib.sha256(b"").hexdigest()
                capture_status = "metadata-only"
                replayable = False
            elif stream_state is not None:
                body_size = stream_state.total_size
                body_sha256 = stream_state.hasher.hexdigest()
                if stream_state.exceeded_limit:
                    if self.overflow_mode == "metadata-only":
                        capture_status = "metadata-only"
                        captured_size = 0
                    else:
                        capture_status = "truncated"
                        captured_bytes = bytes(stream_state.captured_buf)
                        captured_size = len(captured_bytes)
                        if captured_size > 0:
                            blob_ref = f"blobs/{capture_id}.bin"
                            self._write_blob(blob_ref, captured_bytes)
                    replayable = False
                else:
                    raw_bytes = bytes(stream_state.captured_buf)
                    captured_size = len(raw_bytes)
                    capture_status = "complete"
                    replayable = True
                    raw_body_for_views = raw_bytes
                    if captured_size > 0:
                        blob_ref = f"blobs/{capture_id}.bin"
                        self._write_blob(blob_ref, raw_bytes)
            else:
                raw_bytes = req.raw_content if req.raw_content is not None else b""
                body_size = len(raw_bytes)
                body_sha256 = hashlib.sha256(raw_bytes).hexdigest()
                if body_size > self.max_body_bytes:
                    if self.overflow_mode == "metadata-only":
                        capture_status = "metadata-only"
                        captured_size = 0
                    else:
                        capture_status = "truncated"
                        truncated_bytes = raw_bytes[: self.max_body_bytes]
                        captured_size = len(truncated_bytes)
                        blob_ref = f"blobs/{capture_id}.bin"
                        self._write_blob(blob_ref, truncated_bytes)
                    replayable = False
                else:
                    capture_status = "complete"
                    captured_size = body_size
                    replayable = True
                    raw_body_for_views = raw_bytes
                    if captured_size > 0:
                        blob_ref = f"blobs/{capture_id}.bin"
                        self._write_blob(blob_ref, raw_bytes)

            ws_metadata: dict[str, Any] | None = None
            if is_websocket:
                ws_metadata = {
                    "upgrade": req.headers.get("Upgrade", ""),
                    "connection": req.headers.get("Connection", ""),
                    "sec_websocket_version": req.headers.get(
                        "Sec-WebSocket-Version", ""
                    ),
                    "sec_websocket_key": req.headers.get("Sec-WebSocket-Key", ""),
                    "sec_websocket_protocol": req.headers.get(
                        "Sec-WebSocket-Protocol"
                    ),
                    "response_status": resp.status_code if resp is not None else None,
                    "sec_websocket_accept": (
                        resp.headers.get("Sec-WebSocket-Accept")
                        if resp is not None
                        else None
                    ),
                }

            derived_views = build_derived_views(
                raw_query=raw_query,
                headers=ordered_headers,
                raw_body=raw_body_for_views,
                content_type=content_type,
                capture_status=capture_status,
            )

            actual_target = (
                resp.headers.get("X-Echo-Target") if resp is not None else None
            )

            record: dict[str, Any] = {
                "schema_version": SCHEMA_VERSION,
                "capture_id": capture_id,
                "captured_at": datetime.now(timezone.utc).isoformat(),
                "route_id": self._resolve_route_id(authority),
                "capture_status": capture_status,
                "replayable": replayable,
                "is_websocket": is_websocket,
                "websocket_handshake": ws_metadata,
                "request": {
                    "method": req.method,
                    "scheme": scheme,
                    "authority": authority,
                    "raw_target": raw_target,
                    "raw_path": raw_path,
                    "raw_query": raw_query,
                    "http_version": req.http_version,
                    "headers": ordered_headers,
                    "trailers": ordered_trailers,
                    "body": {
                        "size": body_size,
                        "captured_size": captured_size,
                        "sha256": body_sha256,
                        "blob_ref": blob_ref,
                        "content_type": content_type,
                        "content_encoding": content_encoding,
                        "max_capture_bytes": self.max_body_bytes,
                    },
                },
                "forwarding": {
                    "upstream": self.upstream_target,
                    "target": actual_target,
                    "response_status": resp.status_code if resp is not None else None,
                    "duration_ms": duration_ms,
                    "proxy_error": proxy_error,
                },
                "derived_views": derived_views,
            }

            self._write_record(capture_id, record)

            if resp is not None:
                resp.headers["X-Capture-Id"] = capture_id
                resp.headers["X-Capture-Status"] = capture_status
        except Exception as exc:
            print(f"[recorder] finalize error (non-fatal to proxy): {exc}", flush=True)

    def _write_blob(self, blob_ref: str, data: bytes) -> None:
        target_path = self.storage_dir / blob_ref
        tmp_path = target_path.with_suffix(".bin.tmp")
        tmp_path.write_bytes(data)
        os.replace(tmp_path, target_path)

    def _write_record(self, capture_id: str, record: dict[str, Any]) -> None:
        target_path = self.captures_dir / f"{capture_id}.json"
        tmp_path = target_path.with_suffix(".json.tmp")
        tmp_path.write_text(
            json.dumps(record, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(tmp_path, target_path)


addons = [CaptureRecorderAddon()]
