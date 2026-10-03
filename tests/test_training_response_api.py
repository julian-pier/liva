from __future__ import annotations

import json

import app as appmod
from tests.test_training_response import add_session, bench, make_db


def test_training_response_endpoint_returns_all_scopes(monkeypatch):
    conn = make_db()
    add_session(conn, 1, "2026-01-01", [bench()])
    monkeypatch.setattr(appmod, "get_training_db", lambda: conn)

    response = appmod.app.test_client().get("/api/training/response?end=2026-01-03")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["version"] == "training_response_v1"
    assert set(payload["scopes"]) == {"overall", "chest", "back", "legs", "shoulders", "biceps", "triceps"}
    assert payload["scopes"]["overall"]["load"] == [1.0, 0.0, 0.0]
    assert payload["scopes"]["overall"]["performance_trend"] == [None, None, None]
    json.dumps(payload, allow_nan=False)


def test_training_response_endpoint_rejects_invalid_end_date():
    response = appmod.app.test_client().get("/api/training/response?end=not-a-date")
    assert response.status_code == 400
    assert response.get_json()["error"] == "invalid_end_date"


def test_training_response_endpoint_applies_requested_day_window(monkeypatch):
    conn = make_db()
    add_session(conn, 1, "2026-01-01", [bench()])
    add_session(conn, 2, "2026-01-10", [bench()])
    monkeypatch.setattr(appmod, "get_training_db", lambda: conn)

    response = appmod.app.test_client().get("/api/training/response?end=2026-01-10&days=3")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["dates"] == ["2026-01-08", "2026-01-09", "2026-01-10"]
    assert payload["scopes"]["overall"]["load"] == [0.0, 0.0, 1.0]
    assert payload["meta"]["range_days"] == 3
    assert payload["meta"]["range_start"] == "2026-01-08"


def test_training_response_endpoint_applies_exact_date_window(monkeypatch):
    conn = make_db()
    add_session(conn, 1, "2026-01-01", [bench()])
    add_session(conn, 2, "2026-01-10", [bench()])
    monkeypatch.setattr(appmod, "get_training_db", lambda: conn)

    response = appmod.app.test_client().get(
        "/api/training/response?start=2026-01-05&end=2026-01-10"
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["dates"] == [
        "2026-01-05", "2026-01-06", "2026-01-07",
        "2026-01-08", "2026-01-09", "2026-01-10",
    ]
    assert payload["scopes"]["overall"]["load"] == [0.0, 0.0, 0.0, 0.0, 0.0, 1.0]
    assert payload["meta"]["range_start"] == "2026-01-05"
    assert payload["meta"]["range_end"] == "2026-01-10"
