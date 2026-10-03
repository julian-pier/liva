#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from database.connections import get_polar_db  # noqa: E402
from integrations.polar_client import ensure_polar_schema  # noqa: E402

BERLIN_TZ = ZoneInfo("Europe/Berlin")
SMART_SYNC_URL = "http://127.0.0.1:5000/api/polar/sync"
BACKFILL_DAYS = 7

def send_notify(target_date: str) -> None:
    """One canonical, per-day completion notice; never direct Ntfy."""
    from control_center import notifications
    provider = notifications.status()["active_provider"]
    notifications.deliver(
        provider=provider,
        dedupe_key=f"polar-sync:complete:{target_date}",
        text="HRV & RHR sind jetzt vollständig da",
    )



@dataclass(frozen=True)
class SyncWindowDecision:
    active: bool
    window_name: str
    min_interval_seconds: int | None
    reason: str


def berlin_now() -> datetime:
    return datetime.now(BERLIN_TZ)


def today_in_berlin() -> date:
    return berlin_now().date()


def determine_sync_window(now: datetime) -> SyncWindowDecision:
    local_now = now.astimezone(BERLIN_TZ)
    current_time = local_now.timetz().replace(tzinfo=None)
    if time(4, 30) <= current_time <= time(23, 30):
        return SyncWindowDecision(True, "daily", 120, "daily sync window active")
    return SyncWindowDecision(False, "outside_daily_window", None, "daily sync window not active")


def _has_value(value: Any) -> bool:
    return value is not None and value != ""


def has_complete_sleep(conn, target_date: str) -> bool:
    row = conn.execute(
        """
        SELECT date, sleep_score, sleep_start, sleep_end, actual_sleep_minutes
        FROM polar_sleep
        WHERE date = ?
        """,
        (target_date,),
    ).fetchone()
    if not row or row["date"] != target_date or not _has_value(row["sleep_score"]):
        return False
    return True


def has_complete_nightly_recharge(conn, target_date: str) -> bool:
    row = conn.execute(
        """
        SELECT date, mean_recovery_rmssd, mean_recovery_rri, ans_status, recovery_indicator
        FROM polar_nightly_recharge
        WHERE date = ?
        """,
        (target_date,),
    ).fetchone()
    if not row or row["date"] != target_date:
        return False
    # Polar does not populate every nightly field for every account/night. Any
    # actual recovery metric is enough to prove that the nightly record arrived.
    return any(
        _has_value(row[name])
        for name in ("mean_recovery_rmssd", "mean_recovery_rri", "ans_status", "recovery_indicator")
    )


def is_target_date_fully_synced(conn, target_date: str) -> bool:
    return has_complete_sleep(conn, target_date) and has_complete_nightly_recharge(conn, target_date)


def missing_recent_sync_dates(conn, target_date: str, *, days: int = BACKFILL_DAYS) -> list[str]:
    """Return incomplete dates in the rolling recovery-import window."""
    target = date.fromisoformat(target_date)
    return [
        (target - timedelta(days=offset)).isoformat()
        for offset in range(max(1, days))
        if not is_target_date_fully_synced(conn, (target - timedelta(days=offset)).isoformat())
    ]


def get_last_attempt(conn, target_date: str) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT target_date, last_attempt_at, last_status, last_message
        FROM polar_smart_sync_state
        WHERE target_date = ?
        """,
        (target_date,),
    ).fetchone()
    return dict(row) if row else None


def record_attempt(conn, target_date: str, *, attempted_at: str, status: str, message: str) -> None:
    conn.execute(
        """
        INSERT INTO polar_smart_sync_state (target_date, last_attempt_at, last_status, last_message)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(target_date) DO UPDATE SET
            last_attempt_at = excluded.last_attempt_at,
            last_status = excluded.last_status,
            last_message = excluded.last_message
        """,
        (target_date, attempted_at, status, message[:1000]),
    )
    conn.commit()


def is_rate_limited(last_attempt_at: str | None, now: datetime, min_interval_seconds: int | None) -> bool:
    if not last_attempt_at or not min_interval_seconds:
        return False
    try:
        last_dt = datetime.fromisoformat(last_attempt_at)
    except ValueError:
        return False
    if last_dt.tzinfo is None:
        last_dt = last_dt.replace(tzinfo=BERLIN_TZ)
    elapsed = (now.astimezone(BERLIN_TZ) - last_dt.astimezone(BERLIN_TZ)).total_seconds()
    return elapsed <= min_interval_seconds


def invoke_sync(url: str = SMART_SYNC_URL, *, days: int = 3, timeout_seconds: float = 30.0) -> dict[str, Any]:
    body = json.dumps({"days": days}).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
        raw = response.read().decode("utf-8")
        payload = json.loads(raw) if raw else {}
        if not isinstance(payload, dict):
            raise RuntimeError("Polar sync response was not a JSON object.")
        return payload


def run(now: datetime | None = None, *, sync_url: str = SMART_SYNC_URL) -> int:
    ensure_polar_schema()
    current = (now or berlin_now()).astimezone(BERLIN_TZ)
    target_date = current.date().isoformat()

    with closing(get_polar_db()) as conn:
        missing_dates = missing_recent_sync_dates(conn, target_date)
        if not missing_dates:
            print(f"POLAR_SMART_SYNC: target_date={target_date} recent_window_synced")
            return 0

        window = determine_sync_window(current)
        if not window.active:
            print(f"POLAR_SMART_SYNC: target_date={target_date} skip reason={window.reason}")
            return 0

        last_attempt = get_last_attempt(conn, target_date)
        if is_rate_limited(last_attempt.get('last_attempt_at') if last_attempt else None, current, window.min_interval_seconds):
            print(
                "POLAR_SMART_SYNC: "
                f"target_date={target_date} skip reason=rate_limited window={window.window_name}"
            )
            return 0

        attempted_at = current.isoformat(timespec="seconds")
        try:
            payload = invoke_sync(sync_url, days=BACKFILL_DAYS)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace").strip()
            message = f"http_error status={exc.code} detail={detail[:300]}"
            record_attempt(conn, target_date, attempted_at=attempted_at, status="http_error", message=message)
            print(f"POLAR_SMART_SYNC: target_date={target_date} sync_failed {message}")
            return 1
        except urllib.error.URLError as exc:
            message = f"connection_error detail={exc.reason}"
            record_attempt(conn, target_date, attempted_at=attempted_at, status="connection_error", message=message)
            print(f"POLAR_SMART_SYNC: target_date={target_date} sync_failed {message}")
            return 1
        except Exception as exc:  # noqa: BLE001
            message = f"unexpected_error detail={exc}"
            record_attempt(conn, target_date, attempted_at=attempted_at, status="unexpected_error", message=message)
            print(f"POLAR_SMART_SYNC: target_date={target_date} sync_failed {message}")
            return 1

        message = "sync_requested"
        status = "requested"
        if payload.get("ok") is False:
            errors = payload.get("errors")
            first_error = errors[0] if isinstance(errors, list) and errors else None
            message = str(payload.get("message") or first_error or "Polar sync endpoint returned ok=false")
            lowered = message.lower()
            if "nicht mehr autorisiert" in lowered or "neu verbinden" in lowered or "reconnect" in lowered:
                status = "needs_reconnect"
            else:
                status = "sync_error"
        record_attempt(conn, target_date, attempted_at=attempted_at, status=status, message=message)

        if is_target_date_fully_synced(conn, target_date):
            print(f"POLAR_SMART_SYNC: target_date={target_date} synced_after_attempt")
            send_notify(target_date)
            return 0

        print(
            "POLAR_SMART_SYNC: "
            f"target_date={target_date} attempted window={window.window_name} status={status}"
        )
        return 0 if status == "requested" else 1


def main() -> int:
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
