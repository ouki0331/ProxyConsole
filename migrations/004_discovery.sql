-- ProxyConsole Docker Discovery Status Schema (Migration 004)
-- Tracks Docker Engine discovery availability, refresh timestamps, error state, and socket override.

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
