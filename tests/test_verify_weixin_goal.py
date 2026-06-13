from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch


def load_verify_module():
    script_path = (
        Path(__file__).resolve().parents[1]
        / "deploy"
        / "hermes"
        / "alphadesk-cloud-report"
        / "scripts"
        / "verify_weixin_goal.py"
    )
    spec = importlib.util.spec_from_file_location("verify_weixin_goal", script_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class VerifyWeixinGoalDiagnosticsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.verify = load_verify_module()
        self.tmp = tempfile.TemporaryDirectory()
        self.hermes_home = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_weixin_sync_diagnostics_redacts_account_and_reports_freshness(self) -> None:
        account_dir = self.hermes_home / "weixin" / "accounts"
        account_dir.mkdir(parents=True)
        sync_file = account_dir / "account-secret@im.bot.sync.json"
        sync_file.write_text(json.dumps({"get_updates_buf": "opaque-buffer"}), encoding="utf-8")
        mtime = time.time() - 7
        sync_file.touch()
        import os

        os.utime(sync_file, (mtime, mtime))

        result = self.verify.read_weixin_sync_diagnostics(self.hermes_home, now=mtime + 10)

        self.assertTrue(result["exists"])
        self.assertEqual(len(result["sync_files"]), 1)
        item = result["sync_files"][0]
        self.assertEqual(item["age_seconds"], 10)
        self.assertEqual(item["sync_buffer_length"], len("opaque-buffer"))
        self.assertNotIn("account-secret", json.dumps(result))
        self.assertNotIn("opaque-buffer", json.dumps(result))

    def test_gateway_log_diagnostics_extracts_latest_ingress_and_rate_limit(self) -> None:
        log_dir = self.hermes_home / "logs"
        log_dir.mkdir(parents=True)
        (log_dir / "gateway.log").write_text(
            "\n".join(
                [
                    "2026-06-14 05:00:01,001 INFO gateway.platforms.weixin: [Weixin] inbound from=o9cq800U type=dm media=0",
                    "2026-06-14 05:00:04,002 INFO gateway.run: inbound message: platform=weixin user=u chat=c msg='采集近30天数据并生成报告'",
                    "2026-06-14 05:01:01,003 WARNING gateway.platforms.weixin: [Weixin] rate limited for o9cq800U; backing off 3.0s before retry",
                ]
            ),
            encoding="utf-8",
        )

        result = self.verify.read_gateway_log_diagnostics(self.hermes_home)

        latest = result["latest"]
        self.assertIn("weixin_adapter_inbound", latest)
        self.assertIn("weixin_message", latest)
        self.assertIn("weixin_rate_limited", latest)
        self.assertEqual(latest["weixin_message"]["timestamp"], "2026-06-14 05:00:04,002")

    def test_plugin_diagnostics_reads_version_without_importing_hermes(self) -> None:
        plugin_dir = self.hermes_home / "plugins" / "alphadesk-command"
        plugin_dir.mkdir(parents=True)
        (plugin_dir / "plugin.yaml").write_text(
            'name: alphadesk-command\nversion: "0.1.1"\n',
            encoding="utf-8",
        )

        result = self.verify.read_alphadesk_plugin_diagnostics(self.hermes_home)

        self.assertEqual(result["name"], "alphadesk-command")
        self.assertEqual(result["version"], "0.1.1")

    def test_source_status_cache_prevents_repeated_expensive_checks(self) -> None:
        cache_path = self.hermes_home / "source-status-cache.json"
        fresh = {"ima-knowledge": {"status": "online"}}

        with patch.object(self.verify, "collect_source_status", return_value=fresh) as collect:
            first = self.verify.collect_source_status_cached(
                "http://127.0.0.1:18080",
                cache_path=cache_path,
                ttl_seconds=900,
            )
            second = self.verify.collect_source_status_cached(
                "http://127.0.0.1:18080",
                cache_path=cache_path,
                ttl_seconds=900,
            )

        self.assertEqual(collect.call_count, 1)
        self.assertFalse(first["_cache"]["hit"])
        self.assertTrue(second["_cache"]["hit"])
        self.assertEqual(second["ima-knowledge"]["status"], "online")

    def test_source_status_cache_can_be_forced(self) -> None:
        cache_path = self.hermes_home / "source-status-cache.json"
        cache_path.write_text(
            json.dumps(
                {
                    "base_url": "http://127.0.0.1:18080",
                    "checked_at_epoch": time.time(),
                    "source_status": {"ima-knowledge": {"status": "offline"}},
                }
            ),
            encoding="utf-8",
        )

        with patch.object(self.verify, "collect_source_status", return_value={"ima-knowledge": {"status": "online"}}) as collect:
            result = self.verify.collect_source_status_cached(
                "http://127.0.0.1:18080",
                cache_path=cache_path,
                ttl_seconds=900,
                force=True,
            )

        self.assertEqual(collect.call_count, 1)
        self.assertFalse(result["_cache"]["hit"])
        self.assertEqual(result["ima-knowledge"]["status"], "online")


if __name__ == "__main__":
    unittest.main()
