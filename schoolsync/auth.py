from __future__ import annotations

import logging
import os
import time

from playwright.sync_api import Frame, Page, sync_playwright

from .client import SchoolSyncError, WebUntisClient
from .config import SchoolSyncConfig

LOGGER = logging.getLogger(__name__)


def bootstrap_auth_state(
    config: SchoolSyncConfig,
    max_wait_seconds: int = 600,
    *,
    force_headless: bool | None = None,
) -> None:
    if not config.base_url:
        raise SchoolSyncError("WEBUNTIS_BASE_URL is not configured.")

    launch_headless = config.auth_bootstrap_headless if force_headless is None else force_headless
    has_display = bool(os.getenv("DISPLAY"))
    has_credentials = bool(config.webuntis_username and config.webuntis_password)
    if not launch_headless and not has_display and not has_credentials:
        raise SchoolSyncError(
            "No X display available for manual login. "
            "Set WEBUNTIS_USERNAME/WEBUNTIS_PASSWORD in .env and run with --headless."
        )

    config.state_file.parent.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=launch_headless, slow_mo=50 if not launch_headless else 0)
        context = browser.new_context()
        page = context.new_page()
        try:
            LOGGER.info("Opening login page: %s", config.base_url)
            page.goto(config.base_url, wait_until="domcontentloaded", timeout=config.navigation_timeout_ms)
            _wait_until_schedule_visible(
                page,
                config,
                max_wait_seconds=max_wait_seconds,
                allow_credential_login=has_credentials,
            )
            context.storage_state(path=str(config.state_file))
            LOGGER.info("Auth state saved to %s", config.state_file)
        finally:
            context.close()
            browser.close()


def _wait_until_schedule_visible(
    page: Page,
    config: SchoolSyncConfig,
    max_wait_seconds: int,
    *,
    allow_credential_login: bool,
) -> None:
    end = time.monotonic() + max_wait_seconds
    if allow_credential_login:
        LOGGER.info("Waiting for visible timetable (will auto-submit credentials if login form appears)...")
    else:
        LOGGER.info("Please complete login in the browser window. Waiting for visible timetable...")
    last_login_try = 0.0
    timetable_navigation_attempted = False
    while time.monotonic() < end:
        now = time.monotonic()
        if allow_credential_login and (now - last_login_try) >= 2.0:
            attempted = _try_headless_login(page, config)
            if attempted:
                last_login_try = now

        if _has_verified_timetable(page):
            return

        client = WebUntisClient(config)
        if not timetable_navigation_attempted and client._has_timetable_navigation_surface(page):  # noqa: SLF001
            client._go_to_my_timetable(page)  # noqa: SLF001
            timetable_navigation_attempted = True
            page.wait_for_timeout(1200)
            if _has_verified_timetable(page):
                return

        page.wait_for_timeout(500)
    raise SchoolSyncError(
        "Timeout waiting for timetable after manual login. "
        "Check WEBUNTIS_BASE_URL or selectors in schoolsync/config.py."
    )


def _has_verified_timetable(page: Page) -> bool:
    """Require an actual timetable, not a dashboard element with a broad class name."""
    selectors = (
        "[data-testid='timetable-grid']",
        "[data-testid^='timetable-grid-card']",
        ".lesson-card",
        ".renderedEvent",
        ".timetableGrid",
        ".classreg-timetable",
    )
    roots: list[Page | Frame] = [page, *page.frames]
    for root in roots:
        for selector in selectors:
            try:
                locator = root.locator(selector).first
                if locator.count() > 0 and locator.is_visible():
                    return True
            except Exception:  # noqa: BLE001
                continue
    return False


def _try_headless_login(page: Page, config: SchoolSyncConfig) -> bool:
    username = (config.webuntis_username or "").strip()
    password = (config.webuntis_password or "").strip()
    if not username or not password:
        return False

    user_selectors = (
        "input[autocomplete='username']",
        "input[name*='user' i]",
        "input[id*='user' i]",
        "input[type='email']",
        "input[name='j_username']",
        "input[type='text']",
    )
    pass_selectors = (
        "input[type='password']",
        "input[autocomplete='current-password']",
        "input[name='j_password']",
    )
    submit_selectors = (
        "button[type='submit']",
        "input[type='submit']",
        "button:has-text('Anmelden')",
        "button:has-text('Login')",
    )

    search_roots: list[Page | Frame] = [page, *page.frames]
    for root in search_roots:
        user_field = _find_visible_first(root, user_selectors)
        pass_field = _find_visible_first(root, pass_selectors)
        if user_field is None and pass_field is not None:
            user_field = _find_first_text_like_input(root)
        if user_field is None or pass_field is None:
            continue

        LOGGER.info("Submitting WebUntis login form using configured credentials.")
        try:
            user_field.fill(username, timeout=5000)
            pass_field.fill(password, timeout=5000)
            for sel in submit_selectors:
                try:
                    btn = root.locator(sel).first
                    if btn.count() > 0 and btn.is_visible():
                        btn.click(timeout=5000)
                        page.wait_for_timeout(1500)
                        return True
                except Exception:  # noqa: BLE001
                    continue
            pass_field.press("Enter")
            page.wait_for_timeout(1500)
            return True
        except Exception:  # noqa: BLE001
            continue
    return False


def _find_visible_first(root: Page | Frame, selectors: tuple[str, ...]):
    for sel in selectors:
        loc = root.locator(sel).first
        try:
            if loc.count() > 0 and loc.is_visible():
                return loc
        except Exception:  # noqa: BLE001
            continue
    return None


def _find_first_text_like_input(root: Page | Frame):
    candidates = (
        "input[type='text']",
        "input:not([type])",
        "input[type='search']",
    )
    for sel in candidates:
        loc = root.locator(sel)
        try:
            count = loc.count()
        except Exception:  # noqa: BLE001
            continue
        for i in range(count):
            item = loc.nth(i)
            try:
                if item.is_visible():
                    return item
            except Exception:  # noqa: BLE001
                continue
    return None
