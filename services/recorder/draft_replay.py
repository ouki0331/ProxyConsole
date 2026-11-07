#!/usr/bin/env python3
"""Request draft, structured editor model, normalization, semantic replay, and cURL/fetch export core (DEV-030)."""

from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
import hashlib
import http.client
import json
import os
from pathlib import Path
import shlex
import socket
import ssl
import time
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qsl, quote, unquote, unquote_plus, urlsplit
import uuid

from parser import build_derived_views, parse_cookies_ordered

if TYPE_CHECKING:
    from index_store import CaptureIndexStore

SAFE_HTTP_METHODS = {"GET", "HEAD", "OPTIONS", "TRACE"}
VALID_HTTP_METHODS = {
    "GET",
    "POST",
    "PUT",
    "PATCH",
    "DELETE",
    "HEAD",
    "OPTIONS",
    "TRACE",
}

HOP_BY_HOP_AND_REBUILT_HEADERS = {
    "host",
    "content-length",
    "connection",
    "transfer-encoding",
    "proxy-connection",
    "keep-alive",
    "upgrade",
    "te",
    "trailer",
    "x-forwarded-for",
    "x-forwarded-proto",
    "x-forwarded-host",
}


class DraftNotFoundError(KeyError):
    """Raised when a draft_id does not exist."""


class DraftExpiredError(KeyError):
    """Raised when a draft_id has passed its expires_at timestamp."""

    def __init__(self, draft_id: str, expires_at: str) -> None:
        super().__init__(f"Draft {draft_id!r} expired at {expires_at}")
        self.draft_id = draft_id
        self.expires_at = expires_at


class DraftValidationError(ValueError):
    """Raised when draft fields or structured edits fail validation."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "invalid_draft_edit",
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.details = details or {}


class NonReplayableCaptureError(ValueError):
    """Raised when attempting to create a replay draft or replay a non-replayable capture."""

    def __init__(
        self,
        *,
        capture_id: str,
        reason: str,
        capture_status: str | None,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.capture_id = capture_id
        self.reason = reason
        self.capture_status = capture_status
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        return {
            "error": "non_replayable_capture",
            "capture_id": self.capture_id,
            "reason": self.reason,
            "capture_status": self.capture_status,
            "replayable": False,
            "message": str(self),
            **self.details,
        }


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso_utc(iso_str: str) -> datetime:
    s = iso_str.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def encode_uri_component(val: str) -> str:
    """Percent-encode a query or form component preserving RFC 3986 unreserved characters."""
    return quote(str(val), safe="-._~")


def decode_path_human_readable(raw_path: str) -> str:
    """Decode a percent-encoded wire URI path into a human-readable path."""
    s = str(raw_path or "/")
    if not s.startswith("/"):
        s = "/" + s
    return unquote(s)


def encode_decoded_path(decoded_path: str) -> str:
    """Encode a human-readable decoded path into a wire-safe URI path preserving '/' separators."""
    s = str(decoded_path or "/")
    if not s.startswith("/"):
        s = "/" + s
    segments = s.split("/")
    encoded_segments = [quote(seg, safe="-._~:@!$&'()*+,;=") for seg in segments]
    return "/".join(encoded_segments) or "/"


def parse_raw_query_entries(raw_query: str) -> list[dict[str, Any]]:
    """Parse raw query string into an ordered list of entries with both decoded and wire fields."""
    if not raw_query:
        return []
    entries: list[dict[str, Any]] = []
    for part in raw_query.split("&"):
        if part == "":
            continue
        if "=" in part:
            rk, rv = part.split("=", 1)
            has_eq = True
        else:
            rk, rv = part, ""
            has_eq = False
        dk = unquote_plus(rk)
        dv = unquote_plus(rv)
        entries.append(
            {
                "name": dk,
                "value": dv,
                "raw_name": rk,
                "raw_value": rv,
                "has_equals": has_eq,
            }
        )
    return entries


def build_raw_query_from_entries(entries: list[dict[str, Any]]) -> str:
    """Build wire raw_query from ordered query entries, preserving exact raw tokens when unedited."""
    parts: list[str] = []
    for entry in entries:
        name = str(entry.get("name", ""))
        value = str(entry.get("value", ""))
        rk = entry.get("raw_name")
        rv = entry.get("raw_value")
        has_eq = entry.get("has_equals", True)
        if (
            isinstance(rk, str)
            and isinstance(rv, str)
            and unquote_plus(rk) == name
            and unquote_plus(rv) == value
        ):
            if has_eq or rv != "":
                parts.append(f"{rk}={rv}")
            else:
                parts.append(rk)
        else:
            enc_k = encode_uri_component(name)
            enc_v = encode_uri_component(value)
            parts.append(f"{enc_k}={enc_v}")
    return "&".join(parts)


def entries_from_pairs(pairs: list[list[str] | tuple[str, str]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for item in pairs:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            raise DraftValidationError(
                f"Expected [name, value] pair, got: {item!r}",
                code="invalid_pair",
            )
        k = str(item[0])
        v = str(item[1])
        out.append(
            {
                "name": k,
                "value": v,
                "raw_name": encode_uri_component(k),
                "raw_value": encode_uri_component(v),
                "has_equals": True,
            }
        )
    return out


def apply_pair_mutations(
    pairs: list[list[str]],
    mutations: list[dict[str, Any]],
    *,
    case_insensitive_name: bool = False,
    field_label: str = "pair",
) -> list[list[str]]:
    """Apply ordered list-of-pair mutations (add/insert/set_at/remove_at/set/remove) preserving order and duplicates."""
    result = [[str(k), str(v)] for k, v in pairs]

    def _match(k1: str, k2: str) -> bool:
        return k1.lower() == k2.lower() if case_insensitive_name else k1 == k2

    for mut in mutations:
        if not isinstance(mut, dict):
            raise DraftValidationError(
                f"Invalid {field_label} mutation object: {mut!r}",
                code=f"invalid_{field_label}_mutation",
            )
        op = str(mut.get("op") or mut.get("action") or "").strip().lower()
        name = mut.get("name") if "name" in mut else mut.get("key")
        value = mut.get("value")
        idx = mut.get("index")
        occ = mut.get("occurrence")

        if op in ("add", "append"):
            if name is None:
                raise DraftValidationError(
                    f"'{op}' mutation on {field_label} requires 'name' (or 'key')",
                    code=f"invalid_{field_label}_mutation",
                )
            result.append([str(name), "" if value is None else str(value)])

        elif op == "insert":
            if name is None or idx is None:
                raise DraftValidationError(
                    f"'insert' mutation on {field_label} requires 'index' and 'name'",
                    code=f"invalid_{field_label}_mutation",
                )
            pos = int(idx)
            if pos < 0 or pos > len(result):
                raise DraftValidationError(
                    f"Index {pos} out of range for {field_label} list of length {len(result)}",
                    code=f"invalid_{field_label}_index",
                )
            result.insert(pos, [str(name), "" if value is None else str(value)])

        elif op == "set_at":
            if idx is None:
                raise DraftValidationError(
                    f"'set_at' mutation on {field_label} requires 'index'",
                    code=f"invalid_{field_label}_mutation",
                )
            pos = int(idx)
            if pos < 0 or pos >= len(result):
                raise DraftValidationError(
                    f"Index {pos} out of range for {field_label} list of length {len(result)}",
                    code=f"invalid_{field_label}_index",
                )
            new_name = str(name) if name is not None else result[pos][0]
            new_val = (
                str(value) if value is not None else result[pos][1]
            )
            result[pos] = [new_name, new_val]

        elif op == "remove_at":
            if idx is None:
                raise DraftValidationError(
                    f"'remove_at' mutation on {field_label} requires 'index'",
                    code=f"invalid_{field_label}_mutation",
                )
            pos = int(idx)
            if pos < 0 or pos >= len(result):
                raise DraftValidationError(
                    f"Index {pos} out of range for {field_label} list of length {len(result)}",
                    code=f"invalid_{field_label}_index",
                )
            result.pop(pos)

        elif op in ("set", "update"):
            if idx is not None and name is None:
                pos = int(idx)
                if pos < 0 or pos >= len(result):
                    raise DraftValidationError(
                        f"Index {pos} out of range for {field_label} list of length {len(result)}",
                        code=f"invalid_{field_label}_index",
                    )
                result[pos] = [
                    result[pos][0],
                    "" if value is None else str(value),
                ]
                continue
            if name is None:
                raise DraftValidationError(
                    f"'{op}' mutation on {field_label} requires 'name' (or 'key')",
                    code=f"invalid_{field_label}_mutation",
                )
            target_name = str(name)
            new_name = str(mut.get("new_name", target_name))
            new_val = "" if value is None else str(value)
            if idx is not None:
                pos = int(idx)
                if pos < 0 or pos >= len(result):
                    raise DraftValidationError(
                        f"Index {pos} out of range for {field_label} list of length {len(result)}",
                        code=f"invalid_{field_label}_index",
                    )
                result[pos] = [new_name, new_val]
            else:
                target_occ = int(occ) if occ is not None else 0
                replace_all = bool(mut.get("all", False))
                seen = 0
                updated = False
                for i, (k, _v) in enumerate(result):
                    if _match(k, target_name):
                        if replace_all or seen == target_occ:
                            result[i] = [new_name, new_val]
                            updated = True
                            if not replace_all:
                                break
                        seen += 1
                if not updated:
                    if occ is not None and int(occ) > 0:
                        raise DraftValidationError(
                            f"{field_label} key {target_name!r} occurrence {target_occ} not found",
                            code=f"{field_label}_occurrence_not_found",
                        )
                    result.append([new_name, new_val])

        elif op in ("remove", "delete"):
            if idx is not None:
                pos = int(idx)
                if pos < 0 or pos >= len(result):
                    raise DraftValidationError(
                        f"Index {pos} out of range for {field_label} list of length {len(result)}",
                        code=f"invalid_{field_label}_index",
                    )
                result.pop(pos)
                continue
            if name is None:
                raise DraftValidationError(
                    f"'{op}' mutation on {field_label} requires 'name' or 'index'",
                    code=f"invalid_{field_label}_mutation",
                )
            target_name = str(name)
            remove_all = bool(mut.get("all", occ is None))
            target_occ = int(occ) if occ is not None else 0
            seen = 0
            new_result: list[list[str]] = []
            removed = False
            for k, v in result:
                if _match(k, target_name):
                    if remove_all or seen == target_occ:
                        removed = True
                        seen += 1
                        continue
                    seen += 1
                new_result.append([k, v])
            if not removed and occ is not None:
                raise DraftValidationError(
                    f"{field_label} key {target_name!r} occurrence {target_occ} not found",
                    code=f"{field_label}_occurrence_not_found",
                )
            result = new_result
        else:
            raise DraftValidationError(
                f"Unsupported {field_label} mutation op: {op!r}",
                code=f"invalid_{field_label}_mutation",
            )

    return result


def sync_cookies_into_headers(
    headers: list[list[str]], cookies: list[list[str]]
) -> list[list[str]]:
    """Rebuild the Cookie header inside ordered headers from the ordered cookies list."""
    cookie_value = "; ".join(
        f"{k}={v}" if v != "" else str(k) for k, v in cookies
    )
    out: list[list[str]] = []
    replaced = False
    for k, v in headers:
        if k.lower() == "cookie":
            if cookie_value and not replaced:
                out.append([k, cookie_value])
                replaced = True
            continue
        out.append([k, v])
    if cookie_value and not replaced:
        out.append(["Cookie", cookie_value])
    return out


def _get_header_ci(headers: list[list[str]], name: str) -> str | None:
    target = name.lower()
    for k, v in headers:
        if k.lower() == target:
            return v
    return None


def _ensure_header_ci(
    headers: list[list[str]], name: str, default_value: str
) -> list[list[str]]:
    if _get_header_ci(headers, name) is not None:
        return headers
    return [*headers, [name, default_value]]


def apply_json_path_mutations(
    root_val: Any, mutations: list[dict[str, Any]]
) -> Any:
    """Apply nested dot/index path mutations (set/delete) to a JSON structure."""
    data = json.loads(json.dumps(root_val))
    for mut in mutations:
        if not isinstance(mut, dict):
            raise DraftValidationError(
                f"Invalid JSON mutation: {mut!r}", code="invalid_json_mutation"
            )
        op = str(mut.get("op") or "set").strip().lower()
        path_str = str(mut.get("path") or "").strip()
        if not path_str:
            if op == "set":
                data = mut.get("value")
                continue
            raise DraftValidationError(
                "JSON mutation requires non-empty 'path'",
                code="invalid_json_mutation",
            )
        tokens = [t for t in path_str.split(".") if t != ""]
        cur = data
        for seg in tokens[:-1]:
            if isinstance(cur, list):
                try:
                    idx = int(seg)
                except ValueError as exc:
                    raise DraftValidationError(
                        f"Expected list index at segment {seg!r} in path {path_str!r}",
                        code="invalid_json_path",
                    ) from exc
                if idx < 0 or idx >= len(cur):
                    raise DraftValidationError(
                        f"List index {idx} out of range in path {path_str!r}",
                        code="invalid_json_path",
                    )
                cur = cur[idx]
            elif isinstance(cur, dict):
                if seg not in cur or not isinstance(cur[seg], (dict, list)):
                    cur[seg] = {}
                cur = cur[seg]
            else:
                raise DraftValidationError(
                    f"Cannot traverse into primitive at segment {seg!r} in path {path_str!r}",
                    code="invalid_json_path",
                )

        last = tokens[-1]
        if op in ("set", "update"):
            val = mut.get("value")
            if isinstance(cur, list):
                try:
                    idx = int(last)
                except ValueError as exc:
                    raise DraftValidationError(
                        f"Expected list index at {last!r} in path {path_str!r}",
                        code="invalid_json_path",
                    ) from exc
                if idx == len(cur):
                    cur.append(val)
                elif 0 <= idx < len(cur):
                    cur[idx] = val
                else:
                    raise DraftValidationError(
                        f"List index {idx} out of range in path {path_str!r}",
                        code="invalid_json_path",
                    )
            elif isinstance(cur, dict):
                cur[last] = val
            else:
                raise DraftValidationError(
                    f"Cannot set property {last!r} on non-container in path {path_str!r}",
                    code="invalid_json_path",
                )
        elif op in ("delete", "remove"):
            if isinstance(cur, list):
                idx = int(last)
                if 0 <= idx < len(cur):
                    cur.pop(idx)
            elif isinstance(cur, dict):
                cur.pop(last, None)
        else:
            raise DraftValidationError(
                f"Unsupported JSON mutation op: {op!r}",
                code="invalid_json_mutation",
            )
    return data


def build_side_effect_warning(method: str) -> dict[str, Any]:
    m = method.strip().upper()
    if m in SAFE_HTTP_METHODS:
        return {
            "has_side_effect_warning": False,
            "method": m,
            "is_safe_method": True,
            "warning_level": "none",
            "requires_confirmation_in_ui": False,
            "code": None,
            "message": None,
        }
    return {
        "has_side_effect_warning": True,
        "method": m,
        "is_safe_method": False,
        "warning_level": "warning",
        "requires_confirmation_in_ui": True,
        "code": "non_safe_method_side_effects",
        "message": (
            f"HTTP {m} is a non-safe method and replaying it may mutate state "
            "or trigger side effects on the target service."
        ),
    }


class DraftReplayManager:
    """Manages persistent request drafts, structured edits, normalization, cURL/fetch export, and replay."""

    def __init__(self, store: CaptureIndexStore) -> None:
        self.store = store

    def evaluate_replayability(
        self, capture_id: str, *, raise_on_false: bool = False
    ) -> dict[str, Any]:
        record_path = self.store.captures_dir / f"{capture_id}.json"
        if not record_path.is_file():
            with self.store._connect() as conn:
                idx_row = conn.execute(
                    "SELECT record_file FROM captures_index WHERE capture_id = ?",
                    (capture_id,),
                ).fetchone()
            if idx_row is not None:
                record_path = self.store.storage_dir / idx_row["record_file"]
        if not record_path.is_file():
            raise FileNotFoundError(f"Capture record not found: {capture_id}")

        record = json.loads(record_path.read_text(encoding="utf-8"))
        capture_status = str(record.get("capture_status") or "unknown")
        replayable_flag = bool(record.get("replayable"))
        is_ws = bool(record.get("is_websocket"))
        req = record.get("request") or {}
        body_meta = req.get("body") or {}
        expected_size = int(body_meta.get("size") or 0)
        expected_sha256 = str(body_meta.get("sha256") or "")
        blob_ref = body_meta.get("blob_ref")

        reason: str | None = None
        message: str = "Capture record is complete and replayable."
        details: dict[str, Any] = {
            "is_websocket": is_ws,
            "expected_body_size": expected_size,
            "blob_ref": blob_ref,
        }

        if is_ws:
            reason = "websocket"
            message = "WebSocket handshake/session captures are metadata-only and cannot be replayed."
        elif capture_status == "truncated":
            reason = "truncated"
            message = (
                f"Capture {capture_id!r} has status 'truncated' (body exceeded capture limit) "
                "and cannot be replayed."
            )
        elif capture_status == "metadata-only":
            reason = "metadata-only"
            message = f"Capture {capture_id!r} has status 'metadata-only' and cannot be replayed."
        elif capture_status == "capture-failed":
            reason = "capture-failed"
            message = f"Capture {capture_id!r} has status 'capture-failed' and cannot be replayed."
        elif capture_status != "complete" or not replayable_flag:
            reason = capture_status if capture_status != "complete" else "non_replayable"
            message = (
                f"Capture {capture_id!r} is marked non-replayable "
                f"(status={capture_status!r}, replayable={replayable_flag})."
            )
        else:
            if expected_size > 0 and not blob_ref:
                reason = "missing_blob"
                message = (
                    f"Capture {capture_id!r} expects {expected_size} body bytes "
                    "but has no blob_ref."
                )
            elif blob_ref:
                blob_path = self.store.storage_dir / blob_ref
                if not blob_path.is_file():
                    reason = "missing_blob"
                    message = (
                        f"Capture {capture_id!r} body blob file is missing: {blob_ref}"
                    )
                else:
                    blob_bytes = blob_path.read_bytes()
                    details["actual_body_size"] = len(blob_bytes)
                    if len(blob_bytes) != expected_size:
                        reason = "incomplete_blob"
                        message = (
                            f"Capture {capture_id!r} blob size mismatch: expected "
                            f"{expected_size} bytes, found {len(blob_bytes)} bytes."
                        )
                    elif expected_sha256:
                        actual_sha = hashlib.sha256(blob_bytes).hexdigest()
                        details["actual_body_sha256"] = actual_sha
                        if actual_sha != expected_sha256:
                            reason = "corrupt_blob"
                            message = (
                                f"Capture {capture_id!r} blob SHA-256 mismatch: expected "
                                f"{expected_sha256}, found {actual_sha}."
                            )

        if reason is not None:
            err = NonReplayableCaptureError(
                capture_id=capture_id,
                reason=reason,
                capture_status=capture_status,
                message=message,
                details=details,
            )
            if raise_on_false:
                raise err
            return err.to_dict()

        return {
            "capture_id": capture_id,
            "replayable": True,
            "reason": None,
            "capture_status": capture_status,
            "is_websocket": is_ws,
            "expected_body_size": expected_size,
            "blob_ref": blob_ref,
            "message": message,
        }

    def _load_verified_capture(self, capture_id: str) -> tuple[dict[str, Any], bytes]:
        self.evaluate_replayability(capture_id, raise_on_false=True)
        record_path = self.store.captures_dir / f"{capture_id}.json"
        if not record_path.is_file():
            with self.store._connect() as conn:
                idx_row = conn.execute(
                    "SELECT record_file FROM captures_index WHERE capture_id = ?",
                    (capture_id,),
                ).fetchone()
            if idx_row is not None:
                record_path = self.store.storage_dir / idx_row["record_file"]
        record = json.loads(record_path.read_text(encoding="utf-8"))
        blob_ref = record.get("request", {}).get("body", {}).get("blob_ref")
        blob_bytes = (
            (self.store.storage_dir / blob_ref).read_bytes() if blob_ref else b""
        )
        return record, blob_bytes

    def _initial_body_state(
        self, raw_body: bytes, content_type: str | None
    ) -> dict[str, Any]:
        ct_full = (content_type or "").strip()
        ct_lower = ct_full.lower()
        parse_errors: list[str] = []
        body_b64 = base64.b64encode(raw_body).decode("ascii") if raw_body else ""

        if not raw_body:
            return {
                "body_mode": "empty",
                "content_type": content_type,
                "body_base64": "",
                "body_text": "",
                "json_value": None,
                "form_fields": None,
                "multipart_view": None,
                "parse_errors": [],
            }

        if "application/json" in ct_lower or ct_lower.endswith("+json"):
            try:
                text = raw_body.decode("utf-8")
                val = json.loads(text)
                return {
                    "body_mode": "json",
                    "content_type": content_type,
                    "body_base64": body_b64,
                    "body_text": text,
                    "json_value": val,
                    "form_fields": None,
                    "multipart_view": None,
                    "parse_errors": [],
                }
            except Exception as exc:
                parse_errors.append(f"json_parse_error: {exc}")
                return {
                    "body_mode": "raw_text",
                    "content_type": content_type,
                    "body_base64": body_b64,
                    "body_text": raw_body.decode("utf-8", errors="replace"),
                    "json_value": None,
                    "form_fields": None,
                    "multipart_view": None,
                    "parse_errors": parse_errors,
                }

        if "application/x-www-form-urlencoded" in ct_lower:
            try:
                text = raw_body.decode("utf-8")
                fields = [
                    [k, v]
                    for k, v in parse_qsl(
                        text, keep_blank_values=True, strict_parsing=True
                    )
                ]
                return {
                    "body_mode": "urlencoded_form",
                    "content_type": content_type,
                    "body_base64": body_b64,
                    "body_text": text,
                    "json_value": None,
                    "form_fields": fields,
                    "multipart_view": None,
                    "parse_errors": [],
                }
            except Exception as exc:
                parse_errors.append(f"form_parse_error: {exc}")
                return {
                    "body_mode": "raw_text",
                    "content_type": content_type,
                    "body_base64": body_b64,
                    "body_text": raw_body.decode("utf-8", errors="replace"),
                    "json_value": None,
                    "form_fields": None,
                    "multipart_view": None,
                    "parse_errors": parse_errors,
                }

        if "multipart/form-data" in ct_lower:
            views = build_derived_views(
                raw_query="",
                headers=[],
                raw_body=raw_body,
                content_type=content_type,
                capture_status="complete",
            )
            return {
                "body_mode": "multipart_raw",
                "content_type": content_type,
                "body_base64": body_b64,
                "body_text": None,
                "json_value": None,
                "form_fields": None,
                "multipart_view": views.get("multipart_view"),
                "parse_errors": views.get("parse_errors") or [],
            }

        try:
            text = raw_body.decode("utf-8")
            return {
                "body_mode": "raw_text",
                "content_type": content_type,
                "body_base64": body_b64,
                "body_text": text,
                "json_value": None,
                "form_fields": None,
                "multipart_view": None,
                "parse_errors": [],
            }
        except UnicodeDecodeError:
            return {
                "body_mode": "raw_base64",
                "content_type": content_type,
                "body_base64": body_b64,
                "body_text": None,
                "json_value": None,
                "form_fields": None,
                "multipart_view": None,
                "parse_errors": [],
            }

    def create_draft(
        self,
        capture_id: str,
        *,
        draft_id: str | None = None,
        ttl_seconds: int | float | None = None,
        expires_at: str | None = None,
        edits: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.store.ensure_schema()
        record, raw_body = self._load_verified_capture(capture_id)
        req = record["request"]

        now = _now_utc()
        now_iso = now.isoformat()
        clean_draft_id = (
            draft_id.strip()
            if draft_id and str(draft_id).strip()
            else f"draft-{now.strftime('%Y%m%dT%H%M%S%fZ')}-{uuid.uuid4().hex[:8]}"
        )

        exp_iso: str | None = None
        if expires_at is not None and str(expires_at).strip():
            exp_iso = _parse_iso_utc(str(expires_at)).isoformat()
        elif ttl_seconds is not None:
            ttl_val = float(ttl_seconds)
            if ttl_val <= 0:
                raise DraftValidationError(
                    "ttl_seconds must be positive", code="invalid_ttl"
                )
            exp_iso = (now + timedelta(seconds=ttl_val)).isoformat()

        method = str(req.get("method") or "GET").strip().upper()
        scheme = str(req.get("scheme") or "https").strip().lower()
        authority = str(req.get("authority") or "").strip()
        host = authority.split(":", 1)[0].strip().lower()
        raw_path = str(req.get("raw_path") or "/")
        decoded_path = unquote(raw_path)
        raw_query = str(req.get("raw_query") or "")
        query_entries = parse_raw_query_entries(raw_query)
        http_version = str(req.get("http_version") or "HTTP/1.1")
        headers = [[str(k), str(v)] for k, v in (req.get("headers") or [])]
        cookies = parse_cookies_ordered(headers)

        content_type = _get_header_ci(headers, "Content-Type") or req.get(
            "body", {}
        ).get("content_type")
        bstate = self._initial_body_state(raw_body, content_type)

        with self.store._connect() as conn:
            existing = conn.execute(
                "SELECT draft_id FROM request_drafts WHERE draft_id = ?",
                (clean_draft_id,),
            ).fetchone()
            if existing is not None:
                raise DraftValidationError(
                    f"Draft ID already exists: {clean_draft_id}",
                    code="duplicate_draft_id",
                )
            conn.execute(
                """
                INSERT INTO request_drafts (
                    draft_id, source_capture_id, method, scheme, authority, host,
                    raw_path, decoded_path, raw_query, query_entries_json,
                    http_version, headers_json, cookies_json, body_mode,
                    content_type, body_base64, body_text, json_value_json,
                    form_fields_json, multipart_view_json, parse_errors_json,
                    created_at, updated_at, expires_at
                ) VALUES (
                    ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?,
                    ?, ?, ?, ?,
                    ?, ?, ?, ?,
                    ?, ?, ?,
                    ?, ?, ?
                )
                """,
                (
                    clean_draft_id,
                    capture_id,
                    method,
                    scheme,
                    authority,
                    host,
                    raw_path,
                    decoded_path,
                    raw_query,
                    json.dumps(query_entries, ensure_ascii=False),
                    http_version,
                    json.dumps(headers, ensure_ascii=False),
                    json.dumps(cookies, ensure_ascii=False),
                    bstate["body_mode"],
                    bstate["content_type"],
                    bstate["body_base64"],
                    bstate["body_text"],
                    (
                        json.dumps(bstate["json_value"], ensure_ascii=False)
                        if bstate["json_value"] is not None
                        else None
                    ),
                    (
                        json.dumps(bstate["form_fields"], ensure_ascii=False)
                        if bstate["form_fields"] is not None
                        else None
                    ),
                    (
                        json.dumps(bstate["multipart_view"], ensure_ascii=False)
                        if bstate["multipart_view"] is not None
                        else None
                    ),
                    json.dumps(bstate["parse_errors"], ensure_ascii=False),
                    now_iso,
                    now_iso,
                    exp_iso,
                ),
            )
            conn.commit()
        self.store.retention.checkpoint_wal()

        if edits:
            return self.update_draft(clean_draft_id, edits)
        return self.get_draft(clean_draft_id)

    def _fetch_draft_row(
        self, conn: Any, draft_id: str, *, now: datetime | None = None
    ) -> Any:
        row = conn.execute(
            "SELECT * FROM request_drafts WHERE draft_id = ?",
            (draft_id,),
        ).fetchone()
        if row is None:
            raise DraftNotFoundError(f"Draft not found: {draft_id}")
        exp = row["expires_at"]
        if exp:
            ref_now = now or _now_utc()
            if ref_now >= _parse_iso_utc(exp):
                conn.execute(
                    "DELETE FROM request_drafts WHERE draft_id = ?",
                    (draft_id,),
                )
                conn.commit()
                raise DraftExpiredError(draft_id, exp)
        return row

    def prune_expired_drafts(self, *, now: str | None = None) -> dict[str, Any]:
        self.store.ensure_schema()
        ref_now = _parse_iso_utc(now) if now else _now_utc()
        expired_ids: list[str] = []
        with self.store._connect() as conn:
            rows = conn.execute(
                "SELECT draft_id, expires_at FROM request_drafts WHERE expires_at IS NOT NULL"
            ).fetchall()
            for r in rows:
                if ref_now >= _parse_iso_utc(r["expires_at"]):
                    expired_ids.append(r["draft_id"])
            if expired_ids:
                conn.executemany(
                    "DELETE FROM request_drafts WHERE draft_id = ?",
                    [(did,) for did in expired_ids],
                )
                conn.commit()
        if expired_ids:
            self.store.retention.checkpoint_wal()
        return {
            "pruned_count": len(expired_ids),
            "pruned_draft_ids": expired_ids,
            "reference_now": ref_now.isoformat(),
        }

    def list_drafts(
        self, *, source_capture_id: str | None = None, limit: int = 100
    ) -> dict[str, Any]:
        self.store.ensure_schema()
        self.prune_expired_drafts()
        safe_limit = max(1, min(int(limit), 500))
        with self.store._connect() as conn:
            if source_capture_id:
                rows = conn.execute(
                    """
                    SELECT * FROM request_drafts
                    WHERE source_capture_id = ?
                    ORDER BY created_at DESC
                    LIMIT ?
                    """,
                    (source_capture_id, safe_limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT * FROM request_drafts
                    ORDER BY created_at DESC
                    LIMIT ?
                    """,
                    (safe_limit,),
                ).fetchall()
            items = [self._row_to_draft_dict(conn, r) for r in rows]
        return {
            "total": len(items),
            "items": items,
        }

    def get_draft(
        self, draft_id: str, *, now: str | None = None
    ) -> dict[str, Any]:
        self.store.ensure_schema()
        ref_now = _parse_iso_utc(now) if now else _now_utc()
        with self.store._connect() as conn:
            row = self._fetch_draft_row(conn, draft_id, now=ref_now)
            return self._row_to_draft_dict(conn, row)

    def delete_draft(self, draft_id: str) -> dict[str, Any]:
        self.store.ensure_schema()
        with self.store._connect() as conn:
            row = conn.execute(
                "SELECT draft_id, source_capture_id FROM request_drafts WHERE draft_id = ?",
                (draft_id,),
            ).fetchone()
            if row is None:
                raise DraftNotFoundError(f"Draft not found: {draft_id}")
            conn.execute(
                "DELETE FROM request_drafts WHERE draft_id = ?",
                (draft_id,),
            )
            conn.commit()
        self.store.retention.checkpoint_wal()
        return {
            "deleted": True,
            "draft_id": draft_id,
            "source_capture_id": row["source_capture_id"],
        }

    def update_draft(
        self, draft_id: str, updates: dict[str, Any]
    ) -> dict[str, Any]:
        self.store.ensure_schema()
        now = _now_utc()
        now_iso = now.isoformat()

        with self.store._connect() as conn:
            row = self._fetch_draft_row(conn, draft_id, now=now)

            method = str(row["method"])
            if "method" in updates and updates["method"] is not None:
                cand_m = str(updates["method"]).strip().upper()
                if not cand_m or not cand_m.isalpha():
                    raise DraftValidationError(
                        f"Invalid HTTP method: {updates['method']!r}",
                        code="invalid_method",
                    )
                method = cand_m

            scheme = str(row["scheme"])
            if "scheme" in updates and updates["scheme"] is not None:
                cand_s = str(updates["scheme"]).strip().lower()
                if cand_s not in ("http", "https"):
                    raise DraftValidationError(
                        f"Invalid scheme {cand_s!r}: must be 'http' or 'https'",
                        code="invalid_scheme",
                    )
                scheme = cand_s

            authority = str(row["authority"])
            if "authority" in updates and updates["authority"] is not None:
                authority = str(updates["authority"]).strip()
            elif "host" in updates and updates["host"] is not None:
                authority = str(updates["host"]).strip()
            if not authority or any(c.isspace() for c in authority):
                raise DraftValidationError(
                    f"Invalid authority/host: {authority!r}",
                    code="invalid_authority",
                )
            host = authority.split(":", 1)[0].strip().lower()

            # Path updates: supports raw_path, decoded_path, or path
            raw_path = str(row["raw_path"])
            decoded_path = str(row["decoded_path"])
            if "raw_path" in updates and updates["raw_path"] is not None:
                rp = str(updates["raw_path"]).strip()
                if not rp.startswith("/"):
                    rp = "/" + rp
                if "?" in rp:
                    rp, extra_q = rp.split("?", 1)
                    if "raw_query" not in updates and "query_params" not in updates:
                        updates = {**updates, "raw_query": extra_q}
                raw_path = rp
                decoded_path = unquote(raw_path)
            elif "decoded_path" in updates and updates["decoded_path"] is not None:
                dp = str(updates["decoded_path"])
                if not dp.startswith("/"):
                    dp = "/" + dp
                decoded_path = dp
                raw_path = encode_decoded_path(decoded_path)
            elif "path" in updates and updates["path"] is not None:
                p_in = str(updates["path"])
                if not p_in.startswith("/"):
                    p_in = "/" + p_in
                # If path contains unencoded spaces or non-ASCII, treat as decoded_path; if already valid ASCII URI path, preserve %XX
                if any(ord(ch) > 127 or ch.isspace() for ch in p_in):
                    decoded_path = p_in
                    raw_path = encode_decoded_path(decoded_path)
                else:
                    raw_path = p_in
                    decoded_path = unquote(raw_path)

            # Query updates: supports raw_query, query_entries, query_params, and query_mutations
            query_entries: list[dict[str, Any]] = json.loads(
                row["query_entries_json"]
            )
            if "raw_query" in updates and updates["raw_query"] is not None:
                rq = str(updates["raw_query"]).lstrip("?")
                query_entries = parse_raw_query_entries(rq)
            elif "query_entries" in updates and updates["query_entries"] is not None:
                q_in = updates["query_entries"]
                if not isinstance(q_in, list):
                    raise DraftValidationError(
                        "query_entries must be a list", code="invalid_query"
                    )
                new_entries: list[dict[str, Any]] = []
                for item in q_in:
                    if isinstance(item, (list, tuple)) and len(item) == 2:
                        k, v = str(item[0]), str(item[1])
                        new_entries.append(
                            {
                                "name": k,
                                "value": v,
                                "raw_name": encode_uri_component(k),
                                "raw_value": encode_uri_component(v),
                                "has_equals": True,
                            }
                        )
                    elif isinstance(item, dict):
                        k = str(item.get("name", item.get("key", "")))
                        v = str(item.get("value", ""))
                        new_entries.append(
                            {
                                "name": k,
                                "value": v,
                                "raw_name": item.get("raw_name")
                                or encode_uri_component(k),
                                "raw_value": item.get("raw_value")
                                or encode_uri_component(v),
                                "has_equals": bool(item.get("has_equals", True)),
                            }
                        )
                    else:
                        raise DraftValidationError(
                            f"Invalid query entry: {item!r}",
                            code="invalid_query",
                        )
                query_entries = new_entries
            elif "query_params" in updates and updates["query_params"] is not None:
                if not isinstance(updates["query_params"], list):
                    raise DraftValidationError(
                        "query_params must be a list of [name, value] pairs",
                        code="invalid_query",
                    )
                query_entries = entries_from_pairs(updates["query_params"])

            if "query_mutations" in updates and updates["query_mutations"]:
                q_pairs = [[e["name"], e["value"]] for e in query_entries]
                mutated_q_pairs = apply_pair_mutations(
                    q_pairs,
                    updates["query_mutations"],
                    case_insensitive_name=False,
                    field_label="query",
                )
                # Preserve raw_name/raw_value only when entry was not changed at the same position
                rebuilt_entries: list[dict[str, Any]] = []
                for k, v in mutated_q_pairs:
                    rebuilt_entries.append(
                        {
                            "name": k,
                            "value": v,
                            "raw_name": encode_uri_component(k),
                            "raw_value": encode_uri_component(v),
                            "has_equals": True,
                        }
                    )
                query_entries = rebuilt_entries

            raw_query = build_raw_query_from_entries(query_entries)

            # Headers & Cookies updates
            headers: list[list[str]] = json.loads(row["headers_json"])
            cookies: list[list[str]] = json.loads(row["cookies_json"])
            headers_modified = False
            cookies_modified = False

            if "headers" in updates and updates["headers"] is not None:
                if not isinstance(updates["headers"], list):
                    raise DraftValidationError(
                        "headers must be a list of [name, value] pairs",
                        code="invalid_headers",
                    )
                headers = [
                    [str(item[0]), str(item[1])] for item in updates["headers"]
                ]
                headers_modified = True

            if "header_mutations" in updates and updates["header_mutations"]:
                headers = apply_pair_mutations(
                    headers,
                    updates["header_mutations"],
                    case_insensitive_name=True,
                    field_label="header",
                )
                headers_modified = True

            if "cookies" in updates and updates["cookies"] is not None:
                if not isinstance(updates["cookies"], list):
                    raise DraftValidationError(
                        "cookies must be a list of [name, value] pairs",
                        code="invalid_cookies",
                    )
                cookies = [
                    [str(item[0]), str(item[1])] for item in updates["cookies"]
                ]
                cookies_modified = True

            if "cookie_mutations" in updates and updates["cookie_mutations"]:
                if headers_modified and not cookies_modified:
                    cookies = parse_cookies_ordered(headers)
                cookies = apply_pair_mutations(
                    cookies,
                    updates["cookie_mutations"],
                    case_insensitive_name=False,
                    field_label="cookie",
                )
                cookies_modified = True

            if cookies_modified:
                headers = sync_cookies_into_headers(headers, cookies)
            elif headers_modified:
                cookies = parse_cookies_ordered(headers)

            # Body updates
            body_mode = str(row["body_mode"])
            content_type = _get_header_ci(headers, "Content-Type") or row["content_type"]
            body_base64 = str(row["body_base64"] or "")
            body_text = row["body_text"]
            json_value = (
                json.loads(row["json_value_json"])
                if row["json_value_json"] is not None
                else None
            )
            form_fields: list[list[str]] | None = (
                json.loads(row["form_fields_json"])
                if row["form_fields_json"] is not None
                else None
            )
            multipart_view = (
                json.loads(row["multipart_view_json"])
                if row["multipart_view_json"] is not None
                else None
            )
            parse_errors: list[str] = json.loads(row["parse_errors_json"] or "[]")

            if "content_type" in updates and updates["content_type"] is not None:
                content_type = str(updates["content_type"]).strip()
                headers = apply_pair_mutations(
                    headers,
                    [{"op": "set", "name": "Content-Type", "value": content_type}],
                    case_insensitive_name=True,
                    field_label="header",
                )

            if "json_value" in updates:
                json_value = updates["json_value"]
                body_mode = "json"
                body_text = json.dumps(json_value, ensure_ascii=False)
                raw_bytes = body_text.encode("utf-8")
                body_base64 = base64.b64encode(raw_bytes).decode("ascii")
                form_fields = None
                parse_errors = []
                if not content_type or "json" not in content_type.lower():
                    content_type = "application/json; charset=utf-8"
                    headers = _ensure_header_ci(
                        headers, "Content-Type", content_type
                    )

            elif "json_text" in updates and updates["json_text"] is not None:
                raw_json_str = str(updates["json_text"])
                try:
                    parsed_json = json.loads(raw_json_str)
                except Exception as exc:
                    raise DraftValidationError(
                        f"Invalid JSON body text: {exc}. Use raw_text fallback if sending malformed JSON intentionally.",
                        code="invalid_json_body",
                        details={"parse_error": str(exc), "fallback_mode": "raw_text"},
                    ) from exc
                json_value = parsed_json
                body_mode = "json"
                body_text = raw_json_str
                raw_bytes = raw_json_str.encode("utf-8")
                body_base64 = base64.b64encode(raw_bytes).decode("ascii")
                form_fields = None
                parse_errors = []
                if not content_type or "json" not in content_type.lower():
                    content_type = "application/json; charset=utf-8"
                    headers = _ensure_header_ci(
                        headers, "Content-Type", content_type
                    )

            elif "form_fields" in updates and updates["form_fields"] is not None:
                if not isinstance(updates["form_fields"], list):
                    raise DraftValidationError(
                        "form_fields must be a list of [name, value] pairs",
                        code="invalid_form_fields",
                    )
                form_fields = [
                    [str(item[0]), str(item[1])] for item in updates["form_fields"]
                ]
                body_mode = "urlencoded_form"
                body_text = "&".join(
                    f"{encode_uri_component(k)}={encode_uri_component(v)}"
                    for k, v in form_fields
                )
                raw_bytes = body_text.encode("utf-8")
                body_base64 = base64.b64encode(raw_bytes).decode("ascii")
                json_value = None
                parse_errors = []
                if (
                    not content_type
                    or "application/x-www-form-urlencoded" not in content_type.lower()
                ):
                    content_type = "application/x-www-form-urlencoded"
                    headers = _ensure_header_ci(
                        headers, "Content-Type", content_type
                    )

            elif "form_mutations" in updates and updates["form_mutations"]:
                if body_mode != "urlencoded_form" and form_fields is None:
                    raise DraftValidationError(
                        "Cannot apply structured form_mutations because current draft body is not "
                        "application/x-www-form-urlencoded. Provide form_fields or use raw_text fallback.",
                        code="not_form_body",
                        details={"current_body_mode": body_mode, "parse_errors": parse_errors},
                    )
                form_fields = apply_pair_mutations(
                    form_fields or [],
                    updates["form_mutations"],
                    case_insensitive_name=False,
                    field_label="form",
                )
                body_mode = "urlencoded_form"
                body_text = "&".join(
                    f"{encode_uri_component(k)}={encode_uri_component(v)}"
                    for k, v in form_fields
                )
                raw_bytes = body_text.encode("utf-8")
                body_base64 = base64.b64encode(raw_bytes).decode("ascii")
                json_value = None
                parse_errors = []

            elif "raw_text" in updates and updates["raw_text"] is not None:
                body_text = str(updates["raw_text"])
                raw_bytes = body_text.encode("utf-8")
                body_base64 = base64.b64encode(raw_bytes).decode("ascii")
                req_mode = str(updates.get("body_mode") or "raw_text")
                if req_mode == "empty" or len(raw_bytes) == 0:
                    body_mode = "empty" if len(raw_bytes) == 0 else "raw_text"
                    json_value = None
                    form_fields = None
                    parse_errors = []
                else:
                    body_mode = "raw_text"
                    json_value = None
                    form_fields = None
                    parse_errors = []
                    ct_l = (content_type or "").lower()
                    if "application/json" in ct_l or ct_l.endswith("+json"):
                        try:
                            json_value = json.loads(body_text)
                        except Exception as exc:
                            parse_errors.append(f"json_parse_error: {exc}")
                    elif "application/x-www-form-urlencoded" in ct_l:
                        try:
                            form_fields = [
                                [k, v]
                                for k, v in parse_qsl(
                                    body_text,
                                    keep_blank_values=True,
                                    strict_parsing=True,
                                )
                            ]
                        except Exception as exc:
                            parse_errors.append(f"form_parse_error: {exc}")

            elif "raw_base64" in updates and updates["raw_base64"] is not None:
                b64_in = str(updates["raw_base64"]).strip()
                try:
                    raw_bytes = base64.b64decode(b64_in, validate=True) if b64_in else b""
                except Exception as exc:
                    raise DraftValidationError(
                        f"Invalid base64 body payload: {exc}",
                        code="invalid_base64_body",
                    ) from exc
                body_base64 = base64.b64encode(raw_bytes).decode("ascii")
                body_mode = "empty" if not raw_bytes else "raw_base64"
                try:
                    body_text = raw_bytes.decode("utf-8")
                except UnicodeDecodeError:
                    body_text = None
                json_value = None
                form_fields = None
                parse_errors = []

            elif "body_mode" in updates and updates["body_mode"] == "empty":
                body_mode = "empty"
                body_text = ""
                body_base64 = ""
                json_value = None
                form_fields = None
                parse_errors = []

            if "json_mutations" in updates and updates["json_mutations"]:
                if body_mode != "json" and json_value is None:
                    raise DraftValidationError(
                        "Cannot apply structured json_mutations because current draft body is not valid JSON. "
                        "Provide json_value or edit via raw_text fallback.",
                        code="not_json_body",
                        details={"current_body_mode": body_mode, "parse_errors": parse_errors},
                    )
                json_value = apply_json_path_mutations(
                    json_value, updates["json_mutations"]
                )
                body_mode = "json"
                body_text = json.dumps(json_value, ensure_ascii=False)
                raw_bytes = body_text.encode("utf-8")
                body_base64 = base64.b64encode(raw_bytes).decode("ascii")
                form_fields = None
                parse_errors = []
                if not content_type or "json" not in content_type.lower():
                    content_type = "application/json; charset=utf-8"
                    headers = _ensure_header_ci(
                        headers, "Content-Type", content_type
                    )

            exp_iso = row["expires_at"]
            if "expires_at" in updates:
                exp_iso = (
                    _parse_iso_utc(str(updates["expires_at"])).isoformat()
                    if updates["expires_at"]
                    else None
                )
            elif "ttl_seconds" in updates and updates["ttl_seconds"] is not None:
                ttl_val = float(updates["ttl_seconds"])
                if ttl_val <= 0:
                    raise DraftValidationError(
                        "ttl_seconds must be positive", code="invalid_ttl"
                    )
                exp_iso = (now + timedelta(seconds=ttl_val)).isoformat()

            conn.execute(
                """
                UPDATE request_drafts
                SET method = ?,
                    scheme = ?,
                    authority = ?,
                    host = ?,
                    raw_path = ?,
                    decoded_path = ?,
                    raw_query = ?,
                    query_entries_json = ?,
                    headers_json = ?,
                    cookies_json = ?,
                    body_mode = ?,
                    content_type = ?,
                    body_base64 = ?,
                    body_text = ?,
                    json_value_json = ?,
                    form_fields_json = ?,
                    multipart_view_json = ?,
                    parse_errors_json = ?,
                    updated_at = ?,
                    expires_at = ?
                WHERE draft_id = ?
                """,
                (
                    method,
                    scheme,
                    authority,
                    host,
                    raw_path,
                    decoded_path,
                    raw_query,
                    json.dumps(query_entries, ensure_ascii=False),
                    json.dumps(headers, ensure_ascii=False),
                    json.dumps(cookies, ensure_ascii=False),
                    body_mode,
                    content_type,
                    body_base64,
                    body_text,
                    (
                        json.dumps(json_value, ensure_ascii=False)
                        if json_value is not None
                        else None
                    ),
                    (
                        json.dumps(form_fields, ensure_ascii=False)
                        if form_fields is not None
                        else None
                    ),
                    (
                        json.dumps(multipart_view, ensure_ascii=False)
                        if multipart_view is not None
                        else None
                    ),
                    json.dumps(parse_errors, ensure_ascii=False),
                    now_iso,
                    exp_iso,
                    draft_id,
                ),
            )
            updated_row = self._fetch_draft_row(conn, draft_id, now=now)
            conn.commit()
            out = self._row_to_draft_dict(conn, updated_row)

        self.store.retention.checkpoint_wal()
        return out

    def _normalize_row(self, row: Any) -> dict[str, Any]:
        method = str(row["method"]).upper()
        scheme = str(row["scheme"]).lower()
        authority = str(row["authority"])
        host = str(row["host"])
        raw_path = str(row["raw_path"] or "/")
        raw_query = str(row["raw_query"] or "")
        target_uri = f"{raw_path}?{raw_query}" if raw_query else raw_path
        url = f"{scheme}://{authority}{target_uri}"

        headers: list[list[str]] = json.loads(row["headers_json"])
        cookies: list[list[str]] = json.loads(row["cookies_json"])
        headers = sync_cookies_into_headers(headers, cookies)

        body_b64 = str(row["body_base64"] or "")
        raw_body = base64.b64decode(body_b64) if body_b64 else b""
        body_size = len(raw_body)
        body_sha256 = hashlib.sha256(raw_body).hexdigest()

        # Discover dynamic hop-by-hop headers named in Connection header
        connection_tokens: set[str] = set()
        for k, v in headers:
            if k.lower() == "connection":
                for tok in v.split(","):
                    t_clean = tok.strip().lower()
                    if t_clean:
                        connection_tokens.add(t_clean)

        excluded_headers: list[list[str]] = []
        filtered_headers: list[list[str]] = [["Host", authority]]
        for k, v in headers:
            kl = k.lower()
            if kl in HOP_BY_HOP_AND_REBUILT_HEADERS or kl in connection_tokens:
                excluded_headers.append([k, v])
                continue
            filtered_headers.append([k, v])

        filtered_headers.append(["Content-Length", str(body_size)])

        return {
            "method": method,
            "scheme": scheme,
            "authority": authority,
            "host": host,
            "raw_path": raw_path,
            "raw_query": raw_query,
            "target_uri": target_uri,
            "url": url,
            "headers": filtered_headers,
            "excluded_hop_by_hop_headers": excluded_headers,
            "content_length": body_size,
            "body_bytes": raw_body,
            "body_size": body_size,
            "body_sha256": body_sha256,
            "side_effect_warning": build_side_effect_warning(method),
        }

    def _row_to_draft_dict(self, conn: Any, row: Any) -> dict[str, Any]:
        query_entries: list[dict[str, Any]] = json.loads(
            row["query_entries_json"]
        )
        query_params = [[e["name"], e["value"]] for e in query_entries]
        headers: list[list[str]] = json.loads(row["headers_json"])
        cookies: list[list[str]] = json.loads(row["cookies_json"])
        json_value = (
            json.loads(row["json_value_json"])
            if row["json_value_json"] is not None
            else None
        )
        form_fields = (
            json.loads(row["form_fields_json"])
            if row["form_fields_json"] is not None
            else None
        )
        multipart_view = (
            json.loads(row["multipart_view_json"])
            if row["multipart_view_json"] is not None
            else None
        )
        parse_errors = json.loads(row["parse_errors_json"] or "[]")
        norm = self._normalize_row(row)

        replay_rows = conn.execute(
            """
            SELECT * FROM replay_runs
            WHERE draft_id = ?
            ORDER BY replayed_at DESC
            """,
            (row["draft_id"],),
        ).fetchall()
        replays = [self._replay_row_to_dict(r) for r in replay_rows]

        raw_path = str(row["raw_path"])
        raw_query = str(row["raw_query"])
        raw_target = f"{raw_path}?{raw_query}" if raw_query else raw_path

        return {
            "draft_id": row["draft_id"],
            "source_capture_id": row["source_capture_id"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "expires_at": row["expires_at"],
            "request": {
                "method": row["method"],
                "scheme": row["scheme"],
                "authority": row["authority"],
                "host": row["host"],
                "raw_path": raw_path,
                "decoded_path": row["decoded_path"],
                "raw_query": raw_query,
                "raw_target": raw_target,
                "http_version": row["http_version"],
                "query_params": query_params,
                "query_entries": query_entries,
                "headers": headers,
                "cookies": cookies,
                "body": {
                    "body_mode": row["body_mode"],
                    "content_type": row["content_type"],
                    "size": norm["body_size"],
                    "sha256": norm["body_sha256"],
                    "raw_text": row["body_text"],
                    "raw_base64": row["body_base64"],
                    "json_value": json_value,
                    "form_fields": form_fields,
                    "multipart_view": multipart_view,
                    "parse_errors": parse_errors,
                },
            },
            "normalized_preview": {
                "method": norm["method"],
                "url": norm["url"],
                "raw_path": norm["raw_path"],
                "raw_query": norm["raw_query"],
                "target_uri": norm["target_uri"],
                "headers": norm["headers"],
                "excluded_hop_by_hop_headers": norm[
                    "excluded_hop_by_hop_headers"
                ],
                "content_length": norm["content_length"],
                "body_size": norm["body_size"],
                "body_sha256": norm["body_sha256"],
            },
            "side_effect_warning": norm["side_effect_warning"],
            "replays_count": len(replays),
            "replays": replays,
        }

    def _replay_row_to_dict(self, r: Any) -> dict[str, Any]:
        return {
            "replay_id": r["replay_id"],
            "draft_id": r["draft_id"],
            "source_capture_id": r["source_capture_id"],
            "result_capture_id": r["result_capture_id"],
            "replayed_at": r["replayed_at"],
            "method": r["method"],
            "scheme": r["scheme"],
            "authority": r["authority"],
            "raw_target": r["raw_target"],
            "request_body_size": r["request_body_size"],
            "request_body_sha256": r["request_body_sha256"],
            "response_status": r["response_status"],
            "duration_ms": r["duration_ms"],
            "side_effect_warning": json.loads(
                r["side_effect_warning_json"] or "{}"
            ),
            "error_message": r["error_message"],
        }

    def export_draft(
        self,
        draft_id: str,
        *,
        format_type: str = "all",
        edge_url: str | None = None,
        cacert: str | None = None,
    ) -> dict[str, Any]:
        self.store.ensure_schema()
        with self.store._connect() as conn:
            row = self._fetch_draft_row(conn, draft_id)
            norm = self._normalize_row(row)

        method = norm["method"]
        url = norm["url"]
        authority = norm["authority"]
        host_only = norm["host"]
        target_uri = norm["target_uri"]
        headers = norm["headers"]
        raw_body: bytes = norm["body_bytes"]
        body_text = row["body_text"]

        # 1. Standard portable cURL command
        curl_tokens: list[str] = ["curl", "-sS", "-i", "-X", method, url]
        for k, v in headers:
            curl_tokens.extend(["-H", f"{k}: {v}"])
        if raw_body:
            if body_text is not None:
                curl_tokens.extend(["--data-raw", body_text])
            else:
                curl_tokens.extend(["--data-binary", raw_body.decode("latin-1")])
        curl_cmd = " ".join(shlex.quote(tok) for tok in curl_tokens)

        # 2. Local HTTPS Edge executable cURL command (with --resolve and --cacert)
        eff_edge = edge_url or os.environ.get(
            "PUBLIC_EDGE_HTTPS_URL",
            f"https://127.0.0.1:{os.environ.get('EDGE_HTTPS_PORT', '8443')}",
        )
        parsed_edge = urlsplit(eff_edge)
        edge_port = parsed_edge.port or (
            443 if (parsed_edge.scheme or "https") == "https" else 80
        )
        edge_target_url = f"https://{host_only}:{edge_port}{target_uri}"
        edge_tokens: list[str] = ["curl", "-sS", "-X", method]
        if cacert:
            edge_tokens.extend(["--cacert", cacert])
        else:
            edge_tokens.append("-k")
        edge_tokens.extend(
            ["--resolve", f"{host_only}:{edge_port}:127.0.0.1", edge_target_url]
        )
        for k, v in headers:
            edge_tokens.extend(["-H", f"{k}: {v}"])
        if raw_body:
            if body_text is not None:
                edge_tokens.extend(["--data-raw", body_text])
            else:
                edge_tokens.extend(["--data-binary", raw_body.decode("latin-1")])
        curl_edge_cmd = " ".join(shlex.quote(tok) for tok in edge_tokens)

        # 3. Browser / ES Standard fetch() JavaScript snippet
        # Use tuple array for headers to preserve exact header order and duplicate keys
        fetch_headers_pairs = [[k, v] for k, v in headers]
        fetch_init: dict[str, Any] = {
            "method": method,
            "headers": fetch_headers_pairs,
        }
        if raw_body and method not in ("GET", "HEAD"):
            if body_text is not None:
                fetch_init["body"] = body_text
            else:
                fetch_init["body_base64"] = base64.b64encode(raw_body).decode(
                    "ascii"
                )

        url_js = json.dumps(url, ensure_ascii=False)
        init_lines: list[str] = [
            f"  method: {json.dumps(method)},",
            f"  headers: {json.dumps(fetch_headers_pairs, ensure_ascii=False)},",
        ]
        if raw_body and method not in ("GET", "HEAD"):
            if body_text is not None:
                init_lines.append(
                    f"  body: {json.dumps(body_text, ensure_ascii=False)},"
                )
            else:
                b64_js = json.dumps(
                    base64.b64encode(raw_body).decode("ascii")
                )
                init_lines.append(
                    f"  body: Uint8Array.from(atob({b64_js}), c => c.charCodeAt(0)),"
                )
        fetch_js = (
            f"const response = await fetch({url_js}, {{\n"
            + "\n".join(init_lines)
            + "\n});\n"
        )

        out: dict[str, Any] = {
            "draft_id": draft_id,
            "source_capture_id": row["source_capture_id"],
            "updated_at": row["updated_at"],
            "method": method,
            "url": url,
            "authority": authority,
            "target_uri": target_uri,
            "headers": headers,
            "content_length": norm["content_length"],
            "body_size": norm["body_size"],
            "body_sha256": norm["body_sha256"],
            "side_effect_warning": norm["side_effect_warning"],
            "curl": curl_cmd,
            "curl_local_edge": curl_edge_cmd,
            "fetch": fetch_js,
            "fetch_request": {
                "url": url,
                "edge_url": edge_target_url,
                "init": fetch_init,
            },
        }
        fmt = format_type.strip().lower()
        if fmt == "curl":
            return {
                "draft_id": draft_id,
                "format": "curl",
                "curl": curl_cmd,
                "curl_local_edge": curl_edge_cmd,
                "side_effect_warning": norm["side_effect_warning"],
            }
        if fmt == "fetch":
            return {
                "draft_id": draft_id,
                "format": "fetch",
                "fetch": fetch_js,
                "fetch_request": out["fetch_request"],
                "side_effect_warning": norm["side_effect_warning"],
            }
        return out

    def replay_draft(
        self,
        draft_id: str,
        *,
        edge_url: str | None = None,
        cacert: str | None = None,
        override_host: str | None = None,
    ) -> dict[str, Any]:
        self.store.ensure_schema()
        with self.store._connect() as conn:
            row = self._fetch_draft_row(conn, draft_id)
            source_capture_id = str(row["source_capture_id"])
            norm = self._normalize_row(row)

        # Ensure source capture is still valid/replayable before sending
        self.evaluate_replayability(source_capture_id, raise_on_false=True)

        return self._execute_normalized_replay(
            norm=norm,
            source_capture_id=source_capture_id,
            draft_id=draft_id,
            edge_url=edge_url,
            cacert=cacert,
            override_host=override_host,
        )

    def replay_capture(
        self,
        capture_id: str,
        *,
        edge_url: str | None = None,
        cacert: str | None = None,
        override_host: str | None = None,
    ) -> dict[str, Any]:
        self.store.ensure_schema()
        record, raw_body = self._load_verified_capture(capture_id)
        req = record["request"]
        authority = str(override_host or req.get("authority") or "").strip()
        host = authority.split(":", 1)[0].strip().lower()
        raw_path = str(req.get("raw_path") or "/")
        raw_query = str(req.get("raw_query") or "")
        headers = [[str(k), str(v)] for k, v in (req.get("headers") or [])]
        cookies = parse_cookies_ordered(headers)

        temp_row = {
            "method": str(req.get("method") or "GET").upper(),
            "scheme": str(req.get("scheme") or "https").lower(),
            "authority": authority,
            "host": host,
            "raw_path": raw_path,
            "raw_query": raw_query,
            "headers_json": json.dumps(headers),
            "cookies_json": json.dumps(cookies),
            "body_base64": base64.b64encode(raw_body).decode("ascii"),
        }
        norm = self._normalize_row(temp_row)
        return self._execute_normalized_replay(
            norm=norm,
            source_capture_id=capture_id,
            draft_id=None,
            edge_url=edge_url,
            cacert=cacert,
            override_host=override_host,
        )

    def _execute_normalized_replay(
        self,
        *,
        norm: dict[str, Any],
        source_capture_id: str,
        draft_id: str | None,
        edge_url: str | None,
        cacert: str | None,
        override_host: str | None,
    ) -> dict[str, Any]:
        eff_edge = edge_url or os.environ.get(
            "REPLAY_EDGE_URL", "https://edge:443"
        )
        eff_cacert = cacert if cacert is not None else os.environ.get("REPLAY_CACERT")
        if eff_cacert and not Path(eff_cacert).is_file():
            eff_cacert = None

        authority = str(override_host or norm["authority"]).strip()
        sni_host = authority.split(":", 1)[0].strip()
        target_uri = norm["target_uri"]
        method = norm["method"]
        raw_body: bytes = norm["body_bytes"]

        # Update Host header if override_host was specified
        send_headers: list[list[str]] = []
        for k, v in norm["headers"]:
            if k.lower() == "host":
                send_headers.append(["Host", authority])
            else:
                send_headers.append([k, v])
        send_headers.append(["Connection", "close"])

        parsed_edge = urlsplit(eff_edge)
        edge_scheme = parsed_edge.scheme or "https"
        edge_host = parsed_edge.hostname or "127.0.0.1"
        edge_port = parsed_edge.port or (443 if edge_scheme == "https" else 80)

        now = _now_utc()
        replay_id = (
            f"rpl-{now.strftime('%Y%m%dT%H%M%S%fZ')}-{uuid.uuid4().hex[:8]}"
        )
        start_mono = time.monotonic()

        raw_sock = socket.create_connection((edge_host, edge_port), timeout=30.0)
        if edge_scheme == "https":
            if eff_cacert:
                ssl_ctx = ssl.create_default_context(cafile=eff_cacert)
                ssl_ctx.check_hostname = False
            else:
                ssl_ctx = ssl._create_unverified_context()
            conn_sock = ssl_ctx.wrap_socket(raw_sock, server_hostname=sni_host)
        else:
            conn_sock = raw_sock

        http_conn = http.client.HTTPConnection(
            edge_host, edge_port, timeout=30.0
        )
        http_conn.sock = conn_sock
        try:
            http_conn.putrequest(
                method,
                target_uri,
                skip_host=True,
                skip_accept_encoding=True,
            )
            for k, v in send_headers:
                http_conn.putheader(k, v)
            http_conn.endheaders(message_body=raw_body if raw_body else None)

            resp = http_conn.getresponse()
            resp_bytes = resp.read()
            duration_ms = round((time.monotonic() - start_mono) * 1000.0, 3)
            resp_headers = [[k, v] for k, v in resp.getheaders()]
            result_capture_id = resp.getheader("X-Capture-Id")
            resp_status = resp.status
        finally:
            http_conn.close()

        resp_text = resp_bytes.decode("utf-8", errors="replace")
        try:
            resp_json = json.loads(resp_text)
        except ValueError:
            resp_json = None

        # Ensure the new capture is indexed in SQLite immediately so callers can search it right away
        if result_capture_id:
            for _ in range(20):
                rec_file = self.store.captures_dir / f"{result_capture_id}.json"
                if rec_file.is_file():
                    try:
                        rec_dict = json.loads(rec_file.read_text(encoding="utf-8"))
                        self.store.index_record_dict(
                            rec_dict, f"captures/{result_capture_id}.json"
                        )
                    except Exception:
                        pass
                    break
                time.sleep(0.02)

        with self.store._connect() as conn:
            conn.execute(
                """
                INSERT INTO replay_runs (
                    replay_id, draft_id, source_capture_id, result_capture_id,
                    replayed_at, method, scheme, authority, raw_target,
                    request_body_size, request_body_sha256, response_status,
                    duration_ms, side_effect_warning_json, error_message
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    replay_id,
                    draft_id,
                    source_capture_id,
                    result_capture_id,
                    now.isoformat(),
                    method,
                    norm["scheme"],
                    authority,
                    target_uri,
                    norm["body_size"],
                    norm["body_sha256"],
                    resp_status,
                    duration_ms,
                    json.dumps(norm["side_effect_warning"], ensure_ascii=False),
                    None,
                ),
            )
            conn.commit()
        self.store.retention.checkpoint_wal()

        return {
            "replay_id": replay_id,
            "draft_id": draft_id,
            "replayed_from_capture_id": source_capture_id,
            "source_capture_id": source_capture_id,
            "result_capture_id": result_capture_id,
            "replayed_at": now.isoformat(),
            "duration_ms": duration_ms,
            "side_effect_warning": norm["side_effect_warning"],
            "replay_request": {
                "method": method,
                "scheme": norm["scheme"],
                "authority": authority,
                "target_uri": target_uri,
                "raw_path": norm["raw_path"],
                "raw_query": norm["raw_query"],
                "headers": [h for h in send_headers if h[0].lower() != "connection"],
                "excluded_hop_by_hop_headers": norm[
                    "excluded_hop_by_hop_headers"
                ],
                "content_length": norm["content_length"],
                "body_size": norm["body_size"],
                "body_sha256": norm["body_sha256"],
            },
            "response_status": resp_status,
            "response_headers": resp_headers,
            "response_body": resp_text,
            "response_json": resp_json,
        }

    def get_capture_replay_relations(
        self, capture_id: str
    ) -> dict[str, Any]:
        """Return replay lineage where capture_id is either the result of a replay or the source of replays/drafts."""
        self.store.ensure_schema()
        with self.store._connect() as conn:
            origin_row = conn.execute(
                """
                SELECT * FROM replay_runs
                WHERE result_capture_id = ?
                ORDER BY replayed_at DESC
                LIMIT 1
                """,
                (capture_id,),
            ).fetchone()
            child_rows = conn.execute(
                """
                SELECT * FROM replay_runs
                WHERE source_capture_id = ?
                ORDER BY replayed_at DESC
                """,
                (capture_id,),
            ).fetchall()
            draft_rows = conn.execute(
                """
                SELECT draft_id, created_at, updated_at, expires_at
                FROM request_drafts
                WHERE source_capture_id = ?
                ORDER BY created_at DESC
                """,
                (capture_id,),
            ).fetchall()

        return {
            "is_replay": origin_row is not None,
            "replayed_from": (
                self._replay_row_to_dict(origin_row)
                if origin_row is not None
                else None
            ),
            "replays_from_this_capture": [
                self._replay_row_to_dict(r) for r in child_rows
            ],
            "drafts_from_this_capture": [dict(d) for d in draft_rows],
        }
