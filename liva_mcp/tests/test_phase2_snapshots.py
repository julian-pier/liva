from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path

import liva_mcp.read_service as read_module
from liva_mcp.tool_registry import TOOL_BY_NAME


OLD_NAMES = {"liva_daily_context", "liva_recovery_state", "liva_nutrition_state"}
NEW_NAMES = {"liva_daily_snapshot", "liva_recovery_snapshot", "liva_nutrition_snapshot"}


def _execute(path: Path, *statements: tuple[str, tuple]) -> None:
    with sqlite3.connect(path) as conn:
        for sql, params in statements:
            conn.execute(sql, params)


def test_snapshot_names_replace_old_names_everywhere():
    assert NEW_NAMES.issubset(TOOL_BY_NAME)
    assert OLD_NAMES.isdisjoint(TOOL_BY_NAME)
    root = Path(__file__).resolve().parents[1]
    checked = [root / "README.md", root / "src/liva_mcp/tool_registry.py", root / "src/liva_mcp/schemas.py"]
    source = "\n".join(path.read_text(encoding="utf-8") for path in checked)
    assert not any(name in source for name in OLD_NAMES)


def test_recovery_prefers_fresh_polar_components_without_inventing_readiness(service, monkeypatch):
    monkeypatch.setattr(read_module, "berlin_today", lambda: date(2026, 7, 10))
    _execute(
        service.hrv.database_path,
        ("UPDATE hrv_measurements SET date_utc='2026-05-01', ts_measurement='2026-05-01'", ()),
    )
    _execute(
        service.polar.database_path,
        ("INSERT INTO polar_sleep (date,raw_json,sleep_score,actual_sleep_minutes,updated_at) VALUES (?,?,?,?,?)", ("2026-07-10", "{}", 81, 420, "2026-07-10T07:00:00+02:00")),
        ("INSERT INTO polar_nightly_recharge (date,raw_json,ans_status,mean_recovery_rmssd,updated_at) VALUES (?,?,?,?,?)", ("2026-07-10", "{}", 2.5, 88, "2026-07-10T07:00:00+02:00")),
        ("INSERT INTO polar_continuous_samples (date,raw_json,sample_count,resting_candidate_hr,updated_at) VALUES (?,?,?,?,?)", ("2026-07-10", "{}", 100, 42, "2026-07-10T07:00:00+02:00")),
    )
    result = service.recovery_snapshot()
    assert result["data_status"] == "fresh"
    assert "legacy_hrv" not in result
    assert "legacy_hrv" not in result["freshness"]
    assert result["freshness"]["polar_sleep"]["status"] == "fresh"
    assert result["polar"]["nightly_recharge"]["mean_recovery_rmssd"] == 88
    assert result["canonical_readiness_available"] is False
    assert result["canonical_parity"]["canonical_equivalent"] is False


def test_recovery_without_polar_reports_missing_components(service):
    result = service.recovery_snapshot()
    assert {"polar_sleep", "polar_nightly_recharge", "polar_continuous_hr"}.issubset(result["missing_components"])


def test_recovery_flags_and_annotations_are_read_only_components(service, monkeypatch):
    monkeypatch.setattr(read_module, "berlin_today", lambda: date(2026, 7, 10))
    with sqlite3.connect(service.hrv.database_path) as conn:
        conn.execute("CREATE TABLE recovery_day_flags (date_iso TEXT PRIMARY KEY, sickness_bool INTEGER, alcohol_bool INTEGER, note TEXT, source TEXT, updated_at TEXT)")
        conn.execute("CREATE TABLE hrv_day_flags (date TEXT PRIMARY KEY, flag TEXT, updated_at TEXT)")
        conn.execute("CREATE TABLE recovery_day_annotations (id INTEGER PRIMARY KEY, date_iso TEXT, annotation_type TEXT, value TEXT, note TEXT, source TEXT, updated_at TEXT)")
        conn.execute("INSERT INTO recovery_day_flags VALUES ('2026-07-10',1,0,'sick','test','2026-07-10')")
        conn.execute("INSERT INTO hrv_day_flags VALUES ('2026-07-09','alcohol','2026-07-10')")
        conn.execute("INSERT INTO recovery_day_annotations VALUES (1,'2026-07-10','sleep','poor','note','test','2026-07-10')")
    result = service.recovery_snapshot()
    assert result["flags"]["sickness"] is True
    assert result["flags"]["alcohol"] is False
    assert result["flags"]["historical_hrv_day_flags"][0]["date"] == "2026-07-09"
    assert result["annotations"][0]["annotation_type"] == "sleep"


def test_nutrition_target_priority_override_template_settings(service, monkeypatch):
    monkeypatch.setattr(read_module, "berlin_today", lambda: date(2026, 7, 10))
    with sqlite3.connect(service.nutrition.database_path) as conn:
        conn.execute("INSERT INTO nutrition_week_templates VALUES (1,'Week',1,'x','2026-07-10',NULL,NULL,'custom','{}')")
        conn.execute("INSERT INTO nutrition_week_template_days VALUES (1,1,4,'standard',2900,185,400,55,'x','2026-07-10')")
        conn.execute("INSERT INTO actions_v2_state VALUES ('active_targets_override',?,?)", (json.dumps({"kcal": 2650, "protein": 180, "carbs": 300, "fat": 65}), "2026-07-10"))
    result = service.nutrition_snapshot()
    assert result["target_source"] == "active_targets_override"
    assert result["effective_targets"]["kcal"] == 2650
    assert result["target_candidates"]["active_week_template_day"]["kcal"] == 2900


def test_nutrition_today_actuals_and_missing_actuals(service, monkeypatch):
    monkeypatch.setattr(read_module, "berlin_today", lambda: date(2026, 7, 10))
    with sqlite3.connect(service.nutrition.database_path) as conn:
        conn.execute("DELETE FROM nutrition_daily")
        conn.execute("INSERT INTO nutrition_day_actuals VALUES (1,'2026-07-10',2400,170,250,70,'mfp','x','2026-07-10')")
    result = service.nutrition_snapshot()
    assert result["today_actuals"]["kcal"] == 2400
    assert result["actual_source"] == "nutrition_day_actuals"
    with sqlite3.connect(service.nutrition.database_path) as conn:
        conn.execute("DELETE FROM nutrition_day_actuals")
    missing = service.nutrition_snapshot()
    assert missing["today_actuals"] is None
    assert "today_actuals" in missing["missing_components"]


def test_berlin_date_selects_snapshot_day(service, monkeypatch):
    monkeypatch.setattr(read_module, "berlin_today", lambda: date(2026, 7, 11))
    with sqlite3.connect(service.nutrition.database_path) as conn:
        conn.execute("INSERT INTO nutrition_daily VALUES ('2026-07-11',2200,160,200,60)")
    result = service.nutrition_snapshot()
    assert result["local_date"] == "2026-07-11"
    assert result["nutrition_daily"]["date"] == "2026-07-11"


def test_berlin_day_boundary_uses_local_timezone():
    utc_late_evening = datetime(2026, 7, 10, 22, 30, tzinfo=timezone.utc)
    assert read_module.berlin_today(utc_late_evening) == date(2026, 7, 11)


def test_calendar_limit_missing_card_and_persisted_decision_readback(service, monkeypatch):
    monkeypatch.setattr(read_module, "berlin_today", lambda: date(2026, 7, 10))
    with sqlite3.connect(service.training.database_path) as conn:
        conn.execute("INSERT INTO school_schedule_snapshots VALUES (1,'2026-07-10T08:00:00+02:00','test','2026-07-10','{}','{}','x')")
        for idx in range(40):
            conn.execute("INSERT INTO school_schedule_entries VALUES (?,?,?,?,?,?,?,?,?,?)", (idx + 1, 1, "2026-07-10", f"Event {idx}", "T", "R", f"{idx % 24:02d}:00", f"{idx % 24:02d}:30", "", "confirmed"))
    with sqlite3.connect(service.core.database_path) as conn:
        conn.execute("INSERT INTO training_ai_decisions (id,day_iso,planned_session_id,plan_id,session_name,session_type,status,source,context_hash,decision_json,trigger_reason,created_at,updated_at) VALUES (1,'2026-07-10',3,1,'Pull','gym','fresh','gpt','hash','{}','none','x','2026-07-10')")
        before = conn.execute("SELECT COUNT(*) FROM training_ai_decisions").fetchone()[0]
    result = service.daily_snapshot()
    with sqlite3.connect(service.core.database_path) as conn:
        after = conn.execute("SELECT COUNT(*) FROM training_ai_decisions").fetchone()[0]
    assert result["calendar"]["event_count"] == 25
    assert result["training"]["persisted_training_card"] is None
    assert result["training"]["persisted_ai_decision"]["session_name"] == "Pull"
    assert result["training"]["planned_session"]["source"] == "persisted_training_ai_decision"
    assert before == after
    assert "training_card" in result["missing_components"]
    assert result["canonical_parity"]["canonical_equivalent"] is False


def test_all_snapshots_have_required_envelope(service):
    for result in (service.daily_snapshot(), service.recovery_snapshot(), service.nutrition_snapshot()):
        assert {"generated_at", "local_date", "timezone", "data_status", "freshness", "sources", "missing_components", "canonical_parity"}.issubset(result)
        assert result["canonical_parity"]["canonical_equivalent"] is False
