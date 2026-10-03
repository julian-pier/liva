from __future__ import annotations

from core.autopilot_engine import build_reason_codes, compute_state


def _empty_load():
    return {
        "hi_exposures_3d": 0,
        "hi_exposures_7d": 0,
        "hi_exposures_14d": 0,
        "hi_yesterday": False,
        "push_fatigue_high": False,
        "pull_fatigue_high": False,
        "legs_fatigue_high": False,
    }


def test_recovery_today_missing_but_history_available_not_missing():
    state = compute_state(
        recovery={
            "rmssd_today": None,
            "rhr_today": None,
            "rmssd_7d": 60.0,
            "rmssd_28d": 62.0,
            "rhr_7d": 50.0,
            "rhr_28d": 51.0,
            "recovery_today_missing": True,
            "recovery_history_available": True,
            "recovery_data_missing": False,
        },
        hrv_daily=[],
        sickness=False,
        alcohol=False,
        signal_quality_low_days=0,
        gym_load=_empty_load(),
        run_load={"hi_yesterday": False},
        adherence={"expected_7d": 0, "actual_7d": 0, "skipped_7d": 0, "override_count_7d": 0},
    )
    recovery = state["recovery"]
    assert recovery["recovery_today_missing"] is True
    assert recovery["recovery_history_available"] is True
    assert recovery["recovery_data_missing"] is False
    assert recovery["hrv_ratio"] is not None
    assert recovery["rhr_ratio"] is not None
    assert "RECOVERY_MISSING" not in build_reason_codes(state)


def test_recovery_missing_when_ratio_not_computable():
    state = compute_state(
        recovery={
            "rmssd_today": None,
            "rhr_today": None,
            "rmssd_7d": None,
            "rmssd_28d": None,
            "rhr_7d": None,
            "rhr_28d": None,
            "recovery_today_missing": True,
            "recovery_history_available": False,
            "recovery_data_missing": True,
        },
        hrv_daily=[],
        sickness=False,
        alcohol=False,
        signal_quality_low_days=0,
        gym_load=_empty_load(),
        run_load={"hi_yesterday": False},
        adherence={"expected_7d": 0, "actual_7d": 0, "skipped_7d": 0, "override_count_7d": 0},
    )
    assert state["recovery"]["recovery_data_missing"] is True
    assert "RECOVERY_MISSING" in build_reason_codes(state)


def test_zns_fatigue_is_reported_separately_from_legs_fatigue():
    state = compute_state(
        recovery={
            "rmssd_today": 80.0,
            "rhr_today": 53.0,
            "rmssd_7d": 95.0,
            "rmssd_28d": 120.0,
            "rhr_7d": 52.0,
            "rhr_28d": 49.0,
            "recovery_today_missing": False,
            "recovery_history_available": True,
            "recovery_data_missing": False,
        },
        hrv_daily=[],
        sickness=False,
        alcohol=False,
        signal_quality_low_days=0,
        gym_load={**_empty_load(), "legs_fatigue_high": True},
        run_load={"hi_yesterday": False},
        adherence={"expected_7d": 0, "actual_7d": 0, "skipped_7d": 0, "override_count_7d": 0},
    )
    codes = build_reason_codes(state)
    assert "LEGS_FATIGUE" in codes
    assert "ZNS_FATIGUE" in codes
