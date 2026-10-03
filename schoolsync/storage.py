from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from database.connections import get_training_db

from .models import DaySummary, ScheduleEntry

LOGGER = logging.getLogger(__name__)


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def write_latest_json(path: Path, payload: dict[str, Any]) -> None:
    ensure_parent(path)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def ensure_schema() -> None:
    conn = get_training_db()
    try:
        cur = conn.cursor()
        cur.executescript(
            """
            CREATE TABLE IF NOT EXISTS school_schedule_snapshots (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              fetched_at TEXT NOT NULL,
              source TEXT NOT NULL,
              date TEXT NOT NULL,
              raw_json TEXT NOT NULL,
              derived_summary_json TEXT NOT NULL,
              created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE INDEX IF NOT EXISTS idx_school_schedule_snapshots_date
              ON school_schedule_snapshots(date, fetched_at);

            CREATE TABLE IF NOT EXISTS school_schedule_entries (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              snapshot_id INTEGER NOT NULL,
              date TEXT NOT NULL,
              subject TEXT,
              teacher TEXT,
              room TEXT,
              start_time TEXT,
              end_time TEXT,
              raw_text TEXT NOT NULL,
              status_hint TEXT NOT NULL,
              FOREIGN KEY(snapshot_id) REFERENCES school_schedule_snapshots(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_school_schedule_entries_snapshot
              ON school_schedule_entries(snapshot_id);
            """
        )
        conn.commit()
    finally:
        conn.close()


def persist_snapshot(
    *,
    source: str,
    date_iso: str,
    entries: list[ScheduleEntry],
    summary: DaySummary,
    fetched_at: str | None = None,
) -> int:
    ensure_schema()
    fetched_at_iso = fetched_at or datetime.now().isoformat(timespec="seconds")
    raw_payload = {"entries": [entry.to_dict() for entry in entries]}
    summary_payload = summary.to_dict()

    conn = get_training_db()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO school_schedule_snapshots
              (fetched_at, source, date, raw_json, derived_summary_json)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                fetched_at_iso,
                source,
                date_iso,
                json.dumps(raw_payload, ensure_ascii=False),
                json.dumps(summary_payload, ensure_ascii=False),
            ),
        )
        snapshot_id = int(cur.lastrowid)

        cur.executemany(
            """
            INSERT INTO school_schedule_entries
              (snapshot_id, date, subject, teacher, room, start_time, end_time, raw_text, status_hint)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    snapshot_id,
                    entry.date,
                    entry.subject,
                    entry.teacher,
                    entry.room,
                    entry.start_time,
                    entry.end_time,
                    entry.raw_text,
                    entry.status_hint,
                )
                for entry in entries
            ],
        )
        conn.commit()
        LOGGER.info("Stored SchoolSync snapshot id=%s date=%s entries=%s", snapshot_id, date_iso, len(entries))
        return snapshot_id
    finally:
        conn.close()
