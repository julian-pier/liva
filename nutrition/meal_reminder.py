#!/usr/bin/env python3
"""
Meal Reminder (SQLite-backed)

Reads the current day's meals from LIVA's nutrition SQLite database
and sends a Telegram reminder 15 minutes before each meal time.
Designed to run every minute via cron.
"""

import json
import os
import re
import sys
import sqlite3
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo

from integrations.telegram_hub import resolve_domain_config, send_domain_message

# === Config (adjust here if your DB schema/paths differ) ===
DEFAULT_DB_PATH = "/opt/liva/database/ernaehrung.sqlite3"
DEFAULT_TRAINING_DB_PATH = os.environ.get(
    "TRAINING_DB_PATH",
    "/opt/liva/database/training.sqlite3",
)
DEFAULT_STATE_PATH = "/var/lib/liva/.cache/meal_reminder/state.json"
KEEP_DAYS = 14
DB_PATH = DEFAULT_DB_PATH
TELEGRAM_MEAL_PUSH_MIN_INDEX = 1
TELEGRAM_MEAL_PUSH_MAX_INDEX = 8

# Table/column mapping (adjust if your schema differs)
TABLE_WEEK_TEMPLATES = "nutrition_week_templates"
TABLE_WEEK_TEMPLATE_DAYS = "nutrition_week_template_days"
TABLE_WEEK_DAY_SLOTS = "nutrition_week_day_slots"
TABLE_SLOT_TEMPLATES = "nutrition_slot_templates"
TABLE_SLOT_MEALS = "nutrition_slot_meals"
TABLE_SLOT_MEAL_ITEMS = "nutrition_slot_meal_items"
TABLE_SLOT_MEAL_OVERRIDES = "nutrition_slot_meal_overrides"
TABLE_MEAL_TEMPLATES = "nutrition_meal_templates"
TABLE_MEAL_INGREDIENTS = "nutrition_meal_ingredients"
TABLE_FOODS = "nutrition_foods"

COL_WEEK_TEMPLATES_ID = "id"
COL_WEEK_TEMPLATES_IS_ACTIVE = "is_active"
COL_WEEK_TEMPLATES_UPDATED_AT = "updated_at"

COL_WEEK_TEMPLATE_DAYS_ID = "id"
COL_WEEK_TEMPLATE_DAYS_TEMPLATE_ID = "week_template_id"
COL_WEEK_TEMPLATE_DAYS_WEEKDAY = "weekday"

COL_WEEK_DAY_SLOTS_ID = "id"
COL_WEEK_DAY_SLOTS_DAY_ID = "week_template_day_id"
COL_WEEK_DAY_SLOTS_SLOT_TEMPLATE_ID = "slot_template_id"
COL_WEEK_DAY_SLOTS_SLOT_INDEX = "slot_index"
COL_WEEK_DAY_SLOTS_TIME_TEXT = "time_text"
COL_WEEK_DAY_SLOTS_CUSTOM_TITLE = "custom_title"
COL_WEEK_DAY_SLOTS_MEAL_TEMPLATE_ID = "meal_template_id"

COL_SLOT_TEMPLATES_ID = "id"
COL_SLOT_TEMPLATES_TITLE = "title"
COL_SLOT_TEMPLATES_DEFAULT_TIME = "default_time"

COL_SLOT_MEALS_ID = "id"
COL_SLOT_MEALS_DAY_ID = "week_template_day_id"
COL_SLOT_MEALS_SLOT_INDEX = "slot_index"
COL_SLOT_MEALS_TITLE = "meal_title"

COL_SLOT_MEAL_ITEMS_ID = "id"
COL_SLOT_MEAL_ITEMS_SLOT_MEAL_ID = "slot_meal_id"
COL_SLOT_MEAL_ITEMS_AMOUNT = "amount"
COL_SLOT_MEAL_ITEMS_UNIT = "unit"
COL_SLOT_MEAL_ITEMS_NAME_RAW = "name_raw"
COL_SLOT_MEAL_ITEMS_FOOD_ID = "food_id"
COL_SLOT_MEAL_ITEMS_TYPE = "item_type"
COL_SLOT_MEAL_ITEMS_CALORIES = "calories"

COL_SLOT_MEAL_OVERRIDES_SLOT_ID = "slot_id"
COL_SLOT_MEAL_OVERRIDES_MEAL_TEMPLATE_ID = "meal_template_id"
COL_SLOT_MEAL_OVERRIDES_INGREDIENTS_JSON = "ingredients_json"

COL_MEAL_TEMPLATES_ID = "id"
COL_MEAL_TEMPLATES_TITLE = "title"

COL_MEAL_INGREDIENTS_MEAL_ID = "meal_template_id"
COL_MEAL_INGREDIENTS_FOOD_ID = "food_id"
COL_MEAL_INGREDIENTS_AMOUNT = "amount"
COL_MEAL_INGREDIENTS_UNIT = "unit"
COL_MEAL_INGREDIENTS_SORT = "sort_index"

COL_FOODS_ID = "id"
COL_FOODS_NAME = "name"

# Timezone for the plan (adjust if your system uses a different TZ)
DEFAULT_TIMEZONE = "Europe/Berlin"
LEGACY_ENABLE_ENV = "MEAL_REMINDER_LEGACY_ENABLED"


def _read_setting_int(training_db_path: str, key: str) -> int | None:
    try:
        conn = _connect_ro(training_db_path)
        cur = conn.cursor()
        exists = cur.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='settings_kv' LIMIT 1"
        ).fetchone()
        if not exists:
            conn.close()
            return None
        row = cur.execute(
            "SELECT value FROM settings_kv WHERE key=? LIMIT 1",
            (key,),
        ).fetchone()
        conn.close()
    except Exception:
        return None
    if not row or row[0] in (None, ""):
        return None
    try:
        value = int(str(row[0]).strip())
    except Exception:
        return None
    return value if value > 0 else None


def telegram_pushes_muted(training_db_path: str, now_ts: int) -> bool:
    muted_until = _read_setting_int(training_db_path, "telegram_push_muted_until_ts")
    unmuted_until = _read_setting_int(training_db_path, "telegram_push_unmuted_until_ts")
    muted_active = bool(muted_until and muted_until >= now_ts)
    unmuted_active = bool(unmuted_until and unmuted_until >= now_ts)
    return muted_active and not unmuted_active


def _read_setting_text(training_db_path: str, key: str) -> str | None:
    try:
        conn = _connect_ro(training_db_path)
        cur = conn.cursor()
        exists = cur.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='settings_kv' LIMIT 1"
        ).fetchone()
        if not exists:
            conn.close()
            return None
        row = cur.execute(
            "SELECT value FROM settings_kv WHERE key=? LIMIT 1",
            (key,),
        ).fetchone()
        conn.close()
    except Exception:
        return None
    if not row or row[0] in (None, ""):
        return None
    return str(row[0]).strip()


def _parse_setting_bool(raw: str | None, default: bool = True) -> bool:
    if raw is None:
        return default
    text = str(raw).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


def telegram_push_for_meal_enabled(training_db_path: str, meal_number: int | None) -> bool:
    if meal_number is None:
        return True
    idx = int(meal_number)
    if idx < TELEGRAM_MEAL_PUSH_MIN_INDEX or idx > TELEGRAM_MEAL_PUSH_MAX_INDEX:
        return True
    key = f"telegram_push_meal_{idx}_enabled"
    raw = _read_setting_text(training_db_path, key)
    return _parse_setting_bool(raw, default=True)


def resolve_meal_number(meal: dict) -> int | None:
    slot_index = meal.get("slot_index")
    try:
        if slot_index is not None:
            idx = int(slot_index) + 1
            return idx if idx > 0 else None
    except Exception:
        pass
    title = str(meal.get("title") or "").strip()
    m = re.search(r"\bmeal\s*(\d{1,2})\b", title, flags=re.IGNORECASE)
    if not m:
        return None
    try:
        idx = int(m.group(1))
    except Exception:
        return None
    return idx if idx > 0 else None


def load_state(path):
    """Load state (sent reminders) from disk."""
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        # If corrupted, start fresh
        return {}


def save_state(path, state):
    """Persist state to disk."""
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, path)


def build_message(meal):
    """Format the Telegram message."""
    title = meal.get("title", "(Ohne Titel)")
    items = meal.get("items", [])

    lines = [title, ""]
    for item in items:
        lines.append(f"• {item}")
    return "\n".join(lines)


def _format_amount(value):
    if value is None:
        return None
    try:
        num = float(value)
    except Exception:
        return str(value).strip()
    if num == 0:
        return None
    if num.is_integer():
        return str(int(num))
    return f"{num:g}"


def _format_item(name, amount, unit):
    name = (name or "").strip()
    if not name:
        return None
    amount_str = _format_amount(amount)
    unit_str = (unit or "").strip()
    if amount_str and unit_str:
        return f"{name} {amount_str} {unit_str}"
    if amount_str:
        return f"{name} {amount_str}"
    return name


def _check_table_and_columns(cur, table, required_columns):
    row = cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
    if not row:
        raise RuntimeError(f"Missing table: {table}")
    cols = cur.execute(f"PRAGMA table_info({table})").fetchall()
    col_names = {c[1] for c in cols}
    missing = [c for c in required_columns if c not in col_names]
    if missing:
        raise RuntimeError(f"Missing columns in {table}: {', '.join(missing)}")


def _connect_ro(db_path):
    uri = f"file:{db_path}?mode=ro"
    return sqlite3.connect(uri, uri=True)


def normalize_time_str(value):
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if len(text) >= 5:
        return text[:5]
    return None


def load_meals_from_sqlite(now: datetime, tz: ZoneInfo) -> list:
    """
    Load today's meals from the active LIVA nutrition plan.
    Returns a list of dicts: {"time": "HH:MM", "title": "...", "items": ["..."]}
    """
    conn = _connect_ro(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    # Validate required tables/columns
    _check_table_and_columns(cur, TABLE_WEEK_TEMPLATES, [COL_WEEK_TEMPLATES_ID, COL_WEEK_TEMPLATES_IS_ACTIVE, COL_WEEK_TEMPLATES_UPDATED_AT])
    _check_table_and_columns(cur, TABLE_WEEK_TEMPLATE_DAYS, [COL_WEEK_TEMPLATE_DAYS_ID, COL_WEEK_TEMPLATE_DAYS_TEMPLATE_ID, COL_WEEK_TEMPLATE_DAYS_WEEKDAY])
    _check_table_and_columns(cur, TABLE_WEEK_DAY_SLOTS, [
        COL_WEEK_DAY_SLOTS_ID,
        COL_WEEK_DAY_SLOTS_DAY_ID,
        COL_WEEK_DAY_SLOTS_SLOT_TEMPLATE_ID,
        COL_WEEK_DAY_SLOTS_SLOT_INDEX,
        COL_WEEK_DAY_SLOTS_TIME_TEXT,
        COL_WEEK_DAY_SLOTS_CUSTOM_TITLE,
        COL_WEEK_DAY_SLOTS_MEAL_TEMPLATE_ID,
    ])
    _check_table_and_columns(cur, TABLE_SLOT_TEMPLATES, [COL_SLOT_TEMPLATES_ID, COL_SLOT_TEMPLATES_TITLE, COL_SLOT_TEMPLATES_DEFAULT_TIME])
    _check_table_and_columns(cur, TABLE_SLOT_MEALS, [COL_SLOT_MEALS_ID, COL_SLOT_MEALS_DAY_ID, COL_SLOT_MEALS_SLOT_INDEX, COL_SLOT_MEALS_TITLE])
    _check_table_and_columns(cur, TABLE_SLOT_MEAL_ITEMS, [
        COL_SLOT_MEAL_ITEMS_ID,
        COL_SLOT_MEAL_ITEMS_SLOT_MEAL_ID,
        COL_SLOT_MEAL_ITEMS_AMOUNT,
        COL_SLOT_MEAL_ITEMS_UNIT,
        COL_SLOT_MEAL_ITEMS_NAME_RAW,
        COL_SLOT_MEAL_ITEMS_FOOD_ID,
        COL_SLOT_MEAL_ITEMS_TYPE,
        COL_SLOT_MEAL_ITEMS_CALORIES,
    ])
    _check_table_and_columns(cur, TABLE_SLOT_MEAL_OVERRIDES, [
        COL_SLOT_MEAL_OVERRIDES_SLOT_ID,
        COL_SLOT_MEAL_OVERRIDES_MEAL_TEMPLATE_ID,
        COL_SLOT_MEAL_OVERRIDES_INGREDIENTS_JSON,
    ])
    _check_table_and_columns(cur, TABLE_MEAL_TEMPLATES, [COL_MEAL_TEMPLATES_ID, COL_MEAL_TEMPLATES_TITLE])
    _check_table_and_columns(cur, TABLE_MEAL_INGREDIENTS, [
        COL_MEAL_INGREDIENTS_MEAL_ID,
        COL_MEAL_INGREDIENTS_FOOD_ID,
        COL_MEAL_INGREDIENTS_AMOUNT,
        COL_MEAL_INGREDIENTS_UNIT,
        COL_MEAL_INGREDIENTS_SORT,
    ])
    _check_table_and_columns(cur, TABLE_FOODS, [COL_FOODS_ID, COL_FOODS_NAME])

    # Active week template
    week_row = cur.execute(
        f"""
        SELECT {COL_WEEK_TEMPLATES_ID} AS id
        FROM {TABLE_WEEK_TEMPLATES}
        WHERE {COL_WEEK_TEMPLATES_IS_ACTIVE}=1
        ORDER BY {COL_WEEK_TEMPLATES_UPDATED_AT} DESC, {COL_WEEK_TEMPLATES_ID} DESC
        LIMIT 1
        """
    ).fetchone()
    if not week_row:
        conn.close()
        return []
    week_row = dict(week_row)

    weekday_idx = now.weekday()
    day_row = cur.execute(
        f"""
        SELECT {COL_WEEK_TEMPLATE_DAYS_ID} AS id
        FROM {TABLE_WEEK_TEMPLATE_DAYS}
        WHERE {COL_WEEK_TEMPLATE_DAYS_TEMPLATE_ID}=? AND {COL_WEEK_TEMPLATE_DAYS_WEEKDAY}=?
        LIMIT 1
        """,
        (week_row["id"], weekday_idx),
    ).fetchone()
    if not day_row:
        conn.close()
        return []
    day_row = dict(day_row)

    slots = cur.execute(
        f"""
        SELECT
            {COL_WEEK_DAY_SLOTS_ID} AS id,
            {COL_WEEK_DAY_SLOTS_DAY_ID} AS day_id,
            {COL_WEEK_DAY_SLOTS_SLOT_TEMPLATE_ID} AS slot_template_id,
            {COL_WEEK_DAY_SLOTS_SLOT_INDEX} AS slot_index,
            {COL_WEEK_DAY_SLOTS_TIME_TEXT} AS time_text,
            {COL_WEEK_DAY_SLOTS_CUSTOM_TITLE} AS custom_title,
            {COL_WEEK_DAY_SLOTS_MEAL_TEMPLATE_ID} AS meal_template_id
        FROM {TABLE_WEEK_DAY_SLOTS}
        WHERE {COL_WEEK_DAY_SLOTS_DAY_ID}=?
        ORDER BY {COL_WEEK_DAY_SLOTS_SLOT_INDEX} ASC
        """,
        (day_row["id"],),
    ).fetchall()

    if not slots:
        conn.close()
        return []
    slots = [dict(r) for r in slots]

    slot_template_ids = {row["slot_template_id"] for row in slots if row["slot_template_id"] is not None}
    slot_templates = {}
    if slot_template_ids:
        placeholders = ",".join("?" for _ in slot_template_ids)
        rows = cur.execute(
            f"SELECT {COL_SLOT_TEMPLATES_ID} AS id, {COL_SLOT_TEMPLATES_TITLE} AS title, {COL_SLOT_TEMPLATES_DEFAULT_TIME} AS default_time FROM {TABLE_SLOT_TEMPLATES} WHERE {COL_SLOT_TEMPLATES_ID} IN ({placeholders})",
            tuple(slot_template_ids),
        ).fetchall()
        rows = [dict(r) for r in rows]
        slot_templates = {row["id"]: row for row in rows}

    slot_meals = cur.execute(
        f"""
        SELECT {COL_SLOT_MEALS_ID} AS id, {COL_SLOT_MEALS_DAY_ID} AS day_id,
               {COL_SLOT_MEALS_SLOT_INDEX} AS slot_index, {COL_SLOT_MEALS_TITLE} AS meal_title
        FROM {TABLE_SLOT_MEALS}
        WHERE {COL_SLOT_MEALS_DAY_ID}=?
        """,
        (day_row["id"],),
    ).fetchall()
    slot_meals = [dict(r) for r in slot_meals]
    slot_meal_by_key = {(row["day_id"], row["slot_index"]): row for row in slot_meals}

    slot_ids = [row["id"] for row in slots]
    overrides_by_slot = {}
    if slot_ids:
        placeholders = ",".join("?" for _ in slot_ids)
        override_rows = cur.execute(
            f"SELECT {COL_SLOT_MEAL_OVERRIDES_SLOT_ID} AS slot_id, {COL_SLOT_MEAL_OVERRIDES_MEAL_TEMPLATE_ID} AS meal_template_id, {COL_SLOT_MEAL_OVERRIDES_INGREDIENTS_JSON} AS ingredients_json FROM {TABLE_SLOT_MEAL_OVERRIDES} WHERE {COL_SLOT_MEAL_OVERRIDES_SLOT_ID} IN ({placeholders})",
            tuple(slot_ids),
        ).fetchall()
        override_rows = [dict(r) for r in override_rows]
        overrides_by_slot = {row["slot_id"]: row for row in override_rows}

    meal_template_ids = {row["meal_template_id"] for row in slots if row["meal_template_id"]}
    meal_template_ids.update({row.get("meal_template_id") for row in overrides_by_slot.values() if row.get("meal_template_id")})

    meal_templates = {}
    if meal_template_ids:
        placeholders = ",".join("?" for _ in meal_template_ids)
        rows = cur.execute(
            f"SELECT {COL_MEAL_TEMPLATES_ID} AS id, {COL_MEAL_TEMPLATES_TITLE} AS title FROM {TABLE_MEAL_TEMPLATES} WHERE {COL_MEAL_TEMPLATES_ID} IN ({placeholders})",
            tuple(meal_template_ids),
        ).fetchall()
        rows = [dict(r) for r in rows]
        meal_templates = {row["id"]: row for row in rows}

    ingredients_by_meal = {}
    if meal_template_ids:
        placeholders = ",".join("?" for _ in meal_template_ids)
        rows = cur.execute(
            f"""
            SELECT
                mi.{COL_MEAL_INGREDIENTS_MEAL_ID} AS meal_template_id,
                mi.{COL_MEAL_INGREDIENTS_FOOD_ID} AS food_id,
                mi.{COL_MEAL_INGREDIENTS_AMOUNT} AS amount,
                mi.{COL_MEAL_INGREDIENTS_UNIT} AS unit,
                mi.{COL_MEAL_INGREDIENTS_SORT} AS sort_index,
                f.{COL_FOODS_NAME} AS food_name
            FROM {TABLE_MEAL_INGREDIENTS} mi
            JOIN {TABLE_FOODS} f ON f.{COL_FOODS_ID} = mi.{COL_MEAL_INGREDIENTS_FOOD_ID}
            WHERE mi.{COL_MEAL_INGREDIENTS_MEAL_ID} IN ({placeholders})
            ORDER BY mi.{COL_MEAL_INGREDIENTS_MEAL_ID} ASC, mi.{COL_MEAL_INGREDIENTS_SORT} ASC, mi.rowid ASC
            """,
            tuple(meal_template_ids),
        ).fetchall()
        rows = [dict(r) for r in rows]
        for row in rows:
            ingredients_by_meal.setdefault(row["meal_template_id"], []).append(row)

    slot_meal_ids = [row["id"] for row in slot_meals]
    slot_meal_items_by_meal = {}
    if slot_meal_ids:
        placeholders = ",".join("?" for _ in slot_meal_ids)
        rows = cur.execute(
            f"""
            SELECT
                smi.{COL_SLOT_MEAL_ITEMS_SLOT_MEAL_ID} AS slot_meal_id,
                smi.{COL_SLOT_MEAL_ITEMS_AMOUNT} AS amount,
                smi.{COL_SLOT_MEAL_ITEMS_UNIT} AS unit,
                smi.{COL_SLOT_MEAL_ITEMS_NAME_RAW} AS name_raw,
                smi.{COL_SLOT_MEAL_ITEMS_FOOD_ID} AS food_id,
                smi.{COL_SLOT_MEAL_ITEMS_TYPE} AS item_type,
                smi.{COL_SLOT_MEAL_ITEMS_CALORIES} AS calories,
                f.{COL_FOODS_NAME} AS food_name
            FROM {TABLE_SLOT_MEAL_ITEMS} smi
            LEFT JOIN {TABLE_FOODS} f ON f.{COL_FOODS_ID} = smi.{COL_SLOT_MEAL_ITEMS_FOOD_ID}
            WHERE smi.{COL_SLOT_MEAL_ITEMS_SLOT_MEAL_ID} IN ({placeholders})
            ORDER BY smi.{COL_SLOT_MEAL_ITEMS_SLOT_MEAL_ID} ASC, smi.{COL_SLOT_MEAL_ITEMS_ID} ASC
            """,
            tuple(slot_meal_ids),
        ).fetchall()
        rows = [dict(r) for r in rows]
        for row in rows:
            slot_meal_items_by_meal.setdefault(row["slot_meal_id"], []).append(row)

    conn.close()

    meals_out = []
    for slot in slots:
        slot_template = slot_templates.get(slot["slot_template_id"], {})
        override = overrides_by_slot.get(slot["id"])
        slot_meal = slot_meal_by_key.get((slot["day_id"], slot["slot_index"]))

        time_text = normalize_time_str(slot.get("time_text")) or normalize_time_str(slot_template.get("default_time"))

        # Title resolution
        title = (slot.get("custom_title") or "").strip()
        if not title:
            meal_template_id = slot.get("meal_template_id") or (override.get("meal_template_id") if override else None)
            if meal_template_id:
                title = (meal_templates.get(meal_template_id, {}).get("title") or "").strip()
        if not title and slot_meal:
            title = (slot_meal.get("meal_title") or "").strip()
        if not title:
            title = (slot_template.get("title") or "Meal").strip()

        # Items resolution
        items = []
        if override:
            try:
                override_items = json.loads(override.get("ingredients_json") or "[]")
            except Exception:
                override_items = []
            for item in override_items:
                unit = (item.get("unit") or "").strip().lower()
                if item.get("kcal_value") is not None or unit == "kcal":
                    continue
                name = (item.get("food_name") or item.get("raw") or item.get("name_raw") or "").strip()
                amount = item.get("amount")
                unit = item.get("unit")
                formatted = _format_item(name, amount, unit)
                if formatted:
                    items.append(formatted)
        elif slot_meal and slot_meal_items_by_meal.get(slot_meal.get("id")):
            for item in slot_meal_items_by_meal.get(slot_meal.get("id"), []):
                if (item.get("item_type") or "").lower() == "kcal":
                    continue
                unit = (item.get("unit") or "").strip().lower()
                if unit == "kcal" or item.get("calories") is not None:
                    continue
                name = (item.get("food_name") or item.get("name_raw") or "").strip()
                amount = item.get("amount")
                unit = item.get("unit")
                formatted = _format_item(name, amount, unit)
                if formatted:
                    items.append(formatted)
        else:
            meal_template_id = slot.get("meal_template_id") or (override.get("meal_template_id") if override else None)
            for item in ingredients_by_meal.get(meal_template_id, []):
                name = (item.get("food_name") or "").strip()
                amount = item.get("amount")
                unit = item.get("unit")
                formatted = _format_item(name, amount, unit)
                if formatted:
                    items.append(formatted)

        meals_out.append({
            "time": time_text,
            "title": title,
            "items": items,
            "slot_index": slot.get("slot_index"),
        })

    return meals_out


def main():
    # Legacy sender guard:
    # The live reminder flow is handled in telegram_hub.process_nutrition_inbox().
    # This script is kept only as fallback/manual tool and is disabled by default
    # to avoid duplicate old-format messages.
    if str(os.environ.get(LEGACY_ENABLE_ENV, "0")).strip().lower() not in {"1", "true", "yes", "on"}:
        print(f"Legacy meal reminder disabled ({LEGACY_ENABLE_ENV}!=1).")
        return

    # Read CLI args
    db_path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DB_PATH
    state_path = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_STATE_PATH

    # Read environment variables
    config = resolve_domain_config("nutrition")
    if not config.get("token") or not config.get("chat_id"):
        print("Missing nutrition Telegram bot config.", file=sys.stderr)
        sys.exit(1)

    # Timezone
    try:
        tz = ZoneInfo(DEFAULT_TIMEZONE)
    except Exception as e:
        print(f"Invalid timezone '{DEFAULT_TIMEZONE}': {e}", file=sys.stderr)
        sys.exit(1)

    # Current time in specified timezone
    now = datetime.now(tz)
    now_ts = int(now.timestamp())
    target = now + timedelta(minutes=15)
    target_time_str = target.strftime("%H:%M")
    today_str = now.date().isoformat()

    if telegram_pushes_muted(DEFAULT_TRAINING_DB_PATH, now_ts):
        print("Telegram pushes are muted. Skipping send.")
        return

    # Load state to ensure only one reminder per meal per day
    state = load_state(state_path)
    sent_today = state.get(today_str, {})

    # Cleanup old state entries
    cutoff = now.date() - timedelta(days=KEEP_DAYS)
    for key in list(state.keys()):
        try:
            key_date = date.fromisoformat(key)
        except ValueError:
            continue
        if key_date < cutoff:
            del state[key]

    # Load meals from SQLite
    global DB_PATH
    DB_PATH = db_path
    try:
        meals = load_meals_from_sqlite(now, tz)
    except Exception as e:
        print(f"Failed to load meals from SQLite: {e}", file=sys.stderr)
        sys.exit(1)

    for meal in meals:
        meal_time = meal.get("time")
        if not meal_time:
            continue
        if meal_time == target_time_str:
            meal_number = resolve_meal_number(meal)
            if not telegram_push_for_meal_enabled(DEFAULT_TRAINING_DB_PATH, meal_number):
                continue
            key = f"{meal_time}|{meal.get('title', '')}"
            if sent_today.get(key):
                continue
            message = build_message(meal)
            try:
                result = send_domain_message("nutrition", message)
                if not result.get("ok"):
                    raise RuntimeError(result.get("error") or "send_failed")
            except Exception as e:
                print(f"Failed to send Telegram message: {e}", file=sys.stderr)
                continue
            sent_today[key] = True
            state[today_str] = sent_today
            save_state(state_path, state)
            return

    state[today_str] = sent_today
    save_state(state_path, state)


if __name__ == "__main__":
    main()
