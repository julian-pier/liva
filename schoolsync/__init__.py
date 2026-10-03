"""Minimal SchoolSync package for WebUntis timetable snapshots."""

from .config import SchoolSyncConfig, load_config
from .models import DaySummary, ScheduleEntry

__all__ = [
    "DaySummary",
    "ScheduleEntry",
    "SchoolSyncConfig",
    "load_config",
]
