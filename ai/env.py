from __future__ import annotations

import os
from pathlib import Path
import re
import hashlib


def _read_dotenv_value(dotenv_path: Path, key: str) -> str | None:
    try:
        raw = dotenv_path.read_text(encoding="utf-8")
    except Exception:
        return None

    for line in raw.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if "=" not in s:
            continue
        k, v = s.split("=", 1)
        if k.strip() != key:
            continue
        val = v.strip().strip("'").strip('"').strip()
        return val or None
    return None


def _read_systemd_override_env_value(override_path: Path, key: str) -> str | None:
    """
    Parses a systemd override.conf that contains lines like:
      Environment="OPENAI_API_KEY=..." "OPENAI_MODEL=..."
    """
    try:
        raw = override_path.read_text(encoding="utf-8")
    except Exception:
        return None

    # capture OPENAI_API_KEY=... inside quotes OR unquoted
    m = re.search(rf"\b{re.escape(key)}=([^\s\"']+)", raw)
    if not m:
        return None
    val = (m.group(1) or "").strip().strip("'").strip('"').strip()
    return val or None


def get_openai_api_key_info() -> dict:
    """
    Returns {key, source, fingerprint}. fingerprint is sha256(key)[:12] (safe to show).
    source is one of: systemd_override, env, dotenv, none
    """
    override_candidates = [
        Path("/etc/systemd/system/liva-kienzl-refresh.service.d/override.conf"),
        Path("/etc/systemd/system/liva-ai.service.d/override.conf"),
        Path("/etc/systemd/system/liva.service.d/override.conf"),
    ]
    override_val = None
    override_src = None
    for p in override_candidates:
        val = _read_systemd_override_env_value(p, "OPENAI_API_KEY")
        if val:
            override_val = val
            override_src = f"systemd_override:{p}"
            break

    env_val = (os.environ.get("OPENAI_API_KEY") or "").strip() or None

    base_dir = Path(__file__).resolve().parents[1]
    dotenv = base_dir / ".env"
    dotenv_val = _read_dotenv_value(dotenv, "OPENAI_API_KEY")

    # If a systemd override exists and differs from env, prefer override (allows updating without restart).
    key = None
    source = "none"
    if override_val and override_val != env_val:
        key = override_val
        source = override_src or "systemd_override"
    elif env_val:
        key = env_val
        source = "env"
    elif dotenv_val:
        key = dotenv_val
        source = "dotenv"

    fp = None
    if key:
        fp = hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]

    return {"key": key, "source": source, "fingerprint": fp}


def get_openai_api_key() -> str | None:
    """
    LIVA convention: OPENAI_API_KEY lives in process env (systemd EnvironmentFile)
    and/or in repo `.env`.
    """
    info = get_openai_api_key_info()
    key = info.get("key")
    if not key:
        return None
    os.environ["OPENAI_API_KEY"] = str(key)
    return str(key)
