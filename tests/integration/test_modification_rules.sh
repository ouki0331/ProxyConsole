#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
COMPOSE_FILE="${ROOT_DIR}/deploy/compose/docker-compose.yml"
EDGE_HTTPS_PORT="${EDGE_HTTPS_PORT:-8443}"
CONTROL_API_PORT="${CONTROL_API_PORT:-18480}"
API_BASE="http://127.0.0.1:${CONTROL_API_PORT}"
CACERT="${ROOT_DIR}/var/certificates/edge.crt"

echo "==> [1/10] Starting stack and verifying Rule CRUD, copy, enable/disable, priority & restart persistence (AC-1, AC-2, Rule 9)"
"${ROOT_DIR}/scripts/down.sh" >/dev/null 2>&1 || true
rm -rf "${ROOT_DIR}/var/captures" "${ROOT_DIR}/var/blobs" "${ROOT_DIR}/var/database"
"${ROOT_DIR}/scripts/up.sh"

for _ in $(seq 1 30); do
  if curl -fsS "${API_BASE}/api/v1/health" >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

# Create rule via HTTP API
r1_create="$(curl -fsS -X POST "${API_BASE}/api/v1/rules" \
  -H "Content-Type: application/json" \
  -d '{
    "rule_id": "rule:crud-alpha",
    "name": "CRUD Alpha Rule",
    "description": "Initial description",
    "enabled": true,
    "priority": 50,
    "match": {
      "route_id": "app-a",
      "domain": "app-a.local.test",
      "method": ["POST"],
      "path_prefix": "/v1/crud"
    },
    "actions": [
      {"target": "header", "op": "set", "name": "X-Crud-Alpha", "value": "v1"}
    ]
  }')"

# Copy rule via HTTP API
r1_copy="$(curl -fsS -X POST "${API_BASE}/api/v1/rules/rule:crud-alpha/copy" \
  -H "Content-Type: application/json" \
  -d '{
    "rule_id": "rule:crud-beta",
    "name": "CRUD Beta Copy",
    "priority": 60
  }')"

# Update rule via CLI
r1_update="$("${ROOT_DIR}/scripts/capture-ctl.sh" rule-update "rule:crud-alpha" \
  --description "Updated via CLI" \
  --priority 40)"

# Disable & re-enable via HTTP API and CLI
r1_dis="$(curl -fsS -X POST "${API_BASE}/api/v1/rules/rule:crud-beta/disable" -H "Content-Type: application/json" -d '{}')"
r1_en="$("${ROOT_DIR}/scripts/capture-ctl.sh" rule-enable "rule:crud-beta")"

R1_CREATE="${r1_create}" R1_COPY="${r1_copy}" R1_UPDATE="${r1_update}" R1_DIS="${r1_dis}" R1_EN="${r1_en}" python3 - <<'PY'
import json, os
c = json.loads(os.environ["R1_CREATE"])
cp = json.loads(os.environ["R1_COPY"])
up = json.loads(os.environ["R1_UPDATE"])
dis = json.loads(os.environ["R1_DIS"])
en = json.loads(os.environ["R1_EN"])
assert c["rule_id"] == "rule:crud-alpha" and c["priority"] == 50, c
assert cp["rule_id"] == "rule:crud-beta" and cp["copied_from_rule_id"] == "rule:crud-alpha", cp
assert up["priority"] == 40 and up["description"] == "Updated via CLI", up
assert dis["enabled"] is False, dis
assert en["enabled"] is True, en
PY

# Restart inspector and control-api containers and verify rules persist in SQLite
docker compose -f "${COMPOSE_FILE}" restart inspector control-api >/dev/null
sleep 1
for _ in $(seq 1 30); do
  if curl -fsS "${API_BASE}/api/v1/health" >/dev/null 2>&1 && \
     curl -fsS --cacert "${CACERT}" --resolve "app-a.local.test:${EDGE_HTTPS_PORT}:127.0.0.1" "https://app-a.local.test:${EDGE_HTTPS_PORT}/healthz" >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

rules_after_restart="$("${ROOT_DIR}/scripts/capture-ctl.sh" rule-list)"
RULES_LIST="${rules_after_restart}" python3 - <<'PY'
import json, os
data = json.loads(os.environ["RULES_LIST"])
ids = [r["rule_id"] for r in data["items"]]
assert ids == ["rule:crud-alpha", "rule:crud-beta"], ids
PY

# Delete CRUD test rules and verify 404 on get
curl -fsS -X DELETE "${API_BASE}/api/v1/rules/rule:crud-alpha" >/dev/null
"${ROOT_DIR}/scripts/capture-ctl.sh" rule-delete "rule:crud-beta" >/dev/null
del_code="$(curl -s -o /dev/null -w "%{http_code}" "${API_BASE}/api/v1/rules/rule:crud-alpha")"
if [ "${del_code}" != "404" ]; then
  echo "Expected 404 for deleted rule, got ${del_code}" >&2
  exit 1
fi

echo "==> [2/10] Verifying save-time validation rejects invalid regex, target/op, and schema (AC-9)"
bad_regex_code="$(curl -s -o /tmp/pc_dev040_bad_regex.json -w "%{http_code}" \
  -X POST "${API_BASE}/api/v1/rules" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Bad Regex Rule",
    "match": {"path_regex": "[unclosed("},
    "actions": [{"target": "header", "op": "add", "name": "X-Test", "value": "1"}]
  }')"
bad_action_code="$(curl -s -o /tmp/pc_dev040_bad_action.json -w "%{http_code}" \
  -X POST "${API_BASE}/api/v1/rules" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Bad Action Rule",
    "match": {"domain": "app-a.local.test"},
    "actions": [{"target": "unknown_target", "op": "set", "name": "x", "value": "1"}]
  }')"
bad_empty_code="$(curl -s -o /tmp/pc_dev040_bad_empty.json -w "%{http_code}" \
  -X POST "${API_BASE}/api/v1/rules" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Empty Actions Rule",
    "match": {"domain": "app-a.local.test"},
    "actions": []
  }')"

if [ "${bad_regex_code}" != "400" ] || [ "${bad_action_code}" != "400" ] || [ "${bad_empty_code}" != "400" ]; then
  echo "Expected 400 for invalid rules, got regex=${bad_regex_code} action=${bad_action_code} empty=${bad_empty_code}" >&2
  exit 1
fi
python3 - <<'PY'
import json
r1 = json.loads(open("/tmp/pc_dev040_bad_regex.json").read())
r2 = json.loads(open("/tmp/pc_dev040_bad_action.json").read())
r3 = json.loads(open("/tmp/pc_dev040_bad_empty.json").read())
assert r1["error"] == "invalid_regex", r1
assert r2["error"] == "invalid_action_target", r2
assert r3["error"] == "invalid_actions", r3
PY
rm -f /tmp/pc_dev040_bad_regex.json /tmp/pc_dev040_bad_action.json /tmp/pc_dev040_bad_empty.json

echo "==> [3/10] Verifying individual & composite matchers, duplicate query/header/cookie edits, Unicode/reserved chars & hop-by-hop stripping (AC-3, AC-4, AC-6, AC-12, Rule 4, Rule 5, Rule 6)"
curl -fsS -X POST "${API_BASE}/api/v1/rules" \
  -H "Content-Type: application/json" \
  -d '{
    "rule_id": "rule:composite-json",
    "name": "Composite Matcher + Query/Header/Cookie/JSON Mutator",
    "enabled": true,
    "priority": 10,
    "match": {
      "route_id": "app-a",
      "domain": "app-a.local.test",
      "method": "POST",
      "path_exact": "/v1/orders/create",
      "path_prefix": "/v1/orders",
      "path_regex": "^/v1/orders/[a-z]+$",
      "query_pairs": [
        {"name": "tag", "value": "alpha"},
        {"name": "env", "value_regex": "^prod-[0-9]+$"}
      ],
      "header_pairs": [
        {"name": "x-auth-mode", "value": "token"},
        {"name": "x-trace-id", "value_regex": "^tr-[0-9]+$"}
      ],
      "content_type": "application/json"
    },
    "actions": [
      {"target": "query", "op": "set", "name": "tag", "occurrence": 1, "value": "beta-mod"},
      {"target": "query", "op": "remove", "name": "tag", "occurrence": 0},
      {"target": "query", "op": "add", "name": "tag", "value": "delta"},
      {"target": "query", "op": "set", "name": "jp", "value": "東京 & 大阪"},
      {"target": "query", "op": "add", "name": "expr", "value": "a+b&c=d?e/f#g"},
      {"target": "query", "op": "add", "name": "space key", "value": "hello world"},
      {"target": "header", "op": "set", "name": "X-Multi", "occurrence": 1, "value": "two-mod"},
      {"target": "header", "op": "remove", "name": "X-Multi", "occurrence": 0},
      {"target": "header", "op": "add", "name": "X-Multi", "value": "four"},
      {"target": "header", "op": "add", "name": "Connection", "value": "keep-alive, X-Custom-Hop"},
      {"target": "header", "op": "add", "name": "Keep-Alive", "value": "timeout=5"},
      {"target": "header", "op": "add", "name": "Transfer-Encoding", "value": "chunked"},
      {"target": "cookie", "op": "set", "name": "theme", "value": "dark"},
      {"target": "cookie", "op": "remove", "name": "lang"},
      {"target": "cookie", "op": "add", "name": "debug", "value": "1"},
      {"target": "json", "op": "set", "path": "user.profile.role", "value": "admin"},
      {"target": "json", "op": "set", "path": "user.profile.region", "value": "日本"},
      {"target": "json", "op": "set", "path": "items[1].qty", "value": 99},
      {"target": "json", "op": "remove", "path": "items[0]"}
    ]
  }' >/dev/null

json_hdr="/tmp/pc_dev040_json_hdr.txt"
json_echo="$(curl -fsS -D "${json_hdr}" \
  --cacert "${CACERT}" \
  --resolve "app-a.local.test:${EDGE_HTTPS_PORT}:127.0.0.1" \
  -X POST \
  -H "Content-Type: application/json; charset=utf-8" \
  -H "X-Auth-Mode: token" \
  -H "X-Trace-Id: tr-42" \
  -H "X-Multi: one" \
  -H "X-Multi: two" \
  -H "X-Multi: three" \
  -H "Cookie: sid=s100; theme=light; lang=ja" \
  -H "Keep-Alive: timeout=5" \
  --data-raw '{"user":{"profile":{"name":"Alice","role":"viewer"}},"items":[{"id":1,"qty":2},{"id":2,"qty":5}]}' \
  "https://app-a.local.test:${EDGE_HTTPS_PORT}/v1/orders/create?tag=alpha&tag=beta&tag=gamma&env=prod-1&jp=%E6%97%A5%E6%9C%AC")"

json_cap_id="$(awk 'tolower($1)=="x-capture-id:" {gsub(/\r/,"",$2); print $2}' "${json_hdr}")"
rm -f "${json_hdr}"
json_cap_detail="$(curl -fsS "${API_BASE}/api/v1/captures/${json_cap_id}")"

JSON_ECHO="${json_echo}" JSON_DET="${json_cap_detail}" python3 - <<'PY'
import json, os
echo = json.loads(os.environ["JSON_ECHO"])
det = json.loads(os.environ["JSON_DET"])
assert echo["target"] == "echo-a", echo
assert echo["method"] == "POST", echo
assert echo["path"] == "/v1/orders/create", echo

assert echo["query_params"] == [
    ["tag", "beta-mod"],
    ["tag", "gamma"],
    ["env", "prod-1"],
    ["jp", "東京 & 大阪"],
    ["tag", "delta"],
    ["expr", "a+b&c=d?e/f#g"],
    ["space key", "hello world"],
], echo["query_params"]

hdr_pairs = echo["headers"]
x_multi = [v for k, v in hdr_pairs if k.lower() == "x-multi"]
assert x_multi == ["two-mod", "three", "four"], x_multi
cookies = [v for k, v in hdr_pairs if k.lower() == "cookie"]
assert cookies == ["sid=s100; theme=dark; debug=1"], cookies
hop_hdrs = [v for k, v in hdr_pairs if k.lower() in ("transfer-encoding", "keep-alive")]
assert hop_hdrs == [], hop_hdrs

reval = det["rule_evaluation"]
chg_fields = [c["field"].lower() for c in reval["change_summary"] if c["target"] == "header"]
assert "connection" not in chg_fields and "keep-alive" not in chg_fields and "transfer-encoding" not in chg_fields, chg_fields
assert len(reval["rules"][0]["skipped_actions"]) == 3, reval["rules"][0]
excl_hop = [k.lower() for k, _ in reval["after_summary"]["excluded_hop_by_hop_headers"]]
assert "keep-alive" in excl_hop, reval["after_summary"]["excluded_hop_by_hop_headers"]

assert echo["body_json"] == {
    "user": {"profile": {"name": "Alice", "role": "admin", "region": "日本"}},
    "items": [{"id": 2, "qty": 99}],
}, echo["body_json"]
cl_vals = [int(v) for k, v in hdr_pairs if k.lower() == "content-length"]
assert cl_vals == [echo["body_size"]], (cl_vals, echo["body_size"])
PY

echo "==> [4/10] Verifying URL-encoded form body duplicate key add/set/remove (AC-5)"
"${ROOT_DIR}/scripts/capture-ctl.sh" rule-create \
  --rule-id "rule:form-mut" \
  --name "Form Body Mutator" \
  --priority 20 \
  --domain "app-a.local.test" \
  --method "POST" \
  --path-prefix "/v1/forms" \
  --content-type "application/x-www-form-urlencoded" \
  --form-set "role@1=super-editor" \
  --form-remove "role@0" \
  --form-add "role=auditor" \
  --form-set "note=完了 & 確認" >/dev/null

form_hdr="/tmp/pc_dev040_form_hdr.txt"
form_echo="$(curl -fsS -D "${form_hdr}" \
  --cacert "${CACERT}" \
  --resolve "app-a.local.test:${EDGE_HTTPS_PORT}:127.0.0.1" \
  -X POST \
  -H "Content-Type: application/x-www-form-urlencoded" \
  --data-raw "role=user&role=editor&dept=eng&note=init" \
  "https://app-a.local.test:${EDGE_HTTPS_PORT}/v1/forms/submit")"
form_cap_id="$(awk 'tolower($1)=="x-capture-id:" {gsub(/\r/,"",$2); print $2}' "${form_hdr}")"
rm -f "${form_hdr}"

FORM_ECHO="${form_echo}" python3 - <<'PY'
import json, os
echo = json.loads(os.environ["FORM_ECHO"])
assert echo["form_params"] == [
    ["role", "super-editor"],
    ["dept", "eng"],
    ["note", "完了 & 確認"],
    ["role", "auditor"],
], echo["form_params"]
cl_vals = [int(v) for k, v in echo["headers"] if k.lower() == "content-length"]
assert cl_vals == [echo["body_size"]], (cl_vals, echo["body_size"])
PY

echo "==> [5/10] Verifying multi-rule priority ordering & deterministic sequential chaining (AC-7, Rule 1, Rule 8)"
# Create two rules matching /v1/chain:
# rule:chain-late (priority 30) created first
# rule:chain-early (priority 10) created second
# Priority 10 must execute BEFORE priority 30, and priority 30 must see priority 10's output!
curl -fsS -X POST "${API_BASE}/api/v1/rules" \
  -H "Content-Type: application/json" \
  -d '{
    "rule_id": "rule:chain-late",
    "name": "Chain Step 2 (Priority 30)",
    "priority": 30,
    "match": {
      "domain": "app-a.local.test",
      "path_exact": "/v1/chain",
      "header_pairs": [{"name": "X-Step-One", "value": "applied"}]
    },
    "actions": [
      {"target": "header", "op": "set", "name": "X-Conflict", "value": "winner-prio-30"},
      {"target": "query", "op": "add", "name": "order", "value": "second"},
      {"target": "json", "op": "set", "path": "chain.step2_saw_step1", "value": true}
    ]
  }' >/dev/null

curl -fsS -X POST "${API_BASE}/api/v1/rules" \
  -H "Content-Type: application/json" \
  -d '{
    "rule_id": "rule:chain-early",
    "name": "Chain Step 1 (Priority 10)",
    "priority": 10,
    "match": {
      "domain": "app-a.local.test",
      "path_exact": "/v1/chain"
    },
    "actions": [
      {"target": "header", "op": "set", "name": "X-Step-One", "value": "applied"},
      {"target": "header", "op": "set", "name": "X-Conflict", "value": "loser-prio-10"},
      {"target": "query", "op": "add", "name": "order", "value": "first"},
      {"target": "json", "op": "set", "path": "chain.step1", "value": "done"}
    ]
  }' >/dev/null

chain_hdr="/tmp/pc_dev040_chain_hdr.txt"
chain_echo="$(curl -fsS -D "${chain_hdr}" \
  --cacert "${CACERT}" \
  --resolve "app-a.local.test:${EDGE_HTTPS_PORT}:127.0.0.1" \
  -X POST \
  -H "Content-Type: application/json" \
  --data-raw '{"chain":{}}' \
  "https://app-a.local.test:${EDGE_HTTPS_PORT}/v1/chain")"
chain_cap_id="$(awk 'tolower($1)=="x-capture-id:" {gsub(/\r/,"",$2); print $2}' "${chain_hdr}")"
rm -f "${chain_hdr}"

chain_detail="$(curl -fsS "${API_BASE}/api/v1/captures/${chain_cap_id}")"
CHAIN_ECHO="${chain_echo}" CHAIN_DETAIL="${chain_detail}" python3 - <<'PY'
import json, os
echo = json.loads(os.environ["CHAIN_ECHO"])
det = json.loads(os.environ["CHAIN_DETAIL"])
hdr_map = {k.lower(): v for k, v in echo["headers"]}
assert hdr_map.get("x-step-one") == "applied", echo["headers"]
assert hdr_map.get("x-conflict") == "winner-prio-30", echo["headers"]
assert echo["query_params"] == [["order", "first"], ["order", "second"]], echo["query_params"]
assert echo["body_json"] == {"chain": {"step1": "done", "step2_saw_step1": True}}, echo["body_json"]

reval = det["rule_evaluation"]
assert reval["applied_rule_ids"] == ["rule:chain-early", "rule:chain-late"], reval
assert reval["rule_modified"] is True and reval["outcome"] == "applied", reval
PY

# 5b. Reverse rule chain regression: subsequent rule must match working state only, NOT original state before prior rule (Review 001 Finding 1)
curl -fsS -X POST "${API_BASE}/api/v1/rules" \
  -H "Content-Type: application/json" \
  -d '{
    "rule_id": "rule:rev-1",
    "name": "Reverse Chain 1 (Mutates old -> new)",
    "priority": 10,
    "match": {
      "domain": "app-a.local.test",
      "path_exact": "/v1/reverse-chain",
      "query_pairs": [{"name": "stage", "value": "old"}],
      "header_pairs": [{"name": "X-Stage", "value": "old"}]
    },
    "actions": [
      {"target": "query", "op": "set", "name": "stage", "value": "new"},
      {"target": "header", "op": "set", "name": "X-Stage", "value": "new"}
    ]
  }' >/dev/null

curl -fsS -X POST "${API_BASE}/api/v1/rules" \
  -H "Content-Type: application/json" \
  -d '{
    "rule_id": "rule:rev-2-stale",
    "name": "Reverse Chain 2 (Should NOT match original old after rev-1)",
    "priority": 20,
    "match": {
      "domain": "app-a.local.test",
      "path_exact": "/v1/reverse-chain",
      "query_pairs": [{"name": "stage", "value": "old"}]
    },
    "actions": [
      {"target": "header", "op": "set", "name": "X-Stale-Matched", "value": "bug"}
    ]
  }' >/dev/null

curl -fsS -X POST "${API_BASE}/api/v1/rules" \
  -H "Content-Type: application/json" \
  -d '{
    "rule_id": "rule:rev-3-stale-hdr",
    "name": "Reverse Chain 3 (Should NOT match original X-Stage: old after rev-1)",
    "priority": 25,
    "match": {
      "domain": "app-a.local.test",
      "path_exact": "/v1/reverse-chain",
      "header_pairs": [{"name": "X-Stage", "value": "old"}]
    },
    "actions": [
      {"target": "header", "op": "set", "name": "X-Stale-Hdr-Matched", "value": "bug"}
    ]
  }' >/dev/null

curl -fsS -X POST "${API_BASE}/api/v1/rules" \
  -H "Content-Type: application/json" \
  -d '{
    "rule_id": "rule:rev-4-working",
    "name": "Reverse Chain 4 (Matches working state new)",
    "priority": 30,
    "match": {
      "domain": "app-a.local.test",
      "path_exact": "/v1/reverse-chain",
      "query_pairs": [{"name": "stage", "value": "new"}],
      "header_pairs": [{"name": "X-Stage", "value": "new"}]
    },
    "actions": [
      {"target": "header", "op": "set", "name": "X-Chain-Verified", "value": "ok"}
    ]
  }' >/dev/null

rev_hdr="/tmp/pc_dev040_rev_hdr.txt"
rev_echo="$(curl -fsS -D "${rev_hdr}" \
  --cacert "${CACERT}" \
  --resolve "app-a.local.test:${EDGE_HTTPS_PORT}:127.0.0.1" \
  -H "X-Stage: old" \
  "https://app-a.local.test:${EDGE_HTTPS_PORT}/v1/reverse-chain?stage=old")"
rev_cap_id="$(awk 'tolower($1)=="x-capture-id:" {gsub(/\r/,"",$2); print $2}' "${rev_hdr}")"
rm -f "${rev_hdr}"
rev_detail="$(curl -fsS "${API_BASE}/api/v1/captures/${rev_cap_id}")"

REV_ECHO="${rev_echo}" REV_DET="${rev_detail}" python3 - <<'PY'
import json, os
recho = json.loads(os.environ["REV_ECHO"])
rdet = json.loads(os.environ["REV_DET"])
rhdrs = {k.lower(): v for k, v in recho["headers"]}
assert recho["query_params"] == [["stage", "new"]], recho["query_params"]
assert rhdrs.get("x-stage") == "new", recho["headers"]
assert rhdrs.get("x-chain-verified") == "ok", recho["headers"]
assert "x-stale-matched" not in rhdrs, recho["headers"]
assert "x-stale-hdr-matched" not in rhdrs, recho["headers"]
reval = rdet["rule_evaluation"]
assert reval["matched_rule_ids"] == ["rule:rev-1", "rule:rev-4-working"], reval
assert reval["applied_rule_ids"] == ["rule:rev-1", "rule:rev-4-working"], reval
PY

echo "==> [6/10] Verifying disabled rules and non-matching requests are untouched (AC-8, Rule 2)"
# Disable rule:chain-late and verify /v1/chain only applies rule:chain-early
curl -fsS -X POST "${API_BASE}/api/v1/rules/rule:chain-late/disable" -H "Content-Type: application/json" -d '{}' >/dev/null

chain_dis_echo="$(curl -fsS \
  --cacert "${CACERT}" \
  --resolve "app-a.local.test:${EDGE_HTTPS_PORT}:127.0.0.1" \
  -X POST \
  -H "Content-Type: application/json" \
  --data-raw '{"chain":{}}' \
  "https://app-a.local.test:${EDGE_HTTPS_PORT}/v1/chain")"

# Send request to app-b.local.test (no matching rules)
nomatch_hdr="/tmp/pc_dev040_nomatch_hdr.txt"
nomatch_echo="$(curl -fsS -D "${nomatch_hdr}" \
  --cacert "${CACERT}" \
  --resolve "app-b.local.test:${EDGE_HTTPS_PORT}:127.0.0.1" \
  "https://app-b.local.test:${EDGE_HTTPS_PORT}/v1/untouched?hello=1")"
nomatch_cap_id="$(awk 'tolower($1)=="x-capture-id:" {gsub(/\r/,"",$2); print $2}' "${nomatch_hdr}")"
rm -f "${nomatch_hdr}"
nomatch_detail="$(curl -fsS "${API_BASE}/api/v1/captures/${nomatch_cap_id}")"

CHAIN_DIS_ECHO="${chain_dis_echo}" NOMATCH_ECHO="${nomatch_echo}" NOMATCH_DETAIL="${nomatch_detail}" python3 - <<'PY'
import json, os
ce = json.loads(os.environ["CHAIN_DIS_ECHO"])
ne = json.loads(os.environ["NOMATCH_ECHO"])
nd = json.loads(os.environ["NOMATCH_DETAIL"])
ce_hdrs = {k.lower(): v for k, v in ce["headers"]}
assert ce_hdrs.get("x-conflict") == "loser-prio-10", ce["headers"]
assert ce["body_json"] == {"chain": {"step1": "done"}}, ce
assert ne["target"] == "echo-b" and ne["raw_query"] == "hello=1", ne
reval = nd["rule_evaluation"]
assert reval["rule_modified"] is False and reval["outcome"] == "no_match", reval
assert reval["applied_rule_ids"] == [], reval
PY

echo "==> [7/10] Verifying runtime fail-open on malformed JSON, out-of-bounds index & content-type mismatch (AC-10, Rule 7)"
curl -fsS -X POST "${API_BASE}/api/v1/rules" \
  -H "Content-Type: application/json" \
  -d '{
    "rule_id": "rule:fail-open-json",
    "name": "Fail-Open JSON Mutator",
    "priority": 15,
    "match": {
      "domain": "app-a.local.test",
      "path_prefix": "/v1/fail-open"
    },
    "actions": [
      {"target": "header", "op": "set", "name": "X-Should-Rollback", "value": "1"},
      {"target": "json", "op": "set", "path": "items[99].qty", "value": 10}
    ]
  }' >/dev/null

# 7a. Malformed JSON body -> fail-open delivers exact original malformed payload to echo-a and logs failure
malformed_hdr="/tmp/pc_dev040_malformed_hdr.txt"
malformed_echo="$(curl -fsS -D "${malformed_hdr}" \
  --cacert "${CACERT}" \
  --resolve "app-a.local.test:${EDGE_HTTPS_PORT}:127.0.0.1" \
  -X POST \
  -H "Content-Type: application/json" \
  --data-raw '{broken_json:' \
  "https://app-a.local.test:${EDGE_HTTPS_PORT}/v1/fail-open/malformed")"
malformed_cap_id="$(awk 'tolower($1)=="x-capture-id:" {gsub(/\r/,"",$2); print $2}' "${malformed_hdr}")"
rm -f "${malformed_hdr}"

# 7b. Valid JSON but out-of-bounds array index items[99] -> fail-open rolls back rule:fail-open-json (including X-Should-Rollback)
oob_hdr="/tmp/pc_dev040_oob_hdr.txt"
oob_echo="$(curl -fsS -D "${oob_hdr}" \
  --cacert "${CACERT}" \
  --resolve "app-a.local.test:${EDGE_HTTPS_PORT}:127.0.0.1" \
  -X POST \
  -H "Content-Type: application/json" \
  --data-raw '{"items":[{"qty":1}]}' \
  "https://app-a.local.test:${EDGE_HTTPS_PORT}/v1/fail-open/oob")"
oob_cap_id="$(awk 'tolower($1)=="x-capture-id:" {gsub(/\r/,"",$2); print $2}' "${oob_hdr}")"
rm -f "${oob_hdr}"

malformed_detail="$(curl -fsS "${API_BASE}/api/v1/captures/${malformed_cap_id}")"
oob_detail="$(curl -fsS "${API_BASE}/api/v1/captures/${oob_cap_id}")"

MAL_ECHO="${malformed_echo}" OOB_ECHO="${oob_echo}" MAL_DET="${malformed_detail}" OOB_DET="${oob_detail}" python3 - <<'PY'
import json, os
me = json.loads(os.environ["MAL_ECHO"])
oe = json.loads(os.environ["OOB_ECHO"])
md = json.loads(os.environ["MAL_DET"])
od = json.loads(os.environ["OOB_DET"])

assert me["body_text"] == "{broken_json:", me
me_hdrs = {k.lower(): v for k, v in me["headers"]}
assert "x-should-rollback" not in me_hdrs, me["headers"]
m_eval = md["rule_evaluation"]
assert m_eval["rule_failed"] is True and m_eval["rule_modified"] is False, m_eval
assert m_eval["failed_rule_ids"] == ["rule:fail-open-json"], m_eval
assert m_eval["rules"][0]["errors"][0]["code"] == "malformed_json_body", m_eval

assert oe["body_text"] == '{"items":[{"qty":1}]}', oe
oe_hdrs = {k.lower(): v for k, v in oe["headers"]}
assert "x-should-rollback" not in oe_hdrs, oe["headers"]
o_eval = od["rule_evaluation"]
assert o_eval["rule_failed"] is True and o_eval["rule_modified"] is False, o_eval
assert o_eval["failed_rule_ids"] == ["rule:fail-open-json"], o_eval
assert o_eval["rules"][0]["errors"][0]["code"] == "json_index_out_of_range", o_eval
PY

echo "==> [8/10] Verifying dry-run preview without sending traffic or increasing capture count (AC-11, Rule 3)"
before_index="$(curl -fsS "${API_BASE}/api/v1/index/status")"

# Preview against existing capture ID via HTTP API + candidate_rule
preview_cap_res="$(curl -fsS -X POST "${API_BASE}/api/v1/captures/${nomatch_cap_id}/rules-preview" \
  -H "Content-Type: application/json" \
  -d '{
    "candidate_rule": {
      "rule_id": "rule:candidate-preview",
      "name": "Preview Candidate",
      "priority": 5,
      "match": {"domain": "app-b.local.test", "path_prefix": "/v1/untouched"},
      "actions": [
        {"target": "query", "op": "set", "name": "hello", "value": "previewed"},
        {"target": "header", "op": "add", "name": "X-Preview-Only", "value": "true"}
      ]
    }
  }')"

# Preview synthetic request via CLI rule-preview
preview_cli_res="$("${ROOT_DIR}/scripts/capture-ctl.sh" rule-preview \
  --method POST \
  --authority "app-a.local.test:${EDGE_HTTPS_PORT}" \
  --raw-path "/v1/chain" \
  --content-type "application/json" \
  --body-text '{"chain":{}}')"

after_index="$(curl -fsS "${API_BASE}/api/v1/index/status")"

BEFORE_IDX="${before_index}" AFTER_IDX="${after_index}" PREV_CAP="${preview_cap_res}" PREV_CLI="${preview_cli_res}" python3 - <<'PY'
import json, os
b_idx = json.loads(os.environ["BEFORE_IDX"])
a_idx = json.loads(os.environ["AFTER_IDX"])
p_cap = json.loads(os.environ["PREV_CAP"])
p_cli = json.loads(os.environ["PREV_CLI"])

assert b_idx["indexed_captures"] == a_idx["indexed_captures"], (b_idx, a_idx)
assert b_idx["capture_files_on_disk"] == a_idx["capture_files_on_disk"], (b_idx, a_idx)

assert p_cap["dry_run"] is True and p_cap["sent_to_target"] is False, p_cap
assert p_cap["rule_evaluation"]["applied_rule_ids"] == ["rule:candidate-preview"], p_cap
assert p_cap["modified_request"]["raw_query"] == "hello=previewed", p_cap

assert p_cli["dry_run"] is True and p_cli["sent_to_target"] is False, p_cli
assert p_cli["rule_evaluation"]["applied_rule_ids"] == ["rule:chain-early"], p_cli
assert json.loads(p_cli["modified_request"]["body_text"]) == {"chain": {"step1": "done"}}, p_cli
PY

echo "==> [9/10] Verifying WebSocket handshake, multipart/form-data, streamed request & protected-header-only rule (AC-12, AC-13, AC-14, Review 001 Findings 2 & 3)"
curl -fsS -X POST "${API_BASE}/api/v1/rules" \
  -H "Content-Type: application/json" \
  -d '{
    "rule_id": "rule:broad-app-a",
    "name": "Broad App-A Rule",
    "priority": 90,
    "match": {
      "domain": "app-a.local.test",
      "path_prefix": "/v1/special"
    },
    "actions": [
      {"target": "json", "op": "set", "path": "cannot_apply", "value": 1}
    ]
  }' >/dev/null

# 9a. Multipart request matching rule:broad-app-a -> body not corrupted, rule marked failed/skipped
mp_hdr="/tmp/pc_dev040_mp_hdr.txt"
mp_echo="$(curl -fsS -D "${mp_hdr}" \
  --cacert "${CACERT}" \
  --resolve "app-a.local.test:${EDGE_HTTPS_PORT}:127.0.0.1" \
  -X POST \
  -F "field1=val1" \
  "https://app-a.local.test:${EDGE_HTTPS_PORT}/v1/special/multipart")"
mp_cap_id="$(awk 'tolower($1)=="x-capture-id:" {gsub(/\r/,"",$2); print $2}' "${mp_hdr}")"
rm -f "${mp_hdr}"

# 9b. WebSocket upgrade handshake matching rule:broad-app-a -> skipped_websocket, not corrupted
ws_cap_id="$(EDGE_PORT="${EDGE_HTTPS_PORT}" CERT_FILE="${CACERT}" python3 - <<'PY'
import base64, hashlib, json, os, socket, ssl

edge_port = int(os.environ["EDGE_PORT"])
cert_file = os.environ["CERT_FILE"]
app_a = "app-a.local.test"
ws_key = base64.b64encode(b"ProxyConsoleWS40").decode("ascii")

raw_sock = socket.create_connection(("127.0.0.1", edge_port), timeout=15)
ctx = ssl.create_default_context(cafile=cert_file)
tls_sock = ctx.wrap_socket(raw_sock, server_hostname=app_a)

handshake_req = (
    f"GET /v1/special/ws?mode=realtime HTTP/1.1\r\n"
    f"Host: {app_a}:{edge_port}\r\n"
    f"Upgrade: websocket\r\n"
    f"Connection: Upgrade\r\n"
    f"Sec-WebSocket-Key: {ws_key}\r\n"
    f"Sec-WebSocket-Version: 13\r\n"
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
assert " 101 " in header_lines[0], f"Expected 101, got: {header_lines[0]}"

resp_headers = {}
for line in header_lines[1:]:
    if ":" in line:
        k, v = line.split(":", 1)
        resp_headers[k.strip().lower()] = v.strip()

ws_capture_id = resp_headers.get("x-capture-id")
assert ws_capture_id, resp_headers

# Send masked text frame & read echo to verify WebSocket connection works
payload = b"hello-ws-rule-skip"
mask_key = b"\x12\x34\x56\x78"
masked = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))
tls_sock.sendall(bytes([0x81, 0x80 | len(payload)]) + mask_key + masked)

frame_data = leftover
while len(frame_data) < 2:
    frame_data += tls_sock.recv(4096)
f_len = frame_data[1] & 0x7F
while len(frame_data) < 2 + f_len:
    frame_data += tls_sock.recv(4096)
echo_payload = json.loads(frame_data[2 : 2 + f_len].decode("utf-8"))
assert echo_payload["ws_echo"] == "hello-ws-rule-skip", echo_payload

tls_sock.sendall(bytes([0x88, 0x80]) + mask_key)
tls_sock.close()
print(ws_capture_id)
PY
)"

# 9c. Streamed request (> 1 MiB CAPTURE_MAX_BODY_BYTES) matching rule with non-body & body actions -> skipped consistently (Review 001 Finding 2)
curl -fsS -X POST "${API_BASE}/api/v1/rules" \
  -H "Content-Type: application/json" \
  -d '{
    "rule_id": "rule:streamed-nonbody",
    "name": "Streamed Request Non-Body Rule",
    "priority": 5,
    "match": {
      "domain": "app-a.local.test",
      "path_exact": "/v1/streamed-test"
    },
    "actions": [
      {"target": "query", "op": "set", "name": "q", "value": "should-not-apply"},
      {"target": "header", "op": "set", "name": "X-Stream-Mut", "value": "should-not-apply"},
      {"target": "cookie", "op": "set", "name": "sid", "value": "should-not-apply"}
    ]
  }' >/dev/null

stream_body_file="/tmp/pc_dev040_stream_body.bin"
python3 -c 'from pathlib import Path; Path("/tmp/pc_dev040_stream_body.bin").write_bytes(b"S" * (1024 * 1024 + 65536))'
stream_hdr="/tmp/pc_dev040_stream_hdr.txt"
stream_echo="$(curl -fsS -D "${stream_hdr}" \
  --cacert "${CACERT}" \
  --resolve "app-a.local.test:${EDGE_HTTPS_PORT}:127.0.0.1" \
  -X POST \
  -H "Content-Type: application/octet-stream" \
  -H "X-Stream-Orig: keep-me" \
  --data-binary "@${stream_body_file}" \
  "https://app-a.local.test:${EDGE_HTTPS_PORT}/v1/streamed-test?q=orig")"
stream_cap_id="$(awk 'tolower($1)=="x-capture-id:" {gsub(/\r/,"",$2); print $2}' "${stream_hdr}")"
rm -f "${stream_hdr}" "${stream_body_file}"

# 9d. Rule that ONLY manipulates protected headers (Host, Content-Length, hop-by-hop, Connection-token) -> skipped, modified=False, change_summary=[] (Review 001 Finding 3)
curl -fsS -X POST "${API_BASE}/api/v1/rules" \
  -H "Content-Type: application/json" \
  -d '{
    "rule_id": "rule:protected-only",
    "name": "Protected Headers Only Rule",
    "priority": 5,
    "match": {
      "domain": "app-a.local.test",
      "path_exact": "/v1/protected-only"
    },
    "actions": [
      {"target": "header", "op": "set", "name": "Host", "value": "evil.local.test"},
      {"target": "header", "op": "set", "name": "Content-Length", "value": "99999"},
      {"target": "header", "op": "add", "name": "Keep-Alive", "value": "timeout=10"},
      {"target": "header", "op": "add", "name": "Transfer-Encoding", "value": "chunked"},
      {"target": "header", "op": "add", "name": "Connection", "value": "keep-alive"}
    ]
  }' >/dev/null

prot_hdr="/tmp/pc_dev040_prot_hdr.txt"
prot_echo="$(curl -fsS -D "${prot_hdr}" \
  --cacert "${CACERT}" \
  --resolve "app-a.local.test:${EDGE_HTTPS_PORT}:127.0.0.1" \
  -X POST \
  -H "Content-Type: text/plain" \
  --data-raw "hello-protected" \
  "https://app-a.local.test:${EDGE_HTTPS_PORT}/v1/protected-only")"
prot_cap_id="$(awk 'tolower($1)=="x-capture-id:" {gsub(/\r/,"",$2); print $2}' "${prot_hdr}")"
rm -f "${prot_hdr}"

prot_preview="$(curl -fsS -X POST "${API_BASE}/api/v1/rules/preview" \
  -H "Content-Type: application/json" \
  -d '{
    "request": {
      "method": "POST",
      "authority": "app-a.local.test:8443",
      "path": "/v1/protected-only",
      "headers": [
        ["Connection", "keep-alive, X-Hop-Token"],
        ["X-Hop-Token", "initial"]
      ],
      "body_text": "hello-protected"
    },
    "rules": [
      {
        "rule_id": "rule:preview-prot-token",
        "priority": 1,
        "match": {"path_exact": "/v1/protected-only"},
        "actions": [
          {"target": "header", "op": "set", "name": "Host", "value": "evil.local.test"},
          {"target": "header", "op": "set", "name": "X-Hop-Token", "value": "mutated-hop"}
        ]
      }
    ]
  }')"

mp_detail="$(curl -fsS "${API_BASE}/api/v1/captures/${mp_cap_id}")"
ws_detail="$(curl -fsS "${API_BASE}/api/v1/captures/${ws_cap_id}")"
stream_detail="$(curl -fsS "${API_BASE}/api/v1/captures/${stream_cap_id}")"
prot_detail="$(curl -fsS "${API_BASE}/api/v1/captures/${prot_cap_id}")"

MP_ECHO="${mp_echo}" MP_DET="${mp_detail}" WS_DET="${ws_detail}" \
ST_ECHO="${stream_echo}" ST_DET="${stream_detail}" \
PR_ECHO="${prot_echo}" PR_DET="${prot_detail}" PR_PREV="${prot_preview}" python3 - <<'PY'
import json, os
me = json.loads(os.environ["MP_ECHO"])
md = json.loads(os.environ["MP_DET"])
wd = json.loads(os.environ["WS_DET"])
se = json.loads(os.environ["ST_ECHO"])
sd = json.loads(os.environ["ST_DET"])
pe = json.loads(os.environ["PR_ECHO"])
pd = json.loads(os.environ["PR_DET"])
pp = json.loads(os.environ["PR_PREV"])

assert "form-data; name=\"field1\"" in me["body_text"], me["body_text"]
assert md["rule_evaluation"]["rule_failed"] is True, md["rule_evaluation"]
assert md["rule_evaluation"]["rules"][0]["errors"][0]["code"] == "unsupported_body_type", md["rule_evaluation"]

assert wd["is_websocket"] is True, wd
assert wd["rule_evaluation"]["outcome"] == "skipped_websocket", wd["rule_evaluation"]
assert wd["rule_evaluation"]["skipped_rule_ids"] == ["rule:broad-app-a"], wd["rule_evaluation"]

# Streamed request assertions: target receives unmodified query/headers/body, metadata shows skipped & modified=False
assert se["raw_query"] == "q=orig", se
se_hdrs = {k.lower(): v for k, v in se["headers"]}
assert se_hdrs.get("x-stream-orig") == "keep-me", se["headers"]
assert "x-stream-mut" not in se_hdrs, se["headers"]
assert se["body_size"] == 1024 * 1024 + 65536, se["body_size"]
s_eval = sd["rule_evaluation"]
assert s_eval["modified"] is False and s_eval["rule_modified"] is False, s_eval
assert s_eval["outcome"] == "skipped", s_eval
assert s_eval["matched_rule_ids"] == ["rule:streamed-nonbody"], s_eval
assert s_eval["applied_rule_ids"] == [], s_eval
assert s_eval["skipped_rule_ids"] == ["rule:streamed-nonbody"], s_eval
assert s_eval["change_summary"] == [] and s_eval["change_count"] == 0, s_eval
assert s_eval["rules"][0]["reason"] == "streamed_request", s_eval

# Protected-header-only rule assertions: target receives original Host & Content-Length, metadata shows skipped & modified=False
pe_hdrs = {k.lower(): v for k, v in pe["headers"]}
assert pe["host"] == "app-a.local.test:8443", pe
assert pe_hdrs.get("content-length") == str(len("hello-protected")), pe["headers"]
p_eval = pd["rule_evaluation"]
assert p_eval["modified"] is False and p_eval["rule_modified"] is False, p_eval
assert p_eval["outcome"] == "skipped", p_eval
assert p_eval["matched_rule_ids"] == ["rule:protected-only"], p_eval
assert p_eval["applied_rule_ids"] == [], p_eval
assert p_eval["skipped_rule_ids"] == ["rule:protected-only"], p_eval
assert p_eval["change_summary"] == [] and p_eval["change_count"] == 0, p_eval
assert p_eval["rules"][0]["reason"] == "protected_header", p_eval
assert len(p_eval["rules"][0]["skipped_actions"]) == 5, p_eval

# Preview assertions for Connection-nominated token + Host
assert pp["modified"] is False and pp["outcome"] == "skipped", pp
assert pp["applied_rule_ids"] == [], pp
assert pp["skipped_rule_ids"] == ["rule:preview-prot-token"], pp
assert pp["change_summary"] == [], pp
assert pp["rules"][0]["reason"] == "protected_header", pp
PY

echo "==> [10/10] Verifying capture search filters & index rebuild preservation for rules (AC-13, Rule 10)"
search_applied="$(curl -fsS "${API_BASE}/api/v1/captures?applied_rule_id=rule:composite-json")"
search_failed="$("${ROOT_DIR}/scripts/capture-ctl.sh" search --failed-rule-id "rule:fail-open-json")"
search_mod_only="$(curl -fsS "${API_BASE}/api/v1/captures?rule_modified=true")"
search_skipped="$(curl -fsS "${API_BASE}/api/v1/captures?skipped_rule_id=rule:broad-app-a")"

# Rebuild index and verify rules table + capture rule metadata remain intact
curl -fsS -X POST "${API_BASE}/api/v1/index/rebuild" -H "Content-Type: application/json" -d '{}' >/dev/null
rules_after_rebuild="$(curl -fsS "${API_BASE}/api/v1/rules")"
search_applied_after="$(curl -fsS "${API_BASE}/api/v1/captures?applied_rule_id=rule:composite-json")"

S_APP="${search_applied}" S_FAIL="${search_failed}" S_MOD="${search_mod_only}" S_SKIP="${search_skipped}" \
R_REB="${rules_after_rebuild}" S_APP_AFTER="${search_applied_after}" \
JSON_CAP_ID="${json_cap_id}" MAL_CAP_ID="${malformed_cap_id}" OOB_CAP_ID="${oob_cap_id}" WS_CAP_ID="${ws_cap_id}" python3 - <<'PY'
import json, os
s_app = json.loads(os.environ["S_APP"])
s_fail = json.loads(os.environ["S_FAIL"])
s_mod = json.loads(os.environ["S_MOD"])
s_skip = json.loads(os.environ["S_SKIP"])
r_reb = json.loads(os.environ["R_REB"])
s_app_after = json.loads(os.environ["S_APP_AFTER"])

app_ids = [it["capture_id"] for it in s_app["items"]]
assert os.environ["JSON_CAP_ID"] in app_ids, app_ids

fail_ids = [it["capture_id"] for it in s_fail["items"]]
assert os.environ["MAL_CAP_ID"] in fail_ids and os.environ["OOB_CAP_ID"] in fail_ids, fail_ids

for it in s_mod["items"]:
    assert it["rule_modified"] is True, it
    assert len(it["applied_rule_ids"]) >= 1, it

skip_ids = [it["capture_id"] for it in s_skip["items"]]
assert os.environ["WS_CAP_ID"] in skip_ids, skip_ids

assert r_reb["total"] >= 4, r_reb
app_ids_after = [it["capture_id"] for it in s_app_after["items"]]
assert app_ids_after == app_ids, (app_ids_after, app_ids)
PY

echo "ALL DEV-040 AUTOMATIC REQUEST MODIFICATION RULE TESTS PASSED!"
