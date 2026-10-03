from __future__ import annotations

import importlib
from flask import Flask

import database.connections as connections
from tests.test_actions_v2_api import auth


def test_create_guest_key_is_read_only_and_capped(tmp_path, monkeypatch):
    monkeypatch.setenv("LIVA_AI_READ_KEY", "test")
    monkeypatch.setenv("LIVA_AI_WRITE_KEY", "test")
    monkeypatch.setenv("AI_WRITE_ENABLED", "1")
    monkeypatch.setenv("LIVA_IP_WHITELIST", "")
    connections.CORE_DB = str(tmp_path / "core.sqlite3")
    connections.AUTH_DB = str(tmp_path / "auth.sqlite3")

    import ai.actions_v2 as actions_v2
    import security.private_access as private_access

    importlib.reload(private_access)
    importlib.reload(actions_v2)

    app = Flask(__name__)
    app.config["TESTING"] = True
    app.config["SECRET_KEY"] = "test"
    app.register_blueprint(actions_v2.actions_v2_api)
    app.register_blueprint(private_access.private_access_bp)

    @app.before_request
    def _guards():
        response = private_access.enforce_private_access()
        if response is not None:
            return response
        response = private_access.enforce_key_read_only()
        if response is not None:
            return response
        return None

    with app.test_client() as client:
        created = client.post(
            "/api/v2/actions/liva/act",
            json={"domain": "access", "command": "create_guest_key", "payload": {"label": "Gast für Max", "expires_in_hours": 24}},
            headers=auth(),
        )
        assert created.status_code == 200
        body = created.get_json()
        assert body["guest_key"].startswith("thk_")
        assert body["execution"]["mode"] == "applied_live"

        capped = client.post(
            "/api/v2/actions/liva/act",
            json={"domain": "access", "command": "create_guest_key", "payload": {"expires_in_hours": 169}},
            headers=auth(),
        )
        assert capped.status_code == 400

        read = client.get("/_private_access/status", headers={"Authorization": f"Bearer {body['guest_key']}", "Accept": "application/json"})
        assert read.status_code == 200
        assert read.get_json()["role"] == "public"
        assert private_access._validate_key(body["guest_key"])[0] == "ok"

        with client.session_transaction() as session:
            session["role"] = "key"
            session["key_id"] = body["result"]["key_id"]

        blocked = client.post(
            "/api/v2/actions/liva/act",
            json={"domain": "recovery", "command": "set_alcohol_flag", "date": "2026-05-12"},
            headers={"Accept": "application/json"},
        )
        assert blocked.status_code == 403


def test_create_guest_key_requires_write_enabled(tmp_path, monkeypatch):
    monkeypatch.setenv("LIVA_AI_READ_KEY", "test")
    monkeypatch.setenv("LIVA_AI_WRITE_KEY", "test")
    monkeypatch.setenv("AI_WRITE_ENABLED", "0")
    connections.CORE_DB = str(tmp_path / "core.sqlite3")
    connections.AUTH_DB = str(tmp_path / "auth.sqlite3")

    import ai.actions_v2 as actions_v2
    importlib.reload(actions_v2)

    app = Flask(__name__)
    app.config["TESTING"] = True
    app.register_blueprint(actions_v2.actions_v2_api)

    with app.test_client() as client:
        resp = client.post(
            "/api/v2/actions/liva/act",
            json={"domain": "access", "command": "create_guest_key"},
            headers=auth(),
        )
        assert resp.status_code == 403
