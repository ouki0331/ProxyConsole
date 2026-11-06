-- ProxyConsole Retention Policy and Execution History Schema (Migration 002)
-- Persists configurable retention thresholds (min retention window, soft target, hard limit,
-- periodic schedule) and recent cleanup execution history across restarts.

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
