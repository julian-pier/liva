from __future__ import annotations

import asyncio
import atexit
import fcntl
import json
import logging
import os
import socket
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime, time as dt_time, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from kasa import Discover

from smart_home.config import TAPO_DEVICES, TAPO_PASSWORD, TAPO_USERNAME

LOG = logging.getLogger(__name__)
LOG_PREFIX = "[pc-monitor]"
BERLIN_TZ = ZoneInfo("Europe/Berlin")
ROOT_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT_DIR / "smart_home" / "data"
PC_STATE_PATH = DATA_DIR / "pc_state.json"
PC_SIGNAL_PATH = DATA_DIR / "pc_presence_signal.json"
REMOTE_DEVICE_STATE_PATH = DATA_DIR / "remote_devices_state.json"
LOCK_PATH = DATA_DIR / "pc_monitor_automation.lock"


def _env_text(name: str, default: str) -> str:
    raw = os.getenv(name)
    text = str(raw if raw is not None else default).strip()
    return text or str(default).strip()


def _env_int(name: str, default: int, *, minimum: int | None = None, maximum: int | None = None) -> int:
    raw = os.getenv(name)
    try:
        value = int(str(raw).strip()) if raw is not None else int(default)
    except Exception:
        value = int(default)
    if minimum is not None:
        value = max(int(minimum), value)
    if maximum is not None:
        value = min(int(maximum), value)
    return value


def _env_float(name: str, default: float, *, minimum: float | None = None) -> float:
    raw = os.getenv(name)
    try:
        value = float(str(raw).strip()) if raw is not None else float(default)
    except Exception:
        value = float(default)
    if minimum is not None:
        value = max(float(minimum), value)
    return value


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return bool(default)
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _parse_ports(value: str) -> tuple[int, ...]:
    ports: list[int] = []
    for part in str(value or "").split(","):
        item = part.strip()
        if not item:
            continue
        try:
            port = int(item)
        except Exception:
            continue
        if 1 <= port <= 65535 and port not in ports:
            ports.append(port)
    return tuple(ports)


def _now_local() -> datetime:
    return datetime.now(BERLIN_TZ)


def _iso_local(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(BERLIN_TZ).isoformat(timespec="seconds")


def _iso_utc(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_iso(value: Any) -> datetime | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=BERLIN_TZ)
        return parsed.astimezone(BERLIN_TZ)
    except Exception:
        return None


def _json_load(path: Path) -> dict[str, Any] | None:
    try:
        if not path.exists():
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else None
    except Exception:
        return None


def _json_save(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    try:
        tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp_path.replace(path)
    finally:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except Exception:
            pass


@dataclass(frozen=True)
class MonitorPlug:
    name: str
    ip: str
    alias: str


@dataclass(frozen=True)
class PCMonitorConfig:
    pc_ip: str
    rdp_port: int
    extra_tcp_ports: tuple[int, ...]
    poll_interval_seconds: float
    offline_confirm_polls: int
    night_start_hour: int
    night_end_hour: int
    power_off_delay_seconds: int
    ping_enabled: bool
    ping_timeout_seconds: int
    tcp_timeout_seconds: float
    signal_max_age_seconds: int
    status_signal_grace_seconds: int
    listener_enabled: bool
    listener_scheme: str
    listener_port: int
    listener_ping_path: str
    listener_token: str
    monitor_state_timeout_seconds: float
    monitor_command_timeout_seconds: float
    monitor_connect_retries: int
    monitor_retry_delay_seconds: float
    monitor_command_retry_seconds: int
    monitor_failure_retry_seconds: int
    offline_monitor_refresh_seconds: int
    log_repeat_interval: int
    monitor_plugs: tuple[MonitorPlug, ...]

    @property
    def tcp_ports(self) -> tuple[int, ...]:
        ports: list[int] = [self.rdp_port]
        for port in self.extra_tcp_ports:
            if port not in ports:
                ports.append(port)
        return tuple(ports)

    @classmethod
    def from_env(cls) -> "PCMonitorConfig":
        default_pc_ip = _env_text("LIVA_PC_MONITOR_HOST", _env_text("LIVA_REMOTE_PC_HOST", "192.0.2.2"))
        rdp_port = _env_int("LIVA_PC_MONITOR_RDP_PORT", 3389, minimum=1, maximum=65535)
        extra_ports = _parse_ports(_env_text("LIVA_PC_MONITOR_EXTRA_TCP_PORTS", "8765"))
        monitor_plugs = _load_monitor_plugs_from_env()
        return cls(
            pc_ip=default_pc_ip,
            rdp_port=rdp_port,
            extra_tcp_ports=extra_ports,
            poll_interval_seconds=_env_float("LIVA_PC_MONITOR_POLL_INTERVAL_SECONDS", 3.0, minimum=0.5),
            offline_confirm_polls=_env_int("LIVA_PC_MONITOR_OFFLINE_CONFIRM_POLLS", 3, minimum=2),
            night_start_hour=_env_int("LIVA_PC_MONITOR_NIGHT_START_HOUR", 22, minimum=0, maximum=23),
            night_end_hour=_env_int("LIVA_PC_MONITOR_NIGHT_END_HOUR", 6, minimum=0, maximum=23),
            power_off_delay_seconds=_env_int("LIVA_PC_MONITOR_POWER_OFF_DELAY_SECONDS", 60, minimum=1),
            ping_enabled=_env_bool("LIVA_PC_MONITOR_PING_ENABLED", True),
            ping_timeout_seconds=_env_int("LIVA_PC_MONITOR_PING_TIMEOUT_SECONDS", 1, minimum=1, maximum=10),
            tcp_timeout_seconds=_env_float("LIVA_PC_MONITOR_TCP_TIMEOUT_SECONDS", 0.9, minimum=0.1),
            signal_max_age_seconds=_env_int("LIVA_PC_MONITOR_SIGNAL_MAX_AGE_SECONDS", 240, minimum=5),
            status_signal_grace_seconds=_env_int("LIVA_PC_MONITOR_STATUS_SIGNAL_GRACE_SECONDS", 120, minimum=5),
            listener_enabled=_env_bool("LIVA_PC_MONITOR_LISTENER_ENABLED", bool(_env_text("LIVA_REMOTE_PC_HTTP_TOKEN", ""))),
            listener_scheme=_env_text("LIVA_REMOTE_PC_CONTROL_SCHEME", "http"),
            listener_port=_env_int("LIVA_REMOTE_PC_CONTROL_PORT", 8765, minimum=1, maximum=65535),
            listener_ping_path=_env_text("LIVA_PC_MONITOR_LISTENER_PING_PATH", "/ping"),
            listener_token=_env_text("LIVA_REMOTE_PC_HTTP_TOKEN", ""),
            monitor_state_timeout_seconds=_env_float("LIVA_PC_MONITOR_PLUG_STATE_TIMEOUT_SECONDS", 2.2, minimum=0.5),
            monitor_command_timeout_seconds=_env_float("LIVA_PC_MONITOR_PLUG_COMMAND_TIMEOUT_SECONDS", 4.0, minimum=0.5),
            monitor_connect_retries=_env_int("LIVA_PC_MONITOR_PLUG_CONNECT_RETRIES", 3, minimum=1, maximum=10),
            monitor_retry_delay_seconds=_env_float("LIVA_PC_MONITOR_PLUG_RETRY_DELAY_SECONDS", 0.8, minimum=0.1),
            monitor_command_retry_seconds=_env_int("LIVA_PC_MONITOR_PLUG_COMMAND_RETRY_SECONDS", 60, minimum=5),
            # A failed switch is retried carefully.  In particular, do not keep
            # reconnecting to an already unhealthy Tapo plug every minute.
            monitor_failure_retry_seconds=_env_int("LIVA_PC_MONITOR_PLUG_FAILURE_RETRY_SECONDS", 900, minimum=60),
            offline_monitor_refresh_seconds=_env_int("LIVA_PC_MONITOR_OFFLINE_MONITOR_REFRESH_SECONDS", 60, minimum=10),
            log_repeat_interval=_env_int("LIVA_PC_MONITOR_LOG_REPEAT_INTERVAL", 10, minimum=1),
            monitor_plugs=monitor_plugs,
        )


def _load_monitor_plugs_from_env() -> tuple[MonitorPlug, ...]:
    raw_ips = _env_text("LIVA_PC_MONITOR_PLUG_IPS", "")
    explicit_ips = [item.strip() for item in raw_ips.split(",") if item.strip()]
    if explicit_ips:
        plugs = []
        for index, ip in enumerate(explicit_ips, start=1):
            plugs.append(MonitorPlug(name=f"monitor_{index}", ip=ip, alias=f"Monitor {index}"))
        return tuple(plugs)

    plugs: list[MonitorPlug] = []
    for device_name in ("monitor_links", "monitor_rechts"):
        cfg = TAPO_DEVICES.get(device_name) or {}
        ip = str(cfg.get("ip") or "").strip()
        if not ip:
            continue
        plugs.append(
            MonitorPlug(
                name=device_name,
                ip=ip,
                alias=str(cfg.get("alias") or device_name),
            )
        )
    return tuple(plugs)


@dataclass
class SignalSnapshot:
    last_event: str | None = None
    last_event_at: datetime | None = None
    last_online_signal_at: datetime | None = None
    last_offline_signal_at: datetime | None = None
    source: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "last_event": self.last_event,
            "last_event_at": _iso_local(self.last_event_at),
            "last_online_signal_at": _iso_local(self.last_online_signal_at),
            "last_offline_signal_at": _iso_local(self.last_offline_signal_at),
            "source": self.source,
        }


class PCStatusSignalStore:
    def __init__(self, path: Path = PC_SIGNAL_PATH) -> None:
        self._path = path

    def record_event(self, event: str, *, source: str = "pc_status_api", now: datetime | None = None) -> SignalSnapshot:
        when = now or _now_local()
        current = self.read()
        event_name = "online" if str(event).strip().lower() == "online" else "offline"
        current.last_event = event_name
        current.last_event_at = when
        current.source = source
        if event_name == "online":
            current.last_online_signal_at = when
        else:
            current.last_offline_signal_at = when
        _json_save(self._path, current.to_dict())
        LOG.info("%s status-signal gespeichert: event=%s source=%s at=%s", LOG_PREFIX, event_name, source, _iso_local(when))
        return current

    def read(self) -> SignalSnapshot:
        payload = _json_load(self._path) or {}
        return SignalSnapshot(
            last_event=str(payload.get("last_event") or "").strip() or None,
            last_event_at=_parse_iso(payload.get("last_event_at")),
            last_online_signal_at=_parse_iso(payload.get("last_online_signal_at")),
            last_offline_signal_at=_parse_iso(payload.get("last_offline_signal_at")),
            source=str(payload.get("source") or "").strip() or None,
        )


@dataclass
class CheckResult:
    name: str
    ok: bool
    severity: str
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "ok": self.ok,
            "severity": self.severity,
            "detail": self.detail,
        }


@dataclass
class ProbeSnapshot:
    result: str
    checks: list[CheckResult]
    positive_reasons: list[str]
    negative_reasons: list[str]
    summary: str

    @property
    def is_positive(self) -> bool:
        return self.result == "positive"

    def to_dict(self) -> dict[str, Any]:
        return {
            "result": self.result,
            "checks": [item.to_dict() for item in self.checks],
            "positive_reasons": list(self.positive_reasons),
            "negative_reasons": list(self.negative_reasons),
            "summary": self.summary,
        }

    def signature(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True)


class PCPresenceProbe:
    def __init__(self, config: PCMonitorConfig, signal_store: PCStatusSignalStore) -> None:
        self._config = config
        self._signal_store = signal_store

    def probe(self, *, now: datetime, current_state: str) -> ProbeSnapshot:
        checks: list[CheckResult] = []
        strong_positive = False
        support_positive = 0
        negative_reasons: list[str] = []
        positive_reasons: list[str] = []

        for port in self._config.tcp_ports:
            ok = self._tcp_port_open(self._config.pc_ip, port, timeout_seconds=self._config.tcp_timeout_seconds)
            severity = "primary" if port == self._config.rdp_port else "secondary"
            detail = (
                f"TCP-Port {port} erreichbar -> positives PC-Signal"
                if ok
                else f"TCP-Port {port} nicht erreichbar"
            )
            checks.append(CheckResult(name=f"tcp:{port}", ok=ok, severity=severity, detail=detail))
            if ok:
                strong_positive = True
                positive_reasons.append(f"tcp:{port}")

        if self._config.listener_enabled and self._config.listener_token:
            listener_ok = self._listener_ping(timeout_seconds=self._config.tcp_timeout_seconds)
            checks.append(
                CheckResult(
                    name="listener_ping",
                    ok=listener_ok,
                    severity="supporting",
                    detail=(
                        "HTTP-Listener erreichbar -> positives Zusatzsignal"
                        if listener_ok
                        else "HTTP-Listener nicht erreichbar"
                    ),
                )
            )
            if listener_ok:
                strong_positive = True
                positive_reasons.append("listener_ping")
        else:
            checks.append(
                CheckResult(
                    name="listener_ping",
                    ok=False,
                    severity="disabled",
                    detail="HTTP-Zusatzsignal deaktiviert",
                )
            )

        ping_ok = False
        if self._config.ping_enabled:
            ping_ok = self._ping_reachable(self._config.pc_ip, timeout_seconds=self._config.ping_timeout_seconds)
            checks.append(
                CheckResult(
                    name="ping",
                    ok=ping_ok,
                    severity="secondary",
                    detail="Ping erfolgreich -> sekundäres PC-Signal" if ping_ok else "Ping fehlgeschlagen",
                )
            )
            if ping_ok:
                support_positive += 1
                positive_reasons.append("ping")
            else:
                negative_reasons.append("ping")
        else:
            checks.append(CheckResult(name="ping", ok=False, severity="disabled", detail="Ping deaktiviert"))

        signal_snapshot = self._signal_store.read()
        signal_age_limit = min(self._config.signal_max_age_seconds, self._config.status_signal_grace_seconds)
        online_signal_fresh = self._is_fresh(signal_snapshot.last_online_signal_at, now, signal_age_limit)
        offline_signal_fresh = self._is_fresh(signal_snapshot.last_offline_signal_at, now, signal_age_limit)

        checks.append(
            CheckResult(
                name="status_signal_online",
                ok=online_signal_fresh,
                severity="supporting",
                detail=(
                    "Frisches Online-Signal vorhanden -> positives Zusatzsignal"
                    if online_signal_fresh
                    else "Kein frisches Online-Signal"
                ),
            )
        )
        if online_signal_fresh:
            support_positive += 1
            positive_reasons.append("status_signal_online")

        checks.append(
            CheckResult(
                name="status_signal_offline",
                ok=offline_signal_fresh,
                severity="supporting",
                detail=(
                    "Frisches Offline-Signal vorhanden -> negatives Zusatzsignal"
                    if offline_signal_fresh
                    else "Kein frisches Offline-Signal"
                ),
            )
        )
        if offline_signal_fresh:
            negative_reasons.append("status_signal_offline")

        if strong_positive:
            result = "positive"
        elif support_positive >= 2:
            result = "positive"
        elif support_positive >= 1 and current_state in {"PC_ON", "PC_MAYBE_OFF"}:
            result = "positive"
        else:
            result = "negative"

        has_tcp_positive = any(reason.startswith("tcp:") for reason in positive_reasons)
        if result == "positive" and not has_tcp_positive:
            summary = "Nur Zusatzsignale positiv -> PC bleibt AN bzw. kommt robust zurück"
        elif result == "positive" and has_tcp_positive and not ping_ok:
            summary = "Ping fehlgeschlagen, aber TCP erreichbar -> PC bleibt AN"
        elif result == "positive" and has_tcp_positive:
            summary = "RDP/TCP erreichbar -> positives PC-Signal"
        elif result == "positive":
            summary = "Kombinierte Bewertung positiv -> PC ist AN"
        else:
            summary = "Keine robuste Positiv-Kombination -> negativer Poll"

        return ProbeSnapshot(
            result=result,
            checks=checks,
            positive_reasons=positive_reasons,
            negative_reasons=negative_reasons,
            summary=summary,
        )

    @staticmethod
    def _is_fresh(value: datetime | None, now: datetime, max_age_seconds: int) -> bool:
        if value is None:
            return False
        return (now - value).total_seconds() <= max(1, int(max_age_seconds))

    @staticmethod
    def _tcp_port_open(host: str, port: int, *, timeout_seconds: float) -> bool:
        try:
            with socket.create_connection((host, int(port)), timeout=max(0.1, float(timeout_seconds))):
                return True
        except Exception:
            return False

    @staticmethod
    def _ping_reachable(host: str, *, timeout_seconds: int) -> bool:
        try:
            proc = subprocess.run(
                ["ping", "-c", "1", "-W", str(max(1, int(timeout_seconds))), host],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=max(2, int(timeout_seconds) + 1),
            )
            return proc.returncode == 0
        except Exception:
            return False

    def _listener_ping(self, *, timeout_seconds: float) -> bool:
        path = "/" + str(self._config.listener_ping_path or "/ping").lstrip("/")
        token = str(self._config.listener_token or "").strip()
        if not token:
            return False
        quoted = urllib.parse.quote(token, safe="")
        url = f"{self._config.listener_scheme}://{self._config.pc_ip}:{self._config.listener_port}{path}?token={quoted}"
        req = urllib.request.Request(url, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=max(0.2, float(timeout_seconds))) as response:
                status = int(getattr(response, "status", 200))
                return 200 <= status < 300
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            return False


@dataclass
class MonitorDeviceState:
    name: str
    alias: str
    ip: str
    is_on: bool | None
    reachable: bool
    error: str | None = None

    def to_remote_cache(self) -> dict[str, Any]:
        return {
            "is_on": self.is_on,
            "reachable": self.reachable,
            "error": self.error,
            "updated_at": _iso_utc(_now_local()),
        }


class MonitorController:
    def __init__(self, config: PCMonitorConfig) -> None:
        self._config = config

    async def _get_device(self, plug: MonitorPlug, *, timeout_seconds: float):
        last_error: Exception | None = None
        for attempt in range(1, self._config.monitor_connect_retries + 1):
            device = None
            try:
                device = await Discover.discover_single(
                    plug.ip,
                    username=TAPO_USERNAME,
                    password=TAPO_PASSWORD,
                    timeout=timeout_seconds,
                )
                await device.update()
                if attempt > 1:
                    LOG.info(
                        "%s Monitor-Verbindung nach Retry erfolgreich: device=%s attempt=%s/%s",
                        LOG_PREFIX,
                        plug.name,
                        attempt,
                        self._config.monitor_connect_retries,
                    )
                return device
            except Exception as exc:
                last_error = exc
                if device is not None:
                    try:
                        await device.disconnect()
                    except Exception:
                        pass
                if attempt < self._config.monitor_connect_retries:
                    LOG.warning(
                        "%s Monitor-Verbindung fehlgeschlagen, retry folgt: device=%s attempt=%s/%s error=%s",
                        LOG_PREFIX,
                        plug.name,
                        attempt,
                        self._config.monitor_connect_retries,
                        str(exc).strip() or exc.__class__.__name__,
                    )
                    await asyncio.sleep(self._config.monitor_retry_delay_seconds)

        assert last_error is not None
        raise last_error

    async def _read_single_state(self, plug: MonitorPlug) -> MonitorDeviceState:
        device = None
        try:
            device = await self._get_device(plug, timeout_seconds=self._config.monitor_state_timeout_seconds)
            return MonitorDeviceState(
                name=plug.name,
                alias=plug.alias,
                ip=plug.ip,
                is_on=bool(device.is_on),
                reachable=True,
            )
        except Exception as exc:
            return MonitorDeviceState(
                name=plug.name,
                alias=plug.alias,
                ip=plug.ip,
                is_on=None,
                reachable=False,
                error=str(exc).strip() or "state_read_failed",
            )
        finally:
            if device is not None:
                try:
                    await device.disconnect()
                except Exception:
                    pass

    async def _set_single_state(self, plug: MonitorPlug, power_on: bool) -> MonitorDeviceState:
        device = None
        try:
            device = await self._get_device(plug, timeout_seconds=self._config.monitor_command_timeout_seconds)
            if power_on:
                await device.turn_on()
            else:
                await device.turn_off()
            await device.update()
            return MonitorDeviceState(
                name=plug.name,
                alias=plug.alias,
                ip=plug.ip,
                is_on=bool(device.is_on),
                reachable=True,
            )
        except Exception as exc:
            return MonitorDeviceState(
                name=plug.name,
                alias=plug.alias,
                ip=plug.ip,
                is_on=None,
                reachable=False,
                error=str(exc).strip() or "state_write_failed",
            )
        finally:
            if device is not None:
                try:
                    await device.disconnect()
                except Exception:
                    pass

    def read_states(self) -> list[MonitorDeviceState]:
        states = asyncio.run(self._read_states_async())
        self._update_remote_device_cache(states)
        return states

    async def _read_states_async(self) -> list[MonitorDeviceState]:
        states: list[MonitorDeviceState] = []
        for plug in self._config.monitor_plugs:
            states.append(await self._read_single_state(plug))
        return states

    def ensure_power(self, power_on: bool, *, reason: str) -> list[MonitorDeviceState]:
        current_states = self.read_states()
        differing = [state for state in current_states if state.is_on is not power_on]
        if not differing:
            LOG.info("%s Monitore bereits im Zielzustand -> keine Schaltung (target=%s, reason=%s)", LOG_PREFIX, power_on, reason)
            return current_states

        LOG.info(
            "%s Monitor-Schaltung startet: target=%s reason=%s devices=%s",
            LOG_PREFIX,
            power_on,
            reason,
            ", ".join(state.name for state in differing),
        )
        updated_states = asyncio.run(self._set_states_async(power_on=power_on, only_names={state.name for state in differing}))
        self._update_remote_device_cache(updated_states)
        for state in updated_states:
            LOG.info(
                "%s Monitor-Status nach Schaltung: device=%s reachable=%s is_on=%s error=%s",
                LOG_PREFIX,
                state.name,
                state.reachable,
                state.is_on,
                state.error,
            )
        return updated_states

    def retry_power(self, power_on: bool, *, names: set[str], reason: str) -> list[MonitorDeviceState]:
        """Retry only plugs that failed a previous event-driven switch.

        This intentionally avoids a periodic state discovery of every monitor
        plug while the PC is running.
        """
        if not names:
            return []
        LOG.info(
            "%s Vorsichtiger Monitor-Recovery-Versuch: target=%s reason=%s devices=%s",
            LOG_PREFIX, power_on, reason, ", ".join(sorted(names)),
        )
        states = asyncio.run(self._set_named_states_async(power_on=power_on, names=names))
        self._update_remote_device_cache(states)
        return states

    async def _set_named_states_async(self, *, power_on: bool, names: set[str]) -> list[MonitorDeviceState]:
        states: list[MonitorDeviceState] = []
        for plug in self._config.monitor_plugs:
            if plug.name in names:
                states.append(await self._set_single_state(plug, power_on))
        return states

    async def _set_states_async(self, *, power_on: bool, only_names: set[str]) -> list[MonitorDeviceState]:
        states: list[MonitorDeviceState] = []
        for plug in self._config.monitor_plugs:
            if plug.name in only_names:
                states.append(await self._set_single_state(plug, power_on))
            else:
                states.append(await self._read_single_state(plug))
        return states

    @staticmethod
    def all_match_target(states: list[MonitorDeviceState], power_on: bool) -> bool:
        return bool(states) and all(state.is_on is power_on for state in states)

    @staticmethod
    def any_on(states: list[MonitorDeviceState]) -> bool:
        return any(state.is_on is True for state in states)

    def _update_remote_device_cache(self, states: list[MonitorDeviceState]) -> None:
        existing = _json_load(REMOTE_DEVICE_STATE_PATH) or {}
        for state in states:
            existing[state.name] = state.to_remote_cache()
        _json_save(REMOTE_DEVICE_STATE_PATH, existing)


@dataclass
class AutomationState:
    schema_version: int = 2
    state: str = "PC_CONFIRMED_OFF"
    is_online: bool = False
    last_online_at: str | None = None
    last_offline_at: str | None = None
    pending_shutdown_deadline: str | None = None
    shutdown_executed_for_cycle: bool = False
    consecutive_negative_polls: int = 0
    last_transition_at: str | None = None
    last_detection_at: str | None = None
    last_evaluation: dict[str, Any] = field(default_factory=dict)
    config: dict[str, Any] = field(default_factory=dict)


class AutomationStateStore:
    def __init__(self, path: Path = PC_STATE_PATH) -> None:
        self._path = path

    def load(self) -> AutomationState:
        payload = _json_load(self._path) or {}
        return AutomationState(
            schema_version=int(payload.get("schema_version", 2)),
            state=str(payload.get("state") or "PC_CONFIRMED_OFF"),
            is_online=bool(payload.get("is_online", False)),
            last_online_at=str(payload.get("last_online_at") or "") or None,
            last_offline_at=str(payload.get("last_offline_at") or "") or None,
            pending_shutdown_deadline=str(payload.get("pending_shutdown_deadline") or "") or None,
            shutdown_executed_for_cycle=bool(payload.get("shutdown_executed_for_cycle", False)),
            consecutive_negative_polls=int(payload.get("consecutive_negative_polls", 0) or 0),
            last_transition_at=str(payload.get("last_transition_at") or "") or None,
            last_detection_at=str(payload.get("last_detection_at") or "") or None,
            last_evaluation=payload.get("last_evaluation") if isinstance(payload.get("last_evaluation"), dict) else {},
            config=payload.get("config") if isinstance(payload.get("config"), dict) else {},
        )

    def save(self, state: AutomationState) -> None:
        _json_save(self._path, asdict(state))

    def read_snapshot(self) -> dict[str, Any]:
        return asdict(self.load())


class PCMonitorAutomationService:
    def __init__(
        self,
        config: PCMonitorConfig | None = None,
        *,
        state_store: AutomationStateStore | None = None,
        signal_store: PCStatusSignalStore | None = None,
        monitor_controller: MonitorController | None = None,
        probe: PCPresenceProbe | None = None,
    ) -> None:
        self.config = config or PCMonitorConfig.from_env()
        self.state_store = state_store or AutomationStateStore()
        self.signal_store = signal_store or PCStatusSignalStore()
        self.monitor_controller = monitor_controller or MonitorController(self.config)
        self.probe = probe or PCPresenceProbe(self.config, self.signal_store)
        self.state = self.state_store.load()
        self._last_logged_signature: str | None = None
        self._repeat_log_count = 0
        self._last_monitor_refresh_at: datetime | None = None
        self._last_monitor_refresh_states: list[MonitorDeviceState] | None = None
        self._last_monitor_command_at: datetime | None = None
        self._last_monitor_command_target: bool | None = None
        self._pending_monitor_recovery: set[str] = set()
        self._next_monitor_recovery_at: datetime | None = None
        # A worker restart must still be able to turn the monitors on when the
        # PC was already running, but subsequent positive probes do nothing.
        self._initial_online_sync_pending = self.state.state == "PC_ON"

    def run_forever(self) -> None:
        LOG.info(
            "%s Service gestartet: pc_ip=%s tcp_ports=%s night=%02d:00-%02d:00 off_delay=%ss poll=%.1fs monitor_plugs=%s",
            LOG_PREFIX,
            self.config.pc_ip,
            self.config.tcp_ports,
            self.config.night_start_hour,
            self.config.night_end_hour,
            self.config.power_off_delay_seconds,
            self.config.poll_interval_seconds,
            ", ".join(plug.ip for plug in self.config.monitor_plugs),
        )
        while True:
            self.step()
            time.sleep(self.config.poll_interval_seconds)

    def step(self, *, now: datetime | None = None) -> dict[str, Any]:
        current_time = now or _now_local()
        current_state = self.state.state
        snapshot = self.probe.probe(now=current_time, current_state=current_state)
        self._log_probe(snapshot)

        if snapshot.is_positive:
            self._handle_positive(snapshot, current_time)
        else:
            self._handle_negative(snapshot, current_time)

        self._handle_timer_only_transition(current_time)
        self.state.last_detection_at = _iso_local(current_time)
        self.state.last_evaluation = snapshot.to_dict()
        self.state.config = {
            "pc_ip": self.config.pc_ip,
            "tcp_ports": list(self.config.tcp_ports),
            "night_start_hour": self.config.night_start_hour,
            "night_end_hour": self.config.night_end_hour,
            "power_off_delay_seconds": self.config.power_off_delay_seconds,
            "poll_interval_seconds": self.config.poll_interval_seconds,
            "offline_confirm_polls": self.config.offline_confirm_polls,
            "offline_monitor_refresh_seconds": self.config.offline_monitor_refresh_seconds,
            "monitor_command_retry_seconds": self.config.monitor_command_retry_seconds,
            "monitor_failure_retry_seconds": self.config.monitor_failure_retry_seconds,
            "monitor_plug_ips": [plug.ip for plug in self.config.monitor_plugs],
        }
        self.state_store.save(self.state)
        return self.state_store.read_snapshot()

    def _ensure_monitors_power(self, power_on: bool, *, reason: str, now: datetime) -> list[MonitorDeviceState] | None:
        states = self.monitor_controller.ensure_power(power_on, reason=reason)
        self._last_monitor_command_at = now
        self._last_monitor_command_target = power_on
        return states

    def _handle_positive(self, snapshot: ProbeSnapshot, now: datetime) -> None:
        entered_pc_on = self.state.state != "PC_ON"
        needs_initial_sync = self._initial_online_sync_pending
        if self.state.pending_shutdown_deadline:
            LOG.info("%s PC kam während Timer zurück -> Timer abgebrochen", LOG_PREFIX)
            self.state.pending_shutdown_deadline = None
        self.state.consecutive_negative_polls = 0
        self.state.is_online = True
        self.state.last_online_at = _iso_local(now)
        self.state.shutdown_executed_for_cycle = False
        if self.state.state != "PC_ON":
            self._transition_to("PC_ON", now, f"Robuste Positivbewertung: {snapshot.positive_reasons}")
        if entered_pc_on or needs_initial_sync:
            self._initial_online_sync_pending = False
            monitor_states = self._ensure_monitors_power(True, reason="pc_online", now=now)
        elif self._monitor_recovery_ready(now):
            monitor_states = self.monitor_controller.retry_power(
                True,
                names=self._pending_monitor_recovery,
                reason="pc_online_recovery",
            )
            self._record_monitor_recovery_result(monitor_states, now=now)
        else:
            return
        if monitor_states is None:
            return
        if entered_pc_on or needs_initial_sync:
            self._record_monitor_recovery_result(monitor_states, now=now)
        if self.monitor_controller.all_match_target(monitor_states, True):
            LOG.info("%s Monitore AN bestätigt", LOG_PREFIX)
        else:
            LOG.warning("%s Monitore sollten AN sein, sind aber nicht vollständig bestätigt", LOG_PREFIX)

    def _handle_negative(self, snapshot: ProbeSnapshot, now: datetime) -> None:
        self.state.consecutive_negative_polls += 1
        if self.state.state == "PC_ON":
            self.state.is_online = True
            self._transition_to("PC_MAYBE_OFF", now, f"Erster negativer Poll: {snapshot.negative_reasons or snapshot.summary}")

        if self.state.consecutive_negative_polls < self.config.offline_confirm_polls:
            LOG.info(
                "%s Negative Polls gesammelt: %s/%s -> PC noch nicht als AUS bestätigt",
                LOG_PREFIX,
                self.state.consecutive_negative_polls,
                self.config.offline_confirm_polls,
            )
            return

        if self.state.state not in {"PC_CONFIRMED_OFF", "OFF_TIMER_RUNNING"}:
            LOG.info(
                "%s %s aufeinanderfolgende negative Prüfungen -> PC als AUS bestätigt",
                LOG_PREFIX,
                self.state.consecutive_negative_polls,
            )
        self.state.is_online = False
        self.state.last_offline_at = _iso_local(now)

        if self._is_night_window(now) and not self.state.shutdown_executed_for_cycle:
            if self.state.pending_shutdown_deadline is None:
                deadline = now + timedelta(seconds=self.config.power_off_delay_seconds)
                self.state.pending_shutdown_deadline = _iso_local(deadline)
                self._transition_to("OFF_TIMER_RUNNING", now, f"Nach {self.config.night_start_hour}:00, AUS bestätigt -> Ausschalt-Timer gestartet")
                LOG.info("%s Nach %02d:00, AUS bestätigt -> Ausschalt-Timer gestartet (%s)", LOG_PREFIX, self.config.night_start_hour, self.state.pending_shutdown_deadline)
            else:
                self.state.state = "OFF_TIMER_RUNNING"
        else:
            if self.state.pending_shutdown_deadline is not None:
                LOG.info("%s Ausschalt-Timer verworfen -> Bedingung nicht mehr erfüllt", LOG_PREFIX)
            self.state.pending_shutdown_deadline = None
            self._transition_to("PC_CONFIRMED_OFF", now, "PC bestätigt AUS, aber kein Nacht-Auto-Off aktiv")
            if not self._is_night_window(now):
                LOG.info("%s Vor %02d:00 -> kein automatisches Ausschalten", LOG_PREFIX, self.config.night_start_hour)

    def _handle_timer_only_transition(self, now: datetime) -> None:
        if self.state.state == "PC_CONFIRMED_OFF" and self._is_night_window(now) and not self.state.shutdown_executed_for_cycle:
            deadline = now + timedelta(seconds=self.config.power_off_delay_seconds)
            self.state.pending_shutdown_deadline = _iso_local(deadline)
            self._transition_to("OFF_TIMER_RUNNING", now, "Nachtfenster erreicht bei bestätigtem AUS -> Timer gestartet")
            LOG.info("%s Nachtfenster erreicht -> Ausschalt-Timer gestartet (%s)", LOG_PREFIX, self.state.pending_shutdown_deadline)
            return

        if self.state.state != "OFF_TIMER_RUNNING":
            return

        deadline = _parse_iso(self.state.pending_shutdown_deadline)
        if deadline is None:
            self._transition_to("PC_CONFIRMED_OFF", now, "Timer fehlte -> zurück zu bestätigt AUS")
            return

        if not self._is_night_window(now):
            LOG.info("%s Timer außerhalb Nachtfenster -> abgebrochen", LOG_PREFIX)
            self.state.pending_shutdown_deadline = None
            self._transition_to("PC_CONFIRMED_OFF", now, "Nachtfenster beendet")
            return

        remaining = (deadline - now).total_seconds()
        if remaining > 0:
            LOG.info("%s Ausschalt-Timer läuft noch %.1fs", LOG_PREFIX, remaining)
            return

        LOG.info("%s Timer abgelaufen, PC weiter AUS -> Monitore AUS", LOG_PREFIX)
        final_states = self._ensure_monitors_power(False, reason="confirmed_off_after_timer", now=now) or []
        self.state.shutdown_executed_for_cycle = self.monitor_controller.all_match_target(final_states, False)
        self.state.pending_shutdown_deadline = None
        self._transition_to("PC_CONFIRMED_OFF", now, "Timer abgelaufen -> Monitore ausgeschaltet")
        if self.state.shutdown_executed_for_cycle:
            LOG.info("%s Monitore AUS bestätigt", LOG_PREFIX)
        else:
            LOG.warning("%s Monitore sollten AUS sein, sind aber nicht vollständig bestätigt", LOG_PREFIX)

    def _transition_to(self, new_state: str, now: datetime, reason: str) -> None:
        old_state = self.state.state
        self.state.state = new_state
        self.state.last_transition_at = _iso_local(now)
        if old_state != new_state:
            LOG.info("%s Zustandswechsel: %s -> %s (%s)", LOG_PREFIX, old_state, new_state, reason)

    def _is_night_window(self, now: datetime) -> bool:
        start = dt_time(self.config.night_start_hour, 0, 0)
        end = dt_time(self.config.night_end_hour, 0, 0)
        current = now.timetz().replace(tzinfo=None)
        if start == end:
            return True
        if start < end:
            return start <= current < end
        return current >= start or current < end

    def _log_probe(self, snapshot: ProbeSnapshot) -> None:
        signature = snapshot.signature()
        if signature != self._last_logged_signature:
            self._repeat_log_count = 0
            self._last_logged_signature = signature
            self._emit_probe_log(snapshot)
            return

        self._repeat_log_count += 1
        if self._repeat_log_count % self.config.log_repeat_interval == 0:
            LOG.info("%s Probe unverändert x%s: %s", LOG_PREFIX, self._repeat_log_count, snapshot.summary)

    def _emit_probe_log(self, snapshot: ProbeSnapshot) -> None:
        details = "; ".join(f"{item.name}={'ok' if item.ok else 'fail'} ({item.detail})" for item in snapshot.checks)
        LOG.info(
            "%s Probe: result=%s positive=%s negative=%s summary=%s checks=%s",
            LOG_PREFIX,
            snapshot.result,
            ",".join(snapshot.positive_reasons) or "-",
            ",".join(snapshot.negative_reasons) or "-",
            snapshot.summary,
            details,
        )

    def _get_monitor_states(self, now: datetime, *, force_refresh: bool) -> list[MonitorDeviceState]:
        cached_states = self._last_monitor_refresh_states
        cached_at = self._last_monitor_refresh_at
        if not force_refresh and cached_states is not None and cached_at is not None:
            age_seconds = (now - cached_at).total_seconds()
            if age_seconds < self.config.offline_monitor_refresh_seconds:
                return list(cached_states)

        states = self.monitor_controller.read_states()
        self._last_monitor_refresh_at = now
        self._last_monitor_refresh_states = list(states)
        return states

    def _monitor_recovery_ready(self, now: datetime) -> bool:
        return bool(self._pending_monitor_recovery) and (
            self._next_monitor_recovery_at is None or now >= self._next_monitor_recovery_at
        )

    def _record_monitor_recovery_result(self, states: list[MonitorDeviceState], *, now: datetime) -> None:
        self._pending_monitor_recovery = {
            state.name for state in states if not state.reachable or state.is_on is not True
        }
        if self._pending_monitor_recovery:
            self._next_monitor_recovery_at = now + timedelta(seconds=self.config.monitor_failure_retry_seconds)
            LOG.warning(
                "%s Monitor-Recovery geplant in %ss: devices=%s",
                LOG_PREFIX,
                self.config.monitor_failure_retry_seconds,
                ", ".join(sorted(self._pending_monitor_recovery)),
            )
        else:
            self._next_monitor_recovery_at = None


class SingleInstanceLock:
    def __init__(self, path: Path = LOCK_PATH) -> None:
        self._path = path
        self._handle = None

    def acquire(self) -> bool:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        handle = self._path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.close()
            return False

        handle.seek(0)
        handle.truncate()
        handle.write(str(os.getpid()))
        handle.flush()
        self._handle = handle
        atexit.register(self.release)
        return True

    def current_holder(self) -> str | None:
        try:
            return self._path.read_text(encoding="utf-8").strip() or None
        except Exception:
            return None

    def release(self) -> None:
        if self._handle is None:
            return
        try:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        try:
            self._handle.close()
        except Exception:
            pass
        self._handle = None


def read_automation_state(path: Path = PC_STATE_PATH) -> dict[str, Any]:
    return AutomationStateStore(path).read_snapshot()


def main() -> None:
    logging.basicConfig(
        level=os.getenv("LIVA_PC_MONITOR_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    lock = SingleInstanceLock()
    retry_seconds = _env_float("LIVA_PC_MONITOR_LOCK_RETRY_SECONDS", 5.0, minimum=1.0)
    waiting_logged = False
    while not lock.acquire():
        holder = lock.current_holder() or "unknown"
        if not waiting_logged:
            LOG.warning(
                "%s Zweite Instanz erkannt -> warte auf Lock-Freigabe (holder=%s, retry=%.1fs)",
                LOG_PREFIX,
                holder,
                retry_seconds,
            )
            waiting_logged = True
        time.sleep(retry_seconds)
    if waiting_logged:
        LOG.info("%s Lock wieder frei -> Monitor-Worker uebernimmt", LOG_PREFIX)
    service = PCMonitorAutomationService()
    service.run_forever()


if __name__ == "__main__":
    main()
