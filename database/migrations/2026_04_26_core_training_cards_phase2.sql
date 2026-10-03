-- Phase 2.0 CORE Training-Cards
-- Existing core_training_cards table is reused and extended so the legacy
-- CORE training deck stays intact while daily CORE Board cards persist here too.

ALTER TABLE core_training_cards ADD COLUMN day_iso TEXT;
ALTER TABLE core_training_cards ADD COLUMN board_id INTEGER;
ALTER TABLE core_training_cards ADD COLUMN updated_at TEXT;
ALTER TABLE core_training_cards ADD COLUMN source TEXT NOT NULL DEFAULT 'core_board';
ALTER TABLE core_training_cards ADD COLUMN status TEXT NOT NULL DEFAULT 'active';
ALTER TABLE core_training_cards ADD COLUMN session_type TEXT;
ALTER TABLE core_training_cards ADD COLUMN session_label TEXT;
ALTER TABLE core_training_cards ADD COLUMN decision_intent TEXT;
ALTER TABLE core_training_cards ADD COLUMN title TEXT;
ALTER TABLE core_training_cards ADD COLUMN summary TEXT;
ALTER TABLE core_training_cards ADD COLUMN card_json TEXT NOT NULL DEFAULT '{}';
ALTER TABLE core_training_cards ADD COLUMN data_quality_json TEXT NOT NULL DEFAULT '{}';
ALTER TABLE core_training_cards ADD COLUMN source_context_json TEXT NOT NULL DEFAULT '{}';
ALTER TABLE core_training_cards ADD COLUMN user_feedback TEXT;
ALTER TABLE core_training_cards ADD COLUMN user_feedback_at TEXT;

UPDATE core_training_cards
SET source = COALESCE(source, 'core_board'),
    status = COALESCE(status, 'active'),
    updated_at = COALESCE(updated_at, created_at);

CREATE INDEX IF NOT EXISTS idx_core_training_cards_day_status_source
ON core_training_cards(day_iso, status, source);

CREATE INDEX IF NOT EXISTS idx_core_training_cards_updated
ON core_training_cards(updated_at DESC);
