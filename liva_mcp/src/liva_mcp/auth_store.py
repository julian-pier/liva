from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


SCHEMA = """
CREATE TABLE IF NOT EXISTS authorization_requests (
 request_hash TEXT PRIMARY KEY, client_id TEXT NOT NULL, redirect_uri TEXT NOT NULL,
 resource TEXT NOT NULL, scopes_json TEXT NOT NULL, code_challenge TEXT NOT NULL,
 state TEXT, csrf_hash TEXT, expires_at INTEGER NOT NULL, consumed_at INTEGER);
CREATE TABLE IF NOT EXISTS authorization_codes (
 code_hash TEXT PRIMARY KEY, client_id TEXT NOT NULL, redirect_uri TEXT NOT NULL,
 resource TEXT NOT NULL, scopes_json TEXT NOT NULL, code_challenge TEXT NOT NULL,
 expires_at INTEGER NOT NULL, consumed_at INTEGER);
CREATE TABLE IF NOT EXISTS access_tokens (
 token_hash TEXT PRIMARY KEY, jti TEXT UNIQUE NOT NULL, client_id TEXT NOT NULL,
 resource TEXT NOT NULL, scopes_json TEXT NOT NULL, expires_at INTEGER NOT NULL,
 family_id TEXT, revoked_at INTEGER);
CREATE TABLE IF NOT EXISTS refresh_tokens (
 token_hash TEXT PRIMARY KEY, client_id TEXT NOT NULL, resource TEXT NOT NULL,
 scopes_json TEXT NOT NULL, expires_at INTEGER NOT NULL, family_id TEXT NOT NULL,
 consumed_at INTEGER, revoked_at INTEGER);
CREATE TABLE IF NOT EXISTS dynamic_clients (
 client_id TEXT PRIMARY KEY, secret_hash TEXT NOT NULL, redirect_uris_json TEXT NOT NULL,
 scope TEXT NOT NULL, grant_types_json TEXT NOT NULL, response_types_json TEXT NOT NULL,
 token_endpoint_auth_method TEXT NOT NULL, issued_at INTEGER NOT NULL,
 active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)));
CREATE TABLE IF NOT EXISTS dynamic_client_audit (
 id INTEGER PRIMARY KEY, action TEXT NOT NULL, client_hash TEXT NOT NULL,
 fingerprint TEXT, created_at INTEGER NOT NULL, reason TEXT NOT NULL);
"""


def secret_hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class AuthStore:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.RLock()
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(path.parent, 0o700)
        with self.connect() as conn:
            conn.executescript(SCHEMA)
            columns = {row[1] for row in conn.execute("PRAGMA table_info(dynamic_clients)")}
            if "last_used_at" not in columns:
                conn.execute("ALTER TABLE dynamic_clients ADD COLUMN last_used_at INTEGER")
            if "retired_at" not in columns:
                conn.execute("ALTER TABLE dynamic_clients ADD COLUMN retired_at INTEGER")
        self.ensure_refresh_capable_client_scopes()
        os.chmod(path, 0o600)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            conn = sqlite3.connect(self.path, timeout=5)
            conn.row_factory = sqlite3.Row
            try:
                yield conn
                conn.commit()
            finally:
                conn.close()

    def put_request(self, raw: str, values: dict[str, Any]) -> None:
        with self.connect() as conn:
            conn.execute("INSERT INTO authorization_requests VALUES (?,?,?,?,?,?,?,?,?,NULL)", (
                secret_hash(raw), values["client_id"], values["redirect_uri"], values["resource"],
                json.dumps(values["scopes"]), values["code_challenge"], values.get("state"), None, values["expires_at"],
            ))

    def get_request(self, raw: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM authorization_requests WHERE request_hash=? AND consumed_at IS NULL", (secret_hash(raw),)).fetchone()
        return dict(row) if row else None

    def set_csrf(self, request: str, csrf: str) -> bool:
        with self.connect() as conn:
            return conn.execute("UPDATE authorization_requests SET csrf_hash=? WHERE request_hash=? AND consumed_at IS NULL AND expires_at>=?", (secret_hash(csrf), secret_hash(request), int(time.time()))).rowcount == 1

    def approve_request(self, request: str, csrf: str, code: str, expires_at: int) -> dict[str, Any] | None:
        now = int(time.time())
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM authorization_requests WHERE request_hash=?", (secret_hash(request),)).fetchone()
            if not row or row["consumed_at"] is not None or row["expires_at"] < now or row["csrf_hash"] != secret_hash(csrf):
                return None
            if conn.execute("UPDATE authorization_requests SET consumed_at=? WHERE request_hash=? AND consumed_at IS NULL", (now, secret_hash(request))).rowcount != 1:
                return None
            conn.execute("INSERT INTO authorization_codes VALUES (?,?,?,?,?,?,?,NULL)", (secret_hash(code), row["client_id"], row["redirect_uri"], row["resource"], row["scopes_json"], row["code_challenge"], expires_at))
            return dict(row)

    def get_recently_approved_request(self, request: str, csrf: str, not_before: int) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM authorization_requests WHERE request_hash=? AND csrf_hash=? AND consumed_at>=?",
                (secret_hash(request), secret_hash(csrf), not_before),
            ).fetchone()
        return dict(row) if row else None

    def is_code_replayable(self, raw: str) -> bool:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM authorization_codes WHERE code_hash=? AND consumed_at IS NULL AND expires_at>=?",
                (secret_hash(raw), int(time.time())),
            ).fetchone()
        return row is not None

    def get_code(self, raw: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM authorization_codes WHERE code_hash=? AND consumed_at IS NULL", (secret_hash(raw),)).fetchone()
        return dict(row) if row else None

    def inspect_code(self, raw: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM authorization_codes WHERE code_hash=?", (secret_hash(raw),)).fetchone()
        return dict(row) if row else None

    def consume_code(self, raw: str) -> bool:
        now = int(time.time())
        with self.connect() as conn:
            return conn.execute("UPDATE authorization_codes SET consumed_at=? WHERE code_hash=? AND consumed_at IS NULL AND expires_at>=?", (now, secret_hash(raw), now)).rowcount == 1

    def put_access(self, raw: str, *, jti: str, client_id: str, resource: str, scopes: list[str], expires_at: int, family_id: str | None) -> None:
        with self.connect() as conn:
            conn.execute("INSERT INTO access_tokens VALUES (?,?,?,?,?,?,?,NULL)", (secret_hash(raw), jti, client_id, resource, json.dumps(scopes), expires_at, family_id))

    def get_access(self, raw: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM access_tokens WHERE token_hash=? AND revoked_at IS NULL", (secret_hash(raw),)).fetchone()
        return dict(row) if row else None

    def put_refresh(self, raw: str, *, client_id: str, resource: str, scopes: list[str], expires_at: int, family_id: str) -> None:
        with self.connect() as conn:
            conn.execute("INSERT INTO refresh_tokens VALUES (?,?,?,?,?,?,NULL,NULL)", (secret_hash(raw), client_id, resource, json.dumps(scopes), expires_at, family_id))

    def get_refresh(self, raw: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM refresh_tokens WHERE token_hash=? AND consumed_at IS NULL AND revoked_at IS NULL", (secret_hash(raw),)).fetchone()
        return dict(row) if row else None

    def rotate_refresh(self, raw: str) -> bool:
        with self.connect() as conn:
            return conn.execute("UPDATE refresh_tokens SET consumed_at=? WHERE token_hash=? AND consumed_at IS NULL AND revoked_at IS NULL", (int(time.time()), secret_hash(raw))).rowcount == 1

    def revoke_family(self, family_id: str | None) -> None:
        if family_id:
            now = int(time.time())
            with self.connect() as conn:
                conn.execute("UPDATE access_tokens SET revoked_at=? WHERE family_id=? AND revoked_at IS NULL", (now, family_id))
                conn.execute("UPDATE refresh_tokens SET revoked_at=? WHERE family_id=? AND revoked_at IS NULL", (now, family_id))

    def revoke_access(self, raw: str) -> None:
        with self.connect() as conn:
            conn.execute("UPDATE access_tokens SET revoked_at=? WHERE token_hash=? AND revoked_at IS NULL", (int(time.time()), secret_hash(raw)))

    def create_dynamic_client(self, *, client_id: str, secret_hash_value: str, redirect_uris: list[str], scope: str, grant_types: list[str], response_types: list[str], token_endpoint_auth_method: str, issued_at: int, maximum_active: int = 50) -> bool:
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            count = conn.execute("SELECT COUNT(*) FROM dynamic_clients WHERE active=1").fetchone()[0]
            if count >= maximum_active:
                return False
            try:
                conn.execute(
                    "INSERT INTO dynamic_clients (client_id,secret_hash,redirect_uris_json,scope,grant_types_json,response_types_json,token_endpoint_auth_method,issued_at,active,last_used_at,retired_at) VALUES (?,?,?,?,?,?,?,?,1,NULL,NULL)",
                    (client_id, secret_hash_value, json.dumps(redirect_uris), scope, json.dumps(grant_types), json.dumps(response_types), token_endpoint_auth_method, issued_at),
                )
                conn.execute("INSERT INTO dynamic_client_audit (action,client_hash,fingerprint,created_at,reason) VALUES (?,?,?,?,?)", ("created", secret_hash(client_id)[:16], self.dynamic_fingerprint(redirect_uris, scope, grant_types, response_types, token_endpoint_auth_method), issued_at, "dynamic_registration"))
            except sqlite3.IntegrityError:
                return False
            return True

    def get_dynamic_client(self, client_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM dynamic_clients WHERE client_id=? AND active=1", (client_id,)).fetchone()
        return dict(row) if row else None

    def update_dynamic_client_scope(self, client_id: str, scope: str) -> bool:
        with self.connect() as conn:
            return conn.execute(
                "UPDATE dynamic_clients SET scope=? WHERE client_id=? AND active=1",
                (scope, client_id),
            ).rowcount == 1

    def ensure_refresh_capable_client_scopes(self) -> int:
        """Repair clients whose refresh grant survived but offline_access did not.

        Older authorization preflight code could overwrite a dynamic client's
        registered scope with the exact scopes requested by ChatGPT. That made
        durable refresh impossible after the next one-hour access-token expiry.
        """
        updated = 0
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT client_id,scope,grant_types_json FROM dynamic_clients WHERE active=1"
            ).fetchall()
            for row in rows:
                try:
                    grants = json.loads(row["grant_types_json"])
                except json.JSONDecodeError:
                    continue
                scopes = [value for value in str(row["scope"]).split() if value]
                if "refresh_token" not in grants or "offline_access" in scopes:
                    continue
                scopes.append("offline_access")
                if conn.execute(
                    "UPDATE dynamic_clients SET scope=? WHERE client_id=? AND active=1",
                    (" ".join(scopes), row["client_id"]),
                ).rowcount:
                    updated += 1
        return updated

    def active_dynamic_client_count(self) -> int:
        with self.connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS value FROM dynamic_clients WHERE active=1").fetchone()
        return int(row["value"])

    @staticmethod
    def dynamic_fingerprint(redirect_uris: list[str], scope: str, grant_types: list[str], response_types: list[str], token_endpoint_auth_method: str) -> str:
        value = {"redirect_uris": sorted(redirect_uris), "scope": " ".join(sorted(scope.split())), "grant_types": sorted(grant_types), "response_types": sorted(response_types), "token_endpoint_auth_method": token_endpoint_auth_method}
        return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:16]

    def mark_dynamic_client_used(self, client_id: str) -> None:
        with self.connect() as conn:
            conn.execute("UPDATE dynamic_clients SET last_used_at=? WHERE client_id=? AND active=1", (int(time.time()), client_id))

    def list_dynamic_clients(self) -> list[dict[str, Any]]:
        now = int(time.time())
        with self.connect() as conn:
            rows = conn.execute("SELECT d.*, (SELECT COUNT(*) FROM access_tokens a WHERE a.client_id=d.client_id AND a.revoked_at IS NULL AND a.expires_at>=?) AS active_access_tokens, (SELECT COUNT(*) FROM refresh_tokens r WHERE r.client_id=d.client_id AND r.revoked_at IS NULL AND r.expires_at>=?) AS active_refresh_tokens, (SELECT COUNT(*) FROM authorization_requests q WHERE q.client_id=d.client_id AND q.expires_at>=? AND q.consumed_at IS NULL) AS pending_authorization_requests, (SELECT COUNT(*) FROM authorization_codes z WHERE z.client_id=d.client_id AND z.expires_at>=? AND z.consumed_at IS NULL) AS pending_authorization_codes FROM dynamic_clients d ORDER BY d.issued_at", (now, now, now, now)).fetchall()
        result = []
        for row in rows:
            value = dict(row)
            value["redirect_uris"] = json.loads(value.pop("redirect_uris_json"))
            value["grant_types"] = json.loads(value.pop("grant_types_json"))
            value["response_types"] = json.loads(value.pop("response_types_json"))
            value.pop("secret_hash", None)
            value["client_hash"] = secret_hash(value["client_id"])[:16]
            value["fingerprint"] = self.dynamic_fingerprint(value["redirect_uris"], value["scope"], value["grant_types"], value["response_types"], value["token_endpoint_auth_method"])
            result.append(value)
        return result

    def retire_dynamic_clients(self, client_ids: list[str], *, reason: str) -> int:
        now = int(time.time())
        with self.connect() as conn:
            retired = 0
            for client_id in client_ids:
                row = conn.execute("SELECT redirect_uris_json,scope,grant_types_json,response_types_json,token_endpoint_auth_method FROM dynamic_clients WHERE client_id=? AND active=1", (client_id,)).fetchone()
                if not row:
                    continue
                fingerprint = self.dynamic_fingerprint(json.loads(row["redirect_uris_json"]), row["scope"], json.loads(row["grant_types_json"]), json.loads(row["response_types_json"]), row["token_endpoint_auth_method"])
                if conn.execute("UPDATE dynamic_clients SET active=0,retired_at=? WHERE client_id=? AND active=1", (now, client_id)).rowcount:
                    conn.execute("INSERT INTO dynamic_client_audit (action,client_hash,fingerprint,created_at,reason) VALUES (?,?,?,?,?)", ("retired", secret_hash(client_id)[:16], fingerprint, now, reason))
                    retired += 1
            return retired
