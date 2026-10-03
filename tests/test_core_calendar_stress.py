from __future__ import annotations

import sys
import types

from core import core_night_cycle


def _install_fake_gcalsync(monkeypatch, *, events):
    cfg_mod = types.ModuleType("gcalsync.config")
    service_mod = types.ModuleType("gcalsync.service")
    pkg_mod = types.ModuleType("gcalsync")

    class _Cfg:
        timezone = "Europe/Berlin"
        credentials_file = "fake-creds.json"
        token_file = "fake-token.json"
        default_calendar_id = "primary"
        school_calendar_id = ""
        work_calendar_id = ""
        training_calendar_id = ""
        football_calendar_id = ""

    class _CalendarListCall:
        def list(self, pageToken=None, maxResults=250):  # noqa: N803 - mirrors Google client
            return self

        def execute(self):
            return {"items": []}

    class _Service:
        def calendarList(self):
            return _CalendarListCall()

    cfg_mod.load_config = lambda: _Cfg()
    service_mod.build_service = lambda credentials_file, token_file: _Service()
    service_mod.list_events = lambda service, calendar_id, time_min, time_max, max_results=250: list(events)
    pkg_mod.config = cfg_mod
    pkg_mod.service = service_mod

    monkeypatch.setitem(sys.modules, "gcalsync", pkg_mod)
    monkeypatch.setitem(sys.modules, "gcalsync.config", cfg_mod)
    monkeypatch.setitem(sys.modules, "gcalsync.service", service_mod)


def test_all_day_google_events_do_not_add_core_calendar_stress(monkeypatch):
    _install_fake_gcalsync(
        monkeypatch,
        events=[
            {
                "id": "all-day-1",
                "summary": "Urlaub",
                "start": {"date": "2026-04-07"},
                "end": {"date": "2026-04-08"},
            }
        ],
    )

    snap = core_night_cycle._build_calendar_stress_snapshot("2026-04-07")

    assert snap["source"] == "google_calendar"
    assert snap["calendar_available"] is True
    assert snap["entries_count"] == 0
    assert snap["busy_minutes_today"] == 0
    assert snap["daily_stress"] == 0.0
    assert snap["calendar_friction_score"] == 0.0
    assert snap["school_start"] is None
    assert snap["school_end"] is None


def test_timed_google_events_still_count_for_core_calendar_stress(monkeypatch):
    _install_fake_gcalsync(
        monkeypatch,
        events=[
            {
                "id": "timed-1",
                "summary": "Termin",
                "start": {"dateTime": "2026-04-07T10:00:00+02:00"},
                "end": {"dateTime": "2026-04-07T11:30:00+02:00"},
            }
        ],
    )

    snap = core_night_cycle._build_calendar_stress_snapshot("2026-04-07")

    assert snap["entries_count"] == 1
    assert snap["busy_minutes_today"] == 90
    assert snap["school_start"] == "10:00"
    assert snap["school_end"] == "11:30"
    assert snap["daily_stress"] > 0.0
