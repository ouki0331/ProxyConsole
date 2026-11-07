-- ProxyConsole Route Management and Atomic Nginx Apply Schema (Migration 003)
-- Persists desired domain routes, apply state/generation tracking, and apply history.

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
