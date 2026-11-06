#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CERT_DIR="${ROOT_DIR}/var/certificates"
OPENSSL_CNF="${ROOT_DIR}/deploy/certificates/openssl.cnf"
KEY_FILE="${CERT_DIR}/edge.key"
CRT_FILE="${CERT_DIR}/edge.crt"

FORCE="${1:-}"

mkdir -p "${CERT_DIR}"

if [[ -f "${KEY_FILE}" && -f "${CRT_FILE}" && "${FORCE}" != "--force" ]]; then
  echo "[certs] Existing certificate found at ${CERT_DIR} (use --force to regenerate)."
  exit 0
fi

openssl req -x509 -nodes -newkey rsa:2048 \
  -days 365 \
  -keyout "${KEY_FILE}" \
  -out "${CRT_FILE}" \
  -config "${OPENSSL_CNF}" >/dev/null 2>&1

chmod 600 "${KEY_FILE}"
chmod 644 "${CRT_FILE}"

echo "[certs] Generated development TLS certificate and key in ${CERT_DIR}"
