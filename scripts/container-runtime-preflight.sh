#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck disable=SC1091
source "${ROOT_DIR}/scripts/container-runtime.sh"

if [[ ! -S "${CONTAINER_ENGINE_SOCKET}" ]]; then
  echo "[preflight] Engine socket is unavailable: ${CONTAINER_ENGINE_SOCKET}" >&2
  if [[ "${CONTAINER_ENGINE_PROVIDER}" == "podman" ]]; then
    echo "[preflight] Start the user socket (for example: systemctl --user start podman.socket) or set CONTAINER_ENGINE_SOCKET." >&2
  else
    echo "[preflight] Start Docker Engine or set DOCKER_HOST/CONTAINER_ENGINE_SOCKET to a Unix socket." >&2
  fi
  exit 1
fi

if ! docker info >/dev/null 2>&1; then
  echo "[preflight] ${CONTAINER_ENGINE_PROVIDER} CLI cannot query the selected Engine." >&2
  exit 1
fi

docker compose version
echo "[preflight] PASS: provider=${CONTAINER_ENGINE_PROVIDER} socket=${CONTAINER_ENGINE_SOCKET}"
