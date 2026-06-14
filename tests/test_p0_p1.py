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

from pydantic import BaseModel

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

    def test_collect_report_uses_cached_window_snapshots_when_live_source_fails(self) -> None:
        self.insert_channels("cached")
        window = {"channel_id": "cached", "window_start": timestamp(-30), "window_end": timestamp()}
        job = self.insert_job("cached-report", [window], action="collect_report")
        job["report_title"] = "cached report"
        with database(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO source_snapshots(id,channel_id,occurred_at,collected_at,source_url,content,scope_type,scope_key)
                VALUES('cached-snapshot','cached',?,?, 'cached://1','{}','general','')
                """,
                (timestamp(-5), timestamp(-4)),
            )

        with patch("backend.worker.collect_channel", side_effect=RuntimeError("quota exhausted")):
            self.worker.execute(job)

        with database(self.db_path) as conn:
            status = conn.execute("SELECT status FROM source_collection_jobs WHERE id='cached-report'").fetchone()[0]
            run = conn.execute(
                "SELECT status,duplicate_count,error FROM source_collection_runs WHERE job_id='cached-report' AND channel_id='cached'"
            ).fetchone()
            linked = conn.execute("SELECT COUNT(*) FROM source_job_snapshots WHERE job_id='cached-report'").fetchone()[0]
        self.assertEqual(status, "review")
        self.assertEqual(run[0], "cached_after_error")
        self.assertEqual(run[1], 1)
        self.assertIn("quota exhausted", run[2])
        self.assertEqual(linked, 1)

    def test_collect_uses_cached_window_snapshots_when_live_source_fails(self) -> None:
        self.insert_channels("cached")
        window = {"channel_id": "cached", "window_start": timestamp(-30), "window_end": timestamp()}
        job = self.insert_job("cached-collect", [window], action="collect")
        with database(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO source_snapshots(id,channel_id,occurred_at,collected_at,source_url,content,scope_type,scope_key)
                VALUES('cached-snapshot','cached',?,?, 'cached://1','{}','general','')
                """,
                (timestamp(-5), timestamp(-4)),
            )

        with patch("backend.worker.collect_channel", side_effect=RuntimeError("quota exhausted")):
            self.worker.execute(job)

        with database(self.db_path) as conn:
            status = conn.execute("SELECT status FROM source_collection_jobs WHERE id='cached-collect'").fetchone()[0]
            run = conn.execute(
                "SELECT status,duplicate_count,error FROM source_collection_runs WHERE job_id='cached-collect' AND channel_id='cached'"
            ).fetchone()
            linked = conn.execute("SELECT COUNT(*) FROM source_job_snapshots WHERE job_id='cached-collect'").fetchone()[0]
        self.assertEqual(status, "partial_completed")
        self.assertEqual(run[0], "cached_after_error")
        self.assertEqual(run[1], 1)
        self.assertIn("quota exhausted", run[2])
        self.assertEqual(linked, 1)

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

    def test_dashboard_source_jobs_exclude_research_child_jobs(self) -> None:
        now = timestamp()
        with self.main.db() as conn:
            conn.execute(
                "INSERT INTO channels(id,name,type,url,collection_mode,status,updated_at) VALUES('dash-source','dash','test','','requests','online',?)",
                (now,),
            )
            conn.execute(
                """
                INSERT INTO source_collection_jobs(id,action,channel_ids,windows,lookback_days,skill_name,report_title,status,created_at)
                VALUES('standalone-source','collect','["dash-source"]','[]',30,'skill','standalone','completed',?)
                """,
                (now,),
            )
            conn.execute(
                """
                INSERT INTO source_collection_jobs(
                  id,action,channel_ids,windows,lookback_days,skill_name,report_title,status,created_at,parent_task_id,query,evidence_layer
                ) VALUES('research-child','collect','["dash-source"]','[]',30,'skill','research child','completed',?,'task-a','300782','akshare')
                """,
                (now,),
            )
        source_job_ids = {job["id"] for job in self.main.dashboard()["source_jobs"]}
        self.assertIn("standalone-source", source_job_ids)
        self.assertNotIn("research-child", source_job_ids)

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

    def test_agent_collect_report_requires_token_and_uses_three_cloud_sources(self) -> None:
        request = Mock(headers={})
        payload = self.main.AgentCollectReportInput(lookback_days=30)
        with self.main.db() as conn:
            for channel_id in self.main.AGENT_DEFAULT_CHANNEL_IDS:
                conn.execute(
                    "INSERT OR REPLACE INTO source_collection_watermarks_v2(channel_id,scope_key,last_success_at) VALUES(?,?,?)",
                    (channel_id, "", timestamp()),
                )
        with patch.dict("os.environ", {"ALPHADESK_AGENT_TOKEN": "agent-secret"}):
            with self.assertRaises(self.main.HTTPException) as raised:
                self.main.agent_collect_report(payload, request)
            self.assertEqual(raised.exception.status_code, 401)

            result = self.main.agent_collect_report(
                payload,
                Mock(headers={"authorization": "Bearer agent-secret"}),
            )

        self.assertEqual(result["status"], "queued")
        self.assertEqual(result["lookback_days"], 30)
        self.assertEqual(tuple(result["channel_ids"]), self.main.AGENT_DEFAULT_CHANNEL_IDS)
        self.assertIn("/evidence", result["evidence_url"])
        with self.main.db() as conn:
            job = conn.execute("SELECT action,channel_ids,windows,lookback_days,report_title FROM source_collection_jobs WHERE id=?", (result["job_id"],)).fetchone()
        self.assertEqual(job["action"], "collect")
        self.assertEqual(json.loads(job["channel_ids"]), list(self.main.AGENT_DEFAULT_CHANNEL_IDS))
        self.assertEqual(job["lookback_days"], 30)
        self.assertEqual(len(json.loads(job["windows"])), 3)
        self.assertIn("三信源", job["report_title"])

    def test_agent_evidence_endpoint_returns_collected_source_context(self) -> None:
        now = timestamp()
        with self.main.db() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO channels(id,name,type,url,collection_mode,status,updated_at) VALUES('wechat-mp-rss','微信公众号','rss','','wechat_rss','online',?)",
                (now,),
            )
            conn.execute(
                """
                INSERT INTO source_collection_jobs(
                  id,action,channel_ids,windows,lookback_days,skill_name,report_title,status,created_at,started_at,completed_at
                ) VALUES('agent-evidence','collect','["wechat-mp-rss"]','[]',30,'skill','evidence','completed',?,?,?)
                """,
                (now, now, now),
            )
            conn.execute(
                """
                INSERT INTO source_collection_runs(job_id,channel_id,status,completed_at,snapshot_count)
                VALUES('agent-evidence','wechat-mp-rss','completed',?,1)
                """,
                (now,),
            )
            conn.execute(
                """
                INSERT INTO source_snapshots(id,channel_id,occurred_at,collected_at,source_url,content,normalized_item_count)
                VALUES('snap-agent','wechat-mp-rss',?,?,?,'raw content',1)
                """,
                (now, now, "https://mp.weixin.qq.com/s/demo"),
            )
            conn.execute("INSERT INTO source_job_snapshots(job_id,snapshot_id) VALUES('agent-evidence','snap-agent')")
            conn.execute(
                """
                INSERT INTO normalized_source_items(
                  id,snapshot_id,channel_id,item_key,occurred_at,author,title,content,source_url,
                  metadata,quality_score,normalization_mode,created_at
                ) VALUES('item-agent','snap-agent','wechat-mp-rss','item',?,'调研纪要','AI 算力','光模块与材料涨价','https://mp.weixin.qq.com/s/demo',
                  '{"source_account":{"name":"调研纪要"}}',88,'fixed',?)
                """,
                (now, now),
            )

        with patch.dict("os.environ", {"ALPHADESK_AGENT_TOKEN": "agent-secret"}):
            evidence = self.main.agent_job_evidence(
                "agent-evidence",
                Mock(headers={"authorization": "Bearer agent-secret"}),
                limit_per_channel=3,
                preview_chars=500,
            )

        self.assertEqual(evidence["job"]["action"], "collect")
        self.assertEqual(evidence["attached_snapshot_counts"], [{"channel_id": "wechat-mp-rss", "count": 1}])
        self.assertEqual(evidence["selected_items"][0]["kind"], "normalized")
        self.assertEqual(evidence["selected_items"][0]["source_label"], "微信公众号：调研纪要")
        self.assertIn("光模块", evidence["selected_items"][0]["content_preview"])

    def test_agent_report_status_and_latest_report_are_token_protected(self) -> None:
        now = timestamp()
        with self.main.db() as conn:
            conn.execute(
                """
                INSERT INTO source_collection_jobs(
                  id,action,channel_ids,windows,lookback_days,skill_name,report_title,status,
                  created_at,completed_at,report,report_anchor
                ) VALUES('agent-report','collect_report',?, '[]',30,'skill','agent report','review',?,?,?,?)
                """,
                (
                    json.dumps(list(self.main.AGENT_DEFAULT_CHANNEL_IDS)),
                    now,
                    now,
                    "<html><head></head><body>ok</body></html>",
                    now,
                ),
            )
            conn.execute(
                """
                INSERT INTO source_collection_runs(job_id,channel_id,status,completed_at,snapshot_count)
                VALUES('agent-report','zsxq','completed',?,1)
                """,
                (now,),
            )

        with patch.dict("os.environ", {"ALPHADESK_AGENT_TOKEN": "agent-secret"}):
            status = self.main.agent_job_status("agent-report", Mock(headers={"x-agent-token": "agent-secret"}))
            latest = self.main.agent_latest_report(Mock(headers={"authorization": "Bearer agent-secret"}))

        self.assertTrue(status["report_ready"])
        self.assertIsNone(status["report"])
        self.assertEqual(status["runs"][0]["channel_id"], "zsxq")
        self.assertEqual(latest["id"], "agent-report")
        self.assertIn("<html>", latest["report"])

    def test_env_model_provider_is_seeded_for_cloud_bootstrap(self) -> None:
        with self.main.db() as conn:
            conn.execute("DELETE FROM model_providers")
        with patch.dict(
            "os.environ",
            {
                "ALPHADESK_MODEL_API_KEY": "env-model-secret",
                "ALPHADESK_MODEL_BASE_URL": "https://gateway.example/v1",
                "ALPHADESK_MODEL_NAME": "env-chat",
                "ALPHADESK_MODEL_PROTOCOL": "openai_chat_completions",
            },
        ):
            self.main.init_db()

        provider = self.main.provider_row()
        self.assertIsNotNone(provider)
        self.assertEqual(provider["id"], "env-default")
        self.assertEqual(provider["base_url"], "https://gateway.example/v1")
        self.assertEqual(provider["model"], "env-chat")
        self.assertTrue(provider["encrypted_api_key"])

    def test_env_model_provider_infers_api_key_family_defaults(self) -> None:
        with patch.dict("os.environ", {"OPENAI_API_KEY": "openai-secret"}, clear=True):
            openai_config = self.main.env_model_provider_config()
        self.assertEqual(openai_config["protocol"], "openai_chat_completions")
        self.assertEqual(openai_config["base_url"], "https://api.openai.com/v1")
        self.assertEqual(openai_config["model"], "gpt-4o-mini")

        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "anthropic-secret"}, clear=True):
            anthropic_config = self.main.env_model_provider_config()
        self.assertEqual(anthropic_config["protocol"], "anthropic_messages")
        self.assertEqual(anthropic_config["base_url"], "https://api.anthropic.com")
        self.assertEqual(anthropic_config["model"], "claude-3-5-sonnet-latest")

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

    def test_source_job_snapshot_fallback_uses_job_scope(self) -> None:
        started_at = timestamp(-2)
        collected_at = timestamp(-1)
        completed_at = timestamp()
        with self.main.db() as conn:
            conn.execute(
                "INSERT INTO channels(id,name,type,url,collection_mode,status,updated_at) VALUES('fallback-scope','fallback','test','','requests','online',?)",
                (collected_at,),
            )
            conn.execute(
                """
                INSERT INTO source_collection_jobs(
                  id,action,channel_ids,windows,lookback_days,skill_name,report_title,status,created_at,started_at,completed_at
                ) VALUES('fallback-job','collect','["fallback-scope"]','[]',30,'skill','fallback','completed',?,?,?)
                """,
                (started_at, started_at, completed_at),
            )
            conn.executemany(
                """
                INSERT INTO source_snapshots(id,channel_id,occurred_at,collected_at,source_url,content,scope_type,scope_key)
                VALUES(?,?,?,?,?,?,?,?)
                """,
                [
                    ("fallback-general", "fallback-scope", collected_at, collected_at, "fallback://general", "GENERAL_ONLY", "general", ""),
                    ("fallback-research", "fallback-scope", collected_at, collected_at, "fallback://research", "RESEARCH_CHILD", "research", "300782"),
                ],
            )
        snapshots = self.main.source_job_snapshots("fallback-job")["snapshots"]
        snapshot_ids = {item["id"] for item in snapshots}
        self.assertEqual(snapshot_ids, {"fallback-general"})

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

    def test_fixed_normalizer_preserves_zsxq_mcp_topic_fields(self) -> None:
        snapshot = {
            "channel_id": "zsxq",
            "occurred_at": "2026-06-12T10:33:08+08:00",
            "collected_at": "2026-06-12T10:34:00+08:00",
            "source_url": "zsxq://group/28888222124181/topic/22255225848488181",
            "content": json.dumps(
                {
                    "platform": "zsxq_mcp",
                    "adapter": "zsxq_mcp",
                    "query": "半导体",
                    "collection_window": {},
                    "topic": {
                        "topic_id": "22255225848488181",
                        "title": "午盘0612",
                        "type": "talk",
                        "create_time": "2026-06-12T10:33:08.636+0800",
                        "content": "风险偏好在持续小幅回升",
                        "owner": {"user_id": "88842481581212", "name": "橙子不糊涂"},
                        "group": {"group_id": "28888222124181", "name": "橙子不糊涂的科技花园"},
                        "counts": {"likes": 127, "comments": 0},
                        "files": [{"file_id": "file-1", "name": "report.pdf"}],
                    },
                },
                ensure_ascii=False,
            ),
        }
        items, note = self.main.fixed_normalized_items(snapshot)
        self.assertEqual(note, "")
        self.assertEqual(items[0]["item_key"], "zsxq:22255225848488181")
        self.assertEqual(items[0]["author"], "橙子不糊涂")
        self.assertEqual(items[0]["title"], "午盘0612")
        self.assertEqual(items[0]["content"], "风险偏好在持续小幅回升")
        self.assertEqual(items[0]["attachments"], ["report.pdf"])
        self.assertEqual(items[0]["metadata"]["group"]["group_id"], "28888222124181")
        self.assertGreaterEqual(items[0]["quality_score"], 90)

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

    def test_fixed_normalizer_splits_x_twtapi_results(self) -> None:
        snapshot = {
            "channel_id": "x-twtapi",
            "occurred_at": "2026-06-04T13:00:00+08:00",
            "collected_at": "2026-06-04T13:00:00+08:00",
            "source_url": "https://x.com/search?q=601869&f=live",
            "content": json.dumps(
                {
                    "platform": "x_twtapi",
                    "adapter": "twtapi_search",
                    "query": "长飞光纤 601869",
                    "collection_window": {},
                    "tweets": [
                        {
                            "id": "123",
                            "text": "长飞光纤订单与产能讨论",
                            "created_at": "2026-06-04T12:00:00+08:00",
                            "user": {"screen_name": "market_watch", "name": "Market Watch"},
                        }
                    ],
                },
                ensure_ascii=False,
            ),
        }
        items, note = self.main.fixed_normalized_items(snapshot)
        self.assertEqual(note, "")
        self.assertEqual(items[0]["content"], "长飞光纤订单与产能讨论")
        self.assertEqual(items[0]["author"], "@market_watch")
        self.assertEqual(items[0]["source_url"], "https://x.com/market_watch/status/123")
        self.assertEqual(items[0]["metadata"]["platform"], "x_twtapi")

    def test_ima_config_is_encrypted_masked_and_preserved(self) -> None:
        saved = self.main.update_ima_config(
            self.main.ImaConfigInput(
                client_id="client-a",
                api_key="secret-a",
                skill_download_url="https://example.com/ima-skills.zip",
            )
        )
        self.assertEqual(saved["config"]["api_key"], self.main.MASKED_SECRET)
        self.assertTrue(saved["config"]["api_key_configured"])
        self.assertEqual(saved["config"]["client_id"], "client-a")
        with self.main.db() as conn:
            encrypted = conn.execute(
                "SELECT encrypted_config FROM channel_request_configs WHERE channel_id='ima-knowledge'"
            ).fetchone()["encrypted_config"]
        self.assertNotIn("secret-a", encrypted)
        self.assertEqual(self.main.channel_request_config("ima-knowledge")["api_key"], "secret-a")

        self.main.update_ima_config(
            self.main.ImaConfigInput(
                client_id="client-b",
                api_key=self.main.MASKED_SECRET,
                skill_download_url="https://example.com/ima-skills-2.zip",
            )
        )
        preserved = self.main.channel_request_config("ima-knowledge")
        self.assertEqual(preserved["client_id"], "client-b")
        self.assertEqual(preserved["api_key"], "secret-a")
        self.assertEqual(preserved["skill_download_url"], "https://example.com/ima-skills-2.zip")

        cleared = self.main.update_ima_config(
            self.main.ImaConfigInput(client_id="client-b", api_key="", clear_credentials=True)
        )
        self.assertFalse(cleared["config"]["api_key_configured"])
        self.assertEqual(self.main.channel_request_config("ima-knowledge")["api_key"], "")

    def test_itick_config_preserves_online_status_when_credentials_remain(self) -> None:
        with self.main.db() as conn:
            conn.execute("UPDATE channels SET status='online' WHERE id='itick'")
        self.main.update_itick_config(
            self.main.ItickConfigInput(api_key="secret-token", default_symbols=["SH:600519"])
        )
        with self.main.db() as conn:
            status = conn.execute("SELECT status FROM channels WHERE id='itick'").fetchone()["status"]
        self.assertEqual(status, "online")
        self.main.update_itick_config(self.main.ItickConfigInput(clear_credentials=True))
        with self.main.db() as conn:
            status = conn.execute("SELECT status FROM channels WHERE id='itick'").fetchone()["status"]
        self.assertEqual(status, "pending")

    def test_x_twtapi_config_preserves_online_status_when_credentials_remain(self) -> None:
        with self.main.db() as conn:
            conn.execute("UPDATE channels SET status='online' WHERE id='x-twtapi'")
        self.main.update_x_twtapi_config(
            self.main.TwtApiConfigInput(
                api_key="secret-token",
                default_queries=["A股"],
                tracked_users=["https://x.com/aleabitoreddit"],
            )
        )
        saved_config = self.main.channel_request_config("x-twtapi")
        self.assertEqual(saved_config["tracked_users"], ["aleabitoreddit"])
        with self.main.db() as conn:
            status = conn.execute("SELECT status FROM channels WHERE id='x-twtapi'").fetchone()["status"]
        self.assertEqual(status, "online")
        self.main.update_x_twtapi_config(self.main.TwtApiConfigInput(clear_credentials=True))
        with self.main.db() as conn:
            status = conn.execute("SELECT status FROM channels WHERE id='x-twtapi'").fetchone()["status"]
        self.assertEqual(status, "pending")

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
                "UPDATE channels SET url='https://example.test/login',collection_mode='playwright' WHERE id='web-rumors'"
            )
        with patch.object(self.main, "BROWSER_WORKSPACE_PUBLIC_URL", "http://127.0.0.1:7900/vnc.html"), patch.object(
            self.main.subprocess,
            "Popen",
        ) as popen:
            result = self.main.launch_channel_login("web-rumors")
        self.assertEqual(result["login_url"], "http://127.0.0.1:7900/vnc.html")
        popen.assert_called_once()

    def test_playwright_login_reuses_running_browser_process(self) -> None:
        with self.main.db() as conn:
            conn.execute(
                "UPDATE channels SET url='https://example.test/login',collection_mode='playwright' WHERE id='web-rumors'"
            )
        process = Mock()
        process.poll.return_value = None
        with patch.object(self.main.subprocess, "Popen", return_value=process) as popen:
            self.main.launch_channel_login("web-rumors")
            result = self.main.launch_channel_login("web-rumors")
        self.assertEqual(result["status"], "opened")
        popen.assert_called_once()

    def test_zsxq_mcp_config_is_encrypted_masked_and_preserved(self) -> None:
        saved = self.main.update_zsxq_mcp_config(
            self.main.ZsxqMcpConfigInput(
                mcp_url="https://mcp.example.test/topic/mcp?api_key=secret-a",
                timeout_seconds=12,
                page_limit=7,
                max_pages=3,
                include_comments=True,
            )
        )
        self.assertTrue(saved["config"]["mcp_url_configured"])
        self.assertNotIn("secret-a", saved["config"].get("mcp_url_display", ""))
        stored = self.main.channel_request_config("zsxq")
        self.assertEqual(stored["mcp_url"], "https://mcp.example.test/topic/mcp?api_key=secret-a")
        self.assertEqual(stored["page_limit"], 7)
        with self.main.db() as conn:
            row = conn.execute("SELECT collection_mode,url,group_ids,parsing_strategy FROM channels WHERE id='zsxq'").fetchone()
        self.assertEqual(row["collection_mode"], "zsxq_mcp")
        self.assertEqual(row["url"], "mcp://zsxq")
        self.assertEqual(json.loads(row["group_ids"]), ["28888222124181"])
        self.assertEqual(row["parsing_strategy"], "fixed")

        self.main.update_zsxq_mcp_config(self.main.ZsxqMcpConfigInput(page_limit=9, max_pages=2))
        self.assertEqual(self.main.channel_request_config("zsxq")["mcp_url"], "https://mcp.example.test/topic/mcp?api_key=secret-a")

        cleared = self.main.update_zsxq_mcp_config(self.main.ZsxqMcpConfigInput(clear_credentials=True))
        self.assertFalse(cleared["config"]["mcp_url_configured"])
        self.assertEqual(self.main.channel_request_config("zsxq")["mcp_url"], "")

    def test_zsxq_check_uses_mcp_status_without_browser_subprocess(self) -> None:
        expected = {
            "status": "online",
            "message": "ok",
            "checked_at": timestamp(),
            "group_id": "28888222124181",
        }
        with patch.object(self.main, "zsxq_mcp_status", return_value=expected) as status_check, patch.object(
            self.main.subprocess,
            "run",
        ) as run:
            result = self.main.check_channel("zsxq")
        self.assertEqual(result["status"], "online")
        status_check.assert_called_once()
        run.assert_not_called()

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

    def test_clear_source_jobs_keeps_research_child_jobs(self) -> None:
        now = timestamp()
        with self.main.db() as conn:
            conn.execute(
                "INSERT INTO channels(id,name,type,url,collection_mode,status,updated_at) VALUES('clear-source','clear','test','','requests','online',?)",
                (now,),
            )
            conn.execute(
                "INSERT INTO tasks(id,title,target,objective,status,created_at) VALUES('task-clear','研究','300782','objective','review',?)",
                (now,),
            )
            conn.execute(
                """
                INSERT INTO source_collection_jobs(id,action,channel_ids,windows,lookback_days,skill_name,report_title,status,created_at)
                VALUES('clear-standalone','report','["clear-source"]','[]',30,'skill','standalone','review',?)
                """,
                (now,),
            )
            conn.execute(
                """
                INSERT INTO source_collection_jobs(
                  id,action,channel_ids,windows,lookback_days,skill_name,report_title,status,created_at,parent_task_id,query,evidence_layer
                ) VALUES('clear-child','collect','["clear-source"]','[]',30,'skill','child','completed',?,'task-clear','300782','akshare')
                """,
                (now,),
            )
            conn.execute(
                "INSERT INTO source_collection_runs(job_id,channel_id,status) VALUES('clear-standalone','clear-source','completed')"
            )
            conn.execute(
                "INSERT INTO source_collection_runs(job_id,channel_id,status) VALUES('clear-child','clear-source','completed')"
            )
        result = self.main.clear_task_list("source-jobs")
        self.assertEqual(result["deleted_source_jobs"], 1)
        with self.main.db() as conn:
            remaining_jobs = {row["id"] for row in conn.execute("SELECT id FROM source_collection_jobs")}
            remaining_runs = {row["job_id"] for row in conn.execute("SELECT job_id FROM source_collection_runs")}
        self.assertEqual(remaining_jobs, {"clear-child"})
        self.assertEqual(remaining_runs, {"clear-child"})

    def test_research_layer_mapping_honors_enabled_market_and_werss_channels(self) -> None:
        with self.main.db() as conn:
            conn.execute("UPDATE channels SET status='offline',research_enabled=0")
            conn.execute("UPDATE channels SET status='online',research_enabled=1 WHERE id IN ('akshare','itick','wechat-mp-rss','x-twtapi')")
        self.assertEqual(set(self.main.channel_ids_for_layer("market_data")), {"akshare", "itick"})
        self.assertEqual(set(self.main.channel_ids_for_layer("akshare")), {"akshare", "itick"})
        self.assertIn("wechat-mp-rss", self.main.channel_ids_for_layer("http_requests"))
        self.assertIn("x-twtapi", self.main.channel_ids_for_layer("http_requests"))

    def test_stock_realtime_sources_bypass_recent_collection_watermark(self) -> None:
        current = timestamp()
        with self.main.db() as conn:
            conn.execute("UPDATE channels SET status='offline',research_enabled=0")
            conn.execute("UPDATE channels SET status='online',research_enabled=1 WHERE id IN ('akshare','itick','industry-news','x-twtapi')")
            conn.execute(
                "INSERT INTO channels(id,name,type,url,collection_mode,status,research_enabled,updated_at) VALUES('plain-http','plain','test','https://example.test/{query}','requests','online',1,?)",
                (current,),
            )
            conn.executemany(
                "INSERT OR REPLACE INTO source_collection_watermarks_v2(channel_id,scope_key,last_success_at) VALUES(?,?,?)",
                [
                    ("akshare", "300782", current),
                    ("itick", "300782", current),
                    ("industry-news", "300782", current),
                    ("x-twtapi", "300782", current),
                    ("plain-http", "300782", current),
                ],
            )
        result = self.main.create_source_job(
            self.main.SourceJobInput(
                action="collect",
                channel_ids=["akshare", "itick", "industry-news", "x-twtapi", "plain-http"],
                lookback_days=30,
                parent_task_id="task-realtime",
                query="300782",
                evidence_layer="market_data",
            )
        )
        self.assertEqual(result["status"], "queued")
        self.assertEqual({window["channel_id"] for window in result["windows"]}, {"akshare", "itick", "industry-news", "x-twtapi"})

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

    def test_stock_research_forces_market_data_before_final_report(self) -> None:
        now = timestamp()
        with self.main.db() as conn:
            conn.execute("UPDATE channels SET status='offline',research_enabled=0")
            conn.execute("UPDATE channels SET status='online',research_enabled=1 WHERE id='akshare'")
            conn.execute(
                "INSERT INTO tasks(id,title,target,objective,status,created_at) VALUES('task-force-market','研究','300782','objective','analyzing',?)",
                (now,),
            )
        decision = self.main.AgentDecision(
            decision="final",
            report="<html><head></head><body>too early</body></html>",
            used_model_knowledge=False,
        )
        with patch.object(self.main, "refresh_general_sources_before_research", return_value=None), patch.object(
            self.main,
            "task_snapshot_context",
            return_value=(now, timestamp(-30), "context"),
        ), patch.object(self.main, "call_provider_structured", return_value=decision):
            result = self.main.advance_analysis_task("task-force-market")
        self.assertEqual(result["status"], "evidence_queued")
        self.assertEqual(result["evidence_layer"], "market_data")
        with self.main.db() as conn:
            job = conn.execute(
                "SELECT channel_ids,query,evidence_layer FROM source_collection_jobs WHERE parent_task_id='task-force-market'"
            ).fetchone()
        self.assertEqual(json.loads(job["channel_ids"]), ["akshare"])
        self.assertEqual(job["query"], "300782")
        self.assertEqual(job["evidence_layer"], "market_data")

    def test_legacy_akshare_evidence_layer_counts_as_market_data(self) -> None:
        with self.main.db() as conn:
            conn.execute(
                """
                INSERT INTO source_collection_jobs(id,action,channel_ids,windows,lookback_days,skill_name,report_title,status,created_at,parent_task_id,query,evidence_layer)
                VALUES('legacy-market','collect','["akshare"]','[]',30,'skill','legacy','completed',?,'task-legacy','300782','akshare')
                """,
                (timestamp(),),
            )
        completed = self.main.completed_evidence_layers("task-legacy", {"completed_layers": ["akshare"]})
        self.assertIn("market_data", completed)
        self.assertNotIn("akshare", completed)

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

    def test_general_source_report_excludes_research_scoped_snapshots(self) -> None:
        now = timestamp()
        with self.main.db() as conn:
            conn.execute(
                "INSERT INTO channels(id,name,type,url,collection_mode,status,updated_at) VALUES(?,?,?,?,?,?,?)",
                ("report-scope", "report-scope", "test", "", "requests", "online", now),
            )
            conn.executemany(
                """
                INSERT INTO source_snapshots(id,channel_id,occurred_at,collected_at,source_url,content,scope_type,scope_key)
                VALUES(?,?,?,?,?,?,?,?)
                """,
                [
                    ("report-general", "report-scope", now, now, "report://general", "GENERAL_REPORT_CONTENT", "general", ""),
                    ("report-research", "report-scope", now, now, "report://research", "RESEARCH_TASK_CONTENT", "research", "300782"),
                ],
            )

        def capture_prompt(prompt, system_prompt="", purpose=""):
            self.assertIn("GENERAL_REPORT_CONTENT", prompt)
            self.assertNotIn("RESEARCH_TASK_CONTENT", prompt)
            self.assertEqual(purpose, "source_report")
            return "<html><head></head><body>ok</body></html>"

        with patch.object(self.main, "call_provider", side_effect=capture_prompt):
            self.main.generate_source_report(
                self.main.SourceJobInput(action="report", channel_ids=["report-scope"], report_title="general only")
            )

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
    class AnthropicStructuredProbe(BaseModel):
        status: str
        confidence: int

    class FakeAnthropicResponse:
        def __init__(self, payload: dict, status_code: int = 200) -> None:
            self.payload = payload
            self.status_code = status_code
            self.text = json.dumps(payload, ensure_ascii=False)

        def json(self):
            return self.payload

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

    def test_anthropic_text_uses_messages_api(self) -> None:
        gateway = ModelGateway(timeout_seconds=11, connect_timeout_seconds=3, network_retries=0)
        config = ProviderRuntimeConfig(
            id="anthropic",
            base_url="https://api.anthropic.com",
            model="claude-test",
            protocol="anthropic_messages",
            api_key="secret",
            extra_body={"max_tokens": 123, "top_p": 0.8, "anthropic_version": "2023-06-01", "anthropic_beta": "test-beta"},
        )
        calls = []

        def fake_post(url, **kwargs):
            calls.append({"url": url, **kwargs})
            return self.FakeAnthropicResponse(
                {
                    "content": [{"type": "text", "text": "模型通道可用"}],
                    "usage": {"input_tokens": 7, "output_tokens": 4},
                }
            )

        with patch("backend.model_gateway.requests.post", side_effect=fake_post):
            result = gateway.run_text(config, "只回复：模型通道可用", instructions="system rules")

        self.assertEqual(result.output, "模型通道可用")
        self.assertEqual(result.input_tokens, 7)
        self.assertEqual(result.output_tokens, 4)
        call = calls[0]
        self.assertEqual(call["url"], "https://api.anthropic.com/v1/messages")
        self.assertEqual(call["headers"]["x-api-key"], "secret")
        self.assertEqual(call["headers"]["anthropic-version"], "2023-06-01")
        self.assertEqual(call["headers"]["anthropic-beta"], "test-beta")
        self.assertEqual(call["timeout"], (3, 11))
        self.assertEqual(call["json"]["model"], "claude-test")
        self.assertEqual(call["json"]["system"], "system rules")
        self.assertEqual(call["json"]["messages"], [{"role": "user", "content": "只回复：模型通道可用"}])
        self.assertEqual(call["json"]["max_tokens"], 123)
        self.assertEqual(call["json"]["temperature"], 0.2)
        self.assertEqual(call["json"]["top_p"], 0.8)
        self.assertNotIn("anthropic_version", call["json"])
        self.assertNotIn("anthropic_beta", call["json"])

    def test_anthropic_structured_uses_forced_tool_schema(self) -> None:
        gateway = ModelGateway(timeout_seconds=11, connect_timeout_seconds=3, network_retries=0)
        config = ProviderRuntimeConfig(
            id="anthropic",
            base_url="https://gateway.example/v1",
            model="claude-test",
            protocol="anthropic_messages",
            api_key="secret",
            extra_body={},
        )
        calls = []

        def fake_post(url, **kwargs):
            calls.append({"url": url, **kwargs})
            return self.FakeAnthropicResponse(
                {
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "emit_structured_response",
                            "input": {"status": "ok", "confidence": 91},
                        }
                    ],
                    "usage": {"input_tokens": 9, "output_tokens": 6},
                }
            )

        with patch("backend.model_gateway.requests.post", side_effect=fake_post):
            result = gateway.run_structured(
                config,
                "返回结构化状态",
                instructions="system rules",
                output_type=self.AnthropicStructuredProbe,
            )

        self.assertEqual(result.output.status, "ok")
        self.assertEqual(result.output.confidence, 91)
        call = calls[0]
        self.assertEqual(call["url"], "https://gateway.example/v1/messages")
        self.assertEqual(call["json"]["tool_choice"], {"type": "tool", "name": "emit_structured_response"})
        self.assertEqual(call["json"]["tools"][0]["name"], "emit_structured_response")
        self.assertIn("confidence", call["json"]["tools"][0]["input_schema"]["properties"])


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
