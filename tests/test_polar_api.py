from __future__ import annotations

import importlib
import io
import urllib.error
from urllib.parse import urlparse, parse_qs

import pytest
from flask import Flask

import database.connections as connections


@pytest.fixture()
def polar_app(tmp_path, monkeypatch):
    connections.POLAR_DB = str(tmp_path / "polar.sqlite3")
    monkeypatch.setenv("POLAR_CLIENT_ID", "polar-client")
    monkeypatch.setenv("POLAR_CLIENT_SECRET", "polar-secret")
    monkeypatch.setenv("POLAR_REDIRECT_URI", "https://liva.example.com/api/polar/callback")
    monkeypatch.setenv("LIVA_FLASK_SECRET", "test-flask-secret")

    import integrations.polar_client as polar_client_module
    importlib.reload(polar_client_module)
    import integrations.polar_api as polar_api_module
    importlib.reload(polar_api_module)

    app = Flask(__name__)
    app.secret_key = "test-secret"
    app.config["TESTING"] = True
    app.config["POLAR_CLIENT_ID"] = "polar-client"
    app.config["POLAR_CLIENT_SECRET"] = "polar-secret"
    app.config["POLAR_REDIRECT_URI"] = "https://liva.example.com/api/polar/callback"
    polar_api_module.init_polar_app(app)
    app.register_blueprint(polar_api_module.polar_bp)
    return app, polar_client_module, polar_api_module


def test_status_reports_disconnected_by_default(polar_app):
    app, _client_module, _api_module = polar_app
    client = app.test_client()

    resp = client.get("/api/polar/status")

    assert resp.status_code == 200
    assert resp.get_json() == {
        "connected": False,
        "polar_user_id": None,
        "expires_at": None,
        "has_refresh_token": False,
        "needs_reconnect": True,
    }


def test_connect_redirects_to_expected_oauth_url(polar_app):
    app, _client_module, _api_module = polar_app
    client = app.test_client()

    resp = client.get("/api/polar/connect")

    assert resp.status_code == 302
    location = resp.headers["Location"]
    parsed = urlparse(location)
    qs = parse_qs(parsed.query)
    assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == "https://auth.polar.com/oauth/authorize"
    assert qs["client_id"] == ["polar-client"]
    assert qs["response_type"] == ["code"]
    assert qs["redirect_uri"] == ["https://liva.example.com/api/polar/callback"]
    assert qs["scope"] == ["sleep:read nightly_recharge:read continuous_samples:read activity:read training_sessions:read profile:read"]
    assert qs["state"]


def test_callback_stores_tokens_without_logging_secret_values(polar_app, monkeypatch):
    app, polar_client_module, _api_module = polar_app
    client = app.test_client()

    with client.session_transaction() as sess:
        sess["polar_oauth_state"] = "abc123"

    class StubPolarClient:
        def require_configured(self):
            return None

        def exchange_code(self, code: str):
            assert code == "oauth-code"
            return polar_client_module.PolarTokenRecord(
                id=1,
                polar_user_id="user@example.com",
                access_token="access-token",
                refresh_token="refresh-token",
                token_type="bearer",
                expires_at="2026-05-09T10:00:00+00:00",
                scope="sleep:read",
                created_at="2026-05-09T09:00:00+00:00",
                updated_at="2026-05-09T09:00:00+00:00",
            )

    monkeypatch.setattr("integrations.polar_api.create_polar_client", lambda: StubPolarClient())
    messages: list[str] = []
    monkeypatch.setattr("integrations.polar_api.log_polar_sync", lambda sync_type, status, message: messages.append(message))

    resp = client.get("/api/polar/callback?code=oauth-code&state=abc123")

    assert resp.status_code == 200
    assert resp.get_data(as_text=True) == "Polar verbunden. Du kannst dieses Fenster schließen."
    assert messages
    assert all("access-token" not in msg and "refresh-token" not in msg for msg in messages)


def test_test_sync_returns_not_connected_without_token(polar_app):
    app, _client_module, _api_module = polar_app
    client = app.test_client()

    resp = client.post("/api/polar/test-sync")

    assert resp.status_code == 200
    assert resp.get_json() == {"ok": False, "message": "Polar ist noch nicht verbunden."}


def test_disconnect_clears_tokens(polar_app):
    app, polar_client_module, _api_module = polar_app
    client = app.test_client()
    polar_client_module.ensure_polar_schema()
    conn = connections.get_polar_db()
    conn.execute(
        """
        INSERT INTO polar_tokens (polar_user_id, access_token, refresh_token, token_type, expires_at, scope, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        ("user@example.com", "access", "refresh", "bearer", "2026-05-09T10:00:00+00:00", "sleep:read", "2026-05-09T09:00:00+00:00", "2026-05-09T09:00:00+00:00"),
    )
    conn.commit()
    conn.close()

    resp = client.post("/api/polar/disconnect")

    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True
    assert body["deleted_tokens"] == 1


def test_status_reports_needs_reconnect_without_refresh_token(polar_app):
    app, polar_client_module, _api_module = polar_app
    client = app.test_client()
    polar_client_module.ensure_polar_schema()
    conn = connections.get_polar_db()
    conn.execute(
        """
        INSERT INTO polar_tokens (polar_user_id, access_token, refresh_token, token_type, expires_at, scope, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        ("user@example.com", "access", None, "bearer", "2026-05-09T10:00:00+00:00", "sleep:read", "2026-05-09T09:00:00+00:00", "2026-05-09T09:00:00+00:00"),
    )
    conn.commit()
    conn.close()

    resp = client.get("/api/polar/status")

    assert resp.status_code == 200
    body = resp.get_json()
    assert body["connected"] is True
    assert body["has_refresh_token"] is False
    assert body["needs_reconnect"] is True


def test_status_ignores_reconnect_error_before_current_token(polar_app):
    app, polar_client_module, _api_module = polar_app
    client = app.test_client()
    polar_client_module.ensure_polar_schema()
    conn = connections.get_polar_db()
    conn.execute(
        """
        INSERT INTO polar_tokens (polar_user_id, access_token, refresh_token, token_type, expires_at, scope, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        ("user@example.com", "fresh-access", "fresh-refresh", "bearer", "2099-05-09T10:00:00+00:00", "sleep:read", "2026-05-09T10:00:00+00:00", "2026-05-09T10:00:00+00:00"),
    )
    conn.execute(
        """
        INSERT INTO polar_sync_log (sync_type, status, message, created_at)
        VALUES (?, ?, ?, ?)
        """,
        ("token_refresh", "error", "Polar Reconnect nötig.", "2026-05-09T09:00:00+00:00"),
    )
    conn.commit()
    conn.close()

    resp = client.get("/api/polar/status")

    assert resp.status_code == 200
    assert resp.get_json()["needs_reconnect"] is False


def test_get_json_refreshes_once_after_401_and_retries_successfully(polar_app, monkeypatch):
    _app, polar_client_module, _api_module = polar_app
    client = polar_client_module.PolarClient(
        client_id="polar-client",
        client_secret="polar-secret",
        redirect_uri="https://liva.example.com/api/polar/callback",
    )
    polar_client_module.ensure_polar_schema()
    conn = connections.get_polar_db()
    conn.execute(
        """
        INSERT INTO polar_tokens (polar_user_id, access_token, refresh_token, token_type, expires_at, scope, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        ("user@example.com", "old-access", "refresh-token", "bearer", "2099-05-09T10:00:00+00:00", "sleep:read", "2026-05-09T09:00:00+00:00", "2026-05-09T09:00:00+00:00"),
    )
    conn.commit()
    conn.close()

    calls = {"count": 0}
    refreshes: list[str] = []
    logs: list[tuple[str, str, str]] = []

    class FakeResponse:
        status = 200

        def __init__(self, payload: str):
            self.payload = payload

        def read(self):
            return self.payload.encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    def fake_urlopen(req, timeout=0):
        calls["count"] += 1
        auth = req.headers.get("Authorization")
        if calls["count"] == 1:
            raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", hdrs=None, fp=io.BytesIO(b"<html>Unauthorized</html>"))
        assert auth == "Bearer new-access"
        return FakeResponse('{"ok": true, "nightSleeps": []}')

    def fake_refresh(refresh_token):
        refreshes.append(refresh_token)
        conn = connections.get_polar_db()
        conn.execute("UPDATE polar_tokens SET access_token = ?, updated_at = ? WHERE id = 1", ("new-access", "2026-05-09T10:01:00+00:00"))
        conn.commit()
        conn.close()
        return client.get_saved_token()

    monkeypatch.setattr(polar_client_module.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(client, "refresh_access_token", fake_refresh)
    monkeypatch.setattr(polar_client_module, "log_polar_sync", lambda sync_type, status, message: logs.append((sync_type, status, message)))

    payload = client.get_json("/sleeps", params={"from": "2026-05-10", "to": "2026-05-11"})

    assert payload["ok"] is True
    assert calls["count"] == 2
    assert refreshes == ["refresh-token"]
    assert any(message == "Polar Token automatisch erneuert." for _sync_type, _status, message in logs)


def test_get_json_401_without_refresh_token_raises_clear_reconnect_error(polar_app, monkeypatch):
    _app, polar_client_module, _api_module = polar_app
    client = polar_client_module.PolarClient(
        client_id="polar-client",
        client_secret="polar-secret",
        redirect_uri="https://liva.example.com/api/polar/callback",
    )
    polar_client_module.ensure_polar_schema()
    conn = connections.get_polar_db()
    conn.execute(
        """
        INSERT INTO polar_tokens (polar_user_id, access_token, refresh_token, token_type, expires_at, scope, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        ("user@example.com", "old-access", None, "bearer", "2099-05-09T10:00:00+00:00", "sleep:read", "2026-05-09T09:00:00+00:00", "2026-05-09T09:00:00+00:00"),
    )
    conn.commit()
    conn.close()

    logs: list[tuple[str, str, str]] = []

    def fake_urlopen(req, timeout=0):
        raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", hdrs=None, fp=io.BytesIO(b"<html><body>Unauthorized</body></html>"))

    monkeypatch.setattr(polar_client_module.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(polar_client_module, "log_polar_sync", lambda sync_type, status, message: logs.append((sync_type, status, message)))

    with pytest.raises(polar_client_module.PolarClientError) as excinfo:
        client.get_json("/sleeps", params={"from": "2026-05-10", "to": "2026-05-11"})

    assert str(excinfo.value) == "Polar nicht mehr autorisiert: bitte neu verbinden."
    assert any(message == "Polar Reconnect nötig." for _sync_type, _status, message in logs)


def test_expired_token_with_rejected_refresh_requires_reconnect(polar_app, monkeypatch):
    _app, polar_client_module, _api_module = polar_app
    client = polar_client_module.PolarClient(
        client_id="polar-client",
        client_secret="polar-secret",
        redirect_uri="https://liva.example.com/api/polar/callback",
    )
    polar_client_module.ensure_polar_schema()
    conn = connections.get_polar_db()
    conn.execute(
        """
        INSERT INTO polar_tokens (polar_user_id, access_token, refresh_token, token_type, expires_at, scope, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        ("user@example.com", "expired-access", "rejected-refresh", "bearer", "2026-05-09T10:00:00+00:00", "sleep:read", "2026-05-09T09:00:00+00:00", "2026-05-09T09:00:00+00:00"),
    )
    conn.commit()
    conn.close()

    logs: list[tuple[str, str, str]] = []

    def fake_urlopen(req, timeout=0):
        raise urllib.error.HTTPError(req.full_url, 400, "Bad Request", hdrs=None, fp=io.BytesIO(b"invalid refresh token"))

    monkeypatch.setattr(polar_client_module.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(polar_client_module, "log_polar_sync", lambda sync_type, status, message: logs.append((sync_type, status, message)))

    with pytest.raises(polar_client_module.PolarClientError, match="Polar nicht mehr autorisiert: bitte neu verbinden"):
        client.get_json("/sleeps", params={"from": "2026-05-10", "to": "2026-05-11"})

    assert any(message == "Polar Reconnect nötig." for _sync_type, _status, message in logs)


def test_safe_http_error_detail_sanitizes_html_401_body(polar_app):
    _app, polar_client_module, _api_module = polar_app
    exc = urllib.error.HTTPError(
        "https://example.invalid",
        401,
        "Unauthorized",
        hdrs=None,
        fp=io.BytesIO(b"<html><body><h1>401 Unauthorized</h1><p>Expired token</p></body></html>"),
    )

    detail = polar_client_module._safe_http_error_detail(exc)

    assert "<html" not in detail.lower()
    assert "401 Unauthorized" in detail
    assert "Expired token" in detail


def test_get_range_json_passes_features_as_repeated_params(polar_app, monkeypatch):
    _app, polar_client_module, _api_module = polar_app
    client = polar_client_module.PolarClient(
        client_id="polar-client",
        client_secret="polar-secret",
        redirect_uri="https://liva.example.com/api/polar/callback",
    )
    captured = {}

    def fake_get_json(path, params=None):
        captured["path"] = path
        captured["params"] = params
        return {"ok": True}

    monkeypatch.setattr(client, "get_json", fake_get_json)

    client.get_sleeps("2026-05-10", "2026-05-12", features=["sleep-score", "sleep-result", "sleep-evaluation"])

    assert captured["path"] == "/sleeps"
    assert captured["params"]["from"] == "2026-05-10"
    assert captured["params"]["to"] == "2026-05-12"
    assert captured["params"]["features"] == ["sleep-score", "sleep-result", "sleep-evaluation"]


def test_get_range_json_keeps_single_feature_as_single_item_list(polar_app, monkeypatch):
    _app, polar_client_module, _api_module = polar_app
    client = polar_client_module.PolarClient(
        client_id="polar-client",
        client_secret="polar-secret",
        redirect_uri="https://liva.example.com/api/polar/callback",
    )
    captured = {}

    def fake_get_json(path, params=None):
        captured["params"] = params
        return {"ok": True}

    monkeypatch.setattr(client, "get_json", fake_get_json)

    client.get_sleeps("2026-05-10", "2026-05-12", features=["sleep-score"])

    assert captured["params"]["features"] == ["sleep-score"]


def test_test_sync_returns_sleep_feature_debug_summary(polar_app, monkeypatch):
    app, polar_client_module, _api_module = polar_app
    client = app.test_client()
    polar_client_module.ensure_polar_schema()
    conn = connections.get_polar_db()
    conn.execute(
        """
        INSERT INTO polar_tokens (polar_user_id, access_token, refresh_token, token_type, expires_at, scope, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        ("user@example.com", "access", "refresh", "bearer", "2099-05-09T10:00:00+00:00", "sleep:read", "2026-05-09T09:00:00+00:00", "2026-05-09T09:00:00+00:00"),
    )
    conn.commit()
    conn.close()

    class StubPolarClient:
        def require_configured(self):
            return None

        def get_saved_token(self):
            return object()

        def get_json(self, path, params=None):
            assert path == "/sleeps"
            assert params is not None
            return {"nightSleeps": [{"sleepDate": "2026-05-12"}]}

        def get_sleeps(self, from_date, to_date, features=None):
            assert to_date > from_date
            if features is None:
                return {"nightSleeps": [{"sleepDate": "2026-05-12"}]}
            if features == ["sleep-score"]:
                return {"nightSleeps": [{"sleepDate": "2026-05-12", "sleepScore": {"sleepScore": 52.2}}]}
            if features == ["sleep-score", "sleep-result", "sleep-evaluation"]:
                return {
                    "nightSleeps": [{
                        "sleepDate": "2026-05-12",
                        "sleepScore": {"sleepScore": 52.2},
                        "sleepResult": {"hypnogram": {"sleepStart": "2026-05-11T23:41:00+02:00"}},
                        "sleepEvaluation": {"asleepDuration": "21960s"},
                    }]
                }
            raise AssertionError(f"unexpected features: {features}")

    monkeypatch.setattr("integrations.polar_api.create_polar_client", lambda: StubPolarClient())

    resp = client.post("/api/polar/test-sync")

    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True
    assert body["sleep_debug"]["debug_day"]
    assert body["sleep_debug"]["without_features"]["ok"] is True
    assert body["sleep_debug"]["without_features"]["root_keys"] == ["nightSleeps"]
    assert body["sleep_debug"]["without_features"]["first_item_keys"] == ["sleepDate"]
    assert body["sleep_debug"]["without_features"]["item_count"] == 1
    assert body["sleep_debug"]["sleep_score"]["ok"] is True
    assert body["sleep_debug"]["sleep_score"]["has_sleepScore"] is True
    assert body["sleep_debug"]["sleep_score"]["has_sleepResult"] is False
    assert body["sleep_debug"]["sleep_multi_feature"]["ok"] is True
    assert body["sleep_debug"]["sleep_multi_feature"]["has_sleepScore"] is True
    assert body["sleep_debug"]["sleep_multi_feature"]["has_sleepResult"] is True
    assert body["sleep_debug"]["sleep_multi_feature"]["has_sleepEvaluation"] is True
    assert body["breathing_debug"]["nightly"]["ok"] is False


def test_test_sync_keeps_partial_sleep_debug_when_multi_feature_fails(polar_app, monkeypatch):
    app, polar_client_module, _api_module = polar_app
    client = app.test_client()
    polar_client_module.ensure_polar_schema()
    conn = connections.get_polar_db()
    conn.execute(
        """
        INSERT INTO polar_tokens (polar_user_id, access_token, refresh_token, token_type, expires_at, scope, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        ("user@example.com", "access", "refresh", "bearer", "2099-05-09T10:00:00+00:00", "sleep:read", "2026-05-09T09:00:00+00:00", "2026-05-09T09:00:00+00:00"),
    )
    conn.commit()
    conn.close()

    class StubPolarClient:
        def require_configured(self):
            return None

        def get_saved_token(self):
            return object()

        def get_json(self, path, params=None):
            return {"nightSleeps": [{"sleepDate": "2026-05-12"}]}

        def get_sleeps(self, from_date, to_date, features=None):
            if features is None:
                return {"nightSleeps": [{"sleepDate": "2026-05-12"}]}
            if features == ["sleep-score"]:
                return {"nightSleeps": [{"sleepDate": "2026-05-12", "sleepScore": {"sleepScore": 52.2}}]}
            if features == ["sleep-score", "sleep-result", "sleep-evaluation"]:
                raise polar_client_module.PolarClientError("Polar HTTP 400: Range is too large, maximum length is 1 days")
            raise AssertionError(f"unexpected features: {features}")

    monkeypatch.setattr("integrations.polar_api.create_polar_client", lambda: StubPolarClient())

    resp = client.post("/api/polar/test-sync")

    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True
    assert body["sleep_debug"]["without_features"]["ok"] is True
    assert body["sleep_debug"]["sleep_score"]["ok"] is True
    assert body["sleep_debug"]["sleep_multi_feature"]["ok"] is False
    assert "Range is too large" in body["sleep_debug"]["sleep_multi_feature"]["error"]
    assert body["breathing_debug"]["nightly"]["ok"] is False


def test_test_sync_returns_breathing_debug(polar_app, monkeypatch):
    app, polar_client_module, _api_module = polar_app
    client = app.test_client()
    polar_client_module.ensure_polar_schema()
    conn = connections.get_polar_db()
    conn.execute(
        """
        INSERT INTO polar_tokens (polar_user_id, access_token, refresh_token, token_type, expires_at, scope, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        ("user@example.com", "access", "refresh", "bearer", "2099-05-09T10:00:00+00:00", "sleep:read", "2026-05-09T09:00:00+00:00", "2026-05-09T09:00:00+00:00"),
    )
    conn.commit()
    conn.close()

    class StubPolarClient:
        def require_configured(self):
            return None

        def get_saved_token(self):
            return object()

        def get_json(self, path, params=None):
            return {"nightSleeps": [{"sleepDate": "2026-05-12"}]}

        def get_sleeps(self, from_date, to_date, features=None):
            return {
                "nightSleeps": [{
                    "sleepDate": "2026-05-12",
                    "sleepScore": {"sleepScore": 52.2},
                    "sleepEvaluation": {"respirationRate": 14.2},
                }]
            }

        def get_nightly_recharge(self, from_date, to_date, features=None):
            return {
                "nightlyRechargeResults": [{
                    "sleepResultDate": "2026-05-12",
                    "meanNightlyRecoveryRespirationInterval": 0,
                    "breathingRateSamples": [],
                }]
            }

    monkeypatch.setattr("integrations.polar_api.create_polar_client", lambda: StubPolarClient())

    resp = client.post("/api/polar/test-sync")

    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True
    assert body["breathing_debug"]["debug_day"]
    assert body["breathing_debug"]["nightly"]["ok"] is True
    assert body["breathing_debug"]["nightly"]["computedBreathingRate"] is None
    assert body["breathing_debug"]["nightly"]["breathingRateSamplesLength"] == 0
    assert body["breathing_debug"]["sleep"]["ok"] is True
    assert body["breathing_debug"]["sleep"]["found_paths"]
    assert body["breathing_debug"]["sleep"]["found_paths"][0]["interpreted_breathing_rate"] == 14.2
