from __future__ import annotations

import argparse
import os
import secrets
from pathlib import Path
from urllib.parse import urlsplit

from argon2 import PasswordHasher


def _env_quote(value: str) -> str:
    return "'" + value.replace("'", "'\\''") + "'"


def generate(config_dir: Path, data_dir: Path, repo_root: Path, base_url: str) -> None:
    base_url = base_url.rstrip("/")
    parsed = urlsplit(base_url)
    if parsed.scheme != "https" or not parsed.netloc or parsed.path or parsed.query or parsed.fragment:
        raise ValueError("base URL must be an HTTPS origin")
    config_dir.mkdir(mode=0o700, parents=True, exist_ok=True); data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(config_dir, 0o700); os.chmod(data_dir, 0o700)
    database_root = data_dir / "database-view"
    database_root.mkdir(mode=0o700, exist_ok=False)
    for name in ("training.sqlite3", "plans.sqlite3", "hrv.sqlite3", "ernaehrung.sqlite3", "runs.sqlite3", "polar.sqlite3", "core.sqlite3"):
        placeholder = database_root / name
        placeholder.touch(mode=0o400, exist_ok=False)
    client_id = secrets.token_urlsafe(24); client_secret = secrets.token_urlsafe(48)
    signing_secret = secrets.token_urlsafe(48); passphrase = secrets.token_urlsafe(24)
    values = {
        "LIVA_MCP_REPO_ROOT": str(repo_root.resolve()), "LIVA_MCP_REMOTE_BASE_URL": base_url,
        "LIVA_MCP_OAUTH_ISSUER": base_url, "LIVA_MCP_OAUTH_CLIENT_ID": client_id,
        "LIVA_MCP_OAUTH_CLIENT_SECRET": client_secret, "LIVA_MCP_AUTH_SIGNING_SECRET": signing_secret,
        "LIVA_MCP_AUTH_PASSPHRASE_HASH": PasswordHasher().hash(passphrase),
        "LIVA_MCP_AUTH_DB": str((data_dir / "auth.sqlite3").resolve()),
        "LIVA_MCP_DB_ROOT": str(database_root.resolve()),
    }
    env_path = config_dir / "env"
    setup_path = config_dir / "chatgpt-setup.txt"
    if env_path.exists() or setup_path.exists():
        raise FileExistsError("Remote MCP configuration already exists")
    env_path.write_text("\n".join(f"{key}={_env_quote(value)}" for key, value in values.items()) + "\n", encoding="utf-8")
    setup_path.write_text(
        f"""LIVA Remote MCP
MCP URL: {base_url}/mcp
Fallback fixed OAuth Client ID: {client_id}
Fallback fixed OAuth Client Secret: {client_secret}
Initial personal passphrase: {passphrase}

ChatGPT web setup
1. Open ChatGPT in a web browser.
2. Enable Developer mode under Settings > Apps > Advanced settings (workspace controls may be required).
3. Open Settings > Apps and choose Create / + for a custom developer app.
4. Name: LIVA
5. Description: persönliche read-only Trainings-, Recovery-, Ernährungs- und Tagesdaten aus LIVA
6. Enter only the MCP URL shown above. OAuth discovery and Dynamic Client Registration are automatic.
7. Do not enter the fallback Client ID or Client Secret when ChatGPT offers automatic OAuth registration.
8. Create the app, complete the OAuth flow, and enter the personal passphrase on the LIVA authorization page.
9. Open a new normal chat and add LIVA from the Apps/tools menu.

First test prompt
Nutze ausschließlich die LIVA-App. Lies zuerst `liva_daily_snapshot` und analysiere meine aktuelle Tageslage. Trenne Daten, Interpretation und Empfehlung und nenne fehlende oder veraltete Komponenten.

Keep this file private. Do not paste these credentials into chats or logs.
""",
        encoding="utf-8",
    )
    os.chmod(env_path, 0o600); os.chmod(setup_path, 0o600)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--repo-root", type=Path, default=Path("/opt/liva"))
    parser.add_argument("--config-dir", type=Path, default=Path("/var/lib/liva/.config/liva-mcp-remote"))
    parser.add_argument("--data-dir", type=Path, default=Path("/var/lib/liva/.local/share/liva-mcp-remote"))
    args = parser.parse_args()
    generate(args.config_dir, args.data_dir, args.repo_root, args.base_url)
