from __future__ import annotations

import sqlite3
from pathlib import Path

from flask import Flask

import database.connections as db_conn
from core import core_training_blueprint
from core.core_training_deck import answer_card, delete_card, ensure_core_training_deck_schema, next_card


def _mk_db(path: str, sql: str) -> None:
    conn = sqlite3.connect(path)
    if sql:
        conn.executescript(sql)
    conn.commit()
    conn.close()


def _prep(tmp_path: Path) -> Flask:
    db_conn.CORE_DB = str(tmp_path / "core.sqlite3")
    db_conn.TRAINING_DB = str(tmp_path / "training.sqlite3")
    db_conn.HRV_DB = str(tmp_path / "hrv.sqlite3")
    db_conn.RUNS_DB = str(tmp_path / "runs.sqlite3")
    db_conn.NUTRITION_DB = str(tmp_path / "nutrition.sqlite3")
    db_conn.PLANS_DB = str(tmp_path / "plans.sqlite3")

    _mk_db(db_conn.TRAINING_DB, "CREATE TABLE workouts (id INTEGER PRIMARY KEY, date_iso TEXT, name TEXT); INSERT INTO workouts (date_iso, name) VALUES ('2026-02-20', 'Push'), ('2026-02-24', 'Lower');")
    _mk_db(db_conn.HRV_DB, "CREATE TABLE hrv_measurements (id INTEGER PRIMARY KEY, ts_measurement TEXT, date_utc TEXT, rmssd REAL, hr REAL, signal_quality TEXT); INSERT INTO hrv_measurements (ts_measurement, date_utc, rmssd, hr, signal_quality) VALUES ('2026-02-26', '2026-02-26', 64, 52, 'good'), ('2026-02-25', '2026-02-25', 62, 53, 'good'), ('2026-02-24', '2026-02-24', 59, 54, 'good'), ('2026-02-23', '2026-02-23', 57, 55, 'good'), ('2026-02-22', '2026-02-22', 58, 55, 'good'), ('2026-02-21', '2026-02-21', 61, 53, 'good');")
    _mk_db(db_conn.RUNS_DB, "CREATE TABLE runs (id INTEGER PRIMARY KEY, date TEXT, distance REAL); INSERT INTO runs (date, distance) VALUES ('2026-02-20', 5000), ('2026-02-23', 8000);")
    _mk_db(db_conn.NUTRITION_DB, "CREATE TABLE nutrition_day_actuals (id INTEGER PRIMARY KEY, day TEXT, kcal REAL); INSERT INTO nutrition_day_actuals (day, kcal) VALUES ('2026-02-25', 3200), ('2026-02-24', 3150), ('2026-02-23', 3180); CREATE TABLE weight_logs (id INTEGER PRIMARY KEY, date_iso TEXT, weight_kg REAL); INSERT INTO weight_logs (date_iso, weight_kg) VALUES ('2026-02-13', 80.1), ('2026-02-26', 80.6);")
    _mk_db(db_conn.PLANS_DB, "CREATE TABLE gym_plans (id INTEGER PRIMARY KEY, title TEXT, focus TEXT, plan_json TEXT, is_active INTEGER, updated_at TEXT); INSERT INTO gym_plans (title, focus, plan_json, is_active, updated_at) VALUES ('PPL', 'Hybrid', '{\"base_week\": {\"Mo\": [{\"title\": \"Push\"}], \"Di\": [{\"title\": \"Lower\"}]}}', 1, '2026-02-26');")
    ensure_core_training_deck_schema()

    app = Flask(__name__, template_folder="../templates", static_folder="../static")
    app.secret_key = "test"
    app.register_blueprint(core_training_blueprint.core_training_bp)
    core_training_blueprint.is_core_access_allowed = lambda req, role=None: True
    return app


def test_next_endpoint_returns_valid_schema(tmp_path: Path):
    app = _prep(tmp_path)
    client = app.test_client()

    res = client.get('/api/core_training/next')
    assert res.status_code == 200
    data = res.get_json()
    assert set(data.keys()) == {'card', 'prediction', 'stats'}
    assert {'id', 'signature', 'text', 'meta'} <= set(data['card'].keys())
    assert {'answer', 'confidence', 'bar_position'} <= set(data['prediction'].keys())
    assert {'answered_count', 'summary'} <= set(data['stats'].keys())


def test_answer_and_reset_endpoints_work(tmp_path: Path):
    app = _prep(tmp_path)
    client = app.test_client()

    nxt = client.get('/api/core_training/next').get_json()
    answered = client.post(
        '/api/core_training/answer',
        json={
            'card_id': nxt['card']['id'],
            'signature': nxt['card']['signature'],
            'answer': 'yes',
            'client_ms': 180,
        },
        headers={'X-CSRF-Token': 'test'},
    )
    assert answered.status_code == 200
    payload = answered.get_json()
    assert payload['ok'] is True
    assert 'next' in payload
    assert 'stats' in payload

    reset = client.post('/api/core_training/reset', headers={'X-CSRF-Token': 'test'})
    assert reset.status_code == 200
    assert reset.get_json()['ok'] is True


def test_undo_restores_last_card(tmp_path: Path):
    app = _prep(tmp_path)
    client = app.test_client()

    nxt = client.get('/api/core_training/next').get_json()
    answered = client.post(
        '/api/core_training/answer',
        json={
            'card_id': nxt['card']['id'],
            'signature': nxt['card']['signature'],
            'answer': 'yes',
            'client_ms': 120,
        },
        headers={'X-CSRF-Token': 'test'},
    )
    assert answered.status_code == 200

    undone = client.post('/api/core_training/undo', headers={'X-CSRF-Token': 'test'})
    assert undone.status_code == 200
    payload = undone.get_json()
    assert payload['ok'] is True
    assert payload['card']['signature'] == nxt['card']['signature']


def test_answer_uses_original_card_when_state_already_advanced(tmp_path: Path):
    app = _prep(tmp_path)
    with app.app_context():
        first = next_card()
        second = next_card()
        assert first["card"]["signature"] != second["card"]["signature"]

        payload = answer_card(
            first["card"]["id"],
            first["card"]["signature"],
            "yes",
            client_ms=120,
            recent_signatures=[first["card"]["signature"]],
        )
        assert payload["ok"] is True

    conn = sqlite3.connect(db_conn.CORE_DB)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT topic, signature FROM core_training_answers ORDER BY id DESC LIMIT 1").fetchone()
    conn.close()
    assert row["signature"] == first["card"]["signature"]
    assert row["topic"] == first["card"]["meta"]["topic"]


def test_delete_card_removes_prompt_and_history(tmp_path: Path):
    app = _prep(tmp_path)
    with app.app_context():
        first = next_card()
        answer_card(first["card"]["id"], first["card"]["signature"], "yes", client_ms=120)
        deleted = delete_card(first["card"]["signature"])
        assert deleted["ok"] is True

    conn = sqlite3.connect(db_conn.CORE_DB)
    conn.row_factory = sqlite3.Row
    seen = conn.execute("SELECT COUNT(*) AS c FROM core_training_seen WHERE card_text_norm=?", (first["card"]["text"].strip().lower(),)).fetchone()["c"]
    answers = conn.execute("SELECT COUNT(*) AS c FROM core_training_answers WHERE card_text_norm=?", (first["card"]["text"].strip().lower(),)).fetchone()["c"]
    tombstone = conn.execute("SELECT COUNT(*) AS c FROM core_training_deleted_cards WHERE card_text_norm=?", (first["card"]["text"].strip().lower(),)).fetchone()["c"]
    conn.close()
    assert seen == 0
    assert answers == 0
    assert tombstone == 1
