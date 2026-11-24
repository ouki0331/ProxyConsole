#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT_DIR / "services" / "recorder"))

from index_store import CaptureIndexStore  # noqa: E402


class DemoSeedModeTest(unittest.TestCase):
    def test_normal_mode_does_not_seed_demo_routes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            "os.environ", {"PROXYCONSOLE_DEMO_MODE": "false"}
        ):
            store = CaptureIndexStore(storage_dir=tmp)
            store.ensure_schema()
            with store._connect() as conn:
                count = conn.execute("SELECT COUNT(*) AS cnt FROM routes").fetchone()
            self.assertEqual(int(count["cnt"]), 0)

    def test_test_mode_seeds_demo_routes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            "os.environ", {"PROXYCONSOLE_DEMO_MODE": "true"}
        ):
            store = CaptureIndexStore(storage_dir=tmp)
            store.ensure_schema()
            with store._connect() as conn:
                rows = conn.execute(
                    "SELECT route_id FROM routes ORDER BY route_id"
                ).fetchall()
            self.assertEqual(
                [row["route_id"] for row in rows],
                ["route:app-a", "route:app-b"],
            )


if __name__ == "__main__":
    unittest.main()
