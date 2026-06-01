from __future__ import annotations

import json
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Callable
from uuid import uuid4

from backend.collectors import collect_channel


class CollectionWorker:
    def __init__(
        self,
        *,
        db_path: Path,
        profile_for: Callable[[str], Path],
        report_after_collection: Callable[[dict], tuple[str, str]],
        normalize_snapshot: Callable[[str], dict] | None = None,
        on_evidence_ready: Callable[[str], None] | None = None,
        request_config_for: Callable[[str], dict] | None = None,
        poll_seconds: float = 1.5,
    ) -> None:
        self.db_path = db_path
        self.profile_for = profile_for
        self.report_after_collection = report_after_collection
        self.normalize_snapshot = normalize_snapshot
        self.on_evidence_ready = on_evidence_ready
        self.request_config_for = request_config_for
        self.poll_seconds = poll_seconds
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=20)
        conn.row_factory = sqlite3.Row
        return conn

    def start(self) -> None:
        if self.thread and self.thread.is_alive():
            return
        self.thread = threading.Thread(target=self.run, daemon=True, name="source-collection-worker")
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()

    def run(self) -> None:
        while not self.stop_event.is_set():
            job = self.claim_next()
            if job:
                if job.pop("_resume_report", False):
                    self.generate_report(job)
                else:
                    self.execute(job)
            else:
                self.stop_event.wait(self.poll_seconds)

    def claim_next(self) -> dict | None:
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT * FROM source_collection_jobs
                WHERE status='queued'
                   OR (action='collect_report' AND report IS NULL
                       AND status IN ('completed','deduplicated','generating_report'))
                ORDER BY created_at LIMIT 1
                """
            ).fetchone()
            if not row:
                return None
            if row["status"] != "queued":
                conn.execute(
                    "UPDATE source_collection_jobs SET status='generating_report',error='' WHERE id=?",
                    (row["id"],),
                )
                resumed = dict(conn.execute("SELECT * FROM source_collection_jobs WHERE id=?", (row["id"],)).fetchone())
                resumed["_resume_report"] = True
                return resumed
            started_at = datetime.now().astimezone().isoformat(timespec="seconds")
            conn.execute(
                "UPDATE source_collection_jobs SET status='running',started_at=?,error='' WHERE id=? AND status='queued'",
                (started_at, row["id"]),
            )
            return dict(conn.execute("SELECT * FROM source_collection_jobs WHERE id=?", (row["id"],)).fetchone())

    def execute(self, job: dict) -> None:
        snapshots: list[dict] = []
        successful_channels: set[str] = set()
        try:
            with self.connect() as conn:
                channels = {row["id"]: dict(row) for row in conn.execute("SELECT * FROM channels")}
            for channel in channels.values():
                channel["group_ids"] = json.loads(channel.get("group_ids") or "[]")
                if self.request_config_for:
                    channel["request_config"] = self.request_config_for(channel["id"])
            for window in json.loads(job["windows"]):
                channel = channels[window["channel_id"]]
                try:
                    channel_snapshots = collect_channel(
                        channel,
                        window,
                        self.profile_for(channel["id"]),
                        job.get("query", ""),
                    )
                except Exception:
                    if (channel.get("request_config") or {}).get("adapter") == "mx_authorized_request_replay":
                        checked_at = datetime.now().astimezone().isoformat(timespec="seconds")
                        with self.connect() as conn:
                            conn.execute(
                                "UPDATE channels SET status='offline',last_check=?,updated_at=? WHERE id=?",
                                (checked_at, checked_at, channel["id"]),
                            )
                    raise
                if not channel_snapshots:
                    raise RuntimeError(f"No snapshots collected for channel {channel['id']}")
                snapshots.extend(channel_snapshots)
                successful_channels.add(channel["id"])
            self.complete(job, snapshots, successful_channels)
        except Exception as exc:
            with self.connect() as conn:
                conn.execute(
                    "UPDATE source_collection_jobs SET status='failed',error=?,completed_at=? WHERE id=?",
                    (str(exc)[:1200], datetime.now().astimezone().isoformat(timespec="seconds"), job["id"]),
                )
                if job.get("parent_task_id"):
                    conn.execute(
                        "UPDATE tasks SET status='agent_failed',agent_error=? WHERE id=?",
                        (f"证据采集失败: {exc}"[:1200], job["parent_task_id"]),
                    )

    def complete(self, job: dict, snapshots: list[dict], successful_channels: set[str]) -> None:
        collected_at = datetime.now().astimezone().isoformat(timespec="seconds")
        scope_type = "research" if job.get("parent_task_id") or job.get("query") or job.get("evidence_layer") else "general"
        scope_key = job.get("query", "") if scope_type == "research" else ""
        inserted = 0
        inserted_channels: set[str] = set()
        inserted_snapshot_ids: list[str] = []
        with self.connect() as conn:
            for item in snapshots:
                before = conn.total_changes
                snapshot_id = uuid4().hex[:12]
                conn.execute(
                    """
                    INSERT OR IGNORE INTO source_snapshots(
                      id,channel_id,occurred_at,collected_at,source_url,content,scope_type,scope_key
                    ) VALUES(?,?,?,?,?,?,?,?)
                    """,
                    (
                        snapshot_id,
                        item["channel_id"],
                        item["occurred_at"],
                        collected_at,
                        item["source_url"],
                        item["content"],
                        scope_type,
                        scope_key,
                    ),
                )
                delta = conn.total_changes - before
                inserted += delta
                if delta:
                    inserted_channels.add(item["channel_id"])
                    inserted_snapshot_ids.append(snapshot_id)
                else:
                    existing = conn.execute(
                        "SELECT id FROM source_snapshots WHERE channel_id=? AND source_url=? AND occurred_at=?",
                        (item["channel_id"], item["source_url"], item["occurred_at"]),
                    ).fetchone()
                    if not existing:
                        continue
                    snapshot_id = existing["id"]
                    if scope_type == "general":
                        conn.execute(
                            "UPDATE source_snapshots SET scope_type='general',scope_key='' WHERE id=?",
                            (snapshot_id,),
                        )
                conn.execute(
                    "INSERT OR IGNORE INTO source_job_snapshots(job_id,snapshot_id) VALUES(?,?)",
                    (job["id"], snapshot_id),
                )
            for window in json.loads(job["windows"]):
                if window["channel_id"] in inserted_channels:
                    conn.execute(
                        "INSERT OR REPLACE INTO source_collection_watermarks_v2(channel_id,scope_key,last_success_at) VALUES(?,?,?)",
                        (window["channel_id"], job.get("query", "") or "", window["window_end"]),
                    )
            status = "generating_report" if job["action"] == "collect_report" else "completed" if inserted else "deduplicated"
            conn.execute(
                "UPDATE source_collection_jobs SET status=?,snapshot_count=?,completed_at=? WHERE id=?",
                (status, inserted, collected_at, job["id"]),
            )
            if job.get("parent_task_id"):
                conn.execute(
                    "UPDATE tasks SET status='evidence_ready' WHERE id=?",
                    (job["parent_task_id"],),
                )
        if self.normalize_snapshot:
            for snapshot_id in inserted_snapshot_ids:
                self.normalize_snapshot(snapshot_id)
        if job["action"] == "collect_report":
            self.generate_report(job)
        if job.get("parent_task_id") and self.on_evidence_ready:
            try:
                self.on_evidence_ready(job["parent_task_id"])
            except Exception as exc:
                with self.connect() as conn:
                    conn.execute(
                        "UPDATE tasks SET status='agent_failed',agent_error=? WHERE id=?",
                        (str(exc)[:1200], job["parent_task_id"]),
                    )

    def generate_report(self, job: dict) -> None:
        try:
            report, anchor = self.report_after_collection(job)
            completed_at = datetime.now().astimezone().isoformat(timespec="seconds")
            with self.connect() as conn:
                conn.execute(
                    """
                    UPDATE source_collection_jobs
                    SET status='review',report=?,report_anchor=?,completed_at=?,error=''
                    WHERE id=?
                    """,
                    (report, anchor, completed_at, job["id"]),
                )
        except Exception as exc:
            with self.connect() as conn:
                conn.execute(
                    "UPDATE source_collection_jobs SET status='report_failed',error=?,completed_at=? WHERE id=?",
                    (str(exc)[:1200], datetime.now().astimezone().isoformat(timespec="seconds"), job["id"]),
                )
