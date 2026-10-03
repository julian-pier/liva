"""Small, authority-only router for current LIVA versus durable Memory V2 reads."""
from __future__ import annotations

import re
from typing import Final


LIVE_ROUTES: Final = {
    "CURRENT_TRAINING": {"canonical_tool": "liva_training_decision_plan", "fallback_tool": "liva_coach_read", "mode": "training_today_decision_context"},
    "CURRENT_WEIGHT": {"canonical_tool": "liva_weight_state"},
    "CURRENT_NUTRITION": {"canonical_tool": "liva_nutrition_snapshot", "fallback_tool": "liva_coach_read", "mode": "nutrition_data"},
    "CURRENT_RECOVERY": {"canonical_tool": "liva_recovery_snapshot"},
}
MEMORY_ROUTES: Final = {
    "DURABLE_TRAINING_KNOWLEDGE": {"canonical_tool": "liva_memory_recall", "scope": "personal"},
    "HISTORICAL_TRAINING": {"canonical_tool": "liva_memory_recall", "scope": "historical"},
    "TRAINING_PROCEDURE": {"canonical_tool": "liva_memory_recall", "scope": "procedures"},
}


def routing_contract() -> dict[str, dict[str, str]]:
    """Public authority map; intentionally contains no live values or answer logic."""
    return {**LIVE_ROUTES, **MEMORY_ROUTES}


def classify_training_intent(query: str) -> str | None:
    """Classify only explicit training/body intents; unrelated questions remain untouched."""
    text = " ".join(query.casefold().split())
    if not text:
        return None

    procedure = ("wie soll liva" in text or "daily check-in" in text or "daily checkin" in text or
                 "welche coaching-regeln" in text or "wie soll chatgpt" in text)
    if procedure:
        return "TRAINING_PROCEDURE"

    historical = any(token in text for token in (
        "damals", "früher", "frueher", "sommer-bootcamp", "sommer bootcamp", "bulk-phase", "bulk phase",
        "mini-cut", "mini cut", "gym-wechsel", "gym wechsel", "gewechselt", "vergangen", "historisch",
    ))
    if historical:
        return "HISTORICAL_TRAINING"

    durable = any(token in text for token in (
        "langfristig", "generell", "normalerweise", "wie funktioniere", "wie bewerte", "bedeutet", "rolle spielt",
        "warum sollte", "körperbild", "kochen", "kalorienziele", "als athlet", "coachen", "coaching",
    ))
    if durable:
        return "DURABLE_TRAINING_KNOWLEDGE"

    if any(token in text for token in ("hrv", "geschlafen", "recovered", "recovery", "erholung", "regeneration", "schlaf", "sickness", "krank", "alcohol", "alkohol gesetzt")):
        return "CURRENT_RECOVERY"
    if any(token in text for token in ("kalorien", "makros", "gegessen", "esse ich", "fehlt mir", "nutrition", "ernährung", "ernaehrung", "mahlzeit", "essen heute")):
        return "CURRENT_NUTRITION"
    if ("wiege" in text or "gewogen" in text or "gewichtstrend" in text or "gewicht aktuell" in text or
            re.search(r"\bgewicht\b", text) and any(token in text for token in ("mein", "aktuell", "heute", "entwickelt"))):
        return "CURRENT_WEIGHT"
    if any(token in text for token in (
        "trainiere ich", "nächste einheit", "naechste einheit", "welcher plan ist aktuell", "aktueller trainingsplan",
        "welche gewichte soll", "letztes workout", "läuft mein training aktuell", "laeuft mein training aktuell",
        "session kommt als nächstes", "session kommt als naechstes", "training heute", "heutiges training",
        "trainingsplan", "nächstes training", "naechstes training", "nächste trainingseinheit",
        "naechste trainingseinheit", "was steht heute im training an", "welche einheit heute",
    )):
        return "CURRENT_TRAINING"
    return None


def route_for_query(query: str) -> dict[str, str] | None:
    intent = classify_training_intent(query)
    if intent is None:
        return None
    route = routing_contract()[intent]
    return {"intent": intent, **route}
