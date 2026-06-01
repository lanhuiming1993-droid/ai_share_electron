from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Callable

from backend.db_migrations import migration_status


def check(name: str, status: str, detail: str, *, blocking: bool = True, **metadata: Any) -> dict:
    return {"name": name, "status": status, "blocking": blocking, "detail": detail, **metadata}


def database_check(db_path: Path) -> dict:
    try:
        conn = sqlite3.connect(db_path, timeout=3)
        try:
            conn.execute("SELECT 1").fetchone()
            schema = migration_status(conn)
        finally:
            conn.close()
    except Exception as exc:
        return check("sqlite", "failed", f"{type(exc).__name__}: {exc}")
    if schema["status"] != "current":
        return check("sqlite", "failed", "SQLite schema revision is not current.", schema=schema)
    return check("sqlite", "ok", "SQLite is available and schema revision is current.", schema=schema)


def callable_check(name: str, validator: Callable[[], object], detail: str) -> dict:
    try:
        validator()
    except Exception as exc:
        return check(name, "failed", f"{type(exc).__name__}: {exc}")
    return check(name, "ok", detail)


def directory_check(name: str, path: Path) -> dict:
    if not path.is_dir():
        return check(name, "failed", f"Required directory is missing: {path}")
    return check(name, "ok", f"Directory is available: {path}", path=str(path))


def worker_check(worker: object) -> dict:
    thread = getattr(worker, "thread", None)
    running = bool(thread and thread.is_alive())
    return check(
        "collection_worker",
        "ok" if running else "failed",
        "Collection worker thread is running." if running else "Collection worker thread is not running.",
    )


def telemetry_check(telemetry: dict) -> dict:
    status = str(telemetry.get("status") or "disabled")
    metadata = {key: value for key, value in telemetry.items() if key not in {"status", "detail"}}
    if status == "enabled":
        return check("opentelemetry", "ok", "OTLP tracing is enabled.", blocking=False, **metadata)
    return check(
        "opentelemetry",
        "info",
        str(telemetry.get("detail") or "OTLP tracing is disabled."),
        blocking=False,
        **metadata,
    )


def summarize_readiness(checks: list[dict]) -> dict:
    blockers = [item for item in checks if item["blocking"] and item["status"] != "ok"]
    return {
        "status": "ready" if not blockers else "not_ready",
        "checks": checks,
        "blocking_failures": [item["name"] for item in blockers],
    }
