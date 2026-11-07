#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
COMPOSE_FILE="${ROOT_DIR}/deploy/compose/docker-compose.yml"
EDGE_HTTPS_PORT="${EDGE_HTTPS_PORT:-8443}"
CONTROL_API_PORT="${CONTROL_API_PORT:-18480}"
API_BASE="http://127.0.0.1:${CONTROL_API_PORT}"
CACERT="${ROOT_DIR}/var/certificates/edge.crt"

cleanup() {
  curl -fsS -X POST "${API_BASE}/api/v1/discovery/config" \
    -H "Content-Type: application/json" \
    -d '{"socket_path_override": null}' >/dev/null 2>&1 || true
  docker compose -f "${COMPOSE_FILE}" up -d echo-b >/dev/null 2>&1 || true
}
trap cleanup EXIT

echo "==> [1/10] Starting stack and verifying Web Console static serving on localhost (AC-1, AC-2, Rule 10)"
"${ROOT_DIR}/scripts/down.sh" >/dev/null 2>&1 || true
rm -rf "${ROOT_DIR}/var/captures" "${ROOT_DIR}/var/blobs" "${ROOT_DIR}/var/database"
"${ROOT_DIR}/scripts/up.sh"

for _ in $(seq 1 30); do
  if curl -fsS "${API_BASE}/api/v1/health" >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

index_html="$(curl -fsS -D /tmp/pc_index_hdr.txt "${API_BASE}/")"
console_html="$(curl -fsS "${API_BASE}/console")"
css_body="$(curl -fsS -D /tmp/pc_css_hdr.txt "${API_BASE}/styles.css")"
js_body="$(curl -fsS -D /tmp/pc_js_hdr.txt "${API_BASE}/app.js")"
curl -fsS "${API_BASE}/favicon.ico" >/dev/null

INDEX_HTML="${index_html}" CONSOLE_HTML="${console_html}" CSS_BODY="${css_body}" JS_BODY="${js_body}" python3 - <<'PY'
import os

index_html = os.environ["INDEX_HTML"]
console_html = os.environ["CONSOLE_HTML"]
css_body = os.environ["CSS_BODY"]
js_body = os.environ["JS_BODY"]

with open("/tmp/pc_index_hdr.txt", encoding="utf-8") as f:
    idx_hdr = f.read().lower()
with open("/tmp/pc_css_hdr.txt", encoding="utf-8") as f:
    css_hdr = f.read().lower()
with open("/tmp/pc_js_hdr.txt", encoding="utf-8") as f:
    js_hdr = f.read().lower()

assert "text/html" in idx_hdr, idx_hdr
assert "text/css" in css_hdr, css_hdr
assert "javascript" in js_hdr, js_hdr
assert index_html == console_html
assert '<table id="routes-table">' in index_html
assert '<table id="docker-containers-table">' in index_html
assert '<dialog id="copy-route-dialog"' in index_html
assert '<dialog id="delete-route-dialog"' in index_html
assert "@container" in css_body
assert "loadAllState" in js_body
PY
rm -f /tmp/pc_index_hdr.txt /tmp/pc_css_hdr.txt /tmp/pc_js_hdr.txt

echo "==> [2/10] Launching headless browser and running end-to-end UI + live HTTPS routing verification (AC-3..AC-12, Rules 1..9)"
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
import ssl
import struct
import subprocess
import sys
import tempfile
import time
import urllib.error
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


def https_request(host_header: str, path: str = "/test") -> tuple[int, str]:
    ctx = ssl.create_default_context(cafile=CACERT)
    ctx.check_hostname = False
    conn = socket.create_connection(("127.0.0.1", EDGE_HTTPS_PORT), timeout=5.0)
    with ctx.wrap_socket(conn, server_hostname=host_header) as tls:
        req = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host_header}\r\n"
            "Connection: close\r\n\r\n"
        )
        tls.sendall(req.encode("ascii"))
        raw = b""
        while True:
            chunk = tls.recv(4096)
            if not chunk:
                break
            raw += chunk
    head, _, body = raw.partition(b"\r\n\r\n")
    status_line = head.splitlines()[0].decode("latin1")
    code = int(status_line.split()[1])
    return code, body.decode("utf-8", errors="replace")


def api_post(path: str, payload: dict) -> dict:
    req = urllib.request.Request(
        f"{API_BASE}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10.0) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return json.loads(exc.read().decode("utf-8"))


browser_bin = find_browser_binary()
with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
    s.bind(("127.0.0.1", 0))
    debug_port = s.getsockname()[1]

user_data_dir = tempfile.mkdtemp(prefix="pc_chrome_")
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

    # --- STEP 3: Initial Load & Route List / Docker Discovery Table (AC-3, Rule 1, Rule 3) ---
    print("    -> [3/10] Verifying initial route table and Docker discovery rendering in browser (AC-3, Rule 1, Rule 3)")
    initial = cdp.eval_js(
        """
        const routeRows = Array.from(document.querySelectorAll('#routes-tbody tr')).map(tr => ({
          routeId: tr.dataset.routeId,
          domain: tr.dataset.domain,
          enabled: tr.querySelector('[data-enabled]')?.dataset.enabled,
          upstream: tr.querySelector('td:nth-child(3) code')?.textContent,
          targetType: tr.querySelector('[data-target-type]')?.dataset.targetType,
          targetState: tr.querySelector('[data-target-state]')?.dataset.targetState,
        }));
        const dockerRows = Array.from(document.querySelectorAll('#docker-tbody tr')).map(tr => ({
          service: tr.dataset.serviceName,
          state: tr.dataset.containerState,
          targetId: tr.dataset.targetId,
          btnText: tr.querySelector('.btn-select-target')?.textContent?.trim(),
          btnDisabled: tr.querySelector('.btn-select-target')?.disabled,
        }));
        const syncBadge = document.getElementById('apply-sync-badge')?.dataset.inSync;
        return { routeRows, dockerRows, syncBadge };
        """
    )
    assert initial["syncBadge"] == "true", initial
    assert len(initial["routeRows"]) == 2, initial["routeRows"]
    r_by_id = {r["routeId"]: r for r in initial["routeRows"]}
    assert r_by_id["route:app-a"]["domain"] == "app-a.local.test"
    assert r_by_id["route:app-a"]["enabled"] == "true"
    assert r_by_id["route:app-a"]["upstream"] == "http://echo-a:8080"
    assert r_by_id["route:app-a"]["targetType"] == "manual"
    assert r_by_id["route:app-a"]["targetState"] == "running"

    target_ids_in_ui = [d["targetId"] for d in initial["dockerRows"]]
    assert "docker:echo-a:8080" in target_ids_in_ui, target_ids_in_ui
    assert "docker:echo-b:8080" in target_ids_in_ui, target_ids_in_ui
    assert "docker:echo-b:8081" in target_ids_in_ui, target_ids_in_ui
    assert all(d["service"] in ("echo-a", "echo-b") for d in initial["dockerRows"]), initial["dockerRows"]

    # Toggle "Show ProxyConsole system containers" checkbox and verify SYSTEM containers appear as guarded
    cdp.eval_js(
        """
        const cb = document.getElementById('include-self-checkbox');
        cb.checked = true;
        cb.dispatchEvent(new Event('change', { bubbles: true }));
        """
    )
    cdp.wait_idle()
    with_self = cdp.eval_js(
        """
        return Array.from(document.querySelectorAll('#docker-tbody tr')).map(tr => ({
          service: tr.dataset.serviceName,
          btnText: tr.querySelector('.btn-select-target')?.textContent?.trim(),
          btnDisabled: tr.querySelector('.btn-select-target')?.disabled,
        }));
        """
    )
    services_with_self = {r["service"] for r in with_self}
    for sys_svc in ("edge", "inspector", "control-api", "router"):
        assert sys_svc in services_with_self, services_with_self
    for r in with_self:
        if r["service"] in ("edge", "inspector", "control-api", "router") and r["btnText"]:
            assert r["btnDisabled"] is True, r
            assert "System" in r["btnText"], r

    # Uncheck include-self back to default
    cdp.eval_js(
        """
        const cb = document.getElementById('include-self-checkbox');
        cb.checked = false;
        cb.dispatchEvent(new Event('change', { bubbles: true }));
        """
    )
    cdp.wait_idle()

    # --- STEP 4: Create & Edit Route with Manual Upstream Target via Browser UI (AC-4) ---
    print("    -> [4/10] Creating and editing a Manual Target route in browser and verifying live HTTPS routing (AC-4)")
    cdp.eval_js(
        """
        const manualRadio = document.getElementById('mode-manual');
        manualRadio.checked = true;
        manualRadio.dispatchEvent(new Event('change', { bubbles: true }));
        document.getElementById('route-domain').value = 'ui-manual.local.test';
        document.getElementById('route-id-input').value = 'route:ui-manual';
        document.getElementById('route-upstream-scheme').value = 'http';
        document.getElementById('route-upstream-host').value = 'echo-a';
        document.getElementById('route-upstream-port').value = '8080';
        document.getElementById('route-description').value = 'Created via manual UI mode';
        document.getElementById('route-submit-btn').click();
        """
    )
    cdp.wait_idle()
    fb = cdp.eval_js("return { state: document.getElementById('feedback-banner').dataset.state, title: document.getElementById('feedback-title').textContent };")
    assert fb["state"] == "success" and fb["title"] == "Route Created", fb

    code, body = https_request("ui-manual.local.test", "/manual-create-check")
    assert code == 200, (code, body)
    data = json.loads(body)
    assert data["target"] == "echo-a" and data["listen_port"] == 8080, data

    # Edit route:ui-manual in browser UI to point to echo-b:8081
    cdp.eval_js(
        """
        document.querySelector('.btn-edit-route[data-route-id="route:ui-manual"]').click();
        """
    )
    edit_form_state = cdp.eval_js(
        """
        return {
          mode: document.getElementById('route-edit-mode').value,
          origId: document.getElementById('route-original-id').value,
          domain: document.getElementById('route-domain').value,
          host: document.getElementById('route-upstream-host').value,
          port: document.getElementById('route-upstream-port').value,
        };
        """
    )
    assert edit_form_state["mode"] == "edit", edit_form_state
    assert edit_form_state["origId"] == "route:ui-manual", edit_form_state
    assert edit_form_state["host"] == "echo-a", edit_form_state

    cdp.eval_js(
        """
        document.getElementById('route-upstream-host').value = 'echo-b';
        document.getElementById('route-upstream-port').value = '8081';
        document.getElementById('route-description').value = 'Updated via manual UI edit';
        document.getElementById('route-submit-btn').click();
        """
    )
    cdp.wait_idle()
    fb = cdp.eval_js("return { state: document.getElementById('feedback-banner').dataset.state, title: document.getElementById('feedback-title').textContent };")
    assert fb["state"] == "success" and fb["title"] == "Route Updated", fb

    code, body = https_request("ui-manual.local.test", "/manual-edit-check")
    assert code == 200, (code, body)
    data = json.loads(body)
    assert data["target"] == "echo-b" and data["listen_port"] == 8081, data

    # --- STEP 5: Docker Discovery Selection & Multi-Route Sharing in Browser UI (AC-5, Rule 3) ---
    print("    -> [5/10] Selecting Docker container+port target from discovery table and creating multiple routes (AC-5, Rule 3)")
    cdp.eval_js("document.getElementById('refresh-discovery-btn').click();")
    cdp.wait_idle()

    # Click 'Use Target' button on docker:echo-b:8081 row in the Docker table
    cdp.eval_js(
        """
        document.querySelector('.btn-select-target[data-target-id="docker:echo-b:8081"]').click();
        """
    )
    selected_target = cdp.eval_js("return document.getElementById('route-target-select').value;")
    assert selected_target == "docker:echo-b:8081", selected_target

    cdp.eval_js(
        """
        document.getElementById('route-domain').value = 'ui-docker-1.local.test';
        document.getElementById('route-id-input').value = 'route:ui-docker-1';
        document.getElementById('route-description').value = 'Docker target route 1';
        document.getElementById('route-submit-btn').click();
        """
    )
    cdp.wait_idle()

    # Create a second route sharing the exact same Docker target (docker:echo-b:8081)
    cdp.eval_js(
        """
        document.querySelector('.btn-select-target[data-target-id="docker:echo-b:8081"]').click();
        document.getElementById('route-domain').value = 'ui-docker-2.local.test';
        document.getElementById('route-id-input').value = 'route:ui-docker-2';
        document.getElementById('route-description').value = 'Docker target route 2 sharing echo-b:8081';
        document.getElementById('route-submit-btn').click();
        """
    )
    cdp.wait_idle()

    for dom in ("ui-docker-1.local.test", "ui-docker-2.local.test"):
        code, body = https_request(dom, "/docker-target-check")
        assert code == 200, (dom, code, body)
        d = json.loads(body)
        assert d["target"] == "echo-b" and d["listen_port"] == 8081 and d["host"] == dom, d

    # --- STEP 6: Route Copy via Modal <dialog> with New Domain Validation (AC-6, Rule 6) ---
    print("    -> [6/10] Testing Route Copy modal dialog, domain conflict prevention, and live routing (AC-6, Rule 6)")
    cdp.eval_js(
        """
        document.querySelector('.btn-copy-route[data-route-id="route:ui-docker-1"]').click();
        """
    )
    dialog_open = cdp.eval_js("return document.getElementById('copy-route-dialog').open;")
    assert dialog_open is True

    # Submit empty domain -> should stay open and display inline error
    cdp.eval_js(
        """
        document.getElementById('copy-new-domain').value = '';
        document.getElementById('copy-confirm-btn').click();
        """
    )
    err_empty = cdp.eval_js(
        "return { open: document.getElementById('copy-route-dialog').open, err: document.getElementById('copy-domain-error').textContent };"
    )
    assert err_empty["open"] is True and "required" in err_empty["err"], err_empty

    # Submit same domain as source -> should stay open and display conflict error
    cdp.eval_js(
        """
        document.getElementById('copy-new-domain').value = 'ui-docker-1.local.test';
        document.getElementById('copy-confirm-btn').click();
        """
    )
    err_same = cdp.eval_js(
        "return { open: document.getElementById('copy-route-dialog').open, err: document.getElementById('copy-domain-error').textContent };"
    )
    assert err_same["open"] is True and "different" in err_same["err"], err_same

    # Submit valid new domain -> copies route, closes modal, applies to Nginx
    cdp.eval_js(
        """
        document.getElementById('copy-new-domain').value = 'ui-docker-copy.local.test';
        document.getElementById('copy-new-route-id').value = 'route:ui-docker-copy';
        document.getElementById('copy-confirm-btn').click();
        """
    )
    cdp.wait_idle()
    after_copy = cdp.eval_js(
        """
        const dlgOpen = document.getElementById('copy-route-dialog').open;
        const row = document.querySelector('#routes-tbody tr[data-route-id="route:ui-docker-copy"]');
        return {
          dlgOpen,
          exists: Boolean(row),
          metaText: row?.querySelector('th .cell-meta')?.textContent || '',
        };
        """
    )
    assert after_copy["dlgOpen"] is False, after_copy
    assert after_copy["exists"] is True, after_copy
    assert "copied from route:ui-docker-1" in after_copy["metaText"], after_copy

    code_src, _ = https_request("ui-docker-1.local.test", "/copy-src-check")
    code_cpy, body_cpy = https_request("ui-docker-copy.local.test", "/copy-dst-check")
    assert code_src == 200 and code_cpy == 200
    assert json.loads(body_cpy)["listen_port"] == 8081

    # --- STEP 7: Enable / Disable & Page Reload State Consistency (AC-7, Rule 9) ---
    print("    -> [7/10] Testing Route Disable/Enable in browser and state consistency across page reload (AC-7, Rule 9)")
    cdp.eval_js(
        """
        document.querySelector('.btn-toggle-enable[data-route-id="route:ui-docker-copy"]').click();
        """
    )
    cdp.wait_idle()
    code_dis, _ = https_request("ui-docker-copy.local.test", "/should-404")
    assert code_dis == 404, f"Expected 404 for disabled route, got {code_dis}"

    # Reload the page in the browser and verify server state is rendered accurately (Rule 9)
    cdp.call("Page.reload", {"ignoreCache": True})
    time.sleep(0.2)
    cdp.wait_idle()
    reloaded_state = cdp.eval_js(
        """
        const row = document.querySelector('#routes-tbody tr[data-route-id="route:ui-docker-copy"]');
        return {
          enabledAttr: row?.querySelector('[data-enabled]')?.dataset.enabled,
          badgeText: row?.querySelector('[data-enabled]')?.textContent?.trim(),
          toggleBtnText: row?.querySelector('.btn-toggle-enable')?.textContent?.trim(),
        };
        """
    )
    assert reloaded_state["enabledAttr"] == "false", reloaded_state
    assert reloaded_state["badgeText"] == "Disabled", reloaded_state
    assert reloaded_state["toggleBtnText"] == "Enable", reloaded_state

    # Re-enable route from browser UI
    cdp.eval_js(
        """
        document.querySelector('.btn-toggle-enable[data-route-id="route:ui-docker-copy"]').click();
        """
    )
    cdp.wait_idle()
    code_en, _ = https_request("ui-docker-copy.local.test", "/reenabled-200")
    assert code_en == 200, f"Expected 200 after re-enabling route, got {code_en}"

    # --- STEP 8: Destructive Delete Confirmation <dialog> (AC-8, Rule 7) ---
    print("    -> [8/10] Testing destructive Delete confirmation dialog and cancel/confirm flows (AC-8, Rule 7)")
    cdp.eval_js(
        """
        document.querySelector('.btn-delete-route[data-route-id="route:ui-docker-copy"]').click();
        """
    )
    del_dlg = cdp.eval_js(
        """
        return {
          open: document.getElementById('delete-route-dialog').open,
          targetId: document.getElementById('delete-target-route-id').textContent,
          targetDomain: document.getElementById('delete-target-domain').textContent,
        };
        """
    )
    assert del_dlg["open"] is True, del_dlg
    assert del_dlg["targetId"] == "route:ui-docker-copy", del_dlg
    assert del_dlg["targetDomain"] == "ui-docker-copy.local.test", del_dlg

    # Click Cancel first -> route must NOT be deleted
    cdp.eval_js("document.getElementById('delete-cancel-btn').click();")
    code_still_ok, _ = https_request("ui-docker-copy.local.test", "/after-cancel")
    assert code_still_ok == 200

    # Open delete dialog again and confirm deletion
    cdp.eval_js(
        """
        document.querySelector('.btn-delete-route[data-route-id="route:ui-docker-copy"]').click();
        document.getElementById('delete-confirm-btn').click();
        """
    )
    cdp.wait_idle()
    row_gone = cdp.eval_js(
        "return document.querySelector('#routes-tbody tr[data-route-id=\"route:ui-docker-copy\"]') === null;"
    )
    assert row_gone is True
    code_deleted, _ = https_request("ui-docker-copy.local.test", "/after-delete")
    assert code_deleted == 404

    # --- STEP 9: Validation Errors & Stopped Container Behavior in Browser UI (AC-9, Rule 4, Rule 8) ---
    print("    -> [9/10] Testing form validation errors (duplicate domain, invalid port, empty host, stopped target) in browser (AC-9, Rule 4, Rule 8)")
    # 9a. Invalid port & empty host in Manual mode
    cdp.eval_js(
        """
        document.getElementById('new-route-btn').click();
        const manualRadio = document.getElementById('mode-manual');
        manualRadio.checked = true;
        manualRadio.dispatchEvent(new Event('change', { bubbles: true }));
        document.getElementById('route-domain').value = 'bad-input.local.test';
        document.getElementById('route-upstream-host').value = '';
        document.getElementById('route-upstream-port').value = '70000';
        document.getElementById('route-submit-btn').click();
        """
    )
    val_errs = cdp.eval_js(
        """
        return {
          fbState: document.getElementById('feedback-banner').dataset.state,
          hostErr: document.getElementById('route-host-error').textContent,
          portErr: document.getElementById('route-port-error').textContent,
        };
        """
    )
    assert val_errs["fbState"] == "error", val_errs
    assert "required" in val_errs["hostErr"], val_errs
    assert "65535" in val_errs["portErr"], val_errs

    # 9b. Duplicate domain rejected by backend (409 duplicate_domain) and displayed on field
    cdp.eval_js(
        """
        document.getElementById('route-domain').value = 'app-a.local.test';
        document.getElementById('route-upstream-host').value = 'echo-a';
        document.getElementById('route-upstream-port').value = '8080';
        document.getElementById('route-submit-btn').click();
        """
    )
    cdp.wait_idle()
    dup_err = cdp.eval_js(
        """
        return {
          fbState: document.getElementById('feedback-banner').dataset.state,
          fbCode: document.getElementById('feedback-banner').dataset.errorCode,
          domainErr: document.getElementById('route-domain-error').textContent,
        };
        """
    )
    assert dup_err["fbState"] == "error", dup_err
    assert dup_err["fbCode"] == "duplicate_domain", dup_err
    assert "app-a.local.test" in dup_err["domainErr"], dup_err

    # 9c. Stop echo-b container -> refresh in UI -> verify existing routes preserved with target_state='stopped' and stopped target rejected
    subprocess.run(
        ["docker", "compose", "-f", COMPOSE_FILE, "stop", "echo-b"],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    cdp.eval_js("document.getElementById('refresh-discovery-btn').click();")
    cdp.wait_idle()

    stopped_ui = cdp.eval_js(
        """
        const routeRow = document.querySelector('#routes-tbody tr[data-route-id="route:ui-docker-1"]');
        const dockerBtn = document.querySelector('.btn-select-target[data-target-id="docker:echo-b:8081"]');
        return {
          routeExists: Boolean(routeRow),
          routeTargetState: routeRow?.querySelector('[data-target-state]')?.dataset.targetState,
          dockerBtnDisabled: dockerBtn?.disabled,
          dockerBtnText: dockerBtn?.textContent?.trim(),
        };
        """
    )
    assert stopped_ui["routeExists"] is True, "Stopped container must NOT auto-delete route (Rule 4)"
    assert stopped_ui["routeTargetState"] == "stopped", stopped_ui
    assert stopped_ui["dockerBtnDisabled"] is True, stopped_ui
    assert "Stopped" in stopped_ui["dockerBtnText"], stopped_ui

    # Attempting to select stopped option in <select> and submit is blocked with error
    cdp.eval_js(
        """
        document.getElementById('new-route-btn').click();
        document.getElementById('route-domain').value = 'stopped-attempt.local.test';
        document.getElementById('route-target-select').value = 'docker:echo-b:8081';
        document.getElementById('route-submit-btn').click();
        """
    )
    stopped_err = cdp.eval_js(
        """
        return {
          fbState: document.getElementById('feedback-banner').dataset.state,
          targetErr: document.getElementById('route-target-error').textContent,
        };
        """
    )
    assert stopped_err["fbState"] == "error", stopped_err
    assert "stopped" in stopped_err["targetErr"].lower(), stopped_err

    # Restart echo-b and refresh
    subprocess.run(
        ["docker", "compose", "-f", COMPOSE_FILE, "up", "-d", "echo-b"],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    cdp.eval_js("document.getElementById('refresh-discovery-btn').click();")
    cdp.wait_idle()

    # --- STEP 10: Docker Unavailable Fallback & Nginx Apply Error / Retry in Browser UI (AC-10, AC-11, Rule 5, Rule 8) ---
    print("    -> [10/10] Testing Docker unavailable manual fallback and Nginx apply error/retry in browser (AC-10, AC-11, Rule 5, Rule 8)")
    api_post("/api/v1/discovery/config", {"socket_path_override": "/var/run/nonexistent-docker.sock"})
    cdp.eval_js("document.getElementById('refresh-discovery-btn').click();")
    cdp.wait_idle()

    unavail_ui = cdp.eval_js(
        """
        return {
          badgeAvail: document.getElementById('discovery-availability-badge').dataset.available,
          badgeText: document.getElementById('discovery-availability-badge').textContent.trim(),
          errBoxHidden: document.getElementById('discovery-error-box').hidden,
          errMsg: document.getElementById('discovery-error-message').textContent,
        };
        """
    )
    assert unavail_ui["badgeAvail"] == "false", unavail_ui
    assert unavail_ui["badgeText"] == "Docker Unavailable", unavail_ui
    assert unavail_ui["errBoxHidden"] is False, unavail_ui
    assert "nonexistent-docker.sock" in unavail_ui["errMsg"], unavail_ui

    # Click 'Use Manual Host/Port Input' fallback button and create a route while Docker discovery is down
    cdp.eval_js(
        """
        document.getElementById('switch-to-manual-btn').click();
        document.getElementById('route-domain').value = 'ui-fallback.local.test';
        document.getElementById('route-id-input').value = 'route:ui-fallback';
        document.getElementById('route-upstream-host').value = 'echo-a';
        document.getElementById('route-upstream-port').value = '8080';
        document.getElementById('route-submit-btn').click();
        """
    )
    cdp.wait_idle()
    code_fb, body_fb = https_request("ui-fallback.local.test", "/fallback-ok")
    assert code_fb == 200 and json.loads(body_fb)["target"] == "echo-a"

    # Restore Docker socket override
    api_post("/api/v1/discovery/config", {"socket_path_override": None})
    cdp.eval_js("document.getElementById('refresh-discovery-btn').click();")
    cdp.wait_idle()

    # Trigger Nginx validation failure and verify UI displays apply error box and Retry Apply Config recovers
    api_post(
        "/api/v1/routes/apply",
        {"inject_invalid_directive": "invalid_nginx_directive_for_ui_test on;"},
    )
    cdp.eval_js("document.getElementById('refresh-all-btn').click();")
    cdp.wait_idle()

    apply_err_ui = cdp.eval_js(
        """
        return {
          inSync: document.getElementById('apply-sync-badge').dataset.inSync,
          statusText: document.getElementById('apply-status-text').textContent.trim(),
          errBoxHidden: document.getElementById('apply-error-box').hidden,
          errMsg: document.getElementById('apply-error-message').textContent,
        };
        """
    )
    assert apply_err_ui["inSync"] == "false", apply_err_ui
    assert apply_err_ui["statusText"] == "validation_failed", apply_err_ui
    assert apply_err_ui["errBoxHidden"] is False, apply_err_ui
    assert "invalid_nginx_directive_for_ui_test" in apply_err_ui["errMsg"], apply_err_ui

    # Existing routes still work while apply error is displayed
    code_a, _ = https_request("app-a.local.test", "/still-alive")
    assert code_a == 200

    # Click 'Retry Apply Config' button in browser -> recovers to In Sync / applied
    cdp.eval_js("document.getElementById('retry-apply-btn').click();")
    cdp.wait_idle()
    recovered_ui = cdp.eval_js(
        """
        return {
          inSync: document.getElementById('apply-sync-badge').dataset.inSync,
          statusText: document.getElementById('apply-status-text').textContent.trim(),
          errBoxHidden: document.getElementById('apply-error-box').hidden,
        };
        """
    )
    assert recovered_ui["inSync"] == "true", recovered_ui
    assert recovered_ui["statusText"] == "applied", recovered_ui
    assert recovered_ui["errBoxHidden"] is True, recovered_ui

    # Clean up test routes via the UI delete dialog so only default routes remain
    for rid in ("route:ui-manual", "route:ui-docker-1", "route:ui-docker-2", "route:ui-fallback"):
        cdp.eval_js(
            f"""
            document.querySelector('.btn-delete-route[data-route-id="{rid}"]').click();
            document.getElementById('delete-confirm-btn').click();
            """
        )
        cdp.wait_idle()

    final_routes = cdp.eval_js(
        "return Array.from(document.querySelectorAll('#routes-tbody tr')).map(tr => tr.dataset.routeId);"
    )
    assert final_routes == ["route:app-a", "route:app-b"], final_routes

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

echo "==> [3/3] Verifying capture, durable SQLite index, retention chain, and Git hygiene (AC-13)"
for _ in $(seq 1 20); do
  captures_json="$(curl -fsS "${API_BASE}/api/v1/captures?limit=50")"
  if CAPTURES_JSON="${captures_json}" python3 - <<'PY'
import json, os
data = json.loads(os.environ["CAPTURES_JSON"])
hosts = {item["host"] for item in data.get("items", [])}
needed = {"ui-manual.local.test", "ui-docker-1.local.test", "ui-docker-2.local.test", "ui-fallback.local.test"}
raise SystemExit(0 if needed.issubset(hosts) else 1)
PY
  then
    break
  fi
  sleep 0.5
done

captures_json="$(curl -fsS "${API_BASE}/api/v1/captures?limit=50")"
retention_json="$(curl -fsS "${API_BASE}/api/v1/retention/status")"
CAPTURES_JSON="${captures_json}" RETENTION_JSON="${retention_json}" python3 - <<'PY'
import json, os
c_data = json.loads(os.environ["CAPTURES_JSON"])
r_data = json.loads(os.environ["RETENTION_JSON"])
hosts = {item["host"] for item in c_data.get("items", [])}
for expected_host in ("ui-manual.local.test", "ui-docker-1.local.test", "ui-docker-2.local.test", "ui-fallback.local.test"):
    assert expected_host in hosts, f"Expected capture for {expected_host}, got {hosts}"
assert "storage" in r_data and "policy" in r_data, r_data
PY

git_dirty="$(git -C "${ROOT_DIR}" status --porcelain -- var/ 2>/dev/null || true)"
if [[ -n "${git_dirty}" ]]; then
  echo "ERROR: Runtime files appeared in git status: ${git_dirty}" >&2
  exit 1
fi

echo "PASS: All DEV-022 Web Console & Docker target tests succeeded!"
