#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
COMPOSE_FILE="${ROOT_DIR}/deploy/compose/docker-compose.yml"
EDGE_HTTPS_PORT="${EDGE_HTTPS_PORT:-8443}"
CONTROL_API_PORT="${CONTROL_API_PORT:-18480}"
API_BASE="http://127.0.0.1:${CONTROL_API_PORT}"
CACERT="${ROOT_DIR}/var/certificates/edge.crt"

cleanup() {
  docker compose -f "${COMPOSE_FILE}" exec -T control-api python3 - <<'PY' >/dev/null 2>&1 || true
from index_store import CaptureIndexStore
store = CaptureIndexStore()
for cid in ("cap-ui-metadata-only", "cap-ui-capture-failed", "cap-ui-missing-blob"):
    p = store.captures_dir / f"{cid}.json"
    if p.exists():
        p.unlink()
store.rebuild_index()
PY
}
trap cleanup EXIT

echo "==> [1/10] Starting stack and seeding captures for Web Request Viewer / Editor / Replay Console E2E (AC-1, AC-2)"
"${ROOT_DIR}/scripts/down.sh" >/dev/null 2>&1 || true
rm -rf "${ROOT_DIR}/var/captures" "${ROOT_DIR}/var/blobs" "${ROOT_DIR}/var/database"
"${ROOT_DIR}/scripts/up.sh"

for _ in $(seq 1 30); do
  if curl -fsS "${API_BASE}/api/v1/health" >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

# 1a. Seed multiple GET requests across app-a and app-b for pagination & filter tests
for i in 1 2 3 4 5; do
  curl -fsS --cacert "${CACERT}" \
    --resolve "app-a.local.test:${EDGE_HTTPS_PORT}:127.0.0.1" \
    "https://app-a.local.test:${EDGE_HTTPS_PORT}/v1/page-seed/a-${i}?page=${i}" >/dev/null
done
for i in 1 2; do
  curl -fsS --cacert "${CACERT}" \
    --resolve "app-b.local.test:${EDGE_HTTPS_PORT}:127.0.0.1" \
    "https://app-b.local.test:${EDGE_HTTPS_PORT}/v1/page-seed/b-${i}?page=${i}" >/dev/null
done

# 1b. Seed URL-encoded form POST with duplicate keys
curl -fsS --cacert "${CACERT}" \
  --resolve "app-a.local.test:${EDGE_HTTPS_PORT}:127.0.0.1" \
  -X POST "https://app-a.local.test:${EDGE_HTTPS_PORT}/v1/forms/enroll?source=web" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -H "Cookie: form_sid=f001" \
  --data-raw "role=user&role=editor&dept=engineering" >/dev/null

# 1c. Seed JSON POST with duplicate query params, duplicate headers, cookies, and nested JSON
curl -fsS --cacert "${CACERT}" \
  --resolve "app-a.local.test:${EDGE_HTTPS_PORT}:127.0.0.1" \
  -X POST "https://app-a.local.test:${EDGE_HTTPS_PORT}/v1/orders?tag=alpha&tag=beta&tag=gamma&jp=%E6%97%A5%E6%9C%AC%E8%AA%9E" \
  -H "Content-Type: application/json; charset=utf-8" \
  -H "X-Multi: one" \
  -H "X-Multi: two" \
  -H "X-Multi: three" \
  -H "Cookie: sid=orig123; theme=light; lang=ja" \
  --data-binary '{"user":{"name":"花子","profile":{"role":"viewer","active":true}},"items":[{"sku":"A1","qty":1},{"sku":"B2","qty":2}],"audit":{"note":"initial"}}' >/dev/null

# 1d. Seed a truncated capture (> 1 MiB body) and synthetic metadata-only, capture-failed, missing-blob records
python3 - <<'PY' "${CACERT}" "${EDGE_HTTPS_PORT}"
import subprocess, sys
cacert, port = sys.argv[1], sys.argv[2]
big_payload = b"X" * (1024 * 1024 + 8192)
subprocess.run(
    [
        "curl", "-fsS", "--cacert", cacert,
        "--resolve", f"app-a.local.test:{port}:127.0.0.1",
        "-X", "POST",
        f"https://app-a.local.test:{port}/v1/large-truncated",
        "-H", "Content-Type: text/plain",
        "--data-binary", "@-",
    ],
    input=big_payload,
    stdout=subprocess.DEVNULL,
    check=True,
)
PY

"${ROOT_DIR}/scripts/capture-ctl.sh" sync >/dev/null

json_cap_id="$(curl -fsS "${API_BASE}/api/v1/captures?path=/v1/orders&limit=1" | python3 -c 'import sys,json; print(json.load(sys.stdin)["items"][0]["capture_id"])')"
form_cap_id="$(curl -fsS "${API_BASE}/api/v1/captures?path=/v1/forms/enroll&limit=1" | python3 -c 'import sys,json; print(json.load(sys.stdin)["items"][0]["capture_id"])')"
trunc_cap_id="$(curl -fsS "${API_BASE}/api/v1/captures?path=/v1/large-truncated&limit=1" | python3 -c 'import sys,json; print(json.load(sys.stdin)["items"][0]["capture_id"])')"

# Record original SHA-256 of JSON capture record & blob to verify immutability after UI draft edits
orig_hashes="$(docker compose -f "${COMPOSE_FILE}" exec -T control-api python3 -c "
import hashlib, pathlib
p = pathlib.Path('/var/lib/proxyconsole')
j_rec = (p / 'captures' / '${json_cap_id}.json').read_bytes()
j_blob = (p / 'blobs' / '${json_cap_id}.bin').read_bytes()
print(hashlib.sha256(j_rec).hexdigest() + ':' + hashlib.sha256(j_blob).hexdigest())
")"

# Inject synthetic metadata-only, capture-failed, and missing-blob records for AC-9 UI verification
docker compose -f "${COMPOSE_FILE}" exec -T control-api python3 - <<PY
import json
from index_store import CaptureIndexStore

store = CaptureIndexStore()
base = json.loads((store.captures_dir / "${json_cap_id}.json").read_text(encoding="utf-8"))

meta_rec = json.loads(json.dumps(base))
meta_rec["capture_id"] = "cap-ui-metadata-only"
meta_rec["captured_at"] = "2099-01-01T00:00:03+00:00"
meta_rec["capture_status"] = "metadata-only"
meta_rec["replayable"] = False
meta_rec["request"]["raw_target"] = "/v1/ui-non-replayable-meta"
meta_rec["request"]["raw_path"] = "/v1/ui-non-replayable-meta"
meta_rec["request"]["body"]["blob_ref"] = None
(store.captures_dir / "cap-ui-metadata-only.json").write_text(json.dumps(meta_rec), encoding="utf-8")
store.index_record_dict(meta_rec, "captures/cap-ui-metadata-only.json")

fail_rec = json.loads(json.dumps(base))
fail_rec["capture_id"] = "cap-ui-capture-failed"
fail_rec["captured_at"] = "2099-01-01T00:00:02+00:00"
fail_rec["capture_status"] = "capture-failed"
fail_rec["replayable"] = False
fail_rec["request"]["raw_target"] = "/v1/ui-non-replayable-failed"
fail_rec["request"]["raw_path"] = "/v1/ui-non-replayable-failed"
fail_rec["request"]["body"]["blob_ref"] = None
(store.captures_dir / "cap-ui-capture-failed.json").write_text(json.dumps(fail_rec), encoding="utf-8")
store.index_record_dict(fail_rec, "captures/cap-ui-capture-failed.json")

miss_rec = json.loads(json.dumps(base))
miss_rec["capture_id"] = "cap-ui-missing-blob"
miss_rec["captured_at"] = "2099-01-01T00:00:01+00:00"
miss_rec["capture_status"] = "complete"
miss_rec["replayable"] = True
miss_rec["request"]["raw_target"] = "/v1/ui-non-replayable-missing-blob"
miss_rec["request"]["raw_path"] = "/v1/ui-non-replayable-missing-blob"
miss_rec["request"]["body"]["blob_ref"] = "blobs/cap-ui-missing-blob.bin"
(store.captures_dir / "cap-ui-missing-blob.json").write_text(json.dumps(miss_rec), encoding="utf-8")
store.index_record_dict(miss_rec, "captures/cap-ui-missing-blob.json")
PY

echo "==> [2/10] Launching headless browser and driving Web Request Viewer / Editor / Replay Console E2E (AC-2..AC-14, Rules 1..10)"
ROOT_DIR="${ROOT_DIR}" \
COMPOSE_FILE="${COMPOSE_FILE}" \
API_BASE="${API_BASE}" \
EDGE_HTTPS_PORT="${EDGE_HTTPS_PORT}" \
CACERT="${CACERT}" \
JSON_CAP_ID="${json_cap_id}" \
FORM_CAP_ID="${form_cap_id}" \
TRUNC_CAP_ID="${trunc_cap_id}" \
ORIG_HASHES="${orig_hashes}" \
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
JSON_CAP_ID = os.environ["JSON_CAP_ID"]
FORM_CAP_ID = os.environ["FORM_CAP_ID"]
TRUNC_CAP_ID = os.environ["TRUNC_CAP_ID"]
ORIG_HASHES = os.environ["ORIG_HASHES"]


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


browser_bin = find_browser_binary()
with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
    s.bind(("127.0.0.1", 0))
    debug_port = s.getsockname()[1]

user_data_dir = tempfile.mkdtemp(prefix="pc_req_chrome_")
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

    # --- STEP 3: Switch to Requests & Replay view, verify newest-first list, pagination & filtering (AC-2, Rule 1) ---
    print("    -> [3/10] Verifying Requests list newest-first order, pagination, and filtering in browser (AC-2, Rule 1)")
    cdp.eval_js("document.getElementById('view-requests-btn').click();")
    list_state = cdp.eval_js(
        """
        const rows = Array.from(document.querySelectorAll('#captures-tbody tr')).map(tr => ({
          id: tr.dataset.captureId,
          method: tr.dataset.method,
          status: tr.dataset.captureStatus,
          replayable: tr.dataset.replayable,
          reason: tr.dataset.replayReason,
        }));
        const pageInfo = document.getElementById('captures-page-info');
        return {
          rows,
          total: Number(pageInfo.dataset.total),
          offset: Number(pageInfo.dataset.offset),
          routesHidden: document.getElementById('routes-workspace').hidden,
          requestsHidden: document.getElementById('requests-workspace').hidden,
        };
        """
    )
    assert list_state["routesHidden"] is True and list_state["requestsHidden"] is False
    assert list_state["total"] >= 12, list_state
    assert len(list_state["rows"]) == 10, list_state["rows"]
    # Synthetic records have timestamps in 2099 so newest-first places them at top
    assert list_state["rows"][0]["id"] == "cap-ui-metadata-only", list_state["rows"][0]

    # Pagination test: set page size = 5, apply, go to next page
    cdp.eval_js(
        """
        const sel = document.getElementById('filter-limit-select');
        sel.value = '5';
        document.getElementById('captures-filter-submit-btn').click();
        """
    )
    cdp.wait_idle()
    page1_ids = cdp.eval_js(
        "return Array.from(document.querySelectorAll('#captures-tbody tr')).map(tr => tr.dataset.captureId);"
    )
    assert len(page1_ids) == 5, page1_ids

    cdp.eval_js("document.getElementById('captures-next-page-btn').click();")
    cdp.wait_idle()
    page2_ids = cdp.eval_js(
        "return Array.from(document.querySelectorAll('#captures-tbody tr')).map(tr => tr.dataset.captureId);"
    )
    assert len(page2_ids) == 5, page2_ids
    assert set(page1_ids).isdisjoint(set(page2_ids)), (page1_ids, page2_ids)

    # Filter test: filter by method=POST and path=/v1/orders
    cdp.eval_js(
        """
        document.getElementById('filter-method-select').value = 'POST';
        document.getElementById('filter-path-input').value = '/v1/orders';
        document.getElementById('captures-filter-submit-btn').click();
        """
    )
    cdp.wait_idle()
    filtered_ids = cdp.eval_js(
        "return Array.from(document.querySelectorAll('#captures-tbody tr')).map(tr => tr.dataset.captureId);"
    )
    assert filtered_ids == [JSON_CAP_ID], (filtered_ids, JSON_CAP_ID)

    # Reset filters
    cdp.eval_js("document.getElementById('captures-filter-reset-btn').click();")
    cdp.wait_idle()

    # --- STEP 4: Verify non-replayable records are disabled with explicit reasons in list & detail (AC-9, Rule 6) ---
    print("    -> [4/10] Verifying non-replayable records (truncated, metadata-only, capture-failed, missing_blob) in UI (AC-9, Rule 6)")
    non_replayable_cases = [
        (TRUNC_CAP_ID, "truncated"),
        ("cap-ui-metadata-only", "metadata-only"),
        ("cap-ui-capture-failed", "capture-failed"),
        ("cap-ui-missing-blob", "missing_blob"),
    ]
    for cid, expected_reason in non_replayable_cases:
        cdp.eval_js(
            f"""
            const btn = document.querySelector('.btn-view-capture[data-capture-id="{cid}"]');
            if (!btn) throw new Error('Row not found for {cid}');
            btn.click();
            """
        )
        cdp.wait_idle()
        nr_state = cdp.eval_js(
            f"""
            const rowDraftBtn = document.querySelector('.btn-create-draft[data-capture-id="{cid}"]');
            const box = document.getElementById('detail-replayability-box');
            const detailDraftBtn = document.getElementById('detail-create-draft-btn');
            return {{
              rowBtnDisabled: rowDraftBtn?.disabled,
              boxReplayable: box?.dataset.replayable,
              boxReason: box?.dataset.reason,
              reasonText: document.getElementById('detail-replayability-reason')?.textContent,
              msgText: document.getElementById('detail-replayability-message')?.textContent,
              detailBtnDisabled: detailDraftBtn?.disabled,
              fbState: document.getElementById('feedback-banner')?.dataset.state,
            }};
            """
        )
        assert nr_state["rowBtnDisabled"] is True, (cid, nr_state)
        assert nr_state["detailBtnDisabled"] is True, (cid, nr_state)
        assert nr_state["boxReplayable"] == "false", (cid, nr_state)
        assert nr_state["boxReason"] == expected_reason, (cid, expected_reason, nr_state)
        assert nr_state["reasonText"] == expected_reason, (cid, expected_reason, nr_state)
        assert nr_state["fbState"] == "error", (cid, nr_state)

    # Remove the 3 synthetic records so the table only has real captures for the remaining steps
    subprocess.run(
        [
            "docker", "compose", "-f", COMPOSE_FILE, "exec", "-T", "control-api",
            "python3", "-c",
            "from index_store import CaptureIndexStore; s = CaptureIndexStore();\n"
            "for c in ('cap-ui-metadata-only','cap-ui-capture-failed','cap-ui-missing-blob'):\n"
            "  p = s.captures_dir / f'{c}.json'\n"
            "  if p.exists(): p.unlink()\n"
            "s.rebuild_index()",
        ],
        check=True,
    )
    cdp.eval_js("document.getElementById('refresh-captures-btn').click();")
    cdp.wait_idle()

    # --- STEP 5: Inspect JSON capture detail, verify ordered duplicate query/headers/cookies & decoded view (AC-3, AC-4, Rule 2, Rule 3, Rule 4) ---
    print("    -> [5/10] Verifying Request Detail ordered duplicate query/headers/cookies and decoded vs wire view (AC-3, AC-4, Rule 2..4)")
    cdp.eval_js(
        f"""
        document.querySelector('.btn-view-capture[data-capture-id="{JSON_CAP_ID}"]').click();
        """
    )
    cdp.wait_idle()
    detail_info = cdp.eval_js(
        """
        const qPairs = Array.from(document.querySelectorAll('#detail-query-tbody tr')).map(tr => [
          tr.querySelector('td:nth-child(2) code')?.textContent,
          tr.querySelector('td:nth-child(3) code')?.textContent,
        ]);
        const hPairs = Array.from(document.querySelectorAll('#detail-headers-tbody tr')).map(tr => [
          tr.querySelector('td:nth-child(2) code')?.textContent,
          tr.querySelector('td:nth-child(3) code')?.textContent,
        ]);
        const cPairs = Array.from(document.querySelectorAll('#detail-cookies-tbody tr')).map(tr => [
          tr.querySelector('td:nth-child(2) code')?.textContent,
          tr.querySelector('td:nth-child(3) code')?.textContent,
        ]);
        return {
          captureId: document.getElementById('detail-capture-id').textContent,
          sha256: document.getElementById('detail-body-sha256').textContent,
          decodedPath: document.getElementById('detail-decoded-path').textContent,
          rawQuery: document.getElementById('detail-raw-query').textContent,
          qPairs,
          hPairs,
          cPairs,
          jsonBody: JSON.parse(document.getElementById('detail-derived-json').textContent),
        };
        """
    )
    assert detail_info["captureId"] == JSON_CAP_ID
    assert detail_info["qPairs"] == [
        ["tag", "alpha"],
        ["tag", "beta"],
        ["tag", "gamma"],
        ["jp", "日本語"],
    ], detail_info["qPairs"]
    x_multi_detail = [v for k, v in detail_info["hPairs"] if k and k.lower() == "x-multi"]
    assert x_multi_detail == ["one", "two", "three"], x_multi_detail
    assert detail_info["cPairs"] == [
        ["sid", "orig123"],
        ["theme", "light"],
        ["lang", "ja"],
    ], detail_info["cPairs"]
    assert detail_info["jsonBody"]["user"]["profile"]["role"] == "viewer"
    orig_detail_sha = detail_info["sha256"]

    # --- STEP 6: Create Draft in UI, edit path, duplicate query/headers, cookies, nested JSON, save & reload, verify immutability (AC-3..AC-6, Rules 3..5, Rule 8) ---
    print("    -> [6/10] Creating draft in UI, editing query/headers/cookies/path/nested JSON, saving & reloading, verifying immutability (AC-3..AC-6)")
    cdp.eval_js("document.getElementById('detail-create-draft-btn').click();")
    cdp.wait_idle()

    draft_id = cdp.eval_js("return document.getElementById('draft-id-badge').textContent;")
    assert draft_id.startswith("draft-"), draft_id

    # Edit decoded path, query pairs, header pairs, cookie pairs in the browser DOM
    cdp.eval_js(
        """
        const setInput = (el, val) => {
          el.value = val;
          el.dispatchEvent(new Event('input', { bubbles: true }));
          el.dispatchEvent(new Event('change', { bubbles: true }));
        };

        // 1. Human-readable decoded path with Unicode & space
        setInput(document.getElementById('draft-decoded-path-input'), '/v1/日本語 パス/orders');

        // 2. Query pairs: edit second 'tag' (#2) to 'beta-ui', remove first 'tag' (#1), add 'expr' and 'space key'
        const qRows = Array.from(document.querySelectorAll('#draft-query-rows .pair-row'));
        setInput(qRows[1].querySelector('.pair-val-input'), 'beta-ui');
        setInput(qRows[3].querySelector('.pair-val-input'), '東京 & 大阪');
        qRows[0].querySelector('.btn-remove-pair').click();

        document.getElementById('add-query-pair-btn').click();
        let curQ = Array.from(document.querySelectorAll('#draft-query-rows .pair-row'));
        setInput(curQ[curQ.length - 1].querySelector('.pair-key-input'), 'expr');
        setInput(curQ[curQ.length - 1].querySelector('.pair-val-input'), 'a+b&c=d?e/f#g');

        document.getElementById('add-query-pair-btn').click();
        curQ = Array.from(document.querySelectorAll('#draft-query-rows .pair-row'));
        setInput(curQ[curQ.length - 1].querySelector('.pair-key-input'), 'space key');
        setInput(curQ[curQ.length - 1].querySelector('.pair-val-input'), 'hello world');

        // 3. Header pairs: edit second X-Multi, remove first X-Multi, add fourth X-Multi and hop-by-hop Connection + X-Custom-Hop
        const hRows = Array.from(document.querySelectorAll('#draft-header-rows .pair-row'));
        const xmIndices = [];
        hRows.forEach((r, idx) => {
          if (r.querySelector('.pair-key-input').value.toLowerCase() === 'x-multi') {
            xmIndices.push(idx);
          }
        });
        setInput(hRows[xmIndices[1]].querySelector('.pair-val-input'), 'two-ui');
        hRows[xmIndices[0]].querySelector('.btn-remove-pair').click();

        document.getElementById('add-header-pair-btn').click();
        let curH = Array.from(document.querySelectorAll('#draft-header-rows .pair-row'));
        setInput(curH[curH.length - 1].querySelector('.pair-key-input'), 'X-Multi');
        setInput(curH[curH.length - 1].querySelector('.pair-val-input'), 'four-ui');

        document.getElementById('add-header-pair-btn').click();
        curH = Array.from(document.querySelectorAll('#draft-header-rows .pair-row'));
        setInput(curH[curH.length - 1].querySelector('.pair-key-input'), 'Connection');
        setInput(curH[curH.length - 1].querySelector('.pair-val-input'), 'keep-alive, X-Custom-Hop');

        document.getElementById('add-header-pair-btn').click();
        curH = Array.from(document.querySelectorAll('#draft-header-rows .pair-row'));
        setInput(curH[curH.length - 1].querySelector('.pair-key-input'), 'X-Custom-Hop');
        setInput(curH[curH.length - 1].querySelector('.pair-val-input'), 'must-be-stripped');

        // 4. Cookie pairs: update 'theme' -> 'dark', remove 'lang', add 'trace_id' -> 'ui-999'
        const cRows = Array.from(document.querySelectorAll('#draft-cookie-rows .pair-row'));
        setInput(cRows[1].querySelector('.pair-val-input'), 'dark');
        cRows[2].querySelector('.btn-remove-pair').click();

        document.getElementById('add-cookie-pair-btn').click();
        const curC = Array.from(document.querySelectorAll('#draft-cookie-rows .pair-row'));
        setInput(curC[curC.length - 1].querySelector('.pair-key-input'), 'trace_id');
        setInput(curC[curC.length - 1].querySelector('.pair-val-input'), 'ui-999');
        """
    )

    # Use Nested JSON Path editor in UI to set user.profile.role = "superadmin" and items.1.qty = 77
    cdp.eval_js(
        """
        document.getElementById('draft-json-path-input').value = 'user.profile.role';
        document.getElementById('draft-json-path-val-input').value = '"superadmin"';
        document.getElementById('draft-json-path-set-btn').click();
        """
    )
    cdp.wait_idle()

    cdp.eval_js(
        """
        document.getElementById('draft-json-path-input').value = 'items.1.qty';
        document.getElementById('draft-json-path-val-input').value = '77';
        document.getElementById('draft-json-path-set-btn').click();
        """
    )
    cdp.wait_idle()

    # Click Save Draft and then Reload Draft to verify round-trip persistence
    cdp.eval_js("document.getElementById('draft-save-btn').click();")
    cdp.wait_idle()
    cdp.eval_js("document.getElementById('draft-reload-btn').click();")
    cdp.wait_idle()

    saved_ui = cdp.eval_js(
        """
        return {
          dirty: document.getElementById('draft-sync-badge').dataset.dirty,
          decodedPath: document.getElementById('draft-decoded-path-input').value,
          wirePath: document.getElementById('draft-wire-path').textContent,
          wireQuery: document.getElementById('draft-wire-query').textContent,
          wireContentLen: Number(document.getElementById('draft-wire-content-length').textContent),
          excludedHeaders: document.getElementById('draft-wire-excluded-headers').textContent.toLowerCase(),
          jsonVal: JSON.parse(document.getElementById('draft-json-textarea').value),
        };
        """
    )
    assert saved_ui["dirty"] == "false", saved_ui
    assert saved_ui["decodedPath"] == "/v1/日本語 パス/orders", saved_ui
    assert saved_ui["wirePath"] == "/v1/%E6%97%A5%E6%9C%AC%E8%AA%9E%20%E3%83%91%E3%82%B9/orders", saved_ui
    assert "connection" in saved_ui["excludedHeaders"] and "x-custom-hop" in saved_ui["excludedHeaders"], saved_ui["excludedHeaders"]
    assert saved_ui["jsonVal"]["user"]["profile"]["role"] == "superadmin", saved_ui["jsonVal"]
    assert saved_ui["jsonVal"]["items"][1]["qty"] == 77, saved_ui["jsonVal"]

    # Verify original capture detail in UI & on disk is completely unchanged (AC-6, Rule 5)
    cdp.eval_js("document.getElementById('draft-view-source-btn').click();")
    cdp.wait_idle()
    after_edit_detail = cdp.eval_js(
        """
        return {
          sha256: document.getElementById('detail-body-sha256').textContent,
          rawPath: document.getElementById('detail-raw-path').textContent,
          jsonBody: JSON.parse(document.getElementById('detail-derived-json').textContent),
        };
        """
    )
    assert after_edit_detail["sha256"] == orig_detail_sha
    assert after_edit_detail["rawPath"] == "/v1/orders"
    assert after_edit_detail["jsonBody"]["user"]["profile"]["role"] == "viewer"

    # --- STEP 7: Unsafe Method Replay Confirmation (Cancel vs Confirm) & Lineage Navigation (AC-7, AC-8, Rule 7, Rule 9) ---
    print("    -> [7/10] Verifying unsafe method replay confirmation dialog (cancel vs confirm) and lineage navigation (AC-7, AC-8, Rule 7, Rule 9)")
    cdp.eval_js("document.getElementById('draft-replay-btn').click();")
    modal_state = cdp.eval_js(
        """
        const dlg = document.getElementById('replay-confirm-dialog');
        return {
          open: dlg.open,
          method: document.getElementById('replay-confirm-method').textContent,
          url: document.getElementById('replay-confirm-url').textContent,
          warning: document.getElementById('replay-confirm-warning').textContent,
          replayBoxHidden: document.getElementById('replay-result-box').hidden,
        };
        """
    )
    assert modal_state["open"] is True, modal_state
    assert modal_state["method"] == "POST", modal_state
    assert "/v1/%E6%97%A5%E6%9C%AC%E8%AA%9E%20%E3%83%91%E3%82%B9/orders" in modal_state["url"], modal_state
    assert modal_state["replayBoxHidden"] is True, "Replay must NOT run before confirmation!"

    # Click Cancel first -> must close modal and NOT send replay
    cdp.eval_js("document.getElementById('replay-confirm-cancel-btn').click();")
    after_cancel = cdp.eval_js(
        """
        return {
          open: document.getElementById('replay-confirm-dialog').open,
          replayBoxHidden: document.getElementById('replay-result-box').hidden,
        };
        """
    )
    assert after_cancel["open"] is False
    assert after_cancel["replayBoxHidden"] is True

    # Now click Replay again and Confirm in modal dialog
    cdp.eval_js("document.getElementById('draft-replay-btn').click();")
    cdp.eval_js("document.getElementById('replay-confirm-send-btn').click();")
    cdp.wait_idle()

    rpl_ui = cdp.eval_js(
        """
        const box = document.getElementById('replay-result-box');
        return {
          hidden: box.hidden,
          status: box.dataset.status,
          newCaptureId: document.getElementById('replay-result-capture-id').textContent,
          lineageText: document.getElementById('replay-result-lineage').textContent,
          echoBody: JSON.parse(document.getElementById('replay-result-body').textContent),
        };
        """
    )
    assert rpl_ui["hidden"] is False, rpl_ui
    assert rpl_ui["status"] == "200", rpl_ui
    assert rpl_ui["newCaptureId"].startswith("cap-"), rpl_ui
    assert JSON_CAP_ID in rpl_ui["lineageText"] and draft_id in rpl_ui["lineageText"], rpl_ui["lineageText"]

    echo = rpl_ui["echoBody"]
    assert echo["target"] == "echo-a", echo
    assert echo["path"] == "/v1/%E6%97%A5%E6%9C%AC%E8%AA%9E%20%E3%83%91%E3%82%B9/orders", echo["path"]
    assert echo["query_params"] == [
        ["tag", "beta-ui"],
        ["tag", "gamma"],
        ["jp", "東京 & 大阪"],
        ["expr", "a+b&c=d?e/f#g"],
        ["space key", "hello world"],
    ], echo["query_params"]
    echo_xm = [v for k, v in echo["headers"] if k.lower() == "x-multi"]
    assert echo_xm == ["two-ui", "three", "four-ui"], echo_xm
    echo_hop = [v for k, v in echo["headers"] if k.lower() == "x-custom-hop"]
    assert echo_hop == [], echo_hop
    echo_cookie = [v for k, v in echo["headers"] if k.lower() == "cookie"]
    assert echo_cookie == ["sid=orig123; theme=dark; trace_id=ui-999"], echo_cookie
    assert echo["body_json"]["user"]["profile"]["role"] == "superadmin"
    assert echo["body_json"]["items"][1]["qty"] == 77
    assert echo["body_size"] == saved_ui["wireContentLen"]

    # Click 'Inspect New Capture & Lineage' button to navigate to the new replay capture detail
    cdp.eval_js("document.getElementById('replay-view-new-capture-btn').click();")
    cdp.wait_idle()
    child_lineage = cdp.eval_js(
        """
        return {
          detailId: document.getElementById('detail-capture-id').textContent,
          parentHidden: document.getElementById('detail-parent-lineage').hidden,
          parentSrcId: document.getElementById('detail-nav-source-capture-btn').textContent,
          parentDraftId: document.getElementById('detail-nav-source-draft-btn').textContent,
        };
        """
    )
    assert child_lineage["detailId"] == rpl_ui["newCaptureId"], child_lineage
    assert child_lineage["parentHidden"] is False, child_lineage
    assert child_lineage["parentSrcId"] == JSON_CAP_ID, child_lineage
    assert child_lineage["parentDraftId"] == draft_id, child_lineage

    # Navigate back to parent source capture via the lineage button
    cdp.eval_js("document.getElementById('detail-nav-source-capture-btn').click();")
    cdp.wait_idle()
    parent_replays = cdp.eval_js(
        """
        return Array.from(document.querySelectorAll('#detail-child-replays-list .btn-open-child-replay')).map(b => b.dataset.resultCaptureId);
        """
    )
    assert rpl_ui["newCaptureId"] in parent_replays, parent_replays

    # --- STEP 8: cURL & fetch() Export Preview, Copy/Select, and Live Traffic Execution (AC-10, AC-14, Rule 8, Rule 10) ---
    print("    -> [8/10] Verifying cURL & fetch() preview in UI, clipboard/selection, and live export traffic capture (AC-10, AC-14)")
    cdp.eval_js("document.getElementById('copy-curl-btn').click();")
    cdp.wait_idle()
    cdp.eval_js("document.getElementById('copy-fetch-btn').click();")
    cdp.wait_idle()
    exp_ui = cdp.eval_js(
        """
        return {
          curl: document.getElementById('export-curl-text').value,
          curlEdge: document.getElementById('export-curl-edge-text').value,
          fetchJs: document.getElementById('export-fetch-text').value,
          copyStatus: document.getElementById('export-copy-status').textContent,
        };
        """
    )
    assert "superadmin" in exp_ui["curl"] and "viewer" not in exp_ui["curl"], exp_ui["curl"]
    assert "superadmin" in exp_ui["fetchJs"] and "viewer" not in exp_ui["fetchJs"], exp_ui["fetchJs"]
    assert ("Copied" in exp_ui["copyStatus"]) or ("Selected" in exp_ui["copyStatus"]), exp_ui["copyStatus"]

    # Execute the UI-exported Local Edge cURL command and verify it goes through the capture/index chain!
    curl_proc = subprocess.run(
        exp_ui["curlEdge"],
        shell=True,
        check=True,
        capture_output=True,
        text=True,
    )
    curl_echo = json.loads(curl_proc.stdout)
    assert curl_echo["target"] == "echo-a"
    assert curl_echo["body_json"]["user"]["profile"]["role"] == "superadmin"

    # --- STEP 9: URL-Encoded Form Duplicate Key Editing & Replay in UI (AC-5) ---
    print("    -> [9/10] Verifying URL-encoded form duplicate key editing and replay in browser (AC-5)")
    cdp.eval_js(
        f"""
        document.querySelector('.btn-view-capture[data-capture-id="{FORM_CAP_ID}"]').click();
        """
    )
    cdp.wait_idle()
    cdp.eval_js("document.getElementById('detail-create-draft-btn').click();")
    cdp.wait_idle()

    cdp.eval_js(
        """
        const setInput = (el, val) => {
          el.value = val;
          el.dispatchEvent(new Event('input', { bubbles: true }));
          el.dispatchEvent(new Event('change', { bubbles: true }));
        };
        const fRows = Array.from(document.querySelectorAll('#draft-form-rows .pair-row'));
        // Edit second 'role' from 'editor' to 'owner'
        setInput(fRows[1].querySelector('.pair-val-input'), 'owner');
        // Add third 'role' = 'auditor'
        document.getElementById('add-form-pair-btn').click();
        const curF = Array.from(document.querySelectorAll('#draft-form-rows .pair-row'));
        setInput(curF[curF.length - 1].querySelector('.pair-key-input'), 'role');
        setInput(curF[curF.length - 1].querySelector('.pair-val-input'), 'auditor');
        """
    )
    cdp.eval_js("document.getElementById('draft-replay-btn').click();")
    cdp.wait_idle()
    cdp.eval_js("document.getElementById('replay-confirm-send-btn').click();")
    cdp.wait_idle()

    form_rpl = cdp.eval_js(
        "return JSON.parse(document.getElementById('replay-result-body').textContent);"
    )
    assert form_rpl["form_params"] == [
        ["role", "user"],
        ["role", "owner"],
        ["dept", "engineering"],
        ["role", "auditor"],
    ], form_rpl["form_params"]

    # --- STEP 10: Validation Error, Page Reload + Container Restart Persistence, and Deleted/Expired Draft Recovery (AC-11, AC-12) ---
    print("    -> [10/10] Verifying validation error, page reload & container restart persistence, and deleted/expired draft recovery (AC-11, AC-12)")
    # Load the JSON draft again from Saved Drafts bar
    cdp.eval_js(
        f"""
        document.querySelector('.btn-load-saved-draft[data-draft-id="{draft_id}"]').click();
        """
    )
    cdp.wait_idle()

    # Enter invalid JSON and attempt Save -> must show validation error and NOT overwrite server state
    cdp.eval_js(
        """
        const ta = document.getElementById('draft-json-textarea');
        ta.value = '{broken_json: true';
        ta.dispatchEvent(new Event('input', { bubbles: true }));
        document.getElementById('draft-save-btn').click();
        """
    )
    cdp.wait_idle()
    val_err = cdp.eval_js(
        """
        const vbox = document.getElementById('draft-validation-error-box');
        const fb = document.getElementById('feedback-banner');
        return {
          hidden: vbox.hidden,
          code: vbox.dataset.errorCode,
          fbState: fb.dataset.state,
        };
        """
    )
    assert val_err["hidden"] is False, val_err
    assert val_err["code"] == "invalid_json_body", val_err
    assert val_err["fbState"] == "error", val_err

    # Recover from validation error via Reload Draft button
    cdp.eval_js("document.getElementById('draft-reload-btn').click();")
    cdp.wait_idle()

    # Restart control-api container and reload browser page to verify draft persistence (AC-12)
    subprocess.run(
        ["docker", "compose", "-f", COMPOSE_FILE, "restart", "control-api"],
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
    cdp.eval_js(
        f"""
        document.getElementById('view-requests-btn').click();
        document.querySelector('.btn-load-saved-draft[data-draft-id="{draft_id}"]').click();
        """
    )
    cdp.wait_idle()
    persisted_role = cdp.eval_js(
        "return JSON.parse(document.getElementById('draft-json-textarea').value).user.profile.role;"
    )
    assert persisted_role == "superadmin", persisted_role

    # Delete draft via UI button, then attempt to load it by ID -> must show recovery box, then recreate draft!
    cdp.eval_js("document.getElementById('draft-delete-btn').click();")
    cdp.wait_idle()

    cdp.eval_js(
        f"""
        document.getElementById('lookup-draft-id-input').value = '{draft_id}';
        document.getElementById('lookup-draft-btn').click();
        """
    )
    cdp.wait_idle()
    del_err = cdp.eval_js(
        """
        const rbox = document.getElementById('draft-error-recovery-box');
        const fb = document.getElementById('feedback-banner');
        return {
          hidden: rbox.hidden,
          code: rbox.dataset.errorCode,
          fbState: fb.dataset.state,
        };
        """
    )
    assert del_err["hidden"] is False, del_err
    assert del_err["code"] == "draft_not_found", del_err
    assert del_err["fbState"] == "error", del_err

    # Click 'Recreate Draft from Source Capture' recovery button
    cdp.eval_js("document.getElementById('draft-recover-recreate-btn').click();")
    cdp.wait_idle()
    recreated = cdp.eval_js(
        """
        return {
          errHidden: document.getElementById('draft-error-recovery-box').hidden,
          newDraftId: document.getElementById('draft-id-badge').textContent,
          fbState: document.getElementById('feedback-banner').dataset.state,
        };
        """
    )
    assert recreated["errHidden"] is True, recreated
    assert recreated["newDraftId"].startswith("draft-") and recreated["newDraftId"] != draft_id, recreated
    assert recreated["fbState"] == "success", recreated

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

# Verify original JSON capture file and blob on disk are still bit-for-bit identical (Rule 5, AC-6)
after_hashes="$(docker compose -f "${COMPOSE_FILE}" exec -T control-api python3 -c "
import hashlib, pathlib
p = pathlib.Path('/var/lib/proxyconsole')
j_rec = (p / 'captures' / '${json_cap_id}.json').read_bytes()
j_blob = (p / 'blobs' / '${json_cap_id}.bin').read_bytes()
print(hashlib.sha256(j_rec).hexdigest() + ':' + hashlib.sha256(j_blob).hexdigest())
")"
if [[ "${orig_hashes}" != "${after_hashes}" ]]; then
  echo "ERROR: Original capture record or blob was modified! Before=${orig_hashes}, After=${after_hashes}" >&2
  exit 1
fi

git_dirty="$(git -C "${ROOT_DIR}" status --porcelain -- var/ 2>/dev/null || true)"
if [[ -n "${git_dirty}" ]]; then
  echo "ERROR: Runtime files appeared in git status: ${git_dirty}" >&2
  exit 1
fi

echo "PASS: All DEV-031 Web Request Viewer / Editor / Replay Console E2E tests succeeded!"
