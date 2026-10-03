from __future__ import annotations

import importlib
import os
from datetime import date as real_date, datetime as real_datetime, timedelta


def _load_app_module(monkeypatch):
    monkeypatch.setenv("LIVA_FLASK_SECRET", "test-secret")
    if "app" in os.sys.modules:
        return importlib.reload(os.sys.modules["app"])
    return importlib.import_module("app")


def _freeze_clock(monkeypatch, appmod, frozen_day: real_date):
    class FakeDate(real_date):
        @classmethod
        def today(cls):
            return frozen_day

    class FakeDateTime(real_datetime):
        @classmethod
        def now(cls, tz=None):
            base = real_datetime(frozen_day.year, frozen_day.month, frozen_day.day, 9, 0, 0)
            if tz is not None:
                return base.replace(tzinfo=tz)
            return base

    monkeypatch.setattr(appmod, "date", FakeDate)
    monkeypatch.setattr(appmod, "datetime", FakeDateTime)


def _base_plan() -> dict:
    return {
        "id": 1,
        "base_week": [
            {"strength_exercises": [{"exercise_name": "Bench"}], "session_name": "Push", "run_sessions": []},
            {"strength_exercises": [{"exercise_name": "Squat"}], "session_name": "Legs", "run_sessions": []},
            {"strength_exercises": [], "run_sessions": [{"run_type": "z2"}]},
            {"strength_exercises": [{"exercise_name": "Row"}], "session_name": "Pull", "run_sessions": []},
            {"strength_exercises": [{"exercise_name": "RDL"}], "session_name": "Lower", "run_sessions": []},
            {"strength_exercises": [{"exercise_name": "Press"}], "session_name": "Upper", "run_sessions": []},
            {"strength_exercises": [], "run_sessions": []},
        ],
    }


def _slot_by_day(week_final, day_iso: str) -> dict:
    return next((row for row in week_final if row.get("day_id") == day_iso), {})


def _mock_completion_maps(*_args, **_kwargs):
    return {}, {}


def _mock_schedule_all_open(*_args, **_kwargs):
    return {"training_today": True, "status": "scheduled", "window": {"training_start": "17:00", "training_end": "18:00"}}


def test_high_priority_skip_rolls_main_sessions(monkeypatch):
    appmod = _load_app_module(monkeypatch)
    monday = real_date(2026, 3, 2)
    _freeze_clock(monkeypatch, appmod, monday)

    monkeypatch.setattr(appmod, "_autopilot_load_core_override_choice_map", lambda *_args, **_kwargs: {monday.isoformat(): "rest"})
    monkeypatch.setattr(appmod, "_autopilot_load_actual_completion_maps", _mock_completion_maps)
    monkeypatch.setattr(appmod, "plan_training_window", _mock_schedule_all_open)

    week_final, meta = appmod._autopilot_build_rolling_shifted_week(_base_plan(), monday)

    assert _slot_by_day(week_final, monday.isoformat())["kind"] == "rest"
    assert _slot_by_day(week_final, (monday + timedelta(days=1)).isoformat())["session_name"] == "Push"
    assert _slot_by_day(week_final, (monday + timedelta(days=2)).isoformat())["session_name"] == "Legs"
    assert _slot_by_day(week_final, (monday + timedelta(days=3)).isoformat())["session_name"] == "Pull"
    assert _slot_by_day(week_final, (monday + timedelta(days=4)).isoformat())["session_name"] == "Lower"
    assert _slot_by_day(week_final, (monday + timedelta(days=5)).isoformat())["session_name"] == "Upper"
    assert any(item["session_name"] == "Push" and item["from_date"] == monday.isoformat() for item in meta["shift_events"])
    names = [row.get("session_name") for row in week_final if row.get("kind") == "plan"]
    assert names.count("Push") == 1


def test_drop_ok_run_is_dropped_without_bloating_week(monkeypatch):
    appmod = _load_app_module(monkeypatch)
    monday = real_date(2026, 3, 2)
    _freeze_clock(monkeypatch, appmod, monday)

    wed = (monday + timedelta(days=2)).isoformat()
    monkeypatch.setattr(appmod, "_autopilot_load_core_override_choice_map", lambda *_args, **_kwargs: {wed: "rest"})
    monkeypatch.setattr(appmod, "_autopilot_load_actual_completion_maps", _mock_completion_maps)
    monkeypatch.setattr(appmod, "plan_training_window", _mock_schedule_all_open)

    week_final, meta = appmod._autopilot_build_rolling_shifted_week(_base_plan(), monday)

    assert _slot_by_day(week_final, wed)["kind"] == "rest"
    assert _slot_by_day(week_final, (monday + timedelta(days=3)).isoformat())["session_name"] == "Pull"
    assert any(item["session_name"] == "Run Z2" and item["catchup_policy"] == appmod._CATCHUP_DROP for item in meta["dropped_sessions"])


def test_woche_boundary_carries_must_session_into_next_week(monkeypatch):
    appmod = _load_app_module(monkeypatch)
    friday = real_date(2026, 3, 6)
    _freeze_clock(monkeypatch, appmod, friday)

    monkeypatch.setattr(appmod, "_autopilot_load_core_override_choice_map", lambda *_args, **_kwargs: {friday.isoformat(): "rest"})
    monkeypatch.setattr(appmod, "_autopilot_load_actual_completion_maps", _mock_completion_maps)
    def _no_weekend_window(day_id, *args, **kwargs):
        if real_date.fromisoformat(day_id).weekday() >= 5:
            return {"status": "no_window_available"}
        return _mock_schedule_all_open()

    monkeypatch.setattr(appmod, "plan_training_window", _no_weekend_window)

    week_final, meta = appmod._autopilot_build_rolling_shifted_week(_base_plan(), friday)
    monday_after = (friday + timedelta(days=3)).isoformat()
    shifted_lower = next((row for row in week_final if row.get("session_name") == "Lower"), {})

    assert shifted_lower.get("day_id") and shifted_lower["day_id"] > monday_after
    assert any(item["session_name"] == "Lower" and item["to_date"] == shifted_lower["day_id"] for item in meta["shift_events"])


def test_calendar_conflict_pushes_to_next_sensible_day(monkeypatch):
    appmod = _load_app_module(monkeypatch)
    monday = real_date(2026, 3, 2)
    _freeze_clock(monkeypatch, appmod, monday)

    def fake_window(day_id, decision=None, **_kwargs):
        if day_id == monday.isoformat() and (decision or {}).get("kind") == "plan":
            return {"training_today": True, "status": "no_window_available"}
        return _mock_schedule_all_open()

    monkeypatch.setattr(appmod, "_autopilot_load_core_override_choice_map", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(appmod, "_autopilot_load_actual_completion_maps", _mock_completion_maps)
    monkeypatch.setattr(appmod, "plan_training_window", fake_window)

    week_final, _meta = appmod._autopilot_build_rolling_shifted_week(_base_plan(), monday)

    assert _slot_by_day(week_final, monday.isoformat())["kind"] == "rest"
    assert _slot_by_day(week_final, (monday + timedelta(days=1)).isoformat())["session_name"] == "Push"


def test_explicit_rest_override_wins_over_catchup(monkeypatch):
    appmod = _load_app_module(monkeypatch)
    monday = real_date(2026, 3, 2)
    _freeze_clock(monkeypatch, appmod, monday)

    tuesday = (monday + timedelta(days=1)).isoformat()
    overrides = {monday.isoformat(): "rest", tuesday: "rest"}
    monkeypatch.setattr(appmod, "_autopilot_load_core_override_choice_map", lambda *_args, **_kwargs: overrides)
    monkeypatch.setattr(appmod, "_autopilot_load_actual_completion_maps", _mock_completion_maps)
    monkeypatch.setattr(appmod, "plan_training_window", _mock_schedule_all_open)

    week_final, _meta = appmod._autopilot_build_rolling_shifted_week(_base_plan(), monday)

    assert _slot_by_day(week_final, tuesday)["kind"] == "rest"
    assert _slot_by_day(week_final, (monday + timedelta(days=2)).isoformat())["session_name"] == "Push"


def test_payload_exposes_shift_and_explain_fields(monkeypatch):
    appmod = _load_app_module(monkeypatch)
    monday = real_date(2026, 3, 2)
    _freeze_clock(monkeypatch, appmod, monday)

    plan = _base_plan()
    monkeypatch.setattr(appmod, "_get_active_plan_row", lambda: {"id": 1, "data": "{}"})
    monkeypatch.setattr(appmod, "plan_row_to_dict", lambda _row: plan)
    monkeypatch.setattr(appmod, "_autopilot_state_snapshot", lambda: {
        "flags": {"sick": False, "alcohol": False, "local_fatigue": {"legs": False, "push": False, "pull": False}, "hi_yesterday": False},
        "recovery": {"hrv_ratio": 1.0, "rhr_ratio": 1.0, "rmssd_today": 100.0, "rmssd_28d": 100.0, "rhr_today": 50.0, "rhr_28d": 50.0},
        "metrics": {"expected_7d": 5, "actual_7d": 5, "skipped_7d": 0, "override_count_7d": 0},
        "gym": {"hi_exposures_3d": 0},
        "learning_profile": {"learning_ready": False},
    })
    monkeypatch.setattr(appmod, "_autopilot_load_core_override_choice_map", lambda *_args, **_kwargs: {monday.isoformat(): "rest"})
    monkeypatch.setattr(appmod, "_autopilot_load_actual_completion_maps", _mock_completion_maps)
    monkeypatch.setattr(appmod, "plan_training_window", _mock_schedule_all_open)
    monkeypatch.setattr(appmod, "api_dashboard_plan_check_week_overview", lambda: appmod.jsonify({"ok": True, "week": {"week_type": "build"}, "layer": {"week_type": "build", "rpe_cap": 8.5}}))
    monkeypatch.setattr(appmod, "api_dashboard_plan_check_for_session", lambda: appmod.jsonify({
        "ok": True,
        "session": {"session_key": "d1-push", "session_name": "Push"},
        "reference_workout": None,
        "items": [],
        "summary": {"status": "green", "flags": []},
    }))

    with appmod.app.test_request_context(f"/api/next_session?day={monday.isoformat()}&explain=1"):
        payload, status = appmod._autopilot_plan_check_payload()

    assert status == 200
    assert payload["schedule_shift"]["shift_applied"] is True
    assert isinstance(payload["schedule_shift"]["shift_events"], list)
    explain = payload["autopilot_explain"]
    assert explain["shift_applied"] is True
    assert "shift_events" in explain
    assert "dropped_sessions" in explain
    assert payload["final_day"]["catchup_policy"] is None
    tomorrow = next((row for row in payload["week_days"] if row["day_id"] == (monday + timedelta(days=1)).isoformat()), {})
    assert tomorrow.get("catchup_policy") == appmod._CATCHUP_MUST
    assert tomorrow.get("meta", {}).get("shifted_from_date") == monday.isoformat()


def test_next_session_payload_uses_rolling_schedule(monkeypatch):
    appmod = _load_app_module(monkeypatch)
    monday = real_date(2026, 3, 2)
    tuesday = monday + timedelta(days=1)
    _freeze_clock(monkeypatch, appmod, monday)

    plan = _base_plan()
    monkeypatch.setattr(appmod, "_get_active_plan_row", lambda: {"id": 1, "data": "{}"})
    monkeypatch.setattr(appmod, "plan_row_to_dict", lambda _row: plan)
    monkeypatch.setattr(appmod, "_autopilot_state_snapshot", lambda: {
        "flags": {"sick": False, "alcohol": False, "local_fatigue": {"legs": False, "push": False, "pull": False}, "hi_yesterday": False},
        "recovery": {"hrv_ratio": 1.0, "rhr_ratio": 1.0, "rmssd_today": 100.0, "rmssd_28d": 100.0, "rhr_today": 50.0, "rhr_28d": 50.0},
        "metrics": {"expected_7d": 5, "actual_7d": 5, "skipped_7d": 0, "override_count_7d": 0},
        "gym": {"hi_exposures_3d": 0},
        "learning_profile": {"learning_ready": False},
    })
    monkeypatch.setattr(appmod, "_autopilot_load_core_override_choice_map", lambda *_args, **_kwargs: {monday.isoformat(): "rest"})
    monkeypatch.setattr(appmod, "_autopilot_load_actual_completion_maps", _mock_completion_maps)
    monkeypatch.setattr(appmod, "plan_training_window", _mock_schedule_all_open)
    monkeypatch.setattr(appmod, "api_dashboard_plan_check_week_overview", lambda: appmod.jsonify({"ok": True, "week": {"week_type": "build"}, "layer": {"week_type": "build", "rpe_cap": 8.5}}))
    monkeypatch.setattr(appmod, "api_dashboard_plan_check_for_session", lambda: appmod.jsonify({
        "ok": True,
        "session": {"session_key": "d1-push", "session_name": "Push"},
        "reference_workout": None,
        "items": [],
        "summary": {"status": "green", "flags": []},
    }))

    with appmod.app.test_request_context(f"/api/next_session?day={tuesday.isoformat()}"):
        response, status = appmod.api_next_session()
    assert status == 200
    payload = response.get_json()
    shifted_tuesday = next((row for row in payload["week_days"] if row["day_id"] == tuesday.isoformat()), {})

    assert shifted_tuesday.get("final_label") == "Push"
    assert shifted_tuesday.get("meta", {}).get("shifted_from_date") == monday.isoformat()
    assert payload["final_day"]["shifted_from_date"] == monday.isoformat()


def test_no_duplicate_sessions_after_skip_and_shift(monkeypatch):
    appmod = _load_app_module(monkeypatch)
    monday = real_date(2026, 3, 2)
    _freeze_clock(monkeypatch, appmod, monday)

    monkeypatch.setattr(appmod, "_autopilot_load_core_override_choice_map", lambda *_args, **_kwargs: {monday.isoformat(): "rest"})
    monkeypatch.setattr(appmod, "_autopilot_load_actual_completion_maps", _mock_completion_maps)
    monkeypatch.setattr(appmod, "plan_training_window", _mock_schedule_all_open)

    week_final, _meta = appmod._autopilot_build_rolling_shifted_week(_base_plan(), monday)
    session_keys = [row.get("session_key") for row in week_final if row.get("kind") == "plan"]

    assert len(session_keys) == len([key for key in session_keys if key])
    assert len(session_keys) == len(set(session_keys))
