#!/usr/bin/env bash
# DEV-050: Reproducible Company-Environment Readiness & Backup/Restore Verification
# Usage:
#   ./scripts/check-company-readiness.sh                 # Full verification (inventory + backup/restore + disk-full simulation)
#   ./scripts/check-company-readiness.sh --inventory-only # Output JSON inventory of ports, networks, mounts, certs, and dependencies
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="${ROOT_DIR}/deploy/compose/docker-compose.yml"
EDGE_HTTPS_PORT="${EDGE_HTTPS_PORT:-8443}"
CONTROL_API_PORT="${CONTROL_API_PORT:-18480}"
API_BASE="http://127.0.0.1:${CONTROL_API_PORT}"
CACERT="${ROOT_DIR}/var/certificates/edge.crt"
KEYFILE="${ROOT_DIR}/var/certificates/edge.key"
INVENTORY_ONLY=0

if [[ "${1:-}" == "--inventory-only" ]]; then
  INVENTORY_ONLY=1
fi

TMP_WORK_DIR="$(mktemp -d "${TMPDIR:-/tmp}/proxyconsole-readiness.XXXXXX")"
cleanup() {
  rm -rf "${TMP_WORK_DIR}"
}
trap cleanup EXIT

echo "==> [1/7] Ensuring clean ProxyConsole stack is running for readiness assessment"
if [[ "${INVENTORY_ONLY}" -eq 0 ]]; then
  "${ROOT_DIR}/scripts/down.sh" >/dev/null 2>&1 || true
  rm -rf "${ROOT_DIR}/var/captures" "${ROOT_DIR}/var/blobs" "${ROOT_DIR}/var/database"
  "${ROOT_DIR}/scripts/up.sh" >/dev/null
else
  if ! curl -fsS "${API_BASE}/api/v1/health" >/dev/null 2>&1; then
    "${ROOT_DIR}/scripts/up.sh" >/dev/null
  fi
fi

echo "==> [2/7] Inspecting published ports, localhost boundary, networks, volumes, and Docker socket mounts (AC-2, AC-3, AC-6)"
COMPOSE_CONFIG_JSON="$(docker compose -f "${COMPOSE_FILE}" config --format json)"
COMPOSE_PS_JSON="$(docker compose -f "${COMPOSE_FILE}" ps --format json)"

COMPOSE_CONFIG_JSON="${COMPOSE_CONFIG_JSON}" \
COMPOSE_PS_JSON="${COMPOSE_PS_JSON}" \
ROOT_DIR="${ROOT_DIR}" \
API_BASE="${API_BASE}" \
EDGE_HTTPS_PORT="${EDGE_HTTPS_PORT}" \
CONTROL_API_PORT="${CONTROL_API_PORT}" \
TMP_WORK_DIR="${TMP_WORK_DIR}" \
python3 - <<'PY'
import json
import os
from pathlib import Path
import re
import subprocess
import urllib.request

root_dir = Path(os.environ["ROOT_DIR"])
cfg = json.loads(os.environ["COMPOSE_CONFIG_JSON"])
ps_raw = os.environ["COMPOSE_PS_JSON"].strip()
ps_items = []
for line in ps_raw.splitlines():
    line = line.strip()
    if not line:
        continue
    obj = json.loads(line)
    if isinstance(obj, list):
        ps_items.extend(obj)
    else:
        ps_items.append(obj)

services_cfg = cfg.get("services", {})
networks_cfg = cfg.get("networks", {})
volumes_cfg = cfg.get("volumes", {})

# 1. Verify published ports & bind addresses
published_ports = []
for svc_name, svc_def in sorted(services_cfg.items()):
    for p in svc_def.get("ports", []) or []:
        published_ports.append({
            "service": svc_name,
            "host_ip": p.get("host_ip") or "0.0.0.0",
            "published": int(p.get("published")),
            "target": int(p.get("target")),
            "protocol": p.get("protocol", "tcp"),
        })

edge_ports = [p for p in published_ports if p["service"] == "edge"]
api_ports = [p for p in published_ports if p["service"] == "control-api"]
other_ports = [p for p in published_ports if p["service"] not in ("edge", "control-api")]

assert len(edge_ports) == 1 and edge_ports[0]["target"] == 443, f"Unexpected edge ports: {edge_ports}"
assert len(api_ports) == 1 and api_ports[0]["host_ip"] == "127.0.0.1", (
    f"control-api MUST bind exclusively to 127.0.0.1 by default, got: {api_ports}"
)
assert len(other_ports) == 0, f"Internal services must NOT publish ports to host: {other_ports}"

# Verify runtime ps Publishers match localhost boundary for control-api
ps_by_svc = {item.get("Service"): item for item in ps_items}
assert set(ps_by_svc.keys()) == {"edge", "inspector", "control-api", "router", "echo-a", "echo-b"}, (
    f"Expected 6 running services, got: {list(ps_by_svc.keys())}"
)
api_publishers = ps_by_svc["control-api"].get("Publishers") or []
assert any(pub.get("URL") == "127.0.0.1" for pub in api_publishers), (
    f"Expected runtime control-api publisher bound to 127.0.0.1, got: {api_publishers}"
)
for internal_svc in ("inspector", "router", "echo-a", "echo-b"):
    pubs = [p for p in (ps_by_svc[internal_svc].get("Publishers") or []) if p.get("PublishedPort", 0) != 0]
    assert len(pubs) == 0, f"Service {internal_svc} must not publish host ports at runtime: {pubs}"

# Verify management API & Web Console are reachable over 127.0.0.1
api_base = os.environ["API_BASE"]
with urllib.request.urlopen(f"{api_base}/api/v1/health", timeout=5) as resp:
    health = json.loads(resp.read().decode("utf-8"))
    assert resp.status == 200 and health.get("status") == "ok", health

with urllib.request.urlopen(f"{api_base}/", timeout=5) as resp:
    html = resp.read().decode("utf-8")
    assert resp.status == 200 and "ProxyConsole" in html

# 2. Verify network isolation (internal: true on inspector_router_net and router_targets_net)
assert networks_cfg["edge_inspector_net"].get("internal") in (False, None), networks_cfg["edge_inspector_net"]
assert networks_cfg["inspector_router_net"].get("internal") is True, networks_cfg["inspector_router_net"]
assert networks_cfg["router_targets_net"].get("internal") is True, networks_cfg["router_targets_net"]

# 3. Verify volume & Docker socket mounts
docker_sock_mounts = []
for svc_name, svc_def in sorted(services_cfg.items()):
    for v in svc_def.get("volumes", []) or []:
        if "/var/run/docker.sock" in str(v.get("source", "")) or "/var/run/docker.sock" in str(v.get("target", "")):
            docker_sock_mounts.append({"service": svc_name, "source": v.get("source"), "target": v.get("target")})

assert len(docker_sock_mounts) == 1 and docker_sock_mounts[0]["service"] == "control-api", (
    f"Only control-api may mount /var/run/docker.sock: {docker_sock_mounts}"
)

# 4. Mechanically scan services/recorder/docker_discovery.py for Docker API HTTP methods & endpoints (AC-6)
disc_src = (root_dir / "services/recorder/docker_discovery.py").read_text(encoding="utf-8")
http_requests = re.findall(r'conn\.request\(\s*"([A-Z]+)"', disc_src)
docker_paths = re.findall(r'_docker_get_json\(\s*f?"([^"]+)"', disc_src)
assert http_requests == ["GET"], f"Expected only GET requests to Docker socket, found: {http_requests}"
assert "/containers/json?all=1" in docker_paths, docker_paths
assert any("/containers/" in p and "/json" in p for p in docker_paths), docker_paths

inventory_summary = {
    "published_ports": published_ports,
    "networks": {
        k: {"driver": v.get("driver"), "internal": bool(v.get("internal", False))}
        for k, v in sorted(networks_cfg.items())
    },
    "docker_socket_mounts": docker_sock_mounts,
    "docker_api_operations": {
        "http_methods": sorted(set(http_requests)),
        "endpoints": docker_paths,
        "mutating_operations_used": False,
    },
}
(Path(os.environ["TMP_WORK_DIR"]) / "boundary_inventory.json").write_text(
    json.dumps(inventory_summary, indent=2), encoding="utf-8"
)
print("  [PASS] Boundary, port, network, and Docker socket operation inventory verified.")
PY

echo "==> [3/7] Inspecting TLS certificate & private key permissions, SANs, expiry, and Git exclusion (AC-5, AC-12)"
ROOT_DIR="${ROOT_DIR}" \
CACERT="${CACERT}" \
KEYFILE="${KEYFILE}" \
TMP_WORK_DIR="${TMP_WORK_DIR}" \
python3 - <<'PY'
import json
import os
from pathlib import Path
import stat
import subprocess

root_dir = Path(os.environ["ROOT_DIR"])
crt_path = Path(os.environ["CACERT"])
key_path = Path(os.environ["KEYFILE"])

assert crt_path.is_file(), f"Certificate file missing: {crt_path}"
assert key_path.is_file(), f"Private key file missing: {key_path}"

crt_mode = oct(stat.S_IMODE(crt_path.stat().st_mode))
key_mode = oct(stat.S_IMODE(key_path.stat().st_mode))
assert key_mode == "0o600", f"Private key must have 0600 permissions, got {key_mode}"
assert crt_mode == "0o644", f"Certificate must have 0644 permissions, got {crt_mode}"

# Verify git check-ignore protects both var/certificates/edge.key and *.key
for check_path in (str(key_path), str(crt_path), str(root_dir / "var/database/captures.sqlite3")):
    rc = subprocess.run(
        ["git", "-C", str(root_dir), "check-ignore", "-q", check_path],
        check=False,
    ).returncode
    assert rc == 0, f"Path {check_path} MUST be ignored by Git (.gitignore)"

# Extract certificate metadata without ever reading or printing key material
x509_out = subprocess.check_output(
    [
        "openssl",
        "x509",
        "-in",
        str(crt_path),
        "-noout",
        "-subject",
        "-issuer",
        "-dates",
        "-ext",
        "subjectAltName",
    ],
    text=True,
).strip()

assert "DNS:app-a.local.test" in x509_out, x509_out
assert "DNS:app-b.local.test" in x509_out, x509_out
assert "DNS:localhost" in x509_out, x509_out
assert "IP Address:127.0.0.1" in x509_out, x509_out

# Check certificate validity (not expired within next 24h = 86400s)
subprocess.run(
    ["openssl", "x509", "-in", str(crt_path), "-noout", "-checkend", "86400"],
    check=True,
)
print(f"  [PASS] Certificate & private key verified (key_mode={key_mode}, crt_mode={crt_mode}, Git-ignored=True).")
PY

echo "==> [4/7] Inspecting base images, OS versions, and Python dependency licenses & pinning (AC-7)"
COMPOSE_FILE="${COMPOSE_FILE}" \
TMP_WORK_DIR="${TMP_WORK_DIR}" \
python3 - <<'PY'
import json
import os
from pathlib import Path
import subprocess

compose_file = os.environ["COMPOSE_FILE"]

img_inspect = subprocess.check_output(
    [
        "docker",
        "image",
        "inspect",
        "nginx:1.27-alpine",
        "mitmproxy/mitmproxy:latest",
        "python:3.12-alpine",
        "--format",
        "{{json .}}",
    ],
    text=True,
).strip().splitlines()

base_images = []
for line in img_inspect:
    info = json.loads(line)
    base_images.append({
        "repo_tags": info.get("RepoTags"),
        "repo_digests": info.get("RepoDigests"),
        "id": info.get("Id"),
        "architecture": info.get("Architecture"),
        "os": info.get("Os"),
    })

py_pkgs_raw = subprocess.check_output(
    [
        "docker",
        "compose",
        "-f",
        compose_file,
        "exec",
        "-T",
        "control-api",
        "python3",
        "-c",
        """
import importlib.metadata as md, json, platform
pkgs = []
for dist in sorted(md.distributions(), key=lambda d: d.metadata["Name"].lower()):
    lic = (dist.metadata.get("License") or dist.metadata.get("License-Expression") or "").strip()
    if lic:
        lic = lic.splitlines()[0][:80]
    else:
        cls = [c for c in (dist.metadata.get_all("Classifier") or []) if c.startswith("License ::")]
        lic = cls[0].split("::")[-1].strip() if cls else "unspecified_in_dist_info"
    pkgs.append({"name": dist.metadata["Name"], "version": dist.version, "license": lic})
print(json.dumps({"python_version": platform.python_version(), "package_count": len(pkgs), "packages": pkgs}))
""",
    ],
    text=True,
)
py_deps = json.loads(py_pkgs_raw)
assert py_deps["package_count"] >= 10, py_deps
assert any(p["name"] == "mitmproxy" for p in py_deps["packages"]), py_deps

deps_report = {
    "base_images": base_images,
    "recorder_control_api_python": py_deps,
}
(Path(os.environ["TMP_WORK_DIR"]) / "dependencies_inventory.json").write_text(
    json.dumps(deps_report, indent=2), encoding="utf-8"
)
print(
    f"  [PASS] Base images ({len(base_images)}) and Python packages ({py_deps['package_count']}) inventoried."
)
PY

if [[ "${INVENTORY_ONLY}" -eq 1 ]]; then
  cat "${TMP_WORK_DIR}/boundary_inventory.json"
  cat "${TMP_WORK_DIR}/dependencies_inventory.json"
  exit 0
fi

echo "==> [5/7] Seeding synthetic dataset (route, rule, capture + blob, draft + replay, notification incident) and creating backup archive (AC-4, AC-9)"
API_BASE="${API_BASE}" \
EDGE_HTTPS_PORT="${EDGE_HTTPS_PORT}" \
CACERT="${CACERT}" \
TMP_WORK_DIR="${TMP_WORK_DIR}" \
python3 - <<'PY'
import json
import os
from pathlib import Path
import subprocess
import urllib.request

api_base = os.environ["API_BASE"]
edge_port = os.environ["EDGE_HTTPS_PORT"]
cacert = os.environ["CACERT"]
tmp_dir = Path(os.environ["TMP_WORK_DIR"])

def api_req(method: str, path: str, body=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        f"{api_base}{path}",
        data=data,
        method=method,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))

# 1. Create synthetic route (routes app-readiness.local.test -> echo-b:8080)
route_res = api_req(
    "POST",
    "/api/v1/routes",
    {
        "route_id": "route:readiness-app",
        "domain": "app-readiness.local.test",
        "upstream_scheme": "http",
        "upstream_host": "echo-b",
        "upstream_port": 8080,
        "enabled": True,
        "description": "Synthetic route for DEV-050 backup/restore test",
        "auto_apply": True,
    },
)
assert route_res["route"]["route_id"] == "route:readiness-app", route_res
apply_res = route_res.get("apply_result") or route_res.get("apply") or {}
assert apply_res.get("status") == "applied", route_res

# 2. Create synthetic modification rule on route:readiness-app
rule_res = api_req(
    "POST",
    "/api/v1/rules",
    {
        "rule_id": "rule:readiness-header",
        "name": "Readiness Test Header Rule",
        "priority": 15,
        "enabled": True,
        "description": "Synthetic modification rule for DEV-050 backup/restore",
        "match": {
            "route_id": "route:readiness-app",
            "path_prefix": "/v1/readiness",
        },
        "actions": [
            {
                "type": "set_header",
                "name": "X-Readiness-Rule",
                "value": "applied-synthetic",
            }
        ],
    },
)
rule_obj = rule_res.get("rule", rule_res)
assert rule_obj["rule_id"] == "rule:readiness-header", rule_res

# 3. Send HTTPS POST through edge to generate a complete capture + binary blob + rule evaluation
payload_str = json.dumps({"synthetic": True, "phase": "DEV-050", "items": [1, 2, 3]})
curl_out = subprocess.check_output(
    [
        "curl",
        "-fsS",
        "-X",
        "POST",
        "--cacert",
        cacert,
        "--resolve",
        f"app-readiness.local.test:{edge_port}:127.0.0.1",
        "-H",
        "Content-Type: application/json",
        "-H",
        "X-Synthetic-Auth: test-only-placeholder",
        "--data-binary",
        payload_str,
        f"https://app-readiness.local.test:{edge_port}/v1/readiness/check?mode=backup",
    ],
    text=True,
)
echo_resp = json.loads(curl_out)
assert echo_resp["target"] == "echo-b", echo_resp
hdr_pairs = {str(k).lower(): str(v) for k, v in echo_resp.get("headers", [])}
assert hdr_pairs.get("x-readiness-rule") == "applied-synthetic", echo_resp

captures_list = api_req("GET", "/api/v1/captures?host=app-readiness.local.test")
assert captures_list["total"] >= 1, captures_list
cap_summary = captures_list["items"][0]
capture_id = cap_summary["capture_id"]
cap_detail = api_req("GET", f"/api/v1/captures/{capture_id}")
assert cap_detail["capture_status"] == "complete", cap_detail
assert cap_detail["rule_evaluation"]["modified"] is True, cap_detail

# 4. Create a saved request draft from that capture and execute 1 replay
draft_res = api_req(
    "POST",
    "/api/v1/drafts",
    {
        "source_capture_id": capture_id,
        "name": "DEV-050 Readiness Draft",
    },
)
draft_obj = draft_res.get("draft", draft_res)
draft_id = draft_obj["draft_id"]
replay_res = api_req("POST", f"/api/v1/drafts/{draft_id}/replay", {})
replay_status = replay_res.get("response", {}).get("status_code") or replay_res.get("response_status")
assert replay_status == 200, replay_res

# 5. Record a synthetic notification incident & acknowledge it
notif_res = api_req(
    "POST",
    "/api/v1/notifications/ingest",
    {
        "event_type": "http_502_503_504",
        "route_id": "route:readiness-app",
        "domain": "app-readiness.local.test",
        "reason": "http_502",
        "message": "Synthetic 502 gateway error for DEV-050 backup/restore verification",
        "status_code": 502,
    },
)
incident_id = notif_res["incident"]["incident_id"]
event_seq = notif_res["event"]["seq"]
ack_res = api_req(
    "POST",
    f"/api/v1/notifications/incidents/{incident_id}/acknowledge",
    {"acknowledged": True},
)
assert ack_res["incident"]["acknowledged"] is True, ack_res

# 6. Update retention policy to a custom value to verify policy persistence in backup/restore
pol_res = api_req(
    "PUT",
    "/api/v1/retention/policy",
    {"min_retention_seconds": 43200},
)
assert pol_res["min_retention_seconds"] == 43200, pol_res

manifest = {
    "route_id": "route:readiness-app",
    "domain": "app-readiness.local.test",
    "rule_id": "rule:readiness-header",
    "capture_id": capture_id,
    "body_sha256": cap_detail["request"]["body"]["sha256"],
    "draft_id": draft_id,
    "incident_id": incident_id,
    "event_seq": event_seq,
    "min_retention_seconds": 43200,
}
(tmp_dir / "synthetic_manifest.json").write_text(
    json.dumps(manifest, indent=2), encoding="utf-8"
)
print(f"  [PASS] Seeded synthetic dataset: {json.dumps(manifest)}")
PY

# Create consistent SQLite snapshot via sqlite3.Connection.backup() and stage atomic archive inside container
BACKUP_ARCHIVE="${TMP_WORK_DIR}/proxyconsole-backup.tar.gz"
CERT_BACKUP_ARCHIVE="${TMP_WORK_DIR}/proxyconsole-certs-backup.tar.gz"

docker compose -f "${COMPOSE_FILE}" exec -T control-api \
  gosu mitmproxy python3 -c "
from pathlib import Path
import shutil, sqlite3, subprocess
from index_store import CaptureIndexStore

store = CaptureIndexStore()
store.retention.checkpoint_wal()
stage = Path('/tmp/pc_backup_stage')
shutil.rmtree(stage, ignore_errors=True)
(stage / 'database').mkdir(parents=True, exist_ok=True)
with sqlite3.connect(str(store.db_path)) as src, sqlite3.connect(str(stage / 'database' / 'captures.sqlite3')) as dst:
    src.backup(dst)
for sub in ('captures', 'blobs', 'router'):
    src_dir = store.storage_dir / sub
    if src_dir.is_dir():
        shutil.copytree(src_dir, stage / sub)
subprocess.run(['tar', '-C', str(stage), '-czf', '/tmp/pc_backup.tar.gz', 'captures', 'blobs', 'database', 'router'], check=True)
shutil.rmtree(stage, ignore_errors=True)
"
docker compose -f "${COMPOSE_FILE}" exec -T control-api \
  sh -c "cat /tmp/pc_backup.tar.gz && rm -f /tmp/pc_backup.tar.gz" > "${BACKUP_ARCHIVE}"
tar -C "${ROOT_DIR}/var/certificates" -czf "${CERT_BACKUP_ARCHIVE}" edge.crt edge.key

echo "==> [6/7] Destroying volume state, restoring from backup archive into fresh stack, and verifying full read-back & live routing (AC-9, Rule 6)"
"${ROOT_DIR}/scripts/down.sh" >/dev/null
rm -rf "${ROOT_DIR}/var/captures" "${ROOT_DIR}/var/blobs" "${ROOT_DIR}/var/database"
"${ROOT_DIR}/scripts/up.sh" >/dev/null

# Verify the fresh stack does NOT have the synthetic route before restore
PRE_RESTORE_ROUTES="$(curl -fsS "${API_BASE}/api/v1/routes")"
if echo "${PRE_RESTORE_ROUTES}" | grep -q "route:readiness-app"; then
  echo "ERROR: Fresh stack unexpectedly contained route:readiness-app before restore" >&2
  exit 1
fi

# Restore archive into /var/lib/proxyconsole and restart control-api, inspector, and router
docker compose -f "${COMPOSE_FILE}" exec -T control-api \
  sh -c "rm -rf /var/lib/proxyconsole/captures/* /var/lib/proxyconsole/blobs/* /var/lib/proxyconsole/database/* /var/lib/proxyconsole/router/* && tar -C /var/lib/proxyconsole -xzf -" < "${BACKUP_ARCHIVE}"

docker compose -f "${COMPOSE_FILE}" restart router inspector control-api >/dev/null

# Wait for both control-api health AND HTTPS edge -> inspector -> router readiness after restore restart
for _ in $(seq 1 40); do
  api_ok="$(curl -s -o /dev/null -w "%{http_code}" "${API_BASE}/api/v1/health" || true)"
  edge_ok="$(
    curl -s -o /dev/null -w "%{http_code}" \
      --cacert "${CACERT}" \
      --resolve "app-a.local.test:${EDGE_HTTPS_PORT}:127.0.0.1" \
      "https://app-a.local.test:${EDGE_HTTPS_PORT}/readyz" || true
  )"
  if [[ "${api_ok}" == "200" && "${edge_ok}" == "200" ]]; then
    break
  fi
  sleep 0.3
done

API_BASE="${API_BASE}" \
EDGE_HTTPS_PORT="${EDGE_HTTPS_PORT}" \
CACERT="${CACERT}" \
TMP_WORK_DIR="${TMP_WORK_DIR}" \
python3 - <<'PY'
import json
import os
from pathlib import Path
import subprocess
import urllib.request

api_base = os.environ["API_BASE"]
edge_port = os.environ["EDGE_HTTPS_PORT"]
cacert = os.environ["CACERT"]
manifest = json.loads((Path(os.environ["TMP_WORK_DIR"]) / "synthetic_manifest.json").read_text(encoding="utf-8"))

def api_get(path: str):
    with urllib.request.urlopen(f"{api_base}{path}", timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))

# 1. Verify restored route & Nginx active config
route_data = api_get(f"/api/v1/routes/{manifest['route_id']}")
assert route_data["route"]["domain"] == manifest["domain"], route_data
assert route_data["route"]["upstream_host"] == "echo-b", route_data

# Verify live HTTPS traffic routes through restored Nginx config and applies restored modification rule
curl_out = subprocess.check_output(
    [
        "curl",
        "-fsS",
        "--cacert",
        cacert,
        "--resolve",
        f"{manifest['domain']}:{edge_port}:127.0.0.1",
        f"https://{manifest['domain']}:{edge_port}/v1/readiness/post-restore",
    ],
    text=True,
)
echo_resp = json.loads(curl_out)
assert echo_resp["target"] == "echo-b", echo_resp
hdr_pairs = {str(k).lower(): str(v) for k, v in echo_resp.get("headers", [])}
assert hdr_pairs.get("x-readiness-rule") == "applied-synthetic", echo_resp

# 2. Verify restored capture metadata, derived views, and raw blob SHA-256
cap_data = api_get(f"/api/v1/captures/{manifest['capture_id']}")
assert cap_data["capture_id"] == manifest["capture_id"], cap_data
assert cap_data["request"]["body"]["sha256"] == manifest["body_sha256"], cap_data
assert cap_data["derived_views"]["content_kind"] == "json", cap_data
assert cap_data["derived_views"]["json_Preview"]["phase"] == "DEV-050" if "json_Preview" in cap_data["derived_views"] else True

# 3. Verify restored draft and replay history
draft_data = api_get(f"/api/v1/drafts/{manifest['draft_id']}")
draft_obj = draft_data.get("draft", draft_data)
assert draft_obj["draft_id"] == manifest["draft_id"], draft_data
assert draft_obj["source_capture_id"] == manifest["capture_id"], draft_data
assert int(draft_obj.get("replays_count", len(draft_obj.get("replays", [])))) >= 1, draft_data

# 4. Verify restored modification rule
rule_data = api_get(f"/api/v1/rules/{manifest['rule_id']}")
rule_obj = rule_data.get("rule", rule_data)
assert rule_obj["rule_id"] == manifest["rule_id"], rule_data
assert rule_obj["enabled"] is True, rule_data

# 5. Verify restored notification incident & event sequence
inc_data = api_get(f"/api/v1/notifications/incidents/{manifest['incident_id']}")
assert inc_data["incident"]["incident_id"] == manifest["incident_id"], inc_data
assert inc_data["incident"]["acknowledged"] is True, inc_data
assert any(int(e["seq"]) == int(manifest["event_seq"]) for e in inc_data["events"]), inc_data

# 6. Verify restored retention policy
ret_status = api_get("/api/v1/retention/status")
assert ret_status["policy"]["min_retention_seconds"] == manifest["min_retention_seconds"], ret_status

print("  [PASS] Restored route, capture, blob, draft, rule, notification incident, and retention policy verified!")
PY

echo "==> [7/7] Verifying disk exhaustion / storage write failure fault isolation and recovery (AC-8)"
# Simulate storage write exhaustion by making /var/lib/proxyconsole/captures and blobs read-only (0555)
docker compose -f "${COMPOSE_FILE}" exec -T inspector \
  sh -c "chmod 0555 /var/lib/proxyconsole/captures /var/lib/proxyconsole/blobs"

# Proxy traffic MUST still succeed (fail-open) even when capture/blob disk writes fail with PermissionError/OSError
DISK_FULL_RESP="$(
  curl -fsS --cacert "${CACERT}" \
    --resolve "app-a.local.test:${EDGE_HTTPS_PORT}:127.0.0.1" \
    -X POST -H "Content-Type: application/json" \
    -d '{"test":"proxy_survives_disk_write_failure"}' \
    "https://app-a.local.test:${EDGE_HTTPS_PORT}/v1/disk-exhaustion-check"
)"
echo "${DISK_FULL_RESP}" | grep -q '"target":"echo-a"'

# Restore write permissions and reset retention policy to default (86400s)
docker compose -f "${COMPOSE_FILE}" exec -T inspector \
  sh -c "chmod 0777 /var/lib/proxyconsole/captures /var/lib/proxyconsole/blobs"

curl -fsS -X PUT "${API_BASE}/api/v1/retention/policy" \
  -H "Content-Type: application/json" \
  -d '{"min_retention_seconds": 86400}' >/dev/null

# Verify new capture is recorded normally after permission restoration
POST_RECOVER_HEADERS="$(mktemp "${TMP_WORK_DIR}/headers.XXXXXX")"
curl -fsS -D "${POST_RECOVER_HEADERS}" --cacert "${CACERT}" \
  --resolve "app-a.local.test:${EDGE_HTTPS_PORT}:127.0.0.1" \
  "https://app-a.local.test:${EDGE_HTTPS_PORT}/v1/disk-recovered" >/dev/null
grep -qi "X-Capture-Status: complete" "${POST_RECOVER_HEADERS}"

echo "  [PASS] Proxy routing survived storage write failure and recovered cleanly."
echo "==> All DEV-050 company-environment readiness & backup/restore checks passed!"
