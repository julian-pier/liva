from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


def read_actions_token_file(raw_path: str) -> str | None:
    if not raw_path:
        return None
    path = Path(raw_path).expanduser()
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode) or path.is_symlink() or metadata.st_mode & 0o077:
        raise ValueError("Actions bridge token file must be a private regular file")
    token = path.read_text(encoding="utf-8").strip()
    if len(token) < 32 or len(token) > 4096:
        raise ValueError("Actions bridge token is missing or invalid")
    return token


@dataclass(frozen=True)
class Config:
    repo_root: Path
    database_root: Path | None = None
    memos_base_url: str | None = None
    memos_read_token: str | None = None
    memos_write_token: str | None = None
    write_state_path: Path | None = None
    nutrition_write_path: Path | None = None
    core_write_path: Path | None = None
    actions_base_url: str | None = None
    actions_token: str | None = None
    memory_vault: Path | None = None
    memory_runtime_dir: Path | None = None

    def __post_init__(self) -> None:
        if self.memos_base_url is None:
            pass
        else:
            parsed = urlsplit(self.memos_base_url)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
                or parsed.query
                or parsed.fragment
                or parsed.path not in {"", "/"}
            ):
                raise ValueError("Invalid usememos base URL")
        if self.actions_base_url:
            parsed = urlsplit(self.actions_base_url)
            if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"} or parsed.path not in {"", "/"} or parsed.query or parsed.fragment or parsed.username or parsed.password:
                raise ValueError("Invalid actions bridge URL")

    @classmethod
    def from_env(cls) -> "Config":
        default_root = Path(__file__).resolve().parents[3]
        root = Path(os.environ.get("LIVA_MCP_REPO_ROOT", default_root)).resolve()
        base_url = os.environ.get("LIVA_MCP_MEMOS_BASE_URL", "").strip() or None
        read_token = os.environ.get("LIVA_MCP_MEMOS_READ_TOKEN", "").strip() or None
        write_token = os.environ.get("LIVA_MCP_MEMOS_WRITE_TOKEN", "").strip() or None
        actions_base_url = os.environ.get("LIVA_MCP_ACTIONS_BASE_URL", "").strip() or None
        actions_token = os.environ.get("LIVA_MCP_ACTIONS_TOKEN", "").strip() or None
        if actions_token is None:
            actions_token = read_actions_token_file(os.environ.get("LIVA_MCP_ACTIONS_TOKEN_FILE", "").strip())
        database_root_raw = os.environ.get("LIVA_MCP_DB_ROOT", "").strip()
        database_root = Path(database_root_raw).resolve() if database_root_raw else None
        memory_vault_raw = os.environ.get("LIVA_MEMORY_VAULT", "").strip()
        memory_runtime_raw = os.environ.get("LIVA_MEMORY_RUNTIME_DIR", "").strip()
        return cls(repo_root=root, database_root=database_root, memos_base_url=base_url, memos_read_token=read_token, memos_write_token=write_token, actions_base_url=actions_base_url, actions_token=actions_token, memory_vault=Path(memory_vault_raw).resolve() if memory_vault_raw else None, memory_runtime_dir=Path(memory_runtime_raw).resolve() if memory_runtime_raw else None)

    def database_path(self, filename: str) -> Path:
        allowed = {
            "training.sqlite3",
            "plans.sqlite3",
            "hrv.sqlite3",
            "ernaehrung.sqlite3",
            "runs.sqlite3",
            "polar.sqlite3",
            "core.sqlite3",
        }
        if filename not in allowed:
            raise ValueError("Database is not allowlisted")
        return (self.database_root or (self.repo_root / "database")) / filename

    def write_database_path(self) -> Path:
        """Dedicated MCP-owned state; never a mutable application database."""
        return self.write_state_path or ((self.database_root or (self.repo_root / "database")) / "liva_mcp_writes.sqlite3")

    def memory_policy_path(self) -> Path:
        return self.repo_root / "config" / "liva_memory_policy.yaml"

    def nutrition_production_write_path(self) -> Path:
        """The narrowly scoped canonical database allowed for weight writes."""
        return self.nutrition_write_path or self.database_path("ernaehrung.sqlite3")

    def core_production_write_path(self) -> Path:
        return self.core_write_path or self.database_path("core.sqlite3")

    def memory_vault_path(self) -> Path:
        """The single LIVA Memory V2 vault; configurable for isolated tests."""
        default_data = Path(
            os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share"))
        )
        return self.memory_vault or (default_data / "liva" / "memory-vault")

    def memory_runtime_path(self) -> Path:
        """Runtime state is deliberately outside the versioned memory vault."""
        default = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state"))) / "liva-memory"
        return self.memory_runtime_dir or default
