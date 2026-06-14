from __future__ import annotations

import importlib.util
from pathlib import Path
import unittest


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

    def test_report_to_chat_text_omits_style_and_keeps_body_text(self) -> None:
        html = "<html><head><style>.x{color:red}</style></head><body><h1>Title</h1><p>Hello <b>world</b></p></body></html>"

        text = self.script.report_to_chat_text(html)

        self.assertIn("Title", text)
        self.assertIn("Hello world", text)
        self.assertNotIn("color:red", text)

    def test_format_report_for_chat_truncates_long_report_and_lists_sources(self) -> None:
        latest_status = {
            "id": "job-1",
            "status": "review",
            "lookback_days": 30,
            "runs": [
                {"channel_id": "wechat-mp-rss", "status": "deduplicated", "duplicate_count": 12},
                {"channel_id": "ima-knowledge", "status": "cached_after_error", "duplicate_count": 2, "error": "forbidden"},
                {"channel_id": "zsxq", "status": "completed", "snapshot_count": 3},
            ],
        }
        report = {"id": "job-1", "status": "review", "report": "<html><body><p>" + ("A" * 80) + "</p></body></html>"}

        output = self.script.format_report_for_chat(latest_status, report, 20)

        self.assertIn("Job: job-1", output)
        self.assertIn("- wechat-mp-rss: deduplicated; used=12", output)
        self.assertIn("- ima-knowledge: cached_after_error; used=2; note=forbidden", output)
        self.assertIn("- zsxq: completed; used=3", output)
        self.assertIn("AAAAAAAAAAAAAAAAAAAA", output)
        self.assertIn("Truncated for chat delivery", output)


if __name__ == "__main__":
    unittest.main()
