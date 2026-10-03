from __future__ import annotations

import json
import re
import sqlite3
from datetime import date, datetime, timezone
from typing import Any

from database.connections import get_core_db, get_nutrition_db
from memory import get_memory_service
from memory.memory_service import sanitize_memory_content


TIMEFRAMES = {"today", "week", "phase", "year", "global"}
STATUSES = {"active", "pending", "dismissed", "promoted", "superseded"}
WRITE_POLICIES = {"auto_log", "candidate", "manual_review", "never_auto"}
CONTROL_ACTIONS = {"promote", "dismiss", "keep_pending", "update", "apply_review_log"}
CONSOLIDATION_STATUSES = {"draft", "applied", "dismissed", "superseded"}
CONSOLIDATION_ACTIONS = {"apply", "dismiss", "update"}
CONSOLIDATION_BATCH_ACTIONS = {"apply_review_log", "dismiss", "mark_needs_review"}
CONSOLIDATION_STRATEGIES = {"append_section_note", "append_log_entry", "replace_section_candidate", "manual_review_only"}
REVIEW_ENTITY_TYPES = {"event", "consolidation", "memory_file_patch"}
REVIEW_ACTIONS = {
    "create",
    "update",
    "promote",
    "dismiss",
    "keep_pending",
    "apply_review_log",
    "apply_memory_file",
    "batch_apply_review_log",
    "batch_dismiss",
    "mark_needs_review",
    "duplicate_marked",
    "conflict_marked",
    "create_patch",
    "update_patch",
    "dismiss_patch",
    "apply_memory_file_patch",
}
REVIEW_ACTORS = {"system", "ui", "v2_action", "gpt", "manual"}
STOPWORDS = {
    "der", "die", "das", "und", "oder", "mit", "ohne", "für", "fuer", "bei", "aus", "dem", "den", "ein", "eine",
    "einer", "eines", "ist", "sind", "wird", "werden", "nicht", "nur", "mehrere", "heute", "woche", "phase",
    "jahr", "global", "core", "event", "kandidat", "training", "im", "in", "am", "an", "zu",
}
EDITABLE_FIELDS = {
    "title",
    "summary",
    "timeframe",
    "category",
    "confidence",
    "target_memory_file",
    "target_section",
    "write_policy",
}
TARGET_SECTIONS: dict[str, set[str]] = {
    "review_log": {"LOG_EINTRAEGE"},
    "training_history": {"AKTUELLER_BLOCK", "RELEVANTE_UMSTELLUNGEN", "BELASTUNGSWECHSEL", "PROBLEME_DURCHBRUECHE"},
    "athlete_dossier": {"AKTUELLE_PHASE", "STAERKEN", "SCHWAECHEN", "AUFFAELLIGE_MUSTER", "OFFENE_PUNKTE"},
    "core_patterns": {"TRAINING", "VERHALTEN", "ENTSCHEIDUNGEN", "ORGANISATION", "WIEDERKEHRENDE_TENDENZEN"},
    "health_notes": {"EINSCHRAENKUNGEN", "VERLETZUNGEN", "RECOVERY", "MEDIZINISCHE_HINWEISE"},
}

CONSOLIDATION_EDITABLE_FIELDS = {
    "title",
    "summary",
    "timeframe",
    "category",
    "target_memory_file",
    "target_section",
    "proposed_strategy",
    "confidence",
}


def _utc_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_day(day_iso: Any | None) -> str | None:
    if day_iso in (None, ""):
        return None
    text = str(day_iso).strip()
    date.fromisoformat(text)
    return text


def _week_iso(day_iso: str | None) -> str | None:
    if not day_iso:
        return None
    parsed = date.fromisoformat(day_iso)
    iso = parsed.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def _phase_id(board: dict[str, Any], training_card: dict[str, Any] | None = None) -> str | None:
    machine = board.get("machine_briefing") if isinstance(board.get("machine_briefing"), dict) else {}
    for candidate in (
        machine.get("phase_id"),
        machine.get("phase"),
        machine.get("phase_label"),
        ((training_card or {}).get("source_context") or {}).get("plan_name"),
    ):
        text = str(candidate or "").strip()
        if text:
            return text[:120]
    mode = _nutrition_mode()
    return mode[:120] if mode else None


def _nutrition_mode() -> str | None:
    conn = get_nutrition_db()
    try:
        row = conn.execute("SELECT value FROM nutrition_settings WHERE key='active_mode' LIMIT 1").fetchone()
        if not row:
            return None
        return str(row["value"] if isinstance(row, sqlite3.Row) else row[0]).strip() or None
    except Exception:
        return None
    finally:
        conn.close()


def _json_dumps(value: Any, default: Any) -> str:
    if value is None:
        value = default
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _json_loads(value: Any, default: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(str(value or ""))
    except Exception:
        return default


def _text(value: Any, limit: int = 500) -> str | None:
    text = " ".join(str(value or "").split()).strip()
    if not text:
        return None
    return text[:limit]


def _slug(value: Any, limit: int = 120) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"[^a-z0-9äöüß]+", "-", text).strip("-")
    return text[:limit] or "event"


def _event_key(event: dict[str, Any]) -> str | None:
    source = str(event.get("source") or "").strip()
    source_id = str(event.get("source_id") or "").strip()
    timeframe = str(event.get("timeframe") or "").strip()
    category = str(event.get("category") or "").strip()
    title = _slug(event.get("title"), 80)
    if not source or not timeframe or not category:
        return None
    if source_id:
        return f"{source}:{source_id}:{timeframe}:{category}:{title}"
    day_iso = str(event.get("day_iso") or "").strip()
    if day_iso:
        return f"{source}:{day_iso}:{timeframe}:{category}:{title}"
    return f"{source}:{timeframe}:{category}:{title}"


def _tokenize(value: Any) -> list[str]:
    text = str(value or "").strip().lower()
    text = re.sub(r"[^a-z0-9äöüß]+", " ", text)
    tokens: list[str] = []
    for token in text.split():
        if len(token) < 3:
            continue
        if token in STOPWORDS:
            continue
        tokens.append(token)
    return tokens


def ensure_core_memory_events_schema(conn: sqlite3.Connection | None = None) -> None:
    own_conn = conn is None
    db = conn or get_core_db()
    try:
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS core_memory_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                day_iso TEXT,
                week_iso TEXT,
                phase_id TEXT,
                year INTEGER,
                source TEXT NOT NULL,
                source_id TEXT,
                event_key TEXT,
                timeframe TEXT NOT NULL,
                category TEXT NOT NULL,
                title TEXT NOT NULL,
                summary TEXT NOT NULL,
                evidence_json TEXT NOT NULL DEFAULT '{}',
                confidence REAL,
                status TEXT NOT NULL DEFAULT 'active',
                target_memory_file TEXT,
                target_section TEXT,
                write_policy TEXT NOT NULL DEFAULT 'candidate',
                applied_at TEXT,
                applied_result_json TEXT NOT NULL DEFAULT '{}'
            )
            """
        )
        _ensure_column(db, "core_memory_events", "event_key", "TEXT")
        db.execute("CREATE INDEX IF NOT EXISTS idx_core_memory_events_day_iso ON core_memory_events(day_iso)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_core_memory_events_week_iso ON core_memory_events(week_iso)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_core_memory_events_timeframe ON core_memory_events(timeframe)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_core_memory_events_category ON core_memory_events(category)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_core_memory_events_status ON core_memory_events(status)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_core_memory_events_target_file ON core_memory_events(target_memory_file)")
        db.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_core_memory_events_event_key
            ON core_memory_events(event_key)
            WHERE event_key IS NOT NULL AND event_key != ''
            """
        )
        if own_conn:
            db.commit()
    finally:
        if own_conn:
            db.close()


def ensure_core_memory_consolidations_schema(conn: sqlite3.Connection | None = None) -> None:
    own_conn = conn is None
    db = conn or get_core_db()
    try:
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS core_memory_consolidations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                consolidation_key TEXT,
                status TEXT NOT NULL DEFAULT 'draft',
                timeframe TEXT NOT NULL,
                category TEXT NOT NULL,
                title TEXT NOT NULL,
                summary TEXT NOT NULL,
                source_event_ids_json TEXT NOT NULL DEFAULT '[]',
                evidence_json TEXT NOT NULL DEFAULT '{}',
                target_memory_file TEXT,
                target_section TEXT,
                proposed_strategy TEXT,
                conflict_status TEXT NOT NULL DEFAULT 'ready_to_apply',
                evidence_strength REAL,
                duplicate_of_consolidation_id INTEGER,
                confidence REAL,
                applied_at TEXT,
                applied_result_json TEXT NOT NULL DEFAULT '{}',
                dismissed_at TEXT,
                dismissed_reason TEXT
            )
            """
        )
        db.execute("CREATE INDEX IF NOT EXISTS idx_core_memory_consolidations_status ON core_memory_consolidations(status)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_core_memory_consolidations_timeframe ON core_memory_consolidations(timeframe)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_core_memory_consolidations_category ON core_memory_consolidations(category)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_core_memory_consolidations_target_file ON core_memory_consolidations(target_memory_file)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_core_memory_consolidations_conflict_status ON core_memory_consolidations(conflict_status)")
        _ensure_column(db, "core_memory_consolidations", "conflict_status", "TEXT NOT NULL DEFAULT 'ready_to_apply'")
        _ensure_column(db, "core_memory_consolidations", "evidence_strength", "REAL")
        _ensure_column(db, "core_memory_consolidations", "duplicate_of_consolidation_id", "INTEGER")
        db.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_core_memory_consolidations_key
            ON core_memory_consolidations(consolidation_key)
            WHERE consolidation_key IS NOT NULL AND consolidation_key != ''
            """
        )
        if own_conn:
            db.commit()
    finally:
        if own_conn:
            db.close()


def ensure_core_memory_review_log_schema(conn: sqlite3.Connection | None = None) -> None:
    own_conn = conn is None
    db = conn or get_core_db()
    try:
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS core_memory_review_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                entity_type TEXT NOT NULL,
                entity_id INTEGER NOT NULL,
                action TEXT NOT NULL,
                previous_status TEXT,
                new_status TEXT,
                note TEXT,
                reason TEXT,
                actor TEXT NOT NULL DEFAULT 'system',
                payload_json TEXT NOT NULL DEFAULT '{}'
            )
            """
        )
        db.execute("CREATE INDEX IF NOT EXISTS idx_core_memory_review_log_entity ON core_memory_review_log(entity_type, entity_id)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_core_memory_review_log_action ON core_memory_review_log(action)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_core_memory_review_log_created_at ON core_memory_review_log(created_at)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_core_memory_review_log_actor ON core_memory_review_log(actor)")
        if own_conn:
            db.commit()
    finally:
        if own_conn:
            db.close()


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    columns = {str(row["name"] if isinstance(row, sqlite3.Row) else row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def append_memory_review_log(
    entity_type: str,
    entity_id: Any,
    action: str,
    *,
    previous_status: str | None = None,
    new_status: str | None = None,
    note: str | None = None,
    reason: str | None = None,
    actor: str = "system",
    payload: dict[str, Any] | None = None,
    conn: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    entity_type_text = str(entity_type or "").strip().lower()
    action_text = str(action or "").strip().lower()
    actor_text = str(actor or "system").strip().lower() or "system"
    if entity_type_text not in REVIEW_ENTITY_TYPES:
        raise ValueError("invalid_entity_type")
    if action_text not in REVIEW_ACTIONS:
        raise ValueError("invalid_review_action")
    if actor_text not in REVIEW_ACTORS:
        actor_text = "system"
    ensure_core_memory_review_log_schema(conn)
    own_conn = conn is None
    db = conn or get_core_db()
    try:
        now = _utc_iso()
        cur = db.execute(
            """
            INSERT INTO core_memory_review_log (
                created_at, entity_type, entity_id, action, previous_status, new_status,
                note, reason, actor, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                now,
                entity_type_text,
                int(entity_id),
                action_text,
                _text(previous_status, 60),
                _text(new_status, 60),
                _text(note, 500),
                _text(reason, 500),
                actor_text,
                _json_dumps(payload or {}, {}),
            ),
        )
        if own_conn:
            db.commit()
        return {
            "id": int(cur.lastrowid),
            "created_at": now,
            "entity_type": entity_type_text,
            "entity_id": int(entity_id),
            "action": action_text,
            "previous_status": _text(previous_status, 60),
            "new_status": _text(new_status, 60),
            "note": _text(note, 500),
            "reason": _text(reason, 500),
            "actor": actor_text,
            "payload": payload or {},
        }
    finally:
        if own_conn:
            db.close()


def get_memory_review_history(entity_type: str, entity_id: Any) -> list[dict[str, Any]]:
    ensure_core_memory_review_log_schema()
    conn = get_core_db()
    try:
        rows = conn.execute(
            """
            SELECT * FROM core_memory_review_log
            WHERE entity_type=? AND entity_id=?
            ORDER BY created_at DESC, id DESC
            """,
            (str(entity_type or "").strip().lower(), int(entity_id)),
        ).fetchall()
        history: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["payload"] = _json_loads(item.pop("payload_json", "{}"), {})
            history.append(item)
        return history
    finally:
        conn.close()


def get_recent_memory_review_log(limit: int = 50) -> list[dict[str, Any]]:
    ensure_core_memory_review_log_schema()
    safe_limit = max(1, min(200, int(limit)))
    conn = get_core_db()
    try:
        rows = conn.execute(
            "SELECT * FROM core_memory_review_log ORDER BY created_at DESC, id DESC LIMIT ?",
            (safe_limit,),
        ).fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["payload"] = _json_loads(item.pop("payload_json", "{}"), {})
            out.append(item)
        return out
    finally:
        conn.close()


def classify_memory_timeframe(event: dict[str, Any]) -> str:
    explicit = str(event.get("timeframe") or "").strip().lower()
    if explicit in TIMEFRAMES:
        return explicit
    category = str(event.get("category") or "").strip().lower()
    if category in {"schedule_shift"}:
        return "week"
    if category in {"phase_rule", "nutrition_response"}:
        return "phase"
    if category in {"behavior_pattern"}:
        return "global"
    return "today"


def route_memory_target(event: dict[str, Any]) -> tuple[str | None, str | None]:
    timeframe = classify_memory_timeframe(event)
    category = str(event.get("category") or "").strip().lower()
    if timeframe == "today":
        return "review_log", "LOG_EINTRAEGE"
    if timeframe == "week":
        if category == "schedule_shift":
            return "training_history", "BELASTUNGSWECHSEL"
        return "review_log", "LOG_EINTRAEGE"
    if timeframe == "phase":
        if category in {"phase_rule", "nutrition_response"}:
            return "athlete_dossier", "AKTUELLE_PHASE"
        return "core_patterns", "ENTSCHEIDUNGEN"
    if timeframe == "year":
        if category in {"exercise_response", "fatigue_pattern"}:
            return "training_history", "PROBLEME_DURCHBRUECHE"
        return "training_history", "AKTUELLER_BLOCK"
    if timeframe == "global":
        if category == "fatigue_pattern":
            return "health_notes", "RECOVERY"
        if category == "behavior_pattern":
            return "athlete_dossier", "AUFFAELLIGE_MUSTER"
        if category == "decision_rule":
            return "core_patterns", "ENTSCHEIDUNGEN"
        return "core_patterns", "TRAINING"
    return None, None


def _event_base(day_iso: str, board: dict[str, Any], *, source: str, source_id: Any = None) -> dict[str, Any]:
    parsed = _parse_day(day_iso)
    return {
        "day_iso": parsed,
        "week_iso": _week_iso(parsed),
        "phase_id": _phase_id(board),
        "year": date.fromisoformat(parsed).year if parsed else None,
        "source": source,
        "source_id": str(source_id) if source_id not in (None, "") else None,
    }


def _training_card_candidate_items(training_card: dict[str, Any]) -> list[dict[str, Any]]:
    items = training_card.get("items") if isinstance(training_card.get("items"), list) else []
    out: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        if not any(item.get(key) for key in ("history_weight", "history_reps", "history_date")):
            continue
        display_name = _text(item.get("display_name"), 120) or "Übung"
        history_bits = [
            _text(item.get("history_weight"), 80),
            _text(item.get("history_reps"), 40),
            _text(item.get("history_date"), 40),
        ]
        history_text = ", ".join(bit for bit in history_bits if bit)
        out.append(
            {
                "timeframe": "phase",
                "category": "exercise_response",
                "title": f"Exercise-Historie: {display_name}",
                "summary": f"{display_name}: letzter sicherer Referenzpunkt {history_text}." if history_text else f"{display_name}: sichere Historie erkannt.",
                "confidence": 0.42,
                "write_policy": "candidate",
                "evidence_json": {
                    "item": {
                        "display_name": item.get("display_name"),
                        "history_weight": item.get("history_weight"),
                        "history_reps": item.get("history_reps"),
                        "history_date": item.get("history_date"),
                    }
                },
            }
        )
    return out[:4]


def build_memory_events_from_board(
    day_iso: str,
    board: dict[str, Any],
    training_card: dict[str, Any] | None = None,
    memory_candidates: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    if not isinstance(board, dict) or not board:
        raise ValueError("board_required")
    base = _event_base(day_iso, board, source="core_board", source_id=board.get("id"))
    intent = _text(board.get("decision_intent"), 80) or "unknown"
    headline = _text(board.get("human_headline"), 160) or "Tagesentscheidung gespeichert"
    summary = _text(board.get("human_summary"), 400) or headline
    visible_reasons = board.get("visible_reasons") if isinstance(board.get("visible_reasons"), list) else []
    machine = board.get("machine_briefing") if isinstance(board.get("machine_briefing"), dict) else {}
    constraints = machine.get("constraints") if isinstance(machine.get("constraints"), dict) else {}
    consistency = board.get("board_consistency") if isinstance(board.get("board_consistency"), dict) else {}
    events: list[dict[str, Any]] = [
        {
            **base,
            "timeframe": "today",
            "category": "decision_rule",
            "title": f"CORE Entscheidung: {headline}",
            "summary": summary,
            "confidence": board.get("confidence"),
            "status": "active",
            "write_policy": "auto_log",
            "evidence_json": {
                "decision_intent": intent,
                "visible_reasons": visible_reasons[:4],
                "constraints": constraints,
            },
        }
    ]
    if isinstance(training_card, dict) and training_card:
        events.append(
            {
                **_event_base(day_iso, board, source="core_training_card", source_id=training_card.get("id")),
                "timeframe": "today",
                "category": "training_response",
                "title": f"Training-Card: {_text(training_card.get('title'), 160) or 'Training'}",
                "summary": _text(training_card.get("summary"), 400)
                or _text(training_card.get("footer_note"), 240)
                or "Training-Card wurde aus Board, Plan und sicherer Historie gebaut.",
                "confidence": board.get("confidence"),
                "status": "active",
                "write_policy": "auto_log",
                "evidence_json": {
                    "session_type": training_card.get("session_type"),
                    "planned_session_label": training_card.get("planned_session_label"),
                    "recommended_session_label": training_card.get("recommended_session_label"),
                    "items_count": len(training_card.get("items") or []),
                },
            }
        )
        events.extend(
            {
                **_event_base(day_iso, board, source="core_training_card", source_id=training_card.get("id")),
                **candidate,
            }
            for candidate in _training_card_candidate_items(training_card)
        )
    if consistency.get("status") and consistency.get("status") != "ok":
        events.append(
            {
                **base,
                "timeframe": "today",
                "category": "open_question",
                "title": "Board/Text-Mismatch erkannt",
                "summary": _text(
                    f"Board-Text passt nicht sauber zur Einheit. Plan: {consistency.get('planned_session_label') or '-'}; Empfehlung: {consistency.get('recommended_session_label') or '-'}; Tokens: {', '.join(consistency.get('detected_text_tokens') or [])}",
                    400,
                )
                or "Board-Text passt nicht sauber zur heutigen Training-Card.",
                "confidence": 0.9,
                "status": "active",
                "write_policy": "auto_log",
                "evidence_json": consistency,
            }
        )
    if constraints.get("allow_bonus_sets") is False and intent in {"train_controlled", "reduce_volume", "technique_only"}:
        mode = _nutrition_mode()
        timeframe = "phase" if mode in {"cut", "maintenance", "bulk", "lean_bulk"} else "global"
        events.append(
            {
                **base,
                "timeframe": timeframe,
                "category": "decision_rule",
                "title": "Kontrollregel für Belastung erkannt",
                "summary": _text(
                    f"{intent}: keine Bonus-Sätze und keine Eskalation außerhalb des Plans. Kontext: {mode or 'offene Phase'}.",
                    400,
                )
                or "Belastungsregel erkannt.",
                "confidence": 0.58,
                "status": "pending",
                "write_policy": "candidate",
                "evidence_json": {"decision_intent": intent, "constraints": constraints, "nutrition_mode": mode},
            }
        )
    for raw_candidate in memory_candidates or []:
        if not isinstance(raw_candidate, dict):
            continue
        events.append(
            {
                **base,
                "source": "gpt_memory_candidate",
                "source_id": str(board.get("id") or ""),
                "timeframe": raw_candidate.get("timeframe"),
                "category": _text(raw_candidate.get("category"), 80) or "open_question",
                "title": _text(raw_candidate.get("title"), 160) or "Memory-Kandidat",
                "summary": _text(raw_candidate.get("summary"), 600) or "Kein Summary geliefert.",
                "confidence": raw_candidate.get("confidence"),
                "status": "pending",
                "write_policy": _text(raw_candidate.get("write_policy"), 40) or "candidate",
                "evidence_json": raw_candidate.get("evidence") if isinstance(raw_candidate.get("evidence"), dict) else {"raw": raw_candidate.get("evidence")},
            }
        )

    normalized: list[dict[str, Any]] = []
    for raw in events:
        timeframe = classify_memory_timeframe(raw)
        target_file, target_section = route_memory_target(raw)
        write_policy = str(raw.get("write_policy") or "candidate").strip().lower()
        if write_policy not in WRITE_POLICIES:
            write_policy = "candidate"
        status = str(raw.get("status") or ("active" if write_policy == "auto_log" else "pending")).strip().lower()
        if status not in STATUSES:
            status = "active" if write_policy == "auto_log" else "pending"
        event = {
            "day_iso": raw.get("day_iso"),
            "week_iso": raw.get("week_iso"),
            "phase_id": raw.get("phase_id"),
            "year": raw.get("year"),
            "source": _text(raw.get("source"), 80) or "manual",
            "source_id": _text(raw.get("source_id"), 80),
            "timeframe": timeframe,
            "category": _text(raw.get("category"), 80) or "open_question",
            "title": _text(raw.get("title"), 160) or "Memory-Eintrag",
            "summary": _text(raw.get("summary"), 600) or "Kein Summary.",
            "evidence_json": raw.get("evidence_json") if isinstance(raw.get("evidence_json"), dict) else {},
            "confidence": raw.get("confidence"),
            "status": status,
            "target_memory_file": target_file,
            "target_section": target_section,
            "write_policy": write_policy,
        }
        event["event_key"] = _event_key(event)
        normalized.append(event)
    return normalized


def _row_to_event(row: sqlite3.Row | None) -> dict[str, Any]:
    if not row:
        return {}
    data = dict(row)
    data["evidence"] = _json_loads(data.pop("evidence_json", "{}"), {})
    data["applied_result"] = _json_loads(data.pop("applied_result_json", "{}"), {})
    return data


def _row_to_consolidation(row: sqlite3.Row | None) -> dict[str, Any]:
    if not row:
        return {}
    data = dict(row)
    data["source_event_ids"] = _json_loads(data.pop("source_event_ids_json", "[]"), [])
    data["evidence"] = _json_loads(data.pop("evidence_json", "{}"), {})
    data["applied_result"] = _json_loads(data.pop("applied_result_json", "{}"), {})
    data = _enrich_consolidation_diagnostics(data)
    data["review_history"] = get_memory_review_history("consolidation", data["id"])
    return data


def _token_set(value: Any) -> set[str]:
    return set(_tokenize(value))


def _event_polarity(event: dict[str, Any]) -> str:
    text = f"{event.get('title') or ''} {event.get('summary') or ''}".lower()
    positive = any(token in text for token in ("progress", "carbs", "normal", "trainieren", "erlaubt", "ja"))
    restrictive = any(token in text for token in ("kein", "ohne", "stop", "nicht", "vermeiden", "limit", "cap"))
    if positive and restrictive:
        return "mixed"
    if restrictive:
        return "restrictive"
    if positive:
        return "positive"
    return "neutral"


def _consolidation_reason_explanation(reason_codes: list[str], strategy: str, top_tokens: list[str]) -> str:
    if "mixed_positive_and_restrictive" in reason_codes:
        return "Diese Konsolidierung mischt progressive und restriktive Hinweise. Bitte prüfen, ob daraus eine kontrollierte Regel statt eines Widerspruchs wird."
    if "duplicate_of_applied" in reason_codes:
        return "Ein ähnlicher Draft wurde bereits angewendet. Bitte prüfen, ob dieser Entwurf wirklich neue Information enthält."
    if "manual_review_only" in reason_codes:
        return "Dieser Entwurf bleibt bewusst im manuellen Review, weil Ziel oder Inhalt vorsichtiger behandelt werden müssen."
    if "weak_evidence" in reason_codes:
        return "Die Evidenz ist aktuell zu schwach oder zu uneinheitlich, um sauber konsolidiert zu werden."
    if "low_similarity" in reason_codes:
        return "Die Source-Events teilen zu wenig gemeinsame Leitwörter für eine stabile Konsolidierung."
    if "conflicting_targets" in reason_codes:
        return "Die zugehörigen Events deuten auf unterschiedliche Zielbereiche, deshalb ist der Draft noch nicht sauber einordenbar."
    if "conflicting_categories" in reason_codes:
        return "Die Events wirken fachlich gemischt und sollten erst manuell getrennt oder neu zugeschnitten werden."
    if "conflict_status_without_details" in reason_codes:
        return "Diese Konsolidierung wurde als Konflikt markiert und braucht Review, bevor daraus Memory-Patches entstehen."
    if "needs_review" in reason_codes:
        return "Diese Konsolidierung braucht manuelle Prüfung."
    if "similar_applied" in reason_codes:
        return "Diese Konsolidierung ist bereits einem angewendeten Muster ähnlich und sollte erst manuell abgegrenzt werden."
    if strategy == "manual_review_only":
        return "Dieser Entwurf braucht bewusst menschliches Review, bevor er weiterverwendet wird."
    if top_tokens:
        return f"Bitte prüfen, ob die Leitwörter {', '.join(top_tokens[:4])} wirklich dieselbe Regel beschreiben."
    return "Bitte prüfen, ob die zugrunde liegenden Events wirklich dasselbe Muster beschreiben."


def _review_suggestion_for_consolidation(row: dict[str, Any]) -> str:
    conflict_status = str(row.get("conflict_status") or "").strip().lower()
    evidence_strength = float(row.get("evidence_strength") or 0.0)
    if conflict_status == "conflict":
        return "dismiss_suggested" if evidence_strength < 0.55 else "review_required"
    if conflict_status == "weak":
        return "dismiss_or_wait"
    if conflict_status == "needs_review":
        return "can_mark_needs_review"
    if conflict_status == "ready_to_apply":
        return "can_apply_review_log"
    return "review_required"


def _enrich_consolidation_diagnostics(consolidation: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(consolidation, dict):
        return {}
    evidence = consolidation.get("evidence") if isinstance(consolidation.get("evidence"), dict) else {}
    conflict_status = str(consolidation.get("conflict_status") or "").strip().lower()
    duplicate_of = consolidation.get("duplicate_of_consolidation_id")
    similar_applied = bool(evidence.get("similar_applied"))
    top_tokens = evidence.get("top_tokens") if isinstance(evidence.get("top_tokens"), list) else []
    source_event_ids = consolidation.get("source_event_ids") if isinstance(consolidation.get("source_event_ids"), list) else []
    source_titles = evidence.get("source_titles") if isinstance(evidence.get("source_titles"), list) else []
    source_summaries = evidence.get("source_summaries") if isinstance(evidence.get("source_summaries"), list) else []
    conflict_details = evidence.get("conflict_details") if isinstance(evidence.get("conflict_details"), dict) else {}
    reason_codes = conflict_details.get("reason_codes") if isinstance(conflict_details.get("reason_codes"), list) else []
    positive_tokens = conflict_details.get("positive_tokens") if isinstance(conflict_details.get("positive_tokens"), list) else []
    restrictive_tokens = conflict_details.get("restrictive_tokens") if isinstance(conflict_details.get("restrictive_tokens"), list) else []
    mixed_tokens = conflict_details.get("mixed_tokens") if isinstance(conflict_details.get("mixed_tokens"), list) else []
    if conflict_status != "ready_to_apply":
        if not reason_codes:
            if conflict_status == "conflict":
                reason_codes = ["conflict_status_without_details"]
            elif conflict_status == "weak":
                reason_codes = ["weak_evidence"]
            elif conflict_status == "needs_review":
                reason_codes = ["needs_review"]
        if duplicate_of and "duplicate_of_existing" not in reason_codes:
            reason_codes.append("duplicate_of_existing")
        if similar_applied and "similar_applied" not in reason_codes:
            reason_codes.append("similar_applied")
        if conflict_status == "conflict" and not mixed_tokens:
            mixed_tokens = [str(token) for token in top_tokens[:6]]
        explanation = _text(conflict_details.get("explanation"), 500)
        if not explanation:
            explanation = _consolidation_reason_explanation(reason_codes, str(consolidation.get("proposed_strategy") or ""), [str(token) for token in top_tokens])
        evidence["conflict_details"] = {
            "reason_codes": [str(code) for code in reason_codes if str(code).strip()],
            "positive_tokens": [str(token) for token in positive_tokens if str(token).strip()],
            "restrictive_tokens": [str(token) for token in restrictive_tokens if str(token).strip()],
            "mixed_tokens": [str(token) for token in mixed_tokens if str(token).strip()],
            "source_event_ids": [int(item) for item in source_event_ids if str(item).strip()],
            "source_titles": [str(item) for item in source_titles if str(item).strip()],
            "source_summaries": [str(item) for item in source_summaries if str(item).strip()],
            "explanation": explanation,
        }
    consolidation["evidence"] = evidence
    consolidation["review_suggestion"] = _review_suggestion_for_consolidation(consolidation)
    consolidation["conflict_details"] = evidence.get("conflict_details")
    return consolidation


def _score_evidence_strength(event_count: int, top_tokens: list[str], statuses: set[str]) -> float:
    base = 0.25 + min(0.35, event_count * 0.12) + min(0.2, len(top_tokens) * 0.04)
    if "promoted" in statuses:
        base += 0.12
    if "pending" in statuses and len(statuses) == 1:
        base -= 0.04
    return round(max(0.0, min(1.0, base)), 2)


def _consolidation_conflict_status(events: list[dict[str, Any]], top_tokens: list[str], strategy: str) -> tuple[str, dict[str, Any]]:
    polarities = {_event_polarity(event) for event in events}
    restrictive = sum(1 for event in events if _event_polarity(event) == "restrictive")
    positive = sum(1 for event in events if _event_polarity(event) == "positive")
    positive_tokens = sorted({token for event in events for token in _tokenize(f"{event.get('title') or ''} {event.get('summary') or ''}") if token in {"progress", "normal", "trainieren", "carbs", "erlaubt"}})
    restrictive_tokens = sorted({token for event in events for token in _tokenize(f"{event.get('title') or ''} {event.get('summary') or ''}") if token in {"kein", "ohne", "nicht", "vermeiden", "limit", "cap"}})
    mixed_tokens = sorted(set(positive_tokens) & set(restrictive_tokens))
    event_count = len(events)
    evidence_strength = _score_evidence_strength(event_count, top_tokens, {str(event.get("status")) for event in events})
    reason_codes: list[str] = []
    if strategy == "manual_review_only":
        reason_codes.append("manual_review_only")
    if "mixed" in polarities or (positive and restrictive):
        reason_codes.append("mixed_positive_and_restrictive")
    if event_count < 2 or evidence_strength < 0.45:
        reason_codes.append("weak_evidence")
    if len(top_tokens) < 2:
        reason_codes.append("low_similarity")
    evidence = {
        "event_count": event_count,
        "top_tokens": top_tokens,
        "positive_count": positive,
        "restrictive_count": restrictive,
        "polarities": sorted(polarities),
        "evidence_strength": evidence_strength,
        "conflict_details": {
            "reason_codes": reason_codes,
            "positive_tokens": positive_tokens,
            "restrictive_tokens": restrictive_tokens,
            "mixed_tokens": mixed_tokens,
            "source_event_ids": [int(event.get("id")) for event in events if event.get("id") is not None],
            "source_titles": [_text(event.get("title"), 120) for event in events if _text(event.get("title"), 120)],
            "source_summaries": [_text(event.get("summary"), 220) for event in events if _text(event.get("summary"), 220)],
            "explanation": _consolidation_reason_explanation(reason_codes, strategy, top_tokens),
        },
    }
    if strategy == "manual_review_only":
        return "needs_review", evidence
    if "mixed" in polarities or (positive and restrictive):
        return "conflict", evidence
    if event_count < 2 or evidence_strength < 0.45:
        return "weak", evidence
    return "ready_to_apply", evidence


def _similarity_ratio(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / float(max(1, union))


def _apply_review_log(event: dict[str, Any], *, note: str | None = None, reason: str | None = None) -> dict[str, Any]:
    target_file = "review_log"
    target_section = "LOG_EINTRAEGE"
    content_lines = [
        f"### {event.get('day_iso') or event.get('created_at') or _utc_iso()}",
        f"- {event.get('title')}",
        f"- {event.get('summary')}",
    ]
    if note:
        content_lines.append(f"- Review-Notiz: {note}")
    if reason:
        content_lines.append(f"- Anlass: {reason}")
    content = sanitize_memory_content("\n".join(content_lines))
    payload = {
        "write_plan": [
            {
                "logical_file": target_file,
                "section": target_section,
                "strategy": "append_log_entry",
                "content": content,
            }
        ],
        "reason": reason or f"core_memory_event:{event.get('event_key') or event.get('id')}",
    }
    applied = get_memory_service().apply(payload)
    return {"ok": bool(applied.get("success")), "memory_apply": applied, "target_memory_file": target_file, "target_section": target_section}


def _is_review_log_applied(event: dict[str, Any]) -> bool:
    applied_result = event.get("applied_result") if isinstance(event.get("applied_result"), dict) else {}
    if event.get("applied_at") and applied_result.get("target_memory_file") == "review_log":
        return True
    memory_apply = applied_result.get("memory_apply") if isinstance(applied_result.get("memory_apply"), dict) else {}
    for row in memory_apply.get("updated_files") or []:
        if isinstance(row, dict) and row.get("logical_file") == "review_log":
            return True
    return False


def upsert_core_memory_events(events: list[dict[str, Any]]) -> dict[str, Any]:
    ensure_core_memory_events_schema()
    conn = get_core_db()
    created = 0
    updated = 0
    pending = 0
    auto_logged = 0
    errors: list[str] = []
    rows: list[dict[str, Any]] = []
    try:
        now = _utc_iso()
        for event in events:
            event_key = _event_key(event)
            evidence_json = _json_dumps(event.get("evidence_json"), {})
            row = None
            if event_key:
                row = conn.execute("SELECT * FROM core_memory_events WHERE event_key=? LIMIT 1", (event_key,)).fetchone()
            if not row:
                row = conn.execute(
                    """
                    SELECT * FROM core_memory_events
                    WHERE day_iso IS ? AND source=? AND COALESCE(source_id,'')=COALESCE(?, '') AND timeframe=? AND category=? AND title=?
                    LIMIT 1
                    """,
                    (
                        event.get("day_iso"),
                        event.get("source"),
                        event.get("source_id"),
                        event.get("timeframe"),
                        event.get("category"),
                        event.get("title"),
                    ),
                ).fetchone()
            if row:
                conn.execute(
                    """
                    UPDATE core_memory_events
                    SET updated_at=?, week_iso=?, phase_id=?, year=?, event_key=?, summary=?, evidence_json=?, confidence=?,
                        status=?, target_memory_file=?, target_section=?, write_policy=?
                    WHERE id=?
                    """,
                    (
                        now,
                        event.get("week_iso"),
                        event.get("phase_id"),
                        event.get("year"),
                        event_key,
                        event.get("summary"),
                        evidence_json,
                        event.get("confidence"),
                        event.get("status"),
                        event.get("target_memory_file"),
                        event.get("target_section"),
                        event.get("write_policy"),
                        row["id"],
                    ),
                )
                event_id = int(row["id"])
                updated += 1
            else:
                cur = conn.execute(
                    """
                    INSERT INTO core_memory_events (
                        created_at, updated_at, day_iso, week_iso, phase_id, year, source, source_id, event_key,
                        timeframe, category, title, summary, evidence_json, confidence, status,
                        target_memory_file, target_section, write_policy, applied_at, applied_result_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, '{}')
                    """,
                    (
                        now,
                        now,
                        event.get("day_iso"),
                        event.get("week_iso"),
                        event.get("phase_id"),
                        event.get("year"),
                        event.get("source"),
                        event.get("source_id"),
                        event_key,
                        event.get("timeframe"),
                        event.get("category"),
                        event.get("title"),
                        event.get("summary"),
                        evidence_json,
                        event.get("confidence"),
                        event.get("status"),
                        event.get("target_memory_file"),
                        event.get("target_section"),
                        event.get("write_policy"),
                    ),
                )
                event_id = int(cur.lastrowid)
                created += 1
            stored = _row_to_event(conn.execute("SELECT * FROM core_memory_events WHERE id=?", (event_id,)).fetchone())
            if stored.get("status") == "pending":
                pending += 1
            if stored.get("write_policy") == "auto_log" and not stored.get("applied_at"):
                try:
                    applied_result = _apply_review_log(stored, reason=f"core_memory_event:{stored.get('event_key') or event_id}")
                    conn.execute(
                        "UPDATE core_memory_events SET applied_at=?, applied_result_json=? WHERE id=?",
                        (now if applied_result.get("ok") else None, _json_dumps(applied_result, {}), event_id),
                    )
                    if applied_result.get("ok"):
                        auto_logged += 1
                    else:
                        errors.append("auto_log_failed")
                except Exception as exc:
                    conn.execute(
                        "UPDATE core_memory_events SET applied_result_json=? WHERE id=?",
                        (_json_dumps({"ok": False, "error": str(exc)}, {}), event_id),
                    )
                    errors.append(str(exc))
            rows.append(_row_to_event(conn.execute("SELECT * FROM core_memory_events WHERE id=?", (event_id,)).fetchone()))
        conn.commit()
        return {"created": created, "updated": updated, "pending": pending, "auto_logged": auto_logged, "errors": errors, "events": rows}
    finally:
        conn.close()


def get_core_memory_context(
    day_iso: str | None = None,
    timeframe: str | None = None,
    status: str | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    ensure_core_memory_events_schema()
    limit = max(1, min(200, int(limit)))
    params: list[Any] = []
    clauses: list[str] = []
    if day_iso:
        parsed = _parse_day(day_iso)
        if timeframe == "today" or timeframe is None:
            clauses.append("day_iso=?")
            params.append(parsed)
        elif timeframe == "week":
            clauses.append("week_iso=?")
            params.append(_week_iso(parsed))
        elif timeframe == "year":
            clauses.append("year=?")
            params.append(date.fromisoformat(parsed).year)
    if timeframe in TIMEFRAMES:
        clauses.append("timeframe=?")
        params.append(timeframe)
    if status:
        clauses.append("status=?")
        params.append(status)
    sql = "SELECT * FROM core_memory_events"
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY updated_at DESC, id DESC LIMIT ?"
    params.append(limit)
    conn = get_core_db()
    try:
        return [_row_to_event(row) for row in conn.execute(sql, tuple(params)).fetchall()]
    finally:
        conn.close()


def summarize_memory_layers(day_iso: str | None = None) -> dict[str, Any]:
    from core.core_memory_file_patches import get_memory_file_patches

    layers: dict[str, list[dict[str, Any]]] = {}
    counts: dict[str, dict[str, int]] = {}
    for timeframe in ("today", "week", "phase", "year", "global"):
        rows = get_core_memory_context(day_iso=day_iso, timeframe=timeframe, limit=12)
        layers[timeframe] = rows
        counts[timeframe] = {
            "total": len(rows),
            "pending": sum(1 for row in rows if row.get("status") == "pending"),
            "auto_logged": sum(1 for row in rows if row.get("write_policy") == "auto_log"),
            "promoted": sum(1 for row in rows if row.get("status") == "promoted"),
        }
    today_pending = sum(1 for row in layers.get("today", []) if row.get("status") == "pending")
    total_pending = sum(entry.get("pending", 0) for entry in counts.values())
    patch_drafts = get_memory_file_patches(status="draft", limit=80)
    patch_applied = get_memory_file_patches(status="applied", limit=40)
    return {
        "date": day_iso,
        "layers": layers,
        "counts": counts,
        "headline": {
            "today_total": counts.get("today", {}).get("total", 0),
            "today_pending": today_pending,
            "phase_pending": counts.get("phase", {}).get("pending", 0),
            "global_pending": counts.get("global", {}).get("pending", 0),
            "pending_total": total_pending,
            "draft_total": len(get_memory_consolidations(status="draft", limit=20)),
            "patch_total": len(patch_drafts) + len(patch_applied),
            "patch_draft_total": len(patch_drafts),
        },
    }


def build_memory_layer_counts(layers: dict[str, list[dict[str, Any]]] | None) -> dict[str, int]:
    scoped = layers if isinstance(layers, dict) else {}
    counts = {
        "today": 0,
        "week": 0,
        "phase": 0,
        "year": 0,
        "global": 0,
        "pending": 0,
        "active": 0,
        "promoted": 0,
        "dismissed": 0,
    }
    for timeframe in ("today", "week", "phase", "year", "global"):
        rows = scoped.get(timeframe) if isinstance(scoped.get(timeframe), list) else []
        counts[timeframe] = len(rows)
        for row in rows:
            status = str(row.get("status") or "").strip().lower()
            if status in {"pending", "active", "promoted", "dismissed"}:
                counts[status] += 1
    return counts


def get_memory_inbox(day_iso: str | None = None, *, limit: int = 80) -> dict[str, Any]:
    ensure_core_memory_events_schema()
    rows = get_core_memory_context(day_iso=day_iso, limit=limit)
    grouped = {
        "today": [row for row in rows if row.get("timeframe") == "today"],
        "week": [row for row in rows if row.get("timeframe") == "week"],
        "phase": [row for row in rows if row.get("timeframe") == "phase"],
        "year": [row for row in rows if row.get("timeframe") == "year"],
        "global": [row for row in rows if row.get("timeframe") == "global"],
    }
    counts = {
        "today": len(grouped["today"]),
        "week": len(grouped["week"]),
        "phase_pending": sum(1 for row in grouped["phase"] if row.get("status") == "pending"),
        "global_pending": sum(1 for row in grouped["global"] if row.get("status") == "pending"),
        "pending_total": sum(1 for row in rows if row.get("status") == "pending"),
        "promoted_total": sum(1 for row in rows if row.get("status") == "promoted"),
        "dismissed_total": sum(1 for row in rows if row.get("status") == "dismissed"),
    }
    return {"ok": True, "counts": counts, "events": rows, "groups": grouped}


def _consolidation_target_for_group(timeframe: str, category: str, target_memory_file: str | None, target_section: str | None) -> tuple[str | None, str | None, str]:
    file_key = str(target_memory_file or "").strip()
    section_key = str(target_section or "").strip()
    category_key = str(category or "").strip().lower()
    timeframe_key = str(timeframe or "").strip().lower()
    strategy = "append_section_note"
    if file_key == "master_profile":
        return file_key, section_key, "manual_review_only"
    if file_key == "health_notes" and category_key in {"open_question", "medical", "fatigue_pattern"}:
        return file_key, section_key or "RECOVERY", "manual_review_only"
    if not file_key or not section_key:
        if category_key in {"decision_rule"}:
            return "core_patterns", "ENTSCHEIDUNGEN", "append_section_note"
        if category_key in {"phase_rule", "nutrition_response"}:
            return "athlete_dossier", "AKTUELLE_PHASE", "append_section_note"
        if category_key in {"fatigue_pattern"}:
            return "health_notes", "RECOVERY", "manual_review_only"
        if timeframe_key in {"week", "year"}:
            return "training_history", "AKTUELLER_BLOCK", "append_section_note"
        return "core_patterns", "TRAINING", "append_section_note"
    return file_key, section_key, strategy


def _event_tokens(event: dict[str, Any]) -> list[str]:
    return _tokenize(f"{event.get('title') or ''} {event.get('summary') or ''}")


def _has_any_token(top_tokens: list[str], values: tuple[str, ...]) -> bool:
    token_set = {str(token or "").strip().lower() for token in top_tokens}
    return any(value in token_set for value in values)


def _build_consolidation_title(events: list[dict[str, Any]], top_tokens: list[str], category: str) -> str:
    first_title = _text((events[0] if events else {}).get("title"), 120)
    if category == "exercise_response":
        return "Übungshistorie: sichere Referenzpunkte"
    if category == "nutrition_response":
        return "Ernährung: wiederkehrende Reaktion"
    if category == "fatigue_pattern":
        return "Fatigue-Muster: wiederkehrende Belastung"
    if category == "decision_rule" and _has_any_token(top_tokens, ("controlled", "train", "bonus", "bonusvolumen", "rpe", "cap")):
        return "Kontrollierte Trainingstage: keine Zusatzeskalation"
    if top_tokens:
        lead = " ".join(token.capitalize() for token in top_tokens[:4])
        if category == "decision_rule":
            return f"{lead}: Entscheidungsregel"
        if category == "phase_rule":
            return f"{lead}: Phasenmuster"
        return lead
    return first_title or "Konsolidiertes Memory-Muster"


def _build_consolidation_summary(events: list[dict[str, Any]], top_tokens: list[str], category: str) -> str:
    lines: list[str] = []
    if top_tokens:
        lines.append(
            f"Mehrere CORE-Ereignisse weisen auf ein wiederkehrendes Muster hin. Relevante Leitwörter: {', '.join(top_tokens[:5])}."
        )
    summaries = [_text(event.get("summary"), 220) for event in events]
    summaries = [entry for entry in summaries if entry]
    if summaries:
        lines.append(f"Wiederkehrende Beobachtungen: {summaries[0]}")
    if len(summaries) > 1:
        lines.append(f"Weitere passende Spuren: {summaries[1]}")
    if category == "decision_rule":
        lines.append("Der Vorschlag sollte als reviewbarer Hinweis gelten, nicht als harte Regel ohne Prüfung.")
    return " ".join(lines)[:900] or "Mehrere CORE-Ereignisse deuten auf ein wiederkehrendes Muster hin."


def _consolidation_key(
    timeframe: str,
    category: str,
    target_memory_file: str | None,
    target_section: str | None,
    top_tokens: list[str],
    source_event_ids: list[int],
) -> str:
    lead = "-".join(top_tokens[:4]) if top_tokens else "-".join(str(item) for item in source_event_ids[:3])
    return f"{timeframe}:{category}:{target_memory_file or 'none'}:{target_section or 'none'}:{lead}"


def find_consolidation_candidates(
    timeframe: str | None = None,
    category: str | None = None,
    min_events: int = 2,
    status_filter: list[str] | None = None,
) -> list[dict[str, Any]]:
    ensure_core_memory_events_schema()
    rows = get_core_memory_context(day_iso=None, timeframe=timeframe if timeframe in TIMEFRAMES else None, limit=400)
    allowed_statuses = set(status_filter or ["pending", "promoted"])
    filtered = [
        row for row in rows
        if row.get("status") in allowed_statuses
        and row.get("status") != "dismissed"
        and row.get("summary")
        and row.get("timeframe") != "today"
        and (not category or row.get("category") == category)
    ]
    buckets: dict[tuple[str, str, str, str], list[dict[str, Any]]] = {}
    for row in filtered:
        key = (
            str(row.get("timeframe") or ""),
            str(row.get("category") or ""),
            str(row.get("target_memory_file") or ""),
            str(row.get("target_section") or ""),
        )
        buckets.setdefault(key, []).append(row)

    candidates: list[dict[str, Any]] = []
    for bucket_key, bucket_rows in buckets.items():
        if len(bucket_rows) < min_events:
            continue
        token_counts: dict[str, int] = {}
        titles: list[str] = []
        dates: list[str] = []
        event_ids: list[int] = []
        for row in bucket_rows:
            event_ids.append(int(row.get("id")))
            if row.get("title"):
                titles.append(str(row.get("title")))
            if row.get("day_iso"):
                dates.append(str(row.get("day_iso")))
            for token in set(_event_tokens(row)):
                token_counts[token] = token_counts.get(token, 0) + 1
        top_tokens = [token for token, count in sorted(token_counts.items(), key=lambda item: (-item[1], item[0])) if count >= 2][:6]
        if not top_tokens and len(bucket_rows) < 3:
            continue
        title = _build_consolidation_title(bucket_rows, top_tokens, bucket_key[1])
        summary = _build_consolidation_summary(bucket_rows, top_tokens, bucket_key[1])
        target_file, target_section, strategy = _consolidation_target_for_group(bucket_key[0], bucket_key[1], bucket_key[2], bucket_key[3])
        confidence = round(min(0.92, 0.4 + (len(bucket_rows) * 0.12) + (min(len(top_tokens), 4) * 0.05)), 2)
        conflict_status, conflict_evidence = _consolidation_conflict_status(bucket_rows, top_tokens, strategy)
        evidence = {
            "event_count": len(bucket_rows),
            "top_tokens": top_tokens,
            "dates": sorted(set(dates))[:10],
            "source_titles": titles[:6],
            "source_statuses": sorted({str(row.get("status")) for row in bucket_rows}),
        }
        evidence.update(conflict_evidence)
        candidates.append(
            {
                "timeframe": bucket_key[0],
                "category": bucket_key[1],
                "title": title,
                "summary": summary,
                "source_event_ids": sorted(set(event_ids)),
                "evidence": evidence,
                "target_memory_file": target_file,
                "target_section": target_section,
                "proposed_strategy": strategy,
                "conflict_status": conflict_status,
                "evidence_strength": evidence.get("evidence_strength"),
                "duplicate_of_consolidation_id": None,
                "confidence": confidence,
                "consolidation_key": _consolidation_key(bucket_key[0], bucket_key[1], target_file, target_section, top_tokens, event_ids),
            }
        )
    return candidates


def upsert_consolidation_draft(draft: dict[str, Any]) -> dict[str, Any]:
    ensure_core_memory_consolidations_schema()
    ensure_core_memory_review_log_schema()
    conn = get_core_db()
    try:
        now = _utc_iso()
        draft = _enrich_consolidation_diagnostics({**draft})
        key = str(draft.get("consolidation_key") or "").strip() or None
        row = conn.execute("SELECT * FROM core_memory_consolidations WHERE consolidation_key=? LIMIT 1", (key,)).fetchone() if key else None
        if row:
            previous = _row_to_consolidation(row)
            conn.execute(
                """
                UPDATE core_memory_consolidations
                SET updated_at=?, timeframe=?, category=?, title=?, summary=?, source_event_ids_json=?, evidence_json=?,
                    target_memory_file=?, target_section=?, proposed_strategy=?, conflict_status=?, evidence_strength=?, duplicate_of_consolidation_id=?, confidence=?
                WHERE id=?
                """,
                (
                    now,
                    draft.get("timeframe"),
                    draft.get("category"),
                    draft.get("title"),
                    draft.get("summary"),
                    _json_dumps(draft.get("source_event_ids"), []),
                    _json_dumps(draft.get("evidence"), {}),
                    draft.get("target_memory_file"),
                    draft.get("target_section"),
                    draft.get("proposed_strategy"),
                    draft.get("conflict_status"),
                    draft.get("evidence_strength"),
                    draft.get("duplicate_of_consolidation_id"),
                    draft.get("confidence"),
                    row["id"],
                ),
            )
            draft_id = int(row["id"])
            status_before = previous.get("status")
            if draft.get("duplicate_of_consolidation_id"):
                append_memory_review_log(
                    "consolidation",
                    draft_id,
                    "duplicate_marked",
                    previous_status=status_before,
                    new_status=status_before,
                    actor="system",
                    payload={
                        "duplicate_of_consolidation_id": draft.get("duplicate_of_consolidation_id"),
                        "similar_applied": bool((draft.get("evidence") or {}).get("similar_applied")),
                    },
                    conn=conn,
                )
            if draft.get("conflict_status") == "conflict":
                append_memory_review_log(
                    "consolidation",
                    draft_id,
                    "conflict_marked",
                    previous_status=status_before,
                    new_status=status_before,
                    actor="system",
                    payload={"conflict_status": draft.get("conflict_status"), "evidence": draft.get("evidence") or {}},
                    conn=conn,
                )
        else:
            cur = conn.execute(
                """
                INSERT INTO core_memory_consolidations (
                    created_at, updated_at, consolidation_key, status, timeframe, category, title, summary,
                    source_event_ids_json, evidence_json, target_memory_file, target_section, proposed_strategy,
                    conflict_status, evidence_strength, duplicate_of_consolidation_id, confidence,
                    applied_at, applied_result_json, dismissed_at, dismissed_reason
                ) VALUES (?, ?, ?, 'draft', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, '{}', NULL, NULL)
                """,
                (
                    now,
                    now,
                    key,
                    draft.get("timeframe"),
                    draft.get("category"),
                    draft.get("title"),
                    draft.get("summary"),
                    _json_dumps(draft.get("source_event_ids"), []),
                    _json_dumps(draft.get("evidence"), {}),
                    draft.get("target_memory_file"),
                    draft.get("target_section"),
                    draft.get("proposed_strategy"),
                    draft.get("conflict_status"),
                    draft.get("evidence_strength"),
                    draft.get("duplicate_of_consolidation_id"),
                    draft.get("confidence"),
                ),
            )
            draft_id = int(cur.lastrowid)
            append_memory_review_log(
                "consolidation",
                draft_id,
                "create",
                previous_status=None,
                new_status="draft",
                actor="system",
                payload={
                    "consolidation_key": key,
                    "timeframe": draft.get("timeframe"),
                    "category": draft.get("category"),
                    "target_memory_file": draft.get("target_memory_file"),
                    "target_section": draft.get("target_section"),
                    "conflict_status": draft.get("conflict_status"),
                    "duplicate_of_consolidation_id": draft.get("duplicate_of_consolidation_id"),
                },
                conn=conn,
            )
            if draft.get("duplicate_of_consolidation_id"):
                append_memory_review_log(
                    "consolidation",
                    draft_id,
                    "duplicate_marked",
                    previous_status="draft",
                    new_status="draft",
                    actor="system",
                    payload={
                        "duplicate_of_consolidation_id": draft.get("duplicate_of_consolidation_id"),
                        "similar_applied": bool((draft.get("evidence") or {}).get("similar_applied")),
                    },
                    conn=conn,
                )
            if draft.get("conflict_status") == "conflict":
                append_memory_review_log(
                    "consolidation",
                    draft_id,
                    "conflict_marked",
                    previous_status="draft",
                    new_status="draft",
                    actor="system",
                    payload={"conflict_status": draft.get("conflict_status"), "evidence": draft.get("evidence") or {}},
                    conn=conn,
                )
        conn.commit()
        return _row_to_consolidation(conn.execute("SELECT * FROM core_memory_consolidations WHERE id=?", (draft_id,)).fetchone())
    finally:
        conn.close()


def build_consolidation_drafts(
    timeframe: str | None = None,
    category: str | None = None,
    min_events: int = 2,
    status_filter: list[str] | None = None,
) -> dict[str, Any]:
    ensure_core_memory_consolidations_schema()
    drafts = find_consolidation_candidates(timeframe=timeframe, category=category, min_events=min_events, status_filter=status_filter)
    existing_all = get_memory_consolidations(limit=200)
    existing_keys = {row.get("consolidation_key") for row in existing_all}
    normalized_existing = [
        {
            "id": row.get("id"),
            "status": row.get("status"),
            "title_tokens": _token_set(row.get("title")),
            "summary_tokens": _token_set(row.get("summary")),
            "timeframe": row.get("timeframe"),
            "category": row.get("category"),
            "target_memory_file": row.get("target_memory_file"),
            "target_section": row.get("target_section"),
        }
        for row in existing_all
    ]
    created = 0
    updated = 0
    rows: list[dict[str, Any]] = []
    for draft in drafts:
        draft_tokens = _token_set(f"{draft.get('title') or ''} {draft.get('summary') or ''}")
        duplicate_of = None
        similar_applied = False
        for existing in normalized_existing:
            if existing["timeframe"] != draft.get("timeframe") or existing["category"] != draft.get("category"):
                continue
            if existing["target_memory_file"] != draft.get("target_memory_file") or existing["target_section"] != draft.get("target_section"):
                continue
            score = _similarity_ratio(draft_tokens, set(existing["title_tokens"]) | set(existing["summary_tokens"]))
            if score >= 0.74:
                duplicate_of = existing["id"]
                if existing["status"] == "applied":
                    similar_applied = True
                break
        if duplicate_of:
            draft["duplicate_of_consolidation_id"] = duplicate_of
            draft["conflict_status"] = "conflict"
            evidence = draft.get("evidence") if isinstance(draft.get("evidence"), dict) else {}
            evidence["duplicate_of_consolidation_id"] = duplicate_of
            evidence["similar_applied"] = similar_applied
            conflict_details = evidence.get("conflict_details") if isinstance(evidence.get("conflict_details"), dict) else {}
            reason_codes = conflict_details.get("reason_codes") if isinstance(conflict_details.get("reason_codes"), list) else []
            if "duplicate_of_applied" not in reason_codes:
                reason_codes.append("duplicate_of_applied")
            conflict_details["reason_codes"] = reason_codes
            conflict_details["explanation"] = (
                "Dieser Draft ist einem bestehenden Consolidation-Entwurf sehr ähnlich und sollte erst manuell abgegrenzt oder verworfen werden."
            )
            evidence["conflict_details"] = conflict_details
            draft["evidence"] = evidence
        result = upsert_consolidation_draft(draft)
        if draft.get("consolidation_key") in existing_keys:
            updated += 1
        else:
            created += 1
        rows.append(result)
    return {"created": created, "updated": updated, "drafts": rows}


def get_memory_consolidations(status: str | None = None, timeframe: str | None = None, limit: int = 80, conflict_status: str | None = None) -> list[dict[str, Any]]:
    ensure_core_memory_consolidations_schema()
    ensure_core_memory_review_log_schema()
    limit = max(1, min(200, int(limit)))
    params: list[Any] = []
    clauses: list[str] = []
    if status:
        clauses.append("status=?")
        params.append(status)
    if timeframe and timeframe in TIMEFRAMES:
        clauses.append("timeframe=?")
        params.append(timeframe)
    if conflict_status:
        clauses.append("conflict_status=?")
        params.append(conflict_status)
    sql = "SELECT * FROM core_memory_consolidations"
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY updated_at DESC, id DESC LIMIT ?"
    params.append(limit)
    conn = get_core_db()
    try:
        return [_row_to_consolidation(row) for row in conn.execute(sql, tuple(params)).fetchall()]
    finally:
        conn.close()


def _apply_consolidation_to_memory_file(draft: dict[str, Any], mode: str, note: str | None = None) -> dict[str, Any]:
    target_file = str(draft.get("target_memory_file") or "").strip()
    target_section = str(draft.get("target_section") or "").strip()
    strategy = str(draft.get("proposed_strategy") or "").strip()
    if target_file == "master_profile":
        raise ValueError("invalid_target")
    if strategy not in {"append_section_note", "append_log_entry"}:
        raise ValueError("invalid_strategy")
    if target_file == "health_notes" and strategy == "manual_review_only":
        raise ValueError("invalid_strategy")
    _validate_target(target_file, target_section)
    content = sanitize_memory_content(
        f"### {_utc_iso()[:10]}\n- {draft.get('title')}\n- {draft.get('summary')}" + (f"\n- Review-Notiz: {note}" if note else "")
    )
    if mode == "review_log":
        payload = {
            "write_plan": [
                {"logical_file": "review_log", "section": "LOG_EINTRAEGE", "strategy": "append_log_entry", "content": content}
            ],
            "reason": f"core_memory_consolidation:{draft.get('consolidation_key') or draft.get('id')}",
        }
    else:
        payload = {
            "write_plan": [
                {"logical_file": target_file, "section": target_section, "strategy": strategy, "content": content}
            ],
            "reason": f"core_memory_consolidation:{draft.get('consolidation_key') or draft.get('id')}",
        }
    applied = get_memory_service().apply(payload)
    return {
        "ok": bool(applied.get("success")),
        "mode": mode,
        "memory_apply": applied,
        "target_memory_file": "review_log" if mode == "review_log" else target_file,
        "target_section": "LOG_EINTRAEGE" if mode == "review_log" else target_section,
    }


def apply_consolidation(consolidation_id: Any, mode: str = "review_log", note: str | None = None, actor: str = "system") -> dict[str, Any]:
    ensure_core_memory_consolidations_schema()
    ensure_core_memory_review_log_schema()
    mode_text = str(mode or "review_log").strip().lower()
    if mode_text not in {"review_log", "memory_file"}:
        raise ValueError("invalid_mode")
    conn = get_core_db()
    try:
        row = conn.execute("SELECT * FROM core_memory_consolidations WHERE id=? LIMIT 1", (int(consolidation_id),)).fetchone()
        if not row:
            raise ValueError("consolidation_not_found")
        draft = _row_to_consolidation(row)
        previous_status = draft.get("status")
        applied_result = _apply_consolidation_to_memory_file(draft, mode_text, note=note)
        if not applied_result.get("ok"):
            return draft
        now = _utc_iso()
        conn.execute(
            "UPDATE core_memory_consolidations SET status='applied', updated_at=?, applied_at=?, applied_result_json=? WHERE id=?",
            (now, now, _json_dumps(applied_result, {}), int(consolidation_id)),
        )
        append_memory_review_log(
            "consolidation",
            consolidation_id,
            "apply_review_log" if mode_text == "review_log" else "apply_memory_file",
            previous_status=previous_status,
            new_status="applied",
            note=note,
            actor=actor,
            payload=applied_result,
            conn=conn,
        )
        conn.commit()
        return _row_to_consolidation(conn.execute("SELECT * FROM core_memory_consolidations WHERE id=?", (int(consolidation_id),)).fetchone())
    finally:
        conn.close()


def dismiss_consolidation(consolidation_id: Any, reason: str | None = None, actor: str = "system") -> dict[str, Any]:
    ensure_core_memory_consolidations_schema()
    ensure_core_memory_review_log_schema()
    conn = get_core_db()
    try:
        row = conn.execute("SELECT * FROM core_memory_consolidations WHERE id=? LIMIT 1", (int(consolidation_id),)).fetchone()
        if not row:
            raise ValueError("consolidation_not_found")
        current = _row_to_consolidation(row)
        now = _utc_iso()
        conn.execute(
            "UPDATE core_memory_consolidations SET status='dismissed', updated_at=?, dismissed_at=?, dismissed_reason=? WHERE id=?",
            (now, now, _text(reason, 400), int(consolidation_id)),
        )
        append_memory_review_log(
            "consolidation",
            consolidation_id,
            "dismiss",
            previous_status=current.get("status"),
            new_status="dismissed",
            reason=reason,
            actor=actor,
            payload={"dismissed_reason": _text(reason, 400)},
            conn=conn,
        )
        conn.commit()
        return _row_to_consolidation(conn.execute("SELECT * FROM core_memory_consolidations WHERE id=?", (int(consolidation_id),)).fetchone())
    finally:
        conn.close()


def control_consolidation(
    consolidation_id: Any,
    action: str,
    mode: str | None = None,
    edits: dict[str, Any] | None = None,
    note: str | None = None,
    actor: str = "system",
) -> dict[str, Any]:
    ensure_core_memory_consolidations_schema()
    ensure_core_memory_review_log_schema()
    action_text = str(action or "").strip().lower()
    if action_text not in CONSOLIDATION_ACTIONS:
        raise ValueError("invalid_action")
    if action_text == "dismiss":
        return dismiss_consolidation(consolidation_id, reason=note, actor=actor)
    if action_text == "apply":
        return apply_consolidation(consolidation_id, mode=mode or "review_log", note=note, actor=actor)
    conn = get_core_db()
    try:
        row = conn.execute("SELECT * FROM core_memory_consolidations WHERE id=? LIMIT 1", (int(consolidation_id),)).fetchone()
        if not row:
            raise ValueError("consolidation_not_found")
        current = _row_to_consolidation(row)
        updates: dict[str, Any] = {}
        for key, value in (edits or {}).items():
            if key not in CONSOLIDATION_EDITABLE_FIELDS:
                raise ValueError("invalid_update_field")
            if key == "timeframe":
                value = str(value or "").strip().lower()
                if value not in TIMEFRAMES:
                    raise ValueError("invalid_timeframe")
            if key == "proposed_strategy":
                value = str(value or "").strip().lower()
                if value not in CONSOLIDATION_STRATEGIES:
                    raise ValueError("invalid_strategy")
            if key == "confidence":
                value = _safe_confidence(value)
            else:
                value = _text(value, 900 if key == "summary" else 180)
            updates[key] = value
        merged = {**current, **updates}
        _validate_target(merged.get("target_memory_file"), merged.get("target_section"))
        merged = _enrich_consolidation_diagnostics(merged)
        conn.execute(
            """
            UPDATE core_memory_consolidations
            SET updated_at=?, timeframe=COALESCE(?, timeframe), category=COALESCE(?, category), title=COALESCE(?, title),
                summary=COALESCE(?, summary), target_memory_file=COALESCE(?, target_memory_file), target_section=COALESCE(?, target_section),
                proposed_strategy=COALESCE(?, proposed_strategy), confidence=?, evidence_json=?
            WHERE id=?
            """,
            (
                _utc_iso(),
                updates.get("timeframe"),
                updates.get("category"),
                updates.get("title"),
                updates.get("summary"),
                updates.get("target_memory_file"),
                updates.get("target_section"),
                updates.get("proposed_strategy"),
                updates.get("confidence", current.get("confidence")),
                _json_dumps(merged.get("evidence"), {}),
                int(consolidation_id),
            ),
        )
        append_memory_review_log(
            "consolidation",
            consolidation_id,
            "update",
            previous_status=current.get("status"),
            new_status=current.get("status"),
            note=note,
            actor=actor,
            payload={"edits": updates},
            conn=conn,
        )
        conn.commit()
        return _row_to_consolidation(conn.execute("SELECT * FROM core_memory_consolidations WHERE id=?", (int(consolidation_id),)).fetchone())
    finally:
        conn.close()


def batch_control_consolidations(
    consolidation_ids: list[Any] | None,
    action: str,
    *,
    mode: str | None = None,
    note: str | None = None,
    reason: str | None = None,
    explicit_confirm_all: bool = False,
    status: str | None = None,
    timeframe: str | None = None,
    conflict_status: str | None = None,
    actor: str = "system",
) -> dict[str, Any]:
    action_text = str(action or "").strip().lower()
    if action_text not in CONSOLIDATION_BATCH_ACTIONS:
        raise ValueError("invalid_action")
    resolved_ids = [int(raw_id) for raw_id in (consolidation_ids or []) if str(raw_id).strip()]
    if not resolved_ids:
        if not explicit_confirm_all:
            raise ValueError("selection_required")
        resolved_ids = [int(row["id"]) for row in get_memory_consolidations(status=status, timeframe=timeframe, limit=200, conflict_status=conflict_status)]
    changed: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    batch_note = note or reason
    for raw_id in resolved_ids:
        try:
            consolidation_id = int(raw_id)
            if action_text == "apply_review_log":
                changed.append(control_consolidation(consolidation_id, "apply", mode=mode or "review_log", note=batch_note, actor=actor))
            elif action_text == "dismiss":
                changed.append(control_consolidation(consolidation_id, "dismiss", note=batch_note, actor=actor))
            elif action_text == "mark_needs_review":
                changed.append(control_consolidation(consolidation_id, "update", edits={"proposed_strategy": "manual_review_only"}, note=batch_note, actor=actor))
                conn = get_core_db()
                try:
                    append_memory_review_log(
                        "consolidation",
                        consolidation_id,
                        "mark_needs_review",
                        previous_status=changed[-1].get("status"),
                        new_status=changed[-1].get("status"),
                        note=batch_note,
                        actor=actor,
                        payload={"mode": mode or "review_log"},
                        conn=conn,
                    )
                    conn.commit()
                finally:
                    conn.close()
        except Exception as exc:
            errors.append({"id": raw_id, "error": str(exc)})
    if action_text in {"apply_review_log", "dismiss"}:
        review_action = "batch_apply_review_log" if action_text == "apply_review_log" else "batch_dismiss"
        for item in changed:
            conn = get_core_db()
            try:
                append_memory_review_log(
                    "consolidation",
                    item.get("id"),
                    review_action,
                    previous_status=item.get("status"),
                    new_status=item.get("status"),
                    note=note,
                    reason=reason,
                    actor=actor,
                    payload={"batch_ids": resolved_ids, "mode": mode or "review_log"},
                    conn=conn,
                )
                conn.commit()
            finally:
                conn.close()
    return {"changed": changed, "errors": errors, "selection_count": len(resolved_ids)}


def _validate_target(target_memory_file: str | None, target_section: str | None) -> None:
    if not target_memory_file and not target_section:
        return
    file_key = str(target_memory_file or "").strip()
    section_key = str(target_section or "").strip()
    if file_key not in TARGET_SECTIONS:
        raise ValueError("invalid_target")
    if section_key not in TARGET_SECTIONS[file_key]:
        raise ValueError("invalid_target")


def _safe_confidence(value: Any) -> float | None:
    if value in (None, ""):
        return None
    parsed = float(value)
    if parsed > 1:
        parsed = parsed / 100.0
    return round(max(0.0, min(1.0, parsed)), 2)


def _control_update_fields(current: dict[str, Any], edits: dict[str, Any]) -> dict[str, Any]:
    updates: dict[str, Any] = {}
    for key, value in (edits or {}).items():
        if key not in EDITABLE_FIELDS:
            raise ValueError("invalid_update_field")
        if key == "timeframe":
            timeframe = str(value or "").strip().lower()
            if timeframe not in TIMEFRAMES:
                raise ValueError("invalid_timeframe")
            updates[key] = timeframe
        elif key == "write_policy":
            policy = str(value or "").strip().lower()
            if policy not in WRITE_POLICIES:
                raise ValueError("invalid_write_policy")
            updates[key] = policy
        elif key == "confidence":
            updates[key] = _safe_confidence(value)
        else:
            updates[key] = _text(value, 600 if key == "summary" else 180)
    merged = {**current, **updates}
    target_file = merged.get("target_memory_file")
    target_section = merged.get("target_section")
    if "timeframe" in updates and ("target_memory_file" not in updates or "target_section" not in updates):
        routed_file, routed_section = route_memory_target(merged)
        merged["target_memory_file"] = updates.get("target_memory_file") or routed_file
        merged["target_section"] = updates.get("target_section") or routed_section
    _validate_target(merged.get("target_memory_file"), merged.get("target_section"))
    updates["event_key"] = _event_key(merged)
    if "target_memory_file" not in updates:
        updates["target_memory_file"] = merged.get("target_memory_file")
    if "target_section" not in updates:
        updates["target_section"] = merged.get("target_section")
    return updates


def control_memory_event(event_id: Any, action: str, edits: dict[str, Any] | None = None, note: str | None = None, actor: str = "system") -> dict[str, Any]:
    ensure_core_memory_events_schema()
    ensure_core_memory_review_log_schema()
    action_text = str(action or "").strip().lower()
    if action_text not in CONTROL_ACTIONS:
        raise ValueError("invalid_action")
    conn = get_core_db()
    try:
        row = conn.execute("SELECT * FROM core_memory_events WHERE id=? LIMIT 1", (int(event_id),)).fetchone()
        if not row:
            raise ValueError("event_not_found")
        event = _row_to_event(row)
        now = _utc_iso()
        applied_result = event.get("applied_result") if isinstance(event.get("applied_result"), dict) else {}
        applied_result = dict(applied_result)
        if note:
            applied_result["note"] = note
        applied_result["action"] = action_text

        previous_status = event.get("status")
        if action_text == "update":
            updates = _control_update_fields(event, edits or {})
            conn.execute(
                """
                UPDATE core_memory_events
                SET updated_at=?, title=COALESCE(?, title), summary=COALESCE(?, summary), timeframe=COALESCE(?, timeframe),
                    category=COALESCE(?, category), confidence=?, target_memory_file=COALESCE(?, target_memory_file),
                    target_section=COALESCE(?, target_section), write_policy=COALESCE(?, write_policy), event_key=COALESCE(?, event_key),
                    applied_result_json=?
                WHERE id=?
                """,
                (
                    now,
                    updates.get("title"),
                    updates.get("summary"),
                    updates.get("timeframe"),
                    updates.get("category"),
                    updates.get("confidence", event.get("confidence")),
                    updates.get("target_memory_file"),
                    updates.get("target_section"),
                    updates.get("write_policy"),
                    updates.get("event_key"),
                    _json_dumps(applied_result, {}),
                    int(event_id),
                ),
            )
            append_memory_review_log(
                "event",
                event_id,
                "update",
                previous_status=previous_status,
                new_status=previous_status,
                note=note,
                actor=actor,
                payload={"edits": updates},
                conn=conn,
            )
        elif action_text == "dismiss":
            conn.execute(
                "UPDATE core_memory_events SET status='dismissed', updated_at=?, applied_result_json=? WHERE id=?",
                (now, _json_dumps(applied_result, {}), int(event_id)),
            )
            append_memory_review_log("event", event_id, "dismiss", previous_status=previous_status, new_status="dismissed", note=note, actor=actor, payload=applied_result, conn=conn)
        elif action_text == "keep_pending":
            conn.execute(
                "UPDATE core_memory_events SET status='pending', updated_at=?, applied_result_json=? WHERE id=?",
                (now, _json_dumps(applied_result, {}), int(event_id)),
            )
            append_memory_review_log("event", event_id, "keep_pending", previous_status=previous_status, new_status="pending", note=note, actor=actor, payload=applied_result, conn=conn)
        elif action_text == "promote":
            if edits:
                updates = _control_update_fields(event, edits)
                event = {**event, **updates}
                conn.execute(
                    """
                    UPDATE core_memory_events
                    SET title=COALESCE(?, title), summary=COALESCE(?, summary), timeframe=COALESCE(?, timeframe),
                        category=COALESCE(?, category), confidence=?, target_memory_file=COALESCE(?, target_memory_file),
                        target_section=COALESCE(?, target_section), write_policy=COALESCE(?, write_policy), event_key=COALESCE(?, event_key)
                    WHERE id=?
                    """,
                    (
                        updates.get("title"),
                        updates.get("summary"),
                        updates.get("timeframe"),
                        updates.get("category"),
                        updates.get("confidence", event.get("confidence")),
                        updates.get("target_memory_file"),
                        updates.get("target_section"),
                        updates.get("write_policy"),
                        updates.get("event_key"),
                        int(event_id),
                    ),
                )
            target_file = event.get("target_memory_file")
            applied_result.setdefault("applied", False)
            if target_file in {"athlete_dossier", "core_patterns", "master_profile"}:
                applied_result["promotion_policy"] = "Promoted as candidate for later dossier consolidation."
            conn.execute(
                "UPDATE core_memory_events SET status='promoted', updated_at=?, applied_result_json=? WHERE id=?",
                (now, _json_dumps(applied_result, {}), int(event_id)),
            )
            append_memory_review_log(
                "event",
                event_id,
                "promote",
                previous_status=previous_status,
                new_status="promoted",
                note=note,
                actor=actor,
                payload=applied_result,
                conn=conn,
            )
        elif action_text == "apply_review_log":
            refreshed = _row_to_event(conn.execute("SELECT * FROM core_memory_events WHERE id=? LIMIT 1", (int(event_id),)).fetchone())
            if not _is_review_log_applied(refreshed):
                review_result = _apply_review_log(refreshed, note=note, reason=f"core_memory_control:{action_text}:{refreshed.get('event_key') or event_id}")
                applied_result.update(review_result)
                conn.execute(
                    "UPDATE core_memory_events SET status='promoted', updated_at=?, applied_at=?, applied_result_json=? WHERE id=?",
                    (now, now if review_result.get("ok") else None, _json_dumps(applied_result, {}), int(event_id)),
                )
            else:
                applied_result["review_log_already_applied"] = True
                conn.execute(
                    "UPDATE core_memory_events SET status='promoted', updated_at=?, applied_result_json=? WHERE id=?",
                    (now, _json_dumps(applied_result, {}), int(event_id)),
                )
            append_memory_review_log(
                "event",
                event_id,
                "apply_review_log",
                previous_status=previous_status,
                new_status="promoted",
                note=note,
                actor=actor,
                payload=applied_result,
                conn=conn,
            )
        conn.commit()
        return _row_to_event(conn.execute("SELECT * FROM core_memory_events WHERE id=?", (int(event_id),)).fetchone())
    finally:
        conn.close()


def promote_memory_event(event_id: Any, mode: str = "apply") -> dict[str, Any]:
    ensure_core_memory_events_schema()
    mode_text = str(mode or "apply").strip().lower()
    if mode_text == "apply":
        conn = get_core_db()
        try:
            row = conn.execute("SELECT target_memory_file FROM core_memory_events WHERE id=? LIMIT 1", (int(event_id),)).fetchone()
        finally:
            conn.close()
        target_file = str((row["target_memory_file"] if row else "") or "").strip()
        if target_file == "review_log":
            return control_memory_event(event_id, "apply_review_log")
        return control_memory_event(event_id, "promote", note="Promoted as candidate for later dossier consolidation.")
    return control_memory_event(event_id, "promote")
