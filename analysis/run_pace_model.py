from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import date
from typing import Any, Iterable

from database.connections import get_runs_db


MODEL_VERSION = "hr_speed_v1"
RUN_SPORTS = {"", "run", "running", "trail_run", "trail running", "trailrun"}


@dataclass(frozen=True)
class HrPaceModel:
    intercept_mps: float
    slope_mps_per_bpm: float
    max_hr_bpm: float
    min_sample_hr: float
    max_sample_hr: float
    sample_count: int
    latest_run_date: str
    signature: str


def _day(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value or "")[:10])
    except ValueError:
        return None


def _weighted_fit(samples: list[tuple[float, float, float]]) -> tuple[float, float] | None:
    weight = sum(item[2] for item in samples)
    if weight <= 0:
        return None
    mean_hr = sum(hr * w for hr, _speed, w in samples) / weight
    mean_speed = sum(speed * w for _hr, speed, w in samples) / weight
    variance = sum(w * (hr - mean_hr) ** 2 for hr, _speed, w in samples)
    if variance <= 0:
        return None
    slope = sum(w * (hr - mean_hr) * (speed - mean_speed) for hr, speed, w in samples) / variance
    if not 0.005 <= slope <= 0.08:
        return None
    return mean_speed - slope * mean_hr, slope


def fit_hr_pace_model(rows: Iterable[dict[str, Any]], *, today: date | None = None) -> HrPaceModel | None:
    """Fit a robust, recency-weighted athlete model from real run HR/pace pairs."""
    today = today or date.today()
    accepted: list[tuple[float, float, float, str, float]] = []
    max_hr_values: list[float] = []
    signature_rows: list[tuple[Any, ...]] = []
    for raw in rows:
        row = dict(raw)
        run_day = _day(row.get("date"))
        sport = str(row.get("sport_type") or "").strip().lower()
        try:
            pace = float(row.get("pace") or 0)
            avg_hr = float(row.get("avg_hr") or 0)
            distance = float(row.get("distance") or 0)
            moving_time = float(row.get("moving_time") or 0)
            max_hr = float(row.get("max_hr") or 0)
        except (TypeError, ValueError):
            continue
        if not run_day or sport not in RUN_SPORTS or not (180 <= pace <= 600 and 90 <= avg_hr <= 210 and distance >= 2000 and moving_time >= 600):
            continue
        age_days = max(0, (today - run_day).days)
        if age_days > 730:
            continue
        recency = 0.5 ** (age_days / 180)
        distance_weight = max(0.5, min(2.0, math.sqrt(distance / 5000)))
        accepted.append((avg_hr, 1000 / pace, recency * distance_weight, run_day.isoformat(), pace))
        if 120 <= max_hr <= 220:
            max_hr_values.append(max_hr)
        signature_rows.append((run_day.isoformat(), round(distance), round(moving_time), round(avg_hr, 1), round(max_hr, 1), round(pace, 2)))
    if len(accepted) < 4 or max(hr for hr, *_ in accepted) - min(hr for hr, *_ in accepted) < 10:
        return None
    samples = [(hr, speed, weight) for hr, speed, weight, _day_value, _pace in accepted]
    fitted = _weighted_fit(samples)
    if not fitted:
        return None
    intercept, slope = fitted
    residuals = [abs(pace - 1000 / max(0.1, intercept + slope * hr)) for hr, _speed, _weight, _day_value, pace in accepted]
    median_residual = sorted(residuals)[len(residuals) // 2]
    trimmed = [sample for sample, residual in zip(samples, residuals) if residual <= max(30.0, median_residual * 2.5)]
    if len(trimmed) >= 4:
        fitted = _weighted_fit(trimmed)
        if fitted:
            intercept, slope = fitted
    observed_max = max(max_hr_values or [max(hr for hr, *_ in accepted) + 10])
    max_hr_bpm = max(max(hr for hr, *_ in accepted) + 5, min(205.0, observed_max))
    signature = hashlib.sha256(json.dumps(sorted(signature_rows), separators=(",", ":")).encode()).hexdigest()[:16]
    return HrPaceModel(
        intercept_mps=intercept,
        slope_mps_per_bpm=slope,
        max_hr_bpm=max_hr_bpm,
        min_sample_hr=min(hr for hr, *_ in accepted),
        max_sample_hr=max(hr for hr, *_ in accepted),
        sample_count=len(accepted),
        latest_run_date=max(day_value for *_prefix, day_value, _pace in accepted),
        signature=signature,
    )


def load_current_hr_pace_model(*, today: date | None = None) -> HrPaceModel | None:
    conn = get_runs_db()
    try:
        rows = conn.execute(
            "SELECT date,distance,moving_time,avg_hr,max_hr,pace,sport_type FROM runs WHERE date IS NOT NULL ORDER BY date DESC"
        ).fetchall()
    finally:
        conn.close()
    return fit_hr_pace_model(rows, today=today)


def execution_model_quality(model: HrPaceModel, *, today: date | None = None) -> dict[str, Any]:
    """Gate watch-facing pace targets more strictly than analytical use."""
    today = today or date.today()
    latest = _day(model.latest_run_date)
    age_days = (today - latest).days if latest else 10_000
    hr_span = float(model.max_sample_hr) - float(model.min_sample_hr)
    reasons = []
    if model.sample_count < 12:
        reasons.append("too_few_runs")
    if hr_span < 25:
        reasons.append("insufficient_hr_range")
    if age_days > 60:
        reasons.append("stale_runs")
    return {
        "execution_ready": not reasons,
        "confidence": "high" if not reasons else "low",
        "reasons": reasons,
        "sample_count": model.sample_count,
        "latest_run_date": model.latest_run_date,
        "hr_span_bpm": round(hr_span, 1),
    }


def execution_pace_for_hr_percent(model: HrPaceModel, low_pct: float, high_pct: float) -> dict[str, Any]:
    low_pct, high_pct = sorted((float(low_pct), float(high_pct)))

    def pace_at(percent: float) -> float:
        requested_hr = model.max_hr_bpm * percent / 100
        # Do not extrapolate an old sparse curve without bounds. The cap still
        # permits useful easy/recovery guidance below the observed HR range.
        modeled_hr = min(model.max_sample_hr + 15, max(model.min_sample_hr - 30, requested_hr))
        speed = max(1000 / 450, min(1000 / 210, model.intercept_mps + model.slope_mps_per_bpm * modeled_hr))
        return 1000 / speed

    slow = min(450, round(pace_at(low_pct) + 5))
    fast = max(210, round(pace_at(high_pct) - 5))
    if fast > slow:
        fast, slow = slow, fast
    midpoint = round(2 / (1 / fast + 1 / slow))
    return {
        "metric": "pace",
        "pace_s_per_km": midpoint,
        "pace_min_s_per_km": fast,
        "pace_max_s_per_km": slow,
        "source": "current_hr_pace_model",
        "internal_hr_min_pct": round(low_pct, 1),
        "internal_hr_max_pct": round(high_pct, 1),
        "model_version": MODEL_VERSION,
        "model_signature": model.signature,
        "model_sample_count": model.sample_count,
        "model_latest_run_date": model.latest_run_date,
        "estimated_max_hr_bpm": round(model.max_hr_bpm),
    }


def rpe_to_hr_percent(low: float, high: float) -> tuple[float, float]:
    def convert(value: float) -> float:
        return max(60.0, min(100.0, 55.0 + float(value) * 4.5))
    return convert(low), convert(high)
