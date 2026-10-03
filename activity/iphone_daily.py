from __future__ import annotations

import sqlite3
import uuid
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from database.connections import get_activity_db


BERLIN = ZoneInfo("Europe/Berlin")
SOURCE = "stayfree_ios"
DAILY_NAMESPACE = uuid.UUID("9eb5c74e-c6ad-49d8-91d7-388916bba693")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def usage_day_bounds(usage_day: str) -> tuple[str, str]:
    selected = date.fromisoformat(usage_day)
    start = datetime.combine(selected, time(hour=4), tzinfo=BERLIN)
    end = datetime.combine(selected + timedelta(days=1), time(hour=4), tzinfo=BERLIN)
    return start.isoformat(), end.isoformat()


def current_usage_day(now: datetime | None = None) -> str:
    local_now = (now or datetime.now(timezone.utc)).astimezone(BERLIN)
    if local_now.timetz().replace(tzinfo=None) < time(hour=4):
        local_now -= timedelta(days=1)
    return local_now.date().isoformat()


def ensure_iphone_daily_schema(conn: sqlite3.Connection | None = None) -> None:
    owns_connection = conn is None
    conn = conn or get_activity_db()
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS iphone_devices (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                model TEXT,
                system_version TEXT,
                app_version TEXT,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS iphone_daily_usage (
                id TEXT PRIMARY KEY,
                device_id TEXT NOT NULL REFERENCES iphone_devices(id) ON DELETE CASCADE,
                usage_day TEXT NOT NULL,
                day_start TEXT NOT NULL,
                day_end TEXT NOT NULL,
                total_usage_seconds INTEGER NOT NULL CHECK(total_usage_seconds >= 0),
                source TEXT NOT NULL CHECK(source = 'stayfree_ios'),
                source_updated_at TEXT,
                imported_at TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(device_id, usage_day, source)
            );

            CREATE INDEX IF NOT EXISTS idx_iphone_daily_usage_day
                ON iphone_daily_usage(usage_day, device_id);

            CREATE TABLE IF NOT EXISTS iphone_daily_apps (
                id TEXT PRIMARY KEY,
                daily_usage_id TEXT NOT NULL REFERENCES iphone_daily_usage(id) ON DELETE CASCADE,
                device_id TEXT NOT NULL REFERENCES iphone_devices(id) ON DELETE CASCADE,
                usage_day TEXT NOT NULL,
                app_key TEXT,
                app_identity TEXT NOT NULL,
                app_name TEXT NOT NULL,
                duration_seconds INTEGER NOT NULL CHECK(duration_seconds >= 0),
                category TEXT,
                source TEXT NOT NULL CHECK(source = 'stayfree_ios'),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(daily_usage_id, app_identity)
            );

            CREATE INDEX IF NOT EXISTS idx_iphone_daily_apps_day
                ON iphone_daily_apps(usage_day, device_id, duration_seconds DESC);

            CREATE TABLE IF NOT EXISTS iphone_import_runs (
                id TEXT PRIMARY KEY,
                device_id TEXT NOT NULL REFERENCES iphone_devices(id) ON DELETE CASCADE,
                usage_day TEXT NOT NULL,
                status TEXT NOT NULL,
                source TEXT NOT NULL CHECK(source = 'stayfree_ios'),
                started_at TEXT NOT NULL,
                finished_at TEXT NOT NULL,
                records_received INTEGER NOT NULL DEFAULT 0 CHECK(records_received >= 0),
                total_usage_seconds INTEGER NOT NULL DEFAULT 0 CHECK(total_usage_seconds >= 0),
                error_code TEXT,
                created_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_iphone_import_runs_day
                ON iphone_import_runs(usage_day, device_id, created_at DESC);
            """
        )
        conn.commit()
    finally:
        if owns_connection:
            conn.close()


def _daily_id(device_id: str, usage_day: str) -> str:
    return str(uuid.uuid5(DAILY_NAMESPACE, f"{SOURCE}:{device_id}:{usage_day}"))


def _app_identity(app: dict[str, Any]) -> str:
    key = str(app.get("app_key") or "").strip().casefold()
    return f"key:{key}" if key else f"name:{str(app['name']).strip().casefold()}"


def upsert_iphone_daily(device: dict[str, Any], daily: dict[str, Any]) -> dict[str, Any]:
    ensure_iphone_daily_schema()
    now = utc_now_iso()
    daily_id = _daily_id(device["id"], daily["usage_day"])
    run_id = str(uuid.uuid4())
    conn = get_activity_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT id, created_at FROM iphone_daily_usage WHERE device_id=? AND usage_day=? AND source=?",
            (device["id"], daily["usage_day"], SOURCE),
        ).fetchone()
        created_at = existing["created_at"] if existing else now
        if existing:
            daily_id = existing["id"]

        conn.execute(
            """
            INSERT INTO iphone_devices(
                id, name, model, system_version, app_version,
                first_seen_at, last_seen_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                name=excluded.name,
                model=COALESCE(excluded.model, iphone_devices.model),
                system_version=COALESCE(excluded.system_version, iphone_devices.system_version),
                app_version=COALESCE(excluded.app_version, iphone_devices.app_version),
                last_seen_at=excluded.last_seen_at,
                updated_at=excluded.updated_at
            """,
            (
                device["id"], device["name"], device.get("model"), device.get("system_version"),
                device.get("app_version"), now, now, now, now,
            ),
        )
        conn.execute(
            """
            INSERT INTO iphone_daily_usage(
                id, device_id, usage_day, day_start, day_end, total_usage_seconds,
                source, source_updated_at, imported_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(device_id, usage_day, source) DO UPDATE SET
                day_start=excluded.day_start,
                day_end=excluded.day_end,
                total_usage_seconds=excluded.total_usage_seconds,
                source_updated_at=excluded.source_updated_at,
                imported_at=excluded.imported_at,
                updated_at=excluded.updated_at
            """,
            (
                daily_id, device["id"], daily["usage_day"], daily["day_start"], daily["day_end"],
                daily["total_usage_seconds"], SOURCE, daily.get("source_updated_at"), now, created_at, now,
            ),
        )

        conn.execute("DELETE FROM iphone_daily_apps WHERE daily_usage_id=?", (daily_id,))
        for app in daily["apps"]:
            identity = _app_identity(app)
            app_id = str(uuid.uuid5(DAILY_NAMESPACE, f"{daily_id}:{identity}"))
            conn.execute(
                """
                INSERT INTO iphone_daily_apps(
                    id, daily_usage_id, device_id, usage_day, app_key, app_identity,
                    app_name, duration_seconds, category, source, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    app_id, daily_id, device["id"], daily["usage_day"], app.get("app_key"), identity,
                    app["name"], app["duration_seconds"], app.get("category"), SOURCE, now, now,
                ),
            )

        conn.execute(
            """
            INSERT INTO iphone_import_runs(
                id, device_id, usage_day, status, source, started_at, finished_at,
                records_received, total_usage_seconds, error_code, created_at
            ) VALUES (?, ?, ?, 'imported', ?, ?, ?, ?, ?, NULL, ?)
            """,
            (
                run_id, device["id"], daily["usage_day"], SOURCE, now, now,
                len(daily["apps"]), daily["total_usage_seconds"], now,
            ),
        )
        conn.commit()
        return {
            "daily_usage_id": daily_id,
            "usage_day": daily["usage_day"],
            "total_usage_seconds": daily["total_usage_seconds"],
            "apps_stored": len(daily["apps"]),
            "created": existing is None,
            "imported_at": now,
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def iphone_daily_for_day(usage_day: str, device_id: str | None = None) -> dict[str, Any]:
    ensure_iphone_daily_schema()
    conn = get_activity_db()
    try:
        params: list[Any] = [usage_day, SOURCE]
        device_clause = ""
        if device_id:
            device_clause = " AND u.device_id=?"
            params.append(device_id)
        row = conn.execute(
            f"""
            SELECT u.*, d.name AS device_name, d.model
            FROM iphone_daily_usage u
            JOIN iphone_devices d ON d.id=u.device_id
            WHERE u.usage_day=? AND u.source=?{device_clause}
            ORDER BY u.imported_at DESC LIMIT 1
            """,
            params,
        ).fetchone()
        if not row:
            selected = date.fromisoformat(usage_day)
            running = selected >= date.fromisoformat(current_usage_day())
            return {
                "available": False,
                "usage_day": usage_day,
                "status": "not_completed" if running else "not_imported",
                "total_usage_seconds": None,
                "apps": [],
            }
        apps = conn.execute(
            """
            SELECT app_key, app_name, duration_seconds, category
            FROM iphone_daily_apps
            WHERE daily_usage_id=?
            ORDER BY duration_seconds DESC, app_name COLLATE NOCASE ASC
            """,
            (row["id"],),
        ).fetchall()
        return {
            "available": True,
            "status": "complete",
            "device": {"id": row["device_id"], "name": row["device_name"], "model": row["model"]},
            "usage_day": row["usage_day"],
            "day_start": row["day_start"],
            "day_end": row["day_end"],
            "total_usage_seconds": int(row["total_usage_seconds"]),
            "source": row["source"],
            "source_updated_at": row["source_updated_at"],
            "imported_at": row["imported_at"],
            "apps": [
                {
                    "app_key": app["app_key"],
                    "name": app["app_name"],
                    "duration_seconds": int(app["duration_seconds"]),
                    "category": app["category"],
                }
                for app in apps
            ],
        }
    finally:
        conn.close()


def iphone_period_summary(start_day: str, end_day: str, device_id: str | None = None) -> dict[str, Any]:
    start = date.fromisoformat(start_day)
    end = date.fromisoformat(end_day)
    if end < start:
        raise ValueError("end_date before start_date")
    count = (end - start).days + 1
    if count > 90:
        raise ValueError("maximum period is 90 days")

    def collect(first: date, days: int, include_apps: bool) -> tuple[list[dict[str, Any]], dict[str, int]]:
        daily: list[dict[str, Any]] = []
        apps: dict[str, int] = {}
        for offset in range(days):
            day = (first + timedelta(days=offset)).isoformat()
            summary = iphone_daily_for_day(day, device_id)
            daily.append({
                "date": day,
                "available": bool(summary["available"]),
                "total_usage_seconds": summary["total_usage_seconds"],
            })
            if include_apps and summary["available"]:
                for app in summary["apps"]:
                    apps[app["name"]] = apps.get(app["name"], 0) + int(app["duration_seconds"])
        return daily, apps

    daily, apps = collect(start, count, True)
    previous, _ = collect(start - timedelta(days=count), count, False)
    available = [row for row in daily if row["available"]]
    previous_available = [row for row in previous if row["available"]]
    return {
        "daily": daily,
        "completed_days": len(available),
        "average_usage_seconds": round(sum(row["total_usage_seconds"] for row in available) / len(available), 3) if available else None,
        "previous_average_usage_seconds": round(sum(row["total_usage_seconds"] for row in previous_available) / len(previous_available), 3) if previous_available else None,
        "usage_by_app": [
            {"name": name, "seconds": seconds}
            for name, seconds in sorted(apps.items(), key=lambda item: (-item[1], item[0].casefold()))
        ],
    }
