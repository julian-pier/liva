from tests.test_actions_v2_api import auth, client
from tests.test_actions_v2_plan_lifecycle import _create_training_plan
import database.connections as connections


def test_active_training_plan_detail_returns_full_workouts_exercises_sets(client):
    _create_training_plan(client, "TP Active", True, [{"day": "Mo", "events": [{"kind": "gym", "title": "Push", "items": [{"kind": "exercise", "name": "Bench", "sets": 3, "reps": {"min": 6, "max": 8}, "rpe_list": [8, 8, 9]}]}]}])
    body = client.get("/api/v2/actions/training/plans/active/detail", headers=auth()).get_json()
    assert body["ok"] is True
    assert body["days"][0]["workouts"]
    assert body["days"][0]["workouts"][0]["exercises"]
    assert body["days"][0]["workouts"][0]["exercises"][0]["sets"]


def test_training_patch_dry_run_replace_exercise_preview_only(client):
    plan_id = _create_training_plan(client, "TP Replace", False, [{"day": "Mo", "events": [{"kind": "gym", "title": "Push", "items": [{"kind": "exercise", "name": "Bench", "sets": 3, "reps": {"min": 6, "max": 8}, "rpe_list": [8, 8, 9]}]}]}])
    body = client.post(
        f"/api/v2/actions/training/plans/{plan_id}/patch",
        json={"dry_run": True, "operations": [{"op": "replace_exercise", "match": {"day_index": 0, "workout_index": 0, "exercise_index": 0}, "replacement": {"name": "Incline Bench", "canonical_id": "incline_bench"}}]},
        headers=auth(),
    ).get_json()
    after = client.get(f"/api/v2/actions/training/plans/{plan_id}/detail", headers=auth()).get_json()
    assert body["execution"]["mode"] == "dry_run"
    assert after["days"][0]["workouts"][0]["exercises"][0]["name"] == "Bench"


def test_training_patch_live_persists_and_can_archive(client):
    plan_id = _create_training_plan(client, "TP Live", False, [{"day": "Mo", "events": [{"kind": "gym", "title": "Push", "items": [{"kind": "exercise", "name": "Bench", "sets": 3, "reps": {"min": 6, "max": 8}, "rpe_list": [8, 8, 9]}]}]}])
    live = client.post(
        f"/api/v2/actions/training/plans/{plan_id}/patch",
        json={"dry_run": False, "operations": [{"op": "rename_workout", "match": {"day_index": 0, "workout_index": 0}, "title": "Upper A"}]},
        headers=auth(),
    ).get_json()
    archive = client.post(
        f"/api/v2/actions/training/plans/{plan_id}/patch",
        json={"dry_run": False, "operations": [{"op": "archive_plan"}]},
        headers=auth(),
    ).get_json()
    assert live["after"]["days"][0]["workouts"][0]["title"] == "Upper A"
    assert archive["after"]["archived"] is True


def test_training_patch_set_active_plan(client):
    plan_id = _create_training_plan(client, "TP Activate", False, [{"day": "Di", "events": [{"kind": "gym", "title": "Pull", "items": [{"kind": "exercise", "name": "Row", "sets": 2, "reps": {"min": 8, "max": 10}, "rpe_list": [8, 8]}]}]}])
    body = client.post(
        f"/api/v2/actions/training/plans/{plan_id}/patch",
        json={"dry_run": False, "operations": [{"op": "set_active_plan"}]},
        headers=auth(),
    ).get_json()
    assert body["after"]["is_active"] is True


def test_training_patch_variation_note_set_target_and_caps(client):
    plan_id = _create_training_plan(client, "TP Ops", False, [{"day": "Mo", "events": [{"kind": "gym", "title": "Push", "items": [{"kind": "exercise", "name": "Bench", "variation": "30 Grad", "sets": 2, "reps": {"min": 6, "max": 8}, "rpe_list": [8, 8]}]}]}])
    detail = client.get(f"/api/v2/actions/training/plans/{plan_id}/detail", headers=auth()).get_json()
    exercise_path = detail["days"][0]["workouts"][0]["exercises"][0]["exercise_path"]
    set_path = detail["days"][0]["workouts"][0]["exercises"][0]["sets"][0]["set_path"]
    body = client.post(
        f"/api/v2/actions/training/plans/{plan_id}/patch",
        json={"dry_run": False, "operations": [
            {"op": "update_exercise_variation", "match": {"exercise_path": exercise_path}, "variation": "45 Grad"},
            {"op": "update_exercise_note", "match": {"exercise_path": exercise_path}, "note": "Ellbogen enger"},
            {"op": "update_set_target", "match": {"set_path": set_path}, "target_reps": "6-8", "target_rpe": 8, "rir": 2, "load_target": 40},
            {"op": "update_caps", "scope": {"plan_id": plan_id}, "caps": {"max_rpe": 8.5}},
        ]},
        headers=auth(),
    ).get_json()
    after = client.get(f"/api/v2/actions/training/plans/{plan_id}/detail", headers=auth()).get_json()
    ex = after["days"][0]["workouts"][0]["exercises"][0]
    assert body["ok"] is True
    assert ex["variation"] == "45 Grad"
    assert ex["notes"] == "Ellbogen enger"
    assert ex["sets"][0]["load_target"] == 40
    assert after["caps"]["max_rpe"] == 8.5


def test_training_patch_add_delete_set_and_create_exercise(client):
    plan_id = _create_training_plan(client, "TP Sets", False, [{"day": "Mo", "events": [{"kind": "gym", "title": "Push", "items": [{"kind": "exercise", "name": "Bench", "sets": 1, "reps": {"min": 6, "max": 8}, "rpe_list": [8]}]}]}])
    detail = client.get(f"/api/v2/actions/training/plans/{plan_id}/detail", headers=auth()).get_json()
    exercise_path = detail["days"][0]["workouts"][0]["exercises"][0]["exercise_path"]
    set_path = detail["days"][0]["workouts"][0]["exercises"][0]["sets"][0]["set_path"]
    body = client.post(
        f"/api/v2/actions/training/plans/{plan_id}/patch",
        json={"dry_run": False, "operations": [
            {"op": "add_set", "target": {"exercise_path": exercise_path}, "set": {"target_reps": "8-10", "target_rpe": 8}},
            {"op": "delete_set", "match": {"set_path": set_path}},
            {"op": "create_exercise", "exercise": {"name": "Neue Übung", "canonical_id": "neue_uebung", "muscle_group": "chest", "equipment": "dumbbell", "notes": "test"}},
        ]},
        headers=auth(),
    ).get_json()
    after = client.get(f"/api/v2/actions/training/plans/{plan_id}/detail", headers=auth()).get_json()
    conn = connections.get_training_db()
    created = conn.execute("SELECT label, movement_key FROM movement_library WHERE movement_key='neue_uebung'").fetchone()
    conn.close()
    assert body["ok"] is True
    assert len(after["days"][0]["workouts"][0]["exercises"][0]["sets"]) == 1
    assert after["days"][0]["workouts"][0]["exercises"][0]["sets"][0]["target_reps"] == "8-10"
    assert tuple(created) == ("Neue Übung", "neue_uebung")


def test_training_patch_periodization_and_overwrite_plan(client):
    plan_id = _create_training_plan(client, "TP Overwrite", False, [{"day": "Mo", "events": [{"kind": "gym", "title": "Push", "items": [{"kind": "exercise", "name": "Bench", "sets": 1, "reps": {"min": 6, "max": 8}, "rpe_list": [8]}]}]}])
    body = client.post(
        f"/api/v2/actions/training/plans/{plan_id}/patch",
        json={"dry_run": False, "operations": [
            {"op": "update_periodization", "scope": {"plan_id": plan_id}, "periodization": {"phase": "accumulation", "week": 2, "deload": False}},
            {"op": "overwrite_plan", "days": [{"day_id": "Mo", "day_index": 0, "label": "Mo", "workouts": [{"workout_index": 0, "title": "Push A", "notes": "Heute defensiv", "exercises": [{"exercise_id": "x1", "canonical_id": "incline_db_press", "name": "Incline DB Press", "variation": "30 Grad", "sets": [{"set_index": 0, "target_reps": "8-10", "target_rpe": 8}], "notes": "test"}]}]}]},
        ]},
        headers=auth(),
    ).get_json()
    after = client.get(f"/api/v2/actions/training/plans/{plan_id}/detail", headers=auth()).get_json()
    assert body["ok"] is True
    assert after["periodization"]["phase"] == "accumulation"
    assert after["days"][0]["workouts"][0]["title"] == "Push A"
    assert after["days"][0]["workouts"][0]["notes"] == "Heute defensiv"
    assert after["days"][0]["workouts"][0]["exercises"][0]["name"] == "Incline DB Press"


def test_training_patch_rename_day_workout_note_move_replace_and_progression(client):
    plan_id = _create_training_plan(
        client,
        "TP Rich Ops",
        False,
        [{"day": "Mo", "events": [{"kind": "gym", "title": "Push", "items": [
            {"kind": "exercise", "name": "Bench", "variation": "Flat", "sets": 2, "reps": {"min": 6, "max": 8}, "rpe_list": [8, 8]},
            {"kind": "exercise", "name": "Flys", "variation": "SZ", "sets": 1, "reps": {"min": 10, "max": 12}, "rpe_list": [8]},
        ]}]}],
    )
    detail = client.get(f"/api/v2/actions/training/plans/{plan_id}/detail", headers=auth()).get_json()
    exercise_path = detail["days"][0]["workouts"][0]["exercises"][0]["exercise_path"]
    second_path = detail["days"][0]["workouts"][0]["exercises"][1]["exercise_path"]
    body = client.post(
        f"/api/v2/actions/training/plans/{plan_id}/patch",
        json={"dry_run": False, "operations": [
            {"op": "rename_day", "match": {"day_index": 0}, "title": "Montag Push"},
            {"op": "rename_workout", "match": {"day_index": 0, "workout_index": 0}, "title": "Push A"},
            {"op": "set_workout_note", "match": {"day_index": 0, "workout_index": 0}, "note": "Heute fokussiert"},
            {"op": "move_exercise", "match": {"exercise_path": second_path}, "target": {"day_index": 0, "workout_index": 0, "exercise_index": 0}},
            {"op": "replace_exercise", "match": {"exercise_path": exercise_path}, "replacement": {"name": "Incline Bench", "canonical_id": "incline_bench", "variation": "30 Grad"}},
            {"op": "update_progression_rule", "match": {"day_index": 0, "workout_index": 0, "exercise_index": 0}, "progression_rule": {"mode": "double_progression"}},
        ]},
        headers=auth(),
    ).get_json()
    after = client.get(f"/api/v2/actions/training/plans/{plan_id}/detail", headers=auth()).get_json()
    assert body["ok"] is True
    assert after["days"][0]["title"] == "Montag Push"
    assert after["days"][0]["workouts"][0]["title"] == "Push A"
    assert after["days"][0]["workouts"][0]["notes"] == "Heute fokussiert"
    assert after["days"][0]["workouts"][0]["exercises"][0]["name"] == "Incline Bench"
    assert after["days"][0]["workouts"][0]["exercises"][0]["variation"] == "30 Grad"
    assert after["days"][0]["workouts"][0]["exercises"][0]["progression_rule"]["mode"] == "double_progression"
    assert after["days"][0]["workouts"][0]["exercises"][1]["name"] == "Bench"


def test_training_patch_update_exercise_library_and_delete_exercise(client):
    plan_id = _create_training_plan(client, "TP Exercise CRUD", False, [{"day": "Mo", "events": [{"kind": "gym", "title": "Push", "items": [{"kind": "exercise", "name": "Bench", "sets": 1, "reps": {"min": 6, "max": 8}, "rpe_list": [8]}]}]}])
    create_resp = client.post(
        f"/api/v2/actions/training/plans/{plan_id}/patch",
        json={"dry_run": False, "operations": [{"op": "create_exercise", "exercise": {"name": "Neue Übung 2", "canonical_id": "neue_uebung_2", "muscle_group": "back", "equipment": "cable", "notes": "start"}}]},
        headers=auth(),
    ).get_json()
    search = client.get("/api/v2/actions/training/exercises/search?query=Neue%20%C3%9Cbung%202", headers=auth()).get_json()
    exercise_id = next(item["exercise_id"] for item in search["exercises"] if item["canonical_id"] == "neue_uebung_2")
    update_resp = client.post(
        f"/api/v2/actions/training/plans/{plan_id}/patch",
        json={"dry_run": False, "operations": [{"op": "update_exercise", "exercise_id": exercise_id, "patch": {"name": "Neue Übung 2B", "canonical_id": "neue_uebung_2b", "muscle_group": "chest", "equipment": "machine", "notes": "updated"}}]},
        headers=auth(),
    ).get_json()
    detail_resp = client.get(f"/api/v2/actions/training/exercises/{exercise_id}/detail", headers=auth()).get_json()
    plan_detail = client.get(f"/api/v2/actions/training/plans/{plan_id}/detail", headers=auth()).get_json()
    exercise_path = plan_detail["days"][0]["workouts"][0]["exercises"][0]["exercise_path"]
    delete_resp = client.post(
        f"/api/v2/actions/training/plans/{plan_id}/patch",
        json={"dry_run": False, "operations": [{"op": "delete_exercise", "match": {"exercise_path": exercise_path}}]},
        headers=auth(),
    ).get_json()
    after = client.get(f"/api/v2/actions/training/plans/{plan_id}/detail", headers=auth()).get_json()
    assert create_resp["ok"] is True
    assert update_resp["ok"] is True
    assert detail_resp["name"] == "Neue Übung 2B"
    assert detail_resp["canonical_id"] == "neue_uebung_2b"
    assert detail_resp["muscle_group"] == "chest"
    assert detail_resp["equipment"] == "machine"
    assert detail_resp["notes"] == "updated"
    assert delete_resp["ok"] is True
    assert after["days"][0]["workouts"][0]["exercises"] == []
