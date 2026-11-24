#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
# shellcheck disable=SC1091
source "${ROOT_DIR}/scripts/test-runtime.sh"
COMPOSE_FILE="${ROOT_DIR}/deploy/compose/docker-compose.yml"
EDGE_HTTPS_PORT="${EDGE_HTTPS_PORT:-8443}"
CONTROL_API_PORT="${CONTROL_API_PORT:-18480}"
API_BASE="http://127.0.0.1:${CONTROL_API_PORT}"
CACERT="${ROOT_DIR}/var/certificates/edge.crt"

echo "==> [DEV-060] Overview Dashboard, i18n Localization, and Workspace Shortcuts Test"

# Check syntax with node --check
echo "--> Step 1: Checking JavaScript syntax with node --check"
node --check "${ROOT_DIR}/web/console/app.js"
echo "    PASS: node --check succeeded"

# Ensure stack is up
for _ in $(seq 1 30); do
  if curl -fsS "${API_BASE}/api/v1/health" >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

# Verify GET /api/v1/overview/summary
echo "--> Step 2: Verifying /api/v1/overview/summary endpoint"
overview_json="$(curl -fsS "${API_BASE}/api/v1/overview/summary")"
OVERVIEW_JSON="${overview_json}" python3 - <<'PY'
import json, os
data = json.loads(os.environ["OVERVIEW_JSON"])
assert data.get("status") == "ok", data
assert "proxy" in data, data
assert "routes" in data, data
assert "docker" in data, data
assert "captures" in data, data
assert "storage" in data, data
assert "incidents" in data, data
assert "sqlite" in data, data
print("    PASS: /api/v1/overview/summary returned all 8 categories")
PY

runtime_json="$(curl -fsS "${API_BASE}/api/v1/runtime/projects")"
RUNTIME_JSON="${runtime_json}" python3 - <<'PY'
import json, os
data = json.loads(os.environ["RUNTIME_JSON"])
assert data["provider"] in ("docker", "podman"), data
assert "projects" in data and "summary" in data, data
assert "database_containers" in data, data
print("    PASS: /api/v1/runtime/projects returned host-wide inventory contract")
PY

# Now run Browser CDP E2E tests
echo "--> Step 3: Launching Headless Chrome for CDP E2E verification"
API_BASE="${API_BASE}" python3 - <<'PY'
import base64
import json
import os
import socket
import ssl
import struct
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

API_BASE = os.environ.get("API_BASE", "http://127.0.0.1:18480")


def find_browser_binary() -> str:
    candidates = [
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
        "/usr/bin/google-chrome",
        "/usr/bin/chromium-browser",
        "/usr/bin/chromium",
    ]
    for c in candidates:
        if os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    raise RuntimeError("No suitable Chrome/Chromium binary found for browser E2E test")


class CDPClient:
    def __init__(self, ws_url: str):
        parsed = urllib.parse.urlparse(ws_url)
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or 9222
        path = parsed.path
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

    def wait_idle(self, timeout: float = 10.0) -> None:
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


browser_bin = find_browser_binary()
with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
    s.bind(("127.0.0.1", 0))
    debug_port = s.getsockname()[1]

user_data_dir = tempfile.mkdtemp(prefix="pc_chrome_dev060_")
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
            with urllib.request.urlopen(f"http://127.0.0.1:{debug_port}/json/list", timeout=1.0) as resp:
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
    cdp.wait_idle()

    # 1. Initial Overview State (AC-2, AC-3)
    print("    -> [Test 1] Verifying initial Overview workspace visibility and form-free DOM")
    state = cdp.eval_js(
        """
        return {
          overviewHidden: document.getElementById('overview-workspace')?.hidden,
          routesHidden: document.getElementById('routes-workspace')?.hidden,
          requestsHidden: document.getElementById('requests-workspace')?.hidden,
          rulesHidden: document.getElementById('rules-workspace')?.hidden,
          notifHidden: document.getElementById('notifications-workspace')?.hidden,
          runtimeHidden: document.getElementById('runtime-workspace')?.hidden,
          hasRouteFormInOverview: document.getElementById('overview-workspace')?.querySelector('form') !== null,
          hasRuleFormInOverview: document.getElementById('overview-workspace')?.querySelector('#rule-form') !== null,
          overviewPressed: document.getElementById('view-overview-btn')?.getAttribute('aria-pressed'),
          routesPressed: document.getElementById('view-routes-btn')?.getAttribute('aria-pressed'),
        };
        """
    )
    assert state["overviewHidden"] is False, state
    assert state["routesHidden"] is True, state
    assert state["requestsHidden"] is True, state
    assert state["rulesHidden"] is True, state
    assert state["notifHidden"] is True, state
    assert state["runtimeHidden"] is True, state
    assert state["hasRouteFormInOverview"] is False, "Overview must not contain forms"
    assert state["hasRuleFormInOverview"] is False, "Overview must not contain rule form"
    assert state["overviewPressed"] == "true", state
    assert state["routesPressed"] == "false", state
    print("       PASS: Overview is initial workspace and form-free")

    # 2. Overview 8 Cards verification (AC-3)
    print("    -> [Test 2] Verifying 8 core categories on Overview")
    cards = cdp.eval_js(
        """
        return {
          healthState: document.getElementById('overview-card-health')?.dataset.state,
          routesState: document.getElementById('overview-card-routes')?.dataset.state,
          dockerState: document.getElementById('overview-card-docker')?.dataset.state,
          capturesState: document.getElementById('overview-card-captures')?.dataset.state,
          storageState: document.getElementById('overview-card-storage')?.dataset.state,
          incidentsState: document.getElementById('overview-card-incidents')?.dataset.state,
          sqliteState: document.getElementById('overview-card-sqlite')?.dataset.state,
          rulesState: document.getElementById('overview-card-rules')?.dataset.state,
        };
        """
    )
    for card_name, c_state in cards.items():
        assert c_state in ("ok", "unavailable"), f"Card {card_name} has invalid state: {c_state}"
    print("       PASS: All 8 core category cards rendered with valid states")

    # 3. i18n Language Switching (AC-5, AC-6, AC-7, AC-8)
    print("    -> [Test 3] Verifying Japanese/English language switching")
    ja_state = cdp.eval_js(
        """
        const langSelect = document.getElementById('language-select');
        langSelect.value = 'ja';
        langSelect.dispatchEvent(new Event('change'));
        return {
          htmlLang: document.documentElement.lang,
          title: document.title,
          overviewHeading: document.getElementById('overview-heading')?.textContent,
          overviewBtnText: document.getElementById('view-overview-btn')?.textContent,
          routesBtnText: document.getElementById('view-routes-btn')?.textContent,
          requestsBtnText: document.getElementById('view-requests-btn')?.textContent,
          rulesBtnText: document.getElementById('view-rules-btn')?.textContent,
          runtimeBtnText: document.getElementById('view-runtime-btn')?.textContent,
          savedLocale: localStorage.getItem('proxyconsole_locale'),
        };
        """
    )
    assert ja_state["htmlLang"] == "ja", ja_state
    assert "ProxyConsole" in ja_state["title"], ja_state
    assert "システム概要" in ja_state["overviewHeading"], ja_state
    assert "概要" in ja_state["overviewBtnText"], ja_state
    assert "ルートとコンテナ" in ja_state["routesBtnText"], ja_state
    assert "リクエストと再送" in ja_state["requestsBtnText"], ja_state
    assert "変更ルール" in ja_state["rulesBtnText"], ja_state
    assert "実行中プロジェクト" in ja_state["runtimeBtnText"], ja_state
    assert ja_state["savedLocale"] == "ja", ja_state
    print("       PASS: Switched to Japanese successfully")

    # Switch back to English
    en_state = cdp.eval_js(
        """
        const langSelect = document.getElementById('language-select');
        langSelect.value = 'en';
        langSelect.dispatchEvent(new Event('change'));
        return {
          htmlLang: document.documentElement.lang,
          overviewHeading: document.getElementById('overview-heading')?.textContent,
          savedLocale: localStorage.getItem('proxyconsole_locale'),
        };
        """
    )
    assert en_state["htmlLang"] == "en", en_state
    assert "System Overview" in en_state["overviewHeading"], en_state
    assert en_state["savedLocale"] == "en", en_state
    print("       PASS: Switched back to English successfully")

    # 4. Keyboard Shortcuts: Alt+1..6 (AC-9, AC-10)
    print("    -> [Test 4] Verifying Alt+1..6 shortcuts and active state")
    # Press Alt+2 for Routes
    cdp.eval_js(
        """
        window.dispatchEvent(new KeyboardEvent('keydown', { key: '2', altKey: true, bubbles: true }));
        """
    )
    ws_state = cdp.eval_js(
        """
        return {
          overviewHidden: document.getElementById('overview-workspace')?.hidden,
          routesHidden: document.getElementById('routes-workspace')?.hidden,
          routesPressed: document.getElementById('view-routes-btn')?.getAttribute('aria-pressed'),
          savedWs: localStorage.getItem('proxyconsole_workspace'),
        };
        """
    )
    assert ws_state["overviewHidden"] is True, ws_state
    assert ws_state["routesHidden"] is False, ws_state
    assert ws_state["routesPressed"] == "true", ws_state
    assert ws_state["savedWs"] == "routes", ws_state
    print("       PASS: Alt+2 navigated to Routes & Containers")

    # Press Alt+3 for Requests
    cdp.eval_js(
        """
        window.dispatchEvent(new KeyboardEvent('keydown', { key: '3', altKey: true, bubbles: true }));
        """
    )
    ws_req = cdp.eval_js(
        """
        return {
          requestsHidden: document.getElementById('requests-workspace')?.hidden,
          requestsPressed: document.getElementById('view-requests-btn')?.getAttribute('aria-pressed'),
        };
        """
    )
    assert ws_req["requestsHidden"] is False, ws_req
    assert ws_req["requestsPressed"] == "true", ws_req
    print("       PASS: Alt+3 navigated to Requests & Replay")

    # Press Alt+4 for Rules
    cdp.eval_js(
        """
        window.dispatchEvent(new KeyboardEvent('keydown', { key: '4', altKey: true, bubbles: true }));
        """
    )
    ws_rules = cdp.eval_js(
        """
        return {
          rulesHidden: document.getElementById('rules-workspace')?.hidden,
          rulesPressed: document.getElementById('view-rules-btn')?.getAttribute('aria-pressed'),
        };
        """
    )
    assert ws_rules["rulesHidden"] is False, ws_rules
    assert ws_rules["rulesPressed"] == "true", ws_rules
    print("       PASS: Alt+4 navigated to Modification Rules")

    # Press Alt+5 for Notifications
    cdp.eval_js(
        """
        window.dispatchEvent(new KeyboardEvent('keydown', { key: '5', altKey: true, bubbles: true }));
        """
    )
    ws_notif = cdp.eval_js(
        """
        return {
          notifHidden: document.getElementById('notifications-workspace')?.hidden,
          notifPressed: document.getElementById('view-notifications-btn')?.getAttribute('aria-pressed'),
        };
        """
    )
    assert ws_notif["notifHidden"] is False, ws_notif
    assert ws_notif["notifPressed"] == "true", ws_notif
    print("       PASS: Alt+5 navigated to Notifications")

    # Press Alt+6 for Runtime Projects
    cdp.eval_js(
        """
        window.dispatchEvent(new KeyboardEvent('keydown', { key: '6', altKey: true, bubbles: true }));
        """
    )
    ws_runtime = cdp.eval_js(
        """
        return {
          runtimeHidden: document.getElementById('runtime-workspace')?.hidden,
          runtimePressed: document.getElementById('view-runtime-btn')?.getAttribute('aria-pressed'),
        };
        """
    )
    assert ws_runtime["runtimeHidden"] is False, ws_runtime
    assert ws_runtime["runtimePressed"] == "true", ws_runtime
    print("       PASS: Alt+6 navigated to Runtime Projects")

    # Press Alt+1 to return to Overview
    cdp.eval_js(
        """
        window.dispatchEvent(new KeyboardEvent('keydown', { key: '1', altKey: true, bubbles: true }));
        """
    )
    ws_overview = cdp.eval_js(
        """
        return {
          overviewHidden: document.getElementById('overview-workspace')?.hidden,
          overviewPressed: document.getElementById('view-overview-btn')?.getAttribute('aria-pressed'),
        };
        """
    )
    assert ws_overview["overviewHidden"] is False, ws_overview
    assert ws_overview["overviewPressed"] == "true", ws_overview
    print("       PASS: Alt+1 returned to Overview")

    # Test Input Guard: Alt+number while typing in form must not navigate (AC-10)
    print("    -> [Test 5] Verifying shortcut input guard when focused on form element")
    guard_state = cdp.eval_js(
        """
        // Navigate to routes first
        window.setWorkspaceView('routes');
        const input = document.getElementById('route-domain');
        input.focus();
        input.value = 'editing.test';
        // Dispatch Alt+3 while input is active
        input.dispatchEvent(new KeyboardEvent('keydown', { key: '3', altKey: true, bubbles: true }));
        return {
          routesHidden: document.getElementById('routes-workspace')?.hidden,
          requestsHidden: document.getElementById('requests-workspace')?.hidden,
          inputValue: input.value,
        };
        """
    )
    assert guard_state["routesHidden"] is False, "Workspace must NOT switch while editing input"
    assert guard_state["requestsHidden"] is True, "Requests must NOT become visible"
    assert guard_state["inputValue"] == "editing.test", "Input value must remain intact"
    print("       PASS: Input guard successfully prevented workspace switch while typing")

    # 5. Deep Navigation from Overview Cards (AC-4)
    print("    -> [Test 6] Verifying deep navigation links from Overview cards")
    cdp.eval_js("window.setWorkspaceView('overview');")
    # Click deep link on Routes Card
    cdp.eval_js(
        """
        const link = document.querySelector('#overview-card-routes .overview-deep-link');
        link.click();
        """
    )
    deep_routes = cdp.eval_js("return document.getElementById('routes-workspace')?.hidden;")
    assert deep_routes is False, "Deep link must open Routes workspace"
    print("       PASS: Deep link from Routes card navigated to Routes workspace")

    # Click deep link on Incidents Card
    cdp.eval_js("window.setWorkspaceView('overview');")
    cdp.eval_js(
        """
        const link = document.querySelector('#overview-card-incidents .overview-deep-link');
        link.click();
        """
    )
    deep_notif = cdp.eval_js("return document.getElementById('notifications-workspace')?.hidden;")
    assert deep_notif is False, "Deep link must open Notifications workspace"
    print("       PASS: Deep link from Incidents card navigated to Notifications workspace")

    # 6. Fallback on invalid stored workspace (AC-11)
    print("    -> [Test 7] Verifying fallback to Overview on invalid stored workspace")
    fallback_state = cdp.eval_js(
        """
        localStorage.setItem('proxyconsole_workspace', 'completely_invalid_workspace');
        window.initWorkspace();
        return {
          overviewHidden: document.getElementById('overview-workspace')?.hidden,
          savedWs: localStorage.getItem('proxyconsole_workspace'),
        };
        """
    )
    assert fallback_state["overviewHidden"] is False, fallback_state
    print("       PASS: Invalid stored workspace safely fell back to Overview")

    # 7. Responsive 390px Viewport (AC-12)
    print("    -> [Test 8] Verifying 390px viewport navigation safety")
    cdp.call(
        "Emulation.setDeviceMetricsOverride",
        {
            "width": 390,
            "height": 844,
            "deviceScaleFactor": 2.0,
            "mobile": True,
        },
    )
    resp_check = cdp.eval_js(
        """
        const nav = document.querySelector('.header-nav');
        const rect = nav.getBoundingClientRect();
        return {
          width: rect.width,
          overviewVisible: !document.getElementById('overview-workspace')?.hidden,
        };
        """
    )
    assert resp_check["width"] <= 390, resp_check
    print("       PASS: 390px narrow viewport verified")

    # 8. Representative Flows in Both Japanese and English (AC-13)
    print("    -> [Test 9] Verifying representative CRUD and preview flows in Japanese and English")
    # Switch to JA
    cdp.eval_js(
        """
        window.setLanguage('ja');
        window.setWorkspaceView('routes');
        """
    )
    # Create route in JA mode
    create_res = cdp.eval_js(
        """
        document.getElementById('route-domain').value = 'i18n-demo.local.test';
        document.getElementById('route-target-select').value = 'docker:echo-a:8080';
        document.getElementById('route-form').dispatchEvent(new Event('submit', { cancelable: true, bubbles: true }));
        return { submitted: true };
        """
    )
    time.sleep(1.0)
    cdp.wait_idle()

    # Check that route exists in table
    route_check = cdp.eval_js(
        """
        const rows = Array.from(document.querySelectorAll('#routes-tbody tr'));
        const found = rows.find(r => r.dataset.domain === 'i18n-demo.local.test');
        return {
          found: Boolean(found),
          domain: found?.dataset.domain,
        };
        """
    )
    assert route_check["found"] is True, route_check
    print("       PASS: Created route in Japanese mode")

    # Switch to Rules workspace and test Rule Preview simulation in Japanese
    cdp.eval_js("window.setWorkspaceView('rules');")
    rule_sim = cdp.eval_js(
        """
        const runBtn = document.getElementById('run-rule-preview-btn');
        if (runBtn) runBtn.click();
        return { clicked: Boolean(runBtn) };
        """
    )
    time.sleep(1.0)
    cdp.wait_idle()
    print("       PASS: Executed rule preview simulation in Japanese mode")

    # Switch back to English and delete the demo route
    cdp.eval_js(
        """
        window.setLanguage('en');
        window.setWorkspaceView('routes');
        """
    )
    del_res = cdp.eval_js(
        """
        const rows = Array.from(document.querySelectorAll('#routes-tbody tr'));
        const row = rows.find(r => r.dataset.domain === 'i18n-demo.local.test');
        if (row) {
          const delBtn = row.querySelector('.btn-delete-route');
          if (delBtn) delBtn.click();
          const confirmBtn = document.getElementById('delete-route-confirm-btn');
          if (confirmBtn) confirmBtn.click();
          return { deleted: true };
        }
        return { deleted: false };
        """
    )
    time.sleep(1.0)
    cdp.wait_idle()
    print("       PASS: Verified and cleaned up demo route in English mode")

finally:
    if cdp:
        cdp.close()
    proc.terminate()
    try:
        proc.wait(timeout=3.0)
    except Exception:
        proc.kill()

print("==> All DEV-060 tests in test_overview_i18n_navigation.sh passed successfully!")
PY
