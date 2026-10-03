-- LIVA Activity Phase 2: completed StayFree iOS usage days only.
-- Runtime initialization is mirrored by activity/iphone_daily.py.

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS iphone_daily_usage (
    id TEXT PRIMARY KEY,
    device_id TEXT NOT NULL REFERENCES iphone_devices(id) ON DELETE CASCADE,
    usage_day TEXT NOT NULL,
    day_start TEXT NOT NULL,
    day_end TEXT NOT NULL,
    total_usage_seconds INTEGER NOT NULL CHECK(total_usage_seconds >= 0),
    source TEXT NOT NULL CHECK(source = 'stayfree_ios'),
    source_updated_at TEXT,
    imported_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(device_id, usage_day, source)
);

CREATE INDEX IF NOT EXISTS idx_iphone_daily_usage_day
    ON iphone_daily_usage(usage_day, device_id);

CREATE TABLE IF NOT EXISTS iphone_daily_apps (
    id TEXT PRIMARY KEY,
    daily_usage_id TEXT NOT NULL REFERENCES iphone_daily_usage(id) ON DELETE CASCADE,
    device_id TEXT NOT NULL REFERENCES iphone_devices(id) ON DELETE CASCADE,
    usage_day TEXT NOT NULL,
    app_key TEXT,
    app_identity TEXT NOT NULL,
    app_name TEXT NOT NULL,
    duration_seconds INTEGER NOT NULL CHECK(duration_seconds >= 0),
    category TEXT,
    source TEXT NOT NULL CHECK(source = 'stayfree_ios'),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(daily_usage_id, app_identity)
);

CREATE INDEX IF NOT EXISTS idx_iphone_daily_apps_day
    ON iphone_daily_apps(usage_day, device_id, duration_seconds DESC);

CREATE TABLE IF NOT EXISTS iphone_import_runs (
    id TEXT PRIMARY KEY,
    device_id TEXT NOT NULL REFERENCES iphone_devices(id) ON DELETE CASCADE,
    usage_day TEXT NOT NULL,
    status TEXT NOT NULL,
    source TEXT NOT NULL CHECK(source = 'stayfree_ios'),
    started_at TEXT NOT NULL,
    finished_at TEXT NOT NULL,
    records_received INTEGER NOT NULL DEFAULT 0 CHECK(records_received >= 0),
    total_usage_seconds INTEGER NOT NULL DEFAULT 0 CHECK(total_usage_seconds >= 0),
    error_code TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_iphone_import_runs_day
    ON iphone_import_runs(usage_day, device_id, created_at DESC);
