#!/usr/bin/env python3
from __future__ import annotations

from database.connections import get_auth_db, get_training_db


def _table_exists(conn, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
        (table,),
    ).fetchone()
    return bool(row)


def main() -> None:
    src = get_training_db()
    dst = get_auth_db()
    moved_keys = 0
    moved_logs = 0
    try:
        dst.execute(
            """
            CREATE TABLE IF NOT EXISTS access_keys (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                label TEXT NOT NULL,
                key_hash TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                revoked_at TEXT,
                last_seen_at TEXT,
                last_ip TEXT,
                last_user_agent TEXT
            )
            """
        )
        dst.execute(
            """
            CREATE TABLE IF NOT EXISTS access_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                key_id INTEGER,
                session_id INTEGER,
                method TEXT,
                path TEXT,
                status INTEGER,
                ip TEXT,
                user_agent TEXT,
                FOREIGN KEY(key_id) REFERENCES access_keys(id)
            )
            """
        )
        dst.execute("CREATE INDEX IF NOT EXISTS idx_access_keys_active ON access_keys(revoked_at, expires_at)")
        dst.execute("CREATE INDEX IF NOT EXISTS idx_access_log_key_ts ON access_log(key_id, ts)")

        if _table_exists(src, "access_keys"):
            rows = src.execute(
                """
                SELECT id, label, key_hash, created_at, expires_at, revoked_at, last_seen_at, last_ip, last_user_agent
                FROM access_keys
                """
            ).fetchall()
            for row in rows:
                dst.execute(
                    """
                    INSERT OR IGNORE INTO access_keys
                    (id, label, key_hash, created_at, expires_at, revoked_at, last_seen_at, last_ip, last_user_agent)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        int(row["id"]),
                        row["label"],
                        row["key_hash"],
                        row["created_at"],
                        row["expires_at"],
                        row["revoked_at"],
                        row["last_seen_at"],
                        row["last_ip"],
                        row["last_user_agent"],
                    ),
                )
                moved_keys += 1

        if _table_exists(src, "access_log"):
            rows = src.execute(
                """
                SELECT id, ts, key_id, session_id, method, path, status, ip, user_agent
                FROM access_log
                """
            ).fetchall()
            for row in rows:
                dst.execute(
                    """
                    INSERT OR IGNORE INTO access_log
                    (id, ts, key_id, session_id, method, path, status, ip, user_agent)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        int(row["id"]),
                        row["ts"],
                        row["key_id"],
                        row["session_id"],
                        row["method"],
                        row["path"],
                        row["status"],
                        row["ip"],
                        row["user_agent"],
                    ),
                )
                moved_logs += 1

        dst.commit()
        print(f"migration_done moved_access_keys={moved_keys} moved_access_log={moved_logs}")
    finally:
        src.close()
        dst.close()


if __name__ == "__main__":
    main()
