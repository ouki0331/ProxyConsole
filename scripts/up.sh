#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="${ROOT_DIR}/deploy/compose/docker-compose.yml"

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

"${ROOT_DIR}/scripts/generate-certs.sh"

docker compose -f "${COMPOSE_FILE}" build --quiet
docker compose -f "${COMPOSE_FILE}" up -d

echo "[up] Waiting for HTTPS edge (${EDGE_HTTPS_PORT}) and control API (${CONTROL_API_PORT})..."
for _ in $(seq 1 30); do
  status_code="$(
    curl -s -o /dev/null -w "%{http_code}" \
      --cacert "${ROOT_DIR}/var/certificates/edge.crt" \
      --resolve "${APP_A_HOST}:${EDGE_HTTPS_PORT}:127.0.0.1" \
      "https://${APP_A_HOST}:${EDGE_HTTPS_PORT}/readyz" || true
  )"
  api_code="$(
    curl -s -o /dev/null -w "%{http_code}" \
      "http://127.0.0.1:${CONTROL_API_PORT}/api/v1/health" || true
  )"
  if [[ "${status_code}" == "200" && "${api_code}" == "200" ]]; then
    echo "[up] Proxy chain is ready on https://127.0.0.1:${EDGE_HTTPS_PORT} (API: http://127.0.0.1:${CONTROL_API_PORT})"
    exit 0
  fi
  sleep 0.5
done

echo "[up] ERROR: Stack did not become ready in time (edge=${status_code:-none}, api=${api_code:-none})." >&2
docker compose -f "${COMPOSE_FILE}" ps >&2
exit 1
