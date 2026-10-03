from __future__ import annotations

from liva_mcp.intent_routing import classify_training_intent, routing_contract
from liva_mcp.memory_v2_service import MemoryV2Service
from liva_mcp.tool_registry import TOOL_BY_NAME


def test_training_routing_matrix_has_no_clear_current_or_historical_confusion():
    cases = {
        # Current training (8)
        "Was trainiere ich heute?": "CURRENT_TRAINING", "Was ist meine nächste Einheit?": "CURRENT_TRAINING",
        "Welcher Plan ist aktuell?": "CURRENT_TRAINING", "Was ist mein aktueller Trainingsplan?": "CURRENT_TRAINING",
        "Welche Gewichte soll ich heute nehmen?": "CURRENT_TRAINING", "Wie war mein letztes Workout?": "CURRENT_TRAINING",
        "Wie läuft mein Training aktuell?": "CURRENT_TRAINING", "Welche Session kommt als Nächstes?": "CURRENT_TRAINING",
        # Weight (7)
        "Wie viel wiege ich aktuell?": "CURRENT_WEIGHT", "Was habe ich heute gewogen?": "CURRENT_WEIGHT",
        "Was ist mein Gewichtstrend?": "CURRENT_WEIGHT", "Wie entwickelt sich mein Gewicht?": "CURRENT_WEIGHT",
        "Was ist mein Gewicht?": "CURRENT_WEIGHT", "Wie viel wiege ich?": "CURRENT_WEIGHT",
        "Ist mein Gewicht heute höher?": "CURRENT_WEIGHT",
        # Nutrition (8)
        "Wie viele Kalorien esse ich aktuell?": "CURRENT_NUTRITION", "Was sind meine Makros?": "CURRENT_NUTRITION",
        "Was habe ich heute gegessen?": "CURRENT_NUTRITION", "Wie viel fehlt mir heute noch?": "CURRENT_NUTRITION",
        "Wie viele Kalorien fehlen mir heute noch?": "CURRENT_NUTRITION", "Wie sieht meine Nutrition heute aus?": "CURRENT_NUTRITION",
        "Was esse ich heute?": "CURRENT_NUTRITION", "Welche Makros habe ich heute?": "CURRENT_NUTRITION",
        # Recovery (7)
        "Wie ist meine HRV?": "CURRENT_RECOVERY", "Wie habe ich geschlafen?": "CURRENT_RECOVERY",
        "Bin ich heute recovered?": "CURRENT_RECOVERY", "Ist heute sickness gesetzt?": "CURRENT_RECOVERY",
        "Ist heute alcohol gesetzt?": "CURRENT_RECOVERY", "Wie ist meine Recovery heute?": "CURRENT_RECOVERY",
        "Wie sieht meine Recovery aus?": "CURRENT_RECOVERY",
        # Durable (9)
        "Wie sollte man mich im Gym coachen?": "DURABLE_TRAINING_KNOWLEDGE", "Wie funktioniere ich als Athlet?": "DURABLE_TRAINING_KNOWLEDGE",
        "Wie bewerte ich Fortschritt?": "DURABLE_TRAINING_KNOWLEDGE", "Wie gehe ich langfristig mit Recovery um?": "DURABLE_TRAINING_KNOWLEDGE",
        "Welche Rolle spielt Körperbild für mich?": "DURABLE_TRAINING_KNOWLEDGE", "Was bedeutet Gewicht für mein Körperbild?": "DURABLE_TRAINING_KNOWLEDGE",
        "Wie gehe ich langfristig mit Kalorienzielen um?": "DURABLE_TRAINING_KNOWLEDGE", "Was esse ich normalerweise?": "DURABLE_TRAINING_KNOWLEDGE",
        "Was bedeutet Training heute für meine Identität?": "DURABLE_TRAINING_KNOWLEDGE",
        # Historical (7)
        "Warum habe ich damals das Gym gewechselt?": "HISTORICAL_TRAINING", "Was war im Sommer-Bootcamp?": "HISTORICAL_TRAINING",
        "Wie lief meine frühere Bulk-Phase?": "HISTORICAL_TRAINING", "Was waren früher meine Ziele beim Bankdrücken?": "HISTORICAL_TRAINING",
        "Was passierte beim Gym-Wechsel?": "HISTORICAL_TRAINING", "Wie war mein Mini-Cut?": "HISTORICAL_TRAINING",
        "Was war historisch mit meinem Training?": "HISTORICAL_TRAINING",
        # Procedure (5)
        "Wie soll LIVA bei einem Daily Check-in vorgehen?": "TRAINING_PROCEDURE", "Welche Coaching-Regeln gelten?": "TRAINING_PROCEDURE",
        "Wie soll ChatGPT mich im Gym coachen?": "TRAINING_PROCEDURE", "Was macht LIVA im Daily Checkin?": "TRAINING_PROCEDURE",
        "Wie soll LIVA trainieren entscheiden?": "TRAINING_PROCEDURE",
    }
    assert len(cases) == 51
    for query, expected in cases.items():
        assert classify_training_intent(query) == expected, query


def test_current_memory_recall_is_a_live_authority_redirect_without_memory_results():
    service = object.__new__(MemoryV2Service)
    result = service.recall({"query": "Wie viele Kalorien esse ich aktuell?"})
    assert result["answer_context"] == []
    assert result["sources_available"] is False
    assert result["routing"]["intent"] == "CURRENT_NUTRITION"
    assert result["routing"]["canonical_tool"] == "liva_nutrition_snapshot"
    assert result["routing"]["memory_called"] is False


def test_context_and_memory_descriptions_publish_the_authority_boundary(service):
    snapshot = service.context_snapshot()
    contract = snapshot["routing"]
    assert contract["CURRENT_TRAINING"]["canonical_tool"] == "liva_training_decision_plan"
    assert contract["CURRENT_WEIGHT"]["canonical_tool"] == "liva_weight_state"
    assert contract["CURRENT_NUTRITION"]["canonical_tool"] == "liva_nutrition_snapshot"
    assert contract["CURRENT_RECOVERY"]["canonical_tool"] == "liva_recovery_snapshot"
    assert routing_contract()["DURABLE_TRAINING_KNOWLEDGE"]["canonical_tool"] == "liva_memory_recall"
    description = TOOL_BY_NAME["liva_memory_recall"].description.casefold()
    assert "not canonical" in description and "current weight" in description
