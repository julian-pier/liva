import sqlite3
from pathlib import Path

from flask import request
from flask import Flask, Response, g

from core.core_service import (
    build_device_fingerprint,
    capture_http_event,
    ensure_core_schema,
    get_core_intel_payload,
    record_heartbeat,
    should_emit_intel,
)
import core.core_service as core_service
import database.connections as db_conn


def _set_core_db(tmp_path: Path):
    db_conn.CORE_DB = str(tmp_path / "core_test.sqlite3")


def test_fingerprint_stability():
    a = build_device_fingerprint(
        ip_hash="ipx",
        user_agent="Mozilla/5.0",
        accept_language="de-DE",
        timezone_hint="Europe/Berlin",
    )
    b = build_device_fingerprint(
        ip_hash="ipx",
        user_agent="Mozilla/5.0",
        accept_language="de-DE",
        timezone_hint="Europe/Berlin",
    )
    c = build_device_fingerprint(
        ip_hash="ipy",
        user_agent="Mozilla/5.0",
        accept_language="de-DE",
        timezone_hint="Europe/Berlin",
    )
    assert a == b
    assert a != c


def test_no_repeat_gate_same_signal_not_emitted():
    decision = should_emit_intel(
        last_signal_value=1.2,
        last_direction="up",
        signal_value=1.21,
        days_since_last_shown=1,
        cooldown_days=2,
    )
    assert decision.emit is False


def test_no_repeat_gate_threshold_and_direction_flip_emit():
    threshold = should_emit_intel(
        last_signal_value=0.4,
        last_direction="up",
        signal_value=1.3,
        days_since_last_shown=1,
        cooldown_days=2,
    )
    flip = should_emit_intel(
        last_signal_value=1.0,
        last_direction="up",
        signal_value=0.2,
        days_since_last_shown=1,
        cooldown_days=2,
    )
    assert threshold.emit is True
    assert flip.emit is True


def test_capture_http_event_populates_device_session_event(tmp_path, monkeypatch):
    _set_core_db(tmp_path)
    ensure_core_schema()
    monkeypatch.setenv("CORE_LOG_EVENTS", "1")

    app = Flask(__name__)

    with app.test_request_context("/api/core/ping", headers={"User-Agent": "pytest-agent", "Accept-Language": "de"}):
        g.client_ip = "127.0.0.1"
        resp = capture_http_event(
            req=request,
            resp=Response("ok", status=200),
            latency_ms=23,
        )
        cookie_header = resp.headers.get("Set-Cookie") or ""

    sid = ""
    for part in cookie_header.split(";"):
        if part.strip().startswith("th_core_sid="):
            sid = part.split("=", 1)[1].strip()
            break
    assert sid

    with app.test_request_context(
        "/api/core/ping",
        headers={"User-Agent": "pytest-agent", "Accept-Language": "de", "Cookie": f"th_core_sid={sid}"},
    ):
        g.client_ip = "127.0.0.1"
        capture_http_event(
            req=request,
            resp=Response("ok", status=200),
            latency_ms=31,
        )
    core_service._flush_core_event_buffer_once()

    conn = sqlite3.connect(db_conn.CORE_DB)
    cur = conn.cursor()
    assert cur.execute("SELECT COUNT(*) FROM core_event").fetchone()[0] >= 1
    conn.close()


def test_heartbeat_marks_active_and_inactive(tmp_path):
    _set_core_db(tmp_path)
    ensure_core_schema()

    record_heartbeat("abc123", "/core")

    conn = sqlite3.connect(db_conn.CORE_DB)
    cur = conn.cursor()
    cur.execute(
        "INSERT OR REPLACE INTO core_session (id, device_id, start_ts, last_seen_ts, is_active, current_route, req_count) VALUES (?, NULL, 1, 1, 1, '/', 0)",
        ("stale",),
    )
    conn.commit()
    conn.close()

    record_heartbeat("abc123", "/core/live")

    conn = sqlite3.connect(db_conn.CORE_DB)
    cur = conn.cursor()
    active = cur.execute("SELECT is_active FROM core_session WHERE id='abc123'").fetchone()[0]
    stale = cur.execute("SELECT is_active FROM core_session WHERE id='stale'").fetchone()[0]
    conn.close()

    assert active == 1
    assert stale == 0


def test_resolved_emitted_when_signal_back_to_baseline(tmp_path, monkeypatch):
    _set_core_db(tmp_path)
    ensure_core_schema()

    conn = sqlite3.connect(db_conn.CORE_DB)
    conn.execute(
        """
        INSERT INTO core_intel_state
        (fingerprint_hash, last_shown_day, last_signal_value, last_direction, cooldown_days)
        VALUES (?, ?, ?, ?, 2)
        """,
        ("fp_test", "2026-02-18", 1.2, "down"),
    )
    conn.commit()
    conn.close()

    def fake_athlete():
        return [
            core_service._candidate_base(
                category="ATH_PERF_DELTA",
                entity_type="exercise",
                entity_id="Bench",
                metric="topset_score",
                window="last3_vs_4w",
                title="Bench Delta -2%",
                body="near baseline",
                severity=0.2,
                confidence=0.8,
                signal_value=0.1,
                baseline_value=100.0,
                direction="flat",
                evidence={"sample_size": 8},
            )
            | {"fingerprint_hash": "fp_test"}
        ]

    monkeypatch.setattr(core_service, "_athlete_candidates", fake_athlete)
    monkeypatch.setattr(core_service, "_system_candidates", lambda: [])

    payload = get_core_intel_payload(mode="today", day_iso="2026-02-19", force=True)
    assert payload["resolved"]
    assert payload["resolved"][0]["category"].startswith("RESOLVED_")
