from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from pathlib import Path

from flask import Flask

import core.core_api as core_api
import database.connections as db_conn
from core.core_training_engine import ensure_core_training_schema


def _set_db_paths(tmp_path: Path) -> None:
    db_conn.CORE_DB = str(tmp_path / "core.sqlite3")
    db_conn.TRAINING_DB = str(tmp_path / "training.sqlite3")
    db_conn.NUTRITION_DB = str(tmp_path / "nutrition.sqlite3")
    db_conn.HRV_DB = str(tmp_path / "hrv.sqlite3")
    db_conn.RUNS_DB = str(tmp_path / "runs.sqlite3")


def _mk_training_db(path: str) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE workouts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT,
            date_iso TEXT,
            name TEXT,
            created_at TEXT
        )
        """
    )
    for days in (12, 6, 2):
        d = (date.today() - timedelta(days=days)).isoformat()
        conn.execute(
            "INSERT INTO workouts (date, date_iso, name, created_at) VALUES (?, ?, ?, ?)",
            (d, d, f"Session-{days}", "2026-01-01T00:00:00Z"),
        )
    conn.commit()
    conn.close()


def _mk_hrv_db(path: str) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE hrv_measurements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts_measurement TEXT,
            date_utc TEXT,
            rmssd REAL,
            hr REAL,
            sdnn REAL
        )
        """
    )
    for d, rmssd in ((13, 41.0), (10, 39.0), (6, 36.0), (2, 34.5)):
        day = (date.today() - timedelta(days=d)).isoformat()
        conn.execute(
            "INSERT INTO hrv_measurements (ts_measurement, date_utc, rmssd, hr, sdnn) VALUES (?, ?, ?, ?, ?)",
            (day, day, rmssd, 56, 82),
        )
    conn.commit()
    conn.close()


def _mk_nutrition_db(path: str) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE nutrition_daily (
            date TEXT,
            date_iso TEXT,
            kcal INTEGER,
            protein INTEGER,
            carbs INTEGER,
            fat INTEGER,
            created_at TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE weight_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT,
            date_iso TEXT,
            weight_kg REAL,
            kcal INTEGER,
            protein INTEGER,
            carbs INTEGER,
            fat INTEGER,
            sugar INTEGER,
            created_at TEXT,
            raw_json TEXT
        )
        """
    )
    for i, kcal in enumerate((3180, 3320, 3410, 3370, 3450, 3520), start=1):
        d = (date.today() - timedelta(days=7 - i)).isoformat()
        conn.execute(
            "INSERT INTO nutrition_daily (date, date_iso, kcal, protein, carbs, fat, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (d, d, kcal, 180, 320, 86, "2026-01-01T00:00:00Z"),
        )
    for offset, w in ((13, 80.3), (10, 80.7), (7, 80.9), (3, 81.0), (1, 81.1)):
        d = (date.today() - timedelta(days=offset)).isoformat()
        conn.execute(
            "INSERT INTO weight_logs (date, date_iso, weight_kg, kcal, protein, carbs, fat, sugar, created_at, raw_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (d, d, w, 3350, 180, 320, 86, 40, "2026-01-01T00:00:00Z", "{}"),
        )
    conn.commit()
    conn.close()


def _mk_runs_db(path: str) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT,
            distance REAL,
            moving_time INTEGER,
            avg_speed REAL,
            avg_hr REAL,
            max_hr REAL,
            elevation_gain REAL,
            pace REAL
        )
        """
    )
    for d, dist in ((5, 5200.0), (3, 6100.0)):
        day = (date.today() - timedelta(days=d)).isoformat() + " 08:00:00"
        conn.execute(
            "INSERT INTO runs (date, distance, moving_time, avg_speed, avg_hr, max_hr, elevation_gain, pace) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (day, dist, 1650, 3.1, 145, 168, 35, 307),
        )
    conn.commit()
    conn.close()


def _build_app(monkeypatch, tmp_path: Path) -> Flask:
    _set_db_paths(tmp_path)
    _mk_training_db(db_conn.TRAINING_DB)
    _mk_hrv_db(db_conn.HRV_DB)
    _mk_nutrition_db(db_conn.NUTRITION_DB)
    _mk_runs_db(db_conn.RUNS_DB)

    ensure_core_training_schema()

    app = Flask(__name__)
    app.secret_key = "test-secret"
    app.register_blueprint(core_api.core_bp)

    monkeypatch.setattr(core_api, "is_core_access_allowed", lambda req, role=None: True)
    core_api._RL_BUCKETS.clear()
    core_api._CACHE.clear()
    return app


def test_profile_init_creates_singleton(monkeypatch, tmp_path):
    app = _build_app(monkeypatch, tmp_path)
    client = app.test_client()

    res = client.get("/api/core/profile")
    data = res.get_json()

    assert res.status_code == 200
    assert data["ok"] is True
    assert data["profile"]["id"] == 1
    assert data["profile"]["tone_style"] in {"calm_direct", "ultra_short", "analytic", "hard_guard"}


def test_session_start_end(monkeypatch, tmp_path):
    app = _build_app(monkeypatch, tmp_path)
    client = app.test_client()

    r1 = client.post("/api/core/training/session/start", json={"mode": "mission"})
    d1 = r1.get_json()
    assert r1.status_code == 200
    assert d1["ok"] is True
    assert isinstance(d1["session_id"], int)

    r2 = client.post("/api/core/training/session/end", json={"session_id": d1["session_id"]})
    d2 = r2.get_json()
    assert r2.status_code == 200
    assert d2["ok"] is True
    assert d2["session"]["ended_at"]


def test_next_step_schema(monkeypatch, tmp_path):
    app = _build_app(monkeypatch, tmp_path)
    client = app.test_client()

    for mode in ("mission", "priority", "replay"):
        sid = client.post("/api/core/training/session/start", json={"mode": mode}).get_json()["session_id"]
        out = client.get(f"/api/core/training/next_card?session_id={sid}").get_json()

        assert out["ok"] is True
        card = out["card"]
        payload = card["payload"]
        assert card["mode"] == mode
        assert card["card_type"] == "day_simulation"
        assert isinstance(payload.get("timeline_rows"), list)
        assert isinstance(payload.get("situation_lines"), list)
        assert isinstance(payload.get("proposal_lines"), list)
        assert isinstance(payload.get("controls"), dict)
        assert isinstance(payload.get("hrv"), dict)
        assert isinstance(payload.get("exercise_cards"), list)
        assert isinstance(payload.get("allowed_labels"), list)
        assert {"accept", "tighten", "ease", "reroute"}.issubset(set(payload.get("allowed_labels") or []))
        assert isinstance(payload.get("title"), str)


def test_feedback_updates_stats_and_learning(monkeypatch, tmp_path):
    app = _build_app(monkeypatch, tmp_path)
    client = app.test_client()

    sid = client.post("/api/core/training/session/start", json={"mode": "priority"}).get_json()["session_id"]

    deltas = []
    for idx, action in enumerate(("tighten", "tighten", "reroute", "ease", "accept"), start=1):
        card = client.get(f"/api/core/training/next_card?session_id={sid}").get_json()["card"]
        resp = client.post(
            "/api/core/training/label",
            json={
                "session_id": sid,
                "card_id": card["id"],
                "label": action,
                "confidence_user": 3,
                "free_text": "bitte kurz und direkt" if idx % 2 == 0 else "",
            },
        )
        assert resp.status_code == 200
        payload = resp.get_json()
        assert payload["ok"] is True
        assert "session_stats" in payload
        deltas.extend(payload.get("profile_delta") or [])

    stats = client.get("/api/core/training/stats").get_json()["stats"]
    assert stats["total_sessions"] >= 1
    assert stats["total_labels"] >= 5
    assert 0 <= stats["agreement_rate"] <= 100
    assert 0 <= stats["confidence_alignment"] <= 100
    assert isinstance(stats["top_preferences"], list)
    assert any(abs(float(item.get("value") or 0.0)) > 0.01 for item in stats["top_preferences"])


def test_long_session_generates_diverse_signatures(monkeypatch, tmp_path):
    app = _build_app(monkeypatch, tmp_path)
    client = app.test_client()

    sid = client.post("/api/core/training/session/start", json={"mode": "mission"}).get_json()["session_id"]

    signatures = []
    titles = []
    for _ in range(14):
        card = client.get(f"/api/core/training/next_card?session_id={sid}").get_json()["card"]
        payload = card["payload"]
        signatures.append(payload.get("signature"))
        titles.append(payload.get("title"))
        client.post(
            "/api/core/training/label",
            json={"session_id": sid, "card_id": card["id"], "label": "accept", "confidence_user": 3},
        )

    assert len(set(signatures)) >= 10
    assert len(set(titles)) >= 10
