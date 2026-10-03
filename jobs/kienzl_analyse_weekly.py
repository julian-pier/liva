from __future__ import annotations

from analysis.kienzl_weekly import refresh_kienzl_weekly_analyse


def main() -> int:
    row = refresh_kienzl_weekly_analyse(days=90, tz_name="Europe/Berlin")
    print(f"KIENZL_ANALYSE_WEEKLY: stored week_end={row.get('week_end')} model={row.get('model') or 'none'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
