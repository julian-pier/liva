from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .bridge import backfill, run_daily
from .config import BridgeConfig, default_config_path


def main() -> int:
    parser = argparse.ArgumentParser(description="LIVA StayFree iOS daily bridge")
    parser.add_argument("--config", type=Path, default=default_config_path())
    parser.add_argument("--day", help="Import one completed StayFree usage day (YYYY-MM-DD)")
    parser.add_argument("--backfill", action="store_true", help="Import available completed history")
    parser.add_argument("--dry-run", action="store_true", help="List backfill coverage without uploading")
    args = parser.parse_args()
    try:
        config = BridgeConfig.load(args.config)
        result = backfill(config, dry_run=args.dry_run) if (args.backfill or args.dry_run) else run_daily(config, args.day)
    except Exception as exc:
        print(json.dumps({"ok": False, "error": type(exc).__name__, "detail": str(exc)}, ensure_ascii=False))
        return 1
    complete = result.get("status") != "partial"
    print(json.dumps({"ok": complete, **result}, ensure_ascii=False))
    return 0 if complete else 1


if __name__ == "__main__":
    raise SystemExit(main())
