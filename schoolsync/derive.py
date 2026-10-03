from __future__ import annotations

from .models import DaySummary, ScheduleEntry


def derive_day_summary(entries: list[ScheduleEntry]) -> DaySummary:
    intervals = []
    for entry in entries:
        if entry.start_time and entry.end_time:
            start = _to_min(entry.start_time)
            end = _to_min(entry.end_time)
            if end > start:
                intervals.append((start, end))
    intervals.sort(key=lambda x: x[0])

    if not intervals:
        return DaySummary(
            school_start=None,
            school_end=None,
            total_blocks=len(entries),
            total_minutes_in_school=0,
            free_windows=[],
            largest_free_window_min=0,
            has_afternoon_school=False,
            is_fragmented_day=False,
        )

    merged = _merge_overlaps(intervals)
    school_start = _to_hhmm(merged[0][0])
    school_end = _to_hhmm(merged[-1][1])
    total_minutes = sum(end - start for start, end in merged)
    free_windows = _find_free_windows(merged)
    largest_gap = max((w["minutes"] for w in free_windows), default=0)
    has_afternoon_school = any(start >= (14 * 60) for start, _ in merged)
    is_fragmented = _is_fragmented(free_windows)

    return DaySummary(
        school_start=school_start,
        school_end=school_end,
        total_blocks=len(entries),
        total_minutes_in_school=total_minutes,
        free_windows=free_windows,
        largest_free_window_min=largest_gap,
        has_afternoon_school=has_afternoon_school,
        is_fragmented_day=is_fragmented,
    )


def _find_free_windows(intervals: list[tuple[int, int]]) -> list[dict[str, int | str]]:
    windows: list[dict[str, int | str]] = []
    current_end = intervals[0][1]
    for start, end in intervals[1:]:
        if start > current_end:
            windows.append(
                {
                    "start": _to_hhmm(current_end),
                    "end": _to_hhmm(start),
                    "minutes": start - current_end,
                }
            )
        current_end = max(current_end, end)
    return windows


def _merge_overlaps(intervals: list[tuple[int, int]]) -> list[tuple[int, int]]:
    if not intervals:
        return []
    merged: list[tuple[int, int]] = [intervals[0]]
    for start, end in intervals[1:]:
        last_start, last_end = merged[-1]
        if start <= last_end:
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))
    return merged


def _is_fragmented(free_windows: list[dict[str, int | str]]) -> bool:
    long_30 = [w for w in free_windows if int(w["minutes"]) >= 30]
    if len(long_30) >= 2:
        return True
    return any(int(w["minutes"]) >= 90 for w in free_windows)


def _to_min(hhmm: str) -> int:
    hour, minute = hhmm.split(":", 1)
    return int(hour) * 60 + int(minute)


def _to_hhmm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"
