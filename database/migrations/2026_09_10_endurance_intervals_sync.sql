-- Durable LIVA -> Intervals.icu outbox. Runtime startup also applies these
-- additions idempotently for installations without a separate migrator.
ALTER TABLE endurance_external_map ADD COLUMN external_event_id TEXT;
ALTER TABLE endurance_external_map ADD COLUMN last_synced_at TEXT;
ALTER TABLE endurance_external_map ADD COLUMN last_attempt_at TEXT;
ALTER TABLE endurance_external_map ADD COLUMN payload_hash TEXT;
ALTER TABLE endurance_external_map ADD COLUMN synced_revision INTEGER;

CREATE TABLE IF NOT EXISTS endurance_sync_jobs (
  id TEXT PRIMARY KEY,
  provider TEXT NOT NULL,
  session_id TEXT,
  external_id TEXT NOT NULL,
  operation TEXT NOT NULL CHECK(operation IN ('UPSERT','DELETE')),
  status TEXT NOT NULL DEFAULT 'pending',
  attempt_count INTEGER NOT NULL DEFAULT 0,
  next_attempt_at TEXT,
  last_error TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_endurance_sync_jobs_due
  ON endurance_sync_jobs(provider, status, next_attempt_at);
