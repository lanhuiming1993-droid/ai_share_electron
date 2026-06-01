from __future__ import annotations

import json
import re
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Callable
from uuid import uuid4

from backend.collectors import collect_channel


def current_time() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def canonical_scope_key(query: str) -> str:
    value = str(query or "").strip()
    code_match = re.search(r"(?<!\d)(\d{6})(?!\d)", value)
    return code_match.group(1) if code_match else value.casefold()


def snapshots_cover_window(snapshots: list[dict]) -> bool:
    for snapshot in snapshots:
        try:
            payload = json.loads(snapshot.get("content") or "")
        except json.JSONDecodeError:
            continue
        if payload.get("category") != "collector_diagnostics":
            continue
        errors = payload.get("metadata", {}).get("errors", {})
        if any(str(key).endswith("_coverage") for key in errors):
            return False
    return True


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

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.db_path, timeout=20)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

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
                   OR (action IN ('report','collect_report') AND report IS NULL
                       AND status IN ('completed','deduplicated','partial_completed','generating_report'))
                ORDER BY created_at LIMIT 1
                """
            ).fetchone()
            if not row:
                return None
            if row["status"] != "queued":
                conn.execute(
                    "UPDATE source_collection_jobs SET status='generating_report' WHERE id=?",
                    (row["id"],),
                )
                resumed = dict(conn.execute("SELECT * FROM source_collection_jobs WHERE id=?", (row["id"],)).fetchone())
                resumed["_resume_report"] = True
                return resumed
            started_at = current_time()
            conn.execute(
                "UPDATE source_collection_jobs SET status='running',started_at=?,error='' WHERE id=? AND status='queued'",
                (started_at, row["id"]),
            )
            return dict(conn.execute("SELECT * FROM source_collection_jobs WHERE id=?", (row["id"],)).fetchone())

    def record_run(
        self,
        job_id: str,
        channel_id: str,
        status: str,
        *,
        started_at: str = "",
        snapshot_count: int = 0,
        duplicate_count: int = 0,
        error: str = "",
        coverage_complete: bool = True,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO source_collection_runs(
                  job_id,channel_id,status,started_at,completed_at,snapshot_count,duplicate_count,error,coverage_complete
                ) VALUES(?,?,?,?,?,?,?,?,?)
                ON CONFLICT(job_id,channel_id) DO UPDATE SET
                  status=excluded.status,
                  started_at=CASE WHEN excluded.started_at<>'' THEN excluded.started_at ELSE source_collection_runs.started_at END,
                  completed_at=excluded.completed_at,
                  snapshot_count=excluded.snapshot_count,
                  duplicate_count=excluded.duplicate_count,
                  error=excluded.error,
                  coverage_complete=excluded.coverage_complete
                """,
                (
                    job_id,
                    channel_id,
                    status,
                    started_at,
                    "" if status == "running" else current_time(),
                    snapshot_count,
                    duplicate_count,
                    error[:1200],
                    int(coverage_complete),
                ),
            )

    def persist_channel_result(self, job: dict, window: dict, snapshots: list[dict]) -> tuple[int, int, list[str]]:
        collected_at = current_time()
        scope_type = "research" if job.get("parent_task_id") or job.get("query") or job.get("evidence_layer") else "general"
        scope_key = canonical_scope_key(job.get("query", "")) if scope_type == "research" else ""
        inserted = 0
        duplicates = 0
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
                if conn.total_changes - before:
                    inserted += 1
                    inserted_snapshot_ids.append(snapshot_id)
                else:
                    duplicates += 1
                    existing = conn.execute(
                        """
                        SELECT id FROM source_snapshots
                        WHERE channel_id=? AND source_url=? AND occurred_at=? AND scope_type=? AND scope_key=?
                        """,
                        (item["channel_id"], item["source_url"], item["occurred_at"], scope_type, scope_key),
                    ).fetchone()
                    if not existing:
                        continue
                    snapshot_id = existing["id"]
                conn.execute(
                    "INSERT OR IGNORE INTO source_job_snapshots(job_id,snapshot_id) VALUES(?,?)",
                    (job["id"], snapshot_id),
                )
            conn.execute(
                """
                INSERT OR REPLACE INTO source_collection_watermarks_v2(channel_id,scope_key,last_success_at)
                VALUES(?,?,?)
                """,
                (window["channel_id"], scope_key, window["window_end"]),
            )
        return inserted, duplicates, inserted_snapshot_ids

    def execute(self, job: dict) -> None:
        successful_channels: set[str] = set()
        errors: dict[str, str] = {}
        total_inserted = 0
        try:
            with self.connect() as conn:
                channels = {row["id"]: dict(row) for row in conn.execute("SELECT * FROM channels")}
            for channel in channels.values():
                channel["group_ids"] = json.loads(channel.get("group_ids") or "[]")
                if self.request_config_for:
                    channel["request_config"] = self.request_config_for(channel["id"])
            for window in json.loads(job["windows"]):
                channel = channels[window["channel_id"]]
                started_at = current_time()
                self.record_run(job["id"], channel["id"], "running", started_at=started_at)
                try:
                    channel_snapshots = collect_channel(
                        channel,
                        window,
                        self.profile_for(channel["id"]),
                        job.get("query", ""),
                    )
                    inserted, duplicates, snapshot_ids = self.persist_channel_result(job, window, channel_snapshots)
                    total_inserted += inserted
                    successful_channels.add(channel["id"])
                    coverage_complete = snapshots_cover_window(channel_snapshots)
                    if not coverage_complete:
                        errors[channel["id"]] = "Source returned partial time-window coverage"
                    self.record_run(
                        job["id"],
                        channel["id"],
                        "partial_coverage" if not coverage_complete else "completed" if inserted else "deduplicated",
                        started_at=started_at,
                        snapshot_count=inserted,
                        duplicate_count=duplicates,
                        coverage_complete=coverage_complete,
                    )
                    if self.normalize_snapshot:
                        for snapshot_id in snapshot_ids:
                            try:
                                self.normalize_snapshot(snapshot_id)
                            except Exception as exc:
                                errors[channel["id"]] = f"Normalization failed after collection: {exc}"[:1200]
                except Exception as exc:
                    errors[channel["id"]] = str(exc)[:1200]
                    self.record_run(job["id"], channel["id"], "failed", started_at=started_at, error=str(exc))
                    if (channel.get("request_config") or {}).get("adapter") == "mx_authorized_request_replay":
                        checked_at = current_time()
                        with self.connect() as conn:
                            conn.execute(
                                "UPDATE channels SET status='offline',last_check=?,updated_at=? WHERE id=?",
                                (checked_at, checked_at, channel["id"]),
                            )
            self.finish_collection(job, successful_channels, errors, total_inserted)
        except Exception as exc:
            self.fail_job(job, str(exc))

    def finish_collection(self, job: dict, successful_channels: set[str], errors: dict[str, str], inserted: int) -> None:
        completed_at = current_time()
        if not successful_channels:
            self.fail_job(job, json.dumps(errors, ensure_ascii=False) or "No source channel completed successfully")
            return
        if job["action"] == "collect_report":
            status = "generating_report"
        elif errors:
            status = "partial_completed"
        else:
            status = "completed" if inserted else "deduplicated"
        error = json.dumps(errors, ensure_ascii=False) if errors else ""
        with self.connect() as conn:
            conn.execute(
                "UPDATE source_collection_jobs SET status=?,snapshot_count=?,completed_at=?,error=? WHERE id=?",
                (status, inserted, completed_at, error[:1200], job["id"]),
            )
            if job.get("parent_task_id"):
                conn.execute("UPDATE tasks SET status='evidence_ready' WHERE id=?", (job["parent_task_id"],))
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

    def fail_job(self, job: dict, error: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE source_collection_jobs SET status='failed',error=?,completed_at=? WHERE id=?",
                (error[:1200], current_time(), job["id"]),
            )
            if job.get("parent_task_id"):
                conn.execute(
                    "UPDATE tasks SET status='agent_failed',agent_error=? WHERE id=?",
                    (f"证据采集失败: {error}"[:1200], job["parent_task_id"]),
                )

    def generate_report(self, job: dict) -> None:
        try:
            report, anchor = self.report_after_collection(job)
            completed_at = current_time()
            with self.connect() as conn:
                failures = conn.execute(
                    "SELECT COUNT(*) AS count FROM source_collection_runs WHERE job_id=? AND status='failed'",
                    (job["id"],),
                ).fetchone()["count"]
                status = "partial_review" if failures else "review"
                conn.execute(
                    """
                    UPDATE source_collection_jobs
                    SET status=?,report=?,report_anchor=?,completed_at=?
                    WHERE id=?
                    """,
                    (status, report, anchor, completed_at, job["id"]),
                )
        except Exception as exc:
            with self.connect() as conn:
                conn.execute(
                    "UPDATE source_collection_jobs SET status='report_failed',error=?,completed_at=? WHERE id=?",
                    (str(exc)[:1200], current_time(), job["id"]),
                )
