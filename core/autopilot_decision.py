from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta
from typing import Any, Mapping


EXPLAIN_RULE_ORDER = [
    "PLAN_KIND",
    "SICKNESS_WINDOW",
    "SICKNESS_SEVERITY",
    "PLAN_FIT",
    "FATIGUE_STATE",
    "DELOAD_TIMING",
    "GYM_PRIORITY",
    "RECENCY",
    "MISSING_DATA",
    "LATE_LOGGING",
    "MANUAL_OVERRIDE_PRESENT",
    "SESSION_REORDER_ONLY",
    "SUBJECTIVE_IGNORED",
    "CARDIO_DEBT",
    "RECOVERY_GOOD",
    "NO_GYM_CONFLICT",
    "PLAN_REST_Z2_ALLOWED",
    "CARDIO_REINTRO",
]


@dataclass(frozen=True)
class DecisionCheck:
    rule: str
    passed: bool
    effect: str
    why: str


@dataclass(frozen=True)
class Decision:
    final_kind: str
    chosen_session: str | None
    mode: str
    adjustments: dict[str, Any]
    user_one_liner: str
    applied_effect: str
    final_kind_source: str
    explain: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _parse_day(value: date | str | None) -> date:
    if isinstance(value, date):
        return value
    if isinstance(value, str) and value.strip():
        return date.fromisoformat(value.strip())
    return date.today()


def _parse_dt(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not value.strip():
        return None
    txt = value.strip()
    if txt.endswith("Z"):
        txt = txt[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(txt)
    except Exception:
        return None


def _to_int(value: Any) -> int | None:
    try:
        return int(value)
    except Exception:
        return None


def _to_float(value: Any) -> float | None:
    try:
        return float(value)
    except Exception:
        return None


def _derive_rhr_state(signals_ctx: Mapping[str, Any], rules_ctx: Mapping[str, Any]) -> str:
    explicit = str(signals_ctx.get("rhr_state") or "").strip().lower()
    if explicit in {"extreme", "high", "normal", "unknown"}:
        return explicit

    delta = _to_float(signals_ctx.get("rhr_delta"))
    high_th = _to_float(rules_ctx.get("rhr_high_delta_threshold"))
    extreme_th = _to_float(rules_ctx.get("rhr_extreme_delta_threshold"))
    if high_th is None:
        high_th = 4.0
    if extreme_th is None:
        extreme_th = 8.0
    if delta is None:
        return "unknown"
    if delta >= extreme_th:
        return "extreme"
    if delta >= high_th:
        return "high"
    return "normal"


def _derive_hrv_state(signals_ctx: Mapping[str, Any], rules_ctx: Mapping[str, Any]) -> str:
    explicit = str(signals_ctx.get("hrv_state") or "").strip().lower()
    if explicit in {"down", "stable", "up", "missing"}:
        return explicit

    ratio = _to_float(signals_ctx.get("hrv_ratio"))
    down_th = _to_float(rules_ctx.get("hrv_down_ratio_threshold"))
    if down_th is None:
        down_th = 0.90
    if ratio is None:
        return "missing"
    if ratio <= down_th:
        return "down"
    if ratio >= 1.05:
        return "up"
    return "stable"


def _recent_training(day: date, history_ctx: Mapping[str, Any], now: datetime | None) -> dict[str, Any]:
    sessions = history_ctx.get("last_sessions") or []
    if not isinstance(sessions, list):
        sessions = []
    if now is None:
        now = datetime.combine(day, time(12, 0, 0))

    parsed: list[dict[str, Any]] = []
    for item in sessions:
        if not isinstance(item, Mapping):
            continue
        ts = _parse_dt(item.get("ts"))
        if ts is None:
            continue
        parsed.append(
            {
                "ts": ts,
                "kind": str(item.get("kind") or "train"),
                "intensity": str(item.get("intensity") or ""),
            }
        )

    # Missing gym logs must not be interpreted as missed sessions by default.
    # We only synthesize conservative gym completions (normal intensity) when explicit plan hints exist.
    assume_completed_if_missing = bool(history_ctx.get("assume_completed_if_missing", True))
    if assume_completed_if_missing and not parsed:
        planned_recent = history_ctx.get("planned_recent_sessions") or []
        if isinstance(planned_recent, list):
            ref_now = now if now is not None else datetime.combine(day, time(12, 0, 0))
            for item in planned_recent:
                if not isinstance(item, Mapping):
                    continue
                if bool(item.get("explicit_skipped")):
                    continue
                kind = str(item.get("kind") or "train").strip().lower()
                # Do not assume runs were completed; debt should rely on real run logs.
                if kind.startswith("run"):
                    continue
                hours_ago = _to_float(item.get("hours_ago"))
                if hours_ago is None or hours_ago < 0:
                    continue
                parsed.append(
                    {
                        "ts": ref_now.replace(microsecond=0) - timedelta(hours=hours_ago),
                        "kind": kind or "train",
                        "intensity": "normal",
                    }
                )
        elif bool(history_ctx.get("planned_gym_last_24h")):
            ref_now = now if now is not None else datetime.combine(day, time(12, 0, 0))
            parsed.append({"ts": ref_now.replace(microsecond=0) - timedelta(hours=12), "kind": "train", "intensity": "normal"})
        elif bool(history_ctx.get("planned_gym_last_48h")):
            ref_now = now if now is not None else datetime.combine(day, time(12, 0, 0))
            parsed.append({"ts": ref_now.replace(microsecond=0) - timedelta(hours=36), "kind": "train", "intensity": "normal"})
    parsed.sort(key=lambda x: x["ts"], reverse=True)

    last = parsed[0] if parsed else None
    hard_24h = False
    hard_36h = False
    trained_24h = False
    trained_48h = False
    for item in parsed:
        delta_h = (now - item["ts"]).total_seconds() / 3600.0
        if delta_h < 0:
            continue
        if delta_h <= 48.0:
            trained_48h = True
        if delta_h <= 24.0:
            trained_24h = True
            if item["intensity"] == "hard":
                hard_24h = True
        if delta_h <= 36.0 and item["intensity"] == "hard":
            hard_36h = True

    return {
        "last_48h": "trained" if trained_48h else "rested",
        "trained_24h": trained_24h,
        "hard_last_24h": hard_24h,
        "hard_last_36h": hard_36h,
        "last_session_kind": (last["kind"] if last else "none"),
    }


def _sickness_window_info(signals_ctx: Mapping[str, Any], day_obj: date, now: datetime | None) -> dict[str, Any]:
    sick_raw = bool(signals_ctx.get("sick"))
    hours_ago = _to_float(signals_ctx.get("sick_hours_ago"))
    sick_ts_raw = signals_ctx.get("sick_ts")
    sick_ts = _parse_dt(sick_ts_raw)
    hours_since_sick: float | None = None
    unknown = False

    if not sick_raw:
        return {
            "sick_raw": False,
            "sick_ts": sick_ts_raw,
            "sick_hours_ago": hours_ago,
            "hours_since_sick": None,
            "sick_window_unknown": False,
            "active_24h": False,
        }

    if hours_ago is not None:
        active = 0.0 <= hours_ago <= 24.0
        return {
            "sick_raw": True,
            "sick_ts": sick_ts_raw,
            "sick_hours_ago": hours_ago,
            "hours_since_sick": hours_ago,
            "sick_window_unknown": False,
            "active_24h": active,
        }

    if sick_ts is not None:
        ref_now = now if now is not None else datetime.combine(day_obj, time(12, 0, 0))
        hours_since_sick = (ref_now - sick_ts).total_seconds() / 3600.0
        if hours_since_sick < 0:
            return {
                "sick_raw": True,
                "sick_ts": sick_ts_raw,
                "sick_hours_ago": None,
                "hours_since_sick": hours_since_sick,
                "sick_window_unknown": False,
                "active_24h": False,
            }
        return {
            "sick_raw": True,
            "sick_ts": sick_ts_raw,
            "sick_hours_ago": None,
            "hours_since_sick": hours_since_sick,
            "sick_window_unknown": False,
            "active_24h": hours_since_sick <= 24.0,
        }

    unknown = True
    return {
        "sick_raw": True,
        "sick_ts": sick_ts_raw,
        "sick_hours_ago": None,
        "hours_since_sick": hours_since_sick,
        "sick_window_unknown": unknown,
        "active_24h": False,
    }


def _standard_light_adjustments(extreme: bool, planned_topset: bool) -> dict[str, Any]:
    return {
        "rpe_shift": -1.0,
        "sets_rule": {
            "mapping": {"2": 2, "3": 2, "4": 3, ">=5": "-2" if extreme else "-1"},
            "description": "2->2, 3->2, 4->3, >=5 -1",
            "extreme_variant": bool(extreme),
        },
        "topset_delta_mainlifts": -1 if planned_topset else 0,
    }


def _deload_adjustments(planned_topset: bool) -> dict[str, Any]:
    return {
        "rpe_shift": -1.0,
        "sets_rule": {
            "mapping": {"2": 2, "3": 2, "4": 3, ">=5": "-1"},
            "description": "deload: locker halten",
            "extreme_variant": False,
        },
        "topset_delta_mainlifts": -1 if planned_topset else 0,
    }


def _no_adjustments() -> dict[str, Any]:
    return {
        "rpe_shift": 0.0,
        "sets_rule": {
            "mapping": {"2": 2, "3": 3, "4": 4, ">=5": "0"},
            "description": "keine Anpassung",
            "extreme_variant": False,
        },
        "topset_delta_mainlifts": 0,
    }


def _compose_one_liner(final_kind: str, mode: str, *, plan_is_rest: bool, overreach_expected: bool, deload_phase: bool) -> str:
    if plan_is_rest and final_kind == "rest":
        return "Plan-Ruhetag. Heute frei."
    if final_kind == "off":
        return "Plan-Off-Tag. Heute frei."
    if deload_phase and final_kind in {"train", "light"}:
        return "Deload: locker trainieren (RPE 6-7)."
    if overreach_expected and final_kind == "train":
        return "Overreach-Phase: Müdigkeit ist eingeplant. Nach Plan durchziehen."
    if final_kind == "light":
        return "Heute leichter: Erholung schlechter als geplant. RPE -1, je Übung -1 Satz."
    if final_kind == "rest":
        return "Heute pausieren: Belastung ist ungewöhnlich hoch. Morgen neu ansetzen."
    return "Plan-Tag. Voll nach Plan."


def _effect_rank(effect: str) -> int:
    mapping = {
        "override_rest_with_z2": 6,
        "force_rest": 5,
        "force_rest_or_off": 5,
        "prefer_rest_or_light": 4,
        "prefer_light": 3,
        "prefer_light_or_rest": 3,
        "note_only": 1,
        "lower_confidence": 1,
        "no_decision_change": 1,
        "none": 0,
    }
    return mapping.get(effect, 0)


def _parse_week_fraction(raw_week: str) -> tuple[int | None, int | None]:
    txt = str(raw_week or "").strip()
    if "/" not in txt:
        return None, None
    left, right = txt.split("/", 1)
    return _to_int(left), _to_int(right)


def _expected_fatigue(plan_ctx: Mapping[str, Any]) -> str:
    override = str(plan_ctx.get("expected_fatigue_level") or "").strip().lower()
    if override in {"low", "moderate", "high"}:
        return override

    phase = str(plan_ctx.get("phase") or "").strip().upper()
    if "DELOAD" in phase or bool(plan_ctx.get("is_deload")) or bool(plan_ctx.get("deload")):
        return "low"

    days_to_deload = _to_int(plan_ctx.get("days_to_deload"))
    if days_to_deload is not None:
        if days_to_deload <= 2:
            return "high"
        return "moderate"

    week_cur, week_total = _parse_week_fraction(str(plan_ctx.get("week") or ""))
    if week_cur is not None and week_total is not None:
        if week_total >= 4 and week_cur >= (week_total - 1):
            return "high"
        if week_cur <= 2:
            return "low"
        return "moderate"

    return "moderate"


def _cardio_debt_state(history_ctx: Mapping[str, Any], rules_ctx: Mapping[str, Any]) -> dict[str, Any]:
    weeks = _to_int(rules_ctx.get("cardio_weeks_window"))
    if weeks is None:
        weeks = 3
    planned_raw = _to_int(history_ctx.get("planned_runs_last_3_weeks"))
    performed_raw = _to_int(history_ctx.get("performed_runs_last_3_weeks"))
    days_since_last = _to_int(history_ctx.get("days_since_last_run"))
    planned = planned_raw if planned_raw is not None else 0
    performed = performed_raw if performed_raw is not None else 0

    min_days_since = _to_int(rules_ctx.get("cardio_debt_min_days_since_last_run"))
    if min_days_since is None:
        min_days_since = 10
    min_skip_rate = _to_float(rules_ctx.get("cardio_debt_min_skip_rate"))
    if min_skip_rate is None:
        min_skip_rate = 0.7

    performed_ratio: float | None = None
    if planned_raw is not None and planned_raw > 0:
        performed_ratio = max(0.0, min(1.0, performed / float(planned_raw)))
    skip_rate: float | None = None
    if performed_ratio is not None:
        skip_rate = round(max(0.0, min(1.0, 1.0 - performed_ratio)), 3)

    debt_days_gate = bool(days_since_last is not None and days_since_last >= min_days_since)
    debt_run_pattern = bool(
        (performed_raw is not None and performed_raw == 0)
        or (skip_rate is not None and skip_rate >= min_skip_rate and planned_raw is not None and planned_raw > 0)
    )
    debt = bool(debt_days_gate and debt_run_pattern)
    skipped = max(0, planned - performed) if planned_raw is not None else None

    return {
        "weeks_window": weeks,
        "planned_runs": planned_raw,
        "performed_runs": performed_raw,
        "days_since_last_run": days_since_last,
        "performed_ratio": None if performed_ratio is None else round(performed_ratio, 3),
        "skip_rate": skip_rate,
        "skipped_runs": skipped,
        "debt_by_run_pattern": debt_run_pattern,
        "debt_by_days_since_last_run": debt_days_gate,
        "cardio_debt": debt,
    }


def _primary_and_contributors(checks: list[dict[str, Any]]) -> tuple[str, list[str], str]:
    passed = [c for c in checks if c.get("passed") is True]
    if not passed:
        return "PLAN_KIND", [], ""
    passed_sorted = sorted(
        passed,
        key=lambda c: (
            -_effect_rank(str(c.get("effect") or "none")),
            EXPLAIN_RULE_ORDER.index(str(c.get("rule"))) if str(c.get("rule")) in EXPLAIN_RULE_ORDER else 999,
        ),
    )
    primary = passed_sorted[0]
    primary_rule = str(primary.get("rule") or "PLAN_KIND")
    primary_why = str(primary.get("why") or "")
    contributors = [str(c.get("rule")) for c in passed_sorted[1:] if str(c.get("rule")) != primary_rule]
    return primary_rule, list(dict.fromkeys(contributors)), primary_why


def decide_autopilot(
    day: date | str | None,
    plan_ctx: Mapping[str, Any] | None,
    signals_ctx: Mapping[str, Any] | None,
    history_ctx: Mapping[str, Any] | None,
    rules_ctx: Mapping[str, Any] | None,
    now: datetime | None = None,
) -> Decision:
    plan_ctx = plan_ctx or {}
    signals_ctx = signals_ctx or {}
    history_ctx = history_ctx or {}
    rules_ctx = rules_ctx or {}
    day_obj = _parse_day(day)

    phase = str(plan_ctx.get("phase") or "unknown").strip().upper()
    week = str(plan_ctx.get("week") or "?/?")
    days_to_deload = _to_int(plan_ctx.get("days_to_deload"))
    planned_kind = str(plan_ctx.get("planned_session_kind") or "train").strip().lower()
    planned_run_kind = str(plan_ctx.get("planned_run_kind") or "").strip().lower()
    chosen_session = plan_ctx.get("chosen_session")
    if chosen_session is not None:
        chosen_session = str(chosen_session)

    planned_topset = bool(plan_ctx.get("planned_topset_mainlift"))
    pending_gym = bool(plan_ctx.get("pending_gym_in_week"))
    pending_gym_name = plan_ctx.get("pending_gym_session_name")
    today_gym_planned = bool(plan_ctx.get("today_gym_planned"))
    should_use_slot_for_gym_today = bool(plan_ctx.get("should_use_slot_for_gym_today"))

    rhr_state = _derive_rhr_state(signals_ctx, rules_ctx)
    hrv_state = _derive_hrv_state(signals_ctx, rules_ctx)
    sickness_info = _sickness_window_info(signals_ctx, day_obj, now=now)
    active_sick = bool(sickness_info["active_24h"])
    sick_severe = bool(signals_ctx.get("sick_severe"))
    subjective_low = bool(signals_ctx.get("subjective_low_motivation"))

    late_logging = bool(history_ctx.get("late_logging") or history_ctx.get("last_session_missing"))
    manual_override = bool(history_ctx.get("manual_override_forced_train"))
    session_reorder_only = bool(history_ctx.get("session_reorder_only"))

    recent = _recent_training(day_obj, history_ctx, now=now)
    recency_guard = bool(recent["hard_last_36h"]) and not bool(history_ctx.get("ignore_recency"))
    missing_data = hrv_state == "missing" or rhr_state == "unknown"

    rhr_high = rhr_state in {"high", "extreme"}
    rhr_extreme = rhr_state == "extreme"
    hrv_down = hrv_state == "down"

    observed_fatigue = "low"
    if rhr_extreme and hrv_down:
        observed_fatigue = "extreme"
    elif rhr_high and hrv_down:
        observed_fatigue = "high"
    elif rhr_high:
        observed_fatigue = "moderate"

    overreach_expected = phase in {"OVERREACH"} or (days_to_deload is not None and days_to_deload <= 1)
    deload_phase = phase == "DELOAD"

    expected_fatigue = _expected_fatigue(plan_ctx)
    overreach_expected = (expected_fatigue == "high") and (not deload_phase)

    fit_order = {"low": 0, "moderate": 1, "high": 2, "extreme": 3}
    matches_plan = fit_order.get(observed_fatigue, 0) <= fit_order.get(expected_fatigue, 1)

    plan_is_rest = planned_kind in {"rest", "off"}
    deload_imminent = days_to_deload is not None and days_to_deload <= 1
    allow_z2_on_plan_rest = bool(rules_ctx.get("allow_z2_on_plan_rest", False))

    need_gym_priority_swap = planned_kind == "run" and pending_gym
    if need_gym_priority_swap and isinstance(pending_gym_name, str) and pending_gym_name.strip():
        chosen_session = pending_gym_name.strip()

    fatigue_safety_trigger = bool(
        ((rhr_extreme or (rhr_high and hrv_down)) and expected_fatigue != "high")
    )
    moderate_mismatch_low_expected = bool(
        observed_fatigue == "moderate" and (not matches_plan) and expected_fatigue == "low"
    )
    high_actionable = bool(
        observed_fatigue == "high" and ((not matches_plan) or expected_fatigue == "low")
    )

    checks = [
        DecisionCheck(
            rule="PLAN_KIND",
            passed=plan_is_rest,
            effect="force_rest_or_off" if plan_is_rest else "none",
            why=f"planned={planned_kind}",
        ),
        DecisionCheck(
            rule="SICKNESS_WINDOW",
            passed=active_sick,
            effect="prefer_light" if active_sick else "none",
            why="sick marker <=24h" if active_sick else "no active sick marker",
        ),
        DecisionCheck(
            rule="SICKNESS_SEVERITY",
            passed=bool(active_sick and (rhr_extreme or sick_severe)),
            effect="force_rest" if active_sick and (rhr_extreme or sick_severe) else "none",
            why="sick + very high pulse" if active_sick and (rhr_extreme or sick_severe) else "no severe sickness signal",
        ),
        DecisionCheck(
            rule="PLAN_FIT",
            passed=matches_plan,
            effect="none" if matches_plan else "prefer_light",
            why=f"observed={observed_fatigue}, expected={expected_fatigue}",
        ),
        DecisionCheck(
            rule="FATIGUE_STATE",
            passed=observed_fatigue in {"moderate", "high", "extreme"},
            effect=(
                "prefer_rest_or_light"
                if observed_fatigue == "extreme"
                else "prefer_light"
                if (high_actionable or moderate_mismatch_low_expected)
                else "note_only"
                if observed_fatigue in {"moderate", "high"}
                else "none"
            ),
            why=f"fatigue={observed_fatigue}",
        ),
        DecisionCheck(
            rule="DELOAD_TIMING",
            passed=deload_imminent,
            effect="note_only" if deload_imminent else "none",
            why=f"days_to_deload={days_to_deload}" if deload_imminent else "not near deload",
        ),
        DecisionCheck(
            rule="GYM_PRIORITY",
            passed=need_gym_priority_swap,
            effect="note_only" if need_gym_priority_swap else "none",
            why="run replaced by pending gym" if need_gym_priority_swap else "no run/gym conflict",
        ),
        DecisionCheck(
            rule="RECENCY",
            passed=recency_guard,
            effect="prefer_light_or_rest" if (recency_guard and not matches_plan) else "note_only" if recency_guard else "none",
            why="hard session in last 36h" if recency_guard else "spacing ok",
        ),
        DecisionCheck(
            rule="MISSING_DATA",
            passed=missing_data,
            effect="lower_confidence" if missing_data else "none",
            why="some recovery data missing" if missing_data else "data complete",
        ),
        DecisionCheck(
            rule="LATE_LOGGING",
            passed=late_logging,
            effect="note_only" if late_logging else "none",
            why="late/missing log ignored" if late_logging else "logs timely",
        ),
        DecisionCheck(
            rule="MANUAL_OVERRIDE_PRESENT",
            passed=manual_override,
            effect="note_only" if manual_override else "none",
            why="manual override exists" if manual_override else "no manual override",
        ),
        DecisionCheck(
            rule="SESSION_REORDER_ONLY",
            passed=session_reorder_only,
            effect="no_decision_change" if session_reorder_only else "none",
            why="session reordered in week" if session_reorder_only else "no reorder",
        ),
        DecisionCheck(
            rule="SUBJECTIVE_IGNORED",
            passed=subjective_low and observed_fatigue == "low" and not active_sick,
            effect="note_only" if subjective_low and observed_fatigue == "low" and not active_sick else "none",
            why="motivation low but recovery ok" if subjective_low and observed_fatigue == "low" and not active_sick else "subjective neutral",
        ),
        DecisionCheck(
            rule="CARDIO_DEBT",
            passed=False,
            effect="none",
            why="cardio debt not evaluated",
        ),
        DecisionCheck(
            rule="RECOVERY_GOOD",
            passed=False,
            effect="none",
            why="recovery gate not evaluated",
        ),
        DecisionCheck(
            rule="NO_GYM_CONFLICT",
            passed=False,
            effect="none",
            why="gym gate not evaluated",
        ),
        DecisionCheck(
            rule="PLAN_REST_Z2_ALLOWED",
            passed=False,
            effect="none",
            why="plan-rest gate not evaluated",
        ),
        DecisionCheck(
            rule="CARDIO_REINTRO",
            passed=False,
            effect="none",
            why="reintro not applied",
        ),
    ]

    mode = "normal"
    if deload_phase:
        mode = "deload"
    elif overreach_expected:
        mode = "overreach"

    default_plan_kind = "train"
    if planned_kind == "off":
        default_plan_kind = "off"
    elif planned_kind == "rest":
        default_plan_kind = "rest"

    checks_dict = [asdict(c) for c in checks]
    allowed_effects = {
        "force_rest",
        "force_rest_or_off",
        "prefer_rest_or_light",
        "prefer_light",
        "prefer_light_or_rest",
        "override_rest_with_z2",
    }
    changing = [
        c
        for c in checks_dict
        if c.get("passed") is True and str(c.get("effect") or "none") in allowed_effects
    ]
    changing_sorted = sorted(
        changing,
        key=lambda c: (
            -_effect_rank(str(c.get("effect") or "none")),
            EXPLAIN_RULE_ORDER.index(str(c.get("rule"))) if str(c.get("rule")) in EXPLAIN_RULE_ORDER else 999,
        ),
    )
    applied = changing_sorted[0] if changing_sorted else None
    applied_effect = str((applied or {}).get("effect") or "none")
    applied_rule = str((applied or {}).get("rule") or "")

    final_kind = default_plan_kind
    final_kind_source = "default:plan"
    if applied is not None:
        if applied_effect == "force_rest":
            final_kind = "rest"
        elif applied_effect == "force_rest_or_off":
            final_kind = "off" if planned_kind == "off" else "rest"
        elif applied_effect in {"prefer_rest_or_light", "prefer_light", "prefer_light_or_rest"}:
            final_kind = "light"
        elif applied_effect == "override_rest_with_z2":
            final_kind = "light"
        final_kind_source = f"rule:{applied_rule}"

    # Deload is already a built-in light week; avoid redundant "light day" labeling.
    if deload_phase and final_kind == "light":
        final_kind = "train"
        if applied_effect in {"prefer_light", "prefer_light_or_rest"}:
            applied_effect = "none"
            final_kind_source = "default:plan"

    if final_kind in {"rest", "off"}:
        mode = "recovery"

    adjustments = _no_adjustments()
    if final_kind == "light":
        adjustments = _standard_light_adjustments(
            extreme=(observed_fatigue in {"high", "extreme"}),
            planned_topset=planned_topset,
        )
        if mode not in {"deload", "overreach"}:
            mode = "recovery"
    elif mode == "deload" and final_kind == "train":
        adjustments = _deload_adjustments(planned_topset=planned_topset)

    # Cardio re-intro (Z2 instead of rest/off), only on top of an existing rest/off outcome.
    base_final_kind = final_kind
    cardio_state = _cardio_debt_state(history_ctx, rules_ctx)
    cardio_debt = bool(cardio_state["cardio_debt"])
    recovery_data_present = rhr_state != "unknown" and hrv_state != "missing"
    recovery_good = (
        (not active_sick)
        and recovery_data_present
        and (rhr_state == "normal")
        and (hrv_state in {"stable", "up"})
        and (observed_fatigue in {"low", "moderate"})
    )
    no_gym_conflict = (not today_gym_planned) and (not should_use_slot_for_gym_today) and (not need_gym_priority_swap)
    plan_rest_gate_ok = (planned_kind not in {"rest", "off"}) or allow_z2_on_plan_rest
    can_reintro = (
        base_final_kind in {"rest", "off"}
        and cardio_debt
        and recovery_good
        and no_gym_conflict
        and plan_rest_gate_ok
    )
    threshold_debt_downgrade = bool(
        planned_kind == "run"
        and planned_run_kind == "threshold"
        and cardio_debt
        and (not active_sick)
        and (not sick_severe)
        and (rhr_state in {"normal", "high"})
        and (hrv_state in {"stable", "up"})
        and (not today_gym_planned)
    )
    z2_duration = _to_int(rules_ctx.get("cardio_reintro_duration_min"))
    if z2_duration is None:
        z2_duration = 30
    z2_duration = max(20, min(40, z2_duration))

    # mutate the placeholder checks deterministically
    def _set_check(rule: str, passed: bool, effect: str, why: str) -> None:
        for idx, chk in enumerate(checks):
            if chk.rule == rule:
                checks[idx] = DecisionCheck(rule=rule, passed=passed, effect=effect, why=why)
                return

    _set_check(
        "CARDIO_DEBT",
        cardio_debt,
        "prefer_light" if cardio_debt else "none",
        (
            f"last_run={cardio_state['days_since_last_run']}d, skipped={cardio_state['skipped_runs']}/{cardio_state['planned_runs']}"
            if cardio_state.get("planned_runs") not in {None, 0}
            else f"last_run={cardio_state['days_since_last_run']}d"
        ),
    )
    _set_check(
        "RECOVERY_GOOD",
        bool(recovery_good and cardio_debt and base_final_kind in {"rest", "off"}),
        "note_only" if (recovery_good and cardio_debt and base_final_kind in {"rest", "off"}) else "none",
        "recovery suitable for easy run" if recovery_good else "recovery not suitable",
    )
    _set_check(
        "NO_GYM_CONFLICT",
        bool(no_gym_conflict and cardio_debt and base_final_kind in {"rest", "off"}),
        "note_only" if (no_gym_conflict and cardio_debt and base_final_kind in {"rest", "off"}) else "none",
        "no gym conflict today" if no_gym_conflict else "gym has priority",
    )
    _set_check(
        "PLAN_REST_Z2_ALLOWED",
        bool(plan_rest_gate_ok and cardio_debt and base_final_kind in {"rest", "off"}),
        "note_only" if (plan_rest_gate_ok and cardio_debt and base_final_kind in {"rest", "off"}) else "none",
        "plan-rest override enabled" if plan_rest_gate_ok else "plan-rest override disabled",
    )

    if can_reintro:
        final_kind = "light"
        mode = "recovery"
        chosen_session = "Z2 Run"
        applied_effect = "override_rest_with_z2"
        final_kind_source = "rule:CARDIO_REINTRO"
        adjustments = {
            **_no_adjustments(),
            "cardio_reintro": {
                "enabled": True,
                "run_type": "z2",
                "duration_min": z2_duration,
                "note": "locker, nasal breathing / easy talk-test",
                "history_kind": "run_z2",
            },
        }
        _set_check(
            "CARDIO_REINTRO",
            True,
            "override_rest_with_z2",
            "runs skipped recently + recovery good",
        )
    elif threshold_debt_downgrade:
        final_kind = "train"
        mode = "normal"
        chosen_session = "Run Z2"
        applied_effect = "override_rest_with_z2"
        final_kind_source = "rule:CARDIO_REINTRO"
        adjustments = {
            **_no_adjustments(),
            "cardio_reintro": {
                "enabled": True,
                "run_type": "z2",
                "duration_min": z2_duration,
                "note": "locker, nasal breathing / easy talk-test",
                "history_kind": "run_z2",
            },
        }
        _set_check(
            "CARDIO_REINTRO",
            True,
            "override_rest_with_z2",
            "threshold downgraded to z2 (cardio debt)",
        )
    else:
        adjustments = {**adjustments, "cardio_reintro": {"enabled": False}}

    confidence = "high"
    if missing_data:
        confidence = "medium"
    if missing_data and final_kind == "train":
        confidence = "low"
    if final_kind in {"rest", "off"}:
        confidence = "high"

    checks_dict = [asdict(c) for c in checks]
    primary_reason, contributors, primary_why = _primary_and_contributors(checks_dict)
    plan_fit_chk = next((c for c in checks_dict if c.get("rule") == "PLAN_FIT"), None)
    plan_fit_passed = bool(plan_fit_chk and plan_fit_chk.get("passed") is True)
    if applied_effect == "none":
        if final_kind == "train" and plan_fit_passed:
            primary_reason = "PLAN_FIT"
            primary_why = str(plan_fit_chk.get("why") or "")
            contributors = []
            for c in checks_dict:
                if c.get("passed") is True and c.get("rule") not in {"PLAN_FIT", "PLAN_KIND"}:
                    contributors.append(str(c.get("rule")))
            contributors = list(dict.fromkeys(contributors))
        elif final_kind in {"rest", "off"} and plan_is_rest:
            primary_reason = "PLAN_KIND"
            primary_why = "planned=" + planned_kind
            contributors = []
    elif final_kind == "train" and plan_fit_passed:
        old_primary = primary_reason
        primary_reason = "PLAN_FIT"
        primary_why = str(plan_fit_chk.get("why") or "")
        merged = []
        if old_primary and old_primary != primary_reason:
            merged.append(old_primary)
        merged.extend(contributors or [])
        contributors = list(dict.fromkeys([c for c in merged if c and c != primary_reason]))
    elif final_kind == "train" and mode == "overreach":
        old_primary = primary_reason
        primary_reason = "OVERREACH_EXPECTED"
        primary_why = "overreach planned"
        merged = []
        if old_primary and old_primary != primary_reason:
            merged.append(old_primary)
        merged.extend(contributors or [])
        contributors = list(dict.fromkeys([c for c in merged if c and c != primary_reason]))

    user_one_liner = _compose_one_liner(
        final_kind,
        mode,
        plan_is_rest=plan_is_rest,
        overreach_expected=overreach_expected,
        deload_phase=deload_phase,
    )
    if can_reintro or threshold_debt_downgrade:
        user_one_liner = "Reintro: Z2 statt Pause (Runs lange her, Daten gut)."

    explain = {
        "inputs": {
            "phase": phase,
            "week": week,
            "days_to_deload": days_to_deload,
            "sick": bool(signals_ctx.get("sick")),
            "sick_active_24h": active_sick,
            "sick_ts": sickness_info.get("sick_ts"),
            "sick_hours_ago": sickness_info.get("sick_hours_ago"),
            "hours_since_sick": sickness_info.get("hours_since_sick"),
            "sick_window_unknown": bool(sickness_info.get("sick_window_unknown")),
            "rhr_state": rhr_state,
            "hrv_state": hrv_state,
            "expected_fatigue": expected_fatigue,
            "observed_fatigue": observed_fatigue,
            "plan_fit": matches_plan,
            "plan_ctx_partial": bool(plan_ctx.get("plan_ctx_partial")),
            "recent_training": recent,
            "metrics": history_ctx.get("metrics") if isinstance(history_ctx.get("metrics"), Mapping) else {},
            "cardio_debt": cardio_state,
        },
        "checks": checks_dict,
        "summary": {
            "primary": primary_reason,
            "primary_why": primary_why,
            "contributors": contributors,
        },
        "decision": {
            "final_kind": final_kind,
            "reason": primary_reason,
            "reason_chain": " + ".join([primary_reason, *contributors]) if contributors else primary_reason,
            "confidence": confidence,
            "applied_effect": applied_effect,
            "final_kind_source": final_kind_source,
        },
    }

    return Decision(
        final_kind=final_kind,
        chosen_session=chosen_session,
        mode=mode,
        adjustments=adjustments,
        user_one_liner=user_one_liner,
        applied_effect=applied_effect,
        final_kind_source=final_kind_source,
        explain=explain,
    )


def format_decision_summary(decision: Decision) -> str:
    summary = decision.explain.get("summary") or {}
    primary = str(summary.get("primary") or (decision.explain.get("decision") or {}).get("reason") or "PLAN_FIT")
    why = str(summary.get("primary_why") or "").strip()
    contributors = summary.get("contributors") or []

    line = f"{decision.final_kind.upper()} because: {primary}"
    if why:
        line += f" ({why})"
    if contributors:
        line += " | Contributors: " + ", ".join(str(x) for x in contributors)
    return line
