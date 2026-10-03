from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any


DEFAULT_NOTIFY_BIN = Path("/var/lib/liva/bin/liva-notify")


def send_ntfy_notification(
    text: str,
    *,
    title: str = "LIVA",
    priority: int = 3,
    tags: str = "satellite",
    timeout_s: int = 10,
) -> dict[str, Any]:
    """Send one notification through the host's configured ntfy wrapper."""
    message = str(text or "").strip()
    if not message:
        return {"ok": False, "sent": False, "channel": "ntfy", "error": "empty_message"}

    notify_bin = Path(str(os.environ.get("LIVA_NOTIFY_BIN") or DEFAULT_NOTIFY_BIN))
    if not notify_bin.is_file() or not os.access(notify_bin, os.X_OK):
        return {"ok": False, "sent": False, "channel": "ntfy", "error": "notify_binary_unavailable"}

    env = os.environ.copy()
    env["LIVA_NOTIFY_TITLE"] = str(title or "LIVA").strip() or "LIVA"
    env["LIVA_NOTIFY_PRIORITY"] = str(max(1, min(5, int(priority))))
    env["LIVA_NOTIFY_TAGS"] = str(tags or "satellite").strip() or "satellite"
    try:
        proc = subprocess.run(
            [str(notify_bin), message],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            timeout=max(3, int(timeout_s)),
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "sent": False, "channel": "ntfy", "error": "notify_timeout"}
    except OSError:
        return {"ok": False, "sent": False, "channel": "ntfy", "error": "notify_start_failed"}
    if proc.returncode != 0:
        return {
            "ok": False,
            "sent": False,
            "channel": "ntfy",
            "error": "notify_failed",
            "exit_code": int(proc.returncode),
        }
    return {"ok": True, "sent": True, "channel": "ntfy"}
