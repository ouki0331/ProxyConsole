-- DEV-030: Persistent request drafts and replay lineage tracking
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
