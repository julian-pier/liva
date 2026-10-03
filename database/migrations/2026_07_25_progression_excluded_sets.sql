-- One-off per-set progression exclusions. The raw workout remains unchanged;
-- flagged sets count as eligible volume but are omitted from comparison only.
ALTER TABLE sets ADD COLUMN progression_excluded INTEGER NOT NULL DEFAULT 0;
