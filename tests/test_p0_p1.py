from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from backend.collectors import collect_http
from backend.industry_news_sources import collect_public_industry_news
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


class MainBehaviorTests(unittest.TestCase):
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
