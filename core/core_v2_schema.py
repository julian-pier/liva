from __future__ import annotations

import sqlite3

from database.connections import get_core_db


CORE_V2_TABLES: tuple[str, ...] = (
    "core_observations",
    "core_patterns",
    "core_hypotheses",
    "core_decision_reviews",
    "core_model_updates",
    "core_blind_spots",
    "core_daily_snapshot_v2",
    "core_clone_snapshots",
    "core_simulation_runs",
    "core_parameter_versions",
    "core_parameter_updates",
    "core_simulation_feedback",
    "core_decision_explain_v2",
)


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
        (table,),
    ).fetchone()
    return bool(row)


def ensure_core_v2_schema() -> None:
    conn = get_core_db()
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS core_observations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                observed_at TEXT NOT NULL,
                domain TEXT NOT NULL,
                signal_key TEXT NOT NULL,
                signal_value_json TEXT NOT NULL DEFAULT '{}',
                source TEXT NOT NULL DEFAULT 'system',
                quality_score REAL NOT NULL DEFAULT 0.0,
                confidence REAL NOT NULL DEFAULT 0.0,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_core_observations_observed_at ON core_observations(observed_at DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_core_observations_domain ON core_observations(domain, observed_at DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_core_observations_signal ON core_observations(signal_key, observed_at DESC)")

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS core_patterns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                pattern_key TEXT NOT NULL UNIQUE,
                title TEXT NOT NULL,
                summary TEXT NOT NULL,
                domain TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'monitoring',
                first_seen TEXT NOT NULL,
                last_seen TEXT NOT NULL,
                confidence REAL NOT NULL DEFAULT 0.0,
                stability_score REAL NOT NULL DEFAULT 0.0,
                evidence_count INTEGER NOT NULL DEFAULT 0,
                support_score REAL NOT NULL DEFAULT 0.0,
                contradiction_score REAL NOT NULL DEFAULT 0.0,
                evidence_json TEXT NOT NULL DEFAULT '[]',
                notes TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_core_patterns_status ON core_patterns(status, confidence DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_core_patterns_domain ON core_patterns(domain, last_seen DESC)")

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS core_hypotheses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                hypothesis_key TEXT NOT NULL UNIQUE,
                title TEXT NOT NULL,
                statement TEXT NOT NULL,
                domain TEXT NOT NULL,
                type TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'testing',
                confidence REAL NOT NULL DEFAULT 0.0,
                impact_score REAL NOT NULL DEFAULT 0.0,
                support_count INTEGER NOT NULL DEFAULT 0,
                contradiction_count INTEGER NOT NULL DEFAULT 0,
                evidence_json TEXT NOT NULL DEFAULT '[]',
                relevance_text TEXT NOT NULL DEFAULT '',
                last_reviewed_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_core_hypotheses_status ON core_hypotheses(status, confidence DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_core_hypotheses_domain ON core_hypotheses(domain, updated_at DESC)")

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS core_decision_reviews (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                decision_id INTEGER,
                review_date TEXT NOT NULL,
                outcome_label TEXT NOT NULL,
                outcome_score REAL NOT NULL DEFAULT 0.0,
                expected_vs_actual_json TEXT NOT NULL DEFAULT '{}',
                what_worked TEXT,
                what_failed TEXT,
                should_repeat INTEGER NOT NULL DEFAULT 0,
                should_weaken_reason_codes_json TEXT NOT NULL DEFAULT '[]',
                should_strengthen_reason_codes_json TEXT NOT NULL DEFAULT '[]',
                generated_summary TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(decision_id) REFERENCES core_decision_log(id)
            )
            """
        )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_core_decision_reviews_decision_date ON core_decision_reviews(decision_id, review_date)"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_core_decision_reviews_outcome ON core_decision_reviews(outcome_label, review_date DESC)")

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS core_model_updates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                update_date TEXT NOT NULL,
                update_type TEXT NOT NULL,
                object_type TEXT NOT NULL,
                object_key TEXT NOT NULL,
                old_state_json TEXT NOT NULL DEFAULT '{}',
                new_state_json TEXT NOT NULL DEFAULT '{}',
                reason_summary TEXT NOT NULL,
                trigger_decision_id INTEGER,
                created_at TEXT NOT NULL,
                FOREIGN KEY(trigger_decision_id) REFERENCES core_decision_log(id)
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_core_model_updates_date ON core_model_updates(update_date DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_core_model_updates_object ON core_model_updates(object_type, object_key)")

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS core_blind_spots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                blind_spot_key TEXT NOT NULL UNIQUE,
                title TEXT NOT NULL,
                description TEXT NOT NULL,
                severity TEXT NOT NULL,
                domain TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'open',
                recommendation TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_core_blind_spots_status ON core_blind_spots(status, severity)")

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS core_daily_snapshot_v2 (
                date TEXT PRIMARY KEY,
                raw_context_json TEXT NOT NULL DEFAULT '{}',
                interpreted_patterns_json TEXT NOT NULL DEFAULT '[]',
                active_hypotheses_json TEXT NOT NULL DEFAULT '[]',
                decision_summary_json TEXT NOT NULL DEFAULT '{}',
                uncertainty_json TEXT NOT NULL DEFAULT '{}',
                next_review_at TEXT,
                created_at TEXT NOT NULL
            )
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS core_clone_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL,
                created_at TEXT NOT NULL,
                clone_version TEXT NOT NULL,
                base_state_json TEXT NOT NULL DEFAULT '{}',
                sensitivities_json TEXT NOT NULL DEFAULT '{}',
                learned_patterns_json TEXT NOT NULL DEFAULT '[]',
                summary_json TEXT NOT NULL DEFAULT '{}'
            )
            """
        )
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_core_clone_snapshots_date ON core_clone_snapshots(date)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_core_clone_snapshots_created ON core_clone_snapshots(created_at DESC)")

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS core_simulation_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL,
                created_at TEXT NOT NULL,
                scenario_key TEXT NOT NULL,
                scenario_type TEXT NOT NULL,
                input_context_json TEXT NOT NULL DEFAULT '{}',
                predicted_outcome_json TEXT NOT NULL DEFAULT '{}',
                chosen_by_core INTEGER NOT NULL DEFAULT 0,
                score REAL NOT NULL DEFAULT 0.0,
                explanation_json TEXT NOT NULL DEFAULT '{}'
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_core_simulation_runs_date ON core_simulation_runs(date DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_core_simulation_runs_scenario ON core_simulation_runs(scenario_key, date DESC)")

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS core_parameter_versions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                version_tag TEXT NOT NULL,
                parameter_json TEXT NOT NULL DEFAULT '{}',
                derived_from_range_start TEXT,
                derived_from_range_end TEXT,
                notes TEXT,
                active INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_core_parameter_versions_active ON core_parameter_versions(active, id DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_core_parameter_versions_created ON core_parameter_versions(created_at DESC)")

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS core_parameter_updates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                parameter_name TEXT NOT NULL,
                old_value REAL NOT NULL DEFAULT 0.0,
                new_value REAL NOT NULL DEFAULT 0.0,
                delta REAL NOT NULL DEFAULT 0.0,
                trigger_reason TEXT NOT NULL,
                evidence_json TEXT NOT NULL DEFAULT '{}',
                confidence REAL NOT NULL DEFAULT 0.0
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_core_parameter_updates_created ON core_parameter_updates(created_at DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_core_parameter_updates_name ON core_parameter_updates(parameter_name, created_at DESC)")

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS core_simulation_feedback (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                prediction_date TEXT NOT NULL,
                actual_date TEXT NOT NULL,
                created_at TEXT NOT NULL,
                selected_mode TEXT NOT NULL DEFAULT '',
                selected_scenario_key TEXT NOT NULL DEFAULT '',
                predicted_json TEXT NOT NULL DEFAULT '{}',
                actual_json TEXT NOT NULL DEFAULT '{}',
                error_json TEXT NOT NULL DEFAULT '{}',
                overall_error REAL NOT NULL DEFAULT 0.0,
                quality_label TEXT NOT NULL DEFAULT 'inconclusive',
                driver_impact_json TEXT NOT NULL DEFAULT '{}'
            )
            """
        )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_core_sim_feedback_pair ON core_simulation_feedback(prediction_date, actual_date)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_core_sim_feedback_actual ON core_simulation_feedback(actual_date DESC)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_core_sim_feedback_quality ON core_simulation_feedback(quality_label, actual_date DESC)"
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS core_decision_explain_v2 (
                date TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                decision_json TEXT NOT NULL DEFAULT '{}',
                night_cycle_json TEXT NOT NULL DEFAULT '{}',
                clone_json TEXT NOT NULL DEFAULT '{}',
                scenarios_json TEXT NOT NULL DEFAULT '{}',
                learning_json TEXT NOT NULL DEFAULT '{}',
                trace_json TEXT NOT NULL DEFAULT '{}',
                payload_json TEXT NOT NULL DEFAULT '{}'
            )
            """
        )

        conn.commit()
    finally:
        conn.close()


def core_v2_schema_ready() -> bool:
    conn = get_core_db()
    try:
        for table in CORE_V2_TABLES:
            if not _table_exists(conn, table):
                return False
        return True
    finally:
        conn.close()
