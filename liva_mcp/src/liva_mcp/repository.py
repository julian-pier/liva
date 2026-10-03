from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Sequence
from urllib.parse import quote


_DENIED_ACTIONS = {
    sqlite3.SQLITE_INSERT,
    sqlite3.SQLITE_UPDATE,
    sqlite3.SQLITE_DELETE,
    sqlite3.SQLITE_CREATE_INDEX,
    sqlite3.SQLITE_CREATE_TABLE,
    sqlite3.SQLITE_CREATE_TEMP_INDEX,
    sqlite3.SQLITE_CREATE_TEMP_TABLE,
    sqlite3.SQLITE_CREATE_TEMP_TRIGGER,
    sqlite3.SQLITE_CREATE_TEMP_VIEW,
    sqlite3.SQLITE_CREATE_TRIGGER,
    sqlite3.SQLITE_CREATE_VIEW,
    sqlite3.SQLITE_DROP_INDEX,
    sqlite3.SQLITE_DROP_TABLE,
    sqlite3.SQLITE_DROP_TEMP_INDEX,
    sqlite3.SQLITE_DROP_TEMP_TABLE,
    sqlite3.SQLITE_DROP_TEMP_TRIGGER,
    sqlite3.SQLITE_DROP_TEMP_VIEW,
    sqlite3.SQLITE_DROP_TRIGGER,
    sqlite3.SQLITE_DROP_VIEW,
    sqlite3.SQLITE_ALTER_TABLE,
    sqlite3.SQLITE_REINDEX,
    sqlite3.SQLITE_ANALYZE,
    sqlite3.SQLITE_ATTACH,
    sqlite3.SQLITE_DETACH,
}


class ReadOnlyRepository:
    def __init__(self, database_path: Path):
        self.database_path = Path(database_path).resolve()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        if not self.database_path.is_file():
            raise FileNotFoundError("Required LIVA database is unavailable")
        uri = f"file:{quote(str(self.database_path), safe='/')}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=5, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")

        def authorizer(action: int, arg1: str | None, arg2: str | None, db: str | None, source: str | None) -> int:
            if action in _DENIED_ACTIONS:
                return sqlite3.SQLITE_DENY
            if action == sqlite3.SQLITE_PRAGMA and (arg1 or "").lower() != "query_only":
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        conn.set_authorizer(authorizer)
        try:
            yield conn
        finally:
            conn.close()

    def all(self, sql: str, parameters: Sequence[Any] = ()) -> list[dict[str, Any]]:
        if not sql.lstrip().upper().startswith(("SELECT", "WITH")):
            raise PermissionError("Only SELECT queries are allowed")
        with self.connect() as conn:
            return [dict(row) for row in conn.execute(sql, tuple(parameters)).fetchall()]

    def one(self, sql: str, parameters: Sequence[Any] = ()) -> dict[str, Any] | None:
        rows = self.all(sql, parameters)
        return rows[0] if rows else None

    def table_exists(self, table_name: str) -> bool:
        allowed = {
            "actions_v2_state", "autopilot_day", "core_override_choices", "core_training_cards",
            "gym_plans", "hrv_day_flags", "hrv_measurements", "nutrition_active_day_plan",
            "nutrition_daily", "nutrition_day_actuals", "nutrition_settings", "nutrition_week_plans",
            "nutrition_week_template_days", "nutrition_week_templates", "plans", "polar_continuous_samples",
            "polar_nightly_recharge", "polar_sleep", "recovery_day_annotations", "recovery_day_flags",
            "garmin_activity_files", "garmin_activity_laps", "garmin_activity_payloads",
            "garmin_activity_samples", "runs", "school_schedule_entries", "school_schedule_snapshots", "training_ai_decisions",
            "weight_logs", "workouts",
        }
        if table_name not in allowed:
            return False
        row = self.one("SELECT 1 AS present FROM sqlite_master WHERE type='table' AND name=?", (table_name,))
        return bool(row)
