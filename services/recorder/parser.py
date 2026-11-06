#!/usr/bin/env python3
"""Derived view generator for HTTP Capture Contract v0.

All functions in this module treat derived parsing as non-fatal: any malformed
JSON, URL-encoded form, or multipart payload appends a diagnostic message to
`parse_errors` without failing raw byte capture or proxy forwarding.
"""

from __future__ import annotations

from email.parser import BytesParser
from email.policy import HTTP
import hashlib
import json
from typing import Any
from urllib.parse import parse_qsl


def parse_cookies_ordered(headers: list[list[str]]) -> list[list[str]]:
    cookies: list[list[str]] = []
    for k, v in headers:
        if k.lower() == "cookie":
            for pair in v.split(";"):
                pair = pair.strip()
                if not pair:
                    continue
                if "=" in pair:
                    ck, cv = pair.split("=", 1)
                    cookies.append([ck.strip(), cv.strip()])
                else:
                    cookies.append([pair, ""])
    return cookies


def _extract_boundary(content_type: str) -> str | None:
    for segment in content_type.split(";")[1:]:
        segment = segment.strip()
        if segment.lower().startswith("boundary="):
            val = segment.split("=", 1)[1].strip()
            if len(val) >= 2 and val[0] == '"' and val[-1] == '"':
                val = val[1:-1]
            return val or None
    return None


def build_derived_views(
    raw_query: str,
    headers: list[list[str]],
    raw_body: bytes | None,
    content_type: str | None,
    capture_status: str,
) -> dict[str, Any]:
    parse_errors: list[str] = []

    try:
        query_params = [
            [k, v] for k, v in parse_qsl(raw_query, keep_blank_values=True)
        ]
    except Exception as exc:  # pragma: no cover
        query_params = []
        parse_errors.append(f"query_parse_error: {exc}")

    try:
        cookies_view = parse_cookies_ordered(headers)
    except Exception as exc:  # pragma: no cover
        cookies_view = []
        parse_errors.append(f"cookie_parse_error: {exc}")

    ct_full = (content_type or "").strip()
    ct_lower = ct_full.lower()

    json_view: Any = None
    form_view: list[list[str]] | None = None
    multipart_view: list[dict[str, Any]] | None = None
    text_preview: str | None = None
    hex_preview: str = ""
    content_kind = "empty"

    if raw_body:
        hex_preview = raw_body[:64].hex()

    if not raw_body:
        content_kind = "empty"
    elif capture_status != "complete":
        content_kind = "partial_or_omitted"
    elif "application/json" in ct_lower or ct_lower.endswith("+json"):
        content_kind = "json"
        try:
            decoded_json_str = raw_body.decode("utf-8")
            text_preview = decoded_json_str[:512]
            json_view = json.loads(decoded_json_str)
        except Exception as exc:
            parse_errors.append(f"json_parse_error: {exc}")
            text_preview = raw_body[:512].decode("utf-8", errors="replace")
    elif "application/x-www-form-urlencoded" in ct_lower:
        content_kind = "urlencoded_form"
        try:
            decoded_form = raw_body.decode("utf-8")
            text_preview = decoded_form[:512]
            form_view = [
                [k, v]
                for k, v in parse_qsl(
                    decoded_form, keep_blank_values=True, strict_parsing=True
                )
            ]
        except Exception as exc:
            parse_errors.append(f"form_parse_error: {exc}")
            form_view = [
                [k, v]
                for k, v in parse_qsl(
                    raw_body.decode("utf-8", errors="replace"),
                    keep_blank_values=True,
                )
            ]
    elif "multipart/form-data" in ct_lower:
        content_kind = "multipart_form"
        boundary = _extract_boundary(ct_full)
        if not boundary:
            parse_errors.append("multipart_parse_error: missing boundary parameter")
        elif f"--{boundary}".encode("ascii", errors="ignore") not in raw_body:
            parse_errors.append("multipart_parse_error: boundary delimiter not found in body")
        else:
            try:
                mime_raw = (
                    f"Content-Type: {ct_full}\r\nMIME-Version: 1.0\r\n\r\n".encode(
                        "latin-1"
                    )
                    + raw_body
                )
                msg = BytesParser(policy=HTTP).parsebytes(mime_raw)
                parts_out: list[dict[str, Any]] = []
                for part in msg.iter_parts():
                    part_bytes = part.get_payload(decode=True) or b""
                    part_name = part.get_param("name", header="content-disposition")
                    part_filename = part.get_filename()
                    part_ct = part.get_content_type()
                    part_entry: dict[str, Any] = {
                        "name": part_name,
                        "filename": part_filename,
                        "content_type": part_ct,
                        "size": len(part_bytes),
                        "sha256": hashlib.sha256(part_bytes).hexdigest(),
                    }
                    if part_filename is None:
                        try:
                            part_entry["value_text"] = part_bytes.decode("utf-8")
                        except UnicodeDecodeError:
                            part_entry["value_text"] = None
                    parts_out.append(part_entry)
                if not parts_out:
                    parse_errors.append("multipart_parse_error: no parts parsed")
                else:
                    multipart_view = parts_out
            except Exception as exc:
                parse_errors.append(f"multipart_parse_error: {exc}")
    else:
        try:
            decoded_txt = raw_body.decode("utf-8")
            if ct_lower.startswith("text/"):
                content_kind = "text"
                text_preview = decoded_txt[:512]
            else:
                content_kind = "binary"
        except UnicodeDecodeError:
            content_kind = "binary"

    return {
        "content_kind": content_kind,
        "query_params": query_params,
        "cookies_view": cookies_view,
        "json_view": json_view,
        "form_view": form_view,
        "multipart_view": multipart_view,
        "text_preview": text_preview,
        "hex_preview": hex_preview,
        "parse_errors": parse_errors,
    }
