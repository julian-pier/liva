import ipaddress
from pathlib import Path

from flask import Flask

import security.private_access as pa


def _build_app() -> Flask:
    app = Flask(__name__, template_folder=str(Path(__file__).resolve().parents[1] / "templates"))
    app.secret_key = "test-secret"
    return app


def _build_guarded_app() -> Flask:
    app = _build_app()
    app.register_blueprint(pa.private_access_bp)

    @app.before_request
    def _guard():
        return pa.enforce_private_access()

    @app.get("/")
    def _home():
        return "real website"

    @app.get("/api/protected")
    def _protected():
        return {"ok": True}

    return app


def test_resolve_client_ip_uses_forwarded_headers_for_trusted_proxy(monkeypatch):
    app = _build_app()
    monkeypatch.setattr(
        pa,
        "TRUSTED_PROXY_NETWORKS",
        (ipaddress.ip_network("172.16.0.0/12"),),
    )
    with app.test_request_context(
        "/",
        environ_base={"REMOTE_ADDR": "172.20.0.15"},
        headers={"X-Forwarded-For": "100.89.12.34, 172.20.0.15"},
    ):
        ip, source = pa._resolve_client_ip_for_whitelist()
    assert ip == "100.89.12.34"
    assert source == "forwarded_headers"


def test_resolve_client_ip_keeps_remote_for_untrusted_proxy(monkeypatch):
    app = _build_app()
    monkeypatch.setattr(
        pa,
        "TRUSTED_PROXY_NETWORKS",
        (ipaddress.ip_network("127.0.0.1/32"),),
    )
    with app.test_request_context(
        "/",
        environ_base={"REMOTE_ADDR": "172.20.0.15"},
        headers={"X-Forwarded-For": "100.89.12.34"},
    ):
        ip, source = pa._resolve_client_ip_for_whitelist()
    assert ip == "172.20.0.15"
    assert source == "remote_addr"


def test_openapi_schema_paths_are_exempt_from_private_access():
    assert pa._is_exempt_path("/openapi.yaml") is True
    assert pa._is_exempt_path("/.well-known/openapi.yaml") is True


def test_private_access_accepts_raw_authorization_api_key(monkeypatch):
    app = _build_app()
    monkeypatch.setattr(pa, "AI_READ_API_KEY", "read-key")
    monkeypatch.setattr(pa, "AI_WRITE_API_KEY", "write-key")

    with app.test_request_context(
        "/api/dashboard/today",
        environ_base={"REMOTE_ADDR": "203.0.113.10"},
        headers={"Authorization": "write-key", "Accept": "application/json"},
    ):
        assert pa.get_current_role() == "master"
        assert pa.enforce_private_access() is None


def test_private_access_accepts_liva_api_key_header(monkeypatch):
    app = _build_app()
    monkeypatch.setattr(pa, "AI_READ_API_KEY", "read-key")
    monkeypatch.setattr(pa, "AI_WRITE_API_KEY", "write-key")

    with app.test_request_context(
        "/api/dashboard/today",
        environ_base={"REMOTE_ADDR": "203.0.113.10"},
        headers={"X-LIVA-Api-Key": "read-key", "Accept": "application/json"},
    ):
        assert pa.get_current_role() == "key"
        assert pa.enforce_private_access() is None


def test_direct_tailscale_ip_auto_whitelists(monkeypatch):
    app = _build_app()
    monkeypatch.setattr(pa, "AI_READ_API_KEY", "read-key")
    monkeypatch.setattr(pa, "AI_WRITE_API_KEY", "write-key")
    monkeypatch.setattr(pa, "IP_WHITELIST_ENTRIES", ["100.64.0.0/10", "fd7a:115c:a1e0::/48"])

    with app.test_request_context(
        "/",
        environ_base={"REMOTE_ADDR": "100.89.12.34"},
    ):
        assert pa.get_current_role() == "master"
        assert pa.enforce_private_access() is None


def test_forwarded_tailscale_ip_does_not_auto_whitelist(monkeypatch):
    app = _build_app()
    app.register_blueprint(pa.private_access_bp)
    monkeypatch.setattr(pa, "AI_READ_API_KEY", "read-key")
    monkeypatch.setattr(pa, "AI_WRITE_API_KEY", "write-key")
    monkeypatch.setattr(pa, "TRUSTED_PROXY_NETWORKS", (ipaddress.ip_network("100.64.0.0/10"),))
    monkeypatch.setattr(pa, "IP_WHITELIST_ENTRIES", ["100.64.0.0/10", "fd7a:115c:a1e0::/48"])

    with app.test_request_context(
        "/",
        environ_base={"REMOTE_ADDR": "100.98.133.6"},
        headers={"X-Forwarded-For": "100.89.12.34"},
    ):
        assert pa.get_current_role() == "public"
        response = pa.enforce_private_access()
        assert response is not None


def test_browser_tailscale_request_gets_real_website(monkeypatch):
    app = _build_guarded_app()
    monkeypatch.setattr(pa, "AI_READ_API_KEY", "read-key")
    monkeypatch.setattr(pa, "AI_WRITE_API_KEY", "write-key")
    monkeypatch.setattr(pa, "IP_WHITELIST_ENTRIES", ["100.64.0.0/10", "fd7a:115c:a1e0::/48"])

    response = app.test_client().get("/", environ_base={"REMOTE_ADDR": "100.89.12.34"})

    assert response.status_code == 200
    assert response.get_data(as_text=True) == "real website"
    assert "/_private_access" not in (response.headers.get("Location") or "")


def test_browser_public_request_redirects_to_private_access(monkeypatch):
    app = _build_guarded_app()
    monkeypatch.setattr(pa, "AI_READ_API_KEY", "read-key")
    monkeypatch.setattr(pa, "AI_WRITE_API_KEY", "write-key")
    monkeypatch.setattr(pa, "IP_WHITELIST_ENTRIES", ["100.64.0.0/10", "fd7a:115c:a1e0::/48"])

    response = app.test_client().get("/", environ_base={"REMOTE_ADDR": "203.0.113.10"})

    assert response.status_code == 302
    assert "/_private_access" in response.headers["Location"]
    assert "no-store" in response.headers.get("Cache-Control", "")
    assert response.headers.get("Pragma") == "no-cache"


def test_private_access_login_page_is_not_cacheable(monkeypatch):
    app = _build_guarded_app()
    monkeypatch.setattr(pa, "AI_READ_API_KEY", "read-key")
    monkeypatch.setattr(pa, "AI_WRITE_API_KEY", "write-key")

    response = app.test_client().get(
        "/_private_access?next=/",
        environ_base={"REMOTE_ADDR": "203.0.113.10"},
    )

    assert response.status_code == 200
    assert "no-store" in response.headers.get("Cache-Control", "")
    assert response.headers.get("Pragma") == "no-cache"
    assert b"/_private_access/status" in response.data


def test_private_access_status_flips_when_tailscale_is_seen(monkeypatch):
    app = _build_guarded_app()
    monkeypatch.setattr(pa, "AI_READ_API_KEY", "read-key")
    monkeypatch.setattr(pa, "AI_WRITE_API_KEY", "write-key")
    monkeypatch.setattr(pa, "IP_WHITELIST_ENTRIES", ["100.64.0.0/10", "fd7a:115c:a1e0::/48"])

    public_response = app.test_client().get(
        "/_private_access/status",
        environ_base={"REMOTE_ADDR": "203.0.113.10"},
    )
    tailscale_response = app.test_client().get(
        "/_private_access/status",
        environ_base={"REMOTE_ADDR": "100.89.12.34"},
    )

    assert public_response.status_code == 200
    assert public_response.get_json()["allowed"] is False
    assert "no-store" in public_response.headers.get("Cache-Control", "")
    assert tailscale_response.status_code == 200
    assert tailscale_response.get_json()["allowed"] is True


def test_api_route_with_valid_authorization_header_bypasses_login(monkeypatch):
    app = _build_guarded_app()
    monkeypatch.setattr(pa, "AI_READ_API_KEY", "read-key")
    monkeypatch.setattr(pa, "AI_WRITE_API_KEY", "write-key")

    response = app.test_client().get(
        "/api/protected",
        environ_base={"REMOTE_ADDR": "203.0.113.10"},
        headers={"Authorization": "Bearer read-key", "Accept": "application/json"},
    )

    assert response.status_code == 200
    assert response.is_json
    assert response.get_json() == {"ok": True}
    assert "text/html" not in (response.content_type or "")


def test_api_route_without_key_stays_unauthorized(monkeypatch):
    app = _build_guarded_app()
    monkeypatch.setattr(pa, "AI_READ_API_KEY", "read-key")
    monkeypatch.setattr(pa, "AI_WRITE_API_KEY", "write-key")

    response = app.test_client().get(
        "/api/protected",
        environ_base={"REMOTE_ADDR": "203.0.113.10"},
        headers={"Accept": "application/json"},
    )

    assert response.status_code == 401
    assert response.is_json
    assert response.get_json()["error"] == "auth_required"


def test_spoofed_forwarded_tailscale_ip_does_not_bypass_login(monkeypatch):
    app = _build_guarded_app()
    monkeypatch.setattr(pa, "AI_READ_API_KEY", "read-key")
    monkeypatch.setattr(pa, "AI_WRITE_API_KEY", "write-key")
    monkeypatch.setattr(pa, "IP_WHITELIST_ENTRIES", ["100.64.0.0/10", "fd7a:115c:a1e0::/48"])

    response = app.test_client().get(
        "/",
        environ_base={"REMOTE_ADDR": "100.89.12.34"},
        environ_overrides={"werkzeug.proxy_fix.orig": {"REMOTE_ADDR": "203.0.113.10"}},
        headers={"X-Forwarded-For": "100.89.12.34"},
    )

    assert response.status_code == 302
    assert "/_private_access" in response.headers["Location"]


def test_tailscale_identity_header_from_trusted_proxy_allows_browser(monkeypatch):
    app = _build_guarded_app()
    monkeypatch.setattr(pa, "AI_READ_API_KEY", "read-key")
    monkeypatch.setattr(pa, "AI_WRITE_API_KEY", "write-key")
    monkeypatch.setattr(pa, "TRUSTED_PROXY_NETWORKS", (ipaddress.ip_network("127.0.0.1/32"),))
    monkeypatch.setattr(pa, "IP_WHITELIST_ENTRIES", ["100.64.0.0/10", "fd7a:115c:a1e0::/48"])

    response = app.test_client().get(
        "/",
        environ_base={"REMOTE_ADDR": "100.89.12.34"},
        environ_overrides={"werkzeug.proxy_fix.orig": {"REMOTE_ADDR": "127.0.0.1"}},
        headers={"Tailscale-User-Login": "user@example.test", "X-Forwarded-For": "100.89.12.34"},
    )

    assert response.status_code == 200
    assert response.get_data(as_text=True) == "real website"
