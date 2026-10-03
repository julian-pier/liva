from app_support.weight_trend import build_weight_trend


def _rows(start: str, values: list[float | None]) -> list[dict]:
    from datetime import date, timedelta

    first = date.fromisoformat(start)
    return [
        {"id": index + 1, "date": (first + timedelta(days=index)).isoformat(), "weight": value}
        for index, value in enumerate(values)
    ]


def test_weight_trend_uses_current_calendar_week_and_latest_same_day_record():
    rows = _rows("2026-09-07", [69.0] + [70.0] * 7 + [71.0] * 7)
    rows.append({"id": 99, "date": "2026-09-15", "weight": 71.0})
    trend = build_weight_trend(rows, "2026-09-21")

    weeks = trend["calendar_week"]
    assert trend["primary"] == "calendar_week"
    assert "rolling_7d" not in trend
    assert (weeks["previous"]["start"], weeks["previous"]["end"]) == ("2026-09-14", "2026-09-20")
    assert (weeks["current"]["start"], weeks["current"]["end"]) == ("2026-09-21", "2026-09-27")
    assert weeks["current"]["sample_count"] == 1
    assert weeks["current"]["coverage_ratio"] == 1 / 7
    assert weeks["current"]["pending_dates"] == ["2026-09-22", "2026-09-23", "2026-09-24", "2026-09-25", "2026-09-26", "2026-09-27"]
    assert abs(weeks["delta_kg"] - (1 / 7)) < 1e-12
    assert weeks["status"] == "provisional"
    assert weeks["calorie_adjustment_eligible"] is False
    assert trend["latest_measurement"] == {"date": "2026-09-21", "weight_kg": 71.0}


def test_weight_trend_quality_does_not_fill_missing_or_invalid_measurements():
    rows = _rows("2026-09-14", [70.0, 70.1, 70.2, -1.0, None, None, None, 71.0, 71.1, 71.2, 71.3])
    trend = build_weight_trend(rows, "2026-09-24")["calendar_week"]

    assert trend["previous"]["sample_count"] == 3
    assert trend["current"]["sample_count"] == 4
    assert trend["current"]["coverage_ratio"] == 4 / 7
    assert trend["current"]["missing_dates"] == []
    assert trend["current"]["pending_dates"] == ["2026-09-25", "2026-09-26", "2026-09-27"]
    assert trend["status"] == "provisional"
    assert trend["delta_kg"] is not None


def test_weight_trend_never_labels_a_running_sunday_week_complete_and_handles_iso_year():
    trend = build_weight_trend([], "2027-01-03")["calendar_week"]

    assert trend["current"]["iso_week"] == "2026-W53"
    assert (trend["current"]["start"], trend["current"]["end"]) == ("2026-12-28", "2027-01-03")
    assert trend["previous"]["iso_week"] == "2026-W52"
    assert trend["status"] == "no_data"
