from pathlib import Path

from ai.actions_v2 import _normalize_create_training_plan_payload, _training_plan_summary_from_plan_json


def test_actions_v2_create_supports_rolling_sequence():
    normalized = _normalize_create_training_plan_payload(
        {
            "name": "Rolling Plan",
            "mode": "rolling_sequence",
            "periodization_enabled": False,
            "sequence": [
                {"kind": "gym", "title": "FB", "items": [{"kind": "exercise", "name": "Bench"}]},
                {"kind": "cardio", "mode": "bike", "title": "Zone 2", "items": [{"kind": "cardio", "duration_min": 30, "intensity": "Z2"}]},
            ],
        }
    )
    assert normalized["plan_json"]["meta"]["mode"] == "rolling_sequence"
    assert normalized["plan_json"]["meta"]["periodization_enabled"] is False
    assert normalized["plan_json"]["meta"]["rolling_week_pattern"]["Sa"] == "optional"
    assert normalized["sequence"][1]["mode"] == "bike"
    assert normalized["days"] == []


def test_actions_v2_create_promotes_rotation_day_block_to_rolling_sequence():
    normalized = _normalize_create_training_plan_payload(
        {
            "name": "Bootcamp Rolling",
            "mode": "rolling",
            "weekly_reset": False,
            "gym_exposures_before_run_recovery_day": 6,
            "value": {
                "days": {
                    "rotation": {
                        "events": [
                            {"kind": "gym", "title": "Push A", "items": [{"kind": "exercise", "name": "Bench"}]},
                            {"kind": "gym", "title": "Pull A", "items": [{"kind": "exercise", "name": "Rows"}]},
                            {"kind": "gym", "title": "Push B", "items": [{"kind": "exercise", "name": "OHP"}]},
                            {"kind": "gym", "title": "Pull B", "items": [{"kind": "exercise", "name": "PullUps"}]},
                        ]
                    },
                    "Di": [{"kind": "cardio", "mode": "run", "title": "Easy Run", "items": [{"kind": "run", "duration_min": 30}]}],
                }
            },
        }
    )
    assert normalized["plan_json"]["meta"]["mode"] == "rolling_sequence"
    assert normalized["plan_json"]["meta"]["weekly_reset"] is False
    assert normalized["plan_json"]["meta"]["gym_exposures_before_run_recovery_day"] == 6
    assert [event["title"] for event in normalized["sequence"]] == ["Push A", "Pull A", "Push B", "Pull B"]
    assert "Di" in normalized["base_week"]
    assert "rotation" not in normalized["base_week"]
    assert normalized["sequence_count"] == 4


def test_training_plan_summary_counts_rotation_strength_sessions():
    normalized = _normalize_create_training_plan_payload(
        {
            "name": "Bootcamp Rolling",
            "mode": "rolling",
            "value": {
                "days": {
                    "rotation": {
                        "events": [
                            {"kind": "gym", "title": "Push A", "exercises": [{"name": "Bench", "sets": 3, "rep_range": {"min": 6, "max": 10}, "rpe_list": [8, 9, 9]}]},
                            {"kind": "gym", "title": "Pull A", "exercises": [{"name": "Rows", "sets": 2, "rep_range": {"min": 8, "max": 10}, "rpe_list": [8, 9]}]},
                            {"kind": "gym", "title": "Push B", "exercises": [{"name": "OHP", "sets": 2, "rep_range": {"min": 6, "max": 10}, "rpe_list": [8, 9]}]},
                            {"kind": "gym", "title": "Pull B", "exercises": [{"name": "PullUps", "sets": 2, "rep_range": {"min": 5, "max": 8}, "rpe_list": [8, 9]}]},
                        ]
                    },
                    "Mo": [{"kind": "cardio", "mode": "run", "title": "Run A", "items": [{"kind": "run", "duration_min": 30}]}],
                    "Mi": [{"kind": "cardio", "mode": "run", "title": "Run B", "items": [{"kind": "run", "duration_min": 40}]}],
                    "So": [{"kind": "cardio", "mode": "run", "title": "Run C", "items": [{"kind": "run", "duration_min": 60}]}],
                }
            },
        }
    )
    summary = _training_plan_summary_from_plan_json(normalized["plan_json"])
    assert summary["strength_days"] == 4
    assert summary["run_days"] == 3


def test_planning_endpoints_do_not_import_recovery_logic():
    for rel_path in ("ai/plan_management.py", "ai/api_ai_plans.py", "plans/plans_api.py"):
        text = Path(rel_path).read_text(encoding="utf-8")
        lowered = text.lower()
        assert "hrv" not in lowered
        assert "readiness" not in lowered
