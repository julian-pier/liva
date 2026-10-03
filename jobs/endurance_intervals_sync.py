from __future__ import annotations

import json
import logging

from integrations.endurance_intervals_sync import reconcile_sync_queue, run_sync_jobs


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    queued = reconcile_sync_queue()
    result = run_sync_jobs()
    print(json.dumps({"queued": queued, "sync": result}, ensure_ascii=False))
    return 0 if result["errors"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
