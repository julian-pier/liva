-- Remove the retired RemoteXPC/Syslog and snapshot prototype model.
-- A verified external backup is required before applying this migration.
PRAGMA foreign_keys = ON;
BEGIN IMMEDIATE;

DROP TABLE IF EXISTS iphone_app_sessions;
DROP TABLE IF EXISTS iphone_screen_sessions;
DROP TABLE IF EXISTS iphone_telemetry_gaps;
DROP TABLE IF EXISTS iphone_watcher_health;
DROP TABLE IF EXISTS iphone_current_state;
DROP TABLE IF EXISTS iphone_app_catalog;
DROP TABLE IF EXISTS iphone_state_events;
DROP TABLE IF EXISTS iphone_shortcut_events;

DROP TABLE IF EXISTS iphone_current_activity;
DROP TABLE IF EXISTS iphone_app_totals;
DROP TABLE IF EXISTS iphone_domain_totals;
DROP TABLE IF EXISTS iphone_sync_log;
DROP TABLE IF EXISTS iphone_activity_snapshots;

-- iphone_devices is shared with the new StayFree daily model. Keep only devices
-- referenced by retained aggregate data.
DELETE FROM iphone_devices
WHERE NOT EXISTS (
    SELECT 1 FROM iphone_daily_usage daily WHERE daily.device_id = iphone_devices.id
);

COMMIT;
