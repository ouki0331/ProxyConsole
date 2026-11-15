#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
COMPOSE_FILE="${ROOT_DIR}/deploy/compose/docker-compose.yml"
EDGE_HTTPS_PORT="${EDGE_HTTPS_PORT:-8443}"
CONTROL_API_PORT="${CONTROL_API_PORT:-18480}"
API_BASE="http://127.0.0.1:${CONTROL_API_PORT}"
CACERT="${ROOT_DIR}/var/certificates/edge.crt"

cleanup() {
  docker compose -f "${COMPOSE_FILE}" start echo-b >/dev/null 2>&1 || true
  curl -fsS -X POST "${API_BASE}/api/v1/notifications/config" \
    -H "Content-Type: application/json" \
    --data '{"window_sec": 300, "ignore_4xx": true, "simulate_unavailable": false, "simulate_persistence_failure": false}' >/dev/null 2>&1 || true
}
trap cleanup EXIT

echo "==> [1/9] Starting clean stack for Aggregated Local Notifications & Live Event Feed E2E (DEV-042 AC-1)"
"${ROOT_DIR}/scripts/down.sh" >/dev/null 2>&1 || true
rm -rf "${ROOT_DIR}/var/captures" "${ROOT_DIR}/var/blobs" "${ROOT_DIR}/var/database"
"${ROOT_DIR}/scripts/up.sh"

for _ in $(seq 1 30); do
  if curl -fsS "${API_BASE}/api/v1/health" >/dev/null 2>&1 && \
     curl -fsS "${API_BASE}/api/v1/notifications/status" >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

echo "==> [2/9] Verifying HTTP 4xx exclusion by default (AC-3, Rule 1)"
for code in 400 404 422; do
  http_code="$(curl -sS -o /dev/null -w "%{http_code}" --cacert "${CACERT}" \
    --resolve "app-a.local.test:${EDGE_HTTPS_PORT}:127.0.0.1" \
    -X POST "https://app-a.local.test:${EDGE_HTTPS_PORT}/v1/client-err?__pc_status=${code}" \
    -H "Content-Type: application/json" \
    -H "X-Secret-Token: super-secret-hdr-4xx" \
    --data-binary '{"secret":"body-4xx-never-notify"}')"
  if [[ "${http_code}" != "${code}" ]]; then
    echo "Expected HTTP ${code}, got ${http_code}" >&2
    exit 1
  fi
done

"${ROOT_DIR}/scripts/capture-ctl.sh" sync >/dev/null
inc_total_after_4xx="$(curl -fsS "${API_BASE}/api/v1/notifications/incidents" | python3 -c 'import sys,json; print(json.load(sys.stdin)["total"])')"
if [[ "${inc_total_after_4xx}" != "0" ]]; then
  echo "Expected 0 incidents after HTTP 4xx requests, got ${inc_total_after_4xx}" >&2
  exit 1
fi

echo "==> [3/9] Triggering and classifying HTTP 500, 502/503/504, upstream_timeout, nginx_warn, nginx_error, and container_unavailable (AC-2, Rule 1, Rule 2, Rule 8)"
# 1) HTTP 500 on route:app-a with sensitive header/body to test payload sanitization
code_500="$(curl -sS -o /dev/null -w "%{http_code}" --cacert "${CACERT}" \
  --resolve "app-a.local.test:${EDGE_HTTPS_PORT}:127.0.0.1" \
  -X POST "https://app-a.local.test:${EDGE_HTTPS_PORT}/v1/orders/boom?__pc_status=500" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer SECRET_HEADER_TOKEN_DO_NOT_LEAK_999" \
  --data-binary '{"credit_card":"SECRET_BODY_PAN_DO_NOT_LEAK_888"}')"
[[ "${code_500}" == "500" ]] || { echo "Expected 500, got ${code_500}" >&2; exit 1; }

# 2) HTTP 503 on route:app-a
code_503="$(curl -sS -o /dev/null -w "%{http_code}" --cacert "${CACERT}" \
  --resolve "app-a.local.test:${EDGE_HTTPS_PORT}:127.0.0.1" \
  "https://app-a.local.test:${EDGE_HTTPS_PORT}/v1/gateway-busy?__pc_status=503")"
[[ "${code_503}" == "503" ]] || { echo "Expected 503, got ${code_503}" >&2; exit 1; }

# 3) Upstream timeout via /__pc_fast_timeout (proxy_read_timeout 1s) with __pc_sleep_ms=1600
code_timeout="$(curl -sS -o /dev/null -w "%{http_code}" --cacert "${CACERT}" \
  --resolve "app-a.local.test:${EDGE_HTTPS_PORT}:127.0.0.1" \
  "https://app-a.local.test:${EDGE_HTTPS_PORT}/__pc_fast_timeout?__pc_sleep_ms=1600")"
[[ "${code_timeout}" == "504" ]] || { echo "Expected 504 from fast timeout location, got ${code_timeout}" >&2; exit 1; }

# 4) Nginx warn and error lines written to shared router error log
docker compose -f "${COMPOSE_FILE}" exec -T router sh -c \
  'printf "2026/11/15 12:00:01 [warn] 42#42: *101 an upstream response is buffered to a temporary file while reading upstream, server: app-a.local.test, request: \"GET /v1/large HTTP/1.1\", upstream: \"http://172.18.0.5:8080/v1/large\", host: \"app-a.local.test\"\n2026/11/15 12:00:02 [error] 42#42: *102 connect() failed (111: Connection refused) while connecting to upstream, server: app-a.local.test, request: \"GET /v1/err HTTP/1.1\", upstream: \"http://172.18.0.5:8080/v1/err\", host: \"app-a.local.test\"\n" >> /var/lib/proxyconsole/router/nginx-error.log'

# 5) Stop echo-b container to trigger container_unavailable on route:app-b
docker compose -f "${COMPOSE_FILE}" stop echo-b >/dev/null
sleep 3

"${ROOT_DIR}/scripts/capture-ctl.sh" sync >/dev/null

# Verify all 6 event types are classified and no secret header/body leaked
API_BASE="${API_BASE}" python3 - <<'PY'
import json
import os
import urllib.request

api_base = os.environ["API_BASE"]
with urllib.request.urlopen(f"{api_base}/api/v1/notifications/incidents?limit=100", timeout=5.0) as r:
    raw_text = r.read().decode("utf-8")
    data = json.loads(raw_text)

assert "SECRET_HEADER_TOKEN_DO_NOT_LEAK_999" not in raw_text, "Secret header leaked into incident payload!"
assert "SECRET_BODY_PAN_DO_NOT_LEAK_888" not in raw_text, "Secret body leaked into incident payload!"

items = data["items"]
types_seen = {item["event_type"] for item in items}
required_types = {
    "http_500",
    "http_502_503_504",
    "upstream_timeout",
    "nginx_warn",
    "nginx_error",
    "container_unavailable",
}
missing = required_types - types_seen
assert not missing, f"Missing expected event types: {missing} (seen: {types_seen})"

for item in items:
    assert item["fingerprint"].startswith("fp:"), item
    assert item["route_id"], item
    assert item["normalized_reason"], item
PY

echo "==> [4/9] Verifying 5-minute window aggregation, milestone suppression, separate route/fingerprint incidents, and post-window cycle rollover (AC-4, AC-5, Rule 2, Rule 3)"
# Send 4 more HTTP 500 requests to route:app-a (bringing count from 1 -> 5)
for _ in 2 3 4 5; do
  curl -sS -o /dev/null --cacert "${CACERT}" \
    --resolve "app-a.local.test:${EDGE_HTTPS_PORT}:127.0.0.1" \
    -X POST "https://app-a.local.test:${EDGE_HTTPS_PORT}/v1/orders/boom?__pc_status=500" \
    -H "Content-Type: application/json" \
    --data-binary '{"retry":true}'
done

"${ROOT_DIR}/scripts/capture-ctl.sh" sync >/dev/null

API_BASE="${API_BASE}" python3 - <<'PY'
import json
import os
import time
import urllib.request

api_base = os.environ["API_BASE"]
with urllib.request.urlopen(
    f"{api_base}/api/v1/notifications/incidents?route_id=route:app-a&event_type=http_500",
    timeout=5.0,
) as r:
    inc_list = json.loads(r.read().decode("utf-8"))["items"]

assert len(inc_list) == 1, f"Expected 1 aggregated http_500 incident for route:app-a, got {len(inc_list)}"
inc = inc_list[0]
assert inc["count"] == 5, f"Expected aggregated count 5, got {inc['count']}"
assert inc["suppressed_count"] == 3, f"Expected 3 suppressed intermediate alerts (counts 2,3,4), got {inc['suppressed_count']}"
assert inc["first_seen_epoch"] <= inc["last_seen_epoch"], inc

# Inspect incident detail events: count=1 (initial, browser_notify=True), counts 2,3,4 (browser_notify=False), count=5 (browser_notify=True)
with urllib.request.urlopen(
    f"{api_base}/api/v1/notifications/incidents/{inc['incident_id']}",
    timeout=5.0,
) as r:
    detail = json.loads(r.read().decode("utf-8"))

events = sorted(detail["events"], key=lambda e: e["seq"])
assert len(events) == 5, f"Expected 5 events in timeline, got {len(events)}"
notify_flags = [(e["count"], e["event_kind"], e["browser_notify"]) for e in events]
assert notify_flags == [
    (1, "initial", True),
    (2, "count_update", False),
    (3, "count_update", False),
    (4, "count_update", False),
    (5, "count_update", True),
], f"Unexpected milestone notification flags: {notify_flags}"

# Test window expiration rollover (AC-5): set window_sec=1, ingest an event, wait 1.4s, ingest same fingerprint -> new cycle_number=2
req_cfg = urllib.request.Request(
    f"{api_base}/api/v1/notifications/config",
    data=json.dumps({"window_sec": 1}).encode("utf-8"),
    headers={"Content-Type": "application/json"},
    method="POST",
)
urllib.request.urlopen(req_cfg, timeout=5.0).read()

def ingest_cycle_test():
    req = urllib.request.Request(
        f"{api_base}/api/v1/notifications/ingest",
        data=json.dumps({
            "event_type": "nginx_warn",
            "route_id": "route:cycle-test",
            "domain": "cycle.local.test",
            "reason": "upstream response buffered to temporary file",
            "window_sec": 1,
        }).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=5.0) as resp:
        return json.loads(resp.read().decode("utf-8"))

c1 = ingest_cycle_test()["incident"]
time.sleep(1.3)
c2 = ingest_cycle_test()["incident"]
assert c1["incident_id"] != c2["incident_id"], "Expected new incident_id after aggregation window expired"
assert c1["fingerprint"] == c2["fingerprint"], "Expected identical fingerprint across cycles"
assert c2["cycle_number"] == c1["cycle_number"] + 1, f"Expected cycle_number to increment: {c1} vs {c2}"

# Restore default 300s window
req_restore = urllib.request.Request(
    f"{api_base}/api/v1/notifications/config",
    data=json.dumps({"window_sec": 300}).encode("utf-8"),
    headers={"Content-Type": "application/json"},
    method="POST",
)
urllib.request.urlopen(req_restore, timeout=5.0).read()
PY

echo "==> [5/9] Verifying single recovery transition (open -> resolved) when target container recovers (AC-6, Rule 4)"
docker compose -f "${COMPOSE_FILE}" start echo-b >/dev/null
for _ in $(seq 1 20); do
  code_b="$(curl -sS -o /dev/null -w "%{http_code}" --cacert "${CACERT}" \
    --resolve "app-b.local.test:${EDGE_HTTPS_PORT}:127.0.0.1" \
    "https://app-b.local.test:${EDGE_HTTPS_PORT}/v1/recover-check" || true)"
  if [[ "${code_b}" == "200" ]]; then
    break
  fi
  sleep 1
done
sleep 2

# Send a second 200 OK request to verify recovery is NOT recorded twice
curl -fsS --cacert "${CACERT}" \
  --resolve "app-b.local.test:${EDGE_HTTPS_PORT}:127.0.0.1" \
  "https://app-b.local.test:${EDGE_HTTPS_PORT}/v1/recover-check-2" >/dev/null
"${ROOT_DIR}/scripts/capture-ctl.sh" sync >/dev/null

API_BASE="${API_BASE}" python3 - <<'PY'
import json
import os
import urllib.request

api_base = os.environ["API_BASE"]
with urllib.request.urlopen(
    f"{api_base}/api/v1/notifications/incidents?route_id=route:app-b&event_type=container_unavailable",
    timeout=5.0,
) as r:
    items = json.loads(r.read().decode("utf-8"))["items"]

assert len(items) >= 1, "Expected container_unavailable incident for route:app-b"
inc = items[0]
assert inc["status"] == "resolved", f"Expected route:app-b incident to be resolved, got {inc}"
assert inc["resolved_at"], f"Expected resolved_at timestamp, got {inc}"
assert inc["recovery_reason"], f"Expected recovery_reason, got {inc}"

with urllib.request.urlopen(
    f"{api_base}/api/v1/notifications/incidents/{inc['incident_id']}",
    timeout=5.0,
) as r:
    detail = json.loads(r.read().decode("utf-8"))

recovery_events = [e for e in detail["events"] if e["event_kind"] == "recovery"]
assert len(recovery_events) == 1, f"Expected strictly 1 recovery event, got {len(recovery_events)}: {recovery_events}"
assert recovery_events[0]["browser_notify"] is True, recovery_events[0]

# Calling resolve again on an already-resolved incident must be idempotent (0 new recovery events)
req_res = urllib.request.Request(
    f"{api_base}/api/v1/notifications/incidents/{inc['incident_id']}/resolve",
    data=json.dumps({"recovery_reason": "duplicate_check"}).encode("utf-8"),
    headers={"Content-Type": "application/json"},
    method="POST",
)
res_again = json.loads(urllib.request.urlopen(req_res, timeout=5.0).read().decode("utf-8"))
assert res_again["transitioned"] is False, f"Expected transitioned=False on already-resolved incident: {res_again}"
PY

echo "==> [6/9] Verifying CLI notification commands and filter parameters (AC-8)"
cli_status="$("${ROOT_DIR}/scripts/capture-ctl.sh" notification-status)"
echo "${cli_status}" | python3 -c 'import sys,json; d=json.load(sys.stdin); assert d["total_incidents"] >= 5 and d["latest_seq"] >= 8, d'

cli_open="$("${ROOT_DIR}/scripts/capture-ctl.sh" notification-list --status open --route-id route:app-a)"
echo "${cli_open}" | python3 -c 'import sys,json; d=json.load(sys.stdin); assert d["total"] >= 1 and all(x["status"]=="open" and x["route_id"]=="route:app-a" for x in d["items"]), d'

echo "==> [7/9] Restarting control-api and inspector containers and verifying state & history persistence (AC-7, Rule 5)"
pre_restart_snapshot="$(curl -fsS "${API_BASE}/api/v1/notifications/status")"
docker compose -f "${COMPOSE_FILE}" restart control-api inspector >/dev/null

for _ in $(seq 1 30); do
  if curl -fsS "${API_BASE}/api/v1/health" >/dev/null 2>&1 && \
     curl -fsS "${API_BASE}/api/v1/notifications/status" >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

post_restart_snapshot="$(curl -fsS "${API_BASE}/api/v1/notifications/status")"
PRE_JSON="${pre_restart_snapshot}" POST_JSON="${post_restart_snapshot}" python3 - <<'PY'
import json
import os

pre = json.loads(os.environ["PRE_JSON"])
post = json.loads(os.environ["POST_JSON"])
assert post["total_incidents"] >= pre["total_incidents"], (pre, post)
assert post["open_incidents"] == pre["open_incidents"], (pre, post)
assert post["resolved_incidents"] == pre["resolved_incidents"], (pre, post)
assert post["latest_seq"] >= pre["latest_seq"], (pre, post)
PY

echo "==> [8/9] Running Headless Browser E2E for Notifications Workspace, WebSocket Live Feed, Reconnect Resync, Permission States, A11y/Responsive, and Failure Isolation (AC-8..AC-15, Rules 6..10)"
ROOT_DIR="${ROOT_DIR}" \
COMPOSE_FILE="${COMPOSE_FILE}" \
API_BASE="${API_BASE}" \
EDGE_HTTPS_PORT="${EDGE_HTTPS_PORT}" \
CACERT="${CACERT}" \
python3 - <<'PY'
import base64
import json
import os
import shutil
import socket
import struct
import subprocess
import tempfile
import time
import urllib.request
from urllib.parse import urlparse

ROOT_DIR = os.environ["ROOT_DIR"]
COMPOSE_FILE = os.environ["COMPOSE_FILE"]
API_BASE = os.environ["API_BASE"]
EDGE_HTTPS_PORT = int(os.environ["EDGE_HTTPS_PORT"])
CACERT = os.environ["CACERT"]


def find_browser_binary() -> str:
    candidates = [
        "/opt/homebrew/bin/chromium",
        "/usr/local/bin/chromium",
        shutil.which("chromium") or "",
        shutil.which("chromium-browser") or "",
        shutil.which("google-chrome") or "",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    ]
    for path in candidates:
        if path and os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    raise RuntimeError("No headless browser binary (chromium / Google Chrome) found")


class CDPClient:
    def __init__(self, ws_url: str):
        parsed = urlparse(ws_url)
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or 9222
        path = parsed.path or "/"
        if parsed.query:
            path = f"{path}?{parsed.query}"

        self.sock = socket.create_connection((host, port), timeout=15.0)
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        handshake = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        self.sock.sendall(handshake.encode("ascii"))
        resp = b""
        while b"\r\n\r\n" not in resp:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise RuntimeError("WebSocket handshake closed unexpectedly")
            resp += chunk
        header_part, self._rx_buf = resp.split(b"\r\n\r\n", 1)
        if b"101" not in header_part.splitlines()[0]:
            raise RuntimeError(f"WebSocket upgrade failed: {header_part!r}")
        self._msg_id = 0

    def _recv_exact(self, n: int) -> bytes:
        while len(self._rx_buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise RuntimeError("WebSocket connection closed while reading frame")
            self._rx_buf += chunk
        out = self._rx_buf[:n]
        self._rx_buf = self._rx_buf[n:]
        return out

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        fin_op = 0x80 | (opcode & 0x0F)
        mask_key = b"\x37\xfa\x21\x3d"
        length = len(payload)
        header = bytearray([fin_op])
        if length < 126:
            header.append(0x80 | length)
        elif length < 65536:
            header.append(0x80 | 126)
            header.extend(struct.pack("!H", length))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack("!Q", length))
        header.extend(mask_key)
        masked = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(bytes(header) + masked)

    def _recv_message(self) -> dict:
        fragments = bytearray()
        while True:
            b0, b1 = self._recv_exact(2)
            fin = bool(b0 & 0x80)
            opcode = b0 & 0x0F
            masked = bool(b1 & 0x80)
            length = b1 & 0x7F
            if length == 126:
                length = struct.unpack("!H", self._recv_exact(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._recv_exact(8))[0]
            mask_key = self._recv_exact(4) if masked else b""
            data = self._recv_exact(length)
            if masked:
                data = bytes(b ^ mask_key[i % 4] for i, b in enumerate(data))

            if opcode == 0x8:
                raise RuntimeError("WebSocket closed by browser")
            if opcode == 0x9:
                self._send_frame(0xA, data)
                continue
            if opcode == 0xA:
                continue
            fragments.extend(data)
            if fin:
                return json.loads(fragments.decode("utf-8"))

    def call(self, method: str, params: dict | None = None) -> dict:
        self._msg_id += 1
        target_id = self._msg_id
        payload = json.dumps(
            {"id": target_id, "method": method, "params": params or {}}
        ).encode("utf-8")
        self._send_frame(0x1, payload)
        while True:
            msg = self._recv_message()
            if msg.get("id") == target_id:
                if "error" in msg:
                    raise RuntimeError(f"CDP error in {method}: {msg['error']}")
                return msg.get("result", {})

    def eval_js(self, expression: str):
        wrapped = f"""
        (async () => {{
          {expression}
        }})()
        """
        res = self.call(
            "Runtime.evaluate",
            {
                "expression": wrapped,
                "awaitPromise": True,
                "returnByValue": True,
            },
        )
        if "exceptionDetails" in res:
            raise RuntimeError(f"JS exception: {res['exceptionDetails']}")
        return res.get("result", {}).get("value")

    def wait_idle(self, timeout: float = 15.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            state = self.eval_js(
                "return { busy: document.body?.dataset?.busy, fb: document.getElementById('feedback-banner')?.dataset?.state };"
            )
            if state and state.get("busy") == "false" and state.get("fb") != "loading":
                return
            time.sleep(0.05)
        raise RuntimeError("Timed out waiting for Web Console to become idle")

    def close(self) -> None:
        try:
            self.sock.close()
        except Exception:
            pass


def api_post(path: str, payload: dict) -> dict:
    req = urllib.request.Request(
        f"{API_BASE}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=5.0) as r:
        return json.loads(r.read().decode("utf-8"))


browser_bin = find_browser_binary()
with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
    s.bind(("127.0.0.1", 0))
    debug_port = s.getsockname()[1]

user_data_dir = tempfile.mkdtemp(prefix="pc_notif_chrome_")
proc = subprocess.Popen(
    [
        browser_bin,
        "--headless=new",
        "--disable-gpu",
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--no-first-run",
        f"--user-data-dir={user_data_dir}",
        f"--remote-debugging-port={debug_port}",
        f"{API_BASE}/",
    ],
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)

cdp = None
try:
    ws_url = None
    for _ in range(60):
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{debug_port}/json/list", timeout=1.0
            ) as resp:
                pages = json.loads(resp.read().decode("utf-8"))
                for p in pages:
                    if p.get("type") == "page" and p.get("webSocketDebuggerUrl"):
                        ws_url = p["webSocketDebuggerUrl"]
                        break
                if ws_url:
                    break
        except Exception:
            pass
        time.sleep(0.1)

    if not ws_url:
        raise RuntimeError("Could not obtain CDP page webSocketDebuggerUrl")

    cdp = CDPClient(ws_url)
    cdp.call("Page.enable")
    cdp.call("Runtime.enable")

    # Install Notification spy before any user interaction to verify Rule 7 (no auto-prompt on load)
    cdp.eval_js(
        """
        window.__notifSpy = {
          requestPermissionCalls: 0,
          nextPermissionResult: "granted",
          currentPermission: "default",
          sent: [],
        };
        class MockNotification {
          static get permission() {
            return window.__notifSpy.currentPermission;
          }
          static async requestPermission() {
            window.__notifSpy.requestPermissionCalls += 1;
            window.__notifSpy.currentPermission = window.__notifSpy.nextPermissionResult;
            return window.__notifSpy.currentPermission;
          }
          constructor(title, options = {}) {
            window.__notifSpy.sent.push({
              title,
              body: options.body || "",
              tag: options.tag || "",
              data: options.data || null,
            });
          }
          close() {}
        }
        window.Notification = MockNotification;
        """
    )
    cdp.wait_idle()

    # Wait up to 5s for WebSocket feed to report connected
    for _ in range(50):
        ws_st = cdp.eval_js("return window.__pcNotificationsState?.wsState;")
        if ws_st == "connected":
            break
        time.sleep(0.1)

    # Switch to Notifications workspace and verify A11y, labels, live regions & no auto-permission request (AC-11, AC-12, AC-14, Rule 7)
    cdp.eval_js("document.getElementById('view-notifications-btn').click();")
    init_ui = cdp.eval_js(
        """
        const btn = document.getElementById('enable-browser-notif-btn');
        btn.focus();
        const labelsOk = [
          'notif-filter-status',
          'notif-filter-type',
          'notif-filter-route',
          'notif-filter-fingerprint',
          'notif-filter-ack',
          'notif-filter-since',
          'notif-filter-until',
          'notif-filter-q',
        ].every(id => Boolean(document.querySelector(`label[for="${id}"]`)));
        return {
          notifWsHidden: document.getElementById('notifications-workspace').hidden,
          routesWsHidden: document.getElementById('routes-workspace').hidden,
          viewPressed: document.getElementById('view-notifications-btn').getAttribute('aria-pressed'),
          wsState: window.__pcNotificationsState.wsState,
          latestSeq: window.__pcNotificationsState.latestSeq,
          reqPermCallsOnLoad: window.__notifSpy.requestPermissionCalls,
          activeId: document.activeElement?.id,
          labelsOk,
          srLive: document.getElementById('notif-sr-announcer')?.getAttribute('aria-live'),
          toastRole: document.getElementById('notif-toast-region')?.getAttribute('role'),
          incidentRows: document.querySelectorAll('#notif-incidents-tbody tr').length,
        };
        """
    )
    assert init_ui["notifWsHidden"] is False and init_ui["routesWsHidden"] is True, init_ui
    assert init_ui["viewPressed"] == "true", init_ui
    assert init_ui["wsState"] == "connected", init_ui
    assert init_ui["reqPermCallsOnLoad"] == 0, f"Browser permission must NOT be requested on page load: {init_ui}"
    assert init_ui["activeId"] == "enable-browser-notif-btn", init_ui
    assert init_ui["labelsOk"] is True, init_ui
    assert init_ui["srLive"] == "polite" and init_ui["toastRole"] == "region", init_ui
    assert init_ui["incidentRows"] >= 5, init_ui

    # Verify narrow viewport @container responsiveness (AC-14)
    cdp.call(
        "Emulation.setDeviceMetricsOverride",
        {"width": 390, "height": 844, "deviceScaleFactor": 2, "mobile": True},
    )
    narrow_cell_display = cdp.eval_js(
        "return getComputedStyle(document.querySelector('#notif-incidents-tbody td')).display;"
    )
    assert narrow_cell_display == "block", f"Expected stacked block cell on narrow @container viewport, got: {narrow_cell_display}"
    cdp.call("Emulation.clearDeviceMetricsOverride")

    # Verify Incident Detail & Filter/Acknowledge UI (AC-8, AC-11)
    cdp.eval_js(
        """
        const firstInspect = document.querySelector('#notif-incidents-tbody .btn-inspect-incident');
        firstInspect.click();
        """
    )
    cdp.wait_idle()
    detail_ui = cdp.eval_js(
        """
        return {
          detailHidden: document.getElementById('notif-detail-content').hidden,
          idText: document.getElementById('notif-detail-id').textContent,
          fpText: document.getElementById('notif-detail-fingerprint').textContent,
          statusText: document.getElementById('notif-detail-status').textContent,
          eventRows: document.querySelectorAll('#notif-detail-events-tbody tr').length,
        };
        """
    )
    assert detail_ui["detailHidden"] is False, detail_ui
    assert detail_ui["idText"].startswith("inc-"), detail_ui
    assert detail_ui["fpText"].startswith("fp:"), detail_ui
    assert detail_ui["statusText"] in ("open", "resolved"), detail_ui
    assert detail_ui["eventRows"] >= 1, detail_ui

    # Filter by status=resolved in UI
    cdp.eval_js(
        """
        document.getElementById('notif-filter-status').value = 'resolved';
        document.getElementById('notif-filter-form').dispatchEvent(new Event('submit', { cancelable: true }));
        """
    )
    cdp.wait_idle()
    resolved_rows = cdp.eval_js(
        """
        const rows = Array.from(document.querySelectorAll('#notif-incidents-tbody tr'));
        return rows.map(r => r.dataset.status);
        """
    )
    assert len(resolved_rows) >= 1 and all(s == "resolved" for s in resolved_rows), resolved_rows

    # Reset filters in UI
    cdp.eval_js("document.getElementById('notif-filter-reset-btn').click();")
    cdp.wait_idle()

    # Test Browser Notification Permission States (AC-12, Rule 7, Rule 8):
    # 1) While permission is still "default" (not enabled), ingest a new live event -> WebSocket updates UI, 0 browser notifications sent
    api_post(
        "/api/v1/notifications/ingest",
        {
            "event_type": "http_500",
            "route_id": "route:app-a",
            "domain": "app-a.local.test",
            "reason": "HTTP 500 on /v1/default-perm-check",
        },
    )
    time.sleep(0.4)
    sent_while_default = cdp.eval_js("return window.__notifSpy.sent.length;")
    assert sent_while_default == 0, f"Expected 0 browser notifications while permission is default, got {sent_while_default}"

    # 2) User explicitly clicks Enable (#enable-browser-notif-btn) -> permission becomes "granted"
    cdp.eval_js("document.getElementById('enable-browser-notif-btn').click();")
    time.sleep(0.2)
    perm_granted_state = cdp.eval_js(
        """
        return {
          reqCalls: window.__notifSpy.requestPermissionCalls,
          enabled: window.__pcNotificationsState.browserEnabled,
          badgePerm: document.getElementById('browser-notif-permission-badge').dataset.permission,
        };
        """
    )
    assert perm_granted_state["reqCalls"] == 1, perm_granted_state
    assert perm_granted_state["enabled"] is True, perm_granted_state
    assert perm_granted_state["badgePerm"] == "granted", perm_granted_state

    # Trigger 5 occurrences of a new incident over live WebSocket:
    # Occurrence 1 (initial) -> notifies
    # Occurrences 2, 3, 4 -> suppressed
    # Occurrence 5 (milestone) -> notifies
    for _ in range(5):
        api_post(
            "/api/v1/notifications/ingest",
            {
                "event_type": "http_502_503_504",
                "route_id": "route:app-a",
                "domain": "app-a.local.test",
                "reason": "HTTP 502 on /v1/ws-live-milestone",
            },
        )
    # Resolve that incident -> 1 recovery notification
    api_post(
        "/api/v1/notifications/recover",
        {
            "route_id": "route:app-a",
            "event_types": ["http_502_503_504"],
            "recovery_reason": "HTTP 200 OK on route:app-a",
        },
    )
    time.sleep(0.8)

    live_notifs = cdp.eval_js("return window.__notifSpy.sent;")
    milestone_notifs = [
        n for n in live_notifs if "ws-live-milestone" in n["body"]
    ]
    assert len(milestone_notifs) == 3, f"Expected 3 browser notifications (initial=1, milestone=5, recovery), got {len(milestone_notifs)}: {milestone_notifs}"
    assert [n["data"]["event_kind"] for n in milestone_notifs] == [
        "initial",
        "count_update",
        "recovery",
    ], milestone_notifs
    assert [n["data"]["count"] for n in milestone_notifs] == [1, 5, 5], milestone_notifs

    # 3) Test WebSocket disconnect, missed events during disconnect, cursor resync, and duplicate delivery idempotency (AC-9, AC-10, Rule 6)
    seq_before_drop = cdp.eval_js("return window.__pcNotificationsState.latestSeq;")
    # Close browser WebSocket and pause auto-reconnect briefly while server records 2 events
    cdp.eval_js(
        """
        if (window.__pcNotificationsState) {
          document.getElementById('notif-ws-state-badge').dataset.wsState = 'disconnected';
        }
        """
    )
    api_post("/api/v1/notifications/config", {"drop_ws_connections": True})
    ev_missed_1 = api_post(
        "/api/v1/notifications/ingest",
        {
            "event_type": "nginx_error",
            "route_id": "route:app-a",
            "domain": "app-a.local.test",
            "reason": "upstream prematurely closed connection while reading response header",
        },
    )["event"]
    ev_missed_2 = api_post(
        "/api/v1/notifications/ingest",
        {
            "event_type": "nginx_error",
            "route_id": "route:app-a",
            "domain": "app-a.local.test",
            "reason": "upstream prematurely closed connection while reading response header",
        },
    )["event"]

    # Click Reconnect & Resync button in UI
    cdp.eval_js("document.getElementById('notif-ws-reconnect-btn').click();")
    cdp.wait_idle()
    time.sleep(0.5)

    resync_state = cdp.eval_js(
        f"""
        const incId = {json.dumps(ev_missed_2["incident_id"])};
        const row = document.querySelector(`#notif-incidents-tbody tr[data-incident-id="${{incId}}"]`);
        const countBeforeDup = row ? Number(row.dataset.count) : 0;
        const sentBeforeDup = window.__pcNotificationsState.browserSentCount;

        // Simulate duplicate WebSocket message delivery for ev_missed_1 and ev_missed_2
        window.__pcNotificationsState.deliverEvent({json.dumps(ev_missed_1)});
        window.__pcNotificationsState.deliverEvent({json.dumps(ev_missed_2)});
        const rowAfter = document.querySelector(`#notif-incidents-tbody tr[data-incident-id="${{incId}}"]`);
        const countAfterDup = rowAfter ? Number(rowAfter.dataset.count) : 0;
        const sentAfterDup = window.__pcNotificationsState.browserSentCount;
        return {{
          latestSeq: window.__pcNotificationsState.latestSeq,
          countBeforeDup,
          countAfterDup,
          sentBeforeDup,
          sentAfterDup,
        }};
        """
    )
    assert resync_state["latestSeq"] >= ev_missed_2["seq"] > seq_before_drop, resync_state
    assert resync_state["countBeforeDup"] == 2, resync_state
    assert resync_state["countAfterDup"] == 2, f"Duplicate delivery must NOT double-increment count: {resync_state}"
    assert resync_state["sentAfterDup"] == resync_state["sentBeforeDup"], f"Duplicate delivery must NOT re-send browser notification: {resync_state}"

    # 4) Test denied & unsupported browser notification permission states (AC-12, Rule 7)
    denied_and_unsupported = cdp.eval_js(
        """
        window.__notifSpy.currentPermission = "denied";
        document.getElementById('enable-browser-notif-btn').click();
        const deniedBadge = document.getElementById('browser-notif-permission-badge').dataset.permission;
        const savedNotification = window.Notification;
        delete window.Notification;
        document.getElementById('enable-browser-notif-btn').click();
        const unsupportedBadge = document.getElementById('browser-notif-permission-badge').dataset.permission;
        window.Notification = savedNotification;
        window.__notifSpy.currentPermission = "granted";
        return { deniedBadge, unsupportedBadge };
        """
    )
    assert denied_and_unsupported["deniedBadge"] == "denied", denied_and_unsupported
    assert denied_and_unsupported["unsupportedBadge"] == "unsupported", denied_and_unsupported

    # 5) Test Failure Isolation (AC-13, Rule 9):
    # Inject API + WebSocket + Persistence failure in Notifications subsystem
    api_post(
        "/api/v1/notifications/config",
        {
            "simulate_unavailable": True,
            "simulate_persistence_failure": True,
            "drop_ws_connections": True,
        },
    )
    cdp.eval_js("document.getElementById('notif-refresh-btn').click();")
    cdp.wait_idle()

    # Verify proxy traffic and capture still work while notifications persistence/API are failing
    subprocess.run(
        [
            "curl",
            "-fsS",
            "--cacert",
            CACERT,
            "--resolve",
            f"app-a.local.test:{EDGE_HTTPS_PORT}:127.0.0.1",
            f"https://app-a.local.test:{EDGE_HTTPS_PORT}/v1/fault-isolation-ok?__pc_status=500",
        ],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    subprocess.run(
        [
            "curl",
            "-fsS",
            "--cacert",
            CACERT,
            "--resolve",
            f"app-a.local.test:{EDGE_HTTPS_PORT}:127.0.0.1",
            f"https://app-a.local.test:{EDGE_HTTPS_PORT}/v1/healthy-proxy-route",
        ],
        check=True,
        stdout=subprocess.DEVNULL,
    )

    isolation_ui = cdp.eval_js(
        """
        const unavailHidden = document.getElementById('notifications-unavailable-box').hidden;
        document.getElementById('view-routes-btn').click();
        const routesVisible = !document.getElementById('routes-workspace').hidden;
        const routeRows = document.querySelectorAll('#routes-tbody tr').length;
        document.getElementById('view-requests-btn').click();
        const reqsVisible = !document.getElementById('requests-workspace').hidden;
        document.getElementById('view-rules-btn').click();
        const rulesVisible = !document.getElementById('rules-workspace').hidden;
        document.getElementById('view-notifications-btn').click();
        return { unavailHidden, routesVisible, routeRows, reqsVisible, rulesVisible };
        """
    )
    assert isolation_ui["unavailHidden"] is False, isolation_ui
    assert isolation_ui["routesVisible"] is True and isolation_ui["routeRows"] >= 2, isolation_ui
    assert isolation_ui["reqsVisible"] is True and isolation_ui["rulesVisible"] is True, isolation_ui

    # Restore Notifications subsystem and verify recovery via Retry button
    api_post(
        "/api/v1/notifications/config",
        {
            "simulate_unavailable": False,
            "simulate_persistence_failure": False,
        },
    )
    cdp.eval_js("document.getElementById('retry-notifications-btn').click();")
    cdp.wait_idle()
    recovered_ui = cdp.eval_js(
        "return document.getElementById('notifications-unavailable-box').hidden;"
    )
    assert recovered_ui is True, "Expected notifications-unavailable-box to hide after recovery"

finally:
    if cdp:
        cdp.close()
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except Exception:
        proc.kill()
    shutil.rmtree(user_data_dir, ignore_errors=True)
PY

echo "==> [9/9] Aggregated Local Notifications & Live Event Feed E2E (DEV-042) passed!"
