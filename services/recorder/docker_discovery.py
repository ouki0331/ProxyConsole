#!/usr/bin/env python3
"""Docker container discovery and route target selection service (DEV-021).

Queries the local Docker Engine API over Unix domain socket (/var/run/docker.sock)
from the control plane, extracts container metadata, network attachments, and
internal/exposed ports, filters out ProxyConsole self-infrastructure containers
by default, and resolves selected (container, port) pairs into internal network
upstream targets for RouteManager.
"""

from __future__ import annotations

from datetime import datetime, timezone
import http.client
import json
import os
import socket
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

if TYPE_CHECKING:
    from index_store import CaptureIndexStore

DEFAULT_DOCKER_SOCKET_PATH = "/var/run/docker.sock"
DEFAULT_COMPOSE_PROJECT = "proxyconsole-dev001"
SELF_SERVICE_NAMES = {
    "edge",
    "inspector",
    "control-api",
    "router",
    "db",
    "postgres",
    "redis",
}
STACK_NETWORK_SUFFIXES = (
    "router_targets_net",
    "inspector_router_net",
    "edge_inspector_net",
)
ROUTER_TARGET_NETWORK_SUFFIX = "router_targets_net"


class DiscoveryError(RuntimeError):
    """Base error for Docker discovery operations."""


class DiscoveryUnavailableError(DiscoveryError):
    """Raised when the Docker Engine socket cannot be reached or queried."""


class DiscoveryTargetNotFoundError(KeyError):
    """Raised when a requested container or target port candidate is not found."""


class DiscoveryTargetValidationError(ValueError):
    """Raised when a requested target is stopped, self-infrastructure, or invalid."""


class _UnixSocketHTTPConnection(http.client.HTTPConnection):
    """HTTPConnection over a local Unix domain socket."""

    def __init__(self, socket_path: str, timeout: float = 5.0) -> None:
        super().__init__("localhost", timeout=timeout)
        self.socket_path = socket_path

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(self.socket_path)
        self.sock = sock


def _parse_port_spec(spec: str) -> tuple[int, str] | None:
    if not spec:
        return None
    parts = spec.strip().split("/", 1)
    try:
        port_num = int(parts[0])
    except ValueError:
        return None
    proto = (parts[1] if len(parts) > 1 else "tcp").strip().lower() or "tcp"
    if port_num < 1 or port_num > 65535:
        return None
    return port_num, proto


class DockerDiscoveryService:
    def __init__(self, store: CaptureIndexStore) -> None:
        self.store = store
        self.compose_project = os.environ.get(
            "COMPOSE_PROJECT_NAME", DEFAULT_COMPOSE_PROJECT
        )

    def get_configured_socket_path(self) -> str:
        self.store.ensure_schema()
        with self.store._connect() as conn:
            row = conn.execute(
                "SELECT socket_path_override FROM discovery_status WHERE id = 1"
            ).fetchone()
        if row is not None and row["socket_path_override"]:
            return str(row["socket_path_override"])
        return os.environ.get("DOCKER_SOCKET_PATH", DEFAULT_DOCKER_SOCKET_PATH)

    def set_socket_path_override(self, socket_path_override: str | None) -> dict[str, Any]:
        self.store.ensure_schema()
        cleaned = (
            str(socket_path_override).strip()
            if socket_path_override is not None and str(socket_path_override).strip()
            else None
        )
        now_iso = datetime.now(timezone.utc).isoformat()
        with self.store._connect() as conn:
            conn.execute(
                """
                UPDATE discovery_status
                SET socket_path_override = ?,
                    updated_at = ?
                WHERE id = 1
                """,
                (cleaned, now_iso),
            )
            conn.commit()
        self.store.retention.checkpoint_wal()
        return self.refresh()

    def _docker_get_json(self, path: str, *, socket_path: str, timeout: float = 5.0) -> Any:
        if not os.path.exists(socket_path):
            raise DiscoveryUnavailableError(
                f"Docker socket path does not exist: {socket_path}"
            )
        conn = _UnixSocketHTTPConnection(socket_path, timeout=timeout)
        try:
            conn.request(
                "GET",
                path,
                headers={"Host": "localhost", "Accept": "application/json"},
            )
            resp = conn.getresponse()
            raw = resp.read().decode("utf-8", errors="replace")
            if resp.status >= 400:
                raise DiscoveryUnavailableError(
                    f"Docker API {path} returned HTTP {resp.status}: {raw.strip()}"
                )
            return json.loads(raw) if raw.strip() else None
        except DiscoveryUnavailableError:
            raise
        except Exception as exc:
            raise DiscoveryUnavailableError(
                f"Failed to query Docker socket {socket_path}: {type(exc).__name__}: {exc}"
            ) from exc
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def _is_in_scope(
        self,
        labels: dict[str, Any],
        network_names: list[str],
        scope: str,
    ) -> bool:
        if scope == "all":
            return True
        proj = str(labels.get("com.docker.compose.project") or "")
        if proj == self.compose_project:
            return True
        if str(labels.get("proxyconsole.discoverable") or "").lower() == "true":
            return True
        for net_name in network_names:
            if any(net_name.endswith(sfx) for sfx in STACK_NETWORK_SUFFIXES):
                return True
        return False

    def _is_self_container(
        self,
        container_name: str,
        service_name: str,
        labels: dict[str, Any],
    ) -> bool:
        role_label = str(labels.get("proxyconsole.role") or "").strip().lower()
        if role_label == "system":
            return True
        if role_label == "target":
            return False
        if service_name in SELF_SERVICE_NAMES:
            return True
        lower_name = container_name.lower()
        for self_svc in SELF_SERVICE_NAMES:
            if (
                lower_name == self_svc
                or lower_name.startswith(f"{self.compose_project}-{self_svc}-")
                or lower_name.startswith(f"{self.compose_project}_{self_svc}_")
            ):
                return True
        return False

    def _extract_container_and_targets(
        self,
        summary: dict[str, Any],
        detail: dict[str, Any],
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        cid = str(detail.get("Id") or summary.get("Id") or "")
        short_id = cid[:12]

        raw_name = str(detail.get("Name") or "")
        if not raw_name:
            names = summary.get("Names") or []
            raw_name = str(names[0]) if names else short_id
        container_name = raw_name.lstrip("/")

        config = detail.get("Config") or {}
        labels = config.get("Labels") or summary.get("Labels") or {}
        service_name = str(
            labels.get("com.docker.compose.service") or container_name
        )
        compose_project = str(labels.get("com.docker.compose.project") or "")
        image = str(config.get("Image") or summary.get("Image") or "")

        state_obj = detail.get("State") or {}
        docker_state = str(
            state_obj.get("Status") or summary.get("State") or "unknown"
        ).lower()
        is_running = bool(state_obj.get("Running")) and docker_state == "running"
        normalized_state = "running" if is_running else "stopped"
        status_text = str(summary.get("Status") or docker_state)

        is_self = self._is_self_container(container_name, service_name, labels)
        role = "system" if is_self else "target"

        net_settings = detail.get("NetworkSettings") or {}
        networks_map = (
            net_settings.get("Networks")
            or (summary.get("NetworkSettings") or {}).get("Networks")
            or {}
        )
        networks_list: list[dict[str, Any]] = []
        router_reachable_hosts: list[str] = []
        fallback_hosts: list[str] = []

        for net_name in sorted(networks_map.keys()):
            net_info = networks_map[net_name] or {}
            ip_addr = str(net_info.get("IPAddress") or "").strip()
            raw_aliases = net_info.get("Aliases") or []
            aliases = [
                str(a).strip()
                for a in raw_aliases
                if isinstance(a, str) and str(a).strip()
            ]
            reachable = net_name.endswith(ROUTER_TARGET_NETWORK_SUFFIX) or (
                net_name == ROUTER_TARGET_NETWORK_SUFFIX
            )
            networks_list.append(
                {
                    "network_name": net_name,
                    "ip_address": ip_addr,
                    "aliases": aliases,
                    "reachable_from_router": reachable,
                }
            )
            candidate_host = (
                service_name
                if service_name in aliases
                else (container_name if container_name else ip_addr)
            )
            if reachable:
                if service_name:
                    router_reachable_hosts.append(service_name)
                elif candidate_host:
                    router_reachable_hosts.append(candidate_host)
                elif ip_addr:
                    router_reachable_hosts.append(ip_addr)
            else:
                if service_name:
                    fallback_hosts.append(service_name)
                elif candidate_host:
                    fallback_hosts.append(candidate_host)
                elif ip_addr:
                    fallback_hosts.append(ip_addr)

        reachable_from_router = any(
            n["reachable_from_router"] for n in networks_list
        )
        preferred_host = (
            router_reachable_hosts[0]
            if router_reachable_hosts
            else (
                fallback_hosts[0]
                if fallback_hosts
                else (service_name or container_name)
            )
        )

        # Extract internal / exposed / published ports
        ports_by_key: dict[tuple[int, str], dict[str, Any]] = {}

        # 1. From detail NetworkSettings.Ports
        ns_ports = net_settings.get("Ports") or {}
        if isinstance(ns_ports, dict):
            for port_spec, bindings in ns_ports.items():
                parsed = _parse_port_spec(str(port_spec))
                if parsed is None:
                    continue
                p_num, proto = parsed
                entry = ports_by_key.setdefault(
                    (p_num, proto),
                    {
                        "internal_port": p_num,
                        "protocol": proto,
                        "exposed": True,
                        "published_ports": [],
                    },
                )
                if isinstance(bindings, list):
                    for b in bindings:
                        if not isinstance(b, dict):
                            continue
                        h_ip = str(b.get("HostIp") or "")
                        h_port_raw = b.get("HostPort")
                        try:
                            h_port = int(h_port_raw) if h_port_raw else 0
                        except ValueError:
                            h_port = 0
                        if h_port > 0:
                            pub_item = {"host_ip": h_ip, "host_port": h_port}
                            if pub_item not in entry["published_ports"]:
                                entry["published_ports"].append(pub_item)

        # 2. From detail Config.ExposedPorts (present even when container is stopped)
        exposed_ports = config.get("ExposedPorts") or {}
        if isinstance(exposed_ports, dict):
            for port_spec in exposed_ports.keys():
                parsed = _parse_port_spec(str(port_spec))
                if parsed is None:
                    continue
                p_num, proto = parsed
                ports_by_key.setdefault(
                    (p_num, proto),
                    {
                        "internal_port": p_num,
                        "protocol": proto,
                        "exposed": True,
                        "published_ports": [],
                    },
                )

        # 3. From summary Ports array
        sum_ports = summary.get("Ports") or []
        if isinstance(sum_ports, list):
            for sp in sum_ports:
                if not isinstance(sp, dict):
                    continue
                priv = sp.get("PrivatePort")
                proto = str(sp.get("Type") or "tcp").strip().lower() or "tcp"
                try:
                    p_num = int(priv)
                except (TypeError, ValueError):
                    continue
                if p_num < 1 or p_num > 65535:
                    continue
                entry = ports_by_key.setdefault(
                    (p_num, proto),
                    {
                        "internal_port": p_num,
                        "protocol": proto,
                        "exposed": True,
                        "published_ports": [],
                    },
                )
                pub_port_raw = sp.get("PublicPort")
                try:
                    pub_port = int(pub_port_raw) if pub_port_raw else 0
                except (TypeError, ValueError):
                    pub_port = 0
                if pub_port > 0:
                    pub_item = {
                        "host_ip": str(sp.get("IP") or ""),
                        "host_port": pub_port,
                    }
                    if pub_item not in entry["published_ports"]:
                        entry["published_ports"].append(pub_item)

        sorted_ports: list[dict[str, Any]] = []
        target_candidates: list[dict[str, Any]] = []
        target_key_name = service_name or container_name

        for (p_num, proto) in sorted(ports_by_key.keys()):
            if proto != "tcp":
                continue
            p_info = ports_by_key[(p_num, proto)]
            target_id = f"docker:{target_key_name}:{p_num}"
            port_entry = {
                "target_id": target_id,
                "internal_port": p_num,
                "protocol": proto,
                "exposed": bool(p_info["exposed"]),
                "published_ports": p_info["published_ports"],
                "upstream_scheme": "http",
                "upstream_host": preferred_host,
                "upstream_port": p_num,
                "upstream_url": f"http://{preferred_host}:{p_num}",
                "selectable": bool(
                    is_running and not is_self and reachable_from_router
                ),
            }
            sorted_ports.append(port_entry)
            target_candidates.append(
                {
                    "target_id": target_id,
                    "container_id": cid,
                    "container_short_id": short_id,
                    "container_name": container_name,
                    "service_name": service_name,
                    "compose_project": compose_project,
                    "image": image,
                    "state": normalized_state,
                    "docker_state": docker_state,
                    "running": is_running,
                    "is_self": is_self,
                    "role": role,
                    "reachable_from_router": reachable_from_router,
                    "selectable": bool(
                        is_running and not is_self and reachable_from_router
                    ),
                    "upstream_scheme": "http",
                    "upstream_host": preferred_host,
                    "upstream_port": p_num,
                    "upstream_url": f"http://{preferred_host}:{p_num}",
                    "protocol": proto,
                    "published_ports": p_info["published_ports"],
                    "networks": [n["network_name"] for n in networks_list],
                }
            )

        container_record = {
            "container_id": cid,
            "container_short_id": short_id,
            "container_name": container_name,
            "service_name": service_name,
            "compose_project": compose_project,
            "image": image,
            "state": normalized_state,
            "docker_state": docker_state,
            "running": is_running,
            "status": status_text,
            "is_self": is_self,
            "role": role,
            "reachable_from_router": reachable_from_router,
            "preferred_host": preferred_host,
            "networks": networks_list,
            "ports": sorted_ports,
        }
        return container_record, target_candidates

    def _scan_docker(
        self,
        *,
        scope: str = "project",
    ) -> dict[str, Any]:
        self.store.ensure_schema()
        socket_path = self.get_configured_socket_path()
        now_iso = datetime.now(timezone.utc).isoformat()

        try:
            raw_list = self._docker_get_json(
                "/containers/json?all=1", socket_path=socket_path
            )
            if not isinstance(raw_list, list):
                raise DiscoveryUnavailableError(
                    "Unexpected non-list response from Docker /containers/json"
                )

            all_containers: list[dict[str, Any]] = []
            all_targets: list[dict[str, Any]] = []

            for item in raw_list:
                if not isinstance(item, dict):
                    continue
                labels = item.get("Labels") or {}
                net_map = (item.get("NetworkSettings") or {}).get("Networks") or {}
                net_names = list(net_map.keys()) if isinstance(net_map, dict) else []
                if not self._is_in_scope(labels, net_names, scope):
                    continue

                cid = str(item.get("Id") or "")
                if not cid:
                    continue
                detail = self._docker_get_json(
                    f"/containers/{quote(cid)}/json", socket_path=socket_path
                )
                if not isinstance(detail, dict):
                    detail = item
                c_rec, c_targets = self._extract_container_and_targets(item, detail)
                all_containers.append(c_rec)
                all_targets.extend(c_targets)

            all_containers.sort(
                key=lambda c: (
                    1 if c["is_self"] else 0,
                    c["service_name"],
                    c["container_name"],
                )
            )
            all_targets.sort(
                key=lambda t: (
                    1 if t["is_self"] else 0,
                    t["service_name"],
                    t["upstream_port"],
                )
            )

            non_self_containers = [c for c in all_containers if not c["is_self"]]
            running_non_self = [c for c in non_self_containers if c["running"]]
            selectable_targets = [
                t for t in all_targets if t["running"] and not t["is_self"]
            ]

            with self.store._connect() as conn:
                conn.execute(
                    """
                    UPDATE discovery_status
                    SET available = 1,
                        status = 'ok',
                        last_refresh_at = ?,
                        last_success_at = ?,
                        last_error = NULL,
                        containers_count = ?,
                        running_containers_count = ?,
                        targets_count = ?,
                        updated_at = ?
                    WHERE id = 1
                    """,
                    (
                        now_iso,
                        now_iso,
                        len(non_self_containers),
                        len(running_non_self),
                        len(selectable_targets),
                        now_iso,
                    ),
                )
                conn.commit()

            return {
                "available": True,
                "status": "ok",
                "socket_path": socket_path,
                "refreshed_at": now_iso,
                "error": None,
                "fallback_manual_upstream_supported": True,
                "all_containers": all_containers,
                "all_targets": all_targets,
            }
        except Exception as exc:
            err_msg = str(exc)
            with self.store._connect() as conn:
                conn.execute(
                    """
                    UPDATE discovery_status
                    SET available = 0,
                        status = 'unavailable',
                        last_refresh_at = ?,
                        last_error = ?,
                        containers_count = 0,
                        running_containers_count = 0,
                        targets_count = 0,
                        updated_at = ?
                    WHERE id = 1
                    """,
                    (now_iso, err_msg, now_iso),
                )
                conn.commit()
            return {
                "available": False,
                "status": "unavailable",
                "socket_path": socket_path,
                "refreshed_at": now_iso,
                "error": err_msg,
                "fallback_manual_upstream_supported": True,
                "all_containers": [],
                "all_targets": [],
            }

    def get_status(self, *, refresh: bool = True) -> dict[str, Any]:
        if refresh:
            self._scan_docker()
        self.store.ensure_schema()
        socket_path = self.get_configured_socket_path()
        with self.store._connect() as conn:
            row = conn.execute(
                "SELECT * FROM discovery_status WHERE id = 1"
            ).fetchone()
        if row is None:
            return {
                "available": False,
                "status": "uninitialized",
                "socket_path": socket_path,
                "socket_path_override": None,
                "last_refresh_at": None,
                "last_success_at": None,
                "last_error": None,
                "containers_count": 0,
                "running_containers_count": 0,
                "targets_count": 0,
                "fallback_manual_upstream_supported": True,
            }
        return {
            "available": bool(row["available"]),
            "status": str(row["status"]),
            "socket_path": socket_path,
            "socket_path_override": row["socket_path_override"],
            "last_refresh_at": row["last_refresh_at"],
            "last_success_at": row["last_success_at"],
            "last_error": row["last_error"],
            "containers_count": int(row["containers_count"]),
            "running_containers_count": int(row["running_containers_count"]),
            "targets_count": int(row["targets_count"]),
            "fallback_manual_upstream_supported": True,
            "updated_at": row["updated_at"],
        }

    def list_containers(
        self,
        *,
        include_self: bool = False,
        state: str | None = None,
        scope: str = "project",
    ) -> dict[str, Any]:
        scan = self._scan_docker(scope=scope)
        containers = scan["all_containers"]
        if not include_self:
            containers = [c for c in containers if not c["is_self"]]
        if state and state.lower() != "all":
            want = state.strip().lower()
            if want not in ("running", "stopped"):
                raise ValueError(
                    f"Invalid state filter {state!r}: must be 'running', 'stopped', or 'all'"
                )
            containers = [c for c in containers if c["state"] == want]
        return {
            "available": scan["available"],
            "status": scan["status"],
            "socket_path": scan["socket_path"],
            "refreshed_at": scan["refreshed_at"],
            "error": scan["error"],
            "fallback_manual_upstream_supported": True,
            "include_self": bool(include_self),
            "state_filter": state or "all",
            "total": len(containers),
            "items": containers,
        }

    def list_targets(
        self,
        *,
        include_self: bool = False,
        include_stopped: bool = False,
        scope: str = "project",
    ) -> dict[str, Any]:
        scan = self._scan_docker(scope=scope)
        targets = scan["all_targets"]
        if not include_self:
            targets = [t for t in targets if not t["is_self"]]
        if not include_stopped:
            targets = [t for t in targets if t["running"]]
        return {
            "available": scan["available"],
            "status": scan["status"],
            "socket_path": scan["socket_path"],
            "refreshed_at": scan["refreshed_at"],
            "error": scan["error"],
            "fallback_manual_upstream_supported": True,
            "include_self": bool(include_self),
            "include_stopped": bool(include_stopped),
            "total": len(targets),
            "items": targets,
        }

    def refresh(
        self,
        *,
        include_self: bool = False,
        include_stopped: bool = False,
        scope: str = "project",
    ) -> dict[str, Any]:
        scan = self._scan_docker(scope=scope)
        containers = (
            scan["all_containers"]
            if include_self
            else [c for c in scan["all_containers"] if not c["is_self"]]
        )
        targets = scan["all_targets"]
        if not include_self:
            targets = [t for t in targets if not t["is_self"]]
        if not include_stopped:
            targets = [t for t in targets if t["running"]]
        return {
            "available": scan["available"],
            "status": scan["status"],
            "socket_path": scan["socket_path"],
            "refreshed_at": scan["refreshed_at"],
            "error": scan["error"],
            "fallback_manual_upstream_supported": True,
            "include_self": bool(include_self),
            "include_stopped": bool(include_stopped),
            "containers_total": len(containers),
            "containers": containers,
            "targets_total": len(targets),
            "targets": targets,
        }

    def resolve_target(
        self,
        *,
        target_id: str | None = None,
        container: str | None = None,
        port: int | None = None,
        upstream_scheme: str = "http",
        allow_self: bool = False,
        scope: str = "project",
    ) -> dict[str, Any]:
        scan = self._scan_docker(scope=scope)
        if not scan["available"]:
            raise DiscoveryUnavailableError(
                f"Docker discovery is unavailable ({scan['error']}). "
                "Specify manual upstream_host and upstream_port instead."
            )

        all_targets = scan["all_targets"]
        all_containers = scan["all_containers"]
        matched_target: dict[str, Any] | None = None

        if target_id:
            clean_tid = str(target_id).strip()
            for t in all_targets:
                if t["target_id"] == clean_tid:
                    matched_target = t
                    break
            if matched_target is None and clean_tid.startswith("docker:"):
                parts = clean_tid.split(":")
                if len(parts) == 3:
                    _, c_ref, p_str = parts
                    try:
                        p_num = int(p_str)
                    except ValueError:
                        p_num = -1
                    for t in all_targets:
                        if p_num == t["upstream_port"] and (
                            c_ref
                            in (
                                t["service_name"],
                                t["container_name"],
                                t["container_id"],
                                t["container_short_id"],
                            )
                        ):
                            matched_target = t
                            break
            if matched_target is None:
                raise DiscoveryTargetNotFoundError(
                    f"Discovered target {clean_tid!r} not found"
                )
        elif container:
            clean_c = str(container).strip()
            matched_c = None
            for c in all_containers:
                if clean_c in (
                    c["service_name"],
                    c["container_name"],
                    c["container_id"],
                    c["container_short_id"],
                ):
                    matched_c = c
                    break
            if matched_c is None:
                raise DiscoveryTargetNotFoundError(
                    f"Discovered container {clean_c!r} not found"
                )
            c_targets = [
                t
                for t in all_targets
                if t["container_id"] == matched_c["container_id"]
            ]
            if port is not None:
                want_port = int(port)
                for t in c_targets:
                    if t["upstream_port"] == want_port:
                        matched_target = t
                        break
                if matched_target is None:
                    raise DiscoveryTargetNotFoundError(
                        f"Port {want_port} is not exposed on container {clean_c!r}"
                    )
            else:
                if len(c_targets) == 1:
                    matched_target = c_targets[0]
                elif len(c_targets) == 0:
                    raise DiscoveryTargetValidationError(
                        f"Container {clean_c!r} has no discovered TCP ports"
                    )
                else:
                    ports_str = ", ".join(str(t["upstream_port"]) for t in c_targets)
                    raise DiscoveryTargetValidationError(
                        f"Container {clean_c!r} exposes multiple ports ({ports_str}); "
                        "explicit port or target_id is required"
                    )
        else:
            raise DiscoveryTargetValidationError(
                "Either target_id or container (+ port) must be provided"
            )

        if matched_target["is_self"] and not allow_self:
            raise DiscoveryTargetValidationError(
                f"Target {matched_target['target_id']!r} belongs to ProxyConsole system container "
                f"{matched_target['service_name']!r} and cannot be selected by default"
            )

        if not matched_target["running"]:
            raise DiscoveryTargetValidationError(
                f"Target {matched_target['target_id']!r} container is not running "
                f"(state={matched_target['state']!r})"
            )

        if not matched_target["reachable_from_router"] and not allow_self:
            raise DiscoveryTargetValidationError(
                f"Target {matched_target['target_id']!r} is not attached to the internal router network"
            )

        return {
            "upstream_scheme": upstream_scheme,
            "upstream_host": matched_target["upstream_host"],
            "upstream_port": matched_target["upstream_port"],
            "target_metadata": {
                "source": "docker_discovery",
                "target_id": matched_target["target_id"],
                "container_id": matched_target["container_id"],
                "container_short_id": matched_target["container_short_id"],
                "container_name": matched_target["container_name"],
                "service_name": matched_target["service_name"],
                "image": matched_target["image"],
                "selected_port": matched_target["upstream_port"],
                "selected_at": scan["refreshed_at"],
            },
            "resolved_target": matched_target,
        }

    def get_container_states_map(self) -> dict[str, dict[str, Any]]:
        """Best-effort map of service_name / container_name / container_id -> state info."""
        socket_path = self.get_configured_socket_path()
        if not os.path.exists(socket_path):
            return {}
        try:
            scan = self._scan_docker()
            if not scan["available"]:
                return {}
            out: dict[str, dict[str, Any]] = {}
            for c in scan["all_containers"]:
                info = {
                    "state": c["state"],
                    "docker_state": c["docker_state"],
                    "running": c["running"],
                    "container_id": c["container_id"],
                    "container_short_id": c["container_short_id"],
                    "container_name": c["container_name"],
                    "service_name": c["service_name"],
                }
                out[c["service_name"]] = info
                out[c["container_name"]] = info
                out[c["container_id"]] = info
                out[c["container_short_id"]] = info
            return out
        except Exception:
            return {}
