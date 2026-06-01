from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.db_migrations import LATEST_SCHEMA_REVISION, ensure_migration_ledger
from backend.observability import configure_optional_telemetry
from backend.runtime_health import database_check, summarize_readiness, telemetry_check
from backend.source_registry import CANONICAL_CHANNEL_NAMES, source_catalog, tool_catalog


class EnterpriseBaselineTests(unittest.TestCase):
    def test_source_registry_exposes_stable_capabilities_and_display_names(self) -> None:
        sources = {item["id"]: item for item in source_catalog()}
        self.assertIn("market_snapshot", sources["akshare"]["capabilities"])
        self.assertEqual(sources["web-rumors"]["credential_mode"], "encrypted_session_config")
        self.assertEqual(CANONICAL_CHANNEL_NAMES["146aa28e21"], "TG 小作文频道")
        self.assertEqual([tool["priority"] for tool in tool_catalog()], [1, 2, 3, 4])

    def test_migration_ledger_makes_sqlite_readiness_visible(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "workbench.db"
            conn = sqlite3.connect(db_path)
            try:
                schema = ensure_migration_ledger(conn)
                conn.commit()
            finally:
                conn.close()
            self.assertEqual(schema["status"], "current")
            self.assertEqual(schema["current_revision"], LATEST_SCHEMA_REVISION)
            self.assertEqual(database_check(db_path)["status"], "ok")

    def test_readiness_blocks_failed_required_checks_only(self) -> None:
        readiness = summarize_readiness(
            [
                {"name": "sqlite", "status": "ok", "blocking": True},
                {"name": "opentelemetry", "status": "info", "blocking": False},
            ]
        )
        self.assertEqual(readiness["status"], "ready")

    def test_opentelemetry_is_opt_in(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            telemetry = configure_optional_telemetry(object())
        self.assertEqual(telemetry["status"], "disabled")
        self.assertEqual(telemetry_check(telemetry)["status"], "info")


if __name__ == "__main__":
    unittest.main()
