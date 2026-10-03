from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path


def _read_dotenv(dotenv_path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not dotenv_path.exists():
        return values
    try:
        raw = dotenv_path.read_text(encoding="utf-8")
    except OSError:
        return values
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'").strip('"')
        if key:
            values[key] = value
    return values


def _env_or_dotenv(name: str, dotenv_values: dict[str, str], default: str | None = None) -> str | None:
    env_value = os.getenv(name)
    if env_value is not None:
        return env_value
    if name in dotenv_values:
        return dotenv_values[name]
    return default


@dataclass(frozen=True)
class WebUntisSelectors:
    # If your WebUntis tenant renders a different timetable container, adjust this tuple.
    schedule_visible_selectors: tuple[str, ...] = (
        "[data-testid='timetable-grid']",
        "[data-testid='timetable']",
        "[data-testid='week-view']",
        "[data-testid='timetable-view']",
        "[data-testid^='timetable-grid-card']",
        ".lesson-card",
        ".renderedEvent",
        ".timetableGrid",
        ".classreg-timetable",
        "[class*='timetable']",
        "[class*='period-grid']",
    )
    login_form_selectors: tuple[str, ...] = (
        "input[type='password']",
        "form[action*='login']",
        "button[type='submit']",
    )
    today_button_selectors: tuple[str, ...] = (
        "button:has-text('Heute')",
        "button:has-text('Today')",
        "[role='button']:has-text('Heute')",
        "[role='button']:has-text('Today')",
        "a:has-text('Heute')",
        "a:has-text('Today')",
    )
    week_button_selectors: tuple[str, ...] = (
        "button:has-text('Woche')",
        "button:has-text('Week')",
        "[role='button']:has-text('Woche')",
        "[role='button']:has-text('Week')",
        "a:has-text('Woche')",
        "a:has-text('Week')",
    )
    next_week_button_selectors: tuple[str, ...] = (
        "button[data-testid='date-picker-with-arrows-next']",
        "[data-testid='date-picker-with-arrows-next']",
        "button:has(svg[data-src='/assets/icons/arrow-right.svg'])",
        "button:has(.untis-icon-arrow-right)",
        "button:has-text('Nächste Woche')",
        "button:has-text('Next week')",
        "[role='button']:has-text('Nächste Woche')",
        "[role='button']:has-text('Next week')",
        "button[aria-label*='Nächste Woche' i]",
        "button[aria-label*='Next week' i]",
    )
    timetable_menu_selectors: tuple[str, ...] = (
        "a[href*='/timetable/my-student']",
        "a.menu-item-link-Mein.Stundenplan",
        "a:has-text('Mein Stundenplan')",
        "a:has-text('My Timetable')",
    )
    # Main place to tune if your DOM differs: choose selectors that target one lesson block each.
    lesson_block_selectors: tuple[str, ...] = (
        ".lesson-card.clickable",
        ".lesson-card.no-shadow.clickable",
        "[data-testid*='event']",
        "[data-testid*='lesson']",
        ".renderedEvent",
        "[class*='timetable'] [class*='event']",
        "[class*='timetable'] [class*='lesson']",
        "[class*='period'] [class*='item']",
    )


@dataclass(frozen=True)
class SchoolSyncConfig:
    base_url: str
    state_file: Path
    latest_today_file: Path
    headless: bool
    navigation_timeout_ms: int
    wait_timeout_ms: int
    retries: int
    debug_enabled: bool
    debug_dir: Path
    webuntis_username: str | None
    webuntis_password: str | None
    auth_bootstrap_headless: bool
    selectors: WebUntisSelectors


def _to_bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _to_int(value: str | None, default: int) -> int:
    if value is None:
        return default
    try:
        return int(value.strip())
    except (TypeError, ValueError):
        return default


def load_config() -> SchoolSyncConfig:
    repo_root = Path(__file__).resolve().parents[1]
    dotenv_values = _read_dotenv(repo_root / ".env")
    base_url = (_env_or_dotenv("WEBUNTIS_BASE_URL", dotenv_values, "") or "").strip()
    state_file = Path(
        (_env_or_dotenv("WEBUNTIS_STATE_FILE", dotenv_values, str(repo_root / "var/playwright/webuntis_state.json")))
    )
    latest_today_file = Path(
        (_env_or_dotenv("WEBUNTIS_LATEST_TODAY_FILE", dotenv_values, str(repo_root / "var/schoolsync/latest_today.json")))
    )
    return SchoolSyncConfig(
        base_url=base_url,
        state_file=state_file,
        latest_today_file=latest_today_file,
        headless=_to_bool(_env_or_dotenv("WEBUNTIS_HEADLESS", dotenv_values), default=True),
        navigation_timeout_ms=_to_int(_env_or_dotenv("WEBUNTIS_NAV_TIMEOUT_MS", dotenv_values), default=30000),
        wait_timeout_ms=_to_int(_env_or_dotenv("WEBUNTIS_WAIT_TIMEOUT_MS", dotenv_values), default=12000),
        retries=max(0, _to_int(_env_or_dotenv("WEBUNTIS_RETRIES", dotenv_values), default=2)),
        debug_enabled=_to_bool(_env_or_dotenv("WEBUNTIS_DEBUG", dotenv_values), default=True),
        debug_dir=Path(
            (_env_or_dotenv("WEBUNTIS_DEBUG_DIR", dotenv_values, str(repo_root / "var/schoolsync/debug")))
        ),
        webuntis_username=(
            _env_or_dotenv("WEBUNTIS_USERNAME", dotenv_values, None) or None
        ),
        webuntis_password=(
            _env_or_dotenv("WEBUNTIS_PASSWORD", dotenv_values, None) or None
        ),
        auth_bootstrap_headless=_to_bool(
            _env_or_dotenv("WEBUNTIS_AUTH_BOOTSTRAP_HEADLESS", dotenv_values, "false"),
            default=False,
        ),
        selectors=WebUntisSelectors(),
    )


def setup_logging(level: int = logging.INFO) -> None:
    logging.basicConfig(
        level=level,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )
