from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime

LATEST_SCHEMA_REVISION = "20260601_01_enterprise_baseline"


@dataclass(frozen=True, slots=True)
class Migration:
    revision: str
    description: str


MIGRATIONS = (
    Migration("20260601_00_legacy_bootstrap", "Record the idempotent SQLite schema bootstrap shipped before the migration ledger."),
    Migration(LATEST_SCHEMA_REVISION, "Add the enterprise baseline: source registry, readiness probes and migration visibility."),
)


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def ensure_migration_ledger(conn: sqlite3.Connection) -> dict:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
          revision TEXT PRIMARY KEY,
          description TEXT NOT NULL,
          applied_at TEXT NOT NULL
        )
        """
    )
    applied = {row[0] for row in conn.execute("SELECT revision FROM schema_migrations")}
    for migration in MIGRATIONS:
        if migration.revision not in applied:
            conn.execute(
                "INSERT INTO schema_migrations(revision,description,applied_at) VALUES(?,?,?)",
                (migration.revision, migration.description, _now()),
            )
    return migration_status(conn)


def migration_status(conn: sqlite3.Connection) -> dict:
    table_exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
    ).fetchone()
    if not table_exists:
        return {
            "status": "missing",
            "current_revision": "",
            "expected_revision": LATEST_SCHEMA_REVISION,
            "applied_revisions": [],
        }
    rows = list(conn.execute("SELECT revision,description,applied_at FROM schema_migrations ORDER BY revision"))
    revisions = [row[0] for row in rows]
    return {
        "status": "current" if LATEST_SCHEMA_REVISION in revisions else "outdated",
        "current_revision": revisions[-1] if revisions else "",
        "expected_revision": LATEST_SCHEMA_REVISION,
        "applied_revisions": [
            {"revision": row[0], "description": row[1], "applied_at": row[2]}
            for row in rows
        ],
    }
