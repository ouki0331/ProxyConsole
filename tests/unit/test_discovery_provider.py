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


if __name__ == "__main__":
    unittest.main()
