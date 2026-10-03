from __future__ import annotations

from datetime import date, timedelta

from core import core_control_room


def _history_row(day_offset: int, weight: float, reps: int) -> dict[str, object]:
    return {
        "date": (date(2026, 3, 3) - timedelta(days=day_offset)).isoformat(),
        "weight": weight,
        "reps": reps,
    }


def test_exercise_plateau_snapshot_requires_repeated_recent_stagnation():
    history = [
        _history_row(42, 80, 8),
        _history_row(35, 82.5, 8),
        _history_row(28, 82.5, 8),
        _history_row(21, 82.5, 7),
        _history_row(14, 82.5, 7),
        _history_row(7, 82.5, 7),
    ]

    snapshot = core_control_room._exercise_plateau_snapshot(history, today=date(2026, 3, 3))

    assert snapshot["should_trigger"] is True
    assert snapshot["history_count_recent"] == 6
    assert snapshot["comparison_count"] == 5
    assert snapshot["stalled_count"] >= 2
    assert snapshot["latest_stalled"] is True
    assert snapshot["trend"] in {"flat", "down"}
    assert snapshot["plateau_signature"] == "2026-02-24:4:1"


def test_exercise_plateau_snapshot_skips_old_or_progressing_work():
    old_history = [
        _history_row(180, 80, 8),
        _history_row(170, 80, 8),
        _history_row(160, 80, 8),
        _history_row(150, 80, 8),
        _history_row(140, 80, 8),
        _history_row(130, 80, 8),
    ]
    progressing_history = [
        _history_row(35, 80, 8),
        _history_row(28, 82.5, 8),
        _history_row(21, 82.5, 9),
        _history_row(14, 85, 8),
        _history_row(7, 85, 9),
        _history_row(0, 87.5, 8),
    ]

    old_snapshot = core_control_room._exercise_plateau_snapshot(old_history, today=date(2026, 3, 3))
    progressing_snapshot = core_control_room._exercise_plateau_snapshot(progressing_history, today=date(2026, 3, 3))

    assert old_snapshot["should_trigger"] is False
    assert progressing_snapshot["should_trigger"] is False
    assert progressing_snapshot["stalled_count"] == 0
    assert progressing_snapshot["progress_count"] >= 3
