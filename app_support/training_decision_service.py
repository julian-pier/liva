"""Side-effect-free production service for MCP training decisions.

It deliberately resolves only explicit rolling-sequence sessions from an active
plan and a real workout anchor. It never derives a session from weekday alone
and never writes workout sets.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import date as Date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class DecisionPaths:
    training: Path
    plans: Path
    hrv: Path
    nutrition: Path
    core: Path
    core_write: Path | None = None
    audit: Path | None = None


def _connect(path: Path, *, write: bool = False) -> sqlite3.Connection:
    if not write:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
        conn.execute("PRAGMA query_only=ON")
    else:
        conn = sqlite3.connect(path, timeout=5)
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        # The remote service bind-mounts this one canonical database file.
        # DELETE mode keeps SQLite journals in that file and avoids fragile
        # required mounts for transient -wal/-shm sidecars.
        conn.execute("PRAGMA journal_mode=DELETE")
    conn.row_factory = sqlite3.Row
    return conn


def _hash(value: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def _token(value: Any) -> str:
    return "".join(ch for ch in str(value or "").casefold() if ch.isalnum())


def _session_matches(anchor: str, title: str) -> bool:
    a, b = _token(anchor), _token(title)
    return bool(a and b and (a in b or b in a or ("pusha" in a and "pusha" in b) or ("pulla" in a and "pulla" in b) or ("pushb" in a and "pushb" in b) or ("pullb" in a and "pullb" in b)))


def _latest_row(path: Path, sql: str, params: tuple[Any, ...]) -> dict[str, Any] | None:
    try:
        with _connect(path) as conn:
            row = conn.execute(sql, params).fetchone()
            return dict(row) if row else None
    except sqlite3.DatabaseError:
        return None


WEEKDAY_KEYS = ("Mo", "Di", "Mi", "Do", "Fr", "Sa", "So")


def _rolling_day_resolution(plan_json: dict[str, Any], requested_date: str, selected: dict[str, Any], selected_index: int) -> dict[str, Any]:
    """Separate the next rolling position from its calendar relation."""
    meta = plan_json.get("meta") if isinstance(plan_json.get("meta"), dict) else {}
    pattern = meta.get("rolling_week_pattern") if isinstance(meta.get("rolling_week_pattern"), dict) else {}
    day = Date.fromisoformat(requested_date)
    day_key = WEEKDAY_KEYS[day.weekday()]
    raw_status = str(pattern.get(day_key) or "").strip().casefold()
    selected_payload = {
        "session_key": str(selected["id"]),
        "name": str(selected["title"]),
        "session_type": "gym",
    }
    if raw_status in {"rest", "off", "pause"}:
        next_date = None
        for offset in range(1, 15):
            candidate = day + timedelta(days=offset)
            if str(pattern.get(WEEKDAY_KEYS[candidate.weekday()]) or "").strip().casefold() in {"train", "training", "gym"}:
                next_date = candidate.isoformat()
                break
        return {
            "requested_date": requested_date,
            "timezone": "Europe/Berlin",
            "today_status": "rest",
            "decision_scope": "requested_date",
            "session_relation": "next",
            "is_today_session": False,
            "is_rest_day": True,
            "today_session": None,
            "next_session": selected_payload,
            "next_session_date": next_date,
            "calendar_source": "active_plan.meta.rolling_week_pattern",
            "rolling_sequence_position": selected_index,
            "resolution_status": "resolved",
            "resolution_reason": f"rolling week pattern marks {day_key} as rest; sequence item remains next",
        }
    if raw_status in {"train", "training", "gym"}:
        return {
            "requested_date": requested_date,
            "timezone": "Europe/Berlin",
            "today_status": "training",
            "decision_scope": "requested_date",
            "session_relation": "today",
            "is_today_session": True,
            "is_rest_day": False,
            "today_session": selected_payload,
            "next_session": selected_payload,
            "next_session_date": requested_date,
            "calendar_source": "active_plan.meta.rolling_week_pattern",
            "rolling_sequence_position": selected_index,
            "resolution_status": "resolved",
            "resolution_reason": f"rolling week pattern marks {day_key} as training",
        }
    return {
        "requested_date": requested_date,
        "timezone": "Europe/Berlin",
        "today_status": "unresolved",
        "decision_scope": "requested_date",
        "session_relation": "next",
        "is_today_session": False,
        "is_rest_day": False,
        "today_session": None,
        "next_session": selected_payload,
        "next_session_date": None,
        "calendar_source": "unavailable",
        "rolling_sequence_position": selected_index,
        "resolution_status": "unresolved",
        "resolution_reason": f"rolling sequence has no calendar status for {day_key}",
    }


def build_training_decision_context(paths: DecisionPaths, date_iso: str, subjective_input: dict[str, Any] | None = None, constraints: str | None = None, force_session_key: str | None = None) -> dict[str, Any]:
    plan_row = _latest_row(paths.plans, "SELECT id,title,plan_json,updated_at FROM gym_plans WHERE is_active=1 AND COALESCE(is_archived,0)=0 ORDER BY updated_at DESC,id DESC LIMIT 1", ())
    if not plan_row:
        raise ValueError("active_plan_missing")
    try:
        plan_json = json.loads(plan_row.get("plan_json") or "{}")
    except json.JSONDecodeError as exc:
        raise ValueError("active_plan_invalid") from exc
    if str(((plan_json.get("meta") or {}).get("mode") or "")).strip().lower() != "rolling_sequence":
        raise ValueError("active_plan_is_not_rolling_sequence")
    sessions = [item for item in (plan_json.get("sequence") or []) if isinstance(item, dict) and str(item.get("kind") or "gym").lower() == "gym" and str(item.get("id") or "").strip() and str(item.get("title") or "").strip()]
    if not sessions:
        raise ValueError("rolling_sequence_has_no_gym_sessions")
    anchor = _latest_row(paths.training, "SELECT date_iso,name FROM workouts WHERE date(date_iso)<=date(?) AND TRIM(COALESCE(name,''))!='' ORDER BY date(date_iso) DESC,id DESC LIMIT 1", (date_iso,))
    if force_session_key:
        selected_index = next((index for index, item in enumerate(sessions) if str(item.get("id")) == force_session_key), None)
        selected = sessions[selected_index] if selected_index is not None else None
        if not selected:
            raise ValueError("invalid_force_session_key")
        resolver = {"source": "validated_force_session_key", "anchor": anchor}
    else:
        if not anchor:
            raise ValueError("rolling_anchor_missing")
        anchor_index = next((index for index, item in enumerate(sessions) if _session_matches(anchor.get("name"), item.get("title"))), None)
        if anchor_index is None:
            raise ValueError("rolling_anchor_not_in_active_plan")
        selected_index = (anchor_index + 1) % len(sessions)
        selected = sessions[selected_index]
        resolver = {"source": "rolling_real_workout_anchor", "anchor": anchor, "anchor_index": anchor_index}
    recovery = _latest_row(paths.hrv, "SELECT date_utc,ts_measurement,hr,rmssd,fatigue,training_motivation,sleep_quality,sickness_bool,alcohol_bool FROM hrv_measurements WHERE date(COALESCE(date_utc,ts_measurement))<=date(?) ORDER BY COALESCE(date_utc,ts_measurement) DESC LIMIT 1", (date_iso,))
    weight = _latest_row(paths.nutrition, "SELECT id,date_iso,weight_kg,created_at FROM weight_logs WHERE date_iso<=? AND weight_kg IS NOT NULL ORDER BY date_iso DESC,id DESC LIMIT 1", (date_iso,))
    existing = _latest_row(paths.core, "SELECT id,day_iso,session_name,session_type,status,context_hash,updated_at FROM training_ai_decisions WHERE day_iso=? AND status!='superseded' ORDER BY updated_at DESC,id DESC LIMIT 1", (date_iso,))
    exercises = [{"exercise": str(item.get("name") or "").strip(), "planned_sets": item.get("sets"), "target_rep_range": item.get("reps"), "variation": item.get("variation")} for item in (selected.get("items") or []) if isinstance(item, dict) and str(item.get("name") or "").strip()]
    temporal = _rolling_day_resolution(plan_json, date_iso, selected, selected_index)
    selected_session = {"session_key": str(selected["id"]), "name": str(selected["title"]), "session_type": "gym", "exercises": exercises, "session_relation": temporal["session_relation"], "is_today_session": temporal["is_today_session"]}
    temporal["next_session"] = {**(temporal["next_session"] or {}), "exercises": exercises}
    if temporal["today_session"] is not None:
        temporal["today_session"] = {**temporal["today_session"], "exercises": exercises}
    context = {"date_iso": date_iso, "plan": {"id": plan_row["id"], "title": plan_row.get("title"), "updated_at": plan_row.get("updated_at"), "mode": "rolling_sequence"}, "selected_session": selected_session, "temporal_resolution": temporal, "resolver": resolver, "recovery": recovery, "weight": weight, "subjective_input": subjective_input or {}, "constraints": constraints or None}
    context_hash = _hash(context)
    warnings = []
    if recovery is None: warnings.append("recovery_missing")
    if weight is None: warnings.append("weight_missing")
    return {"context": context, "context_hash": context_hash, "data_quality": "partial" if warnings else "ok", "warnings": warnings, "missing_sources": warnings, "existing_decision": existing}


def plan_training_decision(context_result: dict[str, Any], mode: str = "dry_run") -> dict[str, Any]:
    context = context_result["context"]
    selected = context["selected_session"]
    items = [{"exercise": item["exercise"], "action": "normal", "badge": "normal", "note": "Plan unverändert; keine Satz- oder Lastvorgaben erzeugt.", "sets": []} for item in selected["exercises"]]
    decision = {"date": context["date_iso"], "session": {"name": selected["name"], "session_key": selected["session_key"], "plan_id": context["plan"]["id"], "session_type": "gym", "decision": "normal", "headline": f"{selected['name']} · kontrolliert", "summary_note": "Rolling-Session aus realem Workout-Anker; keine Progression ohne explizite Datenentscheidung.", "global_rule": "Plan ausführen, keine erfundenen Satz- oder Lastwerte."}, "items": items}
    temporal = context["temporal_resolution"]
    return {"no_write": True, "mode": mode, "selected_session": selected, **temporal, "decision": decision, "context_hash": context_result["context_hash"], "dashboard_card_payload": {"status": "fresh", "title": f"Nächste geplante Einheit · {selected['name']}", "headline": decision["session"]["headline"], "items": items}, "warnings": list(context_result.get("warnings") or [])}


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute("CREATE TABLE IF NOT EXISTS training_ai_decisions (id INTEGER PRIMARY KEY AUTOINCREMENT, day_iso TEXT NOT NULL, planned_session_id INTEGER, plan_id INTEGER, session_name TEXT, session_type TEXT, status TEXT NOT NULL DEFAULT 'fresh', source TEXT NOT NULL, context_hash TEXT, decision_json TEXT NOT NULL, trigger_reason TEXT NOT NULL DEFAULT 'none', created_at TEXT NOT NULL, updated_at TEXT NOT NULL, superseded_at TEXT)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_training_ai_decisions_day_status ON training_ai_decisions(day_iso,status)")


def write_training_decision(paths: DecisionPaths, context_result: dict[str, Any], plan: dict[str, Any], reason: str, idempotency_key: str | None = None) -> dict[str, Any]:
    if not reason.strip(): raise ValueError("reason_required")
    if plan.get("session_relation") != "today" or plan.get("resolution_status") != "resolved":
        raise ValueError("requested_date_has_no_resolved_training_session")
    target = paths.core_write or paths.core
    now = datetime.now(timezone.utc).isoformat()
    decision = plan["decision"]; session = decision["session"]; day = context_result["context"]["date_iso"]
    if paths.audit:
        with _connect(paths.audit, write=True) as audit:
            audit.execute("CREATE TABLE IF NOT EXISTS training_decision_audit (id INTEGER PRIMARY KEY AUTOINCREMENT, idempotency_key TEXT UNIQUE, decision_id INTEGER, day_iso TEXT NOT NULL, reason TEXT NOT NULL, context_hash TEXT NOT NULL, created_at TEXT NOT NULL)")
            if idempotency_key:
                replay = audit.execute("SELECT id,decision_id,context_hash FROM training_decision_audit WHERE idempotency_key=?", (idempotency_key,)).fetchone()
                if replay:
                    if str(replay["context_hash"] or "") != context_result["context_hash"]:
                        raise ValueError("idempotency_key_context_conflict")
                    return {"status": "duplicate", "changed": False, "replayed": True, "decision_id": int(replay["decision_id"]), "context_hash": context_result["context_hash"], "affected_ids": [int(replay["decision_id"])], "readback": read_training_decision_for_dashboard(paths, day), "audit_id": int(replay["id"])}
    with _connect(target, write=True) as conn:
        _ensure_schema(conn)
        existing = conn.execute("SELECT id,context_hash,session_name,decision_json FROM training_ai_decisions WHERE day_iso=? AND status!='superseded' ORDER BY updated_at DESC,id DESC LIMIT 1", (day,)).fetchone()
        if existing and str(existing["context_hash"] or "") == context_result["context_hash"] and str(existing["session_name"] or "") == session["name"]:
            return {"status": "duplicate", "changed": False, "replayed": False, "decision_id": int(existing["id"]), "context_hash": context_result["context_hash"], "affected_ids": [int(existing["id"])], "readback": read_training_decision_for_dashboard(paths, day), "audit_id": None}
        conn.execute("UPDATE training_ai_decisions SET status='superseded',superseded_at=?,updated_at=? WHERE day_iso=? AND status IN ('fresh','stale','failed')", (now,now,day))
        cur = conn.execute("INSERT INTO training_ai_decisions(day_iso,plan_id,session_name,session_type,status,source,context_hash,decision_json,trigger_reason,created_at,updated_at) VALUES (?,?,?,?, 'fresh','liva_mcp',? ,?,'none',?,?)", (day, context_result["context"]["plan"]["id"], session["name"], "gym", context_result["context_hash"], json.dumps(decision,ensure_ascii=False), now, now))
        decision_id = int(cur.lastrowid)
    audit_id = None
    if paths.audit:
        with _connect(paths.audit, write=True) as audit:
            audit.execute("CREATE TABLE IF NOT EXISTS training_decision_audit (id INTEGER PRIMARY KEY AUTOINCREMENT, idempotency_key TEXT UNIQUE, decision_id INTEGER, day_iso TEXT NOT NULL, reason TEXT NOT NULL, context_hash TEXT NOT NULL, created_at TEXT NOT NULL)")
            cur = audit.execute("INSERT INTO training_decision_audit(idempotency_key,decision_id,day_iso,reason,context_hash,created_at) VALUES (?,?,?,?,?,?)", (idempotency_key, decision_id, day, reason, context_result["context_hash"], now))
            audit_id = int(cur.lastrowid)
    return {"status": "success", "changed": True, "replayed": False, "decision_id": decision_id, "context_hash": context_result["context_hash"], "written_at": now, "affected_ids": [decision_id], "readback": read_training_decision_for_dashboard(paths, day), "audit_id": audit_id}


def read_training_decision_for_dashboard(paths: DecisionPaths, date_iso: str) -> dict[str, Any]:
    row = _latest_row(paths.core, "SELECT id,day_iso,session_name,session_type,status,source,context_hash,decision_json,created_at,updated_at FROM training_ai_decisions WHERE day_iso=? AND status!='superseded' ORDER BY updated_at DESC,id DESC LIMIT 1", (date_iso,))
    if row is None and paths.core_write:
        row = _latest_row(paths.core_write, "SELECT id,day_iso,session_name,session_type,status,source,context_hash,decision_json,created_at,updated_at FROM training_ai_decisions WHERE day_iso=? AND status!='superseded' ORDER BY updated_at DESC,id DESC LIMIT 1", (date_iso,))
    if not row: return {"dashboard_status": "missing", "decision_id": None, "visible_in_frontend": False, "card_payload": None}
    try: decision = json.loads(row.get("decision_json") or "{}")
    except json.JSONDecodeError: decision = {}
    session = decision.get("session") if isinstance(decision,dict) else {}
    items = decision.get("items") if isinstance(decision,dict) and isinstance(decision.get("items"),list) else []
    return {"dashboard_status": "fresh" if row.get("status") == "fresh" else row.get("status"), "decision_id": row.get("id"), "context_hash": row.get("context_hash"), "session_name": row.get("session_name") or session.get("name"), "session_key": session.get("session_key"), "visible_in_frontend": bool(row.get("status") == "fresh"), "card_payload": {"status": row.get("status"), "title": f"Nächste geplante Einheit · {row.get('session_name') or session.get('name') or '—'}", "headline": session.get("headline") or row.get("session_name") or "Training-Entscheidung", "items": items}}
