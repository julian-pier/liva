"""
Tests for create_training_plan payload handling from Custom GPT.
Tests ensure that multiple payload shapes are supported:
- value.days
- plan.days
- plan.base_week
And that proper error messages are returned when data is missing.
"""
import json
from tests.test_actions_v2_api import auth, client


# Minimal training plan structure for testing
MINIMAL_DAY = {"day": "Mo", "events": [{"kind": "gym", "title": "Test", "items": [{"kind": "exercise", "name": "Test", "sets": 3, "reps": 10}]}]}
MINIMAL_BASE_WEEK = {"Mo": [{"kind": "gym", "title": "Test", "items": [{"kind": "exercise", "name": "Test", "sets": 3, "reps": 10}]}]}


def test_liva_create_training_plan_with_value_days(client):
    """Test that create_training_plan accepts value.days payload structure."""
    resp = client.post(
        "/api/v2/actions/liva/act",
        json={
            "domain": "training",
            "command": "create_training_plan",
            "dry_run": True,
            "confirm": False,
            "name": "Torso/Limbs x5 – Chest/Lats Fokus",
            "value": {
                "days": [MINIMAL_DAY]
            }
        },
        headers=auth(),
    )
    body = resp.get_json()
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {body}"
    assert body["ok"] is True
    assert body["execution"]["mode"] == "dry_run"
    assert body["result"]["plan_name"] == "Torso/Limbs x5 – Chest/Lats Fokus"
    assert body["result"]["days_count"] >= 1


def test_liva_create_training_plan_with_plan_days(client):
    """Test that create_training_plan accepts plan.days payload structure."""
    resp = client.post(
        "/api/v2/actions/liva/act",
        json={
            "domain": "training",
            "command": "create_training_plan",
            "dry_run": True,
            "confirm": False,
            "plan": {
                "name": "Torso/Limbs x5 – Chest/Lats Fokus",
                "days": [MINIMAL_DAY]
            }
        },
        headers=auth(),
    )
    body = resp.get_json()
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {body}"
    assert body["ok"] is True
    assert body["execution"]["mode"] == "dry_run"
    assert body["result"]["plan_name"] == "Torso/Limbs x5 – Chest/Lats Fokus"
    assert body["result"]["days_count"] >= 1


def test_liva_create_training_plan_with_payload_days(client):
    resp = client.post(
        "/api/v2/actions/liva/act",
        json={
            "domain": "training",
            "command": "create_training_plan",
            "dry_run": True,
            "confirm": False,
            "payload": {
                "name": "Payload Days Plan",
                "days": [MINIMAL_DAY],
            },
        },
        headers=auth(),
    )
    body = resp.get_json()
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {body}"
    assert body["ok"] is True
    assert body["result"]["plan_name"] == "Payload Days Plan"
    assert body["result"]["days_count"] >= 1


def test_liva_create_training_plan_with_payload_value_days(client):
    resp = client.post(
        "/api/v2/actions/liva/act",
        json={
            "domain": "training",
            "command": "create_training_plan",
            "dry_run": True,
            "confirm": False,
            "payload": {
                "name": "Payload Value Days Plan",
                "value": {"days": [MINIMAL_DAY]},
            },
        },
        headers=auth(),
    )
    body = resp.get_json()
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {body}"
    assert body["ok"] is True
    assert body["result"]["plan_name"] == "Payload Value Days Plan"
    assert body["result"]["days_count"] >= 1


def test_liva_create_training_plan_with_payload_plan_days(client):
    resp = client.post(
        "/api/v2/actions/liva/act",
        json={
            "domain": "training",
            "command": "create_training_plan",
            "dry_run": True,
            "confirm": False,
            "payload": {
                "plan": {
                    "name": "Payload Plan Days Plan",
                    "days": [MINIMAL_DAY],
                }
            },
        },
        headers=auth(),
    )
    body = resp.get_json()
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {body}"
    assert body["ok"] is True
    assert body["result"]["plan_name"] == "Payload Plan Days Plan"
    assert body["result"]["days_count"] >= 1


def test_liva_create_training_plan_with_value_days_object_map(client):
    resp = client.post(
        "/api/v2/actions/liva/act",
        json={
            "domain": "training",
            "command": "create_training_plan",
            "dry_run": True,
            "confirm": False,
            "name": "Object Map Days Plan",
            "value": {
                "days": {
                    "Mo": MINIMAL_DAY["events"],
                    "Di": {
                        "events": [
                            {"kind": "gym", "title": "Pull", "items": [{"kind": "exercise", "name": "Rows", "sets": 3, "reps": 10}]}
                        ]
                    },
                }
            },
        },
        headers=auth(),
    )
    body = resp.get_json()
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {body}"
    assert body["ok"] is True
    assert body["result"]["plan_name"] == "Object Map Days Plan"
    assert body["result"]["days_count"] == 2


def test_liva_create_training_plan_with_payload_value_days_object_map(client):
    resp = client.post(
        "/api/v2/actions/liva/act",
        json={
            "domain": "training",
            "command": "create_training_plan",
            "dry_run": True,
            "confirm": False,
            "payload": {
                "name": "Payload Object Map Days Plan",
                "value": {
                    "days": {
                        "Mo": MINIMAL_DAY["events"],
                    }
                },
            },
        },
        headers=auth(),
    )
    body = resp.get_json()
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {body}"
    assert body["ok"] is True
    assert body["result"]["plan_name"] == "Payload Object Map Days Plan"
    assert body["result"]["days_count"] == 1


def test_liva_create_training_plan_with_plan_base_week(client):
    """Test that create_training_plan accepts plan.base_week payload structure."""
    resp = client.post(
        "/api/v2/actions/liva/act",
        json={
            "domain": "training",
            "command": "create_training_plan",
            "dry_run": True,
            "confirm": False,
            "plan": {
                "name": "Torso/Limbs x5 – Chest/Lats Fokus",
                "base_week": MINIMAL_BASE_WEEK
            }
        },
        headers=auth(),
    )
    body = resp.get_json()
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {body}"
    assert body["ok"] is True
    assert body["execution"]["mode"] == "dry_run"
    assert body["result"]["plan_name"] == "Torso/Limbs x5 – Chest/Lats Fokus"
    assert body["result"]["days_count"] >= 1


def test_liva_create_training_plan_missing_days_error(client):
    """Test that proper error is returned when no days/base_week data is provided."""
    resp = client.post(
        "/api/v2/actions/liva/act",
        json={
            "domain": "training",
            "command": "create_training_plan",
            "dry_run": True,
            "confirm": False,
            "name": "Empty Plan"
        },
        headers=auth(),
    )
    body = resp.get_json()
    assert resp.status_code == 400, f"Expected 400, got {resp.status_code}: {body}"
    assert body["ok"] is False
    # Should have clear error message about missing days/base_week
    error_msg = body["error"]["message"].lower()
    assert "days" in error_msg or "base_week" in error_msg


def test_liva_create_training_plan_confirm_required_for_live(client):
    """Test that live write requires confirm=true."""
    resp = client.post(
        "/api/v2/actions/liva/act",
        json={
            "domain": "training",
            "command": "create_training_plan",
            "dry_run": False,
            "confirm": False,
            "name": "Live Plan",
            "value": {
                "days": [MINIMAL_DAY]
            }
        },
        headers=auth(),
    )
    body = resp.get_json()
    assert resp.status_code == 400
    assert body["ok"] is False
    assert body["error"]["code"] == "confirmation_required"


def test_liva_create_training_plan_name_resolution_priority(client):
    """Test that name is resolved with correct priority: plan.name > value.name > plan_name > name."""
    resp = client.post(
        "/api/v2/actions/liva/act",
        json={
            "domain": "training",
            "command": "create_training_plan",
            "dry_run": True,
            "confirm": False,
            "name": "top_level_name",
            "plan_name": "plan_name_value",
            "value": {
                "name": "value_name",
                "days": [MINIMAL_DAY]
            },
            "plan": {
                "name": "plan_name_preferred",
                "days": [MINIMAL_DAY]
            }
        },
        headers=auth(),
    )
    body = resp.get_json()
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {body}"
    # plan.name should be preferred
    assert body["result"]["plan_name"] == "plan_name_preferred"


def test_liva_create_training_plan_set_active_default_false(client):
    """Test that set_active defaults to false when not specified."""
    before = client.get("/api/v2/actions/training/plan-state?detail=full", headers=auth()).get_json()["plan"]["active_version"]
    resp = client.post(
        "/api/v2/actions/liva/act",
        json={
            "domain": "training",
            "command": "create_training_plan",
            "dry_run": True,
            "confirm": False,
            "name": "Inactive Plan",
            "value": {
                "days": [MINIMAL_DAY]
            }
        },
        headers=auth(),
    )
    body = resp.get_json()
    assert resp.status_code == 200
    # should have set_active=false in result
    assert body["result"]["set_active"] is False


def test_liva_create_training_plan_dry_run_no_write(client):
    """Test that dry_run never writes to DB."""
    before = client.get("/api/v2/actions/training/plan-state?detail=full", headers=auth()).get_json()["plan"]["active_version"]
    resp = client.post(
        "/api/v2/actions/liva/act",
        json={
            "domain": "training",
            "command": "create_training_plan",
            "dry_run": True,
            "confirm": False,
            "name": "Test Dry Run",
            "value": {
                "days": [
                    {
                        "day": "Mo",
                        "events": [
                            {
                                "kind": "gym",
                                "title": "Strength",
                                "items": [
                                    {"kind": "exercise", "name": "Squat", "sets": 4, "reps": 6}
                                ]
                            }
                        ]
                    },
                    {
                        "day": "We",
                        "events": [
                            {
                                "kind": "cardio",
                                "mode": "run",
                                "items": [
                                    {"kind": "run", "distance_km": 5, "duration_min": 30}
                                ]
                            }
                        ]
                    }
                ]
            }
        },
        headers=auth(),
    )
    body = resp.get_json()
    after = client.get("/api/v2/actions/training/plan-state?detail=full", headers=auth()).get_json()["plan"]["active_version"]
    
    assert resp.status_code == 200
    assert body["execution"]["mode"] == "dry_run"
    assert body["execution"]["live_state_changed"] is False
    assert before == after, "dry_run should not change database state"
    # Should contain plan preview
    assert body["result"]["plan_name"] == "Test Dry Run"
    assert body["result"]["days_count"] == 2


def test_liva_create_training_plan_error_invalid_plan_payload(client):
    """Test that invalid plan structure returns proper error."""
    resp = client.post(
        "/api/v2/actions/liva/act",
        json={
            "domain": "training",
            "command": "create_training_plan",
            "dry_run": True,
            "confirm": False,
            "name": "Bad Plan",
            "plan": {
                "days": "not_a_list"  # Should be a list
            }
        },
        headers=auth(),
    )
    body = resp.get_json()
    assert resp.status_code == 400
    assert body["ok"] is False


def test_liva_create_training_plan_multiple_days_structure(client):
    """Test with multiple days to ensure proper aggregation."""
    resp = client.post(
        "/api/v2/actions/liva/act",
        json={
            "domain": "training",
            "command": "create_training_plan",
            "dry_run": True,
            "confirm": False,
            "plan": {
                "name": "5-Day Split",
                "days": [
                    {
                        "day": "Monday",
                        "events": [
                            {
                                "kind": "gym",
                                "title": "Chest",
                                "items": [
                                    {"kind": "exercise", "name": "Bench Press", "sets": 4, "reps": 8}
                                ]
                            }
                        ]
                    },
                    {
                        "day": "Tuesday",
                        "events": [
                            {
                                "kind": "gym",
                                "title": "Back",
                                "items": [
                                    {"kind": "exercise", "name": "Deadlift", "sets": 3, "reps": 5}
                                ]
                            }
                        ]
                    },
                    {
                        "day": "Wednesday",
                        "events": [
                            {
                                "kind": "cardio",
                                "mode": "run",
                                "items": [
                                    {"kind": "run", "distance_km": 10, "duration_min": 60}
                                ]
                            }
                        ]
                    },
                    {
                        "day": "Thursday",
                        "events": [
                            {
                                "kind": "gym",
                                "title": "Legs",
                                "items": [
                                    {"kind": "exercise", "name": "Squat", "sets": 4, "reps": 6}
                                ]
                            }
                        ]
                    },
                    {
                        "day": "Friday",
                        "events": [
                            {
                                "kind": "gym",
                                "title": "Arms",
                                "items": [
                                    {"kind": "exercise", "name": "Curl", "sets": 3, "reps": 10}
                                ]
                            }
                        ]
                    },
                ]
            }
        },
        headers=auth(),
    )
    body = resp.get_json()
    assert resp.status_code == 200
    assert body["ok"] is True
    assert body["result"]["days_count"] == 5
    assert body["result"]["strength_days"] == 4
    assert body["result"]["run_days"] == 1
    assert body["result"]["cardio_days"] == 1
