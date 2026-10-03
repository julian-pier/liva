from __future__ import annotations

import argparse
import json
import os
import sqlite3
import time
from pathlib import Path

from .auth_store import AuthStore


def _load_env(path: Path) -> None:
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ[key] = value


def _store(args: argparse.Namespace) -> AuthStore:
    if args.env_file:
        _load_env(Path(args.env_file))
    value = args.auth_db or os.environ.get("LIVA_MCP_AUTH_DB", "").strip()
    if not value:
        raise SystemExit("An auth database or --env-file is required")
    return AuthStore(Path(value).resolve())


def _public(row: dict) -> dict:
    return {
        "client_hash": row["client_hash"],
        "fingerprint": row["fingerprint"],
        "redirect_uris": row["redirect_uris"],
        "scope": row["scope"],
        "grant_types": row["grant_types"],
        "issued_at": row["issued_at"],
        "last_used_at": row.get("last_used_at"),
        "active": bool(row["active"]),
        "active_access_tokens": row["active_access_tokens"],
        "active_refresh_tokens": row["active_refresh_tokens"],
        "pending_authorization_requests": row["pending_authorization_requests"],
        "pending_authorization_codes": row["pending_authorization_codes"],
    }


def _backup(source: Path, directory: Path) -> Path:
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    destination = directory / f"liva-auth-before-dcr-cleanup-{int(time.time())}.sqlite3"
    with sqlite3.connect(source) as src, sqlite3.connect(destination) as dst:
        src.backup(dst)
    os.chmod(destination, 0o600)
    return destination


def _stale(rows: list[dict], older_days: int) -> list[dict]:
    cutoff = int(time.time()) - older_days * 86400
    return [row for row in rows if row["active"] and row["issued_at"] < cutoff and (row.get("last_used_at") or 0) < cutoff and row["active_access_tokens"] == 0 and row["active_refresh_tokens"] == 0 and row["pending_authorization_requests"] == 0 and row["pending_authorization_codes"] == 0]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Safely inspect and clean stale LIVA dynamic OAuth clients")
    parser.add_argument("--env-file", default="", help="EnvironmentFile containing LIVA_MCP_AUTH_DB")
    parser.add_argument("--auth-db", default="", help="Explicit auth database path")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="List safe metadata without client secrets")
    cleanup = sub.add_parser("cleanup", help="Preview stale dynamic clients (non-destructive)")
    cleanup.add_argument("--older-than-days", type=int, default=30)
    cleanup.add_argument("--apply", action="store_true", help=argparse.SUPPRESS)
    cleanup.add_argument("--backup-dir", default="/var/lib/liva/.local/share/liva-mcp-remote/backups")
    args = parser.parse_args(argv)
    if args.command == "list":
        rows = _store(args).list_dynamic_clients()
        print(json.dumps({"count": len(rows), "clients": [_public(row) for row in rows]}, ensure_ascii=False, sort_keys=True))
        return 0
    if args.older_than_days < 1:
        raise SystemExit("--older-than-days must be positive")
    if args.apply:
        raise SystemExit("Refusing to retire OAuth clients: existing ChatGPT conversations must remain connected")
    store = _store(args)
    rows = store.list_dynamic_clients()
    candidates = _stale(rows, args.older_than_days)
    result = {"count_before": sum(1 for row in rows if row["active"]), "candidate_count": len(candidates), "candidates": [_public(row) for row in candidates], "applied": False}
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
