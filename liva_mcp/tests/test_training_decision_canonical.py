from types import SimpleNamespace

import pytest

from liva_mcp.read_service import ReadService


def _value(**overrides):
    values = {
        "date": "2026-08-31",
        "subjective_status": None,
        "hrv_status_text": None,
        "recovery_note": None,
        "pain_notes": None,
        "time_available_min": None,
        "constraints": None,
        "force_session_key": None,
        "dry_run": True,
        "reason": "",
        "idempotency_key": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _service_with_context(context):
    service = ReadService.__new__(ReadService)
    service._canonical_training_context = lambda _date: context
    return service


def test_plan_and_write_dry_run_use_identical_canonical_session():
    context = {
        "date": "2026-08-31",
        "context_hash": "canonical-hash",
        "planned_session": {
            "available": True,
            "name": "Pull B",
            "session_key": "pull-b",
            "session_type": "gym",
            "exercises": [{"name": "RDLs", "sets": 2}],
        },
    }
    service = _service_with_context(context)
    planned = service.training_decision_plan(_value())
    write_preview = service.training_decision_write(_value())
    assert planned["selected_session"] == write_preview["selected_session"]
    assert planned["context_hash"] == write_preview["context_hash"] == "canonical-hash"
    assert planned["next_step"] == write_preview["next_step"] == "ready_for_training_decision_write"


def test_write_dry_run_hard_stops_when_canonical_date_is_rest():
    service = _service_with_context({"date": "2026-09-01", "context_hash": "rest-hash", "planned_session": {"available": False}})
    preview = service.training_decision_write(_value(date="2026-09-01"))
    assert preview["selected_session"] is None
    assert preview["is_rest_day"] is True
    assert preview["next_step"] == "do_not_write_training_decision"


def test_force_session_cannot_override_canonical_resolution():
    service = _service_with_context({
        "date": "2026-08-31",
        "context_hash": "canonical-hash",
        "planned_session": {"available": True, "name": "Pull B", "session_key": "pull-b", "session_type": "gym"},
    })
    with pytest.raises(ValueError, match="force_session_key_conflicts_with_canonical_session"):
        service.training_decision_write(_value(force_session_key="push-a"))
