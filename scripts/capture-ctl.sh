#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck disable=SC1091
source "${ROOT_DIR}/scripts/container-runtime.sh"
COMPOSE_FILE="${ROOT_DIR}/deploy/compose/docker-compose.yml"
COMPOSE_PROJECT="${COMPOSE_PROJECT_NAME:-proxyconsole-dev001}"

service_container_id() {
  local service_name="$1"
  local include_stopped="${2:-0}"
  local ps_args=(ps)
  if [[ "${include_stopped}" == "1" ]]; then
    ps_args+=(-a)
  fi
  docker "${ps_args[@]}" \
    --filter "label=com.docker.compose.project=${COMPOSE_PROJECT}" \
    --filter "label=com.docker.compose.service=${service_name}" \
    --format '{{.ID}}' | head -n 1
}

if [[ "${1:-}" == "sync" ]]; then
  mkdir -p "${ROOT_DIR}/var/captures" "${ROOT_DIR}/var/blobs" "${ROOT_DIR}/var/database"
  cid="$(service_container_id inspector 1)"
  if [[ -n "${cid}" ]]; then
    docker cp "${cid}:/var/lib/proxyconsole/captures/." "${ROOT_DIR}/var/captures/" 2>/dev/null || true
    docker cp "${cid}:/var/lib/proxyconsole/blobs/." "${ROOT_DIR}/var/blobs/" 2>/dev/null || true
    docker cp "${cid}:/var/lib/proxyconsole/database/." "${ROOT_DIR}/var/database/" 2>/dev/null || true
  fi
  echo "[capture-ctl] Synced captures, blobs, and index DB to ${ROOT_DIR}/var/"
  exit 0
fi

EXEC_SERVICE="inspector"
if [[ -n "$(service_container_id control-api 2>/dev/null || true)" ]]; then
  EXEC_SERVICE="control-api"
fi

docker compose -f "${COMPOSE_FILE}" exec -T "${EXEC_SERVICE}" \
  gosu mitmproxy python3 /app/recorder/capture_cli.py "$@"
