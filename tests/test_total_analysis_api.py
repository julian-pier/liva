from __future__ import annotations

import ai.actions_v2 as actions_v2
import app as appmod


def test_calendar_load_returns_daily_busy_context(monkeypatch):
    monkeypatch.setattr(
        actions_v2,
        "_calendar_range_payload",
        lambda start, end: {
            "warnings": [],
            "days": [
                {
                    "date": "2026-09-28",
                    "available": True,
                    "day_pressure": "high",
                    "total_minutes_in_school": 360,
                    "events": [
                        {"title": "Mathe Klausur", "start": "2026-09-28T08:00:00+02:00", "end": "2026-09-28T09:30:00+02:00"},
                    ],
                }
            ],
        },
    )

    response = appmod.app.test_client().get("/api/analysis/calendar_load?start=2026-09-28&end=2026-09-28")

    assert response.status_code == 200
    assert response.get_json()["days"] == [
        {
            "date": "2026-09-28",
            "busy_minutes": 360,
            "event_count": 1,
            "important_count": 1,
            "day_pressure": "high",
            "available": True,
        }
    ]


def test_recovery_series_can_be_bounded_by_dates(monkeypatch):
    rows = [
        {"date": "2026-09-27", "usable": True},
        {"date": "2026-09-28", "usable": True},
        {"date": "2026-09-29", "usable": True},
    ]
    monkeypatch.setattr(
        appmod,
        "_recovery_payload",
        lambda *_args: {"ok": True, "rows": list(rows), "usable_rows": list(rows), "latest": rows[-1], "latest_usable": rows[-1]},
    )

    response = appmod.app.test_client().get("/api/hrv/recovery?start=2026-09-28&end=2026-09-29")
    payload = response.get_json()

    assert response.status_code == 200
    assert [row["date"] for row in payload["rows"]] == ["2026-09-28", "2026-09-29"]
    assert payload["range_start"] == "2026-09-28"
    assert payload["range_end"] == "2026-09-29"


def test_historical_target_does_not_reuse_current_global_preset():
    context = appmod._target_context_for_date(
        "2024-01-15",
        {"selected_mode": "lean_bulk", "presets": {"lean_bulk": 3800}},
        [{"start_date": "2024-01-01", "end_date": "2024-02-01", "mode": "cut", "target_kcal": None}],
        [],
    )

    assert context["source"] == "dated_mode"
    assert context["mode"] == "cut"
    assert context["target_kcal"] is None


def test_historical_target_marks_conflicts_without_overriding_priority():
    context = appmod._target_context_for_date(
        "2024-01-15",
        {},
        [{"start_date": "2024-01-01", "end_date": None, "mode": "lean_bulk", "target_kcal": 3100}],
        [{
            "id": 1,
            "week_template_id": 7,
            "start_date": "2024-01-01",
            "end_date": None,
            "snapshot": {
                "template_id": 7,
                "title": "Mini-Cut",
                "active_mode": "cut",
                "active_mode_settings": {"kcal_target": 2400},
                "day_targets": {"0": {"kcal": 2400}},
            },
        }],
    )

    assert context["source"] == "nutrition_plan"
    assert context["mode"] == "cut"
    assert context["target_kcal"] == 2400
    assert context["context_ambiguous"] is True
