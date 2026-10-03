-- CORE Heavy-First / Night-Cycle v2 schema upgrade

CREATE TABLE IF NOT EXISTS core_clone_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    created_at TEXT NOT NULL,
    clone_version TEXT NOT NULL,
    base_state_json TEXT NOT NULL DEFAULT '{}',
    sensitivities_json TEXT NOT NULL DEFAULT '{}',
    learned_patterns_json TEXT NOT NULL DEFAULT '[]',
    summary_json TEXT NOT NULL DEFAULT '{}'
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_core_clone_snapshots_date ON core_clone_snapshots(date);
CREATE INDEX IF NOT EXISTS idx_core_clone_snapshots_created ON core_clone_snapshots(created_at DESC);

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
);
CREATE INDEX IF NOT EXISTS idx_core_simulation_runs_date ON core_simulation_runs(date DESC);
CREATE INDEX IF NOT EXISTS idx_core_simulation_runs_scenario ON core_simulation_runs(scenario_key, date DESC);

CREATE TABLE IF NOT EXISTS core_parameter_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    version_tag TEXT NOT NULL,
    parameter_json TEXT NOT NULL DEFAULT '{}',
    derived_from_range_start TEXT,
    derived_from_range_end TEXT,
    notes TEXT,
    active INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_core_parameter_versions_active ON core_parameter_versions(active, id DESC);
CREATE INDEX IF NOT EXISTS idx_core_parameter_versions_created ON core_parameter_versions(created_at DESC);

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
);
CREATE INDEX IF NOT EXISTS idx_core_parameter_updates_created ON core_parameter_updates(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_core_parameter_updates_name ON core_parameter_updates(parameter_name, created_at DESC);

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
);
