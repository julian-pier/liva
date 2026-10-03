from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import date, datetime, timedelta, timezone
from typing import Any

from database.connections import get_core_db, get_hrv_db, get_nutrition_db, get_runs_db, get_training_db


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _today_iso() -> str:
    return date.today().isoformat()


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return float(default)
        return float(value)
    except Exception:
        return float(default)


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        if value is None:
            return int(default)
        return int(value)
    except Exception:
        return int(default)


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, float(value)))


def _parse_json(raw: Any, fallback: Any) -> Any:
    if raw in (None, ""):
        return fallback
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(str(raw))
    except Exception:
        return fallback


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


def _is_date_only(raw: Any) -> bool:
    return bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(raw or "").strip()))


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    except Exception:
        return set()


def _norm(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())


def _norm_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().lower()).strip("_")


def _dedupe_lines(lines: list[str], limit: int = 4) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in lines:
        line = _norm(raw)
        if not line:
            continue
        key = re.sub(r"[^a-z0-9äöüß]+", "", line.lower())
        if key in seen:
            continue
        seen.add(key)
        out.append(line)
        if len(out) >= limit:
            break
    return out


def ensure_core_daily_decision_schema() -> None:
    conn = get_core_db()
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS core_daily_decisions (
                day_iso TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                decision_json TEXT NOT NULL DEFAULT '{}',
                readiness_json TEXT NOT NULL DEFAULT '{}',
                status TEXT NOT NULL DEFAULT 'draft',
                message_signature TEXT,
                message_text TEXT,
                telegram_sent_at TEXT,
                telegram_message_id INTEGER,
                missing_data_used INTEGER NOT NULL DEFAULT 0,
                needs_manual_attention INTEGER NOT NULL DEFAULT 0,
                accepted_at TEXT,
                next_review_after TEXT,
                normal_update_count INTEGER NOT NULL DEFAULT 0,
                raw_context_json TEXT NOT NULL DEFAULT '{}'
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS core_daily_decision_feedback (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                day_iso TEXT NOT NULL,
                feedback TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT 'telegram',
                chat_id TEXT,
                message_id INTEGER,
                callback_id TEXT,
                created_at TEXT NOT NULL,
                raw_json TEXT NOT NULL DEFAULT '{}'
            )
            """
        )
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_core_daily_feedback_once
            ON core_daily_decision_feedback(day_iso, feedback, source)
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS core_morning_missing_prompts (
                day_iso TEXT PRIMARY KEY,
                missing_keys_json TEXT NOT NULL DEFAULT '[]',
                prompted_at TEXT NOT NULL,
                raw_json TEXT NOT NULL DEFAULT '{}'
            )
            """
        )
        conn.commit()
    finally:
        conn.close()


def _latest_hrv_signal(day_iso: str) -> dict[str, Any]:
    conn = get_hrv_db()
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            """
            SELECT date_utc, ts_measurement, rmssd, hr, sleep_quality, fatigue, training_motivation, sickness_bool
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
        return {"present_today": False, "observed_at": None}
    observed_raw = str(row["ts_measurement"] or row["date_utc"] or "")
    observed_day = str(row["date_utc"] or observed_raw)[:10]
    return {
        "present_today": observed_day == day_iso,
        "observed_at": observed_raw,
        "rmssd": row["rmssd"],
        "hr": row["hr"],
        "sleep_quality": row["sleep_quality"],
        "fatigue": row["fatigue"],
        "motivation": row["training_motivation"],
        "sick": bool(_safe_int(row["sickness_bool"], 0)),
    }


def _latest_weight_signal(day_iso: str) -> dict[str, Any]:
    conn = get_nutrition_db()
    conn.row_factory = sqlite3.Row
    try:
        cols = _table_columns(conn, "weight_logs")
        time_expr = "date_iso"
        if "updated_at" in cols and "created_at" in cols:
            time_expr = "COALESCE(updated_at, created_at, date_iso)"
        elif "updated_at" in cols:
            time_expr = "COALESCE(updated_at, date_iso)"
        elif "created_at" in cols:
            time_expr = "COALESCE(created_at, date_iso)"
        row = conn.execute(
            f"""
            SELECT date_iso, weight_kg, {time_expr} AS observed_at
            FROM weight_logs
            WHERE date_iso <= ? AND weight_kg IS NOT NULL
            ORDER BY date_iso DESC
            LIMIT 1
            """,
            (day_iso,),
        ).fetchone()
    except Exception:
        row = None
    finally:
        conn.close()
    if not row:
        return {"present_today": False, "observed_at": None}
    observed_raw = str(row["observed_at"] or row["date_iso"] or "")
    date_only = _is_date_only(observed_raw)
    return {
        "present_today": str(row["date_iso"] or "") == day_iso,
        "observed_at": observed_raw,
        "timer_observed_at": None if date_only else observed_raw,
        "date_only": date_only,
        "weight_kg": row["weight_kg"],
    }


def _missing_prompt_row(day_iso: str) -> dict[str, Any] | None:
    ensure_core_daily_decision_schema()
    conn = get_core_db()
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT * FROM core_morning_missing_prompts WHERE day_iso=? LIMIT 1",
            (day_iso,),
        ).fetchone()
    finally:
        conn.close()
    return dict(row) if row else None


def _decision_row_status(day_iso: str) -> dict[str, Any]:
    ensure_core_daily_decision_schema()
    conn = get_core_db()
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            """
            SELECT status, telegram_sent_at, accepted_at, needs_manual_attention, next_review_after
            FROM core_daily_decisions
            WHERE day_iso=?
            LIMIT 1
            """,
            (day_iso,),
        ).fetchone()
    finally:
        conn.close()
    return dict(row) if row else {}


def morning_readiness(day_iso: str | None = None) -> dict[str, Any]:
    day_iso = day_iso or _today_iso()
    hrv = _latest_hrv_signal(day_iso)
    weight = _latest_weight_signal(day_iso)
    subjective_present = bool(
        hrv.get("present_today")
        and (
            hrv.get("sleep_quality") is not None
            or hrv.get("fatigue") is not None
            or hrv.get("motivation") is not None
        )
    )
    recovery_present = bool(hrv.get("present_today") and (hrv.get("rmssd") is not None or hrv.get("hr") is not None))
    weight_present = bool(weight.get("present_today") and weight.get("weight_kg") is not None)

    signals = {
        "recovery": {"present": recovery_present, **hrv},
        "weight": {"present": weight_present, **weight},
        "subjective": {"present": subjective_present, **hrv},
    }
    missing = [key for key in ("recovery", "weight", "subjective") if not bool(signals[key].get("present"))]
    timer_times: list[datetime] = []
    for item in (hrv, weight):
        if not item:
            continue
        if not bool(item.get("present_today")):
            continue
        dt = _parse_dt(item.get("timer_observed_at") or item.get("observed_at"))
        if dt is not None:
            timer_times.append(dt)
    first_signal_at = min(timer_times) if timer_times else None
    newest_signal_at = max(timer_times) if timer_times else None
    now = datetime.now(timezone.utc)
    minutes_since_first_signal = None
    if first_signal_at is not None:
        minutes_since_first_signal = int(max(0.0, (now - first_signal_at).total_seconds() / 60.0))

    prompt_row = _missing_prompt_row(day_iso)
    row_status = _decision_row_status(day_iso)
    prompted = bool(prompt_row)
    has_today_timer_signal = bool(first_signal_at is not None)
    if row_status.get("needs_manual_attention"):
        readiness_state = "needs_manual_attention"
    elif row_status.get("accepted_at") or str(row_status.get("status") or "") == "accepted":
        readiness_state = "accepted"
    elif row_status.get("telegram_sent_at"):
        readiness_state = "sent"
    elif not missing:
        readiness_state = "decision_ready"
    elif not has_today_timer_signal:
        readiness_state = "waiting_for_morning_data"
    elif (minutes_since_first_signal or 0) >= 120:
        readiness_state = "fallback_ready"
    elif (minutes_since_first_signal or 0) >= 60:
        readiness_state = "prompted_missing" if prompted else "missing_prompt_due"
    else:
        readiness_state = "waiting_for_morning_data"

    return {
        "day_iso": day_iso,
        "complete": not missing,
        "missing": missing,
        "signals": signals,
        "state": readiness_state,
        "prompted_missing": prompted,
        "first_signal_at": first_signal_at.isoformat().replace("+00:00", "Z") if first_signal_at else None,
        "newest_signal_at": newest_signal_at.isoformat().replace("+00:00", "Z") if newest_signal_at else None,
        "minutes_since_first_signal": minutes_since_first_signal,
        "should_prompt_missing": bool(readiness_state == "missing_prompt_due"),
        "should_fallback": bool(readiness_state == "fallback_ready"),
    }


def _query_optional(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
    try:
        return list(conn.execute(sql, params).fetchall())
    except Exception:
        return []


def _training_history_context(day_iso: str) -> dict[str, Any]:
    conn = get_training_db()
    conn.row_factory = sqlite3.Row
    source_table = ""
    try:
        workouts: list[sqlite3.Row] = []
        if {"workouts", "exercises", "sets"}.issubset(_table_columns(conn, "sqlite_master")):
            workouts = []
        cols = _table_columns(conn, "workouts")
        if cols:
            source_table = "workouts"
            workouts = _query_optional(
                conn,
                """
                SELECT *
                FROM workouts
                WHERE date_iso <= ?
                ORDER BY date_iso DESC
                LIMIT 8
                """,
                (day_iso,),
            )
        if not workouts:
            source_table = "sessions"
            workouts = _query_optional(
                conn,
                """
                SELECT *
                FROM sessions
                WHERE substr(COALESCE(date_iso, date, started_at), 1, 10) <= ?
                ORDER BY COALESCE(date_iso, date, started_at) DESC
                LIMIT 8
                """,
                (day_iso,),
            )
    finally:
        conn.close()
    rows = [dict(r) for r in workouts]
    exercise_rows: list[dict[str, Any]] = []
    try:
        conn2 = get_training_db()
        conn2.row_factory = sqlite3.Row
        exercise_rows = [
            dict(r)
            for r in _query_optional(
                conn2,
                """
                SELECT w.date_iso, w.name AS workout_name, e.name, e.variation, e.device, s.reps, s.weight, s.rpe
                FROM workouts w
                JOIN exercises e ON e.workout_id = w.id
                LEFT JOIN sets s ON s.exercise_id = e.id
                WHERE w.date_iso <= ?
                ORDER BY w.date_iso DESC, e.id DESC, s.set_number ASC
                LIMIT 80
                """,
                (day_iso,),
            )
        ]
        if exercise_rows:
            source_table = "workouts/exercises/sets"
    except Exception:
        exercise_rows = []
    finally:
        try:
            conn2.close()
        except Exception:
            pass
    recent_text = json.dumps((exercise_rows or rows)[:24], ensure_ascii=False).lower()
    local: dict[str, int] = {}
    if any(tok in recent_text for tok in ("rdl", "romanian", "hinge", "deadlift", "beinbeuger", "leg curl")):
        local.update({"hamstrings": 66, "hip_hinge": 64})
    if any(tok in recent_text for tok in ("squat", "leg press", "beinpresse", "lunge")):
        local["quads"] = max(local.get("quads", 0), 56)
    if any(tok in recent_text for tok in ("pull", "row", "lat", "chin", "klimm")):
        local["upper_back"] = max(local.get("upper_back", 0), 52)
    return {
        "available": bool(rows),
        "source_table": source_table or None,
        "recent_sessions": rows[:5],
        "recent_exercises": exercise_rows[:20],
        "local_fatigue_hint": local,
    }


def _run_ergo_context(day_iso: str) -> dict[str, Any]:
    conn = get_runs_db()
    conn.row_factory = sqlite3.Row
    try:
        runs = _query_optional(
            conn,
            """
            SELECT *
            FROM runs
            WHERE substr(COALESCE(date, date_iso, start_date), 1, 10) <= ?
            ORDER BY COALESCE(date, date_iso, start_date) DESC
            LIMIT 10
            """,
            (day_iso,),
        )
    finally:
        conn.close()
    rows = [dict(r) for r in runs]
    return {"available": bool(rows), "recent_runs": rows[:6]}


def _nutrition_context(day_iso: str) -> dict[str, Any]:
    conn = get_nutrition_db()
    conn.row_factory = sqlite3.Row
    source_table = None
    try:
        rows: list[sqlite3.Row] = []
        candidates = [
            ("nutrition_daily", "date_iso"),
            ("nutrition_day_actuals", "day"),
            ("nutrition_days", "date_iso"),
            ("daily_nutrition", "date_iso"),
        ]
        for table, date_col in candidates:
            if not _table_columns(conn, table):
                continue
            rows = _query_optional(
                conn,
                f"""
                SELECT *
                FROM {table}
                WHERE {date_col} <= ?
                ORDER BY {date_col} DESC
                LIMIT 7
                """,
                (day_iso,),
            )
            source_table = table
            if rows:
                break
    finally:
        conn.close()
    recent = [dict(r) for r in rows[:7]]
    coverage = None
    if recent:
        coverage = sum(1 for r in recent if any(r.get(k) is not None for k in ("kcal", "calories", "protein", "protein_g", "p"))) / max(1, len(recent))
    return {"available": bool(rows), "source_table": source_table, "recent_days": recent, "coverage": coverage}


def _core_observation_context(day_iso: str) -> dict[str, Any]:
    conn = get_core_db()
    conn.row_factory = sqlite3.Row
    try:
        rows = _query_optional(
            conn,
            """
            SELECT *
            FROM core_observations
            WHERE substr(COALESCE(date_iso, created_at), 1, 10) <= ?
            ORDER BY COALESCE(created_at, date_iso) DESC
            LIMIT 20
            """,
            (day_iso,),
        )
    finally:
        conn.close()
    return {"available": bool(rows), "recent": [dict(r) for r in rows[:12]]}


def collect_daily_decision_context(
    day_iso: str | None = None,
    *,
    state: dict[str, Any] | None = None,
    ns_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    day_iso = day_iso or _today_iso()
    readiness = morning_readiness(day_iso)
    training = _training_history_context(day_iso)
    runs = _run_ergo_context(day_iso)
    nutrition = _nutrition_context(day_iso)
    observations = _core_observation_context(day_iso)
    return {
        "day_iso": day_iso,
        "readiness": readiness,
        "hrv": readiness.get("signals", {}).get("recovery", {}),
        "weight": readiness.get("signals", {}).get("weight", {}),
        "subjective": readiness.get("signals", {}).get("subjective", {}),
        "planned_unit": _planned_unit_from_payload(ns_payload or {}),
        "next_session": ns_payload or {"available": False},
        "state": state or {"available": False},
        "training_history": training,
        "local_fatigue": training.get("local_fatigue_hint") or {},
        "cardio_load": runs,
        "nutrition": nutrition,
        "calendar_stress": ((state or {}).get("stress") if isinstance((state or {}).get("stress"), dict) else {"available": False}),
        "observations": observations,
        "behavior": observations,
        "overrides": _feedback_adjustment(day_iso),
    }


def _planned_unit_from_payload(ns_payload: dict[str, Any]) -> dict[str, Any]:
    final_day = ns_payload.get("final_day") if isinstance(ns_payload.get("final_day"), dict) else {}
    planned_day = ns_payload.get("planned_day") if isinstance(ns_payload.get("planned_day"), dict) else {}
    session = ns_payload.get("session") if isinstance(ns_payload.get("session"), dict) else {}
    kind = str(final_day.get("kind") or ns_payload.get("type") or "").strip().lower()
    title = _norm(session.get("session_name") or final_day.get("label") or ns_payload.get("title") or "Rest")
    if title.lower() in {"wird vorbereitet", "prepared", "loading", "error", ""}:
        title = ""
    if kind == "plan":
        unit_type = "gym"
        title = title or _norm(final_day.get("label") or planned_day.get("session_name") or planned_day.get("label") or "Gym")
    elif kind == "run":
        unit_type = "run"
        run_kind = str(final_day.get("run_kind") or planned_day.get("run_kind") or "").strip().lower()
        title = "Z2 Run" if run_kind in {"", "z2", "easy"} else "Threshold Run"
    elif kind in {"ergo", "bike"}:
        unit_type = "ergo"
        title = title or "Ergo Z2"
    elif kind == "rest":
        unit_type = "rest"
        title = "Rest / Routine"
    else:
        unit_type = kind or "unknown"
        title = title or "Noch offen"
    return {
        "type": unit_type,
        "plan_name": title or _display_unit_label({"type": unit_type}),
        "source_kind": kind or "rest",
        "planned_label": _norm(planned_day.get("label") or planned_day.get("session_name") or ""),
        "final_label": _norm(final_day.get("label") or title),
        "run_kind": str(final_day.get("run_kind") or planned_day.get("run_kind") or "").strip().lower(),
        "duration_min": _safe_int(final_day.get("run_duration_min") or ((ns_payload.get("blocks") or {}).get("run") or {}).get("duration_min"), 0) or None,
    }


def _display_unit_label(unit: dict[str, Any] | None) -> str:
    unit = unit if isinstance(unit, dict) else {}
    unit_type = str(unit.get("type") or "").strip().lower()
    raw = _norm(unit.get("plan_name") or unit.get("final_label") or unit.get("planned_label") or "")
    if raw.lower() in {"wird vorbereitet", "prepared", "loading", "rest", ""}:
        raw = ""
    if unit_type == "gym":
        return raw or "Gym"
    if unit_type == "run":
        run_kind = str(unit.get("run_kind") or "").strip().lower()
        if raw and raw.lower() not in {"run", "laufen"}:
            return raw
        return "Z2 Run" if run_kind in {"", "z2", "easy"} else "Threshold Run"
    if unit_type == "ergo":
        return raw if raw and raw.lower() != "ergo" else "Ergo Z2"
    if unit_type == "rest":
        return "Rest / Routine"
    if unit_type == "mobility":
        return raw or "Mobility"
    return raw or "Noch offen"


def build_core_daily_ui_payload(day_iso: str | None = None) -> dict[str, Any]:
    day_iso = day_iso or _today_iso()
    decision = get_daily_decision(day_iso) or {}
    readiness = decision.get("data_readiness") if isinstance(decision.get("data_readiness"), dict) else morning_readiness(day_iso)
    unit = decision.get("planned_unit") if isinstance(decision.get("planned_unit"), dict) else {}
    unit = dict(unit or {})
    unit["display_label"] = _display_unit_label(unit)
    if unit.get("type") == "unknown":
        unit["type"] = "waiting"

    consequences = decision.get("consequences") if isinstance(decision.get("consequences"), dict) else {}
    constraints = consequences.get("training_constraints") if isinstance(consequences.get("training_constraints"), dict) else {}
    state = str(readiness.get("state") or "").strip()
    missing = [str(x) for x in (readiness.get("missing") or []) if str(x).strip()]
    if state == "waiting_for_morning_data":
        hero = "CORE wartet noch auf Morgenwerte."
        display_status = "HRV, Gewicht und Check-in eintragen"
    elif state in {"missing_prompt_due", "prompted_missing"}:
        hero = str(decision.get("hero") or "").strip() or "CORE wartet noch auf die fehlenden Morgenwerte."
        display_status = "Es fehlen noch: " + ", ".join(missing) if missing else "Morgenwerte unvollständig"
    elif decision.get("missing_data_used") or state == "fallback_ready":
        hero = str(decision.get("hero") or "").strip() or "Entscheidung mit fehlenden Morgenwerten."
        display_status = "Entscheidung mit fehlenden Morgenwerten"
    elif decision:
        hero = str(decision.get("hero") or "").strip() or "CORE Daily Decision steht."
        display_status = "Morgenwerte vollständig" if readiness.get("complete") else "Daily Decision aktiv"
    else:
        hero = "CORE wartet noch auf Morgenwerte."
        display_status = "HRV, Gewicht und Check-in eintragen"

    return {
        "ok": bool(decision),
        "day_iso": day_iso,
        "daily_decision": decision,
        "planned_unit": unit,
        "hero": hero,
        "data_readiness": readiness,
        "permissions": decision.get("permissions") if isinstance(decision.get("permissions"), dict) else {},
        "training_constraints": constraints,
        "reasons": decision.get("reasons") if isinstance(decision.get("reasons"), list) else [],
        "display_status": display_status,
        "missing_data_used": bool(decision.get("missing_data_used")),
        "needs_manual_attention": bool(decision.get("needs_manual_attention")),
    }


def _local_fatigue_from_state(state: dict[str, Any], ns_payload: dict[str, Any]) -> dict[str, int]:
    flags = state.get("flags") if isinstance(state.get("flags"), dict) else {}
    local = flags.get("local_fatigue") if isinstance(flags.get("local_fatigue"), dict) else {}
    fatigue = state.get("fatigue") if isinstance(state.get("fatigue"), dict) else {}
    preload = _clamp(_safe_float(fatigue.get("muscular_preload"), 0.0) * 100.0)
    risks: dict[str, int] = {}
    if bool(local.get("legs")):
        risks.update({"quads": 68, "hamstrings": 72, "glutes": 58, "hip_hinge": 68})
    if bool(local.get("pull")):
        risks.update({"lats": 66, "upper_back": 64, "elbow_flexors": 56})
    if bool(local.get("push")):
        risks.update({"chest": 62, "front_delts": 60, "triceps": 58})
    if bool(flags.get("zns_fatigue")):
        risks["systemic"] = max(risks.get("systemic", 0), 78)
    if preload >= 35:
        risks["general_muscular"] = max(risks.get("general_muscular", 0), int(round(preload)))

    text_blob = json.dumps(ns_payload.get("items") or [], ensure_ascii=False).lower()
    if any(tok in text_blob for tok in ("rdl", "romanian deadlift", "beinbeuger", "leg curl")) and risks.get("hamstrings", 0) >= 60:
        risks["hamstrings"] = max(risks.get("hamstrings", 0), 78)
        risks["hip_hinge"] = max(risks.get("hip_hinge", 0), 74)
    return risks


def _feedback_adjustment(day_iso: str) -> dict[str, Any]:
    conn = get_core_db()
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT feedback, created_at
            FROM core_daily_decision_feedback
            WHERE day_iso=?
            ORDER BY id DESC
            LIMIT 8
            """,
            (day_iso,),
        ).fetchall()
    except Exception:
        rows = []
    finally:
        conn.close()
    feedback = [str(r["feedback"] or "").strip() for r in rows]
    return {
        "accepted": "accepted" in feedback,
        "lighter": "lighter" in feedback,
        "harder": "harder" in feedback,
        "later": "review_later" in feedback,
        "manual": "plan_change" in feedback,
        "latest": feedback[0] if feedback else "",
    }


def _reason(code: str, text: str, impact: int) -> dict[str, Any]:
    return {"code": code, "text": text, "impact": int(_clamp(impact, 0, 100))}


def build_daily_decision(
    *,
    day_iso: str,
    state: dict[str, Any],
    ns_payload: dict[str, Any],
    legacy_decision: dict[str, Any] | None = None,
    scenarios: dict[str, Any] | None = None,
    missing_data_used: bool | None = None,
) -> dict[str, Any]:
    legacy_decision = legacy_decision if isinstance(legacy_decision, dict) else {}
    planned = _planned_unit_from_payload(ns_payload)
    readiness = morning_readiness(day_iso)
    if missing_data_used is None:
        missing_data_used = bool(readiness.get("should_fallback"))
    context = collect_daily_decision_context(day_iso, state=state, ns_payload=ns_payload)

    recovery = state.get("recovery") if isinstance(state.get("recovery"), dict) else {}
    fatigue = state.get("fatigue") if isinstance(state.get("fatigue"), dict) else {}
    nutrition = state.get("nutrition") if isinstance(state.get("nutrition"), dict) else {}
    load = state.get("load") if isinstance(state.get("load"), dict) else {}
    run_ctx = state.get("run_context") if isinstance(state.get("run_context"), dict) else {}
    stress = state.get("stress") if isinstance(state.get("stress"), dict) else {}
    illness = state.get("illness") if isinstance(state.get("illness"), dict) else {}
    subjective = (ns_payload.get("session") or {}).get("meta", {}).get("decision_meta", {}).get("inputs", {}).get("subjective")
    if not isinstance(subjective, dict):
        subjective = {}

    hrv_pressure = _clamp(_safe_float(recovery.get("hrv_pressure"), 0.0) * 100.0)
    back_to_back = _clamp(_safe_float(load.get("back_to_back_risk"), 0.0) * 100.0)
    run_interference = _clamp(_safe_float(run_ctx.get("interference_risk"), 0.0) * 100.0)
    nutrition_gap = _clamp((1.0 - _safe_float(nutrition.get("energy_support_score"), 0.65)) * 100.0)
    calendar_friction = _clamp(_safe_float(stress.get("calendar_friction_score"), _safe_float(stress.get("daily_stress"), 0.0)) * 100.0)
    muscular_preload = _clamp(_safe_float(fatigue.get("muscular_preload"), 0.0) * 100.0)
    sick = bool(illness.get("sick") or recovery.get("sick_flag"))

    local_risk = _local_fatigue_from_state(state, ns_payload)
    for key, val in (context.get("local_fatigue") or {}).items():
        local_risk[str(key)] = max(_safe_int(local_risk.get(str(key)), 0), _safe_int(val, 0))
    max_local = max(local_risk.values(), default=0)
    recovery_risk = int(round(_clamp(hrv_pressure * 0.42 + back_to_back * 0.20 + nutrition_gap * 0.16 + muscular_preload * 0.14 + calendar_friction * 0.08)))
    if sick:
        recovery_risk = max(recovery_risk, 92)

    progression_permission = int(round(_clamp(88 - recovery_risk * 0.52 - max_local * 0.22 - nutrition_gap * 0.14 - calendar_friction * 0.08)))
    volume_permission = int(round(_clamp(86 - recovery_risk * 0.36 - muscular_preload * 0.32 - back_to_back * 0.18 - calendar_friction * 0.12)))
    intensity_permission = int(round(_clamp(88 - recovery_risk * 0.46 - max_local * 0.18 - hrv_pressure * 0.18)))
    cardio_quality_permission = int(round(_clamp(82 - run_interference * 0.35 - recovery_risk * 0.26 - max_local * 0.14 - calendar_friction * 0.10)))
    execution_strictness = int(round(_clamp(48 + recovery_risk * 0.35 + max_local * 0.26 + calendar_friction * 0.12 + (20 if missing_data_used else 0))))

    feedback = _feedback_adjustment(day_iso)
    if feedback.get("lighter"):
        progression_permission = int(_clamp(progression_permission - 18))
        volume_permission = int(_clamp(volume_permission - 14))
        intensity_permission = int(_clamp(intensity_permission - 12))
        execution_strictness = int(_clamp(execution_strictness + 16))
    elif feedback.get("harder"):
        progression_permission = int(_clamp(progression_permission + 6))
        intensity_permission = int(_clamp(intensity_permission + 4))
        execution_strictness = int(_clamp(execution_strictness + 6))

    if sick:
        progression_permission = min(progression_permission, 5)
        volume_permission = min(volume_permission, 12)
        intensity_permission = min(intensity_permission, 8)
        cardio_quality_permission = min(cardio_quality_permission, 10)
        execution_strictness = max(execution_strictness, 92)

    constraints: dict[str, Any] = {
        "blocked_exercises": [],
        "blocked_patterns": [],
        "caution_exercises": [],
        "caution_patterns": [],
        "volume_multiplier": round(max(0.55, min(1.08, volume_permission / 82.0)), 2),
        "set_delta": -1 if volume_permission < 56 else 0,
        "allow_progression": bool(progression_permission >= 68 and not sick and not feedback.get("lighter")),
        "allow_topset": bool(intensity_permission >= 58 and not sick),
        "rpe_cap": 9.0 if intensity_permission >= 78 else (8.5 if intensity_permission >= 62 else (8.0 if intensity_permission >= 42 else 7.0)),
        "rep_target_bias": "normal",
        "exercise_substitutions": [],
        "cardio_after": None,
        "notes_for_card": [],
        "execution_strictness": execution_strictness,
    }
    if not constraints["allow_progression"]:
        constraints["rep_target_bias"] = "technique" if execution_strictness >= 72 else "lower_load"
        constraints["notes_for_card"].append("Heute keine PR-/Lastsprung-Sprache anzeigen.")

    if local_risk.get("hamstrings", 0) >= 72 or local_risk.get("hip_hinge", 0) >= 72:
        constraints["blocked_exercises"] = ["Romanian Deadlift", "RDL"]
        constraints["blocked_patterns"] = ["hip_hinge"]
        constraints["caution_patterns"] = ["hamstrings", "hip_hinge"]
        constraints["exercise_substitutions"] = [
            {"from": "Romanian Deadlift", "to": "Leg Curl", "reason": "hamstring/hinge fatigue"},
            {"from": "RDL", "to": "Leg Curl", "reason": "hamstring/hinge fatigue"},
        ]
        constraints["notes_for_card"].append("Hinge-Reiz heute nicht priorisieren; Hamstrings nur kontrolliert.")
    elif local_risk.get("hamstrings", 0) >= 58:
        constraints["caution_patterns"] = ["hamstrings", "hip_hinge"]
    if local_risk.get("upper_back", 0) >= 64:
        constraints["caution_patterns"] = _dedupe_lines(list(constraints["caution_patterns"]) + ["pull", "upper_back"], 6)

    cardio_after = None
    if planned["type"] == "gym" and cardio_quality_permission >= 46 and run_interference < 56 and not sick:
        cardio_after = {"type": "ergo", "duration_min": 32, "target_hr": 135, "intent": "controlled_z2"}
        constraints["cardio_after"] = cardio_after
    if planned["type"] == "run" and (local_risk.get("hamstrings", 0) >= 68 or run_interference >= 66):
        planned["type"] = "ergo"
        planned["plan_name"] = "Ergo Z2"
        constraints["cardio_after"] = {"type": "ergo", "duration_min": 32, "target_hr": 135, "intent": "controlled_z2"}
        constraints["notes_for_card"].append("Run-Impact heute vermeiden; Z2 auf Ergo ersetzen.")
    if sick:
        constraints.update(
            {
                "blocked_patterns": _dedupe_lines(list(constraints.get("blocked_patterns") or []) + ["high_intensity", "impact", "heavy_loading"], 8),
                "volume_multiplier": min(float(constraints.get("volume_multiplier") or 1.0), 0.55),
                "set_delta": min(_safe_int(constraints.get("set_delta"), 0), -2),
                "allow_progression": False,
                "allow_topset": False,
                "rpe_cap": min(float(constraints.get("rpe_cap") or 7.0), 6.0),
                "rep_target_bias": "technique",
                "cardio_after": None,
            }
        )
        constraints["notes_for_card"].append("Infektflag: Training stoppen oder nur sehr locker bewegen.")

    reasons: list[dict[str, Any]] = []
    if sick:
        reasons.append(_reason("sickness", "Infekt-/Krankheitsflag hat Vorrang.", 96))
    if max_local >= 68:
        if local_risk.get("hamstrings", 0) >= 68:
            reasons.append(_reason("local_hamstrings", "Der Engpass sitzt lokal eher in Hamstrings/Hinge als im ganzen System.", local_risk.get("hamstrings", 0)))
        else:
            reasons.append(_reason("local_fatigue", "Lokale Fatigue begrenzt heute die Übungsauswahl.", max_local))
    if hrv_pressure >= 30:
        reasons.append(_reason("recovery", "HRV/Ruhepuls geben weniger Reserve frei.", int(hrv_pressure)))
    if back_to_back >= 35:
        reasons.append(_reason("recent_load", "Die letzten harten Tage erhöhen die Folgekosten.", int(back_to_back)))
    if nutrition_gap >= 36:
        reasons.append(_reason("nutrition", "Energie-/Ernährungslage trägt heute keinen aggressiven Push.", int(nutrition_gap)))
    if calendar_friction >= 55:
        reasons.append(_reason("calendar", "Der Tag hat genug Reibung, dass saubere Ausführung wichtiger ist als Draufpacken.", int(calendar_friction)))
    if missing_data_used:
        reasons.append(_reason("missing_morning_data", "CORE entscheidet mit unvollständiger Morgenlage.", 45))
    reasons = sorted(reasons, key=lambda x: int(x.get("impact") or 0), reverse=True)[:3]

    plan_name = planned.get("plan_name") or "Training"
    if sick:
        hero = "Heute kein Training erzwingen; Recovery hat Vorrang."
        do = ["Akute Lage ernst nehmen", "nur lockere Bewegung, wenn sie wirklich gut tut"]
        avoid = ["Training erzwingen", "Puls- oder Lastdruck"]
        watch = ["Infektzeichen", "Ruhepuls und Energie im Tagesverlauf"]
    elif planned["type"] == "ergo":
        hero = "Heute Z2 nicht als Lauf, sondern kontrolliert auf dem Ergo fahren."
        do = ["32 Minuten Ergo ruhig halten", "Atmung und Puls stabil lassen"]
        avoid = ["Watt-Jagen", "aus dem Ergo doch einen Lauf machen"]
        watch = ["Hamstrings", "Pulsdrift", "Beine beim Anfahren"]
    elif planned["type"] == "gym":
        if progression_permission >= 72 and max_local < 58:
            hero = f"Heute {plan_name} sauber ausführen; kleine Progression ist erlaubt, wenn die ersten Sätze es hergeben."
        elif progression_permission >= 48:
            hero = f"Heute {plan_name} normal ausführen, aber Progression nicht aktiv suchen."
        else:
            hero = f"Heute {plan_name} bleibt drin, aber eng geführt und ohne Last-Ego."
        do = ["Plan starten und nach dem ersten Arbeitsblock ehrlich prüfen", "saubere Reps vor Lastsprung"]
        avoid = ["Progression erzwingen"] if not constraints["allow_progression"] else ["Extra-Sätze ohne klaren Grund"]
        watch = []
        if local_risk.get("hamstrings", 0) >= 58:
            watch.append("Hamstrings bei Squats/Hinge")
        if execution_strictness >= 70:
            watch.append("RPE-Ehrlichkeit")
        if cardio_after:
            do.append("danach 32 Minuten Ergo kontrolliert")
            avoid.append("Cardio als zweiten Wettkampf fahren")
    elif planned["type"] == "run":
        hero = f"Heute {plan_name} kontrolliert laufen; Qualität nur, wenn Puls und Beine früh stabil bleiben."
        do = ["Run ruhig starten", "Pulsdeckel respektieren"]
        avoid = ["Pace erzwingen", "späte Eskalation"]
        watch = ["Pulsdrift", "Waden/Hamstrings", "Atmung"]
    elif planned["type"] == "rest":
        hero = "Heute ist kein Training der Hebel; Erholung und Tagesroutine sind die Entscheidung."
        do = ["Bewegung niedrigschwellig halten", "Essen und Schlafroutine absichern"]
        avoid = ["spontane harte Einheit"]
        watch = ["Energie im Tagesverlauf", "Abendroutine"]
    else:
        hero = f"Heute {plan_name} kontrolliert halten und nur so weit gehen, wie die Tageslage es trägt."
        do = ["Plan klein halten", "früh stoppen, wenn die Signale kippen"]
        avoid = ["spontanes Draufpacken"]
        watch = ["Energie", "lokale Fatigue"]

    if constraints["blocked_exercises"]:
        avoid.append("RDLs oder schwere Hip-Hinge-Varianten")
    if missing_data_used:
        watch.append("Entscheidung wurde ohne vollständige Morgenwerte getroffen")

    legacy_mode = "REST" if sick or planned["type"] == "rest" else ("LIGHT" if progression_permission < 42 or intensity_permission < 42 else ("NORMAL" if progression_permission < 76 else "HEAVY"))
    if planned["type"] in {"run", "ergo"} and legacy_mode == "HEAVY":
        legacy_mode = "NORMAL"

    return {
        "id": f"daily:{day_iso}",
        "day_iso": day_iso,
        "hero": hero,
        "planned_unit": planned,
        "permissions": {
            "progression_permission": progression_permission,
            "volume_permission": volume_permission,
            "intensity_permission": intensity_permission,
            "top_set_permission": int(_clamp(intensity_permission - max(0, recovery_risk - 45) * 0.35)),
            "exercise_substitution_pressure": int(_clamp(max_local * 0.72 + recovery_risk * 0.18)),
            "cardio_quality_permission": cardio_quality_permission,
            "recovery_risk": recovery_risk,
            "local_fatigue_risk": local_risk,
            "execution_strictness": execution_strictness,
            "gym": {
                "progression_permission": progression_permission,
                "volume_permission": volume_permission,
                "intensity_permission": intensity_permission,
                "top_set_permission": int(_clamp(intensity_permission - max(0, recovery_risk - 45) * 0.35)),
                "exercise_substitution_pressure": int(_clamp(max_local * 0.72 + recovery_risk * 0.18)),
                "execution_strictness": execution_strictness,
                "recovery_risk": recovery_risk,
                "local_fatigue_risk": local_risk,
            },
            "run": {
                "run_permission": int(_clamp(cardio_quality_permission - max(0, local_risk.get("hamstrings", 0) - 55) * 0.55)),
                "pace_permission": int(_clamp(cardio_quality_permission - run_interference * 0.35 - recovery_risk * 0.18)),
                "z2_strictness": int(_clamp(52 + recovery_risk * 0.28 + run_interference * 0.24)),
                "impact_risk": int(_clamp(local_risk.get("hamstrings", 0) * 0.55 + run_interference * 0.35)),
                "local_leg_risk": int(_clamp(max(local_risk.get("hamstrings", 0), local_risk.get("quads", 0), local_risk.get("hip_hinge", 0)))),
                "cardio_quality_permission": cardio_quality_permission,
            },
            "ergo": {
                "ergo_permission": int(_clamp(cardio_quality_permission + 8 - recovery_risk * 0.08)),
                "target_hr_permission": int(_clamp(cardio_quality_permission + 5)),
                "watt_chasing_risk": int(_clamp(100 - execution_strictness + intensity_permission * 0.18)),
                "z2_strictness": int(_clamp(56 + recovery_risk * 0.24 + run_interference * 0.18)),
                "cardio_quality_permission": cardio_quality_permission,
            },
            "rest_mobility": {
                "recovery_priority": int(_clamp(recovery_risk + (35 if sick else 0))),
                "routine_priority": int(_clamp(45 + nutrition_gap * 0.24 + calendar_friction * 0.18)),
                "spontaneous_training_risk": int(_clamp(100 - execution_strictness + motivation if (motivation := _safe_float(subjective.get("motivation"), 0.0)) else 100 - execution_strictness)),
            },
        },
        "consequences": {
            "do": _dedupe_lines(do, 5),
            "avoid": _dedupe_lines(avoid, 5),
            "watch": _dedupe_lines(watch, 5),
            "training_constraints": constraints,
            "smart_home_actions": [],
        },
        "reasons": reasons,
        "data_readiness": readiness,
        "missing_data_used": bool(missing_data_used),
        "feedback": feedback,
        "legacy_compat": {
            "mode": legacy_mode,
            "source": "daily_permissions",
            "note": "legacy only, not coaching decision",
        },
        "status": "needs_manual_attention" if feedback.get("manual") else ("accepted" if feedback.get("accepted") else "proposed"),
        "needs_manual_attention": bool(feedback.get("manual")),
    }


def upsert_daily_decision(
    *,
    day_iso: str,
    decision: dict[str, Any],
    readiness: dict[str, Any] | None = None,
    raw_context: dict[str, Any] | None = None,
) -> None:
    ensure_core_daily_decision_schema()
    now = _utc_now()
    conn = get_core_db()
    try:
        row = conn.execute("SELECT created_at FROM core_daily_decisions WHERE day_iso=?", (day_iso,)).fetchone()
        created_at = str(row[0]) if row else now
        conn.execute(
            """
            INSERT INTO core_daily_decisions (
                day_iso, created_at, updated_at, decision_json, readiness_json, status,
                missing_data_used, needs_manual_attention, raw_context_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(day_iso) DO UPDATE SET
                updated_at=excluded.updated_at,
                decision_json=excluded.decision_json,
                readiness_json=excluded.readiness_json,
                status=excluded.status,
                missing_data_used=excluded.missing_data_used,
                needs_manual_attention=excluded.needs_manual_attention,
                raw_context_json=excluded.raw_context_json
            """,
            (
                day_iso,
                created_at,
                now,
                json.dumps(decision or {}, ensure_ascii=False),
                json.dumps(readiness or decision.get("data_readiness") or {}, ensure_ascii=False),
                str(decision.get("status") or "proposed"),
                1 if bool(decision.get("missing_data_used")) else 0,
                1 if bool(decision.get("needs_manual_attention")) else 0,
                json.dumps(raw_context or {}, ensure_ascii=False),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def get_daily_decision(day_iso: str | None = None) -> dict[str, Any] | None:
    ensure_core_daily_decision_schema()
    day_iso = day_iso or _today_iso()
    conn = get_core_db()
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            """
            SELECT *
            FROM core_daily_decisions
            WHERE day_iso <= ?
            ORDER BY day_iso DESC
            LIMIT 1
            """,
            (day_iso,),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    item = dict(row)
    decision = _parse_json(item.get("decision_json"), {})
    if isinstance(decision, dict):
        decision["_row"] = {
            "status": item.get("status"),
            "telegram_sent_at": item.get("telegram_sent_at"),
            "message_signature": item.get("message_signature"),
            "needs_manual_attention": bool(item.get("needs_manual_attention")),
            "accepted_at": item.get("accepted_at"),
            "next_review_after": item.get("next_review_after"),
            "normal_update_count": _safe_int(item.get("normal_update_count"), 0),
        }
        return decision
    return None


def latest_training_constraints(day_iso: str | None = None) -> dict[str, Any]:
    ensure_core_daily_decision_schema()
    day_iso = day_iso or _today_iso()
    conn = get_core_db()
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT decision_json FROM core_daily_decisions WHERE day_iso=? LIMIT 1", (day_iso,)).fetchone()
    finally:
        conn.close()
    decision = _parse_json(row["decision_json"], {}) if row else {}
    if not isinstance(decision, dict):
        return {}
    consequences = decision.get("consequences") if isinstance(decision.get("consequences"), dict) else {}
    constraints = consequences.get("training_constraints") if isinstance(consequences.get("training_constraints"), dict) else {}
    return dict(constraints or {})


def _claim_missing_prompt(day_iso: str, missing: list[str], readiness: dict[str, Any]) -> bool:
    ensure_core_daily_decision_schema()
    conn = get_core_db()
    try:
        row = conn.execute("SELECT 1 FROM core_morning_missing_prompts WHERE day_iso=? LIMIT 1", (day_iso,)).fetchone()
        if row:
            return False
        conn.execute(
            """
            INSERT INTO core_morning_missing_prompts (day_iso, missing_keys_json, prompted_at, raw_json)
            VALUES (?, ?, ?, ?)
            """,
            (
                day_iso,
                json.dumps(missing, ensure_ascii=False),
                _utc_now(),
                json.dumps(readiness or {}, ensure_ascii=False),
            ),
        )
        conn.commit()
        return True
    finally:
        conn.close()


def maybe_prompt_missing_morning_data(day_iso: str | None = None) -> dict[str, Any]:
    day_iso = day_iso or _today_iso()
    readiness = morning_readiness(day_iso)
    missing = list(readiness.get("missing") or [])
    if readiness.get("state") == "prompted_missing":
        return {"ok": True, "sent": False, "reason": "already_prompted", "readiness": readiness}
    if not readiness.get("should_prompt_missing") or not missing:
        return {"ok": True, "sent": False, "reason": "not_due", "readiness": readiness}
    if not _claim_missing_prompt(day_iso, missing, readiness):
        return {"ok": True, "sent": False, "reason": "already_prompted", "readiness": readiness}
    labels = {"recovery": "HRV/Recovery-Messung", "weight": "Gewicht", "subjective": "Morning-Check"}
    missing_txt = ", ".join(labels.get(x, x) for x in missing)
    if missing == ["weight"]:
        text = "Mir fehlt noch dein Gewicht. Trag es bitte ein, dann mache ich die Tagesentscheidung sauber."
    else:
        text = f"Mir fehlt noch {missing_txt}. Trag das bitte ein, dann mache ich die Tagesentscheidung sauber."
    try:
        from integrations.telegram_hub import send_domain_message

        sent = send_domain_message("core", text)
    except Exception as exc:
        return {"ok": False, "sent": False, "error": str(exc), "readiness": readiness}
    return {"ok": bool(sent.get("ok")), "sent": bool(sent.get("ok")), "telegram": sent, "readiness": readiness}


def _message_signature(decision: dict[str, Any], text: str) -> str:
    constraints = ((decision.get("consequences") or {}).get("training_constraints") or {}) if isinstance(decision.get("consequences"), dict) else {}
    payload = {
        "hero": decision.get("hero"),
        "planned": decision.get("planned_unit"),
        "permissions": decision.get("permissions"),
        "constraints": constraints,
        "reasons": [r.get("code") for r in (decision.get("reasons") or []) if isinstance(r, dict)],
        "text_core": re.sub(r"\d+", "#", text.lower()),
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def compose_morning_message(decision: dict[str, Any]) -> tuple[str, str]:
    planned = decision.get("planned_unit") if isinstance(decision.get("planned_unit"), dict) else {}
    consequences = decision.get("consequences") if isinstance(decision.get("consequences"), dict) else {}
    permissions = decision.get("permissions") if isinstance(decision.get("permissions"), dict) else {}
    constraints = consequences.get("training_constraints") if isinstance(consequences.get("training_constraints"), dict) else {}

    parts: list[str] = ["Guten Morgen."]
    hero = _norm(decision.get("hero"))
    if hero:
        parts.append(hero)
    reasons = [str(r.get("text") or "").strip() for r in (decision.get("reasons") or []) if isinstance(r, dict)]
    if reasons:
        parts.append(reasons[0])
    watch = _dedupe_lines([str(x) for x in (consequences.get("watch") or [])], 2)
    if watch:
        parts.append("Achte besonders auf " + " und ".join(watch[:2]) + ".")
    if constraints.get("blocked_exercises"):
        parts.append("RDLs bleiben heute raus.")
    cardio = constraints.get("cardio_after") if isinstance(constraints.get("cardio_after"), dict) else None
    if cardio:
        duration = _safe_int(cardio.get("duration_min"), 32)
        target = _safe_int(cardio.get("target_hr"), 135)
        parts.append(f"Danach {duration}:00 Ergo bei ca. {target} bpm, kein Watt-Jagen.")
    elif planned.get("type") == "ergo":
        parts.append("Fahr das Ergo ruhig und brich die Watt-Jagd früh ab.")
    if decision.get("missing_data_used"):
        parts.append("Ein Teil der Morgenwerte fehlt noch, deshalb bleibt die Entscheidung bewusst enger.")
    if _safe_int(permissions.get("progression_permission"), 0) < 50 and planned.get("type") == "gym":
        parts.append("Progression wird heute nicht aktiv gesucht.")

    text = " ".join(_dedupe_lines(parts, 8))
    text = re.sub(r"\s+", " ", text).strip()
    sig = _message_signature(decision, text)
    return text, sig


def core_morning_reply_markup(day_iso: str) -> dict[str, Any]:
    compact_day = str(day_iso or _today_iso()).replace("-", "")
    return {
        "inline_keyboard": [
            [
                {"text": "Passt", "callback_data": f"core:ok:{compact_day}"},
                {"text": "Leichter", "callback_data": f"core:lighter:{compact_day}"},
                {"text": "Härter", "callback_data": f"core:harder:{compact_day}"},
            ],
            [
                {"text": "Später neu prüfen", "callback_data": f"core:later:{compact_day}"},
                {"text": "Plan ändern", "callback_data": f"core:plan:{compact_day}"},
            ],
        ]
    }


def maybe_send_core_morning_message(day_iso: str | None = None, *, force: bool = False) -> dict[str, Any]:
    ensure_core_daily_decision_schema()
    day_iso = day_iso or _today_iso()
    decision = get_daily_decision(day_iso)
    if not isinstance(decision, dict):
        return {"ok": False, "sent": False, "error": "missing_decision"}
    readiness = decision.get("data_readiness") if isinstance(decision.get("data_readiness"), dict) else morning_readiness(day_iso)
    if not force and (not readiness.get("complete")) and not decision.get("missing_data_used"):
        prompt = maybe_prompt_missing_morning_data(day_iso)
        return {"ok": bool(prompt.get("ok")), "sent": False, "reason": "waiting_for_morning_data", "prompt": prompt}

    text, sig = compose_morning_message(decision)
    conn = get_core_db()
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT telegram_sent_at, message_signature, normal_update_count FROM core_daily_decisions WHERE day_iso=? LIMIT 1",
            (day_iso,),
        ).fetchone()
        already_sent = bool(row and row["telegram_sent_at"])
        same_signature = bool(row and str(row["message_signature"] or "") == sig)
        if already_sent and same_signature and not force:
            return {"ok": True, "sent": False, "reason": "already_sent"}
        if already_sent and not force and _safe_int(row["normal_update_count"] if row else 0, 0) >= 1:
            return {"ok": True, "sent": False, "reason": "daily_update_cap_reached"}
    finally:
        conn.close()

    try:
        from integrations.telegram_hub import send_domain_message

        sent = send_domain_message("core", text, reply_markup=core_morning_reply_markup(day_iso))
    except Exception as exc:
        return {"ok": False, "sent": False, "error": str(exc)}
    if not sent.get("ok"):
        return {"ok": False, "sent": False, "error": sent.get("error") or "send_failed", "telegram": sent}

    conn2 = get_core_db()
    try:
        conn2.execute(
            """
            UPDATE core_daily_decisions
            SET telegram_sent_at=?,
                telegram_message_id=?,
                message_signature=?,
                message_text=?,
                normal_update_count=CASE
                    WHEN telegram_sent_at IS NOT NULL THEN normal_update_count + 1
                    ELSE normal_update_count
                END
            WHERE day_iso=?
            """,
            (_utc_now(), sent.get("message_id"), sig, text, day_iso),
        )
        conn2.commit()
    finally:
        conn2.close()
    return {"ok": True, "sent": True, "message_id": sent.get("message_id"), "signature": sig}


def record_daily_decision_feedback(
    *,
    day_iso: str,
    feedback: str,
    source: str = "telegram",
    chat_id: str | None = None,
    message_id: int | None = None,
    callback_id: str | None = None,
    raw: dict[str, Any] | None = None,
) -> dict[str, Any]:
    ensure_core_daily_decision_schema()
    feedback = str(feedback or "").strip().lower()
    if feedback not in {"accepted", "lighter", "harder", "review_later", "plan_change"}:
        return {"ok": False, "error": "invalid_feedback"}
    day_iso = str(day_iso or _today_iso()).strip() or _today_iso()
    now = _utc_now()
    conn = get_core_db()
    try:
        conn.execute(
            """
            INSERT OR IGNORE INTO core_daily_decision_feedback (
                day_iso, feedback, source, chat_id, message_id, callback_id, created_at, raw_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                day_iso,
                feedback,
                source,
                chat_id,
                int(message_id) if message_id else None,
                callback_id,
                now,
                json.dumps(raw or {}, ensure_ascii=False),
            ),
        )
        if feedback == "accepted":
            conn.execute("UPDATE core_daily_decisions SET status='accepted', accepted_at=? WHERE day_iso=?", (now, day_iso))
        elif feedback == "review_later":
            conn.execute(
                "UPDATE core_daily_decisions SET status='review_later', next_review_after=? WHERE day_iso=?",
                ((datetime.now(timezone.utc) + timedelta(hours=2)).replace(microsecond=0).isoformat().replace("+00:00", "Z"), day_iso),
            )
        elif feedback == "plan_change":
            conn.execute("UPDATE core_daily_decisions SET status='needs_manual_attention', needs_manual_attention=1 WHERE day_iso=?", (day_iso,))
        elif feedback in {"lighter", "harder"}:
            conn.execute("UPDATE core_daily_decisions SET status=? WHERE day_iso=?", (feedback, day_iso))
        conn.commit()
    finally:
        conn.close()
    return {"ok": True, "feedback": feedback, "day_iso": day_iso}
