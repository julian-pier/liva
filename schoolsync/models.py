from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class RawLessonBlock:
    raw_text: str
    selector: str
    class_name: str | None
    title: str | None
    aria_label: str | None
    data_date: str | None
    data_start: str | None
    data_end: str | None
    data_start_datetime: str | None
    data_end_datetime: str | None
    data_subject: str | None
    data_teacher: str | None
    data_room: str | None
    bbox_top: float | None
    bbox_bottom: float | None
    bbox_left: float | None


@dataclass(frozen=True)
class ScheduleEntry:
    date: str
    subject: str | None
    teacher: str | None
    room: str | None
    start_time: str | None
    end_time: str | None
    raw_text: str
    status_hint: str
    source_selector: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "date": self.date,
            "subject": self.subject,
            "teacher": self.teacher,
            "room": self.room,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "raw_text": self.raw_text,
            "status_hint": self.status_hint,
            "source_selector": self.source_selector,
        }


@dataclass(frozen=True)
class DaySummary:
    school_start: str | None
    school_end: str | None
    total_blocks: int
    total_minutes_in_school: int
    free_windows: list[dict[str, Any]]
    largest_free_window_min: int
    has_afternoon_school: bool
    is_fragmented_day: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "school_start": self.school_start,
            "school_end": self.school_end,
            "total_blocks": self.total_blocks,
            "total_minutes_in_school": self.total_minutes_in_school,
            "free_windows": self.free_windows,
            "largest_free_window_min": self.largest_free_window_min,
            "has_afternoon_school": self.has_afternoon_school,
            "is_fragmented_day": self.is_fragmented_day,
        }
