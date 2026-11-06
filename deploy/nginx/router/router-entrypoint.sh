#!/bin/sh
set -eu

# If invoked for one-off validation (e.g. `nginx -t`), execute directly
if [ "${1:-}" = "nginx" ] && [ "${2:-}" = "-t" ]; then
  exec "$@"
fi

ROUTER_STATE_DIR="/var/lib/proxyconsole/router"
CANDIDATES_DIR="${ROUTER_STATE_DIR}/candidates"
REQUESTS_DIR="${ROUTER_STATE_DIR}/requests"
RESPONSES_DIR="${ROUTER_STATE_DIR}/responses"
ACTIVE_CONF="${ROUTER_STATE_DIR}/active.conf"
PREVIOUS_CONF="${ROUTER_STATE_DIR}/previous.conf"
LIVE_CONF="/etc/nginx/conf.d/default.conf"

mkdir -p "${CANDIDATES_DIR}" "${REQUESTS_DIR}" "${RESPONSES_DIR}"
chmod -R 0777 "${ROUTER_STATE_DIR}" 2>/dev/null || true
rm -f "${REQUESTS_DIR}"/*.req 2>/dev/null || true

validate_candidate() {
  cand_path="$1"
  val_id="$2"
  val_dir="/tmp/nginx-val-${val_id}"
  rm -rf "${val_dir}"
  mkdir -p "${val_dir}"
  cp "${cand_path}" "${val_dir}/candidate.conf"
  cat > "${val_dir}/nginx.conf" <<EOF
user  nginx;
worker_processes  auto;
error_log  /var/log/nginx/error.log notice;
pid        ${val_dir}/nginx.pid;

events {
    worker_connections  1024;
}

http {
    include       /etc/nginx/mime.types;
    default_type  application/octet-stream;
    sendfile        on;
    keepalive_timeout  65;
    include ${val_dir}/candidate.conf;
}
EOF
  set +e
  val_output="$(nginx -t -c "${val_dir}/nginx.conf" 2>&1)"
  val_rc=$?
  set -e
  rm -rf "${val_dir}"
  printf "%s\n" "${val_output}"
  return "${val_rc}"
}

if [ -s "${ACTIVE_CONF}" ]; then
  if validate_candidate "${ACTIVE_CONF}" "boot" >/dev/null 2>&1; then
    cp "${ACTIVE_CONF}" "/etc/nginx/conf.d/.boot.tmp"
    mv -f "/etc/nginx/conf.d/.boot.tmp" "${LIVE_CONF}"
  fi
else
  cp "${LIVE_CONF}" "${ROUTER_STATE_DIR}/.active.boot.tmp"
  chmod 0666 "${ROUTER_STATE_DIR}/.active.boot.tmp" 2>/dev/null || true
  mv -f "${ROUTER_STATE_DIR}/.active.boot.tmp" "${ACTIVE_CONF}"
fi

# Start background atomic apply watcher
(
  while true; do
    for req_file in "${REQUESTS_DIR}"/*.req; do
      if [ ! -f "${req_file}" ]; then
        continue
      fi
      REQ_ID=""
      CANDIDATE_FILE=""
      SIMULATE_RELOAD_FAILURE="0"
      # shellcheck disable=SC1090
      . "${req_file}" || true
      rm -f "${req_file}"

      if [ -z "${REQ_ID}" ] || [ -z "${CANDIDATE_FILE}" ] || [ ! -f "${CANDIDATE_FILE}" ]; then
        continue
      fi

      resp_tmp="${RESPONSES_DIR}/.${REQ_ID}.resp.tmp"
      resp_final="${RESPONSES_DIR}/${REQ_ID}.resp"

      # Step 1: Validate candidate in isolated temporary config BEFORE touching live config
      set +e
      val_msg="$(validate_candidate "${CANDIDATE_FILE}" "${REQ_ID}")"
      val_status=$?
      set -e

      if [ "${val_status}" -ne 0 ]; then
        {
          printf "STATUS=validation_failed\n"
          printf "ROLLED_BACK=0\n"
          printf "%s\n" "${val_msg}"
        } > "${resp_tmp}"
        chmod 0666 "${resp_tmp}" 2>/dev/null || true
        mv -f "${resp_tmp}" "${resp_final}"
        continue
      fi

      # Step 2: Backup current live config and atomically replace via temporary non-.conf file
      cp "${LIVE_CONF}" "${PREVIOUS_CONF}"
      chmod 0666 "${PREVIOUS_CONF}" 2>/dev/null || true
      stage_tmp="/etc/nginx/conf.d/.default.conf.tmp.${REQ_ID}"
      cp "${CANDIDATE_FILE}" "${stage_tmp}"
      mv -f "${stage_tmp}" "${LIVE_CONF}"

      # Step 3: Reload Nginx (or simulate reload failure) and rollback on failure
      if [ "${SIMULATE_RELOAD_FAILURE}" = "1" ]; then
        reload_status=1
        reload_msg="nginx: [emerg] simulated reload failure after atomic replace"
      else
        set +e
        reload_msg="$(nginx -s reload 2>&1)"
        reload_status=$?
        set -e
      fi

      if [ "${reload_status}" -ne 0 ]; then
        rollback_tmp="/etc/nginx/conf.d/.rollback.conf.tmp.${REQ_ID}"
        cp "${PREVIOUS_CONF}" "${rollback_tmp}"
        mv -f "${rollback_tmp}" "${LIVE_CONF}"
        nginx -s reload >/dev/null 2>&1 || true
        {
          printf "STATUS=reload_failed_rolled_back\n"
          printf "ROLLED_BACK=1\n"
          printf "%s\n" "${reload_msg}"
        } > "${resp_tmp}"
        chmod 0666 "${resp_tmp}" 2>/dev/null || true
        mv -f "${resp_tmp}" "${resp_final}"
        continue
      fi

      # Step 4: Persist validated & reloaded config to active.conf
      active_tmp="${ROUTER_STATE_DIR}/.active.tmp.${REQ_ID}"
      cp "${LIVE_CONF}" "${active_tmp}"
      chmod 0666 "${active_tmp}" 2>/dev/null || true
      mv -f "${active_tmp}" "${ACTIVE_CONF}"

      # Wait briefly for new Nginx worker to accept connections
      sleep 0.1

      {
        printf "STATUS=applied\n"
        printf "ROLLED_BACK=0\n"
        printf "%s\n" "${val_msg}"
      } > "${resp_tmp}"
      chmod 0666 "${resp_tmp}" 2>/dev/null || true
      mv -f "${resp_tmp}" "${resp_final}"
    done
    sleep 0.05
  done
) &

exec "$@"
