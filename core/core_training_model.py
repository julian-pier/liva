from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from typing import Any

from database.connections import get_core_db

FEATURE_NAMES = [
    "recovery_state",
    "strength_trend",
    "run_consistency",
    "nutrition_adherence",
    "weight_trend",
    "session_gap",
    "proximity_to_failure",
    "run_load",
    "gym_frequency",
    "warning_load",
]

DEFAULT_TOPIC_WEIGHTS = {
    "volume_vs_intensity": {"strength_trend:down": 0.18, "warning_load:high": 0.22, "recovery_state:low": 0.16},
    "proximity_to_failure": {"proximity_to_failure:high": 0.38, "recovery_state:low": 0.22, "proximity_to_failure:low": -0.16},
    "progression_discipline": {"strength_trend:up": 0.22, "strength_trend:flat": 0.08, "recovery_state:low": -0.2},
    "deload_philosophy": {"warning_load:high": 0.42, "warning_load:ok": 0.2, "recovery_state:low": 0.14},
    "consistency_frequency": {"gym_frequency:low": 0.34, "session_gap:long": 0.24, "recovery_state:high": -0.08},
    "interference_run_gym": {"run_load:high": 0.28, "run_consistency:high": 0.14, "recovery_state:low": 0.12},
    "nutrition_as_limiter": {"nutrition_adherence:low": 0.4, "weight_trend:up": 0.16, "weight_trend:down": 0.12},
    "signal_weighting": {"strength_trend:down": 0.24, "recovery_state:low": 0.24, "warning_load:high": 0.18},
}

LEGACY_TOPIC_BOOTSTRAP = {
    "plan_first": ["consistency", "consistency_frequency"],
    "rpe_honesty": ["consistency"],
    "logging_quality": ["consistency"],
    "nutrition_discipline": ["consistency"],
    "regeneration_honesty": ["consistency"],
    "run_quality": ["consistency_frequency", "consistency"],
    "regression_response": ["consistency_frequency", "consistency"],
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ensure_core_training_model_schema() -> None:
    conn = get_core_db()
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS core_training_model (
                key TEXT PRIMARY KEY,
                weights_json TEXT NOT NULL DEFAULT '{}',
                bias REAL NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.commit()
    finally:
        conn.close()


def feature_vector(features: dict[str, Any]) -> dict[str, float]:
    out: dict[str, float] = {}
    for name in FEATURE_NAMES:
        bucket = str(features.get(name) or "missing")
        out[f"{name}:{bucket}"] = 1.0
    return out


def sigmoid(x: float) -> float:
    if x >= 0:
        z = math.exp(-x)
        return 1.0 / (1.0 + z)
    z = math.exp(x)
    return z / (1.0 + z)


def bar_position(prob: float) -> float:
    confidence = abs(prob - 0.5) * 2.0
    return 0.5 + (prob - 0.5) * (0.35 + 0.65 * confidence)


def _decode_weights(raw: str | None) -> dict[str, float]:
    try:
        weights = json.loads(raw or "{}")
    except Exception:
        weights = {}
    if not isinstance(weights, dict):
        weights = {}
    return {str(k): float(v) for k, v in weights.items()}


def _load_bootstrap_topic(conn, topic: str) -> tuple[dict[str, float], float] | None:
    parents = list(LEGACY_TOPIC_BOOTSTRAP.get(topic) or [])
    if not parents:
        return None
    merged: dict[str, float] = {}
    total_bias = 0.0
    sources = 0
    for parent in parents:
        row = conn.execute(
            "SELECT weights_json, bias FROM core_training_model WHERE key=?",
            (f"topic:{parent}",),
        ).fetchone()
        if not row:
            continue
        weights = _decode_weights(row["weights_json"])
        for key, value in weights.items():
            merged[key] = merged.get(key, 0.0) + value
        total_bias += float(row["bias"] or 0.0)
        sources += 1
    if not sources:
        return None
    averaged = {key: value / sources for key, value in merged.items()}
    return averaged, (total_bias / sources)


def _load_topic(conn, topic: str) -> tuple[dict[str, float], float]:
    row = conn.execute("SELECT weights_json, bias FROM core_training_model WHERE key=?", (f"topic:{topic}",)).fetchone()
    if row:
        return (_decode_weights(row["weights_json"]), float(row["bias"] or 0.0))
    bootstrapped = _load_bootstrap_topic(conn, topic)
    if bootstrapped:
        return bootstrapped
    defaults = dict(DEFAULT_TOPIC_WEIGHTS.get(topic) or {})
    return defaults, 0.0


def get_topic_snapshot(topic: str) -> dict[str, Any]:
    ensure_core_training_model_schema()
    conn = get_core_db()
    try:
        weights, bias = _load_topic(conn, topic)
        return {"weights": weights, "bias": bias}
    finally:
        conn.close()


def restore_topic_snapshot(topic: str, weights: dict[str, float], bias: float) -> None:
    ensure_core_training_model_schema()
    conn = get_core_db()
    try:
        conn.execute(
            """
            INSERT INTO core_training_model (key, weights_json, bias, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                weights_json=excluded.weights_json,
                bias=excluded.bias,
                updated_at=excluded.updated_at
            """,
            (f"topic:{topic}", json.dumps(weights, ensure_ascii=False, sort_keys=True), float(bias), _utc_now()),
        )
        conn.commit()
    finally:
        conn.close()


def predict_topic(topic: str, features: dict[str, Any]) -> dict[str, Any]:
    ensure_core_training_model_schema()
    conn = get_core_db()
    try:
        weights, bias = _load_topic(conn, topic)
    finally:
        conn.close()
    vec = feature_vector(features)
    score = bias
    for key, value in vec.items():
        score += weights.get(key, 0.0) * value
    prob = sigmoid(score)
    answer = "yes" if prob >= 0.5 else "no"
    confidence = abs(prob - 0.5) * 2.0
    return {
        "answer": answer,
        "probability": prob,
        "confidence": confidence,
        "bar_position": max(0.0, min(1.0, bar_position(prob))),
    }


def update_topic(topic: str, features: dict[str, Any], answer: str, lr: float = 0.18) -> dict[str, Any]:
    ensure_core_training_model_schema()
    vec = feature_vector(features)
    conn = get_core_db()
    try:
        weights, bias = _load_topic(conn, topic)
        score = bias + sum(weights.get(k, 0.0) * v for k, v in vec.items())
        before = sigmoid(score)
        target = 1.0 if str(answer).strip().lower() == "yes" else 0.0
        error = target - before
        bias += lr * error
        for key, value in vec.items():
            weights[key] = weights.get(key, 0.0) + lr * error * value
        conn.execute(
            """
            INSERT INTO core_training_model (key, weights_json, bias, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                weights_json=excluded.weights_json,
                bias=excluded.bias,
                updated_at=excluded.updated_at
            """,
            (f"topic:{topic}", json.dumps(weights, ensure_ascii=False, sort_keys=True), bias, _utc_now()),
        )
        conn.commit()
        after = sigmoid(bias + sum(weights.get(k, 0.0) * v for k, v in vec.items()))
        return {
            "before": before,
            "after": after,
            "delta_confidence": abs(after - 0.5) * 2.0 - abs(before - 0.5) * 2.0,
        }
    finally:
        conn.close()


def rebuild_models_from_answer_rows(rows: list[dict[str, Any]], lr: float = 0.18) -> None:
    ensure_core_training_model_schema()
    snapshots: dict[str, tuple[dict[str, float], float]] = {}
    for row in rows:
        topic = str(row.get("topic") or "").strip()
        if not topic:
            continue
        features = dict(row.get("features") or {})
        answer = "yes" if str(row.get("answer") or "").strip().lower() == "yes" else "no"
        weights, bias = snapshots.get(topic, (dict(DEFAULT_TOPIC_WEIGHTS.get(topic) or {}), 0.0))
        vec = feature_vector(features)
        score = bias + sum(weights.get(k, 0.0) * v for k, v in vec.items())
        before = sigmoid(score)
        target = 1.0 if answer == "yes" else 0.0
        error = target - before
        bias += lr * error
        for key, value in vec.items():
            weights[key] = weights.get(key, 0.0) + lr * error * value
        snapshots[topic] = (weights, bias)

    conn = get_core_db()
    try:
        conn.execute("DELETE FROM core_training_model")
        now = _utc_now()
        for topic, (weights, bias) in snapshots.items():
            conn.execute(
                """
                INSERT INTO core_training_model (key, weights_json, bias, updated_at)
                VALUES (?, ?, ?, ?)
                """,
                (f"topic:{topic}", json.dumps(weights, ensure_ascii=False, sort_keys=True), float(bias), now),
            )
        conn.commit()
    finally:
        conn.close()


__all__ = [
    "ensure_core_training_model_schema",
    "feature_vector",
    "predict_topic",
    "update_topic",
    "rebuild_models_from_answer_rows",
    "get_topic_snapshot",
    "restore_topic_snapshot",
    "sigmoid",
    "bar_position",
]
