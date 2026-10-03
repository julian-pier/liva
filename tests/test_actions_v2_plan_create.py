import json
import sqlite3

import database.connections as connections
import nutrition.nutrition_planning_db as nutrition_db
from tests.test_actions_v2_api import auth, client


def _ensure_test_planning_schema():
    if hasattr(nutrition_db, "_SCHEMA_READY"):
        nutrition_db._SCHEMA_READY = False
    nutrition_db.ensure_nutrition_planning_schema()


def _seed_test_food(name="Brot"):
    _ensure_test_planning_schema()
    conn = connections.get_nutrition_db()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS nutrition_foods (
          id INTEGER PRIMARY KEY,
          name TEXT NOT NULL,
          brand TEXT,
          unit_default TEXT,
          portion_g REAL,
          kcal_per_100 REAL,
          p_per_100 REAL,
          c_per_100 REAL,
          f_per_100 REAL,
          fiber_per_100 REAL,
          salt_per_100 REAL,
          tags TEXT,
          sugar_per_100 REAL,
          common_portion_size REAL,
          mfp_search_hint TEXT,
          category TEXT,
          is_favorite INTEGER DEFAULT 0,
          is_active INTEGER DEFAULT 1,
          name_normalized TEXT,
          created_at TEXT,
          updated_at TEXT
        );
        """
    )
    cur = conn.cursor()
    cols = {row[1] for row in cur.execute("PRAGMA table_info(nutrition_foods)").fetchall()}
    normalized_name = name.strip().lower()
    if "name_normalized" in cols:
        existing = cur.execute("SELECT id FROM nutrition_foods WHERE name_normalized=? LIMIT 1", (normalized_name,)).fetchone()
        if existing:
            conn.close()
            return int(existing[0])
    else:
        existing = cur.execute("SELECT id FROM nutrition_foods WHERE LOWER(name)=? LIMIT 1", (normalized_name,)).fetchone()
        if existing:
            conn.close()
            return int(existing[0])
    now = "2026-05-24T08:00:00Z"
    values = {
        "name": name,
        "brand": None,
        "unit_default": "g",
        "portion_g": 100.0,
        "kcal_per_100": 250.0,
        "p_per_100": 8.0,
        "c_per_100": 45.0,
        "f_per_100": 3.0,
        "fiber_per_100": None,
        "salt_per_100": None,
        "tags": None,
        "sugar_per_100": 0.0,
        "common_portion_size": 100.0,
        "mfp_search_hint": None,
        "category": None,
        "is_favorite": 0,
        "is_active": 1,
        "name_normalized": normalized_name,
        "created_at": now,
        "updated_at": now,
    }
    insert_cols = [col for col in values if col in cols]
    placeholders = ", ".join("?" for _ in insert_cols)
    cur.execute(
        f"INSERT INTO nutrition_foods ({', '.join(insert_cols)}) VALUES ({placeholders})",
        tuple(values[col] for col in insert_cols),
    )
    food_id = int(cur.lastrowid)
    conn.commit()
    conn.close()
    return food_id


def _meal_template_row(title: str):
    conn = connections.get_nutrition_db()
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT id, title FROM nutrition_meal_templates WHERE title=? ORDER BY id DESC LIMIT 1", (title,)).fetchone()
    conn.close()
    return dict(row) if row else None


def test_actliva_training_create_plan_requires_confirm(client):
    resp = client.post(
        "/api/v2/actions/liva/act",
        json={
            "domain": "training",
            "command": "create_training_plan",
            "payload": {
                "name": "Hybrid Build 6W",
                "block_length": 6,
                "set_active": True,
                "days": [{"day": "Mo", "events": [{"kind": "gym", "title": "Push", "items": [{"kind": "exercise", "name": "Schrägbankdrücken", "variation": "Smith", "sets": 3, "reps": {"min": 6, "max": 10}}]}]}],
                "weeks": [{"week": 1, "phase": "Build"}],
            },
        },
        headers=auth(),
    )
    body = resp.get_json()
    assert resp.status_code == 400
    assert body["error"]["code"] == "confirmation_required"


def test_actliva_training_create_plan_dry_run_writes_nothing(client):
    before = client.get("/api/v2/actions/training/plan-state?detail=full", headers=auth()).get_json()["plan"]["active_version"]
    resp = client.post(
        "/api/v2/actions/liva/act",
        json={
            "domain": "training",
            "command": "create_training_plan",
            "dry_run": True,
            "payload": {
                "name": "Hybrid Build 6W",
                "block_length": 6,
                "set_active": True,
                "days": [{"day": "Mo", "events": [{"kind": "gym", "title": "Push", "time": "18:30", "items": [{"kind": "exercise", "name": "Schrägbankdrücken", "variation": "Smith", "sets": 3, "reps": {"min": 6, "max": 10}, "rpe_list": [8, 8, 9]}]}]}],
                "weeks": [{"week": 1, "phase": "Build"}],
            },
        },
        headers=auth(),
    )
    body = resp.get_json()
    after = client.get("/api/v2/actions/training/plan-state?detail=full", headers=auth()).get_json()["plan"]["active_version"]
    assert resp.status_code == 200
    assert body["execution"]["mode"] == "dry_run"
    assert body["execution"]["live_state_changed"] is False
    assert body["result"]["plan_name"] == "Hybrid Build 6W"
    assert before == after


def test_actliva_training_create_plan_dry_run_accepts_value_days_on_v2_facade(client):
    resp = client.post(
        "/api/v2/actions/liva/act",
        json={
            "domain": "training",
            "command": "create_training_plan",
            "dry_run": True,
            "confirm": False,
            "name": "Test Plan",
            "value": {
                "days": [
                    {
                        "day": "Mo",
                        "events": [
                            {
                                "kind": "gym",
                                "title": "Test",
                                "items": [{"kind": "exercise", "name": "Test", "sets": 1, "reps": 10}],
                            }
                        ],
                    }
                ]
            },
        },
        headers=auth(),
    )
    body = resp.get_json()
    assert resp.status_code == 200
    assert body["ok"] is True
    assert body["execution"]["mode"] == "dry_run"
    assert body["execution"]["live_state_changed"] is False
    assert body["result"]["normalized_plan"]["name"] == "Test Plan"
    assert body["result"]["set_active"] is False


def test_actliva_training_create_plan_alias_accepts_value_days_payload(client):
    resp = client.post(
        "/actions/liva/act",
        json={
            "domain": "training",
            "command": "create_training_plan",
            "dry_run": True,
            "confirm": False,
            "name": "Alias Test Plan",
            "value": {
                "days": [
                    {
                        "day": "Mo",
                        "events": [
                            {
                                "kind": "gym",
                                "title": "Test",
                                "items": [{"kind": "exercise", "name": "Test", "sets": 1, "reps": 10}],
                            }
                        ],
                    }
                ]
            },
        },
        headers=auth(),
    )
    body = resp.get_json()
    assert resp.status_code == 200
    assert body["ok"] is True
    assert body["execution"]["mode"] == "dry_run"
    assert body["result"]["normalized_plan"]["name"] == "Alias Test Plan"


def test_actliva_training_create_plan_dry_run_counts_ergo_as_cardio_and_formats_items(client):
    resp = client.post(
        "/api/v2/actions/liva/act",
        json={
            "domain": "training",
            "command": "create_training_plan",
            "dry_run": True,
            "payload": {
                "name": "Hybrid Build Ergo",
                "block_length": 4,
                "days": [
                    {
                        "day": "Mo",
                        "events": [
                            {
                                "kind": "ergo",
                                "title": "Ergo – Z2",
                                "time": "19:45",
                                "items": [
                                    {
                                        "kind": "cardio",
                                        "name": "Ergo – Z2",
                                        "duration_min": 30,
                                        "intensity": "locker",
                                        "note": "nasal",
                                    }
                                ],
                            }
                        ],
                    }
                ],
            },
        },
        headers=auth(),
    )
    body = resp.get_json()
    assert resp.status_code == 200
    assert body["result"]["run_days"] == 0
    assert body["result"]["cardio_days"] == 1
    assert body["result"]["cardio_events"] == 1
    assert body["result"]["cardio_items"] == 1
    item = body["result"]["normalized_plan"]["base_week"]["Mo"][0]["items"][0]
    assert body["result"]["normalized_plan"]["base_week"]["Mo"][0]["kind"] == "cardio"
    assert body["result"]["normalized_plan"]["base_week"]["Mo"][0]["mode"] == "ergo"
    assert item["kind"] == "cardio"
    assert item["mode"] == "ergo"
    assert item["amount_value"] == 30
    assert item["amount_unit"] == "min"
    assert item["notes"] == "nasal"
    assert item["display"] == "30 min locker @ nasal"


def test_actliva_training_create_plan_live_is_visible_in_readliva(client):
    resp = client.post(
        "/api/v2/actions/liva/act",
        json={
            "domain": "training",
            "command": "create_training_plan",
            "confirm": True,
            "payload": {
                "name": "Hybrid Build 6W",
                "block_length": 6,
                "set_active": True,
                "focus": "Hybrid",
                "days": [
                    {"day": "Mo", "events": [{"kind": "gym", "title": "Push", "time": "18:30", "items": [{"kind": "exercise", "name": "Schrägbankdrücken", "variation": "Smith", "sets": 3, "reps": {"min": 6, "max": 10}, "rpe_list": [8, 8, 9]}]}]},
                    {"day": "Sa", "events": [{"kind": "run", "title": "Run - Long", "time": "18:00", "items": [], "notes": "locker"}]},
                ],
                "weeks": [{"week": 1, "phase": "Build", "rpe_cap": 9}],
            },
        },
        headers=auth(),
    )
    body = resp.get_json()
    plan_read = client.post("/api/v2/actions/liva/read", json={"mode": "training_plan", "detail": "full"}, headers=auth()).get_json()["result"]["plan"]
    assert resp.status_code == 200
    assert body["execution"]["mode"] == "applied_live"
    assert body["execution"]["live_state_changed"] is True
    assert body["result"]["source"] == "gym_plans"
    assert body["result"]["plan_name"] == "Hybrid Build 6W"
    assert plan_read["active_version"] == "Hybrid Build 6W"
    assert "Mo" in plan_read["days"]
    assert "Sa" in plan_read["days"]


def test_actliva_training_create_plan_live_readback_keeps_cardio_visible(client):
    resp = client.post(
        "/api/v2/actions/liva/act",
        json={
            "domain": "training",
            "command": "create_training_plan",
            "confirm": True,
            "payload": {
                "name": "Hybrid Build Cardio",
                "set_active": True,
                "days": [
                    {
                        "day": "Mo",
                        "events": [
                            {
                                "kind": "gym",
                                "title": "Push",
                                "items": [{"kind": "exercise", "name": "Bankdrücken", "sets": 3, "reps": {"min": 6, "max": 8}, "rpe_list": [8, 8, 9]}],
                            }
                        ],
                    },
                    {
                        "day": "Sa",
                        "events": [
                            {
                                "kind": "ergo",
                                "title": "Ergo – Z2",
                                "time": "19:45",
                                "items": [{"kind": "cardio", "name": "Ergo – Z2", "duration_min": 30, "intensity": "locker", "note": "nasal"}],
                            }
                        ],
                    },
                ],
            },
        },
        headers=auth(),
    )
    body = resp.get_json()
    readback = client.get("/api/v2/actions/training/plan-state?detail=full", headers=auth()).get_json()["plan"]
    assert resp.status_code == 200
    assert body["result"]["strength_days"] == 1
    assert body["result"]["cardio_days"] == 1
    assert body["result"]["cardio_events"] == 1
    assert body["result"]["cardio_items"] == 1
    assert body["result"]["run_days"] == 0
    sa_event = readback["days"]["Sa"]["events"][0]
    assert sa_event["kind"] == "cardio"
    assert sa_event["mode"] == "ergo"
    assert sa_event["items"][0]["display"] == "30 min locker @ nasal"
    assert sa_event["items"][0]["display"] != "—"
    assert readback["days"]["Mo"]["exercises"][0]["display_name"] == "Bankdrücken"


def test_actliva_nutrition_create_plan_dry_run_known_food_ids(client):
    food_id = _seed_test_food()
    resp = client.post(
        "/api/v2/actions/liva/act",
        json={
            "domain": "nutrition",
            "command": "create_nutrition_plan",
            "dry_run": True,
            "payload": {
                "name": "Standard Cut Woche",
                "set_active": True,
                "days": [
                    {
                        "day": "Mo",
                        "meals": [
                            {
                                "time": "06:40",
                                "title": "Breakfast",
                                "items": [{"food_id": food_id, "amount": 100, "unit": "g"}],
                            }
                        ],
                    }
                ],
            },
        },
        headers=auth(),
    )
    body = resp.get_json()
    assert resp.status_code == 200
    assert body["execution"]["mode"] == "dry_run"
    assert body["result"]["unresolved_items"] == []


def test_actliva_nutrition_create_plan_unknown_food_returns_unresolved_and_no_write(client):
    before = client.post("/api/v2/actions/nutrition/data", json={"mode": "planned_meals", "date": "2026-04-16"}, headers=auth()).get_json()
    resp = client.post(
        "/api/v2/actions/liva/act",
        json={
            "domain": "nutrition",
            "command": "create_nutrition_plan",
            "dry_run": True,
            "payload": {
                "name": "Unknown Foods",
                "days": [{"day": "Mo", "meals": [{"title": "Bad Meal", "items": [{"food_name": "NichtExistierendesFood", "amount": 1, "unit": "g"}]}]}],
            },
        },
        headers=auth(),
    )
    after = client.post("/api/v2/actions/nutrition/data", json={"mode": "planned_meals", "date": "2026-04-16"}, headers=auth()).get_json()
    body = resp.get_json()
    assert resp.status_code == 200
    assert body["result"]["unresolved_items"]
    assert body["result"]["unresolved_items"][0]["reason"] in {"not_found", "ambiguous"}
    assert before == after


def test_actliva_nutrition_create_plan_live_writes_templates_slots_and_preserves_active_when_set_inactive(client):
    food_bread = _seed_test_food("Brot")
    food_skyr = _seed_test_food("Skyr")
    _ensure_test_planning_schema()
    previous_active, err = nutrition_db.create_week_template("Mini-Cut", set_active=True)
    assert err is None
    resp = client.post(
        "/api/v2/actions/liva/act",
        json={
            "domain": "nutrition",
            "command": "create_nutrition_plan",
            "confirm": True,
            "payload": {
                "name": "GPT Nutrition Smoke Test",
                "set_active": False,
                "days": [
                    {
                        "day": "Mo",
                        "meals": [
                            {
                                "time": "06:40",
                                "title": "Brot + Skyr",
                                "items": [
                                    {"food_id": food_bread, "amount": 1, "unit": "pcs"},
                                    {"food_id": food_skyr, "amount": 200, "unit": "g"},
                                ],
                            }
                        ],
                    }
                ],
            },
        },
        headers=auth(),
    )
    body = resp.get_json()
    conn = connections.get_nutrition_db()
    conn.row_factory = sqlite3.Row
    meal_template = conn.execute("SELECT id, title FROM nutrition_meal_templates WHERE title=? ORDER BY id DESC LIMIT 1", ("Brot + Skyr",)).fetchone()
    assert meal_template is not None
    ingredients = conn.execute("SELECT food_id, amount, unit FROM nutrition_meal_ingredients WHERE meal_template_id=? ORDER BY sort_index, id", (meal_template["id"],)).fetchall()
    template_id = int(body["result"]["template_id"])
    active_row = conn.execute("SELECT id, title FROM nutrition_week_templates WHERE is_active=1 ORDER BY id DESC LIMIT 1").fetchone()
    monday_day = conn.execute(
        "SELECT id FROM nutrition_week_template_days WHERE week_template_id=? AND weekday=0 LIMIT 1",
        (template_id,),
    ).fetchone()
    assert monday_day is not None
    slot = conn.execute(
        "SELECT week_template_day_id, slot_index, time_text, meal_template_id, custom_title FROM nutrition_week_day_slots WHERE week_template_day_id=? ORDER BY slot_index LIMIT 1",
        (int(monday_day["id"]),),
    ).fetchone()
    conn.close()
    assert resp.status_code == 200
    assert body["execution"]["mode"] == "applied_live"
    assert body["execution"]["live_state_changed"] is True
    assert body["result"]["template_id"] is not None
    assert body["result"]["meals_count"] == 1
    assert body["result"]["slots_count"] == 1
    assert slot is not None
    assert int(slot["meal_template_id"]) == int(meal_template["id"])
    assert str(slot["time_text"]) == "06:40"
    assert str(slot["custom_title"]) == "Brot + Skyr"
    assert len(ingredients) == 2
    assert {int(row["food_id"]) for row in ingredients} == {food_bread, food_skyr}
    created = body["result"]["created_template_after"]
    assert int(created["id"]) == template_id
    assert created["title"] == "GPT Nutrition Smoke Test"
    assert created["is_active"] is False
    for forbidden in ("foods", "meal_templates", "slot_templates", "meal_ingredients_map", "gap_notes", "macro_settings", "week_template", "week_summary"):
        assert forbidden not in created
    monday = next(day for day in created["days"] if day["label"] == "Mo")
    assert len(monday["slots"]) == 1
    assert monday["slots"][0]["meal_title"] == "Brot + Skyr"
    assert monday["slots"][0]["items"][0]["food_id"] == food_bread
    assert monday["slots"][0]["items"][1]["food_id"] == food_skyr
    assert created["summary"]["days_count"] == 7
    assert created["summary"]["filled_days_count"] == 1
    assert created["summary"]["slots_count"] == 1
    assert created["summary"]["meals_count"] == 1
    assert active_row is not None
    assert int(active_row["id"]) == int(previous_active["id"])
    assert body["result"].get("active_plan_after") is None


def test_training_adjustments_still_work_after_plan_create(client):
    create_resp = client.post(
        "/api/v2/actions/liva/act",
        json={
            "domain": "training",
            "command": "create_training_plan",
            "confirm": True,
            "payload": {
                "name": "Mutable Plan",
                "set_active": True,
                "days": [{"day": "Mo", "events": [{"kind": "gym", "title": "Push", "items": [{"kind": "exercise", "name": "Schrägbankdrücken", "variation": "Smith", "sets": 2, "reps": {"min": 6, "max": 10}, "rpe_list": [8, 9]}]}]}],
                "weeks": [{"week": 1, "phase": "Build"}],
            },
        },
        headers=auth(),
    )
    assert create_resp.status_code == 200
    adjust = client.post(
        "/api/v2/actions/training/adjust",
        json={"command": "change_sets", "day": "Mo", "exercise": "Schrägbankdrücken (Smith)", "sets": 4},
        headers=auth(),
    )
    body = adjust.get_json()
    plan = client.get("/api/v2/actions/training/plan-state?detail=full", headers=auth()).get_json()["plan"]
    assert adjust.status_code == 200
    assert body["execution"]["live_state_changed"] is True
    assert plan["days"]["Mo"]["exercises"][0]["sets"] == 4


def test_nutrition_adjust_targets_still_works(client):
    resp = client.post(
        "/api/v2/actions/nutrition/control",
        json={"command": "adjust_targets", "kcal": 2650, "protein": 180, "carbs": 300, "fat": 65},
        headers=auth(),
    )
    body = resp.get_json()
    assert resp.status_code == 200
    assert body["execution"]["mode"] == "applied_live"


def test_nutrition_create_food_supports_piece_macros(client):
    resp = client.post(
        "/api/v2/actions/nutrition/control",
        json={
            "command": "create_food",
            "name": "Test Proteinriegel",
            "brand": "LIVA Test",
            "unit": "pcs",
            "kcal": 210,
            "protein": 20,
            "carbs": 18,
            "fat": 7,
        },
        headers=auth(),
    )
    body = resp.get_json()

    assert resp.status_code == 200
    assert body["execution"]["live_state_changed"] is True
    food = body["result"]["food"]
    assert food["unit_default"] == "pcs"
    assert food["kcal_per_100"] == 210
    assert food["p_per_100"] == 20


def test_nutrition_create_food_dry_run_normalizes_gram_portion(client):
    resp = client.post(
        "/api/v2/actions/nutrition/control",
        json={
            "command": "create_food",
            "dry_run": True,
            "name": "Test Portion",
            "unit": "g",
            "serving_size": 50,
            "kcal": 100,
            "protein": 5,
            "carbs": 12,
            "fat": 3,
        },
        headers=auth(),
    )
    body = resp.get_json()

    assert resp.status_code == 200
    assert body["execution"]["live_state_changed"] is False
    normalized = body["result"]["normalized_food"]
    assert normalized["kcal_per_100"] == 200
    assert normalized["p_per_100"] == 10


def test_actliva_create_food_requires_confirmation_and_writes(client):
    payload = {
        "domain": "nutrition",
        "command": "create_food",
        "payload": {
            "name": "MCP Test Banane",
            "unit": "pcs",
            "kcal": 105,
            "protein": 1.3,
            "carbs": 27,
            "fat": 0.4,
        },
    }
    blocked = client.post("/api/v2/actions/liva/act", json=payload, headers=auth())
    written = client.post("/api/v2/actions/liva/act", json={**payload, "confirm": True}, headers=auth())

    assert blocked.status_code == 400
    assert blocked.get_json()["error"]["code"] == "confirmation_required"
    assert written.status_code == 200
    assert written.get_json()["result"]["food"]["name"] == "MCP Test Banane"


def test_adjust_targets_updates_active_plan_bands_without_dropping_other_modes(client):
    _ensure_test_planning_schema()
    template, error = nutrition_db.create_week_template("Target preservation", set_active=True)
    assert error is None
    _, error = nutrition_db.update_week_template_macro_settings(
        template["id"],
        {
            "active_mode": "cut",
            "modes": {
                "cut": {
                    "kcal_target": 2500,
                    "protein_target": 180,
                    "carbs_target": 280,
                    "fat_target": 65,
                    "green_low": 2400,
                    "green_high": 2600,
                    "yellow_low": 2250,
                    "yellow_high": 2750,
                },
                "maintenance": {
                    "kcal_target": 3000,
                    "protein_target": 175,
                    "carbs_target": 390,
                    "fat_target": 75,
                    "green_low": 2900,
                    "green_high": 3100,
                    "yellow_low": 2750,
                    "yellow_high": 3250,
                },
            },
        },
    )
    assert error is None

    response = client.post(
        "/api/v2/actions/nutrition/control",
        json={
            "command": "adjust_targets",
            "mode": "cut",
            "kcal": 2400,
            "protein": 185,
            "carbs": 255,
            "fat": 65,
            "green_low": 2300,
            "green_high": 2500,
            "yellow_low": 2150,
            "yellow_high": 2650,
        },
        headers=auth(),
    )
    settings, error = nutrition_db.get_week_template_macro_settings(template["id"])

    assert response.status_code == 200
    assert error is None
    assert settings["modes"]["cut"]["green_low"] == 2300
    assert settings["modes"]["cut"]["kcal_target"] == 2400
    assert settings["modes"]["maintenance"]["kcal_target"] == 3000
