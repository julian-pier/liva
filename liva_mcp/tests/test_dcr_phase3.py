from __future__ import annotations

import base64
import hashlib
import json
import re
import shutil
import sqlite3
from urllib.parse import parse_qs, urlsplit

import pytest
from argon2 import PasswordHasher
from mcp.shared.auth import OAuthClientInformationFull, OAuthClientMetadata
from pydantic import AnyUrl
from starlette.testclient import TestClient

from liva_mcp.auth_store import AuthStore
from liva_mcp.dcr import MAX_ACTIVE_DYNAMIC_CLIENTS
from liva_mcp.oauth_provider import CLAUDE_CALLBACK, PersonalOAuthProvider
from liva_mcp.remote_config import RemoteConfig
from liva_mcp.remote_server import create_app


CALLBACK = "https://chatgpt.com/connector/oauth/dcr_test"
LEGACY_CALLBACK = "https://chatgpt.com/connector_platform_oauth_redirect"

EXPECTED_PUBLIC_TOOLS = {
    "liva_context_snapshot",
    "liva_daily_snapshot",
    "liva_training_state",
    "liva_training_plan",
    "liva_recovery_snapshot",
    "liva_nutrition_snapshot",
    "liva_weight_state",
    "liva_runs_state",
    "liva_coach_read",
    "liva_coach_act",
    "liva_memory_recall",
    "liva_memory_context",
    "liva_memory_audit",
    "liva_memory_begin",
    "liva_memory_finish",
    "liva_memory_source",
    "liva_memory_import_file",
    "liva_post_daily_note",
    "liva_upsert_weight",
    "liva_post_nutrition_context",
    "liva_post_daily_flag",
    "liva_post_training_rawlog",
    "liva_training_decision_plan",
    "liva_training_decision_write",
    "liva_operator_plan",
    "liva_operator_execute",
}
READ_ONLY_TOOLS = EXPECTED_PUBLIC_TOOLS - {
    "liva_coach_act",
    "liva_post_daily_note",
    "liva_upsert_weight",
    "liva_post_nutrition_context",
    "liva_post_daily_flag",
    "liva_post_training_rawlog",
    "liva_training_decision_write",
    "liva_operator_plan",
    "liva_operator_execute",
    "liva_memory_begin",
    "liva_memory_finish",
    "liva_memory_import_file",
}


def assert_public_tool_contract(response):
    tools = response.json()["result"]["tools"]
    assert len(tools) == len(EXPECTED_PUBLIC_TOOLS) == 26
    by_name = {tool["name"]: tool for tool in tools}
    assert set(by_name) == EXPECTED_PUBLIC_TOOLS
    for name, tool in by_name.items():
        expected_scope = "liva.read" if name in READ_ONLY_TOOLS else "liva.write"
        assert tool["_meta"]["securitySchemes"] == [{"type": "oauth2", "scopes": [expected_scope]}]
        assert tool["annotations"]["readOnlyHint"] is (name in READ_ONLY_TOOLS)
    context = by_name["liva_context_snapshot"]
    assert context["inputSchema"]["properties"] == {}
    assert context["annotations"]["readOnlyHint"] is True


@pytest.fixture
def remote_config(repo_root, tmp_path):
    database_root = tmp_path.parent / f"{tmp_path.name}-dcr-database-view"
    database_root.mkdir()
    for source in (repo_root / "database").glob("*.sqlite3"):
        shutil.copy2(source, database_root / source.name)
    return RemoteConfig(
        repo_root=repo_root, database_root=database_root,
        base_url="https://liva.example:10000", issuer="https://liva.example:10000",
        client_id="fixed-client", client_secret="s" * 48, signing_secret="k" * 48,
        passphrase_hash=PasswordHasher().hash("test-personal-passphrase"),
        auth_db=tmp_path.parent / f"{tmp_path.name}-dcr-auth" / "auth.sqlite3",
    )


@pytest.fixture
def client(remote_config):
    with TestClient(create_app(remote_config), base_url=remote_config.base_url, follow_redirects=False) as value:
        yield value


def registration_payload(**overrides):
    payload = {
        "redirect_uris": [CALLBACK],
        "scope": "liva.read offline_access",
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "client_secret_basic",
        "client_name": "ChatGPT DCR test",
    }
    payload.update(overrides)
    return payload


def register(client, **overrides):
    return client.post("/oauth/register", json=registration_payload(**overrides), headers={"user-agent": "ChatGPT-Test/1"})


def pkce():
    verifier = "dcr-verified-" + "v" * 50
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return verifier, challenge


def database_hashes(root):
    return {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in sorted(root.glob("*.sqlite3"))}


def authorize(client, config, client_id, callback, scope="liva.read offline_access", **overrides):
    verifier, challenge = pkce()
    params = {
        "client_id": client_id, "redirect_uri": callback, "response_type": "code",
        "code_challenge": challenge, "code_challenge_method": "S256", "scope": scope,
        "resource": config.resource, "state": "dcr-state",
    }
    params.update(overrides)
    return client.get("/oauth/authorize", params=params), verifier


def approve(client, location, passphrase="test-personal-passphrase"):
    login = client.get(location)
    request_id = re.search(r'name="request" value="([^"]+)', login.text).group(1)
    csrf = re.search(r'name="csrf" value="([^"]+)', login.text).group(1)
    response = client.post("/oauth/login", data={"request": request_id, "csrf": csrf, "passphrase": passphrase})
    callback = json.loads(re.search(r'window\.location\.replace\(("[^"]+")\)', response.text).group(1))
    return parse_qs(urlsplit(callback).query)["code"][0]


def exchange(client, config, client_id, client_secret, code, verifier, callback=CALLBACK, **overrides):
    data = {
        "grant_type": "authorization_code", "client_id": client_id, "code": code,
        "redirect_uri": callback, "code_verifier": verifier, "resource": config.resource,
    }
    data.update(overrides)
    return client.post("/oauth/token", data=data, auth=(client_id, client_secret))


def test_metadata_advertises_registration_endpoint(client, remote_config):
    metadata = client.get("/.well-known/oauth-authorization-server").json()
    assert metadata["registration_endpoint"] == f"{remote_config.base_url}/oauth/register"


def test_official_mcp_client_registration_shape_is_accepted(client):
    metadata = OAuthClientMetadata(
        redirect_uris=[AnyUrl(CALLBACK)], scope="liva.read offline_access",
        token_endpoint_auth_method="client_secret_basic", client_name="ChatGPT",
    )
    response = client.post("/oauth/register", json=metadata.model_dump(mode="json", exclude_none=True))
    assert response.status_code == 201
    parsed = OAuthClientInformationFull.model_validate(response.json())
    assert parsed.client_id and parsed.client_secret


@pytest.mark.parametrize("scope", [None, "", "   "])
def test_missing_or_empty_scope_defaults_to_read_only(client, scope):
    payload = registration_payload()
    payload["grant_types"] = ["authorization_code"]
    if scope is None:
        del payload["scope"]
    else:
        payload["scope"] = scope
    response = client.post("/oauth/register", json=payload)
    assert response.status_code == 201
    assert response.json()["scope"] == "liva.read"


@pytest.mark.parametrize(("scope", "expected_scope"), [
    (None, "liva.read offline_access"),
    ("liva.read", "liva.read offline_access"),
    ("offline_access", "liva.read offline_access"),
])
def test_refresh_grant_normalizes_to_read_and_offline_access(client, scope, expected_scope):
    payload = registration_payload(grant_types=["authorization_code", "refresh_token"])
    if scope is None:
        del payload["scope"]
    else:
        payload["scope"] = scope
    response = client.post("/oauth/register", json=payload)
    assert response.status_code == 201
    assert response.json()["scope"] == expected_scope


def test_normalized_refresh_scope_client_receives_refresh_token(client, remote_config):
    payload = registration_payload()
    del payload["scope"]
    registered = client.post("/oauth/register", json=payload).json()
    authorization, verifier = authorize(client, remote_config, registered["client_id"], CALLBACK)
    code = approve(client, authorization.headers["location"])
    token = exchange(client, remote_config, registered["client_id"], registered["client_secret"], code, verifier)
    assert token.status_code == 200
    assert "refresh_token" in token.json()


def test_chatgpt_scope_omission_still_creates_durable_session(client, remote_config):
    registered = register(client, scope="liva.read liva.write").json()
    authorization, verifier = authorize(
        client, remote_config, registered["client_id"], CALLBACK,
        scope="liva.read liva.write",
    )
    code = approve(client, authorization.headers["location"])
    token = exchange(
        client, remote_config, registered["client_id"], registered["client_secret"],
        code, verifier,
    )
    assert token.status_code == 200
    assert token.json()["scope"] == "liva.read liva.write offline_access"
    assert token.json()["refresh_token"]


def test_dynamic_client_secret_post_exchange_is_accepted_without_leaking_secret(client, remote_config, caplog):
    registered = register(client).json()
    authorization, verifier = authorize(client, remote_config, registered["client_id"], CALLBACK)
    code = approve(client, authorization.headers["location"])
    with caplog.at_level("INFO", logger="liva_mcp.audit"):
        response = client.post("/oauth/token", data={
            "grant_type": "authorization_code", "client_id": registered["client_id"], "client_secret": registered["client_secret"],
            "code": code, "redirect_uri": CALLBACK, "code_verifier": verifier, "resource": remote_config.resource,
    })
    assert response.status_code == 200
    assert "auth_method=post" in caplog.text and "secret_valid=yes" in caplog.text
    assert "sdk_auth=not_used" in caplog.text and "token_handler=custom" in caplog.text and "token_issued=yes" in caplog.text
    assert registered["client_secret"] not in caplog.text


def test_prevalidated_dynamic_basic_client_uses_custom_token_handler(client, remote_config, caplog):
    registered = register(client).json()
    authorization, verifier = authorize(client, remote_config, registered["client_id"], CALLBACK)
    code = approve(client, authorization.headers["location"])
    with caplog.at_level("INFO", logger="liva_mcp.audit"):
        response = exchange(client, remote_config, registered["client_id"], registered["client_secret"], code, verifier)
    assert response.status_code == 200
    assert "auth_method=basic" in caplog.text and "secret_valid=yes" in caplog.text
    assert "token_handler=custom" in caplog.text and "token_issued=yes" in caplog.text


def test_dynamic_token_exchange_rejects_missing_secret_and_wrong_client(client, remote_config):
    registered = register(client).json()
    other = register(client).json()
    authorization, verifier = authorize(client, remote_config, registered["client_id"], CALLBACK)
    code = approve(client, authorization.headers["location"])
    data = {
        "grant_type": "authorization_code", "client_id": registered["client_id"], "code": code,
        "redirect_uri": CALLBACK, "code_verifier": verifier, "resource": remote_config.resource,
    }
    assert client.post("/oauth/token", data=data).status_code == 401
    data["client_id"] = other["client_id"]
    assert client.post("/oauth/token", data=data, auth=(other["client_id"], other["client_secret"])).status_code == 400


def test_dynamic_token_exchange_requires_pkce_and_uses_code_bound_defaults(client, remote_config):
    registered = register(client).json()
    authorization, verifier = authorize(client, remote_config, registered["client_id"], CALLBACK)
    code = approve(client, authorization.headers["location"])
    assert exchange(
        client, remote_config, registered["client_id"], registered["client_secret"], code, verifier,
        code_verifier="",
    ).status_code == 400
    response = exchange(
        client, remote_config, registered["client_id"], registered["client_secret"], code, verifier,
        callback="", resource="",
    )
    assert response.status_code == 200


def test_default_scope_client_completes_code_flow_and_lists_read_tools(client, remote_config):
    payload = registration_payload()
    payload["grant_types"] = ["authorization_code"]
    del payload["scope"]
    registered = client.post("/oauth/register", json=payload).json()
    authorization, verifier = authorize(client, remote_config, registered["client_id"], CALLBACK, scope="liva.read")
    code = approve(client, authorization.headers["location"])
    token = exchange(client, remote_config, registered["client_id"], registered["client_secret"], code, verifier)
    assert token.status_code == 200
    tools = client.post("/mcp", headers={"authorization": f"Bearer {token.json()['access_token']}", "accept": "application/json, text/event-stream", "content-type": "application/json"}, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
    assert tools.status_code == 200
    assert_public_tool_contract(tools)


def test_dynamic_client_accepts_explicit_write_scope(client):
    payload = registration_payload()
    payload["scope"] = "liva.read liva.write"
    response = client.post("/oauth/register", json=payload)
    assert response.status_code == 201
    assert response.json()["scope"] == "liva.read liva.write offline_access"


@pytest.mark.parametrize("scope", ["liva.read liva.write", "liva.write liva.read", "liva.read,liva.write", "liva.write,liva.read", "liva.read liva.write liva.read"])
def test_dcr_normalizes_write_scope_order_separators_and_duplicates(client, scope):
    response = register(client, scope=scope, grant_types=["authorization_code"])
    assert response.status_code == 201
    assert response.json()["scope"] == "liva.read liva.write"


@pytest.mark.parametrize("scope", ["liva.read liva.write", "liva.write liva.read", "liva.read,liva.write", "liva.write,liva.read"])
def test_old_read_only_dynamic_client_upgrades_and_exchanges_write_scope(client, remote_config, scope, caplog):
    registered = register(client, scope="liva.read", grant_types=["authorization_code"]).json()
    with caplog.at_level("INFO", logger="liva_mcp.audit"):
        authorization, verifier = authorize(client, remote_config, registered["client_id"], CALLBACK, scope=scope)
        assert authorization.status_code == 302
        assert authorization.headers["location"].startswith("https://liva.example:10000/oauth/login?")
        code = approve(client, authorization.headers["location"])
        token = exchange(client, remote_config, registered["client_id"], registered["client_secret"], code, verifier)
    assert token.status_code == 200
    assert token.json()["scope"] == "liva.read liva.write"
    with sqlite3.connect(remote_config.auth_db) as connection:
        scope_after = connection.execute("SELECT scope FROM dynamic_clients WHERE client_id=?", (registered["client_id"],)).fetchone()[0]
    assert scope_after == "liva.read liva.write"
    assert "client_scope_upgraded=yes" in caplog.text
    assert "requested_scopes_normalized=liva.read,liva.write" in caplog.text
    assert registered["client_secret"] not in caplog.text


def test_standard_descriptive_registration_metadata_is_accepted_and_not_reflected(client):
    response = register(
        client,
        client_uri="https://chatgpt.com/apps/liva",
        logo_uri="https://chatgpt.com/assets/liva.png",
        contacts=["security@example.invalid"],
        tos_uri="https://chatgpt.com/terms",
        policy_uri="https://chatgpt.com/privacy",
        software_id="chatgpt",
        software_version="2026.07",
    )
    assert response.status_code == 201
    assert response.json()["client_name"] == "ChatGPT DCR test"
    assert not ({"client_uri", "logo_uri", "contacts", "tos_uri", "policy_uri", "software_id", "software_version"} & set(response.json()))


@pytest.mark.parametrize("redirect", [CALLBACK, LEGACY_CALLBACK])
def test_allowed_chatgpt_redirects_register(client, redirect):
    response = register(client, redirect_uris=[redirect])
    assert response.status_code == 201
    assert response.json()["redirect_uris"] == [redirect]


@pytest.mark.parametrize("redirect", [
    CLAUDE_CALLBACK,
])
def test_allowed_claude_redirects_register(client, redirect):
    response = register(client, redirect_uris=[redirect], token_endpoint_auth_method="none")
    assert response.status_code == 201
    assert response.json()["redirect_uris"] == [redirect]


def test_dynamic_public_client_with_none_auth_method_works(client, remote_config):
    payload = registration_payload(
        redirect_uris=[CLAUDE_CALLBACK],
        scope="liva.read",
        grant_types=["authorization_code"],
        response_types=["code"],
        token_endpoint_auth_method="none",
        client_name="Claude Free DCR test",
    )
    registered = client.post("/oauth/register", json=payload).json()
    authorization, verifier = authorize(
        client, remote_config, registered["client_id"], CLAUDE_CALLBACK,
        scope="liva.read",
    )
    assert authorization.status_code == 302
    code = approve(client, authorization.headers["location"])
    token = client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "client_id": registered["client_id"],
            "code": code,
            "redirect_uri": CLAUDE_CALLBACK,
            "code_verifier": verifier,
            "resource": remote_config.resource,
        },
    )
    assert token.status_code == 200
    assert token.json()["token_type"] == "Bearer"


@pytest.mark.parametrize("redirect", [
    "https://evil.example/connector/oauth/test",
    "http://chatgpt.com/connector/oauth/test",
    "https://chatgpt.com/connector/oauth/test?next=evil",
    "https://chatgpt.com/connector/oauth/test#fragment",
    "https://chatgpt.com.evil.example/connector/oauth/test",
    "https://chatgpt.com@evil.example/connector/oauth/test",
    "https://chatgpt.com%2Fevil.example/connector/oauth/test",
    "https://chatgpt.com/connector/oauth/%2e%2e",
    "https://claude.ai/api/mcp/auth_callback/extra",
    "https://claude.ai/api/mcp/oauth/callback",
    "https://claude.ai/api/connector/oauth/callback",
    "http://claude.ai/api/mcp/auth_callback",
])
def test_unsafe_redirects_are_rejected(client, redirect):
    response = register(client, redirect_uris=[redirect])
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_redirect_uri"


@pytest.mark.parametrize(("overrides", "error"), [
    ({"grant_types": ["client_credentials"]}, "invalid_client_metadata"),
    ({"response_types": ["token"]}, "invalid_client_metadata"),
    ({"token_endpoint_auth_method": "client_secret_post"}, "invalid_client_metadata"),
])
def test_invalid_client_metadata_is_rejected(client, overrides, error):
    response = register(client, **overrides)
    assert response.status_code == 400
    assert response.json()["error"] == error


def test_claude_registration_shape_ignores_unknown_metadata(client, caplog):
    payload = registration_payload(
        redirect_uris=[CLAUDE_CALLBACK],
        scope="liva.read liva.write offline_access",
        token_endpoint_auth_method="none",
        resource="https://liva.example/mcp",
        unknown_claude_extension="ignored",
    )
    with caplog.at_level("INFO", logger="liva_mcp.audit"):
        response = client.post("/oauth/register", json=payload, headers={"user-agent": "python-http/diagnostic"})
    assert response.status_code == 201
    assert "ignored_fields=resource,unknown_claude_extension" in caplog.text
    assert "redirect_present=yes" in caplog.text
    assert "redirect_count=1" in caplog.text
    assert "dcr_auth_method=none" in caplog.text
    assert "dcr_response_type=code" in caplog.text
    assert "dcr_grant_types=authorization_code+refresh_token" in caplog.text
    assert "ua=python-http" in caplog.text
    assert "unknown_claude_extension=ignored" not in caplog.text


def test_known_dcr_scope_value_is_still_strictly_validated(client):
    response = register(client, scope="liva.read unknown_scope")
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_client_metadata"


def test_secret_is_returned_once_and_only_hash_is_stored(client, remote_config):
    response = register(client)
    assert response.status_code == 201
    registered = response.json()
    assert len(registered["client_secret"].encode()) >= 32
    assert registered["client_secret_expires_at"] == 0
    with sqlite3.connect(remote_config.auth_db) as connection:
        row = connection.execute("SELECT secret_hash FROM dynamic_clients WHERE client_id=?", (registered["client_id"],)).fetchone()
    assert row and row[0].startswith("$argon2")
    assert row[0] != registered["client_secret"]
    assert registered["client_secret"].encode() not in remote_config.auth_db.read_bytes()
    provider = PersonalOAuthProvider(remote_config, AuthStore(remote_config.auth_db))
    loaded = __import__("asyncio").run(provider.get_client(registered["client_id"]))
    assert loaded.client_secret != registered["client_secret"]
    assert client.get("/oauth/register").status_code == 405


def test_dynamic_client_full_pkce_refresh_and_revocation_flow(client, remote_config):
    registered = register(client).json()
    authorization, verifier = authorize(client, remote_config, registered["client_id"], CALLBACK)
    assert authorization.status_code == 302
    code = approve(client, authorization.headers["location"])
    assert exchange(client, remote_config, registered["client_id"], "wrong-secret", code, verifier).status_code == 401
    assert exchange(client, remote_config, registered["client_id"], registered["client_secret"], code, "wrong-" + verifier).status_code == 400
    assert exchange(client, remote_config, registered["client_id"], registered["client_secret"], code, verifier, callback="https://chatgpt.com/connector/oauth/other").status_code == 400
    assert exchange(client, remote_config, registered["client_id"], registered["client_secret"], code, verifier, resource="https://other.example/mcp").status_code == 400
    token = exchange(client, remote_config, registered["client_id"], registered["client_secret"], code, verifier)
    assert token.status_code == 200 and "refresh_token" in token.json()
    assert exchange(client, remote_config, registered["client_id"], registered["client_secret"], code, verifier).status_code == 400
    tokens = token.json()
    headers = {"authorization": f"Bearer {tokens['access_token']}", "accept": "application/json, text/event-stream", "content-type": "application/json"}
    before = database_hashes(remote_config.database_root)
    listed = client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
    assert listed.status_code == 200
    assert_public_tool_contract(listed)
    snapshot = client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "liva_daily_snapshot", "arguments": {}}})
    assert snapshot.status_code == 200 and snapshot.json()["result"]["structuredContent"]["ok"] is True
    assert database_hashes(remote_config.database_root) == before
    refreshed = client.post("/oauth/token", data={"grant_type": "refresh_token", "client_id": registered["client_id"], "refresh_token": tokens["refresh_token"], "scope": "liva.read offline_access", "resource": remote_config.resource}, auth=(registered["client_id"], registered["client_secret"]))
    assert refreshed.status_code == 200
    revoked = client.post("/oauth/revoke", data={"token": refreshed.json()["refresh_token"], "token_type_hint": "refresh_token"}, auth=(registered["client_id"], registered["client_secret"]))
    assert revoked.status_code == 200
    assert client.post("/mcp", headers={"authorization": f"Bearer {refreshed.json()['access_token']}"}, json={}).status_code == 401


def test_dynamic_client_redirect_is_exact(client, remote_config):
    registered = register(client).json()
    response, _ = authorize(client, remote_config, registered["client_id"], "https://chatgpt.com/connector/oauth/another_allowed_callback")
    assert response.status_code == 400


def test_no_refresh_without_offline_access(client, remote_config):
    registered = register(client, scope="liva.read", grant_types=["authorization_code"]).json()
    authorization, verifier = authorize(client, remote_config, registered["client_id"], CALLBACK, scope="liva.read")
    code = approve(client, authorization.headers["location"])
    token = exchange(client, remote_config, registered["client_id"], registered["client_secret"], code, verifier)
    assert token.status_code == 200 and "refresh_token" not in token.json()


def test_no_refresh_when_grant_was_not_registered(client, remote_config):
    registered = register(client, grant_types=["authorization_code"]).json()
    authorization, verifier = authorize(client, remote_config, registered["client_id"], CALLBACK)
    code = approve(client, authorization.headers["location"])
    token = exchange(client, remote_config, registered["client_id"], registered["client_secret"], code, verifier)
    assert token.status_code == 200 and "refresh_token" not in token.json()


def test_registration_rate_limit(client):
    client.app.register_limiter.limit = 1
    assert register(client).status_code == 201
    assert register(client).status_code == 429


def test_active_dynamic_client_limit(client, remote_config):
    client.app.register_limiter.limit = 100
    store = AuthStore(remote_config.auth_db)
    for index in range(MAX_ACTIVE_DYNAMIC_CLIENTS):
        assert store.create_dynamic_client(
            client_id=f"filled-client-{index}", secret_hash_value="$argon2id$placeholder",
            redirect_uris=[CALLBACK], scope="liva.read", grant_types=["authorization_code"],
            response_types=["code"], token_endpoint_auth_method="client_secret_basic", issued_at=1,
        )
    response = register(client)
    assert response.status_code == 400
    assert store.active_dynamic_client_count() == MAX_ACTIVE_DYNAMIC_CLIENTS


def test_registration_logs_are_diagnostic_but_secret_free(client, remote_config, caplog):
    with caplog.at_level("INFO", logger="liva_mcp.audit"):
        response = register(client)
        registered = response.json()
        client.post("/oauth/token", data={"grant_type": "authorization_code", "client_id": registered["client_id"], "code": "not-a-code", "redirect_uri": CALLBACK, "code_verifier": "never-log-this-verifier", "resource": remote_config.resource}, auth=(registered["client_id"], registered["client_secret"]))
    assert "path=/oauth/register" in caplog.text
    assert "path=/oauth/token" in caplog.text
    assert "redirect_host=chatgpt.com" in caplog.text
    assert "request_id=" in caplog.text
    for forbidden in (registered["client_secret"], "not-a-code", "never-log-this-verifier", "Basic "):
        assert forbidden not in caplog.text
