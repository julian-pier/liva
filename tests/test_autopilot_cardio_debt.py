from __future__ import annotations

from core.autopilot_decision import decide_autopilot


DAY = "2026-02-15"


def _base_plan(kind: str) -> dict:
    return {
        "phase": "BUILD",
        "week": "4/8",
        "days_to_deload": 4,
        "planned_session_kind": kind,
    }


def test_cardio_debt_reintro_on_plan_rest_when_safe():
    d = decide_autopilot(
        day=DAY,
        plan_ctx=_base_plan("rest"),
        signals_ctx={"sick": False, "rhr_state": "normal", "hrv_state": "stable"},
        history_ctx={"planned_runs_last_3_weeks": 6, "performed_runs_last_3_weeks": 0, "days_since_last_run": 12},
        rules_ctx={"allow_z2_on_plan_rest": True},
    )
    assert d.chosen_session == "Z2 Run"
    assert d.final_kind == "light"
    assert d.final_kind_source == "rule:CARDIO_REINTRO"
    assert d.applied_effect == "override_rest_with_z2"


def test_cardio_debt_blocked_when_recovery_bad():
    d = decide_autopilot(
        day=DAY,
        plan_ctx=_base_plan("rest"),
        signals_ctx={"sick": False, "rhr_state": "high", "hrv_state": "down"},
        history_ctx={"planned_runs_last_3_weeks": 6, "performed_runs_last_3_weeks": 0, "days_since_last_run": 12},
        rules_ctx={"allow_z2_on_plan_rest": True},
    )
    assert d.chosen_session is None
    assert d.final_kind == "rest"


def test_cardio_debt_never_displaces_gym():
    d = decide_autopilot(
        day=DAY,
        plan_ctx=_base_plan("train"),
        signals_ctx={"sick": False, "rhr_state": "normal", "hrv_state": "stable"},
        history_ctx={"planned_runs_last_3_weeks": 6, "performed_runs_last_3_weeks": 0, "days_since_last_run": 12},
        rules_ctx={"allow_z2_on_plan_rest": True},
    )
    assert d.chosen_session != "Z2 Run"
    assert d.final_kind == "train"


def test_cardio_debt_false_no_change():
    d = decide_autopilot(
        day=DAY,
        plan_ctx=_base_plan("rest"),
        signals_ctx={"sick": False, "rhr_state": "normal", "hrv_state": "stable"},
        history_ctx={"planned_runs_last_3_weeks": 6, "performed_runs_last_3_weeks": 5, "days_since_last_run": 4},
        rules_ctx={"allow_z2_on_plan_rest": True},
    )
    assert d.final_kind == "rest"
    assert d.chosen_session is None


def test_threshold_run_downgraded_to_z2_when_cardio_debt_and_ok_recovery():
    d = decide_autopilot(
        day=DAY,
        plan_ctx={**_base_plan("run"), "planned_run_kind": "threshold"},
        signals_ctx={"sick": False, "rhr_state": "high", "hrv_state": "stable"},
        history_ctx={"planned_runs_last_3_weeks": 6, "performed_runs_last_3_weeks": 0, "days_since_last_run": 14},
        rules_ctx={"allow_z2_on_plan_rest": False},
    )
    assert d.chosen_session == "Run Z2"
    assert d.final_kind == "train"
    assert d.final_kind_source == "rule:CARDIO_REINTRO"
