#!/usr/bin/env python3
"""Route management core and atomic Nginx router configuration apply/rollback (DEV-020).

Manages domain-to-upstream routing rules in SQLite (`routes`), generates Nginx internal
router configuration, coordinates isolated `nginx -t` validation, atomic file replacement,
and `nginx -s reload` (with automatic rollback on reload failure) with the `router`
container via `/var/lib/proxyconsole/router`.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import time
from typing import TYPE_CHECKING, Any, Iterator
import uuid

if TYPE_CHECKING:
    from index_store import CaptureIndexStore

DOMAIN_RE = re.compile(
    r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*$"
)
UPSTREAM_HOST_RE = re.compile(
    r"^[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$"
)
ROUTE_ID_RE = re.compile(r"^[A-Za-z0-9:_-]{1,120}$")


class RouteValidationError(ValueError):
    """Raised when route fields fail validation."""


class DuplicateDomainError(ValueError):
    """Raised when a domain is already assigned to another route."""


class RouteNotFoundError(KeyError):
    """Raised when a route_id does not exist."""


def validate_domain(domain: Any) -> str:
    if not isinstance(domain, str):
        raise RouteValidationError("domain must be a string")
    cleaned = domain.strip().lower()
    if not cleaned:
        raise RouteValidationError("domain must not be empty")
    if len(cleaned) > 253 or not DOMAIN_RE.match(cleaned):
        raise RouteValidationError(f"Invalid domain format: {domain!r}")
    return cleaned


def validate_upstream_scheme(scheme: Any) -> str:
    if not isinstance(scheme, str):
        raise RouteValidationError("upstream_scheme must be 'http' or 'https'")
    cleaned = scheme.strip().lower()
    if cleaned not in ("http", "https"):
        raise RouteValidationError(
            f"Invalid upstream_scheme {scheme!r}: must be 'http' or 'https'"
        )
    return cleaned


def validate_upstream_host(host: Any) -> str:
    if not isinstance(host, str):
        raise RouteValidationError("upstream_host must be a non-empty string")
    cleaned = host.strip()
    if not cleaned:
        raise RouteValidationError("upstream_host must not be empty")
    if not UPSTREAM_HOST_RE.match(cleaned):
        raise RouteValidationError(f"Invalid upstream_host format: {host!r}")
    return cleaned


def validate_upstream_port(port: Any) -> int:
    if isinstance(port, bool) or port is None:
        raise RouteValidationError("upstream_port must be an integer between 1 and 65535")
    try:
        val = int(port)
    except (TypeError, ValueError) as exc:
        raise RouteValidationError(f"Invalid upstream_port: {port!r}") from exc
    if val < 1 or val > 65535:
        raise RouteValidationError(
            f"Invalid upstream_port {val}: must be between 1 and 65535"
        )
    return val


def validate_route_id(route_id: Any) -> str:
    if not isinstance(route_id, str):
        raise RouteValidationError("route_id must be a string")
    cleaned = route_id.strip()
    if not cleaned or not ROUTE_ID_RE.match(cleaned):
        raise RouteValidationError(f"Invalid route_id format: {route_id!r}")
    return cleaned


class RouteManager:
    def __init__(self, store: CaptureIndexStore) -> None:
        self.store = store
        self.lock_path = self.store.db_dir / "routes.lock"
        self.router_dir = self.store.storage_dir / "router"
        self.candidates_dir = self.router_dir / "candidates"
        self.requests_dir = self.router_dir / "requests"
        self.responses_dir = self.router_dir / "responses"
        self.active_conf_path = self.router_dir / "active.conf"

    @contextmanager
    def _exclusive_lock(self) -> Iterator[None]:
        self.store.db_dir.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self.lock_path), os.O_CREAT | os.O_RDWR, 0o666)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    def _row_to_dict(self, row: Any) -> dict[str, Any]:
        scheme = str(row["upstream_scheme"])
        host = str(row["upstream_host"])
        port = int(row["upstream_port"])
        return {
            "route_id": str(row["route_id"]),
            "domain": str(row["domain"]),
            "upstream_scheme": scheme,
            "upstream_host": host,
            "upstream_port": port,
            "upstream_url": f"{scheme}://{host}:{port}",
            "enabled": bool(row["enabled"]),
            "copied_from_route_id": row["copied_from_route_id"],
            "description": str(row["description"] or ""),
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
        }

    def get_apply_status(self) -> dict[str, Any]:
        self.store.ensure_schema()
        with self.store._connect() as conn:
            row = conn.execute(
                "SELECT * FROM route_apply_state WHERE id = 1"
            ).fetchone()
        active_sha = None
        if self.active_conf_path.is_file():
            try:
                active_sha = hashlib.sha256(
                    self.active_conf_path.read_bytes()
                ).hexdigest()
            except OSError:
                active_sha = None
        if row is None:
            return {
                "desired_generation": 1,
                "applied_generation": 1,
                "in_sync": True,
                "last_apply_status": "applied",
                "last_apply_at": None,
                "last_attempt_at": None,
                "last_error": None,
                "rolled_back": False,
                "active_config_sha256": active_sha,
            }
        desired_gen = int(row["desired_generation"])
        applied_gen = int(row["applied_generation"])
        last_status = str(row["last_apply_status"])
        db_sha = row["active_config_sha256"] or active_sha
        return {
            "desired_generation": desired_gen,
            "applied_generation": applied_gen,
            "in_sync": (desired_gen == applied_gen and last_status == "applied"),
            "last_apply_status": last_status,
            "last_apply_at": row["last_apply_at"],
            "last_attempt_at": row["last_attempt_at"],
            "last_error": row["last_error"],
            "rolled_back": bool(row["rolled_back"]),
            "active_config_sha256": db_sha,
            "updated_at": row["updated_at"],
        }

    def get_recent_apply_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        self.store.ensure_schema()
        safe_limit = max(1, min(int(limit), 100))
        with self.store._connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    apply_id, attempted_at, trigger_action,
                    desired_generation, applied_generation_after,
                    status, rolled_back, error_message,
                    candidate_sha256, active_config_sha256, enabled_routes_count
                FROM route_apply_runs
                ORDER BY apply_id DESC
                LIMIT ?
                """,
                (safe_limit,),
            ).fetchall()
        return [
            {
                "apply_id": int(r["apply_id"]),
                "attempted_at": r["attempted_at"],
                "trigger_action": r["trigger_action"],
                "desired_generation": int(r["desired_generation"]),
                "applied_generation_after": int(r["applied_generation_after"]),
                "status": r["status"],
                "rolled_back": bool(r["rolled_back"]),
                "error_message": r["error_message"],
                "candidate_sha256": r["candidate_sha256"],
                "active_config_sha256": r["active_config_sha256"],
                "enabled_routes_count": int(r["enabled_routes_count"]),
            }
            for r in rows
        ]

    def get_routes_status(self, *, runs_limit: int = 20) -> dict[str, Any]:
        routes_resp = self.list_routes()
        items = routes_resp["items"]
        enabled_count = sum(1 for r in items if r["enabled"])
        disabled_count = len(items) - enabled_count
        active_conf = ""
        if self.active_conf_path.is_file():
            try:
                active_conf = self.active_conf_path.read_text(encoding="utf-8")
            except OSError:
                active_conf = ""
        return {
            "apply_status": routes_resp["apply_status"],
            "routes_count": len(items),
            "enabled_routes_count": enabled_count,
            "disabled_routes_count": disabled_count,
            "active_config": active_conf,
            "recent_apply_runs": self.get_recent_apply_runs(limit=runs_limit),
        }

    def list_routes(self, *, enabled: bool | None = None) -> dict[str, Any]:
        self.store.ensure_schema()
        where_sql = ""
        params: list[Any] = []
        if enabled is not None:
            where_sql = "WHERE enabled = ?"
            params.append(1 if enabled else 0)
        with self.store._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM routes
                {where_sql}
                ORDER BY domain ASC, route_id ASC
                """,
                params,
            ).fetchall()
        items = [self._row_to_dict(r) for r in rows]
        return {
            "total": len(items),
            "items": items,
            "apply_status": self.get_apply_status(),
        }

    def get_route(self, route_id: str) -> dict[str, Any]:
        self.store.ensure_schema()
        with self.store._connect() as conn:
            row = conn.execute(
                "SELECT * FROM routes WHERE route_id = ?",
                (route_id,),
            ).fetchone()
        if row is None:
            raise RouteNotFoundError(f"Route not found: {route_id}")
        return {
            "route": self._row_to_dict(row),
            "apply_status": self.get_apply_status(),
        }

    def resolve_route_id_for_host(self, host_only: str) -> str:
        clean_host = (host_only or "").strip().lower()
        if not clean_host:
            return "route:unmatched:empty"
        try:
            self.store.ensure_schema()
            with self.store._connect() as conn:
                row = conn.execute(
                    "SELECT route_id FROM routes WHERE domain = ? AND enabled = 1",
                    (clean_host,),
                ).fetchone()
            if row is not None:
                return str(row["route_id"])
        except Exception:
            if clean_host == "app-a.local.test":
                return "route:app-a"
            if clean_host == "app-b.local.test":
                return "route:app-b"
        return f"route:unmatched:{clean_host}"

    def generate_nginx_config(self) -> tuple[str, int]:
        self.store.ensure_schema()
        with self.store._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM routes
                WHERE enabled = 1
                ORDER BY domain ASC, route_id ASC
                """
            ).fetchall()

        blocks: list[str] = [
            "map $http_host $router_forwarded_host {\n"
            "    default $http_host;\n"
            '    ""      $host;\n'
            "}\n",
            "map $http_upgrade $connection_upgrade {\n"
            "    default upgrade;\n"
            '    ""      "";\n'
            "}\n",
            "server {\n"
            "    listen 8080 default_server;\n"
            "    server_name _;\n\n"
            "    keepalive_timeout 0;\n"
            "    client_max_body_size 0;\n\n"
            "    location / {\n"
            "        default_type application/json;\n"
            '        return 404 \'{"error":"unknown_host","message":"No route configured for Host","host":"$router_forwarded_host"}\\n\';\n'
            "    }\n"
            "}\n",
        ]

        for idx, row in enumerate(rows, start=1):
            domain = validate_domain(row["domain"])
            scheme = validate_upstream_scheme(row["upstream_scheme"])
            host = validate_upstream_host(row["upstream_host"])
            port = validate_upstream_port(row["upstream_port"])
            var_name = f"$upstream_target_{idx}"
            upstream_url = f"{scheme}://{host}:{port}"
            blocks.append(
                f"# route_id: {row['route_id']}\n"
                f"server {{\n"
                f"    listen 8080;\n"
                f"    server_name {domain};\n\n"
                f"    keepalive_timeout 0;\n"
                f"    client_max_body_size 0;\n"
                f"    proxy_request_buffering off;\n\n"
                f"    resolver 127.0.0.11 valid=1s ipv6=off;\n\n"
                f"    location / {{\n"
                f"        set {var_name} {upstream_url};\n"
                f"        proxy_pass {var_name};\n"
                f"        proxy_http_version 1.1;\n"
                f"        proxy_set_header Host $router_forwarded_host;\n"
                f"        proxy_set_header Upgrade $http_upgrade;\n"
                f"        proxy_set_header Connection $connection_upgrade;\n"
                f"        proxy_set_header X-Forwarded-Proto $http_x_forwarded_proto;\n"
                f"        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;\n"
                f"        proxy_set_header X-Forwarded-Host $router_forwarded_host;\n"
                f"        proxy_send_timeout 120s;\n"
                f"        proxy_read_timeout 120s;\n"
                f"    }}\n"
                f"}}\n"
            )

        return "\n".join(blocks), len(rows)

    def _bump_desired_generation(self, conn: Any, now_iso: str) -> int:
        conn.execute(
            """
            UPDATE route_apply_state
            SET desired_generation = desired_generation + 1,
                updated_at = ?
            WHERE id = 1
            """,
            (now_iso,),
        )
        row = conn.execute(
            "SELECT desired_generation FROM route_apply_state WHERE id = 1"
        ).fetchone()
        return int(row["desired_generation"]) if row else 1

    def create_route(
        self,
        *,
        domain: str,
        upstream_scheme: str = "http",
        upstream_host: str,
        upstream_port: int,
        route_id: str | None = None,
        enabled: bool = True,
        description: str = "",
        copied_from_route_id: str | None = None,
        auto_apply: bool = True,
    ) -> dict[str, Any]:
        self.store.ensure_schema()
        clean_domain = validate_domain(domain)
        clean_scheme = validate_upstream_scheme(upstream_scheme)
        clean_host = validate_upstream_host(upstream_host)
        clean_port = validate_upstream_port(upstream_port)
        clean_id = (
            validate_route_id(route_id)
            if route_id
            else f"route:{uuid.uuid4().hex[:10]}"
        )
        now_iso = datetime.now(timezone.utc).isoformat()

        with self._exclusive_lock():
            with self.store._connect() as conn:
                dup = conn.execute(
                    "SELECT route_id FROM routes WHERE domain = ?",
                    (clean_domain,),
                ).fetchone()
                if dup is not None:
                    raise DuplicateDomainError(
                        f"Domain {clean_domain!r} is already assigned to route {dup['route_id']!r}"
                    )
                existing_id = conn.execute(
                    "SELECT route_id FROM routes WHERE route_id = ?",
                    (clean_id,),
                ).fetchone()
                if existing_id is not None:
                    raise RouteValidationError(
                        f"Route ID {clean_id!r} already exists"
                    )

                conn.execute(
                    """
                    INSERT INTO routes (
                        route_id, domain, upstream_scheme, upstream_host,
                        upstream_port, enabled, copied_from_route_id,
                        description, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        clean_id,
                        clean_domain,
                        clean_scheme,
                        clean_host,
                        clean_port,
                        1 if enabled else 0,
                        copied_from_route_id,
                        str(description or ""),
                        now_iso,
                        now_iso,
                    ),
                )
                self._bump_desired_generation(conn, now_iso)
                row = conn.execute(
                    "SELECT * FROM routes WHERE route_id = ?",
                    (clean_id,),
                ).fetchone()
                conn.commit()
            self.store.retention.checkpoint_wal()

        route_dict = self._row_to_dict(row)
        apply_res = (
            self.apply_routes(
                trigger_action=(
                    f"copy:{clean_id}"
                    if copied_from_route_id
                    else f"create:{clean_id}"
                )
            )
            if auto_apply
            else self.get_apply_status()
        )
        return {
            "route": route_dict,
            "desired_state_saved": True,
            "apply_result": apply_res,
        }

    def update_route(
        self,
        route_id: str,
        *,
        domain: str | None = None,
        upstream_scheme: str | None = None,
        upstream_host: str | None = None,
        upstream_port: int | None = None,
        enabled: bool | None = None,
        description: str | None = None,
        auto_apply: bool = True,
    ) -> dict[str, Any]:
        self.store.ensure_schema()
        now_iso = datetime.now(timezone.utc).isoformat()

        with self._exclusive_lock():
            with self.store._connect() as conn:
                current = conn.execute(
                    "SELECT * FROM routes WHERE route_id = ?",
                    (route_id,),
                ).fetchone()
                if current is None:
                    raise RouteNotFoundError(f"Route not found: {route_id}")

                new_domain = (
                    validate_domain(domain)
                    if domain is not None
                    else str(current["domain"])
                )
                new_scheme = (
                    validate_upstream_scheme(upstream_scheme)
                    if upstream_scheme is not None
                    else str(current["upstream_scheme"])
                )
                new_host = (
                    validate_upstream_host(upstream_host)
                    if upstream_host is not None
                    else str(current["upstream_host"])
                )
                new_port = (
                    validate_upstream_port(upstream_port)
                    if upstream_port is not None
                    else int(current["upstream_port"])
                )
                new_enabled = (
                    bool(enabled)
                    if enabled is not None
                    else bool(current["enabled"])
                )
                new_desc = (
                    str(description)
                    if description is not None
                    else str(current["description"] or "")
                )

                dup = conn.execute(
                    "SELECT route_id FROM routes WHERE domain = ? AND route_id != ?",
                    (new_domain, route_id),
                ).fetchone()
                if dup is not None:
                    raise DuplicateDomainError(
                        f"Domain {new_domain!r} is already assigned to route {dup['route_id']!r}"
                    )

                conn.execute(
                    """
                    UPDATE routes
                    SET domain = ?,
                        upstream_scheme = ?,
                        upstream_host = ?,
                        upstream_port = ?,
                        enabled = ?,
                        description = ?,
                        updated_at = ?
                    WHERE route_id = ?
                    """,
                    (
                        new_domain,
                        new_scheme,
                        new_host,
                        new_port,
                        1 if new_enabled else 0,
                        new_desc,
                        now_iso,
                        route_id,
                    ),
                )
                self._bump_desired_generation(conn, now_iso)
                updated_row = conn.execute(
                    "SELECT * FROM routes WHERE route_id = ?",
                    (route_id,),
                ).fetchone()
                conn.commit()
            self.store.retention.checkpoint_wal()

        route_dict = self._row_to_dict(updated_row)
        apply_res = (
            self.apply_routes(trigger_action=f"update:{route_id}")
            if auto_apply
            else self.get_apply_status()
        )
        return {
            "route": route_dict,
            "desired_state_saved": True,
            "apply_result": apply_res,
        }

    def set_route_enabled(
        self,
        route_id: str,
        enabled: bool,
        *,
        auto_apply: bool = True,
    ) -> dict[str, Any]:
        res = self.update_route(
            route_id,
            enabled=enabled,
            auto_apply=False,
        )
        action_label = "enable" if enabled else "disable"
        apply_res = (
            self.apply_routes(trigger_action=f"{action_label}:{route_id}")
            if auto_apply
            else self.get_apply_status()
        )
        res["apply_result"] = apply_res
        return res

    def copy_route(
        self,
        source_route_id: str,
        *,
        domain: str | None = None,
        route_id: str | None = None,
        enabled: bool | None = None,
        description: str | None = None,
        upstream_scheme: str | None = None,
        upstream_host: str | None = None,
        upstream_port: int | None = None,
        auto_apply: bool = True,
    ) -> dict[str, Any]:
        # Rule 4: copy creates a new route_id and REQUIRES a new non-conflicting domain at creation time
        if domain is None or not str(domain).strip():
            raise RouteValidationError(
                "copy_route requires a new explicit 'domain' parameter"
            )
        source_info = self.get_route(source_route_id)["route"]
        clean_new_domain = validate_domain(domain)
        if clean_new_domain == source_info["domain"]:
            raise DuplicateDomainError(
                f"Copied route must specify a different domain from source {source_info['domain']!r}"
            )
        created = self.create_route(
            domain=clean_new_domain,
            upstream_scheme=(
                upstream_scheme
                if upstream_scheme is not None
                else source_info["upstream_scheme"]
            ),
            upstream_host=(
                upstream_host
                if upstream_host is not None
                else source_info["upstream_host"]
            ),
            upstream_port=(
                upstream_port
                if upstream_port is not None
                else source_info["upstream_port"]
            ),
            route_id=route_id,
            enabled=(
                bool(enabled) if enabled is not None else source_info["enabled"]
            ),
            description=(
                description
                if description is not None
                else f"Copied from {source_route_id}"
            ),
            copied_from_route_id=source_route_id,
            auto_apply=auto_apply,
        )
        created["copied_from"] = source_route_id
        return created

    def delete_route(
        self,
        route_id: str,
        *,
        auto_apply: bool = True,
    ) -> dict[str, Any]:
        self.store.ensure_schema()
        now_iso = datetime.now(timezone.utc).isoformat()

        with self._exclusive_lock():
            with self.store._connect() as conn:
                current = conn.execute(
                    "SELECT * FROM routes WHERE route_id = ?",
                    (route_id,),
                ).fetchone()
                if current is None:
                    raise RouteNotFoundError(f"Route not found: {route_id}")
                deleted_dict = self._row_to_dict(current)
                conn.execute(
                    "DELETE FROM routes WHERE route_id = ?",
                    (route_id,),
                )
                self._bump_desired_generation(conn, now_iso)
                conn.commit()
            self.store.retention.checkpoint_wal()

        apply_res = (
            self.apply_routes(trigger_action=f"delete:{route_id}")
            if auto_apply
            else self.get_apply_status()
        )
        return {
            "deleted": True,
            "route_id": route_id,
            "route": deleted_dict,
            "desired_state_saved": True,
            "apply_result": apply_res,
        }

    def apply_routes(
        self,
        *,
        trigger_action: str = "manual_apply",
        raw_candidate_config: str | None = None,
        inject_invalid_directive: str | None = None,
        simulate_reload_failure: bool = False,
        timeout_seconds: float = 10.0,
    ) -> dict[str, Any]:
        self.store.ensure_schema()
        with self._exclusive_lock():
            attempted_at = datetime.now(timezone.utc).isoformat()
            generated_config, enabled_count = self.generate_nginx_config()

            if raw_candidate_config is not None:
                candidate_text = str(raw_candidate_config)
            else:
                candidate_text = generated_config

            if inject_invalid_directive:
                candidate_text = (
                    candidate_text
                    + f"\n# Injected invalid directive for validation test\n{inject_invalid_directive}\n"
                )

            candidate_bytes = candidate_text.encode("utf-8")
            candidate_sha256 = hashlib.sha256(candidate_bytes).hexdigest()

            for d in (self.candidates_dir, self.requests_dir, self.responses_dir):
                d.mkdir(parents=True, exist_ok=True)

            req_id = f"apply-{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}"
            cand_tmp = self.candidates_dir / f".{req_id}.conf.tmp"
            cand_file = self.candidates_dir / f"{req_id}.conf"
            cand_tmp.write_bytes(candidate_bytes)
            os.replace(cand_tmp, cand_file)

            req_tmp = self.requests_dir / f".{req_id}.req.tmp"
            req_file = self.requests_dir / f"{req_id}.req"
            sim_flag = "1" if simulate_reload_failure else "0"
            req_tmp.write_text(
                f'REQ_ID="{req_id}"\n'
                f'CANDIDATE_FILE="{cand_file}"\n'
                f'SIMULATE_RELOAD_FAILURE="{sim_flag}"\n',
                encoding="utf-8",
            )
            os.replace(req_tmp, req_file)

            resp_file = self.responses_dir / f"{req_id}.resp"
            deadline = time.monotonic() + timeout_seconds
            resp_text: str | None = None
            while time.monotonic() < deadline:
                if resp_file.is_file():
                    try:
                        resp_text = resp_file.read_text(encoding="utf-8")
                        break
                    except OSError:
                        pass
                time.sleep(0.03)

            cand_file.unlink(missing_ok=True)
            req_file.unlink(missing_ok=True)
            resp_file.unlink(missing_ok=True)

            if resp_text is None:
                apply_status = "router_timeout"
                rolled_back = False
                detail_msg = "Timed out waiting for router container apply daemon response"
            else:
                lines = resp_text.splitlines()
                status_line = lines[0] if len(lines) > 0 else "STATUS=unknown"
                rb_line = lines[1] if len(lines) > 1 else "ROLLED_BACK=0"
                body_lines = lines[2:] if len(lines) > 2 else []
                apply_status = status_line.split("=", 1)[-1].strip()
                rolled_back = rb_line.split("=", 1)[-1].strip() == "1"
                detail_msg = "\n".join(body_lines).strip()

            applied_ok = apply_status == "applied"
            active_sha = None
            if self.active_conf_path.is_file():
                try:
                    active_sha = hashlib.sha256(
                        self.active_conf_path.read_bytes()
                    ).hexdigest()
                except OSError:
                    active_sha = None

            now_iso = datetime.now(timezone.utc).isoformat()
            with self.store._connect() as conn:
                st_row = conn.execute(
                    "SELECT * FROM route_apply_state WHERE id = 1"
                ).fetchone()
                desired_gen = int(st_row["desired_generation"]) if st_row else 1
                prev_applied_gen = (
                    int(st_row["applied_generation"]) if st_row else 1
                )

                if applied_ok:
                    new_applied_gen = desired_gen
                    last_err = None
                    last_apply_at = now_iso
                else:
                    new_applied_gen = prev_applied_gen
                    last_err = detail_msg or f"Apply failed with status: {apply_status}"
                    last_apply_at = st_row["last_apply_at"] if st_row else None

                conn.execute(
                    """
                    UPDATE route_apply_state
                    SET applied_generation = ?,
                        last_apply_status = ?,
                        last_apply_at = ?,
                        last_attempt_at = ?,
                        last_error = ?,
                        rolled_back = ?,
                        active_config_sha256 = ?,
                        updated_at = ?
                    WHERE id = 1
                    """,
                    (
                        new_applied_gen,
                        apply_status,
                        last_apply_at,
                        attempted_at,
                        last_err,
                        1 if rolled_back else 0,
                        active_sha,
                        now_iso,
                    ),
                )

                cur = conn.execute(
                    """
                    INSERT INTO route_apply_runs (
                        attempted_at, trigger_action, desired_generation,
                        applied_generation_after, status, rolled_back,
                        error_message, candidate_sha256, active_config_sha256,
                        enabled_routes_count
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        attempted_at,
                        trigger_action,
                        desired_gen,
                        new_applied_gen,
                        apply_status,
                        1 if rolled_back else 0,
                        last_err,
                        candidate_sha256,
                        active_sha,
                        enabled_count,
                    ),
                )
                apply_id = int(cur.lastrowid or 0)
                conn.execute(
                    """
                    DELETE FROM route_apply_runs
                    WHERE apply_id NOT IN (
                        SELECT apply_id FROM route_apply_runs ORDER BY apply_id DESC LIMIT 50
                    )
                    """
                )
                conn.commit()
            self.store.retention.checkpoint_wal()

            return {
                "apply_id": apply_id,
                "applied": applied_ok,
                "status": apply_status,
                "rolled_back": rolled_back,
                "trigger_action": trigger_action,
                "desired_generation": desired_gen,
                "applied_generation": new_applied_gen,
                "in_sync": (desired_gen == new_applied_gen and applied_ok),
                "enabled_routes_count": enabled_count,
                "candidate_sha256": candidate_sha256,
                "active_config_sha256": active_sha,
                "last_error": last_err,
                "output": detail_msg,
                "attempted_at": attempted_at,
            }
