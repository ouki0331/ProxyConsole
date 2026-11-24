#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck disable=SC1091
source "${ROOT_DIR}/scripts/container-runtime.sh"

CI_WORK_DIR="$(mktemp -d)"
CI_ORIGINAL_VAR_PRESENT=0
export COMPOSE_PROJECT_NAME="proxyconsole-ci-$(id -u)-$$"
export EDGE_HTTPS_PORT="${EDGE_HTTPS_PORT:-18443}"
export CONTROL_API_PORT="${CONTROL_API_PORT:-28480}"

if [[ -e "${ROOT_DIR}/var" ]]; then
  mv "${ROOT_DIR}/var" "${CI_WORK_DIR}/original-var"
  CI_ORIGINAL_VAR_PRESENT=1
fi

cleanup() {
  set +e
  "${ROOT_DIR}/scripts/down.sh" >/dev/null 2>&1 || true
  if [[ -e "${ROOT_DIR}/var" ]]; then
    mv "${ROOT_DIR}/var" "${CI_WORK_DIR}/test-var"
  fi
  if [[ ${CI_ORIGINAL_VAR_PRESENT} -eq 1 ]]; then
    mv "${CI_WORK_DIR}/original-var" "${ROOT_DIR}/var"
  fi
  rm -rf "${CI_WORK_DIR}"
}
trap cleanup EXIT

make -C "${ROOT_DIR}" runtime-test

"${ROOT_DIR}/scripts/container-runtime-preflight.sh"

node --check "${ROOT_DIR}/web/console/app.js"
python3 -m py_compile "${ROOT_DIR}"/services/recorder/*.py "${ROOT_DIR}/tests/helpers/echo_server.py"
bash -n "${ROOT_DIR}"/scripts/*.sh "${ROOT_DIR}"/tests/integration/*.sh

make -C "${ROOT_DIR}" config-test
make -C "${ROOT_DIR}" test

if git -C "${ROOT_DIR}" ls-files | grep -Eq '(^|/)(var/(certificates|captures|blobs|database)|.*\.key$)'; then
  echo "[ci-local] Tracked runtime data or private key detected." >&2
  exit 1
fi

echo "[ci-local] PASS: provider=${CONTAINER_ENGINE_PROVIDER}"
