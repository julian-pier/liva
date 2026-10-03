#!/usr/bin/env python3
import argparse
import json
import os
import sys


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from nutrition.nutrition_planning_db import reconcile_logged_planned_meals


def main() -> int:
    parser = argparse.ArgumentParser(description="Reconcile planned logged meals to current active plan slots.")
    parser.add_argument("--date", dest="date_iso", help="Single log_date in YYYY-MM-DD format.")
    parser.add_argument("--days", type=int, help="Rolling day window ending today.")
    parser.add_argument("--apply", action="store_true", help="Persist resolved links and planned status rows.")
    args = parser.parse_args()

    if bool(args.date_iso) == bool(args.days):
        parser.error("Provide exactly one of --date or --days.")

    result = reconcile_logged_planned_meals(date_iso=args.date_iso, days=args.days, apply=args.apply)
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
