from __future__ import annotations

from pathlib import Path

from flask import Flask, g

import security.private_access as private_access
import security.write_guard as write_guard


def test_guest_write_guard_blocks_terminal_theme_and_mutations(monkeypatch):
    app = Flask(__name__)
    app.config.update(TESTING=True, SECRET_KEY="test")
    monkeypatch.setattr(private_access, "get_current_role", lambda: "key")

    with app.test_request_context("/api/terminal/exec", method="POST", headers={"Accept": "application/json"}):
        response, status = private_access.enforce_key_read_only()
        assert status == 403
        assert response.get_json()["error"] == "forbidden"

    with app.test_request_context("/api/settings/theme", method="POST", headers={"Accept": "application/json"}):
        _, status = private_access.enforce_key_read_only()
        assert status == 403

    with app.test_request_context("/api/nutrition/plan/week", method="PATCH", headers={"Accept": "application/json"}):
        _, status = private_access.enforce_key_read_only()
        assert status == 403


def test_guest_keeps_safe_read_and_preview_requests(monkeypatch):
    app = Flask(__name__)
    app.config.update(TESTING=True, SECRET_KEY="test")
    monkeypatch.setattr(private_access, "get_current_role", lambda: "key")

    with app.test_request_context("/api/nutrition/plan/week", method="GET"):
        assert private_access.enforce_key_read_only() is None

    with app.test_request_context("/api/training/validate_draft", method="POST"):
        assert private_access.enforce_key_read_only() is None


def test_guest_can_pass_read_only_admin_decorators(monkeypatch):
    app = Flask(__name__)
    app.config.update(TESTING=True, SECRET_KEY="test")
    monkeypatch.setattr(private_access, "get_current_role", lambda: "key")

    protected = private_access.require_master(lambda: "visible")
    tailscale_protected = private_access.require_tailscale(lambda: "visible")

    with app.test_request_context("/api/export", method="GET"):
        assert protected() == "visible"

    with app.test_request_context("/api/physique/updates", method="GET", environ_base={"REMOTE_ADDR": "203.0.113.10"}):
        assert tailscale_protected() == "visible"

    with app.test_request_context("/api/settings/access_keys", method="POST", headers={"Accept": "application/json"}):
        response, status = protected()
        assert status == 403
        assert response.get_json()["error"] == "forbidden"


def test_guest_frontend_contains_current_read_views():
    root = Path(__file__).parents[1]
    planning = (root / "templates/planning.html").read_text(encoding="utf-8")
    dashboard = (root / "templates/dashboard.html").read_text(encoding="utf-8")
    nutrition = (root / "templates/partials/_nutrition_evaluation_content.html").read_text(encoding="utf-8")
    mobile = (root / "static/js/planning_mobile.js").read_text(encoding="utf-8")
    coach = (root / "templates/partials/_training_analysis_content.html").read_text(encoding="utf-8")
    modern_dashboard = (root / "frontend/src/DashboardApp.tsx").read_text(encoding="utf-8")

    combined = "\n".join((planning, dashboard, nutrition, mobile, coach))
    assert "Kein Zugang" not in combined
    assert "Im Gastzugang ausgeblendet" not in combined
    assert "js/planning_nutrition.js" in planning
    assert 'fetchJSON("/api/nutrition/plan/week")' in mobile
    assert 'dashboardRoot?.dataset.authLevel === "key"' in modern_dashboard
    assert 'readOnly || !slotId' in modern_dashboard


def test_legacy_write_guard_does_not_exempt_terminal_or_theme():
    app = Flask(__name__)
    app.config.update(TESTING=True, SECRET_KEY="test")

    for path in ("/api/terminal/exec", "/api/settings/theme"):
        with app.test_request_context(path, method="POST", headers={"Accept": "application/json"}):
            g.auth_level = "key"
            try:
                write_guard.enforce_write_protection()
            except Exception as exc:
                assert getattr(exc, "code", None) == 403
            else:
                raise AssertionError(f"guest write unexpectedly allowed for {path}")
