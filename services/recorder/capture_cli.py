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

from docker_discovery import (
    DiscoveryTargetNotFoundError,
    DiscoveryTargetValidationError,
    DiscoveryUnavailableError,
)
from draft_replay import (
    DraftExpiredError,
    DraftNotFoundError,
    DraftValidationError,
    NonReplayableCaptureError,
)
from index_store import CaptureIndexStore
from route_manager import (
    DuplicateDomainError,
    RouteNotFoundError,
    RouteValidationError,
)
from rule_engine import (
    RuleNotFoundError,
    RuleValidationError,
)

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
    rule_mod = _parse_cli_bool(getattr(args, "rule_modified", None))
    rule_fail = _parse_cli_bool(getattr(args, "rule_failed", None))
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
        source_capture_id=getattr(args, "source_capture_id", None),
        draft_id=getattr(args, "draft_id", None),
        rule_id=getattr(args, "rule_id", None),
        applied_rule_id=getattr(args, "applied_rule_id", None),
        failed_rule_id=getattr(args, "failed_rule_id", None),
        skipped_rule_id=getattr(args, "skipped_rule_id", None),
        rule_modified=rule_mod,
        rule_failed=rule_fail,
        rule_outcome=getattr(args, "rule_outcome", None),
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


def _parse_cli_bool(val: str | None) -> bool | None:
    if val is None:
        return None
    s = str(val).strip().lower()
    if s in ("1", "true", "yes", "on"):
        return True
    if s in ("0", "false", "no", "off"):
        return False
    raise ValueError(f"Invalid boolean value: {val}")


def cmd_route_list(args: argparse.Namespace) -> int:
    store = CaptureIndexStore(storage_dir=args.storage_dir, db_path=args.db_path)
    en = _parse_cli_bool(args.enabled)
    res = store.routes.list_routes(enabled=en)
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0


def cmd_route_get(args: argparse.Namespace) -> int:
    store = CaptureIndexStore(storage_dir=args.storage_dir, db_path=args.db_path)
    try:
        res = store.routes.get_route(args.route_id)
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0
    except RouteNotFoundError as exc:
        print(
            json.dumps({"error": "route_not_found", "message": str(exc)}, ensure_ascii=False),
            file=sys.stderr,
        )
        return 2


def cmd_route_status(args: argparse.Namespace) -> int:
    store = CaptureIndexStore(storage_dir=args.storage_dir, db_path=args.db_path)
    res = store.routes.get_routes_status(runs_limit=args.runs_limit)
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0


def cmd_route_create(args: argparse.Namespace) -> int:
    store = CaptureIndexStore(storage_dir=args.storage_dir, db_path=args.db_path)
    en = _parse_cli_bool(args.enabled)
    try:
        res = store.routes.create_route(
            domain=args.domain,
            upstream_scheme=args.upstream_scheme,
            upstream_host=getattr(args, "upstream_host", None),
            upstream_port=getattr(args, "upstream_port", None),
            target_id=getattr(args, "target_id", None),
            container=getattr(args, "container", None),
            port=getattr(args, "port", None),
            allow_self_target=bool(getattr(args, "allow_self_target", False)),
            route_id=args.route_id,
            enabled=True if en is None else en,
            description=args.description or "",
            auto_apply=not args.no_apply,
        )
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0
    except DiscoveryTargetNotFoundError as exc:
        print(
            json.dumps({"error": "target_not_found", "message": str(exc)}, ensure_ascii=False),
            file=sys.stderr,
        )
        return 2
    except DiscoveryUnavailableError as exc:
        print(
            json.dumps(
                {
                    "error": "discovery_unavailable",
                    "message": str(exc),
                    "fallback_manual_upstream_supported": True,
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 2
    except DiscoveryTargetValidationError as exc:
        print(
            json.dumps({"error": "invalid_target", "message": str(exc)}, ensure_ascii=False),
            file=sys.stderr,
        )
        return 2
    except DuplicateDomainError as exc:
        print(
            json.dumps({"error": "duplicate_domain", "message": str(exc)}, ensure_ascii=False),
            file=sys.stderr,
        )
        return 2
    except (RouteValidationError, ValueError) as exc:
        print(
            json.dumps({"error": "invalid_route", "message": str(exc)}, ensure_ascii=False),
            file=sys.stderr,
        )
        return 2


def cmd_route_update(args: argparse.Namespace) -> int:
    store = CaptureIndexStore(storage_dir=args.storage_dir, db_path=args.db_path)
    en = _parse_cli_bool(args.enabled)
    try:
        res = store.routes.update_route(
            args.route_id,
            domain=args.domain,
            upstream_scheme=args.upstream_scheme,
            upstream_host=args.upstream_host,
            upstream_port=args.upstream_port,
            target_id=getattr(args, "target_id", None),
            container=getattr(args, "container", None),
            port=getattr(args, "port", None),
            allow_self_target=bool(getattr(args, "allow_self_target", False)),
            enabled=en,
            description=args.description,
            auto_apply=not args.no_apply,
        )
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0
    except RouteNotFoundError as exc:
        print(
            json.dumps({"error": "route_not_found", "message": str(exc)}, ensure_ascii=False),
            file=sys.stderr,
        )
        return 2
    except DiscoveryTargetNotFoundError as exc:
        print(
            json.dumps({"error": "target_not_found", "message": str(exc)}, ensure_ascii=False),
            file=sys.stderr,
        )
        return 2
    except DiscoveryUnavailableError as exc:
        print(
            json.dumps(
                {
                    "error": "discovery_unavailable",
                    "message": str(exc),
                    "fallback_manual_upstream_supported": True,
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 2
    except DiscoveryTargetValidationError as exc:
        print(
            json.dumps({"error": "invalid_target", "message": str(exc)}, ensure_ascii=False),
            file=sys.stderr,
        )
        return 2
    except DuplicateDomainError as exc:
        print(
            json.dumps({"error": "duplicate_domain", "message": str(exc)}, ensure_ascii=False),
            file=sys.stderr,
        )
        return 2
    except (RouteValidationError, ValueError) as exc:
        print(
            json.dumps({"error": "invalid_route", "message": str(exc)}, ensure_ascii=False),
            file=sys.stderr,
        )
        return 2


def cmd_route_copy(args: argparse.Namespace) -> int:
    store = CaptureIndexStore(storage_dir=args.storage_dir, db_path=args.db_path)
    en = _parse_cli_bool(args.enabled)
    try:
        res = store.routes.copy_route(
            args.source_route_id,
            domain=args.domain,
            route_id=args.route_id,
            enabled=en,
            description=args.description,
            auto_apply=not args.no_apply,
        )
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0
    except RouteNotFoundError as exc:
        print(
            json.dumps({"error": "route_not_found", "message": str(exc)}, ensure_ascii=False),
            file=sys.stderr,
        )
        return 2
    except DuplicateDomainError as exc:
        print(
            json.dumps({"error": "duplicate_domain", "message": str(exc)}, ensure_ascii=False),
            file=sys.stderr,
        )
        return 2
    except (RouteValidationError, ValueError) as exc:
        print(
            json.dumps({"error": "invalid_route", "message": str(exc)}, ensure_ascii=False),
            file=sys.stderr,
        )
        return 2


def cmd_route_enable_disable(args: argparse.Namespace, enabled: bool) -> int:
    store = CaptureIndexStore(storage_dir=args.storage_dir, db_path=args.db_path)
    try:
        res = store.routes.set_route_enabled(
            args.route_id, enabled, auto_apply=not args.no_apply
        )
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0
    except RouteNotFoundError as exc:
        print(
            json.dumps({"error": "route_not_found", "message": str(exc)}, ensure_ascii=False),
            file=sys.stderr,
        )
        return 2


def cmd_route_delete(args: argparse.Namespace) -> int:
    store = CaptureIndexStore(storage_dir=args.storage_dir, db_path=args.db_path)
    try:
        res = store.routes.delete_route(
            args.route_id, auto_apply=not args.no_apply
        )
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0
    except RouteNotFoundError as exc:
        print(
            json.dumps({"error": "route_not_found", "message": str(exc)}, ensure_ascii=False),
            file=sys.stderr,
        )
        return 2


def cmd_route_apply(args: argparse.Namespace) -> int:
    store = CaptureIndexStore(storage_dir=args.storage_dir, db_path=args.db_path)
    res = store.routes.apply_routes(
        trigger_action=args.trigger_action or "cli_apply",
        raw_candidate_config=args.raw_candidate_config,
        inject_invalid_directive=args.inject_invalid_directive,
        simulate_reload_failure=bool(args.simulate_reload_failure),
    )
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0 if res["applied"] else 2


def cmd_discovery_status(args: argparse.Namespace) -> int:
    store = CaptureIndexStore(storage_dir=args.storage_dir, db_path=args.db_path)
    res = store.discovery.get_status(refresh=not args.no_refresh)
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0


def cmd_discovery_containers(args: argparse.Namespace) -> int:
    store = CaptureIndexStore(storage_dir=args.storage_dir, db_path=args.db_path)
    res = store.discovery.list_containers(
        include_self=bool(args.include_self),
        state=args.state,
        scope=args.scope,
    )
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0


def cmd_discovery_targets(args: argparse.Namespace) -> int:
    store = CaptureIndexStore(storage_dir=args.storage_dir, db_path=args.db_path)
    res = store.discovery.list_targets(
        include_self=bool(args.include_self),
        include_stopped=bool(args.include_stopped),
        scope=args.scope,
    )
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0


def cmd_discovery_refresh(args: argparse.Namespace) -> int:
    store = CaptureIndexStore(storage_dir=args.storage_dir, db_path=args.db_path)
    res = store.discovery.refresh(
        include_self=bool(args.include_self),
        include_stopped=bool(args.include_stopped),
        scope=args.scope,
    )
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0


def cmd_discovery_config(args: argparse.Namespace) -> int:
    store = CaptureIndexStore(storage_dir=args.storage_dir, db_path=args.db_path)
    override = None if args.reset else args.socket_path_override
    res = store.discovery.set_socket_path_override(override)
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
    p.add_argument("--source-capture-id", default=None)
    p.add_argument("--draft-id", default=None)
    p.add_argument("--rule-id", default=None)
    p.add_argument("--applied-rule-id", default=None)
    p.add_argument("--failed-rule-id", default=None)
    p.add_argument("--skipped-rule-id", default=None)
    p.add_argument("--rule-modified", default=None)
    p.add_argument("--rule-failed", default=None)
    p.add_argument("--rule-outcome", default=None)


def _parse_kv_mutation(item: str, op: str) -> dict[str, Any]:
    """Parse KEY=VALUE, INDEX:KEY=VALUE, or KEY@OCC=VALUE for pair mutations."""
    if op in ("add", "append"):
        if "=" not in item:
            raise ValueError(f"Expected KEY=VALUE for --*-add, got: {item}")
        k, v = item.split("=", 1)
        return {"op": "add", "name": k, "value": v}
    if op == "set":
        if "=" not in item:
            raise ValueError(f"Expected KEY=VALUE for --*-set, got: {item}")
        lhs, v = item.split("=", 1)
        if lhs.startswith("#"):
            idx = int(lhs[1:])
            return {"op": "set_at", "index": idx, "value": v}
        if "@" in lhs:
            k, occ_str = lhs.rsplit("@", 1)
            return {
                "op": "set",
                "name": k,
                "occurrence": int(occ_str),
                "value": v,
            }
        return {"op": "set", "name": lhs, "value": v}
    if op == "remove":
        if item.startswith("#"):
            return {"op": "remove_at", "index": int(item[1:])}
        if "@" in item:
            k, occ_str = item.rsplit("@", 1)
            return {"op": "remove", "name": k, "occurrence": int(occ_str)}
        return {"op": "remove", "name": item}
    raise ValueError(f"Unknown op: {op}")


def cmd_replayability(args: argparse.Namespace) -> int:
    store = CaptureIndexStore(storage_dir=args.storage_dir, db_path=args.db_path)
    try:
        res = store.drafts.evaluate_replayability(
            args.capture_id, raise_on_false=False
        )
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0 if res.get("replayable") else 2
    except FileNotFoundError as exc:
        print(
            json.dumps(
                {"error": "capture_not_found", "message": str(exc)},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 2


def cmd_draft_create(args: argparse.Namespace) -> int:
    store = CaptureIndexStore(storage_dir=args.storage_dir, db_path=args.db_path)
    edits = json.loads(args.edits_json) if args.edits_json else None
    try:
        res = store.drafts.create_draft(
            args.capture_id,
            draft_id=args.draft_id,
            ttl_seconds=args.ttl_seconds,
            expires_at=args.expires_at,
            edits=edits,
        )
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0
    except FileNotFoundError as exc:
        print(
            json.dumps(
                {"error": "capture_not_found", "message": str(exc)},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 2
    except NonReplayableCaptureError as exc:
        print(json.dumps(exc.to_dict(), ensure_ascii=False), file=sys.stderr)
        return 2
    except DraftValidationError as exc:
        print(
            json.dumps(
                {"error": exc.code, "message": str(exc), **exc.details},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 2


def cmd_draft_list(args: argparse.Namespace) -> int:
    store = CaptureIndexStore(storage_dir=args.storage_dir, db_path=args.db_path)
    res = store.drafts.list_drafts(
        source_capture_id=args.source_capture_id, limit=args.limit
    )
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0


def cmd_draft_get(args: argparse.Namespace) -> int:
    store = CaptureIndexStore(storage_dir=args.storage_dir, db_path=args.db_path)
    try:
        res = store.drafts.get_draft(args.draft_id, now=args.now)
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0
    except DraftExpiredError as exc:
        print(
            json.dumps(
                {
                    "error": "draft_expired",
                    "draft_id": exc.draft_id,
                    "expires_at": exc.expires_at,
                    "message": str(exc),
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 2
    except DraftNotFoundError as exc:
        print(
            json.dumps(
                {"error": "draft_not_found", "message": str(exc)},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 2


def cmd_draft_update(args: argparse.Namespace) -> int:
    store = CaptureIndexStore(storage_dir=args.storage_dir, db_path=args.db_path)
    updates: dict[str, Any] = (
        json.loads(args.updates_json) if args.updates_json else {}
    )
    for field in (
        "method",
        "scheme",
        "authority",
        "host",
        "raw_path",
        "decoded_path",
        "path",
        "raw_query",
        "content_type",
        "json_text",
        "raw_text",
        "raw_base64",
        "ttl_seconds",
        "expires_at",
    ):
        val = getattr(args, field, None)
        if val is not None:
            updates[field] = val

    q_muts = list(updates.get("query_mutations") or [])
    for item in args.query_add:
        q_muts.append(_parse_kv_mutation(item, "add"))
    for item in args.query_set:
        q_muts.append(_parse_kv_mutation(item, "set"))
    for item in args.query_remove:
        q_muts.append(_parse_kv_mutation(item, "remove"))
    if q_muts:
        updates["query_mutations"] = q_muts

    h_muts = list(updates.get("header_mutations") or [])
    for item in args.header_add:
        h_muts.append(_parse_kv_mutation(item, "add"))
    for item in args.header_set:
        h_muts.append(_parse_kv_mutation(item, "set"))
    for item in args.header_remove:
        h_muts.append(_parse_kv_mutation(item, "remove"))
    if h_muts:
        updates["header_mutations"] = h_muts

    c_muts = list(updates.get("cookie_mutations") or [])
    for item in args.cookie_add:
        c_muts.append(_parse_kv_mutation(item, "add"))
    for item in args.cookie_set:
        c_muts.append(_parse_kv_mutation(item, "set"))
    for item in args.cookie_remove:
        c_muts.append(_parse_kv_mutation(item, "remove"))
    if c_muts:
        updates["cookie_mutations"] = c_muts

    j_muts = list(updates.get("json_mutations") or [])
    for item in args.json_set:
        if "=" not in item:
            raise ValueError(f"Expected PATH=JSON_VALUE for --json-set, got: {item}")
        p_str, v_str = item.split("=", 1)
        try:
            parsed_v = json.loads(v_str)
        except Exception:
            parsed_v = v_str
        j_muts.append({"op": "set", "path": p_str, "value": parsed_v})
    for p_str in args.json_delete:
        j_muts.append({"op": "delete", "path": p_str})
    if j_muts:
        updates["json_mutations"] = j_muts

    f_muts = list(updates.get("form_mutations") or [])
    for item in args.form_add:
        f_muts.append(_parse_kv_mutation(item, "add"))
    for item in args.form_set:
        f_muts.append(_parse_kv_mutation(item, "set"))
    for item in args.form_remove:
        f_muts.append(_parse_kv_mutation(item, "remove"))
    if f_muts:
        updates["form_mutations"] = f_muts

    try:
        res = store.drafts.update_draft(args.draft_id, updates)
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0
    except DraftExpiredError as exc:
        print(
            json.dumps(
                {
                    "error": "draft_expired",
                    "draft_id": exc.draft_id,
                    "expires_at": exc.expires_at,
                    "message": str(exc),
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 2
    except DraftNotFoundError as exc:
        print(
            json.dumps(
                {"error": "draft_not_found", "message": str(exc)},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 2
    except DraftValidationError as exc:
        print(
            json.dumps(
                {"error": exc.code, "message": str(exc), **exc.details},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 2


def cmd_draft_delete(args: argparse.Namespace) -> int:
    store = CaptureIndexStore(storage_dir=args.storage_dir, db_path=args.db_path)
    try:
        res = store.drafts.delete_draft(args.draft_id)
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0
    except DraftNotFoundError as exc:
        print(
            json.dumps(
                {"error": "draft_not_found", "message": str(exc)},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 2


def cmd_draft_export(args: argparse.Namespace) -> int:
    store = CaptureIndexStore(storage_dir=args.storage_dir, db_path=args.db_path)
    try:
        res = store.drafts.export_draft(
            args.draft_id,
            format_type=args.format,
            edge_url=args.edge_url,
            cacert=args.cacert,
        )
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0
    except (DraftNotFoundError, DraftExpiredError) as exc:
        print(
            json.dumps(
                {"error": "draft_not_found", "message": str(exc)},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 2


def cmd_draft_replay(args: argparse.Namespace) -> int:
    store = CaptureIndexStore(storage_dir=args.storage_dir, db_path=args.db_path)
    try:
        res = store.drafts.replay_draft(
            args.draft_id,
            edge_url=args.edge_url,
            cacert=args.cacert or None,
            override_host=args.override_host or None,
        )
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0
    except NonReplayableCaptureError as exc:
        print(json.dumps(exc.to_dict(), ensure_ascii=False), file=sys.stderr)
        return 2
    except (DraftNotFoundError, DraftExpiredError) as exc:
        print(
            json.dumps(
                {"error": "draft_not_found", "message": str(exc)},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 2


def _parse_rule_pair_matcher(item: str) -> dict[str, Any]:
    """Parse KEY or KEY=VALUE or KEY~REGEX or KEY!= (absent) for pair matchers."""
    if item.startswith("!"):
        return {"name": item[1:], "present": False}
    if "~" in item and ("=" not in item or item.index("~") < item.index("=")):
        k, pat = item.split("~", 1)
        return {"name": k, "value_regex": pat}
    if "=" in item:
        k, v = item.split("=", 1)
        return {"name": k, "value": v}
    return {"name": item, "present": True}


def _parse_rule_pair_action(target: str, op: str, item: str) -> dict[str, Any]:
    act = _parse_kv_mutation(item, op)
    act["target"] = target
    return act


def _build_rule_match_from_args(args: argparse.Namespace) -> dict[str, Any] | None:
    has_any = False
    match_obj: dict[str, Any] = {}
    if getattr(args, "match_json", None):
        match_obj = json.loads(args.match_json)
        has_any = True
    if getattr(args, "route_id", None):
        match_obj["route_id"] = list(args.route_id)
        has_any = True
    if getattr(args, "domain", None):
        match_obj["domain"] = list(args.domain)
        has_any = True
    if getattr(args, "method", None):
        match_obj["method"] = list(args.method)
        has_any = True
    if getattr(args, "path_exact", None) is not None:
        match_obj["path_exact"] = args.path_exact
        has_any = True
    if getattr(args, "path_prefix", None) is not None:
        match_obj["path_prefix"] = args.path_prefix
        has_any = True
    if getattr(args, "path_regex", None) is not None:
        match_obj["path_regex"] = args.path_regex
        has_any = True
    if getattr(args, "content_type", None):
        match_obj["content_type"] = list(args.content_type)
        has_any = True
    if getattr(args, "query_match", None):
        q_list = list(match_obj.get("query_pairs") or [])
        for it in args.query_match:
            q_list.append(_parse_rule_pair_matcher(it))
        match_obj["query_pairs"] = q_list
        has_any = True
    if getattr(args, "header_match", None):
        h_list = list(match_obj.get("header_pairs") or [])
        for it in args.header_match:
            h_list.append(_parse_rule_pair_matcher(it))
        match_obj["header_pairs"] = h_list
        has_any = True
    return match_obj if has_any else None


def _build_rule_actions_from_args(args: argparse.Namespace) -> list[dict[str, Any]] | None:
    actions: list[dict[str, Any]] = []
    has_any = False
    if getattr(args, "actions_json", None):
        parsed = json.loads(args.actions_json)
        if not isinstance(parsed, list):
            raise RuleValidationError(
                "--actions-json must be a JSON array", code="invalid_actions"
            )
        actions.extend(parsed)
        has_any = True

    for item in getattr(args, "query_add", None) or []:
        actions.append(_parse_rule_pair_action("query", "add", item))
        has_any = True
    for item in getattr(args, "query_set", None) or []:
        actions.append(_parse_rule_pair_action("query", "set", item))
        has_any = True
    for item in getattr(args, "query_remove", None) or []:
        actions.append(_parse_rule_pair_action("query", "remove", item))
        has_any = True

    for item in getattr(args, "header_add", None) or []:
        actions.append(_parse_rule_pair_action("header", "add", item))
        has_any = True
    for item in getattr(args, "header_set", None) or []:
        actions.append(_parse_rule_pair_action("header", "set", item))
        has_any = True
    for item in getattr(args, "header_remove", None) or []:
        actions.append(_parse_rule_pair_action("header", "remove", item))
        has_any = True

    for item in getattr(args, "cookie_add", None) or []:
        actions.append(_parse_rule_pair_action("cookie", "add", item))
        has_any = True
    for item in getattr(args, "cookie_set", None) or []:
        actions.append(_parse_rule_pair_action("cookie", "set", item))
        has_any = True
    for item in getattr(args, "cookie_remove", None) or []:
        actions.append(_parse_rule_pair_action("cookie", "remove", item))
        has_any = True

    for item in getattr(args, "json_set", None) or []:
        if "=" not in item:
            raise RuleValidationError(
                f"Expected PATH=JSON_VALUE for --json-set, got: {item}",
                code="invalid_action",
            )
        p_str, v_str = item.split("=", 1)
        try:
            parsed_v = json.loads(v_str)
        except Exception:
            parsed_v = v_str
        actions.append({"target": "json", "op": "set", "path": p_str, "value": parsed_v})
        has_any = True
    for p_str in getattr(args, "json_remove", None) or []:
        actions.append({"target": "json", "op": "remove", "path": p_str})
        has_any = True

    for item in getattr(args, "form_add", None) or []:
        actions.append(_parse_rule_pair_action("form", "add", item))
        has_any = True
    for item in getattr(args, "form_set", None) or []:
        actions.append(_parse_rule_pair_action("form", "set", item))
        has_any = True
    for item in getattr(args, "form_remove", None) or []:
        actions.append(_parse_rule_pair_action("form", "remove", item))
        has_any = True

    return actions if has_any else None


def cmd_rule_list(args: argparse.Namespace) -> int:
    store = CaptureIndexStore(storage_dir=args.storage_dir, db_path=args.db_path)
    en = _parse_cli_bool(args.enabled)
    res = store.rules.list_rules(enabled=en)
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0


def cmd_rule_get(args: argparse.Namespace) -> int:
    store = CaptureIndexStore(storage_dir=args.storage_dir, db_path=args.db_path)
    try:
        res = store.rules.get_rule(args.rule_id)
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0
    except RuleNotFoundError as exc:
        print(
            json.dumps(
                {"error": "rule_not_found", "rule_id": exc.rule_id, "message": str(exc)},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 2


def cmd_rule_create(args: argparse.Namespace) -> int:
    store = CaptureIndexStore(storage_dir=args.storage_dir, db_path=args.db_path)
    en = _parse_cli_bool(args.enabled)
    try:
        match_obj = _build_rule_match_from_args(args) or {}
        actions_list = _build_rule_actions_from_args(args) or []
        res = store.rules.create_rule(
            name=args.name,
            match=match_obj,
            actions=actions_list,
            rule_id=args.rule_id,
            description=args.description or "",
            enabled=True if en is None else en,
            priority=args.priority,
        )
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0
    except (RuleValidationError, ValueError) as exc:
        payload = (
            exc.to_dict()
            if isinstance(exc, RuleValidationError)
            else {"error": "invalid_rule", "message": str(exc)}
        )
        print(json.dumps(payload, ensure_ascii=False), file=sys.stderr)
        return 2


def cmd_rule_update(args: argparse.Namespace) -> int:
    store = CaptureIndexStore(storage_dir=args.storage_dir, db_path=args.db_path)
    en = _parse_cli_bool(args.enabled)
    try:
        match_obj = _build_rule_match_from_args(args)
        actions_list = _build_rule_actions_from_args(args)
        res = store.rules.update_rule(
            args.rule_id,
            name=args.name,
            description=args.description,
            enabled=en,
            priority=args.priority,
            match=match_obj,
            actions=actions_list,
        )
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0
    except RuleNotFoundError as exc:
        print(
            json.dumps(
                {"error": "rule_not_found", "rule_id": exc.rule_id, "message": str(exc)},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 2
    except (RuleValidationError, ValueError) as exc:
        payload = (
            exc.to_dict()
            if isinstance(exc, RuleValidationError)
            else {"error": "invalid_rule", "message": str(exc)}
        )
        print(json.dumps(payload, ensure_ascii=False), file=sys.stderr)
        return 2


def cmd_rule_copy(args: argparse.Namespace) -> int:
    store = CaptureIndexStore(storage_dir=args.storage_dir, db_path=args.db_path)
    en = _parse_cli_bool(args.enabled)
    try:
        match_obj = _build_rule_match_from_args(args)
        actions_list = _build_rule_actions_from_args(args)
        res = store.rules.copy_rule(
            args.source_rule_id,
            rule_id=args.rule_id,
            name=args.name,
            description=args.description,
            enabled=en,
            priority=args.priority,
            match=match_obj,
            actions=actions_list,
        )
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0
    except RuleNotFoundError as exc:
        print(
            json.dumps(
                {"error": "rule_not_found", "rule_id": exc.rule_id, "message": str(exc)},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 2
    except (RuleValidationError, ValueError) as exc:
        payload = (
            exc.to_dict()
            if isinstance(exc, RuleValidationError)
            else {"error": "invalid_rule", "message": str(exc)}
        )
        print(json.dumps(payload, ensure_ascii=False), file=sys.stderr)
        return 2


def cmd_rule_enable_disable(args: argparse.Namespace, enabled: bool) -> int:
    store = CaptureIndexStore(storage_dir=args.storage_dir, db_path=args.db_path)
    try:
        res = store.rules.set_rule_enabled(args.rule_id, enabled)
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0
    except RuleNotFoundError as exc:
        print(
            json.dumps(
                {"error": "rule_not_found", "rule_id": exc.rule_id, "message": str(exc)},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 2


def cmd_rule_delete(args: argparse.Namespace) -> int:
    store = CaptureIndexStore(storage_dir=args.storage_dir, db_path=args.db_path)
    try:
        res = store.rules.delete_rule(args.rule_id)
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0
    except RuleNotFoundError as exc:
        print(
            json.dumps(
                {"error": "rule_not_found", "rule_id": exc.rule_id, "message": str(exc)},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 2


def cmd_rule_preview(args: argparse.Namespace) -> int:
    store = CaptureIndexStore(storage_dir=args.storage_dir, db_path=args.db_path)
    try:
        req_spec: dict[str, Any] | None = None
        if args.request_json:
            req_spec = json.loads(args.request_json)
        elif not args.capture_id:
            req_spec = {}
            if args.method:
                req_spec["method"] = args.method
            if args.authority:
                req_spec["authority"] = args.authority
            if args.host:
                req_spec["host"] = args.host
            if args.raw_path:
                req_spec["raw_path"] = args.raw_path
            if args.raw_query is not None:
                req_spec["raw_query"] = args.raw_query
            if args.content_type:
                req_spec["content_type"] = args.content_type
            if args.headers_json:
                req_spec["headers"] = json.loads(args.headers_json)
            if args.body_text is not None:
                req_spec["body_text"] = args.body_text
            if args.body_base64 is not None:
                req_spec["body_base64"] = args.body_base64

        cand_rule = (
            json.loads(args.candidate_rule_json)
            if args.candidate_rule_json
            else None
        )
        res = store.rules.preview_rules(
            capture_id=args.capture_id,
            request_spec=req_spec,
            rule_ids=args.rule_id if args.rule_id else None,
            include_disabled=bool(args.include_disabled),
            candidate_rule=cand_rule,
        )
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0
    except FileNotFoundError as exc:
        print(
            json.dumps(
                {"error": "capture_not_found", "message": str(exc)},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 2
    except RuleNotFoundError as exc:
        print(
            json.dumps(
                {"error": "rule_not_found", "rule_id": exc.rule_id, "message": str(exc)},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 2
    except (RuleValidationError, ValueError) as exc:
        payload = (
            exc.to_dict()
            if isinstance(exc, RuleValidationError)
            else {"error": "invalid_preview", "message": str(exc)}
        )
        print(json.dumps(payload, ensure_ascii=False), file=sys.stderr)
        return 2


def _add_rule_match_and_action_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--match-json", default=None)
    p.add_argument("--actions-json", default=None)
    p.add_argument("--route-id", action="append", default=[])
    p.add_argument("--domain", action="append", default=[])
    p.add_argument("--method", action="append", default=[])
    p.add_argument("--path-exact", default=None)
    p.add_argument("--path-prefix", default=None)
    p.add_argument("--path-regex", default=None)
    p.add_argument("--content-type", action="append", default=[])
    p.add_argument("--query-match", action="append", default=[])
    p.add_argument("--header-match", action="append", default=[])
    p.add_argument("--query-add", action="append", default=[])
    p.add_argument("--query-set", action="append", default=[])
    p.add_argument("--query-remove", action="append", default=[])
    p.add_argument("--header-add", action="append", default=[])
    p.add_argument("--header-set", action="append", default=[])
    p.add_argument("--header-remove", action="append", default=[])
    p.add_argument("--cookie-add", action="append", default=[])
    p.add_argument("--cookie-set", action="append", default=[])
    p.add_argument("--cookie-remove", action="append", default=[])
    p.add_argument("--json-set", action="append", default=[])
    p.add_argument("--json-remove", action="append", default=[])
    p.add_argument("--form-add", action="append", default=[])
    p.add_argument("--form-set", action="append", default=[])
    p.add_argument("--form-remove", action="append", default=[])


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

    p_r_list = sub.add_parser("route-list", help="List persisted domain routes")
    p_r_list.add_argument(
        "--enabled",
        default=None,
        help="Optional filter by enabled status (true/false)",
    )

    p_r_get = sub.add_parser("route-get", help="Get a single route by ID")
    p_r_get.add_argument("route_id", help="Route ID")

    p_r_status = sub.add_parser(
        "route-status",
        help="Show route apply status, active Nginx config, and apply history",
    )
    p_r_status.add_argument("--runs-limit", type=int, default=20)

    p_r_create = sub.add_parser("route-create", help="Create a new domain route")
    p_r_create.add_argument("--domain", required=True)
    p_r_create.add_argument("--upstream-scheme", default="http")
    p_r_create.add_argument("--upstream-host", default=None)
    p_r_create.add_argument("--upstream-port", type=int, default=None)
    p_r_create.add_argument("--target-id", default=None)
    p_r_create.add_argument("--container", default=None)
    p_r_create.add_argument("--port", type=int, default=None)
    p_r_create.add_argument("--allow-self-target", action="store_true")
    p_r_create.add_argument("--route-id", default=None)
    p_r_create.add_argument("--enabled", default="true")
    p_r_create.add_argument("--description", default="")
    p_r_create.add_argument("--no-apply", action="store_true")

    p_r_target = sub.add_parser(
        "route-create-from-target",
        help="Create a new domain route from a discovered Docker container target",
    )
    p_r_target.add_argument("--domain", required=True)
    p_r_target.add_argument("--target-id", default=None)
    p_r_target.add_argument("--container", default=None)
    p_r_target.add_argument("--port", type=int, default=None)
    p_r_target.add_argument("--upstream-scheme", default="http")
    p_r_target.add_argument("--allow-self-target", action="store_true")
    p_r_target.add_argument("--route-id", default=None)
    p_r_target.add_argument("--enabled", default="true")
    p_r_target.add_argument("--description", default="")
    p_r_target.add_argument("--no-apply", action="store_true")

    p_r_update = sub.add_parser("route-update", help="Update an existing route")
    p_r_update.add_argument("route_id", help="Route ID")
    p_r_update.add_argument("--domain", default=None)
    p_r_update.add_argument("--upstream-scheme", default=None)
    p_r_update.add_argument("--upstream-host", default=None)
    p_r_update.add_argument("--upstream-port", type=int, default=None)
    p_r_update.add_argument("--target-id", default=None)
    p_r_update.add_argument("--container", default=None)
    p_r_update.add_argument("--port", type=int, default=None)
    p_r_update.add_argument("--allow-self-target", action="store_true")
    p_r_update.add_argument("--enabled", default=None)
    p_r_update.add_argument("--description", default=None)
    p_r_update.add_argument("--no-apply", action="store_true")

    p_r_copy = sub.add_parser(
        "route-copy", help="Copy an existing route to a new domain"
    )
    p_r_copy.add_argument("source_route_id", help="Source route ID to copy")
    p_r_copy.add_argument("--domain", default=None)
    p_r_copy.add_argument("--route-id", default=None)
    p_r_copy.add_argument("--enabled", default=None)
    p_r_copy.add_argument("--description", default=None)
    p_r_copy.add_argument("--no-apply", action="store_true")

    p_r_enable = sub.add_parser("route-enable", help="Enable a route")
    p_r_enable.add_argument("route_id", help="Route ID")
    p_r_enable.add_argument("--no-apply", action="store_true")

    p_r_disable = sub.add_parser("route-disable", help="Disable a route")
    p_r_disable.add_argument("route_id", help="Route ID")
    p_r_disable.add_argument("--no-apply", action="store_true")

    p_r_delete = sub.add_parser("route-delete", help="Delete a route")
    p_r_delete.add_argument("route_id", help="Route ID")
    p_r_delete.add_argument("--no-apply", action="store_true")

    p_r_apply = sub.add_parser(
        "route-apply", help="Generate, validate, atomically replace, and reload Nginx router config"
    )
    p_r_apply.add_argument("--trigger-action", default=None)
    p_r_apply.add_argument("--raw-candidate-config", default=None)
    p_r_apply.add_argument("--inject-invalid-directive", default=None)
    p_r_apply.add_argument("--simulate-reload-failure", action="store_true")

    p_d_status = sub.add_parser(
        "discovery-status", help="Show Docker discovery status and availability"
    )
    p_d_status.add_argument("--no-refresh", action="store_true")

    p_d_containers = sub.add_parser(
        "discovery-containers", help="List discovered Docker containers"
    )
    p_d_containers.add_argument("--include-self", action="store_true")
    p_d_containers.add_argument("--state", default=None)
    p_d_containers.add_argument("--scope", default="project")

    p_d_targets = sub.add_parser(
        "discovery-targets",
        help="List discovered Docker container + port upstream target candidates",
    )
    p_d_targets.add_argument("--include-self", action="store_true")
    p_d_targets.add_argument("--include-stopped", action="store_true")
    p_d_targets.add_argument("--scope", default="project")

    p_d_refresh = sub.add_parser(
        "discovery-refresh",
        help="Explicitly refresh Docker containers and upstream target candidates",
    )
    p_d_refresh.add_argument("--include-self", action="store_true")
    p_d_refresh.add_argument("--include-stopped", action="store_true")
    p_d_refresh.add_argument("--scope", default="project")

    p_d_config = sub.add_parser(
        "discovery-config",
        help="Configure or reset Docker socket path override",
    )
    p_d_config.add_argument("--socket-path-override", default=None)
    p_d_config.add_argument("--reset", action="store_true")

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

    p_rpl_check = sub.add_parser(
        "replayability",
        help="Check whether a capture record is replayable and return explicit reason if not",
    )
    p_rpl_check.add_argument("capture_id", help="Capture ID")

    p_dr_create = sub.add_parser(
        "draft-create", help="Create a persistent editable draft from a replayable capture"
    )
    p_dr_create.add_argument("capture_id", help="Source capture ID")
    p_dr_create.add_argument("--draft-id", default=None)
    p_dr_create.add_argument("--ttl-seconds", type=float, default=None)
    p_dr_create.add_argument("--expires-at", default=None)
    p_dr_create.add_argument("--edits-json", default=None)

    p_dr_list = sub.add_parser("draft-list", help="List active request drafts")
    p_dr_list.add_argument("--source-capture-id", default=None)
    p_dr_list.add_argument("--limit", type=int, default=100)

    p_dr_get = sub.add_parser("draft-get", help="Get a request draft by ID")
    p_dr_get.add_argument("draft_id", help="Draft ID")
    p_dr_get.add_argument("--now", default=None)

    p_dr_update = sub.add_parser(
        "draft-update", help="Update a request draft with structured or raw edits"
    )
    p_dr_update.add_argument("draft_id", help="Draft ID")
    p_dr_update.add_argument("--method", default=None)
    p_dr_update.add_argument("--scheme", default=None)
    p_dr_update.add_argument("--authority", default=None)
    p_dr_update.add_argument("--host", default=None)
    p_dr_update.add_argument("--raw-path", default=None)
    p_dr_update.add_argument("--decoded-path", default=None)
    p_dr_update.add_argument("--path", default=None)
    p_dr_update.add_argument("--raw-query", default=None)
    p_dr_update.add_argument("--content-type", default=None)
    p_dr_update.add_argument("--query-add", action="append", default=[])
    p_dr_update.add_argument("--query-set", action="append", default=[])
    p_dr_update.add_argument("--query-remove", action="append", default=[])
    p_dr_update.add_argument("--header-add", action="append", default=[])
    p_dr_update.add_argument("--header-set", action="append", default=[])
    p_dr_update.add_argument("--header-remove", action="append", default=[])
    p_dr_update.add_argument("--cookie-add", action="append", default=[])
    p_dr_update.add_argument("--cookie-set", action="append", default=[])
    p_dr_update.add_argument("--cookie-remove", action="append", default=[])
    p_dr_update.add_argument("--json-text", default=None)
    p_dr_update.add_argument("--json-set", action="append", default=[])
    p_dr_update.add_argument("--json-delete", action="append", default=[])
    p_dr_update.add_argument("--form-add", action="append", default=[])
    p_dr_update.add_argument("--form-set", action="append", default=[])
    p_dr_update.add_argument("--form-remove", action="append", default=[])
    p_dr_update.add_argument("--raw-text", default=None)
    p_dr_update.add_argument("--raw-base64", default=None)
    p_dr_update.add_argument("--ttl-seconds", type=float, default=None)
    p_dr_update.add_argument("--expires-at", default=None)
    p_dr_update.add_argument("--updates-json", default=None)

    p_dr_delete = sub.add_parser("draft-delete", help="Delete a request draft")
    p_dr_delete.add_argument("draft_id", help="Draft ID")

    p_dr_export = sub.add_parser(
        "draft-export", help="Export a request draft as cURL and browser fetch JS"
    )
    p_dr_export.add_argument("draft_id", help="Draft ID")
    p_dr_export.add_argument(
        "--format", choices=["all", "curl", "fetch"], default="all"
    )
    p_dr_export.add_argument("--edge-url", default=None)
    p_dr_export.add_argument("--cacert", default=None)

    p_dr_replay = sub.add_parser(
        "draft-replay",
        help="Normalize and replay a request draft through the proxy chain",
    )
    p_dr_replay.add_argument("draft_id", help="Draft ID")
    p_dr_replay.add_argument(
        "--edge-url",
        default=os.environ.get("REPLAY_EDGE_URL", "https://edge:443"),
    )
    p_dr_replay.add_argument(
        "--cacert",
        default=os.environ.get("REPLAY_CACERT", ""),
    )
    p_dr_replay.add_argument("--override-host", default="")

    p_ru_list = sub.add_parser(
        "rule-list", help="List persisted automatic request modification rules"
    )
    p_ru_list.add_argument(
        "--enabled",
        default=None,
        help="Optional filter by enabled status (true/false)",
    )

    p_ru_get = sub.add_parser(
        "rule-get", help="Get a single modification rule by ID"
    )
    p_ru_get.add_argument("rule_id", help="Rule ID")

    p_ru_create = sub.add_parser(
        "rule-create", help="Create a new automatic request modification rule"
    )
    p_ru_create.add_argument("--name", required=True)
    p_ru_create.add_argument("--rule-id", default=None)
    p_ru_create.add_argument("--description", default="")
    p_ru_create.add_argument("--enabled", default="true")
    p_ru_create.add_argument("--priority", type=int, default=100)
    _add_rule_match_and_action_args(p_ru_create)

    p_ru_update = sub.add_parser(
        "rule-update", help="Update an existing modification rule"
    )
    p_ru_update.add_argument("rule_id", help="Rule ID")
    p_ru_update.add_argument("--name", default=None)
    p_ru_update.add_argument("--description", default=None)
    p_ru_update.add_argument("--enabled", default=None)
    p_ru_update.add_argument("--priority", type=int, default=None)
    _add_rule_match_and_action_args(p_ru_update)

    p_ru_copy = sub.add_parser(
        "rule-copy", help="Copy an existing modification rule"
    )
    p_ru_copy.add_argument("source_rule_id", help="Source rule ID to copy")
    p_ru_copy.add_argument("--rule-id", default=None)
    p_ru_copy.add_argument("--name", default=None)
    p_ru_copy.add_argument("--description", default=None)
    p_ru_copy.add_argument("--enabled", default=None)
    p_ru_copy.add_argument("--priority", type=int, default=None)
    _add_rule_match_and_action_args(p_ru_copy)

    p_ru_enable = sub.add_parser("rule-enable", help="Enable a modification rule")
    p_ru_enable.add_argument("rule_id", help="Rule ID")

    p_ru_disable = sub.add_parser(
        "rule-disable", help="Disable a modification rule"
    )
    p_ru_disable.add_argument("rule_id", help="Rule ID")

    p_ru_delete = sub.add_parser("rule-delete", help="Delete a modification rule")
    p_ru_delete.add_argument("rule_id", help="Rule ID")

    p_ru_preview = sub.add_parser(
        "rule-preview",
        help="Dry-run preview modification rules against a capture or synthetic request",
    )
    p_ru_preview.add_argument("--capture-id", default=None)
    p_ru_preview.add_argument("--request-json", default=None)
    p_ru_preview.add_argument("--method", default="GET")
    p_ru_preview.add_argument("--authority", default=None)
    p_ru_preview.add_argument("--host", default=None)
    p_ru_preview.add_argument("--raw-path", default="/")
    p_ru_preview.add_argument("--raw-query", default="")
    p_ru_preview.add_argument("--content-type", default=None)
    p_ru_preview.add_argument("--headers-json", default=None)
    p_ru_preview.add_argument("--body-text", default=None)
    p_ru_preview.add_argument("--body-base64", default=None)
    p_ru_preview.add_argument("--rule-id", action="append", default=[])
    p_ru_preview.add_argument("--include-disabled", action="store_true")
    p_ru_preview.add_argument("--candidate-rule-json", default=None)

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
    if args.subcommand == "route-list":
        return cmd_route_list(args)
    if args.subcommand == "route-get":
        return cmd_route_get(args)
    if args.subcommand == "route-status":
        return cmd_route_status(args)
    if args.subcommand in ("route-create", "route-create-from-target"):
        return cmd_route_create(args)
    if args.subcommand == "route-update":
        return cmd_route_update(args)
    if args.subcommand == "route-copy":
        return cmd_route_copy(args)
    if args.subcommand == "route-enable":
        return cmd_route_enable_disable(args, True)
    if args.subcommand == "route-disable":
        return cmd_route_enable_disable(args, False)
    if args.subcommand == "route-delete":
        return cmd_route_delete(args)
    if args.subcommand == "route-apply":
        return cmd_route_apply(args)
    if args.subcommand == "discovery-status":
        return cmd_discovery_status(args)
    if args.subcommand == "discovery-containers":
        return cmd_discovery_containers(args)
    if args.subcommand == "discovery-targets":
        return cmd_discovery_targets(args)
    if args.subcommand == "discovery-refresh":
        return cmd_discovery_refresh(args)
    if args.subcommand == "discovery-config":
        return cmd_discovery_config(args)
    if args.subcommand == "replay":
        return cmd_replay(args)
    if args.subcommand == "replayability":
        return cmd_replayability(args)
    if args.subcommand == "draft-create":
        return cmd_draft_create(args)
    if args.subcommand == "draft-list":
        return cmd_draft_list(args)
    if args.subcommand == "draft-get":
        return cmd_draft_get(args)
    if args.subcommand == "draft-update":
        return cmd_draft_update(args)
    if args.subcommand == "draft-delete":
        return cmd_draft_delete(args)
    if args.subcommand == "draft-export":
        return cmd_draft_export(args)
    if args.subcommand == "draft-replay":
        return cmd_draft_replay(args)
    if args.subcommand == "rule-list":
        return cmd_rule_list(args)
    if args.subcommand == "rule-get":
        return cmd_rule_get(args)
    if args.subcommand == "rule-create":
        return cmd_rule_create(args)
    if args.subcommand == "rule-update":
        return cmd_rule_update(args)
    if args.subcommand == "rule-copy":
        return cmd_rule_copy(args)
    if args.subcommand == "rule-enable":
        return cmd_rule_enable_disable(args, True)
    if args.subcommand == "rule-disable":
        return cmd_rule_enable_disable(args, False)
    if args.subcommand == "rule-delete":
        return cmd_rule_delete(args)
    if args.subcommand == "rule-preview":
        return cmd_rule_preview(args)
    return 1


if __name__ == "__main__":
    sys.exit(main())
