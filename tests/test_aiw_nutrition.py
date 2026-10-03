import json
import os
import importlib
from datetime import datetime

import pytest
from flask import Flask

import database.connections as connections


def _now_iso():
    return datetime.now().isoformat(timespec="seconds")


def _seed_food(conn, name, is_active=1):
    now = _now_iso()
    conn.execute(
        """
        INSERT INTO nutrition_foods (
            name, brand, unit_default, portion_g, common_portion_size,
            mfp_search_hint, category,
            kcal_per_100, p_per_100, c_per_100, f_per_100,
            sugar_per_100, salt_per_100, tags, is_favorite, is_active,
            created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            name,
            None,
            "g",
            100,
            None,
            None,
            None,
            100,
            10,
            10,
            10,
            None,
            None,
            None,
            0,
            is_active,
            now,
            now,
        ),
    )
    return conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]


def _seed_targets(conn, kcal=2200, p=160, c=240, f=70, mode="maintenance"):
    conn.execute(
        "INSERT OR REPLACE INTO nutrition_settings (key, value) VALUES (?, ?)",
        ("active_mode", mode),
    )
    conn.execute(
        "INSERT OR REPLACE INTO nutrition_settings (key, value) VALUES (?, ?)",
        (f"mode_{mode}_target", str(kcal)),
    )
    conn.execute(
        "INSERT OR REPLACE INTO nutrition_settings (key, value) VALUES (?, ?)",
        (f"mode_{mode}_protein_target", str(p)),
    )
    conn.execute(
        "INSERT OR REPLACE INTO nutrition_settings (key, value) VALUES (?, ?)",
        (f"mode_{mode}_carbs_target", str(c)),
    )
    conn.execute(
        "INSERT OR REPLACE INTO nutrition_settings (key, value) VALUES (?, ?)",
        (f"mode_{mode}_fat_target", str(f)),
    )


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_path = tmp_path / "nutrition.sqlite3"
    monkeypatch.setenv("AI_WRITE_ENABLED", "1")
    monkeypatch.setenv("LIVA_AI_WRITE_KEY", "test")
    monkeypatch.setenv("LIVA_AI_READ_KEY", "test")
    monkeypatch.setenv("AI_WRITE_REQUIRE_APPROVAL", "0")

    connections.NUTRITION_DB = str(db_path)

    import security.write_guard as write_guard
    importlib.reload(write_guard)

    import nutrition.nutrition_planning_db as npdb
    importlib.reload(npdb)
    npdb.ensure_nutrition_planning_schema()

    import ai.api_aiw_nutrition as aiw
    importlib.reload(aiw)
    import ai.api_air_nutrition as air
    importlib.reload(air)

    app = Flask(__name__)
    app.register_blueprint(aiw.api_aiw_nutrition)
    app.register_blueprint(air.api_air_nutrition)
    app.config["TESTING"] = True

    with app.test_client() as test_client:
        yield test_client


def _auth_headers():
    return {"Authorization": "Bearer test"}


def test_missing_targets(client):
    conn = connections.get_nutrition_db()
    _seed_targets(conn, kcal=2100, p=150, c=230, f=60)
    food_id = _seed_food(conn, "Live Food", is_active=1)
    conn.commit()
    conn.close()

    payload = {"intent": "nutrition_plan_write", "food_ids": [food_id], "dry_run": True}
    resp = client.post("/api/aiw/nutrition/mealplan_template", json=payload, headers=_auth_headers())
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["data"]["targets_used"]["source"] == "live"


def test_no_valid_foods(client):
    conn = connections.get_nutrition_db()
    _seed_targets(conn, kcal=2100, p=150, c=230, f=60)
    conn.commit()
    conn.close()
    payload = {
        "intent": "nutrition_plan_write",
        "food_ids": [999],
        "targets": {"kcal": 2000, "p": 150, "c": 200, "f": 70},
    }
    resp = client.post("/api/aiw/nutrition/mealplan_template", json=payload, headers=_auth_headers())
    assert resp.status_code == 400
    body = resp.get_json()
    assert body["error_code"] == "no_valid_foods"
    assert body["detail"]["invalid_ids"] == [999]


def test_partial_valid(client):
    conn = connections.get_nutrition_db()
    _seed_targets(conn, kcal=2100, p=150, c=230, f=60)
    active_id = _seed_food(conn, "Active Food", is_active=1)
    inactive_id = _seed_food(conn, "Inactive Food", is_active=0)
    conn.commit()
    conn.close()

    payload = {
        "intent": "nutrition_plan_write",
        "food_ids": [active_id, inactive_id],
        "targets": {"kcal": 2000, "p": 150, "c": 200, "f": 70},
        "dry_run": True,
    }
    resp = client.post("/api/aiw/nutrition/mealplan_template", json=payload, headers=_auth_headers())
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True
    assert body["data"]["valid_ids"] == [active_id]
    assert body["data"]["inactive_ids"] == [inactive_id]
    assert body["warnings"]


def test_dry_run_no_writes(client):
    conn = connections.get_nutrition_db()
    _seed_targets(conn, kcal=2100, p=150, c=230, f=60)
    food_id = _seed_food(conn, "DryRun Food", is_active=1)
    conn.commit()
    conn.close()

    payload = {
        "intent": "nutrition_plan_write",
        "food_ids": [food_id],
        "targets": {"kcal": 2000, "p": 150, "c": 200, "f": 70},
        "dry_run": True,
    }
    resp = client.post("/api/aiw/nutrition/mealplan_template", json=payload, headers=_auth_headers())
    assert resp.status_code == 200

    conn = connections.get_nutrition_db()
    count = conn.execute("SELECT COUNT(*) AS n FROM mealplan_templates").fetchone()["n"]
    conn.close()
    assert count == 0


def test_approval_flow(tmp_path, monkeypatch):
    db_path = tmp_path / "nutrition.sqlite3"
    monkeypatch.setenv("AI_WRITE_ENABLED", "1")
    monkeypatch.setenv("LIVA_AI_WRITE_KEY", "test")
    monkeypatch.setenv("LIVA_AI_READ_KEY", "test")
    monkeypatch.setenv("AI_WRITE_REQUIRE_APPROVAL", "1")

    connections.NUTRITION_DB = str(db_path)

    import security.write_guard as write_guard
    importlib.reload(write_guard)
    import nutrition.nutrition_planning_db as npdb
    importlib.reload(npdb)
    npdb.ensure_nutrition_planning_schema()

    import ai.api_aiw_nutrition as aiw
    importlib.reload(aiw)

    app = Flask(__name__)
    app.register_blueprint(aiw.api_aiw_nutrition)
    app.config["TESTING"] = True

    conn = connections.get_nutrition_db()
    _seed_targets(conn, kcal=2100, p=150, c=230, f=60)
    food_id = _seed_food(conn, "Approval Food", is_active=1)
    conn.commit()
    conn.close()

    payload = {
        "intent": "nutrition_plan_write",
        "food_ids": [food_id],
        "targets": {"kcal": 2000, "p": 150, "c": 200, "f": 70},
        "dry_run": False,
    }

    with app.test_client() as test_client:
        resp = test_client.post("/api/aiw/nutrition/mealplan_template", json=payload, headers=_auth_headers())
        assert resp.status_code == 409
        body = resp.get_json()
        action_id = body["detail"]["action_id"]

        approve = test_client.post(f"/api/aiw/actions/{action_id}/approve", json={"intent": "nutrition_plan_write"}, headers=_auth_headers())
        assert approve.status_code == 200
        out = approve.get_json()
        assert out["ok"] is True

    conn = connections.get_nutrition_db()
    count = conn.execute("SELECT COUNT(*) AS n FROM mealplan_templates").fetchone()["n"]
    conn.close()
    assert count == 1


def test_targets_ignored_without_flag(client):
    conn = connections.get_nutrition_db()
    _seed_targets(conn, kcal=2000, p=140, c=220, f=60)
    food_id = _seed_food(conn, "Ignore Targets Food", is_active=1)
    conn.commit()
    conn.close()

    payload = {
        "intent": "nutrition_plan_write",
        "food_ids": [food_id],
        "targets": {"kcal": 9999, "p": 999, "c": 999, "f": 999},
        "dry_run": True,
    }
    resp = client.post("/api/aiw/nutrition/mealplan_template", json=payload, headers=_auth_headers())
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["data"]["targets_used"]["source"] == "live"
    assert any(w.get("code") == "manual_targets_ignored_using_live" for w in body["warnings"])


def test_targets_manual_with_flag(client):
    conn = connections.get_nutrition_db()
    _seed_targets(conn, kcal=2000, p=140, c=220, f=60)
    food_id = _seed_food(conn, "Manual Targets Food", is_active=1)
    conn.commit()
    conn.close()

    payload = {
        "intent": "nutrition_plan_write",
        "food_ids": [food_id],
        "targets": {"kcal": 2500, "p": 180, "c": 280, "f": 70},
        "allow_manual_targets": True,
        "dry_run": True,
    }
    resp = client.post("/api/aiw/nutrition/mealplan_template", json=payload, headers=_auth_headers())
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["data"]["targets_used"]["source"] == "manual_override"
    assert any(w.get("code") == "manual_targets_used" for w in body["warnings"])
