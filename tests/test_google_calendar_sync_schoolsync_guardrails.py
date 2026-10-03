from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from scripts import google_calendar_sync_schoolsync as sync_script


def test_sync_aborts_when_school_calendar_id_missing(tmp_path, monkeypatch, capsys):
    source = tmp_path / "latest_today.json"
    source.write_text(
        json.dumps(
            {
                "entries_visible": [
                    {
                        "date": "2026-05-01",
                        "start_time": "08:00",
                        "end_time": "08:45",
                        "subject": "M_LK1",
                        "teacher": "BAU",
                        "room": "R1",
                        "status_hint": "unknown",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        sync_script,
        "load_config",
        lambda: SimpleNamespace(
            latest_schoolsync_file=source,
            credentials_file=Path("cred"),
            token_file=Path("tok"),
            timezone="Europe/Berlin",
            school_calendar_id="",
            school_calendar_name="WebUntis",
            cleanup_missing_school_events=True,
        ),
    )
    monkeypatch.setattr(sync_script, "build_service", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(
        sync_script.argparse.ArgumentParser,
        "parse_args",
        lambda self: SimpleNamespace(from_json=str(source), days_back=3, days_forward=31, dry_run=False, no_cleanup=False),
    )

    assert sync_script.main() == 2
    assert "GOOGLE_CALENDAR_SCHOOL_ID fehlt" in capsys.readouterr().out


def test_sync_uses_configured_school_calendar_id(tmp_path, monkeypatch, capsys):
    source = tmp_path / "latest_today.json"
    source.write_text(
        json.dumps(
            {
                "entries_visible": [
                    {
                        "date": "2026-05-01",
                        "start_time": "08:00",
                        "end_time": "08:45",
                        "subject": "M_LK1",
                        "teacher": "BAU",
                        "room": "R1",
                        "status_hint": "unknown",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        sync_script,
        "load_config",
        lambda: SimpleNamespace(
            latest_schoolsync_file=source,
            credentials_file=Path("cred"),
            token_file=Path("tok"),
            timezone="Europe/Berlin",
            school_calendar_id="f6b1@example.com",
            school_calendar_name="WebUntis",
            cleanup_missing_school_events=False,
        ),
    )
    monkeypatch.setattr(sync_script, "build_service", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(sync_script, "list_events", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(sync_script, "insert_event", lambda *_args, **_kwargs: {"id": "created1"})
    monkeypatch.setattr(
        sync_script.argparse.ArgumentParser,
        "parse_args",
        lambda self: SimpleNamespace(from_json=str(source), days_back=3, days_forward=31, dry_run=False, no_cleanup=False),
    )

    assert sync_script.main() == 0
    assert "f6b1@example.com" in capsys.readouterr().out
