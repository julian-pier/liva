from __future__ import annotations

import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import database.connections as db_conn
import integrations.telegram_hub as telegram_hub
from core import core_daily_decision as daily


def _mk(path: str, sql: str = "") -> None:
    conn = sqlite3.connect(path)
    try:
        if sql:
            conn.executescript(sql)
        conn.commit()
    finally:
        conn.close()


def _prep(tmp_path: Path) -> str:
    db_conn.CORE_DB = str(tmp_path / "core.sqlite3")
    db_conn.HRV_DB = str(tmp_path / "hrv.sqlite3")
    db_conn.NUTRITION_DB = str(tmp_path / "nutrition.sqlite3")
    db_conn.TRAINING_DB = str(tmp_path / "training.sqlite3")
    db_conn.RUNS_DB = str(tmp_path / "runs.sqlite3")
    day = date.today().isoformat()
    _mk(
        db_conn.HRV_DB,
        """
        CREATE TABLE hrv_measurements (
            id INTEGER PRIMARY KEY,
            date_utc TEXT,
            ts_measurement TEXT,
            rmssd REAL,
            hr REAL,
            sleep_quality REAL,
            fatigue REAL,
            training_motivation REAL,
            sickness_bool INTEGER
        );
        """,
    )
    _mk(db_conn.NUTRITION_DB, "CREATE TABLE weight_logs (id INTEGER PRIMARY KEY, date_iso TEXT, weight_kg REAL);")
    _mk(db_conn.TRAINING_DB, "CREATE TABLE workouts (id INTEGER PRIMARY KEY, date_iso TEXT, name TEXT, notes TEXT);")
    _mk(db_conn.RUNS_DB, "CREATE TABLE runs (id INTEGER PRIMARY KEY, date TEXT, distance REAL);")
    _mk(db_conn.CORE_DB)
    daily.ensure_core_daily_decision_schema()
    return day


def _insert_hrv(day: str, minutes_ago: int = 10, *, subjective: bool = True, sick: int = 0) -> None:
    ts = (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    conn = sqlite3.connect(db_conn.HRV_DB)
    try:
        conn.execute(
            """
            INSERT INTO hrv_measurements
              (date_utc, ts_measurement, rmssd, hr, sleep_quality, fatigue, training_motivation, sickness_bool)
            VALUES (?, ?, 64, 52, ?, ?, ?, ?)
            """,
            (day, ts, 4 if subjective else None, 2 if subjective else None, 4 if subjective else None, sick),
        )
        conn.commit()
    finally:
        conn.close()


def _insert_weight(day: str, kg: float = 80.0) -> None:
    conn = sqlite3.connect(db_conn.NUTRITION_DB)
    try:
        conn.execute("INSERT INTO weight_logs (date_iso, weight_kg) VALUES (?, ?)", (day, kg))
        conn.commit()
    finally:
        conn.close()


def test_complete_morning_data_is_decision_ready(tmp_path: Path):
    day = _prep(tmp_path)
    _insert_hrv(day)
    _insert_weight(day)

    readiness = daily.morning_readiness(day)

    assert readiness["complete"] is True
    assert readiness["state"] == "decision_ready"
    assert readiness["should_prompt_missing"] is False


def test_stale_yesterday_data_does_not_start_today_timer(tmp_path: Path):
    day = _prep(tmp_path)
    yesterday = (datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat()
    _insert_hrv(yesterday, minutes_ago=24 * 60)
    _insert_weight(yesterday)

    readiness = daily.morning_readiness(day)

    assert readiness["state"] == "waiting_for_morning_data"
    assert readiness["minutes_since_first_signal"] is None
    assert readiness["should_prompt_missing"] is False
    assert readiness["should_fallback"] is False


def test_missing_weight_after_60_minutes_prompts_once(tmp_path: Path, monkeypatch):
    day = _prep(tmp_path)
    _insert_hrv(day, minutes_ago=61)
    sent: list[str] = []
    monkeypatch.setattr(telegram_hub, "send_domain_message", lambda domain, text, **kwargs: sent.append(text) or {"ok": True, "message_id": 1})

    first = daily.maybe_prompt_missing_morning_data(day)
    second = daily.maybe_prompt_missing_morning_data(day)

    assert first["sent"] is True
    assert second["sent"] is False
    assert second["reason"] == "already_prompted"
    assert len(sent) == 1


def test_fallback_after_120_minutes_sets_missing_data_used(tmp_path: Path):
    day = _prep(tmp_path)
    _insert_hrv(day, minutes_ago=121)

    decision = daily.build_daily_decision(day_iso=day, state={}, ns_payload={"final_day": {"kind": "rest", "label": "Rest"}})

    assert decision["data_readiness"]["state"] == "fallback_ready"
    assert decision["missing_data_used"] is True


def test_sickness_flag_hard_stops_constraints(tmp_path: Path):
    day = _prep(tmp_path)
    _insert_hrv(day, sick=1)
    _insert_weight(day)

    decision = daily.build_daily_decision(
        day_iso=day,
        state={"illness": {"sick": True}},
        ns_payload={"final_day": {"kind": "plan", "label": "LOWER A"}, "items": [{"title": "Squat"}]},
    )
    constraints = decision["consequences"]["training_constraints"]

    assert constraints["allow_progression"] is False
    assert constraints["allow_topset"] is False
    assert constraints["rpe_cap"] <= 6.0
    assert "sickness" in [r["code"] for r in decision["reasons"]]


def test_hamstring_fatigue_blocks_rdl(tmp_path: Path):
    day = _prep(tmp_path)
    _insert_hrv(day)
    _insert_weight(day)

    decision = daily.build_daily_decision(
        day_iso=day,
        state={"flags": {"local_fatigue": {"legs": True}}},
        ns_payload={"final_day": {"kind": "plan", "label": "LOWER A"}, "items": [{"title": "Romanian Deadlift"}]},
    )
    constraints = decision["consequences"]["training_constraints"]

    assert "Romanian Deadlift" in constraints["blocked_exercises"]
    assert "hip_hinge" in constraints["blocked_patterns"]
    assert constraints["exercise_substitutions"][0]["to"] == "Leg Curl"


def test_run_with_high_local_leg_risk_substitutes_ergo(tmp_path: Path):
    day = _prep(tmp_path)
    _insert_hrv(day)
    _insert_weight(day)

    decision = daily.build_daily_decision(
        day_iso=day,
        state={"flags": {"local_fatigue": {"legs": True}}},
        ns_payload={"final_day": {"kind": "run", "run_kind": "z2", "label": "Z2 Run"}},
    )

    assert decision["planned_unit"]["type"] == "ergo"
    assert decision["consequences"]["training_constraints"]["cardio_after"]["type"] == "ergo"


def test_core_callback_buttons_store_feedback_idempotently(tmp_path: Path, monkeypatch):
    day = _prep(tmp_path)
    daily.upsert_daily_decision(day_iso=day, decision={"day_iso": day, "status": "proposed", "consequences": {"training_constraints": {}}})
    answers: list[str] = []
    monkeypatch.setattr(telegram_hub, "answer_callback_query", lambda domain, cb_id, text=None, **kwargs: answers.append(text or "") or {"ok": True})
    monkeypatch.setitem(sys.modules, "core.core_night_cycle", SimpleNamespace(run_night_cycle=lambda **kwargs: {"ok": True}))
    compact = day.replace("-", "")
    cb = {"id": "cb1", "data": f"core:lighter:{compact}", "message": {"message_id": 9, "chat": {"id": "42"}}}

    one = telegram_hub.process_core_callback_query(cb)
    two = telegram_hub.process_core_callback_query(cb)

    conn = sqlite3.connect(db_conn.CORE_DB)
    count = conn.execute("SELECT COUNT(*) FROM core_daily_decision_feedback WHERE feedback='lighter'").fetchone()[0]
    status = conn.execute("SELECT status FROM core_daily_decisions WHERE day_iso=?", (day,)).fetchone()[0]
    conn.close()
    assert one["ok"] is True
    assert two["ok"] is True
    assert count == 1
    assert status == "lighter"
    assert len(answers) == 2


def test_core_callback_legacy_formats_keep_day_compatibility(tmp_path: Path, monkeypatch):
    day = _prep(tmp_path)
    compact = day.replace("-", "")
    daily.upsert_daily_decision(day_iso=day, decision={"day_iso": day, "status": "proposed", "consequences": {"training_constraints": {}}})
    monkeypatch.setattr(telegram_hub, "answer_callback_query", lambda *args, **kwargs: {"ok": True})
    monkeypatch.setitem(sys.modules, "core.core_night_cycle", SimpleNamespace(run_night_cycle=lambda **kwargs: {"ok": True}))

    old_day = telegram_hub.process_core_callback_query({"id": "cb-old", "data": f"c.lt.{compact}", "message": {"message_id": 1, "chat": {"id": "42"}}})
    no_day = telegram_hub.process_core_callback_query({"id": "cb-new-old", "data": "core:harder", "message": {"message_id": 2, "chat": {"id": "42"}}})

    assert old_day["day_iso"] == day
    assert no_day["day_iso"] == day


def test_plan_callback_marks_manual_attention(tmp_path: Path, monkeypatch):
    day = _prep(tmp_path)
    daily.upsert_daily_decision(day_iso=day, decision={"day_iso": day, "status": "proposed", "consequences": {"training_constraints": {}}})
    monkeypatch.setattr(telegram_hub, "answer_callback_query", lambda *args, **kwargs: {"ok": True})

    result = telegram_hub.process_core_callback_query({"id": "cb2", "data": "core:plan", "message": {"message_id": 10, "chat": {"id": "42"}}})

    conn = sqlite3.connect(db_conn.CORE_DB)
    row = conn.execute("SELECT status, needs_manual_attention FROM core_daily_decisions WHERE day_iso=?", (day,)).fetchone()
    conn.close()
    assert result["status"] == "plan_change"
    assert row[0] == "needs_manual_attention"
    assert row[1] == 1


def test_core_morning_reply_markup_contains_date():
    markup = daily.core_morning_reply_markup("2026-04-26")
    data = [btn["callback_data"] for row in markup["inline_keyboard"] for btn in row]
    assert "core:lighter:20260426" in data
    assert "core:harder:20260426" in data
    assert all(value.count(":") == 2 for value in data)


def test_daily_ui_payload_cleans_rest_labels(tmp_path: Path):
    day = _prep(tmp_path)
    decision = {
        "day_iso": day,
        "hero": "Heute ist kein Training der Hebel; Erholung und Tagesroutine sind die Entscheidung.",
        "planned_unit": {"type": "rest", "plan_name": "Wird vorbereitet", "source_kind": "rest"},
        "data_readiness": {"state": "decision_ready", "complete": True, "missing": []},
        "permissions": {"recovery_risk": 20, "execution_strictness": 60},
        "consequences": {"training_constraints": {}, "do": [], "avoid": [], "watch": []},
    }
    daily.upsert_daily_decision(day_iso=day, decision=decision)

    ui = daily.build_core_daily_ui_payload(day)

    assert ui["planned_unit"]["display_label"] == "Rest / Routine"
    assert "Wird vorbereitet" not in ui["planned_unit"]["display_label"]
    assert ui["display_status"] == "Morgenwerte vollständig"


def test_daily_ui_waiting_state_not_final_decision(tmp_path: Path):
    day = _prep(tmp_path)
    decision = {
        "day_iso": day,
        "hero": "Heute Z2 Run kontrolliert laufen.",
        "planned_unit": {"type": "run", "plan_name": "Z2 Run"},
        "data_readiness": {"state": "waiting_for_morning_data", "complete": False, "missing": ["recovery", "weight", "subjective"]},
        "consequences": {"training_constraints": {}},
    }
    daily.upsert_daily_decision(day_iso=day, decision=decision)

    ui = daily.build_core_daily_ui_payload(day)

    assert ui["hero"] == "CORE wartet noch auf Morgenwerte."
    assert ui["display_status"] == "HRV, Gewicht und Check-in eintragen"


def test_harder_feedback_does_not_override_sickness_safety(tmp_path: Path):
    day = _prep(tmp_path)
    _insert_hrv(day, sick=1)
    _insert_weight(day)
    daily.record_daily_decision_feedback(day_iso=day, feedback="harder")

    decision = daily.build_daily_decision(
        day_iso=day,
        state={"illness": {"sick": True}},
        ns_payload={"final_day": {"kind": "plan", "label": "Upper"}},
    )

    constraints = decision["consequences"]["training_constraints"]
    assert constraints["allow_progression"] is False
    assert decision["permissions"]["intensity_permission"] <= 8
