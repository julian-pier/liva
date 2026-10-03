from __future__ import annotations

import copy
import json
import re
import sqlite3
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any

from database.connections import get_plans_db
from plans.endurance_semantics import execution_contract, execution_steps, summarize_steps


PHASE_TYPES = {"base", "build", "threshold", "vo2", "specific", "peak", "taper", "sharpen", "recovery", "custom"}
SESSION_TYPES = {"easy", "steady", "long", "threshold", "interval", "vo2", "5k_specific", "race", "rest", "other"}
TARGET_ZONES = {"easy", "steady", "long", "threshold", "interval", "vo2", "5k_specific", "goal_5k_pace"}
TARGET_FIELDS = {"metric", "basis", "zone", "pace_s_per_km", "pace_min_s_per_km", "pace_max_s_per_km", "hr_bpm", "hr_min_pct", "hr_max_pct", "rpe", "rpe_min", "rpe_max", "min", "max", "source"}
DESTRUCTIVE_OPS = {"delete_phase", "delete_session", "clear_fitness_anchor"}
OPERATION_FIELDS = {
    "set_goal_event": {"op", "event", "title", "distance_m", "target_time_s", "event_date", "priority"},
    "set_season_start": {"op", "start_date", "shift_sessions"},
    "add_phase": {"op", "phase", "id", "name", "phase_type", "start_date", "end_date", "sort_order", "notes"},
    "update_phase": {"op", "phase_id", "name", "phase_type", "start_date", "end_date", "notes"},
    "move_phase": {"op", "phase_id", "start_date", "end_date"},
    "resize_phase": {"op", "phase_id", "start_date", "end_date"},
    "delete_phase": {"op", "phase_id"},
    "add_session": {"op", "session"},
    "update_session": {"op", "session_id", "scheduled_date", "date", "phase_id", "title", "session_type", "sport_type", "status", "duration_s", "duration_min", "distance_m", "distance_km", "load_value", "tags", "notes"},
    "move_session": {"op", "session_id", "scheduled_date", "date"},
    "swap_sessions": {"op", "session_id", "other_session_id"},
    "duplicate_session": {"op", "session_id", "date"},
    "delete_session": {"op", "session_id"},
    "replace_steps": {"op", "session_id", "steps"},
    "move_step": {"op", "step_id", "parent_step_id", "sort_order"},
    "set_session_target": {"op", "step_id", "target"},
    "set_fitness_anchor": {"op", "anchor"},
    "clear_fitness_anchor": {"op"},
    "rebase_future_targets": {"op", "from_date"},
    "normalize_phases": {"op"},
    "derive_metrics": {"op"},
    "repair_workout_semantics": {"op"},
}


class EnduranceError(ValueError):
    pass


def _reject_unknown(payload: dict[str, Any], allowed: set[str], context: str) -> None:
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise EnduranceError(f"{context} contains unsupported fields: {', '.join(unknown)}")


def _normalize_phase(phase: dict[str, Any]) -> dict[str, Any]:
    _reject_unknown(phase, {"id", "name", "phase_type", "start_date", "end_date", "sort_order", "notes"}, "phase")
    out = dict(phase)
    aliases = {"5k_specific": "specific", "peak_taper": "taper"}
    kind = aliases.get(str(out.get("phase_type") or "custom").lower(), str(out.get("phase_type") or "custom").lower())
    if kind not in PHASE_TYPES:
        raise EnduranceError(f"invalid phase_type: {kind}; expected one of {', '.join(sorted(PHASE_TYPES))}")
    out["phase_type"] = kind
    return out


def _normalize_step(step: dict[str, Any]) -> dict[str, Any]:
    _reject_unknown(step, {"id", "kind", "step_type", "parent_step_id", "sort_order", "duration_s", "duration_min", "distance_m", "reps", "repetitions", "target", "notes", "steps", "children"}, "workout step")
    out = dict(step)
    canonical_kind, alias_kind = out.pop("kind", None), out.pop("step_type", None)
    if canonical_kind and alias_kind:
        raise EnduranceError("workout step must use either kind or step_type")
    out["kind"] = canonical_kind or alias_kind or "open"
    if "duration_min" in out:
        if out.get("duration_s") not in (None, ""):
            raise EnduranceError("workout step must use either duration_s or duration_min")
        out["duration_s"] = round(float(out.pop("duration_min")) * 60)
    if "repetitions" in out:
        if out.get("reps") not in (None, ""):
            raise EnduranceError("workout step must use either reps or repetitions")
        out["reps"] = int(out.pop("repetitions"))
    children = out.get("steps") or out.get("children") or []
    out["steps"] = [_normalize_step(item) for item in children]
    out.pop("children", None)
    target = out.get("target") or {}
    if not isinstance(target, dict):
        raise EnduranceError("workout step target must be an object")
    _reject_unknown(target, TARGET_FIELDS, "workout target")
    if target.get("zone") and target["zone"] not in TARGET_ZONES:
        raise EnduranceError(f"invalid target zone: {target['zone']}")
    return out


def _normalize_session(session: dict[str, Any]) -> dict[str, Any]:
    _reject_unknown(session, {"id", "plan_id", "phase_id", "scheduled_date", "date", "session_type", "sport_type", "title", "status", "duration_s", "duration_min", "distance_m", "distance_km", "load_value", "tags", "notes", "sync_state", "actual_run_id", "steps"}, "session")
    out = dict(session)
    scheduled_date, date_alias = out.pop("scheduled_date", None), out.pop("date", None)
    if scheduled_date and date_alias:
        raise EnduranceError("session must use either scheduled_date or date")
    out["scheduled_date"] = scheduled_date or date_alias
    session_type, sport_alias = out.pop("session_type", None), out.pop("sport_type", None)
    if session_type and sport_alias:
        raise EnduranceError("session must use either session_type or sport_type")
    raw_kind = str(session_type or sport_alias or "other").lower()
    out["session_type"] = {"intervals": "interval", "5k": "5k_specific"}.get(raw_kind, raw_kind)
    if out["session_type"] not in SESSION_TYPES:
        raise EnduranceError(f"invalid session_type: {raw_kind}; expected one of {', '.join(sorted(SESSION_TYPES | {'intervals'}))}")
    if "duration_min" in out:
        if out.get("duration_s") not in (None, ""):
            raise EnduranceError("session must use either duration_s or duration_min")
        out["duration_s"] = round(float(out.pop("duration_min")) * 60)
    if "distance_km" in out:
        if out.get("distance_m") not in (None, ""):
            raise EnduranceError("session must use either distance_m or distance_km")
        out["distance_m"] = round(float(out.pop("distance_km")) * 1000)
    out["steps"] = [_normalize_step(item) for item in out.get("steps") or []]
    _validate_session_structure(out)
    return out


def _title_repeat_spec(title: str) -> tuple[int, float, str] | None:
    match = re.search(r"(\d+)\s*[×x]\s*(\d+(?:[.,]\d+)?)\s*(km|min|sek|s|m)\b", str(title or ""), re.I)
    return (int(match.group(1)), float(match.group(2).replace(",", ".")), match.group(3).lower()) if match else None


def _nested_steps(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [child for step in steps for child in ([step] + _nested_steps(list(step.get("steps") or [])))]


def _validate_session_structure(session: dict[str, Any]) -> None:
    steps = list(session.get("steps") or [])
    flat = _nested_steps(steps)
    if _title_repeat_spec(str(session.get("title") or "")) and not any(step.get("kind") == "repeat" for step in flat):
        raise EnduranceError("repeated workout titles require a canonical repeat step")
    if "stride" in str(session.get("title") or "").lower() and not any(step.get("kind") == "stride" for step in flat):
        raise EnduranceError("stride sessions require structured stride steps")
    notes = " ".join(str(value or "") for value in ([session.get("notes")] + [step.get("notes") for step in flat])).lower()
    if "steiger" in notes and not any(step.get("kind") == "stride" for step in flat):
        raise EnduranceError("workout text announcing strides requires structured stride steps")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _loads(value: Any, fallback: Any = None) -> Any:
    if value in (None, ""):
        return copy.deepcopy(fallback)
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return copy.deepcopy(fallback)


def _date(value: Any, field: str) -> str:
    try:
        return date.fromisoformat(str(value)).isoformat()
    except (TypeError, ValueError) as exc:
        raise EnduranceError(f"{field} must be an ISO date") from exc


def ensure_endurance_schema(conn: sqlite3.Connection | None = None) -> None:
    own = conn is None
    conn = conn or get_plans_db()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS endurance_plans (
          id TEXT PRIMARY KEY, title TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active',
          revision INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS endurance_events (
          id TEXT PRIMARY KEY, plan_id TEXT NOT NULL, title TEXT, distance_m INTEGER,
          target_time_s INTEGER, event_date TEXT NOT NULL, priority TEXT NOT NULL DEFAULT 'A',
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
          FOREIGN KEY(plan_id) REFERENCES endurance_plans(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS endurance_phases (
          id TEXT PRIMARY KEY, plan_id TEXT NOT NULL, name TEXT NOT NULL, phase_type TEXT NOT NULL,
          start_date TEXT NOT NULL, end_date TEXT NOT NULL, sort_order INTEGER NOT NULL DEFAULT 0,
          notes TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
          FOREIGN KEY(plan_id) REFERENCES endurance_plans(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS endurance_weeks (
          id TEXT PRIMARY KEY, plan_id TEXT NOT NULL, phase_id TEXT, week_start TEXT NOT NULL,
          planned_load REAL, notes TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
          UNIQUE(plan_id, week_start),
          FOREIGN KEY(plan_id) REFERENCES endurance_plans(id) ON DELETE CASCADE,
          FOREIGN KEY(phase_id) REFERENCES endurance_phases(id) ON DELETE SET NULL
        );
        CREATE TABLE IF NOT EXISTS endurance_sessions (
          id TEXT PRIMARY KEY, plan_id TEXT NOT NULL, phase_id TEXT, scheduled_date TEXT NOT NULL,
          session_type TEXT NOT NULL, title TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'planned',
          duration_s INTEGER, distance_m INTEGER, load_value REAL, tags_json TEXT NOT NULL DEFAULT '[]',
          notes TEXT, sync_state TEXT NOT NULL DEFAULT 'not_synced', actual_run_id INTEGER,
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
          FOREIGN KEY(plan_id) REFERENCES endurance_plans(id) ON DELETE CASCADE,
          FOREIGN KEY(phase_id) REFERENCES endurance_phases(id) ON DELETE SET NULL
        );
        CREATE TABLE IF NOT EXISTS endurance_steps (
          id TEXT PRIMARY KEY, session_id TEXT NOT NULL, parent_step_id TEXT, sort_order INTEGER NOT NULL,
          kind TEXT NOT NULL, duration_s INTEGER, distance_m INTEGER, reps INTEGER,
          target_json TEXT NOT NULL DEFAULT '{}', notes TEXT,
          FOREIGN KEY(session_id) REFERENCES endurance_sessions(id) ON DELETE CASCADE,
          FOREIGN KEY(parent_step_id) REFERENCES endurance_steps(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS endurance_fitness_anchors (
          id TEXT PRIMARY KEY, plan_id TEXT NOT NULL, anchor_date TEXT NOT NULL, source TEXT NOT NULL,
          five_k_time_s INTEGER, threshold_pace_s_per_km INTEGER, zones_json TEXT NOT NULL DEFAULT '{}',
          notes TEXT, created_at TEXT NOT NULL,
          FOREIGN KEY(plan_id) REFERENCES endurance_plans(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS endurance_resolutions (
          id TEXT PRIMARY KEY, session_id TEXT NOT NULL, step_id TEXT NOT NULL, fitness_anchor_id TEXT,
          resolved_at TEXT NOT NULL, resolved_target_json TEXT NOT NULL,
          UNIQUE(session_id, step_id),
          FOREIGN KEY(session_id) REFERENCES endurance_sessions(id) ON DELETE CASCADE,
          FOREIGN KEY(step_id) REFERENCES endurance_steps(id) ON DELETE CASCADE,
          FOREIGN KEY(fitness_anchor_id) REFERENCES endurance_fitness_anchors(id) ON DELETE SET NULL
        );
        CREATE TABLE IF NOT EXISTS endurance_plan_revisions (
          id TEXT PRIMARY KEY, plan_id TEXT NOT NULL, revision INTEGER NOT NULL, operation TEXT NOT NULL,
          summary TEXT NOT NULL, diff_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL,
          FOREIGN KEY(plan_id) REFERENCES endurance_plans(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS endurance_external_map (
          id TEXT PRIMARY KEY, session_id TEXT NOT NULL, provider TEXT NOT NULL, external_id TEXT,
          sync_state TEXT NOT NULL DEFAULT 'not_synced', last_error TEXT, synced_at TEXT,
          UNIQUE(session_id, provider),
          FOREIGN KEY(session_id) REFERENCES endurance_sessions(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_endurance_sessions_plan_date ON endurance_sessions(plan_id, scheduled_date);
        CREATE INDEX IF NOT EXISTS idx_endurance_phases_plan_dates ON endurance_phases(plan_id, start_date, end_date);
        CREATE INDEX IF NOT EXISTS idx_endurance_revisions_plan ON endurance_plan_revisions(plan_id, revision DESC);
        """
    )
    from integrations.endurance_intervals_sync import ensure_sync_schema
    ensure_sync_schema(conn)
    conn.commit()
    if own:
        conn.close()


def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def _event_payload(row: sqlite3.Row | None) -> dict[str, Any] | None:
    item = _row(row)
    if item and item.get("distance_m") and item.get("target_time_s"):
        item["goal_pace_s_per_km"] = round(item["target_time_s"] * 1000 / item["distance_m"])
    return item


def _step_payload(row: sqlite3.Row, resolution: sqlite3.Row | None = None) -> dict[str, Any]:
    item = dict(row)
    item["target"] = _loads(item.pop("target_json", None), {})
    item["resolved"] = _loads(resolution["resolved_target_json"], None) if resolution else None
    item["fitness_anchor_id"] = resolution["fitness_anchor_id"] if resolution else None
    item["resolved_at"] = resolution["resolved_at"] if resolution else None
    return item


def _session_payload(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    item["tags"] = _loads(item.pop("tags_json", None), [])
    resolutions = {r["step_id"]: r for r in conn.execute("SELECT * FROM endurance_resolutions WHERE session_id=?", (item["id"],))}
    steps = [_step_payload(step, resolutions.get(step["id"])) for step in conn.execute("SELECT * FROM endurance_steps WHERE session_id=? ORDER BY sort_order,id", (item["id"],))]
    item["steps"] = steps
    item["execution_steps"] = execution_steps(steps)
    item["execution_contract"] = execution_contract(steps)
    mapping = conn.execute("SELECT external_id,external_event_id,sync_state,last_synced_at,last_attempt_at,last_error FROM endurance_external_map WHERE session_id=? AND provider='intervals'", (item["id"],)).fetchone()
    if mapping:
        item["intervals_sync"] = dict(mapping)
    return item


def _withhold_low_confidence_execution_paces(sessions: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not any(
        isinstance(step.get("resolved"), dict) and step["resolved"].get("source") == "current_hr_pace_model"
        for session in sessions for step in session.get("steps") or []
    ):
        return None
    try:
        from analysis.run_pace_model import execution_model_quality, load_current_hr_pace_model

        model = load_current_hr_pace_model()
        quality = execution_model_quality(model) if model else {"execution_ready": False, "confidence": "low", "reasons": ["no_model"]}
    except Exception:
        quality = {"execution_ready": False, "confidence": "low", "reasons": ["model_unavailable"]}
    if quality.get("execution_ready"):
        return quality
    for session in sessions:
        withheld = False
        for collection in (session.get("steps") or [], session.get("execution_steps") or []):
            for step in collection:
                if isinstance(step.get("resolved"), dict) and step["resolved"].get("source") == "current_hr_pace_model":
                    step["resolved"] = None
                    step["execution_guidance_status"] = "withheld_low_confidence"
                    withheld = True
        if withheld:
            session["execution_guidance_status"] = "benchmark_required"
    return quality


def list_plans() -> list[dict[str, Any]]:
    ensure_endurance_schema()
    conn = get_plans_db()
    rows = conn.execute("SELECT * FROM endurance_plans WHERE status!='archived' ORDER BY updated_at DESC").fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["event"] = _event_payload(conn.execute("SELECT * FROM endurance_events WHERE plan_id=? ORDER BY event_date LIMIT 1", (row["id"],)).fetchone())
        result.append(item)
    conn.close()
    return result


def get_next_open_cardio_session(day_iso: str | None = None) -> dict[str, Any]:
    """Small local-only dashboard read model for the next planned run."""
    ensure_endurance_schema()
    requested_day = _date(day_iso or date.today().isoformat(), "day")
    conn = get_plans_db()
    try:
        plan = conn.execute(
            "SELECT * FROM endurance_plans WHERE status='active' ORDER BY updated_at DESC,id DESC LIMIT 1"
        ).fetchone()
        if not plan:
            return {"ok": True, "available": False, "source": "local_endurance_plan", "message": "Noch kein aktiver Cardio-Plan."}
        session = conn.execute(
            """
            SELECT * FROM endurance_sessions
            WHERE plan_id=? AND scheduled_date>=? AND actual_run_id IS NULL
              AND status IN ('planned','scheduled')
            ORDER BY scheduled_date,id LIMIT 1
            """,
            (plan["id"], requested_day),
        ).fetchone()
        if not session:
            return {
                "ok": True, "available": True, "has_workout": False,
                "source": "local_endurance_plan", "day": requested_day,
                "plan": {"id": plan["id"], "title": plan["title"]},
                "message": "Kein offener Lauf mehr geplant.",
            }
        payload = _session_payload(conn, session)
        phase = conn.execute("SELECT id,name,phase_type FROM endurance_phases WHERE id=?", (session["phase_id"],)).fetchone() if session["phase_id"] else None
        mapping = payload.get("intervals_sync") or {}
        sync_state = str(mapping.get("sync_state") or payload.get("sync_state") or "not_synced")
        intervals_synced = sync_state == "synced"
        return {
            "ok": True, "available": True, "has_workout": True,
            "source": "local_endurance_plan", "day": requested_day,
            "plan": {"id": plan["id"], "title": plan["title"]},
            "phase": dict(phase) if phase else None,
            "session": payload,
            "sync": {
                "state": sync_state,
                "intervals_synced": intervals_synced,
                # Local state can prove the Intervals readback, not delivery to
                # a physical watch. Make that boundary explicit in the API.
                "garmin_state": "via_intervals" if intervals_synced else "waiting",
                "label": "Intervals synchronisiert · Garmin über Intervals" if intervals_synced else ("Syncfehler" if sync_state == "sync_error" else "Noch nicht synchronisiert"),
                "last_synced_at": mapping.get("last_synced_at"),
                "error": mapping.get("last_error"),
            },
        }
    finally:
        conn.close()


def create_plan(payload: dict[str, Any]) -> dict[str, Any]:
    ensure_endurance_schema()
    _reject_unknown(payload, {"title", "event", "phases", "sessions"}, "plan")
    event = payload.get("event") if isinstance(payload.get("event"), dict) else {}
    _reject_unknown(event, {"id", "title", "distance_m", "target_time_s", "event_date", "priority"}, "goal event")
    event_date = _date(event.get("event_date"), "event.event_date")
    distance_m = int(event["distance_m"]) if event.get("distance_m") not in (None, "") else None
    target_time_s = int(event["target_time_s"]) if event.get("target_time_s") not in (None, "") else None
    if distance_m is not None and distance_m <= 0:
        raise EnduranceError("distance_m must be positive")
    if target_time_s is not None and target_time_s <= 0:
        raise EnduranceError("target_time_s must be positive")
    now, plan_id, event_id = _now(), _id("ep"), _id("ee")
    title = str(payload.get("title") or event.get("title") or "Cardio-Plan").strip()[:160]
    conn = get_plans_db()
    try:
        with conn:
            conn.execute("INSERT INTO endurance_plans(id,title,created_at,updated_at) VALUES(?,?,?,?)", (plan_id, title, now, now))
            conn.execute("INSERT INTO endurance_events(id,plan_id,title,distance_m,target_time_s,event_date,priority,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)", (event_id, plan_id, event.get("title"), distance_m, target_time_s, event_date, str(event.get("priority") or "A")[:8], now, now))
            for index, phase in enumerate(payload.get("phases") or []):
                _insert_phase(conn, plan_id, _normalize_phase(phase), index)
            for session in payload.get("sessions") or []:
                _insert_session(conn, plan_id, _normalize_session(session))
            _normalize_phase_chain(conn, plan_id)
            _derive_plan_metrics(conn, plan_id, force_paces=True)
            from integrations.endurance_intervals_sync import queue_plan_sync_changes
            queue_plan_sync_changes(conn, plan_id)
            _record_revision(conn, plan_id, "create_plan", "Cardio-Plan erstellt", {"event_id": event_id, "created_plan": True})
    finally:
        conn.close()
    return get_plan(plan_id)


def _insert_phase(conn: sqlite3.Connection, plan_id: str, phase: dict[str, Any], order: int = 0) -> str:
    phase = _normalize_phase(phase)
    phase_id, now = str(phase.get("id") or _id("eph")), _now()
    start, end = _date(phase.get("start_date"), "start_date"), _date(phase.get("end_date"), "end_date")
    if end < start:
        raise EnduranceError("phase end_date must not precede start_date")
    kind = str(phase.get("phase_type") or "custom").lower()
    requested_order = int(phase.get("sort_order", order))
    if conn.execute("SELECT 1 FROM endurance_phases WHERE plan_id=? AND sort_order=?", (plan_id, requested_order)).fetchone():
        requested_order = int(conn.execute("SELECT COALESCE(MAX(sort_order),-1)+1 FROM endurance_phases WHERE plan_id=?", (plan_id,)).fetchone()[0])
    conn.execute("INSERT INTO endurance_phases VALUES(?,?,?,?,?,?,?,?,?,?)", (phase_id, plan_id, str(phase.get("name") or kind.title())[:100], kind, start, end, requested_order, phase.get("notes"), now, now))
    return phase_id


def _insert_steps(conn: sqlite3.Connection, session_id: str, steps: list[dict[str, Any]], parent_id: str | None = None) -> None:
    for index, step in enumerate(steps):
        step = _normalize_step(step)
        step_id = str(step.get("id") or _id("est"))
        conn.execute("INSERT INTO endurance_steps(id,session_id,parent_step_id,sort_order,kind,duration_s,distance_m,reps,target_json,notes) VALUES(?,?,?,?,?,?,?,?,?,?)", (step_id, session_id, parent_id, int(step.get("sort_order", index)), str(step.get("kind") or "open")[:32], step.get("duration_s"), step.get("distance_m"), step.get("reps"), _json(step.get("target") or {}), step.get("notes")))
        children = step.get("steps") or step.get("children") or []
        if children:
            _insert_steps(conn, session_id, children, step_id)


def _insert_session(conn: sqlite3.Connection, plan_id: str, session: dict[str, Any]) -> str:
    session = _normalize_session(session)
    session_id, now = str(session.get("id") or _id("es")), _now()
    kind = str(session.get("session_type") or "other").lower()
    conn.execute("INSERT INTO endurance_sessions(id,plan_id,phase_id,scheduled_date,session_type,title,status,duration_s,distance_m,load_value,tags_json,notes,sync_state,actual_run_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (session_id, plan_id, session.get("phase_id"), _date(session.get("scheduled_date"), "scheduled_date"), kind, str(session.get("title") or kind.upper())[:140], str(session.get("status") or "planned"), session.get("duration_s"), session.get("distance_m"), session.get("load_value"), _json(session.get("tags") or []), session.get("notes"), str(session.get("sync_state") or "not_synced"), session.get("actual_run_id"), now, now))
    _insert_steps(conn, session_id, list(session.get("steps") or []))
    return session_id


def _phase_chain(conn: sqlite3.Connection, plan_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM endurance_phases WHERE plan_id=? ORDER BY sort_order,start_date,id",
        (plan_id,),
    ).fetchall()


def _allocate_phase_days(total_days: int, weights: list[int]) -> list[int]:
    if not weights:
        return []
    if total_days < len(weights):
        raise EnduranceError("season is too short for the number of phases")
    remaining = total_days - len(weights)
    weight_sum = max(1, sum(max(1, value) for value in weights))
    raw = [remaining * max(1, value) / weight_sum for value in weights]
    allocated = [1 + int(value) for value in raw]
    missing = total_days - sum(allocated)
    order = sorted(range(len(raw)), key=lambda index: (raw[index] - int(raw[index]), -index), reverse=True)
    for index in order[:missing]:
        allocated[index] += 1
    return allocated


def _normalize_phase_chain(conn: sqlite3.Connection, plan_id: str) -> None:
    """Make phases a continuous, ordered chain from season start through race day."""
    phases = _phase_chain(conn, plan_id)
    if not phases:
        return
    event = conn.execute(
        "SELECT event_date FROM endurance_events WHERE plan_id=? ORDER BY event_date LIMIT 1",
        (plan_id,),
    ).fetchone()
    season_start = date.fromisoformat(phases[0]["start_date"])
    season_end = date.fromisoformat(event["event_date"] if event else phases[-1]["end_date"])
    total_days = (season_end - season_start).days + 1
    weights = [
        max(1, (date.fromisoformat(phase["end_date"]) - date.fromisoformat(phase["start_date"])).days + 1)
        for phase in phases
    ]
    lengths = _allocate_phase_days(total_days, weights)
    cursor = season_start
    normalized: list[tuple[str, str, str]] = []
    for index, (phase, length) in enumerate(zip(phases, lengths)):
        phase_end = cursor + timedelta(days=length - 1)
        normalized.append((phase["id"], cursor.isoformat(), phase_end.isoformat()))
        conn.execute(
            "UPDATE endurance_phases SET start_date=?,end_date=?,sort_order=?,updated_at=? WHERE id=?",
            (cursor.isoformat(), phase_end.isoformat(), index, _now(), phase["id"]),
        )
        cursor = phase_end + timedelta(days=1)

    sessions = conn.execute(
        "SELECT id,scheduled_date,phase_id FROM endurance_sessions WHERE plan_id=?",
        (plan_id,),
    ).fetchall()
    for session in sessions:
        matching = next(
            (phase_id for phase_id, start, end in normalized if start <= session["scheduled_date"] <= end),
            None,
        )
        if matching and matching != session["phase_id"]:
            conn.execute(
                "UPDATE endurance_sessions SET phase_id=?,updated_at=? WHERE id=?",
                (matching, _now(), session["id"]),
            )


def _smart_resize_phase(conn: sqlite3.Connection, plan_id: str, row: sqlite3.Row, op: dict[str, Any]) -> None:
    phases = _phase_chain(conn, plan_id)
    index = next(i for i, phase in enumerate(phases) if phase["id"] == row["id"])
    start = date.fromisoformat(row["start_date"])
    end = date.fromisoformat(row["end_date"])
    requested_start = date.fromisoformat(_date(op.get("start_date", row["start_date"]), "start_date"))
    requested_end = date.fromisoformat(_date(op.get("end_date", row["end_date"]), "end_date"))
    if requested_start != start:
        if index == 0:
            raise EnduranceError("first phase start is fixed to the season start")
        previous = phases[index - 1]
        minimum = date.fromisoformat(previous["start_date"]) + timedelta(days=1)
        requested_start = min(end, max(minimum, requested_start))
        conn.execute("UPDATE endurance_phases SET end_date=?,updated_at=? WHERE id=?", ((requested_start - timedelta(days=1)).isoformat(), _now(), previous["id"]))
        start = requested_start
    if requested_end != end:
        if index == len(phases) - 1:
            raise EnduranceError("last phase end is fixed to the goal date")
        following = phases[index + 1]
        maximum = date.fromisoformat(following["end_date"]) - timedelta(days=1)
        requested_end = max(start, min(maximum, requested_end))
        conn.execute("UPDATE endurance_phases SET start_date=?,updated_at=? WHERE id=?", ((requested_end + timedelta(days=1)).isoformat(), _now(), following["id"]))
        end = requested_end
    conn.execute(
        "UPDATE endurance_phases SET start_date=?,end_date=?,updated_at=? WHERE id=?",
        (start.isoformat(), end.isoformat(), _now(), row["id"]),
    )


def _smart_move_phase(conn: sqlite3.Connection, plan_id: str, row: sqlite3.Row, op: dict[str, Any]) -> int:
    phases = _phase_chain(conn, plan_id)
    index = next(i for i, phase in enumerate(phases) if phase["id"] == row["id"])
    if index == 0 or index == len(phases) - 1:
        raise EnduranceError("only inner phases can move; outer edges are fixed to season start and goal")
    old_start = date.fromisoformat(row["start_date"])
    old_end = date.fromisoformat(row["end_date"])
    duration = (old_end - old_start).days
    requested_start = date.fromisoformat(_date(op.get("start_date", row["start_date"]), "start_date"))
    previous, following = phases[index - 1], phases[index + 1]
    minimum = date.fromisoformat(previous["start_date"]) + timedelta(days=1)
    maximum = date.fromisoformat(following["end_date"]) - timedelta(days=duration + 1)
    new_start = min(maximum, max(minimum, requested_start))
    new_end = new_start + timedelta(days=duration)
    conn.execute("UPDATE endurance_phases SET end_date=?,updated_at=? WHERE id=?", ((new_start - timedelta(days=1)).isoformat(), _now(), previous["id"]))
    conn.execute("UPDATE endurance_phases SET start_date=?,end_date=?,updated_at=? WHERE id=?", (new_start.isoformat(), new_end.isoformat(), _now(), row["id"]))
    conn.execute("UPDATE endurance_phases SET start_date=?,updated_at=? WHERE id=?", ((new_end + timedelta(days=1)).isoformat(), _now(), following["id"]))
    return (new_start - old_start).days


def _gym_context(conn: sqlite3.Connection, start_date: str | None = None, end_date: str | None = None) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {}
    try:
        row = conn.execute("SELECT id,title,plan_json FROM gym_plans WHERE is_active=1 AND COALESCE(is_archived,0)=0 ORDER BY updated_at DESC LIMIT 1").fetchone()
        plan = _loads(row["plan_json"], {}) if row else {}
        meta = plan.get("meta") if isinstance(plan.get("meta"), dict) else {}
        if str(meta.get("mode") or "") == "rolling_sequence" and plan.get("sequence"):
            # Use the same real-workout-anchor resolver as today's canonical
            # training decision. A calendar-local counter drifts as soon as a
            # workout is skipped, moved or completed outside the nominal week.
            try:
                from ai.actions_v2 import _plan_session_for_date

                first = date.fromisoformat(start_date) if start_date else date.today()
                last = date.fromisoformat(end_date) if end_date else first + timedelta(days=42)
                anchor_resolution = _plan_session_for_date(first.isoformat())
                start_index = int(anchor_resolution.get("rotation_index") or 0) if isinstance(anchor_resolution, dict) else 0
                pattern = meta.get("rolling_week_pattern") or {}
                sequence = list(plan.get("sequence") or [])
                cursor = first
                training_index = start_index
                while cursor <= last and (cursor - first).days <= 400:
                    weekday = ("Mo", "Di", "Mi", "Do", "Fr", "Sa", "So")[cursor.weekday()]
                    if str(pattern.get(weekday) or "").strip().casefold() in {"train", "training", "gym"}:
                        item = sequence[training_index % len(sequence)]
                        result.setdefault(cursor.isoformat(), []).append({
                            "title": item.get("title") or row["title"] or "Gym",
                            "read_only": True,
                            "source": "actions_v2.canonical_rolling_resolver",
                            "gym_plan_id": row["id"],
                            "session_key": item.get("id"),
                        })
                        training_index += 1
                    cursor += timedelta(days=1)
                return result
            except Exception:
                pass
            pattern = meta.get("rolling_week_pattern") or {}
            anchor = date.fromisoformat(str(meta.get("start_date") or start_date or date.today().isoformat()))
            first = date.fromisoformat(start_date) if start_date else anchor
            last = date.fromisoformat(end_date) if end_date else first + timedelta(days=42)
            first = max(first, anchor)
            cursor, training_index = anchor, 0
            while cursor < first:
                if str(pattern.get(("Mo","Di","Mi","Do","Fr","Sa","So")[cursor.weekday()]) or "").lower() == "train":
                    training_index += 1
                cursor += timedelta(days=1)
            while cursor <= last and (cursor - first).days <= 400:
                weekday = ("Mo","Di","Mi","Do","Fr","Sa","So")[cursor.weekday()]
                if str(pattern.get(weekday) or "").lower() == "train":
                    item = plan["sequence"][training_index % len(plan["sequence"])]
                    result.setdefault(cursor.isoformat(), []).append({"title": item.get("title") or row["title"] or "Gym", "read_only": True, "source": "active_gym_plan", "gym_plan_id": row["id"]})
                    training_index += 1
                cursor += timedelta(days=1)
        else:
            for day in plan.get("base_week") or plan.get("days") or []:
                if day.get("strength_exercises") or day.get("items"):
                    result.setdefault(str(day.get("day") or day.get("weekday") or ""), []).append({"title": day.get("session_name") or day.get("title") or row["title"] or "Gym", "read_only": True, "source": "active_gym_plan", "gym_plan_id": row["id"]})
    except sqlite3.Error:
        pass
    return result


def validate_plan(phases: list[dict[str, Any]], sessions: list[dict[str, Any]], event: dict[str, Any] | None) -> list[dict[str, Any]]:
    warnings: list[dict[str, Any]] = []
    ordered = sorted(phases, key=lambda p: (p["start_date"], p.get("sort_order", 0)))
    for previous, current in zip(ordered, ordered[1:]):
        prev_end, cur_start = date.fromisoformat(previous["end_date"]), date.fromisoformat(current["start_date"])
        if cur_start <= prev_end:
            warnings.append({"code": "phase_overlap", "severity": "warning", "message": f"{previous['name']} und {current['name']} überlappen sich.", "ids": [previous["id"], current["id"]]})
        elif cur_start > prev_end + timedelta(days=1):
            warnings.append({"code": "phase_gap", "severity": "warning", "message": "Zwischen zwei Phasen besteht eine Lücke.", "ids": [previous["id"], current["id"]]})
    quality = {"threshold", "interval", "vo2", "5k_specific", "race"}
    ordered_sessions = sorted(sessions, key=lambda s: s["scheduled_date"])
    for previous, current in zip(ordered_sessions, ordered_sessions[1:]):
        gap = (date.fromisoformat(current["scheduled_date"]) - date.fromisoformat(previous["scheduled_date"])).days
        if gap <= 2 and previous["session_type"] in quality and current["session_type"] in quality:
            warnings.append({"code": "quality_spacing", "severity": "warning", "message": f"Zwei harte Laufeinheiten liegen nur {gap * 24} h auseinander.", "ids": [previous["id"], current["id"]]})
        if gap <= 1 and current["session_type"] == "long" and previous["session_type"] in quality:
            warnings.append({"code": "long_after_quality", "severity": "warning", "message": "Long Run folgt direkt auf eine harte Einheit.", "ids": [previous["id"], current["id"]]})
    if event:
        for session in sessions:
            if session["scheduled_date"] > event["event_date"]:
                warnings.append({"code": "after_goal", "severity": "warning", "message": "Eine Einheit liegt nach dem Zieltermin.", "ids": [session["id"]]})
    counts: dict[str, int] = {}
    for session in sessions:
        counts[session["scheduled_date"]] = counts.get(session["scheduled_date"], 0) + 1
    for day, count in counts.items():
        if count > 1:
            warnings.append({"code": "same_day", "severity": "warning", "message": f"{count} Cardio-Einheiten am {day}.", "ids": []})
    phase_by_id = {phase["id"]: phase for phase in phases}
    for session in sessions:
        phase = phase_by_id.get(session.get("phase_id"))
        if phase and not (phase["start_date"] <= session["scheduled_date"] <= phase["end_date"]):
            warnings.append({"code": "outside_phase", "severity": "warning", "message": f"{session['title']} liegt außerhalb der zugeordneten Phase.", "ids": [session["id"], phase["id"]]})
        if session.get("duration_s") and session.get("steps"):
            summary = summarize_steps(session["steps"])
            resolved_duration = round(float(summary["duration_s"])) if summary["duration_complete"] else None
            if resolved_duration is not None and abs(int(session["duration_s"]) - resolved_duration) > 60:
                warnings.append({"code": "duration_mismatch", "severity": "warning", "message": f"{session['title']}: Sessiondauer und Workout-Steps unterscheiden sich um {abs(int(session['duration_s']) - resolved_duration) // 60} min.", "ids": [session["id"]]})
    return warnings


def _database_snapshot(conn: sqlite3.Connection, plan_id: str) -> dict[str, Any]:
    plan = conn.execute("SELECT * FROM endurance_plans WHERE id=?", (plan_id,)).fetchone()
    if not plan:
        raise EnduranceError("endurance plan not found")
    direct_tables = ("endurance_events", "endurance_phases", "endurance_weeks", "endurance_sessions", "endurance_fitness_anchors")
    snapshot: dict[str, Any] = {"plan": dict(plan)}
    for table in direct_tables:
        snapshot[table] = [dict(row) for row in conn.execute(f"SELECT * FROM {table} WHERE plan_id=?", (plan_id,))]
    snapshot["endurance_steps"] = [dict(row) for row in conn.execute(
        "SELECT st.* FROM endurance_steps st JOIN endurance_sessions s ON s.id=st.session_id WHERE s.plan_id=?",
        (plan_id,),
    )]
    snapshot["endurance_resolutions"] = [dict(row) for row in conn.execute(
        "SELECT r.* FROM endurance_resolutions r JOIN endurance_sessions s ON s.id=r.session_id WHERE s.plan_id=?",
        (plan_id,),
    )]
    snapshot["endurance_external_map"] = [dict(row) for row in conn.execute(
        "SELECT m.* FROM endurance_external_map m JOIN endurance_sessions s ON s.id=m.session_id WHERE s.plan_id=?",
        (plan_id,),
    )]
    return snapshot


def _insert_snapshot_row(conn: sqlite3.Connection, table: str, row: dict[str, Any]) -> None:
    columns = list(row)
    placeholders = ",".join("?" for _ in columns)
    conn.execute(f"INSERT INTO {table}({','.join(columns)}) VALUES({placeholders})", tuple(row[column] for column in columns))


def _restore_database_snapshot(conn: sqlite3.Connection, plan_id: str, snapshot: dict[str, Any]) -> None:
    current_session_ids = [row[0] for row in conn.execute("SELECT id FROM endurance_sessions WHERE plan_id=?", (plan_id,))]
    for session_id in current_session_ids:
        conn.execute("DELETE FROM endurance_resolutions WHERE session_id=?", (session_id,))
        conn.execute("DELETE FROM endurance_external_map WHERE session_id=?", (session_id,))
        conn.execute("DELETE FROM endurance_steps WHERE session_id=?", (session_id,))
    for table in ("endurance_sessions", "endurance_weeks", "endurance_fitness_anchors", "endurance_phases", "endurance_events"):
        conn.execute(f"DELETE FROM {table} WHERE plan_id=?", (plan_id,))

    plan = dict(snapshot.get("plan") or {})
    editable = [column for column in plan if column not in {"id", "revision"}]
    if editable:
        conn.execute(
            f"UPDATE endurance_plans SET {','.join(column + '=?' for column in editable)} WHERE id=?",
            (*[plan[column] for column in editable], plan_id),
        )
    for table in ("endurance_events", "endurance_phases", "endurance_fitness_anchors", "endurance_weeks", "endurance_sessions"):
        for row in snapshot.get(table) or []:
            _insert_snapshot_row(conn, table, row)

    pending = [dict(row) for row in snapshot.get("endurance_steps") or []]
    inserted: set[str] = set()
    while pending:
        ready = [row for row in pending if not row.get("parent_step_id") or row["parent_step_id"] in inserted]
        if not ready:
            raise EnduranceError("undo snapshot contains an invalid workout-step hierarchy")
        for row in ready:
            _insert_snapshot_row(conn, "endurance_steps", row)
            inserted.add(row["id"])
            pending.remove(row)
    for table in ("endurance_resolutions", "endurance_external_map"):
        for row in snapshot.get(table) or []:
            _insert_snapshot_row(conn, table, row)


def _history_state(conn: sqlite3.Connection, plan_id: str) -> tuple[set[int], list[tuple[sqlite3.Row, dict[str, Any]]]]:
    undone: set[int] = set()
    redo_stack: list[tuple[sqlite3.Row, dict[str, Any]]] = []
    rows = conn.execute("SELECT * FROM endurance_plan_revisions WHERE plan_id=? ORDER BY revision", (plan_id,)).fetchall()
    for row in rows:
        diff = _loads(row["diff_json"], {})
        if row["operation"] in {"create_plan", "patch_endurance_plan"}:
            redo_stack.clear()
        elif row["operation"] == "undo" and diff.get("target_revision") is not None:
            undone.add(int(diff["target_revision"]))
            redo_stack.append((row, diff))
        elif row["operation"] == "redo" and diff.get("target_undo_revision") is not None:
            target_undo_revision = int(diff["target_undo_revision"])
            for index in range(len(redo_stack) - 1, -1, -1):
                undo_row, undo_diff = redo_stack[index]
                if int(undo_row["revision"]) == target_undo_revision:
                    undone.discard(int(undo_diff["target_revision"]))
                    redo_stack.pop(index)
                    break
    return undone, redo_stack


def _undo_candidate(conn: sqlite3.Connection, plan_id: str) -> tuple[sqlite3.Row, dict[str, Any]] | None:
    undone, _ = _history_state(conn, plan_id)
    rows = conn.execute("SELECT * FROM endurance_plan_revisions WHERE plan_id=? ORDER BY revision DESC", (plan_id,)).fetchall()
    for row in rows:
        diff = _loads(row["diff_json"], {})
        if row["operation"] == "create_plan" and int(row["revision"]) not in undone and diff.get("created_plan"):
            return row, diff
        if row["operation"] == "patch_endurance_plan" and int(row["revision"]) not in undone and diff.get("database_before"):
            return row, diff
    return None


def _redo_candidate(conn: sqlite3.Connection, plan_id: str) -> tuple[sqlite3.Row, dict[str, Any]] | None:
    _, redo_stack = _history_state(conn, plan_id)
    return redo_stack[-1] if redo_stack else None


def get_plan(plan_id: str, date_from: str | None = None, date_to: str | None = None) -> dict[str, Any]:
    ensure_endurance_schema()
    conn = get_plans_db()
    plan_row = conn.execute("SELECT * FROM endurance_plans WHERE id=?", (plan_id,)).fetchone()
    if not plan_row:
        conn.close()
        raise EnduranceError("endurance plan not found")
    event = _event_payload(conn.execute("SELECT * FROM endurance_events WHERE plan_id=? ORDER BY event_date LIMIT 1", (plan_id,)).fetchone())
    phases = [dict(row) for row in conn.execute("SELECT * FROM endurance_phases WHERE plan_id=? ORDER BY start_date,sort_order", (plan_id,))]
    if len({phase["sort_order"] for phase in phases}) != len(phases):
        for index, phase in enumerate(phases):
            phase["sort_order"] = index
            phase["sort_order_source"] = "normalized_from_dates"
    query, args = "SELECT * FROM endurance_sessions WHERE plan_id=?", [plan_id]
    if date_from:
        query, args = query + " AND scheduled_date>=?", args + [_date(date_from, "date_from")]
    if date_to:
        query, args = query + " AND scheduled_date<=?", args + [_date(date_to, "date_to")]
    rows = conn.execute(query + " ORDER BY scheduled_date,id", args).fetchall()
    sessions = [_session_payload(conn, row) for row in rows]
    pace_model = _withhold_low_confidence_execution_paces(sessions)
    anchor_row = conn.execute("SELECT * FROM endurance_fitness_anchors WHERE plan_id=? ORDER BY anchor_date DESC,created_at DESC LIMIT 1", (plan_id,)).fetchone()
    anchor = dict(anchor_row) if anchor_row else None
    if anchor:
        anchor["zones"] = _loads(anchor.pop("zones_json"), {})
    revisions = [dict(row) for row in conn.execute("SELECT * FROM endurance_plan_revisions WHERE plan_id=? ORDER BY revision DESC LIMIT 12", (plan_id,))]
    for revision in revisions:
        revision["diff"] = _loads(revision.pop("diff_json"), {})
        had_snapshot = bool(revision["diff"].pop("database_before", None))
        revision["undoable"] = revision["operation"] == "patch_endurance_plan" and had_snapshot
    weeks: dict[str, dict[str, Any]] = {}
    for session in sessions:
        monday = (date.fromisoformat(session["scheduled_date"]) - timedelta(days=date.fromisoformat(session["scheduled_date"]).weekday())).isoformat()
        bucket = weeks.setdefault(monday, {"week_start": monday, "distance_m": 0, "duration_s": 0, "load": 0})
        bucket["distance_m"] += session.get("distance_m") or 0
        bucket["duration_s"] += session.get("duration_s") or 0
        bucket["load"] += session.get("load_value") or 0
    context_start = date_from or (phases[0]["start_date"] if phases else (sessions[0]["scheduled_date"] if sessions else None))
    context_end = date_to or (event["event_date"] if event else (phases[-1]["end_date"] if phases else context_start))
    from integrations.endurance_intervals_sync import sync_summary
    result = {**dict(plan_row), "event": event, "phases": phases, "sessions": sessions, "fitness_anchor": anchor, "pace_model": pace_model, "load_summary": list(weeks.values()), "warnings": validate_plan(phases, sessions, event), "recent_changes": revisions, "undo_available": _undo_candidate(conn, plan_id) is not None, "redo_available": _redo_candidate(conn, plan_id) is not None, "gym_context": _gym_context(conn, context_start, context_end), "sync": sync_summary(conn, plan_id)}
    conn.close()
    return result


def _resolve_target(target: dict[str, Any], anchor: dict[str, Any] | None, event: dict[str, Any] | None) -> dict[str, Any] | None:
    if not target or target.get("basis") == "open" or target.get("metric") == "open":
        return None
    if target.get("basis") == "absolute":
        return copy.deepcopy(target)
    metric, zone = target.get("metric", "pace"), str(target.get("zone") or "")
    if metric == "pace" and zone == "goal_5k_pace" and event and event.get("goal_pace_s_per_km"):
        return {"metric": "pace", "pace_s_per_km": event["goal_pace_s_per_km"], "source": "goal_event"}
    if not anchor:
        return None
    zones = anchor.get("zones") or _loads(anchor.get("zones_json"), {}) or {}
    zone_value = zones.get(zone) if isinstance(zones, dict) else None
    if isinstance(zone_value, (int, float)):
        key = "pace_s_per_km" if metric == "pace" else ("hr_bpm" if metric == "hr" else "rpe")
        return {"metric": metric, key: zone_value, "source": "fitness_anchor", "zone": zone}
    if isinstance(zone_value, dict) and zone_value:
        return {"metric": metric, **zone_value, "source": "fitness_anchor", "zone": zone}
    if metric == "pace" and zone == "threshold" and anchor.get("threshold_pace_s_per_km"):
        return {"metric": "pace", "pace_s_per_km": anchor["threshold_pace_s_per_km"], "source": "fitness_anchor", "zone": zone}
    if metric == "pace" and zone in {"interval", "vo2", "5k_specific"} and anchor.get("five_k_time_s"):
        return {"metric": "pace", "pace_s_per_km": round(anchor["five_k_time_s"] / 5), "source": "fitness_anchor", "zone": zone}
    return None


def _goal_model_pace(session_type: str, step_kind: str, goal_pace: int, progress: float, title: str = "") -> int | None:
    title_lower = title.lower()
    if step_kind in {"warmup", "cooldown"} or step_kind == "open":
        return goal_pace + 110
    if step_kind == "recovery":
        return goal_pace + 125
    if "berg" in title_lower and session_type == "interval":
        return None
    if session_type == "easy":
        return goal_pace + 95
    if session_type == "long":
        return goal_pace + 105
    if session_type in {"steady"}:
        return goal_pace + 55
    if session_type == "threshold":
        return goal_pace + max(14, round(31 - 18 * progress))
    if session_type == "interval":
        if "fartlek" in title_lower:
            return goal_pace + max(8, round(20 - 14 * progress))
        return goal_pace + max(2, round(14 - 16 * progress))
    if session_type == "vo2":
        if "speed economy" in title_lower or "×1 min" in title_lower:
            return goal_pace - 5
        return goal_pace + round(9 - 18 * progress)
    if session_type == "5k_specific":
        return goal_pace + max(0, round(12 - 14 * progress))
    if session_type == "race" and "abi" in title_lower:
        return goal_pace
    return None


def _goal_model_hr_percent(session_type: str, step_kind: str) -> tuple[int, int]:
    if step_kind in {"warmup", "cooldown", "open"}:
        return 62, 75
    if step_kind == "recovery":
        return 60, 72
    return {
        "easy": (65, 76),
        "long": (66, 78),
        "steady": (75, 84),
        "threshold": (85, 91),
        "interval": (88, 95),
        "vo2": (91, 97),
        "5k_specific": (90, 96),
        "race": (92, 100),
    }.get(session_type, (70, 85))


def _default_rpe(session_type: str, step_kind: str) -> float:
    if step_kind in {"warmup", "cooldown", "open"}:
        return 3.0
    if step_kind == "recovery":
        return 2.0
    return {"easy": 3.5, "long": 4.0, "steady": 5.0, "threshold": 7.5, "interval": 8.0, "vo2": 8.5, "5k_specific": 8.5, "race": 9.5}.get(session_type, 5.0)


def _repair_workout_semantics(conn: sqlite3.Connection, plan_id: str) -> dict[str, int]:
    """One canonical repair path for legacy aggregate intervals and note-only strides."""
    repaired_repeats = repaired_strides = repaired_warmup_strides = repaired_benchmarks = repaired_targets = 0
    event = _event_payload(conn.execute("SELECT * FROM endurance_events WHERE plan_id=? LIMIT 1", (plan_id,)).fetchone()) or {}
    has_anchor = conn.execute("SELECT 1 FROM endurance_fitness_anchors WHERE plan_id=? LIMIT 1", (plan_id,)).fetchone() is not None
    sessions = conn.execute("SELECT * FROM endurance_sessions WHERE plan_id=? AND scheduled_date>=? ORDER BY scheduled_date", (plan_id, date.today().isoformat())).fetchall()
    for session in sessions:
        session_changed = False
        rows = conn.execute("SELECT * FROM endurance_steps WHERE session_id=? ORDER BY sort_order,id", (session["id"],)).fetchall()
        steps = [{**dict(row), "target": _loads(row["target_json"], {})} for row in rows]
        repeat_spec = _title_repeat_spec(session["title"])
        if repeat_spec and not any(step["kind"] == "repeat" for step in steps):
            reps, amount, unit = repeat_spec
            work_index = next((i for i, step in enumerate(steps) if step["kind"] not in {"open", "warmup", "cooldown", "recovery"}), None)
            recovery_index = next((i for i, step in enumerate(steps) if work_index is not None and i > work_index and step["kind"] == "recovery"), None)
            if work_index is None:
                raise EnduranceError(f"cannot repair repeat workout without work step: {session['id']}")
            roots: list[dict[str, Any]] = []
            for index, step in enumerate(steps):
                if index in {work_index, recovery_index}:
                    continue
                kind = step["kind"]
                if kind == "open":
                    kind = "warmup" if index < work_index else "cooldown"
                roots.append({"kind": kind, "duration_s": step.get("duration_s"), "distance_m": step.get("distance_m"), "target": step["target"], "notes": step.get("notes"), "sort_order": index})
            work = steps[work_index]
            work_step = {"kind": "work", "target": work["target"], "notes": work.get("notes"), "sort_order": 0}
            if unit == "km": work_step["distance_m"] = round(amount * 1000)
            elif unit == "m": work_step["distance_m"] = round(amount)
            else: work_step["duration_s"] = round(amount * 60 if unit == "min" else amount)
            children = [work_step]
            if recovery_index is not None:
                recovery = steps[recovery_index]
                divisor = max(1, reps - 1)
                children.append({"kind": "recovery", "duration_s": round(float(recovery.get("duration_s") or 0) / divisor) or None, "distance_m": round(float(recovery.get("distance_m") or 0) / divisor) or None, "target": recovery["target"], "notes": recovery.get("notes"), "sort_order": 1})
            roots.append({"kind": "repeat", "reps": reps, "notes": "Main Set", "sort_order": work_index, "steps": children, "target": {"metric": "open", "basis": "open"}})
            _replace_steps(conn, session["id"], sorted(roots, key=lambda item: item["sort_order"]))
            repaired_repeats += 1
            session_changed = True
        elif "stride" in session["title"].lower() and not any(step["kind"] == "stride" for step in steps):
            original = steps[0] if steps else None
            if not original or not session["duration_s"] or int(session["duration_s"]) <= 500:
                raise EnduranceError(f"cannot repair stride workout: {session['id']}")
            roots = [
                {"kind": "open", "duration_s": int(session["duration_s"]) - 500, "target": original["target"], "notes": "Locker"},
                {"kind": "repeat", "reps": 4, "notes": "Strides", "target": {"metric": "open", "basis": "open"}, "steps": [
                    {"kind": "stride", "duration_s": 20, "target": {"metric": "rpe", "basis": "absolute", "min": 7, "max": 8, "source": "session_structure"}, "notes": "Zügig-locker, kein Sprint"},
                    {"kind": "recovery", "duration_s": 140, "target": {"metric": "open", "basis": "open"}, "notes": "Vollständig locker erholen"},
                ]},
            ]
            _replace_steps(conn, session["id"], roots)
            repaired_strides += 1
            session_changed = True

        if "standorttest" in session["title"].lower() and not any(step["kind"] == "stride" for step in steps):
            warmup = next((step for step in steps if step["parent_step_id"] is None and step["kind"] in {"open", "warmup"}), None)
            work = next((step for step in steps if step["kind"] == "work" and step.get("distance_m")), None)
            cooldown = next((step for step in reversed(steps) if step["parent_step_id"] is None and step["kind"] in {"open", "cooldown"} and step is not warmup), None)
            if warmup and work and cooldown and int(warmup.get("duration_s") or 0) >= 260:
                warmup_easy = int(warmup["duration_s"]) - 4 * 20 - 3 * 60
                roots = [
                    {"kind": "warmup", "duration_s": warmup_easy, "target": warmup["target"], "notes": "Locker einlaufen"},
                    {"kind": "repeat", "reps": 4, "notes": "4 Steigerungen", "target": {"metric": "open", "basis": "open"}, "steps": [
                        {"kind": "stride", "duration_s": 20, "target": {"metric": "rpe", "basis": "absolute", "min": 7, "max": 8, "source": "session_structure"}, "notes": "Kurze Steigerung, kein Sprint"},
                        {"kind": "recovery", "duration_s": 60, "target": {"metric": "open", "basis": "open"}, "notes": "Locker weiterlaufen"},
                    ]},
                    {"kind": "work", "distance_m": work["distance_m"], "target": work["target"], "notes": work.get("notes")},
                    {"kind": "cooldown", "duration_s": cooldown["duration_s"], "target": cooldown["target"], "notes": cooldown.get("notes")},
                ]
                _replace_steps(conn, session["id"], roots)
                repaired_strides += 1
                session_changed = True

        if "standorttest" in session["title"].lower():
            benchmark_work = conn.execute(
                "SELECT * FROM endurance_steps WHERE session_id=? AND parent_step_id IS NULL AND kind='work' AND distance_m=5000 ORDER BY sort_order,id LIMIT 1",
                (session["id"],),
            ).fetchone()
            if benchmark_work:
                base_order = int(benchmark_work["sort_order"] or 0)
                conn.execute("UPDATE endurance_steps SET sort_order=sort_order+2 WHERE session_id=? AND parent_step_id IS NULL AND sort_order>?", (session["id"], base_order))
                stages = [
                    (benchmark_work["id"], 2000, 88, 92, 8.0, 8.5, "2 km kontrolliert anlaufen"),
                    (_id("est"), 2000, 92, 96, 8.5, 9.5, "2 km hart und gleichmäßig"),
                    (_id("est"), 1000, 96, 100, 9.5, 10.0, "Letzter Kilometer voll ausfahren"),
                ]
                for offset, (step_id, distance_m, hr_low, hr_high, rpe_low, rpe_high, notes) in enumerate(stages):
                    target = {"metric": "hr", "basis": "absolute", "source": "benchmark_progression", "hr_min_pct": hr_low, "hr_max_pct": hr_high, "rpe_min": rpe_low, "rpe_max": rpe_high}
                    if offset == 0:
                        conn.execute("UPDATE endurance_steps SET distance_m=?,duration_s=NULL,target_json=?,notes=?,sort_order=? WHERE id=?", (distance_m, _json(target), notes, base_order, step_id))
                    else:
                        conn.execute("INSERT INTO endurance_steps VALUES(?,?,?,?,?,?,?,?,?,?)", (step_id, session["id"], None, base_order + offset, "work", None, distance_m, None, _json(target), notes))
                repaired_benchmarks += 1
                session_changed = True

        current_rows = conn.execute("SELECT * FROM endurance_steps WHERE session_id=? ORDER BY sort_order,id", (session["id"],)).fetchall()
        if not any(row["kind"] == "stride" for row in current_rows):
            warmup = next((row for row in current_rows if row["parent_step_id"] is None and row["kind"] in {"open", "warmup"} and "steiger" in str(row["notes"] or "").lower()), None)
            count_match = re.search(r"(\d+)\s*(?:[–-]\s*(\d+))?\s*kurze\s+steiger", str(warmup["notes"] if warmup else ""), re.I)
            if warmup and count_match:
                stride_count = int(count_match.group(2) or count_match.group(1))
                stride_duration = 20
                recovery_duration = 60
                easy_duration = int(warmup["duration_s"] or 0) - stride_count * stride_duration - max(0, stride_count - 1) * recovery_duration
                if stride_count < 1 or easy_duration < 120:
                    raise EnduranceError(f"cannot structure warmup strides without enough duration: {session['id']}")
                base_order = int(warmup["sort_order"] or 0)
                repeat_id = _id("est")
                conn.execute("UPDATE endurance_steps SET sort_order=sort_order+1 WHERE session_id=? AND parent_step_id IS NULL AND sort_order>?", (session["id"], base_order))
                conn.execute("UPDATE endurance_steps SET kind='warmup',duration_s=?,notes='Locker einlaufen.' WHERE id=?", (easy_duration, warmup["id"]))
                conn.execute("INSERT INTO endurance_steps VALUES(?,?,?,?,?,?,?,?,?,?)", (repeat_id, session["id"], None, base_order + 1, "repeat", None, None, stride_count, _json({"metric": "open", "basis": "open"}), f"{stride_count} Steigerungen"))
                conn.execute("INSERT INTO endurance_steps VALUES(?,?,?,?,?,?,?,?,?,?)", (_id("est"), session["id"], repeat_id, 0, "stride", stride_duration, None, None, _json({"metric": "rpe", "basis": "absolute", "min": 7, "max": 8, "source": "session_structure"}), "Zügig beschleunigen, locker bleiben."))
                conn.execute("INSERT INTO endurance_steps VALUES(?,?,?,?,?,?,?,?,?,?)", (_id("est"), session["id"], repeat_id, 1, "recovery", recovery_duration, None, None, _json({"metric": "open", "basis": "open"}), "Locker weiterlaufen."))
                repaired_warmup_strides += 1
                session_changed = True

        if "stride" in session["title"].lower():
            conn.execute("UPDATE endurance_steps SET kind='open' WHERE session_id=? AND parent_step_id IS NULL AND kind='work'", (session["id"],))

        rows = conn.execute("SELECT * FROM endurance_steps WHERE session_id=? ORDER BY sort_order,id", (session["id"],)).fetchall()
        explicit_goal = "zielpace" in session["title"].lower() or "race primer" in session["title"].lower() or session["session_type"] == "race"
        for row in rows:
            if row["kind"] == "repeat":
                continue
            target = _loads(row["target_json"], {}) or {}
            if has_anchor or target.get("source") != "goal_model":
                continue
            rpe_min = target.get("rpe_min", target.get("min")); rpe_max = target.get("rpe_max", target.get("max"))
            if explicit_goal:
                if row["kind"] in {"work", "stride"} and event.get("goal_pace_s_per_km"):
                    pace = int(event["goal_pace_s_per_km"])
                    replacement = {"metric": "pace", "basis": "absolute", "zone": "goal_5k_pace", "source": "explicit_goal", "pace_s_per_km": pace, "pace_min_s_per_km": pace, "pace_max_s_per_km": pace, "rpe_min": rpe_min, "rpe_max": rpe_max}
                else:
                    replacement = {"metric": "open", "basis": "open", "source": "explicit_goal_companion", "rpe_min": rpe_min, "rpe_max": rpe_max}
            elif target.get("hr_min_pct") is not None:
                replacement = {"metric": "hr", "basis": "absolute", "source": "effort_model", "hr_min_pct": target["hr_min_pct"], "hr_max_pct": target.get("hr_max_pct", target["hr_min_pct"]), "rpe_min": rpe_min, "rpe_max": rpe_max}
            else:
                replacement = {"metric": "rpe", "basis": "absolute", "source": "effort_model", "min": rpe_min or 3, "max": rpe_max or rpe_min or 4}
            replacement = {key: value for key, value in replacement.items() if value is not None}
            conn.execute("UPDATE endurance_steps SET target_json=? WHERE id=?", (_json(replacement), row["id"]))
            conn.execute("DELETE FROM endurance_resolutions WHERE step_id=?", (row["id"],))
            repaired_targets += 1
            session_changed = True
        if session_changed:
            conn.execute("UPDATE endurance_sessions SET sync_state=CASE WHEN sync_state='synced' THEN 'dirty' ELSE sync_state END,updated_at=? WHERE id=?", (_now(), session["id"]))
    metrics = _derive_plan_metrics(conn, plan_id, force_paces=False, session_ids={row["id"] for row in sessions})
    return {"repeats": repaired_repeats, "strides": repaired_strides, "warmup_strides": repaired_warmup_strides, "benchmarks": repaired_benchmarks, "targets": repaired_targets, **metrics}


def _derive_plan_metrics(
    conn: sqlite3.Connection,
    plan_id: str,
    *,
    force_paces: bool = False,
    session_ids: set[str] | None = None,
) -> dict[str, int]:
    event = _event_payload(conn.execute("SELECT * FROM endurance_events WHERE plan_id=? ORDER BY event_date LIMIT 1", (plan_id,)).fetchone())
    phases = conn.execute("SELECT * FROM endurance_phases WHERE plan_id=? ORDER BY start_date", (plan_id,)).fetchall()
    if not event or not event.get("goal_pace_s_per_km"):
        return {"sessions": 0, "pace_targets": 0}
    season_start = date.fromisoformat(phases[0]["start_date"]) if phases else date.today()
    goal_date = date.fromisoformat(event["event_date"])
    season_days = max(1, (goal_date - season_start).days)
    anchor_exists = conn.execute("SELECT 1 FROM endurance_fitness_anchors WHERE plan_id=? LIMIT 1", (plan_id,)).fetchone() is not None
    changed_sessions = pace_targets = hr_targets = 0
    sessions = conn.execute("SELECT * FROM endurance_sessions WHERE plan_id=? ORDER BY scheduled_date", (plan_id,)).fetchall()
    for session in sessions:
        if session_ids is not None and session["id"] not in session_ids:
            continue
        session_date = date.fromisoformat(session["scheduled_date"])
        progress = max(0.0, min(1.0, (session_date - season_start).days / season_days))
        steps = conn.execute("SELECT * FROM endurance_steps WHERE session_id=? ORDER BY sort_order,id", (session["id"],)).fetchall()
        work_indexes = [index for index, step in enumerate(steps) if step["kind"] not in {"open", "warmup", "cooldown", "recovery", "repeat"}]
        first_work = min(work_indexes) if work_indexes else 0
        updated_targets: dict[str, dict[str, Any]] = {}
        for index, step in enumerate(steps):
            target = _loads(step["target_json"], {}) or {}
            original_target = copy.deepcopy(target)
            effective_kind = step["kind"]
            if effective_kind == "open":
                effective_kind = "warmup" if index <= first_work else "cooldown"
            pace_value = _goal_model_pace(session["session_type"], effective_kind, int(event["goal_pace_s_per_km"]), progress, session["title"])
            explicit_goal = "zielpace" in session["title"].lower() or "race primer" in session["title"].lower() or session["session_type"] == "race"
            goal_work_step = explicit_goal and effective_kind not in {"warmup", "cooldown", "recovery", "open"}
            may_replace = (anchor_exists or goal_work_step) and (not target or target.get("source") == "goal_model" or (force_paces and target.get("metric") in {None, "open", "rpe"}))
            if pace_value and may_replace and step["kind"] != "repeat":
                old_rpe_min = target.get("rpe_min", target.get("min") if target.get("metric") == "rpe" else None)
                old_rpe_max = target.get("rpe_max", target.get("max") if target.get("metric") == "rpe" else None)
                width = 15 if session["session_type"] in {"easy", "long"} or effective_kind in {"warmup", "cooldown", "recovery"} else 8
                zone = "easy" if effective_kind in {"warmup", "cooldown", "open", "recovery"} else ("goal_5k_pace" if session["session_type"] == "race" else session["session_type"])
                if zone not in TARGET_ZONES:
                    zone = "easy"
                target = {
                    "metric": "pace", "basis": "absolute", "zone": zone, "source": "goal_model",
                    "pace_s_per_km": pace_value, "pace_min_s_per_km": pace_value - width, "pace_max_s_per_km": pace_value + width,
                    "rpe_min": old_rpe_min if old_rpe_min is not None else max(1, _default_rpe(session["session_type"], effective_kind) - 0.5),
                    "rpe_max": old_rpe_max if old_rpe_max is not None else min(10, _default_rpe(session["session_type"], effective_kind) + 0.5),
                }
            explicitly_open = target.get("basis") == "open" or target.get("metric") == "open"
            if step["kind"] not in {"repeat", "stride"} and not explicit_goal and not explicitly_open and (target.get("hr_min_pct") is None or target.get("hr_max_pct") is None):
                hr_min_pct, hr_max_pct = _goal_model_hr_percent(session["session_type"], effective_kind)
                target = {**target, "hr_min_pct": hr_min_pct, "hr_max_pct": hr_max_pct}
                hr_targets += 1
            if step["kind"] != "repeat" and not anchor_exists and not explicit_goal and not explicitly_open and target.get("basis") != "fitness_anchor" and target.get("metric") != "rpe" and target.get("pace_s_per_km") is None:
                rpe_min = target.get("rpe_min", target.get("min") if target.get("metric") == "rpe" else None)
                rpe_max = target.get("rpe_max", target.get("max") if target.get("metric") == "rpe" else None)
                target = {key: value for key, value in {"metric": "hr", "basis": "absolute", "source": target.get("source") if target.get("source") not in {None, "goal_model"} else "effort_model", "hr_min_pct": target.get("hr_min_pct"), "hr_max_pct": target.get("hr_max_pct"), "rpe_min": rpe_min, "rpe_max": rpe_max}.items() if value is not None}
            if target != original_target:
                conn.execute("UPDATE endurance_steps SET target_json=? WHERE id=?", (_json(target), step["id"]))
                if target.get("pace_s_per_km") != original_target.get("pace_s_per_km"):
                    pace_targets += 1
            updated_targets[step["id"]] = target

        by_parent: dict[str | None, list[sqlite3.Row]] = {}
        for step in steps:
            by_parent.setdefault(step["parent_step_id"], []).append(step)

        def totals(step: sqlite3.Row) -> tuple[float, float, float]:
            target = updated_targets.get(step["id"], {})
            pace_value = target.get("pace_s_per_km")
            effective_kind = step["kind"]
            if effective_kind == "open":
                effective_kind = "warmup"
            fallback_pace = pace_value or _goal_model_pace(session["session_type"], effective_kind, int(event["goal_pace_s_per_km"]), progress, session["title"]) or int(event["goal_pace_s_per_km"]) + 75
            own_duration = float(step["duration_s"] or 0)
            own_distance = float(step["distance_m"] or (own_duration / fallback_pace * 1000 if own_duration else 0))
            if not own_duration and own_distance:
                own_duration = own_distance * fallback_pace / 1000
            rpe = target.get("rpe")
            if rpe is None and target.get("rpe_min") is not None:
                rpe = (float(target["rpe_min"]) + float(target.get("rpe_max", target["rpe_min"]))) / 2
            if rpe is None and target.get("metric") == "rpe" and target.get("min") is not None:
                rpe = (float(target["min"]) + float(target.get("max", target["min"]))) / 2
            rpe = float(rpe if rpe is not None else _default_rpe(session["session_type"], effective_kind))
            child_distance = child_duration = child_load = 0.0
            for child in by_parent.get(step["id"], []):
                distance_value, duration_value, load_value = totals(child)
                child_distance += distance_value
                child_duration += duration_value
                child_load += load_value
            reps = max(1, int(step["reps"] or 1))
            if step["kind"] == "repeat":
                child_distance = child_duration = child_load = 0.0
                for child in by_parent.get(step["id"], []):
                    distance_value, duration_value, load_value = totals(child)
                    multiplier = max(0, reps - 1) if child["kind"] == "recovery" else reps
                    child_distance += distance_value * multiplier; child_duration += duration_value * multiplier; child_load += load_value * multiplier
                return own_distance + child_distance, own_duration + child_duration, own_duration / 60 * rpe + child_load
            return own_distance + child_distance, own_duration + child_duration, own_duration / 60 * rpe + child_load

        distance_total = duration_total = load_total = 0.0
        for root in by_parent.get(None, []):
            distance_value, duration_value, load_value = totals(root)
            distance_total += distance_value
            duration_total += duration_value
            load_total += load_value
        duration_total = float(session["duration_s"] or duration_total)
        if not load_total and duration_total:
            load_total = duration_total / 60 * _default_rpe(session["session_type"], "work")
        distance_m = session["distance_m"] if session["distance_m"] not in (None, 0) else (int(round(distance_total / 100) * 100) if distance_total else None)
        duration_s = int(round(duration_total)) if duration_total else session["duration_s"]
        load_value = session["load_value"] if session["load_value"] not in (None, 0) else (round(load_total) if load_total else None)
        if distance_m != session["distance_m"] or duration_s != session["duration_s"] or load_value != session["load_value"]:
            conn.execute("UPDATE endurance_sessions SET distance_m=?,duration_s=?,load_value=?,updated_at=? WHERE id=?", (distance_m, duration_s, load_value, _now(), session["id"]))
            changed_sessions += 1
    return {"sessions": changed_sessions, "pace_targets": pace_targets, "hr_targets": hr_targets}


def _record_revision(conn: sqlite3.Connection, plan_id: str, operation: str, summary: str, diff: dict[str, Any]) -> int:
    revision = int(conn.execute("SELECT revision FROM endurance_plans WHERE id=?", (plan_id,)).fetchone()[0]) + 1
    now = _now()
    conn.execute("UPDATE endurance_plans SET revision=?,updated_at=? WHERE id=?", (revision, now, plan_id))
    conn.execute("INSERT INTO endurance_plan_revisions VALUES(?,?,?,?,?,?,?)", (_id("er"), plan_id, revision, operation, summary, _json(diff), now))
    return revision


def _replace_steps(conn: sqlite3.Connection, session_id: str, steps: list[dict[str, Any]]) -> None:
    session = conn.execute("SELECT title FROM endurance_sessions WHERE id=?", (session_id,)).fetchone()
    if not session:
        raise EnduranceError("session not found")
    normalized = [_normalize_step(step) for step in steps]
    _validate_session_structure({"title": session["title"], "steps": normalized})
    conn.execute("DELETE FROM endurance_steps WHERE session_id=?", (session_id,))
    _insert_steps(conn, session_id, normalized)


def _rebase(conn: sqlite3.Connection, plan_id: str, from_date: str, apply: bool) -> dict[str, Any]:
    event = _event_payload(conn.execute("SELECT * FROM endurance_events WHERE plan_id=? ORDER BY event_date LIMIT 1", (plan_id,)).fetchone())
    anchor_row = conn.execute("SELECT * FROM endurance_fitness_anchors WHERE plan_id=? ORDER BY anchor_date DESC,created_at DESC LIMIT 1", (plan_id,)).fetchone()
    anchor = dict(anchor_row) if anchor_row else None
    if anchor:
        anchor["zones"] = _loads(anchor.get("zones_json"), {})
    changes, unchanged = [], 0
    for row in conn.execute("SELECT st.*,s.scheduled_date FROM endurance_steps st JOIN endurance_sessions s ON s.id=st.session_id WHERE s.plan_id=? AND s.scheduled_date>=? ORDER BY s.scheduled_date,st.sort_order", (plan_id, from_date)):
        target = _loads(row["target_json"], {})
        resolved = _resolve_target(target, anchor, event)
        current = conn.execute("SELECT * FROM endurance_resolutions WHERE step_id=?", (row["id"],)).fetchone()
        before = _loads(current["resolved_target_json"], None) if current else None
        if before == resolved:
            unchanged += 1
            continue
        changes.append({"session_id": row["session_id"], "step_id": row["id"], "date": row["scheduled_date"], "before": before, "after": resolved})
        if apply:
            conn.execute("DELETE FROM endurance_resolutions WHERE step_id=?", (row["id"],))
            if resolved is not None:
                conn.execute("INSERT INTO endurance_resolutions VALUES(?,?,?,?,?,?)", (_id("ers"), row["session_id"], row["id"], anchor["id"] if anchor else None, _now(), _json(resolved)))
    return {"from_date": from_date, "changed": len(changes), "unchanged": unchanged, "past_changed": 0, "changes": changes}


def patch_plan(plan_id: str, operations: list[dict[str, Any]], *, dry_run: bool = False, confirm: bool = False, expected_revision: int | None = None) -> dict[str, Any]:
    ensure_endurance_schema()
    if not operations:
        raise EnduranceError("operations must not be empty")
    conn = get_plans_db()
    plan = conn.execute("SELECT * FROM endurance_plans WHERE id=?", (plan_id,)).fetchone()
    if not plan:
        conn.close()
        raise EnduranceError("endurance plan not found")
    if expected_revision is not None and int(plan["revision"]) != int(expected_revision):
        conn.close()
        raise EnduranceError("revision_conflict")
    if any(str(op.get("op")) in DESTRUCTIVE_OPS for op in operations) and not (confirm or dry_run):
        conn.close()
        raise EnduranceError("confirm_required")
    before = get_plan(plan_id)
    diffs: list[dict[str, Any]] = []
    added_session_ids: set[str] = set()
    try:
        conn.execute("BEGIN")
        database_before = _database_snapshot(conn, plan_id)
        for op in operations:
            name = str(op.get("op") or "")
            if name not in OPERATION_FIELDS:
                raise EnduranceError(f"invalid operation: {name}")
            _reject_unknown(op, OPERATION_FIELDS[name], f"operation {name}")
            if name == "set_goal_event":
                event = op.get("event") or op
                if op.get("event") is not None:
                    _reject_unknown(event, {"id", "title", "distance_m", "target_time_s", "event_date", "priority"}, "goal event")
                existing = conn.execute("SELECT id FROM endurance_events WHERE plan_id=? LIMIT 1", (plan_id,)).fetchone()
                values = (event.get("title"), event.get("distance_m"), event.get("target_time_s"), _date(event.get("event_date"), "event_date"), str(event.get("priority") or "A"), _now())
                if existing:
                    conn.execute("UPDATE endurance_events SET title=?,distance_m=?,target_time_s=?,event_date=?,priority=?,updated_at=? WHERE id=?", (*values, existing["id"]))
                else:
                    conn.execute("INSERT INTO endurance_events VALUES(?,?,?,?,?,?,?,?,?)", (_id("ee"), plan_id, *values[:-1], _now(), values[-1]))
                _normalize_phase_chain(conn, plan_id)
            elif name == "set_season_start":
                phases = _phase_chain(conn, plan_id)
                if not phases:
                    raise EnduranceError("season start requires at least one phase")
                old_start = date.fromisoformat(phases[0]["start_date"])
                new_start = date.fromisoformat(_date(op.get("start_date"), "start_date"))
                event_row = conn.execute("SELECT event_date FROM endurance_events WHERE plan_id=? ORDER BY event_date LIMIT 1", (plan_id,)).fetchone()
                if event_row and new_start > date.fromisoformat(event_row["event_date"]):
                    raise EnduranceError("season start must not be after the goal date")
                delta = (new_start - old_start).days
                conn.execute("UPDATE endurance_phases SET start_date=?,updated_at=? WHERE id=?", (new_start.isoformat(), _now(), phases[0]["id"]))
                if delta and op.get("shift_sessions", True):
                    goal_date = event_row["event_date"] if event_row else None
                    for session in conn.execute("SELECT id,scheduled_date,session_type FROM endurance_sessions WHERE plan_id=?", (plan_id,)).fetchall():
                        shifted = (date.fromisoformat(session["scheduled_date"]) + timedelta(days=delta)).isoformat()
                        if session["session_type"] == "race" and goal_date:
                            shifted = goal_date
                        conn.execute("UPDATE endurance_sessions SET scheduled_date=?,sync_state=CASE WHEN sync_state='synced' THEN 'dirty' ELSE sync_state END,updated_at=? WHERE id=?", (shifted, _now(), session["id"]))
                _normalize_phase_chain(conn, plan_id)
            elif name == "add_phase":
                phase_payload = op.get("phase") or {key: value for key, value in op.items() if key != "op"}
                current_count = int(conn.execute("SELECT COUNT(*) FROM endurance_phases WHERE plan_id=?", (plan_id,)).fetchone()[0])
                _insert_phase(conn, plan_id, phase_payload, current_count)
                _normalize_phase_chain(conn, plan_id)
            elif name in {"update_phase", "move_phase", "resize_phase"}:
                _normalize_phase_chain(conn, plan_id)
                row = conn.execute("SELECT * FROM endurance_phases WHERE id=? AND plan_id=?", (op.get("phase_id"), plan_id)).fetchone()
                if not row:
                    raise EnduranceError("phase not found")
                phase_type = str(op.get("phase_type", row["phase_type"])).lower()
                phase_type = {"5k_specific": "specific", "peak_taper": "taper"}.get(phase_type, phase_type)
                if phase_type not in PHASE_TYPES:
                    raise EnduranceError(f"invalid phase_type: {phase_type}")
                if name == "move_phase":
                    delta = _smart_move_phase(conn, plan_id, row, op)
                    if delta:
                        linked = conn.execute("SELECT id,scheduled_date FROM endurance_sessions WHERE phase_id=?", (row["id"],)).fetchall()
                        for session in linked:
                            shifted = (date.fromisoformat(session["scheduled_date"]) + timedelta(days=delta)).isoformat()
                            conn.execute("UPDATE endurance_sessions SET scheduled_date=?,sync_state=CASE WHEN sync_state='synced' THEN 'dirty' ELSE sync_state END,updated_at=? WHERE id=?", (shifted, _now(), session["id"]))
                elif name == "resize_phase":
                    _smart_resize_phase(conn, plan_id, row, op)
                else:
                    start = _date(op.get("start_date", row["start_date"]), "start_date")
                    end = _date(op.get("end_date", row["end_date"]), "end_date")
                    if end < start:
                        raise EnduranceError("phase end_date must not precede start_date")
                    conn.execute("UPDATE endurance_phases SET start_date=?,end_date=?,updated_at=? WHERE id=?", (start, end, _now(), row["id"]))
                conn.execute("UPDATE endurance_phases SET name=?,phase_type=?,notes=?,updated_at=? WHERE id=?", (op.get("name", row["name"]), phase_type, op.get("notes", row["notes"]), _now(), row["id"]))
                _normalize_phase_chain(conn, plan_id)
            elif name == "delete_phase":
                conn.execute("UPDATE endurance_sessions SET phase_id=NULL WHERE phase_id=?", (op.get("phase_id"),))
                conn.execute("DELETE FROM endurance_phases WHERE id=? AND plan_id=?", (op.get("phase_id"), plan_id))
                _normalize_phase_chain(conn, plan_id)
            elif name == "normalize_phases":
                _normalize_phase_chain(conn, plan_id)
            elif name == "derive_metrics":
                diffs.append({"derived_metrics": _derive_plan_metrics(conn, plan_id, force_paces=True)})
            elif name == "repair_workout_semantics":
                diffs.append({"repaired_workout_semantics": _repair_workout_semantics(conn, plan_id)})
            elif name == "add_session":
                added_session_ids.add(_insert_session(conn, plan_id, op.get("session") or op))
            elif name in {"move_session", "update_session"}:
                row = conn.execute("SELECT * FROM endurance_sessions WHERE id=? AND plan_id=?", (op.get("session_id"), plan_id)).fetchone()
                if not row:
                    raise EnduranceError("session not found")
                new_date = _date(op.get("date", op.get("scheduled_date", row["scheduled_date"])), "date")
                if op.get("duration_s") not in (None, "") and op.get("duration_min") not in (None, ""):
                    raise EnduranceError("update_session must use either duration_s or duration_min")
                if op.get("distance_m") not in (None, "") and op.get("distance_km") not in (None, ""):
                    raise EnduranceError("update_session must use either distance_m or distance_km")
                raw_kind = str(op.get("session_type") or op.get("sport_type") or row["session_type"]).lower()
                kind = {"intervals": "interval", "5k": "5k_specific"}.get(raw_kind, raw_kind)
                if kind not in SESSION_TYPES:
                    raise EnduranceError(f"invalid session_type: {raw_kind}")
                duration_s = round(float(op["duration_min"]) * 60) if op.get("duration_min") not in (None, "") else op.get("duration_s", row["duration_s"])
                distance_m = round(float(op["distance_km"]) * 1000) if op.get("distance_km") not in (None, "") else op.get("distance_m", row["distance_m"])
                conn.execute("UPDATE endurance_sessions SET scheduled_date=?,phase_id=?,title=?,session_type=?,status=?,duration_s=?,distance_m=?,load_value=?,tags_json=?,notes=?,sync_state=CASE WHEN sync_state='synced' THEN 'dirty' ELSE sync_state END,updated_at=? WHERE id=?", (new_date, op.get("phase_id", row["phase_id"]), op.get("title", row["title"]), kind, op.get("status", row["status"]), duration_s, distance_m, op.get("load_value", row["load_value"]), _json(op.get("tags", _loads(row["tags_json"], []))), op.get("notes", row["notes"]), _now(), row["id"]))
            elif name == "swap_sessions":
                a = conn.execute("SELECT scheduled_date FROM endurance_sessions WHERE id=? AND plan_id=?", (op.get("session_id"), plan_id)).fetchone()
                b = conn.execute("SELECT scheduled_date FROM endurance_sessions WHERE id=? AND plan_id=?", (op.get("other_session_id"), plan_id)).fetchone()
                if not a or not b:
                    raise EnduranceError("session not found")
                conn.execute("UPDATE endurance_sessions SET scheduled_date=?,updated_at=? WHERE id=?", (b["scheduled_date"], _now(), op["session_id"]))
                conn.execute("UPDATE endurance_sessions SET scheduled_date=?,updated_at=? WHERE id=?", (a["scheduled_date"], _now(), op["other_session_id"]))
            elif name == "duplicate_session":
                source = conn.execute("SELECT * FROM endurance_sessions WHERE id=? AND plan_id=?", (op.get("session_id"), plan_id)).fetchone()
                if not source:
                    raise EnduranceError("session not found")
                payload = _session_payload(conn, source)
                for key in ("id", "plan_id", "created_at", "updated_at", "actual_run_id", "intervals_sync", "execution_steps", "execution_contract"):
                    payload.pop(key, None)
                payload["scheduled_date"] = _date(op.get("date", payload["scheduled_date"]), "date")
                flat = payload["steps"]
                children: dict[str | None, list[dict[str, Any]]] = {}
                for step in flat:
                    children.setdefault(step.get("parent_step_id"), []).append(step)
                def clone_steps(parent: str | None) -> list[dict[str, Any]]:
                    cloned = []
                    for step in sorted(children.get(parent, []), key=lambda item: item["sort_order"]):
                        item = {key: step.get(key) for key in ("kind", "duration_s", "distance_m", "reps", "target", "notes") if step.get(key) is not None}
                        nested = clone_steps(step["id"])
                        if nested:
                            item["steps"] = nested
                        cloned.append(item)
                    return cloned
                payload["steps"] = clone_steps(None)
                _insert_session(conn, plan_id, payload)
            elif name == "delete_session":
                conn.execute("DELETE FROM endurance_sessions WHERE id=? AND plan_id=?", (op.get("session_id"), plan_id))
            elif name == "replace_steps":
                _replace_steps(conn, str(op.get("session_id")), list(op.get("steps") or []))
                conn.execute("UPDATE endurance_sessions SET sync_state=CASE WHEN sync_state='synced' THEN 'dirty' ELSE sync_state END,updated_at=? WHERE id=?", (_now(), op.get("session_id")))
            elif name == "move_step":
                step = conn.execute("SELECT st.* FROM endurance_steps st JOIN endurance_sessions s ON s.id=st.session_id WHERE st.id=? AND s.plan_id=?", (op.get("step_id"), plan_id)).fetchone()
                if not step:
                    raise EnduranceError("step not found")
                conn.execute("UPDATE endurance_steps SET parent_step_id=?,sort_order=? WHERE id=?", (op.get("parent_step_id", step["parent_step_id"]), int(op.get("sort_order", step["sort_order"])), step["id"]))
                conn.execute("UPDATE endurance_sessions SET sync_state=CASE WHEN sync_state='synced' THEN 'dirty' ELSE sync_state END,updated_at=? WHERE id=?", (_now(), step["session_id"]))
            elif name == "set_session_target":
                step = conn.execute("SELECT st.* FROM endurance_steps st JOIN endurance_sessions s ON s.id=st.session_id WHERE st.id=? AND s.plan_id=?", (op.get("step_id"), plan_id)).fetchone()
                if not step:
                    raise EnduranceError("step not found")
                target = op.get("target")
                if not isinstance(target, dict):
                    raise EnduranceError("target must be an object")
                _reject_unknown(target, TARGET_FIELDS, "workout target")
                conn.execute("UPDATE endurance_steps SET target_json=? WHERE id=?", (_json(target), step["id"]))
                conn.execute("DELETE FROM endurance_resolutions WHERE step_id=?", (step["id"],))
                conn.execute("UPDATE endurance_sessions SET sync_state=CASE WHEN sync_state='synced' THEN 'dirty' ELSE sync_state END,updated_at=? WHERE id=?", (_now(), step["session_id"]))
            elif name == "set_fitness_anchor":
                anchor = op.get("anchor") or op
                if not isinstance(anchor, dict):
                    raise EnduranceError("anchor must be an object")
                _reject_unknown(anchor, {"date", "anchor_date", "source", "five_k_time_s", "threshold_pace_s_per_km", "zones", "notes"}, "fitness anchor")
                conn.execute("INSERT INTO endurance_fitness_anchors VALUES(?,?,?,?,?,?,?,?,?)", (_id("efa"), plan_id, _date(anchor.get("date") or anchor.get("anchor_date"), "anchor.date"), str(anchor.get("source") or "manual"), anchor.get("five_k_time_s"), anchor.get("threshold_pace_s_per_km"), _json(anchor.get("zones") or {}), anchor.get("notes"), _now()))
            elif name == "clear_fitness_anchor":
                conn.execute("DELETE FROM endurance_fitness_anchors WHERE plan_id=?", (plan_id,))
            elif name == "rebase_future_targets":
                diffs.append(_rebase(conn, plan_id, _date(op.get("from_date") or date.today().isoformat(), "from_date"), True))
            else:
                raise EnduranceError(f"invalid operation: {name}")
        if added_session_ids:
            added_metrics = _derive_plan_metrics(conn, plan_id, force_paces=True, session_ids=added_session_ids)
            if added_metrics["sessions"] or added_metrics["pace_targets"] or added_metrics["hr_targets"]:
                diffs.append({"added_session_metrics": added_metrics})
        automatic_metrics = _derive_plan_metrics(conn, plan_id, force_paces=False)
        if automatic_metrics["sessions"] or automatic_metrics["pace_targets"] or automatic_metrics["hr_targets"]:
            diffs.append({"automatic_metrics": automatic_metrics})
        from integrations.endurance_intervals_sync import queue_plan_sync_changes
        sync_jobs = queue_plan_sync_changes(conn, plan_id, database_before)
        if sync_jobs["upsert"] or sync_jobs["delete"]:
            diffs.append({"intervals_sync": sync_jobs})
        summary = ", ".join(str(op.get("op")) for op in operations)
        revision = _record_revision(conn, plan_id, "patch_endurance_plan", summary, {"operations": operations, "rebases": diffs, "database_before": database_before})
        preview = get_plan_from_connection(conn, plan_id)
        if dry_run:
            conn.rollback()
            conn.close()
            return {"ok": True, "dry_run": True, "before_revision": before["revision"], "preview": preview, "diff": {"operations": operations, "rebases": diffs}}
        conn.commit()
        conn.close()
        return {"ok": True, "dry_run": False, "revision": revision, "plan": get_plan(plan_id), "diff": {"operations": operations, "rebases": diffs}}
    except Exception:
        conn.rollback()
        conn.close()
        raise


def undo_plan(plan_id: str, *, expected_revision: int | None = None) -> dict[str, Any]:
    ensure_endurance_schema()
    conn = get_plans_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        plan = conn.execute("SELECT * FROM endurance_plans WHERE id=?", (plan_id,)).fetchone()
        if not plan:
            raise EnduranceError("endurance plan not found")
        if expected_revision is not None and int(plan["revision"]) != int(expected_revision):
            raise EnduranceError("revision_conflict")
        candidate = _undo_candidate(conn, plan_id)
        if not candidate:
            raise EnduranceError("nothing_to_undo")
        target, diff = candidate
        current = _database_snapshot(conn, plan_id)
        removes_created_plan = target["operation"] == "create_plan" and bool(diff.get("created_plan"))
        if removes_created_plan:
            conn.execute("UPDATE endurance_plans SET status='archived',updated_at=? WHERE id=?", (_now(), plan_id))
        else:
            _restore_database_snapshot(conn, plan_id, diff["database_before"])
        from integrations.endurance_intervals_sync import queue_plan_sync_changes
        queue_plan_sync_changes(conn, plan_id, current)
        revision = _record_revision(
            conn,
            plan_id,
            "undo",
            f"Rückgängig: {target['summary']}",
            {"target_revision": int(target["revision"]), "target_operation": target["operation"], "database_before": current},
        )
        conn.commit()
        conn.close()
        return {"ok": True, "revision": revision, "undone_revision": int(target["revision"]), "summary": target["summary"], "plan_removed": removes_created_plan, "plan": None if removes_created_plan else get_plan(plan_id)}
    except Exception:
        conn.rollback()
        conn.close()
        raise


def redo_plan(plan_id: str, *, expected_revision: int | None = None) -> dict[str, Any]:
    ensure_endurance_schema()
    conn = get_plans_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        plan = conn.execute("SELECT * FROM endurance_plans WHERE id=?", (plan_id,)).fetchone()
        if not plan:
            raise EnduranceError("endurance plan not found")
        if expected_revision is not None and int(plan["revision"]) != int(expected_revision):
            raise EnduranceError("revision_conflict")
        candidate = _redo_candidate(conn, plan_id)
        if not candidate:
            raise EnduranceError("nothing_to_redo")
        target_undo, diff = candidate
        current = _database_snapshot(conn, plan_id)
        snapshot = diff.get("database_before")
        if not snapshot:
            raise EnduranceError("nothing_to_redo")
        _restore_database_snapshot(conn, plan_id, snapshot)
        from integrations.endurance_intervals_sync import queue_plan_sync_changes
        queue_plan_sync_changes(conn, plan_id, current)
        repeated_summary = str(target_undo["summary"]).removeprefix("Rückgängig: ")
        revision = _record_revision(
            conn,
            plan_id,
            "redo",
            f"Wiederholt: {repeated_summary}",
            {
                "target_undo_revision": int(target_undo["revision"]),
                "target_revision": int(diff["target_revision"]),
                "database_before": current,
            },
        )
        conn.commit()
        conn.close()
        return {
            "ok": True,
            "revision": revision,
            "redone_revision": int(diff["target_revision"]),
            "summary": repeated_summary,
            "plan": get_plan(plan_id),
        }
    except Exception:
        conn.rollback()
        conn.close()
        raise


def get_plan_from_connection(conn: sqlite3.Connection, plan_id: str) -> dict[str, Any]:
    plan = dict(conn.execute("SELECT * FROM endurance_plans WHERE id=?", (plan_id,)).fetchone())
    event = _event_payload(conn.execute("SELECT * FROM endurance_events WHERE plan_id=? LIMIT 1", (plan_id,)).fetchone())
    phases = [dict(row) for row in conn.execute("SELECT * FROM endurance_phases WHERE plan_id=? ORDER BY start_date", (plan_id,))]
    if len({phase["sort_order"] for phase in phases}) != len(phases):
        for index, phase in enumerate(phases):
            phase["sort_order"] = index
            phase["sort_order_source"] = "normalized_from_dates"
    sessions = [_session_payload(conn, row) for row in conn.execute("SELECT * FROM endurance_sessions WHERE plan_id=? ORDER BY scheduled_date", (plan_id,))]
    pace_model = _withhold_low_confidence_execution_paces(sessions)
    anchor_row = conn.execute("SELECT * FROM endurance_fitness_anchors WHERE plan_id=? ORDER BY anchor_date DESC,created_at DESC LIMIT 1", (plan_id,)).fetchone()
    anchor = dict(anchor_row) if anchor_row else None
    if anchor:
        anchor["zones"] = _loads(anchor.pop("zones_json"), {})
    return {**plan, "event": event, "phases": phases, "sessions": sessions, "fitness_anchor": anchor, "pace_model": pace_model, "warnings": validate_plan(phases, sessions, event)}


def preview_rebase(plan_id: str, from_date: str) -> dict[str, Any]:
    ensure_endurance_schema()
    conn = get_plans_db()
    if not conn.execute("SELECT 1 FROM endurance_plans WHERE id=?", (plan_id,)).fetchone():
        conn.close(); raise EnduranceError("endurance plan not found")
    result = _rebase(conn, plan_id, _date(from_date, "from_date"), False)
    conn.close()
    return result
