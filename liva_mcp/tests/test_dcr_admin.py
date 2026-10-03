from __future__ import annotations

import time

from liva_mcp.auth_store import AuthStore
from liva_mcp.dcr_admin import _backup, _public, _stale, main


def add(store: AuthStore, client_id: str, issued_at: int) -> None:
    assert store.create_dynamic_client(
        client_id=client_id, secret_hash_value="$argon2id$placeholder", redirect_uris=["https://chatgpt.com/connector/oauth/test"],
        scope="liva.read", grant_types=["authorization_code"], response_types=["code"],
        token_endpoint_auth_method="client_secret_basic", issued_at=issued_at,
    )


def test_stale_cleanup_candidates_exclude_recent_clients(tmp_path):
    store = AuthStore(tmp_path / "auth.sqlite3")
    add(store, "old", 1)
    add(store, "recent", int(time.time()))
    rows = store.list_dynamic_clients()
    candidates = _stale(rows, 30)
    assert [row["client_id"] for row in candidates] == ["old"]
    assert "secret_hash" not in _public(rows[0])


def test_backup_before_cleanup_and_retire_is_recoverable(tmp_path):
    source = tmp_path / "auth.sqlite3"
    store = AuthStore(source)
    add(store, "old", 1)
    backup = _backup(source, tmp_path / "backups")
    assert backup.exists() and (backup.stat().st_mode & 0o777) == 0o600
    assert store.retire_dynamic_clients(["old"], reason="test") == 1
    restored = AuthStore(tmp_path / "restored.sqlite3")
    import sqlite3
    with sqlite3.connect(backup) as conn:
        assert conn.execute("SELECT COUNT(*) FROM dynamic_clients WHERE active=1").fetchone()[0] == 1
    assert restored.active_dynamic_client_count() == 0


def test_cli_env_file_reads_quoted_path_and_never_prints_secret_fields(tmp_path, capsys, monkeypatch):
    source = tmp_path / "auth.sqlite3"
    store = AuthStore(source)
    add(store, "one", int(time.time()))
    env_file = tmp_path / "service.env"
    env_file.write_text(f"LIVA_MCP_AUTH_DB='{source}'\n")
    monkeypatch.setenv("LIVA_MCP_AUTH_DB", str(tmp_path / "wrong.sqlite3"))
    assert main(["--env-file", str(env_file), "list"]) == 0
    output = capsys.readouterr().out
    assert '"count": 1' in output
    assert "secret_hash" not in output and "client_secret" not in output


def test_cleanup_apply_refuses_to_disconnect_existing_chats(tmp_path):
    source = tmp_path / "auth.sqlite3"
    store = AuthStore(source)
    add(store, "old", 1)
    import pytest
    with pytest.raises(SystemExit, match="must remain connected"):
        main(["--auth-db", str(source), "cleanup", "--older-than-days", "30", "--apply"])
    assert store.get_dynamic_client("old") is not None


def test_store_repairs_refresh_capable_client_scope(tmp_path):
    source = tmp_path / "auth.sqlite3"
    store = AuthStore(source)
    assert store.create_dynamic_client(
        client_id="chatgpt", secret_hash_value="$argon2id$placeholder",
        redirect_uris=["https://chatgpt.com/connector/oauth/test"], scope="liva.read liva.write",
        grant_types=["authorization_code", "refresh_token"], response_types=["code"],
        token_endpoint_auth_method="client_secret_basic", issued_at=1,
    )
    assert AuthStore(source).get_dynamic_client("chatgpt")["scope"] == "liva.read liva.write offline_access"
