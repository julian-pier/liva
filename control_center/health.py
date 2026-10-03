from __future__ import annotations

from typing import Any

from .adapters import backup_snapshot, endurance_snapshot, full_recovery_snapshot
from .models import OperationalCheck, aggregate_status, utc_now_iso
from .redaction import redact


def build_cockpit() -> dict[str, Any]:
    checks: list[OperationalCheck] = []
    jobs = []
    adapters = (endurance_snapshot, backup_snapshot, full_recovery_snapshot)
    for adapter in adapters:
        try:
            adapter_checks, job = adapter()
            checks.extend(adapter_checks)
            jobs.append(job)
        except Exception as exc:  # Defensive boundary: one adapter must not break the cockpit.
            checks.append(OperationalCheck("control-center.adapter.unexpected", "control-center", "Control Center adapter", "unknown", utc_now_iso(), "An adapter failed unexpectedly.", details=redact({"error": str(exc)}), error_class=type(exc).__name__))
    return {
        "ok": True,
        "generated_at": utc_now_iso(),
        "status": aggregate_status(checks),
        "checks": [check.to_dict() for check in checks],
        "jobs": [job.to_dict() for job in jobs],
        "actions": [],
    }
