# /opt/liva/ai_write_api.py
from __future__ import annotations

import json
import os
import sqlite3
import uuid
from datetime import datetime
from typing import Any, Dict, List, Tuple, Optional

from flask import Blueprint, jsonify, request

from security.write_guard import require_ai_write, require_intent
from nutrition.nutrition_planning_db import (
    create_meal_template,
    get_active_week_plan_resolved,
    list_foods,
    save_slot_override,
    update_meal_template,
    update_week_slot,
    update_week_template_title,
)

ai_write_api = Blueprint("ai_write_api", __name__, url_prefix="/api/aiw")

BASE_DIR = "/opt/liva/database"
PLANS_DB = os.getenv("LIVA_PLANS_DB") or f"{BASE_DIR}/plans.sqlite3"
TRAINING_DB = os.getenv("LIVA_TRAINING_DB") or f"{BASE_DIR}/training.sqlite3"
AI_ALLOW_UNKNOWN_EXERCISES = os.getenv("AI_ALLOW_UNKNOWN_EXERCISES", "0") == "1"


# ------------------------------------------------------------
# DB
# ------------------------------------------------------------

def _connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_schema():
    conn = _connect(PLANS_DB)
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS plans (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        block_length INTEGER NOT NULL DEFAULT 4,
        is_active INTEGER NOT NULL DEFAULT 0,
        data TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """)
    conn.commit()
    conn.close()


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _to_int(x, default: int) -> int:
    try:
        return int(x)
    except Exception:
        return default


def _bool(x) -> bool:
    if isinstance(x, bool):
        return x
    if isinstance(x, (int, float)):
        return bool(x)
    if isinstance(x, str):
        return x.strip().lower() in ("1", "true", "yes", "y", "on")
    return False


def _uid() -> str:
    return uuid.uuid4().hex[:8]


def _round_amount(amount: float, unit: str) -> float:
    if amount is None:
        return 0.0
    unit = (unit or "g").lower()
    if unit in ("pcs", "piece", "pieces", "stück"):
        return round(amount * 2.0) / 2.0
    if unit in ("g", "gram", "grams", "ml"):
        return round(amount / 5.0) * 5.0
    return round(amount, 2)


def _to_float(value: Any) -> Optional[float]:
    try:
        return float(value)
    except Exception:
        return None


def _food_unit_info(food: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    unit = (food.get("unit_default") or "g").lower()
    portion = food.get("common_portion_size") or food.get("portion_g")
    if unit in ("pcs", "piece", "pieces", "stück"):
        if not portion or float(portion) <= 0:
            return None
        factor = float(portion) / 100.0
    else:
        factor = 1.0 / 100.0
    return {"unit": unit, "factor": factor}


def _macro_per_unit(food: Dict[str, Any], unit_info: Dict[str, Any]) -> Dict[str, float]:
    factor = unit_info["factor"]
    return {
        "kcal": float(food.get("kcal_per_100") or 0.0) * factor,
        "p": float(food.get("p_per_100") or 0.0) * factor,
        "c": float(food.get("c_per_100") or 0.0) * factor,
        "f": float(food.get("f_per_100") or 0.0) * factor,
    }


def _pick_best_food(foods: List[Dict[str, Any]], key: str) -> Optional[Dict[str, Any]]:
    best = None
    best_score = 0.0
    for food in foods:
        per_unit = food["per_unit"]
        kcal = per_unit.get("kcal") or 0.0
        macro = per_unit.get(key) or 0.0
        if macro <= 0:
            continue
        score = macro / max(kcal, 1.0)
        if score > best_score:
            best_score = score
            best = food
    return best


def _load_foods_map(limit: int = 500) -> Dict[int, Dict[str, Any]]:
    foods = list_foods(limit=limit)
    return {int(food["id"]): dict(food) for food in foods if food.get("id") is not None}


def _normalize_meal_ingredients(raw: Any, foods_by_id: Dict[int, Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[int]]:
    if not isinstance(raw, list):
        return [], []
    ingredients = []
    missing = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        fid = item.get("food_id")
        if fid is None:
            continue
        try:
            fid_int = int(fid)
        except Exception:
            continue
        food = foods_by_id.get(fid_int)
        if not food:
            missing.append(fid_int)
            continue
        amount = item.get("amount")
        try:
            amount_value = float(amount)
        except Exception:
            continue
        if amount_value <= 0:
            continue
        unit = (item.get("unit") or food.get("unit_default") or "g").strip()
        if not unit:
            unit = "g"
        ingredients.append({"food_id": fid_int, "amount": amount_value, "unit": unit})
    return ingredients, sorted(set(missing))


def _build_meal_payload(
    body: Dict[str, Any],
    foods_by_id: Dict[int, Dict[str, Any]],
    require_title: bool = True,
    require_ingredients: bool = True,
) -> Tuple[Optional[Dict[str, Any]], List[int], Optional[str]]:
    title_raw = body.get("title")
    title = (title_raw or "").strip() if title_raw is not None else None
    if require_title and not title:
        return None, [], "title_required"
    ingredients = None
    missing: List[int] = []
    if "ingredients" in body:
        normalized, missing = _normalize_meal_ingredients(body.get("ingredients"), foods_by_id)
        if normalized:
            ingredients = normalized
        elif require_ingredients:
            return None, missing, "ingredients_required"
    elif require_ingredients:
        return None, missing, "ingredients_required"
    payload: Dict[str, Any] = {}
    if title is not None:
        payload["title"] = title
    if ingredients is not None:
        payload["ingredients"] = ingredients
    if "description" in body:
        payload["description"] = body.get("description")
    if "tags" in body:
        payload["tags"] = body.get("tags")
    if "notes" in body:
        payload["notes"] = body.get("notes")
    if "mfp_alias" in body:
        payload["mfp_alias"] = body.get("mfp_alias")
    if "default_servings" in body:
        ds = _to_float(body.get("default_servings"))
        if ds is not None and ds > 0:
            payload["default_servings"] = ds
    if not payload:
        return None, missing, "no_changes"
    return payload, missing, None


def _build_day_ingredients(foods: List[Dict[str, Any]], targets: Dict[str, float]) -> List[Dict[str, Any]]:
    remaining = {
        "kcal": float(targets.get("kcal") or 0.0),
        "p": float(targets.get("p") or 0.0),
        "c": float(targets.get("c") or 0.0),
        "f": float(targets.get("f") or 0.0),
    }
    amounts: Dict[int, Dict[str, Any]] = {}

    for macro in ("p", "c", "f"):
        candidate = _pick_best_food(foods, macro)
        if not candidate:
            continue
        per_unit = candidate["per_unit"]
        if per_unit.get(macro, 0.0) <= 0:
            continue
        desired = max(0.0, remaining[macro]) * 0.85
        if desired <= 0:
            continue
        raw_amount = desired / per_unit[macro]
        unit = candidate["unit"]
        if unit in ("pcs", "piece", "pieces", "stück"):
            raw_amount = min(max(raw_amount, 0.5), 6.0)
        else:
            raw_amount = min(max(raw_amount, 20.0), 600.0)
        amount = _round_amount(raw_amount, unit)
        if amount <= 0:
            continue
        entry = amounts.get(candidate["id"])
        if entry:
            entry["amount"] += amount
        else:
            amounts[candidate["id"]] = {"food_id": candidate["id"], "amount": amount, "unit": unit}
        remaining["kcal"] -= per_unit["kcal"] * amount
        remaining["p"] -= per_unit["p"] * amount
        remaining["c"] -= per_unit["c"] * amount
        remaining["f"] -= per_unit["f"] * amount

    if remaining["kcal"] > 0:
        energy_food = max(foods, key=lambda f: f["per_unit"].get("kcal") or 0.0, default=None)
        if energy_food and (energy_food["per_unit"].get("kcal") or 0.0) > 0:
            unit = energy_food["unit"]
            raw_amount = remaining["kcal"] / energy_food["per_unit"]["kcal"]
            if unit in ("pcs", "piece", "pieces", "stück"):
                raw_amount = min(max(raw_amount, 0.5), 6.0)
            else:
                raw_amount = min(max(raw_amount, 20.0), 600.0)
            amount = _round_amount(raw_amount, unit)
            if amount > 0:
                entry = amounts.get(energy_food["id"])
                if entry:
                    entry["amount"] += amount
                else:
                    amounts[energy_food["id"]] = {"food_id": energy_food["id"], "amount": amount, "unit": unit}

    ingredients = list(amounts.values())
    if not ingredients:
        return []

    if targets.get("kcal"):
        total_kcal = 0.0
        for item in ingredients:
            food = next((f for f in foods if f["id"] == item["food_id"]), None)
            if not food:
                continue
            total_kcal += (food["per_unit"].get("kcal") or 0.0) * item["amount"]
        if total_kcal > 0 and total_kcal > targets["kcal"] * 1.1:
            scale = max(0.5, min(1.0, targets["kcal"] / total_kcal))
            for item in ingredients:
                item["amount"] = _round_amount(item["amount"] * scale, item["unit"])
    return [item for item in ingredients if item["amount"] > 0]


def _ingredients_totals(foods: List[Dict[str, Any]], ingredients: List[Dict[str, Any]]) -> Dict[str, float]:
    totals = {"kcal": 0.0, "p": 0.0, "c": 0.0, "f": 0.0}
    foods_by_id = {f["id"]: f for f in foods}
    for item in ingredients:
        food = foods_by_id.get(int(item.get("food_id") or 0))
        if not food:
            continue
        per_unit = food["per_unit"]
        amt = float(item.get("amount") or 0.0)
        totals["kcal"] += (per_unit.get("kcal") or 0.0) * amt
        totals["p"] += (per_unit.get("p") or 0.0) * amt
        totals["c"] += (per_unit.get("c") or 0.0) * amt
        totals["f"] += (per_unit.get("f") or 0.0) * amt
    return {k: round(v, 1) for k, v in totals.items()}


# ------------------------------------------------------------
# Allowed exercises catalog (from your DB)
# ------------------------------------------------------------

_ALLOWED_CACHE: Dict[str, List[str]] | None = None
_ALLOWED_CACHE_TS: float = 0.0

def _allowed_exercises_map(ttl_s: float = 30.0) -> Dict[str, List[str]]:
    """
    Returns: { "Bench": ["LH","KH",...], ... }
    Cached for ttl_s seconds.
    """
    global _ALLOWED_CACHE, _ALLOWED_CACHE_TS
    now = datetime.now().timestamp()
    if _ALLOWED_CACHE is not None and (now - _ALLOWED_CACHE_TS) < ttl_s:
        return _ALLOWED_CACHE

    conn = _connect(TRAINING_DB)
    rows = conn.execute("""
        SELECT name, device
        FROM exercises
        WHERE name IS NOT NULL AND TRIM(name) != ''
        GROUP BY name, device
    """).fetchall()
    conn.close()

    m: Dict[str, set] = {}
    for r in rows:
        n = (r["name"] or "").strip()
        d = (r["device"] or "").strip()
        if not n:
            continue
        if n not in m:
            m[n] = set()
        if d:
            m[n].add(d)

    out = {k: sorted(list(v)) for k, v in m.items()}
    _ALLOWED_CACHE = out
    _ALLOWED_CACHE_TS = now
    return out


# ------------------------------------------------------------
# Plan normalization (keep UI stable)
# ------------------------------------------------------------

def _normalize_plan(plan: Dict[str, Any], name: str, block_length: int) -> Dict[str, Any]:
    """
    Only ensures required keys exist and adds uid fields.
    Does NOT enforce a specific split or exercise selection besides validation.
    """
    if not isinstance(plan, dict):
        plan = {}

    plan.setdefault("name", name)
    plan.setdefault("block_length", block_length)
    plan.setdefault("meta", {"goal": "", "priorities": []})
    plan.setdefault("base_week", [])
    plan.setdefault("weeks", [])

    if not isinstance(plan["base_week"], list):
        plan["base_week"] = []
    if not isinstance(plan["weeks"], list):
        plan["weeks"] = []

    # base_week days
    for d in plan["base_week"]:
        if not isinstance(d, dict):
            continue
        d.setdefault("day", "")
        d.setdefault("session_name", "")
        d.setdefault("strength_exercises", [])
        d.setdefault("run_sessions", [])

        if not isinstance(d["strength_exercises"], list):
            d["strength_exercises"] = []
        if not isinstance(d["run_sessions"], list):
            d["run_sessions"] = []

        for ex in d["strength_exercises"]:
            if not isinstance(ex, dict):
                continue
            ex.setdefault("uid", _uid())
            ex.setdefault("exercise_name", "")
            ex.setdefault("device", "")
            ex.setdefault("variation", "")
            ex.setdefault("sets", 0)
            ex.setdefault("reps_min", 0)
            ex.setdefault("reps_max", 0)
            ex.setdefault("rpe_min", 0)
            ex.setdefault("rpe_max", 0)
            ex.setdefault("notes", "")

        for run in d["run_sessions"]:
            if not isinstance(run, dict):
                continue
            run.setdefault("uid", _uid())
            run.setdefault("run_type", "")
            run.setdefault("amount_value", "")
            run.setdefault("amount_unit", "")
            run.setdefault("pace", "")
            run.setdefault("interval_reps", "")
            run.setdefault("interval_on", "")
            run.setdefault("interval_on_unit", "min")
            run.setdefault("interval_off", "")
            run.setdefault("interval_off_unit", "min")
            run.setdefault("notes", "")

    # weeks: if empty -> create minimal weeks so UI has something
    if len(plan["weeks"]) == 0:
        for i in range(1, block_length + 1):
            plan["weeks"].append({
                "week": i,
                "strength_volume_factor": 1.0,
                "run_volume_factor": 1.0,
                "deload": False,
                "rpe_cap": None,
                "week_note": "",
            })
    else:
        for i, w in enumerate(plan["weeks"], start=1):
            if not isinstance(w, dict):
                continue
            w.setdefault("week", i)
            w.setdefault("strength_volume_factor", 1.0)
            w.setdefault("run_volume_factor", 1.0)
            w.setdefault("deload", False)
            w.setdefault("rpe_cap", None)
            w.setdefault("week_note", "")

    return plan


# ------------------------------------------------------------
# Validation (only hard rule: exercises must exist in DB)
# ------------------------------------------------------------

def _validate_plan(plan: Dict[str, Any]) -> Tuple[bool, str]:
    if not isinstance(plan.get("base_week"), list):
        return False, "plan.base_week must be a list"

    allowed = _allowed_exercises_map()

    for day in plan["base_week"]:
        if not isinstance(day, dict):
            continue

        se = day.get("strength_exercises") or []
        if se is None:
            se = []
        if not isinstance(se, list):
            return False, "strength_exercises must be a list"

        for ex in se:
            if not isinstance(ex, dict):
                continue
            name = (ex.get("exercise_name") or "").strip()
            dev = (ex.get("device") or "").strip()

            if not name:
                return False, "exercise_name missing"
            if name not in allowed:
                if not AI_ALLOW_UNKNOWN_EXERCISES:
                    return False, f"Unknown exercise: {name}"
                # free mode -> akzeptieren
                continue


            # device check: only if GPT provides one
            if dev:
                allowed_devs = allowed.get(name) or []
                # if we have devices for that exercise, enforce match
                if allowed_devs and dev not in allowed_devs:
                    return False, f"Invalid device '{dev}' for '{name}'. Allowed: {allowed_devs}"

    return True, ""


# ------------------------------------------------------------
# Routes
# ------------------------------------------------------------

@ai_write_api.post("/plans")
@require_ai_write
@require_intent("plan_write")
def create_plan():
    _ensure_schema()
    body = request.get_json(force=True) or {}

    # Accept either:
    # A) { name, block_length, set_active, plan: {...} }
    # B) directly a plan object (contains base_week/weeks at top-level)
    plan = body.get("plan")
    if not isinstance(plan, dict) and isinstance(body.get("base_week"), list):
        plan = body  # body IS the plan

    if not isinstance(plan, dict):
        return jsonify({"ok": False, "error": "invalid_request", "detail": "Missing plan (provide body.plan or a plan object)."}), 400

    name = (body.get("name") or plan.get("name") or "AI Plan").strip()
    block_length = _to_int(body.get("block_length") or plan.get("block_length"), 4)
    set_active = _bool(body.get("set_active", True))

    plan = _normalize_plan(plan, name=name, block_length=block_length)

    ok, err = _validate_plan(plan)
    if not ok:
        return jsonify({"ok": False, "error": "invalid_plan", "detail": err}), 400

    now = _now_iso()
    data_str = json.dumps(plan, ensure_ascii=False)

    conn = _connect(PLANS_DB)
    cur = conn.cursor()

    try:
        cur.execute("BEGIN")
        if set_active:
            cur.execute("UPDATE plans SET is_active=0")
        cur.execute(
            "INSERT INTO plans (name, block_length, is_active, data, created_at, updated_at) VALUES (?,?,?,?,?,?)",
            (name, block_length, 1 if set_active else 0, data_str, now, now),
        )
        plan_id = cur.lastrowid
        conn.commit()
    except Exception as e:
        conn.rollback()
        conn.close()
        return jsonify({"ok": False, "error": "create_failed", "detail": str(e)}), 500

    conn.close()
    return jsonify({"ok": True, "id": plan_id})


@ai_write_api.get("/catalog/exercises")
def catalog_exercises():
    allowed = _allowed_exercises_map()
    return jsonify({
        "ok": True,
        "exercises": [{"name": name, "devices": devices} for name, devices in sorted(allowed.items())]
    })


@ai_write_api.put("/plans/<int:plan_id>")
@require_ai_write
@require_intent("plan_write")
def update_plan(plan_id: int):
    _ensure_schema()
    body = request.get_json(force=True) or {}

    conn = _connect(PLANS_DB)
    cur = conn.cursor()

    row = cur.execute("SELECT id, name, block_length, data FROM plans WHERE id=?", (plan_id,)).fetchone()
    if not row:
        conn.close()
        return jsonify({"ok": False, "error": "not_found"}), 404

    try:
        current = json.loads(row["data"]) if row["data"] else {}
    except Exception:
        current = {}

    # Accept either a full replacement plan or partial updates
    incoming_plan = body.get("plan")
    if not isinstance(incoming_plan, dict) and isinstance(body.get("base_week"), list):
        incoming_plan = body

    name = (body.get("name") or incoming_plan.get("name") if isinstance(incoming_plan, dict) else row["name"] or "").strip() or row["name"]
    block_length = _to_int(body.get("block_length") or (incoming_plan.get("block_length") if isinstance(incoming_plan, dict) else None) or row["block_length"], int(row["block_length"] or 4))
    set_active = body.get("set_active", None)

    if isinstance(incoming_plan, dict):
        # Replace
        new_plan = incoming_plan
    else:
        # Merge minimal
        new_plan = current
        if body.get("base_week") is not None:
            new_plan["base_week"] = body.get("base_week")
        if body.get("weeks") is not None:
            new_plan["weeks"] = body.get("weeks")

    new_plan = _normalize_plan(new_plan, name=name, block_length=block_length)

    ok, err = _validate_plan(new_plan)
    if not ok:
        conn.close()
        return jsonify({"ok": False, "error": "invalid_plan", "detail": err}), 400

    now = _now_iso()

    try:
        cur.execute("BEGIN")

        if set_active is not None:
            sa = 1 if _bool(set_active) else 0
            if sa == 1:
                cur.execute("UPDATE plans SET is_active=0")
            cur.execute("UPDATE plans SET is_active=? WHERE id=?", (sa, plan_id))

        cur.execute(
            "UPDATE plans SET name=?, block_length=?, data=?, updated_at=? WHERE id=?",
            (name, int(block_length), json.dumps(new_plan, ensure_ascii=False), now, plan_id),
        )

        conn.commit()
    except Exception as e:
        conn.rollback()
        conn.close()
        return jsonify({"ok": False, "error": "update_failed", "detail": str(e)}), 500

    conn.close()
    return jsonify({"ok": True, "id": plan_id})


@ai_write_api.post("/plans/<int:plan_id>/set_active")
@require_ai_write
@require_intent("plan_write")
def set_active(plan_id: int):
    _ensure_schema()
    body = request.get_json(silent=True) or {}
    active = _bool(body.get("active", True))

    conn = _connect(PLANS_DB)
    cur = conn.cursor()

    row = cur.execute("SELECT id FROM plans WHERE id=?", (plan_id,)).fetchone()
    if not row:
        conn.close()
        return jsonify({"ok": False, "error": "not_found"}), 404

    now = _now_iso()

    try:
        cur.execute("BEGIN")
        if active:
            cur.execute("UPDATE plans SET is_active=0")
            cur.execute("UPDATE plans SET is_active=1, updated_at=? WHERE id=?", (now, plan_id))
        else:
            cur.execute("UPDATE plans SET is_active=0, updated_at=? WHERE id=?", (now, plan_id))
        conn.commit()
    except Exception as e:
        conn.rollback()
        conn.close()
        return jsonify({"ok": False, "error": "set_active_failed", "detail": str(e)}), 500

    conn.close()
    return jsonify({"ok": True, "id": plan_id, "active": active})


@ai_write_api.post("/nutrition/meals")
@require_ai_write
@require_intent("nutrition_plan_write")
def create_nutrition_meal_template_api():
    body = request.get_json(force=True) or {}
    foods_by_id = _load_foods_map()
    payload, missing, err = _build_meal_payload(body, foods_by_id, require_title=True, require_ingredients=True)
    if err:
        return jsonify({"ok": False, "error": err, "missing_food_ids": missing}), 400
    meal, err = create_meal_template(payload)
    if err:
        return jsonify({"ok": False, "error": err}), 400
    return jsonify({"ok": True, "meal_template": meal, "missing_food_ids": missing})


@ai_write_api.put("/nutrition/meals/<int:meal_id>")
@require_ai_write
@require_intent("nutrition_plan_write")
def update_nutrition_meal_template_api(meal_id: int):
    body = request.get_json(force=True) or {}
    foods_by_id = _load_foods_map()
    payload, missing, err = _build_meal_payload(body, foods_by_id, require_title=False, require_ingredients=False)
    if err:
        return jsonify({"ok": False, "error": err, "missing_food_ids": missing}), 400
    meal, err = update_meal_template(meal_id, payload)
    if err:
        status = 404 if err == "not_found" else 400
        return jsonify({"ok": False, "error": err}), status
    return jsonify({"ok": True, "meal_template": meal, "missing_food_ids": missing})


@ai_write_api.get("/plans/<int:plan_id>")
@require_ai_write
def get_plan(plan_id: int):
    _ensure_schema()
    conn = _connect(PLANS_DB)
    cur = conn.cursor()
    row = cur.execute(
        "SELECT id, name, block_length, is_active, data, updated_at, created_at FROM plans WHERE id=?",
        (plan_id,),
    ).fetchone()
    conn.close()

    if not row:
        return jsonify({"ok": False, "error": "not_found"}), 404

    try:
        plan = json.loads(row["data"]) if row["data"] else {}
    except Exception:
        plan = {}

    return jsonify({
        "ok": True,
        "id": row["id"],
        "name": row["name"],
        "block_length": row["block_length"],
        "is_active": bool(row["is_active"]),
        "updated_at": row["updated_at"],
        "created_at": row["created_at"],
        "plan": plan,
    })
