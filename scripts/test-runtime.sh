#!/usr/bin/env bash
# shellcheck shell=bash

export COMPOSE_PROFILES="${COMPOSE_PROFILES:-test}"
export PROXYCONSOLE_DEMO_MODE="${PROXYCONSOLE_DEMO_MODE:-true}"

# shellcheck disable=SC1091
source "${ROOT_DIR}/scripts/container-runtime.sh"
