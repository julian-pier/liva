from __future__ import annotations

import json

import database.connections as connections
from tests.test_actions_v2_api import auth, client  # noqa: F401
from tests.test_actions_v2_plan_lifecycle import _create_training_plan, _seed_rotation_mirror_plan


def test_training_plan_change_sets_and_read_back(client):
    before = client.post("/api/v2/actions/liva/read", json={"mode": "training_plan", "detail": "full"}, headers=auth()).get_json()
    assert before["result"]["plan"]["days"]["Mo"]["exercises"][0]["sets"] == 2

    changed = client.post(
        "/api/v2/actions/liva/act",
        json={"domain": "training", "command": "change_sets", "day": "Mo", "exercise": "Schrägbankdrücken", "sets": 3},
        headers=auth(),
    ).get_json()
    assert changed["ok"] is True
    assert changed["execution"]["mode"] == "applied_live"
    assert "plans.gym_plans" in changed["execution"]["affected_resources"]
    assert changed["result"]["old_target"]["sets"] == 2
    assert changed["result"]["new_target"]["sets"] == 3

    after = client.post("/api/v2/actions/liva/read", json={"mode": "training_plan", "detail": "full"}, headers=auth()).get_json()
    assert after["result"]["plan"]["days"]["Mo"]["exercises"][0]["sets"] == 3


def test_training_plan_change_rep_range_accepts_rep_range_alias_and_reads_back(client):
    before = client.post("/api/v2/actions/liva/read", json={"mode": "training_plan", "detail": "full"}, headers=auth()).get_json()
    assert before["result"]["plan"]["days"]["Mo"]["exercises"][0]["rep_range"] == {"min": 6, "max": 10}

    changed = client.post(
        "/api/v2/actions/liva/act",
        json={
            "domain": "training",
            "command": "change_rep_range",
            "day": "Mo",
            "exercise": "Schrägbankdrücken",
            "rep_range": {"min": 5, "max": 8},
        },
        headers=auth(),
    ).get_json()
    assert changed["ok"] is True
    assert changed["execution"]["mode"] == "applied_live"
    assert changed["result"]["old_target"]["rep_range"] == {"min": 6, "max": 10}
    assert changed["result"]["new_target"]["rep_range"] == {"min": 5, "max": 8}

    after = client.post("/api/v2/actions/liva/read", json={"mode": "training_plan", "detail": "full"}, headers=auth()).get_json()
    assert after["result"]["plan"]["days"]["Mo"]["exercises"][0]["rep_range"] == {"min": 5, "max": 8}


def test_training_plan_replace_exercise_and_change_rpe_read_back(client):
    replaced = client.post(
        "/api/v2/actions/liva/act",
        json={
            "domain": "training",
            "command": "replace_exercise",
            "day": "Mo",
            "exercise": "Latzug",
            "new_exercise": {"name": "Klimmzüge", "variation": "assistiert", "sets": 3, "reps": {"min": 6, "max": 10}, "rpe_list": [8, 8, 9]},
        },
        headers=auth(),
    ).get_json()
    assert replaced["ok"] is True
    assert replaced["result"]["matched_exercise"] == "Latzug"
    assert replaced["result"]["new_target"]["name"] == "Klimmzüge"

    rpe = client.post(
        "/api/v2/actions/liva/act",
        json={"domain": "training", "command": "change_rpe", "day": "Mo", "exercise": "Schrägbankdrücken", "rpe": [8, 8, 9]},
        headers=auth(),
    ).get_json()
    assert rpe["ok"] is True
    assert rpe["result"]["new_target"]["rpe_list"] == [8, 8]

    after = client.post("/api/v2/actions/liva/read", json={"mode": "training_plan", "detail": "full"}, headers=auth()).get_json()["result"]["plan"]
    names = [item["name"] for item in after["days"]["Mo"]["exercises"]]
    assert "Klimmzüge" in names
    incline = next(item for item in after["days"]["Mo"]["exercises"] if item["name"] == "Schrägbankdrücken")
    assert incline["rpe_list"] == [8, 8]


def test_training_plan_change_variation_keeps_rolling_sequence_in_sync(client):
    plan_id = _create_training_plan(
        client,
        "Rolling Sync Variation",
        True,
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
                                "variation": "LH",
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
    _seed_rotation_mirror_plan(conn, plan_id)
    conn.close()

    changed = client.post(
        "/api/v2/actions/liva/act",
        json={"domain": "training", "command": "change_variation", "day": "Mi", "exercise": "OHP (LH)", "new_variation": "eGym"},
        headers=auth(),
    ).get_json()
    assert changed["ok"] is True
    assert changed["execution"]["mode"] == "applied_live"

    conn = connections.get_plans_db()
    conn.row_factory = __import__("sqlite3").Row
    row = conn.execute("SELECT plan_json FROM gym_plans WHERE id=?", (plan_id,)).fetchone()
    conn.close()
    plan_json = json.loads(row["plan_json"])
    assert plan_json["base_week"]["Mi"][0]["items"][0]["variation"] == "eGym"
    assert plan_json["sequence"][0]["items"][0]["variation"] == "eGym"


def test_training_plan_invalid_day_or_exercise_returns_400_and_keeps_plan(client):
    before = client.post("/api/v2/actions/liva/read", json={"mode": "training_plan", "detail": "full"}, headers=auth()).get_json()["result"]["plan"]
    bad = client.post(
        "/api/v2/actions/liva/act",
        json={"domain": "training", "command": "change_sets", "day": "So", "exercise": "Schrägbankdrücken", "sets": 3},
        headers=auth(),
    )
    assert bad.status_code == 400

    after = client.post("/api/v2/actions/liva/read", json={"mode": "training_plan", "detail": "full"}, headers=auth()).get_json()["result"]["plan"]
    assert after == before
