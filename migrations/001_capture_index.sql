-- ProxyConsole Durable Capture Metadata Index Schema (Migration 001)
-- Source of truth remains captures/*.json and blobs/*.bin.
-- This SQLite database is a rebuildable derived index. Raw body bytes are never stored here.

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
