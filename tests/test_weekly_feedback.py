from __future__ import annotations

from datetime import date, timedelta

import pytest

import app as appmod
from ai import actions_v2


def _week_rows():
    start = date(2026, 9, 14)
    nutrition = []
    recovery = []
    training = []
    for index in range(14):
        day = (start + timedelta(days=index)).isoformat()
        is_current = index >= 7
        nutrition.append({
            "date_iso": day,
            "bodyweight_kg": 73.0 + index * 0.05,
            "calories": None if day == "2026-09-24" else 2500 + (index % 3) * 100,
            "protein_g": 180 + index % 4,
            "carbs_g": 300,
            "fat_g": 70,
            "target_kcal": 2600,
            "mode": "cut",
            "source": "nutrition_plan",
        })
        recovery.append({"date": day, "rmssd": 90, "actualSleepMinutes": 450, "flag": "alcohol" if day == "2026-09-27" else None})
        if index in {0, 2, 7, 9}:
            weight = 105 if is_current else 100
            training.append({"id": index, "date": day, "day_type": "Pull", "exercises": [{"name": "HighRows", "display_name": "HighRows (unilat)", "sets": [{"set_number": 1, "weight": weight, "reps": 8, "rpe": 8}]}]})
    return {"nutrition": nutrition, "training": training, "recovery": recovery, "training_truncated": False}


def test_completed_week_is_monday_to_sunday_and_skips_current_week(monkeypatch):
    monkeypatch.setattr(actions_v2, "_today", lambda: "2026-09-30")
    start, end = actions_v2._completed_week_range({"date": "2026-09-28"})
    assert (start.isoformat(), end.isoformat()) == ("2026-09-21", "2026-09-27")
    with pytest.raises(ValueError, match="fully completed"):
        actions_v2._completed_week_range({"week_start": "2026-09-28"})


def test_weekly_feedback_is_compact_complete_and_keeps_missing_days(monkeypatch):
    monkeypatch.setattr(actions_v2, "_today", lambda: "2026-09-30")
    monkeypatch.setattr(actions_v2, "_analysis_phase_source_rows", lambda *_args: _week_rows())
    monkeypatch.setattr(actions_v2, "_weekly_plan_adherence", lambda *_args: {"adherence_pct": 75.0, "planned_days_recorded": 4, "completed_planned_days": ["2026-09-21", "2026-09-23", "2026-09-25"], "missed_planned_days": ["2026-09-26"], "actual_training_days": ["2026-09-21", "2026-09-23"], "coverage": "historical_saved_decisions"})
    monkeypatch.setattr(appmod, "_analysis_phase_payload", lambda *_args, **_kwargs: {"selected": {"label": "Mini-Cut", "modes": ["cut"], "sources": ["nutrition_plan"], "target_min": 2600, "target_max": 2600, "context_ambiguous": False}})

    payload = actions_v2._weekly_feedback_payload({"week_start": "2026-09-21"})

    assert payload["week"] == {"start": "2026-09-21", "end": "2026-09-27", "calendar_week": 39, "complete": True, "includes_sunday": True}
    assert payload["progression"]["rate_pct"] == 100.0
    assert payload["progression"]["improved"][0]["exercise"] == "HighRows (unilat)"
    assert payload["plan_adherence"]["adherence_pct"] == 75.0
    assert payload["nutrition"]["logged_days"] == 6
    assert payload["nutrition"]["missing_days"] == ["2026-09-24"]
    assert payload["recovery"]["flags"] == [{"date": "2026-09-27", "flag": "alcohol"}]
    assert payload["historical_target_context"]["label"] == "Mini-Cut"
    assert payload["payload_bytes"] < 32_000


def test_weekly_feedback_is_manual_and_only_auto_attached_on_monday(monkeypatch):
    monkeypatch.setattr(actions_v2, "_weekly_feedback_payload", lambda payload: {"week": payload})
    assert actions_v2._weekly_feedback_for_checkin("2026-09-28") == {"week": {"date": "2026-09-28"}}
    assert actions_v2._weekly_feedback_for_checkin("2026-09-29") is None

    result, status = actions_v2._dispatch_liva_read({"mode": "weekly_feedback", "payload": {"week_start": "2026-09-21"}})
    assert status == 200
    assert result["result"] == {"week": {"week_start": "2026-09-21"}}


def test_monday_checkin_response_contains_weekly_feedback(monkeypatch):
    monkeypatch.setattr(actions_v2, "_subjective_checkin_for_date", lambda day: {"available": True, "date": day})
    monkeypatch.setattr(actions_v2, "_weekly_feedback_for_checkin", lambda day: {"week": {"start": "2026-09-21", "end": "2026-09-27"}, "requested_on": day})

    with appmod.app.test_request_context("/api/v2/actions/checkin/today?date=2026-09-28"):
        response = actions_v2.checkin_today.__wrapped__()
    payload = response.get_json()

    assert payload["weekly_feedback"]["week"]["end"] == "2026-09-27"
    assert payload["data_status"]["weekly_feedback"] == "complete"
