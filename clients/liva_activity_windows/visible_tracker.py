from __future__ import annotations

from datetime import datetime, timedelta
from typing import Callable

from .browser_context import BrowserContextStore
from .models import ActivitySegment, ForegroundSnapshot
from .segmenter import SegmentAggregator


class VisibleMonitorTracker:
    def __init__(
        self,
        persist: Callable[[ActivitySegment], None],
        browser_contexts: BrowserContextStore,
        *,
        checkpoint_seconds: int = 5,
        idle_threshold_seconds: int = 180,
        browser_context_max_age_seconds: int = 7 * 86400,
        sleep_gap_seconds: int = 15,
    ) -> None:
        self.persist = persist
        self.browser_contexts = browser_contexts
        self.checkpoint_seconds = checkpoint_seconds
        self.idle_threshold_seconds = idle_threshold_seconds
        self.browser_context_max_age_seconds = browser_context_max_age_seconds
        self.sleep_gap_seconds = sleep_gap_seconds
        self.trackers: dict[str, SegmentAggregator] = {}

    def observe(self, snapshot: ForegroundSnapshot) -> None:
        active_monitors: set[str] = set()
        if snapshot.session_locked:
            idle_started = snapshot.timestamp
            self.close(idle_started)
            return
        for window in snapshot.visible_windows:
            idle_exempt = False
            if snapshot.idle_seconds >= self.idle_threshold_seconds:
                browser = self.browser_contexts.for_window(
                    window.app_exe, window.window_title, snapshot.timestamp,
                    min(self.browser_context_max_age_seconds, 45),
                )
                idle_exempt = bool(browser and browser.media_active)
                if not idle_exempt:
                    continue
            active_monitors.add(window.monitor_id)
            tracker = self.trackers.get(window.monitor_id)
            if tracker is None:
                tracker = SegmentAggregator(
                    self.persist,
                    self.browser_contexts,
                    checkpoint_seconds=self.checkpoint_seconds,
                    browser_context_max_age_seconds=self.browser_context_max_age_seconds,
                    sleep_gap_seconds=self.sleep_gap_seconds,
                    track_kind="visible",
                    monitor_id=window.monitor_id,
                )
                self.trackers[window.monitor_id] = tracker
            tracker.observe(
                ForegroundSnapshot(
                    timestamp=snapshot.timestamp,
                    pid=window.pid,
                    app_exe=window.app_exe,
                    app_name=window.app_name,
                    window_title=window.window_title,
                    idle_seconds=snapshot.idle_seconds,
                    session_locked=False,
                )
            )
        for monitor_id in set(self.trackers) - active_monitors:
            ended_at = snapshot.timestamp
            if snapshot.idle_seconds >= self.idle_threshold_seconds:
                ended_at -= timedelta(seconds=snapshot.idle_seconds)
            self.trackers.pop(monitor_id).close(ended_at)

    def close(self, at: datetime | None = None) -> None:
        for tracker in self.trackers.values():
            tracker.close(at)
        self.trackers.clear()
