-- Autopilot settings + logs (LIVA)

CREATE TABLE IF NOT EXISTS settings_kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_ts INTEGER
);

CREATE TABLE IF NOT EXISTS autopilot_day (
    date TEXT PRIMARY KEY,
    mode TEXT NOT NULL,
    output_json TEXT NOT NULL,
    reason_codes_json TEXT,
    created_ts INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS override_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    decision_type TEXT NOT NULL,
    proposed_json TEXT,
    applied_json TEXT,
    user_override_bool INTEGER DEFAULT 0,
    context_json TEXT
);

CREATE TABLE IF NOT EXISTS movement_library (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    movement_key TEXT NOT NULL,
    label TEXT NOT NULL,
    muscle_group TEXT,
    metadata_json TEXT
);

CREATE TABLE IF NOT EXISTS swap_groups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_key TEXT NOT NULL,
    movement_key TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pain_flags (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    movement_key TEXT,
    note TEXT
);
