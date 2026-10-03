-- Canonical endurance planning model. Runtime startup uses the same idempotent DDL.
-- Kept as an explicit migration for deployment tooling and schema review.
CREATE TABLE IF NOT EXISTS endurance_plans (
  id TEXT PRIMARY KEY, title TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active',
  revision INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS endurance_events (
  id TEXT PRIMARY KEY, plan_id TEXT NOT NULL, title TEXT, distance_m INTEGER, target_time_s INTEGER,
  event_date TEXT NOT NULL, priority TEXT NOT NULL DEFAULT 'A', created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  FOREIGN KEY(plan_id) REFERENCES endurance_plans(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS endurance_phases (
  id TEXT PRIMARY KEY, plan_id TEXT NOT NULL, name TEXT NOT NULL, phase_type TEXT NOT NULL,
  start_date TEXT NOT NULL, end_date TEXT NOT NULL, sort_order INTEGER NOT NULL DEFAULT 0,
  notes TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  FOREIGN KEY(plan_id) REFERENCES endurance_plans(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS endurance_weeks (
  id TEXT PRIMARY KEY, plan_id TEXT NOT NULL, phase_id TEXT, week_start TEXT NOT NULL,
  planned_load REAL, notes TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  UNIQUE(plan_id, week_start), FOREIGN KEY(plan_id) REFERENCES endurance_plans(id) ON DELETE CASCADE,
  FOREIGN KEY(phase_id) REFERENCES endurance_phases(id) ON DELETE SET NULL
);
CREATE TABLE IF NOT EXISTS endurance_sessions (
  id TEXT PRIMARY KEY, plan_id TEXT NOT NULL, phase_id TEXT, scheduled_date TEXT NOT NULL,
  session_type TEXT NOT NULL, title TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'planned',
  duration_s INTEGER, distance_m INTEGER, load_value REAL, tags_json TEXT NOT NULL DEFAULT '[]',
  notes TEXT, sync_state TEXT NOT NULL DEFAULT 'not_synced', actual_run_id INTEGER,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  FOREIGN KEY(plan_id) REFERENCES endurance_plans(id) ON DELETE CASCADE,
  FOREIGN KEY(phase_id) REFERENCES endurance_phases(id) ON DELETE SET NULL
);
CREATE TABLE IF NOT EXISTS endurance_steps (
  id TEXT PRIMARY KEY, session_id TEXT NOT NULL, parent_step_id TEXT, sort_order INTEGER NOT NULL,
  kind TEXT NOT NULL, duration_s INTEGER, distance_m INTEGER, reps INTEGER,
  target_json TEXT NOT NULL DEFAULT '{}', notes TEXT,
  FOREIGN KEY(session_id) REFERENCES endurance_sessions(id) ON DELETE CASCADE,
  FOREIGN KEY(parent_step_id) REFERENCES endurance_steps(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS endurance_fitness_anchors (
  id TEXT PRIMARY KEY, plan_id TEXT NOT NULL, anchor_date TEXT NOT NULL, source TEXT NOT NULL,
  five_k_time_s INTEGER, threshold_pace_s_per_km INTEGER, zones_json TEXT NOT NULL DEFAULT '{}',
  notes TEXT, created_at TEXT NOT NULL, FOREIGN KEY(plan_id) REFERENCES endurance_plans(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS endurance_resolutions (
  id TEXT PRIMARY KEY, session_id TEXT NOT NULL, step_id TEXT NOT NULL, fitness_anchor_id TEXT,
  resolved_at TEXT NOT NULL, resolved_target_json TEXT NOT NULL, UNIQUE(session_id, step_id),
  FOREIGN KEY(session_id) REFERENCES endurance_sessions(id) ON DELETE CASCADE,
  FOREIGN KEY(step_id) REFERENCES endurance_steps(id) ON DELETE CASCADE,
  FOREIGN KEY(fitness_anchor_id) REFERENCES endurance_fitness_anchors(id) ON DELETE SET NULL
);
CREATE TABLE IF NOT EXISTS endurance_plan_revisions (
  id TEXT PRIMARY KEY, plan_id TEXT NOT NULL, revision INTEGER NOT NULL, operation TEXT NOT NULL,
  summary TEXT NOT NULL, diff_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL,
  FOREIGN KEY(plan_id) REFERENCES endurance_plans(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS endurance_external_map (
  id TEXT PRIMARY KEY, session_id TEXT NOT NULL, provider TEXT NOT NULL, external_id TEXT,
  sync_state TEXT NOT NULL DEFAULT 'not_synced', last_error TEXT, synced_at TEXT,
  UNIQUE(session_id, provider), FOREIGN KEY(session_id) REFERENCES endurance_sessions(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_endurance_sessions_plan_date ON endurance_sessions(plan_id, scheduled_date);
CREATE INDEX IF NOT EXISTS idx_endurance_phases_plan_dates ON endurance_phases(plan_id, start_date, end_date);
CREATE INDEX IF NOT EXISTS idx_endurance_revisions_plan ON endurance_plan_revisions(plan_id, revision DESC);
