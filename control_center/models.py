from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal


CheckStatus = Literal["healthy", "degraded", "failing", "unknown", "inactive"]
VALID_STATUSES: frozenset[str] = frozenset({"healthy", "degraded", "failing", "unknown", "inactive"})


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class OperationalCheck:
    """A single, evidence-backed technical statement.

    ``details`` is intentionally a small JSON-compatible diagnostic payload. Callers
    must redact it before exposing it outside the process.
    """

    check_id: str
    component_id: str
    name: str
    status: CheckStatus
    checked_at: str
    reason: str
    last_success_at: str | None = None
    details: dict[str, Any] = field(default_factory=dict)
    error_class: str | None = None
    freshness_seconds: int | None = None

    def __post_init__(self) -> None:
        if self.status not in VALID_STATUSES:
            raise ValueError(f"unsupported operational status: {self.status}")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ManagedJobSnapshot:
    """Read-only view of one already existing scheduler/job implementation."""

    job_id: str
    component_id: str
    name: str
    schedule: str | None
    status: CheckStatus
    source: str
    checked_at: str
    last_run_at: str | None = None
    last_success_at: str | None = None
    pending_count: int = 0
    failed_count: int = 0
    retry_count: int = 0
    job_type: str = "systemd_oneshot"
    enabled: bool | None = None
    active: bool = False
    next_run_at: str | None = None
    last_failure_at: str | None = None
    runtime_seconds: float | None = None
    incident_id: str | None = None
    last_manual_action_at: str | None = None
    last_automatic_action_at: str | None = None
    capabilities: dict[str, bool] = field(default_factory=dict)
    details: dict[str, Any] = field(default_factory=dict)
    actions: tuple[()] = ()

    def __post_init__(self) -> None:
        if self.status not in VALID_STATUSES:
            raise ValueError(f"unsupported operational status: {self.status}")

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["actions"] = []
        return value


def aggregate_status(checks: list[OperationalCheck]) -> CheckStatus:
    """Conservative aggregation; absent evidence never becomes a green cockpit."""
    statuses = {check.status for check in checks}
    if "failing" in statuses:
        return "failing"
    if "degraded" in statuses:
        return "degraded"
    if "unknown" in statuses:
        return "unknown"
    if statuses and statuses == {"inactive"}:
        return "inactive"
    return "healthy"
