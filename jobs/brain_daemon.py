from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path

logger = logging.getLogger("liva.brain_daemon")
_THREAD_LOCK = threading.Lock()
_RUN_LOCK = threading.Lock()
_STARTED = False
_BRAIN_THREAD: threading.Thread | None = None


def _interval_seconds() -> int:
    raw = (os.getenv("LIVA_BRAIN_LOOP_INTERVAL_SECONDS") or "20").strip()
    try:
        value = int(raw)
    except Exception:
        value = 20
    return max(15, min(300, value))


def _error_backoff_seconds() -> int:
    raw = (os.getenv("LIVA_BRAIN_ERROR_BACKOFF_SECONDS") or "20").strip()
    try:
        value = int(raw)
    except Exception:
        value = 20
    return max(5, min(300, value))


def _autostart_mode() -> str:
    return (os.getenv("LIVA_BRAIN_AUTOSTART") or "auto").strip().lower()


def _looks_like_pi() -> bool:
    explicit_role = (os.getenv("LIVA_BRAIN_ROLE") or "").strip().lower()
    if explicit_role in {"brain", "analysis", "pi"}:
        return True
    try:
        model = Path("/proc/device-tree/model").read_text(encoding="utf-8", errors="ignore")
    except Exception:
        model = ""
    return "raspberry pi" in model.lower()


def autostart_enabled() -> bool:
    mode = _autostart_mode()
    if mode in {"0", "false", "off", "no"}:
        return False
    if mode in {"1", "true", "on", "yes"}:
        return True
    return _looks_like_pi()


def run_brain_cycle_once(*, reason: str = "daemon_continuous", requested_by: str = "brain_daemon", logger_override: logging.Logger | None = None):
    active_logger = logger_override or logger
    with _RUN_LOCK:
        try:
            from analysis.brain_engine import run_brain_analysis  # type: ignore
        except Exception as exc:
            payload = {
                "ok": False,
                "generated_at": None,
                "job": {"topics_checked": 0},
                "reason": "brain_engine_unavailable",
                "error": str(exc),
            }
            if active_logger:
                active_logger.warning("brain engine unavailable: %s", exc)
            return payload
        payload = run_brain_analysis(reason=reason, requested_by=requested_by)
    if active_logger:
        active_logger.info(
            "brain cycle ok: generated_at=%s topics=%s",
            payload.get("generated_at"),
            ((payload.get("job") or {}) if isinstance(payload.get("job"), dict) else {}).get("topics_checked"),
        )
    return payload


def start_brain_daemon(*, logger_override: logging.Logger | None = None) -> bool:
    global _STARTED, _BRAIN_THREAD
    if not autostart_enabled():
        return False
    if _STARTED:
        return False
    with _THREAD_LOCK:
        if _STARTED:
            return False
        _STARTED = True
        active_logger = logger_override or logger
        interval_seconds = _interval_seconds()
        error_backoff_seconds = _error_backoff_seconds()

        def _loop() -> None:
            if active_logger:
                active_logger.info("brain daemon started: interval=%ss", interval_seconds)
            while True:
                started = time.monotonic()
                try:
                    run_brain_cycle_once(
                        reason="daemon_continuous",
                        requested_by="brain_daemon",
                        logger_override=active_logger,
                    )
                    elapsed = max(0.0, time.monotonic() - started)
                    sleep_for = max(1.0, float(interval_seconds) - elapsed)
                    time.sleep(sleep_for)
                except Exception:
                    if active_logger:
                        active_logger.exception("brain cycle failed")
                    time.sleep(error_backoff_seconds)

        _BRAIN_THREAD = threading.Thread(target=_loop, name="brain-daemon", daemon=True)
        _BRAIN_THREAD.start()
        return True


def main() -> None:
    logging.basicConfig(
        level=(os.getenv("LIVA_BRAIN_LOG_LEVEL") or "INFO").upper(),
        format="%(asctime)s %(levelname)s [brain-daemon] %(message)s",
    )
    interval_seconds = _interval_seconds()
    error_backoff_seconds = _error_backoff_seconds()
    logger.info("brain daemon started: interval=%ss", interval_seconds)
    while True:
        started = time.monotonic()
        try:
            run_brain_cycle_once(
                reason="daemon_continuous",
                requested_by="brain_daemon",
                logger_override=logger,
            )
            elapsed = max(0.0, time.monotonic() - started)
            sleep_for = max(1.0, float(interval_seconds) - elapsed)
            time.sleep(sleep_for)
        except Exception:
            logger.exception("brain cycle failed")
            time.sleep(error_backoff_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
