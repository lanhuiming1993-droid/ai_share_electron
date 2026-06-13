#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_COMMAND = "采集近30天数据并生成报告"
DEFAULT_BASE_URL = "http://127.0.0.1:18080"
DEFAULT_ENV_FILE = Path("/opt/alphadesk/deploy/cloud.env")
DEFAULT_HERMES_HOME = Path.home() / ".hermes"
AGENT_CHANNEL_IDS = ("wechat-mp-rss", "ima-knowledge", "zsxq")


def parse_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            if value[0] == '"':
                try:
                    value = json.loads(value)
                except json.JSONDecodeError:
                    value = value[1:-1]
            else:
                value = value[1:-1]
        values[key.strip()] = value
    return values


def sha_preview(value: str) -> str:
    if not value:
        return ""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def iso_from_epoch(value: float | int | None) -> str:
    if value is None:
        return ""
    return datetime.fromtimestamp(float(value), timezone.utc).isoformat()


def parse_iso(value: str) -> float:
    if not value:
        return 0.0
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = f"{normalized[:-1]}+00:00"
    return datetime.fromisoformat(normalized).timestamp()


def request_json(method: str, base_url: str, path: str, token: str = "", payload: dict[str, Any] | None = None) -> dict[str, Any]:
    body = None
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(f"{base_url.rstrip('/')}{path}", data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        return {"status": "http_error", "code": exc.code, "detail": detail[:300]}
    except Exception as exc:  # noqa: BLE001 - verifier should report, not crash.
        return {"status": "error", "detail": str(exc)}


def resolve_workbench_db(env: dict[str, str], explicit: Path | None) -> Path | None:
    candidates: list[Path] = []
    if explicit:
        candidates.append(explicit)
    data_root = env.get("ALPHADESK_DATA_ROOT", "").strip()
    if data_root:
        candidates.append(Path(data_root) / "alphadesk" / "workbench.db")
    candidates.extend(
        [
            Path("/opt/alphadesk-data/alphadesk/workbench.db"),
            Path("/app/data/workbench.db"),
            Path.cwd() / "data" / "workbench.db",
        ]
    )
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def read_gateway_state(hermes_home: Path) -> dict[str, Any]:
    path = hermes_home / "gateway_state.json"
    if not path.exists():
        return {"exists": False}
    data = json.loads(path.read_text(encoding="utf-8"))
    platforms = data.get("platforms") or {}
    return {
        "exists": True,
        "gateway_state": data.get("gateway_state"),
        "active_agents": data.get("active_agents"),
        "platforms": {
            name: {
                "state": details.get("state"),
                "error_code": details.get("error_code"),
                "error_message": details.get("error_message"),
                "updated_at": details.get("updated_at"),
            }
            for name, details in platforms.items()
            if name in {"weixin", "lightclawbot"}
        },
        "updated_at": data.get("updated_at"),
    }


def read_weixin_directory(hermes_home: Path) -> dict[str, Any]:
    path = hermes_home / "channel_directory.json"
    if not path.exists():
        return {"exists": False}
    data = json.loads(path.read_text(encoding="utf-8"))
    weixin = (data.get("platforms") or {}).get("weixin") or []
    return {
        "exists": True,
        "updated_at": data.get("updated_at"),
        "weixin_targets": [
            {
                "type": item.get("type"),
                "id_hash": sha_preview(str(item.get("id") or "")),
                "thread_id": item.get("thread_id"),
            }
            for item in weixin
        ],
    }


def parse_sources(value: str) -> list[str]:
    sources = [item.strip() for item in value.split(",") if item.strip()]
    return sources or ["weixin"]


def sql_placeholders(values: list[str]) -> str:
    return ",".join("?" for _ in values)


def find_platform_command(
    hermes_home: Path,
    sources: list[str],
    command: str,
    min_message_id: int,
    min_timestamp: float,
) -> dict[str, Any] | None:
    state_db = hermes_home / "state.db"
    if not state_db.exists():
        return None
    conn = sqlite3.connect(str(state_db))
    conn.row_factory = sqlite3.Row
    try:
        source_marks = sql_placeholders(sources)
        row = conn.execute(
            f"""
            SELECT m.id,m.session_id,s.source,m.role,m.content,m.timestamp
            FROM messages m
            LEFT JOIN sessions s ON s.id=m.session_id
            WHERE s.source IN ({source_marks})
              AND m.role='user'
              AND m.id>?
              AND m.timestamp>=?
              AND m.content LIKE ?
            ORDER BY m.id DESC
            LIMIT 1
            """,
            (*sources, min_message_id, min_timestamp, f"%{command}%"),
        ).fetchone()
        if not row:
            return None
        return {
            "id": row["id"],
            "session_id": row["session_id"],
            "source": row["source"],
            "timestamp": row["timestamp"],
            "timestamp_iso": iso_from_epoch(row["timestamp"]),
            "content_preview": str(row["content"] or "")[:80],
        }
    finally:
        conn.close()


def latest_platform_messages(hermes_home: Path, sources: list[str], limit: int = 5) -> list[dict[str, Any]]:
    state_db = hermes_home / "state.db"
    if not state_db.exists():
        return []
    conn = sqlite3.connect(str(state_db))
    conn.row_factory = sqlite3.Row
    try:
        source_marks = sql_placeholders(sources)
        rows = conn.execute(
            f"""
            SELECT m.id,m.session_id,s.source,m.role,substr(m.content,1,120) AS content,m.timestamp
            FROM messages m
            LEFT JOIN sessions s ON s.id=m.session_id
            WHERE s.source IN ({source_marks})
            ORDER BY m.id DESC
            LIMIT ?
            """,
            (*sources, limit),
        ).fetchall()
        return [
            {
                "id": row["id"],
                "session_id": row["session_id"],
                "source": row["source"],
                "role": row["role"],
                "content_preview": row["content"],
                "timestamp_iso": iso_from_epoch(row["timestamp"]),
            }
            for row in rows
        ]
    finally:
        conn.close()


def collect_source_status(base_url: str) -> dict[str, dict[str, Any]]:
    statuses: dict[str, dict[str, Any]] = {}
    for channel_id in AGENT_CHANNEL_IDS:
        raw = request_json("POST", base_url, f"/api/channels/{channel_id}/check")
        statuses[channel_id] = {
            key: raw.get(key)
            for key in ("status", "message", "subscription_count", "knowledge_base_count", "topic_count", "checked_at")
            if key in raw
        }
    return statuses


def find_report_job(workbench_db: Path, command_seen_at: float) -> dict[str, Any] | None:
    conn = sqlite3.connect(str(workbench_db))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT id,action,status,lookback_days,snapshot_count,created_at,started_at,completed_at,
                   report IS NOT NULL AS report_ready
            FROM source_collection_jobs
            WHERE action='collect_report' AND lookback_days=30
            ORDER BY created_at DESC
            LIMIT 10
            """
        ).fetchall()
        for row in rows:
            created_at = str(row["created_at"] or "")
            created_ts = parse_iso(created_at) if created_at else 0.0
            if created_ts + 5 < command_seen_at:
                continue
            runs = [
                dict(run)
                for run in conn.execute(
                    """
                    SELECT channel_id,status,snapshot_count,duplicate_count,started_at,completed_at,error
                    FROM source_collection_runs
                    WHERE job_id=?
                    ORDER BY started_at,channel_id
                    """,
                    (row["id"],),
                ).fetchall()
            ]
            return {**dict(row), "runs": runs}
        return None
    finally:
        conn.close()


def verify_once(args: argparse.Namespace) -> tuple[bool, dict[str, Any]]:
    env = parse_env(args.env_file)
    workbench_db = resolve_workbench_db(env, args.workbench_db)
    sources = parse_sources(args.sources)
    command = find_platform_command(
        args.hermes_home,
        sources,
        args.command,
        args.since_message_id,
        parse_iso(args.since_iso) if args.since_iso else 0.0,
    )
    source_status = collect_source_status(args.base_url) if args.check_sources else {}
    job = None
    if command and workbench_db:
        job = find_report_job(workbench_db, float(command["timestamp"]))
    required_run_ids = set(AGENT_CHANNEL_IDS)
    actual_run_ids = {run.get("channel_id") for run in (job or {}).get("runs", [])}
    complete = bool(
        command
        and job
        and job.get("report_ready")
        and job.get("status") in {"review", "partial_review"}
        and required_run_ids.issubset(actual_run_ids)
    )
    summary = {
        "complete": complete,
        "command": args.command,
        "gateway": read_gateway_state(args.hermes_home),
        "weixin_directory": read_weixin_directory(args.hermes_home),
        "sources": sources,
        "matched_platform_command": command,
        "latest_platform_messages": latest_platform_messages(args.hermes_home, sources),
        "source_status": source_status,
        "workbench_db": str(workbench_db) if workbench_db else "",
        "matched_report_job": job,
    }
    return complete, summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify Weixin -> Hermes -> AlphaDesk three-source report flow.")
    parser.add_argument("--command", default=DEFAULT_COMMAND)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    parser.add_argument("--hermes-home", type=Path, default=DEFAULT_HERMES_HOME)
    parser.add_argument("--workbench-db", type=Path)
    parser.add_argument("--sources", default="weixin", help="comma-separated Hermes session sources to inspect")
    parser.add_argument("--since-message-id", type=int, default=0)
    parser.add_argument("--since-iso", default="")
    parser.add_argument("--watch-seconds", type=int, default=0)
    parser.add_argument("--interval", type=int, default=15)
    parser.add_argument("--check-sources", action="store_true")
    args = parser.parse_args()

    deadline = time.time() + max(args.watch_seconds, 0)
    last_summary: dict[str, Any] = {}
    while True:
        complete, summary = verify_once(args)
        last_summary = summary
        if complete:
            print(json.dumps(summary, ensure_ascii=False, indent=2))
            return 0
        if args.watch_seconds <= 0 or time.time() >= deadline:
            print(json.dumps(last_summary, ensure_ascii=False, indent=2))
            return 1
        time.sleep(max(args.interval, 1))


if __name__ == "__main__":
    raise SystemExit(main())
