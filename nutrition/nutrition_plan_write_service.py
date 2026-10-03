from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from database.connections import get_nutrition_db
from nutrition.nutrition_planning_db import (
    _build_meal_signature,
    _build_week_plan_payload_wrapper,
    _insert_meal_ingredient,
    _refresh_meal_template_calcs,
    archive_week_template,
    create_week_day_group,
    detach_week_day_group_members,
    create_week_template,
    ensure_nutrition_planning_schema,
    get_active_week_plan_resolved,
    list_week_templates,
    set_active_week_template,
    update_week_template_title,
    update_week_day_group_amount,
    sync_week_day_group,
)

WEEKDAY_ALIASES = {
    "mo": "Mo",
    "montag": "Mo",
    "di": "Di",
    "dienstag": "Di",
    "mi": "Mi",
    "mittwoch": "Mi",
    "do": "Do",
    "donnerstag": "Do",
    "fr": "Fr",
    "freitag": "Fr",
    "sa": "Sa",
    "samstag": "Sa",
    "so": "So",
    "sonntag": "So",
}
WEEKDAY_ORDER = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]
WEEKDAY_INDEX = {label: idx for idx, label in enumerate(WEEKDAY_ORDER)}


def _utcnow_iso() -> str:
    return datetime.utcnow().isoformat()


def _normalize_weekday(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError("day is required")
    key = text.lower()
    if key in WEEKDAY_ALIASES:
        return WEEKDAY_ALIASES[key]
    short = key[:2]
    if short in WEEKDAY_ALIASES:
        return WEEKDAY_ALIASES[short]
    raise ValueError(f"invalid_day:{text}")


def _normalize_food_name(value: Any) -> str:
    return " ".join(str(value or "").strip().lower().split())


def _meal_time_sort_key(meal: dict[str, Any], original_index: int) -> tuple[int, int, int]:
    text = str(meal.get("time") or meal.get("time_text") or "").strip()
    try:
        hours_text, minutes_text = text.split(":", 1)
        hours = int(hours_text)
        minutes = int(minutes_text)
        if 0 <= hours <= 23 and 0 <= minutes <= 59:
            return (0, hours * 60 + minutes, original_index)
    except (TypeError, ValueError):
        pass
    return (1, 24 * 60, original_index)


def _sort_meals_chronologically(meals: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [meal for _, meal in sorted(enumerate(meals), key=lambda pair: _meal_time_sort_key(pair[1], pair[0]))]


def _food_candidates(cur, food_name: str) -> list[dict[str, Any]]:
    norm = _normalize_food_name(food_name)
    if not norm:
        return []
    rows = cur.execute(
        """
        SELECT id, name
        FROM nutrition_foods
        WHERE is_active=1 AND LOWER(TRIM(name)) = ?
        ORDER BY name ASC, id ASC
        """,
        (norm,),
    ).fetchall()
    if rows:
        return [{"food_id": int(row["id"]), "name": row["name"]} for row in rows]
    like_rows = cur.execute(
        """
        SELECT id, name
        FROM nutrition_foods
        WHERE is_active=1 AND LOWER(name) LIKE ?
        ORDER BY name ASC, id ASC
        LIMIT 8
        """,
        (f"%{norm}%",),
    ).fetchall()
    return [{"food_id": int(row["id"]), "name": row["name"]} for row in like_rows]


def _resolve_food_item(cur, item: dict[str, Any]) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    if item.get("food_id") not in (None, ""):
        try:
            food_id = int(item.get("food_id"))
        except Exception:
            return None, {"food_id": item.get("food_id"), "reason": "invalid_food_id", "candidates": []}
        row = cur.execute("SELECT id, name FROM nutrition_foods WHERE id=? AND is_active=1 LIMIT 1", (food_id,)).fetchone()
        if not row:
            return None, {"food_id": food_id, "reason": "not_found", "candidates": []}
        return {
            "food_id": int(row["id"]),
            "food_name": row["name"],
            "amount": float(item.get("amount") or 0),
            "unit": str(item.get("unit") or "g").strip() or "g",
        }, None
    food_name = str(item.get("food_name") or item.get("name") or "").strip()
    if not food_name:
        return None, {"food_name": "", "reason": "missing_food_name", "candidates": []}
    candidates = _food_candidates(cur, food_name)
    exact = [row for row in candidates if _normalize_food_name(row["name"]) == _normalize_food_name(food_name)]
    if len(exact) == 1:
        row = exact[0]
        return {
            "food_id": int(row["food_id"]),
            "food_name": row["name"],
            "amount": float(item.get("amount") or 0),
            "unit": str(item.get("unit") or "g").strip() or "g",
        }, None
    if len(exact) > 1:
        return None, {"food_name": food_name, "reason": "ambiguous", "candidates": exact[:8]}
    if candidates:
        return None, {"food_name": food_name, "reason": "ambiguous", "candidates": candidates[:8]}
    return None, {"food_name": food_name, "reason": "not_found", "candidates": []}


def _slot_template_id(cur, title: str, time_text: str | None) -> int:
    row = cur.execute("SELECT id FROM nutrition_slot_templates WHERE title=? LIMIT 1", (title,)).fetchone()
    now = _utcnow_iso()
    if row:
        return int(row["id"])
    cur.execute(
        """
        INSERT INTO nutrition_slot_templates (title, default_time, context_tag, macro_intent, created_at, updated_at)
        VALUES (?, ?, 'meal', NULL, ?, ?)
        """,
        (title, time_text, now, now),
    )
    return int(cur.lastrowid)


def _create_or_reuse_meal_template_in_tx(cur, title: str, ingredients: list[dict[str, Any]]) -> dict[str, Any]:
    signature = _build_meal_signature(title, ingredients)
    existing = cur.execute("SELECT * FROM nutrition_meal_templates WHERE signature=? LIMIT 1", (signature,)).fetchone()
    if existing:
        return dict(existing)
    now = _utcnow_iso()
    cur.execute(
        """
        INSERT INTO nutrition_meal_templates (title, description, tags, default_servings, mfp_alias, notes, signature, created_at, updated_at)
        VALUES (?, NULL, NULL, 1.0, NULL, ?, ?, ?, ?)
        """,
        (title, "actions_v2.create_nutrition_plan", signature, now, now),
    )
    meal_id = int(cur.lastrowid)
    for idx, ingredient in enumerate(ingredients):
        _insert_meal_ingredient(cur, meal_id, ingredient, idx)
    _refresh_meal_template_calcs(cur, meal_id)
    row = cur.execute("SELECT * FROM nutrition_meal_templates WHERE id=?", (meal_id,)).fetchone()
    return dict(row)


def _compact_created_template(payload: dict[str, Any], *, meals_count: int, slots_count: int) -> dict[str, Any]:
    week = payload.get("week_template") if isinstance(payload.get("week_template"), dict) else {}
    out_days: list[dict[str, Any]] = []
    filled_days_count = 0
    for day in week.get("days") or []:
        if not isinstance(day, dict):
            continue
        compact_slots: list[dict[str, Any]] = []
        for slot in day.get("slots") or []:
            if not isinstance(slot, dict):
                continue
            items = []
            for item in slot.get("items") or []:
                if not isinstance(item, dict):
                    continue
                items.append({"food_id": item.get("food_id"), "name": item.get("name"), "amount": item.get("amount"), "unit": item.get("unit")})
            meal_template = slot.get("meal_template") if isinstance(slot.get("meal_template"), dict) else {}
            compact_slots.append(
                {
                    "id": slot.get("id"),
                    "slot_index": slot.get("slot_index"),
                    "time_text": slot.get("time_text"),
                    "custom_title": slot.get("custom_title"),
                    "meal_template_id": slot.get("meal_template_id"),
                    "meal_title": slot.get("custom_title") or slot.get("display_title") or slot.get("meal_title") or meal_template.get("title"),
                    "items": items,
                    "macros": {
                        "kcal": meal_template.get("kcal_per_serving"),
                        "p": meal_template.get("p_per_serving"),
                        "c": meal_template.get("c_per_serving"),
                        "f": meal_template.get("f_per_serving"),
                    },
                }
            )
        if compact_slots:
            filled_days_count += 1
        out_days.append({"weekday": day.get("weekday"), "label": day.get("label"), "slots": compact_slots})
    return {
        "id": week.get("id"),
        "title": week.get("title"),
        "is_active": bool(week.get("is_active")),
        "days": out_days,
        "summary": {"days_count": len(out_days), "filled_days_count": filled_days_count, "slots_count": slots_count, "meals_count": meals_count},
    }


def _readback_template(template_id: int, *, meals_count: int, slots_count: int) -> dict[str, Any]:
    return _compact_created_template(_build_week_plan_payload_wrapper(template_id), meals_count=meals_count, slots_count=slots_count)


def _template_row(template_id: int) -> dict[str, Any] | None:
    conn = get_nutrition_db()
    conn.row_factory = conn.row_factory
    try:
        row = conn.execute("SELECT * FROM nutrition_week_templates WHERE id=?", (template_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _normalize_create_plan_source(body: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    value = body.get("value") if isinstance(body.get("value"), dict) else {}
    payload = body.get("payload") if isinstance(body.get("payload"), dict) else {}
    week_template = body.get("week_template") if isinstance(body.get("week_template"), dict) else {}
    normalized: dict[str, Any] = {}
    for source in (payload, value, week_template):
        for key, value_item in source.items():
            if key not in normalized:
                normalized[key] = value_item
    for key, value_item in body.items():
        if key in {"value", "payload", "week_template"}:
            continue
        if value_item is not None:
            normalized[key] = value_item
    for key in ("name", "title", "set_active", "allow_partial", "target_kcal", "target_p", "target_c", "target_f", "macro_targets", "notes", "reason", "source_message", "raw_text"):
        if body.get(key) is not None:
            normalized[key] = body.get(key)
    received_keys = sorted({str(key) for source in [body, value, payload, week_template] for key in source.keys()})
    return normalized, received_keys


def _normalize_structured_nutrition_plan(body: dict[str, Any]) -> dict[str, Any]:
    ensure_nutrition_planning_schema()
    normalized_source, received_keys = _normalize_create_plan_source(body)
    if not isinstance(normalized_source, dict):
        raise ValueError("nutrition plan payload must be an object")
    title = str(normalized_source.get("name") or normalized_source.get("title") or "").strip()
    if not title:
        raise ValueError("name is required")
    days_input = normalized_source.get("days")
    if not isinstance(days_input, list) or not days_input:
        raise ValueError("missing_days")
    set_active = bool(normalized_source.get("set_active", True))
    allow_partial = bool(normalized_source.get("allow_partial", False))
    macro_targets = normalized_source.get("macro_targets") if isinstance(normalized_source.get("macro_targets"), dict) else {}
    target_kcal = normalized_source.get("target_kcal")
    target_p = normalized_source.get("target_p")
    target_c = normalized_source.get("target_c")
    target_f = normalized_source.get("target_f")
    if any(value is not None for value in (target_kcal, target_p, target_c, target_f)):
        macro_targets = {
            **macro_targets,
            "target_kcal": target_kcal if target_kcal is not None else macro_targets.get("target_kcal"),
            "target_p": target_p if target_p is not None else macro_targets.get("target_p"),
            "target_c": target_c if target_c is not None else macro_targets.get("target_c"),
            "target_f": target_f if target_f is not None else macro_targets.get("target_f"),
        }
    unresolved_items: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    normalized_days: list[dict[str, Any]] = []
    conn = get_nutrition_db()
    try:
        cur = conn.cursor()
        for day_entry in days_input:
            if not isinstance(day_entry, dict):
                raise ValueError("days entries must be objects")
            weekday = _normalize_weekday(day_entry.get("day"))
            meals = day_entry.get("meals") if isinstance(day_entry.get("meals"), list) else []
            normalized_meals: list[dict[str, Any]] = []
            for meal in meals:
                if not isinstance(meal, dict):
                    raise ValueError(f"meal entries for {weekday} must be objects")
                title_text = str(meal.get("title") or meal.get("name") or "Meal").strip() or "Meal"
                items = meal.get("items")
                if not isinstance(items, list) or not items:
                    raise ValueError(f"meal {title_text} for {weekday} requires items")
                resolved_ingredients: list[dict[str, Any]] = []
                meal_unresolved: list[dict[str, Any]] = []
                for item in items:
                    if not isinstance(item, dict):
                        raise ValueError(f"items for meal {title_text} must be objects")
                    resolved, unresolved = _resolve_food_item(cur, item)
                    if unresolved:
                        meal_unresolved.append(unresolved)
                    elif resolved:
                        resolved_ingredients.append(resolved)
                if meal_unresolved:
                    unresolved_items.extend(meal_unresolved)
                    if not allow_partial:
                        continue
                    warnings.append({"code": "partial_meal_items", "meal": title_text, "day": weekday, "count": len(meal_unresolved)})
                if not resolved_ingredients:
                    if allow_partial:
                        warnings.append({"code": "meal_skipped_no_resolved_items", "meal": title_text, "day": weekday})
                        continue
                    continue
                normalized_meals.append({"title": title_text, "time": str(meal.get("time") or "").strip() or None, "items": resolved_ingredients})
            normalized_days.append({"day": weekday, "meals": _sort_meals_chronologically(normalized_meals)})
    finally:
        conn.close()
    return {
        "title": title,
        "name": title,
        "set_active": set_active,
        "allow_partial": allow_partial,
        "macro_targets": macro_targets,
        "target_kcal": target_kcal,
        "target_p": target_p,
        "target_c": target_c,
        "target_f": target_f,
        "unresolved_items": unresolved_items,
        "warnings": warnings,
        "normalized_days": normalized_days,
        "rules": normalized_source.get("rules") if isinstance(normalized_source.get("rules"), dict) else {},
        "notes": str(normalized_source.get("notes") or "").strip() or None,
        "source_message": str(normalized_source.get("source_message") or "").strip() or None,
        "reason": str(normalized_source.get("reason") or "").strip() or None,
        "raw_text": str(normalized_source.get("raw_text") or "").strip() or None,
        "received_keys": received_keys,
    }


def _apply_macro_targets(template_id: int, macro_targets: dict[str, Any]) -> None:
    if not macro_targets:
        return
    conn = get_nutrition_db()
    try:
        row = conn.execute("SELECT macro_modes_json FROM nutrition_week_templates WHERE id=?", (template_id,)).fetchone()
        if not row:
            raise ValueError("template_not_found")
        try:
            existing = json.loads(row["macro_modes_json"] or "{}")
        except Exception:
            existing = {}
        modes = existing.get("modes") if isinstance(existing.get("modes"), dict) else {}
        custom_mode = dict(modes.get("custom") or {})
        custom_mode.update(
            {
                "kcal_target": macro_targets.get("kcal", macro_targets.get("target_kcal")),
                "protein_target": macro_targets.get("p", macro_targets.get("target_p")),
                "carbs_target": macro_targets.get("c", macro_targets.get("target_c")),
                "fat_target": macro_targets.get("f", macro_targets.get("target_f")),
            }
        )
        modes["custom"] = custom_mode
        payload = {"active_mode": "custom", "modes": modes}
        conn.execute(
            """
            UPDATE nutrition_week_templates
            SET macro_active_mode=?, macro_modes_json=?, updated_at=?
            WHERE id=?
            """,
            ("custom", json.dumps(payload, ensure_ascii=False), _utcnow_iso(), template_id),
        )
        conn.commit()
    finally:
        conn.close()


def _apply_day_macro_targets(template_id: int, macro_targets: dict[str, Any]) -> None:
    if not macro_targets:
        return
    target_kcal = macro_targets.get("target_kcal", macro_targets.get("kcal"))
    target_p = macro_targets.get("target_p", macro_targets.get("p"))
    target_c = macro_targets.get("target_c", macro_targets.get("c"))
    target_f = macro_targets.get("target_f", macro_targets.get("f"))
    conn = get_nutrition_db()
    try:
        conn.execute(
            """
            UPDATE nutrition_week_template_days
            SET target_kcal=?, target_p=?, target_c=?, target_f=?, updated_at=?
            WHERE week_template_id=?
            """,
            (target_kcal, target_p, target_c, target_f, _utcnow_iso(), template_id),
        )
        conn.commit()
    finally:
        conn.close()


def _write_template_days(template_id: int, normalized: dict[str, Any], *, clear_existing: bool = True, title_override: str | None = None, set_active: bool | None = None) -> dict[str, Any]:
    conn = get_nutrition_db()
    conn.row_factory = conn.row_factory
    warnings = list(normalized.get("warnings") or [])
    resource_touches = {"nutrition.nutrition_week_templates", "nutrition.nutrition_week_template_days"}
    slots_written = 0
    meals_written = 0
    written_days: set[str] = set()
    try:
        cur = conn.cursor()
        row = cur.execute("SELECT * FROM nutrition_week_templates WHERE id=?", (template_id,)).fetchone()
        if not row:
            raise ValueError("template_not_found")
        now = _utcnow_iso()
        if title_override:
            cur.execute("UPDATE nutrition_week_templates SET title=?, updated_at=? WHERE id=?", (title_override, now, template_id))
        day_rows = cur.execute("SELECT id, weekday FROM nutrition_week_template_days WHERE week_template_id=?", (template_id,)).fetchall()
        day_map = {WEEKDAY_ORDER[int(day_row["weekday"])]: int(day_row["id"]) for day_row in day_rows}
        if clear_existing:
            cur.execute("DELETE FROM nutrition_week_day_slots WHERE week_template_day_id IN (SELECT id FROM nutrition_week_template_days WHERE week_template_id=?)", (template_id,))
        for day_entry in normalized["normalized_days"]:
            day_id = day_map.get(day_entry["day"])
            if not day_id:
                warnings.append({"code": "day_not_found_for_template", "day": day_entry["day"]})
                continue
            day_entry["meals"] = _sort_meals_chronologically(list(day_entry.get("meals") or []))
            for idx, meal in enumerate(day_entry["meals"]):
                meal_template = _create_or_reuse_meal_template_in_tx(cur, meal["title"], meal["items"])
                slot_template_id = _slot_template_id(cur, meal["title"], meal.get("time"))
                cur.execute(
                    """
                    INSERT INTO nutrition_week_day_slots
                        (week_template_day_id, slot_template_id, slot_index, active_option_id, locked, time_text, custom_title, note_text, meal_template_id, created_at, updated_at)
                    VALUES (?, ?, ?, NULL, 0, ?, ?, ?, ?, ?, ?)
                    """,
                    (day_id, slot_template_id, idx, meal.get("time"), meal["title"], None, int(meal_template["id"]), now, now),
                )
                slots_written += 1
                meals_written += 1
                written_days.add(day_entry["day"])
                resource_touches.update({"nutrition.nutrition_week_day_slots", "nutrition.nutrition_meal_templates", "nutrition.nutrition_meal_ingredients"})
        cur.execute("UPDATE nutrition_week_templates SET revision=COALESCE(revision, 0)+1, updated_at=? WHERE id=?", (now, template_id))
        conn.commit()
    finally:
        conn.close()
    if normalized.get("macro_targets"):
        _apply_macro_targets(template_id, normalized["macro_targets"])
        _apply_day_macro_targets(template_id, normalized["macro_targets"])
    if set_active:
        set_active_week_template(template_id)
    elif set_active is False:
        conn = get_nutrition_db()
        try:
            conn.execute("UPDATE nutrition_week_templates SET is_active=0, updated_at=? WHERE id=?", (_utcnow_iso(), template_id))
            conn.commit()
        finally:
            conn.close()
    # The source weekday of every linked group is canonical. Any generic plan
    # write is projected afterwards so old clients and GPT patches cannot leave
    # a group in a half-updated state.
    conn = get_nutrition_db()
    try:
        group_rows = conn.execute("SELECT id FROM nutrition_week_day_groups WHERE week_template_id=?", (template_id,)).fetchall()
    finally:
        conn.close()
    for group_row in group_rows:
        _, sync_error = sync_week_day_group(template_id, str(group_row["id"]))
        if sync_error:
            warnings.append({"code": "day_group_sync_failed", "group_id": str(group_row["id"]), "reason": sync_error})
    input_meals_count = sum(len(day["meals"]) for day in normalized["normalized_days"])
    if input_meals_count > 0 and slots_written == 0:
        raise ValueError("no meals were written to nutrition_week_day_slots")
    return {
        "template_id": template_id,
        "days_count": len(normalized["normalized_days"]),
        "meals_count": meals_written,
        "slots_count": slots_written,
        "warnings": warnings,
        "unresolved_items": normalized.get("unresolved_items") or [],
        "summary": {"days_count": len(normalized["normalized_days"]), "meals_count": meals_written, "slots_count": slots_written},
        "created_template_after": _readback_template(template_id, meals_count=meals_written, slots_count=slots_written),
        "days_written": sorted(written_days, key=lambda item: WEEKDAY_INDEX.get(item, 99)),
        "_affected_resources": sorted(resource_touches),
    }


def create_structured_nutrition_plan(body: dict[str, Any], *, dry_run: bool = False) -> dict[str, Any]:
    try:
        normalized = _normalize_structured_nutrition_plan(body)
    except ValueError as exc:
        code = str(exc)
        normalized_source, received_keys = _normalize_create_plan_source(body)
        raw_text = str(normalized_source.get("raw_text") or "").strip()
        if code == "missing_days":
            error_code = "structured_days_required" if raw_text and not dry_run else "missing_days"
            return {
                "ok": False,
                "error": {
                    "code": error_code,
                    "message": "create_nutrition_plan requires days as top-level days, value.days, or payload.days",
                    "accepted_shapes": ["days", "value.days", "payload.days"],
                },
                "received_keys": received_keys,
            }
        raise
    if dry_run:
        normalized_meals_count = sum(len(day["meals"]) for day in normalized["normalized_days"])
        normalized_plan = {
            "name": normalized["name"],
            "target_kcal": normalized.get("target_kcal"),
            "target_p": normalized.get("target_p"),
            "target_c": normalized.get("target_c"),
            "target_f": normalized.get("target_f"),
            "set_active": normalized.get("set_active"),
            "days": normalized["normalized_days"],
            "rules": normalized.get("rules") or {},
            "notes": normalized.get("notes"),
            "source_message": normalized.get("source_message"),
            "reason": normalized.get("reason"),
            "days_count": len(normalized["normalized_days"]),
            "meal_count": normalized_meals_count,
        }
        return {
            "ok": True,
            "template_id": None,
            "days_count": len(normalized["normalized_days"]),
            "meals_count": normalized_meals_count,
            "slots_count": normalized_meals_count,
            "warnings": normalized["warnings"],
            "unresolved_items": normalized["unresolved_items"],
            "summary": {"days_count": len(normalized["normalized_days"]), "meals_count": normalized_meals_count, "slots_count": normalized_meals_count},
            "normalized_days": normalized["normalized_days"],
            "normalized_plan": normalized_plan,
        }
    if normalized["unresolved_items"] and not normalized["allow_partial"]:
        return {
            "ok": False,
            "template_id": None,
            "days_count": len(normalized["normalized_days"]),
            "meals_count": 0,
            "slots_count": 0,
            "warnings": normalized["warnings"],
            "unresolved_items": normalized["unresolved_items"],
            "summary": {"days_count": len(normalized["normalized_days"]), "meals_count": 0, "slots_count": 0},
        }
    week, err = create_week_template(normalized["title"], source_template_id=None, set_active=normalized["set_active"])
    if err or not week:
        raise ValueError(err or "create_week_template_failed")
    result = _write_template_days(int(week["id"]), normalized, clear_existing=True, title_override=normalized["title"], set_active=normalized["set_active"])
    active_plan_after = get_active_week_plan_resolved() if normalized["set_active"] else None
    return {
        "ok": True,
        **result,
        **({"active_plan_after": {"week_template": active_plan_after.get("week_template"), "days": active_plan_after.get("days") if isinstance(active_plan_after.get("days"), list) else []}} if active_plan_after is not None else {}),
    }


def list_compact_nutrition_plans(include_archived: bool = True) -> list[dict[str, Any]]:
    ensure_nutrition_planning_schema()
    conn = get_nutrition_db()
    conn.row_factory = conn.row_factory
    summary_by_template: dict[int, dict[str, Any]] = {}
    try:
        cur = conn.cursor()
        day_rows = cur.execute("SELECT id, week_template_id FROM nutrition_week_template_days").fetchall()
        day_ids_by_template: dict[int, list[int]] = {}
        for row in day_rows:
            day_ids_by_template.setdefault(int(row["week_template_id"]), []).append(int(row["id"]))
        for template_id, day_ids in day_ids_by_template.items():
            placeholders = ",".join("?" for _ in day_ids)
            days_count = len(day_ids)
            slots_count = int(
                cur.execute(
                    f"SELECT COUNT(*) AS c FROM nutrition_week_day_slots WHERE week_template_day_id IN ({placeholders})",
                    tuple(day_ids),
                ).fetchone()["c"]
                or 0
            )
            legacy_meals = cur.execute(
                f"SELECT id, week_template_day_id, slot_index FROM nutrition_slot_meals WHERE week_template_day_id IN ({placeholders})",
                tuple(day_ids),
            ).fetchall()
            legacy_slots = {(int(row["week_template_day_id"]), int(row["slot_index"])) for row in legacy_meals}
            legacy_days = {int(row["week_template_day_id"]) for row in legacy_meals}
            legacy_slot_item_counts: dict[tuple[int, int], int] = {}
            if legacy_meals:
                legacy_meal_ids = [int(row["id"]) for row in legacy_meals]
                meal_placeholders = ",".join("?" for _ in legacy_meal_ids)
                legacy_item_rows = cur.execute(
                    f"""
                    SELECT sm.week_template_day_id, sm.slot_index, COUNT(smi.id) AS item_count
                    FROM nutrition_slot_meals sm
                    LEFT JOIN nutrition_slot_meal_items smi ON smi.slot_meal_id = sm.id
                    WHERE sm.id IN ({meal_placeholders})
                    GROUP BY sm.week_template_day_id, sm.slot_index
                    """,
                    tuple(legacy_meal_ids),
                ).fetchall()
                legacy_slot_item_counts = {
                    (int(row["week_template_day_id"]), int(row["slot_index"])): int(row["item_count"] or 0)
                    for row in legacy_item_rows
                }
            new_style_rows = cur.execute(
                f"""
                SELECT week_template_day_id, slot_index, meal_template_id
                FROM nutrition_week_day_slots
                WHERE week_template_day_id IN ({placeholders}) AND meal_template_id IS NOT NULL
                """,
                tuple(day_ids),
            ).fetchall()
            new_style_slots = {(int(row["week_template_day_id"]), int(row["slot_index"])) for row in new_style_rows}
            new_style_days = {int(row["week_template_day_id"]) for row in new_style_rows}
            new_style_slot_item_counts: dict[tuple[int, int], int] = {}
            if new_style_rows:
                new_style_meal_ids = sorted({int(row["meal_template_id"]) for row in new_style_rows if row["meal_template_id"] is not None})
                meal_template_placeholders = ",".join("?" for _ in new_style_meal_ids)
                ingredient_rows = cur.execute(
                    f"""
                    SELECT meal_template_id, COUNT(*) AS item_count
                    FROM nutrition_meal_ingredients
                    WHERE meal_template_id IN ({meal_template_placeholders})
                    GROUP BY meal_template_id
                    """,
                    tuple(new_style_meal_ids),
                ).fetchall()
                ingredient_count_by_template = {int(row["meal_template_id"]): int(row["item_count"] or 0) for row in ingredient_rows}
                new_style_slot_item_counts = {
                    (int(row["week_template_day_id"]), int(row["slot_index"])): int(ingredient_count_by_template.get(int(row["meal_template_id"]), 0))
                    for row in new_style_rows
                    if row["meal_template_id"] is not None
                }
            filled_slots = legacy_slots | new_style_slots
            filled_days = legacy_days | new_style_days
            items_count = 0
            for slot_key in filled_slots:
                if slot_key in legacy_slot_item_counts:
                    items_count += legacy_slot_item_counts[slot_key]
                else:
                    items_count += new_style_slot_item_counts.get(slot_key, 0)
            summary_by_template[template_id] = {
                "days_count": days_count,
                "slots_count": slots_count,
                "filled_slots_count": len(filled_slots),
                "filled_days_count": len(filled_days),
                "meals_count": len(filled_slots),
                "items_count": items_count,
            }
    finally:
        conn.close()
    out = []
    for row in list_week_templates(include_archived=include_archived):
        template_id = int(row["id"])
        out.append(
            {
                "id": template_id,
                "title": row.get("title"),
                "is_active": bool(row.get("is_active")),
                "is_archived": row.get("archived_at") is not None,
                "updated_at": row.get("updated_at"),
                "summary": summary_by_template.get(template_id, {"days_count": 0, "slots_count": 0, "filled_slots_count": 0, "filled_days_count": 0, "meals_count": 0, "items_count": 0}),
            }
        )
    return out


def activate_nutrition_plan(template_id: int, *, dry_run: bool = False) -> dict[str, Any]:
    target = _template_row(template_id)
    if not target:
        raise ValueError("template_not_found")
    if dry_run:
        active = next((item for item in list_week_templates(include_archived=True) if item.get("is_active")), None)
        return {
            "template_id": template_id,
            "previous_active_template": active,
            "target_template": {"id": target["id"], "title": target["title"], "is_active": bool(target["is_active"])},
            "will_change": not bool(target["is_active"]),
        }
    updated, err = set_active_week_template(template_id)
    if err or not updated:
        raise ValueError(err or "activate_template_failed")
    return {
        "template_id": template_id,
        "activated": True,
        "active_template_after": _readback_template(template_id, meals_count=0, slots_count=0),
        "_affected_resources": ["nutrition.nutrition_week_templates"],
    }


def archive_nutrition_plan(template_id: int, *, dry_run: bool = False) -> dict[str, Any]:
    row = _template_row(template_id)
    if not row:
        raise ValueError("template_not_found")
    if dry_run:
        return {
            "template_id": template_id,
            "template": {"id": row["id"], "title": row["title"], "is_active": bool(row["is_active"]), "archived_at": row.get("archived_at")},
            "will_change": row.get("archived_at") is None,
        }
    updated, err = archive_week_template(template_id)
    if err or not updated:
        raise ValueError(err or "archive_template_failed")
    return {
        "template_id": template_id,
        "archived": True,
        "template_after": {"id": updated["id"], "title": updated["title"], "is_active": bool(updated["is_active"]), "archived_at": updated.get("archived_at")},
        "_affected_resources": ["nutrition.nutrition_week_templates"],
    }


def delete_nutrition_plan(template_id: int, *, hard_delete: bool = False, allow_delete_active: bool = False, delete_orphan_meals: bool = False, dry_run: bool = False) -> dict[str, Any]:
    if not hard_delete:
        raise ValueError("hard_delete_required")
    conn = get_nutrition_db()
    conn.row_factory = conn.row_factory
    try:
        cur = conn.cursor()
        row = cur.execute("SELECT * FROM nutrition_week_templates WHERE id=?", (template_id,)).fetchone()
        if not row:
            raise ValueError("template_not_found")
        if row["is_active"] and not allow_delete_active:
            raise ValueError("cannot_delete_active_template")
        day_rows = cur.execute("SELECT id FROM nutrition_week_template_days WHERE week_template_id=?", (template_id,)).fetchall()
        day_ids = [int(item["id"]) for item in day_rows]
        slot_rows = cur.execute(
            f"SELECT id, meal_template_id FROM nutrition_week_day_slots WHERE week_template_day_id IN ({','.join('?' for _ in day_ids)})" if day_ids else "SELECT id, meal_template_id FROM nutrition_week_day_slots WHERE 1=0",
            tuple(day_ids),
        ).fetchall()
        slot_ids = [int(item["id"]) for item in slot_rows]
        meal_ids = [int(item["meal_template_id"]) for item in slot_rows if item["meal_template_id"] is not None]
        if dry_run:
            return {"template_id": template_id, "template": {"id": row["id"], "title": row["title"], "is_active": bool(row["is_active"])}, "will_delete": True, "summary": {"days_deleted": len(day_ids), "slots_deleted": len(slot_ids), "meal_templates_considered": len(set(meal_ids))}}
        meal_templates_deleted = 0
        ingredients_deleted = 0
        if delete_orphan_meals:
            for meal_id in sorted(set(meal_ids)):
                refs = cur.execute("SELECT COUNT(*) AS c FROM nutrition_week_day_slots WHERE meal_template_id=?", (meal_id,)).fetchone()
                meal_row = cur.execute("SELECT notes FROM nutrition_meal_templates WHERE id=?", (meal_id,)).fetchone()
                scoped_ref_count = sum(1 for item in meal_ids if item == meal_id)
                if int(refs["c"] or 0) == scoped_ref_count and meal_row and str(meal_row["notes"] or "") == "actions_v2.create_nutrition_plan":
                    ing_deleted = cur.execute("DELETE FROM nutrition_meal_ingredients WHERE meal_template_id=?", (meal_id,))
                    ingredients_deleted += int(ing_deleted.rowcount or 0)
                    meal_deleted = cur.execute("DELETE FROM nutrition_meal_templates WHERE id=?", (meal_id,))
                    meal_templates_deleted += int(meal_deleted.rowcount or 0)
        slots_deleted = int(cur.execute("DELETE FROM nutrition_week_day_slots WHERE week_template_day_id IN (SELECT id FROM nutrition_week_template_days WHERE week_template_id=?)", (template_id,)).rowcount or 0)
        days_deleted = int(cur.execute("DELETE FROM nutrition_week_template_days WHERE week_template_id=?", (template_id,)).rowcount or 0)
        week_templates_deleted = int(cur.execute("DELETE FROM nutrition_week_templates WHERE id=?", (template_id,)).rowcount or 0)
        conn.commit()
        return {
            "template_id": template_id,
            "deleted": True,
            "week_templates_deleted": week_templates_deleted,
            "days_deleted": days_deleted,
            "slots_deleted": slots_deleted,
            "meal_templates_deleted": meal_templates_deleted,
            "ingredients_deleted": ingredients_deleted,
            "_affected_resources": ["nutrition.nutrition_week_templates", "nutrition.nutrition_week_template_days", "nutrition.nutrition_week_day_slots"],
        }
    finally:
        conn.close()


def replace_nutrition_plan(template_id: int, body: dict[str, Any], *, dry_run: bool = False) -> dict[str, Any]:
    target = _template_row(template_id)
    if not target:
        raise ValueError("template_not_found")
    source = body.get("replace_with") if isinstance(body.get("replace_with"), dict) else body
    normalized = _normalize_structured_nutrition_plan(source)
    if normalized["unresolved_items"] and not normalized["allow_partial"]:
        return {"ok": False, "template_id": template_id, "warnings": normalized["warnings"], "unresolved_items": normalized["unresolved_items"]}
    if dry_run:
        return {
            "template_id": template_id,
            "warnings": normalized["warnings"],
            "unresolved_items": normalized["unresolved_items"],
            "replacement_preview": {
                "title": normalized["title"],
                "days_count": len(normalized["normalized_days"]),
                "meals_count": sum(len(day["meals"]) for day in normalized["normalized_days"]),
                "was_active": bool(target["is_active"]),
            },
        }
    set_active = body.get("set_active") if body.get("set_active") is not None else None
    result = _write_template_days(template_id, normalized, clear_existing=True, title_override=normalized["title"], set_active=set_active)
    return {"ok": True, **result}


def patch_nutrition_plan(template_id: int, operations: list[dict[str, Any]], *, allow_partial: bool = False, dry_run: bool = False, expected_revision: int | None = None) -> dict[str, Any]:
    if not isinstance(operations, list) or not operations:
        raise ValueError("operations are required")
    target = _template_row(template_id)
    if not target:
        raise ValueError("template_not_found")
    if expected_revision is not None and int(target.get("revision") or 1) != int(expected_revision):
        return {"ok": False, "template_id": template_id, "changed": False, "error": "stale_plan_revision", "expected_revision": int(expected_revision), "current_revision": int(target.get("revision") or 1)}
    group_ops = {"link_weekdays_to_day_group", "detach_weekdays_from_day_group", "update_day_group_item_amount", "sync_day_group"}
    if any(str(op.get("op") or "") in group_ops for op in operations if isinstance(op, dict)):
        if not all(isinstance(op, dict) and str(op.get("op") or "") in group_ops for op in operations):
            return {"ok": False, "template_id": template_id, "changed": False, "operations_failed": [{"reason": "day_group_operations_must_be_sent_separately"}]}
        preview = []
        for op in operations:
            kind = str(op.get("op"))
            try:
                if kind == "link_weekdays_to_day_group":
                    weekdays = [WEEKDAY_INDEX[_normalize_weekday(day)] for day in (op.get("weekdays") or [])]
                    source = WEEKDAY_INDEX[_normalize_weekday(op.get("source_weekday") or op.get("source_day"))]
                    if len(set(weekdays)) < 2 or source not in weekdays:
                        raise ValueError("select_at_least_two_days_including_source")
                    preview.append({"op": kind, "weekdays": weekdays, "source_weekday": source, "name": str(op.get("name") or "Gemeinsamer Tag")})
                elif kind == "detach_weekdays_from_day_group":
                    group_id = str(op.get("day_group_id") or op.get("group_id") or "").strip()
                    weekdays = [WEEKDAY_INDEX[_normalize_weekday(day)] for day in (op.get("weekdays") or [])]
                    if not group_id or not weekdays: raise ValueError("group_id_and_weekdays_required")
                    preview.append({"op": kind, "group_id": group_id, "weekdays": weekdays})
                elif kind == "sync_day_group":
                    group_id = str(op.get("day_group_id") or op.get("group_id") or "").strip()
                    if not group_id: raise ValueError("group_id_required")
                    preview.append({"op": kind, "group_id": group_id})
                else:
                    group_id = str(op.get("day_group_id") or op.get("group_id") or "").strip()
                    match = op.get("match") if isinstance(op.get("match"), dict) else {}
                    food_id = op.get("food_id") if op.get("food_id") is not None else match.get("food_id")
                    if not group_id or food_id is None or op.get("amount") is None: raise ValueError("group_id_food_id_and_amount_required")
                    preview.append({"op": kind, "group_id": group_id, "food_id": int(food_id), "amount": float(op.get("amount")), "unit": str(op.get("unit") or "g")})
            except (ValueError, TypeError) as exc:
                return {"ok": False, "template_id": template_id, "changed": False, "operations_failed": [{"op": kind, "reason": str(exc)}]}
        if dry_run:
            return {"ok": True, "template_id": template_id, "changed": bool(preview), "group_operation_preview": preview, "requires_confirmation": any(item["op"] != "update_day_group_item_amount" for item in preview)}
        results = []
        for op in operations:
            kind = str(op.get("op"))
            if kind == "link_weekdays_to_day_group":
                weekdays = [WEEKDAY_INDEX[_normalize_weekday(day)] for day in op.get("weekdays")]
                source = WEEKDAY_INDEX[_normalize_weekday(op.get("source_weekday") or op.get("source_day"))]
                result, err = create_week_day_group(template_id, weekdays, source, str(op.get("name") or "Gemeinsamer Tag"), op.get("color"))
            elif kind == "detach_weekdays_from_day_group":
                result, err = detach_week_day_group_members(template_id, str(op.get("day_group_id") or op.get("group_id")), [WEEKDAY_INDEX[_normalize_weekday(day)] for day in op.get("weekdays")])
            elif kind == "sync_day_group":
                result, err = sync_week_day_group(template_id, str(op.get("day_group_id") or op.get("group_id")))
            else:
                match = op.get("match") if isinstance(op.get("match"), dict) else {}
                result, err = update_week_day_group_amount(template_id, str(op.get("day_group_id") or op.get("group_id")), slot_index=op.get("slot_index", match.get("slot_index")), time_text=op.get("time_text", match.get("time_text")), food_id=int(op.get("food_id", match.get("food_id"))), amount=float(op.get("amount")), unit=str(op.get("unit") or "g"))
            if err: return {"ok": False, "template_id": template_id, "changed": False, "operations_failed": [{"op": kind, "reason": err}]}
            results.append({"op": kind, "result": result})
        return {"ok": True, "template_id": template_id, "changed": bool(results), "operations_applied": len(results), "group_results": results, "updated_plan_after": _build_week_plan_payload_wrapper(template_id)}
    base = _build_week_plan_payload_wrapper(template_id)
    week = base.get("week_template") if isinstance(base.get("week_template"), dict) else {}
    normalized_days = []
    for day in week.get("days") or []:
        if not isinstance(day, dict):
            continue
        meals = []
        for slot in day.get("slots") or []:
            if not isinstance(slot, dict):
                continue
            items = []
            for item in slot.get("items") or []:
                if not isinstance(item, dict):
                    continue
                items.append({"food_id": item.get("food_id"), "amount": item.get("amount"), "unit": item.get("unit")})
            meals.append({"time": slot.get("time_text"), "title": slot.get("custom_title") or slot.get("meal_title") or "Meal", "items": items})
        normalized_days.append({"day": day.get("label"), "meals": meals})
    mutable = {"name": week.get("title") or target["title"], "days": normalized_days, "set_active": bool(target["is_active"])}
    warnings: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    applied = 0
    def find_day(day_label: Any) -> dict[str, Any]:
        label = _normalize_weekday(day_label)
        for entry in mutable["days"]:
            if entry["day"] == label:
                return entry
        raise ValueError("day_not_found")
    for op in operations:
        if not isinstance(op, dict):
            failed.append({"op": None, "reason": "invalid_operation"})
            if not allow_partial:
                break
            continue
        kind = str(op.get("op") or "").strip()
        try:
            if kind == "rename_template":
                mutable["name"] = str(op.get("title") or op.get("name") or "").strip() or mutable["name"]
            elif kind == "replace_day_meals":
                day = find_day(op.get("day"))
                meals = op.get("meals")
                if not isinstance(meals, list):
                    raise ValueError("meals_required")
                day["meals"] = meals
            elif kind == "add_meal_to_day":
                day = find_day(op.get("day"))
                meal = op.get("meal")
                if not isinstance(meal, dict):
                    raise ValueError("meal_required")
                day["meals"].append(meal)
            elif kind == "remove_meal_from_day":
                day = find_day(op.get("day"))
                time_text = str(op.get("time") or "").strip()
                before = len(day["meals"])
                day["meals"] = [meal for meal in day["meals"] if str(meal.get("time") or "").strip() != time_text]
                if len(day["meals"]) == before:
                    raise ValueError("meal_not_found")
            elif kind == "replace_meal_slot":
                day = find_day(op.get("day"))
                time_text = str(op.get("time") or "").strip()
                meal = op.get("meal")
                if not isinstance(meal, dict):
                    raise ValueError("meal_required")
                matched = False
                for idx, existing in enumerate(day["meals"]):
                    if str(existing.get("time") or "").strip() == time_text:
                        day["meals"][idx] = meal
                        matched = True
                        break
                if not matched:
                    raise ValueError("meal_not_found")
            elif kind == "update_meal_time":
                day = find_day(op.get("day"))
                from_time = str(op.get("time") or "").strip()
                next_time = str(op.get("new_time") or "").strip()
                matched = False
                for meal in day["meals"]:
                    if str(meal.get("time") or "").strip() == from_time:
                        meal["time"] = next_time
                        matched = True
                        break
                if not matched:
                    raise ValueError("meal_not_found")
            elif kind == "update_macro_targets":
                mutable["macro_targets"] = op.get("macro_targets") if isinstance(op.get("macro_targets"), dict) else {}
            else:
                raise ValueError("unknown_operation")
            applied += 1
        except ValueError as exc:
            failed.append({"op": kind, "reason": str(exc)})
            if not allow_partial:
                break
    if failed and not allow_partial:
        return {"ok": False, "template_id": template_id, "changed": False, "operations_applied": applied, "operations_failed": failed, "warnings": warnings}
    replacement_body = {"name": mutable["name"], "days": mutable["days"], "set_active": bool(target["is_active"])}
    if isinstance(mutable.get("macro_targets"), dict):
        replacement_body["macro_targets"] = mutable["macro_targets"]
    normalized = _normalize_structured_nutrition_plan(replacement_body)
    if normalized["unresolved_items"] and not allow_partial:
        return {"ok": False, "template_id": template_id, "changed": False, "operations_applied": applied, "operations_failed": failed, "warnings": normalized["warnings"], "unresolved_items": normalized["unresolved_items"]}
    if dry_run:
        return {"ok": True, "template_id": template_id, "changed": applied > 0, "operations_applied": applied, "operations_failed": failed, "warnings": warnings + list(normalized["warnings"]), "unresolved_items": normalized["unresolved_items"], "updated_template_preview": {"title": normalized["title"], "days_count": len(normalized["normalized_days"])}}
    result = _write_template_days(template_id, normalized, clear_existing=True, title_override=normalized["title"], set_active=None)
    return {"ok": True, "template_id": template_id, "changed": applied > 0, "operations_applied": applied, "operations_failed": failed, "warnings": warnings + list(result.get("warnings") or []), "updated_plan_after": result["created_template_after"], **result}
