import importlib
import os
from pathlib import Path

import pytest
from flask import Flask

import database.connections as connections


@pytest.fixture()
def client(tmp_path, monkeypatch):
    runs_db = tmp_path / "runs.sqlite3"
    training_db = tmp_path / "training.sqlite3"
    plans_db = tmp_path / "plans.sqlite3"
    nutrition_db = tmp_path / "nutrition.sqlite3"
    hrv_db = tmp_path / "hrv.sqlite3"

    monkeypatch.setenv("LIVA_AI_READ_KEY", "test")
    monkeypatch.setenv("LIVA_AI_WRITE_KEY", "test")

    connections.RUNS_DB = str(runs_db)
    connections.TRAINING_DB = str(training_db)
    connections.PLANS_DB = str(plans_db)
    connections.NUTRITION_DB = str(nutrition_db)
    connections.HRV_DB = str(hrv_db)

    conn = connections.get_runs_db()
    conn.execute(
        """
        CREATE TABLE runs (
            id INTEGER PRIMARY KEY,
            date TEXT,
            distance REAL,
            moving_time INTEGER,
            avg_speed REAL,
            avg_hr REAL,
            max_hr REAL,
            elevation_gain REAL,
            pace REAL
        )
        """
    )
    for i in range(3):
        conn.execute(
            "INSERT INTO runs (id, date, distance, moving_time, avg_speed, avg_hr, max_hr, elevation_gain, pace) VALUES (?,?,?,?,?,?,?,?,?)",
            (i + 1, f"2026-02-0{i+1}", 1000 * (i + 1), 300 + i, 3.0, 140 + i, 170 + i, 10, 300),
        )
    conn.commit()
    conn.close()

    conn = connections.get_training_db()
    conn.executescript(
        """
        CREATE TABLE workouts (id INTEGER PRIMARY KEY, date_iso TEXT, name TEXT, notes TEXT, created_at TEXT);
        CREATE TABLE exercises (id INTEGER PRIMARY KEY, workout_id INTEGER, name TEXT, variation TEXT, device TEXT, laterality TEXT, created_at TEXT);
        CREATE TABLE sets (id INTEGER PRIMARY KEY, exercise_id INTEGER, workout_id INTEGER, set_number INTEGER, weight REAL, reps INTEGER, rpe REAL, created_at TEXT);
        """
    )
    conn.execute("INSERT INTO workouts (id, date_iso, name, notes, created_at) VALUES (1, '2026-01-25', 'Upper A', '', '2026-01-25')")
    conn.execute("INSERT INTO workouts (id, date_iso, name, notes, created_at) VALUES (2, '2026-02-01', 'Upper A', '', '2026-02-01')")
    conn.execute("INSERT INTO workouts (id, date_iso, name, notes, created_at) VALUES (3, '2026-02-08', 'Upper A', '', '2026-02-08')")
    conn.execute("INSERT INTO workouts (id, date_iso, name, notes, created_at) VALUES (4, '2026-02-15', 'Upper A', '', '2026-02-15')")
    conn.execute("INSERT INTO workouts (id, date_iso, name, notes, created_at) VALUES (5, '2026-02-22', 'Pull', '', '2026-02-22')")
    conn.execute("INSERT INTO exercises (id, workout_id, name, variation, device, laterality, created_at) VALUES (1,1,'Bench','Flat', 'barbell', 'both', '2026-01-25')")
    conn.execute("INSERT INTO exercises (id, workout_id, name, variation, device, laterality, created_at) VALUES (2,2,'Bench','Flat', 'barbell', 'both', '2026-02-01')")
    conn.execute("INSERT INTO exercises (id, workout_id, name, variation, device, laterality, created_at) VALUES (3,3,'Bench','Flat', 'barbell', 'both', '2026-02-08')")
    conn.execute("INSERT INTO exercises (id, workout_id, name, variation, device, laterality, created_at) VALUES (4,4,'Bench','Flat', 'barbell', 'both', '2026-02-15')")
    conn.execute("INSERT INTO exercises (id, workout_id, name, variation, device, laterality, created_at) VALUES (5,5,'Row','Chest Supported', 'machine', 'both', '2026-02-22')")
    bench_sets = [
        (1, 1, 1, 1, 100, 8, 8.5, '2026-01-25'),
        (2, 1, 1, 2, 97.5, 9, 8.0, '2026-01-25'),
        (3, 2, 2, 1, 102.5, 8, 9.0, '2026-02-01'),
        (4, 2, 2, 2, 100, 9, 8.5, '2026-02-01'),
        (5, 3, 3, 1, 105, 8, 9.0, '2026-02-08'),
        (6, 3, 3, 2, 102.5, 10, 8.5, '2026-02-08'),
        (7, 4, 4, 1, 105, 10, 8.0, '2026-02-15'),
        (8, 4, 4, 2, 100, 10, 7.5, '2026-02-15'),
        (9, 5, 5, 1, 80, 12, 8.0, '2026-02-22'),
    ]
    for row in bench_sets:
        conn.execute(
            "INSERT INTO sets (id, exercise_id, workout_id, set_number, weight, reps, rpe, created_at) VALUES (?,?,?,?,?,?,?,?)",
            row,
        )
    conn.commit()
    conn.close()

    conn = connections.get_plans_db()
    conn.executescript(
        """
        CREATE TABLE plans (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            data TEXT NOT NULL,
            block_length INTEGER NOT NULL DEFAULT 4,
            is_active INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        """
    )
    conn.execute("INSERT INTO plans (name, data, block_length, is_active, created_at, updated_at) VALUES ('Plan A', '{""name"": ""Plan A""}', 4, 1, '2026-02-01', '2026-02-01')")
    conn.commit()
    conn.close()

    conn = connections.get_nutrition_db()
    conn.executescript(
        """
        CREATE TABLE nutrition_settings (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE nutrition_day_actuals (id INTEGER PRIMARY KEY AUTOINCREMENT, day TEXT, kcal REAL, p REAL, c REAL, f REAL, source TEXT, created_at TEXT, updated_at TEXT);
        CREATE TABLE nutrition_foods (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, brand TEXT, unit_default TEXT, portion_g REAL, common_portion_size REAL, mfp_search_hint TEXT, category TEXT, kcal_per_100 REAL, p_per_100 REAL, c_per_100 REAL, f_per_100 REAL, sugar_per_100 REAL, salt_per_100 REAL, tags TEXT, is_favorite INTEGER DEFAULT 0, is_active INTEGER DEFAULT 1, created_at TEXT, updated_at TEXT);
        CREATE TABLE nutrition_meal_templates (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT, mode TEXT, notes TEXT, is_favorite INTEGER, created_at TEXT, updated_at TEXT);
        CREATE TABLE nutrition_meal_ingredients (id INTEGER PRIMARY KEY AUTOINCREMENT, meal_template_id INTEGER, food_id INTEGER, amount REAL, unit TEXT, sort_index INTEGER, created_at TEXT, updated_at TEXT);
        CREATE TABLE nutrition_week_plans (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT, is_active INTEGER, start_monday TEXT, created_at TEXT, updated_at TEXT);
        CREATE TABLE nutrition_day_plan_slots (id INTEGER PRIMARY KEY AUTOINCREMENT, week_plan_id INTEGER, weekday INTEGER, slot_index INTEGER, time_text TEXT, label_text TEXT, meal_template_id INTEGER, created_at TEXT, updated_at TEXT);
        """
    )
    conn.execute("INSERT INTO nutrition_settings (key, value) VALUES ('active_mode', 'maintenance')")
    conn.execute("INSERT INTO nutrition_settings (key, value) VALUES ('mode_maintenance_target', '2200')")
    conn.execute("INSERT INTO nutrition_settings (key, value) VALUES ('mode_maintenance_protein_target', '160')")
    conn.execute("INSERT INTO nutrition_settings (key, value) VALUES ('mode_maintenance_carbs_target', '240')")
    conn.execute("INSERT INTO nutrition_settings (key, value) VALUES ('mode_maintenance_fat_target', '70')")
    conn.execute("INSERT INTO nutrition_foods (id, name, brand, unit_default, portion_g, common_portion_size, mfp_search_hint, category, kcal_per_100, p_per_100, c_per_100, f_per_100, sugar_per_100, salt_per_100, tags, is_favorite, is_active, created_at, updated_at) VALUES (1, 'Rice', 'Test', 'g', 100, NULL, NULL, NULL, 130, 2, 28, 1, NULL, NULL, NULL, 0, 1, '2026-02-01', '2026-02-01')")
    conn.execute("INSERT INTO nutrition_meal_templates (id, title, mode, notes, is_favorite, created_at, updated_at) VALUES (1, 'Meal', 'fixed', '', 0, '2026-02-01', '2026-02-01')")
    conn.execute("INSERT INTO nutrition_meal_ingredients (meal_template_id, food_id, amount, unit, sort_index, created_at, updated_at) VALUES (1, 1, 100, 'g', 0, '2026-02-01', '2026-02-01')")
    conn.execute("INSERT INTO nutrition_week_plans (id, title, is_active, start_monday, created_at, updated_at) VALUES (1, 'Week', 1, '2026-02-03', '2026-02-01', '2026-02-01')")
    conn.execute("INSERT INTO nutrition_day_plan_slots (week_plan_id, weekday, slot_index, time_text, label_text, meal_template_id, created_at, updated_at) VALUES (1, 1, 0, '08:00', 'Breakfast', 1, '2026-02-01', '2026-02-01')")
    conn.commit()
    conn.close()

    import sqlite3
    conn = sqlite3.connect(str(hrv_db))
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE hrv_measurements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts_measurement TEXT NOT NULL UNIQUE,
            date_utc TEXT,
            hr REAL,
            rmssd REAL,
            sdnn REAL,
            avnn REAL,
            signal_quality TEXT,
            source_file TEXT,
            created_at TEXT
        )
        """
    )
    conn.execute("INSERT INTO hrv_measurements (ts_measurement, date_utc, hr, rmssd, sdnn, avnn, signal_quality, source_file, created_at) VALUES ('2026-02-01 06:00:00+0000', '2026-02-01 06:00:00+0000', 50, 70, 30, 900, 'ok', 'file', '2026-02-01')")
    conn.commit()
    conn.close()

    import security.write_guard as write_guard
    importlib.reload(write_guard)
    import nutrition.nutrition_planning_db as npdb
    importlib.reload(npdb)
    npdb.ensure_nutrition_planning_schema()

    import ai.api_ai as ai_api_module
    importlib.reload(ai_api_module)

    app = Flask(__name__)
    app.register_blueprint(ai_api_module.ai_api)
    app.config["TESTING"] = True

    with app.test_client() as test_client:
        yield test_client


def _auth_headers():
    return {"Authorization": "Bearer test"}


def test_api_key_header_can_read_ai_endpoints(client):
    resp = client.get("/api/ai/meta", headers={"X-API-Key": "test"})
    body = resp.get_json()
    assert resp.status_code == 200
    assert body["ok"] is True


def test_raw_authorization_key_can_read_ai_endpoints(client):
    resp = client.get("/api/ai/meta", headers={"Authorization": "test"})
    body = resp.get_json()
    assert resp.status_code == 200
    assert body["ok"] is True


def test_chatgpt_action_get_read_without_key_is_unauthorized(client):
    resp = client.get("/api/ai/meta", headers={"User-Agent": "ChatGPT-User/1.0"})
    body = resp.get_json()
    assert resp.status_code == 401
    assert body["ok"] is False


def test_ai_get_read_without_key_is_unauthorized(client):
    resp = client.get("/api/ai/meta", headers={"User-Agent": "curl/8.5.0"})
    body = resp.get_json()
    assert resp.status_code == 401
    assert body["ok"] is False


def test_openapi_includes_ai_index_with_explicit_auth():
    spec = Path("openapi/openapi.yaml").read_text(encoding="utf-8")
    block = spec.split("  /api/ai/index:", 1)[1].split("  /api/ai/meta:", 1)[0]
    assert "operationId: aiIndex" in block
    assert "security:" in block
    assert "- ApiKeyAuth: []" in block
    assert "#/components/schemas/IndexResponse" in block


def test_openapi_stays_within_action_operation_limit():
    spec = Path("openapi/openapi.yaml").read_text(encoding="utf-8")
    assert spec.count("operationId:") <= 30


def test_meta_keys_present(client):
    resp = client.get("/api/ai/meta", headers=_auth_headers())
    body = resp.get_json()
    assert resp.status_code == 200
    assert resp.is_json
    assert "text/html" not in (resp.content_type or "")
    assert body["ok"] is True
    assert "db_bounds" in body
    assert "timezone" in body


def test_index_keeps_30_endpoints_and_includes_memory(client):
    resp = client.get("/api/ai/index", headers=_auth_headers())
    assert resp.status_code == 200
    assert resp.is_json
    body = resp.get_json()
    assert body["ok"] is True
    assert body["max_operations"] == 30
    assert len(body["endpoints"]) == 24

    endpoint_map = {(item["method"], item["path"]) for item in body["endpoints"]}
    assert not any(path.startswith("/api/memory") for _method, path in endpoint_map)


def test_pagination_runs_list(client):
    resp = client.get("/api/ai/runs/list?limit=1", headers=_auth_headers())
    body = resp.get_json()
    assert body["ok"] is True
    assert len(body["data"]) == 1
    assert body["meta"]["cursor_next"]


def test_runs_list_supports_action_field_aliases(client):
    resp = client.get(
        "/api/ai/runs/list?limit=1&fields=id,date,distance_m,moving_time_s,avg_pace_s_per_km,elevation_gain_m,title",
        headers=_auth_headers(),
    )
    body = resp.get_json()
    assert body["ok"] is True
    row = body["data"][0]
    assert row["distance_m"] == 3000
    assert row["moving_time_s"] == 302
    assert row["avg_pace_s_per_km"] == 300
    assert row["title"]


def test_pagination_workouts(client):
    resp = client.get("/api/ai/gym/workouts?limit=1", headers=_auth_headers())
    body = resp.get_json()
    assert body["ok"] is True
    assert body["meta"]["cursor_next"]


def test_targets_live(client):
    resp = client.get("/api/ai/nutrition/targets/live", headers=_auth_headers())
    body = resp.get_json()
    assert resp.status_code == 200
    assert resp.is_json
    assert body["ok"] is True
    assert body["data"]["kcal"] >= 0
    assert {"kcal", "p", "c", "f", "source", "effective_date"}.issubset(body["data"])


def test_meals_list_resolved_food(client):
    resp = client.get("/api/ai/nutrition/meals/list", headers=_auth_headers())
    body = resp.get_json()
    assert body["ok"] is True
    assert body["data"]
    items = body["data"][0]["items"]
    assert items
    assert "resolved_food" in items[0]


def test_gym_progression_overview_shows_real_progress(client):
    resp = client.get("/api/ai/gym/progression/overview?exercise=Bench", headers=_auth_headers())
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True
    assert body["data"]
    bench = body["data"][0]
    assert bench["exercise"] == "Bench"
    assert bench["sessions"] == 4
    assert bench["progression_events"] >= 2
    assert bench["latest_signal"] == "progress"
    assert bench["delta_e1rm_vs_first_pct"] > 0


def test_gym_progression_exercise_returns_session_timeline(client):
    resp = client.get("/api/ai/gym/progression/exercise?exercise=Bench&device=barbell", headers=_auth_headers())
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True
    assert len(body["data"]) == 4
    assert body["data"][0]["signal"] == "seed"
    assert body["data"][1]["signal"] == "progress"
    assert body["data"][1]["is_all_time_pr"] is True
    assert body["data"][2]["signal_reason"] in {"successful_load_progression", "performance_score_up"}
    assert body["data"][2]["delta_e1rm_vs_previous"] > 0


def test_gym_progression_exercise_supports_action_field_aliases(client):
    resp = client.get(
        "/api/ai/gym/progression/exercise?exercise=Bench&device=barbell&fields=date,e1rm,top_set_weight,top_set_reps,top_set_rpe,signal",
        headers=_auth_headers(),
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True
    row = body["data"][0]
    assert row["date"] == "2026-01-25"
    assert row["top_set_weight"] == 100.0
    assert row["top_set_reps"] == 8
    assert row["top_set_rpe"] == 8.5


def test_gym_progression_exercise_missing_param_is_transport_stable(client):
    resp = client.get("/api/ai/gym/progression/exercise", headers=_auth_headers())
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is False
    assert body["error_code"] == "missing_params"


def test_gym_progression_monthly_returns_month_buckets(client):
    resp = client.get("/api/ai/gym/progression/monthly?exercise=Bench&device=barbell", headers=_auth_headers())
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True
    assert len(body["data"]) >= 2
    months = {row["month"]: row for row in body["data"]}
    assert "2026-01" in months
    assert "2026-02" in months
    assert months["2026-02"]["sessions"] == 3
    assert months["2026-02"]["progression_events"] >= 2
    assert months["2026-02"]["delta_e1rm_vs_prev_month"] > 0


def test_progression_monthly_helper_handles_missing_e1rm():
    import ai.api_ai_gym as gym_api

    payload = gym_api._build_progression_monthly({
        ("Bench", "LH", "barbell", "both"): [
            {"date_iso": "2026-02-01", "workout_id": 1, "e1rm": None, "signal": "stable"},
            {"date_iso": "2026-02-08", "workout_id": 2, "e1rm": None, "signal": "stable"},
        ]
    })
    assert len(payload) == 1
    assert payload[0]["month"] == "2026-02"
    assert payload[0]["best_e1rm"] is None
