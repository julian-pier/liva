from __future__ import annotations

"""Training load, observed performance and transparent response modelling.

The module is deliberately read-only and independent from Flask.  Every load
value depends only on the sets logged on that date.  Observed performance uses
only exact exercise identities and baselines made from earlier observations.
"""

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
import math
from statistics import median
import sqlite3
from typing import Any, Iterable

from analysis.progression_rules import calculate_effort_adjusted_e1rm
from analysis.training_progress import _canonical_aliases, _load_sessions, _slot_sets


VERSION = "training_response_v1"
SCOPE_ORDER = ("overall", "chest", "back", "legs", "shoulders", "biceps", "triceps")
SCOPE_LABELS = {
    "overall": "Gesamt",
    "chest": "Brust",
    "back": "Rücken",
    "legs": "Beine",
    "shoulders": "Schulter",
    "biceps": "Bizeps",
    "triceps": "Trizeps",
}

BASELINE_DAYS = 42
MIN_BASELINE_POINTS = 3
MIN_CALIBRATION_POINTS = 12
DEFAULT_RPE_WEIGHT = 0.65
REGIME_RATIO_MIN = 0.55
REGIME_RATIO_MAX = 1.80
REGIME_CONFIRM_RATIO_MIN = 0.65
REGIME_CONFIRM_RATIO_MAX = 1.55


def _norm(value: Any) -> str:
    return " ".join(str(value or "").strip().lower().split())


def _laterality(value: Any) -> str:
    normalized = _norm(value)
    if normalized in {"", "both", "bilat", "bilateral"}:
        return "bilateral"
    if normalized in {"unilat", "unilateral"}:
        return "unilateral"
    return normalized


@dataclass(frozen=True)
class ExerciseIdentity:
    name: str
    variation: str
    device: str
    laterality: str = "bilateral"
    execution_mode: str = ""

    @classmethod
    def make(
        cls,
        name: str,
        variation: str = "",
        device: str = "",
        laterality: str = "bilateral",
        execution_mode: str = "",
    ) -> "ExerciseIdentity":
        return cls(_norm(name), _norm(variation), _norm(device), _laterality(laterality), _norm(execution_mode))

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "ExerciseIdentity":
        return cls.make(
            row.get("exercise_name") or row.get("name") or row.get("canonical_exercise_id"),
            row.get("variation_id") or row.get("variation"),
            row.get("device"),
            row.get("laterality"),
            row.get("execution_mode"),
        )

    @property
    def key(self) -> str:
        return "|".join((self.name, self.variation, self.device, self.laterality, self.execution_mode))


def _ids(name: str, combos: Iterable[tuple[str, str, str]]) -> list[ExerciseIdentity]:
    return [ExerciseIdentity.make(name, variation, device, laterality) for variation, device, laterality in combos]


# Exact combinations present in the athlete's training history. New combinations
# are intentionally not inferred from broad keywords; they appear in the
# unmapped diagnostics until this table is extended deliberately.
_PROFILE_ROWS: list[tuple[dict[str, float], list[ExerciseIdentity]]] = [
    ({"shoulders": 1.0}, _ids("Seitheben", [("SZ", "SZ", "unilateral"), ("KH", "KH", "bilateral")])),
    ({"back": 1.0, "biceps": 0.30}, _ids("breite Rows", [("eGym", "eGym", "bilateral"), ("Smith", "Smith", "bilateral"), ("KH", "KH", "bilateral")])),
    ({"legs": 1.0}, _ids("Beinbeuger", [("eGym", "eGym", "bilateral")])),
    ({"chest": 1.0, "triceps": 0.50, "shoulders": 0.30}, _ids("Schrägbankdrücken", [("KH", "KH", "bilateral"), ("Smith", "Smith", "bilateral")])),
    ({"triceps": 1.0}, _ids("Katanas", [("SZ", "SZ", "unilateral")])),
    ({"chest": 1.0, "triceps": 0.60, "shoulders": 0.25}, _ids("Dips", [("weighted", "BW", "bilateral"), ("weighted", "", "bilateral")])),
    ({"chest": 1.0}, _ids("Flys", [("SZ", "SZ", "bilateral")])),
    ({"shoulders": 1.0, "back": 0.35}, _ids("reversed Flys", [("SZ", "SZ", "unilateral")]) + _ids("reverse Flys", [("", "", "bilateral")])),
    ({"back": 1.0, "biceps": 0.35}, _ids("PullUps", [("weighted", "BW", "bilateral")]) + _ids("ChinUps", [("weighted", "BW", "bilateral"), ("weighted", "", "bilateral")])),
    ({"back": 1.0, "biceps": 0.30}, _ids("enge Rows", [("KH", "KH", "bilateral")]) + _ids("HighRows", [("SZ", "SZ", "bilateral")]) + _ids("T-Bar Row", [("LH", "LH", "bilateral")])),
    ({"triceps": 1.0}, _ids("Pushdowns", [("unilat", "unilat", "bilateral"), ("bilat", "bilat", "bilateral")])),
    ({"back": 1.0, "biceps": 0.35}, _ids("Latzug", [("eGym", "eGym", "bilateral")]) + _ids("Pullover", [("SZ", "SZ", "bilateral")])),
    ({"legs": 1.0, "back": 0.20}, _ids("RDLs", [("LH", "LH", "bilateral")])),
    ({"chest": 1.0, "triceps": 0.50, "shoulders": 0.30}, _ids("Bench", [("LH", "LH", "bilateral"), ("Flat", "barbell", "bilateral")]) + _ids("Brustpresse", [("eGym", "eGym", "bilateral")])),
    ({"shoulders": 1.0, "triceps": 0.40}, _ids("OHP", [("Smith", "Smith", "bilateral"), ("KH", "KH", "bilateral"), ("LH", "LH", "bilateral"), ("eGym", "eGym", "bilateral")])),
    ({"biceps": 1.0}, _ids("Hammers", [("SZ", "SZ", "bilateral"), ("KH", "KH", "bilateral")]) + _ids("sup. Curls", [("KH", "KH", "bilateral")]) + _ids("Incline Curls", [("KH", "KH", "bilateral")]) + _ids("Curls", [("KH", "KH", "bilateral"), ("LH", "LH", "bilateral"), ("SZ", "SZ", "bilateral")]) + _ids("Preachers", [("KH", "KH", "bilateral"), ("SZ", "SZ", "bilateral"), ("SZ", "SZ", "unilateral")]) + _ids("Bayesians", [("SZ", "SZ", "bilateral")]) + _ids("Cable Curls", [("SZ", "SZ", "bilateral")])),
    ({"shoulders": 1.0}, _ids("Carters", [("SZ", "SZ", "unilateral")]) + _ids("Upright Rows", [("Smith", "Smith", "bilateral")]) + _ids("Y-Raises", [("SZ", "SZ", "bilateral")]) + _ids("Rotator", [("gym80", "maschine", "unilateral")])),
    ({"legs": 1.0}, _ids("Adduktoren", [("eGym", "eGym", "bilateral")]) + _ids("Wadenheben", [("unilat", "", "unilateral"), ("gym80", "bilat", "bilateral"), ("gym80", "unilat", "unilateral"), ("unilat", "unilat", "unilateral")]) + _ids("Beinpresse", [("unilat", "", "unilateral"), ("unilat", "eGym", "unilateral"), ("bilat", "", "bilateral")]) + _ids("Beinstrecker", [("bilat", "", "unilateral"), ("unilat", "eGym", "unilateral"), ("unilat", "", "unilateral"), ("eGym", "eGym", "unilateral")]) + _ids("Squats", [("LH", "LH", "bilateral"), ("Smith", "Smith", "bilateral")])),
    ({"chest": 1.0}, _ids("Incline Flys", [("SZ", "SZ", "bilateral")])),
    ({"triceps": 1.0}, _ids("French Press", [("Smith", "Smith", "bilateral")])),
]

EXERCISE_MUSCLE_SHARES: dict[ExerciseIdentity, dict[str, float]] = {
    identity: dict(shares)
    for shares, identities in _PROFILE_ROWS
    for identity in identities
}


# Each entry remains a separate identity and receives its own rolling baseline.
REFERENCE_IDENTITIES: dict[str, tuple[ExerciseIdentity, ...]] = {
    "chest": tuple(_ids("Bench", [("LH", "LH", "bilateral"), ("Flat", "barbell", "bilateral")]) + _ids("Schrägbankdrücken", [("Smith", "Smith", "bilateral"), ("KH", "KH", "bilateral")])),
    "back": tuple(_ids("breite Rows", [("eGym", "eGym", "bilateral")]) + _ids("enge Rows", [("KH", "KH", "bilateral")]) + _ids("Latzug", [("eGym", "eGym", "bilateral")])),
    "legs": tuple(_ids("RDLs", [("LH", "LH", "bilateral")]) + _ids("Beinpresse", [("unilat", "", "unilateral"), ("unilat", "eGym", "unilateral")]) + _ids("Beinbeuger", [("eGym", "eGym", "bilateral")])),
    "shoulders": tuple(_ids("OHP", [("Smith", "Smith", "bilateral"), ("KH", "KH", "bilateral"), ("LH", "LH", "bilateral")]) + _ids("Seitheben", [("SZ", "SZ", "unilateral")])),
    "biceps": tuple(_ids("sup. Curls", [("KH", "KH", "bilateral")]) + _ids("Hammers", [("SZ", "SZ", "bilateral")]) + _ids("Incline Curls", [("KH", "KH", "bilateral")])),
    "triceps": tuple(_ids("Pushdowns", [("unilat", "unilat", "bilateral"), ("bilat", "bilat", "bilateral")]) + _ids("Katanas", [("SZ", "SZ", "unilateral")]) + _ids("French Press", [("Smith", "Smith", "bilateral")])),
}


@dataclass(frozen=True)
class ResponseParams:
    adaptation_amplitude: float = 0.32
    fatigue_amplitude: float = 0.50
    adaptation_tau_days: float = 20.0
    fatigue_tau_days: float = 3.5


DEFAULT_PARAMS = ResponseParams()
DEFAULT_PARAMS_BY_SCOPE = {
    "overall": ResponseParams(0.10, 0.18, 20.0, 3.5),
    **{scope: DEFAULT_PARAMS for scope in SCOPE_ORDER if scope != "overall"},
}


def rpe_load_weight(rpe: float | None) -> float:
    if rpe is None:
        return DEFAULT_RPE_WEIGHT
    try:
        value = float(rpe)
    except (TypeError, ValueError):
        return DEFAULT_RPE_WEIGHT
    anchors = ((6.0, 0.35), (7.0, 0.65), (8.0, 1.0), (9.0, 1.15), (10.0, 1.25))
    if value <= anchors[0][0]:
        return anchors[0][1]
    if value >= anchors[-1][0]:
        return anchors[-1][1]
    for (x0, y0), (x1, y1) in zip(anchors, anchors[1:]):
        if x0 <= value <= x1:
            fraction = (value - x0) / (x1 - x0)
            return y0 + fraction * (y1 - y0)
    return DEFAULT_RPE_WEIGHT


def muscle_shares_for_row(row: dict[str, Any]) -> dict[str, float] | None:
    shares = EXERCISE_MUSCLE_SHARES.get(ExerciseIdentity.from_row(row))
    return dict(shares) if shares else None


def _valid_work_set(row: dict[str, Any]) -> bool:
    try:
        reps = float(row.get("reps"))
    except (TypeError, ValueError):
        return False
    if not math.isfinite(reps) or reps <= 0:
        return False
    if row.get("is_warmup") in (True, 1, "1") or row.get("inferred_warmup") is True:
        return False
    if row.get("is_working_set") in (False, 0, "0"):
        return False
    return True


def _session_performance(rows: list[dict[str, Any]]) -> float | None:
    scores: list[float] = []
    for row in rows:
        if not _valid_work_set(row):
            continue
        if row.get("intentional_deload") or row.get("technique_set"):
            continue
        score = calculate_effort_adjusted_e1rm(row.get("weight"), row.get("reps"), row.get("rpe"))
        if score is not None and math.isfinite(float(score)) and float(score) > 0:
            scores.append(float(score))
    if not scores:
        return None
    top = sorted(scores, reverse=True)[:2]
    return float(median(top))


def _daterange(start: date, end: date) -> Iterable[date]:
    current = start
    while current <= end:
        yield current
        current += timedelta(days=1)


def impulse_response(days_since_load: int | float, params: ResponseParams = DEFAULT_PARAMS) -> float:
    d = max(0.0, float(days_since_load))
    return (
        params.adaptation_amplitude * math.exp(-d / params.adaptation_tau_days)
        - params.fatigue_amplitude * math.exp(-d / params.fatigue_tau_days)
    )


def model_potential(loads: list[float], params: ResponseParams = DEFAULT_PARAMS) -> list[float]:
    adaptation_state = 0.0
    fatigue_state = 0.0
    adaptation_decay = math.exp(-1.0 / params.adaptation_tau_days)
    fatigue_decay = math.exp(-1.0 / params.fatigue_tau_days)
    out: list[float] = []
    for raw_load in loads:
        load = min(max(0.0, float(raw_load or 0.0)), 20.0)
        adaptation_state = adaptation_state * adaptation_decay + load
        fatigue_state = fatigue_state * fatigue_decay + load
        effect = params.adaptation_amplitude * adaptation_state - params.fatigue_amplitude * fatigue_state
        out.append(round(max(80.0, min(120.0, 100.0 + effect)), 3))
    return out


def _prediction_before_session(loads: list[float], index: int, params: ResponseParams) -> float:
    effect = 0.0
    for j in range(index):
        load = float(loads[j] or 0.0)
        if load > 0:
            effect += min(load, 20.0) * impulse_response(index - j, params)
    return 100.0 + effect


def _prior_predictions(loads: list[float], params: ResponseParams) -> list[float]:
    """Potential before each day's session, computed in linear time."""
    adaptation_state = 0.0
    fatigue_state = 0.0
    adaptation_decay = math.exp(-1.0 / params.adaptation_tau_days)
    fatigue_decay = math.exp(-1.0 / params.fatigue_tau_days)
    predictions: list[float] = []
    for raw_load in loads:
        adaptation_state *= adaptation_decay
        fatigue_state *= fatigue_decay
        predictions.append(100.0 + params.adaptation_amplitude * adaptation_state - params.fatigue_amplitude * fatigue_state)
        load = min(max(0.0, float(raw_load or 0.0)), 20.0)
        adaptation_state += load
        fatigue_state += load
    return predictions


def _previous_load_index(loads: list[float], index: int) -> int | None:
    for previous in range(index - 1, max(-1, index - 11), -1):
        if float(loads[previous] or 0.0) >= 0.65:
            return previous
    return None


def _fit_params(
    loads: list[float],
    performance: list[float | None],
    performance_details: dict[str, list[dict[str, Any]]],
    dates: list[str],
    default_params: ResponseParams = DEFAULT_PARAMS,
) -> tuple[ResponseParams, bool, float | None, int]:
    points = [(i, float(v)) for i, v in enumerate(performance) if isinstance(v, (int, float))]
    cycle_points = [(i, v) for i, v in points if _previous_load_index(loads, i) is not None]
    reference_keys = {
        str(detail.get("identity_key") or detail.get("scope") or "")
        for details in performance_details.values()
        for detail in details
        if detail.get("identity_key") or detail.get("scope")
    }
    prior_load_dates = {_previous_load_index(loads, i) for i, _ in cycle_points}
    sufficient = (
        len(cycle_points) >= MIN_CALIBRATION_POINTS
        and len(reference_keys) >= 2
        and len({idx for idx in prior_load_dates if idx is not None}) >= 6
    )
    if not sufficient:
        return default_params, False, None, len(cycle_points)

    candidates: list[ResponseParams] = []
    for adaptation in tuple(default_params.adaptation_amplitude * factor for factor in (0.75, 1.0, 1.25)):
        for fatigue in tuple(default_params.fatigue_amplitude * factor for factor in (0.85, 1.0, 1.15)):
            if fatigue <= adaptation:
                continue
            for adaptation_tau in (14.0, 20.0, 28.0):
                for fatigue_tau in (2.0, 3.5, 5.0):
                    if fatigue_tau >= adaptation_tau:
                        continue
                    candidates.append(ResponseParams(adaptation, fatigue, adaptation_tau, fatigue_tau))

    best = default_params
    best_error = float("inf")
    for candidate in candidates:
        predictions = _prior_predictions(loads, candidate)
        residuals = [abs(value - predictions[i]) for i, value in cycle_points]
        error = float(median(residuals)) if residuals else float("inf")
        if error < best_error - 1e-9:
            best = candidate
            best_error = error
    return best, True, (best_error if math.isfinite(best_error) else None), len(cycle_points)


def _confidence(
    *,
    calibrated: bool,
    points: int,
    cycle_points: int,
    median_error: float | None,
    missing_rpe_rate: float,
) -> dict[str, Any]:
    if points <= 0:
        return {"label": "none", "score": 0.0, "reason": "Keine validen Referenzmessungen."}
    if not calibrated:
        score = min(0.38, 0.12 + points / 60.0) * max(0.65, 1.0 - missing_rpe_rate)
        return {
            "label": "low",
            "score": round(score, 3),
            "reason": f"Defaultmodell; {cycle_points}/{MIN_CALIBRATION_POINTS} vergleichbare Reaktionspunkte.",
        }
    error_factor = max(0.0, min(1.0, 1.0 - float(median_error or 0.0) / 8.0))
    sample_factor = min(1.0, cycle_points / 24.0)
    score = (0.45 + 0.30 * sample_factor + 0.25 * error_factor) * max(0.7, 1.0 - missing_rpe_rate)
    label = "high" if score >= 0.78 else "medium" if score >= 0.55 else "low"
    return {
        "label": label,
        "score": round(score, 3),
        "reason": f"Begrenzt kalibriert mit {cycle_points} Reaktionspunkten; medianer Fehler {float(median_error or 0.0):.1f} Punkte.",
    }


def _observed_window(loads: list[float], performance: list[float | None]) -> dict[str, Any]:
    cycles: list[tuple[int, float]] = []
    for i, value in enumerate(performance):
        if not isinstance(value, (int, float)):
            continue
        previous = _previous_load_index(loads, i)
        if previous is None:
            continue
        cycles.append((i - previous, float(value)))
    positive = [(delay, value) for delay, value in cycles if value > 100.0]
    baseline_returns = sorted(delay for delay, value in cycles if 99.0 <= value <= 101.5)
    if len(cycles) < 4 or len(positive) < 3:
        return {
            "cycles": len(cycles),
            "window_hours": None,
            "baseline_return_hours": None,
            "confidence": "low" if cycles else "none",
            "statement": f"Noch zu wenig vergleichbare Daten für ein beobachtetes Leistungsfenster · Grundlage: {len(cycles)} Zyklen.",
        }
    best_value = max(value for _, value in positive)
    relevant_delays = sorted(delay for delay, value in positive if value >= best_value - 2.0)
    low_days = relevant_delays[max(0, int((len(relevant_delays) - 1) * 0.25))]
    high_days = relevant_delays[min(len(relevant_delays) - 1, int((len(relevant_delays) - 1) * 0.75))]
    confidence = "high" if len(cycles) >= 18 else "medium" if len(cycles) >= 8 else "low"
    confidence_label = {"high": "hoch", "medium": "mittel", "low": "niedrig"}[confidence]
    baseline_return_hours = int(median(baseline_returns) * 24) if len(baseline_returns) >= 3 else None
    low_hours = int(low_days * 24)
    high_hours = int(high_days * 24)
    window_text = f"{low_hours} Stunden" if low_hours == high_hours else f"{low_hours}–{high_hours} Stunden"
    return {
        "cycles": len(cycles),
        "window_hours": [low_hours, high_hours],
        "baseline_return_hours": baseline_return_hours,
        "confidence": confidence,
        "statement": (
            f"Beste gemessene Reaktionen lagen häufig nach {window_text}"
            f" · Grundlage: {len(cycles)} vergleichbare Zyklen · Sicherheit: {confidence_label}."
        ),
    }


def measured_performance_trend(
    performance: list[float | None],
    window: int = 5,
    smoothing: float = 0.35,
) -> list[float | None]:
    """Trailing robust median plus EMA, emitted at measurement dates only."""
    recent: list[float] = []
    trend: list[float | None] = []
    smoothed: float | None = None
    for value in performance:
        if not isinstance(value, (int, float)) or not math.isfinite(float(value)):
            trend.append(None)
            continue
        recent.append(float(value))
        robust_value = float(median(recent[-max(1, window):]))
        smoothed = robust_value if smoothed is None else smoothing * robust_value + (1.0 - smoothing) * smoothed
        trend.append(round(smoothed, 3))
    return trend


def _empty_scope(scope: str) -> dict[str, Any]:
    return {
        "key": scope,
        "label": SCOPE_LABELS[scope],
        "load": [],
        "load_details": {},
        "performance": [],
        "performance_trend": [],
        "performance_details": {},
        "potential": [],
        "confidence": {"label": "none", "score": 0.0, "reason": "Keine Daten."},
        "observed_response": {"cycles": 0, "window_hours": None, "baseline_return_hours": None, "confidence": "none", "statement": "Noch keine Daten."},
        "model": {"calibrated": False, "params": DEFAULT_PARAMS.__dict__, "median_error": None},
    }


def compute_training_response_payload(conn: sqlite3.Connection, *, end_iso: str | None = None) -> dict[str, Any]:
    sessions = _load_sessions(conn, end_iso=end_iso)
    if not sessions:
        return {
            "version": VERSION,
            "dates": [],
            "scopes": {scope: _empty_scope(scope) for scope in SCOPE_ORDER},
            "meta": {
                "scope_order": list(SCOPE_ORDER),
                "units": {"load": "gewichtete Arbeitssätze", "performance": "Baseline-Index"},
                "unmapped_exercises": [],
                "measurement_regime_resets": [],
            },
        }

    aliases = _canonical_aliases(conn)
    start = min(date.fromisoformat(session["session_date"]) for session in sessions)
    last = max(date.fromisoformat(session["session_date"]) for session in sessions)
    requested_end = date.fromisoformat(end_iso[:10]) if end_iso else max(date.today(), last)
    end = max(last, requested_end)
    dates = [day.isoformat() for day in _daterange(start, end)]
    index_by_date = {day: idx for idx, day in enumerate(dates)}

    loads = {scope: [0.0 for _ in dates] for scope in SCOPE_ORDER}
    breakdown: dict[str, dict[str, Counter[str]]] = {
        scope: defaultdict(Counter) for scope in SCOPE_ORDER
    }
    mapped_set_count = Counter()
    missing_rpe_count = Counter()
    unmapped = Counter()
    performance_sessions: dict[ExerciseIdentity, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))

    references_by_identity: dict[ExerciseIdentity, set[str]] = defaultdict(set)
    for scope, identities in REFERENCE_IDENTITIES.items():
        for identity in identities:
            references_by_identity[identity].add(scope)

    for session in sessions:
        day = session["session_date"]
        day_index = index_by_date[day]
        slotted = _slot_sets(session["sets"], aliases)
        rows_by_identity: dict[ExerciseIdentity, list[dict[str, Any]]] = defaultdict(list)
        for row in slotted:
            if not _valid_work_set(row):
                continue
            identity = ExerciseIdentity.from_row(row)
            shares = EXERCISE_MUSCLE_SHARES.get(identity)
            if not shares:
                unmapped[(identity.name, identity.variation, identity.device, identity.laterality)] += 1
                continue
            rows_by_identity[identity].append(row)
            set_load = rpe_load_weight(row.get("rpe"))
            mapped_set_count["overall"] += 1
            if row.get("rpe") is None:
                missing_rpe_count["overall"] += 1
            loads["overall"][day_index] += set_load
            breakdown["overall"][day][identity.name] += set_load
            for scope, share in shares.items():
                contribution = set_load * float(share)
                loads[scope][day_index] += contribution
                breakdown[scope][day][identity.name] += contribution
                mapped_set_count[scope] += 1
                if row.get("rpe") is None:
                    missing_rpe_count[scope] += 1

        for identity, rows in rows_by_identity.items():
            if identity not in references_by_identity:
                continue
            score = _session_performance(rows)
            if score is not None:
                performance_sessions[identity][day].append(score)

    variant_points: dict[ExerciseIdentity, dict[str, tuple[float, float, float]]] = defaultdict(dict)
    measurement_regime_resets: list[dict[str, str]] = []
    for identity, per_day in performance_sessions.items():
        observations: list[tuple[date, float]] = []
        pending_regime: list[tuple[date, float]] = []
        for day in sorted(per_day):
            current_date = date.fromisoformat(day)
            session_score = float(median(sorted(per_day[day], reverse=True)[:2]))
            history = [score for prior_day, score in observations if current_date - timedelta(days=BASELINE_DAYS) <= prior_day < current_date]
            if len(history) >= MIN_BASELINE_POINTS:
                historical_median = float(median(history))
                ratio = session_score / historical_median if historical_median > 0 else 1.0
                if ratio < REGIME_RATIO_MIN or ratio > REGIME_RATIO_MAX:
                    if pending_regime:
                        pending_median = float(median(score for _, score in pending_regime))
                        confirmation_ratio = session_score / pending_median if pending_median > 0 else 1.0
                        if REGIME_CONFIRM_RATIO_MIN <= confirmation_ratio <= REGIME_CONFIRM_RATIO_MAX:
                            observations = list(pending_regime)
                            measurement_regime_resets.append({"identity_key": identity.key, "confirmed_on": day})
                            pending_regime = []
                            history = [score for prior_day, score in observations if current_date - timedelta(days=BASELINE_DAYS) <= prior_day < current_date]
                        else:
                            pending_regime = [(current_date, session_score)]
                            continue
                    else:
                        pending_regime = [(current_date, session_score)]
                        continue
                else:
                    pending_regime = []
            if len(history) >= MIN_BASELINE_POINTS:
                baseline = float(median(history))
                if baseline > 0:
                    variant_points[identity][day] = (100.0 * session_score / baseline, session_score, baseline)
            observations.append((current_date, session_score))

    scope_performance: dict[str, list[float | None]] = {scope: [None for _ in dates] for scope in SCOPE_ORDER}
    scope_performance_details: dict[str, dict[str, list[dict[str, Any]]]] = {scope: defaultdict(list) for scope in SCOPE_ORDER}
    for scope, identities in REFERENCE_IDENTITIES.items():
        day_values: dict[str, list[float]] = defaultdict(list)
        for identity in identities:
            for day, (value, score, baseline) in variant_points.get(identity, {}).items():
                day_values[day].append(value)
                scope_performance_details[scope][day].append({
                    "exercise": identity.name,
                    "variation": identity.variation,
                    "device": identity.device,
                    "identity_key": identity.key,
                    "index": round(value, 3),
                    "delta_pct": round(value - 100.0, 3),
                    "measured_score": round(score, 3),
                    "baseline_score": round(baseline, 3),
                    "baseline_days": BASELINE_DAYS,
                })
        for day, values in day_values.items():
            scope_performance[scope][index_by_date[day]] = round(float(median(values)), 3)

    for i, day in enumerate(dates):
        area_values = [
            float(scope_performance[scope][i])
            for scope in SCOPE_ORDER[1:]
            if isinstance(scope_performance[scope][i], (int, float))
        ]
        if area_values:
            scope_performance["overall"][i] = round(float(median(area_values)), 3)
            for scope in SCOPE_ORDER[1:]:
                if isinstance(scope_performance[scope][i], (int, float)):
                    scope_performance_details["overall"][day].append({
                        "scope": scope,
                        "label": SCOPE_LABELS[scope],
                        "index": scope_performance[scope][i],
                    })

    scopes: dict[str, dict[str, Any]] = {}
    for scope in SCOPE_ORDER:
        rounded_loads = [round(float(value), 3) for value in loads[scope]]
        details = {
            day: [
                {"exercise": exercise, "load": round(float(value), 3)}
                for exercise, value in values.most_common()
            ]
            for day, values in breakdown[scope].items()
            if values
        }
        performance = scope_performance[scope]
        perf_details = dict(scope_performance_details[scope])
        params, calibrated, median_error, cycle_points = _fit_params(
            rounded_loads,
            performance,
            perf_details,
            dates,
            default_params=DEFAULT_PARAMS_BY_SCOPE[scope],
        )
        points = sum(1 for value in performance if isinstance(value, (int, float)))
        total_mapped = int(mapped_set_count[scope])
        missing_rate = (int(missing_rpe_count[scope]) / total_mapped) if total_mapped else 0.0
        confidence = _confidence(
            calibrated=calibrated,
            points=points,
            cycle_points=cycle_points,
            median_error=median_error,
            missing_rpe_rate=missing_rate,
        )
        scopes[scope] = {
            "key": scope,
            "label": SCOPE_LABELS[scope],
            "load": rounded_loads,
            "load_details": details,
            "performance": performance,
            "performance_trend": measured_performance_trend(performance),
            "performance_details": perf_details,
            "potential": model_potential(rounded_loads, params),
            "confidence": confidence,
            "observed_response": _observed_window(rounded_loads, performance),
            "model": {
                "calibrated": calibrated,
                "params": {
                    "adaptation_amplitude": params.adaptation_amplitude,
                    "fatigue_amplitude": params.fatigue_amplitude,
                    "adaptation_tau_days": params.adaptation_tau_days,
                    "fatigue_tau_days": params.fatigue_tau_days,
                },
                "median_error": round(float(median_error), 3) if median_error is not None else None,
                "performance_points": points,
                "cycle_points": cycle_points,
            },
            "data_quality": {
                "mapped_sets": total_mapped,
                "missing_rpe_sets": int(missing_rpe_count[scope]),
                "missing_rpe_rate": round(missing_rate, 4),
            },
        }

    unmapped_rows = [
        {"name": name, "variation": variation, "device": device, "laterality": laterality, "sets": count}
        for (name, variation, device, laterality), count in unmapped.most_common(20)
    ]
    return {
        "version": VERSION,
        "dates": dates,
        "scopes": scopes,
        "meta": {
            "scope_order": list(SCOPE_ORDER),
            "scope_labels": SCOPE_LABELS,
            "units": {"load": "gewichtete Arbeitssätze", "performance": "Baseline-Index (100 = vorherige 42 Tage)"},
            "baseline_days": BASELINE_DAYS,
            "minimum_baseline_points": MIN_BASELINE_POINTS,
            "minimum_calibration_points": MIN_CALIBRATION_POINTS,
            "unmapped_exercises": unmapped_rows,
            "measurement_regime_resets": measurement_regime_resets,
            "reference_identities": {
                scope: [identity.key for identity in identities]
                for scope, identities in REFERENCE_IDENTITIES.items()
            },
        },
    }
