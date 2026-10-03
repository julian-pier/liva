from __future__ import annotations

from typing import Any, Dict, List
from datetime import datetime

from flask import Blueprint, jsonify, request

from database.connections import get_nutrition_db
from nutrition.nutrition_planning_db import ensure_nutrition_planning_schema, get_live_macro_targets
from security.write_guard import require_ai_read

api_air_nutrition = Blueprint("api_air_nutrition", __name__, url_prefix="/api/air")


def _json_error(status: int, error_code: str, message: str, detail: Dict[str, Any] | None = None):
    return jsonify({"ok": False, "error_code": error_code, "message": message, "detail": detail or {}}), status


def _ensure_schema():
    ensure_nutrition_planning_schema()


def _row_to_food(row) -> Dict[str, Any]:
    raw_active = row.get("is_active")
    return {
        "id": int(row["id"]),
        "name": row["name"],
        "brand": row["brand"],
        "kcal": row["kcal_per_100"],
        "p": row["p_per_100"],
        "c": row["c_per_100"],
        "f": row["f_per_100"],
        "unit": row["unit_default"],
        "serving_g": row["portion_g"],
        "is_active": 1 if raw_active is None else int(raw_active),
    }


@api_air_nutrition.get("/nutrition/foods")
@require_ai_read
def ai_read_foods():
    _ensure_schema()
    query = (request.args.get("query") or "").strip()
    limit = int(request.args.get("limit") or 50)
    limit = max(1, min(500, limit))
    only_active = (request.args.get("only_active") or "1").strip() in ("1", "true", "yes", "on")

    conn = get_nutrition_db()
    cur = conn.cursor()

    sql = """
        SELECT id, name, brand, unit_default, portion_g, kcal_per_100, p_per_100, c_per_100, f_per_100, is_active
        FROM nutrition_foods
    """
    params: List[Any] = []
    clauses: List[str] = []
    if query:
        clauses.append("(name LIKE ? OR brand LIKE ?)")
        token = f"%{query}%"
        params.extend([token, token])
    if only_active:
        clauses.append("COALESCE(is_active, 1) = 1")

    if clauses:
        sql += " WHERE " + " AND ".join(clauses)

    sql += " ORDER BY name ASC LIMIT ?"
    params.append(limit)

    rows = cur.execute(sql, tuple(params)).fetchall()
    conn.close()

    foods = [_row_to_food(row) for row in rows]
    return jsonify({"ok": True, "data": foods})


@api_air_nutrition.get("/nutrition/foods/ids")
@require_ai_read
def ai_read_food_ids():
    _ensure_schema()
    only_active = (request.args.get("only_active") or "1").strip() in ("1", "true", "yes", "on")

    conn = get_nutrition_db()
    cur = conn.cursor()
    if only_active:
        rows = cur.execute(
            "SELECT id FROM nutrition_foods WHERE COALESCE(is_active, 1) = 1 ORDER BY id ASC"
        ).fetchall()
    else:
        rows = cur.execute("SELECT id FROM nutrition_foods ORDER BY id ASC").fetchall()
    conn.close()

    ids = [int(row["id"]) for row in rows]
    return jsonify({"ok": True, "data": {"ids": ids}})


@api_air_nutrition.get("/nutrition/foods/<int:food_id>")
@require_ai_read
def ai_read_food(food_id: int):
    _ensure_schema()
    conn = get_nutrition_db()
    cur = conn.cursor()
    row = cur.execute(
        """
        SELECT id, name, brand, unit_default, portion_g, kcal_per_100, p_per_100, c_per_100, f_per_100, is_active
        FROM nutrition_foods
        WHERE id = ?
        """,
        (food_id,),
    ).fetchone()
    conn.close()

    if not row:
        return _json_error(404, "not_found", "Food not found")

    return jsonify({"ok": True, "data": _row_to_food(row)})


@api_air_nutrition.get("/nutrition/targets/live")
@require_ai_read
def ai_read_live_targets():
    _ensure_schema()
    targets = get_live_macro_targets()
    payload = {
        "kcal": targets.get("kcal", 0.0),
        "p": targets.get("p", 0.0),
        "c": targets.get("c", 0.0),
        "f": targets.get("f", 0.0),
        "source": "live",
        "effective_date": datetime.now().date().isoformat(),
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }
    return jsonify({"ok": True, "data": payload})


@api_air_nutrition.get("/nutrition/targets/history")
@require_ai_read
def ai_read_targets_history():
    _ensure_schema()
    limit = int(request.args.get("limit") or 30)
    limit = max(1, min(365, limit))
    live = get_live_macro_targets()
    entry = {
        "kcal": live.get("kcal", 0.0),
        "p": live.get("p", 0.0),
        "c": live.get("c", 0.0),
        "f": live.get("f", 0.0),
        "source": "live",
        "effective_date": datetime.now().date().isoformat(),
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }
    return jsonify({"ok": True, "data": [entry][:limit]})
