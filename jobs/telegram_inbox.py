from __future__ import annotations

import json

from integrations.telegram_hub import process_telegram_inboxes


def main() -> int:
    result = process_telegram_inboxes(timeout=0)
    print(json.dumps(result, ensure_ascii=True))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
