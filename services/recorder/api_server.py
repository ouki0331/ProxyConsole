#!/usr/bin/env python3
"""Local HTTP API server for capture index, retention, and route management (DEV-010 / DEV-011 / DEV-020)."""

from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

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

API_HOST = os.environ.get("CONTROL_API_LISTEN_HOST", "0.0.0.0")
API_PORT = int(os.environ.get("CONTROL_API_PORT", "18480"))
WEB_CONSOLE_DIR = Path(
    os.environ.get(
        "WEB_CONSOLE_DIR",
        str(Path(__file__).resolve().parent.parent.parent / "web" / "console"),
    )
)
if not WEB_CONSOLE_DIR.is_dir() and Path("/app/web/console").is_dir():
    WEB_CONSOLE_DIR = Path("/app/web/console")

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

    def _send_static_file(self, filename: str, content_type: str) -> None:
        file_path = WEB_CONSOLE_DIR / filename
        if not file_path.is_file():
            self._send_json(
                404,
                {
                    "error": "console_asset_not_found",
                    "filename": filename,
                },
            )
            return
        body = file_path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
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
            if path in ("/", "/index.html", "/console", "/console/index.html"):
                self._send_static_file(
                    "index.html", "text/html; charset=utf-8"
                )
                return

            if path in ("/styles.css", "/console/styles.css"):
                self._send_static_file(
                    "styles.css", "text/css; charset=utf-8"
                )
                return

            if path in ("/app.js", "/console/app.js"):
                self._send_static_file(
                    "app.js", "application/javascript; charset=utf-8"
                )
                return

            if path == "/favicon.ico":
                self.send_response(204)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return

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

            if path == "/api/v1/discovery/status":
                ref_str = _first_param(qs, "refresh")
                do_refresh = (
                    _parse_bool(ref_str) if ref_str is not None else True
                )
                self._send_json(
                    200, self.store.discovery.get_status(refresh=do_refresh)
                )
                return

            if path == "/api/v1/discovery/containers":
                inc_self_str = _first_param(qs, "include_self")
                inc_self = (
                    _parse_bool(inc_self_str)
                    if inc_self_str is not None
                    else False
                )
                state_filter = _first_param(qs, "state")
                scope = _first_param(qs, "scope") or "project"
                self._send_json(
                    200,
                    self.store.discovery.list_containers(
                        include_self=inc_self,
                        state=state_filter,
                        scope=scope,
                    ),
                )
                return

            if path == "/api/v1/discovery/targets":
                inc_self_str = _first_param(qs, "include_self")
                inc_self = (
                    _parse_bool(inc_self_str)
                    if inc_self_str is not None
                    else False
                )
                inc_stop_str = _first_param(qs, "include_stopped")
                inc_stop = (
                    _parse_bool(inc_stop_str)
                    if inc_stop_str is not None
                    else False
                )
                scope = _first_param(qs, "scope") or "project"
                self._send_json(
                    200,
                    self.store.discovery.list_targets(
                        include_self=inc_self,
                        include_stopped=inc_stop,
                        scope=scope,
                    ),
                )
                return

            if path == "/api/v1/routes":
                en_str = _first_param(qs, "enabled")
                enabled_filter = (
                    _parse_bool(en_str) if en_str is not None else None
                )
                self._send_json(
                    200,
                    self.store.routes.list_routes(enabled=enabled_filter),
                )
                return

            if path == "/api/v1/routes/status":
                runs_limit_str = _first_param(qs, "runs_limit")
                runs_limit = (
                    int(runs_limit_str) if runs_limit_str is not None else 20
                )
                self._send_json(
                    200,
                    self.store.routes.get_routes_status(runs_limit=runs_limit),
                )
                return

            if path.startswith("/api/v1/routes/"):
                route_id = unquote(path[len("/api/v1/routes/") :])
                try:
                    self._send_json(200, self.store.routes.get_route(route_id))
                except RouteNotFoundError as exc:
                    self._send_json(
                        404,
                        {
                            "error": "route_not_found",
                            "route_id": route_id,
                            "message": str(exc),
                        },
                    )
                return

            if path == "/api/v1/rules":
                en_str = _first_param(qs, "enabled")
                enabled_filter = (
                    _parse_bool(en_str) if en_str is not None else None
                )
                self._send_json(
                    200,
                    self.store.rules.list_rules(enabled=enabled_filter),
                )
                return

            if path.startswith("/api/v1/rules/"):
                rule_id = unquote(path[len("/api/v1/rules/") :])
                try:
                    self._send_json(200, self.store.rules.get_rule(rule_id))
                except RuleNotFoundError as exc:
                    self._send_json(
                        404,
                        {
                            "error": "rule_not_found",
                            "rule_id": rule_id,
                            "message": str(exc),
                        },
                    )
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
                source_capture_id = _first_param(qs, "source_capture_id")
                draft_id_filter = _first_param(qs, "draft_id")
                rule_id_filter = _first_param(qs, "rule_id")
                applied_rule_id = _first_param(qs, "applied_rule_id")
                failed_rule_id = _first_param(qs, "failed_rule_id")
                skipped_rule_id = _first_param(qs, "skipped_rule_id")
                rule_mod_str = _first_param(qs, "rule_modified")
                rule_fail_str = _first_param(qs, "rule_failed")
                rule_outcome = _first_param(qs, "rule_outcome")
                rule_modified = (
                    _parse_bool(rule_mod_str)
                    if rule_mod_str is not None
                    else None
                )
                rule_failed = (
                    _parse_bool(rule_fail_str)
                    if rule_fail_str is not None
                    else None
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
                    source_capture_id=source_capture_id,
                    draft_id=draft_id_filter,
                    rule_id=rule_id_filter,
                    applied_rule_id=applied_rule_id,
                    failed_rule_id=failed_rule_id,
                    skipped_rule_id=skipped_rule_id,
                    rule_modified=rule_modified,
                    rule_failed=rule_failed,
                    rule_outcome=rule_outcome,
                )
                self._send_json(200, result)
                return

            if path == "/api/v1/drafts":
                src_cid = _first_param(qs, "source_capture_id") or _first_param(
                    qs, "capture_id"
                )
                limit_str = _first_param(qs, "limit")
                limit = int(limit_str) if limit_str is not None else 100
                self._send_json(
                    200,
                    self.store.drafts.list_drafts(
                        source_capture_id=src_cid, limit=limit
                    ),
                )
                return

            if path.startswith("/api/v1/drafts/"):
                rest = path[len("/api/v1/drafts/") :]
                if rest.endswith("/export"):
                    draft_id = unquote(rest[: -len("/export")])
                    fmt = _first_param(qs, "format") or "all"
                    edge_url = _first_param(qs, "edge_url")
                    cacert = _first_param(qs, "cacert")
                    try:
                        self._send_json(
                            200,
                            self.store.drafts.export_draft(
                                draft_id,
                                format_type=fmt,
                                edge_url=edge_url,
                                cacert=cacert,
                            ),
                        )
                    except DraftExpiredError as exc:
                        self._send_json(
                            404,
                            {
                                "error": "draft_expired",
                                "draft_id": exc.draft_id,
                                "expires_at": exc.expires_at,
                                "message": str(exc),
                            },
                        )
                    except DraftNotFoundError as exc:
                        self._send_json(
                            404,
                            {
                                "error": "draft_not_found",
                                "draft_id": draft_id,
                                "message": str(exc),
                            },
                        )
                    return

                draft_id = unquote(rest)
                now_param = _first_param(qs, "now")
                try:
                    self._send_json(
                        200,
                        self.store.drafts.get_draft(draft_id, now=now_param),
                    )
                except DraftExpiredError as exc:
                    self._send_json(
                        404,
                        {
                            "error": "draft_expired",
                            "draft_id": exc.draft_id,
                            "expires_at": exc.expires_at,
                            "message": str(exc),
                        },
                    )
                except DraftNotFoundError as exc:
                    self._send_json(
                        404,
                        {
                            "error": "draft_not_found",
                            "draft_id": draft_id,
                            "message": str(exc),
                        },
                    )
                return

            if (
                path.startswith("/api/v1/captures/")
                and path.endswith("/replayability")
            ):
                capture_id = unquote(
                    path[len("/api/v1/captures/") : -len("/replayability")]
                )
                try:
                    self._send_json(
                        200,
                        self.store.drafts.evaluate_replayability(
                            capture_id, raise_on_false=False
                        ),
                    )
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

    def _handle_route_update(self, route_id: str) -> None:
        body = self._read_json_body()
        auto_apply = (
            _parse_bool(body["auto_apply"])
            if "auto_apply" in body and body["auto_apply"] is not None
            else True
        )
        enabled_val = (
            _parse_bool(body["enabled"])
            if "enabled" in body and body["enabled"] is not None
            else None
        )
        allow_self = (
            _parse_bool(body["allow_self_target"])
            if "allow_self_target" in body
            and body["allow_self_target"] is not None
            else False
        )
        res = self.store.routes.update_route(
            route_id,
            domain=body.get("domain"),
            upstream_scheme=body.get("upstream_scheme"),
            upstream_host=body.get("upstream_host"),
            upstream_port=body.get("upstream_port"),
            target_id=body.get("target_id"),
            container=body.get("container"),
            port=body.get("port"),
            allow_self_target=allow_self,
            enabled=enabled_val,
            description=body.get("description"),
            auto_apply=auto_apply,
        )
        self._send_json(200, res)

    def do_PUT(self) -> None:
        split = urlsplit(self.path)
        path = split.path.rstrip("/") or "/"
        try:
            if path == "/api/v1/retention/policy":
                self._handle_policy_update()
                return
            if path.startswith("/api/v1/routes/") and path != "/api/v1/routes/status":
                route_id = unquote(path[len("/api/v1/routes/") :])
                self._handle_route_update(route_id)
                return
            if path.startswith("/api/v1/rules/") and path != "/api/v1/rules/preview":
                rule_id = unquote(path[len("/api/v1/rules/") :])
                body = self._read_json_body()
                enabled_val = (
                    _parse_bool(body["enabled"])
                    if "enabled" in body and body["enabled"] is not None
                    else None
                )
                res = self.store.rules.update_rule(
                    rule_id,
                    name=body.get("name"),
                    description=body.get("description"),
                    enabled=enabled_val,
                    priority=body.get("priority"),
                    match=body.get("match"),
                    actions=body.get("actions"),
                )
                self._send_json(200, res)
                return
            if path.startswith("/api/v1/drafts/"):
                draft_id = unquote(path[len("/api/v1/drafts/") :])
                body = self._read_json_body()
                res = self.store.drafts.update_draft(draft_id, body)
                self._send_json(200, res)
                return
            self._send_json(404, {"error": "not_found", "path": path})
        except RuleNotFoundError as exc:
            self._send_json(
                404,
                {
                    "error": "rule_not_found",
                    "rule_id": exc.rule_id,
                    "message": str(exc),
                },
            )
        except RuleValidationError as exc:
            status_code = 409 if exc.code == "duplicate_rule_id" else 400
            self._send_json(status_code, exc.to_dict())
        except DraftExpiredError as exc:
            self._send_json(
                404,
                {
                    "error": "draft_expired",
                    "draft_id": exc.draft_id,
                    "expires_at": exc.expires_at,
                    "message": str(exc),
                },
            )
        except DraftNotFoundError as exc:
            self._send_json(
                404,
                {"error": "draft_not_found", "message": str(exc)},
            )
        except DraftValidationError as exc:
            self._send_json(
                400,
                {
                    "error": exc.code,
                    "message": str(exc),
                    **exc.details,
                },
            )
        except RouteNotFoundError as exc:
            self._send_json(
                404,
                {"error": "route_not_found", "message": str(exc)},
            )
        except DiscoveryTargetNotFoundError as exc:
            self._send_json(
                404,
                {"error": "target_not_found", "message": str(exc)},
            )
        except DiscoveryUnavailableError as exc:
            self._send_json(
                503,
                {
                    "error": "discovery_unavailable",
                    "message": str(exc),
                    "fallback_manual_upstream_supported": True,
                },
            )
        except DiscoveryTargetValidationError as exc:
            self._send_json(
                400,
                {"error": "invalid_target", "message": str(exc)},
            )
        except DuplicateDomainError as exc:
            self._send_json(
                409,
                {"error": "duplicate_domain", "message": str(exc)},
            )
        except (RouteValidationError, ValueError) as exc:
            self._send_json(
                400,
                {"error": "invalid_route", "message": str(exc)},
            )
        except Exception as exc:
            self._send_json(
                500,
                {"error": "internal_error", "message": str(exc)},
            )

    def do_PATCH(self) -> None:
        self.do_PUT()

    def do_DELETE(self) -> None:
        split = urlsplit(self.path)
        path = split.path.rstrip("/") or "/"
        qs = parse_qs(split.query, keep_blank_values=False)
        try:
            if path.startswith("/api/v1/routes/") and path != "/api/v1/routes/status":
                route_id = unquote(path[len("/api/v1/routes/") :])
                auto_q = _first_param(qs, "auto_apply")
                auto_apply = _parse_bool(auto_q) if auto_q is not None else True
                res = self.store.routes.delete_route(
                    route_id, auto_apply=auto_apply
                )
                self._send_json(200, res)
                return
            if path.startswith("/api/v1/drafts/"):
                draft_id = unquote(path[len("/api/v1/drafts/") :])
                res = self.store.drafts.delete_draft(draft_id)
                self._send_json(200, res)
                return
            if path.startswith("/api/v1/rules/") and path != "/api/v1/rules/preview":
                rule_id = unquote(path[len("/api/v1/rules/") :])
                res = self.store.rules.delete_rule(rule_id)
                self._send_json(200, res)
                return
            self._send_json(404, {"error": "not_found", "path": path})
        except RuleNotFoundError as exc:
            self._send_json(
                404,
                {
                    "error": "rule_not_found",
                    "rule_id": exc.rule_id,
                    "message": str(exc),
                },
            )
        except DraftNotFoundError as exc:
            self._send_json(
                404,
                {"error": "draft_not_found", "message": str(exc)},
            )
        except RouteNotFoundError as exc:
            self._send_json(
                404,
                {"error": "route_not_found", "message": str(exc)},
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

            if path == "/api/v1/discovery/refresh":
                inc_self = (
                    _parse_bool(body["include_self"])
                    if "include_self" in body and body["include_self"] is not None
                    else False
                )
                inc_stop = (
                    _parse_bool(body["include_stopped"])
                    if "include_stopped" in body
                    and body["include_stopped"] is not None
                    else False
                )
                scope = str(body.get("scope") or "project")
                self._send_json(
                    200,
                    self.store.discovery.refresh(
                        include_self=inc_self,
                        include_stopped=inc_stop,
                        scope=scope,
                    ),
                )
                return

            if path == "/api/v1/discovery/config":
                override = body.get("socket_path_override")
                res = self.store.discovery.set_socket_path_override(override)
                self._send_json(200, res)
                return

            if path in ("/api/v1/routes", "/api/v1/routes/from-target"):
                auto_apply = (
                    _parse_bool(body["auto_apply"])
                    if "auto_apply" in body and body["auto_apply"] is not None
                    else True
                )
                enabled = (
                    _parse_bool(body["enabled"])
                    if "enabled" in body and body["enabled"] is not None
                    else True
                )
                allow_self = (
                    _parse_bool(body["allow_self_target"])
                    if "allow_self_target" in body
                    and body["allow_self_target"] is not None
                    else False
                )
                if path == "/api/v1/routes/from-target":
                    res = self.store.routes.create_route_from_target(
                        domain=body.get("domain", ""),
                        target_id=body.get("target_id"),
                        container=body.get("container"),
                        port=body.get("port"),
                        upstream_scheme=body.get("upstream_scheme", "http"),
                        allow_self_target=allow_self,
                        route_id=body.get("route_id"),
                        enabled=enabled,
                        description=body.get("description", ""),
                        auto_apply=auto_apply,
                    )
                else:
                    res = self.store.routes.create_route(
                        domain=body.get("domain", ""),
                        upstream_scheme=body.get("upstream_scheme", "http"),
                        upstream_host=body.get("upstream_host"),
                        upstream_port=body.get("upstream_port"),
                        target_id=body.get("target_id"),
                        container=body.get("container"),
                        port=body.get("port"),
                        allow_self_target=allow_self,
                        route_id=body.get("route_id"),
                        enabled=enabled,
                        description=body.get("description", ""),
                        auto_apply=auto_apply,
                    )
                self._send_json(201, res)
                return

            if path == "/api/v1/routes/apply":
                sim_reload = (
                    _parse_bool(body["simulate_reload_failure"])
                    if "simulate_reload_failure" in body
                    and body["simulate_reload_failure"] is not None
                    else False
                )
                res = self.store.routes.apply_routes(
                    trigger_action=str(body.get("trigger_action") or "manual_apply"),
                    raw_candidate_config=body.get("raw_candidate_config"),
                    inject_invalid_directive=body.get("inject_invalid_directive"),
                    simulate_reload_failure=sim_reload,
                )
                http_code = 200 if res["applied"] else 422
                self._send_json(http_code, res)
                return

            if path.startswith("/api/v1/routes/") and path.endswith("/copy"):
                source_id = unquote(
                    path[len("/api/v1/routes/") : -len("/copy")]
                )
                auto_apply = (
                    _parse_bool(body["auto_apply"])
                    if "auto_apply" in body and body["auto_apply"] is not None
                    else True
                )
                enabled = (
                    _parse_bool(body["enabled"])
                    if "enabled" in body and body["enabled"] is not None
                    else None
                )
                res = self.store.routes.copy_route(
                    source_id,
                    domain=body.get("domain"),
                    route_id=body.get("route_id"),
                    enabled=enabled,
                    description=body.get("description"),
                    upstream_scheme=body.get("upstream_scheme"),
                    upstream_host=body.get("upstream_host"),
                    upstream_port=body.get("upstream_port"),
                    auto_apply=auto_apply,
                )
                self._send_json(201, res)
                return

            if path.startswith("/api/v1/routes/") and path.endswith("/enable"):
                route_id = unquote(
                    path[len("/api/v1/routes/") : -len("/enable")]
                )
                auto_apply = (
                    _parse_bool(body["auto_apply"])
                    if "auto_apply" in body and body["auto_apply"] is not None
                    else True
                )
                res = self.store.routes.set_route_enabled(
                    route_id, True, auto_apply=auto_apply
                )
                self._send_json(200, res)
                return

            if path.startswith("/api/v1/routes/") and path.endswith("/disable"):
                route_id = unquote(
                    path[len("/api/v1/routes/") : -len("/disable")]
                )
                auto_apply = (
                    _parse_bool(body["auto_apply"])
                    if "auto_apply" in body and body["auto_apply"] is not None
                    else True
                )
                res = self.store.routes.set_route_enabled(
                    route_id, False, auto_apply=auto_apply
                )
                self._send_json(200, res)
                return

            if path == "/api/v1/rules":
                enabled_val = (
                    _parse_bool(body["enabled"])
                    if "enabled" in body and body["enabled"] is not None
                    else True
                )
                res = self.store.rules.create_rule(
                    name=body.get("name", ""),
                    match=body.get("match"),
                    actions=body.get("actions"),
                    rule_id=body.get("rule_id"),
                    description=body.get("description", ""),
                    enabled=enabled_val,
                    priority=body.get("priority", 100),
                    copied_from_rule_id=body.get("copied_from_rule_id"),
                )
                self._send_json(201, res)
                return

            if path == "/api/v1/rules/preview":
                include_disabled = (
                    _parse_bool(body["include_disabled"])
                    if "include_disabled" in body
                    and body["include_disabled"] is not None
                    else False
                )
                res = self.store.rules.preview_rules(
                    capture_id=body.get("capture_id"),
                    request_spec=body.get("request")
                    if isinstance(body.get("request"), dict)
                    else body,
                    rule_ids=body.get("rule_ids"),
                    include_disabled=include_disabled,
                    candidate_rule=body.get("candidate_rule"),
                    rules=body.get("rules"),
                )
                self._send_json(200, res)
                return

            if (
                path.startswith("/api/v1/captures/")
                and path.endswith("/rules-preview")
            ):
                cap_id = unquote(
                    path[len("/api/v1/captures/") : -len("/rules-preview")]
                )
                include_disabled = (
                    _parse_bool(body["include_disabled"])
                    if "include_disabled" in body
                    and body["include_disabled"] is not None
                    else False
                )
                res = self.store.rules.preview_rules(
                    capture_id=cap_id,
                    rule_ids=body.get("rule_ids"),
                    include_disabled=include_disabled,
                    candidate_rule=body.get("candidate_rule"),
                    rules=body.get("rules"),
                )
                self._send_json(200, res)
                return

            if path.startswith("/api/v1/rules/") and path.endswith("/copy"):
                source_id = unquote(
                    path[len("/api/v1/rules/") : -len("/copy")]
                )
                enabled_val = (
                    _parse_bool(body["enabled"])
                    if "enabled" in body and body["enabled"] is not None
                    else None
                )
                res = self.store.rules.copy_rule(
                    source_id,
                    rule_id=body.get("rule_id"),
                    name=body.get("name"),
                    description=body.get("description"),
                    enabled=enabled_val,
                    priority=body.get("priority"),
                    match=body.get("match"),
                    actions=body.get("actions"),
                )
                self._send_json(201, res)
                return

            if path.startswith("/api/v1/rules/") and path.endswith("/enable"):
                rule_id = unquote(
                    path[len("/api/v1/rules/") : -len("/enable")]
                )
                res = self.store.rules.set_rule_enabled(rule_id, True)
                self._send_json(200, res)
                return

            if path.startswith("/api/v1/rules/") and path.endswith("/disable"):
                rule_id = unquote(
                    path[len("/api/v1/rules/") : -len("/disable")]
                )
                res = self.store.rules.set_rule_enabled(rule_id, False)
                self._send_json(200, res)
                return

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

            if path == "/api/v1/drafts":
                cap_id = body.get("capture_id") or body.get("source_capture_id")
                if not cap_id:
                    raise DraftValidationError(
                        "Creating a draft requires 'capture_id' (or 'source_capture_id')",
                        code="missing_capture_id",
                    )
                res = self.store.drafts.create_draft(
                    str(cap_id),
                    draft_id=body.get("draft_id"),
                    ttl_seconds=body.get("ttl_seconds"),
                    expires_at=body.get("expires_at"),
                    edits=body.get("edits"),
                )
                self._send_json(201, res)
                return

            if path == "/api/v1/drafts/prune":
                now_param = _first_param(qs, "now") or body.get("now")
                res = self.store.drafts.prune_expired_drafts(
                    now=str(now_param) if now_param else None
                )
                self._send_json(200, res)
                return

            if (
                path.startswith("/api/v1/captures/")
                and path.endswith("/drafts")
            ):
                cap_id = unquote(
                    path[len("/api/v1/captures/") : -len("/drafts")]
                )
                res = self.store.drafts.create_draft(
                    cap_id,
                    draft_id=body.get("draft_id"),
                    ttl_seconds=body.get("ttl_seconds"),
                    expires_at=body.get("expires_at"),
                    edits=body.get("edits"),
                )
                self._send_json(201, res)
                return

            if (
                path.startswith("/api/v1/captures/")
                and path.endswith("/replay")
            ):
                cap_id = unquote(
                    path[len("/api/v1/captures/") : -len("/replay")]
                )
                res = self.store.drafts.replay_capture(
                    cap_id,
                    edge_url=body.get("edge_url"),
                    cacert=body.get("cacert"),
                    override_host=body.get("override_host"),
                )
                self._send_json(200, res)
                return

            if (
                path.startswith("/api/v1/drafts/")
                and path.endswith("/replay")
            ):
                draft_id = unquote(
                    path[len("/api/v1/drafts/") : -len("/replay")]
                )
                res = self.store.drafts.replay_draft(
                    draft_id,
                    edge_url=body.get("edge_url"),
                    cacert=body.get("cacert"),
                    override_host=body.get("override_host"),
                )
                self._send_json(200, res)
                return

            if (
                path.startswith("/api/v1/drafts/")
                and path.endswith("/export")
            ):
                draft_id = unquote(
                    path[len("/api/v1/drafts/") : -len("/export")]
                )
                fmt = (
                    _first_param(qs, "format")
                    or body.get("format")
                    or "all"
                )
                res = self.store.drafts.export_draft(
                    draft_id,
                    format_type=str(fmt),
                    edge_url=body.get("edge_url") or _first_param(qs, "edge_url"),
                    cacert=body.get("cacert") or _first_param(qs, "cacert"),
                )
                self._send_json(200, res)
                return

            self._send_json(404, {"error": "not_found", "path": path})
        except FileNotFoundError as exc:
            self._send_json(
                404,
                {"error": "capture_not_found", "message": str(exc)},
            )
        except RuleNotFoundError as exc:
            self._send_json(
                404,
                {
                    "error": "rule_not_found",
                    "rule_id": exc.rule_id,
                    "message": str(exc),
                },
            )
        except RuleValidationError as exc:
            status_code = 409 if exc.code == "duplicate_rule_id" else 400
            self._send_json(status_code, exc.to_dict())
        except DraftExpiredError as exc:
            self._send_json(
                404,
                {
                    "error": "draft_expired",
                    "draft_id": exc.draft_id,
                    "expires_at": exc.expires_at,
                    "message": str(exc),
                },
            )
        except DraftNotFoundError as exc:
            self._send_json(
                404,
                {"error": "draft_not_found", "message": str(exc)},
            )
        except NonReplayableCaptureError as exc:
            self._send_json(400, exc.to_dict())
        except DraftValidationError as exc:
            self._send_json(
                400,
                {
                    "error": exc.code,
                    "message": str(exc),
                    **exc.details,
                },
            )
        except RouteNotFoundError as exc:
            self._send_json(
                404,
                {"error": "route_not_found", "message": str(exc)},
            )
        except DiscoveryTargetNotFoundError as exc:
            self._send_json(
                404,
                {"error": "target_not_found", "message": str(exc)},
            )
        except DiscoveryUnavailableError as exc:
            self._send_json(
                503,
                {
                    "error": "discovery_unavailable",
                    "message": str(exc),
                    "fallback_manual_upstream_supported": True,
                },
            )
        except DiscoveryTargetValidationError as exc:
            self._send_json(
                400,
                {"error": "invalid_target", "message": str(exc)},
            )
        except DuplicateDomainError as exc:
            self._send_json(
                409,
                {"error": "duplicate_domain", "message": str(exc)},
            )
        except (RouteValidationError, ValueError) as exc:
            self._send_json(
                400,
                {"error": "invalid_route", "message": str(exc)},
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
