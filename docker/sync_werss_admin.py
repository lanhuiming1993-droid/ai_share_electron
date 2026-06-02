from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import bcrypt


DB_PATH = Path("/app/data/db.db")


def table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    ).fetchone()
    return bool(row)


def main() -> None:
    username = os.environ.get("USERNAME", "admin").strip()
    password = os.environ.get("PASSWORD", "admin@123")
    if not DB_PATH.exists() or not username or not password:
        return
    with sqlite3.connect(DB_PATH, timeout=20) as conn:
        if not table_exists(conn, "users"):
            return
        row = conn.execute(
            "SELECT id,username,password_hash FROM users WHERE username=? OR id='0' ORDER BY CASE WHEN username=? THEN 0 ELSE 1 END LIMIT 1",
            (username, username),
        ).fetchone()
        if not row:
            return
        password_matches = False
        try:
            password_matches = bcrypt.checkpw(password.encode("utf-8"), row[2].encode("utf-8"))
        except (TypeError, ValueError):
            pass
        if row[1] == username and password_matches:
            return
        password_hash = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
        conn.execute(
            "UPDATE users SET username=?,password_hash=?,role='admin',is_active=1 WHERE id=?",
            (username, password_hash, row[0]),
        )
        conn.commit()
        print("AlphaDesk synchronized the WeRSS administrator credentials.")


if __name__ == "__main__":
    main()
