from __future__ import annotations

import threading
import time
from datetime import datetime, timezone

from .models import BrowserContext

BROWSER_EXE = {
    "chrome.exe": "chrome",
    "msedge.exe": "edge",
    "opera.exe": "opera",
    "opera_gx.exe": "opera",
}


class BrowserContextStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._contexts: dict[tuple[str, int | None], BrowserContext] = {}
        self._dashboard_open_until = 0.0

    def set_dashboard_open(self, is_open: bool) -> None:
        with self._lock:
            # Extension heartbeats every 30 seconds. Expiry prevents a crashed
            # browser from leaving fast-sync enabled forever.
            self._dashboard_open_until = time.monotonic() + 45 if is_open else 0.0

    def fast_sync_requested(self) -> bool:
        with self._lock:
            return time.monotonic() < self._dashboard_open_until

    def update(self, context: BrowserContext) -> None:
        with self._lock:
            self._contexts[(context.browser.lower(), context.window_id)] = context

    def replace_browser(self, browser: str, contexts: list[BrowserContext]) -> None:
        key_browser = browser.lower()
        with self._lock:
            self._contexts = {key: value for key, value in self._contexts.items() if key[0] != key_browser}
            for context in contexts:
                self._contexts[(key_browser, context.window_id)] = context

    def for_foreground(self, app_exe: str | None, now: datetime, max_age_seconds: int) -> BrowserContext | None:
        browser = BROWSER_EXE.get((app_exe or "").lower())
        if not browser:
            return None
        with self._lock:
            candidates = [ctx for (name, _window_id), ctx in self._contexts.items() if name == browser and ctx.focused]
        context = max(candidates, key=lambda ctx: ctx.timestamp, default=None)
        if not context:
            return None
        age = (now.astimezone(timezone.utc) - context.timestamp.astimezone(timezone.utc)).total_seconds()
        return context if -5 <= age <= max_age_seconds else None

    def for_window(self, app_exe: str | None, window_title: str | None, now: datetime, max_age_seconds: int) -> BrowserContext | None:
        browser = BROWSER_EXE.get((app_exe or "").lower())
        if not browser:
            return None
        title = (window_title or "").casefold()
        with self._lock:
            candidates = [ctx for (name, _window_id), ctx in self._contexts.items() if name == browser]
        fresh = []
        for context in candidates:
            age = (now.astimezone(timezone.utc) - context.timestamp.astimezone(timezone.utc)).total_seconds()
            if -5 <= age <= max_age_seconds:
                fresh.append(context)
        title_matches = [ctx for ctx in fresh if ctx.page_title and ctx.page_title.casefold() in title]
        if title_matches:
            return max(title_matches, key=lambda ctx: len(ctx.page_title or ""))
        focused = [ctx for ctx in fresh if ctx.focused]
        return max(focused, key=lambda ctx: ctx.timestamp, default=None)
