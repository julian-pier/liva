from __future__ import annotations

import sqlite3
from pathlib import Path

import database.connections as db_conn
import core.core_training_deck as training_deck
from core.core_training_deck import ensure_core_training_deck_schema, next_card


def test_recent_signatures_blocked(tmp_path: Path, monkeypatch):
    db_conn.CORE_DB = str(tmp_path / "core.sqlite3")
    ensure_core_training_deck_schema()

    monkeypatch.setattr(
        "core.core_training_deck._generate_candidates",
        lambda payload, **kwargs: [
            {"id": "1", "signature": "blocked", "text": "A", "meta": {"topic": "consistency", "kind": "principle"}, "prediction": {"answer": "yes", "confidence": 0.2, "bar_position": 0.6}},
            {"id": "2", "signature": "fresh", "text": "B", "meta": {"topic": "consistency", "kind": "principle"}, "prediction": {"answer": "no", "confidence": 0.1, "bar_position": 0.4}},
        ],
    )
    monkeypatch.setattr("core.core_training_deck.load_latest_features", lambda: {"features": {}, "summaries": {"recovery": {"summary": "x"}, "strength": {"summary": "x"}, "runs": {"summary": "x"}, "nutrition": {"summary": "x"}, "weight": {"summary": "x"}, "gap": {"summary": "x"}}})

    payload = next_card(recent_signatures=["blocked"])
    assert payload["card"]["signature"] == "fresh"


def test_last_served_signature_is_avoided(tmp_path: Path, monkeypatch):
    db_conn.CORE_DB = str(tmp_path / "core.sqlite3")
    ensure_core_training_deck_schema()

    conn = sqlite3.connect(db_conn.CORE_DB)
    conn.row_factory = sqlite3.Row
    conn.execute(
        "UPDATE core_training_state SET last_signature=?, last_card_json=? WHERE id=1",
        ("just_served", "{}"),
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(
        "core.core_training_deck._generate_candidates",
        lambda payload, **kwargs: [
            {"id": "1", "signature": "just_served", "text": "A", "meta": {"topic": "consistency", "kind": "principle"}, "prediction": {"answer": "yes", "confidence": 0.2, "bar_position": 0.6}},
            {"id": "2", "signature": "fresh_again", "text": "B", "meta": {"topic": "consistency", "kind": "principle"}, "prediction": {"answer": "no", "confidence": 0.1, "bar_position": 0.4}},
        ],
    )
    monkeypatch.setattr("core.core_training_deck.load_latest_features", lambda: {"features": {}, "summaries": {"recovery": {"summary": "x"}, "strength": {"summary": "x"}, "runs": {"summary": "x"}, "nutrition": {"summary": "x"}, "weight": {"summary": "x"}, "gap": {"summary": "x"}}})

    payload = next_card()
    assert payload["card"]["signature"] == "fresh_again"


def test_rotation_prefers_next_family_slot(tmp_path: Path, monkeypatch):
    db_conn.CORE_DB = str(tmp_path / "core.sqlite3")
    ensure_core_training_deck_schema()

    conn = sqlite3.connect(db_conn.CORE_DB)
    conn.row_factory = sqlite3.Row
    conn.execute("UPDATE core_training_state SET rotation_index=? WHERE id=1", (2,))
    conn.commit()
    conn.close()

    monkeypatch.setattr(
        "core.core_training_deck._generate_candidates",
        lambda payload, **kwargs: [
            {"id": "1", "signature": "a", "text": "A", "meta": {"topic": "consistency", "kind": "principle", "family": "consistency_frequency"}, "prediction": {"answer": "yes", "confidence": 0.1, "bar_position": 0.6}},
            {"id": "2", "signature": "b", "text": "B", "meta": {"topic": "progression_discipline", "kind": "principle", "family": "progression_discipline"}, "prediction": {"answer": "yes", "confidence": 0.1, "bar_position": 0.6}},
        ],
    )
    monkeypatch.setattr("core.core_training_deck.load_latest_features", lambda: {"features": {}, "summaries": {"recovery": {"summary": "x"}, "strength": {"summary": "x"}, "runs": {"summary": "x"}, "nutrition": {"summary": "x"}, "weight": {"summary": "x"}, "gap": {"summary": "x"}}})

    payload = next_card()
    assert payload["card"]["signature"] == "b"


def test_seen_cards_can_return_when_not_recent(tmp_path: Path, monkeypatch):
    db_conn.CORE_DB = str(tmp_path / "core.sqlite3")
    ensure_core_training_deck_schema()

    conn = sqlite3.connect(db_conn.CORE_DB)
    conn.row_factory = sqlite3.Row
    conn.execute(
        "INSERT INTO core_training_seen (signature, first_seen_at, last_seen_at, times_seen, card_topic, card_kind) VALUES (?, ?, ?, 1, ?, ?)",
        ("old_card", "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z", "consistency", "principle"),
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(
        "core.core_training_deck._generate_candidates",
        lambda payload, **kwargs: [
            {"id": "1", "signature": "old_card", "text": "A", "meta": {"topic": "consistency", "kind": "principle", "family": "consistency"}, "prediction": {"answer": "yes", "confidence": 0.1, "bar_position": 0.6}},
        ],
    )
    monkeypatch.setattr("core.core_training_deck.load_latest_features", lambda: {"features": {}, "summaries": {"recovery": {"summary": "x"}, "strength": {"summary": "x"}, "runs": {"summary": "x"}, "nutrition": {"summary": "x"}, "weight": {"summary": "x"}, "gap": {"summary": "x"}}})

    payload = next_card()
    assert payload["card"]["signature"] == "old_card"


def test_persisted_recent_seen_signature_is_avoided_after_restart(tmp_path: Path, monkeypatch):
    db_conn.CORE_DB = str(tmp_path / "core.sqlite3")
    ensure_core_training_deck_schema()

    conn = sqlite3.connect(db_conn.CORE_DB)
    conn.row_factory = sqlite3.Row
    conn.execute(
        "INSERT INTO core_training_seen (signature, first_seen_at, last_seen_at, times_seen, card_topic, card_kind) VALUES (?, ?, ?, 1, ?, ?)",
        ("recent_card", "2026-03-01T10:00:00Z", "2026-03-01T10:00:00Z", "consistency", "principle"),
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(
        "core.core_training_deck._generate_candidates",
        lambda payload, **kwargs: [
            {"id": "1", "signature": "recent_card", "text": "A", "meta": {"topic": "consistency", "kind": "principle", "family": "consistency"}, "prediction": {"answer": "yes", "confidence": 0.1, "bar_position": 0.6}},
            {"id": "2", "signature": "fresh_after_restart", "text": "B", "meta": {"topic": "consistency", "kind": "principle", "family": "consistency"}, "prediction": {"answer": "no", "confidence": 0.1, "bar_position": 0.4}},
        ],
    )
    monkeypatch.setattr("core.core_training_deck.load_latest_features", lambda: {"features": {}, "summaries": {"recovery": {"summary": "x"}, "strength": {"summary": "x"}, "runs": {"summary": "x"}, "nutrition": {"summary": "x"}, "weight": {"summary": "x"}, "gap": {"summary": "x"}}})

    payload = next_card()
    assert payload["card"]["signature"] == "fresh_after_restart"


def test_persisted_recent_seen_prompt_is_avoided_after_restart(tmp_path: Path, monkeypatch):
    db_conn.CORE_DB = str(tmp_path / "core.sqlite3")
    ensure_core_training_deck_schema()

    conn = sqlite3.connect(db_conn.CORE_DB)
    conn.row_factory = sqlite3.Row
    conn.execute(
        "INSERT INTO core_training_seen (signature, first_seen_at, last_seen_at, times_seen, card_text_norm, card_topic, card_kind) VALUES (?, ?, ?, 1, ?, ?, ?)",
        ("old_sig", "2026-03-01T10:00:00Z", "2026-03-01T10:00:00Z", "wenn du den plan änderst: gibt es dafür einen echten grund?", "plan_first", "principle"),
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(
        "core.core_training_deck._generate_candidates",
        lambda payload, **kwargs: [
            {"id": "1", "signature": "new_sig_same_text", "text": "Wenn du den Plan änderst: gibt es dafür einen echten Grund?", "meta": {"topic": "plan_first", "kind": "principle", "family": "plan_first"}, "prediction": {"answer": "yes", "confidence": 0.1, "bar_position": 0.6}},
            {"id": "2", "signature": "fresh_text", "text": "Wenn der Plan heute Rest sagt: lässt du es dann wirklich sein?", "meta": {"topic": "plan_first", "kind": "principle", "family": "plan_first"}, "prediction": {"answer": "no", "confidence": 0.1, "bar_position": 0.4}},
        ],
    )
    monkeypatch.setattr("core.core_training_deck.load_latest_features", lambda: {"features": {}, "summaries": {"recovery": {"summary": "x"}, "strength": {"summary": "x"}, "runs": {"summary": "x"}, "nutrition": {"summary": "x"}, "weight": {"summary": "x"}, "gap": {"summary": "x"}}})

    payload = next_card()
    assert payload["card"]["signature"] == "fresh_text"


def test_schema_repairs_mismatched_topics_from_seen_signature(tmp_path: Path):
    db_conn.CORE_DB = str(tmp_path / "core.sqlite3")
    training_deck._SCHEMA_READY_FOR_DB = None
    ensure_core_training_deck_schema()

    conn = sqlite3.connect(db_conn.CORE_DB)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        INSERT OR REPLACE INTO core_training_seen
        (signature, first_seen_at, last_seen_at, times_seen, card_text_norm, card_json, card_topic, card_kind)
        VALUES (?, ?, ?, 1, ?, ?, ?, ?)
        """,
        (
            "sig-repair",
            "2026-03-01T10:00:00Z",
            "2026-03-01T10:00:00Z",
            "bist du bei rpe wirklich ehrlich?",
            '{"signature":"sig-repair","text":"Bist du bei RPE wirklich ehrlich?","meta":{"topic":"rpe_honesty","kind":"principle","family":"rpe_honesty","dimension":"honesty"}}',
            "rpe_honesty",
            "principle",
        ),
    )
    conn.execute(
        """
        INSERT INTO core_training_answers
        (t, signature, card_id, topic, kind, answer, prediction, confidence, features_json, client_ms)
        VALUES ('2026-03-01T10:00:01Z', 'sig-repair', 'card-repair', 'consistency_frequency', 'principle:consistency_frequency:frequency_rule', 'yes', 'yes', 0.5, '{}', 100)
        """
    )
    conn.commit()
    conn.close()

    # Simulate a process restart so startup repair logic runs again.
    training_deck._SCHEMA_READY_FOR_DB = None
    ensure_core_training_deck_schema()

    conn = sqlite3.connect(db_conn.CORE_DB)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT topic, kind, card_text_norm FROM core_training_answers WHERE signature='sig-repair'").fetchone()
    conn.close()
    assert row["topic"] == "rpe_honesty"
    assert row["kind"] == "principle:rpe_honesty:honesty"
    assert row["card_text_norm"] == "bist du bei rpe wirklich ehrlich?"
