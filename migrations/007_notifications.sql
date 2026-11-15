-- DEV-042: Aggregated local notifications and live event feed schema

CREATE TABLE IF NOT EXISTS notification_config (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    aggregation_window_seconds INTEGER NOT NULL DEFAULT 300,
    notify_http_4xx INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS notification_incidents (
    incident_id TEXT PRIMARY KEY,
    fingerprint TEXT NOT NULL,
    event_type TEXT NOT NULL,
    severity TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    route_id TEXT NOT NULL,
    domain TEXT NOT NULL DEFAULT '',
    normalized_reason TEXT NOT NULL,
    message TEXT NOT NULL,
    count INTEGER NOT NULL DEFAULT 1,
    suppressed_count INTEGER NOT NULL DEFAULT 0,
    cycle_number INTEGER NOT NULL DEFAULT 1,
    window_seconds INTEGER NOT NULL DEFAULT 300,
    window_started_unix REAL NOT NULL,
    first_seen TEXT NOT NULL,
    first_seen_unix REAL NOT NULL,
    last_seen TEXT NOT NULL,
    last_seen_unix REAL NOT NULL,
    resolved_at TEXT,
    resolved_at_unix REAL,
    recovery_reason TEXT,
    acknowledged INTEGER NOT NULL DEFAULT 0,
    acknowledged_at TEXT,
    last_status_code INTEGER,
    last_capture_id TEXT,
    last_seq INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_notification_incidents_lookup
    ON notification_incidents (route_id, fingerprint, status, window_started_unix DESC);

CREATE INDEX IF NOT EXISTS idx_notification_incidents_status_time
    ON notification_incidents (status, last_seen_unix DESC, incident_id DESC);

CREATE INDEX IF NOT EXISTS idx_notification_incidents_type
    ON notification_incidents (event_type, last_seen_unix DESC);

CREATE TABLE IF NOT EXISTS notification_events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    incident_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    event_type TEXT NOT NULL,
    severity TEXT NOT NULL,
    status TEXT NOT NULL,
    route_id TEXT NOT NULL,
    domain TEXT NOT NULL DEFAULT '',
    fingerprint TEXT NOT NULL,
    normalized_reason TEXT NOT NULL,
    message TEXT NOT NULL,
    count INTEGER NOT NULL,
    suppressed_count INTEGER NOT NULL DEFAULT 0,
    notify_browser INTEGER NOT NULL DEFAULT 0,
    occurred_at TEXT NOT NULL,
    occurred_at_unix REAL NOT NULL,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    resolved_at TEXT,
    recovery_reason TEXT,
    acknowledged INTEGER NOT NULL DEFAULT 0,
    acknowledged_at TEXT,
    incident_snapshot_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_notification_events_incident
    ON notification_events (incident_id, seq ASC);

CREATE INDEX IF NOT EXISTS idx_notification_events_time
    ON notification_events (occurred_at_unix DESC, seq DESC);
