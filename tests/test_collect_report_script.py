from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


def load_collect_report_module():
    script_path = (
        Path(__file__).resolve().parents[1]
        / "deploy"
        / "hermes"
        / "alphadesk-cloud-report"
        / "scripts"
        / "collect_report.py"
    )
    spec = importlib.util.spec_from_file_location("alphadesk_collect_report", script_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class AlphaDeskCollectReportScriptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.script = load_collect_report_module()

    def test_format_evidence_for_hermes_lists_sources_and_items(self) -> None:
        latest_status = {
            "id": "job-1",
            "status": "partial_completed",
            "lookback_days": 30,
            "query": "长光华芯",
            "runs": [
                {"channel_id": "wechat-mp-rss", "status": "deduplicated", "duplicate_count": 12},
                {"channel_id": "ima-knowledge", "status": "cached_after_error", "duplicate_count": 2, "error": "forbidden"},
                {"channel_id": "zsxq", "status": "completed", "snapshot_count": 3},
            ],
        }
        evidence = {
            "attached_snapshot_counts": [
                {"channel_id": "wechat-mp-rss", "count": 12},
                {"channel_id": "ima-knowledge", "count": 2},
                {"channel_id": "zsxq", "count": 3},
            ],
            "selected_items": [
                {
                    "channel_id": "wechat-mp-rss",
                    "occurred_at": "2026-06-14T00:00:00+00:00",
                    "author": "调研纪要",
                    "title": "AI 算力更新",
                    "content_preview": "光模块与材料涨价线索",
                    "source_url": "https://mp.weixin.qq.com/s/demo",
                }
            ],
        }

        output = self.script.format_evidence_for_hermes(latest_status, evidence, 1000)

        self.assertIn("Job: job-1", output)
        self.assertIn("Research query: 长光华芯", output)
        self.assertIn("- wechat-mp-rss: deduplicated; used=12", output)
        self.assertIn("- ima-knowledge: cached_after_error; used=2; note=forbidden", output)
        self.assertIn("- zsxq: completed; used=3", output)
        self.assertIn("请你作为行业分析师", output)
        self.assertIn("光模块与材料涨价线索", output)
        self.assertIn("Snapshot coverage", output)
        self.assertIn("render_report_pdf.py", output)
        self.assertIn("结构化 HTML", output)
        self.assertIn("span.source-tag", output)
        self.assertIn("span.fact", output)
        self.assertIn("span.infer", output)
        self.assertIn("span.unverified", output)
        self.assertIn("MEDIA:/absolute/path/to/report.pdf", output)

    def test_format_evidence_for_hermes_truncates_selected_evidence(self) -> None:
        latest_status = {"id": "job-1", "status": "completed", "lookback_days": 30, "runs": []}
        evidence = {
            "selected_items": [
                {
                    "channel_id": "zsxq",
                    "occurred_at": "2026-06-14T00:00:00+00:00",
                    "author": "知识星球",
                    "content_preview": "A" * 500,
                }
            ]
        }

        output = self.script.format_evidence_for_hermes(latest_status, evidence, 260)

        self.assertIn("Evidence truncated for Hermes context", output)

    def test_main_posts_query_to_agent_collect_report(self) -> None:
        calls: list[tuple[str, str, dict | None]] = []

        def fake_request(method: str, _base_url: str, path: str, _token: str, payload=None):
            calls.append((method, path, payload))
            if path == "/api/agent/collect-report":
                return {"job_id": "job-1", "poll_url": "/api/agent/jobs/job-1", "channel_ids": ["wechat-mp-rss"]}
            if path == "/api/agent/jobs/job-1":
                return {"id": "job-1", "status": "completed", "lookback_days": 7, "query": "长光华芯", "runs": []}
            if path.startswith("/api/agent/jobs/job-1/evidence"):
                return {"selected_items": [], "attached_snapshot_counts": []}
            raise AssertionError(path)

        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / "cloud.env"
            env_file.write_text("ALPHADESK_AGENT_TOKEN=test-token\n", encoding="utf-8")
            argv = [
                "collect_report.py",
                "--days",
                "7",
                "--query",
                "长光华芯",
                "--env-file",
                str(env_file),
                "--interval",
                "0",
            ]
            with patch.object(sys, "argv", argv), patch.object(self.script, "request_json", side_effect=fake_request):
                self.assertEqual(self.script.main(), 0)

        self.assertEqual(
            calls[0],
            (
                "POST",
                "/api/agent/collect-report",
                {"lookback_days": 7, "force_refresh": True, "query": "长光华芯"},
            ),
        )


if __name__ == "__main__":
    unittest.main()
