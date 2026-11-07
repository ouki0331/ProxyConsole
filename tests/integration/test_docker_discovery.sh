#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
COMPOSE_FILE="${ROOT_DIR}/deploy/compose/docker-compose.yml"
EDGE_HTTPS_PORT="${EDGE_HTTPS_PORT:-8443}"
CONTROL_API_PORT="${CONTROL_API_PORT:-18480}"
API_BASE="http://127.0.0.1:${CONTROL_API_PORT}"
CACERT="${ROOT_DIR}/var/certificates/edge.crt"

cleanup() {
  curl -fsS -X POST "${API_BASE}/api/v1/discovery/config" \
    -H "Content-Type: application/json" \
    -d '{"socket_path_override": null}' >/dev/null 2>&1 || true
  docker compose -f "${COMPOSE_FILE}" up -d echo-b >/dev/null 2>&1 || true
}
trap cleanup EXIT

echo "==> [1/10] Starting stack and verifying discovery does NOT implicitly create routes (Rule 1, AC-1)"
"${ROOT_DIR}/scripts/down.sh" >/dev/null 2>&1 || true
rm -rf "${ROOT_DIR}/var/captures" "${ROOT_DIR}/var/blobs" "${ROOT_DIR}/var/database"
"${ROOT_DIR}/scripts/up.sh"

for _ in $(seq 1 30); do
  if curl -fsS "${API_BASE}/api/v1/health" >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

disc_status="$(curl -fsS "${API_BASE}/api/v1/discovery/status")"
routes_before="$(curl -fsS "${API_BASE}/api/v1/routes")"
DISC_STATUS="${disc_status}" ROUTES_BEFORE="${routes_before}" python3 - <<'PY'
import json, os
st = json.loads(os.environ["DISC_STATUS"])
rt = json.loads(os.environ["ROUTES_BEFORE"])
assert st["available"] is True, st
assert st["status"] == "ok", st
assert st["containers_count"] >= 2, st
assert st["targets_count"] >= 3, st
assert rt["total"] == 2, f"Discovery must not implicitly create routes: {rt}"
assert [r["route_id"] for r in rt["items"]] == ["route:app-a", "route:app-b"], rt
PY

echo "==> [2/10] Verifying container metadata, networks, and multi-port target candidates (AC-1, AC-2, AC-3, Rule 3, Rule 5)"
containers_json="$(curl -fsS "${API_BASE}/api/v1/discovery/containers")"
targets_json="$(curl -fsS "${API_BASE}/api/v1/discovery/targets")"
cli_targets_json="$("${ROOT_DIR}/scripts/capture-ctl.sh" discovery-targets)"

CONTAINERS_JSON="${containers_json}" TARGETS_JSON="${targets_json}" CLI_TARGETS_JSON="${cli_targets_json}" python3 - <<'PY'
import json, os

c_data = json.loads(os.environ["CONTAINERS_JSON"])
t_data = json.loads(os.environ["TARGETS_JSON"])
cli_t_data = json.loads(os.environ["CLI_TARGETS_JSON"])

assert c_data["available"] is True, c_data
assert t_data["available"] is True, t_data
assert cli_t_data["available"] is True, cli_t_data

by_svc = {c["service_name"]: c for c in c_data["items"]}
assert "echo-a" in by_svc and "echo-b" in by_svc, by_svc.keys()

for svc in ("echo-a", "echo-b"):
    c = by_svc[svc]
    assert len(c["container_id"]) >= 12, c
    assert c["container_name"], c
    assert c["image"], c
    assert c["state"] == "running", c
    assert c["running"] is True, c
    assert c["is_self"] is False, c
    assert c["reachable_from_router"] is True, c
    assert c["preferred_host"] == svc, c
    assert any(n["network_name"].endswith("router_targets_net") and n["reachable_from_router"] for n in c["networks"]), c["networks"]

ports_a = [p["internal_port"] for p in by_svc["echo-a"]["ports"]]
ports_b = [p["internal_port"] for p in by_svc["echo-b"]["ports"]]
assert ports_a == [8080], ports_a
assert ports_b == [8080, 8081], f"Expected multi-port [8080, 8081] on echo-b, got {ports_b}"

target_ids = [t["target_id"] for t in t_data["items"]]
assert "docker:echo-a:8080" in target_ids, target_ids
assert "docker:echo-b:8080" in target_ids, target_ids
assert "docker:echo-b:8081" in target_ids, target_ids
for t in t_data["items"]:
    assert t["selectable"] is True, t
    assert t["running"] is True, t
    assert t["is_self"] is False, t
PY

echo "==> [3/10] Verifying ProxyConsole self containers are excluded by default and guarded (AC-5, Rule 4)"
containers_with_self="$(curl -fsS "${API_BASE}/api/v1/discovery/containers?include_self=true")"
CONTAINERS_DEFAULT="${containers_json}" CONTAINERS_SELF="${containers_with_self}" API_BASE="${API_BASE}" python3 - <<'PY'
import json, os, urllib.error, urllib.request

def_data = json.loads(os.environ["CONTAINERS_DEFAULT"])
self_data = json.loads(os.environ["CONTAINERS_SELF"])
api_base = os.environ["API_BASE"]

def_svcs = {c["service_name"] for c in def_data["items"]}
for sys_svc in ("edge", "inspector", "control-api", "router"):
    assert sys_svc not in def_svcs, f"System container {sys_svc} leaked into default discovery list"

all_by_svc = {c["service_name"]: c for c in self_data["items"]}
for sys_svc in ("edge", "inspector", "control-api", "router"):
    assert sys_svc in all_by_svc, f"Expected {sys_svc} when include_self=true, got {all_by_svc.keys()}"
    assert all_by_svc[sys_svc]["is_self"] is True, all_by_svc[sys_svc]
    assert all_by_svc[sys_svc]["role"] == "system", all_by_svc[sys_svc]
    for p in all_by_svc[sys_svc]["ports"]:
        assert p["selectable"] is False, p

# Verify selecting a ProxyConsole system container as route target is rejected by default
req = urllib.request.Request(
    f"{api_base}/api/v1/routes/from-target",
    data=json.dumps({"domain": "self-guard.local.test", "container": "router", "port": 8080}).encode("utf-8"),
    headers={"Content-Type": "application/json"},
    method="POST",
)
try:
    urllib.request.urlopen(req, timeout=10)
    raise AssertionError("Expected 400 when selecting system container 'router'")
except urllib.error.HTTPError as exc:
    assert exc.code == 400, exc.code
    err = json.loads(exc.read().decode("utf-8"))
    assert err["error"] == "invalid_target", err
PY

echo "==> [4/10] Creating & updating route from discovered target and verifying live traffic to multi-port container (AC-6, Rule 3, Rule 5)"
# Also verify multi-port container without explicit port is rejected as ambiguous
python3 - "${API_BASE}" <<'PY'
import json, sys, urllib.error, urllib.request
api_base = sys.argv[1]
req = urllib.request.Request(
    f"{api_base}/api/v1/routes/from-target",
    data=json.dumps({"domain": "ambiguous.local.test", "container": "echo-b"}).encode("utf-8"),
    headers={"Content-Type": "application/json"},
    method="POST",
)
try:
    urllib.request.urlopen(req, timeout=10)
    raise AssertionError("Expected 400 when selecting multi-port container without port")
except urllib.error.HTTPError as exc:
    assert exc.code == 400, exc.code
    err = json.loads(exc.read().decode("utf-8"))
    assert err["error"] == "invalid_target", err
PY

create_disc="$(curl -fsS -X POST "${API_BASE}/api/v1/routes/from-target" \
  -H "Content-Type: application/json" \
  -d '{
    "route_id": "route:disc-b8081",
    "domain": "disc-b8081.local.test",
    "target_id": "docker:echo-b:8081",
    "description": "Route created from discovered echo-b port 8081"
  }')"
CREATE_DISC="${create_disc}" python3 - <<'PY'
import json, os
d = json.loads(os.environ["CREATE_DISC"])
assert d["apply_result"]["applied"] is True, d
rt = d["route"]
assert rt["route_id"] == "route:disc-b8081", rt
assert rt["upstream_host"] == "echo-b", rt
assert rt["upstream_port"] == 8081, rt
assert rt["target_state"] == "running", rt
assert rt["target_metadata"]["target_id"] == "docker:echo-b:8081", rt
assert rt["target_metadata"]["selected_port"] == 8081, rt
PY

resp_b8081="$(curl -fsS --cacert "${CACERT}" \
  --resolve "disc-b8081.local.test:${EDGE_HTTPS_PORT}:127.0.0.1" \
  "https://disc-b8081.local.test:${EDGE_HTTPS_PORT}/multiport-check?port=8081")"
RESP_B8081="${resp_b8081}" python3 - <<'PY'
import json, os
d = json.loads(os.environ["RESP_B8081"])
assert d["target"] == "echo-b", d
assert d["listen_port"] == 8081, f"Expected echo-b port 8081, got {d}"
assert d["path"] == "/multiport-check", d
PY

# Update route via container + port to echo-a:8080 and verify live traffic switches
upd_to_a="$(curl -fsS -X PUT "${API_BASE}/api/v1/routes/route:disc-b8081" \
  -H "Content-Type: application/json" \
  -d '{"container": "echo-a", "port": 8080}')"
UPD_TO_A="${upd_to_a}" python3 -c 'import json, os; d = json.loads(os.environ["UPD_TO_A"]); assert d["apply_result"]["applied"] is True and d["route"]["upstream_host"] == "echo-a" and d["route"]["upstream_port"] == 8080'

resp_switched_a="$(curl -fsS --cacert "${CACERT}" \
  --resolve "disc-b8081.local.test:${EDGE_HTTPS_PORT}:127.0.0.1" \
  "https://disc-b8081.local.test:${EDGE_HTTPS_PORT}/switched-to-a")"
RESP_SW_A="${resp_switched_a}" python3 -c 'import json, os; d = json.loads(os.environ["RESP_SW_A"]); assert d["target"] == "echo-a" and d["listen_port"] == 8080, d'

# Switch back to docker:echo-b:8081 via CLI
"${ROOT_DIR}/scripts/capture-ctl.sh" route-update "route:disc-b8081" --target-id "docker:echo-b:8081" >/dev/null

echo "==> [5/10] Sharing identical discovered container + port across multiple routes (AC-7, Rule 6)"
"${ROOT_DIR}/scripts/capture-ctl.sh" route-create-from-target \
  --route-id "route:disc-b8081-shared" \
  --domain "disc-b8081-shared.local.test" \
  --target-id "docker:echo-b:8081" \
  --description "Second domain sharing echo-b:8081" >/dev/null

resp_s1="$(curl -fsS --cacert "${CACERT}" --resolve "disc-b8081.local.test:${EDGE_HTTPS_PORT}:127.0.0.1" "https://disc-b8081.local.test:${EDGE_HTTPS_PORT}/shared-1")"
resp_s2="$(curl -fsS --cacert "${CACERT}" --resolve "disc-b8081-shared.local.test:${EDGE_HTTPS_PORT}:127.0.0.1" "https://disc-b8081-shared.local.test:${EDGE_HTTPS_PORT}/shared-2")"
RESP_S1="${resp_s1}" RESP_S2="${resp_s2}" python3 - <<'PY'
import json, os
d1 = json.loads(os.environ["RESP_S1"])
d2 = json.loads(os.environ["RESP_S2"])
assert d1["target"] == "echo-b" and d1["listen_port"] == 8081 and d1["path"] == "/shared-1", d1
assert d2["target"] == "echo-b" and d2["listen_port"] == 8081 and d2["path"] == "/shared-2", d2
assert d2["host"].startswith("disc-b8081-shared.local.test"), d2
PY

echo "==> [6/10] Stopping echo-b and verifying stopped state, target exclusion, and route preservation (AC-4, AC-9, Rule 2, Rule 8)"
initial_echob_id="$(CONTAINERS_JSON="${containers_json}" python3 -c '
import json, os
d = json.loads(os.environ["CONTAINERS_JSON"])
for c in d["items"]:
    if c["service_name"] == "echo-b":
        print(c["container_id"])
        break
')"

docker compose -f "${COMPOSE_FILE}" stop echo-b >/dev/null

refresh_stopped="$(curl -fsS -X POST "${API_BASE}/api/v1/discovery/refresh" -H "Content-Type: application/json" -d '{}')"
targets_stopped_inc="$(curl -fsS "${API_BASE}/api/v1/discovery/targets?include_stopped=true")"
routes_when_stopped="$(curl -fsS "${API_BASE}/api/v1/routes")"

REFRESH_STOPPED="${refresh_stopped}" TARGETS_STOPPED_INC="${targets_stopped_inc}" ROUTES_STOPPED="${routes_when_stopped}" API_BASE="${API_BASE}" python3 - <<'PY'
import json, os, urllib.error, urllib.request

ref = json.loads(os.environ["REFRESH_STOPPED"])
t_inc = json.loads(os.environ["TARGETS_STOPPED_INC"])
rts = json.loads(os.environ["ROUTES_STOPPED"])
api_base = os.environ["API_BASE"]

by_svc = {c["service_name"]: c for c in ref["containers"]}
assert "echo-b" in by_svc, by_svc
assert by_svc["echo-b"]["state"] == "stopped", by_svc["echo-b"]
assert by_svc["echo-b"]["running"] is False, by_svc["echo-b"]

# Active targets list (default include_stopped=false) must NOT include stopped echo-b
active_target_ids = [t["target_id"] for t in ref["targets"]]
assert "docker:echo-b:8080" not in active_target_ids, active_target_ids
assert "docker:echo-b:8081" not in active_target_ids, active_target_ids
assert "docker:echo-a:8080" in active_target_ids, active_target_ids

# With include_stopped=true, echo-b targets appear with running=False and selectable=False
inc_by_id = {t["target_id"]: t for t in t_inc["items"]}
assert inc_by_id["docker:echo-b:8081"]["state"] == "stopped", inc_by_id["docker:echo-b:8081"]
assert inc_by_id["docker:echo-b:8081"]["running"] is False, inc_by_id["docker:echo-b:8081"]
assert inc_by_id["docker:echo-b:8081"]["selectable"] is False, inc_by_id["docker:echo-b:8081"]

# Rule 2: Existing routes pointing to echo-b must NOT be deleted, and must report target_state == "stopped"
rt_by_id = {r["route_id"]: r for r in rts["items"]}
assert "route:disc-b8081" in rt_by_id, rt_by_id
assert "route:disc-b8081-shared" in rt_by_id, rt_by_id
assert rt_by_id["route:disc-b8081"]["target_state"] == "stopped", rt_by_id["route:disc-b8081"]
assert rt_by_id["route:app-b"]["target_state"] == "stopped", rt_by_id["route:app-b"]

# Selecting a stopped container target for a new route must fail with 400 invalid_target
req = urllib.request.Request(
    f"{api_base}/api/v1/routes/from-target",
    data=json.dumps({"domain": "stopped-target.local.test", "target_id": "docker:echo-b:8081"}).encode("utf-8"),
    headers={"Content-Type": "application/json"},
    method="POST",
)
try:
    urllib.request.urlopen(req, timeout=10)
    raise AssertionError("Expected 400 when selecting stopped target")
except urllib.error.HTTPError as exc:
    assert exc.code == 400, exc.code
    err = json.loads(exc.read().decode("utf-8"))
    assert err["error"] == "invalid_target", err
PY

echo "==> [7/10] Recreating echo-b and verifying running state and updated container ID reflection (AC-9, Rule 8)"
docker compose -f "${COMPOSE_FILE}" up -d --force-recreate echo-b >/dev/null

refresh_recreated="$(curl -fsS -X POST "${API_BASE}/api/v1/discovery/refresh" -H "Content-Type: application/json" -d '{}')"
routes_recreated="$(curl -fsS "${API_BASE}/api/v1/routes")"
REFRESH_REC="${refresh_recreated}" ROUTES_REC="${routes_recreated}" INITIAL_ID="${initial_echob_id}" python3 - <<'PY'
import json, os
ref = json.loads(os.environ["REFRESH_REC"])
rts = json.loads(os.environ["ROUTES_REC"])
init_id = os.environ["INITIAL_ID"]

by_svc = {c["service_name"]: c for c in ref["containers"]}
eb = by_svc["echo-b"]
assert eb["state"] == "running", eb
assert eb["running"] is True, eb
assert eb["container_id"] != init_id, f"Expected new container_id after recreate, still got {eb['container_id']}"

target_ids = [t["target_id"] for t in ref["targets"]]
assert "docker:echo-b:8080" in target_ids and "docker:echo-b:8081" in target_ids, target_ids

rt_by_id = {r["route_id"]: r for r in rts["items"]}
assert rt_by_id["route:disc-b8081"]["target_state"] == "running", rt_by_id["route:disc-b8081"]
assert rt_by_id["route:disc-b8081"]["target_container_id"] == eb["container_id"], rt_by_id["route:disc-b8081"]
PY

# Wait for recreated echo-b to answer on 8081 via router
rec_ok=0
for _ in $(seq 1 30); do
  if curl -fsS --cacert "${CACERT}" \
    --resolve "disc-b8081.local.test:${EDGE_HTTPS_PORT}:127.0.0.1" \
    "https://disc-b8081.local.test:${EDGE_HTTPS_PORT}/after-recreate" >/dev/null 2>&1; then
    rec_ok=1
    break
  fi
  sleep 0.5
done
if [[ "${rec_ok}" -ne 1 ]]; then
  echo "ERROR: disc-b8081.local.test did not recover after recreating echo-b" >&2
  exit 1
fi

echo "==> [8/10] Simulating Docker socket failure: verifying explicit error state, existing route survival, and manual upstream fallback (AC-8, Rule 7)"
unavail_res="$(curl -fsS -X POST "${API_BASE}/api/v1/discovery/config" \
  -H "Content-Type: application/json" \
  -d '{"socket_path_override": "/var/run/nonexistent-docker.sock"}')"
unavail_status="$(curl -fsS "${API_BASE}/api/v1/discovery/status")"
unavail_containers="$(curl -fsS "${API_BASE}/api/v1/discovery/containers")"

UNAVAIL_RES="${unavail_res}" UNAVAIL_STATUS="${unavail_status}" UNAVAIL_CONTAINERS="${unavail_containers}" API_BASE="${API_BASE}" python3 - <<'PY'
import json, os, urllib.error, urllib.request

res = json.loads(os.environ["UNAVAIL_RES"])
st = json.loads(os.environ["UNAVAIL_STATUS"])
ct = json.loads(os.environ["UNAVAIL_CONTAINERS"])
api_base = os.environ["API_BASE"]

# Must report unavailable and error reason (never stale 'ok')
assert res["available"] is False and res["status"] == "unavailable", res
assert res["error"], res
assert st["available"] is False and st["status"] == "unavailable", st
assert st["last_error"], st
assert st["containers_count"] == 0 and st["targets_count"] == 0, st
assert ct["available"] is False and ct["status"] == "unavailable" and ct["items"] == [], ct
assert ct["fallback_manual_upstream_supported"] is True, ct

# Target-based route creation must fail with 503 discovery_unavailable
req = urllib.request.Request(
    f"{api_base}/api/v1/routes/from-target",
    data=json.dumps({"domain": "should-fail-503.local.test", "target_id": "docker:echo-a:8080"}).encode("utf-8"),
    headers={"Content-Type": "application/json"},
    method="POST",
)
try:
    urllib.request.urlopen(req, timeout=10)
    raise AssertionError("Expected 503 when discovery is unavailable")
except urllib.error.HTTPError as exc:
    assert exc.code == 503, exc.code
    err = json.loads(exc.read().decode("utf-8"))
    assert err["error"] == "discovery_unavailable", err
    assert err["fallback_manual_upstream_supported"] is True, err
PY

# Existing routes must continue routing traffic normally while discovery is down
curl -fsS --cacert "${CACERT}" --resolve "app-a.local.test:${EDGE_HTTPS_PORT}:127.0.0.1" "https://app-a.local.test:${EDGE_HTTPS_PORT}/while-discovery-down" >/dev/null
curl -fsS --cacert "${CACERT}" --resolve "disc-b8081.local.test:${EDGE_HTTPS_PORT}:127.0.0.1" "https://disc-b8081.local.test:${EDGE_HTTPS_PORT}/while-discovery-down" >/dev/null

# Manual upstream_host / upstream_port route creation must still succeed while discovery is down
manual_create="$(curl -fsS -X POST "${API_BASE}/api/v1/routes" \
  -H "Content-Type: application/json" \
  -d '{
    "route_id": "route:manual-fallback",
    "domain": "manual-fallback.local.test",
    "upstream_scheme": "http",
    "upstream_host": "echo-a",
    "upstream_port": 8080,
    "description": "Manual route created while Docker discovery is unavailable"
  }')"
MANUAL_CREATE="${manual_create}" python3 -c 'import json, os; d = json.loads(os.environ["MANUAL_CREATE"]); assert d["apply_result"]["applied"] is True and d["route"]["route_id"] == "route:manual-fallback"'

resp_manual="$(curl -fsS --cacert "${CACERT}" \
  --resolve "manual-fallback.local.test:${EDGE_HTTPS_PORT}:127.0.0.1" \
  "https://manual-fallback.local.test:${EDGE_HTTPS_PORT}/manual-fallback-ok")"
RESP_MANUAL="${resp_manual}" python3 -c 'import json, os; d = json.loads(os.environ["RESP_MANUAL"]); assert d["target"] == "echo-a" and d["path"] == "/manual-fallback-ok", d'

# Restore default Docker socket path and verify discovery recovers to 'ok'
restored_res="$(curl -fsS -X POST "${API_BASE}/api/v1/discovery/config" \
  -H "Content-Type: application/json" \
  -d '{"socket_path_override": null}')"
RESTORED_RES="${restored_res}" python3 -c 'import json, os; d = json.loads(os.environ["RESTORED_RES"]); assert d["available"] is True and d["status"] == "ok" and d["targets_total"] >= 3, d'

echo "==> [9/10] Verifying capture and durable index integration for discovered and fallback routes (AC-10)"
cap_disc="$(curl -fsS "${API_BASE}/api/v1/captures?host=disc-b8081.local.test")"
cap_man="$(curl -fsS "${API_BASE}/api/v1/captures?host=manual-fallback.local.test")"
CAP_DISC="${cap_disc}" CAP_MAN="${cap_man}" python3 - <<'PY'
import json, os
cd = json.loads(os.environ["CAP_DISC"])
cm = json.loads(os.environ["CAP_MAN"])
assert cd["total"] >= 1, cd
assert cd["items"][0]["route_id"] == "route:disc-b8081", cd["items"][0]
assert cm["total"] >= 1, cm
assert cm["items"][0]["route_id"] == "route:manual-fallback", cm["items"][0]
PY

echo "==> [10/10] Cleaning up test routes and checking Git hygiene"
curl -fsS -X DELETE "${API_BASE}/api/v1/routes/route:disc-b8081" >/dev/null
curl -fsS -X DELETE "${API_BASE}/api/v1/routes/route:disc-b8081-shared" >/dev/null
curl -fsS -X DELETE "${API_BASE}/api/v1/routes/route:manual-fallback" >/dev/null
"${ROOT_DIR}/scripts/capture-ctl.sh" sync

leaked_files="$(git -C "${ROOT_DIR}" status --porcelain | grep -E '(\.key$|\.pem$|\.crt$|\.mitm$|\.flow$|\.sqlite|\.sqlite3$|^.. var/)' || true)"
if [[ -n "${leaked_files}" ]]; then
  echo "FAIL: Secret, certificate, or runtime database appeared in git status:" >&2
  echo "${leaked_files}" >&2
  exit 1
fi

echo "==> All DEV-021 Docker discovery and route target selection checks passed!"
