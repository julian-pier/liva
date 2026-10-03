import pytest
from jsonschema import Draft202012Validator, ValidationError

from liva_mcp.tool_registry import COACH_ACT_SCHEMA, COACH_READ_SCHEMA


def test_endurance_read_accepts_canonical_string_plan_id():
    Draft202012Validator(COACH_READ_SCHEMA).validate(
        {"mode": "endurance_plan", "payload": {"plan_id": "ep_123456"}}
    )


def test_endurance_read_accepts_external_string_session_id():
    Draft202012Validator(COACH_READ_SCHEMA).validate(
        {
            "mode": "endurance_session",
            "payload": {
                "plan_id": "ep_123456",
                "session_id": "intervals-date:2026-09-05",
            },
        }
    )


def test_endurance_patch_schema_publishes_anchor_and_target_shapes():
    request = {
        "domain": "endurance",
        "command": "patch_endurance_plan",
        "dry_run": True,
        "payload": {
            "plan_id": "ep_123456",
            "operations": [
                {
                    "op": "set_fitness_anchor",
                    "anchor": {
                        "date": "2026-12-12",
                        "source": "5k_benchmark",
                        "five_k_time_s": 1275,
                        "threshold_pace_s_per_km": 260,
                    },
                }
            ],
        },
    }
    Draft202012Validator(COACH_ACT_SCHEMA).validate(request)
    request["payload"]["operations"] = [{"op": "clear_fitness_anchor"}]
    Draft202012Validator(COACH_ACT_SCHEMA).validate(request)
    request["payload"]["operations"] = [
        {
            "op": "set_session_target",
            "step_id": "step-1",
            "target": {"metric": "pace", "basis": "absolute", "pace_s_per_km": 260},
        }
    ]
    Draft202012Validator(COACH_ACT_SCHEMA).validate(request)


def test_endurance_patch_contract_rejects_unknown_top_level_payload_fields():
    valid = {
        "domain": "endurance",
        "command": "patch_endurance_plan",
        "dry_run": True,
        "payload": {
            "plan_id": "ep_123456",
            "operations": [{
                "op": "add_session",
                "session": {
                    "date": "2027-01-01",
                    "sport_type": "intervals",
                    "title": "4 × 4 min",
                    "duration_min": 40,
                },
            }],
        },
    }
    Draft202012Validator(COACH_ACT_SCHEMA).validate(valid)
    invalid = {**valid, "payload": {**valid["payload"], "silently_ignored": True}}
    with pytest.raises(ValidationError):
        Draft202012Validator(COACH_ACT_SCHEMA).validate(invalid)


def test_endurance_patch_contract_rejects_unknown_session_and_step_fields():
    request = {
        "domain": "endurance",
        "command": "patch_endurance_plan",
        "dry_run": True,
        "payload": {
            "plan_id": "ep_123456",
            "operations": [{
                "op": "add_session",
                "session": {
                    "date": "2027-01-01",
                    "sport_type": "intervals",
                    "title": "4 × 4 min",
                    "steps": [{"step_type": "work", "duration_min": 4, "recovery_s": 120}],
                },
            }],
        },
    }
    with pytest.raises(ValidationError):
        Draft202012Validator(COACH_ACT_SCHEMA).validate(request)

    request["payload"]["operations"][0]["session"].pop("steps")
    request["payload"]["operations"][0]["session"]["recovery_s"] = 120
    with pytest.raises(ValidationError):
        Draft202012Validator(COACH_ACT_SCHEMA).validate(request)
