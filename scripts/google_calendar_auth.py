#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from gcalsync.config import load_config  # noqa: E402
from gcalsync.service import GoogleCalendarAuthError, bootstrap_token  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Bootstrap Google Calendar OAuth token.")
    parser.add_argument(
        "--no-local-server",
        action="store_true",
        help="Use console auth flow instead of opening local browser callback.",
    )
    parser.add_argument(
        "--local-port",
        type=int,
        default=8080,
        help="Local callback port for OAuth local-server flow (default: 8080).",
    )
    args = parser.parse_args()

    cfg = load_config()
    try:
        token_path = bootstrap_token(
            credentials_file=cfg.credentials_file,
            token_file=cfg.token_file,
            local_server=not args.no_local_server,
            local_port=args.local_port,
        )
    except GoogleCalendarAuthError as exc:
        print(f"[GCal] Auth bootstrap failed: {exc}")
        return 2

    print(f"[GCal] Token saved: {token_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
