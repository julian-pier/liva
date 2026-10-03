#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from gcalsync.config import load_config  # noqa: E402
from gcalsync.service import (  # noqa: E402
    GoogleCalendarAuthError,
    build_service,
    compact_event,
    delete_event,
    insert_event,
    list_calendars,
    list_events,
    patch_event,
    update_event,
)


def _calendar_id(cfg, value: str | None) -> str:
    if not value or value == "default":
        return cfg.default_calendar_id
    if value == "school":
        return cfg.school_calendar_id
    if value == "football":
        return cfg.football_calendar_id
    if value == "training":
        return cfg.training_calendar_id
    if value == "work":
        return cfg.work_calendar_id
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description="Google Calendar CLI for read/create/update/delete.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list-calendars", help="List accessible calendars.")

    p_list = sub.add_parser("list-events", help="List events.")
    p_list.add_argument("--calendar", default="default")
    p_list.add_argument("--time-min", default=None)
    p_list.add_argument("--time-max", default=None)
    p_list.add_argument("--q", default=None)
    p_list.add_argument("--private-prop", action="append", default=[])
    p_list.add_argument("--raw", action="store_true")

    p_create = sub.add_parser("create-event", help="Create event from JSON file.")
    p_create.add_argument("--calendar", default="default")
    p_create.add_argument("--json-file", required=True)

    p_update = sub.add_parser("update-event", help="Replace event with JSON file.")
    p_update.add_argument("--calendar", default="default")
    p_update.add_argument("--event-id", required=True)
    p_update.add_argument("--json-file", required=True)

    p_patch = sub.add_parser("patch-event", help="Patch event with JSON file.")
    p_patch.add_argument("--calendar", default="default")
    p_patch.add_argument("--event-id", required=True)
    p_patch.add_argument("--json-file", required=True)

    p_delete = sub.add_parser("delete-event", help="Delete event.")
    p_delete.add_argument("--calendar", default="default")
    p_delete.add_argument("--event-id", required=True)

    args = parser.parse_args()
    cfg = load_config()
    try:
        service = build_service(cfg.credentials_file, cfg.token_file)
    except GoogleCalendarAuthError as exc:
        print(f"[GCal] {exc}")
        return 2

    if args.cmd == "list-calendars":
        items = list_calendars(service)
        for c in items:
            print(f"{c.get('id')} | {c.get('summary')} | tz={c.get('timeZone')}")
        return 0

    cal_id = _calendar_id(cfg, getattr(args, "calendar", "default"))

    if args.cmd == "list-events":
        events = list_events(
            service,
            calendar_id=cal_id,
            time_min=args.time_min,
            time_max=args.time_max,
            q=args.q,
            private_extended_property=args.private_prop or None,
        )
        if args.raw:
            print(json.dumps(events, ensure_ascii=False, indent=2))
            return 0
        for event in events:
            print(compact_event(event))
        return 0

    if args.cmd == "create-event":
        body = json.loads(Path(args.json_file).read_text(encoding="utf-8"))
        created = insert_event(service, calendar_id=cal_id, event=body)
        print(f"created: {created.get('id')}")
        return 0

    if args.cmd == "update-event":
        body = json.loads(Path(args.json_file).read_text(encoding="utf-8"))
        updated = update_event(service, calendar_id=cal_id, event_id=args.event_id, event=body)
        print(f"updated: {updated.get('id')}")
        return 0

    if args.cmd == "patch-event":
        body = json.loads(Path(args.json_file).read_text(encoding="utf-8"))
        updated = patch_event(service, calendar_id=cal_id, event_id=args.event_id, patch=body)
        print(f"patched: {updated.get('id')}")
        return 0

    if args.cmd == "delete-event":
        delete_event(service, calendar_id=cal_id, event_id=args.event_id)
        print(f"deleted: {args.event_id}")
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())

