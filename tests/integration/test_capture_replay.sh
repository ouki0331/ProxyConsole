#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
COMPOSE_FILE="${ROOT_DIR}/deploy/compose/docker-compose.yml"
CERT_FILE="${ROOT_DIR}/var/certificates/edge.crt"

if [[ -f "${ROOT_DIR}/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "${ROOT_DIR}/.env"
  set +a
fi

export EDGE_HTTPS_PORT="${EDGE_HTTPS_PORT:-8443}"
export APP_A_HOST="${APP_A_HOST:-app-a.local.test}"
export APP_B_HOST="${APP_B_HOST:-app-b.local.test}"

echo "=================================================================="
echo "=== [DEV-002 1/8] Start stack & verify DEV-001 baseline        ==="
echo "=================================================================="
"${ROOT_DIR}/scripts/up.sh"

echo "=================================================================="
echo "=== [DEV-002 2/8] Capture GET, JSON, Form, Multipart, Binary   ==="
echo "=================================================================="
WORK_TMP="$(mktemp -d)"
trap 'rm -rf "${WORK_TMP}"' EXIT

python3 - "${WORK_TMP}" "${EDGE_HTTPS_PORT}" "${APP_A_HOST}" "${APP_B_HOST}" "${CERT_FILE}" <<'PY'
import hashlib
import http.client
import json
import os
from pathlib import Path
import socket
import ssl
import sys

work_tmp = Path(sys.argv[1])
edge_port = int(sys.argv[2])
app_a = sys.argv[3]
app_b = sys.argv[4]
cert_file = sys.argv[5]

def send_https_request(host_header: str, method: str, target_uri: str, headers: list[tuple[str, str]], body: bytes = b"") -> tuple[int, dict[str, str], bytes]:
    sni = host_header.split(":", 1)[0]
    raw_sock = socket.create_connection(("127.0.0.1", edge_port), timeout=30)
    ctx = ssl.create_default_context(cafile=cert_file)
    tls_sock = ctx.wrap_socket(raw_sock, server_hostname=sni)
    conn = http.client.HTTPConnection("127.0.0.1", edge_port, timeout=30)
    conn.sock = tls_sock
    conn.putrequest(method, target_uri, skip_host=True, skip_accept_encoding=True)
    conn.putheader("Host", host_header)
    for k, v in headers:
        conn.putheader(k, v)
    conn.putheader("Content-Length", str(len(body)))
    conn.putheader("Connection", "close")
    conn.endheaders(message_body=body if body else None)
    resp = conn.getresponse()
    resp_body = resp.read()
    resp_headers = {k.lower(): v for k, v in resp.getheaders()}
    status = resp.status
    conn.close()
    return status, resp_headers, resp_body

manifest = {}

# 1. GET with duplicate query parameters, percent-encoded path segment, and duplicate headers
get_uri = "/v1/search/%2Fencoded?tag=first&tag=second&empty=&filter=active"
status, rheaders, rbody = send_https_request(
    f"{app_a}:{edge_port}",
    "GET",
    get_uri,
    [
        ("X-Custom-Multi", "first-val"),
        ("Accept", "application/json"),
        ("X-Custom-Multi", "second-val"),
        ("Cookie", "sid=abc; theme=dark"),
    ],
)
assert status == 200, f"GET failed: {status}"
manifest["get"] = {
    "capture_id": rheaders["x-capture-id"],
    "sha256": hashlib.sha256(b"").hexdigest(),
    "size": 0,
}

# 2. JSON POST
json_bytes = json.dumps({"event": "OrderCreated", "count": 7, "tags": ["a", "b"]}, separators=(",", ":")).encode("utf-8")
status, rheaders, rbody = send_https_request(
    f"{app_a}:{edge_port}",
    "POST",
    "/v1/events?tag=first&tag=second",
    [("Content-Type", "application/json; charset=utf-8")],
    json_bytes,
)
assert status == 200, f"JSON POST failed: {status}"
manifest["json"] = {
    "capture_id": rheaders["x-capture-id"],
    "sha256": hashlib.sha256(json_bytes).hexdigest(),
    "size": len(json_bytes),
}

# 3. URL-encoded form POST
form_bytes = b"action=update&role=member&flag=one&flag=two"
status, rheaders, rbody = send_https_request(
    f"{app_b}:{edge_port}",
    "POST",
    "/v1/profile?source=form",
    [("Content-Type", "application/x-www-form-urlencoded")],
    form_bytes,
)
assert status == 200, f"Form POST failed: {status}"
manifest["form"] = {
    "capture_id": rheaders["x-capture-id"],
    "sha256": hashlib.sha256(form_bytes).hexdigest(),
    "size": len(form_bytes),
}

# 4. Multipart form POST (text field + binary file)
boundary = "----ProxyConsoleBoundary7MA4YWxkTrZu0gW"
file_part_bytes = b"\x00\xff\x80\xfe\x01\x02ProxyConsoleBinaryPart\r\n\x03"
multipart_bytes = (
    f"--{boundary}\r\n"
    f'Content-Disposition: form-data; name="title"\r\n\r\n'
    f"daily-report\r\n"
    f"--{boundary}\r\n"
    f'Content-Disposition: form-data; name="attachment"; filename="sample.bin"\r\n'
    f"Content-Type: application/octet-stream\r\n\r\n"
).encode("utf-8") + file_part_bytes + f"\r\n--{boundary}--\r\n".encode("utf-8")

status, rheaders, rbody = send_https_request(
    f"{app_a}:{edge_port}",
    "POST",
    "/v1/upload/multipart",
    [("Content-Type", f"multipart/form-data; boundary={boundary}")],
    multipart_bytes,
)
assert status == 200, f"Multipart POST failed: {status}"
manifest["multipart"] = {
    "capture_id": rheaders["x-capture-id"],
    "sha256": hashlib.sha256(multipart_bytes).hexdigest(),
    "size": len(multipart_bytes),
    "part_file_sha256": hashlib.sha256(file_part_bytes).hexdigest(),
    "part_file_size": len(file_part_bytes),
}

# 5. Raw binary body POST (non-UTF8 bytes 0..255 repeated twice = 512 bytes)
binary_bytes = bytes(range(256)) * 2
status, rheaders, rbody = send_https_request(
    f"{app_b}:{edge_port}",
    "POST",
    "/v1/upload/raw?format=bin",
    [("Content-Type", "application/octet-stream")],
    binary_bytes,
)
assert status == 200, f"Binary POST failed: {status}"
manifest["binary"] = {
    "capture_id": rheaders["x-capture-id"],
    "sha256": hashlib.sha256(binary_bytes).hexdigest(),
    "size": len(binary_bytes),
}

(work_tmp / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
print("PASS: Sent GET, JSON POST, Form POST, Multipart POST, and Binary POST.")
PY

echo "=================================================================="
echo "=== [DEV-002 3/8] Restart inspector & verify read-back         ==="
echo "=================================================================="
docker compose -f "${COMPOSE_FILE}" restart inspector

for _ in $(seq 1 30); do
  code="$(
    curl -s -o /dev/null -w "%{http_code}" \
      --cacert "${CERT_FILE}" \
      --resolve "${APP_A_HOST}:${EDGE_HTTPS_PORT}:127.0.0.1" \
      "https://${APP_A_HOST}:${EDGE_HTTPS_PORT}/ready-after-restart" || true
  )"
  if [[ "${code}" == "200" ]]; then
    break
  fi
  sleep 0.5
done

"${ROOT_DIR}/scripts/capture-ctl.sh" sync

python3 - "${WORK_TMP}" "${ROOT_DIR}" <<'PY'
import json
from pathlib import Path
import subprocess
import sys

work_tmp = Path(sys.argv[1])
root_dir = Path(sys.argv[2])
manifest = json.loads((work_tmp / "manifest.json").read_text(encoding="utf-8"))

def read_via_container(cid: str) -> dict:
    out = subprocess.check_output(
        [str(root_dir / "scripts/capture-ctl.sh"), "get", cid],
        text=True,
    )
    return json.loads(out)

def read_via_host_reader(cid: str) -> dict:
    env = dict(**os.environ, PYTHONPATH=str(root_dir / "services/recorder"))
    out = subprocess.check_output(
        [
            sys.executable,
            str(root_dir / "services/recorder/capture_cli.py"),
            "--storage-dir",
            str(root_dir / "var"),
            "get",
            cid,
        ],
        text=True,
        env=env,
    )
    return json.loads(out)

import os

for kind, expected in manifest.items():
    cid = expected["capture_id"]
    rec_container = read_via_container(cid)
    rec_host = read_via_host_reader(cid)

    for rec in (rec_container, rec_host):
        assert rec["schema_version"] == "v0"
        assert rec["capture_status"] == "complete"
        assert rec["replayable"] is True
        assert rec["request"]["body"]["size"] == expected["size"]
        assert rec["request"]["body"]["sha256"] == expected["sha256"]
        assert rec["reader_verification"]["captured_size_matches"] is True
        assert rec["reader_verification"]["full_body_sha256_matches"] is True

    if kind == "get":
        req = rec_host["request"]
        assert req["raw_path"] == "/v1/search/%2Fencoded", f"raw_path lost encoding: {req['raw_path']}"
        assert req["raw_query"] == "tag=first&tag=second&empty=&filter=active"
        views = rec_host["regenerated_derived_views"]
        assert views["query_params"] == [
            ["tag", "first"],
            ["tag", "second"],
            ["empty", ""],
            ["filter", "active"],
        ], f"Unexpected query_params: {views['query_params']}"
        custom_hdrs = [v for k, v in req["headers"] if k.lower() == "x-custom-multi"]
        assert custom_hdrs == ["first-val", "second-val"], f"Duplicate headers not preserved: {custom_hdrs}"
        assert views["cookies_view"] == [["sid", "abc"], ["theme", "dark"]]

    elif kind == "json":
        views = rec_host["regenerated_derived_views"]
        assert views["content_kind"] == "json"
        assert views["json_view"] == {"event": "OrderCreated", "count": 7, "tags": ["a", "b"]}
        assert views["query_params"] == [["tag", "first"], ["tag", "second"]]

    elif kind == "form":
        views = rec_host["regenerated_derived_views"]
        assert views["content_kind"] == "urlencoded_form"
        assert views["form_view"] == [
            ["action", "update"],
            ["role", "member"],
            ["flag", "one"],
            ["flag", "two"],
        ]

    elif kind == "multipart":
        views = rec_host["regenerated_derived_views"]
        assert views["content_kind"] == "multipart_form"
        parts = views["multipart_view"]
        assert len(parts) == 2
        assert parts[0]["name"] == "title"
        assert parts[0]["value_text"] == "daily-report"
        assert parts[1]["name"] == "attachment"
        assert parts[1]["filename"] == "sample.bin"
        assert parts[1]["size"] == expected["part_file_size"]
        assert parts[1]["sha256"] == expected["part_file_sha256"]

    elif kind == "binary":
        views = rec_host["regenerated_derived_views"]
        assert views["content_kind"] == "binary"
        assert len(views["hex_preview"]) == 128

print("PASS: All 5 record types persisted across inspector restart and passed SHA-256 & view regeneration checks.")
PY

echo "=================================================================="
echo "=== [DEV-002 4/8] Semantic replay (exact match)                ==="
echo "=================================================================="
python3 - "${WORK_TMP}" "${ROOT_DIR}" <<'PY'
import json
from pathlib import Path
import subprocess
import sys

work_tmp = Path(sys.argv[1])
root_dir = Path(sys.argv[2])
manifest = json.loads((work_tmp / "manifest.json").read_text(encoding="utf-8"))

for kind in ("json", "binary"):
    cid = manifest[kind]["capture_id"]
    out = subprocess.check_output(
        [str(root_dir / "scripts/capture-ctl.sh"), "replay", cid],
        text=True,
    )
    res = json.loads(out)
    assert res["response_status"] == 200, f"Replay status failed: {res}"
    echo = res["response_json"]
    assert echo["body_size"] == manifest[kind]["size"]
    assert echo["body_sha256"] == manifest[kind]["sha256"]
    if kind == "json":
        assert echo["target"] == "echo-a"
        assert echo["method"] == "POST"
        assert echo["path"] == "/v1/events"
        assert echo["raw_query"] == "tag=first&tag=second"
        assert echo["body_json"] == {"event": "OrderCreated", "count": 7, "tags": ["a", "b"]}
    elif kind == "binary":
        assert echo["target"] == "echo-b"
        assert echo["method"] == "POST"
        assert echo["path"] == "/v1/upload/raw"
        assert echo["raw_query"] == "format=bin"

print("PASS: Exact semantic replay verified for JSON POST and Binary POST.")
PY

echo "=================================================================="
echo "=== [DEV-002 5/8] Replay with single-field query/form mutation ==="
echo "=================================================================="
python3 - "${WORK_TMP}" "${ROOT_DIR}" <<'PY'
import json
from pathlib import Path
import subprocess
import sys

work_tmp = Path(sys.argv[1])
root_dir = Path(sys.argv[2])
manifest = json.loads((work_tmp / "manifest.json").read_text(encoding="utf-8"))

# Mutate single query item on GET record
get_cid = manifest["get"]["capture_id"]
out_q = subprocess.check_output(
    [
        str(root_dir / "scripts/capture-ctl.sh"),
        "replay",
        get_cid,
        "--set-query",
        "filter=archived",
    ],
    text=True,
)
res_q = json.loads(out_q)
assert res_q["response_status"] == 200
echo_q = res_q["response_json"]
assert echo_q["target"] == "echo-a"
assert echo_q["path"] == "/v1/search/%2Fencoded"
assert echo_q["query_params"] == [
    ["tag", "first"],
    ["tag", "second"],
    ["empty", ""],
    ["filter", "archived"],
], f"Unexpected mutated query_params: {echo_q['query_params']}"

# Mutate single form field on URL-encoded form record
form_cid = manifest["form"]["capture_id"]
out_f = subprocess.check_output(
    [
        str(root_dir / "scripts/capture-ctl.sh"),
        "replay",
        form_cid,
        "--set-form",
        "role=admin",
    ],
    text=True,
)
res_f = json.loads(out_f)
assert res_f["response_status"] == 200
echo_f = res_f["response_json"]
assert echo_f["target"] == "echo-b"
assert echo_f["path"] == "/v1/profile"
assert echo_f["raw_query"] == "source=form"
assert echo_f["form_params"] == [
    ["action", "update"],
    ["role", "admin"],
    ["flag", "one"],
    ["flag", "two"],
], f"Unexpected mutated form_params: {echo_f['form_params']}"

print("PASS: Single-item query mutation and URL-encoded form mutation verified.")
PY

echo "=================================================================="
echo "=== [DEV-002 6/8] 100 MiB upload pass-through & capture limit  ==="
echo "=================================================================="
python3 - "${WORK_TMP}" "${ROOT_DIR}" "${EDGE_HTTPS_PORT}" "${APP_A_HOST}" "${CERT_FILE}" <<'PY'
import hashlib
import json
from pathlib import Path
import subprocess
import sys

work_tmp = Path(sys.argv[1])
root_dir = Path(sys.argv[2])
edge_port = sys.argv[3]
app_a = sys.argv[4]
cert_file = sys.argv[5]

large_file = work_tmp / "large_100mib.bin"
chunk = (b"ProxyConsole100MiBStreamVerify-" + bytes(range(33))) * 1024  # 64 KiB
assert len(chunk) == 65536
total_chunks = 1600  # 1600 * 64 KiB = 100 MiB (104,857,600 bytes)
hasher = hashlib.sha256()
with large_file.open("wb") as f:
    for _ in range(total_chunks):
        f.write(chunk)
        hasher.update(chunk)

expected_size = 104857600
expected_sha256 = hasher.hexdigest()

hdr_file = work_tmp / "large_headers.txt"
body_file = work_tmp / "large_resp.json"

subprocess.run(
    [
        "curl",
        "-sS",
        "-D",
        str(hdr_file),
        "-o",
        str(body_file),
        "--cacert",
        cert_file,
        "--resolve",
        f"{app_a}:{edge_port}:127.0.0.1",
        "-X",
        "POST",
        "-H",
        "Content-Type: application/octet-stream",
        "-H",
        "Expect:",
        "--data-binary",
        f"@{large_file}",
        f"https://{app_a}:{edge_port}/v1/upload/100mib?mode=stream",
    ],
    check=True,
)

echo_resp = json.loads(body_file.read_text(encoding="utf-8"))
assert echo_resp["target"] == "echo-a"
assert echo_resp["body_size"] == expected_size, f"Expected {expected_size}, got {echo_resp['body_size']}"
assert echo_resp["body_sha256"] == expected_sha256, f"SHA-256 mismatch at target: {echo_resp['body_sha256']} != {expected_sha256}"

large_cid = None
for line in hdr_file.read_text(encoding="utf-8", errors="replace").splitlines():
    if line.lower().startswith("x-capture-id:"):
        large_cid = line.split(":", 1)[1].strip()

assert large_cid, "Missing X-Capture-Id header on 100 MiB response"

rec = json.loads(
    subprocess.check_output(
        [str(root_dir / "scripts/capture-ctl.sh"), "get", large_cid],
        text=True,
    )
)
assert rec["capture_status"] in ("truncated", "metadata-only"), f"Unexpected status: {rec['capture_status']}"
assert rec["replayable"] is False, "Oversized capture must be non-replayable"
assert rec["request"]["body"]["size"] == expected_size
assert rec["request"]["body"]["sha256"] == expected_sha256
assert rec["request"]["body"]["captured_size"] <= rec["request"]["body"]["max_capture_bytes"]

# Verify replay CLI refuses to replay truncated record
proc = subprocess.run(
    [str(root_dir / "scripts/capture-ctl.sh"), "replay", large_cid],
    capture_output=True,
    text=True,
)
assert proc.returncode != 0, "Replay of truncated 100 MiB capture must fail"
err_data = json.loads(proc.stderr.strip())
assert err_data["error"] == "non_replayable_capture"

large_file.unlink()
print(f"PASS: 100 MiB ({expected_size} bytes) upload reached target intact; capture marked {rec['capture_status']} (replayable=False) and replay rejected.")
PY

echo "=================================================================="
echo "=== [DEV-002 7/8] WebSocket pass-through & handshake metadata  ==="
echo "=================================================================="
python3 - "${ROOT_DIR}" "${EDGE_HTTPS_PORT}" "${APP_A_HOST}" "${CERT_FILE}" <<'PY'
import base64
import hashlib
import json
import os
from pathlib import Path
import socket
import ssl
import struct
import subprocess
import sys

root_dir = Path(sys.argv[1])
edge_port = int(sys.argv[2])
app_a = sys.argv[3]
cert_file = sys.argv[4]

ws_key = base64.b64encode(b"ProxyConsoleWS16").decode("ascii")
expected_accept = base64.b64encode(
    hashlib.sha1((ws_key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")).digest()
).decode("ascii")

raw_sock = socket.create_connection(("127.0.0.1", edge_port), timeout=15)
ctx = ssl.create_default_context(cafile=cert_file)
tls_sock = ctx.wrap_socket(raw_sock, server_hostname=app_a)

handshake_req = (
    f"GET /ws/live?room=alpha&mode=realtime HTTP/1.1\r\n"
    f"Host: {app_a}:{edge_port}\r\n"
    f"Upgrade: websocket\r\n"
    f"Connection: Upgrade\r\n"
    f"Sec-WebSocket-Key: {ws_key}\r\n"
    f"Sec-WebSocket-Version: 13\r\n"
    f"Sec-WebSocket-Protocol: chat.v1\r\n"
    f"\r\n"
).encode("ascii")
tls_sock.sendall(handshake_req)

resp_buf = b""
while b"\r\n\r\n" not in resp_buf:
    chunk = tls_sock.recv(4096)
    if not chunk:
        break
    resp_buf += chunk

header_part, leftover = resp_buf.split(b"\r\n\r\n", 1)
header_lines = header_part.decode("latin-1").split("\r\n")
status_line = header_lines[0]
assert " 101 " in status_line, f"Expected 101 Switching Protocols, got: {status_line}"

resp_headers = {}
for line in header_lines[1:]:
    if ":" in line:
        k, v = line.split(":", 1)
        resp_headers[k.strip().lower()] = v.strip()

assert resp_headers.get("sec-websocket-accept") == expected_accept
ws_capture_id = resp_headers.get("x-capture-id")
assert ws_capture_id, f"Expected X-Capture-Id on 101 response, got: {resp_headers}"

# Send masked text frame "hello-websocket"
payload = b"hello-websocket"
mask_key = b"\x12\x34\x56\x78"
masked_payload = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))
frame = bytes([0x81, 0x80 | len(payload)]) + mask_key + masked_payload
tls_sock.sendall(frame)

# Read echoed frame from echo-a
frame_data = leftover
while len(frame_data) < 2:
    frame_data += tls_sock.recv(4096)
f_len = frame_data[1] & 0x7F
while len(frame_data) < 2 + f_len:
    frame_data += tls_sock.recv(4096)
echo_payload = json.loads(frame_data[2 : 2 + f_len].decode("utf-8"))
assert echo_payload == {
    "target": "echo-a",
    "ws_echo": "hello-websocket",
    "path": "/ws/live",
    "raw_query": "room=alpha&mode=realtime",
}, f"Unexpected WebSocket echo payload: {echo_payload}"

# Send close frame
tls_sock.sendall(bytes([0x88, 0x80]) + mask_key)
tls_sock.close()

# Verify WebSocket handshake metadata record
rec = json.loads(
    subprocess.check_output(
        [str(root_dir / "scripts/capture-ctl.sh"), "get", ws_capture_id],
        text=True,
    )
)
assert rec["is_websocket"] is True
assert rec["capture_status"] == "metadata-only"
assert rec["replayable"] is False
ws_meta = rec["websocket_handshake"]
assert ws_meta["upgrade"].lower() == "websocket"
assert ws_meta["sec_websocket_version"] == "13"
assert ws_meta["sec_websocket_key"] == ws_key
assert ws_meta["sec_websocket_protocol"] == "chat.v1"
assert ws_meta["response_status"] == 101
assert ws_meta["sec_websocket_accept"] == expected_accept

print("PASS: WebSocket connection & bidirectional message succeeded; handshake metadata recorded.")
PY

echo "=================================================================="
echo "=== [DEV-002 8/8] Capture parse failure resilience             ==="
echo "=================================================================="
python3 - "${ROOT_DIR}" "${EDGE_HTTPS_PORT}" "${APP_A_HOST}" "${CERT_FILE}" <<'PY'
import hashlib
import json
from pathlib import Path
import subprocess
import sys

root_dir = Path(sys.argv[1])
edge_port = sys.argv[2]
app_a = sys.argv[3]
cert_file = sys.argv[4]

# 1. Malformed JSON body with Content-Type: application/json
bad_json = b'{"unterminated": [1, 2, '
hdr_json = subprocess.check_output(
    [
        "curl",
        "-sS",
        "-i",
        "--cacert",
        cert_file,
        "--resolve",
        f"{app_a}:{edge_port}:127.0.0.1",
        "-X",
        "POST",
        "-H",
        "Content-Type: application/json",
        "--data-binary",
        bad_json.decode("latin-1"),
        f"https://{app_a}:{edge_port}/v1/broken-json",
    ]
).decode("utf-8", errors="replace")

headers_raw, body_raw = hdr_json.split("\r\n\r\n", 1)
assert " 200 " in headers_raw.splitlines()[0], f"Expected 200 for malformed JSON, got: {headers_raw}"
echo_data = json.loads(body_raw)
assert echo_data["target"] == "echo-a"
assert echo_data["body_size"] == len(bad_json)
assert echo_data["body_sha256"] == hashlib.sha256(bad_json).hexdigest()

bad_json_cid = None
for line in headers_raw.splitlines():
    if line.lower().startswith("x-capture-id:"):
        bad_json_cid = line.split(":", 1)[1].strip()
assert bad_json_cid

rec_bad_json = json.loads(
    subprocess.check_output(
        [str(root_dir / "scripts/capture-ctl.sh"), "get", bad_json_cid],
        text=True,
    )
)
assert rec_bad_json["capture_status"] == "complete"
assert rec_bad_json["replayable"] is True
assert rec_bad_json["request"]["body"]["sha256"] == hashlib.sha256(bad_json).hexdigest()
assert any("json_parse_error" in e for e in rec_bad_json["derived_views"]["parse_errors"])

# 2. Malformed multipart body with Content-Type: multipart/form-data (missing boundary in body)
bad_mp = b"this-is-not-a-valid-multipart-body"
hdr_mp = subprocess.check_output(
    [
        "curl",
        "-sS",
        "-i",
        "--cacert",
        cert_file,
        "--resolve",
        f"{app_a}:{edge_port}:127.0.0.1",
        "-X",
        "POST",
        "-H",
        "Content-Type: multipart/form-data; boundary=NonExistentBoundary",
        "--data-binary",
        bad_mp.decode("latin-1"),
        f"https://{app_a}:{edge_port}/v1/broken-multipart",
    ]
).decode("utf-8", errors="replace")

mp_headers_raw, mp_body_raw = hdr_mp.split("\r\n\r\n", 1)
assert " 200 " in mp_headers_raw.splitlines()[0]
mp_cid = None
for line in mp_headers_raw.splitlines():
    if line.lower().startswith("x-capture-id:"):
        mp_cid = line.split(":", 1)[1].strip()
assert mp_cid

rec_bad_mp = json.loads(
    subprocess.check_output(
        [str(root_dir / "scripts/capture-ctl.sh"), "get", mp_cid],
        text=True,
    )
)
assert rec_bad_mp["capture_status"] == "complete"
assert rec_bad_mp["replayable"] is True
assert any("multipart_parse_error" in e for e in rec_bad_mp["derived_views"]["parse_errors"])

print("PASS: Capture parse failures recorded in parse_errors without interrupting proxy routing or raw byte storage.")
PY

"${ROOT_DIR}/scripts/capture-ctl.sh" sync

leaked_files="$(git -C "${ROOT_DIR}" status --porcelain | grep -E '(\.key$|\.pem$|\.crt$|\.mitm$|\.flow$|^.. var/)' || true)"
if [[ -n "${leaked_files}" ]]; then
  echo "FAIL: Secret, certificate, or runtime capture appeared in git status:" >&2
  echo "${leaked_files}" >&2
  exit 1
fi

echo "ALL DEV-002 CAPTURE & REPLAY TESTS PASSED."
