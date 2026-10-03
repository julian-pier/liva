from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

from .models import ActivitySegment


class ActivitySpool:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS segments (
                    id TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    synced INTEGER NOT NULL DEFAULT 0,
                    revision INTEGER NOT NULL DEFAULT 1,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS idx_spool_unsynced ON segments(synced, updated_at);
                """
            )
            columns = {row[1] for row in conn.execute("PRAGMA table_info(segments)")}
            if "revision" not in columns:
                conn.execute("ALTER TABLE segments ADD COLUMN revision INTEGER NOT NULL DEFAULT 1")

    def upsert(self, segment: ActivitySegment) -> None:
        payload = json.dumps(segment.to_payload(), ensure_ascii=False, separators=(",", ":"))
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO segments(id, payload, synced, revision, updated_at) VALUES (?, ?, 0, 1, CURRENT_TIMESTAMP)
                ON CONFLICT(id) DO UPDATE SET
                    payload=excluded.payload, synced=0, revision=segments.revision + 1,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (segment.id, payload),
            )

    def pending(self, limit: int) -> list[dict]:
        with self._lock, self._connect() as conn:
            rows = conn.execute("SELECT payload FROM segments WHERE synced=0 ORDER BY updated_at, id LIMIT ?", (limit,)).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def pending_with_revisions(self, limit: int) -> list[tuple[dict, int]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT payload, revision FROM segments WHERE synced=0 ORDER BY updated_at, id LIMIT ?",
                (limit,),
            ).fetchall()
        return [(json.loads(row["payload"]), int(row["revision"])) for row in rows]

    def mark_synced(self, ids: list[str]) -> None:
        if not ids:
            return
        placeholders = ",".join("?" for _ in ids)
        with self._lock, self._connect() as conn:
            conn.execute(f"UPDATE segments SET synced=1 WHERE id IN ({placeholders})", ids)

    def mark_synced_revisions(self, acknowledgements: list[tuple[str, int]]) -> None:
        if not acknowledgements:
            return
        with self._lock, self._connect() as conn:
            conn.executemany(
                "UPDATE segments SET synced=1 WHERE id=? AND revision=?",
                acknowledgements,
            )

    def pending_count(self) -> int:
        with self._lock, self._connect() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM segments WHERE synced=0").fetchone()[0])
