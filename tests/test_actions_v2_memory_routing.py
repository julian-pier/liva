from __future__ import annotations

from tests.test_actions_v2_api import auth, client  # noqa: F401


def test_memory_update_and_remove_route_to_health_notes(client):
    stored = client.post(
        "/api/v2/actions/memory/store",
        json={"command": "store_fact", "topic": "sickness", "text": "User war krank und brauchte Pause."},
        headers=auth(),
    ).get_json()
    assert stored["result"]["memory_write"]["logical_file"] == "health_notes"

    updated = client.post(
        "/api/v2/actions/memory/store",
        json={"command": "update_entry", "logical_file": "health_notes", "match_text": "Bestehende Recovery-Notiz", "content": "Recovery war durch Krankheit eingeschränkt."},
        headers=auth(),
    ).get_json()
    assert updated["ok"] is True
    assert updated["result"]["updated"] is True

    removed = client.post(
        "/api/v2/actions/memory/store",
        json={"command": "remove_entry", "logical_file": "health_notes", "text": "Recovery war durch Krankheit eingeschränkt."},
        headers=auth(),
    ).get_json()
    assert removed["ok"] is True
    assert removed["result"]["removed"] is True
