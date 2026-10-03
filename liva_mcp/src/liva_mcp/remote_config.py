from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from .config import read_actions_token_file


@dataclass(frozen=True)
class RemoteConfig:
    repo_root: Path
    database_root: Path
    base_url: str
    issuer: str
    client_id: str
    client_secret: str
    signing_secret: str
    passphrase_hash: str
    auth_db: Path
    host: str = "127.0.0.1"
    port: int = 8765
    access_token_seconds: int = 3600
    # A conversation can remain unused for months. Refresh tokens rotate on
    # every use and are explicitly revocable, so keep the dormant binding valid
    # across long ChatGPT history gaps instead of forcing a reconnect.
    refresh_token_seconds: int = 315_360_000
    authorization_code_seconds: int = 120
    authorization_request_seconds: int = 300
    nutrition_write_db: Path | None = None
    core_write_db: Path | None = None
    smoke_test_client_id: str | None = None
    memos_base_url: str | None = None
    memos_read_token: str | None = None
    memos_write_token: str | None = None
    actions_base_url: str | None = None
    actions_token: str | None = None

    def __post_init__(self) -> None:
        for value in (self.base_url, self.issuer):
            parsed = urlsplit(value)
            if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path:
                raise ValueError("Remote MCP public URLs must be HTTPS origins")
        if self.base_url != self.issuer:
            raise ValueError("OAuth issuer must equal the remote MCP origin")
        if len(self.client_secret.encode()) < 32 or len(self.signing_secret.encode()) < 32:
            raise ValueError("Remote MCP secrets are too short")
        resolved_root = self.repo_root.resolve(); resolved_auth = self.auth_db.resolve(); resolved_databases = self.database_root.resolve()
        if resolved_auth == resolved_root or resolved_root in resolved_auth.parents:
            raise ValueError("The auth database must be outside the repository")
        if resolved_databases == resolved_root or resolved_root in resolved_databases.parents:
            raise ValueError("The remote database view must be outside the repository")
        if self.actions_base_url:
            parsed = urlsplit(self.actions_base_url)
            if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"} or parsed.path not in {"", "/"} or parsed.query or parsed.fragment or parsed.username or parsed.password:
                raise ValueError("Invalid actions bridge URL")

    @property
    def resource(self) -> str:
        return f"{self.base_url}/mcp"

    @property
    def write_db(self) -> Path:
        return self.auth_db.parent / "liva_mcp_writes.sqlite3"

    @property
    def public_host(self) -> str:
        parsed = urlsplit(self.base_url)
        assert parsed.netloc
        return parsed.netloc.lower()

    @classmethod
    def from_env(cls) -> "RemoteConfig":
        names = (
            "LIVA_MCP_REPO_ROOT", "LIVA_MCP_REMOTE_BASE_URL", "LIVA_MCP_OAUTH_ISSUER",
            "LIVA_MCP_OAUTH_CLIENT_ID", "LIVA_MCP_OAUTH_CLIENT_SECRET",
            "LIVA_MCP_AUTH_SIGNING_SECRET", "LIVA_MCP_AUTH_PASSPHRASE_HASH", "LIVA_MCP_AUTH_DB", "LIVA_MCP_DB_ROOT",
        )
        required = {name: os.environ.get(name, "").strip() for name in names}
        if any(not value for value in required.values()):
            raise ValueError("Remote MCP configuration is incomplete")
        base_url = required["LIVA_MCP_REMOTE_BASE_URL"].rstrip("/")
        issuer = required["LIVA_MCP_OAUTH_ISSUER"].rstrip("/")
        repo_root = Path(required["LIVA_MCP_REPO_ROOT"]).resolve()
        auth_db = Path(required["LIVA_MCP_AUTH_DB"]).resolve()
        database_root = Path(required["LIVA_MCP_DB_ROOT"]).resolve()
        nutrition_write_raw = os.environ.get("LIVA_MCP_NUTRITION_WRITE_DB", "").strip()
        core_write_raw = os.environ.get("LIVA_MCP_CORE_WRITE_DB", "").strip()
        smoke_test_client_id = os.environ.get("LIVA_MCP_SMOKE_TEST_CLIENT_ID", "").strip() or None
        memos_base_url = os.environ.get("LIVA_MCP_MEMOS_BASE_URL", "").strip() or None
        memos_read_token = os.environ.get("LIVA_MCP_MEMOS_READ_TOKEN", "").strip() or None
        memos_write_token = os.environ.get("LIVA_MCP_MEMOS_WRITE_TOKEN", "").strip() or None
        actions_base_url = os.environ.get("LIVA_MCP_ACTIONS_BASE_URL", "").strip() or None
        actions_token = os.environ.get("LIVA_MCP_ACTIONS_TOKEN", "").strip() or None
        if actions_token is None:
            actions_token = read_actions_token_file(os.environ.get("LIVA_MCP_ACTIONS_TOKEN_FILE", "").strip())
        return cls(
            repo_root=repo_root, database_root=database_root, base_url=base_url, issuer=issuer,
            client_id=required["LIVA_MCP_OAUTH_CLIENT_ID"], client_secret=required["LIVA_MCP_OAUTH_CLIENT_SECRET"],
            signing_secret=required["LIVA_MCP_AUTH_SIGNING_SECRET"], passphrase_hash=required["LIVA_MCP_AUTH_PASSPHRASE_HASH"],
            auth_db=auth_db, nutrition_write_db=Path(nutrition_write_raw).resolve() if nutrition_write_raw else None, core_write_db=Path(core_write_raw).resolve() if core_write_raw else None,
            memos_base_url=memos_base_url, memos_read_token=memos_read_token, memos_write_token=memos_write_token,
            actions_base_url=actions_base_url, actions_token=actions_token,
            smoke_test_client_id=smoke_test_client_id,
        )
