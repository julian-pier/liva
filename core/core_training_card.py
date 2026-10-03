from __future__ import annotations

import json
import math
import sqlite3
import sys
import time
from typing import Any

from database.connections import get_core_db, get_training_db
from database.connections import get_hrv_db
from integrations.endurance_approval import build_endurance_core_context

from core.core_daily_board import (
    enrich_core_board,
    _columns,
    _data_status,
    _event_exercises,
    _json_dumps,
    _json_loads,
    _latest_training_session,
    _parse_day,
    _plan_day,
    _recovery_status,
    _row_to_board,
    _table_exists,
    _text,
    _utc_iso,
    _weight_status,
    ensure_core_daily_boards_schema,
    get_core_daily_board,
)


CARD_JSON_DEFAULTS = {"card_json": {}, "data_quality_json": {}, "source_context_json": {}}
INTENT_DEFAULTS = {
    "full_send": {"max_rpe_main": 9.0, "max_rpe_accessory": 8.5, "volume_factor": 1.0, "allow_bonus_sets": False},
    "train_normal": {"max_rpe_main": 8.5, "max_rpe_accessory": 8.0, "volume_factor": 1.0, "allow_bonus_sets": False},
    "train_controlled": {"max_rpe_main": 8.0, "max_rpe_accessory": 8.0, "volume_factor": 1.0, "allow_bonus_sets": False},
    "reduce_volume": {"max_rpe_main": 7.5, "max_rpe_accessory": 7.5, "volume_factor": 0.7, "allow_bonus_sets": False},
    "technique_only": {"max_rpe_main": 7.0, "max_rpe_accessory": 6.5, "volume_factor": 0.65, "allow_bonus_sets": False},
    "recovery_only": {"max_rpe_main": None, "max_rpe_accessory": None, "volume_factor": 0.0, "allow_bonus_sets": False},
    "hard_stop": {"max_rpe_main": None, "max_rpe_accessory": None, "volume_factor": 0.0, "allow_bonus_sets": False},
    "unknown": {"max_rpe_main": 8.0, "max_rpe_accessory": 8.0, "volume_factor": 1.0, "allow_bonus_sets": False},
}
ALLOWED_DECISION_INTENTS = [
    "full_send",
    "train_normal",
    "train_controlled",
    "reduce_volume",
    "technique_only",
    "recovery_only",
    "hard_stop",
]


def _legacy_mode_from_intent(intent: str) -> str:
    mapping = {
        "full_send": "HEAVY",
        "train_normal": "NORMAL",
        "train_controlled": "LIGHT",
        "reduce_volume": "LIGHT",
        "technique_only": "LIGHT",
        "recovery_only": "REST",
        "hard_stop": "REST",
        "unknown": "NORMAL",
    }
    return mapping.get(str(intent or "").strip(), "NORMAL")


def _legacy_card_type(session_type: str | None, card: dict[str, Any]) -> str:
    raw = str(session_type or card.get("session_type") or "").strip().lower()
    mapping = {
        "run": "cardio",
        "ergo": "cardio",
        "gym": "training",
        "recovery": "recovery",
        "hard_stop": "recovery",
        "unknown": "training",
    }
    return mapping.get(raw, "training")


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    if column not in _columns(conn, table):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def ensure_core_training_card_schema(conn: sqlite3.Connection | None = None) -> None:
    own_conn = conn is None
    db = conn or get_core_db()
    try:
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS core_training_cards (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                mode TEXT,
                card_type TEXT,
                payload_json TEXT,
                created_at TEXT
            )
            """
        )
        _ensure_column(db, "core_training_cards", "day_iso", "TEXT")
        _ensure_column(db, "core_training_cards", "board_id", "INTEGER")
        _ensure_column(db, "core_training_cards", "updated_at", "TEXT")
        _ensure_column(db, "core_training_cards", "source", "TEXT NOT NULL DEFAULT 'core_board'")
        _ensure_column(db, "core_training_cards", "status", "TEXT NOT NULL DEFAULT 'active'")
        _ensure_column(db, "core_training_cards", "session_type", "TEXT")
        _ensure_column(db, "core_training_cards", "session_label", "TEXT")
        _ensure_column(db, "core_training_cards", "decision_intent", "TEXT")
        _ensure_column(db, "core_training_cards", "title", "TEXT")
        _ensure_column(db, "core_training_cards", "summary", "TEXT")
        _ensure_column(db, "core_training_cards", "card_json", "TEXT NOT NULL DEFAULT '{}'")
        _ensure_column(db, "core_training_cards", "data_quality_json", "TEXT NOT NULL DEFAULT '{}'")
        _ensure_column(db, "core_training_cards", "source_context_json", "TEXT NOT NULL DEFAULT '{}'")
        _ensure_column(db, "core_training_cards", "user_feedback", "TEXT")
        _ensure_column(db, "core_training_cards", "user_feedback_at", "TEXT")
        now = _utc_iso()
        db.execute(f"UPDATE core_training_cards SET created_at=COALESCE(NULLIF(TRIM(created_at), ''), '{now}')")
        db.execute("UPDATE core_training_cards SET payload_json=COALESCE(NULLIF(TRIM(payload_json), ''), '{}')")
        db.execute("UPDATE core_training_cards SET card_json=COALESCE(NULLIF(TRIM(card_json), ''), COALESCE(NULLIF(TRIM(payload_json), ''), '{}'))")
        db.execute("UPDATE core_training_cards SET source=COALESCE(source, 'core_board') WHERE source IS NULL OR TRIM(source)=''")
        db.execute("UPDATE core_training_cards SET status=COALESCE(status, 'active') WHERE status IS NULL OR TRIM(status)=''")
        db.execute("UPDATE core_training_cards SET updated_at=COALESCE(updated_at, created_at) WHERE updated_at IS NULL OR TRIM(updated_at)=''")
        db.execute(
            """
            UPDATE core_training_cards
            SET mode = CASE
                WHEN decision_intent='full_send' THEN 'HEAVY'
                WHEN decision_intent IN ('train_controlled', 'reduce_volume', 'technique_only') THEN 'LIGHT'
                WHEN decision_intent IN ('recovery_only', 'hard_stop') THEN 'REST'
                ELSE COALESCE(NULLIF(TRIM(mode), ''), 'NORMAL')
            END
            WHERE mode IS NULL OR TRIM(mode)=''
            """
        )
        db.execute(
            """
            UPDATE core_training_cards
            SET card_type = CASE
                WHEN LOWER(COALESCE(session_type, '')) IN ('run', 'ergo') THEN 'cardio'
                WHEN LOWER(COALESCE(session_type, '')) IN ('recovery', 'hard_stop') THEN 'recovery'
                ELSE COALESCE(NULLIF(TRIM(card_type), ''), 'training')
            END
            WHERE card_type IS NULL OR TRIM(card_type)=''
            """
        )
        db.execute(
            "CREATE INDEX IF NOT EXISTS idx_core_training_cards_day_status_source ON core_training_cards(day_iso, status, source)"
        )
        db.execute("CREATE INDEX IF NOT EXISTS idx_core_training_cards_updated ON core_training_cards(updated_at DESC)")
        if own_conn:
            db.commit()
    finally:
        if own_conn:
            db.close()


def _row_to_training_card(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    data = dict(row)
    card = _json_loads(data.get("card_json"), {})
    if not isinstance(card, dict) or not card:
        card = _json_loads(data.get("payload_json"), {})
    out = {
        "id": data.get("id"),
        "day_iso": data.get("day_iso"),
        "date": data.get("day_iso"),
        "board_id": data.get("board_id"),
        "created_at": data.get("created_at"),
        "updated_at": data.get("updated_at") or data.get("created_at"),
        "source": data.get("source") or "core_board",
        "status": data.get("status") or "active",
        "session_type": data.get("session_type"),
        "session_label": data.get("session_label"),
        "decision_intent": data.get("decision_intent"),
        "title": data.get("title"),
        "summary": data.get("summary"),
        "data_quality": _json_loads(data.get("data_quality_json"), {}),
        "source_context": _json_loads(data.get("source_context_json"), {}),
        "user_feedback": data.get("user_feedback"),
        "user_feedback_at": data.get("user_feedback_at"),
    }
    if isinstance(card, dict):
        out.update(card)
    out.setdefault("title", data.get("title"))
    out.setdefault("summary", data.get("summary"))
    out.setdefault("session_type", data.get("session_type"))
    out.setdefault("session_label", data.get("session_label"))
    out.setdefault("decision_intent", data.get("decision_intent"))
    out.setdefault("items", [])
    out.setdefault("footer_note", "")
    out.setdefault("source", data.get("source") or "core_board")
    out.setdefault("board_id", data.get("board_id"))
    out["data_quality"] = _json_loads(data.get("data_quality_json"), {})
    out["source_context"] = _json_loads(data.get("source_context_json"), {})
    return _normalize_training_card_payload(out)


def _get_board_by_id(board_id: Any) -> dict[str, Any] | None:
    try:
        board_id = int(board_id)
    except Exception:
        return None
    conn = get_core_db()
    try:
        ensure_core_daily_boards_schema(conn)
        row = conn.execute("SELECT * FROM core_daily_boards WHERE id=? LIMIT 1", (board_id,)).fetchone()
        return _row_to_board(row) if row else None
    finally:
        conn.close()


def _peek_board_read_only(day_iso: str) -> dict[str, Any] | None:
    conn = get_core_db()
    try:
        if not _table_exists(conn, "core_daily_boards"):
            return None
        row = conn.execute(
            "SELECT * FROM core_daily_boards WHERE day_iso=? AND status='active' ORDER BY updated_at DESC, id DESC LIMIT 1",
            (day_iso,),
        ).fetchone()
        return _row_to_board(row) if row else None
    finally:
        conn.close()


def _peek_training_card_read_only(day_iso: str) -> dict[str, Any] | None:
    conn = get_core_db()
    try:
        if not _table_exists(conn, "core_training_cards"):
            return None
        row = conn.execute(
            """
            SELECT * FROM core_training_cards
            WHERE day_iso=? AND status='active' AND source='core_board'
            ORDER BY updated_at DESC, id DESC
            LIMIT 1
            """,
            (day_iso,),
        ).fetchone()
        return _row_to_training_card(row) if row else None
    finally:
        conn.close()


def get_persisted_core_training_card(day_iso: str, board_id: int | None = None) -> dict[str, Any] | None:
    day = _parse_day(day_iso)
    conn = get_core_db()
    try:
        ensure_core_training_card_schema(conn)
        if board_id:
            row = conn.execute(
                """
                SELECT * FROM core_training_cards
                WHERE day_iso=? AND status='active' AND source='core_board' AND board_id=?
                ORDER BY updated_at DESC, id DESC
                LIMIT 1
                """,
                (day, int(board_id)),
            ).fetchone()
        else:
            row = conn.execute(
                """
                SELECT * FROM core_training_cards
                WHERE day_iso=? AND status='active' AND source='core_board'
                ORDER BY updated_at DESC, id DESC
                LIMIT 1
                """,
                (day,),
            ).fetchone()
        return _row_to_training_card(row) if row else None
    finally:
        conn.close()


def _latest_recovery_snapshot() -> dict[str, Any]:
    base = _recovery_status()
    snapshot = {
        "hrv": base.get("latest_rmssd"),
        "resting_hr": base.get("latest_hr"),
        "sleep_quality": None,
        "status": "usable" if base.get("status") in {"fresh", "stale", "partial"} else "missing",
    }
    try:
        conn = get_hrv_db()
    except Exception:
        return snapshot
    try:
        if not _table_exists(conn, "hrv_measurements"):
            return snapshot
        cols = _columns(conn, "hrv_measurements")
        if "sleep_quality" not in cols:
            return snapshot
        row = conn.execute("SELECT sleep_quality FROM hrv_measurements ORDER BY ts_measurement DESC LIMIT 1").fetchone()
        if row:
            try:
                snapshot["sleep_quality"] = float(row["sleep_quality"]) if row["sleep_quality"] not in (None, "") else None
            except Exception:
                snapshot["sleep_quality"] = None
        return snapshot
    finally:
        conn.close()


def _morning_data_readiness(day_iso: str) -> tuple[dict[str, Any], list[str]]:
    plan_day = _plan_day(day_iso)
    status = _data_status(day_iso, plan_day)
    domains = status.get("domains") if isinstance(status.get("domains"), dict) else {}
    readiness = {
        "overall": "ready" if status.get("status") == "ok" else "partial" if status.get("status") == "partial" else "missing",
        "weight": (domains.get("weight") or {}).get("status") or "missing",
        "recovery": (domains.get("recovery") or {}).get("status") or "missing",
        "training_log": (domains.get("training_log") or {}).get("status") or "missing",
        "nutrition": (domains.get("nutrition") or {}).get("status") or "missing",
        "calendar": "unknown",
    }
    warnings: list[str] = []
    if not plan_day.get("available"):
        warnings.append("Keine klare heutige Einheit gefunden.")
    return readiness, warnings


def build_core_morning_context(day_iso: str | None = None) -> dict[str, Any]:
    day = _parse_day(day_iso)
    plan_day = _plan_day(day)
    board = _peek_board_read_only(day)
    card = _peek_training_card_read_only(day)
    session_context = _resolved_session_context(day, plan_day, board or {})
    plan_available = bool(plan_day.get("available"))
    planned_session_label = _text(session_context.get("planned_session_label"), 120) if plan_available else None
    recommended_session_label = _text(session_context.get("recommended_session_label"), 120) if plan_available else None
    must_use_session_label = recommended_session_label if plan_available else None
    session_type = _text(session_context.get("recommended_session_type"), 40)
    readiness, warnings = _morning_data_readiness(day)
    if not must_use_session_label:
        warnings.append("Keine klare heutige Einheit gefunden.")
    latest_weight = _weight_status()
    latest_training = _latest_training_session() or {}
    endurance = build_endurance_core_context(day, refresh=True)
    payload = {
        "must_use_session_label": must_use_session_label,
        "session_type": session_type,
        "planned_session_label": planned_session_label,
        "recommended_session_label": recommended_session_label,
        "planned_session_source": _text(session_context.get("plan_source") or session_context.get("session_detection_source"), 80),
        "training_card_exists": bool(card),
        "board_exists": bool(board),
        "data_readiness": readiness,
        "recovery_snapshot": _latest_recovery_snapshot(),
        "weight_snapshot": {
            "weight_kg": latest_weight.get("latest_weight"),
            "status": latest_weight.get("status") or "missing",
        },
        "training_context": {
            "last_training": _text(latest_training.get("name"), 120),
            "last_training_date": _text(latest_training.get("date_iso"), 40),
            "today_plan": planned_session_label,
            "local_fatigue_notes": [],
        },
        "decision_contract": {
            "do_not_invent_session": True,
            "use_must_use_session_label": True,
            "do_not_output_weights": True,
            "do_not_output_sets_reps": True,
            "build_training_card": True,
            "include_endurance_approval": True,
            "do_not_confuse_original_run_with_ergo_execution": True,
            "allowed_decision_intents": list(ALLOWED_DECISION_INTENTS),
        },
        "suggested_payload_skeleton": {
            "trigger": "user_morning_checkin",
            "human_headline": f"{must_use_session_label or 'Heutige Einheit'} sauber ausführen, aber nicht drauflegen." if must_use_session_label else "Heutige Einheit sauber ausführen.",
            "decision_intent": "train_controlled",
            "build_training_card": True,
            "persist": True,
            "machine_briefing": {
                "endurance_approval": {
                    "available": bool(endurance.get("available")),
                    "has_workout": bool(endurance.get("has_workout")),
                    "original_label": endurance.get("original_label") or ((endurance.get("original") or {}).get("label")),
                    "execution_mode": endurance.get("execution_mode") or ((endurance.get("liva") or {}).get("execution_mode")),
                    "final_summary": endurance.get("final_summary") or ((endurance.get("liva") or {}).get("final_summary")),
                    "status": endurance.get("status") or ((endurance.get("liva") or {}).get("status")),
                    "reason": endurance.get("reason") or ((endurance.get("liva") or {}).get("reason")),
                    "conflicts": endurance.get("conflicts") or ((endurance.get("liva") or {}).get("conflicts")) or [],
                }
            },
        },
        "endurance_approval": {
            "available": bool(endurance.get("available")),
            "connected": bool(endurance.get("connected")),
            "reachable": bool(endurance.get("reachable")),
            "has_workout": bool(endurance.get("has_workout")),
            "original_label": endurance.get("original_label") or ((endurance.get("original") or {}).get("label")),
            "execution_mode": endurance.get("execution_mode") or ((endurance.get("liva") or {}).get("execution_mode")),
            "final_summary": endurance.get("final_summary") or ((endurance.get("liva") or {}).get("final_summary")),
            "status": endurance.get("status") or ((endurance.get("liva") or {}).get("status")) or "UNKNOWN",
            "reason": endurance.get("reason") or ((endurance.get("liva") or {}).get("reason")),
            "conflicts": endurance.get("conflicts") or ((endurance.get("liva") or {}).get("conflicts")) or [],
            "gpt_instruction": endurance.get("gpt_instruction") or "Bei Morning-Checkin diese Ausdauer-Freigabe berücksichtigen. Originalplan nicht mit Durchführung verwechseln.",
        },
    }
    if card:
        payload["training_card_title"] = _text(card.get("title"), 160)
    if warnings:
        payload["warnings"] = list(dict.fromkeys(str(item) for item in warnings if str(item).strip()))
    endurance_status = str(((payload.get("endurance_approval") or {}).get("status")) or "").upper()
    if endurance_status in {"YELLOW", "RED"} and payload["endurance_approval"].get("reason"):
        payload["endurance_warning"] = payload["endurance_approval"]["reason"]
    return {"ok": True, "date": day, "morning_context": payload}


def get_core_training_card(day_iso: str, board_id: int | None = None) -> dict[str, Any] | None:
    card = get_persisted_core_training_card(day_iso, board_id=board_id)
    if not card:
        return None
    day = _parse_day(day_iso)
    try:
        board = _get_board_by_id(board_id) if board_id else get_core_daily_board(day, create_if_missing=False)
        if isinstance(board, dict) and board:
            board = enrich_core_board(board, training_card=card)
            card["board_consistency"] = board.get("board_consistency")
            card["legacy_core_status"] = board.get("legacy_core_status")
        return card
    except Exception:
        return card


def _briefing_constraints(board: dict[str, Any]) -> dict[str, Any]:
    machine = board.get("machine_briefing") if isinstance(board.get("machine_briefing"), dict) else {}
    constraints = machine.get("constraints") if isinstance(machine.get("constraints"), dict) else {}
    training_bias = machine.get("training_bias") if isinstance(machine.get("training_bias"), dict) else {}
    return {"constraints": constraints, "training_bias": training_bias}


def _first_plan_event(plan_day: dict[str, Any]) -> dict[str, Any]:
    events = plan_day.get("events") if isinstance(plan_day.get("events"), list) else []
    return events[0] if events else {}


def _token_match(text: str, tokens: tuple[str, ...]) -> bool:
    lowered = str(text or "").strip().lower()
    return any(token in lowered for token in tokens)


RUN_TOKENS = ("run", "lauf", "laufen", "jog", "pace")
ERGO_TOKENS = ("ergo", "bike", "rad", "fahrrad", "watt")


def _label_type(label: str, kind: str | None = None) -> str:
    text = str(label or "").strip().lower()
    kind_lower = str(kind or "").strip().lower()
    if _token_match(text, ERGO_TOKENS):
        return "ergo"
    if _token_match(text, RUN_TOKENS):
        return "run"
    if "z2" in text:
        if kind_lower == "run":
            return "run"
        if kind_lower == "ergo" and _token_match(text, ERGO_TOKENS):
            return "ergo"
        return "run"
    if kind_lower in {"run", "ergo"}:
        return kind_lower
    return "unknown"


def _normalize_session_label(label: str, session_type: str) -> str:
    text = _text(label, 120) or ""
    if not text:
        return {
            "run": "Run Z2",
            "ergo": "Ergo Z2",
            "recovery": "Recovery",
            "hard_stop": "Hard Stop",
            "gym": "Training",
        }.get(session_type, "Training")
    lowered = text.lower()
    if session_type == "run" and ("z2 run" in lowered or "run z2" in lowered):
        return "Run Z2"
    if session_type == "ergo" and ("z2 ergo" in lowered or "ergo z2" in lowered):
        return "Ergo Z2"
    if session_type == "run" and "run" not in lowered and "lauf" not in lowered:
        if "z2" in lowered:
            return "Run Z2"
        return f"Run {text}".strip()
    if session_type == "ergo" and "ergo" not in lowered and "bike" not in lowered and "rad" not in lowered:
        if "z2" in lowered:
            return "Ergo Z2"
        return f"Ergo {text}".strip()
    return text


def _next_session_payload(day_iso: str) -> dict[str, Any] | None:
    app_mod = sys.modules.get("app")
    func = getattr(app_mod, "_core_decision_call_next_session", None) if app_mod is not None else None
    if callable(func):
        try:
            payload = func(day_iso)
            if isinstance(payload, dict) and payload.get("ok") is True:
                return payload
        except Exception:
            pass
    return None


def _planned_session_from_next_session(day_iso: str) -> dict[str, Any]:
    payload = _next_session_payload(day_iso) or {}
    session = payload.get("session") if isinstance(payload.get("session"), dict) else {}
    session_name = _text(session.get("session_name"), 120) or _text(session.get("title"), 120)
    items = payload.get("items") if isinstance(payload.get("items"), list) else []
    item = items[0] if items else {}
    inline = _text(item.get("suggestion_inline"), 240)
    duration_target = None
    hr_target = None
    if isinstance(item.get("planned"), dict):
        run_plan = item["planned"].get("run") if isinstance(item["planned"].get("run"), dict) else {}
        if run_plan.get("duration_min") not in (None, ""):
            duration_target = f"{int(run_plan['duration_min'])} min"
        if run_plan.get("target_hr_bpm") not in (None, ""):
            hr_target = f"Ziel: {int(run_plan['target_hr_bpm'])} bpm"
    planned_type = _label_type(session_name or "", str(item.get("type") or ""))
    return {
        "available": bool(session_name),
        "label": session_name,
        "type": planned_type,
        "inline": inline,
        "duration_target": duration_target,
        "hr_target": hr_target,
        "source": "next_session" if session_name else None,
    }


def _machine_session_context(board: dict[str, Any]) -> dict[str, Any]:
    machine = board.get("machine_briefing") if isinstance(board.get("machine_briefing"), dict) else {}
    block = machine.get("training_session") if isinstance(machine.get("training_session"), dict) else {}
    planned = machine.get("planned_session") if isinstance(machine.get("planned_session"), dict) else {}
    recommended = machine.get("recommended_session") if isinstance(machine.get("recommended_session"), dict) else {}
    planned_raw = machine.get("planned_session")
    recommended_raw = machine.get("recommended_session")
    planned_label = _text(
        block.get("planned_session_label")
        or planned.get("label")
        or (planned_raw if isinstance(planned_raw, str) else None)
        or machine.get("planned_session_label"),
        120,
    )
    planned_type = _text(
        block.get("planned_session_type")
        or planned.get("type")
        or machine.get("planned_session_type"),
        40,
    )
    recommended_label = _text(
        block.get("recommended_session_label")
        or recommended.get("label")
        or (recommended_raw if isinstance(recommended_raw, str) else None)
        or machine.get("recommended_session_label"),
        120,
    )
    recommended_type = _text(
        block.get("recommended_session_type")
        or recommended.get("type")
        or machine.get("recommended_session_type")
        or machine.get("session_type")
        or machine.get("recommended_type"),
        40,
    )
    adaptation_note = _text(
        block.get("adaptation_note")
        or machine.get("adaptation_note")
        or machine.get("adaptation"),
        180,
    )
    return {
        "planned_label": planned_label,
        "planned_type": planned_type,
        "recommended_label": recommended_label,
        "recommended_type": recommended_type,
        "adaptation_note": adaptation_note,
    }


def _resolve_plan_session(plan_title: str, plan_kind: str, next_session: dict[str, Any]) -> tuple[str, str, str]:
    next_label = _text(next_session.get("label"), 120)
    next_type = _text(next_session.get("type"), 40)
    if next_session.get("available") and next_label:
        label = _normalize_session_label(next_label, next_type or _label_type(next_label, plan_kind))
        session_type = _label_type(label, next_type or plan_kind)
        if session_type == "unknown":
            session_type = "run" if "z2" in str(label).lower() else _label_type(plan_title, plan_kind)
        return label, session_type, "next_session"

    base_type = _label_type(plan_title, plan_kind)
    base_label = _normalize_session_label(plan_title, base_type if base_type != "unknown" else "unknown")
    if base_type == "unknown" and plan_kind == "ergo" and _token_match(plan_title, ERGO_TOKENS):
        base_type = "ergo"
    if base_type == "unknown" and plan_kind == "run":
        base_type = "run"
    if base_type == "unknown" and "z2" in str(plan_title).lower():
        base_type = "run"
        base_label = "Run Z2"
    return base_label, base_type, "plan_label"


def _machine_requests_adaptation(planned_label: str, planned_type: str, machine_session: dict[str, Any]) -> bool:
    planned_machine_label = _text(machine_session.get("planned_label"), 120)
    planned_machine_type = _text(machine_session.get("planned_type"), 40)
    recommended_label = _text(machine_session.get("recommended_label"), 120)
    recommended_type = _text(machine_session.get("recommended_type"), 40)
    if not (recommended_label or recommended_type):
        return False
    if recommended_label and planned_label and recommended_label.strip().lower() != planned_label.strip().lower():
        return True
    if recommended_type and planned_type and recommended_type.strip().lower() != planned_type.strip().lower():
        return True
    if planned_machine_label and planned_label and planned_machine_label.strip().lower() != planned_label.strip().lower():
        return True
    if planned_machine_type and planned_type and planned_machine_type.strip().lower() != planned_type.strip().lower():
        return True
    return False


def _resolved_session_context(day_iso: str, plan_day: dict[str, Any], board: dict[str, Any]) -> dict[str, Any]:
    first = _first_plan_event(plan_day)
    plan_title = _text(first.get("title"), 120) or "Training"
    plan_kind = _text(first.get("kind"), 40) or ""
    next_session = _planned_session_from_next_session(day_iso)
    machine_session = _machine_session_context(board)

    # The plan row for the requested date is authoritative.  A process-global
    # next-session snapshot can describe another (usually current) date and
    # must only serve as fallback when the plan contains no event at all.
    planned_label, planned_type, planned_detection_source = _resolve_plan_session(
        plan_title,
        plan_kind,
        {} if first else next_session,
    )
    if planned_type == "unknown" and first:
        planned_type = "gym"

    recommended_label = planned_label
    recommended_type = planned_type
    detection_source = planned_detection_source

    machine_planned_label = _text(machine_session.get("planned_label"), 120)
    machine_planned_type = _text(machine_session.get("planned_type"), 40)
    if machine_planned_label and not next_session.get("available"):
        planned_label = _normalize_session_label(machine_planned_label, machine_planned_type or planned_type)
        if machine_planned_type:
            planned_type = machine_planned_type
            detection_source = "machine_briefing"
    elif machine_planned_type and not next_session.get("available"):
        planned_type = machine_planned_type
        planned_label = _normalize_session_label(planned_label, planned_type)
        detection_source = "machine_briefing"

    if _machine_requests_adaptation(planned_label, planned_type, machine_session):
        recommended_label = _normalize_session_label(
            machine_session.get("recommended_label") or planned_label,
            machine_session.get("recommended_type") or planned_type,
        )
        recommended_type = _text(machine_session.get("recommended_type"), 40) or _label_type(recommended_label or "", "") or planned_type
        detection_source = "machine_briefing"
    else:
        recommended_label = _normalize_session_label(recommended_label, recommended_type)

    planned_label = _normalize_session_label(planned_label, planned_type)
    recommended_label = _normalize_session_label(recommended_label, recommended_type)
    adapted = (planned_type != recommended_type) or (str(planned_label).strip().lower() != str(recommended_label).strip().lower())
    adaptation_note = machine_session.get("adaptation_note")
    if adapted and not adaptation_note:
        adaptation_note = f"CORE-Anpassung: {planned_label} → {recommended_label}"

    intent = str(board.get("decision_intent") or "").strip().lower()
    if not first and intent == "recovery_only":
        recommended_type = "recovery"
        recommended_label = "Recovery"
        planned_type = planned_type if planned_type != "unknown" else "recovery"
        adapted = adapted or (planned_type != recommended_type)
    if not first and intent == "hard_stop":
        recommended_type = "hard_stop"
        recommended_label = "Hard Stop"
        adapted = adapted or (planned_type != recommended_type)

    return {
        "planned_session_label": planned_label,
        "planned_session_type": planned_type,
        "recommended_session_label": recommended_label,
        "recommended_session_type": recommended_type,
        "adapted_from_plan": bool(adapted),
        "adaptation_note": adaptation_note if adapted else None,
        "session_detection_source": detection_source,
        "plan_source": plan_day.get("source"),
        "plan_day_key": plan_day.get("day_key"),
        "plan_event_title": plan_title,
        "plan_event_kind": plan_kind,
        "next_session_inline": next_session.get("inline"),
        "next_session_duration_target": next_session.get("duration_target"),
        "next_session_hr_target": next_session.get("hr_target"),
    }


def _rpe_text(value: Any) -> str:
    try:
        val = float(value)
    except Exception:
        return "—"
    return str(int(val)) if abs(val - int(val)) < 0.01 else f"{val:.1f}"


def _weight_text(value: Any) -> str | None:
    try:
        val = float(value)
    except Exception:
        return None
    if val <= 0:
        return None
    return f"{int(val)} kg" if abs(val - int(val)) < 0.01 else f"{val:.1f} kg"


def _contains_generic_working_weight_text(value: Any) -> bool:
    text = str(value or "").strip().lower()
    return "letztes gutes arbeitsgewicht" in text or "last_good_working_weight" in text


def _norm_machine_text(value: Any) -> str:
    return str(value or "").strip().lower()


def _device_rounding_step(name: str, device: str | None) -> float | None:
    text = f"{_norm_machine_text(name)} {_norm_machine_text(device)}"
    if any(token in text for token in ("kurzhantel", "dumbbell", " db ", " kh ")):
        return None
    if any(token in text for token in ("smith", "lh", "langhantel", "barbell")):
        return 2.5
    if any(token in text for token in ("egym", "seilzug", "kabel", "cable", "wadenheben", "calf", "sz")):
        return 1.0
    return 2.5


_KH_ALLOWED_LOADS = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 11.5, 14.0, 16.5, 19.0, 22.0, 25.0, 27.5, 30.0, 32.5]


def _round_weight_for_device(value: Any, name: str, device: str | None, *, direction: str = "nearest") -> float | None:
    try:
        numeric = float(value)
    except Exception:
        return None
    if numeric <= 0:
        return None
    text = f"{_norm_machine_text(name)} {_norm_machine_text(device)}"
    if any(token in text for token in ("kurzhantel", "dumbbell", " db ", " kh ")):
        if direction == "up":
            bigger = [w for w in _KH_ALLOWED_LOADS if w >= numeric]
            return float(bigger[0] if bigger else _KH_ALLOWED_LOADS[-1])
        if direction == "down":
            smaller = [w for w in _KH_ALLOWED_LOADS if w <= numeric]
            return float(smaller[-1] if smaller else _KH_ALLOWED_LOADS[0])
        return float(min(_KH_ALLOWED_LOADS, key=lambda w: abs(w - numeric)))
    step = _device_rounding_step(name, device)
    if not step:
        return round(numeric, 2)
    if direction == "up":
        return round(math.ceil(numeric / step) * step, 2)
    if direction == "down":
        return round(math.floor(numeric / step) * step, 2)
    return round(round(numeric / step) * step, 2)


def _choose_caps(intent: str, board: dict[str, Any]) -> dict[str, Any]:
    base = dict(INTENT_DEFAULTS.get(intent, INTENT_DEFAULTS["unknown"]))
    briefing = _briefing_constraints(board)
    constraints = briefing["constraints"]
    if constraints.get("max_rpe_main") not in (None, ""):
        base["max_rpe_main"] = float(constraints["max_rpe_main"])
    if constraints.get("max_rpe_accessory") not in (None, ""):
        base["max_rpe_accessory"] = float(constraints["max_rpe_accessory"])
    if constraints.get("allow_bonus_sets") is not None:
        base["allow_bonus_sets"] = bool(constraints.get("allow_bonus_sets"))
    return base




def _history_lookup_key(name: str, variation: str | None = None) -> tuple[str, str]:
    return (str(name or "").strip().lower(), str(variation or "").strip().lower())


def _lookup_last_exercise_contexts_batch(plan_items: list[dict[str, Any]]) -> tuple[dict[tuple[str, str], dict[str, Any]], dict[str, Any]]:
    requested: list[tuple[str, str, str, str | None]] = []
    seen: set[tuple[str, str]] = set()
    for item in plan_items[:8]:
        name = _text(item.get("name"), 120) or _text(item.get("display_name"), 120) or ""
        variation = _text(item.get("variation"), 80)
        if not name:
            continue
        key = _history_lookup_key(name, variation)
        if key in seen:
            continue
        seen.add(key)
        requested.append((key[0], key[1], name, variation))
    if not requested:
        return {}, {"requested": 0, "matched": 0, "query_ms": 0}

    started = time.perf_counter()
    conn = get_training_db()
    try:
        if not _table_exists(conn, "workouts") or not _table_exists(conn, "exercises") or not _table_exists(conn, "sets"):
            return {}, {"requested": len(requested), "matched": 0, "query_ms": 0}
        clauses: list[str] = []
        params: list[Any] = []
        for _, _, name, variation in requested:
            clause = ["LOWER(TRIM(e.name)) = LOWER(TRIM(?))"]
            params.append(name)
            if variation:
                clause.append("(LOWER(TRIM(COALESCE(e.variation, ''))) = LOWER(TRIM(?)) OR LOWER(TRIM(COALESCE(e.device, ''))) = LOWER(TRIM(?)))")
                params.extend([variation, variation])
            clauses.append("(" + " AND ".join(clause) + ")")
        sql = f"""
            SELECT
                LOWER(TRIM(e.name)) AS key_name,
                LOWER(TRIM(COALESCE(e.variation, ''))) AS key_variation,
                LOWER(TRIM(COALESCE(e.device, ''))) AS key_device,
                w.date_iso,
                e.name,
                e.variation,
                e.device,
                MAX(CASE WHEN s.weight IS NOT NULL THEN s.weight END) AS max_weight,
                AVG(CASE WHEN s.rpe IS NOT NULL THEN s.rpe END) AS avg_rpe,
                MAX(CASE WHEN s.reps IS NOT NULL THEN s.reps END) AS max_reps
            FROM exercises e
            JOIN workouts w ON w.id = e.workout_id
            LEFT JOIN sets s ON s.exercise_id = e.id AND s.workout_id = w.id
            WHERE {" OR ".join(clauses)}
            GROUP BY w.id, e.id
            ORDER BY date(w.date_iso) DESC, w.id DESC, e.id DESC
        """
        rows = [dict(row) for row in conn.execute(sql, tuple(params)).fetchall()]
    finally:
        conn.close()
    out: dict[tuple[str, str], dict[str, Any]] = {}
    for norm_name, norm_variation, raw_name, raw_variation in requested:
        for row in rows:
            row_name = str(row.get("key_name") or "").strip().lower()
            if row_name != norm_name:
                continue
            if norm_variation:
                variations = {str(row.get("key_variation") or "").strip().lower(), str(row.get("key_device") or "").strip().lower()}
                if norm_variation not in variations:
                    continue
            out[(norm_name, norm_variation)] = row
            break
    query_ms = int(round((time.perf_counter() - started) * 1000))
    return out, {"requested": len(requested), "matched": len(out), "query_ms": query_ms}


def _sets_target(raw_sets: Any, volume_factor: float) -> str:
    try:
        sets = int(raw_sets)
    except Exception:
        return "aus Plan übernehmen"
    if volume_factor >= 0.98:
        return str(sets)
    reduced = max(1, int(round(sets * volume_factor)))
    if reduced == sets:
        reduced = max(1, sets - 1)
    return str(reduced)


def _rep_target(item: dict[str, Any]) -> str:
    reps = item.get("reps")
    if isinstance(reps, dict):
        low = reps.get("min")
        high = reps.get("max")
        if low and high:
            return f"{low}–{high}"
        if low:
            return str(low)
        if high:
            return str(high)
    if isinstance(reps, list) and reps:
        return " / ".join(str(x) for x in reps[:2] if x not in (None, ""))
    return "aus Plan übernehmen"


def _display_name(item: dict[str, Any]) -> str:
    name = _text(item.get("display_name") or item.get("name"), 120) or "Übung"
    variation = _text(item.get("variation"), 80)
    if variation and variation.lower() not in name.lower():
        return f"{name} ({variation})"
    return name


def _gym_global_hint(intent: str, allow_bonus_sets: bool) -> str:
    base = "Kein Zusatzsatz." if not allow_bonus_sets else "Keine Extras außerhalb des Plans."
    if intent == "train_controlled":
        return "Nur steigern, wenn das Warm-up klar normal läuft. Kein Zusatzsatz."
    if intent == "reduce_volume":
        return "Volumen heute kürzen, Qualität halten."
    if intent == "technique_only":
        return "Technik vor Last. Nichts ausreizen."
    return base


def _derive_gym_target(history: dict[str, Any], item: dict[str, Any], intent: str, role: str, rpe_cap: float | None) -> dict[str, str]:
    name = _text(item.get("name"), 120) or _text(item.get("display_name"), 120) or "Übung"
    device = _text(item.get("device"), 80) or _text(item.get("variation"), 80)
    ref_weight_raw = history.get("max_weight")
    ref_weight = _round_weight_for_device(ref_weight_raw, name, device, direction="nearest") if ref_weight_raw not in (None, "") else None
    ref_reps = int(history["max_reps"]) if history.get("max_reps") not in (None, "") else None
    target_weight = ref_weight
    progression = "halten"
    note = "Nur steigern, wenn Warm-up klar normal läuft."

    if ref_weight is None:
        return {
            "weight_target": "—",
            "weight_target_reason": "Kein sicherer Referenzsatz vorhanden. Bitte kurz im Verlauf prüfen.",
            "reference_line": "",
            "progression_hint": "halten",
            "note": "Kein sicherer Referenzsatz vorhanden. Bitte kurz im Verlauf prüfen.",
        }

    step = _device_rounding_step(name, device) or 0.0
    if intent == "full_send":
        candidate = _round_weight_for_device(ref_weight + step, name, device, direction="up")
        if candidate and candidate > ref_weight:
            target_weight = candidate
            progression = "Gewicht ↑"
        note = "Sauber progressieren, wenn die ersten Sätze stabil sind."
    elif intent == "train_normal":
        if role == "Hauptübung":
            candidate = _round_weight_for_device(ref_weight + step, name, device, direction="up")
            if candidate and candidate > ref_weight:
                target_weight = candidate
                progression = "Gewicht ↑"
            note = "Nur steigern, wenn Warm-up normal läuft. Sonst Referenzgewicht halten."
        else:
            note = "Sauber ausführen, kein Erzwingen."
    elif intent == "train_controlled":
        if role != "Hauptübung":
            candidate = _round_weight_for_device(ref_weight * 0.98, name, device, direction="down")
            if candidate and candidate < ref_weight:
                target_weight = candidate
                progression = "Gewicht ↓"
            note = "Heute konservativer, kein Erzwingen."
        else:
            note = "Nur steigern, wenn Warm-up klar normal läuft. Sonst Gewicht halten."
    elif intent in {"reduce_volume", "technique_only"}:
        candidate = _round_weight_for_device(ref_weight * (0.95 if intent == "technique_only" else 0.98), name, device, direction="down")
        if candidate and candidate < ref_weight:
            target_weight = candidate
            progression = "Gewicht ↓"
        note = "Heute bewusst konservativer, kein Zusatzsatz."

    ref_bits = []
    ref_text = _weight_text(ref_weight)
    if ref_text:
        if ref_reps is not None:
            ref_text = f"{ref_text} × {ref_reps}"
        if rpe_cap is not None:
            ref_text = f"{ref_text} @RPE {int(round(rpe_cap))}"
        ref_bits.append(ref_text)
    ref_date = _text(history.get("date_iso"), 40)
    if ref_date:
        ref_bits.append(ref_date)
    return {
        "weight_target": _weight_text(target_weight) or "kein sicheres Zielgewicht · bitte aus Verlauf prüfen",
        "reference_line": " · ".join(ref_bits),
        "progression_hint": progression,
        "note": note,
    }


def _build_compact_adjustments(items: list[dict[str, Any]], intent: str) -> list[str]:
    adjustments: list[str] = []
    for item in items:
        if not isinstance(item, dict) or item.get("item_type") != "exercise":
            continue
        name = _text(item.get("display_name"), 80) or "Übung"
        weight_target = _text(item.get("weight_target"), 80)
        note = _text(item.get("note"), 180)
        progression = _text(item.get("progression_hint"), 60)
        if _contains_generic_working_weight_text(weight_target):
            weight_target = "kein sicheres Zielgewicht · Verlauf prüfen"
        if _contains_generic_working_weight_text(note):
            note = ""
        if weight_target and weight_target != "kein sicheres Zielgewicht · bitte aus Verlauf prüfen":
            action = note or ("kontrolliert" if progression in {"halten", ""} else progression)
            adjustments.append(f"{name}: {weight_target} anpeilen · {action}".strip())
        elif note:
            adjustments.append(f"{name}: {note}")
        elif weight_target:
            adjustments.append(f"{name}: {weight_target}")
        if len(adjustments) >= 3:
            break
    if not adjustments and intent in {"train_controlled", "reduce_volume", "technique_only"}:
        adjustments.append("Keine Zusatzsätze außer wenn explizit geplant.")
    return adjustments[:3]


def _normalize_training_card_payload(card: dict[str, Any]) -> dict[str, Any]:
    items = card.get("items") if isinstance(card.get("items"), list) else []
    normalized_items: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        row = dict(item)
        row.setdefault("exercise_name", _text(row.get("display_name"), 120) or _text(row.get("name"), 120) or "Übung")
        row.setdefault("device", _text(row.get("device"), 80) or _text(row.get("variation"), 80))
        row.setdefault("target_sets", row.get("recommended_sets"))
        row.setdefault("target_reps", row.get("rep_target"))
        row.setdefault("target_weight", row.get("weight_target"))
        row.setdefault("target_rpe", row.get("rpe_cap"))
        row.setdefault("reference_weight", row.get("history_weight"))
        row.setdefault("reference_reps", row.get("history_reps"))
        row.setdefault("reference_rpe", row.get("history_rpe"))
        row.setdefault("adjustment_type", row.get("progression_hint") or row.get("action"))
        row.setdefault("adjustment_reason", row.get("note"))
        if _contains_generic_working_weight_text(row.get("target_weight")):
            row["target_weight"] = "—"
        if _contains_generic_working_weight_text(row.get("weight_target")):
            row["weight_target_reason"] = row.get("weight_target")
            row["weight_target"] = row.get("target_weight") or "—"
        normalized_items.append(row)
    card["items"] = normalized_items
    raw_adjustments = card.get("compact_adjustments") if isinstance(card.get("compact_adjustments"), list) else []
    sanitized_adjustments = [str(x) for x in raw_adjustments if not _contains_generic_working_weight_text(x)]
    card["compact_adjustments"] = sanitized_adjustments if sanitized_adjustments else _build_compact_adjustments(normalized_items, str(card.get("decision_intent") or "unknown"))
    raw_today = card.get("today_changed_lines") if isinstance(card.get("today_changed_lines"), list) else []
    card["today_changed_lines"] = [_text(x, 220) for x in raw_today if _text(x, 220)]
    card["today_changed_source"] = _text(card.get("today_changed_source"), 120) or ""
    return card


def _build_why_lines(card: dict[str, Any], board: dict[str, Any]) -> list[str]:
    raw = board.get("visible_reasons") if isinstance(board.get("visible_reasons"), list) else []
    lines: list[str] = []
    for row in raw:
        if isinstance(row, dict):
            text = _text(row.get("text") or row.get("label") or row.get("reason"), 180)
        else:
            text = _text(row, 180)
        if text and text not in lines:
            lines.append(text)
        if len(lines) >= 4:
            break
    if not lines:
        for fallback in (card.get("summary"), card.get("global_hint"), card.get("footer_note")):
            text = _text(fallback, 180)
            if text and text not in lines:
                lines.append(text)
    return lines[:4]


def _collect_today_changed_lines(card: dict[str, Any], board: dict[str, Any], source_context: dict[str, Any] | None = None) -> tuple[list[str], str]:
    def _push(lines: list[str], raw: Any, *, limit: int = 4) -> None:
        if len(lines) >= limit:
            return
        if isinstance(raw, list):
            for entry in raw:
                _push(lines, entry, limit=limit)
                if len(lines) >= limit:
                    return
            return
        if isinstance(raw, dict):
            text = _text(
                raw.get("text")
                or raw.get("label")
                or raw.get("reason")
                or raw.get("headline")
                or raw.get("summary")
                or raw.get("note"),
                220,
            )
        else:
            text = _text(raw, 220)
        if text and text not in lines:
            lines.append(text)

    machine = board.get("machine_briefing") if isinstance(board.get("machine_briefing"), dict) else {}
    src = source_context if isinstance(source_context, dict) else {}
    lines: list[str] = []

    candidates: list[tuple[str, Any]] = [
        ("card.today_changed_lines", card.get("today_changed_lines")),
        ("card.gpt_reason_lines", card.get("gpt_reason_lines")),
        ("card.coach_notes", card.get("coach_notes")),
        ("card.reason_lines", card.get("reason_lines")),
        ("card.decision_reasons", card.get("decision_reasons")),
        ("card.adaptation_notes", card.get("adaptation_notes")),
        ("card.why_today", card.get("why_today")),
        ("board.machine_briefing.today_changed_lines", machine.get("today_changed_lines")),
        ("board.machine_briefing.gpt_reason_lines", machine.get("gpt_reason_lines")),
        ("board.machine_briefing.coach_notes", machine.get("coach_notes")),
        ("board.machine_briefing.reason_lines", machine.get("reason_lines")),
        ("board.machine_briefing.decision_reasons", machine.get("decision_reasons")),
        ("board.machine_briefing.adaptation_notes", machine.get("adaptation_notes")),
        ("board.machine_briefing.why_today", machine.get("why_today")),
        ("board.machine_briefing.human_summary", machine.get("human_summary")),
        ("board.machine_briefing.compact_reason", machine.get("compact_reason")),
        ("board.machine_briefing.explanation", machine.get("explanation")),
        ("board.visible_reasons", board.get("visible_reasons")),
        ("source_context.today_changed_lines", src.get("today_changed_lines")),
        ("source_context.reason_lines", src.get("reason_lines")),
        ("source_context.visible_reasons", src.get("visible_reasons")),
    ]

    for source_name, candidate in candidates:
        before = len(lines)
        _push(lines, candidate)
        if len(lines) > before:
            return lines[:3], source_name

    fallback_bits = []
    for fallback in (card.get("summary"), card.get("global_hint"), card.get("footer_note")):
        text = _text(fallback, 220)
        if text and text not in fallback_bits:
            fallback_bits.append(text)
    return fallback_bits[:3], "fallback.summary"


def _merge_gpt_training_card_fields(card: dict[str, Any], board: dict[str, Any]) -> dict[str, Any]:
    board_card = board.get("training_card") if isinstance(board.get("training_card"), dict) else {}
    if not isinstance(board_card, dict) or not board_card:
        return card

    merged = dict(card)
    board_has_today_changed = bool(board_card.get("today_changed_lines"))
    for key in (
        "title",
        "summary",
        "session_type",
        "session_label",
        "planned_session_label",
        "recommended_session_label",
        "decision_intent",
        "today_changed_lines",
        "today_changed_source",
        "gpt_reason_lines",
        "reason_lines",
        "why_today",
        "coach_notes",
        "adaptation_notes",
    ):
        value = board_card.get(key)
        if value not in (None, "", [], {}):
            merged[key] = value
    if board_has_today_changed:
        merged["today_changed_source"] = _text(board_card.get("today_changed_source"), 120) or "gpt_morning_checkin"

    if not merged.get("today_changed_lines"):
        machine = board.get("machine_briefing") if isinstance(board.get("machine_briefing"), dict) else {}
        machine_card = machine.get("training_card") if isinstance(machine.get("training_card"), dict) else {}
        for key in ("today_changed_lines", "gpt_reason_lines", "reason_lines", "why_today"):
            value = machine_card.get(key)
            if value not in (None, "", [], {}):
                merged[key] = value
                if key == "today_changed_lines" and not merged.get("today_changed_source"):
                    merged["today_changed_source"] = f"machine_briefing.training_card.{key}"
                break
    return merged


def _dedupe_repeated_item_notes(card: dict[str, Any]) -> dict[str, Any]:
    items = card.get("items") if isinstance(card.get("items"), list) else []
    note_counts: dict[str, int] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        note = _text(item.get("note"), 240)
        if note:
            note_counts[note] = note_counts.get(note, 0) + 1
    repeated = [note for note, count in note_counts.items() if count >= 2]
    if not repeated:
        return card

    footer = _text(card.get("footer_note"), 240)
    global_hint = _text(card.get("global_hint"), 240)
    merged: list[str] = []
    for note in repeated:
        if note not in merged:
            merged.append(note)
    for part in [global_hint, footer]:
        if part and part not in merged:
            merged.append(part)
    card["global_hint"] = merged[0] if merged else ""
    card["footer_note"] = " ".join(merged[1:]).strip() if len(merged) > 1 else ""

    for item in items:
        if not isinstance(item, dict):
            continue
        note = _text(item.get("note"), 240)
        if note in repeated:
            item["note"] = None
    return card


def _gym_items(plan_day: dict[str, Any], board: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    caps = _choose_caps(str(board.get("decision_intent") or "unknown"), board)
    volume_factor = float(caps.get("volume_factor") or 1.0)
    intent = str(board.get("decision_intent") or "unknown")
    first = _first_plan_event(plan_day)
    plan_items = _event_exercises(first)
    if not plan_items:
        plan_items = plan_day.get("exercises") if isinstance(plan_day.get("exercises"), list) else []
    history_lookup, history_meta = _lookup_last_exercise_contexts_batch(plan_items)
    items: list[dict[str, Any]] = []
    history_hits = 0
    for idx, item in enumerate(plan_items[:8]):
        role = "Hauptübung" if idx < 2 else "Accessory"
        variation = _text(item.get("variation"), 80)
        name = _text(item.get("name"), 120) or _text(item.get("display_name"), 120) or "Übung"
        history = history_lookup.get(_history_lookup_key(name, variation), {})
        if history:
            history_hits += 1
        rpe_cap = caps["max_rpe_main"] if role == "Hauptübung" else caps["max_rpe_accessory"]
        target = _derive_gym_target(history, item, intent, role, rpe_cap)
        items.append(
            {
                "item_type": "exercise",
                "exercise_key": _text(item.get("exercise_key"), 120) or "unknown",
                "display_name": _display_name(item),
                "role": role,
                "recommended_sets": _sets_target(item.get("sets"), volume_factor),
                "rep_target": _rep_target(item),
                "weight_target": target["weight_target"],
                "rpe_cap": _rpe_text(rpe_cap),
                "action": "hold_or_small_progress" if intent in {"train_normal", "train_controlled"} else str(board.get("decision_intent") or "hold"),
                "note": target["note"],
                "progression_hint": target["progression_hint"],
                "reference_line": target["reference_line"],
                "history_weight": _weight_text(history.get("max_weight")),
                "history_reps": str(int(history["max_reps"])) if history.get("max_reps") not in (None, "") else None,
                "history_rpe": _rpe_text(history.get("avg_rpe")) if history.get("avg_rpe") not in (None, "") else None,
                "history_date": _text(history.get("date_iso"), 40),
                "exercise_name": _display_name(item),
                "device": _text(item.get("device"), 80) or variation,
                "target_sets": _sets_target(item.get("sets"), volume_factor),
                "target_reps": _rep_target(item),
                "target_weight": target["weight_target"],
                "target_rpe": _rpe_text(rpe_cap),
                "reference_weight": _weight_text(history.get("max_weight")),
                "reference_reps": str(int(history["max_reps"])) if history.get("max_reps") not in (None, "") else None,
                "reference_rpe": _rpe_text(history.get("avg_rpe")) if history.get("avg_rpe") not in (None, "") else None,
                "adjustment_type": target["progression_hint"],
                "adjustment_reason": target["note"],
            }
        )
    return items, {"history_hits": history_hits, "plan_items": len(plan_items), "history_batch": history_meta}


def _build_run_card(day_iso: str, plan_day: dict[str, Any], board: dict[str, Any], session_type: str, session_label: str, session_context: dict[str, Any]) -> dict[str, Any]:
    endurance = build_endurance_core_context(day_iso, refresh=True)
    if endurance.get("has_workout"):
        original = endurance.get("original") if isinstance(endurance.get("original"), dict) else {}
        liva = endurance.get("liva") if isinstance(endurance.get("liva"), dict) else {}
        mode = str(liva.get("execution_mode") or "RUN").upper()
        final_mode_label = "Ergo" if mode == "ERGO" else "Run"
        details = [x for x in [original.get("target_label"), f"Load {original.get('training_load')}" if original.get("training_load") not in (None, "") else "", f"Intensität {original.get('intensity')}" if original.get("intensity") not in (None, "") else ""] if _text(x, 80)]
        return {
            "day_iso": day_iso,
            "title": f"Nächste geplante Einheit · {final_mode_label}",
            "session_type": "ergo" if mode == "ERGO" else "run",
            "session_label": session_label or final_mode_label,
            "decision_intent": board.get("decision_intent") or "train_controlled",
            "summary": "CORE hält die Einheit bewusst kontrolliert. Zielbereich treffen, nicht pushen.",
            "planned_session_label": session_context.get("planned_session_label"),
            "recommended_session_label": session_context.get("recommended_session_label"),
            "adapted_from_plan": True,
            "adaptation_note": session_context.get("adaptation_note") or "Athletica-Original und LIVA-Durchführung getrennt halten.",
            "endurance": {
                "title": "Ausdauer zusätzlich",
                "status": liva.get("status"),
                "original_label": original.get("label"),
                "final_summary": liva.get("final_summary"),
                "reason": liva.get("reason"),
                "conflicts": liva.get("conflicts") or [],
            },
            "items": [
                {
                    "item_type": "cardio",
                    "display_name": _text(original.get("title"), 120) or final_mode_label,
                    "target": _text(liva.get("final_summary"), 200) or "kontrolliert",
                    "duration_target": original.get("duration_label") or "aus Plan übernehmen",
                    "hr_target": original.get("target_label") or "Zielbereich respektieren",
                    "pace_target": "kein Lauf-Impact durch Ergo" if mode == "ERGO" else "nicht pushen",
                    "rpe_cap": "locker",
                    "action": "controlled",
                    "note": _text(liva.get("reason"), 220) or "Zielbereich nicht überschreiten.",
                },
                {
                    "item_type": "note",
                    "display_name": "Details",
                    "target": _text(original.get("structure_label"), 180) or "Struktur aus Athletica übernehmen",
                    "note": " · ".join(details) if details else ("Ergo locker im Zielpulsbereich, keine Zusatzhärte." if mode == "ERGO" else "Zielbereich sauber treffen."),
                },
            ],
            "footer_note": "Original bleibt Run, LIVA-Final ist die Durchführung.",
            "source": "core_board",
            "board_id": board.get("id"),
        }
    if session_type == "ergo":
        title = "Ergo Z2 · ruhig"
        display_name = "Ergo Z2"
        pace_target = "keine Watt-Jagd"
    elif session_type == "run":
        title = "Run Z2 · ruhig"
        display_name = "Run Z2"
        pace_target = "keine Pace-Jagd"
    else:
        title = "Z2 · ruhig"
        display_name = "Z2"
        pace_target = "keine Pace-/Watt-Jagd"
    first = _first_plan_event(plan_day)
    card = {
        "day_iso": day_iso,
        "title": title,
        "session_type": session_type,
        "session_label": session_label or display_name,
        "decision_intent": board.get("decision_intent") or "train_controlled",
        "summary": "Ruhig starten, Pulsdeckel respektieren, Mobility danach.",
        "planned_session_label": session_context.get("planned_session_label"),
        "recommended_session_label": session_context.get("recommended_session_label"),
        "adapted_from_plan": bool(session_context.get("adapted_from_plan")),
        "adaptation_note": session_context.get("adaptation_note"),
        "items": [
            {
                "item_type": "cardio",
                "display_name": display_name,
                "target": "ruhig und kontrolliert",
                "duration_target": session_context.get("next_session_duration_target") or _text(first.get("time"), 40) or "aus Plan übernehmen",
                "hr_target": session_context.get("next_session_hr_target") or "Pulsdeckel respektieren",
                "pace_target": pace_target,
                "rpe_cap": "locker",
                "action": "controlled",
                "note": "Wenn der Puls driftet, Tempo rausnehmen statt gegenarbeiten.",
            },
            {
                "item_type": "mobility",
                "display_name": "Mobility danach",
                "target": "kurz und sauber",
                "note": "Nach der Einheit abhaken, nicht zur zweiten Session aufblasen.",
            },
        ],
        "footer_note": "Qualität vor Tempo.",
        "source": "core_board",
        "board_id": board.get("id"),
    }
    return card


def _build_recovery_card(day_iso: str, board: dict[str, Any], intent: str) -> dict[str, Any]:
    hard_stop = intent == "hard_stop"
    return {
        "day_iso": day_iso,
        "title": "Heute kein Training erzwingen" if hard_stop else "Recovery statt Training",
        "session_type": "hard_stop" if hard_stop else "recovery",
        "session_label": "Hard Stop" if hard_stop else "Recovery",
        "decision_intent": intent,
        "summary": "CORE sieht heute keinen sinnvollen Trainingstag." if hard_stop else "Heute keine harte Einheit erzwingen.",
        "items": [
            {
                "item_type": "note" if hard_stop else "mobility",
                "display_name": "Training stoppen" if hard_stop else "Mobility / Bewegung",
                "target": "kein Training" if hard_stop else "locker",
                "note": "Kein Druck, kein Testen, keine Ersatzeskalation." if hard_stop else "Nur bewegen, nichts erzwingen.",
            }
        ],
        "footer_note": "Heute nicht verhandeln." if hard_stop else "Heute zählt Erholung.",
        "source": "core_board",
        "board_id": board.get("id"),
    }


def _build_unknown_card(day_iso: str, plan_day: dict[str, Any], board: dict[str, Any], session_label: str) -> dict[str, Any]:
    return {
        "day_iso": day_iso,
        "title": f"{session_label or 'Training'} · offen",
        "session_type": "unknown",
        "session_label": session_label or "Training",
        "decision_intent": board.get("decision_intent") or "unknown",
        "summary": "Plan sichtbar, aber ohne harte Progressionsaussage.",
        "items": [
            {
                "item_type": "note",
                "display_name": session_label or "Heutige Einheit",
                "target": "aus Plan übernehmen",
                "note": "Heute nichts aggressiv ableiten. Struktur ja, Eskalation nein.",
            }
        ],
        "footer_note": "Nur das übernehmen, was plan- und datenbasiert klar ist.",
        "source": "core_board",
        "board_id": board.get("id"),
    }


def _build_gym_card(day_iso: str, plan_day: dict[str, Any], board: dict[str, Any], session_label: str) -> tuple[dict[str, Any], dict[str, Any]]:
    items, quality = _gym_items(plan_day, board)
    intent = str(board.get("decision_intent") or "unknown")
    title_variant = {
        "full_send": "frei",
        "train_normal": "normal",
        "train_controlled": "kontrolliert",
        "reduce_volume": "gekürzt",
        "technique_only": "technisch",
        "unknown": "offen",
    }.get(intent, "kontrolliert")
    summary = {
        "full_send": "Plan normal, mit sauberem Spielraum wenn alles klar läuft.",
        "train_normal": "Plan bleibt. Kleine Progression nur, wenn sie heute wirklich sauber ist.",
        "train_controlled": "Plan bleibt, aber ohne Zusatzdruck.",
        "reduce_volume": "Hauptübungen bleiben, Volumen heute reduziert.",
        "technique_only": "Heute nur sauber bewegen, nicht jagen.",
        "unknown": "Plan sichtbar, aber ohne aggressive Empfehlung.",
    }.get(intent, "Plan bleibt, aber ohne Zusatzdruck.")
    card = {
        "day_iso": day_iso,
        "title": f"{session_label or 'Gym'} · {title_variant}",
        "session_type": "gym",
        "session_label": session_label or "Gym",
        "decision_intent": intent,
        "summary": summary,
        "global_hint": _gym_global_hint(intent, bool(_choose_caps(intent, board).get("allow_bonus_sets"))),
        "rule": _gym_global_hint(intent, bool(_choose_caps(intent, board).get("allow_bonus_sets"))),
        "compact_adjustments": _build_compact_adjustments(items, intent),
        "items": items or [
            {
                "item_type": "note",
                "display_name": session_label or "Heutige Einheit",
                "target": "aus Plan übernehmen",
                "note": "Keine sauberen Übungsdetails gefunden. Bitte Plan direkt übernehmen.",
            }
        ],
        "footer_note": "Wenn das Warm-up schwer wirkt: konservativ bleiben.",
        "source": "core_board",
        "board_id": board.get("id"),
    }
    card["why_lines"] = _build_why_lines(card, board)
    today_changed_lines, today_changed_source = _collect_today_changed_lines(card, board)
    card["today_changed_lines"] = today_changed_lines
    card["today_changed_source"] = today_changed_source
    endurance = build_endurance_core_context(day_iso, refresh=True)
    if endurance.get("has_workout"):
        original = endurance.get("original") if isinstance(endurance.get("original"), dict) else {}
        liva = endurance.get("liva") if isinstance(endurance.get("liva"), dict) else {}
        card["endurance"] = {
            "title": "Ausdauer zusätzlich",
            "status": liva.get("status"),
            "original_label": original.get("label"),
            "final_summary": liva.get("final_summary"),
            "reason": liva.get("reason"),
            "conflicts": liva.get("conflicts") or [],
            "execution_mode": liva.get("execution_mode"),
        }
    return _dedupe_repeated_item_notes(card), quality


def upsert_core_training_card(
    *,
    day_iso: str,
    board_id: int | None,
    source: str,
    session_type: str | None,
    session_label: str | None,
    decision_intent: str | None,
    title: str | None,
    summary: str | None,
    card: dict[str, Any],
    data_quality: dict[str, Any],
    source_context: dict[str, Any],
) -> dict[str, Any]:
    day = _parse_day(day_iso)
    now = _utc_iso()
    legacy_mode = _legacy_mode_from_intent(str(decision_intent or "unknown"))
    legacy_card_type = _legacy_card_type(session_type, card)
    payload_json = _json_dumps(card, CARD_JSON_DEFAULTS["card_json"])
    conn = get_core_db()
    try:
        ensure_core_training_card_schema(conn)
        existing = conn.execute(
            """
            SELECT id, created_at FROM core_training_cards
            WHERE day_iso=? AND status='active' AND source='core_board'
            ORDER BY updated_at DESC, id DESC
            LIMIT 1
            """,
            (day,),
        ).fetchone()
        if existing:
            conn.execute(
                """
                UPDATE core_training_cards
                SET mode=?, card_type=?, payload_json=?, board_id=?, updated_at=?, source=?, status='active', session_type=?, session_label=?,
                    decision_intent=?, title=?, summary=?, card_json=?, data_quality_json=?, source_context_json=?
                WHERE id=?
                """,
                (
                    legacy_mode,
                    legacy_card_type,
                    payload_json,
                    board_id,
                    now,
                    source,
                    session_type,
                    session_label,
                    decision_intent,
                    title,
                    summary,
                    payload_json,
                    _json_dumps(data_quality, CARD_JSON_DEFAULTS["data_quality_json"]),
                    _json_dumps(source_context, CARD_JSON_DEFAULTS["source_context_json"]),
                    existing["id"],
                ),
            )
            row_id = int(existing["id"])
        else:
            cur = conn.execute(
                """
                INSERT INTO core_training_cards (
                    mode, card_type, payload_json, created_at,
                    day_iso, board_id, updated_at, source, status,
                    session_type, session_label, decision_intent, title, summary,
                    card_json, data_quality_json, source_context_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    legacy_mode,
                    legacy_card_type,
                    payload_json,
                    now,
                    day,
                    board_id,
                    now,
                    source,
                    session_type,
                    session_label,
                    decision_intent,
                    title,
                    summary,
                    payload_json,
                    _json_dumps(data_quality, CARD_JSON_DEFAULTS["data_quality_json"]),
                    _json_dumps(source_context, CARD_JSON_DEFAULTS["source_context_json"]),
                ),
            )
            row_id = int(cur.lastrowid)
        conn.commit()
        row = conn.execute("SELECT * FROM core_training_cards WHERE id=?", (row_id,)).fetchone()
        return _row_to_training_card(row)
    finally:
        conn.close()


def build_core_training_card(day_iso: str, board: dict[str, Any] | None = None, force: bool = False) -> dict[str, Any]:
    day = _parse_day(day_iso)
    if board is None:
        board = get_core_daily_board(day, create_if_missing=False)
    if not isinstance(board, dict) or not board:
        raise ValueError("board_required")
    if not force:
        existing = get_core_training_card(day, board_id=board.get("id"))
        board_card = board.get("training_card") if isinstance(board.get("training_card"), dict) else {}
        board_today_changed = board_card.get("today_changed_lines") if isinstance(board_card.get("today_changed_lines"), list) else []
        existing_today_changed = existing.get("today_changed_lines") if isinstance(existing, dict) and isinstance(existing.get("today_changed_lines"), list) else []
        normalized_board_lines = [_text(x, 220) for x in board_today_changed if _text(x, 220)]
        normalized_existing_lines = [_text(x, 220) for x in existing_today_changed if _text(x, 220)]
        should_rebuild_for_gpt_lines = bool(normalized_board_lines) and normalized_board_lines != normalized_existing_lines
        if existing and not should_rebuild_for_gpt_lines:
            return existing

    plan_day = _plan_day(day)
    intent = str(board.get("decision_intent") or "unknown").strip().lower() or "unknown"
    session_context = _resolved_session_context(day, plan_day, board)
    session_type = str(session_context.get("recommended_session_type") or "unknown")
    session_label = str(session_context.get("recommended_session_label") or "Training")
    endurance = build_endurance_core_context(day, refresh=True)
    if endurance.get("has_workout") and not (plan_day.get("available") and session_type == "gym"):
        endurance_mode = str(endurance.get("execution_mode") or ((endurance.get("liva") or {}).get("execution_mode")) or "RUN").upper()
        session_type = "ergo" if endurance_mode == "ERGO" else "run"
        session_label = str(((endurance.get("original") or {}).get("title")) or session_label)
    data_quality = {
        "board_available": True,
        "plan_available": bool(plan_day.get("available")),
        "plan_source": plan_day.get("source"),
        "decision_intent": intent,
        "history_available": False,
        "missing_safe_values": [],
    }
    source_context = {
        "board_id": board.get("id"),
        "board_source": board.get("source"),
        "plan_name": plan_day.get("plan_name"),
        "day_key": plan_day.get("day_key"),
        "machine_briefing": board.get("machine_briefing") if isinstance(board.get("machine_briefing"), dict) else {},
        "visible_reasons": board.get("visible_reasons") if isinstance(board.get("visible_reasons"), list) else [],
    }
    source_context.update(session_context)

    if intent == "hard_stop":
        card = _build_recovery_card(day, board, "hard_stop")
    elif intent == "recovery_only":
        card = _build_recovery_card(day, board, "recovery_only")
    elif session_type in {"run", "ergo"}:
        card = _build_run_card(day, plan_day, board, session_type, session_label, session_context)
    elif session_type == "gym":
        card, quality = _build_gym_card(day, plan_day, board, session_label)
        data_quality["history_available"] = bool(quality.get("history_hits"))
        if not quality.get("history_hits"):
            data_quality["missing_safe_values"].append("weight_target")
    else:
        card = _build_unknown_card(day, plan_day, board, session_label)

    card = _merge_gpt_training_card_fields(card, board)

    if not isinstance(card.get("items"), list):
        card["items"] = []
    card.setdefault("planned_session_label", session_context.get("planned_session_label"))
    card.setdefault("recommended_session_label", session_context.get("recommended_session_label"))
    card.setdefault("adapted_from_plan", bool(session_context.get("adapted_from_plan")))
    card.setdefault("adaptation_note", session_context.get("adaptation_note"))
    for item in card["items"]:
        if isinstance(item, dict) and item.get("weight_target") in (None, ""):
            item["weight_target"] = "—"
    if not card.get("today_changed_lines"):
        today_changed_lines, today_changed_source = _collect_today_changed_lines(card, board, source_context)
        card["today_changed_lines"] = today_changed_lines
        card["today_changed_source"] = today_changed_source
    if card.get("today_changed_lines") and "today_changed_lines" not in source_context:
        source_context["today_changed_lines"] = list(card.get("today_changed_lines") or [])
        source_context["today_changed_source"] = card.get("today_changed_source")
    card = _dedupe_repeated_item_notes(card)

    return upsert_core_training_card(
        day_iso=day,
        board_id=board.get("id"),
        source="core_board",
        session_type=card.get("session_type"),
        session_label=card.get("session_label"),
        decision_intent=card.get("decision_intent"),
        title=card.get("title"),
        summary=card.get("summary"),
        card=card,
        data_quality=data_quality,
        source_context=source_context,
    )
