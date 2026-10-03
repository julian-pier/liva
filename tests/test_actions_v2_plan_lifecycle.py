import json
import sqlite3

import database.connections as connections
import nutrition.nutrition_planning_db as nutrition_db
from tests.test_actions_v2_api import auth, client
from tests.test_actions_v2_plan_create import _ensure_test_planning_schema, _seed_test_food


def _create_training_plan(test_client, name: str, set_active: bool, days):
    resp = test_client.post(
        "/api/v2/actions/liva/act",
        json={"domain": "training", "command": "create_training_plan", "confirm": True, "payload": {"name": name, "set_active": set_active, "days": days}},
        headers=auth(),
    )
    assert resp.status_code == 200
    return resp.get_json()["result"]["plan_id"]


def _create_nutrition_plan(test_client, name: str, set_active: bool, days):
    resp = test_client.post(
        "/api/v2/actions/liva/act",
        json={"domain": "nutrition", "command": "create_nutrition_plan", "confirm": True, "payload": {"name": name, "set_active": set_active, "days": days}},
        headers=auth(),
    )
    assert resp.status_code == 200
    return resp.get_json()["result"]["template_id"]


def _seed_rotation_mirror_plan(conn, plan_id: int) -> dict:
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT plan_json FROM gym_plans WHERE id=?", (plan_id,)).fetchone()
    plan_json = json.loads(row["plan_json"])
    plan_json["meta"] = {"mode": "rolling_sequence"}
    ordered_days = [day for day in ("Mo", "Di", "Mi", "Do", "Fr", "Sa", "So") if plan_json["base_week"].get(day)]
    plan_json["days"] = [
        {"day": day, "events": plan_json["base_week"][day]}
        for day in ordered_days
    ]
    plan_json["sequence"] = [
        {**dict(event), "items": [dict(item) for item in event.get("items") or []]}
        for day in ordered_days
        for event in plan_json["base_week"][day]
    ]
    conn.execute("UPDATE gym_plans SET plan_json=? WHERE id=?", (json.dumps(plan_json, ensure_ascii=False), plan_id))
    conn.commit()
    return plan_json


def test_activate_training_plan_sets_exact_plan_active(client):
    other_id = _create_training_plan(client, "Alt Plan", False, [{"day": "Di", "events": [{"kind": "gym", "title": "Pull", "items": [{"kind": "exercise", "name": "Row", "sets": 3, "reps": {"min": 8, "max": 10}, "rpe_list": [8, 8, 8]}]}]}])
    resp = client.post("/api/v2/actions/liva/act", json={"domain": "training", "command": "activate_training_plan", "confirm": True, "payload": {"plan_id": other_id}}, headers=auth())
    body = resp.get_json()
    plans = client.post("/api/v2/actions/liva/read", json={"mode": "training_plans"}, headers=auth()).get_json()["result"]["plans"]
    assert resp.status_code == 200
    assert body["result"]["plan_id"] == other_id
    assert body["result"]["activated"] is True
    assert any(plan["id"] == other_id and plan["is_active"] for plan in plans)
    assert sum(1 for plan in plans if plan["is_active"]) == 1


def test_readliva_training_plans_include_archived_returns_active_and_inactive_plans(client):
    archived_id = _create_training_plan(client, "Archived Readback", False, [{"day": "Fr", "events": [{"kind": "gym", "title": "Upper", "items": [{"kind": "exercise", "name": "Press", "sets": 3, "reps": {"min": 8, "max": 10}, "rpe_list": [8, 8, 8]}]}]}])
    active_id = _create_training_plan(client, "Fresh Active", True, [{"day": "Mo", "events": [{"kind": "gym", "title": "Push", "items": [{"kind": "exercise", "name": "Bench", "sets": 3, "reps": {"min": 6, "max": 8}, "rpe_list": [8, 8, 9]}]}]}])
    archive_resp = client.post("/api/v2/actions/liva/act", json={"domain": "training", "command": "archive_training_plan", "confirm": True, "payload": {"plan_id": archived_id, "archive_reason": "schema read test"}}, headers=auth())
    assert archive_resp.status_code == 200

    body = client.post("/api/v2/actions/liva/read", json={"mode": "training_plans", "include_archived": True}, headers=auth()).get_json()
    plans = body["result"]["plans"]
    assert body["ok"] is True
    assert any(plan["id"] == active_id and plan["is_active"] for plan in plans)
    assert any(plan["id"] == archived_id and plan["is_archived"] for plan in plans)


def test_readliva_training_plan_honors_plan_id_for_compact_and_full(client):
    inactive_id = _create_training_plan(client, "Inactive Detail Plan", False, [{"day": "Di", "events": [{"kind": "gym", "title": "Chest", "items": [{"kind": "exercise", "name": "Bench", "sets": 2, "reps": {"min": 6, "max": 8}, "rpe_list": [8, 9]}]}]}])
    _create_training_plan(client, "Current Active Plan", True, [{"day": "Mo", "events": [{"kind": "gym", "title": "Active", "items": [{"kind": "exercise", "name": "Row", "sets": 3, "reps": {"min": 8, "max": 10}, "rpe_list": [8, 8, 8]}]}]}])

    compact = client.post("/api/v2/actions/liva/read", json={"mode": "training_plan", "plan_id": inactive_id, "detail": "compact"}, headers=auth()).get_json()
    full = client.post("/api/v2/actions/liva/read", json={"mode": "training_plan", "plan_id": inactive_id, "detail": "full"}, headers=auth()).get_json()

    assert compact["ok"] is True
    assert compact["result"]["plan"]["id"] == inactive_id
    assert compact["result"]["plan"]["plan_id"] == inactive_id
    assert compact["result"]["plan"]["title"] == "Inactive Detail Plan"
    assert compact["result"]["plan"]["is_active"] is False

    assert full["ok"] is True
    assert full["result"]["plan"]["id"] == inactive_id
    assert full["result"]["plan"]["title"] == "Inactive Detail Plan"
    assert "Di" in full["result"]["plan"]["days"]


def test_readliva_training_plan_honors_id_alias_and_missing_id_errors(client):
    inactive_id = _create_training_plan(client, "Alias Plan", False, [{"day": "Fr", "events": [{"kind": "gym", "title": "Upper", "items": [{"kind": "exercise", "name": "Press", "sets": 3, "reps": {"min": 8, "max": 10}, "rpe_list": [8, 8, 8]}]}]}])
    alias_body = client.post("/api/v2/actions/liva/read", json={"mode": "training_plan", "id": inactive_id, "detail": "compact"}, headers=auth()).get_json()
    missing = client.post("/api/v2/actions/liva/read", json={"mode": "training_plan", "plan_id": 999999, "detail": "compact"}, headers=auth())

    assert alias_body["ok"] is True
    assert alias_body["result"]["plan"]["id"] == inactive_id
    assert missing.status_code == 404
    assert missing.get_json()["error"]["code"] == "plan_not_found"


def test_readliva_training_plan_day_and_exercise_apply_to_requested_inactive_plan(client):
    inactive_id = _create_training_plan(client, "Inactive Filter Plan", False, [{"day": "Sa", "events": [{"kind": "gym", "title": "Special Day", "items": [{"kind": "exercise", "name": "Pulldown", "sets": 3, "reps": {"min": 10, "max": 12}, "rpe_list": [8, 8, 9]}]}]}])
    _create_training_plan(client, "Other Active Plan", True, [{"day": "Sa", "events": [{"kind": "gym", "title": "Different", "items": [{"kind": "exercise", "name": "Squat", "sets": 3, "reps": {"min": 5, "max": 8}, "rpe_list": [8, 8, 9]}]}]}])

    day_body = client.post("/api/v2/actions/liva/read", json={"mode": "training_plan", "plan_id": inactive_id, "detail": "day", "day": "Sa"}, headers=auth()).get_json()
    ex_body = client.post("/api/v2/actions/liva/read", json={"mode": "training_plan", "plan_id": inactive_id, "detail": "exercise", "day": "Sa", "exercise": "Pulldown"}, headers=auth()).get_json()

    assert day_body["ok"] is True
    assert day_body["result"]["plan"]["id"] == inactive_id
    assert day_body["result"]["plan"]["matched_day"] == "Sa"
    assert any(ex["name"] == "Pulldown" for ex in day_body["result"]["plan"]["days"]["Sa"]["exercises"])
    assert ex_body["ok"] is True
    assert ex_body["result"]["plan"]["id"] == inactive_id
    assert ex_body["result"]["plan"]["target"]["name"] == "Pulldown"


def test_readliva_training_plan_active_mode_remains_backward_compatible(client):
    active_id = _create_training_plan(client, "Compatibility Active", True, [{"day": "Mi", "events": [{"kind": "gym", "title": "Pull", "items": [{"kind": "exercise", "name": "Row", "sets": 3, "reps": {"min": 8, "max": 10}, "rpe_list": [8, 8, 8]}]}]}])
    body = client.post("/api/v2/actions/liva/read", json={"mode": "training_plan", "detail": "compact"}, headers=auth()).get_json()
    plan = body["result"]["plan"]
    plans = client.post("/api/v2/actions/liva/read", json={"mode": "training_plans", "include_archived": True}, headers=auth()).get_json()["result"]["plans"]
    assert body["ok"] is True
    assert isinstance(plan, dict)
    assert plan
    assert any(item["id"] == active_id and item["is_active"] for item in plans)


def test_archive_training_plan_archives_non_active_plan(client):
    plan_id = _create_training_plan(client, "Archive Me", False, [{"day": "Fr", "events": [{"kind": "gym", "title": "Upper", "items": [{"kind": "exercise", "name": "Press", "sets": 3, "reps": {"min": 8, "max": 10}, "rpe_list": [8, 8, 8]}]}]}])
    resp = client.post("/api/v2/actions/liva/act", json={"domain": "training", "command": "archive_training_plan", "confirm": True, "payload": {"plan_id": plan_id, "archive_reason": "test"}}, headers=auth())
    assert resp.status_code == 200
    plans = client.post("/api/v2/actions/liva/read", json={"mode": "training_plans"}, headers=auth()).get_json()["result"]["plans"]
    assert any(plan["id"] == plan_id and plan["is_archived"] for plan in plans)


def test_delete_training_plan_requires_confirm_and_hard_delete(client):
    plan_id = _create_training_plan(client, "Delete Me", False, [{"day": "Sa", "events": [{"kind": "gym", "title": "Legs", "items": [{"kind": "exercise", "name": "Squat", "sets": 3, "reps": {"min": 5, "max": 8}, "rpe_list": [8, 8, 9]}]}]}])
    missing_confirm = client.post("/api/v2/actions/liva/act", json={"domain": "training", "command": "delete_training_plan", "payload": {"plan_id": plan_id, "hard_delete": True}}, headers=auth())
    assert missing_confirm.status_code == 400
    resp = client.post("/api/v2/actions/liva/act", json={"domain": "training", "command": "delete_training_plan", "confirm": True, "payload": {"plan_id": plan_id, "hard_delete": True}}, headers=auth())
    assert resp.status_code == 200
    plans = client.post("/api/v2/actions/liva/read", json={"mode": "training_plans"}, headers=auth()).get_json()["result"]["plans"]
    assert all(plan["id"] != plan_id for plan in plans)


def test_replace_training_plan_keeps_plan_id(client):
    plan_id = _create_training_plan(client, "Replace Me", False, [{"day": "Mo", "events": [{"kind": "gym", "title": "Push", "items": [{"kind": "exercise", "name": "Bench", "sets": 2, "reps": {"min": 6, "max": 8}, "rpe_list": [8, 9]}]}]}])
    resp = client.post(
        "/api/v2/actions/liva/act",
        json={"domain": "training", "command": "replace_training_plan", "confirm": True, "payload": {"plan_id": plan_id, "replace_with": {"name": "Replaced", "days": [{"day": "Di", "events": [{"kind": "ergo", "title": "Ergo – Z2", "items": [{"kind": "cardio", "duration_min": 30, "intensity": "locker", "note": "nasal"}]}]}]}}},
        headers=auth(),
    )
    body = resp.get_json()
    assert resp.status_code == 200
    assert body["result"]["plan_id"] == plan_id
    assert body["result"]["updated_plan_after"]["title"] == "Replaced"


def test_patch_training_plan_updates_cardio_duration_and_display(client):
    plan_id = _create_training_plan(client, "Patch Cardio", False, [{"day": "Di", "events": [{"kind": "ergo", "title": "Ergo – Z2", "items": [{"kind": "cardio", "duration_min": 30, "intensity": "locker", "note": "nasal"}]}]}])
    resp = client.post(
        "/api/v2/actions/liva/act",
        json={"domain": "training", "command": "patch_training_plan", "confirm": True, "payload": {"plan_id": plan_id, "operations": [{"op": "replace_cardio", "day": "Di", "title": "Ergo – Z2", "patch": {"duration_min": 40, "intensity": "locker", "notes": "nasal"}}]}},
        headers=auth(),
    )
    assert resp.status_code == 200
    conn = connections.get_plans_db()
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT plan_json FROM gym_plans WHERE id=?", (plan_id,)).fetchone()
    conn.close()
    item = __import__("json").loads(row["plan_json"])["base_week"]["Di"][0]["items"][0]
    assert item["duration_min"] == 40
    assert item["display"] == "40 min locker @ nasal"


def test_patch_training_plan_replace_exercise_target_accepts_rep_range_alias(client):
    plan_id = _create_training_plan(
        client,
        "Patch Rep Range Alias",
        False,
        [
            {"day": "Mo", "events": [{"kind": "gym", "title": "Push A", "items": [{"kind": "exercise", "name": "Bench", "sets": 2, "reps": {"min": 6, "max": 8}, "rpe_list": [8, 9]}]}]},
            {"day": "Di", "events": [{"kind": "gym", "title": "Pull A", "items": [{"kind": "exercise", "name": "Rows", "sets": 2, "reps": {"min": 8, "max": 10}, "rpe_list": [8, 9]}]}]},
            {"day": "Mi", "events": [{"kind": "gym", "title": "Push B", "items": [{"kind": "exercise", "name": "OHP", "sets": 2, "reps": {"min": 6, "max": 8}, "rpe_list": [8, 9]}]}]},
            {
                "day": "Do",
                "events": [
                    {
                        "kind": "gym",
                        "title": "Pull B",
                        "items": [
                            {
                                "kind": "exercise",
                                "name": "PullUps",
                                "sets": 3,
                                "reps": {"min": 4, "max": 6},
                                "rpe_list": [7, 8, 8],
                            }
                        ],
                    }
                ],
            }
        ],
    )
    resp = client.post(
        "/api/v2/actions/liva/act",
        json={
            "domain": "training",
            "command": "patch_training_plan",
            "confirm": True,
            "payload": {
                "plan_id": plan_id,
                "operations": [
                    {
                        "op": "replace_exercise_target",
                        "day": "Do",
                        "exercise": "pullups",
                        "patch": {"rep_range": {"min": 5, "max": 8}},
                    }
                ],
            },
        },
        headers=auth(),
    )
    body = resp.get_json()
    assert resp.status_code == 200
    assert body["ok"] is True
    assert body["execution"]["live_state_changed"] is True

    conn = connections.get_plans_db()
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT plan_json FROM gym_plans WHERE id=?", (plan_id,)).fetchone()
    conn.close()
    item = json.loads(row["plan_json"])["base_week"]["Do"][0]["items"][0]
    assert item["reps"] == {"min": 5, "max": 8}
    assert item["rpe_list"] == [7, 8, 8]


def test_patch_training_plan_replace_exercise_target_updates_canonical_readback_and_rotation_mirror(client):
    target_id = _create_training_plan(
        client,
        "Patch Target Mirror",
        False,
        [
            {"day": "Mo", "events": [{"kind": "gym", "title": "Push A", "items": [{"kind": "exercise", "name": "Bench", "sets": 2, "reps": {"min": 6, "max": 8}, "rpe_list": [8, 9]}]}]},
            {"day": "Di", "events": [{"kind": "gym", "title": "Pull A", "items": [{"kind": "exercise", "name": "Rows", "sets": 2, "reps": {"min": 8, "max": 10}, "rpe_list": [8, 9]}]}]},
            {"day": "Mi", "events": [{"kind": "gym", "title": "Push B", "items": [{"kind": "exercise", "name": "OHP", "sets": 2, "reps": {"min": 6, "max": 8}, "rpe_list": [8, 9]}]}]},
            {
                "day": "Do",
                "events": [
                    {
                        "kind": "gym",
                        "title": "Pull B",
                        "items": [
                            {
                                "kind": "exercise",
                                "name": "PullUps",
                                "variation": "weighted",
                                "note": "Alt",
                                "sets": 2,
                                "reps": {"min": 4, "max": 6},
                                "rpe_list": [7, 8],
                            }
                        ],
                    }
                ],
            }
        ],
    )
    conn = connections.get_plans_db()
    _seed_rotation_mirror_plan(conn, target_id)
    conn.close()

    resp = client.post(
        "/api/v2/actions/liva/act",
        json={
            "domain": "training",
            "command": "patch_training_plan",
            "confirm": True,
            "payload": {
                "plan_id": target_id,
                "operations": [
                    {
                        "op": "replace_exercise_target",
                        "day": "Do",
                        "exercise": "PullUps (weighted)",
                        "variation": "weighted",
                        "patch": {"rep_range": {"min": 5, "max": 8}, "rpe_list": [8, 9], "note": "Neu"},
                    }
                ],
            },
        },
        headers=auth(),
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True

    detail = client.post("/api/v2/actions/liva/read", json={"mode": "training_plan", "plan_id": target_id, "detail": "exercise", "day": "Do", "exercise": "PullUps (weighted)"}, headers=auth()).get_json()
    target = detail["result"]["plan"]["target"]
    assert target["rep_range"] == {"min": 5, "max": 8}
    assert target["rpe_list"] == [8, 9]
    assert target["note"] == "Neu"

    conn = connections.get_plans_db()
    conn.row_factory = sqlite3.Row
    raw = json.loads(conn.execute("SELECT plan_json FROM gym_plans WHERE id=?", (target_id,)).fetchone()["plan_json"])
    conn.close()
    assert raw["sequence"][-1]["items"][0]["reps"] == {"min": 5, "max": 8}
    assert raw["sequence"][-1]["items"][0]["rpe_list"] == [8, 9]
    assert raw["sequence"][-1]["items"][0]["note"] == "Neu"


def test_actliva_patch_training_plan_top_level_operations_updates_only_target_plan(client):
    active_id = _create_training_plan(client, "Active Guard Plan", True, [{"day": "Mo", "events": [{"kind": "gym", "title": "Push", "items": [{"kind": "exercise", "name": "Bench", "sets": 3, "reps": {"min": 6, "max": 8}, "rpe_list": [8, 8, 9]}]}]}])
    target_id = _create_training_plan(client, "Patch Target Plan", False, [{"day": "Di", "events": [{"kind": "ergo", "title": "Ergo – Z2", "items": [{"kind": "cardio", "duration_min": 30, "intensity": "locker", "note": "nasal"}]}]}])
    conn = connections.get_plans_db()
    before_count = conn.execute("SELECT COUNT(*) FROM gym_plans").fetchone()[0]
    active_before = conn.execute("SELECT is_active FROM gym_plans WHERE id=?", (active_id,)).fetchone()[0]
    conn.close()

    resp = client.post(
        "/api/v2/actions/liva/act",
        json={
            "domain": "training",
            "command": "patch_training_plan",
            "plan_id": target_id,
            "confirm": True,
            "dry_run": False,
            "operations": [{"op": "replace_cardio", "day": "Di", "title": "Ergo – Z2", "patch": {"duration_min": 42, "intensity": "locker", "notes": "contract"}}],
        },
        headers=auth(),
    )
    body = resp.get_json()
    assert resp.status_code == 200
    assert body["ok"] is True
    assert body["execution"]["live_state_changed"] is True
    assert body["result"]["plan_id"] == target_id
    conn = connections.get_plans_db()
    after_count = conn.execute("SELECT COUNT(*) FROM gym_plans").fetchone()[0]
    active_after = conn.execute("SELECT is_active FROM gym_plans WHERE id=?", (active_id,)).fetchone()[0]
    row = conn.execute("SELECT plan_json FROM gym_plans WHERE id=?", (target_id,)).fetchone()
    conn.close()
    item = __import__("json").loads(row["plan_json"])["base_week"]["Di"][0]["items"][0]
    assert item["duration_min"] == 42
    assert before_count == after_count
    assert active_before == active_after == 1


def test_actliva_patch_training_plan_nested_payload_operations_works_without_copy_fallback(client):
    active_id = _create_training_plan(client, "Active Guard Plan 2", True, [{"day": "Mo", "events": [{"kind": "gym", "title": "Push", "items": [{"kind": "exercise", "name": "Bench", "sets": 3, "reps": {"min": 6, "max": 8}, "rpe_list": [8, 8, 9]}]}]}])
    target_id = _create_training_plan(client, "Nested Patch Target", False, [{"day": "Di", "events": [{"kind": "ergo", "title": "Ergo – Z2", "items": [{"kind": "cardio", "duration_min": 30, "intensity": "locker", "note": "nasal"}]}]}])
    conn = connections.get_plans_db()
    before_count = conn.execute("SELECT COUNT(*) FROM gym_plans").fetchone()[0]
    conn.close()

    resp = client.post(
        "/api/v2/actions/liva/act",
        json={
            "domain": "training",
            "command": "patch_training_plan",
            "confirm": True,
            "dry_run": False,
            "payload": {
                "plan_id": target_id,
                "operations": [{"op": "replace_cardio", "day": "Di", "title": "Ergo – Z2", "patch": {"duration_min": 44, "intensity": "locker", "notes": "nested"}}],
            },
        },
        headers=auth(),
    )
    body = resp.get_json()
    assert resp.status_code == 200
    assert body["ok"] is True
    assert body["execution"]["live_state_changed"] is True
    conn = connections.get_plans_db()
    after_count = conn.execute("SELECT COUNT(*) FROM gym_plans").fetchone()[0]
    active_after = conn.execute("SELECT is_active FROM gym_plans WHERE id=?", (active_id,)).fetchone()[0]
    row = conn.execute("SELECT plan_json FROM gym_plans WHERE id=?", (target_id,)).fetchone()
    conn.close()
    item = __import__("json").loads(row["plan_json"])["base_week"]["Di"][0]["items"][0]
    assert item["duration_min"] == 44
    assert before_count == after_count
    assert active_after == 1


def test_actliva_patch_training_plan_value_operations_dry_run_works(client):
    target_id = _create_training_plan(client, "Value Patch Target", False, [{"day": "Di", "events": [{"kind": "ergo", "title": "Ergo – Z2", "items": [{"kind": "cardio", "duration_min": 30, "intensity": "locker", "note": "nasal"}]}]}])
    resp = client.post(
        "/api/v2/actions/liva/act",
        json={
            "domain": "training",
            "command": "patch_training_plan",
            "plan_id": target_id,
            "dry_run": True,
            "value": {
                "operations": [{"op": "replace_cardio", "day": "Di", "title": "Ergo – Z2", "patch": {"duration_min": 46, "intensity": "locker", "notes": "value"}}],
            },
        },
        headers=auth(),
    )
    body = resp.get_json()
    assert resp.status_code == 200
    assert body["ok"] is True
    assert body["execution"]["mode"] == "dry_run"
    assert body["execution"]["live_state_changed"] is False
    assert body["will_change"] == ["training_plan"]
    assert body["result"]["plan_id"] == target_id
    assert body["result"]["operations_count"] == 1
    assert body["result"]["matched_count"] == 1
    assert body["result"]["unmatched_count"] == 0


def test_actliva_patch_training_plan_missing_target_or_operations_stops_without_new_plan(client):
    target_id = _create_training_plan(client, "Invalid Patch Guard", False, [{"day": "Di", "events": [{"kind": "ergo", "title": "Ergo – Z2", "items": [{"kind": "cardio", "duration_min": 30, "intensity": "locker", "note": "nasal"}]}]}])
    conn = connections.get_plans_db()
    before_count = conn.execute("SELECT COUNT(*) FROM gym_plans").fetchone()[0]
    conn.close()

    missing_target = client.post(
        "/api/v2/actions/liva/act",
        json={"domain": "training", "command": "patch_training_plan", "confirm": True, "dry_run": False, "operations": [{"op": "rename_plan", "name": "Nope"}]},
        headers=auth(),
    )
    missing_ops = client.post(
        "/api/v2/actions/liva/act",
        json={"domain": "training", "command": "patch_training_plan", "confirm": True, "dry_run": False, "plan_id": target_id},
        headers=auth(),
    )
    for resp, code in ((missing_target, "target_plan_required"), (missing_ops, "operations_required")):
        body = resp.get_json()
        assert resp.status_code == 400
        assert body["ok"] is False
        assert body["error"]["code"] == code
    body = missing_ops.get_json()
    assert body["error"]["accepted_shapes"] == ["operations", "value.operations", "payload.operations"]
    assert "plan_id" in body["received_keys"]
    conn = connections.get_plans_db()
    after_count = conn.execute("SELECT COUNT(*) FROM gym_plans").fetchone()[0]
    conn.close()
    assert before_count == after_count


def test_actliva_patch_training_plan_update_exercise_note_dry_run_updates_preview_only(client):
    target_id = _create_training_plan(client, "Note Patch Dry", False, [{"day": "Mo", "events": [{"kind": "gym", "title": "Push", "items": [{"kind": "exercise", "name": "Schrägbankdrücken", "variation": "KH", "note": "CORE Hauptmission Road to 40s", "sets": 3, "reps": {"min": 6, "max": 8}, "rpe_list": [8, 8, 9]}]}]}])
    before = client.post("/api/v2/actions/liva/read", json={"mode": "training_plan", "plan_id": target_id, "detail": "full"}, headers=auth()).get_json()
    resp = client.post(
        "/api/v2/actions/liva/act",
        json={
            "domain": "training",
            "command": "patch_training_plan",
            "plan_id": target_id,
            "dry_run": True,
            "operations": [{"op": "update_exercise_note", "match": {"day": "Mo", "exercise": "Schrägbankdrücken", "variation": "KH"}, "note": "Hauptmission Road to 40s"}],
        },
        headers=auth(),
    )
    after = client.post("/api/v2/actions/liva/read", json={"mode": "training_plan", "plan_id": target_id, "detail": "full"}, headers=auth()).get_json()
    body = resp.get_json()
    assert resp.status_code == 200
    assert body["ok"] is True
    assert body["execution"]["mode"] == "dry_run"
    assert body["execution"]["live_state_changed"] is False
    assert body["will_change"] == ["training_plan"]
    assert body["result"]["changed_notes_count"] == 1
    assert before["result"]["plan"]["days"]["Mo"]["events"][0]["items"][0]["note"] == "CORE Hauptmission Road to 40s"
    assert after["result"]["plan"]["days"]["Mo"]["events"][0]["items"][0]["note"] == "CORE Hauptmission Road to 40s"


def test_actliva_patch_training_plan_write_updates_only_note_and_keeps_inactive_state(client):
    target_id = _create_training_plan(client, "Inactive Note Patch", False, [{"day": "Mo", "events": [{"kind": "gym", "title": "Push", "items": [{"kind": "exercise", "name": "Schrägbankdrücken", "variation": "KH", "note": "CORE Hauptmission Road to 40s", "sets": 3, "reps": {"min": 6, "max": 8}, "rpe_list": [8, 8, 9]}]}]}])
    conn = connections.get_plans_db()
    before_active = conn.execute("SELECT is_active FROM gym_plans WHERE id=?", (target_id,)).fetchone()[0]
    conn.close()
    resp = client.post(
        "/api/v2/actions/liva/act",
        json={
            "domain": "training",
            "command": "patch_training_plan",
            "plan_id": target_id,
            "confirm": True,
            "dry_run": False,
            "operations": [{"op": "update_exercise_note", "match": {"day": "Mo", "exercise": "Schrägbankdrücken", "variation": "KH"}, "note": "Hauptmission Road to 40s"}],
        },
        headers=auth(),
    )
    body = resp.get_json()
    detail = client.post("/api/v2/actions/liva/read", json={"mode": "training_plan", "plan_id": target_id, "detail": "full"}, headers=auth()).get_json()
    conn = connections.get_plans_db()
    after_active = conn.execute("SELECT is_active FROM gym_plans WHERE id=?", (target_id,)).fetchone()[0]
    conn.close()
    assert resp.status_code == 200
    assert body["ok"] is True
    assert body["execution"]["mode"] == "applied_live"
    assert body["execution"]["live_state_changed"] is True
    assert "training_plan" in body["execution"]["affected_resources"]
    assert body["result"]["plan_id"] == target_id
    assert before_active == after_active == 0
    assert detail["result"]["plan"]["days"]["Mo"]["events"][0]["items"][0]["note"] == "Hauptmission Road to 40s"


def test_actliva_patch_training_plan_day_read_sees_updated_note(client):
    target_id = _create_training_plan(client, "Day Readback Patch", False, [{"day": "Mo", "events": [{"kind": "gym", "title": "Push", "items": [{"kind": "exercise", "name": "Squats", "note": "CORE Nebenmission Road to 100x10", "sets": 3, "reps": {"min": 6, "max": 8}, "rpe_list": [8, 8, 9]}]}]}])
    resp = client.post(
        "/api/v2/actions/liva/act",
        json={
            "domain": "training",
            "command": "patch_training_plan",
            "plan_id": target_id,
            "confirm": True,
            "dry_run": False,
            "operations": [{"op": "update_exercise_note", "match": {"day": "Mo", "exercise": "Squats"}, "note": "Nebenmission Road to 100x10"}],
        },
        headers=auth(),
    )
    day_detail = client.post("/api/v2/actions/liva/read", json={"mode": "training_plan", "plan_id": target_id, "detail": "day", "day": "Mo"}, headers=auth()).get_json()
    assert resp.status_code == 200
    assert day_detail["ok"] is True
    assert day_detail["result"]["plan"]["days"]["Mo"]["events"][0]["items"][0]["note"] == "Nebenmission Road to 100x10"


def test_actliva_patch_training_plan_single_note_persists_raw_plan_json_and_keeps_other_day_copy(client):
    target_id = _create_training_plan(
        client,
        "Rotation Note Patch",
        False,
        [
            {"day": "Mo", "events": [{"kind": "gym", "title": "Push", "items": [{"kind": "exercise", "name": "Schrägbankdrücken", "variation": "KH", "note": "CORE Hauptmission Road to 40s", "sets": 3, "reps": {"min": 6, "max": 8}, "rpe_list": [8, 8, 9]}]}]},
            {"day": "Di", "events": [{"kind": "gym", "title": "Pull", "items": [{"kind": "exercise", "name": "RDLs", "note": "CORE Hams/Posterior Chain", "sets": 3, "reps": {"min": 6, "max": 8}, "rpe_list": [8, 8, 9]}]}]},
            {"day": "Mi", "events": [{"kind": "gym", "title": "Push B", "items": [{"kind": "exercise", "name": "Schrägbankdrücken", "variation": "KH", "note": "CORE Technik + Chest-Frequenz", "sets": 3, "reps": {"min": 8, "max": 10}, "rpe_list": [8, 8, 8]}]}]},
            {"day": "Do", "events": [{"kind": "gym", "title": "Pull B", "items": [{"kind": "exercise", "name": "PullUps", "note": "CORE Fun-/Skill-Ziel", "sets": 3, "reps": {"min": 5, "max": 8}, "rpe_list": [8, 8, 9]}]}]},
        ],
    )
    conn = connections.get_plans_db()
    _seed_rotation_mirror_plan(conn, target_id)
    conn.close()

    resp = client.post(
        "/api/v2/actions/liva/act",
        json={
            "domain": "training",
            "command": "patch_training_plan",
            "plan_id": target_id,
            "confirm": True,
            "dry_run": False,
            "operations": [{"op": "update_exercise_note", "match": {"day": "Mo", "exercise": "Schrägbankdrücken", "variation": "KH"}, "note": "Hauptmission Road to 40s"}],
        },
        headers=auth(),
    )
    conn = connections.get_plans_db()
    conn.row_factory = sqlite3.Row
    raw = json.loads(conn.execute("SELECT plan_json FROM gym_plans WHERE id=?", (target_id,)).fetchone()["plan_json"])
    conn.close()
    day_detail = client.post("/api/v2/actions/liva/read", json={"mode": "training_plan", "plan_id": target_id, "detail": "day", "day": "Mo"}, headers=auth()).get_json()
    full = client.post("/api/v2/actions/liva/read", json={"mode": "training_plan", "plan_id": target_id, "detail": "full"}, headers=auth()).get_json()
    body = resp.get_json()

    assert resp.status_code == 200
    assert body["ok"] is True
    assert raw["base_week"]["Mo"][0]["items"][0]["note"] == "Hauptmission Road to 40s"
    assert raw["sequence"][0]["items"][0]["note"] == "Hauptmission Road to 40s"
    assert raw["base_week"]["Mi"][0]["items"][0]["note"] == "CORE Technik + Chest-Frequenz"
    assert day_detail["result"]["plan"]["days"]["Mo"]["events"][0]["items"][0]["note"] == "Hauptmission Road to 40s"
    assert full["result"]["plan"]["days"]["Mo"]["events"][0]["items"][0]["note"] == "Hauptmission Road to 40s"
    rotation_notes = [item.get("note") for event in full["result"]["plan"]["days"]["rotation"]["events"] for item in (event.get("items") or []) if item.get("kind") == "exercise"]
    assert "Hauptmission Road to 40s" in rotation_notes
    assert raw["sequence"][2]["items"][0]["note"] == "CORE Technik + Chest-Frequenz"
    assert "CORE Hauptmission Road to 40s" not in json.dumps(raw, ensure_ascii=False)


def test_actliva_patch_training_plan_full_read_updates_rotation_and_batch_notes(client):
    target_id = _create_training_plan(
        client,
        "Rotation Note Patch Batch",
        False,
        [
            {"day": "Mo", "events": [{"kind": "gym", "title": "Push", "items": [{"kind": "exercise", "name": "Schrägbankdrücken", "variation": "KH", "note": "CORE Hauptmission Road to 40s", "sets": 3, "reps": {"min": 6, "max": 8}, "rpe_list": [8, 8, 9]}]}]},
            {"day": "Di", "events": [{"kind": "gym", "title": "Pull", "items": [{"kind": "exercise", "name": "RDLs", "note": "CORE Hams/Posterior Chain", "sets": 3, "reps": {"min": 6, "max": 8}, "rpe_list": [8, 8, 9]}]}]},
            {"day": "Mi", "events": [{"kind": "gym", "title": "Push B", "items": [{"kind": "exercise", "name": "OHP", "note": "CORE Schulter-/Press-Kraftanker", "sets": 3, "reps": {"min": 6, "max": 8}, "rpe_list": [8, 8, 9]}]}]},
            {"day": "Do", "events": [{"kind": "gym", "title": "Pull B", "items": [{"kind": "exercise", "name": "Curls", "note": "CORE Bizeps standardisieren", "sets": 3, "reps": {"min": 10, "max": 12}, "rpe_list": [8, 8, 9]}]}]},
        ],
    )
    conn = connections.get_plans_db()
    _seed_rotation_mirror_plan(conn, target_id)
    conn.close()

    resp = client.post(
        "/api/v2/actions/liva/act",
        json={
            "domain": "training",
            "command": "patch_training_plan",
            "plan_id": target_id,
            "confirm": True,
            "dry_run": False,
            "operations": [
                {"op": "update_exercise_note", "match": {"day": "Mo", "exercise": "Schrägbankdrücken", "variation": "KH"}, "note": "Hauptmission Road to 40s"},
                {"op": "update_exercise_note", "match": {"day": "Di", "exercise": "RDLs"}, "note": "Hams/Posterior Chain"},
                {"op": "update_exercise_note", "match": {"day": "Mi", "exercise": "OHP"}, "note": "Schulter-/Press-Kraftanker"},
                {"op": "update_exercise_note", "match": {"day": "Do", "exercise": "Curls"}, "note": "Bizeps standardisieren"},
            ],
        },
        headers=auth(),
    )
    conn = connections.get_plans_db()
    conn.row_factory = sqlite3.Row
    raw = json.loads(conn.execute("SELECT plan_json FROM gym_plans WHERE id=?", (target_id,)).fetchone()["plan_json"])
    conn.close()
    full = client.post("/api/v2/actions/liva/read", json={"mode": "training_plan", "plan_id": target_id, "detail": "full"}, headers=auth()).get_json()
    plans = client.post("/api/v2/actions/liva/read", json={"mode": "training_plans"}, headers=auth()).get_json()["result"]["plans"]
    body = resp.get_json()

    assert resp.status_code == 200
    assert body["ok"] is True
    assert body["result"]["matched_count"] == 4
    assert body["result"]["unmatched_count"] == 0
    assert body["result"]["changed_notes_count"] == 4
    assert full["result"]["plan"]["days"]["Mo"]["events"][0]["items"][0]["note"] == "Hauptmission Road to 40s"
    assert full["result"]["plan"]["days"]["Di"]["events"][0]["items"][0]["note"] == "Hams/Posterior Chain"
    rotation_events = full["result"]["plan"]["days"]["rotation"]["events"]
    rotation_notes = [item.get("note") for event in rotation_events for item in (event.get("items") or []) if item.get("kind") == "exercise"]
    assert "CORE Hauptmission Road to 40s" not in rotation_notes
    assert "CORE Hams/Posterior Chain" not in rotation_notes
    assert "Hauptmission Road to 40s" in rotation_notes
    assert "Hams/Posterior Chain" in rotation_notes
    raw_dump = json.dumps(raw, ensure_ascii=False)
    assert "CORE Hauptmission Road to 40s" not in raw_dump
    assert "CORE Hams/Posterior Chain" not in raw_dump
    assert "CORE Schulter-/Press-Kraftanker" not in raw_dump
    assert "CORE Bizeps standardisieren" not in raw_dump
    summary = next(plan["summary"] for plan in plans if plan["id"] == target_id)
    assert summary["days_count"] == 4
    assert summary["strength_days"] == 4


def test_actliva_patch_training_plan_note_match_accepts_workout_title_display_name_and_device(client):
    target_id = _create_training_plan(
        client,
        "Workout Title Note Match",
        False,
        [
            {
                "day": "Mi",
                "events": [
                    {
                        "kind": "gym",
                        "title": "Push B · OHP + Beinpresse + Chest Volume",
                        "items": [
                            {
                                "kind": "exercise",
                                "name": "OHP",
                                "variation": "eGym",
                                "note": "Alt",
                                "sets": 2,
                                "reps": {"min": 6, "max": 10},
                                "rpe_list": [8, 9],
                            }
                        ],
                    }
                ],
            }
        ],
    )
    conn = connections.get_plans_db()
    _seed_rotation_mirror_plan(conn, target_id)
    conn.close()

    resp = client.post(
        "/api/v2/actions/liva/act",
        json={
            "domain": "training",
            "command": "patch_training_plan",
            "plan_id": target_id,
            "confirm": True,
            "dry_run": False,
            "operations": [
                {
                    "op": "update_exercise_note",
                    "match": {
                        "day": "Push B · OHP + Beinpresse + Chest Volume",
                        "exercise": "OHP (eGym)",
                        "device": "eGym",
                    },
                    "note": "Schulter-/Press-Kraftanker | eGym als offizielle Press-Linie",
                }
            ],
        },
        headers=auth(),
    )
    body = resp.get_json()
    detail = client.post("/api/v2/actions/liva/read", json={"mode": "training_plan", "plan_id": target_id, "detail": "full"}, headers=auth()).get_json()

    assert resp.status_code == 200
    assert body["ok"] is True
    assert body["result"]["matched_count"] == 1
    assert body["result"]["unmatched_count"] == 0
    assert detail["result"]["plan"]["days"]["Mi"]["events"][0]["items"][0]["note"] == "Schulter-/Press-Kraftanker | eGym als offizielle Press-Linie"


def test_readliva_rolling_sequence_full_read_includes_event_ids_and_exercises_for_persisted_sequence(client):
    plan_id = _create_training_plan(
        client,
        "Rolling Readback IDs",
        False,
        [
            {"day": "Mo", "events": [{"kind": "cardio", "mode": "run", "title": "Run A", "items": [{"kind": "run", "duration_min": 30}]}]},
        ],
    )
    conn = connections.get_plans_db()
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT plan_json FROM gym_plans WHERE id=?", (plan_id,)).fetchone()
    plan_json = json.loads(row["plan_json"])
    plan_json["meta"]["mode"] = "rolling_sequence"
    plan_json["sequence"] = [
        {
            "kind": "gym",
            "title": "Push A · Schrägbank + Squat",
            "items": [
                {
                    "kind": "exercise",
                    "name": "Schrägbankdrücken",
                    "variation": "KH",
                    "sets": 3,
                    "reps": {"min": 6, "max": 10},
                    "rpe_list": [8, 8, 9],
                    "note": "Top-Slot",
                }
            ],
        }
    ]
    conn.execute("UPDATE gym_plans SET plan_json=? WHERE id=?", (json.dumps(plan_json, ensure_ascii=False), plan_id))
    conn.commit()
    conn.close()

    full = client.post("/api/v2/actions/liva/read", json={"mode": "training_plan", "plan_id": plan_id, "detail": "full"}, headers=auth()).get_json()
    rotation = full["result"]["plan"]["days"]["rotation"]
    assert rotation["events"][0]["id"]
    assert rotation["events"][0]["title"] == "Push A · Schrägbank + Squat"
    assert rotation["events"][0]["exercises"][0]["display_name"] == "Schrägbankdrücken (KH)"
    assert rotation["events"][0]["exercises"][0]["exercise_id"]
    assert rotation["events"][0]["exercises"][0]["rep_range"] == {"min": 6, "max": 10}
    assert rotation["exercises"][0]["note"] == "Top-Slot"

    exercise_detail = client.post(
        "/api/v2/actions/liva/read",
        json={"mode": "training_plan", "plan_id": plan_id, "detail": "exercise", "day": "rotation", "exercise": "Schrägbankdrücken (KH)"},
        headers=auth(),
    ).get_json()
    assert exercise_detail["ok"] is True
    assert exercise_detail["result"]["plan"]["target"]["display_name"] == "Schrägbankdrücken (KH)"
    assert exercise_detail["result"]["plan"]["target"]["rep_range"] == {"min": 6, "max": 10}


def test_direct_training_plan_patch_supports_rolling_rotation_exercise_path(client):
    plan_id = _create_training_plan(
        client,
        "Rolling Direct Patch",
        False,
        [{"day": "Mo", "events": [{"kind": "cardio", "title": "Run A", "items": [{"kind": "run", "duration_min": 30}]}]}],
    )
    conn = connections.get_plans_db()
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT plan_json FROM gym_plans WHERE id=?", (plan_id,)).fetchone()
    plan_json = json.loads(row["plan_json"])
    plan_json["meta"]["mode"] = "rolling_sequence"
    plan_json["sequence"] = [{"id": "pull-a", "kind": "gym", "title": "Pull A", "items": [{"kind": "exercise", "id": "ex-old", "name": "Latzug", "variation": "eGym", "sets": 3, "reps": {"min": 8, "max": 12}}]}]
    conn.execute("UPDATE gym_plans SET plan_json=? WHERE id=?", (json.dumps(plan_json, ensure_ascii=False), plan_id))
    conn.commit()
    conn.close()

    detail = client.get(f"/api/v2/actions/training/plans/{plan_id}/detail", headers=auth()).get_json()
    exercise = detail["days"][-1]["workouts"][0]["exercises"][0]
    assert detail["days"][-1]["day_id"] == "rotation"

    preview = client.post(
        f"/api/v2/actions/training/plans/{plan_id}/patch",
        json={"dry_run": True, "operations": [{"op": "replace_exercise", "match": {"exercise_path": exercise["exercise_path"]}, "replacement": {"name": "Weighted Pull-ups", "variation": "weighted", "exercise_id": "ex-new"}}]},
        headers=auth(),
    ).get_json()
    assert preview["ok"] is True
    assert preview["execution"]["live_state_changed"] is False
    assert preview["after"]["days"][-1]["workouts"][0]["exercises"][0]["name"] == "Weighted Pull-ups"

    live = client.post(
        f"/api/v2/actions/training/plans/{plan_id}/patch",
        json={"dry_run": False, "operations": [{"op": "replace_exercise", "match": {"exercise_path": exercise["exercise_path"]}, "replacement": {"name": "Weighted Pull-ups", "variation": "weighted", "exercise_id": "ex-new"}}]},
        headers=auth(),
    ).get_json()
    assert live["ok"] is True
    assert live["after"]["days"][-1]["workouts"][0]["exercises"][0]["name"] == "Weighted Pull-ups"


def test_actliva_patch_training_plan_batch_with_unmatched_note_op_rolls_back(client):
    target_id = _create_training_plan(client, "Atomic Note Patch", False, [{"day": "Mo", "events": [{"kind": "gym", "title": "Push", "items": [{"kind": "exercise", "name": "Bench", "note": "CORE Alt", "sets": 3, "reps": {"min": 6, "max": 8}, "rpe_list": [8, 8, 9]}]}]}])
    resp = client.post(
        "/api/v2/actions/liva/act",
        json={
            "domain": "training",
            "command": "patch_training_plan",
            "plan_id": target_id,
            "confirm": True,
            "dry_run": False,
            "operations": [
                {"op": "update_exercise_note", "match": {"day": "Mo", "exercise": "Bench"}, "note": "Neu"},
                {"op": "update_exercise_note", "match": {"day": "Di", "exercise": "Nicht Da"}, "note": "Fehlt"},
            ],
        },
        headers=auth(),
    )
    conn = connections.get_plans_db()
    conn.row_factory = sqlite3.Row
    raw = json.loads(conn.execute("SELECT plan_json FROM gym_plans WHERE id=?", (target_id,)).fetchone()["plan_json"])
    conn.close()
    body = resp.get_json()

    assert resp.status_code == 400
    assert body["ok"] is False
    assert body["execution"]["live_state_changed"] is False
    assert body["error"]["code"] == "operations_failed"
    assert raw["base_week"]["Mo"][0]["items"][0]["note"] == "CORE Alt"


def test_patch_training_plan_replace_cardio_matches_legacy_title_and_run_detail(client):
    plan_id = _create_training_plan(client, "Legacy Cardio", False, [{"day": "Di", "events": [{"kind": "ergo", "time": "19:45", "title": "Z2", "items": [{"kind": "run_detail", "text": "30min locker @ nasal"}]}]}])
    resp = client.post(
        "/api/v2/actions/liva/act",
        json={"domain": "training", "command": "patch_training_plan", "confirm": True, "payload": {"plan_id": plan_id, "operations": [{"op": "replace_cardio", "day": "Di", "title": "Ergo – Z2", "patch": {"duration_min": 40, "intensity": "locker", "notes": "nasal"}}]}},
        headers=auth(),
    )
    assert resp.status_code == 200
    conn = connections.get_plans_db()
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT plan_json FROM gym_plans WHERE id=?", (plan_id,)).fetchone()
    conn.close()
    event = __import__("json").loads(row["plan_json"])["base_week"]["Di"][0]
    assert event["kind"] == "cardio"
    assert event["mode"] == "ergo"
    assert event["title"] == "Ergo – Z2"
    item = event["items"][0]
    assert item["kind"] == "cardio"
    assert item["duration_min"] == 40
    assert item["amount_value"] == 40
    assert item["amount_unit"] == "min"
    assert item["display"] == "40 min locker @ nasal"


def test_patch_training_plan_replace_cardio_ambiguous_when_multiple_candidates(client):
    plan_id = _create_training_plan(
        client,
        "Ambiguous Cardio",
        False,
        [{"day": "Di", "events": [
            {"kind": "ergo", "time": "19:45", "title": "Z2", "items": [{"kind": "run_detail", "text": "30min locker @ nasal"}]},
            {"kind": "run", "time": "07:00", "title": "Easy", "items": [{"kind": "run_detail", "text": "20min locker"}]},
        ]}],
    )
    resp = client.post(
        "/api/v2/actions/liva/act",
        json={"domain": "training", "command": "patch_training_plan", "confirm": True, "payload": {"plan_id": plan_id, "operations": [{"op": "replace_cardio", "day": "Di", "patch": {"duration_min": 40, "intensity": "locker"}}]}},
        headers=auth(),
    )
    body = resp.get_json()
    assert resp.status_code == 200
    assert body["result"]["changed"] is False
    assert body["result"]["operations_failed"][0]["reason"] == "ambiguous_cardio"


def test_patch_training_plan_replace_cardio_not_found_reports_available_events(client):
    plan_id = _create_training_plan(client, "Cardio Preview", False, [{"day": "Di", "events": [{"kind": "gym", "time": "19:45", "title": "Push", "items": [{"kind": "exercise", "name": "Bench", "sets": 3, "reps": {"min": 6, "max": 8}, "rpe_list": [8, 8, 9]}]}]}])
    resp = client.post(
        "/api/v2/actions/liva/act",
        json={"domain": "training", "command": "patch_training_plan", "confirm": True, "payload": {"plan_id": plan_id, "operations": [{"op": "replace_cardio", "day": "Di", "title": "Threshold", "patch": {"duration_min": 40}}]}},
        headers=auth(),
    )
    body = resp.get_json()
    failed = body["result"]["operations_failed"][0]
    assert resp.status_code == 200
    assert failed["reason"] == "cardio_not_found"
    assert failed["requested_day"] == "Di"
    assert failed["requested_title"] == "Threshold"
    assert failed["available_cardio_events"] == []


def test_training_active_plan_not_deleted_or_archived_without_override(client):
    active_id = client.post("/api/v2/actions/liva/read", json={"mode": "training_plans"}, headers=auth()).get_json()["result"]["plans"][0]["id"]
    delete_resp = client.post("/api/v2/actions/liva/act", json={"domain": "training", "command": "delete_training_plan", "confirm": True, "payload": {"plan_id": active_id, "hard_delete": True}}, headers=auth())
    archive_resp = client.post("/api/v2/actions/liva/act", json={"domain": "training", "command": "archive_training_plan", "confirm": True, "payload": {"plan_id": active_id}}, headers=auth())
    assert delete_resp.status_code == 400
    assert archive_resp.status_code == 400


def test_activate_nutrition_plan_sets_template_active(client):
    food_id = _seed_test_food("Reis")
    template_id = _create_nutrition_plan(client, "Alt Template", False, [{"day": "Mo", "meals": [{"title": "Meal", "items": [{"food_id": food_id, "amount": 100, "unit": "g"}]}]}])
    resp = client.post("/api/v2/actions/liva/act", json={"domain": "nutrition", "command": "activate_nutrition_plan", "confirm": True, "payload": {"template_id": template_id}}, headers=auth())
    assert resp.status_code == 200
    plans = client.post("/api/v2/actions/liva/read", json={"mode": "nutrition_plans"}, headers=auth()).get_json()["result"]["plans"]
    assert any(plan["id"] == template_id and plan["is_active"] for plan in plans)


def test_delete_nutrition_plan_deletes_template_days_slots_not_global_meals(client):
    food_id = _seed_test_food("Haferflocken")
    template_id = _create_nutrition_plan(client, "Delete Template", False, [{"day": "Mo", "meals": [{"title": "Oats", "items": [{"food_id": food_id, "amount": 100, "unit": "g"}]}]}])
    conn = connections.get_nutrition_db()
    meal_row = conn.execute("SELECT id FROM nutrition_meal_templates WHERE title='Oats' ORDER BY id DESC LIMIT 1").fetchone()
    conn.close()
    resp = client.post("/api/v2/actions/liva/act", json={"domain": "nutrition", "command": "delete_nutrition_plan", "confirm": True, "payload": {"template_id": template_id, "hard_delete": True}}, headers=auth())
    body = resp.get_json()
    assert resp.status_code == 200
    conn = connections.get_nutrition_db()
    assert conn.execute("SELECT id FROM nutrition_week_templates WHERE id=?", (template_id,)).fetchone() is None
    assert conn.execute("SELECT id FROM nutrition_meal_templates WHERE id=?", (meal_row["id"],)).fetchone() is not None
    conn.close()
    assert body["result"]["slots_deleted"] >= 1


def test_replace_nutrition_plan_keeps_template_id_and_replaces_slots(client):
    food_a = _seed_test_food("Kartoffel")
    food_b = _seed_test_food("Huhn")
    template_id = _create_nutrition_plan(client, "Replace Nutri", False, [{"day": "Mo", "meals": [{"time": "06:00", "title": "Alt", "items": [{"food_id": food_a, "amount": 100, "unit": "g"}]}]}])
    resp = client.post(
        "/api/v2/actions/liva/act",
        json={"domain": "nutrition", "command": "replace_nutrition_plan", "confirm": True, "payload": {"template_id": template_id, "replace_with": {"name": "Neu", "days": [{"day": "Di", "meals": [{"time": "07:15", "title": "Neu Meal", "items": [{"food_id": food_b, "amount": 200, "unit": "g"}]}]}]}}},
        headers=auth(),
    )
    body = resp.get_json()
    assert resp.status_code == 200
    assert body["result"]["template_id"] == template_id
    assert body["result"]["created_template_after"]["title"] == "Neu"


def test_patch_nutrition_plan_updates_meal_amount_and_unknown_food_aborts(client):
    food_a = _seed_test_food("Skyr Natur")
    food_b = _seed_test_food("Banane")
    template_id = _create_nutrition_plan(client, "Patch Nutri", False, [{"day": "Mo", "meals": [{"time": "06:40", "title": "Bowl", "items": [{"food_id": food_a, "amount": 200, "unit": "g"}]}]}])
    bad = client.post(
        "/api/v2/actions/liva/act",
        json={"domain": "nutrition", "command": "patch_nutrition_plan", "confirm": True, "payload": {"template_id": template_id, "operations": [{"op": "replace_meal_slot", "day": "Mo", "time": "06:40", "meal": {"title": "Bad", "items": [{"food_name": "gibt-es-nicht", "amount": 10, "unit": "g"}]}}]}},
        headers=auth(),
    )
    assert bad.status_code == 200
    assert bad.get_json()["result"]["ok"] is False
    resp = client.post(
        "/api/v2/actions/liva/act",
        json={"domain": "nutrition", "command": "patch_nutrition_plan", "confirm": True, "payload": {"template_id": template_id, "operations": [{"op": "replace_meal_slot", "day": "Mo", "time": "06:40", "meal": {"title": "Bowl 2", "items": [{"food_id": food_a, "amount": 250, "unit": "g"}, {"food_id": food_b, "amount": 100, "unit": "g"}]}}]}},
        headers=auth(),
    )
    body = resp.get_json()
    assert resp.status_code == 200
    monday = next(day for day in body["result"]["updated_plan_after"]["days"] if day["label"] == "Mo")
    assert monday["slots"][0]["items"][0]["amount"] == 250.0


def test_actliva_patch_nutrition_plan_top_level_operations_updates_only_target_template(client):
    food_a = _seed_test_food("ActLiva Skyr")
    active_id = _create_nutrition_plan(client, "Active Nutrition Guard", True, [{"day": "Mo", "meals": [{"time": "08:00", "title": "Active Bowl", "items": [{"food_id": food_a, "amount": 200, "unit": "g"}]}]}])
    target_id = _create_nutrition_plan(client, "Patch Nutrition Target", False, [{"day": "Mo", "meals": [{"time": "06:40", "title": "Bowl", "items": [{"food_id": food_a, "amount": 200, "unit": "g"}]}]}])
    conn = connections.get_nutrition_db()
    before_templates = conn.execute("SELECT COUNT(*) FROM nutrition_week_templates").fetchone()[0]
    before_overrides = conn.execute("SELECT COUNT(*) FROM nutrition_slot_meal_overrides").fetchone()[0]
    active_before = conn.execute("SELECT is_active FROM nutrition_week_templates WHERE id=?", (active_id,)).fetchone()[0]
    conn.close()

    resp = client.post(
        "/api/v2/actions/liva/act",
        json={
            "domain": "nutrition",
            "command": "patch_nutrition_plan",
            "template_id": target_id,
            "confirm": True,
            "dry_run": False,
            "operations": [{"op": "replace_meal_slot", "day": "Mo", "time": "06:40", "meal": {"title": "Bowl 2", "items": [{"food_id": food_a, "amount": 250, "unit": "g"}]}}],
        },
        headers=auth(),
    )
    body = resp.get_json()
    assert resp.status_code == 200
    assert body["ok"] is True
    assert body["execution"]["live_state_changed"] is True
    conn = connections.get_nutrition_db()
    after_templates = conn.execute("SELECT COUNT(*) FROM nutrition_week_templates").fetchone()[0]
    after_overrides = conn.execute("SELECT COUNT(*) FROM nutrition_slot_meal_overrides").fetchone()[0]
    active_after = conn.execute("SELECT is_active FROM nutrition_week_templates WHERE id=?", (active_id,)).fetchone()[0]
    conn.close()
    monday = next(day for day in body["result"]["updated_plan_after"]["days"] if day["label"] == "Mo")
    assert monday["slots"][0]["items"][0]["amount"] == 250.0
    assert before_templates == after_templates
    assert before_overrides == after_overrides
    assert active_before == active_after == 1


def test_actliva_patch_nutrition_plan_nested_payload_operations_works(client):
    food_a = _seed_test_food("ActLiva Skyr Nested")
    target_id = _create_nutrition_plan(client, "Patch Nutrition Nested", False, [{"day": "Mo", "meals": [{"time": "06:40", "title": "Bowl", "items": [{"food_id": food_a, "amount": 200, "unit": "g"}]}]}])
    conn = connections.get_nutrition_db()
    before_templates = conn.execute("SELECT COUNT(*) FROM nutrition_week_templates").fetchone()[0]
    conn.close()

    resp = client.post(
        "/api/v2/actions/liva/act",
        json={
            "domain": "nutrition",
            "command": "patch_nutrition_plan",
            "confirm": True,
            "dry_run": False,
            "payload": {
                "template_id": target_id,
                "operations": [{"op": "replace_meal_slot", "day": "Mo", "time": "06:40", "meal": {"title": "Bowl 3", "items": [{"food_id": food_a, "amount": 260, "unit": "g"}]}}],
            },
        },
        headers=auth(),
    )
    body = resp.get_json()
    assert resp.status_code == 200
    assert body["ok"] is True
    assert body["execution"]["live_state_changed"] is True
    conn = connections.get_nutrition_db()
    after_templates = conn.execute("SELECT COUNT(*) FROM nutrition_week_templates").fetchone()[0]
    conn.close()
    monday = next(day for day in body["result"]["updated_plan_after"]["days"] if day["label"] == "Mo")
    assert monday["slots"][0]["items"][0]["amount"] == 260.0
    assert before_templates == after_templates


def test_actliva_patch_nutrition_plan_missing_target_or_operations_stops_without_new_template(client):
    food_a = _seed_test_food("ActLiva Skyr Invalid")
    target_id = _create_nutrition_plan(client, "Invalid Nutrition Patch", False, [{"day": "Mo", "meals": [{"time": "06:40", "title": "Bowl", "items": [{"food_id": food_a, "amount": 200, "unit": "g"}]}]}])
    conn = connections.get_nutrition_db()
    before_templates = conn.execute("SELECT COUNT(*) FROM nutrition_week_templates").fetchone()[0]
    conn.close()

    missing_target = client.post(
        "/api/v2/actions/liva/act",
        json={"domain": "nutrition", "command": "patch_nutrition_plan", "confirm": True, "dry_run": False, "operations": [{"op": "rename_meal", "day": "Mo", "time": "06:40", "title": "X"}]},
        headers=auth(),
    )
    missing_ops = client.post(
        "/api/v2/actions/liva/act",
        json={"domain": "nutrition", "command": "patch_nutrition_plan", "confirm": True, "dry_run": False, "template_id": target_id},
        headers=auth(),
    )
    for resp, code in ((missing_target, "target_plan_required"), (missing_ops, "operations_required")):
        body = resp.get_json()
        assert resp.status_code == 400
        assert body["ok"] is False
        assert body["error"]["code"] == code
    conn = connections.get_nutrition_db()
    after_templates = conn.execute("SELECT COUNT(*) FROM nutrition_week_templates").fetchone()[0]
    conn.close()
    assert before_templates == after_templates


def test_readliva_lists_nutrition_plans(client):
    _ensure_test_planning_schema()
    week, err = nutrition_db.create_week_template("Mini-Cut", set_active=True)
    assert err is None
    resp = client.post("/api/v2/actions/liva/read", json={"mode": "nutrition_plans"}, headers=auth())
    plans = resp.get_json()["result"]["plans"]
    assert resp.status_code == 200
    assert any(plan["id"] == week["id"] and plan["is_active"] for plan in plans)


def test_nutrition_plans_summary_counts_legacy_slot_meals_and_items(client):
    _ensure_test_planning_schema()
    week, err = nutrition_db.create_week_template("Legacy Plan", set_active=False)
    assert err is None
    template_id = int(week["id"])
    conn = connections.get_nutrition_db()
    conn.row_factory = sqlite3.Row
    day_rows = conn.execute("SELECT id FROM nutrition_week_template_days WHERE week_template_id=? ORDER BY weekday", (template_id,)).fetchall()
    slot_rows = conn.execute("SELECT id, week_template_day_id, slot_index FROM nutrition_week_day_slots WHERE week_template_day_id IN (SELECT id FROM nutrition_week_template_days WHERE week_template_id=?) ORDER BY week_template_day_id, slot_index", (template_id,)).fetchall()
    assert day_rows
    assert slot_rows
    now = "2026-05-24T10:00:00"
    inserted_meal_ids = []
    for day_row in day_rows:
        meal_cur = conn.execute(
            "INSERT INTO nutrition_slot_meals (week_template_day_id, slot_index, meal_title, multiplier, template_id, created_at, updated_at) VALUES (?, 0, ?, 1.0, NULL, ?, ?)",
            (int(day_row["id"]), "Legacy Meal", now, now),
        )
        inserted_meal_ids.append(int(meal_cur.lastrowid))
    for slot_meal_id in inserted_meal_ids:
        conn.execute(
            "INSERT INTO nutrition_slot_meal_items (slot_meal_id, amount, unit, name_raw, food_id, item_type, calories, display_text, created_at, updated_at) VALUES (?, 100, 'g', 'Food A', NULL, 'food', NULL, 'Food A', ?, ?)",
            (slot_meal_id, now, now),
        )
        conn.execute(
            "INSERT INTO nutrition_slot_meal_items (slot_meal_id, amount, unit, name_raw, food_id, item_type, calories, display_text, created_at, updated_at) VALUES (?, 200, 'g', 'Food B', NULL, 'food', NULL, 'Food B', ?, ?)",
            (slot_meal_id, now, now),
        )
    conn.commit()
    expected_days = conn.execute("SELECT COUNT(*) AS c FROM nutrition_week_template_days WHERE week_template_id=?", (template_id,)).fetchone()["c"]
    expected_slots = conn.execute("SELECT COUNT(*) AS c FROM nutrition_week_day_slots WHERE week_template_day_id IN (SELECT id FROM nutrition_week_template_days WHERE week_template_id=?)", (template_id,)).fetchone()["c"]
    expected_meals = conn.execute("SELECT COUNT(*) AS c FROM nutrition_slot_meals WHERE week_template_day_id IN (SELECT id FROM nutrition_week_template_days WHERE week_template_id=?)", (template_id,)).fetchone()["c"]
    expected_items = conn.execute("SELECT COUNT(*) AS c FROM nutrition_slot_meal_items WHERE slot_meal_id IN (SELECT id FROM nutrition_slot_meals WHERE week_template_day_id IN (SELECT id FROM nutrition_week_template_days WHERE week_template_id=?))", (template_id,)).fetchone()["c"]
    conn.close()
    resp = client.post("/api/v2/actions/liva/read", json={"mode": "nutrition_plans"}, headers=auth())
    plans = resp.get_json()["result"]["plans"]
    summary = next(plan["summary"] for plan in plans if plan["id"] == template_id)
    assert summary["days_count"] == expected_days
    assert summary["slots_count"] == expected_slots
    assert summary["filled_slots_count"] == expected_meals
    assert summary["filled_days_count"] == expected_days
    assert summary["meals_count"] == expected_meals
    assert summary["items_count"] == expected_items


def test_nutrition_plans_summary_counts_new_style_slots(client):
    food_id = _seed_test_food("Summary Brot")
    template_id = _create_nutrition_plan(client, "New Style Summary", False, [{"day": "Mo", "meals": [{"time": "06:40", "title": "Brot", "items": [{"food_id": food_id, "amount": 100, "unit": "g"}]}]}])
    resp = client.post("/api/v2/actions/liva/read", json={"mode": "nutrition_plans"}, headers=auth())
    plans = resp.get_json()["result"]["plans"]
    summary = next(plan["summary"] for plan in plans if plan["id"] == template_id)
    assert summary["filled_slots_count"] == 1
    assert summary["filled_days_count"] == 1
    assert summary["meals_count"] == 1
    assert summary["items_count"] == 1


def test_nutrition_plans_summary_avoids_double_count_for_same_day_slot(client):
    food_id = _seed_test_food("Double Slot Food")
    template_id = _create_nutrition_plan(client, "No Double Count", False, [{"day": "Mo", "meals": [{"time": "08:00", "title": "One", "items": [{"food_id": food_id, "amount": 100, "unit": "g"}]}]}])
    conn = connections.get_nutrition_db()
    conn.row_factory = sqlite3.Row
    day_row = conn.execute("SELECT id FROM nutrition_week_template_days WHERE week_template_id=? AND weekday=0", (template_id,)).fetchone()
    now = "2026-05-24T10:30:00"
    meal_cur = conn.execute(
        "INSERT OR REPLACE INTO nutrition_slot_meals (week_template_day_id, slot_index, meal_title, multiplier, template_id, created_at, updated_at) VALUES (?, 0, ?, 1.0, NULL, ?, ?)",
        (int(day_row["id"]), "Legacy Overlay", now, now),
    )
    slot_meal_id = int(meal_cur.lastrowid or conn.execute("SELECT id FROM nutrition_slot_meals WHERE week_template_day_id=? AND slot_index=0", (int(day_row["id"]),)).fetchone()["id"])
    conn.execute(
        "INSERT INTO nutrition_slot_meal_items (slot_meal_id, amount, unit, name_raw, food_id, item_type, calories, display_text, created_at, updated_at) VALUES (?, 50, 'g', 'Overlay', NULL, 'food', NULL, 'Overlay', ?, ?)",
        (slot_meal_id, now, now),
    )
    conn.commit()
    conn.close()
    resp = client.post("/api/v2/actions/liva/read", json={"mode": "nutrition_plans"}, headers=auth())
    plans = resp.get_json()["result"]["plans"]
    summary = next(plan["summary"] for plan in plans if plan["id"] == template_id)
    assert summary["filled_slots_count"] == 1
    assert summary["meals_count"] == 1
    assert summary["items_count"] == 1


def test_create_nutrition_plan_live_persists_summary_targets_and_single_template(client):
    food_a = _seed_test_food("Persist Summary Brot")
    food_b = _seed_test_food("Persist Summary Milch")
    food_c = _seed_test_food("Persist Summary Whey")
    food_d = _seed_test_food("Persist Summary Reis")
    food_e = _seed_test_food("Persist Summary Huhn")
    food_f = _seed_test_food("Persist Summary Banane")
    conn = connections.get_nutrition_db()
    before_templates = conn.execute("SELECT COUNT(*) FROM nutrition_week_templates").fetchone()[0]
    conn.close()

    resp = client.post(
        "/api/v2/actions/liva/act",
        json={
            "domain": "nutrition",
            "command": "create_nutrition_plan",
            "confirm": True,
            "dry_run": False,
            "name": "DEBUG Nutrition Persistenz Fixture",
            "set_active": False,
            "target_kcal": 2700,
            "target_p": 180,
            "target_c": 360,
            "target_f": 60,
            "value": {
                "days": [
                    {
                        "day": "Mo",
                        "meals": [
                            {
                                "time": "06:40",
                                "title": "Meal 1",
                                "items": [
                                    {"food_id": food_a, "amount": 100, "unit": "g"},
                                    {"food_id": food_b, "amount": 250, "unit": "ml"},
                                    {"food_id": food_c, "amount": 30, "unit": "g"},
                                ],
                            },
                            {
                                "time": "12:30",
                                "title": "Meal 2",
                                "items": [
                                    {"food_id": food_d, "amount": 150, "unit": "g"},
                                    {"food_id": food_e, "amount": 200, "unit": "g"},
                                    {"food_id": food_f, "amount": 120, "unit": "g"},
                                ],
                            },
                        ],
                    }
                ]
            },
        },
        headers=auth(),
    )
    body = resp.get_json()
    template_id = body["result"]["template_id"]
    plans = client.post("/api/v2/actions/liva/read", json={"mode": "nutrition_plans"}, headers=auth()).get_json()["result"]["plans"]
    summary = next(plan["summary"] for plan in plans if plan["id"] == template_id)
    conn = connections.get_nutrition_db()
    conn.row_factory = sqlite3.Row
    after_templates = conn.execute("SELECT COUNT(*) FROM nutrition_week_templates").fetchone()[0]
    day_rows = conn.execute(
        "SELECT weekday, target_kcal, target_p, target_c, target_f FROM nutrition_week_template_days WHERE week_template_id=? ORDER BY weekday",
        (template_id,),
    ).fetchall()
    slot_rows = conn.execute(
        "SELECT id, meal_template_id FROM nutrition_week_day_slots WHERE week_template_day_id IN (SELECT id FROM nutrition_week_template_days WHERE week_template_id=?) ORDER BY id",
        (template_id,),
    ).fetchall()
    ingredient_count = conn.execute(
        "SELECT COUNT(*) AS c FROM nutrition_meal_ingredients WHERE meal_template_id IN (SELECT meal_template_id FROM nutrition_week_day_slots WHERE week_template_day_id IN (SELECT id FROM nutrition_week_template_days WHERE week_template_id=?))",
        (template_id,),
    ).fetchone()["c"]
    template_row = conn.execute(
        "SELECT is_active, macro_active_mode, macro_modes_json FROM nutrition_week_templates WHERE id=?",
        (template_id,),
    ).fetchone()
    conn.close()

    assert resp.status_code == 200
    assert body["ok"] is True
    assert body["execution"]["live_state_changed"] is True
    assert body["execution"]["detail"] == "Created an inactive nutrition week template."
    assert body["result"]["created_template_after"]["is_active"] is False
    assert after_templates == before_templates + 1
    assert len(day_rows) == 7
    assert len(slot_rows) == 2
    assert ingredient_count == 6
    assert summary["days_count"] == 7
    assert summary["filled_days_count"] == 1
    assert summary["filled_slots_count"] == 2
    assert summary["meals_count"] == 2
    assert summary["items_count"] == 6
    assert template_row["is_active"] == 0
    assert template_row["macro_active_mode"] == "custom"
    assert "\"kcal_target\": 2700" in str(template_row["macro_modes_json"])
    assert "\"protein_target\": 180" in str(template_row["macro_modes_json"])
    assert "\"carbs_target\": 360" in str(template_row["macro_modes_json"])
    assert "\"fat_target\": 60" in str(template_row["macro_modes_json"])
    assert all(row["target_kcal"] == 2700 for row in day_rows)
    assert all(row["target_p"] == 180 for row in day_rows)
    assert all(row["target_c"] == 360 for row in day_rows)
    assert all(row["target_f"] == 60 for row in day_rows)


def test_create_nutrition_plan_set_active_true_marks_new_template_active(client):
    food_id = _seed_test_food("Set Active Reis")
    old_active_id = _create_nutrition_plan(client, "Old Active Nutrition", True, [{"day": "Mo", "meals": [{"title": "Old", "items": [{"food_id": food_id, "amount": 100, "unit": "g"}]}]}])
    resp = client.post(
        "/api/v2/actions/liva/act",
        json={
            "domain": "nutrition",
            "command": "create_nutrition_plan",
            "confirm": True,
            "dry_run": False,
            "payload": {
                "name": "New Active Nutrition",
                "set_active": True,
                "days": [{"day": "Mo", "meals": [{"title": "New", "items": [{"food_id": food_id, "amount": 120, "unit": "g"}]}]}],
            },
        },
        headers=auth(),
    )
    body = resp.get_json()
    new_id = body["result"]["template_id"]
    conn = connections.get_nutrition_db()
    old_active = conn.execute("SELECT is_active FROM nutrition_week_templates WHERE id=?", (old_active_id,)).fetchone()[0]
    new_active = conn.execute("SELECT is_active FROM nutrition_week_templates WHERE id=?", (new_id,)).fetchone()[0]
    conn.close()

    assert resp.status_code == 200
    assert body["execution"]["detail"] == "Created a live nutrition week template and updated the active LIVA nutrition plan."
    assert old_active == 0
    assert new_active == 1
