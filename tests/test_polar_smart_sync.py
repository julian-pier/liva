from __future__ import annotations

import importlib
from datetime import datetime, timedelta

import database.connections as connections


def _reload_modules(tmp_path):
    connections.POLAR_DB = str(tmp_path / "polar.sqlite3")
    import integrations.polar_client as polar_client_module
    importlib.reload(polar_client_module)
    import scripts.polar_smart_sync as smart_sync_module
    importlib.reload(smart_sync_module)
    polar_client_module.ensure_polar_schema()
    return polar_client_module, smart_sync_module


def test_ensure_polar_schema_adds_smart_sync_table(tmp_path):
    _polar_client_module, _smart_sync_module = _reload_modules(tmp_path)
    conn = connections.get_polar_db()
    tables = {
        row["name"]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    conn.close()
    assert "polar_smart_sync_state" in tables


def test_is_target_date_fully_synced_requires_sleep_and_nightly(tmp_path):
    _polar_client_module, smart_sync_module = _reload_modules(tmp_path)
    conn = connections.get_polar_db()
    conn.execute(
        """
        INSERT INTO polar_sleep (date, raw_json, sleep_score, created_at, updated_at)
        VALUES ('2026-05-13', '{}', 81, '2026-05-13T05:50:00+02:00', '2026-05-13T05:50:00+02:00')
        """
    )
    conn.execute(
        """
        INSERT INTO polar_nightly_recharge (date, raw_json, mean_recovery_rmssd, created_at, updated_at)
        VALUES ('2026-05-13', '{}', 54, '2026-05-13T05:50:00+02:00', '2026-05-13T05:50:00+02:00')
        """
    )
    conn.commit()

    assert smart_sync_module.is_target_date_fully_synced(conn, "2026-05-13") is True
    assert smart_sync_module.is_target_date_fully_synced(conn, "2026-05-12") is False
    conn.close()


def test_determine_sync_window_weekday_and_weekend():
    import scripts.polar_smart_sync as smart_sync_module

    assert smart_sync_module.determine_sync_window(datetime(2026, 5, 13, 6, 0, tzinfo=smart_sync_module.BERLIN_TZ)).window_name == "daily"
    assert smart_sync_module.determine_sync_window(datetime(2026, 5, 13, 21, 0, tzinfo=smart_sync_module.BERLIN_TZ)).active is True
    assert smart_sync_module.determine_sync_window(datetime(2026, 5, 13, 3, 59, tzinfo=smart_sync_module.BERLIN_TZ)).active is False
    assert smart_sync_module.determine_sync_window(datetime(2026, 5, 16, 9, 0, tzinfo=smart_sync_module.BERLIN_TZ)).window_name == "daily"


def test_run_skips_when_already_synced(tmp_path, monkeypatch):
    _polar_client_module, smart_sync_module = _reload_modules(tmp_path)
    conn = connections.get_polar_db()
    target = datetime(2026, 5, 13).date()
    for offset in range(smart_sync_module.BACKFILL_DAYS):
        day = (target - timedelta(days=offset)).isoformat()
        conn.execute(
            """
            INSERT INTO polar_sleep (date, raw_json, sleep_score, created_at, updated_at)
            VALUES (?, '{}', 80, '2026-05-13T05:40:00+02:00', '2026-05-13T05:40:00+02:00')
            """,
            (day,),
        )
        conn.execute(
            """
            INSERT INTO polar_nightly_recharge (date, raw_json, ans_status, created_at, updated_at)
            VALUES (?, '{}', -3.4, '2026-05-13T05:40:00+02:00', '2026-05-13T05:40:00+02:00')
            """,
            (day,),
        )
    conn.commit()
    conn.close()

    called = {"count": 0}

    def _fake_invoke_sync(*args, **kwargs):
        called["count"] += 1
        return {"ok": True}

    monkeypatch.setattr(smart_sync_module, "invoke_sync", _fake_invoke_sync)
    rc = smart_sync_module.run(datetime(2026, 5, 13, 6, 0, tzinfo=smart_sync_module.BERLIN_TZ))
    assert rc == 0
    assert called["count"] == 0


def test_run_backfills_missing_recent_day_when_today_is_already_synced(tmp_path, monkeypatch):
    _polar_client_module, smart_sync_module = _reload_modules(tmp_path)
    conn = connections.get_polar_db()
    for table, values in (
        ("polar_sleep", "sleep_score"),
        ("polar_nightly_recharge", "mean_recovery_rmssd"),
    ):
        conn.execute(
            f"""
            INSERT INTO {table} (date, raw_json, {values}, created_at, updated_at)
            VALUES ('2026-05-13', '{{}}', 80, '2026-05-13T05:40:00+02:00', '2026-05-13T05:40:00+02:00')
            """
        )
    conn.commit()
    conn.close()

    calls = []
    monkeypatch.setattr(
        smart_sync_module,
        "invoke_sync",
        lambda *args, **kwargs: calls.append(kwargs) or {"ok": True},
    )

    rc = smart_sync_module.run(datetime(2026, 5, 13, 6, 0, tzinfo=smart_sync_module.BERLIN_TZ))

    assert rc == 0
    assert calls == [{"days": 7}]


def test_run_skips_when_rate_limited(tmp_path, monkeypatch):
    _polar_client_module, smart_sync_module = _reload_modules(tmp_path)
    conn = connections.get_polar_db()
    smart_sync_module.record_attempt(
        conn,
        "2026-05-13",
        attempted_at="2026-05-13T06:01:00+02:00",
        status="requested",
        message="sync_requested",
    )
    conn.close()

    called = {"count": 0}

    def _fake_invoke_sync(*args, **kwargs):
        called["count"] += 1
        return {"ok": True}

    monkeypatch.setattr(smart_sync_module, "invoke_sync", _fake_invoke_sync)
    rc = smart_sync_module.run(datetime(2026, 5, 13, 6, 3, 0, tzinfo=smart_sync_module.BERLIN_TZ))
    assert rc == 0
    assert called["count"] == 0


def test_run_invokes_sync_and_persists_attempt(tmp_path, monkeypatch):
    _polar_client_module, smart_sync_module = _reload_modules(tmp_path)
    notices = []
    monkeypatch.setattr(smart_sync_module, "send_notify", lambda target_date: notices.append(target_date))

    def _fake_invoke_sync(*args, **kwargs):
        conn = connections.get_polar_db()
        conn.execute(
            """
            INSERT INTO polar_sleep (date, raw_json, sleep_score, created_at, updated_at)
            VALUES ('2026-05-13', '{}', 84, '2026-05-13T06:00:00+02:00', '2026-05-13T06:00:00+02:00')
            ON CONFLICT(date) DO UPDATE SET sleep_score = excluded.sleep_score, updated_at = excluded.updated_at
            """
        )
        conn.execute(
            """
            INSERT INTO polar_nightly_recharge (date, raw_json, mean_recovery_rmssd, created_at, updated_at)
            VALUES ('2026-05-13', '{}', 61, '2026-05-13T06:00:00+02:00', '2026-05-13T06:00:00+02:00')
            ON CONFLICT(date) DO UPDATE SET mean_recovery_rmssd = excluded.mean_recovery_rmssd, updated_at = excluded.updated_at
            """
        )
        conn.commit()
        conn.close()
        return {"ok": True}

    monkeypatch.setattr(smart_sync_module, "invoke_sync", _fake_invoke_sync)
    rc = smart_sync_module.run(datetime(2026, 5, 13, 6, 0, tzinfo=smart_sync_module.BERLIN_TZ))
    assert rc == 0

    conn = connections.get_polar_db()
    row = conn.execute("SELECT * FROM polar_smart_sync_state WHERE target_date = '2026-05-13'").fetchone()
    assert row["last_status"] == "requested"
    assert row["last_message"] == "sync_requested"
    assert smart_sync_module.is_target_date_fully_synced(conn, "2026-05-13") is True
    conn.close()
    assert notices == ["2026-05-13"]


def test_polar_completion_uses_active_canonical_provider(monkeypatch):
    import scripts.polar_smart_sync as smart_sync_module
    from control_center import notifications

    sent = []
    monkeypatch.setattr(notifications, "status", lambda: {"active_provider": "telegram"})
    monkeypatch.setattr(notifications, "deliver", lambda **kwargs: sent.append(kwargs))
    smart_sync_module.send_notify("2026-05-13")
    assert sent == [{"provider": "telegram", "dedupe_key": "polar-sync:complete:2026-05-13", "text": "HRV & RHR sind jetzt vollständig da"}]


def test_run_returns_error_when_endpoint_reports_failure(tmp_path, monkeypatch):
    _polar_client_module, smart_sync_module = _reload_modules(tmp_path)

    monkeypatch.setattr(smart_sync_module, "invoke_sync", lambda *args, **kwargs: {"ok": False, "message": "Polar ist noch nicht verbunden."})
    rc = smart_sync_module.run(datetime(2026, 5, 13, 6, 0, tzinfo=smart_sync_module.BERLIN_TZ))
    assert rc == 1

    conn = connections.get_polar_db()
    row = conn.execute("SELECT * FROM polar_smart_sync_state WHERE target_date = '2026-05-13'").fetchone()
    conn.close()
    assert row["last_status"] == "sync_error"
    assert "nicht verbunden" in row["last_message"]


def test_run_marks_needs_reconnect_for_token_problem(tmp_path, monkeypatch):
    _polar_client_module, smart_sync_module = _reload_modules(tmp_path)

    monkeypatch.setattr(
        smart_sync_module,
        "invoke_sync",
        lambda *args, **kwargs: {"ok": False, "message": "Polar nicht mehr autorisiert: bitte neu verbinden."},
    )
    rc = smart_sync_module.run(datetime(2026, 5, 13, 6, 0, tzinfo=smart_sync_module.BERLIN_TZ))
    assert rc == 1

    conn = connections.get_polar_db()
    row = conn.execute("SELECT * FROM polar_smart_sync_state WHERE target_date = '2026-05-13'").fetchone()
    conn.close()
    assert row["last_status"] == "needs_reconnect"
    assert "neu verbinden" in row["last_message"]


def test_run_marks_needs_reconnect_for_sync_error_list(tmp_path, monkeypatch):
    _polar_client_module, smart_sync_module = _reload_modules(tmp_path)

    monkeypatch.setattr(
        smart_sync_module,
        "invoke_sync",
        lambda *args, **kwargs: {"ok": False, "errors": ["2026-05-13 sleep: Polar nicht mehr autorisiert: bitte neu verbinden."]},
    )
    rc = smart_sync_module.run(datetime(2026, 5, 13, 6, 0, tzinfo=smart_sync_module.BERLIN_TZ))
    assert rc == 1

    conn = connections.get_polar_db()
    row = conn.execute("SELECT * FROM polar_smart_sync_state WHERE target_date = '2026-05-13'").fetchone()
    conn.close()
    assert row["last_status"] == "needs_reconnect"
    assert "nicht mehr autorisiert" in row["last_message"]
