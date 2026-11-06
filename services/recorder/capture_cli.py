#!/usr/bin/env python3
"""Capture reader, durable index query, and semantic replay CLI (DEV-002 / DEV-010)."""

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

from index_store import CaptureIndexStore

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


def cmd_search(args: argparse.Namespace) -> int:
    store = CaptureIndexStore(storage_dir=args.storage_dir, db_path=args.db_path)
    res = store.query_captures(
        limit=args.limit,
        offset=args.offset,
        sort=args.sort,
        from_time=args.from_time,
        to_time=args.to_time,
        host=args.host,
        authority=args.authority,
        method=args.method,
        path=args.path,
        path_prefix=args.path_prefix,
        capture_status=args.capture_status,
        response_status=args.response_status,
    )
    if getattr(args, "items_only", False):
        print(json.dumps(res["items"], ensure_ascii=False, indent=2))
    else:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0


def cmd_get(args: argparse.Namespace) -> int:
    store = CaptureIndexStore(storage_dir=args.storage_dir, db_path=args.db_path)
    out = store.get_capture_detail(args.capture_id)
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


def cmd_import(args: argparse.Namespace) -> int:
    store = CaptureIndexStore(storage_dir=args.storage_dir, db_path=args.db_path)
    res = store.import_all_captures()
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0


def cmd_rebuild(args: argparse.Namespace) -> int:
    store = CaptureIndexStore(storage_dir=args.storage_dir, db_path=args.db_path)
    res = store.rebuild_index()
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0


def cmd_index_status(args: argparse.Namespace) -> int:
    store = CaptureIndexStore(storage_dir=args.storage_dir, db_path=args.db_path)
    res = store.get_index_status()
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0


def cmd_retention_status(args: argparse.Namespace) -> int:
    store = CaptureIndexStore(storage_dir=args.storage_dir, db_path=args.db_path)
    res = store.get_retention_status(runs_limit=args.runs_limit)
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0


def cmd_retention_policy(args: argparse.Namespace) -> int:
    store = CaptureIndexStore(storage_dir=args.storage_dir, db_path=args.db_path)
    enabled_val: bool | None = None
    if args.periodic_enabled is not None:
        s = str(args.periodic_enabled).strip().lower()
        if s in ("1", "true", "yes", "on"):
            enabled_val = True
        elif s in ("0", "false", "no", "off"):
            enabled_val = False
        else:
            raise ValueError(f"Invalid --periodic-enabled value: {args.periodic_enabled}")

    if any(
        v is not None
        for v in (
            args.min_retention_seconds,
            args.soft_limit_bytes,
            args.hard_limit_bytes,
            args.periodic_interval_seconds,
            enabled_val,
        )
    ):
        res = store.update_retention_policy(
            min_retention_seconds=args.min_retention_seconds,
            soft_limit_bytes=args.soft_limit_bytes,
            hard_limit_bytes=args.hard_limit_bytes,
            periodic_interval_seconds=args.periodic_interval_seconds,
            periodic_enabled=enabled_val,
        )
    else:
        res = store.get_retention_policy()
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0


def cmd_retention_run(args: argparse.Namespace) -> int:
    store = CaptureIndexStore(storage_dir=args.storage_dir, db_path=args.db_path)
    trigger = args.trigger_source or ("dry_run" if args.dry_run else "manual")
    res = store.run_retention(
        dry_run=bool(args.dry_run),
        now=args.now,
        trigger_source=trigger,
    )
    print(json.dumps(res, ensure_ascii=False, indent=2))
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


def _add_search_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--offset", type=int, default=0)
    p.add_argument("--sort", choices=["desc", "asc"], default="desc")
    p.add_argument("--from-time", default=None)
    p.add_argument("--to-time", default=None)
    p.add_argument("--host", default=None)
    p.add_argument("--authority", default=None)
    p.add_argument("--method", default=None)
    p.add_argument("--path", default=None)
    p.add_argument("--path-prefix", default=None)
    p.add_argument("--capture-status", default=None)
    p.add_argument("--response-status", type=int, default=None)


def main() -> int:
    parser = argparse.ArgumentParser(description="ProxyConsole capture reader, index & replay CLI")
    parser.add_argument(
        "--storage-dir",
        default=os.environ.get("CAPTURE_STORAGE_DIR", "/var/lib/proxyconsole"),
        help="Path to capture storage directory containing captures/, blobs/, database/",
    )
    parser.add_argument(
        "--db-path",
        default=None,
        help="Optional override path for SQLite index file",
    )
    sub = parser.add_subparsers(dest="subcommand", required=True)

    p_list = sub.add_parser("list", help="Query capture records from durable index")
    _add_search_args(p_list)
    p_list.add_argument(
        "--items-only",
        action="store_true",
        help="Print only the items array",
    )

    p_search = sub.add_parser("search", help="Search capture records with pagination metadata")
    _add_search_args(p_search)

    p_get = sub.add_parser("get", help="Read and verify a single capture record detail")
    p_get.add_argument("capture_id", help="Capture ID to read")

    p_detail = sub.add_parser("detail", help="Alias for get: read capture record detail")
    p_detail.add_argument("capture_id", help="Capture ID to read")

    sub.add_parser("import", help="Idempotently import captures/*.json into SQLite index")
    sub.add_parser("rebuild", help="Delete and rebuild SQLite index from captures/*.json")
    sub.add_parser("index-status", help="Show durable index status and import errors")

    p_ret_status = sub.add_parser(
        "retention-status",
        help="Show retention policy, storage usage, and recent cleanup history",
    )
    p_ret_status.add_argument(
        "--runs-limit",
        type=int,
        default=20,
        help="Maximum number of recent runs to include",
    )

    p_ret_policy = sub.add_parser(
        "retention-policy",
        help="Get or update retention policy settings",
    )
    p_ret_policy.add_argument(
        "--min-retention-seconds",
        type=int,
        default=None,
        help="Minimum retention window in seconds (default 86400)",
    )
    p_ret_policy.add_argument(
        "--soft-limit-bytes",
        type=int,
        default=None,
        help="Soft storage target in bytes (default 20 GiB)",
    )
    p_ret_policy.add_argument(
        "--hard-limit-bytes",
        type=int,
        default=None,
        help="Hard storage limit in bytes (default 25 GiB)",
    )
    p_ret_policy.add_argument(
        "--periodic-interval-seconds",
        type=float,
        default=None,
        help="Periodic cleanup interval in seconds",
    )
    p_ret_policy.add_argument(
        "--periodic-enabled",
        default=None,
        help="Enable or disable periodic cleanup (true/false)",
    )

    p_ret_run = sub.add_parser(
        "retention-run",
        help="Execute retention cleanup or preview via --dry-run",
    )
    p_ret_run.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview deletion targets and freed bytes without modifying disk or index",
    )
    p_ret_run.add_argument(
        "--now",
        default=None,
        help="Optional ISO-8601 reference timestamp for deterministic age evaluation",
    )
    p_ret_run.add_argument(
        "--trigger-source",
        default=None,
        help="Optional trigger source label (manual/periodic/dry_run)",
    )

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
    if args.subcommand in ("list", "search"):
        return cmd_search(args)
    if args.subcommand in ("get", "detail"):
        return cmd_get(args)
    if args.subcommand == "import":
        return cmd_import(args)
    if args.subcommand == "rebuild":
        return cmd_rebuild(args)
    if args.subcommand == "index-status":
        return cmd_index_status(args)
    if args.subcommand == "retention-status":
        return cmd_retention_status(args)
    if args.subcommand == "retention-policy":
        return cmd_retention_policy(args)
    if args.subcommand == "retention-run":
        return cmd_retention_run(args)
    if args.subcommand == "replay":
        return cmd_replay(args)
    return 1


if __name__ == "__main__":
    sys.exit(main())
