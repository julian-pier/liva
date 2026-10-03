from __future__ import annotations

import hashlib
import json
import os
import uuid
from datetime import datetime
from typing import Any, Dict, List, Tuple

from flask import Blueprint, jsonify, request

from database.connections import get_nutrition_db
from nutrition.nutrition_planning_db import ensure_nutrition_planning_schema, get_live_macro_targets
from security.write_guard import require_ai_write, require_intent

api_aiw_nutrition = Blueprint("api_aiw_nutrition", __name__, url_prefix="/api/aiw")


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _json_error(status: int, error_code: str, message: str, detail: Dict[str, Any] | None = None):
    return jsonify({"ok": False, "error_code": error_code, "message": message, "detail": detail or {}}), status


def _json_ok(data: Dict[str, Any], warnings: List[Dict[str, Any]] | None = None):
    return jsonify({"ok": True, "data": data, "warnings": warnings or []})


def _ensure_ai_schema():
    ensure_nutrition_planning_schema()
    conn = get_nutrition_db()
    cur = conn.cursor()

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS mealplan_templates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            targets_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            created_by TEXT NULL,
            source TEXT NULL
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS mealplan_template_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            template_id INTEGER NOT NULL,
            food_id INTEGER NOT NULL,
            qty REAL NOT NULL DEFAULT 1.0,
            notes TEXT NULL,
            sort_order INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY(template_id) REFERENCES mealplan_templates(id) ON DELETE CASCADE,
            FOREIGN KEY(food_id) REFERENCES nutrition_foods(id) ON DELETE RESTRICT
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS pending_actions (
            id TEXT PRIMARY KEY,
            action_type TEXT NOT NULL,
            request_json TEXT NOT NULL,
            request_hash TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL,
            approved_at TEXT NULL,
            executed_at TEXT NULL,
            response_json TEXT NULL,
            error TEXT NULL
        )
        """
    )

    cur.execute("CREATE INDEX IF NOT EXISTS idx_mealplan_templates_created_at ON mealplan_templates(created_at)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_mealplan_items_template ON mealplan_template_items(template_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_pending_actions_status ON pending_actions(status)")

    conn.commit()
    conn.close()


def _normalize_food_ids(raw: Any) -> List[int]:
    if not isinstance(raw, list):
        return []
    out = []
    for item in raw:
        try:
            out.append(int(item))
        except Exception:
            continue
    return out


def _validate_targets(raw: Any) -> Tuple[Dict[str, float] | None, str | None]:
    if not isinstance(raw, dict):
        return None, "targets_required"
    targets: Dict[str, float] = {}
    for key in ("kcal", "p", "c", "f"):
        if key not in raw:
            return None, "targets_required"
        value = raw.get(key)
        try:
            num = float(value)
        except Exception:
            return None, "targets_invalid"
        if num < 0:
            return None, "targets_invalid"
        targets[key] = num
    return targets, None


def _live_targets_payload() -> Dict[str, Any]:
    live = get_live_macro_targets()
    return {
        "kcal": float(live.get("kcal") or 0.0),
        "p": float(live.get("p") or 0.0),
        "c": float(live.get("c") or 0.0),
        "f": float(live.get("f") or 0.0),
        "source": "live",
    }


def _load_foods(ids: List[int]) -> Dict[int, Dict[str, Any]]:
    if not ids:
        return {}
    conn = get_nutrition_db()
    cur = conn.cursor()
    placeholders = ",".join("?" for _ in ids)
    rows = cur.execute(
        f"""
        SELECT id, name, brand, unit_default, portion_g, kcal_per_100, p_per_100, c_per_100, f_per_100,
               is_active
        FROM nutrition_foods
        WHERE id IN ({placeholders})
        """,
        tuple(ids),
    ).fetchall()
    conn.close()
    return {int(row["id"]): dict(row) for row in rows}


def _resolve_food_ids(food_ids: List[int]) -> Tuple[List[int], List[int], List[int], List[Dict[str, Any]]]:
    foods_by_id = _load_foods(food_ids)
    invalid_ids = [fid for fid in food_ids if fid not in foods_by_id]
    inactive_ids: List[int] = []
    valid_ids: List[int] = []
    resolved: List[Dict[str, Any]] = []

    for fid in food_ids:
        food = foods_by_id.get(fid)
        if not food:
            continue
        raw_active = food.get("is_active")
        is_active = 1 if raw_active is None else int(raw_active)
        if is_active == 0:
            inactive_ids.append(fid)
            continue
        valid_ids.append(fid)
        resolved.append(food)
    return invalid_ids, inactive_ids, valid_ids, resolved


def _compute_macros(resolved_foods: List[Dict[str, Any]], qtys: List[float]) -> Dict[str, float]:
    totals = {"kcal": 0.0, "p": 0.0, "c": 0.0, "f": 0.0}
    for food, qty in zip(resolved_foods, qtys):
        totals["kcal"] += float(food.get("kcal_per_100") or 0.0) * qty
        totals["p"] += float(food.get("p_per_100") or 0.0) * qty
        totals["c"] += float(food.get("c_per_100") or 0.0) * qty
        totals["f"] += float(food.get("f_per_100") or 0.0) * qty
    return {k: round(v, 2) for k, v in totals.items()}


def _build_response_payload(
    title: str,
    targets: Dict[str, float],
    targets_used: Dict[str, Any],
    resolved_foods: List[Dict[str, Any]],
    valid_ids: List[int],
) -> Dict[str, Any]:
    qtys = [1.0 for _ in valid_ids]
    items = [
        {
            "food_id": fid,
            "qty": qtys[idx],
            "sort_order": idx,
        }
        for idx, fid in enumerate(valid_ids)
    ]
    macros = _compute_macros(resolved_foods, qtys)
    resolved = []
    for food in resolved_foods:
        resolved.append(
            {
                "id": int(food["id"]),
                "name": food.get("name"),
                "brand": food.get("brand"),
                "unit": food.get("unit_default"),
                "serving_g": food.get("portion_g"),
                "kcal_per_100": food.get("kcal_per_100"),
                "p_per_100": food.get("p_per_100"),
                "c_per_100": food.get("c_per_100"),
                "f_per_100": food.get("f_per_100"),
            }
        )
    return {
        "title": title,
        "targets": targets,
        "targets_used": targets_used,
        "items": items,
        "resolved_foods": resolved,
        "macros": macros,
    }


def _create_template(title: str, targets: Dict[str, float], valid_ids: List[int]) -> int:
    conn = get_nutrition_db()
    cur = conn.cursor()
    now = _now_iso()
    cur.execute(
        """
        INSERT INTO mealplan_templates (title, targets_json, created_at, created_by, source)
        VALUES (?, ?, ?, ?, ?)
        """,
        (title, json.dumps(targets, ensure_ascii=False), now, "ai", "aiw"),
    )
    template_id = cur.lastrowid
    for idx, fid in enumerate(valid_ids):
        cur.execute(
            """
            INSERT INTO mealplan_template_items (template_id, food_id, qty, notes, sort_order)
            VALUES (?, ?, ?, ?, ?)
            """,
            (template_id, fid, 1.0, None, idx),
        )
    conn.commit()
    conn.close()
    return int(template_id)


def _store_pending_action(action_type: str, body: Dict[str, Any]) -> str:
    action_id = uuid.uuid4().hex
    payload = json.dumps(body, ensure_ascii=False, sort_keys=True)
    request_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    conn = get_nutrition_db()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO pending_actions (id, action_type, request_json, request_hash, status, created_at)
        VALUES (?, ?, ?, ?, 'pending', ?)
        """,
        (action_id, action_type, payload, request_hash, _now_iso()),
    )
    conn.commit()
    conn.close()
    return action_id


def _approval_required(body: Dict[str, Any]) -> bool:
    if body.get("dry_run"):
        return False
    return (os.getenv("AI_WRITE_REQUIRE_APPROVAL") or "0").strip() == "1"


def _handle_mealplan_template(body: Dict[str, Any], allow_approval: bool = True):
    if not isinstance(body, dict):
        return _json_error(400, "invalid_request", "JSON body required")

    if body.get("intent") != "nutrition_plan_write":
        return _json_error(400, "invalid_intent", "intent must be nutrition_plan_write")

    food_ids = _normalize_food_ids(body.get("food_ids"))
    if not food_ids:
        return _json_error(400, "invalid_request", "food_ids required", {"food_ids": body.get("food_ids")})

    warnings: List[Dict[str, Any]] = []
    targets_used = _live_targets_payload()
    targets = {k: targets_used[k] for k in ("kcal", "p", "c", "f")}

    if body.get("targets") is not None:
        allow_manual = bool(body.get("allow_manual_targets")) or body.get("targets_source") == "manual_override"
        if allow_manual:
            manual_targets, target_err = _validate_targets(body.get("targets"))
            if target_err:
                return _json_error(400, "invalid_targets", "targets must include kcal,p,c,f as non-negative numbers")
            targets = manual_targets
            targets_used = {**manual_targets, "source": "manual_override"}
            warnings.append({"code": "manual_targets_used"})
        else:
            warnings.append({"code": "manual_targets_ignored_using_live"})

    _ensure_ai_schema()

    invalid_ids, inactive_ids, valid_ids, resolved_foods = _resolve_food_ids(food_ids)
    if not valid_ids:
        return _json_error(
            400,
            "no_valid_foods",
            "No valid foods found",
            {
                "invalid_ids": invalid_ids,
                "inactive_ids": inactive_ids,
                "valid_ids": [],
                "hint": "Load foods or query catalog",
            },
        )

    if invalid_ids or inactive_ids:
        warnings.append(
            {
                "code": "partial_foods",
                "message": "Some foods are invalid or inactive",
                "detail": {
                    "invalid_ids": invalid_ids,
                    "inactive_ids": inactive_ids,
                    "valid_ids": valid_ids,
                },
            }
        )

    title = (body.get("title") or "").strip() or f"AI Mealplan {datetime.now().strftime('%Y-%m-%d')}"
    if len(valid_ids) < 3:
        warnings.append({"code": "template_stub_insufficient_foods"})

    payload = _build_response_payload(title, targets, targets_used, resolved_foods, valid_ids)

    if body.get("dry_run"):
        payload["would_create"] = True
        payload["valid_ids"] = valid_ids
        payload["inactive_ids"] = inactive_ids
        payload["invalid_ids"] = invalid_ids
        return _json_ok(payload, warnings)

    if allow_approval and _approval_required(body):
        action_id = _store_pending_action("nutrition_mealplan_template", body)
        return (
            jsonify(
                {
                    "ok": False,
                    "error_code": "approval_required",
                    "message": "The requested action requires approval",
                    "detail": {"action_id": action_id, "retryable": True},
                }
            ),
            409,
        )

    template_id = _create_template(title, targets, valid_ids)
    payload["template_id"] = template_id
    payload["valid_ids"] = valid_ids
    payload["inactive_ids"] = inactive_ids
    payload["invalid_ids"] = invalid_ids
    return _json_ok(payload, warnings)


@api_aiw_nutrition.post("/nutrition/mealplan_template")
@require_ai_write
@require_intent("nutrition_plan_write")
def create_nutrition_mealplan_template():
    body = request.get_json(silent=True) or {}
    return _handle_mealplan_template(body, allow_approval=True)


@api_aiw_nutrition.post("/actions/<action_id>/approve")
@require_ai_write
@require_intent("nutrition_plan_write")
def approve_action(action_id: str):
    _ensure_ai_schema()
    conn = get_nutrition_db()
    cur = conn.cursor()
    row = cur.execute(
        "SELECT id, action_type, request_json, status, response_json FROM pending_actions WHERE id=?",
        (action_id,),
    ).fetchone()
    if not row:
        conn.close()
        return _json_error(404, "not_found", "Action not found")

    if row["action_type"] != "nutrition_mealplan_template":
        conn.close()
        return _json_error(409, "approval_state_invalid", "Action type mismatch")

    status = row["status"]
    if status == "executed":
        response_json = row["response_json"]
        conn.close()
        if response_json:
            return jsonify(json.loads(response_json))
        return _json_error(500, "approval_state_invalid", "Executed action missing response")

    if status not in ("pending", "approved"):
        conn.close()
        return _json_error(409, "approval_state_invalid", f"Action is {status}")

    now = _now_iso()
    if status == "pending":
        cur.execute(
            "UPDATE pending_actions SET status='approved', approved_at=? WHERE id=?",
            (now, action_id),
        )
        conn.commit()

    body = json.loads(row["request_json"])
    response = _handle_mealplan_template(body, allow_approval=False)

    if isinstance(response, tuple):
        payload, status_code = response
        payload_json = payload.get_json(silent=True)
        if status_code >= 400:
            cur.execute(
                "UPDATE pending_actions SET status='failed', error=?, executed_at=? WHERE id=?",
                (json.dumps(payload_json or {}, ensure_ascii=False), now, action_id),
            )
            conn.commit()
            conn.close()
            return response
        response_json = json.dumps(payload_json or {}, ensure_ascii=False)
        cur.execute(
            """
            UPDATE pending_actions
            SET status='executed', executed_at=?, response_json=?
            WHERE id=?
            """,
            (now, response_json, action_id),
        )
        conn.commit()
        conn.close()
        return response

    payload_json = response.get_json(silent=True)
    cur.execute(
        """
        UPDATE pending_actions
        SET status='executed', executed_at=?, response_json=?
        WHERE id=?
        """,
        (now, json.dumps(payload_json or {}, ensure_ascii=False), action_id),
    )
    conn.commit()
    conn.close()
    return response
