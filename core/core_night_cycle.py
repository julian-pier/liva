from __future__ import annotations

import json
import os
import sqlite3
from datetime import date, datetime, time as dt_time, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from flask import current_app

from core.body_clone_engine import (
    build_clone_profile,
    build_history_rows_from_decisions,
    generate_scenarios,
    rolling_window_range,
    simulate_scenario,
)
from core.heavy_first_engine import MODE_HEAVY, MODE_LIGHT, MODE_NORMAL, MODE_REST, decide_heavy_first
from core.core_daily_decision import (
    build_daily_decision,
    collect_daily_decision_context,
    maybe_send_core_morning_message,
    upsert_daily_decision,
)
from core.core_model import run_personal_learning_pass
from core.parameter_tuning_engine import get_active_parameters, latest_parameter_updates, tune_parameters
from database.connections import get_core_db, get_hrv_db, get_nutrition_db, get_runs_db, get_training_db


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return int(default)


def _safe_bool(value: Any) -> bool:
    return bool(value)


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _parse_hhmm_to_min(raw: Any) -> int | None:
    text = str(raw or "").strip()
    if not text or ":" not in text:
        return None
    try:
        hh_s, mm_s = text.split(":", 1)
        hh = int(hh_s)
        mm = int(mm_s)
    except Exception:
        return None
    if hh < 0 or hh > 23 or mm < 0 or mm > 59:
        return None
    return (hh * 60) + mm


def _format_minutes_short(total_min: Any) -> str:
    minutes = max(0, _safe_int(total_min, 0))
    hours = minutes // 60
    rest = minutes % 60
    if hours and rest:
        return f"{hours}h {rest:02d}m"
    if hours:
        return f"{hours}h"
    return f"{rest}m"


def _parse_iso_dt(raw: Any) -> datetime | None:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        if text.endswith("Z"):
            text = f"{text[:-1]}+00:00"
        dt = datetime.fromisoformat(text)
    except Exception:
        return None
    if dt.tzinfo is None:
        try:
            dt = dt.replace(tzinfo=ZoneInfo("Europe/Berlin"))
        except Exception:
            return None
    return dt


def _calendar_event_is_all_day(row: dict[str, Any]) -> bool:
    start = row.get("start") if isinstance(row.get("start"), dict) else {}
    end = row.get("end") if isinstance(row.get("end"), dict) else {}
    return bool(start.get("date")) and bool(end.get("date")) and not start.get("dateTime") and not end.get("dateTime")


def _compute_day_stress_from_intervals(
    *,
    intervals: list[tuple[int, int]],
    entries_count: int,
    horizon_start: int = 7 * 60,
    horizon_end: int = 22 * 60,
) -> dict[str, Any]:
    horizon_total = max(1, horizon_end - horizon_start)
    clipped: list[tuple[int, int]] = []
    for st_raw, en_raw in intervals:
        st = max(horizon_start, st_raw)
        en = min(horizon_end, en_raw)
        if en > st:
            clipped.append((st, en))

    clipped.sort(key=lambda x: x[0])
    merged: list[list[int]] = []
    for st, en in clipped:
        if not merged or st > merged[-1][1]:
            merged.append([st, en])
        else:
            merged[-1][1] = max(merged[-1][1], en)

    busy_minutes = sum((en - st) for st, en in merged)
    free_minutes = max(0, horizon_total - busy_minutes)
    free_ratio = free_minutes / float(horizon_total)

    largest_free = 0
    cursor = horizon_start
    for st, en in merged:
        if st > cursor:
            largest_free = max(largest_free, st - cursor)
        cursor = max(cursor, en)
    if horizon_end > cursor:
        largest_free = max(largest_free, horizon_end - cursor)

    stress = 1.0 - free_ratio
    if largest_free < 90:
        stress += 0.08
    if busy_minutes >= 8 * 60:
        stress += 0.1
    if entries_count >= 7:
        stress += 0.06
    calendar_friction = (stress * 0.72) + (_clamp((90.0 - largest_free) / 90.0) * 0.28)

    return {
        "busy_minutes_today": int(round(busy_minutes)),
        "free_minutes_today": int(round(free_minutes)),
        "free_ratio_today": round(_clamp(free_ratio), 3),
        "largest_free_window_min": int(round(largest_free)),
        "daily_stress": round(_clamp(stress), 3),
        "calendar_friction_score": round(_clamp(calendar_friction), 3),
    }


def _parse_json(raw: Any, fallback: Any) -> Any:
    if raw in (None, ""):
        return fallback
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(str(raw))
    except Exception:
        return fallback


def _invoke_next_session(day_iso: str) -> dict[str, Any]:
    try:
        with current_app.test_request_context(f"/api/next_session?day={day_iso}&explain=1"):
            response = current_app.view_functions["api_next_session"]()
        if isinstance(response, tuple):
            response = response[0]
        if hasattr(response, "get_json"):
            payload = response.get_json(silent=True)
            if isinstance(payload, dict):
                return payload
    except Exception:
        return {}
    return {}


def _extract_meta_from_next_session(payload: dict[str, Any]) -> dict[str, Any]:
    session = payload.get("session") if isinstance(payload.get("session"), dict) else {}
    meta = session.get("meta") if isinstance(session.get("meta"), dict) else {}
    decision_meta = meta.get("decision_meta") if isinstance(meta.get("decision_meta"), dict) else {}
    core_overlay = decision_meta.get("core_overlay") if isinstance(decision_meta.get("core_overlay"), dict) else {}
    trace = payload.get("autopilot_explain", {}).get("trace") if isinstance(payload.get("autopilot_explain"), dict) else {}
    runtime_inputs = trace.get("runtime_inputs") if isinstance(trace, dict) and isinstance(trace.get("runtime_inputs"), dict) else {}
    return {
        "meta": meta,
        "decision_meta": decision_meta,
        "core_overlay": core_overlay,
        "runtime_inputs": runtime_inputs,
    }


def _build_recovery_snapshot(day_iso: str) -> dict[str, Any]:
    conn = get_hrv_db()
    conn.row_factory = sqlite3.Row
    try:
        today_row = conn.execute(
            """
            SELECT *
            FROM hrv_measurements
            WHERE substr(date_utc, 1, 10) <= ?
            ORDER BY date_utc DESC
            LIMIT 1
            """,
            (day_iso,),
        ).fetchone()
        rmssd_7 = conn.execute(
            "SELECT AVG(rmssd) AS v FROM hrv_measurements WHERE substr(date_utc, 1, 10) BETWEEN date(?, '-6 day') AND ?",
            (day_iso, day_iso),
        ).fetchone()
        rmssd_28 = conn.execute(
            "SELECT AVG(rmssd) AS v FROM hrv_measurements WHERE substr(date_utc, 1, 10) BETWEEN date(?, '-27 day') AND ?",
            (day_iso, day_iso),
        ).fetchone()
        hr_7 = conn.execute(
            "SELECT AVG(hr) AS v FROM hrv_measurements WHERE substr(date_utc, 1, 10) BETWEEN date(?, '-6 day') AND ?",
            (day_iso, day_iso),
        ).fetchone()
        hr_28 = conn.execute(
            "SELECT AVG(hr) AS v FROM hrv_measurements WHERE substr(date_utc, 1, 10) BETWEEN date(?, '-27 day') AND ?",
            (day_iso, day_iso),
        ).fetchone()
        sleep_28 = conn.execute(
            "SELECT AVG(sleep_quality) AS v FROM hrv_measurements WHERE substr(date_utc, 1, 10) BETWEEN date(?, '-27 day') AND ? AND sleep_quality IS NOT NULL",
            (day_iso, day_iso),
        ).fetchone()
        motivation_28 = conn.execute(
            "SELECT AVG(training_motivation) AS v FROM hrv_measurements WHERE substr(date_utc, 1, 10) BETWEEN date(?, '-27 day') AND ? AND training_motivation IS NOT NULL",
            (day_iso, day_iso),
        ).fetchone()
        fatigue_28 = conn.execute(
            "SELECT AVG(fatigue) AS v FROM hrv_measurements WHERE substr(date_utc, 1, 10) BETWEEN date(?, '-27 day') AND ? AND fatigue IS NOT NULL",
            (day_iso, day_iso),
        ).fetchone()
        wake_rows = conn.execute(
            """
            SELECT ts_measurement
            FROM hrv_measurements
            WHERE substr(date_utc, 1, 10) BETWEEN date(?, '-27 day') AND ?
              AND ts_measurement IS NOT NULL
              AND ts_measurement <> ''
            """,
            (day_iso, day_iso),
        ).fetchall()
    except Exception:
        return {"hrv_pressure": 0.0, "missing_extreme": False}
    finally:
        conn.close()

    rmssd_today = _safe_float(today_row["rmssd"], 0.0) if today_row else 0.0
    hr_today = _safe_float(today_row["hr"], 0.0) if today_row else 0.0
    rmssd_base = _safe_float(rmssd_28["v"] if rmssd_28 else 0.0, 0.0)
    hr_base = _safe_float(hr_28["v"] if hr_28 else 0.0, 0.0)

    hrv_ratio = rmssd_today / rmssd_base if rmssd_today > 0 and rmssd_base > 0 else 1.0
    rhr_ratio = hr_today / hr_base if hr_today > 0 and hr_base > 0 else 1.0
    pressure = _clamp(max(0.0, (1.0 - hrv_ratio) * 1.18) + max(0.0, (rhr_ratio - 1.0) * 1.35))

    wake_minutes: list[int] = []
    for row in wake_rows or []:
        raw = str(row["ts_measurement"] or "").strip()
        if not raw:
            continue
        hhmm = ""
        if "T" in raw and len(raw) >= 16:
            hhmm = raw[11:16]
        else:
            parts = raw.split(" ")
            if len(parts) >= 2 and len(parts[1]) >= 5:
                hhmm = parts[1][:5]
        if not hhmm:
            continue
        mins = _parse_hhmm_to_min(hhmm)
        if mins is not None:
            wake_minutes.append(mins)
    wake_avg_hhmm = None
    if wake_minutes:
        avg_min = int(round(sum(wake_minutes) / len(wake_minutes)))
        avg_min = max(0, min((24 * 60) - 1, avg_min))
        wake_avg_hhmm = f"{avg_min // 60:02d}:{avg_min % 60:02d}"

    return {
        "rmssd_today": rmssd_today or None,
        "hr_today": hr_today or None,
        "rmssd_7d": _safe_float(rmssd_7["v"] if rmssd_7 else 0.0, 0.0) or None,
        "rmssd_28d": rmssd_base or None,
        "hr_7d": _safe_float(hr_7["v"] if hr_7 else 0.0, 0.0) or None,
        "hr_28d": hr_base or None,
        "hrv_ratio": round(hrv_ratio, 3),
        "rhr_ratio": round(rhr_ratio, 3),
        "hrv_pressure": round(pressure, 3),
        "missing_extreme": bool(rmssd_today <= 0.0 and hr_today <= 0.0 and rmssd_base <= 0.0 and hr_base <= 0.0),
        "sick_flag": bool(_safe_int(today_row["sickness_bool"] if today_row else 0, 0)),
        "sleep_quality_today": _safe_float(today_row["sleep_quality"], 0.0) if today_row and today_row["sleep_quality"] is not None else None,
        "sleep_quality_28d": _safe_float(sleep_28["v"] if sleep_28 else 0.0, 0.0) or None,
        "fatigue_today": _safe_float(today_row["fatigue"], 0.0) if today_row and today_row["fatigue"] is not None else None,
        "fatigue_28d": _safe_float(fatigue_28["v"] if fatigue_28 else 0.0, 0.0) or None,
        "motivation_today": _safe_float(today_row["training_motivation"], 0.0) if today_row and today_row["training_motivation"] is not None else None,
        "motivation_28d": _safe_float(motivation_28["v"] if motivation_28 else 0.0, 0.0) or None,
        "measurement_time": str(today_row["ts_measurement"] or "") if today_row and today_row["ts_measurement"] is not None else "",
        "measurement_time_28d": wake_avg_hhmm,
    }


def _build_training_snapshot(day_iso: str) -> dict[str, Any]:
    conn = get_training_db()
    conn.row_factory = sqlite3.Row
    try:
        count_14 = conn.execute(
            "SELECT COUNT(DISTINCT date_iso) AS days, COUNT(*) AS workouts FROM workouts WHERE date_iso BETWEEN date(?, '-13 day') AND ?",
            (day_iso, day_iso),
        ).fetchone()
        sets_7 = conn.execute(
            """
            SELECT COUNT(*) AS sets, AVG(COALESCE(s.rpe, 0)) AS avg_rpe
            FROM workouts w
            LEFT JOIN sets s ON s.workout_id = w.id
            WHERE w.date_iso BETWEEN date(?, '-6 day') AND ?
            """,
            (day_iso, day_iso),
        ).fetchone()
        last_day = conn.execute("SELECT MAX(date_iso) AS d FROM workouts WHERE date_iso <= ?", (day_iso,)).fetchone()
        last3_load_rows = conn.execute(
            """
            SELECT w.date_iso AS d, AVG(COALESCE(s.rpe, 0)) AS avg_rpe
            FROM workouts w
            LEFT JOIN sets s ON s.workout_id = w.id
            WHERE w.date_iso BETWEEN date(?, '-2 day') AND ?
            GROUP BY w.date_iso
            ORDER BY w.date_iso DESC
            """,
            (day_iso, day_iso),
        ).fetchall()
        all_stats = conn.execute(
            """
            SELECT
                COUNT(*) AS workouts_total,
                COUNT(DISTINCT date_iso) AS days_total,
                MIN(date_iso) AS first_day
            FROM workouts
            WHERE date_iso <= ?
            """,
            (day_iso,),
        ).fetchone()
    except Exception:
        return {"back_to_back_risk": 0.0, "history_sessions": 0, "history_days": 0, "heavy_days_3": 0}
    finally:
        conn.close()

    last_workout = str(last_day["d"] or "") if last_day else ""
    days_since_last_workout = 7
    if last_workout:
        try:
            days_since_last_workout = max(0, (date.fromisoformat(day_iso) - date.fromisoformat(last_workout)).days)
        except Exception:
            days_since_last_workout = 7

    workout_days = _safe_int(count_14["days"] if count_14 else 0, 0)
    avg_rpe = _safe_float(sets_7["avg_rpe"] if sets_7 else 0.0, 0.0)
    heavy_days_3 = 0
    for row in last3_load_rows or []:
        if _safe_float(row["avg_rpe"], 0.0) >= 8.0:
            heavy_days_3 += 1
    # Back-to-back must reflect acute stacked load.
    # Older weekly density can add context, but should decay quickly after rest days.
    if days_since_last_workout <= 0:
        rest_decay = 1.0
    elif days_since_last_workout == 1:
        rest_decay = 0.75
    elif days_since_last_workout == 2:
        rest_decay = 0.35
    elif days_since_last_workout == 3:
        rest_decay = 0.15
    else:
        rest_decay = 0.05

    acute_stack = 0.46 if days_since_last_workout <= 1 else 0.0
    density_stack = 0.0
    if avg_rpe >= 8.1:
        density_stack += 0.24
    if workout_days >= 6:
        density_stack += 0.22
    if heavy_days_3 >= 2:
        density_stack += 0.2

    back_to_back = acute_stack + (density_stack * rest_decay)

    history_days = 0
    first_day = str(all_stats["first_day"] or "") if all_stats else ""
    if first_day:
        try:
            history_days = max(1, (date.fromisoformat(day_iso) - date.fromisoformat(first_day)).days + 1)
        except Exception:
            history_days = _safe_int((all_stats["days_total"] if all_stats else 0), 0)
    else:
        history_days = _safe_int((all_stats["days_total"] if all_stats else 0), 0)

    return {
        "workout_days_14d": workout_days,
        "sets_7d": _safe_int(sets_7["sets"] if sets_7 else 0, 0),
        "avg_rpe_7d": round(avg_rpe, 2),
        "days_since_last_workout": days_since_last_workout,
        "heavy_days_3": heavy_days_3,
        "back_to_back_risk": round(_clamp(back_to_back), 3),
        "history_sessions": _safe_int((all_stats["workouts_total"] if all_stats else 0), 0),
        "history_days": history_days,
    }


def _build_run_snapshot(day_iso: str) -> dict[str, Any]:
    conn = get_runs_db()
    conn.row_factory = sqlite3.Row
    try:
        run_7 = conn.execute(
            "SELECT COUNT(*) AS runs, SUM(distance) AS km, AVG(avg_hr) AS avg_hr FROM runs WHERE substr(date,1,10) BETWEEN date(?, '-6 day') AND ?",
            (day_iso, day_iso),
        ).fetchone()
        run_3 = conn.execute(
            "SELECT COUNT(*) AS runs FROM runs WHERE substr(date,1,10) BETWEEN date(?, '-2 day') AND ?",
            (day_iso, day_iso),
        ).fetchone()
    except Exception:
        return {"interference_risk": 0.0}
    finally:
        conn.close()

    runs7 = _safe_int(run_7["runs"] if run_7 else 0, 0)
    runs3 = _safe_int(run_3["runs"] if run_3 else 0, 0)
    km7 = _safe_float(run_7["km"] if run_7 else 0.0, 0.0)
    if km7 > 1000:
        km7 = km7 / 1000.0
    avg_hr = _safe_float(run_7["avg_hr"] if run_7 else 0.0, 0.0)

    risk = 0.0
    if runs3 >= 2:
        risk += 0.36
    if km7 >= 25:
        risk += 0.24
    if avg_hr >= 158:
        risk += 0.22
    return {
        "runs_7d": runs7,
        "km_7d": round(km7, 2),
        "avg_hr_7d": round(avg_hr, 1) if avg_hr > 0 else None,
        "interference_risk": round(_clamp(risk), 3),
    }


def _build_nutrition_snapshot(day_iso: str) -> dict[str, Any]:
    conn = get_nutrition_db()
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT date_iso, kcal, protein
            FROM nutrition_daily
            WHERE date_iso BETWEEN date(?, '-6 day') AND ?
            ORDER BY date_iso DESC
            """,
            (day_iso, day_iso),
        ).fetchall()
        settings_rows = conn.execute(
            "SELECT key, value FROM nutrition_settings WHERE key IN ('active_mode', 'selected_mode', 'target_kcal', 'target_maintenance') OR key LIKE 'mode_%_target'"
        ).fetchall()
    except Exception:
        return {
            "deficit_risk": 0.25,
            "coverage_7d": 0.0,
            "target_kcal": None,
            "delta_vs_target": None,
            "energy_support_score": 0.52,
            "compliance_score": 0.42,
            "stability_score": 0.45,
            "reliability_score": 0.42,
        }
    finally:
        conn.close()

    logged_days = sum(1 for row in rows if row["kcal"] is not None)
    kcals = [_safe_float(row["kcal"], 0.0) for row in rows if row["kcal"] is not None and _safe_float(row["kcal"], 0.0) > 0]
    proteins = [_safe_float(row["protein"], 0.0) for row in rows if row["protein"] is not None and _safe_float(row["protein"], 0.0) > 0]
    avg_kcal = sum(kcals) / len(kcals) if kcals else 0.0
    avg_protein = sum(proteins) / len(proteins) if proteins else 0.0
    coverage = (logged_days / 7.0) if rows else 0.0

    settings = {str(row["key"] or ""): row["value"] for row in (settings_rows or [])}
    active_mode = str(settings.get("active_mode") or settings.get("selected_mode") or "").strip().lower()
    target = None
    if active_mode:
        target = _safe_float(settings.get(f"mode_{active_mode}_target"), 0.0) or None
    if target is None:
        target = _safe_float(settings.get("target_kcal"), 0.0) or None
    if target is None and active_mode == "maintenance":
        target = _safe_float(settings.get("target_maintenance"), 0.0) or None
    delta_vs_target = (avg_kcal - target) if avg_kcal and target else None

    kcal_var = 0.0
    if len(kcals) >= 2 and avg_kcal > 0:
        kcal_var = (sum((v - avg_kcal) ** 2 for v in kcals) / len(kcals)) ** 0.5 / avg_kcal
    protein_var = 0.0
    if len(proteins) >= 2 and avg_protein > 0:
        protein_var = (sum((v - avg_protein) ** 2 for v in proteins) / len(proteins)) ** 0.5 / avg_protein

    if target and avg_kcal > 0:
        target_ratio = avg_kcal / max(1.0, target)
        energy_support = _clamp(
            1.0
            - max(0.0, 0.92 - target_ratio) * 1.45
            - max(0.0, 0.78 - target_ratio) * 0.95
        )
        compliance = _clamp(1.0 - min(1.0, abs(avg_kcal - target) / max(260.0, target * 0.24)))
    elif avg_kcal > 0:
        energy_support = _clamp(0.55 + min(0.35, (avg_kcal - 1750.0) / 2300.0))
        compliance = _clamp(0.40 + (coverage * 0.45))
    else:
        energy_support = 0.42
        compliance = _clamp(coverage * 0.65)

    stability = _clamp(
        1.0
        - min(1.0, (kcal_var / 0.24) * 0.72 + (protein_var / 0.32) * 0.28)
    )
    reliability = _clamp((coverage * 0.56) + (stability * 0.30) + (compliance * 0.14))
    energy_support = _clamp((energy_support * 0.78) + (coverage * 0.22))
    deficit_risk = _clamp(((1.0 - energy_support) * 0.74) + ((1.0 - reliability) * 0.26))

    return {
        "coverage_7d": round(_clamp(coverage), 3),
        "avg_kcal_7d": round(avg_kcal, 1) if avg_kcal else None,
        "avg_protein_7d": round(avg_protein, 1) if avg_protein else None,
        "deficit_risk": round(_clamp(deficit_risk), 3),
        "target_kcal": round(target, 1) if target else None,
        "delta_vs_target": round(delta_vs_target, 1) if delta_vs_target is not None else None,
        "energy_support_score": round(energy_support, 3),
        "compliance_score": round(compliance, 3),
        "stability_score": round(stability, 3),
        "reliability_score": round(reliability, 3),
    }


def _weight_snapshot(day_iso: str) -> dict[str, Any]:
    conn = get_nutrition_db()
    conn.row_factory = sqlite3.Row
    try:
        latest = conn.execute(
            """
            SELECT date_iso, weight_kg
            FROM weight_logs
            WHERE date_iso <= ? AND weight_kg IS NOT NULL
            ORDER BY date_iso DESC
            LIMIT 1
            """,
            (day_iso,),
        ).fetchone()
        base_7 = conn.execute(
            """
            SELECT date_iso, weight_kg
            FROM weight_logs
            WHERE date_iso <= date(?, '-7 day') AND weight_kg IS NOT NULL
            ORDER BY date_iso DESC
            LIMIT 1
            """,
            (day_iso,),
        ).fetchone()
        base_3 = conn.execute(
            """
            SELECT date_iso, weight_kg
            FROM weight_logs
            WHERE date_iso <= date(?, '-3 day') AND weight_kg IS NOT NULL
            ORDER BY date_iso DESC
            LIMIT 1
            """,
            (day_iso,),
        ).fetchone()
    except Exception:
        return {"weight_kg": None, "date_iso": "", "delta_7d": None, "delta_3d": None}
    finally:
        conn.close()

    if not latest:
        return {"weight_kg": None, "date_iso": "", "delta_7d": None, "delta_3d": None}

    latest_kg = _safe_float(latest["weight_kg"], 0.0) or None
    base_7_kg = _safe_float(base_7["weight_kg"], 0.0) if base_7 and base_7["weight_kg"] is not None else None
    base_3_kg = _safe_float(base_3["weight_kg"], 0.0) if base_3 and base_3["weight_kg"] is not None else None
    delta_7d = (latest_kg - base_7_kg) if latest_kg is not None and base_7_kg is not None else None
    delta_3d = (latest_kg - base_3_kg) if latest_kg is not None and base_3_kg is not None else None

    return {
        "weight_kg": latest_kg,
        "date_iso": str(latest["date_iso"] or ""),
        "delta_7d": round(delta_7d, 2) if delta_7d is not None else None,
        "delta_3d": round(delta_3d, 2) if delta_3d is not None else None,
    }


def _build_calendar_stress_snapshot(day_iso: str) -> dict[str, Any]:
    out: dict[str, Any] = {
        "source": "calendar_free_time",
        "calendar_available": False,
        "entries_count": 0,
        "busy_minutes_today": None,
        "free_minutes_today": None,
        "free_ratio_today": None,
        "largest_free_window_min": None,
        "school_start": None,
        "school_end": None,
        "daily_stress": None,
        "calendar_friction_score": None,
    }
    try:
        day = date.fromisoformat(day_iso)
    except Exception:
        return out

    def _format_hhmm(minutes: int | None) -> str | None:
        if minutes is None:
            return None
        m = max(0, min((24 * 60) - 1, int(minutes)))
        return f"{m // 60:02d}:{m % 60:02d}"

    def _google_snapshot() -> dict[str, Any] | None:
        try:
            from gcalsync.config import load_config
            from gcalsync.service import build_service, list_events
        except Exception:
            return None

        try:
            cfg = load_config()
            tz_name = str(getattr(cfg, "timezone", "Europe/Berlin") or "Europe/Berlin")
            tz = ZoneInfo(tz_name)
            service = build_service(cfg.credentials_file, cfg.token_file)
            day_start = datetime.combine(day, dt_time(0, 0), tzinfo=tz)
            day_end = day_start + timedelta(days=1)

            calendar_ids: list[str] = []
            for attr in [
                "default_calendar_id",
                "school_calendar_id",
                "work_calendar_id",
                "training_calendar_id",
                "football_calendar_id",
            ]:
                cid = str(getattr(cfg, attr, "") or "").strip()
                if cid and cid not in calendar_ids:
                    calendar_ids.append(cid)

            # Optional explicit IDs (e.g., family calendar) via env.
            extra_tokens: list[str] = []
            for env_key in [
                "GOOGLE_CALENDAR_FAMILY_ID",
                "GOOGLE_CALENDAR_FAMILY_IDS",
                "GOOGLE_CALENDAR_EXTRA_IDS",
                "CORE_CALENDAR_STRESS_IDS",
            ]:
                raw = str(os.getenv(env_key, "") or "").strip()
                if not raw:
                    continue
                extra_tokens.extend([part.strip() for part in raw.split(",") if part.strip()])
            for cid in extra_tokens:
                if cid not in calendar_ids:
                    calendar_ids.append(cid)

            # Auto-include selected calendars from Google calendar list.
            try:
                page_token = None
                while True:
                    resp = service.calendarList().list(pageToken=page_token, maxResults=250).execute()
                    for entry in resp.get("items", []) or []:
                        if not isinstance(entry, dict):
                            continue
                        cid = str(entry.get("id") or "").strip()
                        if not cid:
                            continue
                        selected = bool(entry.get("selected", True))
                        hidden = bool(entry.get("hidden", False))
                        if not selected or hidden:
                            continue
                        if cid not in calendar_ids:
                            calendar_ids.append(cid)
                    page_token = resp.get("nextPageToken")
                    if not page_token:
                        break
            except Exception:
                pass

            if not calendar_ids:
                return None

            raw_events: list[dict[str, Any]] = []
            for calendar_id in calendar_ids:
                try:
                    rows = list_events(
                        service,
                        calendar_id=calendar_id,
                        time_min=day_start.isoformat(),
                        time_max=day_end.isoformat(),
                        max_results=250,
                    )
                except Exception:
                    continue
                for row in rows or []:
                    if isinstance(row, dict):
                        raw_events.append(row)

            seen: set[str] = set()
            events: list[dict[str, Any]] = []
            for row in raw_events:
                start = row.get("start") if isinstance(row.get("start"), dict) else {}
                end = row.get("end") if isinstance(row.get("end"), dict) else {}
                sig = "|".join(
                    [
                        str(row.get("id") or "").strip(),
                        str(start.get("dateTime") or start.get("date") or "").strip(),
                        str(end.get("dateTime") or end.get("date") or "").strip(),
                        str(row.get("summary") or "").strip(),
                    ]
                )
                if sig in seen:
                    continue
                seen.add(sig)
                events.append(row)

            intervals: list[tuple[int, int]] = []
            span_start: int | None = None
            span_end: int | None = None
            stress_entries_count = 0
            for row in events:
                if _calendar_event_is_all_day(row):
                    # All-day blocks are real calendar entries, but they should not
                    # count as scheduled daytime load for CORE stress.
                    continue
                start = row.get("start") if isinstance(row.get("start"), dict) else {}
                end = row.get("end") if isinstance(row.get("end"), dict) else {}
                st_date = str(start.get("date") or "").strip()
                en_date = str(end.get("date") or "").strip()
                st_dt_raw = start.get("dateTime")
                en_dt_raw = end.get("dateTime")

                start_min: int | None = None
                end_min: int | None = None

                if st_date and en_date:
                    try:
                        start_day = date.fromisoformat(st_date)
                        end_day = date.fromisoformat(en_date)
                        if start_day <= day < end_day:
                            start_min = 0
                            end_min = 24 * 60
                    except Exception:
                        pass
                else:
                    st_dt = _parse_iso_dt(st_dt_raw)
                    en_dt = _parse_iso_dt(en_dt_raw)
                    if st_dt is None:
                        continue
                    if en_dt is None:
                        en_dt = st_dt + timedelta(hours=1)
                    st_local = st_dt.astimezone(tz)
                    en_local = en_dt.astimezone(tz)
                    clip_start = max(st_local, day_start)
                    clip_end = min(en_local, day_end)
                    if clip_end > clip_start:
                        start_min = int((clip_start - day_start).total_seconds() // 60)
                        end_min = int((clip_end - day_start).total_seconds() // 60)

                if start_min is None or end_min is None or end_min <= start_min:
                    continue
                intervals.append((start_min, end_min))
                stress_entries_count += 1
                span_start = start_min if span_start is None else min(span_start, start_min)
                span_end = end_min if span_end is None else max(span_end, end_min)

            stats = _compute_day_stress_from_intervals(intervals=intervals, entries_count=stress_entries_count)
            return {
                "source": "google_calendar",
                "calendar_available": True,
                "entries_count": stress_entries_count,
                "school_start": _format_hhmm(span_start),
                "school_end": _format_hhmm(span_end),
                **stats,
            }
        except Exception:
            return None

    def _schoolsync_snapshot() -> dict[str, Any] | None:
        path = Path("/opt/liva/var/schoolsync/latest_today.json")
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None

        entries = payload.get("entries_visible") if isinstance(payload.get("entries_visible"), list) else payload.get("entries")
        rows = [row for row in (entries or []) if isinstance(row, dict) and str(row.get("date") or "").strip() == day_iso]
        rows = [row for row in rows if str(row.get("status_hint") or "").strip().lower() not in {"cancelled", "eva"}]

        intervals: list[tuple[int, int]] = []
        for row in rows:
            st = _parse_hhmm_to_min(row.get("start_time"))
            en = _parse_hhmm_to_min(row.get("end_time"))
            if st is None or en is None or en <= st:
                continue
            intervals.append((st, en))

        span_start = min((st for st, _ in intervals), default=None)
        span_end = max((en for _, en in intervals), default=None)
        stats = _compute_day_stress_from_intervals(intervals=intervals, entries_count=len(rows))
        return {
            "source": "schoolsync_latest_json",
            "calendar_available": True,
            "entries_count": len(rows),
            "school_start": _format_hhmm(span_start),
            "school_end": _format_hhmm(span_end),
            **stats,
        }

    google = _google_snapshot()
    if isinstance(google, dict) and google.get("calendar_available"):
        out.update(google)
        return out

    fallback = _schoolsync_snapshot()
    if isinstance(fallback, dict) and fallback.get("calendar_available"):
        out.update(fallback)
        return out

    return out


def _build_today_signals(day_iso: str, state: dict[str, Any]) -> dict[str, Any]:
    recovery = state.get("recovery") if isinstance(state.get("recovery"), dict) else {}
    fatigue = state.get("fatigue") if isinstance(state.get("fatigue"), dict) else {}
    training = state.get("load") if isinstance(state.get("load"), dict) else {}
    run_ctx = state.get("run_context") if isinstance(state.get("run_context"), dict) else {}
    nutrition = state.get("nutrition") if isinstance(state.get("nutrition"), dict) else {}
    stress = state.get("stress") if isinstance(state.get("stress"), dict) else {}
    illness = state.get("illness") if isinstance(state.get("illness"), dict) else {}
    weight = _weight_snapshot(day_iso)

    def _status_from_cut(value: float | None, *, good_at_or_below: float | None = None, good_at_or_above: float | None = None, mid_at_or_below: float | None = None, mid_at_or_above: float | None = None) -> str:
        if value is None:
            return "missing"
        v = _safe_float(value, 0.0)
        if good_at_or_below is not None and v <= good_at_or_below:
            return "good"
        if good_at_or_above is not None and v >= good_at_or_above:
            return "good"
        if mid_at_or_below is not None and v <= mid_at_or_below:
            return "mid"
        if mid_at_or_above is not None and v >= mid_at_or_above:
            return "mid"
        return "risk"

    def _item(key: str, label: str, value: str | None, note: str | None, status: str) -> dict[str, str]:
        if not value:
            return {"key": key, "label": label, "value": "nicht geloggt", "note": "heute offen", "status": "missing"}
        return {"key": key, "label": label, "value": value, "note": str(note or ""), "status": status}

    hrv_today = recovery.get("rmssd_today")
    hrv_ratio = recovery.get("hrv_ratio")
    hrv_avg = recovery.get("rmssd_28d")
    rhr_today = recovery.get("hr_today")
    rhr_ratio = recovery.get("rhr_ratio")
    rhr_avg = recovery.get("hr_28d")
    sleep_quality = recovery.get("sleep_quality_today")
    sleep_quality_avg = recovery.get("sleep_quality_28d")
    motivation_today = recovery.get("motivation_today")
    motivation_avg = recovery.get("motivation_28d")
    fatigue_today = recovery.get("fatigue_today")
    fatigue_avg = recovery.get("fatigue_28d")
    wake_today_raw = recovery.get("measurement_time")
    wake_avg = str(recovery.get("measurement_time_28d") or "").strip()

    muscular_preload = _safe_float(fatigue.get("muscular_preload"), 0.0)
    systemic_extreme = bool(_safe_bool(fatigue.get("systemic_extreme")))

    heavy_days_3 = _safe_int(training.get("heavy_days_3"), 0)
    avg_rpe_7d = training.get("avg_rpe_7d")
    back_to_back_risk = _safe_float(training.get("back_to_back_risk"), 0.0)

    kcal_7d = nutrition.get("avg_kcal_7d")
    kcal_target = nutrition.get("target_kcal")
    kcal_delta = nutrition.get("delta_vs_target")

    run_km_7d = _safe_float(run_ctx.get("km_7d"), 0.0)
    run_interference = _safe_float(run_ctx.get("interference_risk"), 0.0)

    stress_today = stress.get("daily_stress")
    busy_minutes_today = stress.get("busy_minutes_today")

    sick = bool(_safe_bool(illness.get("sick")))

    def _hhmm(ts: Any) -> str:
        text = str(ts or "").strip()
        if not text:
            return ""
        try:
            if "T" in text:
                return text[11:16]
            parts = text.split(" ")
            if len(parts) >= 2 and len(parts[1]) >= 5:
                return parts[1][:5]
        except Exception:
            return ""
        return ""

    def _fmt_num(value: Any, digits: int = 1) -> str:
        return f"{_safe_float(value, 0.0):.{digits}f}"

    def _fmt_delta(value: Any, digits: int = 1) -> str:
        v = _safe_float(value, 0.0)
        return f"{'+' if v >= 0 else ''}{v:.{digits}f}"

    def _avg_or_today(value: Any, *, digits: int = 1, prefix: str = "Ø", suffix: str = "") -> str:
        if value is None:
            return "heute gemessen"
        return f"{prefix}{_fmt_num(value, digits)}{suffix}"

    wake_today = _hhmm(wake_today_raw)
    wake_avg_note = f"Ø{wake_avg}" if wake_avg else "heute gemessen"
    if wake_today and wake_avg:
        wt = _parse_hhmm_to_min(wake_today)
        wa = _parse_hhmm_to_min(wake_avg)
        if wt is not None and wa is not None:
            if wt <= wa - 10:
                wake_avg_note = f"Ø{wake_avg} · früher als üblich"
            elif wt >= wa + 10:
                wake_avg_note = f"Ø{wake_avg} · später als üblich"

    weight_kg = weight.get("weight_kg")
    weight_delta_7d = weight.get("delta_7d")
    weight_note = f"Δ7d {_fmt_delta(weight_delta_7d, 1)}" if weight_delta_7d is not None else "heute gemessen"
    if weight_delta_7d is not None:
        if weight_delta_7d >= 0.25:
            weight_note = f"{weight_note} · leichter Aufbau"
        elif abs(weight_delta_7d) <= 0.15:
            weight_note = f"{weight_note} · Trend stabil"

    kcal_note = "heute gemessen"
    if kcal_delta is not None and kcal_target is not None:
        kcal_note = f"{_fmt_delta(kcal_delta, 0)} von Ziel · Ø Ziel {round(_safe_float(kcal_target, 0.0))} kcal"
    energy_support = _safe_float(nutrition.get("energy_support_score"), 1.0 - _safe_float(nutrition.get("deficit_risk"), 0.0))
    reliability = _safe_float(nutrition.get("reliability_score"), _safe_float(nutrition.get("coverage_7d"), 0.0))
    kcal_note = f"{kcal_note} · Verlässlichkeit {round(reliability * 100)}%"

    run_note = "kein Laufdruck" if run_km_7d <= 0.05 else (
        "Interferenz hoch" if run_interference >= 0.58 else ("Interferenz moderat" if run_interference >= 0.3 else "Interferenz leicht")
    )

    cal_note = "heute offen"
    if busy_minutes_today is not None:
        busy_h = _safe_float(busy_minutes_today, 0.0) / 60.0
        if abs(busy_h - round(busy_h)) < 0.05:
            busy_h_text = f"{int(round(busy_h))} h"
        else:
            busy_h_text = f"{busy_h:.1f} h"
        cal_note = (
            f"{busy_h_text} verplant · moderater Kalenderdruck"
            if _safe_float(stress_today, 0.0) < 0.45
            else f"{busy_h_text} verplant · hoher Kalenderdruck"
        )

    b2b_note = f"Belastungsdichte {'erhöht' if back_to_back_risk >= 0.35 else 'kontrolliert'} · {heavy_days_3} von 3 Tagen hart"

    energy_today = None
    energy_avg = None
    if fatigue_today is not None:
        energy_today = _clamp(10.0 - _safe_float(fatigue_today, 0.0), 0.0, 10.0)
    if fatigue_avg is not None:
        energy_avg = _clamp(10.0 - _safe_float(fatigue_avg, 0.0), 0.0, 10.0)

    items = [
        _item(
            "hrv",
            "HRV",
            f"{_fmt_num(hrv_today, 1)} ms" if hrv_today is not None else None,
            f"{_avg_or_today(hrv_avg, digits=1)}{' · über Schnitt' if hrv_today is not None and hrv_avg is not None and _safe_float(hrv_today) >= _safe_float(hrv_avg) else (' · unter Schnitt' if hrv_today is not None and hrv_avg is not None else '')}",
            _status_from_cut(_safe_float(hrv_ratio, 0.0) if hrv_ratio is not None else None, good_at_or_above=0.98, mid_at_or_above=0.92),
        ),
        _item(
            "rhr",
            "RHR",
            f"{_fmt_num(rhr_today, 1)} bpm" if rhr_today is not None else None,
            f"{_avg_or_today(rhr_avg, digits=1)}{' · unter Schnitt' if rhr_today is not None and rhr_avg is not None and _safe_float(rhr_today) <= _safe_float(rhr_avg) else (' · über Schnitt' if rhr_today is not None and rhr_avg is not None else '')}",
            _status_from_cut(_safe_float(rhr_ratio, 0.0) if rhr_ratio is not None else None, good_at_or_below=1.02, mid_at_or_below=1.06),
        ),
        _item(
            "sleep_quality",
            "SCHLAFQUALITÄT",
            f"{_fmt_num(sleep_quality, 1)} / 10" if sleep_quality is not None else None,
            _avg_or_today(sleep_quality_avg, digits=1),
            _status_from_cut(sleep_quality, good_at_or_above=7.0, mid_at_or_above=5.0),
        ),
        _item(
            "measurement_time",
            "AUFSTEHEN",
            wake_today or None,
            wake_avg_note,
            "neutral",
        ),
        _item(
            "energy",
            "ENERGIE",
            f"{_fmt_num(energy_today, 1)} / 10" if energy_today is not None else None,
            _avg_or_today(energy_avg, digits=1),
            _status_from_cut(energy_today, good_at_or_above=7.0, mid_at_or_above=5.0),
        ),
        _item(
            "motivation",
            "MOTIVATION",
            f"{_fmt_num(motivation_today, 1)} / 10" if motivation_today is not None else None,
            _avg_or_today(motivation_avg, digits=1),
            _status_from_cut(motivation_today, good_at_or_above=7.0, mid_at_or_above=5.0),
        ),
        _item(
            "weight",
            "GEWICHT",
            f"{_fmt_num(weight_kg, 1)} kg" if weight_kg is not None else None,
            weight_note,
            "neutral",
        ),
        _item(
            "muscular_preload",
            "MUSKEL-VORERMÜDUNG",
            f"{round(muscular_preload * 100)} %",
            "keine Vorermüdung" if muscular_preload <= 0.02 else "lokale Müdigkeit",
            _status_from_cut(muscular_preload, good_at_or_below=0.2, mid_at_or_below=0.45),
        ),
        _item(
            "load_density",
            "LETZTER LOAD",
            f"{heavy_days_3}/3 hart",
            f"ØRPE 7d {_fmt_num(avg_rpe_7d, 1)}" if avg_rpe_7d is not None else "ØRPE 7d offen",
            "risk" if heavy_days_3 >= 2 else ("mid" if heavy_days_3 == 1 else "good"),
        ),
        _item(
            "back_to_back_risk",
            "MEHRERE HARTE TAGE",
            f"{round(back_to_back_risk * 100)} %",
            b2b_note,
            _status_from_cut(back_to_back_risk, good_at_or_below=0.22, mid_at_or_below=0.45),
        ),
        _item(
            "nutrition",
            "KALORIEN 7D",
            f"{round(_safe_float(kcal_7d, 0.0))} kcal" if kcal_7d is not None else None,
            kcal_note,
            _status_from_cut(1.0 - energy_support, good_at_or_below=0.3, mid_at_or_below=0.55),
        ),
        _item(
            "run_load",
            "LAUFBELASTUNG",
            f"{_fmt_num(run_km_7d, 1)} km",
            run_note,
            _status_from_cut(run_interference, good_at_or_below=0.3, mid_at_or_below=0.58),
        ),
        _item(
            "calendar_stress",
            "KALENDERSTRESS",
            f"{round(_safe_float(stress_today, 0.0) * 100)} %" if stress_today is not None else None,
            cal_note,
            _status_from_cut(stress_today, good_at_or_below=0.28, mid_at_or_below=0.5),
        ),
        _item(
            "systemic_extreme",
            "SYSTEMERSCHÖPFUNG",
            "Warnzeichen" if systemic_extreme else "unauffällig",
            "Stoppzeichen vorhanden" if systemic_extreme else "keine Stoppzeichen",
            "risk" if systemic_extreme else "good",
        ),
        _item(
            "illness",
            "INFEKTSTATUS",
            "Warnzeichen" if sick else "unauffällig",
            "Warnzeichen aktiv" if sick else "keine Warnzeichen",
            "risk" if sick else "good",
        ),
    ]

    present = [row for row in items if str(row.get("status") or "") != "missing"]
    missing = [row for row in items if str(row.get("status") or "") == "missing"]

    # Show all currently known decision-relevant signals first.
    # If data is sparse, append missing markers to keep full transparency.
    selected: list[dict[str, str]] = list(present)
    if len(selected) < len(items):
        selected.extend(missing)

    return {
        "summary": f"Alle {len(selected)} Signale fließen heute in CORE ein.",
        "items": selected,
    }


def _state_from_runtime_inputs(runtime: dict[str, Any]) -> dict[str, Any]:
    recovery = runtime.get("recovery") if isinstance(runtime.get("recovery"), dict) else {}
    flags = runtime.get("flags") if isinstance(runtime.get("flags"), dict) else {}
    subjective = runtime.get("subjective") if isinstance(runtime.get("subjective"), dict) else {}
    metrics = runtime.get("metrics") if isinstance(runtime.get("metrics"), dict) else {}

    sleep_debt_score = 0.0
    if str(subjective.get("fatigue_state") or "").strip().lower() == "high":
        sleep_debt_score += 0.38
    if str(subjective.get("readiness_state") or "").strip().lower() == "low":
        sleep_debt_score += 0.34
    sleep_debt_score = _clamp(sleep_debt_score)

    readiness_pressure = 0.0
    if str(subjective.get("readiness_state") or "").strip().lower() == "low":
        readiness_pressure += 0.45
    if str(subjective.get("fatigue_state") or "").strip().lower() == "high":
        readiness_pressure += 0.32
    if bool(flags.get("zns_fatigue")):
        readiness_pressure += 0.22

    muscular_preload = 0.0
    local = flags.get("local_fatigue") if isinstance(flags.get("local_fatigue"), dict) else {}
    if bool(local.get("legs")):
        muscular_preload += 0.32
    if bool(local.get("push")):
        muscular_preload += 0.23
    if bool(local.get("pull")):
        muscular_preload += 0.22

    stress_score = 0.0
    override_count = _safe_int(metrics.get("override_count_7d"), 0)
    if override_count >= 3:
        stress_score += 0.24
    if _safe_int(metrics.get("skipped_7d"), 0) >= 2:
        stress_score += 0.22

    return {
        "recovery": {
            "hrv_pressure": _clamp(max(0.0, (1.0 - _safe_float(recovery.get("hrv_ratio"), 1.0)) * 1.2) + max(0.0, (_safe_float(recovery.get("rhr_ratio"), 1.0) - 1.0) * 1.25)),
            "missing_extreme": bool(_safe_bool(flags.get("recovery_missing")) and _safe_bool(flags.get("zns_fatigue"))),
        },
        "readiness": {
            "sleep_debt_score": round(sleep_debt_score, 3),
            "readiness_pressure": round(_clamp(readiness_pressure), 3),
            "trainability_score": round(_clamp(1.0 - readiness_pressure), 3),
        },
        "fatigue": {
            "muscular_preload": round(_clamp(muscular_preload), 3),
            "systemic_extreme": bool(_safe_bool(flags.get("zns_fatigue")) and _safe_bool(flags.get("hi_yesterday"))),
        },
        "stress": {
            "daily_stress": round(_clamp(stress_score), 3),
        },
        "illness": {
            "sick": bool(_safe_bool(flags.get("sick"))),
            "infection_acute": bool(_safe_bool(flags.get("sick"))),
        },
    }


def _load_latest_clone_snapshot(conn: sqlite3.Connection, day_iso: str) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT *
        FROM core_clone_snapshots
        WHERE date <= ?
        ORDER BY date DESC, id DESC
        LIMIT 1
        """,
        (day_iso,),
    ).fetchone()
    if not row:
        return None
    return {
        "id": _safe_int(row["id"]),
        "date": str(row["date"] or ""),
        "clone_version": str(row["clone_version"] or "clone-v1"),
        "base_state": _parse_json(row["base_state_json"], {}),
        "sensitivities": _parse_json(row["sensitivities_json"], {}),
        "learned_patterns": _parse_json(row["learned_patterns_json"], []),
        "summary": _parse_json(row["summary_json"], {}),
    }


def _mode_from_raw_json(raw_json: Any) -> str:
    raw = _parse_json(raw_json, {})
    session_payload = raw.get("session_payload") if isinstance(raw.get("session_payload"), dict) else {}
    session = session_payload.get("session") if isinstance(session_payload.get("session"), dict) else {}
    meta = session.get("meta") if isinstance(session.get("meta"), dict) else {}
    mode = str(meta.get("autopilot_mode_effective") or meta.get("autopilot_mode_suggested") or "").strip().upper()
    if mode == "PUSH":
        return MODE_HEAVY
    if mode in {MODE_HEAVY, MODE_NORMAL, MODE_LIGHT, MODE_REST}:
        return mode
    return MODE_NORMAL


def _load_recent_decisions(conn: sqlite3.Connection, day_iso: str, lookback_days: int = 84) -> list[dict[str, Any]]:
    if lookback_days and lookback_days > 0:
        start, end = rolling_window_range(day_iso, days=lookback_days)
        rows = conn.execute(
            """
            SELECT id, day_iso, raw_json
            FROM core_decision_log
            WHERE day_iso BETWEEN ? AND ?
              AND source IN ('live_today', 'plateau_scan')
            ORDER BY day_iso DESC
            """,
            (start, end),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT id, day_iso, raw_json
            FROM core_decision_log
            WHERE day_iso <= ?
              AND source IN ('live_today', 'plateau_scan')
            ORDER BY day_iso DESC
            """,
            (day_iso,),
        ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        out.append({"id": _safe_int(row["id"]), "day_iso": str(row["day_iso"] or ""), "mode": _mode_from_raw_json(row["raw_json"])})
    return out


def _load_latest_reviews_by_decision(conn: sqlite3.Connection, day_iso: str, lookback_days: int = 84) -> dict[int, dict[str, Any]]:
    if lookback_days and lookback_days > 0:
        start, end = rolling_window_range(day_iso, days=lookback_days)
        rows = conn.execute(
            """
            WITH latest_reviews AS (
                SELECT r.*
                FROM core_decision_reviews r
                JOIN (
                    SELECT decision_id, MAX(review_date) AS review_date
                    FROM core_decision_reviews
                    GROUP BY decision_id
                ) x
                  ON x.decision_id = r.decision_id
                 AND x.review_date = r.review_date
            )
            SELECT *
            FROM latest_reviews
            WHERE review_date BETWEEN ? AND ?
            """,
            (start, end),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            WITH latest_reviews AS (
                SELECT r.*
                FROM core_decision_reviews r
                JOIN (
                    SELECT decision_id, MAX(review_date) AS review_date
                    FROM core_decision_reviews
                    WHERE review_date <= ?
                    GROUP BY decision_id
                ) x
                  ON x.decision_id = r.decision_id
                 AND x.review_date = r.review_date
            )
            SELECT *
            FROM latest_reviews
            WHERE review_date <= ?
            """,
            (day_iso, day_iso),
        ).fetchall()
    return {int(row["decision_id"]): dict(row) for row in rows if row["decision_id"] is not None}


def _store_clone_snapshot(conn: sqlite3.Connection, *, day_iso: str, clone_profile: dict[str, Any]) -> int:
    conn.execute(
        """
        INSERT INTO core_clone_snapshots (
            date, created_at, clone_version, base_state_json,
            sensitivities_json, learned_patterns_json, summary_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(date) DO UPDATE SET
            created_at=excluded.created_at,
            clone_version=excluded.clone_version,
            base_state_json=excluded.base_state_json,
            sensitivities_json=excluded.sensitivities_json,
            learned_patterns_json=excluded.learned_patterns_json,
            summary_json=excluded.summary_json
        """,
        (
            day_iso,
            _utc_now(),
            str(clone_profile.get("version") or "clone-v1"),
            json.dumps(clone_profile.get("base_state") or {}, ensure_ascii=False),
            json.dumps(clone_profile.get("sensitivities") or {}, ensure_ascii=False),
            json.dumps(clone_profile.get("learned_patterns") or [], ensure_ascii=False),
            json.dumps(clone_profile.get("summary") or {}, ensure_ascii=False),
        ),
    )
    row = conn.execute("SELECT id FROM core_clone_snapshots WHERE date=? LIMIT 1", (day_iso,)).fetchone()
    return _safe_int(row["id"] if row else 0, 0)


def _store_simulation_runs(
    conn: sqlite3.Connection,
    *,
    day_iso: str,
    state: dict[str, Any],
    simulations: list[dict[str, Any]],
    chosen_scenario_key: str,
) -> None:
    conn.execute("DELETE FROM core_simulation_runs WHERE date=?", (day_iso,))
    for row in simulations:
        conn.execute(
            """
            INSERT INTO core_simulation_runs (
                date, created_at, scenario_key, scenario_type,
                input_context_json, predicted_outcome_json, chosen_by_core,
                score, explanation_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                day_iso,
                _utc_now(),
                str(row.get("scenario_key") or ""),
                str(row.get("scenario_type") or ""),
                json.dumps(state or {}, ensure_ascii=False),
                json.dumps(row or {}, ensure_ascii=False),
                1 if str(row.get("scenario_key") or "") == chosen_scenario_key else 0,
                _safe_float(row.get("score"), 0.0),
                json.dumps({"summary": row.get("summary"), "top_drivers": row.get("top_drivers") or []}, ensure_ascii=False),
            ),
        )


def _float_or_none(value: Any) -> float | None:
    try:
        v = float(value)
    except Exception:
        return None
    if v != v:
        return None
    return float(v)


def _extract_prediction_from_payload(payload: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    decision = payload.get("decision") if isinstance(payload.get("decision"), dict) else {}
    night_cycle = payload.get("night_cycle") if isinstance(payload.get("night_cycle"), dict) else {}
    future_lab = night_cycle.get("future_lab") if isinstance(night_cycle.get("future_lab"), dict) else {}

    selected_key = str(decision.get("selected_scenario_key") or "")
    selected_mode = str(decision.get("mode") or "")

    projection = future_lab.get("scenario_projection") if isinstance(future_lab.get("scenario_projection"), list) else []
    selected_projection: dict[str, Any] | None = None
    first_projection: dict[str, Any] | None = None
    for row in projection:
        if not isinstance(row, dict):
            continue
        estimate = row.get("tomorrow_estimate") if isinstance(row.get("tomorrow_estimate"), dict) else {}
        if not estimate:
            continue
        entry = {
            "scenario_key": str(row.get("scenario_key") or ""),
            "label": str(row.get("label") or ""),
            "selected": bool(row.get("selected")),
            "readiness_tomorrow_pct": _float_or_none(estimate.get("readiness_tomorrow_pct")),
            "rmssd_tomorrow_ms": _float_or_none(estimate.get("rmssd_tomorrow_ms")),
            "rhr_tomorrow_bpm": _float_or_none(estimate.get("rhr_tomorrow_bpm")),
            "fatigue_tomorrow_10": _float_or_none(estimate.get("fatigue_tomorrow_10")),
        }
        if first_projection is None:
            first_projection = entry
        if selected_key and entry["scenario_key"] == selected_key:
            selected_projection = entry
            break
        if bool(row.get("selected")):
            selected_projection = entry
        if not selected_projection and selected_mode and str(entry.get("label") or "").upper().startswith(selected_mode.upper()):
            selected_projection = entry

    predicted = selected_projection or first_projection
    if not predicted:
        morgen_status = future_lab.get("morgen_status") if isinstance(future_lab.get("morgen_status"), dict) else {}
        if not morgen_status:
            return None
        predicted = {
            "scenario_key": selected_key,
            "label": selected_mode,
            "selected": True,
            "readiness_tomorrow_pct": _float_or_none(morgen_status.get("readiness_tomorrow_pct")),
            "rmssd_tomorrow_ms": _float_or_none(morgen_status.get("rmssd_tomorrow_ms")),
            "rhr_tomorrow_bpm": _float_or_none(morgen_status.get("rhr_tomorrow_bpm")),
            "fatigue_tomorrow_10": _float_or_none(morgen_status.get("fatigue_tomorrow_10")),
        }

    driver_impact: dict[str, float] = {}
    for item in (future_lab.get("today_signal_estimates") if isinstance(future_lab.get("today_signal_estimates"), list) else []):
        if not isinstance(item, dict):
            continue
        key = str(item.get("key") or "").strip()
        if not key:
            continue
        impact = _float_or_none(item.get("impact"))
        if impact is None:
            continue
        driver_impact[key] = round(impact, 4)

    return {
        "selected_mode": selected_mode or str(predicted.get("label") or ""),
        "selected_scenario_key": str(predicted.get("scenario_key") or selected_key),
        "predicted": predicted,
        "driver_impact": driver_impact,
    }


def _load_prediction_for_actual_day(conn: sqlite3.Connection, day_iso: str) -> dict[str, Any] | None:
    actual_day = date.fromisoformat(day_iso)
    expected_prediction_day = (actual_day - timedelta(days=1)).isoformat()

    row = conn.execute(
        """
        SELECT date, payload_json
        FROM core_decision_explain_v2
        WHERE date = ?
        LIMIT 1
        """,
        (expected_prediction_day,),
    ).fetchone()

    if not row:
        row = conn.execute(
            """
            SELECT date, payload_json
            FROM core_decision_explain_v2
            WHERE date < ?
            ORDER BY date DESC
            LIMIT 1
            """,
            (day_iso,),
        ).fetchone()
        if not row:
            return None
        prediction_day = str(row["date"] or "")
        try:
            prediction_date = date.fromisoformat(prediction_day)
        except Exception:
            return None
        if (actual_day - prediction_date).days > 3:
            return None
    payload = _parse_json(row["payload_json"], {})
    extracted = _extract_prediction_from_payload(payload)
    if not extracted:
        return None
    return {
        "prediction_date": str(row["date"] or ""),
        "selected_mode": str(extracted.get("selected_mode") or ""),
        "selected_scenario_key": str(extracted.get("selected_scenario_key") or ""),
        "predicted": extracted.get("predicted") if isinstance(extracted.get("predicted"), dict) else {},
        "driver_impact": extracted.get("driver_impact") if isinstance(extracted.get("driver_impact"), dict) else {},
    }


def _actual_morning_snapshot(state: dict[str, Any]) -> dict[str, Any]:
    recovery = state.get("recovery") if isinstance(state.get("recovery"), dict) else {}
    readiness = state.get("readiness") if isinstance(state.get("readiness"), dict) else {}
    stress = state.get("stress") if isinstance(state.get("stress"), dict) else {}

    rmssd_today = _float_or_none(recovery.get("rmssd_today"))
    rmssd_base = _float_or_none(recovery.get("rmssd_28d"))
    rhr_today = _float_or_none(recovery.get("hr_today"))
    rhr_base = _float_or_none(recovery.get("hr_28d"))

    fatigue_today = _float_or_none(recovery.get("fatigue_today"))
    fatigue_source = "measured"
    if fatigue_today is None:
        fatigue_source = "derived"
        fatigue_today = round(
            _clamp(
                2.6 + (_safe_float(readiness.get("readiness_pressure"), 0.0) * 5.2) + (_safe_float(stress.get("daily_stress"), 0.0) * 1.6),
                1.0,
                10.0,
            ),
            1,
        )

    readiness_today = 72.0
    trainability_score = _float_or_none(readiness.get("trainability_score"))
    if trainability_score is not None:
        readiness_today = 58.0 + (_clamp(trainability_score, 0.0, 1.0) * 42.0)
    if rmssd_today and rmssd_base and rmssd_base > 0:
        readiness_today += ((rmssd_today / rmssd_base) - 1.0) * 22.0
    if rhr_today and rhr_base and rhr_base > 0:
        readiness_today -= ((rhr_today / rhr_base) - 1.0) * 20.0
    if fatigue_today is not None:
        readiness_today -= max(0.0, fatigue_today - 5.0) * 4.0
    readiness_today -= _safe_float(stress.get("daily_stress"), 0.0) * 10.0
    readiness_today = round(_clamp(readiness_today, 0.0, 100.0), 1)

    return {
        "readiness_today_pct": readiness_today,
        "rmssd_today_ms": round(rmssd_today, 1) if rmssd_today is not None else None,
        "rhr_today_bpm": round(rhr_today, 1) if rhr_today is not None else None,
        "fatigue_today_10": round(_clamp(fatigue_today, 0.0, 10.0), 1) if fatigue_today is not None else None,
        "source": {
            "fatigue": fatigue_source,
            "readiness": "composite",
        },
    }


def _simulation_error(
    *,
    predicted: dict[str, Any],
    actual: dict[str, Any],
) -> tuple[dict[str, Any], float, str]:
    pred_readiness = _float_or_none(predicted.get("readiness_tomorrow_pct"))
    pred_rmssd = _float_or_none(predicted.get("rmssd_tomorrow_ms"))
    pred_rhr = _float_or_none(predicted.get("rhr_tomorrow_bpm"))
    pred_fatigue = _float_or_none(predicted.get("fatigue_tomorrow_10"))

    act_readiness = _float_or_none(actual.get("readiness_today_pct"))
    act_rmssd = _float_or_none(actual.get("rmssd_today_ms"))
    act_rhr = _float_or_none(actual.get("rhr_today_bpm"))
    act_fatigue = _float_or_none(actual.get("fatigue_today_10"))

    err: dict[str, Any] = {}
    weighted_terms: list[tuple[float, float]] = []

    if pred_readiness is not None and act_readiness is not None:
        delta = pred_readiness - act_readiness
        abs_delta = abs(delta)
        err["readiness_delta_pct"] = round(delta, 2)
        err["readiness_abs_error_pct"] = round(abs_delta, 2)
        weighted_terms.append((min(1.0, abs_delta / 24.0), 0.4))

    if pred_rmssd is not None and act_rmssd is not None:
        delta = pred_rmssd - act_rmssd
        abs_delta = abs(delta)
        err["rmssd_delta_ms"] = round(delta, 2)
        err["rmssd_abs_error_ms"] = round(abs_delta, 2)
        weighted_terms.append((min(1.0, abs_delta / 12.0), 0.27))

    if pred_rhr is not None and act_rhr is not None:
        delta = pred_rhr - act_rhr
        abs_delta = abs(delta)
        err["rhr_delta_bpm"] = round(delta, 2)
        err["rhr_abs_error_bpm"] = round(abs_delta, 2)
        weighted_terms.append((min(1.0, abs_delta / 8.0), 0.2))

    if pred_fatigue is not None and act_fatigue is not None:
        delta = pred_fatigue - act_fatigue
        abs_delta = abs(delta)
        err["fatigue_delta_10"] = round(delta, 2)
        err["fatigue_abs_error_10"] = round(abs_delta, 2)
        weighted_terms.append((min(1.0, abs_delta / 3.0), 0.13))

    if weighted_terms:
        total_weight = sum(weight for _, weight in weighted_terms) or 1.0
        overall = round(sum(value * weight for value, weight in weighted_terms) / total_weight, 3)
    else:
        overall = 0.0

    if overall <= 0.23:
        quality = "good"
    elif overall <= 0.45:
        quality = "fair"
    elif overall <= 0.65:
        quality = "weak"
    else:
        quality = "bad"

    err["metrics_covered"] = len(weighted_terms)
    return err, overall, quality


def _store_simulation_feedback_for_day(
    conn: sqlite3.Connection,
    *,
    day_iso: str,
    state: dict[str, Any],
) -> dict[str, Any] | None:
    prediction = _load_prediction_for_actual_day(conn, day_iso)
    if not prediction:
        return None

    predicted = prediction.get("predicted") if isinstance(prediction.get("predicted"), dict) else {}
    if not predicted:
        return None

    actual = _actual_morning_snapshot(state)
    error_json, overall_error, quality_label = _simulation_error(predicted=predicted, actual=actual)

    if _safe_int(error_json.get("metrics_covered"), 0) <= 0:
        return None

    prediction_date = str(prediction.get("prediction_date") or "")
    selected_mode = str(prediction.get("selected_mode") or "")
    selected_scenario_key = str(prediction.get("selected_scenario_key") or "")
    driver_impact = prediction.get("driver_impact") if isinstance(prediction.get("driver_impact"), dict) else {}

    conn.execute(
        """
        INSERT INTO core_simulation_feedback (
            prediction_date, actual_date, created_at, selected_mode, selected_scenario_key,
            predicted_json, actual_json, error_json, overall_error, quality_label, driver_impact_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(prediction_date, actual_date) DO UPDATE SET
            created_at=excluded.created_at,
            selected_mode=excluded.selected_mode,
            selected_scenario_key=excluded.selected_scenario_key,
            predicted_json=excluded.predicted_json,
            actual_json=excluded.actual_json,
            error_json=excluded.error_json,
            overall_error=excluded.overall_error,
            quality_label=excluded.quality_label,
            driver_impact_json=excluded.driver_impact_json
        """,
        (
            prediction_date,
            day_iso,
            _utc_now(),
            selected_mode,
            selected_scenario_key,
            json.dumps(predicted, ensure_ascii=False),
            json.dumps(actual, ensure_ascii=False),
            json.dumps(error_json, ensure_ascii=False),
            overall_error,
            quality_label,
            json.dumps(driver_impact, ensure_ascii=False),
        ),
    )
    return {
        "prediction_date": prediction_date,
        "actual_date": day_iso,
        "selected_mode": selected_mode,
        "selected_scenario_key": selected_scenario_key,
        "overall_error": overall_error,
        "quality_label": quality_label,
        "predicted": predicted,
        "actual": actual,
        "error": error_json,
    }


def _latest_simulation_feedback(conn: sqlite3.Connection, *, day_iso: str) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT *
        FROM core_simulation_feedback
        WHERE actual_date <= ?
        ORDER BY actual_date DESC, id DESC
        LIMIT 1
        """,
        (day_iso,),
    ).fetchone()
    if not row:
        return None
    return {
        "prediction_date": str(row["prediction_date"] or ""),
        "actual_date": str(row["actual_date"] or ""),
        "selected_mode": str(row["selected_mode"] or ""),
        "selected_scenario_key": str(row["selected_scenario_key"] or ""),
        "overall_error": _safe_float(row["overall_error"], 0.0),
        "quality_label": str(row["quality_label"] or "inconclusive"),
        "predicted": _parse_json(row["predicted_json"], {}),
        "actual": _parse_json(row["actual_json"], {}),
        "error": _parse_json(row["error_json"], {}),
    }


def _simulation_feedback_summary(conn: sqlite3.Connection, *, day_iso: str, days: int = 60) -> dict[str, Any]:
    since = (date.fromisoformat(day_iso) - timedelta(days=max(14, int(days)))).isoformat()
    rows = conn.execute(
        """
        SELECT overall_error, quality_label, error_json
        FROM core_simulation_feedback
        WHERE actual_date BETWEEN ? AND ?
        ORDER BY actual_date DESC
        """,
        (since, day_iso),
    ).fetchall()
    if not rows:
        return {
            "window_days": days,
            "samples": 0,
            "mean_overall_error": None,
            "quality_mix": {"good": 0, "fair": 0, "weak": 0, "bad": 0},
            "hit_count": 0,
            "uncertain_count": 0,
            "missed_count": 0,
            "mean_abs_readiness_error": None,
            "mean_abs_rmssd_error_ms": None,
            "mean_abs_rhr_error_bpm": None,
            "mean_abs_fatigue_error_10": None,
            "quality_bars": [],
        }

    deltas_ready: list[float] = []
    deltas_rmssd: list[float] = []
    deltas_rhr: list[float] = []
    deltas_fatigue: list[float] = []
    parsed_rows: list[dict[str, Any]] = []
    for row in rows:
        err = _parse_json(row["error_json"], {})
        parsed_rows.append(
            {
                "overall_error": _safe_float(row["overall_error"], 0.0),
                "quality_label": str(row["quality_label"] or "").strip().lower(),
                "err": err,
            }
        )
        if err.get("readiness_delta_pct") is not None:
            deltas_ready.append(_safe_float(err.get("readiness_delta_pct"), 0.0))
        if err.get("rmssd_delta_ms") is not None:
            deltas_rmssd.append(_safe_float(err.get("rmssd_delta_ms"), 0.0))
        if err.get("rhr_delta_bpm") is not None:
            deltas_rhr.append(_safe_float(err.get("rhr_delta_bpm"), 0.0))
        if err.get("fatigue_delta_10") is not None:
            deltas_fatigue.append(_safe_float(err.get("fatigue_delta_10"), 0.0))

    def _mean_or_zero(vals: list[float], min_samples: int = 8) -> float:
        if len(vals) < min_samples:
            return 0.0
        return sum(vals) / max(1, len(vals))

    # Systematische Fehlkalibrierung kompensieren (pred-actual Bias),
    # damit die Präzision nicht durch einen konstanten Offset kollabiert.
    bias_ready = _mean_or_zero(deltas_ready)
    bias_rmssd = _mean_or_zero(deltas_rmssd)
    bias_rhr = _mean_or_zero(deltas_rhr)
    bias_fatigue = _mean_or_zero(deltas_fatigue)

    quality_mix = {"good": 0, "fair": 0, "weak": 0, "bad": 0}
    overall_vals: list[float] = []
    ready_vals: list[float] = []
    rmssd_vals: list[float] = []
    rhr_vals: list[float] = []
    fatigue_vals: list[float] = []
    quality_bars: list[dict[str, Any]] = []
    for idx, row in enumerate(parsed_rows):
        err = row["err"] if isinstance(row["err"], dict) else {}
        weighted_terms: list[tuple[float, float]] = []

        ready_delta = _float_or_none(err.get("readiness_delta_pct"))
        if ready_delta is not None:
            ready_abs = abs(ready_delta - bias_ready)
            ready_vals.append(ready_abs)
            weighted_terms.append((min(1.0, ready_abs / 24.0), 0.4))

        rmssd_delta = _float_or_none(err.get("rmssd_delta_ms"))
        if rmssd_delta is not None:
            rmssd_abs = abs(rmssd_delta - bias_rmssd)
            rmssd_vals.append(rmssd_abs)
            weighted_terms.append((min(1.0, rmssd_abs / 12.0), 0.27))

        rhr_delta = _float_or_none(err.get("rhr_delta_bpm"))
        if rhr_delta is not None:
            rhr_abs = abs(rhr_delta - bias_rhr)
            rhr_vals.append(rhr_abs)
            weighted_terms.append((min(1.0, rhr_abs / 8.0), 0.2))

        fatigue_delta = _float_or_none(err.get("fatigue_delta_10"))
        if fatigue_delta is not None:
            fatigue_abs = abs(fatigue_delta - bias_fatigue)
            fatigue_vals.append(fatigue_abs)
            weighted_terms.append((min(1.0, fatigue_abs / 3.0), 0.13))

        if weighted_terms:
            total_weight = sum(weight for _, weight in weighted_terms) or 1.0
            overall = sum(value * weight for value, weight in weighted_terms) / total_weight
        else:
            overall = _safe_float(row.get("overall_error"), 0.0)
        overall = round(overall, 3)
        overall_vals.append(overall)

        if overall <= 0.23:
            quality = "good"
        elif overall <= 0.45:
            quality = "fair"
        elif overall <= 0.65:
            quality = "weak"
        else:
            quality = "bad"
        quality_mix[quality] += 1

        if idx < 14:
            quality_bars.append(
                {
                    "quality": quality,
                    "overall_error": overall,
                }
            )

    n = max(1, len(rows))
    hit_count = quality_mix.get("good", 0) + quality_mix.get("fair", 0)
    uncertain_count = quality_mix.get("weak", 0)
    missed_count = quality_mix.get("bad", 0)
    return {
        "window_days": days,
        "samples": len(rows),
        "mean_overall_error": round(sum(overall_vals) / n, 3),
        "quality_mix": quality_mix,
        "hit_count": hit_count,
        "uncertain_count": uncertain_count,
        "missed_count": missed_count,
        "mean_abs_readiness_error": round(sum(ready_vals) / max(1, len(ready_vals)), 3) if ready_vals else None,
        "mean_abs_rmssd_error_ms": round(sum(rmssd_vals) / max(1, len(rmssd_vals)), 3) if rmssd_vals else None,
        "mean_abs_rhr_error_bpm": round(sum(rhr_vals) / max(1, len(rhr_vals)), 3) if rhr_vals else None,
        "mean_abs_fatigue_error_10": round(sum(fatigue_vals) / max(1, len(fatigue_vals)), 3) if fatigue_vals else None,
        "quality_bars": quality_bars,
        "bias_correction": {
            "readiness_delta_pct": round(bias_ready, 3),
            "rmssd_delta_ms": round(bias_rmssd, 3),
            "rhr_delta_bpm": round(bias_rhr, 3),
            "fatigue_delta_10": round(bias_fatigue, 3),
        },
    }




def _compose_model_precision(
    *,
    feedback_14d: dict[str, Any],
    feedback_60d: dict[str, Any],
    quality_14d: dict[str, Any],
    clone_trust: float,
) -> dict[str, Any]:
    sim14_samples = _safe_int(feedback_14d.get("samples"), 0)
    sim14_hit = _safe_float(feedback_14d.get("hit_count"), 0.0)
    sim14_uncertain = _safe_float(feedback_14d.get("uncertain_count"), 0.0)
    sim14_rate = (sim14_hit + (sim14_uncertain * 0.5)) / max(1, sim14_samples) if sim14_samples > 0 else None

    prior_rate = _safe_float(quality_14d.get("accuracy"), -1.0)
    if prior_rate < 0:
        mean_overall_60 = feedback_60d.get("mean_overall_error")
        if mean_overall_60 is not None:
            prior_rate = _clamp(1.0 - _safe_float(mean_overall_60, 0.5))
        else:
            prior_rate = _clamp(clone_trust)
    prior_total = _safe_int(quality_14d.get("total"), 0)
    prior_strength = min(40.0, max(8.0, prior_total * 0.35))

    if sim14_rate is not None and sim14_samples >= 8:
        blended_rate = _clamp(sim14_rate)
        effective_total = sim14_samples
        source = "simulation_14d"
        uncertain_count = _safe_int(feedback_14d.get("uncertain_count"), 0)
        missed_count = _safe_int(feedback_14d.get("missed_count"), 0)
        hit_count = _safe_int(feedback_14d.get("hit_count"), 0)
    else:
        sim_component = (sim14_rate * sim14_samples) if sim14_rate is not None else 0.0
        blended_rate = _clamp((sim_component + (prior_rate * prior_strength)) / max(1.0, sim14_samples + prior_strength))
        effective_total = max(sim14_samples, int(round(min(14.0, sim14_samples + (prior_strength * 0.6)))))
        source = "blended_history"

        too_conservative = _safe_int(quality_14d.get("too_conservative"), 0)
        too_aggressive = _safe_int(quality_14d.get("too_aggressive"), 0)
        quality_total = max(1, _safe_int(quality_14d.get("total"), 0))
        prior_uncertain_ratio = _clamp(((too_conservative + too_aggressive) / quality_total) * 0.45 + 0.12, 0.08, 0.34)
        uncertain_count = int(round(effective_total * prior_uncertain_ratio))
        hit_count = int(round(effective_total * blended_rate))
        if hit_count + uncertain_count > effective_total:
            uncertain_count = max(0, effective_total - hit_count)
        missed_count = max(0, effective_total - hit_count - uncertain_count)

    rmssd_error = feedback_14d.get("mean_abs_rmssd_error_ms")
    if rmssd_error is None:
        rmssd_error = feedback_60d.get("mean_abs_rmssd_error_ms")
    if rmssd_error is None:
        rmssd_error = max(1.6, (1.0 - _clamp(clone_trust)) * 9.0)

    bars = feedback_14d.get("quality_bars") if isinstance(feedback_14d.get("quality_bars"), list) else []
    if not bars:
        bars = feedback_60d.get("quality_bars") if isinstance(feedback_60d.get("quality_bars"), list) else []

    return {
        "hit_rate_14d": round(blended_rate, 3),
        "window_days": 14,
        "rmssd_error_ms": round(_safe_float(rmssd_error, 4.0), 1),
        "predictions_total": max(0, int(effective_total)),
        "predictions_hit": max(0, int(hit_count)),
        "predictions_uncertain": max(0, int(uncertain_count)),
        "predictions_missed": max(0, int(missed_count)),
        "quality_bars": bars[:14] if isinstance(bars, list) else [],
        "source": source,
    }


def _build_forecast_adaptation_profile(
    *,
    feedback_14d: dict[str, Any],
    feedback_60d: dict[str, Any],
    calibration_profile: dict[str, Any],
) -> dict[str, Any]:
    samples_14 = _safe_int(feedback_14d.get("samples"), 0)
    source = feedback_14d if samples_14 >= 8 else feedback_60d
    source_name = "feedback_14d" if source is feedback_14d else "feedback_60d"
    samples = _safe_int(source.get("samples"), 0)
    bias = source.get("bias_correction") if isinstance(source.get("bias_correction"), dict) else {}

    reliability = _clamp(_safe_float(calibration_profile.get("model_reliability"), 0.5))
    sample_strength = _clamp((samples - 4.0) / 20.0)
    confidence_strength = _clamp((0.35 + (reliability * 0.65)) * sample_strength)

    # error_delta ist pred - actual. Für die nächste Prognose korrigieren wir
    # in Gegenrichtung (negative Bias -> Prognose anheben).
    readiness_corr = _clamp(-_safe_float(bias.get("readiness_delta_pct"), 0.0) * confidence_strength, -24.0, 24.0)
    rmssd_corr = _clamp(-_safe_float(bias.get("rmssd_delta_ms"), 0.0) * confidence_strength, -20.0, 20.0)
    rhr_corr = _clamp(-_safe_float(bias.get("rhr_delta_bpm"), 0.0) * confidence_strength, -8.0, 8.0)
    fatigue_corr = _clamp(-_safe_float(bias.get("fatigue_delta_10"), 0.0) * confidence_strength, -2.8, 2.8)

    return {
        "enabled": bool(samples >= 8 and confidence_strength >= 0.12),
        "source": source_name,
        "samples": samples,
        "strength": round(confidence_strength, 3),
        "readiness_pct_correction": round(readiness_corr, 2),
        "rmssd_ms_correction": round(rmssd_corr, 2),
        "rhr_bpm_correction": round(rhr_corr, 2),
        "fatigue_10_correction": round(fatigue_corr, 2),
        "bias_raw": {
            "readiness_delta_pct": round(_safe_float(bias.get("readiness_delta_pct"), 0.0), 3),
            "rmssd_delta_ms": round(_safe_float(bias.get("rmssd_delta_ms"), 0.0), 3),
            "rhr_delta_bpm": round(_safe_float(bias.get("rhr_delta_bpm"), 0.0), 3),
            "fatigue_delta_10": round(_safe_float(bias.get("fatigue_delta_10"), 0.0), 3),
        },
    }


def _coverage_from_state_for_learning(state: dict[str, Any]) -> dict[str, float]:
    sources = state.get("sources") if isinstance(state.get("sources"), dict) else {}
    training_cov = 0.82 if bool(sources.get("training_db")) else 0.28
    run_cov = 0.82 if bool(sources.get("runs_db")) else 0.28
    recovery_cov = 0.86 if bool(sources.get("hrv_db")) else 0.24
    nutrition_cov = 0.82 if bool(sources.get("nutrition_db")) else 0.28
    return {
        "training": round(training_cov, 3),
        "run": round(run_cov, 3),
        "recovery": round(recovery_cov, 3),
        "nutrition": round(nutrition_cov, 3),
    }


def _simulation_calibration_profile(conn: sqlite3.Connection, *, day_iso: str, days: int = 60) -> dict[str, Any]:
    since = (date.fromisoformat(day_iso) - timedelta(days=max(14, int(days)))).isoformat()
    rows = conn.execute(
        """
        SELECT overall_error, quality_label, error_json
        FROM core_simulation_feedback
        WHERE actual_date BETWEEN ? AND ?
        ORDER BY actual_date DESC
        """,
        (since, day_iso),
    ).fetchall()
    if not rows:
        return {
            "samples": 0,
            "optimistic_ratio": 0.0,
            "pessimistic_ratio": 0.0,
            "high_error_ratio": 0.0,
            "calibration_shift": 0.0,
            "model_reliability": 0.5,
        }

    optimistic = 0
    pessimistic = 0
    high_error = 0
    overall_vals: list[float] = []
    for row in rows:
        err = _parse_json(row["error_json"], {})
        readiness_bias = _safe_float(err.get("readiness_delta_pct"), 0.0)
        if readiness_bias >= 8.0:
            optimistic += 1
        elif readiness_bias <= -8.0:
            pessimistic += 1
        overall = _safe_float(row["overall_error"], 0.0)
        overall_vals.append(overall)
        quality = str(row["quality_label"] or "").strip().lower()
        if overall >= 0.55 or quality in {"weak", "bad"}:
            high_error += 1

    n = max(1, len(rows))
    optimistic_ratio = optimistic / n
    pessimistic_ratio = pessimistic / n
    high_error_ratio = high_error / n
    mean_overall = sum(overall_vals) / n if overall_vals else 0.5
    reliability = _clamp(1.0 - mean_overall)

    # Positive shift => zuletzt zu optimistisch => konservativer kalibrieren.
    if n < 8:
        # In frühen Phasen nur vorsichtig in Richtung Sicherheit verschieben.
        shift = max(0.0, high_error_ratio - 0.2) * 0.22
    else:
        shift = (
            (optimistic_ratio - pessimistic_ratio) * 0.62
            + max(0.0, high_error_ratio - 0.24) * 0.55
        )
    shift = _clamp(shift, -0.35, 0.35)

    return {
        "samples": len(rows),
        "optimistic_ratio": round(optimistic_ratio, 3),
        "pessimistic_ratio": round(pessimistic_ratio, 3),
        "high_error_ratio": round(high_error_ratio, 3),
        "calibration_shift": round(shift, 3),
        "model_reliability": round(reliability, 3),
    }


def _apply_simulation_calibration(
    *,
    simulations: list[dict[str, Any]],
    calibration_profile: dict[str, Any],
) -> list[dict[str, Any]]:
    shift = _safe_float(calibration_profile.get("calibration_shift"), 0.0)
    if abs(shift) < 0.001:
        return simulations

    out: list[dict[str, Any]] = []
    for row in simulations:
        if not isinstance(row, dict):
            continue
        x = dict(row)
        perf_good = _clamp(_safe_float(x.get("performance_probability_good"), 0.0))
        perf_bad = _clamp(_safe_float(x.get("performance_probability_bad"), 0.0))
        recovery_risk = _clamp(_safe_float(x.get("recovery_risk"), 0.0))
        fatigue_risk = _clamp(_safe_float(x.get("fatigue_risk"), 0.0))
        hrv_drop_risk = _clamp(_safe_float(x.get("hrv_drop_risk"), 0.0))
        readiness_delta = _safe_float(x.get("readiness_tomorrow_delta_estimate"), 0.0)

        if shift > 0:
            perf_good = _clamp(perf_good - (shift * 0.08))
            perf_bad = _clamp(perf_bad + (shift * 0.12))
            recovery_risk = _clamp(recovery_risk + (shift * 0.09))
            fatigue_risk = _clamp(fatigue_risk + (shift * 0.08))
            readiness_delta = readiness_delta - (shift * 0.035)
        else:
            mag = abs(shift)
            perf_good = _clamp(perf_good + (mag * 0.04))
            perf_bad = _clamp(perf_bad - (mag * 0.06))
            recovery_risk = _clamp(recovery_risk - (mag * 0.03))
            fatigue_risk = _clamp(fatigue_risk - (mag * 0.02))
            readiness_delta = readiness_delta + (mag * 0.015)

        score = round((perf_good * 100.0) - (recovery_risk * 28.0) - (fatigue_risk * 24.0) - (hrv_drop_risk * 18.0) + (readiness_delta * 34.0), 2)
        x["performance_probability_good"] = round(perf_good, 3)
        x["performance_probability_bad"] = round(perf_bad, 3)
        x["recovery_risk"] = round(recovery_risk, 3)
        x["fatigue_risk"] = round(fatigue_risk, 3)
        x["readiness_tomorrow_delta_estimate"] = round(readiness_delta, 3)
        x["score"] = score
        out.append(x)
    out.sort(key=lambda row: _safe_float(row.get("score"), -999.0), reverse=True)
    return out


def _trace_steps(completed_at: str) -> list[dict[str, Any]]:
    return [
        {"key": "state_build", "label": "Daily State Build", "status": "done", "at": completed_at, "icon": "stack"},
        {"key": "clone_update", "label": "Clone Update", "status": "done", "at": completed_at, "icon": "dna"},
        {"key": "scenario_generation", "label": "Scenario Generation", "status": "done", "at": completed_at, "icon": "cards"},
        {"key": "scenario_simulation", "label": "Scenario Simulation", "status": "done", "at": completed_at, "icon": "pulse"},
        {"key": "heavy_first_decision", "label": "Heavy-First Decision", "status": "done", "at": completed_at, "icon": "ladder"},
        {"key": "explain_build", "label": "Explain Build", "status": "done", "at": completed_at, "icon": "layers"},
        {"key": "parameter_review", "label": "Parameter Review", "status": "done", "at": completed_at, "icon": "tune"},
    ]


def _build_decision_quality_summary(conn: sqlite3.Connection, day_iso: str, days: int = 14) -> dict[str, Any]:
    since = (date.fromisoformat(day_iso) - timedelta(days=max(7, days))).isoformat()
    rows = conn.execute(
        """
        SELECT outcome_label
        FROM core_decision_reviews
        WHERE review_date >= ?
        ORDER BY review_date DESC
        """,
        (since,),
    ).fetchall()
    total = len(rows)
    if total == 0:
        return {"window_days": days, "total": 0, "accuracy": None, "too_conservative": 0, "too_aggressive": 0}
    good = sum(1 for row in rows if str(row["outcome_label"] or "").lower() in {"good", "mixed"})
    too_conservative = sum(1 for row in rows if str(row["outcome_label"] or "").lower() == "too_conservative")
    too_aggressive = sum(1 for row in rows if str(row["outcome_label"] or "").lower() == "too_aggressive")
    return {
        "window_days": days,
        "total": total,
        "accuracy": round(good / total, 3),
        "too_conservative": too_conservative,
        "too_aggressive": too_aggressive,
    }


def _canonical_option_label(row: dict[str, Any]) -> str:
    gym_mode = str(row.get("gym_mode") or "").upper()
    run_plan = str(row.get("run_plan") or "NONE").upper()
    if gym_mode == MODE_REST:
        return MODE_REST
    if gym_mode == MODE_HEAVY and run_plan != "NONE":
        return "HEAVY + RUN"
    if gym_mode == MODE_NORMAL and run_plan != "NONE":
        return "NORMAL + RUN"
    if gym_mode == MODE_LIGHT and run_plan != "NONE":
        return "LIGHT + RUN"
    if gym_mode in {MODE_HEAVY, MODE_NORMAL, MODE_LIGHT}:
        return gym_mode
    return str(row.get("label") or row.get("scenario_key") or "OPTION").upper()


def _scenario_balance_score(row: dict[str, Any]) -> float:
    perf = _safe_float(row.get("performance_probability_good"), _safe_float(row.get("performance_score"), 0.0))
    recovery = _safe_float(row.get("recovery_risk"), 0.0)
    fatigue = _safe_float(row.get("fatigue_risk"), 0.0)
    bad = _safe_float(row.get("performance_probability_bad"), 0.0)
    return round((perf * 100.0) - (recovery * 38.0) - (fatigue * 34.0) - (bad * 18.0), 2)


def _dedupe_simulations(simulations: list[dict[str, Any]], chosen_scenario_key: str) -> list[dict[str, Any]]:
    buckets: dict[str, list[dict[str, Any]]] = {}
    for row in simulations or []:
        if not isinstance(row, dict):
            continue
        label = _canonical_option_label(row)
        buckets.setdefault(label, []).append(row)

    deduped: list[dict[str, Any]] = []
    for option_label, rows in buckets.items():
        selected = next((r for r in rows if str(r.get("scenario_key") or "") == chosen_scenario_key), None)
        representative = selected or max(rows, key=lambda x: _safe_float(x.get("score"), -9999.0))
        rep = dict(representative)
        rep["option_label"] = option_label
        rep["selected"] = bool(selected)
        rep["variants_count"] = len(rows)
        rep["variant_keys"] = [str(r.get("scenario_key") or "") for r in rows]
        rep["balance_score"] = _scenario_balance_score(rep)
        deduped.append(rep)

    deduped.sort(key=lambda row: _safe_float(row.get("score"), -9999.0), reverse=True)
    return deduped


def _pick_option_by_label(options: list[dict[str, Any]], label: str) -> dict[str, Any] | None:
    target = str(label or "").upper()
    for row in options or []:
        if str(row.get("option_label") or "").upper() == target:
            return row
    return None


def _build_option_anchors(options: list[dict[str, Any]], chosen_scenario_key: str) -> dict[str, Any]:
    if not options:
        return {"safest": None, "best_balance": None, "chosen": None, "highest_price": None}

    safest = min(options, key=lambda r: (_safe_float(r.get("recovery_risk"), 0.0) * 0.58) + (_safe_float(r.get("fatigue_risk"), 0.0) * 0.42))
    best_balance = max(options, key=lambda r: _safe_float(r.get("balance_score"), -9999.0))
    chosen = next((r for r in options if str(r.get("scenario_key") or "") == chosen_scenario_key), None)
    if not chosen:
        chosen = next((r for r in options if bool(r.get("selected"))), None)
    highest_price = min(options, key=lambda r: _safe_float(r.get("score"), 9999.0))
    return {"safest": safest, "best_balance": best_balance, "chosen": chosen, "highest_price": highest_price}


def _future_progression_score(row: dict[str, Any]) -> float:
    mode = str(row.get("option_label") or _canonical_option_label(row)).upper()
    base = {
        "REST": 8.0,
        "LIGHT": 52.0,
        "NORMAL": 72.0,
        "HEAVY": 90.0,
        "LIGHT + RUN": 60.0,
        "NORMAL + RUN": 80.0,
        "HEAVY + RUN": 96.0,
    }.get(mode, 64.0)
    perf = _safe_float(row.get("performance_probability_good"), _safe_float(row.get("performance_score"), 0.0))
    return round(base * (0.45 + (perf * 0.55)), 1)


def _future_cost_score(row: dict[str, Any]) -> float:
    recovery = _safe_float(row.get("recovery_risk"), 0.0)
    fatigue = _safe_float(row.get("fatigue_risk"), 0.0)
    hrv_drop = _safe_float(row.get("hrv_drop_risk"), 0.0)
    return round(((recovery * 0.44) + (fatigue * 0.34) + (hrv_drop * 0.22)) * 100.0, 1)


def _future_reserve_tomorrow(row: dict[str, Any]) -> int:
    readiness_delta = _safe_float(row.get("readiness_tomorrow_delta_estimate"), 0.0)
    recovery = _safe_float(row.get("recovery_risk"), 0.0)
    fatigue = _safe_float(row.get("fatigue_risk"), 0.0)
    reserve = 72.0 + (readiness_delta * 100.0) - (recovery * 35.0) - (fatigue * 22.0)
    return int(round(max(0.0, min(100.0, reserve))))


def _future_cost_level(score: float) -> str:
    if score >= 66:
        return "hoch"
    if score >= 40:
        return "moderat"
    return "niedrig"


def _future_anchor_tags(
    *,
    row: dict[str, Any],
    chosen_key: str,
    safest_key: str,
    best_balance_key: str,
    highest_reiz_key: str,
    highest_reserve_key: str,
    highest_price_key: str,
) -> list[str]:
    key = str(row.get("scenario_key") or "")
    tags: list[str] = []
    if key == chosen_key:
        tags.append("heute gewählt")
    if key == safest_key:
        tags.append("sicherste Zukunft")
    if key == best_balance_key:
        tags.append("beste Balance")
    if key == highest_reiz_key:
        tags.append("höchster Reiz heute")
    if key == highest_reserve_key:
        tags.append("höchste Reserve morgen")
    if key == highest_price_key:
        tags.append("höchster Preis")
    return tags


def _future_option_note(row: dict[str, Any], tags: list[str]) -> str:
    mode = str(row.get("option_label") or _canonical_option_label(row)).upper()
    if mode == "REST":
        return "maximale Erholung morgen, kein Trainingsreiz heute"
    if mode == "LIGHT":
        return "kontrollierbar, moderater Reiz bei reduzierten Folgekosten"
    if mode == "NORMAL":
        return "solide Balance aus Reiz heute und Reserve morgen"
    if mode == "HEAVY":
        return "starker Reiz heute mit höheren Folgekosten"
    if "+ RUN" in mode:
        return "hohe Gesamtlast, nur bei klarer Freigabe sinnvoll"
    if "sicherste Zukunft" in tags:
        return "sicherste Zukunft mit niedrigen Folgekosten"
    if "beste Balance" in tags:
        return "beste Balance aus Reiz und Kosten"
    return "tragbare Option mit moderaten Trade-offs"


def _scenario_morning_estimates(
    row: dict[str, Any],
    recovery: dict[str, Any],
    adaptation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    rmssd_base = _safe_float(recovery.get("rmssd_today"), 0.0) or _safe_float(recovery.get("rmssd_28d"), 45.0) or 45.0
    rhr_base = _safe_float(recovery.get("hr_today"), 0.0) or _safe_float(recovery.get("hr_28d"), 56.0) or 56.0

    perf_good = _safe_float(row.get("performance_probability_good"), 0.0)
    recovery_risk = _safe_float(row.get("recovery_risk"), 0.0)
    fatigue_risk = _safe_float(row.get("fatigue_risk"), 0.0)
    hrv_drop = _safe_float(row.get("hrv_drop_risk"), 0.0)
    readiness_delta = _safe_float(row.get("readiness_tomorrow_delta_estimate"), 0.0)

    rmssd_drop_ratio = _clamp((hrv_drop * 0.55) + (recovery_risk * 0.28) + (fatigue_risk * 0.12), 0.0, 0.78)
    rmssd_tomorrow = rmssd_base * (1.0 - (rmssd_drop_ratio * 0.48))

    rhr_rise = (recovery_risk * 5.0) + (fatigue_risk * 3.2) - (perf_good * 1.2)
    rhr_tomorrow = rhr_base + max(0.0, rhr_rise)

    readiness_pct = max(0.0, min(100.0, 74.0 + (readiness_delta * 100.0) - (recovery_risk * 16.0) - (fatigue_risk * 12.0)))
    fatigue_10 = max(1.0, min(10.0, 2.6 + (fatigue_risk * 5.6) + (recovery_risk * 1.4) - (perf_good * 0.8)))

    adapt = adaptation if isinstance(adaptation, dict) else {}
    if bool(adapt.get("enabled")):
        rmssd_tomorrow += _safe_float(adapt.get("rmssd_ms_correction"), 0.0)
        rhr_tomorrow += _safe_float(adapt.get("rhr_bpm_correction"), 0.0)
        readiness_pct += _safe_float(adapt.get("readiness_pct_correction"), 0.0)
        fatigue_10 += _safe_float(adapt.get("fatigue_10_correction"), 0.0)

    rmssd_tomorrow = max(0.0, rmssd_tomorrow)
    rhr_tomorrow = max(0.0, rhr_tomorrow)
    readiness_pct = max(0.0, min(100.0, readiness_pct))
    fatigue_10 = max(1.0, min(10.0, fatigue_10))

    rmssd_low = max(0.0, rmssd_tomorrow - 4.0)
    rmssd_high = rmssd_tomorrow + 4.0

    return {
        "rmssd_tomorrow_ms": round(rmssd_tomorrow, 1),
        "rmssd_delta_vs_base_ms": round(rmssd_tomorrow - rmssd_base, 1),
        "rmssd_range_low_ms": round(rmssd_low, 1),
        "rmssd_range_high_ms": round(rmssd_high, 1),
        "rhr_tomorrow_bpm": round(rhr_tomorrow, 1),
        "rhr_delta_vs_base_bpm": round(rhr_tomorrow - rhr_base, 1),
        "readiness_tomorrow_pct": int(round(readiness_pct)),
        "fatigue_tomorrow_10": round(fatigue_10, 1),
        "rmssd_base_ms": round(rmssd_base, 1),
        "rhr_base_bpm": round(rhr_base, 1),
    }


def _today_signal_tomorrow_estimate(
    *,
    state: dict[str, Any],
    clone_profile: dict[str, Any],
    chosen_row: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    recovery = state.get("recovery") if isinstance(state.get("recovery"), dict) else {}
    readiness = state.get("readiness") if isinstance(state.get("readiness"), dict) else {}
    stress = state.get("stress") if isinstance(state.get("stress"), dict) else {}
    nutrition = state.get("nutrition") if isinstance(state.get("nutrition"), dict) else {}
    load = state.get("load") if isinstance(state.get("load"), dict) else {}
    run_ctx = state.get("run_context") if isinstance(state.get("run_context"), dict) else {}
    sens = (clone_profile.get("sensitivities") or {}) if isinstance(clone_profile.get("sensitivities"), dict) else {}

    chosen = chosen_row or {}
    fatigue_risk = _safe_float(chosen.get("fatigue_risk"), 0.0)
    hrv_drop_risk = _safe_float(chosen.get("hrv_drop_risk"), 0.0)
    readiness_delta = _safe_float(chosen.get("readiness_tomorrow_delta_estimate"), 0.0)

    back_to_back_impact = _safe_float(load.get("back_to_back_risk"), 0.0) * (0.7 + (1.0 - _safe_float(sens.get("back_to_back_tolerance"), 0.5)) * 0.6)
    recovery_impact = _safe_float(recovery.get("hrv_pressure"), 0.0) * (0.55 + _safe_float(sens.get("hrv_sensitivity"), 0.4) * 0.6)
    sleep_impact = _safe_float(readiness.get("sleep_debt_score"), 0.0) * (0.5 + _safe_float(sens.get("sleep_sensitivity"), 0.4) * 0.6)
    stress_impact = _safe_float(stress.get("daily_stress"), 0.0) * 0.8
    nutrition_energy_gap = 1.0 - _safe_float(
        nutrition.get("energy_support_score"),
        1.0 - _safe_float(nutrition.get("deficit_risk"), 0.0),
    )
    nutrition_reliability_gap = 1.0 - _safe_float(
        nutrition.get("reliability_score"),
        _safe_float(nutrition.get("coverage_7d"), 0.0),
    )
    nutrition_impact = _clamp(
        ((nutrition_energy_gap * 0.68) + (nutrition_reliability_gap * 0.32))
        * (0.45 + _safe_float(sens.get("nutrition_sensitivity"), 0.4) * 0.55)
    )
    run_impact = _safe_float(run_ctx.get("interference_risk"), 0.0) * (0.45 + _safe_float(sens.get("run_interference_sensitivity"), 0.35) * 0.55)

    items = [
        {
            "key": "load_density",
            "label": "Belastungsdichte heute",
            "impact": round(back_to_back_impact, 3),
            "today_value": f"{_safe_int(load.get('heavy_days_3'), 0)} von 3 Tagen hart",
            "tomorrow_effect": (
                "drueckt morgen vor allem die Reserve"
                if back_to_back_impact >= 0.42
                else "ist vorhanden, aber morgen noch planbar"
            ),
        },
        {
            "key": "recovery_state",
            "label": "Erholung heute",
            "impact": round(recovery_impact, 3),
            "today_value": f"HRV-Druck {_safe_int(_safe_float(recovery.get('hrv_pressure'), 0.0) * 100, 0)}",
            "tomorrow_effect": (
                "kann morgen HRV und Reserve spuerbar druecken"
                if recovery_impact >= 0.36 or hrv_drop_risk >= 0.45
                else "bleibt fuer morgen eher stabil"
            ),
        },
        {
            "key": "sleep_state",
            "label": "Schlaflage heute",
            "impact": round(sleep_impact, 3),
            "today_value": f"Schlafdruck {_safe_int(_safe_float(readiness.get('sleep_debt_score'), 0.0) * 100, 0)}",
            "tomorrow_effect": (
                "zieht morgen die Energie runter"
                if sleep_impact >= 0.34
                else "wirkt morgen nur leicht"
            ),
        },
        {
            "key": "calendar_stress",
            "label": "Alltagsstress heute",
            "impact": round(stress_impact, 3),
            "today_value": f"{_safe_int(_safe_float(stress.get('daily_stress'), 0.0) * 100, 0)} Stress",
            "tomorrow_effect": (
                "kostet morgen zusaetzliche Erholung"
                if stress_impact >= 0.34 or fatigue_risk >= 0.55
                else "bleibt morgen moderat"
            ),
        },
        {
            "key": "nutrition_state",
            "label": "Ernaehrungslage heute",
            "impact": round(nutrition_impact, 3),
            "today_value": (
                f"Energie-Support {_safe_int(_safe_float(nutrition.get('energy_support_score'), 0.0) * 100, 0)} "
                f"· Verlässlichkeit {_safe_int(_safe_float(nutrition.get('reliability_score'), 0.0) * 100, 0)}"
            ),
            "tomorrow_effect": (
                "limitiert morgen die Erholung"
                if nutrition_impact >= 0.33
                else "ist fuer morgen ausreichend"
            ),
        },
        {
            "key": "run_load",
            "label": "Laufbelastung heute",
            "impact": round(run_impact, 3),
            "today_value": f"{round(_safe_float(run_ctx.get('km_7d'), 0.0), 1)} km in 7d",
            "tomorrow_effect": (
                "erhoht morgen die Gesamtmüdigkeit"
                if run_impact >= 0.3
                else "spielt morgen kaum rein"
            ),
        },
    ]

    for row in items:
        impact = _safe_float(row.get("impact"), 0.0)
        if impact >= 0.45:
            row["strength"] = "stark"
        elif impact >= 0.28:
            row["strength"] = "moderat"
        else:
            row["strength"] = "leicht"
        row["tomorrow_hint"] = (
            "morgen eher enger"
            if readiness_delta < -0.07 and impact >= 0.28
            else "morgen noch im Rahmen"
        )

    items.sort(key=lambda r: _safe_float(r.get("impact"), 0.0), reverse=True)
    return items[:4]


def _driver_evidence(
    row: dict[str, Any],
    *,
    recovery: dict[str, Any],
    training: dict[str, Any],
    run_ctx: dict[str, Any],
    nutrition: dict[str, Any],
    stress: dict[str, Any],
    heavy_vs_normal: dict[str, Any],
) -> str:
    code = str(row.get("code") or "").upper()
    if code == "SIM_HEAVY_RISK":
        d_score = _safe_float(heavy_vs_normal.get("score_delta"), 0.0)
        d_rec = _safe_float(heavy_vs_normal.get("recovery_delta"), 0.0)
        return f"Heavy liegt {abs(round(d_score, 1))} Punkte {'unter' if d_score < 0 else 'über'} NORMAL; Recovery-Risiko {'+' if d_rec >= 0 else ''}{round(d_rec * 100)} Punkte."
    if code == "BACK_TO_BACK_LOAD":
        return f"{_safe_int(training.get('heavy_days_3'), 0)} der letzten 3 Tage lagen über der Belastungsschwelle."
    if code == "HRV_DEPRESSED":
        return f"HRV-Quote {round(_safe_float(recovery.get('hrv_ratio'), 1.0) * 100)}% vom Ø-Wert; RHR-Quote {round(_safe_float(recovery.get('rhr_ratio'), 1.0) * 100)}%."
    if code == "RUN_INTERFERENCE":
        return f"Lauf-Interferenzrisiko bei {round(_safe_float(run_ctx.get('interference_risk'), 0.0) * 100)}%."
    if code == "NUTRITION_UNDERSUPPORTED":
        return (
            f"Energie-Support {round(_safe_float(nutrition.get('energy_support_score'), 0.0) * 100)}% · "
            f"Verlässlichkeit {round(_safe_float(nutrition.get('reliability_score'), _safe_float(nutrition.get('coverage_7d'), 0.0)) * 100)}%."
        )
    if code == "MUSCULAR_FATIGUE":
        return f"Lokale Vorermüdung bei {round(_safe_float(row.get('raw_signal'), 0.0) * 100)}% Signalstärke."
    if code == "HEAVY_RECOVERY_COST":
        return (
            f"Heavy-Folgekosten: Recovery-Risiko {round(_safe_float(heavy_vs_normal.get('heavy_recovery'), 0.0) * 100)}% · "
            f"Fatigue-Risiko {round(_safe_float(row.get('raw_signal'), 0.0) * 100)}%."
        )
    if code == "CALENDAR_FRICTION":
        return (
            f"Kalenderfriktion bei {round(_safe_float(row.get('raw_signal'), 0.0) * 100)}% "
            f"({ _format_minutes_short(stress.get('free_minutes_today')) if stress.get('free_minutes_today') is not None else 'ohne belastbares Zeitfenster'} frei)."
        )
    if code == "STRESS_LOAD":
        free_min = stress.get("free_minutes_today")
        if free_min is not None:
            return (
                f"Freie Kalenderzeit heute {_format_minutes_short(free_min)}; "
                f"Alltagsstress bei {round(_safe_float(stress.get('daily_stress'), 0.0) * 100)}%."
            )
        return f"Alltagsstress bei {round(_safe_float(stress.get('daily_stress'), 0.0) * 100)}%."
    if code == "DATA_QUALITY_LOW":
        return (
            f"Datenqualität {round((1.0 - _safe_float(row.get('raw_signal'), 0.0)) * 100)}%: "
            f"Nutrition-Coverage {round(_safe_float(nutrition.get('coverage_7d'), 0.0) * 100)}%."
        )
    return f"Signalwirkung {round(_safe_float(row.get('impact'), _safe_float(row.get('value'), 0.0)) * 100)}%."


def _decision_bridge_line(
    *,
    chosen_mode: str,
    blocker_count: int,
    objection_total: float,
    normal_threshold: float,
    light_threshold: float,
    heavy_vs_normal_delta: float | None,
    capacity_today_score: float | None = None,
    recovery_cost_score: float | None = None,
    feasibility_today_score: float | None = None,
) -> str:
    capacity = _safe_float(capacity_today_score, -1.0)
    cost = _safe_float(recovery_cost_score, -1.0)
    feasibility = _safe_float(feasibility_today_score, -1.0)
    has_layers = capacity >= 0.0 and cost >= 0.0 and feasibility >= 0.0

    if has_layers:
        if chosen_mode == MODE_REST:
            return "Freigabe ist in allen Kernschichten kollabiert, daher heute REST."
        if chosen_mode == MODE_LIGHT:
            return "Kapazität/Umsetzbarkeit reichen für HEAVY nicht, Folgekosten bleiben zu hoch -> LIGHT."
        if chosen_mode == MODE_NORMAL:
            return "Kapazität ist trainierbar, aber Kosten- oder Umsetzbarkeitslayer bremsen HEAVY -> NORMAL."
        if capacity >= 0.58 and cost <= 0.58 and feasibility >= 0.38:
            return "Kapazität gut, Folgekosten tragbar, Umsetzbarkeit ausreichend -> HEAVY bleibt."
        return "HEAVY bleibt knapp innerhalb der Freigabegrenzen."

    if chosen_mode == MODE_REST:
        return "Hard Stop aktiv, daher heute klar REST."
    if chosen_mode == MODE_LIGHT:
        return "Gegenwind hat die Downgrade-Schwelle klar gerissen."
    if chosen_mode == MODE_NORMAL:
        return "Gegenwind liegt über der Heavy-Schwelle, aber unter der Light-Schwelle."
    if blocker_count > 0:
        return "Trotz Gegenwind kein harter Stopp, daher noch trainierbar."
    if objection_total >= (normal_threshold * 0.92):
        return "Kein Hard Stop, Gegenwind reicht noch nicht für Downgrade."
    if heavy_vs_normal_delta is not None and heavy_vs_normal_delta < 0:
        return "Heavy kostet mehr als NORMAL, bleibt aber noch innerhalb der Freigabe."
    if objection_total >= (normal_threshold * 0.72):
        return "Gegenwind vorhanden, aber Schwelle für Downgrade nicht gerissen."
    return "Keine harte Bremse, daher kein Downgrade."


def _friendly_driver_label(code: str, fallback: str = "") -> str:
    mapping = {
        "SLEEP_DEFICIT": "Schlaf heute zu kurz",
        "HRV_DEPRESSED": "Erholung gedrückt",
        "NUTRITION_UNDERSUPPORTED": "Energie nicht voll gedeckt",
        "RUN_INTERFERENCE": "Laufen drückt heute rein",
        "MUSCULAR_FATIGUE": "Muskeln noch vorermüdet",
        "BACK_TO_BACK_LOAD": "Mehrere harte Tage in Folge",
        "HEAVY_RECOVERY_COST": "Folgekosten nach Heavy erhöht",
        "CALENDAR_FRICTION": "Tag schwer sauber umsetzbar",
        "STRESS_LOAD": "Alltagsstress heute hoch",
        "DATA_QUALITY_LOW": "Datenbasis heute weniger verlässlich",
        "SIM_HEAVY_RISK": "Nachtanalyse sieht Heavy teurer",
        "SICK": "Krankheitszeichen aktiv",
        "INFECTION_ACUTE": "Akute Infektlage",
        "RECOVERY_MISSING_EXTREME": "Erholungslage unklar",
        "SYSTEMIC_FATIGUE_EXTREME": "System stark erschöpft",
        "NOT_TRAINABLE": "Heute nicht trainierbar",
    }
    key = str(code or "").upper()
    return mapping.get(key, fallback or key or "Signal")


def _store_explain_payload(conn: sqlite3.Connection, *, day_iso: str, payload: dict[str, Any]) -> None:
    conn.execute(
        """
        INSERT INTO core_decision_explain_v2 (
            date, created_at, decision_json, night_cycle_json, clone_json,
            scenarios_json, learning_json, trace_json, payload_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(date) DO UPDATE SET
            created_at=excluded.created_at,
            decision_json=excluded.decision_json,
            night_cycle_json=excluded.night_cycle_json,
            clone_json=excluded.clone_json,
            scenarios_json=excluded.scenarios_json,
            learning_json=excluded.learning_json,
            trace_json=excluded.trace_json,
            payload_json=excluded.payload_json
        """,
        (
            day_iso,
            _utc_now(),
            json.dumps(payload.get("decision") or {}, ensure_ascii=False),
            json.dumps(payload.get("night_cycle") or {}, ensure_ascii=False),
            json.dumps(payload.get("clone") or {}, ensure_ascii=False),
            json.dumps(payload.get("scenarios") or {}, ensure_ascii=False),
            json.dumps(payload.get("learning") or {}, ensure_ascii=False),
            json.dumps(payload.get("trace") or {}, ensure_ascii=False),
            json.dumps(payload or {}, ensure_ascii=False),
        ),
    )


def load_explain_payload(day_iso: str | None = None) -> dict[str, Any] | None:
    day_iso = day_iso or date.today().isoformat()
    conn = get_core_db()
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT date, payload_json
            FROM core_decision_explain_v2
            WHERE date <= ?
            ORDER BY date DESC
            LIMIT 10
            """,
            (day_iso,),
        ).fetchall()
        if not rows:
            return None

        fallback_payload: dict[str, Any] | None = None
        for row in rows:
            payload = _parse_json(row["payload_json"], {})
            if not isinstance(payload, dict):
                continue
            if fallback_payload is None:
                fallback_payload = payload
            items = (((payload.get("today_signals") or {}).get("items")) if isinstance(payload.get("today_signals"), dict) else None) or []
            if isinstance(items, list) and len(items) > 0:
                return payload
        return fallback_payload
    finally:
        conn.close()


def run_night_cycle(
    *,
    day_iso: str | None = None,
    apply_parameter_updates: bool = True,
) -> dict[str, Any]:
    day_iso = day_iso or date.today().isoformat()
    completed_at = _utc_now()
    ns_payload = _invoke_next_session(day_iso)
    meta_bundle = _extract_meta_from_next_session(ns_payload)
    runtime_state = _state_from_runtime_inputs(meta_bundle.get("runtime_inputs") or {})

    recovery_db = _build_recovery_snapshot(day_iso)
    training_db = _build_training_snapshot(day_iso)
    run_db = _build_run_snapshot(day_iso)
    nutrition_db = _build_nutrition_snapshot(day_iso)
    calendar_stress = _build_calendar_stress_snapshot(day_iso)

    runtime_stress = runtime_state.get("stress") if isinstance(runtime_state.get("stress"), dict) else {}
    stress_daily = calendar_stress.get("daily_stress")
    if stress_daily is None:
        stress_daily = runtime_stress.get("daily_stress")
    merged_stress = {
        **runtime_stress,
        **calendar_stress,
        "source": "calendar_free_time" if bool(calendar_stress.get("calendar_available")) else "runtime_proxy",
        "daily_stress": round(_clamp(_safe_float(stress_daily, 0.0)), 3),
    }

    runtime_fatigue = runtime_state.get("fatigue") if isinstance(runtime_state.get("fatigue"), dict) else {}
    days_since_last_workout = _safe_int(training_db.get("days_since_last_workout"), 7)
    heavy_days_recent = _safe_int(training_db.get("heavy_days_3"), 0)
    muscular_preload = _safe_float(runtime_fatigue.get("muscular_preload"), 0.0)
    if days_since_last_workout >= 2:
        if days_since_last_workout == 2:
            muscular_preload *= 0.28
            muscular_preload = min(muscular_preload, 0.09)
        elif days_since_last_workout == 3:
            muscular_preload *= 0.12
            muscular_preload = min(muscular_preload, 0.04)
        else:
            muscular_preload *= 0.06
            muscular_preload = min(muscular_preload, 0.02)
    # If there was no hard day in the last 3 days, local preload should not stay sticky.
    if heavy_days_recent <= 0:
        if days_since_last_workout >= 2:
            muscular_preload = min(muscular_preload, 0.02)
        elif days_since_last_workout == 1:
            muscular_preload = min(muscular_preload, 0.06)
        else:
            muscular_preload = min(muscular_preload, 0.08)
    muscular_preload = round(_clamp(muscular_preload), 3)

    final_state = {
        "date": day_iso,
        "readiness": {
            **(runtime_state.get("readiness") if isinstance(runtime_state.get("readiness"), dict) else {}),
        },
        "recovery": {
            **(runtime_state.get("recovery") if isinstance(runtime_state.get("recovery"), dict) else {}),
            **recovery_db,
        },
        "fatigue": {
            **runtime_fatigue,
            "muscular_preload": muscular_preload,
        },
        "nutrition": nutrition_db,
        "load": {
            "back_to_back_risk": _safe_float(training_db.get("back_to_back_risk"), 0.0),
            "heavy_days_3": _safe_int(training_db.get("heavy_days_3"), 0),
            "avg_rpe_7d": _safe_float(training_db.get("avg_rpe_7d"), 0.0),
        },
        "run_context": {
            "interference_risk": _safe_float(run_db.get("interference_risk"), 0.0),
            "km_7d": _safe_float(run_db.get("km_7d"), 0.0),
        },
        "stress": merged_stress,
        "illness": {
            **(runtime_state.get("illness") if isinstance(runtime_state.get("illness"), dict) else {}),
            "sick": bool((runtime_state.get("illness") or {}).get("sick")) or bool(recovery_db.get("sick_flag")),
        },
        "history_days": _safe_int(training_db.get("history_days"), 28),
        "history_sessions": _safe_int(training_db.get("history_sessions"), 0),
        "recovery_cycles": max(1, _safe_int(training_db.get("history_days"), 28) // 2),
        "sources": {
            "next_session": bool(ns_payload),
            "training_db": bool(training_db),
            "runs_db": bool(run_db),
            "nutrition_db": bool(nutrition_db),
            "hrv_db": bool(recovery_db),
        },
    }

    conn = get_core_db()
    conn.row_factory = sqlite3.Row
    out_payload: dict[str, Any] | None = None
    try:
        parameters, _active = get_active_parameters(conn)
        decisions = _load_recent_decisions(conn, day_iso, lookback_days=0)
        reviews = _load_latest_reviews_by_decision(conn, day_iso, lookback_days=0)
        history_rows = build_history_rows_from_decisions(decisions, reviews)
        latest_clone = _load_latest_clone_snapshot(conn, day_iso)
        clone_profile = build_clone_profile(state=final_state, history_rows=history_rows, latest_snapshot=latest_clone)
        clone_snapshot_id = _store_clone_snapshot(conn, day_iso=day_iso, clone_profile=clone_profile)

        scenarios = generate_scenarios(state=final_state)
        simulations = [simulate_scenario(scenario=sc, state=final_state, clone_profile=clone_profile) for sc in scenarios]
        calibration_profile = _simulation_calibration_profile(conn, day_iso=day_iso, days=60)
        simulations = _apply_simulation_calibration(simulations=simulations, calibration_profile=calibration_profile)
        simulations = sorted(simulations, key=lambda row: _safe_float(row.get("score"), -999.0), reverse=True)

        decision = decide_heavy_first(state=final_state, scenarios=simulations, parameter_weights=parameters)
        chosen_mode = str(decision.get("chosen_mode") or MODE_NORMAL).upper()

        chosen_candidates = [row for row in simulations if str(row.get("gym_mode") or "").upper() == chosen_mode]
        chosen = chosen_candidates[0] if chosen_candidates else (simulations[0] if simulations else {})
        chosen_scenario_key = str(chosen.get("scenario_key") or "")
        _store_simulation_runs(conn, day_iso=day_iso, state=final_state, simulations=simulations, chosen_scenario_key=chosen_scenario_key)

        feedback_today = _store_simulation_feedback_for_day(conn, day_iso=day_iso, state=final_state)
        feedback_latest = feedback_today or _latest_simulation_feedback(conn, day_iso=day_iso)
        feedback_summary = _simulation_feedback_summary(conn, day_iso=day_iso, days=60)
        feedback_summary_14d = _simulation_feedback_summary(conn, day_iso=day_iso, days=14)
        forecast_adaptation = _build_forecast_adaptation_profile(
            feedback_14d=feedback_summary_14d,
            feedback_60d=feedback_summary,
            calibration_profile=calibration_profile,
        )

        tuning = tune_parameters(conn, day_iso=day_iso, apply_updates=apply_parameter_updates)
        recent_updates = latest_parameter_updates(conn, limit=8)
        quality = _build_decision_quality_summary(conn, day_iso, days=14)
        model_precision = _compose_model_precision(
            feedback_14d=feedback_summary_14d,
            feedback_60d=feedback_summary,
            quality_14d=quality,
            clone_trust=_safe_float((clone_profile.get("summary") or {}).get("trust_score"), 0.5),
        )

        if quality.get("total"):
            quality_summary = (
                f"Letzte {quality.get('window_days')} Entscheidungen: {round(_safe_float(quality.get('accuracy'), 0.0) * 100)}% sauber getroffen."
            )
        else:
            quality_summary = "Noch kein belastbares 14-Tage-Outcome-Fenster."

        options = _dedupe_simulations(simulations, chosen_scenario_key)
        anchors = _build_option_anchors(options, chosen_scenario_key=chosen_scenario_key)

        heavy_row = next(
            (
                row
                for row in simulations
                if str(row.get("gym_mode") or "").upper() == MODE_HEAVY and str(row.get("run_plan") or "NONE").upper() == "NONE"
            ),
            None,
        ) or _pick_option_by_label(options, MODE_HEAVY)
        normal_row = next(
            (
                row
                for row in simulations
                if str(row.get("gym_mode") or "").upper() == MODE_NORMAL and str(row.get("run_plan") or "NONE").upper() == "NONE"
            ),
            None,
        ) or _pick_option_by_label(options, MODE_NORMAL)

        heavy_vs_normal: dict[str, Any] = {}
        if heavy_row and normal_row:
            heavy_vs_normal = {
                "heavy_score": round(_safe_float(heavy_row.get("score"), 0.0), 2),
                "normal_score": round(_safe_float(normal_row.get("score"), 0.0), 2),
                "score_delta": round(_safe_float(heavy_row.get("score"), 0.0) - _safe_float(normal_row.get("score"), 0.0), 2),
                "heavy_recovery": round(_safe_float(heavy_row.get("recovery_risk"), 0.0), 3),
                "normal_recovery": round(_safe_float(normal_row.get("recovery_risk"), 0.0), 3),
                "recovery_delta": round(_safe_float(heavy_row.get("recovery_risk"), 0.0) - _safe_float(normal_row.get("recovery_risk"), 0.0), 3),
                "heavy_performance": round(_safe_float(heavy_row.get("performance_probability_good"), 0.0), 3),
                "normal_performance": round(_safe_float(normal_row.get("performance_probability_good"), 0.0), 3),
                "performance_delta": round(
                    _safe_float(heavy_row.get("performance_probability_good"), 0.0)
                    - _safe_float(normal_row.get("performance_probability_good"), 0.0),
                    3,
                ),
            }

        explain_meta = decision.get("explanation_payload") if isinstance(decision.get("explanation_payload"), dict) else {}
        objection_total = _safe_float(explain_meta.get("objection_total"), 0.0)
        thresholds = explain_meta.get("thresholds") if isinstance(explain_meta.get("thresholds"), dict) else {}
        normal_threshold = _safe_float(thresholds.get("normal"), 1.18)
        light_threshold = _safe_float(thresholds.get("light"), 2.05)
        capacity_today_score = _safe_float(explain_meta.get("capacity_today_score"), -1.0)
        recovery_cost_score = _safe_float(explain_meta.get("recovery_cost_score"), -1.0)
        feasibility_today_score = _safe_float(explain_meta.get("feasibility_today_score"), -1.0)
        decision_clarity = _safe_float(explain_meta.get("decision_clarity"), _safe_float(decision.get("confidence"), 0.5))

        decisive_drivers_raw = decision.get("decisive_reasons") if isinstance(decision.get("decisive_reasons"), list) else []
        blockers_raw = decision.get("blockers") if isinstance(decision.get("blockers"), list) else []
        objections_raw = decision.get("objections") if isinstance(decision.get("objections"), list) else []

        decisive_drivers = [
            {
                "code": str(row.get("code") or ""),
                "label": _friendly_driver_label(str(row.get("code") or ""), str(row.get("label") or "")),
                "severity": str(row.get("severity") or "moderate"),
                "impact": _safe_float(row.get("impact"), _safe_float(row.get("weighted_score"), 0.0)),
                "detail": str(row.get("detail") or ""),
                "evidence": _driver_evidence(
                    row,
                    recovery=final_state.get("recovery") if isinstance(final_state.get("recovery"), dict) else {},
                    training=training_db,
                    run_ctx=final_state.get("run_context") if isinstance(final_state.get("run_context"), dict) else {},
                    nutrition=final_state.get("nutrition") if isinstance(final_state.get("nutrition"), dict) else {},
                    stress=final_state.get("stress") if isinstance(final_state.get("stress"), dict) else {},
                    heavy_vs_normal=heavy_vs_normal,
                ),
            }
            for row in decisive_drivers_raw[:4]
            if isinstance(row, dict)
        ]

        blockers = [
            {
                "code": str(row.get("code") or ""),
                "label": _friendly_driver_label(str(row.get("code") or ""), str(row.get("label") or "")),
                "severity": str(row.get("severity") or "hard_stop"),
                "impact": _safe_float(row.get("impact"), 1.0),
                "evidence": _driver_evidence(
                    row,
                    recovery=final_state.get("recovery") if isinstance(final_state.get("recovery"), dict) else {},
                    training=training_db,
                    run_ctx=final_state.get("run_context") if isinstance(final_state.get("run_context"), dict) else {},
                    nutrition=final_state.get("nutrition") if isinstance(final_state.get("nutrition"), dict) else {},
                    stress=final_state.get("stress") if isinstance(final_state.get("stress"), dict) else {},
                    heavy_vs_normal=heavy_vs_normal,
                ),
            }
            for row in blockers_raw[:4]
            if isinstance(row, dict)
        ]

        why_not_heavy: list[dict[str, Any]] = []
        for row in objections_raw[:8]:
            if not isinstance(row, dict):
                continue
            value = _safe_float(row.get("weighted_score"), _safe_float(row.get("impact"), 0.0))
            if value < 0.09:
                continue
            why_not_heavy.append(
                {
                    "code": str(row.get("code") or ""),
                    "label": _friendly_driver_label(str(row.get("code") or ""), str(row.get("label") or "")),
                    "severity": str(row.get("severity") or "moderate"),
                    "value": value,
                    "evidence": _driver_evidence(
                        row,
                        recovery=final_state.get("recovery") if isinstance(final_state.get("recovery"), dict) else {},
                        training=training_db,
                        run_ctx=final_state.get("run_context") if isinstance(final_state.get("run_context"), dict) else {},
                        nutrition=final_state.get("nutrition") if isinstance(final_state.get("nutrition"), dict) else {},
                        stress=final_state.get("stress") if isinstance(final_state.get("stress"), dict) else {},
                        heavy_vs_normal=heavy_vs_normal,
                    ),
                }
            )
            if len(why_not_heavy) >= 6:
                break

        for_signals: list[dict[str, str]] = []
        against_signals: list[dict[str, str]] = []

        if not blockers:
            for_signals.append({"label": "Kein Hard Stop", "evidence": "Keine Krankheits- oder Stopp-Signale aktiv."})
        free_minutes_today = (final_state.get("stress") or {}).get("free_minutes_today")
        if free_minutes_today is not None:
            for_signals.append(
                {
                    "label": "Kalender lässt Trainingsfenster zu",
                    "evidence": f"Belegte Kalenderzeit heute: {_format_minutes_short((final_state.get('stress') or {}).get('busy_minutes_today'))}.",
                }
            )
        else:
            for_signals.append(
                {
                    "label": "Tagesrhythmus ohne harten Zeitstopp",
                    "evidence": "Kalenderdaten heute unvollständig, kein Zeit-Stop signalisiert.",
                }
            )
        if chosen:
            chosen_perf = _safe_float(chosen.get("performance_probability_good"), 0.0)
            for_signals.append({"label": "Leistungsprognose tragbar", "evidence": f"Performance-Prognose {round(chosen_perf * 100)}%."})

        against_source = why_not_heavy[:3] or decisive_drivers[:3]
        for row in against_source:
            against_signals.append({"label": str(row.get("label") or ""), "evidence": str(row.get("evidence") or row.get("detail") or "")})

        bridge_line = _decision_bridge_line(
            chosen_mode=chosen_mode,
            blocker_count=len(blockers),
            objection_total=objection_total,
            normal_threshold=normal_threshold,
            light_threshold=light_threshold,
            heavy_vs_normal_delta=heavy_vs_normal.get("score_delta") if heavy_vs_normal else None,
            capacity_today_score=capacity_today_score if capacity_today_score >= 0 else None,
            recovery_cost_score=recovery_cost_score if recovery_cost_score >= 0 else None,
            feasibility_today_score=feasibility_today_score if feasibility_today_score >= 0 else None,
        )

        chosen_hrv_drop = _safe_float(chosen.get("hrv_drop_risk"), 0.0) if isinstance(chosen, dict) else 0.0
        chosen_readiness_delta = _safe_float(chosen.get("readiness_tomorrow_delta_estimate"), 0.0) if isinstance(chosen, dict) else 0.0
        chosen_fatigue_risk = _safe_float(chosen.get("fatigue_risk"), 0.0) if isinstance(chosen, dict) else 0.0
        chosen_perf_good = _safe_float(chosen.get("performance_probability_good"), 0.0) if isinstance(chosen, dict) else 0.0
        chosen_label = str((anchors.get("chosen") or {}).get("option_label") or chosen_mode)
        if chosen_hrv_drop >= 0.55:
            tomorrow_headline = f"{chosen_label}: HRV wird morgen voraussichtlich spürbar drücken."
            tomorrow_frame = "Dieser Preis ist als Progressions-Tradeoff bewusst eingeplant."
        elif chosen_readiness_delta < -0.06:
            tomorrow_headline = f"{chosen_label}: Morgen ist ein kleiner Recovery-Drop wahrscheinlich."
            tomorrow_frame = "Die Last bleibt im Rahmen, aber kostet Erholung."
        elif chosen_fatigue_risk >= 0.55:
            tomorrow_headline = f"{chosen_label}: Ermüdung morgen erhöht, aber planbar."
            tomorrow_frame = "Der Reiz ist höher als bei REST, die Folgekosten bleiben beobachtbar."
        else:
            tomorrow_headline = f"{chosen_label}: Morgen voraussichtlich stabil mit moderaten Kosten."
            tomorrow_frame = "Kein harter Gegenschlag erwartet."
        tomorrow_detail = (
            f"Performance-Prognose {round(chosen_perf_good * 100)}% · "
            f"HRV-Rückgang {round(chosen_hrv_drop * 100)}% · "
            f"Readiness Δ {round(chosen_readiness_delta * 100)}%."
        )

        safest_key = str((anchors.get("safest") or {}).get("scenario_key") or "")
        best_balance_key = str((anchors.get("best_balance") or {}).get("scenario_key") or "")
        chosen_anchor_key = str((anchors.get("chosen") or {}).get("scenario_key") or chosen_scenario_key)
        highest_price_key = str((anchors.get("highest_price") or {}).get("scenario_key") or "")

        highest_reiz = max(options, key=lambda r: _future_progression_score(r)) if options else None
        highest_reserve = max(options, key=lambda r: _future_reserve_tomorrow(r)) if options else None
        highest_reiz_key = str((highest_reiz or {}).get("scenario_key") or "")
        highest_reserve_key = str((highest_reserve or {}).get("scenario_key") or "")

        chosen_progression = _future_progression_score(chosen or {})
        chosen_cost = _future_cost_score(chosen or {})
        chosen_ratio = round(chosen_progression / max(1.0, chosen_cost), 2)
        chosen_reserve = _future_reserve_tomorrow(chosen or {})
        chosen_hrv_tomorrow = int(round(max(0.0, 100.0 - (chosen_hrv_drop * 100.0))))
        chosen_fatigue_tomorrow = int(round(chosen_fatigue_risk * 100.0))
        chosen_cost_level = _future_cost_level(chosen_cost)

        normal_opt = _pick_option_by_label(options, MODE_NORMAL)
        rest_opt = _pick_option_by_label(options, MODE_REST)
        normal_reserve = _future_reserve_tomorrow(normal_opt) if isinstance(normal_opt, dict) else None
        rest_reserve = _future_reserve_tomorrow(rest_opt) if isinstance(rest_opt, dict) else None
        reserve_gap_vs_normal = (chosen_reserve - normal_reserve) if normal_reserve is not None else None

        if chosen_label.upper().startswith("HEAVY") and reserve_gap_vs_normal is not None and reserve_gap_vs_normal <= -8:
            future_hero = f"Wenn du heute {chosen_label} gehst, sinkt morgen die Reserve spürbar gegenüber NORMAL."
        elif chosen_cost >= 64:
            future_hero = f"{chosen_label} setzt heute auf Reiz, morgen mit erhöhten Folgekosten."
        elif chosen_reserve >= 72:
            future_hero = f"{chosen_label} hält fuer morgen eine solide Reserve frei."
        else:
            future_hero = f"{chosen_label} bleibt morgen voraussichtlich im planbaren Bereich."

        relevant_labels = {MODE_HEAVY, MODE_NORMAL, MODE_LIGHT, MODE_REST}
        future_compare: list[dict[str, Any]] = []
        for row in options:
            option_label = str(row.get("option_label") or _canonical_option_label(row))
            if option_label not in relevant_labels:
                continue
            progression_score = _future_progression_score(row)
            cost_score = _future_cost_score(row)
            reserve_score = _future_reserve_tomorrow(row)
            tags = _future_anchor_tags(
                row=row,
                chosen_key=chosen_anchor_key,
                safest_key=safest_key,
                best_balance_key=best_balance_key,
                highest_reiz_key=highest_reiz_key,
                highest_reserve_key=highest_reserve_key,
                highest_price_key=highest_price_key,
            )
            future_compare.append(
                {
                    "scenario_key": str(row.get("scenario_key") or ""),
                    "label": option_label,
                    "selected": bool(str(row.get("scenario_key") or "") == chosen_anchor_key or bool(row.get("selected"))),
                    "reiz_heute": round(progression_score, 1),
                    "performance_heute": round(_safe_float(row.get("performance_probability_good"), 0.0) * 100.0, 1),
                    "recovery_kosten": round(cost_score, 1),
                    "reserve_morgen": reserve_score,
                    "hrv_morgen": int(round(max(0.0, 100.0 - (_safe_float(row.get("hrv_drop_risk"), 0.0) * 100.0)))),
                    "muedigkeit_morgen": int(round(_safe_float(row.get("fatigue_risk"), 0.0) * 100.0)),
                    "folgekosten_48h": int(round((_safe_float(row.get("recovery_risk"), 0.0) * 0.6 + _safe_float(row.get("fatigue_risk"), 0.0) * 0.4) * 100.0)),
                    "reiz_kosten_verhaeltnis": round(progression_score / max(1.0, cost_score), 2),
                    "cost_level": _future_cost_level(cost_score),
                    "note": _future_option_note(row, tags),
                    "tags": tags,
                }
            )
        future_compare.sort(key=lambda x: {"REST": 0, "LIGHT": 1, "NORMAL": 2, "HEAVY": 3}.get(str(x.get("label") or ""), 9))

        sens = (clone_profile.get("sensitivities") or {}) if isinstance(clone_profile.get("sensitivities"), dict) else {}
        sleep_sens = _safe_float(sens.get("sleep_sensitivity"), 0.0)
        hrv_sens = _safe_float(sens.get("hrv_sensitivity"), 0.0)
        load_sens = 1.0 - _safe_float(sens.get("back_to_back_tolerance"), 0.5)
        clone_insight_points: list[str] = []
        if load_sens >= 0.5:
            clone_insight_points.append("An ähnlichen Tagen kosten mehrere harte Tage meist mehr Recovery als HRV.")
        else:
            clone_insight_points.append("Mehrere harte Tage sind aktuell solide tolerierbar, aber nicht frei.")
        if sleep_sens >= 0.5:
            clone_insight_points.append("Schlaf wirkt überdurchschnittlich stark auf die morgige Reserve.")
        if hrv_sens >= 0.5:
            clone_insight_points.append("HRV reagiert in deinem Modell sensibel auf hohe Last.")
        if not clone_insight_points:
            clone_insight_points.append("NORMAL bleibt an vergleichbaren Tagen meist die stabilste Balance.")
        today_signal_estimates = _today_signal_tomorrow_estimate(
            state=final_state,
            clone_profile=clone_profile,
            chosen_row=chosen if isinstance(chosen, dict) else None,
        )
        recovery_ctx = final_state.get("recovery") if isinstance(final_state.get("recovery"), dict) else {}
        chosen_morning = _scenario_morning_estimates(chosen or {}, recovery_ctx, forecast_adaptation)
        compare_meta_by_key = {
            str(row.get("scenario_key") or ""): row
            for row in future_compare
            if isinstance(row, dict)
        }
        scenario_projection: list[dict[str, Any]] = []
        for row in options:
            option_label = str(row.get("option_label") or _canonical_option_label(row))
            if option_label not in {MODE_HEAVY, MODE_NORMAL, MODE_LIGHT, MODE_REST}:
                continue
            key = str(row.get("scenario_key") or "")
            cmp_meta = compare_meta_by_key.get(key) or {}
            scenario_projection.append(
                {
                    "label": option_label,
                    "scenario_key": key,
                    "selected": bool(key == chosen_anchor_key or bool(row.get("selected"))),
                    "tags": cmp_meta.get("tags") if isinstance(cmp_meta.get("tags"), list) else [],
                    "tomorrow_estimate": _scenario_morning_estimates(row, recovery_ctx, forecast_adaptation),
                }
            )
        scenario_projection.sort(key=lambda x: {"REST": 0, "LIGHT": 1, "NORMAL": 2, "HEAVY": 3}.get(str(x.get("label") or ""), 9))

        daily_decision = build_daily_decision(
            day_iso=day_iso,
            state=final_state,
            ns_payload=ns_payload,
            legacy_decision=decision,
            scenarios={"list": options},
        )
        compat_mode = str(((daily_decision.get("legacy_compat") or {}).get("mode")) or chosen_mode).upper()
        daily_context = collect_daily_decision_context(day_iso, state=final_state, ns_payload=ns_payload)

        payload = {
            "ok": True,
            "date": day_iso,
            "generated_at": completed_at,
            "today_signals": _build_today_signals(day_iso, final_state),
            "daily_decision": daily_decision,
            "decision": {
                "hero": daily_decision.get("hero"),
                "planned_unit": daily_decision.get("planned_unit"),
                "permissions": daily_decision.get("permissions"),
                "consequences": daily_decision.get("consequences"),
                "reasons": daily_decision.get("reasons"),
                "status": daily_decision.get("status"),
                "mode": compat_mode,
                "legacy_compat": {
                    "mode": compat_mode,
                    "legacy_mode": chosen_mode,
                    "note": "legacy only, not coaching decision",
                },
                "started_from": str(decision.get("started_from") or MODE_HEAVY),
                "final_confidence": _safe_float(decision.get("confidence"), 0.5),
                "downgrade_path": decision.get("downgrade_path") if isinstance(decision.get("downgrade_path"), list) else [MODE_HEAVY, chosen_mode],
                "decisive_drivers": decisive_drivers[:3],
                "blockers": blockers[:4],
                "subtitle": str(decision.get("subtitle") or "HEAVY blieb Standard"),
                "selected_scenario_key": chosen_scenario_key,
                "why_not_heavy": why_not_heavy,
                "for_signals": for_signals[:3],
                "against_signals": against_signals[:3],
                "bridge_line": bridge_line,
                "kpis": {
                    "hard_stop_count": len(blockers),
                    "counterwind_count": len(why_not_heavy),
                    "objection_total": round(objection_total, 3),
                    "normal_threshold": round(normal_threshold, 3),
                    "light_threshold": round(light_threshold, 3),
                    "capacity_today_score": round(capacity_today_score, 3) if capacity_today_score >= 0 else None,
                    "recovery_cost_score": round(recovery_cost_score, 3) if recovery_cost_score >= 0 else None,
                    "feasibility_today_score": round(feasibility_today_score, 3) if feasibility_today_score >= 0 else None,
                    "decision_clarity": round(decision_clarity, 3),
                    "heavy_vs_normal": heavy_vs_normal,
                    "heavy_days_3": _safe_int(training_db.get("heavy_days_3"), 0),
                },
                "choice_context": {
                    "safest_option_label": str((anchors.get("safest") or {}).get("option_label") or ""),
                    "best_balance_label": str((anchors.get("best_balance") or {}).get("option_label") or ""),
                    "chosen_label": str((anchors.get("chosen") or {}).get("option_label") or chosen_mode),
                },
            },
            "night_cycle": {
                "ran": True,
                "completed_at": completed_at,
                "simulations_count": len(simulations),
                "clone_updated": True,
                "clone_snapshot_id": clone_snapshot_id,
                "future_lab": {
                    "hero_line": future_hero,
                    "morgen_status": chosen_morning,
                    "kpis": {
                        "reserve_morgen": chosen_reserve,
                        "hrv_morgen": chosen_hrv_tomorrow,
                        "muedigkeit_morgen": chosen_fatigue_tomorrow,
                        "recovery_kosten": chosen_cost,
                        "recovery_kosten_level": chosen_cost_level,
                        "reiz_heute": round(chosen_progression, 1),
                        "reiz_kosten_verhaeltnis": chosen_ratio,
                    },
                    "comparison": future_compare,
                    "scenario_projection": scenario_projection,
                    "today_signal_estimates": today_signal_estimates,
                    "forecast_adaptation": forecast_adaptation,
                    "clone_insight": {
                        "headline": "So reagiert dein Modell auf ähnliche Tage",
                        "points": clone_insight_points[:3],
                    },
                    "model_precision": {
                        **(model_precision if isinstance(model_precision, dict) else {}),
                        "latest_validation": feedback_latest or {},
                        "feedback_window_14d": feedback_summary_14d,
                        "feedback_window_60d": feedback_summary,
                    },
                    "prognose_sicherheit": {
                        "modellvertrauen": round(
                            _clamp(
                                (
                                    _safe_float((clone_profile.get("summary") or {}).get("trust_score"), 0.5) * 0.55
                                    + _safe_float(calibration_profile.get("model_reliability"), 0.5) * 0.45
                                )
                            ),
                            3,
                        ),
                        "calibration_shift": _safe_float(calibration_profile.get("calibration_shift"), 0.0),
                        "calibration_samples_60d": _safe_int(calibration_profile.get("samples"), 0),
                        "datengrundlage": {
                            "tage": _safe_int((clone_profile.get("summary") or {}).get("evidence_days"), 0),
                            "sessions": _safe_int((clone_profile.get("summary") or {}).get("evidence_sessions"), 0),
                            "recovery_zyklen": _safe_int((clone_profile.get("summary") or {}).get("recovery_cycles"), 0),
                            "aehnliche_tage": max(0, min(_safe_int((clone_profile.get("summary") or {}).get("evidence_days"), 0), _safe_int((clone_profile.get("summary") or {}).get("evidence_sessions"), 0) * 2)),
                        },
                    },
                    "anchors": {
                        "sicherste_zukunft": str((anchors.get("safest") or {}).get("option_label") or ""),
                        "beste_balance": str((anchors.get("best_balance") or {}).get("option_label") or ""),
                        "heute_gewaehlt": chosen_label,
                        "hoechster_reiz": str((highest_reiz or {}).get("option_label") or ""),
                        "hoechste_reserve_morgen": str((highest_reserve or {}).get("option_label") or ""),
                    },
                },
                "tomorrow_outlook": {
                    "headline": tomorrow_headline,
                    "detail": tomorrow_detail,
                    "frame": tomorrow_frame,
                    "expected_hrv_drop_risk": round(chosen_hrv_drop, 3),
                    "expected_readiness_delta": round(chosen_readiness_delta, 3),
                    "expected_fatigue_risk": round(chosen_fatigue_risk, 3),
                },
                "best_scenario": {
                    "scenario_key": str(simulations[0].get("scenario_key") if simulations else ""),
                    "label": str((anchors.get("safest") or {}).get("option_label") or (simulations[0].get("label") if simulations else "")),
                    "score": _safe_float(simulations[0].get("score"), 0.0) if simulations else 0.0,
                },
                "riskiest_scenario": {
                    "scenario_key": str(simulations[-1].get("scenario_key") if simulations else ""),
                    "label": str((anchors.get("highest_price") or {}).get("option_label") or (simulations[-1].get("label") if simulations else "")),
                    "score": _safe_float(simulations[-1].get("score"), 0.0) if simulations else 0.0,
                },
                "summary": "Nachtanalyse abgeschlossen.",
                "actions": [
                    "Daten neu bewertet",
                    "Optionen verglichen",
                    "Modell fein nachgezogen",
                ],
            },
            "clone": {
                "version": str(clone_profile.get("version") or "clone-v1"),
                "evidence_days": _safe_int((clone_profile.get("summary") or {}).get("evidence_days"), 0),
                "trust_score": _safe_float((clone_profile.get("summary") or {}).get("trust_score"), 0.5),
                "sensitivities": [
                    {"key": "sleep_sensitivity", "label": "Schlaf", "score": _safe_float((clone_profile.get("sensitivities") or {}).get("sleep_sensitivity"), 0.0)},
                    {"key": "hrv_sensitivity", "label": "Erholung", "score": _safe_float((clone_profile.get("sensitivities") or {}).get("hrv_sensitivity"), 0.0)},
                    {"key": "nutrition_sensitivity", "label": "Ernährung", "score": _safe_float((clone_profile.get("sensitivities") or {}).get("nutrition_sensitivity"), 0.0)},
                    {
                        "key": "run_interference_sensitivity",
                        "label": "Laufbelastung",
                        "score": _safe_float((clone_profile.get("sensitivities") or {}).get("run_interference_sensitivity"), 0.0),
                    },
                    {"key": "back_to_back_tolerance", "label": "Mehrere harte Tage", "score": _safe_float((clone_profile.get("sensitivities") or {}).get("back_to_back_tolerance"), 0.0)},
                ],
                "current_state": clone_profile.get("base_state") if isinstance(clone_profile.get("base_state"), dict) else {},
                "summary": str((clone_profile.get("summary") or {}).get("line") or ""),
                "basis": {
                    "days": _safe_int((clone_profile.get("summary") or {}).get("evidence_days"), 0),
                    "sessions": _safe_int((clone_profile.get("summary") or {}).get("evidence_sessions"), 0),
                    "recovery_cycles": _safe_int((clone_profile.get("summary") or {}).get("recovery_cycles"), 0),
                },
            },
            "scenarios": {
                "list": [
                    {
                        "scenario_key": str(row.get("scenario_key") or ""),
                        "label": str(row.get("option_label") or row.get("label") or ""),
                        "selected": bool(row.get("selected")) or str(row.get("scenario_key") or "") == chosen_scenario_key,
                        "performance_score": _safe_float(row.get("performance_probability_good"), 0.0),
                        "recovery_risk": _safe_float(row.get("recovery_risk"), 0.0),
                        "fatigue_risk": _safe_float(row.get("fatigue_risk"), 0.0),
                        "hrv_drop_risk": _safe_float(row.get("hrv_drop_risk"), 0.0),
                        "readiness_tomorrow_delta_estimate": _safe_float(row.get("readiness_tomorrow_delta_estimate"), 0.0),
                        "performance_bad_risk": _safe_float(row.get("performance_probability_bad"), 0.0),
                        "balance_score": _safe_float(row.get("balance_score"), 0.0),
                        "summary": str(row.get("summary") or ""),
                        "top_drivers": row.get("top_drivers") if isinstance(row.get("top_drivers"), list) else [],
                        "score": _safe_float(row.get("score"), 0.0),
                        "variants_count": _safe_int(row.get("variants_count"), 1),
                        "variant_keys": row.get("variant_keys") if isinstance(row.get("variant_keys"), list) else [],
                        "tags": [
                            tag
                            for tag in [
                                "sicherste Option"
                                if anchors.get("safest") and str((anchors.get("safest") or {}).get("scenario_key") or "") == str(row.get("scenario_key") or "")
                                else None,
                                "beste Balance"
                                if anchors.get("best_balance") and str((anchors.get("best_balance") or {}).get("scenario_key") or "") == str(row.get("scenario_key") or "")
                                else None,
                                "heute gewählt"
                                if str(row.get("scenario_key") or "") == chosen_scenario_key or bool(row.get("selected"))
                                else None,
                                "höchster Reiz"
                                if str(row.get("scenario_key") or "") == highest_reiz_key
                                else None,
                                "höchste Reserve morgen"
                                if str(row.get("scenario_key") or "") == highest_reserve_key
                                else None,
                                "höchster Preis"
                                if anchors.get("highest_price") and str((anchors.get("highest_price") or {}).get("scenario_key") or "") == str(row.get("scenario_key") or "")
                                else None,
                            ]
                            if tag
                        ],
                    }
                    for row in options
                ],
                "anchors": {
                    "safest_option_key": str((anchors.get("safest") or {}).get("scenario_key") or ""),
                    "safest_option_label": str((anchors.get("safest") or {}).get("option_label") or ""),
                    "best_balance_key": str((anchors.get("best_balance") or {}).get("scenario_key") or ""),
                    "best_balance_label": str((anchors.get("best_balance") or {}).get("option_label") or ""),
                    "chosen_key": str((anchors.get("chosen") or {}).get("scenario_key") or chosen_scenario_key),
                    "chosen_label": str((anchors.get("chosen") or {}).get("option_label") or chosen_mode),
                    "highest_reiz_key": highest_reiz_key,
                    "highest_reiz_label": str((highest_reiz or {}).get("option_label") or ""),
                    "highest_reserve_key": highest_reserve_key,
                    "highest_reserve_label": str((highest_reserve or {}).get("option_label") or ""),
                },
            },
            "learning": {
                "latest_updates": recent_updates,
                "trend_summary": str(tuning.get("trend_summary") or "Keine neue Kalibrierung."),
                "decision_quality_14d": quality,
                "quality_summary": quality_summary,
                "parameter_set": tuning.get("active_parameters") if isinstance(tuning.get("active_parameters"), dict) else parameters,
                "simulation_feedback": {
                    "latest": feedback_latest or {},
                    "window_60d": feedback_summary,
                },
                "auto_learning": {"ok": False, "status": "pending"},
            },
            "trace": {"steps": _trace_steps(completed_at)},
            "inputs": {
                "runtime_inputs": meta_bundle.get("runtime_inputs") if isinstance(meta_bundle.get("runtime_inputs"), dict) else {},
                "state_build": final_state,
                "daily_decision_context": daily_context,
            },
        }

        _store_explain_payload(conn, day_iso=day_iso, payload=payload)
        conn.commit()
        out_payload = payload
    finally:
        conn.close()

    if isinstance(out_payload, dict) and isinstance(out_payload.get("daily_decision"), dict):
        try:
            upsert_daily_decision(
                day_iso=day_iso,
                decision=out_payload.get("daily_decision") or {},
                readiness=(out_payload.get("daily_decision") or {}).get("data_readiness") if isinstance(out_payload.get("daily_decision"), dict) else {},
                raw_context=collect_daily_decision_context(day_iso, state=final_state, ns_payload=ns_payload),
            )
        except Exception:
            pass

    auto_learning: dict[str, Any]
    try:
        auto_learning = run_personal_learning_pass(
            coverage=_coverage_from_state_for_learning(final_state),
            as_of=date.fromisoformat(day_iso),
            lookback_days=180,
        )
    except Exception as exc:
        auto_learning = {"ok": False, "error": str(exc)}

    if isinstance(out_payload, dict):
        learning_block = out_payload.get("learning") if isinstance(out_payload.get("learning"), dict) else {}
        learning_block["auto_learning"] = auto_learning
        out_payload["learning"] = learning_block
        try:
            persist_conn = get_core_db()
            try:
                _store_explain_payload(persist_conn, day_iso=day_iso, payload=out_payload)
                persist_conn.commit()
            finally:
                persist_conn.close()
        except Exception:
            # Auto-Learning-Ergebnis ist trotzdem im Response enthalten.
            pass
        if day_iso == date.today().isoformat():
            try:
                maybe_send_core_morning_message(day_iso)
            except Exception:
                pass
        return out_payload
    return {"ok": False, "error": "night_cycle_failed"}
