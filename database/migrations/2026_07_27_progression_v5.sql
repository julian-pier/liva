-- Deterministic progression V5: stable exercise identity, fixed set slots,
-- and explicit technical exclusions. Existing rows remain valid via the
-- engine's deterministic legacy fallbacks.
ALTER TABLE exercises ADD COLUMN canonical_exercise_id TEXT;
ALTER TABLE exercises ADD COLUMN variation_id TEXT;
ALTER TABLE exercises ADD COLUMN execution_mode TEXT NOT NULL DEFAULT '';

ALTER TABLE sets ADD COLUMN set_slot TEXT;
ALTER TABLE sets ADD COLUMN is_warmup INTEGER;
ALTER TABLE sets ADD COLUMN is_working_set INTEGER;
ALTER TABLE sets ADD COLUMN intentional_deload INTEGER NOT NULL DEFAULT 0;
ALTER TABLE sets ADD COLUMN technique_set INTEGER NOT NULL DEFAULT 0;

CREATE INDEX IF NOT EXISTS idx_sets_set_slot ON sets(exercise_id, set_slot);
