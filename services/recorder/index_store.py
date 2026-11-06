#!/usr/bin/env python3
"""Durable SQLite metadata index for HTTP Capture Contract v0 (DEV-010).

`captures/*.json` and `blobs/*.bin` are the authoritative raw records.
This SQLite index stores searchable metadata only (never raw body bytes) and can
be deleted and rebuilt idempotently at any time.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from typing import Any

from parser import build_derived_views
from retention import (
    DEFAULT_HARD_LIMIT_BYTES,
    DEFAULT_MIN_RETENTION_SECONDS,
    DEFAULT_PERIODIC_ENABLED,
    DEFAULT_PERIODIC_INTERVAL_SECONDS,
    DEFAULT_SOFT_LIMIT_BYTES,
    RetentionEngine,
)
from route_manager import RouteManager

INDEX_SCHEMA_VERSION = 3
MIGRATION_NAME = "001_capture_index.sql"
RETENTION_MIGRATION_NAME = "002_retention_policy.sql"
ROUTES_MIGRATION_NAME = "003_routes.sql"

DEFAULT_ROUTES_MIGRATION_SQL = """
CREATE TABLE IF NOT EXISTS routes (
    route_id TEXT PRIMARY KEY,
    domain TEXT NOT NULL UNIQUE,
    upstream_scheme TEXT NOT NULL,
    upstream_host TEXT NOT NULL,
    upstream_port INTEGER NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    copied_from_route_id TEXT,
    description TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_routes_domain
    ON routes (domain);

CREATE INDEX IF NOT EXISTS idx_routes_enabled
    ON routes (enabled, domain);

CREATE TABLE IF NOT EXISTS route_apply_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    seeded INTEGER NOT NULL DEFAULT 0,
    desired_generation INTEGER NOT NULL DEFAULT 1,
    applied_generation INTEGER NOT NULL DEFAULT 1,
    last_apply_status TEXT NOT NULL DEFAULT 'applied',
    last_apply_at TEXT,
    last_attempt_at TEXT,
    last_error TEXT,
    rolled_back INTEGER NOT NULL DEFAULT 0,
    active_config_sha256 TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS route_apply_runs (
    apply_id INTEGER PRIMARY KEY AUTOINCREMENT,
    attempted_at TEXT NOT NULL,
    trigger_action TEXT NOT NULL,
    desired_generation INTEGER NOT NULL,
    applied_generation_after INTEGER NOT NULL,
    status TEXT NOT NULL,
    rolled_back INTEGER NOT NULL DEFAULT 0,
    error_message TEXT,
    candidate_sha256 TEXT NOT NULL,
    active_config_sha256 TEXT,
    enabled_routes_count INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_route_apply_runs_id
    ON route_apply_runs (apply_id DESC);
"""

DEFAULT_RETENTION_MIGRATION_SQL = """
CREATE TABLE IF NOT EXISTS retention_policy (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    min_retention_seconds INTEGER NOT NULL DEFAULT 86400,
    soft_limit_bytes INTEGER NOT NULL DEFAULT 21474836480,
    hard_limit_bytes INTEGER NOT NULL DEFAULT 26843545600,
    periodic_interval_seconds REAL NOT NULL DEFAULT 60.0,
    periodic_enabled INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS retention_runs (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    ran_at TEXT NOT NULL,
    reference_now TEXT NOT NULL,
    trigger_source TEXT NOT NULL,
    dry_run INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    reasons_json TEXT NOT NULL,
    deleted_captures_count INTEGER NOT NULL,
    deleted_orphan_blobs_count INTEGER NOT NULL,
    deleted_corrupt_captures_count INTEGER NOT NULL,
    missing_blobs_count INTEGER NOT NULL,
    dangling_index_removed_count INTEGER NOT NULL DEFAULT 0,
    freed_bytes INTEGER NOT NULL,
    bytes_before INTEGER NOT NULL,
    bytes_after INTEGER NOT NULL,
    remaining_captures_count INTEGER NOT NULL,
    policy_snapshot_json TEXT NOT NULL,
    details_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_retention_runs_ran_at
    ON retention_runs (run_id DESC);
"""

DEFAULT_MIGRATION_SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS captures_index (
    capture_id TEXT PRIMARY KEY,
    schema_version TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    captured_at_unix_us INTEGER NOT NULL,
    route_id TEXT NOT NULL,
    method TEXT NOT NULL,
    scheme TEXT NOT NULL,
    authority TEXT NOT NULL,
    host TEXT NOT NULL,
    raw_target TEXT NOT NULL,
    raw_path TEXT NOT NULL,
    raw_query TEXT NOT NULL,
    http_version TEXT NOT NULL,
    capture_status TEXT NOT NULL,
    replayable INTEGER NOT NULL,
    is_websocket INTEGER NOT NULL,
    body_size INTEGER NOT NULL,
    captured_body_size INTEGER NOT NULL,
    body_sha256 TEXT NOT NULL,
    blob_ref TEXT,
    content_type TEXT,
    content_encoding TEXT,
    content_kind TEXT,
    response_status INTEGER,
    target_service TEXT,
    duration_ms REAL,
    proxy_error TEXT,
    has_parse_errors INTEGER NOT NULL DEFAULT 0,
    record_file TEXT NOT NULL,
    indexed_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_captures_sort_desc
    ON captures_index (captured_at_unix_us DESC, capture_id DESC);

CREATE INDEX IF NOT EXISTS idx_captures_sort_asc
    ON captures_index (captured_at_unix_us ASC, capture_id ASC);

CREATE INDEX IF NOT EXISTS idx_captures_host
    ON captures_index (host);

CREATE INDEX IF NOT EXISTS idx_captures_authority
    ON captures_index (authority);

CREATE INDEX IF NOT EXISTS idx_captures_method
    ON captures_index (method);

CREATE INDEX IF NOT EXISTS idx_captures_path
    ON captures_index (raw_path);

CREATE INDEX IF NOT EXISTS idx_captures_status
    ON captures_index (capture_status);

CREATE INDEX IF NOT EXISTS idx_captures_response_status
    ON captures_index (response_status);

CREATE TABLE IF NOT EXISTS capture_import_errors (
    record_file TEXT PRIMARY KEY,
    error_type TEXT NOT NULL,
    error_message TEXT NOT NULL,
    detected_at TEXT NOT NULL
);
"""


def parse_iso_to_unix_us(iso_str: str) -> int:
    s = iso_str.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return int(round(dt.timestamp() * 1_000_000))


class CaptureIndexStore:
    def __init__(
        self,
        storage_dir: Path | str | None = None,
        db_path: Path | str | None = None,
        migration_path: Path | str | None = None,
    ) -> None:
        self.storage_dir = Path(
            storage_dir
            or os.environ.get("CAPTURE_STORAGE_DIR", "/var/lib/proxyconsole")
        )
        self.captures_dir = self.storage_dir / "captures"
        self.blobs_dir = self.storage_dir / "blobs"
        self.db_dir = self.storage_dir / "database"
        self.db_path = (
            Path(db_path)
            if db_path
            else Path(
                os.environ.get(
                    "CAPTURE_INDEX_DB",
                    str(self.db_dir / "captures.sqlite3"),
                )
            )
        )
        self.migration_path = (
            Path(migration_path)
            if migration_path
            else Path(
                os.environ.get(
                    "CAPTURE_MIGRATION_FILE",
                    "/app/migrations/001_capture_index.sql",
                )
            )
        )
        self.retention_migration_path = Path(
            os.environ.get(
                "RETENTION_MIGRATION_FILE",
                "/app/migrations/002_retention_policy.sql",
            )
        )
        self.routes_migration_path = Path(
            os.environ.get(
                "ROUTES_MIGRATION_FILE",
                "/app/migrations/003_routes.sql",
            )
        )
        self.retention = RetentionEngine(self)
        self.routes = RouteManager(self)

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA busy_timeout=5000;")
        return conn

    def ensure_schema(self) -> None:
        sql_text_001 = DEFAULT_MIGRATION_SQL
        if self.migration_path.is_file():
            sql_text_001 = self.migration_path.read_text(encoding="utf-8")

        sql_text_002 = DEFAULT_RETENTION_MIGRATION_SQL
        if self.retention_migration_path.is_file():
            sql_text_002 = self.retention_migration_path.read_text(encoding="utf-8")

        sql_text_003 = DEFAULT_ROUTES_MIGRATION_SQL
        if self.routes_migration_path.is_file():
            sql_text_003 = self.routes_migration_path.read_text(encoding="utf-8")

        with self._connect() as conn:
            conn.executescript(sql_text_001)
            conn.executescript(sql_text_002)
            conn.executescript(sql_text_003)
            now_iso = datetime.now(timezone.utc).isoformat()
            conn.execute(
                """
                INSERT INTO schema_migrations (version, name, applied_at)
                VALUES (1, ?, ?)
                ON CONFLICT(version) DO NOTHING
                """,
                (MIGRATION_NAME, now_iso),
            )
            conn.execute(
                """
                INSERT INTO schema_migrations (version, name, applied_at)
                VALUES (2, ?, ?)
                ON CONFLICT(version) DO NOTHING
                """,
                (RETENTION_MIGRATION_NAME, now_iso),
            )
            conn.execute(
                """
                INSERT INTO schema_migrations (version, name, applied_at)
                VALUES (3, ?, ?)
                ON CONFLICT(version) DO NOTHING
                """,
                (ROUTES_MIGRATION_NAME, now_iso),
            )
            conn.execute(
                """
                INSERT INTO retention_policy (
                    id, min_retention_seconds, soft_limit_bytes,
                    hard_limit_bytes, periodic_interval_seconds,
                    periodic_enabled, updated_at
                ) VALUES (1, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO NOTHING
                """,
                (
                    DEFAULT_MIN_RETENTION_SECONDS,
                    DEFAULT_SOFT_LIMIT_BYTES,
                    DEFAULT_HARD_LIMIT_BYTES,
                    DEFAULT_PERIODIC_INTERVAL_SECONDS,
                    1 if DEFAULT_PERIODIC_ENABLED else 0,
                    now_iso,
                ),
            )
            conn.execute(
                """
                INSERT INTO route_apply_state (
                    id, seeded, desired_generation, applied_generation,
                    last_apply_status, last_apply_at, last_attempt_at,
                    last_error, rolled_back, active_config_sha256, updated_at
                ) VALUES (1, 0, 1, 1, 'applied', ?, ?, NULL, 0, NULL, ?)
                ON CONFLICT(id) DO NOTHING
                """,
                (now_iso, now_iso, now_iso),
            )
            st_row = conn.execute(
                "SELECT seeded FROM route_apply_state WHERE id = 1"
            ).fetchone()
            if st_row is not None and int(st_row["seeded"]) == 0:
                seed_routes = [
                    (
                        "route:app-a",
                        os.environ.get("APP_A_HOST", "app-a.local.test").strip().lower(),
                        "http",
                        "echo-a",
                        8080,
                        1,
                        None,
                        "Seed route for echo-a (DEV-001 compatibility)",
                        now_iso,
                        now_iso,
                    ),
                    (
                        "route:app-b",
                        os.environ.get("APP_B_HOST", "app-b.local.test").strip().lower(),
                        "http",
                        "echo-b",
                        8080,
                        1,
                        None,
                        "Seed route for echo-b (DEV-001 compatibility)",
                        now_iso,
                        now_iso,
                    ),
                ]
                conn.executemany(
                    """
                    INSERT INTO routes (
                        route_id, domain, upstream_scheme, upstream_host,
                        upstream_port, enabled, copied_from_route_id,
                        description, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(route_id) DO NOTHING
                    """,
                    seed_routes,
                )
                conn.execute(
                    "UPDATE route_apply_state SET seeded = 1 WHERE id = 1"
                )
            conn.commit()

    def _extract_row_params(
        self, record: dict[str, Any], record_file: str
    ) -> tuple[Any, ...]:
        capture_id = record["capture_id"]
        if not isinstance(capture_id, str) or not capture_id.strip():
            raise ValueError("Missing or invalid capture_id")

        captured_at = record["captured_at"]
        captured_at_us = parse_iso_to_unix_us(captured_at)

        req = record["request"]
        if not isinstance(req, dict):
            raise ValueError("Missing or invalid request object")

        body = req["body"]
        if not isinstance(body, dict):
            raise ValueError("Missing or invalid request.body object")

        authority = str(req.get("authority") or "")
        host = authority.split(":", 1)[0].strip().lower()
        forwarding = record.get("forwarding") or {}
        derived = record.get("derived_views") or {}
        parse_errors = derived.get("parse_errors") or []

        now_iso = datetime.now(timezone.utc).isoformat()

        return (
            capture_id,
            str(record.get("schema_version") or "v0"),
            captured_at,
            captured_at_us,
            str(record.get("route_id") or ""),
            str(req["method"]).upper(),
            str(req.get("scheme") or "https"),
            authority,
            host,
            str(req.get("raw_target") or req.get("raw_path") or "/"),
            str(req["raw_path"]),
            str(req.get("raw_query") or ""),
            str(req.get("http_version") or "HTTP/1.1"),
            str(record["capture_status"]),
            1 if record.get("replayable") else 0,
            1 if record.get("is_websocket") else 0,
            int(body["size"]),
            int(body.get("captured_size", body["size"])),
            str(body["sha256"]),
            body.get("blob_ref"),
            body.get("content_type"),
            body.get("content_encoding"),
            derived.get("content_kind"),
            forwarding.get("response_status"),
            forwarding.get("target"),
            forwarding.get("duration_ms"),
            forwarding.get("proxy_error"),
            1 if len(parse_errors) > 0 else 0,
            record_file,
            now_iso,
        )

    def index_record_dict(
        self, record: dict[str, Any], record_file: str | None = None
    ) -> None:
        self.ensure_schema()
        rel_file = record_file or f"captures/{record.get('capture_id', 'unknown')}.json"
        params = self._extract_row_params(record, rel_file)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO captures_index (
                    capture_id, schema_version, captured_at, captured_at_unix_us,
                    route_id, method, scheme, authority, host, raw_target,
                    raw_path, raw_query, http_version, capture_status,
                    replayable, is_websocket, body_size, captured_body_size,
                    body_sha256, blob_ref, content_type, content_encoding,
                    content_kind, response_status, target_service, duration_ms,
                    proxy_error, has_parse_errors, record_file, indexed_at
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                ON CONFLICT(capture_id) DO UPDATE SET
                    schema_version = excluded.schema_version,
                    captured_at = excluded.captured_at,
                    captured_at_unix_us = excluded.captured_at_unix_us,
                    route_id = excluded.route_id,
                    method = excluded.method,
                    scheme = excluded.scheme,
                    authority = excluded.authority,
                    host = excluded.host,
                    raw_target = excluded.raw_target,
                    raw_path = excluded.raw_path,
                    raw_query = excluded.raw_query,
                    http_version = excluded.http_version,
                    capture_status = excluded.capture_status,
                    replayable = excluded.replayable,
                    is_websocket = excluded.is_websocket,
                    body_size = excluded.body_size,
                    captured_body_size = excluded.captured_body_size,
                    body_sha256 = excluded.body_sha256,
                    blob_ref = excluded.blob_ref,
                    content_type = excluded.content_type,
                    content_encoding = excluded.content_encoding,
                    content_kind = excluded.content_kind,
                    response_status = excluded.response_status,
                    target_service = excluded.target_service,
                    duration_ms = excluded.duration_ms,
                    proxy_error = excluded.proxy_error,
                    has_parse_errors = excluded.has_parse_errors,
                    record_file = excluded.record_file,
                    indexed_at = excluded.indexed_at
                """,
                params,
            )
            conn.execute(
                "DELETE FROM capture_import_errors WHERE record_file = ?",
                (rel_file,),
            )
            conn.commit()

    def import_all_captures(self) -> dict[str, Any]:
        self.ensure_schema()
        self.captures_dir.mkdir(parents=True, exist_ok=True)

        imported_count = 0
        errors: list[dict[str, str]] = []
        seen_files: set[str] = set()

        with self._connect() as conn:
            for path in sorted(self.captures_dir.glob("*.json")):
                rel_file = f"captures/{path.name}"
                seen_files.add(rel_file)
                try:
                    raw_text = path.read_text(encoding="utf-8")
                    rec = json.loads(raw_text)
                    if not isinstance(rec, dict):
                        raise ValueError("Root JSON value must be an object")
                    params = self._extract_row_params(rec, rel_file)
                    conn.execute(
                        """
                        INSERT INTO captures_index (
                            capture_id, schema_version, captured_at, captured_at_unix_us,
                            route_id, method, scheme, authority, host, raw_target,
                            raw_path, raw_query, http_version, capture_status,
                            replayable, is_websocket, body_size, captured_body_size,
                            body_sha256, blob_ref, content_type, content_encoding,
                            content_kind, response_status, target_service, duration_ms,
                            proxy_error, has_parse_errors, record_file, indexed_at
                        ) VALUES (
                            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                        )
                        ON CONFLICT(capture_id) DO UPDATE SET
                            schema_version = excluded.schema_version,
                            captured_at = excluded.captured_at,
                            captured_at_unix_us = excluded.captured_at_unix_us,
                            route_id = excluded.route_id,
                            method = excluded.method,
                            scheme = excluded.scheme,
                            authority = excluded.authority,
                            host = excluded.host,
                            raw_target = excluded.raw_target,
                            raw_path = excluded.raw_path,
                            raw_query = excluded.raw_query,
                            http_version = excluded.http_version,
                            capture_status = excluded.capture_status,
                            replayable = excluded.replayable,
                            is_websocket = excluded.is_websocket,
                            body_size = excluded.body_size,
                            captured_body_size = excluded.captured_body_size,
                            body_sha256 = excluded.body_sha256,
                            blob_ref = excluded.blob_ref,
                            content_type = excluded.content_type,
                            content_encoding = excluded.content_encoding,
                            content_kind = excluded.content_kind,
                            response_status = excluded.response_status,
                            target_service = excluded.target_service,
                            duration_ms = excluded.duration_ms,
                            proxy_error = excluded.proxy_error,
                            has_parse_errors = excluded.has_parse_errors,
                            record_file = excluded.record_file,
                            indexed_at = excluded.indexed_at
                        """,
                        params,
                    )
                    conn.execute(
                        "DELETE FROM capture_import_errors WHERE record_file = ?",
                        (rel_file,),
                    )
                    imported_count += 1
                except Exception as exc:
                    err_entry = {
                        "record_file": rel_file,
                        "error_type": type(exc).__name__,
                        "error_message": str(exc),
                        "detected_at": datetime.now(timezone.utc).isoformat(),
                    }
                    errors.append(err_entry)
                    conn.execute(
                        """
                        INSERT INTO capture_import_errors (
                            record_file, error_type, error_message, detected_at
                        ) VALUES (?, ?, ?, ?)
                        ON CONFLICT(record_file) DO UPDATE SET
                            error_type = excluded.error_type,
                            error_message = excluded.error_message,
                            detected_at = excluded.detected_at
                        """,
                        (
                            err_entry["record_file"],
                            err_entry["error_type"],
                            err_entry["error_message"],
                            err_entry["detected_at"],
                        ),
                    )

            # Prune error records and index rows for files that were removed from disk
            existing_err_rows = conn.execute(
                "SELECT record_file FROM capture_import_errors"
            ).fetchall()
            for r in existing_err_rows:
                if r["record_file"] not in seen_files:
                    conn.execute(
                        "DELETE FROM capture_import_errors WHERE record_file = ?",
                        (r["record_file"],),
                    )

            existing_idx_rows = conn.execute(
                "SELECT capture_id, record_file FROM captures_index"
            ).fetchall()
            for r in existing_idx_rows:
                if r["record_file"] not in seen_files:
                    conn.execute(
                        "DELETE FROM captures_index WHERE capture_id = ?",
                        (r["capture_id"],),
                    )

            total_indexed = conn.execute(
                "SELECT COUNT(*) AS cnt FROM captures_index"
            ).fetchone()["cnt"]
            all_err_rows = conn.execute(
                "SELECT record_file, error_type, error_message, detected_at "
                "FROM capture_import_errors ORDER BY record_file ASC"
            ).fetchall()
            conn.commit()

        return {
            "schema_version": INDEX_SCHEMA_VERSION,
            "imported_count": imported_count,
            "total_indexed": total_indexed,
            "error_count": len(all_err_rows),
            "import_errors": [dict(r) for r in all_err_rows],
        }

    def rebuild_index(self) -> dict[str, Any]:
        saved_policy = None
        saved_runs: list[dict[str, Any]] = []
        saved_routes: list[dict[str, Any]] = []
        saved_route_state = None
        saved_route_runs: list[dict[str, Any]] = []
        if self.db_path.is_file():
            try:
                with self._connect() as conn:
                    pol_row = conn.execute(
                        "SELECT * FROM retention_policy WHERE id = 1"
                    ).fetchone()
                    if pol_row is not None:
                        saved_policy = dict(pol_row)
                    run_rows = conn.execute(
                        "SELECT * FROM retention_runs ORDER BY run_id ASC"
                    ).fetchall()
                    saved_runs = [dict(r) for r in run_rows]
                    route_rows = conn.execute(
                        "SELECT * FROM routes ORDER BY route_id ASC"
                    ).fetchall()
                    saved_routes = [dict(r) for r in route_rows]
                    rs_row = conn.execute(
                        "SELECT * FROM route_apply_state WHERE id = 1"
                    ).fetchone()
                    if rs_row is not None:
                        saved_route_state = dict(rs_row)
                    rr_rows = conn.execute(
                        "SELECT * FROM route_apply_runs ORDER BY apply_id ASC"
                    ).fetchall()
                    saved_route_runs = [dict(r) for r in rr_rows]
            except Exception:
                pass

        for suffix in ("", "-wal", "-shm"):
            p = Path(str(self.db_path) + suffix)
            if p.exists():
                p.unlink()
        self.ensure_schema()

        if (
            saved_policy is not None
            or saved_runs
            or saved_route_state is not None
            or saved_routes
            or saved_route_runs
        ):
            with self._connect() as conn:
                if saved_policy is not None:
                    conn.execute(
                        """
                        UPDATE retention_policy
                        SET min_retention_seconds = ?,
                            soft_limit_bytes = ?,
                            hard_limit_bytes = ?,
                            periodic_interval_seconds = ?,
                            periodic_enabled = ?,
                            updated_at = ?
                        WHERE id = 1
                        """,
                        (
                            saved_policy["min_retention_seconds"],
                            saved_policy["soft_limit_bytes"],
                            saved_policy["hard_limit_bytes"],
                            saved_policy["periodic_interval_seconds"],
                            saved_policy["periodic_enabled"],
                            saved_policy["updated_at"],
                        ),
                    )
                for sr in saved_runs:
                    conn.execute(
                        """
                        INSERT INTO retention_runs (
                            run_id, ran_at, reference_now, trigger_source, dry_run,
                            status, reasons_json, deleted_captures_count,
                            deleted_orphan_blobs_count, deleted_corrupt_captures_count,
                            missing_blobs_count, dangling_index_removed_count,
                            freed_bytes, bytes_before, bytes_after,
                            remaining_captures_count, policy_snapshot_json, details_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            sr["run_id"],
                            sr["ran_at"],
                            sr["reference_now"],
                            sr["trigger_source"],
                            sr["dry_run"],
                            sr["status"],
                            sr["reasons_json"],
                            sr["deleted_captures_count"],
                            sr["deleted_orphan_blobs_count"],
                            sr["deleted_corrupt_captures_count"],
                            sr["missing_blobs_count"],
                            sr["dangling_index_removed_count"],
                            sr["freed_bytes"],
                            sr["bytes_before"],
                            sr["bytes_after"],
                            sr["remaining_captures_count"],
                            sr["policy_snapshot_json"],
                            sr["details_json"],
                        ),
                    )
                if saved_route_state is not None:
                    conn.execute("DELETE FROM routes")
                    for rt in saved_routes:
                        conn.execute(
                            """
                            INSERT INTO routes (
                                route_id, domain, upstream_scheme, upstream_host,
                                upstream_port, enabled, copied_from_route_id,
                                description, created_at, updated_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                rt["route_id"],
                                rt["domain"],
                                rt["upstream_scheme"],
                                rt["upstream_host"],
                                rt["upstream_port"],
                                rt["enabled"],
                                rt["copied_from_route_id"],
                                rt["description"],
                                rt["created_at"],
                                rt["updated_at"],
                            ),
                        )
                    conn.execute(
                        """
                        UPDATE route_apply_state
                        SET seeded = ?,
                            desired_generation = ?,
                            applied_generation = ?,
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
                            saved_route_state["seeded"],
                            saved_route_state["desired_generation"],
                            saved_route_state["applied_generation"],
                            saved_route_state["last_apply_status"],
                            saved_route_state["last_apply_at"],
                            saved_route_state["last_attempt_at"],
                            saved_route_state["last_error"],
                            saved_route_state["rolled_back"],
                            saved_route_state["active_config_sha256"],
                            saved_route_state["updated_at"],
                        ),
                    )
                for rr in saved_route_runs:
                    conn.execute(
                        """
                        INSERT INTO route_apply_runs (
                            apply_id, attempted_at, trigger_action,
                            desired_generation, applied_generation_after,
                            status, rolled_back, error_message,
                            candidate_sha256, active_config_sha256, enabled_routes_count
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            rr["apply_id"],
                            rr["attempted_at"],
                            rr["trigger_action"],
                            rr["desired_generation"],
                            rr["applied_generation_after"],
                            rr["status"],
                            rr["rolled_back"],
                            rr["error_message"],
                            rr["candidate_sha256"],
                            rr["active_config_sha256"],
                            rr["enabled_routes_count"],
                        ),
                    )
                conn.commit()

        result = self.import_all_captures()
        result["rebuilt"] = True
        return result

    def get_index_status(self) -> dict[str, Any]:
        self.ensure_schema()
        raw_files = (
            len(list(self.captures_dir.glob("*.json")))
            if self.captures_dir.is_dir()
            else 0
        )
        with self._connect() as conn:
            total_indexed = conn.execute(
                "SELECT COUNT(*) AS cnt FROM captures_index"
            ).fetchone()["cnt"]
            err_rows = conn.execute(
                "SELECT record_file, error_type, error_message, detected_at "
                "FROM capture_import_errors ORDER BY record_file ASC"
            ).fetchall()
            migrations = conn.execute(
                "SELECT version, name, applied_at FROM schema_migrations ORDER BY version ASC"
            ).fetchall()
        return {
            "schema_version": INDEX_SCHEMA_VERSION,
            "db_path": str(self.db_path),
            "raw_capture_files": raw_files,
            "total_indexed": total_indexed,
            "import_errors_count": len(err_rows),
            "import_errors": [dict(r) for r in err_rows],
            "migrations": [dict(m) for m in migrations],
        }

    def query_captures(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        sort: str = "desc",
        from_time: str | None = None,
        to_time: str | None = None,
        host: str | None = None,
        authority: str | None = None,
        method: str | None = None,
        path: str | None = None,
        path_prefix: str | None = None,
        capture_status: str | None = None,
        response_status: int | None = None,
    ) -> dict[str, Any]:
        self.ensure_schema()

        where_clauses: list[str] = []
        params: list[Any] = []

        if from_time:
            where_clauses.append("captured_at_unix_us >= ?")
            params.append(parse_iso_to_unix_us(from_time))

        if to_time:
            where_clauses.append("captured_at_unix_us <= ?")
            params.append(parse_iso_to_unix_us(to_time))

        if host:
            host_clean = host.strip()
            host_only = host_clean.split(":", 1)[0].lower()
            if ":" in host_clean:
                where_clauses.append("(LOWER(authority) = LOWER(?) OR host = ?)")
                params.extend([host_clean, host_only])
            else:
                where_clauses.append("host = ?")
                params.append(host_only)

        if authority:
            auth_clean = authority.strip()
            if ":" in auth_clean:
                where_clauses.append("LOWER(authority) = LOWER(?)")
                params.append(auth_clean)
            else:
                where_clauses.append("(LOWER(authority) = LOWER(?) OR host = LOWER(?))")
                params.extend([auth_clean, auth_clean])

        if method:
            where_clauses.append("UPPER(method) = UPPER(?)")
            params.append(method.strip())

        if path:
            where_clauses.append("raw_path = ?")
            params.append(path.strip())

        if path_prefix:
            where_clauses.append("raw_path LIKE ?")
            params.append(path_prefix.strip() + "%")

        if capture_status:
            where_clauses.append("capture_status = ?")
            params.append(capture_status.strip())

        if response_status is not None:
            where_clauses.append("response_status = ?")
            params.append(int(response_status))

        where_sql = (
            "WHERE " + " AND ".join(where_clauses) if where_clauses else ""
        )
        sort_dir = "ASC" if str(sort).lower() == "asc" else "DESC"
        order_sql = f"ORDER BY captured_at_unix_us {sort_dir}, capture_id {sort_dir}"

        safe_limit = max(1, min(int(limit), 1000))
        safe_offset = max(0, int(offset))

        with self._connect() as conn:
            total = conn.execute(
                f"SELECT COUNT(*) AS cnt FROM captures_index {where_sql}",
                params,
            ).fetchone()["cnt"]

            rows = conn.execute(
                f"""
                SELECT
                    capture_id, schema_version, captured_at, captured_at_unix_us,
                    route_id, method, scheme, authority, host, raw_target,
                    raw_path, raw_query, http_version, capture_status,
                    replayable, is_websocket, body_size, captured_body_size,
                    body_sha256, blob_ref, content_type, content_encoding,
                    content_kind, response_status, target_service, duration_ms,
                    proxy_error, has_parse_errors, record_file, indexed_at
                FROM captures_index
                {where_sql}
                {order_sql}
                LIMIT ? OFFSET ?
                """,
                [*params, safe_limit, safe_offset],
            ).fetchall()

            err_rows = conn.execute(
                "SELECT record_file, error_type, error_message, detected_at "
                "FROM capture_import_errors ORDER BY record_file ASC"
            ).fetchall()

        items: list[dict[str, Any]] = []
        for r in rows:
            items.append(
                {
                    "capture_id": r["capture_id"],
                    "schema_version": r["schema_version"],
                    "captured_at": r["captured_at"],
                    "route_id": r["route_id"],
                    "method": r["method"],
                    "scheme": r["scheme"],
                    "authority": r["authority"],
                    "host": r["host"],
                    "raw_target": r["raw_target"],
                    "raw_path": r["raw_path"],
                    "raw_query": r["raw_query"],
                    "http_version": r["http_version"],
                    "capture_status": r["capture_status"],
                    "replayable": bool(r["replayable"]),
                    "is_websocket": bool(r["is_websocket"]),
                    "body_size": r["body_size"],
                    "captured_body_size": r["captured_body_size"],
                    "body_sha256": r["body_sha256"],
                    "blob_ref": r["blob_ref"],
                    "content_type": r["content_type"],
                    "content_encoding": r["content_encoding"],
                    "content_kind": r["content_kind"],
                    "response_status": r["response_status"],
                    "target": r["target_service"],
                    "duration_ms": r["duration_ms"],
                    "proxy_error": r["proxy_error"],
                    "has_parse_errors": bool(r["has_parse_errors"]),
                    "record_file": r["record_file"],
                }
            )

        return {
            "total": total,
            "limit": safe_limit,
            "offset": safe_offset,
            "sort": sort_dir.lower(),
            "filters": {
                "from_time": from_time,
                "to_time": to_time,
                "host": host,
                "authority": authority,
                "method": method,
                "path": path,
                "path_prefix": path_prefix,
                "capture_status": capture_status,
                "response_status": response_status,
            },
            "items": items,
            "import_errors_count": len(err_rows),
            "import_errors": [dict(e) for e in err_rows],
        }

    def get_capture_detail(self, capture_id: str) -> dict[str, Any]:
        self.ensure_schema()
        with self._connect() as conn:
            idx_row = conn.execute(
                "SELECT * FROM captures_index WHERE capture_id = ?",
                (capture_id,),
            ).fetchone()

        record_path = self.captures_dir / f"{capture_id}.json"
        if not record_path.is_file() and idx_row is not None:
            record_path = self.storage_dir / idx_row["record_file"]

        if not record_path.is_file():
            raise FileNotFoundError(f"Capture record not found: {capture_id}")

        record = json.loads(record_path.read_text(encoding="utf-8"))
        req = record["request"]
        body_meta = req["body"]
        blob_ref = body_meta.get("blob_ref")

        blob_bytes = b""
        if blob_ref:
            blob_path = self.storage_dir / blob_ref
            if not blob_path.is_file():
                raise FileNotFoundError(f"Blob file not found: {blob_path}")
            blob_bytes = blob_path.read_bytes()

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
        out["indexed"] = idx_row is not None
        out["derived_views"] = regenerated_views
        out["regenerated_derived_views"] = regenerated_views
        out["reader_verification"] = {
            "blob_bytes_len": len(blob_bytes),
            "blob_sha256": blob_sha256,
            "captured_size_matches": captured_size_matches,
            "full_body_sha256_matches": sha256_matches,
        }
        return out

    def get_retention_policy(self) -> dict[str, Any]:
        return self.retention.get_policy()

    def update_retention_policy(
        self,
        *,
        min_retention_seconds: int | None = None,
        soft_limit_bytes: int | None = None,
        hard_limit_bytes: int | None = None,
        periodic_interval_seconds: float | None = None,
        periodic_enabled: bool | None = None,
    ) -> dict[str, Any]:
        return self.retention.update_policy(
            min_retention_seconds=min_retention_seconds,
            soft_limit_bytes=soft_limit_bytes,
            hard_limit_bytes=hard_limit_bytes,
            periodic_interval_seconds=periodic_interval_seconds,
            periodic_enabled=periodic_enabled,
        )

    def get_retention_status(self, *, runs_limit: int = 20) -> dict[str, Any]:
        return self.retention.get_status(runs_limit=runs_limit)

    def run_retention(
        self,
        *,
        dry_run: bool = False,
        now: str | None = None,
        trigger_source: str = "manual",
    ) -> dict[str, Any]:
        return self.retention.run_cleanup(
            dry_run=dry_run,
            now=now,
            trigger_source=trigger_source,
        )
