from __future__ import annotations

import importlib
from pathlib import Path

import core.core_api as core_api


def _load_app_module(monkeypatch):
    monkeypatch.setenv("LIVA_FLASK_SECRET", "test-secret")
    appmod = importlib.import_module("app")
    monkeypatch.setattr(appmod, "enforce_private_access", lambda: None)
    monkeypatch.setattr(appmod, "enforce_key_read_only", lambda: None)
    monkeypatch.setattr(appmod, "enforce_ip_whitelist", lambda: None)
    monkeypatch.setattr(appmod, "enforce_write_protection", lambda: None)
    monkeypatch.setattr(appmod, "ensure_core_v2_ready", lambda: None)
    monkeypatch.setattr(core_api, "_ensure_core_access", lambda: None)
    monkeypatch.setattr(
        appmod,
        "core_board_view_model",
        lambda *_args, **_kwargs: {"summary": {"headline": "QA", "line": "Read only"}, "signals": [], "ui": {"training_card": {"href": "/training"}}, "decision": {"label": "OPEN"}},
    )
    return appmod


def test_test_ui_sets_session_and_redirects_to_root(monkeypatch):
    appmod = _load_app_module(monkeypatch)
    client = appmod.app.test_client()

    response = client.get("/test-ui")

    assert response.status_code == 302
    assert response.headers["Location"].endswith("/")
    with client.session_transaction() as sess:
        assert sess["test_mode"] is True
        assert sess["role"] == "qa_readonly"


def test_normal_core_page_contains_banner_in_test_mode(monkeypatch):
    appmod = _load_app_module(monkeypatch)
    client = appmod.app.test_client()
    with client.session_transaction() as sess:
        sess["test_mode"] = True
        sess["role"] = "qa_readonly"

    response = client.get("/core")

    assert response.status_code == 200
    body = response.data.decode("utf-8")
    assert 'data-testid="test-mode-banner"' in body
    assert "TESTMODUS · Read-only · keine echten Schreibaktionen" in body
    assert 'data-testid="main-navigation"' in body
    assert 'data-testid="core-decision-card"' in body


def test_modern_dashboard_exposes_read_only_test_mode_to_react(monkeypatch):
    appmod = _load_app_module(monkeypatch)
    client = appmod.app.test_client()
    with client.session_transaction() as sess:
        sess["test_mode"] = True
        sess["role"] = "qa_readonly"

    response = client.get("/")

    assert response.status_code == 200
    body = response.data.decode("utf-8")
    assert 'id="liva-dashboard-root"' in body
    assert 'data-liva-surface="dashboard"' in body
    assert 'data-test-mode="true"' in body


def test_post_to_real_write_endpoint_is_blocked_in_test_mode(monkeypatch):
    appmod = _load_app_module(monkeypatch)
    client = appmod.app.test_client()
    with client.session_transaction() as sess:
        sess["test_mode"] = True
        sess["role"] = "qa_readonly"

    response = client.post("/api/hrv/flag", json={"date": "2026-05-13", "flag": "sick"})

    assert response.status_code == 200
    payload = response.get_json()
    assert payload == {
        "ok": True,
        "test_mode": True,
        "dry_run": True,
        "blocked_write": True,
        "message": "Testmodus: keine echte Änderung gespeichert.",
    }


def test_same_post_outside_test_mode_is_not_caught_by_test_mode_block(monkeypatch):
    appmod = _load_app_module(monkeypatch)
    called = {"value": False}

    def fake_flag():
        called["value"] = True
        return appmod.jsonify({"ok": True, "live": True})

    appmod.app.view_functions["api_hrv_flag"] = fake_flag
    client = appmod.app.test_client()

    response = client.post("/api/hrv/flag", json={"date": "2026-05-13", "flag": "sick"})

    assert response.status_code == 200
    assert response.get_json() == {"ok": True, "live": True}
    assert called["value"] is True


def test_test_ui_exit_disables_test_mode(monkeypatch):
    appmod = _load_app_module(monkeypatch)
    client = appmod.app.test_client()
    with client.session_transaction() as sess:
        sess["test_mode"] = True
        sess["role"] = "qa_readonly"

    response = client.get("/test-ui/exit")

    assert response.status_code == 302
    assert response.headers["Location"].endswith("/")
    with client.session_transaction() as sess:
        assert "test_mode" not in sess
        assert "role" not in sess


def test_dashboard_js_no_longer_fetches_actions_v2_training_card():
    path = "/opt/liva/static/js/dashboard_vnext.js"
    with open(path, "r", encoding="utf-8") as handle:
        content = handle.read()
    assert "/api/v2/actions/core/training-card" not in content


def test_pcs_unit_is_rendered_in_german(monkeypatch):
    appmod = _load_app_module(monkeypatch)
    assert appmod._format_de_unit_label("pcs") == "Stk."
    assert appmod._format_de_unit_label("g") == "g"


def test_mobile_shell_uses_document_scroll_instead_of_nested_main_content():
    stylesheet = (Path(__file__).parents[1] / "static" / "css" / "style.css").read_text(encoding="utf-8")

    assert "@media (max-width: 767px) and (pointer: coarse)" in stylesheet
    assert ".main-area,\n    .main-content {\n        overflow: visible !important;" in stylesheet
    assert "body:not(.core-explain-open):not(.physique-no-scroll)" in stylesheet


def test_mobile_subpage_headers_do_not_keep_desktop_flex_height():
    stylesheet = (Path(__file__).parents[1] / "static" / "css" / "style.css").read_text(encoding="utf-8")

    assert ".liva-subpage-hero-copy {\n        flex: 0 1 auto;\n        width: 100%;" in stylesheet
    assert ".liva-subpage-hero {\n        gap: 8px !important;\n        padding: 12px 14px !important;" in stylesheet
    assert "font-size: 1.35rem !important;" in stylesheet


def test_mobile_keeps_saved_compact_mode_and_uses_distinct_page_inset():
    root = Path(__file__).parents[1]
    script = (root / "static" / "js" / "app.js").read_text(encoding="utf-8")
    stylesheet = (root / "static" / "css" / "style.css").read_text(encoding="utf-8")

    assert 'root.dataset.layoutMode = "wide"' not in script
    assert "safeStorage.get(LAYOUT_STORAGE_KEY) || root.dataset.layoutMode" in script
    assert 'html[data-layout-mode="wide"] .app-page-shell {' in stylesheet
    assert "padding-inline: 10px !important;" in stylesheet
    assert 'html[data-layout-mode="compact"] .app-page-shell {' in stylesheet
    assert "padding-inline: 28px !important;" in stylesheet
    assert 'html[data-layout-mode="compact"] .liva-subpage-shell {' in stylesheet
    assert "gap: 10px !important;" in stylesheet
    assert "font-size: 1.2rem !important;" in stylesheet


def test_navigation_does_not_intercept_touch_links_for_double_navigation():
    script = (Path(__file__).parents[1] / "static" / "js" / "app.js").read_text(encoding="utf-8")

    assert "installInstantNavGuard" not in script
    assert "window.location.assign(targetUrl.href)" not in script


def test_mobile_navigation_never_locks_ios_body_scroll_or_prevents_pointerdown():
    root = Path(__file__).parents[1]
    script = (root / "static" / "js" / "app.js").read_text(encoding="utf-8")
    stylesheet = (root / "static" / "css" / "style.css").read_text(encoding="utf-8")

    assert 'mobileMenuBtn.addEventListener("pointerdown"' not in script
    assert 'classList.toggle("mobile-nav-open"' not in script
    assert "body.mobile-nav-open {\n        overflow-y: auto !important;" in stylesheet
    assert 'document.addEventListener("touchmove"' not in script
    assert 'window.setTimeout(clearStaleUiLocks' not in script
    assert "min-height: 100dvh !important;" in stylesheet
    assert "overflow: visible !important;" in stylesheet
