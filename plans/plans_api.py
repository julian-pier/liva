# plans_api.py
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import date, datetime, timedelta
from typing import Any, Dict

from flask import Blueprint, jsonify, request

plans_api = Blueprint("plans_api", __name__)

def get_plans_db() -> sqlite3.Connection:
    conn = sqlite3.connect("./database/plans.sqlite3")
    conn.row_factory = sqlite3.Row
    return conn


def _clear_autopilot_week_cache() -> None:
    conn = sqlite3.connect("./database/training.sqlite3")
    try:
        conn.execute("DELETE FROM autopilot_week")
        conn.commit()
    except Exception:
        pass
    finally:
        conn.close()


def _uid() -> str:
    return uuid.uuid4().hex[:8]


def _safe_load_plan(row: sqlite3.Row) -> Dict[str, Any]:
    raw = row["data"]
    try:
        plan = json.loads(raw) if raw else {}
    except Exception:
        plan = {}
    if not isinstance(plan, dict):
        plan = {}
    return plan


def _normalize_plan(plan: Dict[str, Any], *, fallback_name: str, fallback_block: int) -> Dict[str, Any]:
    plan = dict(plan) if isinstance(plan, dict) else {}
    plan.setdefault("name", fallback_name)
    plan.setdefault("block_length", fallback_block)
    plan.setdefault("meta", {"goal": "", "priorities": []})
    plan.setdefault("base_week", [])
    plan.setdefault("weeks", [])

    if not isinstance(plan["base_week"], list):
        plan["base_week"] = []
    if not isinstance(plan["weeks"], list):
        plan["weeks"] = []

    for d in plan["base_week"]:
        if not isinstance(d, dict):
            continue
        d.setdefault("day", "")
        d.setdefault("session_name", "")
        d.setdefault("strength_exercises", [])
        d.setdefault("run_sessions", [])

        if not isinstance(d["strength_exercises"], list):
            d["strength_exercises"] = []
        if not isinstance(d["run_sessions"], list):
            d["run_sessions"] = []

        for ex in d["strength_exercises"]:
            if not isinstance(ex, dict):
                continue
            ex.setdefault("uid", _uid())
            ex.setdefault("exercise_name", "")
            ex.setdefault("device", "")
            ex.setdefault("variation", "")
            ex.setdefault("sets", 0)
            ex.setdefault("reps_min", 0)
            ex.setdefault("reps_max", 0)
            ex.setdefault("rpe_min", 0)
            ex.setdefault("rpe_max", 0)
            ex.setdefault("notes", "")

        for run in d["run_sessions"]:
            if not isinstance(run, dict):
                continue
            run.setdefault("uid", _uid())
            run.setdefault("run_type", "")
            run.setdefault("amount_value", "")
            run.setdefault("amount_unit", "")
            run.setdefault("pace", "")
            run.setdefault("interval_reps", "")
            run.setdefault("interval_on", "")
            run.setdefault("interval_on_unit", "min")
            run.setdefault("interval_off", "")
            run.setdefault("interval_off_unit", "min")
            run.setdefault("notes", "")

    for i, w in enumerate(plan["weeks"], start=1):
        if not isinstance(w, dict):
            continue
        w.setdefault("week", i)
        w.setdefault("strength_volume_factor", 1.0)
        w.setdefault("run_volume_factor", 1.0)
        w.setdefault("deload", False)
        w.setdefault("rpe_cap", None)
        w.setdefault("week_note", "")

    return plan


def _summarize(plan: dict) -> dict:
    base_week = plan.get("base_week") or []
    weeks = plan.get("weeks") or []
    if not isinstance(base_week, list): base_week = []
    if not isinstance(weeks, list): weeks = []

    strength_days = 0
    run_days = 0

    for d in base_week:
        if not isinstance(d, dict):
            continue
        se = d.get("strength_exercises") or []
        rs = d.get("run_sessions") or []

        if isinstance(se, list) and len(se) > 0:
            strength_days += 1
        if isinstance(rs, list) and len(rs) > 0:
            run_days += 1

    return {
        "days": len(base_week),
        "weeks": len(weeks),

        # WICHTIG: exakt diese Keys braucht das aktive Planning-Frontend.
        "strength_days": strength_days,
        "run_days": run_days,
    }


def _parse_iso_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.strptime(value.strip(), "%Y-%m-%d").date()
    except Exception:
        return None


def _plan_start_date(row: sqlite3.Row, plan: Dict[str, Any]) -> date | None:
    start_raw = row["start_date"] if "start_date" in row.keys() else None
    start = _parse_iso_date(start_raw)
    if start:
        return start
    meta = plan.get("meta") if isinstance(plan.get("meta"), dict) else {}
    return _parse_iso_date(meta.get("start_date") if isinstance(meta, dict) else None)


def _plan_total_weeks(row: sqlite3.Row | None, plan: Dict[str, Any]) -> int | None:
    meta = plan.get("meta") if isinstance(plan.get("meta"), dict) else {}
    raw = None
    if isinstance(meta, dict):
        raw = meta.get("total_weeks")
    if raw is None and row is not None:
        raw = row["total_weeks"] if "total_weeks" in row.keys() else None
    try:
        val = int(raw) if raw is not None else None
    except Exception:
        return None
    return val if val and val > 0 else None


def _plan_end_date(row: sqlite3.Row, plan: Dict[str, Any]) -> date | None:
    meta = plan.get("meta") if isinstance(plan.get("meta"), dict) else {}
    end_raw = meta.get("end_date") if isinstance(meta, dict) else None
    end_d = _parse_iso_date(end_raw)
    if end_d:
        return end_d
    if row is not None and "end_date" in row.keys():
        return _parse_iso_date(row["end_date"])
    return None


def _plan_active_for_date(row: sqlite3.Row, plan: Dict[str, Any], session_date: date) -> bool:
    start_d = _plan_start_date(row, plan)
    if not start_d:
        return False
    end_d = _plan_end_date(row, plan)
    if end_d:
        if session_date > end_d:
            return False
        return session_date >= start_d
    is_active = bool(row["is_active"]) if "is_active" in row.keys() else False
    return is_active and session_date >= start_d


def _plan_week_index_for_date(
    plan: Dict[str, Any],
    row: sqlite3.Row,
    start_d: date | None,
    session_date: date,
) -> int | None:
    if not start_d:
        return None
    meta = plan.get("meta") if isinstance(plan.get("meta"), dict) else {}
    weekday = start_d.weekday()
    week1_start = start_d if weekday == 0 else (start_d + timedelta(days=(7 - weekday)))
    if session_date < week1_start:
        week_current = 1
    else:
        week_current = ((session_date - week1_start).days // 7) + 1
    if isinstance(meta, dict) and meta.get("week_override") is not None:
        try:
            week_current = int(meta["week_override"])
        except Exception:
            pass
    total_weeks = _plan_total_weeks(row, plan)
    if total_weeks:
        week_current = max(1, min(total_weeks, week_current))
    return week_current


def _select_session_entry(plan: Dict[str, Any], session_name: str | None) -> Dict[str, Any] | None:
    target = " ".join((session_name or "").strip().lower().split())
    for day in plan.get("base_week") or []:
        if not isinstance(day, dict):
            continue
        name = " ".join(str(day.get("session_name") or "").strip().lower().split())
        if target and name == target:
            return day
    if plan.get("base_week"):
        first = plan["base_week"][0]
        return first if isinstance(first, dict) else None
    return None


def _planned_exercises_for_day(day: Dict[str, Any]) -> list[dict]:
    exercises = []
    for entry in day.get("strength_exercises") or []:
        if not isinstance(entry, dict):
            continue
        exercises.append({
            "exercise_name": entry.get("exercise_name", ""),
            "variation": entry.get("variation", ""),
            "sets": entry.get("sets") or 0,
            "device": entry.get("device", ""),
            "reps_min": entry.get("reps_min"),
            "reps_max": entry.get("reps_max"),
            "rpe_min": entry.get("rpe_min"),
            "rpe_max": entry.get("rpe_max"),
            "notes": entry.get("notes", ""),
        })
    return exercises


def _build_template_map(plan: Dict[str, Any]) -> Dict[str, Any]:
    template_map: Dict[str, Any] = {}
    for idx, day in enumerate(plan.get("base_week") or []):
        if not isinstance(day, dict):
            continue
        session_name = (day.get("session_name") or "").strip() or f"Session {idx + 1}"
        template_map[session_name] = _planned_exercises_for_day(day)
    return template_map


def resolve_active_plan_payload(conn: sqlite3.Connection, session_date: date, session_name: str | None) -> Dict[str, Any]:
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM plans WHERE is_active = 1 ORDER BY updated_at DESC, id DESC LIMIT 1"
    ).fetchone()
    if row:
        plan = _normalize_plan(
            _safe_load_plan(row),
            fallback_name=row["name"],
            fallback_block=int(row["block_length"] or 4),
        )
        if not _plan_active_for_date(row, plan, session_date):
            return {"plan_id": None}
        start_d = _plan_start_date(row, plan)
    else:
        # Fallback for contexts that intentionally use date-range selection without active flag.
        rows = conn.execute("SELECT * FROM plans ORDER BY updated_at DESC, id DESC").fetchall()
        candidates = []
        for cand in rows:
            cand_plan = _normalize_plan(
                _safe_load_plan(cand),
                fallback_name=cand["name"],
                fallback_block=int(cand["block_length"] or 4),
            )
            if not _plan_active_for_date(cand, cand_plan, session_date):
                continue
            cand_start = _plan_start_date(cand, cand_plan)
            sort_start = cand_start or date.min
            candidates.append((sort_start, cand, cand_plan, cand_start))
        if not candidates:
            return {"plan_id": None}
        _, row, plan, start_d = max(
            candidates,
            key=lambda entry: (entry[0], entry[1]["updated_at"], entry[1]["id"]),
        )

    session_entry = _select_session_entry(plan, session_name)
    exercises = _planned_exercises_for_day(session_entry) if session_entry else []
    plan_context = {
        "plan_id": row["id"],
        "plan_name": plan.get("name") or row["name"],
        "session_name": session_entry.get("session_name") if session_entry else "",
        "exercises": exercises,
    }
    meta = plan.get("meta") if isinstance(plan.get("meta"), dict) else {}
    week_index = None
    if start_d and isinstance(start_d, date):
        week_index = _plan_week_index_for_date(plan, row, start_d, session_date)

    return {
        "plan_id": row["id"],
        "plan_name": plan.get("name") or row["name"],
        "block_name": (meta.get("block_name") if isinstance(meta, dict) else "") or "",
        "week_index": week_index,
        "template_map": _build_template_map(plan),
        "planned_exercises_for_template": exercises,
        "plan_context": plan_context,
    }



@plans_api.get("/api/plans")
def list_plans():
    conn = get_plans_db()
    rows = conn.execute("""
        SELECT id, name, block_length, is_active, data, created_at, updated_at
        FROM plans
        ORDER BY is_active DESC, updated_at DESC
    """).fetchall()
    conn.close()

    out = []
    for r in rows:
        plan = _safe_load_plan(r)
        plan = _normalize_plan(plan, fallback_name=r["name"], fallback_block=int(r["block_length"] or 4))
        out.append({
            "id": r["id"],
            "name": r["name"],
            "block_length": int(r["block_length"] or plan.get("block_length") or 4),
            "is_active": int(r["is_active"] or 0),
            "meta": plan.get("meta") if isinstance(plan.get("meta"), dict) else {},
            "summary": _summarize(plan),
            "updated_at": r["updated_at"],
            "created_at": r["created_at"],
        })
    return jsonify(out)


@plans_api.get("/api/plans/active")
def get_active_plan():
    date_raw = (request.args.get("date") or "").strip()
    if not date_raw:
        return jsonify({"ok": False, "error": "missing_date"}), 400
    session_name = (request.args.get("session_name") or "").strip()
    session_date = _parse_iso_date(date_raw)
    if not session_date:
        return jsonify({"ok": False, "error": "invalid_date"}), 400

    conn = get_plans_db()
    payload = resolve_active_plan_payload(conn, session_date, session_name)
    conn.close()
    return jsonify(payload)


@plans_api.get("/api/plans/<int:plan_id>")
def get_plan(plan_id: int):
    conn = get_plans_db()
    row = conn.execute("""
        SELECT id, name, block_length, is_active, data, created_at, updated_at
        FROM plans
        WHERE id = ?
        LIMIT 1
    """, (plan_id,)).fetchone()
    conn.close()

    if not row:
        return jsonify({"ok": False, "error": "not_found"}), 404

    plan = _safe_load_plan(row)
    plan = _normalize_plan(plan, fallback_name=row["name"], fallback_block=int(row["block_length"] or 4))

    # Return plan fields top-level so /planung can read base_week/weeks directly
    payload = dict(plan)
    payload.update({
        "ok": True,
        "id": row["id"],
        "is_active": bool(row["is_active"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    })
    return jsonify(payload)


@plans_api.post("/api/plans/<int:plan_id>/set_active")
def set_active(plan_id: int):
    conn = get_plans_db()
    cur = conn.cursor()
    now = datetime.now().isoformat(timespec="seconds")

    cur.execute("SELECT id, data FROM plans WHERE id = ?", (plan_id,))
    row = cur.fetchone()
    if not row:
        conn.close()
        return jsonify({"ok": False, "error": "not_found"}), 404

    updated_data = None
    try:
        plan = json.loads(row["data"]) if row["data"] else {}
        if isinstance(plan, dict):
            meta = plan.get("meta") if isinstance(plan.get("meta"), dict) else {}
            if not meta.get("start_date"):
                meta["start_date"] = datetime.now().date().isoformat()
                plan["meta"] = meta
                updated_data = json.dumps(plan, ensure_ascii=False)
    except Exception:
        updated_data = None

    cur.execute("BEGIN")
    try:
        cur.execute("UPDATE plans SET is_active = 0")
        cur.execute("UPDATE plans SET is_active = 1, updated_at = ? WHERE id = ?", (now, plan_id))
        if updated_data:
            cur.execute("UPDATE plans SET data = ? WHERE id = ?", (updated_data, plan_id))
        conn.commit()
    except Exception as e:
        conn.rollback()
        conn.close()
        return jsonify({"ok": False, "error": "set_active_failed", "detail": str(e)}), 500

    conn.close()
    _clear_autopilot_week_cache()
    return jsonify({"ok": True, "id": plan_id})


@plans_api.delete("/api/plans/<int:plan_id>")
def delete_plan(plan_id: int):
    conn = get_plans_db()
    cur = conn.cursor()

    cur.execute("BEGIN")
    try:
        cur.execute("DELETE FROM plans WHERE id = ?", (plan_id,))
        conn.commit()
    except Exception as e:
        conn.rollback()
        conn.close()
        return jsonify({"ok": False, "error": "delete_failed", "detail": str(e)}), 500

    conn.close()
    return jsonify({"ok": True, "id": plan_id})
