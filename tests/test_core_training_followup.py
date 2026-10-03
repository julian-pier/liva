from __future__ import annotations

import sqlite3
from collections import Counter
from pathlib import Path

import database.connections as db_conn
from core.core_training_deck import (
    _predict_card_from_history,
    _followup_card,
    _generate_candidates,
    TEMPLATES,
    ensure_core_training_deck_schema,
    should_trigger_followup,
)


def _payload():
    return {
        "features": {
            "recovery_state": "low",
            "strength_trend": "down",
            "run_consistency": "high",
            "nutrition_adherence": "low",
            "weight_trend": "up",
            "session_gap": "normal",
            "proximity_to_failure": "high",
            "gym_frequency": "low",
            "run_load": "high",
            "warning_load": "high",
        },
        "summaries": {
            "recovery": {"summary": "Recovery wirkt gedrückt"},
            "strength": {"summary": "Leistungstrend rückläufig"},
            "runs": {"summary": "Lauf-Rhythmus stabil"},
            "nutrition": {"summary": "Ernährung war lückenhaft"},
            "weight": {"summary": "Körpergewicht steigt"},
            "gap": {"summary": "Gym-Abstand normal"},
            "failure": {"summary": "Top-Sätze nah am Limit"},
            "warnings": {"summary": "Warnlage deutlich verdichtet"},
        },
    }


def test_contradiction_triggers_followup_when_confident():
    assert should_trigger_followup("no", "yes", 0.83, contradiction_streak=0, topic_answers=3, disagreement_rate=0.2) is True


def test_aligned_answer_does_not_trigger_followup():
    assert should_trigger_followup("yes", "yes", 0.91, contradiction_streak=3, topic_answers=14, disagreement_rate=0.4) is False


def test_followup_not_gap_default(tmp_path: Path):
    db_conn.CORE_DB = str(tmp_path / "core.sqlite3")
    ensure_core_training_deck_schema()
    conn = sqlite3.connect(db_conn.CORE_DB)
    conn.row_factory = sqlite3.Row
    payload = _payload()
    followups = []
    for base in _generate_candidates(payload)[:30]:
        followups.append(_followup_card(base, payload, conn)["card"]["text"])
    conn.close()
    gap_like = [text for text in followups if ("Lücke" in text or "Gym-Tag" in text or "Abstand" in text)]
    assert len(gap_like) < max(1, int(len(followups) * 0.2))


def test_followup_science_topics_present(tmp_path: Path):
    db_conn.CORE_DB = str(tmp_path / "core.sqlite3")
    ensure_core_training_deck_schema()
    payload = _payload()
    topics = {card["meta"]["topic"] for card in _generate_candidates(payload)}
    assert {"volume_vs_intensity", "proximity_to_failure", "deload_philosophy"} <= topics


def test_question_format(tmp_path: Path):
    db_conn.CORE_DB = str(tmp_path / "core.sqlite3")
    ensure_core_training_deck_schema()
    conn = sqlite3.connect(db_conn.CORE_DB)
    conn.row_factory = sqlite3.Row
    payload = _payload()
    questions = [card["text"] for card in _generate_candidates(payload)[:40]]
    questions.extend(_followup_card(card, payload, conn)["card"]["text"] for card in _generate_candidates(payload)[:20])
    conn.close()
    for text in questions:
        assert text.startswith("Wenn")
        assert "→" in text
        assert text.endswith("?")
        assert " oder " not in text.lower()


def test_fixed_question_bank_is_large_and_balanced():
    fixed_counts = Counter(template["topic"] for template in TEMPLATES if template.get("fixed_question"))
    assert sum(fixed_counts.values()) >= 400
    for topic in (
        "consistency_frequency",
        "plan_first",
        "rpe_honesty",
        "logging_quality",
        "nutrition_discipline",
        "regeneration_honesty",
        "run_quality",
        "regression_response",
    ):
        assert fixed_counts[topic] >= 50


def test_card_prediction_learns_per_exact_question(tmp_path: Path):
    db_conn.CORE_DB = str(tmp_path / "core.sqlite3")
    ensure_core_training_deck_schema()
    conn = sqlite3.connect(db_conn.CORE_DB)
    conn.row_factory = sqlite3.Row
    card = {
        "signature": "sig-a",
        "text": "Bist du bei RPE wirklich ehrlich?",
        "meta": {"topic": "rpe_honesty"},
    }
    features = _payload()["features"]
    before = _predict_card_from_history(conn, card, features)
    for idx in range(6):
        conn.execute(
            """
            INSERT INTO core_training_answers (t, signature, card_id, topic, kind, card_text_norm, answer, prediction, confidence, features_json, client_ms)
            VALUES ('2026-03-01T00:00:00Z', ?, ?, 'rpe_honesty', 'principle:rpe_honesty:honesty', ?, 'yes', 'yes', 0.2, '{}', 100)
            """,
            (f"sig-a-{idx}", f"card-{idx}", "bist du bei rpe wirklich ehrlich?"),
        )
    after = _predict_card_from_history(conn, card, features)
    conn.close()
    assert after["confidence"] > before["confidence"]
    assert after["answer"] == "yes"
