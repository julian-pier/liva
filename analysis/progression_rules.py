from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Literal


ProgressStatus = Literal["progress", "stable", "regress"]
DisplayProgressStatus = Literal["progress", "neutral", "regress"]
TbType = Literal["T", "B"]

RULE_VERSION = "progression_rules_v6"
# Logs created on or after this V5 rollout date must contain RPE. Historical
# logs remain read-only and may use the deterministic raw-e1RM fallback.
RPE_REQUIRED_SINCE = "2026-07-27"


@dataclass(frozen=True)
class ProgressRuleConfig:
    load_progression_min_pct: float = 1.0
    load_progression_score_floor_pct: float = -2.0
    legacy_load_progression_score_floor_pct: float = -1.0
    load_progression_rpe_ceiling: float = 1.0
    rep_progression_min_reps: float = 1.0
    rep_progression_score_floor_pct: float = 0.0
    rep_progression_rpe_ceiling: float = 1.0
    progress_score_min_pct: float = 1.5
    regress_score_max_pct: float = -3.0
    high_effort_rpe_delta: float = 1.5
    high_effort_progress_floor_pct: float = 3.0
    # Kept for backwards-compatible callers. V5 exposes a result as soon as
    # one meaningful comparison exists; confidence is informational only and
    # never changes the classification.
    min_comparable_per_session: int = 1
    # Legacy helpers still accept these values, but the canonical V5 engine
    # no longer derives comparison slots from relative top/backoff weights.
    top_backoff_pct: float = 0.05
    top_backoff_abs_threshold: float = 5.0


DEFAULT_CONFIG = ProgressRuleConfig()


@dataclass(frozen=True)
class SetIdentity:
    exercise_name: str = ""
    canonical_exercise: str | None = None
    device: str = ""
    variation: str = ""
    laterality: str = ""
    execution_mode: str = ""
    session_name: str | None = None


@dataclass(frozen=True)
class SetPerformance:
    weight: float | None = None
    reps: int | float | None = None
    rpe: float | None = None
    e1rm: float | None = None
    set_number: int | None = None
    order_idx: int | None = None
    tb_type: TbType | None = None
    tb_slot: int | None = None
    source_set_id: int | None = None
    source_workout_id: int | None = None
    source_session_date: str | None = None
    logged_at: str | None = None
    exercise_name: str = ""
    canonical_exercise: str | None = None
    canonical_exercise_id: str | None = None
    device: str = ""
    variation: str = ""
    variation_id: str = ""
    laterality: str = ""
    execution_mode: str = ""
    set_slot: str | None = None
    slot_source: str | None = None
    session_name: str | None = None
    progression_excluded: bool = False

    @property
    def identity(self) -> SetIdentity:
        return SetIdentity(
            exercise_name=self.exercise_name,
            canonical_exercise=self.canonical_exercise,
            device=self.device,
            variation=self.variation,
            laterality=self.laterality,
            execution_mode=self.execution_mode,
            session_name=self.session_name,
        )


@dataclass(frozen=True)
class SetComparisonResult:
    status: ProgressStatus | None
    comparable: bool
    match_level: str
    reason_code: str
    reason_text: str
    current: SetPerformance | None
    reference: SetPerformance | None
    deltas: dict[str, float | None] = field(default_factory=dict)
    detail_reason_code: str | None = None
    exclusion_reason: str | None = None
    comparison_mode: Literal["rpe_adjusted", "legacy_raw_e1rm"] | None = None


@dataclass(frozen=True)
class SessionProgressSummary:
    total_sets: int
    comparable_sets: int
    progress: int
    stable: int
    regress: int
    excluded: int
    progress_rate: float | None
    regression_rate: float | None
    net_progress: float | None
    coverage: float | None
    status: Literal["progress", "stable", "regress", "not_enough_data"]
    display_status: Literal["progress", "neutral", "regress"]
    reason_code: str
    reason_text: str

    @property
    def improved(self) -> int:
        return self.progress

    @property
    def same(self) -> int:
        return self.stable

    @property
    def worse(self) -> int:
        return self.regress

    @property
    def new(self) -> int:
        return self.excluded

    @property
    def unknown(self) -> int:
        return 0


def _norm(value: Any) -> str:
    return str(value or "").strip().lower()


def _to_float(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        out = float(value)
        if out != out:
            return None
        return out
    except Exception:
        return None


def _to_int(value: Any) -> int | None:
    try:
        if value is None or value == "":
            return None
        return int(value)
    except Exception:
        return None


def calculate_e1rm(weight: float | None, reps: int | float | None) -> float | None:
    w = _to_float(weight)
    r = _to_float(reps)
    if w is None or r is None or w <= 0 or r <= 0:
        return None
    return w * (1.0 + (r / 30.0))


def calculate_effort_adjusted_e1rm(
    weight: float | None,
    reps: int | float | None,
    rpe: float | None,
    *,
    allow_missing_rpe: bool = False,
) -> float | None:
    w = _to_float(weight)
    r = _to_float(reps)
    effort = _to_float(rpe)
    if w is None or r is None or w <= 0 or r <= 0:
        return None
    if effort is None:
        return calculate_e1rm(w, r) if allow_missing_rpe else None
    rir = max(0.0, min(4.0, 10.0 - effort))
    adjusted_reps = r + rir
    return w * (1.0 + (adjusted_reps / 30.0))


def is_legacy_logged_at(logged_at: str | None, *, rpe_required_since: str = RPE_REQUIRED_SINCE) -> bool:
    """Whether a timestamp belongs to the pre-RPE-required legacy format."""

    value = str(logged_at or "").strip()
    return bool(value and value[:10] < str(rpe_required_since)[:10])


def has_regression_trend(
    performance_scores: list[float | None],
    *,
    regress_threshold_pct: float = -3.0,
    severe_drop_threshold_pct: float = -7.0,
) -> bool:
    """Gate long-term warnings independently from single-set classification.

    A warning is allowed only after two consecutive regression-sized drops or
    one severe drop in the latest execution. Earlier isolated dips do not turn
    the current trend into a regression warning.
    """

    clean = [float(value) for value in performance_scores if value is not None and float(value) > 0]
    if len(clean) < 2:
        return False
    deltas = [((current / previous) - 1.0) * 100.0 for previous, current in zip(clean, clean[1:])]
    if deltas[-1] <= severe_drop_threshold_pct:
        return True
    return len(deltas) >= 2 and deltas[-2] <= regress_threshold_pct and deltas[-1] <= regress_threshold_pct


def set_key_name(value: SetPerformance) -> str:
    # Progression identity is intentionally limited to exercise + variation.
    # Composite storage IDs may also encode device/laterality and therefore
    # must not take precedence over the canonical/base exercise name.
    return _norm(value.canonical_exercise or value.exercise_name or value.canonical_exercise_id)


def normalize_set_performance(raw: SetPerformance | dict[str, Any] | None) -> SetPerformance | None:
    if raw is None:
        return None
    if isinstance(raw, SetPerformance):
        if raw.e1rm is not None:
            return raw
        return replace(raw, e1rm=calculate_e1rm(raw.weight, raw.reps))
    weight = _to_float(raw.get("weight") if "weight" in raw else raw.get("weight_kg"))
    reps = _to_float(raw.get("reps"))
    rpe = _to_float(raw.get("rpe"))
    set_number = _to_int(raw.get("set_number") if "set_number" in raw else raw.get("set_slot"))
    order_idx = _to_int(raw.get("order_idx"))
    return SetPerformance(
        weight=weight,
        reps=reps,
        rpe=rpe,
        e1rm=_to_float(raw.get("e1rm")) or calculate_e1rm(weight, reps),
        set_number=set_number,
        order_idx=order_idx,
        tb_type=raw.get("tb_type") if raw.get("tb_type") in {"T", "B"} else None,
        tb_slot=_to_int(raw.get("tb_slot")),
        source_set_id=_to_int(raw.get("source_set_id") if "source_set_id" in raw else raw.get("set_id")),
        source_workout_id=_to_int(raw.get("source_workout_id") if "source_workout_id" in raw else raw.get("workout_id")),
        source_session_date=str(raw.get("source_session_date") or raw.get("date_iso") or "")[:10] or None,
        logged_at=str(raw.get("logged_at") or raw.get("created_at") or "").strip() or None,
        exercise_name=_norm(raw.get("exercise_name") or raw.get("name")),
        canonical_exercise=_norm(raw.get("canonical_exercise")) or None,
        canonical_exercise_id=_norm(raw.get("canonical_exercise_id") or raw.get("canonical_id") or raw.get("exercise_key")) or None,
        device=_norm(raw.get("device")),
        variation=_norm(raw.get("variation")),
        variation_id=_norm(raw.get("variation_id") or raw.get("variation")),
        laterality=_norm(raw.get("laterality")),
        execution_mode=_norm(raw.get("execution_mode")),
        set_slot=str(raw.get("set_slot") or raw.get("slot") or "").strip() or None,
        slot_source=str(raw.get("slot_source") or "").strip() or None,
        session_name=_norm(raw.get("session_name")) or None,
        progression_excluded=bool(raw.get("progression_excluded")),
    )


def _same_identity(current: SetPerformance, reference: SetPerformance) -> bool:
    cur_name = set_key_name(current)
    ref_name = set_key_name(reference)
    if not cur_name or not ref_name or cur_name != ref_name:
        return False
    if _norm(current.variation_id or current.variation) != _norm(reference.variation_id or reference.variation):
        return False
    return True


def _identity_exclusion_reason(current: SetPerformance, reference: SetPerformance) -> str:
    if set_key_name(current) != set_key_name(reference):
        return "different_exercise"
    if _norm(current.variation_id or current.variation) != _norm(reference.variation_id or reference.variation):
        return "different_variation"
    return "different_identity"


def _deltas(
    current: SetPerformance,
    reference: SetPerformance | None,
    *,
    comparison_mode: Literal["rpe_adjusted", "legacy_raw_e1rm"] | None = None,
) -> dict[str, float | None]:
    if reference is None:
        return {
            "weight_delta": None,
            "reps_delta": None,
            "rpe_delta": None,
            "e1rm_delta": None,
            "e1rm_delta_pct": None,
        }
    if comparison_mode == "legacy_raw_e1rm":
        # A legacy pair is always symmetric: the presence of RPE on just one
        # side must never manufacture an advantage.
        current_e1rm = calculate_e1rm(current.weight, current.reps)
        reference_e1rm = calculate_e1rm(reference.weight, reference.reps)
    else:
        current_e1rm = calculate_effort_adjusted_e1rm(current.weight, current.reps, current.rpe)
        reference_e1rm = calculate_effort_adjusted_e1rm(reference.weight, reference.reps, reference.rpe)
    e1rm_delta = (current_e1rm - reference_e1rm) if current_e1rm is not None and reference_e1rm is not None else None
    e1rm_delta_pct = (e1rm_delta / reference_e1rm) if e1rm_delta is not None and reference_e1rm and reference_e1rm > 0 else None
    return {
        "weight_delta": (float(current.weight) - float(reference.weight)) if current.weight is not None and reference.weight is not None else None,
        "reps_delta": (float(current.reps) - float(reference.reps)) if current.reps is not None and reference.reps is not None else None,
        "rpe_delta": (float(current.rpe) - float(reference.rpe)) if current.rpe is not None and reference.rpe is not None else None,
        "e1rm_delta": e1rm_delta,
        "e1rm_delta_pct": e1rm_delta_pct,
        "current_score": current_e1rm,
        "reference_score": reference_e1rm,
        "score_delta_pct": (e1rm_delta_pct * 100.0) if e1rm_delta_pct is not None else None,
        "load_delta_pct": (((float(current.weight) / float(reference.weight)) - 1.0) * 100.0) if current.weight is not None and reference.weight not in (None, 0) else None,
    }


def map_progress_status(status: ProgressStatus | None, *, comparable: bool = True) -> DisplayProgressStatus:
    if status == "progress" and comparable:
        return "progress"
    if status == "regress" and comparable:
        return "regress"
    return "neutral"


def _result(
    status: ProgressStatus | None,
    comparable: bool,
    match_level: str,
    reason_code: str,
    reason_text: str,
    current: SetPerformance | None,
    reference: SetPerformance | None,
    *,
    detail_reason_code: str | None = None,
    exclusion_reason: str | None = None,
    comparison_mode: Literal["rpe_adjusted", "legacy_raw_e1rm"] | None = None,
) -> SetComparisonResult:
    return SetComparisonResult(
        status=status,
        comparable=bool(comparable),
        match_level=match_level,
        reason_code=reason_code,
        reason_text=reason_text,
        current=current,
        reference=reference,
        deltas=_deltas(current, reference, comparison_mode=comparison_mode) if current is not None else {},
        detail_reason_code=detail_reason_code,
        exclusion_reason=exclusion_reason,
        comparison_mode=comparison_mode,
    )


def compare_set_progress(
    current: SetPerformance,
    reference: SetPerformance | None,
    config: ProgressRuleConfig = DEFAULT_CONFIG,
    *,
    match_level: str = "direct",
    rpe_required_since: str = RPE_REQUIRED_SINCE,
) -> SetComparisonResult:
    current = normalize_set_performance(current) or current
    reference = normalize_set_performance(reference)
    if reference is None:
        return _result(None, False, match_level, "missing_reference", "Kein Referenzset gefunden.", current, None, exclusion_reason="missing_reference")
    if not _same_identity(current, reference):
        exclusion_reason = _identity_exclusion_reason(current, reference)
        return _result(
            None,
            False,
            match_level,
            exclusion_reason,
            "Nicht vergleichbar, weil Uebung oder Variante abweicht.",
            current,
            reference,
            exclusion_reason=exclusion_reason,
        )
    if current.rpe is None and not is_legacy_logged_at(current.logged_at, rpe_required_since=rpe_required_since):
        return _result(None, False, match_level, "missing_current_rpe", "Aktueller Satz hat keine RPE.", current, reference, exclusion_reason="missing_current_rpe")

    comparison_mode: Literal["rpe_adjusted", "legacy_raw_e1rm"] = (
        "rpe_adjusted" if current.rpe is not None and reference.rpe is not None else "legacy_raw_e1rm"
    )
    d = _deltas(current, reference, comparison_mode=comparison_mode)
    score_delta_pct = d.get("score_delta_pct")
    load_delta_pct = d.get("load_delta_pct")
    rpe_delta = d.get("rpe_delta")
    if score_delta_pct is None or load_delta_pct is None:
        return _result(None, False, match_level, "missing_performance_data", "Leistungsdaten reichen nicht fuer einen sicheren Vergleich.", current, reference, exclusion_reason="missing_performance_data")

    high_effort_penalty = comparison_mode == "rpe_adjusted" and bool(
        rpe_delta is not None
        and rpe_delta >= config.high_effort_rpe_delta
        and score_delta_pct < config.high_effort_progress_floor_pct
    )
    if comparison_mode == "rpe_adjusted" and (
        load_delta_pct >= config.load_progression_min_pct
        and score_delta_pct >= config.load_progression_score_floor_pct
        and rpe_delta is not None
        and rpe_delta <= config.load_progression_rpe_ceiling
        and not high_effort_penalty
    ):
        return _result("progress", True, match_level, "successful_load_progression", "Hoehere Last bei gehaltener effort-adjusted Leistung.", current, reference, detail_reason_code="successful_load_progression", comparison_mode=comparison_mode)
    if comparison_mode == "legacy_raw_e1rm" and (
        load_delta_pct >= config.load_progression_min_pct
        and score_delta_pct >= config.legacy_load_progression_score_floor_pct
    ):
        return _result("progress", True, match_level, "legacy_successful_load_progression", "Hoehere Last bei konservativ gehaltener roher e1RM-Leistung.", current, reference, detail_reason_code="legacy_successful_load_progression", comparison_mode=comparison_mode)
    if score_delta_pct >= config.progress_score_min_pct and not high_effort_penalty:
        reason = "performance_score_up" if comparison_mode == "rpe_adjusted" else "legacy_raw_e1rm_up"
        text = "Effort-adjusted Leistungswert ist relevant gestiegen." if comparison_mode == "rpe_adjusted" else "Roher e1RM-Leistungswert ist relevant gestiegen."
        return _result("progress", True, match_level, reason, text, current, reference, detail_reason_code=reason, comparison_mode=comparison_mode)
    reps_delta = d.get("reps_delta")
    effort_delta_covered_by_reps = bool(
        reps_delta is not None
        and rpe_delta is not None
        and reps_delta >= max(config.rep_progression_min_reps, max(0.0, rpe_delta))
    )
    if comparison_mode == "rpe_adjusted" and (
        effort_delta_covered_by_reps
        and load_delta_pct >= 0.0
        and score_delta_pct >= config.rep_progression_score_floor_pct
    ):
        return _result(
            "progress",
            True,
            match_level,
            "successful_rep_progression",
            "Mehr Wiederholungen bei gleicher oder hoeherer Last; der Rep-Zuwachs deckt den RPE-Anstieg mindestens ab.",
            current,
            reference,
            detail_reason_code="successful_rep_progression",
            comparison_mode=comparison_mode,
        )
    if score_delta_pct <= config.regress_score_max_pct:
        reason = "performance_score_down" if comparison_mode == "rpe_adjusted" else "legacy_raw_e1rm_down"
        text = "Effort-adjusted Leistungswert ist klar gesunken." if comparison_mode == "rpe_adjusted" else "Roher e1RM-Leistungswert ist klar gesunken."
        return _result("regress", True, match_level, reason, text, current, reference, detail_reason_code=reason, comparison_mode=comparison_mode)
    reason = "high_effort_progress_blocked" if high_effort_penalty else "performance_score_stable" if comparison_mode == "rpe_adjusted" else "legacy_raw_e1rm_stable"
    text = "Mehrleistung beruht auf deutlich hoeherer Ausbelastung." if high_effort_penalty else "Leistungswert liegt im stabilen Korridor." if comparison_mode == "rpe_adjusted" else "Roher e1RM-Leistungswert liegt im stabilen Korridor."
    return _result("stable", True, match_level, reason, text, current, reference, detail_reason_code=reason, comparison_mode=comparison_mode)


def choose_best_set(sets: list[SetPerformance | dict[str, Any]]) -> SetPerformance | None:
    normalized = [s for s in (normalize_set_performance(raw) for raw in sets) if s is not None]
    valid = [s for s in normalized if s.e1rm is not None and s.weight is not None and s.reps is not None]
    if not valid:
        return None

    def score(s: SetPerformance) -> tuple[float, float, float, int, float, int]:
        has_rpe = int(s.rpe is not None)
        rpe_score = -float(s.rpe) if s.rpe is not None else -8.0
        order_score = -int(s.order_idx or 0)
        return (float(s.e1rm or 0.0), float(s.weight or 0.0), float(s.reps or 0.0), has_rpe, rpe_score, order_score)

    return max(valid, key=score)


def classify_top_backoff_slots(
    sets: list[SetPerformance | dict[str, Any]],
    config: ProgressRuleConfig = DEFAULT_CONFIG,
) -> list[SetPerformance]:
    normalized = [s for s in (normalize_set_performance(raw) for raw in sets) if s is not None]
    weights = [float(s.weight) for s in normalized if s.weight is not None]
    if not weights:
        return [replace(s, tb_type=None, tb_slot=None) for s in normalized]
    top_weight = max(weights)
    cutoff = max(top_weight * (1.0 - float(config.top_backoff_pct)), top_weight - float(config.top_backoff_abs_threshold))
    counters = {"T": 0, "B": 0}
    out: list[SetPerformance] = []
    for s in normalized:
        if s.weight is None:
            out.append(replace(s, tb_type=None, tb_slot=None))
            continue
        tb_type: TbType = "T" if float(s.weight) >= cutoff else "B"
        counters[tb_type] += 1
        out.append(replace(s, tb_type=tb_type, tb_slot=counters[tb_type]))
    return out


def _identity_tuple(s: SetPerformance, fields: tuple[str, ...]) -> tuple[Any, ...]:
    values = []
    for field_name in fields:
        if field_name == "name":
            values.append(set_key_name(s))
        else:
            values.append(_norm(getattr(s, field_name)))
    return tuple(values)


def choose_best_reference_candidate(current: SetPerformance, candidates: list[SetPerformance]) -> SetPerformance | None:
    if not candidates:
        return None
    best_by_perf = choose_best_set(candidates)

    def score(c: SetPerformance) -> tuple[int, int, int, float, float, int]:
        same_tb = int(c.tb_type == current.tb_type and c.tb_slot == current.tb_slot and c.tb_type is not None)
        same_set_number = int(c.set_number is not None and current.set_number is not None and c.set_number == current.set_number)
        if current.tb_slot is not None and c.tb_slot is not None:
            slot_delta = abs(int(c.tb_slot) - int(current.tb_slot))
        else:
            slot_delta = 999
        reps_delta = abs(float(c.reps or 0.0) - float(current.reps or 0.0))
        weight_delta = abs(float(c.weight or 0.0) - float(current.weight or 0.0))
        best_set_bonus = int(best_by_perf is not None and c == best_by_perf)
        return (-same_tb, -same_set_number, slot_delta, reps_delta, weight_delta, -best_set_bonus)

    return min(candidates, key=score)


def find_reference_set(
    current: SetPerformance,
    previous_sessions: list[list[SetPerformance | dict[str, Any]]],
    config: ProgressRuleConfig = DEFAULT_CONFIG,
) -> tuple[SetPerformance | None, str]:
    del config
    current = normalize_set_performance(current) or current
    normalized_sessions = [
        [s for s in (normalize_set_performance(raw) for raw in session) if s is not None]
        for session in previous_sessions
    ]
    levels: list[tuple[str, tuple[str, ...], bool, bool]] = [
        ("exact_session_tb_slot", ("name", "device", "variation", "laterality", "session_name"), True, False),
        ("exact_tb_slot", ("name", "device", "variation", "laterality"), True, False),
        ("same_identity_set_number", ("name", "device", "variation", "laterality"), False, True),
        ("same_identity", ("name", "device", "variation", "laterality"), False, False),
    ]
    for distance, session in enumerate(reversed(normalized_sessions), start=1):
        for level_name, fields, require_tb, require_set_number in levels:
            cur_key = _identity_tuple(current, fields)
            if not cur_key or cur_key[0] == "":
                continue
            candidates = []
            for candidate in session:
                if _identity_tuple(candidate, fields) != cur_key:
                    continue
                if require_tb and not (
                    candidate.tb_type == current.tb_type
                    and candidate.tb_slot == current.tb_slot
                    and current.tb_type is not None
                    and current.tb_slot is not None
                ):
                    continue
                if require_set_number and not (
                    candidate.set_number is not None
                    and current.set_number is not None
                    and candidate.set_number == current.set_number
                ):
                    continue
                candidates.append(candidate)
            if candidates:
                picked = choose_best_reference_candidate(current, candidates)
                return picked, f"{level_name}:lookback_{distance}"
    return None, "no_match"


def summarize_session_progress(
    comparisons: list[SetComparisonResult],
    config: ProgressRuleConfig = DEFAULT_CONFIG,
) -> SessionProgressSummary:
    total = len(comparisons)
    comparable_items = [c for c in comparisons if c.comparable]
    comparable = len(comparable_items)
    progress = sum(1 for c in comparable_items if c.status == "progress")
    stable = sum(1 for c in comparable_items if c.status == "stable")
    regress = sum(1 for c in comparable_items if c.status == "regress")
    excluded = total - comparable
    coverage = (float(comparable) / float(total)) if total else None
    if comparable == 0:
        return SessionProgressSummary(
            total_sets=total,
            comparable_sets=comparable,
            progress=progress,
            stable=stable,
            regress=regress,
            excluded=excluded,
            progress_rate=None,
            regression_rate=None,
            net_progress=None,
            coverage=coverage,
            status="not_enough_data",
            display_status="neutral",
            reason_code="no_comparable_sets",
            reason_text="Keine vergleichbaren Sets vorhanden.",
        )
    progress_rate = float(progress) / float(comparable)
    regression_rate = float(regress) / float(comparable)
    net = float(progress - regress) / float(comparable)
    if net >= 0.25:
        status = "progress"
        display_status = "progress"
        reason_code = "session_net_progress_positive"
        reason_text = "Mehr verbesserte als verschlechterte vergleichbare Sets."
    elif net <= -0.25:
        status = "regress"
        display_status = "regress"
        reason_code = "session_net_progress_negative"
        reason_text = "Mehr verschlechterte als verbesserte vergleichbare Sets."
    else:
        status = "stable"
        display_status = "neutral"
        reason_code = "session_stable"
        reason_text = "Session liegt insgesamt stabil im Vergleich."
    return SessionProgressSummary(
        total_sets=total,
        comparable_sets=comparable,
        progress=progress,
        stable=stable,
        regress=regress,
        excluded=excluded,
        progress_rate=progress_rate,
        regression_rate=regression_rate,
        net_progress=net,
        coverage=coverage,
        status=status,
        display_status=display_status,
        reason_code=reason_code,
        reason_text=reason_text,
    )


def evaluate_plan_target_status(set_data: dict[str, Any], target: dict[str, Any] | None) -> tuple[str, str]:
    reps = _to_float(set_data.get("reps"))
    weight = _to_float(set_data.get("weight"))
    rpe = _to_float(set_data.get("rpe"))
    if reps is None or weight is None:
        return "ok", "missing data"
    if not target:
        return "ok", ""
    reps_min = _to_float(target.get("reps_min"))
    reps_max = _to_float(target.get("reps_max"))
    rpe_min = _to_float(target.get("rpe_min"))
    rpe_max = _to_float(target.get("rpe_max"))
    if reps_min is not None and reps < reps_min:
        return "bad", "below reps min"
    if rpe_max is not None and rpe is not None and rpe > rpe_max:
        return "bad", "above rpe max"
    in_reps = reps_min is not None and reps_max is not None and reps_min <= reps <= reps_max
    in_rpe = rpe_min is not None and rpe_max is not None and rpe is not None and rpe_min <= rpe <= rpe_max
    if in_reps and (in_rpe or rpe is None or rpe_min is None or rpe_max is None):
        return "ok", "on target"
    if reps_max is not None and reps > reps_max and (rpe_max is None or rpe is None or rpe <= rpe_max):
        return "good", "above reps target"
    if rpe_min is not None and rpe is not None and rpe < rpe_min and (reps_min is None or reps >= reps_min):
        return "good", "easier than target"
    return "ok", ""
