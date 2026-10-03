from __future__ import annotations

from dataclasses import dataclass
from typing import Any


MODE_HEAVY = "HEAVY"
MODE_NORMAL = "NORMAL"
MODE_LIGHT = "LIGHT"
MODE_REST = "REST"


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def _safe_bool(value: Any) -> bool:
    return bool(value)


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


@dataclass(frozen=True)
class Objection:
    code: str
    label: str
    severity: str
    weighted_score: float
    raw_signal: float
    weight: float
    detail: str


@dataclass(frozen=True)
class LayerComponent:
    code: str
    label: str
    signal: float
    weight_key: str
    parameter_weight: float
    detail: str


def _hard_stop_hits(state: dict[str, Any]) -> list[dict[str, str]]:
    illness = state.get("illness") if isinstance(state.get("illness"), dict) else {}
    recovery = state.get("recovery") if isinstance(state.get("recovery"), dict) else {}
    fatigue = state.get("fatigue") if isinstance(state.get("fatigue"), dict) else {}
    readiness = state.get("readiness") if isinstance(state.get("readiness"), dict) else {}

    hits: list[dict[str, str]] = []
    if _safe_bool(illness.get("sick")):
        hits.append({"code": "SICK", "label": "Krankheitsflag aktiv", "severity": "hard_stop"})
    if _safe_bool(illness.get("infection_acute")):
        hits.append({"code": "INFECTION_ACUTE", "label": "Akute Infektlage", "severity": "hard_stop"})
    if _safe_bool(recovery.get("missing_extreme")):
        hits.append({"code": "RECOVERY_MISSING_EXTREME", "label": "Recovery-Daten extrem unzureichend", "severity": "hard_stop"})
    if _safe_bool(fatigue.get("systemic_extreme")):
        hits.append({"code": "SYSTEMIC_FATIGUE_EXTREME", "label": "Systemische Erschöpfung", "severity": "hard_stop"})
    if _safe_float(readiness.get("trainability_score"), 1.0) <= 0.12:
        hits.append({"code": "NOT_TRAINABLE", "label": "Heute klar nicht trainierbar", "severity": "hard_stop"})
    return hits


def _severity_from_score(score: float) -> str:
    if score >= 0.78:
        return "strong"
    if score >= 0.46:
        return "moderate"
    return "low"


def _scaled_weight(parameter_weight: float) -> float:
    # Linear und transparent: 0.04..0.40 -> 0.10..1.00
    clamped = _clamp(parameter_weight, 0.04, 0.4)
    return round(_clamp(clamped / 0.4, 0.1, 1.0), 3)


def _group_weights(
    *,
    keys: list[str],
    parameter_weights: dict[str, float],
    default_weights: dict[str, float],
) -> dict[str, float]:
    raw: dict[str, float] = {}
    for key in keys:
        fallback = _safe_float(default_weights.get(key), 0.12)
        raw[key] = _clamp(_safe_float(parameter_weights.get(key), fallback), 0.04, 0.4)
    total = sum(raw.values()) or 1.0
    return {key: raw[key] / total for key in keys}


def _calendar_friction_score(stress: dict[str, Any]) -> float:
    explicit = stress.get("calendar_friction_score")
    if explicit is not None:
        return _clamp(_safe_float(explicit, 0.0))

    daily_stress = _safe_float(stress.get("daily_stress"), 0.0)
    free_ratio = stress.get("free_ratio_today")
    free_score = _clamp(1.0 - _safe_float(free_ratio, 0.5)) if free_ratio is not None else daily_stress

    largest_window = _safe_float(stress.get("largest_free_window_min"), 0.0)
    if largest_window > 0:
        window_pressure = _clamp((90.0 - largest_window) / 90.0)
    else:
        window_pressure = 0.35 if stress.get("calendar_available") else 0.55
    return _clamp((free_score * 0.62) + (window_pressure * 0.23) + (daily_stress * 0.15))


def _data_quality_score(state: dict[str, Any]) -> float:
    recovery = state.get("recovery") if isinstance(state.get("recovery"), dict) else {}
    nutrition = state.get("nutrition") if isinstance(state.get("nutrition"), dict) else {}
    stress = state.get("stress") if isinstance(state.get("stress"), dict) else {}

    has_recovery_today = bool(recovery.get("rmssd_today") is not None or recovery.get("hr_today") is not None)
    has_recovery_window = bool(recovery.get("rmssd_7d") is not None or recovery.get("hr_7d") is not None)
    recovery_quality = 1.0 if has_recovery_today else (0.58 if has_recovery_window else 0.25)

    nutrition_cov = _clamp(_safe_float(nutrition.get("coverage_7d"), 0.0))
    nutrition_stability = _clamp(_safe_float(nutrition.get("stability_score"), 0.45))
    nutrition_quality = _clamp((nutrition_cov * 0.62) + (nutrition_stability * 0.38))

    calendar_quality = 1.0 if bool(stress.get("calendar_available")) else 0.6
    return _clamp((recovery_quality * 0.44) + (nutrition_quality * 0.36) + (calendar_quality * 0.20))


def _layer_scores(
    *,
    state: dict[str, Any],
    scenarios: list[dict[str, Any]],
    parameter_weights: dict[str, float],
) -> dict[str, Any]:
    recovery = state.get("recovery") if isinstance(state.get("recovery"), dict) else {}
    readiness = state.get("readiness") if isinstance(state.get("readiness"), dict) else {}
    fatigue = state.get("fatigue") if isinstance(state.get("fatigue"), dict) else {}
    nutrition = state.get("nutrition") if isinstance(state.get("nutrition"), dict) else {}
    load = state.get("load") if isinstance(state.get("load"), dict) else {}
    run_ctx = state.get("run_context") if isinstance(state.get("run_context"), dict) else {}
    stress = state.get("stress") if isinstance(state.get("stress"), dict) else {}

    scenario_by_key = {str(row.get("scenario_key") or ""): row for row in (scenarios or []) if isinstance(row, dict)}
    heavy = scenario_by_key.get("gym_heavy", {}) or next(
        (
            row
            for row in (scenarios or [])
            if isinstance(row, dict)
            and str(row.get("gym_mode") or "").upper() == MODE_HEAVY
            and str(row.get("run_plan") or "NONE").upper() == "NONE"
        ),
        {},
    )

    heavy_recovery_risk = _safe_float(heavy.get("recovery_risk"), 0.0)
    heavy_fatigue_risk = _safe_float(heavy.get("fatigue_risk"), 0.0)
    heavy_perf_bad = _safe_float(heavy.get("performance_probability_bad"), 0.0)
    scenario_followup_cost = _clamp((heavy_recovery_risk * 0.56) + (heavy_fatigue_risk * 0.44))
    simulation_risk = _clamp((scenario_followup_cost * 0.72) + (heavy_perf_bad * 0.28))

    energy_support = _clamp(
        _safe_float(
            nutrition.get("energy_support_score"),
            1.0 - _safe_float(nutrition.get("deficit_risk"), 0.25),
        )
    )
    nutrition_compliance = _clamp(_safe_float(nutrition.get("compliance_score"), _safe_float(nutrition.get("coverage_7d"), 0.0)))
    nutrition_coverage = _clamp(_safe_float(nutrition.get("coverage_7d"), 0.0))
    nutrition_capacity_pressure = _clamp(
        ((1.0 - energy_support) * 0.66)
        + ((1.0 - nutrition_compliance) * 0.20)
        + ((1.0 - nutrition_coverage) * 0.14)
    )

    trainability_gap = _clamp(1.0 - _safe_float(readiness.get("trainability_score"), 1.0))
    systemic_penalty = 1.0 if _safe_bool(fatigue.get("systemic_extreme")) else 0.0
    fatigue_capacity_pressure = _clamp(
        (_safe_float(fatigue.get("muscular_preload"), 0.0) * 0.62)
        + (systemic_penalty * 0.38)
        + (trainability_gap * 0.24)
    )

    capacity_weights = _group_weights(
        keys=["sleep", "hrv", "fatigue", "nutrition"],
        parameter_weights=parameter_weights,
        default_weights={"sleep": 0.14, "hrv": 0.16, "fatigue": 0.15, "nutrition": 0.14},
    )
    capacity_components = [
        LayerComponent(
            code="SLEEP_DEFICIT",
            label="Schlafdefizit",
            signal=_safe_float(readiness.get("sleep_debt_score"), 0.0),
            weight_key="sleep",
            parameter_weight=_safe_float(parameter_weights.get("sleep"), 0.14),
            detail="Schlaflage begrenzt die trainierbare Kapazität heute.",
        ),
        LayerComponent(
            code="HRV_DEPRESSED",
            label="Erholung gedrückt",
            signal=_safe_float(recovery.get("hrv_pressure"), 0.0),
            weight_key="hrv",
            parameter_weight=_safe_float(parameter_weights.get("hrv"), 0.16),
            detail="HRV/RHR spricht für begrenzte Kapazität.",
        ),
        LayerComponent(
            code="MUSCULAR_FATIGUE",
            label="Muskuläre/systemische Vorermüdung",
            signal=fatigue_capacity_pressure,
            weight_key="fatigue",
            parameter_weight=_safe_float(parameter_weights.get("fatigue"), 0.15),
            detail="Vorermüdung drückt die reproduzierbare Tageskapazität.",
        ),
        LayerComponent(
            code="NUTRITION_UNDERSUPPORTED",
            label="Energie-/Ernährungslage limitiert",
            signal=nutrition_capacity_pressure,
            weight_key="nutrition",
            parameter_weight=_safe_float(parameter_weights.get("nutrition"), 0.14),
            detail="Energieversorgung und Ernährungsstabilität begrenzen die Heavy-Kapazität.",
        ),
    ]
    capacity_pressure = _clamp(
        sum(_safe_float(capacity_weights.get(row.weight_key), 0.0) * _clamp(row.signal) for row in capacity_components)
    )
    capacity_today_score = _clamp(1.0 - capacity_pressure)

    cost_weights = _group_weights(
        keys=["back_to_back", "run_interference", "simulation", "fatigue"],
        parameter_weights=parameter_weights,
        default_weights={"back_to_back": 0.11, "run_interference": 0.12, "simulation": 0.09, "fatigue": 0.15},
    )
    recovery_cost_components = [
        LayerComponent(
            code="BACK_TO_BACK_LOAD",
            label="Back-to-back Last",
            signal=_safe_float(load.get("back_to_back_risk"), 0.0),
            weight_key="back_to_back",
            parameter_weight=_safe_float(parameter_weights.get("back_to_back"), 0.11),
            detail="Mehrere harte Tage erhöhen die Folgekosten deutlich.",
        ),
        LayerComponent(
            code="RUN_INTERFERENCE",
            label="Run-Interferenz",
            signal=_safe_float(run_ctx.get("interference_risk"), 0.0),
            weight_key="run_interference",
            parameter_weight=_safe_float(parameter_weights.get("run_interference"), 0.12),
            detail="Ausdauerlast erhöht die Kosten der Kraft-Entscheidung.",
        ),
        LayerComponent(
            code="SIM_HEAVY_RISK",
            label="Simulation/Folgetag-Risiko",
            signal=simulation_risk,
            weight_key="simulation",
            parameter_weight=_safe_float(parameter_weights.get("simulation"), 0.09),
            detail="Szenario-Risiken werden als Folgekosten-Layer eingebunden.",
        ),
        LayerComponent(
            code="HEAVY_RECOVERY_COST",
            label="Heavy-Folgekosten",
            signal=scenario_followup_cost,
            weight_key="fatigue",
            parameter_weight=_safe_float(parameter_weights.get("fatigue"), 0.15),
            detail="Erwartete Recovery-/Fatigue-Kosten am Folgetag.",
        ),
    ]
    recovery_cost_score = _clamp(
        sum(_safe_float(cost_weights.get(row.weight_key), 0.0) * _clamp(row.signal) for row in recovery_cost_components)
    )

    data_quality_score = _data_quality_score(state)
    data_quality_penalty = _clamp(1.0 - data_quality_score)
    calendar_friction = _calendar_friction_score(stress)
    daily_stress = _safe_float(stress.get("daily_stress"), 0.0)

    stress_weight = _clamp(_safe_float(parameter_weights.get("stress"), 0.09), 0.04, 0.4)
    calendar_weight = _clamp((stress_weight / 0.4) * 0.62, 0.32, 0.68)
    daily_stress_weight = _clamp((stress_weight / 0.4) * 0.24, 0.16, 0.34)
    data_quality_weight = _clamp(1.0 - calendar_weight - daily_stress_weight, 0.12, 0.34)
    w_sum = calendar_weight + daily_stress_weight + data_quality_weight
    calendar_weight /= w_sum
    daily_stress_weight /= w_sum
    data_quality_weight /= w_sum

    feasibility_risk = _clamp(
        (calendar_friction * calendar_weight)
        + (daily_stress * daily_stress_weight)
        + (data_quality_penalty * data_quality_weight)
    )
    feasibility_today_score = _clamp(1.0 - feasibility_risk)
    feasibility_components = [
        LayerComponent(
            code="CALENDAR_FRICTION",
            label="Kalenderfriktion",
            signal=calendar_friction,
            weight_key="stress",
            parameter_weight=_safe_float(parameter_weights.get("stress"), 0.09),
            detail="Tag ist organisatorisch schwerer sauber umsetzbar.",
        ),
        LayerComponent(
            code="STRESS_LOAD",
            label="Alltagsstress",
            signal=daily_stress,
            weight_key="stress",
            parameter_weight=_safe_float(parameter_weights.get("stress"), 0.09),
            detail="Alltagsdruck erhöht Reibung in der Tagesumsetzung.",
        ),
        LayerComponent(
            code="DATA_QUALITY_LOW",
            label="Datenqualität/Compliance",
            signal=data_quality_penalty,
            weight_key="nutrition",
            parameter_weight=_safe_float(parameter_weights.get("nutrition"), 0.14),
            detail="Niedrige Coverage reduziert die Sicherheit für aggressive Freigaben.",
        ),
    ]

    return {
        "capacity_today_score": round(capacity_today_score, 3),
        "recovery_cost_score": round(recovery_cost_score, 3),
        "feasibility_today_score": round(feasibility_today_score, 3),
        "decision_load": round(_clamp((1.0 - capacity_today_score) + recovery_cost_score + (1.0 - feasibility_today_score), 0.0, 3.0), 3),
        "data_quality_score": round(data_quality_score, 3),
        "capacity_components": capacity_components,
        "recovery_cost_components": recovery_cost_components,
        "feasibility_components": feasibility_components,
    }


def _build_objections_from_layers(*, layer_scores: dict[str, Any]) -> list[Objection]:
    all_components = (
        list(layer_scores.get("capacity_components") or [])
        + list(layer_scores.get("recovery_cost_components") or [])
        + list(layer_scores.get("feasibility_components") or [])
    )
    out: list[Objection] = []
    for row in all_components:
        if not isinstance(row, LayerComponent):
            continue
        signal = _clamp(row.signal)
        weight = _clamp(row.parameter_weight, 0.04, 0.4)
        weight_effect = _scaled_weight(weight)
        final_effect = round(_clamp(signal * weight_effect), 3)
        out.append(
            Objection(
                code=row.code,
                label=row.label,
                severity=_severity_from_score(final_effect),
                weighted_score=final_effect,
                raw_signal=round(signal, 3),
                weight=round(weight, 3),
                detail=row.detail,
            )
        )
    return out


def decide_heavy_first(
    *,
    state: dict[str, Any],
    scenarios: list[dict[str, Any]],
    parameter_weights: dict[str, float],
) -> dict[str, Any]:
    base_mode = MODE_HEAVY
    hard_stops = _hard_stop_hits(state)
    if hard_stops:
        confidence = 0.88 if any(hit.get("code") == "SICK" for hit in hard_stops) else 0.78
        reasons = [
            {
                "code": str(hit.get("code") or ""),
                "label": str(hit.get("label") or ""),
                "severity": "hard_stop",
                "impact": 1.0,
            }
            for hit in hard_stops
        ]
        return {
            "chosen_mode": MODE_REST,
            "started_from": base_mode,
            "downgrade_path": [MODE_HEAVY, MODE_REST],
            "decisive_reasons": reasons,
            "blockers": reasons,
            "confidence": round(_clamp(confidence), 3),
            "objections": [],
            "subtitle": "Hard stop erkannt -> REST",
            "explanation_payload": {
                "decision_style": "heavy_first_hard_stop",
                "notes": "Bei Krankheit/akuter Nicht-Trainierbarkeit wird nicht auf LIGHT heruntergeregelt, sondern klar auf REST.",
                "decision_clarity": round(_clamp(confidence), 3),
            },
        }

    layers = _layer_scores(state=state, scenarios=scenarios, parameter_weights=parameter_weights)
    objections = _build_objections_from_layers(layer_scores=layers)
    objections_sorted = sorted(objections, key=lambda row: row.weighted_score, reverse=True)
    total_objection = _safe_float(layers.get("decision_load"), 0.0)
    strong_count = sum(1 for row in objections_sorted if row.weighted_score >= 0.72)
    moderate_count = sum(1 for row in objections_sorted if row.weighted_score >= 0.42)

    capacity = _safe_float(layers.get("capacity_today_score"), 0.0)
    recovery_cost = _safe_float(layers.get("recovery_cost_score"), 1.0)
    feasibility = _safe_float(layers.get("feasibility_today_score"), 0.0)

    mode = MODE_HEAVY
    downgrade_path = [MODE_HEAVY]
    subtitle = "HEAVY blieb Standard"
    normal_threshold = 1.22
    light_threshold = 1.95

    heavy_allowed = (
        capacity >= 0.58
        and recovery_cost <= 0.58
        and feasibility >= 0.38
        and total_objection < normal_threshold
    )
    normal_allowed = (
        capacity >= 0.36
        and recovery_cost <= 0.78
        and feasibility >= 0.24
        and total_objection < light_threshold
    )

    if capacity < 0.12 and recovery_cost > 0.88 and feasibility < 0.16:
        mode = MODE_REST
        downgrade_path = [MODE_HEAVY, MODE_REST]
        subtitle = "Extrem niedrige Freigabe in allen Kernschichten -> REST"
    elif heavy_allowed:
        mode = MODE_HEAVY
        downgrade_path = [MODE_HEAVY]
        subtitle = "HEAVY bleibt freigegeben (Kapazität, Kosten und Umsetzbarkeit tragbar)"
    elif normal_allowed:
        mode = MODE_NORMAL
        downgrade_path = [MODE_HEAVY, MODE_NORMAL]
        subtitle = "HEAVY wird auf NORMAL gedrosselt (Kosten/Umsetzbarkeit bremsen)"
    else:
        mode = MODE_LIGHT
        downgrade_path = [MODE_HEAVY, MODE_NORMAL, MODE_LIGHT]
        subtitle = "HEAVY fällt auf LIGHT (Kernschichten zeigen klaren Gegenwind)"

    if mode == MODE_HEAVY:
        margin = min(capacity - 0.58, 0.58 - recovery_cost, feasibility - 0.38, normal_threshold - total_objection)
    elif mode == MODE_NORMAL:
        margin = min(capacity - 0.36, 0.78 - recovery_cost, feasibility - 0.24, light_threshold - total_objection)
    elif mode == MODE_REST:
        margin = max(0.0, max(0.12 - capacity, recovery_cost - 0.88, 0.16 - feasibility))
    else:
        margin = max(0.0, max(0.58 - capacity, recovery_cost - 0.58, 0.38 - feasibility, total_objection - normal_threshold))

    decision_clarity = _clamp(0.52 + (max(0.0, margin) * 0.75) + (_safe_float(layers.get("data_quality_score"), 0.0) * 0.12))
    confidence = decision_clarity

    decisive = [
        {
            "code": row.code,
            "label": row.label,
            "severity": row.severity,
            "impact": row.weighted_score,
            "raw_signal": row.raw_signal,
            "weight": row.weight,
            "weight_effect": _scaled_weight(row.weight),
            "final_effect": row.weighted_score,
            "detail": row.detail,
        }
        for row in objections_sorted[:4]
        if row.weighted_score >= 0.2 or mode != MODE_HEAVY
    ]

    return {
        "chosen_mode": mode,
        "started_from": base_mode,
        "downgrade_path": downgrade_path,
        "decisive_reasons": decisive,
        "blockers": [],
        "confidence": round(confidence, 3),
        "objections": [
            {
                "code": row.code,
                "label": row.label,
                "severity": row.severity,
                "weighted_score": row.weighted_score,
                "raw_signal": row.raw_signal,
                "weight": row.weight,
                "weight_effect": _scaled_weight(row.weight),
                "final_effect": row.weighted_score,
                "detail": row.detail,
            }
            for row in objections_sorted
        ],
        "subtitle": subtitle,
        "explanation_payload": {
            "decision_style": "heavy_first_layered",
            "objection_total": round(total_objection, 3),
            "thresholds": {"normal": normal_threshold, "light": light_threshold},
            "strong_objections": strong_count,
            "moderate_objections": moderate_count,
            "capacity_today_score": round(capacity, 3),
            "recovery_cost_score": round(recovery_cost, 3),
            "feasibility_today_score": round(feasibility, 3),
            "decision_clarity": round(decision_clarity, 3),
            "data_quality_score": round(_safe_float(layers.get("data_quality_score"), 0.0), 3),
            "layer_drivers": {
                "capacity": [
                    {
                        "code": row.code,
                        "label": row.label,
                        "signal": round(_clamp(row.signal), 3),
                        "weight_key": row.weight_key,
                        "parameter_weight": round(_clamp(row.parameter_weight, 0.04, 0.4), 3),
                    }
                    for row in (layers.get("capacity_components") or [])
                    if isinstance(row, LayerComponent)
                ],
                "recovery_cost": [
                    {
                        "code": row.code,
                        "label": row.label,
                        "signal": round(_clamp(row.signal), 3),
                        "weight_key": row.weight_key,
                        "parameter_weight": round(_clamp(row.parameter_weight, 0.04, 0.4), 3),
                    }
                    for row in (layers.get("recovery_cost_components") or [])
                    if isinstance(row, LayerComponent)
                ],
                "feasibility": [
                    {
                        "code": row.code,
                        "label": row.label,
                        "signal": round(_clamp(row.signal), 3),
                        "weight_key": row.weight_key,
                        "parameter_weight": round(_clamp(row.parameter_weight, 0.04, 0.4), 3),
                    }
                    for row in (layers.get("feasibility_components") or [])
                    if isinstance(row, LayerComponent)
                ],
            },
        },
    }
