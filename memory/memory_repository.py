from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from database.connections import get_core_db


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def ensure_memory_schema() -> None:
    conn = get_core_db()
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS memory_settings (
                key TEXT PRIMARY KEY,
                value TEXT,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS memory_file_registry (
                logical_name TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                folder_name TEXT NOT NULL,
                relative_path TEXT,
                last_known_modified_at TEXT,
                last_synced_at TEXT,
                metadata_json TEXT,
                updated_at TEXT NOT NULL
            )
            """
        )
        _ensure_column(conn, "memory_file_registry", "relative_path", "TEXT")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS memory_operation_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                operation_type TEXT NOT NULL,
                logical_name TEXT,
                status TEXT NOT NULL,
                detail_json TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_memory_operation_log_lookup
            ON memory_operation_log (operation_type, status, created_at DESC)
            """
        )
        conn.commit()
    finally:
        conn.close()


def get_setting(key: str, default: str | None = None) -> str | None:
    ensure_memory_schema()
    conn = get_core_db()
    try:
        row = conn.execute("SELECT value FROM memory_settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default
    finally:
        conn.close()


def set_setting(key: str, value: str | None) -> None:
    ensure_memory_schema()
    conn = get_core_db()
    now = utc_now_iso()
    try:
        conn.execute(
            """
            INSERT INTO memory_settings (key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = excluded.updated_at
            """,
            (key, value, now),
        )
        conn.commit()
    finally:
        conn.close()


def get_registry_entry(logical_name: str) -> dict[str, Any] | None:
    ensure_memory_schema()
    conn = get_core_db()
    try:
        row = conn.execute("SELECT * FROM memory_file_registry WHERE logical_name = ?", (logical_name,)).fetchone()
        return _row_to_registry_entry(row)
    finally:
        conn.close()


def list_registry_entries() -> list[dict[str, Any]]:
    ensure_memory_schema()
    conn = get_core_db()
    try:
        rows = conn.execute("SELECT * FROM memory_file_registry ORDER BY logical_name").fetchall()
        return [_row_to_registry_entry(row) for row in rows]
    finally:
        conn.close()


def upsert_registry_entry(
    logical_name: str,
    *,
    title: str,
    folder_name: str,
    relative_path: str | None = None,
    last_known_modified_at: str | None = None,
    last_synced_at: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    ensure_memory_schema()
    conn = get_core_db()
    now = utc_now_iso()
    metadata_json = json.dumps(metadata or {}, ensure_ascii=False, separators=(",", ":"))
    try:
        conn.execute(
            """
            INSERT INTO memory_file_registry (
                logical_name,
                title,
                folder_name,
                relative_path,
                last_known_modified_at,
                last_synced_at,
                metadata_json,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(logical_name) DO UPDATE SET
                title = excluded.title,
                folder_name = excluded.folder_name,
                relative_path = COALESCE(excluded.relative_path, memory_file_registry.relative_path),
                last_known_modified_at = COALESCE(excluded.last_known_modified_at, memory_file_registry.last_known_modified_at),
                last_synced_at = COALESCE(excluded.last_synced_at, memory_file_registry.last_synced_at),
                metadata_json = excluded.metadata_json,
                updated_at = excluded.updated_at
            """,
            (
                logical_name,
                title,
                folder_name,
                relative_path,
                last_known_modified_at,
                last_synced_at,
                metadata_json,
                now,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def record_operation(
    operation_type: str,
    *,
    status: str,
    logical_name: str | None = None,
    detail: dict[str, Any] | None = None,
) -> None:
    ensure_memory_schema()
    conn = get_core_db()
    try:
        conn.execute(
            """
            INSERT INTO memory_operation_log (operation_type, logical_name, status, detail_json, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                operation_type,
                logical_name,
                status,
                json.dumps(detail or {}, ensure_ascii=False, separators=(",", ":")),
                utc_now_iso(),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def latest_successful_operation(operation_type: str | None = None) -> dict[str, Any] | None:
    ensure_memory_schema()
    conn = get_core_db()
    try:
        if operation_type:
            row = conn.execute(
                """
                SELECT * FROM memory_operation_log
                WHERE status = 'success' AND operation_type = ?
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (operation_type,),
            ).fetchone()
        else:
            row = conn.execute(
                """
                SELECT * FROM memory_operation_log
                WHERE status = 'success'
                ORDER BY created_at DESC
                LIMIT 1
                """
            ).fetchone()
        if not row:
            return None
        return {
            "id": row["id"],
            "operation_type": row["operation_type"],
            "logical_name": row["logical_name"],
            "status": row["status"],
            "detail": json.loads(row["detail_json"] or "{}"),
            "created_at": row["created_at"],
        }
    finally:
        conn.close()


def _row_to_registry_entry(row) -> dict[str, Any] | None:
    if row is None:
        return None
    metadata = json.loads(row["metadata_json"] or "{}")
    return {
        "logical_name": row["logical_name"],
        "title": row["title"],
        "folder_name": row["folder_name"],
        "relative_path": row["relative_path"],
        "last_known_modified_at": row["last_known_modified_at"],
        "last_synced_at": row["last_synced_at"],
        "updated_at": row["updated_at"],
        "metadata": metadata,
    }


def _ensure_column(conn, table_name: str, column_name: str, column_sql: str) -> None:
    rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    if any(row["name"] == column_name for row in rows):
        return
    conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_sql}")
