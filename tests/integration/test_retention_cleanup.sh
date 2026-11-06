#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
COMPOSE_FILE="${ROOT_DIR}/deploy/compose/docker-compose.yml"
EDGE_HTTPS_PORT="${EDGE_HTTPS_PORT:-8443}"
CONTROL_API_PORT="${CONTROL_API_PORT:-18480}"
API_BASE="http://127.0.0.1:${CONTROL_API_PORT}"
CACERT="${ROOT_DIR}/var/certificates/edge.crt"
REF_NOW="2026-11-07T12:00:00+00:00"

echo "==> [1/10] Starting clean stack and verifying default retention policy & byte scope"
"${ROOT_DIR}/scripts/down.sh" >/dev/null 2>&1 || true
rm -rf "${ROOT_DIR}/var/captures" "${ROOT_DIR}/var/blobs" "${ROOT_DIR}/var/database"
"${ROOT_DIR}/scripts/up.sh"

for _ in $(seq 1 30); do
  if curl -fsS "${API_BASE}/api/v1/health" >/dev/null 2>&1; then
    break
  fi
  sleep 1
done
curl -fsS "${API_BASE}/api/v1/health" >/dev/null

init_status="$(curl -fsS "${API_BASE}/api/v1/retention/status")"
INIT_JSON="${init_status}" python3 - <<'PY'
import json, os
data = json.loads(os.environ["INIT_JSON"])
pol = data["policy"]
st = data["storage"]
assert pol["min_retention_seconds"] == 86400, pol
assert pol["soft_limit_bytes"] == 21474836480, pol
assert pol["hard_limit_bytes"] == 26843545600, pol
assert pol["periodic_enabled"] is True, pol
assert "captures/*.json" in st["calculation_scope"], st
assert "blobs/*.bin" in st["calculation_scope"], st
assert "captures.sqlite3" in st["calculation_scope"], st
PY

# Temporarily disable background periodic timer while running deterministic manual/dry-run tests
curl -fsS -X PUT "${API_BASE}/api/v1/retention/policy" \
  -H "Content-Type: application/json" \
  -d '{"periodic_enabled": false, "periodic_interval_seconds": 60}' >/dev/null

echo "==> [2/10] Seeding synthetic dataset (complete, truncated, metadata-only, bodyless, tie-break timestamps)"
docker compose -f "${COMPOSE_FILE}" exec -T inspector gosu mitmproxy python3 - <<'PY'
import hashlib, json
from pathlib import Path
from index_store import CaptureIndexStore

storage = Path("/var/lib/proxyconsole")
cap_dir = storage / "captures"
blob_dir = storage / "blobs"
cap_dir.mkdir(parents=True, exist_ok=True)
blob_dir.mkdir(parents=True, exist_ok=True)

for f in cap_dir.glob("*.json"):
    f.unlink()
for f in blob_dir.glob("*.bin"):
    f.unlink()

def make_record(
    cid: str,
    captured_at: str,
    status: str,
    body_bytes: bytes,
    full_body_size: int | None = None,
    write_blob: bool = True,
):
    actual_full_size = full_body_size if full_body_size is not None else len(body_bytes)
    sha = hashlib.sha256(body_bytes).hexdigest()
    blob_ref = None
    captured_size = 0
    if write_blob and len(body_bytes) > 0:
        blob_ref = f"blobs/{cid}.bin"
        (blob_dir / f"{cid}.bin").write_bytes(body_bytes)
        captured_size = len(body_bytes)
    replayable = (status == "complete")
    rec = {
        "schema_version": "v0",
        "capture_id": cid,
        "captured_at": captured_at,
        "route_id": "app-a.local.test",
        "capture_status": status,
        "replayable": replayable,
        "is_websocket": False,
        "request": {
            "method": "POST" if actual_full_size > 0 else "GET",
            "scheme": "https",
            "authority": "app-a.local.test:8443",
            "raw_target": f"/retention/{cid}",
            "raw_path": f"/retention/{cid}",
            "raw_query": "",
            "http_version": "HTTP/1.1",
            "headers": [
                ["Host", "app-a.local.test:8443"],
                ["Content-Type", "application/octet-stream"],
            ],
            "trailers": [],
            "body": {
                "size": actual_full_size,
                "captured_size": captured_size,
                "sha256": sha,
                "blob_ref": blob_ref,
                "content_type": "application/octet-stream",
                "content_encoding": None,
            },
        },
        "forwarding": {
            "target": "http://router:8080",
            "response_status": 200,
            "duration_ms": 2.5,
            "proxy_error": None,
        },
        "derived_views": {
            "content_kind": "binary" if actual_full_size > 0 else "empty",
            "parse_errors": [],
        },
    }
    (cap_dir / f"{cid}.json").write_text(
        json.dumps(rec, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

# 1 & 2: Two 48h-old complete records with identical timestamp to verify tie-break (a before b)
make_record("cap_01_old_tie_b", "2026-11-05T12:00:00+00:00", "complete", b"B" * 10240)
make_record("cap_01_old_tie_a", "2026-11-05T12:00:00+00:00", "complete", b"A" * 10240)
# 3: 36h-old truncated record with 8 KiB partial blob
make_record("cap_02_old_trunc", "2026-11-06T00:00:00+00:00", "truncated", b"T" * 8192, full_body_size=16384, write_blob=True)
# 4: 30h-old metadata-only record (no blob on disk)
make_record("cap_03_old_meta", "2026-11-06T06:00:00+00:00", "metadata-only", b"M" * 16384, full_body_size=16384, write_blob=False)
# 5: 26h-old complete bodyless GET record (no blob on disk)
make_record("cap_04_old_nobdy", "2026-11-06T10:00:00+00:00", "complete", b"", full_body_size=0, write_blob=False)
# 6: 20h-old recent (<24h) complete record with 12 KiB blob
make_record("cap_05_recent_20h", "2026-11-06T16:00:00+00:00", "complete", b"R" * 12288)
# 7: 2h-old recent (<24h) complete record with 12 KiB blob
make_record("cap_06_recent_02h", "2026-11-07T10:00:00+00:00", "complete", b"N" * 12288)

store = CaptureIndexStore()
store.rebuild_index()
store.retention.checkpoint_wal()
PY

echo "==> [3/10] Verifying no records are deleted when usage <= soft target (AC-2)"
status_seeded="$(curl -fsS "${API_BASE}/api/v1/retention/status")"
total_seeded="$(STATUS_JSON="${status_seeded}" python3 -c 'import json, os; d = json.loads(os.environ["STATUS_JSON"]); assert d["storage"]["captures_count"] == 7; print(d["storage"]["total_bytes"])')"

soft_high=$((total_seeded + 10000))
hard_high=$((total_seeded + 50000))
curl -fsS -X PUT "${API_BASE}/api/v1/retention/policy" \
  -H "Content-Type: application/json" \
  -d "{\"min_retention_seconds\": 86400, \"soft_limit_bytes\": ${soft_high}, \"hard_limit_bytes\": ${hard_high}, \"periodic_enabled\": false}" >/dev/null

run_under_soft="$(curl -fsS -X POST "${API_BASE}/api/v1/retention/run" \
  -H "Content-Type: application/json" \
  -d "{\"now\": \"${REF_NOW}\"}")"
RUN_JSON="${run_under_soft}" python3 - <<'PY'
import json, os
res = json.loads(os.environ["RUN_JSON"])
assert res["deleted_captures_count"] == 0, res
assert res["remaining_captures_count"] == 7, res
assert res["freed_bytes"] == 0, res
assert "within_soft_limit" in res["reasons"], res
PY

echo "==> [4/10] Verifying dry-run non-mutation (AC-9) and soft target expired cleanup + tie-break (AC-3, AC-6, AC-7)"
# Calculate threshold so the 3 oldest expired records (cap_01_old_tie_a, cap_01_old_tie_b, cap_02_old_trunc) must be deleted
thresholds_step4="$(docker compose -f "${COMPOSE_FILE}" exec -T inspector gosu mitmproxy python3 - <<'PY'
from pathlib import Path
from index_store import CaptureIndexStore
store = CaptureIndexStore()
st = store.retention.get_storage_usage()
cap_dir = store.captures_dir
blob_dir = store.blobs_dir
def rec_size(cid: str) -> int:
    j = (cap_dir / f"{cid}.json").stat().st_size
    b_path = blob_dir / f"{cid}.bin"
    b = b_path.stat().st_size if b_path.is_file() else 0
    return j + b

s_tie_a = rec_size("cap_01_old_tie_a")
s_tie_b = rec_size("cap_01_old_tie_b")
s_trunc = rec_size("cap_02_old_trunc")
freed_3 = s_tie_a + s_tie_b + s_trunc
# Setting soft_limit_bytes to (total_bytes - freed_3 + 100) requires deleting all 3 oldest records
soft_target = st["total_bytes"] - freed_3 + 100
hard_limit = st["total_bytes"] + 50000
print(f"{soft_target} {hard_limit} {freed_3}")
PY
)"
read -r soft_step4 hard_step4 expected_freed_step4 <<< "${thresholds_step4}"

curl -fsS -X PUT "${API_BASE}/api/v1/retention/policy" \
  -H "Content-Type: application/json" \
  -d "{\"min_retention_seconds\": 86400, \"soft_limit_bytes\": ${soft_step4}, \"hard_limit_bytes\": ${hard_step4}, \"periodic_enabled\": false}" >/dev/null

dry_run_step4="$("${ROOT_DIR}/scripts/capture-ctl.sh" retention-run --dry-run --now "${REF_NOW}")"
DRY_JSON="${dry_run_step4}" EXP_FREED="${expected_freed_step4}" SOFT_LIM="${soft_step4}" python3 - <<'PY'
import json, os
res = json.loads(os.environ["DRY_JSON"])
assert res["dry_run"] is True, res
assert res["deleted_capture_ids"] == [
    "cap_01_old_tie_a",
    "cap_01_old_tie_b",
    "cap_02_old_trunc",
], f"Unexpected deletion order: {res['deleted_capture_ids']}"
assert res["freed_bytes"] == int(os.environ["EXP_FREED"]), res
assert res["bytes_after"] <= int(os.environ["SOFT_LIM"]), res
assert "soft_limit_and_expired" in res["reasons"], res
# Verify truncated record byte accounting in dry-run output
trunc_entry = [r for r in res["deleted_captures"] if r["capture_id"] == "cap_02_old_trunc"][0]
assert trunc_entry["capture_status"] == "truncated", trunc_entry
assert trunc_entry["blob_bytes"] == 8192, trunc_entry
PY

# Verify dry-run did NOT modify filesystem or SQLite index
post_dry_status="$(curl -fsS "${API_BASE}/api/v1/retention/status")"
POST_DRY_JSON="${post_dry_status}" python3 - <<'PY'
import json, os
st = json.loads(os.environ["POST_DRY_JSON"])["storage"]
assert st["captures_count"] == 7, st
assert st["blobs_count"] == 5, st
assert st["indexed_count"] == 7, st
PY

# Now execute real manual cleanup
exec_step4="$(curl -fsS -X POST "${API_BASE}/api/v1/retention/run" \
  -H "Content-Type: application/json" \
  -d "{\"dry_run\": false, \"now\": \"${REF_NOW}\"}")"
EXEC_JSON="${exec_step4}" EXP_FREED="${expected_freed_step4}" SOFT_LIM="${soft_step4}" python3 - <<'PY'
import json, os
res = json.loads(os.environ["EXEC_JSON"])
assert res["dry_run"] is False, res
assert res["deleted_capture_ids"] == [
    "cap_01_old_tie_a",
    "cap_01_old_tie_b",
    "cap_02_old_trunc",
], res
assert res["retained_capture_ids"] == [
    "cap_03_old_meta",
    "cap_04_old_nobdy",
    "cap_05_recent_20h",
    "cap_06_recent_02h",
], res
assert res["freed_bytes"] == int(os.environ["EXP_FREED"]), res
assert res["bytes_after"] <= int(os.environ["SOFT_LIM"]), res
assert res["storage_after"]["captures_count"] == 4, res
assert res["storage_after"]["blobs_count"] == 2, res
assert res["storage_after"]["indexed_count"] == 4, res
PY

echo "==> [5/10] Verifying <24h records are retained even when exceeding soft target if < hard limit (AC-4, AC-7)"
# Set soft_limit_bytes very small (e.g. 1024 bytes) while keeping hard_limit_bytes above total_bytes.
# First run will delete remaining >24h bodyless records (cap_03_old_meta, cap_04_old_nobdy) and keep <24h records (cap_05_recent_20h, cap_06_recent_02h).
curl -fsS -X PUT "${API_BASE}/api/v1/retention/policy" \
  -H "Content-Type: application/json" \
  -d "{\"min_retention_seconds\": 86400, \"soft_limit_bytes\": 1024, \"hard_limit_bytes\": ${hard_step4}, \"periodic_enabled\": false}" >/dev/null

exec_step5a="$(curl -fsS -X POST "${API_BASE}/api/v1/retention/run" \
  -H "Content-Type: application/json" \
  -d "{\"now\": \"${REF_NOW}\"}")"
EXEC_5A="${exec_step5a}" python3 - <<'PY'
import json, os
res = json.loads(os.environ["EXEC_5A"])
assert res["deleted_capture_ids"] == ["cap_03_old_meta", "cap_04_old_nobdy"], res
for r in res["deleted_captures"]:
    assert r["blob_bytes"] == 0, r
    assert r["record_bytes"] > 0, r
assert res["retained_capture_ids"] == ["cap_05_recent_20h", "cap_06_recent_02h"], res
assert res["bytes_after"] > 1024, res
PY

# Second run with only <24h records remaining: 0 deleted, reason is within_min_retention_window
exec_step5b="$(curl -fsS -X POST "${API_BASE}/api/v1/retention/run" \
  -H "Content-Type: application/json" \
  -d "{\"now\": \"${REF_NOW}\"}")"
EXEC_5B="${exec_step5b}" python3 - <<'PY'
import json, os
res = json.loads(os.environ["EXEC_5B"])
assert res["deleted_captures_count"] == 0, res
assert res["retained_capture_ids"] == ["cap_05_recent_20h", "cap_06_recent_02h"], res
assert "within_min_retention_window" in res["reasons"], res
PY

echo "==> [6/10] Verifying hard limit overrides 24h retention and deletes oldest <24h record (AC-5)"
hard_step6="$(docker compose -f "${COMPOSE_FILE}" exec -T inspector gosu mitmproxy python3 - <<'PY'
from index_store import CaptureIndexStore
store = CaptureIndexStore()
st = store.retention.get_storage_usage()
j = (store.captures_dir / "cap_05_recent_20h.json").stat().st_size
b = (store.blobs_dir / "cap_05_recent_20h.bin").stat().st_size
# Setting hard_limit_bytes to (total_bytes - (j + b) + 100) forces deleting cap_05_recent_20h while keeping cap_06_recent_02h
print(st["total_bytes"] - (j + b) + 100)
PY
)"

curl -fsS -X PUT "${API_BASE}/api/v1/retention/policy" \
  -H "Content-Type: application/json" \
  -d "{\"min_retention_seconds\": 86400, \"soft_limit_bytes\": 1024, \"hard_limit_bytes\": ${hard_step6}, \"periodic_enabled\": false}" >/dev/null

exec_step6="$(curl -fsS -X POST "${API_BASE}/api/v1/retention/run" \
  -H "Content-Type: application/json" \
  -d "{\"now\": \"${REF_NOW}\"}")"
EXEC_6="${exec_step6}" HARD_LIM="${hard_step6}" python3 - <<'PY'
import json, os
res = json.loads(os.environ["EXEC_6"])
assert res["deleted_capture_ids"] == ["cap_05_recent_20h"], res
assert res["deleted_captures"][0]["reason"] == "hard_limit_exceeded", res
assert res["retained_capture_ids"] == ["cap_06_recent_02h"], res
assert res["bytes_after"] <= int(os.environ["HARD_LIM"]), res
assert "hard_limit_exceeded" in res["reasons"], res
assert res["storage_after"]["captures_count"] == 1, res
assert res["storage_after"]["blobs_count"] == 1, res
assert res["storage_after"]["indexed_count"] == 1, res
PY

echo "==> [7/10] Verifying missing blob, orphan blob, corrupt capture & dangling index reconciliation (AC-8, Rule 6)"
# Raise limits so capacity doesn't delete valid records during anomaly reconciliation test
curl -fsS -X PUT "${API_BASE}/api/v1/retention/policy" \
  -H "Content-Type: application/json" \
  -d '{"min_retention_seconds": 86400, "soft_limit_bytes": 10485760, "hard_limit_bytes": 20971520, "periodic_enabled": false}' >/dev/null

docker compose -f "${COMPOSE_FILE}" exec -T inspector gosu mitmproxy python3 - <<'PY'
import hashlib, json
from pathlib import Path
from index_store import CaptureIndexStore

store = CaptureIndexStore()
cap_dir = store.captures_dir
blob_dir = store.blobs_dir

# 1. Valid capture JSON whose referenced blob file is missing on disk
missing_rec = {
    "schema_version": "v0",
    "capture_id": "cap_07_missing_blob",
    "captured_at": "2026-11-07T11:00:00+00:00",
    "route_id": "app-a.local.test",
    "capture_status": "complete",
    "replayable": True,
    "is_websocket": False,
    "request": {
        "method": "POST",
        "scheme": "https",
        "authority": "app-a.local.test:8443",
        "raw_target": "/retention/missing-blob",
        "raw_path": "/retention/missing-blob",
        "raw_query": "",
        "http_version": "HTTP/1.1",
        "headers": [["Host", "app-a.local.test:8443"]],
        "trailers": [],
        "body": {
            "size": 512,
            "captured_size": 512,
            "sha256": hashlib.sha256(b"X" * 512).hexdigest(),
            "blob_ref": "blobs/cap_07_missing_blob.bin",
            "content_type": "text/plain",
            "content_encoding": None,
        },
    },
    "forwarding": {
        "target": "http://router:8080",
        "response_status": 200,
        "duration_ms": 1.0,
        "proxy_error": None,
    },
    "derived_views": {"content_kind": "text", "parse_errors": []},
}
(cap_dir / "cap_07_missing_blob.json").write_text(
    json.dumps(missing_rec, indent=2) + "\n", encoding="utf-8"
)
store.index_record_dict(missing_rec, "captures/cap_07_missing_blob.json")

# 2. Orphan blob on disk with no capture JSON
(blob_dir / "orphan_9999.bin").write_bytes(b"O" * 4096)

# 3. Corrupt capture JSON on disk
(cap_dir / "corrupt_bad.json").write_text("{corrupt_json: true\n", encoding="utf-8")

# 4. Dangling index row (simulating crash after deleting JSON/blob but before index row removal)
ghost_rec = dict(missing_rec)
ghost_rec["capture_id"] = "cap_ghost_dangling"
ghost_rec["request"] = dict(missing_rec["request"])
ghost_rec["request"]["body"] = dict(missing_rec["request"]["body"])
ghost_rec["request"]["body"]["blob_ref"] = None
store.index_record_dict(ghost_rec, "captures/cap_ghost_dangling.json")
store.retention.checkpoint_wal()
PY

# Preview anomaly handling in dry-run first
anom_dry="$(curl -fsS -X POST "${API_BASE}/api/v1/retention/run" \
  -H "Content-Type: application/json" \
  -d "{\"dry_run\": true, \"now\": \"${REF_NOW}\"}")"
ANOM_DRY="${anom_dry}" python3 - <<'PY'
import json, os
res = json.loads(os.environ["ANOM_DRY"])
assert res["deleted_orphan_blobs_count"] == 1, res
assert res["orphan_blobs"][0]["blob_file"] == "blobs/orphan_9999.bin", res
assert res["deleted_corrupt_captures_count"] == 1, res
assert res["corrupt_captures"][0]["record_file"] == "captures/corrupt_bad.json", res
assert res["missing_blobs_count"] == 1, res
assert res["missing_blobs"][0]["capture_id"] == "cap_07_missing_blob", res
assert res["dangling_index_removed_count"] == 1, res
assert res["dangling_index_capture_ids"] == ["cap_ghost_dangling"], res
PY

# Execute anomaly cleanup & reconciliation
anom_exec="$(curl -fsS -X POST "${API_BASE}/api/v1/retention/run" \
  -H "Content-Type: application/json" \
  -d "{\"dry_run\": false, \"now\": \"${REF_NOW}\"}")"
ANOM_EXEC="${anom_exec}" python3 - <<'PY'
import json, os
res = json.loads(os.environ["ANOM_EXEC"])
assert res["status"] == "ok", res
assert res["deleted_orphan_blobs_count"] == 1, res
assert res["deleted_corrupt_captures_count"] == 1, res
assert res["missing_blobs_count"] == 1, res
assert res["missing_blobs"][0]["action"] == "reconciled", res
assert res["dangling_index_removed_count"] == 1, res
assert "orphan_blob_cleanup" in res["reasons"], res
assert "corrupt_capture_cleanup" in res["reasons"], res
assert "missing_blob_reconciled" in res["reasons"], res
assert "dangling_index_reconciled" in res["reasons"], res
PY

# Verify reconciled record detail via API works without error and has replayable=false, blob_ref=null
reconciled_detail="$(curl -fsS "${API_BASE}/api/v1/captures/cap_07_missing_blob")"
REC_DETAIL="${reconciled_detail}" python3 - <<'PY'
import json, os
d = json.loads(os.environ["REC_DETAIL"])
assert d["capture_status"] == "capture-failed", d
assert d["replayable"] is False, d
assert d["request"]["body"]["blob_ref"] is None, d
assert d["request"]["body"]["captured_size"] == 0, d
PY

echo "==> [8/10] Verifying periodic automatic cleanup uses shared engine and policy (AC-10)"
# Seed an expired capture with a timestamp >24h before real wall-clock time so periodic worker deletes it
docker compose -f "${COMPOSE_FILE}" exec -T inspector gosu mitmproxy python3 - <<'PY'
import hashlib, json
from datetime import datetime, timedelta, timezone
from index_store import CaptureIndexStore

store = CaptureIndexStore()
old_iso = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
body = b"P" * 16384
cid = "cap_08_periodic_expired"
(store.blobs_dir / f"{cid}.bin").write_bytes(body)
rec = {
    "schema_version": "v0",
    "capture_id": cid,
    "captured_at": old_iso,
    "route_id": "app-b.local.test",
    "capture_status": "complete",
    "replayable": True,
    "is_websocket": False,
    "request": {
        "method": "POST",
        "scheme": "https",
        "authority": "app-b.local.test:8443",
        "raw_target": "/retention/periodic",
        "raw_path": "/retention/periodic",
        "raw_query": "",
        "http_version": "HTTP/1.1",
        "headers": [["Host", "app-b.local.test:8443"]],
        "trailers": [],
        "body": {
            "size": len(body),
            "captured_size": len(body),
            "sha256": hashlib.sha256(body).hexdigest(),
            "blob_ref": f"blobs/{cid}.bin",
            "content_type": "application/octet-stream",
            "content_encoding": None,
        },
    },
    "forwarding": {
        "target": "http://router:8080",
        "response_status": 200,
        "duration_ms": 1.2,
        "proxy_error": None,
    },
    "derived_views": {"content_kind": "binary", "parse_errors": []},
}
(store.captures_dir / f"{cid}.json").write_text(
    json.dumps(rec, indent=2) + "\n", encoding="utf-8"
)
store.index_record_dict(rec, f"captures/{cid}.json")
store.retention.checkpoint_wal()
PY

soft_periodic="$(docker compose -f "${COMPOSE_FILE}" exec -T inspector gosu mitmproxy python3 - <<'PY'
from index_store import CaptureIndexStore
store = CaptureIndexStore()
st = store.retention.get_storage_usage()
j = (store.captures_dir / "cap_08_periodic_expired.json").stat().st_size
b = (store.blobs_dir / "cap_08_periodic_expired.bin").stat().st_size
print(st["total_bytes"] - (j + b) + 200)
PY
)"

# Enable periodic worker with 1.0s interval
curl -fsS -X PUT "${API_BASE}/api/v1/retention/policy" \
  -H "Content-Type: application/json" \
  -d "{\"min_retention_seconds\": 86400, \"soft_limit_bytes\": ${soft_periodic}, \"hard_limit_bytes\": 20971520, \"periodic_interval_seconds\": 1.0, \"periodic_enabled\": true}" >/dev/null

periodic_ok=0
for _ in $(seq 1 15); do
  st_json="$(curl -fsS "${API_BASE}/api/v1/retention/status")"
  if ST_JSON="${st_json}" python3 -c '
import json, os, sys
d = json.loads(os.environ["ST_JSON"])
runs = [r for r in d["recent_runs"] if r["trigger_source"] == "periodic" and "cap_08_periodic_expired" in r["details"].get("deleted_capture_ids", [])]
sys.exit(0 if runs else 1)
'; then
    periodic_ok=1
    break
  fi
  sleep 1
done

if [[ "${periodic_ok}" -ne 1 ]]; then
  echo "ERROR: Periodic cleanup did not delete cap_08_periodic_expired within expected window" >&2
  exit 1
fi

echo "==> [9/10] Verifying policy and run history persistence across container restarts (AC-11)"
docker compose -f "${COMPOSE_FILE}" restart inspector control-api >/dev/null

for _ in $(seq 1 30); do
  if curl -fsS "${API_BASE}/api/v1/health" >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

after_restart_status="$(curl -fsS "${API_BASE}/api/v1/retention/status")"
RESTART_JSON="${after_restart_status}" SOFT_PER="${soft_periodic}" python3 - <<'PY'
import json, os
d = json.loads(os.environ["RESTART_JSON"])
pol = d["policy"]
assert pol["soft_limit_bytes"] == int(os.environ["SOFT_PER"]), pol
assert pol["periodic_enabled"] is True, pol
runs = d["recent_runs"]
assert len(runs) >= 5, f"Expected >= 5 persisted runs, got {len(runs)}"
sources = {r["trigger_source"] for r in runs}
assert "manual" in sources and "periodic" in sources, sources
PY

echo "==> [10/10] Restoring default retention policy and verifying live proxy routing & capture"
curl -fsS -X PUT "${API_BASE}/api/v1/retention/policy" \
  -H "Content-Type: application/json" \
  -d '{"min_retention_seconds": 86400, "soft_limit_bytes": 21474836480, "hard_limit_bytes": 26843545600, "periodic_interval_seconds": 60, "periodic_enabled": true}' >/dev/null

live_resp=""
for _ in $(seq 1 30); do
  if live_resp="$(curl -fsS --cacert "${CACERT}" \
    --resolve "app-a.local.test:${EDGE_HTTPS_PORT}:127.0.0.1" \
    "https://app-a.local.test:${EDGE_HTTPS_PORT}/retention-final-check?ok=1" 2>/dev/null)"; then
    break
  fi
  sleep 1
done
LIVE_JSON="${live_resp}" python3 -c 'import json, os; d = json.loads(os.environ["LIVE_JSON"]); assert d["target"] == "echo-a" and d["path"] == "/retention-final-check"'

"${ROOT_DIR}/scripts/capture-ctl.sh" sync

leaked_files="$(git -C "${ROOT_DIR}" status --porcelain | grep -E '(\.key$|\.pem$|\.crt$|\.mitm$|\.flow$|\.sqlite|\.sqlite3$|^.. var/)' || true)"
if [[ -n "${leaked_files}" ]]; then
  echo "FAIL: Secret, certificate, or runtime database appeared in git status:" >&2
  echo "${leaked_files}" >&2
  exit 1
fi

echo "==> All DEV-011 retention & cleanup integration checks passed!"
