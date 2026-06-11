from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch


def timestamp() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


class SourceWeightBehaviorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        import backend.main as main

        cls.main = main

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = self.main.DB_PATH
        self.main.DB_PATH = Path(self.temp_dir.name) / "main.db"
        self.main.init_db()

    def tearDown(self) -> None:
        self.main.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    def test_default_source_weights_are_evenly_allocated_to_100_percent(self) -> None:
        config = self.main.source_weight_config()

        self.assertFalse(config["configured"])
        self.assertAlmostEqual(config["total_weight"], 100.0, places=2)
        self.assertGreaterEqual(len(config["weights"]), 2)

    def test_source_weight_save_requires_percent_total_to_be_100(self) -> None:
        payload = self.main.SourceWeightConfigInput(weights=[{"channel_id": "akshare", "weight": 99}])

        with self.assertRaises(self.main.HTTPException):
            self.main.save_source_weights(payload)

    def test_source_report_prompt_includes_analysis_weights(self) -> None:
        now = timestamp()
        with self.main.db() as conn:
            conn.executemany(
                """
                INSERT INTO source_snapshots(id,channel_id,occurred_at,collected_at,source_url,content,scope_type,scope_key)
                VALUES(?,?,?,?,?,?,'general','')
                """,
                [
                    ("weighted-akshare", "akshare", now, now, "akshare://weighted", "AKSHARE_WEIGHTED_CONTENT"),
                    ("weighted-news", "industry-news", now, now, "news://weighted", "NEWS_WEIGHTED_CONTENT"),
                ],
            )

        self.main.save_source_weights(
            self.main.SourceWeightConfigInput(
                weights=[
                    {"channel_id": "akshare", "weight": 80},
                    {"channel_id": "industry-news", "weight": 20},
                ]
            )
        )

        def capture_prompt(prompt: str, system_prompt: str = "", purpose: str = "") -> str:
            self.assertEqual(purpose, "source_report")
            self.assertIn("Source weight allocation for analysis only", prompt)
            self.assertIn("akshare", prompt)
            self.assertIn("configured=80.0%, effective=80.0%", prompt)
            self.assertIn("industry-news", prompt)
            self.assertIn("configured=20.0%, effective=20.0%", prompt)
            self.assertIn("AKSHARE_WEIGHTED_CONTENT", prompt)
            self.assertIn("NEWS_WEIGHTED_CONTENT", prompt)
            return "<html><head></head><body>ok</body></html>"

        with patch.object(self.main, "call_provider", side_effect=capture_prompt):
            report, _anchor = self.main.generate_source_report(
                self.main.SourceJobInput(action="report", channel_ids=["akshare", "industry-news"], report_title="weighted")
            )

        self.assertIn("<html>", report)


if __name__ == "__main__":
    unittest.main()
