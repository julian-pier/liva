from __future__ import annotations

from datetime import date

import integrations.telegram_hub as telegram_hub


def test_parse_quick_calendar_text_without_year():
    out = telegram_hub.parse_quick_calendar_text(
        "Zahnarzt, 27.02, 13:30",
        today=date(2026, 1, 10),
    )
    assert out is not None
    assert out.title == "Zahnarzt"
    assert out.date_iso == "2026-02-27"
    assert out.start_time == "13:30"
    assert out.end_time == "14:30"


def test_parse_quick_calendar_text_with_year():
    out = telegram_hub.parse_quick_calendar_text(
        "Physio, 27.02.2026, 16:00",
        today=date(2026, 1, 10),
    )
    assert out is not None
    assert out.title == "Physio"
    assert out.date_iso == "2026-02-27"
    assert out.start_time == "16:00"
    assert out.end_time == "17:00"


def test_parse_quick_calendar_text_with_range():
    out = telegram_hub.parse_quick_calendar_text(
        "Arzt, 27.02, 13:30-14:15",
        today=date(2026, 1, 10),
    )
    assert out is not None
    assert out.title == "Arzt"
    assert out.date_iso == "2026-02-27"
    assert out.start_time == "13:30"
    assert out.end_time == "14:15"
    assert out.all_day is False


def test_parse_quick_calendar_text_all_day_without_time():
    out = telegram_hub.parse_quick_calendar_text(
        "Geburtstag Oma, 27.02",
        today=date(2026, 1, 10),
    )
    assert out is not None
    assert out.title == "Geburtstag Oma"
    assert out.date_iso == "2026-02-27"
    assert out.start_time is None
    assert out.end_time is None
    assert out.all_day is True


def test_parse_quick_calendar_text_relative_today():
    out = telegram_hub.parse_quick_calendar_text(
        "Medienscouts, heute, 14:00",
        today=date(2026, 3, 11),
    )
    assert out is not None
    assert out.title == "Medienscouts"
    assert out.date_iso == "2026-03-11"
    assert out.start_time == "14:00"
    assert out.end_time == "15:00"


def test_parse_quick_calendar_text_relative_tomorrow():
    out = telegram_hub.parse_quick_calendar_text(
        "Medienscouts, morgen, 14:00",
        today=date(2026, 3, 11),
    )
    assert out is not None
    assert out.date_iso == "2026-03-12"


def test_parse_quick_calendar_text_relative_uebermorgen():
    out = telegram_hub.parse_quick_calendar_text(
        "Medienscouts, uebermorgen, 14:00",
        today=date(2026, 3, 11),
    )
    assert out is not None
    assert out.date_iso == "2026-03-13"


def test_parse_quick_calendar_text_weekday_names():
    out_sat = telegram_hub.parse_quick_calendar_text(
        "Medienscouts, samstag, 14:00",
        today=date(2026, 3, 11),
    )
    out_thu = telegram_hub.parse_quick_calendar_text(
        "Medienscouts, donnerstag, 14:00",
        today=date(2026, 3, 11),
    )
    assert out_sat is not None
    assert out_thu is not None
    assert out_sat.date_iso == "2026-03-14"
    assert out_thu.date_iso == "2026-03-12"


def test_parse_monthly_shift_plan_for_configured_person_only():
    sample = """
Plan für März 2026:

1.3 So: Alex A 14:30
15-18Uhr Soccergeburtstag

2.3 Mo: Ulla

14.3 Sa: Alex R 11:30
12-15Uhr Soccergeburtstag
15-18Uhr Soccergeburtstag

15.3 So: Alex A 14:30
15-18Uhr Soccergeburtstag

22.3 So: Alex A 14:30
15-18Uhr Soccergeburtstag
"""
    days = telegram_hub.parse_monthly_shift_plan(sample, target_name="Alex A")
    assert len(days) == 3
    assert [d.date_iso for d in days] == ["2026-03-01", "2026-03-15", "2026-03-22"]
    assert all(d.start_time == "14:30" for d in days)
    assert all(d.end_time == "18:00" for d in days)
    assert all("Alex R" not in d.raw_block for d in days)
    assert all("Ulla" not in d.raw_block for d in days)
