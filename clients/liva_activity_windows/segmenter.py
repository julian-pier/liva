from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Callable

from .browser_context import BrowserContextStore
from .models import ActivitySegment, ForegroundSnapshot


class SegmentAggregator:
    def __init__(
        self,
        persist: Callable[[ActivitySegment], None],
        browser_contexts: BrowserContextStore,
        *,
        idle_threshold_seconds: int = 180,
        checkpoint_seconds: int = 15,
        browser_context_max_age_seconds: int = 15,
        sleep_gap_seconds: int = 15,
        uuid_factory: Callable[[], uuid.UUID] = uuid.uuid4,
        track_kind: str = "foreground",
        monitor_id: str | None = None,
    ) -> None:
        self.persist = persist
        self.browser_contexts = browser_contexts
        self.idle_threshold_seconds = idle_threshold_seconds
        self.checkpoint_seconds = checkpoint_seconds
        self.browser_context_max_age_seconds = browser_context_max_age_seconds
        self.sleep_gap_seconds = sleep_gap_seconds
        self.uuid_factory = uuid_factory
        self.track_kind = track_kind
        self.monitor_id = monitor_id
        self.current: ActivitySegment | None = None
        self.last_observed_at: datetime | None = None
        self.last_checkpoint_at: datetime | None = None

    def _idle_exempt(self, snapshot: ForegroundSnapshot) -> bool:
        if snapshot.session_locked or snapshot.idle_seconds < self.idle_threshold_seconds:
            return False
        browser = self.browser_contexts.for_window(
            snapshot.app_exe, snapshot.window_title, snapshot.timestamp,
            min(self.browser_context_max_age_seconds, 45),
        )
        audible_media = bool(browser and browser.media_active)
        if audible_media:
            return True
        for window in snapshot.visible_windows:
            context = self.browser_contexts.for_window(
                window.app_exe, window.window_title, snapshot.timestamp,
                min(self.browser_context_max_age_seconds, 45),
            )
            if context and context.media_active:
                return True
        return False

    def _is_idle(self, snapshot: ForegroundSnapshot) -> bool:
        no_foreground = snapshot.pid is None and not snapshot.app_exe and not snapshot.window_title
        threshold_idle = snapshot.idle_seconds >= self.idle_threshold_seconds and not self._idle_exempt(snapshot)
        return snapshot.session_locked or threshold_idle or no_foreground

    def _candidate(self, snapshot: ForegroundSnapshot, started_at: datetime | None = None) -> ActivitySegment:
        idle_exempt = self._idle_exempt(snapshot)
        is_idle = self._is_idle(snapshot)
        start = started_at or snapshot.timestamp
        browser = None if is_idle else self.browser_contexts.for_window(
            snapshot.app_exe, snapshot.window_title, snapshot.timestamp, self.browser_context_max_age_seconds
        )
        return ActivitySegment(
            id=str(self.uuid_factory()), started_at=start, ended_at=snapshot.timestamp,
            app_exe=None if is_idle else snapshot.app_exe,
            app_name=None if is_idle else snapshot.app_name,
            window_title=None if is_idle else snapshot.window_title,
            browser=browser.browser if browser else None,
            domain=browser.domain if browser else None,
            page_title=browser.page_title if browser else None,
            is_idle=is_idle,
            source="windows_visible" if self.track_kind == "visible" else "windows_agent",
            track_kind=self.track_kind,
            monitor_id=self.monitor_id,
            idle_exempt=idle_exempt,
        )

    def observe(self, snapshot: ForegroundSnapshot) -> ActivitySegment:
        if self.last_observed_at and (snapshot.timestamp - self.last_observed_at).total_seconds() > self.sleep_gap_seconds:
            if self.current:
                self.current.ended_at = self.last_observed_at
                self.persist(self.current)
            self.current = None

        candidate_start = snapshot.timestamp
        is_idle = self._is_idle(snapshot)
        if is_idle and snapshot.idle_seconds >= self.idle_threshold_seconds and not snapshot.session_locked:
            candidate_start = snapshot.timestamp - timedelta(seconds=max(0.0, snapshot.idle_seconds))
        candidate = self._candidate(snapshot, candidate_start)

        if self.current is None:
            self.current = candidate
            self.persist(self.current)
            self.last_checkpoint_at = snapshot.timestamp
        elif self.current.state_key() != candidate.state_key():
            boundary = candidate.started_at if candidate.is_idle and not self.current.idle_exempt else snapshot.timestamp
            boundary = max(self.current.started_at, min(boundary, snapshot.timestamp))
            self.current.ended_at = boundary
            self.persist(self.current)
            candidate.started_at = boundary
            candidate.ended_at = snapshot.timestamp
            self.current = candidate
            self.persist(self.current)
            self.last_checkpoint_at = snapshot.timestamp
        else:
            self.current.ended_at = snapshot.timestamp
            self.current.idle_exempt = candidate.idle_exempt
            if not self.last_checkpoint_at or (snapshot.timestamp - self.last_checkpoint_at).total_seconds() >= self.checkpoint_seconds:
                self.persist(self.current)
                self.last_checkpoint_at = snapshot.timestamp

        self.last_observed_at = snapshot.timestamp
        return self.current

    def close(self, at: datetime | None = None) -> None:
        if not self.current:
            return
        if at is not None:
            self.current.ended_at = max(self.current.started_at, at)
        self.persist(self.current)
        self.current = None
