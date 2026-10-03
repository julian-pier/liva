#!/usr/bin/env python3
from __future__ import annotations

import logging
import sys
from collections import Counter
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from schoolsync.client import AuthStateExpiredError, WebUntisClient  # noqa: E402
from schoolsync.config import load_config, setup_logging  # noqa: E402
from schoolsync.derive import derive_day_summary  # noqa: E402
from schoolsync.parser import normalize_blocks  # noqa: E402
from schoolsync.storage import persist_snapshot, write_latest_json  # noqa: E402


def main() -> int:
    setup_logging(logging.INFO)
    config = load_config()
    client = WebUntisClient(config)
    fetched_at = datetime.now().isoformat(timespec="seconds")

    try:
        date_iso, raw_blocks = client.fetch_visible_raw_blocks()
    except AuthStateExpiredError as exc:
        print(f"[SchoolSync] Login state invalid: {exc}")
        print("[SchoolSync] Please run: python scripts/webuntis_auth_bootstrap.py")
        return 2
    except Exception as exc:  # noqa: BLE001
        print(f"[SchoolSync] Fetch failed: {exc}")
        return 1

    entries_visible = normalize_blocks(raw_blocks, default_date=date_iso)
    if not entries_visible and raw_blocks:
        # Fallback if strict lesson filter removed everything for a tenant-specific DOM.
        entries_visible = normalize_blocks(raw_blocks, default_date=date_iso, apply_lesson_filter=False)
    if raw_blocks and not entries_visible:
        raise RuntimeError(
            "WebUntis returned only timetable placeholders; refusing to overwrite the last valid snapshot."
        )
    entries_today = [entry for entry in entries_visible if entry.date == date_iso]
    summary_today = derive_day_summary(entries_today)
    week_dates = _week_dates_for_focus(date_iso, entries_visible=entries_visible)
    summaries_by_date = _derive_summaries_by_date(entries_visible, week_dates=week_dates)
    snapshot_id = persist_snapshot(
        source="webuntis_playwright",
        date_iso=date_iso,
        entries=entries_visible,
        summary=summary_today,
        fetched_at=fetched_at,
    )

    payload = {
        "fetched_at": fetched_at,
        "source": "webuntis_playwright",
        "focus_date": date_iso,
        "date": date_iso,
        "entries_visible": [entry.to_dict() for entry in entries_visible],
        "entries_today": [entry.to_dict() for entry in entries_today],
        "entries": [entry.to_dict() for entry in entries_today],
        "week_dates": week_dates,
        "derived_summaries_by_date": summaries_by_date,
        "derived_summary_today": summary_today.to_dict(),
        "derived_summary": summary_today.to_dict(),
        "sqlite_snapshot_id": snapshot_id,
    }
    write_latest_json(config.latest_today_file, payload)

    start = summary_today.school_start or "--:--"
    end = summary_today.school_end or "--:--"
    print(f"Heute Schule {start} bis {end}")
    print(f"{summary_today.total_blocks} Bloecke heute")
    print(f"Groesste Freistunde heute: {summary_today.largest_free_window_min} min")
    print(f"Nachmittagsunterricht heute: {'ja' if summary_today.has_afternoon_school else 'nein'}")
    print(f"Fragmentiert heute: {'ja' if summary_today.is_fragmented_day else 'nein'}")
    print(f"Sichtbarer Stundenplan: {len(entries_visible)} Bloecke auf {len(summaries_by_date)} Tagen")
    print(f"[SchoolSync] JSON: {config.latest_today_file}")
    print(f"[SchoolSync] SQLite snapshot_id: {snapshot_id}")
    return 0


def _derive_summaries_by_date(entries_visible: list, *, week_dates: list[str] | None = None) -> dict[str, dict]:
    grouped: dict[str, list] = defaultdict(list)
    for entry in entries_visible:
        grouped[entry.date].append(entry)
    days = set(grouped.keys())
    if week_dates:
        days.update(week_dates)
    out: dict[str, dict] = {}
    for day in sorted(days):
        out[day] = derive_day_summary(grouped.get(day, [])).to_dict()
    return out


def _week_dates_for_focus(focus_date_iso: str, *, entries_visible: list | None = None) -> list[str]:
    focus = None
    candidates = []
    for entry in entries_visible or []:
        try:
            d = datetime.fromisoformat(entry.date).date()
        except Exception:  # noqa: BLE001
            continue
        iso = d.isocalendar()
        candidates.append((iso.year, iso.week, d))
    if candidates:
        week_counter = Counter((y, w) for y, w, _ in candidates)
        target_year_week, _ = week_counter.most_common(1)[0]
        target_dates = [d for y, w, d in candidates if (y, w) == target_year_week]
        if target_dates:
            focus = min(target_dates)
    if focus is None:
        try:
            focus = datetime.fromisoformat(focus_date_iso).date()
        except ValueError:
            focus = datetime.now().date()
    monday = focus - timedelta(days=focus.weekday())
    return [(monday + timedelta(days=i)).isoformat() for i in range(5)]


if __name__ == "__main__":
    raise SystemExit(main())
