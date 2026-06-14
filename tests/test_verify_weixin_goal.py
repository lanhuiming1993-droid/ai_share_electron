from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
import time
from types import SimpleNamespace
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

    def test_audited_gateway_command_can_complete_goal_with_collect_job_and_response(self) -> None:
        command_ts = self.verify.parse_iso("2026-06-14T05:00:04+00:00")
        audit_path = self.hermes_home / self.verify.ALPHADESK_COMMAND_AUDIT_FILE
        audit_path.write_text(
            json.dumps(
                {
                    "timestamp": command_ts,
                    "timestamp_iso": "2026-06-14T05:00:04+00:00",
                    "platform": "weixin",
                    "days": 30,
                    "chat_id": "chat-secret",
                    "user_id": "user-secret",
                    "content_preview": "请帮我采集近 30 天的数据，并生成分析报告。",
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        workbench_db = self.hermes_home / "workbench.db"
        conn = sqlite3.connect(workbench_db)
        try:
            conn.executescript(
                """
                CREATE TABLE source_collection_jobs (
                  id TEXT PRIMARY KEY, action TEXT NOT NULL, status TEXT NOT NULL,
                  lookback_days INTEGER NOT NULL, snapshot_count INTEGER NOT NULL DEFAULT 0,
                  created_at TEXT NOT NULL, started_at TEXT NOT NULL DEFAULT '',
                  completed_at TEXT NOT NULL DEFAULT '', report TEXT
                );
                CREATE TABLE source_collection_runs (
                  job_id TEXT NOT NULL, channel_id TEXT NOT NULL, status TEXT NOT NULL,
                  snapshot_count INTEGER NOT NULL DEFAULT 0, duplicate_count INTEGER NOT NULL DEFAULT 0,
                  started_at TEXT NOT NULL DEFAULT '', completed_at TEXT NOT NULL DEFAULT '',
                  error TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE source_snapshots (
                  id TEXT PRIMARY KEY, channel_id TEXT NOT NULL
                );
                CREATE TABLE source_job_snapshots (
                  job_id TEXT NOT NULL, snapshot_id TEXT NOT NULL
                );
                """
            )
            conn.execute(
                """
                INSERT INTO source_collection_jobs(id,action,status,lookback_days,created_at,report)
                VALUES('job','collect','partial_completed',30,'2026-06-14T05:00:05+00:00',NULL)
                """
            )
            conn.executemany(
                "INSERT INTO source_collection_runs(job_id,channel_id,status) VALUES('job',?,?)",
                [
                    ("wechat-mp-rss", "deduplicated"),
                    ("ima-knowledge", "cached_after_error"),
                    ("zsxq", "deduplicated"),
                ],
            )
            conn.executemany(
                "INSERT INTO source_snapshots(id,channel_id) VALUES(?,?)",
                [
                    ("snap-werss", "wechat-mp-rss"),
                    ("snap-ima", "ima-knowledge"),
                    ("snap-zsxq", "zsxq"),
                ],
            )
            conn.executemany(
                "INSERT INTO source_job_snapshots(job_id,snapshot_id) VALUES('job',?)",
                [("snap-werss",), ("snap-ima",), ("snap-zsxq",)],
            )
            conn.commit()
        finally:
            conn.close()
        state_db = self.hermes_home / "state.db"
        state = sqlite3.connect(state_db)
        try:
            state.executescript(
                """
                CREATE TABLE sessions(id TEXT PRIMARY KEY, source TEXT NOT NULL);
                CREATE TABLE messages(id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT, timestamp REAL);
                """
            )
            state.execute("INSERT INTO sessions(id,source) VALUES('s','weixin')")
            state.execute(
                "INSERT INTO messages(id,session_id,role,content,timestamp) VALUES(1,'s','assistant',?,?)",
                ("近30天三信源报告：AI 算力、材料涨价与知识星球线索已经完成综合分析。" * 2, command_ts + 60),
            )
            state.commit()
        finally:
            state.close()
        args = SimpleNamespace(
            env_file=self.hermes_home / "cloud.env",
            workbench_db=workbench_db,
            sources="weixin,lightclawbot",
            command="采集近30天数据并生成报告",
            since_message_id=0,
            since_iso="2026-06-14T05:00:00+00:00",
            check_sources=False,
            base_url="http://127.0.0.1:18080",
            source_status_cache=self.hermes_home / "source-cache.json",
            source_check_ttl=900,
            force_source_check=False,
            hermes_home=self.hermes_home,
        )

        complete, summary = self.verify.verify_once(args)

        self.assertTrue(complete)
        self.assertEqual(summary["matched_platform_command"]["evidence"], "alphadesk_command_audit")
        self.assertEqual(summary["matched_report_job"]["id"], "job")
        self.assertEqual(summary["matched_report_job"]["action"], "collect")
        self.assertIsNotNone(summary["matched_platform_response"])
        audit_diagnostics = summary["ingress_diagnostics"]["alphadesk_command_audit"]
        self.assertTrue(audit_diagnostics["exists"])
        self.assertNotIn("chat-secret", json.dumps(audit_diagnostics, ensure_ascii=False))

    def test_partial_review_is_not_goal_complete(self) -> None:
        workbench_db = self.hermes_home / "workbench.db"
        conn = sqlite3.connect(workbench_db)
        try:
            conn.executescript(
                """
                CREATE TABLE source_collection_jobs (
                  id TEXT PRIMARY KEY, action TEXT NOT NULL, status TEXT NOT NULL,
                  lookback_days INTEGER NOT NULL, snapshot_count INTEGER NOT NULL DEFAULT 0,
                  created_at TEXT NOT NULL, started_at TEXT NOT NULL DEFAULT '',
                  completed_at TEXT NOT NULL DEFAULT '', report TEXT
                );
                CREATE TABLE source_collection_runs (
                  job_id TEXT NOT NULL, channel_id TEXT NOT NULL, status TEXT NOT NULL,
                  snapshot_count INTEGER NOT NULL DEFAULT 0, duplicate_count INTEGER NOT NULL DEFAULT 0,
                  started_at TEXT NOT NULL DEFAULT '', completed_at TEXT NOT NULL DEFAULT '',
                  error TEXT NOT NULL DEFAULT ''
                );
                """
            )
            conn.execute(
                """
                INSERT INTO source_collection_jobs(id,action,status,lookback_days,created_at,report)
                VALUES('job','collect_report','partial_review',30,'2026-06-14T05:00:05+00:00','<html></html>')
                """
            )
            conn.executemany(
                "INSERT INTO source_collection_runs(job_id,channel_id,status) VALUES('job',?,?)",
                [
                    ("wechat-mp-rss", "deduplicated"),
                    ("ima-knowledge", "failed"),
                    ("zsxq", "deduplicated"),
                ],
            )
            conn.commit()
        finally:
            conn.close()
        args = SimpleNamespace(
            env_file=self.hermes_home / "cloud.env",
            workbench_db=workbench_db,
            sources="weixin",
            command="采集近30天数据并生成报告",
            since_message_id=0,
            since_iso="",
            check_sources=False,
            base_url="http://127.0.0.1:18080",
            source_status_cache=self.hermes_home / "source-cache.json",
            source_check_ttl=900,
            force_source_check=False,
            hermes_home=self.hermes_home,
        )

        with (
            patch.object(self.verify, "parse_env", return_value={}),
            patch.object(self.verify, "resolve_workbench_db", return_value=workbench_db),
            patch.object(
                self.verify,
                "find_platform_command",
                return_value={"timestamp": self.verify.parse_iso("2026-06-14T05:00:04+00:00")},
            ),
        ):
            complete, summary = self.verify.verify_once(args)

        self.assertFalse(complete)
        self.assertEqual(summary["matched_report_job"]["status"], "partial_review")


if __name__ == "__main__":
    unittest.main()
