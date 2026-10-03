from __future__ import annotations

import json
import time
from dataclasses import replace
from typing import Callable

from .bridge import backfill, last_completed_usage_day, run_daily
from .config import BridgeConfig

RETRY_INTERVAL_SECONDS = 5 * 60
MAX_SCHEDULED_ATTEMPTS = 37


def run_catchup_attempt(config: BridgeConfig, target: str) -> dict:
    # Import every completed iOS day that appeared while Windows was offline.
    # The fixed historical reference is an installer check only; it must not
    # break future runs after StayFree eventually prunes that old cache day.
    report = backfill(replace(config, verification_day=None, verification_total_seconds=None))
    if report.get("status") == "partial":
        raise RuntimeError("scheduled backfill is incomplete")
    return run_daily(config, target)


def run_scheduled(
    config: BridgeConfig,
    *,
    run_once: Callable | None = None,
    sleeper: Callable[[float], None] = time.sleep,
    attempts: int = MAX_SCHEDULED_ATTEMPTS,
) -> int:
    """Import one completed day, retrying hidden after boot/sync/network delays."""
    target = last_completed_usage_day()
    attempt_runner = run_once or run_catchup_attempt
    for attempt in range(1, attempts + 1):
        try:
            result = attempt_runner(config, target)
            print(json.dumps({"ok": True, "attempt": attempt, **result}, ensure_ascii=False), flush=True)
            if result.get("status") in {"imported", "already_imported"}:
                return 0
        except Exception as exc:
            print(json.dumps({
                "ok": False,
                "attempt": attempt,
                "usage_day": target,
                "error": type(exc).__name__,
                "detail": str(exc),
            }, ensure_ascii=False), flush=True)
        if attempt < attempts:
            sleeper(RETRY_INTERVAL_SECONDS)
    return 1
