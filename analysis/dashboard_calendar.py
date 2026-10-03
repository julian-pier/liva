from __future__ import annotations

from datetime import date, datetime, timedelta
from statistics import mean
from threading import Lock
from time import monotonic
from zoneinfo import ZoneInfo
import re
import sqlite3
from typing import Any

from analysis.progression_rules import SetPerformance, compare_set_progress


TZ_BERLIN = ZoneInfo("Europe/Berlin")
CACHE_TTL_SECONDS = 60.0

_MONTH_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_MONTH_V2_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_RANGE_V2_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_TIMELINE_V1_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_MONTH_LIVE_V1_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_CACHE_LOCK = Lock()


def berlin_today() -> date:
    return datetime.now(TZ_BERLIN).date()


def _coerce_iso_day(raw: Any) -> str | None:
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    if len(text) >= 10:
        maybe = text[:10]
        try:
            date.fromisoformat(maybe)
            return maybe
        except Exception:
            pass
    try:
        return datetime.fromisoformat(text).date().isoformat()
    except Exception:
        return None


def _to_float(raw: Any) -> float | None:
    if raw is None:
        return None
    try:
        val = float(raw)
    except Exception:
        return None
    if val != val:
        return None
    return val


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
        (table,),
    ).fetchone()
    return bool(row)


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    if not _table_exists(conn, table):
        return set()
    cols: set[str] = set()
    for row in conn.execute(f"PRAGMA table_info({table})").fetchall():
        name = row[1] if isinstance(row, tuple) else row["name"]
        if name:
            cols.add(str(name))
    return cols


def _first_existing(cols: set[str], candidates: list[str]) -> str | None:
    for cand in candidates:
        if cand in cols:
            return cand
    return None


def _month_start_from_param(month_raw: str | None, today: date) -> date:
    if month_raw and re.match(r"^\d{4}-\d{2}$", month_raw.strip()):
        y_s, m_s = month_raw.strip().split("-")
        y = int(y_s)
        m = int(m_s)
        if 1 <= m <= 12:
            return date(y, m, 1)
    return date(today.year, today.month, 1)


def _next_month_start(d: date) -> date:
    if d.month == 12:
        return date(d.year + 1, 1, 1)
    return date(d.year, d.month + 1, 1)


def _month_grid_bounds(month_start: date) -> tuple[date, date]:
    month_end = _next_month_start(month_start) - timedelta(days=1)
    grid_start = month_start - timedelta(days=month_start.weekday())
    grid_end = month_end + timedelta(days=(6 - month_end.weekday()))
    return grid_start, grid_end


def _iter_days(start: date, end: date) -> list[str]:
    out: list[str] = []
    cur = start
    while cur <= end:
        out.append(cur.isoformat())
        cur += timedelta(days=1)
    return out


def _fmt_tonnage_t(tonnage_kg: float | None) -> float | None:
    if tonnage_kg is None:
        return None
    if tonnage_kg <= 0:
        return 0.0
    return round(tonnage_kg / 1000.0, 2)


def _fmt_summary(hard_sets: int | None, tonnage_kg: float | None, run_min: float | None) -> str | None:
    parts: list[str] = []
    if hard_sets and hard_sets > 0:
        parts.append(f"{hard_sets} Sätze")
    ton_t = _fmt_tonnage_t(tonnage_kg)
    if ton_t is not None and ton_t > 0:
        parts.append(f"{ton_t:.2f}t")
    if run_min is not None and run_min > 0:
        parts.append(f"Run {int(round(run_min))} min")
    return " · ".join(parts) if parts else None


def _fmt_toplift(exercise: str | None, weight: float | None, reps: int | None, rpe: float | None) -> str | None:
    name = (exercise or "").strip()
    if not name or weight is None or reps is None:
        return None
    if rpe is None:
        return f"{name} {int(round(weight))}x{int(reps)}"
    rpe_txt = f"{rpe:.1f}".rstrip("0").rstrip(".")
    return f"{name} {int(round(weight))}x{int(reps)}@{rpe_txt}"


def _run_label(run: dict[str, Any] | None) -> str:
    if not isinstance(run, dict):
        return "Run"
    kind = str(run.get("run_type") or "").strip()
    return kind if kind else "Run"


def _is_truthy_text(raw: Any, *, mode: str) -> bool:
    if raw is None:
        return False
    text = str(raw).strip().lower()
    if not text:
        return False
    falsey = {"0", "false", "no", "none", "nothing", "nein", "healthy", "ok"}
    if text in falsey:
        return False
    if mode == "alcohol":
        if text in {"kein", "keine", "nichts"}:
            return False
        return True
    return True


def _load_actual_runs(
    runs_conn: sqlite3.Connection,
    training_conn: sqlite3.Connection,
    start_iso: str,
    end_iso: str,
) -> dict[str, dict[str, float]]:
    out: dict[str, dict[str, float]] = {}

    def add(day_iso: str, minutes: float | None, distance_km: float | None) -> None:
        if not day_iso:
            return
        entry = out.setdefault(day_iso, {"minutes": 0.0, "distance_km": 0.0})
        if minutes is not None and minutes > 0:
            entry["minutes"] += minutes
        if distance_km is not None and distance_km > 0:
            entry["distance_km"] += distance_km

    if _table_exists(runs_conn, "runs"):
        cols = _table_columns(runs_conn, "runs")
        date_col = _first_existing(cols, ["date", "date_iso", "day"])
        moving_col = _first_existing(cols, ["moving_time", "moving_time_s", "duration_s", "minutes"])
        distance_col = _first_existing(cols, ["distance", "distance_m", "km", "distance_km"])
        if date_col and (moving_col or distance_col):
            rows = runs_conn.execute(
                f"""
                SELECT {date_col} AS d,
                       {moving_col if moving_col else 'NULL'} AS moving,
                       {distance_col if distance_col else 'NULL'} AS dist
                FROM runs
                WHERE {date_col} IS NOT NULL
                  AND SUBSTR({date_col}, 1, 10) BETWEEN ? AND ?
                """,
                (start_iso, end_iso),
            ).fetchall()
            for row in rows:
                d = _coerce_iso_day(row["d"])
                if not d:
                    continue
                mv = _to_float(row["moving"])
                dist = _to_float(row["dist"])
                minutes = None
                if mv is not None:
                    minutes = mv / 60.0 if moving_col in {"moving_time", "moving_time_s", "duration_s"} else mv
                distance_km = None
                if dist is not None:
                    distance_km = dist / 1000.0 if distance_col in {"distance", "distance_m"} else dist
                add(d, minutes, distance_km)

    if _table_exists(training_conn, "runs"):
        cols = _table_columns(training_conn, "runs")
        date_col = _first_existing(cols, ["date_iso", "date", "day"])
        min_col = _first_existing(cols, ["minutes", "moving_time", "moving_time_s"])
        km_col = _first_existing(cols, ["km", "distance_km"])
        if date_col and (min_col or km_col):
            rows = training_conn.execute(
                f"""
                SELECT {date_col} AS d,
                       {min_col if min_col else 'NULL'} AS m,
                       {km_col if km_col else 'NULL'} AS km
                FROM runs
                WHERE {date_col} IS NOT NULL
                  AND SUBSTR({date_col}, 1, 10) BETWEEN ? AND ?
                """,
                (start_iso, end_iso),
            ).fetchall()
            for row in rows:
                d = _coerce_iso_day(row["d"])
                if not d:
                    continue
                mv = _to_float(row["m"])
                km = _to_float(row["km"])
                minutes = None
                if mv is not None:
                    minutes = mv / 60.0 if min_col in {"moving_time", "moving_time_s"} else mv
                add(d, minutes, km)

    return out


def _load_actual_workouts(
    training_conn: sqlite3.Connection,
    start_iso: str,
    end_iso: str,
) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}

    if not _table_exists(training_conn, "workouts"):
        return out

    workout_cols = _table_columns(training_conn, "workouts")
    date_col = _first_existing(workout_cols, ["date_iso", "date"])
    if not date_col:
        return out

    workouts = training_conn.execute(
        f"""
        SELECT id,
               SUBSTR({date_col}, 1, 10) AS day_iso,
               name,
               notes,
               created_at
        FROM workouts
        WHERE {date_col} IS NOT NULL
          AND SUBSTR({date_col}, 1, 10) BETWEEN ? AND ?
        ORDER BY SUBSTR({date_col}, 1, 10) ASC, created_at ASC, id ASC
        """,
        (start_iso, end_iso),
    ).fetchall()

    day_workout_ids: dict[str, list[int]] = {}
    for row in workouts:
        d = _coerce_iso_day(row["day_iso"])
        if not d:
            continue
        entry = out.setdefault(
            d,
            {
                "did_train": False,
                "session_title": None,
                "summary": None,
                "tonnage_kg": None,
                "hard_sets": None,
                "toplift": None,
                "notes": [],
            },
        )
        entry["did_train"] = True
        if row["name"]:
            entry["session_title"] = str(row["name"]).strip() or entry["session_title"]
        if row["notes"]:
            note = str(row["notes"]).strip()
            if note:
                entry["notes"].append(note)
        day_workout_ids.setdefault(d, []).append(int(row["id"]))

    has_sets = _table_exists(training_conn, "sets")
    has_exercises = _table_exists(training_conn, "exercises")
    if has_sets and has_exercises:
        rows = training_conn.execute(
            f"""
            SELECT
                SUBSTR(w.{date_col}, 1, 10) AS day_iso,
                SUM(CASE WHEN s.weight IS NOT NULL AND s.reps IS NOT NULL THEN (s.weight * s.reps) END) AS tonnage,
                SUM(CASE WHEN s.reps IS NOT NULL AND s.reps > 0 THEN 1 ELSE 0 END) AS hard_sets
            FROM workouts w
            LEFT JOIN exercises e ON e.workout_id = w.id
            LEFT JOIN sets s ON s.exercise_id = e.id
            WHERE w.{date_col} IS NOT NULL
              AND SUBSTR(w.{date_col}, 1, 10) BETWEEN ? AND ?
            GROUP BY SUBSTR(w.{date_col}, 1, 10)
            """,
            (start_iso, end_iso),
        ).fetchall()

        for row in rows:
            d = _coerce_iso_day(row["day_iso"])
            if not d:
                continue
            entry = out.setdefault(d, {"did_train": True})
            tonnage_kg = _to_float(row["tonnage"])
            hard_sets = _to_float(row["hard_sets"])
            entry["tonnage_kg"] = tonnage_kg if tonnage_kg is not None else None
            entry["hard_sets"] = int(hard_sets) if hard_sets is not None else None

        tops = training_conn.execute(
            f"""
            SELECT
                SUBSTR(w.{date_col}, 1, 10) AS day_iso,
                e.name AS exercise,
                s.weight AS weight,
                s.reps AS reps,
                s.rpe AS rpe,
                (s.weight * (1.0 + (s.reps / 30.0))) AS e1rm
            FROM workouts w
            JOIN exercises e ON e.workout_id = w.id
            JOIN sets s ON s.exercise_id = e.id
            WHERE w.{date_col} IS NOT NULL
              AND SUBSTR(w.{date_col}, 1, 10) BETWEEN ? AND ?
              AND s.weight IS NOT NULL
              AND s.reps IS NOT NULL
              AND s.reps > 0
            ORDER BY SUBSTR(w.{date_col}, 1, 10) ASC, e1rm DESC
            """,
            (start_iso, end_iso),
        ).fetchall()

        seen: set[str] = set()
        for row in tops:
            d = _coerce_iso_day(row["day_iso"])
            if not d or d in seen:
                continue
            seen.add(d)
            entry = out.setdefault(d, {"did_train": True})
            exercise = str(row["exercise"] or "").strip() if row["exercise"] is not None else None
            weight = _to_float(row["weight"])
            reps = int(row["reps"]) if row["reps"] is not None else None
            rpe = _to_float(row["rpe"])
            entry["toplift"] = _fmt_toplift(exercise, weight, reps, rpe)

    for d, entry in out.items():
        entry.setdefault("did_train", True)
        entry.setdefault("session_title", None)
        entry.setdefault("tonnage_kg", None)
        entry.setdefault("hard_sets", None)
        entry.setdefault("toplift", None)
        entry["summary"] = _fmt_summary(entry.get("hard_sets"), entry.get("tonnage_kg"), None)

    return out


def _week_state_for_date(plan: dict[str, Any], target_day: date) -> dict[str, Any] | None:
    if not isinstance(plan, dict):
        return None
    meta = plan.get("meta") if isinstance(plan.get("meta"), dict) else {}
    start_iso = (meta.get("start_date") or plan.get("start_date") or "").strip()
    if not start_iso:
        return None

    try:
        start_d = date.fromisoformat(start_iso)
    except Exception:
        return None

    block_length = int(meta.get("block_length") or plan.get("block_length") or 4)
    if block_length < 1:
        block_length = 4

    weekday = start_d.weekday()
    week1_start = start_d if weekday == 0 else (start_d + timedelta(days=(7 - weekday)))
    if target_day < week1_start:
        week_current = 1
    else:
        week_current = ((target_day - week1_start).days // 7) + 1

    if meta.get("week_override") is not None:
        try:
            week_current = int(meta.get("week_override"))
        except Exception:
            pass

    weeks = plan.get("weeks") if isinstance(plan.get("weeks"), list) else []
    week_entry = None
    if weeks:
        idx = max(0, week_current - 1)
        if idx >= len(weeks):
            idx = idx % len(weeks)
        row = weeks[idx]
        if isinstance(row, dict):
            week_entry = row

    explicit_type = None
    week_override_type = meta.get("week_type_override")
    if isinstance(week_override_type, str) and week_override_type.strip():
        explicit_type = week_override_type.strip().lower()
    elif isinstance(week_entry, dict):
        if isinstance(week_entry.get("week_type"), str) and week_entry.get("week_type").strip():
            explicit_type = week_entry.get("week_type").strip().lower()
        elif week_entry.get("deload") in (1, True):
            explicit_type = "deload"

    if explicit_type is None:
        week_in_block = ((week_current - 1) % block_length) + 1
        if week_in_block == block_length:
            explicit_type = "deload"
        elif week_in_block == (block_length - 1):
            explicit_type = "overreach"
        else:
            explicit_type = "build"

    return {
        "week_current": week_current,
        "week_type": explicit_type,
        "strength_volume_factor": _to_float((week_entry or {}).get("strength_volume_factor")) if week_entry else None,
    }


def _estimate_strength_sets(day_plan: dict[str, Any]) -> float | None:
    if not isinstance(day_plan, dict):
        return None
    rows = day_plan.get("strength_exercises") if isinstance(day_plan.get("strength_exercises"), list) else []
    if not rows:
        return 0.0
    total = 0.0
    for row in rows:
        if not isinstance(row, dict):
            continue
        sets_v = _to_float(row.get("sets"))
        if sets_v is None:
            total += 1.0
        else:
            total += max(0.0, sets_v)
    return total


def _build_plan_day_map(plan: dict[str, Any] | None, days: list[str]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    if not isinstance(plan, dict):
        return out

    base_week = plan.get("base_week") if isinstance(plan.get("base_week"), list) else []

    planned_strength_vals: list[float] = []
    for day_plan in base_week:
        if not isinstance(day_plan, dict):
            continue
        v = _estimate_strength_sets(day_plan)
        if v is not None and v > 0:
            planned_strength_vals.append(v)
    baseline_strength = mean(planned_strength_vals) if planned_strength_vals else None

    for day_iso in days:
        try:
            day_obj = date.fromisoformat(day_iso)
        except Exception:
            continue
        idx = day_obj.weekday()
        plan_day = base_week[idx] if idx < len(base_week) and isinstance(base_week[idx], dict) else {}

        strength_rows = plan_day.get("strength_exercises") if isinstance(plan_day.get("strength_exercises"), list) else []
        run_rows = plan_day.get("run_sessions") if isinstance(plan_day.get("run_sessions"), list) else []
        has_strength = bool(strength_rows or (str(plan_day.get("session_name") or "").strip()))
        has_run = bool(run_rows)
        has_plan = bool(base_week)

        session_name = str(plan_day.get("session_name") or "").strip()
        if has_strength and has_run:
            plan_title = f"{session_name or 'Gym'} + {_run_label(run_rows[0] if run_rows else None)}"
            kind = "train"
        elif has_strength:
            plan_title = session_name or "Gym"
            kind = "train"
        elif has_run:
            plan_title = _run_label(run_rows[0] if run_rows else None)
            kind = "train"
        elif has_plan:
            plan_title = "Off"
            kind = "off"
        else:
            plan_title = None
            kind = None

        week_state = _week_state_for_date(plan, day_obj)
        deload = False
        overreach = False

        wtype = (week_state or {}).get("week_type")
        if isinstance(wtype, str):
            lw = wtype.strip().lower()
            if "deload" in lw:
                deload = True
            if "overreach" in lw or "functional" in lw:
                overreach = True

        planned_strength = _estimate_strength_sets(plan_day)
        vol_factor = _to_float((week_state or {}).get("strength_volume_factor"))
        if planned_strength is not None and vol_factor is not None:
            planned_strength *= vol_factor

        if not deload and not overreach and planned_strength is not None and baseline_strength and baseline_strength > 0:
            rel = planned_strength / baseline_strength
            if rel <= 0.65:
                deload = True
            elif rel >= 1.25:
                overreach = True

        out[day_iso] = {
            "has_plan": has_plan,
            "plan_title": plan_title,
            "kind": kind,
            "deload": deload,
            "overreach": overreach,
        }

    return out


def _load_hrv_flags_by_day(hrv_conn: sqlite3.Connection, start_iso: str, end_iso: str) -> dict[str, dict[str, bool]]:
    out: dict[str, dict[str, bool]] = {}
    if not _table_exists(hrv_conn, "hrv_measurements"):
        return out

    cols = _table_columns(hrv_conn, "hrv_measurements")
    has_sick_bool = "sickness_bool" in cols
    has_alc_bool = "alcohol_bool" in cols
    has_sick_txt = "sickness" in cols
    has_alc_txt = "alcohol" in cols

    sick_bool_expr = "sickness_bool" if has_sick_bool else "0 AS sickness_bool"
    alc_bool_expr = "alcohol_bool" if has_alc_bool else "0 AS alcohol_bool"
    sick_txt_expr = "sickness" if has_sick_txt else "NULL AS sickness"
    alc_txt_expr = "alcohol" if has_alc_txt else "NULL AS alcohol"

    rows = hrv_conn.execute(
        f"""
        SELECT
            SUBSTR(COALESCE(date_utc, ts_measurement), 1, 10) AS day_iso,
            {sick_bool_expr},
            {alc_bool_expr},
            {sick_txt_expr},
            {alc_txt_expr}
        FROM hrv_measurements
        WHERE SUBSTR(COALESCE(date_utc, ts_measurement), 1, 10) BETWEEN ? AND ?
        """,
        (start_iso, end_iso),
    ).fetchall()

    for row in rows:
        d = _coerce_iso_day(row["day_iso"])
        if not d:
            continue
        entry = out.setdefault(d, {"sick": False, "alcohol": False})
        if row["sickness_bool"] in (1, True):
            entry["sick"] = True
        if row["alcohol_bool"] in (1, True):
            entry["alcohol"] = True
        if _is_truthy_text(row["sickness"], mode="sickness"):
            entry["sick"] = True
        if _is_truthy_text(row["alcohol"], mode="alcohol"):
            entry["alcohol"] = True

    return out


def _build_month_payload_uncached(
    training_conn: sqlite3.Connection,
    runs_conn: sqlite3.Connection,
    hrv_conn: sqlite3.Connection,
    plan: dict[str, Any] | None,
    *,
    month_raw: str | None,
    today: date,
) -> dict[str, Any]:
    month_start = _month_start_from_param(month_raw, today)
    month_iso = month_start.strftime("%Y-%m")
    grid_start, grid_end = _month_grid_bounds(month_start)

    start_iso = grid_start.isoformat()
    end_iso = grid_end.isoformat()
    day_keys = _iter_days(grid_start, grid_end)

    workouts = _load_actual_workouts(training_conn, start_iso, end_iso)
    runs = _load_actual_runs(runs_conn, training_conn, start_iso, end_iso)
    plan_map = _build_plan_day_map(plan, day_keys)
    hrv_flags = _load_hrv_flags_by_day(hrv_conn, start_iso, end_iso)

    cells: list[dict[str, Any]] = []
    for d in day_keys:
        day_obj = date.fromisoformat(d)
        w = workouts.get(d, {})
        run_info = runs.get(d, {})
        run_minutes = _to_float(run_info.get("minutes"))

        did_train = bool(w.get("did_train")) or (run_minutes is not None and run_minutes > 0)
        session_title = w.get("session_title")
        if not session_title and run_minutes and run_minutes > 0:
            session_title = "Run"

        summary = w.get("summary")
        if not summary and run_minutes and run_minutes > 0:
            summary = f"Run {int(round(run_minutes))} min"
        elif summary and run_minutes and run_minutes > 0 and "Run" not in summary:
            summary = f"{summary} · Run {int(round(run_minutes))} min"

        p = plan_map.get(d, {})
        f = hrv_flags.get(d, {})

        cells.append(
            {
                "date": d,
                "in_month": (day_obj.month == month_start.month and day_obj.year == month_start.year),
                "dow": day_obj.weekday(),
                "day": day_obj.day,
                "actual": {
                    "did_train": did_train,
                    "session_title": session_title,
                    "summary": summary,
                    "tonnage_t": _fmt_tonnage_t(_to_float(w.get("tonnage_kg"))),
                    "hard_sets": int(w["hard_sets"]) if isinstance(w.get("hard_sets"), int) else (int(w.get("hard_sets")) if _to_float(w.get("hard_sets")) is not None else None),
                    "toplift": w.get("toplift"),
                },
                "planned": {
                    "has_plan": bool(p.get("has_plan")),
                    "plan_title": p.get("plan_title"),
                    "kind": p.get("kind"),
                },
                "phase": {
                    "deload": bool(p.get("deload")),
                    "overreach": bool(p.get("overreach")),
                },
                "flags": {
                    "sick": bool(f.get("sick")),
                    "alcohol": bool(f.get("alcohol")),
                },
            }
        )

    weeks: list[list[dict[str, Any]]] = []
    for i in range(0, len(cells), 7):
        weeks.append(cells[i : i + 7])

    return {
        "meta": {
            "month": month_iso,
            "tz": "Europe/Berlin",
            "today": today.isoformat(),
        },
        "weeks": weeks,
    }


def build_calendar_month_payload(
    training_conn: sqlite3.Connection,
    runs_conn: sqlite3.Connection,
    hrv_conn: sqlite3.Connection,
    plan: dict[str, Any] | None,
    *,
    month_raw: str | None,
    today: date | None = None,
) -> dict[str, Any]:
    today_d = today or berlin_today()
    month_start = _month_start_from_param(month_raw, today_d)
    month_iso = month_start.strftime("%Y-%m")

    now_m = monotonic()
    with _CACHE_LOCK:
        cached = _MONTH_CACHE.get(month_iso)
        if cached and (now_m - cached[0]) <= CACHE_TTL_SECONDS:
            return cached[1]

    payload = _build_month_payload_uncached(
        training_conn,
        runs_conn,
        hrv_conn,
        plan,
        month_raw=month_iso,
        today=today_d,
    )

    with _CACHE_LOCK:
        _MONTH_CACHE[month_iso] = (monotonic(), payload)
    return payload


def _day_workout_details(training_conn: sqlite3.Connection, day_iso: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not _table_exists(training_conn, "workouts"):
        return out

    workout_cols = _table_columns(training_conn, "workouts")
    date_col = _first_existing(workout_cols, ["date_iso", "date"])
    if not date_col:
        return out

    workouts = training_conn.execute(
        f"""
        SELECT id, name, notes
        FROM workouts
        WHERE SUBSTR({date_col}, 1, 10) = ?
        ORDER BY created_at ASC, id ASC
        """,
        (day_iso,),
    ).fetchall()

    for w in workouts:
        wid = int(w["id"])
        metrics = training_conn.execute(
            """
            SELECT
                SUM(CASE WHEN s.weight IS NOT NULL AND s.reps IS NOT NULL THEN (s.weight * s.reps) END) AS tonnage,
                SUM(CASE WHEN s.reps IS NOT NULL AND s.reps > 0 THEN 1 ELSE 0 END) AS hard_sets,
                AVG(CASE WHEN s.rpe IS NOT NULL THEN s.rpe END) AS avg_rpe
            FROM sets s
            JOIN exercises e ON e.id = s.exercise_id
            WHERE e.workout_id = ?
            """,
            (wid,),
        ).fetchone()

        top = training_conn.execute(
            """
            SELECT e.name AS exercise, s.weight, s.reps, s.rpe,
                   (s.weight * (1.0 + (s.reps / 30.0))) AS e1rm
            FROM sets s
            JOIN exercises e ON e.id = s.exercise_id
            WHERE e.workout_id = ?
              AND s.weight IS NOT NULL
              AND s.reps IS NOT NULL
              AND s.reps > 0
            ORDER BY e1rm DESC
            LIMIT 1
            """,
            (wid,),
        ).fetchone()

        tonnage_kg = _to_float(metrics["tonnage"]) if metrics is not None else None
        hard_sets = int(metrics["hard_sets"]) if metrics is not None and _to_float(metrics["hard_sets"]) is not None else None
        avg_rpe = _to_float(metrics["avg_rpe"]) if metrics is not None else None
        toplift = None
        if top is not None:
            toplift = _fmt_toplift(
                str(top["exercise"] or "").strip() if top["exercise"] is not None else None,
                _to_float(top["weight"]),
                int(top["reps"]) if top["reps"] is not None else None,
                _to_float(top["rpe"]),
            )

        out.append(
            {
                "id": wid,
                "title": str(w["name"] or "").strip() or "Workout",
                "note": str(w["notes"] or "").strip() or None,
                "hard_sets": hard_sets,
                "tonnage_t": _fmt_tonnage_t(tonnage_kg),
                "avg_rpe": round(avg_rpe, 2) if avg_rpe is not None else None,
                "toplift": toplift,
                "summary": _fmt_summary(hard_sets, tonnage_kg, None),
            }
        )

    return out


def _day_run_details(runs_conn: sqlite3.Connection, training_conn: sqlite3.Connection, day_iso: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []

    if _table_exists(runs_conn, "runs"):
        cols = _table_columns(runs_conn, "runs")
        date_col = _first_existing(cols, ["date", "date_iso", "day"])
        moving_col = _first_existing(cols, ["moving_time", "moving_time_s", "duration_s", "minutes"])
        distance_col = _first_existing(cols, ["distance", "distance_m", "km", "distance_km"])
        if date_col and (moving_col or distance_col):
            rows = runs_conn.execute(
                f"""
                SELECT {moving_col if moving_col else 'NULL'} AS moving,
                       {distance_col if distance_col else 'NULL'} AS dist,
                       avg_hr,
                       pace
                FROM runs
                WHERE SUBSTR({date_col}, 1, 10) = ?
                """,
                (day_iso,),
            ).fetchall()
            for row in rows:
                mv = _to_float(row["moving"])
                dist = _to_float(row["dist"])
                minutes = None
                if mv is not None:
                    minutes = mv / 60.0 if moving_col in {"moving_time", "moving_time_s", "duration_s"} else mv
                distance_km = None
                if dist is not None:
                    distance_km = dist / 1000.0 if distance_col in {"distance", "distance_m"} else dist
                out.append(
                    {
                        "source": "strava",
                        "duration_min": round(minutes, 1) if minutes is not None else None,
                        "distance_km": round(distance_km, 2) if distance_km is not None else None,
                        "avg_hr": _to_float(row["avg_hr"]),
                        "pace_s": _to_float(row["pace"]),
                    }
                )

    if _table_exists(training_conn, "runs"):
        cols = _table_columns(training_conn, "runs")
        date_col = _first_existing(cols, ["date_iso", "date", "day"])
        min_col = _first_existing(cols, ["minutes", "moving_time", "moving_time_s"])
        km_col = _first_existing(cols, ["km", "distance_km"])
        if date_col and (min_col or km_col):
            rows = training_conn.execute(
                f"""
                SELECT {min_col if min_col else 'NULL'} AS minutes,
                       {km_col if km_col else 'NULL'} AS km
                FROM runs
                WHERE SUBSTR({date_col}, 1, 10) = ?
                """,
                (day_iso,),
            ).fetchall()
            for row in rows:
                mv = _to_float(row["minutes"])
                km = _to_float(row["km"])
                minutes = mv / 60.0 if mv is not None and min_col in {"moving_time", "moving_time_s"} else mv
                out.append(
                    {
                        "source": "manual",
                        "duration_min": round(minutes, 1) if minutes is not None else None,
                        "distance_km": round(km, 2) if km is not None else None,
                        "avg_hr": None,
                        "pace_s": None,
                    }
                )

    return out


def build_calendar_day_payload(
    training_conn: sqlite3.Connection,
    runs_conn: sqlite3.Connection,
    hrv_conn: sqlite3.Connection,
    plan: dict[str, Any] | None,
    *,
    day_raw: str,
    today: date | None = None,
) -> dict[str, Any]:
    today_d = today or berlin_today()
    target_iso = _coerce_iso_day(day_raw)
    if not target_iso:
        target_iso = today_d.isoformat()

    month_payload = build_calendar_month_payload(
        training_conn,
        runs_conn,
        hrv_conn,
        plan,
        month_raw=target_iso[:7],
        today=today_d,
    )

    selected = None
    for week in month_payload.get("weeks") or []:
        for cell in week or []:
            if cell.get("date") == target_iso:
                selected = cell
                break
        if selected:
            break

    if selected is None:
        target_d = date.fromisoformat(target_iso)
        selected = {
            "date": target_iso,
            "in_month": True,
            "dow": target_d.weekday(),
            "day": target_d.day,
            "actual": {"did_train": False, "session_title": None, "summary": None, "tonnage_t": None, "hard_sets": None, "toplift": None},
            "planned": {"has_plan": False, "plan_title": None, "kind": None},
            "phase": {"deload": False, "overreach": False},
            "flags": {"sick": False, "alcohol": False},
        }

    workouts = _day_workout_details(training_conn, target_iso)
    runs = _day_run_details(runs_conn, training_conn, target_iso)

    total_run_min = sum((_to_float(r.get("duration_min")) or 0.0) for r in runs)
    workout_summary = selected["actual"].get("summary")
    if not workout_summary and total_run_min > 0:
        workout_summary = f"Run {int(round(total_run_min))} min"

    weekday_names = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]
    day_obj = date.fromisoformat(target_iso)

    return {
        "meta": {
            "date": target_iso,
            "tz": "Europe/Berlin",
            "today": today_d.isoformat(),
        },
        "day": {
            "date": target_iso,
            "weekday": weekday_names[day_obj.weekday()],
            "planned": selected.get("planned") or {},
            "actual": {
                **(selected.get("actual") or {}),
                "summary": workout_summary,
                "workouts": workouts,
                "runs": runs,
            },
            "phase": selected.get("phase") or {},
            "flags": selected.get("flags") or {},
        },
    }


def _load_hrv_flags_and_presence_by_day(
    hrv_conn: sqlite3.Connection,
    start_iso: str,
    end_iso: str,
) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    if not _table_exists(hrv_conn, "hrv_measurements"):
        return out

    cols = _table_columns(hrv_conn, "hrv_measurements")
    has_sick_bool = "sickness_bool" in cols
    has_alc_bool = "alcohol_bool" in cols
    has_sick_txt = "sickness" in cols
    has_alc_txt = "alcohol" in cols

    sick_bool_expr = "sickness_bool" if has_sick_bool else "0 AS sickness_bool"
    alc_bool_expr = "alcohol_bool" if has_alc_bool else "0 AS alcohol_bool"
    sick_txt_expr = "sickness" if has_sick_txt else "NULL AS sickness"
    alc_txt_expr = "alcohol" if has_alc_txt else "NULL AS alcohol"

    rows = hrv_conn.execute(
        f"""
        SELECT
            SUBSTR(COALESCE(date_utc, ts_measurement), 1, 10) AS day_iso,
            {sick_bool_expr},
            {alc_bool_expr},
            {sick_txt_expr},
            {alc_txt_expr},
            rmssd,
            hr
        FROM hrv_measurements
        WHERE SUBSTR(COALESCE(date_utc, ts_measurement), 1, 10) BETWEEN ? AND ?
        """,
        (start_iso, end_iso),
    ).fetchall()

    for row in rows:
        d = _coerce_iso_day(row["day_iso"])
        if not d:
            continue
        entry = out.setdefault(d, {"sick": False, "alcohol": False, "has_recovery": False, "_rmssd_vals": [], "_rhr_vals": []})
        if row["sickness_bool"] in (1, True):
            entry["sick"] = True
        if row["alcohol_bool"] in (1, True):
            entry["alcohol"] = True
        if _is_truthy_text(row["sickness"], mode="sickness"):
            entry["sick"] = True
        if _is_truthy_text(row["alcohol"], mode="alcohol"):
            entry["alcohol"] = True
        rmssd_v = _to_float(row["rmssd"])
        rhr_v = _to_float(row["hr"])
        if rmssd_v is not None:
            entry["_rmssd_vals"].append(rmssd_v)
        if rhr_v is not None:
            entry["_rhr_vals"].append(rhr_v)
        if rmssd_v is not None or rhr_v is not None:
            entry["has_recovery"] = True

    for entry in out.values():
        rmssd_vals = entry.pop("_rmssd_vals", [])
        rhr_vals = entry.pop("_rhr_vals", [])
        entry["rmssd"] = (sum(rmssd_vals) / len(rmssd_vals)) if rmssd_vals else None
        entry["rhr"] = (sum(rhr_vals) / len(rhr_vals)) if rhr_vals else None

    return out


def _load_activity_daily_v2(
    training_conn: sqlite3.Connection,
    runs_conn: sqlite3.Connection,
    start_iso: str,
    end_iso: str,
) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}

    if _table_exists(training_conn, "workouts"):
        workout_cols = _table_columns(training_conn, "workouts")
        date_col = _first_existing(workout_cols, ["date_iso", "date"])
        if date_col and _table_exists(training_conn, "sets") and _table_exists(training_conn, "exercises"):
            rows = training_conn.execute(
                f"""
                SELECT
                    SUBSTR(w.{date_col}, 1, 10) AS day_iso,
                    SUM(CASE WHEN s.weight IS NOT NULL AND s.reps IS NOT NULL THEN s.weight * s.reps END) AS tonnage,
                    SUM(CASE WHEN s.reps IS NOT NULL AND s.reps > 0 THEN 1 ELSE 0 END) AS hard_sets,
                    AVG(CASE WHEN s.rpe IS NOT NULL THEN s.rpe END) AS avg_rpe
                FROM workouts w
                LEFT JOIN exercises e ON e.workout_id = w.id
                LEFT JOIN sets s ON s.exercise_id = e.id
                WHERE w.{date_col} IS NOT NULL
                  AND SUBSTR(w.{date_col}, 1, 10) BETWEEN ? AND ?
                GROUP BY SUBSTR(w.{date_col}, 1, 10)
                """,
                (start_iso, end_iso),
            ).fetchall()
            for row in rows:
                d = _coerce_iso_day(row["day_iso"])
                if not d:
                    continue
                entry = out.setdefault(d, {})
                entry["strength_tonnage"] = _to_float(row["tonnage"]) or 0.0
                hs = _to_float(row["hard_sets"])
                entry["hard_sets"] = int(hs) if hs is not None else 0
                entry["avg_rpe"] = _to_float(row["avg_rpe"])

            names = training_conn.execute(
                f"""
                SELECT day_iso, name FROM (
                  SELECT SUBSTR({date_col}, 1, 10) AS day_iso,
                         name,
                         ROW_NUMBER() OVER (
                           PARTITION BY SUBSTR({date_col}, 1, 10)
                           ORDER BY created_at DESC, id DESC
                         ) AS rn
                  FROM workouts
                  WHERE {date_col} IS NOT NULL
                    AND SUBSTR({date_col}, 1, 10) BETWEEN ? AND ?
                )
                WHERE rn = 1
                """,
                (start_iso, end_iso),
            ).fetchall()
            for row in names:
                d = _coerce_iso_day(row["day_iso"])
                if d:
                    out.setdefault(d, {})["title"] = str(row["name"] or "").strip() or None

            tops = training_conn.execute(
                f"""
                SELECT day_iso, exercise, weight, reps, rpe, e1rm FROM (
                  SELECT
                    SUBSTR(w.{date_col}, 1, 10) AS day_iso,
                    e.name AS exercise,
                    s.weight AS weight,
                    s.reps AS reps,
                    s.rpe AS rpe,
                    (s.weight * (1.0 + (s.reps / 30.0))) AS e1rm,
                    ROW_NUMBER() OVER (
                      PARTITION BY SUBSTR(w.{date_col}, 1, 10)
                      ORDER BY (s.weight * (1.0 + (s.reps / 30.0))) DESC
                    ) AS rn
                  FROM workouts w
                  JOIN exercises e ON e.workout_id = w.id
                  JOIN sets s ON s.exercise_id = e.id
                  WHERE w.{date_col} IS NOT NULL
                    AND SUBSTR(w.{date_col}, 1, 10) BETWEEN ? AND ?
                    AND s.weight IS NOT NULL
                    AND s.reps IS NOT NULL
                    AND s.reps > 0
                )
                WHERE rn = 1
                """,
                (start_iso, end_iso),
            ).fetchall()
            for row in tops:
                d = _coerce_iso_day(row["day_iso"])
                if d:
                    e1rm_v = _to_float(row["e1rm"])
                    out.setdefault(d, {})["toplift"] = _fmt_toplift(
                        str(row["exercise"] or "").strip() if row["exercise"] is not None else None,
                        _to_float(row["weight"]),
                        int(row["reps"]) if row["reps"] is not None else None,
                        _to_float(row["rpe"]),
                    )
                    if e1rm_v is not None and e1rm_v > 0:
                        out.setdefault(d, {})["performance_score"] = e1rm_v

    run_map = _load_actual_runs(runs_conn, training_conn, start_iso, end_iso)
    for d, rv in run_map.items():
        entry = out.setdefault(d, {})
        entry["run_minutes"] = _to_float(rv.get("minutes")) or 0.0
        entry["run_distance_km"] = _to_float(rv.get("distance_km")) or 0.0

    for d, entry in out.items():
        ton = _to_float(entry.get("strength_tonnage")) or 0.0
        hs = int(entry.get("hard_sets") or 0)
        run_min = _to_float(entry.get("run_minutes")) or 0.0
        entry["did_strength"] = bool(ton > 0 or hs > 0)
        entry["did_run"] = bool(run_min > 0)
        entry["did_any"] = bool(entry["did_strength"] or entry["did_run"])
        if entry.get("performance_score") is None and entry["did_strength"] and entry.get("strength_tonnage"):
            # fallback proxy: tonnage-scaled index if no clean top-set/e1RM was found
            entry["performance_score"] = float(entry.get("strength_tonnage") or 0.0) ** 0.5

    return out


def _rolling_sum(vals: list[float], window: int) -> list[float]:
    out: list[float] = []
    running = 0.0
    w = max(1, int(window))
    for i, v in enumerate(vals):
        running += float(v or 0.0)
        if i >= w:
            running -= float(vals[i - w] or 0.0)
        out.append(running)
    return out


def _rolling_mean_nullable(vals: list[float | None], window: int) -> list[float | None]:
    out: list[float | None] = []
    w = max(1, int(window))
    for i in range(len(vals)):
        s = max(0, i - w + 1)
        chunk = [v for v in vals[s : i + 1] if v is not None]
        if not chunk:
            out.append(None)
        else:
            out.append(sum(chunk) / float(len(chunk)))
    return out


def _rolling_count_true(vals: list[bool], window: int) -> list[int]:
    out: list[int] = []
    w = max(1, int(window))
    run = 0
    for i, v in enumerate(vals):
        run += 1 if v else 0
        if i >= w and vals[i - w]:
            run -= 1
        out.append(run)
    return out


def _linear_slope(values: list[float]) -> float | None:
    if len(values) < 5:
        return None
    n = len(values)
    x_mean = (n - 1) / 2.0
    y_mean = sum(values) / float(n)
    num = 0.0
    den = 0.0
    for i, y in enumerate(values):
        dx = i - x_mean
        num += dx * (y - y_mean)
        den += dx * dx
    if den <= 0:
        return None
    return num / den


def _build_spans(
    dates: list[str],
    marks: dict[str, bool],
    *,
    kind: str,
    min_len: int,
    label: str,
    color_key: str,
    confidence: str = "med",
) -> list[dict[str, Any]]:
    spans: list[dict[str, Any]] = []
    start = None
    for idx, d in enumerate(dates):
        flagged = bool(marks.get(d))
        if flagged and start is None:
            start = d
        if (not flagged) and start is not None:
            end = dates[idx - 1] if idx > 0 else start
            n = (date.fromisoformat(end) - date.fromisoformat(start)).days + 1
            if n >= min_len:
                spans.append(
                    {
                        "kind": kind,
                        "start": start,
                        "end": end,
                        "label": label,
                        "color_key": color_key,
                        "confidence": confidence,
                    }
                )
            start = None
    if start is not None and dates:
        end = dates[-1]
        n = (date.fromisoformat(end) - date.fromisoformat(start)).days + 1
        if n >= min_len:
            spans.append(
                {
                    "kind": kind,
                    "start": start,
                    "end": end,
                    "label": label,
                    "color_key": color_key,
                    "confidence": confidence,
                }
            )
    return spans


def _span_day_set(spans: list[dict[str, Any]]) -> set[str]:
    out: set[str] = set()
    for span in spans:
        s = date.fromisoformat(span["start"])
        e = date.fromisoformat(span["end"])
        cur = s
        while cur <= e:
            out.add(cur.isoformat())
            cur += timedelta(days=1)
    return out


def _phase_spans_v2(
    all_dates: list[str],
    grid_dates: set[str],
    daily: dict[str, dict[str, Any]],
    hrv_flags: dict[str, dict[str, Any]],
    *,
    today: date | None = None,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]], dict[str, Any]]:
    today_iso = (today or berlin_today()).isoformat()
    tonnage = [_to_float((daily.get(d) or {}).get("strength_tonnage")) or 0.0 for d in all_dates]
    hard_sets = [int((daily.get(d) or {}).get("hard_sets") or 0) for d in all_dates]
    avg_rpe = [_to_float((daily.get(d) or {}).get("avg_rpe")) for d in all_dates]
    perf = [_to_float((daily.get(d) or {}).get("performance_score")) for d in all_dates]
    did_strength = [bool((daily.get(d) or {}).get("did_strength")) for d in all_dates]
    did_any = [bool((daily.get(d) or {}).get("did_any")) for d in all_dates]
    rmssd = [_to_float((hrv_flags.get(d) or {}).get("rmssd")) for d in all_dates]
    rhr = [_to_float((hrv_flags.get(d) or {}).get("rhr")) for d in all_dates]

    vol7 = _rolling_sum(tonnage, 7)
    sets7 = _rolling_sum([float(v) for v in hard_sets], 7)
    rpe7 = _rolling_mean_nullable(avg_rpe, 7)
    perf14 = _rolling_mean_nullable(perf, 14)
    rmssd7 = _rolling_mean_nullable(rmssd, 7)
    rhr7 = _rolling_mean_nullable(rhr, 7)
    train_days7 = _rolling_sum([1.0 if x else 0.0 for x in did_strength], 7)
    any_days7 = _rolling_count_true(did_any, 7)
    rpe_points7 = _rolling_count_true([v is not None for v in avg_rpe], 7)

    baseline_days = max(1, len(all_dates) - len(grid_dates))
    baseline_idx = list(range(0, baseline_days))

    base_vol_candidates = [vol7[i] for i in baseline_idx if train_days7[i] >= 3 and vol7[i] > 0]
    baseline_vol = (sum(base_vol_candidates) / len(base_vol_candidates)) if base_vol_candidates else None

    base_rpe_candidates = [avg_rpe[i] for i in baseline_idx if did_strength[i] and avg_rpe[i] is not None]
    baseline_rpe = (sum(base_rpe_candidates) / len(base_rpe_candidates)) if base_rpe_candidates else None
    base_sets_candidates = [sets7[i] for i in baseline_idx if train_days7[i] >= 3 and sets7[i] > 0]
    baseline_sets = (sum(base_sets_candidates) / len(base_sets_candidates)) if base_sets_candidates else None

    base_perf_candidates = [perf14[i] for i in baseline_idx if perf14[i] is not None]
    baseline_perf = (sum(base_perf_candidates) / len(base_perf_candidates)) if base_perf_candidates else None

    base_rmssd_candidates = [rmssd7[i] for i in baseline_idx if rmssd7[i] is not None]
    baseline_rmssd = (sum(base_rmssd_candidates) / len(base_rmssd_candidates)) if base_rmssd_candidates else None
    base_rhr_candidates = [rhr7[i] for i in baseline_idx if rhr7[i] is not None]
    baseline_rhr = (sum(base_rhr_candidates) / len(base_rhr_candidates)) if base_rhr_candidates else None

    deload_raw: dict[str, bool] = {}
    deload_low_rpe_raw: dict[str, bool] = {}
    deload_no_sessions_raw: dict[str, bool] = {}
    overreach_raw: dict[str, bool] = {}
    overreach_track_a_raw: dict[str, bool] = {}
    overreach_track_b_raw: dict[str, bool] = {}
    overreach_track_c_raw: dict[str, bool] = {}

    day_debug: dict[str, dict[str, Any]] = {}

    for i, d in enumerate(all_dates):
        vr = None
        if baseline_vol is not None and baseline_vol > 0:
            vr = vol7[i] / baseline_vol
        rd = None
        if baseline_rpe is not None and rpe7[i] is not None:
            rd = rpe7[i] - baseline_rpe
        sets_ratio = None
        if baseline_sets is not None and baseline_sets > 0:
            sets_ratio = sets7[i] / baseline_sets
        perf_ratio = None
        if baseline_perf is not None and baseline_perf > 0 and perf14[i] is not None:
            perf_ratio = perf14[i] / baseline_perf
        rmssd_delta = None
        if baseline_rmssd is not None and baseline_rmssd > 0 and rmssd7[i] is not None:
            rmssd_delta = (rmssd7[i] - baseline_rmssd) / baseline_rmssd
        rhr_delta = None
        if baseline_rhr is not None and baseline_rhr > 0 and rhr7[i] is not None:
            rhr_delta = (rhr7[i] - baseline_rhr) / baseline_rhr

        recent_perf = [v for v in perf14[max(0, i - 13) : i + 1] if v is not None]
        perf_slope = _linear_slope(recent_perf) if len(recent_perf) >= 6 else None

        deload = False
        overreach = False
        if d <= today_iso and rpe7[i] is not None and rpe_points7[i] >= 3:
            deload = rpe7[i] < 7.0
        if vr is not None and train_days7[i] >= 3:
            if rd is not None:
                overreach_a = (vr >= 1.20) and (rd >= 0.3)
            else:
                overreach_a = vr >= 1.35
        else:
            overreach_a = False

        overreach_b = False
        if perf_ratio is not None:
            perf_drop = perf_ratio <= 0.97
            perf_stag = perf_ratio <= 1.00 and (perf_slope is not None and perf_slope <= 0.0)
            load_ctx = ((vr is not None and vr >= 1.10) or (rd is not None and rd >= 0.2))
            overreach_b = (perf_drop or perf_stag) and load_ctx

        overreach_c = False
        if rmssd_delta is not None and rhr_delta is not None and vr is not None:
            overreach_c = (rmssd_delta <= -0.08) and (rhr_delta >= 0.04) and (vr >= 1.10)

        overreach = bool(overreach_a or overreach_b or overreach_c)

        deload_raw[d] = bool(deload)
        deload_low_rpe_raw[d] = bool(deload)
        overreach_raw[d] = bool(overreach)
        overreach_track_a_raw[d] = bool(overreach_a)
        overreach_track_b_raw[d] = bool(overreach_b)
        overreach_track_c_raw[d] = bool(overreach_c)
        day_debug[d] = {
            "vol7": round(vol7[i], 2),
            "sets7": round(sets7[i], 2),
            "rpe7": round(rpe7[i], 3) if rpe7[i] is not None else None,
            "vol_ratio": round(vr, 3) if vr is not None else None,
            "sets_ratio": round(sets_ratio, 3) if sets_ratio is not None else None,
            "rpe_delta": round(rd, 3) if rd is not None else None,
            "perf14": round(perf14[i], 3) if perf14[i] is not None else None,
            "perf_ratio": round(perf_ratio, 3) if perf_ratio is not None else None,
            "perf_slope": round(perf_slope, 5) if perf_slope is not None else None,
            "rmssd_delta": round(rmssd_delta, 3) if rmssd_delta is not None else None,
            "rhr_delta": round(rhr_delta, 3) if rhr_delta is not None else None,
            "overreach_a": overreach_a,
            "overreach_b": overreach_b,
            "overreach_c": overreach_c,
            "train_days7": int(round(train_days7[i])),
            "any_days7": int(any_days7[i]),
            "baseline_vol": round(baseline_vol, 2) if baseline_vol is not None else None,
            "baseline_rpe": round(baseline_rpe, 3) if baseline_rpe is not None else None,
            "baseline_perf": round(baseline_perf, 3) if baseline_perf is not None else None,
        }

    # no-session deload streak: >= 5 consecutive days, only up to today (never infer in future)
    run = 0
    tmp_deload: list[str] = []
    for i, d in enumerate(all_dates):
        if d <= today_iso and not did_any[i]:
            run += 1
            tmp_deload.append(d)
        else:
            if run >= 5:
                for x in tmp_deload:
                    deload_no_sessions_raw[x] = True
            run = 0
            tmp_deload = []
    if run >= 5:
        for x in tmp_deload:
            deload_no_sessions_raw[x] = True

    for d in all_dates:
        deload_raw[d] = bool(deload_raw.get(d) or deload_no_sessions_raw.get(d))

    sick_raw: dict[str, bool] = {}
    run = 0
    tmp: list[str] = []
    for d in all_dates:
        is_sick = bool((hrv_flags.get(d) or {}).get("sick"))
        if is_sick:
            run += 1
            tmp.append(d)
        else:
            if run >= 2:
                for x in tmp:
                    sick_raw[x] = True
            run = 0
            tmp = []
    if run >= 2:
        for x in tmp:
            sick_raw[x] = True

    conf_train = "high" if (baseline_vol is not None and baseline_rpe is not None) else ("med" if baseline_vol is not None else "low")
    conf_overreach = "high" if any(overreach_track_b_raw.values()) or any(overreach_track_c_raw.values()) else conf_train
    deload_spans = _build_spans(all_dates, deload_raw, kind="deload", min_len=5, label="DELOAD", color_key="cal-deload", confidence=conf_train)
    overreach_spans = _build_spans(
        all_dates, overreach_raw, kind="overreach", min_len=5, label="OVERREACH", color_key="cal-overreach", confidence=conf_overreach
    )
    sick_spans = _build_spans(all_dates, sick_raw, kind="sick_cluster", min_len=2, label="SICK", color_key="cal-sick", confidence="high")

    # clip spans to month grid for output
    def _clip_spans(spans: list[dict[str, Any]]) -> list[dict[str, Any]]:
        clipped: list[dict[str, Any]] = []
        if not spans:
            return clipped
        grid_min = min(grid_dates)
        grid_max = max(grid_dates)
        for sp in spans:
            s = max(sp["start"], grid_min)
            e = min(sp["end"], grid_max)
            if s <= e:
                clipped.append({**sp, "start": s, "end": e})
        return clipped

    spans = _clip_spans(deload_spans) + _clip_spans(overreach_spans) + _clip_spans(sick_spans)

    for sp in spans:
        try:
            s = date.fromisoformat(sp["start"])
            e = date.fromisoformat(sp["end"])
        except Exception:
            sp["trigger_reasons"] = []
            continue
        span_days: list[str] = []
        cur = s
        while cur <= e:
            span_days.append(cur.isoformat())
            cur += timedelta(days=1)

        reasons: list[str] = []
        if sp.get("kind") == "deload":
            if any(deload_no_sessions_raw.get(d) for d in span_days):
                reasons.append("no_sessions_streak")
            if any(deload_low_rpe_raw.get(d) for d in span_days):
                reasons.append("low_rpe")
            if not reasons:
                reasons = ["low_rpe"]
        elif sp.get("kind") == "overreach":
            if any(overreach_track_a_raw.get(d) for d in span_days):
                reasons.append("volume_rpe")
            if any(overreach_track_b_raw.get(d) for d in span_days):
                reasons.append("performance_drop")
            if any(overreach_track_c_raw.get(d) for d in span_days):
                reasons.append("recovery_cost")
            if not reasons:
                reasons = ["volume_rpe"]
        elif sp.get("kind") == "sick_cluster":
            reasons = ["sick_flags"]

        vol_vals = [(_to_float((day_debug.get(d) or {}).get("vol_ratio"))) for d in span_days]
        rpe_vals = [(_to_float((day_debug.get(d) or {}).get("rpe_delta"))) for d in span_days]
        perf_vals = [(_to_float((day_debug.get(d) or {}).get("perf_ratio"))) for d in span_days]
        vol_vals = [v for v in vol_vals if v is not None]
        rpe_vals = [v for v in rpe_vals if v is not None]
        perf_vals = [v for v in perf_vals if v is not None]

        sp["trigger_reasons"] = reasons
        sp["why"] = {
            "days": len(span_days),
            "vol_ratio_min": round(min(vol_vals), 3) if vol_vals else None,
            "vol_ratio_max": round(max(vol_vals), 3) if vol_vals else None,
            "rpe_delta_min": round(min(rpe_vals), 3) if rpe_vals else None,
            "rpe_delta_max": round(max(rpe_vals), 3) if rpe_vals else None,
            "perf_ratio_min": round(min(perf_vals), 3) if perf_vals else None,
            "perf_ratio_max": round(max(perf_vals), 3) if perf_vals else None,
            "avg_rpe": round(mean([v for v in [(_to_float((day_debug.get(d) or {}).get("rpe7"))) for d in span_days] if v is not None]), 3)
            if any((_to_float((day_debug.get(d) or {}).get("rpe7"))) is not None for d in span_days)
            else None,
            "session_count": int(sum(1 for d in span_days if (daily.get(d) or {}).get("did_strength"))),
        }

    deload_days = _span_day_set(_clip_spans(deload_spans))
    overreach_days = _span_day_set(_clip_spans(overreach_spans))

    phase_by_day: dict[str, dict[str, Any]] = {}
    for d in all_dates:
        phase_by_day[d] = {
            "deload": d in deload_days,
            "overreach": d in overreach_days,
            "debug": day_debug.get(d) or {},
        }

    stats = {
        "baseline_vol": baseline_vol,
        "baseline_rpe": baseline_rpe,
        "baseline_sets": baseline_sets,
        "baseline_perf": baseline_perf,
    }
    return spans, phase_by_day, stats


def _dominant_types_v2(
    *,
    day_iso: str,
    d: dict[str, Any],
    planned: dict[str, Any],
    hrv: dict[str, Any],
) -> dict[str, Any]:
    did_strength = bool(d.get("did_strength"))
    did_run = bool(d.get("did_run"))
    has_recovery = bool(hrv.get("has_recovery"))

    dominant = "off"
    secondary: list[str] = []

    if did_strength and did_run:
        s_score = (_to_float(d.get("strength_tonnage")) or 0.0) + (float(d.get("hard_sets") or 0) * 120.0)
        r_score = (_to_float(d.get("run_minutes")) or 0.0) * 80.0
        if s_score >= r_score:
            dominant = "strength"
            secondary.append("run")
        else:
            dominant = "run"
            secondary.append("strength")
    elif did_strength:
        dominant = "strength"
    elif did_run:
        dominant = "run"
    elif bool(planned.get("has_plan")) and planned.get("kind") == "train":
        dominant = "none"
    elif has_recovery:
        dominant = "recovery"
    else:
        dominant = "off"

    tints = {
        "strength": dominant == "strength" or "strength" in secondary,
        "run": dominant == "run" or "run" in secondary,
        "recovery": dominant == "recovery" or "recovery" in secondary,
        "off": dominant == "off",
        "sick": bool(hrv.get("sick")),
    }

    return {
        "dominant": dominant,
        "secondary": secondary[:2],
        "tints": tints,
    }


def build_calendar_month_v2_payload(
    training_conn: sqlite3.Connection,
    runs_conn: sqlite3.Connection,
    hrv_conn: sqlite3.Connection,
    plan: dict[str, Any] | None,
    *,
    month_raw: str | None,
    mode: str = "both",
    today: date | None = None,
    debug: bool = False,
) -> dict[str, Any]:
    today_d = today or berlin_today()
    month_start = _month_start_from_param(month_raw, today_d)
    month_iso = month_start.strftime("%Y-%m")
    grid_start, grid_end = _month_grid_bounds(month_start)

    context_start = grid_start - timedelta(days=42)
    context_end = grid_end

    context_days = _iter_days(context_start, context_end)
    grid_days = _iter_days(grid_start, grid_end)
    grid_day_set = set(grid_days)

    daily = _load_activity_daily_v2(training_conn, runs_conn, context_start.isoformat(), context_end.isoformat())
    planned_map = _build_plan_day_map(plan, context_days)
    hrv_map = _load_hrv_flags_and_presence_by_day(hrv_conn, context_start.isoformat(), context_end.isoformat())

    spans, phase_by_day, phase_stats = _phase_spans_v2(context_days, grid_day_set, daily, hrv_map, today=today_d)

    cells: list[dict[str, Any]] = []
    for day_iso in grid_days:
        day_obj = date.fromisoformat(day_iso)
        d = daily.get(day_iso, {})
        p = planned_map.get(day_iso, {"has_plan": False, "plan_title": None, "kind": None})
        h = hrv_map.get(day_iso, {"sick": False, "alcohol": False, "has_recovery": False})
        ph = phase_by_day.get(day_iso, {"deload": False, "overreach": False})

        types = _dominant_types_v2(day_iso=day_iso, d=d, planned=p, hrv=h)

        actual_did = bool(d.get("did_any"))
        planned_has = bool(p.get("has_plan"))

        if mode == "actual":
            planned_has = False
        elif mode == "plan":
            actual_did = False

        cell = {
            "date": day_iso,
            "in_month": (day_obj.month == month_start.month and day_obj.year == month_start.year),
            "day": day_obj.day,
            "dow": day_obj.weekday(),
            "types": types,
            "planned": {
                "has": planned_has,
                "title": p.get("plan_title"),
                "kind": p.get("kind"),
            },
            "actual": {
                "did": actual_did,
                "title": d.get("title"),
                "tonnage_t": _fmt_tonnage_t(_to_float(d.get("strength_tonnage"))),
                "hard_sets": int(d.get("hard_sets") or 0) if d.get("did_strength") else None,
                "avg_rpe": round(_to_float(d.get("avg_rpe")), 2) if _to_float(d.get("avg_rpe")) is not None else None,
                "toplift": d.get("toplift"),
                "run_minutes": round(_to_float(d.get("run_minutes")) or 0.0, 1) if d.get("did_run") else None,
            },
            "flags": {
                "sick": bool(h.get("sick")),
                "alcohol": bool(h.get("alcohol")),
            },
            "phase": {
                "deload": bool(ph.get("deload")),
                "overreach": bool(ph.get("overreach")),
            },
            "state": {
                "rest": bool((p.get("kind") == "off") and not actual_did),
            },
        }
        if debug:
            cell["debug"] = ph.get("debug") or {}
        cells.append(cell)

    weeks: list[list[dict[str, Any]]] = []
    for i in range(0, len(cells), 7):
        weeks.append(cells[i : i + 7])

    payload = {
        "meta": {
            "month": month_iso,
            "tz": "Europe/Berlin",
            "today": today_d.isoformat(),
            "mode": mode,
        },
        "weeks": weeks,
        "spans": spans,
    }

    if debug:
        payload["debug"] = {
            "thresholds": {
                "deload_legacy": {"vol_ratio": 0.65, "rpe_delta": -0.5, "fallback_vol_ratio": 0.55, "min_span_days": 5},
                "overreach": {
                    "track_a": {"vol_ratio": 1.20, "rpe_delta": 0.3, "fallback_vol_ratio": 1.35},
                    "track_b": {"perf_ratio_drop": 0.97, "perf_stag_max": 1.00, "min_load_ctx": 1.10},
                    "track_c": {"rmssd_delta": -0.08, "rhr_delta": 0.04, "min_load_ctx": 1.10},
                    "min_span_days": 5,
                },
                "deload": {"no_sessions_min_days": 5, "low_rpe_max": 7.0, "low_rpe_min_sessions": 3},
                "sick": {"min_span_days": 2},
            }
        }
        payload["debug"]["baseline"] = {
            "vol7": round(phase_stats.get("baseline_vol"), 3) if phase_stats.get("baseline_vol") is not None else None,
            "rpe": round(phase_stats.get("baseline_rpe"), 3) if phase_stats.get("baseline_rpe") is not None else None,
            "sets7": round(phase_stats.get("baseline_sets"), 3) if phase_stats.get("baseline_sets") is not None else None,
            "perf14": round(phase_stats.get("baseline_perf"), 3) if phase_stats.get("baseline_perf") is not None else None,
        }

    return payload


def _parse_range_bounds(
    *,
    start_raw: str | None,
    end_raw: str | None,
    today: date,
    default_days: int = 120,
    max_days: int = 400,
) -> tuple[date, date]:
    start_d = date.fromisoformat(start_raw) if (start_raw and _coerce_iso_day(start_raw)) else None
    end_d = date.fromisoformat(end_raw) if (end_raw and _coerce_iso_day(end_raw)) else None
    if start_d and end_d and start_d <= end_d:
        pass
    elif start_d and not end_d:
        end_d = min(today, start_d + timedelta(days=max(1, default_days) - 1))
    elif end_d and not start_d:
        start_d = end_d - timedelta(days=max(1, default_days) - 1)
    else:
        end_d = today
        start_d = end_d - timedelta(days=max(1, default_days) - 1)

    if not start_d or not end_d:
        end_d = today
        start_d = end_d - timedelta(days=max(1, default_days) - 1)

    if start_d > end_d:
        start_d, end_d = end_d, start_d
    span = (end_d - start_d).days + 1
    if span > max_days:
        start_d = end_d - timedelta(days=max_days - 1)
    return start_d, end_d


def build_calendar_range_v2_payload(
    training_conn: sqlite3.Connection,
    runs_conn: sqlite3.Connection,
    hrv_conn: sqlite3.Connection,
    plan: dict[str, Any] | None,
    *,
    start_raw: str | None,
    end_raw: str | None,
    mode: str = "both",
    today: date | None = None,
    debug: bool = False,
) -> dict[str, Any]:
    today_d = today or berlin_today()
    start_d, end_d = _parse_range_bounds(start_raw=start_raw, end_raw=end_raw, today=today_d)
    mode_safe = mode if mode in {"both", "plan", "actual"} else "both"
    cache_key = f"{start_d.isoformat()}|{end_d.isoformat()}|{mode_safe}|{today_d.isoformat()}|{int(debug)}"
    now_m = monotonic()
    with _CACHE_LOCK:
        cached = _RANGE_V2_CACHE.get(cache_key)
        if cached and (now_m - cached[0]) <= CACHE_TTL_SECONDS:
            return cached[1]

    visible_days = _iter_days(start_d, end_d)
    visible_set = set(visible_days)
    context_start = start_d - timedelta(days=42)
    context_days = _iter_days(context_start, end_d)

    daily = _load_activity_daily_v2(training_conn, runs_conn, context_start.isoformat(), end_d.isoformat())
    planned_map = _build_plan_day_map(plan, context_days)
    hrv_map = _load_hrv_flags_and_presence_by_day(hrv_conn, context_start.isoformat(), end_d.isoformat())
    spans, phase_by_day, _phase_stats = _phase_spans_v2(context_days, visible_set, daily, hrv_map, today=today_d)

    days: list[dict[str, Any]] = []
    for d_iso in visible_days:
        d_obj = date.fromisoformat(d_iso)
        d = daily.get(d_iso, {})
        p = planned_map.get(d_iso, {"has_plan": False, "plan_title": None, "kind": None})
        h = hrv_map.get(d_iso, {"sick": False, "alcohol": False, "has_recovery": False})
        ph = phase_by_day.get(d_iso, {"deload": False, "overreach": False, "debug": {}})
        types = _dominant_types_v2(day_iso=d_iso, d=d, planned=p, hrv=h)

        actual_did = bool(d.get("did_any"))
        planned_has = bool(p.get("has_plan"))
        if mode_safe == "actual":
            planned_has = False
        elif mode_safe == "plan":
            actual_did = False

        dbg = ph.get("debug") if isinstance(ph.get("debug"), dict) else {}
        vol_ratio = _to_float(dbg.get("vol_ratio")) or 0.0
        rmssd_delta = _to_float(dbg.get("rmssd_delta")) or 0.0
        rhr_delta = _to_float(dbg.get("rhr_delta")) or 0.0
        perf_ratio = _to_float(dbg.get("perf_ratio")) or 1.0

        load_intensity = 3 if vol_ratio >= 1.2 else (2 if vol_ratio >= 1.0 else (1 if vol_ratio >= 0.7 else 0))
        recovery_cost = (-rmssd_delta if rmssd_delta < 0 else 0.0) + (rhr_delta if rhr_delta > 0 else 0.0)
        recovery_intensity = 3 if recovery_cost >= 0.20 else (2 if recovery_cost >= 0.10 else (1 if recovery_cost >= 0.04 else 0))
        perf_drop = max(0.0, 1.0 - perf_ratio)
        performance_intensity = 3 if perf_drop >= 0.06 else (2 if perf_drop >= 0.03 else (1 if perf_drop >= 0.01 else 0))

        cell = {
            "date": d_iso,
            "dow": d_obj.weekday(),
            "day": d_obj.day,
            "in_month": True,
            "types": types,
            "planned": {
                "has": planned_has,
                "title": p.get("plan_title"),
                "kind": p.get("kind"),
            },
            "actual": {
                "did": actual_did,
                "title": d.get("title"),
                "summary": _fmt_summary(int(d.get("hard_sets") or 0), _to_float(d.get("strength_tonnage")), _to_float(d.get("run_minutes"))),
                "tonnage_t": _fmt_tonnage_t(_to_float(d.get("strength_tonnage"))),
                "hard_sets": int(d.get("hard_sets") or 0) if d.get("did_strength") else None,
                "avg_rpe": round(_to_float(d.get("avg_rpe")), 2) if _to_float(d.get("avg_rpe")) is not None else None,
                "toplift": d.get("toplift"),
                "run_minutes": round(_to_float(d.get("run_minutes")) or 0.0, 1) if d.get("did_run") else None,
                "run_distance_km": round(_to_float(d.get("run_distance_km")) or 0.0, 2) if d.get("did_run") else None,
            },
            "flags": {
                "sick": bool(h.get("sick")),
                "alcohol": bool(h.get("alcohol")),
            },
            "phase": {
                "deload": bool(ph.get("deload")),
                "overreach": bool(ph.get("overreach")),
            },
            "state": {
                "rest": bool((p.get("kind") == "off") and not actual_did),
            },
            "focus": {
                "load": load_intensity,
                "recovery": recovery_intensity,
                "performance": performance_intensity,
            },
        }
        if debug:
            cell["debug"] = dbg
        days.append(cell)

    payload = {
        "meta": {
            "start": start_d.isoformat(),
            "end": end_d.isoformat(),
            "tz": "Europe/Berlin",
            "today": today_d.isoformat(),
            "mode": mode_safe,
        },
        "days": days,
        "spans": spans,
    }
    with _CACHE_LOCK:
        _RANGE_V2_CACHE[cache_key] = (now_m, payload)
    return payload


def _norm_text(raw: Any) -> str:
    return str(raw or "").strip().lower()


def _planned_kind_from_cell(cell: dict[str, Any]) -> tuple[str | None, str | None]:
    planned = cell.get("planned") if isinstance(cell, dict) else {}
    title = str((planned or {}).get("title") or "").strip() or None
    kind_raw = _norm_text((planned or {}).get("kind"))
    if not kind_raw:
        return None, title
    if kind_raw == "off":
        return "rest", title or "Rest"
    if kind_raw != "train":
        return kind_raw, title
    t = _norm_text(title)
    if any(x in t for x in ("run", "lauf", "cardio")):
        return "run", title
    return "strength", title


def _e1rm(weight: float | None, reps: int | None) -> float | None:
    if weight is None or reps is None:
        return None
    if reps <= 0 or weight <= 0:
        return None
    return float(weight) * (1.0 + (float(reps) / 30.0))


def _is_intentionally_easier(cur_rpe: float | None, prev_rpe: float | None) -> bool:
    if cur_rpe is None or prev_rpe is None:
        return False
    return cur_rpe <= (prev_rpe - 1.0)


def _classify_set_delta(
    *,
    cur_w: float | None,
    cur_r: int | None,
    cur_rpe: float | None,
    prev_w: float | None,
    prev_r: int | None,
    prev_rpe: float | None,
    is_deload_day: bool,
) -> str:
    if prev_w is None or prev_r is None or cur_w is None or cur_r is None:
        return "neutral"
    result = compare_set_progress(
        SetPerformance(weight=cur_w, reps=cur_r, rpe=cur_rpe),
        SetPerformance(weight=prev_w, reps=prev_r, rpe=prev_rpe),
    )
    if result.status == "progress":
        return "improved"
    if result.status == "regress":
        if is_deload_day or _is_intentionally_easier(cur_rpe, prev_rpe):
            return "neutral"
        return "worse"
    return "neutral"


def _load_strength_day_counters(
    training_conn: sqlite3.Connection,
    *,
    compare_start_iso: str,
    end_iso: str,
    visible_days: set[str],
    deload_days: set[str],
) -> dict[str, dict[str, Any]]:
    # Calendar is a display consumer of the canonical session payload. It must
    # never reclassify sets or apply deload-specific count changes.
    from analysis.training_progress import compute_progress_payloads

    del deload_days
    canonical = compute_progress_payloads(
        training_conn,
        start_iso=compare_start_iso,
        end_iso=end_iso,
    )
    by_day: dict[str, dict[str, Any]] = {}
    for progress in canonical:
        day_iso = str(progress.get("session_date") or "")
        if day_iso not in visible_days:
            continue
        entry = by_day.setdefault(
            day_iso,
            {
                "has": True,
                "session_id": progress.get("session_id"),
                "title": progress.get("session_name") or "Workout",
                "total_sets": 0,
                "improved_sets": 0,
                "worse_sets": 0,
                "neutral_sets": 0,
                "new_sets": 0,
                "sessions": [],
                "rule_version": progress.get("rule_version"),
            },
        )
        entry["total_sets"] += int(progress.get("total_eligible_sets") or 0)
        entry["improved_sets"] += int(progress.get("improved") or 0)
        entry["worse_sets"] += int(progress.get("worse") or 0)
        entry["neutral_sets"] += int(progress.get("same") or 0) + int(progress.get("unmatched_known_exercise") or 0) + int(progress.get("unknown") or 0)
        entry["new_sets"] += int(progress.get("new_exercise") or 0)
        entry["session_id"] = progress.get("session_id")
        entry["title"] = progress.get("session_name") or entry["title"]
        entry["sessions"].append(progress)
    for entry in by_day.values():
        entry["perf_score"] = max(-9, min(9, int(entry["improved_sets"]) - int(entry["worse_sets"])))
    return by_day


def _load_run_day_stats_timeline(
    runs_conn: sqlite3.Connection,
    training_conn: sqlite3.Connection,
    *,
    start_iso: str,
    end_iso: str,
) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}

    def _add(day_iso: str, dist_km: float | None, time_s: float | None, pace_s_per_km: float | None) -> None:
        if not day_iso:
            return
        entry = out.setdefault(day_iso, {"distance_km": 0.0, "time_s": 0.0, "pace_items": []})
        if dist_km is not None and dist_km > 0:
            entry["distance_km"] += dist_km
        if time_s is not None and time_s > 0:
            entry["time_s"] += time_s
        if pace_s_per_km is not None and pace_s_per_km > 0:
            entry["pace_items"].append(pace_s_per_km)

    if _table_exists(runs_conn, "runs"):
        cols = _table_columns(runs_conn, "runs")
        date_col = _first_existing(cols, ["date", "date_iso", "day"])
        dist_col = _first_existing(cols, ["distance", "distance_m", "distance_km", "km"])
        time_col = _first_existing(cols, ["moving_time", "moving_time_s", "duration_s", "time_s"])
        pace_col = _first_existing(cols, ["pace", "pace_s_per_km"])
        if date_col:
            rows = runs_conn.execute(
                f"""
                SELECT SUBSTR({date_col},1,10) AS d,
                       {dist_col if dist_col else 'NULL'} AS dist,
                       {time_col if time_col else 'NULL'} AS moving,
                       {pace_col if pace_col else 'NULL'} AS pace
                FROM runs
                WHERE {date_col} IS NOT NULL
                  AND SUBSTR({date_col},1,10) BETWEEN ? AND ?
                """,
                (start_iso, end_iso),
            ).fetchall()
            for row in rows:
                d = _coerce_iso_day(row["d"])
                if not d:
                    continue
                dist_v = _to_float(row["dist"])
                moving_v = _to_float(row["moving"])
                pace_v = _to_float(row["pace"])
                dist_km = None if dist_v is None else (dist_v / 1000.0 if dist_col in {"distance", "distance_m"} else dist_v)
                time_s = None if moving_v is None else (moving_v * 60.0 if time_col in {"minutes"} else moving_v)
                _add(d, dist_km, time_s, pace_v)

    if _table_exists(training_conn, "runs"):
        cols = _table_columns(training_conn, "runs")
        date_col = _first_existing(cols, ["date_iso", "date", "day"])
        dist_col = _first_existing(cols, ["distance_km", "km"])
        min_col = _first_existing(cols, ["minutes", "moving_time", "moving_time_s"])
        if date_col:
            rows = training_conn.execute(
                f"""
                SELECT SUBSTR({date_col},1,10) AS d,
                       {dist_col if dist_col else 'NULL'} AS dist_km,
                       {min_col if min_col else 'NULL'} AS mins
                FROM runs
                WHERE {date_col} IS NOT NULL
                  AND SUBSTR({date_col},1,10) BETWEEN ? AND ?
                """,
                (start_iso, end_iso),
            ).fetchall()
            for row in rows:
                d = _coerce_iso_day(row["d"])
                if not d:
                    continue
                km_v = _to_float(row["dist_km"])
                mins_v = _to_float(row["mins"])
                time_s = None
                if mins_v is not None:
                    time_s = mins_v * 60.0 if min_col == "minutes" else mins_v
                _add(d, km_v, time_s, None)

    final: dict[str, dict[str, Any]] = {}
    for d, v in out.items():
        dist = _to_float(v.get("distance_km")) or 0.0
        time_s = _to_float(v.get("time_s")) or 0.0
        pace_items = [x for x in (v.get("pace_items") or []) if _to_float(x) is not None]
        pace = None
        if pace_items:
            pace = sum(pace_items) / float(len(pace_items))
        elif dist > 0 and time_s > 0:
            pace = time_s / dist
        final[d] = {
            "distance_km": round(dist, 3) if dist > 0 else None,
            "time_s": round(time_s, 1) if time_s > 0 else None,
            "pace_s_per_km": round(pace, 1) if pace is not None else None,
        }
    return final


def build_calendar_timeline_v1_payload(
    training_conn: sqlite3.Connection,
    runs_conn: sqlite3.Connection,
    hrv_conn: sqlite3.Connection,
    plan: dict[str, Any] | None,
    *,
    start_raw: str | None,
    end_raw: str | None,
    mode: str = "both",
    today: date | None = None,
    debug: bool = False,
) -> dict[str, Any]:
    today_d = today or berlin_today()
    start_d, end_d = _parse_range_bounds(start_raw=start_raw, end_raw=end_raw, today=today_d, default_days=84, max_days=420)
    mode_safe = mode if mode in {"both", "plan", "actual"} else "both"
    cache_key = f"{start_d.isoformat()}|{end_d.isoformat()}|{mode_safe}|{today_d.isoformat()}|{int(debug)}"
    now_m = monotonic()
    with _CACHE_LOCK:
        cached = _TIMELINE_V1_CACHE.get(cache_key)
        if cached and (now_m - cached[0]) <= CACHE_TTL_SECONDS:
            return cached[1]

    visible_days = _iter_days(start_d, end_d)
    visible_set = set(visible_days)
    context_start = start_d - timedelta(days=42)
    compare_start = start_d - timedelta(days=180)
    context_days = _iter_days(context_start, end_d)

    daily = _load_activity_daily_v2(training_conn, runs_conn, context_start.isoformat(), end_d.isoformat())
    planned_map = _build_plan_day_map(plan, context_days)
    hrv_map = _load_hrv_flags_and_presence_by_day(hrv_conn, context_start.isoformat(), end_d.isoformat())
    spans, phase_by_day, _phase_stats = _phase_spans_v2(context_days, visible_set, daily, hrv_map, today=today_d)

    deload_days = _span_day_set([sp for sp in spans if sp.get("kind") == "deload"])
    overreach_days = _span_day_set([sp for sp in spans if sp.get("kind") == "overreach"])
    sick_span_days = _span_day_set([sp for sp in spans if sp.get("kind") == "sick_cluster"])

    strength_by_day = _load_strength_day_counters(
        training_conn,
        compare_start_iso=compare_start.isoformat(),
        end_iso=end_d.isoformat(),
        visible_days=visible_set,
        deload_days=deload_days,
    )
    run_by_day = _load_run_day_stats_timeline(
        runs_conn,
        training_conn,
        start_iso=start_d.isoformat(),
        end_iso=end_d.isoformat(),
    )

    # Run improved heuristic: faster pace (>=2%) or longer distance (>=10%) vs previous run day.
    prev_run: dict[str, Any] | None = None
    run_improved_by_day: dict[str, bool | None] = {}
    for d_iso in visible_days:
        cur = run_by_day.get(d_iso)
        if not cur:
            continue
        improved: bool | None = None
        if prev_run:
            prev_pace = _to_float(prev_run.get("pace_s_per_km"))
            cur_pace = _to_float(cur.get("pace_s_per_km"))
            prev_dist = _to_float(prev_run.get("distance_km"))
            cur_dist = _to_float(cur.get("distance_km"))
            if prev_pace is not None and cur_pace is not None and prev_pace > 0:
                improved = cur_pace <= (prev_pace * 0.98)
            elif prev_dist is not None and cur_dist is not None and prev_dist > 0:
                improved = cur_dist >= (prev_dist * 1.10)
        run_improved_by_day[d_iso] = improved
        prev_run = cur

    days: list[dict[str, Any]] = []
    for d_iso in visible_days:
        day_obj = date.fromisoformat(d_iso)
        d = daily.get(d_iso, {})
        p = planned_map.get(d_iso, {"has_plan": False, "plan_title": None, "kind": None})
        h = hrv_map.get(d_iso, {"sick": False, "alcohol": False, "has_recovery": False})
        ph = phase_by_day.get(d_iso, {"deload": False, "overreach": False, "debug": {}})
        strength = strength_by_day.get(d_iso) or {}
        run = run_by_day.get(d_iso) or {}
        planned_kind, planned_title = _planned_kind_from_cell({
            "planned": {
                "kind": p.get("kind"),
                "title": p.get("plan_title"),
            }
        })

        actual_strength_has = bool(strength.get("has")) or bool(d.get("did_strength"))
        actual_run_has = bool(run) or bool(d.get("did_run"))
        if mode_safe == "actual":
            planned_kind = None
            planned_title = None
        elif mode_safe == "plan":
            actual_strength_has = False
            actual_run_has = False

        strength_payload = {
            "has": bool(actual_strength_has),
            "session_id": strength.get("session_id"),
            "title": strength.get("title") or d.get("title"),
            "total_sets": int(strength.get("total_sets") or 0) if actual_strength_has else 0,
            "improved_sets": int(strength.get("improved_sets") or 0) if actual_strength_has else 0,
            "worse_sets": int(strength.get("worse_sets") or 0) if actual_strength_has else 0,
            "neutral_sets": int(strength.get("neutral_sets") or 0) if actual_strength_has else 0,
            "perf_score": int(strength.get("perf_score") or 0) if actual_strength_has else None,
        }
        run_payload = {
            "has": bool(actual_run_has),
            "distance_km": run.get("distance_km") if actual_run_has else None,
            "time_s": run.get("time_s") if actual_run_has else None,
            "pace_s_per_km": run.get("pace_s_per_km") if actual_run_has else None,
            "improved": run_improved_by_day.get(d_iso) if actual_run_has else None,
        }
        dbg = ph.get("debug") if isinstance(ph.get("debug"), dict) else {}
        vol_ratio = _to_float(dbg.get("vol_ratio")) or 0.0
        rmssd_delta = _to_float(dbg.get("rmssd_delta")) or 0.0
        rhr_delta = _to_float(dbg.get("rhr_delta")) or 0.0
        perf_ratio = _to_float(dbg.get("perf_ratio")) or 1.0
        load_intensity = 3 if vol_ratio >= 1.2 else (2 if vol_ratio >= 1.0 else (1 if vol_ratio >= 0.7 else 0))
        recovery_cost = (-rmssd_delta if rmssd_delta < 0 else 0.0) + (rhr_delta if rhr_delta > 0 else 0.0)
        recovery_intensity = 3 if recovery_cost >= 0.20 else (2 if recovery_cost >= 0.10 else (1 if recovery_cost >= 0.04 else 0))
        perf_drop = max(0.0, 1.0 - perf_ratio)
        performance_intensity = 3 if perf_drop >= 0.06 else (2 if perf_drop >= 0.03 else (1 if perf_drop >= 0.01 else 0))
        day_payload = {
            "date": d_iso,
            "dow": day_obj.weekday(),
            "day": day_obj.day,
            "planned": {
                "kind": planned_kind,
                "title": planned_title,
                "has": planned_kind is not None,
                "has_plan": planned_kind is not None,
                "plan_title": planned_title,
            },
            "actual": {
                "strength": strength_payload,
                "run": run_payload,
                "did": bool(actual_strength_has or actual_run_has),
                "did_train": bool(actual_strength_has or actual_run_has),
                "title": strength_payload.get("title") or ("Run" if actual_run_has else None),
                "session_title": strength_payload.get("title") or ("Run" if actual_run_has else None),
                "summary": _fmt_summary(
                    strength_payload.get("total_sets"),
                    (_to_float(d.get("strength_tonnage")) or 0.0) if actual_strength_has else None,
                    ((_to_float(run_payload.get("time_s")) or 0.0) / 60.0) if actual_run_has else None,
                ),
            },
            "flags": {
                "sick": bool(h.get("sick")),
                "alcohol": bool(h.get("alcohol")),
            },
            "phase": {
                "deload": bool(ph.get("deload")),
                "overreach": bool(ph.get("overreach")),
            },
            "spans": {
                "in_deload": d_iso in deload_days,
                "in_overreach": d_iso in overreach_days,
                "in_sick": d_iso in sick_span_days,
            },
            "state": {
                "rest": bool((planned_kind == "rest") and not actual_strength_has and not actual_run_has),
            },
            "types": _dominant_types_v2(day_iso=d_iso, d=d, planned=p, hrv=h),
            "focus": {
                "load": load_intensity,
                "recovery": recovery_intensity,
                "performance": performance_intensity,
            },
        }
        if debug:
            day_payload["debug"] = ph.get("debug") or {}
        days.append(day_payload)

    payload = {
        "meta": {
            "start": start_d.isoformat(),
            "end": end_d.isoformat(),
            "today": today_d.isoformat(),
            "tz": "Europe/Berlin",
            "mode": mode_safe,
        },
        "days": days,
        "spans": spans,
    }
    with _CACHE_LOCK:
        _TIMELINE_V1_CACHE[cache_key] = (now_m, payload)
    return payload


def _rolling_slope(values: list[float | None], idx: int, window: int = 7) -> float | None:
    s = max(0, idx - window + 1)
    chunk = values[s : idx + 1]
    pts = [(i, v) for i, v in enumerate(chunk) if v is not None]
    if len(pts) < 4:
        return None
    n = len(pts)
    x_mean = sum(float(p[0]) for p in pts) / float(n)
    y_mean = sum(float(p[1]) for p in pts) / float(n)
    den = sum((float(p[0]) - x_mean) ** 2 for p in pts)
    if den <= 0:
        return None
    num = sum((float(p[0]) - x_mean) * (float(p[1]) - y_mean) for p in pts)
    return num / den


def _trend_from_slope(slope: float | None, tol: float = 0.12) -> str | None:
    if slope is None:
        return None
    if slope > tol:
        return "up"
    if slope < -tol:
        return "down"
    return "flat"


def build_calendar_month_live_v1_payload(
    training_conn: sqlite3.Connection,
    runs_conn: sqlite3.Connection,
    hrv_conn: sqlite3.Connection,
    plan: dict[str, Any] | None,
    *,
    month_raw: str | None,
    mode: str = "both",
    today: date | None = None,
    debug: bool = False,
) -> dict[str, Any]:
    today_d = today or berlin_today()
    month_start = _month_start_from_param(month_raw, today_d)
    month_iso = month_start.strftime("%Y-%m")
    grid_start, grid_end = _month_grid_bounds(month_start)
    mode_safe = mode if mode in {"both", "plan", "actual"} else "both"
    cache_key = f"{month_iso}|{mode_safe}|{today_d.isoformat()}|{int(debug)}"
    now_m = monotonic()
    with _CACHE_LOCK:
        cached = _MONTH_LIVE_V1_CACHE.get(cache_key)
        if cached and (now_m - cached[0]) <= CACHE_TTL_SECONDS:
            return cached[1]

    visible_days = _iter_days(grid_start, grid_end)
    visible_set = set(visible_days)
    context_start = grid_start - timedelta(days=42)
    compare_start = grid_start - timedelta(days=180)
    context_days = _iter_days(context_start, grid_end)

    daily = _load_activity_daily_v2(training_conn, runs_conn, context_start.isoformat(), grid_end.isoformat())
    planned_map = _build_plan_day_map(plan, context_days)
    hrv_map = _load_hrv_flags_and_presence_by_day(hrv_conn, context_start.isoformat(), grid_end.isoformat())
    spans_raw, phase_by_day, _phase_stats = _phase_spans_v2(context_days, visible_set, daily, hrv_map, today=today_d)

    # map spans to month_live semantics
    bars: list[dict[str, Any]] = []
    for sp in spans_raw:
        kind = str(sp.get("kind") or "")
        if kind == "overreach":
            kind = "functional_overreach"
        if kind not in {"functional_overreach", "deload", "sick_cluster"}:
            continue
        start = str(sp.get("start") or "")
        end = str(sp.get("end") or "")
        if not start or not end:
            continue
        # Future clipping guard
        if start > today_d.isoformat():
            continue
        if end > today_d.isoformat():
            end = today_d.isoformat()
        if start > end:
            continue
        bars.append({
            "kind": kind,
            "start": start,
            "end": end,
            "label": "FO" if kind == "functional_overreach" else ("DELOAD" if kind == "deload" else "SICK"),
            "confidence": sp.get("confidence") or "med",
            "reasons": list(sp.get("trigger_reasons") or []),
            "why": sp.get("why") or {},
        })

    deload_days = _span_day_set([b for b in bars if b.get("kind") == "deload"])
    fo_days = _span_day_set([b for b in bars if b.get("kind") == "functional_overreach"])
    sick_days = _span_day_set([b for b in bars if b.get("kind") == "sick_cluster"])

    strength_by_day = _load_strength_day_counters(
        training_conn,
        compare_start_iso=compare_start.isoformat(),
        end_iso=grid_end.isoformat(),
        visible_days=visible_set,
        deload_days=deload_days,
    )
    run_by_day = _load_run_day_stats_timeline(
        runs_conn,
        training_conn,
        start_iso=grid_start.isoformat(),
        end_iso=grid_end.isoformat(),
    )

    # readiness baseline from full context
    ctx_rmssd = [_to_float((hrv_map.get(d) or {}).get("rmssd")) for d in context_days]
    ctx_rhr = [_to_float((hrv_map.get(d) or {}).get("rhr")) for d in context_days]
    base_rmssd_vals = [v for v in ctx_rmssd[:42] if v is not None and v > 0]
    base_rhr_vals = [v for v in ctx_rhr[:42] if v is not None and v > 0]
    base_rmssd = (sum(base_rmssd_vals) / float(len(base_rmssd_vals))) if base_rmssd_vals else None
    base_rhr = (sum(base_rhr_vals) / float(len(base_rhr_vals))) if base_rhr_vals else None

    readiness_series: list[float | None] = []
    perf_series: list[float | None] = []
    all_days = visible_days
    for d_iso in all_days:
        h = hrv_map.get(d_iso, {})
        rmssd = _to_float(h.get("rmssd"))
        rhr = _to_float(h.get("rhr"))
        readiness: float | None = None
        if base_rmssd and base_rhr and rmssd and rhr:
            rmssd_rel = (rmssd / base_rmssd) - 1.0
            rhr_rel = (base_rhr / rhr) - 1.0
            val = 50.0 + (rmssd_rel * 28.0) + (rhr_rel * 22.0)
            readiness = max(0.0, min(100.0, val))
        readiness_series.append(readiness)
        s = strength_by_day.get(d_iso) or {}
        if s.get("has"):
            perf_series.append(float(max(-9, min(9, int(s.get("perf_score") or 0)))))
        else:
            perf_series.append(None)

    days: list[dict[str, Any]] = []
    for i, d_iso in enumerate(all_days):
        day_obj = date.fromisoformat(d_iso)
        d = daily.get(d_iso, {})
        p = planned_map.get(d_iso, {"has_plan": False, "plan_title": None, "kind": None})
        h = hrv_map.get(d_iso, {"sick": False, "alcohol": False})
        ph = phase_by_day.get(d_iso, {"deload": False, "overreach": False})
        planned_kind, planned_title = _planned_kind_from_cell({"planned": {"kind": p.get("kind"), "title": p.get("plan_title")}})
        s = strength_by_day.get(d_iso) or {}
        r = run_by_day.get(d_iso) or {}

        strength_has = bool(s.get("has")) or bool(d.get("did_strength"))
        run_has = bool(r) or bool(d.get("did_run"))
        actual_did = bool(strength_has or run_has)
        if mode_safe == "actual":
            planned_kind = None
            planned_title = None
        elif mode_safe == "plan":
            strength_has = False
            run_has = False
            actual_did = False

        rd_score = readiness_series[i]
        rd_trend = _trend_from_slope(_rolling_slope(readiness_series, i, 7), tol=0.15)
        pf_score = perf_series[i]
        pf_trend = _trend_from_slope(_rolling_slope(perf_series, i, 7), tol=0.05)
        if pf_score is None:
            pf_trend = None

        is_future = d_iso > today_d.isoformat()
        cell = {
            "date": d_iso,
            "dow": day_obj.weekday() + 1,
            "day": day_obj.day,
            "in_month": (day_obj.month == month_start.month and day_obj.year == month_start.year),
            "planned": {
                "kind": planned_kind,
                "title": planned_title,
                "is_rest": bool(planned_kind == "rest"),
            },
            "actual": {
                "strength": {
                    "has": bool(strength_has),
                    "session_id": s.get("session_id"),
                    "title": s.get("title") or d.get("title"),
                    "total_sets": int(s.get("total_sets") or 0) if strength_has else 0,
                    "improved_sets": int(s.get("improved_sets") or 0) if strength_has else 0,
                    "worse_sets": int(s.get("worse_sets") or 0) if strength_has else 0,
                    "neutral_sets": int(s.get("neutral_sets") or 0) if strength_has else 0,
                    "new_sets": int(s.get("new_sets") or 0) if strength_has else 0,
                },
                "run": {
                    "has": bool(run_has),
                    "distance_km": r.get("distance_km") if run_has else None,
                    "time_s": r.get("time_s") if run_has else None,
                    "pace_s_per_km": r.get("pace_s_per_km") if run_has else None,
                },
            },
            "flags": {
                "sick": bool(h.get("sick")),
                "alcohol": bool(h.get("alcohol")),
            },
            "readiness": {
                "score": round(rd_score, 1) if rd_score is not None else None,
                "trend": rd_trend,
            },
            "perf": {
                "score": int(max(-9, min(9, int(pf_score)))) if pf_score is not None else None,
                "trend": pf_trend,
            },
            "tags": {
                "rest_day": bool((planned_kind == "rest") and not actual_did),
                "planned_only": bool((planned_kind is not None) and (not actual_did) and (d_iso <= today_d.isoformat())),
            },
            "phase": {
                "deload": (d_iso in deload_days) and (not is_future),
                "functional_overreach": (d_iso in fo_days) and (not is_future),
            },
            "spans": {
                "in_deload": (d_iso in deload_days) and (not is_future),
                "in_functional_overreach": (d_iso in fo_days) and (not is_future),
                "in_sick": (d_iso in sick_days) and (not is_future),
            },
            "state": {
                "rest": bool((planned_kind == "rest") and not actual_did),
            },
            "types": _dominant_types_v2(day_iso=d_iso, d=d, planned=p, hrv=h),
        }
        if debug:
            cell["debug"] = (ph.get("debug") or {})
        days.append(cell)

    payload = {
        "meta": {
            "month": month_iso,
            "today": today_d.isoformat(),
            "tz": "Europe/Berlin",
            "mode": mode_safe,
        },
        "days": days,
        "bars": bars,
    }
    with _CACHE_LOCK:
        _MONTH_LIVE_V1_CACHE[cache_key] = (now_m, payload)
    return payload


def build_calendar_day_v2_payload(
    training_conn: sqlite3.Connection,
    runs_conn: sqlite3.Connection,
    hrv_conn: sqlite3.Connection,
    plan: dict[str, Any] | None,
    *,
    day_raw: str,
    today: date | None = None,
    debug: bool = False,
) -> dict[str, Any]:
    today_d = today or berlin_today()
    target_iso = _coerce_iso_day(day_raw) or today_d.isoformat()

    month_payload = build_calendar_month_v2_payload(
        training_conn,
        runs_conn,
        hrv_conn,
        plan,
        month_raw=target_iso[:7],
        mode="both",
        today=today_d,
        debug=debug,
    )

    selected = None
    for week in month_payload.get("weeks") or []:
        for cell in week or []:
            if cell.get("date") == target_iso:
                selected = cell
                break
        if selected:
            break

    if selected is None:
        d = date.fromisoformat(target_iso)
        selected = {
            "date": target_iso,
            "in_month": True,
            "day": d.day,
            "dow": d.weekday(),
            "types": {"dominant": "off", "secondary": [], "tints": {}},
            "planned": {"has": False, "title": None, "kind": None},
            "actual": {"did": False, "title": None, "tonnage_t": None, "hard_sets": None, "avg_rpe": None, "toplift": None},
            "flags": {"sick": False, "alcohol": False},
            "phase": {"deload": False, "overreach": False},
            "state": {"rest": False},
        }

    workouts = _day_workout_details(training_conn, target_iso)
    runs = _day_run_details(runs_conn, training_conn, target_iso)

    weekday_names = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]
    day_obj = date.fromisoformat(target_iso)

    out = {
        "meta": {
            "date": target_iso,
            "tz": "Europe/Berlin",
            "today": today_d.isoformat(),
        },
        "day": {
            "date": target_iso,
            "weekday": weekday_names[day_obj.weekday()],
            "types": selected.get("types") or {},
            "planned": selected.get("planned") or {},
            "actual": {
                **(selected.get("actual") or {}),
                "workouts": workouts,
                "runs": runs,
            },
            "phase": selected.get("phase") or {},
            "state": selected.get("state") or {"rest": False},
            "flags": selected.get("flags") or {},
        },
    }
    if debug:
        out["debug"] = {
            "spans_in_month": month_payload.get("spans") or [],
            "cell_debug": selected.get("debug") if isinstance(selected, dict) else None,
        }
    return out
