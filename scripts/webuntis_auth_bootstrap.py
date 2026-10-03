#!/usr/bin/env python3
from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from schoolsync.auth import bootstrap_auth_state  # noqa: E402
from schoolsync.config import load_config, setup_logging  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Manual WebUntis login and Playwright auth-state bootstrap.")
    parser.add_argument(
        "--max-wait-seconds",
        type=int,
        default=600,
        help="How long to wait for visible timetable after manual login (default: 600).",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run bootstrap in headless mode (requires WEBUNTIS_USERNAME/WEBUNTIS_PASSWORD in .env).",
    )
    parser.add_argument(
        "--manual",
        action="store_true",
        help="Open a browser for manual login and do not submit configured credentials automatically.",
    )
    args = parser.parse_args()

    setup_logging(logging.INFO)
    config = load_config()
    if args.manual:
        if args.headless:
            parser.error("--manual cannot be combined with --headless.")
        config = replace(
            config,
            webuntis_username=None,
            webuntis_password=None,
        )

    try:
        bootstrap_auth_state(
            config,
            max_wait_seconds=args.max_wait_seconds,
            force_headless=True if args.headless else False if args.manual else None,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[SchoolSync] Auth bootstrap failed: {exc}")
        return 1

    print(f"[SchoolSync] Auth state saved: {config.state_file}")
    print("[SchoolSync] You can now run: python scripts/webuntis_fetch_today.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
