#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck disable=SC1091
source "${ROOT_DIR}/scripts/container-runtime.sh"
COMPOSE_FILE="${ROOT_DIR}/deploy/compose/docker-compose.yml"

docker compose -f "${COMPOSE_FILE}" down -v --remove-orphans
echo "[down] Proxy chain stopped."
