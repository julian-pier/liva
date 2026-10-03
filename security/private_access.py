from __future__ import annotations

import functools
import hashlib
import hmac
import ipaddress
import os
import secrets
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from flask import Blueprint, abort, g, jsonify, make_response, redirect, render_template, request, session, url_for

from database.connections import get_auth_db
from security.write_guard import is_tailscale_ip

private_access_bp = Blueprint("private_access", __name__)

MASTER_SECRET = (
    os.getenv("LIVA_MASTER_SECRET")
    or os.getenv("LIVA_SITE_PASSWORD")
    or os.getenv("LIVA_ADMIN_PASSWORD")
    or ""
).strip()

AUTH_PEPPER = (
    os.getenv("LIVA_AUTH_PEPPER")
    or MASTER_SECRET
    or "liva-default-pepper"
).strip()

AI_READ_API_KEY = (os.getenv("LIVA_AI_READ_KEY") or "").strip()
AI_WRITE_API_KEY = (os.getenv("LIVA_AI_WRITE_KEY") or "").strip()
if AI_READ_API_KEY and not AI_WRITE_API_KEY:
    AI_WRITE_API_KEY = AI_READ_API_KEY
if AI_WRITE_API_KEY and not AI_READ_API_KEY:
    AI_READ_API_KEY = AI_WRITE_API_KEY

WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
KEY_WRITE_EXEMPT_PATHS = {
    # Read-only compute endpoints used by /training UI (parser/ampel/last-set previews).
    "/api/training/validate_draft",
    "/api/training/last-slot-references",
    "/api/training/resolve-variation",
}
KEY_WRITE_EXEMPT_PREFIXES: tuple[str, ...] = ()
ROLE_RANK = {"public": 0, "key": 1, "master": 2}
_EXACT_EXEMPT_PATHS = {
    "/_private_access",
    "/_private_access/status",
    "/login",
    "/logout",
    "/favicon.ico",
    "/robots.txt",
    "/healthz",
    "/openapi.yaml",
    "/.well-known/openapi.yaml",
    "/api/debug/auth_health",
    # Dedicated machine-to-machine auth is enforced inside activity.api.
    "/api/activity/ingest",
    "/api/activity/iphone/daily",
}
_PREFIX_EXEMPT_PATHS = ("/static/", "/api/ai/", "/api/aiw/", "/api/pc-status/")
_CORE_PREFIXES = ("/core", "/api/core", "/ws/core")

_LOG_WINDOW_SECONDS = 15
_LAST_PUBLIC_API_LOG: dict[str, float] = {}
_AUTH_SCHEMA_READY = False
_DEFAULT_TRUSTED_PROXY_CIDRS = (
    "127.0.0.1/32",
    "::1/128",
    "100.64.0.0/10",
    "fd7a:115c:a1e0::/48",
    "172.16.0.0/12",
)
_DEFAULT_TAILSCALE_WHITELIST_ENTRIES = ("100.64.0.0/10", "fd7a:115c:a1e0::/48")
INTERNAL_BYPASS_PATHS = {"/add_row", "/api/log_weight"}
_ACCESS_ACTIVITY_IGNORE_PREFIXES = ("/api/", "/jit/", "/live", "/_private_access", "/login", "/logout")
_ACCESS_ACTIVITY_IGNORE_EXACT = {"/favicon.ico", "/robots.txt"}
_ACTIVITY_LOG_SKIP_PREFIXES = ("/static/", "/api/", "/jit/", "/live")
_ACTIVITY_LOG_SKIP_EXACT = {"/favicon.ico", "/robots.txt", "/login", "/_private_access", "/logout"}
_ACTIVITY_DEDUP_WINDOW_SECONDS = 20
_ACTIVITY_LAST_SEEN_UPDATE_SECONDS = 60
def _parse_positive_int_env(name: str, default: int, min_value: int = 1) -> int:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        val = int(raw)
    except Exception:
        return default
    return max(min_value, val)


_KEY_SESSION_VALIDATE_CACHE_TTL_SECONDS = _parse_positive_int_env(
    "LIVA_KEY_VALIDATE_CACHE_TTL_SECONDS", default=60, min_value=5
)
_AUTH_DEBUG_LOG = (os.getenv("LIVA_AUTH_DEBUG_LOG") or "").strip().lower() in {"1", "true", "yes", "on"}
OPENAI_CHATGPT_BOT_IPS = {
    "20.215.220.65",
    "20.215.220.78",
    "68.221.67.166",
    "68.221.67.167",
}


# ---------- parsing / IP ----------


def _parse_networks(raw: str | None, defaults: tuple[str, ...]) -> tuple[ipaddress._BaseNetwork, ...]:
    entries = [part.strip() for part in (raw or "").split(",") if part.strip()]
    if not entries:
        entries = list(defaults)
    out = []
    for entry in entries:
        try:
            if "/" in entry:
                out.append(ipaddress.ip_network(entry, strict=False))
            else:
                suffix = "/128" if ":" in entry else "/32"
                out.append(ipaddress.ip_network(entry + suffix, strict=False))
        except Exception:
            continue
    return tuple(out)


TRUSTED_PROXY_NETWORKS = _parse_networks(
    os.getenv("LIVA_TRUSTED_PROXIES"),
    _DEFAULT_TRUSTED_PROXY_CIDRS,
)


def _normalize_ip_candidate(value: str) -> str:
    token = (value or "").strip().strip('"').strip("'")
    if not token:
        return ""
    if token.lower().startswith("for="):
        token = token.split("=", 1)[1].strip()
    token = token.strip('"').strip("'")
    if token.startswith("["):
        end = token.find("]")
        if end > 0:
            token = token[1:end]
    if "%" in token:
        token = token.split("%", 1)[0]
    if token.count(":") == 1 and "." in token:
        token = token.rsplit(":", 1)[0].strip()
    try:
        return str(ipaddress.ip_address(token))
    except Exception:
        return ""


def _is_trusted_proxy(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address((ip or "").strip())
    except Exception:
        return False
    return any(addr in network for network in TRUSTED_PROXY_NETWORKS)


def _proxy_original_remote_addr() -> str:
    orig = request.environ.get("werkzeug.proxy_fix.orig") or {}
    if isinstance(orig, dict):
        return _normalize_ip_candidate(str(orig.get("REMOTE_ADDR") or ""))
    return ""


def _first_forwarded_ip() -> str:
    forwarded = (request.headers.get("Forwarded") or "").strip()
    if forwarded:
        for segment in forwarded.split(","):
            for token in segment.split(";"):
                part = token.strip()
                if part.lower().startswith("for="):
                    ip = _normalize_ip_candidate(part)
                    if ip:
                        return ip
    xff = (request.headers.get("X-Forwarded-For") or "").strip()
    if xff:
        for part in xff.split(","):
            ip = _normalize_ip_candidate(part)
            if ip:
                return ip
    x_real_ip = _normalize_ip_candidate((request.headers.get("X-Real-IP") or "").strip())
    return x_real_ip


def _parse_internal_allowed_ips() -> set[str]:
    # Keep common local/container sources always allowed for INTERNAL_BYPASS_PATHS.
    # ENV values are additive (not replacing these defaults) to avoid breakage on docker IP churn.
    defaults = {"127.0.0.1", "::1", "172.18.0.2", "172.19.0.2", "172.29.0.2"}
    raw_internal = (os.getenv("LIVA_INTERNAL_ALLOWED_IPS") or "").strip()
    raw_n8n = (os.getenv("LIVA_N8N_IPS") or "").strip()
    out: set[str] = set(defaults)
    for raw in (raw_internal, raw_n8n):
        for entry in raw.split(","):
            entry = entry.strip()
            if entry:
                out.add(entry)
    return out


def _parse_whitelist_entries() -> list[str]:
    auto_tailscale = (os.getenv("LIVA_AUTO_WHITELIST_TAILSCALE") or "1").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    raw = (os.getenv("LIVA_IP_WHITELIST") or "").strip()
    entries = [entry.strip() for entry in raw.split(",") if entry.strip()] if raw else []
    if auto_tailscale:
        entries.extend(_DEFAULT_TAILSCALE_WHITELIST_ENTRIES)
    entries.extend([ip for ip in OPENAI_CHATGPT_BOT_IPS if ip])
    # de-dup while preserving order
    seen = set()
    out = []
    for entry in entries:
        if entry in seen:
            continue
        seen.add(entry)
        out.append(entry)
    return out


IP_WHITELIST_ENTRIES = _parse_whitelist_entries()
INTERNAL_ALLOWED_IPS = _parse_internal_allowed_ips()


def _resolve_client_ip_for_whitelist() -> tuple[str, str]:
    remote = _normalize_ip_candidate((request.remote_addr or "").strip())
    orig_remote = _proxy_original_remote_addr()
    ingress_peer = orig_remote or remote

    if _is_trusted_proxy(ingress_peer):
        if orig_remote and remote and remote != ingress_peer:
            return remote, "proxyfix"
        forwarded_ip = _first_forwarded_ip()
        if forwarded_ip:
            return forwarded_ip, "forwarded_headers"
    return ingress_peer or remote, "remote_addr"


def _is_ip_whitelisted(ip: str) -> bool:
    if not ip:
        return False

    try:
        addr = ipaddress.ip_address(ip)
    except Exception:
        return False

    # Environment can be changed by service configuration and isolated test
    # apps after this module was imported. Do not freeze access policy at
    # import time; evaluate the small allowlist for the current request.
    for entry in _parse_whitelist_entries():
        try:
            if "/" in entry:
                if addr in ipaddress.ip_network(entry, strict=False):
                    return True
            else:
                if addr == ipaddress.ip_address(entry):
                    return True
        except Exception:
            continue

    return False


def _is_internal_allowed(remote_ip: str) -> bool:
    remote_ip = (remote_ip or "").strip()
    if not remote_ip:
        return False
    try:
        addr = ipaddress.ip_address(remote_ip)
    except Exception:
        return False

    for entry in INTERNAL_ALLOWED_IPS:
        try:
            if "/" in entry:
                if addr in ipaddress.ip_network(entry, strict=False):
                    return True
            else:
                if addr == ipaddress.ip_address(entry):
                    return True
        except Exception:
            continue
    return False



# ---------- auth state ----------

def _safe_next_url(raw: str | None) -> str:
    if not raw:
        return "/"
    parsed = urlparse(raw)
    path = parsed.path or "/"
    if parsed.netloc and parsed.netloc != request.host:
        return "/"
    if not path.startswith("/"):
        path = "/" + path
    if parsed.query:
        return f"{path}?{parsed.query}"
    return path


def _hash_secret(secret: str) -> str:
    payload = f"{AUTH_PEPPER}|{secret}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _extract_bearer_token() -> str:
    auth = (request.headers.get("Authorization") or "").strip()
    if not auth:
        return ""
    parts = auth.split()
    if len(parts) < 2:
        return ""
    if parts[0].lower() != "bearer":
        return ""
    return parts[1].strip()


def _extract_ai_token() -> str:
    auth = (request.headers.get("Authorization") or "").strip()
    if auth:
        parts = auth.split(None, 1)
        if len(parts) == 2 and parts[0].lower() in {"bearer", "token", "apikey", "api-key"}:
            token = parts[1].strip()
            if token:
                return token
        if len(parts) == 1:
            return parts[0].strip()

    for header in (
        "X-API-Key",
        "X-Api-Key",
        "X-AI-Key",
        "X-LIVA-AI-Key",
        "X-LIVA-Api-Key",
        "X-Authorization",
    ):
        token = (request.headers.get(header) or "").strip()
        if token.lower().startswith("bearer "):
            token = token.split(" ", 1)[1].strip()
        if token:
            return token

    for arg in ("api_key", "apikey", "access_key", "key", "token"):
        token = (request.args.get(arg) or "").strip()
        if token:
            return token
    return ""


def _api_key_status() -> tuple[bool, bool, str]:
    token = _extract_ai_token()
    if token and AI_WRITE_API_KEY and hmac.compare_digest(token, AI_WRITE_API_KEY):
        return True, True, "write"
    if token and AI_READ_API_KEY and hmac.compare_digest(token, AI_READ_API_KEY):
        return True, True, "read"
    return bool(token), False, ""


def _has_tailscale_identity_header() -> bool:
    # Tailscale Serve strips these from incoming requests and then adds them
    # only for tailnet traffic. Funnel traffic does not receive them.
    for header in (
        "Tailscale-User-Login",
        "Tailscale-User-Name",
        "Tailscale-User-Profile-Pic",
        "Tailscale-Login",
        "Tailscale-User",
        "Tailscale-Name",
    ):
        if (request.headers.get(header) or "").strip():
            return True
    return False


def _tailscale_private_classification(client_ip: str, client_source: str) -> tuple[bool, str]:
    remote = _normalize_ip_candidate((request.remote_addr or "").strip())
    orig_remote = _proxy_original_remote_addr()
    ingress_peer = orig_remote or remote

    if ingress_peer and is_tailscale_ip(ingress_peer):
        if client_source in {"forwarded_headers", "proxyfix"} and client_ip and client_ip != ingress_peer:
            if _has_tailscale_identity_header():
                return True, "tailscale_identity_header"
            return False, f"unverified_{client_source}"
        return True, "direct_tailscale_peer"

    if remote and not orig_remote and is_tailscale_ip(remote):
        return True, "direct_tailscale_remote"

    if _is_trusted_proxy(ingress_peer) and _has_tailscale_identity_header():
        return True, "tailscale_identity_header"

    if client_ip and is_tailscale_ip(client_ip):
        return False, f"unverified_{client_source}"

    return False, "not_tailscale"


def _log_private_access_decision(decision: str, *, role: str, classification: str = "", api_key_present: bool | None = None, api_key_valid: bool | None = None) -> None:
    try:
        print(
            "[private_access] "
            f"decision={decision} "
            f"path={request.path or ''} "
            f"host={request.host or ''} "
            f"remote_addr={(request.remote_addr or '').strip()} "
            f"orig_remote_addr={_proxy_original_remote_addr()} "
            f"x_forwarded_for={_truncate_header((request.headers.get('X-Forwarded-For') or '').strip(), 120)} "
            f"x_real_ip={_truncate_header((request.headers.get('X-Real-IP') or '').strip(), 120)} "
            f"api_key_present={bool(getattr(g, 'api_key_present', False) if api_key_present is None else api_key_present)} "
            f"api_key_valid={bool(getattr(g, 'api_key_valid', False) if api_key_valid is None else api_key_valid)} "
            f"classification={classification or getattr(g, 'request_classification', '') or ''} "
            f"client_ip={getattr(g, 'client_ip', '') or ''} "
            f"client_ip_source={getattr(g, 'client_ip_source', '') or ''} "
            f"role={role}"
        )
    except Exception:
        pass


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def get_current_role() -> str:
    cached_role = getattr(g, "_cached_auth_role", None)
    if cached_role in {"public", "key", "master"}:
        return cached_role

    remote = (request.remote_addr or "").strip()
    client_ip, client_source = _resolve_client_ip_for_whitelist()
    g.client_ip = client_ip
    g.client_ip_source = client_source
    g.client_ip_details = {
        "remote_addr": remote,
        "x_forwarded_for": (request.headers.get("X-Forwarded-For") or "").strip(),
        "x_real_ip": (request.headers.get("X-Real-IP") or "").strip(),
        "orig_remote_addr": _proxy_original_remote_addr(),
    }
    api_key_present, api_key_valid, api_key_role = _api_key_status()
    g.api_key_present = api_key_present
    g.api_key_valid = api_key_valid
    g.request_classification = "api_key" if api_key_valid else ""
    if api_key_role == "write":
        g.whitelisted_ip = True
        g.key_id = None
        g.auth_level = "master"
        g.ai_key = True
        g._cached_auth_role = "master"
        return "master"
    if api_key_role == "read":
        g.whitelisted_ip = True
        g.key_id = None
        g.auth_level = "key"
        g.ai_key = True
        g._cached_auth_role = "key"
        return "key"
    if request.path in INTERNAL_BYPASS_PATHS and _is_internal_allowed(remote):
        g.whitelisted_ip = True
        g.key_id = None
        g.auth_level = "master"
        g._cached_auth_role = "master"
        return "master"

    tailscale_private, tailscale_reason = _tailscale_private_classification(client_ip, client_source)
    whitelisted = _is_ip_whitelisted(client_ip)
    if tailscale_private:
        whitelisted = True
        g.request_classification = "tailscale_private"
        g.tailscale_private_reason = tailscale_reason
    elif client_ip and is_tailscale_ip(client_ip):
        whitelisted = False
        g.request_classification = "public_spoof_or_unverified"
        g.tailscale_private_reason = tailscale_reason
    elif whitelisted:
        g.request_classification = "private_whitelist"
        g.tailscale_private_reason = tailscale_reason
    else:
        g.request_classification = "public"
        g.tailscale_private_reason = tailscale_reason
    g.whitelisted_ip = whitelisted
    if _AUTH_DEBUG_LOG:
        try:
            print(
                "AUTH_CLIENT "
                f"ip={client_ip} src={client_source} remote={(request.remote_addr or '').strip()} "
                f"xff={(request.headers.get('X-Forwarded-For') or '').strip()[:120]}"
            )
        except Exception:
            pass

    if whitelisted:
        role = "master"
        g.key_id = None
    else:
        role = (session.get("role") or "public").strip().lower()
        if role not in {"public", "key", "master"}:
            role = "public"
        key_id = session.get("key_id")
        if role == "key":
            now_ts = int(time.time())
            try:
                key_id_int = int(key_id or 0)
            except Exception:
                key_id_int = 0
            try:
                cached_key_id = int(session.get("_key_validated_id") or 0)
            except Exception:
                cached_key_id = 0
            try:
                cached_until_ts = int(session.get("_key_validated_until_ts") or 0)
            except Exception:
                cached_until_ts = 0
            valid = bool(key_id_int and cached_key_id == key_id_int and cached_until_ts > now_ts)
            if not valid:
                valid, exp_ts = _is_key_session_valid(key_id)
                if valid and key_id_int:
                    ttl_until = now_ts + _KEY_SESSION_VALIDATE_CACHE_TTL_SECONDS
                    if exp_ts is not None:
                        ttl_until = min(ttl_until, max(now_ts + 1, exp_ts - 1))
                    session["_key_validated_id"] = key_id_int
                    session["_key_validated_until_ts"] = int(ttl_until)
                else:
                    session.pop("_key_validated_id", None)
                    session.pop("_key_validated_until_ts", None)
            if not valid:
                role = "public"
                key_id = None
                session["role"] = "public"
                session["key_id"] = None
        g.key_id = key_id

    g.auth_level = role
    g._cached_auth_role = role
    return role


def _set_session(role: str, key_id: int | None = None) -> None:
    session["role"] = role
    session["key_id"] = key_id
    session["csrf_token"] = secrets.token_urlsafe(24)
    session["login_ts"] = int(time.time())
    session.pop("_key_validated_id", None)
    session.pop("_key_validated_until_ts", None)


def get_auth_template_context() -> dict:
    role = get_current_role()
    if role in {"master", "key"} and not session.get("csrf_token"):
        session["csrf_token"] = secrets.token_urlsafe(24)
    theme = get_user_theme_preference() or ""
    client_ip = str(getattr(g, "client_ip", "") or "")
    auth_is_tailscale = bool(client_ip and is_tailscale_ip(client_ip))
    if role == "key" and not theme:
        # Key users always get an explicit per-user theme baseline (no local fallback bleed).
        theme = "dark"
    # Key users should always be able to log out.
    # Master over whitelisted IP (e.g. tailscale auto-master) does not need logout.
    can_logout = role == "key" or (role == "master" and not bool(getattr(g, "whitelisted_ip", False)))
    return {
        "auth_level": role,
        "auth_is_master": role == "master",
        "auth_is_key": role == "key",
        "auth_can_logout": can_logout,
        "auth_key_label": None,
        "role_label": role.upper(),
        "auth_is_tailscale": auth_is_tailscale,
        "auth_theme": theme,
        "csrf_token": session.get("csrf_token") or "",
    }


# ---------- request typing / helpers ----------

def _wants_json_response() -> bool:
    if request.path.startswith("/api/") or request.path.startswith("/jit/"):
        return True
    if (request.headers.get("X-Requested-With") or "").strip().lower() == "xmlhttprequest":
        return True
    accept = (request.headers.get("Accept") or "").lower()
    if "application/json" in accept:
        return True
    if request.is_json:
        return True
    return False


def _is_exempt_path(path: str) -> bool:
    if path in _EXACT_EXEMPT_PATHS:
        return True
    return any(path.startswith(prefix) for prefix in _PREFIX_EXEMPT_PATHS)


def _core_enabled() -> bool:
    raw = (os.getenv("CORE_ENABLED") or os.getenv("LIVA_CORE_ENABLED") or "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _core_secret() -> str:
    return (os.getenv("LIVA_CORE_SECRET") or "").strip()


def _extract_core_token() -> str:
    token = (request.args.get("core_key") or "").strip()
    if token:
        return token
    token = (request.headers.get("X-Core-Key") or "").strip()
    if token:
        return token
    auth = (request.headers.get("Authorization") or "").strip()
    if auth.lower().startswith("bearer "):
        return auth.split(" ", 1)[1].strip()
    return ""


def _core_bypass_allowed() -> bool:
    path = request.path or ""
    if not any(path.startswith(prefix) for prefix in _CORE_PREFIXES):
        return False
    if _core_enabled():
        return True
    secret = _core_secret()
    if not secret:
        return False
    return hmac.compare_digest(_extract_core_token(), secret)


def _json_auth_required():
    return jsonify({"ok": False, "error": "auth_required"}), 401


def _no_store_response(response):
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0, private"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    response.headers["Surrogate-Control"] = "no-store"
    response.headers["Vary"] = "Cookie, Authorization, X-Forwarded-For, X-Real-IP"
    return response


def _redirect_no_store(location: str):
    return _no_store_response(redirect(location))


def _render_private_access(error: str | None, next_url: str, status: int = 200):
    response = make_response(render_template("private_access.html", error=error, next_url=next_url), status)
    return _no_store_response(response)


def _forbidden(message: str):
    if _wants_json_response():
        return jsonify({"ok": False, "error": "forbidden", "detail": message}), 403
    abort(403, description=message)


def _log_public_api_once() -> None:
    ip = getattr(g, "client_ip", "") or "unknown"
    key = f"{ip}|{request.method}|{request.path}"
    now = time.time()
    last = _LAST_PUBLIC_API_LOG.get(key, 0.0)
    if now - last < _LOG_WINDOW_SECONDS:
        return
    _LAST_PUBLIC_API_LOG[key] = now
    try:
        print(
            f"PUBLIC_API_BLOCK method={request.method} path={request.path} ip={ip} ua={request.headers.get('User-Agent','')[:120]}"
        )
    except Exception:
        pass


def _truncate_header(value: str, limit: int = 200) -> str:
    text = (value or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def _normalize_activity_path(path: str) -> str:
    p = (path or "").strip()
    if not p:
        return "/"
    if p == "/":
        return "/"
    if p.startswith("/static/"):
        return os.path.basename(p) or p
    parts = [seg for seg in p.split("/") if seg]
    if not parts:
        return "/"
    return "/" + parts[0]


# ---------- global guards ----------

def enforce_private_access():
    role = get_current_role()

    if _is_exempt_path(request.path):
        decision = "allow_api_key" if bool(getattr(g, "api_key_valid", False)) else "allow_exempt"
        _log_private_access_decision(decision, role=role)
        return

    if _core_bypass_allowed():
        _log_private_access_decision("allow_core_bypass", role=role)
        return

    if role != "public":
        if bool(getattr(g, "api_key_valid", False)):
            decision = "allow_api_key"
        elif getattr(g, "request_classification", "") == "tailscale_private":
            decision = "allow_tailscale"
        else:
            decision = "allow_authenticated"
        _log_private_access_decision(decision, role=role)
        return

    if _wants_json_response():
        _log_private_access_decision("json_unauthorized", role=role)
        return _json_auth_required()

    login_url = url_for("private_access.private_access_gate", next=_safe_next_url(request.url))
    _log_private_access_decision("redirect_login", role=role)
    return _redirect_no_store(login_url)


def enforce_key_read_only():
    role = get_current_role()
    if role != "key":
        return

    if request.method in WRITE_METHODS:
        path = request.path or ""
        if path in KEY_WRITE_EXEMPT_PATHS or path.startswith(KEY_WRITE_EXEMPT_PREFIXES):
            return
        return _forbidden("Gastzugang ist schreibgeschuetzt.")


def verify_csrf() -> bool:
    provided = (
        request.headers.get("X-CSRF-Token")
        or request.form.get("csrf_token")
        or (request.get_json(silent=True) or {}).get("csrf_token")
    )
    expected = session.get("csrf_token")
    return bool(provided and expected and hmac.compare_digest(str(provided), str(expected)))


# ---------- decorators ----------

def require_role(min_role: str):
    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            role = get_current_role()
            if ROLE_RANK.get(role, 0) >= ROLE_RANK.get(min_role, 0):
                return fn(*args, **kwargs)
            if _wants_json_response():
                return _json_auth_required()
            login_url = url_for("private_access.private_access_gate", next=_safe_next_url(request.url))
            return _redirect_no_store(login_url)

        return wrapper

    return decorator


def require_master(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        role = get_current_role()
        if role == "master" or (role == "key" and request.method in {"GET", "HEAD", "OPTIONS"}):
            return fn(*args, **kwargs)
        return _forbidden("Master-Zugriff erforderlich.")

    return wrapper


def require_key_or_master(fn):
    return require_role("key")(fn)


def require_tailscale(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        client_ip = str(getattr(g, "client_ip", "") or "")
        role = get_current_role()
        if (client_ip and is_tailscale_ip(client_ip)) or (
            role == "key" and request.method in {"GET", "HEAD", "OPTIONS"}
        ):
            return fn(*args, **kwargs)
        return _forbidden("Nur über Tailscale verfügbar.")

    return wrapper


# ---------- schema + key validation ----------


def _is_busy_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return "locked" in text or "busy" in text


def _with_auth_conn(callback, *, commit: bool = False, retries: int = 4, delay_s: float = 0.15):
    last_exc = None
    for attempt in range(retries):
        conn = None
        try:
            conn = get_auth_db()
            result = callback(conn)
            if commit:
                conn.commit()
            return result
        except sqlite3.OperationalError as exc:
            if not _is_busy_error(exc):
                raise
            last_exc = exc
            if attempt >= retries - 1:
                raise
            time.sleep(delay_s)
        finally:
            if conn is not None:
                conn.close()
    if last_exc:
        raise last_exc


def _is_key_session_valid(key_id: int | None) -> tuple[bool, int | None]:
    if not key_id:
        return False, None
    conn = None
    try:
        conn = get_auth_db()
        row = conn.execute(
            """
            SELECT id, expires_at, revoked_at
            FROM access_keys
            WHERE id = ?
            LIMIT 1
            """,
            (int(key_id),),
        ).fetchone()
    except Exception:
        return False, None
    finally:
        if conn is not None:
            conn.close()

    if not row:
        return False, None
    if row["revoked_at"]:
        return False, None
    exp = _parse_ts(row["expires_at"])
    if not exp or exp <= datetime.now(timezone.utc):
        return False, None
    return True, int(exp.timestamp())

def ensure_auth_schema() -> None:
    global _AUTH_SCHEMA_READY
    if _AUTH_SCHEMA_READY:
        return
    conn = None
    try:
        conn = get_auth_db()
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS access_keys (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                label TEXT NOT NULL,
                key_hash TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                revoked_at TEXT,
                last_seen_at TEXT,
                last_ip TEXT,
                last_user_agent TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS access_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                key_id INTEGER,
                session_id INTEGER,
                method TEXT,
                path TEXT,
                status INTEGER,
                ip TEXT,
                user_agent TEXT,
                FOREIGN KEY(key_id) REFERENCES access_keys(id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS user_preferences (
                key_id INTEGER NOT NULL,
                pref_key TEXT NOT NULL,
                pref_value TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (key_id, pref_key),
                FOREIGN KEY(key_id) REFERENCES access_keys(id)
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_access_keys_active ON access_keys(revoked_at, expires_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_access_log_key_ts ON access_log(key_id, ts)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_user_preferences_key ON user_preferences(pref_key, key_id)")
        conn.commit()
    finally:
        if conn is not None:
            conn.close()
    _AUTH_SCHEMA_READY = True


def _validate_key(access_key: str) -> tuple[str, int | None]:
    if not access_key:
        return "invalid", None
    conn = None
    try:
        ensure_auth_schema()
        conn = get_auth_db()
        row = conn.execute(
            """
            SELECT id, expires_at, revoked_at
            FROM access_keys
            WHERE key_hash = ?
            LIMIT 1
            """,
            (_hash_secret(access_key),),
        ).fetchone()
    except sqlite3.OperationalError as exc:
        if _is_busy_error(exc):
            return "busy", None
        return "error", None
    except sqlite3.Error:
        return "error", None
    finally:
        if conn is not None:
            conn.close()

    if not row:
        return "invalid", None
    if row["revoked_at"]:
        return "invalid", None

    expires_at = _parse_ts(row["expires_at"])
    if not expires_at or expires_at <= datetime.now(timezone.utc):
        return "invalid", None

    return "ok", int(row["id"])


def require_localhost(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        remote = (request.remote_addr or "").strip()
        if remote in {"127.0.0.1", "::1"}:
            return fn(*args, **kwargs)
        return jsonify({"ok": False, "error": "forbidden"}), 403

    return wrapper


def get_user_theme_preference() -> str | None:
    role = get_current_role()
    if role != "key":
        return None
    try:
        key_id = int(getattr(g, "key_id", 0) or 0)
    except Exception:
        key_id = 0
    if key_id <= 0:
        return None
    conn = None
    try:
        ensure_auth_schema()
        conn = get_auth_db()
        row = conn.execute(
            """
            SELECT pref_value
            FROM user_preferences
            WHERE key_id = ? AND pref_key = 'theme'
            LIMIT 1
            """,
            (key_id,),
        ).fetchone()
    except Exception:
        return None
    finally:
        if conn is not None:
            conn.close()
    if not row:
        return None
    value = (row["pref_value"] or "").strip().lower()
    if value in {"dark", "light"}:
        return value
    return None


def _set_user_theme_preference(theme: str) -> bool:
    role = get_current_role()
    if role != "key":
        return False
    try:
        key_id = int(getattr(g, "key_id", 0) or 0)
    except Exception:
        key_id = 0
    if key_id <= 0:
        return False
    value = (theme or "").strip().lower()
    if value not in {"dark", "light"}:
        return False
    conn = None
    try:
        ensure_auth_schema()
        conn = get_auth_db()
        conn.execute(
            """
            INSERT INTO user_preferences (key_id, pref_key, pref_value, updated_at)
            VALUES (?, 'theme', ?, ?)
            ON CONFLICT(key_id, pref_key) DO UPDATE SET
                pref_value = excluded.pref_value,
                updated_at = excluded.updated_at
            """,
            (key_id, value, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
        return True
    except Exception:
        return False
    finally:
        if conn is not None:
            conn.close()


# ---------- auth routes ----------
@private_access_bp.get("/_private_access/status")
def private_access_status():
    role = get_current_role()
    allowed = role in {"master", "key"}
    decision = "status_allowed" if allowed else "status_public"
    _log_private_access_decision(decision, role=role)
    return _no_store_response(
        jsonify(
            {
                "ok": True,
                "allowed": allowed,
                "role": role,
                "classification": getattr(g, "request_classification", "") or "",
            }
        )
    )


@private_access_bp.route("/login", methods=["GET", "POST"], endpoint="login")
@private_access_bp.route("/_private_access", methods=["GET", "POST"], endpoint="private_access_gate")
def login():
    request_start = time.perf_counter()
    role = get_current_role()
    next_url = _safe_next_url(request.values.get("next"))
    try:
        print(
            "AUTH_LOGIN_START "
            f"method={request.method} path={request.path} client_ip={getattr(g, 'client_ip', '')} "
            f"start_ts={int(time.time() * 1000)}"
        )
    except Exception:
        pass

    if role in {"master", "key"}:
        return _redirect_no_store(next_url)

    error = None
    if request.method == "POST":
        master_secret = (request.form.get("master_secret") or request.form.get("password") or "").strip()
        access_key = (request.form.get("access_key") or "").strip()

        if master_secret and MASTER_SECRET and hmac.compare_digest(master_secret, MASTER_SECRET):
            _set_session("master")
            return _redirect_no_store(next_url)

        key_started = time.perf_counter()
        key_status, key_id = _validate_key(access_key)
        key_elapsed_ms = (time.perf_counter() - key_started) * 1000
        try:
            print(
                f"AUTH_VALIDATE_KEY_DONE status={key_status} duration_ms={key_elapsed_ms:.2f} "
                f"client_ip={getattr(g, 'client_ip', '')}"
            )
            if key_elapsed_ms > 300:
                print(f"AUTH_VALIDATE_KEY_WARN duration_ms={key_elapsed_ms:.2f}")
        except Exception:
            pass

        if key_status == "busy":
            return _render_private_access("Auth DB busy, try again.", next_url, 503)
        if key_status == "ok" and key_id is not None:
            _set_session("key", key_id)
            return _redirect_no_store(next_url)

        error = "Falsches Passwort oder ungültiger Key."

    try:
        total_ms = (time.perf_counter() - request_start) * 1000
        print(f"AUTH_LOGIN_END duration_ms={total_ms:.2f} status=render_form")
    except Exception:
        pass
    return _render_private_access(error, next_url)


@private_access_bp.route("/logout", methods=["GET", "POST"])
def logout():
    session.clear()
    resp = redirect(url_for("private_access.private_access_gate"))
    return _no_store_response(resp)


@private_access_bp.get("/api/debug/access")
@require_master
def debug_access():
    remote = (request.remote_addr or "").strip()
    xff = (request.headers.get("X-Forwarded-For") or "").strip()
    client_ip, source = _resolve_client_ip_for_whitelist()
    whitelisted = _is_ip_whitelisted(client_ip)
    return jsonify(
        {
            "ok": True,
            "remote_addr": remote,
            "x_forwarded_for": xff,
            "resolved_client_ip": client_ip,
            "client_ip_source": source,
            "auth_level": get_current_role(),
            "whitelisted": whitelisted,
            "internal_allowed": _is_internal_allowed(remote),
            "forwarded_headers": {
                "x_forwarded_for": _truncate_header(xff),
            },
        }
    )


@private_access_bp.get("/api/debug/auth_health")
@require_localhost
def debug_auth_health():
    timings_ms: dict[str, float] = {}
    db_open_ok = False
    select_ok = False
    last_error = ""

    t0 = time.perf_counter()
    conn = None
    try:
        ensure_auth_schema()
        conn = get_auth_db()
        db_open_ok = True
        timings_ms["open_db"] = round((time.perf_counter() - t0) * 1000, 2)

        t1 = time.perf_counter()
        conn.execute("SELECT COUNT(1) AS cnt FROM access_keys").fetchone()
        select_ok = True
        timings_ms["select_access_keys"] = round((time.perf_counter() - t1) * 1000, 2)
    except sqlite3.Error as exc:
        last_error = str(exc)
    finally:
        if conn is not None:
            conn.close()

    return jsonify(
        {
            "ok": db_open_ok and select_ok,
            "db_open_ok": db_open_ok,
            "select_ok": select_ok,
            "last_error": last_error,
            "timings_ms": timings_ms,
        }
    )


@private_access_bp.get("/api/settings/theme")
@require_key_or_master
def get_theme_preference():
    theme = get_user_theme_preference()
    if theme not in {"dark", "light"}:
        theme = "dark"
    return jsonify({"ok": True, "theme": theme})


@private_access_bp.post("/api/settings/theme")
@require_key_or_master
def set_theme_preference():
    if not verify_csrf():
        return jsonify({"ok": False, "error": "csrf_invalid"}), 400
    payload = request.get_json(silent=True) or {}
    theme = str(payload.get("theme") or "").strip().lower()
    if theme not in {"dark", "light"}:
        return jsonify({"ok": False, "error": "invalid_theme"}), 400
    role = get_current_role()
    if role == "key":
        if not _set_user_theme_preference(theme):
            return jsonify({"ok": False, "error": "save_failed"}), 500
    return jsonify({"ok": True, "theme": theme})


# ---------- key management API ----------
@private_access_bp.get("/api/settings/access_keys")
@require_master
def list_access_keys():
    ensure_auth_schema()
    try:
        def _op(conn):
            return conn.execute(
                """
                SELECT
                    a.id,
                    a.label,
                    a.created_at,
                    a.expires_at,
                    a.revoked_at,
                    a.last_seen_at,
                    a.last_ip,
                    a.last_user_agent,
                    (
                        SELECT COUNT(1)
                        FROM access_log
                        WHERE key_id = a.id
                    ) AS hits_count,
                    (
                        SELECT path
                        FROM access_log
                        WHERE key_id = a.id
                        ORDER BY ts DESC
                        LIMIT 1
                    ) AS last_path
                FROM access_keys AS a
                ORDER BY a.created_at DESC
                """
            ).fetchall()

        rows = _with_auth_conn(_op, commit=False)
        return jsonify(
            {
                "ok": True,
                "items": [
                    {
                        "id": int(row["id"]),
                        "label": row["label"],
                        "created_at": row["created_at"],
                        "expires_at": row["expires_at"],
                        "revoked_at": row["revoked_at"],
                        "last_seen_at": row["last_seen_at"],
                        "last_ip": row["last_ip"],
                        "last_user_agent": row["last_user_agent"],
                        "hits_count": int(row["hits_count"] or 0),
                        "last_path": row["last_path"],
                    }
                    for row in rows
                ],
            }
        )
    except sqlite3.OperationalError as exc:
        if _is_busy_error(exc):
            return jsonify({"ok": False, "error": "db_busy"}), 503
        raise


@private_access_bp.post("/api/settings/access_keys")
@require_master
def create_access_key():
    ensure_auth_schema()
    body = request.get_json(silent=True) or {}
    label = str(body.get("label") or "").strip()
    if not label:
        return jsonify({"ok": False, "error": "label_required"}), 400

    days = body.get("days")
    try:
        days_int = max(1, min(365, int(days or 30)))
    except Exception:
        days_int = 30

    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(days=days_int)

    plain_key = f"thk_{secrets.token_urlsafe(24)}"
    try:
        def _op(conn):
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO access_keys (label, key_hash, created_at, expires_at, revoked_at, last_seen_at, last_ip, last_user_agent)
                VALUES (?, ?, ?, ?, NULL, NULL, NULL, NULL)
                """,
                (
                    label,
                    _hash_secret(plain_key),
                    now.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
                    expires_at.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
                ),
            )
            return int(cur.lastrowid)

        key_id = _with_auth_conn(_op, commit=True)
        return jsonify({"ok": True, "item": {"id": key_id, "label": label}, "plain_key": plain_key})
    except sqlite3.OperationalError as exc:
        if _is_busy_error(exc):
            return jsonify({"ok": False, "error": "db_busy"}), 503
        raise


@private_access_bp.post("/api/settings/access_keys/<int:key_id>/extend")
@require_master
def extend_access_key(key_id: int):
    body = request.get_json(silent=True) or {}
    raw = (body.get("expires_at") or "").strip()
    if not raw:
        return jsonify({"ok": False, "error": "expires_at_required"}), 400

    exp = _parse_ts(raw)
    if not exp or exp <= datetime.now(timezone.utc):
        return jsonify({"ok": False, "error": "expires_at_invalid"}), 400

    try:
        def _op(conn):
            cur = conn.cursor()
            cur.execute(
                "UPDATE access_keys SET expires_at=? WHERE id=?",
                (exp.replace(microsecond=0).isoformat().replace("+00:00", "Z"), key_id),
            )
            return cur.rowcount

        rowcount = _with_auth_conn(_op, commit=True)
        if rowcount <= 0:
            return jsonify({"ok": False, "error": "not_found"}), 404
        return jsonify({"ok": True})
    except sqlite3.OperationalError as exc:
        if _is_busy_error(exc):
            return jsonify({"ok": False, "error": "db_busy"}), 503
        raise


@private_access_bp.post("/api/settings/access_keys/<int:key_id>/revoke")
@require_master
def revoke_access_key(key_id: int):
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    try:
        def _op(conn):
            cur = conn.cursor()
            cur.execute("UPDATE access_keys SET revoked_at=COALESCE(revoked_at, ?) WHERE id=?", (now, key_id))
            return cur.rowcount

        rowcount = _with_auth_conn(_op, commit=True)
        if rowcount <= 0:
            return jsonify({"ok": False, "error": "not_found"}), 404
        return jsonify({"ok": True})
    except sqlite3.OperationalError as exc:
        if _is_busy_error(exc):
            return jsonify({"ok": False, "error": "db_busy"}), 503
        raise


@private_access_bp.delete("/api/settings/access_keys/<int:key_id>")
@require_master
def delete_access_key(key_id: int):
    try:
        def _op(conn):
            cur = conn.cursor()
            cur.execute("DELETE FROM access_log WHERE key_id=?", (key_id,))
            cur.execute("DELETE FROM user_preferences WHERE key_id=?", (key_id,))
            cur.execute("DELETE FROM access_keys WHERE id=?", (key_id,))
            return cur.rowcount

        rowcount = _with_auth_conn(_op, commit=True)
        if rowcount <= 0:
            return jsonify({"ok": False, "error": "not_found"}), 404
        return jsonify({"ok": True})
    except sqlite3.OperationalError as exc:
        if _is_busy_error(exc):
            return jsonify({"ok": False, "error": "db_busy"}), 503
        raise
    except sqlite3.IntegrityError:
        return jsonify({"ok": False, "error": "constraint"}), 400


@private_access_bp.post("/api/settings/access_keys/<int:key_id>/restore")
@require_master
def restore_access_key(key_id: int):
    body = request.get_json(silent=True) or {}
    raw = (body.get("expires_at") or "").strip()
    exp = None
    if raw:
        exp = _parse_ts(raw)
    now = datetime.now(timezone.utc)

    try:
        def _op(conn):
            cur = conn.cursor()
            current = cur.execute("SELECT expires_at FROM access_keys WHERE id=? LIMIT 1", (key_id,)).fetchone()
            if not current:
                return "not_found"
            target = exp or _parse_ts(current["expires_at"])
            if not target or target <= now:
                return "invalid_exp"
            cur.execute(
                "UPDATE access_keys SET expires_at=?, revoked_at=NULL WHERE id=?",
                (target.replace(microsecond=0).isoformat().replace("+00:00", "Z"), key_id),
            )
            return "ok"

        result = _with_auth_conn(_op, commit=True)
        if result == "not_found":
            return jsonify({"ok": False, "error": "not_found"}), 404
        if result == "invalid_exp":
            return jsonify({"ok": False, "error": "expires_at_invalid"}), 400
        return jsonify({"ok": True})
    except sqlite3.OperationalError as exc:
        if _is_busy_error(exc):
            return jsonify({"ok": False, "error": "db_busy"}), 503
        raise


@private_access_bp.get("/api/settings/access_keys/<int:key_id>/activity")
@require_master
def access_key_activity(key_id: int):
    summary_raw = (request.args.get("summary") or "").strip().lower()
    wants_summary = summary_raw in {"1", "true", "yes", "y"}
    limit_raw = request.args.get("limit")
    try:
        limit_n = max(1, min(200, int(limit_raw or 50)))
    except Exception:
        limit_n = 50

    conn = get_auth_db()
    if wants_summary:
        rows = conn.execute(
            """
            SELECT ts, path
            FROM access_log
            WHERE key_id = ?
            ORDER BY ts DESC
            """,
            (key_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT ts, method, path, status, ip, user_agent
            FROM access_log
            WHERE key_id = ?
            ORDER BY ts DESC
            LIMIT ?
            """,
            (key_id, limit_n),
        ).fetchall()
    conn.close()

    if wants_summary:
        bucket: dict[str, dict[str, str | int]] = {}
        for row in rows:
            raw_path = row["path"] or ""
            if raw_path in _ACCESS_ACTIVITY_IGNORE_EXACT:
                continue
            if raw_path.startswith(_ACCESS_ACTIVITY_IGNORE_PREFIXES):
                continue
            norm = _normalize_activity_path(raw_path)
            if norm not in bucket:
                bucket[norm] = {"path": norm, "count": 0, "last_ts": row["ts"]}
            bucket[norm]["count"] = int(bucket[norm]["count"]) + 1
            if row["ts"] and (bucket[norm]["last_ts"] is None or row["ts"] > bucket[norm]["last_ts"]):
                bucket[norm]["last_ts"] = row["ts"]

        items = sorted(
            bucket.values(),
            key=lambda x: (int(x["count"]), str(x.get("last_ts") or "")),
            reverse=True,
        )
        return jsonify({"ok": True, "items": items})

    return jsonify(
        {
            "ok": True,
            "items": [
                {
                    "ts": row["ts"],
                    "method": row["method"],
                    "path": row["path"],
                    "status": row["status"],
                    "ip": row["ip"],
                    "user_agent": row["user_agent"],
                }
                for row in rows
            ],
        }
    )


def log_key_activity(response):
    conn = None
    try:
        role = get_current_role()
        if role != "key":
            return response

        path = (request.path or "").strip() or "/"
        if path in _ACTIVITY_LOG_SKIP_EXACT or path.startswith(_ACTIVITY_LOG_SKIP_PREFIXES):
            return response

        key_id = session.get("key_id")
        if not key_id:
            return response

        now_ts = int(time.time())
        activity_sig = f"{int(key_id)}|{request.method}|{path}|{int(response.status_code)}"
        last_sig = str(session.get("_activity_sig") or "")
        try:
            last_ts = int(session.get("_activity_sig_ts") or 0)
        except Exception:
            last_ts = 0
        if activity_sig == last_sig and (now_ts - last_ts) < _ACTIVITY_DEDUP_WINDOW_SECONDS:
            return response
        session["_activity_sig"] = activity_sig
        session["_activity_sig_ts"] = now_ts

        now = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        ip = getattr(g, "client_ip", "")
        ua = (request.headers.get("User-Agent") or "")[:400]
        try:
            last_seen_write_ts = int(session.get("_auth_last_seen_write_ts") or 0)
        except Exception:
            last_seen_write_ts = 0
        should_update_last_seen = (now_ts - last_seen_write_ts) >= _ACTIVITY_LAST_SEEN_UPDATE_SECONDS

        conn = get_auth_db()
        conn.execute(
            """
            INSERT INTO access_log (ts, key_id, session_id, method, path, status, ip, user_agent)
            VALUES (?, ?, NULL, ?, ?, ?, ?, ?)
            """,
            (now, int(key_id), request.method, path, int(response.status_code), ip, ua),
        )
        if should_update_last_seen:
            conn.execute(
                """
                UPDATE access_keys
                SET last_seen_at=?, last_ip=?, last_user_agent=?
                WHERE id=?
                """,
                (now, ip, ua, int(key_id)),
            )
            session["_auth_last_seen_write_ts"] = now_ts
        conn.commit()
    except sqlite3.Error:
        pass
    except Exception:
        pass
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    return response
