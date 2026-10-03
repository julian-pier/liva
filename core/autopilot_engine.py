from __future__ import annotations

from analysis.training_analysis import classify_pattern
from analysis.runs_autopilot import latest_run_by_kind, compute_run_targets


RUN_KIND_LABELS = {
    "z2": "Run Z2",
    "threshold": "Run THRESHOLD",
}


def clamp(val: float, lo: float, hi: float) -> float:
    return lo if val < lo else hi if val > hi else val


def _as_float(val, default: float = 0.0) -> float:
    try:
        if val is None:
            return float(default)
        return float(val)
    except Exception:
        return float(default)


def _clean_ratio(num: float | None, den: float | None) -> float | None:
    try:
        if num is None or den is None:
            return None
        den = float(den)
        if den == 0:
            return None
        return float(num) / den
    except Exception:
        return None


def _safe_float(val):
    try:
        return float(val)
    except Exception:
        return None


def _safe_int(val):
    try:
        return int(val)
    except Exception:
        return None


def _is_lower_session(slot: dict | None) -> bool:
    if not isinstance(slot, dict):
        return False
    category = str(slot.get("category") or "").strip().lower()
    if category in {"legs", "lower"}:
        return True
    name = str(slot.get("session_name") or slot.get("label") or "").strip().lower()
    return any(token in name for token in ("legs", "lower", "beine"))


def _format_range(min_v: int, max_v: int) -> str:
    if min_v == max_v:
        return f"{min_v}"
    return f"{min_v}\u2013{max_v}"


def compute_state(
    recovery: dict,
    hrv_daily: list[dict],
    sickness: bool,
    alcohol: bool,
    signal_quality_low_days: int,
    gym_load: dict,
    run_load: dict,
    adherence: dict,
) -> dict:
    rmssd_7d = _safe_float(recovery.get("rmssd_7d"))
    rmssd_28d = _safe_float(recovery.get("rmssd_28d"))
    rhr_7d = _safe_float(recovery.get("rhr_7d"))
    rhr_28d = _safe_float(recovery.get("rhr_28d"))
    rmssd_today = _safe_float(recovery.get("rmssd_today"))
    rhr_today = _safe_float(recovery.get("rhr_today"))
    recovery_today_missing = bool(recovery.get("recovery_today_missing", (rmssd_today is None or rhr_today is None)))
    recovery_history_available = bool(
        recovery.get(
            "recovery_history_available",
            ((rmssd_7d is not None and rmssd_28d is not None) or (rhr_7d is not None and rhr_28d is not None)),
        )
    )
    hrv_ratio = _clean_ratio(rmssd_today if rmssd_today is not None else rmssd_7d, rmssd_28d)
    rhr_ratio = _clean_ratio(rhr_today if rhr_today is not None else rhr_7d, rhr_28d)
    recovery_missing = bool((recovery_today_missing and not recovery_history_available) or (hrv_ratio is None and rhr_ratio is None))

    recovery_payload = {
        "rmssd_today": recovery.get("rmssd_today"),
        "rhr_today": recovery.get("rhr_today"),
        "rmssd_7d": rmssd_7d,
        "rmssd_28d": rmssd_28d,
        "rhr_7d": rhr_7d,
        "rhr_28d": rhr_28d,
        "hrv_ratio": hrv_ratio,
        "rhr_ratio": rhr_ratio,
        "recovery_today_missing": recovery_today_missing,
        "recovery_history_available": recovery_history_available,
        "recovery_data_missing": recovery_missing,
    }
    local_fatigue = {
        "push": bool(gym_load.get("push_fatigue_high")),
        "pull": bool(gym_load.get("pull_fatigue_high")),
        "legs": bool(gym_load.get("legs_fatigue_high")),
    }
    zns_fatigue = bool(
        _is_low_recovery(recovery_payload)
        or gym_load.get("hi_yesterday")
        or run_load.get("hi_yesterday")
    )

    expected = adherence.get("expected_7d") or 0
    actual = adherence.get("actual_7d") or 0
    skipped = adherence.get("skipped_7d") or 0
    adherence_low = False

    return {
        "recovery": {
            "rmssd_today": recovery.get("rmssd_today"),
            "rhr_today": recovery.get("rhr_today"),
            "rmssd_7d": rmssd_7d,
            "rmssd_28d": rmssd_28d,
            "rhr_7d": rhr_7d,
            "rhr_28d": rhr_28d,
            "hrv_ratio": hrv_ratio,
            "rhr_ratio": rhr_ratio,
            "recovery_today_missing": recovery_today_missing,
            "recovery_history_available": recovery_history_available,
            "recovery_data_missing": recovery_missing,
        },
        "flags": {
            "sick": bool(sickness),
            "alcohol": bool(alcohol),
            "adherence_low": bool(adherence_low),
            "hi_yesterday": bool(gym_load.get("hi_yesterday") or run_load.get("hi_yesterday")),
            "zns_fatigue": zns_fatigue,
            "local_fatigue": local_fatigue,
        },
        "metrics": {
            "expected_7d": adherence.get("expected_7d"),
            "actual_7d": adherence.get("actual_7d"),
            "skipped_7d": adherence.get("skipped_7d"),
            "override_count_7d": adherence.get("override_count_7d"),
            "override_count_7d_whitelisted": adherence.get("override_count_7d_whitelisted", adherence.get("override_count_7d")),
            "override_types_7d": adherence.get("override_types_7d") or [],
            "logged_done_7d": adherence.get("logged_done_7d", 0),
            "assumed_done_7d": adherence.get("assumed_done_7d", 0),
            "skipped_gym_7d": adherence.get("skipped_gym_7d", 0),
            "skipped_runs_7d": adherence.get("skipped_runs_7d", 0),
            "late_window_hours": adherence.get("late_window_hours", 36),
            "late_window_days": adherence.get("late_window_days", 2),
            "planned_runs_last_3_weeks": adherence.get("planned_runs_last_3_weeks"),
            "performed_runs_last_3_weeks": adherence.get("performed_runs_last_3_weeks"),
            "days_since_last_run": adherence.get("days_since_last_run"),
        },
        "run": run_load,
        "gym": gym_load,
        "raw_recovery": recovery,
        "signal_quality_low_days": signal_quality_low_days,
    }


def _is_low_recovery(recovery: dict) -> bool:
    hrv_ratio = recovery.get("hrv_ratio")
    rhr_ratio = recovery.get("rhr_ratio")
    rmssd_today = _safe_float(recovery.get("rmssd_today"))
    rhr_today = _safe_float(recovery.get("rhr_today"))
    rmssd_28d = _safe_float(recovery.get("rmssd_28d"))
    rhr_28d = _safe_float(recovery.get("rhr_28d"))

    if hrv_ratio is not None and hrv_ratio < 0.90:
        return True
    if rhr_ratio is not None and rhr_ratio > 1.06:
        return True
    if rhr_today is not None and rhr_28d is not None and (rhr_today - rhr_28d) >= 4.0:
        return True
    if rmssd_today is not None and rmssd_28d is not None and rmssd_today < (0.85 * rmssd_28d):
        return True
    return False


def _build_adaptive_knobs(
    learning: dict | None,
    recovery: dict,
    flags: dict,
    metrics: dict,
) -> dict:
    profile = learning or {}
    override_rate = clamp(_as_float(profile.get("override_rate_28d"), 0.18), 0.0, 1.0)
    recovery_low_rate = clamp(_as_float(profile.get("recovery_low_rate_21d"), 0.25), 0.0, 1.0)
    run_compliance = clamp(_as_float(profile.get("run_compliance_21d"), 0.65), 0.0, 1.0)
    gym_avg_rpe = clamp(_as_float(profile.get("gym_avg_rpe_14d"), 7.4), 5.0, 10.0)
    training_days_14d = clamp(_as_float(profile.get("training_days_14d"), 7.0), 0.0, 14.0)

    aggression = 1.0
    aggression += (run_compliance - 0.60) * 0.35
    aggression -= max(0.0, override_rate - 0.20) * 0.70
    aggression -= max(0.0, recovery_low_rate - 0.30) * 0.60
    aggression += max(0.0, (training_days_14d / 14.0) - 0.45) * 0.25
    if gym_avg_rpe > 8.6:
        aggression -= 0.10
    if gym_avg_rpe < 7.0:
        aggression += 0.06
    aggression = clamp(aggression, 0.72, 1.18)

    threshold_bias = (run_compliance - 0.55) * 1.05 + (0.34 - recovery_low_rate) * 0.85
    if flags.get("alcohol"):
        threshold_bias -= 0.24
    if flags.get("hi_yesterday"):
        threshold_bias -= 0.14
    threshold_bias = clamp(threshold_bias, -0.55, 0.55)

    gym_bias = (0.30 - override_rate) * 0.9
    if gym_avg_rpe < 7.2:
        gym_bias += 0.12
    elif gym_avg_rpe > 8.6:
        gym_bias -= 0.12
    gym_bias = clamp(gym_bias, -0.45, 0.50)

    light_day_bias = max(0.0, recovery_low_rate - 0.28) * 1.1
    if flags.get("hi_yesterday"):
        light_day_bias += 0.15
    if _is_low_recovery(recovery):
        light_day_bias += 0.25
    light_day_bias = clamp(light_day_bias, 0.0, 0.75)

    z2_scale = clamp(0.92 + aggression * 0.24 - light_day_bias * 0.20, 0.75, 1.15)
    threshold_scale = clamp(0.88 + aggression * 0.22 - light_day_bias * 0.35, 0.65, 1.08)
    gym_rpe_shift_factor = clamp(1.00 + (aggression - 1.0) * 0.7 - light_day_bias * 0.5, 0.75, 1.15)
    force_light_day = bool(light_day_bias >= 0.55 or (_is_low_recovery(recovery) and override_rate >= 0.30))

    return {
        "aggression": aggression,
        "threshold_bias": threshold_bias,
        "gym_bias": gym_bias,
        "light_day_bias": light_day_bias,
        "run_duration_scale_z2": z2_scale,
        "run_duration_scale_threshold": threshold_scale,
        "gym_rpe_shift_factor": gym_rpe_shift_factor,
        "force_light_day": force_light_day,
    }


def _score_candidate(
    candidate: dict,
    *,
    planned_kind: str | None,
    planned_run_kind: str | None,
    classes: dict,
    recovery: dict,
    flags: dict,
    adaptive: dict,
) -> dict:
    kind = candidate.get("kind")
    run_kind = candidate.get("run_kind")
    minimal = bool(candidate.get("minimal_gym"))

    progression = 0.0
    compliance = 0.0
    risk = 0.0

    if kind == "rest":
        progression = 0.24
    elif kind == "run":
        if run_kind == "threshold":
            progression = 0.95 + _as_float(adaptive.get("threshold_bias"), 0.0)
            risk = 0.78
        else:
            progression = 0.72 + max(0.0, _as_float(adaptive.get("threshold_bias"), 0.0)) * 0.20
            risk = 0.30
    elif kind == "plan":
        progression = (0.65 if minimal else 1.0) + (_as_float(adaptive.get("gym_bias"), 0.0) * (0.55 if minimal else 1.0))
        risk = 0.36 if minimal else 0.58

    if classes.get("RC") in {"RC1", "RC2"}:
        risk += 0.15
    if classes.get("RC") == "RC3":
        risk += 0.45
    if flags.get("hi_yesterday") and kind in {"run", "plan"}:
        risk += 0.10
    if (flags.get("local_fatigue") or {}).get("legs") and kind == "plan" and not minimal and _is_lower_session(candidate):
        risk += 0.12
    if _is_low_recovery(recovery) and kind in {"run", "plan"}:
        risk += 0.14 if minimal else 0.24

    if planned_kind == kind:
        compliance += 0.35
        if kind == "run" and planned_run_kind and planned_run_kind == run_kind:
            compliance += 0.18
        if kind == "plan" and not minimal:
            compliance += 0.08
    elif planned_kind == "run" and kind == "run" and run_kind == "z2":
        compliance += 0.10
    elif planned_kind == "plan" and kind == "plan" and minimal:
        compliance += 0.10
    elif kind == "rest":
        compliance -= 0.15

    total = (progression * 1.20) + (compliance * 0.80) - (risk * 1.45)
    total = round(total, 3)

    return {
        "progression": round(progression, 3),
        "compliance": round(compliance, 3),
        "risk": round(risk, 3),
        "total": total,
    }


def build_reason_codes(state: dict, classes: dict | None = None) -> list[str]:
    codes = []
    recovery = state.get("recovery") or {}
    flags = state.get("flags") or {}
    local = flags.get("local_fatigue") or {}

    if flags.get("sick"):
        codes.append("SICK")
    if recovery.get("recovery_data_missing"):
        codes.append("RECOVERY_MISSING")
    if recovery.get("hrv_ratio") is not None and recovery.get("hrv_ratio") < 0.82:
        codes.append("HRV_VERY_LOW")
    elif recovery.get("hrv_ratio") is not None and recovery.get("hrv_ratio") < 0.90:
        codes.append("HRV_LOW")
    if recovery.get("rhr_ratio") is not None and recovery.get("rhr_ratio") > 1.06:
        codes.append("RHR_HIGH")
    if flags.get("alcohol"):
        codes.append("ALCOHOL")
    if flags.get("zns_fatigue"):
        codes.append("ZNS_FATIGUE")
    # adherence is ignored for v4
    if local.get("legs"):
        codes.append("LEGS_FATIGUE")
    if local.get("push"):
        codes.append("PUSH_FATIGUE")
    if local.get("pull"):
        codes.append("PULL_FATIGUE")
    if flags.get("hi_yesterday") or (state.get("gym", {}).get("hi_exposures_3d") or 0) >= 2:
        codes.append("HI_BACK_TO_BACK_GUARD")
    return sorted(set(codes))


def _run_target_from_settings(zones: dict | None, rhr_28d: float | None, kind: str) -> tuple[int, int]:
    if zones and isinstance(zones, dict):
        key = "z2" if kind == "z2" else "threshold"
        rng = zones.get(key)
        if isinstance(rng, (list, tuple)) and len(rng) == 2:
            lo = _safe_int(rng[0])
            hi = _safe_int(rng[1])
            if lo is not None and hi is not None:
                return int(lo), int(hi)

    base = _safe_float(rhr_28d)
    if base is None:
        return (142, 158) if kind == "z2" else (165, 172)
    if kind == "threshold":
        target = int(round(base + 115))
        return target - 6, target + 6
    target = int(round(base + 90))
    return target - 8, target + 8


def _round_to_5(mins: float | None) -> int | None:
    if mins is None:
        return None
    try:
        return int(round(mins / 5.0) * 5)
    except Exception:
        return None


def build_run_item(kind: str, duration_min: int, rhr_28d: float | None, zones: dict | None, structure: str | None = None, warning: str | None = None):
    targets = compute_run_targets(kind)
    label = RUN_KIND_LABELS.get(kind, f"Run {kind.upper()}")
    duration_target = _round_to_5(targets.get("duration_min") if targets else duration_min)
    duration_text = f"{duration_target} min" if duration_target else f"{duration_min} min"
    target_hr = targets.get("target_hr") if targets else None
    target_pace_s = targets.get("target_pace_s") if targets else None
    if target_hr is None and target_pace_s is None:
        lo, hi = _run_target_from_settings(zones, rhr_28d, kind)
        target_hr = int(round((lo + hi) / 2.0))
    bpm_text = ""
    if target_hr:
        bpm_text = f"Ziel: {int(round(target_hr))} bpm"
    if target_pace_s and not bpm_text:
        pace_min = int(target_pace_s // 60)
        pace_sec = int(target_pace_s % 60)
        bpm_text = f"Ziel: {pace_min}:{pace_sec:02d}/km"
    left = f"{label} \u00b7 {duration_text}"
    if bpm_text:
        left = f"{left} \u00b7 {bpm_text}"
    if structure:
        left = f"{left} \u00b7 ({structure})"
    if warning:
        left = f"{left} \u00b7 ({warning})"

    history = latest_run_by_kind(kind)
    compare = ""
    if history:
        parts = []
        if history.get("moving_time"):
            parts.append(_format_duration_min(history["moving_time"]))
        if history.get("km"):
            parts.append(f"{history['km']:.1f} km")
        pace = _format_pace_s(history.get("pace_s"))
        if pace:
            parts.append(pace)
        if history.get("avg_hr"):
            parts.append(f"\u00d8 {int(round(history['avg_hr']))} bpm")
        if parts:
            compare = "letztes: " + " \u00b7 ".join(parts)

    item = {
        "title": label,
        "slot_index": 0,
        "coverage": "planned_missing",
        "suggestion_inline": left,
        "force_inline": True,
        "pill": {"text": kind.upper(), "color": "yellow"},
        "structured": {"new_reps": [], "new_weights": []},
        "planned": {
            "device": "",
            "sets": 0,
            "reps_min": 0,
            "reps_max": 0,
            "rpe_min": None,
            "rpe_max": None,
            "run": {
                "type": kind,
                "duration_min": int(duration_target or duration_min or 0),
                "target_hr_bpm": int(round(target_hr)) if target_hr is not None else None,
                "target_pace_s_per_km": int(round(target_pace_s)) if target_pace_s is not None else None,
                "basis": (targets or {}).get("basis") if targets else "heuristic_fallback",
                "history_n": int(((targets or {}).get("basis") or "median_last_0").split("_")[2]) if targets and (targets or {}).get("basis") and len(((targets or {}).get("basis") or "").split("_")) >= 3 else (0 if targets is None else None),
            },
        },
        "done": {"sets": 0, "reps": [], "avg_rpe": None, "weights": [], "rpes": [], "set_numbers": []},
    }
    if compare:
        item["done"]["notes"] = compare
    return item


def _format_pace_s(pace_s):
    try:
        s = int(round(float(pace_s)))
    except Exception:
        return ""
    if s <= 0:
        return ""
    m = s // 60
    sec = s % 60
    return f"{m}:{sec:02d}/km"


def _format_duration_min(seconds):
    try:
        if seconds is None:
            return ""
        mins = int(round(float(seconds) / 60.0))
    except Exception:
        return ""
    return f"{mins} min" if mins > 0 else ""


def build_rest_item(reason_codes: list[str]):
    left = "Walk 20\u201330 min \u00b7 Mobility 10\u201315 min"
    return {
        "title": "Rest / Recovery",
        "slot_index": 0,
        "coverage": "planned_missing",
        "suggestion_inline": left,
        "force_inline": True,
        "pill": {"text": "REST", "color": "yellow"},
        "structured": {"new_reps": [], "new_weights": []},
        "planned": {"device": "", "sets": 0, "reps_min": 0, "reps_max": 0, "rpe_min": None, "rpe_max": None},
        "done": {"sets": 0, "reps": [], "avg_rpe": None, "weights": [], "rpes": [], "set_numbers": []},
    }


def determine_next_session(
    state: dict,
    today_slots: list[dict],
    rotation_sessions: list[dict],
    rotation_index: int | None,
    day_context: dict | None = None,
    learning_profile: dict | None = None,
) -> dict:
    recovery = state.get("recovery") or {}
    flags = state.get("flags") or {}
    metrics = state.get("metrics") or {}
    ctx = day_context or {}
    is_today = bool(ctx.get("is_today"))
    is_tomorrow = bool(ctx.get("is_tomorrow"))

    eff_flags = dict(flags)
    if not (is_today or is_tomorrow):
        eff_flags["sick"] = False
        eff_flags["alcohol"] = False
        eff_flags["adherence_low"] = False
    forecast_wait_for_tomorrow = bool(is_tomorrow and not eff_flags.get("sick"))
    if forecast_wait_for_tomorrow:
        # For tomorrow we do not pre-penalize based on today's global recovery/alcohol.
        # Exception remains local fatigue via eff_flags["local_fatigue"].
        eff_flags["alcohol"] = False
        eff_flags["adherence_low"] = False
        eff_flags["hi_yesterday"] = False
    local = (eff_flags.get("local_fatigue") or {})

    recovery_eff = dict(recovery or {})
    if forecast_wait_for_tomorrow:
        recovery_eff["hrv_ratio"] = None
        recovery_eff["rhr_ratio"] = None
        recovery_eff["recovery_data_missing"] = False
        recovery_eff["rmssd_today"] = None
        recovery_eff["rhr_today"] = None

    rhr_ratio = recovery_eff.get("rhr_ratio")
    hrv_ratio = recovery_eff.get("hrv_ratio")
    recovery_missing = bool(recovery_eff.get("recovery_data_missing"))
    low_recovery = _is_low_recovery(recovery_eff)

    extreme_recovery = bool(
        (rhr_ratio is not None and rhr_ratio > 1.12)
        or (
            rhr_ratio is not None
            and rhr_ratio > 1.08
            and hrv_ratio is not None
            and hrv_ratio < 0.82
        )
    )

    rc = "RC0"
    if eff_flags.get("sick"):
        rc = "RC3"
    elif extreme_recovery:
        rc = "RC3"
    elif low_recovery:
        rc = "RC2"
    elif recovery_missing or (hrv_ratio is not None and hrv_ratio < 0.95) or (rhr_ratio is not None and rhr_ratio > 1.03):
        rc = "RC1"
    if not (is_today or is_tomorrow):
        rc = "RC0"

    expected = metrics.get("expected_7d") or 0
    actual = metrics.get("actual_7d") or 0
    skipped = metrics.get("skipped_7d") or 0
    adherence_low = False
    alcohol = bool(eff_flags.get("alcohol"))
    if alcohol and adherence_low:
        bc = "BC3"
    elif alcohol:
        bc = "BC1"
    elif adherence_low:
        bc = "BC2"
    else:
        bc = "BC0"
    if not (is_today or is_tomorrow):
        bc = "BC0"

    lf_legs = bool(local.get("legs"))

    classes = {"RC": rc, "BC": bc, "LF_LEGS": lf_legs, "LF_PUSH": bool(local.get("push")), "LF_PULL": bool(local.get("pull"))}
    state_eff = dict(state)
    state_eff["recovery"] = recovery_eff
    state_eff["flags"] = eff_flags
    reason_codes = build_reason_codes(state_eff, classes)

    learning = learning_profile if isinstance(learning_profile, dict) else (state.get("learning_profile") or {})
    learning_ready = bool(learning.get("learning_ready"))
    if learning_ready:
        adaptive = _build_adaptive_knobs(learning, recovery_eff, eff_flags, metrics)
    else:
        adaptive = {
            "aggression": 1.0,
            "threshold_bias": 0.0,
            "gym_bias": 0.0,
            "light_day_bias": 0.0,
            "run_duration_scale_z2": 1.0,
            "run_duration_scale_threshold": 1.0,
            "gym_rpe_shift_factor": 1.0,
            "force_light_day": False,
        }

    decision_meta = {
        "inputs": {
            "recovery": recovery,
            "flags": eff_flags,
            "metrics": metrics,
            "run": state.get("run"),
            "gym": state.get("gym"),
            "classes": classes,
            "learning_profile": learning,
        },
        "triggers": reason_codes,
        "guards": [],
        "outputs": {},
        "adaptive": adaptive,
    }

    def _today_summary():
        gym_slot = None
        run_z2 = None
        run_thr = None
        for slot in today_slots or []:
            if slot.get("kind") == "plan" and gym_slot is None:
                gym_slot = slot
            if slot.get("kind") == "run":
                rk = slot.get("run_kind")
                if rk == "threshold" and run_thr is None:
                    run_thr = slot
                if rk == "z2" and run_z2 is None:
                    run_z2 = slot
        return gym_slot, run_z2, run_thr

    gym_slot, run_z2_slot, run_thr_slot = _today_summary()

    def _slot_duration(slot: dict | None) -> int | None:
        if not isinstance(slot, dict):
            return None
        try:
            val = int(slot.get("run_duration_min") or 0)
        except Exception:
            val = 0
        return val if val > 0 else None

    def pick_today_gym():
        return gym_slot

    def make_rest():
        decision_meta["outputs"] = {"choice": "rest:recovery"}
        return {
            "choice_id": "rest:recovery",
            "kind": "rest",
            "minimal_gym": False,
            "reason_codes": reason_codes,
            "decision_meta": decision_meta,
            "badges": ["REST"],
        }

    def _scaled_duration(kind: str, duration_min: int) -> int:
        base = int(duration_min or 0)
        if base <= 0:
            base = 45 if kind == "z2" else 30
        if not learning_ready:
            return base
        scale_key = "run_duration_scale_threshold" if kind == "threshold" else "run_duration_scale_z2"
        scale = _as_float(adaptive.get(scale_key), 1.0)
        scaled = int(round(base * scale))
        if kind == "threshold":
            return int(clamp(scaled, 20, 70))
        return int(clamp(scaled, 20, 95))

    def make_run(kind, duration, downshift=False):
        if downshift:
            reason_codes.append("DOWN_SHIFT_RUN")
        chosen_duration = int(duration or 0)
        if not downshift:
            planned_duration = _slot_duration(run_thr_slot if kind == "threshold" else run_z2_slot)
            if planned_duration is not None:
                chosen_duration = planned_duration
        chosen_duration = _scaled_duration(kind, chosen_duration)
        decision_meta["outputs"] = {"choice": f"run:{kind}"}
        return {
            "choice_id": f"run:{kind}",
            "kind": "run",
            "run_kind": kind,
            "run_duration_min": chosen_duration,
            "minimal_gym": False,
            "reason_codes": reason_codes,
            "decision_meta": decision_meta,
            "badges": ["Z2"] if kind == "z2" else ["THRESHOLD"],
        }

    def make_gym(minimal=False):
        slot = pick_today_gym()
        if not slot and rotation_sessions:
            idx = rotation_index if rotation_index is not None else 0
            slot = {
                "kind": "plan",
                "session_key": rotation_sessions[idx % len(rotation_sessions)]["session_key"],
                "session_name": rotation_sessions[idx % len(rotation_sessions)]["session_name"],
            }
        choice_id = f"plan:{slot['session_key']}" if slot and slot.get("session_key") else "rest:recovery"
        decision_meta["outputs"] = {"choice": choice_id}
        payload = {
            "choice_id": choice_id,
            "kind": "plan",
            "session_key": slot.get("session_key") if slot else None,
            "session_name": slot.get("session_name") if slot else None,
            "minimal_gym": bool(minimal),
            "reason_codes": reason_codes,
            "decision_meta": decision_meta,
            "badges": ["GYM"],
        }
        if minimal:
            payload["badges"] = ["RPE ↓", "Volumen ↓"]
        return payload

    has_gym_today = gym_slot is not None
    gym_is_legs = _is_lower_session(gym_slot)
    upper_preserved_from_local_legs = bool(has_gym_today and (not gym_is_legs) and lf_legs and (not eff_flags.get("zns_fatigue")))
    has_run_z2_today = run_z2_slot is not None
    has_run_thr_today = run_thr_slot is not None
    has_planned_today = has_gym_today or has_run_z2_today or has_run_thr_today

    if eff_flags.get("sick") and is_today:
        return make_rest()
    if eff_flags.get("sick") and is_tomorrow:
        if lf_legs and gym_is_legs:
            return make_gym(minimal=True)
        if has_run_z2_today or has_run_thr_today:
            downshift = bool(has_run_thr_today and not has_run_z2_today)
            return make_run("z2", 25, downshift=downshift)
        return make_gym(minimal=True)

    if rc == "RC3":
        return make_rest()
    if (today_slots or []) and not has_planned_today:
        return make_rest()

    # Tomorrow policy: do not pre-decide by global recovery; allow only local fatigue handling.
    if forecast_wait_for_tomorrow and lf_legs and has_gym_today and gym_is_legs:
            return make_gym(minimal=True)

    if bc == "BC3":
        if has_run_z2_today or has_run_thr_today:
            downshift = bool(has_run_thr_today and not has_run_z2_today)
            return make_run("z2", 30, downshift=downshift)
        if lf_legs and gym_is_legs:
            if has_gym_today:
                return make_gym(minimal=True)
            return make_rest()
        if has_gym_today and not gym_is_legs:
            return make_gym(minimal=True)
        return make_rest()
    if bc == "BC1":
        if has_run_z2_today or has_run_thr_today:
            downshift = bool(has_run_thr_today and not has_run_z2_today)
            return make_run("z2", 30, downshift=downshift)
        if lf_legs and gym_is_legs:
            if has_gym_today:
                return make_gym(minimal=True)
            return make_rest()
        if has_gym_today:
            return make_gym(minimal=True)
        return make_rest()
    if bc == "BC2":
        if has_run_z2_today or has_run_thr_today:
            downshift = bool(has_run_thr_today and not has_run_z2_today)
            return make_run("z2", 30, downshift=downshift)
        if lf_legs and gym_is_legs:
            if has_gym_today:
                return make_gym(minimal=True)
            return make_rest()
        if has_gym_today:
            return make_gym(minimal=True)
        return make_rest()

    if rc == "RC2":
        if has_run_z2_today or has_run_thr_today:
            downshift = bool(has_run_thr_today and not has_run_z2_today)
            return make_run("z2", 30, downshift=downshift)
        if lf_legs and gym_is_legs:
            if has_gym_today:
                return make_gym(minimal=True)
            return make_rest()
        if has_gym_today:
            return make_gym(minimal=True)
        return make_rest()
    if rc == "RC1":
        if has_run_z2_today or has_run_thr_today:
            downshift = bool(has_run_thr_today and not has_run_z2_today)
            return make_run("z2", 45, downshift=downshift)
        if upper_preserved_from_local_legs:
            return make_gym(minimal=False)
        if lf_legs and gym_is_legs:
            if has_gym_today:
                return make_gym(minimal=True)
            return make_rest()
        if has_gym_today:
            return make_gym(minimal=True)
        return make_rest()

    if learning_ready and adaptive.get("force_light_day"):
        decision_meta["guards"].append("LEARNED_LIGHT_DAY")
        if "LEARNED_LIGHT_DAY" not in reason_codes:
            reason_codes.append("LEARNED_LIGHT_DAY")
        if has_run_z2_today or has_run_thr_today:
            downshift = bool(has_run_thr_today and not has_run_z2_today)
            return make_run("z2", 30, downshift=downshift)
        if has_gym_today:
            return make_gym(minimal=True)
        return make_rest()

    if learning_ready and (is_today or is_tomorrow):
        planned_kind = (today_slots or [{}])[0].get("kind") if (today_slots and isinstance(today_slots[0], dict)) else None
        planned_run_kind = (today_slots or [{}])[0].get("run_kind") if (today_slots and isinstance(today_slots[0], dict)) else None
        candidates: list[dict] = [{"kind": "rest"}]
        if has_gym_today:
            candidates.append({"kind": "plan", "minimal_gym": False, "category": gym_slot.get("category"), "session_name": gym_slot.get("session_name")})
            candidates.append({"kind": "plan", "minimal_gym": True, "category": gym_slot.get("category"), "session_name": gym_slot.get("session_name")})
        if has_run_z2_today:
            candidates.append({"kind": "run", "run_kind": "z2"})
        if has_run_thr_today and not eff_flags.get("hi_yesterday"):
            candidates.append({"kind": "run", "run_kind": "threshold"})

        scored = []
        for cand in candidates:
            score = _score_candidate(
                cand,
                planned_kind=planned_kind,
                planned_run_kind=planned_run_kind,
                classes=classes,
                recovery=recovery_eff,
                flags=eff_flags,
                adaptive=adaptive,
            )
            scored.append({
                "candidate": cand,
                "score": score,
            })
        scored.sort(
            key=lambda x: (
                float((x.get("score") or {}).get("total") or -9999.0),
                1 if (x.get("candidate") or {}).get("kind") == "plan" and not (x.get("candidate") or {}).get("minimal_gym") else 0,
                1 if (x.get("candidate") or {}).get("kind") == "run" and (x.get("candidate") or {}).get("run_kind") == "threshold" else 0,
            ),
            reverse=True,
        )
        decision_meta["candidate_scores"] = scored
        chosen = (scored[0] or {}).get("candidate") if scored else None
        if isinstance(chosen, dict):
            ckind = chosen.get("kind")
            if ckind == "run":
                rk = chosen.get("run_kind") or "z2"
                return make_run("threshold" if rk == "threshold" else "z2", 30 if rk == "threshold" else 45)
            if ckind == "plan":
                return make_gym(minimal=bool(chosen.get("minimal_gym")))
            return make_rest()

    if has_run_thr_today and not eff_flags.get("hi_yesterday"):
        return make_run("threshold", 30)
    if has_run_z2_today:
        return make_run("z2", 45)
    if has_gym_today:
        return make_gym(minimal=False)
    return make_gym(minimal=False)


def apply_gym_mods(items: list[dict], state: dict, rpe_hardcap: float) -> dict:
    recovery = state.get("recovery") or {}
    flags = state.get("flags") or {}
    metrics = state.get("metrics") or {}
    learning = state.get("learning_profile") or {}
    local = (flags.get("local_fatigue") or {})

    low_recovery = _is_low_recovery(recovery)
    learning_ready = bool(learning.get("learning_ready"))
    adaptive = (
        _build_adaptive_knobs(learning, recovery, flags, metrics)
        if learning_ready
        else {
            "gym_rpe_shift_factor": 1.0,
            "force_light_day": False,
        }
    )

    base_shift = 0.0
    if low_recovery:
        base_shift = -1.0
    if flags.get("alcohol"):
        base_shift = min(base_shift, -0.5)
    if learning_ready:
        shift_factor = _as_float(adaptive.get("gym_rpe_shift_factor"), 1.0)
        base_shift += (shift_factor - 1.0) * 0.9

    hi_guard = flags.get("hi_yesterday") or state.get("gym", {}).get("hi_exposures_3d", 0) >= 2
    rpe_cap = rpe_hardcap
    if hi_guard:
        rpe_cap = min(rpe_cap, 7.5)
    if flags.get("alcohol"):
        rpe_cap = min(rpe_cap, 7.5)
    if learning_ready and adaptive.get("force_light_day"):
        rpe_cap = min(rpe_cap, 7.0)
    sets_removed = False
    min_shift = 0.0

    for idx, item in enumerate(items or []):
        planned = item.get("planned") or {}
        name = (item.get("title") or "").strip()
        pattern = classify_pattern(name or "", "")
        item_shift = base_shift
        if pattern in {"chest", "shoulders_arms"} and local.get("push"):
            item_shift = min(item_shift, -1.0)
        if pattern in {"back"} and local.get("pull"):
            item_shift = min(item_shift, -1.0)
        if pattern in {"legs"} and local.get("legs"):
            item_shift = min(item_shift, -1.5)
        min_shift = min(min_shift, item_shift)

        for key in ("rpe_min", "rpe_max"):
            if planned.get(key) is not None:
                try:
                    planned[key] = max(5.0, float(planned[key]) + item_shift)
                except Exception:
                    pass
                if planned.get(key) is not None:
                    planned[key] = min(planned[key], rpe_cap)
        item["planned"] = planned

        if low_recovery and idx >= 2:
            planned_sets = planned.get("sets")
            if isinstance(planned_sets, int) and planned_sets > 1:
                planned["sets"] = planned_sets - 1
                sets_removed = True
            structured = item.get("structured") or {}
            reps = structured.get("new_reps") or []
            weights = structured.get("new_weights") or []
            if isinstance(reps, list) and len(reps) > 1:
                structured["new_reps"] = reps[:-1]
            if isinstance(weights, list) and len(weights) > 1:
                structured["new_weights"] = weights[:-1]
            item["structured"] = structured

    return {
        "rpe_shift": min_shift,
        "sets_removed": sets_removed,
        "rpe_cap": rpe_cap,
    }


def shift_week_schedule(week_plan: list[dict], today_id: str, state: dict) -> list[dict]:
    plan = []
    for day in week_plan:
        d = dict(day)
        if "origin_day_id" not in d:
            d["origin_day_id"] = d.get("day_id")
        plan.append(d)
    if not plan:
        return plan

    index_by_id = {d.get("day_id"): i for i, d in enumerate(plan) if d.get("day_id")}
    if today_id not in index_by_id:
        return plan

    def _is_strength(slot: dict) -> bool:
        return slot.get("kind") == "plan"

    today_idx = index_by_id[today_id]
    today_slot = dict(plan[today_idx])

    backlog_strength: list[dict] = []
    if _is_strength(today_slot):
        backlog_strength.append(today_slot)

    plan[today_idx] = {
        "day_id": plan[today_idx]["day_id"],
        "kind": "rest",
        "label": "Rest",
        "origin_day_id": today_slot.get("origin_day_id") or today_slot.get("day_id"),
    }

    for i in range(today_idx + 1, len(plan)):
        if not backlog_strength:
            break
        current = dict(plan[i])
        replacement = backlog_strength.pop(0)

        if _is_strength(current):
            backlog_strength.append(current)

        placed = dict(replacement)
        placed["day_id"] = current.get("day_id")
        placed["origin_day_id"] = replacement.get("origin_day_id") or replacement.get("day_id")
        # tomorrow light re-entry
        if i == today_idx + 1 and placed.get("kind") == "plan":
            placed["minimal_gym"] = True
        plan[i] = placed

    return plan
