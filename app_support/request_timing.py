from __future__ import annotations

import json
import os
import threading
from contextlib import contextmanager
from time import perf_counter
from typing import Any

from flask import current_app, g, has_request_context, request


_TIMING_PREFIXES = (
    "/",
    "/training",
    "/planung",
    "/makros",
    "/mfp",
    "/hrv",
    "/cardio",
    "/core",
    "/api/dashboard/",
    "/api/dashboard_snapshot",
    "/api/today_snapshot",
    "/api/next_session",
    "/api/training_snapshot",
    "/api/remote_snapshot",
    "/api/core/",
)


def _timing_active() -> bool:
    if not has_request_context():
        return False
    path = str(getattr(request, "path", "") or "")
    return path in _TIMING_PREFIXES or any(path.startswith(prefix) for prefix in _TIMING_PREFIXES if prefix.endswith("/"))


def init_request_timing() -> None:
    if not _timing_active():
        return
    g.request_timing_started_at = perf_counter()
    g.request_timing_parts = {}


def record_timing(name: str, duration_s: float, **extra: Any) -> None:
    if not _timing_active():
        return
    try:
        bucket = g.request_timing_parts.setdefault(
            str(name),
            {"count": 0, "total_ms": 0.0, "max_ms": 0.0},
        )
        ms = max(0.0, float(duration_s) * 1000.0)
        bucket["count"] = int(bucket.get("count") or 0) + 1
        bucket["total_ms"] = round(float(bucket.get("total_ms") or 0.0) + ms, 1)
        bucket["max_ms"] = round(max(float(bucket.get("max_ms") or 0.0), ms), 1)
        if extra:
            bucket["last"] = extra
    except Exception:
        return


@contextmanager
def timed_block(name: str, **extra: Any):
    started = perf_counter()
    try:
        yield
    finally:
        record_timing(name, perf_counter() - started, **extra)


def flush_request_timing(response_status: int | None = None) -> None:
    if not _timing_active():
        return
    started = getattr(g, "request_timing_started_at", None)
    parts = getattr(g, "request_timing_parts", None)
    if started is None or not isinstance(parts, dict):
        return
    total_ms = round(max(0.0, (perf_counter() - float(started)) * 1000.0), 1)
    payload = {
        "request_id": str(getattr(g, "request_id", "") or ""),
        "path": str(getattr(request, "path", "") or ""),
        "query": str(getattr(request, "query_string", b"") or b"").decode("utf-8", "ignore"),
        "method": str(getattr(request, "method", "") or ""),
        "status": int(response_status or 0),
        "total_ms": total_ms,
        "pid": int(os.getpid()),
        "thread": threading.current_thread().name,
        "parts": parts,
    }
    try:
        print(f"REQTIMING {json.dumps(payload, ensure_ascii=False, sort_keys=True)}", flush=True)
    except Exception:
        pass
    try:
        current_app.logger.warning("REQTIMING %s", json.dumps(payload, ensure_ascii=False, sort_keys=True))
    except Exception:
        pass
