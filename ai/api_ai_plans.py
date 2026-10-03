from __future__ import annotations

import json
from typing import Any, Dict, List

from flask import jsonify, request

from database import connections
from security.write_guard import require_ai_read
from ai.api_ai_common import parse_limit


def _parse_plan_row(row) -> Dict[str, Any]:
    data = dict(row)
    raw = data.get("data")
    try:
        data["plan"] = json.loads(raw) if raw else None
    except Exception:
        data["plan"] = None
    data.pop("data", None)
    return data


def register(ai_api):
    @ai_api.get("/plans/list")
    @require_ai_read
    def plans_list():
        limit = parse_limit(request.args.get("limit"), default=200, max_limit=500)
        conn = connections.get_plans_db()
        cur = conn.cursor()
        rows = cur.execute(
            """
            SELECT id, name, block_length, is_active, updated_at, created_at
            FROM plans
            ORDER BY updated_at DESC, id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        conn.close()
        return jsonify({"ok": True, "data": [dict(r) for r in rows], "meta": {"limit": limit}})

    @ai_api.get("/plans/active")
    @require_ai_read
    def plans_active():
        conn = connections.get_plans_db()
        cur = conn.cursor()
        row = cur.execute(
            """
            SELECT id, name, block_length, is_active, data, updated_at, created_at
            FROM plans
            WHERE is_active=1
            ORDER BY updated_at DESC, id DESC
            LIMIT 1
            """
        ).fetchone()
        conn.close()
        return jsonify({"ok": True, "data": _parse_plan_row(row) if row else None})

    @ai_api.get("/plans/<int:plan_id>")
    @require_ai_read
    def plans_get(plan_id: int):
        conn = connections.get_plans_db()
        cur = conn.cursor()
        row = cur.execute(
            """
            SELECT id, name, block_length, is_active, data, updated_at, created_at
            FROM plans
            WHERE id=?
            """,
            (plan_id,),
        ).fetchone()
        conn.close()
        if not row:
            return jsonify({"ok": False, "error_code": "not_found", "message": "plan not found", "data": None})
        return jsonify({"ok": True, "data": _parse_plan_row(row)})
