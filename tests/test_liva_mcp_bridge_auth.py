from __future__ import annotations

from flask import Flask, jsonify

from security.write_guard import require_ai_read, require_ai_write


def _app() -> Flask:
    app = Flask(__name__)

    @app.post("/api/v2/actions/liva/read")
    @require_ai_read
    def liva_read():
        return jsonify({"ok": True})

    @app.post("/api/v2/actions/liva/act")
    @require_ai_write
    def liva_act():
        return jsonify({"ok": True})

    @app.post("/api/v2/actions/training/today/card/refresh")
    @require_ai_write
    def training_card_refresh():
        return jsonify({"ok": True})

    @app.post("/api/v2/actions/other/act")
    @require_ai_write
    def other_act():
        return jsonify({"ok": True})

    return app


def test_private_bridge_token_only_authorizes_loopback_liva_facades(tmp_path, monkeypatch):
    token_file = tmp_path / "bridge-token"
    token_file.write_text("b" * 64, encoding="utf-8")
    token_file.chmod(0o600)
    monkeypatch.setenv("LIVA_MCP_ACTIONS_TOKEN_FILE", str(token_file))
    monkeypatch.setenv("AI_WRITE_ENABLED", "1")
    monkeypatch.delenv("LIVA_AI_READ_KEY", raising=False)
    monkeypatch.delenv("LIVA_AI_WRITE_KEY", raising=False)
    headers = {"Authorization": "Bearer " + "b" * 64, "X-LIVA-MCP-Bridge": "1"}

    client = _app().test_client()
    assert client.post("/api/v2/actions/liva/read", headers=headers).status_code == 200
    assert client.post("/api/v2/actions/liva/act", headers=headers).status_code == 200
    assert client.post("/api/v2/actions/training/today/card/refresh", headers=headers).status_code == 200
    assert client.post("/api/v2/actions/other/act", headers=headers).status_code != 200
    assert client.post("/api/v2/actions/liva/act", headers={"Authorization": headers["Authorization"]}).status_code != 200
    assert client.post(
        "/api/v2/actions/liva/act", headers=headers, environ_overrides={"REMOTE_ADDR": "192.0.2.10"}
    ).status_code != 200


def test_bridge_rejects_token_file_with_group_permissions(tmp_path, monkeypatch):
    token_file = tmp_path / "bridge-token"
    token_file.write_text("b" * 64, encoding="utf-8")
    token_file.chmod(0o640)
    monkeypatch.setenv("LIVA_MCP_ACTIONS_TOKEN_FILE", str(token_file))
    monkeypatch.setenv("AI_WRITE_ENABLED", "1")
    monkeypatch.delenv("LIVA_AI_WRITE_KEY", raising=False)

    response = _app().test_client().post(
        "/api/v2/actions/liva/act",
        headers={"Authorization": "Bearer " + "b" * 64, "X-LIVA-MCP-Bridge": "1"},
    )
    assert response.status_code != 200
