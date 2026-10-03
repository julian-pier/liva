from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional
from zoneinfo import ZoneInfo

from database.connections import get_nutrition_db
from nutrition.nutrition_planning_db import get_logging_day_payload

BERLIN = ZoneInfo("Europe/Berlin")

TERMINAL_INTERNAL_STATES = {
    "logged",
    "skipped",
    "replaced",
    "cancelled_by_replan",
    "merged_into_other_meal",
    "fulfilled_by_alternative",
}

OPEN_INTERNAL_STATES = {
    "planned",
    "upcoming",
    "due_now",
    "missed_window",
    "shifted_by_core",
    "portion_adjusted_by_core",
    "accepted_adjustment",
    "switched",
}

VISIBLE_STATE_LABELS = {
    "planned": "offen",
    "upcoming": "offen",
    "due_now": "offen",
    "missed_window": "offen",
    "shifted_by_core": "angepasst",
    "portion_adjusted_by_core": "angepasst",
    "accepted_adjustment": "angepasst",
    "switched": "getauscht",
    "logged": "geloggt",
    "skipped": "ausgelassen",
    "replaced": "ersetzt",
    "cancelled_by_replan": "entfaellt",
    "merged_into_other_meal": "zusammengefuehrt",
    "fulfilled_by_alternative": "anders geloest",
}

COMPAT_STATUS_BY_INTERNAL = {
    "planned": "open",
    "upcoming": "open",
    "due_now": "open",
    "missed_window": "open",
    "shifted_by_core": "shifted",
    "portion_adjusted_by_core": "adjusted_by_core",
    "accepted_adjustment": "adjusted_by_core",
    "switched": "adjusted_by_core",
    "logged": "logged",
    "skipped": "skipped",
    "replaced": "adjusted_by_core",
    "cancelled_by_replan": "skipped",
    "merged_into_other_meal": "skipped",
    "fulfilled_by_alternative": "skipped",
}


def _utcnow_iso() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat()


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        out = float(value)
        if out != out:  # NaN
            return default
        return out
    except Exception:
        return default


def _safe_json(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except Exception:
        return "{}"


def _parse_hhmm(text: Any) -> Optional[int]:
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


def _to_hhmm(minutes: int) -> str:
    bounded = max(0, min(23 * 60 + 59, int(minutes)))
    return f"{bounded // 60:02d}:{bounded % 60:02d}"


def ensure_core_day_state_schema() -> None:
    conn = get_nutrition_db()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS nutrition_day_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                day_iso TEXT NOT NULL,
                meal_slot_id INTEGER NULL,
                event_type TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                source TEXT NOT NULL DEFAULT 'core',
                reason_code TEXT NULL,
                reason_text TEXT NULL,
                dedupe_key TEXT NOT NULL,
                payload_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                superseded_by_event_id INTEGER NULL,
                UNIQUE(day_iso, dedupe_key)
            )
            """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_nutrition_day_events_day ON nutrition_day_events(day_iso, id ASC)"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_nutrition_day_events_slot ON nutrition_day_events(day_iso, meal_slot_id, id ASC)"
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS nutrition_active_day_plan (
                day_iso TEXT PRIMARY KEY,
                signature TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                rebuilt_at TEXT NOT NULL
            )
            """
        )
        conn.commit()
    finally:
        conn.close()


def _event_dedupe_key(day_iso: str, meal_slot_id: int | None, event_type: str, payload: dict[str, Any]) -> str:
    source = {
        "day_iso": day_iso,
        "slot": int(meal_slot_id or 0),
        "event_type": str(event_type or "").strip().lower(),
        "payload": payload,
    }
    raw = _safe_json(source)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def append_day_event(
    *,
    day_iso: str,
    event_type: str,
    meal_slot_id: int | None = None,
    payload: dict[str, Any] | None = None,
    source: str = "core",
    reason_code: str | None = None,
    reason_text: str | None = None,
    dedupe_key: str | None = None,
) -> dict[str, Any]:
    ensure_core_day_state_schema()
    payload_obj = payload if isinstance(payload, dict) else {}
    key = str(dedupe_key or "").strip() or _event_dedupe_key(day_iso, meal_slot_id, event_type, payload_obj)
    conn = get_nutrition_db()
    conn.row_factory = None
    try:
        cur = conn.cursor()
        now = _utcnow_iso()
        cur.execute(
            """
            INSERT OR IGNORE INTO nutrition_day_events (
                day_iso, meal_slot_id, event_type, status, source, reason_code, reason_text,
                dedupe_key, payload_json, created_at, superseded_by_event_id
            ) VALUES (?, ?, ?, 'active', ?, ?, ?, ?, ?, ?, NULL)
            """,
            (
                day_iso,
                int(meal_slot_id) if meal_slot_id else None,
                str(event_type or "").strip(),
                str(source or "core").strip() or "core",
                str(reason_code or "").strip() or None,
                str(reason_text or "").strip() or None,
                key,
                _safe_json(payload_obj),
                now,
            ),
        )
        row = cur.execute(
            """
            SELECT id, day_iso, meal_slot_id, event_type, status, source, reason_code, reason_text,
                   dedupe_key, payload_json, created_at, superseded_by_event_id
            FROM nutrition_day_events
            WHERE day_iso=? AND dedupe_key=?
            LIMIT 1
            """,
            (day_iso, key),
        ).fetchone()
        event_id = int(row[0]) if row else 0
        # Keep only one active suggestion per slot.
        if event_id > 0 and str(event_type or "").strip() == "suggestion_created" and meal_slot_id:
            cur.execute(
                """
                UPDATE nutrition_day_events
                SET status='superseded', superseded_by_event_id=?
                WHERE day_iso=?
                  AND meal_slot_id=?
                  AND event_type='suggestion_created'
                  AND status='active'
                  AND id<>?
                """,
                (event_id, day_iso, int(meal_slot_id), event_id),
            )
        # Keep latest switch/replace as active truth for the slot.
        if event_id > 0 and str(event_type or "").strip() in {"meal_switched", "meal_replaced"} and meal_slot_id:
            cur.execute(
                """
                UPDATE nutrition_day_events
                SET status='superseded', superseded_by_event_id=?
                WHERE day_iso=?
                  AND meal_slot_id=?
                  AND event_type IN ('meal_switched', 'meal_replaced')
                  AND status='active'
                  AND id<>?
                """,
                (event_id, day_iso, int(meal_slot_id), event_id),
            )
        conn.commit()
    finally:
        conn.close()

    return {
        "id": event_id,
        "day_iso": day_iso,
        "meal_slot_id": int(meal_slot_id) if meal_slot_id else None,
        "event_type": str(event_type or "").strip(),
        "source": str(source or "core").strip() or "core",
        "reason_code": str(reason_code or "").strip() or None,
        "reason_text": str(reason_text or "").strip() or None,
        "payload": payload_obj,
        "dedupe_key": key,
    }


def list_day_events(day_iso: str) -> list[dict[str, Any]]:
    ensure_core_day_state_schema()
    conn = get_nutrition_db()
    conn.row_factory = None
    try:
        rows = conn.execute(
            """
            SELECT id, day_iso, meal_slot_id, event_type, status, source, reason_code, reason_text,
                   dedupe_key, payload_json, created_at, superseded_by_event_id
            FROM nutrition_day_events
            WHERE day_iso=?
            ORDER BY id ASC
            """,
            (day_iso,),
        ).fetchall()
    finally:
        conn.close()
    out: list[dict[str, Any]] = []
    for row in rows:
        payload_obj: dict[str, Any]
        try:
            payload_obj = json.loads(row[9] or "{}") if row[9] else {}
            if not isinstance(payload_obj, dict):
                payload_obj = {}
        except Exception:
            payload_obj = {}
        out.append(
            {
                "id": int(row[0]),
                "day_iso": str(row[1]),
                "meal_slot_id": int(row[2]) if row[2] is not None else None,
                "event_type": str(row[3] or ""),
                "status": str(row[4] or ""),
                "source": str(row[5] or ""),
                "reason_code": str(row[6] or "") or None,
                "reason_text": str(row[7] or "") or None,
                "dedupe_key": str(row[8] or ""),
                "payload": payload_obj,
                "created_at": str(row[10] or ""),
                "superseded_by_event_id": int(row[11]) if row[11] is not None else None,
            }
        )
    return out


@dataclass
class _MealNode:
    slot_id: int
    slot_index: int
    base_title: str
    base_time: str
    base_items: list[dict[str, Any]]
    base_macros: dict[str, Any]
    effective_name: str
    effective_time: str
    effective_items: list[dict[str, Any]]
    effective_macros: dict[str, Any]
    state: str
    source_of_truth: str
    last_mutation_type: str | None
    last_mutation_at: str | None
    reminder_eligible: bool
    stale_base_reference_blocked: bool
    pending_suggestion: dict[str, Any] | None
    base_status: str
    status_source: str
    trace: dict[str, Any] | None
    logged_meal: dict[str, Any] | None

    def to_payload(self) -> dict[str, Any]:
        comp = COMPAT_STATUS_BY_INTERNAL.get(self.state, "open")
        if self.state == "logged" and self.base_status in {"manual_override", "changed"}:
            comp = self.base_status
        return {
            "slot_id": self.slot_id,
            "slot_index": self.slot_index,
            "time_text": self.effective_time,
            "shifted_time_text": self.effective_time if self.effective_time != self.base_time else None,
            "title": self.effective_name,
            "items": self.effective_items,
            "macros": self.effective_macros,
            "status": comp,
            "state": self.state,
            "state_label": VISIBLE_STATE_LABELS.get(self.state, "offen"),
            "source_of_truth": self.source_of_truth,
            "last_mutation_type": self.last_mutation_type,
            "last_mutation_at": self.last_mutation_at,
            "reminder_eligible": bool(self.reminder_eligible),
            "stale_base_reference_blocked": bool(self.stale_base_reference_blocked),
            "pending_suggestion": self.pending_suggestion,
            "base_title": self.base_title,
            "base_time_text": self.base_time,
            "base_status": self.base_status,
            "status_source": self.status_source,
            "trace": self.trace,
            "logged_meal": self.logged_meal,
        }


def _derive_internal_from_base_status(status: str, status_source: str, logged_meal: dict[str, Any] | None) -> str:
    status_norm = str(status or "").strip().lower() or "open"
    source_norm = str(status_source or "").strip().lower() or "user"
    if status_norm in {"logged", "telegram_confirmed", "changed", "manual_override"} or isinstance(logged_meal, dict):
        return "logged"
    if status_norm == "skipped":
        return "skipped"
    if status_norm == "adjusted_by_core":
        return "portion_adjusted_by_core" if source_norm == "core" else "accepted_adjustment"
    if status_norm == "shifted":
        return "shifted_by_core" if source_norm == "core" else "accepted_adjustment"
    return "planned"


def _sum_macros(rows: list[dict[str, Any]]) -> dict[str, float]:
    kcal = 0.0
    p = 0.0
    c = 0.0
    f = 0.0
    for row in rows:
        macros = row.get("macros") if isinstance(row.get("macros"), dict) else {}
        kcal += _safe_float(macros.get("kcal"), 0.0)
        p += _safe_float(macros.get("p"), 0.0)
        c += _safe_float(macros.get("c"), 0.0)
        f += _safe_float(macros.get("f"), 0.0)
    return {"kcal": round(kcal, 1), "p": round(p, 1), "c": round(c, 1), "f": round(f, 1)}


def _node_from_base(meal: dict[str, Any]) -> _MealNode:
    slot_id = _safe_int(meal.get("slot_id"), 0)
    slot_index = _safe_int(meal.get("slot_index"), 0)
    base_title = str(meal.get("title") or "Meal").strip() or "Meal"
    base_time = str(meal.get("shifted_time_text") or meal.get("time_text") or "").strip() or "--:--"
    logged_meal = meal.get("logged_meal") if isinstance(meal.get("logged_meal"), dict) else None
    state = _derive_internal_from_base_status(
        str(meal.get("status") or "open"),
        str(meal.get("status_source") or "user"),
        logged_meal,
    )
    effective_title = base_title
    effective_items = [dict(i) for i in (meal.get("items") or []) if isinstance(i, dict)]
    effective_macros = dict(meal.get("macros") or {})
    if state == "logged" and logged_meal:
        effective_title = str(logged_meal.get("title") or effective_title).strip() or effective_title
        effective_items = [dict(i) for i in (logged_meal.get("items") or []) if isinstance(i, dict)]
        effective_macros = dict(logged_meal.get("macros") or effective_macros)
    return _MealNode(
        slot_id=slot_id,
        slot_index=slot_index,
        base_title=base_title,
        base_time=base_time,
        base_items=[dict(i) for i in (meal.get("items") or []) if isinstance(i, dict)],
        base_macros=dict(meal.get("macros") or {}),
        effective_name=effective_title,
        effective_time=base_time,
        effective_items=effective_items,
        effective_macros=effective_macros,
        state=state,
        source_of_truth="logged_reality" if state == "logged" else "base_plan",
        last_mutation_type=str(meal.get("reason_code") or "").strip() or None,
        last_mutation_at=None,
        reminder_eligible=state not in TERMINAL_INTERNAL_STATES and state != "logged",
        stale_base_reference_blocked=False,
        pending_suggestion=None,
        base_status=str(meal.get("status") or "open").strip().lower() or "open",
        status_source=str(meal.get("status_source") or "user").strip().lower() or "user",
        trace=meal.get("trace") if isinstance(meal.get("trace"), dict) else None,
        logged_meal=logged_meal,
    )


def _apply_event_to_node(node: _MealNode, event: dict[str, Any]) -> None:
    ev_type = str(event.get("event_type") or "").strip().lower()
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    created_at = str(event.get("created_at") or "").strip() or None
    if ev_type == "meal_skipped":
        if node.state == "logged":
            return
        node.state = "skipped"
        node.source_of_truth = "user_action"
        node.last_mutation_type = "meal_skipped"
        node.last_mutation_at = created_at
        node.reminder_eligible = False
        return
    if ev_type == "meal_logged":
        node.state = "logged"
        node.source_of_truth = "logged_reality"
        node.last_mutation_type = "meal_logged"
        node.last_mutation_at = created_at
        node.reminder_eligible = False
        if payload.get("title"):
            node.effective_name = str(payload.get("title") or node.effective_name).strip() or node.effective_name
        if isinstance(payload.get("items"), list):
            node.effective_items = [dict(i) for i in payload.get("items") if isinstance(i, dict)]
        if isinstance(payload.get("macros"), dict):
            node.effective_macros = dict(payload.get("macros") or {})
        if payload.get("time_text"):
            node.effective_time = str(payload.get("time_text") or node.effective_time).strip() or node.effective_time
        return
    if ev_type in {"meal_shifted", "core_shifted_time"}:
        if node.state == "logged":
            return
        to_time = str(payload.get("to_time") or payload.get("time_text") or "").strip()
        if to_time:
            node.effective_time = to_time
        if node.state not in TERMINAL_INTERNAL_STATES and node.state != "logged":
            node.state = "shifted_by_core" if str(event.get("source") or "").strip().lower() == "core" else "accepted_adjustment"
            node.reminder_eligible = True
        node.source_of_truth = "core_adjustment"
        node.last_mutation_type = "meal_shifted"
        node.last_mutation_at = created_at
        return
    if ev_type in {"meal_switched", "meal_replaced"}:
        if node.state == "logged":
            return
        new_title = str(payload.get("title") or payload.get("effective_name") or "").strip()
        if new_title:
            node.effective_name = new_title
        if isinstance(payload.get("items"), list):
            node.effective_items = [dict(i) for i in payload.get("items") if isinstance(i, dict)]
        if isinstance(payload.get("macros"), dict):
            node.effective_macros = dict(payload.get("macros") or {})
        if payload.get("time_text"):
            node.effective_time = str(payload.get("time_text") or node.effective_time).strip() or node.effective_time
        node.state = "switched" if ev_type == "meal_switched" else "replaced"
        node.source_of_truth = ev_type
        node.last_mutation_type = ev_type
        node.last_mutation_at = created_at
        node.reminder_eligible = True
        node.stale_base_reference_blocked = bool(
            node.base_title and node.effective_name and node.base_title.strip().lower() != node.effective_name.strip().lower()
        )
        return
    if ev_type == "meal_resolved_by_alternative":
        if node.state == "logged":
            return
        node.state = "fulfilled_by_alternative"
        node.source_of_truth = "user_action"
        node.last_mutation_type = "meal_resolved_by_alternative"
        node.last_mutation_at = created_at
        node.reminder_eligible = False
        return
    if ev_type == "core_adjusted_portion":
        if node.state == "logged":
            return
        if isinstance(payload.get("macros"), dict):
            node.effective_macros = dict(payload.get("macros") or {})
        node.state = "portion_adjusted_by_core"
        node.source_of_truth = "core_adjustment"
        node.last_mutation_type = "core_adjusted_portion"
        node.last_mutation_at = created_at
        node.reminder_eligible = True


def build_active_day_plan(day_iso: str, *, base_payload: dict[str, Any] | None = None) -> dict[str, Any]:
    ensure_core_day_state_schema()
    base = dict(base_payload) if isinstance(base_payload, dict) else get_logging_day_payload(day_iso)
    planned = [m for m in (base.get("planned_meals") or []) if isinstance(m, dict)]
    nodes: dict[int, _MealNode] = {}
    for meal in planned:
        node = _node_from_base(meal)
        if node.slot_id > 0:
            nodes[node.slot_id] = node

    events = list_day_events(day_iso)
    pending_suggestion_by_slot: dict[int, dict[str, Any]] = {}
    pending_by_key: dict[str, dict[str, Any]] = {}
    for event in events:
        if str(event.get("status") or "").strip().lower() != "active":
            continue
        slot_id = _safe_int(event.get("meal_slot_id"), 0)
        ev_type = str(event.get("event_type") or "").strip().lower()
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        if ev_type == "suggestion_created":
            key = str(payload.get("suggestion_key") or event.get("dedupe_key") or event.get("id") or "")
            pending_by_key[key] = event
            if slot_id > 0:
                pending_suggestion_by_slot[slot_id] = event
            continue
        if ev_type in {"suggestion_accepted", "suggestion_rejected", "suggestion_deferred", "suggestion_expired"}:
            key = str(payload.get("suggestion_key") or "")
            if key and key in pending_by_key:
                pending_slot = _safe_int((pending_by_key[key].get("meal_slot_id")), 0)
                pending_by_key.pop(key, None)
                if pending_slot > 0 and pending_suggestion_by_slot.get(pending_slot):
                    pending_suggestion_by_slot.pop(pending_slot, None)
            continue
        if slot_id <= 0:
            continue
        node = nodes.get(slot_id)
        if not node:
            continue
        _apply_event_to_node(node, event)

    now_min = _parse_hhmm(datetime.now(BERLIN).strftime("%H:%M")) or 0
    planned_out: list[dict[str, Any]] = []
    stale_blocks: list[dict[str, Any]] = []
    for slot_id, node in sorted(nodes.items(), key=lambda pair: (pair[1].slot_index, pair[0])):
        if node.slot_id in pending_suggestion_by_slot:
            node.pending_suggestion = pending_suggestion_by_slot[node.slot_id]
        tmin = _parse_hhmm(node.effective_time)
        if node.state in OPEN_INTERNAL_STATES and tmin is not None and tmin + 45 < now_min:
            node.state = "missed_window"
        if node.state in TERMINAL_INTERNAL_STATES or node.state == "logged":
            node.reminder_eligible = False
        row = node.to_payload()
        if row.get("stale_base_reference_blocked"):
            stale_blocks.append(
                {
                    "slot_id": node.slot_id,
                    "base_title": node.base_title,
                    "effective_title": node.effective_name,
                }
            )
        planned_out.append(row)

    planned_totals = _sum_macros([m for m in planned_out if str(m.get("state") or "") not in {"logged"}])
    logged_totals = base.get("logged_totals") if isinstance(base.get("logged_totals"), dict) else {"kcal": 0.0, "p": 0.0, "c": 0.0, "f": 0.0}
    targets = base.get("targets") if isinstance(base.get("targets"), dict) else {"kcal": 0.0, "p": 0.0, "c": 0.0, "f": 0.0}
    remaining = {
        "kcal": round(_safe_float(targets.get("kcal"), 0.0) - _safe_float(logged_totals.get("kcal"), 0.0), 1),
        "p": round(_safe_float(targets.get("p"), 0.0) - _safe_float(logged_totals.get("p"), 0.0), 1),
        "c": round(_safe_float(targets.get("c"), 0.0) - _safe_float(logged_totals.get("c"), 0.0), 1),
        "f": round(_safe_float(targets.get("f"), 0.0) - _safe_float(logged_totals.get("f"), 0.0), 1),
    }

    snapshot_payload = {
        "day_iso": day_iso,
        "planned_meals": [
            {
                "slot_id": m.get("slot_id"),
                "title": m.get("title"),
                "time_text": m.get("time_text"),
                "state": m.get("state"),
                "source_of_truth": m.get("source_of_truth"),
            }
            for m in planned_out
        ],
        "events_count": len(events),
    }
    signature = hashlib.sha1(_safe_json(snapshot_payload).encode("utf-8")).hexdigest()
    conn = get_nutrition_db()
    try:
        conn.execute(
            """
            INSERT INTO nutrition_active_day_plan (day_iso, signature, payload_json, rebuilt_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(day_iso) DO UPDATE SET
                signature=excluded.signature,
                payload_json=excluded.payload_json,
                rebuilt_at=excluded.rebuilt_at
            """,
            (day_iso, signature, _safe_json(snapshot_payload), _utcnow_iso()),
        )
        conn.commit()
    finally:
        conn.close()

    out = dict(base)
    out["planned_meals"] = planned_out
    out["planned_totals"] = planned_totals
    out["remaining"] = remaining
    out["active_day_plan"] = {
        "signature": signature,
        "events_count": len(events),
        "open_meals": len([m for m in planned_out if str(m.get("state") or "") in OPEN_INTERNAL_STATES]),
        "pending_suggestions": len(pending_by_key),
    }
    out["event_history"] = events
    out["stale_reference_blocks"] = stale_blocks
    return out


__all__ = [
    "ensure_core_day_state_schema",
    "append_day_event",
    "list_day_events",
    "build_active_day_plan",
    "VISIBLE_STATE_LABELS",
]
