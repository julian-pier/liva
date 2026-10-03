from __future__ import annotations

import json
import os
import re
from typing import List

from ai.env import get_openai_api_key
from ai.openai_responses import responses_create

DEFAULT_MODEL = os.environ.get("OPENAI_MODEL_KIENZL") or os.environ.get("OPENAI_MODEL") or "gpt-4o-mini"


def _sanitize_phrase(text: str) -> str:
    t = (text or "").strip()
    t = re.sub(r"^[-*•]\\s+", "", t)
    t = t.replace("\n", " ").replace("\r", " ")
    t = re.sub(r"\\s+", " ", t).strip()
    t = t.replace("**", "").replace("__", "").strip()
    parts = re.split(r"(?<=[.!?])\\s+", t)
    parts = [p.strip() for p in parts if p.strip()]
    if len(parts) > 2:
        t = " ".join(parts[:2]).strip()
    if len(t) > 240:
        t = t[:240].rstrip()
        t = re.sub(r"\\s+\\S+$", "", t).strip()
        if not t.endswith("."):
            t += "."
    return t


def _is_valid_phrase(text: str) -> bool:
    t = _sanitize_phrase(text)
    if not t:
        return False
    if "\n" in t:
        return False
    if any(tok in t for tok in ("```", "#", "* ", "- ")):
        return False
    if len(re.split(r"(?<=[.!?])\\s+", t.strip())) > 2:
        return False
    if len(t) < 20:
        return False
    return True


def generate_kienzl_phrases(*, prompt_seed: str, avoid: List[str], n: int) -> List[str]:
    api_key = get_openai_api_key()
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY fehlt")

    system = (
        "Du bist 'Kienzl'.\n"
        "Kienzl-Stimme: direkt, erwachsen, leicht provokant, ggf. offen kritisierend. Kein 'vielleicht', kein 'könnte', kein Coaching-Kitsch, keine Kalender-Sprüche.\n"
        "Struktur (fast immer 2 Sätze, im Notfall 3):\n"
        "- Satz 1: Signal -> Interpretation (knallhart).\n"
        "- Satz 2: Handlung -> Rahmen/Trade-off. Sehr oft Schluss-Klammer: 'Punkt.' / 'fertig.' / 'genau so.'.\n"
        "Wording-Muster (gern benutzen): 'Das ist nicht X, das ist Y.' / 'Reagiere auf Trend, nicht auf Ego.' / 'Mach’s planbarer, sonst Rauschen.' / 'Ändere eine Variable.'\n"
        "Tabus: keine Emojis, kein Markdown, keine Bullet-Listen, keine langen Erklärungen. Max 1–2 Sätze.\n"
        "Output-Regeln (hart):\n"
        "- Du gibst GENAU ein JSON-Array von Strings zurück.\n"
        "- Jeder String: 1–2 Sätze, Deutsch.\n"
        "- Keine Zahlen in den Sätzen.\n"
    )

    avoid_block = "\n".join([f"- {a}" for a in (avoid or [])[:10]])
    user = (
        "ACTION PROMPT_SEED:\n"
        f"{(prompt_seed or '').strip()}\n\n"
        "DO NOT COPY (nicht wiederholen, nicht paraphrasieren):\n"
        f"{avoid_block}\n\n"
        f"Erzeuge {int(n)} Varianten."
    )

    def call(extra: str = "") -> str:
        return responses_create(
            api_key=api_key,
            model=DEFAULT_MODEL,
            input_messages=[
                {"role": "system", "content": system + (("\n" + extra) if extra else "")},
                {"role": "user", "content": user},
            ],
            temperature=0.7,
            max_output_tokens=420,
            timeout_s=60,
        ).strip()

    last = ""
    for attempt in range(1, 3):
        extra = ""
        if attempt == 2:
            extra = "Dein letzter Output war ungültig. Gib strikt ein JSON-Array von Strings zurück, sonst nichts."
        out = call(extra)
        last = out
        try:
            arr = json.loads(out)
        except Exception:
            continue
        if not isinstance(arr, list):
            continue
        cleaned = []
        for x in arr:
            if not isinstance(x, str):
                continue
            s = _sanitize_phrase(x)
            if _is_valid_phrase(s):
                cleaned.append(s)
        uniq = []
        seen = set()
        for s in cleaned:
            k = s.lower()
            if k in seen:
                continue
            seen.add(k)
            uniq.append(s)
        if len(uniq) >= max(1, int(n * 0.7)):
            return uniq[:n]
    raise RuntimeError(f"OpenAI output invalid: {last[:200]}")
