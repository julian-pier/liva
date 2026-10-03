from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from smart_home.pc_monitor_automation import (
    AutomationStateStore,
    BERLIN_TZ,
    MonitorDeviceState,
    PCMonitorAutomationService,
    PCMonitorConfig,
    ProbeSnapshot,
)


class DummyProbe:
    def __init__(self, snapshots):
        self._snapshots = list(snapshots)

    def probe(self, *, now, current_state):
        if not self._snapshots:
            raise AssertionError("no probe snapshot queued")
        return self._snapshots.pop(0)


class DummyMonitorController:
    def __init__(self, states_per_read=None):
        self.ensure_calls = []
        self.read_calls = 0
        self._states_per_read = list(states_per_read or [])
        self._default_state = [
            MonitorDeviceState("monitor_links", "Monitor links", "192.0.2.71", True, True),
            MonitorDeviceState("monitor_rechts", "Monitor rechts", "192.0.2.70", True, True),
        ]

    def read_states(self):
        self.read_calls += 1
        if self._states_per_read:
            return self._states_per_read.pop(0)
        return list(self._default_state)

    def ensure_power(self, power_on: bool, *, reason: str):
        self.ensure_calls.append((power_on, reason))
        return [
            MonitorDeviceState("monitor_links", "Monitor links", "192.0.2.71", power_on, True),
            MonitorDeviceState("monitor_rechts", "Monitor rechts", "192.0.2.70", power_on, True),
        ]

    @staticmethod
    def all_match_target(states, power_on: bool):
        return bool(states) and all(state.is_on is power_on for state in states)

    @staticmethod
    def any_on(states):
        return any(state.is_on is True for state in states)


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
        listener_enabled=False,
        listener_scheme="http",
        listener_port=8765,
        listener_ping_path="/ping",
        listener_token="",
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


def _positive_snapshot() -> ProbeSnapshot:
    return ProbeSnapshot(
        result="positive",
        checks=[],
        positive_reasons=["tcp:3389"],
        negative_reasons=[],
        summary="RDP/TCP erreichbar -> positives PC-Signal",
    )


def _negative_snapshot() -> ProbeSnapshot:
    return ProbeSnapshot(
        result="negative",
        checks=[],
        positive_reasons=[],
        negative_reasons=["ping"],
        summary="Keine robuste Positiv-Kombination -> negativer Poll",
    )


def _service(tmp_path: Path, probe, monitor_controller) -> PCMonitorAutomationService:
    return PCMonitorAutomationService(
        config=_config(),
        state_store=AutomationStateStore(tmp_path / "pc_state.json"),
        signal_store=None,
        monitor_controller=monitor_controller,
        probe=probe,
    )


def test_daytime_pc_off_does_not_auto_power_off(tmp_path: Path):
    monitor = DummyMonitorController()
    service = _service(tmp_path, DummyProbe([_negative_snapshot(), _negative_snapshot(), _negative_snapshot()]), monitor)

    base = datetime(2026, 4, 11, 21, 30, tzinfo=BERLIN_TZ)
    service.step(now=base)
    service.step(now=base + timedelta(seconds=3))
    state = service.step(now=base + timedelta(seconds=6))

    assert state["state"] == "PC_CONFIRMED_OFF"
    assert state["pending_shutdown_deadline"] is None
    assert monitor.ensure_calls == []


def test_pc_online_turns_monitors_on(tmp_path: Path):
    monitor = DummyMonitorController()
    service = _service(tmp_path, DummyProbe([_positive_snapshot()]), monitor)

    state = service.step(now=datetime(2026, 4, 11, 18, 0, tzinfo=BERLIN_TZ))

    assert state["state"] == "PC_ON"
    assert monitor.ensure_calls == [(True, "pc_online")]


def test_night_offline_starts_timer_and_turns_monitors_off_after_delay(tmp_path: Path):
    monitor = DummyMonitorController()
    service = _service(
        tmp_path,
        DummyProbe([_negative_snapshot(), _negative_snapshot(), _negative_snapshot(), _negative_snapshot()]),
        monitor,
    )
    service.state.state = "PC_ON"
    service.state.is_online = True

    base = datetime(2026, 4, 11, 22, 30, tzinfo=BERLIN_TZ)
    service.step(now=base)
    service.step(now=base + timedelta(seconds=3))
    mid_state = service.step(now=base + timedelta(seconds=6))
    final_state = service.step(now=base + timedelta(seconds=67))

    assert mid_state["state"] == "OFF_TIMER_RUNNING"
    assert mid_state["pending_shutdown_deadline"] is not None
    assert final_state["state"] == "PC_CONFIRMED_OFF"
    assert final_state["shutdown_executed_for_cycle"] is True
    assert monitor.ensure_calls[-1] == (False, "confirmed_off_after_timer")


def test_pc_return_during_timer_cancels_shutdown(tmp_path: Path):
    monitor = DummyMonitorController()
    service = _service(
        tmp_path,
        DummyProbe([_negative_snapshot(), _negative_snapshot(), _negative_snapshot(), _positive_snapshot()]),
        monitor,
    )
    service.state.state = "PC_ON"
    service.state.is_online = True

    base = datetime(2026, 4, 11, 22, 30, tzinfo=BERLIN_TZ)
    service.step(now=base)
    service.step(now=base + timedelta(seconds=3))
    timer_state = service.step(now=base + timedelta(seconds=6))
    final_state = service.step(now=base + timedelta(seconds=20))

    assert timer_state["state"] == "OFF_TIMER_RUNNING"
    assert final_state["state"] == "PC_ON"
    assert final_state["pending_shutdown_deadline"] is None
    assert (False, "confirmed_off_after_timer") not in monitor.ensure_calls


def test_brief_negative_probe_only_moves_to_maybe_off(tmp_path: Path):
    monitor = DummyMonitorController()
    service = _service(tmp_path, DummyProbe([_negative_snapshot()]), monitor)
    service.state.state = "PC_ON"
    service.state.is_online = True

    state = service.step(now=datetime(2026, 4, 11, 22, 30, tzinfo=BERLIN_TZ))

    assert state["state"] == "PC_MAYBE_OFF"
    assert state["is_online"] is True
    assert monitor.ensure_calls == []


def test_confirmed_off_daytime_never_polls_monitor_plugs(tmp_path: Path):
    monitor = DummyMonitorController()
    service = _service(tmp_path, DummyProbe([_negative_snapshot(), _negative_snapshot()]), monitor)
    service.state.state = "PC_CONFIRMED_OFF"
    service.state.is_online = False
    service.state.consecutive_negative_polls = service.config.offline_confirm_polls - 1

    base = datetime(2026, 4, 11, 21, 30, tzinfo=BERLIN_TZ)
    service.step(now=base)
    service.step(now=base + timedelta(seconds=3))

    assert monitor.read_calls == 0


def test_positive_state_does_not_poll_or_recommand_monitors(tmp_path: Path):
    monitor = DummyMonitorController()
    service = _service(tmp_path, DummyProbe([_positive_snapshot(), _positive_snapshot()]), monitor)

    base = datetime(2026, 4, 11, 18, 0, tzinfo=BERLIN_TZ)
    service.step(now=base)
    service.step(now=base + timedelta(seconds=3))

    assert monitor.ensure_calls == [(True, "pc_online")]
