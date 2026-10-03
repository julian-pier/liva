"""Explicit, non-secret technical configuration registry for Control Center."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from . import self_healing, store


@dataclass(frozen=True)
class ConfigDefinition:
    config_id: str
    name: str
    description: str
    default: Any
    source: str
    editable: bool
    secret: bool = False
    restart_required: bool = False
    risk: str = "medium"
    components: tuple[str, ...] = ()


REGISTRY: dict[str, ConfigDefinition] = {
    "notification-provider": ConfigDefinition("notification-provider", "Notification Provider", "Einziger globaler Provider für technische Control-Center-Meldungen.", "auto", "control_center_state", True, risk="high", components=("control-center", "telegram", "ntfy")),
    "self-healing-enabled": ConfigDefinition("self-healing-enabled", "Self-Healing", "Globaler Kill Switch für begrenzte technische Recovery-Aktionen.", False, "self_healing_state", True, risk="high", components=("self-healing",)),
    "recovery-primary-media": ConfigDefinition("recovery-primary-media", "Primary Recovery Media", "Read-only technische Recovery-Metadaten.", None, "full_recovery_state", False, components=("full-recovery",)),
    "telegram-system-secret": ConfigDefinition("telegram-system-secret", "Telegram System Credentials", "Nur Konfigurationsstatus der bestehenden System-Credentials.", None, "telegram_environment", False, secret=True, components=("telegram",)),
    "ntfy-secret": ConfigDefinition("ntfy-secret", "Ntfy Credentials", "Nur Konfigurationsstatus des bestehenden Ntfy-Transportwegs.", None, "ntfy_environment", False, secret=True, components=("ntfy",)),
}


def _provider_default() -> str:
    from .notifications import provider_status
    telegram = provider_status("telegram")
    return "telegram" if telegram["configured"] else "ntfy" if provider_status("ntfy")["configured"] else "telegram"


def _notification_provider() -> dict[str, Any]:
    state = store.get_state("technical-config:notification-provider")
    effective = str(state.get("effective") if isinstance(state, dict) else _provider_default())
    return {"effective": effective, "pending": None, "status": "active"}


def _self_healing() -> dict[str, Any]:
    state = self_healing.global_state()
    return {"effective": bool(state["enabled"]), "pending": None, "status": "active"}


def _recovery_media() -> dict[str, Any]:
    state = store.get_full_recovery_state() or {}
    return {"effective": {"uuid": state.get("primary_media_uuid"), "status": state.get("primary_media_status")}, "pending": None, "status": "read_only"}


def _secret_status(config_id: str) -> dict[str, Any]:
    from .notifications import provider_status
    provider = "telegram" if config_id.startswith("telegram") else "ntfy"
    status = provider_status(provider)
    return {"effective": "configured" if status["configured"] else "missing", "pending": None, "status": status["status"]}


def view(config_id: str) -> dict[str, Any] | None:
    definition = REGISTRY.get(config_id)
    if not definition: return None
    if config_id == "notification-provider": value = _notification_provider()
    elif config_id == "self-healing-enabled": value = _self_healing()
    elif config_id == "recovery-primary-media": value = _recovery_media()
    else: value = _secret_status(config_id)
    return {**definition.__dict__, **value}


def views() -> list[dict[str, Any]]:
    return [view(config_id) for config_id in REGISTRY]


def mutate(config_id: str, requested: Any, *, actor_role: str) -> dict[str, Any]:
    definition = REGISTRY.get(config_id)
    if not definition: raise KeyError(config_id)
    previous = view(config_id) or {}
    if not definition.editable or definition.secret: raise PermissionError("config is read-only")
    if config_id == "notification-provider":
        provider = str(requested or "").strip().lower()
        if provider not in {"telegram", "ntfy"}: raise ValueError("provider must be telegram or ntfy")
        from .notifications import provider_status
        candidate = provider_status(provider, verify=True)
        if not (candidate["configured"] and candidate["healthy"]): raise ValueError("provider is not configured and healthy")
        store.set_state("technical-config:notification-provider", {"effective": provider, "pending": None, "updated_at": store.utc_now()})
    elif config_id == "self-healing-enabled":
        if not isinstance(requested, bool): raise ValueError("value must be boolean")
        self_healing.set_global_enabled(requested)
    else:
        raise PermissionError("config is read-only")
    resulting = view(config_id) or {}
    store.record_config_audit({"config_id": config_id, "previous": {"effective": previous.get("effective")}, "requested": {"value": requested}, "actor_role": actor_role, "validation": "valid", "apply_result": "applied", "resulting": {"effective": resulting.get("effective"), "pending": resulting.get("pending")}})
    return resulting
