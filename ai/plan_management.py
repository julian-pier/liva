from __future__ import annotations

import copy
import json
import sqlite3
from datetime import datetime
from typing import Any

from database import connections
from nutrition.nutrition_plan_write_service import (
    activate_nutrition_plan,
    archive_nutrition_plan,
    create_structured_nutrition_plan,
    delete_nutrition_plan,
    replace_nutrition_plan,
)
from nutrition.nutrition_planning_db import (
    _get_or_create_slot_template_id,
    _build_week_plan_payload_wrapper,
    _food_macro_factor,
    create_food,
    create_meal_template,
    create_week_template,
    delete_food,
    delete_meal_template,
    delete_week_day_slot,
    ensure_nutrition_planning_schema,
    list_foods,
    list_meal_templates,
    list_week_templates,
    resolve_meal_template,
    restore_week_template,
    update_food,
    update_meal_template,
    update_week_slot,
)

WEEKDAY_ORDER = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]


def _utcnow_iso() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat()


def _bool(value: Any, *, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _int_or_none(value: Any) -> int | None:
    try:
        if value in (None, ""):
            return None
        return int(value)
    except Exception:
        return None


def _float_or_none(value: Any) -> float | None:
    try:
        if value in (None, ""):
            return None
        return float(value)
    except Exception:
        return None


def _norm(value: Any) -> str:
    return " ".join(str(value or "").strip().lower().split())


def _is_placeholder_meal_title(title: Any) -> bool:
    text = str(title or "").strip().lower()
    if not text:
        return True
    parts = text.split()
    return len(parts) == 2 and parts[0] == "meal" and parts[1].isdigit()


def _nutrition_slot_path(day_index: int, meal_index: int, slot_index: int) -> str:
    return f"d{day_index}:m{meal_index}:s{slot_index}"


def _nutrition_meal_time_sort_key(meal: dict[str, Any], original_index: int) -> tuple[int, int, int]:
    text = str(meal.get("time_text") or meal.get("time") or "").strip()
    try:
        hours_text, minutes_text = text.split(":", 1)
        hours = int(hours_text)
        minutes = int(minutes_text)
        if 0 <= hours <= 23 and 0 <= minutes <= 59:
            return (0, hours * 60 + minutes, original_index)
    except (TypeError, ValueError):
        pass
    return (1, 24 * 60, original_index)


def _sort_nutrition_meals_chronologically(meals: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [meal for _, meal in sorted(enumerate(meals), key=lambda pair: _nutrition_meal_time_sort_key(pair[1], pair[0]))]


def _parse_slot_path(value: Any) -> tuple[int, int, int] | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        day_part, meal_part, slot_part = text.split(":")
        if not (day_part.startswith("d") and meal_part.startswith("m") and slot_part.startswith("s")):
            return None
        return int(day_part[1:]), int(meal_part[1:]), int(slot_part[1:])
    except Exception:
        return None


def _training_exercise_path(day_index: int, workout_index: int, exercise_index: int) -> str:
    return f"d{day_index}:w{workout_index}:e{exercise_index}"


def _training_set_path(day_index: int, workout_index: int, exercise_index: int, set_index: int) -> str:
    return f"d{day_index}:w{workout_index}:e{exercise_index}:s{set_index}"


def _parse_training_exercise_path(value: Any) -> tuple[int, int, int] | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        day_part, workout_part, exercise_part = text.split(":")
        if not (day_part.startswith("d") and workout_part.startswith("w") and exercise_part.startswith("e")):
            return None
        return int(day_part[1:]), int(workout_part[1:]), int(exercise_part[1:])
    except Exception:
        return None


def _parse_training_set_path(value: Any) -> tuple[int, int, int, int] | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        day_part, workout_part, exercise_part, set_part = text.split(":")
        if not (day_part.startswith("d") and workout_part.startswith("w") and exercise_part.startswith("e") and set_part.startswith("s")):
            return None
        return int(day_part[1:]), int(workout_part[1:]), int(exercise_part[1:]), int(set_part[1:])
    except Exception:
        return None


def _plan_diff(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    return {
        "before_counts": dict(before.get("counts") or {}),
        "after_counts": dict(after.get("counts") or {}),
        "changed": before != after,
    }


def _load_food_by_id(food_id: int | None) -> dict[str, Any] | None:
    if food_id is None:
        return None
    conn = connections.get_nutrition_db()
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            """
            SELECT id, name, unit_default, portion_g, common_portion_size,
                   kcal_per_100, p_per_100, c_per_100, f_per_100
            FROM nutrition_foods
            WHERE id=? AND COALESCE(is_active, 1)=1
            LIMIT 1
            """,
            (food_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _load_nutrition_template_row(plan_id: int) -> dict[str, Any] | None:
    conn = connections.get_nutrition_db()
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            """
            SELECT id, title, is_active, archived_at, created_at, updated_at
            FROM nutrition_week_templates
            WHERE id=?
            LIMIT 1
            """,
            (plan_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _slot_macros_from_food(food: dict[str, Any] | None, amount: Any, unit: Any) -> dict[str, Any]:
    normalized_amount = _float_or_none(amount)
    normalized_unit = str(unit or "g").strip().lower() or "g"
    if not food or normalized_amount is None:
        return {
            "kcal": None,
            "protein_g": None,
            "carbs_g": None,
            "fat_g": None,
            "macro_resolution": "unresolved",
        }
    factor = _food_macro_factor(
        normalized_amount,
        normalized_unit,
        food.get("unit_default"),
        food.get("common_portion_size"),
        food.get("portion_g"),
    )
    food_unit = str(food.get("unit_default") or "g").strip().lower()
    piece_units = {"pcs", "piece", "pieces", "stück", "stueck"}
    portion_g = _float_or_none(food.get("common_portion_size")) or _float_or_none(food.get("portion_g"))
    unresolved = food_unit in piece_units and normalized_unit in {"g", "gram", "grams", "ml"} and not portion_g
    if unresolved:
        return {
            "kcal": None,
            "protein_g": None,
            "carbs_g": None,
            "fat_g": None,
            "macro_resolution": "unresolved_piece_conversion",
        }
    macro_basis = "per_piece" if food_unit in piece_units and normalized_unit in piece_units and not portion_g else "per_100g"
    return {
        "kcal": round((_float_or_none(food.get("kcal_per_100")) or 0.0) * factor, 1),
        "protein_g": round((_float_or_none(food.get("p_per_100")) or 0.0) * factor, 1),
        "carbs_g": round((_float_or_none(food.get("c_per_100")) or 0.0) * factor, 1),
        "fat_g": round((_float_or_none(food.get("f_per_100")) or 0.0) * factor, 1),
        "macro_resolution": "resolved",
        "macro_basis": macro_basis,
    }


def _nutrition_slot_amount_fields(amount: Any, unit: Any) -> dict[str, Any]:
    raw_unit = str(unit or "g").strip().lower() or "g"
    normalized_unit = "pcs" if raw_unit in {"pcs", "piece", "pieces", "stück", "stueck", "stk"} else raw_unit
    normalized_amount = _float_or_none(amount)
    if normalized_amount is None:
        display_amount = ""
    elif normalized_unit == "pcs":
        display_amount = f"{int(normalized_amount) if float(normalized_amount).is_integer() else f'{normalized_amount:g}'}×"
    elif float(normalized_amount).is_integer():
        display_amount = f"{int(normalized_amount)} {normalized_unit}"
    else:
        display_amount = f"{normalized_amount:g} {normalized_unit}"
    return {
        "amount": normalized_amount,
        "unit": normalized_unit,
        "amount_g": normalized_amount if normalized_unit == "g" else None,
        "amount_ml": normalized_amount if normalized_unit == "ml" else None,
        "amount_pcs": normalized_amount if normalized_unit == "pcs" else None,
        "display_amount": display_amount,
    }


def _recalculate_nutrition_mutable(mutable: dict[str, Any]) -> dict[str, Any]:
    plan_kcal = plan_p = plan_c = plan_f = 0.0
    meals_count = 0
    slots_count = 0
    warnings = list(mutable.get("warnings") or [])
    empty_placeholder_count = 0
    for day in mutable.get("days") or []:
        day["meals"] = _sort_nutrition_meals_chronologically(list(day.get("meals") or []))
        day_kcal = day_p = day_c = day_f = 0.0
        for meal_index, meal in enumerate(day.get("meals") or []):
            meals_count += 1
            meal["meal_index"] = meal_index
            meal_slots = meal.get("slots") or []
            meal["empty"] = len(meal_slots) == 0
            meal["placeholder"] = _is_placeholder_meal_title(meal.get("title")) or meal["empty"]
            if meal["empty"]:
                empty_placeholder_count += 1
                warnings.append(
                    {
                        "code": "empty_meal_placeholder",
                        "day_index": day.get("day_index"),
                        "meal_index": meal_index,
                        "title": meal.get("title"),
                    }
                )
            meal_kcal = meal_p = meal_c = meal_f = 0.0
            for slot_index, slot in enumerate(meal_slots):
                slots_count += 1
                slot["slot_index"] = slot_index
                slot["day_index"] = day.get("day_index")
                slot["meal_index"] = meal_index
                slot["slot_path"] = _nutrition_slot_path(int(day.get("day_index") or 0), meal_index, slot_index)
                slot.update(_nutrition_slot_amount_fields(slot.get("amount"), slot.get("unit") or "g"))
                food = _load_food_by_id(_int_or_none(slot.get("food_id")))
                macros = _slot_macros_from_food(food, slot.get("amount"), slot.get("unit"))
                if food:
                    slot["food_name"] = food.get("name")
                slot.update(macros)
                meal_kcal += macros["kcal"] or 0.0
                meal_p += macros["protein_g"] or 0.0
                meal_c += macros["carbs_g"] or 0.0
                meal_f += macros["fat_g"] or 0.0
            meal["totals"] = {
                "kcal": round(meal_kcal, 1),
                "protein_g": round(meal_p, 1),
                "carbs_g": round(meal_c, 1),
                "fat_g": round(meal_f, 1),
            }
            day_kcal += meal_kcal
            day_p += meal_p
            day_c += meal_c
            day_f += meal_f
        day["totals"] = {
            "kcal": round(day_kcal, 1),
            "protein_g": round(day_p, 1),
            "carbs_g": round(day_c, 1),
            "fat_g": round(day_f, 1),
        }
        plan_kcal += day_kcal
        plan_p += day_p
        plan_c += day_c
        plan_f += day_f
    mutable["totals"] = {
        "kcal": round(plan_kcal, 1),
        "protein_g": round(plan_p, 1),
        "carbs_g": round(plan_c, 1),
        "fat_g": round(plan_f, 1),
    }
    mutable["counts"] = {
        "days_count": len(mutable.get("days") or []),
        "meals_count": meals_count,
        "slots_count": slots_count,
    }
    mutable["empty_placeholder_count"] = empty_placeholder_count
    mutable["warnings"] = warnings
    return mutable


def search_nutrition_foods(query: str, *, limit: int = 25) -> list[dict[str, Any]]:
    ensure_nutrition_planning_schema()
    rows = list_foods(query=query, limit=limit)
    out = []
    for row in rows:
        out.append(
            {
                "food_id": row.get("id"),
                "name": row.get("name"),
                "kcal_per_100g": row.get("kcal_per_100"),
                "protein_g_per_100g": row.get("p_per_100"),
                "carbs_g_per_100g": row.get("c_per_100"),
                "fat_g_per_100g": row.get("f_per_100"),
                "aliases": [alias for alias in [row.get("mfp_search_hint"), row.get("brand")] if alias],
                "source": "nutrition_foods",
            }
        )
    return out


def list_nutrition_meal_templates(query: str = "", *, limit: int = 200) -> list[dict[str, Any]]:
    rows = list_meal_templates(query=query, limit=limit)
    out = []
    for row in rows:
        out.append(
            {
                "template_id": row.get("id"),
                "title": row.get("title"),
                "description": row.get("description"),
                "default_servings": row.get("default_servings"),
                "notes": row.get("notes"),
                "kcal_per_serving": row.get("kcal_per_serving"),
                "protein_g_per_serving": row.get("p_per_serving"),
                "carbs_g_per_serving": row.get("c_per_serving"),
                "fat_g_per_serving": row.get("f_per_serving"),
                "created_at": row.get("created_at"),
                "updated_at": row.get("updated_at"),
            }
        )
    return out


def get_nutrition_meal_template_detail(template_id: int) -> dict[str, Any]:
    detail = resolve_meal_template(template_id)
    if not detail:
        raise ValueError("template_not_found")
    items = []
    for idx, item in enumerate(detail.get("ingredients") or []):
        amount_fields = _nutrition_slot_amount_fields(item.get("amount"), item.get("unit") or "g")
        items.append(
            {
                "item_index": idx,
                "food_id": item.get("food_id"),
                "food_name": item.get("food_name"),
                **amount_fields,
                "kcal_per_100g": item.get("kcal_per_100"),
                "protein_g_per_100g": item.get("p_per_100"),
                "carbs_g_per_100g": item.get("c_per_100"),
                "fat_g_per_100g": item.get("f_per_100"),
            }
        )
    return {
        "ok": True,
        "template_id": detail.get("id"),
        "title": detail.get("title"),
        "description": detail.get("description"),
        "default_servings": detail.get("default_servings"),
        "notes": detail.get("notes"),
        "items": items,
        "kcal_per_serving": detail.get("kcal_per_serving"),
        "protein_g_per_serving": detail.get("p_per_serving"),
        "carbs_g_per_serving": detail.get("c_per_serving"),
        "fat_g_per_serving": detail.get("f_per_serving"),
        "created_at": detail.get("created_at"),
        "updated_at": detail.get("updated_at"),
    }


def list_nutrition_plan_details() -> list[dict[str, Any]]:
    ensure_nutrition_planning_schema()
    rows = list_week_templates(include_archived=True)
    out = []
    for row in rows:
        detail = get_nutrition_plan_detail(int(row["id"]))
        out.append(
            {
                "plan_id": detail["plan_id"],
                "name": detail["name"],
                "is_active": detail["is_active"],
                "status": "active" if detail["is_active"] else "inactive",
                "archived": detail["archived"],
                "deleted": detail["deleted"],
                "created_at": detail["created_at"],
                "updated_at": detail["updated_at"],
                "totals": detail["totals"],
                "days_count": detail["counts"]["days_count"],
                "meals_count": detail["counts"]["meals_count"],
                "slots_count": detail["counts"]["slots_count"],
            }
        )
    return out


def get_active_nutrition_plan_detail() -> dict[str, Any]:
    plans = [row for row in list_week_templates(include_archived=True) if row.get("is_active")]
    if not plans:
        raise ValueError("no_active_plan")
    return get_nutrition_plan_detail(int(plans[0]["id"]))


def get_nutrition_plan_detail(plan_id: int) -> dict[str, Any]:
    ensure_nutrition_planning_schema()
    template_row = _load_nutrition_template_row(plan_id)
    if not template_row:
        raise ValueError("plan_not_found")
    payload = _build_week_plan_payload_wrapper(plan_id)
    if not payload.get("ok"):
        raise ValueError("plan_not_found")
    week = payload.get("week_template") if isinstance(payload.get("week_template"), dict) else {}
    warnings: list[dict[str, Any]] = []
    days_out = []
    plan_kcal = plan_p = plan_c = plan_f = 0.0
    meal_count = 0
    slot_count = 0
    empty_placeholder_count = 0
    for day_index, day in enumerate(week.get("days") or []):
        meals_out = []
        day_kcal = day_p = day_c = day_f = 0.0
        for meal_index, slot in enumerate(day.get("slots") or []):
            meal_count += 1
            meal_id = slot.get("id")
            meal_title = slot.get("custom_title") or slot.get("meal_title") or f"Meal {meal_index + 1}"
            raw_items = []
            if isinstance((slot.get("override") or {}).get("ingredients"), list):
                raw_items = list((slot.get("override") or {}).get("ingredients") or [])
                warnings.append({"code": "slot_override_ingredients_used", "slot_path": f"d{day_index}:m{meal_index}"})
            elif isinstance(slot.get("items"), list):
                raw_items = list(slot.get("items") or [])
            slots_out = []
            meal_kcal = meal_p = meal_c = meal_f = 0.0
            for item_index, item in enumerate(raw_items):
                slot_count += 1
                amount_struct = _nutrition_slot_amount_fields(item.get("amount"), item.get("unit") or "g")
                amount_g = amount_struct["amount_g"]
                slot_path = _nutrition_slot_path(day_index, meal_index, item_index)
                food_id = _int_or_none(item.get("food_id"))
                food = _load_food_by_id(food_id)
                macros = _slot_macros_from_food(food, amount_struct.get("amount"), amount_struct.get("unit"))
                kcal = macros.get("kcal")
                protein = macros.get("protein_g")
                carbs = macros.get("carbs_g")
                fat = macros.get("fat_g")
                if macros.get("macro_resolution") != "resolved":
                    warnings.append({"code": "unresolved_item_macros", "slot_path": slot_path, "unit": item.get("unit")})
                meal_kcal += kcal or 0.0
                meal_p += protein or 0.0
                meal_c += carbs or 0.0
                meal_f += fat or 0.0
                slots_out.append(
                    {
                        "slot_id": None,
                        "slot_path": slot_path,
                        "slot_index": item_index,
                        "day_index": day_index,
                        "meal_index": meal_index,
                        **amount_struct,
                        "food_id": food_id,
                        "food_name": item.get("food_name") or item.get("name") or item.get("display_text"),
                        "kcal": kcal,
                        "protein_g": protein,
                        "carbs_g": carbs,
                        "fat_g": fat,
                        "macro_resolution": macros.get("macro_resolution"),
                        "macro_basis": macros.get("macro_basis"),
                    }
                )
            meal_empty = len(slots_out) == 0
            meal_placeholder = _is_placeholder_meal_title(meal_title) or meal_empty
            if meal_empty:
                empty_placeholder_count += 1
                warnings.append(
                    {
                        "code": "empty_meal_placeholder",
                        "day_index": day_index,
                        "meal_index": meal_index,
                        "title": meal_title,
                    }
                )
            meal_totals = {
                "kcal": round(meal_kcal, 1),
                "protein_g": round(meal_p, 1),
                "carbs_g": round(meal_c, 1),
                "fat_g": round(meal_f, 1),
            }
            day_kcal += meal_kcal
            day_p += meal_p
            day_c += meal_c
            day_f += meal_f
            meals_out.append(
                {
                    "meal_id": meal_id,
                    "meal_template_id": slot.get("meal_template_id"),
                    "meal_index": meal_index,
                    "title": meal_title,
                    "time_text": slot.get("time_text"),
                    "note_text": slot.get("note_text"),
                    "slots": slots_out,
                    "totals": meal_totals,
                    "empty": meal_empty,
                    "placeholder": meal_placeholder,
                }
            )
        day_totals = {
            "kcal": round(day_kcal, 1),
            "protein_g": round(day_p, 1),
            "carbs_g": round(day_c, 1),
            "fat_g": round(day_f, 1),
        }
        plan_kcal += day_kcal
        plan_p += day_p
        plan_c += day_c
        plan_f += day_f
        days_out.append(
            {
                "day_id": day.get("id"),
                "day_index": day_index,
                "title": day.get("label"),
                "label": day.get("label"),
                "meals": meals_out,
                "totals": day_totals,
            }
        )
    detail = {
        "ok": True,
        "plan_id": template_row.get("id"),
        "name": template_row.get("title") or week.get("title"),
        "status": "active" if template_row.get("is_active") else ("archived" if template_row.get("archived_at") else "inactive"),
        "is_active": bool(template_row.get("is_active")),
        "archived": bool(template_row.get("archived_at")),
        "deleted": False,
        "deleted_at": None,
        "created_at": template_row.get("created_at"),
        "updated_at": template_row.get("updated_at"),
        "revision": week.get("revision") or template_row.get("revision") or 1,
        "day_groups": payload.get("day_groups") or [],
        "grouping_contract": payload.get("grouping_contract") or {},
        "days": days_out,
        "totals": {
            "kcal": round(plan_kcal, 1),
            "protein_g": round(plan_p, 1),
            "carbs_g": round(plan_c, 1),
            "fat_g": round(plan_f, 1),
        },
        "counts": {
            "days_count": len(days_out),
            "meals_count": meal_count,
            "slots_count": slot_count,
        },
        "empty_placeholder_count": empty_placeholder_count,
        "warnings": warnings,
    }
    return _recalculate_nutrition_mutable(detail)


def _nutrition_mutable_from_detail(detail: dict[str, Any]) -> dict[str, Any]:
    return _recalculate_nutrition_mutable(
        {
        "name": detail["name"],
        "days": copy.deepcopy(detail["days"]),
        "meta": {
            "plan_id": detail["plan_id"],
            "is_active": detail["is_active"],
            "archived": detail["archived"],
        },
        }
    )


def _nutrition_export_write_payload(mutable: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": mutable["name"],
        "days": [
            {
                "day": day.get("label") or WEEKDAY_ORDER[int(day.get("day_index") or 0)],
                "meals": [
                    {
                        "title": meal.get("title") or "Meal",
                        "time": meal.get("time_text"),
                        "items": [
                            {
                                "food_id": slot.get("food_id"),
                                "amount": slot.get("amount"),
                                "unit": slot.get("unit") or "g",
                            }
                            for slot in (meal.get("slots") or [])
                        ],
                    }
                    for meal in (day.get("meals") or [])
                ],
            }
            for day in (mutable.get("days") or [])
        ],
    }


def _normalize_nutrition_overwrite_days(days: Any) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for day_index, day in enumerate(days or []):
        if not isinstance(day, dict):
            continue
        label = str(day.get("day") or day.get("label") or day.get("title") or WEEKDAY_ORDER[day_index % len(WEEKDAY_ORDER)]).strip() or WEEKDAY_ORDER[day_index % len(WEEKDAY_ORDER)]
        meals_in = day.get("meals") if isinstance(day.get("meals"), list) else day.get("slots")
        meals_out = []
        for meal_index, meal in enumerate(meals_in or []):
            if not isinstance(meal, dict):
                continue
            title = str(meal.get("title") or meal.get("meal_title") or f"Meal {meal_index + 1}").strip() or f"Meal {meal_index + 1}"
            note = meal.get("note") if meal.get("note") is not None else meal.get("note_text")
            time_text = meal.get("time") if meal.get("time") is not None else meal.get("time_text")
            items = meal.get("items") if isinstance(meal.get("items"), list) else meal.get("slots")
            slots = []
            for item_index, item in enumerate(items or []):
                if not isinstance(item, dict):
                    continue
                food_id = _int_or_none(item.get("food_id"))
                food = _load_food_by_id(food_id)
                unit = str(item.get("unit") or (food or {}).get("unit_default") or "g").strip().lower() or "g"
                amount = item.get("amount")
                if amount is None and unit == "g":
                    amount = item.get("amount_g")
                slots.append(
                    {
                        "slot_id": None,
                        "slot_path": _nutrition_slot_path(day_index, meal_index, item_index),
                        "slot_index": item_index,
                        "food_id": food_id,
                        "food_name": item.get("food_name") or item.get("name"),
                        "amount": _float_or_none(amount),
                        "unit": unit,
                    }
                )
            meals_out.append(
                {
                    "meal_id": _int_or_none(meal.get("meal_id")),
                    "meal_template_id": _int_or_none(meal.get("meal_template_id") or meal.get("template_id")),
                    "meal_index": meal_index,
                    "title": title,
                    "time_text": time_text,
                    "note_text": note,
                    "slots": slots,
                }
            )
        normalized.append({"day_index": day_index, "label": label, "title": label, "meals": meals_out})
    return normalized


def _nutrition_slot_candidates(mutable: dict[str, Any], match: dict[str, Any]) -> list[dict[str, Any]]:
    candidates = []
    slot_id = _int_or_none(match.get("slot_id"))
    slot_path = str(match.get("slot_path") or "").strip()
    day_index = _int_or_none(match.get("day_index"))
    meal_index = _int_or_none(match.get("meal_index"))
    slot_index = _int_or_none(match.get("slot_index"))
    food_id = _int_or_none(match.get("food_id"))
    food_name = _norm(match.get("food_name"))
    parsed_slot_path = _parse_slot_path(slot_path) if slot_path else None
    if slot_path and parsed_slot_path is None:
        raise ValueError("invalid_slot_path")
    if parsed_slot_path is not None:
        day_index, meal_index, slot_index = parsed_slot_path
    for d_idx, day in enumerate(mutable.get("days") or []):
        if day_index is not None and day_index != d_idx:
            continue
        for m_idx, meal in enumerate(day.get("meals") or []):
            if meal_index is not None and meal_index != m_idx:
                continue
            for s_idx, slot in enumerate(meal.get("slots") or []):
                if slot_id is not None and _int_or_none(slot.get("slot_id")) != slot_id:
                    continue
                if slot_path and str(slot.get("slot_path") or _nutrition_slot_path(d_idx, m_idx, s_idx)) != slot_path:
                    continue
                if slot_index is not None and s_idx != slot_index:
                    continue
                if food_id is not None and _int_or_none(slot.get("food_id")) != food_id:
                    continue
                if food_name and _norm(slot.get("food_name")) != food_name:
                    continue
                candidates.append({"day": day, "meal": meal, "slot": slot, "day_index": d_idx, "meal_index": m_idx, "slot_index": s_idx})
    return candidates


def _nutrition_meal_candidates(mutable: dict[str, Any], match: dict[str, Any]) -> list[dict[str, Any]]:
    candidates = []
    meal_id = _int_or_none(match.get("meal_id"))
    day_index = _int_or_none(match.get("day_index"))
    meal_index = _int_or_none(match.get("meal_index"))
    for d_idx, day in enumerate(mutable.get("days") or []):
        if day_index is not None and day_index != d_idx:
            continue
        for m_idx, meal in enumerate(day.get("meals") or []):
            if meal_id is not None and _int_or_none(meal.get("meal_id")) != meal_id:
                continue
            if meal_index is not None and m_idx != meal_index:
                continue
            candidates.append({"day": day, "meal": meal, "day_index": d_idx, "meal_index": m_idx})
    return candidates


def _require_single_or_scope(candidates: list[dict[str, Any]], scope: str | None) -> list[dict[str, Any]]:
    if not candidates:
        raise ValueError("no_matching_slot")
    if len(candidates) > 1 and scope not in {"first", "all"}:
        raise ValueError("multiple_matches_require_scope")
    return candidates[:1] if scope != "all" else candidates


def patch_nutrition_plan_v2(plan_id: int, operations: list[dict[str, Any]], *, dry_run: bool = True) -> dict[str, Any]:
    if not isinstance(operations, list) or not operations:
        raise ValueError("operations are required")
    before = get_nutrition_plan_detail(plan_id)
    mutable = _nutrition_mutable_from_detail(before)
    warnings: list[dict[str, Any]] = []
    active_changed = False
    affected_resources = [
        "nutrition.nutrition_week_templates",
        "nutrition.nutrition_week_template_days",
        "nutrition.nutrition_week_day_slots",
        "nutrition.nutrition_slot_meals",
        "nutrition.nutrition_slot_meal_items",
    ]
    created_resources: list[dict[str, Any]] = []
    for raw in operations:
        if not isinstance(raw, dict):
            raise ValueError("invalid_operation")
        op = str(raw.get("op") or "").strip()
        if op == "rename_plan":
            mutable["name"] = str(raw.get("name") or "").strip() or mutable["name"]
        elif op == "rename_day":
            day_index = _int_or_none((raw.get("match") or {}).get("day_index"))
            if day_index is None or day_index >= len(mutable["days"]):
                raise ValueError("day_not_found")
            mutable["days"][day_index]["title"] = str(raw.get("title") or "").strip() or mutable["days"][day_index]["title"]
            mutable["days"][day_index]["label"] = mutable["days"][day_index]["title"]
        elif op == "rename_meal":
            candidates = _nutrition_meal_candidates(mutable, raw.get("match") if isinstance(raw.get("match"), dict) else {})
            chosen = _require_single_or_scope(candidates, "first")[0]
            chosen["meal"]["title"] = str(raw.get("title") or "").strip() or chosen["meal"]["title"]
        elif op == "update_meal_time":
            candidates = _nutrition_meal_candidates(mutable, raw.get("match") if isinstance(raw.get("match"), dict) else {})
            chosen = _require_single_or_scope(candidates, "first")[0]
            chosen["meal"]["time_text"] = str(raw.get("time") or "").strip() or None
        elif op == "replace_meal_items":
            candidates = _nutrition_meal_candidates(mutable, raw.get("match") if isinstance(raw.get("match"), dict) else {})
            chosen = _require_single_or_scope(candidates, "first")[0]
            raw_items = raw.get("items")
            if not isinstance(raw_items, list) or not raw_items:
                raise ValueError("items_required")
            replacement_slots: list[dict[str, Any]] = []
            for slot_index, raw_item in enumerate(raw_items):
                if not isinstance(raw_item, dict):
                    raise ValueError("invalid_item")
                food_id = _int_or_none(raw_item.get("food_id"))
                food = _load_food_by_id(food_id)
                if not food:
                    raise ValueError("food_not_found")
                unit = str(raw_item.get("unit") or "").strip().lower()
                amount = _float_or_none(raw_item.get("amount"))
                if amount is None and raw_item.get("amount_g") is not None:
                    amount = _float_or_none(raw_item.get("amount_g"))
                    unit = unit or "g"
                if amount is None:
                    raise ValueError("amount_required")
                if amount <= 0:
                    raise ValueError("invalid_amount")
                unit = unit or str(food.get("unit_default") or "g").strip().lower() or "g"
                slot = {
                    "slot_id": None,
                    "slot_path": _nutrition_slot_path(chosen["day_index"], chosen["meal_index"], slot_index),
                    "slot_index": slot_index,
                    "food_id": int(food["id"]),
                    "food_name": food["name"],
                    "amount": amount,
                    "unit": unit,
                }
                slot.update(_nutrition_slot_amount_fields(amount, unit))
                slot.update(_slot_macros_from_food(food, slot.get("amount"), slot.get("unit")))
                replacement_slots.append(slot)
            chosen["meal"]["slots"] = replacement_slots
        elif op == "set_meal_note":
            candidates = _nutrition_meal_candidates(mutable, raw.get("match") if isinstance(raw.get("match"), dict) else {})
            chosen = _require_single_or_scope(candidates, "first")[0]
            chosen["meal"]["note_text"] = str(raw.get("note") or "").strip() or None
        elif op == "move_meal":
            candidates = _nutrition_meal_candidates(mutable, raw.get("match") if isinstance(raw.get("match"), dict) else {})
            chosen = _require_single_or_scope(candidates, "first")[0]
            target = raw.get("target") if isinstance(raw.get("target"), dict) else {}
            dest_day_index = _int_or_none(target.get("day_index"))
            dest_meal_index = _int_or_none(target.get("meal_index"))
            if dest_day_index is None or dest_meal_index is None:
                raise ValueError("target_required")
            try:
                source_day_meals = mutable["days"][chosen["day_index"]].setdefault("meals", [])
                target_day_meals = mutable["days"][dest_day_index].setdefault("meals", [])
            except Exception as exc:
                raise ValueError("target_not_found") from exc
            meal_payload = copy.deepcopy(chosen["meal"])
            source_day_meals.pop(chosen["meal_index"])
            insert_at = max(0, min(dest_meal_index, len(target_day_meals)))
            target_day_meals.insert(insert_at, meal_payload)
        elif op == "replace_slot_food":
            match = raw.get("match") if isinstance(raw.get("match"), dict) else {}
            candidates = _require_single_or_scope(_nutrition_slot_candidates(mutable, match), raw.get("scope"))
            repl = raw.get("replacement") if isinstance(raw.get("replacement"), dict) else {}
            food_id = _int_or_none(repl.get("food_id"))
            food = _load_food_by_id(food_id)
            if not food:
                raise ValueError("food_not_found")
            for item in candidates:
                item["slot"]["food_id"] = int(food["id"])
                item["slot"]["food_name"] = food["name"]
                # Determine unit and amount: prefer explicit replacement fields
                if repl.get("amount_g") is not None:
                    unit = "g"
                    amount = float(repl["amount_g"])
                else:
                    # replacement may provide amount+unit, otherwise keep existing
                    unit = str(repl.get("unit") or food.get("unit_default") or item["slot"].get("unit") or "g").strip().lower()
                    amount = _float_or_none(repl.get("amount")) if repl.get("amount") is not None else _float_or_none(item["slot"].get("amount"))
                item["slot"]["unit"] = unit
                item["slot"]["amount"] = amount
                # amount_g only if unit is grams
                item["slot"]["amount_g"] = amount if unit == "g" and amount is not None else None
                item["slot"].update(_slot_macros_from_food(food, item["slot"].get("amount"), item["slot"].get("unit")))
        elif op == "update_slot_amount":
            match = raw.get("match") if isinstance(raw.get("match"), dict) else {}
            candidates = _require_single_or_scope(_nutrition_slot_candidates(mutable, match), raw.get("scope"))
            unit = str(raw.get("unit") or "").strip().lower()
            amount = _float_or_none(raw.get("amount"))
            if amount is None and raw.get("amount_g") is not None:
                amount = _float_or_none(raw.get("amount_g"))
                unit = unit or "g"
            if amount is None:
                raise ValueError("amount_required")
            unit = unit or str((candidates[0]["slot"] or {}).get("unit") or "g").strip().lower() or "g"
            for item in candidates:
                item["slot"]["amount"] = amount
                item["slot"]["unit"] = unit
                item["slot"]["amount_g"] = amount if unit == "g" else None
                food = _load_food_by_id(_int_or_none(item["slot"].get("food_id")))
                item["slot"].update(_nutrition_slot_amount_fields(amount, unit))
                item["slot"].update(_slot_macros_from_food(food, item["slot"].get("amount"), item["slot"].get("unit")))
        elif op == "delete_slot":
            match = raw.get("match") if isinstance(raw.get("match"), dict) else {}
            candidates = _require_single_or_scope(_nutrition_slot_candidates(mutable, match), "first")
            item = candidates[0]
            meal_slots = item["meal"].get("slots") or []
            meal_slots.pop(item["slot_index"])
        elif op == "delete_meal":
            candidates = _nutrition_meal_candidates(mutable, raw.get("match") if isinstance(raw.get("match"), dict) else {})
            chosen = _require_single_or_scope(candidates, "first")[0]
            chosen["day"]["meals"].pop(chosen["meal_index"])
        elif op == "add_meal":
            target = raw.get("target") if isinstance(raw.get("target"), dict) else {}
            day_index = _int_or_none(target.get("day_index"))
            meal_index = _int_or_none(target.get("meal_index"))
            if day_index is None:
                raise ValueError("target_required")
            try:
                target_meals = mutable["days"][day_index].setdefault("meals", [])
            except Exception as exc:
                raise ValueError("target_not_found") from exc
            raw_items = raw.get("items")
            if not isinstance(raw_items, list) or not raw_items:
                raise ValueError("items_required")
            meal_slots: list[dict[str, Any]] = []
            insert_at = len(target_meals) if meal_index is None else max(0, min(meal_index, len(target_meals)))
            for slot_index, raw_item in enumerate(raw_items):
                if not isinstance(raw_item, dict):
                    raise ValueError("invalid_item")
                food_id = _int_or_none(raw_item.get("food_id"))
                food = _load_food_by_id(food_id)
                if not food:
                    raise ValueError("food_not_found")
                unit = str(raw_item.get("unit") or food.get("unit_default") or "g").strip().lower() or "g"
                amount = _float_or_none(raw_item.get("amount"))
                if amount is None and raw_item.get("amount_g") is not None:
                    amount = _float_or_none(raw_item.get("amount_g"))
                    unit = "g"
                if amount is None:
                    raise ValueError("amount_required")
                if amount <= 0:
                    raise ValueError("invalid_amount")
                slot = {"slot_id": None, "slot_index": slot_index, "food_id": int(food["id"]), "food_name": food["name"], "amount": amount, "unit": unit}
                slot.update(_nutrition_slot_amount_fields(amount, unit))
                slot.update(_slot_macros_from_food(food, amount, unit))
                meal_slots.append(slot)
            target_meals.insert(
                insert_at,
                {
                    "meal_id": None,
                    "meal_template_id": None,
                    "meal_index": insert_at,
                    "title": str(raw.get("title") or "Meal").strip() or "Meal",
                    "time_text": str(raw.get("time") or "").strip() or None,
                    "note_text": str(raw.get("note") or "").strip() or None,
                    "slots": meal_slots,
                },
            )
        elif op == "delete_empty_meals":
            for day in mutable.get("days") or []:
                day["meals"] = [
                    meal
                    for meal in (day.get("meals") or [])
                    if not (len(meal.get("slots") or []) == 0 and (_is_placeholder_meal_title(meal.get("title")) or bool(meal.get("placeholder"))))
                ]
        elif op == "add_slot":
            target = raw.get("target") if isinstance(raw.get("target"), dict) else {}
            day_index = _int_or_none(target.get("day_index"))
            meal_index = _int_or_none(target.get("meal_index"))
            if day_index is None or meal_index is None:
                raise ValueError("target_required")
            try:
                meal = mutable["days"][day_index]["meals"][meal_index]
            except Exception as exc:
                raise ValueError("target_not_found") from exc
            food_id = _int_or_none(raw.get("food_id"))
            food = _load_food_by_id(food_id)
            if not food:
                raise ValueError("food_not_found")
            unit = str(raw.get("unit") or food.get("unit_default") or "g").strip().lower() or "g"
            amount = _float_or_none(raw.get("amount"))
            if amount is None and raw.get("amount_g") is not None:
                amount = _float_or_none(raw.get("amount_g"))
                unit = "g"
            slot = {"slot_id": None, "slot_path": f"d{day_index}:m{meal_index}:new{len(meal.get('slots') or [])}", "slot_index": len(meal.get("slots") or []), "food_id": int(food["id"]), "food_name": food["name"], "amount": amount, "unit": unit}
            slot.update(_nutrition_slot_amount_fields(amount, unit))
            slot.update(_slot_macros_from_food(food, slot.get("amount"), slot.get("unit")))
            meal.setdefault("slots", []).append(slot)
        elif op == "move_slot":
            match = raw.get("match") if isinstance(raw.get("match"), dict) else {}
            target = raw.get("target") if isinstance(raw.get("target"), dict) else {}
            candidates = _require_single_or_scope(_nutrition_slot_candidates(mutable, match), "first")
            item = candidates[0]
            dest_day = _int_or_none(target.get("day_index"))
            dest_meal = _int_or_none(target.get("meal_index"))
            dest_slot = _int_or_none(target.get("slot_index"))
            if None in {dest_day, dest_meal}:
                raise ValueError("target_required")
            slot_payload = copy.deepcopy(item["slot"])
            item["meal"]["slots"].pop(item["slot_index"])
            target_slots = mutable["days"][dest_day]["meals"][dest_meal].setdefault("slots", [])
            insert_at = len(target_slots) if dest_slot is None else max(0, min(dest_slot, len(target_slots)))
            target_slots.insert(insert_at, slot_payload)
        elif op == "duplicate_plan":
            warnings.append({"code": "duplicate_plan_requested", "name": raw.get("name")})
        elif op == "set_active_plan":
            active_changed = not before["is_active"]
            mutable["meta"]["is_active"] = True
        elif op == "archive_plan":
            mutable["meta"]["archived"] = True
            active_changed = before["is_active"]
            mutable["meta"]["is_active"] = False
        elif op == "unarchive_plan":
            mutable["meta"]["archived"] = False
        elif op == "soft_delete_plan":
            mutable["meta"]["deleted"] = True
            active_changed = before["is_active"]
            mutable["meta"]["is_active"] = False
        elif op == "create_food":
            food_payload = raw.get("food") if isinstance(raw.get("food"), dict) else {}
            normalized_food = {
                "name": food_payload.get("name"),
                "unit_default": food_payload.get("default_unit") or food_payload.get("unit_default") or "g",
                "kcal_per_100": food_payload.get("kcal_per_100g") if food_payload.get("kcal_per_100g") is not None else food_payload.get("kcal_per_100"),
                "p_per_100": food_payload.get("protein_g_per_100g") if food_payload.get("protein_g_per_100g") is not None else food_payload.get("p_per_100"),
                "c_per_100": food_payload.get("carbs_g_per_100g") if food_payload.get("carbs_g_per_100g") is not None else food_payload.get("c_per_100"),
                "f_per_100": food_payload.get("fat_g_per_100g") if food_payload.get("fat_g_per_100g") is not None else food_payload.get("f_per_100"),
            }
            if not dry_run:
                created, err = create_food(normalized_food)
                if err or not created:
                    raise ValueError(err or "db_write_failed")
                created_resources.append({"type": "food", "food_id": created.get("id"), "name": created.get("name")})
                affected_resources.append("nutrition.nutrition_foods")
        elif op == "update_food":
            food_id = _int_or_none(raw.get("food_id"))
            patch = raw.get("patch") if isinstance(raw.get("patch"), dict) else {}
            normalized_patch = dict(patch)
            if "default_unit" in normalized_patch and "unit_default" not in normalized_patch:
                normalized_patch["unit_default"] = normalized_patch.pop("default_unit")
            for source_key, target_key in {
                "kcal_per_100g": "kcal_per_100",
                "protein_g_per_100g": "p_per_100",
                "carbs_g_per_100g": "c_per_100",
                "fat_g_per_100g": "f_per_100",
            }.items():
                if source_key in normalized_patch and target_key not in normalized_patch:
                    normalized_patch[target_key] = normalized_patch.pop(source_key)
            if not dry_run:
                updated, err = update_food(int(food_id or 0), normalized_patch)
                if err or not updated:
                    raise ValueError(err or "db_write_failed")
                affected_resources.append("nutrition.nutrition_foods")
        elif op in {"delete_food", "soft_delete_food", "archive_food"}:
            food_id = _int_or_none(raw.get("food_id"))
            if not food_id:
                raise ValueError("food_not_found")
            if not dry_run:
                deleted, error = delete_food(int(food_id), force=_bool(raw.get("force"), default=False))
                if not deleted:
                    raise ValueError((error or {}).get("error") if isinstance(error, dict) else (error or "db_write_failed"))
                affected_resources.append("nutrition.nutrition_foods")
        elif op == "create_meal_template":
            template_payload = raw.get("template") if isinstance(raw.get("template"), dict) else {}
            normalized_template = {
                "title": template_payload.get("title"),
                "ingredients": template_payload.get("items") or template_payload.get("ingredients") or [],
                "notes": template_payload.get("notes"),
                "default_servings": template_payload.get("default_servings"),
            }
            if not dry_run:
                created, err = create_meal_template(normalized_template)
                if err or not created:
                    raise ValueError(err or "db_write_failed")
                created_resources.append({"type": "meal_template", "template_id": created.get("id"), "title": created.get("title")})
                affected_resources.append("nutrition.nutrition_meal_templates")
                affected_resources.append("nutrition.nutrition_meal_ingredients")
        elif op == "update_meal_template":
            template_id = _int_or_none(raw.get("template_id"))
            normalized_template = {
                "title": raw.get("title"),
                "ingredients": raw.get("items") if isinstance(raw.get("items"), list) else raw.get("ingredients"),
                "notes": raw.get("notes"),
            }
            normalized_template = {key: value for key, value in normalized_template.items() if value is not None}
            if not dry_run:
                updated, err = update_meal_template(int(template_id or 0), normalized_template)
                if err or not updated:
                    raise ValueError(err or "db_write_failed")
                affected_resources.append("nutrition.nutrition_meal_templates")
                affected_resources.append("nutrition.nutrition_meal_ingredients")
        elif op in {"delete_meal_template", "soft_delete_meal_template", "archive_meal_template"}:
            template_id = _int_or_none(raw.get("template_id"))
            if not template_id:
                raise ValueError("template_not_found")
            if not dry_run:
                deleted, err = delete_meal_template(int(template_id))
                if not deleted:
                    raise ValueError(err or "db_write_failed")
                affected_resources.append("nutrition.nutrition_meal_templates")
                affected_resources.append("nutrition.nutrition_meal_ingredients")
        elif op == "add_meal_from_template":
            target = raw.get("target") if isinstance(raw.get("target"), dict) else {}
            day_index = _int_or_none(target.get("day_index"))
            meal_index = _int_or_none(target.get("meal_index"))
            template_id = _int_or_none(raw.get("template_id"))
            if day_index is None or meal_index is None or template_id is None:
                raise ValueError("target_required")
            template_detail = get_nutrition_meal_template_detail(template_id)
            meal_entry = {
                "meal_id": None,
                "meal_template_id": template_id,
                "meal_index": meal_index,
                "title": template_detail.get("title"),
                "time_text": None,
                "note_text": template_detail.get("notes"),
                "slots": [
                    {
                        "slot_id": None,
                        "food_id": item.get("food_id"),
                        "food_name": item.get("food_name"),
                        "amount": item.get("amount"),
                        "unit": item.get("unit"),
                    }
                    for item in (template_detail.get("items") or [])
                ],
            }
            mutable["days"][day_index].setdefault("meals", [])
            insert_at = max(0, min(meal_index, len(mutable["days"][day_index]["meals"])))
            mutable["days"][day_index]["meals"].insert(insert_at, meal_entry)
        elif op == "overwrite_plan":
            mutable["days"] = _normalize_nutrition_overwrite_days(raw.get("days"))
        else:
            raise ValueError("unknown_operation")
    recalculated = _recalculate_nutrition_mutable(copy.deepcopy(mutable))
    after_preview = get_nutrition_plan_detail(plan_id)
    after_preview["name"] = recalculated["name"]
    after_preview["days"] = recalculated["days"]
    after_preview["warnings"] = list(after_preview.get("warnings") or []) + warnings
    after_preview["archived"] = bool(recalculated["meta"].get("archived"))
    after_preview["is_active"] = bool(recalculated["meta"].get("is_active"))
    after_preview["deleted"] = bool(recalculated["meta"].get("deleted"))
    after_preview["status"] = "active" if after_preview["is_active"] else ("archived" if after_preview["archived"] else "inactive")
    after_preview["totals"] = recalculated["totals"]
    after_preview["counts"] = recalculated["counts"]
    after_preview["empty_placeholder_count"] = recalculated.get("empty_placeholder_count", 0)
    if dry_run:
        return {
            "ok": True,
            "execution": {"mode": "dry_run", "live_state_changed": False, "affected_resources": sorted(set(affected_resources))},
            "before": before,
            "after": after_preview,
            "diff": _plan_diff(before, after_preview),
            "active_plan_changed": active_changed,
            "validation": {"operations_count": len(operations), "created_resources": created_resources},
            "warnings": after_preview.get("warnings") or [],
        }
    wrote_mutable = False
    catalog_only_ops = {
        "create_food",
        "update_food",
        "delete_food",
        "soft_delete_food",
        "archive_food",
        "create_meal_template",
        "update_meal_template",
        "delete_meal_template",
        "soft_delete_meal_template",
        "archive_meal_template",
    }
    for raw in operations:
        op = str((raw or {}).get("op") or "").strip()
        if op == "duplicate_plan":
            # Direct DB copy without validation - preserves empty placeholder meals
            new_template, err = create_week_template(
                title=str(raw.get("name") or f"{recalculated['name']} Kopie"),
                source_template_id=plan_id,
                set_active=False
            )
            if err or not new_template:
                raise ValueError(err or "duplicate_plan_failed")
            created_resources.append({
                "type": "nutrition_plan",
                "plan_id": int(new_template.get("id")),
                "name": new_template.get("title")
            })
            affected_resources.append("nutrition.nutrition_week_templates")
            affected_resources.append("nutrition.nutrition_week_template_days")
            affected_resources.append("nutrition.nutrition_week_day_slots")
        elif op == "set_active_plan":
            activate_nutrition_plan(plan_id, dry_run=False)
        elif op == "archive_plan":
            archive_nutrition_plan(plan_id, dry_run=False)
        elif op == "unarchive_plan":
            updated, err = restore_week_template(plan_id)
            if err or not updated:
                raise ValueError("plan_not_found")
        elif op == "soft_delete_plan":
            archive_nutrition_plan(plan_id, dry_run=False)
        elif op in catalog_only_ops:
            # Catalog-only operations already persisted themselves above.
            # Do not rewrite the whole nutrition plan afterwards; that can
            # reinsert stale mutable slot state, especially after delete_food.
            pass
        else:
            wrote_mutable = True
    if wrote_mutable:
        _persist_nutrition_plan_mutable(plan_id, recalculated)
    final = get_nutrition_plan_detail(plan_id)
    if any(str((raw or {}).get("op") or "") == "set_active_plan" for raw in operations):
        active_detail = get_active_nutrition_plan_detail()
        active_list = list_nutrition_plan_details()
        active_ids = [int(item["plan_id"]) for item in active_list if item.get("is_active")]
        if active_detail.get("plan_id") != plan_id or final.get("is_active") is not True or final.get("status") != "active" or active_ids != [plan_id]:
            raise ValueError("db_write_failed")
        final["is_active"] = True
        final["status"] = "active"
    if any(str((raw or {}).get("op") or "") == "archive_plan" for raw in operations):
        final["archived"] = True
        final["status"] = "archived"
    if any(str((raw or {}).get("op") or "") == "soft_delete_plan" for raw in operations):
        final["deleted"] = True
        final["archived"] = True
        final["status"] = "archived"
    return {
        "ok": True,
        "execution": {"mode": "write", "live_state_changed": True, "affected_resources": sorted(set(affected_resources))},
        "before": before,
        "after": final,
        "diff": _plan_diff(before, final),
        "active_plan_changed": active_changed,
        "validation": {"operations_count": len(operations), "created_resources": created_resources},
        "warnings": final.get("warnings") or [],
    }


def get_nutrition_plan_detail_from_mutable(mutable: dict[str, Any]) -> dict[str, Any]:
    recalculated = _recalculate_nutrition_mutable(copy.deepcopy(mutable))
    return {"totals": dict(recalculated.get("totals") or {}), "counts": dict(recalculated.get("counts") or {})}


def _ensure_legacy_slot_meal(slot_id: int, title: str | None) -> None:
    conn = connections.get_nutrition_db()
    conn.row_factory = sqlite3.Row
    try:
        slot = conn.execute("SELECT * FROM nutrition_week_day_slots WHERE id=?", (slot_id,)).fetchone()
        if not slot:
            raise ValueError("db_write_failed")
        existing = conn.execute(
            """
            SELECT id FROM nutrition_slot_meals
            WHERE week_template_day_id=? AND slot_index=?
            ORDER BY id DESC LIMIT 1
            """,
            (slot["week_template_day_id"], slot["slot_index"]),
        ).fetchone()
        if existing:
            return
        now = _utcnow_iso()
        conn.execute(
            """
            INSERT INTO nutrition_slot_meals
                (week_template_day_id, slot_index, meal_title, multiplier, template_id, created_at, updated_at)
            VALUES (?, ?, ?, 1, NULL, ?, ?)
            """,
            (slot["week_template_day_id"], slot["slot_index"], str(title or "Meal").strip() or "Meal", now, now),
        )
        conn.commit()
    finally:
        conn.close()


def _delete_slot_source(slot_id: int) -> None:
    conn = connections.get_nutrition_db()
    conn.row_factory = sqlite3.Row
    try:
        slot = conn.execute("SELECT * FROM nutrition_week_day_slots WHERE id=?", (slot_id,)).fetchone()
        if not slot:
            raise ValueError("db_write_failed")
        slot_meal = conn.execute(
            """
            SELECT id FROM nutrition_slot_meals
            WHERE week_template_day_id=? AND slot_index=?
            ORDER BY id DESC LIMIT 1
            """,
            (slot["week_template_day_id"], slot["slot_index"]),
        ).fetchone()
        if slot_meal:
            conn.execute("DELETE FROM nutrition_slot_meal_items WHERE slot_meal_id=?", (slot_meal["id"],))
            conn.execute("DELETE FROM nutrition_slot_meals WHERE id=?", (slot_meal["id"],))
        conn.execute("DELETE FROM nutrition_slot_meal_overrides WHERE slot_id=?", (slot_id,))
        conn.commit()
    finally:
        conn.close()


def _clear_slot_contents(slot_id: int) -> None:
    conn = connections.get_nutrition_db()
    conn.row_factory = sqlite3.Row
    try:
        slot = conn.execute("SELECT * FROM nutrition_week_day_slots WHERE id=?", (slot_id,)).fetchone()
        if not slot:
            raise ValueError("db_write_failed")
        now = _utcnow_iso()
        conn.execute("DELETE FROM nutrition_slot_meal_overrides WHERE slot_id=?", (slot_id,))
        slot_meal = conn.execute(
            """
            SELECT id FROM nutrition_slot_meals
            WHERE week_template_day_id=? AND slot_index=?
            ORDER BY id DESC LIMIT 1
            """,
            (slot["week_template_day_id"], slot["slot_index"]),
        ).fetchone()
        if slot_meal:
            conn.execute("DELETE FROM nutrition_slot_meal_items WHERE slot_meal_id=?", (slot_meal["id"],))
        conn.execute("UPDATE nutrition_week_day_slots SET meal_template_id=NULL, updated_at=? WHERE id=?", (now, slot_id))
        conn.commit()
    finally:
        conn.close()


def _write_slot_meal_source(slot_id: int, meal: dict[str, Any]) -> None:
    conn = connections.get_nutrition_db()
    conn.row_factory = sqlite3.Row
    try:
        slot = conn.execute("SELECT * FROM nutrition_week_day_slots WHERE id=?", (slot_id,)).fetchone()
        if not slot:
            raise ValueError("db_write_failed")
        now = _utcnow_iso()
        title = str(meal.get("title") or "Meal").strip() or "Meal"
        slot_meal = conn.execute(
            """
            SELECT * FROM nutrition_slot_meals
            WHERE week_template_day_id=? AND slot_index=?
            ORDER BY id DESC LIMIT 1
            """,
            (slot["week_template_day_id"], slot["slot_index"]),
        ).fetchone()
        if slot_meal:
            slot_meal_id = int(slot_meal["id"])
            conn.execute(
                "UPDATE nutrition_slot_meals SET meal_title=?, updated_at=? WHERE id=?",
                (title, now, slot_meal_id),
            )
        else:
            cursor = conn.execute(
                """
                INSERT INTO nutrition_slot_meals
                    (week_template_day_id, slot_index, meal_title, multiplier, template_id, created_at, updated_at)
                VALUES (?, ?, ?, 1, NULL, ?, ?)
                """,
                (slot["week_template_day_id"], slot["slot_index"], title, now, now),
            )
            slot_meal_id = int(cursor.lastrowid)
        conn.execute("DELETE FROM nutrition_slot_meal_items WHERE slot_meal_id=?", (slot_meal_id,))
        seen: set[tuple[Any, ...]] = set()
        for item in meal.get("slots") or []:
            food_id = _int_or_none(item.get("food_id"))
            amount = _float_or_none(item.get("amount"))
            unit = str(item.get("unit") or "g").strip().lower() or "g"
            if food_id is None or amount is None:
                continue
            dedupe_key = (food_id, amount, unit, _norm(item.get("food_name")))
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            conn.execute(
                """
                INSERT INTO nutrition_slot_meal_items
                    (slot_meal_id, amount, unit, name_raw, food_id, item_type, calories, display_text, fingerprint, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, 'food', NULL, NULL, NULL, ?, ?)
                """,
                (slot_meal_id, amount, unit, item.get("food_name"), food_id, now, now),
            )
        conn.execute("DELETE FROM nutrition_slot_meal_overrides WHERE slot_id=?", (slot_id,))
        conn.execute(
            "UPDATE nutrition_week_day_slots SET meal_template_id=COALESCE(meal_template_id, NULL), updated_at=? WHERE id=?",
            (now, slot_id),
        )
        conn.commit()
    finally:
        conn.close()


def _persist_nutrition_plan_mutable(plan_id: int, mutable: dict[str, Any]) -> None:
    conn = connections.get_nutrition_db()
    conn.row_factory = sqlite3.Row
    try:
        now = _utcnow_iso()
        conn.execute("BEGIN")
        conn.execute(
            "UPDATE nutrition_week_templates SET title=?, updated_at=? WHERE id=?",
            (str(mutable.get("name") or "Plan").strip() or "Plan", now, plan_id),
        )
        day_rows = conn.execute(
            "SELECT * FROM nutrition_week_template_days WHERE week_template_id=? ORDER BY weekday, id",
            (plan_id,),
        ).fetchall()
        day_by_index = {idx: dict(row) for idx, row in enumerate(day_rows)}
        for day_index, day_row in day_by_index.items():
            slot_rows = conn.execute(
                "SELECT id FROM nutrition_week_day_slots WHERE week_template_day_id=? ORDER BY slot_index, id",
                (day_row["id"],),
            ).fetchall()
            slot_ids = [int(row["id"]) for row in slot_rows]
            if slot_ids:
                placeholders = ",".join("?" for _ in slot_ids)
                conn.execute(
                    f"DELETE FROM nutrition_slot_meal_items WHERE slot_meal_id IN (SELECT id FROM nutrition_slot_meals WHERE week_template_day_id=? )",
                    (day_row["id"],),
                )
                conn.execute("DELETE FROM nutrition_slot_meals WHERE week_template_day_id=?", (day_row["id"],))
                conn.execute(f"DELETE FROM nutrition_slot_meal_overrides WHERE slot_id IN ({placeholders})", tuple(slot_ids))
            conn.execute("DELETE FROM nutrition_week_day_slots WHERE week_template_day_id=?", (day_row["id"],))
            desired_day = next((entry for entry in (mutable.get("days") or []) if _int_or_none(entry.get("day_index")) == day_index), None)
            if not desired_day:
                continue
            for meal_index, meal in enumerate(desired_day.get("meals") or []):
                slot_template_id = _get_or_create_slot_template_id(conn.cursor(), f"Meal {meal_index + 1}")
                cursor = conn.execute(
                    """
                    INSERT INTO nutrition_week_day_slots
                        (week_template_day_id, slot_template_id, slot_index, active_option_id, locked, time_text, custom_title, note_text, meal_template_id, created_at, updated_at)
                    VALUES (?, ?, ?, NULL, 0, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        day_row["id"],
                        slot_template_id,
                        meal_index,
                        meal.get("time_text"),
                        meal.get("title"),
                        meal.get("note_text"),
                        # A patched meal is persisted from its canonical slot
                        # items below. Keeping the old template link can make a
                        # stale template win again on later reads.
                        None,
                        now,
                        now,
                    ),
                )
                slot_id = int(cursor.lastrowid)
                meal_slots = []
                for slot in meal.get("slots") or []:
                    food_id = _int_or_none(slot.get("food_id"))
                    amount = _float_or_none(slot.get("amount"))
                    unit = str(slot.get("unit") or "g").strip().lower() or "g"
                    if food_id is None or amount is None:
                        continue
                    meal_slots.append({"food_id": food_id, "food_name": slot.get("food_name"), "amount": amount, "unit": unit})
                if meal_slots:
                    slot_meal_cursor = conn.execute(
                        """
                        INSERT INTO nutrition_slot_meals
                            (week_template_day_id, slot_index, meal_title, multiplier, template_id, created_at, updated_at)
                        VALUES (?, ?, ?, 1, NULL, ?, ?)
                        """,
                        (day_row["id"], meal_index, str(meal.get("title") or "Meal").strip() or "Meal", now, now),
                    )
                    slot_meal_id = int(slot_meal_cursor.lastrowid)
                    for slot in meal_slots:
                        conn.execute(
                            """
                            INSERT INTO nutrition_slot_meal_items
                                (slot_meal_id, amount, unit, name_raw, food_id, item_type, calories, display_text, fingerprint, created_at, updated_at)
                            VALUES (?, ?, ?, ?, ?, 'food', NULL, NULL, NULL, ?, ?)
                            """,
                            (slot_meal_id, slot["amount"], slot["unit"], slot.get("food_name"), slot["food_id"], now, now),
                        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise ValueError("db_write_failed")
    finally:
        conn.close()


def list_training_plan_details() -> list[dict[str, Any]]:
    conn = connections.get_plans_db()
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("SELECT id FROM gym_plans ORDER BY is_active DESC, updated_at DESC, id DESC").fetchall()
    finally:
        conn.close()
    return [get_training_plan_list_item(int(row["id"])) for row in rows]


def search_training_exercises(query: str, *, limit: int = 25) -> list[dict[str, Any]]:
    conn = connections.get_training_db()
    conn.row_factory = sqlite3.Row
    try:
        like = f"%{str(query or '').strip().lower()}%"
        has_library = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='movement_library'"
        ).fetchone()
        rows = (
            conn.execute(
                """
                SELECT id, movement_key, label, muscle_group, metadata_json
                FROM movement_library
                WHERE ? = '%%' OR LOWER(label) LIKE ? OR LOWER(movement_key) LIKE ?
                ORDER BY label ASC, id ASC
                LIMIT ?
                """,
                (like, like, like, limit),
            ).fetchall()
            if has_library
            else []
        )
        remaining = max(0, limit - len(rows))
        historical = []
        if remaining and conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='exercises'").fetchone():
            columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(exercises)").fetchall()}
            variation_sql = "COALESCE(variation, '')" if "variation" in columns else "''"
            device_sql = "COALESCE(device, '')" if "device" in columns else "''"
            laterality_sql = "COALESCE(laterality, '')" if "laterality" in columns else "''"
            historical = conn.execute(
                f"""
                SELECT MIN(id) AS id, name, {variation_sql} AS variation,
                       {device_sql} AS device, {laterality_sql} AS laterality,
                       COUNT(*) AS session_count, MAX(workout_id) AS latest_workout_id
                FROM exercises
                WHERE ? = '%%' OR LOWER(name) LIKE ? OR LOWER({variation_sql}) LIKE ? OR LOWER({device_sql}) LIKE ?
                GROUP BY LOWER(name), LOWER({variation_sql}), LOWER({device_sql}), LOWER({laterality_sql})
                ORDER BY session_count DESC, name ASC
                LIMIT ?
                """,
                (like, like, like, like, remaining),
            ).fetchall()
    finally:
        conn.close()
    out = []
    for row in rows:
        metadata = json.loads(row["metadata_json"] or "{}") if row["metadata_json"] else {}
        out.append(
            {
                "exercise_id": row["id"],
                "canonical_id": row["movement_key"],
                "name": row["label"],
                "muscle_group": row["muscle_group"],
                "equipment": metadata.get("equipment"),
                "notes": metadata.get("notes"),
                "source": "movement_library",
            }
        )
    known = {(str(item.get("name") or "").casefold(), str(item.get("equipment") or "").casefold()) for item in out}
    for row in historical:
        variation = str(row["variation"] or row["device"] or "").strip() or None
        key = (str(row["name"] or "").casefold(), str(variation or "").casefold())
        if key in known:
            continue
        out.append(
            {
                "exercise_id": row["id"],
                "canonical_id": None,
                "name": row["name"],
                "variation": str(row["variation"] or "").strip() or None,
                "equipment": str(row["device"] or "").strip() or None,
                "laterality": str(row["laterality"] or "").strip() or None,
                "session_count": int(row["session_count"] or 0),
                "latest_workout_id": row["latest_workout_id"],
                "source": "training_history",
            }
        )
    return out[:limit]


def get_training_exercise_detail(exercise_id: int) -> dict[str, Any]:
    conn = connections.get_training_db()
    conn.row_factory = sqlite3.Row
    try:
        has_library = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='movement_library'").fetchone()
        row = conn.execute(
            "SELECT id, movement_key, label, muscle_group, metadata_json FROM movement_library WHERE id=? LIMIT 1",
            (exercise_id,),
        ).fetchone() if has_library else None
        historical = None
        if not row and conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='exercises'").fetchone():
            columns = {str(info[1]) for info in conn.execute("PRAGMA table_info(exercises)").fetchall()}
            variation_sql = "COALESCE(variation, '')" if "variation" in columns else "''"
            device_sql = "COALESCE(device, '')" if "device" in columns else "''"
            laterality_sql = "COALESCE(laterality, '')" if "laterality" in columns else "''"
            historical = conn.execute(
                f"SELECT id, workout_id, name, {variation_sql} AS variation, {device_sql} AS device, {laterality_sql} AS laterality FROM exercises WHERE id=? LIMIT 1",
                (exercise_id,),
            ).fetchone()
            if historical:
                session_count = conn.execute(
                    f"SELECT COUNT(*) FROM exercises WHERE LOWER(name)=LOWER(?) AND LOWER({variation_sql})=LOWER(?) AND LOWER({device_sql})=LOWER(?) AND LOWER({laterality_sql})=LOWER(?)",
                    (historical["name"], historical["variation"], historical["device"], historical["laterality"]),
                ).fetchone()[0]
    finally:
        conn.close()
    if not row and not historical:
        raise ValueError("exercise_not_found")
    if historical:
        return {
            "ok": True,
            "exercise_id": historical["id"],
            "canonical_id": None,
            "name": historical["name"],
            "variation": str(historical["variation"] or "").strip() or None,
            "equipment": str(historical["device"] or "").strip() or None,
            "laterality": str(historical["laterality"] or "").strip() or None,
            "session_count": int(session_count or 0),
            "example_workout_id": historical["workout_id"],
            "source": "training_history",
        }
    metadata = json.loads(row["metadata_json"] or "{}") if row["metadata_json"] else {}
    return {
        "ok": True,
        "exercise_id": row["id"],
        "canonical_id": row["movement_key"],
        "name": row["label"],
        "muscle_group": row["muscle_group"],
        "equipment": metadata.get("equipment"),
        "notes": metadata.get("notes"),
        "source": "movement_library",
    }


def get_training_plan_list_item(plan_id: int) -> dict[str, Any]:
    detail = get_training_plan_detail(plan_id)
    return {
        "plan_id": detail["plan_id"],
        "name": detail["name"],
        "is_active": detail["is_active"],
        "archived": detail["archived"],
        "deleted": detail["deleted"],
        "created_at": detail["created_at"],
        "updated_at": detail["updated_at"],
        "totals": detail["totals"],
        "days_count": detail["counts"]["days_count"],
        "meals_count": detail["counts"]["workouts_count"],
        "slots_count": detail["counts"]["exercises_count"],
    }


def _load_training_row(plan_id: int | None = None, *, active_only: bool = False) -> dict[str, Any]:
    conn = connections.get_plans_db()
    conn.row_factory = sqlite3.Row
    try:
        if active_only:
            row = conn.execute("SELECT * FROM gym_plans WHERE is_active=1 AND COALESCE(is_archived, 0)=0 ORDER BY updated_at DESC, id DESC LIMIT 1").fetchone()
        else:
            row = conn.execute("SELECT * FROM gym_plans WHERE id=?", (plan_id,)).fetchone()
    finally:
        conn.close()
    if not row:
        raise ValueError("plan_not_found")
    data = dict(row)
    data["plan"] = json.loads(data.get("plan_json") or "{}")
    return data


def get_active_training_plan_detail() -> dict[str, Any]:
    row = _load_training_row(active_only=True)
    return get_training_plan_detail(int(row["id"]))


def get_training_plan_detail(plan_id: int) -> dict[str, Any]:
    row = _load_training_row(plan_id)
    plan = row.get("plan") if isinstance(row.get("plan"), dict) else {}
    base_week = plan.get("base_week") if isinstance(plan.get("base_week"), dict) else {}
    sequence = plan.get("sequence") if isinstance(plan.get("sequence"), list) else []
    is_rolling = str((plan.get("meta") or {}).get("mode") or "").strip().lower() == "rolling_sequence" and bool(sequence)
    # Rolling plans may retain a calendar mirror for legacy consumers. Keep
    # those days in the detail for compatibility, but append the authoritative
    # sequence as one explicit `rotation` day for stable selectors.
    day_titles = plan.get("day_titles") if isinstance(plan.get("day_titles"), dict) else {}
    rules = json.loads(row.get("rules_json") or "{}") if row.get("rules_json") else {}
    blocks = json.loads(row.get("blocks_json") or "{}") if row.get("blocks_json") else {}
    periodization = ((rules.get("liva_operator") or {}).get("periodization")) if isinstance(rules, dict) else None
    plan_caps = ((rules.get("liva_operator") or {}).get("caps")) if isinstance(rules, dict) else None
    days = []
    workouts_count = exercises_count = sets_count = 0
    warnings = []
    day_order = WEEKDAY_ORDER + ["rotation"] if is_rolling else WEEKDAY_ORDER
    for day_index, weekday in enumerate(day_order):
        source_events = sequence if weekday == "rotation" else base_week.get(weekday)
        events = source_events if isinstance(source_events, list) else []
        workouts = []
        for workout_index, event in enumerate(events):
            workouts_count += 1
            exercises = []
            for exercise_index, item in enumerate(event.get("items") or []):
                if str(item.get("kind") or "exercise") != "exercise":
                    continue
                exercises_count += 1
                set_rows = []
                reps = item.get("reps") if isinstance(item.get("reps"), dict) else {}
                rpe_list = item.get("rpe_list") if isinstance(item.get("rpe_list"), list) else []
                set_target_reps = item.get("set_target_reps") if isinstance(item.get("set_target_reps"), list) else []
                for set_index in range(int(item.get("sets") or len(rpe_list) or 0)):
                    sets_count += 1
                    set_rows.append(
                        {
                            "set_path": _training_set_path(day_index, workout_index, exercise_index, set_index),
                            "set_index": set_index,
                            "target_reps": set_target_reps[set_index] if set_index < len(set_target_reps) else (reps.get("min") if reps else item.get("reps")),
                            "rep_range": reps if reps else None,
                            # A single trailing RPE target applies to remaining
                            # work sets as well; exposing null here made the
                            # direct plan detail disagree with normalized views.
                            "target_rpe": rpe_list[set_index] if set_index < len(rpe_list) else (rpe_list[-1] if rpe_list else None),
                            "rir": item.get("rir"),
                            "load_target": (item.get("load_targets") or [None])[set_index] if isinstance(item.get("load_targets"), list) and set_index < len(item.get("load_targets")) else item.get("load_target"),
                            "notes": ((item.get("set_notes") or [None])[set_index] if isinstance(item.get("set_notes"), list) and set_index < len(item.get("set_notes")) else None),
                        }
                    )
                exercises.append(
                    {
                        "exercise_path": _training_exercise_path(day_index, workout_index, exercise_index),
                        "exercise_id": item.get("id") or f"{weekday}:{workout_index}:{exercise_index}",
                        "canonical_id": item.get("canonical_id"),
                        "name": item.get("name"),
                        "variation": item.get("variation"),
                        "order": exercise_index,
                        "sets": set_rows,
                        "target_reps": reps.get("min") if reps else item.get("reps"),
                        "rep_range": reps if reps else None,
                        "target_rpe": rpe_list[0] if rpe_list else None,
                        "rir": item.get("rir"),
                        "rest_seconds": item.get("rest_seconds"),
                        "notes": item.get("note"),
                        "progression_rule": item.get("progression_rule"),
                        "progression": item.get("progression") if isinstance(item.get("progression"), dict) else None,
                        "caps": item.get("caps") if isinstance(item.get("caps"), dict) else None,
                    }
                )
            workouts.append(
                {
                    "workout_id": event.get("id") or f"{weekday}:{workout_index}",
                    "workout_index": workout_index,
                    "title": event.get("title") or weekday,
                    "order": workout_index,
                    "notes": event.get("note"),
                    "exercises": exercises,
                }
            )
        title = str(day_titles.get(weekday) or ("rotation" if is_rolling else weekday))
        days.append({"day_id": weekday, "day_index": day_index, "title": title, "label": title, "workouts": workouts})
    return {
        "ok": True,
        "plan_id": row.get("id"),
        "name": row.get("title"),
        "status": "active" if row.get("is_active") else ("archived" if row.get("is_archived") else "inactive"),
        "is_active": bool(row.get("is_active")),
        "archived": bool(row.get("is_archived")),
        "deleted": False,
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
        "days": days,
        "totals": {"workouts": workouts_count, "exercises": exercises_count, "sets": sets_count},
        "counts": {"days_count": len(days), "workouts_count": workouts_count, "exercises_count": exercises_count, "sets_count": sets_count},
        "periodization": periodization or {},
        "caps": plan_caps or {},
        "rules_json": rules if isinstance(rules, dict) else {},
        "blocks_json": blocks if isinstance(blocks, dict) else {},
        "warnings": warnings,
    }


def _training_exercise_candidates(mutable: dict[str, Any], match: dict[str, Any]) -> list[dict[str, Any]]:
    candidates = []
    exercise_id = str(match.get("exercise_id") or "").strip()
    canonical_id = str(match.get("canonical_id") or "").strip()
    exercise_path = str(match.get("exercise_path") or "").strip()
    name = _norm(match.get("name"))
    day_index = _int_or_none(match.get("day_index"))
    workout_index = _int_or_none(match.get("workout_index"))
    exercise_index = _int_or_none(match.get("exercise_index"))
    parsed_path = _parse_training_exercise_path(exercise_path) if exercise_path else None
    if exercise_path and parsed_path is None:
        raise ValueError("invalid_exercise_path")
    if parsed_path is not None:
        day_index, workout_index, exercise_index = parsed_path
    for d_idx, day in enumerate(mutable.get("days") or []):
        if day_index is not None and d_idx != day_index:
            continue
        for w_idx, workout in enumerate(day.get("workouts") or []):
            if workout_index is not None and w_idx != workout_index:
                continue
            for e_idx, ex in enumerate(workout.get("exercises") or []):
                if exercise_id and str(ex.get("exercise_id") or "") != exercise_id:
                    continue
                if canonical_id and str(ex.get("canonical_id") or "") != canonical_id:
                    continue
                if name and _norm(ex.get("name")) != name:
                    continue
                if exercise_index is not None and e_idx != exercise_index:
                    continue
                candidates.append({"day": day, "workout": workout, "exercise": ex, "day_index": d_idx, "workout_index": w_idx, "exercise_index": e_idx})
    return candidates


def _training_set_candidates(mutable: dict[str, Any], match: dict[str, Any]) -> list[dict[str, Any]]:
    candidates = []
    set_path = str(match.get("set_path") or "").strip()
    set_index = _int_or_none(match.get("set_index"))
    parsed_path = _parse_training_set_path(set_path) if set_path else None
    if set_path and parsed_path is None:
        raise ValueError("invalid_set_path")
    exercise_match = dict(match)
    if parsed_path is not None:
        d_idx, w_idx, e_idx, s_idx = parsed_path
        exercise_match.update({"day_index": d_idx, "workout_index": w_idx, "exercise_index": e_idx})
        set_index = s_idx
    for candidate in _training_exercise_candidates(mutable, exercise_match):
        for idx, row in enumerate(candidate["exercise"].get("sets") or []):
            if set_index is not None and idx != set_index:
                continue
            candidates.append({**candidate, "set": row, "set_index": idx})
    return candidates


def patch_training_plan_v2(plan_id: int, operations: list[dict[str, Any]], *, dry_run: bool = True) -> dict[str, Any]:
    if not isinstance(operations, list) or not operations:
        raise ValueError("operations are required")
    before = get_training_plan_detail(plan_id)
    mutable = copy.deepcopy(before)
    active_changed = False
    affected_resources = ["plans.gym_plans"]
    created_resources: list[dict[str, Any]] = []
    for raw in operations:
        if not isinstance(raw, dict):
            raise ValueError("invalid_operation")
        op = str(raw.get("op") or "").strip()
        if op == "rename_plan":
            mutable["name"] = str(raw.get("name") or "").strip() or mutable["name"]
        elif op in {"rename_day", "rename_workout"}:
            match = raw.get("match") if isinstance(raw.get("match"), dict) else {}
            day_index = _int_or_none(match.get("day_index"))
            workout_index = _int_or_none(match.get("workout_index"))
            if day_index is None:
                raise ValueError("day_not_found")
            if op == "rename_day":
                mutable["days"][day_index]["title"] = str(raw.get("title") or "").strip() or mutable["days"][day_index]["title"]
                mutable["days"][day_index]["label"] = mutable["days"][day_index]["title"]
            else:
                mutable["days"][day_index]["workouts"][workout_index]["title"] = str(raw.get("title") or "").strip() or mutable["days"][day_index]["workouts"][workout_index]["title"]
        elif op == "set_workout_note":
            match = raw.get("match") if isinstance(raw.get("match"), dict) else {}
            day_index = _int_or_none(match.get("day_index"))
            workout_index = _int_or_none(match.get("workout_index"))
            if day_index is None or workout_index is None:
                raise ValueError("workout_not_found")
            mutable["days"][day_index]["workouts"][workout_index]["notes"] = str(raw.get("note") or "").strip() or None
        elif op == "rename_exercise_slot":
            chosen = _require_single_or_scope(_training_exercise_candidates(mutable, raw.get("match") if isinstance(raw.get("match"), dict) else {}), "first")[0]
            chosen["exercise"]["name"] = str(raw.get("title") or raw.get("name") or "").strip() or chosen["exercise"]["name"]
        elif op == "replace_exercise":
            chosen = _require_single_or_scope(_training_exercise_candidates(mutable, raw.get("match") if isinstance(raw.get("match"), dict) else {}), "first")[0]
            replacement = raw.get("replacement") if isinstance(raw.get("replacement"), dict) else {}
            chosen["exercise"]["name"] = str(replacement.get("name") or chosen["exercise"]["name"])
            chosen["exercise"]["canonical_id"] = replacement.get("canonical_id")
            if replacement.get("variation") is not None:
                chosen["exercise"]["variation"] = replacement.get("variation")
            if replacement.get("exercise_id") is not None:
                chosen["exercise"]["exercise_id"] = replacement.get("exercise_id")
        elif op == "update_exercise_variation":
            chosen = _require_single_or_scope(_training_exercise_candidates(mutable, raw.get("match") if isinstance(raw.get("match"), dict) else {}), "first")[0]
            chosen["exercise"]["variation"] = str(raw.get("variation") or "").strip() or None
        elif op == "update_exercise_note":
            chosen = _require_single_or_scope(_training_exercise_candidates(mutable, raw.get("match") if isinstance(raw.get("match"), dict) else {}), "first")[0]
            chosen["exercise"]["notes"] = str(raw.get("note") or "").strip() or None
        elif op == "update_exercise_target":
            chosen = _require_single_or_scope(_training_exercise_candidates(mutable, raw.get("match") if isinstance(raw.get("match"), dict) else {}), "first")[0]
            patch = raw.get("patch") if isinstance(raw.get("patch"), dict) else raw
            target_sets = _int_or_none(patch.get("target_sets"))
            exercise_sets = chosen["exercise"].setdefault("sets", [])
            if target_sets is not None and target_sets >= 0:
                while len(exercise_sets) < target_sets:
                    exercise_sets.append(
                        {
                            "set_path": _training_set_path(chosen["day_index"], chosen["workout_index"], chosen["exercise_index"], len(exercise_sets)),
                            "set_index": len(exercise_sets),
                            "target_reps": None,
                            "rep_range": None,
                            "target_rpe": None,
                            "rir": chosen["exercise"].get("rir"),
                            "load_target": None,
                            "notes": None,
                        }
                    )
                if len(exercise_sets) > target_sets:
                    del exercise_sets[target_sets:]
            if patch.get("target_reps") is not None:
                chosen["exercise"]["target_reps"] = patch.get("target_reps")
                for target_set in exercise_sets:
                    target_set["target_reps"] = patch.get("target_reps")
            if isinstance(patch.get("rep_range"), dict):
                chosen["exercise"]["rep_range"] = patch.get("rep_range")
                for target_set in exercise_sets:
                    target_set["rep_range"] = copy.deepcopy(patch.get("rep_range"))
            if patch.get("target_rpe") is not None:
                chosen["exercise"]["target_rpe"] = patch.get("target_rpe")
                for target_set in exercise_sets:
                    target_set["target_rpe"] = patch.get("target_rpe")
            if isinstance(patch.get("rpe_list"), list):
                for idx, value in enumerate(patch.get("rpe_list") or []):
                    if idx >= len(exercise_sets):
                        break
                    exercise_sets[idx]["target_rpe"] = value
                if patch.get("rpe_list"):
                    chosen["exercise"]["target_rpe"] = (patch.get("rpe_list") or [None])[0]
            if patch.get("rest_seconds") is not None:
                chosen["exercise"]["rest_seconds"] = patch.get("rest_seconds")
            if patch.get("notes") is not None:
                chosen["exercise"]["notes"] = patch.get("notes")
                for target_set in exercise_sets:
                    target_set["notes"] = patch.get("notes")
            if patch.get("rir") is not None:
                chosen["exercise"]["rir"] = patch.get("rir")
                for target_set in exercise_sets:
                    target_set["rir"] = patch.get("rir")
        elif op == "update_set_target":
            set_match = raw.get("match") if isinstance(raw.get("match"), dict) else {}
            chosen = _require_single_or_scope(_training_set_candidates(mutable, set_match), "first")[0]
            target_set = chosen["set"]
            if raw.get("target_reps") is not None:
                target_set["target_reps"] = raw.get("target_reps")
            if isinstance(raw.get("rep_range"), dict):
                target_set["rep_range"] = raw.get("rep_range")
            if raw.get("target_rpe") is not None:
                target_set["target_rpe"] = raw.get("target_rpe")
            if raw.get("rir") is not None:
                target_set["rir"] = raw.get("rir")
            if raw.get("load_target") is not None:
                target_set["load_target"] = raw.get("load_target")
            if raw.get("notes") is not None:
                target_set["notes"] = raw.get("notes")
        elif op == "add_exercise":
            target = raw.get("target") if isinstance(raw.get("target"), dict) else {}
            day_index = _int_or_none(target.get("day_index"))
            workout_index = _int_or_none(target.get("workout_index"))
            exercise = {
                "exercise_id": raw.get("exercise_id"),
                "canonical_id": raw.get("canonical_id"),
                "name": raw.get("name"),
                "variation": raw.get("variation"),
                "order": len(mutable["days"][day_index]["workouts"][workout_index].get("exercises") or []),
                "sets": [],
                "target_reps": raw.get("target_reps"),
                "rep_range": raw.get("rep_range") if isinstance(raw.get("rep_range"), dict) else None,
                "target_rpe": raw.get("target_rpe"),
                "rir": raw.get("rir"),
                "rest_seconds": raw.get("rest_seconds"),
                "notes": raw.get("notes"),
                "progression_rule": raw.get("progression_rule"),
                "caps": raw.get("caps") if isinstance(raw.get("caps"), dict) else None,
            }
            for set_index, set_payload in enumerate(raw.get("sets") or []):
                if not isinstance(set_payload, dict):
                    continue
                exercise["sets"].append(
                    {
                        "set_path": _training_set_path(day_index, workout_index, len(mutable["days"][day_index]["workouts"][workout_index].get("exercises") or []), set_index),
                        "set_index": set_index,
                        "target_reps": set_payload.get("target_reps"),
                        "rep_range": set_payload.get("rep_range") if isinstance(set_payload.get("rep_range"), dict) else None,
                        "target_rpe": set_payload.get("target_rpe"),
                        "rir": set_payload.get("rir"),
                        "load_target": set_payload.get("load_target"),
                        "notes": set_payload.get("notes"),
                    }
                )
            target_exercises = mutable["days"][day_index]["workouts"][workout_index].setdefault("exercises", [])
            position = _int_or_none(target.get("position"))
            insert_at = len(target_exercises) if position is None else max(0, min(position, len(target_exercises)))
            target_exercises.insert(insert_at, exercise)
        elif op == "delete_exercise":
            chosen = _require_single_or_scope(_training_exercise_candidates(mutable, raw.get("match") if isinstance(raw.get("match"), dict) else {}), "first")[0]
            chosen["workout"]["exercises"].pop(chosen["exercise_index"])
        elif op == "move_exercise":
            chosen = _require_single_or_scope(_training_exercise_candidates(mutable, raw.get("match") if isinstance(raw.get("match"), dict) else {}), "first")[0]
            target = raw.get("target") if isinstance(raw.get("target"), dict) else {}
            dest_day = _int_or_none(target.get("day_index"))
            dest_workout = _int_or_none(target.get("workout_index"))
            dest_index = _int_or_none(target.get("exercise_index"))
            exercise = copy.deepcopy(chosen["exercise"])
            chosen["workout"]["exercises"].pop(chosen["exercise_index"])
            target_exercises = mutable["days"][dest_day]["workouts"][dest_workout].setdefault("exercises", [])
            if dest_index is None:
                dest_index = _int_or_none(target.get("position"))
            target_exercises.insert(len(target_exercises) if dest_index is None else dest_index, exercise)
        elif op == "add_set":
            target = raw.get("target") if isinstance(raw.get("target"), dict) else {}
            chosen = _require_single_or_scope(_training_exercise_candidates(mutable, target), "first")[0]
            set_payload = raw.get("set") if isinstance(raw.get("set"), dict) else {}
            set_index = len(chosen["exercise"].setdefault("sets", []))
            chosen["exercise"]["sets"].append(
                {
                    "set_path": _training_set_path(chosen["day_index"], chosen["workout_index"], chosen["exercise_index"], set_index),
                    "set_index": set_index,
                    "target_reps": set_payload.get("target_reps"),
                    "rep_range": set_payload.get("rep_range") if isinstance(set_payload.get("rep_range"), dict) else None,
                    "target_rpe": set_payload.get("target_rpe"),
                    "rir": set_payload.get("rir"),
                    "load_target": set_payload.get("load_target"),
                    "notes": set_payload.get("notes"),
                }
            )
        elif op == "delete_set":
            set_match = raw.get("match") if isinstance(raw.get("match"), dict) else {}
            chosen = _require_single_or_scope(_training_set_candidates(mutable, set_match), "first")[0]
            chosen["exercise"]["sets"].pop(chosen["set_index"])
        elif op == "update_periodization":
            mutable["periodization"] = raw.get("periodization") if isinstance(raw.get("periodization"), dict) else {}
        elif op == "update_progression_rule":
            chosen = _require_single_or_scope(_training_exercise_candidates(mutable, raw.get("match") if isinstance(raw.get("match"), dict) else {}), "first")[0]
            chosen["exercise"]["progression_rule"] = raw.get("progression_rule") if isinstance(raw.get("progression_rule"), dict) else {}
        elif op == "update_caps":
            if isinstance(raw.get("match"), dict):
                chosen = _require_single_or_scope(_training_exercise_candidates(mutable, raw.get("match")), "first")[0]
                chosen["exercise"]["caps"] = raw.get("caps") if isinstance(raw.get("caps"), dict) else {}
            else:
                mutable["caps"] = raw.get("caps") if isinstance(raw.get("caps"), dict) else {}
        elif op == "create_exercise":
            exercise_payload = raw.get("exercise") if isinstance(raw.get("exercise"), dict) else {}
            if not dry_run:
                conn = connections.get_training_db()
                try:
                    conn.execute(
                        """
                        CREATE TABLE IF NOT EXISTS movement_library (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            movement_key TEXT NOT NULL,
                            label TEXT NOT NULL,
                            muscle_group TEXT,
                            metadata_json TEXT
                        )
                        """
                    )
                    conn.execute(
                        "INSERT INTO movement_library (movement_key, label, muscle_group, metadata_json) VALUES (?, ?, ?, ?)",
                        (
                            str(exercise_payload.get("canonical_id") or exercise_payload.get("name") or "").strip().lower(),
                            str(exercise_payload.get("name") or "").strip() or "Neue Übung",
                            exercise_payload.get("muscle_group"),
                            json.dumps(
                                {
                                    "equipment": exercise_payload.get("equipment"),
                                    "notes": exercise_payload.get("notes"),
                                },
                                ensure_ascii=False,
                            ),
                        ),
                    )
                    conn.commit()
                    created_resources.append({"type": "exercise", "canonical_id": exercise_payload.get("canonical_id"), "name": exercise_payload.get("name")})
                    affected_resources.append("training.movement_library")
                finally:
                    conn.close()
        elif op == "update_exercise":
            exercise_id = _int_or_none(raw.get("exercise_id"))
            patch = raw.get("patch") if isinstance(raw.get("patch"), dict) else {}
            if not exercise_id:
                raise ValueError("exercise_not_found")
            if not dry_run:
                conn = connections.get_training_db()
                conn.row_factory = sqlite3.Row
                try:
                    row = conn.execute("SELECT id, movement_key, label, muscle_group, metadata_json FROM movement_library WHERE id=?", (exercise_id,)).fetchone()
                    if not row:
                        raise ValueError("exercise_not_found")
                    metadata = json.loads(row["metadata_json"] or "{}") if row["metadata_json"] else {}
                    if "equipment" in patch:
                        metadata["equipment"] = patch.get("equipment")
                    if "notes" in patch:
                        metadata["notes"] = patch.get("notes")
                    conn.execute(
                        """
                        UPDATE movement_library
                        SET movement_key=?, label=?, muscle_group=?, metadata_json=?
                        WHERE id=?
                        """,
                        (
                            str(patch.get("canonical_id") or row["movement_key"] or "").strip().lower(),
                            str(patch.get("name") or row["label"] or "").strip() or row["label"],
                            patch.get("muscle_group") if "muscle_group" in patch else row["muscle_group"],
                            json.dumps(metadata, ensure_ascii=False),
                            exercise_id,
                        ),
                    )
                    conn.commit()
                    affected_resources.append("training.movement_library")
                finally:
                    conn.close()
        elif op == "overwrite_plan":
            days = raw.get("days") if isinstance(raw.get("days"), list) else []
            mutable["days"] = copy.deepcopy(days)
        elif op == "duplicate_plan":
            pass
        elif op == "set_active_plan":
            active_changed = not before["is_active"]
            mutable["is_active"] = True
        elif op == "archive_plan":
            active_changed = before["is_active"]
            mutable["archived"] = True
            mutable["is_active"] = False
        elif op == "unarchive_plan":
            mutable["archived"] = False
        elif op == "soft_delete_plan":
            active_changed = before["is_active"]
            mutable["archived"] = True
            mutable["deleted"] = True
            mutable["is_active"] = False
        else:
            raise ValueError("unknown_operation")
    if dry_run:
        return {"ok": True, "execution": {"mode": "dry_run", "live_state_changed": False, "affected_resources": sorted(set(affected_resources))}, "before": before, "after": mutable, "diff": _plan_diff(before, mutable), "active_plan_changed": active_changed, "validation": {"operations_count": len(operations), "created_resources": created_resources}, "warnings": mutable.get("warnings") or []}
    _persist_training_plan(plan_id, mutable, operations)
    after = get_training_plan_detail(plan_id)
    return {"ok": True, "execution": {"mode": "write", "live_state_changed": True, "affected_resources": sorted(set(affected_resources))}, "before": before, "after": after, "diff": _plan_diff(before, after), "active_plan_changed": active_changed, "validation": {"operations_count": len(operations), "created_resources": created_resources}, "warnings": after.get("warnings") or []}


def _persist_training_plan(plan_id: int, mutable: dict[str, Any], operations: list[dict[str, Any]]) -> None:
    row = _load_training_row(plan_id)
    plan = row.get("plan") if isinstance(row.get("plan"), dict) else {}
    rules = json.loads(row.get("rules_json") or "{}") if row.get("rules_json") else {}
    blocks = json.loads(row.get("blocks_json") or "{}") if row.get("blocks_json") else {}
    plan["name"] = mutable["name"]
    new_base_week: dict[str, list[dict[str, Any]]] = {}
    day_titles: dict[str, str] = {}
    new_sequence: list[dict[str, Any]] = []
    new_days: list[dict[str, Any]] = []
    rolling_mode = str((plan.get("meta") or {}).get("mode") or "").strip().lower() == "rolling_sequence"
    days_to_persist = [day for day in (mutable.get("days") or []) if str(day.get("day_id") or day.get("label") or "") == "rotation"] if rolling_mode else list(mutable.get("days") or [])
    for day in days_to_persist:
        weekday = str(day.get("day_id") or day.get("label") or "")
        day_titles[weekday] = str(day.get("title") or day.get("label") or weekday)
        new_base_week[weekday] = []
        day_events: list[dict[str, Any]] = []
        for workout in day.get("workouts") or []:
            items = []
            for ex in workout.get("exercises") or []:
                rep_range = ex.get("rep_range") if isinstance(ex.get("rep_range"), dict) else None
                sets = ex.get("sets") or []
                rpe_list = [s.get("target_rpe") for s in sets if s.get("target_rpe") is not None]
                set_target_reps = [s.get("target_reps") for s in sets]
                load_targets = [s.get("load_target") for s in sets]
                set_notes = [s.get("notes") for s in sets]
                items.append(
                    {
                        "kind": "exercise",
                        "id": ex.get("exercise_id"),
                        "name": ex.get("name"),
                        "canonical_id": ex.get("canonical_id"),
                        "variation": ex.get("variation"),
                        "sets": max(len(sets), _int_or_none(ex.get("target_sets")) or len(sets)),
                        "reps": rep_range or ex.get("target_reps"),
                        "set_target_reps": set_target_reps,
                        "rpe_list": rpe_list,
                        "rir": ex.get("rir"),
                        "rest_seconds": ex.get("rest_seconds"),
                        "note": ex.get("notes"),
                        "notes": ex.get("notes"),
                        "progression_rule": ex.get("progression_rule"),
                        "progression": ex.get("progression") if isinstance(ex.get("progression"), dict) else None,
                        "caps": ex.get("caps") if isinstance(ex.get("caps"), dict) else None,
                        "load_targets": load_targets,
                        "set_notes": set_notes,
                    }
                )
            event_payload = {"id": workout.get("workout_id"), "kind": "gym", "title": workout.get("title"), "note": workout.get("notes"), "items": items}
            new_base_week[weekday].append(event_payload)
            day_events.append(copy.deepcopy(event_payload))
            new_sequence.append(copy.deepcopy(event_payload))
        new_days.append({"day": weekday, "label": day_titles[weekday], "title": day_titles[weekday], "events": day_events})
    plan["base_week"] = {"rotation": new_sequence} if rolling_mode else new_base_week
    plan["day_titles"] = day_titles
    if new_sequence:
        plan["sequence"] = new_sequence
    elif "sequence" in plan:
        plan.pop("sequence", None)
    if new_days:
        plan["days"] = new_days
    elif "days" in plan:
        plan.pop("days", None)
    if isinstance(mutable.get("periodization"), dict) or isinstance(mutable.get("caps"), dict):
        operator = dict((rules.get("liva_operator") if isinstance(rules, dict) else {}) or {})
        if isinstance(mutable.get("periodization"), dict):
            operator["periodization"] = mutable.get("periodization") or {}
        if isinstance(mutable.get("caps"), dict):
            operator["caps"] = mutable.get("caps") or {}
        rules["liva_operator"] = operator
    conn = connections.get_plans_db()
    try:
        now = _utcnow_iso()
        conn.execute(
            "UPDATE gym_plans SET title=?, plan_json=?, rules_json=?, blocks_json=?, updated_at=? WHERE id=?",
            (mutable["name"], json.dumps(plan, ensure_ascii=False), json.dumps(rules, ensure_ascii=False), json.dumps(blocks, ensure_ascii=False), now, plan_id),
        )
        for raw in operations:
            op = str((raw or {}).get("op") or "")
            if op == "set_active_plan":
                conn.execute("UPDATE gym_plans SET is_active=0 WHERE is_active=1")
                conn.execute("UPDATE gym_plans SET is_active=1, is_archived=0, updated_at=? WHERE id=?", (now, plan_id))
            elif op == "archive_plan":
                conn.execute("UPDATE gym_plans SET is_archived=1, is_active=0, updated_at=? WHERE id=?", (now, plan_id))
            elif op == "unarchive_plan":
                conn.execute("UPDATE gym_plans SET is_archived=0, updated_at=? WHERE id=?", (now, plan_id))
            elif op == "soft_delete_plan":
                conn.execute("UPDATE gym_plans SET is_archived=1, is_active=0, updated_at=? WHERE id=?", (now, plan_id))
            elif op == "duplicate_plan":
                title = str(raw.get("name") or f"{mutable['name']} Kopie")
                conn.execute(
                    "INSERT INTO gym_plans (title, focus, plan_json, is_active, is_archived, created_at, updated_at, rules_json, blocks_json) VALUES (?, ?, ?, 0, 0, ?, ?, ?, ?)",
                    (title, row.get("focus") or "", json.dumps(plan, ensure_ascii=False), now, now, row.get("rules_json") or "{}", row.get("blocks_json") or "{}"),
                )
        conn.commit()
    finally:
        conn.close()
