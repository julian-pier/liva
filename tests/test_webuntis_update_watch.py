from __future__ import annotations

import json
from datetime import date, datetime

from jobs import webuntis_update_watch as watch


def test_recent_success_suppresses_transient_failure_window():
    now = watch.datetime(2026, 9, 16, 16, 10)
    state = {"last_success_at": "2026-09-16T16:00:42"}

    assert watch._has_recent_success(state, now=now, grace_minutes=30)
    assert not watch._has_recent_success(state, now=now, grace_minutes=5)


def test_classify_webuntis_change_detects_cancelled_and_free():
    cancelled = watch.classify_webuntis_change(
        {"date": "2026-03-08", "start_time": "08:00", "subject": "Mathe", "status_hint": "cancelled"},
        today=date(2026, 3, 8),
    )
    free = watch.classify_webuntis_change(
        {"date": "2026-03-09", "start_time": "10:00", "subject": "Deutsch", "raw_text": "Freistunde"},
        today=date(2026, 3, 8),
    )

    assert cancelled.is_alert is True
    assert cancelled.type == "entfall"
    assert "Heute" in cancelled.display_text
    assert free.is_alert is True
    assert free.type == "freistunde"
    assert "Morgen" in free.display_text


def test_extract_alert_candidates_includes_cancelled_and_freistunde():
    payload = {
        "entries_visible": [
            {"date": "2026-03-02", "start_time": "08:00", "end_time": "09:30", "subject": "KU_GK1", "status_hint": "eva"},
            {"date": "2026-03-02", "start_time": "09:55", "end_time": "11:45", "subject": "KR_GK2", "status_hint": "cancelled"},
            {"date": "2026-03-02", "start_time": "11:45", "end_time": "13:15", "subject": "E_GK1", "raw_text": "Freistunde"},
        ]
    }
    out = watch._extract_alert_candidates(payload, now=datetime(2026, 3, 2, 7, 0), today=date(2026, 3, 2))
    assert [item.alert_type for item in out] == ["entfall", "entfall", "freistunde"]


def test_extract_alert_candidates_skips_past_slots_and_keeps_future_ones():
    payload = {
        "entries_visible": [
            {"date": "2026-03-02", "start_time": "08:00", "end_time": "09:30", "subject": "KU_GK1", "status_hint": "eva"},
            {"date": "2026-03-02", "start_time": "11:45", "end_time": "13:15", "subject": "E_GK1", "status_hint": "cancelled"},
            {"date": "2026-03-03", "start_time": "08:00", "end_time": "09:30", "subject": "M_LK1", "status_hint": "cancelled"},
        ]
    }
    out = watch._extract_alert_candidates(payload, now=datetime(2026, 3, 2, 10, 0), today=date(2026, 3, 2))
    assert [item.display_text for item in out] == [
        "Heute, 11:45-13:15 Uhr: E_GK1 fällt aus.",
        "Morgen, 08:00-09:30 Uhr: M_LK1 fällt aus.",
    ]


def test_main_dedupes_and_allows_changed_alert(tmp_path, monkeypatch):
    latest = tmp_path / "latest_today.json"
    state = tmp_path / "state.json"
    lock = tmp_path / "watch.lock"
    log = tmp_path / "watch.log"
    monkeypatch.setattr(watch, "LATEST_JSON", latest)
    monkeypatch.setattr(watch, "STATE_FILE", state)
    monkeypatch.setattr(watch, "LOCK_FILE", lock)
    monkeypatch.setattr(watch, "LOG_FILE", log)
    monkeypatch.setattr(watch, "_ntfy_ready", lambda: True)
    monkeypatch.setattr(watch, "_read_env_int", lambda *args, **kwargs: 0)

    sent: list[str] = []

    def fake_send(text: str) -> bool:
        sent.append(text)
        return True

    monkeypatch.setattr(watch, "_send_school_alert", fake_send)

    first_payload = {
        "entries_visible": [
            {
                "date": "2099-03-10",
                "start_time": "08:00",
                "end_time": "08:45",
                "subject": "Mathe",
                "teacher": "M1",
                "room": "R1",
                "status_hint": "cancelled",
            }
        ]
    }
    second_payload = {
        "entries_visible": [
            {
                "date": "2099-03-10",
                "start_time": "09:00",
                "end_time": "09:45",
                "subject": "Mathe",
                "teacher": "M1",
                "room": "R1",
                "status_hint": "cancelled",
            }
        ]
    }
    payload_box = {"payload": first_payload}

    def fake_run(script):
        if script == watch.FETCH_SCRIPT:
            latest.write_text(json.dumps(payload_box["payload"]), encoding="utf-8")
            return 0, "[SchoolSync] ok"
        if script == watch.CALENDAR_SYNC_SCRIPT:
            return 0, "[GCal] ok"
        raise AssertionError(script)

    monkeypatch.setattr(watch, "_run_python", fake_run)
    monkeypatch.setattr(watch, "_run_fetch_with_reauth", lambda: fake_run(watch.FETCH_SCRIPT))

    assert watch.main() == 0
    assert len(sent) == 1

    assert watch.main() == 0
    assert len(sent) == 1

    payload_box["payload"] = second_payload
    assert watch.main() == 0
    assert len(sent) == 2


def test_main_rate_limit_does_not_block_telegram(tmp_path, monkeypatch, capsys):
    latest = tmp_path / "latest_today.json"
    state = tmp_path / "state.json"
    lock = tmp_path / "watch.lock"
    log = tmp_path / "watch.log"
    monkeypatch.setattr(watch, "LATEST_JSON", latest)
    monkeypatch.setattr(watch, "STATE_FILE", state)
    monkeypatch.setattr(watch, "LOCK_FILE", lock)
    monkeypatch.setattr(watch, "LOG_FILE", log)
    monkeypatch.setattr(watch, "_ntfy_ready", lambda: True)
    monkeypatch.setattr(watch, "_read_env_int", lambda *args, **kwargs: 0)

    latest.write_text(
        json.dumps(
            {
                    "entries_visible": [
                        {"date": "2099-03-10", "start_time": "08:00", "end_time": "08:45", "subject": "Mathe", "status_hint": "cancelled"}
                    ]
                }
            ),
        encoding="utf-8",
    )
    monkeypatch.setattr(watch, "_run_fetch_with_reauth", lambda: (0, "[SchoolSync] ok"))
    monkeypatch.setattr(
        watch,
        "_run_python",
        lambda script: (0, "[GCal] Google Calendar Rate Limit erreicht. Calendar-Sync übersprungen.")
        if script == watch.CALENDAR_SYNC_SCRIPT
        else (0, "[SchoolSync] ok"),
    )

    sent: list[str] = []
    monkeypatch.setattr(watch, "_send_school_alert", lambda text: sent.append(text) or True)

    assert watch.main() == 0
    assert len(sent) == 1
    assert "Google Rate Limit" in capsys.readouterr().out
