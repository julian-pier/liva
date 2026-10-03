from __future__ import annotations

import base64
import asyncio
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import jwt
import pytest
from argon2 import PasswordHasher
from starlette.testclient import TestClient

from liva_mcp.auth_store import AuthStore
from liva_mcp.oauth_provider import PersonalOAuthProvider
from liva_mcp.remote_config import RemoteConfig
from liva_mcp.remote_server import _invoke, create_app, install_log_redaction
from liva_mcp.remote_setup import generate
from liva_mcp.config import Config
from liva_mcp.read_service import ReadService
from liva_mcp.server import McpServer
from liva_mcp.tool_registry import TOOL_BY_NAME

REMOTE_VISIBLE = set(TOOL_BY_NAME) - {"liva_memory_pending", "liva_memory_ingest_file", "liva_memory_commit"}


CALLBACK = "https://chatgpt.com/connector/oauth/test_callback"
PASSPHRASE = "test-personal-passphrase"


@pytest.fixture
def remote_config(repo_root: Path, tmp_path: Path) -> RemoteConfig:
    database_root = tmp_path.parent / f"{tmp_path.name}-database-view"
    database_root.mkdir()
    for source in (repo_root / "database").glob("*.sqlite3"):
        shutil.copy2(source, database_root / source.name)
    return RemoteConfig(
        repo_root=repo_root, database_root=database_root, base_url="https://liva.example:10000", issuer="https://liva.example:10000",
        client_id="test-client", client_secret="s" * 48, signing_secret="k" * 48,
        passphrase_hash=PasswordHasher().hash(PASSPHRASE), auth_db=tmp_path.parent / f"{tmp_path.name}-auth" / "auth.sqlite3",
    )


@pytest.fixture
def client(remote_config: RemoteConfig):
    with TestClient(create_app(remote_config), base_url=remote_config.base_url, follow_redirects=False) as value:
        yield value


def pkce() -> tuple[str, str]:
    verifier = "verified-" + "v" * 50
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return verifier, challenge


def start_authorization(client: TestClient, config: RemoteConfig, **overrides: str):
    verifier, challenge = pkce()
    params = {
        "client_id": config.client_id, "redirect_uri": CALLBACK, "response_type": "code",
        "code_challenge": challenge, "code_challenge_method": "S256", "scope": "liva.read offline_access",
        "resource": config.resource, "state": "expected-state",
    }
    params.update(overrides)
    return client.get("/oauth/authorize", params=params), verifier


def approve(client: TestClient, location: str, passphrase: str = PASSPHRASE, csrf_override: str | None = None) -> str:
    login = client.get(location)
    request_id = re.search(r'name="request" value="([^"]+)', login.text).group(1)
    csrf = re.search(r'name="csrf" value="([^"]+)', login.text).group(1)
    response = client.post("/oauth/login", data={"request": request_id, "csrf": csrf_override or csrf, "passphrase": passphrase})
    if response.status_code != 200:
        return ""
    callback = json.loads(re.search(r'window\.location\.replace\(("[^"]+")\)', response.text).group(1))
    return parse_qs(urlsplit(callback).query)["code"][0]


def test_login_page_is_styled_accessible_and_autofocused(client: TestClient, remote_config: RemoteConfig):
    authorization, _ = start_authorization(client, remote_config)
    login = client.get(authorization.headers["location"])
    assert login.status_code == 200
    assert '<html lang="de">' in login.text
    assert 'id="passphrase"' in login.text
    assert "required autofocus" in login.text
    assert 'type="text" inputmode="numeric"' in login.text
    assert 'autocomplete="off"' in login.text
    assert 'for="passphrase"' in login.text
    assert "style-src 'unsafe-inline'" in login.headers["content-security-policy"]
    assert "x-liva-oauth-login" not in login.headers


def token_exchange(client: TestClient, config: RemoteConfig, code: str, verifier: str, **overrides: str):
    data = {"grant_type": "authorization_code", "client_id": config.client_id, "code": code, "redirect_uri": CALLBACK, "code_verifier": verifier, "resource": config.resource}
    data.update(overrides)
    return client.post("/oauth/token", data=data, auth=(config.client_id, config.client_secret))


def valid_token(client: TestClient, config: RemoteConfig) -> dict[str, str]:
    authorization, verifier = start_authorization(client, config)
    code = approve(client, authorization.headers["location"])
    response = token_exchange(client, config, code, verifier)
    assert response.status_code == 200
    return response.json()


def mcp(client: TestClient, token: str, method: str, params: dict | None = None):
    return client.post("/mcp", headers={"authorization": f"Bearer {token}", "accept": "application/json, text/event-stream", "content-type": "application/json"}, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}})


def without_generated_at(value):
    if isinstance(value, dict):
        return {key: without_generated_at(item) for key, item in value.items() if key != "generated_at"}
    if isinstance(value, list):
        return [without_generated_at(item) for item in value]
    return value


def test_metadata_health_and_anonymous_challenge(client: TestClient, remote_config: RemoteConfig):
    protected = client.get("/.well-known/oauth-protected-resource")
    assert protected.json() == {"resource": remote_config.resource, "authorization_servers": [remote_config.issuer], "scopes_supported": ["liva.read", "liva.write"], "bearer_methods_supported": ["header"]}
    assert client.get("/.well-known/oauth-protected-resource/mcp").json() == protected.json()
    metadata = client.get("/.well-known/oauth-authorization-server").json()
    assert metadata["authorization_endpoint"].endswith("/oauth/authorize")
    assert metadata["token_endpoint"].endswith("/oauth/token")
    assert metadata["code_challenge_methods_supported"] == ["S256"]
    assert "client_credentials" not in metadata["grant_types_supported"]
    assert client.get("/healthz").json() == {"status": "ok", "version": "0.6.0"}
    for path in ("/.well-known/oauth-protected-resource", "/.well-known/oauth-protected-resource/mcp", "/.well-known/oauth-authorization-server"):
        assert client.head(path).status_code == 200
    assert client.options("/.well-known/oauth-authorization-server").status_code == 405
    anonymous = client.post("/mcp", json={})
    assert anonymous.status_code == 401 and "resource_metadata=" in anonymous.headers["www-authenticate"]
    assert 'scope="liva.read liva.write"' in anonymous.headers["www-authenticate"]
    assert "offline_access" not in anonymous.headers["www-authenticate"]


def test_standard_https_origin_dcr_oauth_and_mcp_flow(remote_config: RemoteConfig, tmp_path: Path):
    config = RemoteConfig(
        **{**remote_config.__dict__, "base_url": "https://liva.example", "issuer": "https://liva.example",
           "auth_db": tmp_path.parent / f"{tmp_path.name}-standard-origin-auth" / "auth.sqlite3"}
    )
    with TestClient(create_app(config), base_url=config.base_url, follow_redirects=False) as client:
        metadata = client.get("/.well-known/oauth-authorization-server").json()
        assert metadata["issuer"] == "https://liva.example"
        assert metadata["registration_endpoint"] == "https://liva.example/oauth/register"
        assert client.get("/.well-known/oauth-protected-resource").json()["resource"] == "https://liva.example/mcp"
        assert client.post("/mcp", json={}, headers={"host": "liva.example:10000"}).status_code == 401
        registration = client.post("/oauth/register", json={
            "redirect_uris": [CALLBACK], "scope": "liva.read", "grant_types": ["authorization_code"],
            "response_types": ["code"], "token_endpoint_auth_method": "client_secret_basic",
        })
        assert registration.status_code == 201
        registered = registration.json()
        authorization, verifier = start_authorization(client, config, client_id=registered["client_id"], scope="liva.read")
        assert authorization.status_code == 302
        code = approve(client, authorization.headers["location"])
        token = client.post("/oauth/token", data={
            "grant_type": "authorization_code", "client_id": registered["client_id"], "code": code,
            "redirect_uri": CALLBACK, "code_verifier": verifier, "resource": config.resource,
        }, auth=(registered["client_id"], registered["client_secret"]))
        assert token.status_code == 200
        tools = mcp(client, token.json()["access_token"], "tools/list").json()["result"]["tools"]
        assert {tool["name"] for tool in tools} == REMOTE_VISIBLE


def test_full_oauth_remote_initialize_and_exact_tools(client: TestClient, remote_config: RemoteConfig):
    tokens = valid_token(client, remote_config)
    initialized = mcp(client, tokens["access_token"], "initialize", {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}})
    assert initialized.status_code == 200
    listed = mcp(client, tokens["access_token"], "tools/list").json()["result"]["tools"]
    assert {tool["name"] for tool in listed} == REMOTE_VISIBLE
    assert len(listed) == 26
    for tool in listed:
        if tool["name"].startswith("liva_post_") or tool["name"].startswith("liva_upsert_") or tool["name"].startswith("liva_operator_") or tool["name"] in {"liva_coach_act", "liva_training_decision_write", "liva_memory_begin", "liva_memory_finish", "liva_memory_import_file"}:
            assert tool["annotations"]["readOnlyHint"] is False
            assert tool["_meta"]["securitySchemes"] == [{"type": "oauth2", "scopes": ["liva.write"]}]
        else:
            assert tool["annotations"]["readOnlyHint"] is True
            assert tool["_meta"]["securitySchemes"] == [{"type": "oauth2", "scopes": ["liva.read"]}]
    remote_result = mcp(client, tokens["access_token"], "tools/call", {"name": "liva_daily_snapshot", "arguments": {}}).json()["result"]["structuredContent"]
    stdio_result = McpServer(ReadService(Config(repo_root=remote_config.repo_root))).handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "liva_daily_snapshot", "arguments": {}}})
    stdio_payload = json.loads(stdio_result["result"]["content"][0]["text"])
    assert without_generated_at(remote_result) == without_generated_at(stdio_payload)


def test_remote_memory_audit_is_public_read_only_and_bounded(client: TestClient, remote_config: RemoteConfig):
    tokens = valid_token(client, remote_config)
    response = mcp(client, tokens["access_token"], "tools/call", {"name": "liva_memory_audit", "arguments": {"scope": "vault", "max_issues": 1}})
    assert response.status_code == 200
    payload = response.json()["result"]["structuredContent"]
    assert payload["ok"] is True
    data = payload["data"]
    assert data["scope"] == "vault" and data["issues_returned"] <= 1
    assert data["total_issues"] >= data["issues_returned"]


def test_remote_live_daily_note_with_write_scope_is_json_serializable(client: TestClient, remote_config: RemoteConfig):
    authorization, verifier = start_authorization(client, remote_config, scope="liva.read liva.write")
    code = approve(client, authorization.headers["location"])
    token = token_exchange(client, remote_config, code, verifier)
    assert token.status_code == 200
    response = mcp(client, token.json()["access_token"], "tools/call", {"name":"liva_post_daily_note", "arguments":{"category":"system", "text":"remote live write", "dry_run":False, "reason":"test"}})
    assert response.status_code == 200
    payload = response.json()["result"]["structuredContent"]
    assert payload["ok"] is True
    assert payload["data"]["status"] == "success"
    assert payload["data"]["audit_id"] > 0
    json.dumps(payload)


def test_smoke_claim_is_signed_and_only_issued_to_configured_client(remote_config: RemoteConfig):
    config = remote_config.__class__(**{**remote_config.__dict__, "smoke_test_client_id": "smoke-client"})
    provider = PersonalOAuthProvider(config, AuthStore(config.auth_db))
    smoke_token = provider._issue("smoke-client", ["liva.read", "liva.write"], config.resource)
    normal_token = provider._issue("test-client", ["liva.read", "liva.write"], config.resource)
    smoke_access = asyncio.run(provider.load_access_token(smoke_token))
    normal_access = asyncio.run(provider.load_access_token(normal_token))
    assert smoke_access and smoke_access.claims["liva_smoke_test"] is True
    assert normal_access and normal_access.claims["liva_smoke_test"] is False


def test_code_is_one_time_and_wrong_verifier_fails(client: TestClient, remote_config: RemoteConfig):
    authorization, verifier = start_authorization(client, remote_config)
    code = approve(client, authorization.headers["location"])
    assert token_exchange(client, remote_config, code, "wrong-" + verifier).status_code == 400
    assert token_exchange(client, remote_config, code, verifier).status_code == 200
    assert token_exchange(client, remote_config, code, verifier).status_code == 400


@pytest.mark.parametrize("override", [
    {"redirect_uri": "https://chatgpt.com/connector/oauth/other"},
    {"resource": "https://other.example/mcp"},
])
def test_code_exchange_remains_bound(client: TestClient, remote_config: RemoteConfig, override: dict[str, str]):
    authorization, verifier = start_authorization(client, remote_config)
    code = approve(client, authorization.headers["location"])
    assert token_exchange(client, remote_config, code, verifier, **override).status_code == 400


def test_code_exchange_uses_code_bound_resource_when_request_omits_it(client: TestClient, remote_config: RemoteConfig):
    authorization, verifier = start_authorization(client, remote_config)
    code = approve(client, authorization.headers["location"])
    response = token_exchange(client, remote_config, code, verifier, resource="")
    assert response.status_code == 200
    assert mcp(client, response.json()["access_token"], "tools/list").status_code == 200


def test_token_resource_mismatch_is_audited_without_code_contents(client: TestClient, remote_config: RemoteConfig, caplog):
    with caplog.at_level("INFO", logger="liva_mcp.audit"):
        response = client.post("/oauth/token", data={"grant_type": "authorization_code", "client_id": remote_config.client_id, "code": "never-log-this-code", "code_verifier": "never-log-this-verifier", "resource": "https://other.example/mcp"}, auth=(remote_config.client_id, remote_config.client_secret))
    assert response.status_code == 400
    assert "oauth_error=resource_mismatch" in caplog.text
    assert "never-log-this-code" not in caplog.text
    assert "never-log-this-verifier" not in caplog.text


def test_login_redirect_is_bound_and_double_submit_is_safe(client: TestClient, remote_config: RemoteConfig):
    authorization, verifier = start_authorization(client, remote_config)
    login = client.get(authorization.headers["location"])
    request_id = re.search(r'name="request" value="([^"]+)', login.text).group(1)
    csrf = re.search(r'name="csrf" value="([^"]+)', login.text).group(1)
    form = {"request": request_id, "csrf": csrf, "passphrase": PASSPHRASE}
    first = client.post("/oauth/login", data=form)
    assert first.status_code == 200
    assert "window.location.replace" in first.text
    redirected = urlsplit(json.loads(re.search(r'window\.location\.replace\(("[^"]+")\)', first.text).group(1)))
    assert redirected.scheme == "https" and redirected.hostname == "chatgpt.com"
    query = parse_qs(redirected.query)
    assert query["state"] == ["expected-state"] and len(query["code"]) == 1
    assert "script-src 'unsafe-inline'" in first.headers["content-security-policy"]
    second = client.post("/oauth/login", data=form)
    assert second.status_code == 200
    assert second.text == first.text
    assert second.headers["x-liva-oauth-error"] == "duplicate_handoff_replayed"
    for _ in range(12):
        replay = client.post("/oauth/login", data=form)
        assert replay.status_code == 200
        assert replay.headers["x-liva-oauth-error"] == "duplicate_handoff_replayed"
    code = query["code"][0]
    assert token_exchange(client, remote_config, code, verifier).status_code == 200
    assert token_exchange(client, remote_config, code, verifier).status_code == 400
    consumed = client.post("/oauth/login", data=form)
    assert consumed.status_code == 200
    assert consumed.headers["x-liva-oauth-error"] == "already_completed_consumed"


def test_expired_code_is_rejected(remote_config: RemoteConfig):
    expired = RemoteConfig(**{**remote_config.__dict__, "authorization_code_seconds": -1})
    with TestClient(create_app(expired), base_url=expired.base_url, follow_redirects=False) as client:
        authorization, verifier = start_authorization(client, expired)
        code = approve(client, authorization.headers["location"])
        assert token_exchange(client, expired, code, verifier).status_code == 400


@pytest.mark.parametrize("overrides", [
    {"code_challenge_method": "plain"}, {"redirect_uri": "https://evil.example/callback"},
    {"resource": "https://other.example/mcp"}, {"scope": "liva.write"},
])
def test_authorization_rejects_unsafe_parameters(client: TestClient, remote_config: RemoteConfig, overrides: dict[str, str]):
    response, _ = start_authorization(client, remote_config, **overrides)
    assert response.status_code in {302, 400}
    assert "/oauth/login" not in response.headers.get("location", "")


def test_client_credentials_and_csrf_are_required(client: TestClient, remote_config: RemoteConfig):
    authorization, verifier = start_authorization(client, remote_config)
    assert approve(client, authorization.headers["location"], csrf_override="wrong") == ""
    authorization, verifier = start_authorization(client, remote_config)
    code = approve(client, authorization.headers["location"])
    response = client.post("/oauth/token", data={"grant_type": "authorization_code", "client_id": remote_config.client_id, "code": code, "redirect_uri": CALLBACK, "code_verifier": verifier, "resource": remote_config.resource}, auth=(remote_config.client_id, "wrong-secret"))
    assert response.status_code == 401


def test_login_audit_marks_csrf_failure_without_secrets(client: TestClient, remote_config: RemoteConfig, caplog):
    authorization, _ = start_authorization(client, remote_config)
    login = client.get(authorization.headers["location"])
    request_id = re.search(r'name="request" value="([^"]+)', login.text).group(1)
    with caplog.at_level("INFO", logger="liva_mcp.audit"):
        response = client.post("/oauth/login", data={"request": request_id, "csrf": "wrong", "passphrase": "never-log-this"})
    assert response.status_code == 400
    assert "oauth_error=csrf_mismatch" in caplog.text
    assert "never-log-this" not in caplog.text


def test_wrong_passphrase_is_generic(client: TestClient, remote_config: RemoteConfig):
    authorization, _ = start_authorization(client, remote_config)
    location = authorization.headers["location"]
    login = client.get(location)
    request_id = re.search(r'name="request" value="([^"]+)', login.text).group(1)
    csrf = re.search(r'name="csrf" value="([^"]+)', login.text).group(1)
    response = client.post("/oauth/login", data={"request": request_id, "csrf": csrf, "passphrase": "wrong"})
    assert response.status_code == 403 and response.text == "Authorization failed"


def test_invalid_wrong_audience_scope_and_expired_tokens(client: TestClient, remote_config: RemoteConfig):
    assert client.post("/mcp", headers={"authorization": "Bearer invalid"}, json={}).status_code == 401
    store = AuthStore(remote_config.auth_db); provider = PersonalOAuthProvider(remote_config, store)
    wrong_audience = provider._issue(remote_config.client_id, ["liva.read"], "https://other.example/mcp")
    assert mcp(client, wrong_audience, "tools/list").status_code == 401
    wrong_scope = provider._issue(remote_config.client_id, ["offline_access"], remote_config.resource)
    assert mcp(client, wrong_scope, "tools/list").status_code == 403
    now = int(time.time()); raw = jwt.encode({"iss": remote_config.issuer, "aud": remote_config.resource, "sub": "liva-owner", "client_id": remote_config.client_id, "scope": "liva.read", "iat": now - 100, "nbf": now - 100, "exp": now - 1, "jti": "expired"}, remote_config.signing_secret, algorithm="HS256")
    store.put_access(raw, jti="expired", client_id=remote_config.client_id, resource=remote_config.resource, scopes=["liva.read"], expires_at=now - 1, family_id=None)
    assert mcp(client, raw, "tools/list").status_code == 401


def test_refresh_rotation_and_revocation(client: TestClient, remote_config: RemoteConfig):
    tokens = valid_token(client, remote_config)
    refreshed = client.post("/oauth/token", data={"grant_type": "refresh_token", "client_id": remote_config.client_id, "refresh_token": tokens["refresh_token"], "scope": "liva.read offline_access", "resource": remote_config.resource}, auth=(remote_config.client_id, remote_config.client_secret))
    assert refreshed.status_code == 200 and refreshed.json()["refresh_token"] != tokens["refresh_token"]
    replay = client.post("/oauth/token", data={"grant_type": "refresh_token", "client_id": remote_config.client_id, "refresh_token": tokens["refresh_token"], "resource": remote_config.resource}, auth=(remote_config.client_id, remote_config.client_secret))
    assert replay.status_code == 400
    revoked = client.post("/oauth/revoke", data={"token": refreshed.json()["refresh_token"], "token_type_hint": "refresh_token"}, auth=(remote_config.client_id, remote_config.client_secret))
    assert revoked.status_code == 200
    assert mcp(client, refreshed.json()["access_token"], "tools/list").status_code == 401


def test_access_token_without_refresh_is_revocable(client: TestClient, remote_config: RemoteConfig):
    authorization, verifier = start_authorization(client, remote_config, scope="liva.read")
    code = approve(client, authorization.headers["location"])
    token = token_exchange(client, remote_config, code, verifier).json()
    assert "refresh_token" not in token
    assert client.post("/oauth/revoke", data={"token": token["access_token"]}, auth=(remote_config.client_id, remote_config.client_secret)).status_code == 200
    assert mcp(client, token["access_token"], "tools/list").status_code == 401


def test_host_methods_paths_body_limit_and_rate_limit(client: TestClient):
    assert client.get("/healthz", headers={"host": "evil.example"}).status_code == 400
    assert client.post("/healthz").status_code == 405
    assert client.get("/unknown").status_code == 404
    assert client.post("/oauth/token", content=b"x" * 65_537, headers={"content-type": "application/x-www-form-urlencoded"}).status_code == 413
    client.app.global_limiter.limit = 1
    assert client.get("/healthz").status_code == 200
    assert client.get("/healthz").status_code == 429


def test_security_headers_no_cors_and_minimal_errors(client: TestClient):
    response = client.get("/healthz")
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "access-control-allow-origin" not in response.headers
    error = client.post("/mcp", headers={"authorization": "Bearer invalid"}, json={})
    assert "traceback" not in error.text.lower() and "/private/" not in error.text


def test_stdio_entrypoint_still_lists_same_tools(repo_root: Path):
    request = '\n'.join((json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}), json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}))) + '\n'
    completed = subprocess.run([sys.executable, "-m", "liva_mcp.server"], input=request, text=True, capture_output=True, check=True, env={"PYTHONPATH": os.pathsep.join((str(Path(__file__).parents[2]), str(Path(__file__).parents[1] / "src"))), "LIVA_MCP_REPO_ROOT": str(repo_root)})
    messages = [json.loads(line) for line in completed.stdout.splitlines()]
    assert "Before every answer" in messages[0]["result"]["instructions"]
    assert "liva_context_snapshot" in messages[0]["result"]["instructions"]
    assert {tool["name"] for tool in messages[1]["result"]["tools"]} == REMOTE_VISIBLE


def test_remote_config_never_accepts_auth_db_inside_repo(repo_root: Path):
    with pytest.raises(ValueError):
        RemoteConfig(repo_root=repo_root, database_root=repo_root / "database", base_url="https://liva.example", issuer="https://liva.example", client_id="x", client_secret="s" * 48, signing_secret="k" * 48, passphrase_hash="hash", auth_db=repo_root / "auth.sqlite3")


def test_remote_response_limit_and_log_redaction(remote_config: RemoteConfig, monkeypatch, caplog):
    monkeypatch.setattr("liva_mcp.remote_server.call_tool", lambda *args, **kwargs: {"ok": True, "data": "x" * 262_144})
    assert _invoke(object(), "liva_daily_snapshot", {})["error"]["code"] == "response_too_large"
    install_log_redaction(remote_config)
    with caplog.at_level("ERROR"):
        __import__("logging").getLogger("phase3-test").error("secret=%s", remote_config.client_secret)
    assert remote_config.client_secret not in caplog.text and "[REDACTED]" in caplog.text


def test_secret_setup_permissions_and_refuses_overwrite(tmp_path: Path):
    config_dir = tmp_path / "config"; data_dir = tmp_path / "data"; repo = tmp_path / "repo"
    generate(config_dir, data_dir, repo, "https://liva.example:10000")
    assert config_dir.stat().st_mode & 0o777 == 0o700
    assert (config_dir / "env").stat().st_mode & 0o777 == 0o600
    assert (config_dir / "chatgpt-setup.txt").stat().st_mode & 0o777 == 0o600
    assert len(list((data_dir / "database-view").glob("*.sqlite3"))) == 7
    assert all(path.stat().st_mode & 0o777 == 0o400 for path in (data_dir / "database-view").glob("*.sqlite3"))
    assert "AI_WRITE_KEY" not in (config_dir / "env").read_text()
    with pytest.raises(FileExistsError):
        generate(config_dir, data_dir, repo, "https://liva.example:10000")
