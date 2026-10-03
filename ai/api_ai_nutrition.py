from __future__ import annotations

from datetime import date
from typing import Any, Dict, List, Optional, Tuple

from flask import jsonify, request

from database import connections
from nutrition.nutrition_planning_db import get_live_macro_targets
from security.write_guard import require_ai_read
from ai.api_ai_common import (
    add_pagination_meta,
    apply_fields,
    build_date_where,
    parse_fields,
    parse_limit,
    parse_time_range,
    parse_cursor,
)


MEAL_FIELDS = [
    "id",
    "title",
    "mode",
    "notes",
    "is_favorite",
    "created_at",
    "updated_at",
]


def _fetch_targets_used() -> Dict[str, Any]:
    live = get_live_macro_targets()
    return {
        "kcal": live.get("kcal", 0.0),
        "p": live.get("p", 0.0),
        "c": live.get("c", 0.0),
        "f": live.get("f", 0.0),
        "source": live.get("source", "live"),
        "mode": live.get("mode", "maintenance"),
    }


def _resolve_foods(conn, food_ids: List[int]) -> Dict[int, Dict[str, Any]]:
    if not food_ids:
        return {}
    placeholders = ",".join(["?"] * len(food_ids))
    rows = conn.execute(
        f"""
        SELECT
            id,
            name,
            brand,
            kcal_per_100 AS kcal,
            p_per_100 AS p,
            c_per_100 AS c,
            f_per_100 AS f,
            unit_default AS unit,
            portion_g AS serving_g,
            is_active
        FROM nutrition_foods
        WHERE id IN ({placeholders})
        """,
        food_ids,
    ).fetchall()
    return {row["id"]: dict(row) for row in rows}


def register(ai_api):
    @ai_api.get("/nutrition/targets/live")
    @require_ai_read
    def nutrition_targets_live():
        live = get_live_macro_targets()
        payload = {
            "kcal": live.get("kcal", 0.0),
            "p": live.get("p", 0.0),
            "c": live.get("c", 0.0),
            "f": live.get("f", 0.0),
            "source": live.get("source", "live"),
            "effective_date": live.get("effective_date") or date.today().isoformat(),
            "updated_at": live.get("updated_at"),
        }
        if live.get("mode") is not None:
            payload["mode"] = live.get("mode")
        if live.get("template_id") is not None:
            payload["template_id"] = live.get("template_id")
        if live.get("template_title") is not None:
            payload["template_title"] = live.get("template_title")
        if live.get("weekday") is not None:
            payload["weekday"] = live.get("weekday")
        return jsonify({"ok": True, "data": payload})

    @ai_api.get("/nutrition/actuals/daily")
    @require_ai_read
    def nutrition_actuals_daily():
        date_from, date_to, last_token = parse_time_range(request.args)
        limit = parse_limit(request.args.get("limit"), default=200, max_limit=500)
        offset = parse_cursor(request.args.get("cursor"))

        conn = connections.get_nutrition_db()
        cur = conn.cursor()
        try:
            row = cur.execute("SELECT COUNT(*) AS n FROM nutrition_day_actuals").fetchone()
            use_day_actuals = (row["n"] if row else 0) > 0
        except Exception:
            use_day_actuals = False

        if use_day_actuals:
            where, params = build_date_where("nda.day", date_from, date_to)
            sql = """
                SELECT
                    nda.day,
                    nda.kcal,
                    nda.p,
                    nda.c,
                    nda.f,
                    nda.source,
                    wl.weight_kg AS bodyweight_kg
                FROM nutrition_day_actuals nda
                LEFT JOIN weight_logs wl ON wl.date_iso = nda.day
            """
            if where:
                sql += " WHERE " + " AND ".join(where)
            sql += " ORDER BY nda.day DESC LIMIT ? OFFSET ?"
            params.extend([limit + 1, offset])
            rows = [dict(r) for r in cur.execute(sql, params).fetchall()]
        else:
            where, params = build_date_where("date_iso", date_from, date_to)
            sql = """
                SELECT
                    date_iso AS day,
                    kcal,
                    protein AS p,
                    carbs AS c,
                    fat AS f,
                    'weight_logs' AS source,
                    weight_kg AS bodyweight_kg
                FROM weight_logs
            """
            if where:
                sql += " WHERE " + " AND ".join(where)
            sql += " ORDER BY date_iso DESC LIMIT ? OFFSET ?"
            params.extend([limit + 1, offset])
            rows = [dict(r) for r in cur.execute(sql, params).fetchall()]
        conn.close()

        trimmed = rows[:limit]
        targets_used = _fetch_targets_used()
        meta = {"from": date_from, "to": date_to, "last": last_token, "targets_used": targets_used}
        meta = add_pagination_meta(meta, offset, limit, len(trimmed))
        return jsonify({"ok": True, "data": trimmed, "meta": meta})

    @ai_api.get("/nutrition/adherence/summary")
    @require_ai_read
    def nutrition_adherence_summary():
        date_from, date_to, last_token = parse_time_range(request.args)
        conn = connections.get_nutrition_db()
        cur = conn.cursor()
        where, params = build_date_where("day", date_from, date_to)
        sql = """
            SELECT
                COUNT(*) AS n_days,
                AVG(kcal) AS avg_kcal,
                AVG(p) AS avg_p,
                AVG(c) AS avg_c,
                AVG(f) AS avg_f
            FROM nutrition_day_actuals
        """
        if where:
            sql += " WHERE " + " AND ".join(where)
        row = cur.execute(sql, params).fetchone()
        conn.close()
        targets_used = _fetch_targets_used()
        data = dict(row) if row else {"n_days": 0, "avg_kcal": None, "avg_p": None, "avg_c": None, "avg_f": None}
        return jsonify({
            "ok": True,
            "data": data,
            "meta": {"from": date_from, "to": date_to, "last": last_token, "targets_used": targets_used},
        })

    @ai_api.get("/nutrition/meals/list")
    @require_ai_read
    def nutrition_meals_list():
        limit = parse_limit(request.args.get("limit"), default=200, max_limit=500)
        offset = parse_cursor(request.args.get("cursor"))
        fields = parse_fields(request.args.get("fields"), MEAL_FIELDS)

        conn = connections.get_nutrition_db()
        cur = conn.cursor()
        rows = cur.execute(
            """
            SELECT id, title, mode, notes, is_favorite, created_at, updated_at
            FROM nutrition_meal_templates
            ORDER BY updated_at DESC, id DESC
            LIMIT ? OFFSET ?
            """,
            (limit + 1, offset),
        ).fetchall()

        meal_templates = [dict(r) for r in rows]
        trimmed = meal_templates[:limit]

        meal_ids = [m["id"] for m in trimmed]
        resolved_foods: Dict[int, Dict[str, Any]] = {}
        items_by_meal: Dict[int, List[Dict[str, Any]]] = {mid: [] for mid in meal_ids}
        if meal_ids:
            placeholders = ",".join(["?"] * len(meal_ids))
            item_rows = cur.execute(
                f"""
                SELECT
                    i.meal_template_id AS meal_template_id,
                    i.food_id AS food_id,
                    i.amount AS amount,
                    i.unit AS unit,
                    i.sort_index AS sort_index,
                    f.name AS name,
                    f.brand AS brand,
                    f.kcal_per_100 AS kcal,
                    f.p_per_100 AS p,
                    f.c_per_100 AS c,
                    f.f_per_100 AS f,
                    f.portion_g AS serving_g,
                    f.unit_default AS unit_default,
                    f.is_active AS is_active
                FROM nutrition_meal_ingredients i
                JOIN nutrition_foods f ON f.id = i.food_id
                WHERE i.meal_template_id IN ({placeholders})
                ORDER BY i.meal_template_id ASC, i.sort_index ASC, i.id ASC
                """,
                meal_ids,
            ).fetchall()

            for row in item_rows:
                item = dict(row)
                food_payload = {
                    "id": item["food_id"],
                    "name": item["name"],
                    "brand": item["brand"],
                    "kcal": item["kcal"],
                    "p": item["p"],
                    "c": item["c"],
                    "f": item["f"],
                    "serving_g": item["serving_g"],
                    "unit": item["unit_default"],
                    "is_active": item["is_active"],
                }
                resolved_foods[item["food_id"]] = food_payload
                items_by_meal[item["meal_template_id"]].append({
                    "food_id": item["food_id"],
                    "amount": item["amount"],
                    "unit": item["unit"],
                    "sort_index": item["sort_index"],
                    "resolved_food": food_payload,
                })

        conn.close()

        data = apply_fields(trimmed, fields)
        for entry in data:
            meal_id = entry.get("id")
            entry["items"] = items_by_meal.get(meal_id, [])

        meta = add_pagination_meta({"targets_used": _fetch_targets_used()}, offset, limit, len(trimmed))
        return jsonify({"ok": True, "data": data, "meta": meta})

    @ai_api.get("/nutrition/mealplan/week")
    @require_ai_read
    def nutrition_mealplan_week():
        conn = connections.get_nutrition_db()
        cur = conn.cursor()

        week_plan = cur.execute(
            """
            SELECT id, title, start_monday, updated_at
            FROM nutrition_week_plans
            WHERE is_active=1
            ORDER BY updated_at DESC, id DESC
            LIMIT 1
            """
        ).fetchone()
        if not week_plan:
            conn.close()
            return jsonify({"ok": True, "data": None, "meta": {"targets_used": _fetch_targets_used()}})

        slots = cur.execute(
            """
            SELECT id, week_plan_id, weekday, slot_index, time_text, label_text, meal_template_id
            FROM nutrition_day_plan_slots
            WHERE week_plan_id=?
            ORDER BY weekday ASC, slot_index ASC
            """,
            (week_plan["id"],),
        ).fetchall()

        template_ids = [row["meal_template_id"] for row in slots if row["meal_template_id"]]
        templates: Dict[int, Dict[str, Any]] = {}
        if template_ids:
            placeholders = ",".join(["?"] * len(template_ids))
            templates_rows = cur.execute(
                f"""
                SELECT id, title, mode, notes, is_favorite, created_at, updated_at
                FROM nutrition_meal_templates
                WHERE id IN ({placeholders})
                """,
                template_ids,
            ).fetchall()
            templates = {row["id"]: dict(row) for row in templates_rows}

        food_ids: List[int] = []
        items_by_template: Dict[int, List[Dict[str, Any]]] = {tid: [] for tid in templates.keys()}
        if template_ids:
            placeholders = ",".join(["?"] * len(template_ids))
            item_rows = cur.execute(
                f"""
                SELECT
                    i.meal_template_id AS meal_template_id,
                    i.food_id AS food_id,
                    i.amount AS amount,
                    i.unit AS unit,
                    i.sort_index AS sort_index,
                    f.name AS name,
                    f.brand AS brand,
                    f.kcal_per_100 AS kcal,
                    f.p_per_100 AS p,
                    f.c_per_100 AS c,
                    f.f_per_100 AS f,
                    f.portion_g AS serving_g,
                    f.unit_default AS unit_default,
                    f.is_active AS is_active
                FROM nutrition_meal_ingredients i
                JOIN nutrition_foods f ON f.id = i.food_id
                WHERE i.meal_template_id IN ({placeholders})
                ORDER BY i.meal_template_id ASC, i.sort_index ASC, i.id ASC
                """,
                template_ids,
            ).fetchall()
            for row in item_rows:
                item = dict(row)
                food_payload = {
                    "id": item["food_id"],
                    "name": item["name"],
                    "brand": item["brand"],
                    "kcal": item["kcal"],
                    "p": item["p"],
                    "c": item["c"],
                    "f": item["f"],
                    "serving_g": item["serving_g"],
                    "unit": item["unit_default"],
                    "is_active": item["is_active"],
                }
                items_by_template[item["meal_template_id"]].append({
                    "food_id": item["food_id"],
                    "amount": item["amount"],
                    "unit": item["unit"],
                    "sort_index": item["sort_index"],
                    "resolved_food": food_payload,
                })

        conn.close()

        week_payload = dict(week_plan)
        week_slots = []
        for row in slots:
            slot = dict(row)
            meal_template_id = slot.get("meal_template_id")
            template = templates.get(meal_template_id)
            if template:
                template = dict(template)
                template["items"] = items_by_template.get(meal_template_id, [])
            slot["meal_template"] = template
            week_slots.append(slot)

        week_payload["slots"] = week_slots
        return jsonify({"ok": True, "data": week_payload, "meta": {"targets_used": _fetch_targets_used()}})
