"""Canonical, calendar-based bodyweight trend calculation.

This module intentionally contains no database or HTTP code.  Every consumer
passes it raw ``weight_logs`` rows, so the period definitions and data-quality
rules cannot drift between Actions, MCP, CORE, or the UI.
"""
from __future__ import annotations

from datetime import date, timedelta
from math import isfinite
from typing import Any, Iterable


TIMEZONE = "Europe/Berlin"
EXPECTED_DAYS = 7
MINIMUM_SAMPLES_FOR_ACTIONABLE_TREND = 5


def _as_date(value: str | date) -> date:
    return value if isinstance(value, date) else date.fromisoformat(str(value)[:10])


def _valid_weight(value: Any) -> float | None:
    try:
        weight = float(value)
    except (TypeError, ValueError):
        return None
    return weight if isfinite(weight) and weight > 0 else None


def _canonical_by_date(rows: Iterable[dict[str, Any]], as_of: date) -> dict[date, dict[str, Any]]:
    """Choose the established latest-id row once per local measurement date."""
    selected: dict[date, tuple[tuple[int, str], dict[str, Any]]] = {}
    for row in rows:
        try:
            day = _as_date(row.get("date"))
        except (TypeError, ValueError):
            continue
        if day > as_of:
            continue
        # Writes upsert by date and legacy reads use id DESC.  Preserve that
        # deterministic rule for any historical duplicate rows.
        try:
            row_id = int(row.get("id") or row.get("canonical_id") or 0)
        except (TypeError, ValueError):
            row_id = 0
        revision = str(row.get("updated_at") or row.get("created_at") or "")
        key = (row_id, revision)
        if day not in selected or key > selected[day][0]:
            selected[day] = (key, dict(row))
    return {day: row for day, (_, row) in selected.items()}


def _window(rows: dict[date, dict[str, Any]], start: date, end: date, *, observed_through: date | None = None, iso_week: str | None = None) -> dict[str, Any]:
    dates = [start + timedelta(days=offset) for offset in range((end - start).days + 1)]
    values: list[float] = []
    measured_dates: set[date] = set()
    for day in dates:
        row = rows.get(day)
        weight = _valid_weight(row.get("weight") if row else None)
        if weight is not None:
            values.append(weight)
            measured_dates.add(day)
    count = len(values)
    payload: dict[str, Any] = {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "mean_kg": sum(values) / count if count else None,
        "sample_count": count,
        "expected_days": EXPECTED_DAYS,
        "coverage_ratio": count / EXPECTED_DAYS,
        "missing_dates": [day.isoformat() for day in dates if day <= (observed_through or end) and day not in measured_dates],
    }
    if iso_week:
        payload["iso_week"] = iso_week
    if observed_through and observed_through < end:
        payload["observed_through"] = observed_through.isoformat()
        payload["pending_dates"] = [day.isoformat() for day in dates if day > observed_through]
    return payload


def _comparison(previous: dict[str, Any], current: dict[str, Any]) -> tuple[float | None, str]:
    previous_count = int(previous["sample_count"])
    current_count = int(current["sample_count"])
    if not previous_count or not current_count:
        return None, "no_data"
    # A numeric delta remains useful as an explicitly provisional observation;
    # it must never authorize a calorie adjustment before both weeks have five
    # actual measurement dates.
    delta = float(current["mean_kg"]) - float(previous["mean_kg"])
    return delta, "usable" if min(previous_count, current_count) >= MINIMUM_SAMPLES_FOR_ACTIONABLE_TREND else "provisional"


def build_weight_trend(rows: Iterable[dict[str, Any]], as_of: str | date) -> dict[str, Any]:
    """Return the versioned weight-trend contract for an explicit local date."""
    day = _as_date(as_of)
    canonical = _canonical_by_date(rows, day)

    current_week_start = day - timedelta(days=day.weekday())
    current_week_end = current_week_start + timedelta(days=6)
    previous_week_start = current_week_start - timedelta(days=7)
    previous_week_end = current_week_start - timedelta(days=1)
    current_iso = current_week_start.isocalendar()
    previous_iso = previous_week_start.isocalendar()
    calendar_current = _window(canonical, current_week_start, current_week_end, observed_through=day, iso_week=f"{current_iso.year}-W{current_iso.week:02d}")
    calendar_previous = _window(canonical, previous_week_start, previous_week_end, iso_week=f"{previous_iso.year}-W{previous_iso.week:02d}")
    calendar_delta, calendar_status = _comparison(calendar_previous, calendar_current)

    valid_measurements = {
        measured_day: row
        for measured_day, row in canonical.items()
        if _valid_weight(row.get("weight")) is not None
    }
    latest_day = max(valid_measurements, default=None)
    latest = valid_measurements.get(latest_day) if latest_day else None
    latest_weight = _valid_weight(latest.get("weight") if latest else None)
    latest_measurement = None
    if latest_day and latest_weight is not None:
        latest_measurement = {"date": latest_day.isoformat(), "weight_kg": latest_weight}
        source_updated_at = latest.get("updated_at") or latest.get("created_at")
        if source_updated_at:
            latest_measurement["source_updated_at"] = source_updated_at

    return {
        "schema_version": 2,
        "timezone": TIMEZONE,
        "as_of": day.isoformat(),
        "unit": "kg",
        "primary": "calendar_week",
        "calendar_week": {
            "previous": calendar_previous,
            "current": calendar_current,
            "delta_kg": calendar_delta,
            "status": calendar_status,
            "calorie_adjustment_eligible": calendar_status == "usable",
        },
        "latest_measurement": latest_measurement,
    }
