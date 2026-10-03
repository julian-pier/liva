import json
import math
import re
import sqlite3
import hashlib
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, date, timedelta
from typing import Any, Dict, List, Optional, Sequence, Tuple
from uuid import uuid4
from zoneinfo import ZoneInfo

from database import connections as db_connections
from database.connections import get_nutrition_db

from .planning_mealplan_import import parse_mealplan_text


@dataclass(frozen=True)
class MacroTotals:
    kcal: float
    p: float
    c: float
    f: float
    sugar: float = 0.0
    salt: float = 0.0

    def scaled(self, factor: float) -> "MacroTotals":
        return MacroTotals(
            kcal=self.kcal * factor,
            p=self.p * factor,
            c=self.c * factor,
            f=self.f * factor,
            sugar=self.sugar * factor,
            salt=self.salt * factor,
        )

    def add(self, other: "MacroTotals") -> "MacroTotals":
        return MacroTotals(
            kcal=self.kcal + other.kcal,
            p=self.p + other.p,
            c=self.c + other.c,
            f=self.f + other.f,
            sugar=self.sugar + other.sugar,
            salt=self.salt + other.salt,
        )


def _utcnow_iso() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat()


def _clamp_float(value: Any, lo: Optional[float] = None, hi: Optional[float] = None) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            x = float(value)
        except Exception:
            return None
    else:
        text = str(value).strip()
        if text == "":
            return None
        text = text.replace(",", ".")
        try:
            x = float(text)
        except Exception:
            return None
    if not math.isfinite(x):
        return None
    if lo is not None and x < lo:
        x = lo
    if hi is not None and x > hi:
        x = hi
    return x


def _as_int_bool(value: Any) -> int:
    return 1 if bool(value) else 0


def _column_exists(cur, table: str, column: str) -> bool:
    rows = cur.execute(f"PRAGMA table_info({table})").fetchall()
    return any((row[1] == column) for row in rows)


def _ensure_column(cur, table: str, column_def: str) -> None:
    column_name = column_def.strip().split()[0]
    if column_name.endswith("("):
        column_name = column_name[:-1]
    if not _column_exists(cur, table, column_name):
        cur.execute(f"ALTER TABLE {table} ADD COLUMN {column_def}")


def _ensure_nutrition_settings_table(cur) -> None:
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS nutrition_settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
        """
    )


def _column_notnull(cur, table: str, column: str) -> bool:
    rows = cur.execute(f"PRAGMA table_info({table})").fetchall()
    for row in rows:
        if row[1] == column:
            return bool(row[3])
    return False


def _ensure_slot_meal_overrides_nullable(cur) -> None:
    if not _column_notnull(cur, "nutrition_slot_meal_overrides", "meal_template_id"):
        return
    cur.execute("ALTER TABLE nutrition_slot_meal_overrides RENAME TO nutrition_slot_meal_overrides_old")
    cur.execute(
        """
        CREATE TABLE nutrition_slot_meal_overrides (
            slot_id INTEGER PRIMARY KEY,
            meal_template_id INTEGER NULL,
            ingredients_json TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(slot_id) REFERENCES nutrition_week_day_slots(id) ON DELETE CASCADE
        )
        """
    )
    cur.execute(
        """
        INSERT INTO nutrition_slot_meal_overrides (slot_id, meal_template_id, ingredients_json, updated_at)
        SELECT slot_id, meal_template_id, ingredients_json, updated_at
        FROM nutrition_slot_meal_overrides_old
        """
    )
    cur.execute("DROP TABLE nutrition_slot_meal_overrides_old")


def _backfill_common_portions(cur) -> None:
    # Common single-piece foods need a gram equivalent so pcs-based macros are
    # not treated as 1g.  Older imports sometimes stored ``1`` as the common
    # portion while the actual gram weight was available in ``portion_g``.
    cur.execute(
        """
        UPDATE nutrition_foods
        SET common_portion_size=portion_g
        WHERE lower(COALESCE(unit_default, '')) IN ('pcs', 'piece', 'pieces', 'stück')
          AND portion_g IS NOT NULL
          AND portion_g > 1
          AND (common_portion_size IS NULL OR common_portion_size <= 1)
        """
    )
    cur.execute(
        """
        UPDATE nutrition_foods
        SET common_portion_size=180.0,
            portion_g=COALESCE(portion_g, 180.0)
        WHERE name_normalized='apfel'
          AND (common_portion_size IS NULL OR common_portion_size <= 0)
        """
    )


def _table_has_rows(cur, table: str) -> bool:
    row = cur.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone()
    return row is not None


DEFAULT_WEEK_TARGETS = {
    "standard": {
        "kcal": 2550,
        "p": 190,
        "c": 240,
        "f": 80,
    },
    "weekend": {
        "kcal": 2350,
        "p": 170,
        "c": 210,
        "f": 70,
    },
}
WEEKDAY_LABELS = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]
DEFAULT_BASE_SLOT_COUNT = 4
LOGGING_STATUS_VALUES = {
    "planned",
    "open",
    "shifted",
    "adjusted_by_core",
    "logged",
    "changed",
    "skipped",
    "catch_up",
    "manual_override",
    "telegram_confirmed",
}


SLOT_TEMPLATES = [
    {
        "key": "meal_1",
        "title": "Meal 1",
        "default_time": "06:40",
        "context_tag": "meal",
        "macro_intent": None,
    },
    {
        "key": "meal_2",
        "title": "Meal 2",
        "default_time": "09:30",
        "context_tag": "meal",
        "macro_intent": None,
    },
    {
        "key": "meal_3",
        "title": "Meal 3",
        "default_time": "14:00",
        "context_tag": "meal",
        "macro_intent": None,
    },
    {
        "key": "meal_4",
        "title": "Meal 4",
        "default_time": "16:00",
        "context_tag": "meal",
        "macro_intent": None,
    },
    {
        "key": "meal_5",
        "title": "Meal 5",
        "default_time": "18:30",
        "context_tag": "meal",
        "macro_intent": None,
    },
    {
        "key": "meal_6",
        "title": "Meal 6",
        "default_time": "20:30",
        "context_tag": "meal",
        "macro_intent": None,
    },
    {
        "key": "meal_7",
        "title": "Meal 7",
        "default_time": "21:30",
        "context_tag": "meal",
        "macro_intent": None,
    },
]

WEEK_TEMPLATE_TAGS = [
    {"weekday": i, "day_type": "weekend" if i >= 5 else "standard"} for i in range(7)
]

_SCHEMA_READY_FOR: set[str] = set()


def ensure_nutrition_planning_schema():
    schema_key = str(db_connections.NUTRITION_DB)
    if schema_key in _SCHEMA_READY_FOR:
        return
    conn = get_nutrition_db()
    cur = conn.cursor()
    _ensure_nutrition_settings_table(cur)

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS nutrition_foods (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            brand TEXT NULL,
            unit_default TEXT NOT NULL DEFAULT 'g',
            portion_g REAL NULL,
            common_portion_size REAL NULL,
            mfp_search_hint TEXT NULL,
            category TEXT NULL,
            kcal_per_100 REAL NOT NULL,
            p_per_100 REAL NOT NULL,
            c_per_100 REAL NOT NULL,
            f_per_100 REAL NOT NULL,
            sugar_per_100 REAL NULL,
            salt_per_100 REAL NULL,
            tags TEXT NULL,
            is_favorite INTEGER NOT NULL DEFAULT 0,
            is_active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    _ensure_column(cur, "nutrition_foods", "common_portion_size REAL NULL")
    _ensure_column(cur, "nutrition_foods", "mfp_search_hint TEXT NULL")
    _ensure_column(cur, "nutrition_foods", "category TEXT NULL")
    _ensure_column(cur, "nutrition_foods", "is_active INTEGER NOT NULL DEFAULT 1")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_nutrition_foods_name ON nutrition_foods(name)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_nutrition_foods_tags ON nutrition_foods(tags)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_nutrition_foods_active ON nutrition_foods(is_active)")
    _ensure_column(cur, "nutrition_foods", "name_normalized TEXT NULL")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_nutrition_foods_name_norm ON nutrition_foods(name_normalized)")
    cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_nutrition_foods_name_norm ON nutrition_foods(name_normalized)")
    cur.execute("UPDATE nutrition_foods SET name_normalized=LOWER(TRIM(name)) WHERE name_normalized IS NULL OR name_normalized=''")
    _backfill_common_portions(cur)

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS nutrition_meal_templates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            description TEXT NULL,
            tags TEXT NULL,
            default_servings REAL NOT NULL DEFAULT 1.0,
            mfp_alias TEXT NULL,
            export_text TEXT NULL,
            notes TEXT NULL,
            kcal_per_serving REAL NULL,
            p_per_serving REAL NULL,
            c_per_serving REAL NULL,
            f_per_serving REAL NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    _ensure_column(cur, "nutrition_meal_templates", "description TEXT NULL")
    _ensure_column(cur, "nutrition_meal_templates", "tags TEXT NULL")
    _ensure_column(cur, "nutrition_meal_templates", "default_servings REAL NOT NULL DEFAULT 1.0")
    _ensure_column(cur, "nutrition_meal_templates", "mfp_alias TEXT NULL")
    _ensure_column(cur, "nutrition_meal_templates", "export_text TEXT NULL")
    _ensure_column(cur, "nutrition_meal_templates", "notes TEXT NULL")
    _ensure_column(cur, "nutrition_meal_templates", "kcal_per_serving REAL NULL")
    _ensure_column(cur, "nutrition_meal_templates", "p_per_serving REAL NULL")
    _ensure_column(cur, "nutrition_meal_templates", "c_per_serving REAL NULL")
    _ensure_column(cur, "nutrition_meal_templates", "f_per_serving REAL NULL")
    _ensure_column(cur, "nutrition_meal_templates", "signature TEXT NULL")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_nutrition_meal_templates_title ON nutrition_meal_templates(title)")
    cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_nutrition_meal_templates_signature ON nutrition_meal_templates(signature)")

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS nutrition_meal_ingredients (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            meal_template_id INTEGER NOT NULL,
            food_id INTEGER NOT NULL,
            amount REAL NOT NULL,
            unit TEXT NOT NULL DEFAULT 'g',
            sort_index INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(meal_template_id) REFERENCES nutrition_meal_templates(id) ON DELETE CASCADE,
            FOREIGN KEY(food_id) REFERENCES nutrition_foods(id) ON DELETE RESTRICT
        )
        """
    )
    _ensure_column(cur, "nutrition_meal_ingredients", "unit TEXT NOT NULL DEFAULT 'g'")
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_nutrition_meal_ingredients_meal ON nutrition_meal_ingredients(meal_template_id, sort_index)")

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS nutrition_meal_ingredient_raw (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            meal_template_id INTEGER NOT NULL,
            raw_name TEXT NULL,
            amount REAL NULL,
            unit TEXT NULL,
            kcal_value REAL NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(meal_template_id) REFERENCES nutrition_meal_templates(id) ON DELETE CASCADE
        )
        """
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_nutrition_meal_ingredient_raw_meal ON nutrition_meal_ingredient_raw(meal_template_id)")

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS nutrition_slot_templates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            default_time TEXT NULL,
            context_tag TEXT NULL,
            macro_intent TEXT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    _ensure_column(cur, "nutrition_slot_templates", "default_time TEXT NULL")
    _ensure_column(cur, "nutrition_slot_templates", "context_tag TEXT NULL")
    _ensure_column(cur, "nutrition_slot_templates", "macro_intent TEXT NULL")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_slot_templates_title ON nutrition_slot_templates(title)")

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS nutrition_slot_options (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            slot_template_id INTEGER NOT NULL,
            meal_template_id INTEGER NOT NULL,
            default_servings REAL NOT NULL DEFAULT 1.0,
            weight REAL NOT NULL DEFAULT 1.0,
            priority INTEGER NOT NULL DEFAULT 0,
            is_quick INTEGER NOT NULL DEFAULT 0,
            is_clean INTEGER NOT NULL DEFAULT 0,
            is_cheap INTEGER NOT NULL DEFAULT 0,
            is_high_protein INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(slot_template_id) REFERENCES nutrition_slot_templates(id) ON DELETE CASCADE,
            FOREIGN KEY(meal_template_id) REFERENCES nutrition_meal_templates(id) ON DELETE CASCADE
        )
        """
    )
    _ensure_column(cur, "nutrition_slot_options", "is_quick INTEGER NOT NULL DEFAULT 0")
    _ensure_column(cur, "nutrition_slot_options", "is_clean INTEGER NOT NULL DEFAULT 0")
    _ensure_column(cur, "nutrition_slot_options", "is_cheap INTEGER NOT NULL DEFAULT 0")
    _ensure_column(cur, "nutrition_slot_options", "is_high_protein INTEGER NOT NULL DEFAULT 0")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_slot_options_slot ON nutrition_slot_options(slot_template_id)")

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS nutrition_week_templates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            is_active INTEGER NOT NULL DEFAULT 1,
            archived_at TEXT NULL,
            last_used_at TEXT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    _ensure_column(cur, "nutrition_week_templates", "archived_at TEXT NULL")
    _ensure_column(cur, "nutrition_week_templates", "last_used_at TEXT NULL")
    _ensure_column(cur, "nutrition_week_templates", "macro_active_mode TEXT NULL")
    _ensure_column(cur, "nutrition_week_templates", "macro_modes_json TEXT NULL")
    _ensure_column(cur, "nutrition_week_templates", "revision INTEGER NOT NULL DEFAULT 1")
    _ensure_week_template_timeline_schema(cur)

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS nutrition_week_template_days (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            week_template_id INTEGER NOT NULL,
            weekday INTEGER NOT NULL,
            day_type TEXT NOT NULL DEFAULT 'standard',
            target_kcal REAL NULL,
            target_p REAL NULL,
            target_c REAL NULL,
            target_f REAL NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(week_template_id, weekday),
            FOREIGN KEY(week_template_id) REFERENCES nutrition_week_templates(id) ON DELETE CASCADE
        )
        """
    )
    cur.execute("CREATE INDEX IF NOT EXISTS idx_week_template_days_template ON nutrition_week_template_days(week_template_id)")

    # A day group is a durable link between weekday rows.  The weekday rows
    # intentionally remain materialized: old exports, date overrides and the
    # daily resolver can continue to address a concrete weekday without a
    # compatibility layer.  Group mutations are applied atomically to every
    # member, making the group the user-visible source of truth.
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS nutrition_week_day_groups (
            id TEXT PRIMARY KEY,
            week_template_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            color TEXT NOT NULL DEFAULT '#f6b93b',
            source_week_template_day_id INTEGER NOT NULL,
            revision INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(week_template_id) REFERENCES nutrition_week_templates(id) ON DELETE CASCADE,
            FOREIGN KEY(source_week_template_day_id) REFERENCES nutrition_week_template_days(id) ON DELETE CASCADE
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS nutrition_week_day_group_members (
            group_id TEXT NOT NULL,
            week_template_day_id INTEGER NOT NULL UNIQUE,
            created_at TEXT NOT NULL,
            PRIMARY KEY(group_id, week_template_day_id),
            FOREIGN KEY(group_id) REFERENCES nutrition_week_day_groups(id) ON DELETE CASCADE,
            FOREIGN KEY(week_template_day_id) REFERENCES nutrition_week_template_days(id) ON DELETE CASCADE
        )
        """
    )
    cur.execute("CREATE INDEX IF NOT EXISTS idx_week_day_group_members_group ON nutrition_week_day_group_members(group_id)")
    # Repair early partial groups from interrupted/older link operations.
    cur.execute(
        """DELETE FROM nutrition_week_day_groups
           WHERE id IN (
             SELECT g.id FROM nutrition_week_day_groups g
             LEFT JOIN nutrition_week_day_group_members m ON m.group_id=g.id
             GROUP BY g.id HAVING COUNT(m.week_template_day_id) < 2
        )"""
    )
    # Give existing links a stable visual identity as well.  Old rows all used
    # the initial yellow default, which made multiple compositions ambiguous.
    color_rows = cur.execute("SELECT id, week_template_id, color FROM nutrition_week_day_groups ORDER BY week_template_id, created_at, id").fetchall()
    color_index_by_template: Dict[int, int] = {}
    for color_row in color_rows:
        template_key = int(color_row["week_template_id"])
        index = color_index_by_template.get(template_key, 0)
        color_index_by_template[template_key] = index + 1
        if color_row["color"] not in DAY_GROUP_COLORS:
            cur.execute("UPDATE nutrition_week_day_groups SET color=? WHERE id=?", (DAY_GROUP_COLORS[index % len(DAY_GROUP_COLORS)], color_row["id"]))

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS nutrition_week_day_slots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            week_template_day_id INTEGER NOT NULL,
            slot_template_id INTEGER NOT NULL,
            slot_index INTEGER NOT NULL,
            active_option_id INTEGER NULL,
            locked INTEGER NOT NULL DEFAULT 0,
            time_text TEXT NULL,
            custom_title TEXT NULL,
            note_text TEXT NULL,
            meal_template_id INTEGER NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(week_template_day_id, slot_index),
            FOREIGN KEY(week_template_day_id) REFERENCES nutrition_week_template_days(id) ON DELETE CASCADE,
            FOREIGN KEY(slot_template_id) REFERENCES nutrition_slot_templates(id) ON DELETE RESTRICT,
            FOREIGN KEY(active_option_id) REFERENCES nutrition_slot_options(id) ON DELETE SET NULL,
            FOREIGN KEY(meal_template_id) REFERENCES nutrition_meal_templates(id) ON DELETE SET NULL
        )
        """
    )
    _ensure_column(cur, "nutrition_week_day_slots", "time_text TEXT NULL")
    _ensure_column(cur, "nutrition_week_day_slots", "custom_title TEXT NULL")
    _ensure_column(cur, "nutrition_week_day_slots", "note_text TEXT NULL")
    _ensure_column(cur, "nutrition_week_day_slots", "meal_template_id INTEGER NULL")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_week_day_slots_template ON nutrition_week_day_slots(week_template_day_id)")
    cur.execute(
        """
        UPDATE nutrition_week_day_slots
        SET meal_template_id = (
            SELECT meal_template_id FROM nutrition_slot_options WHERE nutrition_slot_options.id = nutrition_week_day_slots.active_option_id
        )
        WHERE meal_template_id IS NULL AND active_option_id IS NOT NULL
        """
    )
    cur.execute("UPDATE nutrition_week_day_slots SET active_option_id=NULL WHERE active_option_id IS NOT NULL")

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS nutrition_date_overrides (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            override_date TEXT NOT NULL,
            slot_id INTEGER NOT NULL,
            meal_template_id INTEGER NULL,
            notes TEXT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(override_date, slot_id)
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS nutrition_export_cache (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cache_key TEXT NOT NULL UNIQUE,
            payload TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS nutrition_slot_meal_overrides (
            slot_id INTEGER PRIMARY KEY,
            meal_template_id INTEGER NULL,
            ingredients_json TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(slot_id) REFERENCES nutrition_week_day_slots(id) ON DELETE CASCADE
        )
        """
    )
    _ensure_slot_meal_overrides_nullable(cur)

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS nutrition_slot_meals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            week_template_day_id INTEGER NOT NULL,
            slot_index INTEGER NOT NULL,
            meal_title TEXT NOT NULL,
            multiplier REAL NOT NULL DEFAULT 1.0,
            template_id INTEGER NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(week_template_day_id, slot_index),
            FOREIGN KEY(week_template_day_id) REFERENCES nutrition_week_template_days(id) ON DELETE CASCADE,
            FOREIGN KEY(template_id) REFERENCES nutrition_meal_templates(id) ON DELETE SET NULL
        )
        """
    )
    _ensure_column(cur, "nutrition_slot_meals", "template_id INTEGER NULL")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_slot_meals_day ON nutrition_slot_meals(week_template_day_id)")
    # A day-specific meal title is canonical.  Keep the materialized meal row
    # aligned so exports and older readers cannot resurrect the library title.
    cur.execute(
        """UPDATE nutrition_slot_meals
           SET meal_title=(
               SELECT s.custom_title
               FROM nutrition_week_day_slots s
               WHERE s.week_template_day_id=nutrition_slot_meals.week_template_day_id
                 AND s.slot_index=nutrition_slot_meals.slot_index
           )
           WHERE EXISTS (
               SELECT 1 FROM nutrition_week_day_slots s
               WHERE s.week_template_day_id=nutrition_slot_meals.week_template_day_id
                 AND s.slot_index=nutrition_slot_meals.slot_index
                 AND NULLIF(TRIM(s.custom_title), '') IS NOT NULL
           )"""
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS nutrition_slot_meal_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            slot_meal_id INTEGER NOT NULL,
            amount REAL NULL,
            unit TEXT NULL,
            name_raw TEXT NULL,
            food_id INTEGER NULL,
            item_type TEXT NOT NULL DEFAULT 'food',
            calories REAL NULL,
            display_text TEXT NULL,
            client_req_id TEXT NULL,
            fingerprint TEXT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(slot_meal_id) REFERENCES nutrition_slot_meals(id) ON DELETE CASCADE,
            FOREIGN KEY(food_id) REFERENCES nutrition_foods(id) ON DELETE SET NULL
        )
        """
    )
    _ensure_column(cur, "nutrition_slot_meal_items", "client_req_id TEXT NULL")
    _ensure_column(cur, "nutrition_slot_meal_items", "fingerprint TEXT NULL")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_slot_meal_items_meal ON nutrition_slot_meal_items(slot_meal_id)")
    # Backfill fingerprints and dedupe before adding uniqueness constraints.
    rows = cur.execute(
        "SELECT id, slot_meal_id, amount, unit, name_raw, food_id, item_type, calories, fingerprint FROM nutrition_slot_meal_items"
    ).fetchall()
    if rows:
        food_ids = [row["food_id"] for row in rows if row["food_id"]]
        foods_by_id: Dict[int, Dict[str, Any]] = {}
        if food_ids:
            placeholders = ",".join("?" for _ in food_ids)
            food_rows = cur.execute(
                f"SELECT id, name FROM nutrition_foods WHERE id IN ({placeholders})",
                tuple(food_ids),
            ).fetchall()
            foods_by_id = {row["id"]: dict(row) for row in food_rows}
        for row in rows:
            if row["fingerprint"]:
                continue
            name = row["name_raw"] or (foods_by_id.get(row["food_id"]) or {}).get("name")
            fp = _fingerprint_item(
                name=name,
                amount=row["amount"],
                unit=row["unit"],
                food_id=row["food_id"],
                item_type=row["item_type"] or "food",
                calories=row["calories"],
            )
            cur.execute(
                "UPDATE nutrition_slot_meal_items SET fingerprint=? WHERE id=?",
                (fp, row["id"]),
            )
        # Deduplicate on (slot_meal_id, fingerprint)
        rows = cur.execute(
            "SELECT id, slot_meal_id, fingerprint FROM nutrition_slot_meal_items WHERE fingerprint IS NOT NULL ORDER BY id"
        ).fetchall()
        seen = set()
        for row in rows:
            key = (row["slot_meal_id"], row["fingerprint"])
            if key in seen:
                cur.execute("DELETE FROM nutrition_slot_meal_items WHERE id=?", (row["id"],))
            else:
                seen.add(key)
    # client_req_id is informational only; do not enforce global uniqueness.
    cur.execute("DROP INDEX IF EXISTS ux_slot_items_reqid")
    cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_slot_items_fingerprint ON nutrition_slot_meal_items(slot_meal_id, fingerprint)")

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS nutrition_planned_meal_status (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            log_date TEXT NOT NULL,
            slot_id INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'open',
            shifted_time_text TEXT NULL,
            logged_meal_id INTEGER NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(log_date, slot_id),
            FOREIGN KEY(slot_id) REFERENCES nutrition_week_day_slots(id) ON DELETE CASCADE
        )
        """
    )
    cur.execute("CREATE INDEX IF NOT EXISTS idx_nutrition_planned_meal_status_date ON nutrition_planned_meal_status(log_date)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_nutrition_planned_meal_status_slot ON nutrition_planned_meal_status(slot_id)")
    _ensure_column(cur, "nutrition_planned_meal_status", "status_source TEXT NOT NULL DEFAULT 'user'")
    _ensure_column(cur, "nutrition_planned_meal_status", "reason_code TEXT NULL")
    _ensure_column(cur, "nutrition_planned_meal_status", "trace_json TEXT NULL")

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS nutrition_logged_meals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            log_date TEXT NOT NULL,
            slot_id INTEGER NULL,
            title TEXT NOT NULL,
            meal_slot TEXT NULL,
            source TEXT NOT NULL DEFAULT 'free',
            created_from_planned_slot_id INTEGER NULL,
            created_from_template_id INTEGER NULL,
            edited_from_template INTEGER NOT NULL DEFAULT 0,
            original_planned_time TEXT NULL,
            logged_at TEXT NOT NULL,
            is_favorite INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(slot_id) REFERENCES nutrition_week_day_slots(id) ON DELETE SET NULL
        )
        """
    )
    cur.execute("CREATE INDEX IF NOT EXISTS idx_nutrition_logged_meals_date ON nutrition_logged_meals(log_date, logged_at)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_nutrition_logged_meals_slot ON nutrition_logged_meals(slot_id)")
    _ensure_column(cur, "nutrition_logged_meals", "adjusted_from_meal_id INTEGER NULL")
    _ensure_column(cur, "nutrition_logged_meals", "adjustment_reason TEXT NULL")
    _ensure_column(cur, "nutrition_logged_meals", "action_source TEXT NULL")
    _ensure_column(cur, "nutrition_logged_meals", "trace_json TEXT NULL")

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS nutrition_logged_meal_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            logged_meal_id INTEGER NOT NULL,
            food_id INTEGER NULL,
            food_name TEXT NULL,
            amount REAL NULL,
            unit TEXT NULL,
            item_type TEXT NOT NULL DEFAULT 'food',
            calories REAL NULL,
            sort_index INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(logged_meal_id) REFERENCES nutrition_logged_meals(id) ON DELETE CASCADE,
            FOREIGN KEY(food_id) REFERENCES nutrition_foods(id) ON DELETE SET NULL
        )
        """
    )
    _ensure_column(cur, "nutrition_logged_meal_items", "protein_override REAL NULL")
    _ensure_column(cur, "nutrition_logged_meal_items", "carbs_override REAL NULL")
    _ensure_column(cur, "nutrition_logged_meal_items", "fat_override REAL NULL")
    _ensure_column(cur, "nutrition_logged_meal_items", "sugar_override REAL NULL")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_nutrition_logged_meal_items_meal ON nutrition_logged_meal_items(logged_meal_id, sort_index)")

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS nutrition_logging_recent_foods (
            food_id INTEGER PRIMARY KEY,
            last_amount REAL NULL,
            last_unit TEXT NULL,
            last_meal_slot TEXT NULL,
            last_logged_at TEXT NOT NULL,
            use_count INTEGER NOT NULL DEFAULT 0,
            today_count INTEGER NOT NULL DEFAULT 0,
            today_date TEXT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(food_id) REFERENCES nutrition_foods(id) ON DELETE CASCADE
        )
        """
    )
    cur.execute("CREATE INDEX IF NOT EXISTS idx_nutrition_logging_recent_foods_logged ON nutrition_logging_recent_foods(last_logged_at DESC)")

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS nutrition_logging_recent_meals (
            meal_key TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            meal_slot TEXT NULL,
            payload_json TEXT NOT NULL,
            last_logged_at TEXT NOT NULL,
            use_count INTEGER NOT NULL DEFAULT 0,
            today_count INTEGER NOT NULL DEFAULT 0,
            today_date TEXT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    cur.execute("CREATE INDEX IF NOT EXISTS idx_nutrition_logging_recent_meals_logged ON nutrition_logging_recent_meals(last_logged_at DESC)")

    # no sample data seeding
    _ensure_default_week_slots(cur)
    _ensure_week_slots_for_slot_meals(cur)

    conn.commit()
    conn.close()
    _SCHEMA_READY_FOR.add(schema_key)


def _table_to_dict(row) -> Dict[str, Any]:
    return dict(row) if row else {}


def _macro_settings_default_payload() -> Dict[str, Any]:
    empty_mode = {
        "kcal_target": None,
        "protein_target": None,
        "carbs_target": None,
        "fat_target": None,
        "green_tol": None,
        "yellow_tol": None,
        "green_low": None,
        "green_high": None,
        "yellow_low": None,
        "yellow_high": None,
        "updated_at": None,
    }
    return {
        "active_mode": "maintenance",
        "modes": {
            "cut": dict(empty_mode),
            "lean_bulk": dict(empty_mode),
            "maintenance": dict(empty_mode),
            "custom": dict(empty_mode),
        },
    }


def _week_template_snapshot(cur, week_row: sqlite3.Row) -> Dict[str, Any]:
    settings = _get_week_template_macro_settings(cur, week_row)
    active_mode = settings.get("active_mode") or "maintenance"
    mode_settings = dict(((settings.get("modes") or {}).get(active_mode) or {}))
    day_rows = cur.execute(
        """
        SELECT weekday, target_kcal, target_p, target_c, target_f
        FROM nutrition_week_template_days
        WHERE week_template_id=?
        ORDER BY weekday ASC
        """,
        (int(week_row["id"]),),
    ).fetchall()
    day_targets: Dict[str, Dict[str, Optional[float]]] = {}
    for row in day_rows:
        day_targets[str(int(row["weekday"]))] = {
            "kcal": _clamp_float(row["target_kcal"], 0.0, None),
            "p": _clamp_float(row["target_p"], 0.0, None),
            "c": _clamp_float(row["target_c"], 0.0, None),
            "f": _clamp_float(row["target_f"], 0.0, None),
        }
    return {
        "template_id": int(week_row["id"]),
        "title": week_row["title"],
        "active_mode": active_mode,
        "active_mode_settings": mode_settings,
        "day_targets": day_targets,
    }


def _timeline_date_from_timestamp(raw_value: Any) -> Optional[str]:
    text = str(raw_value or "").strip()
    if not text:
        return None
    return text[:10] if len(text) >= 10 else None


def _earliest_logged_date(cur) -> Optional[str]:
    try:
        row = cur.execute("SELECT MIN(date_iso) AS min_date FROM weight_logs WHERE date_iso IS NOT NULL").fetchone()
    except Exception:
        return None
    if not row:
        return None
    return _timeline_date_from_timestamp(row["min_date"])


def _eligible_week_templates_for_timeline(cur) -> List[sqlite3.Row]:
    return cur.execute(
        """
        SELECT *
        FROM nutrition_week_templates
        WHERE is_active=1 OR last_used_at IS NOT NULL
        ORDER BY created_at ASC, id ASC
        """
    ).fetchall()


def _timeline_rebuild_required(cur) -> bool:
    timeline_rows = cur.execute(
        "SELECT id, week_template_id, start_date, end_date FROM nutrition_week_template_timeline ORDER BY start_date ASC, id ASC"
    ).fetchall()
    if not timeline_rows:
        return True
    templates = _eligible_week_templates_for_timeline(cur)
    if len(timeline_rows) == 1 and len(templates) > 1:
        return True
    # Important safety guard:
    # historical timeline rows may legitimately reference only the plans that were
    # active at the time, while newer templates can exist without historical spans.
    # Rebuilding purely because a newer template is "missing" from the timeline can
    # collapse valid history into a simplified synthetic sequence.
    return False


def _backfill_week_template_timeline(cur) -> None:
    if not _timeline_rebuild_required(cur):
        return
    cur.execute("DELETE FROM nutrition_week_template_timeline")
    rows = _eligible_week_templates_for_timeline(cur)
    if not rows:
        return

    earliest_logged = _earliest_logged_date(cur)
    activation_dates: List[str] = []
    for idx, row in enumerate(rows):
        if idx == 0:
            activation_date = (
                _timeline_date_from_timestamp(row["created_at"])
                or _timeline_date_from_timestamp(row["last_used_at"])
                or _timeline_date_from_timestamp(row["updated_at"])
                or date.today().isoformat()
            )
        else:
            activation_date = (
                _timeline_date_from_timestamp(row["last_used_at"])
                or _timeline_date_from_timestamp(row["created_at"])
                or _timeline_date_from_timestamp(row["updated_at"])
                or date.today().isoformat()
            )
        activation_dates.append(activation_date)

    now = _utcnow_iso()
    for idx, row in enumerate(rows):
        activation_date = activation_dates[idx]
        start_date = activation_date
        if idx == 0 and earliest_logged and earliest_logged < start_date:
            start_date = earliest_logged
        next_date = activation_dates[idx + 1] if idx + 1 < len(activation_dates) else None
        end_date = None
        if next_date:
            next_dt = date.fromisoformat(next_date)
            start_dt = date.fromisoformat(start_date)
            end_date = start_date if next_dt <= start_dt else (next_dt - date.resolution).isoformat()
        cur.execute(
            """
            INSERT INTO nutrition_week_template_timeline
                (week_template_id, start_date, end_date, snapshot_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                int(row["id"]),
                start_date,
                end_date,
                json.dumps(_week_template_snapshot(cur, row), ensure_ascii=False),
                now,
                now,
            ),
        )


def _ensure_week_template_timeline_schema(cur) -> None:
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS nutrition_week_template_timeline (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            week_template_id INTEGER NOT NULL,
            start_date TEXT NOT NULL,
            end_date TEXT NULL,
            snapshot_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(week_template_id) REFERENCES nutrition_week_templates(id) ON DELETE CASCADE
        )
        """
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_week_template_timeline_dates ON nutrition_week_template_timeline(start_date, end_date)"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_week_template_timeline_template ON nutrition_week_template_timeline(week_template_id, start_date)"
    )
    _backfill_week_template_timeline(cur)


def _record_week_template_timeline_snapshot(cur, template_id: int, *, effective_date: Optional[str] = None) -> None:
    _ensure_week_template_timeline_schema(cur)
    week_row = cur.execute("SELECT * FROM nutrition_week_templates WHERE id=?", (int(template_id),)).fetchone()
    if not week_row:
        return
    start_date = (effective_date or _timeline_date_from_timestamp(_utcnow_iso()) or date.today().isoformat()).strip()
    snapshot_json = json.dumps(_week_template_snapshot(cur, week_row), ensure_ascii=False)
    now = _utcnow_iso()

    latest = cur.execute(
        """
        SELECT *
        FROM nutrition_week_template_timeline
        ORDER BY start_date DESC, id DESC
        LIMIT 1
        """
    ).fetchone()

    if latest and int(latest["week_template_id"]) == int(template_id) and latest["start_date"] == start_date:
        cur.execute(
            """
            UPDATE nutrition_week_template_timeline
            SET end_date=NULL, snapshot_json=?, updated_at=?
            WHERE id=?
            """,
            (snapshot_json, now, int(latest["id"])),
        )
        return

    cur.execute(
        "DELETE FROM nutrition_week_template_timeline WHERE start_date=? AND end_date IS NULL",
        (start_date,),
    )

    cur.execute(
        """
        UPDATE nutrition_week_template_timeline
        SET end_date=date(?, '-1 day'), updated_at=?
        WHERE end_date IS NULL
          AND start_date < ?
        """,
        (start_date, now, start_date),
    )

    cur.execute(
        """
        INSERT INTO nutrition_week_template_timeline
            (week_template_id, start_date, end_date, snapshot_json, created_at, updated_at)
        VALUES (?, ?, NULL, ?, ?, ?)
        """,
        (int(template_id), start_date, snapshot_json, now, now),
    )


def _load_global_macro_mode_settings(cur) -> Dict[str, Any]:
    _ensure_nutrition_settings_table(cur)
    rows = cur.execute("SELECT key, value FROM nutrition_settings").fetchall()
    settings = {row["key"]: row["value"] for row in rows}

    def parse_value(key: str) -> Optional[float]:
        raw = settings.get(key)
        if raw in (None, ""):
            return None
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None

    defaults = {"green_tol": 150.0, "yellow_tol": 300.0}
    payload = _macro_settings_default_payload()
    for mode in ("cut", "lean_bulk", "maintenance", "custom"):
        target = parse_value(f"mode_{mode}_target")
        protein = parse_value(f"mode_{mode}_protein_target")
        carbs = parse_value(f"mode_{mode}_carbs_target")
        fat = parse_value(f"mode_{mode}_fat_target")
        green_tol = parse_value(f"mode_{mode}_green_tol")
        yellow_tol = parse_value(f"mode_{mode}_yellow_tol")
        green_low = parse_value(f"mode_{mode}_green_low")
        green_high = parse_value(f"mode_{mode}_green_high")
        yellow_low = parse_value(f"mode_{mode}_yellow_low")
        yellow_high = parse_value(f"mode_{mode}_yellow_high")
        if target is not None:
            if green_low is None and green_tol is not None:
                green_low = target - green_tol
            if green_high is None and green_tol is not None:
                green_high = target + green_tol
            if yellow_low is None and yellow_tol is not None:
                yellow_low = target - yellow_tol
            if yellow_high is None and yellow_tol is not None:
                yellow_high = target + yellow_tol
            if green_tol is None and green_low is not None and green_high is not None:
                green_tol = max(target - green_low, green_high - target)
            if yellow_tol is None and yellow_low is not None and yellow_high is not None:
                yellow_tol = max(target - yellow_low, yellow_high - target)
        payload["modes"][mode] = {
            "kcal_target": target,
            "protein_target": protein,
            "carbs_target": carbs,
            "fat_target": fat,
            "green_tol": green_tol if green_tol is not None else defaults["green_tol"],
            "yellow_tol": yellow_tol if yellow_tol is not None else defaults["yellow_tol"],
            "green_low": green_low,
            "green_high": green_high,
            "yellow_low": yellow_low,
            "yellow_high": yellow_high,
            "updated_at": settings.get(f"mode_{mode}_updated_at"),
        }
    payload["active_mode"] = settings.get("active_mode") or settings.get("selected_mode") or "maintenance"
    return payload


def _normalize_template_macro_settings(raw: Optional[Dict[str, Any]], fallback: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    base = _macro_settings_default_payload()
    if fallback:
        base["active_mode"] = fallback.get("active_mode") or base["active_mode"]
        for mode, values in (fallback.get("modes") or {}).items():
            if mode in base["modes"] and isinstance(values, dict):
                base["modes"][mode].update(values)
    if raw:
        base["active_mode"] = raw.get("active_mode") or base["active_mode"]
        for mode, values in (raw.get("modes") or {}).items():
            if mode in base["modes"] and isinstance(values, dict):
                base["modes"][mode].update(values)
    return base


def _get_week_template_macro_settings(cur, week_row: sqlite3.Row) -> Dict[str, Any]:
    fallback = _load_global_macro_mode_settings(cur)
    raw_json = week_row["macro_modes_json"] if "macro_modes_json" in week_row.keys() else None
    raw = None
    if raw_json:
        try:
            raw = json.loads(raw_json)
        except Exception:
            raw = None
    if raw is None:
        raw = {}
    if week_row["macro_active_mode"] if "macro_active_mode" in week_row.keys() else None:
        raw["active_mode"] = week_row["macro_active_mode"]
    return _normalize_template_macro_settings(raw, fallback)


def _active_targets_from_macro_settings(settings: Dict[str, Any]) -> Dict[str, Optional[float]]:
    active_mode = settings.get("active_mode") or "maintenance"
    mode_settings = (settings.get("modes") or {}).get(active_mode) or {}
    return {
        "mode": active_mode,
        "kcal": _clamp_float(mode_settings.get("kcal_target"), 0.0, None),
        "p": _clamp_float(mode_settings.get("protein_target"), 0.0, None),
        "c": _clamp_float(mode_settings.get("carbs_target"), 0.0, None),
        "f": _clamp_float(mode_settings.get("fat_target"), 0.0, None),
    }


def _validate_macro_settings_payload(payload: Dict[str, Any]) -> Optional[str]:
    allowed = {"cut", "lean_bulk", "maintenance", "custom"}
    active_mode = payload.get("active_mode")
    if active_mode is not None and active_mode not in allowed:
        return "invalid_mode"
    for mode, values in (payload.get("modes") or {}).items():
        if mode not in allowed or not isinstance(values, dict):
            return "invalid_mode"
        kcal_target = _clamp_float(values.get("kcal_target"), 0.0, None)
        protein_target = _clamp_float(values.get("protein_target"), 0.0, None)
        carbs_target = _clamp_float(values.get("carbs_target"), 0.0, None)
        fat_target = _clamp_float(values.get("fat_target"), 0.0, None)
        green_tol = _clamp_float(values.get("green_tol"), 0.0, None)
        yellow_tol = _clamp_float(values.get("yellow_tol"), 0.0, None)
        green_low = _clamp_float(values.get("green_low"), 0.0, None)
        green_high = _clamp_float(values.get("green_high"), 0.0, None)
        yellow_low = _clamp_float(values.get("yellow_low"), 0.0, None)
        yellow_high = _clamp_float(values.get("yellow_high"), 0.0, None)
        if kcal_target is None or kcal_target <= 0:
            return "invalid_target"
        for val in (protein_target, carbs_target, fat_target):
            if val is not None and val < 0:
                return "invalid_target"
        uses_bounds = any(v is not None for v in (green_low, green_high, yellow_low, yellow_high))
        if uses_bounds:
            if None in (green_low, green_high, yellow_low, yellow_high):
                return "invalid_bounds"
            if not (green_low <= kcal_target <= green_high):
                return "invalid_bounds"
            if not (yellow_low <= kcal_target <= yellow_high):
                return "invalid_bounds"
            if not (yellow_low <= green_low and green_high <= yellow_high):
                return "invalid_bounds"
        else:
            if green_tol is None or yellow_tol is None or yellow_tol < green_tol:
                return "invalid_bounds"
    return None


def _compute_ingredient_macros(rows: Sequence[sqlite3.Row]) -> MacroTotals:
    total = MacroTotals(kcal=0.0, p=0.0, c=0.0, f=0.0)
    for row in rows:
        row_data = dict(row)
        factor = _food_macro_factor(
            amount=row_data.get("amount"),
            unit=row_data.get("unit"),
            food_unit_default=row_data.get("unit_default"),
            common_portion=row_data.get("common_portion_size"),
            portion_g=row_data.get("portion_g"),
        )
        macros = MacroTotals(
            kcal=row_data.get("kcal_per_100") or 0.0,
            p=row_data.get("p_per_100") or 0.0,
            c=row_data.get("c_per_100") or 0.0,
            f=row_data.get("f_per_100") or 0.0,
            sugar=row_data.get("sugar_per_100") or 0.0,
            salt=row_data.get("salt_per_100") or 0.0,
        ).scaled(factor)
        total = total.add(macros)
    return total


def _resolve_grams(amount: float, unit: str, common_portion: Optional[float]) -> float:
    amt = _clamp_float(amount, 0.0, None) or 0.0
    unit = (unit or "g").lower()
    if unit in ("g", "gram", "grams", "ml"):
        return amt
    if unit in ("pcs", "stück", "piece", "pieces"):
        if common_portion:
            return amt * common_portion
        return amt
    return amt


def _food_macro_factor(
    amount: float,
    unit: str,
    food_unit_default: Optional[str],
    common_portion: Optional[float],
    portion_g: Optional[float],
) -> float:
    normalized_food_unit = (food_unit_default or "g").lower()
    normalized_unit = (unit or "g").lower()
    amt = _clamp_float(amount, 0.0, None) or 0.0
    if normalized_food_unit in ("pcs", "piece", "pieces", "stück"):
        common = _clamp_float(common_portion, 0.0, None) or 0.0
        portion_weight = _clamp_float(portion_g, 0.0, None) or 0.0
        # ``1`` is a legacy placeholder, not a useful gram equivalent for a
        # single piece when the food row contains a real portion weight.
        portion = common if common > 1 else portion_weight
        if normalized_unit in ("pcs", "piece", "pieces", "stück"):
            # Pcs foods without a gram equivalent are legacy per-piece rows:
            # their stored macro values describe one piece. Returning zero here
            # silently dropped the complete food from logging totals.
            return (amt * portion) / 100.0 if portion and portion > 0 else amt
        if normalized_unit in ("g", "gram", "grams", "ml") and portion and portion > 0:
            return amt / portion
        # If a pcs-based food was logged in grams/ml without a known portion size,
        # converting "grams" into "pieces" would explode the macros. Treat it as
        # unresolved instead of assuming 1g == 1 piece.
        if normalized_unit in ("g", "gram", "grams", "ml"):
            return 0.0
        return amt
    grams = _resolve_grams(amt, normalized_unit, common_portion)
    return grams / 100.0


def _format_amount(amount: float, unit: str) -> str:
    unit = (unit or "g").lower()
    if unit in ("g", "gram", "grams"):
        return f"{int(amount)}g"
    if unit == "ml":
        return f"{int(amount)}ml"
    if unit in ("pcs", "piece", "pieces", "stück"):
        return f"{float(amount):.1f}x"
    return f"{amount} {unit}"


def _format_amount_human(amount: float, unit: str) -> str:
    unit = (unit or "g").lower()
    if unit in ("g", "gram", "grams", "ml"):
        return f"{int(amount)} {unit}"
    if unit in ("pcs", "piece", "pieces", "stück"):
        return f"{int(amount)} pcs"
    return f"{amount} {unit}"


def _display_amount_struct(amount: Any, unit: Any) -> Dict[str, Any]:
    normalized_unit = _normalize_unit_import(unit or "g")
    normalized_amount = _clamp_float(amount, 0.0, None)
    amount_g = normalized_amount if normalized_unit == "g" else None
    amount_ml = normalized_amount if normalized_unit == "ml" else None
    amount_pcs = normalized_amount if normalized_unit == "pcs" else None
    if normalized_amount is None:
        display_amount = ""
    elif normalized_unit == "pcs":
        display_amount = f"{int(normalized_amount) if float(normalized_amount).is_integer() else f'{normalized_amount:g}'}×"
    elif float(normalized_amount).is_integer():
        display_amount = f"{int(normalized_amount)} {_format_amount_human(1, normalized_unit).split(' ', 1)[1]}"
    else:
        display_amount = f"{normalized_amount:g} {_format_amount_human(1, normalized_unit).split(' ', 1)[1]}"
    return {
        "amount": normalized_amount,
        "unit": normalized_unit,
        "amount_g": amount_g,
        "amount_ml": amount_ml,
        "amount_pcs": amount_pcs,
        "display_amount": display_amount.strip(),
    }


def _build_export_text(rows: Sequence[sqlite3.Row]) -> str:
    tokens = []
    for row in rows:
        name = row["food_name"] or ""
        amount = row["amount"] or 0
        unit = row["unit"] or "g"
        tokens.append(f"{_format_amount(amount, unit)} {name}".strip())
    return ", ".join(tokens)


def _compute_override_macros(ingredients: List[Dict[str, Any]], foods_by_id: Dict[int, Dict[str, Any]]) -> MacroTotals:
    total = MacroTotals(kcal=0.0, p=0.0, c=0.0, f=0.0)
    for item in ingredients:
        if item.get("kcal_value") is not None or (item.get("unit") == "kcal"):
            total = total.add(MacroTotals(kcal=float(item.get("kcal_value") or item.get("amount") or 0.0), p=0.0, c=0.0, f=0.0))
            continue
        food = foods_by_id.get(int(item.get("food_id") or 0))
        if not food:
            continue
        factor = _food_macro_factor(
            amount=item.get("amount") or 0,
            unit=item.get("unit") or "g",
            food_unit_default=food.get("unit_default"),
            common_portion=food.get("common_portion_size"),
            portion_g=food.get("portion_g"),
        )
        macros = MacroTotals(
            kcal=food.get("kcal_per_100") or 0.0,
            p=food.get("p_per_100") or 0.0,
            c=food.get("c_per_100") or 0.0,
            f=food.get("f_per_100") or 0.0,
            sugar=food.get("sugar_per_100") or 0.0,
            salt=food.get("salt_per_100") or 0.0,
        ).scaled(factor)
        total = total.add(macros)
    return total


def _build_override_export_text(ingredients: List[Dict[str, Any]], foods_by_id: Dict[int, Dict[str, Any]]) -> str:
    tokens = []
    for item in ingredients:
        food = foods_by_id.get(int(item.get("food_id") or 0))
        if not food:
            continue
        amount = item.get("amount") or 0
        unit = item.get("unit") or "g"
        tokens.append(f"{_format_amount(amount, unit)} {food.get('name')}".strip())
    return ", ".join(tokens)


def _refresh_meal_template_calcs(cur, meal_template_id: int) -> None:
    rows = cur.execute(
        """
        SELECT mi.amount, mi.unit, f.name AS food_name,
               f.kcal_per_100, f.p_per_100, f.c_per_100, f.f_per_100,
               f.sugar_per_100, f.salt_per_100, f.common_portion_size, f.unit_default, f.portion_g
        FROM nutrition_meal_ingredients mi
        JOIN nutrition_foods f ON f.id=mi.food_id
        WHERE mi.meal_template_id=?
        ORDER BY mi.sort_index, mi.id
        """,
        (meal_template_id,),
    ).fetchall()
    macros = _compute_ingredient_macros(rows)
    export_text = _build_export_text(rows)
    now = _utcnow_iso()
    cur.execute(
        """
        UPDATE nutrition_meal_templates
        SET kcal_per_serving=?, p_per_serving=?, c_per_serving=?, f_per_serving=?, export_text=?, updated_at=?
        WHERE id=?
        """,
        (macros.kcal, macros.p, macros.c, macros.f, export_text, now, meal_template_id),
    )



def _ensure_default_slot_templates(cur) -> List[Dict[str, Any]]:
    existing = [dict(row) for row in cur.execute("SELECT * FROM nutrition_slot_templates").fetchall()]
    by_title = {row["title"]: row for row in existing}
    out: List[Dict[str, Any]] = []
    now = _utcnow_iso()
    for slot in SLOT_TEMPLATES:
        row = by_title.get(slot["title"])
        if not row:
            cur.execute(
                """
                INSERT INTO nutrition_slot_templates (title, default_time, context_tag, macro_intent, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (slot["title"], slot.get("default_time"), slot.get("context_tag"), slot.get("macro_intent"), now, now),
            )
            row = dict(cur.execute("SELECT * FROM nutrition_slot_templates WHERE id=?", (cur.lastrowid,)).fetchone())
        else:
            if (
                row.get("default_time") != slot.get("default_time")
                or row.get("context_tag") != slot.get("context_tag")
                or row.get("macro_intent") != slot.get("macro_intent")
            ):
                cur.execute(
                    """
                    UPDATE nutrition_slot_templates
                    SET default_time=?, context_tag=?, macro_intent=?, updated_at=?
                    WHERE id=?
                    """,
                    (slot.get("default_time"), slot.get("context_tag"), slot.get("macro_intent"), now, row["id"]),
                )
                row = dict(cur.execute("SELECT * FROM nutrition_slot_templates WHERE id=?", (row["id"],)).fetchone())
        out.append(row)
    return out


def _ensure_default_week_slots(cur) -> None:
    default_templates = _ensure_default_slot_templates(cur)
    week = cur.execute(
        "SELECT * FROM nutrition_week_templates WHERE is_active=1 ORDER BY updated_at DESC, id DESC LIMIT 1"
    ).fetchone()
    if not week:
        return
    existing_slot_count = cur.execute(
        """
        SELECT COUNT(*) AS value
        FROM nutrition_week_day_slots s
        JOIN nutrition_week_template_days d ON d.id=s.week_template_day_id
        WHERE d.week_template_id=?
        """,
        (week["id"],),
    ).fetchone()["value"]
    # Legacy bootstrapping is only valid for a completely empty week. Never
    # refill a structured plan to four slots: three-meal/rest-day plans are
    # intentional, and synthetic placeholders corrupt canonical ordering.
    if int(existing_slot_count or 0) > 0:
        return
    day_rows = cur.execute(
        "SELECT * FROM nutrition_week_template_days WHERE week_template_id=? ORDER BY weekday",
        (week["id"],),
    ).fetchall()
    now = _utcnow_iso()
    for day in day_rows:
        slots = cur.execute(
            "SELECT id, slot_template_id, slot_index FROM nutrition_week_day_slots WHERE week_template_day_id=? ORDER BY slot_index",
            (day["id"],),
        ).fetchall()
        slots_by_index = {row["slot_index"]: row for row in slots}
        for idx, slot_template in enumerate(default_templates[:DEFAULT_BASE_SLOT_COUNT]):
            if idx in slots_by_index:
                continue
            cur.execute(
                """
                INSERT INTO nutrition_week_day_slots
                    (week_template_day_id, slot_template_id, slot_index, active_option_id, locked, time_text, custom_title, note_text, meal_template_id, created_at, updated_at)
                VALUES (?, ?, ?, NULL, 0, NULL, NULL, NULL, NULL, ?, ?)
                """,
                (day["id"], slot_template["id"], idx, now, now),
            )


def _ensure_week_slots_for_slot_meals(cur) -> None:
    week = cur.execute(
        "SELECT * FROM nutrition_week_templates WHERE is_active=1 ORDER BY updated_at DESC, id DESC LIMIT 1"
    ).fetchone()
    if not week:
        return
    day_rows = cur.execute(
        "SELECT * FROM nutrition_week_template_days WHERE week_template_id=? ORDER BY weekday",
        (week["id"],),
    ).fetchall()
    now = _utcnow_iso()
    for day in day_rows:
        max_slot = cur.execute(
            "SELECT COALESCE(MAX(slot_index), -1) AS mx FROM nutrition_slot_meals WHERE week_template_day_id=?",
            (day["id"],),
        ).fetchone()["mx"]
        if max_slot is None or max_slot < 0:
            continue
        existing_slots = cur.execute(
            "SELECT slot_index FROM nutrition_week_day_slots WHERE week_template_day_id=? ORDER BY slot_index",
            (day["id"],),
        ).fetchall()
        existing_idx = {row["slot_index"] for row in existing_slots}
        for slot_index in range(0, max_slot + 1):
            if slot_index in existing_idx:
                continue
            title = f"Meal {slot_index + 1}"
            slot_template_id = _get_or_create_slot_template_id(cur, title)
            cur.execute(
                """
                INSERT INTO nutrition_week_day_slots
                    (week_template_day_id, slot_template_id, slot_index, active_option_id, locked, time_text, custom_title, note_text, meal_template_id, created_at, updated_at)
                VALUES (?, ?, ?, NULL, 0, NULL, NULL, NULL, NULL, ?, ?)
                """,
                (day["id"], slot_template_id, slot_index, now, now),
            )

# === Foods ===

def list_foods(
    query: Optional[str] = None,
    limit: int = 300,
    favorites_only: bool = False,
    *,
    initialize_schema: bool = True,
) -> List[Dict[str, Any]]:
    if initialize_schema:
        ensure_nutrition_planning_schema()
    conn = get_nutrition_db()
    cur = conn.cursor()
    q = (query or "").strip().lower()
    params: List[Any] = []
    sql = "SELECT * FROM nutrition_foods"
    clauses: List[str] = []
    if q:
        clauses.append("LOWER(name) LIKE ?")
        params.append(f"%{q}%")
    if favorites_only:
        clauses.append("is_favorite=1")
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY is_favorite DESC, name ASC LIMIT ?"
    params.append(max(1, min(500, limit)))
    rows = cur.execute(sql, tuple(params)).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def create_food(payload: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    ensure_nutrition_planning_schema()
    name = (payload.get("name") or "").strip()
    if not name:
        return None, "name_required"
    name_norm = _normalize_item_name(name)
    kcal = _clamp_float(payload.get("kcal_per_100"), 0.0, None)
    p = _clamp_float(payload.get("p_per_100"), 0.0, None)
    c = _clamp_float(payload.get("c_per_100"), 0.0, None)
    f = _clamp_float(payload.get("f_per_100"), 0.0, None)
    if None in (kcal, p, c, f):
        return None, "macros_required"
    now = _utcnow_iso()
    conn = get_nutrition_db()
    cur = conn.cursor()
    existing = cur.execute(
        "SELECT * FROM nutrition_foods WHERE name_normalized=? LIMIT 1",
        (name_norm,),
    ).fetchone()
    if existing:
        conn.close()
        return None, "duplicate_name"
    existing_columns = {str(row["name"]) for row in cur.execute("PRAGMA table_info(nutrition_foods)").fetchall()}
    insert_payload = {
        "name": name,
        "brand": payload.get("brand"),
        "unit_default": payload.get("unit_default") or "g",
        "portion_g": _clamp_float(payload.get("portion_g"), 0.0, None),
        "common_portion_size": _clamp_float(payload.get("common_portion_size"), 0.0, None),
        "mfp_search_hint": payload.get("mfp_search_hint"),
        "category": payload.get("category"),
        "kcal_per_100": kcal,
        "p_per_100": p,
        "c_per_100": c,
        "f_per_100": f,
        "sugar_per_100": _clamp_float(payload.get("sugar_per_100"), 0.0, None),
        "salt_per_100": _clamp_float(payload.get("salt_per_100"), 0.0, None),
        "tags": payload.get("tags"),
        "is_favorite": _as_int_bool(payload.get("is_favorite")),
        "name_normalized": name_norm,
        "created_at": now,
        "updated_at": now,
    }
    insert_columns = [column for column in insert_payload if column in existing_columns]
    cur.execute(
        f"INSERT INTO nutrition_foods ({', '.join(insert_columns)}) VALUES ({', '.join('?' for _ in insert_columns)})",
        tuple(insert_payload[column] for column in insert_columns),
    )
    food_id = cur.lastrowid
    conn.commit()
    row = cur.execute("SELECT * FROM nutrition_foods WHERE id=?", (food_id,)).fetchone()
    conn.close()
    return (dict(row) if row else None), None


def update_food(food_id: int, payload: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    ensure_nutrition_planning_schema()
    conn = get_nutrition_db()
    cur = conn.cursor()
    row = cur.execute("SELECT * FROM nutrition_foods WHERE id=?", (food_id,)).fetchone()
    if not row:
        conn.close()
        return None, "not_found"
    name = (payload.get("name") or row["name"] or "").strip()
    if not name:
        conn.close()
        return None, "name_required"
    name_norm = _normalize_item_name(name)
    existing = cur.execute(
        "SELECT id FROM nutrition_foods WHERE name_normalized=? AND id!=? LIMIT 1",
        (name_norm, food_id),
    ).fetchone()
    if existing:
        conn.close()
        return None, "duplicate_name"
    updated = {
        "name": name,
        "brand": payload.get("brand") if "brand" in payload else row["brand"],
        "unit_default": row["unit_default"],
        "portion_g": payload.get("portion_g") if "portion_g" in payload else row["portion_g"],
        "common_portion_size": payload.get("common_portion_size") if "common_portion_size" in payload else row["common_portion_size"],
        "mfp_search_hint": payload.get("mfp_search_hint") if "mfp_search_hint" in payload else row["mfp_search_hint"],
        "category": payload.get("category") if "category" in payload else row["category"],
        "kcal_per_100": _clamp_float(payload.get("kcal_per_100"), 0.0, None) if "kcal_per_100" in payload else row["kcal_per_100"],
        "p_per_100": _clamp_float(payload.get("p_per_100"), 0.0, None) if "p_per_100" in payload else row["p_per_100"],
        "c_per_100": _clamp_float(payload.get("c_per_100"), 0.0, None) if "c_per_100" in payload else row["c_per_100"],
        "f_per_100": _clamp_float(payload.get("f_per_100"), 0.0, None) if "f_per_100" in payload else row["f_per_100"],
        "sugar_per_100": _clamp_float(payload.get("sugar_per_100"), 0.0, None) if "sugar_per_100" in payload else row["sugar_per_100"],
        "salt_per_100": _clamp_float(payload.get("salt_per_100"), 0.0, None) if "salt_per_100" in payload else row["salt_per_100"],
        "tags": payload.get("tags") if "tags" in payload else row["tags"],
        "is_favorite": _as_int_bool(payload.get("is_favorite")) if "is_favorite" in payload else row["is_favorite"],
    }
    if None in (updated["kcal_per_100"], updated["p_per_100"], updated["c_per_100"], updated["f_per_100"]):
        conn.close()
        return None, "macros_required"
    existing_columns = {str(info["name"]) for info in cur.execute("PRAGMA table_info(nutrition_foods)").fetchall()}
    update_payload = {
        "name": updated["name"],
        "brand": updated["brand"],
        "unit_default": updated["unit_default"],
        "portion_g": updated["portion_g"],
        "common_portion_size": updated["common_portion_size"],
        "mfp_search_hint": updated["mfp_search_hint"],
        "category": updated["category"],
        "kcal_per_100": updated["kcal_per_100"],
        "p_per_100": updated["p_per_100"],
        "c_per_100": updated["c_per_100"],
        "f_per_100": updated["f_per_100"],
        "sugar_per_100": updated["sugar_per_100"],
        "salt_per_100": updated["salt_per_100"],
        "tags": updated["tags"],
        "is_favorite": updated["is_favorite"],
        "name_normalized": name_norm,
        "updated_at": _utcnow_iso(),
    }
    update_columns = [column for column in update_payload if column in existing_columns]
    cur.execute(
        f"UPDATE nutrition_foods SET {', '.join(f'{column}=?' for column in update_columns)} WHERE id=?",
        tuple(update_payload[column] for column in update_columns) + (food_id,),
    )
    conn.commit()
    row = cur.execute("SELECT * FROM nutrition_foods WHERE id=?", (food_id,)).fetchone()
    conn.close()
    return (dict(row) if row else None), None


def delete_food(food_id: int, force: bool = False) -> Tuple[bool, Optional[Dict[str, Any]]]:
    ensure_nutrition_planning_schema()
    conn = get_nutrition_db()
    cur = conn.cursor()
    if not force:
        in_use = cur.execute(
            "SELECT 1 FROM nutrition_meal_ingredients WHERE food_id=? LIMIT 1",
            (food_id,),
        ).fetchone()
        if in_use:
            conn.close()
            return False, {"error": "food_in_use"}
    cur.execute("DELETE FROM nutrition_foods WHERE id=?", (food_id,))
    conn.commit()
    conn.close()
    return True, None


# === Meals ===

def list_meal_templates(
    query: Optional[str] = None,
    limit: int = 200,
    *,
    initialize_schema: bool = True,
) -> List[Dict[str, Any]]:
    if initialize_schema:
        ensure_nutrition_planning_schema()
    conn = get_nutrition_db()
    cur = conn.cursor()
    q = (query or "").strip().lower()
    params: List[Any] = []
    sql = "SELECT * FROM nutrition_meal_templates"
    if q:
        sql += " WHERE LOWER(title) LIKE ?"
        params.append(f"%{q}%")
    sql += " ORDER BY title ASC LIMIT ?"
    params.append(max(1, min(500, limit)))
    rows = cur.execute(sql, tuple(params)).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def resolve_meal_template(template_id: int) -> Optional[Dict[str, Any]]:
    ensure_nutrition_planning_schema()
    conn = get_nutrition_db()
    cur = conn.cursor()
    row = cur.execute("SELECT * FROM nutrition_meal_templates WHERE id=?", (template_id,)).fetchone()
    if not row:
        conn.close()
        return None
    ingredients = cur.execute(
        """
        SELECT mi.id, mi.amount, mi.unit, mi.sort_index,
               f.id AS food_id, f.name AS food_name, f.unit_default, f.common_portion_size, f.kcal_per_100, f.p_per_100, f.c_per_100, f.f_per_100,
               f.sugar_per_100, f.salt_per_100, f.mfp_search_hint, f.category
        FROM nutrition_meal_ingredients mi
        JOIN nutrition_foods f ON f.id=mi.food_id
        WHERE mi.meal_template_id=?
        ORDER BY mi.sort_index, mi.id
        """,
        (template_id,),
    ).fetchall()
    ingredients_data = []
    for ing in ingredients:
            ingredients_data.append(dict(ing))
    conn.close()
    data = dict(row)
    data["ingredients"] = ingredients_data
    return data


# Additional helper functions (create/update meal templates, slot templates, etc.)
def _insert_meal_ingredient(cur, meal_template_id: int, ingredient: Dict[str, Any], sort_index: int) -> bool:
    food_id = ingredient.get("food_id")
    amount = _clamp_float(ingredient.get("amount"), 0.0, None)
    if not food_id or amount is None:
        return False
    unit = (ingredient.get("unit") or "g").strip()
    now = _utcnow_iso()
    cur.execute(
        """
        INSERT INTO nutrition_meal_ingredients (meal_template_id, food_id, amount, unit, sort_index, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (meal_template_id, int(food_id), amount, unit, sort_index, now, now),
    )
    return True


def create_meal_template(payload: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    ensure_nutrition_planning_schema()
    title = (payload.get("title") or "").strip()
    if not title:
        return None, "title_required"
    ingredients = payload.get("ingredients") or []
    signature = _build_meal_signature(title, ingredients)
    now = _utcnow_iso()
    conn = get_nutrition_db()
    cur = conn.cursor()
    existing = cur.execute(
        "SELECT * FROM nutrition_meal_templates WHERE signature=? LIMIT 1",
        (signature,),
    ).fetchone()
    if existing:
        conn.close()
        return dict(existing), None
    cur.execute(
        """
        INSERT INTO nutrition_meal_templates (title, description, tags, default_servings, mfp_alias, notes, signature, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            title,
            payload.get("description"),
            payload.get("tags"),
            _clamp_float(payload.get("default_servings"), 0.1, None) or 1.0,
            payload.get("mfp_alias"),
            payload.get("notes"),
            signature,
            now,
            now,
        ),
    )
    meal_id = cur.lastrowid
    for idx, ingredient in enumerate(ingredients):
        _insert_meal_ingredient(cur, meal_id, ingredient, idx)
    _refresh_meal_template_calcs(cur, meal_id)
    conn.commit()
    row = cur.execute("SELECT * FROM nutrition_meal_templates WHERE id=?", (meal_id,)).fetchone()
    conn.close()
    return (dict(row) if row else None), None


def update_meal_template(template_id: int, payload: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    ensure_nutrition_planning_schema()
    conn = get_nutrition_db()
    cur = conn.cursor()
    row = cur.execute("SELECT * FROM nutrition_meal_templates WHERE id=?", (template_id,)).fetchone()
    if not row:
        conn.close()
        return None, "not_found"
    title = (payload.get("title") or row["title"] or "").strip()
    if not title:
        conn.close()
        return None, "title_required"
    if "ingredients" in payload:
        ingredients_for_sig = payload.get("ingredients") or []
    else:
        rows = cur.execute(
            """
            SELECT food_id, amount, unit
            FROM nutrition_meal_ingredients
            WHERE meal_template_id=?
            ORDER BY sort_index, id
            """,
            (template_id,),
        ).fetchall()
        ingredients_for_sig = [dict(r) for r in rows]
    signature = _build_meal_signature(title, ingredients_for_sig)
    existing = cur.execute(
        "SELECT id FROM nutrition_meal_templates WHERE signature=? AND id!=? LIMIT 1",
        (signature, template_id),
    ).fetchone()
    if existing:
        conn.close()
        return None, "duplicate_signature"
    now = _utcnow_iso()
    cur.execute(
        """
        UPDATE nutrition_meal_templates
        SET title=?, description=?, tags=?, default_servings=?, mfp_alias=?, notes=?, signature=?, updated_at=?
        WHERE id=?
        """,
        (
            title,
            payload.get("description") if "description" in payload else row["description"],
            payload.get("tags") if "tags" in payload else row["tags"],
            _clamp_float(payload.get("default_servings"), 0.1, None) if "default_servings" in payload else row["default_servings"],
            payload.get("mfp_alias") if "mfp_alias" in payload else row["mfp_alias"],
            payload.get("notes") if "notes" in payload else row["notes"],
            signature,
            now,
            template_id,
        ),
    )
    if "ingredients" in payload:
        cur.execute("DELETE FROM nutrition_meal_ingredients WHERE meal_template_id=?", (template_id,))
        for idx, ingredient in enumerate(payload.get("ingredients") or []):
            _insert_meal_ingredient(cur, template_id, ingredient, idx)
    _refresh_meal_template_calcs(cur, template_id)
    conn.commit()
    updated = cur.execute("SELECT * FROM nutrition_meal_templates WHERE id=?", (template_id,)).fetchone()
    conn.close()
    return (dict(updated) if updated else None), None


def delete_meal_template(template_id: int) -> Tuple[bool, Optional[str]]:
    ensure_nutrition_planning_schema()
    conn = get_nutrition_db()
    cur = conn.cursor()
    row = cur.execute("SELECT id FROM nutrition_meal_templates WHERE id=?", (template_id,)).fetchone()
    if not row:
        conn.close()
        return False, "not_found"
    cur.execute("DELETE FROM nutrition_meal_templates WHERE id=?", (template_id,))
    conn.commit()
    conn.close()
    return True, None


def delete_all_meal_templates() -> Dict[str, Any]:
    ensure_nutrition_planning_schema()
    conn = get_nutrition_db()
    cur = conn.cursor()
    now = _utcnow_iso()
    cur.execute("BEGIN")
    try:
        count_row = cur.execute("SELECT COUNT(*) AS cnt FROM nutrition_meal_templates").fetchone()
        total = count_row["cnt"] if count_row else 0
        cur.execute("DELETE FROM nutrition_slot_meal_overrides")
        cur.execute("UPDATE nutrition_week_day_slots SET meal_template_id=NULL, updated_at=?", (now,))
        cur.execute("DELETE FROM nutrition_meal_templates")
        conn.commit()
        conn.close()
        return {"ok": True, "deleted": total}
    except Exception as exc:
        conn.rollback()
        conn.close()
        return {"ok": False, "error": str(exc)}


def add_meal_item(template_id: int, payload: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    ensure_nutrition_planning_schema()
    conn = get_nutrition_db()
    cur = conn.cursor()
    if not cur.execute("SELECT id FROM nutrition_meal_templates WHERE id=?", (template_id,)).fetchone():
        conn.close()
        return None, "not_found"
    if not payload.get("food_id"):
        conn.close()
        return None, "food_id_required"
    amount = _clamp_float(payload.get("amount"), 0.0, None)
    if amount is None:
        conn.close()
        return None, "amount_required"
    max_index = cur.execute(
        "SELECT COALESCE(MAX(sort_index), -1) FROM nutrition_meal_ingredients WHERE meal_template_id=?", (template_id,)
    ).fetchone()[0]
    idx = max_index + 1
    _insert_meal_ingredient(cur, template_id, payload, idx)
    _refresh_meal_template_calcs(cur, template_id)
    conn.commit()
    item_id = cur.lastrowid
    row = cur.execute("SELECT * FROM nutrition_meal_ingredients WHERE id=?", (item_id,)).fetchone()
    conn.close()
    return (dict(row) if row else None), None


def update_meal_item(item_id: int, payload: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    ensure_nutrition_planning_schema()
    conn = get_nutrition_db()
    cur = conn.cursor()
    row = cur.execute("SELECT * FROM nutrition_meal_ingredients WHERE id=?", (item_id,)).fetchone()
    if not row:
        conn.close()
        return None, "not_found"
    amount = _clamp_float(payload.get("amount"), 0.0, None) if "amount" in payload else row["amount"]
    if amount is None:
        conn.close()
        return None, "amount_required"
    unit = payload.get("unit") if "unit" in payload else row["unit"]
    sort_index = payload.get("sort_index") if "sort_index" in payload else row["sort_index"]
    cur.execute(
        """
        UPDATE nutrition_meal_ingredients
        SET amount=?, unit=?, sort_index=?, updated_at=?
        WHERE id=?
        """,
        (amount, unit or row["unit"], sort_index, _utcnow_iso(), item_id),
    )
    _refresh_meal_template_calcs(cur, row["meal_template_id"])
    conn.commit()
    updated = cur.execute("SELECT * FROM nutrition_meal_ingredients WHERE id=?", (item_id,)).fetchone()
    conn.close()
    return (dict(updated) if updated else None), None


def delete_meal_item(item_id: int) -> None:
    ensure_nutrition_planning_schema()
    conn = get_nutrition_db()
    cur = conn.cursor()
    row = cur.execute("SELECT * FROM nutrition_meal_ingredients WHERE id=?", (item_id,)).fetchone()
    if row:
        cur.execute("DELETE FROM nutrition_meal_ingredients WHERE id=?", (item_id,))
        _refresh_meal_template_calcs(cur, row["meal_template_id"])
    conn.commit()
    conn.close()


# === Slot Templates ===


def list_slot_templates(*, initialize_schema: bool = True) -> List[Dict[str, Any]]:
    if initialize_schema:
        ensure_nutrition_planning_schema()
    conn = get_nutrition_db()
    cur = conn.cursor()
    rows = cur.execute("SELECT * FROM nutrition_slot_templates ORDER BY id").fetchall()
    result = []
    for row in rows:
        data = dict(row)
        data["options_count"] = 0
        result.append(data)
    conn.close()
    return result


def create_slot_template(payload: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    ensure_nutrition_planning_schema()
    title = (payload.get("title") or "").strip()
    if not title:
        return None, "title_required"
    now = _utcnow_iso()
    conn = get_nutrition_db()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO nutrition_slot_templates (title, default_time, context_tag, macro_intent, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            title,
            payload.get("default_time"),
            payload.get("context_tag"),
            payload.get("macro_intent"),
            now,
            now,
        ),
    )
    slot_id = cur.lastrowid
    conn.commit()
    row = cur.execute("SELECT * FROM nutrition_slot_templates WHERE id=?", (slot_id,)).fetchone()
    conn.close()
    return (dict(row) if row else None), None


def update_slot_template(slot_id: int, payload: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    ensure_nutrition_planning_schema()
    conn = get_nutrition_db()
    cur = conn.cursor()
    row = cur.execute("SELECT * FROM nutrition_slot_templates WHERE id=?", (slot_id,)).fetchone()
    if not row:
        conn.close()
        return None, "not_found"
    title = (payload.get("title") or row["title"] or "").strip()
    if not title:
        conn.close()
        return None, "title_required"
    cur.execute(
        """
        UPDATE nutrition_slot_templates
        SET title=?, default_time=?, context_tag=?, macro_intent=?, updated_at=?
        WHERE id=?
        """,
        (
            title,
            payload.get("default_time") if "default_time" in payload else row["default_time"],
            payload.get("context_tag") if "context_tag" in payload else row["context_tag"],
            payload.get("macro_intent") if "macro_intent" in payload else row["macro_intent"],
            _utcnow_iso(),
            slot_id,
        ),
    )
    conn.commit()
    updated = cur.execute("SELECT * FROM nutrition_slot_templates WHERE id=?", (slot_id,)).fetchone()
    conn.close()
    return (dict(updated) if updated else None), None


# === Slot Options ===


def list_slot_options(slot_template_id: int) -> List[Dict[str, Any]]:
    ensure_nutrition_planning_schema()
    conn = get_nutrition_db()
    cur = conn.cursor()
    rows = cur.execute(
        """
        SELECT so.*, mt.title AS meal_title, mt.mfp_alias,
               mt.kcal_per_serving, mt.p_per_serving, mt.c_per_serving, mt.f_per_serving, mt.export_text, mt.tags
        FROM nutrition_slot_options so
        JOIN nutrition_meal_templates mt ON mt.id=so.meal_template_id
        WHERE so.slot_template_id=?
        ORDER BY so.priority DESC, so.weight DESC, so.id
        """,
        (slot_template_id,),
    ).fetchall()
    result = []
    for row in rows:
        data = dict(row)
        data["flags"] = {
            "quick": bool(row["is_quick"]),
            "clean": bool(row["is_clean"]),
            "cheap": bool(row["is_cheap"]),
            "high_protein": bool(row["is_high_protein"]),
        }
        data["meal_summary"] = {
            "id": row["meal_template_id"],
            "title": row["meal_title"],
            "mfp_alias": row["mfp_alias"],
            "kcal_per_serving": row["kcal_per_serving"] or 0.0,
            "p_per_serving": row["p_per_serving"] or 0.0,
            "c_per_serving": row["c_per_serving"] or 0.0,
            "f_per_serving": row["f_per_serving"] or 0.0,
            "export_text": row["export_text"],
            "tags": row["tags"],
        }
        result.append(data)
    conn.close()
    return result


def create_slot_option(slot_template_id: int, payload: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    ensure_nutrition_planning_schema()
    meal_id = payload.get("meal_template_id")
    if not meal_id:
        return None, "meal_required"
    now = _utcnow_iso()
    conn = get_nutrition_db()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO nutrition_slot_options (slot_template_id, meal_template_id, default_servings, weight, priority,
            is_quick, is_clean, is_cheap, is_high_protein, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            slot_template_id,
            meal_id,
            _clamp_float(payload.get("default_servings"), 0.1, None) or 1.0,
            _clamp_float(payload.get("weight"), 0.0, None) or 1.0,
            int(payload.get("priority") or 0),
            _as_int_bool(payload.get("is_quick")),
            _as_int_bool(payload.get("is_clean")),
            _as_int_bool(payload.get("is_cheap")),
            _as_int_bool(payload.get("is_high_protein")),
            now,
            now,
        ),
    )
    option_id = cur.lastrowid
    conn.commit()
    row = cur.execute("SELECT * FROM nutrition_slot_options WHERE id=?", (option_id,)).fetchone()
    conn.close()
    return (dict(row) if row else None), None


def _serialize_meal_template_summary(row: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not row:
        return {}
    return {
        "id": row["id"],
        "title": row["title"],
        "default_servings": row.get("default_servings") or 1.0,
        "mfp_alias": row.get("mfp_alias"),
        "export_text": row.get("export_text"),
        "tags": row.get("tags"),
        "kcal_per_serving": row.get("kcal_per_serving") or 0.0,
        "p_per_serving": row.get("p_per_serving") or 0.0,
        "c_per_serving": row.get("c_per_serving") or 0.0,
        "f_per_serving": row.get("f_per_serving") or 0.0,
    }


def _macro_totals_from_meal_summary(summary: Dict[str, Any]) -> MacroTotals:
    if not summary:
        return MacroTotals(0.0, 0.0, 0.0, 0.0)
    return MacroTotals(
        kcal=summary.get("kcal_per_serving") or 0.0,
        p=summary.get("p_per_serving") or 0.0,
        c=summary.get("c_per_serving") or 0.0,
        f=summary.get("f_per_serving") or 0.0,
    )


def _macro_to_dict(macro: MacroTotals) -> Dict[str, float]:
    return {
        "kcal": round(macro.kcal, 1),
        "p": round(macro.p, 1),
        "c": round(macro.c, 1),
        "f": round(macro.f, 1),
        "sugar": round(macro.sugar, 1),
        "salt": round(macro.salt, 1),
    }


def _macro_diff(target: MacroTotals, actual: MacroTotals) -> MacroTotals:
    return MacroTotals(
        kcal=target.kcal - actual.kcal,
        p=target.p - actual.p,
        c=target.c - actual.c,
        f=target.f - actual.f,
        sugar=target.sugar - actual.sugar,
        salt=target.salt - actual.salt,
    )


def _macro_divide(macro: MacroTotals, divisor: float) -> MacroTotals:
    if divisor <= 0:
        return macro
    return MacroTotals(
        kcal=macro.kcal / divisor,
        p=macro.p / divisor,
        c=macro.c / divisor,
        f=macro.f / divisor,
        sugar=macro.sugar / divisor,
        salt=macro.salt / divisor,
    )


def _load_macro_targets_from_settings(cur) -> Dict[str, Optional[float]]:
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS nutrition_settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
        """
    )
    rows = cur.execute("SELECT key, value FROM nutrition_settings").fetchall()
    settings = {row["key"]: row["value"] for row in rows}
    active_mode = settings.get("active_mode") or settings.get("selected_mode") or "maintenance"

    def parse_value(key: str) -> Optional[float]:
        raw = settings.get(key)
        if raw is None:
            return None
        try:
            return float(str(raw).replace(",", "."))
        except Exception:
            return None

    kcal = parse_value(f"mode_{active_mode}_target")
    protein = parse_value(f"mode_{active_mode}_protein_target")
    carbs = parse_value(f"mode_{active_mode}_carbs_target")
    fat = parse_value(f"mode_{active_mode}_fat_target")
    return {
        "kcal": kcal,
        "p": protein,
        "c": carbs,
        "f": fat,
        "mode": active_mode,
    }


def _coerce_macro_float(value: Any) -> float:
    parsed = _clamp_float(value, 0.0, None)
    return float(parsed) if parsed is not None else 0.0


def _berlin_today() -> date:
    return datetime.now(ZoneInfo("Europe/Berlin")).date()


def _active_week_template_macro_targets(cur, today: date) -> Optional[Dict[str, Any]]:
    week_row = cur.execute(
        "SELECT * FROM nutrition_week_templates WHERE is_active=1 ORDER BY updated_at DESC, id DESC LIMIT 1"
    ).fetchone()
    if not week_row:
        return None

    macro_settings = _get_week_template_macro_settings(cur, week_row)
    active_targets = _active_targets_from_macro_settings(macro_settings)
    today_weekday = today.weekday()
    day_row = cur.execute(
        """
        SELECT weekday, target_kcal, target_p, target_c, target_f, updated_at
        FROM nutrition_week_template_days
        WHERE week_template_id=? AND weekday=?
        LIMIT 1
        """,
        (week_row["id"], today_weekday),
    ).fetchone()

    target = {
        "kcal": active_targets.get("kcal"),
        "p": active_targets.get("p"),
        "c": active_targets.get("c"),
        "f": active_targets.get("f"),
    }
    updated_at = week_row["updated_at"] if "updated_at" in week_row.keys() else None
    if day_row:
        target.update(
            {
                "kcal": day_row["target_kcal"] if day_row["target_kcal"] is not None else target.get("kcal"),
                "p": day_row["target_p"] if day_row["target_p"] is not None else target.get("p"),
                "c": day_row["target_c"] if day_row["target_c"] is not None else target.get("c"),
                "f": day_row["target_f"] if day_row["target_f"] is not None else target.get("f"),
            }
        )
        updated_at = day_row["updated_at"] or updated_at

    return {
        "kcal": _coerce_macro_float(target.get("kcal")),
        "p": _coerce_macro_float(target.get("p")),
        "c": _coerce_macro_float(target.get("c")),
        "f": _coerce_macro_float(target.get("f")),
        "source": "active_week_template",
        "mode": active_targets.get("mode") or macro_settings.get("active_mode") or "maintenance",
        "template_id": int(week_row["id"]),
        "template_title": week_row["title"],
        "weekday": today_weekday,
        "effective_date": today.isoformat(),
        "updated_at": updated_at,
    }


def get_live_macro_targets(*, initialize_schema: bool = True) -> Dict[str, Any]:
    if initialize_schema:
        ensure_nutrition_planning_schema()
    conn = get_nutrition_db()
    cur = conn.cursor()
    today = _berlin_today()
    targets = _active_week_template_macro_targets(cur, today)
    if targets:
        conn.close()
        return targets
    targets = _load_macro_targets_from_settings(cur)
    conn.close()
    return {
        "kcal": _coerce_macro_float(targets.get("kcal")),
        "p": _coerce_macro_float(targets.get("p")),
        "c": _coerce_macro_float(targets.get("c")),
        "f": _coerce_macro_float(targets.get("f")),
        "source": "settings",
        "mode": targets.get("mode") or "maintenance",
        "effective_date": today.isoformat(),
        "updated_at": None,
    }


def _macro_dict_diff(target: Dict[str, Optional[float]], actual: MacroTotals) -> Dict[str, Optional[float]]:
    out: Dict[str, Optional[float]] = {}
    for key, value in (("kcal", actual.kcal), ("p", actual.p), ("c", actual.c), ("f", actual.f)):
        t = target.get(key)
        if t is None:
            out[key] = None
        else:
            out[key] = round(t - value, 1)
    return out


def _macro_dict_from_targets(target: Dict[str, Optional[float]]) -> Dict[str, Optional[float]]:
    return {
        "kcal": None if target.get("kcal") is None else round(float(target.get("kcal")), 1),
        "p": None if target.get("p") is None else round(float(target.get("p")), 1),
        "c": None if target.get("c") is None else round(float(target.get("c")), 1),
        "f": None if target.get("f") is None else round(float(target.get("f")), 1),
    }


def _flags_list_from_option(row: Dict[str, Any]) -> List[str]:
    flags = []
    if row.get("is_quick"):
        flags.append("quick")
    if row.get("is_clean"):
        flags.append("clean")
    if row.get("is_cheap"):
        flags.append("cheap")
    if row.get("is_high_protein"):
        flags.append("high_protein")
    return flags


def _serialize_slot_option(row: Dict[str, Any], meal_templates_by_id: Dict[int, Dict[str, Any]]) -> Dict[str, Any]:
    meal = meal_templates_by_id.get(row["meal_template_id"])
    return {
        "id": row["id"],
        "slot_template_id": row["slot_template_id"],
        "meal_template_id": row["meal_template_id"],
        "default_servings": row.get("default_servings") or 1.0,
        "weight": row.get("weight") or 1.0,
        "priority": row.get("priority") or 0,
        "flags": _flags_list_from_option(row),
        "meal_template": _serialize_meal_template_summary(meal),
    }


def get_active_week_plan_resolved(*, initialize_schema: bool = True) -> Dict[str, Any]:
    if initialize_schema:
        ensure_nutrition_planning_schema()
    conn = get_nutrition_db()
    payload: Dict[str, Any] = {"ok": False, "week_template": None}
    try:
        cur = conn.cursor()
        week_row = cur.execute(
            "SELECT * FROM nutrition_week_templates WHERE is_active=1 ORDER BY updated_at DESC, id DESC LIMIT 1"
        ).fetchone()
        if not week_row:
            payload["error"] = "no_active_week_template"
        else:
            payload = _build_week_plan_payload(cur, week_row)
    finally:
        conn.close()
    if not payload.get("week_template"):
        return payload
    payload["slot_templates"] = list_slot_templates(initialize_schema=initialize_schema)
    payload["meal_templates"] = list_meal_templates(limit=400, initialize_schema=initialize_schema)
    payload["foods"] = list_foods(limit=500, initialize_schema=initialize_schema)
    payload["ok"] = True
    return payload


def list_week_templates(include_archived: bool = False) -> List[Dict[str, Any]]:
    ensure_nutrition_planning_schema()
    conn = get_nutrition_db()
    cur = conn.cursor()
    if include_archived:
        rows = cur.execute(
            "SELECT id, title, is_active, archived_at, last_used_at, created_at, updated_at FROM nutrition_week_templates ORDER BY updated_at DESC, id DESC"
        ).fetchall()
    else:
        rows = cur.execute(
            "SELECT id, title, is_active, archived_at, last_used_at, created_at, updated_at FROM nutrition_week_templates WHERE archived_at IS NULL ORDER BY updated_at DESC, id DESC"
        ).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def set_active_week_template(template_id: int) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    ensure_nutrition_planning_schema()
    conn = get_nutrition_db()
    cur = conn.cursor()
    row = cur.execute("SELECT * FROM nutrition_week_templates WHERE id=?", (template_id,)).fetchone()
    if not row:
        conn.close()
        return None, "not_found"
    now = _utcnow_iso()
    cur.execute("UPDATE nutrition_week_templates SET is_active=0")
    cur.execute(
        "UPDATE nutrition_week_templates SET is_active=1, last_used_at=?, updated_at=? WHERE id=?",
        (now, now, template_id),
    )
    _record_week_template_timeline_snapshot(cur, template_id, effective_date=_timeline_date_from_timestamp(now))
    conn.commit()
    updated = cur.execute("SELECT * FROM nutrition_week_templates WHERE id=?", (template_id,)).fetchone()
    conn.close()
    return (dict(updated) if updated else None), None


def create_week_template(title: str, source_template_id: Optional[int] = None, set_active: bool = True) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    ensure_nutrition_planning_schema()
    name = (title or "").strip() or "Neues Template"
    conn = get_nutrition_db()
    cur = conn.cursor()
    now = _utcnow_iso()

    if set_active:
        cur.execute("UPDATE nutrition_week_templates SET is_active=0")

    cur.execute(
        "INSERT INTO nutrition_week_templates (title, is_active, archived_at, last_used_at, created_at, updated_at) VALUES (?, ?, NULL, ?, ?, ?)",
        (name, 1 if set_active else 0, now if set_active else None, now, now),
    )
    new_template_id = cur.lastrowid

    if source_template_id:
        source_week = cur.execute(
            "SELECT * FROM nutrition_week_templates WHERE id=?", (source_template_id,)
        ).fetchone()
        if not source_week:
            conn.rollback()
            conn.close()
            return None, "source_not_found"
        cur.execute(
            """
            UPDATE nutrition_week_templates
            SET macro_active_mode=?, macro_modes_json=?, updated_at=?
            WHERE id=?
            """,
            (
                source_week["macro_active_mode"] if "macro_active_mode" in source_week.keys() else None,
                source_week["macro_modes_json"] if "macro_modes_json" in source_week.keys() else None,
                now,
                new_template_id,
            ),
        )
        source_days = cur.execute(
            "SELECT * FROM nutrition_week_template_days WHERE week_template_id=? ORDER BY weekday",
            (source_template_id,),
        ).fetchall()
        day_id_map: Dict[int, int] = {}
        for day in source_days:
            cur.execute(
                """
                INSERT INTO nutrition_week_template_days
                    (week_template_id, weekday, day_type, target_kcal, target_p, target_c, target_f, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    new_template_id,
                    day["weekday"],
                    day["day_type"],
                    day["target_kcal"] if "target_kcal" in day.keys() else None,
                    day["target_p"] if "target_p" in day.keys() else None,
                    day["target_c"] if "target_c" in day.keys() else None,
                    day["target_f"] if "target_f" in day.keys() else None,
                    now,
                    now,
                ),
            )
            day_id_map[day["id"]] = cur.lastrowid

        source_slots = cur.execute(
            "SELECT * FROM nutrition_week_day_slots WHERE week_template_day_id IN ({}) ORDER BY week_template_day_id, slot_index".format(
                ",".join("?" for _ in day_id_map) if day_id_map else "NULL"
            ),
            tuple(day_id_map.keys()) if day_id_map else (),
        ).fetchall()
        slot_id_map: Dict[int, int] = {}
        for slot in source_slots:
            new_day_id = day_id_map.get(slot["week_template_day_id"])
            if not new_day_id:
                continue
            cur.execute(
                """
                INSERT INTO nutrition_week_day_slots
                    (week_template_day_id, slot_template_id, slot_index, active_option_id, locked, time_text, custom_title, note_text, meal_template_id, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    new_day_id,
                    slot["slot_template_id"],
                    slot["slot_index"],
                    slot["active_option_id"] if "active_option_id" in slot.keys() else None,
                    (slot["locked"] if "locked" in slot.keys() else 0) or 0,
                    slot["time_text"] if "time_text" in slot.keys() else None,
                    slot["custom_title"] if "custom_title" in slot.keys() else None,
                    slot["note_text"] if "note_text" in slot.keys() else None,
                    slot["meal_template_id"] if "meal_template_id" in slot.keys() else None,
                    now,
                    now,
                ),
            )
            slot_id_map[slot["id"]] = cur.lastrowid

        if slot_id_map:
            overrides = cur.execute(
                "SELECT * FROM nutrition_slot_meal_overrides WHERE slot_id IN ({})".format(
                    ",".join("?" for _ in slot_id_map)
                ),
                tuple(slot_id_map.keys()),
            ).fetchall()
            for override in overrides:
                new_slot_id = slot_id_map.get(override["slot_id"])
                if not new_slot_id:
                    continue
                cur.execute(
                    """
                    INSERT INTO nutrition_slot_meal_overrides (slot_id, meal_template_id, ingredients_json, updated_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        new_slot_id,
                        override["meal_template_id"] if "meal_template_id" in override.keys() else None,
                        override["ingredients_json"] if "ingredients_json" in override.keys() else None,
                        now,
                    ),
                )
        # Keep the semantic weekday links when a plan is duplicated. Member
        # weekday ids are new in the cloned template, so map them explicitly.
        source_groups = cur.execute(
            "SELECT * FROM nutrition_week_day_groups WHERE week_template_id=? ORDER BY created_at, id",
            (source_template_id,),
        ).fetchall()
        for source_group in source_groups:
            new_source_day_id = day_id_map.get(source_group["source_week_template_day_id"])
            if not new_source_day_id:
                continue
            new_group_id = str(uuid4())
            cur.execute(
                """INSERT INTO nutrition_week_day_groups
                   (id, week_template_id, name, color, source_week_template_day_id, revision, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (new_group_id, new_template_id, source_group["name"], source_group["color"], new_source_day_id, source_group["revision"], now, now),
            )
            members = cur.execute("SELECT week_template_day_id FROM nutrition_week_day_group_members WHERE group_id=?", (source_group["id"],)).fetchall()
            for member in members:
                new_member_id = day_id_map.get(member["week_template_day_id"])
                if new_member_id:
                    cur.execute("INSERT INTO nutrition_week_day_group_members (group_id, week_template_day_id, created_at) VALUES (?, ?, ?)", (new_group_id, new_member_id, now))
    else:
        for tag in WEEK_TEMPLATE_TAGS:
            cur.execute(
                """
                INSERT INTO nutrition_week_template_days
                    (week_template_id, weekday, day_type, target_kcal, target_p, target_c, target_f, created_at, updated_at)
                VALUES (?, ?, ?, NULL, NULL, NULL, NULL, ?, ?)
                """,
                (new_template_id, tag["weekday"], tag["day_type"], now, now),
            )
        default_templates = _ensure_default_slot_templates(cur)
        day_rows = cur.execute(
            "SELECT * FROM nutrition_week_template_days WHERE week_template_id=? ORDER BY weekday",
            (new_template_id,),
        ).fetchall()
        for day in day_rows:
            for idx, slot_template in enumerate(default_templates[:DEFAULT_BASE_SLOT_COUNT]):
                cur.execute(
                    """
                    INSERT INTO nutrition_week_day_slots
                        (week_template_day_id, slot_template_id, slot_index, active_option_id, locked, time_text, custom_title, note_text, meal_template_id, created_at, updated_at)
                    VALUES (?, ?, ?, NULL, 0, NULL, NULL, NULL, NULL, ?, ?)
                    """,
                    (day["id"], slot_template["id"], idx, now, now),
                )
        fallback_settings = _load_global_macro_mode_settings(cur)
        cur.execute(
            """
            UPDATE nutrition_week_templates
            SET macro_active_mode=?, macro_modes_json=?, updated_at=?
            WHERE id=?
            """,
            (
                fallback_settings.get("active_mode") or "maintenance",
                json.dumps(fallback_settings, ensure_ascii=False),
                now,
                new_template_id,
            ),
        )

    if set_active:
        _record_week_template_timeline_snapshot(cur, new_template_id, effective_date=_timeline_date_from_timestamp(now))

    conn.commit()
    row = cur.execute("SELECT * FROM nutrition_week_templates WHERE id=?", (new_template_id,)).fetchone()
    conn.close()
    return (dict(row) if row else None), None


def archive_week_template(template_id: int) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    ensure_nutrition_planning_schema()
    conn = get_nutrition_db()
    cur = conn.cursor()
    row = cur.execute("SELECT * FROM nutrition_week_templates WHERE id=?", (template_id,)).fetchone()
    if not row:
        conn.close()
        return None, "not_found"
    now = _utcnow_iso()
    cur.execute(
        "UPDATE nutrition_week_templates SET archived_at=?, is_active=0, updated_at=? WHERE id=?",
        (now, now, template_id),
    )
    conn.commit()
    updated = cur.execute("SELECT * FROM nutrition_week_templates WHERE id=?", (template_id,)).fetchone()
    conn.close()
    return (dict(updated) if updated else None), None


def restore_week_template(template_id: int) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    ensure_nutrition_planning_schema()
    conn = get_nutrition_db()
    cur = conn.cursor()
    row = cur.execute("SELECT * FROM nutrition_week_templates WHERE id=?", (template_id,)).fetchone()
    if not row:
        conn.close()
        return None, "not_found"
    cur.execute(
        "UPDATE nutrition_week_templates SET archived_at=NULL, updated_at=? WHERE id=?",
        (_utcnow_iso(), template_id),
    )
    conn.commit()
    updated = cur.execute("SELECT * FROM nutrition_week_templates WHERE id=?", (template_id,)).fetchone()
    conn.close()
    return (dict(updated) if updated else None), None


def delete_week_template(template_id: int) -> Tuple[bool, Optional[str]]:
    ensure_nutrition_planning_schema()
    conn = get_nutrition_db()
    cur = conn.cursor()
    row = cur.execute("SELECT id FROM nutrition_week_templates WHERE id=?", (template_id,)).fetchone()
    if not row:
        conn.close()
        return False, "not_found"
    cur.execute("DELETE FROM nutrition_week_templates WHERE id=?", (template_id,))
    conn.commit()
    conn.close()
    return True, None


# UI Lab chart tokens: high-legibility accents on LIVA's dark surfaces.
# At most three day groups can coexist (every group needs at least two days),
# but keep the complete chart sequence for cloned/imported templates.
DAY_GROUP_COLORS = ("#ffd25a", "#80cdf7", "#2ce4aa", "#5d89ff", "#ef7378")


def _touch_week_revision(cur, template_id: int) -> int:
    cur.execute(
        "UPDATE nutrition_week_templates SET revision=COALESCE(revision, 0)+1, updated_at=? WHERE id=?",
        (_utcnow_iso(), int(template_id)),
    )
    row = cur.execute("SELECT revision FROM nutrition_week_templates WHERE id=?", (int(template_id),)).fetchone()
    return int(row["revision"] or 1) if row else 1


def _copy_week_day_content(cur, source_day_id: int, target_day_id: int) -> None:
    """Replace one weekday's plan content with an exact, self-contained copy."""
    source = cur.execute("SELECT * FROM nutrition_week_template_days WHERE id=?", (source_day_id,)).fetchone()
    target = cur.execute("SELECT * FROM nutrition_week_template_days WHERE id=?", (target_day_id,)).fetchone()
    if not source or not target:
        raise ValueError("day_not_found")
    now = _utcnow_iso()
    cur.execute(
        """UPDATE nutrition_week_template_days
           SET day_type=?, target_kcal=?, target_p=?, target_c=?, target_f=?, updated_at=? WHERE id=?""",
        (source["day_type"], source["target_kcal"], source["target_p"], source["target_c"], source["target_f"], now, target_day_id),
    )
    cur.execute("DELETE FROM nutrition_slot_meals WHERE week_template_day_id=?", (target_day_id,))
    cur.execute("DELETE FROM nutrition_week_day_slots WHERE week_template_day_id=?", (target_day_id,))
    slot_id_map: Dict[int, int] = {}
    source_slots = cur.execute(
        "SELECT * FROM nutrition_week_day_slots WHERE week_template_day_id=? ORDER BY slot_index, id", (source_day_id,)
    ).fetchall()
    for slot in source_slots:
        cur.execute(
            """INSERT INTO nutrition_week_day_slots
               (week_template_day_id, slot_template_id, slot_index, active_option_id, locked, time_text, custom_title, note_text, meal_template_id, created_at, updated_at)
               VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?)""",
            (target_day_id, slot["slot_template_id"], slot["slot_index"], slot["locked"], slot["time_text"], slot["custom_title"], slot["note_text"], slot["meal_template_id"], now, now),
        )
        slot_id_map[int(slot["id"])] = int(cur.lastrowid)
    for old_slot_id, new_slot_id in slot_id_map.items():
        override = cur.execute("SELECT * FROM nutrition_slot_meal_overrides WHERE slot_id=?", (old_slot_id,)).fetchone()
        if override:
            cur.execute(
                "INSERT INTO nutrition_slot_meal_overrides (slot_id, meal_template_id, ingredients_json, updated_at) VALUES (?, ?, ?, ?)",
                (new_slot_id, override["meal_template_id"], override["ingredients_json"], now),
            )
    source_meals = cur.execute(
        "SELECT * FROM nutrition_slot_meals WHERE week_template_day_id=? ORDER BY slot_index, id", (source_day_id,)
    ).fetchall()
    for meal in source_meals:
        cur.execute(
            """INSERT INTO nutrition_slot_meals
               (week_template_day_id, slot_index, meal_title, multiplier, template_id, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (target_day_id, meal["slot_index"], meal["meal_title"], meal["multiplier"], meal["template_id"], now, now),
        )
        new_meal_id = int(cur.lastrowid)
        for item in cur.execute("SELECT * FROM nutrition_slot_meal_items WHERE slot_meal_id=? ORDER BY id", (meal["id"],)).fetchall():
            cur.execute(
                """INSERT INTO nutrition_slot_meal_items
                   (slot_meal_id, amount, unit, name_raw, food_id, item_type, calories, display_text, client_req_id, fingerprint, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (new_meal_id, item["amount"], item["unit"], item["name_raw"], item["food_id"], item["item_type"], item["calories"], item["display_text"], item["client_req_id"], item["fingerprint"], now, now),
            )


def _group_payloads(cur, template_id: int) -> List[Dict[str, Any]]:
    rows = cur.execute(
        """SELECT g.*, d.weekday AS source_weekday, m.week_template_day_id, member_day.weekday
           FROM nutrition_week_day_groups g
           JOIN nutrition_week_day_group_members m ON m.group_id=g.id
           JOIN nutrition_week_template_days d ON d.id=g.source_week_template_day_id
           JOIN nutrition_week_template_days member_day ON member_day.id=m.week_template_day_id
           WHERE g.week_template_id=? ORDER BY g.created_at, member_day.weekday""",
        (template_id,),
    ).fetchall()
    grouped: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        data = grouped.setdefault(str(row["id"]), {
            "id": str(row["id"]), "name": row["name"], "color": row["color"],
            "revision": int(row["revision"] or 1), "source_weekday": int(row["source_weekday"]),
            "weekdays": [], "member_day_ids": [],
        })
        data["weekdays"].append(int(row["weekday"]))
        data["member_day_ids"].append(int(row["week_template_day_id"]))
    return list(grouped.values())


def create_week_day_group(template_id: int, weekdays: Sequence[Any], source_weekday: Any, name: str, color: Optional[str] = None) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    ensure_nutrition_planning_schema()
    selected = sorted({int(day) for day in weekdays if str(day).strip() in {str(x) for x in range(7)}})
    try:
        source_weekday = int(source_weekday)
    except (TypeError, ValueError):
        return None, "source_weekday_required"
    if len(selected) < 2 or source_weekday not in selected:
        return None, "select_at_least_two_days_including_source"
    title = str(name or "").strip() or "Gemeinsamer Tag"
    conn = get_nutrition_db(); cur = conn.cursor()
    try:
        days = cur.execute("SELECT id, weekday FROM nutrition_week_template_days WHERE week_template_id=?", (template_id,)).fetchall()
        by_weekday = {int(row["weekday"]): int(row["id"]) for row in days}
        if any(day not in by_weekday for day in selected):
            return None, "weekday_not_found"
        source_day_id = by_weekday[source_weekday]
        now = _utcnow_iso(); group_id = str(uuid4())
        used_color_rows = cur.execute("SELECT color FROM nutrition_week_day_groups WHERE week_template_id=?", (template_id,)).fetchall()
        used_colors = {str(row["color"] or "") for row in used_color_rows}
        assigned_color = color if color in DAY_GROUP_COLORS else next(
            (candidate for candidate in DAY_GROUP_COLORS if candidate not in used_colors),
            DAY_GROUP_COLORS[len(used_colors) % len(DAY_GROUP_COLORS)],
        )
        cur.execute(
            "INSERT INTO nutrition_week_day_groups (id, week_template_id, name, color, source_week_template_day_id, revision, created_at, updated_at) VALUES (?, ?, ?, ?, ?, 1, ?, ?)",
            (group_id, template_id, title, assigned_color, source_day_id, now, now),
        )
        # A weekday can belong to exactly one group. Detaching it from a prior
        # group leaves the prior group intact for its remaining members.
        for weekday in selected:
            day_id = by_weekday[weekday]
            cur.execute("DELETE FROM nutrition_week_day_group_members WHERE week_template_day_id=?", (day_id,))
            cur.execute("INSERT INTO nutrition_week_day_group_members (group_id, week_template_day_id, created_at) VALUES (?, ?, ?)", (group_id, day_id, now))
            if day_id != source_day_id:
                _copy_week_day_content(cur, source_day_id, day_id)
        # Moving a weekday into a new template can leave its old group with
        # one member. A one-day group has no semantic value and must not render
        # as a stray connector in the week strip.
        cur.execute(
            """DELETE FROM nutrition_week_day_groups
               WHERE id IN (
                 SELECT g.id FROM nutrition_week_day_groups g
                 LEFT JOIN nutrition_week_day_group_members m ON m.group_id=g.id
                 GROUP BY g.id HAVING COUNT(m.week_template_day_id) < 2
               )"""
        )
        revision = _touch_week_revision(cur, template_id); conn.commit()
        group = next((g for g in _group_payloads(cur, template_id) if g["id"] == group_id), None)
        if group: group["template_revision"] = revision
        return group, None
    except Exception as exc:
        conn.rollback(); return None, str(exc)
    finally:
        conn.close()


def detach_week_day_group_members(template_id: int, group_id: str, weekdays: Sequence[Any]) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    ensure_nutrition_planning_schema(); conn = get_nutrition_db(); cur = conn.cursor()
    try:
        group = cur.execute("SELECT * FROM nutrition_week_day_groups WHERE id=? AND week_template_id=?", (group_id, template_id)).fetchone()
        if not group: return None, "group_not_found"
        selected = {int(day) for day in weekdays}
        rows = cur.execute("""SELECT m.week_template_day_id, d.weekday FROM nutrition_week_day_group_members m
                            JOIN nutrition_week_template_days d ON d.id=m.week_template_day_id WHERE m.group_id=?""", (group_id,)).fetchall()
        to_remove = [row for row in rows if int(row["weekday"]) in selected]
        if not to_remove: return None, "no_matching_group_members"
        for row in to_remove: cur.execute("DELETE FROM nutrition_week_day_group_members WHERE group_id=? AND week_template_day_id=?", (group_id, row["week_template_day_id"]))
        remaining = cur.execute("SELECT week_template_day_id FROM nutrition_week_day_group_members WHERE group_id=?", (group_id,)).fetchall()
        if len(remaining) < 2: cur.execute("DELETE FROM nutrition_week_day_groups WHERE id=?", (group_id,))
        elif int(group["source_week_template_day_id"]) in {int(row["week_template_day_id"]) for row in to_remove}:
            cur.execute("UPDATE nutrition_week_day_groups SET source_week_template_day_id=?, revision=revision+1, updated_at=? WHERE id=?", (remaining[0]["week_template_day_id"], _utcnow_iso(), group_id))
        revision = _touch_week_revision(cur, template_id); conn.commit()
        return {"group_id": group_id, "detached_weekdays": sorted(selected), "template_revision": revision}, None
    except Exception as exc:
        conn.rollback(); return None, str(exc)
    finally: conn.close()


def sync_week_day_group(template_id: int, group_id: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Project the group's canonical source day onto every linked weekday."""
    ensure_nutrition_planning_schema(); conn = get_nutrition_db(); cur = conn.cursor()
    try:
        group = cur.execute("SELECT * FROM nutrition_week_day_groups WHERE id=? AND week_template_id=?", (group_id, template_id)).fetchone()
        if not group: return None, "group_not_found"
        source_id = int(group["source_week_template_day_id"])
        members = cur.execute("SELECT week_template_day_id FROM nutrition_week_day_group_members WHERE group_id=?", (group_id,)).fetchall()
        if len(members) < 2: return None, "group_requires_two_members"
        for member in members:
            target_id = int(member["week_template_day_id"])
            if target_id != source_id: _copy_week_day_content(cur, source_id, target_id)
        now = _utcnow_iso()
        cur.execute("UPDATE nutrition_week_day_groups SET revision=revision+1, updated_at=? WHERE id=?", (now, group_id))
        revision = _touch_week_revision(cur, template_id); conn.commit()
        return {"group_id": group_id, "source_day_id": source_id, "affected_days": len(members), "template_revision": revision}, None
    except Exception as exc:
        conn.rollback(); return None, str(exc)
    finally: conn.close()


def update_week_day_group_amount(template_id: int, group_id: str, *, slot_index: Optional[int], time_text: Optional[str], food_id: int, amount: float, unit: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Atomically changes one food amount in the same meal on every group member."""
    ensure_nutrition_planning_schema(); conn = get_nutrition_db(); cur = conn.cursor()
    try:
        group = cur.execute("SELECT * FROM nutrition_week_day_groups WHERE id=? AND week_template_id=?", (group_id, template_id)).fetchone()
        if not group: return None, "group_not_found"
        members = cur.execute("SELECT week_template_day_id FROM nutrition_week_day_group_members WHERE group_id=?", (group_id,)).fetchall()
        if len(members) < 2: return None, "group_requires_two_members"
        updates = []
        for member in members:
            where, args = ("slot_index=?", [int(slot_index)]) if slot_index is not None else ("COALESCE(time_text, '')=?", [str(time_text or "")])
            slot = cur.execute(f"SELECT * FROM nutrition_week_day_slots WHERE week_template_day_id=? AND {where}", (member["week_template_day_id"], *args)).fetchone()
            if not slot: return None, "slot_not_found_in_every_group_day"
            slot_meal = cur.execute("SELECT id FROM nutrition_slot_meals WHERE week_template_day_id=? AND slot_index=?", (member["week_template_day_id"], slot["slot_index"])).fetchone()
            if slot_meal:
                item = cur.execute("SELECT id FROM nutrition_slot_meal_items WHERE slot_meal_id=? AND food_id=?", (slot_meal["id"], int(food_id))).fetchone()
                if not item: return None, "food_not_found_in_every_group_day"
                updates.append(("slot_meal_item", int(item["id"])))
                continue
            override = cur.execute("SELECT ingredients_json FROM nutrition_slot_meal_overrides WHERE slot_id=?", (slot["id"],)).fetchone()
            if override:
                try: ingredients = json.loads(override["ingredients_json"] or "[]")
                except (TypeError, ValueError): ingredients = []
            else:
                rows = cur.execute("""SELECT mi.food_id, mi.amount, mi.unit, f.name AS food_name
                                    FROM nutrition_meal_ingredients mi JOIN nutrition_foods f ON f.id=mi.food_id
                                    WHERE mi.meal_template_id=? ORDER BY mi.sort_index, mi.id""", (slot["meal_template_id"],)).fetchall()
                ingredients = [{"food_id": row["food_id"], "food_name": row["food_name"], "amount": row["amount"], "unit": row["unit"]} for row in rows]
            matching = next((item for item in ingredients if int(item.get("food_id") or 0) == int(food_id)), None)
            if not matching: return None, "food_not_found_in_every_group_day"
            matching["amount"] = float(amount); matching["unit"] = unit
            updates.append(("slot_override", int(slot["id"]), ingredients, slot["meal_template_id"]))
        now = _utcnow_iso()
        for update in updates:
            if update[0] == "slot_meal_item":
                cur.execute("UPDATE nutrition_slot_meal_items SET amount=?, unit=?, updated_at=? WHERE id=?", (float(amount), unit, now, update[1]))
            else:
                _, slot_id, ingredients, meal_template_id = update
                cur.execute("""INSERT INTO nutrition_slot_meal_overrides (slot_id, meal_template_id, ingredients_json, updated_at)
                               VALUES (?, ?, ?, ?)
                               ON CONFLICT(slot_id) DO UPDATE SET meal_template_id=excluded.meal_template_id, ingredients_json=excluded.ingredients_json, updated_at=excluded.updated_at""",
                            (slot_id, meal_template_id, json.dumps(ingredients, ensure_ascii=False), now))
        cur.execute("UPDATE nutrition_week_day_groups SET revision=revision+1, updated_at=? WHERE id=?", (now, group_id))
        revision = _touch_week_revision(cur, template_id); conn.commit()
        return {"group_id": group_id, "affected_days": len(updates), "food_id": int(food_id), "amount": float(amount), "unit": unit, "template_revision": revision}, None
    except Exception as exc:
        conn.rollback(); return None, str(exc)
    finally: conn.close()


def _build_week_plan_payload(cur, week_row: sqlite3.Row) -> Dict[str, Any]:
    template_id = week_row["id"]
    day_rows = [dict(row) for row in cur.execute(
        "SELECT * FROM nutrition_week_template_days WHERE week_template_id=? ORDER BY weekday",
        (template_id,),
    ).fetchall()]
    day_ids = [row["id"] for row in day_rows]
    slot_rows: List[Dict[str, Any]] = []
    if day_ids:
        placeholders = ",".join("?" for _ in day_ids)
        slot_rows = [dict(row) for row in cur.execute(
            f"SELECT * FROM nutrition_week_day_slots WHERE week_template_day_id IN ({placeholders}) ORDER BY week_template_day_id, slot_index",
            tuple(day_ids),
        ).fetchall()]
    slot_template_rows = [dict(row) for row in cur.execute("SELECT * FROM nutrition_slot_templates").fetchall()]
    slot_templates_by_id = {row["id"]: dict(row) for row in slot_template_rows}
    meal_ids = {row.get("meal_template_id") for row in slot_rows if row.get("meal_template_id")}
    meal_templates_by_id: Dict[int, Dict[str, Any]] = {}
    meal_ingredients_map: Dict[int, List[Dict[str, Any]]] = {}
    if meal_ids:
        placeholders = ",".join("?" for _ in meal_ids)
        meal_rows = cur.execute(
            f"SELECT * FROM nutrition_meal_templates WHERE id IN ({placeholders})",
            tuple(meal_ids),
        ).fetchall()
        meal_templates_by_id = {row["id"]: dict(row) for row in meal_rows}
        ingredient_rows = cur.execute(
            f"""
            SELECT mi.meal_template_id, mi.food_id, mi.amount, mi.unit, f.name AS food_name, f.common_portion_size
            FROM nutrition_meal_ingredients mi
            JOIN nutrition_foods f ON f.id=mi.food_id
            WHERE mi.meal_template_id IN ({placeholders})
            ORDER BY mi.meal_template_id, mi.sort_index, mi.id
            """,
            tuple(meal_ids),
        ).fetchall()
        for row in ingredient_rows:
            meal_ingredients_map.setdefault(row["meal_template_id"], []).append(
                {
                    "food_id": row["food_id"],
                    "food_name": row["food_name"],
                    "amount": row["amount"],
                    "unit": row["unit"],
                    "common_portion_size": row["common_portion_size"],
                }
            )
    override_rows = [dict(row) for row in cur.execute(
        "SELECT * FROM nutrition_slot_meal_overrides WHERE slot_id IN ({})".format(
            ",".join("?" for _ in slot_rows) if slot_rows else "NULL"
        ),
        tuple([row["id"] for row in slot_rows]) if slot_rows else (),
    ).fetchall()] if slot_rows else []
    overrides_by_slot = {row["slot_id"]: row for row in override_rows}
    slot_meal_rows = [dict(row) for row in cur.execute(
        "SELECT * FROM nutrition_slot_meals WHERE week_template_day_id IN ({})".format(
            ",".join("?" for _ in day_rows) if day_rows else "NULL"
        ),
        tuple([row["id"] for row in day_rows]) if day_rows else (),
    ).fetchall()] if day_rows else []
    slot_meal_by_key = {(row["week_template_day_id"], row["slot_index"]): row for row in slot_meal_rows}
    slot_meal_max_by_day: Dict[int, int] = {}
    for row in slot_meal_rows:
        day_id = row.get("week_template_day_id")
        if day_id is None:
            continue
        slot_meal_max_by_day[day_id] = max(slot_meal_max_by_day.get(day_id, -1), int(row.get("slot_index") or 0))
    slot_meal_ids = [row["id"] for row in slot_meal_rows]
    slot_meal_items = [dict(row) for row in cur.execute(
        "SELECT * FROM nutrition_slot_meal_items WHERE slot_meal_id IN ({})".format(
            ",".join("?" for _ in slot_meal_ids) if slot_meal_ids else "NULL"
        ),
        tuple(slot_meal_ids) if slot_meal_ids else (),
    ).fetchall()] if slot_meal_ids else []
    slot_meal_items_by_meal: Dict[int, List[Dict[str, Any]]] = defaultdict(list)
    for row in slot_meal_items:
        slot_meal_items_by_meal[row["slot_meal_id"]].append(row)
    foods_by_id = {row["id"]: dict(row) for row in cur.execute("SELECT * FROM nutrition_foods").fetchall()}
    slots_by_day: Dict[int, List[Dict[str, Any]]] = defaultdict(list)

    def _format_slot_items(source_items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        items: List[Dict[str, Any]] = []
        for item in source_items or []:
            kcal_value = item.get("kcal_value")
            unit = (item.get("unit") or "").lower()
            if kcal_value is not None or unit == "kcal":
                items.append(
                    {
                        "item_type": "kcal_only",
                        "calories": kcal_value if kcal_value is not None else (item.get("amount") or 0),
                        "name": item.get("food_name") or item.get("raw") or item.get("name_raw") or "Kcal",
                        "amount": item.get("amount") or 0,
                        "unit": "kcal",
                        "food_id": None,
                    }
                )
                continue
            items.append(
                {
                    "item_type": "food",
                    "food_id": item.get("food_id"),
                    "name": item.get("food_name") or item.get("name_raw") or item.get("raw") or "Food",
                    **_display_amount_struct(item.get("amount"), item.get("unit") or "g"),
                }
            )
        return items
    for row in slot_rows:
        template = slot_templates_by_id.get(row["slot_template_id"])
        meal = meal_templates_by_id.get(row.get("meal_template_id"))
        override = overrides_by_slot.get(row["id"])
        slot_meal = slot_meal_by_key.get((row["week_template_day_id"], row["slot_index"]))
        slot_meal_id = slot_meal.get("id") if slot_meal else None
        override_ingredients = None
        override_macros = None
        override_export = None
        slot_source_items = slot_meal_items_by_meal.get(slot_meal["id"], []) if slot_meal else []
        if slot_source_items:
            items = slot_source_items
            enriched_items = []
            for item in items:
                if item.get("item_type") == "kcal_only":
                    enriched_items.append(
                        {
                            "food_id": None,
                            "food_name": item.get("name_raw") or "Kcal",
                            "unit": "kcal",
                            "amount": item.get("calories") or 0,
                            "kcal_value": item.get("calories") or 0,
                        }
                    )
                    continue
                food = foods_by_id.get(int(item.get("food_id") or 0))
                if not food and item.get("name_raw"):
                    food = _resolve_food_row_for_import_label(cur, foods_by_id, item.get("name_raw"))
                if food:
                    enriched_items.append(
                        {
                            "food_id": food["id"],
                            "food_name": food.get("name"),
                            "unit_default": food.get("unit_default"),
                            "common_portion_size": food.get("common_portion_size"),
                            "amount": item.get("amount") or 0,
                            "unit": item.get("unit") or "g",
                        }
                    )
                else:
                    enriched_items.append(
                        {
                            "food_id": None,
                            "food_name": item.get("name_raw") or "Unbekannt",
                            "unit_default": None,
                            "common_portion_size": None,
                            "amount": item.get("amount") or 0,
                            "unit": item.get("unit") or "g",
                        }
                    )
            override_ingredients = enriched_items
            override_macros = _compute_override_macros(override_ingredients, foods_by_id)
            override_export = _build_override_export_text(override_ingredients, foods_by_id)
            override = None
        elif override:
            try:
                override_ingredients = json.loads(override.get("ingredients_json") or "[]")
            except Exception:
                override_ingredients = []
            enriched = []
            for item in override_ingredients:
                if item.get("kcal_value") is not None or item.get("unit") == "kcal":
                    enriched.append(
                        {
                            "food_id": None,
                            "food_name": item.get("food_name") or item.get("raw") or "Kcal",
                            "unit_default": None,
                            "common_portion_size": None,
                            "amount": item.get("amount") or item.get("kcal_value") or 0,
                            "unit": "kcal",
                            "kcal_value": item.get("kcal_value") or item.get("amount"),
                        }
                    )
                    continue
                food = foods_by_id.get(int(item.get("food_id") or 0))
                if not food and (item.get("food_name") or item.get("raw")):
                    food = _resolve_food_row_for_import_label(cur, foods_by_id, item.get("food_name") or item.get("raw"))
                if not food:
                    enriched.append(
                        {
                            "food_id": None,
                            "food_name": item.get("food_name") or item.get("raw") or "Unbekannt",
                            "unit_default": None,
                            "common_portion_size": None,
                            "amount": item.get("amount") or 0,
                            "unit": item.get("unit") or "g",
                        }
                    )
                    continue
                enriched.append(
                    {
                        "food_id": food["id"],
                        "food_name": food.get("name"),
                        "unit_default": food.get("unit_default"),
                        "common_portion_size": food.get("common_portion_size"),
                        "amount": item.get("amount") or 0,
                        "unit": item.get("unit") or "g",
                    }
                )
            override_ingredients = enriched
            override_macros = _compute_override_macros(override_ingredients, foods_by_id)
            override_export = _build_override_export_text(override_ingredients, foods_by_id)
        slot_items: List[Dict[str, Any]] = []
        if override_ingredients:
            slot_items = _format_slot_items(override_ingredients)
        elif meal and meal_ingredients_map.get(meal.get("id")):
            slot_items = _format_slot_items(meal_ingredients_map.get(meal.get("id")) or [])
        time_value = row.get("time_text") or (template or {}).get("default_time") or _default_time_for_slot_index(int(row.get("slot_index") or 0))
        slot_data = {
            "id": row["id"],
            "slot_index": row["slot_index"],
            "locked": bool(row["locked"]),
            "time_text": time_value,
            "custom_title": row.get("custom_title"),
            "note_text": row.get("note_text"),
            "slot_template": dict(template) if template else {},
            "meal_template_id": row.get("meal_template_id"),
            "slot_meal_id": slot_meal_id,
            "meal_title": slot_meal.get("meal_title") if slot_meal else None,
            # A custom title belongs to the weekday template and must win over
            # the original library-meal title.  Linked day projections copy
            # custom_title atomically, so this ordering makes the shared name
            # visible on every member day as well.
            "display_title": row.get("custom_title") or (slot_meal.get("meal_title") if slot_meal else None) or None,
            "source_template_title": meal.get("title") if meal else None,
            "meal_template": _serialize_meal_template_summary(meal) if meal else None,
            "servings": (meal.get("default_servings") if meal else None) or 1.0,
            "can_edit_time": True,
            "items": slot_items,
            "override": {
                "ingredients": override_ingredients,
                "kcal": override_macros.kcal if override_macros else None,
                "p": override_macros.p if override_macros else None,
                "c": override_macros.c if override_macros else None,
                "f": override_macros.f if override_macros else None,
                "export_text": override_export,
            } if override_ingredients is not None else None,
        }
        slots_by_day[row["week_template_day_id"]].append(slot_data)
    day_entries = []
    week_planned_total = MacroTotals(0.0, 0.0, 0.0, 0.0)
    macro_settings = _get_week_template_macro_settings(cur, week_row)
    targets = _active_targets_from_macro_settings(macro_settings)
    target_dict = _macro_dict_from_targets(targets)
    for day in day_rows:
        planned = MacroTotals(0.0, 0.0, 0.0, 0.0)
        max_slot_index = -1
        for slot in slots_by_day.get(day["id"], []):
            max_slot_index = max(max_slot_index, int(slot.get("slot_index") or 0))
            meal = slot.get("meal_template")
            servings = slot.get("servings") or 1.0
            if slot.get("override") and slot["override"].get("kcal") is not None:
                override_macros = MacroTotals(
                    kcal=slot["override"].get("kcal") or 0.0,
                    p=slot["override"].get("p") or 0.0,
                    c=slot["override"].get("c") or 0.0,
                    f=slot["override"].get("f") or 0.0,
                )
                planned = planned.add(override_macros.scaled(servings))
            elif meal:
                macros = _macro_totals_from_meal_summary(meal)
                planned = planned.add(macros.scaled(servings))
        week_planned_total = week_planned_total.add(planned)
        day_target = {
            "kcal": day.get("target_kcal") if day.get("target_kcal") is not None else targets.get("kcal"),
            "p": day.get("target_p") if day.get("target_p") is not None else targets.get("p"),
            "c": day.get("target_c") if day.get("target_c") is not None else targets.get("c"),
            "f": day.get("target_f") if day.get("target_f") is not None else targets.get("f"),
        }
        remaining = _macro_dict_diff(day_target, planned)
        label = WEEKDAY_LABELS[day["weekday"]] if 0 <= day["weekday"] < len(WEEKDAY_LABELS) else str(day["weekday"])
        max_slot_index = max(max_slot_index, slot_meal_max_by_day.get(day["id"], -1))
        day_entries.append(
            {
                "id": day["id"],
                "weekday": day["weekday"],
                "label": label,
                "day_type": day["day_type"],
                "target": day_target,
                "planned": _macro_to_dict(planned),
                "remaining": remaining,
                "max_slot": max_slot_index + 1 if max_slot_index >= 0 else 0,
                "slots": slots_by_day.get(day["id"], []),
            }
        )
    day_count = max(1, len(day_entries))
    avg_planned = _macro_divide(week_planned_total, day_count)
    avg_remaining = _macro_dict_diff(targets, avg_planned)
    notes: List[str] = []
    for day in day_entries:
        rem = day["remaining"]
        if rem["p"] is not None and rem["p"] > 10:
            notes.append(f"{day['label']}: Protein-Lücke {rem['p']:.0f}g")
        elif rem["p"] is not None and rem["p"] < -5:
            notes.append(f"{day['label']}: Protein {abs(rem['p']):.0f}g über Ziel")
        if rem["kcal"] is not None and rem["kcal"] < -200:
            notes.append(f"{day['label']}: +{int(abs(rem['kcal']))} kcal über Ziel")
        if len(notes) >= 6:
            break
    return {
        "week_template": {
            "id": template_id,
            "title": week_row["title"],
            "revision": int(week_row["revision"] or 1),
            "macro_settings": macro_settings,
            "days": day_entries,
        },
        "day_groups": _group_payloads(cur, template_id),
        "grouping_contract": {
            "model": "linked_weekday_groups",
            "editing_rule": "Changes made through a day group apply atomically to every member weekday.",
            "topology_operations": ["link_weekdays_to_day_group", "detach_weekdays_from_day_group"],
            "safe_amount_operation": "update_day_group_item_amount",
        },
        "meal_ingredients_map": meal_ingredients_map,
        "week_summary": {
            "planned": _macro_to_dict(avg_planned),
            "targets": target_dict,
            "remaining": avg_remaining,
        },
        "gap_notes": notes,
    }


def update_week_template_title(template_id: Optional[int], title: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    ensure_nutrition_planning_schema()
    name = (title or "").strip()
    if not name:
        return None, "title_required"
    conn = get_nutrition_db()
    cur = conn.cursor()
    if template_id is None:
        row = cur.execute(
            "SELECT * FROM nutrition_week_templates WHERE is_active=1 ORDER BY updated_at DESC, id DESC LIMIT 1"
        ).fetchone()
        if not row:
            conn.close()
            return None, "not_found"
        template_id = row["id"]
    cur.execute(
        "UPDATE nutrition_week_templates SET title=?, updated_at=? WHERE id=?",
        (name, _utcnow_iso(), template_id),
    )
    conn.commit()
    updated = cur.execute("SELECT * FROM nutrition_week_templates WHERE id=?", (template_id,)).fetchone()
    conn.close()
    return (dict(updated) if updated else None), None


def get_week_template_macro_settings(template_id: Optional[int]) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    ensure_nutrition_planning_schema()
    conn = get_nutrition_db()
    cur = conn.cursor()
    if template_id is None:
        row = cur.execute(
            "SELECT * FROM nutrition_week_templates WHERE is_active=1 ORDER BY updated_at DESC, id DESC LIMIT 1"
        ).fetchone()
    else:
        row = cur.execute("SELECT * FROM nutrition_week_templates WHERE id=?", (template_id,)).fetchone()
    if not row:
        conn.close()
        return None, "not_found"
    settings = _get_week_template_macro_settings(cur, row)
    conn.close()
    return {
        "template_id": row["id"],
        "active_mode": settings.get("active_mode") or "maintenance",
        "modes": settings.get("modes") or {},
    }, None


def update_week_template_macro_settings(template_id: int, payload: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    ensure_nutrition_planning_schema()
    err = _validate_macro_settings_payload(payload or {})
    if err:
        return None, err
    conn = get_nutrition_db()
    cur = conn.cursor()
    row = cur.execute("SELECT * FROM nutrition_week_templates WHERE id=?", (template_id,)).fetchone()
    if not row:
        conn.close()
        return None, "not_found"
    existing = _get_week_template_macro_settings(cur, row)
    merged = _normalize_template_macro_settings(payload or {}, existing)
    active_targets = _active_targets_from_macro_settings(merged)
    now = _utcnow_iso()
    cur.execute(
        """
        UPDATE nutrition_week_templates
        SET macro_active_mode=?, macro_modes_json=?, updated_at=?
        WHERE id=?
        """,
        (
            merged.get("active_mode") or "maintenance",
            json.dumps({"active_mode": merged.get("active_mode"), "modes": merged.get("modes")}, ensure_ascii=False),
            now,
            template_id,
        ),
    )
    cur.execute(
        """
        UPDATE nutrition_week_template_days
        SET target_kcal=?, target_p=?, target_c=?, target_f=?, updated_at=?
        WHERE week_template_id=?
        """,
        (
            active_targets.get("kcal"),
            active_targets.get("p"),
            active_targets.get("c"),
            active_targets.get("f"),
            now,
            template_id,
        ),
    )
    if row["is_active"]:
        _record_week_template_timeline_snapshot(cur, template_id, effective_date=_timeline_date_from_timestamp(now))
    conn.commit()
    updated = cur.execute("SELECT * FROM nutrition_week_templates WHERE id=?", (template_id,)).fetchone()
    settings = _get_week_template_macro_settings(cur, updated)
    conn.close()
    return {
        "template_id": template_id,
        "active_mode": settings.get("active_mode") or "maintenance",
        "modes": settings.get("modes") or {},
    }, None


def create_week_day_slot(day_id: int, slot_template_id: int) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    ensure_nutrition_planning_schema()
    conn = get_nutrition_db()
    cur = conn.cursor()
    day_row = cur.execute("SELECT * FROM nutrition_week_template_days WHERE id=?", (day_id,)).fetchone()
    if not day_row:
        conn.close()
        return None, "day_not_found"
    template_row = cur.execute("SELECT * FROM nutrition_slot_templates WHERE id=?", (slot_template_id,)).fetchone()
    if not template_row:
        conn.close()
        return None, "slot_template_not_found"
    existing_rows = cur.execute(
        "SELECT slot_index FROM nutrition_week_day_slots WHERE week_template_day_id=? ORDER BY slot_index",
        (day_id,),
    ).fetchall()
    existing_indices = {int(row["slot_index"]) for row in existing_rows}
    next_index = 0
    while next_index in existing_indices:
        next_index += 1
    now = _utcnow_iso()
    cur.execute(
        """
        INSERT INTO nutrition_week_day_slots
            (week_template_day_id, slot_template_id, slot_index, active_option_id, locked, time_text, meal_template_id, created_at, updated_at)
        VALUES (?, ?, ?, NULL, 0, NULL, NULL, ?, ?)
        """,
        (day_id, slot_template_id, int(next_index), now, now),
    )
    slot_id = cur.lastrowid
    conn.commit()
    row = cur.execute("SELECT * FROM nutrition_week_day_slots WHERE id=?", (slot_id,)).fetchone()
    conn.close()
    return (dict(row) if row else None), None


def delete_week_day_slot(slot_id: int) -> Tuple[bool, Optional[str]]:
    ensure_nutrition_planning_schema()
    conn = get_nutrition_db()
    cur = conn.cursor()
    row = cur.execute("SELECT * FROM nutrition_week_day_slots WHERE id=?", (slot_id,)).fetchone()
    if not row:
        conn.close()
        return False, "not_found"
    cur.execute("DELETE FROM nutrition_week_day_slots WHERE id=?", (slot_id,))
    conn.commit()
    conn.close()
    return True, None


def get_slot_ingredients(slot_id: int) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    ensure_nutrition_planning_schema()
    conn = get_nutrition_db()
    cur = conn.cursor()
    slot = cur.execute("SELECT * FROM nutrition_week_day_slots WHERE id=?", (slot_id,)).fetchone()
    if not slot:
        conn.close()
        return None, "not_found"
    meal_id = slot["meal_template_id"]
    slot_meal = cur.execute(
        """
        SELECT * FROM nutrition_slot_meals
        WHERE week_template_day_id=? AND slot_index=?
        ORDER BY id DESC LIMIT 1
        """,
        (slot["week_template_day_id"], slot["slot_index"]),
    ).fetchone()
    slot_meal_id = slot_meal["id"] if slot_meal else None
    override_row = cur.execute(
        "SELECT * FROM nutrition_slot_meal_overrides WHERE slot_id=?",
        (slot_id,),
    ).fetchone()
    ingredients: List[Dict[str, Any]] = []
    is_override = False
    if slot_meal_id:
        item_rows = cur.execute(
            "SELECT DISTINCT * FROM nutrition_slot_meal_items WHERE slot_meal_id=? ORDER BY id",
            (slot_meal_id,),
        ).fetchall()
        if item_rows:
            if not isinstance(item_rows[0], dict):
                item_rows = [dict(row) for row in item_rows]
            food_ids = [int(row["food_id"]) for row in item_rows if row.get("food_id")]
            foods_by_id: Dict[int, Dict[str, Any]] = {}
            if food_ids:
                placeholders = ",".join("?" for _ in food_ids)
                food_rows = cur.execute(
                    f"SELECT id, name, unit_default, common_portion_size FROM nutrition_foods WHERE id IN ({placeholders})",
                    tuple(food_ids),
                ).fetchall()
                foods_by_id = {row["id"]: dict(row) for row in food_rows}
            seen = set()
            for row in item_rows:
                if row.get("item_type") == "kcal_only":
                    key = ("kcal", row.get("name_raw"), float(row.get("calories") or 0))
                    if key in seen:
                        continue
                    seen.add(key)
                    ingredients.append(
                        {
                            "id": row.get("id"),
                            "food_id": None,
                            "food_name": row.get("name_raw") or "Kcal",
                            "unit_default": None,
                            "common_portion_size": None,
                            "amount": row.get("calories") or 0,
                            "unit": "kcal",
                            "item_type": "kcal_only",
                            "kcal_value": row.get("calories") or 0,
                            "fingerprint": row.get("fingerprint"),
                        }
                    )
                    continue
                food = foods_by_id.get(int(row.get("food_id") or 0))
                if not food and row.get("name_raw"):
                    food = _resolve_food_row_for_import_label(cur, foods_by_id, row.get("name_raw"))
                key = (row.get("food_id"), row.get("amount") or 0, row.get("unit") or "g", row.get("name_raw") or "")
                if key in seen:
                    continue
                seen.add(key)
                ingredients.append(
                    {
                        "id": row.get("id"),
                        "food_id": food["id"] if food else None,
                        "food_name": food.get("name") if food else (row.get("name_raw") or "Unbekannt"),
                        "unit_default": food.get("unit_default") if food else None,
                        "common_portion_size": food.get("common_portion_size") if food else None,
                        "amount": row.get("amount") or 0,
                        "unit": row.get("unit") or "g",
                        "item_type": "food",
                        "fingerprint": row.get("fingerprint"),
                    }
                )
    elif override_row:
        is_override = True
        try:
            raw_items = json.loads(override_row["ingredients_json"] or "[]")
        except Exception:
            raw_items = []
        if raw_items:
            food_ids = [int(item.get("food_id") or 0) for item in raw_items if item.get("food_id")]
            foods_by_id: Dict[int, Dict[str, Any]] = {}
            if food_ids:
                placeholders = ",".join("?" for _ in food_ids)
                food_rows = cur.execute(
                    f"SELECT id, name, unit_default, common_portion_size FROM nutrition_foods WHERE id IN ({placeholders})",
                    tuple(food_ids),
                ).fetchall()
                foods_by_id = {row["id"]: dict(row) for row in food_rows}
            seen = set()
            for item in raw_items:
                kcal_value = item.get("kcal_value")
                if kcal_value is None and item.get("unit") == "kcal":
                    kcal_value = item.get("amount")
                if kcal_value is not None:
                    key = ("kcal", item.get("food_name") or item.get("raw"), float(item.get("amount") or kcal_value or 0))
                    if key in seen:
                        continue
                    seen.add(key)
                    ingredients.append(
                        {
                            "food_id": None,
                            "food_name": item.get("food_name") or item.get("raw") or "Kcal",
                            "unit_default": None,
                            "common_portion_size": None,
                            "amount": item.get("amount") or kcal_value,
                            "unit": "kcal",
                            "item_type": "kcal_only",
                            "kcal_value": item.get("amount") or kcal_value,
                        }
                    )
                    continue
                food = foods_by_id.get(int(item.get("food_id") or 0)) if item.get("food_id") else None
                if not food and (item.get("food_name") or item.get("raw")):
                    food = _resolve_food_row_for_import_label(cur, foods_by_id, item.get("food_name") or item.get("raw"))
                key = (food["id"] if food else None, item.get("amount") or 0, item.get("unit") or "g", item.get("food_name") or item.get("raw"))
                if key in seen:
                    continue
                seen.add(key)
                ingredients.append(
                    {
                        "food_id": food["id"] if food else None,
                        "food_name": food.get("name") if food else (item.get("food_name") or item.get("raw") or "Unbekannt"),
                        "unit_default": food.get("unit_default") if food else None,
                        "common_portion_size": food.get("common_portion_size") if food else None,
                        "amount": item.get("amount") or 0,
                        "unit": item.get("unit") or "g",
                        "item_type": "food",
                    }
                )
    elif meal_id:
        rows = cur.execute(
            """
            SELECT mi.amount, mi.unit, f.id AS food_id, f.name AS food_name, f.unit_default, f.common_portion_size
            FROM nutrition_meal_ingredients mi
            JOIN nutrition_foods f ON f.id=mi.food_id
            WHERE mi.meal_template_id=?
            ORDER BY mi.sort_index, mi.id
            """,
            (meal_id,),
        ).fetchall()
        ingredients = [dict(row) for row in rows]
    foods_by_id = {}
    if ingredients:
        ids = {int(item["food_id"]) for item in ingredients if item.get("food_id")}
        if ids:
            placeholders = ",".join("?" for _ in ids)
            rows = cur.execute(
                f"SELECT * FROM nutrition_foods WHERE id IN ({placeholders})",
                tuple(ids),
            ).fetchall()
            foods_by_id = {row["id"]: dict(row) for row in rows}
    macros = _compute_override_macros(ingredients, foods_by_id) if ingredients else MacroTotals(0.0, 0.0, 0.0, 0.0)
    unmatched = sum(1 for item in ingredients if item.get("item_type") != "kcal_only" and not item.get("food_id"))
    conn.close()
    return {
        "slot_id": slot_id,
        "slot_meal_id": slot_meal_id,
        "meal_template_id": meal_id,
        "ingredients": ingredients,
        "is_override": is_override,
        "macros": _macro_to_dict(macros),
        "unmatched_count": unmatched,
    }, None


def save_slot_override(slot_id: int, ingredients: List[Dict[str, Any]]) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    ensure_nutrition_planning_schema()
    conn = get_nutrition_db()
    cur = conn.cursor()
    slot = cur.execute("SELECT * FROM nutrition_week_day_slots WHERE id=?", (slot_id,)).fetchone()
    if not slot:
        conn.close()
        return None, "not_found"
    cleaned: List[Dict[str, Any]] = []
    kcal_items: List[Dict[str, Any]] = []
    for item in ingredients or []:
        unit_raw = (item.get("unit") or "").strip().lower()
        if item.get("item_type") == "kcal_only" or unit_raw == "kcal":
            kcal_value = _clamp_float(item.get("calories") or item.get("amount"), 0.0, None)
            if kcal_value is None:
                continue
            kcal_items.append(
                {
                    "amount": kcal_value,
                    "unit": "kcal",
                    "kcal_value": kcal_value,
                    "food_name": item.get("food_name") or item.get("name") or item.get("raw") or "Kcal",
                    "name_raw": item.get("food_name") or item.get("name") or item.get("raw") or "Kcal",
                    "client_req_id": item.get("client_req_id"),
                }
            )
            continue
        food_id = item.get("food_id")
        amount = _clamp_float(item.get("amount"), 0.0, None)
        unit = (item.get("unit") or "g").strip()
        if not food_id or amount is None:
            continue
        cleaned.append({
            "food_id": int(food_id),
            "amount": amount,
            "unit": unit,
            "name_raw": item.get("food_name") or item.get("name") or item.get("raw"),
            "client_req_id": item.get("client_req_id"),
        })
    now = _utcnow_iso()
    slot_meal = cur.execute(
        """
        SELECT * FROM nutrition_slot_meals
        WHERE week_template_day_id=? AND slot_index=?
        ORDER BY id DESC LIMIT 1
        """,
        (slot["week_template_day_id"], slot["slot_index"]),
    ).fetchone()
    if not slot_meal:
        template_row = cur.execute(
            "SELECT title FROM nutrition_slot_templates WHERE id=?",
            (slot["slot_template_id"],),
        ).fetchone()
        template_meal_row = None
        if slot["meal_template_id"]:
            template_meal_row = cur.execute(
                "SELECT title FROM nutrition_meal_templates WHERE id=?",
                (slot["meal_template_id"],),
            ).fetchone()
        title = (
            slot["custom_title"]
            or (template_meal_row["title"] if template_meal_row else None)
            or (template_row["title"] if template_row else "Meal")
        )
        cur.execute(
            """
            INSERT INTO nutrition_slot_meals
                (week_template_day_id, slot_index, meal_title, multiplier, template_id, created_at, updated_at)
            VALUES (?, ?, ?, 1, NULL, ?, ?)
            """,
            (slot["week_template_day_id"], slot["slot_index"], title, now, now),
        )
        slot_meal = {"id": cur.lastrowid}
    slot_meal_id = slot_meal["id"]
    cur.execute("DELETE FROM nutrition_slot_meal_items WHERE slot_meal_id=?", (slot_meal_id,))
    seen = set()
    for row in cleaned:
        fp = _fingerprint_item(
            name=row.get("name_raw"),
            amount=row.get("amount"),
            unit=row.get("unit"),
            food_id=row.get("food_id"),
            item_type="food",
            calories=None,
        )
        if fp in seen:
            continue
        seen.add(fp)
        cur.execute(
            """
            INSERT INTO nutrition_slot_meal_items
                (slot_meal_id, amount, unit, name_raw, food_id, item_type, calories, display_text, client_req_id, fingerprint, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, 'food', NULL, NULL, ?, ?, ?, ?)
            ON CONFLICT(slot_meal_id, fingerprint) DO UPDATE SET
                amount=excluded.amount,
                unit=excluded.unit,
                food_id=excluded.food_id,
                name_raw=excluded.name_raw,
                client_req_id=excluded.client_req_id,
                updated_at=excluded.updated_at
            """,
            (slot_meal_id, row["amount"], row["unit"], row.get("name_raw"), row["food_id"], row.get("client_req_id"), fp, now, now),
        )
    for row in kcal_items:
        fp = _fingerprint_item(
            name=row.get("name_raw"),
            amount=None,
            unit="kcal",
            food_id=None,
            item_type="kcal_only",
            calories=row.get("amount"),
        )
        if fp in seen:
            continue
        seen.add(fp)
        cur.execute(
            """
            INSERT INTO nutrition_slot_meal_items
                (slot_meal_id, amount, unit, name_raw, food_id, item_type, calories, display_text, client_req_id, fingerprint, created_at, updated_at)
            VALUES (?, NULL, NULL, ?, NULL, 'kcal_only', ?, NULL, ?, ?, ?, ?)
            ON CONFLICT(slot_meal_id, fingerprint) DO UPDATE SET
                calories=excluded.calories,
                name_raw=excluded.name_raw,
                client_req_id=excluded.client_req_id,
                updated_at=excluded.updated_at
            """,
            (slot_meal_id, row.get("name_raw"), row.get("amount"), row.get("client_req_id"), fp, now, now),
        )
    cur.execute("DELETE FROM nutrition_slot_meal_overrides WHERE slot_id=?", (slot_id,))
    conn.commit()
    conn.close()
    data, _ = get_slot_ingredients(slot_id)
    return {
        "slot_id": slot_id,
        "slot_meal_id": slot_meal_id,
        "ingredients": (data or {}).get("ingredients") or [],
        "is_override": False,
    }, None


def update_week_slot(slot_id: int, payload: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    ensure_nutrition_planning_schema()
    conn = get_nutrition_db()
    cur = conn.cursor()
    row = cur.execute("SELECT * FROM nutrition_week_day_slots WHERE id=?", (slot_id,)).fetchone()
    if not row:
        conn.close()
        return None, "not_found"
    if "meal_template_id" in payload:
        raw_value = payload.get("meal_template_id")
        if raw_value is None or raw_value == "":
            meal_template_id = None
        else:
            try:
                meal_template_id = int(raw_value)
            except Exception:
                meal_template_id = row["meal_template_id"]
    else:
        meal_template_id = row["meal_template_id"]
    if "locked" in payload:
        locked = 1 if payload.get("locked") else 0
    else:
        locked = row["locked"]
    if "time_text" in payload:
        time_text = (payload.get("time_text") or "").strip() or None
    else:
        time_text = row["time_text"] if "time_text" in row.keys() else None
    if "custom_title" in payload:
        custom_title = (payload.get("custom_title") or "").strip() or None
    else:
        custom_title = row["custom_title"] if "custom_title" in row.keys() else None
    if "note_text" in payload:
        note_text = (payload.get("note_text") or "").strip() or None
    else:
        note_text = row["note_text"] if "note_text" in row.keys() else None
    cur.execute(
        """
        UPDATE nutrition_week_day_slots
        SET meal_template_id=?, locked=?, time_text=?, custom_title=?, note_text=?, updated_at=?
        WHERE id=?
        """,
        (meal_template_id, locked, time_text, custom_title, note_text, _utcnow_iso(), slot_id),
    )
    if "custom_title" in payload:
        title_row = cur.execute(
            """SELECT COALESCE(NULLIF(TRIM(s.custom_title), ''), NULLIF(TRIM(mt.title), ''), NULLIF(TRIM(st.title), ''), 'Meal') AS title
               FROM nutrition_week_day_slots s
               LEFT JOIN nutrition_meal_templates mt ON mt.id=s.meal_template_id
               LEFT JOIN nutrition_slot_templates st ON st.id=s.slot_template_id
               WHERE s.id=?""",
            (slot_id,),
        ).fetchone()
        if title_row:
            cur.execute(
                """INSERT INTO nutrition_slot_meals
                       (week_template_day_id, slot_index, meal_title, multiplier, template_id, created_at, updated_at)
                   VALUES (?, ?, ?, 1, ?, ?, ?)
                   ON CONFLICT(week_template_day_id, slot_index) DO UPDATE SET
                       meal_title=excluded.meal_title,
                       updated_at=excluded.updated_at""",
                (
                    row["week_template_day_id"],
                    row["slot_index"],
                    title_row["title"],
                    meal_template_id,
                    _utcnow_iso(),
                    _utcnow_iso(),
                ),
            )
    _resequence_week_day_slots_chronologically(cur, int(row["week_template_day_id"]))
    conn.commit()
    updated = cur.execute("SELECT * FROM nutrition_week_day_slots WHERE id=?", (slot_id,)).fetchone()
    conn.close()
    return (dict(updated) if updated else None), None


def _resequence_week_day_slots_chronologically(cur, week_template_day_id: int) -> None:
    rows = cur.execute(
        """
        SELECT s.id, s.slot_index, COALESCE(NULLIF(TRIM(s.time_text), ''), NULLIF(TRIM(st.default_time), '')) AS effective_time
        FROM nutrition_week_day_slots s
        LEFT JOIN nutrition_slot_templates st ON st.id=s.slot_template_id
        WHERE s.week_template_day_id=?
        ORDER BY s.slot_index, s.id
        """,
        (week_template_day_id,),
    ).fetchall()

    def time_key(pair):
        original_index, slot = pair
        text = str(slot["effective_time"] or "").strip()
        try:
            hours_text, minutes_text = text.split(":", 1)
            hours = int(hours_text)
            minutes = int(minutes_text)
            if 0 <= hours <= 23 and 0 <= minutes <= 59:
                return (0, hours * 60 + minutes, original_index)
        except (TypeError, ValueError):
            pass
        return (1, 24 * 60, original_index)

    ordered = [slot for _, slot in sorted(enumerate(rows), key=time_key)]
    mapping = [(int(slot["id"]), int(slot["slot_index"]), new_index) for new_index, slot in enumerate(ordered)]
    if all(old_index == new_index for _, old_index, new_index in mapping):
        return
    offset = 10000
    now = _utcnow_iso()
    for slot_id_value, old_index, new_index in mapping:
        cur.execute("UPDATE nutrition_week_day_slots SET slot_index=?, updated_at=? WHERE id=?", (offset + new_index, now, slot_id_value))
        cur.execute(
            "UPDATE nutrition_slot_meals SET slot_index=?, updated_at=? WHERE week_template_day_id=? AND slot_index=?",
            (offset + new_index, now, week_template_day_id, old_index),
        )
    cur.execute("UPDATE nutrition_week_day_slots SET slot_index=slot_index-? WHERE week_template_day_id=? AND slot_index>=?", (offset, week_template_day_id, offset))
    cur.execute("UPDATE nutrition_slot_meals SET slot_index=slot_index-? WHERE week_template_day_id=? AND slot_index>=?", (offset, week_template_day_id, offset))


def clear_active_week_slots(template_id: Optional[int] = None) -> Tuple[bool, Optional[str], Dict[str, int]]:
    ensure_nutrition_planning_schema()
    conn = get_nutrition_db()
    cur = conn.cursor()
    week = None
    if template_id:
        week = cur.execute("SELECT id FROM nutrition_week_templates WHERE id=?", (template_id,)).fetchone()
    if not week:
        week = cur.execute(
            "SELECT id FROM nutrition_week_templates WHERE is_active=1 ORDER BY updated_at DESC, id DESC LIMIT 1"
        ).fetchone()
    if not week:
        conn.close()
        return False, "no_active_week_template", {}
    day_rows = cur.execute(
        "SELECT id FROM nutrition_week_template_days WHERE week_template_id=?",
        (week["id"],),
    ).fetchall()
    day_ids = [row["id"] for row in day_rows]
    if not day_ids:
        conn.close()
        return True, None, {"cleared_slots": 0, "cleared_items": 0}
    placeholders = ",".join("?" for _ in day_ids)
    slot_rows = cur.execute(
        f"SELECT id FROM nutrition_week_day_slots WHERE week_template_day_id IN ({placeholders})",
        tuple(day_ids),
    ).fetchall()
    slot_ids = [row["id"] for row in slot_rows]
    cleared_items = 0
    cleared_meals = 0
    slot_meal_ids: List[int] = []
    slot_meal_rows = cur.execute(
        f"SELECT id FROM nutrition_slot_meals WHERE week_template_day_id IN ({placeholders})",
        tuple(day_ids),
    ).fetchall()
    slot_meal_ids = [row["id"] for row in slot_meal_rows]
    if slot_meal_ids:
        meal_placeholders = ",".join("?" for _ in slot_meal_ids)
        cleared_items = cur.execute(
            f"SELECT COUNT(*) AS cnt FROM nutrition_slot_meal_items WHERE slot_meal_id IN ({meal_placeholders})",
            tuple(slot_meal_ids),
        ).fetchone()["cnt"]
        cur.execute(
            f"DELETE FROM nutrition_slot_meal_items WHERE slot_meal_id IN ({meal_placeholders})",
            tuple(slot_meal_ids),
        )
        cur.execute(
            f"DELETE FROM nutrition_slot_meals WHERE id IN ({meal_placeholders})",
            tuple(slot_meal_ids),
        )
        cleared_meals = len(slot_meal_ids)
    if slot_ids:
        slot_placeholders = ",".join("?" for _ in slot_ids)
        cur.execute(
            f"DELETE FROM nutrition_slot_meal_overrides WHERE slot_id IN ({slot_placeholders})",
            tuple(slot_ids),
        )
    cur.execute(
        f"""
        UPDATE nutrition_week_day_slots
        SET meal_template_id=NULL, time_text=NULL, custom_title=NULL, note_text=NULL, locked=0, updated_at=?
        WHERE week_template_day_id IN ({placeholders})
        """,
        (_utcnow_iso(), *tuple(day_ids)),
    )
    conn.commit()
    conn.close()
    return True, None, {
        "cleared_slots": len(slot_ids),
        "cleared_items": int(cleared_items or 0),
        "cleared_meals": int(cleared_meals or 0),
    }


def build_mfp_checklist(template_id: Optional[int] = None) -> Dict[str, Any]:
    payload = (
        get_active_week_plan_resolved() if template_id is None else _build_week_plan_payload_wrapper(template_id)
    )
    if not payload.get("week_template"):
        return {"ok": False, "error": "no_plan"}
    checklist = []
    days = payload["week_template"]["days"]
    day_groups = payload.get("day_groups") or []
    group_by_weekday = {
        int(weekday): group
        for group in day_groups
        for weekday in (group.get("weekdays") or [])
    }
    emitted_groups: set[str] = set()
    for raw_day in days:
        group = group_by_weekday.get(int(raw_day.get("weekday") or 0))
        if group:
            group_id = str(group.get("id") or "")
            if group_id in emitted_groups:
                continue
            emitted_groups.add(group_id)
            source_weekday = int(group.get("source_weekday") if group.get("source_weekday") is not None else raw_day.get("weekday") or 0)
            day = next((candidate for candidate in days if int(candidate.get("weekday") or 0) == source_weekday), raw_day)
            compact_label = " · ".join(
                candidate["label"] for candidate in days
                if int(candidate.get("weekday") or 0) in set(group.get("weekdays") or [])
            )
            day_group_meta = {
                "id": group_id,
                "name": group.get("name"),
                "color": group.get("color"),
                "weekdays": list(group.get("weekdays") or []),
                "source_weekday": source_weekday,
            }
        else:
            day = raw_day
            compact_label = day["label"]
            day_group_meta = None
        entries = []
        for slot in day["slots"]:
            meal = slot.get("meal_template")
            override = slot.get("override") or {}
            if not meal and not override.get("ingredients"):
                continue
            ingredients_out: List[Dict[str, Any]] = []
            if override.get("ingredients"):
                for ing in override.get("ingredients") or []:
                    ingredients_out.append(
                        {
                            "food_name": ing.get("food_name"),
                            "amount": ing.get("amount"),
                            "unit": ing.get("unit"),
                            "line": f"{_format_amount_human(ing.get('amount') or 0, ing.get('unit') or 'g')} {ing.get('food_name') or ''}".strip(),
                        }
                    )
            else:
                resolved = resolve_meal_template(meal["id"])
                for ing in (resolved or {}).get("ingredients", []):
                    ingredients_out.append(
                        {
                            "food_name": ing.get("food_name"),
                            "amount": ing.get("amount"),
                            "unit": ing.get("unit"),
                            "line": f"{_format_amount_human(ing.get('amount') or 0, ing.get('unit') or 'g')} {ing.get('food_name') or ''}".strip(),
                        }
                    )
            entries.append(
                {
                    "slot_title": slot.get("custom_title") or slot["slot_template"].get("title"),
                    "meal_title": slot.get("custom_title") or slot.get("display_title") or (meal.get("title") if meal else " + ".join(
                        [ing.get("food_name") for ing in ingredients_out if ing.get("food_name")]
                    ) or "Food"),
                    "mfp_alias": meal.get("mfp_alias") if meal else None,
                    "servings": slot.get("servings") or 1.0,
                    "export_text": override.get("export_text") or (meal.get("export_text") if meal else None),
                    "time_text": slot.get("time_text") or slot.get("slot_template", {}).get("default_time") or _default_time_for_slot_index(slot.get("slot_index") or 0),
                    "slot_index": slot.get("slot_index") or 0,
                    "ingredients": ingredients_out,
                }
            )
        checklist.append(
            {
                "weekday": day["weekday"],
                "label": compact_label,
                "day_group": day_group_meta,
                "slots": entries,
            }
        )
    return {"ok": True, "checklist": checklist}


def _build_week_plan_payload_wrapper(template_id: int) -> Dict[str, Any]:
    conn = get_nutrition_db()
    try:
        cur = conn.cursor()
        week_row = cur.execute("SELECT * FROM nutrition_week_templates WHERE id=?", (template_id,)).fetchone()
        if not week_row:
            return {"ok": False, "error": "template_not_found"}
        payload = _build_week_plan_payload(cur, week_row)
    finally:
        conn.close()
    if not payload.get("week_template"):
        return payload
    payload["slot_templates"] = list_slot_templates()
    payload["meal_templates"] = list_meal_templates(limit=400)
    payload["foods"] = list_foods(limit=500)
    payload["ok"] = True
    return payload


def build_shopping_list(template_id: Optional[int] = None) -> Dict[str, Any]:
    payload = (
        get_active_week_plan_resolved() if template_id is None else _build_week_plan_payload_wrapper(template_id)
    )
    if not payload.get("week_template"):
        return {"ok": False, "error": "no_plan"}
    food_totals: Dict[int, Dict[str, Any]] = {}
    for day in payload["week_template"]["days"]:
        for slot in day["slots"]:
            meal = slot.get("meal_template")
            override = slot.get("override") or {}
            if not meal and not override.get("ingredients"):
                continue
            multiplier = slot.get("servings") or 1.0
            ingredients = override.get("ingredients")
            if not ingredients:
                if not meal:
                    continue
                resolved = resolve_meal_template(meal["id"])
                if not resolved:
                    continue
                ingredients = resolved.get("ingredients", [])
            for ingredient in ingredients:
                grams = _resolve_grams(ingredient["amount"], ingredient["unit"], ingredient.get("common_portion_size"))
                total = grams * multiplier
                key = ingredient["food_id"]
                unit_default = _normalize_unit_import(ingredient.get("unit_default") or "g")
                common_portion = _clamp_float(ingredient.get("common_portion_size"), 0.0, None)
                entry = food_totals.setdefault(
                    key,
                    {
                        "food_id": key,
                        "name": ingredient.get("food_name"),
                        "unit_default": unit_default,
                        "common_portion_size": common_portion,
                        "mfp_hint": ingredient.get("mfp_search_hint"),
                        "total_grams": 0.0,
                        "total_pcs_raw": 0.0,
                        "total_ml_raw": 0.0,
                    },
                )
                if not entry.get("common_portion_size") and common_portion:
                    entry["common_portion_size"] = common_portion
                entry["total_grams"] += total
                raw_amount = (_clamp_float(ingredient.get("amount"), 0.0, None) or 0.0) * (multiplier or 1.0)
                raw_unit = _normalize_unit_import(ingredient.get("unit") or "g")
                if raw_unit == "pcs":
                    entry["total_pcs_raw"] += raw_amount
                elif raw_unit == "ml":
                    entry["total_ml_raw"] += raw_amount
    items = list(food_totals.values())
    for item in items:
        total_grams = _clamp_float(item.get("total_grams"), 0.0, None) or 0.0
        unit_default = _normalize_unit_import(item.get("unit_default") or "g")
        total_pcs_raw = _clamp_float(item.get("total_pcs_raw"), 0.0, None) or 0.0
        total_ml_raw = _clamp_float(item.get("total_ml_raw"), 0.0, None) or 0.0
        display_unit = "g"
        display_amount = total_grams
        if unit_default == "pcs":
            portion = _clamp_float(item.get("common_portion_size"), 0.0, None)
            if portion and portion > 0:
                display_unit = "pcs"
                display_amount = total_grams / portion
            elif total_pcs_raw > 0:
                display_unit = "pcs"
                display_amount = total_pcs_raw
        elif unit_default == "ml":
            display_unit = "ml"
            display_amount = total_ml_raw if total_ml_raw > 0 else total_grams
        else:
            # If items were entered as pieces and no reliable gram conversion exists, keep pcs in output.
            if total_pcs_raw > 0 and not _clamp_float(item.get("common_portion_size"), 0.0, None):
                display_unit = "pcs"
                display_amount = total_pcs_raw
        if display_unit == "pcs":
            display_text = f"{int(round(display_amount))} pcs {item.get('name') or ''}".strip()
        else:
            display_text = f"{int(round(display_amount))} {display_unit} {item.get('name') or ''}".strip()
        item["display_unit"] = display_unit
        item["display_amount"] = display_amount
        item["display_text"] = display_text
    items.sort(key=lambda x: -x["total_grams"])
    return {"ok": True, "shopping_list": items}


# === Mealplan Import Helpers ===

def get_active_week_template_id() -> Optional[int]:
    ensure_nutrition_planning_schema()
    conn = get_nutrition_db()
    cur = conn.cursor()
    row = cur.execute(
        "SELECT id FROM nutrition_week_templates WHERE is_active=1 ORDER BY updated_at DESC, id DESC LIMIT 1"
    ).fetchone()
    conn.close()
    return row["id"] if row else None


def _normalize_import_label(value: Optional[str]) -> str:
    if not value:
        return ""
    text = value.strip().lower()
    text = re.sub(r"[&/+]", " ", text)
    text = re.sub(r"\(.*?\)", " ", text)
    text = re.sub(r"[^\w\s]", " ", text, flags=re.UNICODE)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _normalize_item_name(value: Optional[str]) -> str:
    if not value:
        return ""
    text = value.strip().lower()
    text = re.sub(r"\(.*?\)", " ", text)
    text = re.sub(r"[&/+]", " ", text)
    text = re.sub(r"[^\w\s]", " ", text, flags=re.UNICODE)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _fingerprint_item(*, name: Optional[str], amount: Optional[float], unit: Optional[str], food_id: Optional[int], item_type: str, calories: Optional[float]) -> str:
    normalized_name = _normalize_item_name(name)
    normalized_unit = _normalize_unit_import(unit)
    if item_type == "kcal_only":
        cal_text = _format_amount_signature(calories)
        payload = f"kcal|{normalized_name}|{cal_text}"
    else:
        amount_text = _format_amount_signature(amount)
        payload = f"food|{normalized_name}|{amount_text}|{normalized_unit}|{int(food_id or 0)}"
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


def _match_import_food(cur, label: Optional[str]) -> Tuple[Optional[int], List[Dict[str, Any]]]:
    normalized = _normalize_import_label(label)
    if not normalized:
        return None, []
    tokens = normalized.split()
    seed = tokens[0] if tokens else normalized
    like_term = f"%{seed}%"
    rows = cur.execute(
        "SELECT id, name, is_favorite FROM nutrition_foods WHERE is_active=1 AND LOWER(name) LIKE ? ORDER BY is_favorite DESC, name ASC LIMIT 200",
        (like_term,),
    ).fetchall()
    if rows:
        exact_norm = [row for row in rows if _normalize_import_label(row["name"]) == normalized]
        if len(exact_norm) == 1:
            return exact_norm[0]["id"], []
        candidates = [{"food_id": row["id"], "label": row["name"]} for row in rows[:8]]
        return None, candidates
    rows = cur.execute(
        "SELECT id, name FROM nutrition_foods WHERE is_active=1 ORDER BY is_favorite DESC, name ASC LIMIT 8"
    ).fetchall()
    candidates = [{"food_id": row["id"], "label": row["name"]} for row in rows]
    return None, candidates


def _resolve_food_row_for_import_label(cur, foods_by_id: Dict[int, Dict[str, Any]], label: Optional[str]) -> Optional[Dict[str, Any]]:
    food_id, _candidates = _match_import_food(cur, label)
    if not food_id:
        return None
    food = foods_by_id.get(int(food_id))
    if food:
        return food
    row = cur.execute(
        "SELECT * FROM nutrition_foods WHERE id=?",
        (int(food_id),),
    ).fetchone()
    if not row:
        return None
    food = dict(row)
    foods_by_id[int(food_id)] = food
    return food


def _get_or_create_slot_template_id(cur, title: str) -> int:
    existing = cur.execute(
        "SELECT id FROM nutrition_slot_templates WHERE title=?",
        (title,),
    ).fetchone()
    if existing:
        return existing["id"]
    now = _utcnow_iso()
    cur.execute(
        """
        INSERT INTO nutrition_slot_templates (title, default_time, context_tag, macro_intent, created_at, updated_at)
        VALUES (?, NULL, ?, NULL, ?, ?)
        """,
        (title, "meal", now, now),
    )
    return cur.lastrowid


def _normalize_unit_import(value: Optional[str]) -> str:
    unit = (value or "g").strip().lower()
    if unit in ("pcs", "piece", "pieces", "stk", "stck", "stück", "stueck"):
        return "pcs"
    if unit in ("gram", "grams"):
        return "g"
    if unit in ("milliliter", "millilitre"):
        return "ml"
    if unit in ("kcal", "cal"):
        return "kcal"
    return unit


def _format_amount_signature(value: Optional[float]) -> str:
    amount = _clamp_float(value, 0.0, None) if value is not None else None
    if amount is None:
        return "0"
    text = f"{amount:.3f}".rstrip("0").rstrip(".")
    return text or "0"


def _meal_signature(title: str, item_signatures: List[str]) -> str:
    base = _normalize_import_label(title)
    items = "|".join(item_signatures)
    return f"{base}||{items}"


DEFAULT_SLOT_TIMES = ["06:40", "09:30", "14:00", "16:00", "18:30", "20:30", "21:30"]


def _default_time_for_slot_index(slot_index: int) -> str:
    if slot_index < 0:
        return DEFAULT_SLOT_TIMES[0]
    if slot_index < len(DEFAULT_SLOT_TIMES):
        return DEFAULT_SLOT_TIMES[slot_index]
    return DEFAULT_SLOT_TIMES[-1]


def _ensure_pauschal_food(cur) -> int:
    name = "Abendessen (Pauschale)"
    name_norm = _normalize_item_name(name)
    row = cur.execute(
        "SELECT id FROM nutrition_foods WHERE name_normalized=? LIMIT 1",
        (name_norm,),
    ).fetchone()
    if row:
        return int(row["id"])
    now = _utcnow_iso()
    cur.execute(
        """
        INSERT INTO nutrition_foods
            (name, brand, unit_default, portion_g, common_portion_size, mfp_search_hint, category,
             kcal_per_100, p_per_100, c_per_100, f_per_100, sugar_per_100, salt_per_100, tags, is_favorite,
             name_normalized, created_at, updated_at)
        VALUES (?, NULL, 'pcs', 1, 1, NULL, NULL, ?, ?, ?, ?, NULL, NULL, NULL, 0, ?, ?, ?)
        """,
        (name, 750.0, 25.0, 95.0, 30.0, name_norm, now, now),
    )
    return int(cur.lastrowid)


def _build_meal_signature(title: str, ingredients: List[Dict[str, Any]]) -> str:
    item_signatures: List[str] = []
    for ing in ingredients or []:
        unit = _normalize_unit_import(ing.get("unit"))
        if ing.get("item_type") == "kcal_only" or unit == "kcal":
            kcal_value = ing.get("kcal_value") if "kcal_value" in ing else ing.get("amount")
            item_signatures.append(f"kcal:{_format_amount_signature(kcal_value)}")
            continue
        food_id = ing.get("food_id")
        amount = ing.get("amount")
        if food_id:
            item_signatures.append(f"{int(food_id)}:{_format_amount_signature(amount)}:{unit}")
        else:
            name = ing.get("food_name") or ing.get("name") or ing.get("raw")
            item_signatures.append(f"{_normalize_import_label(name)}:{_format_amount_signature(amount)}:{unit}")
    return _meal_signature(title, item_signatures)


def import_mealplan_text(
    text: str,
    template_id: int,
    *,
    strict: bool = False,
    scope_slots: bool = True,
    scope_templates: bool = True,
    apply: bool = True,
) -> Dict[str, Any]:
    ensure_nutrition_planning_schema()
    parsed = parse_mealplan_text(text, strict=strict)
    issues = [dict(issue) for issue in parsed.get("issues", [])]
    max_slot_overall = int(parsed.get("max_slot_overall") or 0)
    if not parsed.get("days"):
        issues.append({"line": None, "message": "Keine Tage gefunden", "raw": "", "severity": "error"})
        return {"ok": False, "errors": issues}
    conn = get_nutrition_db()
    cur = conn.cursor()
    def _begin_import_tx():
        if conn.in_transaction:
            conn.execute("SAVEPOINT import_tx")
            return "savepoint"
        conn.execute("BEGIN")
        return "begin"

    def _commit_import_tx(mode: str):
        if mode == "savepoint":
            conn.execute("RELEASE import_tx")
            conn.commit()
        else:
            conn.commit()

    def _rollback_import_tx(mode: str):
        if mode == "savepoint":
            conn.execute("ROLLBACK TO import_tx")
            conn.execute("RELEASE import_tx")
            conn.rollback()
        else:
            conn.rollback()
    template_row = cur.execute("SELECT * FROM nutrition_week_templates WHERE id=?", (template_id,)).fetchone()
    if not template_row:
        conn.close()
        issues.append({"line": None, "message": "Template nicht gefunden", "raw": "", "severity": "error"})
        return {"ok": False, "errors": issues}
    day_rows = [dict(row) for row in cur.execute(
        "SELECT * FROM nutrition_week_template_days WHERE week_template_id=? ORDER BY weekday",
        (template_id,),
    ).fetchall()]
    if not day_rows:
        conn.close()
        issues.append({"line": None, "message": "Keine Tage im Template", "raw": "", "severity": "error"})
        return {"ok": False, "errors": issues}
    import_id = uuid4().hex
    unmatched = []
    pending_updates = []
    stats = {
        "created_templates": 0,
        "reused_templates": 0,
        "created_instances": 0,
        "unmatched_count": 0,
        "kcal_only_count": 0,
        "total_days": 0,
        "total_slots_filled": 0,
        "items_total": 0,
    }
    summary_days: Dict[str, Dict[str, int]] = {}
    total_meals = 0
    total_items = 0
    slots_written_by_day: Dict[str, List[int]] = defaultdict(list)
    for day in parsed["days"]:
        weekday = day.get("weekday")
        label = day.get("label") or WEEKDAY_LABELS[weekday] if 0 <= weekday < len(WEEKDAY_LABELS) else "?"
        meals = day.get("meals") or []
        summary_days[label] = {
            "meals": len(meals),
            "items": sum(len(meal.get("items") or []) for meal in meals),
        }
        total_meals += len(meals)
        total_items += summary_days[label]["items"]
        stats["total_slots_filled"] += len(meals)
        for meal in meals:
            slot_index = max(0, meal.get("slot", 1) - 1)
            ingredients = []
            raw_items = []
            kcal_only_total = 0.0
            item_signatures = []
            food_line_count = 0
            item_index = 0
            for item in meal.get("items") or []:
                item_index += 1
                parsed_item = item.get("parsed")
                name = (parsed_item.get("name") if parsed_item else item.get("raw")) or item.get("raw")
                amount = parsed_item.get("amount") if parsed_item else None
                unit = _normalize_unit_import(parsed_item.get("unit") if parsed_item else None)
                is_kcal_only = parsed_item is not None and unit == "kcal"
                if is_kcal_only:
                    kcal_value = _clamp_float(amount, 0.0, None) or 0.0
                    normalized_name = _normalize_import_label(name)
                    if normalized_name and "abendessen" in normalized_name:
                        food_id = _ensure_pauschal_food(cur)
                        scale = kcal_value / 750.0 if kcal_value > 0 else 1.0
                        amount_pcs = max(scale, 0.1)
                        food_line_count += 1
                        item_signatures.append(f"pcs:{_normalize_import_label(name)}")
                        ingredients.append({
                            "food_id": food_id,
                            "food_name": "Abendessen (Pauschale)",
                            "unit": "pcs",
                            "amount": amount_pcs,
                            "raw": item.get("raw"),
                            "parsed": parsed_item,
                            "import_key": f"{import_id}:{label}:{meal.get('slot')}:{item_index}",
                            "line": item.get("line"),
                        })
                        continue
                    kcal_only_total += kcal_value
                    stats["kcal_only_count"] += 1
                    raw_items.append({
                        "raw_name": name,
                        "amount": amount,
                        "unit": unit,
                        "kcal_value": kcal_value,
                    })
                    item_signatures.append(f"kcal:{_format_amount_signature(kcal_value)}")
                    ingredients.append({
                        "food_id": None,
                        "food_name": name,
                        "unit": unit or "kcal",
                        "amount": amount,
                        "raw": item.get("raw"),
                        "parsed": parsed_item,
                        "import_key": f"{import_id}:{label}:{meal.get('slot')}:{item_index}",
                        "line": item.get("line"),
                        "kcal_value": kcal_value,
                    })
                    continue
                food_line_count += 1
                food_id, candidates = _match_import_food(cur, name)
                import_key = f"{import_id}:{label}:{meal.get('slot')}:{item_index}"
                if parsed_item:
                    item_sig = f"{unit}:{_normalize_import_label(name)}"
                else:
                    item_sig = f"raw:{_normalize_import_label(item.get('raw'))}"
                item_signatures.append(item_sig)
                ingredients.append({
                    "food_id": food_id,
                    "food_name": name,
                    "unit": unit or "g",
                    "amount": amount,
                    "raw": item.get("raw"),
                    "parsed": parsed_item,
                    "import_key": import_key,
                    "line": item.get("line"),
                })
                if food_id is None:
                    stats["unmatched_count"] += 1
                    raw_items.append({
                        "raw_name": name,
                        "amount": amount,
                        "unit": unit,
                        "kcal_value": None,
                    })
                    unmatched.append({
                        "day": label,
                        "meal_slot": meal.get("slot"),
                        "meal_title": meal.get("title"),
                        "raw": item.get("raw"),
                        "parsed": parsed_item,
                        "candidates": candidates,
                        "line": item.get("line"),
                        "unmatched_key": import_key,
                    })
            pending_updates.append({
                "weekday": weekday,
                "label": label,
                "slot_index": slot_index,
                "line": meal.get("line"),
                "title": meal.get("title"),
                "servings": meal.get("servings") or 1.0,
                "signature": _meal_signature(meal.get("title") or "", item_signatures),
                "ingredients": ingredients,
                "raw_items": raw_items,
                "kcal_only_total": kcal_only_total,
                "food_line_count": food_line_count,
                "time_text": meal.get("time_text"),
            })
            slots_written_by_day[label].append(meal.get("slot") or (slot_index + 1))
    stats["items_total"] = total_items
    stats["total_days"] = len(summary_days)
    if total_meals == 0:
        issues.append({"line": None, "message": "Keine Mahlzeiten gefunden", "raw": "", "severity": "error"})
    if not scope_slots and not scope_templates:
        issues.append({"line": None, "message": "Wähle mindestens eine Import-Option", "raw": "", "severity": "error"})
    if stats["unmatched_count"] > 0:
        issues.append({
            "line": None,
            "message": f"Makros unvollständig: {stats['unmatched_count']} Zutaten nicht zugeordnet",
            "raw": "",
            "source": "import",
            "severity": "warning",
        })
    weekday_by_day_id = {day.get("id"): day.get("weekday") for day in day_rows if day.get("id") is not None}
    slots = [dict(row) for row in cur.execute(
        "SELECT * FROM nutrition_week_day_slots WHERE week_template_day_id IN ({}) ORDER BY week_template_day_id, slot_index".format(
            ",".join("?" for _ in day_rows)
        ),
        tuple(day["id"] for day in day_rows),
    ).fetchall()]
    slots_by_weekday: Dict[int, Dict[int, Dict[str, Any]]] = defaultdict(dict)
    for slot in slots:
        weekday = weekday_by_day_id.get(slot["week_template_day_id"])
        if weekday is None:
            continue
        slots_by_weekday[weekday][slot["slot_index"]] = slot
    required_max_index = max_slot_overall - 1 if max_slot_overall > 0 else -1
    extended_any = False
    before_max = max(
        (max(slots_by_weekday[weekday].keys()) if slots_by_weekday.get(weekday) else -1)
        for weekday in (day.get("weekday") for day in day_rows if day.get("weekday") is not None)
    )
    if strict:
        for issue in issues:
            if issue.get("severity") == "warning" and issue.get("source") == "parse":
                issue["severity"] = "error"
    if any(issue.get("severity") == "error" for issue in issues):
        conn.close()
        return {"ok": False, "errors": issues}
    if apply and scope_templates and not scope_slots:
        now = _utcnow_iso()
        try:
            tx_mode = _begin_import_tx()
            signature_cache: Dict[str, int] = {}
            existing_sigs = cur.execute(
                """
                SELECT id, signature FROM nutrition_meal_templates
                WHERE signature IS NOT NULL AND signature != ''
                """
            ).fetchall()
            for row in existing_sigs:
                row_data = dict(row)
                if row_data.get("signature"):
                    signature_cache[row_data["signature"]] = row_data.get("id")
            for entry in pending_updates:
                title = (entry.get("title") or "Meal").strip()
                servings = float(entry.get("servings") or 1.0)
                signature = (entry.get("signature") or "").strip() or _meal_signature(title, [])
                if signature in signature_cache:
                    stats["reused_templates"] += 1
                    continue
                cur.execute(
                    """
                    INSERT INTO nutrition_meal_templates
                        (title, description, tags, default_servings, mfp_alias, export_text, notes,
                         kcal_per_serving, p_per_serving, c_per_serving, f_per_serving, signature, created_at, updated_at)
                    VALUES (?, NULL, NULL, ?, NULL, NULL, NULL, NULL, NULL, NULL, NULL, ?, ?, ?)
                    """,
                    (title, servings, signature, now, now),
                )
                meal_template_id = cur.lastrowid
                signature_cache[signature] = meal_template_id
                stats["created_templates"] += 1
                sort_index = 0
                for ing in entry["ingredients"]:
                    food_id = ing.get("food_id")
                    if not food_id:
                        continue
                    amount = ing.get("amount") or 0
                    unit = ing.get("unit") or "g"
                    cur.execute(
                        """
                        INSERT INTO nutrition_meal_ingredients
                            (meal_template_id, food_id, amount, unit, sort_index, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (meal_template_id, int(food_id), amount, unit, sort_index, now, now),
                    )
                    sort_index += 1
                for raw in entry.get("raw_items") or []:
                    cur.execute(
                        """
                        INSERT INTO nutrition_meal_ingredient_raw
                            (meal_template_id, raw_name, amount, unit, kcal_value, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            meal_template_id,
                            raw.get("raw_name"),
                            raw.get("amount"),
                            raw.get("unit"),
                            raw.get("kcal_value"),
                            now,
                            now,
                        ),
                    )
            _commit_import_tx(tx_mode)
        except Exception as exc:
            _rollback_import_tx(tx_mode)
            conn.close()
            return {
                "ok": False,
                "errors": issues,
                "write_errors": [
                    {"message": "DB write fehlgeschlagen", "detail": str(exc)}
                ],
            }
        conn.close()
        return {
            "ok": True,
            "template_id": template_id,
            "import_id": import_id,
            "summary": {
                "days": summary_days,
                "total_meals": total_meals,
                "total_items": total_items,
            },
            "unmatched": unmatched,
            "stats": stats,
            "errors": issues,
            "write_errors": [],
        }
    if apply and scope_slots:
        now = _utcnow_iso()
        try:
            tx_mode = _begin_import_tx()
            # Clear existing slot meals/items for overwrite
            day_ids = [day["id"] for day in day_rows]
            if day_ids:
                placeholders = ",".join("?" for _ in day_ids)
                cur.execute(
                    f"DELETE FROM nutrition_slot_meal_items WHERE slot_meal_id IN (SELECT id FROM nutrition_slot_meals WHERE week_template_day_id IN ({placeholders}))",
                    tuple(day_ids),
                )
                cur.execute(
                    f"DELETE FROM nutrition_slot_meals WHERE week_template_day_id IN ({placeholders})",
                    tuple(day_ids),
                )
            if required_max_index >= 0:
                for day in day_rows:
                    weekday = day.get("weekday")
                    if weekday is None:
                        continue
                    current = slots_by_weekday.get(weekday) or {}
                    day_max = max(current.keys()) if current else -1
                    if required_max_index > day_max:
                        extended_any = True
                    for slot_index in range(day_max + 1, required_max_index + 1):
                        title = f"Meal {slot_index + 1}"
                        slot_template_id = _get_or_create_slot_template_id(cur, title)
                        default_time = _default_time_for_slot_index(slot_index)
                        cur.execute(
                            """
                            INSERT INTO nutrition_week_day_slots
                                (week_template_day_id, slot_template_id, slot_index, active_option_id, locked, time_text, custom_title, note_text, meal_template_id, created_at, updated_at)
                            VALUES (?, ?, ?, NULL, 0, ?, NULL, NULL, NULL, ?, ?)
                            """,
                            (day["id"], slot_template_id, slot_index, default_time, now, now),
                        )
                slots = [dict(row) for row in cur.execute(
                    "SELECT * FROM nutrition_week_day_slots WHERE week_template_day_id IN ({}) ORDER BY week_template_day_id, slot_index".format(
                        ",".join("?" for _ in day_rows)
                    ),
                    tuple(day["id"] for day in day_rows),
                ).fetchall()]
                slots_by_weekday = defaultdict(dict)
                for slot in slots:
                    weekday = weekday_by_day_id.get(slot["week_template_day_id"])
                    if weekday is None:
                        continue
                    slots_by_weekday[weekday][slot["slot_index"]] = slot
            if extended_any:
                after_max = max(
                    (max(slots_by_weekday[weekday].keys()) if slots_by_weekday.get(weekday) else -1)
                    for weekday in (day.get("weekday") for day in day_rows if day.get("weekday") is not None)
                )
                issues.append({
                    "line": None,
                    "message": f"Slots automatisch erweitert: {before_max + 1} → {after_max + 1}",
                    "raw": "",
                    "source": "import",
                    "severity": "warning",
                })
            slot_updates = []
            day_id_by_weekday = {day.get("weekday"): day.get("id") for day in day_rows}
            for entry in pending_updates:
                weekday = entry["weekday"]
                day_id = day_id_by_weekday.get(weekday)
                if day_id is None:
                    issues.append({
                        "line": entry.get("line"),
                        "message": f"Kein Tag für {entry.get('label') or '?'}",
                        "raw": entry.get("title") or "",
                        "severity": "error",
                    })
                    continue
                available = slots_by_weekday.get(weekday)
                if not available:
                    available = {}
                    slots_by_weekday[weekday] = available
                slot_row = available.get(entry["slot_index"])
                if not slot_row:
                    slot_template_id = _get_or_create_slot_template_id(cur, f"Meal {entry.get('slot_index', 0) + 1}")
                    default_time = _default_time_for_slot_index(entry.get("slot_index") or 0)
                    cur.execute(
                        """
                        INSERT INTO nutrition_week_day_slots
                            (week_template_day_id, slot_template_id, slot_index, active_option_id, locked, time_text, custom_title, note_text, meal_template_id, created_at, updated_at)
                        VALUES (?, ?, ?, NULL, 0, ?, NULL, NULL, NULL, ?, ?)
                        """,
                        (day_id, slot_template_id, entry["slot_index"], default_time, now, now),
                    )
                    slot_row = {"id": cur.lastrowid, "slot_index": entry["slot_index"], "meal_template_id": None}
                    available[entry["slot_index"]] = slot_row
                slot_updates.append({
                    "slot_id": slot_row["id"],
                    "weekday": entry["weekday"],
                    "slot_index": entry["slot_index"],
                    "meal_template_id": slot_row.get("meal_template_id") or 0,
                    "ingredients": entry["ingredients"],
                    "title": entry.get("title") or f"Meal {entry.get('slot_index', 0) + 1}",
                    "servings": entry.get("servings") or 1.0,
                    "signature": entry.get("signature") or "",
                    "raw_items": entry.get("raw_items") or [],
                    "kcal_only_total": entry.get("kcal_only_total") or 0.0,
                    "food_line_count": entry.get("food_line_count") or 0,
                    "time_text": entry.get("time_text") or slot_row.get("time_text") or _default_time_for_slot_index(entry.get("slot_index") or 0),
                })
            if any(issue.get("severity") == "error" for issue in issues):
                conn.rollback()
                conn.close()
                return {"ok": False, "errors": issues}
            if slot_updates:
                slot_ids = list({entry["slot_id"] for entry in slot_updates})
                placeholders = ",".join("?" for _ in slot_ids)
                cur.execute(
                    f"UPDATE nutrition_week_day_slots SET meal_template_id=NULL, updated_at=? WHERE week_template_day_id IN ({','.join('?' for _ in day_rows)})",
                    (now, *tuple(day["id"] for day in day_rows)),
                )
                cur.execute(
                    f"DELETE FROM nutrition_slot_meal_overrides WHERE slot_id IN ({placeholders})",
                    tuple(slot_ids),
                )
                signature_cache: Dict[str, int] = {}
                existing_sigs = cur.execute(
                    """
                    SELECT id, signature FROM nutrition_meal_templates
                    WHERE signature IS NOT NULL AND signature != ''
                    """
                ).fetchall()
                for row in existing_sigs:
                    row_data = dict(row)
                    if row_data.get("signature"):
                        signature_cache[row_data["signature"]] = row_data.get("id")
                for entry in slot_updates:
                    title = (entry.get("title") or "Meal").strip()
                    servings = float(entry.get("servings") or 1.0)
                    signature = (entry.get("signature") or "").strip()
                    if not signature:
                        signature = _meal_signature(title, [])
                    meal_template_id = None
                    if scope_templates and (entry.get("food_line_count") or 0) > 1:
                        meal_template_id = signature_cache.get(signature)
                        if meal_template_id:
                            stats["reused_templates"] += 1
                        else:
                            cur.execute(
                                """
                                INSERT INTO nutrition_meal_templates
                                    (title, description, tags, default_servings, mfp_alias, export_text, notes,
                                     kcal_per_serving, p_per_serving, c_per_serving, f_per_serving, signature, created_at, updated_at)
                                VALUES (?, NULL, NULL, ?, NULL, NULL, NULL, NULL, NULL, NULL, NULL, ?, ?, ?)
                                """,
                                (title, servings, signature, now, now),
                            )
                            meal_template_id = cur.lastrowid
                            signature_cache[signature] = meal_template_id
                            stats["created_templates"] += 1
                            sort_index = 0
                            for ing in entry["ingredients"]:
                                food_id = ing.get("food_id")
                                if not food_id:
                                    continue
                                amount = ing.get("amount") or 0
                                unit = ing.get("unit") or "g"
                                cur.execute(
                                    """
                                    INSERT INTO nutrition_meal_ingredients
                                        (meal_template_id, food_id, amount, unit, sort_index, created_at, updated_at)
                                    VALUES (?, ?, ?, ?, ?, ?, ?)
                                    """,
                                    (meal_template_id, int(food_id), amount, unit, sort_index, now, now),
                                )
                                sort_index += 1
                            for raw in entry.get("raw_items") or []:
                                cur.execute(
                                    """
                                    INSERT INTO nutrition_meal_ingredient_raw
                                        (meal_template_id, raw_name, amount, unit, kcal_value, created_at, updated_at)
                                    VALUES (?, ?, ?, ?, ?, ?, ?)
                                    """,
                                    (
                                        meal_template_id,
                                        raw.get("raw_name"),
                                        raw.get("amount"),
                                        raw.get("unit"),
                                        raw.get("kcal_value"),
                                        now,
                                        now,
                                    ),
                                )
                            macros = MacroTotals(0.0, 0.0, 0.0, 0.0)
                            rows = cur.execute(
                                """
                                SELECT mi.amount, mi.unit,
                                       f.kcal_per_100, f.p_per_100, f.c_per_100, f.f_per_100,
                                       f.common_portion_size, f.unit_default, f.portion_g
                                FROM nutrition_meal_ingredients mi
                                JOIN nutrition_foods f ON f.id=mi.food_id
                                WHERE mi.meal_template_id=?
                                ORDER BY mi.sort_index, mi.id
                                """,
                                (meal_template_id,),
                            ).fetchall()
                            if rows:
                                macros = _compute_ingredient_macros(rows)
                            if entry.get("kcal_only_total"):
                                macros = MacroTotals(
                                    kcal=macros.kcal + float(entry.get("kcal_only_total") or 0.0),
                                    p=macros.p,
                                    c=macros.c,
                                    f=macros.f,
                                )
                            cur.execute(
                                """
                                UPDATE nutrition_meal_templates
                                SET kcal_per_serving=?, p_per_serving=?, c_per_serving=?, f_per_serving=?, updated_at=?
                                WHERE id=?
                                """,
                                (macros.kcal, macros.p, macros.c, macros.f, now, meal_template_id),
                            )
                    # Persist slot_meal + items (UPSERT)
                    day_id = day_id_by_weekday.get(entry["weekday"])
                    cur.execute(
                        """
                        INSERT INTO nutrition_slot_meals
                            (week_template_day_id, slot_index, meal_title, multiplier, template_id, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(week_template_day_id, slot_index) DO UPDATE SET
                            meal_title=excluded.meal_title,
                            multiplier=excluded.multiplier,
                            template_id=excluded.template_id,
                            updated_at=excluded.updated_at
                        """,
                        (
                            day_id,
                            entry["slot_index"],
                            title,
                            servings,
                            meal_template_id,
                            now,
                            now,
                        ),
                    )
                    slot_meal_id = cur.execute(
                        "SELECT id FROM nutrition_slot_meals WHERE week_template_day_id=? AND slot_index=?",
                        (day_id, entry["slot_index"]),
                    ).fetchone()["id"]
                    cur.execute(
                        "DELETE FROM nutrition_slot_meal_items WHERE slot_meal_id=?",
                        (slot_meal_id,),
                    )
                    for idx_item, item in enumerate(entry["ingredients"], start=1):
                        if item.get("kcal_value") is not None or item.get("unit") == "kcal":
                            fingerprint = _fingerprint_item(
                                name=item.get("food_name") or item.get("raw"),
                                amount=None,
                                unit="kcal",
                                food_id=None,
                                item_type="kcal_only",
                                calories=item.get("kcal_value") or item.get("amount"),
                            )
                            req_payload = f"{import_id}|{entry.get('label')}|{entry.get('slot_index')}|{idx_item}|kcal|{item.get('kcal_value') or item.get('amount')}"
                            client_req_id = hashlib.sha1(req_payload.encode("utf-8")).hexdigest()
                            cur.execute(
                                """
                                INSERT INTO nutrition_slot_meal_items
                                    (slot_meal_id, amount, unit, name_raw, food_id, item_type, calories, display_text, client_req_id, fingerprint, created_at, updated_at)
                                VALUES (?, NULL, NULL, ?, NULL, 'kcal_only', ?, NULL, ?, ?, ?, ?)
                                ON CONFLICT(slot_meal_id, fingerprint) DO UPDATE SET
                                    calories=excluded.calories,
                                    name_raw=excluded.name_raw,
                                    client_req_id=excluded.client_req_id,
                                    updated_at=excluded.updated_at
                                """,
                                (
                                    slot_meal_id,
                                    item.get("food_name") or item.get("raw"),
                                    item.get("kcal_value") or item.get("amount") or 0,
                                    client_req_id,
                                    fingerprint,
                                    now,
                                    now,
                                ),
                            )
                            continue
                        fingerprint = _fingerprint_item(
                            name=item.get("food_name") or item.get("raw"),
                            amount=item.get("amount"),
                            unit=item.get("unit"),
                            food_id=item.get("food_id"),
                            item_type="food",
                            calories=None,
                        )
                        req_payload = f"{import_id}|{entry.get('label')}|{entry.get('slot_index')}|{idx_item}|{item.get('food_id') or 0}|{item.get('amount')}|{item.get('unit')}"
                        client_req_id = hashlib.sha1(req_payload.encode("utf-8")).hexdigest()
                        cur.execute(
                            """
                            INSERT INTO nutrition_slot_meal_items
                                (slot_meal_id, amount, unit, name_raw, food_id, item_type, calories, display_text, client_req_id, fingerprint, created_at, updated_at)
                            VALUES (?, ?, ?, ?, ?, 'food', NULL, NULL, ?, ?, ?, ?)
                            ON CONFLICT(slot_meal_id, fingerprint) DO UPDATE SET
                                amount=excluded.amount,
                                unit=excluded.unit,
                                food_id=excluded.food_id,
                                name_raw=excluded.name_raw,
                                client_req_id=excluded.client_req_id,
                                updated_at=excluded.updated_at
                            """,
                            (
                                slot_meal_id,
                                item.get("amount"),
                                item.get("unit"),
                                item.get("food_name") or item.get("raw"),
                                item.get("food_id"),
                                client_req_id,
                                fingerprint,
                                now,
                                now,
                            ),
                        )

                    if meal_template_id:
                        payload = json.dumps(entry["ingredients"], ensure_ascii=False)
                        cur.execute(
                            """
                            INSERT INTO nutrition_slot_meal_overrides (slot_id, meal_template_id, ingredients_json, updated_at)
                            VALUES (?, ?, ?, ?)
                            """,
                            (entry["slot_id"], int(meal_template_id), payload, now),
                        )
                    cur.execute(
                        "UPDATE nutrition_week_day_slots SET meal_template_id=?, time_text=?, updated_at=? WHERE id=?",
                        (int(meal_template_id) if meal_template_id else None, entry.get("time_text"), now, entry["slot_id"]),
                    )
                    stats["created_instances"] += 1
                stats["created_instances"] = len(slot_updates)
                missing = []
                for entry in slot_updates:
                    day_id = day_id_by_weekday.get(entry["weekday"])
                    if not day_id:
                        continue
                    exists = cur.execute(
                        "SELECT 1 FROM nutrition_slot_meals WHERE week_template_day_id=? AND slot_index=?",
                        (day_id, entry["slot_index"]),
                    ).fetchone()
                    if not exists:
                        missing.append(f"{entry.get('label')} Meal {entry.get('slot_index', 0) + 1}")
                if missing:
                    raise RuntimeError("slot_missing:" + ", ".join(missing))
            cur.execute(
                "UPDATE nutrition_week_templates SET updated_at=? WHERE id=?",
                (now, template_id),
            )
            _commit_import_tx(tx_mode)
        except Exception as exc:
            _rollback_import_tx(tx_mode)
            conn.close()
            return {
                "ok": False,
                "errors": issues,
                "write_errors": [
                    {
                        "message": "DB write fehlgeschlagen",
                        "detail": str(exc),
                    }
                ],
            }
    slot_defs_total = 0
    if scope_slots and day_rows:
        placeholders = ",".join("?" for _ in day_rows)
        row = conn.execute(
            f"SELECT COUNT(*) AS cnt FROM nutrition_week_day_slots WHERE week_template_day_id IN ({placeholders})",
            tuple(day["id"] for day in day_rows),
        ).fetchone()
        slot_defs_total = row["cnt"] if row else 0
    conn.close()
    summary = {
        "days": summary_days,
        "total_meals": total_meals,
        "total_items": total_items,
        "max_slot": max_slot_overall,
        "slots_written": {label: sorted(list(set(values))) for label, values in slots_written_by_day.items()},
        "slots": {
            "requested": scope_slots,
            "filled": stats.get("created_instances") or 0,
            "expected": total_meals,
        },
        "slot_defs_total": slot_defs_total,
        "templates": {
            "requested": scope_templates,
            "created": stats.get("created_templates") or 0,
            "reused": stats.get("reused_templates") or 0,
            "updated": 0,
        },
        "items_total": total_items,
        "unmatched_items": stats.get("unmatched_count") or 0,
    }
    return {
        "ok": True,
        "template_id": template_id,
        "import_id": import_id,
        "summary": summary,
        "unmatched": unmatched,
        "stats": stats,
        "errors": issues,
        "write_errors": [],
    }


def resolve_mealplan_import(import_id: str, mappings: List[Dict[str, Any]]) -> Dict[str, Any]:
    ensure_nutrition_planning_schema()
    if not import_id:
        return {"ok": False, "error": "missing_import_id"}
    normalized: Dict[str, int] = {}
    food_ids = set()
    for mapping in mappings or []:
        key = mapping.get("unmatched_key")
        food_id = mapping.get("food_id")
        if not key or not food_id:
            continue
        try:
            fid = int(food_id)
        except (TypeError, ValueError):
            continue
        normalized[key] = fid
        food_ids.add(fid)
    if not normalized:
        return {"ok": False, "error": "no_mappings"}
    conn = get_nutrition_db()
    cur = conn.cursor()
    if food_ids:
        placeholders = ",".join("?" for _ in food_ids)
        rows = cur.execute(
            f"SELECT id FROM nutrition_foods WHERE id IN ({placeholders})",
            tuple(food_ids),
        ).fetchall()
        if len(rows) != len(food_ids):
            conn.close()
            return {"ok": False, "error": "food_not_found"}
    like_token = f'%\"import_key\":\"{import_id}:%'
    overrides = cur.execute(
        "SELECT slot_id, ingredients_json FROM nutrition_slot_meal_overrides WHERE ingredients_json LIKE ?",
        (like_token,),
    ).fetchall()
    if not overrides:
        conn.close()
        return {"ok": False, "error": "import_not_found"}
    resolved = 0
    now = _utcnow_iso()
    slot_ids = [override["slot_id"] for override in overrides]
    for override in overrides:
        slot_id = override["slot_id"]
        try:
            items = json.loads(override["ingredients_json"] or "[]")
        except Exception:
            continue
        updated = False
        for item in items:
            key = item.get("import_key")
            if not key or not key.startswith(f"{import_id}:"):
                continue
            if key in normalized:
                item["food_id"] = normalized[key]
                resolved += 1
                updated = True
        if updated:
            payload = json.dumps(items, ensure_ascii=False)
            cur.execute(
                """
                UPDATE nutrition_slot_meal_overrides
                SET ingredients_json=?, updated_at=?
                WHERE slot_id=?
                """,
                (payload, now, slot_id),
            )
    if resolved == 0:
        conn.commit()
        conn.close()
        return {"ok": False, "error": "nothing_resolved"}
    conn.commit()
    remaining = 0
    if slot_ids:
        placeholders = ",".join("?" for _ in slot_ids)
        rows = cur.execute(
            f"SELECT ingredients_json FROM nutrition_slot_meal_overrides WHERE slot_id IN ({placeholders})",
            tuple(slot_ids),
        ).fetchall()
        for row in rows:
            try:
                items = json.loads(row["ingredients_json"] or "[]")
            except Exception:
                continue
            for ingredient in items:
                if ingredient.get("import_key", "").startswith(f"{import_id}:") and not ingredient.get("food_id"):
                    remaining += 1
    conn.close()
    return {"ok": True, "resolved": resolved, "remaining": remaining}


def _berlin_now() -> datetime:
    return datetime.now(ZoneInfo("Europe/Berlin")).replace(microsecond=0)


def _normalize_date_iso(date_iso: Optional[str]) -> str:
    if isinstance(date_iso, str):
        text = date_iso.strip()
        if text:
            try:
                return date.fromisoformat(text).isoformat()
            except Exception:
                pass
    return _berlin_now().date().isoformat()


def _time_from_iso(iso_value: Optional[str]) -> str:
    text = (iso_value or "").strip()
    if not text:
        return "00:00"
    try:
        dt = datetime.fromisoformat(text)
        return dt.strftime("%H:%M")
    except Exception:
        return "00:00"


def _safe_time_text(value: Optional[str]) -> Optional[str]:
    text = (value or "").strip()
    if not text:
        return None
    parts = text.split(":")
    if len(parts) < 2:
        return None
    try:
        hours = int(parts[0])
        minutes = int(parts[1])
    except Exception:
        return None
    if hours < 0 or hours > 23 or minutes < 0 or minutes > 59:
        return None
    return f"{hours:02d}:{minutes:02d}"


def _parse_time_minutes(value: Optional[str]) -> Optional[int]:
    text = _safe_time_text(value)
    if not text:
        return None
    hours, minutes = text.split(":")
    return int(hours) * 60 + int(minutes)


def _minutes_from_logged_at(value: Optional[str]) -> int:
    parsed = _parse_time_minutes(_time_from_iso(value))
    return parsed if parsed is not None else 12 * 60


def _auto_logged_meal_title(log_date: str, logged_at: Optional[str], index: int) -> str:
    if index == 0:
        return "Frühstück"
    minutes = _minutes_from_logged_at(logged_at)
    try:
        is_weekday = date.fromisoformat(str(log_date)).weekday() < 5
    except Exception:
        is_weekday = False
    if is_weekday and 8 * 60 <= minutes <= 15 * 60 + 30:
        return "Schule"
    if minutes < 11 * 60:
        return "Morgens"
    if minutes < 14 * 60 + 30:
        return "Mittags"
    if minutes < 18 * 60:
        return "Nachmittags"
    return "Abends"


def _planned_logged_meal_titles_for_date(date_iso: str) -> Dict[int, str]:
    try:
        planned, _ = _planned_meals_for_date(date_iso)
    except Exception:
        return {}
    out: Dict[int, str] = {}
    for meal in planned:
        try:
            slot_id = int(meal.get("slot_id") or 0)
        except Exception:
            slot_id = 0
        title = str(meal.get("title") or meal.get("meal_title") or "").strip()
        if slot_id > 0 and title:
            out[slot_id] = title
    return out


def _renumber_logged_meals_for_date(cur, date_iso: str) -> None:
    rows = cur.execute(
        """
        SELECT id, log_date, logged_at, slot_id, created_from_planned_slot_id
        FROM nutrition_logged_meals
        WHERE log_date=?
        ORDER BY logged_at ASC, id ASC
        """,
        (date_iso,),
    ).fetchall()
    cols = {row[1] for row in cur.execute("PRAGMA table_info(nutrition_logged_meals)").fetchall()}
    has_meal_name = "meal_name" in cols
    planned_titles = _planned_logged_meal_titles_for_date(date_iso)
    for idx, row in enumerate(rows):
        planned_slot_id = int(row["created_from_planned_slot_id"] or row["slot_id"] or 0)
        title = planned_titles.get(planned_slot_id) or _auto_logged_meal_title(str(row["log_date"] or date_iso), row["logged_at"], idx)
        slot = f"Meal {idx + 1}"
        if has_meal_name:
            cur.execute(
                "UPDATE nutrition_logged_meals SET title=?, meal_name=?, meal_slot=?, updated_at=? WHERE id=?",
                (title, title, slot, _utcnow_iso(), int(row["id"])),
            )
        else:
            cur.execute(
                "UPDATE nutrition_logged_meals SET title=?, meal_slot=?, updated_at=? WHERE id=?",
                (title, slot, _utcnow_iso(), int(row["id"])),
            )


def _logged_meal_title_slot(cur, logged_meal_id: int) -> tuple[str, str]:
    row = cur.execute("SELECT title, meal_slot FROM nutrition_logged_meals WHERE id=?", (int(logged_meal_id),)).fetchone()
    if not row:
        return "Meal", "Meal 1"
    return str(row["title"] or "Meal"), str(row["meal_slot"] or "Meal 1")


def _normalize_meal_slot(value: Any) -> str:
    text = (str(value).strip() if value is not None else "")
    if not text:
        return "frei"
    return text[:48]


def _normalize_logging_items(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    seen = set()
    for item in items or []:
        unit = (item.get("unit") or "g").strip().lower()
        if (item.get("item_type") or "").strip().lower() == "kcal_only" or unit == "kcal":
            kcal_value = _clamp_float(item.get("calories") if "calories" in item else item.get("amount"), 0.0, None)
            if kcal_value is None:
                continue
            key = ("kcal", round(kcal_value, 3), (item.get("food_name") or item.get("name") or "Kcal").strip().lower())
            if key in seen:
                continue
            seen.add(key)
            out.append(
                {
                    "item_type": "kcal_only",
                    "food_id": None,
                    "food_name": (item.get("food_name") or item.get("name") or "Kcal").strip() or "Kcal",
                    "amount": kcal_value,
                    "unit": "kcal",
                    "calories": kcal_value,
                    "protein_override": _clamp_float(item.get("protein_override") if "protein_override" in item else item.get("protein"), 0.0, None),
                    "carbs_override": _clamp_float(item.get("carbs_override") if "carbs_override" in item else item.get("carbs"), 0.0, None),
                    "fat_override": _clamp_float(item.get("fat_override") if "fat_override" in item else item.get("fat"), 0.0, None),
                    "sugar_override": _clamp_float(item.get("sugar_override") if "sugar_override" in item else item.get("sugar"), 0.0, None),
                }
            )
            continue
        amount = _clamp_float(item.get("amount"), 0.0, None)
        if amount is None:
            continue
        raw_food_id = item.get("food_id")
        food_id = int(raw_food_id) if raw_food_id else None
        name = (item.get("food_name") or item.get("name") or "").strip()
        key = (food_id or 0, round(amount, 3), unit, name.lower())
        if key in seen:
            continue
        seen.add(key)
        out.append(
            {
                "item_type": "food",
                "food_id": food_id,
                "food_name": name or None,
                "amount": amount,
                "unit": unit or "g",
                "calories": None,
                "protein_override": None,
                "carbs_override": None,
                "fat_override": None,
                "sugar_override": None,
            }
        )
    return out


def _hydrate_logging_food_items(cur, items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Attach current food metadata needed by clients to render item macros."""
    food_ids = sorted({int(item["food_id"]) for item in items if item.get("food_id")})
    foods_by_id: Dict[int, Dict[str, Any]] = {}
    if food_ids:
        placeholders = ",".join("?" for _ in food_ids)
        rows = cur.execute(
            f"""
            SELECT id, name, unit_default, common_portion_size, portion_g,
                   kcal_per_100, p_per_100, c_per_100, f_per_100, sugar_per_100
            FROM nutrition_foods
            WHERE id IN ({placeholders})
            """,
            tuple(food_ids),
        ).fetchall()
        foods_by_id = {int(row["id"]): dict(row) for row in rows}

    hydrated: List[Dict[str, Any]] = []
    for raw_item in items:
        item = dict(raw_item)
        food = foods_by_id.get(int(item.get("food_id") or 0))
        if food:
            item.update(
                {
                    "food_name": item.get("food_name") or food.get("name") or "Food",
                    "unit_default": food.get("unit_default"),
                    "common_portion_size": food.get("common_portion_size"),
                    "portion_g": food.get("portion_g"),
                    "kcal_per_100": food.get("kcal_per_100") or 0.0,
                    "p_per_100": food.get("p_per_100") or 0.0,
                    "c_per_100": food.get("c_per_100") or 0.0,
                    "f_per_100": food.get("f_per_100") or 0.0,
                    "sugar_per_100": food.get("sugar_per_100") or 0.0,
                }
            )
        hydrated.append(item)
    return hydrated


def _meal_title_from_slot(slot: Dict[str, Any]) -> str:
    return (
        (slot.get("custom_title") or "").strip()
        or ((slot.get("meal_template") or {}).get("title") or "").strip()
        or (slot.get("meal_title") or "").strip()
        or ((slot.get("slot_template") or {}).get("title") or "").strip()
        or "Meal"
    )


def _slot_has_meal(slot: Dict[str, Any]) -> bool:
    items = slot.get("items") or []
    has_items = any(isinstance(item, dict) and ((item.get("food_id") is not None) or str(item.get("name") or "").strip()) for item in items)
    if has_items:
        return True
    custom_title = str(slot.get("custom_title") or "").strip()
    meal_title = str(slot.get("meal_title") or "").strip()
    title = custom_title or meal_title
    if title and not _is_placeholder_title(title):
        return True
    return bool(slot.get("meal_template_id") or slot.get("slot_meal_id"))


def _is_placeholder_title(title: str) -> bool:
    text = str(title or "").strip().lower()
    if not text:
        return True
    parts = text.split()
    return len(parts) == 2 and parts[0] == "meal" and parts[1].isdigit()


def _planned_meals_for_date(date_iso: str) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    day_date = date.fromisoformat(date_iso)
    payload = get_active_week_plan_resolved(initialize_schema=False)
    week = payload.get("week_template") if isinstance(payload, dict) else None
    if not week:
        return [], {}
    weekday = day_date.weekday()
    day_entry = next(
        (
            d
            for d in (week.get("days") or [])
            if d.get("weekday") is not None and int(d.get("weekday")) == weekday
        ),
        None,
    )
    if not day_entry:
        return [], {}
    out: List[Dict[str, Any]] = []
    conn = get_nutrition_db()
    try:
        cur = conn.cursor()
        for slot in day_entry.get("slots") or []:
            if not _slot_has_meal(slot):
                continue
            slot_items = _normalize_logging_items(slot.get("items") or [])
            display_items = _hydrate_logging_food_items(cur, slot_items)
            override = slot.get("override") or {}
            servings = _clamp_float(slot.get("servings"), 0.01, None) or 1.0
            meal_tpl = slot.get("meal_template") or {}
            if override and override.get("kcal") is not None:
                base_macros = {
                    "kcal": override.get("kcal") or 0.0,
                    "p": override.get("p") or 0.0,
                    "c": override.get("c") or 0.0,
                    "f": override.get("f") or 0.0,
                }
            elif slot_items:
                # Recalculate from current food records. Stored meal-template
                # totals can be stale, especially for pcs foods after a food
                # portion or macro correction.
                base_macros = _compute_logged_meal_macros(cur, 0, slot_items)
            else:
                base_macros = {
                    "kcal": meal_tpl.get("kcal_per_serving") or 0.0,
                    "p": meal_tpl.get("p_per_serving") or 0.0,
                    "c": meal_tpl.get("c_per_serving") or 0.0,
                    "f": meal_tpl.get("f_per_serving") or 0.0,
                }
            macros = {
                "kcal": round((base_macros.get("kcal") or 0.0) * servings, 1),
                "p": round((base_macros.get("p") or 0.0) * servings, 1),
                "c": round((base_macros.get("c") or 0.0) * servings, 1),
                "f": round((base_macros.get("f") or 0.0) * servings, 1),
            }
            time_text = _safe_time_text(slot.get("time_text")) or _safe_time_text((slot.get("slot_template") or {}).get("default_time"))
            out.append(
                {
                    "slot_id": int(slot.get("id") or 0),
                    "slot_index": int(slot.get("slot_index") or 0),
                    "time_text": time_text,
                    "title": _meal_title_from_slot(slot),
                    "meal_slot": ((slot.get("slot_template") or {}).get("title") or f"Meal {int(slot.get('slot_index') or 0) + 1}"),
                    "meal_template_id": slot.get("meal_template_id"),
                    "servings": servings,
                    "items": display_items,
                    "macros": macros,
                }
            )
    finally:
        conn.close()
    out.sort(key=lambda row: (_parse_time_minutes(row.get("time_text")) is None, _parse_time_minutes(row.get("time_text")) or 10_000, row.get("slot_index") or 0))
    return out, day_entry.get("target") or {}


def _extract_meal_slot_index(meal_slot: Optional[str]) -> Optional[int]:
    text = str(meal_slot or "").strip().lower()
    match = re.fullmatch(r"meal\s+(\d+)", text)
    if not match:
        return None
    try:
        return max(0, int(match.group(1)) - 1)
    except Exception:
        return None


def _resolve_current_planned_slot_for_logged_meal(cur, logged_meal_row: Dict[str, Any]) -> Optional[int]:
    log_date = _normalize_date_iso(logged_meal_row.get("log_date"))
    try:
        weekday = date.fromisoformat(log_date).weekday()
    except Exception:
        return None

    week_template_cols = {row[1] for row in cur.execute("PRAGMA table_info(nutrition_week_templates)").fetchall()}
    active_plan_sql = "SELECT id FROM nutrition_week_templates WHERE is_active=1"
    if "archived" in week_template_cols:
        active_plan_sql += " AND archived=0"
    active_plan_sql += " ORDER BY id DESC LIMIT 1"
    active_plan = cur.execute(active_plan_sql).fetchone()
    if not active_plan:
        return None
    day_row = cur.execute(
        """
        SELECT id
        FROM nutrition_week_template_days
        WHERE week_template_id=? AND weekday=?
        ORDER BY id ASC
        LIMIT 1
        """,
        (int(active_plan["id"]), weekday),
    ).fetchone()
    if not day_row:
        return None

    slot_rows = cur.execute(
        """
        SELECT s.id, s.slot_index, s.time_text, s.custom_title, s.meal_template_id,
               sm.id AS slot_meal_id, sm.meal_title
        FROM nutrition_week_day_slots s
        LEFT JOIN nutrition_slot_meals sm
            ON sm.week_template_day_id=s.week_template_day_id AND sm.slot_index=s.slot_index
        WHERE s.week_template_day_id=?
        ORDER BY s.slot_index ASC, s.id ASC
        """,
        (int(day_row["id"]),),
    ).fetchall()
    if not slot_rows:
        return None

    logged_template_id = logged_meal_row.get("created_from_template_id")
    logged_time = _safe_time_text(logged_meal_row.get("original_planned_time"))
    logged_slot_index = _extract_meal_slot_index(logged_meal_row.get("meal_slot"))
    logged_title = str(logged_meal_row.get("title") or "").strip().lower()

    candidates: List[Tuple[Tuple[int, int, int, int, int, int], int]] = []
    for row in slot_rows:
        slot = dict(row)
        slot_id = int(slot["id"])
        slot_template_id = slot.get("meal_template_id")
        slot_time = _safe_time_text(slot.get("time_text"))
        slot_index = int(slot.get("slot_index") or 0)

        template_match = bool(logged_template_id and slot_template_id and int(slot_template_id) == int(logged_template_id))
        time_match = bool(logged_time and slot_time and slot_time == logged_time)
        slot_index_match = logged_slot_index is not None and slot_index == logged_slot_index

        slot_title = (
            str(slot.get("custom_title") or "").strip()
            or str(slot.get("meal_title") or "").strip()
        ).lower()
        title_match = bool(logged_title and slot_title and slot_title == logged_title)
        placeholder_penalty = 1 if not _slot_has_meal(slot) else 0

        score = (
            4 if template_match and time_match else
            3 if template_match else
            2 if time_match else
            1 if slot_index_match else
            0
        )
        if score <= 0:
            continue
        tie_break = (
            score,
            1 if template_match else 0,
            1 if time_match else 0,
            1 if title_match else 0,
            -placeholder_penalty,
            -abs(slot_index - (logged_slot_index if logged_slot_index is not None else slot_index)),
        )
        candidates.append((tie_break, slot_id))

    if not candidates:
        return None

    candidates.sort(reverse=True)
    best_score, best_slot_id = candidates[0]
    tied = [slot_id for score, slot_id in candidates if score == best_score]
    if len(tied) != 1:
        return None
    return best_slot_id


def _planned_status_for_logged_meal(
    cur,
    *,
    date_iso: str,
    slot_id: int,
    logged_meal_id: int,
    row: Dict[str, Any],
    items: List[Dict[str, Any]],
) -> Tuple[str, Optional[str], Optional[str]]:
    changed_flag = bool(row.get("edited_from_template")) or _is_kcal_changed_vs_planned(
        cur,
        date_iso=date_iso,
        slot_id=slot_id,
        items=items,
    )
    shifted_time_text = None
    original_time = _safe_time_text(row.get("original_planned_time"))
    actual_time = _time_from_iso(row.get("logged_at"))
    if original_time and actual_time and original_time != actual_time:
        shifted_time_text = actual_time
    return (
        "manual_override" if changed_flag else "logged",
        shifted_time_text,
        "MANUAL_EDIT" if changed_flag else None,
    )


def reconcile_logged_planned_meals(
    *,
    date_iso: Optional[str] = None,
    days: Optional[int] = None,
    apply: bool = False,
) -> Dict[str, int]:
    ensure_nutrition_planning_schema()
    conn = get_nutrition_db()
    cur = conn.cursor()
    params: List[Any] = []
    where = [
        "source IN ('planned', 'day_copy')",
        "("
        "created_from_planned_slot_id IS NULL "
        "OR NOT EXISTS (SELECT 1 FROM nutrition_week_day_slots s WHERE s.id=nutrition_logged_meals.created_from_planned_slot_id)"
        ")",
    ]
    if date_iso:
        where.append("log_date=?")
        params.append(_normalize_date_iso(date_iso))
    elif days:
        cutoff = date.today()
        try:
            cutoff = _berlin_today()
        except Exception:
            cutoff = date.today()
        from_date = (cutoff - timedelta(days=max(0, int(days) - 1))).isoformat()
        where.append("log_date>=?")
        params.append(from_date)
    rows = cur.execute(
        f"""
        SELECT *
        FROM nutrition_logged_meals
        WHERE {' AND '.join(where)}
        ORDER BY log_date ASC, logged_at ASC, id ASC
        """,
        tuple(params),
    ).fetchall()

    scanned = resolved = unresolved = status_written = 0
    now = _utcnow_iso()
    for row in rows:
        scanned += 1
        row_dict = dict(row)
        slot_id = _resolve_current_planned_slot_for_logged_meal(cur, row_dict)
        if slot_id is None:
            unresolved += 1
            if apply:
                cur.execute(
                    "UPDATE nutrition_logged_meals SET created_from_planned_slot_id=NULL, edited_from_template=1, updated_at=? WHERE id=?",
                    (now, int(row["id"])),
                )
            continue
        resolved += 1
        items = _serialize_logged_meal_items(cur, [int(row["id"])]).get(int(row["id"]), [])
        status, shifted_time_text, reason_code = _planned_status_for_logged_meal(
            cur,
            date_iso=str(row["log_date"]),
            slot_id=int(slot_id),
            logged_meal_id=int(row["id"]),
            row=row_dict,
            items=items,
        )
        if apply:
            cur.execute(
                "UPDATE nutrition_logged_meals SET created_from_planned_slot_id=?, edited_from_template=?, updated_at=? WHERE id=?",
                (int(slot_id), 1 if status == "manual_override" else 0, now, int(row["id"])),
            )
            _set_planned_status(
                cur,
                date_iso=str(row["log_date"]),
                slot_id=int(slot_id),
                status=status,
                shifted_time_text=shifted_time_text,
                logged_meal_id=int(row["id"]),
                status_source="system",
                reason_code=reason_code or "RECONCILED_PLANNED_LINK",
                trace_json={"reconciled": True, "logged_meal_id": int(row["id"])},
            )
            status_written += 1
    if apply:
        touched_dates = sorted({str(row["log_date"]) for row in rows})
        for touched_date in touched_dates:
            _renumber_logged_meals_for_date(cur, touched_date)
            _sync_logging_totals_to_weight_logs(cur, touched_date)
        conn.commit()
    conn.close()
    return {
        "scanned": scanned,
        "resolved": resolved,
        "unresolved": unresolved,
        "status_written": status_written,
    }


def _serialize_logged_meal_items(cur, logged_meal_ids: List[int]) -> Dict[int, List[Dict[str, Any]]]:
    if not logged_meal_ids:
        return {}
    placeholders = ",".join("?" for _ in logged_meal_ids)
    rows = cur.execute(
        f"""
        SELECT li.*, f.name AS food_name_ref, f.unit_default, f.common_portion_size,
               f.portion_g, f.kcal_per_100, f.p_per_100, f.c_per_100, f.f_per_100
        FROM nutrition_logged_meal_items li
        LEFT JOIN nutrition_foods f ON f.id=li.food_id
        WHERE li.logged_meal_id IN ({placeholders})
        ORDER BY li.logged_meal_id, li.sort_index, li.id
        """,
        tuple(logged_meal_ids),
    ).fetchall()
    items_by_meal: Dict[int, List[Dict[str, Any]]] = {}
    for row in rows:
        item = dict(row)
        if (item.get("item_type") or "food") == "kcal_only":
            out_item = {
                "id": item.get("id"),
                "item_type": "kcal_only",
                "food_id": None,
                "food_name": item.get("food_name") or "Kcal",
                "amount": item.get("calories") or item.get("amount") or 0,
                "unit": "kcal",
                "calories": item.get("calories") or item.get("amount") or 0,
                "protein_override": item.get("protein_override"),
                "carbs_override": item.get("carbs_override"),
                "fat_override": item.get("fat_override"),
                "sugar_override": item.get("sugar_override"),
            }
        else:
            amount_struct = _display_amount_struct(item.get("amount"), item.get("unit") or "g")
            out_item = {
                "id": item.get("id"),
                "item_type": "food",
                "food_id": item.get("food_id"),
                "food_name": item.get("food_name_ref") or item.get("food_name") or "Food",
                **amount_struct,
                "unit_default": item.get("unit_default"),
                "common_portion_size": item.get("common_portion_size"),
                "portion_g": item.get("portion_g"),
                "kcal_per_100": item.get("kcal_per_100") or 0.0,
                "p_per_100": item.get("p_per_100") or 0.0,
                "c_per_100": item.get("c_per_100") or 0.0,
                "f_per_100": item.get("f_per_100") or 0.0,
            }
        items_by_meal.setdefault(int(item["logged_meal_id"]), []).append(out_item)
    return items_by_meal


def _compute_logged_meal_macros(cur, meal_id: int, items: List[Dict[str, Any]]) -> Dict[str, float]:
    food_ids = [int(item["food_id"]) for item in items if item.get("food_id")]
    foods_by_id: Dict[int, Dict[str, Any]] = {}
    if food_ids:
        unique_food_ids = sorted(set(food_ids))
        placeholders = ",".join("?" for _ in unique_food_ids)
        rows = cur.execute(
            f"""
            SELECT id, unit_default, common_portion_size, portion_g,
                   kcal_per_100, p_per_100, c_per_100, f_per_100, sugar_per_100
            FROM nutrition_foods
            WHERE id IN ({placeholders})
            """,
            tuple(unique_food_ids),
        ).fetchall()
        foods_by_id = {int(row["id"]): dict(row) for row in rows}
    totals = MacroTotals(0.0, 0.0, 0.0, 0.0)
    for item in items:
        if item.get("item_type") == "kcal_only":
            totals = totals.add(
                MacroTotals(
                    kcal=float(item.get("calories") or item.get("amount") or 0),
                    p=float(item.get("protein_override") or 0.0),
                    c=float(item.get("carbs_override") or 0.0),
                    f=float(item.get("fat_override") or 0.0),
                    sugar=float(item.get("sugar_override") or 0.0),
                )
            )
            continue
        food_id = item.get("food_id")
        if not food_id:
            continue
        food = foods_by_id.get(int(food_id))
        if not food:
            continue
        factor = _food_macro_factor(
            amount=item.get("amount") or 0.0,
            unit=item.get("unit") or "g",
            food_unit_default=food.get("unit_default"),
            common_portion=food.get("common_portion_size"),
            portion_g=food.get("portion_g"),
        )
        totals = totals.add(
            MacroTotals(
                kcal=(food.get("kcal_per_100") or 0.0) * factor,
                p=(food.get("p_per_100") or 0.0) * factor,
                c=(food.get("c_per_100") or 0.0) * factor,
                f=(food.get("f_per_100") or 0.0) * factor,
                sugar=(food.get("sugar_per_100") or 0.0) * factor,
            )
        )
    return _macro_to_dict(totals)


def _is_kcal_changed_vs_planned(cur, *, date_iso: str, slot_id: int, items: List[Dict[str, Any]]) -> bool:
    planned = _get_planned_slot_by_id(date_iso, int(slot_id))
    if not planned:
        return False
    planned_kcal = float(((planned.get("macros") or {}).get("kcal")) or 0.0)
    actual_kcal = float((_compute_logged_meal_macros(cur, 0, items).get("kcal")) or 0.0)
    # Ignore tiny floating noise, mark changed only for meaningful kcal deltas.
    return abs(actual_kcal - planned_kcal) > 0.05


def _set_planned_status(
    cur,
    *,
    date_iso: str,
    slot_id: int,
    status: str,
    shifted_time_text: Optional[str] = None,
    logged_meal_id: Optional[int] = None,
    status_source: str = "user",
    reason_code: Optional[str] = None,
    trace_json: Optional[Dict[str, Any]] = None,
) -> None:
    normalized_status = status if status in LOGGING_STATUS_VALUES else "open"
    normalized_source = (status_source or "user").strip().lower() or "user"
    trace_text = None
    if trace_json is not None:
        try:
            trace_text = json.dumps(trace_json, ensure_ascii=False)
        except Exception:
            trace_text = None
    now = _utcnow_iso()
    # Defensive guard: logged meals can outlive planned slot rows after plan rewrites.
    # If the referenced planned slot no longer exists, do not write a status row
    # that would violate nutrition_planned_meal_status.slot_id foreign key.
    if cur.execute(
        "SELECT 1 FROM nutrition_week_day_slots WHERE id=? LIMIT 1",
        (int(slot_id),),
    ).fetchone() is None:
        return
    row_payload = (
        date_iso,
        int(slot_id),
        normalized_status,
        _safe_time_text(shifted_time_text),
        logged_meal_id,
        normalized_source,
        (reason_code or "").strip() or None,
        trace_text,
        now,
    )
    unique_indexes = cur.execute("PRAGMA index_list(nutrition_planned_meal_status)").fetchall()
    has_date_slot_unique = False
    for idx in unique_indexes:
        if not int(idx[2] or 0):
            continue
        idx_cols = [col[2] for col in cur.execute(f"PRAGMA index_info({idx[1]})").fetchall()]
        if idx_cols == ["log_date", "slot_id"]:
            has_date_slot_unique = True
            break
    if has_date_slot_unique:
        cur.execute(
            """
            INSERT INTO nutrition_planned_meal_status
                (log_date, slot_id, status, shifted_time_text, logged_meal_id, status_source, reason_code, trace_json, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(log_date, slot_id) DO UPDATE SET
                status=excluded.status,
                shifted_time_text=excluded.shifted_time_text,
                logged_meal_id=excluded.logged_meal_id,
                status_source=excluded.status_source,
                reason_code=excluded.reason_code,
                trace_json=excluded.trace_json,
                updated_at=excluded.updated_at
            """,
            row_payload,
        )
    else:
        existing = cur.execute(
            "SELECT id FROM nutrition_planned_meal_status WHERE log_date=? AND slot_id=? ORDER BY id DESC LIMIT 1",
            (date_iso, int(slot_id)),
        ).fetchone()
        if existing:
            cur.execute(
                """
                UPDATE nutrition_planned_meal_status
                SET status=?, shifted_time_text=?, logged_meal_id=?, status_source=?, reason_code=?, trace_json=?, updated_at=?
                WHERE id=?
                """,
                (
                    normalized_status,
                    _safe_time_text(shifted_time_text),
                    logged_meal_id,
                    normalized_source,
                    (reason_code or "").strip() or None,
                    trace_text,
                    now,
                    int(existing["id"]),
                ),
            )
        else:
            cur.execute(
                """
                INSERT INTO nutrition_planned_meal_status
                    (log_date, slot_id, status, shifted_time_text, logged_meal_id, status_source, reason_code, trace_json, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                row_payload,
            )


def _record_recent_foods(cur, *, items: List[Dict[str, Any]], meal_slot: str, log_date: str, logged_at: str) -> None:
    now = _utcnow_iso()
    for item in items:
        food_id = item.get("food_id")
        if not food_id:
            continue
        amount = _clamp_float(item.get("amount"), 0.0, None)
        unit = (item.get("unit") or "g").strip().lower()
        cur.execute(
            """
            INSERT INTO nutrition_logging_recent_foods
                (food_id, last_amount, last_unit, last_meal_slot, last_logged_at, use_count, today_count, today_date, updated_at)
            VALUES (?, ?, ?, ?, ?, 1, 1, ?, ?)
            ON CONFLICT(food_id) DO UPDATE SET
                last_amount=excluded.last_amount,
                last_unit=excluded.last_unit,
                last_meal_slot=excluded.last_meal_slot,
                last_logged_at=excluded.last_logged_at,
                use_count=nutrition_logging_recent_foods.use_count + 1,
                today_count=CASE
                    WHEN nutrition_logging_recent_foods.today_date = excluded.today_date THEN nutrition_logging_recent_foods.today_count + 1
                    ELSE 1
                END,
                today_date=excluded.today_date,
                updated_at=excluded.updated_at
            """,
            (int(food_id), amount, unit, meal_slot, logged_at, log_date, now),
        )


def _record_recent_meal(cur, *, title: str, meal_slot: str, items: List[Dict[str, Any]], log_date: str, logged_at: str) -> None:
    normalized_items = []
    for item in items:
        normalized_items.append(
            {
                "food_id": item.get("food_id"),
                "food_name": item.get("food_name"),
                "amount": item.get("amount"),
                "unit": item.get("unit"),
                "item_type": item.get("item_type") or "food",
                "calories": item.get("calories"),
            }
        )
    payload_obj = {"title": title, "meal_slot": meal_slot, "items": normalized_items}
    payload_json = json.dumps(payload_obj, ensure_ascii=True, separators=(",", ":"))
    meal_key = hashlib.sha1(payload_json.encode("utf-8")).hexdigest()
    now = _utcnow_iso()
    cur.execute(
        """
        INSERT INTO nutrition_logging_recent_meals
            (meal_key, title, meal_slot, payload_json, last_logged_at, use_count, today_count, today_date, updated_at)
        VALUES (?, ?, ?, ?, ?, 1, 1, ?, ?)
        ON CONFLICT(meal_key) DO UPDATE SET
            title=excluded.title,
            meal_slot=excluded.meal_slot,
            payload_json=excluded.payload_json,
            last_logged_at=excluded.last_logged_at,
            use_count=nutrition_logging_recent_meals.use_count + 1,
            today_count=CASE
                WHEN nutrition_logging_recent_meals.today_date = excluded.today_date THEN nutrition_logging_recent_meals.today_count + 1
                ELSE 1
            END,
            today_date=excluded.today_date,
            updated_at=excluded.updated_at
        """,
        (meal_key, title, meal_slot, payload_json, logged_at, log_date, now),
    )


def _insert_logged_meal_items(cur, logged_meal_id: int, items: List[Dict[str, Any]]) -> None:
    now = _utcnow_iso()
    cleaned = _normalize_logging_items(items)
    for idx, item in enumerate(cleaned):
        cur.execute(
            """
            INSERT INTO nutrition_logged_meal_items
                (logged_meal_id, food_id, food_name, amount, unit, item_type, calories,
                 protein_override, carbs_override, fat_override, sugar_override,
                 sort_index, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                logged_meal_id,
                item.get("food_id"),
                item.get("food_name"),
                item.get("amount"),
                item.get("unit"),
                item.get("item_type") or "food",
                item.get("calories"),
                item.get("protein_override"),
                item.get("carbs_override"),
                item.get("fat_override"),
                item.get("sugar_override"),
                idx,
                now,
                now,
            ),
        )


def _resolve_logged_at(date_iso: str, *, time_mode: str = "now", planned_time_text: Optional[str] = None, custom_logged_at: Optional[str] = None) -> str:
    if custom_logged_at:
        try:
            return datetime.fromisoformat(custom_logged_at).replace(microsecond=0).isoformat()
        except Exception:
            pass
    if time_mode == "planned":
        tt = _safe_time_text(planned_time_text) or "12:00"
        return f"{date_iso}T{tt}:00"
    if time_mode == "custom":
        tt = _safe_time_text(planned_time_text) or "12:00"
        return f"{date_iso}T{tt}:00"
    return _berlin_now().isoformat()


def _load_logged_meals_for_date(cur, date_iso: str) -> List[Dict[str, Any]]:
    rows = cur.execute(
        """
        SELECT *
        FROM nutrition_logged_meals
        WHERE log_date=?
        ORDER BY logged_at ASC, id ASC
        """,
        (date_iso,),
    ).fetchall()
    meals = [dict(row) for row in rows]
    items_by_meal = _serialize_logged_meal_items(cur, [int(row["id"]) for row in meals])
    out: List[Dict[str, Any]] = []
    for row in meals:
        meal_items = items_by_meal.get(int(row["id"]), [])
        out.append(
            {
                "id": int(row["id"]),
                "log_date": row.get("log_date"),
                "slot_id": row.get("slot_id"),
                "title": row.get("title") or "Meal",
                "meal_slot": row.get("meal_slot") or "frei",
                "source": row.get("source") or "free",
                "created_from_planned_slot_id": row.get("created_from_planned_slot_id"),
                "created_from_template_id": row.get("created_from_template_id"),
                "edited_from_template": bool(row.get("edited_from_template")),
                "original_planned_time": row.get("original_planned_time"),
                "logged_at": row.get("logged_at"),
                "time_text": _time_from_iso(row.get("logged_at")),
                "is_favorite": bool(row.get("is_favorite")),
                "adjusted_from_meal_id": row.get("adjusted_from_meal_id"),
                "adjustment_reason": row.get("adjustment_reason"),
                "action_source": row.get("action_source"),
                "items": meal_items,
                "macros": _compute_logged_meal_macros(cur, int(row["id"]), meal_items),
            }
        )
    return out


def get_logged_meals_range(
    date_from: str,
    date_to: str,
    *,
    limit: int = 100,
) -> List[Dict[str, Any]]:
    """Return real logged meals with foods, quantities and computed macros."""
    ensure_nutrition_planning_schema()
    start = _normalize_date_iso(date_from)
    end = _normalize_date_iso(date_to)
    if start > end:
        start, end = end, start
    bounded_limit = max(1, min(500, int(limit or 100)))
    conn = get_nutrition_db()
    cur = conn.cursor()
    try:
        selected = cur.execute(
            """
            SELECT id, log_date
            FROM nutrition_logged_meals
            WHERE date(log_date) >= date(?) AND date(log_date) <= date(?)
            ORDER BY logged_at DESC, id DESC
            LIMIT ?
            """,
            (start, end, bounded_limit),
        ).fetchall()
        selected_ids = [int(row["id"]) for row in selected]
        selected_dates = list(dict.fromkeys(str(row["log_date"]) for row in selected))
        meals_by_id: Dict[int, Dict[str, Any]] = {}
        for date_iso in selected_dates:
            for meal in _load_logged_meals_for_date(cur, date_iso):
                meals_by_id[int(meal["id"])] = meal
        return [meals_by_id[meal_id] for meal_id in selected_ids if meal_id in meals_by_id]
    finally:
        conn.close()


def _sum_macros(entries: List[Dict[str, Any]]) -> Dict[str, float]:
    kcal = 0.0
    p = 0.0
    c = 0.0
    f = 0.0
    sugar = 0.0
    for entry in entries:
        macros = entry.get("macros") or {}
        kcal += float(macros.get("kcal") or 0.0)
        p += float(macros.get("p") or 0.0)
        c += float(macros.get("c") or 0.0)
        f += float(macros.get("f") or 0.0)
        sugar += float(macros.get("sugar") or 0.0)
    return {"kcal": round(kcal, 1), "p": round(p, 1), "c": round(c, 1), "f": round(f, 1), "sugar": round(sugar, 1)}


def _date_pretty_from_iso(date_iso: str) -> str:
    try:
        return date.fromisoformat(str(date_iso)).strftime("%d.%m.%y")
    except Exception:
        return str(date_iso)


def _sync_logging_totals_to_weight_logs(cur, date_iso: str) -> None:
    # /makros reads nutrition_daily view (which points to weight_logs).
    # Keep that source in sync with meals logging totals for this day.
    try:
        logged = _load_logged_meals_for_date(cur, date_iso)
        totals = _sum_macros(logged)
        has_logged = bool(logged)

        existing_row = cur.execute(
            "SELECT id, date, raw_json FROM weight_logs WHERE date_iso=? LIMIT 1",
            (date_iso,),
        ).fetchone()
        existing = dict(existing_row) if existing_row is not None else None

        raw_obj: Dict[str, Any] = {}
        if existing and existing.get("raw_json"):
            try:
                parsed = json.loads(existing["raw_json"])
                if isinstance(parsed, dict):
                    raw_obj = parsed
            except Exception:
                raw_obj = {}

        raw_obj["source"] = "meals_logging"
        raw_obj["sync"] = "nutrition_logged_meals"
        raw_obj["logged_meals_count"] = len(logged)
        raw_obj["kcal"] = round(float(totals["kcal"] or 0.0), 1) if has_logged else None
        raw_obj["protein"] = round(float(totals["p"] or 0.0), 1) if has_logged else None
        raw_obj["carbs"] = round(float(totals["c"] or 0.0), 1) if has_logged else None
        raw_obj["fat"] = round(float(totals["f"] or 0.0), 1) if has_logged else None
        raw_obj["sugar"] = round(float(totals["sugar"] or 0.0), 1) if has_logged else None

        raw_text = json.dumps(raw_obj, ensure_ascii=True, separators=(",", ":"))

        kcal_val = totals["kcal"] if has_logged else None
        p_val = totals["p"] if has_logged else None
        c_val = totals["c"] if has_logged else None
        f_val = totals["f"] if has_logged else None
        sugar_val = totals["sugar"] if has_logged else None

        if existing:
            date_pretty = (existing.get("date") or "").strip() or _date_pretty_from_iso(date_iso)
            cur.execute(
                """
                UPDATE weight_logs
                SET date=?, kcal=?, protein=?, carbs=?, fat=?, sugar=?, raw_json=?
                WHERE id=?
                """,
                (date_pretty, kcal_val, p_val, c_val, f_val, sugar_val, raw_text, int(existing["id"])),
            )
        elif has_logged:
            cur.execute(
                """
                INSERT INTO weight_logs
                    (date, date_iso, weight_kg, kcal, protein, carbs, fat, sugar, created_at, raw_json)
                VALUES (?, ?, NULL, ?, ?, ?, ?, ?, datetime('now'), ?)
                """,
                (_date_pretty_from_iso(date_iso), date_iso, kcal_val, p_val, c_val, f_val, sugar_val, raw_text),
            )
    except Exception:
        # Never break meals logging if makros-sync fails.
        return


def get_logging_day_payload(date_iso: Optional[str] = None) -> Dict[str, Any]:
    normalized_date = _normalize_date_iso(date_iso)
    planned_meals, target_from_plan = _planned_meals_for_date(normalized_date)
    conn = get_nutrition_db()
    cur = conn.cursor()
    status_rows = cur.execute(
        """
        SELECT slot_id, status, shifted_time_text, logged_meal_id, status_source, reason_code, trace_json
        FROM nutrition_planned_meal_status
        WHERE log_date=?
        """,
        (normalized_date,),
    ).fetchall()
    status_by_slot = {int(row["slot_id"]): dict(row) for row in status_rows}
    logged = _load_logged_meals_for_date(cur, normalized_date)
    logged_by_id = {int(item["id"]): item for item in logged}
    planned_out: List[Dict[str, Any]] = []
    for planned in planned_meals:
        state_row = status_by_slot.get(int(planned["slot_id"])) or {}
        status = state_row.get("status") if state_row.get("status") in LOGGING_STATUS_VALUES else "open"
        linked_log = logged_by_id.get(int(state_row.get("logged_meal_id") or 0))
        trace_obj = None
        if state_row.get("trace_json"):
            try:
                trace_obj = json.loads(state_row.get("trace_json"))
            except Exception:
                trace_obj = None
        planned_out.append(
            {
                **planned,
                "status": status,
                "shifted_time_text": state_row.get("shifted_time_text"),
                "logged_meal_id": state_row.get("logged_meal_id"),
                "logged_meal": linked_log,
                "status_source": state_row.get("status_source") or "user",
                "reason_code": state_row.get("reason_code"),
                "trace": trace_obj,
            }
        )
    planned_totals = _sum_macros(planned_out)
    logged_totals = _sum_macros(logged)
    targets = get_live_macro_targets(initialize_schema=False)
    if target_from_plan and isinstance(target_from_plan, dict):
        for key in ("kcal", "p", "c", "f"):
            if target_from_plan.get(key) is not None:
                targets[key] = float(target_from_plan.get(key))
    remaining = {
        "kcal": round((targets.get("kcal") or 0.0) - logged_totals["kcal"], 1),
        "p": round((targets.get("p") or 0.0) - logged_totals["p"], 1),
        "c": round((targets.get("c") or 0.0) - logged_totals["c"], 1),
        "f": round((targets.get("f") or 0.0) - logged_totals["f"], 1),
    }
    out = {
        "ok": True,
        "date": normalized_date,
        "targets": targets,
        "planned_totals": planned_totals,
        "logged_totals": logged_totals,
        "remaining": remaining,
        "planned_meals": planned_out,
        "logged_meals": logged,
    }
    # This is a read projection. Meal mutations reconcile weight_logs before
    # they commit; synchronizing here made every dashboard/Telegram/MCP read
    # an implicit write and could create a weight_logs row merely by viewing a
    # day.
    conn.close()
    return out


def _get_planned_slot_by_id(date_iso: str, slot_id: int) -> Optional[Dict[str, Any]]:
    meals, _ = _planned_meals_for_date(date_iso)
    for meal in meals:
        if int(meal.get("slot_id") or 0) == int(slot_id):
            return meal
    return None


def log_planned_meal(
    slot_id: int,
    *,
    date_iso: Optional[str] = None,
    time_mode: str = "now",
    custom_time_text: Optional[str] = None,
    custom_logged_at: Optional[str] = None,
    items: Optional[List[Dict[str, Any]]] = None,
    title: Optional[str] = None,
    meal_slot: Optional[str] = None,
    status_source: str = "user",
    reason_code: Optional[str] = None,
    trace: Optional[Dict[str, Any]] = None,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    ensure_nutrition_planning_schema()
    normalized_date = _normalize_date_iso(date_iso)
    planned = _get_planned_slot_by_id(normalized_date, int(slot_id))
    if not planned:
        return None, "slot_not_found"
    source_items = _normalize_logging_items(items if items is not None else (planned.get("items") or []))
    if not source_items:
        return None, "items_required"
    final_title = (title or planned.get("title") or "Meal").strip()
    final_meal_slot = _normalize_meal_slot(meal_slot or planned.get("meal_slot"))
    mode = (time_mode or "now").strip().lower()
    planned_time = custom_time_text if mode == "custom" else planned.get("time_text")
    logged_at = _resolve_logged_at(normalized_date, time_mode=mode, planned_time_text=planned_time, custom_logged_at=custom_logged_at)
    conn = get_nutrition_db()
    cur = conn.cursor()
    now = _utcnow_iso()
    is_changed = _is_kcal_changed_vs_planned(
        cur,
        date_iso=normalized_date,
        slot_id=int(slot_id),
        items=source_items,
    )
    cur.execute(
        """
        INSERT INTO nutrition_logged_meals
            (log_date, slot_id, title, meal_slot, source, created_from_planned_slot_id, created_from_template_id,
             edited_from_template, original_planned_time, logged_at, is_favorite, created_at, updated_at)
        VALUES (?, ?, ?, ?, 'planned', ?, ?, ?, ?, ?, 0, ?, ?)
        """,
        (
            normalized_date,
            int(slot_id),
            final_title,
            final_meal_slot,
            int(slot_id),
            planned.get("meal_template_id"),
            1 if is_changed else 0,
            planned.get("time_text"),
            logged_at,
            now,
            now,
        ),
    )
    logged_meal_id = int(cur.lastrowid)
    _insert_logged_meal_items(cur, logged_meal_id, source_items)
    _renumber_logged_meals_for_date(cur, normalized_date)
    final_title, final_meal_slot = _logged_meal_title_slot(cur, logged_meal_id)
    if is_changed:
        status = "manual_override" if (status_source or "").strip().lower() != "core" else "adjusted_by_core"
    else:
        status = "telegram_confirmed" if (status_source or "").strip().lower() == "telegram" else "logged"
    _set_planned_status(
        cur,
        date_iso=normalized_date,
        slot_id=int(slot_id),
        status=status,
        shifted_time_text=None,
        logged_meal_id=logged_meal_id,
        status_source=status_source,
        reason_code=reason_code,
        trace_json=trace,
    )
    _record_recent_foods(cur, items=source_items, meal_slot=final_meal_slot, log_date=normalized_date, logged_at=logged_at)
    _record_recent_meal(cur, title=final_title, meal_slot=final_meal_slot, items=source_items, log_date=normalized_date, logged_at=logged_at)
    _sync_logging_totals_to_weight_logs(cur, normalized_date)
    conn.commit()
    conn.close()
    return get_logging_day_payload(normalized_date), None


def set_planned_meal_status(
    slot_id: int,
    *,
    date_iso: Optional[str] = None,
    status: str = "open",
    shifted_time_text: Optional[str] = None,
    status_source: str = "user",
    reason_code: Optional[str] = None,
    trace: Optional[Dict[str, Any]] = None,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    ensure_nutrition_planning_schema()
    normalized_date = _normalize_date_iso(date_iso)
    if status not in LOGGING_STATUS_VALUES:
        return None, "invalid_status"
    planned = _get_planned_slot_by_id(normalized_date, int(slot_id))
    if not planned:
        return None, "slot_not_found"
    conn = get_nutrition_db()
    cur = conn.cursor()
    normalized_shifted = _safe_time_text(shifted_time_text) if status == "shifted" else None
    _set_planned_status(
        cur,
        date_iso=normalized_date,
        slot_id=int(slot_id),
        status=status,
        shifted_time_text=normalized_shifted,
        logged_meal_id=None,
        status_source=status_source,
        reason_code=reason_code,
        trace_json=trace,
    )
    conn.commit()
    conn.close()
    return get_logging_day_payload(normalized_date), None


def create_free_logged_meal(
    *,
    date_iso: Optional[str] = None,
    title: Optional[str] = None,
    meal_slot: Optional[str] = None,
    logged_at: Optional[str] = None,
    items: Optional[List[Dict[str, Any]]] = None,
    source: str = "free",
    mark_favorite: bool = False,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    ensure_nutrition_planning_schema()
    normalized_date = _normalize_date_iso(date_iso)
    cleaned_items = _normalize_logging_items(items or [])
    if not cleaned_items:
        return None, "items_required"
    final_title = ((title or "").strip() or "Freies Meal")
    final_slot = _normalize_meal_slot(meal_slot or "frei")
    final_logged_at = _resolve_logged_at(normalized_date, time_mode="custom", planned_time_text=_time_from_iso(logged_at), custom_logged_at=logged_at)
    conn = get_nutrition_db()
    cur = conn.cursor()
    now = _utcnow_iso()
    cur.execute(
        """
        INSERT INTO nutrition_logged_meals
            (log_date, slot_id, title, meal_slot, source, created_from_planned_slot_id, created_from_template_id,
             edited_from_template, original_planned_time, logged_at, is_favorite, created_at, updated_at)
        VALUES (?, NULL, ?, ?, ?, NULL, NULL, 0, NULL, ?, ?, ?, ?)
        """,
        (normalized_date, final_title, final_slot, (source or "free"), final_logged_at, 1 if mark_favorite else 0, now, now),
    )
    logged_meal_id = int(cur.lastrowid)
    _insert_logged_meal_items(cur, logged_meal_id, cleaned_items)
    _renumber_logged_meals_for_date(cur, normalized_date)
    final_title, final_slot = _logged_meal_title_slot(cur, logged_meal_id)
    _record_recent_foods(cur, items=cleaned_items, meal_slot=final_slot, log_date=normalized_date, logged_at=final_logged_at)
    _record_recent_meal(cur, title=final_title, meal_slot=final_slot, items=cleaned_items, log_date=normalized_date, logged_at=final_logged_at)
    _sync_logging_totals_to_weight_logs(cur, normalized_date)
    conn.commit()
    conn.close()
    return get_logging_day_payload(normalized_date), None


def update_logged_meal(
    logged_meal_id: int,
    payload: Dict[str, Any],
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    ensure_nutrition_planning_schema()
    conn = get_nutrition_db()
    cur = conn.cursor()
    row = cur.execute("SELECT * FROM nutrition_logged_meals WHERE id=?", (int(logged_meal_id),)).fetchone()
    if not row:
        conn.close()
        return None, "not_found"
    updated_title = (payload.get("title") if "title" in payload else row["title"]) or "Meal"
    updated_slot = _normalize_meal_slot(payload.get("meal_slot") if "meal_slot" in payload else row["meal_slot"])
    updated_logged_at = payload.get("logged_at") if "logged_at" in payload else row["logged_at"]
    try:
        updated_logged_at = datetime.fromisoformat(str(updated_logged_at)).replace(microsecond=0).isoformat()
    except Exception:
        updated_logged_at = row["logged_at"]
    updated_date = _normalize_date_iso(payload.get("log_date") if "log_date" in payload else row["log_date"])
    mark_favorite = bool(payload.get("is_favorite")) if "is_favorite" in payload else bool(row["is_favorite"])
    cur.execute(
        """
        UPDATE nutrition_logged_meals
        SET log_date=?, title=?, meal_slot=?, logged_at=?, is_favorite=?, updated_at=?
        WHERE id=?
        """,
        (updated_date, updated_title.strip() or "Meal", updated_slot, updated_logged_at, 1 if mark_favorite else 0, _utcnow_iso(), int(logged_meal_id)),
    )
    if "items" in payload:
        cleaned_items = _normalize_logging_items(payload.get("items") or [])
        if not cleaned_items:
            conn.rollback()
            conn.close()
            return None, "items_required"
        cur.execute("DELETE FROM nutrition_logged_meal_items WHERE logged_meal_id=?", (int(logged_meal_id),))
        _insert_logged_meal_items(cur, int(logged_meal_id), cleaned_items)
    changed_flag = False
    source_is_planned = str(row["source"] or "").strip().lower() == "planned"
    planned_slot_id = int(row["created_from_planned_slot_id"]) if row["created_from_planned_slot_id"] else None
    planned_slot_exists = False
    if planned_slot_id is not None:
        planned_slot_exists = cur.execute(
            "SELECT 1 FROM nutrition_week_day_slots WHERE id=? LIMIT 1",
            (planned_slot_id,),
        ).fetchone() is not None
        if not planned_slot_exists:
            # The meal was originally logged from a planned slot that no longer exists.
            # Detach it so future edits behave like a normal logged meal instead of
            # crashing when trying to update planned status.
            cur.execute(
                "UPDATE nutrition_logged_meals SET created_from_planned_slot_id=NULL, edited_from_template=1, updated_at=? WHERE id=?",
                (_utcnow_iso(), int(logged_meal_id)),
            )

    resolved_planned_slot_id = planned_slot_id if planned_slot_exists else None
    if resolved_planned_slot_id is None and source_is_planned:
        refreshed_row = cur.execute("SELECT * FROM nutrition_logged_meals WHERE id=?", (int(logged_meal_id),)).fetchone()
        if refreshed_row is not None:
            refreshed_dict = dict(refreshed_row)
            refreshed_dict["log_date"] = updated_date
            refreshed_dict["logged_at"] = updated_logged_at
            refreshed_dict["title"] = updated_title.strip() or "Meal"
            refreshed_dict["meal_slot"] = updated_slot
            resolved_planned_slot_id = _resolve_current_planned_slot_for_logged_meal(cur, refreshed_dict)

    if resolved_planned_slot_id is not None:
        meal_items = _serialize_logged_meal_items(cur, [int(logged_meal_id)]).get(int(logged_meal_id), [])
        status, shifted_time_text, reason_code = _planned_status_for_logged_meal(
            cur,
            date_iso=updated_date,
            slot_id=int(resolved_planned_slot_id),
            logged_meal_id=int(logged_meal_id),
            row={
                **dict(row),
                "log_date": updated_date,
                "logged_at": updated_logged_at,
                "title": updated_title.strip() or "Meal",
                "meal_slot": updated_slot,
            },
            items=meal_items,
        )
        changed_flag = status == "manual_override"
        cur.execute(
            "UPDATE nutrition_logged_meals SET created_from_planned_slot_id=?, edited_from_template=?, updated_at=? WHERE id=?",
            (int(resolved_planned_slot_id), 1 if changed_flag else 0, _utcnow_iso(), int(logged_meal_id)),
        )
        _set_planned_status(
            cur,
            date_iso=updated_date,
            slot_id=int(resolved_planned_slot_id),
            status=status,
            shifted_time_text=shifted_time_text,
            logged_meal_id=int(logged_meal_id),
            status_source="user",
            reason_code=reason_code,
        )
    elif source_is_planned:
        changed_flag = True
        cur.execute(
            "UPDATE nutrition_logged_meals SET created_from_planned_slot_id=NULL, edited_from_template=1, updated_at=? WHERE id=?",
            (_utcnow_iso(), int(logged_meal_id)),
        )
    old_date = str(row["log_date"] or "")
    if old_date and old_date != updated_date:
        _renumber_logged_meals_for_date(cur, old_date)
    _renumber_logged_meals_for_date(cur, updated_date)
    updated_title, updated_slot = _logged_meal_title_slot(cur, int(logged_meal_id))
    if "items" in payload:
        _record_recent_foods(cur, items=cleaned_items, meal_slot=updated_slot, log_date=updated_date, logged_at=updated_logged_at)
        _record_recent_meal(cur, title=updated_title.strip() or "Meal", meal_slot=updated_slot, items=cleaned_items, log_date=updated_date, logged_at=updated_logged_at)
    if old_date:
        _sync_logging_totals_to_weight_logs(cur, old_date)
    _sync_logging_totals_to_weight_logs(cur, updated_date)
    conn.commit()
    conn.close()
    return get_logging_day_payload(updated_date), None


def delete_logged_meal(logged_meal_id: int) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    ensure_nutrition_planning_schema()
    conn = get_nutrition_db()
    cur = conn.cursor()
    row = cur.execute("SELECT * FROM nutrition_logged_meals WHERE id=?", (int(logged_meal_id),)).fetchone()
    if not row:
        conn.close()
        return None, "not_found"
    date_iso = row["log_date"]
    planned_slot_id = row["created_from_planned_slot_id"]
    cur.execute("DELETE FROM nutrition_logged_meals WHERE id=?", (int(logged_meal_id),))
    _renumber_logged_meals_for_date(cur, date_iso)
    if planned_slot_id:
        _set_planned_status(cur, date_iso=date_iso, slot_id=int(planned_slot_id), status="open", shifted_time_text=None, logged_meal_id=None)
    _sync_logging_totals_to_weight_logs(cur, date_iso)
    conn.commit()
    conn.close()
    return get_logging_day_payload(date_iso), None


def duplicate_logged_meal(logged_meal_id: int, *, date_iso: Optional[str] = None) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    ensure_nutrition_planning_schema()
    conn = get_nutrition_db()
    cur = conn.cursor()
    row = cur.execute("SELECT * FROM nutrition_logged_meals WHERE id=?", (int(logged_meal_id),)).fetchone()
    if not row:
        conn.close()
        return None, "not_found"
    item_rows = cur.execute(
        "SELECT * FROM nutrition_logged_meal_items WHERE logged_meal_id=? ORDER BY sort_index, id",
        (int(logged_meal_id),),
    ).fetchall()
    payload_items = []
    for item in item_rows:
        item_data = dict(item)
        payload_items.append(
            {
                "item_type": item_data.get("item_type"),
                "food_id": item_data.get("food_id"),
                "food_name": item_data.get("food_name"),
                "amount": item_data.get("amount"),
                "unit": item_data.get("unit"),
                "calories": item_data.get("calories"),
            }
        )
    conn.close()
    return create_free_logged_meal(
        date_iso=date_iso or row["log_date"],
        title=(row["title"] or "Meal") + " (Copy)",
        meal_slot=row["meal_slot"] or "frei",
        items=payload_items,
        source="duplicate",
    )


def copy_logged_meals_to_date(
    logged_meal_ids: List[int],
    *,
    target_date: Optional[str] = None,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Copy selected real logged meals to another day, preserving titles, items and times."""
    ensure_nutrition_planning_schema()
    normalized_target = _normalize_date_iso(target_date)
    ids: List[int] = []
    for value in logged_meal_ids or []:
        try:
            meal_id = int(value)
        except (TypeError, ValueError):
            continue
        if meal_id > 0 and meal_id not in ids:
            ids.append(meal_id)
        if len(ids) == 30:
            break
    if not ids:
        return None, "meal_ids_required"

    conn = get_nutrition_db()
    cur = conn.cursor()
    placeholders = ",".join("?" for _ in ids)
    rows = cur.execute(
        f"SELECT id, log_date FROM nutrition_logged_meals WHERE id IN ({placeholders})",
        ids,
    ).fetchall()
    source_dates = list(dict.fromkeys(str(row["log_date"]) for row in rows))
    meals_by_id: Dict[int, Dict[str, Any]] = {}
    for source_date in source_dates:
        for meal in _load_logged_meals_for_date(cur, source_date):
            meals_by_id[int(meal["id"])] = meal
    conn.close()

    if any(meal_id not in meals_by_id for meal_id in ids):
        return None, "not_found"

    target_day = get_logging_day_payload(normalized_target)
    open_targets = [
        meal for meal in (target_day.get("planned_meals") or [])
        if str(meal.get("status") or "open").lower() not in {
            "logged", "changed", "telegram_confirmed", "manual_override", "skipped"
        }
        and not meal.get("logged_meal_id")
    ]
    claimed_slot_ids: set[int] = set()

    def matching_target(source_meal: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        source_foods = {int(item["food_id"]) for item in (source_meal.get("items") or []) if item.get("food_id")}
        source_title = str(source_meal.get("title") or "").strip().lower()
        source_time = _parse_time_minutes(source_meal.get("time_text"))
        candidates: List[Tuple[float, Dict[str, Any]]] = []
        for target in open_targets:
            slot_id = int(target.get("slot_id") or 0)
            if not slot_id or slot_id in claimed_slot_ids:
                continue
            target_foods = {int(item["food_id"]) for item in (target.get("items") or []) if item.get("food_id")}
            overlap = len(source_foods & target_foods) / max(1, len(source_foods | target_foods))
            target_title = str(target.get("title") or "").strip().lower()
            title_match = bool(source_title and target_title and (source_title in target_title or target_title in source_title))
            target_time = _parse_time_minutes(target.get("time_text"))
            time_delta = abs(source_time - target_time) if source_time is not None and target_time is not None else 10_000
            # Food identity is strongest. A title relationship is accepted only
            # near the same time, so generic labels such as "Schule" cannot
            # accidentally consume a later, unrelated planned slot.
            if overlap < 0.45 and not (title_match and time_delta <= 120):
                continue
            score = (overlap * 100.0) + (25.0 if title_match else 0.0) + max(0.0, 30.0 - (time_delta / 4.0))
            candidates.append((score, target))
        if not candidates:
            return None
        candidates.sort(key=lambda entry: entry[0], reverse=True)
        if len(candidates) > 1 and abs(candidates[0][0] - candidates[1][0]) < 0.01:
            return None
        return candidates[0][1]

    for meal_id in ids:
        meal = meals_by_id[meal_id]
        time_text = _safe_time_text(meal.get("time_text")) or "12:00"
        target = matching_target(meal)
        if target:
            slot_id = int(target["slot_id"])
            claimed_slot_ids.add(slot_id)
            _, err = log_planned_meal(
                slot_id,
                date_iso=normalized_target,
                time_mode="custom",
                custom_time_text=time_text,
                custom_logged_at=f"{normalized_target}T{time_text}:00",
                items=meal.get("items") or [],
                title=meal.get("title") or target.get("title") or "Meal",
                meal_slot=target.get("meal_slot") or meal.get("meal_slot") or "frei",
                status_source="user",
                reason_code="COPIED_FROM_DAY",
                trace={"copied_from_logged_meal_id": meal_id},
            )
        else:
            _, err = create_free_logged_meal(
                date_iso=normalized_target,
                title=meal.get("title") or "Meal",
                meal_slot=meal.get("meal_slot") or "frei",
                logged_at=f"{normalized_target}T{time_text}:00",
                items=meal.get("items") or [],
                source="day_copy",
            )
        if err:
            return None, err
    return get_logging_day_payload(normalized_target), None


def search_logging_foods(query: Optional[str], *, limit: int = 25) -> List[Dict[str, Any]]:
    ensure_nutrition_planning_schema()
    q = (query or "").strip().lower()
    conn = get_nutrition_db()
    cur = conn.cursor()
    fetch_limit = max(25, min(150, int(limit) * 5))
    params: List[Any] = []
    where_clauses = ["COALESCE(f.is_active, 1) = 1"]
    if q:
        like = f"%{q}%"
        prefix = f"{q}%"
        where_clauses.append(
            """
            (
                LOWER(COALESCE(f.name, '')) LIKE ?
                OR LOWER(COALESCE(f.brand, '')) LIKE ?
                OR LOWER(COALESCE(f.mfp_search_hint, '')) LIKE ?
                OR LOWER(COALESCE(f.name_normalized, '')) LIKE ?
            )
            """
        )
        params.extend([like, like, like, prefix])
    where_sql = " AND ".join(where_clauses)
    rows = cur.execute(
        f"""
        SELECT f.*,
               COALESCE(r.use_count, 0) AS recent_use_count,
               COALESCE(r.today_count, 0) AS recent_today_count,
               r.last_amount AS recent_last_amount,
               r.last_unit AS recent_last_unit,
               r.last_meal_slot AS recent_last_meal_slot,
               r.last_logged_at AS recent_last_logged_at
        FROM nutrition_foods f
        LEFT JOIN nutrition_logging_recent_foods r ON r.food_id=f.id
        WHERE {where_sql}
        ORDER BY f.is_favorite DESC, COALESCE(r.today_count, 0) DESC, COALESCE(r.use_count, 0) DESC, f.name ASC
        LIMIT ?
        """,
        tuple(params + [fetch_limit]),
    ).fetchall()
    conn.close()
    scored: List[Tuple[Tuple[Any, ...], Dict[str, Any]]] = []
    for row in rows:
        data = dict(row)
        name = (data.get("name") or "").lower()
        brand = (data.get("brand") or "").lower()
        hint = (data.get("mfp_search_hint") or "").lower()
        normalized_name = (data.get("name_normalized") or "").lower()
        if q:
            if q in name:
                match_score = 2
            elif q in brand or q in hint:
                match_score = 1
            else:
                name_tokens = name.split()
                normalized_tokens = normalized_name.split()
                match_score = 1 if any(token.startswith(q) for token in name_tokens + normalized_tokens) else 0
            if match_score == 0:
                continue
        else:
            match_score = 1
        rank = (
            -match_score,
            -(1 if data.get("is_favorite") else 0),
            -int(data.get("recent_today_count") or 0),
            -int(data.get("recent_use_count") or 0),
            name,
        )
        scored.append((rank, data))
    scored.sort(key=lambda item: item[0])
    out: List[Dict[str, Any]] = []
    for _, row in scored[: max(1, min(100, int(limit)))]:
        out.append(
            {
                "id": row.get("id"),
                "name": row.get("name"),
                "brand": row.get("brand"),
                "unit_default": row.get("unit_default") or "g",
                "common_portion_size": row.get("common_portion_size"),
                "portion_g": row.get("portion_g"),
                "kcal_per_100": row.get("kcal_per_100") or 0.0,
                "p_per_100": row.get("p_per_100") or 0.0,
                "c_per_100": row.get("c_per_100") or 0.0,
                "f_per_100": row.get("f_per_100") or 0.0,
                "is_favorite": bool(row.get("is_favorite")),
                "recent": {
                    "use_count": int(row.get("recent_use_count") or 0),
                    "today_count": int(row.get("recent_today_count") or 0),
                    "last_amount": row.get("recent_last_amount"),
                    "last_unit": row.get("recent_last_unit"),
                    "last_meal_slot": row.get("recent_last_meal_slot"),
                    "last_logged_at": row.get("recent_last_logged_at"),
                },
            }
        )
    return out


def get_logging_recents(*, limit: int = 12) -> Dict[str, Any]:
    ensure_nutrition_planning_schema()
    conn = get_nutrition_db()
    cur = conn.cursor()
    food_rows = cur.execute(
        """
        SELECT f.id, f.name, f.unit_default, f.kcal_per_100, f.p_per_100, f.c_per_100, f.f_per_100,
               r.last_amount, r.last_unit, r.last_meal_slot, r.use_count, r.today_count, r.last_logged_at
        FROM nutrition_logging_recent_foods r
        JOIN nutrition_foods f ON f.id=r.food_id
        WHERE COALESCE(f.is_active, 1) = 1
        ORDER BY r.last_logged_at DESC
        LIMIT ?
        """,
        (max(1, min(60, int(limit))),),
    ).fetchall()
    meal_rows = cur.execute(
        """
        SELECT meal_key, title, meal_slot, payload_json, use_count, today_count, last_logged_at
        FROM nutrition_logging_recent_meals
        ORDER BY last_logged_at DESC
        LIMIT ?
        """,
        (max(1, min(60, int(limit))),),
    ).fetchall()
    conn.close()
    recent_foods = [dict(row) for row in food_rows]
    recent_meals: List[Dict[str, Any]] = []
    for row in meal_rows:
        data = dict(row)
        try:
            payload = json.loads(data.get("payload_json") or "{}")
        except Exception:
            payload = {}
        recent_meals.append(
            {
                "meal_key": data.get("meal_key"),
                "title": data.get("title"),
                "meal_slot": data.get("meal_slot"),
                "use_count": data.get("use_count") or 0,
                "today_count": data.get("today_count") or 0,
                "last_logged_at": data.get("last_logged_at"),
                "items": payload.get("items") or [],
            }
        )
    return {"recent_foods": recent_foods, "recent_meals": recent_meals}


def toggle_food_favorite(food_id: int, *, value: Optional[bool] = None) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    ensure_nutrition_planning_schema()
    conn = get_nutrition_db()
    cur = conn.cursor()
    row = cur.execute("SELECT * FROM nutrition_foods WHERE id=?", (int(food_id),)).fetchone()
    if not row:
        conn.close()
        return None, "not_found"
    current = bool(row["is_favorite"])
    desired = (not current) if value is None else bool(value)
    cur.execute(
        "UPDATE nutrition_foods SET is_favorite=?, updated_at=? WHERE id=?",
        (1 if desired else 0, _utcnow_iso(), int(food_id)),
    )
    conn.commit()
    updated = cur.execute("SELECT * FROM nutrition_foods WHERE id=?", (int(food_id),)).fetchone()
    conn.close()
    return (dict(updated) if updated else None), None


def create_recent_meal_log(
    meal_key: str,
    *,
    date_iso: Optional[str] = None,
    logged_at: Optional[str] = None,
    meal_slot: Optional[str] = None,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    ensure_nutrition_planning_schema()
    conn = get_nutrition_db()
    cur = conn.cursor()
    row = cur.execute(
        "SELECT title, meal_slot, payload_json FROM nutrition_logging_recent_meals WHERE meal_key=?",
        ((meal_key or "").strip(),),
    ).fetchone()
    conn.close()
    if not row:
        return None, "not_found"
    row_data = dict(row)
    try:
        payload = json.loads(row_data.get("payload_json") or "{}")
    except Exception:
        payload = {}
    return create_free_logged_meal(
        date_iso=date_iso,
        title=row_data.get("title") or payload.get("title") or "Meal",
        meal_slot=meal_slot or row_data.get("meal_slot") or payload.get("meal_slot") or "frei",
        logged_at=logged_at,
        items=payload.get("items") or [],
        source="recent",
    )
