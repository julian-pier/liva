from liva_mcp.intent_routing import route_for_query
from liva_mcp.memory_v2_service import _without_generated_heading


def test_common_german_aliases_route_to_live_data():
    assert route_for_query("Was steht heute im Training an?")["intent"] == "CURRENT_TRAINING"
    assert route_for_query("Was steht heute an?") is None
    assert route_for_query("Wie ist meine Regeneration heute?")["intent"] == "CURRENT_RECOVERY"
    assert route_for_query("Wie sieht meine Ernährung heute aus?")["intent"] == "CURRENT_NUTRITION"
    assert route_for_query("Wie war mein Training damals?")["intent"] == "HISTORICAL_TRAINING"


def test_matching_h1_is_removed_before_memory_core_generates_one():
    assert _without_generated_heading("# Routing\n\nInhalt", "Routing") == "Inhalt"
    assert _without_generated_heading("# Anderer Titel\n\nInhalt", "Routing").startswith("# Anderer Titel")
