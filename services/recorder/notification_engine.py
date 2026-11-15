#!/usr/bin/env python3
"""Aggregated local notifications and live event feed engine (DEV-042).

Normalizes HTTP 5xx, 502/503/504, upstream timeouts, Nginx warn/error diagnostics,
and stopped target/container states into a common durable event & incident model.
Aggregates identical (route_id + fingerprint) failures within a configurable window
(default 5 minutes / 300s), emits initial / count_update / recovery events with a
monotonically increasing cursor (`seq`), and excludes HTTP 4xx by default.
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

DEFAULT_AGGREGATION_WINDOW_SECONDS = 300

CANONICAL_EVENT_TYPES = (
    "http_500",
    "http_502_503_504",
    "upstream_timeout",
    "nginx_warn",
    "nginx_error",
    "container_unavailable",
)

EVENT_TYPE_ALIASES: dict[str, tuple[str, ...]] = {
    "http_500": ("http_500",),
    "500": ("http_500",),
    "http_5xx": ("http_500", "http_502_503_504"),
    "http_502_503_504": ("http_502_503_504",),
    "gateway_5xx": ("http_502_503_504",),
    "http_502": ("http_502_503_504",),
    "http_503": ("http_502_503_504",),
    "http_504": ("http_502_503_504",),
    "502": ("http_502_503_504",),
    "503": ("http_502_503_504",),
    "504": ("http_502_503_504",),
    "upstream_timeout": ("upstream_timeout",),
    "timeout": ("upstream_timeout",),
    "nginx_warn": ("nginx_warn",),
    "nginx_error": ("nginx_error",),
    "nginx_warn_error": ("nginx_warn", "nginx_error"),
    "nginx": ("nginx_warn", "nginx_error"),
    "container_unavailable": ("container_unavailable",),
    "target_unavailable": ("container_unavailable",),
    "stopped_target": ("container_unavailable",),
}

EVENT_DEFAULT_SEVERITY: dict[str, str] = {
    "http_500": "error",
    "http_502_503_504": "error",
    "upstream_timeout": "error",
    "nginx_warn": "warning",
    "nginx_error": "error",
    "container_unavailable": "critical",
}

SIGNIFICANT_COUNT_MILESTONES = {5, 10, 25, 50, 100}

# Regexes to strip dynamic request IDs, timestamps, PIDs, ephemeral ports, and counts from reasons
_DYNAMIC_CAPTURE_ID_RE = re.compile(r"\b(?:cap|req|inc|evt|draft)-[A-Za-z0-9_-]+\b", re.IGNORECASE)
_DYNAMIC_ISO_TS_RE = re.compile(
    r"\b20\d{2}[-/]\d{2}[-/]\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?\b"
)
_DYNAMIC_NGINX_PID_RE = re.compile(r"\b\d+#\d+:\s*(?:\*\d+\s*)?")
_DYNAMIC_DURATION_RE = re.compile(r"\b\d+(?:\.\d+)?\s*(?:ms|s|sec|seconds)\b", re.IGNORECASE)
_DYNAMIC_COUNT_RE = re.compile(r"\b(?:count|seq|attempt|request_id|id)\s*[=:]\s*[A-Za-z0-9_-]+\b", re.IGNORECASE)
_DYNAMIC_HEX_UUID_RE = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.IGNORECASE)
_WHITESPACE_RE = re.compile(r"[\s_]+")

_NGINX_LOG_LINE_RE = re.compile(
    r"^(?:(?P<ts>\d{4}/\d{2}/\d{2}\s+\d{2}:\d{2}:\d{2})\s+)?"
    r"(?:nginx:\s+)?"
    r"\[(?P<level>warn|warning|error|crit|alert|emerg)\]\s+"
    r"(?:(?P<pid>\d+#\d+):\s*(?:\*\d+\s+)?)?"
    r"(?P<msg>.+)$",
    re.IGNORECASE,
)


class NotificationsUnavailableError(RuntimeError):
    """Raised when the notifications subsystem is unavailable or simulated-offline."""


class NotificationNotFoundError(KeyError):
    """Raised when an incident_id does not exist."""


class NotificationValidationError(ValueError):
    """Raised when notification filter or ingestion parameters are invalid."""

    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.details = details


def _parse_timestamp_to_unix(value: Any) -> float:
    if value is None:
        return time.time()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    s = str(value).strip()
    if not s:
        return time.time()
    try:
        return float(s)
    except ValueError:
        pass
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError as exc:
        raise NotificationValidationError(
            "invalid_timestamp", f"Invalid timestamp format: {value!r}"
        ) from exc


def _unix_to_iso(ts_unix: float) -> str:
    return datetime.fromtimestamp(ts_unix, tz=timezone.utc).isoformat()


def normalize_reason(event_type: str, raw_reason: Any) -> str:
    """Normalizes a failure reason, removing request IDs, timestamps, PIDs, and dynamic counts."""
    text = str(raw_reason or "").strip()
    if not text:
        return f"default_{event_type}"
    text = _DYNAMIC_CAPTURE_ID_RE.sub("", text)
    text = _DYNAMIC_ISO_TS_RE.sub("", text)
    text = _DYNAMIC_NGINX_PID_RE.sub("", text)
    text = _DYNAMIC_DURATION_RE.sub("", text)
    text = _DYNAMIC_COUNT_RE.sub("", text)
    text = _DYNAMIC_HEX_UUID_RE.sub("", text)
    # Strip trailing Nginx request/client context suffix if present
    for marker in (", client:", " while reading ", " while connecting to upstream, client:"):
        if marker in text:
            text = text.split(marker, 1)[0]
    cleaned = _WHITESPACE_RE.sub("_", text.strip().lower()).strip("_:.,;-")
    if not cleaned:
        return f"default_{event_type}"
    return cleaned[:160]


def compute_fingerprint(event_type: str, route_id: str, normalized_reason: str) -> str:
    """Computes a stable fingerprint from event_type, route_id, and normalized_reason only."""
    clean_type = str(event_type or "unknown").strip().lower()
    clean_route = str(route_id or "route:unknown").strip().lower()
    clean_reason = normalize_reason(clean_type, normalized_reason)
    canonical = f"{clean_type}|{clean_route}|{clean_reason}"
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
    return f"fp:{digest}"


def normalize_event_type(raw_type: Any, status_code: int | None = None) -> str:
    s = str(raw_type or "").strip().lower()
    if s in CANONICAL_EVENT_TYPES:
        return s
    if s in ("http_5xx", "5xx"):
        if status_code in (502, 503, 504):
            return "http_502_503_504"
        return "http_500"
    if s in ("500", "501", "505", "http_500"):
        return "http_500"
    if s in ("502", "503", "504", "http_502", "http_503", "http_504", "gateway_5xx", "bad_gateway", "service_unavailable", "gateway_timeout"):
        return "http_502_503_504"
    if s in ("timeout", "upstream_timeout", "gateway_timeout_upstream", "read_timeout", "connect_timeout"):
        return "upstream_timeout"
    if s in ("warn", "warning", "nginx_warn", "nginx_warning"):
        return "nginx_warn"
    if s in ("error", "crit", "alert", "emerg", "nginx_error", "nginx_emerg", "nginx_crit"):
        return "nginx_error"
    if s in ("container_unavailable", "target_unavailable", "stopped_target", "container_stopped", "target_stopped"):
        return "container_unavailable"
    raise NotificationValidationError(
        "invalid_event_type",
        f"Unsupported event_type: {raw_type!r}. Must be one of {list(CANONICAL_EVENT_TYPES)}",
        field="event_type",
    )


class NotificationEngineManager:
    def __init__(self, store: CaptureIndexStore) -> None:
        self.store = store
        self.lock_path = self.store.db_dir / "notifications.lock"
        self.sim_flag_path = self.store.db_dir / "notifications_sim.json"
        self.nginx_log_path = self.store.storage_dir / "router" / "nginx-error.log"
        self.nginx_offset_path = self.store.db_dir / "nginx_error_log.offset"

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

    def _read_sim_state(self) -> dict[str, Any]:
        default_state = {
            "simulate_unavailable": False,
            "simulate_ws_unavailable": False,
            "simulate_persistence_failure": False,
            "ws_drop_generation": 0,
        }
        if not self.sim_flag_path.is_file():
            return default_state
        try:
            data = json.loads(self.sim_flag_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return {
                    "simulate_unavailable": bool(data.get("simulate_unavailable", False)),
                    "simulate_ws_unavailable": bool(data.get("simulate_ws_unavailable", False)),
                    "simulate_persistence_failure": bool(data.get("simulate_persistence_failure", False)),
                    "ws_drop_generation": int(data.get("ws_drop_generation", 0) or 0),
                }
        except Exception:
            pass
        return default_state

    def _write_sim_state(self, state: dict[str, Any]) -> dict[str, Any]:
        self.store.db_dir.mkdir(parents=True, exist_ok=True)
        clean = {
            "simulate_unavailable": bool(state.get("simulate_unavailable", False)),
            "simulate_ws_unavailable": bool(state.get("simulate_ws_unavailable", False)),
            "simulate_persistence_failure": bool(state.get("simulate_persistence_failure", False)),
            "ws_drop_generation": int(state.get("ws_drop_generation", 0) or 0),
        }
        tmp = self.sim_flag_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(clean, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, self.sim_flag_path)
        return clean

    def check_api_available(self) -> None:
        sim = self._read_sim_state()
        if sim["simulate_unavailable"] or sim["simulate_persistence_failure"]:
            raise NotificationsUnavailableError(
                "Notifications subsystem is currently unavailable (simulated failure)"
            )

    def check_ws_available(self) -> int:
        sim = self._read_sim_state()
        if (
            sim["simulate_unavailable"]
            or sim["simulate_ws_unavailable"]
            or sim["simulate_persistence_failure"]
        ):
            raise NotificationsUnavailableError(
                "Notifications WebSocket feed is currently unavailable"
            )
        return int(sim["ws_drop_generation"])

    def get_ws_drop_generation(self) -> int:
        return int(self._read_sim_state().get("ws_drop_generation", 0))

    def get_config(self) -> dict[str, Any]:
        self.store.ensure_schema()
        sim = self._read_sim_state()
        with self.store._connect() as conn:
            row = conn.execute(
                "SELECT * FROM notification_config WHERE id = 1"
            ).fetchone()
        if row is None:
            return {
                "aggregation_window_seconds": DEFAULT_AGGREGATION_WINDOW_SECONDS,
                "window_sec": DEFAULT_AGGREGATION_WINDOW_SECONDS,
                "notify_http_4xx": False,
                "ignore_4xx": True,
                "updated_at": _unix_to_iso(time.time()),
                **sim,
            }
        win_sec = int(row["aggregation_window_seconds"])
        notify_4xx = bool(row["notify_http_4xx"])
        return {
            "aggregation_window_seconds": win_sec,
            "window_sec": win_sec,
            "notify_http_4xx": notify_4xx,
            "ignore_4xx": not notify_4xx,
            "updated_at": str(row["updated_at"]),
            **sim,
        }

    def update_config(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.store.ensure_schema()
        sim = self._read_sim_state()
        sim_changed = False
        for k in ("simulate_unavailable", "simulate_ws_unavailable", "simulate_persistence_failure"):
            if k in payload:
                sim[k] = bool(payload[k])
                sim_changed = True
        if payload.get("drop_ws_connections"):
            sim["ws_drop_generation"] = int(sim.get("ws_drop_generation", 0)) + 1
            sim_changed = True
        if sim_changed:
            self._write_sim_state(sim)

        if (
            "aggregation_window_seconds" in payload
            or "window_sec" in payload
            or "notify_http_4xx" in payload
            or "ignore_4xx" in payload
        ):
            current = self.get_config()
            raw_win = payload.get(
                "aggregation_window_seconds",
                payload.get("window_sec", current["aggregation_window_seconds"]),
            )
            win_sec = int(raw_win)
            if win_sec < 1 or win_sec > 86400:
                raise NotificationValidationError(
                    "invalid_aggregation_window",
                    "aggregation_window_seconds must be between 1 and 86400",
                    field="aggregation_window_seconds",
                )
            if "notify_http_4xx" in payload:
                notify_4xx = bool(payload["notify_http_4xx"])
            elif "ignore_4xx" in payload:
                notify_4xx = not bool(payload["ignore_4xx"])
            else:
                notify_4xx = current["notify_http_4xx"]
            now_iso = _unix_to_iso(time.time())
            with self._exclusive_lock():
                with self.store._connect() as conn:
                    conn.execute(
                        """
                        INSERT INTO notification_config (
                            id, aggregation_window_seconds, notify_http_4xx, updated_at
                        ) VALUES (1, ?, ?, ?)
                        ON CONFLICT(id) DO UPDATE SET
                            aggregation_window_seconds = excluded.aggregation_window_seconds,
                            notify_http_4xx = excluded.notify_http_4xx,
                            updated_at = excluded.updated_at
                        """,
                        (win_sec, 1 if notify_4xx else 0, now_iso),
                    )
                    conn.commit()
                self.store.retention.checkpoint_wal()
        return self.get_config()

    def _incident_row_to_dict(self, row: Any) -> dict[str, Any]:
        event_type = str(row["event_type"])
        route_id = str(row["route_id"])
        norm_reason = str(row["normalized_reason"])
        return {
            "incident_id": str(row["incident_id"]),
            "fingerprint": str(row["fingerprint"]),
            "fingerprint_components": {
                "event_type": event_type,
                "route_id": route_id,
                "normalized_reason": norm_reason,
            },
            "event_type": event_type,
            "severity": str(row["severity"]),
            "status": str(row["status"]),
            "route_id": route_id,
            "domain": str(row["domain"] or ""),
            "normalized_reason": norm_reason,
            "message": str(row["message"]),
            "summary_message": str(row["message"]),
            "count": int(row["count"]),
            "suppressed_count": int(row["suppressed_count"]),
            "cycle_number": int(row["cycle_number"]),
            "window_seconds": int(row["window_seconds"]),
            "window_sec": int(row["window_seconds"]),
            "window_started_unix": float(row["window_started_unix"]),
            "first_seen": str(row["first_seen"]),
            "first_seen_unix": float(row["first_seen_unix"]),
            "first_seen_epoch": float(row["first_seen_unix"]),
            "last_seen": str(row["last_seen"]),
            "last_seen_unix": float(row["last_seen_unix"]),
            "last_seen_epoch": float(row["last_seen_unix"]),
            "resolved_at": row["resolved_at"],
            "resolved_at_unix": (
                float(row["resolved_at_unix"])
                if row["resolved_at_unix"] is not None
                else None
            ),
            "recovery_reason": row["recovery_reason"],
            "acknowledged": bool(row["acknowledged"]),
            "acknowledged_at": row["acknowledged_at"],
            "last_status_code": (
                int(row["last_status_code"])
                if row["last_status_code"] is not None
                else None
            ),
            "last_capture_id": row["last_capture_id"],
            "last_seq": int(row["last_seq"]),
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
        }

    def _event_row_to_dict(self, row: Any) -> dict[str, Any]:
        snapshot: dict[str, Any] = {}
        raw_snap = row["incident_snapshot_json"]
        if raw_snap:
            try:
                parsed = json.loads(str(raw_snap))
                if isinstance(parsed, dict):
                    snapshot = parsed
            except Exception:
                snapshot = {}
        seq = int(row["seq"])
        if snapshot:
            snapshot["last_seq"] = max(int(snapshot.get("last_seq", 0)), seq)
        return {
            "seq": seq,
            "cursor": seq,
            "event_id": str(row["event_id"]),
            "incident_id": str(row["incident_id"]),
            "kind": str(row["kind"]),
            "event_kind": str(row["kind"]),
            "event_type": str(row["event_type"]),
            "severity": str(row["severity"]),
            "status": str(row["status"]),
            "route_id": str(row["route_id"]),
            "domain": str(row["domain"] or ""),
            "fingerprint": str(row["fingerprint"]),
            "normalized_reason": str(row["normalized_reason"]),
            "message": str(row["message"]),
            "summary_message": str(row["message"]),
            "count": int(row["count"]),
            "suppressed_count": int(row["suppressed_count"]),
            "notify_browser": bool(row["notify_browser"]),
            "browser_notify": bool(row["notify_browser"]),
            "occurred_at": str(row["occurred_at"]),
            "occurred_at_unix": float(row["occurred_at_unix"]),
            "first_seen": str(row["first_seen"]),
            "last_seen": str(row["last_seen"]),
            "resolved_at": row["resolved_at"],
            "recovery_reason": row["recovery_reason"],
            "acknowledged": bool(row["acknowledged"]),
            "acknowledged_at": row["acknowledged_at"],
            "incident": snapshot,
        }

    def _resolve_domain_for_route(self, conn: Any, route_id: str, fallback_domain: str = "") -> str:
        if fallback_domain:
            return fallback_domain.split(":", 1)[0].strip().lower()
        try:
            row = conn.execute(
                "SELECT domain FROM routes WHERE route_id = ?",
                (route_id,),
            ).fetchone()
            if row and row["domain"]:
                return str(row["domain"])
        except Exception:
            pass
        if route_id == "route:app-a":
            return "app-a.local.test"
        if route_id == "route:app-b":
            return "app-b.local.test"
        if route_id.startswith("route:unmatched:"):
            return route_id.split("route:unmatched:", 1)[1]
        return ""

    def _insert_event_with_conn(
        self,
        conn: Any,
        *,
        incident_row: Any,
        kind: str,
        notify_browser: bool,
        occurred_iso: str,
        occurred_unix: float,
        message_override: str | None = None,
    ) -> dict[str, Any]:
        inc_dict = self._incident_row_to_dict(incident_row)
        event_id = f"evt-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}-{uuid.uuid4().hex[:8]}"
        msg = message_override or inc_dict["message"]
        cur = conn.execute(
            """
            INSERT INTO notification_events (
                event_id, incident_id, kind, event_type, severity, status,
                route_id, domain, fingerprint, normalized_reason, message,
                count, suppressed_count, notify_browser,
                occurred_at, occurred_at_unix,
                first_seen, last_seen, resolved_at, recovery_reason,
                acknowledged, acknowledged_at, incident_snapshot_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                inc_dict["incident_id"],
                kind,
                inc_dict["event_type"],
                inc_dict["severity"],
                inc_dict["status"],
                inc_dict["route_id"],
                inc_dict["domain"],
                inc_dict["fingerprint"],
                inc_dict["normalized_reason"],
                msg,
                inc_dict["count"],
                inc_dict["suppressed_count"],
                1 if notify_browser else 0,
                occurred_iso,
                occurred_unix,
                inc_dict["first_seen"],
                inc_dict["last_seen"],
                inc_dict["resolved_at"],
                inc_dict["recovery_reason"],
                1 if inc_dict["acknowledged"] else 0,
                inc_dict["acknowledged_at"],
                json.dumps(inc_dict, ensure_ascii=False),
            ),
        )
        seq = int(cur.lastrowid)
        inc_dict["last_seq"] = seq
        conn.execute(
            "UPDATE notification_incidents SET last_seq = ? WHERE incident_id = ?",
            (seq, inc_dict["incident_id"]),
        )
        conn.execute(
            "UPDATE notification_events SET incident_snapshot_json = ? WHERE seq = ?",
            (json.dumps(inc_dict, ensure_ascii=False), seq),
        )
        ev_row = conn.execute(
            "SELECT * FROM notification_events WHERE seq = ?",
            (seq,),
        ).fetchone()
        return self._event_row_to_dict(ev_row)

    def record_normalized_event(
        self,
        *,
        event_type: str,
        route_id: str,
        reason: str,
        domain: str = "",
        severity: str | None = None,
        message: str | None = None,
        status_code: int | None = None,
        capture_id: str | None = None,
        occurred_at: Any = None,
    ) -> dict[str, Any]:
        """Records a normalized error/warning event with 5-minute fingerprint aggregation."""
        sim = self._read_sim_state()
        if sim["simulate_persistence_failure"]:
            raise NotificationsUnavailableError(
                "Simulated notification persistence failure"
            )

        self.store.ensure_schema()

        # Check HTTP 4xx exclusion rule (Rule 1, AC-3)
        cfg = self.get_config()
        if status_code is not None and 400 <= int(status_code) < 500:
            if not cfg.get("notify_http_4xx", False):
                return {
                    "ignored": True,
                    "reason": "http_4xx_excluded_by_default",
                    "status_code": int(status_code),
                }

        canon_type = normalize_event_type(event_type, status_code=status_code)
        clean_route = str(route_id or "route:unknown").strip() or "route:unknown"
        norm_reason = normalize_reason(canon_type, reason)
        fingerprint = compute_fingerprint(canon_type, clean_route, norm_reason)
        sev = str(severity or EVENT_DEFAULT_SEVERITY.get(canon_type, "error")).strip().lower()
        now_unix = _parse_timestamp_to_unix(occurred_at)
        now_iso = _unix_to_iso(now_unix)
        win_sec = int(cfg.get("aggregation_window_seconds", DEFAULT_AGGREGATION_WINDOW_SECONDS))

        with self._exclusive_lock():
            with self.store._connect() as conn:
                resolved_domain = self._resolve_domain_for_route(conn, clean_route, domain)
                clean_msg = str(
                    message
                    or f"[{canon_type}] {clean_route} ({resolved_domain or 'local'}): {norm_reason}"
                ).strip()

                open_row = conn.execute(
                    """
                    SELECT * FROM notification_incidents
                    WHERE route_id = ? AND fingerprint = ? AND status = 'open'
                    ORDER BY window_started_unix DESC, created_at DESC
                    LIMIT 1
                    """,
                    (clean_route, fingerprint),
                ).fetchone()

                if open_row is not None:
                    win_started = float(open_row["window_started_unix"])
                    row_win_sec = int(open_row["window_seconds"] or win_sec)
                    if (now_unix - win_started) <= float(row_win_sec):
                        # Aggregate within current window!
                        inc_id = str(open_row["incident_id"])
                        new_count = int(open_row["count"]) + 1
                        is_milestone = (
                            new_count in SIGNIFICANT_COUNT_MILESTONES
                            or (new_count >= 100 and new_count % 100 == 0)
                        )
                        new_suppressed = (
                            int(open_row["suppressed_count"])
                            if is_milestone
                            else int(open_row["suppressed_count"]) + 1
                        )
                        last_seen_unix = max(float(open_row["last_seen_unix"]), now_unix)
                        last_seen_iso = _unix_to_iso(last_seen_unix)

                        conn.execute(
                            """
                            UPDATE notification_incidents
                            SET count = ?,
                                suppressed_count = ?,
                                last_seen = ?,
                                last_seen_unix = ?,
                                last_status_code = COALESCE(?, last_status_code),
                                last_capture_id = COALESCE(?, last_capture_id),
                                message = ?,
                                updated_at = ?
                            WHERE incident_id = ?
                            """,
                            (
                                new_count,
                                new_suppressed,
                                last_seen_iso,
                                last_seen_unix,
                                status_code,
                                capture_id,
                                clean_msg,
                                now_iso,
                                inc_id,
                            ),
                        )
                        updated_row = conn.execute(
                            "SELECT * FROM notification_incidents WHERE incident_id = ?",
                            (inc_id,),
                        ).fetchone()
                        ev_dict = self._insert_event_with_conn(
                            conn,
                            incident_row=updated_row,
                            kind="count_update",
                            notify_browser=is_milestone,
                            occurred_iso=now_iso,
                            occurred_unix=now_unix,
                        )
                        final_row = conn.execute(
                            "SELECT * FROM notification_incidents WHERE incident_id = ?",
                            (inc_id,),
                        ).fetchone()
                        conn.commit()
                        self.store.retention.checkpoint_wal()
                        return {
                            "ignored": False,
                            "aggregated": True,
                            "deduplicated_in_window": True,
                            "notify_browser": is_milestone,
                            "incident": self._incident_row_to_dict(final_row),
                            "event": ev_dict,
                        }
                    else:
                        # Window expired while incident was still open: close previous window cycle
                        # so the recurrence after window expiry starts a fresh notification cycle (AC-5)
                        conn.execute(
                            """
                            UPDATE notification_incidents
                            SET status = 'resolved',
                                resolved_at = ?,
                                resolved_at_unix = ?,
                                recovery_reason = 'aggregation_window_expired_rollover',
                                updated_at = ?
                            WHERE incident_id = ?
                            """,
                            (now_iso, now_unix, now_iso, str(open_row["incident_id"])),
                        )

                # Determine cycle_number from previous incidents with same route_id + fingerprint
                prev_cycle_row = conn.execute(
                    """
                    SELECT MAX(cycle_number) AS max_cycle
                    FROM notification_incidents
                    WHERE route_id = ? AND fingerprint = ?
                    """,
                    (clean_route, fingerprint),
                ).fetchone()
                next_cycle = (
                    int(prev_cycle_row["max_cycle"]) + 1
                    if (prev_cycle_row and prev_cycle_row["max_cycle"] is not None)
                    else 1
                )

                inc_id = f"inc-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}-{uuid.uuid4().hex[:8]}"
                conn.execute(
                    """
                    INSERT INTO notification_incidents (
                        incident_id, fingerprint, event_type, severity, status,
                        route_id, domain, normalized_reason, message,
                        count, suppressed_count, cycle_number,
                        window_seconds, window_started_unix,
                        first_seen, first_seen_unix,
                        last_seen, last_seen_unix,
                        resolved_at, resolved_at_unix, recovery_reason,
                        acknowledged, acknowledged_at,
                        last_status_code, last_capture_id, last_seq,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, 'open', ?, ?, ?, ?, 1, 0, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, 0, NULL, ?, ?, 0, ?, ?)
                    """,
                    (
                        inc_id,
                        fingerprint,
                        canon_type,
                        sev,
                        clean_route,
                        resolved_domain,
                        norm_reason,
                        clean_msg,
                        next_cycle,
                        win_sec,
                        now_unix,
                        now_iso,
                        now_unix,
                        now_iso,
                        now_unix,
                        status_code,
                        capture_id,
                        now_iso,
                        now_iso,
                    ),
                )
                new_row = conn.execute(
                    "SELECT * FROM notification_incidents WHERE incident_id = ?",
                    (inc_id,),
                ).fetchone()
                ev_dict = self._insert_event_with_conn(
                    conn,
                    incident_row=new_row,
                    kind="initial",
                    notify_browser=True,
                    occurred_iso=now_iso,
                    occurred_unix=now_unix,
                )
                final_row = conn.execute(
                    "SELECT * FROM notification_incidents WHERE incident_id = ?",
                    (inc_id,),
                ).fetchone()
                conn.commit()
            self.store.retention.checkpoint_wal()

        return {
            "ignored": False,
            "aggregated": False,
            "deduplicated_in_window": False,
            "notify_browser": True,
            "incident": self._incident_row_to_dict(final_row),
            "event": ev_dict,
        }

    def resolve_incident(
        self,
        incident_id: str,
        *,
        recovery_reason: str = "upstream_recovered",
        resolved_at: Any = None,
    ) -> dict[str, Any]:
        """Resolves an open incident once. Calling again on a resolved incident is a no-op (AC-6)."""
        self.check_api_available()
        self.store.ensure_schema()
        clean_id = str(incident_id or "").strip()
        if not clean_id:
            raise NotificationValidationError("invalid_incident_id", "incident_id is required")

        now_unix = _parse_timestamp_to_unix(resolved_at)
        now_iso = _unix_to_iso(now_unix)
        clean_reason = str(recovery_reason or "upstream_recovered").strip() or "upstream_recovered"

        with self._exclusive_lock():
            with self.store._connect() as conn:
                row = conn.execute(
                    "SELECT * FROM notification_incidents WHERE incident_id = ?",
                    (clean_id,),
                ).fetchone()
                if row is None:
                    raise NotificationNotFoundError(f"Incident not found: {clean_id}")
                if str(row["status"]) == "resolved":
                    return {
                        "already_resolved": True,
                        "transitioned": False,
                        "incident": self._incident_row_to_dict(row),
                        "event": None,
                    }

                conn.execute(
                    """
                    UPDATE notification_incidents
                    SET status = 'resolved',
                        resolved_at = ?,
                        resolved_at_unix = ?,
                        recovery_reason = ?,
                        updated_at = ?
                    WHERE incident_id = ?
                    """,
                    (now_iso, now_unix, clean_reason, now_iso, clean_id),
                )
                updated_row = conn.execute(
                    "SELECT * FROM notification_incidents WHERE incident_id = ?",
                    (clean_id,),
                ).fetchone()
                rec_msg = f"[recovered] {updated_row['route_id']}: {clean_reason}"
                ev_dict = self._insert_event_with_conn(
                    conn,
                    incident_row=updated_row,
                    kind="recovery",
                    notify_browser=True,
                    occurred_iso=now_iso,
                    occurred_unix=now_unix,
                    message_override=rec_msg,
                )
                final_row = conn.execute(
                    "SELECT * FROM notification_incidents WHERE incident_id = ?",
                    (clean_id,),
                ).fetchone()
                conn.commit()
            self.store.retention.checkpoint_wal()

        return {
            "already_resolved": False,
            "transitioned": True,
            "incident": self._incident_row_to_dict(final_row),
            "event": ev_dict,
        }

    def resolve_open_incidents(
        self,
        *,
        route_id: str | None = None,
        fingerprint: str | None = None,
        event_types: tuple[str, ...] | list[str] | None = None,
        recovery_reason: str = "upstream_recovered",
        resolved_at: Any = None,
    ) -> dict[str, Any]:
        """Resolves all matching open incidents once (Rule 4, AC-6)."""
        sim = self._read_sim_state()
        if sim["simulate_persistence_failure"]:
            raise NotificationsUnavailableError("Simulated persistence failure")

        self.store.ensure_schema()
        clauses = ["status = 'open'"]
        params: list[Any] = []
        if route_id:
            clauses.append("route_id = ?")
            params.append(str(route_id).strip())
        if fingerprint:
            clauses.append("fingerprint = ?")
            params.append(str(fingerprint).strip())
        if event_types:
            placeholders = ",".join("?" for _ in event_types)
            clauses.append(f"event_type IN ({placeholders})")
            params.extend(str(t) for t in event_types)

        where_sql = " AND ".join(clauses)
        with self.store._connect() as conn:
            open_rows = conn.execute(
                f"SELECT incident_id FROM notification_incidents WHERE {where_sql} ORDER BY first_seen_unix ASC",
                params,
            ).fetchall()

        resolved_items: list[dict[str, Any]] = []
        events: list[dict[str, Any]] = []
        for r in open_rows:
            res = self.resolve_incident(
                str(r["incident_id"]),
                recovery_reason=recovery_reason,
                resolved_at=resolved_at,
            )
            if not res["already_resolved"]:
                resolved_items.append(res["incident"])
                if res["event"]:
                    events.append(res["event"])

        return {
            "resolved_count": len(resolved_items),
            "incidents": resolved_items,
            "events": events,
        }

    def acknowledge_incident(
        self,
        incident_id: str,
        *,
        acknowledged: bool = True,
    ) -> dict[str, Any]:
        self.check_api_available()
        self.store.ensure_schema()
        clean_id = str(incident_id or "").strip()
        if not clean_id:
            raise NotificationValidationError("invalid_incident_id", "incident_id is required")

        now_unix = time.time()
        now_iso = _unix_to_iso(now_unix)
        ack_val = 1 if acknowledged else 0
        ack_at = now_iso if acknowledged else None

        with self._exclusive_lock():
            with self.store._connect() as conn:
                row = conn.execute(
                    "SELECT * FROM notification_incidents WHERE incident_id = ?",
                    (clean_id,),
                ).fetchone()
                if row is None:
                    raise NotificationNotFoundError(f"Incident not found: {clean_id}")

                conn.execute(
                    """
                    UPDATE notification_incidents
                    SET acknowledged = ?,
                        acknowledged_at = ?,
                        updated_at = ?
                    WHERE incident_id = ?
                    """,
                    (ack_val, ack_at, now_iso, clean_id),
                )
                updated_row = conn.execute(
                    "SELECT * FROM notification_incidents WHERE incident_id = ?",
                    (clean_id,),
                ).fetchone()
                ev_dict = self._insert_event_with_conn(
                    conn,
                    incident_row=updated_row,
                    kind="acknowledged" if acknowledged else "unacknowledged",
                    notify_browser=False,
                    occurred_iso=now_iso,
                    occurred_unix=now_unix,
                )
                final_row = conn.execute(
                    "SELECT * FROM notification_incidents WHERE incident_id = ?",
                    (clean_id,),
                ).fetchone()
                conn.commit()
            self.store.retention.checkpoint_wal()

        return {
            "incident": self._incident_row_to_dict(final_row),
            "event": ev_dict,
        }

    def acknowledge_all(self, *, status: str | None = None) -> dict[str, Any]:
        self.check_api_available()
        self.store.ensure_schema()
        clauses = ["acknowledged = 0"]
        params: list[Any] = []
        if status and status not in ("all", "*"):
            clauses.append("status = ?")
            params.append(status)
        where_sql = " AND ".join(clauses)
        with self.store._connect() as conn:
            rows = conn.execute(
                f"SELECT incident_id FROM notification_incidents WHERE {where_sql}",
                params,
            ).fetchall()
        updated = []
        for r in rows:
            res = self.acknowledge_incident(str(r["incident_id"]), acknowledged=True)
            updated.append(res["incident"])
        return {
            "acknowledged_count": len(updated),
            "incidents": updated,
        }

    def observe_http_flow(
        self,
        *,
        route_id: str,
        authority: str,
        raw_path: str,
        response_status: int | None,
        proxy_error: str | None,
        response_headers: dict[str, str] | None = None,
        duration_ms: float | None = None,
        capture_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Observes a completed/errored proxy HTTP flow and records/resolves incidents."""
        sim = self._read_sim_state()
        if sim["simulate_persistence_failure"]:
            raise NotificationsUnavailableError("Simulated persistence failure")

        domain = (authority or "").split(":", 1)[0].strip().lower()
        clean_route = str(route_id or "route:unknown").strip()
        resp_hdrs = {str(k).lower(): str(v) for k, v in (response_headers or {}).items()}
        has_echo_target = "x-echo-target" in resp_hdrs

        # 1. Check if route's target container is stopped when an upstream error occurs
        if response_status in (502, 503, 504) or proxy_error:
            try:
                route_info = self.store.routes.get_route(clean_route).get("route", {})
                if route_info.get("target_state") == "stopped":
                    up_host = route_info.get("upstream_host") or "target"
                    return self.record_normalized_event(
                        event_type="container_unavailable",
                        route_id=clean_route,
                        domain=domain,
                        reason=f"target_container_stopped_{up_host}",
                        message=f"Target container '{up_host}' is stopped for {clean_route} ({domain})",
                        status_code=response_status,
                        capture_id=capture_id,
                    )
            except Exception:
                pass

        # 2. Check for upstream timeout
        err_lower = (proxy_error or "").lower()
        is_timeout = (
            ("timeout" in err_lower or "timed out" in err_lower)
            or (raw_path.startswith("/__pc_fast_timeout") and response_status == 504)
            or (response_status == 504 and not has_echo_target)
            or resp_hdrs.get("x-upstream-timeout") in ("1", "true")
        )
        if is_timeout:
            return self.record_normalized_event(
                event_type="upstream_timeout",
                route_id=clean_route,
                domain=domain,
                reason="upstream_response_timeout",
                message=f"Upstream timeout on {clean_route} ({domain or 'unknown'})",
                status_code=response_status or 504,
                capture_id=capture_id,
            )

        # 3. Check for HTTP 502 / 503 / 504 or proxy connection error
        if response_status in (502, 503, 504) or (proxy_error and response_status is None):
            reason_code = (
                f"http_{response_status}"
                if response_status in (502, 503, 504)
                else "upstream_connection_error"
            )
            return self.record_normalized_event(
                event_type="http_502_503_504",
                route_id=clean_route,
                domain=domain,
                reason=reason_code,
                message=f"HTTP {response_status or 'gateway error'} on {clean_route} ({domain or 'unknown'})",
                status_code=response_status,
                capture_id=capture_id,
            )

        # 4. Check for HTTP 500 (500, 501, 505..599)
        if response_status is not None and 500 <= int(response_status) <= 599:
            return self.record_normalized_event(
                event_type="http_500",
                route_id=clean_route,
                domain=domain,
                reason=f"http_{int(response_status)}",
                message=f"HTTP {int(response_status)} application error on {clean_route} ({domain or 'unknown'})",
                status_code=int(response_status),
                capture_id=capture_id,
            )

        # 5. Check for HTTP 4xx (excluded by default per Rule 1 / AC-3)
        if response_status is not None and 400 <= int(response_status) < 500:
            cfg = self.get_config()
            if not cfg.get("notify_http_4xx", False):
                return {"ignored": True, "reason": "http_4xx_excluded_by_default"}

        # 6. Healthy 2xx/3xx response triggers recovery for open traffic/target incidents on this route
        if response_status is not None and 200 <= int(response_status) < 400 and not proxy_error:
            return self.resolve_open_incidents(
                route_id=clean_route,
                event_types=(
                    "http_500",
                    "http_502_503_504",
                    "upstream_timeout",
                    "container_unavailable",
                ),
                recovery_reason=f"upstream_healthy_http_{int(response_status)}",
            )

        return None

    def ingest_nginx_log_line(
        self,
        line: str,
        *,
        route_id: str | None = None,
        occurred_at: Any = None,
        skip_upstream_timeout_And_refused: bool = False,
    ) -> dict[str, Any] | None:
        """Parses an Nginx warn/error log line and records a normalized event."""
        clean_line = (line or "").strip()
        if not clean_line:
            return None
        m = _NGINX_LOG_LINE_RE.match(clean_line)
        if not m:
            return None
        level = m.group("level").lower()
        msg = m.group("msg").strip()

        # When tailing live router error.log, avoid duplicating upstream timed out
        # that `observe_http_flow` already classifies as `upstream_timeout`
        msg_lower = msg.lower()
        if skip_upstream_timeout_And_refused and "upstream timed out" in msg_lower:
            return None

        inferred_route = route_id or "route:router"
        host_match = re.search(r'host:\s*"([^"]+)"', msg, re.IGNORECASE)
        if not route_id and host_match:
            host_only = host_match.group(1).split(":", 1)[0].strip().lower()
            try:
                inferred_route = self.store.routes.resolve_route_id_for_host(host_only)
            except Exception:
                inferred_route = f"route:{host_only}"

        event_type = "nginx_warn" if level in ("warn", "warning") else "nginx_error"
        norm_reason = normalize_reason(event_type, msg)
        return self.record_normalized_event(
            event_type=event_type,
            route_id=inferred_route,
            reason=norm_reason,
            severity="warning" if event_type == "nginx_warn" else "error",
            message=f"Nginx [{level}]: {msg[:180]}",
            occurred_at=occurred_at,
        )

    def ingest_nginx_log_text(
        self,
        text: str,
        *,
        route_id: str | None = None,
        skip_upstream_timeout_And_refused: bool = False,
    ) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for raw_line in (text or "").splitlines():
            res = self.ingest_nginx_log_line(
                raw_line,
                route_id=route_id,
                skip_upstream_timeout_And_refused=skip_upstream_timeout_And_refused,
            )
            if res and not res.get("ignored"):
                results.append(res)
        return results

    def poll_router_logs_and_containers(self) -> None:
        """Background monitor that tails Nginx error logs and checks route container states."""
        sim = self._read_sim_state()
        if sim["simulate_unavailable"] or sim["simulate_persistence_failure"]:
            return

        # 1. Tail /var/lib/proxyconsole/router/nginx-error.log if present
        if self.nginx_log_path.is_file():
            try:
                offset = 0
                if self.nginx_offset_path.is_file():
                    try:
                        offset = int(self.nginx_offset_path.read_text(encoding="utf-8").strip() or "0")
                    except ValueError:
                        offset = 0
                file_size = self.nginx_log_path.stat().st_size
                if file_size < offset:
                    offset = 0
                if file_size > offset:
                    with self.nginx_log_path.open("rb") as f:
                        f.seek(offset)
                        new_bytes = f.read(file_size - offset)
                        new_offset = f.tell()
                    self.nginx_offset_path.write_text(str(new_offset), encoding="utf-8")
                    text = new_bytes.decode("utf-8", errors="replace")
                    self.ingest_nginx_log_text(
                        text,
                        skip_upstream_timeout_And_refused=True,
                    )
            except Exception:
                pass

        # 2. Check enabled routes for stopped / recovered target containers
        try:
            routes_data = self.store.routes.list_routes(enabled=True)
            for r in routes_data.get("items", []):
                r_id = str(r["route_id"])
                r_domain = str(r.get("domain") or "")
                t_state = str(r.get("target_state") or "unknown")
                up_host = str(r.get("upstream_host") or "target")
                if t_state == "stopped":
                    # Only record automatically if there is no open container_unavailable incident in the current window
                    reason = f"target_container_stopped_{up_host}"
                    fp = compute_fingerprint("container_unavailable", r_id, reason)
                    with self.store._connect() as conn:
                        existing = conn.execute(
                            """
                            SELECT incident_id FROM notification_incidents
                            WHERE route_id = ? AND fingerprint = ? AND status = 'open'
                            LIMIT 1
                            """,
                            (r_id, fp),
                        ).fetchone()
                    if existing is None:
                        self.record_normalized_event(
                            event_type="container_unavailable",
                            route_id=r_id,
                            domain=r_domain,
                            reason=reason,
                            message=f"Target container '{up_host}' is stopped for {r_id} ({r_domain})",
                        )
                elif t_state == "running":
                    self.resolve_open_incidents(
                        route_id=r_id,
                        event_types=("container_unavailable",),
                        recovery_reason=f"target_container_running_{up_host}",
                    )
        except Exception:
            pass

    def _expand_event_type_filter(self, raw_filter: str) -> tuple[list[str], str | None]:
        cleaned = raw_filter.strip().lower()
        if not cleaned or cleaned in ("all", "*"):
            return [], None
        specific_reason: str | None = None
        if cleaned in ("502", "http_502"):
            specific_reason = "http_502"
        elif cleaned in ("503", "http_503"):
            specific_reason = "http_503"
        elif cleaned in ("504", "http_504"):
            specific_reason = "http_504"
        types = list(EVENT_TYPE_ALIASES.get(cleaned, (cleaned,)))
        return types, specific_reason

    def list_incidents(
        self,
        *,
        status: str | None = None,
        event_type: str | None = None,
        route_id: str | None = None,
        fingerprint: str | None = None,
        acknowledged: bool | None = None,
        since: Any = None,
        until: Any = None,
        q: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        self.check_api_available()
        self.store.ensure_schema()

        clauses: list[str] = []
        params: list[Any] = []

        if status and status.strip().lower() not in ("all", "*"):
            clean_st = status.strip().lower()
            if clean_st not in ("open", "resolved"):
                raise NotificationValidationError(
                    "invalid_status_filter",
                    f"Invalid status filter {status!r}: must be 'open', 'resolved', or 'all'",
                )
            clauses.append("status = ?")
            params.append(clean_st)

        if event_type and event_type.strip().lower() not in ("all", "*"):
            types, specific_reason = self._expand_event_type_filter(event_type)
            if types:
                placeholders = ",".join("?" for _ in types)
                clauses.append(f"event_type IN ({placeholders})")
                params.extend(types)
            if specific_reason:
                clauses.append("normalized_reason LIKE ?")
                params.append(f"%{specific_reason}%")

        if route_id and route_id.strip().lower() not in ("all", "*"):
            clauses.append("(route_id = ? OR domain = ?)")
            params.extend([route_id.strip(), route_id.strip().lower()])

        if fingerprint and fingerprint.strip():
            clauses.append("fingerprint = ?")
            params.append(fingerprint.strip())

        if acknowledged is not None:
            clauses.append("acknowledged = ?")
            params.append(1 if acknowledged else 0)

        if since is not None and str(since).strip() != "":
            since_unix = _parse_timestamp_to_unix(since)
            clauses.append("last_seen_unix >= ?")
            params.append(since_unix)

        if until is not None and str(until).strip() != "":
            until_unix = _parse_timestamp_to_unix(until)
            clauses.append("first_seen_unix <= ?")
            params.append(until_unix)

        if q and q.strip():
            pat = f"%{q.strip()}%"
            clauses.append(
                "(incident_id LIKE ? OR route_id LIKE ? OR domain LIKE ? OR fingerprint LIKE ? OR normalized_reason LIKE ? OR message LIKE ?)"
            )
            params.extend([pat, pat, pat, pat, pat, pat])

        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        safe_limit = max(1, min(int(limit), 500))
        safe_offset = max(0, int(offset))

        with self.store._connect() as conn:
            total_row = conn.execute(
                f"SELECT COUNT(*) AS cnt FROM notification_incidents {where_sql}",
                params,
            ).fetchone()
            total = int(total_row["cnt"]) if total_row else 0

            counts_rows = conn.execute(
                """
                SELECT status, COUNT(*) AS cnt
                FROM notification_incidents
                GROUP BY status
                """
            ).fetchall()
            status_counts = {str(r["status"]): int(r["cnt"]) for r in counts_rows}

            unack_row = conn.execute(
                "SELECT COUNT(*) AS cnt FROM notification_incidents WHERE status = 'open' AND acknowledged = 0"
            ).fetchone()
            unack_open = int(unack_row["cnt"]) if unack_row else 0

            seq_row = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) AS max_seq FROM notification_events"
            ).fetchone()
            latest_seq = int(seq_row["max_seq"]) if seq_row else 0

            rows = conn.execute(
                f"""
                SELECT * FROM notification_incidents
                {where_sql}
                ORDER BY
                    CASE WHEN status = 'open' THEN 0 ELSE 1 END ASC,
                    last_seen_unix DESC,
                    incident_id DESC
                LIMIT ? OFFSET ?
                """,
                [*params, safe_limit, safe_offset],
            ).fetchall()

        items = [self._incident_row_to_dict(r) for r in rows]
        return {
            "total": total,
            "open_count": status_counts.get("open", 0),
            "resolved_count": status_counts.get("resolved", 0),
            "unacknowledged_open_count": unack_open,
            "latest_seq": latest_seq,
            "items": items,
        }

    def get_incident(self, incident_id: str) -> dict[str, Any]:
        self.check_api_available()
        self.store.ensure_schema()
        clean_id = str(incident_id or "").strip()
        with self.store._connect() as conn:
            row = conn.execute(
                "SELECT * FROM notification_incidents WHERE incident_id = ?",
                (clean_id,),
            ).fetchone()
            if row is None:
                raise NotificationNotFoundError(f"Incident not found: {clean_id}")
            ev_rows = conn.execute(
                "SELECT * FROM notification_events WHERE incident_id = ? ORDER BY seq ASC",
                (clean_id,),
            ).fetchall()
        return {
            "incident": self._incident_row_to_dict(row),
            "events": [self._event_row_to_dict(er) for er in ev_rows],
        }

    def list_events(
        self,
        *,
        since_seq: int | None = None,
        incident_id: str | None = None,
        status: str | None = None,
        event_type: str | None = None,
        route_id: str | None = None,
        fingerprint: str | None = None,
        kind: str | None = None,
        since: Any = None,
        until: Any = None,
        limit: int = 200,
    ) -> dict[str, Any]:
        self.check_api_available()
        self.store.ensure_schema()

        clauses: list[str] = []
        params: list[Any] = []

        if since_seq is not None:
            clauses.append("seq > ?")
            params.append(int(since_seq))

        if incident_id and incident_id.strip():
            clauses.append("incident_id = ?")
            params.append(incident_id.strip())

        if status and status.strip().lower() not in ("all", "*"):
            clauses.append("status = ?")
            params.append(status.strip().lower())

        if event_type and event_type.strip().lower() not in ("all", "*"):
            types, _ = self._expand_event_type_filter(event_type)
            if types:
                placeholders = ",".join("?" for _ in types)
                clauses.append(f"event_type IN ({placeholders})")
                params.extend(types)

        if route_id and route_id.strip().lower() not in ("all", "*"):
            clauses.append("(route_id = ? OR domain = ?)")
            params.extend([route_id.strip(), route_id.strip().lower()])

        if fingerprint and fingerprint.strip():
            clauses.append("fingerprint = ?")
            params.append(fingerprint.strip())

        if kind and kind.strip().lower() not in ("all", "*"):
            clauses.append("kind = ?")
            params.append(kind.strip().lower())

        if since is not None and str(since).strip() != "":
            clauses.append("occurred_at_unix >= ?")
            params.append(_parse_timestamp_to_unix(since))

        if until is not None and str(until).strip() != "":
            clauses.append("occurred_at_unix <= ?")
            params.append(_parse_timestamp_to_unix(until))

        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        safe_limit = max(1, min(int(limit), 1000))
        order_sql = "ORDER BY seq ASC" if since_seq is not None else "ORDER BY seq DESC"

        with self.store._connect() as conn:
            seq_row = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) AS max_seq FROM notification_events"
            ).fetchone()
            latest_seq = int(seq_row["max_seq"]) if seq_row else 0

            rows = conn.execute(
                f"""
                SELECT * FROM notification_events
                {where_sql}
                {order_sql}
                LIMIT ?
                """,
                [*params, safe_limit],
            ).fetchall()

        items = [self._event_row_to_dict(r) for r in rows]
        if since_seq is None:
            # Return chronological ascending or keep descending? Let's keep ascending by seq for cursor consistency!
            items.sort(key=lambda x: int(x["seq"]))
        return {
            "latest_seq": latest_seq,
            "total": len(items),
            "items": items,
        }

    def get_status(self) -> dict[str, Any]:
        self.check_api_available()
        self.store.ensure_schema()
        cfg = self.get_config()
        with self.store._connect() as conn:
            counts_rows = conn.execute(
                "SELECT status, COUNT(*) AS cnt FROM notification_incidents GROUP BY status"
            ).fetchall()
            status_counts = {str(r["status"]): int(r["cnt"]) for r in counts_rows}
            unack_row = conn.execute(
                "SELECT COUNT(*) AS cnt FROM notification_incidents WHERE status = 'open' AND acknowledged = 0"
            ).fetchone()
            unack_open = int(unack_row["cnt"]) if unack_row else 0
            ev_row = conn.execute(
                "SELECT COUNT(*) AS cnt, COALESCE(MAX(seq), 0) AS max_seq FROM notification_events"
            ).fetchone()
            total_events = int(ev_row["cnt"]) if ev_row else 0
            latest_seq = int(ev_row["max_seq"]) if ev_row else 0

        open_cnt = status_counts.get("open", 0)
        resolved_cnt = status_counts.get("resolved", 0)
        return {
            "available": True,
            "latest_seq": latest_seq,
            "open_incidents_count": open_cnt,
            "open_incidents": open_cnt,
            "resolved_incidents_count": resolved_cnt,
            "resolved_incidents": resolved_cnt,
            "unacknowledged_open_count": unack_open,
            "unacknowledged_open_incidents": unack_open,
            "total_incidents_count": open_cnt + resolved_cnt,
            "total_incidents": open_cnt + resolved_cnt,
            "total_events_count": total_events,
            "total_events": total_events,
            "config": cfg,
        }
