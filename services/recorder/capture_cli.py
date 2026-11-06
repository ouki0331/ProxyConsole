#!/usr/bin/env python3
"""Capture reader and semantic replay CLI for HTTP Capture Contract v0."""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
from pathlib import Path
import socket
import ssl
import sys
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit

from parser import build_derived_views

HOP_BY_HOP_AND_REBUILT_HEADERS = {
    "host",
    "content-length",
    "connection",
    "transfer-encoding",
    "proxy-connection",
    "keep-alive",
    "upgrade",
    "x-forwarded-for",
    "x-forwarded-proto",
    "x-forwarded-host",
}


def _load_record(storage_dir: Path, capture_id: str) -> dict[str, Any]:
    record_path = storage_dir / "captures" / f"{capture_id}.json"
    if not record_path.is_file():
        raise FileNotFoundError(f"Capture record not found: {record_path}")
    return json.loads(record_path.read_text(encoding="utf-8"))


def _load_blob(storage_dir: Path, record: dict[str, Any]) -> bytes:
    blob_ref = record.get("request", {}).get("body", {}).get("blob_ref")
    if not blob_ref:
        return b""
    blob_path = storage_dir / blob_ref
    if not blob_path.is_file():
        raise FileNotFoundError(f"Blob file not found: {blob_path}")
    return blob_path.read_bytes()


def cmd_list(args: argparse.Namespace) -> int:
    storage_dir = Path(args.storage_dir)
    captures_dir = storage_dir / "captures"
    items: list[dict[str, Any]] = []
    if captures_dir.is_dir():
        for path in sorted(captures_dir.glob("*.json")):
            try:
                rec = json.loads(path.read_text(encoding="utf-8"))
                req = rec.get("request", {})
                body = req.get("body", {})
                items.append(
                    {
                        "capture_id": rec.get("capture_id"),
                        "captured_at": rec.get("captured_at"),
                        "method": req.get("method"),
                        "authority": req.get("authority"),
                        "raw_path": req.get("raw_path"),
                        "raw_query": req.get("raw_query"),
                        "body_size": body.get("size"),
                        "body_sha256": body.get("sha256"),
                        "capture_status": rec.get("capture_status"),
                        "replayable": rec.get("replayable"),
                        "is_websocket": rec.get("is_websocket"),
                        "response_status": rec.get("forwarding", {}).get(
                            "response_status"
                        ),
                    }
                )
            except Exception as exc:
                items.append({"file": path.name, "error": str(exc)})
    print(json.dumps(items, ensure_ascii=False, indent=2))
    return 0


def cmd_get(args: argparse.Namespace) -> int:
    storage_dir = Path(args.storage_dir)
    record = _load_record(storage_dir, args.capture_id)
    req = record["request"]
    body_meta = req["body"]
    blob_bytes = _load_blob(storage_dir, record)

    blob_sha256 = hashlib.sha256(blob_bytes).hexdigest()
    captured_size_matches = len(blob_bytes) == body_meta.get("captured_size", 0)
    if record.get("capture_status") == "complete":
        sha256_matches = blob_sha256 == body_meta.get("sha256")
    else:
        sha256_matches = False

    regenerated_views = build_derived_views(
        raw_query=req.get("raw_query", ""),
        headers=req.get("headers", []),
        raw_body=blob_bytes if record.get("capture_status") == "complete" else None,
        content_type=body_meta.get("content_type"),
        capture_status=record.get("capture_status", "complete"),
    )

    out = dict(record)
    out["reader_verification"] = {
        "blob_bytes_len": len(blob_bytes),
        "blob_sha256": blob_sha256,
        "captured_size_matches": captured_size_matches,
        "full_body_sha256_matches": sha256_matches,
    }
    out["regenerated_derived_views"] = regenerated_views
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


def _mutate_pairs(
    pairs: list[tuple[str, str]], updates: list[str]
) -> list[tuple[str, str]]:
    result = list(pairs)
    for item in updates:
        if "=" not in item:
            raise ValueError(f"Mutation must be KEY=VALUE, got: {item}")
        target_k, target_v = item.split("=", 1)
        replaced = False
        new_list: list[tuple[str, str]] = []
        for k, v in result:
            if k == target_k and not replaced:
                new_list.append((k, target_v))
                replaced = True
            else:
                new_list.append((k, v))
        if not replaced:
            new_list.append((target_k, target_v))
        result = new_list
    return result


def cmd_replay(args: argparse.Namespace) -> int:
    storage_dir = Path(args.storage_dir)
    record = _load_record(storage_dir, args.capture_id)

    capture_status = record.get("capture_status")
    replayable = bool(record.get("replayable"))
    if not replayable or capture_status != "complete" or record.get("is_websocket"):
        err_payload = {
            "error": "non_replayable_capture",
            "capture_id": record.get("capture_id"),
            "capture_status": capture_status,
            "replayable": replayable,
            "is_websocket": record.get("is_websocket", False),
            "message": "Truncated, metadata-only, failed, or WebSocket records cannot be replayed.",
        }
        print(json.dumps(err_payload, ensure_ascii=False), file=sys.stderr)
        return 2

    req = record["request"]
    raw_body = _load_blob(storage_dir, record)
    if len(raw_body) != req["body"]["size"]:
        err_payload = {
            "error": "incomplete_blob",
            "capture_id": record.get("capture_id"),
            "expected_size": req["body"]["size"],
            "actual_size": len(raw_body),
        }
        print(json.dumps(err_payload, ensure_ascii=False), file=sys.stderr)
        return 2

    raw_path = req["raw_path"]
    raw_query = req.get("raw_query", "")

    if args.set_query:
        query_pairs = parse_qsl(raw_query, keep_blank_values=True)
        mutated_query = _mutate_pairs(query_pairs, args.set_query)
        raw_query = urlencode(mutated_query, doseq=True)

    if args.set_form:
        form_text = raw_body.decode("utf-8")
        form_pairs = parse_qsl(form_text, keep_blank_values=True)
        mutated_form = _mutate_pairs(form_pairs, args.set_form)
        raw_body = urlencode(mutated_form, doseq=True).encode("utf-8")

    target_uri = f"{raw_path}?{raw_query}" if raw_query else raw_path
    authority = args.override_host or req["authority"]
    sni_host = authority.split(":", 1)[0]

    rebuilt_headers: list[tuple[str, str]] = [("Host", authority)]
    for k, v in req.get("headers", []):
        if k.lower() in HOP_BY_HOP_AND_REBUILT_HEADERS:
            continue
        rebuilt_headers.append((k, v))
    rebuilt_headers.append(("Content-Length", str(len(raw_body))))
    rebuilt_headers.append(("Connection", "close"))

    parsed_edge = urlsplit(args.edge_url)
    edge_scheme = parsed_edge.scheme or "https"
    edge_host = parsed_edge.hostname or "127.0.0.1"
    edge_port = parsed_edge.port or (443 if edge_scheme == "https" else 80)

    raw_sock = socket.create_connection((edge_host, edge_port), timeout=30)
    if edge_scheme == "https":
        if args.cacert:
            ssl_ctx = ssl.create_default_context(cafile=args.cacert)
        else:
            ssl_ctx = ssl._create_unverified_context()
        conn_sock = ssl_ctx.wrap_socket(raw_sock, server_hostname=sni_host)
    else:
        conn_sock = raw_sock

    http_conn = http.client.HTTPConnection(edge_host, edge_port, timeout=30)
    http_conn.sock = conn_sock
    http_conn.putrequest(
        req["method"],
        target_uri,
        skip_host=True,
        skip_accept_encoding=True,
    )
    for k, v in rebuilt_headers:
        http_conn.putheader(k, v)
    http_conn.endheaders(message_body=raw_body if raw_body else None)

    resp = http_conn.getresponse()
    resp_bytes = resp.read()
    resp_text = resp_bytes.decode("utf-8", errors="replace")
    try:
        resp_json = json.loads(resp_text)
    except ValueError:
        resp_json = None

    result = {
        "replayed_from_capture_id": record["capture_id"],
        "replay_request": {
            "method": req["method"],
            "authority": authority,
            "target_uri": target_uri,
            "raw_path": raw_path,
            "raw_query": raw_query,
            "body_size": len(raw_body),
            "body_sha256": hashlib.sha256(raw_body).hexdigest(),
        },
        "response_status": resp.status,
        "response_headers": [[k, v] for k, v in resp.getheaders()],
        "response_body": resp_text,
        "response_json": resp_json,
    }
    http_conn.close()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="ProxyConsole capture reader & replay CLI")
    parser.add_argument(
        "--storage-dir",
        default=os.environ.get("CAPTURE_STORAGE_DIR", "/var/lib/proxyconsole"),
        help="Path to capture storage directory containing captures/ and blobs/",
    )
    sub = parser.add_subparsers(dest="subcommand", required=True)

    sub.add_parser("list", help="List saved capture records")

    p_get = sub.add_parser("get", help="Read and verify a single capture record")
    p_get.add_argument("capture_id", help="Capture ID to read")

    p_replay = sub.add_parser("replay", help="Semantically replay a complete capture record")
    p_replay.add_argument("capture_id", help="Capture ID to replay")
    p_replay.add_argument(
        "--edge-url",
        default=os.environ.get("REPLAY_EDGE_URL", "https://edge:443"),
        help="Target proxy URL to send the replay through",
    )
    p_replay.add_argument(
        "--cacert",
        default=os.environ.get("REPLAY_CACERT", ""),
        help="Optional CA certificate path for TLS verification",
    )
    p_replay.add_argument(
        "--override-host",
        default="",
        help="Optional override for Host authority",
    )
    p_replay.add_argument(
        "--set-query",
        action="append",
        default=[],
        help="Modify a single query parameter (KEY=VALUE)",
    )
    p_replay.add_argument(
        "--set-form",
        action="append",
        default=[],
        help="Modify a single URL-encoded form parameter (KEY=VALUE)",
    )

    args = parser.parse_args()
    if args.subcommand == "list":
        return cmd_list(args)
    if args.subcommand == "get":
        return cmd_get(args)
    if args.subcommand == "replay":
        return cmd_replay(args)
    return 1


if __name__ == "__main__":
    sys.exit(main())
