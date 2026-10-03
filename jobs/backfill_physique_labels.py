from __future__ import annotations

import json

from training.physique_gallery import backfill_physique_labels


def main() -> int:
    result = backfill_physique_labels(limit=200)
    print(json.dumps({"ok": True, **result}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
