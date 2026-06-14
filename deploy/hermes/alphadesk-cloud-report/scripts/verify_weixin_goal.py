#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
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
DEFAULT_SOURCE_STATUS_CACHE = Path(os.environ.get("ALPHADESK_SOURCE_STATUS_CACHE", "/tmp/alphadesk-source-status-cache.json"))
AGENT_CHANNEL_IDS = ("wechat-mp-rss", "ima-knowledge", "zsxq")
ALPHADESK_COMMAND_AUDIT_FILE = "alphadesk-command.audit.jsonl"
AGENT_COLLECTION_READY_STATUSES = {"completed", "deduplicated", "partial_completed"}
LEGACY_REPORT_READY_STATUSES = {"review", "partial_review"}
GATEWAY_LOG_PATTERNS = {
    "weixin_adapter_inbound": re.compile(r"\[Weixin\] inbound"),
    "weixin_message": re.compile(r"inbound message: platform=weixin"),
    "lightclawbot_message": re.compile(r"inbound message: platform=lightclawbot"),
    "weixin_rate_limited": re.compile(r"\[Weixin\].*rate limited"),
    "weixin_session_expired": re.compile(r"\[Weixin\].*Session expired"),
    "weixin_getupdates_failed": re.compile(r"\[Weixin\].*getUpdates failed"),
    "weixin_poll_error": re.compile(r"\[Weixin\].*poll error"),
}
LOG_TS_RE = re.compile(r"^(?P<timestamp>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3})\s+(?P<level>[A-Z]+)\s+(?P<message>.*)$")


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


def path_mtime_iso(path: Path) -> str:
    try:
        return iso_from_epoch(path.stat().st_mtime)
    except OSError:
        return ""


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


def read_weixin_sync_diagnostics(hermes_home: Path, now: float | None = None) -> dict[str, Any]:
    account_dir = hermes_home / "weixin" / "accounts"
    if not account_dir.exists():
        return {"exists": False, "sync_files": []}
    now_ts = time.time() if now is None else now
    sync_files: list[dict[str, Any]] = []
    for path in sorted(account_dir.glob("*.sync.json")):
        stat = path.stat()
        sync_buf = ""
        parse_error = ""
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            sync_buf = str(data.get("get_updates_buf") or "")
        except Exception as exc:  # noqa: BLE001 - verifier should report parse issues.
            parse_error = str(exc)
        item = {
            "account_hash": sha_preview(path.name.removesuffix(".sync.json")),
            "updated_at": iso_from_epoch(stat.st_mtime),
            "age_seconds": max(0, int(now_ts - stat.st_mtime)),
            "size": stat.st_size,
            "sync_buffer_length": len(sync_buf),
        }
        if parse_error:
            item["parse_error"] = parse_error[:160]
        sync_files.append(item)
    return {"exists": True, "sync_files": sync_files}


def read_gateway_log_diagnostics(hermes_home: Path, max_lines: int = 2000) -> dict[str, Any]:
    path = hermes_home / "logs" / "gateway.log"
    if not path.exists():
        return {"exists": False}
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    latest: dict[str, dict[str, Any]] = {}
    for line in lines[-max_lines:]:
        for key, pattern in GATEWAY_LOG_PATTERNS.items():
            if not pattern.search(line):
                continue
            parsed = LOG_TS_RE.match(line)
            if parsed:
                event = {
                    "timestamp": parsed.group("timestamp"),
                    "level": parsed.group("level"),
                    "message_preview": parsed.group("message")[:240],
                }
            else:
                event = {"timestamp": "", "level": "", "message_preview": line[:240]}
            latest[key] = event
    return {
        "exists": True,
        "updated_at": path_mtime_iso(path),
        "latest": latest,
    }


def read_alphadesk_plugin_diagnostics(hermes_home: Path) -> dict[str, Any]:
    path = hermes_home / "plugins" / "alphadesk-command" / "plugin.yaml"
    if not path.exists():
        return {"exists": False}
    details: dict[str, Any] = {"exists": True, "updated_at": path_mtime_iso(path)}
    for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if line.startswith("version:"):
            details["version"] = line.split(":", 1)[1].strip().strip("\"'")
        elif line.startswith("name:"):
            details["name"] = line.split(":", 1)[1].strip().strip("\"'")
    return details


def read_alphadesk_command_audit_diagnostics(hermes_home: Path, max_lines: int = 1000) -> dict[str, Any]:
    path = hermes_home / ALPHADESK_COMMAND_AUDIT_FILE
    if not path.exists():
        return {"exists": False}
    latest: dict[str, Any] | None = None
    parse_errors = 0
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    for line in lines[-max_lines:]:
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            parse_errors += 1
            continue
        latest = {
            "timestamp_iso": item.get("timestamp_iso") or iso_from_epoch(item.get("timestamp")),
            "platform": item.get("platform"),
            "days": item.get("days"),
            "chat_hash": sha_preview(str(item.get("chat_id") or "")),
            "user_hash": sha_preview(str(item.get("user_id") or item.get("user_name") or "")),
            "content_preview": str(item.get("content_preview") or "")[:120],
        }
    return {
        "exists": True,
        "updated_at": path_mtime_iso(path),
        "latest": latest,
        "parse_errors": parse_errors,
    }


def read_ingress_diagnostics(hermes_home: Path) -> dict[str, Any]:
    return {
        "weixin_sync": read_weixin_sync_diagnostics(hermes_home),
        "gateway_log": read_gateway_log_diagnostics(hermes_home),
        "alphadesk_command_plugin": read_alphadesk_plugin_diagnostics(hermes_home),
        "alphadesk_command_audit": read_alphadesk_command_audit_diagnostics(hermes_home),
    }


def parse_sources(value: str) -> list[str]:
    sources = [item.strip() for item in value.split(",") if item.strip()]
    return sources or ["weixin"]


def compact_command_text(value: str) -> str:
    return re.sub(r"\s+", "", str(value or "")).strip()


def command_window_days(value: str) -> int | None:
    match = re.search(r"采集近?\s*(\d{1,2})\s*天", str(value or ""))
    if not match:
        return None
    return max(1, min(30, int(match.group(1))))


def sql_placeholders(values: list[str]) -> str:
    return ",".join("?" for _ in values)


def find_audited_platform_command(
    hermes_home: Path,
    sources: list[str],
    command: str,
    min_timestamp: float,
) -> dict[str, Any] | None:
    path = hermes_home / ALPHADESK_COMMAND_AUDIT_FILE
    if not path.exists():
        return None
    target_days = command_window_days(command)
    target_compact = compact_command_text(command)
    best: dict[str, Any] | None = None
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        platform = str(item.get("platform") or "")
        if platform not in sources:
            continue
        timestamp = float(item.get("timestamp") or 0)
        if timestamp < min_timestamp:
            continue
        if target_days is not None and int(item.get("days") or 0) != target_days:
            continue
        content_preview = str(item.get("content_preview") or "")
        if target_days is None and target_compact not in compact_command_text(content_preview):
            continue
        candidate = {
            "id": None,
            "session_id": "",
            "source": platform,
            "timestamp": timestamp,
            "timestamp_iso": item.get("timestamp_iso") or iso_from_epoch(timestamp),
            "content_preview": content_preview[:80],
            "evidence": "alphadesk_command_audit",
        }
        if best is None or float(candidate["timestamp"]) > float(best["timestamp"]):
            best = candidate
    return best


def find_platform_command(
    hermes_home: Path,
    sources: list[str],
    command: str,
    min_message_id: int,
    min_timestamp: float,
) -> dict[str, Any] | None:
    audited = find_audited_platform_command(hermes_home, sources, command, min_timestamp)
    state_db = hermes_home / "state.db"
    if state_db.exists():
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
            if row:
                candidate = {
                    "id": row["id"],
                    "session_id": row["session_id"],
                    "source": row["source"],
                    "timestamp": row["timestamp"],
                    "timestamp_iso": iso_from_epoch(row["timestamp"]),
                    "content_preview": str(row["content"] or "")[:80],
                    "evidence": "messages",
                }
                if audited and float(audited["timestamp"]) <= float(candidate["timestamp"]):
                    return {
                        **audited,
                        "id": candidate["id"],
                        "session_id": candidate["session_id"],
                        "transcript_timestamp": candidate["timestamp"],
                        "transcript_timestamp_iso": candidate["timestamp_iso"],
                        "evidence": "audit+messages",
                    }
                return candidate
        finally:
            conn.close()
    return audited


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


def find_platform_response(
    hermes_home: Path,
    sources: list[str],
    command: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if not command:
        return None
    state_db = hermes_home / "state.db"
    if not state_db.exists():
        return None
    since_ts = float(command.get("timestamp") or 0)
    session_id = str(command.get("session_id") or "")
    conn = sqlite3.connect(str(state_db))
    conn.row_factory = sqlite3.Row
    try:
        source_marks = sql_placeholders(sources)
        session_filter = "OR m.session_id=?" if session_id else ""
        session_params = (session_id,) if session_id else ()
        rows = conn.execute(
            f"""
            SELECT m.id,m.session_id,s.source,m.role,m.content,m.timestamp
            FROM messages m
            LEFT JOIN sessions s ON s.id=m.session_id
            WHERE (s.source IN ({source_marks}) {session_filter})
              AND m.role='assistant'
              AND m.timestamp>=?
              AND length(trim(coalesce(m.content,'')))>=40
            ORDER BY m.id DESC
            LIMIT 5
            """,
            (*sources, *session_params, since_ts),
        ).fetchall()
        for row in rows:
            content = str(row["content"] or "")
            return {
                "id": row["id"],
                "session_id": row["session_id"],
                "source": row["source"],
                "timestamp": row["timestamp"],
                "timestamp_iso": iso_from_epoch(row["timestamp"]),
                "content_chars": len(content),
                "content": content,
                "content_preview": content[:160],
            }
        return None
    finally:
        conn.close()


def pdf_media_paths(response: dict[str, Any] | None) -> list[str]:
    if not response:
        return []
    content = str(response.get("content") or response.get("content_preview") or "")
    return re.findall(r"MEDIA:(/\S+?\.pdf)\b", content)


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


def collect_source_status_cached(
    base_url: str,
    *,
    cache_path: Path,
    ttl_seconds: int,
    force: bool = False,
) -> dict[str, Any]:
    now_ts = time.time()
    if not force and ttl_seconds > 0 and cache_path.exists():
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            checked_at = float(cached.get("checked_at_epoch") or 0)
            if cached.get("base_url") == base_url and now_ts - checked_at <= ttl_seconds:
                statuses = cached.get("source_status") if isinstance(cached.get("source_status"), dict) else {}
                return {
                    **statuses,
                    "_cache": {
                        "hit": True,
                        "age_seconds": max(0, int(now_ts - checked_at)),
                        "ttl_seconds": ttl_seconds,
                    },
                }
        except Exception:
            pass

    statuses = collect_source_status(base_url)
    payload = {"base_url": base_url, "checked_at_epoch": now_ts, "source_status": statuses}
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass
    return {
        **statuses,
        "_cache": {
            "hit": False,
            "age_seconds": 0,
            "ttl_seconds": ttl_seconds,
        },
    }


def find_report_job(workbench_db: Path, command_seen_at: float) -> dict[str, Any] | None:
    conn = sqlite3.connect(str(workbench_db))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT id,action,status,lookback_days,snapshot_count,created_at,started_at,completed_at,
                   report IS NOT NULL AS report_ready
            FROM source_collection_jobs
            WHERE action IN ('collect','collect_report') AND lookback_days=30
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
            try:
                attached_snapshot_counts = [
                    dict(count)
                    for count in conn.execute(
                        """
                        SELECT s.channel_id,COUNT(*) AS count
                        FROM source_job_snapshots js
                        JOIN source_snapshots s ON s.id=js.snapshot_id
                        WHERE js.job_id=?
                        GROUP BY s.channel_id
                        ORDER BY s.channel_id
                        """,
                        (row["id"],),
                    ).fetchall()
                ]
            except sqlite3.OperationalError:
                attached_snapshot_counts = []
            return {**dict(row), "runs": runs, "attached_snapshot_counts": attached_snapshot_counts}
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
    source_status = (
        collect_source_status_cached(
            args.base_url,
            cache_path=args.source_status_cache,
            ttl_seconds=max(args.source_check_ttl, 0),
            force=args.force_source_check,
        )
        if args.check_sources
        else {}
    )
    job = None
    if command and workbench_db:
        job = find_report_job(workbench_db, float(command["timestamp"]))
    response = find_platform_response(args.hermes_home, sources, command)
    required_run_ids = set(AGENT_CHANNEL_IDS)
    actual_run_ids = {run.get("channel_id") for run in (job or {}).get("runs", [])}
    attached_run_ids = {
        count.get("channel_id")
        for count in (job or {}).get("attached_snapshot_counts", [])
        if int(count.get("count") or 0) > 0
    }
    job_ready = False
    if job:
        if job.get("action") == "collect":
            job_ready = job.get("status") in AGENT_COLLECTION_READY_STATUSES
        else:
            job_ready = bool(job.get("report_ready")) and job.get("status") in LEGACY_REPORT_READY_STATUSES
    response_pdf_media = pdf_media_paths(response)
    require_pdf_media = bool(getattr(args, "require_pdf_media", False))
    complete = bool(
        command
        and job
        and job_ready
        and required_run_ids.issubset(actual_run_ids)
        and required_run_ids.issubset(attached_run_ids)
        and response
        and (not require_pdf_media or response_pdf_media)
    )
    summary = {
        "complete": complete,
        "require_pdf_media": require_pdf_media,
        "command": args.command,
        "gateway": read_gateway_state(args.hermes_home),
        "weixin_directory": read_weixin_directory(args.hermes_home),
        "sources": sources,
        "matched_platform_command": command,
        "latest_platform_messages": latest_platform_messages(args.hermes_home, sources),
        "ingress_diagnostics": read_ingress_diagnostics(args.hermes_home),
        "source_status": source_status,
        "workbench_db": str(workbench_db) if workbench_db else "",
        "matched_report_job": job,
        "matched_platform_response": response,
        "response_pdf_media": response_pdf_media,
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
    parser.add_argument("--source-status-cache", type=Path, default=DEFAULT_SOURCE_STATUS_CACHE)
    parser.add_argument("--source-check-ttl", type=int, default=900, help="seconds to cache expensive source checks")
    parser.add_argument("--force-source-check", action="store_true", help="ignore the source status cache")
    parser.add_argument("--require-pdf-media", action="store_true", help="require the response to include MEDIA:/...pdf")
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
