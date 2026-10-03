-- CORE Training schema (core.sqlite3)

CREATE TABLE IF NOT EXISTS core_profile (
  id INTEGER PRIMARY KEY CHECK (id=1),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  tone_style TEXT NOT NULL DEFAULT 'calm_direct',
  autonomy_level INTEGER NOT NULL DEFAULT 2,
  strictness_level INTEGER NOT NULL DEFAULT 2,
  reentry_style TEXT NOT NULL DEFAULT 'minimal',
  primary_goal TEXT NOT NULL DEFAULT 'hybrid',
  goal_weights_json TEXT NOT NULL DEFAULT '{}',
  notes TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS core_training_sessions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  mode TEXT NOT NULL,
  started_at TEXT NOT NULL,
  ended_at TEXT,
  cards_seen INTEGER NOT NULL DEFAULT 0,
  labels_given INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS core_training_cards (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  mode TEXT NOT NULL,
  card_type TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS core_training_labels (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id INTEGER NOT NULL,
  card_id INTEGER NOT NULL,
  label TEXT NOT NULL,
  confidence_user INTEGER,
  free_text TEXT,
  created_at TEXT NOT NULL,
  FOREIGN KEY(session_id) REFERENCES core_training_sessions(id),
  FOREIGN KEY(card_id) REFERENCES core_training_cards(id)
);

CREATE TABLE IF NOT EXISTS core_calibration (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  metric TEXT NOT NULL,
  value REAL NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS core_pref_stats (
  key TEXT PRIMARY KEY,
  value REAL NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_core_training_labels_session_id ON core_training_labels(session_id);
CREATE INDEX IF NOT EXISTS idx_core_training_labels_card_id ON core_training_labels(card_id);
CREATE INDEX IF NOT EXISTS idx_core_training_cards_mode_created_at ON core_training_cards(mode, created_at);
