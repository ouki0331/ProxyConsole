#!/bin/sh
set -eu

CONFIG_FILE="/etc/nginx/conf.d/default.conf"
RESOLVERS="$(awk '$1 == "nameserver" { printf "%s ", $2 }' /etc/resolv.conf)"
RESOLVERS="${RESOLVERS% }"

if [ -z "${RESOLVERS}" ] || printf '%s' "${RESOLVERS}" | grep -Eq '[^0-9A-Fa-f:. ]'; then
  echo "edge-entrypoint: no safe DNS resolver found in /etc/resolv.conf" >&2
  exit 1
fi

sed \
  -e "s/__CONTAINER_DNS_RESOLVERS__/${RESOLVERS}/g" \
  -e "s/127\\.0\\.0\\.11/${RESOLVERS}/g" \
  "${CONFIG_FILE}" > "${CONFIG_FILE}.runtime"
mv -f "${CONFIG_FILE}.runtime" "${CONFIG_FILE}"

exec /docker-entrypoint.sh "$@"
