CREATE TABLE users(
 user_id TEXT PRIMARY KEY,
 kind TEXT NOT NULL CHECK(kind IN ('guest','registered','legacy_unclaimed')),
 created_at TEXT NOT NULL, deleted_at TEXT
);
CREATE UNIQUE INDEX one_legacy_user ON users(kind) WHERE kind='legacy_unclaimed';
CREATE TABLE external_identities(
 issuer TEXT NOT NULL, subject TEXT NOT NULL, user_id TEXT NOT NULL REFERENCES users(user_id),
 created_at TEXT NOT NULL, PRIMARY KEY(issuer,subject)
);
CREATE INDEX external_identity_owner ON external_identities(user_id);
CREATE TABLE access_sessions(
 token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(user_id),
 csrf_hash TEXT NOT NULL, created_at TEXT NOT NULL, expires_at TEXT NOT NULL, revoked_at TEXT
);
CREATE INDEX access_session_owner ON access_sessions(user_id);
CREATE INDEX access_session_expiry ON access_sessions(expires_at);
CREATE TABLE session_owners(
 session_id TEXT PRIMARY KEY REFERENCES sessions(id) ON DELETE CASCADE,
 user_id TEXT NOT NULL REFERENCES users(user_id),
 parent_trip_id TEXT REFERENCES session_owners(session_id), updated_at TEXT NOT NULL
);
CREATE INDEX owned_session_page ON session_owners(user_id,updated_at DESC,session_id DESC);
CREATE TRIGGER same_owner_parent_insert BEFORE INSERT ON session_owners
 WHEN NEW.parent_trip_id IS NOT NULL AND (NEW.parent_trip_id=NEW.session_id OR NOT EXISTS(
 SELECT 1 FROM session_owners p WHERE p.session_id=NEW.parent_trip_id AND p.user_id=NEW.user_id))
 BEGIN SELECT RAISE(ABORT,'parent_owner_mismatch'); END;
CREATE TRIGGER same_owner_parent_update BEFORE UPDATE ON session_owners
 WHEN (NEW.parent_trip_id IS NOT NULL AND (NEW.parent_trip_id=NEW.session_id OR NOT EXISTS(
 SELECT 1 FROM session_owners p WHERE p.session_id=NEW.parent_trip_id AND p.user_id=NEW.user_id)))
 OR EXISTS(SELECT 1 FROM session_owners c WHERE c.parent_trip_id=OLD.session_id AND c.user_id!=NEW.user_id)
 BEGIN SELECT RAISE(ABORT,'parent_owner_mismatch'); END;
CREATE TRIGGER no_legacy_access BEFORE INSERT ON access_sessions
 WHEN EXISTS(SELECT 1 FROM users u WHERE u.user_id=NEW.user_id AND u.kind='legacy_unclaimed')
 BEGIN SELECT RAISE(ABORT,'legacy_login_forbidden'); END;
CREATE TRIGGER no_legacy_identity BEFORE INSERT ON external_identities
 WHEN EXISTS(SELECT 1 FROM users u WHERE u.user_id=NEW.user_id AND u.kind='legacy_unclaimed')
 BEGIN SELECT RAISE(ABORT,'legacy_login_forbidden'); END;
CREATE TABLE legacy_trip_assignments(
 assignment_id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id),
 from_user_id TEXT NOT NULL REFERENCES users(user_id), to_user_id TEXT NOT NULL REFERENCES users(user_id),
 assigned_at TEXT NOT NULL
);
