from __future__ import annotations

from core.autopilot_engine import compute_state, determine_next_session


def _base_load():
    return {
        "hi_exposures_3d": 0,
        "hi_exposures_7d": 0,
        "hi_exposures_14d": 0,
        "hi_yesterday": False,
        "push_fatigue_high": False,
        "pull_fatigue_high": False,
        "legs_fatigue_high": False,
    }


def test_hrv_low_alone_no_longer_forces_rest_rc3():
    state = compute_state(
        recovery={
            "rmssd_today": 80.0,
            "rhr_today": 52.0,
            "rmssd_7d": 100.0,
            "rmssd_28d": 120.0,
            "rhr_7d": 50.0,
            "rhr_28d": 49.0,
            "recovery_today_missing": False,
            "recovery_history_available": True,
            "recovery_data_missing": False,
        },
        hrv_daily=[],
        sickness=False,
        alcohol=False,
        signal_quality_low_days=0,
        gym_load=_base_load(),
        run_load={"hi_yesterday": False},
        adherence={"expected_7d": 5, "actual_7d": 4, "skipped_7d": 1, "override_count_7d": 0},
    )
    decision = determine_next_session(
        state,
        today_slots=[{"kind": "plan", "session_key": "a", "session_name": "Upper A", "category": "push"}],
        rotation_sessions=[],
        rotation_index=None,
        day_context={"is_today": True, "is_tomorrow": False},
    )
    assert decision["kind"] == "plan"
    assert decision.get("minimal_gym") is True


def test_extreme_combo_still_forces_rest():
    state = compute_state(
        recovery={
            "rmssd_today": 70.0,
            "rhr_today": 57.0,
            "rmssd_7d": 95.0,
            "rmssd_28d": 120.0,
            "rhr_7d": 54.0,
            "rhr_28d": 49.0,
            "recovery_today_missing": False,
            "recovery_history_available": True,
            "recovery_data_missing": False,
        },
        hrv_daily=[],
        sickness=False,
        alcohol=False,
        signal_quality_low_days=0,
        gym_load=_base_load(),
        run_load={"hi_yesterday": False},
        adherence={"expected_7d": 5, "actual_7d": 4, "skipped_7d": 1, "override_count_7d": 0},
    )
    decision = determine_next_session(
        state,
        today_slots=[{"kind": "plan", "session_key": "a", "session_name": "Upper A", "category": "push"}],
        rotation_sessions=[],
        rotation_index=None,
        day_context={"is_today": True, "is_tomorrow": False},
    )
    assert decision["kind"] == "rest"


def test_run_day_with_legs_fatigue_keeps_threshold_when_recovery_is_fine():
    state = compute_state(
        recovery={
            "rmssd_today": 102.0,
            "rhr_today": 50.0,
            "rmssd_7d": 100.0,
            "rmssd_28d": 100.0,
            "rhr_7d": 50.0,
            "rhr_28d": 50.0,
            "recovery_today_missing": False,
            "recovery_history_available": True,
            "recovery_data_missing": False,
        },
        hrv_daily=[],
        sickness=False,
        alcohol=False,
        signal_quality_low_days=0,
        gym_load={**_base_load(), "legs_fatigue_high": True},
        run_load={"hi_yesterday": False},
        adherence={"expected_7d": 5, "actual_7d": 4, "skipped_7d": 1, "override_count_7d": 0},
    )
    decision = determine_next_session(
        state,
        today_slots=[{"kind": "run", "run_kind": "threshold"}],
        rotation_sessions=[],
        rotation_index=None,
        day_context={"is_today": True, "is_tomorrow": False},
    )
    assert decision["kind"] == "run"
    assert decision["run_kind"] == "threshold"


def test_tomorrow_ignores_global_recovery_without_sickness():
    state = compute_state(
        recovery={
            "rmssd_today": 70.0,
            "rhr_today": 57.0,
            "rmssd_7d": 95.0,
            "rmssd_28d": 120.0,
            "rhr_7d": 54.0,
            "rhr_28d": 49.0,
            "recovery_today_missing": False,
            "recovery_history_available": True,
            "recovery_data_missing": False,
        },
        hrv_daily=[],
        sickness=False,
        alcohol=True,
        signal_quality_low_days=0,
        gym_load=_base_load(),
        run_load={"hi_yesterday": True},
        adherence={"expected_7d": 5, "actual_7d": 4, "skipped_7d": 1, "override_count_7d": 0},
    )
    decision = determine_next_session(
        state,
        today_slots=[{"kind": "run", "run_kind": "threshold"}],
        rotation_sessions=[],
        rotation_index=None,
        day_context={"is_today": False, "is_tomorrow": True},
    )
    assert decision["kind"] == "run"
    assert decision["run_kind"] == "threshold"


def test_tomorrow_allows_local_fatigue_adjustment():
    state = compute_state(
        recovery={
            "rmssd_today": 110.0,
            "rhr_today": 50.0,
            "rmssd_7d": 105.0,
            "rmssd_28d": 120.0,
            "rhr_7d": 50.0,
            "rhr_28d": 49.0,
            "recovery_today_missing": False,
            "recovery_history_available": True,
            "recovery_data_missing": False,
        },
        hrv_daily=[],
        sickness=False,
        alcohol=False,
        signal_quality_low_days=0,
        gym_load={**_base_load(), "legs_fatigue_high": True},
        run_load={"hi_yesterday": False},
        adherence={"expected_7d": 5, "actual_7d": 4, "skipped_7d": 1, "override_count_7d": 0},
    )
    decision = determine_next_session(
        state,
        today_slots=[{"kind": "plan", "session_key": "legs", "session_name": "Legs", "category": "legs"}],
        rotation_sessions=[],
        rotation_index=None,
        day_context={"is_today": False, "is_tomorrow": True},
    )
    assert decision["kind"] == "plan"
    assert decision.get("minimal_gym") is True


def test_upper_day_with_legs_fatigue_stays_normal_when_recovery_is_fine():
    state = compute_state(
        recovery={
            "rmssd_today": 102.0,
            "rhr_today": 50.0,
            "rmssd_7d": 100.0,
            "rmssd_28d": 100.0,
            "rhr_7d": 50.0,
            "rhr_28d": 50.0,
            "recovery_today_missing": False,
            "recovery_history_available": True,
            "recovery_data_missing": False,
        },
        hrv_daily=[],
        sickness=False,
        alcohol=False,
        signal_quality_low_days=0,
        gym_load={**_base_load(), "legs_fatigue_high": True},
        run_load={"hi_yesterday": False},
        adherence={"expected_7d": 5, "actual_7d": 4, "skipped_7d": 1, "override_count_7d": 0},
    )
    decision = determine_next_session(
        state,
        today_slots=[{"kind": "plan", "session_key": "push_a", "session_name": "Push A", "category": "push"}],
        rotation_sessions=[],
        rotation_index=None,
        day_context={"is_today": True, "is_tomorrow": False},
    )
    assert decision["kind"] == "plan"
    assert decision.get("minimal_gym") is False


def test_upper_day_with_only_mild_rc1_and_legs_fatigue_stays_normal():
    state = compute_state(
        recovery={
            "rmssd_today": 138.64,
            "rhr_today": 51.92,
            "rmssd_7d": 120.73,
            "rmssd_28d": 123.34,
            "rhr_7d": 49.21,
            "rhr_28d": 49.39,
            "recovery_today_missing": False,
            "recovery_history_available": True,
            "recovery_data_missing": False,
        },
        hrv_daily=[],
        sickness=False,
        alcohol=False,
        signal_quality_low_days=0,
        gym_load={**_base_load(), "legs_fatigue_high": True},
        run_load={"hi_yesterday": False},
        adherence={"expected_7d": 10, "actual_7d": 5, "skipped_7d": 5, "override_count_7d": 0},
    )
    decision = determine_next_session(
        state,
        today_slots=[{"kind": "plan", "session_key": "push_a", "session_name": "Push A", "category": "push"}],
        rotation_sessions=[],
        rotation_index=None,
        day_context={"is_today": True, "is_tomorrow": False},
    )
    assert decision["kind"] == "plan"
    assert decision.get("minimal_gym") is False
