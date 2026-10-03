from core.heavy_first_engine import decide_heavy_first


def _base_weights():
    return {
        "sleep": 0.14,
        "hrv": 0.16,
        "nutrition": 0.14,
        "run_interference": 0.12,
        "fatigue": 0.15,
        "back_to_back": 0.11,
        "stress": 0.09,
        "simulation": 0.09,
    }


def _low_state():
    return {
        "illness": {"sick": False, "infection_acute": False},
        "recovery": {"hrv_pressure": 0.12, "missing_extreme": False},
        "readiness": {"sleep_debt_score": 0.1, "trainability_score": 0.86},
        "fatigue": {"muscular_preload": 0.12, "systemic_extreme": False},
        "nutrition": {"deficit_risk": 0.12},
        "load": {"back_to_back_risk": 0.1},
        "run_context": {"interference_risk": 0.1},
        "stress": {"daily_stress": 0.1},
    }


def _high_state():
    return {
        "illness": {"sick": False, "infection_acute": False},
        "recovery": {"hrv_pressure": 0.84, "missing_extreme": False},
        "readiness": {"sleep_debt_score": 0.82, "trainability_score": 0.31},
        "fatigue": {"muscular_preload": 0.78, "systemic_extreme": False},
        "nutrition": {"deficit_risk": 0.76},
        "load": {"back_to_back_risk": 0.66},
        "run_context": {"interference_risk": 0.74},
        "stress": {"daily_stress": 0.7},
    }


def test_heavy_first_keeps_heavy_when_objections_low():
    decision = decide_heavy_first(
        state=_low_state(),
        scenarios=[{"scenario_key": "gym_heavy", "recovery_risk": 0.2, "fatigue_risk": 0.2, "performance_probability_bad": 0.1}],
        parameter_weights=_base_weights(),
    )

    assert decision["started_from"] == "HEAVY"
    assert decision["chosen_mode"] == "HEAVY"
    assert decision["downgrade_path"] == ["HEAVY"]


def test_sick_forces_rest_not_light():
    state = _low_state()
    state["illness"]["sick"] = True

    decision = decide_heavy_first(
        state=state,
        scenarios=[{"scenario_key": "gym_heavy", "recovery_risk": 0.1, "fatigue_risk": 0.1, "performance_probability_bad": 0.1}],
        parameter_weights=_base_weights(),
    )

    assert decision["chosen_mode"] == "REST"
    assert decision["downgrade_path"] == ["HEAVY", "REST"]
    assert any(item["code"] == "SICK" for item in decision["blockers"])


def test_multiple_strong_objections_downgrade_to_light():
    decision = decide_heavy_first(
        state=_high_state(),
        scenarios=[{"scenario_key": "gym_heavy", "recovery_risk": 0.86, "fatigue_risk": 0.81, "performance_probability_bad": 0.74}],
        parameter_weights=_base_weights(),
    )

    assert decision["chosen_mode"] == "LIGHT"
    assert decision["downgrade_path"][-1] == "LIGHT"
    assert len(decision["decisive_reasons"]) >= 2
