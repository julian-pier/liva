from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class PlanInput:
    has_plan: bool = True
    plan_name: str = "Upper_Lower_Autoreg"
    phase: str | None = "BUILD"
    block_week: int | None = 1
    block_weeks_total: int | None = 8
    days_to_deload: int | None = None
    rpe_cap: float = 9.0


@dataclass(frozen=True)
class HrvInput:
    trend: str = "stabil"  # "down" | "up" | "stabil" | "volatile"
    rhr_high: bool = False
    sick: bool = False
    missing: bool = False


@dataclass(frozen=True)
class OverrideEventInput:
    decision_type: str | None
    days_ago: int = 0


@dataclass(frozen=True)
class RpeBreachInput:
    breach_amount: float
    days_ago: int = 0
    session_id: int = 1


@dataclass(frozen=True)
class ExpectedOutput:
    verdict: str
    badge: str
    badge_reason_contains: str | None = None
    chips_count: int = 2
    should_not_count_reorder_event: bool = False


@dataclass(frozen=True)
class Scenario:
    name: str
    plan: PlanInput
    hrv: HrvInput = field(default_factory=HrvInput)
    override_events: list[OverrideEventInput] = field(default_factory=list)
    rpe_cap_breaches: list[RpeBreachInput] = field(default_factory=list)
    expected: ExpectedOutput = field(default_factory=lambda: ExpectedOutput(verdict="neutral", badge="CONSISTENT"))


def render_preview(viewmodel: dict[str, Any]) -> dict[str, Any]:
    has_plan = bool(viewmodel.get("has_plan"))
    if has_plan:
        plan_name = str(viewmodel.get("plan_name") or "").strip() or "Block"
        week = viewmodel.get("block_week")
        total = viewmodel.get("block_weeks_total")
        hero = f"{plan_name} · Woche {week}/{total}"
        phase = str(viewmodel.get("phase") or "").strip()
        days = viewmodel.get("days_to_deload")
        if phase and isinstance(days, int) and days > 0:
            unit = "Tag" if days == 1 else "Tagen"
            sub = f"Phase {phase} → Deload in {days} {unit}"
        elif phase:
            sub = f"Phase {phase}"
        else:
            sub = ""
    else:
        hero = "Kein aktiver Block"
        sub = "Wähle einen Plan in /planung"

    badge = str(viewmodel.get("badge") or "").upper().strip()
    badge_reason = str(viewmodel.get("badge_reason") or "").strip()
    badge_text = f"{badge} · {badge_reason}" if badge and badge_reason else badge

    expected = str(viewmodel.get("hrv_expected") or "").strip()
    observed = str(viewmodel.get("hrv_observed") or "").strip() or "keine Daten"
    verdict = str(viewmodel.get("hrv_verdict") or "neutral").strip() or "neutral"
    if expected:
        hrv_line = f"HRV: Erwartet {expected} · Ist {observed} · {verdict}"
    else:
        hrv_line = "HRV: keine Daten"

    chips = []
    for key in ("chip_guardrail", "chip_expectation"):
        txt = str(viewmodel.get(key) or "").strip()
        if txt:
            chips.append(txt)

    return {
        "hero": hero,
        "sub": sub,
        "job": str(viewmodel.get("job_line") or "").strip(),
        "hrv_line": hrv_line,
        "badge_text": badge_text,
        "chips": chips,
    }


SCENARIOS: list[Scenario] = [
    Scenario(
        name="s1_last_week_build_sick_down_passt",
        plan=PlanInput(phase="BUILD", block_week=7, block_weeks_total=8, days_to_deload=1, rpe_cap=9.0),
        hrv=HrvInput(trend="down", rhr_high=True, sick=True),
        expected=ExpectedOutput(verdict="passt", badge="CONSISTENT"),
    ),
    Scenario(
        name="s2_deload_down_kritisch",
        plan=PlanInput(phase="DELOAD", block_week=8, block_weeks_total=8, days_to_deload=None, rpe_cap=8.0),
        hrv=HrvInput(trend="down", rhr_high=False, sick=False),
        expected=ExpectedOutput(verdict="kritisch", badge="CONSISTENT"),
    ),
    Scenario(
        name="s3_build_early_volatile_neutral",
        plan=PlanInput(phase="BUILD", block_week=2, block_weeks_total=8, days_to_deload=20, rpe_cap=9.0),
        hrv=HrvInput(trend="volatile", rhr_high=False, sick=False),
        expected=ExpectedOutput(verdict="neutral", badge="CONSISTENT"),
    ),
    Scenario(
        name="s4_single_small_rpe_breach_consistent",
        plan=PlanInput(phase="BUILD", block_week=4, block_weeks_total=8, days_to_deload=10, rpe_cap=9.0),
        hrv=HrvInput(trend="stabil", rhr_high=False, sick=False),
        rpe_cap_breaches=[RpeBreachInput(breach_amount=0.25, days_ago=0, session_id=1)],
        expected=ExpectedOutput(verdict="passt", badge="CONSISTENT"),
    ),
    Scenario(
        name="s5_repeated_rpe_breaches_drift",
        plan=PlanInput(phase="BUILD", block_week=5, block_weeks_total=8, days_to_deload=8, rpe_cap=9.0),
        hrv=HrvInput(trend="stabil", rhr_high=False, sick=False),
        rpe_cap_breaches=[
            RpeBreachInput(breach_amount=0.2, days_ago=0, session_id=1),
            RpeBreachInput(breach_amount=0.3, days_ago=1, session_id=2),
        ],
        expected=ExpectedOutput(verdict="passt", badge="DRIFT", badge_reason_contains="wiederholt"),
    ),
    Scenario(
        name="s6_single_large_rpe_breach_drift",
        plan=PlanInput(phase="BUILD", block_week=5, block_weeks_total=8, days_to_deload=8, rpe_cap=9.0),
        hrv=HrvInput(trend="stabil", rhr_high=False, sick=False),
        rpe_cap_breaches=[RpeBreachInput(breach_amount=0.6, days_ago=0, session_id=1)],
        expected=ExpectedOutput(verdict="passt", badge="DRIFT", badge_reason_contains="wiederholt"),
    ),
    Scenario(
        name="s7_explicit_autopilot_overrides_drift",
        plan=PlanInput(phase="BUILD", block_week=6, block_weeks_total=8, days_to_deload=4, rpe_cap=9.0),
        hrv=HrvInput(trend="stabil", rhr_high=False, sick=False),
        override_events=[
            OverrideEventInput("autopilot_override", days_ago=0),
            OverrideEventInput("autopilot_override", days_ago=1),
            OverrideEventInput("autopilot_override", days_ago=2),
        ],
        expected=ExpectedOutput(verdict="passt", badge="DRIFT", badge_reason_contains="Autopilot"),
    ),
    Scenario(
        name="s8_session_reordered_ignored",
        plan=PlanInput(phase="BUILD", block_week=3, block_weeks_total=8, days_to_deload=14, rpe_cap=9.0),
        hrv=HrvInput(trend="stabil", rhr_high=False, sick=False),
        override_events=[OverrideEventInput("session_reordered", days_ago=0)],
        expected=ExpectedOutput(
            verdict="passt",
            badge="CONSISTENT",
            should_not_count_reorder_event=True,
        ),
    ),
    Scenario(
        name="e1_hrv_missing_neutral",
        plan=PlanInput(phase="BUILD", block_week=4, block_weeks_total=8, days_to_deload=9, rpe_cap=9.0),
        hrv=HrvInput(trend="stabil", rhr_high=False, sick=False, missing=True),
        expected=ExpectedOutput(verdict="neutral", badge="CONSISTENT"),
    ),
    Scenario(
        name="e2_plan_partial_missing_phase_week",
        plan=PlanInput(phase=None, block_week=None, block_weeks_total=None, days_to_deload=None, rpe_cap=9.0),
        hrv=HrvInput(trend="stabil", rhr_high=False, sick=False, missing=True),
        expected=ExpectedOutput(verdict="neutral", badge="CONSISTENT"),
    ),
    Scenario(
        name="e3_override_missing_fields_ignored",
        plan=PlanInput(phase="BUILD", block_week=4, block_weeks_total=8, days_to_deload=7, rpe_cap=9.0),
        hrv=HrvInput(trend="stabil", rhr_high=False, sick=False),
        override_events=[
            OverrideEventInput(None, days_ago=0),
            OverrideEventInput("unknown_override", days_ago=1),
        ],
        expected=ExpectedOutput(verdict="passt", badge="CONSISTENT"),
    ),
    Scenario(
        name="e4_rpe_breach_exactly_half_boundary",
        plan=PlanInput(phase="BUILD", block_week=4, block_weeks_total=8, days_to_deload=7, rpe_cap=9.0),
        hrv=HrvInput(trend="stabil", rhr_high=False, sick=False),
        rpe_cap_breaches=[RpeBreachInput(breach_amount=0.5, days_ago=0, session_id=1)],
        expected=ExpectedOutput(verdict="passt", badge="DRIFT", badge_reason_contains="wiederholt"),
    ),
]
