from __future__ import annotations

import hashlib
from pathlib import Path


def _hashes(root: Path) -> dict[str, str]:
    return {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in sorted((root / "database").glob("*.sqlite3"))}


def test_all_database_reads_are_stable(service, repo_root):
    before = _hashes(repo_root)
    assert service.daily_snapshot()["training"]["recent_sessions"]
    assert service.training_state()["active_plan"]["title"] == "Plan A"
    assert service.training_plan()["active_plan"]["plan"]["meta"]["mode"] == "rolling_sequence"
    assert "legacy_hrv" not in service.recovery_snapshot()
    assert service.nutrition_snapshot()["effective_targets"]["protein"] == 180
    weight = service.weight_state()
    assert weight["latest"]["weight_kg"] == 70.5
    assert weight["weight_trend"]["schema_version"] == 2
    assert weight["weight_trend"]["primary"] == "calendar_week"
    assert service.runs_state()["recent"][0]["distance"] == 5000
    assert _hashes(repo_root) == before


def test_runs_state_exposes_rich_garmin_details(service):
    import json
    import sqlite3

    with sqlite3.connect(service.runs.database_path) as conn:
        conn.execute("ALTER TABLE runs ADD COLUMN source TEXT")
        conn.execute("ALTER TABLE runs ADD COLUMN external_id TEXT")
        conn.execute("UPDATE runs SET source='garmin', external_id='garmin:123'")
        conn.execute(
            "CREATE TABLE garmin_activity_payloads (external_id TEXT PRIMARY KEY, garmin_activity_id TEXT, "
            "summary_json TEXT, details_json TEXT, splits_json TEXT, typed_splits_json TEXT, "
            "split_summaries_json TEXT, weather_json TEXT, hr_zones_json TEXT, power_zones_json TEXT, "
            "gear_json TEXT, errors_json TEXT, fetched_at TEXT, updated_at TEXT)"
        )
        conn.execute(
            "INSERT INTO garmin_activity_payloads VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("garmin:123", "123", json.dumps({"activityName": "Morning Run"}),
             json.dumps({"measurementCount": 2, "metricDescriptors": [{"key": "directSpeed"}], "detailsAvailable": True}),
             "{}", "{}", "{}", json.dumps({"temp": 14}), json.dumps([{"zoneNumber": 1}]),
             "[]", "[]", "{}", "2026-01-01", "2026-01-01"),
        )
        conn.execute(
            "CREATE TABLE garmin_activity_laps (external_id TEXT, lap_index INTEGER, start_time TEXT, "
            "distance_m REAL, duration_sec REAL, moving_time_sec REAL, avg_hr REAL, max_hr REAL, "
            "avg_speed_mps REAL, max_speed_mps REAL, avg_cadence REAL, max_cadence REAL, calories REAL, "
            "elevation_gain_m REAL, elevation_loss_m REAL, min_elevation_m REAL, max_elevation_m REAL, "
            "stride_length_m REAL, start_latitude REAL, start_longitude REAL, end_latitude REAL, end_longitude REAL)"
        )
        conn.execute("INSERT INTO garmin_activity_laps (external_id,lap_index,distance_m) VALUES ('garmin:123',1,1000)")
        conn.execute(
            "CREATE TABLE garmin_activity_samples (external_id TEXT, sample_index INTEGER, timestamp_ms INTEGER, "
            "elapsed_sec REAL, moving_sec REAL, distance_m REAL, heart_rate_bpm REAL, speed_mps REAL, "
            "cadence_spm REAL, elevation_m REAL, latitude REAL, longitude REAL, vertical_speed_mps REAL, "
            "performance_condition REAL)"
        )
        conn.execute("INSERT INTO garmin_activity_samples (external_id,sample_index,heart_rate_bpm) VALUES ('garmin:123',0,140)")
        conn.execute(
            "CREATE TABLE garmin_activity_files (external_id TEXT PRIMARY KEY, format TEXT, size_bytes INTEGER, "
            "sha256 TEXT, downloaded_at TEXT)"
        )
        conn.execute("INSERT INTO garmin_activity_files VALUES ('garmin:123','original',42,'abc','2026-01-01')")

    run = service.runs_state(1)["recent"][0]
    details = run["garmin_details"]
    assert details["activity_id"] == "123"
    assert details["laps"][0]["distance_m"] == 1000
    assert details["sample_series"]["samples"][0]["heart_rate_bpm"] == 140
    assert details["original_file"]["size_bytes"] == 42


def test_context_snapshot_keeps_garmin_detail_payload_compact(service, monkeypatch):
    seen = {}
    original = service.runs_state

    def wrapped(limit=10, *, include_garmin_details=True):
        seen["include_garmin_details"] = include_garmin_details
        return original(limit, include_garmin_details=include_garmin_details)

    monkeypatch.setattr(service, "runs_state", wrapped)
    service.context_snapshot()
    assert seen["include_garmin_details"] is False


def test_memory_search_uses_get_only(monkeypatch, repo_root):
    from liva_mcp.config import Config
    from liva_mcp.read_service import ReadService

    seen = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        headers = {}

        def read(self, limit):
            return b'{"memos":[{"name":"memos/1","content":"hello world","snippet":"hello"}]}'

    class Opener:
        def open(self, req, timeout):
            seen.append(req.get_method())
            return Response()

    monkeypatch.setattr("liva_mcp.read_service.request.build_opener", lambda *handlers: Opener())
    service = ReadService(Config(repo_root=repo_root, memos_base_url="http://memos.invalid"))
    result = service.memory_search("hello", 10)
    assert result["count"] == 1
    assert seen == ["GET"]


def test_memory_search_filters_direct_user_memos_by_visible_tag(monkeypatch, repo_root):
    from liva_mcp.config import Config
    from liva_mcp.read_service import ReadService

    class Response:
        headers = {}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self, limit):
            return (
                b'{"memos":['
                b'{"name":"memos/text","content":"#texte\\n\\nEin Gedicht"},'
                b'{"name":"memos/other","content":"#privat\\n\\nEine Notiz"}'
                b']}'
            )

    class Opener:
        def open(self, req, timeout):
            return Response()

    monkeypatch.setattr("liva_mcp.read_service.request.build_opener", lambda *handlers: Opener())
    service = ReadService(Config(repo_root=repo_root, memos_base_url="http://memos.invalid"))

    result = service.memory_search(None, 10, domain="texte")

    assert result["count"] == 1
    assert result["memos"][0]["memo_name"] == "memos/text"
    assert result["memos"][0]["tags"] == ["#texte"]


def test_future_training_and_weight_rows_are_excluded(service):
    service.training.database_path.parent.joinpath("training.sqlite3")
    with __import__("sqlite3").connect(service.training.database_path) as conn:
        conn.execute("INSERT INTO workouts VALUES (2, '2999-01-01', 'Future', '2999-01-01')")
    with __import__("sqlite3").connect(service.nutrition.database_path) as conn:
        conn.execute("INSERT INTO weight_logs (id,date_iso,weight_kg,created_at) VALUES (2, '2999-01-01', 999, '2999-01-01')")
    assert all(row["name"] != "Future" for row in service.training_state()["recent_sessions"])
    assert all(row["weight_kg"] != 999 for row in service.weight_state()["recent"])


def test_stale_recovery_is_not_reported_fresh(service):
    with __import__("sqlite3").connect(service.hrv.database_path) as conn:
        conn.execute("UPDATE hrv_measurements SET date_utc='2000-01-01', ts_measurement='2000-01-01'")
    result = service.recovery_snapshot()
    assert result["data_status"] == "missing"
    assert "legacy_hrv" not in result


def test_invalid_plan_json_is_reported_invalid(service):
    with __import__("sqlite3").connect(service.plans.database_path) as conn:
        conn.execute("UPDATE gym_plans SET plan_json='not-json'")
    result = service.training_plan()
    assert result["data_status"] == "invalid"
    assert result["active_plan"]["plan_parse_error"] is True
    assert result["active_plan"]["plan"] is None


def test_legacy_active_plan_is_used_when_no_active_gym_plan(service):
    with __import__("sqlite3").connect(service.plans.database_path) as conn:
        conn.execute("UPDATE gym_plans SET is_active=0")
        conn.execute(
            "CREATE TABLE plans (id INTEGER PRIMARY KEY, name TEXT, data TEXT, is_active INTEGER, updated_at TEXT)"
        )
        conn.execute("INSERT INTO plans VALUES (7, 'Legacy', '{}', 1, '2026-01-02')")
    result = service.training_plan()
    assert result["data_status"] == "ok"
    assert result["active_plan"]["source"] == "plans"
    assert result["active_plan"]["title"] == "Legacy"


def test_historical_rows_are_reported_stale(service):
    with __import__("sqlite3").connect(service.training.database_path) as conn:
        conn.execute("UPDATE workouts SET date_iso='2000-01-01'")
    with __import__("sqlite3").connect(service.nutrition.database_path) as conn:
        conn.execute("UPDATE weight_logs SET date_iso='2000-01-01'")
    with __import__("sqlite3").connect(service.runs.database_path) as conn:
        conn.execute("UPDATE runs SET date='2000-01-01'")
    assert service.training_state()["data_status"] == "stale"
    assert service.weight_state()["data_status"] == "stale"
    assert service.runs_state()["data_status"] == "stale"
