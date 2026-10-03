from __future__ import annotations

import hashlib
import json
import random
import re
import uuid
from collections import Counter
from datetime import datetime, timezone
from typing import Any

from database import connections as db_conn
from database.connections import get_core_db
from core.core_training_features import load_latest_features
from core.core_training_model import (
    ensure_core_training_model_schema,
    get_topic_snapshot,
    predict_topic,
    rebuild_models_from_answer_rows,
    restore_topic_snapshot,
    update_topic,
)
from core.core_training_question_bank import build_fixed_question_templates
from core.core_training_policy import (
    ensure_core_question_policy_schema,
    ingest_training_answer,
    rebuild_core_question_policy,
)

TOPIC_COPY = {
    "volume_vs_intensity": "Volumen × Intensität",
    "proximity_to_failure": "RPE × Limitnähe",
    "progression_discipline": "Progression × Disziplin",
    "deload_philosophy": "Entlastung × Timing",
    "consistency_frequency": "Konstanz × Frequenz",
    "interference_run_gym": "Lauf × Kraft",
    "nutrition_as_limiter": "Ernährung × Limiter",
    "signal_weighting": "Signale × Gewichtung",
    "plan_first": "Plan zuerst",
    "rpe_honesty": "RPE-Ehrlichkeit",
    "logging_quality": "Logging-Qualität",
    "nutrition_discipline": "Essen × Disziplin",
    "regeneration_honesty": "Regeneration",
    "run_quality": "Lauf-Qualität",
    "regression_response": "Umgang mit Regression",
    "calendar_timing_logic": "Kalender × Timing",
    "telegram_ops_discipline": "Telegram × Ops",
    "inventory_prep_shopping": "Vorrat × Prep",
    "explainability_governance": "Explain × Regeln",
    "macro_scaling_control": "Makros × Mengen",
    "data_freshness_integrity": "Daten × Frische",
}

FAMILY_ROTATION = [
    "volume_vs_intensity",
    "proximity_to_failure",
    "progression_discipline",
    "deload_philosophy",
    "consistency_frequency",
    "interference_run_gym",
    "nutrition_as_limiter",
    "signal_weighting",
    "plan_first",
    "rpe_honesty",
    "logging_quality",
    "nutrition_discipline",
    "regeneration_honesty",
    "run_quality",
    "regression_response",
    "calendar_timing_logic",
    "telegram_ops_discipline",
    "inventory_prep_shopping",
    "explainability_governance",
    "macro_scaling_control",
    "data_freshness_integrity",
]

FOLLOWUP_DIMENSIONS = {
    "volume_knob": "Stellhebel",
    "severity": "Schwelle",
    "persistence": "Dauer",
    "rpe_guard": "Limitnähe",
    "progression_rule": "Progression",
    "deload_threshold": "Entlastung",
    "frequency_rule": "Frequenz",
    "interference_horizon": "Interferenz",
    "nutrition_trigger": "Ernährung",
    "signal_priority": "Signalgewichtung",
}

PRINCIPLE_TOPICS = {
    "plan_first": {
        "label": "Plan-First",
        "orientation": "strength_yes",
        "type": "Principle",
    },
    "rpe_honesty": {
        "label": "RPE-Ehrlichkeit",
        "orientation": "strength_yes",
        "type": "Principle",
    },
    "logging_quality": {
        "label": "Logging-Qualität",
        "orientation": "risk_yes",
        "type": "Principle",
    },
    "nutrition_discipline": {
        "label": "Essen/Disziplin",
        "orientation": "risk_yes",
        "type": "Principle",
    },
    "regeneration_honesty": {
        "label": "Regeneration",
        "orientation": "risk_yes",
        "type": "Principle",
    },
    "run_quality": {
        "label": "Lauf-Qualität",
        "orientation": "risk_yes",
        "type": "Principle",
    },
    "regression_response": {
        "label": "Umgang mit Regression",
        "orientation": "risk_yes",
        "type": "Principle",
    },
    "calendar_timing_logic": {
        "label": "Kalender/Timing",
        "orientation": "strength_yes",
        "type": "Principle",
    },
    "telegram_ops_discipline": {
        "label": "Telegram/Ops",
        "orientation": "strength_yes",
        "type": "Principle",
    },
    "inventory_prep_shopping": {
        "label": "Vorrat/Prep",
        "orientation": "strength_yes",
        "type": "Principle",
    },
    "explainability_governance": {
        "label": "Explain/Regeln",
        "orientation": "strength_yes",
        "type": "Principle",
    },
    "macro_scaling_control": {
        "label": "Makros/Mengen",
        "orientation": "strength_yes",
        "type": "Principle",
    },
    "data_freshness_integrity": {
        "label": "Datenfrische",
        "orientation": "strength_yes",
        "type": "Principle",
    },
}

TEMPLATES = [
    {
        "id": "brake_reduce_sets_first",
        "topic": "volume_vs_intensity",
        "kind": "principle",
        "family": "volume_vs_intensity",
        "dimension": "volume_knob",
        "pill": "Volumen × Intensität",
        "base": "Wenn CORE bremsen soll → lieber weniger Sätze als weniger Intensität?",
        "depends": ["recovery_state", "strength_trend", "warning_load"],
        "followup_dimensions": ["severity", "persistence", "volume_knob"],
    },
    {
        "id": "brake_reduce_intensity_first",
        "topic": "volume_vs_intensity",
        "kind": "principle",
        "family": "volume_vs_intensity",
        "dimension": "volume_knob",
        "pill": "Intensität × Volumen",
        "base": "Wenn CORE bremsen soll → lieber weniger Intensität als weniger Sätze?",
        "depends": ["recovery_state", "strength_trend", "proximity_to_failure"],
        "followup_dimensions": ["severity", "persistence", "volume_knob"],
    },
    {
        "id": "top_sets_near_limit_brake",
        "topic": "proximity_to_failure",
        "kind": "principle",
        "family": "proximity_to_failure",
        "dimension": "rpe_guard",
        "pill": "RPE × Limitnähe",
        "base": "Wenn die letzten Top-Sätze {rpe_label} → soll CORE früher bremsen?",
        "depends": ["proximity_to_failure", "recovery_state"],
        "followup_dimensions": ["persistence", "severity", "rpe_guard"],
    },
    {
        "id": "top_sets_moderate_continue",
        "topic": "proximity_to_failure",
        "kind": "control",
        "family": "proximity_to_failure",
        "dimension": "rpe_guard",
        "pill": "RPE × Puffer",
        "base": "Wenn die letzten Top-Sätze {rpe_label} → soll CORE normal weitermachen?",
        "depends": ["proximity_to_failure", "strength_trend"],
        "followup_dimensions": ["persistence", "signal_priority", "rpe_guard"],
    },
    {
        "id": "fluctuating_performance_force_steps",
        "topic": "progression_discipline",
        "kind": "principle",
        "family": "progression_discipline",
        "dimension": "progression_rule",
        "pill": "Progression × Disziplin",
        "base": "Wenn die Leistung schwankt, aber nicht klar fällt → soll CORE trotzdem kleine Schritte erzwingen?",
        "depends": ["strength_trend", "recovery_state"],
        "followup_dimensions": ["severity", "progression_rule", "signal_priority"],
    },
    {
        "id": "fluctuating_performance_stabilize_first",
        "topic": "progression_discipline",
        "kind": "principle",
        "family": "progression_discipline",
        "dimension": "progression_rule",
        "pill": "Progression × Stabilität",
        "base": "Wenn die Leistung schwankt → soll CORE erst stabilisieren, bevor es steigert?",
        "depends": ["strength_trend", "recovery_state", "proximity_to_failure"],
        "followup_dimensions": ["severity", "progression_rule", "rpe_guard"],
    },
    {
        "id": "early_deload_on_multiple_signals",
        "topic": "deload_philosophy",
        "kind": "principle",
        "family": "deload_philosophy",
        "dimension": "deload_threshold",
        "pill": "Entlastung × Timing",
        "base": "Wenn {warning_label} → soll CORE früh entlasten?",
        "depends": ["warning_load", "recovery_state", "strength_trend", "proximity_to_failure"],
        "followup_dimensions": ["persistence", "severity", "deload_threshold"],
    },
    {
        "id": "single_warning_not_deload_yet",
        "topic": "deload_philosophy",
        "kind": "control",
        "family": "deload_philosophy",
        "dimension": "deload_threshold",
        "pill": "Entlastung × Schwelle",
        "base": "Wenn nur ein Warnsignal da ist → soll CORE noch nicht entlasten?",
        "depends": ["warning_load", "recovery_state", "strength_trend"],
        "followup_dimensions": ["persistence", "severity", "signal_priority"],
    },
    {
        "id": "consistency_prefers_repeatable_units",
        "topic": "consistency_frequency",
        "kind": "principle",
        "family": "consistency_frequency",
        "dimension": "frequency_rule",
        "pill": "Konstanz × Frequenz",
        "base": "Wenn die Konstanz leidet → soll CORE lieber einfachere, wiederholbare Einheiten bevorzugen?",
        "depends": ["gym_frequency", "session_gap", "recovery_state"],
        "followup_dimensions": ["frequency_rule", "severity", "persistence"],
    },
    {
        "id": "rare_training_build_stability",
        "topic": "consistency_frequency",
        "kind": "edge",
        "family": "consistency_frequency",
        "dimension": "frequency_rule",
        "pill": "Konstanz × Stabilität",
        "base": "Wenn die Frequenz {frequency_label} → soll CORE lieber Stabilität aufbauen statt maximale Intensität?",
        "depends": ["gym_frequency", "recovery_state"],
        "followup_dimensions": ["frequency_rule", "severity", "persistence"],
    },
    {
        "id": "high_run_load_lower_conservative",
        "topic": "interference_run_gym",
        "kind": "principle",
        "family": "interference_run_gym",
        "dimension": "interference_horizon",
        "pill": "Lauf × Kraft",
        "base": "Wenn die Laufbelastung {run_load_label} → soll CORE Lower-Training konservativer steuern?",
        "depends": ["run_load", "recovery_state", "strength_trend"],
        "followup_dimensions": ["interference_horizon", "severity", "signal_priority"],
    },
    {
        "id": "high_gym_load_runs_conservative",
        "topic": "interference_run_gym",
        "kind": "principle",
        "family": "interference_run_gym",
        "dimension": "interference_horizon",
        "pill": "Kraft × Lauf",
        "base": "Wenn die Gym-Belastung {gym_load_label} → soll CORE Laufreize konservativer steuern?",
        "depends": ["gym_frequency", "proximity_to_failure", "recovery_state"],
        "followup_dimensions": ["interference_horizon", "severity", "frequency_rule"],
    },
    {
        "id": "nutrition_inconsistent_hold_training",
        "topic": "nutrition_as_limiter",
        "kind": "principle",
        "family": "nutrition_as_limiter",
        "dimension": "nutrition_trigger",
        "pill": "Ernährung × Limiter",
        "base": "Wenn die Ernährung {nutrition_label} → soll CORE Training eher stabil halten statt pushen?",
        "depends": ["nutrition_adherence", "weight_trend", "recovery_state"],
        "followup_dimensions": ["nutrition_trigger", "severity", "signal_priority"],
    },
    {
        "id": "nutrition_inconsistent_simplify",
        "topic": "nutrition_as_limiter",
        "kind": "control",
        "family": "nutrition_as_limiter",
        "dimension": "nutrition_trigger",
        "pill": "Ernährung × Vereinfachung",
        "base": "Wenn die Ernährung {nutrition_label} → soll CORE Vereinfachung priorisieren?",
        "depends": ["nutrition_adherence", "weight_trend"],
        "followup_dimensions": ["nutrition_trigger", "severity", "persistence"],
    },
    {
        "id": "good_recovery_down_trend_take_performance_seriously",
        "topic": "signal_weighting",
        "kind": "principle",
        "family": "signal_weighting",
        "dimension": "signal_priority",
        "pill": "Signale × Gewichtung",
        "base": "Wenn die Recovery {recovery_label}, der Leistungstrend aber {strength_label} → soll CORE Leistung ernster nehmen als Recovery?",
        "depends": ["recovery_state", "strength_trend"],
        "followup_dimensions": ["signal_priority", "persistence", "severity"],
    },
    {
        "id": "bad_recovery_ok_performance_take_recovery_seriously",
        "topic": "signal_weighting",
        "kind": "principle",
        "family": "signal_weighting",
        "dimension": "signal_priority",
        "pill": "Recovery × Gewichtung",
        "base": "Wenn die Recovery {recovery_label}, die Leistung aber {performance_label} → soll CORE Recovery ernster nehmen als Leistung?",
        "depends": ["recovery_state", "strength_trend", "proximity_to_failure"],
        "followup_dimensions": ["signal_priority", "persistence", "severity"],
    },
]

TEMPLATES.extend([
    {
        "id": "plan_first_low_recovery_hold_line",
        "topic": "plan_first",
        "kind": "principle",
        "family": "plan_first",
        "dimension": "discipline",
        "pill": "Plan zuerst",
        "base": "Wenn die Erholung {recovery_label} und Warnsignale da sind → soll CORE lieber beim Plan bleiben?",
        "depends": ["recovery_state", "warning_load", "strength_trend"],
        "followup_dimensions": ["severity", "persistence", "signal_priority"],
    },
    {
        "id": "rpe_honesty_high_rpe_read_conservative",
        "topic": "rpe_honesty",
        "kind": "principle",
        "family": "rpe_honesty",
        "dimension": "honesty",
        "pill": "RPE-Ehrlichkeit",
        "base": "Wenn die letzten Top-Sätze {rpe_label} → soll CORE deine RPE vorsichtiger lesen?",
        "depends": ["proximity_to_failure", "strength_trend", "recovery_state"],
        "followup_dimensions": ["rpe_guard", "severity", "persistence"],
    },
    {
        "id": "logging_quality_warning_interpret_careful",
        "topic": "logging_quality",
        "kind": "principle",
        "family": "logging_quality",
        "dimension": "honesty",
        "pill": "Logging",
        "base": "Wenn Warnsignale da sind und die Leistung {strength_label} → soll CORE deine Logs vorsichtiger lesen?",
        "depends": ["warning_load", "strength_trend", "recovery_state"],
        "followup_dimensions": ["signal_priority", "severity", "persistence"],
    },
    {
        "id": "nutrition_discipline_low_adherence_simplify",
        "topic": "nutrition_discipline",
        "kind": "principle",
        "family": "nutrition_discipline",
        "dimension": "discipline",
        "pill": "Essen",
        "base": "Wenn das Essen {nutrition_label} → soll CORE einfacher steuern?",
        "depends": ["nutrition_adherence", "weight_trend", "recovery_state"],
        "followup_dimensions": ["nutrition_trigger", "severity", "persistence"],
    },
    {
        "id": "regeneration_honesty_low_recovery_protect_more",
        "topic": "regeneration_honesty",
        "kind": "principle",
        "family": "regeneration_honesty",
        "dimension": "recovery",
        "pill": "Regeneration",
        "base": "Wenn die Erholung {recovery_label} → soll CORE dich stärker bremsen?",
        "depends": ["recovery_state", "warning_load", "proximity_to_failure"],
        "followup_dimensions": ["severity", "persistence", "signal_priority"],
    },
    {
        "id": "run_quality_high_load_shift_quality",
        "topic": "run_quality",
        "kind": "principle",
        "family": "run_quality",
        "dimension": "quality",
        "pill": "Lauf-Qualität",
        "base": "Wenn die Laufbelastung {run_load_label} und die Erholung {recovery_label} → soll CORE Qualität lieber verschieben?",
        "depends": ["run_load", "recovery_state", "strength_trend"],
        "followup_dimensions": ["interference_horizon", "severity", "persistence"],
    },
    {
        "id": "regression_response_flat_trend_switch_approach",
        "topic": "regression_response",
        "kind": "principle",
        "family": "regression_response",
        "dimension": "response",
        "pill": "Regression",
        "base": "Wenn der Leistungstrend {strength_label} → soll CORE lieber den Ansatz ändern?",
        "depends": ["strength_trend", "warning_load", "recovery_state"],
        "followup_dimensions": ["progression_rule", "persistence", "severity"],
    },
    {
        "id": "plan_first_rest_really_rest",
        "topic": "plan_first",
        "kind": "principle",
        "family": "plan_first",
        "dimension": "discipline",
        "pill": "Plan zuerst",
        "base": "Wenn der Plan heute Rest sagt: lässt du es dann wirklich sein?",
        "depends": ["recovery_state", "warning_load"],
        "fixed_question": True,
    },
    {
        "id": "plan_first_override_only_for_real_signal",
        "topic": "plan_first",
        "kind": "principle",
        "family": "plan_first",
        "dimension": "discipline",
        "pill": "Plan zuerst",
        "base": "Änderst du den Plan nur dann, wenn es dafür einen echten Grund gibt?",
        "depends": ["warning_load", "strength_trend"],
        "fixed_question": True,
    },
    {
        "id": "plan_first_follow_plan",
        "topic": "plan_first",
        "kind": "principle",
        "family": "plan_first",
        "dimension": "discipline",
        "pill": "Plan zuerst",
        "base": "Wenn du den Plan änderst: gibt es dafür einen echten Grund?",
        "depends": ["strength_trend", "recovery_state"],
        "fixed_question": True,
    },
    {
        "id": "rpe_honesty_brutal_honest",
        "topic": "rpe_honesty",
        "kind": "principle",
        "family": "rpe_honesty",
        "dimension": "honesty",
        "pill": "RPE-Ehrlichkeit",
        "base": "Bist du bei RPE wirklich ehrlich?",
        "depends": ["proximity_to_failure", "strength_trend"],
        "fixed_question": True,
    },
    {
        "id": "rpe_honesty_admit_cap_needed",
        "topic": "rpe_honesty",
        "kind": "principle",
        "family": "rpe_honesty",
        "dimension": "honesty",
        "pill": "RPE-Ehrlichkeit",
        "base": "Merkst du ehrlich, wenn du heute ein RPE-Cap gebraucht hättest?",
        "depends": ["proximity_to_failure", "warning_load"],
        "fixed_question": True,
    },
    {
        "id": "rpe_honesty_logged_lighter",
        "topic": "logging_quality",
        "kind": "principle",
        "family": "logging_quality",
        "dimension": "honesty",
        "pill": "Logging",
        "base": "Hast du heute leichter geloggt, als es war?",
        "depends": ["proximity_to_failure", "recovery_state"],
        "fixed_question": True,
    },
    {
        "id": "logging_quality_pretty_sets",
        "topic": "logging_quality",
        "kind": "principle",
        "family": "logging_quality",
        "dimension": "honesty",
        "pill": "Logging",
        "base": "Loggst du Sätze manchmal schöner, als sie waren?",
        "depends": ["strength_trend", "warning_load"],
        "fixed_question": True,
    },
    {
        "id": "logging_quality_clean_even_when_bad",
        "topic": "logging_quality",
        "kind": "principle",
        "family": "logging_quality",
        "dimension": "honesty",
        "pill": "Logging",
        "base": "Lässt du bei schlechten Einheiten wichtige Details weg?",
        "depends": ["strength_trend", "recovery_state"],
        "fixed_question": True,
    },
    {
        "id": "nutrition_discipline_stress_undereat",
        "topic": "nutrition_discipline",
        "kind": "principle",
        "family": "nutrition_discipline",
        "dimension": "discipline",
        "pill": "Essen",
        "base": "Isst du an stressigen Tagen eher zu wenig als zu viel?",
        "depends": ["nutrition_adherence", "weight_trend"],
        "fixed_question": True,
    },
    {
        "id": "nutrition_discipline_good_eater",
        "topic": "nutrition_discipline",
        "kind": "principle",
        "family": "nutrition_discipline",
        "dimension": "discipline",
        "pill": "Essen",
        "base": "Lässt du Essen an stressigen Tagen schleifen?",
        "depends": ["nutrition_adherence", "weight_trend"],
        "fixed_question": True,
    },
    {
        "id": "nutrition_discipline_keep_simple_on_rough_days",
        "topic": "nutrition_discipline",
        "kind": "principle",
        "family": "nutrition_discipline",
        "dimension": "discipline",
        "pill": "Essen",
        "base": "Verlierst du an chaotischen Tagen den Überblick über dein Essen?",
        "depends": ["nutrition_adherence", "recovery_state"],
        "fixed_question": True,
    },
    {
        "id": "regeneration_honesty_bad_sleep_max_hard",
        "topic": "regeneration_honesty",
        "kind": "principle",
        "family": "regeneration_honesty",
        "dimension": "recovery",
        "pill": "Regeneration",
        "base": "Wenn du schlecht geschlafen hast: trainierst du trotzdem manchmal maximal hart?",
        "depends": ["recovery_state", "warning_load"],
        "fixed_question": True,
    },
    {
        "id": "regeneration_honesty_pause_shortcuts",
        "topic": "regeneration_honesty",
        "kind": "principle",
        "family": "regeneration_honesty",
        "dimension": "recovery",
        "pill": "Regeneration",
        "base": "Verkürzt du Pausen heimlich, um schneller fertig zu sein?",
        "depends": ["recovery_state", "proximity_to_failure"],
        "fixed_question": True,
    },
    {
        "id": "regeneration_honesty_respect_brake",
        "topic": "regeneration_honesty",
        "kind": "principle",
        "family": "regeneration_honesty",
        "dimension": "recovery",
        "pill": "Regeneration",
        "base": "Ignorierst du CORE, wenn es dich bremsen will?",
        "depends": ["recovery_state", "warning_load"],
        "fixed_question": True,
    },
    {
        "id": "run_quality_intervals_anyway",
        "topic": "run_quality",
        "kind": "principle",
        "family": "run_quality",
        "dimension": "quality",
        "pill": "Lauf-Qualität",
        "base": "Machst du Intervalle manchmal, obwohl du weißt, dass es an dem Tag Quatsch ist?",
        "depends": ["run_load", "recovery_state"],
        "fixed_question": True,
    },
    {
        "id": "run_quality_quality_over_ego",
        "topic": "run_quality",
        "kind": "principle",
        "family": "run_quality",
        "dimension": "quality",
        "pill": "Lauf-Qualität",
        "base": "Machst du Qualität zu hart, nur weil locker dir nicht reicht?",
        "depends": ["run_load", "strength_trend"],
        "fixed_question": True,
    },
    {
        "id": "run_quality_z2_when_not_ready",
        "topic": "run_quality",
        "kind": "principle",
        "family": "run_quality",
        "dimension": "quality",
        "pill": "Lauf-Qualität",
        "base": "Versuchst du aus Z2 doch noch Qualität zu machen?",
        "depends": ["run_load", "recovery_state"],
        "fixed_question": True,
    },
    {
        "id": "regression_response_hide_with_tired",
        "topic": "regression_response",
        "kind": "principle",
        "family": "regression_response",
        "dimension": "response",
        "pill": "Regression",
        "base": "Schiebst du Stillstand manchmal nur auf Müdigkeit?",
        "depends": ["strength_trend", "warning_load"],
        "fixed_question": True,
    },
    {
        "id": "regression_response_face_flat",
        "topic": "regression_response",
        "kind": "principle",
        "family": "regression_response",
        "dimension": "response",
        "pill": "Regression",
        "base": "Drückst du bei einer festgefahrenen Übung einfach weiter?",
        "depends": ["strength_trend", "recovery_state"],
        "fixed_question": True,
    },
    {
        "id": "regression_response_accept_change",
        "topic": "regression_response",
        "kind": "principle",
        "family": "regression_response",
        "dimension": "response",
        "pill": "Regression",
        "base": "Hältst du an einem Ansatz fest, obwohl nichts vorangeht?",
        "depends": ["strength_trend", "warning_load"],
        "fixed_question": True,
    },
])

TEMPLATES.extend(build_fixed_question_templates())

_SCHEMA_READY_FOR_DB: str | None = None


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _ts() -> str:
    return _utc_now().strftime("%Y-%m-%dT%H:%M:%SZ")


def ensure_core_training_deck_schema() -> None:
    global _SCHEMA_READY_FOR_DB
    current_db = str(getattr(db_conn, "CORE_DB", "") or "")
    if _SCHEMA_READY_FOR_DB == current_db:
        return
    ensure_core_training_model_schema()
    conn = get_core_db()
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS core_training_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                last_served_at TEXT,
                model_version TEXT,
                rng_seed INTEGER,
                last_signature TEXT,
                last_card_json TEXT,
                last_answer_json TEXT,
                rotation_index INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        ensure_core_question_policy_schema(conn)
        cols = {str(row["name"]) for row in conn.execute("PRAGMA table_info(core_training_state)").fetchall()}
        if "rotation_index" not in cols:
            conn.execute("ALTER TABLE core_training_state ADD COLUMN rotation_index INTEGER NOT NULL DEFAULT 0")
        if "last_answer_json" not in cols:
            conn.execute("ALTER TABLE core_training_state ADD COLUMN last_answer_json TEXT")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS core_training_seen (
                signature TEXT PRIMARY KEY,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                times_seen INTEGER NOT NULL DEFAULT 0,
                last_answer TEXT,
                last_latency_ms INTEGER,
                card_text_norm TEXT,
                card_json TEXT,
                card_topic TEXT,
                card_kind TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS core_training_deleted_cards (
                card_text_norm TEXT PRIMARY KEY,
                topic TEXT,
                deleted_at TEXT NOT NULL
            )
            """
        )
        seen_cols = {str(row["name"]) for row in conn.execute("PRAGMA table_info(core_training_seen)").fetchall()}
        if "card_text_norm" not in seen_cols:
            conn.execute("ALTER TABLE core_training_seen ADD COLUMN card_text_norm TEXT")
        if "card_json" not in seen_cols:
            conn.execute("ALTER TABLE core_training_seen ADD COLUMN card_json TEXT")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS core_training_answers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                t TEXT NOT NULL,
                signature TEXT NOT NULL,
                card_id TEXT NOT NULL,
                topic TEXT NOT NULL,
                kind TEXT NOT NULL,
                card_text_norm TEXT,
                answer TEXT NOT NULL,
                prediction TEXT NOT NULL,
                confidence REAL NOT NULL,
                features_json TEXT NOT NULL,
                client_ms INTEGER
            )
            """
        )
        answer_cols = {str(row["name"]) for row in conn.execute("PRAGMA table_info(core_training_answers)").fetchall()}
        if "card_text_norm" not in answer_cols:
            conn.execute("ALTER TABLE core_training_answers ADD COLUMN card_text_norm TEXT")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_core_training_seen_last_seen ON core_training_seen(last_seen_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_core_training_answers_topic_t ON core_training_answers(topic, t)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_core_training_answers_card_text_norm ON core_training_answers(card_text_norm)")
        conn.execute(
            """
            UPDATE core_training_answers
            SET card_text_norm = (
                SELECT s.card_text_norm
                FROM core_training_seen s
                WHERE s.signature = core_training_answers.signature
            )
            WHERE (card_text_norm IS NULL OR TRIM(card_text_norm) = '')
              AND EXISTS (
                SELECT 1
                FROM core_training_seen s
                WHERE s.signature = core_training_answers.signature
                  AND s.card_text_norm IS NOT NULL
                  AND TRIM(s.card_text_norm) != ''
              )
            """
        )
        mismatch_rows = conn.execute(
            """
            SELECT a.id, a.kind, a.topic, s.card_topic, s.card_kind, s.card_json
            FROM core_training_answers a
            JOIN core_training_seen s ON s.signature = a.signature
            WHERE a.topic != s.card_topic
            """
        ).fetchall()
        for row in mismatch_rows:
            next_topic = str(row["card_topic"] or row["topic"] or "").strip()
            if not next_topic:
                continue
            next_kind = str(row["kind"] or "")
            card_json = str(row["card_json"] or "").strip()
            if card_json:
                try:
                    parsed = json.loads(card_json)
                except Exception:
                    parsed = {}
                meta = parsed.get("meta") if isinstance(parsed, dict) else {}
                if isinstance(meta, dict):
                    kind_base = str(meta.get("kind") or row["card_kind"] or "principle").strip() or "principle"
                    family = str(meta.get("family") or next_topic).strip() or next_topic
                    dimension = str(meta.get("dimension") or family).strip() or family
                    next_kind = f"{kind_base}:{family}:{dimension}"
            conn.execute(
                "UPDATE core_training_answers SET topic=?, kind=? WHERE id=?",
                (next_topic, next_kind, int(row["id"])),
            )
        now = _ts()
        conn.execute(
            """
            INSERT INTO core_training_state (id, created_at, updated_at, model_version, rng_seed)
            VALUES (1, ?, ?, 'deck-v1', 17)
            ON CONFLICT(id) DO UPDATE SET updated_at=excluded.updated_at
            """,
            (now, now),
        )
        conn.commit()
        _SCHEMA_READY_FOR_DB = current_db
    finally:
        conn.close()


def make_signature(template_id: str, topic: str, params: dict[str, Any], features: dict[str, Any]) -> str:
    payload = {
        "template_id": template_id,
        "topic": topic,
        "params": {k: params[k] for k in sorted(params)},
        "features": {k: features[k] for k in sorted(features)},
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _normalize_prompt(text: str) -> str:
    compact = " ".join(str(text or "").strip().lower().split())
    return re.sub(r"[^a-z0-9äöüß\s]+", "", compact).strip()


def _prompt_tokens(text: str) -> set[str]:
    stop = {
        "und", "oder", "der", "die", "das", "den", "dem", "des", "ein", "eine", "einer", "einem", "zu",
        "mit", "für", "von", "bei", "im", "in", "am", "an", "auf", "wenn", "soll", "core", "heute",
    }
    return {
        tok for tok in re.findall(r"[a-z0-9äöüß]+", _normalize_prompt(text))
        if len(tok) >= 3 and tok not in stop
    }


def _is_text_near_duplicate(text: str, recent_norms: set[str], recent_token_sets: list[set[str]], threshold: float = 0.78) -> bool:
    norm = _normalize_prompt(text)
    if not norm:
        return True
    if norm in recent_norms:
        return True
    toks = _prompt_tokens(norm)
    if not toks:
        return True
    for other in recent_token_sets:
        if not other:
            continue
        inter = len(toks & other)
        if inter == 0:
            continue
        sim = inter / max(1, len(toks | other))
        if sim >= threshold:
            return True
    return False


def _clamp_prob(value: float) -> float:
    return max(0.02, min(0.98, float(value)))


def _question_text_bias(text_norm: str) -> float:
    if not text_norm:
        return 0.0
    yes_markers = {
        "soll core", "priorisieren", "schuetzen", "schützen", "konservativ", "stabilisieren",
        "idempotent", "sichtbar", "klar", "explain", "begrenzen", "vereinfachen", "frueh", "früh",
    }
    no_markers = {
        "ignorierst du", "drueckst du", "drückst du", "planlos", "eskalieren", "spam",
        "zu wenig", "zu spaet", "zu spät", "ausfallen", "skip", "chaotisch", "wegdruecken", "wegdrücken",
    }
    bias = 0.0
    for marker in yes_markers:
        if marker in text_norm:
            bias += 0.018
    for marker in no_markers:
        if marker in text_norm:
            bias -= 0.02
    if text_norm.startswith("soll core"):
        bias += 0.04
    if text_norm.startswith("machst du") or text_norm.startswith("ignorierst du"):
        bias -= 0.04
    # Small stable, question-specific tiebreaker to avoid topic-flat priors.
    digest = hashlib.sha1(text_norm.encode("utf-8")).hexdigest()
    stable = ((int(digest[:8], 16) % 1000) / 1000.0 - 0.5) * 0.08
    return max(-0.16, min(0.16, bias + stable))


def _question_similarity_prior(conn, text_norm: str, limit: int = 260) -> dict[str, Any]:
    q_tokens = _prompt_tokens(text_norm)
    if not q_tokens:
        return {"ok": False}
    rows = conn.execute(
        """
        SELECT answer, card_text_norm
        FROM core_training_answers
        WHERE card_text_norm IS NOT NULL AND TRIM(card_text_norm) != ''
        ORDER BY id DESC
        LIMIT ?
        """,
        (int(limit),),
    ).fetchall()
    if not rows:
        return {"ok": False}
    cache: dict[str, set[str]] = {}
    yes_weight = 0.0
    no_weight = 0.0
    hits = 0
    for idx, row in enumerate(rows):
        other_norm = str(row["card_text_norm"] or "").strip()
        if not other_norm:
            continue
        other_tokens = cache.get(other_norm)
        if other_tokens is None:
            other_tokens = _prompt_tokens(other_norm)
            cache[other_norm] = other_tokens
        if not other_tokens:
            continue
        inter = len(q_tokens & other_tokens)
        if inter == 0:
            continue
        sim = inter / max(1, len(q_tokens | other_tokens))
        if sim < 0.34:
            continue
        recency = max(0.35, 1.0 - idx * 0.004)
        weight = (sim ** 1.8) * recency
        if weight <= 0.0:
            continue
        hits += 1
        if str(row["answer"] or "").strip().lower() == "yes":
            yes_weight += weight
        else:
            no_weight += weight
    total = yes_weight + no_weight
    if total < 0.45:
        return {"ok": False}
    return {
        "ok": True,
        "probability": yes_weight / max(1e-6, total),
        "weight": total,
        "hits": hits,
    }


def _feature_label(bucket: str, mapping: dict[str, str]) -> str:
    return mapping.get(bucket, mapping.get("missing", "unklar"))


def _template_text(template: dict[str, Any], features: dict[str, Any]) -> str:
    if template.get("fixed_question"):
        return str(template.get("base") or "").strip()
    params = template.get("_runtime_params") or {}
    labels = {
        "recovery_label": _feature_label(features.get("recovery_state", "missing"), {
            "low": "schlecht ist",
            "ok": "okay ist",
            "high": "gut ist",
            "missing": "unklar ist",
        }),
        "strength_label": _feature_label(features.get("strength_trend", "missing"), {
            "down": "rückläufig ist",
            "flat": "stabil bleibt",
            "up": "ansteigt",
            "missing": "unklar ist",
        }),
        "run_label": _feature_label(features.get("run_consistency", "missing"), {
            "low": "niedrig ist",
            "ok": "okay ist",
            "high": "hoch ist",
            "missing": "unklar ist",
        }),
        "nutrition_label": _feature_label(features.get("nutrition_adherence", "missing"), {
            "low": "mehrere Tage inkonsistent ist",
            "ok": "okay ist",
            "high": "stabil ist",
            "missing": "unklar ist",
        }),
        "weight_label": _feature_label(features.get("weight_trend", "missing"), {
            "down": "klar fällt",
            "flat": "stabil bleibt",
            "up": "klar steigt",
            "missing": "unklar ist",
        }),
        "gap_label": _feature_label(features.get("session_gap", "missing"), {
            "short": "der letzte Gym-Tag noch nicht lange her ist",
            "normal": "der letzte Gym-Abstand normal ist",
            "long": "der letzte Gym-Tag schon länger her ist",
            "missing": "der letzte Gym-Abstand unklar ist",
        }),
        "rpe_label": _feature_label(features.get("proximity_to_failure", "missing"), {
            "low": "weit vom Limit weg waren",
            "ok": "moderat waren",
            "high": "sehr nah am Limit waren",
            "missing": "unklar waren",
        }),
        "frequency_label": _feature_label(features.get("gym_frequency", "missing"), {
            "low": "niedrig ist",
            "ok": "okay ist",
            "high": "hoch ist",
            "missing": "unklar ist",
        }),
        "run_load_label": _feature_label(features.get("run_load", "missing"), {
            "low": "niedrig ist",
            "ok": "okay ist",
            "high": "hoch ist",
            "missing": "unklar ist",
        }),
        "gym_load_label": _feature_label(features.get("gym_frequency", "missing"), {
            "low": "niedrig ist",
            "ok": "okay ist",
            "high": "hoch ist",
            "missing": "unklar ist",
        }),
        "warning_label": _feature_label(features.get("warning_load", "missing"), {
            "low": "nur ein Warnsignal da ist",
            "ok": "mehrere Warnsignale gleichzeitig auftreten",
            "high": "mehrere Warnsignale gleichzeitig auftreten",
            "missing": "die Warnlage unklar ist",
        }),
        "performance_label": _feature_label(features.get("strength_trend", "missing"), {
            "down": "nachgibt",
            "flat": "okay wirkt",
            "up": "okay wirkt",
            "missing": "unklar ist",
        }),
        "window_label": f"über {int(params.get('window', 14))} Tage",
        "persistence_label": {
            1: "einmal",
            2: "zwei Einheiten hintereinander",
            3: "drei Einheiten hintereinander",
        }.get(int(params.get("persistence", 1) or 1), "mehrfach"),
    }
    return template["base"].format(**labels).strip()


def _candidate_params(features: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {"window": 7, "persistence": 1, "severity": "low", "knob": "volume", "rpe_bucket": "moderate"},
        {"window": 14, "persistence": 1, "severity": "ok", "knob": "intensity", "rpe_bucket": "moderate"},
        {"window": 14, "persistence": 2, "severity": "ok", "knob": "volume", "rpe_bucket": "high"},
        {"window": 28, "persistence": 2, "severity": "high", "knob": "intensity", "rpe_bucket": "high"},
        {"window": 28, "persistence": 3, "severity": "high", "knob": "both", "rpe_bucket": "high"},
    ]


def _topic_metrics(conn) -> dict[str, dict[str, float]]:
    rows = conn.execute(
        """
        SELECT topic,
               COUNT(*) AS total,
               SUM(CASE WHEN answer != prediction THEN 1 ELSE 0 END) AS disagree
        FROM core_training_answers
        GROUP BY topic
        """
    ).fetchall()
    metrics: dict[str, dict[str, float]] = {}
    for row in rows:
        total = int(row["total"] or 0)
        disagree = int(row["disagree"] or 0)
        metrics[str(row["topic"] or "")] = {
            "total": float(total),
            "disagree_rate": (disagree / total) if total else 0.0,
        }
    return metrics


def _novelty_score(topic: str, features: dict[str, Any], topic_metrics: dict[str, dict[str, float]]) -> float:
    count = int(topic_metrics.get(topic, {}).get("total", 0.0) or 0)
    missing_bonus = sum(1 for value in features.values() if value == "missing") * 0.03
    return max(0.0, 1.2 - min(count, 12) * 0.08) + missing_bonus


def _topic_need_bonus(topic: str, topic_metrics: dict[str, dict[str, float]]) -> float:
    topic_data = topic_metrics.get(topic) or {}
    total = int(topic_data.get("total", 0.0) or 0)
    if not total:
        return 0.45
    rate = float(topic_data.get("disagree_rate", 0.0) or 0.0)
    return min(0.55, rate * 0.6 + max(0, 6 - total) * 0.04)


def _recent_penalty(signature: str, recent_signatures: list[str]) -> float:
    if signature in recent_signatures:
        return 2.5
    return 0.0




def _load_seen_meta(conn) -> dict[str, dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT signature, times_seen, last_seen_at
        FROM core_training_seen
        ORDER BY last_seen_at DESC, times_seen DESC, signature ASC
        """
    ).fetchall()
    meta: dict[str, dict[str, Any]] = {}
    for rank, row in enumerate(rows):
        meta[str(row["signature"])] = {
            "times_seen": int(row["times_seen"] or 0),
            "last_seen_at": str(row["last_seen_at"] or ""),
            "recent_rank": rank,
        }
    return meta


def _load_recent_seen_signatures(conn, limit: int = 12) -> list[str]:
    rows = conn.execute(
        """
        SELECT signature
        FROM core_training_seen
        ORDER BY last_seen_at DESC, times_seen DESC, signature ASC
        LIMIT ?
        """,
        (int(limit),),
    ).fetchall()
    return [str(row["signature"] or "") for row in rows if str(row["signature"] or "").strip()]


def _load_recent_seen_topics(conn, limit: int = 16) -> list[str]:
    rows = conn.execute(
        """
        SELECT card_topic
        FROM core_training_seen
        WHERE card_topic IS NOT NULL AND TRIM(card_topic) != ''
        ORDER BY last_seen_at DESC, times_seen DESC, signature ASC
        LIMIT ?
        """,
        (int(limit),),
    ).fetchall()
    return [str(row["card_topic"] or "").strip() for row in rows if str(row["card_topic"] or "").strip()]


def _load_recent_seen_prompt_norms(conn, limit: int = 18) -> set[str]:
    rows = conn.execute(
        """
        SELECT card_text_norm
        FROM core_training_seen
        WHERE card_text_norm IS NOT NULL AND TRIM(card_text_norm) != ''
        ORDER BY last_seen_at DESC, times_seen DESC, signature ASC
        LIMIT ?
        """,
        (int(limit),),
    ).fetchall()
    return {str(row["card_text_norm"] or "").strip() for row in rows if str(row["card_text_norm"] or "").strip()}


def _load_recent_seen_prompt_texts(conn, limit: int = 36) -> list[str]:
    rows = conn.execute(
        """
        SELECT card_json
        FROM core_training_seen
        WHERE card_json IS NOT NULL AND TRIM(card_json) != ''
        ORDER BY last_seen_at DESC, times_seen DESC, signature ASC
        LIMIT ?
        """,
        (int(limit),),
    ).fetchall()
    texts: list[str] = []
    for row in rows:
        raw = str(row["card_json"] or "").strip()
        if not raw:
            continue
        try:
            payload = json.loads(raw)
        except Exception:
            payload = {}
        text = str(payload.get("text") or "").strip()
        if text:
            texts.append(text)
    return texts


def _load_deleted_prompt_norms(conn) -> set[str]:
    rows = conn.execute("SELECT card_text_norm FROM core_training_deleted_cards").fetchall()
    return {str(row["card_text_norm"] or "").strip() for row in rows if str(row["card_text_norm"] or "").strip()}


def _load_state(conn) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM core_training_state WHERE id=1").fetchone()
    return dict(row) if row else {}


def _seeded_jitter(signature: str, rng_seed: int) -> float:
    digest = hashlib.sha1(f"{signature}:{rng_seed}".encode("utf-8")).hexdigest()
    value = int(digest[:8], 16) / 0xFFFFFFFF
    return (value - 0.5) * 0.16


def _seen_penalty(signature: str, seen_meta: dict[str, dict[str, Any]], recent_signatures: list[str]) -> float:
    if signature in recent_signatures:
        return 3.0
    meta = seen_meta.get(signature)
    if not meta:
        return 0.0
    recent_rank = int(meta.get("recent_rank", 999))
    times_seen = int(meta.get("times_seen", 0))
    penalty = min(1.5, times_seen * 0.12)
    if recent_rank < 3:
        penalty += 1.35
    elif recent_rank < 8:
        penalty += 0.7
    elif recent_rank < 16:
        penalty += 0.25
    return penalty


def _save_state(conn, card: dict[str, Any]) -> None:
    now = _ts()
    conn.execute(
        """
        UPDATE core_training_state
        SET updated_at=?, last_served_at=?, last_signature=?, last_card_json=?
        WHERE id=1
        """,
        (now, now, card["signature"], json.dumps(card, ensure_ascii=False)),
    )


def _upsert_seen(conn, card: dict[str, Any]) -> None:
    now = _ts()
    card_text_norm = _normalize_prompt(str(card.get("text") or ""))
    card_json = json.dumps(card, ensure_ascii=False)
    conn.execute(
        """
        INSERT INTO core_training_seen (signature, first_seen_at, last_seen_at, times_seen, card_text_norm, card_json, card_topic, card_kind)
        VALUES (?, ?, ?, 1, ?, ?, ?, ?)
        ON CONFLICT(signature) DO UPDATE SET
            last_seen_at=excluded.last_seen_at,
            times_seen=core_training_seen.times_seen + 1,
            card_text_norm=excluded.card_text_norm,
            card_json=excluded.card_json,
            card_topic=excluded.card_topic,
            card_kind=excluded.card_kind
        """,
        (card["signature"], now, now, card_text_norm, card_json, card["meta"]["topic"], card["meta"]["kind"]),
    )


def _load_topic_prediction_cache(features: dict[str, Any]) -> dict[str, dict[str, Any]]:
    topics = {str(template.get("topic") or "consistency_frequency") for template in TEMPLATES}
    return {topic: predict_topic(topic, features) for topic in topics}


def _build_card(
    template: dict[str, Any],
    params: dict[str, Any],
    feature_payload: dict[str, Any],
    topic_predictions: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    features = feature_payload["features"]
    prediction = topic_predictions.get(template["topic"]) or predict_topic(template["topic"], features)
    signature = make_signature(template["id"], template["topic"], params, {key: features.get(key) for key in template["depends"]})
    context = {
        "kind": template["kind"],
        "topic": template["topic"],
        "family": template.get("family", template["topic"]),
        "dimension": template.get("dimension", template.get("family", template["topic"])),
        "pill": template["pill"],
        "feature_digest": {key: features.get(key, "missing") for key in template["depends"]},
        "summaries": {
            "recovery": feature_payload["summaries"]["recovery"]["summary"],
            "strength": feature_payload["summaries"]["strength"]["summary"],
            "runs": feature_payload["summaries"]["runs"]["summary"],
            "nutrition": feature_payload["summaries"]["nutrition"]["summary"],
            "weight": feature_payload["summaries"]["weight"]["summary"],
            "gap": feature_payload["summaries"]["gap"]["summary"],
            "failure": feature_payload["summaries"].get("failure", {}).get("summary", ""),
            "warnings": feature_payload["summaries"].get("warnings", {}).get("summary", ""),
        },
        "params": params,
        "template_id": template["id"],
        "followup_dimensions": list(template.get("followup_dimensions") or []),
    }
    runtime_template = dict(template)
    runtime_template["_runtime_params"] = dict(params)
    return {
        "id": uuid.uuid4().hex,
        "signature": signature,
        "text": _template_text(runtime_template, features),
        "meta": context,
        "prediction": {
            "answer": prediction["answer"],
            "confidence": prediction["confidence"],
            "bar_position": prediction["bar_position"],
            "scope": "topic_bootstrap",
            "basis": "topic_model",
        },
    }


def _predict_card_from_history(conn, card: dict[str, Any], features: dict[str, Any]) -> dict[str, Any]:
    topic = str(card.get("meta", {}).get("topic") or "consistency_frequency")
    text_norm = _normalize_prompt(str(card.get("text") or ""))
    kind_base = str(card.get("meta", {}).get("kind") or "principle")
    family = str(card.get("meta", {}).get("family") or topic)
    dimension = str(card.get("meta", {}).get("dimension") or family)
    kind_key = f"{kind_base}:{family}:{dimension}"
    topic_prediction = predict_topic(topic, features)
    topic_prob = _clamp_prob(float(topic_prediction.get("probability") or 0.5))
    text_bias = _question_text_bias(text_norm)
    text_prob = _clamp_prob(0.5 + text_bias)
    similar = _question_similarity_prior(conn, text_norm)
    if bool(similar.get("ok")):
        sim_prob = _clamp_prob(float(similar.get("probability") or 0.5))
        sim_weight = float(similar.get("weight") or 0.0)
        sim_factor = min(2.8, 1.0 + sim_weight)
        question_prior_prob = _clamp_prob((text_prob * 1.4 + sim_prob * sim_factor + topic_prob * 0.55) / (1.95 + sim_factor))
        question_prior_weight = 1.9 + min(2.2, sim_weight * 0.9)
        prior_basis = "similar_questions"
        prior_scope = "question_similar"
    else:
        question_prior_prob = _clamp_prob((text_prob * 1.8 + topic_prob * 0.55) / 2.35)
        question_prior_weight = 1.35
        prior_basis = "question_text"
        prior_scope = "question_text"
    exact_rows = conn.execute(
        """
        SELECT answer
        FROM core_training_answers
        WHERE card_text_norm=?
        ORDER BY id DESC
        LIMIT 24
        """,
        (text_norm,),
    ).fetchall()
    family_rows = conn.execute(
        """
        SELECT answer
        FROM core_training_answers
        WHERE topic=? AND kind=?
        ORDER BY id DESC
        LIMIT 24
        """,
        (topic, kind_key),
    ).fetchall()
    topic_rows = conn.execute(
        """
        SELECT answer
        FROM core_training_answers
        WHERE topic=?
        ORDER BY id DESC
        LIMIT 36
        """,
        (topic,),
    ).fetchall()

    if not exact_rows and not family_rows and not topic_rows:
        bootstrap_conf = float(topic_prediction.get("confidence") or 0.0)
        spread = abs(question_prior_prob - 0.5) * 2.0
        confidence = min(0.62, 0.16 + bootstrap_conf * 0.32 + spread * 0.24)
        return {
            "answer": "yes" if question_prior_prob >= 0.5 else "no",
            "confidence": confidence,
            "bar_position": max(0.0, min(1.0, 0.5 + (question_prior_prob - 0.5) * (0.35 + 0.65 * confidence))),
            "scope": prior_scope,
            "basis": prior_basis,
        }

    yes_weight = question_prior_weight * question_prior_prob
    no_weight = question_prior_weight * (1.0 - question_prior_prob)
    observed_weight = 0.0
    scope = prior_scope
    basis = prior_basis

    def apply_rows(rows: list[Any], base_weight: float, decay: float) -> None:
        nonlocal yes_weight, no_weight, observed_weight
        for idx, row in enumerate(rows):
            weight = max(base_weight * 0.35, base_weight - idx * decay)
            observed_weight += weight
            if str(row["answer"] or "").strip().lower() == "yes":
                yes_weight += weight
            else:
                no_weight += weight

    apply_rows(exact_rows, 1.0, 0.045)
    if exact_rows:
        scope = "question_exact"
        basis = "exact_question_history"
    if not exact_rows:
        apply_rows(family_rows, 0.72, 0.03)
        if family_rows:
            scope = "question_family"
            basis = "family_history"
    if not exact_rows and not family_rows:
        apply_rows(topic_rows, 0.28, 0.012)
        if topic_rows:
            scope = "question_topic_backoff"
            basis = "topic_history"

    total_weight = max(1e-6, yes_weight + no_weight)
    prob = yes_weight / total_weight
    spread = abs(prob - 0.5) * 2.0
    exposure_factor = min(1.0, observed_weight / 7.0)
    floor = 0.18 if exact_rows else 0.12 if family_rows else 0.08
    ceiling = 0.96 if exact_rows else 0.78 if family_rows else 0.62
    confidence = min(ceiling, floor + spread * (0.28 + 0.72 * exposure_factor))
    return {
        "answer": "yes" if prob >= 0.5 else "no",
        "confidence": confidence,
        "bar_position": max(0.0, min(1.0, 0.5 + (prob - 0.5) * (0.35 + 0.65 * confidence))),
        "scope": scope,
        "basis": basis,
    }


def _generate_candidates(
    feature_payload: dict[str, Any],
    topic_predictions: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    candidates = []
    candidate_params = _candidate_params(feature_payload["features"])
    prediction_cache = topic_predictions or _load_topic_prediction_cache(feature_payload["features"])
    for template in TEMPLATES:
        if template.get("fixed_question"):
            candidates.append(_build_card(template, {}, feature_payload, prediction_cache))
            continue
        for params in candidate_params:
            candidates.append(_build_card(template, params, feature_payload, prediction_cache))
    return candidates


def _topic_stats(conn, topic: str) -> dict[str, float]:
    row = conn.execute(
        """
        SELECT COUNT(*) AS total,
               SUM(CASE WHEN answer != prediction THEN 1 ELSE 0 END) AS disagree
        FROM core_training_answers
        WHERE topic=?
        """,
        (topic,),
    ).fetchone()
    total = int(row["total"] or 0) if row else 0
    disagree = int(row["disagree"] or 0) if row else 0
    return {
        "total": float(total),
        "disagree_rate": (disagree / total) if total else 0.0,
    }


def get_core_principle_scores() -> dict[str, Any]:
    ensure_core_training_deck_schema()
    conn = get_core_db()
    try:
        legacy_map = {
            "plan_first": ["consistency", "consistency_frequency"],
            "rpe_honesty": ["consistency"],
            "logging_quality": ["consistency"],
            "nutrition_discipline": ["consistency"],
            "regeneration_honesty": ["consistency"],
            "run_quality": ["consistency_frequency", "consistency"],
            "regression_response": ["consistency_frequency", "consistency"],
        }

        def _score_rows(rows: list[sqlite3.Row], meta: dict[str, Any]) -> dict[str, Any]:
            total = len(rows)
            if not total:
                return {
                    "strength": 0,
                    "risk": 0,
                    "confidence": 0,
                    "exposures": 0,
                    "recent_yes_rate": 0.0,
                }

            orientation = str(meta.get("orientation") or "risk_yes")
            healthy = 0.0
            recent_unhealthy = 0.0
            recent_total = 0.0
            for idx, row in enumerate(rows):
                answer_yes = str(row["answer"] or "").strip().lower() == "yes"
                if orientation == "strength_yes":
                    healthy_answer = answer_yes
                    unhealthy_answer = not answer_yes
                else:
                    healthy_answer = not answer_yes
                    unhealthy_answer = answer_yes
                weight = max(0.35, 1.0 - idx * 0.045)
                if healthy_answer:
                    healthy += weight
                if idx < 12:
                    recent_total += weight
                    if unhealthy_answer:
                        recent_unhealthy += weight

            total_weight = sum(max(0.35, 1.0 - idx * 0.045) for idx in range(total))
            healthy_rate = healthy / max(1e-6, total_weight)
            exposure_factor = min(1.0, total / 18.0)
            recent_risk = recent_unhealthy / max(1e-6, recent_total or 1.0)
            strength = int(round(max(0.0, min(1.0, healthy_rate * (0.45 + 0.55 * exposure_factor))) * 100))
            risk = int(round(max(0.0, min(1.0, recent_risk * (0.55 + 0.45 * min(1.0, total / 12.0)))) * 100))
            confidence = int(round(min(1.0, total / 16.0) * 100))
            yes_rate = sum(1 for row in rows[:12] if str(row["answer"] or "").strip().lower() == "yes") / max(1, min(total, 12))
            return {
                "strength": strength,
                "risk": risk,
                "confidence": confidence,
                "exposures": total,
                "recent_yes_rate": round(yes_rate, 3),
            }

        out: dict[str, Any] = {}
        for topic, meta in PRINCIPLE_TOPICS.items():
            rows = conn.execute(
                """
                SELECT answer, t
                FROM core_training_answers
                WHERE topic=?
                ORDER BY id DESC
                LIMIT 48
                """,
                (topic,),
            ).fetchall()
            if not rows:
                legacy_topics = legacy_map.get(topic) or []
                if legacy_topics:
                    placeholders = ",".join("?" for _ in legacy_topics)
                    rows = conn.execute(
                        f"""
                        SELECT answer, t
                        FROM core_training_answers
                        WHERE topic IN ({placeholders})
                        ORDER BY id DESC
                        LIMIT 48
                        """,
                        tuple(legacy_topics),
                    ).fetchall()
            score = _score_rows(rows, meta)
            out[topic] = {
                "topic": topic,
                "label": meta["label"],
                "type": meta["type"],
                "strength": score["strength"],
                "risk": score["risk"],
                "confidence": score["confidence"],
                "exposures": score["exposures"],
                "recent_yes_rate": score["recent_yes_rate"],
            }
        return out
    finally:
        conn.close()


def _live_stats_from_conn(conn) -> dict[str, Any]:
    answered = int(conn.execute("SELECT COUNT(*) AS c FROM core_training_answers").fetchone()["c"] or 0)
    return {
        "answered_count": answered,
        "summary": f"{answered} Antworten",
    }


def get_core_training_live_stats() -> dict[str, Any]:
    ensure_core_training_deck_schema()
    conn = get_core_db()
    try:
        return _live_stats_from_conn(conn)
    finally:
        conn.close()


def _select_card(conn, feature_payload: dict[str, Any], recent_signatures: list[str] | None = None) -> dict[str, Any]:
    recent_signatures = recent_signatures or []
    seen_meta = _load_seen_meta(conn)
    topic_metrics = _topic_metrics(conn)
    topic_predictions = _load_topic_prediction_cache(feature_payload["features"])
    persisted_recent_signatures = _load_recent_seen_signatures(conn, limit=12)
    persisted_recent_topics = _load_recent_seen_topics(conn, limit=18)
    persisted_recent_prompt_norms = _load_recent_seen_prompt_norms(conn, limit=18)
    persisted_recent_prompt_texts = _load_recent_seen_prompt_texts(conn, limit=42)
    deleted_prompt_norms = _load_deleted_prompt_norms(conn)
    state = _load_state(conn)
    last_signature = str(state.get("last_signature") or "")
    rotation_index = int(state.get("rotation_index") or 0)
    rng_seed = int(state.get("rng_seed") or 17)
    recent_answer_rows = conn.execute(
        """
        SELECT topic, signature, kind
        FROM core_training_answers
        ORDER BY id DESC
        LIMIT 8
        """
    ).fetchall()
    recent_topics = [str(row["topic"] or "") for row in recent_answer_rows]
    recent_kinds = [str(row["kind"] or "") for row in recent_answer_rows]
    recent_parts = [kind.split(":") for kind in recent_kinds]
    recent_base_kinds = [parts[0] if parts else "" for parts in recent_parts]
    recent_families = [parts[1] if len(parts) > 1 else "" for parts in recent_parts]
    recent_dimensions = [parts[2] if len(parts) > 2 else "" for parts in recent_parts]
    last_topic = recent_topics[0] if recent_topics else ""
    last_family = recent_families[0] if recent_families else ""
    candidates = _generate_candidates(feature_payload, topic_predictions=topic_predictions)
    blocked = set(recent_signatures)
    blocked.update(persisted_recent_signatures)
    if last_signature:
        blocked.add(last_signature)
    pool = [card for card in candidates if card["signature"] not in blocked]
    if not pool:
        pool = [card for card in candidates if card["signature"] not in set(persisted_recent_signatures)]
    if not pool:
        pool = [card for card in candidates if card["signature"] not in set(recent_signatures)]
    if not pool and last_signature:
        pool = [card for card in candidates if card["signature"] != last_signature]
    if not pool:
        pool = candidates
    preferred_family = FAMILY_ROTATION[rotation_index % len(FAMILY_ROTATION)]
    best = None
    best_score = -1e9
    recent_prompt_norms = {_normalize_prompt(card["text"]) for card in candidates if card["signature"] in set(recent_signatures)}
    recent_prompt_norms.update(persisted_recent_prompt_norms)
    recent_prompt_norms.update(deleted_prompt_norms)
    recent_prompt_token_sets = [_prompt_tokens(text) for text in persisted_recent_prompt_texts if str(text or "").strip()]
    if state.get("last_card_json"):
        try:
            last_card = json.loads(state["last_card_json"])
        except Exception:
            last_card = {}
        last_text = _normalize_prompt(str(last_card.get("text") or ""))
        if last_text:
            recent_prompt_norms.add(last_text)
            toks = _prompt_tokens(last_text)
            if toks:
                recent_prompt_token_sets.append(toks)
    text_filtered_pool = [
        card for card in pool
        if not _is_text_near_duplicate(card["text"], recent_prompt_norms, recent_prompt_token_sets)
    ]
    if text_filtered_pool:
        pool = text_filtered_pool
    for card in pool:
        confidence = float(card["prediction"]["confidence"] or 0.0)
        novelty = _novelty_score(card["meta"]["topic"], feature_payload["features"], topic_metrics)
        need = _topic_need_bonus(card["meta"]["topic"], topic_metrics)
        penalty = _recent_penalty(card["signature"], recent_signatures)
        seen_penalty = _seen_penalty(card["signature"], seen_meta, recent_signatures)
        text_penalty = 0.0
        if _normalize_prompt(card["text"]) in recent_prompt_norms:
            text_penalty += 3.0
        topic_hits = sum(1 for topic in recent_topics[:4] if topic == card["meta"]["topic"])
        if topic_hits:
            text_penalty += min(1.8, topic_hits * 0.55)
        if last_topic and card["meta"]["topic"] == last_topic:
            text_penalty += 4.0
        elif card["meta"]["topic"] in set(recent_topics[:6]):
            text_penalty += 1.6
        persisted_topic_hits = sum(1 for topic in persisted_recent_topics[:8] if topic == card["meta"]["topic"])
        if persisted_topic_hits:
            text_penalty += min(3.2, persisted_topic_hits * 0.8)
        family = str(card["meta"].get("family") or "")
        family_hits = sum(1 for item in recent_families[:5] if item and item == family)
        if family_hits:
            text_penalty += min(2.2, family_hits * 0.7)
        if last_family and family == last_family:
            text_penalty += 2.0
        dimension = str(card["meta"].get("dimension") or "")
        dimension_hits = sum(1 for item in recent_dimensions[:4] if item and item == dimension)
        if dimension_hits:
            text_penalty += min(1.8, dimension_hits * 0.65)
        if recent_base_kinds[:2].count(card["meta"]["kind"]) >= 2:
            text_penalty += 0.65
        family_bonus = 0.52 if family == preferred_family else 0.0
        jitter = _seeded_jitter(card["signature"], rng_seed)
        score = (1.0 - confidence) * 1.25 + novelty + need + family_bonus + jitter - penalty - seen_penalty - text_penalty
        if score > best_score:
            best_score = score
            best = card
    assert best is not None
    conn.execute(
        "UPDATE core_training_state SET rotation_index=?, rng_seed=? WHERE id=1",
        (((rotation_index + 1) % len(FAMILY_ROTATION)), ((rng_seed * 1103515245 + 12345) & 0x7FFFFFFF)),
    )
    best["prediction"] = _predict_card_from_history(conn, best, feature_payload["features"])
    return best


def next_card(recent_signatures: list[str] | None = None) -> dict[str, Any]:
    ensure_core_training_deck_schema()
    feature_payload = load_latest_features()
    conn = get_core_db()
    try:
        card = _select_card(conn, feature_payload, recent_signatures=recent_signatures)
        _upsert_seen(conn, card)
        _save_state(conn, card)
        conn.commit()
        return {
            "card": {k: v for k, v in card.items() if k != "prediction"},
            "prediction": dict(card["prediction"]),
            "stats": _live_stats_from_conn(conn),
        }
    finally:
        conn.close()


def should_trigger_followup(
    answer: str,
    prediction_answer: str,
    confidence: float,
    contradiction_streak: int = 0,
    topic_answers: int = 0,
    disagreement_rate: float = 0.0,
    model_uncertainty: float | None = None,
) -> bool:
    contradicted = str(answer).lower() != str(prediction_answer).lower()
    uncertainty = 1.0 - float(model_uncertainty if model_uncertainty is not None else confidence)
    if contradicted and (confidence >= 0.65 or topic_answers < 12 or disagreement_rate >= 0.34):
        return True
    if contradicted and contradiction_streak >= 2:
        return True
    if uncertainty >= 0.72 and topic_answers < 8:
        return True
    return False


def _pick_followup_dimension(base_card: dict[str, Any], conn) -> str:
    dimensions = list(base_card["meta"].get("followup_dimensions") or [])
    if not dimensions:
        return "severity"
    rows = conn.execute(
        "SELECT kind FROM core_training_answers ORDER BY id DESC LIMIT 6"
    ).fetchall()
    recent_dimensions = []
    for row in rows:
        parts = str(row["kind"] or "").split(":")
        if len(parts) > 2:
            recent_dimensions.append(parts[2])
    scored = []
    seed = int(hashlib.sha1(str(base_card.get("signature") or "").encode("utf-8")).hexdigest()[:8], 16)
    for idx, dim in enumerate(dimensions):
        cooldown = sum(1 for item in recent_dimensions[:4] if item == dim)
        jitter = ((seed + idx * 17) % 97) / 97.0
        scored.append((cooldown, jitter, idx, dim))
    scored.sort()
    return scored[0][3]


def _followup_text(base_card: dict[str, Any], feature_payload: dict[str, Any], dimension: str) -> str:
    topic = str(base_card["meta"].get("topic") or "consistency_frequency")
    features = feature_payload["features"]
    recovery_good = features.get("recovery_state") == "high"
    recovery_ok = features.get("recovery_state") == "ok"
    nutrition_bad = features.get("nutrition_adherence") == "low"
    run_high = features.get("run_load") == "high"
    strength_down = features.get("strength_trend") == "down"
    if dimension == "volume_knob":
        if topic == "volume_vs_intensity":
            return "Wenn die Leistung rückläufig ist → lieber Intensität senken statt Sätze reduzieren?"
        return "Wenn CORE reagieren soll → lieber Volumen anpassen statt Intensität?"
    if dimension == "severity":
        if topic == "volume_vs_intensity":
            return "Wenn die Recovery nur leicht schlechter ist → lieber trotzdem zuerst Sätze reduzieren?"
        if topic == "proximity_to_failure":
            return "Wenn nur ein Ausreißer dabei war → soll CORE ruhiger bleiben?"
        if topic == "nutrition_as_limiter":
            return "Wenn die Ernährung nur leicht abweicht → soll CORE ruhig bleiben?"
        if topic == "deload_philosophy":
            return "Wenn nur ein Warnsignal da ist → soll CORE noch nicht entlasten?"
        if topic == "interference_run_gym":
            return "Wenn die Recovery dabei gut bleibt → soll CORE trotzdem konservativer reagieren?"
        if topic == "progression_discipline":
            return "Wenn die Recovery gut ist → soll CORE auch bei Schwankungen kleine Schritte zulassen?"
        if topic == "signal_weighting":
            return "Wenn der Trend erst über zwei Einheiten kippt → soll CORE ruhiger bleiben?"
        if topic == "consistency_frequency":
            return "Wenn die Recovery gut ist → soll CORE trotzdem einfache, wiederholbare Einheiten bevorzugen?"
        return "Wenn die Lage nur leicht schlechter wird → soll CORE noch ruhig bleiben?"
    if dimension == "persistence":
        if topic == "proximity_to_failure":
            return "Wenn das zwei Einheiten hintereinander passiert → soll CORE konsequenter bremsen?"
        if topic == "deload_philosophy":
            return "Wenn zwei Einheiten hintereinander Warnsignale da sind → soll CORE entlasten?"
        if topic == "interference_run_gym":
            return "Wenn das mehrere Wochen so bleibt → soll CORE konsequenter reagieren?"
        if topic == "nutrition_as_limiter":
            return "Wenn die Ernährung fast die ganze Woche abweicht → soll CORE eingreifen?"
        if topic == "signal_weighting":
            return "Wenn der Trend über mehrere Einheiten kippt → soll CORE stärker reagieren?"
        return "Wenn das mehrfach hintereinander passiert → soll CORE stärker reagieren?"
    if dimension == "rpe_guard":
        return "Wenn nur ein Ausreißer dabei war → soll CORE ruhiger bleiben?"
    if dimension == "progression_rule":
        return "Wenn die Recovery nur okay ist → soll CORE dann erst stabilisieren?"
    if dimension == "deload_threshold":
        return "Wenn mehrere Warnsignale gleichzeitig auftreten → soll CORE früh entlasten?"
    if dimension == "frequency_rule":
        if recovery_good:
            return "Wenn die Recovery gut ist → soll CORE trotzdem einfache, wiederholbare Einheiten bevorzugen?"
        return "Wenn die Recovery schlecht ist → soll CORE Konstanz über Perfektion stellen?"
    if dimension == "interference_horizon":
        if run_high:
            return "Wenn die Laufbelastung nur diese Woche hoch ist → soll CORE weniger reagieren?"
        return "Wenn die Belastung mehrere Wochen hoch ist → soll CORE konsequenter reagieren?"
    if dimension == "nutrition_trigger":
        if nutrition_bad and features.get("weight_trend") == "up":
            return "Wenn das Körpergewicht dabei klar steigt → soll CORE stärker eingreifen?"
        return "Wenn die Ernährung nur leicht abweicht → soll CORE ruhig bleiben?"
    if dimension == "signal_priority":
        if strength_down and (recovery_good or recovery_ok):
            return "Wenn der Trend erst über zwei Einheiten kippt → soll CORE ruhiger bleiben?"
        return "Wenn die Recovery gut bleibt → soll CORE den Leistungstrend trotzdem ernster nehmen?"
    return "Wenn die Lage klarer wird → soll CORE konsequenter reagieren?"


def _followup_card(base_card: dict[str, Any], feature_payload: dict[str, Any], conn) -> dict[str, Any]:
    topic = str(base_card["meta"].get("topic") or "consistency_frequency")
    dimension = _pick_followup_dimension(base_card, conn)
    question = _followup_text(base_card, feature_payload, dimension)
    follow = {
        "id": uuid.uuid4().hex,
        "signature": hashlib.sha1((base_card["signature"] + f":followup:{dimension}").encode("utf-8")).hexdigest(),
        "text": question,
        "meta": {
            "kind": "followup",
            "topic": topic,
            "family": str(base_card["meta"].get("family") or topic),
            "pill": TOPIC_COPY.get(topic, "Follow-up"),
            "dimension": dimension,
            "feature_digest": dict(base_card["meta"].get("feature_digest") or {}),
            "summaries": dict(base_card["meta"].get("summaries") or {}),
            "params": {**dict(base_card["meta"].get("params") or {}), "followup": True},
            "template_id": str(base_card["meta"].get("template_id") or "followup"),
            "followup_of": base_card["signature"],
            "followup_dimensions": list(base_card["meta"].get("followup_dimensions") or []),
        },
    }
    pred = _predict_card_from_history(conn, follow, feature_payload["features"])
    return {"card": follow, "prediction": pred}


def _contradiction_streak(conn, topic: str) -> int:
    rows = conn.execute(
        "SELECT answer, prediction FROM core_training_answers WHERE topic=? ORDER BY id DESC LIMIT 4",
        (topic,),
    ).fetchall()
    streak = 0
    for row in rows:
        if str(row["answer"]).lower() != str(row["prediction"]).lower():
            streak += 1
        else:
            break
    return streak


def answer_card(card_id: str, signature: str, answer: str, client_ms: int | None = None, recent_signatures: list[str] | None = None) -> dict[str, Any]:
    ensure_core_training_deck_schema()
    answer = "yes" if str(answer).strip().lower() == "yes" else "no"
    conn = get_core_db()
    try:
        state = _load_state(conn)
        raw_card = state.get("last_card_json") or "{}"
        try:
            stored = json.loads(raw_card)
        except Exception:
            stored = {}
        if str(stored.get("signature") or "") != str(signature or ""):
            seen_row = conn.execute(
                "SELECT card_json FROM core_training_seen WHERE signature=?",
                (signature,),
            ).fetchone()
            if seen_row and str(seen_row["card_json"] or "").strip():
                try:
                    stored = json.loads(str(seen_row["card_json"]))
                except Exception:
                    stored = {}
        if str(stored.get("signature") or "") != str(signature or ""):
            feature_payload = load_latest_features()
            topic_guess = "consistency_frequency"
            prediction_guess = predict_topic(topic_guess, feature_payload.get("features") or {})
            stored = {
                "id": card_id,
                "signature": signature,
                "meta": {"topic": topic_guess, "kind": "principle", "family": topic_guess, "dimension": "frequency_rule", "feature_digest": {}},
                "prediction": {
                    "answer": prediction_guess.get("answer") or "yes",
                    "confidence": float(prediction_guess.get("confidence") or 0.0),
                    "bar_position": float(prediction_guess.get("bar_position") or 0.5),
                    "scope": "question_text",
                    "basis": "question_text",
                },
            }
        else:
            feature_payload = load_latest_features()
        topic = str(stored.get("meta", {}).get("topic") or "consistency_frequency")
        kind = str(stored.get("meta", {}).get("kind") or "principle")
        family = str(stored.get("meta", {}).get("family") or topic)
        dimension = str(stored.get("meta", {}).get("dimension") or family)
        pred_answer = str(stored.get("prediction", {}).get("answer") or "yes")
        pred_conf = float(stored.get("prediction", {}).get("confidence") or 0.0)
        features = feature_payload["features"]
        model_before = get_topic_snapshot(topic)
        seen_before_row = conn.execute(
            "SELECT last_answer, last_latency_ms, last_seen_at FROM core_training_seen WHERE signature=?",
            (signature,),
        ).fetchone()
        learned = update_topic(topic, features, answer)
        conn.execute(
            """
            INSERT INTO core_training_answers (t, signature, card_id, topic, kind, card_text_norm, answer, prediction, confidence, features_json, client_ms)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _ts(),
                signature,
                card_id,
                topic,
                f"{kind}:{family}:{dimension}",
                _normalize_prompt(str(stored.get("text") or "")),
                answer,
                pred_answer,
                pred_conf,
                json.dumps(features, ensure_ascii=False, sort_keys=True),
                int(client_ms or 0),
            ),
        )
        ingest_training_answer(
            question_text=str(stored.get("text") or ""),
            topic=topic,
            dimension=dimension,
            answer=answer,
            signature=signature,
            answered_at=_ts(),
            conn=conn,
        )
        answer_row_id = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
        conn.execute(
            "UPDATE core_training_seen SET last_answer=?, last_latency_ms=?, last_seen_at=? WHERE signature=?",
            (answer, int(client_ms or 0), _ts(), signature),
        )
        undo_payload = {
            "card": stored,
            "answer_row_id": answer_row_id,
            "topic": topic,
            "model_before": model_before,
            "state_before": {
                "last_served_at": state.get("last_served_at"),
                "last_signature": state.get("last_signature"),
                "last_card_json": state.get("last_card_json"),
                "rotation_index": int(state.get("rotation_index") or 0),
                "rng_seed": int(state.get("rng_seed") or 17),
            },
            "seen_before": dict(seen_before_row) if seen_before_row else None,
        }
        conn.execute(
            "UPDATE core_training_state SET last_answer_json=?, updated_at=? WHERE id=1",
            (json.dumps(undo_payload, ensure_ascii=False), _ts()),
        )
        contradiction_streak = _contradiction_streak(conn, topic)
        topic_stats = _topic_stats(conn, topic)
        use_followup = should_trigger_followup(
            answer,
            pred_answer,
            pred_conf,
            contradiction_streak=contradiction_streak,
            topic_answers=int(topic_stats["total"]),
            disagreement_rate=float(topic_stats["disagree_rate"]),
            model_uncertainty=1.0 - pred_conf,
        )
        next_payload = None
        if use_followup:
            nxt = _followup_card(stored, feature_payload, conn)
            _upsert_seen(conn, {**nxt["card"], "meta": nxt["card"]["meta"]})
            _save_state(conn, {**nxt["card"], "prediction": nxt["prediction"]})
            next_payload = nxt
        else:
            selected = _select_card(conn, feature_payload, recent_signatures=recent_signatures)
            _upsert_seen(conn, selected)
            _save_state(conn, selected)
            next_payload = {
                "card": {k: v for k, v in selected.items() if k != "prediction"},
                "prediction": dict(selected["prediction"]),
            }
        conn.commit()
        stats = _live_stats_from_conn(conn)
        return {
            "ok": True,
            "learned": {"updated": True, "delta_confidence": learned["delta_confidence"]},
            "next": next_payload,
            "stats": stats,
        }
    finally:
        try:
            conn.commit()
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass


def reset_deck() -> None:
    ensure_core_training_deck_schema()
    conn = get_core_db()
    try:
        for table in ("core_training_seen", "core_training_answers", "core_training_model"):
            conn.execute(f"DELETE FROM {table}")
        conn.execute("DELETE FROM core_training_question_policy")
        conn.execute("DELETE FROM core_training_policy_events")
        conn.execute(
            "UPDATE core_training_state SET updated_at=?, last_served_at=NULL, last_signature=NULL, last_card_json=NULL, last_answer_json=NULL, rotation_index=?, rng_seed=? WHERE id=1",
            (_ts(), random.SystemRandom().randrange(len(FAMILY_ROTATION)), random.SystemRandom().randint(1, 2_147_483_647)),
        )
        conn.commit()
    finally:
        conn.close()


def undo_last_answer() -> dict[str, Any]:
    ensure_core_training_deck_schema()
    conn = get_core_db()
    try:
        state = _load_state(conn)
        raw = state.get("last_answer_json") or ""
        if not raw:
            return {"ok": False, "error": "nothing_to_undo"}
        try:
            payload = json.loads(raw)
        except Exception:
            return {"ok": False, "error": "invalid_undo_state"}
        topic = str(payload.get("topic") or "consistency")
        model_before = payload.get("model_before") or {}
        restore_topic_snapshot(
            topic,
            {str(k): float(v) for k, v in dict(model_before.get("weights") or {}).items()},
            float(model_before.get("bias") or 0.0),
        )
        answer_row_id = int(payload.get("answer_row_id") or 0)
        if answer_row_id:
            conn.execute("DELETE FROM core_training_answers WHERE id=?", (answer_row_id,))
            rebuild_core_question_policy(conn)
        card = dict(payload.get("card") or {})
        if (not str(card.get("text") or "").strip()) and str(card.get("signature") or "").strip():
            seen_row = conn.execute(
                "SELECT card_json FROM core_training_seen WHERE signature=?",
                (str(card.get("signature") or ""),),
            ).fetchone()
            if seen_row and str(seen_row["card_json"] or "").strip():
                try:
                    card = json.loads(str(seen_row["card_json"]))
                except Exception:
                    pass
        signature = str(card.get("signature") or "")
        seen_before = payload.get("seen_before")
        if signature:
            if seen_before:
                conn.execute(
                    """
                    UPDATE core_training_seen
                    SET last_answer=?, last_latency_ms=?, last_seen_at=?
                    WHERE signature=?
                    """,
                    (
                        seen_before.get("last_answer"),
                        seen_before.get("last_latency_ms"),
                        seen_before.get("last_seen_at"),
                        signature,
                    ),
                )
            else:
                conn.execute(
                    "UPDATE core_training_seen SET last_answer=NULL, last_latency_ms=NULL WHERE signature=?",
                    (signature,),
                )
        state_before = payload.get("state_before") or {}
        conn.execute(
            """
            UPDATE core_training_state
            SET updated_at=?, last_served_at=?, last_signature=?, last_card_json=?, last_answer_json=NULL, rotation_index=?, rng_seed=?
            WHERE id=1
            """,
            (
                _ts(),
                state_before.get("last_served_at"),
                state_before.get("last_signature"),
                state_before.get("last_card_json"),
                int(state_before.get("rotation_index") or 0),
                int(state_before.get("rng_seed") or 17),
            ),
        )
        conn.commit()
        return {
            "ok": True,
            "card": {k: v for k, v in card.items() if k != "prediction"},
            "prediction": dict(card.get("prediction") or {}),
            "stats": _live_stats_from_conn(conn),
        }
    finally:
        conn.close()


def delete_card(signature: str) -> dict[str, Any]:
    ensure_core_training_deck_schema()
    conn = get_core_db()
    try:
        state = _load_state(conn)
        card_row = conn.execute(
            "SELECT card_text_norm, card_topic, card_json FROM core_training_seen WHERE signature=?",
            (str(signature or ""),),
        ).fetchone()
        card_text_norm = ""
        topic = ""
        raw_card_text_norm = ""
        if card_row:
            card_text_norm = str(card_row["card_text_norm"] or "").strip()
            topic = str(card_row["card_topic"] or "").strip()
            try:
                raw_card = json.loads(card_row["card_json"] or "{}")
            except Exception:
                raw_card = {}
            raw_card_text_norm = str(raw_card.get("text") or "").strip().lower()
        if not card_text_norm and str((state.get("last_signature") or "")).strip() == str(signature or "").strip():
            try:
                last_card = json.loads(state.get("last_card_json") or "{}")
            except Exception:
                last_card = {}
            card_text_norm = _normalize_prompt(str(last_card.get("text") or ""))
            raw_card_text_norm = str(last_card.get("text") or "").strip().lower()
            topic = str(last_card.get("meta", {}).get("topic") or topic).strip()
        if not card_text_norm:
            return {"ok": False, "error": "card_not_found"}

        tombstone_keys = [card_text_norm]
        if raw_card_text_norm and raw_card_text_norm not in tombstone_keys:
            tombstone_keys.append(raw_card_text_norm)
        for tombstone_key in tombstone_keys:
            conn.execute(
            """
            INSERT INTO core_training_deleted_cards (card_text_norm, topic, deleted_at)
            VALUES (?, ?, ?)
            ON CONFLICT(card_text_norm) DO UPDATE SET
                topic=excluded.topic,
                deleted_at=excluded.deleted_at
            """,
                (tombstone_key, topic, _ts()),
            )
        conn.execute("DELETE FROM core_training_answers WHERE card_text_norm=?", (card_text_norm,))
        conn.execute("DELETE FROM core_training_seen WHERE card_text_norm=?", (card_text_norm,))
        rebuild_core_question_policy(conn)

        rows = conn.execute(
            """
            SELECT topic, answer, features_json
            FROM core_training_answers
            ORDER BY id ASC
            """
        ).fetchall()
        rebuild_rows = []
        for row in rows:
            try:
                features = json.loads(str(row["features_json"] or "{}"))
            except Exception:
                features = {}
            rebuild_rows.append(
                {
                    "topic": str(row["topic"] or ""),
                    "answer": str(row["answer"] or ""),
                    "features": features if isinstance(features, dict) else {},
                }
            )
        conn.execute("UPDATE core_training_state SET last_signature=NULL, last_card_json=NULL, last_answer_json=NULL WHERE id=1")
        conn.commit()
    finally:
        conn.close()

    rebuild_models_from_answer_rows(rebuild_rows)
    conn = get_core_db()
    try:
        stats = _live_stats_from_conn(conn)
    finally:
        conn.close()
    return {"ok": True, "deleted_prompt": card_text_norm, "stats": stats}


__all__ = [
    "ensure_core_training_deck_schema",
    "next_card",
    "answer_card",
    "undo_last_answer",
    "delete_card",
    "reset_deck",
    "make_signature",
    "should_trigger_followup",
    "get_core_principle_scores",
    "get_core_training_live_stats",
]
