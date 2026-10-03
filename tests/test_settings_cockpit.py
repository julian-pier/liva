from __future__ import annotations

import pytest


@pytest.fixture()
def client():
    from app import app
    app.config.update(TESTING=True)
    with app.test_client() as value:
        with value.session_transaction() as session:
            session["role"] = "master"
            session["csrf_token"] = "test-csrf"
        yield value


def test_settings_cockpit_has_a_real_overview_and_stable_subroutes(client):
    for path in ("/settings", "/settings/notifications", "/settings/recovery", "/settings/jobs", "/settings/self-healing", "/settings/releases", "/settings/integrations", "/settings/config", "/settings/secrets", "/settings/logs"):
        response = client.get(path)
        assert response.status_code == 200
        assert b"data-ops-cockpit" in response.data
        assert b"settings_cockpit.js" in response.data


def test_settings_cockpit_rejects_unknown_subroute_and_retains_legacy_escape_hatch(client):
    assert client.get("/settings/nope").status_code == 404
    legacy = client.get("/settings?legacy=1")
    assert legacy.status_code == 200
    assert b"data-control-center" in legacy.data


def test_cockpit_client_uses_existing_control_center_endpoints_only():
    source = open("static/js/settings_cockpit.js", encoding="utf-8").read()
    for endpoint in ("/api/control-center/summary", "/api/control-center/jobs", "/api/control-center/self-healing", "/api/control-center/notifications", "/api/control-center/releases/status", "/api/control-center/events", "/api/control-center/logs?limit=200"):
        assert endpoint in source
    assert "demo" not in source.lower()
    assert "mock" not in source.lower()


def test_cockpit_logs_view_renders_live_journal_lines_not_control_center_events():
    source = open("static/js/settings_cockpit.js", encoding="utf-8").read()
    logs_view = source[source.index("function logsView"):source.index("function openIncidents")]

    assert "model.logs.logs" in logs_view
    assert "ops-terminal" in logs_view
    assert "model.events.events" not in logs_view


def test_cockpit_actions_do_not_chain_dom_append_and_static_assets_are_versioned():
    source = open("static/js/settings_cockpit.js", encoding="utf-8").read()
    template = open("templates/settings_cockpit.html", encoding="utf-8").read()

    assert '.append(el("div","ops-card-actions")).append' not in source
    assert "20260924_ops_v4" in template


def test_cockpit_recognizes_verified_recovery_states():
    source = open("static/js/settings_cockpit.js", encoding="utf-8").read()

    assert 'transport_verified:"Verifiziert"' in source
    assert 'verified_windows_secondary_copy:"Verifiziert"' in source


def test_cockpit_exposes_confirmed_full_service_restart_to_master(client, monkeypatch):
    from control_center import emergency

    restarted = []
    monkeypatch.setattr(emergency, "restart", lambda target, **_kwargs: restarted.append(target) or {"audit": {"result": "success"}})
    response = client.post(
        "/api/control-center/services/restart-all",
        json={"confirmed": True},
        headers={"X-CSRF-Token": "test-csrf"},
    )

    assert response.status_code == 202
    assert restarted == ["mcp", "liva"]
    source = open("static/js/settings_cockpit.js", encoding="utf-8").read()
    assert "Gesamten LIVA-Service neu starten" in source
    assert "/api/control-center/services/restart-all" in source
