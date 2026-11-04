ALTER TABLE users ADD COLUMN last_active_at TEXT;
UPDATE users SET last_active_at=MAX(created_at,COALESCE((SELECT MAX(o.updated_at) FROM session_owners o WHERE o.user_id=users.user_id),created_at),COALESCE((SELECT MAX(a.created_at) FROM access_sessions a WHERE a.user_id=users.user_id),created_at));
CREATE TABLE approved_memory(
 owner_id TEXT NOT NULL REFERENCES users(user_id),
 field TEXT NOT NULL CHECK(field IN ('origin','currency','climate_preferences','trip_styles','direct_flight_preferred','max_travel_minutes')),
 value TEXT NOT NULL, source_trip_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
 consented_at TEXT NOT NULL, updated_at TEXT NOT NULL, PRIMARY KEY(owner_id,field)
);
CREATE TRIGGER memory_source_owner_insert BEFORE INSERT ON approved_memory
 WHEN NOT EXISTS(SELECT 1 FROM session_owners o WHERE o.session_id=NEW.source_trip_id AND o.user_id=NEW.owner_id)
 BEGIN SELECT RAISE(ABORT,'memory_source_owner_mismatch'); END;
CREATE TRIGGER memory_source_owner_update BEFORE UPDATE ON approved_memory
 WHEN NOT EXISTS(SELECT 1 FROM session_owners o WHERE o.session_id=NEW.source_trip_id AND o.user_id=NEW.owner_id)
 BEGIN SELECT RAISE(ABORT,'memory_source_owner_mismatch'); END;
CREATE TABLE deletion_tombstones(scope TEXT NOT NULL CHECK(scope IN ('user','trip')),resource_id TEXT NOT NULL,owner_id TEXT NOT NULL,deleted_at TEXT NOT NULL,PRIMARY KEY(scope,resource_id));
CREATE TABLE retention_runs(name TEXT PRIMARY KEY,last_run_at TEXT NOT NULL);
CREATE TABLE metric_activity(name TEXT PRIMARY KEY,updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS metrics(name TEXT PRIMARY KEY,value REAL NOT NULL DEFAULT 0);
INSERT INTO metric_activity(name,updated_at) SELECT name,strftime('%Y-%m-%dT%H:%M:%f+00:00','now') FROM metrics;
