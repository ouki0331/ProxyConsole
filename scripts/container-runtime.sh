#!/usr/bin/env bash
# shellcheck shell=bash

# Source this file after ROOT_DIR is set. It selects exactly one container
# runtime and prepends a Docker-compatible shim to PATH so existing shell and
# Python subprocess calls share the same provider choice.

if [[ -n "${PROXYCONSOLE_RUNTIME_READY:-}" ]]; then
  return 0 2>/dev/null || exit 0
fi

if [[ -n "${ROOT_DIR:-}" && -f "${ROOT_DIR}/.env" && -z "${PROXYCONSOLE_ENV_LOADED:-}" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "${ROOT_DIR}/.env"
  set +a
  export PROXYCONSOLE_ENV_LOADED=1
fi

_pc_runtime_request="${CONTAINER_RUNTIME:-auto}"
case "${_pc_runtime_request}" in
  auto|docker|podman) ;;
  *)
    echo "[runtime] CONTAINER_RUNTIME must be auto, docker, or podman (got: ${_pc_runtime_request})." >&2
    return 2 2>/dev/null || exit 2
    ;;
esac

_pc_find_runtime() {
  local candidate="$1"
  command -v "${candidate}" 2>/dev/null || true
}

_pc_docker_bin="$(_pc_find_runtime docker)"
_pc_podman_bin="$(_pc_find_runtime podman)"

if [[ "${_pc_runtime_request}" == "auto" ]]; then
  if [[ -n "${_pc_podman_bin}" ]]; then
    _pc_runtime_request="podman"
  elif [[ -n "${_pc_docker_bin}" ]]; then
    _pc_runtime_request="docker"
  else
    echo "[runtime] Neither podman nor docker is installed. Set CONTAINER_RUNTIME after installing one runtime." >&2
    return 127 2>/dev/null || exit 127
  fi
fi

if [[ "${_pc_runtime_request}" == "podman" ]]; then
  if [[ -z "${_pc_podman_bin}" ]]; then
    echo "[runtime] CONTAINER_RUNTIME=podman was requested, but podman is not installed." >&2
    return 127 2>/dev/null || exit 127
  fi
  export CONTAINER_ENGINE_BIN="${_pc_podman_bin}"
  export CONTAINER_ENGINE_PROVIDER="podman"
  if "${_pc_podman_bin}" compose version >/dev/null 2>&1; then
    export CONTAINER_COMPOSE_PROVIDER="podman-compose-subcommand"
  elif command -v podman-compose >/dev/null 2>&1; then
    export CONTAINER_COMPOSE_PROVIDER="podman-compose-binary"
    export CONTAINER_COMPOSE_BIN="$(command -v podman-compose)"
  else
    echo "[runtime] Podman is installed, but neither 'podman compose' nor 'podman-compose' is available." >&2
    return 127 2>/dev/null || exit 127
  fi
else
  if [[ -z "${_pc_docker_bin}" ]]; then
    echo "[runtime] CONTAINER_RUNTIME=docker was requested, but docker is not installed." >&2
    return 127 2>/dev/null || exit 127
  fi
  if ! "${_pc_docker_bin}" compose version >/dev/null 2>&1; then
    echo "[runtime] Docker is installed, but the Compose v2 plugin is unavailable." >&2
    return 127 2>/dev/null || exit 127
  fi
  export CONTAINER_ENGINE_BIN="${_pc_docker_bin}"
  export CONTAINER_ENGINE_PROVIDER="docker"
  export CONTAINER_COMPOSE_PROVIDER="docker-compose-plugin"
fi

_pc_default_socket=""
if [[ "${CONTAINER_ENGINE_PROVIDER}" == "podman" ]]; then
  _pc_runtime_dir="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
  for _pc_socket_candidate in \
    "${_pc_runtime_dir}/podman/podman.sock" \
    "/run/user/$(id -u)/podman/podman.sock" \
    "/run/podman/podman.sock"; do
    if [[ -S "${_pc_socket_candidate}" ]]; then
      _pc_default_socket="${_pc_socket_candidate}"
      break
    fi
  done
  _pc_default_socket="${_pc_default_socket:-${_pc_runtime_dir}/podman/podman.sock}"
else
  if [[ "${DOCKER_HOST:-}" == unix://* ]]; then
    _pc_default_socket="${DOCKER_HOST#unix://}"
  else
    _pc_default_socket="/var/run/docker.sock"
  fi
fi

export CONTAINER_RUNTIME="${CONTAINER_ENGINE_PROVIDER}"
export CONTAINER_ENGINE_SOCKET="${CONTAINER_ENGINE_SOCKET:-${_pc_default_socket}}"
export PROXYCONSOLE_RUNTIME_READY=1

_pc_script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export PATH="${_pc_script_dir}/runtime-bin:${PATH}"

if [[ "${PROXYCONSOLE_RUNTIME_QUIET:-0}" != "1" ]]; then
  echo "[runtime] provider=${CONTAINER_ENGINE_PROVIDER} compose=${CONTAINER_COMPOSE_PROVIDER} socket=${CONTAINER_ENGINE_SOCKET}" >&2
fi

unset _pc_runtime_request _pc_docker_bin _pc_podman_bin _pc_default_socket
unset _pc_runtime_dir _pc_socket_candidate _pc_script_dir
