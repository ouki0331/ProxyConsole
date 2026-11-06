#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
COMPOSE_FILE="${ROOT_DIR}/deploy/compose/docker-compose.yml"
EDGE_HTTPS_PORT="${EDGE_HTTPS_PORT:-8443}"
CONTROL_API_PORT="${CONTROL_API_PORT:-18480}"
API_BASE="http://127.0.0.1:${CONTROL_API_PORT}"
CACERT="${ROOT_DIR}/var/certificates/edge.crt"

echo "==> [1/10] Starting clean stack and verifying seeded routes (app-a, app-b)"
"${ROOT_DIR}/scripts/down.sh" >/dev/null 2>&1 || true
rm -rf "${ROOT_DIR}/var/captures" "${ROOT_DIR}/var/blobs" "${ROOT_DIR}/var/database"
"${ROOT_DIR}/scripts/up.sh"

for _ in $(seq 1 30); do
  if curl -fsS "${API_BASE}/api/v1/health" >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

routes_init="$(curl -fsS "${API_BASE}/api/v1/routes")"
ROUTES_INIT="${routes_init}" python3 - <<'PY'
import json, os
d = json.loads(os.environ["ROUTES_INIT"])
assert d["total"] == 2, d
ids = [r["route_id"] for r in d["items"]]
assert ids == ["route:app-a", "route:app-b"], ids
assert d["apply_status"]["in_sync"] is True, d
PY

echo "==> [2/10] Creating manual route, sharing upstream across domains, and updating upstream (AC-2, AC-4, AC-7)"
create_c="$(curl -fsS -X POST "${API_BASE}/api/v1/routes" \
  -H "Content-Type: application/json" \
  -d '{
    "route_id": "route:custom-c",
    "domain": "app-c.local.test",
    "upstream_scheme": "http",
    "upstream_host": "echo-a",
    "upstream_port": 8080,
    "enabled": true,
    "description": "Custom route sharing echo-a"
  }')"
CREATE_C="${create_c}" python3 - <<'PY'
import json, os
d = json.loads(os.environ["CREATE_C"])
assert d["desired_state_saved"] is True, d
assert d["route"]["route_id"] == "route:custom-c", d
assert d["route"]["domain"] == "app-c.local.test", d
assert d["apply_result"]["applied"] is True, d
assert d["apply_result"]["status"] == "applied", d
assert d["apply_result"]["in_sync"] is True, d
PY

# Verify both app-a.local.test and app-c.local.test route to echo-a
resp_a="$(curl -fsS --cacert "${CACERT}" --resolve "app-a.local.test:${EDGE_HTTPS_PORT}:127.0.0.1" "https://app-a.local.test:${EDGE_HTTPS_PORT}/shared-check")"
resp_c1="$(curl -fsS --cacert "${CACERT}" --resolve "app-c.local.test:${EDGE_HTTPS_PORT}:127.0.0.1" "https://app-c.local.test:${EDGE_HTTPS_PORT}/shared-check")"
RESP_A="${resp_a}" RESP_C1="${resp_c1}" python3 - <<'PY'
import json, os
da = json.loads(os.environ["RESP_A"])
dc = json.loads(os.environ["RESP_C1"])
assert da["target"] == "echo-a", da
assert dc["target"] == "echo-a", dc
assert dc["host"].startswith("app-c.local.test"), dc
PY

# Update route:custom-c upstream from echo-a to echo-b
update_c="$(curl -fsS -X PUT "${API_BASE}/api/v1/routes/route:custom-c" \
  -H "Content-Type: application/json" \
  -d '{"upstream_host": "echo-b", "upstream_port": 8080}')"
UPDATE_C="${update_c}" python3 -c 'import json, os; d = json.loads(os.environ["UPDATE_C"]); assert d["apply_result"]["applied"] is True and d["route"]["upstream_host"] == "echo-b"'

resp_c2="$(curl -fsS --cacert "${CACERT}" --resolve "app-c.local.test:${EDGE_HTTPS_PORT}:127.0.0.1" "https://app-c.local.test:${EDGE_HTTPS_PORT}/updated-upstream")"
RESP_C2="${resp_c2}" python3 -c 'import json, os; dc = json.loads(os.environ["RESP_C2"]); assert dc["target"] == "echo-b", dc'

echo "==> [3/10] Copying route to new domain and verifying shared upstream routing (AC-3, AC-4, Rule 4)"
copy_a="$("${ROOT_DIR}/scripts/capture-ctl.sh" route-copy "route:app-a" --domain "app-a-clone.local.test" --route-id "route:app-a-clone")"
COPY_A="${copy_a}" python3 - <<'PY'
import json, os
d = json.loads(os.environ["COPY_A"])
assert d["copied_from"] == "route:app-a", d
assert d["route"]["route_id"] == "route:app-a-clone", d
assert d["route"]["domain"] == "app-a-clone.local.test", d
assert d["route"]["upstream_host"] == "echo-a", d
assert d["apply_result"]["applied"] is True, d
PY

resp_clone="$(curl -fsS --cacert "${CACERT}" --resolve "app-a-clone.local.test:${EDGE_HTTPS_PORT}:127.0.0.1" "https://app-a-clone.local.test:${EDGE_HTTPS_PORT}/clone-hello")"
RESP_CLONE="${resp_clone}" python3 -c 'import json, os; d = json.loads(os.environ["RESP_CLONE"]); assert d["target"] == "echo-a" and d["path"] == "/clone-hello"'

echo "==> [4/10] Verifying enable/disable live routing reflection (AC-5, Rule 3)"
dis_res="$(curl -fsS -X POST "${API_BASE}/api/v1/routes/route:app-a-clone/disable" -H "Content-Type: application/json" -d '{}')"
DIS_RES="${dis_res}" python3 -c 'import json, os; d = json.loads(os.environ["DIS_RES"]); assert d["route"]["enabled"] is False and d["apply_result"]["applied"] is True'

# Disabled route must still exist in desired state but return 404 unknown_host on live proxy
get_dis="$(curl -fsS "${API_BASE}/api/v1/routes/route:app-a-clone")"
GET_DIS="${get_dis}" python3 -c 'import json, os; d = json.loads(os.environ["GET_DIS"]); assert d["route"]["enabled"] is False'

dis_http_code="$(curl -sS -o /tmp/pc_dis_body.json -w "%{http_code}" --cacert "${CACERT}" \
  --resolve "app-a-clone.local.test:${EDGE_HTTPS_PORT}:127.0.0.1" \
  "https://app-a-clone.local.test:${EDGE_HTTPS_PORT}/when-disabled")"
assert_dis_code="${dis_http_code}"
if [[ "${assert_dis_code}" != "404" ]]; then
  echo "ERROR: Expected 404 for disabled route, got ${assert_dis_code}" >&2
  exit 1
fi
python3 -c 'import json; d = json.load(open("/tmp/pc_dis_body.json")); assert d["error"] == "unknown_host"'
rm -f /tmp/pc_dis_body.json

# Re-enable via CLI and verify live routing returns 200 again
"${ROOT_DIR}/scripts/capture-ctl.sh" route-enable "route:app-a-clone" >/dev/null
resp_reenabled="$(curl -fsS --cacert "${CACERT}" --resolve "app-a-clone.local.test:${EDGE_HTTPS_PORT}:127.0.0.1" "https://app-a-clone.local.test:${EDGE_HTTPS_PORT}/reenabled")"
RESP_RE="${resp_reenabled}" python3 -c 'import json, os; d = json.loads(os.environ["RESP_RE"]); assert d["target"] == "echo-a"'

echo "==> [5/10] Verifying validation errors: duplicate domain, copy without new domain, invalid scheme/port/host (AC-6)"
python3 - "${API_BASE}" <<'PY'
import json, sys, urllib.error, urllib.request

api_base = sys.argv[1]

def post_expect_err(path: str, payload: dict, expected_codes: tuple[int, ...], expected_err: str):
    req = urllib.request.Request(
        f"{api_base}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=10)
        raise AssertionError(f"Expected error {expected_codes} for {path} {payload}, but succeeded")
    except urllib.error.HTTPError as exc:
        body = json.loads(exc.read().decode("utf-8"))
        assert exc.code in expected_codes, f"Expected {expected_codes}, got {exc.code}: {body}"
        assert body.get("error") == expected_err, f"Expected {expected_err}, got {body}"

# 1. Duplicate domain on create
post_expect_err(
    "/api/v1/routes",
    {"domain": "app-a.local.test", "upstream_scheme": "http", "upstream_host": "echo-a", "upstream_port": 8080},
    (409,),
    "duplicate_domain",
)

# 2. Copy without new domain (Rule 4)
post_expect_err(
    "/api/v1/routes/route:app-a/copy",
    {},
    (400,),
    "invalid_route",
)

# 3. Copy with identical domain to source (Rule 4)
post_expect_err(
    "/api/v1/routes/route:app-a/copy",
    {"domain": "app-a.local.test"},
    (409,),
    "duplicate_domain",
)

# 4. Invalid upstream_scheme
post_expect_err(
    "/api/v1/routes",
    {"domain": "bad-scheme.local.test", "upstream_scheme": "ftp", "upstream_host": "echo-a", "upstream_port": 8080},
    (400,),
    "invalid_route",
)

# 5. Invalid upstream_port (0 and 70000)
post_expect_err(
    "/api/v1/routes",
    {"domain": "bad-port0.local.test", "upstream_scheme": "http", "upstream_host": "echo-a", "upstream_port": 0},
    (400,),
    "invalid_route",
)
post_expect_err(
    "/api/v1/routes",
    {"domain": "bad-port70000.local.test", "upstream_scheme": "http", "upstream_host": "echo-a", "upstream_port": 70000},
    (400,),
    "invalid_route",
)

# 6. Empty upstream_host
post_expect_err(
    "/api/v1/routes",
    {"domain": "empty-host.local.test", "upstream_scheme": "http", "upstream_host": "   ", "upstream_port": 8080},
    (400,),
    "invalid_route",
)
PY

echo "==> [6/10] Deleting route:custom-c and verifying removal from live routing (AC-2, AC-7)"
del_c="$(curl -fsS -X DELETE "${API_BASE}/api/v1/routes/route:custom-c")"
DEL_C="${del_c}" python3 -c 'import json, os; d = json.loads(os.environ["DEL_C"]); assert d["deleted"] is True and d["apply_result"]["applied"] is True'

del_http_code="$(curl -sS -o /dev/null -w "%{http_code}" --cacert "${CACERT}" \
  --resolve "app-c.local.test:${EDGE_HTTPS_PORT}:127.0.0.1" \
  "https://app-c.local.test:${EDGE_HTTPS_PORT}/deleted-check")"
if [[ "${del_http_code}" != "404" ]]; then
  echo "ERROR: Expected 404 after deleting route:custom-c, got ${del_http_code}" >&2
  exit 1
fi

echo "==> [7/10] Verifying failed nginx -t candidate and simulated reload failure preserve existing routes (AC-8, AC-10, Rules 6 & 7)"
pre_fault_status="$(curl -fsS "${API_BASE}/api/v1/routes/status")"
pre_sha="$(PRE_JSON="${pre_fault_status}" python3 -c 'import json, os; print(json.loads(os.environ["PRE_JSON"])["apply_status"]["active_config_sha256"])')"

# 7a: Inject invalid Nginx directive -> nginx -t fails, active config untouched, existing routes still respond 200
python3 - "${API_BASE}" "${pre_sha}" <<'PY'
import json, sys, urllib.error, urllib.request
api_base, pre_sha = sys.argv[1], sys.argv[2]
req = urllib.request.Request(
    f"{api_base}/api/v1/routes/apply",
    data=json.dumps({"inject_invalid_directive": "unknown_broken_directive_xyz on;"}).encode("utf-8"),
    headers={"Content-Type": "application/json"},
    method="POST",
)
try:
    urllib.request.urlopen(req, timeout=10)
    raise AssertionError("Expected 422 on invalid candidate directive")
except urllib.error.HTTPError as exc:
    assert exc.code == 422, exc.code
    res = json.loads(exc.read().decode("utf-8"))
    assert res["applied"] is False, res
    assert res["status"] == "validation_failed", res
    assert res["rolled_back"] is False, res
    assert res["active_config_sha256"] == pre_sha, res
    assert "unknown_broken_directive_xyz" in (res["last_error"] or ""), res
PY

# Verify existing routes still respond 200 immediately after validation failure
curl -fsS --cacert "${CACERT}" --resolve "app-a.local.test:${EDGE_HTTPS_PORT}:127.0.0.1" "https://app-a.local.test:${EDGE_HTTPS_PORT}/after-val-fail" >/dev/null
curl -fsS --cacert "${CACERT}" --resolve "app-b.local.test:${EDGE_HTTPS_PORT}:127.0.0.1" "https://app-b.local.test:${EDGE_HTTPS_PORT}/after-val-fail" >/dev/null
curl -fsS --cacert "${CACERT}" --resolve "app-a-clone.local.test:${EDGE_HTTPS_PORT}:127.0.0.1" "https://app-a-clone.local.test:${EDGE_HTTPS_PORT}/after-val-fail" >/dev/null

# 7b: Simulate reload failure -> atomic rollback restores previous config, existing routes still respond 200
python3 - "${API_BASE}" "${pre_sha}" <<'PY'
import json, sys, urllib.error, urllib.request
api_base, pre_sha = sys.argv[1], sys.argv[2]
req = urllib.request.Request(
    f"{api_base}/api/v1/routes/apply",
    data=json.dumps({"simulate_reload_failure": True}).encode("utf-8"),
    headers={"Content-Type": "application/json"},
    method="POST",
)
try:
    urllib.request.urlopen(req, timeout=10)
    raise AssertionError("Expected 422 on simulated reload failure")
except urllib.error.HTTPError as exc:
    assert exc.code == 422, exc.code
    res = json.loads(exc.read().decode("utf-8"))
    assert res["applied"] is False, res
    assert res["status"] == "reload_failed_rolled_back", res
    assert res["rolled_back"] is True, res
    assert res["active_config_sha256"] == pre_sha, res
    assert "simulated reload failure" in (res["last_error"] or ""), res
PY

curl -fsS --cacert "${CACERT}" --resolve "app-a.local.test:${EDGE_HTTPS_PORT}:127.0.0.1" "https://app-a.local.test:${EDGE_HTTPS_PORT}/after-reload-rollback" >/dev/null

echo "==> [8/10] Verifying desired state and last apply failure status persist across container restarts (AC-9)"
docker compose -f "${COMPOSE_FILE}" restart control-api router >/dev/null

for _ in $(seq 1 30); do
  if curl -fsS "${API_BASE}/api/v1/health" >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

post_restart_status="$(curl -fsS "${API_BASE}/api/v1/routes/status")"
POST_RESTART="${post_restart_status}" python3 - <<'PY'
import json, os
d = json.loads(os.environ["POST_RESTART"])
st = d["apply_status"]
assert st["last_apply_status"] == "reload_failed_rolled_back", st
assert st["rolled_back"] is True, st
assert "simulated reload failure" in (st["last_error"] or ""), st
assert d["routes_count"] == 3, d
PY

# Now execute a clean apply and verify status recovers to 'applied' and in_sync=True
clean_apply="$(curl -fsS -X POST "${API_BASE}/api/v1/routes/apply" -H "Content-Type: application/json" -d '{}')"
CLEAN_APPLY="${clean_apply}" python3 -c 'import json, os; d = json.loads(os.environ["CLEAN_APPLY"]); assert d["applied"] is True and d["status"] == "applied" and d["in_sync"] is True and d["last_error"] is None'

echo "==> [9/10] Verifying dynamic route traffic flows through inspector, capture, index, and retention chain (AC-11)"
chain_resp=""
for _ in $(seq 1 30); do
  if chain_resp="$(curl -fsS --cacert "${CACERT}" \
    --resolve "app-a-clone.local.test:${EDGE_HTTPS_PORT}:127.0.0.1" \
    -X POST -H "Content-Type: application/json" \
    -d '{"route_chain_test": true}' \
    "https://app-a-clone.local.test:${EDGE_HTTPS_PORT}/v1/route-chain-verify?check=dev020" 2>/dev/null)"; then
    break
  fi
  sleep 1
done
CHAIN_RESP="${chain_resp}" python3 -c 'import json, os; d = json.loads(os.environ["CHAIN_RESP"]); assert d["target"] == "echo-a" and d["body_json"]["route_chain_test"] is True'

# Query capture index for the dynamic route request
search_res="$(curl -fsS "${API_BASE}/api/v1/captures?host=app-a-clone.local.test&path=/v1/route-chain-verify")"
SEARCH_RES="${search_res}" python3 - <<'PY'
import json, os
d = json.loads(os.environ["SEARCH_RES"])
assert d["total"] >= 1, d
item = d["items"][0]
assert item["route_id"] == "route:app-a-clone", item
assert item["host"] == "app-a-clone.local.test", item
assert item["target"] == "echo-a", item
assert item["response_status"] == 200, item
PY

echo "==> [10/10] Restoring seed routes and checking Git hygiene"
curl -fsS -X DELETE "${API_BASE}/api/v1/routes/route:app-a-clone" >/dev/null
"${ROOT_DIR}/scripts/capture-ctl.sh" sync

leaked_files="$(git -C "${ROOT_DIR}" status --porcelain | grep -E '(\.key$|\.pem$|\.crt$|\.mitm$|\.flow$|\.sqlite|\.sqlite3$|^.. var/)' || true)"
if [[ -n "${leaked_files}" ]]; then
  echo "FAIL: Secret, certificate, or runtime database appeared in git status:" >&2
  echo "${leaked_files}" >&2
  exit 1
fi

echo "==> All DEV-020 route management & atomic Nginx apply checks passed!"
