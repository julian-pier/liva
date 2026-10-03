from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone
from time import perf_counter
from typing import Any, Callable

from database.connections import get_training_db


_LOCK = threading.Lock()
_IN_FLIGHT: set[str] = set()


def _record_snapshot_timing(name: str, duration_s: float, **extra: Any) -> None:
    try:
        from app_support.request_timing import record_timing

        record_timing(name, duration_s, **extra)
    except Exception:
        return


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _utc_iso(dt: datetime | None = None) -> str:
    value = dt or _utc_now()
    return value.isoformat().replace("+00:00", "Z")


def ensure_read_model_schema() -> None:
    conn = get_training_db()
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS ui_read_models (
                snapshot_key TEXT PRIMARY KEY,
                payload_json TEXT NOT NULL,
                generated_at TEXT NOT NULL,
                fresh_until TEXT NOT NULL,
                build_latency_ms INTEGER NOT NULL DEFAULT 0,
                partial INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                meta_json TEXT NOT NULL DEFAULT '{}'
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_ui_read_models_fresh_until ON ui_read_models(fresh_until)"
        )
        conn.commit()
    finally:
        conn.close()


def _load_row(snapshot_key: str) -> dict[str, Any] | None:
    ensure_read_model_schema()
    conn = get_training_db()
    conn.row_factory = None
    try:
        row = conn.execute(
            """
            SELECT payload_json, generated_at, fresh_until, build_latency_ms, partial, last_error, meta_json
            FROM ui_read_models
            WHERE snapshot_key=?
            LIMIT 1
            """,
            (snapshot_key,),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    try:
        payload = json.loads(row[0] or "{}")
    except Exception:
        payload = {}
    try:
        meta = json.loads(row[6] or "{}")
    except Exception:
        meta = {}
    return {
        "payload": payload if isinstance(payload, dict) else {},
        "generated_at": str(row[1] or ""),
        "fresh_until": str(row[2] or ""),
        "build_latency_ms": int(row[3] or 0),
        "partial": bool(row[4]),
        "last_error": str(row[5] or "") or None,
        "meta": meta if isinstance(meta, dict) else {},
    }


def _store_row(
    snapshot_key: str,
    payload: dict[str, Any],
    *,
    ttl_seconds: int,
    build_latency_ms: int,
    partial: bool = False,
    last_error: str | None = None,
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    ensure_read_model_schema()
    generated_at = _utc_now()
    fresh_until = generated_at + timedelta(seconds=max(1, int(ttl_seconds or 1)))
    row = {
        "payload": payload if isinstance(payload, dict) else {},
        "generated_at": _utc_iso(generated_at),
        "fresh_until": _utc_iso(fresh_until),
        "build_latency_ms": int(build_latency_ms or 0),
        "partial": bool(partial),
        "last_error": str(last_error or "") or None,
        "meta": dict(meta or {}),
    }
    conn = get_training_db()
    try:
        conn.execute(
            """
            INSERT INTO ui_read_models (
                snapshot_key, payload_json, generated_at, fresh_until, build_latency_ms, partial, last_error, meta_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(snapshot_key) DO UPDATE SET
                payload_json=excluded.payload_json,
                generated_at=excluded.generated_at,
                fresh_until=excluded.fresh_until,
                build_latency_ms=excluded.build_latency_ms,
                partial=excluded.partial,
                last_error=excluded.last_error,
                meta_json=excluded.meta_json
            """,
            (
                snapshot_key,
                json.dumps(row["payload"], ensure_ascii=False),
                row["generated_at"],
                row["fresh_until"],
                row["build_latency_ms"],
                1 if row["partial"] else 0,
                row["last_error"],
                json.dumps(row["meta"], ensure_ascii=False),
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return row


def _is_fresh(row: dict[str, Any] | None) -> bool:
    if not isinstance(row, dict):
        return False
    try:
        fresh_until = datetime.fromisoformat(str(row.get("fresh_until") or "").replace("Z", "+00:00"))
    except Exception:
        return False
    return fresh_until > _utc_now()


def build_snapshot(
    snapshot_key: str,
    *,
    builder: Callable[[], dict[str, Any]],
    ttl_seconds: int,
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    started = perf_counter()
    payload = builder() or {}
    elapsed_s = perf_counter() - started
    latency_ms = int(elapsed_s * 1000)
    _record_snapshot_timing("snapshot.build", elapsed_s, snapshot_key=snapshot_key, partial=bool(payload.get("partial")))
    partial = bool(payload.get("partial"))
    return _store_row(
        snapshot_key,
        payload,
        ttl_seconds=ttl_seconds,
        build_latency_ms=latency_ms,
        partial=partial,
        last_error=str(payload.get("error") or "") or None,
        meta=meta,
    )


def _start_refresh_thread(
    snapshot_key: str,
    *,
    builder: Callable[[], dict[str, Any]],
    ttl_seconds: int,
    meta: dict[str, Any] | None = None,
) -> bool:
    # Background builders retain process-global database accessors. Letting
    # them outlive a Flask test request can make them write into the next
    # test's temporary database. Tests exercise synchronous builders elsewhere;
    # asynchronous refresh itself is intentionally disabled in TESTING mode.
    import os

    if os.environ.get("PYTEST_CURRENT_TEST"):
        return False
    try:
        from flask import current_app, has_app_context

        if has_app_context() and bool(current_app.config.get("TESTING")):
            return False
    except Exception:
        pass
    with _LOCK:
        if snapshot_key in _IN_FLIGHT:
            return False
        _IN_FLIGHT.add(snapshot_key)

    def _runner() -> None:
        try:
            build_snapshot(snapshot_key, builder=builder, ttl_seconds=ttl_seconds, meta=meta)
        finally:
            with _LOCK:
                _IN_FLIGHT.discard(snapshot_key)

    threading.Thread(target=_runner, name=f"read-model:{snapshot_key}", daemon=True).start()
    return True


def refresh_snapshot_async(
    snapshot_key: str,
    *,
    builder: Callable[[], dict[str, Any]],
    ttl_seconds: int,
    meta: dict[str, Any] | None = None,
) -> bool:
    return _start_refresh_thread(snapshot_key, builder=builder, ttl_seconds=ttl_seconds, meta=meta)


def get_snapshot(
    snapshot_key: str,
    *,
    builder: Callable[[], dict[str, Any]],
    ttl_seconds: int,
    allow_stale: bool = True,
    background_refresh: bool = True,
    meta: dict[str, Any] | None = None,
    fallback_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    lookup_started = perf_counter()
    row = _load_row(snapshot_key)
    if row and _is_fresh(row):
        _record_snapshot_timing("snapshot.lookup", perf_counter() - lookup_started, snapshot_key=snapshot_key, hit=True, stale=False)
        payload = dict(row["payload"])
        payload["generated_at"] = row["generated_at"]
        payload["fresh_until"] = row["fresh_until"]
        payload["stale"] = False
        payload["partial"] = bool(row["partial"])
        payload["snapshot_hit"] = True
        payload["build_latency_ms"] = int(row["build_latency_ms"] or 0)
        payload["source_latency_ms"] = int(row["build_latency_ms"] or 0)
        return payload

    if row and allow_stale:
        if background_refresh:
            _start_refresh_thread(snapshot_key, builder=builder, ttl_seconds=ttl_seconds, meta=meta)
        _record_snapshot_timing("snapshot.lookup", perf_counter() - lookup_started, snapshot_key=snapshot_key, hit=True, stale=True)
        payload = dict(row["payload"])
        payload["generated_at"] = row["generated_at"]
        payload["fresh_until"] = row["fresh_until"]
        payload["stale"] = True
        payload["partial"] = bool(row["partial"])
        payload["snapshot_hit"] = True
        payload["refresh_triggered"] = bool(background_refresh)
        payload["build_latency_ms"] = int(row["build_latency_ms"] or 0)
        payload["source_latency_ms"] = int(row["build_latency_ms"] or 0)
        return payload

    if background_refresh and isinstance(fallback_payload, dict):
        _start_refresh_thread(snapshot_key, builder=builder, ttl_seconds=ttl_seconds, meta=meta)
        payload = dict(fallback_payload)
        payload["generated_at"] = ""
        payload["fresh_until"] = ""
        payload["stale"] = True
        payload["partial"] = True
        payload["snapshot_hit"] = False
        payload["refresh_triggered"] = True
        payload["build_latency_ms"] = 0
        payload["source_latency_ms"] = 0
        _record_snapshot_timing("snapshot.lookup", perf_counter() - lookup_started, snapshot_key=snapshot_key, hit=False, stale=True, fallback=True)
        return payload

    built = build_snapshot(snapshot_key, builder=builder, ttl_seconds=ttl_seconds, meta=meta)
    _record_snapshot_timing("snapshot.lookup", perf_counter() - lookup_started, snapshot_key=snapshot_key, hit=False, stale=False)
    payload = dict(built["payload"])
    payload["generated_at"] = built["generated_at"]
    payload["fresh_until"] = built["fresh_until"]
    payload["stale"] = False
    payload["partial"] = bool(built["partial"])
    payload["snapshot_hit"] = False
    payload["build_latency_ms"] = int(built["build_latency_ms"] or 0)
    payload["source_latency_ms"] = int(built["build_latency_ms"] or 0)
    return payload


def invalidate_snapshot(snapshot_key: str) -> None:
    ensure_read_model_schema()
    conn = get_training_db()
    try:
        conn.execute("DELETE FROM ui_read_models WHERE snapshot_key=?", (snapshot_key,))
        conn.commit()
    finally:
        conn.close()


def invalidate_snapshot_prefix(prefix: str) -> None:
    ensure_read_model_schema()
    conn = get_training_db()
    try:
        conn.execute("DELETE FROM ui_read_models WHERE snapshot_key LIKE ?", (f"{prefix}%",))
        conn.commit()
    finally:
        conn.close()
