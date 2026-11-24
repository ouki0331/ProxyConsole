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
  docker cp source target
'
grep -Fx 'podman:compose ps' "${CALL_LOG}" >/dev/null
grep -Fx 'podman:cp source target' "${CALL_LOG}" >/dev/null

: >"${CALL_LOG}"
env -i "${base_env[@]}" CONTAINER_RUNTIME=docker bash -c '
  source "${ROOT_DIR}/scripts/container-runtime.sh"
  [[ "${CONTAINER_ENGINE_PROVIDER}" == docker ]]
  docker compose ps
'
grep -Fx 'docker:compose ps' "${CALL_LOG}" >/dev/null

auto_provider="$(env -i "${base_env[@]}" CONTAINER_RUNTIME=auto bash -c '
  source "${ROOT_DIR}/scripts/container-runtime.sh"
  printf "%s" "${CONTAINER_ENGINE_PROVIDER}"
')"
[[ "${auto_provider}" == podman ]]

set +e
invalid_output="$(env -i "${base_env[@]}" CONTAINER_RUNTIME=invalid bash -c '
  source "${ROOT_DIR}/scripts/container-runtime.sh"
' 2>&1)"
invalid_rc=$?
set -e
[[ ${invalid_rc} -eq 2 ]]
[[ "${invalid_output}" == *"must be auto, docker, or podman"* ]]

echo "PASS: container runtime selection and Docker-compatible shim"
