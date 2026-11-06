#!/usr/bin/env python3
"""Retention policy and automatic cleanup engine for HTTP Capture Contract v0 (DEV-011).

Maintains consistency across raw capture JSON files (`captures/*.json`), raw body
blobs (`blobs/*.bin`), and the durable SQLite metadata index (`captures_index`)
while enforcing:
  1. Minimum retention window (default 24 hours / 86400 seconds).
  2. Soft storage target (default 20 GiB): when exceeded, deletes expired records
     (older than min_retention_seconds) in deterministic oldest-first order
     (`captured_at_unix_us ASC, capture_id ASC`) until usage <= soft_limit_bytes.
  3. Hard storage limit (default 25 GiB): takes precedence over min_retention_seconds;
     when exceeded, deletes records in deterministic oldest-first order regardless
     of age until usage <= hard_limit_bytes.
  4. Anomaly reconciliation: identifies and cleans/reconciles missing blobs,
     orphan blobs, corrupt capture files, and dangling index rows without stopping
     cleanup or proxy traffic.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterator

if TYPE_CHECKING:
    from index_store import CaptureIndexStore

DEFAULT_MIN_RETENTION_SECONDS = int(
    os.environ.get("RETENTION_MIN_SECONDS", "86400")
)
DEFAULT_SOFT_LIMIT_BYTES = int(
    os.environ.get("RETENTION_SOFT_LIMIT_BYTES", str(20 * 1024 * 1024 * 1024))
)
DEFAULT_HARD_LIMIT_BYTES = int(
    os.environ.get("RETENTION_HARD_LIMIT_BYTES", str(25 * 1024 * 1024 * 1024))
)
DEFAULT_PERIODIC_INTERVAL_SECONDS = float(
    os.environ.get("RETENTION_PERIODIC_INTERVAL_SECONDS", "60.0")
)
DEFAULT_PERIODIC_ENABLED = os.environ.get(
    "RETENTION_PERIODIC_ENABLED", "1"
).strip().lower() not in ("0", "false", "no")

CALCULATION_SCOPE = (
    "captures/*.json + blobs/*.bin + database/captures.sqlite3 (+ -wal, -shm)"
)


def _parse_iso_to_unix_us(iso_str: str) -> int:
    s = iso_str.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return int(round(dt.timestamp() * 1_000_000))


def _unix_us_to_iso(unix_us: int) -> str:
    return datetime.fromtimestamp(unix_us / 1_000_000.0, tz=timezone.utc).isoformat()


class RetentionEngine:
    def __init__(self, store: CaptureIndexStore) -> None:
        self.store = store
        self.lock_path = self.store.db_dir / "retention.lock"

    @contextmanager
    def _exclusive_lock(self, enabled: bool = True) -> Iterator[None]:
        if not enabled:
            yield
            return
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

    def checkpoint_wal(self) -> None:
        if not self.store.db_path.exists():
            return
        try:
            with self.store._connect() as conn:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
        except Exception:
            pass

    def get_policy(self) -> dict[str, Any]:
        self.store.ensure_schema()
        with self.store._connect() as conn:
            row = conn.execute(
                """
                SELECT
                    min_retention_seconds,
                    soft_limit_bytes,
                    hard_limit_bytes,
                    periodic_interval_seconds,
                    periodic_enabled,
                    updated_at
                FROM retention_policy
                WHERE id = 1
                """
            ).fetchone()
        if row is None:
            return {
                "min_retention_seconds": DEFAULT_MIN_RETENTION_SECONDS,
                "soft_limit_bytes": DEFAULT_SOFT_LIMIT_BYTES,
                "hard_limit_bytes": DEFAULT_HARD_LIMIT_BYTES,
                "periodic_interval_seconds": DEFAULT_PERIODIC_INTERVAL_SECONDS,
                "periodic_enabled": DEFAULT_PERIODIC_ENABLED,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
        return {
            "min_retention_seconds": int(row["min_retention_seconds"]),
            "soft_limit_bytes": int(row["soft_limit_bytes"]),
            "hard_limit_bytes": int(row["hard_limit_bytes"]),
            "periodic_interval_seconds": float(row["periodic_interval_seconds"]),
            "periodic_enabled": bool(row["periodic_enabled"]),
            "updated_at": str(row["updated_at"]),
        }

    def update_policy(
        self,
        *,
        min_retention_seconds: int | None = None,
        soft_limit_bytes: int | None = None,
        hard_limit_bytes: int | None = None,
        periodic_interval_seconds: float | None = None,
        periodic_enabled: bool | None = None,
    ) -> dict[str, Any]:
        current = self.get_policy()
        new_min = (
            int(min_retention_seconds)
            if min_retention_seconds is not None
            else current["min_retention_seconds"]
        )
        new_soft = (
            int(soft_limit_bytes)
            if soft_limit_bytes is not None
            else current["soft_limit_bytes"]
        )
        new_hard = (
            int(hard_limit_bytes)
            if hard_limit_bytes is not None
            else current["hard_limit_bytes"]
        )
        new_interval = (
            float(periodic_interval_seconds)
            if periodic_interval_seconds is not None
            else current["periodic_interval_seconds"]
        )
        new_enabled = (
            bool(periodic_enabled)
            if periodic_enabled is not None
            else current["periodic_enabled"]
        )

        if new_min < 0:
            raise ValueError("min_retention_seconds must be >= 0")
        if new_soft < 0:
            raise ValueError("soft_limit_bytes must be >= 0")
        if new_hard < new_soft:
            raise ValueError(
                f"hard_limit_bytes ({new_hard}) must be >= soft_limit_bytes ({new_soft})"
            )
        if new_interval <= 0:
            raise ValueError("periodic_interval_seconds must be > 0")

        now_iso = datetime.now(timezone.utc).isoformat()
        with self.store._connect() as conn:
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
                    new_min,
                    new_soft,
                    new_hard,
                    new_interval,
                    1 if new_enabled else 0,
                    now_iso,
                ),
            )
            conn.commit()
        self.checkpoint_wal()
        return self.get_policy()

    def get_storage_usage(self, *, ensure_db: bool = True) -> dict[str, Any]:
        if ensure_db:
            self.store.ensure_schema()

        captures_files = (
            sorted(self.store.captures_dir.glob("*.json"))
            if self.store.captures_dir.is_dir()
            else []
        )
        blobs_files = (
            sorted(self.store.blobs_dir.glob("*.bin"))
            if self.store.blobs_dir.is_dir()
            else []
        )

        captures_bytes = sum(
            f.stat().st_size for f in captures_files if f.is_file()
        )
        blobs_bytes = sum(
            f.stat().st_size for f in blobs_files if f.is_file()
        )

        db_files = [
            Path(str(self.store.db_path) + suffix)
            for suffix in ("", "-wal", "-shm")
        ]
        index_db_bytes = sum(
            f.stat().st_size for f in db_files if f.is_file()
        )

        indexed_count = 0
        if self.store.db_path.is_file():
            try:
                with self.store._connect() as conn:
                    indexed_count = int(
                        conn.execute(
                            "SELECT COUNT(*) AS cnt FROM captures_index"
                        ).fetchone()["cnt"]
                    )
            except Exception:
                indexed_count = 0

        return {
            "captures_bytes": captures_bytes,
            "blobs_bytes": blobs_bytes,
            "data_bytes": captures_bytes + blobs_bytes,
            "index_db_bytes": index_db_bytes,
            "total_bytes": captures_bytes + blobs_bytes + index_db_bytes,
            "captures_count": len(captures_files),
            "blobs_count": len(blobs_files),
            "indexed_count": indexed_count,
            "calculation_scope": CALCULATION_SCOPE,
        }

    def get_recent_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        self.store.ensure_schema()
        safe_limit = max(1, min(int(limit), 200))
        with self.store._connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    run_id, ran_at, reference_now, trigger_source, dry_run,
                    status, reasons_json, deleted_captures_count,
                    deleted_orphan_blobs_count, deleted_corrupt_captures_count,
                    missing_blobs_count, dangling_index_removed_count,
                    freed_bytes, bytes_before, bytes_after,
                    remaining_captures_count, policy_snapshot_json, details_json
                FROM retention_runs
                ORDER BY run_id DESC
                LIMIT ?
                """,
                (safe_limit,),
            ).fetchall()

        runs: list[dict[str, Any]] = []
        for r in rows:
            try:
                reasons = json.loads(r["reasons_json"])
            except Exception:
                reasons = []
            try:
                policy_snap = json.loads(r["policy_snapshot_json"])
            except Exception:
                policy_snap = {}
            try:
                details = json.loads(r["details_json"])
            except Exception:
                details = {}
            runs.append(
                {
                    "run_id": int(r["run_id"]),
                    "ran_at": r["ran_at"],
                    "reference_now": r["reference_now"],
                    "trigger_source": r["trigger_source"],
                    "dry_run": bool(r["dry_run"]),
                    "status": r["status"],
                    "reasons": reasons,
                    "deleted_captures_count": int(r["deleted_captures_count"]),
                    "deleted_orphan_blobs_count": int(
                        r["deleted_orphan_blobs_count"]
                    ),
                    "deleted_corrupt_captures_count": int(
                        r["deleted_corrupt_captures_count"]
                    ),
                    "missing_blobs_count": int(r["missing_blobs_count"]),
                    "dangling_index_removed_count": int(
                        r["dangling_index_removed_count"]
                    ),
                    "freed_bytes": int(r["freed_bytes"]),
                    "bytes_before": int(r["bytes_before"]),
                    "bytes_after": int(r["bytes_after"]),
                    "remaining_captures_count": int(
                        r["remaining_captures_count"]
                    ),
                    "policy": policy_snap,
                    "details": details,
                }
            )
        return runs

    def get_status(self, *, runs_limit: int = 20) -> dict[str, Any]:
        policy = self.get_policy()
        storage = self.get_storage_usage(ensure_db=True)
        recent_runs = self.get_recent_runs(limit=runs_limit)
        return {
            "policy": policy,
            "storage": storage,
            "recent_runs": recent_runs,
            "last_run": recent_runs[0] if recent_runs else None,
        }

    def run_cleanup(
        self,
        *,
        dry_run: bool = False,
        now: str | None = None,
        trigger_source: str = "manual",
    ) -> dict[str, Any]:
        ran_at_iso = datetime.now(timezone.utc).isoformat()
        if now:
            ref_now_us = _parse_iso_to_unix_us(now)
            ref_now_iso = _unix_us_to_iso(ref_now_us)
        else:
            ref_now_us = _parse_iso_to_unix_us(ran_at_iso)
            ref_now_iso = ran_at_iso

        with self._exclusive_lock(enabled=not dry_run):
            if not dry_run:
                self.store.ensure_schema()
                self.checkpoint_wal()

            policy = self.get_policy()
            min_retention_seconds = int(policy["min_retention_seconds"])
            soft_limit_bytes = int(policy["soft_limit_bytes"])
            hard_limit_bytes = int(policy["hard_limit_bytes"])

            storage_before = self.get_storage_usage(ensure_db=not dry_run)
            bytes_before = int(storage_before["total_bytes"])

            valid_records: list[dict[str, Any]] = []
            corrupt_captures: list[dict[str, Any]] = []
            referenced_blobs: set[str] = set()
            valid_capture_ids_on_disk: set[str] = set()

            captures_files = (
                sorted(self.store.captures_dir.glob("*.json"))
                if self.store.captures_dir.is_dir()
                else []
            )

            for json_path in captures_files:
                if not json_path.is_file():
                    continue
                rel_record_file = f"captures/{json_path.name}"
                try:
                    json_size = json_path.stat().st_size
                except OSError:
                    continue

                try:
                    raw_text = json_path.read_text(encoding="utf-8")
                    rec = json.loads(raw_text)
                    if not isinstance(rec, dict):
                        raise ValueError("Root JSON value must be an object")
                    _ = self.store._extract_row_params(rec, rel_record_file)
                    capture_id = str(rec["capture_id"])
                    if capture_id in valid_capture_ids_on_disk:
                        raise ValueError(
                            f"Duplicate capture_id on disk: {capture_id}"
                        )
                except Exception as exc:
                    corrupt_captures.append(
                        {
                            "record_file": rel_record_file,
                            "size_bytes": json_size,
                            "error_type": type(exc).__name__,
                            "error_message": str(exc),
                            "action": "would_delete" if dry_run else "deleted",
                            "_path": json_path,
                        }
                    )
                    continue

                valid_capture_ids_on_disk.add(capture_id)
                captured_at = str(rec["captured_at"])
                captured_at_us = _parse_iso_to_unix_us(captured_at)
                age_seconds = round(
                    (ref_now_us - captured_at_us) / 1_000_000.0, 6
                )
                capture_status = str(rec.get("capture_status") or "complete")
                body = rec["request"]["body"]
                body_size = int(body.get("size", 0))
                captured_body_size = int(body.get("captured_size", body_size))
                blob_ref = body.get("blob_ref")

                blob_path: Path | None = None
                blob_size = 0
                has_missing_blob = False

                if blob_ref:
                    rel_blob = str(blob_ref).lstrip("/")
                    referenced_blobs.add(rel_blob)
                    candidate_blob = self.store.storage_dir / rel_blob
                    if candidate_blob.is_file():
                        blob_path = candidate_blob
                        blob_size = candidate_blob.stat().st_size
                    else:
                        has_missing_blob = True
                elif (
                    capture_status in ("complete", "truncated")
                    and captured_body_size > 0
                ):
                    has_missing_blob = True

                valid_records.append(
                    {
                        "capture_id": capture_id,
                        "captured_at": captured_at,
                        "captured_at_unix_us": captured_at_us,
                        "age_seconds": age_seconds,
                        "capture_status": capture_status,
                        "replayable": bool(rec.get("replayable")),
                        "body_size": body_size,
                        "captured_body_size": captured_body_size,
                        "blob_ref": blob_ref,
                        "json_path": json_path,
                        "rel_record_file": rel_record_file,
                        "blob_path": blob_path,
                        "record_bytes": json_size,
                        "blob_bytes": blob_size,
                        "total_bytes": json_size + blob_size,
                        "has_missing_blob": has_missing_blob,
                        "raw_record": rec,
                    }
                )

            # Identify orphan blobs (blobs/*.bin not referenced by any valid capture record)
            orphan_blobs: list[dict[str, Any]] = []
            blobs_files = (
                sorted(self.store.blobs_dir.glob("*.bin"))
                if self.store.blobs_dir.is_dir()
                else []
            )
            for bin_path in blobs_files:
                if not bin_path.is_file():
                    continue
                rel_blob = f"blobs/{bin_path.name}"
                if rel_blob not in referenced_blobs:
                    orphan_blobs.append(
                        {
                            "blob_file": rel_blob,
                            "size_bytes": bin_path.stat().st_size,
                            "action": "would_delete" if dry_run else "deleted",
                            "_path": bin_path,
                        }
                    )

            # Identify dangling index rows & unindexed valid captures
            indexed_ids_in_db: set[str] = set()
            if self.store.db_path.is_file():
                try:
                    with self.store._connect() as conn:
                        rows = conn.execute(
                            "SELECT capture_id FROM captures_index"
                        ).fetchall()
                        indexed_ids_in_db = {
                            str(r["capture_id"]) for r in rows
                        }
                except Exception:
                    indexed_ids_in_db = set()

            dangling_index_capture_ids = sorted(
                cid
                for cid in indexed_ids_in_db
                if cid not in valid_capture_ids_on_disk
            )
            unindexed_capture_ids = {
                r["capture_id"]
                for r in valid_records
                if r["capture_id"] not in indexed_ids_in_db
            }

            # Deterministic oldest-first sort (Rule 5: captured_at_unix_us ASC, capture_id ASC)
            valid_records.sort(
                key=lambda r: (r["captured_at_unix_us"], r["capture_id"])
            )

            corrupt_freed_bytes = sum(c["size_bytes"] for c in corrupt_captures)
            orphan_freed_bytes = sum(o["size_bytes"] for o in orphan_blobs)
            projected_bytes = (
                bytes_before - corrupt_freed_bytes - orphan_freed_bytes
            )

            deleted_captures: list[dict[str, Any]] = []
            retained_captures: list[dict[str, Any]] = []

            for r in valid_records:
                is_expired = r["age_seconds"] > min_retention_seconds
                reason: str | None = None
                if projected_bytes > hard_limit_bytes:
                    reason = "hard_limit_exceeded"
                elif projected_bytes > soft_limit_bytes and is_expired:
                    reason = "soft_limit_and_expired"

                if reason is not None:
                    projected_bytes -= r["total_bytes"]
                    deleted_captures.append(
                        {
                            "capture_id": r["capture_id"],
                            "captured_at": r["captured_at"],
                            "captured_at_unix_us": r["captured_at_unix_us"],
                            "age_seconds": r["age_seconds"],
                            "capture_status": r["capture_status"],
                            "body_size": r["body_size"],
                            "captured_body_size": r["captured_body_size"],
                            "blob_ref": r["blob_ref"],
                            "record_file": r["rel_record_file"],
                            "record_bytes": r["record_bytes"],
                            "blob_bytes": r["blob_bytes"],
                            "total_bytes": r["total_bytes"],
                            "has_missing_blob": r["has_missing_blob"],
                            "reason": reason,
                            "_json_path": r["json_path"],
                            "_blob_path": r["blob_path"],
                        }
                    )
                else:
                    retained_captures.append(r)

            deleted_ids_set = {r["capture_id"] for r in deleted_captures}

            missing_blobs: list[dict[str, Any]] = []
            for r in valid_records:
                if r["has_missing_blob"]:
                    if r["capture_id"] in deleted_ids_set:
                        mb_action = (
                            "would_delete_with_capture"
                            if dry_run
                            else "deleted_with_capture"
                        )
                    else:
                        mb_action = (
                            "would_reconcile" if dry_run else "reconciled"
                        )
                    missing_blobs.append(
                        {
                            "capture_id": r["capture_id"],
                            "record_file": r["rel_record_file"],
                            "missing_blob_ref": r["blob_ref"],
                            "action": mb_action,
                        }
                    )

            reasons: list[str] = []
            if any(
                r["reason"] == "hard_limit_exceeded" for r in deleted_captures
            ):
                reasons.append("hard_limit_exceeded")
            if any(
                r["reason"] == "soft_limit_and_expired"
                for r in deleted_captures
            ):
                reasons.append("soft_limit_and_expired")
            if orphan_blobs:
                reasons.append("orphan_blob_cleanup")
            if corrupt_captures:
                reasons.append("corrupt_capture_cleanup")
            if missing_blobs:
                reasons.append("missing_blob_reconciled")
            if dangling_index_capture_ids:
                reasons.append("dangling_index_reconciled")
            if not deleted_captures:
                if projected_bytes <= soft_limit_bytes:
                    reasons.append("within_soft_limit")
                else:
                    reasons.append("within_min_retention_window")

            captures_freed_bytes = sum(
                r["total_bytes"] for r in deleted_captures
            )
            planned_freed_bytes = (
                corrupt_freed_bytes + orphan_freed_bytes + captures_freed_bytes
            )
            estimated_bytes_after = max(0, bytes_before - planned_freed_bytes)

            public_deleted_captures = [
                {
                    k: v
                    for k, v in r.items()
                    if not k.startswith("_")
                }
                for r in deleted_captures
            ]
            public_orphan_blobs = [
                {
                    k: v
                    for k, v in o.items()
                    if not k.startswith("_")
                }
                for o in orphan_blobs
            ]
            public_corrupt_captures = [
                {
                    k: v
                    for k, v in c.items()
                    if not k.startswith("_")
                }
                for c in corrupt_captures
            ]

            deleted_capture_ids = [r["capture_id"] for r in deleted_captures]
            retained_capture_ids = [r["capture_id"] for r in retained_captures]

            if dry_run:
                return {
                    "run_id": None,
                    "ran_at": ran_at_iso,
                    "reference_now": ref_now_iso,
                    "trigger_source": trigger_source,
                    "dry_run": True,
                    "status": "ok",
                    "reasons": reasons,
                    "policy": policy,
                    "deleted_captures_count": len(public_deleted_captures),
                    "deleted_capture_ids": deleted_capture_ids,
                    "deleted_captures": public_deleted_captures,
                    "deleted_orphan_blobs_count": len(public_orphan_blobs),
                    "orphan_blobs": public_orphan_blobs,
                    "deleted_corrupt_captures_count": len(
                        public_corrupt_captures
                    ),
                    "corrupt_captures": public_corrupt_captures,
                    "missing_blobs_count": len(missing_blobs),
                    "missing_blobs": missing_blobs,
                    "dangling_index_removed_count": len(
                        dangling_index_capture_ids
                    ),
                    "dangling_index_capture_ids": dangling_index_capture_ids,
                    "freed_bytes": planned_freed_bytes,
                    "bytes_before": bytes_before,
                    "bytes_after": estimated_bytes_after,
                    "remaining_captures_count": len(retained_capture_ids),
                    "retained_capture_ids": retained_capture_ids,
                    "storage_before": storage_before,
                    "storage_after": {
                        **storage_before,
                        "captures_bytes": max(
                            0,
                            storage_before["captures_bytes"]
                            - corrupt_freed_bytes
                            - sum(r["record_bytes"] for r in deleted_captures),
                        ),
                        "blobs_bytes": max(
                            0,
                            storage_before["blobs_bytes"]
                            - orphan_freed_bytes
                            - sum(r["blob_bytes"] for r in deleted_captures),
                        ),
                        "total_bytes": estimated_bytes_after,
                        "captures_count": len(retained_capture_ids),
                        "indexed_count": len(retained_capture_ids),
                    },
                }

            # Execute filesystem deletions (raw JSON first, then blob, then index row)
            for r in deleted_captures:
                json_p: Path = r["_json_path"]
                blob_p: Path | None = r["_blob_path"]
                json_p.unlink(missing_ok=True)
                if blob_p is not None:
                    blob_p.unlink(missing_ok=True)

            for o in orphan_blobs:
                o["_path"].unlink(missing_ok=True)

            for c in corrupt_captures:
                c["_path"].unlink(missing_ok=True)

            # Reconcile retained captures with missing blobs on disk
            for r in retained_captures:
                if r["has_missing_blob"]:
                    rec = dict(r["raw_record"])
                    rec["capture_status"] = "capture-failed"
                    rec["replayable"] = False
                    req_obj = dict(rec.get("request") or {})
                    body_obj = dict(req_obj.get("body") or {})
                    body_obj["blob_ref"] = None
                    body_obj["captured_size"] = 0
                    req_obj["body"] = body_obj
                    rec["request"] = req_obj
                    r["raw_record"] = rec
                    r["json_path"].write_text(
                        json.dumps(rec, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8",
                    )

            # Synchronize SQLite index
            with self.store._connect() as conn:
                if deleted_captures:
                    conn.executemany(
                        "DELETE FROM captures_index WHERE capture_id = ?",
                        [(r["capture_id"],) for r in deleted_captures],
                    )
                    conn.executemany(
                        "DELETE FROM capture_import_errors WHERE record_file = ?",
                        [(r["record_file"],) for r in deleted_captures],
                    )
                if dangling_index_capture_ids:
                    conn.executemany(
                        "DELETE FROM captures_index WHERE capture_id = ?",
                        [(cid,) for cid in dangling_index_capture_ids],
                    )
                if corrupt_captures:
                    conn.executemany(
                        "DELETE FROM capture_import_errors WHERE record_file = ?",
                        [(c["record_file"],) for c in corrupt_captures],
                    )
                for r in retained_captures:
                    if (
                        r["capture_id"] in unindexed_capture_ids
                        or r["has_missing_blob"]
                    ):
                        params = self.store._extract_row_params(
                            r["raw_record"], r["rel_record_file"]
                        )
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
                conn.commit()

            self.checkpoint_wal()
            storage_after = self.get_storage_usage(ensure_db=False)
            bytes_after = int(storage_after["total_bytes"])

            details_obj = {
                "deleted_capture_ids": deleted_capture_ids,
                "deleted_captures": public_deleted_captures,
                "orphan_blobs": public_orphan_blobs,
                "corrupt_captures": public_corrupt_captures,
                "missing_blobs": missing_blobs,
                "dangling_index_capture_ids": dangling_index_capture_ids,
                "retained_capture_ids": retained_capture_ids,
            }

            with self.store._connect() as conn:
                cur = conn.execute(
                    """
                    INSERT INTO retention_runs (
                        ran_at, reference_now, trigger_source, dry_run,
                        status, reasons_json, deleted_captures_count,
                        deleted_orphan_blobs_count, deleted_corrupt_captures_count,
                        missing_blobs_count, dangling_index_removed_count,
                        freed_bytes, bytes_before, bytes_after,
                        remaining_captures_count, policy_snapshot_json, details_json
                    ) VALUES (?, ?, ?, 0, 'ok', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        ran_at_iso,
                        ref_now_iso,
                        trigger_source,
                        json.dumps(reasons, ensure_ascii=False),
                        len(public_deleted_captures),
                        len(public_orphan_blobs),
                        len(public_corrupt_captures),
                        len(missing_blobs),
                        len(dangling_index_capture_ids),
                        planned_freed_bytes,
                        bytes_before,
                        bytes_after,
                        len(retained_capture_ids),
                        json.dumps(policy, ensure_ascii=False),
                        json.dumps(details_obj, ensure_ascii=False),
                    ),
                )
                run_id = int(cur.lastrowid or 0)
                conn.execute(
                    """
                    DELETE FROM retention_runs
                    WHERE run_id NOT IN (
                        SELECT run_id FROM retention_runs ORDER BY run_id DESC LIMIT 50
                    )
                    """
                )
                conn.commit()

            self.checkpoint_wal()
            storage_after = self.get_storage_usage(ensure_db=False)
            if int(storage_after["total_bytes"]) != bytes_after:
                bytes_after = int(storage_after["total_bytes"])
                with self.store._connect() as conn:
                    conn.execute(
                        "UPDATE retention_runs SET bytes_after = ? WHERE run_id = ?",
                        (bytes_after, run_id),
                    )
                    conn.commit()
                self.checkpoint_wal()

            return {
                "run_id": run_id,
                "ran_at": ran_at_iso,
                "reference_now": ref_now_iso,
                "trigger_source": trigger_source,
                "dry_run": False,
                "status": "ok",
                "reasons": reasons,
                "policy": policy,
                "deleted_captures_count": len(public_deleted_captures),
                "deleted_capture_ids": deleted_capture_ids,
                "deleted_captures": public_deleted_captures,
                "deleted_orphan_blobs_count": len(public_orphan_blobs),
                "orphan_blobs": public_orphan_blobs,
                "deleted_corrupt_captures_count": len(public_corrupt_captures),
                "corrupt_captures": public_corrupt_captures,
                "missing_blobs_count": len(missing_blobs),
                "missing_blobs": missing_blobs,
                "dangling_index_removed_count": len(dangling_index_capture_ids),
                "dangling_index_capture_ids": dangling_index_capture_ids,
                "freed_bytes": planned_freed_bytes,
                "bytes_before": bytes_before,
                "bytes_after": bytes_after,
                "remaining_captures_count": len(retained_capture_ids),
                "retained_capture_ids": retained_capture_ids,
                "storage_before": storage_before,
                "storage_after": storage_after,
            }
