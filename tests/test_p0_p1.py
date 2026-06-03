from __future__ import annotations

import json
import sqlite3
import tempfile
import time
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from backend.collectors import collect_http
from backend.industry_news_sources import collect_public_industry_news
from backend.model_gateway import GatewayResult, ModelGateway, ProviderRuntimeConfig
from backend.worker import CollectionWorker


def timestamp(offset_minutes: int = 0) -> str:
    return (datetime.now(timezone.utc).astimezone() + timedelta(minutes=offset_minutes)).isoformat(timespec="seconds")


@contextmanager
def database(path: Path):
    conn = sqlite3.connect(path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


class WorkerBehaviorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "worker.db"
        with database(self.db_path) as conn:
            conn.executescript(
                """
                CREATE TABLE channels (
                  id TEXT PRIMARY KEY, group_ids TEXT NOT NULL DEFAULT '[]'
                );
                CREATE TABLE source_collection_jobs (
                  id TEXT PRIMARY KEY, action TEXT NOT NULL, windows TEXT NOT NULL,
                  parent_task_id TEXT NOT NULL DEFAULT '', query TEXT NOT NULL DEFAULT '',
                  evidence_layer TEXT NOT NULL DEFAULT '', status TEXT NOT NULL,
                  created_at TEXT NOT NULL DEFAULT '',
                  snapshot_count INTEGER NOT NULL DEFAULT 0, completed_at TEXT NOT NULL DEFAULT '',
                  error TEXT NOT NULL DEFAULT '', report TEXT, report_anchor TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE source_snapshots (
                  id TEXT PRIMARY KEY, channel_id TEXT NOT NULL, occurred_at TEXT NOT NULL,
                  collected_at TEXT NOT NULL, source_url TEXT NOT NULL, content TEXT NOT NULL,
                  scope_type TEXT NOT NULL DEFAULT 'general', scope_key TEXT NOT NULL DEFAULT '',
                  UNIQUE(channel_id,source_url,occurred_at,scope_type,scope_key)
                );
                CREATE TABLE source_job_snapshots (
                  job_id TEXT NOT NULL, snapshot_id TEXT NOT NULL, PRIMARY KEY(job_id,snapshot_id)
                );
                CREATE TABLE source_collection_watermarks_v2 (
                  channel_id TEXT NOT NULL, scope_key TEXT NOT NULL DEFAULT '',
                  last_success_at TEXT NOT NULL, PRIMARY KEY(channel_id,scope_key)
                );
                CREATE TABLE source_collection_runs (
                  job_id TEXT NOT NULL, channel_id TEXT NOT NULL, status TEXT NOT NULL,
                  started_at TEXT NOT NULL DEFAULT '', completed_at TEXT NOT NULL DEFAULT '',
                  snapshot_count INTEGER NOT NULL DEFAULT 0, duplicate_count INTEGER NOT NULL DEFAULT 0,
                  error TEXT NOT NULL DEFAULT '', coverage_complete INTEGER NOT NULL DEFAULT 1,
                  PRIMARY KEY(job_id,channel_id)
                );
                CREATE TABLE tasks (
                  id TEXT PRIMARY KEY, status TEXT NOT NULL, agent_error TEXT NOT NULL DEFAULT ''
                );
                """
            )
        self.worker = CollectionWorker(
            db_path=self.db_path,
            profile_for=lambda channel_id: Path(self.temp_dir.name) / channel_id,
            report_after_collection=lambda job: ("<html><head></head><body>ok</body></html>", timestamp()),
            poll_seconds=0.01,
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def insert_channels(self, *channel_ids: str) -> None:
        with database(self.db_path) as conn:
            conn.executemany("INSERT INTO channels(id) VALUES(?)", [(channel_id,) for channel_id in channel_ids])

    def insert_job(self, job_id: str, windows: list[dict], *, query: str = "", action: str = "collect") -> dict:
        job = {
            "id": job_id,
            "action": action,
            "windows": json.dumps(windows),
            "parent_task_id": "",
            "query": query,
            "evidence_layer": "",
        }
        with database(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO source_collection_jobs(id,action,windows,parent_task_id,query,evidence_layer,status)
                VALUES(?,?,?,?,?,?,'running')
                """,
                (job_id, action, job["windows"], "", query, ""),
            )
        return job

    def test_partial_collection_persists_successful_channel(self) -> None:
        self.insert_channels("good", "bad")
        windows = [
            {"channel_id": "good", "window_start": timestamp(-30), "window_end": timestamp()},
            {"channel_id": "bad", "window_start": timestamp(-30), "window_end": timestamp()},
        ]
        job = self.insert_job("partial", windows)

        def collect(channel, window, profile, query):
            if channel["id"] == "bad":
                raise RuntimeError("upstream unavailable")
            return [{"channel_id": "good", "occurred_at": timestamp(-1), "source_url": "good://1", "content": "{}"}]

        with patch("backend.worker.collect_channel", collect):
            self.worker.execute(job)
        with database(self.db_path) as conn:
            self.assertEqual(conn.execute("SELECT status FROM source_collection_jobs").fetchone()[0], "partial_completed")
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM source_snapshots").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM source_collection_watermarks_v2").fetchone()[0], 1)
            runs = dict(conn.execute("SELECT channel_id,status FROM source_collection_runs"))
            self.assertEqual(runs, {"good": "completed", "bad": "failed"})

    def test_worker_can_restart_after_clean_stop(self) -> None:
        self.worker.start()
        self.assertTrue(self.worker.thread and self.worker.thread.is_alive())
        self.worker.stop(timeout_seconds=1)
        self.assertFalse(self.worker.thread and self.worker.thread.is_alive())
        self.worker.start()
        self.assertTrue(self.worker.thread and self.worker.thread.is_alive())
        self.worker.stop(timeout_seconds=1)

    def test_worker_survives_transient_loop_error(self) -> None:
        with patch.object(self.worker, "claim_next", side_effect=[RuntimeError("temporary failure"), None, None]):
            self.worker.start()
            time.sleep(0.04)
            self.assertTrue(self.worker.thread and self.worker.thread.is_alive())
            self.worker.stop(timeout_seconds=1)

    def test_zero_new_snapshots_still_advance_watermark(self) -> None:
        self.insert_channels("good")
        occurred_at = timestamp(-5)
        first_window = {"channel_id": "good", "window_start": timestamp(-30), "window_end": timestamp(-2)}
        second_window = {"channel_id": "good", "window_start": timestamp(-2), "window_end": timestamp()}
        item = {"channel_id": "good", "occurred_at": occurred_at, "source_url": "good://same", "content": "{}"}
        with patch("backend.worker.collect_channel", return_value=[item]):
            self.worker.execute(self.insert_job("first", [first_window]))
            self.worker.execute(self.insert_job("second", [second_window]))
        with database(self.db_path) as conn:
            self.assertEqual(conn.execute("SELECT status FROM source_collection_jobs WHERE id='second'").fetchone()[0], "deduplicated")
            self.assertEqual(
                conn.execute("SELECT last_success_at FROM source_collection_watermarks_v2 WHERE channel_id='good'").fetchone()[0],
                second_window["window_end"],
            )

    def test_richer_duplicate_snapshot_refreshes_content_and_normalization(self) -> None:
        self.insert_channels("good")
        occurred_at = timestamp(-5)
        window = {"channel_id": "good", "window_start": timestamp(-30), "window_end": timestamp()}
        summary = {"channel_id": "good", "occurred_at": occurred_at, "source_url": "good://same", "content": "summary"}
        full_text = {**summary, "content": "full article body " * 40}
        normalization_calls: list[tuple[str, bool]] = []

        def normalize(snapshot_id: str, force: bool = False) -> dict:
            normalization_calls.append((snapshot_id, force))
            return {}

        self.worker.normalize_snapshot = normalize
        with patch("backend.worker.collect_channel", side_effect=[[summary], [full_text]]):
            self.worker.execute(self.insert_job("summary", [window]))
            self.worker.execute(self.insert_job("full-text", [window]))
        with database(self.db_path) as conn:
            snapshot_rows = conn.execute("SELECT content FROM source_snapshots").fetchall()
            second_status = conn.execute("SELECT status FROM source_collection_jobs WHERE id='full-text'").fetchone()[0]
        self.assertEqual(snapshot_rows, [(full_text["content"],)])
        self.assertEqual(second_status, "completed")
        self.assertEqual([force for _, force in normalization_calls], [False, True])

    def test_general_and_research_snapshots_do_not_overwrite_each_other(self) -> None:
        self.insert_channels("good")
        window = {"channel_id": "good", "window_start": timestamp(-30), "window_end": timestamp()}
        item = {"channel_id": "good", "occurred_at": timestamp(-1), "source_url": "good://same", "content": "{}"}
        with patch("backend.worker.collect_channel", return_value=[item]):
            self.worker.execute(self.insert_job("general", [window]))
            self.worker.execute(self.insert_job("research", [window], query="300782 company"))
        with database(self.db_path) as conn:
            scopes = conn.execute("SELECT scope_type,scope_key FROM source_snapshots ORDER BY scope_type").fetchall()
        self.assertEqual(scopes, [("general", ""), ("research", "300782")])

    def test_research_preflight_refresh_persists_general_snapshots(self) -> None:
        self.insert_channels("good")
        window = {"channel_id": "good", "window_start": timestamp(-30), "window_end": timestamp()}
        job = self.insert_job("refresh", [window])
        job.update({"parent_task_id": "task", "evidence_layer": "local_source_snapshots"})
        with database(self.db_path) as conn:
            conn.execute("UPDATE source_collection_jobs SET parent_task_id=?,evidence_layer=? WHERE id=?", ("task", "local_source_snapshots", "refresh"))
            conn.execute("INSERT INTO tasks(id,status) VALUES('task','evidence_queued')")
        item = {"channel_id": "good", "occurred_at": timestamp(-1), "source_url": "good://refresh", "content": "{}"}
        with patch("backend.worker.collect_channel", return_value=[item]):
            self.worker.execute(job)
        with database(self.db_path) as conn:
            scope = conn.execute("SELECT scope_type,scope_key FROM source_snapshots").fetchone()
        self.assertEqual(scope, ("general", ""))


class MainBehaviorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        import backend.main as main

        cls.main = main

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = self.main.DB_PATH
        self.main.DB_PATH = Path(self.temp_dir.name) / "main.db"
        self.main.CHANNEL_LOGIN_PROCESSES.clear()
        self.main.init_db()

    def tearDown(self) -> None:
        self.main.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    def test_stock_context_reads_general_and_current_scope_only(self) -> None:
        now = timestamp()
        with self.main.db() as conn:
            conn.execute(
                "INSERT INTO channels(id,name,type,url,collection_mode,status,updated_at) VALUES('scope-test','scope','test','','requests','online',?)",
                (now,),
            )
            conn.executemany(
                """
                INSERT INTO source_snapshots(id,channel_id,occurred_at,collected_at,source_url,content,scope_type,scope_key)
                VALUES(?,?,?,?,?,?,?,?)
                """,
                [
                    ("general", "scope-test", now, now, "scope://general", "GENERAL", "general", ""),
                    ("stock-a", "scope-test", now, now, "scope://a", "STOCK_A", "research", "300782"),
                    ("stock-b", "scope-test", now, now, "scope://b", "STOCK_B", "research", "000001"),
                ],
            )
        _, _, context = self.main.local_snapshot_context(["scope-test"], 30, research_scope_key="300782 company")
        self.assertIn("GENERAL", context)
        self.assertIn("STOCK_A", context)
        self.assertNotIn("STOCK_B", context)

    def test_report_submission_is_asynchronous(self) -> None:
        with self.main.db() as conn:
            conn.execute(
                "INSERT INTO channels(id,name,type,url,collection_mode,status,updated_at) VALUES('report-test','report','test','','requests','online',?)",
                (timestamp(),),
            )
        result = self.main.create_source_job(
            self.main.SourceJobInput(action="report", channel_ids=["report-test"], report_title="async")
        )
        self.assertEqual(result["status"], "generating_report")
        with self.main.db() as conn:
            stored = conn.execute("SELECT status,report FROM source_collection_jobs WHERE id=?", (result["id"],)).fetchone()
        self.assertEqual(stored["status"], "generating_report")
        self.assertIsNone(stored["report"])

    def test_snapshot_view_uses_bounded_preview_and_preserves_full_download(self) -> None:
        now = timestamp()
        large_content = "A" * 250_000
        with self.main.db() as conn:
            conn.execute(
                "INSERT INTO channels(id,name,type,url,collection_mode,status,updated_at) VALUES('large-test','large','test','','requests','online',?)",
                (now,),
            )
            conn.execute(
                """
                INSERT INTO source_collection_jobs(id,action,channel_ids,windows,lookback_days,skill_name,report_title,status,created_at)
                VALUES('large-job','collect','["large-test"]','[]',30,'skill','large preview','completed',?)
                """,
                (now,),
            )
            conn.execute(
                """
                INSERT INTO source_snapshots(id,channel_id,occurred_at,collected_at,source_url,content)
                VALUES('large-snapshot','large-test',?,?,?,?)
                """,
                (now, now, "large://snapshot", large_content),
            )
            conn.execute(
                "INSERT INTO source_job_snapshots(job_id,snapshot_id) VALUES('large-job','large-snapshot')"
            )

        listed = self.main.source_job_snapshots("large-job")["snapshots"][0]
        self.assertNotIn("content", listed)
        self.assertEqual(len(listed["content_preview"]), self.main.SNAPSHOT_LIST_PREVIEW_CHARS)
        self.assertEqual(listed["content_length"], len(large_content))
        self.assertTrue(listed["content_truncated"])

        preview = self.main.source_snapshot_preview("large-snapshot", 200_000)
        self.assertEqual(len(preview["content_preview"]), 200_000)
        self.assertTrue(preview["content_truncated"])

        download = self.main.download_source_snapshot_content("large-snapshot")
        self.assertEqual(download.body.decode(), large_content)
        self.assertIn("alphadesk-snapshot-large-snapshot.txt", download.headers["content-disposition"])

    def test_fixed_normalizers_parse_mx_and_telegram(self) -> None:
        base = {
            "channel_id": "test",
            "occurred_at": timestamp(),
            "collected_at": timestamp(),
            "source_url": "test://item",
        }
        mx_snapshot = {
            **base,
            "content": json.dumps(
                {
                    "platform": "mx_authorized_request_replay",
                    "room": {"id": "20099", "title": "room"},
                    "message": {"id": "1", "parts": [{"type": "text", "msg": "hello"}]},
                }
            ),
        }
        tg_snapshot = {
            **base,
            "content": json.dumps(
                {"platform": "telegram_public_preview", "channel": "news", "post_id": "news/1", "text": "world"}
            ),
        }
        mx_items, _ = self.main.fixed_normalized_items(mx_snapshot)
        tg_items, _ = self.main.fixed_normalized_items(tg_snapshot)
        self.assertEqual(mx_items[0]["content"], "hello")
        self.assertGreaterEqual(mx_items[0]["quality_score"], 80)
        self.assertEqual(tg_items[0]["content"], "world")
        self.assertGreaterEqual(tg_items[0]["quality_score"], 80)

    def test_fixed_normalizer_splits_ima_knowledge_results(self) -> None:
        snapshot = {
            "channel_id": "ima-knowledge",
            "occurred_at": timestamp(),
            "collected_at": timestamp(),
            "source_url": "ima://knowledge/hashed",
            "content": json.dumps(
                {
                    "platform": "ima_knowledge_base",
                    "adapter": "ima_openapi",
                    "query": "卓胜微",
                    "knowledge_base": {"name": "每日复盘更新", "description": "每日AI自动抓取复盘"},
                    "results": [
                        {"title": "卓胜微纪要", "snippet": "卓胜微 射频前端 复盘", "folder": "复盘"},
                        {"title": "半导体更新", "snippet": "CIS 与射频链条"},
                    ],
                },
                ensure_ascii=False,
            ),
        }
        items, note = self.main.fixed_normalized_items(snapshot)
        self.assertEqual(note, "")
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["author"], "每日复盘更新")
        self.assertIn("卓胜微", items[0]["content"])
        self.assertEqual(items[0]["metadata"]["platform"], "ima_knowledge_base")

    def test_mx_har_import_wraps_validation_errors_as_json_http_conflict(self) -> None:
        with patch("backend.import_mx_har.import_har_text", side_effect=ValueError("upstream failed")):
            with self.assertRaises(self.main.HTTPException) as raised:
                self.main.import_mx_har_text("web-rumors", "{}")
        self.assertEqual(raised.exception.status_code, 409)
        self.assertIn("MX HAR 验证失败", raised.exception.detail)

    def test_mx_har_upload_accepts_raw_text_without_json_wrapping(self) -> None:
        self.assertEqual(
            self.main.mx_har_text_from_upload("text/plain; charset=utf-8", b'{"log":{"entries":[]}}'),
            '{"log":{"entries":[]}}',
        )

    def test_mx_har_upload_preserves_legacy_json_envelope_compatibility(self) -> None:
        self.assertEqual(
            self.main.mx_har_text_from_upload("application/json", b'{"har_text":"{\\\"log\\\":{}}"}'),
            '{"log":{}}',
        )

    def test_mx_har_upload_rejects_oversized_raw_har_with_readable_http_413(self) -> None:
        with patch.object(self.main, "MAX_MX_HAR_BYTES", 4):
            with self.assertRaises(self.main.HTTPException) as raised:
                self.main.mx_har_text_from_upload("text/plain", b"12345")
        self.assertEqual(raised.exception.status_code, 413)
        self.assertIn("HAR 文件过大", raised.exception.detail)

    def test_playwright_login_returns_browser_workspace_url(self) -> None:
        with self.main.db() as conn:
            conn.execute(
                "UPDATE channels SET url='https://wx.zsxq.com',collection_mode='playwright' WHERE id='zsxq'"
            )
        with patch.object(self.main, "BROWSER_WORKSPACE_PUBLIC_URL", "http://127.0.0.1:7900/vnc.html"), patch.object(
            self.main.subprocess,
            "Popen",
        ) as popen:
            result = self.main.launch_channel_login("zsxq")
        self.assertEqual(result["login_url"], "http://127.0.0.1:7900/vnc.html")
        popen.assert_called_once()

    def test_playwright_login_reuses_running_browser_process(self) -> None:
        with self.main.db() as conn:
            conn.execute(
                "UPDATE channels SET url='https://wx.zsxq.com',collection_mode='playwright' WHERE id='zsxq'"
            )
        process = Mock()
        process.poll.return_value = None
        with patch.object(self.main.subprocess, "Popen", return_value=process) as popen:
            self.main.launch_channel_login("zsxq")
            result = self.main.launch_channel_login("zsxq")
        self.assertEqual(result["status"], "opened")
        popen.assert_called_once()

    def test_zsxq_login_opens_configured_group_page(self) -> None:
        with self.main.db() as conn:
            conn.execute(
                """
                UPDATE channels
                SET url='https://wx.zsxq.com',validation_url='',group_ids='["28888222124181"]',
                    collection_mode='playwright'
                WHERE id='zsxq'
                """
            )
        with patch.object(self.main.subprocess, "Popen") as popen:
            self.main.launch_channel_login("zsxq")
        command = popen.call_args.args[0]
        self.assertEqual(command[command.index("--url") + 1], "https://wx.zsxq.com/group/28888222124181")

    def test_zsxq_check_uses_configured_group_page(self) -> None:
        with self.main.db() as conn:
            conn.execute(
                """
                UPDATE channels
                SET url='https://wx.zsxq.com',validation_url='',success_url_contains='/group',
                    group_ids='["28888222124181"]',collection_mode='playwright'
                WHERE id='zsxq'
                """
            )
        completed = Mock(
            stdout=json.dumps(
                {
                    "available": True,
                    "message": "已识别知识星球登录后的星球页面",
                    "final_url": "https://wx.zsxq.com/group/28888222124181",
                },
                ensure_ascii=False,
            )
        )
        with patch.object(self.main.subprocess, "run", return_value=completed) as run:
            result = self.main.check_channel("zsxq")
        command = run.call_args.args[0]
        self.assertEqual(command[command.index("--url") + 1], "https://wx.zsxq.com/group/28888222124181")
        self.assertEqual(command[command.index("--channel-id") + 1], "zsxq")
        self.assertEqual(result["status"], "online")

    def test_werss_normalizer_preserves_specific_public_account(self) -> None:
        snapshot = {
            "channel_id": "wechat-mp-rss",
            "occurred_at": timestamp(),
            "collected_at": timestamp(),
            "source_url": "https://mp.weixin.qq.com/s/article",
            "content": json.dumps(
                {
                    "platform": "werss_external_rss",
                    "adapter": "external_sidecar",
                    "feed_id": "MP_WXS_3865156629",
                    "source_account": {"id": "MP_WXS_3865156629", "name": "调研纪要"},
                    "article": {"id": "article", "title": "行业更新", "content": "正文"},
                },
                ensure_ascii=False,
            ),
        }
        items, _ = self.main.fixed_normalized_items(snapshot)
        self.assertEqual(items[0]["author"], "调研纪要")
        self.assertEqual(items[0]["metadata"]["source_account"]["id"], "MP_WXS_3865156629")
        self.assertEqual(self.main.normalized_source_label(items[0]), "微信公众号：调研纪要")

    def test_provider_calls_pydantic_gateway_with_encrypted_runtime_config(self) -> None:
        provider = {
            "id": "provider-test",
            "base_url": "https://api.openai.com/v1",
            "model": "gpt-test",
            "protocol": "openai_responses",
            "encrypted_api_key": "encrypted",
            "enabled": True,
            "extra_body": {},
        }
        cipher = Mock()
        cipher.decrypt.return_value = b"secret"
        gateway_result = GatewayResult(output="ok", input_tokens=3, output_tokens=1, requests=1)
        with patch.object(self.main, "provider_row", return_value=provider), patch.object(self.main, "cipher", return_value=cipher), patch.object(self.main.model_gateway, "run_text", return_value=gateway_result) as run_text:
            result = self.main.call_provider("hello", system_prompt="rules")
        self.assertEqual(result, "ok")
        config = run_text.call_args.args[0]
        self.assertEqual(config.api_key, "secret")
        self.assertEqual(config.protocol, "openai_responses")
        self.assertEqual(run_text.call_args.kwargs["instructions"], "rules")
        with self.main.db() as conn:
            log = conn.execute("SELECT status,input_tokens,output_tokens,request_count FROM model_call_logs").fetchone()
        self.assertEqual(tuple(log), ("completed", 3, 1, 1))

    def test_stock_context_can_include_general_source_reports(self) -> None:
        now = timestamp()
        with self.main.db() as conn:
            conn.execute(
                "INSERT INTO channels(id,name,type,url,collection_mode,status,updated_at) VALUES('context-test','context','test','','requests','offline',?)",
                (now,),
            )
            conn.execute(
                """
                INSERT INTO source_snapshots(id,channel_id,occurred_at,collected_at,source_url,content,scope_type,scope_key)
                VALUES('context-snapshot','context-test',?,?,?,?, 'general','')
                """,
                (now, now, "context://snapshot", "RAW_CONTEXT"),
            )
            conn.execute(
                """
                INSERT INTO source_collection_jobs(
                  id,action,channel_ids,windows,lookback_days,skill_name,report_title,status,created_at,report,report_anchor
                ) VALUES('context-report','report','["context-test"]','[]',30,'skill','行业聚合','review',?,?,?)
                """,
                (now, "<html><body>SOURCE_REPORT_CONTEXT</body></html>", now),
            )
        _, _, context = self.main.local_snapshot_context(["context-test"], 30, include_source_reports=True)
        self.assertIn("RAW_CONTEXT", context)
        self.assertIn("SOURCE_REPORT_CONTEXT", context)

    def test_stock_research_refreshes_all_online_sources_before_analysis(self) -> None:
        now = timestamp()
        with self.main.db() as conn:
            conn.execute("UPDATE channels SET status='offline'")
            conn.executemany(
                "INSERT INTO channels(id,name,type,url,collection_mode,status,updated_at) VALUES(?,?,?,?,?,?,?)",
                [
                    ("online-a", "a", "test", "", "requests", "online", now),
                    ("online-b", "b", "test", "", "playwright", "online", now),
                    ("offline-c", "c", "test", "", "requests", "offline", now),
                ],
            )
        task = {"id": "task-refresh", "target": "300782", "title": "研究", "skill_name": "a-share-growth-hunter", "lookback_days": 30}
        result = self.main.refresh_general_sources_before_research(task, {"completed_layers": [], "steps": 0})
        self.assertEqual(result["evidence_layer"], "local_source_snapshots")
        with self.main.db() as conn:
            job = conn.execute("SELECT channel_ids,windows,parent_task_id,query,evidence_layer FROM source_collection_jobs WHERE id=?", (result["job_id"],)).fetchone()
        self.assertEqual(set(json.loads(job["channel_ids"])), {"online-a", "online-b"})
        self.assertEqual({window["channel_id"] for window in json.loads(job["windows"])}, {"online-a", "online-b"})
        self.assertEqual(job["parent_task_id"], "task-refresh")
        self.assertEqual(job["query"], "")
        self.assertEqual(job["evidence_layer"], "local_source_snapshots")
        self.assertIsNone(self.main.refresh_general_sources_before_research(task, {"completed_layers": [], "steps": 0}))

    def test_general_source_report_reads_only_selected_channels(self) -> None:
        now = timestamp()
        with self.main.db() as conn:
            conn.executemany(
                "INSERT INTO channels(id,name,type,url,collection_mode,status,updated_at) VALUES(?,?,?,?,?,?,?)",
                [
                    ("selected", "selected", "test", "", "requests", "online", now),
                    ("excluded", "excluded", "test", "", "requests", "online", now),
                ],
            )
            conn.executemany(
                """
                INSERT INTO source_snapshots(id,channel_id,occurred_at,collected_at,source_url,content,scope_type,scope_key)
                VALUES(?,?,?,?,?,?,'general','')
                """,
                [
                    ("selected-snapshot", "selected", now, now, "selected://item", "SELECTED_CONTENT"),
                    ("excluded-snapshot", "excluded", now, now, "excluded://item", "EXCLUDED_CONTENT"),
                ],
            )

        def capture_prompt(prompt, system_prompt="", purpose=""):
            self.assertIn("SELECTED_CONTENT", prompt)
            self.assertNotIn("EXCLUDED_CONTENT", prompt)
            self.assertEqual(purpose, "source_report")
            return "<html><head></head><body>ok</body></html>"

        with patch.object(self.main, "call_provider", side_effect=capture_prompt):
            report, _ = self.main.generate_source_report(
                self.main.SourceJobInput(action="report", channel_ids=["selected"], report_title="selected only")
            )
        self.assertIn("<html>", report)

    def test_general_source_report_repairs_non_html_provider_output(self) -> None:
        now = timestamp()
        with self.main.db() as conn:
            conn.execute(
                "INSERT INTO channels(id,name,type,url,collection_mode,status,updated_at) VALUES(?,?,?,?,?,?,?)",
                ("repair-selected", "repair-selected", "test", "", "requests", "online", now),
            )
            conn.execute(
                """
                INSERT INTO source_snapshots(id,channel_id,occurred_at,collected_at,source_url,content,scope_type,scope_key)
                VALUES(?,?,?,?,?,?,'general','')
                """,
                ("repair-snapshot", "repair-selected", now, now, "repair://item", "REPAIR_CONTENT"),
            )

        responses = [
            "报告正文",
            "<html><head><title>fixed</title></head><body>报告正文</body></html>",
        ]
        with patch.object(self.main, "call_provider", side_effect=responses) as provider:
            report, _ = self.main.generate_source_report(
                self.main.SourceJobInput(action="report", channel_ids=["repair-selected"], report_title="repair")
            )
        self.assertIn("<title>fixed</title>", report)
        self.assertEqual(provider.call_args_list[0].kwargs["purpose"], "source_report")
        self.assertEqual(provider.call_args_list[1].kwargs["purpose"], "source_report_html_repair")

    def test_html_report_repair_still_rejects_invalid_result(self) -> None:
        with patch.object(self.main, "call_provider", return_value="still not html"):
            with self.assertRaisesRegex(self.main.HTTPException, "完整 HTML"):
                self.main.require_or_repair_html_report("not html", purpose="source_report")


class ModelGatewayBehaviorTests(unittest.TestCase):
    def test_responses_model_disables_remote_storage(self) -> None:
        gateway = ModelGateway()
        config = ProviderRuntimeConfig(
            id="responses",
            base_url="https://api.openai.com/v1",
            model="gpt-test",
            protocol="openai_responses",
            api_key="secret",
            extra_body={},
        )
        settings = gateway._model_settings(config)
        self.assertFalse(settings["openai_store"])
        self.assertNotIn("temperature", settings)

    def test_chat_model_preserves_openai_compatible_parameters(self) -> None:
        gateway = ModelGateway()
        config = ProviderRuntimeConfig(
            id="chat",
            base_url="https://api.deepseek.com",
            model="deepseek-test",
            protocol="openai_chat_completions",
            api_key="secret",
            extra_body={"top_p": 0.9},
        )
        settings = gateway._model_settings(config)
        self.assertEqual(settings["temperature"], 0.2)
        self.assertEqual(settings["extra_body"], {"top_p": 0.9})


class PaginationBehaviorTests(unittest.TestCase):
    def test_public_industry_news_pages_until_window_boundary(self) -> None:
        end = datetime.now(timezone.utc).astimezone().replace(microsecond=0)
        start = end - timedelta(days=2)

        class Collector:
            def __init__(self) -> None:
                self.calls = 0

            def industry_rankings(self):
                return {"total": 1, "top": [{"name": "AI"}], "bottom": []}

            def global_news(self, page_size=50, sort_end=""):
                self.calls += 1
                if self.calls == 1:
                    return [
                        {
                            "id": str(index),
                            "showTime": (end - timedelta(minutes=index)).isoformat(),
                            "title": f"news-{index}",
                        }
                        for index in range(50)
                    ]
                return [{"id": "old", "showTime": (start - timedelta(minutes=1)).isoformat(), "title": "old"}]

        collector = Collector()
        snapshots = collect_public_industry_news(
            "industry-news",
            {"window_start": start.isoformat(), "window_end": end.isoformat()},
            collector=collector,
        )
        self.assertEqual(collector.calls, 2)
        categories = [json.loads(item["content"]).get("category") for item in snapshots]
        self.assertEqual(categories.count("industry_news"), 50)
        self.assertIn("industry_ranking", categories)

    def test_telegram_preview_pages_until_window_boundary(self) -> None:
        end = datetime.now(timezone.utc).astimezone().replace(microsecond=0)
        start = end - timedelta(days=2)

        def html(items):
            return "".join(
                f'<div class="tgme_widget_message" data-post="news/{post_id}"><time datetime="{occurred_at}"></time><div>{text}</div></div>'
                for post_id, occurred_at, text in items
            )

        pages = [
            html([(20, (end - timedelta(hours=1)).isoformat(), "new"), (19, (end - timedelta(hours=2)).isoformat(), "newer")]),
            html([(18, (start - timedelta(minutes=1)).isoformat(), "old")]),
        ]

        class Response:
            def __init__(self, text):
                self.url = "https://t.me/s/news"
                self.text = text
                self.headers = {"content-type": "text/html"}

            def raise_for_status(self):
                return None

        with patch("backend.collectors.requests.get", side_effect=[Response(page) for page in pages]) as get:
            snapshots = collect_http(
                {"id": "tg", "url": "https://t.me/s/news"},
                {"window_start": start.isoformat(), "window_end": end.isoformat()},
            )
        self.assertEqual(get.call_count, 2)
        self.assertEqual(len(snapshots), 2)


if __name__ == "__main__":
    unittest.main()
