from __future__ import annotations

import sqlite3


def test_normalize_garmin_run():
    from garmin_sync.service import normalize_activity

    row = normalize_activity(
        {
            "activityId": 12345,
            "activityName": "Morning Run",
            "activityType": {"typeKey": "running"},
            "startTimeLocal": "2026-09-07 07:30:00",
            "distance": 5000,
            "movingDuration": 1500,
            "averageHR": 145,
            "maxHR": 172,
            "elevationGain": 44,
        }
    )

    assert row["id"] == -12345
    assert row["external_id"] == "garmin:12345"
    assert row["sport_type"] == "run"
    assert row["pace"] == 300
    assert row["avg_hr"] == 145


def test_store_garmin_activities_is_idempotent(tmp_path, monkeypatch):
    from garmin_sync import service

    db_path = tmp_path / "runs.sqlite3"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE runs (
            id INTEGER PRIMARY KEY, date TEXT, distance REAL, moving_time INTEGER,
            avg_speed REAL, avg_hr REAL, max_hr REAL, elevation_gain REAL, pace REAL,
            sport_type TEXT, run_type TEXT, note TEXT, avg_power REAL, stair_floors REAL
        )
        """
    )
    conn.close()
    monkeypatch.setattr(service, "get_runs_db", lambda: sqlite3.connect(db_path))
    notifications = []
    monkeypatch.setattr(
        service,
        "send_ntfy_notification",
        lambda text, **kwargs: notifications.append((text, kwargs)) or {"sent": True},
    )
    activity = {
        "activityId": 12345,
        "activityType": {"typeKey": "running"},
        "startTimeLocal": "2026-09-07 07:30:00",
        "distance": 5000,
        "movingDuration": 1500,
    }

    first = service._store_activities([activity])
    second = service._store_activities([activity])

    assert first["imported"] == 1
    assert first["notifications_sent"] == 1
    assert second["imported"] == 0
    assert second["updated"] == 1
    assert second["notifications_sent"] == 0
    assert len(notifications) == 1
    assert notifications[0][0] == "Neuer Lauf: Neuer Lauf · 5,00 km · 25:00 min"
    assert notifications[0][1]["title"] == "LIVA · Garmin"
    conn = sqlite3.connect(db_path)
    assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1
    conn.close()


def test_failed_notification_is_retried(tmp_path, monkeypatch):
    from garmin_sync import service

    db_path = tmp_path / "runs.sqlite3"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE runs (
            id INTEGER PRIMARY KEY, date TEXT, distance REAL, moving_time INTEGER,
            avg_speed REAL, avg_hr REAL, max_hr REAL, elevation_gain REAL, pace REAL,
            sport_type TEXT, run_type TEXT, note TEXT, avg_power REAL, stair_floors REAL
        )
        """
    )
    conn.close()
    monkeypatch.setattr(service, "get_runs_db", lambda: sqlite3.connect(db_path))
    outcomes = iter(({"sent": False, "error": "offline"}, {"sent": True}))
    monkeypatch.setattr(service, "send_ntfy_notification", lambda *args, **kwargs: next(outcomes))
    activity = {
        "activityId": 99,
        "activityType": {"typeKey": "running"},
        "startTimeLocal": "2026-09-07 08:00:00",
        "movingDuration": 1200,
    }

    first = service._store_activities([activity])
    second = service._store_activities([activity])

    assert first["notifications_failed"] == 1
    assert second["notifications_sent"] == 1
    conn = sqlite3.connect(db_path)
    assert conn.execute(
        "SELECT attempts, sent_at, last_error FROM garmin_sync_notifications WHERE external_id='garmin:99'"
    ).fetchone()[0] == 2
    conn.close()


def test_detail_metrics_uses_garmin_descriptors():
    from garmin_sync.service import _detail_metrics

    result = _detail_metrics(
        {
            "metricDescriptors": [
                {"metricsIndex": 0, "key": "directTimestamp"},
                {"metricsIndex": 1, "key": "directHeartRate"},
                {"metricsIndex": 2, "key": "sumDistance"},
            ],
            "activityDetailMetrics": [
                {"metrics": [1788753449000, 145, 1234.5]},
                {"metrics": [1788753450000, 146, 1237.0]},
            ],
        }
    )

    assert result == [
        {"directTimestamp": 1788753449000, "directHeartRate": 145, "sumDistance": 1234.5},
        {"directTimestamp": 1788753450000, "directHeartRate": 146, "sumDistance": 1237.0},
    ]
