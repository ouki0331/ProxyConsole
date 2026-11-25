#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TEST_TMP="$(mktemp -d)"
trap 'rm -rf "${TEST_TMP}"' EXIT

FAKE_BIN="${TEST_TMP}/bin"
CALL_LOG="${TEST_TMP}/calls.log"
mkdir -p "${FAKE_BIN}"

cat >"${FAKE_BIN}/podman" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
if [[ "${1:-}" == "compose" && "${2:-}" == "version" ]]; then
  exit 0
fi
printf 'podman:%s\n' "$*" >>"${RUNTIME_TEST_CALL_LOG}"
SH

cat >"${FAKE_BIN}/docker" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
if [[ "${1:-}" == "compose" && "${2:-}" == "version" ]]; then
  exit 0
fi
printf 'docker:%s\n' "$*" >>"${RUNTIME_TEST_CALL_LOG}"
SH
chmod +x "${FAKE_BIN}/podman" "${FAKE_BIN}/docker"

base_env=(
  "PATH=${FAKE_BIN}:/usr/bin:/bin"
  "RUNTIME_TEST_CALL_LOG=${CALL_LOG}"
  "ROOT_DIR=${ROOT_DIR}"
  "PROXYCONSOLE_RUNTIME_QUIET=1"
)

env -i "${base_env[@]}" CONTAINER_RUNTIME=podman bash -c '
  source "${ROOT_DIR}/scripts/container-runtime.sh"
  [[ "${CONTAINER_ENGINE_PROVIDER}" == podman ]]
  [[ "${CONTAINER_COMPOSE_PROVIDER}" == podman-compose-subcommand ]]
  docker compose ps
  docker compose -f compose.yml build --quiet edge router
  docker ps -a --filter label=com.docker.compose.service=inspector --format {{.ID}}
  docker cp source target
'
grep -Fx 'podman:compose ps' "${CALL_LOG}" >/dev/null
grep -Fx 'podman:compose -f compose.yml build edge router' "${CALL_LOG}" >/dev/null
grep -Fx 'podman:ps -a --filter label=com.docker.compose.service=inspector --format {{.ID}}' \
  "${CALL_LOG}" >/dev/null
if grep -F -- '--quiet' "${CALL_LOG}" >/dev/null; then
  echo "Podman compose build received unsupported --quiet flag" >&2
  exit 1
fi
grep -Fx 'podman:cp source target' "${CALL_LOG}" >/dev/null

: >"${CALL_LOG}"
env -i "${base_env[@]}" CONTAINER_RUNTIME=podman COMPOSE_PROFILES=test bash -c '
  source "${ROOT_DIR}/scripts/container-runtime.sh"
  docker compose -f compose.yml up -d
'
grep -Fx 'podman:compose --profile test -f compose.yml up -d' "${CALL_LOG}" >/dev/null

: >"${CALL_LOG}"
env -i "${base_env[@]}" CONTAINER_RUNTIME=docker bash -c '
  source "${ROOT_DIR}/scripts/container-runtime.sh"
  [[ "${CONTAINER_ENGINE_PROVIDER}" == docker ]]
  docker compose ps
  docker compose -f compose.yml build --quiet edge router
'
grep -Fx 'docker:compose ps' "${CALL_LOG}" >/dev/null
grep -Fx 'docker:compose -f compose.yml build --quiet edge router' "${CALL_LOG}" >/dev/null

auto_provider="$(env -i "${base_env[@]}" CONTAINER_RUNTIME=auto bash -c '
  source "${ROOT_DIR}/scripts/container-runtime.sh"
  printf "%s" "${CONTAINER_ENGINE_PROVIDER}"
')"
[[ "${auto_provider}" == podman ]]

grep -Fx 'COPY services/recorder/engine_logs.py /app/recorder/engine_logs.py' \
  "${ROOT_DIR}/services/recorder/Dockerfile" >/dev/null

if [[ "$(grep -c -- '--connect-timeout 1 --max-time 2' "${ROOT_DIR}/scripts/up.sh")" -ne 2 ]]; then
  echo "Readiness probes must have bounded connection and total timeouts" >&2
  exit 1
fi

if rg -n 'resolver 127\.0\.0\.11' \
  "${ROOT_DIR}/deploy/nginx" "${ROOT_DIR}/services/recorder/route_manager.py" >/dev/null; then
  echo "Nginx configuration contains Docker-only DNS resolver" >&2
  exit 1
fi

grep -F '__CONTAINER_DNS_RESOLVERS__' \
  "${ROOT_DIR}/deploy/nginx/edge/default.conf" >/dev/null
grep -F '__CONTAINER_DNS_RESOLVERS__' \
  "${ROOT_DIR}/deploy/nginx/router/default.conf" >/dev/null
grep -F '__CONTAINER_DNS_RESOLVERS__' \
  "${ROOT_DIR}/services/recorder/route_manager.py" >/dev/null
grep -Fx 'CMD ["nginx", "-g", "daemon off;"]' \
  "${ROOT_DIR}/deploy/nginx/edge/Dockerfile" >/dev/null

set +e
invalid_output="$(env -i "${base_env[@]}" CONTAINER_RUNTIME=invalid bash -c '
  source "${ROOT_DIR}/scripts/container-runtime.sh"
' 2>&1)"
invalid_rc=$?
set -e
[[ ${invalid_rc} -eq 2 ]]
[[ "${invalid_output}" == *"must be auto, docker, or podman"* ]]

echo "PASS: container runtime selection and Docker-compatible shim"
