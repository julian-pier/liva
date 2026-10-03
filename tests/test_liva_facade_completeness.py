from __future__ import annotations

import database.connections as connections
import ai.actions_v2 as actions_v2

from tests.test_actions_v2_api import auth, client
from tests.test_actions_v2_plan_lifecycle import _create_training_plan


def test_capabilities_are_current_and_self_describing(client):
    response = client.post("/api/v2/actions/liva/read", json={"mode": "capabilities"}, headers=auth())
    body = response.get_json()
    assert response.status_code == 200 and body["ok"] is True
    contract = body["result"]
    assert contract["forward_compatible_facade"] is True
    assert {"calendar_range", "training_exercises", "nutrition_meal_templates"} <= set(contract["read_modes"])
    assert contract["read_hints"]["nutrition_data"]["payload"]["nutrition_mode"] == "meals"
    assert "rebuild_nutrition_plan_from_actuals" in contract["workflows"]
    for command in ("edit_logged_set", "edit_logged_workout_exercise", "delete_logged_workout"):
        assert command in contract["commands"]["training"]
        assert contract["commands"]["training"][command]["dry_run_supported"] is True
        assert contract["commands"]["training"][command]["confirm_required_for_live"] is True
    assert contract["commands"]["training"]["edit_logged_set"]["indexing"].startswith("exercise_index")
    exclusions = contract["commands"]["training"]["log_gym_session"]["progression_exclusions"]
    assert exclusions["reason_required"] is True
    assert "exercises[].progression_excluded" in exclusions["exercise_level"]
    assert "exercises[].sets[].progression_exclusion_reason" in exclusions["set_level"]
    note_policy = contract["commands"]["training"]["log_gym_session"]["exercise_note_policy"]
    assert note_policy["field"] == "exercises[].notes"
    assert note_policy["do_not_invent"] is True
    nutrition_patch = contract["commands"]["nutrition"]["patch_nutrition_plan"]
    edit_notes = contract["commands"]["training"]["edit_logged_workout_exercise"]
    assert ["notes"] in edit_notes["required_any"]
    assert edit_notes["readback_required"] is True
    assert contract["contract_version"] == "2026-09-10.1"
    endurance_patch = contract["commands"]["endurance"]["patch_endurance_plan"]
    assert endurance_patch["operation_contracts"]["set_fitness_anchor"]["required"] == ["op", "anchor"]
    assert endurance_patch["operation_contracts"]["set_session_target"]["example"]["target"]["pace_s_per_km"] == 260
    assert contract["commands"]["endurance"]["create_endurance_plan"]["session_type_aliases"] == {"intervals": "interval"}
    assert contract["memory"] == {
        "canonical_backend": "liva_memory_v2",
        "recall_tool": "liva_memory_recall",
        "write_flow": {"begin": "liva_memory_begin", "finish": "liva_memory_finish"},
        "import_tool": "liva_memory_import_file",
        "source_tool": "liva_memory_source",
        "legacy_memos": {"status": "read_only_archive"},
    }
    assert nutrition_patch["indexing"].startswith("day_index")
    assert {"rename_meal", "update_meal_time", "replace_meal_items", "delete_empty_meals"} <= set(nutrition_patch["operations"])
    assert nutrition_patch["operations"]["replace_meal_items"]["example"]["items"][0]["food_id"] == 27


def test_capabilities_contract_matrix_matches_runtime_guards(client):
    """The published contract is executable, not documentation only."""
    contract = client.post("/api/v2/actions/liva/read", json={"mode": "capabilities"}, headers=auth()).get_json()["result"]
    published = contract["commands"]
    assert set(published) == {"core", "training", "nutrition", "weight", "cardio", "recovery", "endurance"}
    for domain, commands in published.items():
        assert set(commands) == set(actions_v2.LIVA_ACT_COMMANDS[domain])
        for command, details in commands.items():
            key = (domain, command)
            assert details["dry_run_supported"] is (key in actions_v2.LIVA_DRY_RUN_SUPPORTED)
            assert details["confirm_required_for_live"] is actions_v2._liva_confirm_required(domain, command, dry_run=False, payload={})
            assert actions_v2._liva_confirm_required(domain, command, dry_run=True, payload={}) is False
            for field in details.get("required", []):
                payload = {other: "x" for other in details.get("required", []) if other != field}
                result = actions_v2._capability_required_error(domain, command, payload)
                assert result is not None
                assert result[0]["error"]["message"] == f"missing_required_field: {field}"
            for group_key in ("required_any", "required_any_value", "required_any_duration"):
                groups = details.get(group_key, [])
                if groups:
                    result = actions_v2._capability_required_error(domain, command, {field: "x" for field in details.get("required", [])})
                    assert result is not None
                    assert result[0]["error"]["message"].startswith("missing_required_any: ")
            for field, values in details.get("allowed", {}).items():
                assert values and len(values) == len(set(values)), f"{domain}.{command}.{field} has an invalid enum contract"


def test_training_plan_read_exposes_rolling_gym_and_blocked_rest_days(client):
    import json

    conn = connections.get_plans_db()
    plan_json = {
        "meta": {"mode": "rolling_sequence", "rolling_week_pattern": {"Mo": "train", "Di": "train", "Mi": "train", "Do": "train", "Fr": "train", "Sa": "rest", "So": "rest"}},
        "sequence": [{"id": "push-a", "kind": "gym", "title": "Push A", "items": [{"kind": "exercise", "name": "Press", "sets": 2}]}],
    }
    conn.execute("UPDATE gym_plans SET plan_json=?, is_active=1, is_archived=0 WHERE id=1", (json.dumps(plan_json),))
    conn.commit()
    conn.close()

    body = client.post("/api/v2/actions/liva/read", json={"mode": "training_plan"}, headers=auth()).get_json()
    plan = body["result"]["plan"]

    assert plan["mode"] == "rolling_sequence"
    assert plan["weekly_training_status"] == {"Mo": "gym", "Di": "gym", "Mi": "gym", "Do": "gym", "Fr": "gym", "Sa": "rest", "So": "rest"}
    assert plan["gym_days"] == ["Mo", "Di", "Mi", "Do", "Fr"]
    assert plan["blocked_rest_days"] == ["Sa", "So"]
    assert plan["scheduled_gym_days_per_week"] == 5
    assert plan["scheduled_rest_days_per_week"] == 2


def test_unified_reads_cover_calendar_library_and_plan_details(client):
    conn = connections.get_training_db()
    conn.execute("CREATE TABLE IF NOT EXISTS movement_library (id INTEGER PRIMARY KEY, movement_key TEXT, label TEXT, muscle_group TEXT, metadata_json TEXT)")
    conn.execute("INSERT INTO movement_library (id, movement_key, label, muscle_group, metadata_json) VALUES (99, 'bench_press', 'Bench Press', 'chest', '{}')")
    conn.commit()
    conn.close()
    exercise_search = client.post("/api/v2/actions/liva/read", json={"mode": "training_exercises", "payload": {"query": "Bench", "limit": 10}}, headers=auth()).get_json()
    calendar = client.post("/api/v2/actions/liva/read", json={"mode": "calendar_range", "payload": {"start": "2026-04-16", "end": "2026-04-18"}}, headers=auth()).get_json()
    meal_templates = client.post("/api/v2/actions/liva/read", json={"mode": "nutrition_meal_templates", "payload": {"query": ""}}, headers=auth()).get_json()
    nutrition_plan = client.post("/api/v2/actions/liva/read", json={"mode": "nutrition_plan", "payload": {}}, headers=auth()).get_json()
    assert exercise_search["ok"] is True and "exercises" in exercise_search["result"]
    assert calendar["ok"] is True and "days" in calendar["result"]
    assert meal_templates["ok"] is True and "meal_templates" in meal_templates["result"]
    assert "ok" in nutrition_plan


def test_training_exercise_discovery_falls_back_to_real_training_history(client):
    search = client.post(
        "/api/v2/actions/liva/read",
        json={"mode": "training_exercises", "payload": {"query": "Bench", "limit": 10}},
        headers=auth(),
    ).get_json()
    assert search["ok"] is True
    historical = next(item for item in search["result"]["exercises"] if item["name"] == "Bench")
    assert historical["source"] == "training_history"
    assert historical["exercise_id"] == 1

    detail = client.post(
        "/api/v2/actions/liva/read",
        json={"mode": "training_exercise_detail", "payload": {"exercise_id": historical["exercise_id"]}},
        headers=auth(),
    ).get_json()
    by_name = client.post(
        "/api/v2/actions/liva/read",
        json={"mode": "training_exercise_detail", "payload": {"name": "Bench"}},
        headers=auth(),
    ).get_json()
    assert detail["ok"] is True and detail["result"]["source"] == "training_history"
    assert by_name["ok"] is True and by_name["result"]["exercise_id"] == historical["exercise_id"]


def test_missing_nutrition_actuals_stay_missing_instead_of_zero(client):
    state = client.post("/api/v2/actions/liva/read", json={"mode": "nutrition_state"}, headers=auth()).get_json()["result"]["nutrition"]
    timing = client.post(
        "/api/v2/actions/liva/read",
        json={"mode": "nutrition_timing", "date": "2026-04-30", "days": 1},
        headers=auth(),
    ).get_json()["result"]
    assert state["today_actuals"] is None
    assert state["today_actuals_status"] == "missing"
    assert timing["today"]["totals"] is None
    assert timing["today"]["totals_status"] == "missing"


def test_cardio_summary_days_is_a_backward_looking_window(client):
    conn = connections.get_runs_db()
    conn.execute("INSERT INTO runs (id, date, distance, moving_time, avg_hr, pace) VALUES (99, '2026-08-24', 5870, 1272, 130, 300)")
    conn.commit()
    conn.close()
    body = client.get("/api/v2/actions/cardio/summary?date_to=2026-08-31&days=30", headers=auth()).get_json()
    assert body["range"] == {"date_from": "2026-08-02", "date_to": "2026-08-31", "days": 30}
    assert body["summary"]["total_sessions"] == 1


def test_nutrition_data_accepts_all_published_mode_aliases(client):
    for payload in ({"mode": "daily_totals"}, {"data_mode": "daily_totals"}, {"nutrition_mode": "daily_totals"}):
        body = client.post(
            "/api/v2/actions/liva/read",
            json={"mode": "nutrition_data", "payload": payload},
            headers=auth(),
        ).get_json()
        assert body["ok"] is True, body
        assert body["result"]["mode"] == "daily_totals"


def test_training_deep_dive_rejects_ignored_workout_scope_with_redirect(client):
    response = client.post(
        "/api/v2/actions/liva/read",
        json={"mode": "training_deep_dive", "workout_id": 1},
        headers=auth(),
    )
    body = response.get_json()
    assert response.status_code == 400
    assert body["error"]["code"] == "unsupported_scope"
    assert body["use_mode"] == "training_workout_coach_view"


def test_core_board_uses_canonical_rolling_session_and_rejects_recovery_card(client):
    conn = connections.get_plans_db()
    conn.execute(
        "UPDATE gym_plans SET plan_json=?, is_active=1, is_archived=0 WHERE id=1",
        (__import__("json").dumps({
            "meta": {"mode": "rolling_sequence", "rolling_week_pattern": {key: "train" for key in ("Mo", "Di", "Mi", "Do", "Fr", "Sa", "So")}},
            "sequence": [
                {"id": "push-a", "kind": "gym", "title": "Push A", "items": [{"name": "Bench", "sets": 2}]},
                {"id": "pull-b", "kind": "gym", "title": "Pull B", "items": [{"name": "Row", "sets": 2}]},
            ],
        }),),
    )
    conn.commit()
    conn.close()
    conn = connections.get_training_db()
    conn.execute("INSERT INTO workouts (id, date_iso, name) VALUES (99, '2026-04-20', 'Push A')")
    conn.commit()
    conn.close()

    body = client.post(
        "/api/v2/actions/core/board/build",
        json={
            "date": "2026-04-21",
            "human_headline": "Pull B sauber ausführen.",
            "training_card": {"type": "rest", "title": "Recovery / kein fixes Training"},
        },
        headers=auth(),
    ).get_json()
    assert body["ok"] is True, body
    card = body["board"]["training_card"]
    assert card["title"] == "Pull B"
    assert card["session_type"] == "gym"
    assert body["board"]["board_consistency"]["status"] == "ok"
    assert body["board"]["machine_briefing"]["supplied_training_card_rejected"]["reason"] == "conflicts_with_authoritative_session"


def test_rolling_plan_target_writes_accept_session_label_and_reject_ambiguous_rotation(client):
    import json

    conn = connections.get_plans_db()
    conn.execute(
        "UPDATE gym_plans SET plan_json=?, is_active=1, is_archived=0 WHERE id=1",
        (json.dumps({
            "meta": {"mode": "rolling_sequence"},
            "sequence": [
                {"id": "push-a", "kind": "gym", "title": "Push A", "items": [{"name": "Cable Crunches", "sets": 2}]},
                {"id": "pull-b", "kind": "gym", "title": "Pull B", "items": [{"name": "Cable Crunches", "sets": 3}, {"name": "HighRows", "sets": 2}]},
            ],
        }),),
    )
    conn.commit()
    conn.close()

    scoped = client.post(
        "/api/v2/actions/liva/act",
        json={"domain": "training", "command": "change_sets", "dry_run": True, "payload": {"day": "Pull B", "exercise": "HighRows", "sets": 4}},
        headers=auth(),
    ).get_json()
    ambiguous_response = client.post(
        "/api/v2/actions/liva/act",
        json={"domain": "training", "command": "change_sets", "dry_run": True, "payload": {"day": "rotation", "exercise": "Cable Crunches", "sets": 4}},
        headers=auth(),
    )
    ambiguous = ambiguous_response.get_json()
    assert scoped["ok"] is True, scoped
    assert scoped["result"]["matched_day"] == "rotation:1"
    assert scoped["result"]["matched_exercise"] == "HighRows"
    assert ambiguous_response.status_code == 400
    assert ambiguous["error"]["code"] == "plan_exercise_ambiguous_use_session_label_or_key"


def test_logged_set_edit_and_workout_delete_are_live_with_preview_and_confirmation(client):
    conn = connections.get_training_db()
    before = conn.execute("SELECT weight FROM sets WHERE workout_id=1 ORDER BY id LIMIT 1").fetchone()[0]
    conn.close()
    payload = {"workout_id": 1, "exercise_index": 0, "set_index": 0, "weight": before + 2.5}

    preview = client.post("/api/v2/actions/liva/act", json={"domain": "training", "command": "edit_logged_set", "payload": payload, "dry_run": True}, headers=auth()).get_json()
    conn = connections.get_training_db()
    assert conn.execute("SELECT weight FROM sets WHERE workout_id=1 ORDER BY id LIMIT 1").fetchone()[0] == before
    conn.close()
    live = client.post("/api/v2/actions/liva/act", json={"domain": "training", "command": "edit_logged_set", "payload": payload, "confirm": True}, headers=auth()).get_json()
    conn = connections.get_training_db()
    assert conn.execute("SELECT weight FROM sets WHERE workout_id=1 ORDER BY id LIMIT 1").fetchone()[0] == before + 2.5
    conn.close()
    assert preview["execution"]["mode"] == "dry_run" and preview["execution"]["live_state_changed"] is False
    assert live["execution"]["live_state_changed"] is True

    delete_preview = client.post("/api/v2/actions/liva/act", json={"domain": "training", "command": "delete_logged_workout", "payload": {"workout_id": 1}, "dry_run": True}, headers=auth()).get_json()
    blocked = client.post("/api/v2/actions/liva/act", json={"domain": "training", "command": "delete_logged_workout", "payload": {"workout_id": 1}}, headers=auth())
    deleted = client.post("/api/v2/actions/liva/act", json={"domain": "training", "command": "delete_logged_workout", "payload": {"workout_id": 1}, "confirm": True}, headers=auth()).get_json()
    conn = connections.get_training_db()
    assert conn.execute("SELECT COUNT(*) FROM workouts WHERE id=1").fetchone()[0] == 0
    conn.close()
    assert delete_preview["execution"]["mode"] == "dry_run"
    assert blocked.status_code == 400 and blocked.get_json()["error"]["code"] == "confirmation_required"
    assert deleted["execution"]["live_state_changed"] is True


def test_advertised_add_remove_and_reorder_exercise_commands_mutate_active_plan(client):
    plan_id = _create_training_plan(client, "Facade structural", True, [{"day": "Mo", "events": [{"kind": "gym", "title": "Push", "items": [
        {"kind": "exercise", "name": "Bench", "sets": 2, "reps": {"min": 6, "max": 8}},
        {"kind": "exercise", "name": "Flys", "sets": 2, "reps": {"min": 10, "max": 12}},
    ]}]}])

    add = client.post("/api/v2/actions/liva/act", json={"domain": "training", "command": "add_exercise", "payload": {"day": "Mo", "new_exercise": {"name": "Trizeps", "canonical_id": "triceps", "sets": [{"target_rpe": 8}]}, "position": 1}, "confirm": True}, headers=auth()).get_json()
    reorder = client.post("/api/v2/actions/liva/act", json={"domain": "training", "command": "reorder_exercise", "payload": {"day": "Mo", "exercise": "Trizeps", "position": 0}, "confirm": True}, headers=auth()).get_json()
    remove = client.post("/api/v2/actions/liva/act", json={"domain": "training", "command": "remove_exercise", "payload": {"day": "Mo", "exercise": "Flys"}, "confirm": True}, headers=auth()).get_json()
    detail = client.get(f"/api/v2/actions/training/plans/{plan_id}/detail", headers=auth()).get_json()
    names = [item["name"] for item in detail["days"][0]["workouts"][0]["exercises"]]
    assert add["execution"]["live_state_changed"] is True
    assert reorder["execution"]["live_state_changed"] is True
    assert remove["execution"]["live_state_changed"] is True
    assert names == ["Trizeps", "Bench"]


def test_update_exercise_target_preserves_every_advertised_target_field_in_dry_run(client):
    _create_training_plan(client, "Target contract", True, [{"day": "Mo", "events": [{"kind": "gym", "title": "Push", "items": [{"kind": "exercise", "name": "OHP (eGym)", "sets": 1, "reps": {"min": 5, "max": 8}, "rpe_list": [7]}]}]}])
    payload = {"day": "Mo", "exercise": "OHP (eGym)", "sets": 2, "rep_range": "6-10", "rpe": 8.5, "note": "kontrolliert"}
    response = client.post("/api/v2/actions/liva/act", json={"domain": "training", "command": "update_exercise_target", "payload": payload, "dry_run": True}, headers=auth()).get_json()
    target = response["result"]["new_target"]
    assert response["ok"] is True and target["sets"] == 2
    assert target["rep_range"] == {"min": 6, "max": 10}
    assert target["rpe_list"] == [8.5, 8.5]
    assert target["note"] == "kontrolliert"


def test_cardio_edit_commands_preview_then_update_canonical_run(client):
    payload = {"run_id": 1, "distance_km": 5.25, "duration_min": 26, "avg_hr": 147}
    preview = client.post(
        "/api/v2/actions/liva/act",
        json={"domain": "cardio", "command": "edit_run", "payload": payload, "dry_run": True},
        headers=auth(),
    ).get_json()
    conn = connections.get_runs_db()
    unchanged = conn.execute("SELECT distance, moving_time, avg_hr FROM runs WHERE id=1").fetchone()
    conn.close()
    assert tuple(unchanged) == (5000.0, 1500, 145.0)
    assert preview["execution"]["mode"] == "dry_run"
    assert preview["result"]["after"]["distance"] == 5250.0

    blocked = client.post(
        "/api/v2/actions/liva/act",
        json={"domain": "cardio", "command": "edit_run", "payload": payload},
        headers=auth(),
    )
    live = client.post(
        "/api/v2/actions/liva/act",
        json={"domain": "cardio", "command": "edit_cardio_session", "payload": payload, "confirm": True},
        headers=auth(),
    ).get_json()
    conn = connections.get_runs_db()
    updated = conn.execute("SELECT distance, moving_time, avg_hr FROM runs WHERE id=1").fetchone()
    conn.close()
    assert blocked.status_code == 400 and blocked.get_json()["error"]["code"] == "confirmation_required"
    assert tuple(updated) == (5250.0, 1560, 147.0)
    assert live["execution"]["live_state_changed"] is True
    assert live["result"]["changed_fields"] == ["avg_hr", "distance", "moving_time"]


def test_cardio_edit_semantically_identical_values_are_a_no_op(client):
    response = client.post(
        "/api/v2/actions/liva/act",
        json={
            "domain": "cardio",
            "command": "edit_cardio_session",
            "payload": {
                "run_id": 1,
                "date": "2026-04-13T12:00:00+02:00",
                "distance_m": 5000,
                "duration_s": 1500,
                "avg_hr": 145.0,
                "sport_type": "RUN",
            },
            "dry_run": True,
        },
        headers=auth(),
    ).get_json()
    assert response["ok"] is True
    assert response["result"]["changed"] is False
    assert response["result"]["changed_fields"] == []
