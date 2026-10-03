from __future__ import annotations

import importlib
import json
import os
import sqlite3
from datetime import date as py_date
from datetime import datetime, time, timedelta, timezone

import pytest

from tests.helpers.block_status_scenarios import SCENARIOS, Scenario, render_preview


class _FrozenDate(py_date):
    @classmethod
    def today(cls):
        return cls(2026, 2, 14)


def _seed_training_db(scn: Scenario, today: py_date) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE override_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts INTEGER,
            decision_type TEXT,
            user_override_bool INTEGER
        );
        CREATE TABLE workouts (
            id INTEGER PRIMARY KEY,
            date_iso TEXT
        );
        CREATE TABLE plan_checks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            plan_id INTEGER,
            workout_id INTEGER,
            payload TEXT,
            flags_json TEXT,
            status TEXT
        );
        """
    )

    for ov in scn.override_events:
        ts = int(datetime.combine(today - timedelta(days=ov.days_ago), time.min, tzinfo=timezone.utc).timestamp())
        conn.execute(
            "INSERT INTO override_log (ts, decision_type, user_override_bool) VALUES (?, ?, 1)",
            (ts, ov.decision_type),
        )

    for breach in scn.rpe_cap_breaches:
        workout_id = int(breach.session_id)
        d = (today - timedelta(days=breach.days_ago)).isoformat()
        conn.execute("INSERT OR REPLACE INTO workouts (id, date_iso) VALUES (?, ?)", (workout_id, d))
        cap = float(scn.plan.rpe_cap)
        max_rpe = round(cap + float(breach.breach_amount), 2)
        payload = {
            "adjustments": {"rpe_cap": cap},
            "exercises": [
                {
                    "planned": {"rpe": "8-9"},
                    "done": {"max_rpe": max_rpe},
                }
            ],
        }
        conn.execute(
            """
            INSERT INTO plan_checks (plan_id, workout_id, payload, flags_json, status)
            VALUES (?, ?, ?, ?, ?)
            """,
            (1, workout_id, json.dumps(payload, ensure_ascii=False), json.dumps(["RPE zu hoch"], ensure_ascii=False), "yellow"),
        )

    conn.commit()
    return conn


def _seed_hrv_db(scn: Scenario) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE hrv_measurements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date_utc TEXT,
            ts_measurement TEXT,
            rmssd REAL,
            hr REAL
        )
        """
    )
    if scn.hrv.missing:
        conn.commit()
        return conn

    anchor = py_date.today()
    base_rmssd = 50.0
    base_hr = 55.0

    for i in range(21):
        d = (anchor - timedelta(days=20 - i)).isoformat()
        rmssd = base_rmssd
        hr = base_hr
        if i >= 18:
            if scn.hrv.trend == "down":
                rmssd = 40.0
            elif scn.hrv.trend == "up":
                rmssd = 60.0
            elif scn.hrv.trend == "volatile":
                rmssd = 44.0 if (i % 2 == 0) else 56.0
            else:
                rmssd = 50.0
            if scn.hrv.rhr_high:
                hr = 63.0
        conn.execute(
            "INSERT INTO hrv_measurements (date_utc, ts_measurement, rmssd, hr) VALUES (?, ?, ?, ?)",
            (d, f"{d}T07:00:00", rmssd, hr),
        )
    conn.commit()
    return conn


def _install_scenario(monkeypatch: pytest.MonkeyPatch, appmod, scn: Scenario, today: py_date):
    training_conn = _seed_training_db(scn, today)
    hrv_conn = _seed_hrv_db(scn)

    monkeypatch.setattr(appmod, "date", _FrozenDate)
    monkeypatch.setattr(appmod, "get_training_db", lambda: training_conn)
    monkeypatch.setattr(appmod, "get_hrv_db", lambda: hrv_conn)
    monkeypatch.setattr(appmod, "_load_hrv_flags", lambda days=5: {"sickness": bool(scn.hrv.sick), "alcohol": False})

    if scn.plan.has_plan:
        week_key = f"W{scn.plan.block_week}" if scn.plan.block_week is not None else "W1"
        monkeypatch.setattr(
            appmod,
            "_get_active_gym_plan_record",
            lambda: {
                "id": 123,
                "title": scn.plan.plan_name,
                "plan_json": {
                    "weeks": scn.plan.block_weeks_total or 8,
                    "meta": {"periodization_enabled": True},
                },
                "rules_json": {"weeks": {week_key: {"rpe_cap": scn.plan.rpe_cap}}},
            },
        )
        monkeypatch.setattr(appmod, "_get_active_plan_row", lambda: {"id": 1})
        monkeypatch.setattr(
            appmod,
            "_gym_plan_cycle_status",
            lambda plan_json, rules_json, **kwargs: {
                "week_current": scn.plan.block_week,
                "week_total": scn.plan.block_weeks_total,
                "week_label": week_key,
                "phase_label": scn.plan.phase,
            },
        )
        monkeypatch.setattr(
            appmod,
            "_block_status_last_week_before_deload",
            lambda cycle, plan_json, rules_json, today=None: (
                bool(
                    (scn.plan.days_to_deload is not None and scn.plan.days_to_deload <= 3)
                    or (
                        scn.plan.block_week is not None
                        and scn.plan.block_weeks_total is not None
                        and scn.plan.block_week == scn.plan.block_weeks_total - 1
                    )
                ),
                scn.plan.days_to_deload,
            ),
        )
    else:
        monkeypatch.setattr(appmod, "_get_active_gym_plan_record", lambda: None)

    return training_conn, hrv_conn


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setenv("LIVA_FLASK_SECRET", "test-secret")
    if "app" in os.sys.modules:
        appmod = importlib.reload(os.sys.modules["app"])
    else:
        appmod = importlib.import_module("app")
    monkeypatch.setattr(appmod, "enforce_private_access", lambda: None)
    monkeypatch.setattr(appmod, "enforce_key_read_only", lambda: None)
    monkeypatch.setattr(appmod, "enforce_write_protection", lambda: None)
    monkeypatch.setattr(appmod, "enforce_ip_whitelist", lambda: None)
    appmod.app.config["TESTING"] = True
    with appmod.app.test_client() as c:
        yield c, appmod


@pytest.mark.parametrize("scenario", SCENARIOS, ids=[s.name for s in SCENARIOS])
def test_block_status_scenarios(client, monkeypatch, scenario: Scenario):
    c, appmod = client
    today = _FrozenDate.today()
    training_conn, hrv_conn = _install_scenario(monkeypatch, appmod, scenario, today)
    try:
        resp = c.get("/api/dashboard/block_status")
        assert resp.status_code == 200
        data = resp.get_json()

        assert data["ok"] is True
        assert "plan_name" in data
        assert "block_week" in data or data["has_plan"] is False
        assert "block_weeks_total" in data or data["has_plan"] is False
        assert "phase" in data
        assert "hrv_expected" in data
        assert "hrv_observed" in data
        assert "hrv_verdict" in data
        assert "chip_tripwire" not in data

        if scenario.plan.has_plan:
            assert data["badge"] == scenario.expected.badge
            if scenario.expected.badge_reason_contains:
                assert scenario.expected.badge_reason_contains in str(data.get("badge_reason") or "")

            assert data["hrv_verdict"] == scenario.expected.verdict
            observed = str(data["hrv_observed"] or "")
            if scenario.hrv.sick or scenario.hrv.rhr_high:
                assert "↓" in observed
                assert "(krank/RHR↑)" in observed

            if scenario.hrv.missing:
                assert observed == "keine Daten"
                assert data["hrv_verdict"] == "neutral"
                assert data["badge"] == "CONSISTENT"

            if scenario.plan.days_to_deload is not None and scenario.plan.days_to_deload <= 3:
                assert "↓ oder stabil" in str(data["hrv_expected"])
                if "↓" in observed:
                    assert data["hrv_verdict"] == "passt"
            if scenario.plan.phase == "DELOAD" and scenario.hrv.trend == "down":
                assert str(data["hrv_expected"]) == "↗/stabil"
                assert "↓" in observed
                assert data["hrv_verdict"] == "kritisch"
            if scenario.name == "e2_plan_partial_missing_phase_week":
                assert data.get("partial") is True
                assert data["badge"] in {"CONSISTENT", "NO PLAN"}
                assert data["hrv_verdict"] == "neutral"
            if scenario.name == "e3_override_missing_fields_ignored":
                assert int(data["events"]["override_count_7d"]) == 0
                assert data["badge"] == "CONSISTENT"
            if scenario.name == "e4_rpe_breach_exactly_half_boundary":
                assert data["badge"] == "DRIFT"
                assert "wiederholt" in str(data.get("badge_reason") or "")

            preview = render_preview(data)
            assert preview["hrv_line"].startswith("HRV: Erwartet ")
            assert preview["hrv_line"].endswith(f"· {scenario.expected.verdict}")

            chips = preview["chips"]
            assert len(chips) == scenario.expected.chips_count
            assert all("Kippt" not in chip and "Nächster Check" not in chip for chip in chips)

            if scenario.expected.should_not_count_reorder_event:
                assert int(data["events"]["override_count_7d"]) == 0
                assert data["badge"] == "CONSISTENT"
        else:
            assert data["badge"] == "NO PLAN"
    finally:
        training_conn.close()
        hrv_conn.close()


def test_block_status_no_plan_minimal(client, monkeypatch):
    c, appmod = client
    monkeypatch.setattr(appmod, "date", _FrozenDate)
    monkeypatch.setattr(appmod, "_get_active_gym_plan_record", lambda: None)
    resp = c.get("/api/dashboard/block_status")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert data["has_plan"] is False
    assert data["badge"] == "NO PLAN"
    assert "chip_tripwire" not in data
    preview = render_preview(data)
    assert preview["badge_text"] == "NO PLAN"


def test_block_status_example_snapshot(client, monkeypatch):
    c, appmod = client
    s1 = next(s for s in SCENARIOS if s.name == "s1_last_week_build_sick_down_passt")
    training_conn, hrv_conn = _install_scenario(monkeypatch, appmod, s1, _FrozenDate.today())
    try:
        data = c.get("/api/dashboard/block_status").get_json()
        snapshot_subset = {
            "plan_name": data["plan_name"],
            "phase": data["phase"],
            "hrv_expected": data["hrv_expected"],
            "hrv_observed": data["hrv_observed"],
            "hrv_verdict": data["hrv_verdict"],
            "badge": data["badge"],
            "badge_reason": data["badge_reason"],
        }
        snapshot = json.dumps(snapshot_subset, ensure_ascii=False, sort_keys=True)
        assert '"hrv_verdict": "passt"' in snapshot
        assert '"badge": "CONSISTENT"' in snapshot
    finally:
        training_conn.close()
        hrv_conn.close()


def test_block_status_status_line_pluralization(client, monkeypatch):
    c, appmod = client

    def _setup(days: int):
        scn = next(s for s in SCENARIOS if s.name == "s3_build_early_volatile_neutral")
        training_conn, hrv_conn = _install_scenario(monkeypatch, appmod, scn, _FrozenDate.today())
        monkeypatch.setattr(
            appmod,
            "_block_status_last_week_before_deload",
            lambda cycle, plan_json, rules_json, today=None: (days <= 3, days),
        )
        return training_conn, hrv_conn

    tr1, h1 = _setup(1)
    try:
        data1 = c.get("/api/dashboard/block_status").get_json()
        assert "Deload in 1 Tag" in str(data1.get("status_line") or "")
        prev1 = render_preview(data1)
        assert "in 1 Tag" in prev1["sub"]
    finally:
        tr1.close()
        h1.close()

    tr2, h2 = _setup(2)
    try:
        data2 = c.get("/api/dashboard/block_status").get_json()
        assert "Deload in 2 Tagen" in str(data2.get("status_line") or "")
        prev2 = render_preview(data2)
        assert "in 2 Tagen" in prev2["sub"]
    finally:
        tr2.close()
        h2.close()
