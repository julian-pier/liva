from __future__ import annotations

import subprocess
from datetime import datetime, timedelta, timezone
from typing import Any

from .redaction import redact


UNIT_COMPONENTS = {
    "liva": "liva.service",
    "liva-mcp-remote": "liva-mcp-remote.service",
    "control-center-collector": "liva-control-center-collector.service",
    "self-healing": "liva-self-healing.service",
    "full-recovery": "liva-full-recovery.service",
    "full-recovery-catchup": "liva-full-recovery-catchup.service",
    "endurance-intervals": "liva-endurance-intervals-sync.service",
    "daily-sqlite-backup": "liva-daily-backup.service",
    "legacy-db-tar": "liva-db-backup.service",
    "garmin-sync": "liva-garmin-sync.service",
    "polar-sync": "liva-polar-smart-sync.service",
    "webuntis": "liva-webuntis-update-watch.service",
    "training-calendar": "liva-training-calendar-sync.service",
    "telegram-inbox": "liva-telegram-inbox.service",
    "system-telegram-watch": "liva-system-telegram-watch.service",
    "watchdog": "liva-watchdog.service",
}


def _bounded_time(value: str | None, *, default: datetime, lower: datetime, upper: datetime) -> str:
    if not value:
        return default.isoformat()
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        parsed = parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
    except ValueError:
        return default.isoformat()
    return min(max(parsed, lower), upper).isoformat()


def read_logs(*, component: str | None = None, limit: int = 100, from_at: str | None = None, to_at: str | None = None, search: str | None = None) -> dict[str, Any]:
    selected = [component] if component in UNIT_COMPONENTS else list(UNIT_COMPONENTS)
    capped = max(1, min(int(limit), 200))
    current = datetime.now(timezone.utc)
    earliest = current - timedelta(days=7)
    since = _bounded_time(from_at, default=earliest, lower=earliest, upper=current)
    until = _bounded_time(to_at, default=current, lower=earliest, upper=current)
    lines: list[dict[str, Any]] = []
    try:
        command = ["journalctl"]
        for name in selected:
            command.extend(("--unit", UNIT_COMPONENTS[name]))
        command.extend(("--since", since, "--until", until, "--no-pager", "--output=short-iso", "--lines", str(capped)))
        result = subprocess.run(command, capture_output=True, text=True, timeout=5, check=False)
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                text = str(redact(line))
                if search and search.lower() not in text.lower():
                    continue
                severity = "critical" if any(word in text.lower() for word in (" failed", " error", "fatal")) else "info"
                lines.append({"component": component or "all", "severity": severity, "message": text})
    except Exception as exc:
        return {"ok": False, "logs": [], "error": type(exc).__name__}
    return {"ok": True, "logs": lines[-capped:], "limit": capped}
