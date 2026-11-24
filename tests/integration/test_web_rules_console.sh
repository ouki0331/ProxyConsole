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

cleanup() {
  curl -fsS -X POST "${API_BASE}/api/v1/rules/config" \
    -H "Content-Type: application/json" \
    --data '{"simulate_unavailable": false}' >/dev/null 2>&1 || true
  curl -fsS -X POST "${API_BASE}/api/v1/rules/clear" \
    -H "Content-Type: application/json" \
    --data '{}' >/dev/null 2>&1 || true
}
trap cleanup EXIT

echo "==> [1/11] Starting stack and seeding baseline captures for Web Modification Rules Console E2E (AC-1, AC-2)"
"${ROOT_DIR}/scripts/down.sh" >/dev/null 2>&1 || true
rm -rf "${ROOT_DIR}/var/captures" "${ROOT_DIR}/var/blobs" "${ROOT_DIR}/var/database"
"${ROOT_DIR}/scripts/up.sh"

for _ in $(seq 1 30); do
  if curl -fsS "${API_BASE}/api/v1/health" >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

# Seed baseline JSON and form captures before creating rules
curl -fsS --cacert "${CACERT}" \
  --resolve "app-a.local.test:${EDGE_HTTPS_PORT}:127.0.0.1" \
  -X POST "https://app-a.local.test:${EDGE_HTTPS_PORT}/v1/orders?tag=orig1&tag=orig2" \
  -H "Content-Type: application/json; charset=utf-8" \
  -H "X-Client: seed" \
  -H "Cookie: sid=seed01; lang=ja" \
  --data-binary '{"order":{"id":500,"status":"pending","meta":{"tier":"standard"}},"items":[{"sku":"S1","qty":1}]}' >/dev/null

curl -fsS --cacert "${CACERT}" \
  --resolve "app-a.local.test:${EDGE_HTTPS_PORT}:127.0.0.1" \
  -X POST "https://app-a.local.test:${EDGE_HTTPS_PORT}/v1/forms/submit?src=seed" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  --data-raw "role=viewer&role=member&dept=qa" >/dev/null

"${ROOT_DIR}/scripts/capture-ctl.sh" sync >/dev/null

seed_json_cap_id="$(curl -fsS "${API_BASE}/api/v1/captures?path=/v1/orders&limit=1" | python3 -c 'import sys,json; print(json.load(sys.stdin)["items"][0]["capture_id"])')"
seed_form_cap_id="$(curl -fsS "${API_BASE}/api/v1/captures?path=/v1/forms/submit&limit=1" | python3 -c 'import sys,json; print(json.load(sys.stdin)["items"][0]["capture_id"])')"

echo "==> [2/11] Launching headless browser and driving Web Modification Rules Console E2E (AC-2..AC-14, Rules 1..9)"
ROOT_DIR="${ROOT_DIR}" \
COMPOSE_FILE="${COMPOSE_FILE}" \
API_BASE="${API_BASE}" \
EDGE_HTTPS_PORT="${EDGE_HTTPS_PORT}" \
CACERT="${CACERT}" \
SEED_JSON_CAP_ID="${seed_json_cap_id}" \
SEED_FORM_CAP_ID="${seed_form_cap_id}" \
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
SEED_JSON_CAP_ID = os.environ["SEED_JSON_CAP_ID"]
SEED_FORM_CAP_ID = os.environ["SEED_FORM_CAP_ID"]


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


def count_echo_requests(service: str) -> int:
    logs = subprocess.run(
        ["docker", "compose", "-f", COMPOSE_FILE, "logs", "--no-color", service],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return logs.count(" HTTP/1.1")


def get_captures_total() -> int:
    with urllib.request.urlopen(f"{API_BASE}/api/v1/captures?limit=1", timeout=5.0) as r:
        return int(json.loads(r.read().decode("utf-8"))["total"])


browser_bin = find_browser_binary()
with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
    s.bind(("127.0.0.1", 0))
    debug_port = s.getsockname()[1]

user_data_dir = tempfile.mkdtemp(prefix="pc_rules_chrome_")
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
    cdp.wait_idle()

    # --- STEP 3: Open Modification Rules workspace and verify accessibility, labels, live regions & narrow viewport (AC-2, AC-13) ---
    print("    -> [3/11] Verifying Modification Rules workspace navigation, accessibility, labels, live regions & narrow viewport (AC-2, AC-13)")
    cdp.eval_js("document.getElementById('view-rules-btn').click();")
    nav_state = cdp.eval_js(
        """
        const rulesWs = document.getElementById('rules-workspace');
        const routesWs = document.getElementById('routes-workspace');
        const reqsWs = document.getElementById('requests-workspace');
        const fb = document.getElementById('feedback-banner');
        const labelsOk = [
          'rule-name-input',
          'rule-id-input',
          'rule-priority-input',
          'rule-match-route-id-input',
          'rule-match-domain-input',
          'rule-match-method-input',
          'rule-match-path-exact-input',
          'rule-match-path-prefix-input',
          'rule-match-path-regex-input',
          'rule-match-content-type-input',
        ].every(id => Boolean(document.querySelector(`label[for="${id}"]`)));
        return {
          rulesHidden: rulesWs.hidden,
          routesHidden: routesWs.hidden,
          reqsHidden: reqsWs.hidden,
          btnPressed: document.getElementById('view-rules-btn').getAttribute('aria-pressed'),
          liveRole: fb.getAttribute('role'),
          livePolite: fb.getAttribute('aria-live'),
          labelsOk,
          emptyHidden: document.getElementById('rules-empty-state').hidden,
        };
        """
    )
    assert nav_state["rulesHidden"] is False, nav_state
    assert nav_state["routesHidden"] is True and nav_state["reqsHidden"] is True, nav_state
    assert nav_state["btnPressed"] == "true", nav_state
    assert nav_state["liveRole"] == "status" and nav_state["livePolite"] == "polite", nav_state
    assert nav_state["labelsOk"] is True, nav_state
    assert nav_state["emptyHidden"] is False, nav_state

    # Test narrow viewport container query layout (< 680px) and restore desktop viewport (AC-13)
    cdp.call(
        "Emulation.setDeviceMetricsOverride",
        {"width": 520, "height": 900, "deviceScaleFactor": 1, "mobile": True},
    )
    narrow_grid = cdp.eval_js(
        "return getComputedStyle(document.querySelector('.rules-editor-preview-grid')).gridTemplateColumns;"
    )
    assert " " not in narrow_grid.strip(), f"Expected single column on narrow viewport, got: {narrow_grid}"
    cdp.call("Emulation.clearDeviceMetricsOverride")

    # --- STEP 4: Create a complex JSON/Header/Query/Cookie rule with duplicate pairs, Unicode, spaces, reserved chars & action reordering (AC-3, AC-4, Rule 2, Rule 3) ---
    print("    -> [4/11] Creating structured Rule 1 (JSON + Header + Query + Cookie, duplicate pairs, Unicode, spaces, action reorder) via UI (AC-3, AC-4, Rule 2, Rule 3)")
    cdp.eval_js(
        """
        const setVal = (el, val) => {
          el.value = val;
          el.dispatchEvent(new Event('input', { bubbles: true }));
          el.dispatchEvent(new Event('change', { bubbles: true }));
        };
        setVal(document.getElementById('rule-name-input'), 'Orders Debug & Unicode Mutation');
        setVal(document.getElementById('rule-id-input'), 'rule:ui-orders-1');
        setVal(document.getElementById('rule-priority-input'), '50');
        setVal(document.getElementById('rule-description-input'), 'Rule created via Web UI E2E');
        setVal(document.getElementById('rule-match-route-id-input'), 'route:app-a');
        setVal(document.getElementById('rule-match-domain-input'), 'app-a.local.test');
        setVal(document.getElementById('rule-match-method-input'), 'POST, PUT');
        setVal(document.getElementById('rule-match-path-exact-input'), '/v1/orders');
        setVal(document.getElementById('rule-match-path-prefix-input'), '/v1/');
        setVal(document.getElementById('rule-match-path-regex-input'), '^/v1/orders$');
        setVal(document.getElementById('rule-match-content-type-input'), 'application/json');

        // Add query matcher: tag equals orig1
        document.getElementById('add-rule-query-match-btn').click();
        let qRows = document.querySelectorAll('#rule-query-match-rows .match-pair-row');
        setVal(qRows[0].querySelector('.match-key-input'), 'tag');
        setVal(qRows[0].querySelector('.match-op-select'), 'equals');
        setVal(qRows[0].querySelector('.match-val-input'), 'orig1');

        // Add header matcher: X-Client equals seed
        document.getElementById('add-rule-header-match-btn').click();
        let hRows = document.querySelectorAll('#rule-header-match-rows .match-pair-row');
        setVal(hRows[0].querySelector('.match-key-input'), 'X-Client');
        setVal(hRows[0].querySelector('.match-op-select'), 'equals');
        setVal(hRows[0].querySelector('.match-val-input'), 'seed');

        // Configure ordered actions:
        // Action #1 (existing default row): set_header X-Rule-Note = "console note & a=b"
        let actRows = document.querySelectorAll('#rule-action-rows .rule-action-row');
        setVal(actRows[0].querySelector('.rule-action-type-select'), 'set_header');
        setVal(actRows[0].querySelector('.rule-action-target-input'), 'X-Rule-Note');
        setVal(actRows[0].querySelector('.rule-action-value-input'), 'console note & a=b');

        // Action #2: add_header X-Dup = "first"
        document.getElementById('add-rule-action-btn').click();
        actRows = document.querySelectorAll('#rule-action-rows .rule-action-row');
        setVal(actRows[1].querySelector('.rule-action-type-select'), 'add_header');
        setVal(actRows[1].querySelector('.rule-action-target-input'), 'X-Dup');
        setVal(actRows[1].querySelector('.rule-action-value-input'), 'first');

        // Action #3: add_header X-Dup = "second"
        document.getElementById('add-rule-action-btn').click();
        actRows = document.querySelectorAll('#rule-action-rows .rule-action-row');
        setVal(actRows[2].querySelector('.rule-action-type-select'), 'add_header');
        setVal(actRows[2].querySelector('.rule-action-target-input'), 'X-Dup');
        setVal(actRows[2].querySelector('.rule-action-value-input'), 'second');

        // Action #4: add_query_param filter = "a&b=c 日本語"
        document.getElementById('add-rule-action-btn').click();
        actRows = document.querySelectorAll('#rule-action-rows .rule-action-row');
        setVal(actRows[3].querySelector('.rule-action-type-select'), 'add_query_param');
        setVal(actRows[3].querySelector('.rule-action-target-input'), 'filter');
        setVal(actRows[3].querySelector('.rule-action-value-input'), 'a&b=c 日本語');

        // Action #5: add_query_param filter = "second_val"
        document.getElementById('add-rule-action-btn').click();
        actRows = document.querySelectorAll('#rule-action-rows .rule-action-row');
        setVal(actRows[4].querySelector('.rule-action-type-select'), 'add_query_param');
        setVal(actRows[4].querySelector('.rule-action-target-input'), 'filter');
        setVal(actRows[4].querySelector('.rule-action-value-input'), 'second_val');

        // Action #6: set_cookie mode = "console_ui"
        document.getElementById('add-rule-action-btn').click();
        actRows = document.querySelectorAll('#rule-action-rows .rule-action-row');
        setVal(actRows[5].querySelector('.rule-action-type-select'), 'set_cookie');
        setVal(actRows[5].querySelector('.rule-action-target-input'), 'mode');
        setVal(actRows[5].querySelector('.rule-action-value-input'), 'console_ui');

        // Action #7: set_json_field order.status = "approved_initial"
        document.getElementById('add-rule-action-btn').click();
        actRows = document.querySelectorAll('#rule-action-rows .rule-action-row');
        setVal(actRows[6].querySelector('.rule-action-type-select'), 'set_json_field');
        setVal(actRows[6].querySelector('.rule-action-target-input'), 'order.status');
        setVal(actRows[6].querySelector('.rule-action-value-input'), '"approved_initial"');

        // Action #8: set_json_field order.status = "approved_final"
        document.getElementById('add-rule-action-btn').click();
        actRows = document.querySelectorAll('#rule-action-rows .rule-action-row');
        setVal(actRows[7].querySelector('.rule-action-type-select'), 'set_json_field');
        setVal(actRows[7].querySelector('.rule-action-target-input'), 'order.status');
        setVal(actRows[7].querySelector('.rule-action-value-input'), '"approved_final"');

        // Test reordering: move Action #8 up before Action #7, then move it back down so "approved_final" wins!
        actRows[7].querySelector('.btn-action-up').click();
        actRows = document.querySelectorAll('#rule-action-rows .rule-action-row');
        actRows[6].querySelector('.btn-action-down').click();
        """
    )

    # Verify read-only JSON payload inspector reflects structured form (Rule 3)
    payload_preview = json.loads(
        cdp.eval_js("return document.getElementById('rule-payload-preview').textContent;")
    )
    assert payload_preview["rule_id"] == "rule:ui-orders-1", payload_preview
    assert payload_preview["priority"] == 50, payload_preview
    assert len(payload_preview["actions"]) == 8, payload_preview["actions"]
    assert payload_preview["actions"][6]["value"] == "approved_initial"
    assert payload_preview["actions"][7]["value"] == "approved_final"

    # Submit Rule 1
    cdp.eval_js("document.getElementById('rule-submit-btn').click();")
    cdp.wait_idle()

    # Also create Rule 2 for URL-encoded form body modification (AC-3, AC-4)
    print("    -> [5/11] Creating structured Rule 2 (URL-encoded form duplicate key actions) and verifying Copy / Priority / Enable / Disable / Unsaved Edit Guard (AC-3..AC-6, AC-8)")
    cdp.eval_js("document.getElementById('new-rule-btn').click();")
    cdp.eval_js(
        """
        const setVal = (el, val) => {
          el.value = val;
          el.dispatchEvent(new Event('input', { bubbles: true }));
          el.dispatchEvent(new Event('change', { bubbles: true }));
        };
        setVal(document.getElementById('rule-name-input'), 'Form Role Escalation Rule');
        setVal(document.getElementById('rule-id-input'), 'rule:ui-form-2');
        setVal(document.getElementById('rule-priority-input'), '80');
        setVal(document.getElementById('rule-match-path-prefix-input'), '/v1/forms/');
        setVal(document.getElementById('rule-match-content-type-input'), 'application/x-www-form-urlencoded');

        let actRows = document.querySelectorAll('#rule-action-rows .rule-action-row');
        setVal(actRows[0].querySelector('.rule-action-type-select'), 'add_form_field');
        setVal(actRows[0].querySelector('.rule-action-target-input'), 'role');
        setVal(actRows[0].querySelector('.rule-action-value-input'), 'admin_ui');

        document.getElementById('add-rule-action-btn').click();
        actRows = document.querySelectorAll('#rule-action-rows .rule-action-row');
        setVal(actRows[1].querySelector('.rule-action-type-select'), 'set_form_field');
        setVal(actRows[1].querySelector('.rule-action-target-input'), 'dept');
        setVal(actRows[1].querySelector('.rule-action-value-input'), 'sec ops & 開発');

        document.getElementById('rule-submit-btn').click();
        """
    )
    cdp.wait_idle()

    # Verify both rules appear in #rules-tbody ordered by priority (50 then 80)
    table_rules = cdp.eval_js(
        """
        return {
          rows: Array.from(document.querySelectorAll('#rules-tbody tr')).map(tr => ({
            id: tr.dataset.ruleId,
            prio: Number(tr.dataset.priority),
            enabled: tr.dataset.enabled,
          })),
          fbTitle: document.getElementById('feedback-title')?.textContent,
          fbMsg: document.getElementById('feedback-message')?.textContent,
          errCode: document.getElementById('rule-form-error-box')?.dataset?.errorCode,
          errText: document.getElementById('rule-form-error-text')?.textContent,
        };
        """
    )
    assert [r["id"] for r in table_rules["rows"]] == ["rule:ui-orders-1", "rule:ui-form-2"], table_rules

    # Test Rule Copy via modal (AC-5): copy rule:ui-orders-1 -> rule:ui-orders-copy
    cdp.eval_js(
        """
        document.querySelector('.btn-copy-rule[data-rule-id="rule:ui-orders-1"]').click();
        """
    )
    copy_modal_open = cdp.eval_js("return document.getElementById('copy-rule-dialog').open;")
    assert copy_modal_open is True

    # First try submitting with the SAME rule_id to verify duplicate protection in copy dialog
    cdp.eval_js(
        """
        const idIn = document.getElementById('copy-new-rule-id');
        idIn.value = 'rule:ui-orders-1';
        document.getElementById('copy-rule-confirm-btn').click();
        """
    )
    copy_same_id_err = cdp.eval_js(
        "return document.getElementById('copy-rule-id-error').textContent;"
    )
    assert "different from source" in copy_same_id_err, copy_same_id_err

    # Now enter valid new ID and name and confirm copy
    cdp.eval_js(
        """
        document.getElementById('copy-new-rule-name').value = 'Copied Orders Rule';
        document.getElementById('copy-new-rule-id').value = 'rule:ui-orders-copy';
        document.getElementById('copy-new-rule-priority').value = '90';
        document.getElementById('copy-rule-confirm-btn').click();
        """
    )
    cdp.wait_idle()
    after_copy_ids = cdp.eval_js(
        "return Array.from(document.querySelectorAll('#rules-tbody tr')).map(tr => tr.dataset.ruleId);"
    )
    assert after_copy_ids == ["rule:ui-orders-1", "rule:ui-form-2", "rule:ui-orders-copy"], after_copy_ids

    # Test Unsaved Edit Discard Confirmation Modal (Rule 4, AC-8):
    # Modify rule-name-input without saving, click Cancel/Reset -> modal opens -> click Keep Editing (Cancel) -> input stays!
    cdp.eval_js(
        """
        const nameIn = document.getElementById('rule-name-input');
        nameIn.value = 'Unsaved Temporary Name Edit';
        nameIn.dispatchEvent(new Event('input', { bubbles: true }));
        document.getElementById('rule-cancel-edit-btn').click();
        """
    )
    discard_open = cdp.eval_js("return document.getElementById('discard-rule-edits-dialog').open;")
    assert discard_open is True
    cdp.eval_js("document.getElementById('discard-rule-cancel-btn').click();")
    kept_val = cdp.eval_js("return document.getElementById('rule-name-input').value;")
    assert kept_val == "Unsaved Temporary Name Edit", kept_val

    # Now click Cancel/Reset again and confirm Discard -> form resets cleanly
    cdp.eval_js("document.getElementById('rule-cancel-edit-btn').click();")
    cdp.eval_js("document.getElementById('discard-rule-confirm-btn').click();")
    reset_val = cdp.eval_js("return document.getElementById('rule-name-input').value;")
    assert reset_val == "", reset_val

    # Test Delete Confirmation Modal (Rule 4, AC-8):
    # Click Delete on rule:ui-orders-copy -> click Cancel -> rule remains in table!
    cdp.eval_js(
        """
        document.querySelector('.btn-delete-rule[data-rule-id="rule:ui-orders-copy"]').click();
        """
    )
    del_open = cdp.eval_js("return document.getElementById('delete-rule-dialog').open;")
    assert del_open is True
    cdp.eval_js("document.getElementById('delete-rule-cancel-btn').click();")
    still_there = cdp.eval_js(
        "return Array.from(document.querySelectorAll('#rules-tbody tr')).map(tr => tr.dataset.ruleId);"
    )
    assert "rule:ui-orders-copy" in still_there, still_there

    # Now click Delete on rule:ui-orders-copy and confirm -> removed!
    cdp.eval_js(
        """
        document.querySelector('.btn-delete-rule[data-rule-id="rule:ui-orders-copy"]').click();
        document.getElementById('delete-rule-confirm-btn').click();
        """
    )
    cdp.wait_idle()
    after_del = cdp.eval_js(
        "return Array.from(document.querySelectorAll('#rules-tbody tr')).map(tr => tr.dataset.ruleId);"
    )
    assert "rule:ui-orders-copy" not in after_del, after_del

    # --- STEP 6: Validation Errors (invalid regex, duplicate rule ID, invalid JSON path) preserve user input and allow retry (AC-7, Rule 7) ---
    print("    -> [6/11] Verifying validation errors (invalid regex, duplicate ID, invalid JSON path) preserve user input and allow retry (AC-7, Rule 7)")
    cdp.eval_js("document.getElementById('new-rule-btn').click();")
    cdp.eval_js(
        """
        const setVal = (el, val) => {
          el.value = val;
          el.dispatchEvent(new Event('input', { bubbles: true }));
          el.dispatchEvent(new Event('change', { bubbles: true }));
        };
        setVal(document.getElementById('rule-name-input'), 'Retryable Validation Rule');
        setVal(document.getElementById('rule-id-input'), 'rule:ui-orders-1'); // duplicate ID!
        setVal(document.getElementById('rule-match-path-regex-input'), '[unclosed_regex');
        document.getElementById('rule-submit-btn').click();
        """
    )
    cdp.wait_idle()
    regex_err = cdp.eval_js(
        """
        return {
          code: document.getElementById('rule-form-error-box').dataset.errorCode,
          fieldErr: document.getElementById('rule-path-regex-error').textContent,
          ariaInvalid: document.getElementById('rule-match-path-regex-input').getAttribute('aria-invalid'),
          preservedName: document.getElementById('rule-name-input').value,
        };
        """
    )
    assert regex_err["code"] == "invalid_regex", regex_err
    assert regex_err["ariaInvalid"] == "true" and regex_err["fieldErr"], regex_err
    assert regex_err["preservedName"] == "Retryable Validation Rule", regex_err

    # Fix regex, submit again -> hits duplicate_rule_id while still preserving name!
    cdp.eval_js(
        """
        const rx = document.getElementById('rule-match-path-regex-input');
        rx.value = '^/v1/retry-ok$';
        rx.dispatchEvent(new Event('input', { bubbles: true }));
        document.getElementById('rule-submit-btn').click();
        """
    )
    cdp.wait_idle()
    dup_err = cdp.eval_js(
        """
        return {
          code: document.getElementById('rule-form-error-box').dataset.errorCode,
          idErr: document.getElementById('rule-id-error').textContent,
          ariaInvalid: document.getElementById('rule-id-input').getAttribute('aria-invalid'),
          preservedName: document.getElementById('rule-name-input').value,
        };
        """
    )
    assert dup_err["code"] == "duplicate_rule_id", dup_err
    assert dup_err["ariaInvalid"] == "true" and dup_err["idErr"], dup_err
    assert dup_err["preservedName"] == "Retryable Validation Rule", dup_err

    # Fix rule_id, test invalid JSON path in action -> hits invalid_json_path
    cdp.eval_js(
        """
        const setVal = (el, val) => {
          el.value = val;
          el.dispatchEvent(new Event('input', { bubbles: true }));
          el.dispatchEvent(new Event('change', { bubbles: true }));
        };
        setVal(document.getElementById('rule-id-input'), 'rule:ui-retry-ok');
        const actRow = document.querySelector('#rule-action-rows .rule-action-row');
        setVal(actRow.querySelector('.rule-action-type-select'), 'set_json_field');
        setVal(actRow.querySelector('.rule-action-target-input'), 'order..bad_path');
        setVal(actRow.querySelector('.rule-action-value-input'), 'true');
        document.getElementById('rule-submit-btn').click();
        """
    )
    cdp.wait_idle()
    jpath_err = cdp.eval_js(
        """
        return {
          code: document.getElementById('rule-form-error-box').dataset.errorCode,
          actErr: document.getElementById('rule-actions-error').textContent,
        };
        """
    )
    assert jpath_err["code"] == "invalid_json_path", jpath_err
    assert jpath_err["actErr"], jpath_err

    # Fix JSON path and save -> succeeds! Then delete rule:ui-retry-ok to keep clean
    cdp.eval_js(
        """
        const setVal = (el, val) => {
          el.value = val;
          el.dispatchEvent(new Event('input', { bubbles: true }));
          el.dispatchEvent(new Event('change', { bubbles: true }));
        };
        const actRow = document.querySelector('#rule-action-rows .rule-action-row');
        setVal(actRow.querySelector('.rule-action-target-input'), 'order.retry_ok');
        document.getElementById('rule-submit-btn').click();
        """
    )
    cdp.wait_idle()
    cdp.eval_js(
        """
        document.querySelector('.btn-delete-rule[data-rule-id="rule:ui-retry-ok"]').click();
        document.getElementById('delete-rule-confirm-btn').click();
        """
    )
    cdp.wait_idle()

    # --- STEP 7: Dry-Run Preview (Inline Request & Stored Capture) + Protected Header / Streamed / WebSocket / Unsupported Body outcomes without traffic or capture increment (AC-9, AC-10, Rule 5, Rule 6) ---
    print("    -> [7/11] Verifying Dry-Run Preview (Inline & Stored Capture) and skip/fail outcomes without target traffic or capture increment (AC-9, AC-10, Rule 5, Rule 6)")
    echo_a_before = count_echo_requests("echo-a")
    echo_b_before = count_echo_requests("echo-b")
    captures_before = get_captures_total()

    # 7a. Stored Capture Preview against SEED_JSON_CAP_ID using persisted rules
    cdp.eval_js(
        f"""
        document.getElementById('preview-source-capture').click();
        document.getElementById('preview-include-candidate-checkbox').checked = false;
        document.getElementById('preview-capture-id-input').value = '{SEED_JSON_CAP_ID}';
        document.getElementById('run-rule-preview-btn').click();
        """
    )
    cdp.wait_idle()
    cap_preview = cdp.eval_js(
        """
        return {
          outcome: document.getElementById('preview-outcome-badge').textContent,
          summary: document.getElementById('preview-outcome-summary-text').textContent,
          changeCount: Number(document.getElementById('preview-change-count').textContent),
          beforeJson: JSON.parse(document.getElementById('preview-before-json').textContent),
          afterJson: JSON.parse(document.getElementById('preview-after-json').textContent),
          bannerText: document.getElementById('preview-dry-run-banner').textContent,
        };
        """
    )
    assert cap_preview["outcome"] == "applied", cap_preview
    assert "matched=1, applied=1" in cap_preview["summary"], cap_preview
    assert cap_preview["changeCount"] >= 6, cap_preview
    assert cap_preview["beforeJson"]["json_body"]["order"]["status"] == "pending"
    assert cap_preview["afterJson"]["json_body"]["order"]["status"] == "approved_final"
    assert "DRY-RUN" in cap_preview["bannerText"]

    # 7b. Inline Preview with Protected Header (Content-Length) -> skipped with protected_header reason, feedback is error/warn (NOT success)
    cdp.eval_js(
        """
        const setVal = (el, val) => {
          el.value = val;
          el.dispatchEvent(new Event('input', { bubbles: true }));
          el.dispatchEvent(new Event('change', { bubbles: true }));
        };
        document.getElementById('new-rule-btn').click();
        setVal(document.getElementById('rule-name-input'), 'Candidate Protected Header Test');
        setVal(document.getElementById('rule-id-input'), 'rule:candidate-protected');
        const actRow = document.querySelector('#rule-action-rows .rule-action-row');
        setVal(actRow.querySelector('.rule-action-type-select'), 'set_header');
        setVal(actRow.querySelector('.rule-action-target-input'), 'Content-Length');
        setVal(actRow.querySelector('.rule-action-value-input'), '9999');

        document.getElementById('preview-source-inline').click();
        document.getElementById('preview-include-candidate-checkbox').checked = true;
        setVal(document.getElementById('preview-rule-ids-input'), 'rule:candidate-protected');
        document.getElementById('run-rule-preview-btn').click();
        """
    )
    cdp.wait_idle()
    prot_res = cdp.eval_js(
        """
        return {
          outcome: document.getElementById('preview-outcome-badge').textContent,
          fbState: document.getElementById('feedback-banner').dataset.state,
          tableText: document.getElementById('preview-rules-tbody').textContent,
          discardOpen: document.getElementById('discard-rule-edits-dialog').open,
          candChecked: document.getElementById('preview-include-candidate-checkbox').checked,
          candPayload: JSON.parse(document.getElementById('rule-payload-preview').textContent),
          errHidden: document.getElementById('preview-error-box').hidden,
          errText: document.getElementById('preview-error-message').textContent,
        };
        """
    )
    assert prot_res["outcome"] == "skipped", prot_res
    assert prot_res["fbState"] != "success", prot_res
    assert "protected_header" in prot_res["tableText"], prot_res

    # 7c. Inline Preview with WebSocket handshake -> skipped with websocket_handshake
    cdp.eval_js(
        """
        document.getElementById('preview-inline-websocket').checked = true;
        document.getElementById('run-rule-preview-btn').click();
        """
    )
    cdp.wait_idle()
    ws_res = cdp.eval_js(
        """
        return {
          outcome: document.getElementById('preview-outcome-badge').textContent,
          fbState: document.getElementById('feedback-banner').dataset.state,
          tableText: document.getElementById('preview-rules-tbody').textContent,
        };
        """
    )
    assert ws_res["outcome"].startswith("skipped") and "websocket_handshake" in ws_res["tableText"], ws_res
    assert ws_res["fbState"] != "success", ws_res

    # 7d. Inline Preview with Streamed Request -> skipped with streamed_request
    cdp.eval_js(
        """
        document.getElementById('preview-inline-websocket').checked = false;
        document.getElementById('preview-inline-streamed').checked = true;
        document.getElementById('run-rule-preview-btn').click();
        """
    )
    cdp.wait_idle()
    stream_res = cdp.eval_js(
        """
        return {
          outcome: document.getElementById('preview-outcome-badge').textContent,
          fbState: document.getElementById('feedback-banner').dataset.state,
          tableText: document.getElementById('preview-rules-tbody').textContent,
        };
        """
    )
    assert stream_res["outcome"] == "skipped" and "streamed_request" in stream_res["tableText"], stream_res
    assert stream_res["fbState"] != "success", stream_res

    # 7e. Inline Preview with Unsupported Body Type (JSON mutation against text/plain raw body) -> failed with content_type_mismatch / unsupported_body_type
    cdp.eval_js(
        """
        const setVal = (el, val) => {
          el.value = val;
          el.dispatchEvent(new Event('input', { bubbles: true }));
          el.dispatchEvent(new Event('change', { bubbles: true }));
        };
        document.getElementById('preview-inline-streamed').checked = false;
        const actRow = document.querySelector('#rule-action-rows .rule-action-row');
        setVal(actRow.querySelector('.rule-action-type-select'), 'set_json_field');
        setVal(actRow.querySelector('.rule-action-target-input'), 'order.status');
        setVal(actRow.querySelector('.rule-action-value-input'), '"x"');
        setVal(document.getElementById('preview-inline-content-type'), 'application/octet-stream');
        setVal(document.getElementById('preview-inline-body-mode'), 'raw_text');
        document.getElementById('run-rule-preview-btn').click();
        """
    )
    cdp.wait_idle()
    unsup_res = cdp.eval_js(
        """
        return {
          outcome: document.getElementById('preview-outcome-badge').textContent,
          fbState: document.getElementById('feedback-banner').dataset.state,
          tableText: document.getElementById('preview-rules-tbody').textContent,
        };
        """
    )
    assert unsup_res["outcome"] in ("failed", "skipped"), unsup_res
    assert unsup_res["fbState"] == "error", unsup_res
    assert ("unsupported_body_type" in unsup_res["tableText"]) or ("content_type_mismatch" in unsup_res["tableText"]), unsup_res

    # Clean up candidate form edits
    cdp.eval_js(
        """
        document.getElementById('rule-cancel-edit-btn').click();
        document.getElementById('discard-rule-confirm-btn').click();
        document.getElementById('preview-rule-ids-input').value = '';
        """
    )

    # Verify that all dry-run previews sent ZERO requests to echo-a/echo-b and added ZERO captures (Rule 5, AC-9)
    assert count_echo_requests("echo-a") == echo_a_before
    assert count_echo_requests("echo-b") == echo_b_before
    assert get_captures_total() == captures_before

    # --- STEP 8: Live Proxy Traffic Modification + Capture Detail Rule Evaluation Metadata & Navigation (AC-4, AC-6, AC-11) ---
    print("    -> [8/11] Sending live HTTPS traffic through proxy chain and verifying Capture Detail rule evaluation metadata & navigation (AC-4, AC-6, AC-11)")
    live_out = subprocess.run(
        [
            "curl", "-fsS", "--cacert", CACERT,
            "--resolve", f"app-a.local.test:{EDGE_HTTPS_PORT}:127.0.0.1",
            "-X", "POST",
            f"https://app-a.local.test:{EDGE_HTTPS_PORT}/v1/orders?tag=orig1&tag=orig2",
            "-H", "Content-Type: application/json; charset=utf-8",
            "-H", "X-Client: seed",
            "-H", "Cookie: sid=live99",
            "--data-binary", '{"order":{"id":777,"status":"pending"},"items":[{"sku":"L1","qty":2}]}',
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    live_echo = json.loads(live_out)
    hdrs_lower = [[k.lower(), v] for k, v in live_echo["headers"]]
    assert ["x-rule-note", "console note & a=b"] in hdrs_lower, hdrs_lower
    assert [v for k, v in hdrs_lower if k == "x-dup"] == ["first", "second"], hdrs_lower
    assert live_echo["query_params"] == [
        ["tag", "orig1"],
        ["tag", "orig2"],
        ["filter", "a&b=c 日本語"],
        ["filter", "second_val"],
    ], live_echo["query_params"]
    cookie_vals = [v for k, v in hdrs_lower if k == "cookie"]
    assert any("mode=console_ui" in cv for cv in cookie_vals), cookie_vals
    assert live_echo["body_json"]["order"]["status"] == "approved_final", live_echo["body_json"]

    subprocess.run([f"{ROOT_DIR}/scripts/capture-ctl.sh", "sync"], check=True, stdout=subprocess.DEVNULL)

    # Switch to Requests & Replay workspace, refresh, open latest capture detail, and verify #detail-rule-evaluation-box (AC-11)
    cdp.eval_js(
        """
        document.getElementById('view-requests-btn').click();
        document.getElementById('refresh-captures-btn').click();
        """
    )
    cdp.wait_idle()
    cdp.eval_js(
        """
        document.querySelector('#captures-tbody tr .btn-view-capture').click();
        """
    )
    cdp.wait_idle()
    detail_ev = cdp.eval_js(
        """
        return {
          outcome: document.getElementById('detail-rule-outcome-badge').textContent,
          appliedText: document.getElementById('detail-applied-rules-list').textContent,
          changeCount: Number(document.getElementById('detail-rule-change-count').textContent),
          resultsHidden: document.getElementById('detail-rule-results-wrap').hidden,
          changesHidden: document.getElementById('detail-rule-changes-wrap').hidden,
        };
        """
    )
    assert detail_ev["outcome"] == "applied", detail_ev
    assert "rule:ui-orders-1" in detail_ev["appliedText"], detail_ev
    assert detail_ev["changeCount"] >= 6, detail_ev
    assert detail_ev["resultsHidden"] is False and detail_ev["changesHidden"] is False, detail_ev

    # Click "Filter Requests" chip button in Capture Detail -> filters captures table by rule:ui-orders-1 (AC-11)
    cdp.eval_js(
        """
        document.querySelector('#detail-applied-rules-list .btn-filter-captures-by-rule[data-rule-id="rule:ui-orders-1"]').click();
        """
    )
    cdp.wait_idle()
    filtered_by_rule = cdp.eval_js(
        """
        return {
          ruleFilterVal: document.getElementById('filter-rule-id-input').value,
          rowCount: document.querySelectorAll('#captures-tbody tr').length,
        };
        """
    )
    assert filtered_by_rule["ruleFilterVal"] == "rule:ui-orders-1", filtered_by_rule
    assert filtered_by_rule["rowCount"] == 1, filtered_by_rule

    # Click "Open in Rules" chip button in Capture Detail -> navigates to Rules workspace and loads rule:ui-orders-1 into form (AC-11)
    cdp.eval_js(
        """
        document.querySelector('#detail-applied-rules-list .btn-open-rule-from-detail[data-rule-id="rule:ui-orders-1"]').click();
        """
    )
    cdp.wait_idle()
    nav_to_rule = cdp.eval_js(
        """
        return {
          rulesHidden: document.getElementById('rules-workspace').hidden,
          editMode: document.getElementById('rule-edit-mode').value,
          loadedRuleId: document.getElementById('rule-id-input').value,
        };
        """
    )
    assert nav_to_rule["rulesHidden"] is False, nav_to_rule
    assert nav_to_rule["editMode"] == "edit" and nav_to_rule["loadedRuleId"] == "rule:ui-orders-1", nav_to_rule

    # --- STEP 9: Priority Reordering, Disable/Enable, and Persistence Across Browser Reload & Container Restarts (AC-6) ---
    print("    -> [9/11] Verifying priority reordering, disable/enable on live traffic, and persistence across reload + container restarts (AC-6)")
    # Create a second rule matching /v1/orders with priority 60 that sets order.status = "from_rule_2"
    cdp.eval_js("document.getElementById('new-rule-btn').click();")
    cdp.eval_js(
        """
        const setVal = (el, val) => {
          el.value = val;
          el.dispatchEvent(new Event('input', { bubbles: true }));
          el.dispatchEvent(new Event('change', { bubbles: true }));
        };
        setVal(document.getElementById('rule-name-input'), 'Orders Override Rule 2');
        setVal(document.getElementById('rule-id-input'), 'rule:ui-orders-override');
        setVal(document.getElementById('rule-priority-input'), '60');
        setVal(document.getElementById('rule-match-path-exact-input'), '/v1/orders');

        const actRow = document.querySelector('#rule-action-rows .rule-action-row');
        setVal(actRow.querySelector('.rule-action-type-select'), 'set_json_field');
        setVal(actRow.querySelector('.rule-action-target-input'), 'order.status');
        setVal(actRow.querySelector('.rule-action-value-input'), '"override_prio_60"');
        document.getElementById('rule-submit-btn').click();
        """
    )
    cdp.wait_idle()

    # Right now rule:ui-orders-1 is prio 50 and rule:ui-orders-override is prio 60 -> prio 60 runs second and wins ("override_prio_60")
    out_before_reorder = json.loads(
        subprocess.run(
            [
                "curl", "-fsS", "--cacert", CACERT,
                "--resolve", f"app-a.local.test:{EDGE_HTTPS_PORT}:127.0.0.1",
                "-X", "POST",
                f"https://app-a.local.test:{EDGE_HTTPS_PORT}/v1/orders?tag=orig1",
                "-H", "Content-Type: application/json",
                "-H", "X-Client: seed",
                "--data-binary", '{"order":{"id":778,"status":"pending"}}',
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    )
    assert out_before_reorder["body_json"]["order"]["status"] == "override_prio_60", out_before_reorder

    # Click "↑ Earlier (-10)" on rule:ui-orders-override in the Rules table so it runs BEFORE rule:ui-orders-1 (prio 45 < 50) -> rule:ui-orders-1 runs last and wins ("approved_final")!
    cdp.eval_js(
        """
        document.querySelector('.btn-rule-priority-earlier[data-rule-id="rule:ui-orders-override"]').click();
        """
    )
    cdp.wait_idle()
    out_after_reorder = json.loads(
        subprocess.run(
            [
                "curl", "-fsS", "--cacert", CACERT,
                "--resolve", f"app-a.local.test:{EDGE_HTTPS_PORT}:127.0.0.1",
                "-X", "POST",
                f"https://app-a.local.test:{EDGE_HTTPS_PORT}/v1/orders?tag=orig1",
                "-H", "Content-Type: application/json",
                "-H", "X-Client: seed",
                "--data-binary", '{"order":{"id":779,"status":"pending"}}',
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    )
    assert out_after_reorder["body_json"]["order"]["status"] == "approved_final", out_after_reorder

    # Disable rule:ui-orders-1 via UI Toggle button -> next request gets "override_prio_60"
    cdp.eval_js(
        """
        document.querySelector('.btn-toggle-rule[data-rule-id="rule:ui-orders-1"]').click();
        """
    )
    cdp.wait_idle()
    out_after_disable = json.loads(
        subprocess.run(
            [
                "curl", "-fsS", "--cacert", CACERT,
                "--resolve", f"app-a.local.test:{EDGE_HTTPS_PORT}:127.0.0.1",
                "-X", "POST",
                f"https://app-a.local.test:{EDGE_HTTPS_PORT}/v1/orders?tag=orig1",
                "-H", "Content-Type: application/json",
                "-H", "X-Client: seed",
                "--data-binary", '{"order":{"id":780,"status":"pending"}}',
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    )
    assert out_after_disable["body_json"]["order"]["status"] == "override_prio_60", out_after_disable

    # Re-enable rule:ui-orders-1 via UI Toggle button
    cdp.eval_js(
        """
        document.querySelector('.btn-toggle-rule[data-rule-id="rule:ui-orders-1"]').click();
        """
    )
    cdp.wait_idle()

    # Restart control-api AND inspector containers, reload browser page, and verify rules & order persist (AC-6)
    subprocess.run(
        ["docker", "compose", "-f", COMPOSE_FILE, "restart", "control-api", "inspector"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=True,
    )
    for _ in range(30):
        try:
            with urllib.request.urlopen(f"{API_BASE}/api/v1/health", timeout=1.0) as r:
                if r.status == 200:
                    break
        except Exception:
            pass
        time.sleep(0.5)

    cdp.call("Page.reload")
    cdp.wait_idle()
    cdp.eval_js("document.getElementById('view-rules-btn').click();")
    persisted_rules = cdp.eval_js(
        """
        return Array.from(document.querySelectorAll('#rules-tbody tr')).map(tr => ({
          id: tr.dataset.ruleId,
          prio: Number(tr.dataset.priority),
          enabled: tr.dataset.enabled,
        }));
        """
    )
    assert [r["id"] for r in persisted_rules] == [
        "rule:ui-orders-override",
        "rule:ui-orders-1",
        "rule:ui-form-2",
    ], persisted_rules

    # --- STEP 10: Workspace Isolation when Rules API is Unavailable (AC-12, Rule 8) ---
    print("    -> [10/11] Verifying Routes, Docker, Requests, and Replay workspaces remain usable when Rules API returns HTTP 503 rules_unavailable (AC-12, Rule 8)")
    req_cfg = urllib.request.Request(
        f"{API_BASE}/api/v1/rules/config",
        data=json.dumps({"simulate_unavailable": True}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    urllib.request.urlopen(req_cfg, timeout=5.0).read()

    cdp.eval_js("document.getElementById('refresh-rules-btn').click();")
    cdp.wait_idle()
    unavail_state = cdp.eval_js(
        """
        return {
          badgeAvail: document.getElementById('rules-availability-badge').dataset.available,
          errHidden: document.getElementById('rules-error-box').hidden,
          errCode: document.getElementById('rules-error-box').dataset.errorCode,
        };
        """
    )
    assert unavail_state["badgeAvail"] == "false", unavail_state
    assert unavail_state["errHidden"] is False and unavail_state["errCode"] == "rules_unavailable", unavail_state

    # Switch to Routes & Containers workspace and verify routes & containers are still loaded and usable
    cdp.eval_js("document.getElementById('view-routes-btn').click();")
    routes_ok = cdp.eval_js(
        """
        return {
          routesHidden: document.getElementById('routes-workspace').hidden,
          routeRows: document.querySelectorAll('#routes-tbody tr').length,
          dockerRows: document.querySelectorAll('#docker-tbody tr').length,
        };
        """
    )
    assert routes_ok["routesHidden"] is False and routes_ok["routeRows"] >= 2 and routes_ok["dockerRows"] >= 2, routes_ok

    # Switch to Requests & Replay workspace and verify captures list & detail still work
    cdp.eval_js(
        """
        document.getElementById('view-requests-btn').click();
        document.getElementById('captures-filter-reset-btn').click();
        """
    )
    cdp.wait_idle()
    reqs_ok = cdp.eval_js(
        """
        return {
          reqsHidden: document.getElementById('requests-workspace').hidden,
          captureRows: document.querySelectorAll('#captures-tbody tr').length,
        };
        """
    )
    assert reqs_ok["reqsHidden"] is False and reqs_ok["captureRows"] >= 2, reqs_ok

    # Restore Rules API availability and verify Retry button recovers Rules workspace
    req_cfg_restore = urllib.request.Request(
        f"{API_BASE}/api/v1/rules/config",
        data=json.dumps({"simulate_unavailable": False}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    urllib.request.urlopen(req_cfg_restore, timeout=5.0).read()

    cdp.eval_js(
        """
        document.getElementById('view-rules-btn').click();
        document.getElementById('retry-rules-load-btn').click();
        """
    )
    cdp.wait_idle()
    restored_state = cdp.eval_js(
        """
        return {
          badgeAvail: document.getElementById('rules-availability-badge').dataset.available,
          errHidden: document.getElementById('rules-error-box').hidden,
          ruleCount: document.querySelectorAll('#rules-tbody tr').length,
        };
        """
    )
    assert restored_state["badgeAvail"] == "true" and restored_state["errHidden"] is True and restored_state["ruleCount"] == 3, restored_state

finally:
    if cdp is not None:
        cdp.close()
    proc.terminate()
    try:
        proc.wait(timeout=5.0)
    except Exception:
        proc.kill()
    shutil.rmtree(user_data_dir, ignore_errors=True)
PY

echo "==> [11/11] Checking git status hygiene for runtime artifacts"
git_dirty="$(git -C "${ROOT_DIR}" status --porcelain -- var/ 2>/dev/null || true)"
if [[ -n "${git_dirty}" ]]; then
  echo "ERROR: Runtime files appeared in git status: ${git_dirty}" >&2
  exit 1
fi

echo "PASS: All DEV-041 Web Modification Rules Console E2E tests succeeded!"
