CREATE TABLE telegram_chats(
 bot_id TEXT NOT NULL, chat_id INTEGER NOT NULL, owner_id TEXT NOT NULL REFERENCES users(user_id),
 active_session_id TEXT REFERENCES sessions(id) ON DELETE SET NULL,
 last_update_id INTEGER NOT NULL, blocked INTEGER NOT NULL DEFAULT 0,
 PRIMARY KEY(bot_id,chat_id)
);
CREATE TABLE telegram_inbox(
 bot_id TEXT NOT NULL, update_id INTEGER NOT NULL, owner_id TEXT NOT NULL REFERENCES users(user_id),
 chat_id INTEGER NOT NULL, session_id TEXT REFERENCES sessions(id) ON DELETE SET NULL,
 job_id TEXT REFERENCES jobs(id) ON DELETE SET NULL,
 ack_text TEXT NOT NULL, processed_at TEXT NOT NULL,
 ack_state TEXT NOT NULL DEFAULT 'pending', result_state TEXT NOT NULL DEFAULT 'waiting',
 ack_attempts INTEGER NOT NULL DEFAULT 0, result_attempts INTEGER NOT NULL DEFAULT 0,
 ack_next_at TEXT, result_next_at TEXT,
 PRIMARY KEY(bot_id,update_id)
);
CREATE INDEX telegram_delivery ON telegram_inbox(bot_id,ack_state,result_state,processed_at);
CREATE TABLE telegram_poll_cursors(bot_id TEXT PRIMARY KEY,next_update_id INTEGER NOT NULL DEFAULT 0);
CREATE TABLE telegram_seen(bot_id TEXT NOT NULL,update_id INTEGER NOT NULL,seen_at TEXT NOT NULL,PRIMARY KEY(bot_id,update_id));
