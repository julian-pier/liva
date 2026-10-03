from __future__ import annotations

import json
from pathlib import Path

import database.connections as connections
import nutrition.nutrition_planning_db as nutrition_db

from tests.test_actions_v2_api import auth, client


def test_actions_v2_nutrition_timing_requires_auth(client):
    resp = client.get("/api/v2/actions/nutrition/timing")
    assert resp.status_code == 401
    assert resp.get_json()["ok"] is False


def test_actions_v2_nutrition_timing_returns_compact_payload_and_caps_limits(client):
    conn = connections.get_nutrition_db()
    conn.execute(
        """
        INSERT INTO nutrition_logged_meal_items
        (logged_meal_id, food_id, food_name, amount, unit, calories, protein_override, carbs_override, fat_override, sugar_override, created_at, updated_at)
        VALUES (1, 27, 'Brot', 2, 'pcs', 220, 8, 40, 2, 6, '2026-04-16T08:00:00Z', '2026-04-16T08:00:00Z')
        """
    )
    conn.commit()
    conn.close()

    body = client.get(
        "/api/v2/actions/nutrition/timing?date=2026-04-16&days=30&training_time=10:00&limit_items=999",
        headers=auth(),
    ).get_json()
    assert body["ok"] is True
    assert body["range"]["days"] == 14
    assert body["today"]["meals"]
    assert "timing" in body["today"]
    assert "top_foods" in body["last_days"]
    assert "coach_summary" in body


def test_actions_v2_nutrition_timing_uses_anchor_day_for_today_when_date_to_is_set(client):
    conn = connections.get_nutrition_db()
    conn.execute(
        """
        INSERT INTO nutrition_logged_meals
        (id, log_date, title, meal_name, meal_slot, source, logged_at, created_at, updated_at)
        VALUES
        (11, '2026-04-27', 'Meal A', 'Meal A', 'Meal', 'test', '2026-04-27T08:00:00', '2026-04-27T08:00:00Z', '2026-04-27T08:00:00Z'),
        (12, '2026-05-03', 'Meal B', 'Meal B', 'Meal', 'test', '2026-05-03T18:00:00', '2026-05-03T18:00:00Z', '2026-05-03T18:00:00Z')
        """
    )
    conn.execute(
        """
        INSERT INTO nutrition_logged_meal_items
        (logged_meal_id, food_id, food_name, amount, unit, calories, protein_override, carbs_override, fat_override, sugar_override, created_at, updated_at)
        VALUES
        (11, 27, 'Brot', 1, 'pcs', 100, 5, 18, 1, 2, '2026-04-27T08:00:00Z', '2026-04-27T08:00:00Z'),
        (12, 29, 'Whey', 30, 'g', 117, 23, 2, 2, 1, '2026-05-03T18:00:00Z', '2026-05-03T18:00:00Z')
        """
    )
    conn.commit()
    conn.close()

    body = client.get(
        "/api/v2/actions/nutrition/timing?date_to=2026-05-03&days=7&training_time=19:00&limit_items=50",
        headers=auth(),
    ).get_json()
    assert body["ok"] is True
    assert body["range"]["date_from"] == "2026-04-27"
    assert body["range"]["date_to"] == "2026-05-03"
    assert body["range"]["anchor_date"] == "2026-05-03"
    assert body["date"] == "2026-05-03"
    assert body["today"]["meals"]
    assert all(meal["date"] == "2026-05-03" for meal in body["today"]["meals"])
    assert body["last_days"]["macro_coverage_summary"]["logged_days"] >= 0


def test_actions_v2_nutrition_timing_resolves_pcs_via_common_portion_size(client):
    conn = connections.get_nutrition_db()
    conn.execute("UPDATE nutrition_foods SET common_portion_size=60 WHERE id=27")
    conn.execute(
        """
        INSERT INTO nutrition_logged_meals
        (id, log_date, title, meal_name, meal_slot, source, logged_at, created_at, updated_at)
        VALUES (21, '2026-04-16', 'Pieces Meal', 'Pieces Meal', 'Meal', 'test', '2026-04-16T12:00:00', '2026-04-16T12:00:00Z', '2026-04-16T12:00:00Z')
        """
    )
    conn.execute(
        """
        INSERT INTO nutrition_logged_meal_items
        (logged_meal_id, food_id, food_name, amount, unit, created_at, updated_at)
        VALUES (21, 27, 'Brot', 2, 'pcs', '2026-04-16T12:00:00Z', '2026-04-16T12:00:00Z')
        """
    )
    conn.commit()
    conn.close()
    body = client.get("/api/v2/actions/nutrition/timing?date=2026-04-16&limit_items=50", headers=auth()).get_json()
    item = next(item for meal in body["today"]["meals"] if meal["meal_id"] == 21 for item in meal["items"])
    assert item["macro_status"] == "resolved"
    assert item["macro_source"] == "food_common_portion"
    assert "conversion_note" in item
    assert item["kcal"] is not None
    assert body["today"]["macro_coverage"]["status"] in {"complete", "partial"}


def test_actions_v2_nutrition_timing_marks_unresolved_items_and_warns(client):
    conn = connections.get_nutrition_db()
    conn.execute(
        """
        INSERT INTO nutrition_logged_meals
        (id, log_date, title, meal_name, meal_slot, source, logged_at, created_at, updated_at)
        VALUES (31, '2026-04-16', 'Unknown Meal', 'Unknown Meal', 'Meal', 'test', '2026-04-16T21:00:00', '2026-04-16T21:00:00Z', '2026-04-16T21:00:00Z')
        """
    )
    conn.execute(
        """
        INSERT INTO nutrition_logged_meal_items
        (logged_meal_id, food_id, food_name, amount, unit, created_at, updated_at)
        VALUES (31, NULL, 'Apfel', 1, 'pcs', '2026-04-16T21:00:00Z', '2026-04-16T21:00:00Z')
        """
    )
    conn.commit()
    conn.close()
    body = client.get("/api/v2/actions/nutrition/timing?date=2026-04-16&limit_items=50", headers=auth()).get_json()
    item = next(item for meal in body["today"]["meals"] if meal["meal_id"] == 31 for item in meal["items"])
    assert item["macro_status"] == "unresolved"
    assert item["unresolved_macros"] is True
    assert "unresolved_food_macros:Apfel" in body["today"]["data_warnings"]
    assert body["coach_summary"]["macro_data_quality"] in {"partial", "poor"}
    assert "Tagesmakros wahrscheinlich unterschätzt" in body["coach_summary"]["interpretation_short"]


def test_actions_v2_nutrition_timing_top_foods_are_rounded_and_summary_present(client):
    conn = connections.get_nutrition_db()
    conn.execute("UPDATE nutrition_foods SET common_portion_size=60 WHERE id=27")
    conn.execute(
        """
        INSERT INTO nutrition_logged_meals
        (id, log_date, title, meal_name, meal_slot, source, logged_at, created_at, updated_at)
        VALUES
        (41, '2026-04-16', 'R1', 'R1', 'Meal', 'test', '2026-04-16T07:00:00', '2026-04-16T07:00:00Z', '2026-04-16T07:00:00Z'),
        (42, '2026-04-15', 'R2', 'R2', 'Meal', 'test', '2026-04-15T07:00:00', '2026-04-15T07:00:00Z', '2026-04-15T07:00:00Z')
        """
    )
    conn.execute(
        """
        INSERT INTO nutrition_logged_meal_items
        (logged_meal_id, food_id, food_name, amount, unit, created_at, updated_at)
        VALUES
        (41, 27, 'Brot', 1, 'pcs', '2026-04-16T07:00:00Z', '2026-04-16T07:00:00Z'),
        (42, 27, 'Brot', 1, 'pcs', '2026-04-15T07:00:00Z', '2026-04-15T07:00:00Z')
        """
    )
    conn.commit()
    conn.close()
    body = client.get("/api/v2/actions/nutrition/timing?date=2026-04-16&days=3&limit_items=50", headers=auth()).get_json()
    assert "coach_summary" in body
    top = next(item for item in body["last_days"]["top_foods"] if item["name"] == "Brot")
    assert top["total_kcal"] == round(top["total_kcal"], 1)
    assert top["total_protein"] == round(top["total_protein"], 1)
    assert isinstance(body["last_days"]["macro_coverage_summary"]["unresolved_foods_top"], list)


def test_actions_v2_nutrition_foods_and_query_validation(client):
    body = client.get("/api/v2/actions/nutrition/foods?query=brot&sort=protein", headers=auth()).get_json()
    assert body["ok"] is True
    assert body["count"] >= 1
    assert body["foods"][0]["source"] == "nutrition_db"

    bad = client.get("/api/v2/actions/nutrition/foods?sort=bad", headers=auth())
    assert bad.status_code == 400


def test_actions_v2_cardio_summary_returns_run_split_and_bad_sport_type_400(client):
    body = client.get("/api/v2/actions/cardio/summary?days=14&sport_type=all", headers=auth()).get_json()
    assert body["ok"] is True
    assert "summary" in body
    assert "run" in body
    assert "ergo" in body

    bad = client.get("/api/v2/actions/cardio/summary?sport_type=swim", headers=auth())
    assert bad.status_code == 400


def test_actions_v2_training_today_loads_returns_compact_keys(client):
    body = client.get("/api/v2/actions/training/today-loads?date=2026-04-14&day=Mo", headers=auth()).get_json()
    assert body["ok"] is True
    assert "session" in body
    assert "core_constraints" in body
    assert "exercises" in body
    assert body["exercises"][0]["rounding"]


def test_actions_v2_training_today_loads_uses_newest_reference_first(client):
    body = client.get("/api/v2/actions/training/today-loads?date=2026-04-14&day=Mo&history_limit=5", headers=auth()).get_json()
    assert body["ok"] is True
    first = next(item for item in body["exercises"] if item["display_name"] == "Schrägbankdrücken (Smith)")
    assert first["last_performances"][0]["date"] == "2026-04-08"
    assert first["best_recent_set"]["date"] == "2026-04-08"
    assert first["suggestion"]["reference_date"] == "2026-04-08"
    assert first["suggestion"]["reference_source"] == "exact_variation"
    assert first["suggestion"]["status"] in {"available", "stale_reference"}
    assert first["suggestion"]["confidence"] in {"high", "medium", "low"}
    assert first["suggestion"]["reference_age_days"] == 6


def test_actions_v2_life_today_gracefully_handles_missing_calendar(client, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    body = client.get("/api/v2/actions/life/today?date=2026-04-16&days=9", headers=auth()).get_json()
    assert body["ok"] is True
    assert body["data_status"]["calendar"] in {"fresh", "missing"}
    assert "calendar" in body
    assert body["calendar"]["available"] in {True, False}


def test_actions_v2_bodycomp_cut_status_and_checkin_today_do_not_500(client):
    cut = client.get("/api/v2/actions/bodycomp/cut-status?days=100", headers=auth()).get_json()
    assert cut["ok"] is True
    assert "decision" in cut
    assert cut["data_status"]["nutrition"] in {"fresh", "partial", "stale", "missing"}

    checkin = client.get("/api/v2/actions/checkin/today?date=2026-04-16", headers=auth()).get_json()
    assert checkin["ok"] is True
    assert "checkin" in checkin


def test_actions_v2_bodycomp_cut_status_requires_two_calendar_weeks_for_cut_inference(client):
    conn = connections.get_nutrition_db()
    conn.execute("DELETE FROM weight_logs")
    rows = [
        ("2026-05-03", 70.4, 2610, 150, 280, 65),
        ("2026-05-02", 70.5, 2620, 152, 282, 66),
        ("2026-05-01", 70.6, 2600, 151, 279, 65),
        ("2026-04-30", 70.7, 2625, 155, 281, 66),
        ("2026-04-29", 70.9, 2630, 154, 283, 66),
        ("2026-04-28", 71.0, 2620, 153, 280, 65),
        ("2026-04-27", 71.1, 2627, 157, 284, 67),
    ]
    for day, weight, kcal, protein, carbs, fat in rows:
        conn.execute(
            "INSERT INTO weight_logs (date, date_iso, weight_kg, kcal, protein, carbs, fat, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (day, day, weight, kcal, protein, carbs, fat, f"{day}T07:00:00Z"),
        )
    conn.commit()
    conn.close()

    body = client.get("/api/v2/actions/bodycomp/cut-status?date_to=2026-05-03&days=14", headers=auth()).get_json()
    assert body["ok"] is True
    assert body["phase"]["mode"] in {"cut", "deficit_like", "maintenance"}
    assert body["weight"]["status"] == "no_data"
    assert body["weight"]["calorie_adjustment_eligible"] is False
    assert body["decision"]["status"] == "needs_more_data"
    assert body["decision"]["headline"]
    assert body["decision"]["recommendation"]
    assert body["nutrition"]["protein_gap_g"] is not None
    assert body["weight"]["trend_label"] in {"fallend, aber nicht aggressiv", "uneinheitlich"}


def test_actions_v2_endurance_week_degrades_cleanly_when_not_connected(client):
    body = client.get("/api/v2/actions/endurance/week?date=2026-04-16", headers=auth()).get_json()
    assert body["ok"] is True
    assert "summary" in body


def test_actions_v2_aggregate_gets_validate_iso_dates(client):
    for path in (
        "/api/v2/actions/nutrition/timing?date=2026-99-99",
        "/api/v2/actions/training/today-loads?date=bad",
        "/api/v2/actions/life/today?date=nope",
        "/api/v2/actions/checkin/today?date=wrong",
    ):
        resp = client.get(path, headers=auth())
        assert resp.status_code == 400


def test_actions_v2_openapi_and_gpt_files_include_new_aggregate_paths():
    full_spec = json.loads(Path("openapi/liva-actions-v2.json").read_text(encoding="utf-8"))
    slim_spec = json.loads(Path("openapi/liva-actions-gpt.json").read_text(encoding="utf-8"))
    for path in (
        "/actions/nutrition/timing",
        "/actions/nutrition/foods",
        "/actions/training/today-loads",
        "/actions/cardio/summary",
        "/actions/life/today",
        "/actions/bodycomp/cut-status",
        "/actions/checkin/today",
    ):
        assert path in full_spec["paths"]
    assert {"/api/v2/actions/liva/read", "/api/v2/actions/liva/act"}.issubset(set(slim_spec["paths"]))
    assert 10 <= len(slim_spec["paths"]) <= 20


def test_liva_read_daily_snapshot_and_invalid_mode(client):
    good = client.post("/api/v2/actions/liva/read", headers=auth(), json={"mode": "daily_snapshot"})
    assert good.status_code == 200
    body = good.get_json()
    assert body["ok"] is True
    assert body["mode"] == "daily_snapshot"
    assert "today" in body["result"]

    bad = client.post("/api/v2/actions/liva/read", headers=auth(), json={"mode": "nope"})
    assert bad.status_code == 400


def test_liva_read_nutrition_timing_training_today_loads_and_cut_status(client):
    timing = client.post(
        "/api/v2/actions/liva/read",
        headers=auth(),
        json={"mode": "nutrition_timing", "date_to": "2026-05-03", "days": 7, "training_time": "19:00", "include_items": True},
    ).get_json()
    assert timing["ok"] is True
    assert "today" in timing["result"]
    assert "last_days" in timing["result"]

    loads = client.post(
        "/api/v2/actions/liva/read",
        headers=auth(),
        json={"mode": "training_today_loads", "date": "2026-04-14", "include_history": True, "limit": 5},
    ).get_json()
    assert loads["ok"] is True
    assert "exercises" in loads["result"]

    cut = client.post(
        "/api/v2/actions/liva/read",
        headers=auth(),
        json={"mode": "bodycomp_cut_status", "date_to": "2026-05-03", "days": 14},
    ).get_json()
    assert cut["ok"] is True
    assert "decision" in cut["result"]
    assert "weight" in cut["result"]


def test_liva_act_invalid_domain_command_confirm_and_dry_run(client):
    bad_domain = client.post("/api/v2/actions/liva/act", headers=auth(), json={"domain": "bad", "command": "x"})
    assert bad_domain.status_code == 400

    bad_command = client.post("/api/v2/actions/liva/act", headers=auth(), json={"domain": "weight", "command": "bad"})
    assert bad_command.status_code == 400
    assert bad_command.get_json()["error"]["code"] == "unsupported_command"

    delete_no_confirm = client.post(
        "/api/v2/actions/liva/act",
        headers=auth(),
        json={"domain": "weight", "command": "delete_weight", "payload": {"date": "2026-04-16"}},
    )
    assert delete_no_confirm.status_code == 400
    delete_body = delete_no_confirm.get_json()
    assert delete_body["error"]["code"] == "confirmation_required"
    assert delete_body["error"]["supported"] is True

    dry_run = client.post(
        "/api/v2/actions/liva/act",
        headers=auth(),
        json={
            "domain": "training",
            "command": "log_gym_session",
            "dry_run": True,
            "payload": {"date": "2026-05-04", "session_name": "Push", "raw_text": "Bench 100 x 8"},
        },
    )
    assert dry_run.status_code == 200
    assert dry_run.get_json()["execution"]["mode"] == "dry_run"


def test_liva_act_weight_facade_write_and_readback(client):
    body = client.post(
        "/api/v2/actions/liva/act",
        headers=auth(),
        json={"domain": "weight", "command": "log_weight", "date": "2026-05-04", "weight": 70.2},
    ).get_json()
    assert body["ok"] is True
    assert body["execution"]["mode"] == "applied_live"
    readback = client.post("/api/v2/actions/liva/read", headers=auth(), json={"mode": "weight_data", "date_from": "2026-05-04", "date_to": "2026-05-04"}).get_json()
    assert any(point["weight"] == 70.2 for point in readback["result"]["weight"]["points"])


def test_liva_act_cardio_facade_write_and_readback(client):
    body = client.post(
        "/api/v2/actions/liva/act",
        headers=auth(),
        json={
            "domain": "cardio",
            "command": "create_cardio_session",
            "date": "2026-05-04",
            "payload": {"sport_type": "ergo", "duration": "17:20", "avg_hr": 123},
        },
    ).get_json()
    assert body["ok"] is True
    assert body["execution"]["mode"] == "applied_live"
    readback = client.post("/api/v2/actions/liva/read", headers=auth(), json={"mode": "cardio_data", "date_from": "2026-05-04", "date_to": "2026-05-04"}).get_json()
    assert any(session.get("avg_hr") == 123 for session in readback["result"]["runs"]["sessions"])


def test_liva_act_nutrition_quick_log_and_recovery_annotate(client):
    meal = client.post(
        "/api/v2/actions/liva/act",
        headers=auth(),
        json={
            "domain": "nutrition",
            "command": "quick_log_meal",
            "date": "2026-05-04",
            "payload": {
                "meal_name": "Snack",
                "logged_at": "2026-05-04T16:30:00",
                "items": [{"food_name": "Whey", "amount": 30, "unit": "g"}],
            },
        },
    ).get_json()
    assert meal["ok"] is True
    assert meal["execution"]["mode"] == "applied_live"
    nutrition = client.post(
        "/api/v2/actions/liva/read",
        headers=auth(),
        json={"mode": "nutrition_data", "payload": {"mode": "meals"}, "date_from": "2026-05-04", "date_to": "2026-05-04"},
    ).get_json()
    assert nutrition["ok"] is True
    assert nutrition["result"]["meals"]

    recovery = client.post(
        "/api/v2/actions/liva/act",
        headers=auth(),
        json={"domain": "recovery", "command": "annotate_day", "date": "2026-05-04", "payload": {"note": "schlecht geschlafen"}},
    ).get_json()
    assert recovery["ok"] is True
    assert recovery["execution"]["mode"] == "applied_live"


def test_liva_read_nutrition_data_accepts_published_nested_nutrition_mode(client):
    conn = connections.get_nutrition_db()
    conn.execute(
        """
        INSERT INTO nutrition_logged_meal_items
        (logged_meal_id, food_id, food_name, amount, unit, calories, protein_override, carbs_override, fat_override, created_at, updated_at)
        VALUES (1, 27, 'Brot', 2, 'pcs', 220, 8, 40, 2, '2026-04-16T08:00:00Z', '2026-04-16T08:00:00Z')
        """
    )
    conn.commit()
    conn.close()
    body = client.post(
        "/api/v2/actions/liva/read",
        headers=auth(),
        json={
            "mode": "nutrition_data",
            "payload": {
                "nutrition_mode": "meals",
                "date_from": "2026-04-16",
                "date_to": "2026-04-17",
                "limit": 50,
            },
        },
    ).get_json()

    assert body["ok"] is True, body
    assert body["result"]["mode"] == "meals"
    assert body["result"]["history_detail"] == "full_logged_foods_quantities_and_macros"
    assert body["result"]["meals"][0]["items"]


def test_liva_act_nutrition_log_planned_meal(client, monkeypatch):
    day_payload = {
        "planned_meals": [
            {
                "slot_id": 11,
                "slot_index": 0,
                "title": "Frühstück",
                "meal_slot": "Meal 1",
                "status": "open",
                "items": [{"food_name": "Brot", "amount": 2, "unit": "pcs"}],
            }
        ],
        "logged_meals": [],
    }

    monkeypatch.setattr(nutrition_db, "get_logging_day_payload", lambda date_iso: day_payload)
    monkeypatch.setattr(
        nutrition_db,
        "log_planned_meal",
        lambda slot_id, **kwargs: ({**day_payload, "logged_meals": [{"id": 44, "slot_id": slot_id, "created_from_planned_slot_id": slot_id}]}, None),
    )

    body = client.post(
        "/api/v2/actions/liva/act",
        headers=auth(),
        json={"domain": "nutrition", "command": "log_planned_meal", "date": "2026-05-04", "payload": {"meal_number": 1}},
    ).get_json()
    assert body["ok"] is True
    assert body["execution"]["mode"] == "applied_live"
    assert body["result"]["meal_number"] == 1


def test_liva_act_training_dry_run_and_confirmed_write(client):
    before = client.post(
        "/api/v2/actions/liva/read",
        headers=auth(),
        json={"mode": "training_data", "payload": {"scope": "sessions"}, "date_from": "2026-05-04", "date_to": "2026-05-04"},
    ).get_json()
    dry_run = client.post(
        "/api/v2/actions/liva/act",
        headers=auth(),
        json={
            "domain": "training",
            "command": "log_gym_session",
            "dry_run": True,
            "payload": {"date": "2026-05-04", "session_name": "Push", "raw_text": "Bench 100 x 8"},
        },
    ).get_json()
    assert dry_run["ok"] is True
    assert dry_run["execution"]["mode"] == "dry_run"
    mid = client.post(
        "/api/v2/actions/liva/read",
        headers=auth(),
        json={"mode": "training_data", "payload": {"scope": "sessions"}, "date_from": "2026-05-04", "date_to": "2026-05-04"},
    ).get_json()
    assert len(mid["result"]["sessions"]) == len(before["result"]["sessions"])

    live = client.post(
        "/api/v2/actions/liva/act",
        headers=auth(),
        json={
            "domain": "training",
            "command": "log_gym_session",
            "dry_run": False,
            "confirm": True,
            "payload": {"date": "2026-05-04", "session_name": "Push", "raw_text": "Bench 100 x 8"},
        },
    ).get_json()
    assert live["ok"] is True
    assert live["execution"]["mode"] == "applied_live"
    after = client.post(
        "/api/v2/actions/liva/read",
        headers=auth(),
        json={"mode": "training_data", "payload": {"scope": "sessions"}, "date_from": "2026-05-04", "date_to": "2026-05-04"},
    ).get_json()
    assert len(after["result"]["sessions"]) == len(before["result"]["sessions"]) + 1


def test_liva_act_core_build_board_and_gpt_openapi_facade(client):
    build = client.post(
        "/api/v2/actions/liva/act",
        headers=auth(),
        json={
            "domain": "core",
            "command": "build_board",
            "confirm": True,
            "payload": {"date": "2026-04-16", "mode": "gpt_fast", "memory_mode": "skip", "build_training_card": False},
        },
    )
    assert build.status_code == 200
    body = build.get_json()
    assert body["ok"] is True
    assert body["execution"]["mode"] in {"applied_live", "no_change"}

    slim_spec = json.loads(Path("openapi/liva-actions-gpt.json").read_text(encoding="utf-8"))
    paths = slim_spec.get("paths", {})
    assert {"/api/v2/actions/liva/read", "/api/v2/actions/liva/act"}.issubset(set(paths))
    for forbidden in (
        "/actions/nutrition/timing",
        "/actions/training/today-loads",
        "/actions/cardio/summary",
        "/actions/bodycomp/cut-status",
        "/actions/training/adjust",
        "/actions/core/board/build",
        "/actions/core/memory/read",
    ):
        assert forbidden not in paths
    schemas = slim_spec.get("components", {}).get("schemas", {})
    for forbidden_schema in (
        "RecoveryControlRequest",
        "WeightControlRequest",
        "RunsControlRequest",
        "RemoteControlRequest",
    ):
        assert forbidden_schema not in schemas
    assert Path("openapi/liva-actions-gpt.json").stat().st_size < Path("openapi/liva-actions-v2.json").stat().st_size
