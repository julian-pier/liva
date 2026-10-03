# /opt/liva/security/write_guard.py
from __future__ import annotations

import functools
import hmac
import ipaddress
import os
import platform
import re
import socket
import stat
import time
from pathlib import Path

import psutil
from flask import abort, g, jsonify, request

# ============================================================
# CONFIG
# ============================================================

TAILSCALE_RANGE = ipaddress.ip_network("100.64.0.0/10")
TAILSCALE_RANGE_V6 = ipaddress.ip_network("fd7a:115c:a1e0::/48")
WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
KEY_READONLY_POST_EXEMPT_PATHS = {
    "/api/training/validate_draft",
    "/api/training/last-slot-references",
    "/api/training/resolve-variation",
}
_DEFAULT_TRUSTED_PROXY_CIDRS = (
    "127.0.0.1/32",
    "::1/128",
    "100.64.0.0/10",
    "fd7a:115c:a1e0::/48",
    "172.16.0.0/12",
)

# n8n (docker) -> darf ohne Key schreiben
N8N_IPS = {
    ip.strip()
    for ip in (os.getenv("LIVA_N8N_IPS") or "172.29.0.2").split(",")
    if ip.strip()
}

# interne IPs (loopback + docker etc.)
INTERNAL_ALLOWED_IPS = {
    ip.strip()
    for ip in (os.getenv("LIVA_INTERNAL_ALLOWED_IPS") or "127.0.0.1,::1,172.29.0.2").split(",")
    if ip.strip()
}

ADMIN_PASSWORD = (os.getenv("LIVA_ADMIN_PASSWORD") or "").strip()




# ============================================================
# HELPERS
# ============================================================



_BOOT_TIME_CACHE = None


def _parse_networks(raw: str | None, defaults: tuple[str, ...]) -> tuple[ipaddress._BaseNetwork, ...]:
    entries = [p.strip() for p in (raw or "").split(",") if p.strip()]
    if not entries:
        entries = list(defaults)
    out = []
    for entry in entries:
        try:
            if "/" in entry:
                out.append(ipaddress.ip_network(entry, strict=False))
            else:
                out.append(ipaddress.ip_network(entry + ("/128" if ":" in entry else "/32"), strict=False))
        except Exception:
            continue
    return tuple(out)


TRUSTED_PROXIES = _parse_networks(
    os.getenv("LIVA_TRUSTED_PROXIES"),
    _DEFAULT_TRUSTED_PROXY_CIDRS,
)

def _safe(fn, default=None):
    try:
        return fn()
    except Exception:
        return default

def _bytes_to_mb(x):
    return round(x / 1024 / 1024, 1) if x is not None else None

def _bytes_to_gb(x):
    return round(x / 1024 / 1024 / 1024, 1) if x is not None else None


def _strip_ip_wrapping(value: str) -> str:
    text = (value or "").strip().strip('"').strip("'")
    if not text:
        return ""
    if text.lower().startswith("for="):
        text = text.split("=", 1)[1].strip()
    text = text.strip('"').strip("'")
    if text.startswith("["):
        end = text.find("]")
        if end > 0:
            text = text[1:end]
    if "%" in text:
        text = text.split("%", 1)[0]
    return text.strip()


def _normalize_ip_candidate(value: str) -> str:
    token = _strip_ip_wrapping(value)
    if not token or token.lower() in {"unknown", "_hidden", "obfuscated"}:
        return ""

    try:
        return str(ipaddress.ip_address(token))
    except Exception:
        pass

    if token.count(":") == 1 and "." in token:
        host = token.rsplit(":", 1)[0].strip()
        try:
            return str(ipaddress.ip_address(host))
        except Exception:
            return ""

    return ""


def _is_publicish_ip(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except Exception:
        return False
    return not (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_multicast
        or addr.is_reserved
        or addr.is_unspecified
    )


def _pick_best_ip(candidates: list[str]) -> str:
    clean = [ip for ip in candidates if ip]
    if not clean:
        return ""
    for ip in clean:
        if _is_publicish_ip(ip):
            return ip
    return clean[0]


def _extract_forwarded_candidates(header_value: str) -> list[str]:
    if not header_value:
        return []
    matches = re.findall(r'for=("[^"]+"|\[[^\]]+\]|[^;,]+)', header_value, flags=re.IGNORECASE)
    out = []
    for raw in matches:
        ip = _normalize_ip_candidate(raw)
        if ip:
            out.append(ip)
    return out


def _extract_xff_candidates(header_value: str) -> list[str]:
    if not header_value:
        return []
    out = []
    for part in header_value.split(","):
        ip = _normalize_ip_candidate(part)
        if ip:
            out.append(ip)
    return out


def _ip_in_networks(ip: str, networks: tuple[ipaddress._BaseNetwork, ...]) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except Exception:
        return False
    return any(addr in network for network in networks)


def _is_trusted_proxy(ip: str) -> bool:
    return bool(ip and _ip_in_networks(ip, TRUSTED_PROXIES))


def get_client_ip_details() -> dict[str, object]:
    remote_raw = (request.remote_addr or "").strip()
    remote_ip = _normalize_ip_candidate(remote_raw)
    trusted_proxy = _is_trusted_proxy(remote_ip)

    forwarded = (request.headers.get("Forwarded") or "").strip()
    xff = (request.headers.get("X-Forwarded-For") or "").strip()
    x_real = (request.headers.get("X-Real-IP") or "").strip()

    client_ip = remote_ip
    source = "remote_addr"

    if trusted_proxy:
        forwarded_ip = _pick_best_ip(_extract_forwarded_candidates(forwarded))
        if forwarded_ip:
            client_ip = forwarded_ip
            source = "forwarded"
        else:
            xff_ip = _pick_best_ip(_extract_xff_candidates(xff))
            if xff_ip:
                client_ip = xff_ip
                source = "x_forwarded_for"
            else:
                x_real_ip = _normalize_ip_candidate(x_real)
                if x_real_ip:
                    client_ip = x_real_ip
                    source = "x_real_ip"

    return {
        "remote_addr": remote_raw,
        "remote_ip": remote_ip,
        "client_ip": client_ip or "",
        "source": source,
        "trusted_proxy": trusted_proxy,
        "headers": {
            "forwarded": forwarded,
            "x_forwarded_for": xff,
            "x_real_ip": x_real,
        },
    }


def get_client_ip() -> str:
    return str(get_client_ip_details().get("client_ip") or "")

def is_tailscale_ip(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
        return addr in TAILSCALE_RANGE or addr in TAILSCALE_RANGE_V6
    except Exception:
        return False


def _json_error(status: int, code: str, detail: str = ""):
    payload = {"ok": False, "error": code}
    if detail:
        payload["detail"] = detail
    return jsonify(payload), status


def _env_flag(name: str, default: bool = False) -> bool:
    raw = (os.getenv(name) or "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _ai_key_pair() -> tuple[str, str]:
    read_key = (os.getenv("LIVA_AI_READ_KEY") or "").strip()
    write_key = (os.getenv("LIVA_AI_WRITE_KEY") or "").strip()
    if read_key and not write_key:
        write_key = read_key
    if write_key and not read_key:
        read_key = write_key
    return read_key, write_key


def _valid_liva_mcp_bridge_token(token: str) -> bool:
    if (
        request.path not in {
            "/api/v2/actions/liva/read",
            "/api/v2/actions/liva/act",
            "/api/v2/actions/training/today/card/refresh",
        }
        or request.headers.get("X-LIVA-MCP-Bridge") != "1"
    ):
        return False
    try:
        if not ipaddress.ip_address(request.remote_addr or "").is_loopback:
            return False
        raw_path = (os.getenv("LIVA_MCP_ACTIONS_TOKEN_FILE") or "").strip()
        if not raw_path:
            return False
        path = Path(raw_path).expanduser()
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or path.is_symlink() or metadata.st_mode & 0o077:
            return False
        expected = path.read_text(encoding="utf-8").strip()
        return len(expected) >= 32 and hmac.compare_digest(token, expected)
    except (OSError, ValueError):
        return False

def has_admin_header() -> bool:
    if not ADMIN_PASSWORD:
        return False
    # du kannst beides nutzen
    return (request.headers.get("X-Admin-Password") or request.headers.get("X-Admin-Key") or "") == ADMIN_PASSWORD

def _is_ai_path() -> bool:
    p = request.path or ""
    return (
        p.startswith("/api/ai/")
        or p.startswith("/api/aiw/")
        or p.startswith("/api/v2/actions/")
        or p.startswith("/jit/ai")
    )

# ============================================================
# GLOBAL SECURITY LAYER (called in app.before_request)
# ============================================================

def enforce_ip_whitelist():
    """
    DEAKTIVIERT (du wolltest: jeder darf READ).
    Wenn du später wieder willst -> hier einbauen.
    """
    return

def enforce_write_protection():
    """
    Policy (dein Wunsch):
    - READ: komplett offen
    - WRITE: nur
        -> Tailscale-IPs (100.64.0.0/10) = dürfen ALLES
        -> N8N IP(s) = dürfen schreiben ohne Key
        -> interne IPs (localhost/docker)
    - AI endpoints (/api/ai*, /api/aiw*) regeln Auth selbst (Key/Intent)
    """

    # AI APIs entscheiden selbst (require_ai_write etc.)
    if _is_ai_path():
        return

    # Login-Gate darf POSTen, sonst greift die globale Write-Sperre
    path = request.path or ""
    if path.startswith("/api/pc-status/"):
        return
    if path in {
        "/_private_access", "/login", "/logout",
        "/api/activity/ingest", "/api/activity/iphone/daily",
    }:
        return

    if request.method not in WRITE_METHODS:
        return

    auth_level = getattr(g, "auth_level", "none")
    if auth_level in {"trusted", "master"}:
        return
    if auth_level == "key":
        if path in KEY_READONLY_POST_EXEMPT_PATHS:
            return
        abort(403, description="Gastzugang ist schreibgeschuetzt.")

    ip = get_client_ip()

    # Tailscale darf ALLES
    if is_tailscale_ip(ip):
        return

    # internal ok
    if ip in INTERNAL_ALLOWED_IPS:
        return

    # n8n ok
    if ip in N8N_IPS:
        return

    abort(403, description="Aktion nicht erlaubt (kein Schreibzugriff).")

# ============================================================
# DECORATORS
# ============================================================

def require_admin(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        auth_level = getattr(g, "auth_level", "none")
        if auth_level in {"trusted", "master"} or (
            auth_level == "key" and request.method in {"GET", "HEAD", "OPTIONS"}
        ):
            return fn(*args, **kwargs)

        ip = get_client_ip()

        if is_tailscale_ip(ip) or ip in INTERNAL_ALLOWED_IPS or has_admin_header():
            return fn(*args, **kwargs)

        return jsonify({"success": False, "message": "Admin-Rechte erforderlich."}), 403
    return wrapper

def require_intent(expected: str):
    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            intent = (request.headers.get("X-AI-Intent") or "").strip()

            if not intent and request.is_json:
                try:
                    intent = (request.get_json(silent=True) or {}).get("intent") or ""
                    intent = str(intent).strip()
                except Exception:
                    intent = ""

            if not intent:
                intent = (request.args.get("intent") or "").strip()

            if intent != expected:
                return _json_error(403, "intent_missing_or_invalid", f"Expected '{expected}'")

            return fn(*args, **kwargs)
        return wrapper
    return decorator

def _extract_ai_token():
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
    ):
        token = (request.headers.get(header) or "").strip()
        if token:
            return token

    for arg in ("api_key", "apikey", "key", "token"):
        token = (request.args.get(arg) or "").strip()
        if token:
            return token

    return ""


def require_ai_read(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        ai_read_api_key, ai_write_api_key = _ai_key_pair()
        if request.path in {
            "/api/ai/health",
            "/api/ai/health/",
            "/api/ai/hrv/summary",
            "/api/ai/hrv/summary/",
            "/api/ai/hrv/series",
            "/api/ai/hrv/series/",
            "/api/ai/hrv/series_compact",
            "/api/ai/hrv/series_compact/",
        }:
            return fn(*args, **kwargs)
        token = _extract_ai_token()
        if _valid_liva_mcp_bridge_token(token):
            return fn(*args, **kwargs)
        # READ akzeptiert Read- oder Write-Key
        if not ai_read_api_key and not ai_write_api_key:
            return _json_error(500, "ai_key_missing", "Set LIVA_AI_READ_KEY or LIVA_AI_WRITE_KEY")
        if token != ai_read_api_key and token != ai_write_api_key:
            return _json_error(401, "unauthorized")

        return fn(*args, **kwargs)
    return wrapper


def require_ai_write(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        _, ai_write_api_key = _ai_key_pair()
        if not _env_flag("AI_WRITE_ENABLED", False):
            return _json_error(403, "ai_write_disabled", "Set AI_WRITE_ENABLED=1")
        token = _extract_ai_token()
        if _valid_liva_mcp_bridge_token(token):
            return fn(*args, **kwargs)
        if not ai_write_api_key:
            return _json_error(500, "ai_write_key_missing", "Set LIVA_AI_WRITE_KEY")

        if token != ai_write_api_key:
            return _json_error(401, "unauthorized")

        return fn(*args, **kwargs)
    return wrapper


# ============================================================
# PI STATS (für /settings) — Output bleibt gleich
# ============================================================

def get_pi_stats():
    global _BOOT_TIME_CACHE
    if _BOOT_TIME_CACHE is None:
        _BOOT_TIME_CACHE = _safe(lambda: psutil.boot_time(), None)

    cpu_percent = _safe(lambda: psutil.cpu_percent(interval=0.08), None)
    cpu_cores = _safe(lambda: psutil.cpu_count(logical=True), None)
    cpu_freq = _safe(lambda: psutil.cpu_freq().current if psutil.cpu_freq() else None, None)

    mem = _safe(lambda: psutil.virtual_memory(), None)
    ram_used_mb = _bytes_to_mb(mem.used) if mem else None
    ram_total_mb = _bytes_to_mb(mem.total) if mem else None
    ram_percent = round(mem.percent, 1) if mem else None

    disk = _safe(lambda: psutil.disk_usage("/"), None)
    disk_used_gb = _bytes_to_gb(disk.used) if disk else None
    disk_total_gb = _bytes_to_gb(disk.total) if disk else None
    disk_percent = round(disk.percent, 1) if disk else None

    load = _safe(lambda: os.getloadavg(), None)
    load_str = f"{load[0]:.2f} / {load[1]:.2f} / {load[2]:.2f}" if load else None

    if _BOOT_TIME_CACHE is None:
        uptime_str = None
    else:
        uptime_s = max(0, int(time.time() - _BOOT_TIME_CACHE))
        uptime_h = uptime_s // 3600
        uptime_m = (uptime_s % 3600) // 60
        uptime_str = f"{uptime_h}h {uptime_m}m"

    return {
        "host": socket.gethostname(),
        "os": f"{platform.system()} {platform.release()}",
        "uptime": uptime_str,

        "cpu_percent": round(cpu_percent, 1) if cpu_percent is not None else None,
        "cpu_cores": cpu_cores,
        "cpu_freq_mhz": round(cpu_freq) if cpu_freq else None,

        "ram_used_mb": ram_used_mb,
        "ram_total_mb": ram_total_mb,
        "ram_percent": ram_percent,

        "disk_used_gb": disk_used_gb,
        "disk_total_gb": disk_total_gb,
        "disk_percent": disk_percent,

        "load": load_str,
    }
