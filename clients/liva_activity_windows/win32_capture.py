from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes
from datetime import datetime, timezone

import psutil

from .models import ForegroundSnapshot, VisibleWindowSnapshot


class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]


class RECT(ctypes.Structure):
    _fields_ = [("left", wintypes.LONG), ("top", wintypes.LONG), ("right", wintypes.LONG), ("bottom", wintypes.LONG)]


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", RECT), ("rcWork", RECT), ("dwFlags", wintypes.DWORD)]


class Win32Capture:
    """Small mockable adapter around the only Win32 APIs used by the agent."""

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise RuntimeError("Win32Capture kann nur unter Windows gestartet werden")
        self.user32 = ctypes.windll.user32
        self.kernel32 = ctypes.windll.kernel32
        self.dwmapi = getattr(ctypes.windll, "dwmapi", None)

    def _process_info(self, pid: int) -> tuple[str | None, str | None]:
        try:
            name = psutil.Process(pid).name()
            return name.lower(), name
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            return None, None

    def _window_title(self, hwnd: int) -> str | None:
        length = max(0, int(self.user32.GetWindowTextLengthW(hwnd)))
        if length <= 0:
            return None
        buffer = ctypes.create_unicode_buffer(length + 1)
        self.user32.GetWindowTextW(hwnd, buffer, len(buffer))
        return buffer.value.strip() or None

    def _is_cloaked(self, hwnd: int) -> bool:
        if not self.dwmapi:
            return False
        cloaked = wintypes.DWORD()
        result = self.dwmapi.DwmGetWindowAttribute(hwnd, 14, ctypes.byref(cloaked), ctypes.sizeof(cloaked))
        return result == 0 and bool(cloaked.value)

    def visible_windows(self, foreground_hwnd: int | None = None) -> tuple[VisibleWindowSnapshot, ...]:
        candidates: dict[str, tuple[float, VisibleWindowSnapshot]] = {}
        enum_proc_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def visit(hwnd, _lparam):
            if not self.user32.IsWindowVisible(hwnd) or self.user32.IsIconic(hwnd) or self._is_cloaked(hwnd):
                return True
            title = self._window_title(hwnd)
            if not title:
                return True
            rect = RECT()
            if not self.user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                return True
            width = max(0, rect.right - rect.left)
            height = max(0, rect.bottom - rect.top)
            if width < 320 or height < 240:
                return True
            monitor = self.user32.MonitorFromWindow(hwnd, 2)
            if not monitor:
                return True
            info = MONITORINFO(cbSize=ctypes.sizeof(MONITORINFO))
            if not self.user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
                return True
            monitor_area = max(1, (info.rcMonitor.right - info.rcMonitor.left) * (info.rcMonitor.bottom - info.rcMonitor.top))
            left = max(rect.left, info.rcMonitor.left)
            top = max(rect.top, info.rcMonitor.top)
            right = min(rect.right, info.rcMonitor.right)
            bottom = min(rect.bottom, info.rcMonitor.bottom)
            visible_area = max(0, right - left) * max(0, bottom - top)
            coverage = visible_area / monitor_area
            if coverage < 0.20:
                return True
            pid = wintypes.DWORD()
            self.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            exe, name = self._process_info(int(pid.value))
            if not exe:
                return True
            if exe == "explorer.exe" and title.casefold() in {"program manager", "programmumschaltung"}:
                return True
            monitor_id = f"{info.rcMonitor.left},{info.rcMonitor.top}:{info.rcMonitor.right},{info.rcMonitor.bottom}"
            snapshot = VisibleWindowSnapshot(int(pid.value), exe, name, title, monitor_id, coverage)
            score = coverage + (1.0 if foreground_hwnd and int(hwnd) == int(foreground_hwnd) else 0.0)
            if monitor_id not in candidates or score > candidates[monitor_id][0]:
                candidates[monitor_id] = (score, snapshot)
            return True

        callback = enum_proc_type(visit)
        self.user32.EnumWindows(callback, 0)
        return tuple(value[1] for value in sorted(candidates.values(), key=lambda item: item[1].monitor_id))

    def idle_seconds(self) -> float:
        info = LASTINPUTINFO(cbSize=ctypes.sizeof(LASTINPUTINFO))
        if not self.user32.GetLastInputInfo(ctypes.byref(info)):
            return 0.0
        # LASTINPUTINFO.dwTime is a 32-bit tick value. Modular subtraction
        # keeps it correct across the ~49.7 day GetTickCount rollover.
        self.kernel32.GetTickCount.restype = wintypes.DWORD
        tick = int(self.kernel32.GetTickCount())
        return ((tick - int(info.dwTime)) & 0xFFFFFFFF) / 1000.0

    def session_locked(self) -> bool:
        # The input desktop cannot be opened while the secure lock screen owns it.
        desktop = self.user32.OpenInputDesktop(0, False, 0x0100)
        if not desktop:
            return True
        self.user32.CloseDesktop(desktop)
        return False

    def capture(self) -> ForegroundSnapshot:
        now = datetime.now(timezone.utc)
        idle = self.idle_seconds()
        locked = self.session_locked()
        hwnd = self.user32.GetForegroundWindow()
        if not hwnd:
            return ForegroundSnapshot(now, None, None, None, None, idle, locked, self.visible_windows())
        title = self._window_title(hwnd)
        pid = wintypes.DWORD()
        self.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        exe, name = self._process_info(int(pid.value))
        return ForegroundSnapshot(
            now, int(pid.value) or None, exe, name, title, idle, locked,
            self.visible_windows(hwnd),
        )
