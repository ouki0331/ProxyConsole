#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
COMPOSE_FILE="${ROOT_DIR}/deploy/compose/docker-compose.yml"
EDGE_HTTPS_PORT="${EDGE_HTTPS_PORT:-8443}"
CONTROL_API_PORT="${CONTROL_API_PORT:-18480}"
API_BASE="http://127.0.0.1:${CONTROL_API_PORT}"
CACERT="${ROOT_DIR}/var/certificates/edge.crt"

echo "==> [1/10] Starting stack and creating baseline captures for draft immutability test (AC-2, Rule 1)"
"${ROOT_DIR}/scripts/down.sh" >/dev/null 2>&1 || true
rm -rf "${ROOT_DIR}/var/captures" "${ROOT_DIR}/var/blobs" "${ROOT_DIR}/var/database"
"${ROOT_DIR}/scripts/up.sh"

for _ in $(seq 1 30); do
  if curl -fsS "${API_BASE}/api/v1/health" >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

# 1a. Baseline JSON POST with duplicate query params, duplicate headers, cookies, and hop-by-hop header
json_resp_hdr="/tmp/pc_dev030_json_hdr.txt"
curl -fsS -D "${json_resp_hdr}" \
  --cacert "${CACERT}" \
  --resolve "app-a.local.test:${EDGE_HTTPS_PORT}:127.0.0.1" \
  -X POST \
  -H "Content-Type: application/json" \
  -H "X-Multi: one" \
  -H "X-Multi: two" \
  -H "X-Multi: three" \
  -H "Cookie: sid=orig123; theme=light; lang=ja" \
  -H "Connection: keep-alive, X-Custom-Hop" \
  -H "X-Custom-Hop: should-be-dropped" \
  --data-raw '{"user":{"profile":{"name":"Alice","role":"viewer"}},"items":[{"id":1,"qty":2},{"id":2,"qty":5}]}' \
  "https://app-a.local.test:${EDGE_HTTPS_PORT}/v1/orders?tag=alpha&tag=beta&tag=gamma&jp=%E6%97%A5%E6%9C%AC%E8%AA%9E" >/dev/null

json_cap_id="$(awk 'tolower($1)=="x-capture-id:" {gsub(/\r/,"",$2); print $2}' "${json_resp_hdr}")"
rm -f "${json_resp_hdr}"

# 1b. Baseline URL-encoded form POST with duplicate keys
form_resp_hdr="/tmp/pc_dev030_form_hdr.txt"
curl -fsS -D "${form_resp_hdr}" \
  --cacert "${CACERT}" \
  --resolve "app-a.local.test:${EDGE_HTTPS_PORT}:127.0.0.1" \
  -X POST \
  -H "Content-Type: application/x-www-form-urlencoded" \
  --data-raw "role=user&role=editor&dept=engineering" \
  "https://app-a.local.test:${EDGE_HTTPS_PORT}/v1/forms/submit?mode=initial" >/dev/null

form_cap_id="$(awk 'tolower($1)=="x-capture-id:" {gsub(/\r/,"",$2); print $2}' "${form_resp_hdr}")"
rm -f "${form_resp_hdr}"

echo "    Recorded baseline captures: json=${json_cap_id}, form=${form_cap_id}"

# Record original capture JSON and blob SHA-256 inside container to verify strict immutability
orig_hashes="$(docker compose -f "${COMPOSE_FILE}" exec -T control-api python3 -c "
import hashlib, pathlib
p = pathlib.Path('/var/lib/proxyconsole')
j_rec = (p / 'captures' / '${json_cap_id}.json').read_bytes()
j_blob = (p / 'blobs' / '${json_cap_id}.bin').read_bytes()
print(hashlib.sha256(j_rec).hexdigest() + ':' + hashlib.sha256(j_blob).hexdigest())
")"

# Create draft for JSON capture via HTTP API and draft for Form capture via CLI
json_draft="$(curl -fsS -X POST "${API_BASE}/api/v1/captures/${json_cap_id}/drafts" \
  -H "Content-Type: application/json" \
  -d '{"draft_id": "draft:json-test"}')"

form_draft="$("${ROOT_DIR}/scripts/capture-ctl.sh" draft-create "${form_cap_id}" --draft-id "draft:form-test")"

JSON_DRAFT="${json_draft}" FORM_DRAFT="${form_draft}" JSON_CAP_ID="${json_cap_id}" FORM_CAP_ID="${form_cap_id}" python3 - <<'PY'
import json, os
jd = json.loads(os.environ["JSON_DRAFT"])
fd = json.loads(os.environ["FORM_DRAFT"])
assert jd["draft_id"] == "draft:json-test", jd
assert jd["source_capture_id"] == os.environ["JSON_CAP_ID"], jd
assert jd["request"]["body"]["body_mode"] == "json", jd
assert jd["request"]["body"]["json_value"]["user"]["profile"]["role"] == "viewer", jd
assert jd["request"]["query_params"] == [["tag", "alpha"], ["tag", "beta"], ["tag", "gamma"], ["jp", "日本語"]], jd["request"]["query_params"]
assert jd["request"]["cookies"] == [["sid", "orig123"], ["theme", "light"], ["lang", "ja"]], jd["request"]["cookies"]

assert fd["draft_id"] == "draft:form-test", fd
assert fd["source_capture_id"] == os.environ["FORM_CAP_ID"], fd
assert fd["request"]["body"]["body_mode"] == "urlencoded_form", fd
assert fd["request"]["body"]["form_fields"] == [["role", "user"], ["role", "editor"], ["dept", "engineering"]], fd
PY

echo "==> [2/10] Verifying duplicate query & header ordered add/update/delete and percent-encoded round-trip (AC-3, AC-4, Rule 2, Rule 3)"
updated_json_draft="$(curl -fsS -X PUT "${API_BASE}/api/v1/drafts/draft:json-test" \
  -H "Content-Type: application/json" \
  -d '{
    "decoded_path": "/v1/日本語 パス/orders",
    "query_mutations": [
      {"op": "set", "name": "tag", "occurrence": 1, "value": "beta-edited"},
      {"op": "remove", "name": "tag", "occurrence": 0},
      {"op": "add", "name": "tag", "value": "delta"},
      {"op": "set", "name": "jp", "value": "東京 & 大阪"},
      {"op": "add", "name": "expr", "value": "a+b&c=d?e/f#g"},
      {"op": "add", "name": "space key", "value": "hello world"}
    ],
    "header_mutations": [
      {"op": "set", "name": "X-Multi", "occurrence": 1, "value": "two-edited"},
      {"op": "remove", "name": "X-Multi", "occurrence": 0},
      {"op": "add", "name": "X-Multi", "value": "four"}
    ]
  }')"

UPDATED_DRAFT="${updated_json_draft}" python3 - <<'PY'
import json, os
d = json.loads(os.environ["UPDATED_DRAFT"])
req = d["request"]
assert req["decoded_path"] == "/v1/日本語 パス/orders", req
assert req["raw_path"] == "/v1/%E6%97%A5%E6%9C%AC%E8%AA%9E%20%E3%83%91%E3%82%B9/orders", req
expected_q = [
    ["tag", "beta-edited"],
    ["tag", "gamma"],
    ["jp", "東京 & 大阪"],
    ["tag", "delta"],
    ["expr", "a+b&c=d?e/f#g"],
    ["space key", "hello world"],
]
assert req["query_params"] == expected_q, req["query_params"]
x_multi = [v for k, v in req["headers"] if k.lower() == "x-multi"]
assert x_multi == ["two-edited", "three", "four"], x_multi
PY

echo "==> [3/10] Verifying Cookie view add/update/delete, structured JSON nested edits, hop-by-hop exclusion, and Content-Length recalculation (AC-5, AC-6, AC-7, Rule 4, Rule 5)"
updated_json_draft_2="$(curl -fsS -X PATCH "${API_BASE}/api/v1/drafts/draft:json-test" \
  -H "Content-Type: application/json" \
  -d '{
    "header_mutations": [
      {"op": "add", "name": "Connection", "value": "keep-alive, X-Custom-Hop"},
      {"op": "add", "name": "Keep-Alive", "value": "timeout=5"}
    ],
    "cookie_mutations": [
      {"op": "set", "name": "theme", "value": "dark"},
      {"op": "remove", "name": "lang", "occurrence": 0},
      {"op": "add", "name": "trace_id", "value": "xyz-987"}
    ],
    "json_mutations": [
      {"op": "set", "path": "user.profile.role", "value": "superadmin"},
      {"op": "set", "path": "items.1.qty", "value": 99},
      {"op": "set", "path": "audit.note", "value": "Edited in DEV-030 with significantly longer payload to verify Content-Length recalculation"}
    ]
  }')"

replay_json_res="$(curl -fsS -X POST "${API_BASE}/api/v1/drafts/draft:json-test/replay" \
  -H "Content-Type: application/json" \
  -d '{}')"

UPDATED_DRAFT="${updated_json_draft_2}" REPLAY_RES="${replay_json_res}" python3 - <<'PY'
import json, os
d = json.loads(os.environ["UPDATED_DRAFT"])
rpl = json.loads(os.environ["REPLAY_RES"])

# Verify normalized hop-by-hop header exclusion (Connection and X-Custom-Hop named in Connection)
norm = d["normalized_preview"]
excluded_names = {k.lower() for k, _ in norm["excluded_hop_by_hop_headers"]}
assert "connection" in excluded_names, excluded_names
assert "x-custom-hop" in excluded_names, excluded_names
sent_header_names = {k.lower() for k, _ in norm["headers"]}
assert "x-custom-hop" not in sent_header_names, sent_header_names
assert "transfer-encoding" not in sent_header_names, sent_header_names

# Verify replay reached echo-a with exact edited path, query, headers, cookies, JSON body, and recalculated Content-Length
assert rpl["response_status"] == 200, rpl
echo = rpl["response_json"]
assert echo["target"] == "echo-a", echo
assert echo["path"] == "/v1/%E6%97%A5%E6%9C%AC%E8%AA%9E%20%E3%83%91%E3%82%B9/orders", echo["path"]
assert echo["query_params"] == [
    ["tag", "beta-edited"],
    ["tag", "gamma"],
    ["jp", "東京 & 大阪"],
    ["tag", "delta"],
    ["expr", "a+b&c=d?e/f#g"],
    ["space key", "hello world"],
], echo["query_params"]

echo_x_multi = [v for k, v in echo["headers"] if k.lower() == "x-multi"]
assert echo_x_multi == ["two-edited", "three", "four"], echo_x_multi
echo_custom_hop = [v for k, v in echo["headers"] if k.lower() == "x-custom-hop"]
assert echo_custom_hop == [], f"Hop-by-hop header X-Custom-Hop must not reach target: {echo_custom_hop}"

cookie_hdrs = [v for k, v in echo["headers"] if k.lower() == "cookie"]
assert cookie_hdrs == ["sid=orig123; theme=dark; trace_id=xyz-987"], cookie_hdrs

assert echo["body_size"] == norm["content_length"], (echo["body_size"], norm["content_length"])
assert echo["body_json"]["user"]["profile"]["role"] == "superadmin", echo["body_json"]
assert echo["body_json"]["items"][1]["qty"] == 99, echo["body_json"]
assert "Content-Length recalculation" in echo["body_json"]["audit"]["note"], echo["body_json"]
PY

# Verify original capture JSON and blob bytes are 100% untouched after draft edits and replay (AC-2, Rule 1)
after_hashes="$(docker compose -f "${COMPOSE_FILE}" exec -T control-api python3 -c "
import hashlib, pathlib
p = pathlib.Path('/var/lib/proxyconsole')
j_rec = (p / 'captures' / '${json_cap_id}.json').read_bytes()
j_blob = (p / 'blobs' / '${json_cap_id}.bin').read_bytes()
print(hashlib.sha256(j_rec).hexdigest() + ':' + hashlib.sha256(j_blob).hexdigest())
")"
if [[ "${orig_hashes}" != "${after_hashes}" ]]; then
  echo "ERROR: Original capture record or blob was mutated! (${orig_hashes} != ${after_hashes})" >&2
  exit 1
fi

echo "==> [4/10] Verifying URL-encoded form duplicate key editing via CLI and replay (AC-5, Rule 4)"
"${ROOT_DIR}/scripts/capture-ctl.sh" draft-update "draft:form-test" \
  --form-set "role@1=owner" \
  --form-add "role=auditor" \
  --form-add "special=a+b&c=d=100%" >/dev/null

replay_form_res="$("${ROOT_DIR}/scripts/capture-ctl.sh" draft-replay "draft:form-test")"
REPLAY_FORM_RES="${replay_form_res}" python3 - <<'PY'
import json, os
rpl = json.loads(os.environ["REPLAY_FORM_RES"])
assert rpl["response_status"] == 200, rpl
echo = rpl["response_json"]
assert echo["form_params"] == [
    ["role", "user"],
    ["role", "owner"],
    ["dept", "engineering"],
    ["role", "auditor"],
    ["special", "a+b&c=d=100%"],
], echo["form_params"]
PY

echo "==> [5/10] Verifying malformed JSON/form fallback and explicit structured edit error handling (Rule 4)"
mal_hdr="/tmp/pc_dev030_mal_hdr.txt"
curl -fsS -D "${mal_hdr}" \
  --cacert "${CACERT}" \
  --resolve "app-a.local.test:${EDGE_HTTPS_PORT}:127.0.0.1" \
  -X POST \
  -H "Content-Type: application/json" \
  --data-raw '{"malformed_json": [1, 2, ' \
  "https://app-a.local.test:${EDGE_HTTPS_PORT}/v1/broken-json" >/dev/null
mal_cap_id="$(awk 'tolower($1)=="x-capture-id:" {gsub(/\r/,"",$2); print $2}' "${mal_hdr}")"
rm -f "${mal_hdr}"

mal_draft="$(curl -fsS -X POST "${API_BASE}/api/v1/captures/${mal_cap_id}/drafts" \
  -H "Content-Type: application/json" \
  -d '{"draft_id": "draft:malformed-json"}')"

# Attempting structured json_mutations on malformed JSON returns HTTP 400 with explicit error, while raw_text fallback works
mal_err_code="$(curl -s -o /tmp/pc_mal_err.json -w "%{http_code}" -X PUT "${API_BASE}/api/v1/drafts/draft:malformed-json" \
  -H "Content-Type: application/json" \
  -d '{"json_mutations": [{"op": "set", "path": "a.b", "value": 1}]}')"

mal_raw_ok="$(curl -fsS -X PUT "${API_BASE}/api/v1/drafts/draft:malformed-json" \
  -H "Content-Type: application/json" \
  -d '{"raw_text": "{\"recovered_via_raw_fallback\": true}"}')"

MAL_DRAFT="${mal_draft}" MAL_ERR_CODE="${mal_err_code}" MAL_RAW_OK="${mal_raw_ok}" python3 - <<'PY'
import json, os
md = json.loads(os.environ["MAL_DRAFT"])
assert md["request"]["body"]["body_mode"] == "raw_text", md
assert len(md["request"]["body"]["parse_errors"]) > 0, md
assert md["request"]["body"]["raw_text"] == '{"malformed_json": [1, 2, ', md

assert os.environ["MAL_ERR_CODE"] == "400", os.environ["MAL_ERR_CODE"]
with open("/tmp/pc_mal_err.json", encoding="utf-8") as f:
    err = json.load(f)
assert err["error"] == "not_json_body", err

ok = json.loads(os.environ["MAL_RAW_OK"])
assert ok["request"]["body"]["raw_text"] == '{"recovered_via_raw_fallback": true}', ok
assert ok["request"]["body"]["json_value"] == {"recovered_via_raw_fallback": True}, ok
PY
rm -f /tmp/pc_mal_err.json

echo "==> [6/10] Verifying GET/POST/PUT/PATCH/DELETE replay, target routing, and side-effect warning metadata (AC-8, Rule 7)"
ROOT_DIR="${ROOT_DIR}" API_BASE="${API_BASE}" python3 - <<'PY'
import json, os, urllib.request

api_base = os.environ["API_BASE"]

def api_req(method: str, path: str, body: dict | None = None) -> dict:
    req = urllib.request.Request(
        f"{api_base}{path}",
        data=json.dumps(body).encode("utf-8") if body is not None else None,
        headers={"Content-Type": "application/json"},
        method=method,
    )
    with urllib.request.urlopen(req, timeout=15.0) as resp:
        return json.loads(resp.read().decode("utf-8"))

for m in ("GET", "POST", "PUT", "PATCH", "DELETE"):
    target_host = "app-b.local.test" if m in ("PUT", "DELETE") else "app-a.local.test"
    expected_target = "echo-b" if target_host == "app-b.local.test" else "echo-a"
    upd = api_req(
        "PUT",
        "/api/v1/drafts/draft:malformed-json",
        {
            "method": m,
            "authority": target_host,
            "raw_path": f"/v1/method-check/{m.lower()}",
            "raw_text": "" if m in ("GET", "DELETE") else json.dumps({"method_tested": m}),
        },
    )
    warn = upd["side_effect_warning"]
    if m == "GET":
        assert warn["has_side_effect_warning"] is False, warn
        assert warn["is_safe_method"] is True, warn
    else:
        assert warn["has_side_effect_warning"] is True, warn
        assert warn["is_safe_method"] is False, warn
        assert warn["requires_confirmation_in_ui"] is True, warn
        assert warn["code"] == "non_safe_method_side_effects", warn

    rpl = api_req("POST", "/api/v1/drafts/draft:malformed-json/replay", {})
    assert rpl["response_status"] == 200, (m, rpl)
    assert rpl["side_effect_warning"]["has_side_effect_warning"] == (m != "GET"), rpl
    echo = rpl["response_json"]
    assert echo["method"] == m, echo
    assert echo["target"] == expected_target, echo
    assert echo["host"] == target_host, echo
PY

echo "==> [7/10] Verifying non-replayable capture rejection (truncated, metadata-only, capture-failed, missing blob) (AC-9, Rule 6)"
# 7a. Create truncated capture (> 1 MiB body)
trunc_hdr="/tmp/pc_dev030_trunc_hdr.txt"
python3 -c 'import sys; sys.stdout.write("X" * (1048576 + 65536))' | curl -fsS -D "${trunc_hdr}" \
  --cacert "${CACERT}" \
  --resolve "app-a.local.test:${EDGE_HTTPS_PORT}:127.0.0.1" \
  -X POST \
  -H "Content-Type: text/plain" \
  --data-binary @- \
  "https://app-a.local.test:${EDGE_HTTPS_PORT}/v1/truncated-test" >/dev/null
trunc_cap_id="$(awk 'tolower($1)=="x-capture-id:" {gsub(/\r/,"",$2); print $2}' "${trunc_hdr}")"
rm -f "${trunc_hdr}"

# 7b. Create synthetic metadata-only, capture-failed, and missing-blob records inside storage_dir
docker compose -f "${COMPOSE_FILE}" exec -T -e BASE_CAP_ID="${json_cap_id}" control-api python3 - <<'PY'
import json, os, shutil
from index_store import CaptureIndexStore

store = CaptureIndexStore()
base = json.loads((store.captures_dir / f"{os.environ['BASE_CAP_ID']}.json").read_text(encoding="utf-8"))

# metadata-only record
meta_rec = json.loads(json.dumps(base))
meta_rec["capture_id"] = "cap-test-metadata-only"
meta_rec["capture_status"] = "metadata-only"
meta_rec["replayable"] = False
meta_rec["request"]["body"]["blob_ref"] = None
meta_rec["request"]["body"]["captured_size"] = 0
(store.captures_dir / "cap-test-metadata-only.json").write_text(json.dumps(meta_rec), encoding="utf-8")
store.index_record_dict(meta_rec, "captures/cap-test-metadata-only.json")

# capture-failed record
fail_rec = json.loads(json.dumps(base))
fail_rec["capture_id"] = "cap-test-capture-failed"
fail_rec["capture_status"] = "capture-failed"
fail_rec["replayable"] = False
fail_rec["request"]["body"]["blob_ref"] = None
(store.captures_dir / "cap-test-capture-failed.json").write_text(json.dumps(fail_rec), encoding="utf-8")
store.index_record_dict(fail_rec, "captures/cap-test-capture-failed.json")

# missing-blob record (marked complete, but blob file does not exist)
miss_rec = json.loads(json.dumps(base))
miss_rec["capture_id"] = "cap-test-missing-blob"
miss_rec["capture_status"] = "complete"
miss_rec["replayable"] = True
miss_rec["request"]["raw_path"] = "/v1/must-never-reach-target"
miss_rec["request"]["body"]["blob_ref"] = "blobs/cap-test-missing-blob.bin"
(store.captures_dir / "cap-test-missing-blob.json").write_text(json.dumps(miss_rec), encoding="utf-8")
store.index_record_dict(miss_rec, "captures/cap-test-missing-blob.json")
PY

API_BASE="${API_BASE}" TRUNC_CAP_ID="${trunc_cap_id}" python3 - <<'PY'
import json, os, urllib.error, urllib.request

api_base = os.environ["API_BASE"]
cases = [
    (os.environ["TRUNC_CAP_ID"], "truncated"),
    ("cap-test-metadata-only", "metadata-only"),
    ("cap-test-capture-failed", "capture-failed"),
    ("cap-test-missing-blob", "missing_blob"),
]

for cid, expected_reason in cases:
    # 1. Check /replayability endpoint
    with urllib.request.urlopen(f"{api_base}/api/v1/captures/{cid}/replayability") as r:
        chk = json.loads(r.read().decode("utf-8"))
    assert chk["replayable"] is False, (cid, chk)
    assert chk["reason"] == expected_reason, (cid, expected_reason, chk)

    # 2. Attempt draft creation -> must fail with 400 non_replayable_capture
    for endpoint in (f"/api/v1/captures/{cid}/drafts", f"/api/v1/captures/{cid}/replay"):
        req = urllib.request.Request(
            f"{api_base}{endpoint}",
            data=b"{}",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            urllib.request.urlopen(req)
            raise AssertionError(f"Expected 400 for {endpoint}, but succeeded!")
        except urllib.error.HTTPError as exc:
            assert exc.code == 400, (endpoint, exc.code)
            err_body = json.loads(exc.read().decode("utf-8"))
            assert err_body["error"] == "non_replayable_capture", err_body
            assert err_body["reason"] == expected_reason, (expected_reason, err_body)

# Clean up synthetic non-replayable records so index stays clean
PY

docker compose -f "${COMPOSE_FILE}" exec -T control-api python3 - <<'PY'
from index_store import CaptureIndexStore
store = CaptureIndexStore()
for cid in ("cap-test-metadata-only", "cap-test-capture-failed", "cap-test-missing-blob"):
    p = store.captures_dir / f"{cid}.json"
    if p.exists():
        p.unlink()
store.rebuild_index()
PY

echo "==> [8/10] Verifying cURL and browser fetch exports reproduce identical target-observable semantics (AC-10, Rule 8, Rule 9)"
json_export="$(curl -fsS "${API_BASE}/api/v1/drafts/draft:json-test/export?cacert=${CACERT}")"
form_export="$("${ROOT_DIR}/scripts/capture-ctl.sh" draft-export "draft:form-test" --cacert "${CACERT}")"

JSON_EXPORT="${json_export}" FORM_EXPORT="${form_export}" CACERT="${CACERT}" EDGE_HTTPS_PORT="${EDGE_HTTPS_PORT}" python3 - <<'PY'
import json, os, socket, ssl, subprocess, urllib.request

j_exp = json.loads(os.environ["JSON_EXPORT"])
f_exp = json.loads(os.environ["FORM_EXPORT"])
cacert = os.environ["CACERT"]

# Verify old capture values do not leak into exports (Rule 8)
assert "viewer" not in j_exp["curl"] and "superadmin" in j_exp["curl"], j_exp["curl"]
assert "viewer" not in j_exp["fetch"] and "superadmin" in j_exp["fetch"], j_exp["fetch"]
assert "role=editor" not in f_exp["curl"] and "role=owner" in f_exp["curl"], f_exp["curl"]

# 1. Execute exported curl_local_edge for both JSON and Form drafts and verify echo-a response
for exp in (j_exp, f_exp):
    proc = subprocess.run(
        exp["curl_local_edge"],
        shell=True,
        check=True,
        capture_output=True,
        text=True,
    )
    curl_echo = json.loads(proc.stdout)
    assert curl_echo["target"] == "echo-a", curl_echo
    assert curl_echo["body_sha256"] == exp["body_sha256"], (curl_echo["body_sha256"], exp["body_sha256"])

# 2. Verify fetch JS syntax and execute fetch_request against live HTTPS edge to prove identical target-observable semantics
for exp in (j_exp, f_exp):
    node_Check = subprocess.run(
        [
            "node",
            "-e",
            "const AF = Object.getPrototypeOf(async function(){}).constructor; new AF(process.env.FETCH_CODE);",
        ],
        env={**os.environ, "FETCH_CODE": exp["fetch"]},
        capture_output=True,
        text=True,
    ) if subprocess.run(["which", "node"], capture_output=True).returncode == 0 else None
    if node_Check is not None:
        assert node_Check.returncode == 0, node_Check.stderr

    # Execute the exported fetch_request structure over HTTPS to the edge
    freq = exp["fetch_request"]
    init = freq["init"]
    ctx = ssl.create_default_context(cafile=cacert)
    ctx.check_hostname = False
    import http.client
    from urllib.parse import urlsplit
    sp = urlsplit(freq["edge_url"])
    raw_s = socket.create_connection(("127.0.0.1", sp.port), timeout=10.0)
    tls_s = ctx.wrap_socket(raw_s, server_hostname=sp.hostname)
    hconn = http.client.HTTPConnection("127.0.0.1", sp.port, timeout=10.0)
    hconn.sock = tls_s
    target_p = f"{sp.path}?{sp.query}" if sp.query else sp.path
    hconn.putrequest(init["method"], target_p, skip_host=True, skip_accept_encoding=True)
    for hk, hv in init["headers"]:
        hconn.putheader(hk, hv)
    hconn.putheader("Connection", "close")
    body_b = init.get("body", "").encode("utf-8") if "body" in init else b""
    hconn.endheaders(message_body=body_b if body_b else None)
    r_fetch = hconn.getresponse()
    fetch_echo = json.loads(r_fetch.read().decode("utf-8"))
    hconn.close()
    assert fetch_echo["target"] == "echo-a", fetch_echo
    assert fetch_echo["body_sha256"] == exp["body_sha256"], fetch_echo

j_curl_echo = json.loads(
    subprocess.run(j_exp["curl_local_edge"], shell=True, check=True, capture_output=True, text=True).stdout
)
assert j_curl_echo["body_json"]["user"]["profile"]["role"] == "superadmin"
assert j_curl_echo["query_params"] == [
    ["tag", "beta-edited"],
    ["tag", "gamma"],
    ["jp", "東京 & 大阪"],
    ["tag", "delta"],
    ["expr", "a+b&c=d?e/f#g"],
    ["space key", "hello world"],
]
PY

echo "==> [9/10] Verifying replay capture searchability and lineage tracking (AC-11, Rule 10)"
by_src="$(curl -fsS "${API_BASE}/api/v1/captures?source_capture_id=${json_cap_id}")"
by_draft="$(curl -fsS "${API_BASE}/api/v1/captures?draft_id=draft:json-test")"
orig_detail="$(curl -fsS "${API_BASE}/api/v1/captures/${json_cap_id}")"

BY_SRC="${by_src}" BY_DRAFT="${by_draft}" ORIG_DETAIL="${orig_detail}" JSON_CAP_ID="${json_cap_id}" API_BASE="${API_BASE}" python3 - <<'PY'
import json, os, urllib.request

by_src = json.loads(os.environ["BY_SRC"])
by_draft = json.loads(os.environ["BY_DRAFT"])
orig = json.loads(os.environ["ORIG_DETAIL"])
src_id = os.environ["JSON_CAP_ID"]
api_base = os.environ["API_BASE"]

assert by_src["total"] >= 1, by_src
assert by_draft["total"] >= 1, by_draft
result_cid = by_draft["items"][0]["capture_id"]
assert result_cid != src_id

lineage = by_draft["items"][0]["replay_lineage"]
assert lineage["is_replay"] is True, lineage
assert lineage["source_capture_id"] == src_id, lineage
assert lineage["draft_id"] == "draft:json-test", lineage

# Verify child capture detail also has replay_relations
with urllib.request.urlopen(f"{api_base}/api/v1/captures/{result_cid}") as r:
    child_detail = json.loads(r.read().decode("utf-8"))
rel = child_detail["replay_relations"]
assert rel["is_replay"] is True, rel
assert rel["replayed_from"]["source_capture_id"] == src_id, rel
assert rel["replayed_from"]["draft_id"] == "draft:json-test", rel

# Verify source capture detail lists replays_from_this_capture and drafts_from_this_capture
orig_rel = orig["replay_relations"]
assert any(item["result_capture_id"] == result_cid for item in orig_rel["replays_from_this_capture"]), orig_rel
assert any(d["draft_id"] == "draft:json-test" for d in orig_rel["drafts_from_this_capture"]), orig_rel
PY

echo "==> [10/10] Verifying draft persistence across container restart, TTL expiry, and explicit deletion (AC-12)"
# Create a short-TTL draft and verify expiry
curl -fsS -X POST "${API_BASE}/api/v1/captures/${json_cap_id}/drafts" \
  -H "Content-Type: application/json" \
  -d '{"draft_id": "draft:ttl-test", "ttl_seconds": 0.2}' >/dev/null
sleep 0.4
ttl_code="$(curl -s -o /tmp/pc_ttl_err.json -w "%{http_code}" "${API_BASE}/api/v1/drafts/draft:ttl-test")"
if [[ "${ttl_code}" != "404" ]]; then
  echo "ERROR: Expected 404 for expired draft, got ${ttl_code}" >&2
  exit 1
fi
rm -f /tmp/pc_ttl_err.json

# Restart control-api container and verify persistent draft read-back
docker compose -f "${COMPOSE_FILE}" restart control-api >/dev/null 2>&1
for _ in $(seq 1 30); do
  if curl -fsS "${API_BASE}/api/v1/health" >/dev/null 2>&1; then
    break
  fi
  sleep 0.5
done

persisted_draft="$(curl -fsS "${API_BASE}/api/v1/drafts/draft:json-test")"
PERSISTED_DRAFT="${persisted_draft}" python3 - <<'PY'
import json, os
d = json.loads(os.environ["PERSISTED_DRAFT"])
assert d["draft_id"] == "draft:json-test", d
assert d["request"]["body"]["json_value"]["user"]["profile"]["role"] == "superadmin", d
assert d["replays_count"] >= 1, d
PY

# Explicit delete and verify 404 not-found
curl -fsS -X DELETE "${API_BASE}/api/v1/drafts/draft:malformed-json" >/dev/null
del_code="$(curl -s -o /dev/null -w "%{http_code}" "${API_BASE}/api/v1/drafts/draft:malformed-json")"
if [[ "${del_code}" != "404" ]]; then
  echo "ERROR: Expected 404 after deleting draft, got ${del_code}" >&2
  exit 1
fi

git_dirty="$(git -C "${ROOT_DIR}" status --porcelain -- var/ 2>/dev/null || true)"
if [[ -n "${git_dirty}" ]]; then
  echo "ERROR: Runtime files appeared in git status: ${git_dirty}" >&2
  exit 1
fi

echo "PASS: All DEV-030 Request draft / replay / export core tests succeeded!"
