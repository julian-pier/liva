from __future__ import annotations

from datetime import datetime
from pathlib import Path
import subprocess
import sys

import pytest

from core.autopilot_decision import EXPLAIN_RULE_ORDER, decide_autopilot, format_decision_summary
from tests.helpers.autopilot_scenarios import SCENARIOS, Scenario


FORBIDDEN_USER_TERMS = {
    "hrv_ratio",
    "rhr_ratio",
    "guard",
    "PLAN_KIND",
    "SICKNESS_WINDOW",
    "FATIGUE_STATE",
}


def _triggered_rules(explain: dict) -> list[str]:
    checks = explain.get("checks") or []
    return [str(c.get("rule")) for c in checks if c.get("passed") is True]


def _explain_debug(decision) -> str:
    exp = decision.explain
    checks = exp.get("checks") or []
    compact = ", ".join(f"{c.get('rule')}={bool(c.get('passed'))}" for c in checks)
    return (
        f"final={decision.final_kind} mode={decision.mode} "
        f"reason={exp.get('decision', {}).get('reason')} checks=[{compact}]"
    )


@pytest.mark.parametrize("scenario", SCENARIOS, ids=[s.name for s in SCENARIOS])
def test_autopilot_scenarios(scenario: Scenario):
    decision = decide_autopilot(
        day=scenario.day,
        plan_ctx=scenario.plan_ctx,
        signals_ctx=scenario.signals_ctx,
        history_ctx=scenario.history_ctx,
        rules_ctx=scenario.rules_ctx,
    )
    explain = decision.explain
    reason = str((explain.get("decision") or {}).get("reason") or "")
    triggered = _triggered_rules(explain)
    checks_order = [str(c.get("rule")) for c in (explain.get("checks") or [])]
    debug = _explain_debug(decision)

    assert decision.final_kind == scenario.expected.final_kind, debug
    assert decision.mode == scenario.expected.mode, debug
    assert scenario.expected.reason_contains in str((explain.get("decision") or {}).get("reason_chain") or reason), debug
    assert checks_order == EXPLAIN_RULE_ORDER, debug

    # Adjustments match policy mapping intent
    adjustments = decision.adjustments
    assert float(adjustments.get("rpe_shift")) == pytest.approx(scenario.expected.rpe_shift), debug
    assert int(adjustments.get("topset_delta_mainlifts")) == scenario.expected.topset_delta_mainlifts, debug

    # One-liner is always one line, non-technical and policy-conform
    one_liner = decision.user_one_liner
    assert "\n" not in one_liner, debug
    assert scenario.expected.user_one_liner_contains in one_liner, debug
    for term in FORBIDDEN_USER_TERMS:
        assert term not in one_liner, debug

    # Explain summary consistency
    summary = explain.get("summary") or {}
    assert summary.get("primary") == scenario.expected.explain_primary, debug
    contributors = summary.get("contributors") or []
    for rule in scenario.expected.explain_contributors_contains:
        assert rule in contributors, debug

    for rule in scenario.expected.explain_rules_triggered:
        assert rule in triggered, debug
    for rule in scenario.expected.explain_rules_not_triggered:
        assert rule not in triggered, debug

    # Decision reason must match summary primary
    assert (explain.get("decision") or {}).get("reason") == summary.get("primary"), debug

    if scenario.expected.chosen_session is not None:
        assert decision.chosen_session == scenario.expected.chosen_session, debug
    cardio = (decision.adjustments or {}).get("cardio_reintro") or {}
    if scenario.expected.chosen_session == "Z2 Run":
        assert cardio.get("enabled") is True, debug
        assert 20 <= int(cardio.get("duration_min") or 0) <= 40, debug
    elif cardio:
        assert cardio.get("enabled") in {False, None}, debug


def test_idempotent_recompute_when_late_logging_arrives():
    plan_ctx = {"phase": "BUILD", "week": "3/8", "days_to_deload": 4, "planned_session_kind": "train", "expected_fatigue_level": "low"}
    signals_ctx = {"sick": False, "rhr_state": "high", "hrv_state": "stable"}

    d1 = decide_autopilot(
        day="2026-02-15",
        plan_ctx=plan_ctx,
        signals_ctx=signals_ctx,
        history_ctx={"last_session_missing": True},
        rules_ctx={},
    )
    d2 = decide_autopilot(
        day="2026-02-15",
        plan_ctx=plan_ctx,
        signals_ctx=signals_ctx,
        history_ctx={"last_sessions": [{"ts": "2026-02-14T10:00:00", "kind": "train", "intensity": "normal"}]},
        rules_ctx={},
    )

    assert d1.final_kind == d2.final_kind == "light"
    assert d1.adjustments["rpe_shift"] == d2.adjustments["rpe_shift"] == -1.0


def test_rest_is_rare_in_policy_scenarios():
    decisions = [
        decide_autopilot(
            day=s.day,
            plan_ctx=s.plan_ctx,
            signals_ctx=s.signals_ctx,
            history_ctx=s.history_ctx,
            rules_ctx=s.rules_ctx,
        )
        for s in SCENARIOS
    ]
    rest_n = sum(1 for d in decisions if d.final_kind == "rest")
    assert rest_n <= 12


def test_summary_line_contains_primary_and_optional_contributors():
    scn = next(s for s in SCENARIOS if s.name == "s10_build_high_pulse_and_down_light")
    decision = decide_autopilot(
        day=scn.day,
        plan_ctx=scn.plan_ctx,
        signals_ctx=scn.signals_ctx,
        history_ctx=scn.history_ctx,
        rules_ctx=scn.rules_ctx,
    )
    line = format_decision_summary(decision)
    assert line.startswith("LIGHT because: ")
    assert "FATIGUE_STATE" in line


def test_plan_ctx_propagates_into_explain_inputs():
    scn = next(s for s in SCENARIOS if s.name == "s24_days_to_deload_1_push_allowed_train")
    decision = decide_autopilot(
        day=scn.day,
        plan_ctx=scn.plan_ctx,
        signals_ctx=scn.signals_ctx,
        history_ctx=scn.history_ctx,
        rules_ctx=scn.rules_ctx,
    )
    inputs = decision.explain["inputs"]
    assert inputs["phase"] != "unknown"
    assert "/" in str(inputs["week"])
    assert isinstance(inputs["days_to_deload"], int)


def test_snapshot_t1_planfit_gates_moderate_fatigue():
    scn = next(s for s in SCENARIOS if s.name == "t1_expected_high_observed_moderate_train")
    decision = decide_autopilot(
        day=scn.day,
        plan_ctx=scn.plan_ctx,
        signals_ctx=scn.signals_ctx,
        history_ctx=scn.history_ctx,
        rules_ctx=scn.rules_ctx,
    )
    assert decision.final_kind == "train"
    assert decision.explain["summary"]["primary"] == "PLAN_FIT"
    contributors = decision.explain["summary"]["contributors"] or []
    assert "DELOAD_TIMING" in contributors
    assert "FATIGUE_STATE" in contributors
    assert decision.user_one_liner == "Overreach-Phase: Müdigkeit ist eingeplant. Nach Plan durchziehen."


def test_snapshot_one_liner_umlauts_exact():
    d_train = decide_autopilot(
        day="2026-02-15",
        plan_ctx={"phase": "BUILD", "week": "7/8", "days_to_deload": 1, "planned_session_kind": "train"},
        signals_ctx={"sick": False, "rhr_state": "high", "hrv_state": "stable"},
        history_ctx={},
        rules_ctx={},
    )
    d_light = decide_autopilot(
        day="2026-02-15",
        plan_ctx={"phase": "BUILD", "week": "2/8", "days_to_deload": 6, "planned_session_kind": "train", "expected_fatigue_level": "low"},
        signals_ctx={"sick": False, "rhr_state": "high", "hrv_state": "stable"},
        history_ctx={},
        rules_ctx={},
    )
    d_rest = decide_autopilot(
        day="2026-02-15",
        plan_ctx={"phase": "BUILD", "week": "2/8", "days_to_deload": 6, "planned_session_kind": "train"},
        signals_ctx={"sick": False, "rhr_state": "extreme", "hrv_state": "down"},
        history_ctx={},
        rules_ctx={},
    )
    assert d_train.user_one_liner == "Overreach-Phase: Müdigkeit ist eingeplant. Nach Plan durchziehen."
    assert d_light.user_one_liner == "Heute leichter: Erholung schlechter als geplant. RPE -1, je Übung -1 Satz."
    assert d_rest.user_one_liner == "Heute leichter: Erholung schlechter als geplant. RPE -1, je Übung -1 Satz."


def test_plan_rest_primary_unchanged_plan_kind():
    scn = next(s for s in SCENARIOS if s.name == "s1_plan_rest_forces_rest")
    decision = decide_autopilot(
        day=scn.day,
        plan_ctx=scn.plan_ctx,
        signals_ctx=scn.signals_ctx,
        history_ctx=scn.history_ctx,
        rules_ctx=scn.rules_ctx,
    )
    assert decision.explain["summary"]["primary"] == "PLAN_KIND"


def test_note_only_never_changes_final_kind_parametrized():
    base_plan = {"phase": "BUILD", "week": "4/8", "days_to_deload": 4, "planned_session_kind": "train"}
    base_signals = {"sick": False, "rhr_state": "normal", "hrv_state": "stable"}
    cases = [
        {"name": "fatigue_note_only", "plan": {**base_plan, "expected_fatigue_level": "moderate"}, "signals": {**base_signals, "rhr_state": "high", "hrv_state": "stable"}, "history": {}},
        {"name": "subjective_note_only", "plan": base_plan, "signals": {**base_signals, "subjective_low_motivation": True}, "history": {}},
        {"name": "reorder_note_only", "plan": base_plan, "signals": base_signals, "history": {"session_reorder_only": True}},
        {"name": "late_logging_note_only", "plan": base_plan, "signals": base_signals, "history": {"last_session_missing": True}},
    ]
    for case in cases:
        decision = decide_autopilot(
            day="2026-02-15",
            plan_ctx=case["plan"],
            signals_ctx=case["signals"],
            history_ctx=case["history"],
            rules_ctx={},
        )
        assert decision.final_kind == "train", case["name"]
        assert decision.applied_effect == "none", case["name"]
        assert decision.final_kind_source == "default:plan", case["name"]


def test_sickness_window_missing_timestamp_defaults_to_inactive():
    decision = decide_autopilot(
        day="2026-02-15",
        plan_ctx={"phase": "BUILD", "week": "3/8", "days_to_deload": 4, "planned_session_kind": "train"},
        signals_ctx={"sick": True, "rhr_state": "normal", "hrv_state": "stable"},
        history_ctx={},
        rules_ctx={},
        now=datetime(2026, 2, 15, 12, 0, 0),
    )
    inputs = decision.explain["inputs"]
    assert inputs["sick_active_24h"] is False
    assert inputs["sick_window_unknown"] is True
    triggered = _triggered_rules(decision.explain)
    assert "SICKNESS_WINDOW" not in triggered


def test_sickness_window_239_hours_is_active():
    decision = decide_autopilot(
        day="2026-02-15",
        plan_ctx={"phase": "BUILD", "week": "3/8", "days_to_deload": 4, "planned_session_kind": "train"},
        signals_ctx={"sick": True, "sick_ts": "2026-02-14T12:06:00", "rhr_state": "normal", "hrv_state": "stable"},
        history_ctx={},
        rules_ctx={},
        now=datetime(2026, 2, 15, 12, 0, 0),
    )
    assert decision.explain["inputs"]["sick_active_24h"] is True
    assert "SICKNESS_WINDOW" in _triggered_rules(decision.explain)


def test_sickness_window_241_hours_is_inactive():
    decision = decide_autopilot(
        day="2026-02-15",
        plan_ctx={"phase": "BUILD", "week": "3/8", "days_to_deload": 4, "planned_session_kind": "train"},
        signals_ctx={"sick": True, "sick_ts": "2026-02-14T11:54:00", "rhr_state": "normal", "hrv_state": "stable"},
        history_ctx={},
        rules_ctx={},
        now=datetime(2026, 2, 15, 12, 0, 0),
    )
    assert decision.explain["inputs"]["sick_active_24h"] is False
    assert "SICKNESS_WINDOW" not in _triggered_rules(decision.explain)


def test_sickness_window_negative_delta_is_inactive():
    decision = decide_autopilot(
        day="2026-02-15",
        plan_ctx={"phase": "BUILD", "week": "3/8", "days_to_deload": 4, "planned_session_kind": "train"},
        signals_ctx={"sick": True, "sick_ts": "2026-02-15T13:00:00", "rhr_state": "normal", "hrv_state": "stable"},
        history_ctx={},
        rules_ctx={},
        now=datetime(2026, 2, 15, 12, 0, 0),
    )
    assert decision.explain["inputs"]["sick_active_24h"] is False
    assert "SICKNESS_WINDOW" not in _triggered_rules(decision.explain)


def test_plan_rest_z2_allowed_wording_disabled_snapshot():
    scn = next(s for s in SCENARIOS if s.name == "s26_cardio_debt_plan_rest_default_stays_rest")
    decision = decide_autopilot(
        day=scn.day,
        plan_ctx=scn.plan_ctx,
        signals_ctx=scn.signals_ctx,
        history_ctx=scn.history_ctx,
        rules_ctx=scn.rules_ctx,
    )
    check = next(c for c in decision.explain["checks"] if c["rule"] == "PLAN_REST_Z2_ALLOWED")
    assert check["passed"] is False
    assert check["why"] == "plan-rest override disabled"


def test_explain_compact_includes_plan_fit_line():
    root = Path(__file__).resolve().parents[1]
    cmd = [
        sys.executable,
        str(root / "tools" / "autopilot_explain.py"),
        "--day",
        "2026-02-15",
        "--scenario",
        "t1_expected_high_observed_moderate_train",
        "--explain-compact",
    ]
    proc = subprocess.run(cmd, cwd=root, capture_output=True, text=True, check=True)
    out = proc.stdout
    assert "Plan-Fit: expected=high · observed=moderate · passt" in out
