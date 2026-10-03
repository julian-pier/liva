from __future__ import annotations

import json
import os
import re

from ai.env import get_openai_api_key
from ai.openai_responses import responses_create
MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")

MIN_WORDS = 140
MIN_LINES = 4
MAX_LINES = 6
REQ_START_LINE4 = "Wenn du mich fragst, solltest du"
REQ_SIGNAL = "sag ich dir Bescheid"

FORBIDDEN_PATTERNS = [
    r"\\bdeutet darauf hin\\b",
    r"\\bhinweist\\b",
    r"\\bzeigt, dass\\b",
    r"\\bbedeutet, dass\\b",
    r"\\bspricht für\\b",
    r"\\bweil\\b",
    r"\\bdeshalb\\b",
    r"\\bdadurch\\b",
    r"\\bum .* zu\\b",
    r"\\bvermutlich\\b",
    r"\\bwahrscheinlich\\b",
    r"\\bmöglicherweise\\b",
]


def _word_count(s: str) -> int:
    return len(re.findall(r"\\S+", s or ""))


def _clean_lines(text: str) -> list[str]:
    lines = [ln.strip() for ln in (text or "").splitlines()]
    lines = [ln for ln in lines if ln]
    return lines


def _violates_forbidden(text: str) -> bool:
    t = (text or "").lower()
    for pat in FORBIDDEN_PATTERNS:
        if re.search(pat, t, flags=re.IGNORECASE):
            return True
    return False


def _enforce_line_count(text: str) -> str:
    t = (text or "").strip()
    lines = _clean_lines(t)
    if len(lines) >= MIN_LINES:
        return "\\n".join(lines[:MAX_LINES]).strip()
    parts = re.split(r"(?<=[.!?])\\s+", t)
    parts = [p.strip() for p in parts if p.strip()]
    return "\\n".join(parts[:MAX_LINES]).strip()


def _passes_gate(text: str) -> tuple[bool, str]:
    t = (text or "").strip()
    t = _enforce_line_count(t)
    lines = _clean_lines(t)

    if len(lines) < MIN_LINES:
        return False, f"zu wenige Zeilen ({len(lines)}<{MIN_LINES})"
    if _word_count(t) < MIN_WORDS:
        return False, f"zu kurz ({_word_count(t)}<{MIN_WORDS} Wörter)"

    has_action = any(ln.startswith(REQ_START_LINE4) for ln in lines)
    if not has_action:
        return False, "Handlungsempfehlung fehlt / falscher Startsatz"
    if REQ_SIGNAL not in t:
        return False, "Leitplanke fehlt"
    if _violates_forbidden(t):
        return False, "verbotene Phrase enthalten"
    if ";" in t:
        return False, "Semikolon gefunden"
    return True, "ok"


def generate_coach_note(profile_text: str, facts: dict) -> str:
    api_key = get_openai_api_key()
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY fehlt")

    system = (
        "Du bist mein persönlicher Performance-Coach im LIVA – wir arbeiten seit Jahren zusammen. "
        "Du schreibst entspannt, locker und coachig, aber fachlich sauber und sprachlich korrekt.\n\n"
        "HARTER OUTPUT-VERTRAG:\n"
        f"- 4 bis 6 Sätze (niemals weniger als {MIN_LINES}).\n"
        "- Nach JEDEM Satz genau ein Zeilenumbruch '\\n' (am Ende 4–6 Zeilen).\n"
        f"- Mindestens {MIN_WORDS} Wörter.\n"
        "- Keine Überschriften/Labels. Keine Listen. Keine Semikolons.\n\n"
        "KEINE FALSCHE KAUSALITÄT / KEIN STORYTELLING:\n"
        "- Verboten sind: 'deutet darauf hin', 'hinweist', 'zeigt, dass', 'bedeutet, dass', 'spricht für', "
        "'weil', 'deshalb', 'dadurch', 'um ... zu', 'vermutlich', 'wahrscheinlich', 'möglicherweise'.\n\n"
        "SATZ-PLAN:\n"
        "Satz 1 startet exakt mit 'Diese Woche hattest du …'.\n"
        "Satz 4 startet exakt mit "
        f"'{REQ_START_LINE4} …' und endet IMMER mit: '… und wenn ein klares Belastungssignal auftaucht, {REQ_SIGNAL}.'\n"
    )

    user = (
        "PROFILE:\n"
        f"{profile_text}\n\n"
        "FACTS (JSON):\n"
        f"{json.dumps(facts, ensure_ascii=False)}"
    )

    def call(extra_system: str = "") -> str:
        return responses_create(
            api_key=api_key,
            model=MODEL,
            input_messages=[
                {"role": "system", "content": system + (("\n\n" + extra_system) if extra_system else "")},
                {"role": "user", "content": user},
            ],
            temperature=0.2,
            max_output_tokens=520,
            timeout_s=60,
        ).strip()

    last = ""
    for attempt in range(1, 4):
        extra = ""
        if attempt > 1:
            extra = "DEIN LETZTER OUTPUT WAR UNGÜLTIG. Schreibe komplett neu und halte den Output-Vertrag ein."
        out = _enforce_line_count(call(extra))
        ok, _ = _passes_gate(out)
        if ok:
            return out
        last = out
    return last
