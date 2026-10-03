from flask import Flask

import core.core_api as core_api


def _build_app(monkeypatch):
    app = Flask(__name__)
    app.secret_key = "test-secret"
    app.register_blueprint(core_api.core_bp)
    monkeypatch.setattr(core_api, "is_core_access_allowed", lambda req, role=None: True)
    core_api._RL_BUCKETS.clear()
    core_api._CACHE.clear()
    return app


def test_v2_explain_route_uses_force_flag(monkeypatch):
    app = _build_app(monkeypatch)

    captured = {"day": None, "force": None}

    def fake_explain(day_iso=None, force_rebuild=False):
        captured["day"] = day_iso
        captured["force"] = force_rebuild
        return {"ok": True, "date": day_iso or "2026-03-15", "decision": {"mode": "HEAVY"}}

    monkeypatch.setattr(core_api, "get_explain_v2_payload", fake_explain)

    client = app.test_client()
    resp = client.get("/api/core/v2/explain?day=2026-03-14&force=1")

    assert resp.status_code == 200
    assert captured["day"] == "2026-03-14"
    assert captured["force"] is True
    assert resp.get_json()["decision"]["mode"] == "HEAVY"


def test_v2_decision_route_returns_payload(monkeypatch):
    app = _build_app(monkeypatch)

    monkeypatch.setattr(
        core_api,
        "get_decision_v2_payload",
        lambda day_iso=None: {
            "ok": True,
            "date": day_iso or "2026-03-15",
            "decision": {
                "mode": "NORMAL",
                "started_from": "HEAVY",
                "downgrade_path": ["HEAVY", "NORMAL"],
            },
        },
    )

    client = app.test_client()
    resp = client.get("/api/core/v2/decision?day=2026-03-15")
    payload = resp.get_json()

    assert resp.status_code == 200
    assert payload["ok"] is True
    assert payload["decision"]["mode"] == "NORMAL"
    assert payload["decision"]["started_from"] == "HEAVY"
