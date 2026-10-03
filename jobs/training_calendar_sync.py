from __future__ import annotations

import json
import os
from datetime import datetime
from zoneinfo import ZoneInfo

from core.core_training_schedule import plan_training_horizon


def main() -> int:
    berlin = ZoneInfo("Europe/Berlin")
    today_iso = datetime.now(berlin).date().isoformat()
    days = max(1, min(21, int((os.getenv("LIVA_TRAINING_CALENDAR_SYNC_DAYS") or "10").strip() or "10")))
    payload = plan_training_horizon(
        today_iso,
        days=days,
        write_calendar=True,
        include_anchor=True,
        force_recompute=True,
        now=datetime.now(berlin),
    )
    print(json.dumps({"ok": True, "anchor_day": today_iso, "days": days, "items": len(payload.get("items") or [])}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
