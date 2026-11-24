#!/usr/bin/env python3
from __future__ import annotations

import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch


ROOT_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT_DIR / "services" / "recorder"))

from docker_discovery import DockerDiscoveryService  # noqa: E402


class DiscoveryProviderTest(unittest.TestCase):
    def test_provider_is_read_from_environment(self) -> None:
        with patch.dict(os.environ, {"CONTAINER_ENGINE_PROVIDER": "podman"}):
            service = DockerDiscoveryService(store=None)  # type: ignore[arg-type]
        self.assertEqual(service.provider, "podman")

    def test_unknown_provider_falls_back_to_docker_contract(self) -> None:
        with patch.dict(os.environ, {"CONTAINER_ENGINE_PROVIDER": "unknown"}):
            service = DockerDiscoveryService(store=None)  # type: ignore[arg-type]
        self.assertEqual(service.provider, "docker")

    def test_list_response_includes_provider(self) -> None:
        service = DockerDiscoveryService(store=None)  # type: ignore[arg-type]
        service.provider = "podman"
        service._scan_docker = lambda **_: {  # type: ignore[method-assign]
            "available": True,
            "status": "ok",
            "provider": "podman",
            "socket_path": "/run/user/1000/podman/podman.sock",
            "refreshed_at": "2026-11-24T00:00:00+00:00",
            "error": None,
            "all_containers": [],
            "all_targets": [],
        }
        result = service.list_containers()
        self.assertEqual(result["provider"], "podman")
        self.assertTrue(result["available"])

    def test_external_published_port_is_selectable_without_name_collision(self) -> None:
        with patch.dict(os.environ, {"CONTAINER_ENGINE_PROVIDER": "podman"}):
            service = DockerDiscoveryService(store=None)  # type: ignore[arg-type]
        summary = {
            "Id": "a" * 64,
            "Names": ["/api"],
            "State": "running",
            "Status": "Up 10 minutes",
            "Labels": {
                "com.docker.compose.project": "external",
                "com.docker.compose.service": "api",
            },
            "Ports": [
                {
                    "PrivatePort": 8080,
                    "PublicPort": 18080,
                    "IP": "0.0.0.0",
                    "Type": "tcp",
                }
            ],
            "NetworkSettings": {"Networks": {"external_default": {}}},
        }
        detail = {
            "Id": "a" * 64,
            "Name": "/api",
            "Config": {
                "Image": "example/api:latest",
                "Labels": summary["Labels"],
                "ExposedPorts": {"8080/tcp": {}},
            },
            "State": {"Status": "running", "Running": True},
            "NetworkSettings": {
                "Networks": {
                    "external_default": {
                        "IPAddress": "10.0.0.2",
                        "Aliases": ["api"],
                    }
                },
                "Ports": {
                    "8080/tcp": [
                        {"HostIp": "0.0.0.0", "HostPort": "18080"}
                    ]
                },
            },
        }
        container, targets = service._extract_container_and_targets(summary, detail)
        self.assertEqual(container["project_scope"], "external_project")
        self.assertEqual(container["health"], "none")
        self.assertEqual(len(targets), 1)
        target = targets[0]
        self.assertEqual(target["target_id"], f"engine:podman:{'a' * 12}:8080")
        self.assertEqual(target["reachability"], "published_host_port")
        self.assertEqual(target["upstream_host"], "host.containers.internal")
        self.assertEqual(target["upstream_port"], 18080)
        self.assertTrue(target["selectable"])


if __name__ == "__main__":
    unittest.main()
