#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="${ROOT_DIR}/deploy/compose/docker-compose.yml"

"${ROOT_DIR}/scripts/generate-certs.sh"

echo "[verify-config] Running docker compose config validation..."
docker compose -f "${COMPOSE_FILE}" config -q
echo "[verify-config] PASS: docker compose config (default port)"

EDGE_HTTPS_PORT=443 docker compose -f "${COMPOSE_FILE}" config -q
echo "[verify-config] PASS: docker compose config (EDGE_HTTPS_PORT=443)"

echo "[verify-config] Building edge and router images for Nginx config test..."
docker compose -f "${COMPOSE_FILE}" build --quiet edge router

echo "[verify-config] Running Nginx config test for edge..."
docker compose -f "${COMPOSE_FILE}" run --rm --no-deps edge nginx -t

echo "[verify-config] Running Nginx config test for internal router..."
docker compose -f "${COMPOSE_FILE}" run --rm --no-deps router nginx -t

echo "[verify-config] All configuration checks passed."
