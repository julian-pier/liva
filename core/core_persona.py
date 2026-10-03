from __future__ import annotations

from typing import Iterable

CORE_PERSONA_GUIDELINES = {
    "name": "CORE",
    "language": "de",
    "style": [
        "ruhig",
        "direkt",
        "kurze Saetze",
        "kein Motivations-Fluff",
        "Unsicherheit transparent benennen",
        "Autonomie des Users respektieren",
    ],
}


def clamp_confidence(value: float | int) -> int:
    try:
        v = int(round(float(value)))
    except Exception:
        v = 55
    return max(5, min(95, v))


def confidence_line(value: float | int) -> str:
    return f"Sicherheit {clamp_confidence(value)}%."


def normalize_reason_lines(lines: Iterable[str], *, max_lines: int = 3) -> list[str]:
    out: list[str] = []
    for line in lines:
        text = " ".join(str(line or "").split()).strip()
        if not text:
            continue
        out.append(text[:120])
        if len(out) >= max_lines:
            break
    return out
