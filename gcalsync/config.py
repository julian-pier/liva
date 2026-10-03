from __future__ import annotations

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


def _to_bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class GoogleCalendarConfig:
    credentials_file: Path
    token_file: Path
    timezone: str
    school_calendar_name: str
    default_calendar_id: str
    school_calendar_id: str
    football_calendar_id: str
    training_calendar_id: str
    work_calendar_id: str
    latest_schoolsync_file: Path
    cleanup_missing_school_events: bool


def is_primary_like_calendar_id(value: str | None) -> bool:
    raw = str(value or "").strip().lower()
    if not raw:
        return False
    if raw == "primary":
        return True
    return "@" in raw


def load_config() -> GoogleCalendarConfig:
    repo_root = Path(__file__).resolve().parents[1]
    dotenv_values = _read_dotenv(repo_root / ".env")
    return GoogleCalendarConfig(
        credentials_file=Path(
            _env_or_dotenv(
                "GOOGLE_CALENDAR_CREDENTIALS_FILE",
                dotenv_values,
                str(repo_root / "var/gcalsync/google_credentials.json"),
            )
        ),
        token_file=Path(
            _env_or_dotenv(
                "GOOGLE_CALENDAR_TOKEN_FILE",
                dotenv_values,
                str(repo_root / "var/gcalsync/google_token.json"),
            )
        ),
        timezone=(
            _env_or_dotenv("GOOGLE_CALENDAR_TIMEZONE", dotenv_values, "Europe/Berlin")
            or "Europe/Berlin"
        ),
        school_calendar_name=(
            _env_or_dotenv("GOOGLE_CALENDAR_SCHOOL_NAME", dotenv_values, "WebUntis") or "WebUntis"
        ),
        default_calendar_id=(
            _env_or_dotenv("GOOGLE_CALENDAR_DEFAULT_ID", dotenv_values, "primary") or "primary"
        ),
        school_calendar_id=(
            _env_or_dotenv("GOOGLE_CALENDAR_SCHOOL_ID", dotenv_values, "") or ""
        ),
        football_calendar_id=(
            _env_or_dotenv("GOOGLE_CALENDAR_FOOTBALL_ID", dotenv_values, "primary") or "primary"
        ),
        training_calendar_id=(
            _env_or_dotenv("GOOGLE_CALENDAR_TRAINING_ID", dotenv_values, "primary") or "primary"
        ),
        work_calendar_id=(
            _env_or_dotenv("GOOGLE_CALENDAR_WORK_ID", dotenv_values, "primary") or "primary"
        ),
        latest_schoolsync_file=Path(
            _env_or_dotenv(
                "WEBUNTIS_LATEST_TODAY_FILE",
                dotenv_values,
                str(repo_root / "var/schoolsync/latest_today.json"),
            )
        ),
        cleanup_missing_school_events=_to_bool(
            _env_or_dotenv("GOOGLE_CALENDAR_SCHOOL_CLEANUP", dotenv_values, "true"), True
        ),
    )
