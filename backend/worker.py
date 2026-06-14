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
from backend.logging_config import get_logger, log_event, log_exception

logger = get_logger("collection_worker")


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
        normalize_snapshot: Callable[..., dict] | None = None,
        on_evidence_ready: Callable[[str], None] | None = None,
        request_config_for: Callable[[str], dict] | None = None,
        reports_enabled: bool = True,
        poll_seconds: float = 1.5,
    ) -> None:
        self.db_path = db_path
        self.profile_for = profile_for
        self.report_after_collection = report_after_collection
        self.normalize_snapshot = normalize_snapshot
        self.on_evidence_ready = on_evidence_ready
        self.request_config_for = request_config_for
        self.reports_enabled = reports_enabled
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
            log_event(logger, "INFO", "worker.start.skipped", reason="already_running")
            return
        self.stop_event.clear()
        self.thread = threading.Thread(target=self.run, daemon=True, name="source-collection-worker")
        self.thread.start()
        log_event(logger, "INFO", "worker.started", poll_seconds=self.poll_seconds)

    def stop(self, timeout_seconds: float = 5.0) -> None:
        self.stop_event.set()
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=max(0.0, timeout_seconds))
        log_event(logger, "INFO", "worker.stopped", thread_alive=bool(self.thread and self.thread.is_alive()))

    def run(self) -> None:
        while not self.stop_event.is_set():
            try:
                job = self.claim_next()
                if job:
                    log_event(
                        logger,
                        "INFO",
                        "worker.job.claimed",
                        job_id=job["id"],
                        action=job["action"],
                        status=job["status"],
                        resume_report=bool(job.get("_resume_report")),
                        parent_task_id=job.get("parent_task_id", ""),
                        evidence_layer=job.get("evidence_layer", ""),
                    )
                    if job.pop("_resume_report", False):
                        self.generate_report(job)
                    else:
                        self.execute(job)
                else:
                    self.stop_event.wait(self.poll_seconds)
            except Exception as exc:
                log_exception(logger, "worker.loop.failed", exc)
                self.stop_event.wait(self.poll_seconds)

    def claim_next(self) -> dict | None:
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT * FROM source_collection_jobs
                WHERE status='queued'
                   OR (:reports_enabled AND action IN ('report','collect_report') AND report IS NULL
                       AND status IN ('completed','deduplicated','partial_completed','generating_report'))
                ORDER BY created_at LIMIT 1
                """,
                {"reports_enabled": 1 if self.reports_enabled else 0},
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

    def persist_channel_result(self, job: dict, window: dict, snapshots: list[dict]) -> tuple[int, int, list[str], list[str]]:
        collected_at = current_time()
        general_refresh = job.get("evidence_layer") == "local_source_snapshots" and not canonical_scope_key(job.get("query", ""))
        scope_type = "general" if general_refresh or not (job.get("parent_task_id") or job.get("query") or job.get("evidence_layer")) else "research"
        scope_key = canonical_scope_key(job.get("query", "")) if scope_type == "research" else ""
        inserted = 0
        duplicates = 0
        inserted_snapshot_ids: list[str] = []
        refreshed_snapshot_ids: list[str] = []
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
                        SELECT id,content FROM source_snapshots
                        WHERE channel_id=? AND source_url=? AND occurred_at=? AND scope_type=? AND scope_key=?
                        """,
                        (item["channel_id"], item["source_url"], item["occurred_at"], scope_type, scope_key),
                    ).fetchone()
                    if not existing:
                        continue
                    snapshot_id = existing["id"]
                    if len(str(item["content"] or "").strip()) > len(str(existing["content"] or "").strip()):
                        conn.execute(
                            "UPDATE source_snapshots SET content=?,collected_at=? WHERE id=?",
                            (item["content"], collected_at, snapshot_id),
                        )
                        refreshed_snapshot_ids.append(snapshot_id)
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
        log_event(
            logger,
            "INFO",
            "worker.channel.persisted",
            job_id=job["id"],
            channel_id=window["channel_id"],
            scope_type=scope_type,
            scope_key=scope_key,
            received_snapshots=len(snapshots),
            inserted_snapshots=inserted,
            refreshed_snapshots=len(refreshed_snapshot_ids),
            duplicate_snapshots=duplicates,
        )
        return inserted, duplicates, inserted_snapshot_ids, refreshed_snapshot_ids

    def attach_cached_snapshots_for_window(self, job: dict, window: dict) -> int:
        general_refresh = job.get("evidence_layer") == "local_source_snapshots" and not canonical_scope_key(job.get("query", ""))
        scope_type = "general" if general_refresh or not (job.get("parent_task_id") or job.get("query") or job.get("evidence_layer")) else "research"
        scope_key = canonical_scope_key(job.get("query", "")) if scope_type == "research" else ""
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT id FROM source_snapshots
                WHERE channel_id=? AND occurred_at BETWEEN ? AND ?
                  AND scope_type=? AND scope_key=?
                ORDER BY occurred_at DESC
                """,
                (window["channel_id"], window["window_start"], window["window_end"], scope_type, scope_key),
            ).fetchall()
            for row in rows:
                conn.execute(
                    "INSERT OR IGNORE INTO source_job_snapshots(job_id,snapshot_id) VALUES(?,?)",
                    (job["id"], row["id"]),
                )
        return len(rows)

    def execute(self, job: dict) -> None:
        successful_channels: set[str] = set()
        errors: dict[str, str] = {}
        total_inserted = 0
        total_refreshed = 0
        log_event(
            logger,
            "INFO",
            "worker.collection.started",
            job_id=job["id"],
            action=job["action"],
            parent_task_id=job.get("parent_task_id", ""),
            evidence_layer=job.get("evidence_layer", ""),
        )
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
                log_event(
                    logger,
                    "INFO",
                    "worker.channel.started",
                    job_id=job["id"],
                    channel_id=channel["id"],
                    collection_mode=channel.get("collection_mode", ""),
                    window_start=window["window_start"],
                    window_end=window["window_end"],
                )
                self.record_run(job["id"], channel["id"], "running", started_at=started_at)
                try:
                    channel_snapshots = collect_channel(
                        channel,
                        window,
                        self.profile_for(channel["id"]),
                        job.get("query", ""),
                    )
                    inserted, duplicates, snapshot_ids, refreshed_snapshot_ids = self.persist_channel_result(job, window, channel_snapshots)
                    total_inserted += inserted
                    total_refreshed += len(refreshed_snapshot_ids)
                    successful_channels.add(channel["id"])
                    coverage_complete = snapshots_cover_window(channel_snapshots)
                    if not coverage_complete:
                        errors[channel["id"]] = "Source returned partial time-window coverage"
                    self.record_run(
                        job["id"],
                        channel["id"],
                        "partial_coverage" if not coverage_complete else "completed" if inserted or refreshed_snapshot_ids else "deduplicated",
                        started_at=started_at,
                        snapshot_count=inserted,
                        duplicate_count=duplicates,
                        coverage_complete=coverage_complete,
                    )
                    log_event(
                        logger,
                        "WARNING" if not coverage_complete else "INFO",
                        "worker.channel.completed",
                        job_id=job["id"],
                        channel_id=channel["id"],
                        received_snapshots=len(channel_snapshots),
                        inserted_snapshots=inserted,
                        refreshed_snapshots=len(refreshed_snapshot_ids),
                        duplicate_snapshots=duplicates,
                        coverage_complete=coverage_complete,
                    )
                    if self.normalize_snapshot:
                        for snapshot_id in snapshot_ids:
                            try:
                                self.normalize_snapshot(snapshot_id)
                            except Exception as exc:
                                errors[channel["id"]] = f"Normalization failed after collection: {exc}"[:1200]
                                log_exception(
                                    logger,
                                    "worker.normalization.failed",
                                    exc,
                                    job_id=job["id"],
                                    channel_id=channel["id"],
                                    snapshot_id=snapshot_id,
                                )
                        for snapshot_id in refreshed_snapshot_ids:
                            try:
                                self.normalize_snapshot(snapshot_id, force=True)
                            except Exception as exc:
                                errors[channel["id"]] = f"Normalization failed after snapshot refresh: {exc}"[:1200]
                                log_exception(
                                    logger,
                                    "worker.normalization.refresh_failed",
                                    exc,
                                    job_id=job["id"],
                                    channel_id=channel["id"],
                                    snapshot_id=snapshot_id,
                                )
                except Exception as exc:
                    if job["action"] in ("collect", "collect_report"):
                        cached_count = self.attach_cached_snapshots_for_window(job, window)
                    else:
                        cached_count = 0
                    if cached_count:
                        error = f"Live collection failed; used {cached_count} cached snapshots: {exc}"[:1200]
                        errors[channel["id"]] = error
                        successful_channels.add(channel["id"])
                        self.record_run(
                            job["id"],
                            channel["id"],
                            "cached_after_error",
                            started_at=started_at,
                            snapshot_count=0,
                            duplicate_count=cached_count,
                            error=error,
                        )
                        log_exception(
                            logger,
                            "worker.channel.cached_after_error",
                            exc,
                            job_id=job["id"],
                            channel_id=channel["id"],
                            collection_mode=channel.get("collection_mode", ""),
                            cached_snapshots=cached_count,
                        )
                        continue
                    errors[channel["id"]] = str(exc)[:1200]
                    self.record_run(job["id"], channel["id"], "failed", started_at=started_at, error=str(exc))
                    log_exception(
                        logger,
                        "worker.channel.failed",
                        exc,
                        job_id=job["id"],
                        channel_id=channel["id"],
                        collection_mode=channel.get("collection_mode", ""),
                    )
                    if (channel.get("request_config") or {}).get("adapter") == "mx_authorized_request_replay":
                        checked_at = current_time()
                        with self.connect() as conn:
                            conn.execute(
                                "UPDATE channels SET status='offline',last_check=?,updated_at=? WHERE id=?",
                                (checked_at, checked_at, channel["id"]),
                            )
            self.finish_collection(job, successful_channels, errors, total_inserted, total_refreshed)
        except Exception as exc:
            log_exception(logger, "worker.collection.failed", exc, job_id=job["id"])
            self.fail_job(job, str(exc))

    def finish_collection(self, job: dict, successful_channels: set[str], errors: dict[str, str], inserted: int, refreshed: int = 0) -> None:
        completed_at = current_time()
        if not successful_channels:
            self.fail_job(job, json.dumps(errors, ensure_ascii=False) or "No source channel completed successfully")
            return
        if job["action"] == "collect_report" and self.reports_enabled:
            status = "generating_report"
        elif errors:
            status = "partial_completed"
        else:
            status = "completed" if inserted or refreshed else "deduplicated"
        error = json.dumps(errors, ensure_ascii=False) if errors else ""
        with self.connect() as conn:
            conn.execute(
                "UPDATE source_collection_jobs SET status=?,snapshot_count=?,completed_at=?,error=? WHERE id=?",
                (status, inserted, completed_at, error[:1200], job["id"]),
            )
            if job.get("parent_task_id"):
                conn.execute("UPDATE tasks SET status='evidence_ready' WHERE id=?", (job["parent_task_id"],))
        log_event(
            logger,
            "WARNING" if errors else "INFO",
            "worker.collection.completed",
            job_id=job["id"],
            status=status,
            inserted_snapshots=inserted,
            refreshed_snapshots=refreshed,
            successful_channels=sorted(successful_channels),
            channel_errors=errors,
        )
        if job["action"] == "collect_report" and self.reports_enabled:
            self.generate_report(job)
        if job.get("parent_task_id") and self.on_evidence_ready:
            try:
                self.on_evidence_ready(job["parent_task_id"])
            except Exception as exc:
                log_exception(
                    logger,
                    "worker.evidence_callback.failed",
                    exc,
                    job_id=job["id"],
                    parent_task_id=job["parent_task_id"],
                )
                with self.connect() as conn:
                    conn.execute(
                        "UPDATE tasks SET status='agent_failed',agent_error=? WHERE id=?",
                        (str(exc)[:1200], job["parent_task_id"]),
                    )

    def fail_job(self, job: dict, error: str) -> None:
        log_event(logger, "ERROR", "worker.job.failed", job_id=job["id"], parent_task_id=job.get("parent_task_id", ""), error=error)
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
        log_event(logger, "INFO", "worker.report.started", job_id=job["id"], action=job["action"], report_title=job["report_title"])
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
            log_event(
                logger,
                "INFO",
                "worker.report.completed",
                job_id=job["id"],
                status=status,
                report_anchor=anchor,
                report_chars=len(report),
            )
        except Exception as exc:
            log_exception(logger, "worker.report.failed", exc, job_id=job["id"], report_title=job["report_title"])
            with self.connect() as conn:
                conn.execute(
                    "UPDATE source_collection_jobs SET status='report_failed',error=?,completed_at=? WHERE id=?",
                    (str(exc)[:1200], current_time(), job["id"]),
                )
