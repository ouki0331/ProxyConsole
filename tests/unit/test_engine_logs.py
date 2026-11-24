#!/usr/bin/env python3
from __future__ import annotations

import http.client
import json
from pathlib import Path
import struct
import sys
import threading
import unittest


ROOT_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT_DIR / "services" / "recorder"))

from api_server import CaptureApiHandler, ThreadingHTTPServer  # noqa: E402
from engine_logs import (  # noqa: E402
    EngineLogSourceNotFoundError,
    EngineLogsService,
    EngineLogsUnavailableError,
    EngineLogsValidationError,
    decode_engine_log_payload,
)


def multiplex_frame(stream_type: int, payload: bytes) -> bytes:
    return bytes([stream_type, 0, 0, 0]) + struct.pack(">I", len(payload)) + payload


def inventory_container(container_id: str = "a" * 64) -> dict[str, object]:
    return {
        "inventory_id": f"docker:{container_id}",
        "container_id": container_id,
        "container_short_id": container_id[:12],
        "container_name": "control-api-1",
        "service_name": "control-api",
        "compose_project": "proxyconsole-dev001",
        "role": "system",
        "state": "running",
    }


class FakeDiscovery:
    provider = "docker"

    def __init__(self, *, available: bool = True) -> None:
        self.available = available

    def _scan_docker(self, **_: object) -> dict[str, object]:
        return {
            "available": self.available,
            "provider": "docker",
            "socket_path": "/fake/engine.sock",
            "error": None if self.available else "socket unavailable",
            "all_containers": [inventory_container()] if self.available else [],
        }


class EngineLogDecodeTest(unittest.TestCase):
    def test_multiplexed_stdout_stderr_preserves_stream_and_timestamp(self) -> None:
        payload = b"".join(
            (
                multiplex_frame(1, b"2026-11-24T01:02:03.000000000Z ready\n"),
                multiplex_frame(2, b"2026-11-24T01:02:04Z ERROR failed\n"),
            )
        )
        lines, encoding = decode_engine_log_payload(payload)
        self.assertEqual(encoding, "multiplexed")
        self.assertEqual([line["stream"] for line in lines], ["stdout", "stderr"])
        self.assertEqual(lines[0]["timestamp"], "2026-11-24T01:02:03.000000000Z")
        self.assertEqual(lines[0]["text"], "ready")
        self.assertEqual(lines[1]["severity"], "error")

    def test_plain_text_and_missing_timestamp_are_normalized(self) -> None:
        lines, encoding = decode_engine_log_payload(b"warning: slow\nplain line\n")
        self.assertEqual(encoding, "plain")
        self.assertEqual(lines[0]["severity"], "warn")
        self.assertIsNone(lines[1]["timestamp"])
        self.assertEqual(lines[1]["text"], "plain line")

    def test_invalid_multiplex_header_falls_back_to_plain_text(self) -> None:
        lines, encoding = decode_engine_log_payload(b"\x01not-a-frame\n")
        self.assertEqual(encoding, "plain")
        self.assertEqual(len(lines), 1)

    def test_byte_capped_partial_frame_does_not_become_plain_binary_text(self) -> None:
        payload = multiplex_frame(1, b"complete\n") + multiplex_frame(2, b"truncated")[:-3]
        lines, encoding = decode_engine_log_payload(payload)
        self.assertEqual(encoding, "multiplexed")
        self.assertEqual([line["text"] for line in lines], ["complete"])


class EngineLogsServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = EngineLogsService(FakeDiscovery())  # type: ignore[arg-type]
        payload = (
            b"2026-11-24T01:00:00Z ready\n"
            b"2026-11-24T01:00:01Z ERROR database failed\n"
            b"2026-11-24T01:00:02Z warning retrying database\n"
        )
        self.service._engine_get_bytes = lambda *args, **kwargs: (payload, True)  # type: ignore[method-assign]

    def test_filters_are_literal_case_insensitive_and_bounded(self) -> None:
        result = self.service.get_logs(
            container_id="a" * 64,
            tail=1000,
            since="2026-11-24T00:00:00Z",
            severity="warn",
            search="DATABASE",
        )
        self.assertEqual(result["returned"], 1)
        self.assertEqual(result["lines"][0]["text"], "warning retrying database")
        self.assertEqual(result["requested"]["tail"], 1000)
        self.assertTrue(result["truncated"])
        self.assertNotIn("regex", result)

    def test_default_tail_and_inventory_id_are_supported(self) -> None:
        result = self.service.get_logs(container_id=f"docker:{'a' * 64}")
        self.assertEqual(result["requested"]["tail"], 200)
        self.assertEqual(result["provider"], "docker")
        self.assertEqual(result["container"]["role"], "system")

    def test_invalid_filters_are_rejected(self) -> None:
        for kwargs in (
            {"tail": 0},
            {"tail": 1001},
            {"tail": "nope"},
            {"severity": "critical"},
            {"since": "yesterday"},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(EngineLogsValidationError):
                self.service.get_logs(container_id="a" * 64, **kwargs)

    def test_unknown_container_is_rejected_before_log_read(self) -> None:
        with self.assertRaises(EngineLogSourceNotFoundError):
            self.service.get_logs(container_id="b" * 64)

    def test_engine_unavailable_is_distinct(self) -> None:
        service = EngineLogsService(FakeDiscovery(available=False))  # type: ignore[arg-type]
        with self.assertRaises(EngineLogsUnavailableError):
            service.get_logs(container_id="a" * 64)


class StubLogs:
    def get_logs(self, **kwargs: object) -> dict[str, object]:
        container_id = kwargs.get("container_id")
        severity = kwargs.get("severity")
        if container_id == "missing":
            raise EngineLogSourceNotFoundError("missing")
        if container_id == "unavailable":
            raise EngineLogsUnavailableError("engine offline")
        if severity == "invalid":
            raise EngineLogsValidationError("invalid severity")
        return {"available": True, "returned": 0, "lines": [], "error": None}


class StubStore:
    logs = StubLogs()


class QuietHandler(CaptureApiHandler):
    store = StubStore()  # type: ignore[assignment]

    def log_message(self, format: str, *args: object) -> None:
        pass


class EngineLogsApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), QuietHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def request(self, query: str) -> tuple[int, dict[str, object]]:
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=2)
        conn.request("GET", f"/api/v1/runtime/logs?{query}")
        response = conn.getresponse()
        body = json.loads(response.read().decode("utf-8"))
        conn.close()
        return response.status, body

    def test_http_status_contract(self) -> None:
        self.assertEqual(self.request("container_id=known")[0], 200)
        self.assertEqual(self.request("container_id=missing")[0], 404)
        self.assertEqual(self.request("container_id=unavailable")[0], 503)
        status, body = self.request("container_id=known&severity=invalid")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "invalid_log_filter")


if __name__ == "__main__":
    unittest.main()
