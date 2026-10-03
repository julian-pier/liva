from __future__ import annotations

from gcalsync import service


def test_get_or_create_calendar_returns_existing(monkeypatch):
    service._CALENDAR_ID_CACHE.clear()
    monkeypatch.setattr(
        service,
        "find_calendar_by_summary",
        lambda svc, summary: {"id": "cal_existing_1", "summary": "WebUntis"},
    )
    monkeypatch.setattr(service, "create_calendar", lambda *args, **kwargs: {"id": "cal_new"})

    out = service.get_or_create_calendar(object(), name="WebUntis", timezone="Europe/Berlin")

    assert out == "cal_existing_1"


def test_get_or_create_calendar_creates_when_missing(monkeypatch):
    service._CALENDAR_ID_CACHE.clear()
    calls = {"create": 0}

    monkeypatch.setattr(service, "find_calendar_by_summary", lambda svc, summary: None)

    def _create(_svc, *, summary: str, timezone: str):
        calls["create"] += 1
        return {"id": "cal_created_1", "summary": summary, "timeZone": timezone}

    monkeypatch.setattr(service, "create_calendar", _create)

    out = service.get_webuntis_calendar_id(object(), timezone="Europe/Berlin")

    assert out == "cal_created_1"
    assert calls["create"] == 1


def test_get_webuntis_calendar_id_prefers_configured_id(monkeypatch):
    service._CALENDAR_ID_CACHE.clear()
    called = {"find": 0}

    def _find(_svc, _summary):
        called["find"] += 1
        return {"id": "should_not_happen"}

    monkeypatch.setattr(service, "find_calendar_by_summary", _find)

    out = service.get_webuntis_calendar_id(
        object(),
        timezone="Europe/Berlin",
        configured_calendar_id="calendar-fixed-id@example.com",
        configured_calendar_name="WebUntis",
    )

    assert out == "calendar-fixed-id@example.com"
    assert called["find"] == 0


def test_get_school_calendar_id_for_write_rejects_primary():
    try:
        service.get_school_calendar_id_for_write("primary")
    except service.GoogleCalendarConfigError as exc:
        assert "GOOGLE_CALENDAR_SCHOOL_ID fehlt" in str(exc)
    else:
        raise AssertionError("expected GoogleCalendarConfigError")


def test_get_school_calendar_id_for_write_uses_exact_configured_id():
    out = service.get_school_calendar_id_for_write("f6b1@example.com")
    assert out == "f6b1@example.com"
