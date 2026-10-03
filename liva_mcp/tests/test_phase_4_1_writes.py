from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
import threading
import time
from types import SimpleNamespace
from pathlib import Path

import pytest

from liva_mcp.remote_server import _invoke
from liva_mcp.read_service import ReadService
from liva_mcp.tool_registry import call_tool
from liva_mcp.write_service import WriteService
from app_support.training_decision_service import _connect


WRITE = {"liva.read", "liva.write"}


def invoke(service, name, arguments):
    return call_tool(service, name, arguments, scopes=WRITE, client_id="test-client", request_id="test-request")


@pytest.fixture(autouse=True)
def assert_write_paths_are_temporary(service):
    root = Path(service.config.repo_root).resolve()
    for path in (service.config.database_path("ernaehrung.sqlite3"), service.config.write_database_path()):
        assert Path(path).resolve().is_relative_to(root)


def test_note_dry_run_and_idempotent_live_readback(service):
    arguments = {"category": "general", "text": "annotation", "dry_run": False, "reason": "user asked", "idempotency_key": "note-1"}
    dry = invoke(service, "liva_post_daily_note", {"category": "general", "text": "annotation"})
    assert dry["ok"] and dry["data"]["executed"] is False
    live = invoke(service, "liva_post_daily_note", arguments)
    replay = invoke(service, "liva_post_daily_note", arguments)
    assert live["data"]["status"] == "success"
    assert live["data"]["audit_id"] > 0
    assert live["data"]["readback"]["text"] == "annotation"
    json.dumps(live)
    assert replay["data"]["idempotent_replay"] is True


def test_all_public_write_dry_runs_are_non_mutating_and_have_common_contract(service):
    calls = [
        ("liva_post_daily_note", {"category": "general", "text": "dry"}),
        ("liva_upsert_weight", {"weight_kg": 70}),
        ("liva_post_nutrition_context", {"text": "context", "precision_level": "context"}),
        ("liva_post_daily_flag", {"flag_type": "pain"}),
        ("liva_post_training_rawlog", {"raw_text": "dry rawlog"}),
        ("liva_operator_execute", {"operations": [{"operation_type": "daily.note.post", "arguments": {"category": "general", "text": "dry"}}]}),
    ]
    WriteService(service.config.write_database_path()).initialize()
    with sqlite3.connect(service.config.write_database_path()) as conn:
        before = {table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in ("daily_notes", "write_audit", "idempotency_records")}
    for name, payload in calls:
        result = invoke(service, name, payload)
        assert result["ok"], (name, result)
        data = result["data"]
        assert data["changed"] is False and data["replayed"] is False and data["executed"] is False and data["production_write"] is False
        assert data["dry_run"] is True and "audit_id" not in data
    with sqlite3.connect(service.config.write_database_path()) as conn:
        assert {table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in before} == before


def test_smoke_test_context_blocks_live_writes_but_allows_validated_dry_run(service):
    blocked = call_tool(service, "liva_post_daily_note", {"category": "general", "text": "smoke", "dry_run": False, "reason": "test"}, scopes=WRITE, client_id="smoke", smoke_test=True)
    assert blocked["error"]["code"] == "smoke_test_live_write_blocked"
    invalid = call_tool(service, "liva_post_daily_note", {"category": "general", "text": "smoke", "dry_run": True, "extra": 1}, scopes=WRITE, smoke_test=True)
    assert invalid["error"]["code"] == "invalid_arguments"
    allowed = call_tool(service, "liva_post_daily_note", {"category": "general", "text": "smoke", "dry_run": True}, scopes=WRITE, smoke_test=True)
    assert allowed["ok"] and allowed["data"]["production_write"] is False


def test_audit_is_append_only(service):
    writer = WriteService(service.config.write_database_path())
    with writer._connect() as conn:
        conn.execute("INSERT INTO write_audit (timestamp,tool_name,operation_type,dry_run,executed,reason,affected_domain,affected_ids_json,before_summary_json,after_summary_json,source) VALUES ('now','test','test',0,1,'test','test','[]','{}','{}','test')")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute("DELETE FROM write_audit WHERE tool_name='test'")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute("UPDATE write_audit SET reason='changed' WHERE tool_name='test'")


@pytest.mark.parametrize("text,valid", [
    ("actual\x00", False), ("actual\u0000", False), (r"\\u0000", True), (r"\0", True),
    ("line\nnext", True), ("tab\tvalue", True), ("unicode\u0085control", False), ("unicode\u2028line", True),
])
def test_escape_distinguishes_control_characters_from_visible_escapes(service, text, valid):
    result = invoke(service, "liva_post_daily_note", {"category": "general", "text": text})
    assert result["ok"] is valid


def test_daily_note_replay_conflict_is_atomic_and_readable(service):
    arguments = {"date": "2026-07-20", "category": "general", "text": "private annotation", "dry_run": False, "reason": "test", "idempotency_key": "daily-note-context"}
    first = invoke(service, "liva_post_daily_note", arguments)
    replay = invoke(service, "liva_post_daily_note", arguments)
    fresh = ReadService(service.config)
    replay_from_fresh_service = invoke(fresh, "liva_post_daily_note", arguments)
    conflict = invoke(service, "liva_post_daily_note", {**arguments, "text": "different annotation"})
    assert first["data"]["changed"] is True and first["data"]["replayed"] is False
    assert replay["data"]["changed"] is False and replay["data"]["replayed"] is True
    assert replay_from_fresh_service["data"]["changed"] is False and replay_from_fresh_service["data"]["replayed"] is True
    assert conflict["ok"] is False and "idempotency_key_context_conflict" in conflict["error"]["message"]
    with sqlite3.connect(service.config.write_database_path()) as conn:
        assert conn.execute("SELECT COUNT(*) FROM daily_notes WHERE date_iso=?", ("2026-07-20",)).fetchone()[0] == 1
        stored = conn.execute("SELECT response_json FROM idempotency_records WHERE idempotency_key=?", ("daily-note-context",)).fetchone()[0]
        audit = conn.execute("SELECT after_summary_json FROM write_audit WHERE tool_name=?", ("liva_post_daily_note",)).fetchone()[0]
    assert "private annotation" not in stored and "private annotation" not in audit
    assert any(row["text"] == "private annotation" for row in fresh.overlay.recent("daily_notes", "2026-07-20"))


def test_daily_note_parallel_duplicates_have_one_mutation(service):
    arguments = {"date": "2026-07-21", "category": "system", "text": "parallel annotation", "dry_run": False, "reason": "test", "idempotency_key": "daily-note-parallel"}
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: invoke(service, "liva_post_daily_note", arguments), range(4)))
    assert sum(result["ok"] and result["data"]["changed"] for result in results) == 1
    assert sum(result["ok"] and result["data"]["replayed"] for result in results) == 3
    with sqlite3.connect(service.config.write_database_path()) as conn:
        assert conn.execute("SELECT COUNT(*) FROM daily_notes WHERE date_iso=?", ("2026-07-21",)).fetchone()[0] == 1


def test_daily_note_scope_validation_dry_run_and_pragmas(service):
    assert call_tool(service, "liva_post_daily_note", {"category": "general", "text": "x"}, scopes=set())["error"]["code"] == "insufficient_scope"
    invalid = [
        {}, {"category": "general"}, {"category": "general", "text": "   "},
        {"category": "unknown", "text": "x"}, {"category": "general", "text": "x", "extra": 1},
        {"category": "general", "text": "x\x00"}, {"category": "general", "text": "x\x01"},
        {"category": "general", "text": "x", "idempotency_key": "bad\x00key"},
    ]
    for payload in invalid:
        assert invoke(service, "liva_post_daily_note", {**payload, "dry_run": False, "reason": "validation"})["ok"] is False
    dry = invoke(service, "liva_post_daily_note", {"category": "general", "text": "would not be written"})
    assert dry["data"]["changed"] is False and dry["data"]["replayed"] is False and dry["data"]["executed"] is False
    writer = WriteService(service.config.write_database_path())
    with writer._connect(busy_timeout=True) as conn:
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
        assert conn.execute("SELECT COUNT(*) FROM daily_notes").fetchone()[0] == 0


def test_daily_note_rolls_back_after_post_insert_failure(service, monkeypatch):
    writer = WriteService(service.config.write_database_path())
    original = writer._finish
    def fail(*args, **kwargs):
        raise RuntimeError("injected_after_daily_note_insert")
    monkeypatch.setattr(writer, "_finish", fail)
    value = type("Value", (), {"date": "2026-07-22", "category": "general", "text": "rollback annotation", "dry_run": False, "reason": "fault", "idempotency_key": "daily-note-rollback"})()
    with pytest.raises(RuntimeError, match="injected_after_daily_note_insert"):
        writer.daily_note(value, {})
    monkeypatch.setattr(writer, "_finish", original)
    with sqlite3.connect(service.config.write_database_path()) as conn:
        assert conn.execute("SELECT COUNT(*) FROM daily_notes").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM idempotency_records").fetchone()[0] == 0


def test_daily_note_lock_timeout_is_controlled_and_recovers(service):
    lock_path = service.config.write_database_path()
    lock = sqlite3.connect(lock_path, timeout=5, check_same_thread=False)
    lock.execute("BEGIN EXCLUSIVE")
    def release():
        time.sleep(0.1)
        lock.rollback()
        lock.close()
    thread = threading.Thread(target=release)
    thread.start()
    recovered = invoke(service, "liva_post_daily_note", {"date": "2026-07-23", "category": "system", "text": "short lock", "dry_run": False, "reason": "lock", "idempotency_key": "daily-note-lock-short"})
    thread.join()
    assert recovered["ok"] is True
    permanent = sqlite3.connect(lock_path, timeout=5)
    permanent.execute("BEGIN EXCLUSIVE")
    try:
        failed = invoke(service, "liva_post_daily_note", {"date": "2026-07-24", "category": "system", "text": "permanent lock", "dry_run": False, "reason": "lock", "idempotency_key": "daily-note-lock-long"})
        assert failed == {"ok": False, "status": "error", "error": {"code": "write_failed", "message": "Controlled write failed"}}
    finally:
        permanent.rollback()
        permanent.close()
    assert invoke(service, "liva_post_daily_note", {"date": "2026-07-24", "category": "system", "text": "recovered", "dry_run": False, "reason": "lock", "idempotency_key": "daily-note-lock-recovered"})["ok"] is True


def test_staging_writes_are_honest_and_remain_available_for_audit(service):
    note = invoke(service, "liva_post_daily_note", {"category":"system", "text":"visible note", "dry_run":False, "reason":"test"})
    nutrition = invoke(service, "liva_post_nutrition_context", {"text":"Protein safe, kcal unbekannt", "precision_level":"context", "dry_run":False, "reason":"test"})
    flag = invoke(service, "liva_post_daily_flag", {"flag_type":"pain", "body_part":"shoulder", "dry_run":False, "reason":"test"})
    daily = service.daily_snapshot()["mcp_write_context"]
    recovery = service.recovery_snapshot()["flags"]["mcp_overlay_flags"]
    assert note["data"]["visible_in_frontend"] is False and note["data"]["source"] == "staging"
    assert nutrition["data"]["visible_in_daily_snapshot"] is False
    assert any(row["text"] == "visible note" for row in daily["daily_notes"])
    assert any(row["text"] == "Protein safe, kcal unbekannt" for row in daily["nutrition_context"])
    assert any(row["body_part"] == "shoulder" for row in recovery)


def test_weight_normalizes_and_upserts(service):
    first = invoke(service, "liva_upsert_weight", {"date":"2026-07-11", "weight_kg":"70,4", "dry_run":False, "reason":"measurement"})
    second = invoke(service, "liva_upsert_weight", {"date":"2026-07-11", "weight_kg":"70.2", "dry_run":False, "reason":"correction"})
    assert first["data"]["changed"] is True and first["data"]["replayed"] is False
    assert first["data"]["readback"]["weight_kg"] == 70.4
    assert second["data"]["before"]["weight_kg"] == 70.4
    assert second["data"]["readback"]["weight_kg"] == 70.2
    assert second["data"]["source"] == "production"
    assert second["data"]["production_write"] is True
    assert second["data"]["visible_in_frontend"] is True
    assert second["data"]["readback_source"] == "weight_logs"
    assert second["data"]["changed"] is True and second["data"]["replayed"] is False
    state = service.weight_state(50)
    rows = [row for row in state["recent"] if row["date"] == "2026-07-11"]
    assert len(rows) == 1 and rows[0]["weight_kg"] == 70.2
    assert all(row.get("source") != "liva_mcp_overlay" for row in state["recent"])
    assert not invoke(service, "liva_upsert_weight", {"weight_kg": 300})["ok"]


def test_weight_replay_and_context_conflict_are_idempotent(service):
    arguments = {"date":"2026-07-11", "weight_kg":70.4, "note":"morning", "dry_run":False, "reason":"measurement", "idempotency_key":"weight-replay"}
    first = invoke(service, "liva_upsert_weight", arguments)
    replay = invoke(service, "liva_upsert_weight", arguments)
    replay_three = invoke(service, "liva_upsert_weight", arguments)
    assert first["data"]["changed"] is True and first["data"]["replayed"] is False
    assert replay["data"]["changed"] is False and replay["data"]["replayed"] is True
    assert replay_three["data"]["changed"] is False and replay_three["data"]["replayed"] is True
    conflict = invoke(service, "liva_upsert_weight", {**arguments, "weight_kg":71.0})
    assert conflict["ok"] is False
    assert conflict["error"]["code"] == "invalid_arguments"
    assert "idempotency_key_context_conflict" in conflict["error"]["message"]
    with sqlite3.connect(service.config.database_path("ernaehrung.sqlite3")) as conn:
        assert conn.execute("SELECT weight_kg FROM weight_logs WHERE date_iso=?", ("2026-07-11",)).fetchone()[0] == 70.4


def test_weight_connection_pragmas(service):
    writer = WriteService(service.config.write_database_path(), service.config.database_path("ernaehrung.sqlite3"))
    with writer._connect(busy_timeout=True) as conn:
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    with writer._production_nutrition_connect() as conn:
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000


def test_weight_rolls_back_after_post_mutation_failure(service, monkeypatch):
    writer = WriteService(service.config.write_database_path(), service.config.database_path("ernaehrung.sqlite3"))
    original = writer._row
    calls = {"count": 0}
    def fail_after_mutation(row):
        calls["count"] += 1
        if calls["count"] == 2:
            raise RuntimeError("injected_after_weight_mutation")
        return original(row)
    monkeypatch.setattr(writer, "_row", fail_after_mutation)
    with pytest.raises(RuntimeError, match="injected_after_weight_mutation"):
        writer.weight(type("Value", (), {"date":"2026-07-11", "weight_kg":71.1, "note":None, "dry_run":False, "reason":"fault", "idempotency_key":None})(), {})
    with sqlite3.connect(service.config.database_path("ernaehrung.sqlite3")) as conn:
        assert conn.execute("SELECT weight_kg FROM weight_logs WHERE date_iso=?", ("2026-07-11",)).fetchone() is None


def test_weight_short_lock_recovers_and_permanent_lock_is_controlled(service):
    lock_path = service.config.write_database_path()
    lock = sqlite3.connect(lock_path, timeout=5, check_same_thread=False)
    lock.execute("BEGIN EXCLUSIVE")
    def release():
        time.sleep(0.1)
        lock.rollback(); lock.close()
    thread = threading.Thread(target=release); thread.start()
    result = invoke(service, "liva_upsert_weight", {"date":"2026-07-11", "weight_kg":70.4, "dry_run":False, "reason":"short lock", "idempotency_key":"weight-lock-short"})
    thread.join()
    assert result["ok"] is True
    permanent = sqlite3.connect(lock_path, timeout=5)
    permanent.execute("BEGIN EXCLUSIVE")
    try:
        failed = invoke(service, "liva_upsert_weight", {"date":"2026-07-12", "weight_kg":70.4, "dry_run":False, "reason":"permanent lock", "idempotency_key":"weight-lock-long"})
        assert failed == {"ok":False,"status":"error","error":{"code":"write_failed","message":"Controlled write failed"}}
    finally:
        permanent.rollback(); permanent.close()
    recovered = invoke(service, "liva_upsert_weight", {"date":"2026-07-12", "weight_kg":70.4, "dry_run":False, "reason":"recovery", "idempotency_key":"weight-lock-recovered"})
    assert recovered["ok"] is True


def test_nutrition_never_fills_unknowns(service):
    context = invoke(service, "liva_post_nutrition_context", {"text":"Protein safe, kcal unbekannt", "precision_level":"context", "dry_run":False, "reason":"user report"})
    estimate = invoke(service, "liva_post_nutrition_context", {"text":"ca. 3000 kcal", "precision_level":"estimate", "calories_estimate":3000, "dry_run":False, "reason":"user report"})
    assert all(value is None for value in context["data"]["readback"]["values"].values())
    assert estimate["data"]["readback"]["values"]["calories_estimate"] == 3000


def test_flag_rawlog_and_audit_without_legacy_memory_overlay(service):
    flag = invoke(service, "liva_post_daily_flag", {"flag_type":"pain", "body_part":"shoulder", "severity":"low", "dry_run":False, "reason":"user report"})
    raw = invoke(service, "liva_post_training_rawlog", {"raw_text":"Pullups +10 9@8 / 7@9", "dry_run":False, "reason":"user log"})
    assert flag["data"]["readback"]["body_part"] == "shoulder"
    assert raw["data"]["readback"]["parsed_candidates"] == []
    assert WriteService(service.config.write_database_path()).audit_count() >= 2


def test_legacy_overlay_memory_read_is_not_a_public_tool(service):
    result = call_tool(service, "liva_memory_read", {"memo_id": "liva-mcp-memory/1"}, scopes={"liva.read"})
    assert result["error"]["code"] == "unknown_tool"


def test_operator_is_allowlisted_and_global_dry_run(service):
    plan = invoke(service, "liva_operator_plan", {"text":"Gewicht 70,4"})
    execution = invoke(service, "liva_operator_execute", {"operations":[{"operation_type":"weight.upsert","arguments":{"weight_kg":"70,4"}}], "dry_run":True})
    assert plan["data"]["executed"] is False
    assert execution["data"]["executed"] is False


def test_operator_plan_conservatively_builds_daily_notes(service):
    examples = [
        ("Notiere für heute unter personal: Ich bin müde.", "personal", "Ich bin müde.", None),
        ("Speichere als Tagesnotiz für morgen: Schule fällt aus.", "general", "Schule fällt aus.", None),
        ("Plane eine persönliche Notiz für den 26. Juli 2026, ändere aber noch nichts.", None, None, True),
        ("Erstelle testweise eine persönliche Tagesnotiz für den 26. Juli 2026: Technischer Test. Führe keine echte Änderung aus.", "personal", "Technischer Test.", None),
    ]
    planned = []
    for text, category, note_text, needs_input in examples:
        result = invoke(service, "liva_operator_plan", {"text": text})
        assert result["ok"] and result["data"]["executed"] is False
        data = result["data"]
        if needs_input:
            assert data["needs_input"] is True and data["proposed_operations"] == []
            continue
        assert len(data["proposed_operations"]) == 1
        operation = data["proposed_operations"][0]
        assert operation["operation_type"] == "daily.note.post"
        assert operation["arguments"]["category"] == category
        assert operation["arguments"]["text"] == note_text
        planned.append(operation)
    before = WriteService(service.config.write_database_path()).audit_count()
    execution = invoke(service, "liva_operator_execute", {"operations": planned[:1], "dry_run": True})
    assert execution["ok"] and execution["data"]["executed"] is False
    assert WriteService(service.config.write_database_path()).audit_count() == before


def test_operator_execute_empty_operations_is_noop(service):
    result = invoke(service, "liva_operator_execute", {"operations": [], "dry_run": True})
    assert result["ok"] and result["data"]["executed"] is False and result["data"]["succeeded"] == 0


def test_training_decision_plan_is_read_scoped_dry_run_and_never_guesses_weekday(service):
    conn = sqlite3.connect(service.config.database_path("plans.sqlite3"))
    conn.execute("UPDATE gym_plans SET plan_json=?", (json.dumps({"meta":{"mode":"rolling_sequence","rolling_week_pattern":{"Mo":"train","Di":"train","Mi":"train","Do":"train","Fr":"train","Sa":"train","So":"train"}}, "sequence":[{"id":"pull-a","kind":"gym","title":"FB Pull"},{"id":"push-a","kind":"gym","title":"Push A","items":[{"name":"Bench","sets":3}]}]}),))
    conn.commit(); conn.close()
    before = WriteService(service.config.write_database_path()).audit_count()
    decision_date = service.training.one("SELECT date_iso FROM workouts WHERE id=1")["date_iso"]
    result = invoke(service, "liva_training_decision_plan", {"date": decision_date, "subjective_status": "okay", "time_available_min": 60})
    assert result["ok"] is True
    data = result["data"]
    assert data["executed"] is False and data["dry_run"] is True
    assert data["changed"] is False and data["replayed"] is False
    assert data["production_write"] is False and data["readback_source"] == "core.training_ai_decisions"
    assert data["selected_session"]["session_key"] == "push-a"
    assert data["selected_session"]["name"] == "Push A"
    assert data["session_relation"] == "today" and data["is_today_session"] is True
    assert WriteService(service.config.write_database_path()).audit_count() == before
    rejected = invoke(service, "liva_training_decision_plan", {"date": decision_date, "dry_run": False})
    assert rejected["ok"] is False


def test_training_decision_plan_strict_schema_accepts_documented_fields_only(service):
    conn = sqlite3.connect(service.config.database_path("plans.sqlite3"))
    conn.execute("UPDATE gym_plans SET plan_json=?", (json.dumps({"meta":{"mode":"rolling_sequence","rolling_week_pattern":{"Mo":"train","Di":"train","Mi":"train","Do":"train","Fr":"train","Sa":"train","So":"train"}}, "sequence":[{"id":"pull-a","kind":"gym","title":"FB Pull"},{"id":"push-a","kind":"gym","title":"Push A"}]}),))
    conn.commit(); conn.close()
    decision_date = service.training.one("SELECT date_iso FROM workouts WHERE id=1")["date_iso"]
    valid = invoke(service, "liva_training_decision_plan", {"date": decision_date, "subjective_status": "okay", "time_available_min": 60, "dry_run": True})
    assert valid["ok"] is True
    unknown = invoke(service, "liva_training_decision_plan", {"date": decision_date, "unexpected": "rejected"})
    assert unknown["ok"] is False
    assert unknown["error"]["code"] == "invalid_arguments"


def test_training_decision_write_persists_and_reads_back_dashboard_source(service):
    conn = sqlite3.connect(service.config.database_path("plans.sqlite3"))
    conn.execute("UPDATE gym_plans SET plan_json=?", (json.dumps({"meta":{"mode":"rolling_sequence","rolling_week_pattern":{"Mo":"train","Di":"train","Mi":"train","Do":"train","Fr":"train","Sa":"train","So":"train"}}, "sequence":[{"id":"pull-a","kind":"gym","title":"FB Pull"},{"id":"push-a","kind":"gym","title":"Push A","items":[{"name":"Bench","sets":3}]}]}),))
    conn.commit(); conn.close()
    decision_date = service.training.one("SELECT date_iso FROM workouts WHERE id=1")["date_iso"]
    missing_reason = invoke(service, "liva_training_decision_write", {"date":decision_date, "dry_run":False})
    assert missing_reason["ok"] is False
    dry = invoke(service, "liva_training_decision_write", {"date":decision_date})
    assert dry["ok"] and dry["data"]["executed"] is False
    live = invoke(service, "liva_training_decision_write", {"date":decision_date, "dry_run":False, "reason":"test production decision", "idempotency_key":"decision-1"})
    assert live["ok"] and live["data"]["production_write"] is True
    with sqlite3.connect(service.config.database_path("core.sqlite3")) as conn:
        count_after_write = conn.execute("SELECT COUNT(*) FROM training_ai_decisions WHERE day_iso=?", (decision_date,)).fetchone()[0]
    assert live["data"]["dashboard_status"] == "fresh"
    replay = invoke(service, "liva_training_decision_write", {"date":decision_date, "dry_run":False, "reason":"test production decision", "idempotency_key":"decision-1"})
    assert replay["ok"] and replay["data"]["decision_id"] == live["data"]["decision_id"]
    with sqlite3.connect(service.config.database_path("core.sqlite3")) as conn:
        assert conn.execute("SELECT COUNT(*) FROM training_ai_decisions WHERE day_iso=?", (decision_date,)).fetchone()[0] == count_after_write
    assert live["data"]["changed"] is True and live["data"]["replayed"] is False
    assert replay["data"]["changed"] is False and replay["data"]["replayed"] is True
    invalid_force = invoke(service, "liva_training_decision_write", {"date":decision_date, "force_session_key":"missing"})
    assert invalid_force["ok"] is False


def test_training_decision_write_rejects_idempotency_key_replay_with_changed_context(service):
    conn = sqlite3.connect(service.config.database_path("plans.sqlite3"))
    conn.execute("UPDATE gym_plans SET plan_json=?", (json.dumps({"meta":{"mode":"rolling_sequence","rolling_week_pattern":{"Mo":"train","Di":"train","Mi":"train","Do":"train","Fr":"train","Sa":"train","So":"train"}}, "sequence":[{"id":"pull-a","kind":"gym","title":"FB Pull"},{"id":"push-a","kind":"gym","title":"Push A"}]}),))
    conn.commit(); conn.close()
    decision_date = service.training.one("SELECT date_iso FROM workouts WHERE id=1")["date_iso"]
    first = invoke(service, "liva_training_decision_write", {"date":decision_date, "dry_run":False, "reason":"test production decision", "idempotency_key":"decision-conflict"})
    assert first["ok"] is True
    with sqlite3.connect(service.config.database_path("core.sqlite3")) as conn:
        before = conn.execute("SELECT id,status,context_hash,decision_json FROM training_ai_decisions WHERE day_iso=?", (decision_date,)).fetchone()
    conflict = invoke(service, "liva_training_decision_write", {"date":decision_date, "subjective_status":"different context", "dry_run":False, "reason":"test production decision", "idempotency_key":"decision-conflict"})
    assert conflict["ok"] is False
    assert conflict["error"]["code"] == "invalid_arguments"
    assert "idempotency_key_context_conflict" in conflict["error"]["message"]
    with sqlite3.connect(service.config.database_path("core.sqlite3")) as conn:
        after = conn.execute("SELECT id,status,context_hash,decision_json FROM training_ai_decisions WHERE day_iso=?", (decision_date,)).fetchone()
    assert after == before


def test_training_decision_write_connection_uses_required_sqlite_pragmas(service):
    with _connect(service.config.database_path("core.sqlite3"), write=True) as conn:
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000


def test_overlay_initializes_before_first_write(service):
    overlay = WriteService(service.config.write_database_path())
    assert not overlay.path.exists()
    overlay.initialize()
    assert overlay.path.exists()
    assert overlay.audit_count() == 0


def test_tool_audit_logs_daily_note_metadata(service, monkeypatch, caplog):
    monkeypatch.setattr("liva_mcp.remote_server.get_access_token", lambda: SimpleNamespace(scopes=["liva.read", "liva.write"], client_id="test-client", claims={"jti":"tool-request"}))
    with caplog.at_level("INFO", logger="liva_mcp.tool_audit"):
        result = _invoke(service, "liva_post_daily_note", {"category":"system", "text":"tool audit", "dry_run":False, "reason":"test"})
    assert result["ok"] is True
    assert "tool_name=liva_post_daily_note" in caplog.text
    assert "readback_ok=yes" in caplog.text
    assert "audit_written=yes" in caplog.text


def test_write_exception_is_controlled_tool_error(service, monkeypatch):
    def fail(*_args, **_kwargs):
        raise RuntimeError("database unavailable")
    monkeypatch.setattr(WriteService, "daily_note", fail)
    result = invoke(service, "liva_post_daily_note", {"category":"system", "text":"cannot write", "dry_run":False, "reason":"test"})
    assert result == {"ok": False, "status": "error", "error": {"code": "write_failed", "message": "Controlled write failed"}}


def test_weight_scope_and_strict_validation_precede_mutation(service):
    read_only = call_tool(service, "liva_upsert_weight", {"weight_kg":70.0, "dry_run":False, "reason":"scope"}, scopes={"liva.read"})
    assert read_only["error"]["code"] == "insufficient_scope"
    invalid_payloads = [
        {},
        {"weight_kg": None},
        {"weight_kg": True},
        {"weight_kg": ""},
        {"weight_kg": -1},
        {"weight_kg": "nan"},
        {"weight_kg": "inf"},
        {"weight_kg": 1000000},
        {"weight_kg": 70, "date":"not-a-date"},
        {"weight_kg": 70, "unexpected":"field"},
    ]
    for payload in invalid_payloads:
        assert invoke(service, "liva_upsert_weight", {**payload, "dry_run":False, "reason":"validation"})["ok"] is False
    with sqlite3.connect(service.config.database_path("ernaehrung.sqlite3")) as conn:
        assert conn.execute("SELECT COUNT(*) FROM weight_logs").fetchone()[0] == 1


def test_weight_parallel_identical_requests_have_one_mutation(service):
    arguments = {"date":"2026-07-11", "weight_kg":70.4, "dry_run":False, "reason":"parallel", "idempotency_key":"weight-parallel"}
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: invoke(service, "liva_upsert_weight", arguments), range(4)))
    assert sum(result["ok"] and result["data"]["changed"] for result in results) == 1
    assert sum(result["ok"] and result["data"]["replayed"] for result in results) == 3
    with sqlite3.connect(service.config.database_path("ernaehrung.sqlite3")) as conn:
        assert conn.execute("SELECT COUNT(*) FROM weight_logs WHERE date_iso=?", ("2026-07-11",)).fetchone()[0] == 1
    fresh_service = ReadService(service.config)
    replay = invoke(fresh_service, "liva_upsert_weight", arguments)
    assert replay["data"]["changed"] is False and replay["data"]["replayed"] is True
