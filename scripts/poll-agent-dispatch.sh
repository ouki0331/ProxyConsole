#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ACKNOWLEDGED_ID="${1:-}"

if [[ ! "${ACKNOWLEDGED_ID}" =~ ^[0-9]+$ ]]; then
  echo "Usage: $0 <acknowledged-dispatch-id>" >&2
  exit 64
fi

check_dispatch() {
  git -C "${ROOT_DIR}" fetch origin main --quiet
  remote_json="$(git -C "${ROOT_DIR}" show origin/main:.agents/dispatch.json 2>/dev/null || true)"
  if [[ -z "${remote_json}" ]]; then
    return 1
  fi

  remote_id="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["dispatch_id"])' <<<"${remote_json}")"
  if (( remote_id > ACKNOWLEDGED_ID )); then
    echo "NEW_DISPATCH=${remote_id}"
    python3 -m json.tool <<<"${remote_json}"
    return 0
  fi
  return 1
}

if check_dispatch; then
  exit 0
fi

# Relative waits produce cumulative checkpoints at 10, 20, 30, 60, and 120 minutes.
wait_seconds=(600 600 600 1800 3600)
checkpoints=(10 20 30 60 120)

for index in "${!wait_seconds[@]}"; do
  sleep "${wait_seconds[$index]}"
  echo "[mailbox] Checking origin/main at +${checkpoints[$index]} minutes..."
  if check_dispatch; then
    exit 0
  fi
done

echo "[mailbox] No new dispatch after 120 minutes. Leaving the waiting loop."
exit 2

