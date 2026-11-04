"""Small durable SQLite store; one atomic active job per session."""
import json
import base64
import secrets
from uuid import uuid4
import hashlib
import sqlite3
import threading
import time
import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from datetime import datetime, timedelta, timezone
from typing import Any

from .models import AgentEvent, Job, Message, Session, TripOption, utcnow
from .pricing.models import PriceObservation
from .domain.session_updates import SessionSnapshot
from .domain.identity import (Principal, User, LEGACY_USER_ID, AccessSessionCredentials, AccessSessionPrincipal, OwnedSessionPage)
from .migrations import migrate


logger = logging.getLogger(__name__)


class TransactionAborted(RuntimeError):
    """A nested operation failed and the complete unit of work rolled back."""


from .application.errors import BusySession
from .application.commands import CommandError, StaleCommand, active_command_id, TERMINAL_COMMAND_STATES


def pricing_snapshot_hash(session: Session, option_ids: list[str]) -> str:
    """Captured request identity, independent of budget and newly refined offers."""
    intent = session.intent.model_dump(mode="json", exclude={"budget_total", "assumptions"})
    options = []
    for option in session.options:
        if option.id in option_ids:
            options.append({"id": option.id, "destination": option.destination, "country": option.country,
                            "latitude": option.latitude, "longitude": option.longitude,
                            "stay_entity": option.stay.quote.scope.entity_id if option.stay.quote and option.stay.quote.scope else option.stay.details.get("property_id"),
                            "stay_url": option.stay.source_url})
    payload = json.dumps({"intent": intent, "options": options}, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


class Store:
    def __init__(self, path: str, on_change: Callable[[str], None] | None = None):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.lock = threading.RLock()
        self._transaction_depth = 0
        self._transaction_failed = False
        self._touched_sessions: set[str] = set()
        self._on_change = on_change
        with self.lock, self.db:
            self.db.executescript("""
                PRAGMA journal_mode=WAL;
                PRAGMA busy_timeout=5000;
                CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS session_update_versions(session_id TEXT PRIMARY KEY, sequence INTEGER NOT NULL CHECK(sequence >= 0));
                CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, session_id TEXT NOT NULL, status TEXT NOT NULL, data TEXT NOT NULL);
                CREATE UNIQUE INDEX IF NOT EXISTS one_active_job ON jobs(session_id) WHERE status IN ('queued','running');
                CREATE TABLE IF NOT EXISTS cache(key TEXT PRIMARY KEY, data TEXT NOT NULL, fetched_at REAL NOT NULL, expires_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS metrics(name TEXT PRIMARY KEY, value REAL NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS preferences(id TEXT PRIMARY KEY, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS checkouts(id TEXT PRIMARY KEY, session_id TEXT NOT NULL, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS first_recommendations(session_id TEXT PRIMARY KEY, seconds REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS quote_observations(id TEXT PRIMARY KEY, request_context_hash TEXT,
                    provider_id TEXT NOT NULL, observed_at TEXT, retain_until REAL NOT NULL, data TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS quote_observations_request ON quote_observations(request_context_hash,provider_id);
            """)
        try:
            with self.lock:
                migrate(self.db)
        except BaseException:
            self.db.close()
            raise

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Only the outer unit commits; failed nested work cannot leak updates.

        The lock covers the complete transaction, including synchronous nested
        repository methods. Wakeups are delivered after commit and after unlock.
        """
        changed: tuple[str, ...] = ()
        with self.lock:
            outer = self._transaction_depth == 0
            if outer:
                self.db.execute("BEGIN IMMEDIATE")
                self._transaction_failed = False
                self._touched_sessions = set()
            self._transaction_depth += 1
            try:
                yield
                if outer:
                    if self._transaction_failed:
                        raise TransactionAborted("Nested operation failed; transaction rolled back")
                    for session_id in sorted(self._touched_sessions):
                        self.db.execute("""INSERT INTO session_update_versions(session_id,sequence) SELECT id,1 FROM sessions WHERE id=?
                            ON CONFLICT(session_id) DO UPDATE SET sequence=sequence+1""", (session_id,))
                    self.db.commit()
                    changed = tuple(sorted(self._touched_sessions))
            except BaseException:
                self._transaction_failed = True
                if outer:
                    self.db.rollback()
                raise
            finally:
                self._transaction_depth -= 1
                if outer:
                    self._touched_sessions = set()
                    self._transaction_failed = False
        if self._on_change is not None:
            for session_id in changed:
                try:
                    self._on_change(session_id)
                except Exception:
                    # Durable state was committed. A failed process-local
                    # wakeup must not make callers retry a successful command.
                    logger.exception("Committed session notification failed")

    def _touch_session(self, session_id: str) -> None:
        if self._transaction_depth == 0:
            raise RuntimeError("Observable mutation requires a transaction")
        self._touched_sessions.add(session_id)

    def read_session_snapshot(self, session_id: str) -> SessionSnapshot | None:
        """A single SQL statement reads one committed version of all live state."""
        with self.lock:
            row = self.db.execute("""SELECT s.data AS session_data,
                    COALESCE(v.sequence,0) AS sequence, j.data AS job_data
                FROM sessions AS s
                LEFT JOIN session_update_versions AS v ON v.session_id=s.id
                LEFT JOIN jobs AS j ON j.id=(
                    SELECT id FROM jobs WHERE session_id=s.id
                    ORDER BY CASE WHEN status IN ('queued','running') THEN 0 ELSE 1 END,
                        rowid DESC LIMIT 1)
                WHERE s.id=?""", (session_id,)).fetchone()
        if row is None:
            return None
        job = Job.model_validate_json(row["job_data"]) if row["job_data"] else None
        return SessionSnapshot(sequence=row["sequence"], session=Session.model_validate_json(row["session_data"]),
                               job=job.model_dump(mode="json", exclude={"payload"}) if job else None)

    def close(self) -> None:
        self.db.close()

    def create_session(self, preference_id: str | None = None) -> Session:
        with self.transaction():
            preferences: dict[str, Any] = {}
            if preference_id:
                row = self.db.execute("SELECT data FROM preferences WHERE id=?", (preference_id,)).fetchone()
                if row:
                    preferences = json.loads(row[0])
            session = Session(preferences=preferences)
            # Saved preferences are visible suggestions; never silently reuse dates or a budget.
            self.save_session(session)
            self.increment("sessions_created")
        return session

    def get_session(self, session_id: str) -> Session | None:
        with self.lock:
            row = self.db.execute("SELECT data FROM sessions WHERE id=?", (session_id,)).fetchone()
        return Session.model_validate_json(row[0]) if row else None

    def save_session(self, session: Session) -> None:
        session.updated_at = utcnow()
        # Keep a useful bounded execution trace and conversation.
        session.events = session.events[-120:]
        with self.transaction():
            self._fence_command(session.id)
            self.db.execute("INSERT INTO sessions(id,data) VALUES (?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data", (session.id, session.model_dump_json()))
            # Legacy internal callers remain deliberately non-login. Public
            # creation uses create_owned_session and changes owner atomically.
            self.db.execute("INSERT INTO session_owners(session_id,user_id,updated_at) VALUES (?,?,?) ON CONFLICT(session_id) DO UPDATE SET updated_at=excluded.updated_at",
                            (session.id, LEGACY_USER_ID, session.updated_at))
            self._touch_session(session.id)
            owner = self.session_owner(session.id)
            if owner:
                self.db.execute("UPDATE users SET last_active_at=? WHERE user_id=? AND deleted_at IS NULL", (session.updated_at, owner))

    @staticmethod
    def _user(row) -> User | None:
        return User(row["user_id"], row["kind"], datetime.fromisoformat(row["created_at"])) if row else None

    def create_user(self, kind: str = "guest") -> User:
        if kind not in ("guest", "registered"):
            raise ValueError("invalid_user_kind")
        user_id, created_at = uuid4().hex, datetime.now(timezone.utc)
        with self.transaction():
            self.db.execute("INSERT INTO users(user_id,kind,created_at) VALUES (?,?,?)", (user_id, kind, created_at.isoformat()))
        return User(user_id, kind, created_at)

    def get_user(self, user_id: str) -> User | None:
        with self.lock:
            return self._user(self.db.execute("SELECT * FROM users WHERE user_id=? AND deleted_at IS NULL", (user_id,)).fetchone())

    def resolve_identity(self, issuer: str, subject: str) -> User:
        if not isinstance(issuer, str) or not isinstance(subject, str) or not issuer.strip() or not subject.strip() or len(issuer) > 200 or len(subject) > 300:
            raise ValueError("invalid_identity")
        with self.transaction():
            row = self.db.execute("SELECT u.* FROM external_identities e JOIN users u ON u.user_id=e.user_id WHERE e.issuer=? AND e.subject=?", (issuer, subject)).fetchone()
            if row:
                user = self._user(row)
                if row["deleted_at"] is not None:
                    raise KeyError("owner_not_found")
                if user.kind == "legacy_unclaimed":
                    raise ValueError("legacy_login_forbidden")
                return user
            user = self.create_user("registered")
            self.db.execute("INSERT INTO external_identities(issuer,subject,user_id,created_at) VALUES (?,?,?,?)", (issuer, subject, user.user_id, user.created_at.isoformat()))
            return user

    def identity_matches(self, user_id: str, issuer: str, subject: str) -> bool:
        """Check an existing live mapping without creating or merging identities."""
        with self.lock:
            return self.db.execute(
                "SELECT 1 FROM external_identities e JOIN users u ON u.user_id=e.user_id "
                "WHERE e.user_id=? AND e.issuer=? AND e.subject=? AND u.deleted_at IS NULL "
                "AND u.kind!='legacy_unclaimed'", (user_id, issuer, subject)).fetchone() is not None

    def session_owner(self, session_id: str) -> str | None:
        with self.lock:
            row = self.db.execute("SELECT user_id FROM session_owners WHERE session_id=?", (session_id,)).fetchone()
            return row[0] if row else None

    def get_owned_session(self, session_id: str, owner_id: str) -> Session | None:
        with self.lock:
            row = self.db.execute("SELECT s.data FROM sessions s JOIN session_owners o ON o.session_id=s.id WHERE s.id=? AND o.user_id=?", (session_id, owner_id)).fetchone()
            return Session.model_validate_json(row[0]) if row else None

    def create_owned_session(self, owner_id: str, preference_id: str | None = None, parent_trip_id: str | None = None) -> Session:
        with self.transaction():
            user = self.get_user(owner_id)
            if user is None or user.kind == "legacy_unclaimed":
                raise KeyError("owner_not_found")
            for related in (preference_id, parent_trip_id):
                if related is not None and self.session_owner(related) != owner_id:
                    raise KeyError("related_trip_not_found")
            session = self.create_session(preference_id)
            self.db.execute("UPDATE session_owners SET user_id=?,parent_trip_id=? WHERE session_id=?", (owner_id, parent_trip_id, session.id))
            return session

    def list_owned_sessions(self, owner_id: str, limit: int = 20, cursor: str | None = None) -> OwnedSessionPage:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50:
            raise ValueError("invalid_page_limit")
        params: list[Any] = [owner_id]
        clause = ""
        if cursor is not None:
            try:
                if not isinstance(cursor, str) or len(cursor) > 1000:
                    raise ValueError()
                values = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
                if not isinstance(values, list) or len(values) != 2 or any(not isinstance(v, str) for v in values):
                    raise ValueError()
                datetime.fromisoformat(values[0])
            except (ValueError, TypeError, UnicodeError):
                raise ValueError("invalid_page_cursor") from None
            clause = " AND (o.updated_at<? OR (o.updated_at=? AND o.session_id<?))"
            params.extend([values[0], values[0], values[1]])
        params.append(limit + 1)
        with self.lock:
            rows = self.db.execute("SELECT s.data,o.updated_at,o.session_id FROM session_owners o JOIN sessions s ON s.id=o.session_id WHERE o.user_id=?" + clause + " ORDER BY o.updated_at DESC,o.session_id DESC LIMIT ?", params).fetchall()
        next_cursor = None
        if len(rows) > limit:
            last = rows[limit - 1]
            next_cursor = base64.urlsafe_b64encode(json.dumps([last["updated_at"], last["session_id"]]).encode()).decode().rstrip("=")
        return OwnedSessionPage([Session.model_validate_json(row["data"]) for row in rows[:limit]], next_cursor)

    def create_access_session(self, user_id: str) -> AccessSessionCredentials:
        token, csrf_token = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(hours=12)
        with self.transaction():
            user = self.get_user(user_id)
            if user is None or user.kind == "legacy_unclaimed":
                raise KeyError("owner_not_found")
            self.db.execute("INSERT INTO access_sessions(token_hash,user_id,csrf_hash,created_at,expires_at) VALUES (?,?,?,?,?)",
                (hashlib.sha256(token.encode()).hexdigest(), user_id, hashlib.sha256(csrf_token.encode()).hexdigest(), now.isoformat(), expires_at.isoformat()))
        return AccessSessionCredentials(token, csrf_token, expires_at)

    def resolve_access_session(self, token: str) -> AccessSessionPrincipal | None:
        if not isinstance(token, str) or not 20 <= len(token) <= 512:
            return None
        with self.lock:
            row = self.db.execute("SELECT a.* FROM access_sessions a JOIN users u ON u.user_id=a.user_id WHERE a.token_hash=? AND a.revoked_at IS NULL AND a.expires_at>? AND u.kind!='legacy_unclaimed' AND u.deleted_at IS NULL",
                (hashlib.sha256(token.encode()).hexdigest(), datetime.now(timezone.utc).isoformat())).fetchone()
        if row is not None:
            self.touch_user_activity(row["user_id"])
        return AccessSessionPrincipal(Principal(row["user_id"], issuer="access_session"), row["csrf_hash"], datetime.fromisoformat(row["expires_at"])) if row else None

    def revoke_access_session(self, token: str) -> bool:
        if not isinstance(token, str) or not 20 <= len(token) <= 512:
            return False
        with self.transaction():
            result = self.db.execute("UPDATE access_sessions SET revoked_at=? WHERE token_hash=? AND revoked_at IS NULL",
                (datetime.now(timezone.utc).isoformat(), hashlib.sha256(token.encode()).hexdigest()))
            return bool(result.rowcount)

    def assign_legacy_trip(self, session_id: str, owner_id: str) -> None:
        """Operator-only helper. No HTTP claim surface may call this operation."""
        with self.transaction():
            if self.session_owner(session_id) != LEGACY_USER_ID:
                raise ValueError("trip_not_legacy_unclaimed")
            user = self.get_user(owner_id)
            if user is None or user.kind == "legacy_unclaimed":
                raise KeyError("owner_not_found")
            now = datetime.now(timezone.utc).isoformat()
            self.db.execute("UPDATE session_owners SET user_id=? WHERE session_id=?", (owner_id, session_id))
            self.db.execute("INSERT INTO legacy_trip_assignments VALUES (?,?,?,?,?)", (uuid4().hex, session_id, LEGACY_USER_ID, owner_id, now))

    def add_event(self, session_id: str, step: str, status: str, message: str, **details: Any) -> None:
        with self.transaction():
            session = self.get_session(session_id)
            if session:
                session.events.append(AgentEvent(step=step, status=status, message=message, details=details))
                self.save_session(session)

    def start_message(self, session_id: str, content: str) -> Job:
        with self.transaction():
            session = self.get_session(session_id)
            if not session:
                raise KeyError(session_id)
            job = Job(session_id=session_id, captured_revision=session.revision,
                      payload={"previous_intent": session.intent.model_dump(mode="json"),
                               "previous_options": [option.model_dump(mode="json") for option in session.options],
                               "previous_selected_option_id": session.selected_option_id})
            try:
                self.db.execute("INSERT INTO jobs VALUES (?,?,?,?)", (job.id, session_id, job.status, job.model_dump_json()))
            except sqlite3.IntegrityError as exc:
                active = self.active_job_record(session_id)
                raise BusySession(active.id if active else None) from exc
            session.messages.append(Message(role="user", content=content))
            # Keep the accepted plan readable. Active work blocks checkout;
            # dependency-aware planner invalidates only after validating action.
            session.clarification = None
            self.save_session(session)
        self.increment("messages_received")
        return job

    def active_job_record(self, session_id: str) -> Job | None:
        with self.lock:
            row = self.db.execute("SELECT data FROM jobs WHERE session_id=? AND status IN ('queued','running')", (session_id,)).fetchone()
        return Job.model_validate_json(row[0]) if row else None

    def start_refresh(self, session_id: str, expected_revision: int, option_id: str | None = None) -> tuple[Job, bool]:
        with self.transaction():
            session = self.get_session(session_id)
            if session is None:
                raise KeyError(session_id)
            if session.revision != expected_revision:
                raise ValueError("revision_mismatch")
            targets = [option.id for option in session.options if option_id is None or option.id == option_id]
            if not targets or len(targets) > 3:
                raise ValueError("no_current_options")
            current = self.active_job_record(session_id)
            if current:
                if current.kind == "prices_refresh" and current.captured_revision == expected_revision and current.payload.get("option_ids") == targets:
                    self.increment("pricing_dedup_hits")
                    return current, True
                raise BusySession(current.id)
            # Includes supplier retrieval, shared model queue/backoff and extraction.
            deadline = (datetime.now(timezone.utc) + timedelta(seconds=240)).isoformat()
            job = Job(session_id=session_id, kind="prices_refresh", captured_revision=session.revision, deadline_at=deadline,
                      target_option_ids=targets,
                      payload={"option_ids": targets, "request_hash": pricing_snapshot_hash(session, targets)},
                      progress="Проверка цен поставлена в очередь")
            try:
                self.db.execute("INSERT INTO jobs VALUES (?,?,?,?)", (job.id, session.id, job.status, job.model_dump_json()))
                self._touch_session(session.id)
            except sqlite3.IntegrityError as exc:
                current = self.active_job_record(session_id)
                raise BusySession(current.id if current else None) from exc
        self.increment("pricing_jobs")
        return job, False

    def apply_refresh(self, job: Job, options: list[TripOption], outcomes: list[dict[str, Any]]) -> bool:
        """Compare and apply a whole refresh batch in one SQLite transaction."""
        with self.transaction():
            self._fence_command(job.session_id)
            current_job = self.get_job(job.id)
            session = self.get_session(job.session_id)
            targets = job.payload.get("option_ids", [])
            if current_job is None or current_job.status not in ("queued", "running"):
                return False
            if job.deadline_at and datetime.now(timezone.utc) >= datetime.fromisoformat(job.deadline_at):
                job.status = "failed"
                job.error = "Срок проверки цен истек до применения результата. Повторите проверку."
                self.db.execute("UPDATE jobs SET status=?,data=? WHERE id=?", (job.status, job.model_dump_json(), job.id))
                self._sync_command_status(job)
                self._touch_session(job.session_id)
                return False
            valid = (session is not None
                     and session.revision == job.captured_revision
                     and pricing_snapshot_hash(session, targets) == job.payload.get("request_hash"))
            if not valid:
                job.status = "superseded"
                job.progress = "Условия поездки изменились; ответ старой проверки не применен"
                self.db.execute("UPDATE jobs SET status=?,data=? WHERE id=?", (job.status, job.model_dump_json(), job.id))
                self._sync_command_status(job)
                self._touch_session(job.session_id)
                return False
            replacements = {option.id: option for option in options}
            if set(replacements) != set(targets):
                raise ValueError("Incomplete refresh batch")
            session.options = [replacements.get(option.id, option) for option in session.options]
            session.revision += 1
            session.accepted_plan_revision = session.revision
            session.previous_plan = None
            result_ids = []
            for option in session.options:
                was_target = option.id in targets
                suffix = option.id.split("-", 1)[-1]
                option.id = f"r{session.revision}-{suffix}"
                if was_target:
                    result_ids.append(option.id)
            session.selected_option_id = None
            session.last_changes = ["цены и источники"]
            session.updated_at = utcnow()
            session.events.append(AgentEvent(step="prices", status="completed", message="Проверка цен завершена; суммы, ориентиры и причины отказов обновлены без подтверждения бронирования."))
            session.events = session.events[-120:]
            job.status, job.result_revision, job.outcomes = "completed", session.revision, outcomes
            job.result_option_ids = result_ids
            job.progress = "Проверка завершена"
            self.db.execute("UPDATE sessions SET data=? WHERE id=?", (session.model_dump_json(), session.id))
            self.db.execute("UPDATE jobs SET status=?,data=? WHERE id=?", (job.status, job.model_dump_json(), job.id))
            self._sync_command_status(job)
            self._touch_session(session.id)
        return True

    def save_observation(self, observation: PriceObservation, *, retention_seconds: int = 0) -> None:
        """History is opt-in per source; no generic seven-day quote cache."""
        if retention_seconds <= 0:
            return
        lifetime = min(retention_seconds, 7 * 86400)
        observed_time = observation.observed_at or observation.retrieved_at
        retain_until = observed_time.timestamp() + lifetime
        if retain_until <= time.time():
            return
        with self.transaction():
            self.db.execute("INSERT OR IGNORE INTO quote_observations VALUES (?,?,?,?,?,?)", (
                observation.id, observation.request_context_hash, observation.provider_id,
                observation.observed_at.isoformat() if observation.observed_at else None, retain_until, observation.model_dump_json()))

    def observations_for_request(self, request_hash: str, provider_id: str) -> list[PriceObservation]:
        with self.transaction():
            self.db.execute("DELETE FROM quote_observations WHERE retain_until<=?", (time.time(),))
            rows = self.db.execute("SELECT data FROM quote_observations WHERE request_context_hash=? AND provider_id=? ORDER BY observed_at,id", (request_hash, provider_id)).fetchall()
        return [PriceObservation.model_validate_json(row[0]) for row in rows]

    def get_job(self, job_id: str) -> Job | None:
        with self.lock:
            row = self.db.execute("SELECT data FROM jobs WHERE id=?", (job_id,)).fetchone()
        return Job.model_validate_json(row[0]) if row else None

    def save_job(self, job: Job) -> None:
        with self.transaction():
            active = active_command_id.get()
            if active is not None:
                row = self.db.execute("SELECT status FROM commands WHERE command_id=?", (active,)).fetchone()
                if row is None or row[0] in ('canceled','interrupted'):
                    return  # Planner cancellation handlers must propagate CancelledError.
                if active != job.id:
                    raise StaleCommand("command_identity_mismatch")
                self._fence_command(job.session_id)
            result = self.db.execute("UPDATE jobs SET status=?,data=? WHERE id=?", (job.status, job.model_dump_json(), job.id))
            self._sync_command_status(job)
            if result.rowcount:
                self._touch_session(job.session_id)

    def active_job(self, session_id: str) -> bool:
        with self.lock:
            return self.db.execute("SELECT 1 FROM jobs WHERE session_id=? AND status IN ('queued','running')", (session_id,)).fetchone() is not None

    def recover_interrupted(self) -> None:
        self.recover_commands()

    def _fence_command(self, session_id: str) -> None:
        if self.db.execute("SELECT 1 FROM deletion_tombstones WHERE scope='trip' AND resource_id=?", (session_id,)).fetchone():
            raise StaleCommand("trip_deleted")
        active = active_command_id.get()
        if active is None:
            return
        row = self.db.execute("SELECT c.status,c.session_id,c.owner_id FROM commands c JOIN session_owners o ON o.session_id=c.session_id AND o.user_id=c.owner_id JOIN users u ON u.user_id=c.owner_id AND u.deleted_at IS NULL WHERE c.command_id=?", (active,)).fetchone()
        if row is None or row["status"] in ("canceled", "interrupted") or row["session_id"] != session_id:
            raise StaleCommand("command_no_longer_active")

    def _sync_command_status(self, job: Job) -> None:
        finished = utcnow() if job.status in TERMINAL_COMMAND_STATES else None
        self.db.execute("UPDATE commands SET status=?,finished_at=COALESCE(?,finished_at) WHERE command_id=?", (job.status, finished, job.id))

    @staticmethod
    def _command_hash(session_id, kind, payload, expected_revision):
        return hashlib.sha256(json.dumps({"session_id": session_id, "kind": kind, "payload": payload,
            "expected_revision": expected_revision}, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()).hexdigest()

    def _admit_command(self, owner_id, session_id, kind, payload, key, expected_revision):
        if not isinstance(key, str) or not 8 <= len(key) <= 128 or not key.strip() or any(ord(c) < 32 for c in key):
            raise CommandError("invalid_idempotency_key", 422)
        if isinstance(expected_revision, bool) or not isinstance(expected_revision, int) or expected_revision < 0:
            raise CommandError("revision_required", 422)
        with self.transaction():
            session = self.get_owned_session(session_id, owner_id)
            user = self.get_user(owner_id)
            if session is None or user is None or user.kind == "legacy_unclaimed":
                raise CommandError("not_found", 404)
            payload_hash = self._command_hash(session_id, kind, payload, expected_revision)
            prior = self.db.execute("SELECT command_id,payload_hash FROM commands WHERE owner_id=? AND idempotency_key=?", (owner_id, key)).fetchone()
            if prior:
                if prior["payload_hash"] != payload_hash:
                    raise CommandError("idempotency_conflict", 409)
                job = self.get_job(prior["command_id"])
                if job is None:
                    raise CommandError("not_found", 404)
                self.increment("command_dedup_hits")
                return job, True
            if session.revision != expected_revision:
                raise CommandError("revision_mismatch", 409)
            if self.active_job(session_id):
                raise CommandError("session_busy", 409)
            if self.db.execute("SELECT COUNT(*) FROM commands WHERE owner_id=? AND status='queued'", (owner_id,)).fetchone()[0] >= 2:
                raise CommandError("user_queue_full", 429, retry_after=2)
            if self.db.execute("SELECT COUNT(*) FROM commands WHERE status='queued'").fetchone()[0] >= 32:
                raise CommandError("global_queue_full", 429, retry_after=2)
            if kind == "message":
                job = self.start_message(session_id, payload["content"])
                job.payload.update(action=payload.get("action"), option_ids=payload.get("option_ids", []))
                self.save_job(job)
            else:
                try:
                    job, _ = self.start_refresh(session_id, expected_revision, payload.get("option_id"))
                except ValueError as exc:
                    raise CommandError(str(exc), 409) from None
            self.db.execute("INSERT INTO commands(command_id,owner_id,session_id,idempotency_key,kind,payload_hash,expected_revision,status,created_at) VALUES (?,?,?,?,?,?,?,'queued',?)",
                (job.id, owner_id, session_id, key, kind, payload_hash, expected_revision, job.created_at))
            return job, False

    def admit_message(self, owner_id, session_id, content, key, expected_revision, *, action=None, option_ids=None):
        from .models import MessageInput
        value = MessageInput(content=content, action=action, option_ids=option_ids or [])
        return self._admit_command(owner_id, session_id, "message", {"content": value.content, "action": value.action,
            "option_ids": value.option_ids}, key, expected_revision)

    def admit_refresh(self, owner_id, session_id, body, key):
        return self._admit_command(owner_id, session_id, "prices_refresh", {"option_id": body.option_id}, key, body.expected_revision)

    def claim_next_command(self, last_owner=None) -> Job | None:
        with self.transaction():
            if self.db.execute("SELECT 1 FROM commands WHERE status='running'").fetchone():
                return None
            # Rotate away from the last owner if another owner has work, then
            # preserve FIFO for that owner's queued commands.
            row = self.db.execute("SELECT c.command_id FROM commands c JOIN session_owners o ON o.session_id=c.session_id AND o.user_id=c.owner_id JOIN users u ON u.user_id=c.owner_id AND u.deleted_at IS NULL WHERE c.status='queued' ORDER BY CASE WHEN c.owner_id>? THEN 0 ELSE 1 END,c.owner_id,c.created_at,c.command_id LIMIT 1", (last_owner or "",)).fetchone()
            if row is None:
                return None
            job = self.get_job(row[0])
            if job is None:
                return None
            now = utcnow()
            self.db.execute("UPDATE commands SET status='running',started_at=? WHERE command_id=? AND status='queued'", (now, job.id))
            job.status, job.started_at = "running", now
            self.db.execute("UPDATE jobs SET status=?,data=? WHERE id=?", (job.status, job.model_dump_json(), job.id))
            self._touch_session(job.session_id)
            return job

    def cancel_command(self, owner_id, job_id) -> Job:
        with self.transaction():
            row = self.db.execute("SELECT c.session_id FROM commands c JOIN session_owners o ON o.session_id=c.session_id AND o.user_id=c.owner_id WHERE c.command_id=? AND c.owner_id=?", (job_id, owner_id)).fetchone()
            if row is None or self.get_user(owner_id) is None:
                raise CommandError("not_found", 404)
            job = self.get_job(job_id)
            if job.status in ("queued", "running"):
                job.status, job.error = "canceled", "Запрос отменён пользователем."
                self.save_job(job)
            return job

    def fail_command(self, job_id, code="command_failed") -> None:
        with self.transaction():
            job = self.get_job(job_id)
            if job is not None and job.status in ("queued", "running"):
                job.status = "failed"
                job.error = "Не удалось завершить запрос; повторите его с новым ключом."
                self.save_job(job)

    def recover_commands(self) -> None:
        with self.transaction():
            # Only never-started persistent commands can resume. Legacy active
            # jobs have no trustworthy admission record and cannot be replayed.
            rows = self.db.execute("SELECT j.data,c.command_id,c.status AS command_status FROM jobs j LEFT JOIN commands c ON c.command_id=j.id WHERE j.status IN ('queued','running')").fetchall()
            for row in rows:
                if row["command_id"] is not None and row["command_status"] == "queued":
                    continue
                job = Job.model_validate_json(row["data"])
                job.status = "interrupted" if row["command_id"] is not None else "failed"
                retry = "Повторите проверку цен" if job.kind == "prices_refresh" else "Повторите сообщение"
                job.error = f"Приложение перезапущено. {retry} с новым ключом."
                self.save_job(job)
                self.add_event(job.session_id, "recovery", "warning", job.error)

    def cache_get(self, key: str, allow_stale: bool = False) -> tuple[Any, float, bool] | None:
        with self.lock:
            row = self.db.execute("SELECT data,fetched_at,expires_at FROM cache WHERE key=?", (key,)).fetchone()
        if not row or (row[2] < time.time() and not allow_stale):
            return None
        return json.loads(row[0]), row[1], row[2] < time.time()

    def cache_put(self, key: str, value: Any, ttl: int) -> None:
        now = time.time()
        with self.transaction():
            self.db.execute("INSERT OR REPLACE INTO cache VALUES (?,?,?,?)", (key, json.dumps(value, ensure_ascii=False), now, now + ttl))

    def increment(self, name: str, amount: float = 1) -> None:
        with self.transaction():
            self.db.execute("INSERT INTO metrics(name,value) VALUES (?,?) ON CONFLICT(name) DO UPDATE SET value=value+excluded.value", (name, amount))
            self.db.execute("INSERT INTO metric_activity(name,updated_at) VALUES (?,?) ON CONFLICT(name) DO UPDATE SET updated_at=excluded.updated_at", (name, utcnow()))

    def metrics(self) -> dict[str, Any]:
        with self.lock:
            values = {row[0]: row[1] for row in self.db.execute("SELECT name,value FROM metrics")}
            values["sessions"] = self.db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
            values["active_jobs"] = self.db.execute("SELECT COUNT(*) FROM jobs WHERE status IN ('queued','running')").fetchone()[0]
        completed = values.get("jobs_completed", 0)
        finished = completed + values.get("jobs_failed", 0)
        values["job_success_rate"] = round(completed / finished, 3) if finished else None
        with self.lock:
            values["scenarios_completed"] = self.db.execute("SELECT COUNT(DISTINCT session_id) FROM checkouts").fetchone()[0]
        values["scenario_completion_rate"] = round(values["scenarios_completed"] / values["sessions"], 3) if values["sessions"] else None
        values["completion_rate"] = values["scenario_completion_rate"]
        values.setdefault("itinerary_validation_errors", 0)
        count = values.get("recommendations_completed", 0)
        values["mean_recommendation_seconds"] = round(values.get("recommendation_seconds_total", 0) / count, 2) if count else None
        first_count = values.get("first_recommendations", 0)
        values["mean_first_recommendation_seconds"] = round(values.get("first_recommendation_seconds_total", 0) / first_count, 2) if first_count else None
        from .application.quality_metrics import product_funnel
        with self.lock:
            states = dict(self.db.execute("SELECT status,COUNT(*) FROM commands GROUP BY status"))
        values["product_funnel"] = product_funnel(values, states)
        return values

    def record_first_recommendation(self, session_id: str, seconds: float) -> None:
        with self.transaction():
            result = self.db.execute("INSERT OR IGNORE INTO first_recommendations VALUES (?,?)", (session_id, seconds))
            if result.rowcount:
                self.increment("first_recommendations")
                self.increment("first_recommendation_seconds_total", seconds)

    def save_preferences(self, session: Session) -> None:
        values = {key: getattr(session.intent, key) for key in ("origin", "currency", "trip_styles", "climate_preferences", "hotel_requirements", "pets", "direct_flight_preferred")}
        metadata = {key: session.preferences[key] for key in ("aftercare_amendment", "applied_memory_fields") if key in session.preferences}
        session.preferences = {**values, **metadata}
        with self.transaction():
            self._fence_command(session.id)
            self.db.execute("INSERT OR REPLACE INTO preferences VALUES (?,?)", (session.id, json.dumps(values, ensure_ascii=False)))

    def _memory_owner(self, owner_id):
        user = self.get_user(owner_id)
        if user is None or user.kind == "legacy_unclaimed":
            raise CommandError("not_found", 404)
        return user

    def touch_user_activity(self, owner_id: str, *, now=None) -> None:
        clock = now or datetime.now(timezone.utc)
        cutoff = (clock - timedelta(hours=1)).isoformat()
        with self.lock:
            row = self.db.execute("SELECT last_active_at FROM users WHERE user_id=? AND deleted_at IS NULL", (owner_id,)).fetchone()
            if row is None or row[0] is not None and row[0] > cutoff:
                return
        with self.transaction():
            self.db.execute("UPDATE users SET last_active_at=? WHERE user_id=? AND deleted_at IS NULL AND (last_active_at IS NULL OR last_active_at<=?)", (clock.isoformat(), owner_id, cutoff))

    def list_memory(self, owner_id: str) -> list[dict[str, Any]]:
        with self.lock:
            self._memory_owner(owner_id)
            rows = self.db.execute("SELECT field,value,source_trip_id,consented_at,updated_at FROM approved_memory WHERE owner_id=? ORDER BY field", (owner_id,)).fetchall()
            return [{**dict(row), "value": json.loads(row["value"])} for row in rows]

    def put_memory(self, owner_id: str, field: str, value, source_trip_id: str) -> dict[str, Any]:
        from .application.memory import validate_memory
        value = validate_memory(field, value)
        with self.transaction():
            self._memory_owner(owner_id)
            if self.session_owner(source_trip_id) != owner_id:
                raise CommandError("not_found", 404)
            now = utcnow()
            self.db.execute("INSERT INTO approved_memory(owner_id,field,value,source_trip_id,consented_at,updated_at) VALUES (?,?,?,?,?,?) ON CONFLICT(owner_id,field) DO UPDATE SET value=excluded.value,source_trip_id=excluded.source_trip_id,consented_at=excluded.consented_at,updated_at=excluded.updated_at",
                (owner_id, field, json.dumps(value, ensure_ascii=False), source_trip_id, now, now))
            self.touch_user_activity(owner_id)
            return next(item for item in self.list_memory(owner_id) if item["field"] == field)

    def forget_memory(self, owner_id: str, field: str) -> bool:
        from .application.memory import MEMORY_FIELDS
        if field not in MEMORY_FIELDS:
            raise ValueError("unsupported_memory_field")
        with self.transaction():
            self._memory_owner(owner_id)
            result = self.db.execute("DELETE FROM approved_memory WHERE owner_id=? AND field=?", (owner_id, field))
            return bool(result.rowcount)

    def apply_memory(self, owner_id: str, session: Session) -> list[str]:
        from .application.memory import fill_memory
        with self.lock:
            self._memory_owner(owner_id)
            if self.session_owner(session.id) != owner_id:
                raise CommandError("not_found", 404)
            return fill_memory(session, self.list_memory(owner_id))

    def export_owned_data(self, owner_id: str) -> dict[str, Any]:
        from .application.session_views import public_session_data
        with self.transaction():
            user = self._memory_owner(owner_id)
            sessions = self.db.execute("SELECT s.data FROM sessions s JOIN session_owners o ON o.session_id=s.id WHERE o.user_id=? ORDER BY o.updated_at DESC,o.session_id DESC", (owner_id,)).fetchall()
            receipts = self.db.execute("SELECT c.data FROM checkouts c JOIN session_owners o ON o.session_id=c.session_id WHERE o.user_id=? ORDER BY c.rowid", (owner_id,)).fetchall()
            return {"user": {"id": user.user_id, "kind": user.kind},
                "sessions": [public_session_data(Session.model_validate_json(row[0])) for row in sessions],
                "receipts": [json.loads(row[0]) for row in receipts], "memory": self.list_memory(owner_id),
                "drafts": [dict(r) for r in self.db.execute("SELECT id,session_id,kind,text,status,created_at FROM aftercare_drafts WHERE owner_id=?", (owner_id,))],
                "manual_contexts": [{"session_id": r[0], **json.loads(r[1])} for r in self.db.execute("SELECT session_id,data FROM aftercare_manual WHERE owner_id=?", (owner_id,))],
                "reminders": [{**dict(r), "consent": bool(r["consent"])} for r in self.db.execute("SELECT id,session_id,local_time,timezone,due_at,text,consent,state,attempts,created_at,updated_at FROM reminders WHERE owner_id=?", (owner_id,))]}

    def delete_owned_session(self, owner_id: str, session_id: str) -> list[str]:
        with self.transaction():
            self._memory_owner(owner_id)
            if self.session_owner(session_id) != owner_id:
                raise CommandError("not_found", 404)
            active = [row[0] for row in self.db.execute("SELECT id FROM jobs WHERE session_id=? AND status IN ('queued','running')", (session_id,))]
            for job_id in active:
                job = self.get_job(job_id)
                job.status, job.error = "canceled", "Поездка удалена."
                self.save_job(job)
            self.db.execute("INSERT OR IGNORE INTO deletion_tombstones VALUES ('trip',?,?,?)", (session_id, owner_id, utcnow()))
            self.db.execute("INSERT OR IGNORE INTO telegram_seen SELECT bot_id,update_id,? FROM telegram_inbox WHERE session_id=?", (utcnow(), session_id))
            self.db.execute("DELETE FROM telegram_inbox WHERE session_id=?", (session_id,))
            self.db.execute("UPDATE reminders SET state='canceled',updated_at=? WHERE session_id=? AND state='scheduled'", (utcnow(), session_id))
            # Parent deletion detaches surviving children without rewriting their
            # session JSON, source observations or original timestamps.
            self.db.execute("UPDATE session_owners SET parent_trip_id=NULL WHERE parent_trip_id=?", (session_id,))
            for table, column in (("jobs", "session_id"), ("checkouts", "session_id"), ("session_update_versions", "session_id"),
                                  ("first_recommendations", "session_id"), ("preferences", "id"), ("legacy_trip_assignments", "session_id")):
                self.db.execute(f"DELETE FROM {table} WHERE {column}=?", (session_id,))
            self.db.execute("DELETE FROM sessions WHERE id=?", (session_id,))
            self._touch_session(session_id)
            return active

    def delete_user_data(self, owner_id: str) -> list[str]:
        with self.transaction():
            self._memory_owner(owner_id)
            active = []
            sessions = [row[0] for row in self.db.execute("SELECT session_id FROM session_owners WHERE user_id=?", (owner_id,))]
            for session_id in sessions:
                active.extend(self.delete_owned_session(owner_id, session_id))
            now = utcnow()
            self.db.execute("INSERT OR IGNORE INTO deletion_tombstones VALUES ('user',?,?,?)", (owner_id, owner_id, now))
            self.db.execute("DELETE FROM approved_memory WHERE owner_id=?", (owner_id,))
            self.db.execute("INSERT OR IGNORE INTO telegram_seen SELECT bot_id,update_id,? FROM telegram_inbox WHERE owner_id=?", (now, owner_id))
            self.db.execute("DELETE FROM telegram_inbox WHERE owner_id=?", (owner_id,))
            self.db.execute("DELETE FROM telegram_chats WHERE owner_id=?", (owner_id,))
            self.db.execute("DELETE FROM access_sessions WHERE user_id=?", (owner_id,))
            self.db.execute("DELETE FROM external_identities WHERE user_id=?", (owner_id,))
            self.db.execute("UPDATE users SET deleted_at=? WHERE user_id=?", (now, owner_id))
            return active

    def cleanup_retention(self, *, now=None, limit: int = 100) -> dict[str, int]:
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("invalid_retention_limit")
        clock = now or datetime.now(timezone.utc)
        if clock.tzinfo is None:
            raise ValueError("retention_timezone_required")
        cutoff = (clock - timedelta(days=30)).isoformat()
        result = {"access_sessions": 0, "commands": 0, "trips": 0, "users": 0, "metrics": 0, "telegram_updates": 0}
        with self.transaction():
            prior = self.db.execute("SELECT last_run_at FROM retention_runs WHERE name='daily'").fetchone()
            if prior and datetime.fromisoformat(prior[0]).date() >= clock.date():
                return result
            remaining = limit
            tokens = [r[0] for r in self.db.execute("SELECT token_hash FROM access_sessions WHERE expires_at<=? OR revoked_at IS NOT NULL ORDER BY expires_at LIMIT ?", (clock.isoformat(), remaining))]
            self.db.executemany("DELETE FROM access_sessions WHERE token_hash=?", [(v,) for v in tokens])
            result["access_sessions"] = len(tokens)
            remaining -= len(tokens)
            commands = [r[0] for r in self.db.execute("SELECT command_id FROM commands WHERE status IN ('completed','failed','superseded','canceled','interrupted') AND finished_at<=? ORDER BY finished_at LIMIT ?", (cutoff, remaining))]
            self.db.executemany("DELETE FROM jobs WHERE id=?", [(v,) for v in commands])
            result["commands"] = len(commands)
            remaining -= len(commands)
            updates = list(self.db.execute("SELECT bot_id,update_id FROM telegram_inbox WHERE processed_at<=? ORDER BY processed_at LIMIT ?", (cutoff, remaining)))
            self.db.executemany("DELETE FROM telegram_inbox WHERE bot_id=? AND update_id=?", [(r[0], r[1]) for r in updates])
            remaining -= len(updates)
            seen = list(self.db.execute("SELECT bot_id,update_id FROM telegram_seen WHERE seen_at<=? ORDER BY seen_at LIMIT ?", (cutoff, remaining)))
            self.db.executemany("DELETE FROM telegram_seen WHERE bot_id=? AND update_id=?", [(r[0], r[1]) for r in seen])
            remaining -= len(seen)
            result["telegram_updates"] = len(updates) + len(seen)
            trips = list(self.db.execute("SELECT o.user_id,o.session_id FROM session_owners o JOIN users u ON u.user_id=o.user_id WHERE u.kind='guest' AND u.deleted_at IS NULL AND COALESCE(u.last_active_at,u.created_at)<=? ORDER BY o.updated_at,o.session_id LIMIT ?", (cutoff, remaining)))
            for row in trips:
                self.delete_owned_session(row[0], row[1])
            result["trips"] = len(trips)
            remaining -= len(trips)
            users = [r[0] for r in self.db.execute("SELECT user_id FROM users u WHERE kind='guest' AND deleted_at IS NULL AND COALESCE(last_active_at,created_at)<=? AND NOT EXISTS(SELECT 1 FROM session_owners o WHERE o.user_id=u.user_id) LIMIT ?", (cutoff, remaining))]
            for user_id in users:
                self.delete_user_data(user_id)
            result["users"] = len(users)
            remaining -= len(users)
            metrics = [r[0] for r in self.db.execute("SELECT name FROM metric_activity WHERE updated_at<=? ORDER BY updated_at LIMIT ?", ((clock - timedelta(days=90)).isoformat(), remaining))]
            self.db.executemany("DELETE FROM metrics WHERE name=?", [(v,) for v in metrics])
            self.db.executemany("DELETE FROM metric_activity WHERE name=?", [(v,) for v in metrics])
            result["metrics"] = len(metrics)
            self.db.execute("INSERT INTO retention_runs VALUES ('daily',?) ON CONFLICT(name) DO UPDATE SET last_run_at=excluded.last_run_at", (clock.isoformat(),))
        return result

    def telegram_update(self, bot_id, update_id):
        with self.lock:
            row = self.db.execute("SELECT * FROM telegram_inbox WHERE bot_id=? AND update_id=?", (bot_id, update_id)).fetchone()
            if row:
                return dict(row)
            return {"deleted": True} if self.db.execute("SELECT 1 FROM telegram_seen WHERE bot_id=? AND update_id=?", (bot_id, update_id)).fetchone() else None

    def telegram_inbox_size(self, bot_id):
        with self.lock:
            return self.db.execute("SELECT (SELECT COUNT(*) FROM telegram_inbox WHERE bot_id=?)+(SELECT COUNT(*) FROM telegram_seen WHERE bot_id=?)", (bot_id, bot_id)).fetchone()[0]

    def telegram_chat(self, bot_id, chat_id):
        with self.lock:
            row = self.db.execute("SELECT * FROM telegram_chats WHERE bot_id=? AND chat_id=?", (bot_id, chat_id)).fetchone()
            return dict(row) if row else None

    def set_telegram_chat(self, bot_id, chat_id, owner_id, session_id, update_id):
        with self.transaction():
            if session_id and self.session_owner(session_id) != owner_id:
                raise CommandError("not_found", 404)
            self.db.execute("INSERT INTO telegram_chats(bot_id,chat_id,owner_id,active_session_id,last_update_id) VALUES (?,?,?,?,?) ON CONFLICT(bot_id,chat_id) DO UPDATE SET owner_id=excluded.owner_id,active_session_id=excluded.active_session_id,last_update_id=excluded.last_update_id WHERE excluded.last_update_id>telegram_chats.last_update_id",
                (bot_id, chat_id, owner_id, session_id, update_id))
            self.touch_user_activity(owner_id)

    def record_telegram_update(self, bot_id, update_id, owner_id, chat_id, session_id, job_id, text, *, ignored=False):
        with self.transaction():
            self.db.execute("INSERT INTO telegram_inbox(bot_id,update_id,owner_id,chat_id,session_id,job_id,ack_text,processed_at,ack_state,result_state) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (bot_id, update_id, owner_id, chat_id, session_id, job_id, text, utcnow(), "ignored" if ignored else "pending", "waiting" if job_id else "not_applicable"))

    def telegram_poll_offset(self, bot_id):
        with self.lock:
            row = self.db.execute("SELECT next_update_id FROM telegram_poll_cursors WHERE bot_id=?", (bot_id,)).fetchone()
            return row[0] if row else 0

    def advance_telegram_poll(self, bot_id, update_id):
        with self.transaction():
            self.db.execute("INSERT INTO telegram_poll_cursors VALUES (?,?) ON CONFLICT(bot_id) DO UPDATE SET next_update_id=MAX(next_update_id,excluded.next_update_id)", (bot_id, update_id+1))

    def recover_telegram_deliveries(self, bot_id):
        with self.transaction():
            for field in ("ack_state", "result_state"):
                self.db.execute(f"UPDATE telegram_inbox SET {field}='delivery_unknown' WHERE bot_id=? AND {field}='sending'", (bot_id,))

    def claim_telegram_delivery(self, bot_id):
        with self.transaction():
            now = utcnow()
            row = self.db.execute("""SELECT i.*,j.status AS job_status FROM telegram_inbox i
                JOIN users u ON u.user_id=i.owner_id AND u.deleted_at IS NULL
                JOIN telegram_chats c ON c.bot_id=i.bot_id AND c.chat_id=i.chat_id AND c.owner_id=i.owner_id AND c.blocked=0
                LEFT JOIN jobs j ON j.id=i.job_id
                WHERE i.bot_id=? AND ((i.ack_state='pending' AND (i.ack_next_at IS NULL OR i.ack_next_at<=?))
                 OR (i.result_state='waiting' AND (i.result_next_at IS NULL OR i.result_next_at<=?) AND j.status IN ('completed','failed','superseded','canceled','interrupted')))
                ORDER BY i.processed_at,i.update_id LIMIT 1""", (bot_id, now, now)).fetchone()
            if row is None:
                return None
            data = dict(row)
            stage = "ack" if data["ack_state"] == "pending" and (data["ack_next_at"] is None or data["ack_next_at"] <= now) else "result"
            self.db.execute(f"UPDATE telegram_inbox SET {stage}_state='sending',{stage}_attempts={stage}_attempts+1 WHERE bot_id=? AND update_id=?", (bot_id, data["update_id"]))
            data["stage"] = stage
            return data

    def finish_telegram_delivery(self, bot_id, update_id, stage, outcome, retry_after=0):
        if stage not in ("ack", "result") or outcome not in ("sent", "blocked", "retryable", "delivery_unknown", "failed", "conflict"):
            raise ValueError("invalid_delivery_state")
        with self.transaction():
            row = self.db.execute("SELECT * FROM telegram_inbox WHERE bot_id=? AND update_id=?", (bot_id, update_id)).fetchone()
            if row is None:
                return
            state = outcome
            next_at = None
            if outcome == "retryable":
                state = ("pending" if stage == "ack" else "waiting") if row[stage + "_attempts"] < 3 else "failed"
                next_at = (datetime.now(timezone.utc)+timedelta(seconds=max(1,min(retry_after,300)))).isoformat()
            self.db.execute(f"UPDATE telegram_inbox SET {stage}_state=?,{stage}_next_at=? WHERE bot_id=? AND update_id=?", (state, next_at, bot_id, update_id))
            if outcome == "blocked":
                self.db.execute("UPDATE telegram_chats SET blocked=1 WHERE bot_id=? AND chat_id=?", (bot_id, row["chat_id"]))

    def find_owned_command(self, owner_id, key):
        with self.lock:
            row = self.db.execute("SELECT command_id FROM commands WHERE owner_id=? AND idempotency_key=?", (owner_id, key)).fetchone()
            return self.get_job(row[0]) if row else None

    def get_manual_context(self, owner_id, session_id):
        with self.lock:
            if self.get_owned_session(session_id, owner_id) is None:
                raise CommandError("not_found", 404)
            row = self.db.execute("SELECT data FROM aftercare_manual WHERE session_id=? AND owner_id=?", (session_id, owner_id)).fetchone()
            return json.loads(row[0]) if row else None

    def put_manual_context(self, owner_id, session_id, body):
        with self.transaction():
            if self.get_owned_session(session_id, owner_id) is None:
                raise CommandError("not_found", 404)
            data = {**body.model_dump(mode="json"), "source": "user_provided_unverified", "updated_at": utcnow()}
            self.db.execute("INSERT INTO aftercare_manual VALUES (?,?,?,?) ON CONFLICT(session_id) DO UPDATE SET data=excluded.data,updated_at=excluded.updated_at", (session_id, owner_id, json.dumps(data, ensure_ascii=False), data["updated_at"]))
            return data

    def list_aftercare_drafts(self, owner_id, session_id):
        with self.lock:
            if self.get_owned_session(session_id, owner_id) is None:
                raise CommandError("not_found", 404)
            return [dict(r) for r in self.db.execute("SELECT id,kind,text,status,created_at FROM aftercare_drafts WHERE owner_id=? AND session_id=? ORDER BY created_at DESC,id DESC LIMIT 100", (owner_id, session_id))]

    def create_aftercare_draft(self, owner_id, session_id, body):
        with self.transaction():
            if not self.list_owned_receipts(owner_id, session_id) and not self.get_manual_context(owner_id, session_id):
                raise CommandError("manual_context_required", 409)
            if self.db.execute("SELECT COUNT(*) FROM aftercare_drafts WHERE session_id=?", (session_id,)).fetchone()[0] >= 100:
                raise CommandError("draft_limit", 429)
            item = {"id": uuid4().hex, "kind": body.kind, "text": body.text, "status": "draft", "created_at": utcnow()}
            self.db.execute("INSERT INTO aftercare_drafts VALUES (?,?,?,?,?,?,?)", (item["id"], session_id, owner_id, body.kind, body.text, "draft", item["created_at"]))
            return item

    def archive_aftercare_draft(self, owner_id, session_id, draft_id):
        with self.transaction():
            if self.get_owned_session(session_id, owner_id) is None:
                raise CommandError("not_found", 404)
            row = self.db.execute("SELECT id FROM aftercare_drafts WHERE id=? AND session_id=? AND owner_id=?", (draft_id, session_id, owner_id)).fetchone()
            if row is None:
                raise CommandError("not_found", 404)
            self.db.execute("UPDATE aftercare_drafts SET status='archived' WHERE id=?", (draft_id,))
            return next(item for item in self.list_aftercare_drafts(owner_id, session_id) if item["id"] == draft_id)

    def telegram_private_chat(self, bot_id, owner_id):
        with self.lock:
            row = self.db.execute("""SELECT c.* FROM telegram_chats c JOIN users u ON u.user_id=c.owner_id AND u.deleted_at IS NULL
                JOIN session_owners o ON o.session_id=c.active_session_id AND o.user_id=c.owner_id
                WHERE c.bot_id=? AND c.owner_id=? AND c.chat_id>0 AND c.blocked=0 ORDER BY c.last_update_id DESC LIMIT 1""", (bot_id, owner_id)).fetchone()
            return dict(row) if row else None

    def list_reminders(self, owner_id, session_id):
        with self.lock:
            if self.get_owned_session(session_id, owner_id) is None:
                raise CommandError("not_found", 404)
            items = [dict(r) for r in self.db.execute("SELECT id,session_id,local_time,timezone,due_at,consent,text,state,attempts,created_at,updated_at FROM reminders WHERE owner_id=? AND session_id=? ORDER BY created_at DESC,id DESC LIMIT 100", (owner_id, session_id))]
            for item in items:
                item["consent"] = bool(item["consent"])
            return items

    def create_reminder(self, owner_id, session_id, body, due_at, bot_id):
        with self.transaction():
            if self.get_owned_session(session_id, owner_id) is None:
                raise CommandError("not_found", 404)
            chat = self.telegram_private_chat(bot_id, owner_id)
            if chat is None:
                raise CommandError("reminders_unavailable", 409)
            if self.db.execute("SELECT COUNT(*) FROM reminders WHERE owner_id=?", (owner_id,)).fetchone()[0] >= 100 or self.db.execute("SELECT COUNT(*) FROM reminders").fetchone()[0] >= 1024:
                raise CommandError("reminder_limit", 429)
            reminder_id, stamp = uuid4().hex, utcnow()
            self.db.execute("INSERT INTO reminders(id,owner_id,session_id,bot_id,chat_id,local_time,timezone,due_at,consent,text,state,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,1,?,'scheduled',?,?)",
                (reminder_id, owner_id, session_id, bot_id, chat["chat_id"], body.local_time.isoformat(), body.timezone, due_at.isoformat(), body.text, stamp, stamp))
            return next(item for item in self.list_reminders(owner_id, session_id) if item["id"] == reminder_id)

    def cancel_reminder(self, owner_id, session_id, reminder_id):
        with self.transaction():
            if self.get_owned_session(session_id, owner_id) is None:
                raise CommandError("not_found", 404)
            row = self.db.execute("SELECT state FROM reminders WHERE id=? AND owner_id=? AND session_id=?", (reminder_id, owner_id, session_id)).fetchone()
            if row is None:
                raise CommandError("not_found", 404)
            if row[0] in ("scheduled", "sending"):
                # In-flight delivery cannot honestly be promised canceled.
                state = "delivery_unknown" if row[0] == "sending" else "canceled"
                self.db.execute("UPDATE reminders SET state=?,updated_at=? WHERE id=?", (state, utcnow(), reminder_id))
            return next(item for item in self.list_reminders(owner_id, session_id) if item["id"] == reminder_id)

    def recover_reminders(self, bot_id):
        with self.transaction():
            self.db.execute("UPDATE reminders SET state='delivery_unknown',updated_at=? WHERE bot_id=? AND state='sending'", (utcnow(), bot_id))

    def claim_due_reminder(self, bot_id, now):
        with self.transaction():
            # Bounded invalidation; removed links/blocked chats never dispatch.
            invalid = list(self.db.execute("""SELECT r.id FROM reminders r WHERE r.bot_id=? AND r.state='scheduled'
                AND NOT EXISTS(SELECT 1 FROM users u JOIN session_owners o ON o.user_id=u.user_id
                JOIN telegram_chats c ON c.owner_id=u.user_id
                JOIN session_owners a ON a.session_id=c.active_session_id AND a.user_id=u.user_id
                WHERE u.user_id=r.owner_id AND u.deleted_at IS NULL AND o.session_id=r.session_id
                  AND c.bot_id=r.bot_id AND c.chat_id=r.chat_id AND c.blocked=0) LIMIT 100""", (bot_id,)))
            self.db.executemany("UPDATE reminders SET state='canceled',updated_at=? WHERE id=?", [(now.isoformat(), row[0]) for row in invalid])
            row = self.db.execute("SELECT * FROM reminders WHERE bot_id=? AND state='scheduled' AND attempts<3 AND due_at<=? AND (next_attempt_at IS NULL OR next_attempt_at<=?) ORDER BY due_at,id LIMIT 1", (bot_id, now.isoformat(), now.isoformat())).fetchone()
            if row is None:
                return None
            self.db.execute("UPDATE reminders SET state='sending',attempts=attempts+1,updated_at=? WHERE id=? AND state='scheduled'", (now.isoformat(), row["id"]))
            return dict(row)

    def reminder_can_send(self, reminder_id):
        with self.lock:
            row = self.db.execute("SELECT * FROM reminders WHERE id=? AND state='sending'", (reminder_id,)).fetchone()
            if row is None or self.session_owner(row["session_id"]) != row["owner_id"] or self.get_user(row["owner_id"]) is None:
                return False
            chat = self.telegram_private_chat(row["bot_id"], row["owner_id"])
            return chat is not None and chat["chat_id"] == row["chat_id"]

    def finish_reminder(self, reminder_id, outcome, *, now, retry_after=0):
        if outcome not in ("sent", "failed", "blocked", "conflict", "retryable", "delivery_unknown", "canceled"):
            raise ValueError("invalid_reminder_delivery")
        with self.transaction():
            row = self.db.execute("SELECT * FROM reminders WHERE id=? AND state='sending'", (reminder_id,)).fetchone()
            if row is None:
                return
            next_at = None
            state = outcome if outcome in ("sent", "delivery_unknown", "canceled") else "failed"
            if outcome == "retryable" and row["attempts"] < 3:
                state = "scheduled"
                next_at = (now+timedelta(seconds=max(1,min(retry_after,300)))).isoformat()
            if outcome == "blocked":
                self.db.execute("UPDATE telegram_chats SET blocked=1 WHERE bot_id=? AND chat_id=?", (row["bot_id"], row["chat_id"]))
            self.db.execute("UPDATE reminders SET state=?,next_attempt_at=?,updated_at=? WHERE id=? AND state='sending'", (state, next_at, now.isoformat(), reminder_id))

    def list_owned_receipts(self, owner_id: str, session_id: str) -> list[dict[str, Any]]:
        with self.lock:
            if self.get_owned_session(session_id, owner_id) is None:
                raise CommandError("not_found", 404)
            rows = self.db.execute("SELECT data FROM checkouts WHERE session_id=? ORDER BY rowid DESC LIMIT 100", (session_id,)).fetchall()
            return [json.loads(row[0]) for row in rows]

    def save_checkout(self, checkout: dict[str, Any]) -> None:
        with self.transaction():
            self.db.execute("INSERT INTO checkouts VALUES (?,?,?)", (checkout["id"], checkout["session_id"], json.dumps(checkout, ensure_ascii=False)))

    def find_checkout(self, session_id: str, revision: int, option_id: str) -> dict[str, Any] | None:
        with self.lock:
            rows = self.db.execute("SELECT data FROM checkouts WHERE session_id=? ORDER BY rowid DESC", (session_id,)).fetchall()
        for row in rows:
            checkout = json.loads(row[0])
            if checkout.get("revision") == revision and checkout.get("option_id") == option_id:
                return checkout
        return None
