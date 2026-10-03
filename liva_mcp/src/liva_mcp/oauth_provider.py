from __future__ import annotations

import json
import hashlib
import hmac
import logging
import re
import secrets
import time
from urllib.parse import urlencode

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from mcp.server.auth.provider import (
    AccessToken, AuthorizationCode, AuthorizationParams, AuthorizeError,
    OAuthAuthorizationServerProvider, RefreshToken, TokenError,
)
from mcp.shared.auth import InvalidRedirectUriError, OAuthClientInformationFull, OAuthToken
from pydantic import AnyUrl

from .auth_store import AuthStore
from .remote_config import RemoteConfig


CALLBACK_RE = re.compile(r"^https://chatgpt\.com/connector/oauth/[A-Za-z0-9_-]{1,160}$")
CLAUDE_CALLBACK = "https://claude.ai/api/mcp/auth_callback"
LEGACY_CALLBACK = "https://chatgpt.com/connector_platform_oauth_redirect"
SCOPE_READ = "liva.read"
SCOPE_WRITE = "liva.write"
SCOPE_OFFLINE = "offline_access"
SUPPORTED_SCOPES = (SCOPE_READ, SCOPE_WRITE, SCOPE_OFFLINE)
auth_logger = logging.getLogger("liva_mcp.auth")


def normalize_scopes(value: str | list[str] | None, *, require_read: bool = True) -> list[str]:
    """Parse client scope syntax once, independent of order and separators."""
    if value is None:
        values: list[str] = []
    elif isinstance(value, str):
        values = [item for item in re.split(r"[\s,]+", value.strip()) if item]
    elif isinstance(value, list) and all(isinstance(item, str) for item in value):
        values = [part for item in value for part in re.split(r"[\s,]+", item.strip()) if part]
    else:
        raise ValueError("Scopes must be text")
    unknown = set(values) - set(SUPPORTED_SCOPES)
    if unknown:
        raise ValueError("The requested scope is not allowed")
    normalized = [scope for scope in SUPPORTED_SCOPES if scope in set(values)]
    if require_read and SCOPE_READ not in normalized:
        raise ValueError("liva.read is required")
    return normalized


def redirect_allowed(value: str) -> bool:
    return bool(CALLBACK_RE.fullmatch(value)) or value in {CLAUDE_CALLBACK, LEGACY_CALLBACK}


class ChatGPTClient(OAuthClientInformationFull):
    def validate_redirect_uri(self, redirect_uri: AnyUrl | None) -> AnyUrl:
        if redirect_uri is None or not redirect_allowed(str(redirect_uri)):
            raise InvalidRedirectUriError("Redirect URI is not registered for this client")
        return redirect_uri


class PersonalOAuthProvider(OAuthAuthorizationServerProvider[AuthorizationCode, RefreshToken, AccessToken]):
    def __init__(self, config: RemoteConfig, store: AuthStore):
        self.config = config
        self.store = store

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        if secrets.compare_digest(client_id, self.config.client_id):
            return ChatGPTClient(
                client_id=self.config.client_id, client_secret=self.config.client_secret,
                redirect_uris=[AnyUrl(LEGACY_CALLBACK)], token_endpoint_auth_method="client_secret_basic",
                grant_types=["authorization_code", "refresh_token"], response_types=["code"],
                scope=f"{SCOPE_READ} {SCOPE_WRITE} {SCOPE_OFFLINE}", client_name="LIVA personal controlled access",
            )
        row = self.store.get_dynamic_client(client_id)
        if not row:
            return None
        return OAuthClientInformationFull(
            client_id=row["client_id"], client_secret=self.sdk_client_secret(row["client_id"]),
            redirect_uris=[AnyUrl(value) for value in json.loads(row["redirect_uris_json"])],
            token_endpoint_auth_method=row["token_endpoint_auth_method"],
            grant_types=json.loads(row["grant_types_json"]), response_types=json.loads(row["response_types_json"]),
            scope=row["scope"], client_id_issued_at=row["issued_at"], client_secret_expires_at=0,
        )

    def sdk_client_secret(self, client_id: str) -> str:
        if secrets.compare_digest(client_id, self.config.client_id):
            return self.config.client_secret
        return hmac.new(self.config.signing_secret.encode(), f"dcr:{client_id}".encode(), hashlib.sha256).hexdigest()

    def verify_client_secret(self, client_id: str, client_secret: str) -> bool:
        if secrets.compare_digest(client_id, self.config.client_id):
            return secrets.compare_digest(client_secret, self.config.client_secret)
        row = self.store.get_dynamic_client(client_id)
        if not row:
            return False
        if row.get("token_endpoint_auth_method") == "none" and not client_secret:
            return True
        try:
            return PasswordHasher().verify(row["secret_hash"], client_secret)
        except (VerifyMismatchError, InvalidHashError):
            return False

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        raise NotImplementedError

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        if params.resource != self.config.resource:
            raise AuthorizeError("invalid_request", "The resource parameter is required and must identify this MCP server")
        try:
            scopes = normalize_scopes(params.scopes)
        except ValueError as exc:
            raise AuthorizeError("invalid_scope", str(exc)) from exc
        # ChatGPT currently registers the refresh_token grant but may omit the
        # optional offline_access scope from its authorization request. Treat a
        # refresh-capable registration as the durable-session opt-in so a
        # one-hour access-token expiry never disconnects an existing chat.
        if (
            not secrets.compare_digest(client.client_id or "", self.config.client_id)
            and "refresh_token" in (client.grant_types or [])
            and SCOPE_OFFLINE not in scopes
        ):
            scopes.append(SCOPE_OFFLINE)
        request_id = secrets.token_urlsafe(32)
        if not secrets.compare_digest(client.client_id, self.config.client_id):
            self.store.mark_dynamic_client_used(client.client_id)
        self.store.put_request(request_id, {
            "client_id": client.client_id, "redirect_uri": str(params.redirect_uri),
            "resource": params.resource, "scopes": scopes, "code_challenge": params.code_challenge,
            "state": params.state, "expires_at": int(time.time()) + self.config.authorization_request_seconds,
        })
        return f"{self.config.base_url}/oauth/login?{urlencode({'request': request_id})}"

    async def load_authorization_code(self, client: OAuthClientInformationFull, authorization_code: str) -> AuthorizationCode | None:
        row = self.store.get_code(authorization_code)
        if not row or row["client_id"] != client.client_id:
            return None
        return AuthorizationCode(
            code=authorization_code, scopes=json.loads(row["scopes_json"]), expires_at=row["expires_at"],
            client_id=row["client_id"], code_challenge=row["code_challenge"], redirect_uri=AnyUrl(row["redirect_uri"]),
            redirect_uri_provided_explicitly=True, resource=row["resource"], subject="liva-owner",
        )

    def _issue(self, client_id: str, scopes: list[str], resource: str, family_id: str | None = None) -> str:
        now = int(time.time()); expires = now + self.config.access_token_seconds; jti = secrets.token_urlsafe(24)
        claims = {
            "iss": self.config.issuer, "aud": resource, "sub": "liva-owner", "client_id": client_id,
            "scope": " ".join(scopes), "iat": now, "nbf": now, "exp": expires, "jti": jti,
        }
        if self.config.smoke_test_client_id and secrets.compare_digest(client_id, self.config.smoke_test_client_id):
            claims["liva_smoke_test"] = True
        token = jwt.encode(claims, self.config.signing_secret, algorithm="HS256")
        self.store.put_access(token, jti=jti, client_id=client_id, resource=resource, scopes=scopes, expires_at=expires, family_id=family_id)
        if not secrets.compare_digest(client_id, self.config.client_id):
            self.store.mark_dynamic_client_used(client_id)
        return token

    async def exchange_authorization_code(self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode) -> OAuthToken:
        if not self.store.consume_code(authorization_code.code):
            raise TokenError("invalid_grant", "authorization code is invalid or already used")
        resource = authorization_code.resource or self.config.resource
        family_id = secrets.token_urlsafe(24) if SCOPE_OFFLINE in authorization_code.scopes and "refresh_token" in client.grant_types else None
        access = self._issue(authorization_code.client_id, authorization_code.scopes, resource, family_id)
        refresh = None
        if family_id:
            refresh = secrets.token_urlsafe(48)
            self.store.put_refresh(refresh, client_id=authorization_code.client_id, resource=resource, scopes=authorization_code.scopes, expires_at=int(time.time()) + self.config.refresh_token_seconds, family_id=family_id)
        return OAuthToken(access_token=access, expires_in=self.config.access_token_seconds, scope=" ".join(authorization_code.scopes), refresh_token=refresh)

    async def load_refresh_token(self, client: OAuthClientInformationFull, refresh_token: str) -> RefreshToken | None:
        row = self.store.get_refresh(refresh_token)
        if not row or row["client_id"] != client.client_id or row["expires_at"] < int(time.time()):
            return None
        return RefreshToken(token=refresh_token, client_id=row["client_id"], scopes=json.loads(row["scopes_json"]), expires_at=row["expires_at"], subject="liva-owner")

    async def exchange_refresh_token(self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str]) -> OAuthToken:
        row = self.store.get_refresh(refresh_token.token)
        if not row or not self.store.rotate_refresh(refresh_token.token):
            raise TokenError("invalid_grant", "refresh token is invalid or already used")
        client_id = client.client_id or ""
        access = self._issue(client_id, scopes, row["resource"], row["family_id"])
        new_refresh = secrets.token_urlsafe(48)
        self.store.put_refresh(new_refresh, client_id=client_id, resource=row["resource"], scopes=scopes, expires_at=int(time.time()) + self.config.refresh_token_seconds, family_id=row["family_id"])
        return OAuthToken(access_token=access, expires_in=self.config.access_token_seconds, scope=" ".join(scopes), refresh_token=new_refresh)

    async def load_access_token(self, token: str) -> AccessToken | None:
        """Validate a bearer token without logging its contents."""
        def rejected(reason: str) -> None:
            # Log only fixed categories: never claims, token bytes, or secrets.
            auth_logger.info("access_token_rejected reason=%s", reason)
        try:
            header = jwt.get_unverified_header(token)
            if header.get("alg") != "HS256":
                rejected("algorithm")
                return None
            claims = jwt.decode(token, self.config.signing_secret, algorithms=["HS256"], issuer=self.config.issuer, audience=self.config.resource, options={"require": ["exp", "iat", "nbf", "iss", "aud", "jti", "scope"]})
        except jwt.ExpiredSignatureError:
            rejected("expired")
            return None
        except jwt.ImmatureSignatureError:
            rejected("not_yet_valid")
            return None
        except jwt.InvalidIssuerError:
            rejected("issuer")
            return None
        except jwt.InvalidAudienceError:
            rejected("audience")
            return None
        except jwt.InvalidSignatureError:
            rejected("signature")
            return None
        except jwt.MissingRequiredClaimError:
            rejected("missing_claim")
            return None
        except jwt.InvalidAlgorithmError:
            rejected("algorithm")
            return None
        except jwt.PyJWTError:
            rejected("malformed")
            return None
        scopes = str(claims.get("scope", "")).split()
        row = self.store.get_access(token)
        if not row:
            rejected("store_missing")
            return None
        if row["jti"] != claims.get("jti"):
            rejected("store_jti")
            return None
        if row["resource"] != self.config.resource:
            rejected("store_resource")
            return None
        if row["expires_at"] < int(time.time()):
            rejected("store_expired")
            return None
        if not secrets.compare_digest(row["client_id"], self.config.client_id):
            self.store.mark_dynamic_client_used(row["client_id"])
        return AccessToken(token=token, client_id=row["client_id"], scopes=scopes, expires_at=row["expires_at"], resource=row["resource"], subject="liva-owner", claims={"iss": claims["iss"], "jti": claims["jti"], "liva_smoke_test": claims.get("liva_smoke_test", False)})

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        row = self.store.get_access(token.token) if isinstance(token, AccessToken) else self.store.get_refresh(token.token)
        if row:
            if row.get("family_id"):
                self.store.revoke_family(row["family_id"])
            elif isinstance(token, AccessToken):
                self.store.revoke_access(token.token)
