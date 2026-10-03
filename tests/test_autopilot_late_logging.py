from __future__ import annotations

from datetime import date, datetime
import importlib
import os

from core.autopilot_engine import compute_state, determine_next_session


def _load_app_module(monkeypatch):
    monkeypatch.setenv("LIVA_FLASK_SECRET", "test-secret")
    if "app" in os.sys.modules:
        return importlib.reload(os.sys.modules["app"])
    return importlib.import_module("app")


def _plan_with_gym_on_weekdays(weekdays: set[int]) -> dict:
    base_week = []
    for i in range(7):
        if i in weekdays:
            base_week.append({"strength_exercises": [{"exercise_name": "Squat"}], "run_sessions": []})
        else:
            base_week.append({"strength_exercises": [], "run_sessions": []})
    return {"base_week": base_week}


def test_late_logging_window_assumes_done_not_skipped(monkeypatch):
    appmod = _load_app_module(monkeypatch)

    plan = _plan_with_gym_on_weekdays({0, 1, 2, 3, 4, 5})
    out = appmod._compute_adherence_from_context(
        plan,
        today=date(2026, 2, 15),
        now_dt=datetime(2026, 2, 15, 12, 0, 0),
        logged_gym_dates=set(),
        performed_runs_7d=0,
        override_types_7d=[],
        days_since_last_run=12,
        late_logging_window_hours=36,
        late_logging_window_days=10,
    )
    assert out["expected_gym_7d"] == 6
    assert out["logged_done_7d"] == 0
    assert out["assumed_done_7d"] == 6
    assert out["skipped_gym_7d"] == 0
    assert out["actual_7d"] == 6


def test_slots_older_than_window_count_as_skipped(monkeypatch):
    appmod = _load_app_module(monkeypatch)

    # Sunday 2026-02-15: Thu/Fri/Sat/Sun gym planned.
    plan = _plan_with_gym_on_weekdays({3, 4, 5, 6})
    out = appmod._compute_adherence_from_context(
        plan,
        today=date(2026, 2, 15),
        now_dt=datetime(2026, 2, 15, 12, 0, 0),
        logged_gym_dates=set(),
        performed_runs_7d=0,
        override_types_7d=[],
        days_since_last_run=12,
        late_logging_window_hours=24,
        late_logging_window_days=1,
    )
    assert out["expected_gym_7d"] == 4
    assert out["assumed_done_7d"] == 2
    assert out["skipped_gym_7d"] == 2


def test_runs_are_strict_no_assumed_done(monkeypatch):
    appmod = _load_app_module(monkeypatch)

    base_week = [
        {"strength_exercises": [{"exercise_name": "Bench"}], "run_sessions": [{"run_type": "z2"}]},
        {"strength_exercises": [], "run_sessions": [{"run_type": "z2"}]},
        {"strength_exercises": [], "run_sessions": [{"run_type": "threshold"}]},
        {"strength_exercises": [], "run_sessions": []},
        {"strength_exercises": [], "run_sessions": []},
        {"strength_exercises": [], "run_sessions": []},
        {"strength_exercises": [], "run_sessions": []},
    ]
    out = appmod._compute_adherence_from_context(
        {"base_week": base_week},
        today=date(2026, 2, 15),
        now_dt=datetime(2026, 2, 15, 12, 0, 0),
        logged_gym_dates=set(),
        performed_runs_7d=0,
        override_types_7d=[],
        days_since_last_run=15,
        late_logging_window_hours=36,
        late_logging_window_days=2,
    )
    assert out["expected_runs_7d"] == 3
    assert out["skipped_runs_7d"] == 3
    # assumed_done applies only to gym slots.
    assert out["assumed_done_7d"] <= out["expected_gym_7d"]


def test_decision_meta_metrics_contains_new_fields():
    adherence = {
        "expected_7d": 6,
        "actual_7d": 5,
        "skipped_7d": 1,
        "override_count_7d": 2,
        "override_count_7d_whitelisted": 2,
        "override_types_7d": ["manual_override"],
        "logged_done_7d": 3,
        "assumed_done_7d": 2,
        "skipped_gym_7d": 1,
        "skipped_runs_7d": 0,
        "late_window_hours": 36,
        "late_window_days": 2,
        "planned_runs_last_3_weeks": 6,
        "performed_runs_last_3_weeks": 1,
        "days_since_last_run": 12,
    }
    state = compute_state(
        recovery={
            "rmssd_today": 60.0,
            "rhr_today": 50.0,
            "rmssd_7d": 60.0,
            "rmssd_28d": 62.0,
            "rhr_7d": 50.0,
            "rhr_28d": 51.0,
            "recovery_today_missing": False,
            "recovery_history_available": True,
            "recovery_data_missing": False,
        },
        hrv_daily=[],
        sickness=False,
        alcohol=False,
        signal_quality_low_days=0,
        gym_load={
            "push_fatigue_high": False,
            "pull_fatigue_high": False,
            "legs_fatigue_high": False,
            "hi_yesterday": False,
            "hi_exposures_3d": 0,
        },
        run_load={"hi_yesterday": False},
        adherence=adherence,
    )
    decision = determine_next_session(state, today_slots=[{"kind": "rest"}], rotation_sessions=[], rotation_index=None)
    metrics = (((decision.get("decision_meta") or {}).get("inputs") or {}).get("metrics") or {})
    assert metrics["assumed_done_7d"] == 2
    assert metrics["logged_done_7d"] == 3
    assert metrics["skipped_gym_7d"] == 1
    assert metrics["skipped_runs_7d"] == 0
