from __future__ import annotations

import asyncio
from datetime import datetime
from smart_home.pc_monitor_automation import (
    BERLIN_TZ,
    CheckResult,
    MonitorController,
    MonitorPlug,
    PCMonitorConfig,
    PCPresenceProbe,
    PCStatusSignalStore,
    ProbeSnapshot,
    main,
)


class DummySignalStore:
    def __init__(self, online_at=None, offline_at=None):
        self._online_at = online_at
        self._offline_at = offline_at

    def read(self):
        class Snapshot:
            last_online_signal_at = self._online_at
            last_offline_signal_at = self._offline_at

        return Snapshot()


def _config() -> PCMonitorConfig:
    return PCMonitorConfig(
        pc_ip="192.0.2.2",
        rdp_port=3389,
        extra_tcp_ports=(8765,),
        poll_interval_seconds=3.0,
        offline_confirm_polls=3,
        night_start_hour=22,
        night_end_hour=6,
        power_off_delay_seconds=60,
        ping_enabled=True,
        ping_timeout_seconds=1,
        tcp_timeout_seconds=0.5,
        signal_max_age_seconds=240,
        status_signal_grace_seconds=120,
        listener_enabled=True,
        listener_scheme="http",
        listener_port=8765,
        listener_ping_path="/ping",
        listener_token="secret",
        monitor_state_timeout_seconds=2.0,
        monitor_command_timeout_seconds=4.0,
        monitor_connect_retries=3,
        monitor_retry_delay_seconds=0.01,
        monitor_command_retry_seconds=60,
        monitor_failure_retry_seconds=900,
        offline_monitor_refresh_seconds=60,
        log_repeat_interval=10,
        monitor_plugs=(),
    )


def test_tcp_positive_marks_pc_online(monkeypatch):
    probe = PCPresenceProbe(_config(), DummySignalStore())
    monkeypatch.setattr(probe, "_tcp_port_open", lambda host, port, timeout_seconds: port == 3389)
    monkeypatch.setattr(probe, "_listener_ping", lambda timeout_seconds: False)
    monkeypatch.setattr(probe, "_ping_reachable", lambda host, timeout_seconds: False)

    snapshot = probe.probe(now=datetime(2026, 4, 11, 21, 0, tzinfo=BERLIN_TZ), current_state="PC_CONFIRMED_OFF")

    assert snapshot.result == "positive"
    assert "tcp:3389" in snapshot.positive_reasons
    assert snapshot.summary == "Ping fehlgeschlagen, aber TCP erreichbar -> PC bleibt AN"


def test_ping_alone_is_not_enough_from_confirmed_off(monkeypatch):
    probe = PCPresenceProbe(_config(), DummySignalStore())
    monkeypatch.setattr(probe, "_tcp_port_open", lambda host, port, timeout_seconds: False)
    monkeypatch.setattr(probe, "_listener_ping", lambda timeout_seconds: False)
    monkeypatch.setattr(probe, "_ping_reachable", lambda host, timeout_seconds: True)

    snapshot = probe.probe(now=datetime(2026, 4, 11, 21, 0, tzinfo=BERLIN_TZ), current_state="PC_CONFIRMED_OFF")

    assert snapshot.result == "negative"
    assert snapshot.summary == "Keine robuste Positiv-Kombination -> negativer Poll"


def test_ping_plus_online_signal_becomes_positive(monkeypatch):
    signal_store = DummySignalStore(online_at=datetime(2026, 4, 11, 21, 0, tzinfo=BERLIN_TZ))
    probe = PCPresenceProbe(_config(), signal_store)
    monkeypatch.setattr(probe, "_tcp_port_open", lambda host, port, timeout_seconds: False)
    monkeypatch.setattr(probe, "_listener_ping", lambda timeout_seconds: False)
    monkeypatch.setattr(probe, "_ping_reachable", lambda host, timeout_seconds: True)

    snapshot = probe.probe(now=datetime(2026, 4, 11, 21, 1, tzinfo=BERLIN_TZ), current_state="PC_CONFIRMED_OFF")

    assert snapshot.result == "positive"
    assert snapshot.positive_reasons == ["ping", "status_signal_online"]
    assert snapshot.summary == "Nur Zusatzsignale positiv -> PC bleibt AN bzw. kommt robust zurück"


def test_single_ping_holds_online_state_when_pc_was_already_on(monkeypatch):
    probe = PCPresenceProbe(_config(), DummySignalStore())
    monkeypatch.setattr(probe, "_tcp_port_open", lambda host, port, timeout_seconds: False)
    monkeypatch.setattr(probe, "_listener_ping", lambda timeout_seconds: False)
    monkeypatch.setattr(probe, "_ping_reachable", lambda host, timeout_seconds: True)

    snapshot = probe.probe(now=datetime(2026, 4, 11, 22, 30, tzinfo=BERLIN_TZ), current_state="PC_ON")

    assert snapshot.result == "positive"
    assert snapshot.positive_reasons == ["ping"]


def test_listener_ping_counts_as_strong_positive(monkeypatch):
    probe = PCPresenceProbe(_config(), DummySignalStore())
    monkeypatch.setattr(probe, "_tcp_port_open", lambda host, port, timeout_seconds: False)
    monkeypatch.setattr(probe, "_listener_ping", lambda timeout_seconds: True)
    monkeypatch.setattr(probe, "_ping_reachable", lambda host, timeout_seconds: False)

    snapshot = probe.probe(now=datetime(2026, 4, 11, 22, 30, tzinfo=BERLIN_TZ), current_state="PC_CONFIRMED_OFF")

    assert snapshot.result == "positive"
    assert "listener_ping" in snapshot.positive_reasons


def test_main_waits_for_lock_instead_of_exiting(monkeypatch):
    acquired = {"calls": 0}
    slept = {"calls": 0}
    ran = {"value": False}

    class FakeLock:
        def acquire(self):
            acquired["calls"] += 1
            return acquired["calls"] >= 2

        def current_holder(self):
            return "1234"

    class FakeService:
        def run_forever(self):
            ran["value"] = True

    monkeypatch.setattr("smart_home.pc_monitor_automation.SingleInstanceLock", lambda: FakeLock())
    monkeypatch.setattr("smart_home.pc_monitor_automation.PCMonitorAutomationService", lambda: FakeService())
    monkeypatch.setattr("smart_home.pc_monitor_automation.time.sleep", lambda _seconds: slept.__setitem__("calls", slept["calls"] + 1))
    monkeypatch.setattr("smart_home.pc_monitor_automation.logging.basicConfig", lambda **_kwargs: None)
    monkeypatch.setattr("smart_home.pc_monitor_automation.os.getenv", lambda name, default=None: "5" if name == "LIVA_PC_MONITOR_LOCK_RETRY_SECONDS" else default)

    main()

    assert acquired["calls"] == 2
    assert slept["calls"] == 1
    assert ran["value"] is True


def test_monitor_controller_retries_after_connection_reset(monkeypatch):
    config = _config()
    controller = MonitorController(config)
    plug = MonitorPlug(name="monitor_links", ip="192.0.2.71", alias="Monitor links")
    calls = {"count": 0}

    class FakeDevice:
        is_on = True

        async def update(self):
            return None

        async def disconnect(self):
            return None

    async def fake_discover_single(ip, username, password, timeout):
        calls["count"] += 1
        if calls["count"] < 3:
            raise ConnectionResetError(104, "Connection reset by peer")
        return FakeDevice()

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr("smart_home.pc_monitor_automation.Discover.discover_single", fake_discover_single)
    monkeypatch.setattr("smart_home.pc_monitor_automation.asyncio.sleep", no_sleep)

    state = asyncio.run(controller._read_single_state(plug))

    assert calls["count"] == 3
    assert state.reachable is True
    assert state.is_on is True
    assert state.error is None
