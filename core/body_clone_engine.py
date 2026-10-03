from __future__ import annotations

from datetime import date, timedelta
from typing import Any


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return int(default)


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _avg(items: list[float], fallback: float = 0.0) -> float:
    vals = [float(x) for x in items if isinstance(x, (int, float))]
    if not vals:
        return float(fallback)
    return float(sum(vals) / len(vals))


def build_clone_profile(
    *,
    state: dict[str, Any],
    history_rows: list[dict[str, Any]],
    latest_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    history_rows = [row for row in (history_rows or []) if isinstance(row, dict)]

    outcomes_by_mode: dict[str, list[float]] = {"HEAVY": [], "NORMAL": [], "LIGHT": [], "REST": []}
    for row in history_rows:
        mode = str(row.get("mode") or "").upper()
        if mode not in outcomes_by_mode:
            continue
        outcomes_by_mode[mode].append(_safe_float(row.get("score"), 0.5))

    heavy_mean = _avg(outcomes_by_mode["HEAVY"], 0.55)
    normal_mean = _avg(outcomes_by_mode["NORMAL"], 0.58)
    light_mean = _avg(outcomes_by_mode["LIGHT"], 0.6)

    readiness = state.get("readiness") if isinstance(state.get("readiness"), dict) else {}
    recovery = state.get("recovery") if isinstance(state.get("recovery"), dict) else {}
    nutrition = state.get("nutrition") if isinstance(state.get("nutrition"), dict) else {}
    run_ctx = state.get("run_context") if isinstance(state.get("run_context"), dict) else {}
    load = state.get("load") if isinstance(state.get("load"), dict) else {}

    sleep_sensitivity = _clamp(0.36 + _safe_float(readiness.get("sleep_debt_score"), 0.0) * 0.5)
    hrv_sensitivity = _clamp(0.33 + _safe_float(recovery.get("hrv_pressure"), 0.0) * 0.52)
    nutrition_sensitivity = _clamp(0.32 + _safe_float(nutrition.get("deficit_risk"), 0.0) * 0.56)
    run_interference_sensitivity = _clamp(0.3 + _safe_float(run_ctx.get("interference_risk"), 0.0) * 0.58)
    back_to_back_tolerance = _clamp(0.56 + (heavy_mean - 0.5) * 0.3 - _safe_float(load.get("back_to_back_risk"), 0.0) * 0.42)

    previous_sensitivities = latest_snapshot.get("sensitivities") if isinstance(latest_snapshot, dict) else None
    if isinstance(previous_sensitivities, dict):
        # Keep the clone stateful and smooth over days.
        sleep_sensitivity = _clamp((sleep_sensitivity * 0.34) + (_safe_float(previous_sensitivities.get("sleep_sensitivity"), sleep_sensitivity) * 0.66))
        hrv_sensitivity = _clamp((hrv_sensitivity * 0.34) + (_safe_float(previous_sensitivities.get("hrv_sensitivity"), hrv_sensitivity) * 0.66))
        nutrition_sensitivity = _clamp((nutrition_sensitivity * 0.34) + (_safe_float(previous_sensitivities.get("nutrition_sensitivity"), nutrition_sensitivity) * 0.66))
        run_interference_sensitivity = _clamp(
            (run_interference_sensitivity * 0.34)
            + (_safe_float(previous_sensitivities.get("run_interference_sensitivity"), run_interference_sensitivity) * 0.66)
        )
        back_to_back_tolerance = _clamp(
            (back_to_back_tolerance * 0.34)
            + (_safe_float(previous_sensitivities.get("back_to_back_tolerance"), back_to_back_tolerance) * 0.66)
        )

    evidence_days = _safe_int(state.get("history_days"), 28)
    evidence_sessions = _safe_int(state.get("history_sessions"), len(history_rows))
    evidence_recovery_cycles = _safe_int(state.get("recovery_cycles"), max(1, evidence_days // 2))

    learned_patterns = [
        {
            "key": "mode_reaction_heavy",
            "label": "Heavy-Reaktion",
            "score": round(heavy_mean, 3),
            "note": "Wie oft Heavy sauber getragen wurde.",
        },
        {
            "key": "mode_reaction_normal",
            "label": "Normal-Reaktion",
            "score": round(normal_mean, 3),
            "note": "Wie stabil Normal als Arbeitstempo funktioniert.",
        },
        {
            "key": "mode_reaction_light",
            "label": "Light-Reaktion",
            "score": round(light_mean, 3),
            "note": "Wie gut Light Recovery schützt ohne Leistung komplett zu kappen.",
        },
    ]

    current_state = {
        "load_state": "elevated" if _safe_float(load.get("back_to_back_risk"), 0.0) >= 0.58 else "stable",
        "recovery_state": "strained" if _safe_float(recovery.get("hrv_pressure"), 0.0) >= 0.58 else "stable",
        "nutrition_state": "fragile" if _safe_float(nutrition.get("deficit_risk"), 0.0) >= 0.55 else "supported",
        "readiness_state": "low" if _safe_float(readiness.get("readiness_pressure"), 0.0) >= 0.58 else "workable",
    }

    trust_score = _clamp(0.42 + min(1.0, evidence_days / 90.0) * 0.34 + min(1.0, evidence_sessions / 60.0) * 0.18)
    summary_line = (
        "Clone sieht solide Belastbarkeit, aber erhöhte Recovery-Kosten."
        if current_state["recovery_state"] == "strained"
        else "Clone sieht aktuell stabile Belastbarkeit."
    )

    return {
        "version": "clone-v1.0",
        "base_state": current_state,
        "sensitivities": {
            "sleep_sensitivity": round(sleep_sensitivity, 3),
            "hrv_sensitivity": round(hrv_sensitivity, 3),
            "nutrition_sensitivity": round(nutrition_sensitivity, 3),
            "run_interference_sensitivity": round(run_interference_sensitivity, 3),
            "back_to_back_tolerance": round(back_to_back_tolerance, 3),
        },
        "learned_patterns": learned_patterns,
        "summary": {
            "line": summary_line,
            "evidence_days": evidence_days,
            "evidence_sessions": evidence_sessions,
            "recovery_cycles": evidence_recovery_cycles,
            "trust_score": round(trust_score, 3),
        },
    }


def generate_scenarios(*, state: dict[str, Any]) -> list[dict[str, Any]]:
    nutrition = state.get("nutrition") if isinstance(state.get("nutrition"), dict) else {}
    run_ctx = state.get("run_context") if isinstance(state.get("run_context"), dict) else {}
    recovery = state.get("recovery") if isinstance(state.get("recovery"), dict) else {}

    base = [
        {"scenario_key": "gym_heavy", "label": "HEAVY", "scenario_type": "gym", "gym_mode": "HEAVY", "run_plan": "NONE", "calorie_support": "normal"},
        {"scenario_key": "gym_normal", "label": "NORMAL", "scenario_type": "gym", "gym_mode": "NORMAL", "run_plan": "NONE", "calorie_support": "normal"},
        {"scenario_key": "gym_light", "label": "LIGHT", "scenario_type": "gym", "gym_mode": "LIGHT", "run_plan": "NONE", "calorie_support": "normal"},
        {"scenario_key": "rest", "label": "REST", "scenario_type": "rest", "gym_mode": "REST", "run_plan": "NONE", "calorie_support": "normal"},
    ]

    if _safe_float(run_ctx.get("interference_risk"), 0.0) <= 0.78:
        base.append({"scenario_key": "heavy_plus_run", "label": "HEAVY + RUN", "scenario_type": "combo", "gym_mode": "HEAVY", "run_plan": "EASY", "calorie_support": "normal"})
        base.append({"scenario_key": "normal_plus_run", "label": "NORMAL + RUN", "scenario_type": "combo", "gym_mode": "NORMAL", "run_plan": "EASY", "calorie_support": "normal"})

    if _safe_float(nutrition.get("deficit_risk"), 0.0) >= 0.34:
        base.append(
            {
                "scenario_key": "heavy_with_calorie_support",
                "label": "HEAVY + KCAL SUPPORT",
                "scenario_type": "nutrition_combo",
                "gym_mode": "HEAVY",
                "run_plan": "NONE",
                "calorie_support": "high",
            }
        )
    if _safe_float(recovery.get("hrv_pressure"), 0.0) >= 0.42:
        base.append(
            {
                "scenario_key": "normal_with_low_recovery",
                "label": "NORMAL bei Recovery-Druck",
                "scenario_type": "recovery_guard",
                "gym_mode": "NORMAL",
                "run_plan": "NONE",
                "calorie_support": "normal",
            }
        )
    return base


def _scenario_base_load(gym_mode: str) -> float:
    mode = str(gym_mode or "").upper()
    if mode == "HEAVY":
        return 0.66
    if mode == "NORMAL":
        return 0.45
    if mode == "LIGHT":
        return 0.26
    return 0.08


def simulate_scenario(
    *,
    scenario: dict[str, Any],
    state: dict[str, Any],
    clone_profile: dict[str, Any],
) -> dict[str, Any]:
    sensitivities = clone_profile.get("sensitivities") if isinstance(clone_profile.get("sensitivities"), dict) else {}
    readiness = state.get("readiness") if isinstance(state.get("readiness"), dict) else {}
    recovery = state.get("recovery") if isinstance(state.get("recovery"), dict) else {}
    nutrition = state.get("nutrition") if isinstance(state.get("nutrition"), dict) else {}
    load = state.get("load") if isinstance(state.get("load"), dict) else {}
    run_ctx = state.get("run_context") if isinstance(state.get("run_context"), dict) else {}
    stress = state.get("stress") if isinstance(state.get("stress"), dict) else {}

    gym_mode = str(scenario.get("gym_mode") or "NORMAL").upper()
    run_plan = str(scenario.get("run_plan") or "NONE").upper()
    calorie_support = str(scenario.get("calorie_support") or "normal").lower()

    sleep_pressure = _safe_float(readiness.get("sleep_debt_score"), 0.0) * _safe_float(sensitivities.get("sleep_sensitivity"), 0.4)
    hrv_pressure = _safe_float(recovery.get("hrv_pressure"), 0.0) * _safe_float(sensitivities.get("hrv_sensitivity"), 0.4)
    nutrition_pressure = _safe_float(nutrition.get("deficit_risk"), 0.0) * _safe_float(sensitivities.get("nutrition_sensitivity"), 0.4)
    load_pressure = _safe_float(load.get("back_to_back_risk"), 0.0) * (1.0 - _safe_float(sensitivities.get("back_to_back_tolerance"), 0.5))
    run_pressure = 0.0
    if run_plan in {"EASY", "QUALITY", "LONG"}:
        run_pressure = _safe_float(run_ctx.get("interference_risk"), 0.0) * _safe_float(sensitivities.get("run_interference_sensitivity"), 0.4)
        if run_plan == "QUALITY":
            run_pressure += 0.1
        if run_plan == "LONG":
            run_pressure += 0.08

    stress_pressure = _safe_float(stress.get("daily_stress"), 0.0) * 0.25
    base_load = _scenario_base_load(gym_mode)
    calorie_bonus = 0.0
    if calorie_support == "high":
        calorie_bonus = 0.1
    elif calorie_support == "low":
        calorie_bonus = -0.06

    strain = _clamp(base_load + sleep_pressure + hrv_pressure + nutrition_pressure + load_pressure + run_pressure + stress_pressure - calorie_bonus)

    readiness_headroom = _clamp(1.0 - _safe_float(readiness.get("readiness_pressure"), 0.0))
    performance_good = _clamp(0.74 - (strain * 0.56) + (readiness_headroom * 0.24))
    performance_neutral = _clamp(0.18 + (0.22 * (1.0 - abs(0.52 - performance_good))))
    if performance_good + performance_neutral > 0.95:
        performance_neutral = max(0.05, 0.95 - performance_good)
    performance_bad = _clamp(1.0 - performance_good - performance_neutral)

    recovery_risk = _clamp((strain * 0.72) + (0.08 if gym_mode == "HEAVY" else 0.0))
    fatigue_risk = _clamp((strain * 0.76) + (0.05 if run_plan in {"QUALITY", "LONG"} else 0.0))
    hrv_drop_risk = _clamp((hrv_pressure * 1.08) + (0.18 if gym_mode == "HEAVY" and run_plan in {"QUALITY", "LONG"} else 0.0))

    readiness_delta = round((performance_good * 0.14) - (fatigue_risk * 0.22) - (recovery_risk * 0.2), 3)

    drivers_raw = [
        ("Schlafdruck", sleep_pressure),
        ("HRV/RHR-Druck", hrv_pressure),
        ("Ernährungsdruck", nutrition_pressure),
        ("Back-to-back Last", load_pressure),
        ("Run-Interferenz", run_pressure),
        ("Alltagsstress", stress_pressure),
    ]
    top_drivers = [
        {"label": name, "impact": round(val, 3)}
        for name, val in sorted(drivers_raw, key=lambda row: row[1], reverse=True)[:3]
        if val > 0.03
    ]

    score = round((performance_good * 100.0) - (recovery_risk * 28.0) - (fatigue_risk * 24.0) - (hrv_drop_risk * 18.0) + (readiness_delta * 34.0), 2)
    summary = (
        "Hohe Performance-Chance bei kontrollierbarem Risiko."
        if performance_good >= 0.6 and recovery_risk <= 0.45
        else "Machbar, aber mit spuerbaren Recovery-Kosten."
        if performance_good >= 0.45
        else "Erhoehtes Risiko fuer schwache Session oder Folgekosten."
    )

    return {
        "scenario_key": str(scenario.get("scenario_key") or ""),
        "label": str(scenario.get("label") or scenario.get("scenario_key") or ""),
        "scenario_type": str(scenario.get("scenario_type") or "gym"),
        "gym_mode": gym_mode,
        "run_plan": run_plan,
        "calorie_support": calorie_support,
        "performance_probability_good": round(performance_good, 3),
        "performance_probability_neutral": round(performance_neutral, 3),
        "performance_probability_bad": round(performance_bad, 3),
        "recovery_risk": round(recovery_risk, 3),
        "fatigue_risk": round(fatigue_risk, 3),
        "hrv_drop_risk": round(hrv_drop_risk, 3),
        "readiness_tomorrow_delta_estimate": readiness_delta,
        "summary": summary,
        "top_drivers": top_drivers,
        "score": score,
    }


def build_history_rows_from_decisions(decisions: list[dict[str, Any]], reviews: dict[int, dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in decisions:
        if not isinstance(row, dict):
            continue
        decision_id = _safe_int(row.get("id"), 0)
        mode = str(row.get("mode") or "").upper()
        if mode not in {"HEAVY", "NORMAL", "LIGHT", "REST"}:
            continue
        review = reviews.get(decision_id) or {}
        outcome_score = _safe_float(review.get("outcome_score"), 0.5)
        out.append(
            {
                "day_iso": str(row.get("day_iso") or ""),
                "mode": mode,
                "score": outcome_score,
                "outcome_label": str(review.get("outcome_label") or "inconclusive"),
            }
        )
    return out


def rolling_window_range(day_iso: str, days: int = 84) -> tuple[str, str]:
    end_day = date.fromisoformat(day_iso)
    start_day = end_day - timedelta(days=max(7, int(days)))
    return (start_day.isoformat(), end_day.isoformat())
