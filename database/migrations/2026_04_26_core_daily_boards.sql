CREATE TABLE IF NOT EXISTS core_daily_boards (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    day_iso TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'gpt_checkin',
    trigger TEXT,
    source_message TEXT,
    human_headline TEXT,
    human_summary TEXT,
    decision_intent TEXT,
    confidence REAL,
    machine_briefing_json TEXT NOT NULL DEFAULT '{}',
    visible_reasons_json TEXT NOT NULL DEFAULT '[]',
    training_card_json TEXT NOT NULL DEFAULT '{}',
    data_status_json TEXT NOT NULL DEFAULT '{}',
    legacy_core_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'active',
    user_feedback TEXT,
    user_feedback_at TEXT,
    UNIQUE(day_iso)
);

CREATE INDEX IF NOT EXISTS idx_core_daily_boards_day_status
    ON core_daily_boards(day_iso, status);

CREATE INDEX IF NOT EXISTS idx_core_daily_boards_updated
    ON core_daily_boards(updated_at DESC);
