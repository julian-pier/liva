from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
import secrets
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlsplit

from argon2 import PasswordHasher

from .oauth_provider import PersonalOAuthProvider, SCOPE_OFFLINE, SCOPE_READ, SCOPE_WRITE, normalize_scopes, redirect_allowed


MAX_ACTIVE_DYNAMIC_CLIENTS = 50
ALLOWED_REGISTRATION_FIELDS = {
    "redirect_uris", "scope", "grant_types", "response_types",
    "token_endpoint_auth_method", "client_name", "client_uri", "logo_uri",
    "contacts", "tos_uri", "policy_uri", "software_id", "software_version",
}
IGNORED_DESCRIPTIVE_FIELDS = {
    "client_uri", "logo_uri", "contacts", "tos_uri", "policy_uri",
    "software_id", "software_version",
}
SAFE_LOG_VALUE = re.compile(r"^[A-Za-z0-9._:-]{1,160}$")
audit_logger = logging.getLogger("liva_mcp.audit")


def configure_audit_logging() -> None:
    audit_logger.setLevel(logging.INFO)
    if not audit_logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(levelname)s liva_mcp.audit %(message)s"))
        audit_logger.addHandler(handler)
    audit_logger.propagate = False


@dataclass(frozen=True)
class DcrValidationError(Exception):
    error: str
    description: str
    reason: str = "unspecified"


def validate_registration(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise DcrValidationError("invalid_client_metadata", "Registration metadata must be an object", "payload_not_object")
    # RFC 7591 requires unrecognized metadata to be ignored. Only fields in
    # this allowlist reach validation or persistence below.
    payload = {key: value for key, value in payload.items() if isinstance(key, str) and key in ALLOWED_REGISTRATION_FIELDS}
    redirects = payload.get("redirect_uris")
    if not isinstance(redirects, list) or not 1 <= len(redirects) <= 2 or any(not isinstance(uri, str) for uri in redirects):
        raise DcrValidationError("invalid_redirect_uri", "One or two redirect URIs are required", "redirect_shape")
    if len(set(redirects)) != len(redirects) or any(not redirect_allowed(uri) for uri in redirects):
        raise DcrValidationError("invalid_redirect_uri", "A redirect URI is not allowed", "redirect_not_allowed")
    scope_value = payload.get("scope")
    try:
        scopes = normalize_scopes(scope_value if isinstance(scope_value, str) and scope_value.strip() else SCOPE_READ, require_read=False)
    except ValueError as exc:
        raise DcrValidationError("invalid_client_metadata", str(exc), "scope_not_allowed") from exc
    if SCOPE_READ not in scopes:
        scopes.append(SCOPE_READ)
    grant_types = payload.get("grant_types", ["authorization_code"])
    if not isinstance(grant_types, list) or not grant_types or any(not isinstance(value, str) for value in grant_types):
        raise DcrValidationError("invalid_client_metadata", "Grant types are invalid", "grant_types_shape")
    if len(set(grant_types)) != len(grant_types) or "authorization_code" not in grant_types or any(value not in {"authorization_code", "refresh_token"} for value in grant_types):
        raise DcrValidationError("invalid_client_metadata", "A grant type is not allowed", "grant_type_not_allowed")
    if "refresh_token" in grant_types and SCOPE_OFFLINE not in scopes:
        scopes.append(SCOPE_OFFLINE)
    response_types = payload.get("response_types", ["code"])
    if response_types != ["code"]:
        raise DcrValidationError("invalid_client_metadata", "Only the code response type is allowed", "response_type_not_allowed")
    auth_method = payload.get("token_endpoint_auth_method", "client_secret_basic")
    if auth_method not in {"client_secret_basic", "none"}:
        raise DcrValidationError("invalid_client_metadata", "Only client_secret_basic or none is allowed", "token_endpoint_auth_method_not_allowed")
    client_name = payload.get("client_name")
    if client_name is not None and (not isinstance(client_name, str) or not 1 <= len(client_name) <= 100 or any(ord(char) < 32 for char in client_name)):
        raise DcrValidationError("invalid_client_metadata", "Client name is invalid", "client_name_invalid")
    for field in ("client_uri", "logo_uri", "tos_uri", "policy_uri"):
        value = payload.get(field)
        if value is not None and (not isinstance(value, str) or not 1 <= len(value) <= 2_048 or any(ord(char) < 32 for char in value)):
            raise DcrValidationError("invalid_client_metadata", f"{field} is invalid", f"{field}_invalid")
    contacts = payload.get("contacts")
    if contacts is not None and (not isinstance(contacts, list) or not contacts or len(contacts) > 10 or any(not isinstance(value, str) or not 1 <= len(value) <= 254 or any(ord(char) < 32 for char in value) for value in contacts)):
        raise DcrValidationError("invalid_client_metadata", "contacts are invalid", "contacts_invalid")
    for field in ("software_id", "software_version"):
        value = payload.get(field)
        if value is not None and (not isinstance(value, str) or not 1 <= len(value) <= 200 or any(ord(char) < 32 for char in value)):
            raise DcrValidationError("invalid_client_metadata", f"{field} is invalid", f"{field}_invalid")
    ordered_scopes = [scope for scope in (SCOPE_READ, SCOPE_WRITE, SCOPE_OFFLINE) if scope in scopes]
    ordered_grants = [grant for grant in ("authorization_code", "refresh_token") if grant in grant_types]
    result = {
        "redirect_uris": redirects, "scope": " ".join(ordered_scopes),
        "grant_types": ordered_grants, "response_types": ["code"],
        "token_endpoint_auth_method": auth_method,
    }
    if client_name is not None:
        result["client_name"] = client_name
    # These RFC 7591 descriptive fields are deliberately not persisted, fetched,
    # or reflected. They cannot alter the granted client privileges.
    assert not (IGNORED_DESCRIPTIVE_FIELDS - set(ALLOWED_REGISTRATION_FIELDS))
    return result


def register_dynamic_client(provider: PersonalOAuthProvider, payload: Any) -> dict[str, Any]:
    metadata = validate_registration(payload)
    issued_at = int(time.time())
    for _attempt in range(4):
        client_id = secrets.token_urlsafe(24)
        client_secret = secrets.token_urlsafe(48)
        stored = provider.store.create_dynamic_client(
            client_id=client_id, secret_hash_value=PasswordHasher().hash(client_secret),
            redirect_uris=metadata["redirect_uris"], scope=metadata["scope"],
            grant_types=metadata["grant_types"], response_types=metadata["response_types"],
            token_endpoint_auth_method=metadata["token_endpoint_auth_method"], issued_at=issued_at,
            maximum_active=MAX_ACTIVE_DYNAMIC_CLIENTS,
        )
        if stored:
            return {
                "client_id": client_id, "client_secret": client_secret,
                "client_id_issued_at": issued_at, "client_secret_expires_at": 0,
                **metadata,
            }
        if provider.store.active_dynamic_client_count() >= MAX_ACTIVE_DYNAMIC_CLIENTS:
            raise DcrValidationError("invalid_client_metadata", "The dynamic client limit has been reached")
    raise DcrValidationError("server_error", "Client registration could not be completed")


def _basic_client_id(headers: dict[bytes, bytes]) -> str | None:
    authorization = headers.get(b"authorization", b"").decode("ascii", "ignore")
    if not authorization.startswith("Basic "):
        return None
    try:
        return base64.b64decode(authorization[6:], validate=True).decode("utf-8").split(":", 1)[0]
    except (ValueError, UnicodeError):
        return None


def _user_agent(value: str) -> str:
    lowered = value.lower()
    if "chatgpt" in lowered:
        return "chatgpt"
    if "openai" in lowered:
        return "openai"
    if "httpx" in lowered or "python" in lowered:
        return "python-http"
    if "curl" in lowered:
        return "curl"
    product = value.split("/", 1)[0].strip().lower()
    return product if SAFE_LOG_VALUE.fullmatch(product) else "other"


class RequestAuditMiddleware:
    def __init__(self, app: Any, resource: str):
        self.app = app
        self.resource = resource

    def __getattr__(self, name: str) -> Any:
        return getattr(self.app, name)

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        request_id = secrets.token_hex(8)
        request_parts: list[bytes] = []
        response_parts: list[bytes] = []
        status = 500
        response_error = "-"

        async def observed_receive() -> dict[str, Any]:
            event = await receive()
            if event.get("type") == "http.request" and sum(map(len, request_parts)) < 65_536:
                request_parts.append(event.get("body", b""))
            return event

        async def observed_send(event: dict[str, Any]) -> None:
            nonlocal status, response_error
            outgoing = event
            if event.get("type") == "http.response.start":
                status = int(event["status"])
                headers = {key.lower(): value for key, value in event.get("headers", [])}
                candidate = headers.get(b"x-liva-oauth-error", b"").decode("ascii", "ignore")
                if SAFE_LOG_VALUE.fullmatch(candidate):
                    response_error = candidate
                if scope.get("path") == "/oauth/authorize":
                    location = headers.get(b"location", b"").decode("utf-8", "ignore")
                    callback_error = parse_qs(urlsplit(location).query).get("error", [""])[0]
                    if callback_error:
                        audit = scope.setdefault("_liva_scope_audit", {})
                        if isinstance(audit, dict):
                            audit["authz_error_redirect"] = "yes"
                            audit["callback_error"] = callback_error if SAFE_LOG_VALUE.fullmatch(callback_error) else "invalid"
                outgoing = dict(event)
                outgoing["headers"] = list(event.get("headers", [])) + [(b"x-request-id", request_id.encode())]
            elif event.get("type") == "http.response.body" and sum(map(len, response_parts)) < 65_536:
                response_parts.append(event.get("body", b""))
            await send(outgoing)

        try:
            await self.app(scope, observed_receive, observed_send)
        finally:
            self._log(scope, b"".join(request_parts), b"".join(response_parts), status, request_id, response_error)

    def _log(self, scope: dict[str, Any], request_body: bytes, response_body: bytes, status: int, request_id: str, response_error: str) -> None:
        path = str(scope.get("path", "-"))
        method = str(scope.get("method", "-"))
        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        query = parse_qs(scope.get("query_string", b"").decode("utf-8", "ignore"), keep_blank_values=True)
        fields: dict[str, Any] = {key: value[0] for key, value in query.items() if value}
        content_type = headers.get(b"content-type", b"").decode("ascii", "ignore")
        try:
            if content_type.startswith("application/json") and request_body:
                parsed = json.loads(request_body)
                if isinstance(parsed, dict):
                    fields.update(parsed)
            elif content_type.startswith("application/x-www-form-urlencoded") and request_body:
                fields.update({key: value[0] for key, value in parse_qs(request_body.decode(), keep_blank_values=True).items() if value})
        except (UnicodeError, json.JSONDecodeError):
            pass
        client_id = _basic_client_id(headers) or (fields.get("client_id") if isinstance(fields.get("client_id"), str) else None)
        if path == "/oauth/register" and status == 201:
            try:
                registered = json.loads(response_body)
                if isinstance(registered.get("client_id"), str):
                    client_id = registered["client_id"]
            except (json.JSONDecodeError, AttributeError):
                pass
        client_hash = hashlib.sha256(client_id.encode()).hexdigest()[:12] if client_id else "-"
        redirect_value = fields.get("redirect_uri")
        if redirect_value is None and isinstance(fields.get("redirect_uris"), list) and fields["redirect_uris"]:
            redirect_value = fields["redirect_uris"][0]
        redirect_host = "-"
        if isinstance(redirect_value, str):
            host = (urlsplit(redirect_value).hostname or "").lower()
            redirect_host = host if SAFE_LOG_VALUE.fullmatch(host) else "invalid"
        scope_value = fields.get("scope")
        if isinstance(scope_value, str):
            try:
                scope_log = ",".join(normalize_scopes(scope_value))
            except ValueError:
                scope_log = "invalid"
        else:
            scope_log = "-"
        resource_value = fields.get("resource")
        resource_match = "yes" if resource_value == self.resource else "no" if isinstance(resource_value, str) else "na"
        grant_type = fields.get("grant_type") if isinstance(fields.get("grant_type"), str) and SAFE_LOG_VALUE.fullmatch(fields["grant_type"]) else "-"
        code_present = "yes" if isinstance(fields.get("code"), str) and fields["code"] else "no"
        verifier_present = "yes" if isinstance(fields.get("code_verifier"), str) and fields["code_verifier"] else "no"
        redirect_uris = fields.get("redirect_uris")
        redirect_present = "yes" if (isinstance(fields.get("redirect_uri"), str) and fields["redirect_uri"]) or (isinstance(redirect_uris, list) and bool(redirect_uris)) else "no"
        dcr_audit = scope.get("_liva_dcr_audit", {}) if isinstance(scope.get("_liva_dcr_audit"), dict) else {}
        redirect_count = dcr_audit.get("redirect_count", "-")
        dcr_auth_method = dcr_audit.get("auth_method", "-")
        dcr_response_type = dcr_audit.get("response_type", "-")
        dcr_grant_types = dcr_audit.get("grant_types", "-")
        ignored_fields = dcr_audit.get("ignored_fields", "-")
        validation_failure_reason = dcr_audit.get("validation_failure_reason", "-")
        token_auth = scope.get("_liva_token_auth", {}) if isinstance(scope.get("_liva_token_auth"), dict) else {}
        auth_method = token_auth.get("method", "-")
        client_found = token_auth.get("client_found", "-")
        client_active = token_auth.get("client_active", "-")
        expected_auth = token_auth.get("expected", "-")
        secret_valid = token_auth.get("secret_valid", "-")
        dynamic_client = token_auth.get("dynamic", "-")
        client_id_match = token_auth.get("client_id_match", "-")
        sdk_auth = token_auth.get("sdk_auth", "-")
        token_exchange = scope.get("_liva_token_exchange", {}) if isinstance(scope.get("_liva_token_exchange"), dict) else {}
        token_handler = token_exchange.get("token_handler", "-")
        provider_stage = token_exchange.get("provider_stage", "-")
        final_failure_source = token_exchange.get("final_failure_source", "-")
        grant_allowed = token_exchange.get("grant_allowed", "-")
        redirect_allowed_value = token_exchange.get("redirect_allowed", "-")
        code_found = token_exchange.get("code_found", "-")
        code_used = token_exchange.get("code_used", "-")
        code_expired = token_exchange.get("code_expired", "-")
        pkce_valid = token_exchange.get("pkce_valid", "-")
        token_issued = token_exchange.get("token_issued", "-")
        scope_audit = scope.get("_liva_scope_audit", {}) if isinstance(scope.get("_liva_scope_audit"), dict) else {}
        requested_scope_raw = scope_audit.get("requested_scope_raw", scope_value if isinstance(scope_value, str) else "-")
        requested_scopes_normalized = scope_audit.get("requested_scopes_normalized", scope_log)
        client_allowed_before = scope_audit.get("client_allowed_scopes_before", "-")
        client_allowed_after = scope_audit.get("client_allowed_scopes_after", "-")
        client_scope_upgraded = scope_audit.get("client_scope_upgraded", "no")
        authz_error_redirect = scope_audit.get("authz_error_redirect", "no")
        callback_error = scope_audit.get("callback_error", "-")
        token_scope_issued = token_exchange.get("token_scope_issued", "-")
        error = response_error
        try:
            parsed_response = json.loads(response_body)
            candidate = parsed_response.get("error") if isinstance(parsed_response, dict) else None
            if isinstance(candidate, str) and SAFE_LOG_VALUE.fullmatch(candidate):
                error = candidate
        except json.JSONDecodeError:
            pass
        user_agent = _user_agent(headers.get(b"user-agent", b"").decode("utf-8", "ignore"))
        audit_logger.info(
            "request method=%s path=%s status=%s oauth_error=%s client=%s redirect_host=%s redirect_present=%s redirect_count=%s dcr_auth_method=%s dcr_response_type=%s dcr_grant_types=%s ignored_fields=%s validation_failure_reason=%s grant_type=%s code_present=%s verifier_present=%s scope=%s requested_scope_raw=%s requested_scopes_normalized=%s client_allowed_scopes_before=%s client_allowed_scopes_after=%s client_scope_upgraded=%s authz_error_redirect=%s callback_error=%s token_scope_issued=%s resource_match=%s auth_method=%s expected_auth=%s client_found=%s client_active=%s dynamic_client=%s client_id_match=%s secret_valid=%s sdk_auth=%s token_handler=%s provider_stage=%s final_failure_source=%s grant_allowed=%s redirect_allowed=%s code_found=%s code_used=%s code_expired=%s pkce_valid=%s token_issued=%s ua=%s request_id=%s",
            method, path, status, error, client_hash, redirect_host, redirect_present, redirect_count, dcr_auth_method, dcr_response_type, dcr_grant_types, ignored_fields, validation_failure_reason, grant_type, code_present, verifier_present, scope_log, requested_scope_raw, requested_scopes_normalized, client_allowed_before, client_allowed_after, client_scope_upgraded, authz_error_redirect, callback_error, token_scope_issued, resource_match, auth_method, expected_auth, client_found, client_active, dynamic_client, client_id_match, secret_valid, sdk_auth, token_handler, provider_stage, final_failure_source, grant_allowed, redirect_allowed_value, code_found, code_used, code_expired, pkce_valid, token_issued, user_agent, request_id,
        )
