from datetime import datetime, timedelta
import sqlite3
import time
from zoneinfo import ZoneInfo
from pathlib import Path
import json
from database import connections as db_connections
from flask import Blueprint, jsonify, request
from typing import Optional
from security.private_access import require_master
from app_support.site_read_models import invalidate_snapshot_prefix

from .nutrition_planning_db import (
    add_meal_item,
    archive_week_template,
    build_mfp_checklist,
    build_shopping_list,
    clear_active_week_slots,
    create_food,
    create_meal_template,
    create_slot_template,
    create_week_day_slot,
    create_week_day_group,
    detach_week_day_group_members,
    sync_week_day_group,
    create_week_template,
    delete_food,
    delete_meal_item,
    delete_meal_template,
    delete_all_meal_templates,
    delete_week_day_slot,
    delete_week_template,
    get_active_week_plan_resolved,
    get_active_week_template_id,
    get_week_template_macro_settings,
    get_slot_ingredients,
    import_mealplan_text,
    list_foods,
    list_meal_templates,
    list_slot_templates,
    list_week_templates,
    resolve_meal_template,
    resolve_mealplan_import,
    restore_week_template,
    save_slot_override,
    set_active_week_template,
    update_food,
    update_meal_item,
    update_meal_template,
    update_slot_template,
    update_week_template_title,
    update_week_template_macro_settings,
    update_week_slot,
    update_week_day_group_amount,
    create_free_logged_meal,
    copy_logged_meals_to_date,
    create_recent_meal_log,
    delete_logged_meal,
    duplicate_logged_meal,
    get_logging_day_payload,
    get_logging_recents,
    log_planned_meal,
    search_logging_foods,
    set_planned_meal_status,
    toggle_food_favorite,
    update_logged_meal,
    WEEKDAY_LABELS,
)
from .core_nutrition_engine import (
    ensure_core_nutrition_schema,
    evaluate_core_nutrition_day,
    get_core_nutrition_actions_for_day,
)

nutrition_planning_api = Blueprint("nutrition_planning_api", __name__)

_SCHEMA_OK_FOR: set[str] = set()
_TODAY_PLAN_CACHE_TTL_SECONDS = 8.0
_TODAY_PLAN_CACHE: dict[str, tuple[float, dict]] = {}
_CORE_DAY_CACHE_TTL_SECONDS = 30.0
_CORE_DAY_CACHE: dict[str, tuple[float, dict]] = {}
_TODAY_PLAN_SHARED_CACHE_PATH = Path("/tmp/liva_mealplan_today_cache_v1.json")


def _invalidate_dashboard_nutrition_caches() -> None:
    try:
        invalidate_snapshot_prefix("dashboard_snapshot:")
        invalidate_snapshot_prefix("today_snapshot:")
        invalidate_snapshot_prefix("next_session_snapshot:")
    except Exception:
        pass
    try:
        _TODAY_PLAN_CACHE.clear()
    except Exception:
        pass
    try:
        _CORE_DAY_CACHE.clear()
    except Exception:
        pass
    try:
        if _TODAY_PLAN_SHARED_CACHE_PATH.exists():
            _TODAY_PLAN_SHARED_CACHE_PATH.unlink()
    except Exception:
        pass


def _read_today_shared_cache(cache_key: str) -> Optional[dict]:
    try:
        if not _TODAY_PLAN_SHARED_CACHE_PATH.exists():
            return None
        data = json.loads(_TODAY_PLAN_SHARED_CACHE_PATH.read_text(encoding="utf-8"))
        entries = data.get("entries") if isinstance(data, dict) else None
        entry = entries.get(cache_key) if isinstance(entries, dict) else None
        if not isinstance(entry, dict):
            return None
        ts = float(entry.get("ts") or 0.0)
        payload = entry.get("payload")
        if not isinstance(payload, dict):
            return None
        if (time.time() - ts) > _TODAY_PLAN_CACHE_TTL_SECONDS:
            return None
        return payload
    except Exception:
        return None


def _write_today_shared_cache(cache_key: str, payload: dict) -> None:
    if not isinstance(payload, dict):
        return
    now_ts = time.time()
    data = {"entries": {}}
    try:
        if _TODAY_PLAN_SHARED_CACHE_PATH.exists():
            old = json.loads(_TODAY_PLAN_SHARED_CACHE_PATH.read_text(encoding="utf-8"))
            if isinstance(old, dict) and isinstance(old.get("entries"), dict):
                data = old
    except Exception:
        data = {"entries": {}}
    entries = data.get("entries")
    if not isinstance(entries, dict):
        entries = {}
        data["entries"] = entries
    entries[cache_key] = {"ts": now_ts, "payload": payload}
    # Keep file tiny.
    if len(entries) > 24:
        sorted_items = sorted(
            entries.items(),
            key=lambda kv: float(((kv[1] or {}).get("ts") or 0.0)),
            reverse=True,
        )
        entries = {k: v for k, v in sorted_items[:16]}
        data["entries"] = entries
    tmp_path = _TODAY_PLAN_SHARED_CACHE_PATH.with_suffix(".tmp")
    try:
        tmp_path.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        tmp_path.replace(_TODAY_PLAN_SHARED_CACHE_PATH)
    except Exception:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except Exception:
            pass


def _get_core_day_cached(date_iso: str) -> dict:
    day_key = str(date_iso or "").strip() or _berlin_today().isoformat()
    now_ts = time.time()
    cached = _CORE_DAY_CACHE.get(day_key)
    if cached:
        ts, payload = cached
        if (now_ts - float(ts or 0.0)) <= _CORE_DAY_CACHE_TTL_SECONDS and isinstance(payload, dict):
            return payload
        _CORE_DAY_CACHE.pop(day_key, None)
    payload = evaluate_core_nutrition_day(day_key)
    if isinstance(payload, dict):
        _CORE_DAY_CACHE[day_key] = (now_ts, payload)
    return payload


def _parse_bool(value: object) -> bool:
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _as_int(value: object) -> Optional[int]:
    try:
        if value is None or value == "":
            return None
        return int(value)
    except Exception:
        return None


@nutrition_planning_api.before_app_request
def _ensure_schema_once():
    schema_key = str(db_connections.NUTRITION_DB)
    if schema_key in _SCHEMA_OK_FOR:
        return
    from .nutrition_planning_db import ensure_nutrition_planning_schema

    ensure_nutrition_planning_schema()
    ensure_core_nutrition_schema()
    _SCHEMA_OK_FOR.add(schema_key)


def _berlin_today():
    return datetime.now(ZoneInfo("Europe/Berlin")).date()


def build_today_plan_payload(*, include_items: bool = True, bypass_cache: bool = False) -> dict:
    today = _berlin_today()
    cache_key = f"{today.isoformat()}|include_items={1 if include_items else 0}"
    now_ts = time.time()
    if not bypass_cache:
        cache_entry = _TODAY_PLAN_CACHE.get(cache_key)
        if cache_entry:
            ts, payload_cached = cache_entry
            if (now_ts - float(ts or 0.0)) <= _TODAY_PLAN_CACHE_TTL_SECONDS and isinstance(payload_cached, dict):
                return payload_cached
            _TODAY_PLAN_CACHE.pop(cache_key, None)
        shared_cached = _read_today_shared_cache(cache_key)
        if isinstance(shared_cached, dict):
            _TODAY_PLAN_CACHE[cache_key] = (now_ts, shared_cached)
            return shared_cached

    weekday_idx = today.weekday()
    weekday_label = WEEKDAY_LABELS[weekday_idx] if 0 <= weekday_idx < len(WEEKDAY_LABELS) else str(weekday_idx)
    week_start = (today - timedelta(days=weekday_idx)).isoformat()

    payload = get_active_week_plan_resolved()
    week_template = payload.get("week_template") if isinstance(payload, dict) else None
    day_entry = None
    if week_template:
        for day in week_template.get("days") or []:
            if day.get("weekday") == weekday_idx:
                day_entry = day
                break

    core_day = _get_core_day_cached(today.isoformat())
    core_planned = core_day.get("planned_meals") if isinstance(core_day, dict) else []

    def _filter_items(raw_items):
        out = []
        for item in raw_items or []:
            if not isinstance(item, dict):
                continue
            unit_raw = (item.get("unit") or "").strip().lower()
            if item.get("item_type") == "kcal_only" or unit_raw == "kcal":
                continue
            name = (item.get("name") or item.get("food_name") or item.get("name_raw") or item.get("raw") or "").strip()
            if not name:
                continue
            amount = item.get("amount")
            if isinstance(amount, (int, float)) and amount == 0:
                amount = None
            unit = (item.get("unit") or "").strip() or None
            normalized_unit = (unit or "").strip().lower() or None
            amount_g = amount if normalized_unit == "g" else None
            amount_ml = amount if normalized_unit == "ml" else None
            amount_pcs = amount if normalized_unit == "pcs" else None
            if amount is None:
                display_amount = ""
            elif normalized_unit == "pcs":
                display_amount = f"{int(amount) if isinstance(amount, (int, float)) and float(amount).is_integer() else f'{float(amount):g}'}×"
            elif isinstance(amount, (int, float)) and float(amount).is_integer():
                unit_label = normalized_unit or ""
                display_amount = f"{int(amount)} {unit_label}".strip()
            else:
                unit_label = normalized_unit or ""
                display_amount = f"{float(amount):g} {unit_label}".strip() if isinstance(amount, (int, float)) else f"{amount} {unit_label}".strip()
            out.append(
                {
                    "name": name,
                    "amount": amount,
                    "unit": unit,
                    "amount_g": amount_g,
                    "amount_ml": amount_ml,
                    "amount_pcs": amount_pcs,
                    "display_amount": display_amount,
                }
            )
        return out

    def _default_marker(state_value: str, status_value: str):
        state_norm = str(state_value or "").strip().lower()
        status_norm = str(status_value or "").strip().lower()
        if state_norm == "logged" or status_norm in {"logged", "telegram_confirmed", "changed", "manual_override"}:
            return "done"
        if state_norm in {"shifted_by_core"} or status_norm == "shifted":
            return "shifted"
        if state_norm in {"portion_adjusted_by_core", "accepted_adjustment", "switched"} or status_norm == "adjusted_by_core":
            return "adjusted"
        if state_norm in {"skipped", "missed_window", "cancelled_by_replan", "merged_into_other_meal", "fulfilled_by_alternative"} or status_norm in {"skipped", "missed"}:
            return "missed"
        if state_norm == "replaced":
            return "replaced"
        return "open"

    meals_out = []
    if isinstance(core_planned, list) and core_planned:
        for core_meal in core_planned:
            if not isinstance(core_meal, dict):
                continue
            effective_items = _filter_items(core_meal.get("items") if include_items else [])
            base_items = _filter_items(core_meal.get("base_items") if include_items else [])
            effective_name = (core_meal.get("title") or "").strip()
            base_name = (core_meal.get("base_title") or "").strip()
            time_text = (core_meal.get("time_text") or core_meal.get("shifted_time_text") or "").strip() or None
            base_time = (core_meal.get("base_time_text") or "").strip() or None
            status = str(core_meal.get("status") or "open").strip() or "open"
            state = str(core_meal.get("state") or "").strip().lower() or None
            marker = str(core_meal.get("core_marker") or "").strip().lower() or _default_marker(state, status)
            meals_out.append(
                {
                    "slot_id": int(core_meal.get("slot_id") or 0),
                    "slot_index": int(core_meal.get("slot_index") or 0),
                    "time": time_text,
                    "name": effective_name or base_name or _join_item_names(effective_items) or "Food",
                    "items": effective_items if include_items else [],
                    "status": status,
                    "state": state,
                    "state_label": core_meal.get("state_label"),
                    "source_of_truth": core_meal.get("source_of_truth"),
                    "last_mutation_type": core_meal.get("last_mutation_type"),
                    "last_mutation_at": core_meal.get("last_mutation_at"),
                    "status_source": core_meal.get("status_source"),
                    "base_name": base_name or None,
                    "base_time": base_time,
                    "base_items": base_items if include_items else [],
                    "core_adjusted_name": (effective_name if (base_name and effective_name and effective_name.strip().lower() != base_name.strip().lower()) else None),
                    "core_adjusted_time": (time_text if (base_time and time_text and time_text != base_time) else None),
                    "core_marker": marker,
                    "core_reason": core_meal.get("core_reason") or core_meal.get("reason_code") or core_meal.get("last_mutation_type"),
                    "_sort": (
                        _parse_time_minutes(time_text) is None,
                        _parse_time_minutes(time_text) or 10_000,
                        int(core_meal.get("slot_index") or 0),
                        int(core_meal.get("slot_id") or 0),
                    ),
                }
            )
    elif day_entry:
        for slot in day_entry.get("slots") or []:
            slot_items = slot.get("items") or []
            override_items = (slot.get("override") or {}).get("ingredients") or []
            has_meal = bool(
                slot.get("meal_template")
                or slot.get("meal_template_id")
                or slot.get("slot_meal_id")
                or override_items
                or slot_items
            )
            if not has_meal:
                continue
            filtered_items = _filter_items(slot_items if include_items else [])
            if not filtered_items and not slot.get("meal_template_id") and not slot.get("slot_meal_id"):
                continue
            meal_name = (
                (slot.get("custom_title") or "").strip()
                or ((slot.get("meal_template") or {}).get("title") or "").strip()
                or (slot.get("meal_title") or "").strip()
                or _join_item_names(filtered_items)
                or "Food"
            )
            time_text = (slot.get("time_text") or "").strip() or None
            meals_out.append(
                {
                    "slot_id": int(slot.get("id") or 0),
                    "slot_index": int(slot.get("slot_index") or 0),
                    "time": time_text,
                    "name": meal_name,
                    "items": filtered_items if include_items else [],
                    "status": "planned",
                    "state": "planned",
                    "source_of_truth": "base_plan",
                    "core_marker": "open",
                    "_sort": (
                        _parse_time_minutes(time_text) is None,
                        _parse_time_minutes(time_text) or 10_000,
                        int(slot.get("slot_index") or 0),
                        int(slot.get("id") or 0),
                    ),
                }
            )

    meals_out.sort(key=lambda row: row.get("_sort") or (1, 10_000, 0, 0))
    for row in meals_out:
        row.pop("_sort", None)

    source = None
    if week_template:
        source = {
            "template_id": week_template.get("id"),
            "template_name": week_template.get("title"),
            "week_start": week_start,
        }

    payload_out = {
        "date": today.isoformat(),
        "weekday": weekday_label,
        "source": source,
        "meals": meals_out,
        "core_nutrition": core_day,
    }
    if not bypass_cache:
        _TODAY_PLAN_CACHE[cache_key] = (now_ts, payload_out)
        if len(_TODAY_PLAN_CACHE) > 32:
            old_keys = sorted(_TODAY_PLAN_CACHE.keys(), key=lambda k: float((_TODAY_PLAN_CACHE.get(k) or (0.0, {}))[0] or 0.0))
            for key in old_keys[: max(0, len(_TODAY_PLAN_CACHE) - 24)]:
                _TODAY_PLAN_CACHE.pop(key, None)
        _write_today_shared_cache(cache_key, payload_out)
    return payload_out


def read_cached_today_plan_payload(*, include_items: bool = True) -> dict | None:
    today = _berlin_today()
    cache_key = f"{today.isoformat()}|include_items={1 if include_items else 0}"
    cache_entry = _TODAY_PLAN_CACHE.get(cache_key)
    if cache_entry:
        ts, payload_cached = cache_entry
        if (time.time() - float(ts or 0.0)) <= _TODAY_PLAN_CACHE_TTL_SECONDS and isinstance(payload_cached, dict):
            return payload_cached
        _TODAY_PLAN_CACHE.pop(cache_key, None)
    shared_cached = _read_today_shared_cache(cache_key)
    if isinstance(shared_cached, dict):
        _TODAY_PLAN_CACHE[cache_key] = (time.time(), shared_cached)
        return shared_cached
    return None


def _parse_time_minutes(value: Optional[str]) -> Optional[int]:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    parts = text.split(":")
    try:
        hours = int(parts[0])
        minutes = int(parts[1]) if len(parts) > 1 else 0
    except Exception:
        return None
    if hours < 0 or hours > 23 or minutes < 0 or minutes > 59:
        return None
    return hours * 60 + minutes


def _join_item_names(items):
    names = [str(it.get("name") or "").strip() for it in items or [] if it.get("name")]
    return " + ".join([n for n in names if n])


def _as_bool_or_none(value: object) -> Optional[bool]:
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "on"):
        return True
    if text in ("0", "false", "no", "off"):
        return False
    return None


# === Foods ===

@nutrition_planning_api.get("/api/nutrition/foods")
def api_nutrition_foods_list():
    q = (request.args.get("q") or "").strip()
    limit = _as_int(request.args.get("limit")) or 300
    favorites_only = _parse_bool(request.args.get("favorites"))
    foods = list_foods(query=q, limit=limit, favorites_only=favorites_only)
    return jsonify(ok=True, foods=foods)


@nutrition_planning_api.post("/api/nutrition/foods")
def api_nutrition_foods_create():
    payload = request.get_json(silent=True) or {}
    food, err = create_food(payload)
    if err:
        return jsonify(ok=False, error=err), 400
    return jsonify(ok=True, food=food)


@nutrition_planning_api.put("/api/nutrition/foods/<int:food_id>")
def api_nutrition_foods_update(food_id: int):
    payload = request.get_json(silent=True) or {}
    food, err = update_food(food_id, payload)
    if err == "not_found":
        return jsonify(ok=False, error=err), 404
    if err:
        return jsonify(ok=False, error=err), 400
    return jsonify(ok=True, food=food)


@nutrition_planning_api.delete("/api/nutrition/foods/<int:food_id>")
def api_nutrition_foods_delete(food_id: int):
    force = _parse_bool(request.args.get("force"))
    ok, err = delete_food(food_id, force=force)
    if not ok and err:
        return jsonify(ok=False, **err), 409
    return jsonify(ok=True)


# === Meal templates ===

@nutrition_planning_api.get("/api/nutrition/meal_templates")
def api_nutrition_meal_templates_list():
    q = (request.args.get("q") or "").strip()
    limit = _as_int(request.args.get("limit")) or 300
    templates = list_meal_templates(query=q, limit=limit)
    return jsonify(ok=True, templates=templates)


@nutrition_planning_api.get("/api/nutrition/meal_templates/<int:template_id>/resolved")
def api_nutrition_meal_templates_resolved(template_id: int):
    meal = resolve_meal_template(template_id)
    if not meal:
        return jsonify(ok=False, error="not_found"), 404
    return jsonify(ok=True, meal=meal)


@nutrition_planning_api.post("/api/nutrition/meal_templates")
def api_nutrition_meal_templates_create():
    payload = request.get_json(silent=True) or {}
    tpl, err = create_meal_template(payload)
    if err:
        return jsonify(ok=False, error=err), 400
    return jsonify(ok=True, template=tpl)


@nutrition_planning_api.put("/api/nutrition/meal_templates/<int:template_id>")
def api_nutrition_meal_templates_update(template_id: int):
    payload = request.get_json(silent=True) or {}
    tpl, err = update_meal_template(template_id, payload)
    if err == "not_found":
        return jsonify(ok=False, error=err), 404
    if err:
        return jsonify(ok=False, error=err), 400
    return jsonify(ok=True, template=tpl)


@nutrition_planning_api.delete("/api/nutrition/meal_templates/<int:template_id>")
def api_nutrition_meal_templates_delete(template_id: int):
    ok, err = delete_meal_template(template_id)
    if not ok and err:
        code = 400
        if err == "not_found":
            code = 404
        return jsonify(ok=False, error=err), code
    return jsonify(ok=True)


@nutrition_planning_api.delete("/api/nutrition/meal_templates")
def api_nutrition_meal_templates_delete_all():
    result = delete_all_meal_templates()
    if not result.get("ok"):
        return jsonify(ok=False, error=result.get("error") or "delete_failed"), 400
    return jsonify(ok=True, deleted=result.get("deleted", 0))


@nutrition_planning_api.post("/api/nutrition/meal_templates/<int:template_id>/items")
def api_nutrition_meal_templates_add_item(template_id: int):
    payload = request.get_json(silent=True) or {}
    item, err = add_meal_item(template_id, payload)
    if err == "not_found":
        return jsonify(ok=False, error=err), 404
    if err:
        return jsonify(ok=False, error=err), 400
    return jsonify(ok=True, item=item)


@nutrition_planning_api.put("/api/nutrition/meal_template_items/<int:item_id>")
def api_nutrition_meal_template_items_update(item_id: int):
    payload = request.get_json(silent=True) or {}
    item, err = update_meal_item(item_id, payload)
    if err:
        return jsonify(ok=False, error=err), 400
    return jsonify(ok=True, item=item)


@nutrition_planning_api.delete("/api/nutrition/meal_template_items/<int:item_id>")
def api_nutrition_meal_template_items_delete(item_id: int):
    delete_meal_item(item_id)
    return jsonify(ok=True)


# === Slot templates ===

@nutrition_planning_api.get("/api/nutrition/slots/templates")
def api_slot_templates_list():
    slots = list_slot_templates()
    return jsonify(ok=True, slot_templates=slots)


@nutrition_planning_api.post("/api/nutrition/slots/templates")
def api_slot_templates_create():
    payload = request.get_json(silent=True) or {}
    slot, err = create_slot_template(payload)
    if err:
        return jsonify(ok=False, error=err), 400
    return jsonify(ok=True, slot_template=slot)


@nutrition_planning_api.put("/api/nutrition/slots/templates/<int:slot_template_id>")
def api_slot_templates_update(slot_template_id: int):
    payload = request.get_json(silent=True) or {}
    slot, err = update_slot_template(slot_template_id, payload)
    if err == "not_found":
        return jsonify(ok=False, error=err), 404
    if err:
        return jsonify(ok=False, error=err), 400
    return jsonify(ok=True, slot_template=slot)


@nutrition_planning_api.post("/api/nutrition/plan/days/<int:day_id>/slots")
def api_plan_day_slot_create(day_id: int):
    payload = request.get_json(silent=True) or {}
    slot_template_id = _as_int(payload.get("slot_template_id"))
    if not slot_template_id:
        return jsonify(ok=False, error="slot_template_required"), 400
    slot, err = create_week_day_slot(day_id, slot_template_id)
    if err == "day_not_found":
        return jsonify(ok=False, error=err), 404
    if err:
        return jsonify(ok=False, error=err), 400
    return jsonify(ok=True, slot=slot)


# === Plan & slots ===

@nutrition_planning_api.get("/api/nutrition/plan/week")
def api_nutrition_week_plan():
    template = _as_int(request.args.get("template"))
    if template:
        from .nutrition_planning_db import _build_week_plan_payload_wrapper
        plan = _build_week_plan_payload_wrapper(template)
    else:
        plan = get_active_week_plan_resolved()
    return jsonify(plan)


@nutrition_planning_api.post("/api/nutrition/plan/day-groups")
def api_nutrition_day_group_create():
    """Link weekdays to one canonical plan day, with a deliberate source."""
    payload = request.get_json(silent=True) or {}
    template_id = _as_int(payload.get("template_id")) or get_active_week_template_id()
    if not template_id:
        return jsonify(ok=False, error="active_template_required"), 400
    group, err = create_week_day_group(
        template_id,
        payload.get("weekdays") if isinstance(payload.get("weekdays"), list) else [],
        payload.get("source_weekday"),
        payload.get("name") or "Gemeinsamer Tag",
        payload.get("color"),
    )
    if err:
        return jsonify(ok=False, error=err), 400
    _invalidate_dashboard_nutrition_caches()
    return jsonify(ok=True, day_group=group, message="Die ausgewählten Tage werden jetzt gemeinsam verwaltet.")


@nutrition_planning_api.post("/api/nutrition/plan/day-groups/<string:group_id>/detach")
def api_nutrition_day_group_detach(group_id: str):
    payload = request.get_json(silent=True) or {}
    template_id = _as_int(payload.get("template_id")) or get_active_week_template_id()
    result, err = detach_week_day_group_members(template_id, group_id, payload.get("weekdays") if isinstance(payload.get("weekdays"), list) else [])
    if err:
        return jsonify(ok=False, error=err), 400
    _invalidate_dashboard_nutrition_caches()
    return jsonify(ok=True, result=result, message="Die ausgewählten Tage sind wieder eigenständig.")


@nutrition_planning_api.post("/api/nutrition/plan/day-groups/<string:group_id>/sync")
def api_nutrition_day_group_sync(group_id: str):
    payload = request.get_json(silent=True) or {}
    template_id = _as_int(payload.get("template_id")) or get_active_week_template_id()
    result, err = sync_week_day_group(template_id, group_id)
    if err:
        return jsonify(ok=False, error=err), 400
    _invalidate_dashboard_nutrition_caches()
    return jsonify(ok=True, result=result, message="Das Tagesmuster wurde auf alle verbundenen Tage übertragen.")


@nutrition_planning_api.patch("/api/nutrition/plan/day-groups/<string:group_id>/items/amount")
def api_nutrition_day_group_amount(group_id: str):
    payload = request.get_json(silent=True) or {}
    template_id = _as_int(payload.get("template_id")) or get_active_week_template_id()
    food_id = _as_int(payload.get("food_id"))
    amount = payload.get("amount")
    try:
        amount = float(amount)
    except (TypeError, ValueError):
        amount = None
    if not template_id or not food_id or amount is None or amount < 0:
        return jsonify(ok=False, error="template_id_food_id_and_non_negative_amount_required"), 400
    result, err = update_week_day_group_amount(
        template_id, group_id, slot_index=_as_int(payload.get("slot_index")), time_text=payload.get("time_text"),
        food_id=food_id, amount=amount, unit=str(payload.get("unit") or "g"),
    )
    if err:
        return jsonify(ok=False, error=err), 400
    _invalidate_dashboard_nutrition_caches()
    return jsonify(ok=True, result=result, message="Die Menge wurde für alle Tage der Gruppe geändert.")


@nutrition_planning_api.get("/api/nutrition/plan/today")
def api_nutrition_plan_today():
    include_items = _parse_bool(request.args.get("include_items", "1"))
    bypass_cache = _parse_bool(request.args.get("nocache", "0"))
    return jsonify(build_today_plan_payload(include_items=include_items, bypass_cache=bypass_cache))


# === Logging ===

@nutrition_planning_api.get("/api/nutrition/logging/day")
def api_nutrition_logging_day():
    date_iso = (request.args.get("date") or "").strip() or None
    payload = get_logging_day_payload(date_iso)
    include_core = _parse_bool(request.args.get("include_core"))
    if include_core:
        core_day = _get_core_day_cached(str(payload.get("date") or ""))
        payload["core_nutrition"] = core_day
    return jsonify(payload)


@nutrition_planning_api.get("/api/nutrition/core/day")
def api_nutrition_core_day():
    date_iso = (request.args.get("date") or "").strip() or None
    payload = _get_core_day_cached(str(date_iso or ""))
    payload["actions"] = get_core_nutrition_actions_for_day(payload.get("date"))
    return jsonify(payload)


@nutrition_planning_api.post("/api/nutrition/logging/planned/<int:slot_id>/log")
def api_nutrition_logging_log_planned(slot_id: int):
    payload = request.get_json(silent=True) or {}
    data, err = log_planned_meal(
        slot_id,
        date_iso=payload.get("date"),
        time_mode=(payload.get("time_mode") or "planned"),
        custom_time_text=payload.get("custom_time_text"),
        custom_logged_at=payload.get("logged_at"),
        title=payload.get("title"),
        meal_slot=payload.get("meal_slot"),
        items=payload.get("items"),
        status_source=(payload.get("status_source") or "user"),
        reason_code=payload.get("reason_code"),
        trace=payload.get("trace"),
    )
    if err == "slot_not_found":
        return jsonify(ok=False, error=err), 404
    if err:
        return jsonify(ok=False, error=err), 400
    return jsonify(data)


@nutrition_planning_api.post("/api/nutrition/logging/planned/<int:slot_id>/status")
def api_nutrition_logging_planned_status(slot_id: int):
    payload = request.get_json(silent=True) or {}
    data, err = set_planned_meal_status(
        slot_id,
        date_iso=payload.get("date"),
        status=(payload.get("status") or "open"),
        shifted_time_text=payload.get("shifted_time_text"),
        status_source=(payload.get("status_source") or "user"),
        reason_code=payload.get("reason_code"),
        trace=payload.get("trace"),
    )
    if err == "slot_not_found":
        return jsonify(ok=False, error=err), 404
    if err:
        return jsonify(ok=False, error=err), 400
    return jsonify(data)


@nutrition_planning_api.post("/api/nutrition/logging/free")
def api_nutrition_logging_free_meal():
    payload = request.get_json(silent=True) or {}
    data, err = create_free_logged_meal(
        date_iso=payload.get("date"),
        title=payload.get("title"),
        meal_slot=payload.get("meal_slot"),
        logged_at=payload.get("logged_at"),
        items=payload.get("items"),
        source=(payload.get("source") or "free"),
        mark_favorite=bool(payload.get("is_favorite")),
    )
    if err:
        return jsonify(ok=False, error=err), 400
    return jsonify(data)


def _api_nutrition_logging_update_meal_impl(logged_meal_id: int):
    payload = request.get_json(silent=True) or {}
    data = None
    err = None
    last_exc: Exception | None = None
    for attempt in range(4):
        try:
            data, err = update_logged_meal(logged_meal_id, payload)
            last_exc = None
            break
        except sqlite3.OperationalError as exc:
            last_exc = exc
            if "locked" not in str(exc).lower():
                break
            time.sleep(0.15 * (attempt + 1))
    if last_exc is not None:
        if "locked" in str(last_exc).lower():
            return jsonify(ok=False, error="database_locked"), 503
        return jsonify(ok=False, error=str(last_exc)), 500
    if err == "not_found":
        return jsonify(ok=False, error=err), 404
    if err:
        return jsonify(ok=False, error=err), 400
    return jsonify(data)


@nutrition_planning_api.put("/api/nutrition/logging/meals/<int:logged_meal_id>")
def api_nutrition_logging_update_meal(logged_meal_id: int):
    return _api_nutrition_logging_update_meal_impl(logged_meal_id)


@nutrition_planning_api.post("/api/nutrition/logging/meals/<int:logged_meal_id>/update")
def api_nutrition_logging_update_meal_post(logged_meal_id: int):
    return _api_nutrition_logging_update_meal_impl(logged_meal_id)


def _api_nutrition_logging_delete_meal_impl(logged_meal_id: int):
    payload = request.get_json(silent=True) or {}
    date_iso = (payload.get("date") or request.args.get("date") or "").strip() or None
    data, err = delete_logged_meal(logged_meal_id)
    if err == "not_found":
        return jsonify(ok=False, error=err), 404
    if err:
        return jsonify(ok=False, error=err), 400
    if date_iso and data.get("date") != date_iso:
        data = get_logging_day_payload(date_iso)
    return jsonify(data)


@nutrition_planning_api.delete("/api/nutrition/logging/meals/<int:logged_meal_id>")
def api_nutrition_logging_delete_meal(logged_meal_id: int):
    return _api_nutrition_logging_delete_meal_impl(logged_meal_id)


@nutrition_planning_api.post("/api/nutrition/logging/meals/<int:logged_meal_id>/delete")
def api_nutrition_logging_delete_meal_post(logged_meal_id: int):
    return _api_nutrition_logging_delete_meal_impl(logged_meal_id)


@nutrition_planning_api.post("/api/nutrition/logging/meals/<int:logged_meal_id>/duplicate")
def api_nutrition_logging_duplicate_meal(logged_meal_id: int):
    payload = request.get_json(silent=True) or {}
    data, err = duplicate_logged_meal(logged_meal_id, date_iso=payload.get("date"))
    if err == "not_found":
        return jsonify(ok=False, error=err), 404
    if err:
        return jsonify(ok=False, error=err), 400
    return jsonify(data)


@nutrition_planning_api.post("/api/nutrition/logging/meals/copy")
def api_nutrition_logging_copy_meals():
    payload = request.get_json(silent=True) or {}
    raw_ids = payload.get("meal_ids") if isinstance(payload.get("meal_ids"), list) else []
    meal_ids = [_as_int(value) for value in raw_ids]
    data, err = copy_logged_meals_to_date(
        [value for value in meal_ids if value],
        target_date=payload.get("target_date"),
    )
    if err == "not_found":
        return jsonify(ok=False, error=err), 404
    if err:
        return jsonify(ok=False, error=err), 400
    _invalidate_dashboard_nutrition_caches()
    return jsonify(data)


@nutrition_planning_api.get("/api/nutrition/logging/search/foods")
def api_nutrition_logging_search_foods():
    q = (request.args.get("q") or "").strip()
    limit = _as_int(request.args.get("limit")) or 25
    foods = search_logging_foods(q, limit=limit)
    return jsonify(ok=True, foods=foods)


@nutrition_planning_api.get("/api/nutrition/logging/recents")
def api_nutrition_logging_recents():
    limit = _as_int(request.args.get("limit")) or 12
    return jsonify(ok=True, **get_logging_recents(limit=limit))


@nutrition_planning_api.post("/api/nutrition/logging/foods/<int:food_id>/favorite")
def api_nutrition_logging_food_favorite(food_id: int):
    payload = request.get_json(silent=True) or {}
    value = _as_bool_or_none(payload.get("value"))
    food, err = toggle_food_favorite(food_id, value=value)
    if err == "not_found":
        return jsonify(ok=False, error=err), 404
    if err:
        return jsonify(ok=False, error=err), 400
    return jsonify(ok=True, food=food)


@nutrition_planning_api.post("/api/nutrition/logging/recents/<meal_key>/log")
def api_nutrition_logging_recent_meal_log(meal_key: str):
    payload = request.get_json(silent=True) or {}
    data, err = create_recent_meal_log(
        meal_key,
        date_iso=payload.get("date"),
        logged_at=payload.get("logged_at"),
        meal_slot=payload.get("meal_slot"),
    )
    if err == "not_found":
        return jsonify(ok=False, error=err), 404
    if err:
        return jsonify(ok=False, error=err), 400
    return jsonify(data)


@nutrition_planning_api.get("/api/nutrition/plan/templates")
def api_nutrition_week_templates_list():
    include_archived = _parse_bool(request.args.get("include_archived"))
    templates = list_week_templates(include_archived=include_archived)
    return jsonify(ok=True, templates=templates)


@nutrition_planning_api.post("/api/nutrition/plan/templates")
def api_nutrition_week_templates_create():
    payload = request.get_json(silent=True) or {}
    title = payload.get("title") or "Neues Template"
    source_id = _as_int(payload.get("source_template_id"))
    set_active = _parse_bool(payload.get("set_active", False))
    tpl, err = create_week_template(title, source_template_id=source_id, set_active=set_active)
    if err == "source_not_found":
        return jsonify(ok=False, error=err), 404
    if err:
        return jsonify(ok=False, error=err), 400
    _invalidate_dashboard_nutrition_caches()
    return jsonify(ok=True, template=tpl)


@nutrition_planning_api.post("/api/nutrition/plan/templates/<int:template_id>/set_active")
def api_nutrition_week_templates_set_active(template_id: int):
    tpl, err = set_active_week_template(template_id)
    if err == "not_found":
        return jsonify(ok=False, error=err), 404
    if err:
        return jsonify(ok=False, error=err), 400
    _invalidate_dashboard_nutrition_caches()
    return jsonify(ok=True, template=tpl)


@nutrition_planning_api.post("/api/nutrition/plan/templates/<int:template_id>/archive")
def api_nutrition_week_templates_archive(template_id: int):
    tpl, err = archive_week_template(template_id)
    if err == "not_found":
        return jsonify(ok=False, error=err), 404
    if err:
        return jsonify(ok=False, error=err), 400
    _invalidate_dashboard_nutrition_caches()
    return jsonify(ok=True, template=tpl)


@nutrition_planning_api.post("/api/nutrition/plan/templates/<int:template_id>/restore")
def api_nutrition_week_templates_restore(template_id: int):
    tpl, err = restore_week_template(template_id)
    if err == "not_found":
        return jsonify(ok=False, error=err), 404
    if err:
        return jsonify(ok=False, error=err), 400
    _invalidate_dashboard_nutrition_caches()
    return jsonify(ok=True, template=tpl)


@nutrition_planning_api.delete("/api/nutrition/plan/templates/<int:template_id>")
def api_nutrition_week_templates_delete(template_id: int):
    ok, err = delete_week_template(template_id)
    if not ok and err == "not_found":
        return jsonify(ok=False, error=err), 404
    if not ok:
        return jsonify(ok=False, error=err or "delete_failed"), 400
    _invalidate_dashboard_nutrition_caches()
    return jsonify(ok=True)


@nutrition_planning_api.patch("/api/nutrition/plan/slots/<int:slot_id>")
def api_nutrition_plan_slot_update(slot_id: int):
    payload = request.get_json(silent=True) or {}
    slot, err = update_week_slot(slot_id, payload)
    if err == "not_found":
        return jsonify(ok=False, error=err), 404
    if err:
        return jsonify(ok=False, error=err), 400
    _invalidate_dashboard_nutrition_caches()
    return jsonify(ok=True, slot=slot)


@nutrition_planning_api.delete("/api/nutrition/plan/slots/<int:slot_id>")
def api_nutrition_plan_slot_delete(slot_id: int):
    ok, err = delete_week_day_slot(slot_id)
    if not ok and err == "not_found":
        return jsonify(ok=False, error=err), 404
    if not ok:
        return jsonify(ok=False, error=err or "delete_failed"), 400
    _invalidate_dashboard_nutrition_caches()
    return jsonify(ok=True)


@nutrition_planning_api.post("/api/nutrition/plan/week/clear")
def api_nutrition_plan_week_clear():
    payload = request.get_json(silent=True) or {}
    template_id = payload.get("template_id")
    ok, err, stats = clear_active_week_slots(template_id)
    if not ok:
        return jsonify(ok=False, error=err or "clear_failed"), 400
    _invalidate_dashboard_nutrition_caches()
    return jsonify(ok=True, **(stats or {}))


@nutrition_planning_api.get("/api/nutrition/plan/slots/<int:slot_id>/ingredients")
def api_nutrition_slot_ingredients(slot_id: int):
    data, err = get_slot_ingredients(slot_id)
    if err == "not_found":
        return jsonify(ok=False, error=err), 404
    if err:
        return jsonify(ok=False, error=err), 400
    return jsonify(ok=True, **data)


@nutrition_planning_api.put("/api/nutrition/plan/slots/<int:slot_id>/ingredients")
def api_nutrition_slot_ingredients_update(slot_id: int):
    payload = request.get_json(silent=True) or {}
    ingredients = payload.get("ingredients") or []
    data, err = save_slot_override(slot_id, ingredients)
    if err == "not_found":
        return jsonify(ok=False, error=err), 404
    if err:
        return jsonify(ok=False, error=err), 400
    _invalidate_dashboard_nutrition_caches()
    return jsonify(ok=True, **data)


@nutrition_planning_api.post("/api/planning/meal/items/add")
def api_planning_meal_items_add():
    payload = request.get_json(silent=True) or {}
    slot_meal_id = _as_int(payload.get("slot_meal_id"))
    item_payload = payload.get("payload") or {}
    if not slot_meal_id:
        return jsonify(ok=False, error="missing_slot_meal_id"), 400
    conn = get_nutrition_db()
    cur = conn.cursor()
    slot_meal = cur.execute("SELECT * FROM nutrition_slot_meals WHERE id=?", (slot_meal_id,)).fetchone()
    if not slot_meal:
        conn.close()
        return jsonify(ok=False, error="slot_meal_not_found"), 404
    slot = cur.execute(
        "SELECT id FROM nutrition_week_day_slots WHERE week_template_day_id=? AND slot_index=?",
        (slot_meal["week_template_day_id"], slot_meal["slot_index"]),
    ).fetchone()
    conn.close()
    if not slot:
        return jsonify(ok=False, error="slot_not_found"), 404
    data, err = get_slot_ingredients(slot["id"])
    if err:
        return jsonify(ok=False, error=err), 400
    items = list(data.get("ingredients") or [])
    items.append(item_payload)
    data, err = save_slot_override(slot["id"], items)
    if err:
        return jsonify(ok=False, error=err), 400
    _invalidate_dashboard_nutrition_caches()
    return jsonify(ok=True, slot_meal_id=slot_meal_id, source="slot", **data)


@nutrition_planning_api.post("/api/planning/meal/items/update")
def api_planning_meal_items_update():
    payload = request.get_json(silent=True) or {}
    slot_meal_id = _as_int(payload.get("slot_meal_id"))
    item_id = payload.get("item_id")
    item_payload = payload.get("payload") or {}
    if not slot_meal_id or item_id is None:
        return jsonify(ok=False, error="missing_item"), 400
    conn = get_nutrition_db()
    cur = conn.cursor()
    slot_meal = cur.execute("SELECT * FROM nutrition_slot_meals WHERE id=?", (slot_meal_id,)).fetchone()
    if not slot_meal:
        conn.close()
        return jsonify(ok=False, error="slot_meal_not_found"), 404
    slot = cur.execute(
        "SELECT id FROM nutrition_week_day_slots WHERE week_template_day_id=? AND slot_index=?",
        (slot_meal["week_template_day_id"], slot_meal["slot_index"]),
    ).fetchone()
    conn.close()
    if not slot:
        return jsonify(ok=False, error="slot_not_found"), 404
    data, err = get_slot_ingredients(slot["id"])
    if err:
        return jsonify(ok=False, error=err), 400
    items = list(data.get("ingredients") or [])
    for idx, item in enumerate(items):
        if str(item.get("id") or item.get("fingerprint")) == str(item_id):
            items[idx] = {**item, **item_payload}
            break
    data, err = save_slot_override(slot["id"], items)
    if err:
        return jsonify(ok=False, error=err), 400
    _invalidate_dashboard_nutrition_caches()
    return jsonify(ok=True, slot_meal_id=slot_meal_id, source="slot", **data)


@nutrition_planning_api.post("/api/planning/meal/items/delete")
def api_planning_meal_items_delete():
    payload = request.get_json(silent=True) or {}
    slot_meal_id = _as_int(payload.get("slot_meal_id"))
    item_id = payload.get("item_id")
    if not slot_meal_id or item_id is None:
        return jsonify(ok=False, error="missing_item"), 400
    conn = get_nutrition_db()
    cur = conn.cursor()
    slot_meal = cur.execute("SELECT * FROM nutrition_slot_meals WHERE id=?", (slot_meal_id,)).fetchone()
    if not slot_meal:
        conn.close()
        return jsonify(ok=False, error="slot_meal_not_found"), 404
    slot = cur.execute(
        "SELECT id FROM nutrition_week_day_slots WHERE week_template_day_id=? AND slot_index=?",
        (slot_meal["week_template_day_id"], slot_meal["slot_index"]),
    ).fetchone()
    conn.close()
    if not slot:
        return jsonify(ok=False, error="slot_not_found"), 404
    data, err = get_slot_ingredients(slot["id"])
    if err:
        return jsonify(ok=False, error=err), 400
    items = [item for item in (data.get("ingredients") or []) if str(item.get("id") or item.get("fingerprint")) != str(item_id)]
    data, err = save_slot_override(slot["id"], items)
    if err:
        return jsonify(ok=False, error=err), 400
    _invalidate_dashboard_nutrition_caches()
    return jsonify(ok=True, slot_meal_id=slot_meal_id, source="slot", **data)


@nutrition_planning_api.patch("/api/nutrition/plan/template")
def api_nutrition_plan_template_update():
    payload = request.get_json(silent=True) or {}
    template_id = _as_int(payload.get("template_id"))
    title = payload.get("title")
    template, err = update_week_template_title(template_id, title)
    if err == "not_found":
        return jsonify(ok=False, error=err), 404
    if err:
        return jsonify(ok=False, error=err), 400
    _invalidate_dashboard_nutrition_caches()
    return jsonify(ok=True, template=template)


@nutrition_planning_api.get("/api/nutrition/plan/templates/<int:template_id>/macro_settings")
def api_nutrition_plan_template_macro_settings(template_id: int):
    data, err = get_week_template_macro_settings(template_id)
    if err == "not_found":
        return jsonify(ok=False, error=err), 404
    if err:
        return jsonify(ok=False, error=err), 400
    return jsonify(ok=True, settings=data)


@nutrition_planning_api.post("/api/nutrition/plan/templates/<int:template_id>/macro_settings")
def api_nutrition_plan_template_macro_settings_update(template_id: int):
    payload = request.get_json(silent=True) or {}
    data, err = update_week_template_macro_settings(template_id, payload)
    if err == "not_found":
        return jsonify(ok=False, error=err), 404
    if err:
        return jsonify(ok=False, error=err), 400
    _invalidate_dashboard_nutrition_caches()
    return jsonify(ok=True, settings=data)


# === Exports ===

@nutrition_planning_api.get("/api/nutrition/export/checklist")
@require_master
def api_nutrition_export_checklist():
    template_id = _as_int(request.args.get("template_id"))
    result = build_mfp_checklist(template_id)
    return jsonify(result)


@nutrition_planning_api.get("/api/nutrition/export/shopping_list")
@require_master
def api_nutrition_export_shopping():
    template_id = _as_int(request.args.get("template_id"))
    result = build_shopping_list(template_id)
    return jsonify(result)


@nutrition_planning_api.post("/api/planning/mealplan/import")
def api_planning_mealplan_import():
    payload = request.get_json(silent=True) or {}
    text = (payload.get("text") or "").strip()
    if not text:
        return (
            jsonify(
                ok=False,
                errors=[{"line": None, "message": "Importtext fehlt", "raw": "", "severity": "error"}],
            ),
            400,
        )
    target = payload.get("target") or {}
    target_type = (target.get("type") or "template").lower()
    target_id = _as_int(target.get("id"))
    template_id = target_id if target_id else get_active_week_template_id()
    if target_type == "week" and target_id is None:
        template_id = get_active_week_template_id()
    if not template_id:
        return jsonify(ok=False, error="no_template"), 400
    mode = (payload.get("mode") or "overwrite").lower()
    if mode not in ("overwrite", "new_template"):
        mode = "overwrite"
    if mode == "new_template":
        title = (payload.get("new_template_name") or "").strip()
        if not title:
            title = f"Imported {datetime.utcnow().strftime('%Y-%m-%d %H:%M')}"
        template, err = create_week_template(title, source_template_id=template_id, set_active=False)
        if err:
            return jsonify(ok=False, error=err), 400
        template_id = template["id"]
    options = payload.get("options") or {}
    scope = payload.get("import_scope") or {}
    scope_slots = bool(scope.get("slots", True))
    scope_templates = bool(scope.get("templates", True))
    result = import_mealplan_text(
        text,
        template_id,
        strict=False,
        scope_slots=scope_slots,
        scope_templates=scope_templates,
        apply=True,
    )
    if not result.get("ok"):
        errors = [e for e in (result.get("errors") or []) if e.get("severity") == "error"]
        warnings = [e for e in (result.get("errors") or []) if e.get("severity") == "warning"]
        return (
            jsonify(
                ok=False,
                errors=errors,
                warnings=warnings,
                write_errors=result.get("write_errors") or [],
            ),
            400,
        )
    errors = [e for e in (result.get("errors") or []) if e.get("severity") == "error"]
    warnings = [e for e in (result.get("errors") or []) if e.get("severity") == "warning"]
    _invalidate_dashboard_nutrition_caches()
    return jsonify(
        ok=True,
        mode="import",
        template_id=result.get("template_id"),
        import_id=result.get("import_id"),
        summary=result.get("summary"),
        unmatched=result.get("unmatched"),
        stats=result.get("stats"),
        errors=errors,
        warnings=warnings,
        write_errors=result.get("write_errors") or [],
    )


@nutrition_planning_api.post("/api/planning/mealplan/validate")
def api_planning_mealplan_validate():
    payload = request.get_json(silent=True) or {}
    text = (payload.get("text") or "").strip()
    if not text:
        return (
            jsonify(
                ok=False,
                errors=[{"line": None, "message": "Importtext fehlt", "raw": "", "severity": "error"}],
                warnings=[],
                write_errors=[],
            ),
            400,
        )
    target = payload.get("target") or {}
    target_id = _as_int(target.get("id"))
    template_id = target_id if target_id else get_active_week_template_id()
    if not template_id:
        return jsonify(ok=False, error="no_template", errors=[], warnings=[], write_errors=[]), 400
    scope = payload.get("import_scope") or {}
    scope_slots = bool(scope.get("slots", True))
    scope_templates = bool(scope.get("templates", True))
    result = import_mealplan_text(
        text,
        template_id,
        strict=False,
        scope_slots=scope_slots,
        scope_templates=scope_templates,
        apply=False,
    )
    if not result.get("ok"):
        errors = [e for e in (result.get("errors") or []) if e.get("severity") == "error"]
        warnings = [e for e in (result.get("errors") or []) if e.get("severity") == "warning"]
        return jsonify(ok=False, errors=errors, warnings=warnings, write_errors=[]), 400
    errors = [e for e in (result.get("errors") or []) if e.get("severity") == "error"]
    warnings = [e for e in (result.get("errors") or []) if e.get("severity") == "warning"]
    return jsonify(
        ok=True,
        mode="validate",
        summary=result.get("summary"),
        unmatched=result.get("unmatched"),
        stats=result.get("stats"),
        errors=errors,
        warnings=warnings,
        write_errors=[],
    )


@nutrition_planning_api.post("/api/planning/mealplan/import/resolve")
def api_planning_mealplan_import_resolve():
    payload = request.get_json(silent=True) or {}
    import_id = payload.get("import_id")
    mappings = payload.get("mappings")
    if not import_id:
        return jsonify(ok=False, error="missing_import_id"), 400
    if not isinstance(mappings, list) or not mappings:
        return jsonify(ok=False, error="mappings_required"), 400
    result = resolve_mealplan_import(import_id, mappings)
    if not result.get("ok"):
        return jsonify(ok=False, error=result.get("error", "resolve_failed")), 400
    _invalidate_dashboard_nutrition_caches()
    return jsonify(ok=True, resolved=result.get("resolved"), remaining=result.get("remaining"))
