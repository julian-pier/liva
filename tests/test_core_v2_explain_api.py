from flask import Flask

import core.core_api as core_api


def _build_app(monkeypatch):
    app = Flask(__name__)
    app.secret_key = "test-secret"
    app.register_blueprint(core_api.core_bp)
    monkeypatch.setattr(core_api, "is_core_access_allowed", lambda req, role=None: True)
    return app


def test_core_v2_explain_endpoint(monkeypatch):
    app = _build_app(monkeypatch)
    monkeypatch.setattr(
        core_api,
        "get_explain_v2_payload",
        lambda day_iso=None, force_rebuild=False: {
            "ok": True,
            "date": day_iso or "2026-03-15",
            "decision": {"mode": "HEAVY", "subtitle": "HEAVY blieb Standard"},
            "night_cycle": {"simulations_count": 4},
            "clone": {"trust_score": 0.7},
            "scenarios": {"list": []},
            "learning": {"latest_updates": []},
            "trace": {"steps": []},
            "generated_at": "2026-03-15T00:00:00Z",
        },
    )

    client = app.test_client()
    resp = client.get("/api/core/v2/explain")
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["ok"] is True
    assert payload["decision"]["mode"] == "HEAVY"


def test_core_v2_decision_and_clone_endpoints(monkeypatch):
    app = _build_app(monkeypatch)
    monkeypatch.setattr(
        core_api,
        "get_decision_v2_payload",
        lambda day_iso=None: {"ok": True, "decision": {"mode": "NORMAL"}, "date": day_iso or "2026-03-15"},
    )
    monkeypatch.setattr(
        core_api,
        "get_clone_payload",
        lambda day_iso=None: {"ok": True, "clone": {"trust_score": 0.66}, "date": day_iso or "2026-03-15"},
    )

    client = app.test_client()
    decision_resp = client.get("/api/core/v2/decision")
    clone_resp = client.get("/api/core/v2/clone")

    assert decision_resp.status_code == 200
    assert clone_resp.status_code == 200
    assert decision_resp.get_json()["decision"]["mode"] == "NORMAL"
    assert clone_resp.get_json()["clone"]["trust_score"] == 0.66
