from __future__ import annotations

import json

from garmin_sync.service import sync_garmin_to_database


def main() -> int:
    print(json.dumps(sync_garmin_to_database(), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
