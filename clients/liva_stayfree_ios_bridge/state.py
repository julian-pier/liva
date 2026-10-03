from __future__ import annotations

import sqlite3
from pathlib import Path


class ImportState:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS import_state (
                    usage_day TEXT PRIMARY KEY,
                    payload_hash TEXT,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_attempt_at TEXT,
                    last_success_at TEXT,
                    last_error TEXT
                )
                """
            )

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=5)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def is_current(self, usage_day: str, payload_hash: str) -> bool:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT status, payload_hash FROM import_state WHERE usage_day=?", (usage_day,)
            ).fetchone()
        return bool(row and row["status"] == "imported" and row["payload_hash"] == payload_hash)

    def record_attempt(self, usage_day: str, payload_hash: str, attempted_at: str) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO import_state(usage_day, payload_hash, status, attempts, last_attempt_at)
                VALUES (?, ?, 'attempting', 1, ?)
                ON CONFLICT(usage_day) DO UPDATE SET
                    payload_hash=excluded.payload_hash,
                    status='attempting',
                    attempts=import_state.attempts + 1,
                    last_attempt_at=excluded.last_attempt_at,
                    last_error=NULL
                """,
                (usage_day, payload_hash, attempted_at),
            )

    def record_success(self, usage_day: str, payload_hash: str, succeeded_at: str) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE import_state
                SET payload_hash=?, status='imported', last_success_at=?, last_error=NULL
                WHERE usage_day=?
                """,
                (payload_hash, succeeded_at, usage_day),
            )

    def record_error(self, usage_day: str, payload_hash: str, attempted_at: str, error: str) -> None:
        safe_error = str(error).replace("\r", " ").replace("\n", " ")[:500]
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO import_state(
                    usage_day, payload_hash, status, attempts, last_attempt_at, last_error
                ) VALUES (?, ?, 'retry', 1, ?, ?)
                ON CONFLICT(usage_day) DO UPDATE SET
                    payload_hash=excluded.payload_hash,
                    status='retry',
                    last_attempt_at=excluded.last_attempt_at,
                    last_error=excluded.last_error
                """,
                (usage_day, payload_hash, attempted_at, safe_error),
            )

    def record_waiting(self, usage_day: str, attempted_at: str) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO import_state(usage_day, status, attempts, last_attempt_at, last_error)
                VALUES (?, 'waiting_for_stayfree', 1, ?, 'waiting_for_stayfree')
                ON CONFLICT(usage_day) DO UPDATE SET
                    status='waiting_for_stayfree',
                    attempts=import_state.attempts + 1,
                    last_attempt_at=excluded.last_attempt_at,
                    last_error='waiting_for_stayfree'
                """,
                (usage_day, attempted_at),
            )
