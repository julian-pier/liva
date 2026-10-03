from __future__ import annotations

import json
import threading
import time

from integrations.telegram_hub import process_nutrition_inbox, process_system_inbox, process_training_inbox


POLL_TIMEOUT_SECONDS = 25
ERROR_BACKOFF_SECONDS = 2


def _worker(name: str, func) -> None:
    while True:
        try:
            result = func(timeout=POLL_TIMEOUT_SECONDS)
            updates = int(result.get("updates") or 0)
            processed = int(result.get("processed") or 0)
            errors = list(result.get("errors") or [])
            proposal = result.get("proposal") if isinstance(result.get("proposal"), dict) else None
            proposal_sent = bool(proposal and proposal.get("sent"))
            pr_alert = result.get("pr_alert") if isinstance(result.get("pr_alert"), dict) else None
            pr_alert_sent = bool(pr_alert and pr_alert.get("sent"))
            if updates > 0 or processed > 0 or errors or proposal_sent or pr_alert_sent:
                print(json.dumps({"domain": name, **result}, ensure_ascii=False), flush=True)
        except KeyboardInterrupt:
            return
        except Exception as exc:
            print(json.dumps({"ok": False, "domain": name, "error": str(exc)}, ensure_ascii=False), flush=True)
            time.sleep(ERROR_BACKOFF_SECONDS)


def main() -> int:
    threads = [
        threading.Thread(target=_worker, args=("training", process_training_inbox), daemon=True),
        threading.Thread(target=_worker, args=("nutrition", process_nutrition_inbox), daemon=True),
        threading.Thread(target=_worker, args=("system", process_system_inbox), daemon=True),
    ]
    for thread in threads:
        thread.start()
    try:
        while True:
            for thread in threads:
                if not thread.is_alive():
                    return 1
            time.sleep(1)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
