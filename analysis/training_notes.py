from __future__ import annotations

from typing import Any, Iterable


def progression_exclusion_note(sets: Iterable[dict[str, Any]]) -> str | None:
    """Build one compact, user-facing note from per-set exclusions."""

    rows = [row for row in sets if isinstance(row, dict)]
    excluded_with_positions = [
        (index, row) for index, row in enumerate(rows) if bool(row.get("progression_excluded"))
    ]
    if not excluded_with_positions:
        return None

    reasons: list[str] = []
    for _, row in excluded_with_positions:
        reason = str(row.get("progression_exclusion_reason") or "").strip()
        if reason and reason not in reasons:
            reasons.append(reason)
    reason_text = " | ".join(reasons) or "Nicht mit der bisherigen Ausführung vergleichbar."

    if len(excluded_with_positions) == len(rows):
        prefix = "Progressionsbewertung ausgeschlossen"
    else:
        numbers = [str(row.get("set_number") or index + 1) for index, row in excluded_with_positions]
        noun = "Satz" if len(numbers) == 1 else "Sätze"
        prefix = f"Progressionsbewertung ausgeschlossen ({noun} {', '.join(numbers)})"
    return f"{prefix}: {reason_text}"


def merge_exercise_notes(notes: Any, sets: Iterable[dict[str, Any]]) -> str:
    """Keep authored notes and add a non-duplicated progression exclusion note."""

    authored = str(notes or "").strip()
    exclusion = progression_exclusion_note(sets)
    if not exclusion:
        return authored
    if exclusion in authored:
        return authored
    return "\n".join(part for part in (authored, exclusion) if part)
