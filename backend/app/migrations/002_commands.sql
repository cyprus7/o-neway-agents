CREATE TABLE commands(
 command_id TEXT PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE,
 owner_id TEXT NOT NULL REFERENCES users(user_id),
 session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
 idempotency_key TEXT NOT NULL CHECK(length(idempotency_key) BETWEEN 8 AND 128),
 kind TEXT NOT NULL CHECK(kind IN ('message','prices_refresh')),
 payload_hash TEXT NOT NULL, expected_revision INTEGER NOT NULL CHECK(expected_revision>=0),
 status TEXT NOT NULL CHECK(status IN ('queued','running','completed','failed','superseded','canceled','interrupted')),
 created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT,
 UNIQUE(owner_id,idempotency_key)
);
CREATE INDEX command_queue ON commands(status,created_at,command_id);
CREATE INDEX command_owner_queue ON commands(owner_id,status);
CREATE UNIQUE INDEX one_running_command ON commands(status) WHERE status='running';
CREATE TRIGGER command_owner_insert BEFORE INSERT ON commands
 WHEN NOT EXISTS(SELECT 1 FROM session_owners o WHERE o.session_id=NEW.session_id AND o.user_id=NEW.owner_id)
 BEGIN SELECT RAISE(ABORT,'command_owner_mismatch'); END;
