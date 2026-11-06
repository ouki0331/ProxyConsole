#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="${ROOT_DIR}/deploy/compose/docker-compose.yml"

if [[ "${1:-}" == "sync" ]]; then
  mkdir -p "${ROOT_DIR}/var/captures" "${ROOT_DIR}/var/blobs"
  cid="$(docker compose -f "${COMPOSE_FILE}" ps -a -q inspector)"
  if [[ -n "${cid}" ]]; then
    docker cp "${cid}:/var/lib/proxyconsole/captures/." "${ROOT_DIR}/var/captures/" 2>/dev/null || true
    docker cp "${cid}:/var/lib/proxyconsole/blobs/." "${ROOT_DIR}/var/blobs/" 2>/dev/null || true
  fi
  echo "[capture-ctl] Synced captures and blobs to ${ROOT_DIR}/var/"
  exit 0
fi

docker compose -f "${COMPOSE_FILE}" exec -T inspector \
  python3 /app/recorder/capture_cli.py "$@"
