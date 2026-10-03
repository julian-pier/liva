from __future__ import annotations

import importlib
import os
import sqlite3

import database.connections as connections


def _load_app_module(monkeypatch, tmp_path):
    monkeypatch.setenv("LIVA_FLASK_SECRET", "test-secret")
    connections.HRV_DB = str(tmp_path / "hrv.sqlite3")
    connections.POLAR_DB = str(tmp_path / "polar.sqlite3")
    if "app" in os.sys.modules:
        return importlib.reload(os.sys.modules["app"])
    return importlib.import_module("app")


def _init_hrv_db(path):
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE hrv_measurements (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          ts_measurement TEXT NOT NULL UNIQUE,
          date_utc TEXT,
          hr REAL,
          rmssd REAL,
          sdnn REAL,
          avnn REAL,
          signal_quality TEXT,
          source_file TEXT,
          created_at TEXT DEFAULT (datetime('now')),
          training_motivation REAL,
          fatigue REAL,
          sickness INTEGER,
          sleep_quality REAL,
          alcohol TEXT,
          sickness_bool INTEGER NOT NULL DEFAULT 0,
          alcohol_bool INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    conn.commit()
    conn.close()


def test_hrv_recovery_prefers_polar_and_keeps_legacy_history(monkeypatch, tmp_path):
    _init_hrv_db(str(tmp_path / "hrv.sqlite3"))
    appmod = _load_app_module(monkeypatch, tmp_path)

    hrv_conn = sqlite3.connect(connections.HRV_DB)
    hrv_conn.execute(
        """
        INSERT INTO hrv_measurements
        (ts_measurement, date_utc, hr, rmssd, sdnn, avnn, source_file, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        ("2026-05-12 06:00:00+0000", "2026-05-12 00:00:00+0000", 51, 88, 42, 1170, "HRV4Training", "2026-05-12 06:05:00+0000"),
    )
    hrv_conn.execute(
        """
        INSERT INTO hrv_measurements
        (ts_measurement, date_utc, hr, rmssd, sdnn, avnn, source_file, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        ("2026-05-11 06:10:00+0000", "2026-05-11 00:00:00+0000", 49, 70, 38, 1225, "HRV4Training", "2026-05-11 06:12:00+0000"),
    )
    hrv_conn.commit()
    hrv_conn.close()

    polar_conn = connections.get_polar_db()
    polar_conn.execute(
        """
        INSERT INTO polar_nightly_recharge
        (date, raw_json, ans_status, recovery_indicator, recovery_indicator_sublevel, ans_rate, mean_recovery_rri, mean_recovery_rmssd, mean_recovery_respiration_interval, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "2026-05-12",
            '{"created":"2026-05-12T03:47:00Z"}',
            -5.9518,
            1,
            34,
            2,
            1302,
            124,
            0,
            "2026-05-12T04:00:00+00:00",
            "2026-05-12T04:00:00+00:00",
        ),
    )
    polar_conn.execute(
        """
        INSERT INTO polar_sleep
        (date, raw_json, sleep_score, sleep_start, sleep_end, sleep_minutes, actual_sleep_minutes, deep_sleep_minutes, rem_sleep_minutes, interruptions, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "2026-05-12",
            "{}",
            52.2606,
            "2026-05-11T21:20:00Z",
            "2026-05-12T05:55:00Z",
            515,
            470,
            78,
            96,
            3,
            "2026-05-12T04:00:00+00:00",
            "2026-05-12T04:00:00+00:00",
        ),
    )
    polar_conn.execute(
        """
        INSERT INTO polar_continuous_samples
        (date, raw_json, sample_count, min_hr, max_hr, avg_hr, resting_candidate_hr, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "2026-05-12",
            "{}",
            144,
            42,
            108,
            63,
            46,
            "2026-05-12T04:00:00+00:00",
            "2026-05-12T04:00:00+00:00",
        ),
    )
    polar_conn.execute(
        """
        INSERT INTO polar_activity
        (date, raw_json, steps, active_minutes, inactivity_count, calories, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "2026-05-12",
            '{"activityDays":[{"date":"2026-05-12","activitiesPerDevice":[]}]}',
            None,
            None,
            None,
            None,
            "2026-05-12T04:00:00+00:00",
            "2026-05-12T04:00:00+00:00",
        ),
    )
    polar_conn.execute(
        "INSERT INTO polar_tokens (access_token, created_at, updated_at) VALUES (?, ?, ?)",
        ("token", "2026-05-12T04:00:00+00:00", "2026-05-12T04:00:00+00:00"),
    )
    polar_conn.execute(
        "INSERT INTO polar_sync_log (sync_type, status, message, created_at) VALUES (?, ?, ?, ?)",
        ("sync_range", "ok", "done", "2026-05-12T04:05:00+00:00"),
    )
    polar_conn.commit()
    polar_conn.close()

    client = appmod.app.test_client()
    resp = client.get("/api/hrv/recovery?range_days=30&baseline_days=28")
    assert resp.status_code == 200
    payload = resp.get_json()

    assert payload["ok"] is True
    assert "primary_source_badge" not in payload
    assert payload["latest"]["source"] == "polar"
    assert payload["latest"]["date"] == "2026-05-12"
    assert payload["latest"]["rmssd"] == 124
    assert round(float(payload["latest"]["nightPulse"]), 0) == 46
    assert payload["latest"]["sleepScore"] == 52.2606
    assert payload["latest"]["activitySteps"] is None
    assert len(payload["rows"]) == 2
    assert payload["rows"][0]["source"] == "polar"
    assert payload["rows"][1]["source"] == "legacy"
    assert payload["readiness"]["contradiction"] is True


def test_hrv_recovery_empty_polar_shell_does_not_override_usable_legacy(monkeypatch, tmp_path):
    _init_hrv_db(str(tmp_path / "hrv.sqlite3"))
    appmod = _load_app_module(monkeypatch, tmp_path)

    hrv_conn = sqlite3.connect(connections.HRV_DB)
    hrv_conn.execute(
        """
        INSERT INTO hrv_measurements
        (ts_measurement, date_utc, hr, rmssd, sdnn, avnn, source_file, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        ("2026-05-09 06:00:00+0000", "2026-05-09 00:00:00+0000", 48, 92, 40, 1260, "HRV4Training", "2026-05-09 06:02:00+0000"),
    )
    hrv_conn.commit()
    hrv_conn.close()

    polar_conn = connections.get_polar_db()
    polar_conn.execute(
        """
        INSERT INTO polar_activity
        (date, raw_json, steps, active_minutes, inactivity_count, calories, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "2026-05-09",
            '{"activityDays":[{"date":"2026-05-09","activitiesPerDevice":[]}]}',
            None,
            None,
            None,
            None,
            "2026-05-09T15:06:00+00:00",
            "2026-05-09T15:06:00+00:00",
        ),
    )
    polar_conn.commit()
    polar_conn.close()

    client = appmod.app.test_client()
    resp = client.get("/api/hrv/recovery?range_days=30&baseline_days=28")
    assert resp.status_code == 200
    payload = resp.get_json()

    assert payload["ok"] is True
    assert len(payload["rows"]) == 1
    assert len(payload["usable_rows"]) == 1
    assert payload["rows"][0]["source"] == "legacy"
    assert payload["rows"][0]["rmssd"] == 92
    assert payload["latest"]["source"] == "legacy"
    assert payload["rows"][0]["recovery_score"] is None
    assert payload["rows"][0]["recovery_status"] is None


def test_hrv_recovery_uses_raw_json_fallback_for_sleep_and_breathing(monkeypatch, tmp_path):
    _init_hrv_db(str(tmp_path / "hrv.sqlite3"))
    appmod = _load_app_module(monkeypatch, tmp_path)

    polar_conn = connections.get_polar_db()
    polar_conn.execute(
        """
        INSERT INTO polar_nightly_recharge
        (date, raw_json, ans_status, recovery_indicator, recovery_indicator_sublevel, ans_rate, mean_recovery_rri, mean_recovery_rmssd, mean_recovery_respiration_interval, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "2026-05-12",
            '{"created":"2026-05-12T03:47:00Z","breathingRateSamples":[{"breathingRate":14.1},{"breathingRate":14.3}]}',
            -5.9518,
            1,
            34,
            2,
            1302,
            124,
            0,
            "2026-05-12T04:00:00+00:00",
            "2026-05-12T04:00:00+00:00",
        ),
    )
    polar_conn.execute(
        """
        INSERT INTO polar_sleep
        (date, raw_json, sleep_score, sleep_start, sleep_end, sleep_minutes, actual_sleep_minutes, deep_sleep_minutes, rem_sleep_minutes, interruptions, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "2026-05-12",
            '{"sleepDate":"2026-05-12","sleepResult":{"hypnogram":{"sleepStart":"2026-05-11T23:41:00+02:00","sleepEnd":"2026-05-12T05:47:00+02:00"}},"sleepEvaluation":{"asleepDuration":"21960s","phaseDurations":{"deep":"5100s","rem":"4320s"},"interruptions":{"totalCount":6}},"sleepScore":{"sleepScore":52.2606}}',
            52.2606,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            "2026-05-12T04:00:00+00:00",
            "2026-05-12T04:00:00+00:00",
        ),
    )
    polar_conn.commit()
    polar_conn.close()

    client = appmod.app.test_client()
    resp = client.get("/api/hrv/recovery?range_days=30&baseline_days=28")
    assert resp.status_code == 200
    payload = resp.get_json()

    assert payload["ok"] is True
    latest = payload["latest"]
    assert latest["sleepWindowLabel"] == "23:41 – 05:47"
    assert latest["actualSleepMinutes"] == 366
    assert latest["remSleepMinutes"] == 72
    assert latest["interruptions"] == 6
    assert latest["breathingRate"] == 14.2


def test_hrv_recovery_computes_interruption_minutes_from_sleep_window(monkeypatch, tmp_path):
    _init_hrv_db(str(tmp_path / "hrv.sqlite3"))
    appmod = _load_app_module(monkeypatch, tmp_path)

    polar_conn = connections.get_polar_db()
    polar_conn.execute(
        """
        INSERT INTO polar_nightly_recharge
        (date, raw_json, ans_status, recovery_indicator, recovery_indicator_sublevel, ans_rate, mean_recovery_rri, mean_recovery_rmssd, mean_recovery_respiration_interval, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "2026-05-12",
            '{"created":"2026-05-12T03:47:00Z"}',
            -5.9518,
            1,
            34,
            2,
            1302,
            124,
            0,
            "2026-05-12T04:00:00+00:00",
            "2026-05-12T04:00:00+00:00",
        ),
    )
    polar_conn.execute(
        """
        INSERT INTO polar_sleep
        (date, raw_json, sleep_score, sleep_start, sleep_end, sleep_minutes, actual_sleep_minutes, interruptions, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "2026-05-12",
            "{}",
            52.2606,
            "2026-05-11T22:45:00+02:00",
            "2026-05-12T05:40:00+02:00",
            415,
            358,
            44,
            "2026-05-12T04:00:00+00:00",
            "2026-05-12T04:00:00+00:00",
        ),
    )
    polar_conn.commit()
    polar_conn.close()

    client = appmod.app.test_client()
    resp = client.get("/api/hrv/recovery?range_days=30&baseline_days=28")
    assert resp.status_code == 200
    payload = resp.get_json()

    latest = payload["latest"]
    assert latest["interruptions"] == 44
    assert latest["interruptionMinutes"] == 57


def test_hrv_recovery_finds_breathing_rate_recursively_in_sleep_raw(monkeypatch, tmp_path):
    _init_hrv_db(str(tmp_path / "hrv.sqlite3"))
    appmod = _load_app_module(monkeypatch, tmp_path)

    polar_conn = connections.get_polar_db()
    polar_conn.execute(
        """
        INSERT INTO polar_nightly_recharge
        (date, raw_json, ans_status, recovery_indicator, recovery_indicator_sublevel, ans_rate, mean_recovery_rri, mean_recovery_rmssd, mean_recovery_respiration_interval, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "2026-05-12",
            '{"created":"2026-05-12T03:47:00Z","meanNightlyRecoveryRespirationInterval":0,"breathingRateSamples":[]}',
            -5.9518,
            1,
            34,
            2,
            1302,
            124,
            0,
            "2026-05-12T04:00:00+00:00",
            "2026-05-12T04:00:00+00:00",
        ),
    )
    polar_conn.execute(
        """
        INSERT INTO polar_sleep
        (date, raw_json, sleep_score, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            "2026-05-12",
            '{"sleepDate":"2026-05-12","sleepScore":{"sleepScore":52.2606},"sleepEvaluation":{"respirationRate":14.2}}',
            52.2606,
            "2026-05-12T04:00:00+00:00",
            "2026-05-12T04:00:00+00:00",
        ),
    )
    polar_conn.commit()
    polar_conn.close()

    client = appmod.app.test_client()
    resp = client.get("/api/hrv/recovery?range_days=30&baseline_days=28")
    assert resp.status_code == 200
    payload = resp.get_json()

    latest = payload["latest"]
    assert latest["breathingRate"] == 14.2


def test_hrv_flag_migration_maps_legacy_values(monkeypatch, tmp_path):
    _init_hrv_db(str(tmp_path / "hrv.sqlite3"))
    appmod = _load_app_module(monkeypatch, tmp_path)

    conn = sqlite3.connect(connections.HRV_DB)
    conn.execute(
        "INSERT INTO hrv_measurements (ts_measurement, date_utc, hr, rmssd, source_file, created_at, sickness_bool, alcohol_bool) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("2026-05-12 06:00:00+0000", "2026-05-12 00:00:00+0000", 50, 80, "HRV4Training", "2026-05-12 06:01:00+0000", 1, 0),
    )
    conn.execute(
        "INSERT INTO hrv_measurements (ts_measurement, date_utc, hr, rmssd, source_file, created_at, sickness_bool, alcohol_bool) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("2026-05-11 06:00:00+0000", "2026-05-11 00:00:00+0000", 51, 81, "HRV4Training", "2026-05-11 06:01:00+0000", 0, 1),
    )
    conn.execute(
        "INSERT INTO hrv_measurements (ts_measurement, date_utc, hr, rmssd, source_file, created_at, sickness_bool, alcohol_bool) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("2026-05-10 06:00:00+0000", "2026-05-10 00:00:00+0000", 52, 82, "HRV4Training", "2026-05-10 06:01:00+0000", 1, 1),
    )
    conn.execute(
        "INSERT INTO hrv_measurements (ts_measurement, date_utc, hr, rmssd, source_file, created_at, sickness_bool, alcohol_bool) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("2026-05-09 06:00:00+0000", "2026-05-09 00:00:00+0000", 53, 83, "HRV4Training", "2026-05-09 06:01:00+0000", 0, 0),
    )
    conn.commit()
    conn.close()

    client = appmod.app.test_client()
    payload = client.get("/api/hrv/recovery?range_days=30&baseline_days=28").get_json()
    rows = {row["date"]: row for row in payload["rows"]}

    assert rows["2026-05-12"]["flag"] == "sick"
    assert rows["2026-05-11"]["flag"] == "alcohol"
    assert rows["2026-05-10"]["flag"] == "sick"
    assert rows["2026-05-09"]["flag"] is None


def test_hrv_flag_post_persists_and_recovery_returns_flag(monkeypatch, tmp_path):
    _init_hrv_db(str(tmp_path / "hrv.sqlite3"))
    appmod = _load_app_module(monkeypatch, tmp_path)

    conn = sqlite3.connect(connections.HRV_DB)
    conn.execute(
        "INSERT INTO hrv_measurements (ts_measurement, date_utc, hr, rmssd, source_file, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        ("2026-05-12 06:00:00+0000", "2026-05-12 00:00:00+0000", 50, 80, "HRV4Training", "2026-05-12 06:01:00+0000"),
    )
    conn.commit()
    conn.close()

    client = appmod.app.test_client()

    sick = client.post("/api/hrv/flag", json={"date": "2026-05-12", "flag": "sick"})
    assert sick.status_code == 200
    assert sick.get_json() == {"ok": True, "date": "2026-05-12", "flag": "sick"}

    alcohol = client.post("/api/hrv/flag", json={"date": "2026-05-12", "flag": "alcohol"})
    assert alcohol.status_code == 200
    assert alcohol.get_json()["flag"] == "alcohol"

    cleared = client.post("/api/hrv/flag", json={"date": "2026-05-12", "flag": None})
    assert cleared.status_code == 200
    assert cleared.get_json()["flag"] is None

    invalid = client.post("/api/hrv/flag", json={"date": "2026-05-12", "flag": "bad"})
    assert invalid.status_code == 400

    payload = client.get("/api/hrv/recovery?range_days=30&baseline_days=28").get_json()
    assert payload["rows"][0]["flag"] is None


def test_hrv_recovery_accepts_all_range(monkeypatch, tmp_path):
    _init_hrv_db(str(tmp_path / "hrv.sqlite3"))
    appmod = _load_app_module(monkeypatch, tmp_path)

    conn = sqlite3.connect(connections.HRV_DB)
    conn.execute(
        "INSERT INTO hrv_measurements (ts_measurement, date_utc, hr, rmssd, source_file, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        ("2025-01-10 06:00:00+0000", "2025-01-10 00:00:00+0000", 49, 76, "HRV4Training", "2025-01-10 06:01:00+0000"),
    )
    conn.execute(
        "INSERT INTO hrv_measurements (ts_measurement, date_utc, hr, rmssd, source_file, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        ("2026-05-12 06:00:00+0000", "2026-05-12 00:00:00+0000", 50, 80, "HRV4Training", "2026-05-12 06:01:00+0000"),
    )
    conn.commit()
    conn.close()

    client = appmod.app.test_client()
    payload = client.get("/api/hrv/recovery?range_days=all&baseline_days=28").get_json()

    assert payload["ok"] is True
    assert payload["range_days"] == "all"
    assert len(payload["usable_rows"]) == 2


def test_hrv_recovery_shows_gpt_flag_without_measurement(monkeypatch, tmp_path):
    _init_hrv_db(str(tmp_path / "hrv.sqlite3"))
    appmod = _load_app_module(monkeypatch, tmp_path)

    conn = sqlite3.connect(connections.HRV_DB)
    conn.execute(
        """
        CREATE TABLE recovery_day_flags (
          date_iso TEXT PRIMARY KEY,
          sickness_bool INTEGER NOT NULL DEFAULT 0,
          alcohol_bool INTEGER NOT NULL DEFAULT 0,
          note TEXT,
          source TEXT,
          created_at TEXT,
          updated_at TEXT
        )
        """
    )
    conn.execute(
        """
        INSERT INTO recovery_day_flags
        (date_iso, alcohol_bool, source, created_at, updated_at)
        VALUES (?, 1, 'actions_v2', ?, ?)
        """,
        ("2026-05-12", "2026-05-13T12:00:00Z", "2026-05-13T12:00:00Z"),
    )
    conn.commit()
    conn.close()

    payload = appmod.app.test_client().get("/api/hrv/recovery?range_days=all&baseline_days=28").get_json()

    row = next(row for row in payload["rows"] if row["date"] == "2026-05-12")
    assert row["flag"] == "alcohol"
    assert row["source"] == "annotation"
    assert row["usable"] is False
