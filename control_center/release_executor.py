"""One-shot entry point; deployment IDs are selected only from persisted plans."""

from __future__ import annotations

import json

from . import releases, store


def main() -> int:
    planned = next((item for item in store.list_deployments(limit=20) if item["status"] in {"planned", "rolling_back"}), None)
    if not planned:
        print(json.dumps({"ok": True, "result": "no_planned_deployment"}))
        return 0
    result = releases.execute(planned["deployment_id"], rollback=planned["status"] == "rolling_back")
    print(json.dumps({"ok": result["status"] in {"succeeded", "rolled_back"}, "deployment_id": result["deployment_id"], "status": result["status"]}))
    return 0 if result["status"] in {"succeeded", "rolled_back", "preflight_failed"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
