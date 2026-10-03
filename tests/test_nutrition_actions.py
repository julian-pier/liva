import sqlite3
from datetime import date, timedelta
from pathlib import Path

import database.connections as connections
from tests.test_actions_v2_api import auth, client
from tests.test_actions_v2_plan_create import _seed_test_food
from tests.test_actions_v2_plan_lifecycle import _create_nutrition_plan


def _slot_food_ids(test_client, template_id: int) -> list[int]:
    body = test_client.get(f"/api/v2/actions/nutrition/plans/{template_id}/detail", headers=auth()).get_json()
    return [
        int(slot["food_id"])
        for day in body.get("days") or []
        for meal in day.get("meals") or []
        for slot in meal.get("slots") or []
        if slot.get("food_id") is not None
    ]


def _add_placeholder_meals(template_id: int, *, start_index: int = 4, count: int = 3) -> None:
    conn = connections.get_nutrition_db()
    conn.row_factory = sqlite3.Row
    try:
        day_row = conn.execute(
            "SELECT id FROM nutrition_week_template_days WHERE week_template_id=? AND weekday=0 LIMIT 1",
            (template_id,),
        ).fetchone()
        assert day_row is not None
        now = "2026-05-26T08:00:00Z"
        for slot_index in range(start_index, start_index + count):
            conn.execute(
                """
                INSERT INTO nutrition_week_day_slots
                    (week_template_day_id, slot_template_id, slot_index, active_option_id, locked, time_text, custom_title, note_text, meal_template_id, created_at, updated_at)
                VALUES (?, 1, ?, NULL, 0, NULL, ?, NULL, NULL, ?, ?)
                """,
                (int(day_row["id"]), slot_index, f"Meal {slot_index + 1}", now, now),
            )
        conn.commit()
    finally:
        conn.close()


def _today_day_label() -> str:
    labels = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]
    return labels[date.today().weekday()]


def test_logging_day_read_does_not_create_or_update_weight_logs(client, monkeypatch):
    """A day projection must not reconcile nutrition totals as a side effect."""
    import nutrition.nutrition_planning_db as nutrition_db

    day_iso = "2099-01-01"
    nutrition_db.ensure_nutrition_planning_schema()
    conn = connections.get_nutrition_db()
    try:
        conn.execute(
            """
            INSERT INTO nutrition_logged_meals
                (log_date, title, meal_name, meal_slot, source, logged_at, created_at, updated_at)
            VALUES (?, 'Read-only meal', 'Read-only meal', 'Meal 1', 'test', ?, ?, ?)
            """,
            (day_iso, f"{day_iso}T08:00:00", f"{day_iso}T08:00:00Z", f"{day_iso}T08:00:00Z"),
        )
        meal_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        conn.execute(
            """
            INSERT INTO nutrition_logged_meal_items
                (logged_meal_id, food_id, food_name, amount, unit, calories,
                 protein_override, carbs_override, fat_override, sugar_override,
                 created_at, updated_at)
            VALUES (?, NULL, 'Read-only item', 300, 'kcal', 300, 0, 0, 0, 0, ?, ?)
            """,
            (meal_id, f"{day_iso}T08:00:00Z", f"{day_iso}T08:00:00Z"),
        )
        conn.commit()
        before = conn.execute("SELECT COUNT(*) FROM weight_logs WHERE date_iso=?", (day_iso,)).fetchone()[0]
    finally:
        conn.close()

    monkeypatch.setattr(
        nutrition_db,
        "ensure_nutrition_planning_schema",
        lambda: (_ for _ in ()).throw(AssertionError("a prepared logging-day read must not initialize schema")),
    )
    payload = nutrition_db.get_logging_day_payload(day_iso)

    conn = connections.get_nutrition_db()
    try:
        after = conn.execute("SELECT COUNT(*) FROM weight_logs WHERE date_iso=?", (day_iso,)).fetchone()[0]
    finally:
        conn.close()
    assert payload["date"] == day_iso
    assert before == after == 0


def test_flask_logging_day_initializes_a_fresh_database_before_the_read(tmp_path, monkeypatch):
    """Flask's existing lifecycle hook owns first-use schema preparation."""
    from flask import Flask

    import nutrition.nutrition_planning_db as nutrition_db
    from nutrition import nutrition_planning_api as planning_api

    nutrition_db_path = tmp_path / "nutrition.sqlite3"
    core_db_path = tmp_path / "core.sqlite3"
    monkeypatch.setattr(connections, "NUTRITION_DB", str(nutrition_db_path))
    monkeypatch.setattr(connections, "CORE_DB", str(core_db_path))
    nutrition_db._SCHEMA_READY_FOR.clear()
    planning_api._SCHEMA_OK_FOR.clear()
    app = Flask(__name__)
    app.register_blueprint(planning_api.nutrition_planning_api)

    response = app.test_client().get("/api/nutrition/logging/day?date=2026-09-22")

    assert response.status_code == 200
    assert response.get_json()["date"] == "2026-09-22"
    with sqlite3.connect(nutrition_db_path) as observer:
        before_read = observer.execute("PRAGMA data_version").fetchone()[0]
        response = app.test_client().get("/api/nutrition/logging/day?date=2026-09-22")
        after_read = observer.execute("PRAGMA data_version").fetchone()[0]
    assert response.status_code == 200
    assert before_read == after_read


def _seed_planned_logging_fixture(client, *, day_label: str = "Mi"):
    import nutrition.nutrition_planning_db as nutrition_db

    food_ids = [
        _seed_test_food("Brot Fixture"),
        _seed_test_food("Apfel Fixture"),
        _seed_test_food("Hähnchen Fixture"),
        _seed_test_food("Milch Fixture"),
    ]
    plan_id = _create_nutrition_plan(
        client,
        "EP Logging Reconcile",
        True,
        [{
            "day": day_label,
            "meals": [
                {"title": "Brot + Marmelade", "time": "06:40", "items": [{"food_id": food_ids[0], "amount": 100, "unit": "g"}]},
                {"title": "Apfel + Whey + Haferflocken (zart) + Milch", "time": "09:30", "items": [{"food_id": food_ids[1], "amount": 100, "unit": "g"}]},
                {"title": "Hähnchen mit Reis", "time": "16:00", "items": [{"food_id": food_ids[2], "amount": 150, "unit": "g"}]},
                {"title": "Abendessen + Milch + Whey", "time": "20:30", "items": [{"food_id": food_ids[3], "amount": 250, "unit": "ml"}]},
            ],
        }],
    )
    nutrition_db._planned_meals_for_date.cache_clear() if hasattr(nutrition_db._planned_meals_for_date, "cache_clear") else None
    conn = connections.get_nutrition_db()
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT s.id, s.slot_index, s.time_text, s.meal_template_id
        FROM nutrition_week_day_slots s
        JOIN nutrition_week_template_days d ON d.id=s.week_template_day_id
        WHERE d.week_template_id=?
        ORDER BY s.slot_index
        """,
        (plan_id,),
    ).fetchall()
    conn.close()
    return plan_id, rows, food_ids


def test_active_nutrition_plan_detail_returns_full_days_meals_slots(client):
    chicken_id = _seed_test_food("Hähnchenbrust")
    _create_nutrition_plan(client, "EP Active", True, [{"day": "Mo", "meals": [{"title": "Lunch", "items": [{"food_id": chicken_id, "amount": 180, "unit": "g"}]}]}])
    body = client.get("/api/v2/actions/nutrition/plans/active/detail", headers=auth()).get_json()
    assert body["ok"] is True
    assert body["days"]
    assert body["days"][0]["meals"]
    assert body["days"][0]["meals"][0]["slots"]


def test_nutrition_active_detail_without_active_plan_returns_structured_error(client):
    _create_nutrition_plan(client, "Inactive A", False, [{"day": "Mo", "meals": [{"title": "Meal", "items": [{"food_id": _seed_test_food("No Active Rice"), "amount": 100, "unit": "g"}]}]}])
    resp = client.get("/api/v2/actions/nutrition/plans/active/detail", headers=auth())
    body = resp.get_json()
    assert resp.status_code == 400
    assert body["ok"] is False
    assert body["error"]["code"] == "no_active_plan"
    assert body["error"]["message"] == "no_active_plan"
    assert body["execution"]["mode"] == "dry_run"
    assert body["execution"]["live_state_changed"] is False


def test_nutrition_patch_without_operations_returns_validation_error(client):
    plan_id = _create_nutrition_plan(client, "EP Patch Empty", False, [{"day": "Mo", "meals": [{"title": "Meal", "items": [{"food_id": _seed_test_food("Reis"), "amount": 100, "unit": "g"}]}]}])
    resp = client.post(f"/api/v2/actions/nutrition/plans/{plan_id}/patch", json={"dry_run": True}, headers=auth())
    assert resp.status_code == 400
    assert resp.get_json()["error"]["code"] == "operations are required"


def test_nutrition_patch_top_level_operations_dry_run_does_not_persist(client):
    gyros_id = _seed_test_food("Gyros Pfanne")
    chicken_id = _seed_test_food("Hähnchenbrust")
    plan_id = _create_nutrition_plan(client, "EP Replace", False, [{"day": "Mo", "meals": [{"title": "Lunch", "items": [{"food_id": gyros_id, "amount": 181, "unit": "g"}]}]}])
    before_ids = _slot_food_ids(client, plan_id)
    body = client.post(
        f"/api/v2/actions/nutrition/plans/{plan_id}/patch",
        json={"dry_run": True, "operations": [{"op": "replace_slot_food", "match": {"food_id": gyros_id}, "replacement": {"food_id": chicken_id, "amount_g": 215}, "scope": "first"}]},
        headers=auth(),
    ).get_json()
    after_ids = _slot_food_ids(client, plan_id)
    assert body["ok"] is True
    assert body["execution"]["mode"] == "dry_run"
    assert body["execution"]["live_state_changed"] is False
    assert before_ids == after_ids


def test_nutrition_replace_slot_food_dry_run_recalculates_slot_and_totals(client):
    gyros_id = _seed_test_food("Gyros Pfanne")
    chicken_id = _seed_test_food("Hähnchenbrust")
    conn = connections.get_nutrition_db()
    conn.execute(
        "UPDATE nutrition_foods SET kcal_per_100=?, p_per_100=?, c_per_100=?, f_per_100=? WHERE id=?",
        (176.0, 18.0, 1.0, 11.0, gyros_id),
    )
    conn.execute(
        "UPDATE nutrition_foods SET kcal_per_100=?, p_per_100=?, c_per_100=?, f_per_100=? WHERE id=?",
        (110.0, 23.0, 0.0, 1.0, chicken_id),
    )
    conn.commit()
    conn.close()
    plan_id = _create_nutrition_plan(client, "EP Macro Recalc", False, [{"day": "Mo", "meals": [{"title": "Lunch", "items": [{"food_id": gyros_id, "amount": 215, "unit": "g"}]}]}])
    body = client.post(
        f"/api/v2/actions/nutrition/plans/{plan_id}/patch",
        json={"dry_run": True, "operations": [{"op": "replace_slot_food", "match": {"food_id": gyros_id}, "replacement": {"food_id": chicken_id, "amount_g": 215}, "scope": "first"}]},
        headers=auth(),
    ).get_json()
    slot = body["after"]["days"][0]["meals"][0]["slots"][0]
    meal_totals = body["after"]["days"][0]["meals"][0]["totals"]
    day_totals = body["after"]["days"][0]["totals"]
    plan_totals = body["after"]["totals"]
    assert slot["food_id"] == chicken_id
    assert slot["food_name"] == "Hähnchenbrust"
    assert slot["amount_g"] == 215
    assert slot["kcal"] == 236.5
    assert slot["protein_g"] == 49.4
    assert slot["carbs_g"] == 0.0
    assert slot["fat_g"] == 2.1
    assert meal_totals == {"kcal": 236.5, "protein_g": 49.4, "carbs_g": 0.0, "fat_g": 2.1}
    assert day_totals == {"kcal": 236.5, "protein_g": 49.4, "carbs_g": 0.0, "fat_g": 2.1}
    assert plan_totals == {"kcal": 236.5, "protein_g": 49.4, "carbs_g": 0.0, "fat_g": 2.1}
    assert body["diff"]["before_counts"]["slots_count"] == body["before"]["counts"]["slots_count"]
    assert body["diff"]["after_counts"]["slots_count"] == body["after"]["counts"]["slots_count"]


def test_nutrition_patch_live_replace_persists(client):
    gyros_id = _seed_test_food("Gyros Live")
    chicken_id = _seed_test_food("Chicken Live")
    plan_id = _create_nutrition_plan(client, "EP Live", False, [{"day": "Mo", "meals": [{"title": "Lunch", "items": [{"food_id": gyros_id, "amount": 181, "unit": "g"}]}]}])
    body = client.post(
        f"/api/v2/actions/nutrition/plans/{plan_id}/patch",
        json={"dry_run": False, "operations": [{"op": "replace_slot_food", "match": {"food_id": gyros_id}, "replacement": {"food_id": chicken_id, "amount_g": 215}, "scope": "first"}]},
        headers=auth(),
    ).get_json()
    assert body["ok"] is True
    assert body["execution"]["mode"] == "write"
    assert chicken_id in _slot_food_ids(client, plan_id)


def test_nutrition_update_slot_amount_write_persists_source_row(client):
    chicken_id = _seed_test_food("Amount Chicken")
    plan_id = _create_nutrition_plan(client, "EP Amount Write", False, [{"day": "Mo", "meals": [{"title": "Lunch", "items": [{"food_id": chicken_id, "amount": 215, "unit": "g"}]}]}])
    detail = client.get(f"/api/v2/actions/nutrition/plans/{plan_id}/detail", headers=auth()).get_json()
    slot_path = detail["days"][0]["meals"][0]["slots"][0]["slot_path"]
    body = client.post(
        f"/api/v2/actions/nutrition/plans/{plan_id}/patch",
        json={"dry_run": False, "operations": [{"op": "update_slot_amount", "match": {"slot_path": slot_path}, "amount": 216, "unit": "g"}]},
        headers=auth(),
    ).get_json()
    conn = connections.get_nutrition_db()
    row = conn.execute(
        """
        SELECT i.amount, i.unit
        FROM nutrition_slot_meal_items i
        JOIN nutrition_slot_meals sm ON sm.id=i.slot_meal_id
        JOIN nutrition_week_template_days d ON d.id=sm.week_template_day_id
        WHERE d.week_template_id=?
        """,
        (plan_id,),
    ).fetchone()
    conn.close()
    assert body["after"]["days"][0]["meals"][0]["slots"][0]["display_amount"] == "216 g"
    assert tuple(row) == (216.0, "g")


def test_nutrition_patch_live_replace_persists_with_empty_placeholder_meals(client):
    gyros_id = _seed_test_food("Gyros Placeholder")
    chicken_id = _seed_test_food("Hähnchenbrust")
    conn = connections.get_nutrition_db()
    conn.execute(
        "UPDATE nutrition_foods SET kcal_per_100=?, p_per_100=?, c_per_100=?, f_per_100=? WHERE id=?",
        (176.0, 18.0, 1.0, 11.0, gyros_id),
    )
    conn.execute(
        "UPDATE nutrition_foods SET kcal_per_100=?, p_per_100=?, c_per_100=?, f_per_100=? WHERE id=?",
        (110.0, 23.0, 0.0, 1.0, chicken_id),
    )
    conn.commit()
    conn.close()
    plan_id = _create_nutrition_plan(client, "Mini-Cut Placeholder", False, [{"day": "Mo", "meals": [{"title": "Meal 1", "items": [{"food_id": gyros_id, "amount": 215, "unit": "g"}]}]}])
    _add_placeholder_meals(plan_id)
    dry = client.post(
        f"/api/v2/actions/nutrition/plans/{plan_id}/patch",
        json={"dry_run": True, "operations": [{"op": "replace_slot_food", "match": {"food_id": gyros_id}, "replacement": {"food_id": chicken_id, "amount_g": 215}, "scope": "first"}]},
        headers=auth(),
    ).get_json()
    live = client.post(
        f"/api/v2/actions/nutrition/plans/{plan_id}/patch",
        json={"dry_run": False, "operations": [{"op": "replace_slot_food", "match": {"food_id": gyros_id}, "replacement": {"food_id": chicken_id, "amount_g": 215}, "scope": "first"}]},
        headers=auth(),
    ).get_json()
    detail = client.get(f"/api/v2/actions/nutrition/plans/{plan_id}/detail", headers=auth()).get_json()
    placeholder_meals = [meal for meal in detail["days"][0]["meals"] if meal.get("empty")]
    assert dry["ok"] is True
    assert live["ok"] is True
    assert live["execution"]["mode"] == "write"
    assert detail["days"][0]["meals"][0]["slots"][0]["food_id"] == chicken_id
    assert detail["days"][0]["meals"][0]["slots"][0]["food_name"] == "Hähnchenbrust"
    assert detail["days"][0]["meals"][0]["slots"][0]["amount_g"] == 215
    assert detail["days"][0]["meals"][0]["slots"][0]["kcal"] == 236.5
    assert detail["days"][0]["meals"][0]["slots"][0]["protein_g"] == 49.4
    assert placeholder_meals
    assert all(meal["placeholder"] is True and meal["empty"] is True for meal in placeholder_meals)
    assert any(w["code"] == "empty_meal_placeholder" for w in detail["warnings"])


def test_nutrition_patch_multiple_matches_without_scope_errors(client):
    rice_id = _seed_test_food("Reis Multi")
    plan_id = _create_nutrition_plan(client, "EP Multi", False, [{"day": "Mo", "meals": [{"title": "One", "items": [{"food_id": rice_id, "amount": 100, "unit": "g"}, {"food_id": rice_id, "amount": 120, "unit": "g"}]}]}])
    resp = client.post(
        f"/api/v2/actions/nutrition/plans/{plan_id}/patch",
        json={"dry_run": True, "operations": [{"op": "update_slot_amount", "match": {"food_id": rice_id}, "amount_g": 215}]},
        headers=auth(),
    )
    assert resp.status_code == 400
    assert resp.get_json()["error"]["code"] == "multiple_matches_require_scope"


def test_nutrition_patch_rename_meal_and_archive(client):
    rice_id = _seed_test_food("Rename Rice")
    plan_id = _create_nutrition_plan(client, "EP Rename", False, [{"day": "Mo", "meals": [{"title": "Old", "items": [{"food_id": rice_id, "amount": 100, "unit": "g"}]}]}])
    rename = client.post(
        f"/api/v2/actions/nutrition/plans/{plan_id}/patch",
        json={"dry_run": False, "operations": [{"op": "rename_meal", "match": {"day_index": 0, "meal_index": 0}, "title": "Post Workout"}]},
        headers=auth(),
    ).get_json()
    archive = client.post(
        f"/api/v2/actions/nutrition/plans/{plan_id}/patch",
        json={"dry_run": False, "operations": [{"op": "archive_plan"}]},
        headers=auth(),
    ).get_json()
    assert rename["after"]["days"][0]["meals"][0]["title"] == "Post Workout"
    assert archive["after"]["archived"] is True


def test_nutrition_patch_update_meal_time_move_meal_and_note_write_persist(client):
    rice_id = _seed_test_food("Move Meal Rice")
    chicken_id = _seed_test_food("Move Meal Chicken")
    plan_id = _create_nutrition_plan(
        client,
        "EP Move Time Note",
        False,
        [{"day": "Mo", "meals": [
            {"title": "Breakfast", "time": "08:00", "items": [{"food_id": rice_id, "amount": 100, "unit": "g"}]},
            {"title": "Lunch", "time": "16:00", "items": [{"food_id": chicken_id, "amount": 215, "unit": "g"}]},
        ]}],
    )
    body = client.post(
        f"/api/v2/actions/nutrition/plans/{plan_id}/patch",
        json={"dry_run": False, "operations": [
            {"op": "update_meal_time", "match": {"day_index": 0, "meal_index": 1}, "time": "15:59"},
            {"op": "set_meal_note", "match": {"day_index": 0, "meal_index": 1}, "note": "Testnotiz"},
            {"op": "move_meal", "match": {"day_index": 0, "meal_index": 1}, "target": {"day_index": 0, "meal_index": 0}},
        ]},
        headers=auth(),
    ).get_json()
    detail = client.get(f"/api/v2/actions/nutrition/plans/{plan_id}/detail", headers=auth()).get_json()
    conn = connections.get_nutrition_db()
    slot_rows = conn.execute(
        """
        SELECT slot_index, time_text, custom_title, note_text
        FROM nutrition_week_day_slots s
        JOIN nutrition_week_template_days d ON d.id=s.week_template_day_id
        WHERE d.week_template_id=? AND d.weekday=0
        ORDER BY slot_index
        """,
        (plan_id,),
    ).fetchall()
    conn.close()
    assert body["ok"] is True
    assert detail["days"][0]["meals"][0]["title"] == "Breakfast"
    assert detail["days"][0]["meals"][1]["title"] == "Lunch"
    assert detail["days"][0]["meals"][1]["time_text"] == "15:59"
    assert detail["days"][0]["meals"][1]["note_text"] == "Testnotiz"
    assert [tuple(row) for row in slot_rows] == [
        (0, "08:00", "Breakfast", None),
        (1, "15:59", "Lunch", "Testnotiz"),
    ]


def test_nutrition_patch_errors_are_structured(client):
    plan_id = _create_nutrition_plan(client, "EP Structured Error", False, [{"day": "Mo", "meals": [{"title": "Meal", "items": [{"food_id": _seed_test_food("Error Rice"), "amount": 100, "unit": "g"}]}]}])
    resp = client.post(
        f"/api/v2/actions/nutrition/plans/{plan_id}/patch",
        json={"dry_run": False, "operations": [{"op": "unknown_nope"}]},
        headers=auth(),
    )
    body = resp.get_json()
    assert resp.status_code == 400
    assert body["ok"] is False
    assert body["error"]["code"] == "unknown_operation"
    assert body["error"]["message"] == "unknown_operation"
    assert body["execution"]["mode"] == "write"
    assert body["execution"]["live_state_changed"] is False


def test_nutrition_replace_meal_items_is_atomic_and_recalculates_preview(client):
    old_food_id = _seed_test_food("Old Meal Item")
    oats_id = _seed_test_food("Oats Replacement")
    skyr_id = _seed_test_food("Skyr Replacement")
    conn = connections.get_nutrition_db()
    conn.execute("UPDATE nutrition_foods SET kcal_per_100=370, p_per_100=13, c_per_100=60, f_per_100=7 WHERE id=?", (oats_id,))
    conn.execute("UPDATE nutrition_foods SET kcal_per_100=63, p_per_100=11, c_per_100=4, f_per_100=0.2 WHERE id=?", (skyr_id,))
    conn.commit()
    conn.close()
    plan_id = _create_nutrition_plan(client, "Replace Meal Items", False, [{"day": "Mo", "meals": [{"title": "Old Meal", "items": [{"food_id": old_food_id, "amount": 100, "unit": "g"}]}]}])

    before = client.get(f"/api/v2/actions/nutrition/plans/{plan_id}/detail", headers=auth()).get_json()
    response = client.post(
        f"/api/v2/actions/nutrition/plans/{plan_id}/patch",
        json={"dry_run": True, "operations": [{"op": "replace_meal_items", "match": {"day_index": 0, "meal_index": 0}, "items": [{"food_id": oats_id, "amount_g": 100}, {"food_id": skyr_id, "amount": 250, "unit": "g"}]}]},
        headers=auth(),
    )
    body = response.get_json()
    after_read = client.get(f"/api/v2/actions/nutrition/plans/{plan_id}/detail", headers=auth()).get_json()

    assert response.status_code == 200 and body["ok"] is True
    assert body["execution"]["mode"] == "dry_run"
    assert body["execution"]["live_state_changed"] is False
    slots = body["after"]["days"][0]["meals"][0]["slots"]
    assert [(slot["food_id"], slot["amount_g"]) for slot in slots] == [(oats_id, 100.0), (skyr_id, 250.0)]
    assert body["after"]["days"][0]["meals"][0]["totals"]["kcal"] == 527.5
    assert after_read == before


def test_nutrition_add_meal_is_atomic_and_infers_piece_unit(client):
    base_id = _seed_test_food("Base Meal Food")
    bar_id = _seed_test_food("Added Piece Bar")
    conn = connections.get_nutrition_db()
    conn.execute("UPDATE nutrition_foods SET unit_default='pcs', portion_g=NULL, common_portion_size=NULL, kcal_per_100=108, p_per_100=1.7, c_per_100=16.2, f_per_100=3.9 WHERE id=?", (bar_id,))
    conn.commit()
    conn.close()
    plan_id = _create_nutrition_plan(client, "Add Meal", False, [{"day": "Mo", "meals": [{"title": "Base", "items": [{"food_id": base_id, "amount_g": 100}]}]}])

    body = client.post(
        f"/api/v2/actions/nutrition/plans/{plan_id}/patch",
        json={"dry_run": True, "operations": [{"op": "add_meal", "target": {"day_index": 0, "meal_index": 1}, "title": "Riegel", "time": "18:30", "items": [{"food_id": bar_id, "amount": 1}]}]},
        headers=auth(),
    ).get_json()
    meal = body["after"]["days"][0]["meals"][1]

    assert meal["title"] == "Riegel" and meal["time_text"] == "18:30"
    assert (meal["slots"][0]["unit"], meal["slots"][0]["display_amount"], meal["slots"][0]["kcal"]) == ("pcs", "1×", 108.0)


def test_nutrition_set_active_plan_dry_run_simulates_without_db_change(client):
    food_id = _seed_test_food("Activate Rice")
    plan_a = _create_nutrition_plan(client, "Plan A", False, [{"day": "Mo", "meals": [{"title": "Meal A", "items": [{"food_id": food_id, "amount": 100, "unit": "g"}]}]}])
    plan_b = _create_nutrition_plan(client, "Plan B", False, [{"day": "Di", "meals": [{"title": "Meal B", "items": [{"food_id": food_id, "amount": 120, "unit": "g"}]}]}])
    before = client.get("/api/v2/actions/nutrition/plans", headers=auth()).get_json()
    body = client.post(
        f"/api/v2/actions/nutrition/plans/{plan_b}/patch",
        json={"dry_run": True, "operations": [{"op": "set_active_plan"}]},
        headers=auth(),
    ).get_json()
    after_list = client.get("/api/v2/actions/nutrition/plans", headers=auth()).get_json()
    assert body["ok"] is True
    assert body["execution"]["mode"] == "dry_run"
    assert body["after"]["is_active"] is True
    assert body["after"]["status"] == "active"
    assert all(not plan["is_active"] for plan in before["plans"])
    assert all(not plan["is_active"] for plan in after_list["plans"])
    assert plan_a in [plan["plan_id"] for plan in after_list["plans"]]


def test_nutrition_set_active_plan_write_persists_and_updates_active_detail(client):
    food_id = _seed_test_food("Persist Active Rice")
    plan_a = _create_nutrition_plan(client, "Plan A", False, [{"day": "Mo", "meals": [{"title": "Meal A", "items": [{"food_id": food_id, "amount": 100, "unit": "g"}]}]}])
    plan_b = _create_nutrition_plan(client, "Plan B", False, [{"day": "Di", "meals": [{"title": "Meal B", "items": [{"food_id": food_id, "amount": 120, "unit": "g"}]}]}])
    body = client.post(
        f"/api/v2/actions/nutrition/plans/{plan_b}/patch",
        json={"dry_run": False, "operations": [{"op": "set_active_plan"}]},
        headers=auth(),
    ).get_json()
    plans = client.get("/api/v2/actions/nutrition/plans", headers=auth()).get_json()["plans"]
    active = client.get("/api/v2/actions/nutrition/plans/active/detail", headers=auth()).get_json()
    active_ids = [plan["plan_id"] for plan in plans if plan["is_active"]]
    assert body["ok"] is True
    assert body["execution"]["mode"] == "write"
    assert body["after"]["plan_id"] == plan_b
    assert body["after"]["is_active"] is True
    assert body["after"]["status"] == "active"
    assert active_ids == [plan_b]
    assert any(plan["plan_id"] == plan_a and plan["is_active"] is False for plan in plans)
    assert active["plan_id"] == plan_b
    assert active["is_active"] is True
    assert active["status"] == "active"


def test_nutrition_plan_list_exposes_consistent_active_status(client):
    food_id = _seed_test_food("List Status Rice")
    plan_a = _create_nutrition_plan(client, "Plan 16", False, [{"day": "Mo", "meals": [{"title": "Meal A", "items": [{"food_id": food_id, "amount": 100, "unit": "g"}]}]}])
    plan_b = _create_nutrition_plan(client, "Plan 17", False, [{"day": "Di", "meals": [{"title": "Meal B", "items": [{"food_id": food_id, "amount": 120, "unit": "g"}]}]}])
    client.post(f"/api/v2/actions/nutrition/plans/{plan_b}/patch", json={"dry_run": False, "operations": [{"op": "set_active_plan"}]}, headers=auth())
    plans = client.get("/api/v2/actions/nutrition/plans", headers=auth()).get_json()["plans"]
    active_ids = [plan["plan_id"] for plan in plans if plan["is_active"]]
    assert active_ids == [plan_b]
    assert any(plan["plan_id"] == plan_b and plan["status"] == "active" for plan in plans)
    assert any(plan["plan_id"] == plan_a and plan["status"] == "inactive" for plan in plans)


def test_nutrition_detail_exposes_unique_slot_paths_and_safe_slot_ids(client):
    rice_id = _seed_test_food("Slot Path Rice")
    chicken_id = _seed_test_food("Slot Path Chicken")
    plan_id = _create_nutrition_plan(
        client,
        "EP Slot Paths",
        True,
        [{"day": "Mo", "meals": [{"title": "Meal 1", "items": [{"food_id": rice_id, "amount": 100, "unit": "g"}, {"food_id": chicken_id, "amount": 150, "unit": "g"}]}]}],
    )
    detail = client.get(f"/api/v2/actions/nutrition/plans/{plan_id}/detail", headers=auth()).get_json()
    slots = [
        slot
        for day in detail["days"]
        for meal in day["meals"]
        for slot in meal["slots"]
    ]
    slot_paths = [slot["slot_path"] for slot in slots]
    assert slots
    assert all(slot_path is not None for slot_path in slot_paths)
    assert len(slot_paths) == len(set(slot_paths))
    assert all(slot_path.startswith("d0:m0:s") for slot_path in slot_paths)
    assert all(slot["slot_id"] is None for slot in slots)
    assert all("meal_id" not in slot for slot in slots)


def test_nutrition_patch_supports_slot_path_match_dry_run_and_write(client):
    gyros_id = _seed_test_food("Slot Path Gyros")
    chicken_id = _seed_test_food("Slot Path Chicken 2")
    plan_id = _create_nutrition_plan(
        client,
        "EP Slot Path Patch",
        False,
        [{"day": "Mo", "meals": [{"title": "Lunch", "items": [{"food_id": gyros_id, "amount": 181, "unit": "g"}]}]}],
    )
    before = client.get(f"/api/v2/actions/nutrition/plans/{plan_id}/detail", headers=auth()).get_json()
    slot_path = before["days"][0]["meals"][0]["slots"][0]["slot_path"]
    dry = client.post(
        f"/api/v2/actions/nutrition/plans/{plan_id}/patch",
        json={"dry_run": True, "operations": [{"op": "replace_slot_food", "match": {"slot_path": slot_path}, "replacement": {"food_id": chicken_id, "amount_g": 215}}]},
        headers=auth(),
    ).get_json()
    live = client.post(
        f"/api/v2/actions/nutrition/plans/{plan_id}/patch",
        json={"dry_run": False, "operations": [{"op": "replace_slot_food", "match": {"slot_path": slot_path}, "replacement": {"food_id": chicken_id, "amount_g": 215}}]},
        headers=auth(),
    ).get_json()
    detail = client.get(f"/api/v2/actions/nutrition/plans/{plan_id}/detail", headers=auth()).get_json()
    assert dry["after"]["days"][0]["meals"][0]["slots"][0]["food_id"] == chicken_id
    assert live["after"]["days"][0]["meals"][0]["slots"][0]["food_id"] == chicken_id
    assert detail["days"][0]["meals"][0]["slots"][0]["food_id"] == chicken_id


def test_nutrition_replace_slot_food_dry_run_updates_display_amount_and_macros(client):
    chicken_id = _seed_test_food("Hähnchenbrust")
    conn = connections.get_nutrition_db()
    conn.execute(
        "UPDATE nutrition_foods SET kcal_per_100=?, p_per_100=?, c_per_100=?, f_per_100=? WHERE id=?",
        (110.0, 23.0, 0.0, 1.0, chicken_id),
    )
    before_override_count = conn.execute("SELECT COUNT(*) FROM nutrition_slot_meal_overrides").fetchone()[0]
    conn.commit()
    conn.close()
    plan_id = _create_nutrition_plan(
        client,
        "EP Display Amount",
        False,
        [{"day": "Mo", "meals": [{"title": "Lunch", "items": [{"food_id": chicken_id, "amount": 215, "unit": "g"}]}]}],
    )
    detail = client.get(f"/api/v2/actions/nutrition/plans/{plan_id}/detail", headers=auth()).get_json()
    slot_path = detail["days"][0]["meals"][0]["slots"][0]["slot_path"]
    body = client.post(
        f"/api/v2/actions/nutrition/plans/{plan_id}/patch",
        json={"dry_run": True, "operations": [{"op": "replace_slot_food", "match": {"slot_path": slot_path}, "replacement": {"food_id": chicken_id, "amount_g": 216}}]},
        headers=auth(),
    ).get_json()
    conn = connections.get_nutrition_db()
    after_override_count = conn.execute("SELECT COUNT(*) FROM nutrition_slot_meal_overrides").fetchone()[0]
    conn.close()
    slot = body["after"]["days"][0]["meals"][0]["slots"][0]
    assert slot["amount"] == 216
    assert slot["amount_g"] == 216
    assert slot["unit"] == "g"
    assert slot["display_amount"] == "216 g"
    assert slot["kcal"] == 237.6
    assert slot["protein_g"] == 49.7
    assert slot["carbs_g"] == 0.0
    assert slot["fat_g"] == 2.2
    assert before_override_count == after_override_count


def test_nutrition_patch_invalid_slot_path_returns_structured_error(client):
    rice_id = _seed_test_food("Invalid Slot Path Rice")
    plan_id = _create_nutrition_plan(client, "EP Invalid Slot Path", False, [{"day": "Mo", "meals": [{"title": "Meal", "items": [{"food_id": rice_id, "amount": 100, "unit": "g"}]}]}])
    resp = client.post(
        f"/api/v2/actions/nutrition/plans/{plan_id}/patch",
        json={"dry_run": True, "operations": [{"op": "update_slot_amount", "match": {"slot_path": "bad-path"}, "amount_g": 215}]},
        headers=auth(),
    )
    body = resp.get_json()
    assert resp.status_code == 400
    assert body["ok"] is False
    assert body["error"]["code"] == "invalid_slot_path"
    assert body["error"]["message"] == "invalid_slot_path"


def test_nutrition_delete_empty_meals_dry_run_and_write(client):
    rice_id = _seed_test_food("Delete Empty Rice")
    plan_id = _create_nutrition_plan(client, "EP Delete Empty", False, [{"day": "Mo", "meals": [{"title": "Meal 1", "items": [{"food_id": rice_id, "amount": 100, "unit": "g"}]}]}])
    _add_placeholder_meals(plan_id)
    before = client.get(f"/api/v2/actions/nutrition/plans/{plan_id}/detail", headers=auth()).get_json()
    dry = client.post(
        f"/api/v2/actions/nutrition/plans/{plan_id}/patch",
        json={"dry_run": True, "operations": [{"op": "delete_empty_meals"}]},
        headers=auth(),
    ).get_json()
    mid = client.get(f"/api/v2/actions/nutrition/plans/{plan_id}/detail", headers=auth()).get_json()
    live = client.post(
        f"/api/v2/actions/nutrition/plans/{plan_id}/patch",
        json={"dry_run": False, "operations": [{"op": "delete_empty_meals"}]},
        headers=auth(),
    ).get_json()
    after = client.get(f"/api/v2/actions/nutrition/plans/{plan_id}/detail", headers=auth()).get_json()
    assert len(before["days"][0]["meals"]) > len(dry["after"]["days"][0]["meals"])
    assert len(before["days"][0]["meals"]) == len(mid["days"][0]["meals"])
    assert len(after["days"][0]["meals"]) == 1
    assert after["days"][0]["meals"][0]["title"] == "Meal 1"
    assert after["counts"]["meals_count"] == 1
    assert after["counts"]["slots_count"] == 1
    assert after["empty_placeholder_count"] == 0
    assert live["after"]["empty_placeholder_count"] == 0


def test_nutrition_create_food_and_meal_template_and_materialize_from_template(client):
    seed_food_id = _seed_test_food("Seed Food Materialize")
    plan_id = _create_nutrition_plan(client, "EP Template Materialize", False, [{"day": "Mo", "meals": [{"title": "Meal 1", "items": [{"food_id": seed_food_id, "amount": 10, "unit": "g"}]}]}])
    create_food_resp = client.post(
        f"/api/v2/actions/nutrition/plans/{plan_id}/patch",
        json={"dry_run": False, "operations": [{"op": "create_food", "food": {"name": "Operator Oats", "kcal_per_100g": 380, "protein_g_per_100g": 12, "carbs_g_per_100g": 60, "fat_g_per_100g": 7, "default_unit": "g"}}]},
        headers=auth(),
    ).get_json()
    new_food_id = create_food_resp["validation"]["created_resources"][0]["food_id"]
    create_tpl_resp = client.post(
        f"/api/v2/actions/nutrition/plans/{plan_id}/patch",
        json={"dry_run": False, "operations": [{"op": "create_meal_template", "template": {"title": "Operator Bowl", "items": [{"food_id": new_food_id, "amount": 80, "unit": "g"}]}}]},
        headers=auth(),
    ).get_json()
    template_id = create_tpl_resp["validation"]["created_resources"][0]["template_id"]
    add_tpl_resp = client.post(
        f"/api/v2/actions/nutrition/plans/{plan_id}/patch",
        json={"dry_run": False, "operations": [{"op": "add_meal_from_template", "target": {"day_index": 0, "meal_index": 1}, "template_id": template_id, "materialize": True}]},
        headers=auth(),
    ).get_json()
    detail = client.get(f"/api/v2/actions/nutrition/plans/{plan_id}/detail", headers=auth()).get_json()
    added_meal = detail["days"][0]["meals"][1]
    conn = connections.get_nutrition_db()
    overrides = conn.execute("SELECT COUNT(*) FROM nutrition_slot_meal_overrides").fetchone()[0]
    conn.close()
    assert create_food_resp["ok"] is True
    assert create_tpl_resp["ok"] is True
    assert add_tpl_resp["ok"] is True
    assert added_meal["title"] == "Operator Bowl"
    assert added_meal["slots"][0]["food_id"] == new_food_id
    assert overrides == 0


def test_nutrition_update_food_and_delete_food_and_template_cleanup(client):
    seed_food_id = _seed_test_food("Seed Food Cleanup")
    plan_id = _create_nutrition_plan(client, "EP Food Cleanup", False, [{"day": "Mo", "meals": [{"title": "Meal 1", "items": [{"food_id": seed_food_id, "amount": 10, "unit": "g"}]}]}])
    create_food_resp = client.post(
        f"/api/v2/actions/nutrition/plans/{plan_id}/patch",
        json={"dry_run": False, "operations": [{"op": "create_food", "food": {"name": "Operator Cleanup Food", "kcal_per_100g": 380, "protein_g_per_100g": 12, "carbs_g_per_100g": 60, "fat_g_per_100g": 7, "default_unit": "pcs"}}]},
        headers=auth(),
    ).get_json()
    food_id = create_food_resp["validation"]["created_resources"][0]["food_id"]
    update_food_resp = client.post(
        f"/api/v2/actions/nutrition/plans/{plan_id}/patch",
        json={"dry_run": False, "operations": [{"op": "update_food", "food_id": food_id, "patch": {"name": "Operator Cleanup Food v2", "default_unit": "ml", "kcal_per_100g": 55}}]},
        headers=auth(),
    ).get_json()
    create_tpl_resp = client.post(
        f"/api/v2/actions/nutrition/plans/{plan_id}/patch",
        json={"dry_run": False, "operations": [{"op": "create_meal_template", "template": {"title": "Operator Cleanup Template", "items": [{"food_id": food_id, "amount": 1, "unit": "ml"}]}}]},
        headers=auth(),
    ).get_json()
    template_id = create_tpl_resp["validation"]["created_resources"][0]["template_id"]
    update_tpl_resp = client.post(
        f"/api/v2/actions/nutrition/plans/{plan_id}/patch",
        json={"dry_run": False, "operations": [{"op": "update_meal_template", "template_id": template_id, "title": "Operator Cleanup Template v2"}]},
        headers=auth(),
    ).get_json()
    delete_tpl_resp = client.post(
        f"/api/v2/actions/nutrition/plans/{plan_id}/patch",
        json={"dry_run": False, "operations": [{"op": "delete_meal_template", "template_id": template_id}]},
        headers=auth(),
    ).get_json()
    delete_food_resp = client.post(
        f"/api/v2/actions/nutrition/plans/{plan_id}/patch",
        json={"dry_run": False, "operations": [{"op": "delete_food", "food_id": food_id, "force": True}]},
        headers=auth(),
    ).get_json()
    conn = connections.get_nutrition_db()
    food_row = conn.execute("SELECT name, unit_default, kcal_per_100 FROM nutrition_foods WHERE id=?", (food_id,)).fetchone()
    template_row = conn.execute("SELECT title FROM nutrition_meal_templates WHERE id=?", (template_id,)).fetchone()
    conn.close()
    assert update_food_resp["ok"] is True
    assert update_tpl_resp["ok"] is True
    assert delete_tpl_resp["ok"] is True
    assert delete_food_resp["ok"] is True
    assert update_food_resp["execution"]["mode"] == "write"
    assert food_row is None
    assert template_row is None


def test_nutrition_overwrite_plan_rebuilds_source_rows_transactionally(client):
    chicken_id = _seed_test_food("Overwrite Chicken")
    rice_id = _seed_test_food("Overwrite Rice")
    plan_id = _create_nutrition_plan(client, "EP Overwrite", False, [{"day": "Mo", "meals": [{"title": "Old", "items": [{"food_id": chicken_id, "amount": 100, "unit": "g"}]}]}])
    body = client.post(
        f"/api/v2/actions/nutrition/plans/{plan_id}/patch",
        json={
            "dry_run": False,
            "operations": [
                {
                    "op": "overwrite_plan",
                    "days": [
                        {"day": "Mo", "meals": [{"title": "Neu", "items": [{"food_id": rice_id, "amount": 80, "unit": "g"}]}]},
                        {"day": "Di", "meals": [{"title": "Zwei", "items": [{"food_id": chicken_id, "amount": 215, "unit": "g"}]}]},
                    ],
                }
            ],
        },
        headers=auth(),
    ).get_json()
    detail = client.get(f"/api/v2/actions/nutrition/plans/{plan_id}/detail", headers=auth()).get_json()
    assert body["ok"] is True
    assert detail["days"][0]["meals"][0]["title"] == "Neu"
    assert detail["days"][0]["meals"][0]["slots"][0]["food_id"] == rice_id
    assert detail["days"][1]["meals"][0]["slots"][0]["food_id"] == chicken_id


def test_today_plan_payload_keeps_pcs_units_without_fake_grams(client, monkeypatch):
    from nutrition import nutrition_planning_api as planning_api

    banana_id = _seed_test_food("Banane")
    conn = connections.get_nutrition_db()
    conn.execute(
        "UPDATE nutrition_foods SET unit_default=?, common_portion_size=?, kcal_per_100=?, p_per_100=?, c_per_100=?, f_per_100=? WHERE id=?",
        ("pcs", 1.0, 105.0, 1.3, 27.0, 0.3, banana_id),
    )
    conn.commit()
    conn.close()
    _create_nutrition_plan(client, "EP PCS", True, [{"day": _today_day_label(), "meals": [{"title": "Snack", "items": [{"food_id": banana_id, "amount": 1, "unit": "pcs"}]}]}])
    planning_api._TODAY_PLAN_CACHE.clear()
    monkeypatch.setattr(planning_api, "_get_core_day_cached", lambda _date_iso: {})
    body = planning_api.build_today_plan_payload(include_items=True, bypass_cache=True)
    item = body["meals"][0]["items"][0]
    assert item["amount"] == 1
    assert item["unit"] == "pcs"
    assert item["amount_g"] is None
    assert item["amount_pcs"] == 1
    assert item["display_amount"] == "1×"


def test_pcs_food_macro_factor_uses_gram_portion():
    from nutrition.nutrition_planning_db import _food_macro_factor

    # One 40g slice of bread represents 0.4 units of per-100g macros.
    assert _food_macro_factor(1, "pcs", "pcs", 40, 1) == 0.4
    # A malformed common portion of 1 falls back to the actual portion weight.
    assert _food_macro_factor(1, "pcs", "pcs", 1, 120) == 1.2
    # Legacy pcs foods without a gram equivalent store macros per piece.
    assert _food_macro_factor(2, "pcs", "pcs", None, None) == 2.0


def test_nutrition_plan_detail_resolves_piece_food_macros_without_fake_grams(client):
    bar_id = _seed_test_food("Riegel pro Stück")
    apple_id = _seed_test_food("Apfel mit Portionsgewicht")
    conn = connections.get_nutrition_db()
    conn.execute("UPDATE nutrition_foods SET unit_default='pcs', portion_g=NULL, common_portion_size=NULL, kcal_per_100=108, p_per_100=1.7, c_per_100=16.2, f_per_100=3.9 WHERE id=?", (bar_id,))
    conn.execute("UPDATE nutrition_foods SET unit_default='pcs', portion_g=180, common_portion_size=180, kcal_per_100=53, p_per_100=0, c_per_100=13, f_per_100=0 WHERE id=?", (apple_id,))
    conn.commit()
    conn.close()
    plan_id = _create_nutrition_plan(client, "Piece Stable", False, [{"day": "Mo", "meals": [{"title": "Snacks", "items": [{"food_id": bar_id, "amount": 1, "unit": "pcs"}, {"food_id": apple_id, "amount": 1, "unit": "pcs"}]}]}])

    detail = client.get(f"/api/v2/actions/nutrition/plans/{plan_id}/detail", headers=auth()).get_json()
    bar, apple = detail["days"][0]["meals"][0]["slots"]

    assert (bar["amount"], bar["amount_pcs"], bar["amount_g"], bar["display_amount"]) == (1.0, 1.0, None, "1×")
    assert bar["macro_resolution"] == "resolved" and bar["macro_basis"] == "per_piece"
    assert (bar["kcal"], bar["carbs_g"]) == (108.0, 16.2)
    assert (apple["amount"], apple["amount_pcs"], apple["amount_g"], apple["display_amount"]) == (1.0, 1.0, None, "1×")
    assert apple["macro_resolution"] == "resolved" and apple["macro_basis"] == "per_100g"
    assert (apple["kcal"], apple["carbs_g"]) == (95.4, 23.4)
    assert detail["days"][0]["meals"][0]["totals"]["kcal"] == 203.4
    assert not any(warning.get("code") == "unresolved_item_macros" for warning in detail.get("warnings") or [])

    patched = client.post(
        f"/api/v2/actions/nutrition/plans/{plan_id}/patch",
        json={"dry_run": True, "operations": [{"op": "replace_meal_items", "match": {"day_index": 0, "meal_index": 0}, "items": [{"food_id": bar_id, "amount": 2}]}]},
        headers=auth(),
    ).get_json()
    inferred = patched["after"]["days"][0]["meals"][0]["slots"][0]
    assert (inferred["amount"], inferred["unit"], inferred["display_amount"], inferred["kcal"]) == (2.0, "pcs", "2×", 216.0)


def test_today_plan_payload_keeps_ml_units_without_fake_grams(client, monkeypatch):
    from nutrition import nutrition_planning_api as planning_api

    milk_id = _seed_test_food("Milch Today")
    conn = connections.get_nutrition_db()
    conn.execute(
        "UPDATE nutrition_foods SET unit_default=?, kcal_per_100=?, p_per_100=?, c_per_100=?, f_per_100=? WHERE id=?",
        ("ml", 47.0, 3.4, 4.8, 1.5, milk_id),
    )
    conn.commit()
    conn.close()
    _create_nutrition_plan(client, "EP ML", True, [{"day": _today_day_label(), "meals": [{"title": "Drink", "items": [{"food_id": milk_id, "amount": 250, "unit": "ml"}]}]}])
    planning_api._TODAY_PLAN_CACHE.clear()
    monkeypatch.setattr(planning_api, "_get_core_day_cached", lambda _date_iso: {})
    body = planning_api.build_today_plan_payload(include_items=True, bypass_cache=True)
    item = body["meals"][0]["items"][0]
    assert item["amount"] == 250
    assert item["unit"] == "ml"
    assert item["amount_g"] is None
    assert item["display_amount"] == "250 ml"


def test_today_plan_and_dashboard_ignore_empty_placeholder_meals(client, monkeypatch):
    from nutrition import nutrition_planning_api as planning_api

    rice_id = _seed_test_food("Dashboard Rice")
    plan_id = _create_nutrition_plan(client, "EP Dashboard", True, [{"day": _today_day_label(), "meals": [{"title": "Meal 1", "items": [{"food_id": rice_id, "amount": 100, "unit": "g"}]}]}])
    _add_placeholder_meals(plan_id)
    planning_api._TODAY_PLAN_CACHE.clear()
    monkeypatch.setattr(planning_api, "_get_core_day_cached", lambda _date_iso: {})
    today = planning_api.build_today_plan_payload(include_items=True, bypass_cache=True)
    assert len(today["meals"]) == 1
    assert today["meals"][0]["name"] == "Meal 1"
    assert all(meal["name"] != "Meal 5" for meal in today["meals"])


def test_active_detail_keeps_slot_path_and_chicken_counts_after_replace(client):
    gyros_id = _seed_test_food("Gyros Stable")
    chicken_id = _seed_test_food("Hähnchenbrust")
    plan_id = _create_nutrition_plan(
        client,
        "Mini-Cut Stable",
        True,
        [{"day": "Mo", "meals": [{"title": f"Meal {idx+1}", "items": [{"food_id": gyros_id, "amount": 215, "unit": "g"}]} for idx in range(7)]}],
    )
    client.post(
        f"/api/v2/actions/nutrition/plans/{plan_id}/patch",
        json={"dry_run": False, "operations": [{"op": "replace_slot_food", "match": {"food_id": gyros_id}, "replacement": {"food_id": chicken_id, "amount_g": 215}, "scope": "all"}]},
        headers=auth(),
    )
    detail = client.get("/api/v2/actions/nutrition/plans/active/detail", headers=auth()).get_json()
    slots = [slot for day in detail["days"] for meal in day["meals"] for slot in meal["slots"]]
    assert detail["is_active"] is True
    assert len({slot["slot_path"] for slot in slots}) == len(slots)
    assert sum(1 for slot in slots if slot["food_id"] == gyros_id) == 0
    assert sum(1 for slot in slots if slot["food_id"] == chicken_id and slot["amount_g"] == 215) == 7


def test_nutrition_live_replace_writes_source_tables_without_overrides_or_cross_meal_injection(client):
    oats_id = _seed_test_food("Oats Stable")
    gyros_id = _seed_test_food("Gyros Source")
    chicken_id = _seed_test_food("Hähnchenbrust")
    rice_id = _seed_test_food("Rice Stable")
    plan_id = _create_nutrition_plan(
        client,
        "EP Source Write",
        False,
        [{"day": "Mo", "meals": [
            {"title": "Frühstück", "items": [{"food_id": oats_id, "amount": 100, "unit": "g"}]},
            {"title": "Lunch", "items": [{"food_id": gyros_id, "amount": 215, "unit": "g"}]},
            {"title": "Abendessen", "items": [{"food_id": rice_id, "amount": 180, "unit": "g"}]},
        ]}],
    )
    body = client.post(
        f"/api/v2/actions/nutrition/plans/{plan_id}/patch",
        json={"dry_run": False, "operations": [{"op": "replace_slot_food", "match": {"food_id": gyros_id}, "replacement": {"food_id": chicken_id, "amount_g": 215}, "scope": "all"}]},
        headers=auth(),
    ).get_json()
    detail = client.get(f"/api/v2/actions/nutrition/plans/{plan_id}/detail", headers=auth()).get_json()
    conn = connections.get_nutrition_db()
    overrides = conn.execute("SELECT COUNT(*) FROM nutrition_slot_meal_overrides").fetchone()[0]
    source_items = conn.execute(
        """
        SELECT sm.meal_title, i.food_id, i.amount, i.unit
        FROM nutrition_slot_meal_items i
        JOIN nutrition_slot_meals sm ON sm.id=i.slot_meal_id
        ORDER BY sm.slot_index, i.id
        """
    ).fetchall()
    conn.close()
    assert body["ok"] is True
    assert overrides == 0
    breakfast = detail["days"][0]["meals"][0]["slots"]
    lunch = detail["days"][0]["meals"][1]["slots"]
    dinner = detail["days"][0]["meals"][2]["slots"]
    assert breakfast[0]["food_id"] == oats_id
    assert lunch[0]["food_id"] == chicken_id
    assert lunch[0]["amount_g"] == 215
    assert dinner[0]["food_id"] == rice_id
    assert [(row[0], row[1], row[2], row[3]) for row in source_items] == [
        ("Frühstück", oats_id, 100.0, "g"),
        ("Lunch", chicken_id, 215.0, "g"),
        ("Abendessen", rice_id, 180.0, "g"),
    ]


def test_planning_week_read_prefers_slot_meal_items_over_template_items(client):
    from nutrition.nutrition_planning_db import _build_week_plan_payload_wrapper

    gyros_id = _seed_test_food("Gyros Planning Template")
    chicken_id = _seed_test_food("Hähnchenbrust")
    plan_id = _create_nutrition_plan(
        client,
        "Planung Truth",
        True,
        [{"day": "Mo", "meals": [{"title": "Hähnchen mit Reis", "items": [{"food_id": chicken_id, "amount": 215, "unit": "g"}]}]}],
    )
    conn = connections.get_nutrition_db()
    day_id = conn.execute(
        "SELECT id FROM nutrition_week_template_days WHERE week_template_id=? AND weekday=0",
        (plan_id,),
    ).fetchone()[0]
    slot_row = conn.execute(
        "SELECT id, meal_template_id FROM nutrition_week_day_slots WHERE week_template_day_id=? AND slot_index=0",
        (day_id,),
    ).fetchone()
    slot_meal_row = conn.execute(
        "SELECT id FROM nutrition_slot_meals WHERE week_template_day_id=? AND slot_index=0",
        (day_id,),
    ).fetchone()
    if slot_meal_row:
        conn.execute("UPDATE nutrition_slot_meals SET meal_title=? WHERE id=?", ("Hähnchen mit Reis", slot_meal_row[0]))
        slot_meal_id = slot_meal_row[0]
    else:
        conn.execute(
            """
            INSERT INTO nutrition_slot_meals (week_template_day_id, slot_index, meal_title, multiplier, template_id, created_at, updated_at)
            VALUES (?, 0, ?, 1, NULL, ?, ?)
            """,
            (day_id, "Hähnchen mit Reis", "2026-05-27T10:00:00Z", "2026-05-27T10:00:00Z"),
        )
        slot_meal_id = conn.execute(
            "SELECT id FROM nutrition_slot_meals WHERE week_template_day_id=? AND slot_index=0 ORDER BY id DESC LIMIT 1",
            (day_id,),
        ).fetchone()[0]
    conn.execute("DELETE FROM nutrition_slot_meal_items WHERE slot_meal_id=?", (slot_meal_id,))
    conn.execute(
        """
        INSERT INTO nutrition_slot_meal_items
            (slot_meal_id, amount, unit, name_raw, food_id, item_type, calories, display_text, client_req_id, fingerprint, created_at, updated_at)
        VALUES (?, ?, 'g', 'Hähnchenbrust', ?, 'food', NULL, NULL, NULL, 'planning-chicken', ?, ?)
        """,
        (slot_meal_id, 215.0, chicken_id, "2026-05-27T10:00:00Z", "2026-05-27T10:00:00Z"),
    )
    conn.execute("UPDATE nutrition_meal_templates SET title=? WHERE id=?", ("Gyros Pfanne + Reis", slot_row[1]))
    conn.execute(
        "UPDATE nutrition_meal_ingredients SET food_id=?, amount=?, unit=? WHERE meal_template_id=?",
        (gyros_id, 215.0, "g", slot_row[1]),
    )
    conn.execute(
        """
        INSERT INTO nutrition_slot_meal_overrides (slot_id, meal_template_id, ingredients_json, updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(slot_id) DO UPDATE SET ingredients_json=excluded.ingredients_json, updated_at=excluded.updated_at
        """,
        (slot_row[0], slot_row[1], '[{"food_id": %d, "food_name": "Gyros Planning Template", "amount": 215, "unit": "g"}]' % gyros_id, "2026-05-27T10:00:00Z"),
    )
    conn.commit()
    conn.close()
    body = _build_week_plan_payload_wrapper(plan_id)
    slot = body["week_template"]["days"][0]["slots"][0]
    template_items = body["meal_ingredients_map"][str(slot_row[1])] if str(slot_row[1]) in body["meal_ingredients_map"] else body["meal_ingredients_map"][slot_row[1]]
    assert slot["meal_title"] == "Hähnchen mit Reis"
    assert slot["display_title"] == "Hähnchen mit Reis"
    assert slot["source_template_title"] == "Gyros Pfanne + Reis"
    assert slot["items"][0]["food_id"] == chicken_id
    assert slot["items"][0]["name"] == "Hähnchenbrust"
    assert template_items[0]["food_id"] == gyros_id


def test_planning_week_read_matches_gpt_write_and_ignores_override_truth(client, monkeypatch):
    from nutrition import nutrition_planning_api as planning_api
    from nutrition.nutrition_planning_db import _build_week_plan_payload_wrapper

    gyros_id = _seed_test_food("Gyros Planning Replace")
    chicken_id = _seed_test_food("Hähnchenbrust")
    monday = date.today() - timedelta(days=date.today().weekday())
    plan_id = _create_nutrition_plan(
        client,
        "Planung GPT Sync",
        True,
        [{"day": "Mo", "meals": [{"title": "Lunch", "items": [{"food_id": gyros_id, "amount": 215, "unit": "g"}]}]}],
    )
    patch = client.post(
        f"/api/v2/actions/nutrition/plans/{plan_id}/patch",
        json={"dry_run": False, "operations": [{"op": "replace_slot_food", "match": {"food_id": gyros_id}, "replacement": {"food_id": chicken_id, "amount_g": 215}, "scope": "all"}, {"op": "rename_meal", "match": {"day_index": 0, "meal_index": 0}, "title": "Hähnchen mit Reis"}]},
        headers=auth(),
    ).get_json()
    conn = connections.get_nutrition_db()
    slot_row = conn.execute(
        """
            SELECT s.id, s.meal_template_id
            FROM nutrition_week_day_slots s
            JOIN nutrition_week_template_days d ON d.id=s.week_template_day_id
            WHERE d.week_template_id=? AND d.weekday=?
            ORDER BY s.slot_index LIMIT 1
            """,
            (plan_id, 0),
        ).fetchone()
    conn.execute(
        """
        INSERT INTO nutrition_slot_meal_overrides (slot_id, meal_template_id, ingredients_json, updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(slot_id) DO UPDATE SET ingredients_json=excluded.ingredients_json, updated_at=excluded.updated_at
        """,
        (slot_row[0], slot_row[1], '[{"food_id": %d, "food_name": "Gyros Planning Replace", "amount": 215, "unit": "g"}]' % gyros_id, "2026-05-27T10:00:00Z"),
    )
    override_count = conn.execute("SELECT COUNT(*) FROM nutrition_slot_meal_overrides").fetchone()[0]
    conn.commit()
    conn.close()
    detail = client.get("/api/v2/actions/nutrition/plans/active/detail", headers=auth()).get_json()
    week = _build_week_plan_payload_wrapper(plan_id)
    planning_api._TODAY_PLAN_CACHE.clear()
    monkeypatch.setattr(planning_api, "_berlin_today", lambda: monday)
    monkeypatch.setattr(planning_api, "_get_core_day_cached", lambda _date_iso: {})
    today = planning_api.build_today_plan_payload(include_items=True, bypass_cache=True)
    active_day = detail["days"][0]
    week_day = next(day for day in week["week_template"]["days"] if int(day["weekday"]) == 0)
    active_slot = active_day["meals"][0]["slots"][0]
    week_slot = week_day["slots"][0]
    today_item = today["meals"][0]["items"][0]
    assert override_count == 1
    assert patch["ok"] is True
    assert active_slot["food_id"] == chicken_id
    assert active_slot["amount_g"] == 215
    assert week_slot["meal_title"] == "Hähnchen mit Reis"
    assert week_slot["display_title"] == "Hähnchen mit Reis"
    assert week_slot["source_template_title"] is None
    assert week_slot["items"][0]["food_id"] == chicken_id
    assert week_slot["items"][0]["display_amount"] == "215 g"
    assert today["meals"][0]["name"] == "Hähnchen mit Reis"
    assert today_item["name"] == "Hähnchenbrust"
    assert today_item["display_amount"] == "215 g"


def test_planning_slot_ingredient_write_updates_slot_source_not_overrides(client):
    from nutrition.nutrition_planning_db import _build_week_plan_payload_wrapper, save_slot_override

    chicken_id = _seed_test_food("Planning Chicken Source")
    rice_id = _seed_test_food("Planning Rice Source")
    plan_id = _create_nutrition_plan(
        client,
        "Planung Write Source",
        True,
        [{"day": "Mo", "meals": [{"title": "Lunch", "items": [{"food_id": chicken_id, "amount": 215, "unit": "g"}]}]}],
    )
    week = _build_week_plan_payload_wrapper(plan_id)
    slot = week["week_template"]["days"][0]["slots"][0]
    body, err = save_slot_override(
        slot["id"],
        [{"food_id": rice_id, "food_name": "Planning Rice Source", "amount": 80, "unit": "g"}],
    )
    conn = connections.get_nutrition_db()
    overrides = conn.execute("SELECT COUNT(*) FROM nutrition_slot_meal_overrides").fetchone()[0]
    source_items = conn.execute(
        """
        SELECT i.food_id, i.amount, i.unit
        FROM nutrition_slot_meal_items i
        JOIN nutrition_slot_meals sm ON sm.id=i.slot_meal_id
        JOIN nutrition_week_template_days d ON d.id=sm.week_template_day_id
        WHERE d.week_template_id=?
        ORDER BY i.id
        """,
        (plan_id,),
    ).fetchall()
    conn.close()
    refreshed = _build_week_plan_payload_wrapper(plan_id)
    refreshed_slot = refreshed["week_template"]["days"][0]["slots"][0]
    assert err is None
    assert body["slot_id"] == slot["id"]
    assert overrides == 0
    assert [(row[0], row[1], row[2]) for row in source_items] == [(rice_id, 80.0, "g")]
    assert refreshed_slot["items"][0]["food_id"] == rice_id
    assert refreshed_slot["items"][0]["display_amount"] == "80 g"


def test_update_logged_meal_reconciles_stale_planned_slot_and_writes_status(client):
    import nutrition.nutrition_planning_db as nutrition_db

    _, slot_rows, food_ids = _seed_planned_logging_fixture(client, day_label="Mi")
    conn = connections.get_nutrition_db()
    conn.row_factory = sqlite3.Row
    now = "2026-05-27T12:00:00"
    conn.execute(
        """
        INSERT INTO nutrition_logged_meals
            (id, log_date, slot_id, title, meal_slot, source, created_from_planned_slot_id, created_from_template_id,
             edited_from_template, original_planned_time, logged_at, created_at, updated_at)
        VALUES (?, '2026-05-27', NULL, 'Schule', 'Meal 2', 'planned', 999999, ?, 0, '09:30', '2026-05-27T11:40:00', ?, ?)
        """,
        (16363, int(slot_rows[1]["meal_template_id"]), now, now),
    )
    conn.execute(
        """
        INSERT INTO nutrition_logged_meal_items
            (logged_meal_id, food_id, food_name, amount, unit, item_type, sort_index, created_at, updated_at)
        VALUES (?, ?, 'Apfel Fixture', 100, 'g', 'food', 0, ?, ?)
        """,
        (16363, int(food_ids[1]), now, now),
    )
    before_override_count = conn.execute("SELECT COUNT(*) FROM nutrition_slot_meal_overrides").fetchone()[0]
    conn.commit()
    conn.close()

    payload, err = nutrition_db.update_logged_meal(16363, {"title": "Schule", "logged_at": "2026-05-27T11:40:00"})

    assert err is None
    conn = connections.get_nutrition_db()
    conn.row_factory = sqlite3.Row
    meal_row = conn.execute(
        "SELECT created_from_planned_slot_id, edited_from_template FROM nutrition_logged_meals WHERE id=16363"
    ).fetchone()
    status_row = conn.execute(
        "SELECT slot_id, status, shifted_time_text, logged_meal_id FROM nutrition_planned_meal_status WHERE log_date='2026-05-27' AND logged_meal_id=16363"
    ).fetchone()
    after_override_count = conn.execute("SELECT COUNT(*) FROM nutrition_slot_meal_overrides").fetchone()[0]
    conn.close()
    assert int(meal_row["created_from_planned_slot_id"]) == int(slot_rows[1]["id"])
    assert int(meal_row["edited_from_template"]) == 0
    assert int(status_row["slot_id"]) == int(slot_rows[1]["id"])
    assert status_row["status"] == "logged"
    assert status_row["shifted_time_text"] == "11:40"
    assert after_override_count == before_override_count
    planned_entry = next(item for item in payload["planned_meals"] if int(item["slot_id"]) == int(slot_rows[1]["id"]))
    assert planned_entry["status"] == "logged"


def test_update_logged_meal_unresolvable_planned_meal_stays_detached_without_status(client):
    import nutrition.nutrition_planning_db as nutrition_db

    _, _, food_ids = _seed_planned_logging_fixture(client, day_label="Mi")
    conn = connections.get_nutrition_db()
    conn.row_factory = sqlite3.Row
    now = "2026-05-27T12:00:00"
    conn.execute(
        """
        INSERT INTO nutrition_logged_meals
            (id, log_date, slot_id, title, meal_slot, source, created_from_planned_slot_id, created_from_template_id,
             edited_from_template, original_planned_time, logged_at, created_at, updated_at)
        VALUES (17001, '2026-05-27', NULL, 'Unklar', 'Meal 9', 'planned', NULL, 999001, 0, '23:59', '2026-05-27T23:59:00', ?, ?)
        """,
        (now, now),
    )
    conn.execute(
        """
        INSERT INTO nutrition_logged_meal_items
            (logged_meal_id, food_id, food_name, amount, unit, item_type, sort_index, created_at, updated_at)
        VALUES (17001, ?, 'Brot', 100, 'g', 'food', 0, ?, ?)
        """,
        (int(food_ids[0]), now, now),
    )
    conn.commit()
    conn.close()

    payload, err = nutrition_db.update_logged_meal(17001, {"title": "Unklar"})

    assert err is None
    conn = connections.get_nutrition_db()
    conn.row_factory = sqlite3.Row
    meal_row = conn.execute("SELECT created_from_planned_slot_id, edited_from_template FROM nutrition_logged_meals WHERE id=17001").fetchone()
    status_count = conn.execute("SELECT COUNT(*) FROM nutrition_planned_meal_status WHERE logged_meal_id=17001").fetchone()[0]
    conn.close()
    assert meal_row["created_from_planned_slot_id"] is None
    assert int(meal_row["edited_from_template"]) == 1
    assert status_count == 0
    assert any((item.get("status") or "open") == "open" for item in payload["planned_meals"])


def test_reconcile_logged_planned_meals_backfills_missing_slot_links(client):
    import nutrition.nutrition_planning_db as nutrition_db

    _, slot_rows, food_ids = _seed_planned_logging_fixture(client, day_label="Mi")
    conn = connections.get_nutrition_db()
    conn.row_factory = sqlite3.Row
    now = "2026-05-27T12:00:00"
    fixtures = [
        (16358, "Meal 1", "Frühstück", slot_rows[0]["meal_template_id"], "06:40", "2026-05-27T06:40:00", 100, "g"),
        (16364, "Meal 4", "Abends", slot_rows[1]["meal_template_id"], "09:30", "2026-05-27T21:10:00", 100, "g"),
        (16362, "Meal 3", "Nachmittags", slot_rows[2]["meal_template_id"], "16:00", "2026-05-27T16:00:00", 150, "g"),
        (16363, "Meal 2", "Schule", slot_rows[3]["meal_template_id"], "20:30", "2026-05-27T11:40:00", 250, "ml"),
    ]
    for idx, (meal_id, meal_slot, title, template_id, original_time, logged_at, amount, unit) in enumerate(fixtures):
        conn.execute(
            """
            INSERT INTO nutrition_logged_meals
                (id, log_date, slot_id, title, meal_slot, source, created_from_planned_slot_id, created_from_template_id,
                 edited_from_template, original_planned_time, logged_at, created_at, updated_at)
            VALUES (?, '2026-05-27', NULL, ?, ?, 'planned', NULL, ?, 0, ?, ?, ?, ?)
            """,
            (meal_id, title, meal_slot, int(template_id), original_time, logged_at, now, now),
        )
        conn.execute(
            """
            INSERT INTO nutrition_logged_meal_items
                (logged_meal_id, food_id, food_name, amount, unit, item_type, sort_index, created_at, updated_at)
            VALUES (?, ?, 'Brot', ?, ?, 'food', 0, ?, ?)
            """,
            (meal_id, int(food_ids[idx]), amount, unit, now, now),
        )
    before_slot_ids = [row["id"] for row in conn.execute("SELECT id FROM nutrition_week_day_slots ORDER BY id").fetchall()]
    before_override_count = conn.execute("SELECT COUNT(*) FROM nutrition_slot_meal_overrides").fetchone()[0]
    conn.commit()
    conn.close()

    result = nutrition_db.reconcile_logged_planned_meals(date_iso="2026-05-27", apply=True)

    assert result == {"scanned": 4, "resolved": 4, "unresolved": 0, "status_written": 4}
    conn = connections.get_nutrition_db()
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, created_from_planned_slot_id FROM nutrition_logged_meals WHERE id IN (16358,16364,16362,16363) ORDER BY id"
    ).fetchall()
    statuses = conn.execute(
        "SELECT slot_id, status, logged_meal_id, shifted_time_text FROM nutrition_planned_meal_status WHERE log_date='2026-05-27' ORDER BY slot_id"
    ).fetchall()
    after_slot_ids = [row["id"] for row in conn.execute("SELECT id FROM nutrition_week_day_slots ORDER BY id").fetchall()]
    after_override_count = conn.execute("SELECT COUNT(*) FROM nutrition_slot_meal_overrides").fetchone()[0]
    conn.close()
    assert {int(row["id"]): int(row["created_from_planned_slot_id"]) for row in rows} == {
        16358: int(slot_rows[0]["id"]),
        16364: int(slot_rows[1]["id"]),
        16362: int(slot_rows[2]["id"]),
        16363: int(slot_rows[3]["id"]),
    }
    assert [int(row["slot_id"]) for row in statuses] == [int(slot_rows[idx]["id"]) for idx in range(4)]
    assert [row["status"] for row in statuses] == ["logged", "logged", "logged", "logged"]
    assert [row["shifted_time_text"] for row in statuses] == [None, "21:10", None, "11:40"]
    assert before_slot_ids == after_slot_ids
    assert before_override_count == after_override_count


def test_planning_week_slot_title_helper_prefers_custom_title_over_stale_display_title():
    source = Path("/opt/liva/static/js/planning_nutrition.js").read_text(encoding="utf-8")
    assert "function getWeekSlotTitle(slot)" in source
    helper_start = source.index("function getWeekSlotTitle(slot)")
    helper_end = source.index("\n  }\n", helper_start)
    helper = source[helper_start:helper_end]
    assert "slot.display_title" in helper
    assert "slot.meal_title" in helper
    assert "slot.meal_template?.title" in helper
    assert helper.index("slot.custom_title") < helper.index("slot.display_title")
    assert helper.index("slot.display_title") < helper.index("slot.meal_template?.title")
    assert helper.index("slot.meal_title") < helper.index("slot.meal_template?.title")


def test_individual_day_title_is_canonical_across_plan_read_and_checklist(client):
    import nutrition.nutrition_planning_db as nutrition_db

    food_id = _seed_test_food("Canonical Title Food")
    plan_id = _create_nutrition_plan(
        client,
        "Canonical individual title",
        False,
        [{"day": "So", "meals": [{"title": "Alter Name", "time": "10:30", "items": [{"food_id": food_id, "amount": 100, "unit": "g"}]}]}],
    )
    conn = connections.get_nutrition_db()
    slot_id = conn.execute(
        """SELECT s.id FROM nutrition_week_day_slots s
           JOIN nutrition_week_template_days d ON d.id=s.week_template_day_id
           WHERE d.week_template_id=? AND d.weekday=6 AND s.slot_index=0""",
        (plan_id,),
    ).fetchone()[0]
    # Simulate the legacy materialized value that used to win on a fresh read.
    conn.execute(
        """UPDATE nutrition_slot_meals SET meal_title='Alter Bibliotheksname'
           WHERE week_template_day_id=(SELECT week_template_day_id FROM nutrition_week_day_slots WHERE id=?) AND slot_index=0""",
        (slot_id,),
    )
    conn.commit()
    conn.close()

    updated, error = nutrition_db.update_week_slot(int(slot_id), {"custom_title": "Neuer Sonntagstitel"})
    assert error is None
    assert updated["custom_title"] == "Neuer Sonntagstitel"

    week = nutrition_db._build_week_plan_payload_wrapper(plan_id)
    sunday_slot = week["week_template"]["days"][6]["slots"][0]
    assert sunday_slot["custom_title"] == "Neuer Sonntagstitel"
    assert sunday_slot["meal_title"] == "Neuer Sonntagstitel"
    assert sunday_slot["display_title"] == "Neuer Sonntagstitel"

    checklist = nutrition_db.build_mfp_checklist(plan_id)
    assert checklist["checklist"][6]["slots"][0]["meal_title"] == "Neuer Sonntagstitel"


def test_nutrition_create_and_patch_store_meals_chronologically(client):
    food_id = _seed_test_food("Chronology Food")
    plan_id = _create_nutrition_plan(
        client,
        "Chronological Meals",
        False,
        [{"day": "Mo", "meals": [
            {"title": "Abend", "time": "20:30", "items": [{"food_id": food_id, "amount": 100, "unit": "g"}]},
            {"title": "Früh", "time": "10:30", "items": [{"food_id": food_id, "amount": 100, "unit": "g"}]},
            {"title": "Intra", "time": "18:30", "items": [{"food_id": food_id, "amount": 100, "unit": "g"}]},
            {"title": "Mittag", "time": "15:30", "items": [{"food_id": food_id, "amount": 100, "unit": "g"}]},
        ]}],
    )
    created = client.get(f"/api/v2/actions/nutrition/plans/{plan_id}/detail", headers=auth()).get_json()
    assert [(meal["time_text"], meal["title"]) for meal in created["days"][0]["meals"]] == [
        ("10:30", "Früh"), ("15:30", "Mittag"), ("18:30", "Intra"), ("20:30", "Abend")
    ]

    patched = client.post(
        f"/api/v2/actions/nutrition/plans/{plan_id}/patch",
        json={"dry_run": False, "operations": [{"op": "update_meal_time", "match": {"day_index": 0, "meal_index": 0}, "time": "21:30"}]},
        headers=auth(),
    ).get_json()
    assert [(meal["time_text"], meal["title"]) for meal in patched["after"]["days"][0]["meals"]] == [
        ("15:30", "Mittag"), ("18:30", "Intra"), ("20:30", "Abend"), ("21:30", "Früh")
    ]
    conn = connections.get_nutrition_db()
    rows = conn.execute(
        """
        SELECT s.slot_index, s.time_text, s.custom_title, sm.meal_title
        FROM nutrition_week_day_slots s
        JOIN nutrition_week_template_days d ON d.id=s.week_template_day_id
        LEFT JOIN nutrition_slot_meals sm ON sm.week_template_day_id=d.id AND sm.slot_index=s.slot_index
        WHERE d.week_template_id=? AND d.weekday=0
        ORDER BY s.slot_index
        """,
        (plan_id,),
    ).fetchall()
    conn.close()
    assert [tuple(row) for row in rows] == [
        (0, "15:30", "Mittag", "Mittag"),
        (1, "18:30", "Intra", "Intra"),
        (2, "20:30", "Abend", "Abend"),
        (3, "21:30", "Früh", "Früh"),
    ]

    import nutrition.nutrition_planning_db as nutrition_db

    conn = connections.get_nutrition_db()
    midday_slot_id = conn.execute(
        """
        SELECT s.id FROM nutrition_week_day_slots s
        JOIN nutrition_week_template_days d ON d.id=s.week_template_day_id
        WHERE d.week_template_id=? AND d.weekday=0 AND s.custom_title='Mittag'
        """,
        (plan_id,),
    ).fetchone()[0]
    conn.close()
    updated, error = nutrition_db.update_week_slot(int(midday_slot_id), {"time_text": "22:30"})
    assert error is None and updated is not None
    directly_updated = client.get(f"/api/v2/actions/nutrition/plans/{plan_id}/detail", headers=auth()).get_json()
    assert [(meal["time_text"], meal["title"]) for meal in directly_updated["days"][0]["meals"]] == [
        ("18:30", "Intra"), ("20:30", "Abend"), ("21:30", "Früh"), ("22:30", "Mittag")
    ]


def test_schema_maintenance_does_not_refill_structured_plan_with_placeholder_slots(client):
    import nutrition.nutrition_planning_db as nutrition_db

    food_id = _seed_test_food("No Placeholder Food")
    plan_id = _create_nutrition_plan(
        client,
        "Three Meals Stay Three",
        True,
        [{"day": day, "meals": [
            {"title": "Früh", "time": "10:30", "items": [{"food_id": food_id, "amount": 100, "unit": "g"}]},
            {"title": "Mittag", "time": "15:30", "items": [{"food_id": food_id, "amount": 100, "unit": "g"}]},
            {"title": "Abend", "time": "20:30", "items": [{"food_id": food_id, "amount": 100, "unit": "g"}]},
        ]} for day in ("Mo", "Di", "Mi", "Do", "Fr", "Sa", "So")],
    )
    conn = connections.get_nutrition_db()
    conn.row_factory = sqlite3.Row
    nutrition_db._ensure_default_week_slots(conn.cursor())
    conn.commit()
    counts = conn.execute(
        """
        SELECT d.weekday, COUNT(s.id) AS slot_count
        FROM nutrition_week_template_days d
        LEFT JOIN nutrition_week_day_slots s ON s.week_template_day_id=d.id
        WHERE d.week_template_id=?
        GROUP BY d.weekday ORDER BY d.weekday
        """,
        (plan_id,),
    ).fetchall()
    conn.close()
    assert [int(row["slot_count"]) for row in counts] == [3, 3, 3, 3, 3, 3, 3]
