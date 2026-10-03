from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
from datetime import date, datetime, time as dt_time, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from app_support.weight_trend import build_weight_trend
from database.connections import get_core_db, get_hrv_db, get_nutrition_db, get_plans_db, get_polar_db, get_runs_db, get_training_db
from core.core_daily_decision import morning_readiness
from core.core_memory_layers import summarize_memory_layers
from integrations.endurance_approval import build_endurance_core_context


DAY_LABELS = ("Mo", "Di", "Mi", "Do", "Fr", "Sa", "So")
JSON_DEFAULTS = {
    "machine_briefing_json": {},
    "visible_reasons_json": [],
    "training_card_json": {},
    "data_status_json": {},
    "legacy_core_json": {},
}

SESSION_TEXT_TOKENS = {
    "push": ("push",),
    "pull": ("pull",),
    "legs": ("legs", "leg", "beine"),
    "lower": ("lower",),
    "upper": ("upper",),
    "chest": ("chest", "brust"),
    "back": ("back", "rücken", "ruecken"),
    "run": ("run", "lauf", "laufen", "jog"),
    "z2": ("z2",),
    "ergo": ("ergo",),
    "bike": ("bike", "rad", "fahrrad"),
    "mobility": ("mobility",),
    "rest": ("rest",),
    "recovery": ("recovery",),
}

GYM_SPLIT_TOKENS = {"push", "pull", "legs", "lower", "upper", "chest", "back"}
GENERIC_BOARD_TOKENS = {"mobility"}


def _utc_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _today_iso() -> str:
    return date.today().isoformat()


def _parse_day(raw: Any | None) -> str:
    if raw in (None, ""):
        return _today_iso()
    text = str(raw).strip()
    date.fromisoformat(text)
    return text


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1", (table,)).fetchone()
    return bool(row)


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    if not _table_exists(conn, table):
        return set()
    return {str(row["name"] if isinstance(row, sqlite3.Row) else row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _json_loads(raw: Any, default: Any) -> Any:
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(str(raw or ""))
    except Exception:
        return default


def _json_dumps(value: Any, default: Any) -> str:
    if value is None:
        value = default
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _text(value: Any, limit: int = 1200) -> str | None:
    text = re.sub(r"\s+", " ", str(value or "").strip())
    if not text:
        return None
    return text[:limit]


def _list_text(value: Any, limit: int = 5) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in value:
        text = _text(item, 180)
        if not text:
            continue
        key = re.sub(r"[^a-z0-9äöüß]+", "", text.lower())
        if key in seen:
            continue
        seen.add(key)
        out.append(text)
        if len(out) >= limit:
            break
    return out


def _float_or_none(value: Any) -> float | None:
    try:
        if value in (None, ""):
            return None
        return float(value)
    except Exception:
        return None


def _confidence(value: Any, fallback: float = 0.56) -> float:
    parsed = _float_or_none(value)
    if parsed is None:
        parsed = fallback
    if parsed > 1:
        parsed = parsed / 100.0
    return round(max(0.0, min(1.0, parsed)), 2)


def _status_label(status: Any) -> str:
    raw = str(status or "").strip().lower()
    mapping = {
        "ok": "frisch",
        "fresh": "frisch",
        "partial": "teilweise",
        "stale": "älter",
        "missing": "offen",
    }
    return mapping.get(raw, raw or "offen")








def _status_meter(status: Any) -> int:
    raw = str(status or "").strip().lower()
    mapping = {
        "ok": 84,
        "fresh": 84,
        "partial": 58,
        "stale": 44,
        "missing": 24,
    }
    return mapping.get(raw, 36)


def _parse_dt(raw: Any) -> datetime | None:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def _day_age_label(day_iso: str | None, *, checked: bool = True, default_missing: str = "fehlt") -> str:
    if not day_iso:
        return default_missing
    try:
        age = (date.today() - date.fromisoformat(str(day_iso)[:10])).days
    except Exception:
        return "fehler"
    if age <= 0:
        return "frisch"
    if age == 1:
        return "gestern"
    return "gesichtet" if checked and age <= 1 else "veraltet"




def _source_freshness_for_day(observed_day: str | None, board_day: str) -> str:
    if not observed_day:
        return "fehlt"
    if str(observed_day)[:10] == board_day:
        return "frisch"
    try:
        age = (date.fromisoformat(board_day) - date.fromisoformat(str(observed_day)[:10])).days
    except Exception:
        return "veraltet"
    if age == 1:
        return "gestern"
    if age > 1:
        return "veraltet"
    return "fehler"


def _subjective_signal_snapshot(readiness: dict[str, Any], day_iso: str) -> dict[str, Any]:
    subjective = ((readiness.get("signals") or {}).get("subjective") or {}) if isinstance(readiness, dict) else {}
    observed_day = str(subjective.get("observed_at") or "")[:10] or None
    return {
        "present_today": bool(subjective.get("present")),
        "observed_day": observed_day,
        "freshness": "frisch" if bool(subjective.get("present")) else _source_freshness_for_day(observed_day, day_iso),
        "sleep_quality": _float_or_none(subjective.get("sleep_quality")),
        "fatigue": _float_or_none(subjective.get("fatigue")),
        "motivation": _float_or_none(subjective.get("motivation")),
        "pain": _float_or_none(subjective.get("pain")),
        "sick": bool(subjective.get("sick")),
    }


def _latest_polar_sleep_signal(day_iso: str) -> dict[str, Any]:
    try:
        conn = get_polar_db()
    except Exception:
        return {"present_today": False}
    try:
        if not _table_exists(conn, "polar_sleep"):
            return {"present_today": False}
        row = conn.execute(
            """
            SELECT date, sleep_score, sleep_minutes, actual_sleep_minutes, deep_sleep_minutes, rem_sleep_minutes
            FROM polar_sleep
            WHERE date <= ?
            ORDER BY date DESC
            LIMIT 1
            """,
            (day_iso,),
        ).fetchone()
        if not row:
            return {"present_today": False}
        observed_day = _text(row["date"], 20)
        return {
            "present_today": bool(observed_day == day_iso),
            "observed_day": observed_day,
            "freshness": _source_freshness_for_day(observed_day, day_iso),
            "sleep_score": _float_or_none(row["sleep_score"]),
            "sleep_minutes": _float_or_none(row["sleep_minutes"]),
            "actual_sleep_minutes": _float_or_none(row["actual_sleep_minutes"]),
            "deep_sleep_minutes": _float_or_none(row["deep_sleep_minutes"]),
            "rem_sleep_minutes": _float_or_none(row["rem_sleep_minutes"]),
        }
    finally:
        conn.close()




def _confidence_label(score: float | None) -> str:
    value = float(score or 0.0)
    if value >= 0.75:
        return "high"
    if value >= 0.45:
        return "medium"
    return "low"


def _fmt_num(value: float | int | None, digits: int = 1) -> str:
    if value is None:
        return "—"
    if digits <= 0:
        return f"{int(round(float(value)))}"
    return f"{float(value):.{digits}f}".replace(".", ",")


def _fmt_signed(value: float | None, unit: str = "", digits: int = 1) -> str:
    if value is None:
        return "—"
    out = f"{value:+.{digits}f}".replace(".", ",")
    return f"{out} {unit}".strip()


def _fmt_minutes(total_minutes: float | int | None) -> str:
    if total_minutes is None:
        return "—"
    total = int(round(float(total_minutes)))
    if total < 0:
        total = 0
    if total < 120:
        return f"{total} min"
    hours = total // 60
    minutes = total % 60
    if minutes == 0:
        return f"{hours} h"
    return f"{hours} h {minutes} min"


def _parse_hhmm(text: Any) -> int | None:
    raw = str(text or "").strip()
    if ":" not in raw:
        return None
    try:
        hh_s, mm_s = raw.split(":", 1)
        hh = int(hh_s)
        mm = int(mm_s)
    except Exception:
        return None
    if hh < 0 or hh > 23 or mm < 0 or mm > 59:
        return None
    return hh * 60 + mm


def _to_hhmm(minutes: int | None) -> str | None:
    if minutes is None:
        return None
    bounded = max(0, min(23 * 60 + 59, int(minutes)))
    return f"{bounded // 60:02d}:{bounded % 60:02d}"


def _format_time_window(start: int | None, end: int | None) -> str | None:
    if start is None and end is None:
        return None
    if start is not None and end is not None and start != end:
        return f"{_to_hhmm(start)}–{_to_hhmm(end)}"
    if start is not None:
        return f"ca. {_to_hhmm(start)}"
    return f"bis {_to_hhmm(end)}"


def _status_context_tone(status: str, *, domain: str = "", optional: bool = False) -> str:
    value = str(status or "").strip().upper()
    positive = {"POSITIV", "STABIL", "FRISCH", "RUHIG", "FREI", "KEINER", "GUT", "NIEDRIG"}
    warning = {"BEOBACHTEN", "FALLEND", "LEICHT ERHÖHT", "LÜCKENHAFT", "BELEGT", "KNAPP", "NORMAL", "GEMISCHT", "MODERAT", "OKAY"}
    danger = {"KRITISCH", "ERHÖHT", "HOCH", "FEHLT", "VERALTET", "KLARER KONFLIKT", "AGGRESSIVES DEFIZIT", "UNPLAUSIBEL"}
    if domain in {"datenqualität", "sicherheit"} and value == "HOCH":
        return "green"
    if value == "UNBEKANNT":
        return "gray" if optional else "yellow"
    if value in positive:
        return "green"
    if value in warning:
        return "yellow"
    if value in danger:
        return "red"
    if value == "NEUTRAL":
        return "gray"
    return "gray"


def ensure_core_daily_boards_schema(conn: sqlite3.Connection | None = None) -> None:
    own_conn = conn is None
    db = conn or get_core_db()
    try:
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS core_daily_boards (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                day_iso TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT 'gpt_checkin',
                trigger TEXT,
                source_message TEXT,
                human_headline TEXT,
                human_summary TEXT,
                decision_intent TEXT,
                confidence REAL,
                machine_briefing_json TEXT NOT NULL DEFAULT '{}',
                visible_reasons_json TEXT NOT NULL DEFAULT '[]',
                training_card_json TEXT NOT NULL DEFAULT '{}',
                data_status_json TEXT NOT NULL DEFAULT '{}',
                legacy_core_json TEXT NOT NULL DEFAULT '{}',
                status TEXT NOT NULL DEFAULT 'active',
                user_feedback TEXT,
                user_feedback_at TEXT,
                UNIQUE(day_iso)
            )
            """
        )
        db.execute("CREATE INDEX IF NOT EXISTS idx_core_daily_boards_day_status ON core_daily_boards(day_iso, status)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_core_daily_boards_updated ON core_daily_boards(updated_at DESC)")
        if own_conn:
            db.commit()
    finally:
        if own_conn:
            db.close()


def _legacy_core_payload(day_iso: str) -> dict[str, Any]:
    app_mod = sys.modules.get("app")
    func = getattr(app_mod, "_core_decision_payload", None) if app_mod is not None else None
    if callable(func):
        try:
            payload = func(day_iso)
            if isinstance(payload, dict):
                return payload
        except Exception:
            pass
    conn = get_core_db()
    try:
        if _table_exists(conn, "core_daily_decisions"):
            row = conn.execute(
                "SELECT decision_json, readiness_json, updated_at FROM core_daily_decisions WHERE day_iso=? LIMIT 1",
                (day_iso,),
            ).fetchone()
            if row:
                return {
                    "source": "core_daily_decisions",
                    "decision": _json_loads(row["decision_json"], {}),
                    "readiness": _json_loads(row["readiness_json"], {}),
                    "updated_at": row["updated_at"],
                }
    finally:
        conn.close()
    return {"source": "fallback", "available": False}


def _legacy_decision_day_iso(legacy_core: dict[str, Any]) -> str | None:
    if not isinstance(legacy_core, dict):
        return None
    candidates = [
        legacy_core.get("day_iso"),
        ((legacy_core.get("daily_decision") or {}).get("day_iso")) if isinstance(legacy_core.get("daily_decision"), dict) else None,
        ((legacy_core.get("daily_ui") or {}).get("day_iso")) if isinstance(legacy_core.get("daily_ui"), dict) else None,
        (
            ((legacy_core.get("daily_ui") or {}).get("daily_decision") or {}).get("day_iso")
            if isinstance((legacy_core.get("daily_ui") or {}).get("daily_decision"), dict)
            else None
        ),
        ((legacy_core.get("decision") or {}).get("day_iso")) if isinstance(legacy_core.get("decision"), dict) else None,
    ]
    for value in candidates:
        text = _text(value, 40)
        if text:
            return text
    return None


def _legacy_core_status(day_iso: str, legacy_core: dict[str, Any]) -> dict[str, Any]:
    available = isinstance(legacy_core, dict) and bool(legacy_core)
    legacy_day_iso = _legacy_decision_day_iso(legacy_core)
    stale = bool(available and legacy_day_iso and legacy_day_iso != day_iso)
    return {
        "available": bool(available),
        "stale": stale,
        "legacy_day_iso": legacy_day_iso,
        "board_day_iso": day_iso,
        "message": "Legacy CORE stammt nicht vom Board-Tag." if stale else None,
    }


def _effective_legacy_core(day_iso: str, legacy_core: dict[str, Any]) -> dict[str, Any]:
    return {} if _legacy_core_status(day_iso, legacy_core).get("stale") else (legacy_core if isinstance(legacy_core, dict) else {})


def _latest_training_session() -> dict[str, Any] | None:
    conn = get_training_db()
    try:
        if not _table_exists(conn, "workouts"):
            return None
        row = conn.execute("SELECT date_iso, name FROM workouts ORDER BY date(date_iso) DESC, id DESC LIMIT 1").fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _active_plan() -> dict[str, Any] | None:
    conn = get_plans_db()
    try:
        if _table_exists(conn, "gym_plans"):
            row = conn.execute(
                """
                SELECT id, title AS name, plan_json, updated_at, 'gym_plans' AS source
                FROM gym_plans
                WHERE is_active=1 AND COALESCE(is_archived, 0)=0
                ORDER BY updated_at DESC, id DESC
                LIMIT 1
                """
            ).fetchone()
            if row:
                data = dict(row)
                data["plan"] = _json_loads(data.pop("plan_json", None), {})
                return data
        if _table_exists(conn, "plans"):
            row = conn.execute(
                "SELECT id, name, data, updated_at, 'plans' AS source FROM plans WHERE is_active=1 ORDER BY updated_at DESC, id DESC LIMIT 1"
            ).fetchone()
            if row:
                data = dict(row)
                data["plan"] = _json_loads(data.pop("data", None), {})
                return data
    finally:
        conn.close()
    return None


def _plan_value_for_day(plan: dict[str, Any] | None, day_key: str) -> Any:
    raw = plan.get("plan") if isinstance((plan or {}).get("plan"), dict) else {}
    value: Any = None
    if isinstance(raw.get("base_week"), dict):
        value = raw["base_week"].get(day_key)
    elif isinstance(raw.get("days"), dict):
        value = raw["days"].get(day_key)
    return value


def _event_exercises(event: dict[str, Any]) -> list[dict[str, Any]]:
    items = event.get("items") if isinstance(event.get("items"), list) else event.get("strength_exercises")
    if not isinstance(items, list):
        return []
    exercises: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        name = _text(item.get("name") or item.get("exercise_name"), 90)
        if not name:
            continue
        variation = _text(item.get("variation") or item.get("device"), 60)
        reps = item.get("reps") if isinstance(item.get("reps"), dict) else {"min": item.get("reps_min"), "max": item.get("reps_max")}
        exercise = {
            "name": name,
            "display_name": f"{name} ({variation})" if variation else name,
            "sets": item.get("sets"),
            "reps": {k: v for k, v in (reps or {}).items() if v not in (None, "")} if isinstance(reps, dict) else {},
            "rpe": item.get("rpe") or item.get("rpe_target") or item.get("rpe_list"),
        }
        exercises.append({k: v for k, v in exercise.items() if v not in (None, "", {}, [])})
    return exercises[:8]


def _plan_day(day_iso: str) -> dict[str, Any]:
    parsed = date.fromisoformat(day_iso)
    day_key = DAY_LABELS[parsed.weekday()]
    plan = _active_plan()
    if not plan:
        return {"available": False, "day_key": day_key, "source": "no_active_plan"}
    plan_json = plan.get("plan") if isinstance(plan.get("plan"), dict) else {}
    plan_meta = plan_json.get("meta") if isinstance(plan_json.get("meta"), dict) else {}
    if str(plan_meta.get("mode") or "").strip().lower() == "rolling_sequence":
        # Actions V2 owns rolling-session resolution.  CORE must consume that
        # exact result instead of interpreting a rolling plan as an empty
        # weekday plan and manufacturing a recovery card.
        try:
            from ai.actions_v2 import _plan_session_for_date

            resolved = _plan_session_for_date(day_iso)
        except Exception:
            resolved = {}
        if isinstance(resolved, dict) and resolved.get("available"):
            title = _text(resolved.get("name") or resolved.get("label"), 120) or "Training"
            session_type = _text(resolved.get("session_type") or resolved.get("type"), 30) or "gym"
            raw_exercises = resolved.get("exercises") if isinstance(resolved.get("exercises"), list) else []
            event_items = []
            for exercise in raw_exercises:
                if not isinstance(exercise, dict):
                    continue
                event_items.append(
                    {
                        "name": exercise.get("name") or exercise.get("exercise"),
                        "variation": exercise.get("variation") or exercise.get("device"),
                        "sets": exercise.get("sets") or exercise.get("planned_sets"),
                        "reps": exercise.get("reps") or exercise.get("target_rep_range"),
                        "rpe": exercise.get("rpe") or exercise.get("rpe_list"),
                    }
                )
            event = {"title": title, "kind": session_type, "items": event_items}
            return {
                "available": True,
                "day_key": day_key,
                "plan_name": plan.get("name"),
                "source": "actions_v2.canonical_rolling_resolver",
                "updated_at": plan.get("updated_at"),
                "events": [{"title": title, "kind": session_type, "time": resolved.get("time"), "exercises": _event_exercises(event)}],
                "exercises": _event_exercises(event),
                "session_key": resolved.get("session_key"),
                "resolver": resolved.get("resolver"),
            }
    value: Any = _plan_value_for_day(plan, day_key)
    if value in (None, [], {}) and str(plan.get("source") or "") == "gym_plans":
        conn = get_plans_db()
        try:
            if _table_exists(conn, "plans"):
                row = conn.execute(
                    "SELECT id, name, data, updated_at, 'plans' AS source FROM plans WHERE is_active=1 ORDER BY updated_at DESC, id DESC LIMIT 1"
                ).fetchone()
                if row:
                    fallback_plan = dict(row)
                    fallback_plan["plan"] = _json_loads(fallback_plan.pop("data", None), {})
                    fallback_value = _plan_value_for_day(fallback_plan, day_key)
                    if fallback_value not in (None, [], {}):
                        plan = fallback_plan
                        value = fallback_value
        finally:
            conn.close()
    if isinstance(value, str):
        value = [{"kind": "gym", "title": value}]
    if isinstance(value, dict):
        value = value.get("events") or value.get("items") or []
    if not isinstance(value, list):
        value = []
    events: list[dict[str, Any]] = []
    exercises: list[dict[str, Any]] = []
    for event in value:
        if not isinstance(event, dict):
            continue
        title = _text(event.get("title") or event.get("session_name"), 120)
        kind = _text(event.get("kind") or "gym", 30) or "gym"
        event_exercises = _event_exercises(event)
        exercises.extend(event_exercises)
        events.append({"title": title or kind.title(), "kind": kind, "time": event.get("time"), "exercises": event_exercises})
    return {
        "available": bool(events),
        "day_key": day_key,
        "plan_name": plan.get("name"),
        "source": plan.get("source"),
        "updated_at": plan.get("updated_at"),
        "events": events,
        "exercises": exercises[:10],
    }


def _recovery_status() -> dict[str, Any]:
    try:
        from ai.actions_v2 import _compact_recovery_summary_payload, _recovery_summary

        summary = _recovery_summary()
        compact = _compact_recovery_summary_payload(summary)
        latest_date = _text(compact.get("date"), 20)
        if latest_date:
            age = (date.today() - date.fromisoformat(latest_date[:10])).days
            return {
                "status": "fresh" if age <= 2 else "stale",
                "latest_date": latest_date,
                "latest_rmssd": _float_or_none(compact.get("rmssd")),
                "latest_hr": _float_or_none(compact.get("hr") or compact.get("night_pulse")),
                "summary_status": compact.get("status"),
                "flags": compact.get("flags") if isinstance(compact.get("flags"), dict) else {},
                "source": compact.get("source"),
            }
    except Exception:
        pass
    try:
        conn = get_hrv_db()
    except Exception:
        return {"status": "missing", "latest_rmssd": None, "latest_hr": None}
    try:
        if not _table_exists(conn, "hrv_measurements"):
            return {"status": "missing", "latest_rmssd": None, "latest_hr": None}
        cols = _columns(conn, "hrv_measurements")
        date_expr = "COALESCE(substr(date_utc,1,10), substr(ts_measurement,1,10))"
        rmssd = "rmssd" if "rmssd" in cols else "NULL"
        hr = "hr" if "hr" in cols else "NULL"
        row = conn.execute(
            f"SELECT {date_expr} AS date, {rmssd} AS rmssd, {hr} AS hr FROM hrv_measurements ORDER BY date DESC LIMIT 1"
        ).fetchone()
        if not row:
            return {"status": "missing", "latest_rmssd": None, "latest_hr": None}
        age = (date.today() - date.fromisoformat(str(row["date"])[:10])).days if row["date"] else None
        return {
            "status": "fresh" if age is not None and age <= 2 else "stale",
            "latest_date": row["date"],
            "latest_rmssd": _float_or_none(row["rmssd"]),
            "latest_hr": _float_or_none(row["hr"]),
            "age_days": age,
        }
    except Exception:
        return {"status": "partial", "latest_rmssd": None, "latest_hr": None}
    finally:
        conn.close()


def _shared_recovery_summary_payload() -> dict[str, Any]:
    try:
        from ai.actions_v2 import _compact_recovery_summary_payload, _recovery_summary

        return _compact_recovery_summary_payload(_recovery_summary())
    except Exception:
        return {}


def _weight_status() -> dict[str, Any]:
    conn = get_nutrition_db()
    try:
        if not _table_exists(conn, "weight_logs"):
            return {"status": "missing", "latest_weight": None}
        cols = _columns(conn, "weight_logs")
        weight_col = "weight_kg" if "weight_kg" in cols else "weight" if "weight" in cols else None
        date_col = "date_iso" if "date_iso" in cols else "date" if "date" in cols else None
        if not weight_col or not date_col:
            return {"status": "partial", "latest_weight": None}
        row = conn.execute(f"SELECT {date_col} AS date, {weight_col} AS weight FROM weight_logs ORDER BY date({date_col}) DESC LIMIT 1").fetchone()
        if not row:
            return {"status": "missing", "latest_weight": None}
        age = (date.today() - date.fromisoformat(str(row["date"])[:10])).days if row["date"] else None
        return {
            "status": "fresh" if age is not None and age <= 3 else "stale",
            "latest_date": row["date"],
            "latest_weight": _float_or_none(row["weight"]),
            "age_days": age,
        }
    finally:
        conn.close()


def _nutrition_status() -> dict[str, Any]:
    conn = get_nutrition_db()
    try:
        has_targets = False
        if _table_exists(conn, "nutrition_settings"):
            has_targets = bool(conn.execute("SELECT 1 FROM nutrition_settings WHERE key LIKE '%target%' LIMIT 1").fetchone())
        logs = 0
        if _table_exists(conn, "nutrition_day_actuals"):
            logs += int((conn.execute("SELECT COUNT(*) AS c FROM nutrition_day_actuals").fetchone() or {"c": 0})["c"] or 0)
        if _table_exists(conn, "nutrition_logged_meals"):
            logs += int((conn.execute("SELECT COUNT(*) AS c FROM nutrition_logged_meals").fetchone() or {"c": 0})["c"] or 0)
        if logs:
            status = "fresh"
        elif has_targets:
            status = "partial"
        else:
            status = "missing"
        return {"status": status, "targets_present": has_targets, "log_count": logs}
    finally:
        conn.close()


def _latest_core_training_card_row(day_iso: str) -> dict[str, Any] | None:
    conn = get_core_db()
    try:
        if not _table_exists(conn, "core_training_cards"):
            return None
        row = conn.execute(
            """
            SELECT day_iso, created_at, updated_at, title, session_type, status
            FROM core_training_cards
            WHERE day_iso=? AND COALESCE(status, 'active')='active'
            ORDER BY COALESCE(updated_at, created_at) DESC, id DESC
            LIMIT 1
            """,
            (day_iso,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _read_weight_history(day_iso: str, limit: int = 35) -> list[dict[str, Any]]:
    conn = get_nutrition_db()
    try:
        if not _table_exists(conn, "weight_logs"):
            return []
        cols = _columns(conn, "weight_logs")
        weight_col = "weight_kg" if "weight_kg" in cols else "weight" if "weight" in cols else None
        date_col = "date_iso" if "date_iso" in cols else "date" if "date" in cols else None
        if not weight_col or not date_col:
            return []
        rows = conn.execute(
            f"""
            SELECT {date_col} AS day_iso, {weight_col} AS weight
            FROM weight_logs
            WHERE {date_col} <= ? AND {weight_col} IS NOT NULL
            ORDER BY date({date_col}) DESC, id DESC
            LIMIT ?
            """,
            (day_iso, limit),
        ).fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            weight = _float_or_none(row["weight"])
            day = _text(row["day_iso"], 20)
            if day and weight is not None:
                out.append({"day_iso": day[:10], "weight": weight})
        return out
    finally:
        conn.close()


def _read_recovery_history(day_iso: str, limit: int = 35) -> list[dict[str, Any]]:
    conn = get_hrv_db()
    try:
        if not _table_exists(conn, "hrv_measurements"):
            return []
        cols = _columns(conn, "hrv_measurements")
        if "rmssd" not in cols and "hr" not in cols:
            return []
        rows = conn.execute(
            """
            SELECT substr(COALESCE(date_utc, ts_measurement), 1, 10) AS day_iso, ts_measurement, rmssd, hr, sleep_quality, fatigue, training_motivation
            FROM hrv_measurements
            WHERE substr(COALESCE(date_utc, ts_measurement), 1, 10) <= ?
            ORDER BY COALESCE(ts_measurement, date_utc) DESC, id DESC
            LIMIT ?
            """,
            (day_iso, limit),
        ).fetchall()
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        for row in rows:
            day = _text(row["day_iso"], 20)
            if not day or day in seen:
                continue
            seen.add(day)
            out.append(
                {
                    "day_iso": day[:10],
                    "rmssd": _float_or_none(row["rmssd"]),
                    "hr": _float_or_none(row["hr"]),
                    "sleep_quality": _float_or_none(row["sleep_quality"]),
                    "fatigue": _float_or_none(row["fatigue"]),
                    "motivation": _float_or_none(row["training_motivation"]),
                    "ts_measurement": _text(row["ts_measurement"], 40),
                }
            )
        return out
    finally:
        conn.close()


def _read_training_sessions(day_iso: str, limit: int = 40) -> list[dict[str, Any]]:
    conn = get_training_db()
    try:
        if not _table_exists(conn, "workouts"):
            return []
        rows = conn.execute(
            """
            SELECT date_iso, name, notes
            FROM workouts
            WHERE date_iso <= ?
            ORDER BY date(date_iso) DESC, id DESC
            LIMIT ?
            """,
            (day_iso, limit),
        ).fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            day = _text(row["date_iso"], 20)
            if not day:
                continue
            name = _text(row["name"], 140) or "Training"
            note = _text(row["notes"], 280) or ""
            text = f"{name} {note}".lower()
            out.append(
                {
                    "day_iso": day[:10],
                    "name": name,
                    "notes": note,
                    "is_heavy": bool(re.search(r"\b(rpe\s*(8|9|10)|heavy|topset|hard)\b", text)),
                    "is_skipped": "skip" in text or "abgebrochen" in text or "gekürzt" in text,
                    "split": _normalize_gym_split_label(text),
                }
            )
        return out
    finally:
        conn.close()


def _read_runs(day_iso: str, limit: int = 30) -> list[dict[str, Any]]:
    conn = get_runs_db()
    try:
        if not _table_exists(conn, "runs"):
            return []
        cols = _columns(conn, "runs")
        distance_col = "distance" if "distance" in cols else None
        date_col = "date" if "date" in cols else "date_iso" if "date_iso" in cols else None
        duration_col = "moving_time" if "moving_time" in cols else "duration_min" if "duration_min" in cols else None
        title_col = "name" if "name" in cols else None
        sport_type_col = "sport_type" if "sport_type" in cols else None
        run_type_col = "run_type" if "run_type" in cols else None
        avg_hr_col = "avg_hr" if "avg_hr" in cols else None
        avg_power_col = "avg_power" if "avg_power" in cols else None
        if not date_col:
            return []
        select_cols = [f"{date_col} AS day_iso"]
        if distance_col:
            select_cols.append(f"{distance_col} AS distance")
        if duration_col:
            select_cols.append(f"{duration_col} AS duration")
        if title_col:
            select_cols.append(f"{title_col} AS name")
        if sport_type_col:
            select_cols.append(f"{sport_type_col} AS sport_type")
        if run_type_col:
            select_cols.append(f"{run_type_col} AS run_type")
        if avg_hr_col:
            select_cols.append(f"{avg_hr_col} AS avg_hr")
        if avg_power_col:
            select_cols.append(f"{avg_power_col} AS avg_power")
        rows = conn.execute(
            f"SELECT {', '.join(select_cols)} FROM runs WHERE {date_col} <= ? ORDER BY date({date_col}) DESC, id DESC LIMIT ?",
            (day_iso, limit),
        ).fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            day = _text(row["day_iso"], 20)
            if not day:
                continue
            distance = _float_or_none(row["distance"]) if "distance" in row.keys() else None
            duration = _float_or_none(row["duration"]) if "duration" in row.keys() else None
            if distance is not None and distance > 1000:
                distance = distance / 1000.0
            if duration is not None and duration > 300:
                duration = duration / 60.0
            out.append(
                {
                    "day_iso": day[:10],
                    "distance": distance,
                    "duration": duration,
                    "name": _text(row["name"], 120) if "name" in row.keys() else None,
                    "sport_type": _text(row["sport_type"], 40) if "sport_type" in row.keys() else None,
                    "run_type": _text(row["run_type"], 40) if "run_type" in row.keys() else None,
                    "avg_hr": _float_or_none(row["avg_hr"]) if "avg_hr" in row.keys() else None,
                    "avg_power": _float_or_none(row["avg_power"]) if "avg_power" in row.keys() else None,
                }
            )
        return out
    finally:
        conn.close()


def _classify_cardio_entry(row: dict[str, Any]) -> str | None:
    sport_type = str(row.get("sport_type") or "").strip().lower()
    run_type = str(row.get("run_type") or "").strip().lower()
    name = str(row.get("name") or "").strip().lower()
    text = " ".join(part for part in [sport_type, run_type, name] if part)
    if re.search(r"\b(ergo|bike|rad|cycle|cycling|indoor.?bike)\b", text):
        return "ergo"
    if re.search(r"\b(run|laufen|lauf|jog|jogging|treadmill)\b", text):
        return "run"

    distance = _float_or_none(row.get("distance"))
    duration = _float_or_none(row.get("duration"))
    if distance is None or duration is None or duration <= 0:
        return None

    pace_min_per_km = duration / distance if distance > 0 else None
    kmh = distance / (duration / 60.0) if duration > 0 else None

    if pace_min_per_km is not None and 3.0 <= pace_min_per_km <= 9.5 and (kmh or 0.0) <= 20.0:
        return "run"
    if kmh is not None and kmh >= 18.0:
        return "ergo"
    return None


def _avg(values: list[float]) -> float | None:
    vals = [float(v) for v in values if v is not None]
    if not vals:
        return None
    return sum(vals) / len(vals)


def _calendar_event_is_all_day(row: dict[str, Any]) -> bool:
    start = row.get("start") if isinstance(row.get("start"), dict) else {}
    end = row.get("end") if isinstance(row.get("end"), dict) else {}
    return bool(start.get("date") and not start.get("dateTime") and end.get("date") and not end.get("dateTime"))


def _calendar_google_entries(day_iso: str) -> tuple[list[dict[str, Any]], str | None]:
    try:
        from gcalsync.config import load_config
        from gcalsync.service import build_service, list_events
    except Exception:
        return [], "google_calendar_unavailable"
    try:
        day = date.fromisoformat(day_iso)
        cfg = load_config()
        tz_name = str(getattr(cfg, "timezone", "Europe/Berlin") or "Europe/Berlin")
        tz = ZoneInfo(tz_name)
        service = build_service(cfg.credentials_file, cfg.token_file)
        day_start = datetime.combine(day, dt_time(0, 0), tzinfo=tz)
        day_end = day_start + timedelta(days=1)
        calendar_ids: list[str] = []
        for attr in ("default_calendar_id", "school_calendar_id", "work_calendar_id", "training_calendar_id", "football_calendar_id"):
            cid = str(getattr(cfg, attr, "") or "").strip()
            if cid and cid not in calendar_ids:
                calendar_ids.append(cid)
        extra = str(os.getenv("CORE_CALENDAR_STRESS_IDS", "") or "").strip()
        for cid in [part.strip() for part in extra.split(",") if part.strip()]:
            if cid not in calendar_ids:
                calendar_ids.append(cid)
        if not calendar_ids:
            return [], "google_calendar_empty"
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        for calendar_id in calendar_ids:
            rows = list_events(service, calendar_id=calendar_id, time_min=day_start.isoformat(), time_max=day_end.isoformat(), max_results=250)
            for row in rows or []:
                if not isinstance(row, dict) or _calendar_event_is_all_day(row):
                    continue
                start = row.get("start") if isinstance(row.get("start"), dict) else {}
                end = row.get("end") if isinstance(row.get("end"), dict) else {}
                sig = "|".join([str(row.get("id") or ""), str(start.get("dateTime") or ""), str(end.get("dateTime") or ""), str(row.get("summary") or "")])
                if sig in seen:
                    continue
                seen.add(sig)
                out.append(
                    {
                        "title": _text(row.get("summary"), 180) or "Termin",
                        "description": _text(row.get("description"), 600) or "",
                        "start": _text(start.get("dateTime"), 40),
                        "end": _text(end.get("dateTime"), 40),
                        "source": "google_calendar",
                    }
                )
        return out, None
    except Exception:
        return [], "google_calendar_error"


def _calendar_schoolsync_entries(day_iso: str) -> tuple[list[dict[str, Any]], str | None]:
    path = Path("/opt/liva/var/schoolsync/latest_today.json")
    if not path.exists():
        return [], "schoolsync_missing"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return [], "schoolsync_error"
    entries = payload.get("entries_visible") if isinstance(payload.get("entries_visible"), list) else payload.get("entries")
    out: list[dict[str, Any]] = []
    for row in entries or []:
        if not isinstance(row, dict) or str(row.get("date") or "").strip() != day_iso:
            continue
        if str(row.get("status_hint") or "").strip().lower() in {"cancelled", "eva"}:
            continue
        title_parts = [row.get("subject"), row.get("lesson_text"), row.get("teacher"), row.get("room")]
        title = " · ".join([str(part).strip() for part in title_parts if str(part or "").strip()]) or "Schule"
        out.append(
            {
                "title": _text(title, 180) or "Schule",
                "description": _text(row.get("notes") or row.get("info"), 600) or "",
                "start": f"{day_iso}T{str(row.get('start_time') or '08:00').strip()}:00",
                "end": f"{day_iso}T{str(row.get('end_time') or '08:45').strip()}:00",
                "source": "schoolsync",
            }
        )
    return out, None


def _build_calendar_interpretation(day_iso: str) -> dict[str, Any]:
    from core.core_night_cycle import _build_calendar_stress_snapshot

    snapshot = _build_calendar_stress_snapshot(day_iso)
    google_entries, google_error = _calendar_google_entries(day_iso)
    entries = google_entries
    source = "google_calendar"
    error = google_error
    if not entries:
        school_entries, school_error = _calendar_schoolsync_entries(day_iso)
        if school_entries:
            entries = school_entries
            source = "schoolsync"
            error = None
        elif error is None:
            error = school_error
    status = "gesichtet" if snapshot.get("calendar_available") else ("offen" if error else "fehlt")
    day = date.fromisoformat(day_iso)
    categories = {"school": 0, "exam": 0, "work": 0, "coaching": 0, "private": 0, "doctor": 0, "delivery": 0}
    intervals: list[tuple[int, int]] = []
    stress_notes: list[str] = []
    for entry in entries:
        title = f"{entry.get('title') or ''} {entry.get('description') or ''}".lower()
        if re.search(r"\b(klausur|test|prüfung|pruefung)\b", title):
            categories["exam"] += 1
        if re.search(r"\b(schule|unterricht|deutsch|mathe|englisch|bio|physik|geschichte|chemie)\b", title):
            categories["school"] += 1
        if re.search(r"\b(arbeit|job|sportwelt|djk)\b", title):
            categories["work"] += 1
        if re.search(r"\b(fußball|fussball|training geben|coaching|trainer)\b", title):
            categories["coaching"] += 1
        if re.search(r"\b(arzt|physio|therapie)\b", title):
            categories["doctor"] += 1
        if re.search(r"\b(abgabe|projekt|referat)\b", title):
            categories["delivery"] += 1
        if not any(categories[key] for key in ("school", "exam", "work", "coaching", "doctor", "delivery")):
            categories["private"] += 1
        st = _parse_dt(entry.get("start"))
        en = _parse_dt(entry.get("end"))
        if st and en:
            st_local = st.astimezone(ZoneInfo("Europe/Berlin"))
            en_local = en.astimezone(ZoneInfo("Europe/Berlin"))
            start_min = st_local.hour * 60 + st_local.minute
            end_min = en_local.hour * 60 + en_local.minute
            if end_min > start_min and st_local.date() == day:
                intervals.append((start_min, end_min))
    intervals.sort()
    gaps: list[int] = []
    prev_end: int | None = None
    for start_min, end_min in intervals:
        if prev_end is not None and start_min > prev_end:
            gaps.append(start_min - prev_end)
        prev_end = max(prev_end or end_min, end_min)
    entries_count = int(snapshot.get("entries_count") or len(entries))
    busy_minutes = int(snapshot.get("busy_minutes_today") or 0)
    largest_gap = int(snapshot.get("largest_free_window_min") or (max(gaps) if gaps else 0))
    early_start = _text(snapshot.get("school_start"), 10)
    late_end = _text(snapshot.get("school_end"), 10)
    school_stress = "unbekannt"
    if status == "gesichtet":
        if categories["exam"] or categories["delivery"]:
            school_stress = "hoch"
            stress_notes.append("Klausur/Test/Abgabe im Kalender")
        elif entries_count >= 5 or busy_minutes >= 360 or categories["school"] >= 3:
            school_stress = "moderat"
        else:
            school_stress = "niedrig"
    density = "normal"
    if status != "gesichtet":
        density = "unbekannt"
    elif entries_count == 0 or busy_minutes <= 60:
        density = "frei"
    elif entries_count >= 6 or busy_minutes >= 540:
        density = "überladen"
    elif entries_count >= 4 or busy_minutes >= 360:
        density = "dicht"
    time_window = "planbar"
    if status != "gesichtet":
        time_window = "unbekannt"
    elif largest_gap >= 180:
        time_window = "frei"
    elif largest_gap >= 90:
        time_window = "planbar"
    elif largest_gap >= 45:
        time_window = "knapp"
    else:
        time_window = "eng"
    conflict = "keiner"
    if status != "gesichtet":
        conflict = "unbekannt"
    elif time_window in {"eng"} or density == "überladen":
        conflict = "klar"
    elif time_window == "knapp" or density == "dicht":
        conflict = "möglich"
    evening = "ruhig"
    if status != "gesichtet":
        evening = "unbekannt"
    elif late_end and late_end >= "20:30":
        evening = "spät belastet"
    elif late_end and late_end >= "18:00":
        evening = "belegt"
    early = "normal"
    if status != "gesichtet":
        early = "unbekannt"
    elif early_start and early_start <= "06:30":
        early = "sehr früh"
    elif early_start and early_start <= "07:30":
        early = "früh"
    pressure = "moderat"
    if status != "gesichtet":
        pressure = "unbekannt"
    elif school_stress == "hoch" or density == "überladen" or conflict == "klar":
        pressure = "hoch"
    elif school_stress == "niedrig" and density in {"frei", "normal"} and conflict == "keiner":
        pressure = "niedrig"
    notes = _list_text(
        stress_notes
        + ([f"{entries_count} Termine"] if entries_count else ["kein Konflikt erkannt"])
        + ([f"groesstes Zeitfenster {largest_gap} min"] if largest_gap else []),
        4,
    )
    return {
        "status": status,
        "source": source if entries else snapshot.get("source"),
        "entries_count": entries_count,
        "busy_minutes": busy_minutes,
        "largest_free_window_min": largest_gap,
        "school_start": early_start,
        "school_end": late_end,
        "school_stress": school_stress,
        "calendar_density": density,
        "time_window": time_window,
        "training_conflict": conflict,
        "evening_load": evening,
        "early_start_state": early,
        "alltagsdruck": pressure,
        "categories": categories,
        "notes": notes,
        "calendar_available": bool(snapshot.get("calendar_available")),
        "error": error,
    }


def _data_status(day_iso: str, plan_day: dict[str, Any]) -> dict[str, Any]:
    training_latest = _latest_training_session()
    domains = {
        "training_plan": {"status": "fresh" if plan_day.get("available") else "partial", "source": plan_day.get("source")},
        "training_log": {"status": "fresh" if training_latest else "missing", "latest": training_latest},
        "recovery": _recovery_status(),
        "weight": _weight_status(),
        "nutrition": _nutrition_status(),
    }
    missing = [name for name, payload in domains.items() if payload.get("status") == "missing"]
    partial = [name for name, payload in domains.items() if payload.get("status") in {"partial", "stale"}]
    return {
        "date": day_iso,
        "status": "ok" if not missing and not partial else "partial" if len(missing) < len(domains) else "missing",
        "domains": domains,
        "missing": missing,
        "partial": partial,
    }


def calculate_data_freshness(
    day_iso: str,
    *,
    plan_day: dict[str, Any],
    readiness: dict[str, Any],
    calendar_interpretation: dict[str, Any],
    training_card_row: dict[str, Any] | None,
) -> dict[str, Any]:
    recovery = _recovery_status()
    weight = _weight_status()
    nutrition = _nutrition_status()
    training = _latest_training_session()
    subjective_snapshot = _subjective_signal_snapshot(readiness, day_iso)
    polar_sleep = _latest_polar_sleep_signal(day_iso)
    sources: list[dict[str, Any]] = []

    def add_source(key: str, label: str, freshness: str, detail: str, *, critical: bool = True) -> None:
        sources.append({"key": key, "label": label, "freshness": freshness, "detail": detail, "critical": critical})

    add_source("weight", "Gewicht", _day_age_label(weight.get("latest_date")), f"letzter Eintrag {weight.get('latest_date') or 'fehlt'}")
    add_source("hrv", "HRV", _day_age_label(recovery.get("latest_date")), f"letzte Messung {recovery.get('latest_date') or 'fehlt'}")
    add_source("resting_hr", "Resting HR", _day_age_label(recovery.get("latest_date")), f"letzte Messung {recovery.get('latest_date') or 'fehlt'}")
    add_source("recovery", "Recovery", _day_age_label(recovery.get("latest_date")), f"Recovery-Quelle {recovery.get('latest_date') or 'fehlt'}")
    nutrition_freshness = "frisch" if nutrition.get("status") == "fresh" else "gesichtet" if nutrition.get("status") == "partial" else "fehlt"
    add_source("nutrition", "Nutrition", nutrition_freshness, "Tageslogs vorhanden" if nutrition.get("log_count") else "keine aktuellen Tageslogs")
    training_day = (training or {}).get("date_iso")
    add_source("training", "Training", _day_age_label(training_day), f"letztes Workout {training_day or 'fehlt'}")
    calendar_freshness = str(calendar_interpretation.get("status") or "offen")
    add_source("calendar", "Kalender", calendar_freshness, f"{calendar_interpretation.get('entries_count') or 0} Termine gelesen")
    morning_freshness = subjective_snapshot.get("freshness") or "fehlt"
    if subjective_snapshot.get("present_today"):
        morning_detail = "heute vorhanden"
    elif morning_freshness == "gestern":
        morning_detail = "gestern · nicht verwendet"
    elif morning_freshness == "veraltet":
        morning_detail = f"{subjective_snapshot.get('observed_day') or 'älter'} · nicht verwendet"
    else:
        morning_detail = "fehlt heute"
    add_source("morning_checkin", "Morning Check-in", morning_freshness, morning_detail)
    add_source("motivation", "Motivation", "frisch" if subjective_snapshot.get("present_today") and subjective_snapshot.get("motivation") is not None else morning_freshness if subjective_snapshot.get("motivation") is not None else "fehlt", f"heute {_fmt_num(subjective_snapshot.get('motivation'), 1)}/10" if subjective_snapshot.get("present_today") and subjective_snapshot.get("motivation") is not None else ("letzter Wert: gestern, nicht verwendet" if morning_freshness == "gestern" and subjective_snapshot.get("motivation") is not None else "fehlt heute"), critical=False)
    add_source("energy", "Energie", "frisch" if subjective_snapshot.get("present_today") and subjective_snapshot.get("fatigue") is not None else morning_freshness if subjective_snapshot.get("fatigue") is not None else "fehlt", f"heute {_fmt_num(10.0 - float(subjective_snapshot.get('fatigue')), 1)}/10" if subjective_snapshot.get("present_today") and subjective_snapshot.get("fatigue") is not None else ("letzter Wert: gestern, nicht verwendet" if morning_freshness == "gestern" and subjective_snapshot.get("fatigue") is not None else "fehlt heute"), critical=False)
    add_source("pain", "Schmerz", "frisch" if subjective_snapshot.get("present_today") and subjective_snapshot.get("pain") is not None else "fehlt", f"heute {_fmt_num(subjective_snapshot.get('pain'), 1)}/10" if subjective_snapshot.get("present_today") and subjective_snapshot.get("pain") is not None else "fehlt heute", critical=False)
    sleep_score_freshness = "frisch" if polar_sleep.get("present_today") and polar_sleep.get("sleep_score") is not None else polar_sleep.get("freshness") if polar_sleep.get("sleep_score") is not None else "fehlt"
    sleep_score_detail = (
        f"Polar Sleep Score {int(round(float(polar_sleep.get('sleep_score'))))}/100"
        if polar_sleep.get("present_today") and polar_sleep.get("sleep_score") is not None
        else ("letzter Sleep Score: gestern, nicht verwendet" if sleep_score_freshness == "gestern" else "fehlt")
    )
    add_source("sleep_score", "Schlafscore", sleep_score_freshness or "fehlt", sleep_score_detail, critical=False)
    sleep_duration_freshness = "frisch" if polar_sleep.get("present_today") and polar_sleep.get("sleep_minutes") is not None else "fehlt"
    sleep_duration_detail = (
        f"{_fmt_minutes(polar_sleep.get('sleep_minutes'))}"
        if polar_sleep.get("present_today") and polar_sleep.get("sleep_minutes") is not None
        else ("Schlafscore vorhanden, Dauer fehlt" if polar_sleep.get("present_today") and polar_sleep.get("sleep_score") is not None else "fehlt")
    )
    add_source("sleep_duration", "Schlafdauer", sleep_duration_freshness, sleep_duration_detail, critical=False)
    if training_card_row:
        card_day = _text(training_card_row.get("day_iso"), 20)
        add_source("training_card", "Training-Card", _day_age_label(card_day), training_card_row.get("title") or "Card vorhanden", critical=False)
    else:
        add_source("training_card", "Training-Card", "fehlt", "noch nicht gebaut", critical=False)

    counts: dict[str, int] = {}
    for item in sources:
        freshness = str(item.get("freshness") or "fehlt")
        counts[freshness] = counts.get(freshness, 0) + 1
    stale_count = counts.get("gestern", 0) + counts.get("veraltet", 0)
    missing_critical = len([item for item in sources if item.get("critical") and item.get("freshness") in {"fehlt", "offen", "fehler"}])
    return {"sources": sources, "counts": counts, "stale_count": stale_count, "missing_critical": missing_critical}


def _training_card(day_iso: str, plan_day: dict[str, Any], legacy_core: dict[str, Any]) -> dict[str, Any]:
    events = plan_day.get("events") if isinstance(plan_day.get("events"), list) else []
    first = events[0] if events else {}
    exercises = plan_day.get("exercises") if isinstance(plan_day.get("exercises"), list) else []
    if not events:
        return {
            "type": "rest",
            "session_type": "rest",
            "session_label": "Recovery",
            "planned_session_label": "Recovery",
            "recommended_session_label": "Recovery",
            "adapted_from_plan": False,
            "title": "Recovery / kein fixes Training",
            "subtitle": "Heute liegt kein aktiver Trainingstag im Plan.",
            "intent": "recover",
            "goal": "Heute locker bleiben und nur leichte Bewegung mitnehmen.",
            "sections": [{"title": "Heute wichtig", "items": ["Spaziergang oder Mobility locker halten.", "Kein spontanes hartes Nachholen."]}],
            "exercises": [],
            "fallback": True,
            "source": plan_day.get("source"),
        }
    title = _text(first.get("title"), 120) or "Training"
    kind = str(first.get("kind") or "gym").lower()
    title_lower = title.lower()
    is_cardio = kind in {"run", "ergo"} or any(token in title_lower for token in ("z2", "run", "cardio", "ergo"))
    if is_cardio:
        explicit_ergo = kind == "ergo" and any(token in title_lower for token in ("ergo", "bike", "rad", "watt"))
        session_type = "ergo" if explicit_ergo else "run"
        session_label = "Ergo Z2" if explicit_ergo else "Run Z2"
        return {
            "type": "cardio",
            "session_type": session_type,
            "session_label": session_label,
            "planned_session_label": session_label,
            "recommended_session_label": session_label,
            "adapted_from_plan": False,
            "title": "Z2 Run",
            "subtitle": f"{plan_day.get('day_key')} · {plan_day.get('plan_name') or 'Aktiver Plan'}",
            "intent": "train_controlled",
            "variant": "kontrolliert",
            "goal": "Ziel: ruhig, sauber, kontrolliert",
            "metrics": [
                {"label": "Dauer", "value": _text(first.get('time'), 40) or "aus Plan übernehmen"},
                {"label": "Puls", "value": "Deckel respektieren"},
                {"label": "Tempo", "value": "keine Pace-Jagd"},
                {"label": "Danach", "value": "Mobility"},
            ],
            "sections": [
                {"title": "Leitplanken", "items": ["Pulsdeckel respektieren.", "Tempo bewusst zweitrangig halten.", "Danach kurz Mobility einplanen."]},
                {"title": "Abbruchregel", "items": ["Wenn Puls driftet oder die Beine früh schwer werden, lockerer werden statt drücken."]},
            ],
            "exercises": [],
            "fallback": True,
            "source": plan_day.get("source"),
        }
    constraints = ["Saubere Ausführung vor Progression.", "Wenn Warm-up schwer wirkt: Volumen reduzieren."]
    if exercises:
        constraints.append("Top-Sets erst nehmen, wenn Technik und Tempo stabil bleiben.")
    return {
        "type": "run" if kind in {"run", "ergo"} else "gym",
        "session_type": "run" if kind in {"run", "ergo"} else "gym",
        "session_label": title,
        "planned_session_label": title,
        "recommended_session_label": title,
        "adapted_from_plan": False,
        "title": title,
        "subtitle": f"{plan_day.get('day_key')} · {plan_day.get('plan_name') or 'Aktiver Plan'}",
        "intent": "train_controlled",
        "variant": "kontrolliert",
        "goal": "Geplante Einheit sauber aus dem Plan übernehmen.",
        "metrics": [
            {"label": "Struktur", "value": "aus Plan übernehmen"},
            {"label": "Last", "value": "letztes gutes Arbeitsgewicht"},
            {"label": "Tempo", "value": "sauber vor schwer"},
            {"label": "Reserve", "value": "bei Bedarf Satzdruck rausnehmen"},
        ],
        "sections": [{"title": "Leitplanken", "items": constraints[:3]}],
        "exercises": exercises[:6],
        "fallback": not bool(exercises),
        "source": plan_day.get("source"),
        "legacy_hint": {
            "mode": ((legacy_core.get("today") or {}).get("gym") or {}).get("mode_effective")
            if isinstance(legacy_core.get("today"), dict)
            else None
        },
    }


def _text_tokens_from_fields(headline: str | None, summary: str | None, reasons: list[str]) -> list[str]:
    haystack = " ".join([headline or "", summary or "", *[str(item or "") for item in reasons]]).lower()
    found: list[str] = []
    for label, tokens in SESSION_TEXT_TOKENS.items():
        if any(re.search(rf"\b{re.escape(token)}\b", haystack) for token in tokens):
            found.append(label)
    return found


def _normalize_gym_split_label(value: Any) -> str | None:
    text = str(value or "").strip().lower()
    if not text:
        return None
    if re.search(r"\bpush\b", text) or re.search(r"\b(chest|brust)\b", text):
        return "push"
    if re.search(r"\bpull\b", text) or re.search(r"\b(back|rücken|ruecken)\b", text):
        return "pull"
    if re.search(r"\b(legs|leg|beine)\b", text):
        return "legs"
    if re.search(r"\blower\b", text):
        return "lower"
    if re.search(r"\bupper\b", text):
        return "upper"
    return None


def _normalize_detected_gym_split(tokens: set[str]) -> str | None:
    if "push" in tokens or "chest" in tokens:
        return "push"
    if "pull" in tokens or "back" in tokens:
        return "pull"
    if "legs" in tokens:
        return "legs"
    if "lower" in tokens:
        return "lower"
    if "upper" in tokens:
        return "upper"
    return None


def _session_expectation(card: dict[str, Any]) -> dict[str, Any]:
    planned_label = _text(card.get("planned_session_label") or card.get("session_label") or card.get("title"), 120) or "Training"
    recommended_label = _text(card.get("recommended_session_label") or planned_label, 120) or planned_label
    session_type = _text(card.get("session_type") or card.get("type"), 40) or "unknown"
    label_text = f"{planned_label} {recommended_label}".lower()
    if "push" in label_text:
        family = "push"
    elif "pull" in label_text:
        family = "pull"
    elif "legs" in label_text or "lower" in label_text:
        family = "lower"
    elif "upper" in label_text:
        family = "upper"
    elif session_type in {"run", "ergo"} or any(token in label_text for token in ("run", "z2", "ergo", "bike", "rad")):
        family = "cardio"
    elif session_type in {"rest", "recovery", "hard_stop"}:
        family = "recovery"
    elif session_type == "gym":
        family = "gym"
    else:
        family = "unknown"
    split_label = _normalize_gym_split_label(f"{planned_label} {recommended_label}")
    return {
        "planned_session_label": planned_label,
        "recommended_session_label": recommended_label,
        "session_type": session_type,
        "family": family,
        "split_label": split_label,
    }


def _card_conflicts_with_authoritative(candidate: dict[str, Any], authoritative_card: dict[str, Any]) -> bool:
    authoritative = _session_expectation(authoritative_card)
    supplied = _session_expectation(candidate)
    authoritative_family = str(authoritative.get("family") or "unknown")
    supplied_family = str(supplied.get("family") or "unknown")
    family_conflict = (
        authoritative_family in {"push", "pull", "lower", "upper", "gym", "cardio"}
        and supplied_family in {"recovery", "unknown"}
    )
    split_conflict = bool(
        authoritative.get("split_label")
        and supplied.get("split_label")
        and authoritative.get("split_label") != supplied.get("split_label")
    )
    return family_conflict or split_conflict


def _board_consistency(board: dict[str, Any], training_card: dict[str, Any] | None = None) -> dict[str, Any]:
    card = training_card if isinstance(training_card, dict) and training_card else (board.get("training_card") if isinstance(board.get("training_card"), dict) else {})
    expected = _session_expectation(card)
    reasons = board.get("visible_reasons") if isinstance(board.get("visible_reasons"), list) else []
    headline_detected = _text_tokens_from_fields(board.get("human_headline"), None, [])
    summary_detected = _text_tokens_from_fields(None, board.get("human_summary"), [])
    primary_detected = []
    for token in [*headline_detected, *summary_detected]:
        if token not in primary_detected:
            primary_detected.append(token)
    reasons_detected = _text_tokens_from_fields(None, None, reasons)
    detected = []
    for token in [*primary_detected, *reasons_detected]:
        if token not in detected:
            detected.append(token)
    status = "ok"
    message = "Board-Text passt zur heutigen Training-Card."

    cardio_tokens = {"run", "z2", "ergo", "bike"}
    gym_tokens = {"push", "pull", "legs", "lower", "upper"}
    recovery_tokens = {"rest", "recovery"}
    primary_set = set(primary_detected)
    headline_set = set(headline_detected)
    summary_set = set(summary_detected)
    detected_set = set(detected)
    family = expected.get("family")
    expected_split = str(expected.get("split_label") or "").strip().lower() or None
    primary_split = _normalize_detected_gym_split(primary_set)
    detected_split = _normalize_detected_gym_split(detected_set)

    if family in {"push", "pull", "lower", "upper", "gym"}:
        if expected_split and primary_split and primary_split != expected_split:
            return {
                "status": "mismatch",
                "planned_session_label": expected.get("planned_session_label"),
                "recommended_session_label": expected.get("recommended_session_label"),
                "detected_text_tokens": detected,
                "message": "Board-Text nennt eine andere Gym-Einheit als die heutige Training-Card.",
                "checked_fields": ["human_headline", "human_summary", "visible_reasons"],
            }
        if expected_split and not primary_split and detected_split and detected_split != expected_split and detected_split in GYM_SPLIT_TOKENS:
            status = "warning"
            message = "Visible Reasons nennen eine andere Gym-Einheit als die heutige Training-Card."
        for field_set in (headline_set, summary_set):
            if field_set & cardio_tokens:
                return {
                    "status": "mismatch",
                    "planned_session_label": expected.get("planned_session_label"),
                    "recommended_session_label": expected.get("recommended_session_label"),
                    "detected_text_tokens": detected,
                    "message": "Board-Text passt nicht zur heutigen Training-Card.",
                    "checked_fields": ["human_headline", "human_summary", "visible_reasons"],
                }
        conflicting = primary_set & cardio_tokens
        aligned = primary_set & (gym_tokens | GENERIC_BOARD_TOKENS)
        if conflicting:
            status = "warning"
            message = "Board-Text mischt Hinweise aus verschiedenen Einheitentypen."
            return {
                "status": status,
                "planned_session_label": expected.get("planned_session_label"),
                "recommended_session_label": expected.get("recommended_session_label"),
                "detected_text_tokens": detected,
                "message": message,
                "checked_fields": ["human_headline", "human_summary", "visible_reasons"],
            }
        if not conflicting and not aligned:
            conflicting = detected_set & cardio_tokens
            aligned = detected_set & (gym_tokens | GENERIC_BOARD_TOKENS)
        if conflicting and not aligned:
            status = "mismatch"
            message = "Board-Text passt nicht zur heutigen Training-Card."
        elif conflicting:
            status = "warning"
            message = "Board-Text mischt Hinweise aus verschiedenen Einheitentypen."
    elif family == "cardio":
        for field_set in (headline_set, summary_set):
            if field_set & gym_tokens:
                return {
                    "status": "mismatch",
                    "planned_session_label": expected.get("planned_session_label"),
                    "recommended_session_label": expected.get("recommended_session_label"),
                    "detected_text_tokens": detected,
                    "message": "Board-Text passt nicht zur heutigen Training-Card.",
                    "checked_fields": ["human_headline", "human_summary", "visible_reasons"],
                }
        conflicting = primary_set & gym_tokens
        aligned = primary_set & (cardio_tokens | {"mobility"})
        if conflicting:
            status = "warning"
            message = "Board-Text mischt Hinweise aus verschiedenen Einheitentypen."
            return {
                "status": status,
                "planned_session_label": expected.get("planned_session_label"),
                "recommended_session_label": expected.get("recommended_session_label"),
                "detected_text_tokens": detected,
                "message": message,
                "checked_fields": ["human_headline", "human_summary", "visible_reasons"],
            }
        if not conflicting and not aligned:
            conflicting = detected_set & gym_tokens
            aligned = detected_set & (cardio_tokens | {"mobility"})
        if conflicting and not aligned:
            status = "mismatch"
            message = "Board-Text passt nicht zur heutigen Training-Card."
        elif conflicting:
            status = "warning"
            message = "Board-Text mischt Hinweise aus verschiedenen Einheitentypen."
    elif family == "recovery":
        for field_set in (headline_set, summary_set):
            if field_set & (gym_tokens | cardio_tokens):
                return {
                    "status": "mismatch",
                    "planned_session_label": expected.get("planned_session_label"),
                    "recommended_session_label": expected.get("recommended_session_label"),
                    "detected_text_tokens": detected,
                    "message": "Board-Text passt nicht zur heutigen Training-Card.",
                    "checked_fields": ["human_headline", "human_summary", "visible_reasons"],
                }
        conflicting = primary_set & (gym_tokens | cardio_tokens)
        aligned = primary_set & recovery_tokens
        if conflicting:
            status = "warning"
            message = "Board-Text mischt Hinweise aus verschiedenen Einheitentypen."
            return {
                "status": status,
                "planned_session_label": expected.get("planned_session_label"),
                "recommended_session_label": expected.get("recommended_session_label"),
                "detected_text_tokens": detected,
                "message": message,
                "checked_fields": ["human_headline", "human_summary", "visible_reasons"],
            }
        if not conflicting and not aligned:
            conflicting = detected_set & (gym_tokens | cardio_tokens)
            aligned = detected_set & recovery_tokens
        if conflicting and not aligned:
            status = "mismatch"
            message = "Board-Text passt nicht zur heutigen Training-Card."
        elif conflicting:
            status = "warning"
            message = "Board-Text mischt Hinweise aus verschiedenen Einheitentypen."
    elif not detected:
        status = "warning"
        message = "Board-Text nennt die heutige Einheit nicht klar."

    return {
        "status": status,
        "planned_session_label": expected.get("planned_session_label"),
        "recommended_session_label": expected.get("recommended_session_label"),
        "detected_text_tokens": detected,
        "message": message,
        "checked_fields": ["human_headline", "human_summary", "visible_reasons"],
    }


def enrich_core_board(board: dict[str, Any], training_card: dict[str, Any] | None = None) -> dict[str, Any]:
    if not isinstance(board, dict) or not board:
        return board
    out = dict(board)
    day = _parse_day(out.get("day_iso") or out.get("date"))
    legacy = out.get("legacy_core") if isinstance(out.get("legacy_core"), dict) else {}
    out["legacy_core_status"] = _legacy_core_status(day, legacy)
    out["board_consistency"] = _board_consistency(out, training_card=training_card)
    return out


def _make_signal(
    key: str,
    label: str,
    state: str,
    value: str,
    impact: str,
    *,
    freshness: str = "unbekannt",
    confidence: str = "low",
    evidence: str | None = None,
) -> dict[str, Any]:
    return {
        "key": key,
        "label": label,
        "state": state,
        "value": value,
        "impact": impact,
        "confidence": confidence,
        "freshness": freshness,
        "evidence": evidence or value,
        "badge": str(state or "").strip().upper(),
        "badge_tone": _status_context_tone(str(state or "").strip().upper()),
        "headline": value,
        "priority": 0,
    }


def build_core_signals(
    day_iso: str,
    *,
    board: dict[str, Any],
    card: dict[str, Any],
    readiness: dict[str, Any],
    data_freshness: dict[str, Any],
    calendar_interpretation: dict[str, Any],
) -> dict[str, Any]:
    weights = _read_weight_history(day_iso)
    recovery_hist = _read_recovery_history(day_iso)
    sessions = _read_training_sessions(day_iso)
    runs = _read_runs(day_iso)
    weight_trend = build_weight_trend(
        [{"date": row["day_iso"], "weight": row["weight"]} for row in weights], day_iso
    )
    calendar_week = weight_trend["calendar_week"]
    latest_weight = _float_or_none((weight_trend.get("latest_measurement") or {}).get("weight_kg"))
    hrv_today = recovery_hist[0]["rmssd"] if recovery_hist else None
    hrv3 = _avg([row["rmssd"] for row in recovery_hist[:3] if row.get("rmssd") is not None])
    hrv7 = _avg([row["rmssd"] for row in recovery_hist[:7] if row.get("rmssd") is not None])
    hrv14 = _avg([row["rmssd"] for row in recovery_hist[:14] if row.get("rmssd") is not None])
    rhr_today = recovery_hist[0]["hr"] if recovery_hist else None
    rhr3 = _avg([row["hr"] for row in recovery_hist[:3] if row.get("hr") is not None])
    rhr14 = _avg([row["hr"] for row in recovery_hist[:14] if row.get("hr") is not None])
    subjective_snapshot = _subjective_signal_snapshot(readiness, day_iso)
    polar_sleep = _latest_polar_sleep_signal(day_iso)
    sleep_today = subjective_snapshot.get("sleep_quality") if subjective_snapshot.get("present_today") else None
    sleep7 = _avg([row["sleep_quality"] for row in recovery_hist[:7] if row.get("sleep_quality") is not None])
    motivation_today = subjective_snapshot.get("motivation") if subjective_snapshot.get("present_today") else None
    fatigue_today = subjective_snapshot.get("fatigue") if subjective_snapshot.get("present_today") else None
    pain_today = subjective_snapshot.get("pain") if subjective_snapshot.get("present_today") else None
    polar_sleep_score = polar_sleep.get("sleep_score") if polar_sleep.get("present_today") else None
    polar_sleep_minutes = polar_sleep.get("sleep_minutes") if polar_sleep.get("present_today") else None
    label_text = str(card.get("title") or "").lower()
    training_last_3d = len([row for row in sessions if (date.fromisoformat(day_iso) - date.fromisoformat(row["day_iso"])).days <= 2])
    training_last_7d = len([row for row in sessions if (date.fromisoformat(day_iso) - date.fromisoformat(row["day_iso"])).days <= 6])
    training_last_28d = len([row for row in sessions if (date.fromisoformat(day_iso) - date.fromisoformat(row["day_iso"])).days <= 27])
    heavy_7d = len([row for row in sessions if (date.fromisoformat(day_iso) - date.fromisoformat(row["day_iso"])).days <= 6 and row.get("is_heavy")])
    skipped_28d = len([row for row in sessions[:12] if row.get("is_skipped")])
    run_7d = [row for row in runs if (date.fromisoformat(day_iso) - date.fromisoformat(row["day_iso"])).days <= 6]
    cardio_rows_7d = []
    actual_runs = []
    ergo_7d = []
    for row in run_7d:
        cardio_kind = _classify_cardio_entry(row)
        if not cardio_kind:
            continue
        cardio_rows_7d.append(row)
        if cardio_kind == "run":
            actual_runs.append(row)
        elif cardio_kind == "ergo":
            ergo_7d.append(row)
    run_minutes_7d = sum(float(row.get("duration") or 0.0) for row in actual_runs if row.get("duration") is not None)
    run_distance_7d = sum(float(row.get("distance") or 0.0) for row in actual_runs if row.get("distance") is not None)
    ergo_minutes_7d = sum(float(row.get("duration") or 0.0) for row in ergo_7d if row.get("duration") is not None)
    ergo_distance_7d = sum(float(row.get("distance") or 0.0) for row in ergo_7d if row.get("distance") is not None)
    cardio_avg_power = _avg([float(row.get("avg_power")) for row in ergo_7d if row.get("avg_power") is not None])
    cardio_hr_samples = [float(row.get("avg_hr")) for row in cardio_rows_7d if row.get("avg_hr") is not None]
    cardio_day = bool(str(card.get("type") or "").lower() in {"run", "ergo", "cardio"} or re.search(r"\b(z2|run|laufen|lauf|ergo|bike|cardio)\b", label_text))
    has_cardio_context = bool(cardio_rows_7d or cardio_day)
    fresh = {item["key"]: item["freshness"] for item in data_freshness.get("sources", [])}
    signals: list[dict[str, Any]] = []
    plan_day = _plan_day(day_iso)
    plan_events = plan_day.get("events") if isinstance(plan_day.get("events"), list) else []
    training_time = _parse_hhmm((plan_events[0] or {}).get("time")) if plan_events else None

    def signal(key: str, label: str, badge: str, headline: str, evidence: str, impact: str, *, freshness: str = "unbekannt", confidence: str = "medium") -> None:
        signals.append(
            _make_signal(
                key,
                label,
                badge,
                headline,
                impact,
                freshness=freshness,
                confidence=confidence,
                evidence=evidence,
            )
        )

    if latest_weight is not None and calendar_week["current"]["mean_kg"] is not None:
        delta = calendar_week["delta_kg"]
        status = calendar_week["status"]
        current_week = calendar_week["current"]
        state = "stabil" if delta is not None and abs(delta) < 0.15 else "steigend" if delta is not None and delta > 0 else "fallend"
        impact = "unknown" if status != "usable" else "neutral" if delta is not None and abs(delta) < 0.25 else "caution"
        badge = "STABIL" if delta is not None and abs(delta) < 0.15 else "STEIGEND" if delta is not None and delta > 0 else "FALLEND"
        headline = "Kalenderwochen-Trend vorläufig" if status != "usable" else "Gewicht aktuell stabil" if badge == "STABIL" else "Gewicht zieht leicht an" if badge == "STEIGEND" else "Gewicht leicht fallend"
        delta_text = _fmt_signed(delta, "kg", 1) if delta is not None else "noch kein Vergleich"
        signal("weight_trend", "Gewichtstrend", badge if delta is not None else "VORLÄUFIG", headline, f"KW {current_week['iso_week']}: {current_week['sample_count']}/7; vs. Vorwoche {delta_text}", impact, freshness=fresh.get("weight", "fehlt"))
        week_mean = float(current_week["mean_kg"])
        level = "auf Linie" if abs(latest_weight - week_mean) < 0.25 else "über Linie" if latest_weight > week_mean else "unter Linie"
        signal("weight_level", "Gewichtslage", level.upper(), "Heute über der Wochenlinie" if level == "über Linie" else "Heute unter der Wochenlinie" if level == "unter Linie" else "Heute auf der Wochenlinie", f"{_fmt_signed(latest_weight - week_mean, 'kg', 1)} vs. KW-Mittel", "neutral", freshness=fresh.get("weight", "fehlt"))
    else:
        signal("weight_trend", "Gewichtstrend", "UNBEKANNT", "Nicht sauber beurteilbar", "Zu wenig Gewichtsverlauf", "unknown", freshness=fresh.get("weight", "fehlt"))
        signal("weight_level", "Gewichtslage", "UNBEKANNT", "Heute nicht sauber einordenbar", "Kalenderwochenvergleich fehlt", "unknown", freshness=fresh.get("weight", "fehlt"))

    if hrv_today is not None and hrv14 is not None:
        delta = hrv_today - hrv14
        state = "positiv" if delta >= 8 else "normal" if delta >= -6 else "gedrückt" if delta >= -14 else "kritisch"
        impact = "push" if state == "positiv" else "neutral" if state == "normal" else "caution" if state == "gedrückt" else "limit"
        badge = {"positiv": "POSITIV", "normal": "NORMAL", "gedrückt": "GEDRÜCKT", "kritisch": "KRITISCH"}[state]
        headline = "Klar über Baseline" if badge == "POSITIV" else "Nahe an der Baseline" if badge == "NORMAL" else "Spürbar unter Baseline" if badge == "GEDRÜCKT" else "Recovery klar gedrückt"
        signal("hrv_level", "HRV-Lage", badge, headline, f"{_fmt_signed(delta, 'ms', 0)} vs. Baseline", impact, freshness=fresh.get("hrv", "fehlt"))
    else:
        signal("hrv_level", "HRV-Lage", "UNBEKANNT", "Nicht sauber messbar", "HRV-Baseline fehlt", "unknown", freshness=fresh.get("hrv", "fehlt"))
    if hrv3 is not None and hrv14 is not None:
        delta = hrv3 - hrv14
        state = "stabil" if abs(delta) < 5 else "steigend" if delta > 0 else "fallend"
        signal("hrv_trend", "HRV-Trend", state.upper(), "Trend zieht an" if state == "steigend" else "Trend stabil" if state == "stabil" else "Trend fällt", f"3T vs. 14T {_fmt_signed(delta, 'ms', 0)}", "push" if delta > 6 else "caution" if delta < -6 else "neutral", freshness=fresh.get("hrv", "fehlt"))
    else:
        signal("hrv_trend", "HRV-Trend", "UNBEKANNT", "Trend nicht sauber lesbar", "Zu wenig HRV-Verlauf", "unknown", freshness=fresh.get("hrv", "fehlt"))

    if rhr_today is not None and rhr14 is not None:
        delta = rhr_today - rhr14
        state = "ruhig" if delta <= -2 else "normal" if delta <= 2 else "erhöht" if delta <= 5 else "auffällig erhöht"
        impact = "push" if state == "ruhig" else "neutral" if state == "normal" else "caution" if state == "erhöht" else "limit"
        badge = {"ruhig": "RUHIG", "normal": "NORMAL", "erhöht": "ERHÖHT", "auffällig erhöht": "KRITISCH"}[state]
        headline = "Puls ruhig" if badge == "RUHIG" else "Puls im Normalbereich" if badge == "NORMAL" else "Puls leicht erhöht" if badge == "ERHÖHT" else "Puls auffällig erhöht"
        signal("rhr_level", "Resting-HR-Lage", badge, headline, f"{_fmt_signed(delta, 'bpm', 0)} vs. Baseline", impact, freshness=fresh.get("resting_hr", "fehlt"))
    else:
        signal("rhr_level", "Resting-HR-Lage", "UNBEKANNT", "Nicht sauber messbar", "RHR-Baseline fehlt", "unknown", freshness=fresh.get("resting_hr", "fehlt"))
    if rhr3 is not None and rhr14 is not None:
        delta = rhr3 - rhr14
        state = "stabil" if abs(delta) < 1.5 else "fällt" if delta < 0 else "driftet hoch"
        signal("rhr_trend", "Resting-HR-Trend", state.upper(), "Trend sinkt" if state == "fällt" else "Trend stabil" if state == "stabil" else "Trend driftet hoch", f"3T vs. 14T {_fmt_signed(delta, 'bpm', 0)}", "caution" if delta > 2 else "neutral", freshness=fresh.get("resting_hr", "fehlt"))
    else:
        signal("rhr_trend", "Resting-HR-Trend", "UNBEKANNT", "Trend nicht sauber lesbar", "Zu wenig RHR-Verlauf", "unknown", freshness=fresh.get("resting_hr", "fehlt"))

    if hrv_today is not None and rhr_today is not None and hrv14 is not None and rhr14 is not None:
        positive = hrv_today >= hrv14 and rhr_today <= rhr14
        mixed = (hrv_today >= hrv14 and rhr_today > rhr14) or (hrv_today < hrv14 and rhr_today <= rhr14)
        state = "eindeutig positiv" if positive else "gemischt" if mixed else "widersprüchlich"
        impact = "push" if positive else "caution" if mixed else "limit"
        badge = "POSITIV" if positive else "GEMISCHT" if mixed else "KRITISCH"
        headline = "Recovery zieht in eine Richtung" if badge == "POSITIV" else "Recovery-Signale gemischt" if badge == "GEMISCHT" else "Recovery deckelt heute"
        signal("recovery_conflict", "Recovery-Konflikt", badge, headline, "HRV und RHR gemeinsam bewertet", impact, freshness=fresh.get("recovery", "fehlt"))
    else:
        signal("recovery_conflict", "Recovery-Konflikt", "UNBEKANNT", "Noch nicht sauber kombinierbar", "HRV- oder RHR-Basis fehlt", "unknown", freshness=fresh.get("recovery", "fehlt"))

    if polar_sleep_score is not None:
        badge = "GUT" if polar_sleep_score >= 85 else "STABIL" if polar_sleep_score >= 75 else "BEOBACHTEN"
        headline = "Schlafscore gut" if badge == "GUT" else "Schlafscore solide" if badge == "STABIL" else "Schlafscore eher schwach"
        impact = "push" if badge == "GUT" else "neutral" if badge == "STABIL" else "caution"
        evidence = f"Polar Sleep Score {int(round(float(polar_sleep_score)))}/100"
        if polar_sleep_minutes is None:
            evidence += " · Dauer fehlt"
        signal("sleep_quality", "Schlafqualität", badge, headline, evidence, impact, freshness=fresh.get("sleep_score", "fehlt"))
    elif sleep_today is not None and sleep7 is not None:
        delta = sleep_today - sleep7
        state = "stabil" if abs(delta) < 0.6 else "besser" if delta > 0 else "schlechter"
        impact = "push" if delta > 1 else "caution" if delta < -1 else "neutral"
        signal("sleep_quality", "Schlafqualität", state.upper(), "Schlafqualität besser" if state == "besser" else "Schlafqualität stabil" if state == "stabil" else "Schlafqualität schwächer", f"Heute {_fmt_num(sleep_today, 1)}/10 · 7T {_fmt_num(sleep7, 1)}", impact, freshness=fresh.get("morning_checkin", "fehlt"))
    elif sleep_today is not None:
        signal("sleep_quality", "Schlafqualität", "STABIL", "Heutiger Schlafwert vorhanden", f"Heute {_fmt_num(sleep_today, 1)}/10", "neutral", freshness=fresh.get("morning_checkin", "fehlt"))
    if polar_sleep_minutes is not None:
        debt_state = "OKAY" if polar_sleep_minutes >= 7 * 60 else "BEOBACHTEN" if polar_sleep_minutes >= 6 * 60 else "KRITISCH"
        debt_headline = "Schlafdauer passt" if debt_state == "OKAY" else "Schlafdauer eher knapp" if debt_state == "BEOBACHTEN" else "Schlafdauer klar zu kurz"
        debt_impact = "neutral" if debt_state == "OKAY" else "caution" if debt_state == "BEOBACHTEN" else "limit"
        signal("sleep_debt", "Schlafschuld", debt_state, debt_headline, _fmt_minutes(polar_sleep_minutes), debt_impact, freshness=fresh.get("sleep_duration", "fehlt"))

    energy_series = [10.0 - row["fatigue"] for row in recovery_hist[:7] if row.get("fatigue") is not None]
    energy_today = (10.0 - fatigue_today) if fatigue_today is not None else None
    energy7 = _avg(energy_series[:7])
    energy3 = _avg(energy_series[:3])
    if energy_today is not None and energy7 is not None:
        if abs((energy3 or energy_today) - energy7) >= 1.2:
            badge = "SCHWANKEND"
            headline = "Energie schwankt zuletzt"
            impact = "caution"
        else:
            badge = "GUT" if energy_today >= 7 else "OKAY" if energy_today >= 5 else "NIEDRIG"
            headline = "Heute solide" if badge == "GUT" else "Heute okay" if badge == "OKAY" else "Heute eher niedrig"
            impact = "push" if badge == "GUT" else "neutral" if badge == "OKAY" else "caution"
        signal("energy_level", "Energie-Lage", badge, headline, f"{_fmt_num(energy_today, 1)}/10 · 7T-Schnitt {_fmt_num(energy7, 1)}", impact, freshness=fresh.get("morning_checkin", "fehlt"))
    if motivation_today is not None:
        badge = "GUT" if motivation_today >= 7 else "OKAY" if motivation_today >= 5 else "NIEDRIG"
        headline = "Motivation trägt heute" if badge == "GUT" else "Motivation solide" if badge == "OKAY" else "Motivation eher niedrig"
        signal("motivation_level", "Motivation-Lage", badge, headline, f"{_fmt_num(motivation_today, 1)}/10", "push" if badge == "GUT" else "neutral" if badge == "OKAY" else "caution", freshness=fresh.get("morning_checkin", "fehlt"))
    if pain_today is not None:
        badge = "RUHIG" if pain_today <= 2 else "BEOBACHTEN" if pain_today <= 5 else "KRITISCH"
        headline = "Kein klares Warnsignal" if badge == "RUHIG" else "Warnsignal mitdenken" if badge == "BEOBACHTEN" else "Warnsignal heute relevant"
        signal("pain_signal", "Schmerz-/Warnsignal", badge, headline, f"{_fmt_num(pain_today, 1)}/10", "neutral" if badge == "RUHIG" else "caution" if badge == "BEOBACHTEN" else "limit", freshness=fresh.get("morning_checkin", "fehlt"))

    akut_state = "niedrig" if training_last_3d == 0 else "normal" if training_last_3d == 1 else "hoch"
    signal("acute_load_72h", "Akutlast 72h", akut_state.upper(), "Zuletzt wenig Last" if akut_state == "niedrig" else "Akutlast im Rahmen" if akut_state == "normal" else "Mehrere Einheiten kurz hintereinander", f"{training_last_3d} Einheiten in 72 h", "caution" if akut_state == "hoch" else "neutral", freshness=fresh.get("training", "fehlt"))
    week_ref = (training_last_28d / 4.0) if training_last_28d else None
    if week_ref:
        ratio = training_last_7d / max(1.0, week_ref)
        week_state = "normal" if 0.8 <= ratio <= 1.2 else "unter Schnitt" if ratio < 0.8 else "über Schnitt" if ratio <= 1.5 else "hoch"
        week_impact = "neutral" if week_state in {"normal", "unter Schnitt"} else "caution"
        signal("weekly_load", "Wochenlast", week_state.upper(), "Wochenlast unter Schnitt" if week_state == "unter Schnitt" else "Wochenlast normal" if week_state == "normal" else "Wochenlast über Schnitt" if week_state == "über Schnitt" else "Wochenlast hoch", f"7T {training_last_7d} · 28T-Schnitt {_fmt_num(week_ref, 1)}", week_impact, freshness=fresh.get("training", "fehlt"))
    else:
        signal("weekly_load", "Wochenlast", "UNBEKANNT", "Nicht sauber einordenbar", "Zu wenig Trainingsverlauf", "unknown", freshness=fresh.get("training", "fehlt"))
    sys_state = "niedrig" if training_last_7d <= 2 else "mittel" if training_last_7d <= 4 else "hoch"
    signal("systemic_fatigue", "Systemische Fatigue", sys_state.upper(), "Systemisch ruhig" if sys_state == "niedrig" else "Systemisch im Rahmen" if sys_state == "mittel" else "Systemisch erhöht", f"{training_last_7d} Trainingstage in 7 Tagen", "caution" if sys_state == "hoch" else "neutral", freshness=fresh.get("training", "fehlt"))
    heavy_state = "niedrig" if heavy_7d <= 1 else "normal" if heavy_7d <= 3 else "hoch"
    signal("heavy_set_density", "Heavy-Set-Dichte", heavy_state.upper(), "Kaum schwere Reize" if heavy_state == "niedrig" else "Schwere Reize im Rahmen" if heavy_state == "normal" else "Viele schwere Reize", f"{heavy_7d} harte Einheiten in 7 Tagen", "caution" if heavy_state == "hoch" else "neutral", freshness=fresh.get("training", "fehlt"), confidence="low")

    target_group = "beine" if re.search(r"\b(lower|legs|beine)\b", label_text) else "push" if "push" in label_text else "pull" if "pull" in label_text else "allgemein"
    tg_fresh = "frisch" if training_last_3d == 0 else "belastet" if training_last_3d == 1 else "kritisch"
    signal("target_muscle_readiness", "Zielmuskel-Bereitschaft", tg_fresh.upper(), "Zielmuskeln frisch" if tg_fresh == "frisch" else "Zielmuskeln belastet" if tg_fresh == "belastet" else "Zielmuskeln klar vorbelastet", f"Heutiger Fokus: {target_group.title()}", "push" if tg_fresh == "frisch" else "caution" if tg_fresh == "belastet" else "limit", freshness=fresh.get("training", "fehlt"), confidence="low")
    for key, label in [("chest", "Brust"), ("back", "Ruecken"), ("legs", "Beine"), ("shoulders", "Schulter"), ("arms", "Arme")]:
        state = "kritisch" if target_group == "beine" and key == "legs" and training_last_3d >= 2 else "belastet" if training_last_3d >= 1 else "frisch"
        signal(f"muscle_{key}", f"Muskelgruppe {label}", state.upper(), f"{label} {state}", "Letzte Einheiten als Basis", "neutral" if state == "frisch" else "caution" if state == "belastet" else "limit", freshness=fresh.get("training", "fehlt"), confidence="low")

    signal("performance_trend", "Performance-Trend", "UNBEKANNT", "Nicht sauber beurteilbar", "Ohne Satzdaten kein Trend", "unknown", freshness=fresh.get("training", "fehlt"))
    prog_state = "hoch" if training_last_7d == 0 else "normal" if heavy_7d <= 2 else "niedrig"
    signal("progression_pressure", "Progressionsdruck", prog_state.upper(), "Heute kein großer Progressionsdruck" if prog_state == "niedrig" else "Progressionsdruck im Rahmen" if prog_state == "normal" else "Progressionsdruck eher da", "Aus Trainingsfrequenz abgeleitet", "neutral", freshness=fresh.get("training", "fehlt"), confidence="low")
    stag_state = "leicht" if training_last_28d >= 6 else "kein"
    signal("stagnation_risk", "Stagnationsrisiko", stag_state.upper() if stag_state != "kein" else "KEINER", "Kein klares Stagnationssignal" if stag_state == "kein" else "Leichtes Stagnationsrisiko", f"{training_last_28d} Einheiten im Verlauf", "caution" if stag_state != "kein" else "neutral", freshness=fresh.get("training", "fehlt"), confidence="low")
    exec_state = "hoch" if skipped_28d >= 3 else "mittel" if skipped_28d >= 1 else "niedrig"
    exec_headline = "Zuletzt ohne Kürzungen" if exec_state == "niedrig" else "Zuletzt etwas wacklig" if exec_state == "mittel" else "Zuletzt öfter gekürzt oder abgebrochen"
    exec_evidence = "Keine gekürzten oder abgebrochenen Einheiten im Verlauf" if skipped_28d == 0 else "1 Einheit gekürzt oder abgebrochen" if skipped_28d == 1 else f"{skipped_28d} Einheiten gekürzt oder abgebrochen"
    signal("execution_risk", "Ausführungsrisiko", exec_state.upper(), exec_headline, exec_evidence, "caution" if exec_state != "niedrig" else "neutral", freshness=fresh.get("training", "fehlt"), confidence="low")

    total_cardio_min = run_minutes_7d + ergo_minutes_7d
    if total_cardio_min > 2000 or run_distance_7d > 200 or ergo_distance_7d > 1000:
        signal("cardio_load_7d", "Cardio-Belastung 7T", "UNPLAUSIBEL", "Cardio-Daten unsicher", f"{_fmt_minutes(total_cardio_min)} erkannt · Mapping prüfen", "limit", freshness="gestern")
    else:
        cardio_state = "NIEDRIG" if total_cardio_min == 0 else "NORMAL" if total_cardio_min < 180 else "HOCH"
        cardio_headline = "Kaum Ausdauerzeit" if cardio_state == "NIEDRIG" else "Ausdauerzeit planbar" if cardio_state == "NORMAL" else "Viel Ausdauerzeit"
        dominant = "überwiegend Ergo" if ergo_minutes_7d > run_minutes_7d else "überwiegend Laufen" if run_minutes_7d > ergo_minutes_7d else "gemischt"
        cardio_freshness = "gestern" if cardio_rows_7d else ("offen" if has_cardio_context else "fehlt")
        cardio_evidence = f"{_fmt_minutes(total_cardio_min)} · {dominant}" if cardio_rows_7d else "Keine Cardio-Einheit in 7 Tagen"
        signal("cardio_load_7d", "Cardio-Belastung 7T", cardio_state, cardio_headline, cardio_evidence, "caution" if cardio_state == "HOCH" else "neutral", freshness=cardio_freshness)
    if run_distance_7d > 200 or run_minutes_7d > 24 * 60:
        signal("run_impact", "Laufimpact", "UNPLAUSIBEL", "Laufdaten wirken fehlerhaft", f"{_fmt_num(run_distance_7d, 1)} km in 7 Tagen erkannt", "limit", freshness="gestern")
    else:
        impact_state = "NIEDRIG" if run_distance_7d < 6 else "MODERAT" if run_distance_7d < 20 else "HOCH"
        run_headline = "Kaum Stoßbelastung" if impact_state == "NIEDRIG" else "Laufbelastung im Rahmen" if impact_state == "MODERAT" else "Viel Laufbelastung"
        run_evidence = f"{len(actual_runs)} Läufe · {_fmt_num(run_distance_7d, 1)} km in 7 Tagen" if actual_runs else "Keine Lauf-Einheit in 7 Tagen"
        signal("run_impact", "Laufimpact", impact_state, run_headline, run_evidence, "caution" if impact_state == "HOCH" else "neutral", freshness="gestern" if actual_runs else "fehlt")
    if ergo_minutes_7d > 24 * 60 or ergo_distance_7d > 2000:
        signal("ergo_load", "Ergo-Belastung", "UNPLAUSIBEL", "Ergo-Daten wirken fehlerhaft", f"{_fmt_minutes(ergo_minutes_7d)} · Distanz wird ignoriert", "limit", freshness="gestern")
    else:
        ergo_state = "NIEDRIG" if ergo_minutes_7d < 45 else "MODERAT" if ergo_minutes_7d < 180 else "HOCH"
        ergo_headline = "Kaum Ergo-Belastung" if ergo_state == "NIEDRIG" else "Ergo-Belastung im Rahmen" if ergo_state == "MODERAT" else "Viel lockere Ausdauerarbeit"
        if ergo_7d:
            ergo_evidence = f"{_fmt_minutes(ergo_minutes_7d)} in 7 Tagen"
            if cardio_avg_power is not None:
                ergo_evidence += f" · Ø {_fmt_num(cardio_avg_power, 0)} W"
            ergo_freshness = "gestern"
        else:
            ergo_evidence = "Keine Ergo-Einheit in 7 Tagen"
            ergo_freshness = "fehlt"
        signal("ergo_load", "Ergo-Belastung", ergo_state, ergo_headline, ergo_evidence, "caution" if ergo_state == "HOCH" else "neutral", freshness=ergo_freshness)
    if cardio_day and cardio_hr_samples:
        signal("z2_stability", "Z2-Stabilität", "UNBEKANNT", "Heute noch ohne Z2-Vergleich", "Für ähnliche Z2-Einheiten fehlt eine verwertbare Pulsreihe", "unknown", freshness="offen")
        signal("pulse_drift", "Pulsdrift", "UNBEKANNT", "Pulsdrift heute offen", "Ohne kontinuierliche Pulswerte kein sauberer Driftvergleich", "unknown", freshness="offen")
        signal("power_at_cap", "Leistung bei Pulsdeckel", "UNBEKANNT", "Leistung noch nicht vergleichbar", "Mehrere Einheiten mit ähnlichem Pulsdeckel fehlen", "unknown", freshness="offen")

    nutr_fresh = fresh.get("nutrition", "fehlt")
    signal("calorie_balance", "Kalorienbilanz-Trend", "UNBEKANNT" if nutr_fresh in {"fehlt", "offen"} else "NORMAL", "Noch nicht sauber bewertbar" if nutr_fresh in {"fehlt", "offen"} else "Kalorienlage heute grob okay", "3T/7T-Mittel fehlen", "unknown" if nutr_fresh in {"fehlt", "offen"} else "neutral", freshness=nutr_fresh)
    signal("carb_availability", "Carb-Verfügbarkeit", "UNBEKANNT" if nutr_fresh != "frisch" else "OKAY", "Makros nicht sichtbar" if nutr_fresh != "frisch" else "Für ruhige Einheit ausreichend", "Nutrition erfasst, aber Carbs fehlen" if nutr_fresh != "frisch" else "Heute geloggt · kein Heavy-Fueling-Signal", "unknown" if nutr_fresh != "frisch" else "neutral", freshness=nutr_fresh)
    signal("protein_stability", "Protein-Stabilität", "UNBEKANNT" if nutr_fresh == "fehlt" else "LÜCKENHAFT", "Protein nicht sauber beurteilbar" if nutr_fresh == "fehlt" else "Protein-Tracking noch lückenhaft", "7T-Zielhistorie fehlt", "unknown" if nutr_fresh == "fehlt" else "caution", freshness=nutr_fresh)
    signal("cut_build_pressure", "Cut-/Aufbau-Druck", "NEUTRAL", "Ohne Verlauf nicht aggressiv deutbar", "Gewicht + Kalorien noch zu dünn", "unknown" if nutr_fresh != "frisch" else "neutral", freshness=nutr_fresh)

    meals = []
    try:
        from nutrition.nutrition_planning_db import get_logging_day_payload
        payload = get_logging_day_payload(day_iso)
        meals = [m for m in (payload.get("planned_meals") or []) if isinstance(m, dict)]
    except Exception:
        meals = []
    meal_times = []
    for meal in meals:
        time_text = meal.get("shifted_time_text") or meal.get("time_text")
        tmin = _parse_hhmm(time_text)
        if tmin is None:
            continue
        macros = meal.get("macros") if isinstance(meal.get("macros"), dict) else {}
        carbs = _float_or_none(macros.get("c") or macros.get("carbs"))
        meal_times.append({"time_min": tmin, "carbs": carbs, "title": meal.get("title") or "Meal"})
    if training_time is None:
        signal("carb_timing", "Carb-Timing", "UNBEKANNT", "Kein Trainingsfenster sichtbar", "Training heute zeitlich offen", "unknown", freshness=nutr_fresh)
    elif not meal_times:
        signal("carb_timing", "Carb-Timing", "UNBEKANNT", "Nicht sauber berechenbar", f"Training {_to_hhmm(training_time)} · keine Meal-Zeitpunkte", "unknown", freshness=nutr_fresh)
    else:
        pre_window = [m for m in meal_times if training_time - 180 <= m["time_min"] <= training_time - 60]
        future_meals = [m for m in meal_times if m["time_min"] > training_time]
        last_meal = max([m for m in meal_times if m["time_min"] <= training_time], key=lambda x: x["time_min"], default=None)
        strict_heavy = bool(re.search(r"\b(push|pull|upper|lower|legs|beine|heavy)\b", label_text))
        if pre_window:
            first = sorted(pre_window, key=lambda x: abs((training_time - 120) - x["time_min"]))[0]
            signal("carb_timing", "Carb-Timing", "GUT", "Carbs passend vor Training", f"Training {_to_hhmm(training_time)} · Meal ca. {_to_hhmm(first['time_min'])}", "push", freshness=nutr_fresh)
        elif last_meal and training_time - last_meal["time_min"] >= (300 if strict_heavy else 360):
            signal("carb_timing", "Carb-Timing", "BEOBACHTEN", "Fueling vor Training offen", f"Training {_to_hhmm(training_time)} · letzte Mahlzeit ca. {_to_hhmm(last_meal['time_min'])}", "caution", freshness=nutr_fresh)
        elif future_meals and training_time > (future_meals[0]["time_min"] - 180):
            signal("carb_timing", "Carb-Timing", "PLANBAR", "Carbs noch planbar vor Training", f"Training {_to_hhmm(training_time)} · Meal geplant ca. {_to_hhmm(future_meals[0]['time_min'])}", "neutral", freshness=nutr_fresh)
        else:
            signal("carb_timing", "Carb-Timing", "UNBEKANNT", "Timing nicht sauber beurteilbar", f"Training {_to_hhmm(training_time)} · keine klare Pre-Meal-Lage", "unknown", freshness=nutr_fresh)

    cal = calendar_interpretation
    school_badge = str(cal.get("school_stress") or "UNBEKANNT").upper()
    school_headline = "Kein harter Schultreiber sichtbar" if school_badge == "NIEDRIG" else "Schultag fordernd, aber planbar" if school_badge == "MODERAT" else "Klausur oder Abgabe sichtbar" if school_badge == "HOCH" else "Schulstress nicht sauber lesbar"
    school_evidence = f"{cal.get('entries_count') or 0} Termine · " + ("keine Klausur/Test/Abgabe erkannt" if school_badge != "HOCH" else "harter Termin im Kalender erkannt")
    signal("school_stress", "Schulstress", school_badge, school_headline, school_evidence, "limit" if school_badge == "HOCH" else "caution" if school_badge == "MODERAT" else "neutral", freshness=fresh.get("calendar", "offen"))
    density_badge = str(cal.get("calendar_density") or "UNBEKANNT").upper()
    signal("calendar_density", "Kalenderdichte", density_badge, "Tag ist planbar" if density_badge in {"FREI", "NORMAL"} else "Tag ist dicht" if density_badge == "DICHT" else "Tag wirkt überladen" if density_badge == "ÜBERLADEN" else "Kalender nicht sicher gelesen", f"{cal.get('entries_count') or 0} Termine · größtes freies Fenster {_fmt_minutes(cal.get('largest_free_window_min'))}", "limit" if density_badge == "ÜBERLADEN" else "caution" if density_badge == "DICHT" else "neutral", freshness=fresh.get("calendar", "offen"))
    tw_badge = str(cal.get("time_window") or "UNBEKANNT").upper()
    signal("time_window_pressure", "Zeitfenster-Druck", tw_badge, "Genug Platz für Training" if tw_badge in {"FREI", "PLANBAR"} else "Trainingsfenster eher knapp" if tw_badge == "KNAPP" else "Trainingsfenster eng" if tw_badge == "ENG" else "Zeitfenster nicht sicher", f"Größtes freies Fenster ca. {_fmt_minutes(cal.get('largest_free_window_min'))}", "limit" if tw_badge == "ENG" else "caution" if tw_badge == "KNAPP" else "neutral", freshness=fresh.get("calendar", "offen"))
    conflict_badge = "KEINER" if cal.get("training_conflict") == "keiner" else "MÖGLICH" if cal.get("training_conflict") == "möglich" else "KLARER KONFLIKT" if cal.get("training_conflict") == "klar" else "UNBEKANNT"
    signal("training_conflict", "Trainingskonflikt", conflict_badge, "Training passt in den Tag" if conflict_badge == "KEINER" else "Konflikt möglich" if conflict_badge == "MÖGLICH" else "Kalender kollidiert mit Training" if conflict_badge == "KLARER KONFLIKT" else "Kalenderlage unklar", "Kalender geprüft · kein Überschneidungskonflikt" if conflict_badge == "KEINER" else "Kalenderlage gegen Trainingsfenster geprüft", "limit" if conflict_badge == "KLARER KONFLIKT" else "caution" if conflict_badge == "MÖGLICH" else "neutral", freshness=fresh.get("calendar", "offen"))
    evening_time = _format_time_window(_parse_hhmm(cal.get("school_end")), None)
    eve_badge = "RUHIG" if cal.get("evening_load") == "ruhig" else "BELEGT" if cal.get("evening_load") == "belegt" else "HOCH" if cal.get("evening_load") == "spät belastet" else "UNBEKANNT"
    signal("evening_load", "Abendlast", eve_badge, "Abends frei" if eve_badge == "RUHIG" else "Abends noch gebunden" if eve_badge == "BELEGT" else "Abend spät belastet" if eve_badge == "HOCH" else "Abendlage unklar", f"Nächster Abendtermin {evening_time or 'nicht sicher'}", "caution" if eve_badge in {"BELEGT", "HOCH"} else "neutral", freshness=fresh.get("calendar", "offen"))
    start_time = _format_time_window(_parse_hhmm(cal.get("school_start")), None)
    rhythm_badge = "NORMAL" if cal.get("early_start_state") == "normal" else "FRÜH" if cal.get("early_start_state") == "früh" else "SEHR FRÜH" if cal.get("early_start_state") == "sehr früh" else "UNBEKANNT"
    signal("daily_rhythm", "Frühstart / Tagesrhythmus", rhythm_badge, "Kein früher Start sichtbar" if rhythm_badge == "NORMAL" else "Früher Start im Kalender" if rhythm_badge == "FRÜH" else "Sehr früher Start sichtbar" if rhythm_badge == "SEHR FRÜH" else "Tagesrhythmus unklar", f"Erster relevanter Termin {start_time or 'nicht sicher'}", "caution" if rhythm_badge in {"FRÜH", "SEHR FRÜH"} else "neutral", freshness=fresh.get("calendar", "offen"))
    alld_badge = str(cal.get("alltagsdruck") or "UNBEKANNT").upper()
    signal("alltagsdruck", "Alltagsdruck", alld_badge, "Alltag wirkt ruhig" if alld_badge == "NIEDRIG" else "Alltag moderat fordernd" if alld_badge == "MODERAT" else "Alltag drückt heute" if alld_badge == "HOCH" else "Alltagsdruck nicht sauber lesbar", "Kalenderdichte + Schultermine kombiniert", "limit" if alld_badge == "HOCH" else "caution" if alld_badge == "MODERAT" else "neutral", freshness=fresh.get("calendar", "offen"))

    current_count = 0
    good_count = 0
    critical_count = 0
    for item in signals:
        freshness = str(item.get("freshness") or "")
        if freshness in {"frisch", "gesichtet", "gestern"}:
            current_count += 1
        if item.get("impact") == "push":
            good_count += 1
        if item.get("impact") == "limit":
            critical_count += 1
    dq_state = "hoch" if current_count >= 7 and data_freshness.get("missing_critical", 0) == 0 else "mittel" if current_count >= 5 else "niedrig"
    signal("data_quality", "Datenqualität", dq_state.upper(), "Datenlage gut" if dq_state == "hoch" else "Datenlage teilweise aktuell" if dq_state == "mittel" else "Wichtige Daten fehlen", f"{current_count} Kernquellen aktuell/gesichtet", "neutral" if dq_state != "niedrig" else "caution", freshness="frisch")
    conflict_level = "hoch" if critical_count >= 4 else "mittel" if critical_count >= 2 else "niedrig"
    signal("signal_conflict", "Signal-Konfliktgrad", conflict_level.upper(), "Signale ziehen sauber zusammen" if conflict_level == "niedrig" else "Einige Signale widersprechen sich" if conflict_level == "mittel" else "Mehrere Signale ziehen gegeneinander", f"{critical_count} limitierende Signale", "caution" if conflict_level != "niedrig" else "neutral", freshness="frisch")
    certainty_score = max(0.0, min(1.0, 0.82 - critical_count * 0.07 - data_freshness.get("missing_critical", 0) * 0.12 - data_freshness.get("stale_count", 0) * 0.03 + good_count * 0.03))
    certainty = "hoch" if certainty_score >= 0.72 else "mittel" if certainty_score >= 0.46 else "niedrig"
    signal("decision_certainty", "Entscheidungssicherheit", certainty.upper(), "Gut erklärbar" if certainty == "hoch" else "Teilweise erklärbar" if certainty == "mittel" else "Noch unsicher", f"{int(round(certainty_score * 100))} % · {critical_count} Konflikte", "neutral", freshness="frisch", confidence="high")
    hard_day = bool(re.search(r"\b(push|pull|upper|lower|legs|beine|heavy)\b", label_text))
    pressure = "hoch" if hard_day else "normal" if str(card.get("type") or "").lower() in {"run", "ergo", "cardio", "gym"} else "niedrig"
    signal("decision_pressure", "Entscheidungsdruck", pressure.upper(), "Heute ist wenig Entscheidungslast drauf" if pressure == "niedrig" else "Normale Tagesentscheidung" if pressure == "normal" else "Heute ist die Entscheidung relevant", "Heutige Einheit als Kontext", "caution" if pressure == "hoch" else "neutral", freshness="frisch")

    visible_signals = [_humanize_signal_copy(item) for item in _finalize_signal_cards(signals)]
    counts = {"push": 0, "neutral": 0, "caution": 0, "limit": 0, "unknown": 0}
    for item in visible_signals:
        counts[str(item.get("impact") or "unknown")] = counts.get(str(item.get("impact") or "unknown"), 0) + 1
    top_signals = sorted([item for item in visible_signals if _top_signal_relevant(item)], key=lambda item: int(item.get("priority") or 0), reverse=True)[:5]
    primary_signals = visible_signals[:12]
    secondary_signals = visible_signals[12:]
    optional_missing = len([item for item in (data_freshness.get("sources") or []) if not item.get("critical") and str(item.get("freshness") or "") in {"fehlt", "gestern", "veraltet"}])
    signal_balance = _build_signal_balance(visible_signals, data_freshness, {"hard_conflicts": counts.get("limit") or 0, "relevant_signals": len(visible_signals), "optional_missing": optional_missing})
    return {
        "signals": visible_signals,
        "primary_signals": primary_signals,
        "secondary_signals": secondary_signals,
        "counts": counts,
        "top_signals": top_signals,
        "certainty_score": certainty_score,
        "checked_sources": len(data_freshness.get("sources") or []),
        "hard_conflicts": int(counts.get("limit") or 0),
        "relevant_signals": len(visible_signals),
        "optional_missing": optional_missing,
        "signal_balance": signal_balance,
    }


def _decision_summary_from_signals(
    *,
    board_state: str,
    board_is_final: bool,
    readiness: dict[str, Any],
    calendar_interpretation: dict[str, Any],
    signal_engine: dict[str, Any],
    card: dict[str, Any],
) -> dict[str, str]:
    if not board_is_final:
        return {
            "headline": "Noch keine finale Entscheidung.",
            "summary": "Die aktuellen Daten deuten eine Richtung an, aber die finale Training-Card soll erst nach dem Morning Check-in gebaut werden.",
            "next_step": "Morning Check-in nachtragen, dann Training sauber finalisieren.",
        }
    limit_count = int((signal_engine.get("counts") or {}).get("limit") or 0)
    caution_count = int((signal_engine.get("counts") or {}).get("caution") or 0)
    if limit_count >= 3:
        return {
            "headline": "Recovery deckelt heute.",
            "summary": "Mehrere Signale bremsen. Nicht aggressiv steigern und die Einheit bewusst kontrolliert halten.",
            "next_step": "Warm-up ehrlich lesen und Druck früh rausnehmen.",
        }
    if calendar_interpretation.get("alltagsdruck") == "hoch" or calendar_interpretation.get("time_window") in {"knapp", "eng"}:
        return {
            "headline": "Körperlich solide, aber Alltag eng.",
            "summary": "Wenn trainiert wird, dann kompakt und ohne Zusatzlast. Zeitfenster und Alltagsdruck sind heute echte Leitplanken.",
            "next_step": "Plan kürzen statt hetzen, falls das Fenster kippt.",
        }
    if str(card.get("type") or "").lower() in {"cardio", "run", "ergo"}:
        return {
            "headline": "Ruhige Ausführung zählt.",
            "summary": "Bei Z2 ist heute Pace oder Watt zweitrangig. Kontrolle und Pulsdeckel tragen die Qualität.",
            "next_step": "Ohne Tempojagd starten und sauber ausrollen.",
        }
    if caution_count <= 4 and limit_count == 0:
        return {
            "headline": "Gute Ausgangslage.",
            "summary": "Recovery, Last und Kalender geben heute genug Spielraum für eine saubere normale Einheit.",
            "next_step": "Progression nur nehmen, wenn das Warm-up stabil wirkt.",
        }
    return {
        "headline": "Gemischte Lage.",
        "summary": "Es gibt genug brauchbare Signale für eine Entscheidung, aber nicht für einen aggressiven Tag. Sauber arbeiten, nicht erzwingen.",
        "next_step": "Kontrolle vor Bonusdruck.",
    }


def _prioritize_signal(item: dict[str, Any]) -> int:
    key = str(item.get("key") or "")
    impact = str(item.get("impact") or "")
    badge = str(item.get("badge") or "")
    score = {"limit": 100, "caution": 70, "unknown": 50, "neutral": 35, "push": 25}.get(impact, 20)
    if badge == "UNPLAUSIBEL":
        score += 40
    if key in {"training_conflict", "school_stress", "time_window_pressure", "carb_timing", "recovery_conflict", "hrv_level", "rhr_level", "data_quality"}:
        score += 20
    if key in {"protein_stability", "stagnation_risk"}:
        score -= 15
    return score


def _humanize_signal_copy(item: dict[str, Any]) -> dict[str, Any]:
    out = dict(item)
    key = str(out.get("key") or "").strip().lower()
    headline = str(out.get("headline") or "").strip()
    evidence = str(out.get("evidence") or "").strip()
    replacements = {
        "calendar_density": ("Kalender ruhig" if "planbar" in headline.lower() else headline, "1 Termin, großes Trainingsfenster frei" if "größtes freies fenster" in evidence.lower() and "1 termine" in evidence.lower() else evidence),
        "decision_certainty": ("Entscheidung gut erklärbar" if "erklärbar" in headline.lower() else headline, "Keine Signal-Konflikte" if "0 konflikte" in evidence.lower() else evidence),
        "protein_stability": ("Proteinverlauf lückenhaft", "Nicht als Tagesargument gewertet" if "zielhistorie fehlt" in evidence.lower() else evidence),
        "carb_availability": ("Carbs heute nicht sauber sichtbar", "Nicht als Tagesargument gewertet" if "makros nicht sichtbar" in headline.lower() else evidence),
    }
    if key in replacements:
        new_headline, new_evidence = replacements[key]
        out["headline"] = new_headline
        out["evidence"] = new_evidence
    return out


def _signal_domain(key: str) -> str:
    key = str(key or "").strip().lower()
    if key in {"hrv_level", "hrv_trend", "rhr_level", "rhr_trend", "recovery_conflict", "sleep_quality", "sleep_debt", "systemic_fatigue", "energy_level", "motivation_level", "pain_signal"}:
        return "recovery"
    if key in {"acute_load_72h", "weekly_load", "heavy_set_density", "target_muscle_readiness", "performance_trend", "progression_pressure", "stagnation_risk", "execution_risk", "run_impact", "ergo_load", "cardio_load_7d", "z2_stability", "pulse_drift", "power_at_cap"} or key.startswith("muscle_"):
        return "training"
    if key in {"weight_trend", "weight_level", "calorie_balance", "carb_availability", "protein_stability", "cut_build_pressure", "carb_timing"}:
        return "nutrition"
    if key in {"school_stress", "calendar_density", "time_window_pressure", "training_conflict", "evening_load", "daily_rhythm", "alltagsdruck"}:
        return "calendar"
    return "data"


def _signal_tone(item: dict[str, Any]) -> str:
    impact = str(item.get("impact") or "").strip().lower()
    freshness = str(item.get("freshness") or "").strip().lower()
    if impact == "limit":
        return "red"
    if impact == "caution":
        return "yellow"
    if impact == "push":
        return "green"
    if impact == "unknown" or freshness in {"fehlt", "offen"}:
        return "gray"
    return "yellow" if freshness in {"gestern", "veraltet"} else "green"


def _build_signal_balance(signals: list[dict[str, Any]], data_freshness: dict[str, Any], signal_engine: dict[str, Any]) -> dict[str, Any]:
    domains = [
        ("recovery", "Recovery"),
        ("training", "Training"),
        ("nutrition", "Ernährung"),
        ("calendar", "Kalender"),
    ]
    zones: list[dict[str, Any]] = []
    for key, label in domains:
        domain_items = [item for item in signals if _signal_domain(item.get("key") or "") == key]
        tones = {"green": 0, "yellow": 0, "red": 0, "gray": 0}
        for item in domain_items:
            tone = _signal_tone(item)
            tones[tone] += 1
        if tones["red"] > 0:
            tone = "red"
        elif tones["yellow"] > 0:
            tone = "yellow"
        elif tones["green"] > 0:
            tone = "green"
        else:
            tone = "gray"
        status_map = {
            "recovery": {"green": "stabil", "yellow": "beobachten", "red": "gedrückt", "gray": "dünn"},
            "training": {"green": "planbar", "yellow": "dosieren", "red": "bremsen", "gray": "offen"},
            "nutrition": {"green": "gedeckt", "yellow": "offen", "red": "limitiert", "gray": "dünn"},
            "calendar": {"green": "frei", "yellow": "eng", "red": "blockiert", "gray": "unklar"},
        }
        zones.append({"key": key, "label": label, "status": status_map[key][tone], "tone": tone, "tones": tones})
    hard_conflicts = int(signal_engine.get("hard_conflicts") or 0)
    relevant_signals = int(signal_engine.get("relevant_signals") or len(signals))
    optional_missing = int(signal_engine.get("optional_missing") or 0)
    summary_line = "Recovery, Training, Ernährung und Kalender ziehen heute überwiegend sauber zusammen."
    if hard_conflicts:
        summary_line = "Einige Bereiche bremsen heute sichtbar. CORE hält die Entscheidung deshalb kontrolliert."
    elif optional_missing:
        summary_line = "Alte oder fehlende Werte wurden nicht als heutige Argumente genutzt."
    meta_line = f"{hard_conflicts} harte Konflikte · {relevant_signals} starke Signale · {optional_missing} Werte nicht gewertet"
    data_tone = "hoch" if data_freshness.get("missing_critical", 0) == 0 and data_freshness.get("stale_count", 0) <= 1 else "mittel" if data_freshness.get("missing_critical", 0) <= 2 else "niedrig"
    checked_sources = len(data_freshness.get("sources") or [])
    return {"title": "Signalbilanz", "summary": summary_line, "meta": f"Datenlage: {data_tone} · {checked_sources} Quellen · {hard_conflicts} Konflikte", "zones": zones}


def _top_signal_relevant(item: dict[str, Any]) -> bool:
    key = str(item.get("key") or "").strip().lower()
    impact = str(item.get("impact") or "").strip().lower()
    headline = str(item.get("headline") or "").strip().lower()
    evidence = str(item.get("evidence") or "").strip().lower()
    if "nicht als tagesargument gewertet" in evidence or "nicht gewertet" in evidence:
        return False
    if key in {"protein_stability", "calorie_balance", "cut_build_pressure", "stagnation_risk"} and ("nicht" in headline or "lückenhaft" in headline or "fehlt" in evidence):
        return False
    if impact == "unknown":
        return False
    return True


def _signal_is_optional_unknown(item: dict[str, Any]) -> bool:
    key = str(item.get("key") or "").strip().lower()
    badge = str(item.get("badge") or "").strip().upper()
    headline = str(item.get("headline") or "").strip().lower()
    impact = str(item.get("impact") or "").strip().lower()
    optional_keys = {
        "motivation_level",
        "energy_level",
        "pain_signal",
        "sleep_debt",
        "performance_trend",
        "z2_stability",
        "pulse_drift",
        "power_at_cap",
        "protein_stability",
        "calorie_balance",
        "carb_availability",
        "cut_build_pressure",
        "carb_timing",
    }
    if key not in optional_keys:
        return False
    if impact == "unknown":
        return True
    return badge in {"UNBEKANNT", "FEHLT"} or "nicht sauber" in headline or "nicht vergleich" in headline or "offen" == headline


def _signal_is_visible(item: dict[str, Any]) -> bool:
    key = str(item.get("key") or "").strip().lower()
    freshness = str(item.get("freshness") or "").strip().lower()
    impact = str(item.get("impact") or "").strip().lower()
    badge = str(item.get("badge") or "").strip().upper()
    blocker_keys = {"data_quality", "signal_conflict", "decision_certainty", "training_conflict", "recovery_conflict"}
    subjective_keys = {"motivation_level", "energy_level", "pain_signal"}
    if key in subjective_keys and freshness != "frisch":
        return False
    if _signal_is_optional_unknown(item):
        return False
    if freshness in {"gestern", "veraltet"} and impact not in {"limit"}:
        return False
    if badge in {"UNBEKANNT", "FEHLT"} and key not in blocker_keys:
        return False
    if impact == "unknown" and key not in blocker_keys:
        return False
    return True


def _finalize_signal_cards(signals: list[dict[str, Any]]) -> list[dict[str, Any]]:
    visible: list[dict[str, Any]] = []
    for item in signals:
        badge = str(item.get("badge") or str(item.get("state") or "")).strip().upper()
        domain = ""
        key = str(item.get("key") or "")
        if key in {"data_quality", "decision_certainty"}:
            domain = "datenqualität"
        optional = key in {"protein_stability", "z2_stability", "pulse_drift", "power_at_cap", "pain_signal"}
        item["badge"] = badge
        item["badge_tone"] = _status_context_tone(badge, domain=domain, optional=optional)
        if item.get("headline") == item.get("evidence"):
            item["evidence"] = item.get("freshness") or ""
        item["priority"] = _prioritize_signal(item)
        item["visible"] = _signal_is_visible(item)
        if item["visible"]:
            visible.append(item)
    return visible


def _fallback_reasons(data_status: dict[str, Any], training_card: dict[str, Any]) -> list[str]:
    reasons = []
    recovery = ((data_status.get("domains") or {}).get("recovery") or {})
    weight = ((data_status.get("domains") or {}).get("weight") or {})
    nutrition = ((data_status.get("domains") or {}).get("nutrition") or {})
    card_type = str(training_card.get("type") or "").lower()
    if card_type == "cardio":
        reasons.append("Heute steht ruhige Ausdauerarbeit im Plan, deshalb zählt Kontrolle mehr als Tempo.")
    elif training_card.get("title"):
        reasons.append(f"Heute ist {training_card.get('title')} geplant, deshalb bleibt die Entscheidung nah an der Struktur des Tages.")
    if recovery.get("status") == "fresh":
        reasons.append("Die Daten sind frisch genug für eine klare Einschätzung.")
    elif recovery.get("status") == "missing":
        reasons.append("Ein Teil der Recovery-Sicht fehlt noch, deshalb bleibt CORE heute bewusst zurückhaltend.")
    if nutrition.get("status") in {"partial", "missing", "stale"}:
        reasons.append("Energie und Ernährung sind noch nicht komplett abgesichert, also kein zusätzlicher Druck.")
    elif weight.get("status") in {"missing", "stale"}:
        reasons.append("Ein Teil der Verlaufsdaten ist nicht ganz frisch, deshalb heute lieber sauber als aggressiv.")
    if training_card.get("fallback") and not reasons:
        reasons.append("Die Einheit wird heute bewusst simpel und sauber aus dem Plan geführt.")
    return _list_text(reasons, 4)


def _intent_from_card(training_card: dict[str, Any]) -> str:
    if training_card.get("type") == "rest":
        return "recover"
    return "train_controlled"


def _headline_from_intent(intent: str, training_card: dict[str, Any]) -> str:
    if intent == "recover":
        return "Heute ruhig halten."
    if str(training_card.get("type") or "").lower() == "cardio":
        return "Z2 ruhig durchziehen, danach Mobility."
    if intent == "train_normal":
        return f"{training_card.get('title') or 'Training'} normal ausführen."
    return f"Heute locker bleiben: {training_card.get('title') or 'Training'} sauber ausführen."


def _summary_from_status(training_card: dict[str, Any], data_status: dict[str, Any]) -> str:
    if training_card.get("type") == "rest":
        return "Für heute steht keine harte Einheit im Vordergrund. Halte Bewegung niedrigschwellig und sammle Daten für die nächste Entscheidung."
    if training_card.get("type") == "cardio":
        return "Die Daten sind frisch genug, CORE muss heute nicht raten. Heute geht es nicht um Druck, sondern um saubere Ausdauerarbeit. Halte den Pulsdeckel ein und lass das Tempo bewusst zweitrangig bleiben."
    if data_status.get("status") == "ok":
        return "Die Daten sind frisch genug, CORE muss heute nicht raten. Heute geht es nicht um Druck, sondern um saubere Ausführung und stabile Entscheidungen. Wenn sich das Warm-up gut anfühlt, reicht es, den Plan sauber durchzuziehen."
    return "Die Datenlage ist noch nicht komplett sauber abgesichert. Nimm die geplante Einheit mit, aber ohne erzwungene Progression und ohne unnötigen Druck."


def _display_headline(board: dict[str, Any], card: dict[str, Any]) -> str:
    raw = _text(board.get("human_headline"), 160) or ""
    text = raw.lower()
    card_type = str(card.get("type") or "").lower()
    if raw and "kontrolliert ausführen" not in text and "normal ausführen" not in text:
        return raw
    if card_type in {"cardio", "run", "ergo"}:
        return "Z2 ruhig durchziehen, danach Mobility."
    if card_type == "rest":
        return "Heute locker bleiben."
    title = _text(card.get("title"), 120) or "Training"
    return f"Heute sauber bleiben: {title} aus dem Plan ziehen, aber ohne Extra-Druck."




def _display_reasons(board: dict[str, Any], card: dict[str, Any], data_status: dict[str, Any]) -> list[str]:
    reasons = board.get("visible_reasons") if isinstance(board.get("visible_reasons"), list) else []
    mapped: list[str] = []
    for reason in reasons:
        text = _text(reason, 220) or ""
        lower = text.lower()
        if not text:
            continue
        if "recovery-daten sind aktuell" in lower:
            mapped.append("Die Daten sind frisch genug für eine klare Einschätzung.")
        elif "gewicht-check-in ist aktuell" in lower:
            mapped.append("Gewicht ist aktuell und verändert die Entscheidung heute nicht.")
        elif "training-card nutzt phase-1-fallbacks" in lower:
            mapped.append("Die Einheit wird heute bewusst simpel und sauber aus dem Plan geführt.")
        elif lower.startswith("heute im plan:"):
            mapped.append(_fallback_reasons(data_status, card)[0])
        else:
            mapped.append(text)
    return _list_text(mapped, 4) or _fallback_reasons(data_status, card)


def _quick_stats(data_status: dict[str, Any], training_card: dict[str, Any]) -> list[dict[str, Any]]:
    domains = data_status.get("domains") if isinstance(data_status.get("domains"), dict) else {}
    weight = domains.get("weight") if isinstance(domains.get("weight"), dict) else {}
    recovery = domains.get("recovery") if isinstance(domains.get("recovery"), dict) else {}
    nutrition = domains.get("nutrition") if isinstance(domains.get("nutrition"), dict) else {}
    recovery_status = _status_label(recovery.get("status"))
    return [
        {
            "label": "Gewicht",
            "value": f"{weight.get('latest_weight'):.1f}" if _float_or_none(weight.get("latest_weight")) is not None else "—",
            "unit": "kg",
            "meta": "letzter Check-in" if _float_or_none(weight.get("latest_weight")) is not None else "noch nicht sicher",
            "status": weight.get("status") or "missing",
            "accent": "blue",
        },
        {
            "label": "HRV",
            "value": f"{recovery.get('latest_rmssd'):.0f}" if _float_or_none(recovery.get("latest_rmssd")) is not None else "—",
            "unit": "ms",
            "meta": f"letzte Messung {recovery.get('latest_date') or 'offen'}",
            "status": recovery.get("status") or "missing",
            "accent": "green",
        },
        {
            "label": "Resting HR",
            "value": f"{recovery.get('latest_hr'):.0f}" if _float_or_none(recovery.get("latest_hr")) is not None else "—",
            "unit": "bpm",
            "meta": f"letzte Messung {recovery.get('latest_date') or 'offen'}",
            "status": recovery.get("status") or "missing",
            "accent": "orange",
        },
        {
            "label": "Recovery",
            "value": _status_label(recovery.get("status") or "missing"),
            "unit": "",
            "meta": "HRV und RHR als Basis",
            "status": recovery.get("status") or "missing",
            "accent": "amber",
        },
        {
            "label": "Nutrition",
            "value": _status_label(nutrition.get("status") or "missing"),
            "unit": "",
            "meta": f"Logs {int(nutrition.get('log_count') or 0)}",
            "status": nutrition.get("status") or "missing",
            "accent": "violet",
        },
    ]






def _considered_signals(data_status: dict[str, Any], card: dict[str, Any]) -> list[dict[str, Any]]:
    domains = data_status.get("domains") if isinstance(data_status.get("domains"), dict) else {}
    training_status = "fresh" if str(card.get("type") or "").lower() in {"gym", "cardio", "run"} else "partial"
    items = [
        {"label": "Recovery", "status": domains.get("recovery", {}).get("status"), "detail": "HRV und Resting HR"},
        {"label": "Nutrition", "status": domains.get("nutrition", {}).get("status"), "detail": "Energie und Tagesziel"},
        {"label": "Alltagsstress / Kalender", "status": "partial", "detail": "noch nicht vollständig"},
        {"label": "Schlaf", "status": domains.get("recovery", {}).get("status"), "detail": "indirekt über Recovery"},
        {"label": "Training-Fitness", "status": training_status, "detail": "Plan und Verlauf"},
    ]
    out = []
    for item in items:
        status = item.get("status") or "missing"
        out.append(
            {
                "label": item["label"],
                "status": _status_label(status),
                "meter_pct": _status_meter(status),
                "detail": item["detail"],
            }
        )
    return out




def _reason_cards(reasons: list[str]) -> list[dict[str, str]]:
    cards: list[dict[str, str]] = []
    for reason in reasons[:3]:
        lower = str(reason or "").strip().lower()
        if not lower:
            continue
        if "frisch genug" in lower or "klare einschätzung" in lower:
            cards.append(
                {
                    "title": "Die Daten sind frisch.",
                    "text": "HRV, Gewicht und Recovery reichen für die Entscheidung. CORE muss heute nicht raten.",
                }
            )
        elif "z2" in lower or "ruhe" in lower or "sauber" in lower or "kontrolle" in lower:
            cards.append(
                {
                    "title": "Heute zählt Kontrolle.",
                    "text": "Die geplante Einheit lebt von ruhiger Ausführung. Tempo oder Bonusdruck bringen heute keinen Zusatznutzen.",
                }
            )
        elif "gewicht" in lower or "energie" in lower or "ernährung" in lower:
            cards.append(
                {
                    "title": "Energie ist kein Freifahrtschein.",
                    "text": "Energie ist kein Stop, aber auch kein Grund für Zusatzdruck. Heute reicht kontrolliert gut vollkommen aus.",
                }
            )
        else:
            cards.append({"title": "Was CORE gesehen hat", "text": reason})
    return cards












def _row_to_board(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    data = dict(row)
    board = {
        "id": data.get("id"),
        "date": data.get("day_iso"),
        "day_iso": data.get("day_iso"),
        "created_at": data.get("created_at"),
        "updated_at": data.get("updated_at"),
        "source": data.get("source"),
        "trigger": data.get("trigger"),
        "source_message": data.get("source_message"),
        "human_headline": data.get("human_headline"),
        "human_summary": data.get("human_summary"),
        "decision_intent": data.get("decision_intent"),
        "confidence": _confidence(data.get("confidence"), 0.0),
        "visible_reasons": _json_loads(data.get("visible_reasons_json"), []),
        "training_card": _json_loads(data.get("training_card_json"), {}),
        "data_status": _json_loads(data.get("data_status_json"), {}),
        "legacy_core": _json_loads(data.get("legacy_core_json"), {}),
        "machine_briefing": _json_loads(data.get("machine_briefing_json"), {}),
        "status": data.get("status"),
        "user_feedback": data.get("user_feedback"),
        "user_feedback_at": data.get("user_feedback_at"),
    }
    machine = board.get("machine_briefing") if isinstance(board.get("machine_briefing"), dict) else {}
    if isinstance(machine.get("recovery_summary"), dict):
        board["recovery_summary"] = machine.get("recovery_summary")
    return enrich_core_board(board)


def build_core_daily_board(
    day_iso: str | None = None,
    *,
    source: str = "fallback",
    trigger: str | None = None,
    source_message: str | None = None,
    human_headline: str | None = None,
    human_summary: str | None = None,
    decision_intent: str | None = None,
    confidence: Any = None,
    machine_briefing: Any = None,
    visible_reasons: Any = None,
    training_card: Any = None,
    data_status: Any = None,
    legacy_core: Any = None,
) -> dict[str, Any]:
    day = _parse_day(day_iso)
    machine = _json_loads(machine_briefing, {}) if machine_briefing not in (None, "") else {}
    if not isinstance(machine, dict):
        machine = {"raw": machine_briefing}
    endurance = build_endurance_core_context(day, refresh=True)
    machine["endurance_approval"] = endurance

    legacy = legacy_core if isinstance(legacy_core, dict) else _legacy_core_payload(day)
    effective_legacy = _effective_legacy_core(day, legacy)
    legacy_status = _legacy_core_status(day, legacy)
    if legacy_status.get("stale"):
        machine["legacy_core_suppressed"] = legacy_status
    plan_day = _plan_day(day)
    fallback_card = _training_card(day, plan_day, effective_legacy)
    card = training_card if isinstance(training_card, dict) and training_card else machine.get("training_card")
    if not isinstance(card, dict) or not card:
        card = fallback_card
    elif plan_day.get("available"):
        if _card_conflicts_with_authoritative(card, fallback_card):
            machine["supplied_training_card_rejected"] = {
                "reason": "conflicts_with_authoritative_session",
                "supplied_title": card.get("title"),
                "authoritative_title": fallback_card.get("title"),
            }
            card = fallback_card
    machine["authoritative_training_session"] = {
        "available": bool(plan_day.get("available")),
        "label": fallback_card.get("title"),
        "session_type": fallback_card.get("type"),
        "source": plan_day.get("source"),
        "session_key": plan_day.get("session_key"),
    }
    recovery_summary = _shared_recovery_summary_payload()
    if recovery_summary:
        machine["recovery_summary"] = recovery_summary
    status = data_status if isinstance(data_status, dict) and data_status else machine.get("data_status")
    if not isinstance(status, dict) or not status:
        status = _data_status(day, plan_day)

    reasons = _list_text(visible_reasons, 5) or _list_text(machine.get("visible_reasons"), 5)
    if endurance.get("has_workout"):
        endurance_status = str(endurance.get("status") or ((endurance.get("liva") or {}).get("status")) or "UNKNOWN").upper()
        final_summary = str(endurance.get("final_summary") or ((endurance.get("liva") or {}).get("final_summary")) or "").strip()
        reason = str(endurance.get("reason") or ((endurance.get("liva") or {}).get("reason")) or "").strip()
        if endurance_status == "GREEN" and final_summary:
            reasons = [f"Ausdauer: {final_summary} freigegeben.", *reasons]
        elif endurance_status == "YELLOW" and final_summary:
            reasons = [f"Ausdauer nur angepasst: {final_summary}.", *reasons]
        elif endurance_status == "RED":
            reasons = ["Ausdauer heute nicht sinnvoll: verschieben oder stark kürzen.", *reasons]
        elif reason:
            reasons = [f"Ausdauer: {reason}", *reasons]
        reasons = _list_text(reasons, 5)
    if not reasons:
        reasons = _fallback_reasons(status, card)
    intent = _text(decision_intent or machine.get("decision_intent"), 80) or _intent_from_card(card)
    headline = _text(human_headline or machine.get("human_headline"), 160) or _headline_from_intent(intent, card)
    summary = _text(human_summary or machine.get("human_summary"), 650) or _summary_from_status(card, status)
    recovery_flags = recovery_summary.get("flags") if isinstance(recovery_summary.get("flags"), dict) else {}
    recovery_status = str(recovery_summary.get("status") or "").strip().lower()
    if recovery_flags.get("sickness") or recovery_status == "sick":
        intent = "recovery_only"
        headline = "Krankheitsflag aktiv: Training nur sehr leicht oder verschieben."
        summary = "Recovery-Signale zeigen heute Krankheit/Infekt-Kontext. Kein normales Training oder Progressionsdenken; höchstens sehr leichte Bewegung, wenn sie sich klar gut anfühlt."
        reasons = _list_text(["Krankheitsflag aktiv.", "Recovery-/Polar-Daten sprechen heute gegen normales Training.", *reasons], 5)
        card["decision_intent"] = "recovery_only"
        card["title"] = card.get("title") or "Recovery"
        card["summary"] = "Recovery only: keine normale Progression, nur sehr leichte Bewegung oder verschieben."
    elif recovery_flags.get("alcohol") or recovery_status == "flagged":
        if intent == "train_controlled":
            intent = "reduce_volume"
        headline = "Recovery-Flag aktiv: Training heute konservativ halten."
        summary = "Recovery-Signale zeigen heute einen Flag-Kontext. Volumen und Intensität konservativ halten, keine aggressive Progression."
        reasons = _list_text(["Recovery-Flag aktiv.", "Recovery-/Polar-Daten sprechen heute für Vorsicht.", *reasons], 5)
    conf = _confidence(confidence if confidence is not None else machine.get("confidence"), 0.62 if status.get("status") == "ok" else 0.54)
    now = _utc_iso()

    conn = get_core_db()
    try:
        ensure_core_daily_boards_schema(conn)
        conn.execute(
            """
            INSERT INTO core_daily_boards (
                day_iso, created_at, updated_at, source, trigger, source_message,
                human_headline, human_summary, decision_intent, confidence,
                machine_briefing_json, visible_reasons_json, training_card_json,
                data_status_json, legacy_core_json, status
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active')
            ON CONFLICT(day_iso) DO UPDATE SET
                updated_at=excluded.updated_at,
                source=excluded.source,
                trigger=excluded.trigger,
                source_message=excluded.source_message,
                human_headline=excluded.human_headline,
                human_summary=excluded.human_summary,
                decision_intent=excluded.decision_intent,
                confidence=excluded.confidence,
                machine_briefing_json=excluded.machine_briefing_json,
                visible_reasons_json=excluded.visible_reasons_json,
                training_card_json=excluded.training_card_json,
                data_status_json=excluded.data_status_json,
                legacy_core_json=excluded.legacy_core_json,
                status='active'
            """,
            (
                day,
                now,
                now,
                _text(source, 80) or "fallback",
                _text(trigger, 120),
                _text(source_message, 8000),
                headline,
                summary,
                intent,
                conf,
                _json_dumps(machine, {}),
                _json_dumps(reasons, []),
                _json_dumps(card, {}),
                _json_dumps(status, {}),
                _json_dumps(effective_legacy, {}),
            ),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM core_daily_boards WHERE day_iso=? LIMIT 1", (day,)).fetchone()
        return _row_to_board(row)
    finally:
        conn.close()


def get_core_daily_board(day_iso: str | None = None, *, create_if_missing: bool = True) -> dict[str, Any] | None:
    return get_persisted_core_daily_board(day_iso, create_if_missing=create_if_missing)


def get_persisted_core_daily_board(day_iso: str | None = None, *, create_if_missing: bool = False) -> dict[str, Any] | None:
    day = _parse_day(day_iso)
    conn = get_core_db()
    try:
        ensure_core_daily_boards_schema(conn)
        row = conn.execute(
            "SELECT * FROM core_daily_boards WHERE day_iso=? AND status='active' ORDER BY updated_at DESC, id DESC LIMIT 1",
            (day,),
        ).fetchone()
        if row:
            board = _row_to_board(row)
            try:
                from core.core_training_card import get_persisted_core_training_card

                persisted_training_card = get_persisted_core_training_card(day)
            except Exception:
                persisted_training_card = None
            candidate_card = persisted_training_card if isinstance(persisted_training_card, dict) and persisted_training_card else (board.get("training_card") if isinstance(board.get("training_card"), dict) else {})
            if candidate_card:
                authoritative_plan_day = _plan_day(day)
                authoritative_card = _training_card(day, authoritative_plan_day, {})
                if authoritative_plan_day.get("available") and _card_conflicts_with_authoritative(candidate_card, authoritative_card):
                    board["training_card"] = authoritative_card
                    machine = board.get("machine_briefing") if isinstance(board.get("machine_briefing"), dict) else {}
                    board["machine_briefing"] = {
                        **machine,
                        "persisted_training_card_suppressed": {
                            "reason": "conflicts_with_authoritative_session",
                            "persisted_title": candidate_card.get("title"),
                            "authoritative_title": authoritative_card.get("title"),
                        },
                    }
                    board = enrich_core_board(board, training_card=authoritative_card)
                else:
                    board["training_card"] = candidate_card
                    board = enrich_core_board(board, training_card=candidate_card)
            return board
    finally:
        conn.close()
    if create_if_missing:
        return build_core_daily_board(day, source="phase1_fallback", trigger="read_missing_board")
    return None


def core_board_view_model(day_iso: str | None = None) -> dict[str, Any]:
    board = get_core_daily_board(day_iso, create_if_missing=True) or {}
    board_day = board.get("date") or _parse_day(day_iso)
    readiness = morning_readiness(board_day)
    morning_checkin_done_today = bool(((readiness.get("signals") or {}).get("subjective") or {}).get("present"))
    training_card_row = _latest_core_training_card_row(board_day)
    training_card_status = {"ready": False, "title": None}
    try:
        from core.core_training_card import get_core_training_card

        persisted_training_card = get_core_training_card(board_day)
        if isinstance(persisted_training_card, dict) and persisted_training_card:
            training_card_status = {
                "ready": True,
                "title": persisted_training_card.get("title"),
                "session_type": persisted_training_card.get("session_type"),
            }
    except Exception:
        persisted_training_card = None
    board = enrich_core_board(board, training_card=persisted_training_card if isinstance(persisted_training_card, dict) else None)
    confidence_value = _confidence(board.get("confidence"), 0.0)
    data_status = board.get("data_status") if isinstance(board.get("data_status"), dict) else {}
    card = persisted_training_card if isinstance(persisted_training_card, dict) and persisted_training_card else (board.get("training_card") if isinstance(board.get("training_card"), dict) else {})
    plan_day = _plan_day(board_day)
    calendar_interpretation = _build_calendar_interpretation(board_day)
    data_freshness = calculate_data_freshness(
        board_day,
        plan_day=plan_day,
        readiness=readiness,
        calendar_interpretation=calendar_interpretation,
        training_card_row=training_card_row,
    )
    board_is_final = morning_checkin_done_today
    board_state = "final" if board_is_final else ("stale" if data_freshness.get("missing_critical", 0) >= 3 else "precheck")
    signal_engine = build_core_signals(
        board_day,
        board=board,
        card=card,
        readiness=readiness,
        data_freshness=data_freshness,
        calendar_interpretation=calendar_interpretation,
    )
    decision_summary = _decision_summary_from_signals(
        board_state=board_state,
        board_is_final=board_is_final,
        readiness=readiness,
        calendar_interpretation=calendar_interpretation,
        signal_engine=signal_engine,
        card=card,
    )
    reasons = _display_reasons(board, card, data_status)
    consequence = {
        "title": "Heute bedeutet das" if board_is_final else "Vorläufige Lage",
        "headline": decision_summary["headline"],
        "lines": [decision_summary["summary"], decision_summary["next_step"]],
    }
    freshness_counts = data_freshness.get("counts") or {}
    status_line_parts = ["Final" if board_is_final else "Vorläufig"]
    if not board_is_final:
        status_line_parts.append("Morning Check-in fehlt")
    elif data_freshness.get("missing_critical", 0) >= 2:
        status_line_parts.append("wichtige Daten fehlen")
    elif data_freshness.get("stale_count", 0):
        status_line_parts.append(f"{data_freshness.get('stale_count')} Quellen von gestern/älter")
    else:
        status_line_parts.append("Datenlage gut")
    consistency = board.get("board_consistency") if isinstance(board.get("board_consistency"), dict) else {}
    legacy_status = board.get("legacy_core_status") if isinstance(board.get("legacy_core_status"), dict) else {}
    memory_summary = summarize_memory_layers(board_day)
    memory_counts = memory_summary.get("counts") if isinstance(memory_summary, dict) else {}
    return {
        "ok": bool(board),
        "date": board_day,
        "generated_at": board.get("updated_at") or _utc_iso(),
        "board": board,
        "ui": {
            "headline": "Vorläufiges CORE Board" if not board_is_final else _display_headline(board, card),
            "summary": "Morning Check-in fehlt noch – CORE zeigt nur die bisherige Datenlage." if not board_is_final else decision_summary["summary"],
            "intent": board.get("decision_intent") or ("data_check" if not board_is_final else "train_controlled"),
            "intent_label": {
                "train_controlled": "ruhig ausführen",
                "train_normal": "sauber durchziehen",
                "recover": "Recovery priorisieren",
                "data_check": "Daten prüfen",
            }.get(str(board.get("decision_intent") or ("data_check" if not board_is_final else "")), "Tagesentscheidung"),
            "confidence_pct": int(round((signal_engine.get("certainty_score") or confidence_value) * 100)),
            "visible_reasons": reasons,
            "reason_cards": _reason_cards(reasons),
            "training_card": card,
            "endurance_approval": board.get("machine_briefing", {}).get("endurance_approval") if isinstance(board.get("machine_briefing"), dict) else {},
            "data_status": data_status,
            "data_chips": data_freshness.get("sources"),
            "quick_stats": _quick_stats(data_status, card),
            "board_status": " · ".join(status_line_parts),
            "core_consequence_title": consequence["title"],
            "core_consequence_text": consequence["headline"],
            "core_consequence_lines": consequence["lines"],
            "next_step_text": decision_summary["next_step"],
            "training_card_ready": bool(training_card_status.get("ready")),
            "training_card_status_text": (
                f"Training-Card bereit: {training_card_status.get('title')}"
                if training_card_status.get("ready")
                else "Training-Card noch nicht gebaut."
            ),
            "memory_summary": {
                "today": memory_counts.get("today", {}),
                "week": memory_counts.get("week", {}),
                "phase": memory_counts.get("phase", {}),
                "year": memory_counts.get("year", {}),
                "global": memory_counts.get("global", {}),
            },
            "memory_summary_lines": [
                f"Memory: Heute {(memory_counts.get('today', {}) or {}).get('total', 0)} · Pending {memory_summary.get('headline', {}).get('pending_total', 0)} · Drafts {memory_summary.get('headline', {}).get('draft_total', 0)} · Patches {memory_summary.get('headline', {}).get('patch_total', 0)}",
                (
                    f"{memory_summary.get('headline', {}).get('today_pending', 0)} neue Kandidaten warten auf Review."
                    if memory_summary.get("headline", {}).get("today_pending", 0)
                    else "Heute keine neuen Pending-Kandidaten."
                ),
            ],
            "memory_inbox_url": (os.getenv("LIVA_MEMORY_URL") or "").strip() or None,
            "training_card_title": training_card_status.get("title"),
            "board_consistency": consistency,
            "board_consistency_hint": (
                f"Board-Text passt nicht ganz zur heutigen Einheit. Training-Card nutzt den Plan: {consistency.get('recommended_session_label') or consistency.get('planned_session_label') or card.get('title') or 'Training'}."
                if consistency.get("status") == "mismatch"
                else None
            ),
            "legacy_core_status": legacy_status,
            "legacy_core_debug_note": (
                f"Legacy CORE stammt von {legacy_status.get('legacy_day_iso')} und wurde nicht als heutige Entscheidung genutzt."
                if legacy_status.get("stale") and legacy_status.get("legacy_day_iso")
                else None
            ),
            "considered_signals": _considered_signals(data_status, card),
            "today_signal_items": signal_engine.get("signals"),
            "today_signal_primary": signal_engine.get("primary_signals") or [],
            "today_signal_secondary": signal_engine.get("secondary_signals") or [],
            "today_signal_summary": {
                "total": len(signal_engine.get("signals") or []),
                "counts": signal_engine.get("counts") or {},
                "checked_sources": int(signal_engine.get("checked_sources") or 0),
                "hard_conflicts": int(signal_engine.get("hard_conflicts") or 0),
                "relevant_signals": int(signal_engine.get("relevant_signals") or 0),
                "explainable_pct": int(round((signal_engine.get("certainty_score") or 0.0) * 100)),
                "optional_missing": int(signal_engine.get("optional_missing") or 0),
                "line": (
                    "Die heutigen Signale ziehen sauber zusammen. Alte oder fehlende Werte werden nicht als heutige Argumente genutzt."
                    if int(signal_engine.get("optional_missing") or 0) <= 0
                    else f"{int(signal_engine.get('optional_missing') or 0)} optionale Werte fehlen heute und wurden nicht gewertet."
                ),
            },
            "today_signal_top": signal_engine.get("top_signals"),
            "signal_balance": signal_engine.get("signal_balance") or {},
            "data_status_items": data_freshness.get("sources"),
            "technical_driver_cards": [],
            "tomorrow_glance": "",
            "load_profile_line": "",
            "debug_available": False,
            "machine_briefing": board.get("machine_briefing") if isinstance(board.get("machine_briefing"), dict) else {},
            "board_state": board_state,
            "board_is_final": board_is_final,
            "morning_checkin_done_today": morning_checkin_done_today,
            "calendar_interpretation": calendar_interpretation,
            "data_freshness": data_freshness,
            "decision_summary": decision_summary,
            "freshness_counts": freshness_counts,
        },
    }
