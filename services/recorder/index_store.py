#!/usr/bin/env python3
"""Durable SQLite metadata index for HTTP Capture Contract v0 (DEV-010).

`captures/*.json` and `blobs/*.bin` are the authoritative raw records.
This SQLite index stores searchable metadata only (never raw body bytes) and can
be deleted and rebuilt idempotently at any time.
"""

from __future__ import annotations

import contextlib
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from typing import Any, Iterator

from docker_discovery import DockerDiscoveryService
from draft_replay import DraftReplayManager, decode_path_human_readable
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
from rule_engine import RuleEngineManager

INDEX_SCHEMA_VERSION = 6
MIGRATION_NAME = "001_capture_index.sql"
RETENTION_MIGRATION_NAME = "002_retention_policy.sql"
ROUTES_MIGRATION_NAME = "003_routes.sql"
DISCOVERY_MIGRATION_NAME = "004_discovery.sql"
DRAFTS_MIGRATION_NAME = "005_request_drafts.sql"
RULES_MIGRATION_NAME = "006_modification_rules.sql"

DEFAULT_RULES_MIGRATION_SQL = """
CREATE TABLE IF NOT EXISTS modification_rules (
    rule_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 1,
    priority INTEGER NOT NULL DEFAULT 100,
    match_json TEXT NOT NULL DEFAULT '{}',
    actions_json TEXT NOT NULL DEFAULT '[]',
    copied_from_rule_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_modification_rules_order
    ON modification_rules (enabled DESC, priority ASC, created_at ASC, rule_id ASC);
"""

DEFAULT_DRAFTS_MIGRATION_SQL = """
CREATE TABLE IF NOT EXISTS request_drafts (
    draft_id TEXT PRIMARY KEY,
    source_capture_id TEXT NOT NULL,
    method TEXT NOT NULL,
    scheme TEXT NOT NULL,
    authority TEXT NOT NULL,
    host TEXT NOT NULL,
    raw_path TEXT NOT NULL,
    decoded_path TEXT NOT NULL,
    raw_query TEXT NOT NULL,
    query_entries_json TEXT NOT NULL,
    http_version TEXT NOT NULL DEFAULT 'HTTP/1.1',
    headers_json TEXT NOT NULL,
    cookies_json TEXT NOT NULL,
    body_mode TEXT NOT NULL,
    content_type TEXT,
    body_base64 TEXT NOT NULL DEFAULT '',
    body_text TEXT,
    json_value_json TEXT,
    form_fields_json TEXT,
    multipart_view_json TEXT,
    parse_errors_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    expires_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_request_drafts_source_capture
    ON request_drafts (source_capture_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_request_drafts_expires_at
    ON request_drafts (expires_at);

CREATE TABLE IF NOT EXISTS replay_runs (
    replay_id TEXT PRIMARY KEY,
    draft_id TEXT,
    source_capture_id TEXT NOT NULL,
    result_capture_id TEXT,
    replayed_at TEXT NOT NULL,
    method TEXT NOT NULL,
    scheme TEXT NOT NULL,
    authority TEXT NOT NULL,
    raw_target TEXT NOT NULL,
    request_body_size INTEGER NOT NULL,
    request_body_sha256 TEXT NOT NULL,
    response_status INTEGER,
    duration_ms REAL,
    side_effect_warning_json TEXT NOT NULL,
    error_message TEXT
);

CREATE INDEX IF NOT EXISTS idx_replay_runs_draft_id
    ON replay_runs (draft_id, replayed_at DESC);

CREATE INDEX IF NOT EXISTS idx_replay_runs_source_capture
    ON replay_runs (source_capture_id, replayed_at DESC);

CREATE INDEX IF NOT EXISTS idx_replay_runs_result_capture
    ON replay_runs (result_capture_id);
"""

DEFAULT_DISCOVERY_MIGRATION_SQL = """
CREATE TABLE IF NOT EXISTS discovery_status (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    available INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'uninitialized',
    last_refresh_at TEXT,
    last_success_at TEXT,
    last_error TEXT,
    containers_count INTEGER NOT NULL DEFAULT 0,
    running_containers_count INTEGER NOT NULL DEFAULT 0,
    targets_count INTEGER NOT NULL DEFAULT 0,
    socket_path_override TEXT,
    updated_at TEXT NOT NULL
);
"""

DEFAULT_ROUTES_MIGRATION_SQL = """
CREATE TABLE IF NOT EXISTS routes (
    route_id TEXT PRIMARY KEY,
    domain TEXT NOT NULL UNIQUE,
    upstream_scheme TEXT NOT NULL,
    upstream_host TEXT NOT NULL,
    upstream_port INTEGER NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    copied_from_route_id TEXT,
    target_metadata_json TEXT,
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
        self.discovery_migration_path = Path(
            os.environ.get(
                "DISCOVERY_MIGRATION_FILE",
                "/app/migrations/004_discovery.sql",
            )
        )
        self.drafts_migration_path = Path(
            os.environ.get(
                "DRAFTS_MIGRATION_FILE",
                "/app/migrations/005_request_drafts.sql",
            )
        )
        self.rules_migration_path = Path(
            os.environ.get(
                "RULES_MIGRATION_FILE",
                "/app/migrations/006_modification_rules.sql",
            )
        )
        self.retention = RetentionEngine(self)
        self.routes = RouteManager(self)
        self.discovery = DockerDiscoveryService(self)
        self.drafts = DraftReplayManager(self)
        self.rules = RuleEngineManager(self)

    @contextlib.contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA synchronous=NORMAL;")
            conn.execute("PRAGMA busy_timeout=5000;")
            with conn:
                yield conn
        finally:
            conn.close()

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

        sql_text_004 = DEFAULT_DISCOVERY_MIGRATION_SQL
        if self.discovery_migration_path.is_file():
            sql_text_004 = self.discovery_migration_path.read_text(encoding="utf-8")

        sql_text_005 = DEFAULT_DRAFTS_MIGRATION_SQL
        if self.drafts_migration_path.is_file():
            sql_text_005 = self.drafts_migration_path.read_text(encoding="utf-8")

        sql_text_006 = DEFAULT_RULES_MIGRATION_SQL
        if self.rules_migration_path.is_file():
            sql_text_006 = self.rules_migration_path.read_text(encoding="utf-8")

        with self._connect() as conn:
            conn.executescript(sql_text_001)
            conn.executescript(sql_text_002)
            conn.executescript(sql_text_003)
            conn.executescript(sql_text_004)
            conn.executescript(sql_text_005)
            conn.executescript(sql_text_006)
            route_cols = {
                r["name"]
                for r in conn.execute("PRAGMA table_info(routes)").fetchall()
            }
            if "target_metadata_json" not in route_cols:
                conn.execute(
                    "ALTER TABLE routes ADD COLUMN target_metadata_json TEXT"
                )
            cap_cols = {
                r["name"]
                for r in conn.execute("PRAGMA table_info(captures_index)").fetchall()
            }
            for col_name, col_def in (
                ("rule_modified", "INTEGER NOT NULL DEFAULT 0"),
                ("rule_failed", "INTEGER NOT NULL DEFAULT 0"),
                ("matched_rule_ids_json", "TEXT NOT NULL DEFAULT '[]'"),
                ("applied_rule_ids_json", "TEXT NOT NULL DEFAULT '[]'"),
                ("skipped_rule_ids_json", "TEXT NOT NULL DEFAULT '[]'"),
                ("failed_rule_ids_json", "TEXT NOT NULL DEFAULT '[]'"),
                ("rule_evaluation_json", "TEXT"),
            ):
                if col_name not in cap_cols:
                    conn.execute(
                        f"ALTER TABLE captures_index ADD COLUMN {col_name} {col_def}"
                    )
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
                INSERT INTO schema_migrations (version, name, applied_at)
                VALUES (4, ?, ?)
                ON CONFLICT(version) DO NOTHING
                """,
                (DISCOVERY_MIGRATION_NAME, now_iso),
            )
            conn.execute(
                """
                INSERT INTO schema_migrations (version, name, applied_at)
                VALUES (5, ?, ?)
                ON CONFLICT(version) DO NOTHING
                """,
                (DRAFTS_MIGRATION_NAME, now_iso),
            )
            conn.execute(
                """
                INSERT INTO schema_migrations (version, name, applied_at)
                VALUES (6, ?, ?)
                ON CONFLICT(version) DO NOTHING
                """,
                (RULES_MIGRATION_NAME, now_iso),
            )
            conn.execute(
                """
                INSERT INTO discovery_status (
                    id, available, status, last_refresh_at, last_success_at,
                    last_error, containers_count, running_containers_count,
                    targets_count, socket_path_override, updated_at
                ) VALUES (1, 1, 'uninitialized', NULL, NULL, NULL, 0, 0, 0, NULL, ?)
                ON CONFLICT(id) DO NOTHING
                """,
                (now_iso,),
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
        rule_eval = record.get("rule_evaluation") or {}
        matched_ids = rule_eval.get("matched_rule_ids") or []
        applied_ids = rule_eval.get("applied_rule_ids") or []
        skipped_ids = rule_eval.get("skipped_rule_ids") or []
        failed_ids = rule_eval.get("failed_rule_ids") or []
        rule_modified = 1 if (rule_eval.get("modified") or len(applied_ids) > 0) else 0
        rule_failed = 1 if (rule_eval.get("has_failures") or len(failed_ids) > 0) else 0

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
            rule_modified,
            rule_failed,
            json.dumps(matched_ids, ensure_ascii=False),
            json.dumps(applied_ids, ensure_ascii=False),
            json.dumps(skipped_ids, ensure_ascii=False),
            json.dumps(failed_ids, ensure_ascii=False),
            json.dumps(rule_eval, ensure_ascii=False) if rule_eval else None,
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
                    proxy_error, has_parse_errors, rule_modified, rule_failed,
                    matched_rule_ids_json, applied_rule_ids_json,
                    skipped_rule_ids_json, failed_rule_ids_json,
                    rule_evaluation_json, record_file, indexed_at
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?
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
                    rule_modified = excluded.rule_modified,
                    rule_failed = excluded.rule_failed,
                    matched_rule_ids_json = excluded.matched_rule_ids_json,
                    applied_rule_ids_json = excluded.applied_rule_ids_json,
                    skipped_rule_ids_json = excluded.skipped_rule_ids_json,
                    failed_rule_ids_json = excluded.failed_rule_ids_json,
                    rule_evaluation_json = excluded.rule_evaluation_json,
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
                            proxy_error, has_parse_errors, rule_modified, rule_failed,
                            matched_rule_ids_json, applied_rule_ids_json,
                            skipped_rule_ids_json, failed_rule_ids_json,
                            rule_evaluation_json, record_file, indexed_at
                        ) VALUES (
                            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                            ?, ?, ?, ?, ?, ?, ?
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
                            rule_modified = excluded.rule_modified,
                            rule_failed = excluded.rule_failed,
                            matched_rule_ids_json = excluded.matched_rule_ids_json,
                            applied_rule_ids_json = excluded.applied_rule_ids_json,
                            skipped_rule_ids_json = excluded.skipped_rule_ids_json,
                            failed_rule_ids_json = excluded.failed_rule_ids_json,
                            rule_evaluation_json = excluded.rule_evaluation_json,
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
        if self.db_path.is_file():
            try:
                self.ensure_schema()
                with self._connect() as conn:
                    conn.execute("DELETE FROM captures_index")
                    conn.execute("DELETE FROM capture_import_errors")
                res = self.import_all_captures()
                res["rebuilt"] = True
                return res
            except sqlite3.DatabaseError:
                pass

        saved_policy = None
        saved_runs: list[dict[str, Any]] = []
        saved_routes: list[dict[str, Any]] = []
        saved_route_state = None
        saved_route_runs: list[dict[str, Any]] = []
        saved_drafts: list[dict[str, Any]] = []
        saved_replay_runs: list[dict[str, Any]] = []
        saved_rules: list[dict[str, Any]] = []
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
                    dr_rows = conn.execute(
                        "SELECT * FROM request_drafts ORDER BY created_at ASC"
                    ).fetchall()
                    saved_drafts = [dict(r) for r in dr_rows]
                    rpl_rows = conn.execute(
                        "SELECT * FROM replay_runs ORDER BY replayed_at ASC"
                    ).fetchall()
                    saved_replay_runs = [dict(r) for r in rpl_rows]
                    rule_rows = conn.execute(
                        "SELECT * FROM modification_rules ORDER BY priority ASC, created_at ASC, rule_id ASC"
                    ).fetchall()
                    saved_rules = [dict(r) for r in rule_rows]
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
            or saved_drafts
            or saved_replay_runs
            or saved_rules
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
                                target_metadata_json, description, created_at, updated_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                rt["route_id"],
                                rt["domain"],
                                rt["upstream_scheme"],
                                rt["upstream_host"],
                                rt["upstream_port"],
                                rt["enabled"],
                                rt["copied_from_route_id"],
                                rt.get("target_metadata_json"),
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
                for dr in saved_drafts:
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
                            dr["draft_id"],
                            dr["source_capture_id"],
                            dr["method"],
                            dr["scheme"],
                            dr["authority"],
                            dr["host"],
                            dr["raw_path"],
                            dr["decoded_path"],
                            dr["raw_query"],
                            dr["query_entries_json"],
                            dr["http_version"],
                            dr["headers_json"],
                            dr["cookies_json"],
                            dr["body_mode"],
                            dr["content_type"],
                            dr["body_base64"],
                            dr["body_text"],
                            dr["json_value_json"],
                            dr["form_fields_json"],
                            dr["multipart_view_json"],
                            dr["parse_errors_json"],
                            dr["created_at"],
                            dr["updated_at"],
                            dr["expires_at"],
                        ),
                    )
                for rpl in saved_replay_runs:
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
                            rpl["replay_id"],
                            rpl["draft_id"],
                            rpl["source_capture_id"],
                            rpl["result_capture_id"],
                            rpl["replayed_at"],
                            rpl["method"],
                            rpl["scheme"],
                            rpl["authority"],
                            rpl["raw_target"],
                            rpl["request_body_size"],
                            rpl["request_body_sha256"],
                            rpl["response_status"],
                            rpl["duration_ms"],
                            rpl["side_effect_warning_json"],
                            rpl["error_message"],
                        ),
                    )
                for rl in saved_rules:
                    conn.execute(
                        """
                        INSERT INTO modification_rules (
                            rule_id, name, description, enabled, priority,
                            match_json, actions_json, copied_from_rule_id,
                            created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            rl["rule_id"],
                            rl["name"],
                            rl["description"],
                            rl["enabled"],
                            rl["priority"],
                            rl["match_json"],
                            rl["actions_json"],
                            rl["copied_from_rule_id"],
                            rl["created_at"],
                            rl["updated_at"],
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
            "capture_files_on_disk": raw_files,
            "total_indexed": total_indexed,
            "indexed_captures": total_indexed,
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
        source_capture_id: str | None = None,
        draft_id: str | None = None,
        rule_id: str | None = None,
        applied_rule_id: str | None = None,
        failed_rule_id: str | None = None,
        skipped_rule_id: str | None = None,
        rule_modified: bool | None = None,
        rule_failed: bool | None = None,
        rule_outcome: str | None = None,
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

        if source_capture_id:
            where_clauses.append(
                "capture_id IN (SELECT result_capture_id FROM replay_runs WHERE source_capture_id = ? AND result_capture_id IS NOT NULL)"
            )
            params.append(source_capture_id.strip())

        if draft_id:
            where_clauses.append(
                "capture_id IN (SELECT result_capture_id FROM replay_runs WHERE draft_id = ? AND result_capture_id IS NOT NULL)"
            )
            params.append(draft_id.strip())

        if rule_id:
            rid_tok = f'"{rule_id.strip()}"'
            where_clauses.append(
                "(matched_rule_ids_json LIKE ? OR applied_rule_ids_json LIKE ? OR skipped_rule_ids_json LIKE ? OR failed_rule_ids_json LIKE ?)"
            )
            params.extend([f"%{rid_tok}%"] * 4)

        if applied_rule_id:
            where_clauses.append("applied_rule_ids_json LIKE ?")
            params.append(f'%"{applied_rule_id.strip()}"%')

        if failed_rule_id:
            where_clauses.append("failed_rule_ids_json LIKE ?")
            params.append(f'%"{failed_rule_id.strip()}"%')

        if skipped_rule_id:
            where_clauses.append("skipped_rule_ids_json LIKE ?")
            params.append(f'%"{skipped_rule_id.strip()}"%')

        if rule_modified is not None:
            where_clauses.append("rule_modified = ?")
            params.append(1 if rule_modified else 0)

        if rule_failed is not None:
            where_clauses.append("rule_failed = ?")
            params.append(1 if rule_failed else 0)

        if rule_outcome:
            oc = rule_outcome.strip().lower()
            if oc in ("applied", "modified"):
                where_clauses.append("rule_modified = 1")
            elif oc == "failed":
                where_clauses.append("rule_failed = 1")
            elif oc == "skipped":
                where_clauses.append("skipped_rule_ids_json != '[]'")
            elif oc == "matched":
                where_clauses.append("matched_rule_ids_json != '[]'")
            elif oc in ("none", "unmatched"):
                where_clauses.append("matched_rule_ids_json = '[]'")

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
                    proxy_error, has_parse_errors, rule_modified, rule_failed,
                    matched_rule_ids_json, applied_rule_ids_json,
                    skipped_rule_ids_json, failed_rule_ids_json,
                    rule_evaluation_json, record_file, indexed_at
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

            rpl_map: dict[str, dict[str, Any]] = {}
            if rows:
                cids = [r["capture_id"] for r in rows]
                placeholders = ",".join("?" for _ in cids)
                rpl_rows = conn.execute(
                    f"""
                    SELECT replay_id, draft_id, source_capture_id, result_capture_id, replayed_at
                    FROM replay_runs
                    WHERE result_capture_id IN ({placeholders})
                    """,
                    cids,
                ).fetchall()
                for rr in rpl_rows:
                    rpl_map[rr["result_capture_id"]] = {
                        "is_replay": True,
                        "replay_id": rr["replay_id"],
                        "source_capture_id": rr["source_capture_id"],
                        "draft_id": rr["draft_id"],
                        "replayed_at": rr["replayed_at"],
                    }

        items: list[dict[str, Any]] = []
        for r in rows:
            cid = r["capture_id"]
            try:
                rpl_chk = self.drafts.evaluate_replayability(
                    cid, raise_on_false=False
                )
            except Exception as exc:
                rpl_chk = {
                    "capture_id": cid,
                    "replayable": False,
                    "reason": "missing_record",
                    "capture_status": r["capture_status"],
                    "message": str(exc),
                }
            rule_eval_raw = r["rule_evaluation_json"]
            rule_eval_obj = (
                json.loads(rule_eval_raw) if rule_eval_raw else {}
            )
            matched_ids = json.loads(r["matched_rule_ids_json"] or "[]")
            applied_ids = json.loads(r["applied_rule_ids_json"] or "[]")
            skipped_ids = json.loads(r["skipped_rule_ids_json"] or "[]")
            failed_ids = json.loads(r["failed_rule_ids_json"] or "[]")
            items.append(
                {
                    "capture_id": cid,
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
                    "replayable": bool(rpl_chk.get("replayable")),
                    "replayability": rpl_chk,
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
                    "replay_lineage": rpl_map.get(cid),
                    "rule_modified": bool(r["rule_modified"]),
                    "rule_failed": bool(r["rule_failed"]),
                    "matched_rule_ids": matched_ids,
                    "applied_rule_ids": applied_ids,
                    "skipped_rule_ids": skipped_ids,
                    "failed_rule_ids": failed_ids,
                    "rule_evaluation": {
                        "evaluated": bool(rule_eval_obj.get("evaluated", False)),
                        "modified": bool(r["rule_modified"]),
                        "rule_modified": bool(r["rule_modified"]),
                        "has_failures": bool(r["rule_failed"]),
                        "rule_failed": bool(r["rule_failed"]),
                        "outcome": rule_eval_obj.get("outcome", "no_match"),
                        "matched_rule_ids": matched_ids,
                        "applied_rule_ids": applied_ids,
                        "skipped_rule_ids": skipped_ids,
                        "failed_rule_ids": failed_ids,
                        "change_count": int(rule_eval_obj.get("change_count", 0)),
                        "change_summary": rule_eval_obj.get("change_summary", []),
                    },
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
                "source_capture_id": source_capture_id,
                "draft_id": draft_id,
                "rule_id": rule_id,
                "applied_rule_id": applied_rule_id,
                "failed_rule_id": failed_rule_id,
                "skipped_rule_id": skipped_rule_id,
                "rule_modified": rule_modified,
                "rule_failed": rule_failed,
                "rule_outcome": rule_outcome,
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
        blob_missing = False
        if blob_ref:
            blob_path = self.storage_dir / blob_ref
            if not blob_path.is_file():
                blob_missing = True
            else:
                blob_bytes = blob_path.read_bytes()

        blob_sha256 = (
            hashlib.sha256(blob_bytes).hexdigest() if not blob_missing else None
        )
        captured_size_matches = (not blob_missing) and (
            len(blob_bytes) == body_meta.get("captured_size", 0)
        )
        if record.get("capture_status") == "complete" and not blob_missing:
            sha256_matches = blob_sha256 == body_meta.get("sha256")
        else:
            sha256_matches = False

        regenerated_views = build_derived_views(
            raw_query=req.get("raw_query", ""),
            headers=req.get("headers", []),
            raw_body=(
                blob_bytes
                if (record.get("capture_status") == "complete" and not blob_missing)
                else None
            ),
            content_type=body_meta.get("content_type"),
            capture_status=(
                record.get("capture_status", "complete")
                if not blob_missing
                else "missing_blob"
            ),
        )
        regenerated_views["decoded_path"] = decode_path_human_readable(
            req.get("raw_path", "/")
        )

        rpl_eval = self.drafts.evaluate_replayability(
            capture_id, raise_on_false=False
        )

        out = dict(record)
        out["replayable"] = bool(rpl_eval.get("replayable"))
        out["indexed"] = idx_row is not None
        out["derived_views"] = regenerated_views
        out["regenerated_derived_views"] = regenerated_views
        out["reader_verification"] = {
            "blob_missing": blob_missing,
            "blob_bytes_len": len(blob_bytes),
            "blob_sha256": blob_sha256,
            "captured_size_matches": captured_size_matches,
            "full_body_sha256_matches": sha256_matches,
        }
        out["replay_relations"] = self.drafts.get_capture_replay_relations(
            capture_id
        )
        out["replayability"] = rpl_eval
        if "rule_evaluation" not in out or out["rule_evaluation"] is None:
            out["rule_evaluation"] = {
                "evaluated": False,
                "modified": False,
                "rule_modified": False,
                "has_failures": False,
                "rule_failed": False,
                "outcome": "no_match",
                "matched_rule_ids": [],
                "applied_rule_ids": [],
                "skipped_rule_ids": [],
                "failed_rule_ids": [],
                "change_count": 0,
                "change_summary": [],
                "rules": [],
            }
        else:
            re_obj = out["rule_evaluation"]
            re_obj.setdefault("rule_modified", bool(re_obj.get("modified", False)))
            re_obj.setdefault("rule_failed", bool(re_obj.get("has_failures", False)))
            if "outcome" not in re_obj:
                if re_obj.get("rule_modified"):
                    re_obj["outcome"] = "applied"
                elif re_obj.get("rule_failed"):
                    re_obj["outcome"] = "failed"
                elif out.get("is_websocket") and re_obj.get("skipped_rule_ids"):
                    re_obj["outcome"] = "skipped_websocket"
                elif re_obj.get("skipped_rule_ids"):
                    re_obj["outcome"] = "skipped"
                else:
                    re_obj["outcome"] = "no_match"
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
