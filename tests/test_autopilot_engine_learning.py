from __future__ import annotations

from core.autopilot_engine import compute_state, determine_next_session


def _base_state() -> dict:
    return compute_state(
        recovery={
            "rmssd_today": 95.0,
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
        gym_load={
            "push_fatigue_high": False,
            "pull_fatigue_high": False,
            "legs_fatigue_high": False,
            "hi_yesterday": False,
            "hi_exposures_3d": 0,
        },
        run_load={"hi_yesterday": False},
        adherence={"expected_7d": 5, "actual_7d": 5, "skipped_7d": 0, "override_count_7d": 0},
    )


def test_learning_profile_can_force_light_day_for_gym():
    state = _base_state()
    state["flags"] = {**state["flags"], "hi_yesterday": True}
    state["learning_profile"] = {
        "learning_ready": True,
        "samples_days": 21,
        "override_rate_28d": 0.40,
        "recovery_low_rate_21d": 0.70,
        "run_compliance_21d": 0.55,
        "gym_avg_rpe_14d": 8.6,
        "training_days_14d": 8,
    }
    out = determine_next_session(
        state,
        today_slots=[{"kind": "plan", "session_key": "upper_a", "session_name": "Upper A", "category": "push"}],
        rotation_sessions=[],
        rotation_index=None,
        day_context={"is_today": True, "is_tomorrow": False},
    )
    assert out["kind"] == "plan"
    assert out.get("minimal_gym") is True
    assert "LEARNED_LIGHT_DAY" in (out.get("reason_codes") or [])


def test_learning_profile_scales_threshold_run_duration():
    state = _base_state()
    state["learning_profile"] = {
        "learning_ready": True,
        "samples_days": 28,
        "override_rate_28d": 0.02,
        "recovery_low_rate_21d": 0.08,
        "run_compliance_21d": 1.0,
        "gym_avg_rpe_14d": 6.9,
        "training_days_14d": 13,
    }
    out = determine_next_session(
        state,
        today_slots=[{"kind": "run", "run_kind": "threshold", "run_duration_min": 30}],
        rotation_sessions=[],
        rotation_index=None,
        day_context={"is_today": True, "is_tomorrow": False},
    )
    assert out["kind"] == "run"
    assert out["run_kind"] == "threshold"
    assert int(out.get("run_duration_min") or 0) > 30
