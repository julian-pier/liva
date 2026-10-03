import sqlite3

from flask import Flask

import security.private_access as pa


def _build_app() -> Flask:
    app = Flask(__name__)
    app.secret_key = "test-secret"
    app.register_blueprint(pa.private_access_bp)
    return app


def test_delete_access_key_removes_user_preferences_before_parent_delete(tmp_path, monkeypatch):
    db_path = tmp_path / "auth.sqlite3"

    def _get_auth_db():
        conn = sqlite3.connect(db_path, timeout=1.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    monkeypatch.setattr(pa, "get_auth_db", _get_auth_db)
    monkeypatch.setattr(pa, "_AUTH_SCHEMA_READY", False)
    monkeypatch.setattr(pa, "get_current_role", lambda: "master")

    pa.ensure_auth_schema()

    conn = _get_auth_db()
    try:
        conn.execute(
            """
            INSERT INTO access_keys (label, key_hash, created_at, expires_at)
            VALUES (?, ?, ?, ?)
            """,
            ("Test", "hash-1", "2026-03-03T10:00:00Z", "2026-03-10T10:00:00Z"),
        )
        key_id = conn.execute("SELECT id FROM access_keys LIMIT 1").fetchone()["id"]
        conn.execute(
            """
            INSERT INTO user_preferences (key_id, pref_key, pref_value, updated_at)
            VALUES (?, ?, ?, ?)
            """,
            (key_id, "theme", "dark", "2026-03-03T10:05:00Z"),
        )
        conn.commit()
    finally:
        conn.close()

    app = _build_app()
    client = app.test_client()
    res = client.delete(f"/api/settings/access_keys/{key_id}")

    assert res.status_code == 200
    assert res.get_json()["ok"] is True

    conn = _get_auth_db()
    try:
        remaining_keys = conn.execute("SELECT COUNT(1) AS cnt FROM access_keys").fetchone()["cnt"]
        remaining_prefs = conn.execute("SELECT COUNT(1) AS cnt FROM user_preferences").fetchone()["cnt"]
    finally:
        conn.close()

    assert remaining_keys == 0
    assert remaining_prefs == 0
