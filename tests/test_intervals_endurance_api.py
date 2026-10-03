from __future__ import annotations

import importlib
import os


def _load_app_module(monkeypatch):
    monkeypatch.setenv("LIVA_FLASK_SECRET", "test-secret")
    monkeypatch.setenv("INTERVALS_API_KEY", "")
    if "app" in os.sys.modules:
        return importlib.reload(os.sys.modules["app"])
    return importlib.import_module("app")


def test_dashboard_endurance_card_uses_local_cardio_plan_not_intervals_key(monkeypatch):
    appmod = _load_app_module(monkeypatch)
    monkeypatch.setattr(appmod, "get_next_open_cardio_session", lambda day: {
        "ok": True, "available": True, "has_workout": True, "source": "local_endurance_plan",
        "day": day, "plan": {"id": "ep_test", "title": "5k"},
        "session": {"id": "es_test", "title": "Locker", "scheduled_date": "2026-05-03", "sync_state": "synced"},
        "sync": {"state": "synced", "intervals_synced": True, "garmin_state": "via_intervals"},
    })
    client = appmod.app.test_client()

    resp = client.get("/api/dashboard/endurance-approval?day=2026-05-02")
    assert resp.status_code == 200
    payload = resp.get_json()

    assert payload["ok"] is True
    assert payload["source"] == "local_endurance_plan"
    assert payload["session"]["title"] == "Locker"
    assert payload["sync"]["intervals_synced"] is True


def test_intervals_debug_reports_missing_key_without_crashing(monkeypatch):
    appmod = _load_app_module(monkeypatch)
    client = appmod.app.test_client()

    resp = client.get("/api/debug/intervals/today?day=2026-05-02")
    assert resp.status_code == 200
    payload = resp.get_json()

    assert payload["ok"] is True
    assert payload["intervals_api_key_set"] is False
    assert payload["approval"]["message"] == "Intervals nicht verbunden."
