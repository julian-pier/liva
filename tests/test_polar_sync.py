from __future__ import annotations

import importlib
from datetime import date
from pathlib import Path

import database.connections as connections


def test_ensure_polar_schema_adds_sync_tables(tmp_path):
    connections.POLAR_DB = str(tmp_path / "polar.sqlite3")
    import integrations.polar_client as polar_client_module
    importlib.reload(polar_client_module)
    polar_client_module.ensure_polar_schema()

    conn = connections.get_polar_db()
    tables = {
        row["name"]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    conn.close()

    assert {
        "polar_tokens",
        "polar_sync_log",
        "polar_sleep",
        "polar_nightly_recharge",
        "polar_continuous_samples",
        "polar_activity",
        "polar_training_sessions",
    }.issubset(tables)


def test_sync_range_stores_normalized_rows(tmp_path):
    connections.POLAR_DB = str(tmp_path / "polar.sqlite3")
    import integrations.polar_client as polar_client_module
    importlib.reload(polar_client_module)
    import integrations.polar_sync as polar_sync_module
    importlib.reload(polar_sync_module)
    polar_client_module.ensure_polar_schema()

    class StubClient:
        def get_sleeps(self, from_date, to_date, features=None):
            return [{"sleepDate": from_date, "sleepScore": 82, "sleepStart": f"{from_date}T00:00:00Z", "sleepEnd": f"{to_date}T07:30:00Z", "duration": 450, "actualSleep": 420, "deepSleep": 95, "remSleep": 110, "interruptions": 2}]

        def get_nightly_recharge(self, from_date, to_date, features=None):
            return [{"date": from_date, "ansStatus": 3, "recoveryIndicator": 72, "recoveryIndicatorSubLevel": 2, "ansRate": 68, "meanNightlyRecoveryRri": 1012, "meanNightlyRecoveryRmssd": 54, "meanNightlyRecoveryRespirationInterval": 4.2}]

        def get_continuous_samples(self, from_date, to_date, features=None):
            assert features == ["heart-rate-samples"]
            return {
                "heartRateSamplesPerDay": [
                    {
                        "date": from_date,
                        "samples": [
                            {"heartRate": 52, "triggerType": "TRIGGER_TIMED_247"},
                            {"heartRate": 58, "triggerType": "TRIGGER_TIMED_247"},
                            {"heartRate": 61, "triggerType": "TRIGGER_TIMED_247"},
                            {"heartRate": 49, "triggerType": "TRIGGER_LOW_247"},
                        ],
                    }
                ]
            }

        def get_activity(self, from_date, to_date, features=None):
            return [{"date": from_date, "steps": 10234, "activeDuration": "PT87M", "inactivityAlertCount": 1, "calories": 2510}]

        def get_training_sessions(self, from_date, to_date, features=None):
            assert from_date.endswith("T00:00:00")
            assert not from_date.endswith("T00:00:00Z")
            assert to_date.endswith("T00:00:00")
            assert not to_date.endswith("T00:00:00Z")
            return [{"identifier": {"id": f"session-{from_date}"}, "startTime": f"{from_date}T10:00:00Z", "stopTime": f"{from_date}T11:00:00Z", "durationMillis": 3600000, "sport": "RUNNING", "name": "Morning Run", "avgHeartRate": 145, "maxHeartRate": 172, "calories": 550}]

    summary = polar_sync_module.sync_polar_range(StubClient(), days=2)

    assert summary["ok"] is True
    assert summary["sleep"] == {"stored_rows": 2, "populated_days": 2}
    assert summary["nightly_recharge"] == {"stored_rows": 2, "populated_days": 2}
    assert summary["continuous_samples"] == {"stored_rows": 2, "populated_days": 2}
    assert summary["activity"] == {"stored_rows": 2, "populated_days": 2}
    assert summary["training_sessions"] == {"stored_sessions": 2}

    conn = connections.get_polar_db()
    sleep_row = conn.execute("SELECT * FROM polar_sleep ORDER BY date DESC LIMIT 1").fetchone()
    continuous_row = conn.execute("SELECT * FROM polar_continuous_samples ORDER BY date DESC LIMIT 1").fetchone()
    training_count = conn.execute("SELECT COUNT(*) AS c FROM polar_training_sessions").fetchone()["c"]
    conn.close()

    assert sleep_row["sleep_score"] == 82
    assert continuous_row["sample_count"] == 4
    assert continuous_row["resting_candidate_hr"] == 49
    assert training_count == 2


def test_sync_range_reports_failure_when_any_source_request_fails(tmp_path):
    connections.POLAR_DB = str(tmp_path / "polar.sqlite3")
    import integrations.polar_client as polar_client_module
    importlib.reload(polar_client_module)
    import integrations.polar_sync as polar_sync_module
    importlib.reload(polar_sync_module)
    polar_client_module.ensure_polar_schema()

    class FailingClient:
        def get_sleeps(self, *_args, **_kwargs):
            raise polar_client_module.PolarClientError("Polar nicht mehr autorisiert: bitte neu verbinden.")

        get_nightly_recharge = get_sleeps
        get_continuous_samples = get_sleeps
        get_activity = get_sleeps
        get_training_sessions = get_sleeps

    summary = polar_sync_module.sync_polar_range(FailingClient(), days=1)

    assert summary["ok"] is False
    assert summary["errors"]
    assert "nicht mehr autorisiert" in summary["errors"][0]


def test_sleep_payload_with_night_sleeps_and_nested_sleep_score_is_stored(tmp_path):
    connections.POLAR_DB = str(tmp_path / "polar.sqlite3")
    import integrations.polar_client as polar_client_module
    importlib.reload(polar_client_module)
    import integrations.polar_sync as polar_sync_module
    importlib.reload(polar_sync_module)
    polar_client_module.ensure_polar_schema()

    class StubClient:
        def get_sleeps(self, from_date, to_date, features=None):
            assert features == ["sleep-score", "sleep-result", "sleep-evaluation"]
            return {
                "nightSleeps": [
                    {
                        "sleepDate": from_date,
                        "sleepScore": {"sleepScore": 86.8797},
                    }
                ]
            }

        def get_nightly_recharge(self, from_date, to_date, features=None):
            return []

        def get_continuous_samples(self, from_date, to_date, features=None):
            return []

        def get_activity(self, from_date, to_date, features=None):
            return []

        def get_training_sessions(self, from_date, to_date, features=None):
            return []

    summary = polar_sync_module.sync_polar_range(StubClient(), days=1)
    latest = polar_sync_module.latest_polar_snapshot()

    assert summary["sleep"] == {"stored_rows": 1, "populated_days": 1}
    assert latest["latest_sleep"]["sleep_score"] == 86.8797
    assert latest["latest_sleep"]["has_real_values"] is True


def test_sleep_payload_with_sleep_result_and_evaluation_is_stored(tmp_path):
    connections.POLAR_DB = str(tmp_path / "polar.sqlite3")
    import integrations.polar_client as polar_client_module
    importlib.reload(polar_client_module)
    import integrations.polar_sync as polar_sync_module
    importlib.reload(polar_sync_module)
    polar_client_module.ensure_polar_schema()

    summary = polar_sync_module._store_sleep(
        "2026-05-12",
        {
            "sleepDate": "2026-05-12",
            "sleepResult": {
                "hypnogram": {
                    "sleepStart": "2026-05-11T23:41:00+02:00",
                    "sleepEnd": "2026-05-12T05:47:00+02:00",
                }
            },
            "sleepEvaluation": {
                "asleepDuration": "21960s",
                "phaseDurations": {
                    "deep": "5100s",
                    "rem": "4320s",
                    "light": "12540s",
                    "wake": "1800s",
                },
                "interruptions": {
                    "totalCount": 6,
                    "totalDuration": "1800s",
                }
            },
            "sleepScore": {
                "sleepScore": 52.2606
            }
        },
    )

    latest = polar_sync_module.latest_polar_snapshot()

    assert summary == {"stored_rows": 1, "populated_days": 1}
    assert latest["latest_sleep"]["sleep_start"] == "2026-05-11T23:41:00+02:00"
    assert latest["latest_sleep"]["sleep_end"] == "2026-05-12T05:47:00+02:00"
    assert latest["latest_sleep"]["actual_sleep_minutes"] == 366
    assert latest["latest_sleep"]["deep_sleep_minutes"] == 85
    assert latest["latest_sleep"]["rem_sleep_minutes"] == 72
    assert latest["latest_sleep"]["light_sleep_minutes"] == 209
    assert latest["latest_sleep"]["awake_minutes"] == 30
    assert latest["latest_sleep"]["interruptions"] == 6
    assert latest["latest_sleep"]["interruption_minutes"] == 30
    assert latest["latest_sleep"]["has_real_values"] is True


def test_sleep_duration_falls_back_to_sleep_window_when_asleep_duration_missing(tmp_path):
    connections.POLAR_DB = str(tmp_path / "polar.sqlite3")
    import integrations.polar_client as polar_client_module
    importlib.reload(polar_client_module)
    import integrations.polar_sync as polar_sync_module
    importlib.reload(polar_sync_module)
    polar_client_module.ensure_polar_schema()

    summary = polar_sync_module._store_sleep(
        "2026-05-12",
        {
            "sleepDate": "2026-05-12",
            "sleepResult": {
                "hypnogram": {
                    "sleepStart": "2026-05-11T23:41:00+02:00",
                    "sleepEnd": "2026-05-12T05:47:00+02:00",
                }
            },
            "sleepScore": {
                "sleepScore": 52.2606
            }
        },
    )

    latest = polar_sync_module.latest_polar_snapshot()

    assert summary == {"stored_rows": 1, "populated_days": 1}
    assert latest["latest_sleep"]["sleep_minutes"] == 366


def test_sleep_shell_payload_does_not_override_existing_score(tmp_path):
    connections.POLAR_DB = str(tmp_path / "polar.sqlite3")
    import integrations.polar_client as polar_client_module
    importlib.reload(polar_client_module)
    import integrations.polar_sync as polar_sync_module
    importlib.reload(polar_sync_module)
    polar_client_module.ensure_polar_schema()

    first = polar_sync_module._store_sleep(
        "2026-05-12",
        {
            "sleepDate": "2026-05-12",
            "sleepScore": {"sleepScore": 52.2606},
        },
    )
    second = polar_sync_module._store_sleep(
        "2026-05-12",
        {
            "nightSleeps": [
                {
                    "sleepDate": "2026-05-12",
                }
            ]
        },
    )

    latest = polar_sync_module.latest_polar_snapshot()

    assert first == {"stored_rows": 1, "populated_days": 1}
    assert second == {"stored_rows": 1, "populated_days": 0}
    assert latest["latest_sleep"]["sleep_score"] == 52.2606
    assert latest["latest_sleep"]["has_real_values"] is True


def test_sleep_shell_payload_merges_with_existing_details_without_erasing_them(tmp_path):
    connections.POLAR_DB = str(tmp_path / "polar.sqlite3")
    import integrations.polar_client as polar_client_module
    importlib.reload(polar_client_module)
    import integrations.polar_sync as polar_sync_module
    importlib.reload(polar_sync_module)
    polar_client_module.ensure_polar_schema()

    polar_sync_module._store_sleep(
        "2026-05-12",
        {
            "sleepDate": "2026-05-12",
            "sleepResult": {
                "hypnogram": {
                    "sleepStart": "2026-05-11T23:41:00+02:00",
                    "sleepEnd": "2026-05-12T05:47:00+02:00",
                }
            },
            "sleepEvaluation": {
                "asleepDuration": "21960s",
                "phaseDurations": {"rem": "4320s"},
                "interruptions": {"totalCount": 6},
            },
            "sleepScore": {"sleepScore": 52.2606},
        },
    )
    polar_sync_module._store_sleep(
        "2026-05-12",
        {
            "nightSleeps": [
                {
                    "sleepDate": "2026-05-12",
                }
            ]
        },
    )

    latest = polar_sync_module.latest_polar_snapshot()

    assert latest["latest_sleep"]["sleep_score"] == 52.2606
    assert latest["latest_sleep"]["sleep_start"] == "2026-05-11T23:41:00+02:00"
    assert latest["latest_sleep"]["actual_sleep_minutes"] == 366
    assert latest["latest_sleep"]["rem_sleep_minutes"] == 72
    assert latest["latest_sleep"]["interruptions"] == 6


def test_nightly_recharge_payload_with_wrapper_is_populated(tmp_path):
    connections.POLAR_DB = str(tmp_path / "polar.sqlite3")
    import integrations.polar_client as polar_client_module
    importlib.reload(polar_client_module)
    import integrations.polar_sync as polar_sync_module
    importlib.reload(polar_sync_module)
    polar_client_module.ensure_polar_schema()

    summary = polar_sync_module._store_nightly_recharge(
        "2026-05-12",
        {
            "nightlyRechargeResults": [
                {
                    "created": "2026-05-12T03:47:00Z",
                    "modified": "2026-05-12T03:47:40.977Z",
                    "ansStatus": -5.951819896697998,
                    "recoveryIndicator": 1,
                    "recoveryIndicatorSubLevel": 34,
                    "ansRate": 2,
                    "meanNightlyRecoveryRri": 1302,
                    "meanNightlyRecoveryRmssd": 124,
                    "meanNightlyRecoveryRespirationInterval": 0,
                    "sleepResultDate": "2026-05-12",
                    "hrvSamples": [],
                    "breathingRateSamples": [],
                }
            ]
        },
    )

    latest = polar_sync_module.latest_polar_snapshot()

    assert summary == {"stored_rows": 1, "populated_days": 1}
    assert latest["latest_nightly_recharge"]["date"] == "2026-05-12"
    assert latest["latest_nightly_recharge"]["ans_status"] == -5.951819896697998
    assert latest["latest_nightly_recharge"]["recovery_indicator"] == 1
    assert latest["latest_nightly_recharge"]["recovery_indicator_sublevel"] == 34
    assert latest["latest_nightly_recharge"]["ans_rate"] == 2
    assert latest["latest_nightly_recharge"]["mean_recovery_rri"] == 1302
    assert latest["latest_nightly_recharge"]["mean_recovery_rmssd"] == 124
    assert latest["latest_nightly_recharge"]["has_real_values"] is True


def test_double_wrapped_nightly_recharge_payload_is_unwrapped(tmp_path):
    connections.POLAR_DB = str(tmp_path / "polar.sqlite3")
    import integrations.polar_client as polar_client_module
    importlib.reload(polar_client_module)
    import integrations.polar_sync as polar_sync_module
    importlib.reload(polar_sync_module)
    polar_client_module.ensure_polar_schema()

    summary = polar_sync_module._store_nightly_recharge(
        "2026-05-12",
        {
            "nightlyRechargeResults": {
                "nightlyRechargeResults": [
                    {
                        "created": "2026-05-12T03:47:00Z",
                        "ansStatus": 0,
                        "recoveryIndicator": 0,
                        "recoveryIndicatorSubLevel": 0,
                        "ansRate": 0,
                        "meanNightlyRecoveryRri": 0,
                        "meanNightlyRecoveryRmssd": 0,
                        "meanNightlyRecoveryRespirationInterval": 0,
                        "sleepResultDate": "2026-05-12",
                    }
                ]
            }
        },
    )

    latest = polar_sync_module.latest_polar_snapshot()

    assert summary == {"stored_rows": 1, "populated_days": 1}
    assert latest["latest_nightly_recharge"]["has_real_values"] is True


def test_activity_days_with_empty_device_list_stores_shell_row_only(tmp_path):
    connections.POLAR_DB = str(tmp_path / "polar.sqlite3")
    import integrations.polar_client as polar_client_module
    importlib.reload(polar_client_module)
    import integrations.polar_sync as polar_sync_module
    importlib.reload(polar_sync_module)
    polar_client_module.ensure_polar_schema()

    summary = polar_sync_module._store_activity(
        "2026-05-12",
        {
            "activityDays": [
                {
                    "date": "2026-05-12",
                    "activitiesPerDevice": [],
                }
            ]
        },
    )

    latest = polar_sync_module.latest_polar_snapshot()

    assert summary == {"stored_rows": 1, "populated_days": 0}
    assert latest["latest_activity"]["date"] == "2026-05-12"
    assert latest["latest_activity"]["steps"] is None
    assert latest["latest_activity"]["active_minutes"] is None
    assert latest["latest_activity"]["calories"] is None
    assert latest["latest_activity"]["has_real_values"] is False


def test_polar_state_summary_is_compact(tmp_path):
    connections.POLAR_DB = str(tmp_path / "polar.sqlite3")
    import integrations.polar_client as polar_client_module
    importlib.reload(polar_client_module)
    import integrations.polar_sync as polar_sync_module
    importlib.reload(polar_sync_module)
    polar_client_module.ensure_polar_schema()
    today_iso = date.today().isoformat()

    conn = connections.get_polar_db()
    conn.execute("INSERT INTO polar_tokens (access_token, created_at, updated_at) VALUES (?, ?, ?)", ("token", "2026-05-09T00:00:00+00:00", "2026-05-09T00:00:00+00:00"))
    conn.execute(
        """
        INSERT INTO polar_sleep (date, raw_json, sleep_score, sleep_minutes, created_at, updated_at)
        VALUES (?, '{}', 80, 430, '2026-05-09T00:00:00+00:00', '2026-05-09T00:00:00+00:00')
        """,
        (today_iso,),
    )
    conn.execute(
        """
        INSERT INTO polar_activity (date, raw_json, steps, active_minutes, calories, created_at, updated_at)
        VALUES (?, '{}', 9000, 75, 2400, '2026-05-09T00:00:00+00:00', '2026-05-09T00:00:00+00:00')
        """,
        (today_iso,),
    )
    conn.execute(
        """
        INSERT INTO polar_continuous_samples (date, raw_json, sample_count, min_hr, max_hr, avg_hr, resting_candidate_hr, created_at, updated_at)
        VALUES (?, '{}', 10, 48, 120, 62, 50, '2026-05-09T00:00:00+00:00', '2026-05-09T00:00:00+00:00')
        """,
        (today_iso,),
    )
    conn.commit()
    conn.close()

    state = polar_sync_module.polar_state_summary()

    assert state["source"] == "polar"
    assert state["connected"] is True
    assert "today" in state
    assert state["today"]["sleep"]["sleep_minutes"] == 430


def test_empty_shell_rows_are_not_counted_as_real_values(tmp_path):
    connections.POLAR_DB = str(tmp_path / "polar.sqlite3")
    import integrations.polar_client as polar_client_module
    importlib.reload(polar_client_module)
    import integrations.polar_sync as polar_sync_module
    importlib.reload(polar_sync_module)
    polar_client_module.ensure_polar_schema()

    class StubClient:
        def get_sleeps(self, from_date, to_date, features=None):
            return [{"sleepDate": from_date}]

        def get_nightly_recharge(self, from_date, to_date, features=None):
            return [{"date": from_date}]

        def get_continuous_samples(self, from_date, to_date, features=None):
            return []

        def get_activity(self, from_date, to_date, features=None):
            return [{"date": from_date}]

        def get_training_sessions(self, from_date, to_date, features=None):
            return []

    summary = polar_sync_module.sync_polar_range(StubClient(), days=1)

    assert summary["sleep"] == {"stored_rows": 1, "populated_days": 0}
    assert summary["nightly_recharge"] == {"stored_rows": 1, "populated_days": 0}
    assert summary["activity"] == {"stored_rows": 1, "populated_days": 0}
    assert summary["continuous_samples"] == {"stored_rows": 0, "populated_days": 0}
    assert summary["training_sessions"] == {"stored_sessions": 0}

    latest = polar_sync_module.latest_polar_snapshot()
    assert latest["latest_sleep"]["has_real_values"] is False


def test_continuous_payload_with_heart_rate_samples_per_day_is_stored(tmp_path):
    connections.POLAR_DB = str(tmp_path / "polar.sqlite3")
    import integrations.polar_client as polar_client_module
    importlib.reload(polar_client_module)
    import integrations.polar_sync as polar_sync_module
    importlib.reload(polar_sync_module)
    polar_client_module.ensure_polar_schema()

    summary = polar_sync_module._store_continuous_samples(
        "2026-05-09",
        {
            "heartRateSamplesPerDay": [
                {
                    "date": "2026-05-09",
                    "deviceRef": {"id": "loop2"},
                    "samples": [
                        {"heartRate": 87, "offsetMillis": 69322000, "triggerType": "TRIGGER_TIMED_247"},
                        {"heartRate": 64, "offsetMillis": 69332000, "triggerType": "TRIGGER_LOW_247"},
                        {"heartRate": 72, "offsetMillis": 69342000, "triggerType": "TRIGGER_TIMED_247"},
                    ],
                }
            ]
        },
    )

    assert summary == {"stored_rows": 1, "populated_days": 1}
    conn = connections.get_polar_db()
    row = conn.execute("SELECT date, sample_count, min_hr, max_hr, avg_hr, resting_candidate_hr FROM polar_continuous_samples").fetchone()
    conn.close()
    assert row["date"] == "2026-05-09"
    assert row["sample_count"] == 3
    assert row["min_hr"] == 64
    assert row["max_hr"] == 87
    assert row["resting_candidate_hr"] == 64
