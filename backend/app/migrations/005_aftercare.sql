CREATE TABLE approved_memory_v2(
 owner_id TEXT NOT NULL REFERENCES users(user_id),
 field TEXT NOT NULL CHECK(field IN ('origin','currency','climate_preferences','trip_styles','direct_flight_preferred','max_travel_minutes','pets')),
 value TEXT NOT NULL,source_trip_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
 consented_at TEXT NOT NULL,updated_at TEXT NOT NULL,PRIMARY KEY(owner_id,field)
);
INSERT INTO approved_memory_v2 SELECT * FROM approved_memory;
DROP TABLE approved_memory;
ALTER TABLE approved_memory_v2 RENAME TO approved_memory;
CREATE TRIGGER memory_source_owner_insert BEFORE INSERT ON approved_memory
 WHEN NOT EXISTS(SELECT 1 FROM session_owners o WHERE o.session_id=NEW.source_trip_id AND o.user_id=NEW.owner_id)
 BEGIN SELECT RAISE(ABORT,'memory_source_owner_mismatch'); END;
CREATE TRIGGER memory_source_owner_update BEFORE UPDATE ON approved_memory
 WHEN NOT EXISTS(SELECT 1 FROM session_owners o WHERE o.session_id=NEW.source_trip_id AND o.user_id=NEW.owner_id)
 BEGIN SELECT RAISE(ABORT,'memory_source_owner_mismatch'); END;
CREATE TABLE aftercare_manual(session_id TEXT PRIMARY KEY REFERENCES sessions(id) ON DELETE CASCADE,owner_id TEXT NOT NULL REFERENCES users(user_id),data TEXT NOT NULL,updated_at TEXT NOT NULL);
CREATE TABLE aftercare_drafts(id TEXT PRIMARY KEY,session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,owner_id TEXT NOT NULL REFERENCES users(user_id),kind TEXT NOT NULL CHECK(kind IN ('early_checkin','service_request')),text TEXT NOT NULL,status TEXT NOT NULL CHECK(status IN ('draft','archived')),created_at TEXT NOT NULL);
CREATE INDEX aftercare_draft_trip ON aftercare_drafts(session_id,created_at,id);
CREATE TABLE reminders(
 id TEXT PRIMARY KEY,owner_id TEXT NOT NULL REFERENCES users(user_id),session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
 bot_id TEXT NOT NULL,chat_id INTEGER NOT NULL,local_time TEXT NOT NULL,timezone TEXT NOT NULL,due_at TEXT NOT NULL,
 consent INTEGER NOT NULL CHECK(consent=1),text TEXT NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('scheduled','canceled','sending','sent','failed','delivery_unknown')),
 attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts BETWEEN 0 AND 3),next_attempt_at TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL
);
CREATE INDEX due_reminders ON reminders(bot_id,state,due_at,next_attempt_at);
