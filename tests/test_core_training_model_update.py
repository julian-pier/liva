from __future__ import annotations

from pathlib import Path

import database.connections as db_conn
from core.core_training_model import ensure_core_training_model_schema, predict_topic, update_topic


def test_model_update_shifts_probability(tmp_path: Path):
    db_conn.CORE_DB = str(tmp_path / "core.sqlite3")
    ensure_core_training_model_schema()
    features = {
        "recovery_state": "low",
        "strength_trend": "flat",
        "run_consistency": "ok",
        "nutrition_adherence": "ok",
        "weight_trend": "flat",
        "session_gap": "long",
        "proximity_to_failure": "high",
        "run_load": "ok",
        "gym_frequency": "low",
        "warning_load": "ok",
    }
    before = predict_topic("progression_discipline", features)["probability"]
    for _ in range(4):
        update_topic("progression_discipline", features, "no")
    after = predict_topic("progression_discipline", features)["probability"]
    assert after < before


def test_new_principle_topic_bootstraps_from_legacy_topic(tmp_path: Path):
    db_conn.CORE_DB = str(tmp_path / "core.sqlite3")
    ensure_core_training_model_schema()
    features = {
        "recovery_state": "low",
        "strength_trend": "flat",
        "run_consistency": "ok",
        "nutrition_adherence": "ok",
        "weight_trend": "flat",
        "session_gap": "long",
        "proximity_to_failure": "high",
        "run_load": "ok",
        "gym_frequency": "low",
        "warning_load": "ok",
    }
    for _ in range(5):
        update_topic("consistency", features, "yes")
    prediction = predict_topic("plan_first", features)
    assert prediction["confidence"] > 0.0
    assert prediction["probability"] != 0.5
