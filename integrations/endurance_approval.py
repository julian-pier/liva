from __future__ import annotations

import json
import math
import re
import sqlite3
from datetime import date, datetime, timedelta, timezone
from typing import Any

from database.connections import get_core_db, get_hrv_db, get_nutrition_db, get_plans_db, get_training_db
from integrations.intervals_client import IntervalsClient, IntervalsClientError, IntervalsNotConfiguredError

DAY_ORDER = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]
STATUS_COLORS = {
    "GREEN": "green",
    "YELLOW": "yellow",
    "RED": "red",
    "UNKNOWN": "unknown",
}


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_json(raw: Any, fallback: Any) -> Any:
    if raw in (None, ""):
        return fallback
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(str(raw))
    except Exception:
        return fallback


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    except Exception:
        return set()


def _safe_text(value: Any) -> str:
    return str(value or "").strip()


def _safe_lower(value: Any) -> str:
    return _safe_text(value).lower()


def _safe_float(value: Any) -> float | None:
    try:
        if value in (None, ""):
            return None
        val = float(value)
        return val if math.isfinite(val) else None
    except Exception:
        return None


def _extract_date(raw: Any) -> str | None:
    text = _safe_text(raw)
    if not text:
        return None
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return text
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        return datetime.fromisoformat(text).date().isoformat()
    except Exception:
        return text[:10] if len(text) >= 10 else None


def _extract_datetime(raw: Any) -> str | None:
    text = _safe_text(raw)
    return text or None


def _norm_type_label(*parts: Any) -> str:
    joined = " ".join(_safe_text(p) for p in parts if _safe_text(p))
    return re.sub(r"\s+", " ", joined).strip()


def _duration_label_from_seconds(seconds: float | None) -> str:
    if seconds is None:
        return ""
    try:
        minutes = max(1, int(round(float(seconds) / 60.0)))
    except Exception:
        return ""
    return f"{minutes} min"


def _clean_title(title: Any) -> str:
    text = _safe_text(title)
    if not text:
        return "Unbenannter Vorschlag"
    return re.sub(r"^\s*athletica\.ai\s*\|\s*", "", text, flags=re.IGNORECASE).strip() or text


def _hr_range_label(hr: dict[str, Any] | None) -> str:
    if not isinstance(hr, dict):
        return ""
    start = _safe_float(hr.get("start"))
    end = _safe_float(hr.get("end"))
    units = _safe_lower(hr.get("units"))
    if start is None or end is None:
        return ""
    units_label = "LTHR" if units == "%lthr" else units.upper()
    return f"{int(round(start))}–{int(round(end))} % {units_label}" if units_label == "LTHR" else f"{int(round(start))}–{int(round(end))} {units_label}"


def _goal_label_from_event(event: dict[str, Any]) -> str:
    blob = _safe_lower(" ".join([
        _event_name(event),
        _safe_text(event.get("description")),
        _safe_text((event.get("workout_doc") or {}).get("description")) if isinstance(event.get("workout_doc"), dict) else "",
    ]))
    mappings = [
        (("aerobic development", "aerobic base", "aerobe grundlage"), "Aerobe Grundlage"),
        (("threshold", "schwelle"), "Schwelle"),
        (("vo2max", "vo2"), "VO2max"),
        (("recovery", "regeneration"), "Regeneration"),
        (("easy", "locker"), "Locker"),
        (("long", "langer lauf", "long run"), "Langer Lauf"),
    ]
    for tokens, label in mappings:
        if any(token in blob for token in tokens):
            return label
    return "Ausdauer"


def _structure_segment_label(step: dict[str, Any], *, goal_label: str) -> str:
    duration_label = _duration_label_from_seconds(_safe_float(step.get("duration")))
    if step.get("warmup"):
        return f"{duration_label} einlaufen".strip()
    if step.get("cooldown"):
        return f"{duration_label} auslaufen".strip()
    hr_label = _hr_range_label(step.get("hr"))
    relaxed_tokens = {"Aerobe Grundlage", "Locker", "Regeneration", "Langer Lauf", "Ausdauer"}
    middle = "locker" if goal_label in relaxed_tokens else "im Zielbereich"
    if hr_label:
        hr_low = _safe_float((step.get("hr") or {}).get("start"))
        hr_high = _safe_float((step.get("hr") or {}).get("end"))
        if hr_low is not None and hr_high is not None and hr_high <= 83:
            middle = "locker"
    return f"{duration_label} {middle}".strip()


def _target_label_from_steps(steps: list[dict[str, Any]]) -> str:
    for step in steps:
        if step.get("warmup") or step.get("cooldown"):
            continue
        hr_label = _hr_range_label(step.get("hr"))
        if hr_label:
            return f"Hauptteil {hr_label}"
    return ""


def _compact_description_line(text: str) -> str:
    raw = _safe_text(text).replace("\r", "\n")
    if not raw:
        return ""
    parts = []
    for chunk in re.split(r"[\n•\-]+", raw):
        line = _safe_text(chunk)
        if not line:
            continue
        if line.lower().startswith("the aim of this session is"):
            continue
        parts.append(line)
        if len(parts) >= 2:
            break
    return " · ".join(parts[:2])


def normalize_intervals_event(event: dict[str, Any]) -> dict[str, Any]:
    workout_doc = event.get("workout_doc") if isinstance(event.get("workout_doc"), dict) else {}
    duration_seconds = (
        _safe_float(workout_doc.get("duration"))
        or _safe_float(event.get("moving_time"))
        or _safe_float(event.get("time_target"))
        or 0.0
    )
    steps = [step for step in (workout_doc.get("steps") or []) if isinstance(step, dict)]
    goal_label = _goal_label_from_event(event)
    structure_segments = [_structure_segment_label(step, goal_label=goal_label) for step in steps]
    structure_segments = [segment for segment in structure_segments if segment]
    structure_label = " · ".join(structure_segments[:4]).strip()
    if not structure_label:
        structure_label = _compact_description_line(_safe_text(workout_doc.get("description")) or _safe_text(event.get("description")))
    target_label = _target_label_from_steps(steps)
    if not target_label:
        target_label = _compact_description_line(_safe_text(event.get("description")))
    intensity_value = _safe_float(event.get("icu_intensity"))
    training_load = _event_load(event)
    return {
        "source": "Intervals.icu",
        "title": _clean_title(_event_name(event)),
        "source_title": _event_name(event),
        "sport": _event_type(event) or "Run",
        "date": _extract_event_day(event),
        "duration_seconds": int(round(duration_seconds or 0.0)) if duration_seconds else None,
        "duration_label": _duration_label_from_seconds(duration_seconds) if duration_seconds else "",
        "training_load": int(round(training_load)) if training_load is not None else None,
        "intensity": int(round(intensity_value)) if intensity_value is not None else None,
        "structure_label": structure_label,
        "target_label": target_label,
        "goal_label": goal_label,
        "raw_event_id": _event_id(event),
        "external_id": _safe_text(event.get("external_id")) or _safe_text(event.get("uid")) or _event_id(event),
        "start": _extract_datetime(_event_field(event, "start_date_local", "start_date", "date")),
        "category": _event_category(event),
        "description": _safe_text(event.get("description")) or _safe_text(workout_doc.get("description")),
    }


def ensure_endurance_approval_schema() -> None:
    conn = get_core_db()
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS endurance_plan_approvals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL,
                source TEXT NOT NULL,
                intervals_event_id TEXT,
                original_name TEXT,
                original_type TEXT,
                original_category TEXT,
                original_start TEXT,
                original_duration REAL,
                original_load REAL,
                raw_json TEXT NOT NULL DEFAULT '{}',
                liva_status TEXT NOT NULL DEFAULT 'UNKNOWN',
                liva_decision TEXT NOT NULL DEFAULT 'UNKNOWN',
                liva_reason TEXT,
                conflicts_json TEXT NOT NULL DEFAULT '[]',
                final_plan_text TEXT,
                execution_mode TEXT NOT NULL DEFAULT 'RUN',
                execution_mode_updated_at TEXT,
                execution_mode_source TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        cols = _table_columns(conn, "endurance_plan_approvals")
        if "execution_mode" not in cols:
            conn.execute("ALTER TABLE endurance_plan_approvals ADD COLUMN execution_mode TEXT NOT NULL DEFAULT 'RUN'")
        if "execution_mode_updated_at" not in cols:
            conn.execute("ALTER TABLE endurance_plan_approvals ADD COLUMN execution_mode_updated_at TEXT")
        if "execution_mode_source" not in cols:
            conn.execute("ALTER TABLE endurance_plan_approvals ADD COLUMN execution_mode_source TEXT")
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_endurance_approvals_day_event
            ON endurance_plan_approvals(date, source, intervals_event_id)
            """
        )
        conn.commit()
    finally:
        conn.close()


def _load_active_gym_plan() -> dict[str, Any] | None:
    conn = get_plans_db()
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            """
            SELECT *
            FROM gym_plans
            WHERE is_active=1 AND is_archived=0
            ORDER BY updated_at DESC, id DESC
            LIMIT 1
            """
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    try:
        payload = json.loads(row["plan_json"] or "{}")
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    return payload


def _gym_plan_events_for_day(day_iso: str) -> list[dict[str, Any]]:
    plan = _load_active_gym_plan() or {}
    base_week = plan.get("base_week") if isinstance(plan.get("base_week"), dict) else {}
    weekday_key = DAY_ORDER[date.fromisoformat(day_iso).weekday()]
    events = base_week.get(weekday_key) if isinstance(base_week, dict) else []
    return [item for item in (events or []) if isinstance(item, dict)]


def _event_is_lower(event: dict[str, Any]) -> bool:
    blob = _safe_lower(" ".join([
        event.get("title") or "",
        event.get("name") or "",
        event.get("kind") or "",
        event.get("note") or "",
        event.get("session_name") or "",
    ]))
    if any(token in blob for token in ("lower", "legs", "leg day", "beine", "squat", "deadlift", "rdl", "hinge")):
        return True
    items = event.get("items") if isinstance(event.get("items"), list) else []
    for item in items:
        if not isinstance(item, dict):
            continue
        name = _safe_lower(item.get("name"))
        if any(token in name for token in ("squat", "beinstrecker", "beincurl", "leg", "rdl", "deadlift", "hip thrust", "lunge", "calf")):
            return True
    return False


def _lower_context(day_iso: str) -> dict[str, bool]:
    dates = {
        "yesterday": (date.fromisoformat(day_iso) - timedelta(days=1)).isoformat(),
        "today": day_iso,
        "tomorrow": (date.fromisoformat(day_iso) + timedelta(days=1)).isoformat(),
    }
    return {
        key: any(_event_is_lower(event) for event in _gym_plan_events_for_day(target_day))
        for key, target_day in dates.items()
    }


def _latest_workout_name(day_iso: str) -> str | None:
    conn = get_training_db()
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT name FROM workouts WHERE date_iso=? ORDER BY id DESC LIMIT 1",
            (day_iso,),
        ).fetchone()
    finally:
        conn.close()
    return _safe_text(row["name"]) if row else None


def _read_core_day_summary(day_iso: str) -> dict[str, Any]:
    conn = get_core_db()
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT decision_json, readiness_json FROM core_daily_decisions WHERE day_iso=? LIMIT 1",
            (day_iso,),
        ).fetchone()
    except Exception:
        row = None
    finally:
        conn.close()
    decision = _parse_json(row["decision_json"], {}) if row else {}
    readiness = _parse_json(row["readiness_json"], {}) if row else {}
    hero = _safe_text(decision.get("hero")) if isinstance(decision, dict) else ""
    planned_unit = decision.get("planned_unit") if isinstance(decision.get("planned_unit"), dict) else {}
    permissions = decision.get("permissions") if isinstance(decision.get("permissions"), dict) else {}
    return {
        "hero": hero,
        "planned_unit": planned_unit,
        "permissions": permissions,
        "readiness": readiness if isinstance(readiness, dict) else {},
    }


def _read_hrv_signal(day_iso: str) -> dict[str, Any]:
    conn = get_hrv_db()
    conn.row_factory = sqlite3.Row
    try:
        cols = _table_columns(conn, "hrv_measurements")
        if not cols:
            return {}
        select_cols = [c for c in ("date_utc", "ts_measurement", "rmssd", "hr", "sleep_quality", "fatigue", "training_motivation", "sickness_bool") if c in cols]
        row = conn.execute(
            f"""
            SELECT {", ".join(select_cols)}
            FROM hrv_measurements
            WHERE substr(COALESCE(date_utc, ts_measurement), 1, 10) <= ?
            ORDER BY COALESCE(ts_measurement, date_utc) DESC
            LIMIT 1
            """,
            (day_iso,),
        ).fetchone()
    except Exception:
        row = None
    finally:
        conn.close()
    if not row:
        return {}
    observed_day = _extract_date(row["date_utc"] if "date_utc" in row.keys() else row["ts_measurement"] if "ts_measurement" in row.keys() else None)
    return {
        "present_today": observed_day == day_iso,
        "rmssd": row["rmssd"] if "rmssd" in row.keys() else None,
        "hr": row["hr"] if "hr" in row.keys() else None,
        "sleep_quality": row["sleep_quality"] if "sleep_quality" in row.keys() else None,
        "fatigue": row["fatigue"] if "fatigue" in row.keys() else None,
        "training_motivation": row["training_motivation"] if "training_motivation" in row.keys() else None,
        "sick": bool(row["sickness_bool"]) if "sickness_bool" in row.keys() and row["sickness_bool"] is not None else False,
    }


def _read_nutrition_signal(day_iso: str) -> dict[str, Any]:
    conn = get_nutrition_db()
    conn.row_factory = sqlite3.Row
    try:
        weight_row = conn.execute(
            """
            SELECT date_iso, weight_kg, kcal, protein, carbs, fat
            FROM weight_logs
            WHERE date_iso <= ?
            ORDER BY date_iso DESC, id DESC
            LIMIT 1
            """,
            (day_iso,),
        ).fetchone()
        actual_cols = _table_columns(conn, "nutrition_day_actuals")
        actual_row = None
        if actual_cols:
            actual_row = conn.execute(
                """
                SELECT day, kcal, p, c, f
                FROM nutrition_day_actuals
                WHERE day=?
                ORDER BY id DESC
                LIMIT 1
                """,
                (day_iso,),
            ).fetchone()
    except Exception:
        weight_row = None
        actual_row = None
    finally:
        conn.close()
    return {
        "weight_today": bool(weight_row and _safe_text(weight_row["date_iso"]) == day_iso),
        "weight_kg": weight_row["weight_kg"] if weight_row else None,
        "carbs": (actual_row["c"] if actual_row else None) or (weight_row["carbs"] if weight_row else None),
        "kcal": (actual_row["kcal"] if actual_row else None) or (weight_row["kcal"] if weight_row else None),
        "protein": (actual_row["p"] if actual_row else None) or (weight_row["protein"] if weight_row else None),
    }


def _read_school_stress(day_iso: str) -> dict[str, Any]:
    conn = get_training_db()
    conn.row_factory = sqlite3.Row
    try:
        cols = _table_columns(conn, "school_schedule_entries")
        if not cols:
            return {}
        day_col = "date_iso" if "date_iso" in cols else "day_iso" if "day_iso" in cols else None
        if not day_col:
            return {}
        rows = conn.execute(
            f"SELECT * FROM school_schedule_entries WHERE {day_col}=?",
            (day_iso,),
        ).fetchall()
    except Exception:
        rows = []
    finally:
        conn.close()
    return {"entry_count": len(rows or [])}


def _event_field(event: dict[str, Any], *names: str) -> Any:
    for name in names:
        if name in event and event.get(name) not in (None, ""):
            return event.get(name)
    return None


def _event_duration_minutes(event: dict[str, Any]) -> float | None:
    normalized = normalize_intervals_event(event)
    seconds = _safe_float(normalized.get("duration_seconds"))
    if seconds is None:
        return None
    return round(seconds / 60.0, 1)


def _event_load(event: dict[str, Any]) -> float | None:
    for key in ("load", "icu_training_load", "training_load", "planned_load"):
        val = _safe_float(event.get(key))
        if val is not None:
            return val
    return None


def _event_name(event: dict[str, Any]) -> str:
    for key in ("name", "title", "workoutName", "description"):
        text = _safe_text(event.get(key))
        if text:
            return text
    return "Unbenannter Vorschlag"


def _event_type(event: dict[str, Any]) -> str:
    return _safe_text(_event_field(event, "type", "workout_type", "sport_type"))


def _event_category(event: dict[str, Any]) -> str:
    return _safe_text(_event_field(event, "category", "sub_type", "event_type"))


def _event_id(event: dict[str, Any]) -> str:
    for key in ("id", "event_id", "uid"):
        text = _safe_text(event.get(key))
        if text:
            return text
    fallback = "|".join([
        _event_name(event),
        _extract_datetime(_event_field(event, "start_date_local", "start_date", "date")) or "",
        _event_type(event),
    ])
    return fallback or "unknown-event"


def _event_is_endurance_candidate(event: dict[str, Any]) -> bool:
    blob = _safe_lower(" ".join([
        _event_name(event),
        _event_type(event),
        _event_category(event),
        _safe_text(event.get("description")),
    ]))
    if not blob:
        return False
    positive = (
        "run", "lauf", "jog", "threshold", "tempo", "interval", "z2", "zone 2",
        "easy", "recovery run", "long run", "cardio", "endurance", "vo2", "hill",
        "aerobic development", "aerobic base",
    )
    negative = ("swim", "ride", "bike", "cycle", "strength", "gym", "lift")
    return any(token in blob for token in positive) and not any(token in blob for token in negative)


def _event_intensity(event: dict[str, Any]) -> str:
    normalized = normalize_intervals_event(event)
    blob = _safe_lower(" ".join([
        normalized.get("title"),
        _event_type(event),
        _event_category(event),
        _safe_text(event.get("description")),
        normalized.get("goal_label"),
    ]))
    easy_tokens = ("easy", "locker", "z1", "z2", "recovery", "aerobic")
    hard_tokens = ("threshold", "tempo", "interval", "vo2", "hill", "repeat", "race", "anaerobic")
    if any(token in blob for token in hard_tokens):
        return "hard"
    if any(token in blob for token in easy_tokens):
        return "easy"
    intensity_score = _safe_float(normalized.get("intensity"))
    if intensity_score is not None and intensity_score <= 65:
        return "easy"
    load = _event_load(event)
    duration = _event_duration_minutes(event)
    if load is not None and load >= 70:
        return "hard"
    if intensity_score is not None and intensity_score >= 78:
        return "hard"
    if duration is not None and duration <= 45:
        return "easy"
    return "moderate"


def _event_intensity_rank(event: dict[str, Any]) -> int:
    intensity = _event_intensity(event)
    if intensity == "hard":
        return 3
    if intensity == "moderate":
        return 2
    return 1


def _build_final_summary(normalized: dict[str, Any]) -> str:
    if not isinstance(normalized, dict):
        return ""
    sport = _safe_text(normalized.get("sport"))
    sport_label = "Lauf" if sport.lower() == "run" else sport
    parts = [
        normalized.get("duration_label"),
        sport_label,
        _safe_lower(normalized.get("goal_label")),
    ]
    summary = " · ".join([_safe_text(part) for part in parts if _safe_text(part)])
    target = _safe_text(normalized.get("target_label"))
    if target:
        summary = f"{summary} · {target}" if summary else target
    return summary


def _execution_mode_value(value: Any) -> str:
    raw = _safe_text(value).upper()
    return "ERGO" if raw == "ERGO" else "RUN"


def _build_execution_final_summary(normalized: dict[str, Any], execution_mode: str) -> str:
    mode = _execution_mode_value(execution_mode)
    if mode == "ERGO":
        parts = [
            _safe_text(normalized.get("duration_label")),
            "Ergo",
            _safe_lower(normalized.get("goal_label")),
            "locker halten",
        ]
        summary = " · ".join([part for part in parts if part])
        target = _safe_text(normalized.get("target_label"))
        if target:
            summary = f"{summary} · {target}" if summary else target
        return summary
    return _build_final_summary(normalized)


def _mode_specific_reason(base_reason: str, execution_mode: str) -> str:
    mode = _execution_mode_value(execution_mode)
    if mode != "ERGO":
        return base_reason
    suffix = "Ergo locker im Zielpulsbereich, keine Zusatzhärte."
    if not base_reason:
        return suffix
    if suffix.lower() in base_reason.lower():
        return base_reason
    return f"{base_reason} {suffix}"


def _final_recommendation_text(status: str, decision: str, intensity: str, normalized: dict[str, Any], conflicts: list[str], execution_mode: str = "RUN") -> str:
    goal = _safe_lower(normalized.get("goal_label"))
    easy_goal = any(token in goal for token in ("locker", "aerobe", "regeneration", "ausdauer", "langer"))
    if _execution_mode_value(execution_mode) == "ERGO":
        if decision == "MODIFY":
            return "Angepasst durchführen: locker auf dem Ergo"
        if status == "GREEN" and decision == "DO":
            return "Durchführen: locker auf dem Ergo"
    if decision == "MODIFY" and "CORE vorsichtig" in conflicts and easy_goal:
        return "Angepasst durchführen: locker halten"
    if decision == "MODIFY" and easy_goal:
        return "Durchführen, aber Zielbereich nicht überschreiten"
    if status == "GREEN" and decision == "DO":
        return "Durchführen"
    if decision == "MODIFY":
        return "Angepasst durchführen"
    if decision == "MOVE":
        return "Heute verschieben"
    if decision == "SKIP":
        return "Heute auslassen"
    if status == "UNKNOWN":
        return "Noch offen"
    return "Prüfen"


def _evaluate_event(event: dict[str, Any], day_iso: str) -> dict[str, Any]:
    normalized = normalize_intervals_event(event)
    lower = _lower_context(day_iso)
    core = _read_core_day_summary(day_iso)
    hrv = _read_hrv_signal(day_iso)
    nutrition = _read_nutrition_signal(day_iso)
    school = _read_school_stress(day_iso)

    intensity = _event_intensity(event)
    goal_label = _safe_text(normalized.get("goal_label"))
    is_easy_endurance = intensity == "easy" or goal_label in {"Aerobe Grundlage", "Locker", "Regeneration", "Langer Lauf"}
    conflicts: list[str] = []
    reasons: list[str] = []
    severity = 0

    if not any([lower["yesterday"], lower["today"], lower["tomorrow"], core.get("hero"), hrv, nutrition, school]):
        return {
            "status": "UNKNOWN",
            "decision": "UNKNOWN",
            "reason": "Zu wenige Tagesdaten für eine saubere Freigabe.",
            "conflicts": [],
            "final_plan_text": "Noch offen",
            "execution_mode": "RUN",
            "context": {"intensity": intensity, "lower": lower, "core": core, "hrv": hrv, "nutrition": nutrition, "school": school},
        }

    if lower["today"]:
        conflicts.append("Lower heute")
        severity += 2 if intensity == "hard" else 1
    if lower["yesterday"]:
        conflicts.append("Lower gestern")
        severity += 2 if intensity == "hard" else 1
    if lower["tomorrow"]:
        conflicts.append("Lower morgen")
        severity += 1 if intensity == "hard" else 0

    hero_text = _safe_lower(core.get("hero"))
    permissions = core.get("permissions") if isinstance(core.get("permissions"), dict) else {}
    intensity_permission = _safe_float(permissions.get("intensity_permission"))
    volume_permission = _safe_float(permissions.get("volume_permission"))
    if "qualität nur" in hero_text or "kontrolliert" in hero_text:
        conflicts.append("CORE vorsichtig")
        severity += 1
    if "nicht" in hero_text and any(token in hero_text for token in ("hart", "intens", "push")):
        conflicts.append("CORE gegen Härte")
        severity += 2
    if intensity_permission is not None and intensity_permission < 45:
        conflicts.append("CORE-Tageslast hoch")
        severity += 2 if intensity == "hard" else 1
    elif intensity == "hard" and intensity_permission is not None and intensity_permission < 55:
        conflicts.append("CORE-Tageslast hoch")
        severity += 2
    if intensity == "hard" and volume_permission is not None and volume_permission < 55:
        conflicts.append("Tagesreserve knapp")
        severity += 1

    sleep_quality = _safe_float(hrv.get("sleep_quality"))
    if sleep_quality is not None and sleep_quality < 6:
        conflicts.append("Schlaf schwach")
        severity += 2 if intensity == "hard" else 1
    fatigue_text = _safe_lower(hrv.get("fatigue"))
    if fatigue_text and fatigue_text not in {"ok", "gut", "normal"}:
        conflicts.append("Recovery unsauber")
        severity += 1
    if hrv.get("sick"):
        conflicts.append("Krankheitssignal")
        severity += 3

    carbs = _safe_float(nutrition.get("carbs"))
    duration_min = _safe_float(_event_duration_minutes(event))
    if carbs is not None and ((intensity == "hard") or (duration_min is not None and duration_min >= 75)) and carbs < 160:
        conflicts.append("Carbs knapp")
        severity += 1

    school_entries = int(school.get("entry_count") or 0)
    if school_entries >= 6 and intensity == "hard":
        conflicts.append("Schultag voll")
        severity += 1

    if is_easy_endurance and severity == 0:
        status, decision = "GREEN", "DO"
        reasons.append("Lockerer Ausdauerreiz passt zur heutigen Belastung.")
    elif intensity == "hard" and severity >= 4:
        status = "RED"
        decision = "MOVE" if lower["tomorrow"] or not lower["today"] else "SKIP"
        reasons.append("Gesamtbelastung heute zu hoch.")
    elif is_easy_endurance and conflicts == ["CORE vorsichtig"]:
        status, decision = "YELLOW", "MODIFY"
        reasons.append("CORE ist vorsichtig, deshalb Zielbereich nicht überschreiten.")
    elif intensity == "hard" and severity >= 2:
        status, decision = "YELLOW", "MODIFY"
        reasons.append("Intensität passt heute nicht sauber.")
    elif severity >= 3:
        status, decision = "RED", "SKIP"
        reasons.append("Mehrere Konflikte sprechen gegen den Lauf.")
    elif severity >= 1:
        status, decision = "YELLOW", "MODIFY"
        reasons.append("Der Vorschlag braucht heute Anpassung.")
    else:
        status, decision = "GREEN", "DO"
        reasons.append("Ausdauerreiz passt heute sauber hinein.")

    reason = reasons[0] if reasons else "Freigabe ohne klare Zusatzsignale."
    final_plan_text = _final_recommendation_text(status, decision, intensity, normalized, conflicts)
    final_line = _build_final_summary(normalized)
    return {
        "status": status,
        "decision": decision,
        "reason": reason,
        "conflicts": conflicts,
        "final_plan_text": final_plan_text,
        "final_summary": final_line,
        "execution_mode": "RUN",
        "normalized_workout": normalized,
        "context": {"intensity": intensity, "lower": lower, "core": core, "hrv": hrv, "nutrition": nutrition, "school": school},
    }


def _apply_execution_mode_to_evaluation(evaluation: dict[str, Any], execution_mode: str) -> dict[str, Any]:
    out = dict(evaluation or {})
    normalized = out.get("normalized_workout") if isinstance(out.get("normalized_workout"), dict) else {}
    conflicts = out.get("conflicts") if isinstance(out.get("conflicts"), list) else []
    intensity = _safe_text((out.get("context") or {}).get("intensity"))
    mode = _execution_mode_value(execution_mode)
    out["execution_mode"] = mode
    out["final_plan_text"] = _final_recommendation_text(
        _safe_text(out.get("status")) or "UNKNOWN",
        _safe_text(out.get("decision")) or "UNKNOWN",
        intensity,
        normalized,
        conflicts,
        mode,
    )
    out["final_summary"] = _build_execution_final_summary(normalized, mode)
    out["reason"] = _mode_specific_reason(_safe_text(out.get("reason")), mode)
    return out


def _extract_event_day(event: dict[str, Any]) -> str | None:
    for key in ("start_date_local", "start_date", "date", "day", "event_date"):
        day = _extract_date(event.get(key))
        if day:
            return day
    return None


def _store_approval(day_iso: str, event: dict[str, Any], evaluation: dict[str, Any]) -> None:
    ensure_endurance_approval_schema()
    conn = get_core_db()
    try:
        now = _utc_now_iso()
        conn.execute(
            """
            INSERT INTO endurance_plan_approvals (
                date, source, intervals_event_id, original_name, original_type, original_category,
                original_start, original_duration, original_load, raw_json,
                liva_status, liva_decision, liva_reason, conflicts_json, final_plan_text,
                execution_mode, execution_mode_updated_at, execution_mode_source,
                created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(date, source, intervals_event_id)
            DO UPDATE SET
                original_name=excluded.original_name,
                original_type=excluded.original_type,
                original_category=excluded.original_category,
                original_start=excluded.original_start,
                original_duration=excluded.original_duration,
                original_load=excluded.original_load,
                raw_json=excluded.raw_json,
                liva_status=excluded.liva_status,
                liva_decision=excluded.liva_decision,
                liva_reason=excluded.liva_reason,
                conflicts_json=excluded.conflicts_json,
                final_plan_text=excluded.final_plan_text,
                execution_mode=endurance_plan_approvals.execution_mode,
                execution_mode_updated_at=endurance_plan_approvals.execution_mode_updated_at,
                execution_mode_source=endurance_plan_approvals.execution_mode_source,
                updated_at=excluded.updated_at
            """,
            (
                day_iso,
                "intervals",
                _event_id(event),
                _event_name(event),
                _event_type(event),
                _event_category(event),
                _extract_datetime(_event_field(event, "start_date_local", "start_date", "date")),
                _event_duration_minutes(event),
                _event_load(event),
                json.dumps(event, ensure_ascii=False),
                evaluation.get("status") or "UNKNOWN",
                evaluation.get("decision") or "UNKNOWN",
                evaluation.get("reason") or "",
                json.dumps(evaluation.get("conflicts") or [], ensure_ascii=False),
                evaluation.get("final_plan_text") or "",
                _execution_mode_value(evaluation.get("execution_mode")),
                now,
                "liva_default",
                now,
                now,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _approval_row_to_payload(row: sqlite3.Row) -> dict[str, Any]:
    conflicts = _parse_json(row["conflicts_json"], [])
    if not isinstance(conflicts, list):
        conflicts = []
    raw_json = _parse_json(row["raw_json"], {})
    normalized = normalize_intervals_event(raw_json) if isinstance(raw_json, dict) and raw_json else {}
    execution_mode = _execution_mode_value(row["execution_mode"] if "execution_mode" in row.keys() else "RUN")
    row_status = row["liva_status"] or "UNKNOWN"
    row_decision = row["liva_decision"] or "UNKNOWN"
    row_reason = _mode_specific_reason(row["liva_reason"] or "", execution_mode)
    row_recommendation = _final_recommendation_text(
        row_status,
        row_decision,
        _event_intensity(raw_json) if isinstance(raw_json, dict) and raw_json else "",
        normalized,
        [str(item) for item in conflicts if str(item).strip()],
        execution_mode,
    )
    return {
        "day": row["date"],
        "source": "Intervals.icu",
        "connected": True,
        "reachable": True,
        "has_workout": True,
        "status": row_status,
        "status_color": STATUS_COLORS.get(row_status, "unknown"),
        "decision": row_decision,
        "recommendation": row_recommendation or "Noch offen",
        "final_plan_text": row_recommendation or "Noch offen",
        "reason": row_reason,
        "conflicts": [str(item) for item in conflicts if str(item).strip()],
        "execution_mode": execution_mode,
        "execution_mode_updated_at": row["execution_mode_updated_at"] if "execution_mode_updated_at" in row.keys() else None,
        "execution_mode_source": row["execution_mode_source"] if "execution_mode_source" in row.keys() else None,
        "execution_mode_from_db": bool(row["execution_mode"]) if "execution_mode" in row.keys() else False,
        "final_summary": _build_execution_final_summary(normalized, execution_mode),
        "normalized_workout": normalized,
        "proposal": {
            "name": normalized.get("title") or row["original_name"] or "Unbenannter Vorschlag",
            "source_title": normalized.get("source_title") or row["original_name"] or "",
            "type": normalized.get("sport") or row["original_type"] or "",
            "category": row["original_category"] or "",
            "start": row["original_start"],
            "duration_min": row["original_duration"],
            "load": row["original_load"],
            "duration_label": normalized.get("duration_label") or "",
            "structure_label": normalized.get("structure_label") or "",
            "target_label": normalized.get("target_label") or "",
            "goal_label": normalized.get("goal_label") or "",
            "intensity": normalized.get("intensity"),
            "external_id": normalized.get("external_id") or "",
        },
    }


def _load_stored_approval(day_iso: str) -> dict[str, Any] | None:
    ensure_endurance_approval_schema()
    conn = get_core_db()
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            """
            SELECT *
            FROM endurance_plan_approvals
            WHERE date=?
            ORDER BY updated_at DESC, id DESC
            LIMIT 1
            """,
            (day_iso,),
        ).fetchone()
    finally:
        conn.close()
    return _approval_row_to_payload(row) if row else None


def _load_approval_row(day_iso: str, intervals_event_id: str | None = None) -> sqlite3.Row | None:
    ensure_endurance_approval_schema()
    conn = get_core_db()
    conn.row_factory = sqlite3.Row
    try:
        if intervals_event_id:
            row = conn.execute(
                """
                SELECT *
                FROM endurance_plan_approvals
                WHERE date=? AND intervals_event_id=?
                ORDER BY updated_at DESC, id DESC
                LIMIT 1
                """,
                (day_iso, str(intervals_event_id)),
            ).fetchone()
            if row:
                return row
        row = conn.execute(
            """
            SELECT *
            FROM endurance_plan_approvals
            WHERE date=?
            ORDER BY updated_at DESC, id DESC
            LIMIT 1
            """,
            (day_iso,),
        ).fetchone()
        return row
    finally:
        conn.close()


def set_execution_mode(day_iso: str, intervals_event_id: str | None, execution_mode: str, *, source: str = "dashboard") -> dict[str, Any]:
    row = _load_approval_row(day_iso, intervals_event_id)
    if not row:
        raise ValueError("approval_not_found")
    mode = _execution_mode_value(execution_mode)
    conn = get_core_db()
    try:
        now = _utc_now_iso()
        conn.execute(
            """
            UPDATE endurance_plan_approvals
            SET execution_mode=?, execution_mode_updated_at=?, execution_mode_source=?, updated_at=?
            WHERE id=?
            """,
            (mode, now, source, now, row["id"]),
        )
        conn.commit()
    finally:
        conn.close()
    updated = _load_approval_row(day_iso, intervals_event_id or row["intervals_event_id"])
    if not updated:
        raise ValueError("approval_not_found")
    return _approval_row_to_payload(updated)


def build_endurance_approval_payload(day_iso: str | None = None, *, lookahead_days: int = 3) -> dict[str, Any]:
    ensure_endurance_approval_schema()
    day_iso = day_iso or date.today().isoformat()
    today = date.fromisoformat(day_iso)
    oldest = day_iso
    newest = (today + timedelta(days=max(0, int(lookahead_days)))).isoformat()
    client = IntervalsClient()
    base = {
        "ok": True,
        "day": day_iso,
        "source": "Intervals.icu",
        "connected": client.configured,
        "reachable": False,
        "has_workout": False,
        "status": "UNKNOWN",
        "status_color": "unknown",
        "decision": "UNKNOWN",
        "proposal": None,
        "recommendation": "Noch offen",
        "final_plan_text": "Noch offen",
        "reason": "",
        "conflicts": [],
        "normalized_workout": None,
        "execution_mode": "RUN",
        "execution_mode_from_db": False,
        "message": "",
    }
    if not client.configured:
        base["message"] = "Intervals nicht verbunden."
        stored = _load_stored_approval(day_iso)
        if stored:
            stored["connected"] = False
            stored["message"] = "Intervals nicht verbunden."
            return {"ok": True, **stored}
        return base

    try:
        events = client.get_events(oldest, newest)
        client.get_wellness(oldest, newest)
        client.get_activities(oldest, newest)
        base["reachable"] = True
    except IntervalsNotConfiguredError:
        base["message"] = "Intervals nicht verbunden."
        return base
    except IntervalsClientError as exc:
        stored = _load_stored_approval(day_iso)
        if stored:
            stored["reachable"] = False
            stored["message"] = "Intervals aktuell nicht erreichbar."
            stored["error"] = str(exc)
            return {"ok": True, **stored}
        base["message"] = "Intervals aktuell nicht erreichbar."
        base["error"] = str(exc)
        return base

    event_list = events if isinstance(events, list) else (events.get("events") if isinstance(events, dict) else [])
    event_list = [item for item in (event_list or []) if isinstance(item, dict)]
    day_events = [item for item in event_list if _extract_event_day(item) == day_iso]
    candidates = [item for item in day_events if _event_is_endurance_candidate(item)]
    if not candidates:
        base["message"] = "Heute kein Intervals.icu-Lauf geplant."
        return base

    event = sorted(
        candidates,
        key=lambda item: (_event_intensity_rank(item), _event_duration_minutes(item) or 0),
        reverse=True,
    )[0]
    normalized = normalize_intervals_event(event)
    evaluation = _evaluate_event(event, day_iso)
    existing_row = _load_approval_row(day_iso, _event_id(event))
    existing_mode = _execution_mode_value(existing_row["execution_mode"]) if existing_row and "execution_mode" in existing_row.keys() else "RUN"
    evaluation = _apply_execution_mode_to_evaluation(evaluation, existing_mode)
    _store_approval(day_iso, event, evaluation)
    stored = _load_stored_approval(day_iso)
    if stored:
        stored["message"] = ""
        return {"ok": True, **stored}
    return {
        **base,
        "has_workout": True,
        "status": evaluation["status"],
        "status_color": STATUS_COLORS.get(evaluation["status"], "unknown"),
        "decision": evaluation["decision"],
        "proposal": {
            "name": normalized.get("title"),
            "source_title": normalized.get("source_title"),
            "type": normalized.get("sport"),
            "category": _event_category(event),
            "start": normalized.get("start"),
            "duration_min": _event_duration_minutes(event),
            "load": _event_load(event),
            "duration_label": normalized.get("duration_label"),
            "structure_label": normalized.get("structure_label"),
            "target_label": normalized.get("target_label"),
            "goal_label": normalized.get("goal_label"),
            "intensity": normalized.get("intensity"),
            "external_id": normalized.get("external_id"),
        },
        "normalized_workout": normalized,
        "recommendation": evaluation["final_plan_text"],
        "final_plan_text": evaluation["final_plan_text"],
        "reason": evaluation["reason"],
        "conflicts": evaluation["conflicts"],
        "final_summary": evaluation.get("final_summary") or "",
        "message": "",
    }


def build_intervals_debug_payload(day_iso: str | None = None, *, lookahead_days: int = 3) -> dict[str, Any]:
    day_iso = day_iso or date.today().isoformat()
    today = date.fromisoformat(day_iso)
    oldest = day_iso
    newest = (today + timedelta(days=max(0, int(lookahead_days)))).isoformat()
    client = IntervalsClient()
    payload: dict[str, Any] = {
        "ok": True,
        "day": day_iso,
        "intervals_api_key_set": client.configured,
        "base_url": client.base_url,
        "window": {"oldest": oldest, "newest": newest},
        "raw": {"events": [], "wellness": None, "activities": None},
        "detected_endurance_workouts": [],
        "normalized_workouts": [],
        "approval": None,
        "error": None,
    }
    if not client.configured:
        payload["approval"] = build_endurance_approval_payload(day_iso, lookahead_days=lookahead_days)
        return payload
    try:
        events = client.get_events(oldest, newest)
        wellness = client.get_wellness(oldest, newest)
        activities = client.get_activities(oldest, newest)
        payload["raw"] = {"events": events, "wellness": wellness, "activities": activities}
        event_list = events if isinstance(events, list) else (events.get("events") if isinstance(events, dict) else [])
        event_list = [item for item in (event_list or []) if isinstance(item, dict)]
        payload["detected_endurance_workouts"] = [
            {
                "day": _extract_event_day(item),
                "id": _event_id(item),
                "name": _event_name(item),
                "type": _event_type(item),
                "category": _event_category(item),
                "duration_min": _event_duration_minutes(item),
                "load": _event_load(item),
                "intensity": _event_intensity(item),
            }
            for item in event_list
            if _event_is_endurance_candidate(item)
        ]
        payload["normalized_workouts"] = [
            {
                key: value
                for key, value in normalize_intervals_event(item).items()
                if key in {"title", "sport", "duration_label", "structure_label", "target_label", "goal_label", "training_load", "intensity", "date", "external_id"}
            }
            for item in event_list
            if _event_is_endurance_candidate(item)
        ]
        payload["approval"] = build_endurance_approval_payload(day_iso, lookahead_days=lookahead_days)
        if isinstance(payload.get("approval"), dict):
            payload["execution_mode"] = payload["approval"].get("execution_mode")
            payload["execution_mode_from_db"] = payload["approval"].get("execution_mode_from_db")
            payload["final_plan_text"] = payload["approval"].get("final_plan_text")
            payload["final_summary"] = payload["approval"].get("final_summary")
            payload["original_sport"] = ((payload["approval"].get("normalized_workout") or {}).get("sport"))
    except Exception as exc:
        payload["error"] = str(exc)
        payload["approval"] = build_endurance_approval_payload(day_iso, lookahead_days=lookahead_days)
    return payload


def build_endurance_approval(day_iso: str | None = None, *, refresh: bool = True) -> dict[str, Any]:
    _ = refresh
    return build_endurance_approval_payload(day_iso)


def build_endurance_core_context(day_iso: str | None = None, *, refresh: bool = True) -> dict[str, Any]:
    _ = refresh
    payload = build_endurance_approval_payload(day_iso)
    day = payload.get("day") or payload.get("date") or _extract_date(day_iso) or date.today().isoformat()
    if not payload.get("connected"):
        return {
            "available": False,
            "connected": False,
            "reachable": bool(payload.get("reachable")),
            "has_workout": False,
            "date": day,
            "message": payload.get("message") or "Intervals nicht verbunden.",
        }
    if not payload.get("reachable"):
        return {
            "available": True,
            "connected": True,
            "reachable": False,
            "has_workout": False,
            "date": day,
            "message": payload.get("message") or "Intervals aktuell nicht erreichbar.",
        }
    if not payload.get("has_workout"):
        return {
            "available": True,
            "connected": True,
            "reachable": True,
            "has_workout": False,
            "date": day,
            "message": payload.get("message") or "Heute kein Intervals.icu-Lauf geplant.",
        }

    normalized = payload.get("normalized_workout") if isinstance(payload.get("normalized_workout"), dict) else {}
    proposal = payload.get("proposal") if isinstance(payload.get("proposal"), dict) else {}
    context = payload.get("context") if isinstance(payload.get("context"), dict) else {}
    lower = context.get("lower") if isinstance(context.get("lower"), dict) else {}
    conflicts = [str(item).strip() for item in (payload.get("conflicts") or []) if str(item).strip()]
    execution_mode = _execution_mode_value(payload.get("execution_mode"))
    intensity = _safe_lower(context.get("intensity"))
    duration_seconds = _safe_float(normalized.get("duration_seconds"))
    duration_minutes = int(round(duration_seconds / 60.0)) if duration_seconds else None
    goal_label = _safe_text(normalized.get("goal_label")) or "Ausdauer"
    final_summary = _safe_text(payload.get("final_summary"))
    reason = _safe_text(payload.get("reason"))
    original_label = " · ".join(
        [
            _safe_text(normalized.get("title")),
            _safe_text(normalized.get("sport")),
            _safe_text(normalized.get("duration_label")),
        ]
    ).strip(" ·")

    load_impact = "low"
    if intensity == "hard":
        load_impact = "high"
    elif intensity in {"moderate", "easy"} or (duration_minutes is not None and duration_minutes >= 60):
        load_impact = "moderate"

    leg_impact = "normal"
    if execution_mode == "ERGO":
        leg_impact = "reduced_by_ergo"
    elif lower.get("yesterday") or lower.get("today") or lower.get("tomorrow"):
        leg_impact = "run_with_lower_conflict"

    return {
        "available": True,
        "connected": True,
        "reachable": True,
        "has_workout": True,
        "source": payload.get("source") or "Intervals.icu",
        "date": day,
        "original": {
            "sport": _safe_text(normalized.get("sport")),
            "title": _safe_text(normalized.get("title")),
            "label": original_label,
            "duration_seconds": normalized.get("duration_seconds"),
            "duration_label": _safe_text(normalized.get("duration_label")),
            "training_load": normalized.get("training_load"),
            "intensity": normalized.get("intensity"),
            "target_label": _safe_text(normalized.get("target_label")),
            "structure_label": _safe_text(normalized.get("structure_label")),
            "goal_label": goal_label,
            "intervals_event_id": normalized.get("raw_event_id") or proposal.get("external_id"),
            "external_id": _safe_text(normalized.get("external_id")),
        },
        "liva": {
            "status": payload.get("status") or "UNKNOWN",
            "decision": payload.get("decision") or "UNKNOWN",
            "execution_mode": execution_mode,
            "execution_mode_from_db": bool(payload.get("execution_mode_from_db")),
            "execution_mode_source": payload.get("execution_mode_source"),
            "execution_mode_updated_at": payload.get("execution_mode_updated_at"),
            "recommendation": payload.get("recommendation") or payload.get("final_plan_text") or "Noch offen",
            "final_plan_text": payload.get("final_plan_text") or payload.get("recommendation") or "Noch offen",
            "final_summary": final_summary,
            "reason": reason,
            "conflicts": conflicts,
        },
        "core_signal": {
            "tone": STATUS_COLORS.get(payload.get("status") or "UNKNOWN", "unknown"),
            "load_impact": load_impact,
            "gym_conflict": any("lower" in conflict.lower() or "beine" in conflict.lower() for conflict in conflicts),
            "leg_impact": leg_impact,
            "original_impact": "run" if _safe_lower(normalized.get("sport")) == "run" else _safe_lower(normalized.get("sport")),
            "execution_impact": "ergo" if execution_mode == "ERGO" else "run",
            "should_affect_core": bool((payload.get("status") or "").upper() in {"YELLOW", "RED"} or conflicts),
            "summary": f"Ausdauer geplant: {final_summary}." if final_summary else "Ausdauer geplant.",
        },
        "original_label": original_label,
        "execution_mode": execution_mode,
        "status": payload.get("status") or "UNKNOWN",
        "final_summary": final_summary,
        "reason": reason,
        "conflicts": conflicts,
        "gpt_instruction": "Bei Morning-Checkin diese Ausdauer-Freigabe berücksichtigen. Originalplan nicht mit Durchführung verwechseln.",
    }
