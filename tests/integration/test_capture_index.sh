#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
COMPOSE_FILE="${ROOT_DIR}/deploy/compose/docker-compose.yml"
CERT_FILE="${ROOT_DIR}/var/certificates/edge.crt"

if [[ -f "${ROOT_DIR}/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "${ROOT_DIR}/.env"
  set +a
fi

export EDGE_HTTPS_PORT="${EDGE_HTTPS_PORT:-8443}"
export CONTROL_API_PORT="${CONTROL_API_PORT:-18480}"
export APP_A_HOST="${APP_A_HOST:-app-a.local.test}"
export APP_B_HOST="${APP_B_HOST:-app-b.local.test}"
API_BASE="http://127.0.0.1:${CONTROL_API_PORT}"

echo "=================================================================="
echo "=== [DEV-010 1/8] Ensure stack is up & verify index import     ==="
echo "=================================================================="
"${ROOT_DIR}/scripts/up.sh"

python3 - "${API_BASE}" "${ROOT_DIR}" <<'PY'
import json
from pathlib import Path
import subprocess
import sys
import urllib.request

api_base = sys.argv[1]
root_dir = Path(sys.argv[2])

def api_get(path: str) -> dict:
    with urllib.request.urlopen(f"{api_base}{path}", timeout=10) as resp:
        assert resp.status == 200
        return json.loads(resp.read().decode("utf-8"))

def api_post(path: str) -> dict:
    req = urllib.request.Request(f"{api_base}{path}", method="POST", data=b"")
    with urllib.request.urlopen(req, timeout=15) as resp:
        assert resp.status == 200
        return json.loads(resp.read().decode("utf-8"))

status_before = api_get("/api/v1/index/status")
assert status_before["raw_capture_files"] > 0, f"Expected existing captures, got {status_before}"
assert status_before["total_indexed"] == status_before["raw_capture_files"]

# AC-2 & AC-3: Re-import multiple times via HTTP API and CLI; verify no duplicate rows
imp1 = api_post("/api/v1/index/import")
imp2 = api_post("/api/v1/index/import")
cli_imp = json.loads(
    subprocess.check_output([str(root_dir / "scripts/capture-ctl.sh"), "import"], text=True)
)
assert imp1["total_indexed"] == status_before["total_indexed"]
assert imp2["total_indexed"] == status_before["total_indexed"]
assert cli_imp["total_indexed"] == status_before["total_indexed"]
assert imp1["error_count"] == 0
print(f"PASS: Idempotent import verified (total_indexed={imp1['total_indexed']}, duplicates=0).")
PY

echo "=================================================================="
echo "=== [DEV-010 2/8] Verify new requests appear in search results ==="
echo "=================================================================="
python3 - "${API_BASE}" "${EDGE_HTTPS_PORT}" "${APP_A_HOST}" "${APP_B_HOST}" "${CERT_FILE}" <<'PY'
import json
import subprocess
import sys
import urllib.request

api_base, edge_port, app_a, app_b, cert_file = sys.argv[1:6]

hdr_out = subprocess.check_output(
    [
        "curl",
        "-sS",
        "-i",
        "--cacert",
        cert_file,
        "--resolve",
        f"{app_a}:{edge_port}:127.0.0.1",
        "https://" + f"{app_a}:{edge_port}/v1/dev010/live-search?check=new",
    ]
).decode("utf-8", errors="replace")

new_cid = None
for line in hdr_out.splitlines():
    if line.lower().startswith("x-capture-id:"):
        new_cid = line.split(":", 1)[1].strip()
assert new_cid, "Missing X-Capture-Id on new request"

with urllib.request.urlopen(f"{api_base}/api/v1/captures?limit=5", timeout=10) as resp:
    listing = json.loads(resp.read().decode("utf-8"))

assert listing["items"][0]["capture_id"] == new_cid, f"Expected newest capture {new_cid} first, got {listing['items'][0]['capture_id']}"
assert listing["items"][0]["raw_path"] == "/v1/dev010/live-search"
print(f"PASS: New request {new_cid} immediately reflected at top of search results.")
PY

echo "=================================================================="
echo "=== [DEV-010 3/8] Verify stable ordering (same timestamp)      ==="
echo "=================================================================="
python3 - "${API_BASE}" "${COMPOSE_FILE}" <<'PY'
import hashlib
import json
import subprocess
import sys
import urllib.request

api_base, compose_file = sys.argv[1:3]

fixed_ts = "2099-01-01T00:00:00.000000+00:00"
same_ts_ids = [
    "cap-20990101T000000000000Z-9001-aaaa1111",
    "cap-20990101T000000000000Z-9002-bbbb2222",
    "cap-20990101T000000000000Z-9003-cccc3333",
]

for cid in same_ts_ids:
    rec = {
        "schema_version": "v0",
        "capture_id": cid,
        "captured_at": fixed_ts,
        "route_id": "route:app-a",
        "capture_status": "complete",
        "replayable": True,
        "is_websocket": False,
        "websocket_handshake": None,
        "request": {
            "method": "GET",
            "scheme": "https",
            "authority": "app-a.local.test:8443",
            "raw_target": f"/v1/tie-break/{cid}",
            "raw_path": f"/v1/tie-break/{cid}",
            "raw_query": "",
            "http_version": "HTTP/1.1",
            "headers": [["Host", "app-a.local.test:8443"]],
            "trailers": [],
            "body": {
                "size": 0,
                "captured_size": 0,
                "sha256": hashlib.sha256(b"").hexdigest(),
                "blob_ref": None,
                "content_type": None,
                "content_encoding": None,
                "max_capture_bytes": 1048576,
            },
        },
        "forwarding": {
            "upstream": "http://router:8080",
            "target": "echo-a",
            "response_status": 200,
            "duration_ms": 1.0,
            "proxy_error": None,
        },
        "derived_views": {
            "content_kind": "empty",
            "query_params": [],
            "cookies_view": [],
            "json_view": None,
            "form_view": None,
            "multipart_view": None,
            "text_preview": None,
            "hex_preview": "",
            "parse_errors": [],
        },
    }
    subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            compose_file,
            "exec",
            "-T",
            "inspector",
            "sh",
            "-c",
            f"cat > /var/lib/proxyconsole/captures/{cid}.json",
        ],
        input=json.dumps(rec).encode("utf-8"),
        check=True,
    )

req = urllib.request.Request(f"{api_base}/api/v1/index/import", method="POST", data=b"")
urllib.request.urlopen(req, timeout=10).read()

with urllib.request.urlopen(f"{api_base}/api/v1/captures?limit=3&sort=desc", timeout=10) as resp:
    top3_desc = [item["capture_id"] for item in json.loads(resp.read().decode("utf-8"))["items"]]

assert top3_desc == list(reversed(same_ts_ids)), f"Expected deterministic desc tie-break {list(reversed(same_ts_ids))}, got {top3_desc}"

with urllib.request.urlopen(
    f"{api_base}/api/v1/captures?from_time={ urllib.parse.quote(fixed_ts) }&to_time={ urllib.parse.quote(fixed_ts) }&sort=asc",
    timeout=10,
) as resp:
    tie_asc = [item["capture_id"] for item in json.loads(resp.read().decode("utf-8"))["items"]]

assert tie_asc == same_ts_ids, f"Expected deterministic asc tie-break {same_ts_ids}, got {tie_asc}"
print("PASS: Deterministic newest-first ordering and tie-breaking on identical timestamps verified.")
PY

echo "=================================================================="
echo "=== [DEV-010 4/8] Verify pagination without gap or duplication ==="
echo "=================================================================="
python3 - "${API_BASE}" <<'PY'
import json
import sys
import urllib.request

api_base = sys.argv[1]

with urllib.request.urlopen(f"{api_base}/api/v1/captures?limit=500&offset=0", timeout=10) as resp:
    all_data = json.loads(resp.read().decode("utf-8"))

total = all_data["total"]
all_ids = [item["capture_id"] for item in all_data["items"]]
assert len(all_ids) == total and total >= 10, f"Expected >= 10 captures for pagination test, got {total}"

paged_ids = []
page_size = 3
offset = 0
while offset < total:
    with urllib.request.urlopen(
        f"{api_base}/api/v1/captures?limit={page_size}&offset={offset}&sort=desc",
        timeout=10,
    ) as resp:
        page_data = json.loads(resp.read().decode("utf-8"))
    assert page_data["total"] == total
    page_items = [item["capture_id"] for item in page_data["items"]]
    assert 0 < len(page_items) <= page_size
    paged_ids.extend(page_items)
    offset += page_size

assert len(set(paged_ids)) == len(paged_ids), "Duplicate capture_ids found across pages"
assert paged_ids == all_ids, "Paginated sequence does not match full ordered list"
print(f"PASS: Pagination across {total} records (page_size={page_size}) verified with 0 gaps and 0 duplicates.")
PY

echo "=================================================================="
echo "=== [DEV-010 5/8] Verify individual & combined filters         ==="
echo "=================================================================="
python3 - "${API_BASE}" "${ROOT_DIR}" <<'PY'
import json
from pathlib import Path
import subprocess
import sys
import urllib.parse
import urllib.request

api_base = sys.argv[1]
root_dir = Path(sys.argv[2])

def query_api(params: dict) -> dict:
    qs = urllib.parse.urlencode(params)
    with urllib.request.urlopen(f"{api_base}/api/v1/captures?{qs}", timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))

# 1. Host filter
res_host_b = query_api({"host": "app-b.local.test"})
assert res_host_b["total"] >= 2
assert all(i["host"] == "app-b.local.test" for i in res_host_b["items"])

# 2. Authority filter
res_auth_a = query_api({"authority": "app-a.local.test:8443"})
assert res_auth_a["total"] >= 3
assert all(i["authority"] == "app-a.local.test:8443" for i in res_auth_a["items"])

# 3. Method filter
res_post = query_api({"method": "POST"})
assert res_post["total"] >= 4
assert all(i["method"] == "POST" for i in res_post["items"])

# 4. Path filter
res_path = query_api({"path": "/v1/events"})
assert res_path["total"] >= 2
assert all(i["raw_path"] == "/v1/events" for i in res_path["items"])

# 5. Capture status filter
res_trunc = query_api({"capture_status": "truncated"})
assert res_trunc["total"] >= 1
assert all(i["capture_status"] == "truncated" for i in res_trunc["items"])

res_meta = query_api({"capture_status": "metadata-only"})
assert res_meta["total"] >= 1
assert all(i["capture_status"] == "metadata-only" for i in res_meta["items"])

# 6. Response status filter
res_404 = query_api({"response_status": 404})
assert res_404["total"] >= 1
assert all(i["response_status"] == 404 for i in res_404["items"])

res_101 = query_api({"response_status": 101})
assert res_101["total"] >= 1
assert all(i["response_status"] == 101 for i in res_101["items"])

# 7. Time range filter
sample_ts = res_path["items"][0]["captured_at"]
res_time = query_api({"from_time": sample_ts, "to_time": sample_ts})
assert res_time["total"] >= 1
assert any(i["capture_id"] == res_path["items"][0]["capture_id"] for i in res_time["items"])

# 8. Combined filter (time range + host + method + path + capture_status + response_status)
combined = query_api(
    {
        "from_time": "2020-01-01T00:00:00Z",
        "to_time": "2098-12-31T23:59:59Z",
        "host": "app-a.local.test",
        "method": "POST",
        "path": "/v1/events",
        "capture_status": "complete",
        "response_status": 200,
    }
)
assert combined["total"] >= 2
for item in combined["items"]:
    assert item["host"] == "app-a.local.test"
    assert item["method"] == "POST"
    assert item["raw_path"] == "/v1/events"
    assert item["capture_status"] == "complete"
    assert item["response_status"] == 200

# Also verify CLI search with combined filters matches API
cli_combined = json.loads(
    subprocess.check_output(
        [
            str(root_dir / "scripts/capture-ctl.sh"),
            "search",
            "--host",
            "app-a.local.test",
            "--method",
            "POST",
            "--path",
            "/v1/events",
            "--capture-status",
            "complete",
            "--response-status",
            "200",
        ],
        text=True,
    )
)
assert cli_combined["total"] == combined["total"]
print("PASS: Individual and combined filters (time, host, authority, method, path, capture_status, response_status) verified.")
PY

echo "=================================================================="
echo "=== [DEV-010 6/8] Verify detail interface & derived views      ==="
echo "=================================================================="
python3 - "${API_BASE}" <<'PY'
import json
import sys
import urllib.error
import urllib.request

api_base = sys.argv[1]

with urllib.request.urlopen(f"{api_base}/api/v1/captures?path=/v1/events&method=POST", timeout=10) as resp:
    events_list = json.loads(resp.read().decode("utf-8"))
target_cid = events_list["items"][0]["capture_id"]

with urllib.request.urlopen(f"{api_base}/api/v1/captures/{target_cid}", timeout=10) as resp:
    detail = json.loads(resp.read().decode("utf-8"))

assert detail["capture_id"] == target_cid
assert detail["indexed"] is True
assert detail["capture_status"] == "complete"
assert isinstance(detail["request"]["headers"], list)
assert detail["request"]["body"]["size"] > 0
assert len(detail["request"]["body"]["sha256"]) == 64
assert detail["request"]["body"]["blob_ref"] == f"blobs/{target_cid}.bin"
assert detail["derived_views"]["query_params"] == [["tag", "first"], ["tag", "second"]]
assert detail["derived_views"]["json_view"] == {"event": "OrderCreated", "count": 7, "tags": ["a", "b"]}
assert detail["reader_verification"]["full_body_sha256_matches"] is True

# Verify 404 for unknown capture_id
try:
    urllib.request.urlopen(f"{api_base}/api/v1/captures/cap-does-not-exist", timeout=10)
    raise AssertionError("Expected 404 for missing capture_id")
except urllib.error.HTTPError as err:
    assert err.code == 404

print("PASS: Detail interface returned ordered headers/query, body metadata/blob_ref, and derived views.")
PY

echo "=================================================================="
echo "=== [DEV-010 7/8] Verify restart persistence & index rebuild   ==="
echo "=================================================================="
docker compose -f "${COMPOSE_FILE}" restart inspector control-api

for _ in $(seq 1 30); do
  edge_code="$(
    curl -s -o /dev/null -w "%{http_code}" \
      --cacert "${CERT_FILE}" \
      --resolve "${APP_A_HOST}:${EDGE_HTTPS_PORT}:127.0.0.1" \
      "https://${APP_A_HOST}:${EDGE_HTTPS_PORT}/ready-after-dev010-restart" || true
  )"
  api_code="$(curl -s -o /dev/null -w "%{http_code}" "${API_BASE}/api/v1/health" || true)"
  if [[ "${edge_code}" == "200" && "${api_code}" == "200" ]]; then
    break
  fi
  sleep 0.5
done

python3 - "${API_BASE}" "${COMPOSE_FILE}" <<'PY'
import json
import subprocess
import sys
import urllib.request

api_base, compose_file = sys.argv[1:3]

with urllib.request.urlopen(f"{api_base}/api/v1/captures?limit=500", timeout=10) as resp:
    before_rebuild = json.loads(resp.read().decode("utf-8"))

snapshot_before = [
    (
        i["capture_id"],
        i["captured_at"],
        i["method"],
        i["authority"],
        i["raw_path"],
        i["body_size"],
        i["body_sha256"],
        i["capture_status"],
        i["response_status"],
    )
    for i in before_rebuild["items"]
]
assert len(snapshot_before) >= 10

# Delete SQLite DB files explicitly and trigger rebuild
subprocess.run(
    [
        "docker",
        "compose",
        "-f",
        compose_file,
        "exec",
        "-T",
        "inspector",
        "sh",
        "-c",
        "rm -f /var/lib/proxyconsole/database/captures.sqlite3*",
    ],
    check=True,
)

req = urllib.request.Request(f"{api_base}/api/v1/index/rebuild", method="POST", data=b"")
with urllib.request.urlopen(req, timeout=15) as resp:
    rebuild_res = json.loads(resp.read().decode("utf-8"))

assert rebuild_res["rebuilt"] is True
assert rebuild_res["total_indexed"] == before_rebuild["total"]

with urllib.request.urlopen(f"{api_base}/api/v1/captures?limit=500", timeout=10) as resp:
    after_rebuild = json.loads(resp.read().decode("utf-8"))

snapshot_after = [
    (
        i["capture_id"],
        i["captured_at"],
        i["method"],
        i["authority"],
        i["raw_path"],
        i["body_size"],
        i["body_sha256"],
        i["capture_status"],
        i["response_status"],
    )
    for i in after_rebuild["items"]
]
assert snapshot_after == snapshot_before, "Rebuilt index metadata differs from pre-rebuild snapshot"
print(f"PASS: Restart persistence and full index rebuild from raw captures verified ({rebuild_res['total_indexed']} records).")
PY

echo "=================================================================="
echo "=== [DEV-010 8/8] Verify corrupt capture & index fault safety  ==="
echo "=================================================================="
python3 - "${API_BASE}" "${COMPOSE_FILE}" "${EDGE_HTTPS_PORT}" "${APP_A_HOST}" "${CERT_FILE}" <<'PY'
import json
import subprocess
import sys
import urllib.request

api_base, compose_file, edge_port, app_a, cert_file = sys.argv[1:6]

# 1. Inject a corrupted capture JSON file on disk
subprocess.run(
    [
        "docker",
        "compose",
        "-f",
        compose_file,
        "exec",
        "-T",
        "inspector",
        "sh",
        "-c",
        "printf '{corrupt-json-file' > /var/lib/proxyconsole/captures/cap-corrupt-0001.json",
    ],
    check=True,
)

req = urllib.request.Request(f"{api_base}/api/v1/index/import", method="POST", data=b"")
with urllib.request.urlopen(req, timeout=15) as resp:
    imp_res = json.loads(resp.read().decode("utf-8"))

assert imp_res["error_count"] == 1, f"Expected 1 import error, got {imp_res}"
assert imp_res["import_errors"][0]["record_file"] == "captures/cap-corrupt-0001.json"

# Verify search queries still succeed and report the corrupt record count
with urllib.request.urlopen(f"{api_base}/api/v1/captures?limit=10", timeout=10) as resp:
    search_with_corrupt = json.loads(resp.read().decode("utf-8"))
assert search_with_corrupt["import_errors_count"] == 1
assert search_with_corrupt["total"] >= 10

# Verify proxy routing and new capture indexing continue working while corrupt file exists
live_out = subprocess.check_output(
    [
        "curl",
        "-sS",
        "-i",
        "--cacert",
        cert_file,
        "--resolve",
        f"{app_a}:{edge_port}:127.0.0.1",
        f"https://{app_a}:{edge_port}/v1/dev010/after-corrupt",
    ]
).decode("utf-8", errors="replace")
assert " 200 " in live_out.splitlines()[0]

# Remove corrupt file and verify import_errors clears
subprocess.run(
    [
        "docker",
        "compose",
        "-f",
        compose_file,
        "exec",
        "-T",
        "inspector",
        "rm",
        "-f",
        "/var/lib/proxyconsole/captures/cap-corrupt-0001.json",
    ],
    check=True,
)
req_clean = urllib.request.Request(f"{api_base}/api/v1/index/import", method="POST", data=b"")
with urllib.request.urlopen(req_clean, timeout=15) as resp:
    clean_res = json.loads(resp.read().decode("utf-8"))
assert clean_res["error_count"] == 0

print("PASS: Corrupted capture file isolated in import_errors without stopping queries or proxy routing.")
PY

"${ROOT_DIR}/scripts/capture-ctl.sh" sync

leaked_files="$(git -C "${ROOT_DIR}" status --porcelain | grep -E '(\.key$|\.pem$|\.crt$|\.mitm$|\.flow$|\.sqlite|\.sqlite3$|^.. var/)' || true)"
if [[ -n "${leaked_files}" ]]; then
  echo "FAIL: Secret, certificate, or runtime database appeared in git status:" >&2
  echo "${leaked_files}" >&2
  exit 1
fi

echo "ALL DEV-010 DURABLE INDEX & SEARCH TESTS PASSED."
