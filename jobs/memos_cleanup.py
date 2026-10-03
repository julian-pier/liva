from __future__ import annotations

import json
import sys

from app_support.env_loader import load_project_dotenv

from integrations.memos_client import cleanup_expired_gpt_memos


def main() -> int:
    try:
        load_project_dotenv()
        result = cleanup_expired_gpt_memos(max_age_days=14, dry_run=False)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
