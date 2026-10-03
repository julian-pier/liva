from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from smart_home.pc_monitor_automation import (
    AutomationStateStore,
    MonitorController,
    PCMonitorConfig,
    PC_STATE_PATH,
    PCStatusSignalStore,
)

BERLIN_TZ = ZoneInfo("Europe/Berlin")


class PCStateManager:
    """
    Compatibility shim for older imports.
    The actual automation now lives in smart_home.pc_monitor_automation.
    """

    def __init__(
        self,
        state_path: str | Path | None = None,
        shutdown_delay_seconds: int | None = None,
        shutdown_hour: int | None = None,
        shutdown_end_hour: int | None = None,
    ) -> None:
        config = PCMonitorConfig.from_env()
        if shutdown_delay_seconds is not None:
            config = replace(config, power_off_delay_seconds=int(shutdown_delay_seconds))
        if shutdown_hour is not None:
            config = replace(config, night_start_hour=int(shutdown_hour))
        if shutdown_end_hour is not None:
            config = replace(config, night_end_hour=int(shutdown_end_hour))
        self._config = config
        self._state_store = AutomationStateStore(Path(state_path) if state_path else PC_STATE_PATH)
        self._signal_store = PCStatusSignalStore()
        self._monitor_controller = MonitorController(self._config)

    def start(self) -> None:
        return None

    def stop(self, timeout: float = 2.0) -> None:
        return None

    def get_state(self) -> dict[str, Any]:
        return self._state_store.read_snapshot()

    def handle_online(self, switch_monitors: bool = True) -> dict[str, Any]:
        self._signal_store.record_event("online", source="pc_state_compat")
        return self.get_state()

    def handle_offline(self, switch_monitors: bool = True) -> dict[str, Any]:
        self._signal_store.record_event("offline", source="pc_state_compat")
        return self.get_state()

    def set_monitors_power(self, power_on: bool) -> dict[str, bool | None]:
        states = self._monitor_controller.ensure_power(power_on, reason="compat_set_monitors_power")
        return {state.name: state.is_on for state in states}
