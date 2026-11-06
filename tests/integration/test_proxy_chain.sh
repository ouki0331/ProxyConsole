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
export APP_A_HOST="${APP_A_HOST:-app-a.local.test}"
export APP_B_HOST="${APP_B_HOST:-app-b.local.test}"
UNKNOWN_HOST="unknown.local.test"

cleanup_inspector() {
  docker compose -f "${COMPOSE_FILE}" start inspector >/dev/null 2>&1 || true
}
trap cleanup_inspector EXIT

echo "=== [1/9] Verify Compose and Nginx configurations ==="
"${ROOT_DIR}/scripts/verify-config.sh"

echo "=== [2/9] Start proxy chain ==="
"${ROOT_DIR}/scripts/up.sh"

echo "=== [3/9] Verify internal router and non-edge ports are NOT published to host ==="
for internal_svc in inspector router echo-a echo-b; do
  published_ports="$(docker compose -f "${COMPOSE_FILE}" ps --format json "${internal_svc}" | python3 -c '
import json, sys
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    data = json.loads(line)
    pubs = [p for p in (data.get("Publishers") or []) if p.get("PublishedPort", 0) > 0]
    if pubs:
        print(json.dumps(pubs))
')"
  if [[ -n "${published_ports}" ]]; then
    echo "FAIL: Service ${internal_svc} published ports to host: ${published_ports}" >&2
    exit 1
  fi
done
echo "PASS: inspector, router, echo-a, and echo-b have no host-published ports."

echo "=== [4/9] Verify GET routing for ${APP_A_HOST} -> echo-a ==="
resp_a_file="$(mktemp)"
status_a="$(
  curl -sS -o "${resp_a_file}" -w "%{http_code}" \
    --cacert "${CERT_FILE}" \
    --resolve "${APP_A_HOST}:${EDGE_HTTPS_PORT}:127.0.0.1" \
    "https://${APP_A_HOST}:${EDGE_HTTPS_PORT}/hello-a?mode=get"
)"
python3 - "${status_a}" "${resp_a_file}" "${APP_A_HOST}" <<'PY'
import json, sys
status, path, expected_host = sys.argv[1], sys.argv[2], sys.argv[3]
assert status == "200", f"Expected 200, got {status}"
data = json.load(open(path, encoding="utf-8"))
assert data["target"] == "echo-a", f"Expected echo-a, got {data}"
assert data["method"] == "GET", f"Expected GET, got {data}"
assert data["path"] == "/hello-a", f"Expected /hello-a, got {data}"
assert data["raw_query"] == "mode=get", f"Expected mode=get, got {data}"
assert data["host"].split(":")[0] == expected_host, f"Expected host {expected_host}, got {data['host']}"
print(f"PASS: GET {expected_host} -> target={data['target']} host={data['host']}")
PY
rm -f "${resp_a_file}"

echo "=== [5/9] Verify GET routing for ${APP_B_HOST} -> echo-b ==="
resp_b_file="$(mktemp)"
status_b="$(
  curl -sS -o "${resp_b_file}" -w "%{http_code}" \
    --cacert "${CERT_FILE}" \
    --resolve "${APP_B_HOST}:${EDGE_HTTPS_PORT}:127.0.0.1" \
    "https://${APP_B_HOST}:${EDGE_HTTPS_PORT}/hello-b?mode=get"
)"
python3 - "${status_b}" "${resp_b_file}" "${APP_B_HOST}" <<'PY'
import json, sys
status, path, expected_host = sys.argv[1], sys.argv[2], sys.argv[3]
assert status == "200", f"Expected 200, got {status}"
data = json.load(open(path, encoding="utf-8"))
assert data["target"] == "echo-b", f"Expected echo-b, got {data}"
assert data["method"] == "GET", f"Expected GET, got {data}"
assert data["path"] == "/hello-b", f"Expected /hello-b, got {data}"
assert data["raw_query"] == "mode=get", f"Expected mode=get, got {data}"
assert data["host"].split(":")[0] == expected_host, f"Expected host {expected_host}, got {data['host']}"
print(f"PASS: GET {expected_host} -> target={data['target']} host={data['host']}")
PY
rm -f "${resp_b_file}"

echo "=== [6/9] Verify JSON POST method, path, query, and body at targets ==="
for pair in "${APP_A_HOST}:echo-a" "${APP_B_HOST}:echo-b"; do
  host_name="${pair%%:*}"
  expected_target="${pair##*:}"
  post_resp_file="$(mktemp)"
  post_payload="{\"target_expect\":\"${expected_target}\",\"items\":[1,2,3],\"active\":true}"
  status_post="$(
    curl -sS -o "${post_resp_file}" -w "%{http_code}" \
      --cacert "${CERT_FILE}" \
      --resolve "${host_name}:${EDGE_HTTPS_PORT}:127.0.0.1" \
      -X POST \
      -H "Content-Type: application/json" \
      --data "${post_payload}" \
      "https://${host_name}:${EDGE_HTTPS_PORT}/v1/items/create?env=dev&tag=first&tag=second"
  )"
  python3 - "${status_post}" "${post_resp_file}" "${post_payload}" "${expected_target}" <<'PY'
import json, sys
status, path, expected_raw, expected_target = sys.argv[1:5]
assert status == "200", f"Expected 200, got {status}"
data = json.load(open(path, encoding="utf-8"))
assert data["target"] == expected_target, f"Expected {expected_target}, got {data}"
assert data["method"] == "POST", f"Expected POST, got {data}"
assert data["path"] == "/v1/items/create", f"Expected /v1/items/create, got {data}"
assert data["raw_query"] == "env=dev&tag=first&tag=second", f"Unexpected raw_query: {data['raw_query']}"
assert data["query_params"] == [["env", "dev"], ["tag", "first"], ["tag", "second"]], f"Unexpected query_params: {data['query_params']}"
assert data["body_text"] == expected_raw, f"Unexpected body_text: {data['body_text']}"
assert data["body_json"] == json.loads(expected_raw), f"Unexpected body_json: {data['body_json']}"
print(f"PASS: POST JSON to {expected_target} verified (method={data['method']}, path={data['path']}, query={data['raw_query']}, body_size={data['body_size']})")
PY
  rm -f "${post_resp_file}"
done

echo "=== [7/9] Verify unknown Host is rejected with 404 and not routed to echo targets ==="
unknown_resp_file="$(mktemp)"
status_unknown="$(
  curl -sS -o "${unknown_resp_file}" -w "%{http_code}" \
    --cacert "${CERT_FILE}" \
    --resolve "${UNKNOWN_HOST}:${EDGE_HTTPS_PORT}:127.0.0.1" \
    "https://${UNKNOWN_HOST}:${EDGE_HTTPS_PORT}/should-not-route"
)"
python3 - "${status_unknown}" "${unknown_resp_file}" <<'PY'
import json, sys
status, path = sys.argv[1], sys.argv[2]
body = open(path, encoding="utf-8").read()
assert status == "404", f"Expected 404 for unknown host, got {status} ({body})"
assert "echo-a" not in body and "echo-b" not in body, f"Unknown host leaked to target: {body}"
data = json.loads(body)
assert data.get("error") == "unknown_host", f"Unexpected error payload: {data}"
print(f"PASS: Unknown Host rejected with HTTP {status} ({body.strip()})")
PY
rm -f "${unknown_resp_file}"

echo "=== [8/9] Verify stopping inspector prevents routing and returns 502/504 ==="
docker compose -f "${COMPOSE_FILE}" stop inspector
sleep 1.5
down_resp_a="$(mktemp)"
down_resp_b="$(mktemp)"
status_down_a="$(
  curl -sS -o "${down_resp_a}" -w "%{http_code}" \
    --cacert "${CERT_FILE}" \
    --resolve "${APP_A_HOST}:${EDGE_HTTPS_PORT}:127.0.0.1" \
    "https://${APP_A_HOST}:${EDGE_HTTPS_PORT}/when-inspector-down"
)"
status_down_b="$(
  curl -sS -o "${down_resp_b}" -w "%{http_code}" \
    --cacert "${CERT_FILE}" \
    --resolve "${APP_B_HOST}:${EDGE_HTTPS_PORT}:127.0.0.1" \
    "https://${APP_B_HOST}:${EDGE_HTTPS_PORT}/when-inspector-down"
)"
python3 - "${status_down_a}" "${down_resp_a}" "${status_down_b}" "${down_resp_b}" <<'PY'
import sys
status_a, path_a, status_b, path_b = sys.argv[1:5]
body_a = open(path_a, encoding="utf-8", errors="replace").read()
body_b = open(path_b, encoding="utf-8", errors="replace").read()
assert status_a in ("502", "504"), f"Expected 502 or 504 when inspector is stopped (app-a), got {status_a}"
assert status_b in ("502", "504"), f"Expected 502 or 504 when inspector is stopped (app-b), got {status_b}"
assert "echo-a" not in body_a and "echo-b" not in body_a, "Response unexpectedly came from an echo target"
assert "echo-a" not in body_b and "echo-b" not in body_b, "Response unexpectedly came from an echo target"
print(f"PASS: With inspector stopped, edge returned HTTP {status_a} (app-a) and HTTP {status_b} (app-b) without misrouting.")
PY
rm -f "${down_resp_a}" "${down_resp_b}"

docker compose -f "${COMPOSE_FILE}" start inspector
for _ in $(seq 1 30); do
  rec_code="$(
    curl -s -o /dev/null -w "%{http_code}" \
      --cacert "${CERT_FILE}" \
      --resolve "${APP_A_HOST}:${EDGE_HTTPS_PORT}:127.0.0.1" \
      "https://${APP_A_HOST}:${EDGE_HTTPS_PORT}/recovered" || true
  )"
  if [[ "${rec_code}" == "200" ]]; then
    echo "PASS: Inspector restarted and routing recovered (HTTP 200)."
    break
  fi
  sleep 0.5
done

if [[ "${rec_code:-}" != "200" ]]; then
  echo "FAIL: Proxy chain did not recover after starting inspector." >&2
  exit 1
fi

echo "=== [9/9] Verify no secrets, private keys, or runtime captures in git status ==="
leaked_files="$(git -C "${ROOT_DIR}" status --porcelain | grep -E '(\.key$|\.pem$|\.crt$|\.mitm$|\.flow$|^.. var/)' || true)"
if [[ -n "${leaked_files}" ]]; then
  echo "FAIL: Secret, certificate, or runtime capture appeared in git status:" >&2
  echo "${leaked_files}" >&2
  exit 1
fi
echo "PASS: No secret, key, certificate, or runtime data in git status."

echo "ALL BLACK-BOX TESTS PASSED."
