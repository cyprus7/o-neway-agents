"""Additive identity upgrade and isolated operator assignment, using temporary DBs."""
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from backend.app.domain.identity import LEGACY_USER_ID, Principal
from backend.app.migrations import migrate
from backend.app.models import Session
from backend.app.storage import Store


def test_additive_upgrade_preserves_raw_session_receipt_and_cursor_bytes(tmp_path):
    path = tmp_path / "legacy.db"
    session = Session()
    raw = json.dumps(session.model_dump(mode="json"), indent=3, ensure_ascii=False)
    receipt = '{ "id" : "receipt", "fictional":true, "source_time":"2026-01-01" }'
    db = sqlite3.connect(path)
    db.executescript("CREATE TABLE sessions(id TEXT PRIMARY KEY,data TEXT NOT NULL); CREATE TABLE checkouts(id TEXT PRIMARY KEY,session_id TEXT NOT NULL,data TEXT NOT NULL); CREATE TABLE session_update_versions(session_id TEXT PRIMARY KEY,sequence INTEGER NOT NULL);")
    db.execute("INSERT INTO sessions VALUES (?,?)", (session.id, raw))
    db.execute("INSERT INTO checkouts VALUES (?,?,?)", ("receipt", session.id, receipt))
    db.execute("INSERT INTO session_update_versions VALUES (?,?)", (session.id, 43))
    db.commit()
    db.close()
    for _ in range(2):
        store = Store(str(path))
        assert store.session_owner(session.id) == LEGACY_USER_ID
        assert store.db.execute("SELECT data FROM sessions").fetchone()[0] == raw
        assert store.db.execute("SELECT data FROM checkouts").fetchone()[0] == receipt
        assert store.db.execute("SELECT sequence FROM session_update_versions").fetchone()[0] == 43
        assert store.db.execute("SELECT updated_at FROM session_owners").fetchone()[0] == session.updated_at
        assert {r[0] for r in store.db.execute("SELECT version FROM schema_migrations")} == {1, 2, 3, 4, 5}
        assert store.db.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert store.list_owned_sessions(store.create_user().user_id).items == []
        with pytest.raises(KeyError): store.create_access_session(LEGACY_USER_ID)
        store.close()


def test_migration_failure_rolls_back_all_identity_ddl_then_retry(tmp_path, monkeypatch):
    db = sqlite3.connect(tmp_path / "upgrade.db")
    db.execute("CREATE TABLE sessions(id TEXT PRIMARY KEY,data TEXT NOT NULL)")
    db.commit()
    read = Path.read_text
    def broken(path, *args, **kwargs):
        value = read(path, *args, **kwargs)
        return value + "THIS IS INVALID SQL;\n" if path.name == "001_identity.sql" else value
    with monkeypatch.context() as patch:
        patch.setattr(Path, "read_text", broken)
        with pytest.raises(sqlite3.OperationalError): migrate(db)
    assert not db.in_transaction
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert tables == {"sessions"}
    migrate(db)
    migrate(db)
    assert {r[0] for r in db.execute("SELECT version FROM schema_migrations")} == {1, 2, 3, 4, 5}
    db.close()


def test_owned_create_preference_parent_and_save_preserve_identity(tmp_path):
    store = Store(str(tmp_path / "db.sqlite"))
    a, b = store.create_user(), store.create_user()
    parent = store.create_owned_session(a.user_id)
    parent.intent.origin = "Larnaca"
    store.save_preferences(parent)
    child = store.create_owned_session(a.user_id, preference_id=parent.id, parent_trip_id=parent.id)
    assert child.preferences["origin"] == "Larnaca"
    before = store.session_owner(child.id)
    store.save_session(child)
    assert store.session_owner(child.id) == before == a.user_id
    assert store.get_owned_session(child.id, b.user_id) is None
    assert store.get_owned_session(child.id, a.user_id).id == child.id
    assert store.db.execute("SELECT updated_at FROM session_owners WHERE session_id=?", (child.id,)).fetchone()[0] == child.updated_at
    for kwargs in ({"parent_trip_id": parent.id}, {"preference_id": parent.id}):
        with pytest.raises(KeyError): store.create_owned_session(b.user_id, **kwargs)
    with pytest.raises(sqlite3.IntegrityError):
        with store.transaction():
            store.db.execute("UPDATE session_owners SET user_id=? WHERE session_id=?", (b.user_id, parent.id))
    with pytest.raises(sqlite3.IntegrityError):
        with store.transaction():
            store.db.execute("UPDATE session_owners SET user_id=? WHERE session_id=?", ("missing", child.id))
    assert store.session_owner(parent.id) == a.user_id
    store.close()


def test_owned_keyset_pagination_filters_owner_and_validates_cursor(tmp_path):
    store = Store(str(tmp_path / "db.sqlite"))
    a, b = store.create_user(), store.create_user()
    own = [store.create_owned_session(a.user_id).id for _ in range(5)]
    store.create_owned_session(b.user_id)
    seen, cursor = [], None
    while True:
        page = store.list_owned_sessions(a.user_id, limit=2, cursor=cursor)
        seen.extend(s.id for s in page.items)
        cursor = page.next_cursor
        if cursor is None: break
    assert len(seen) == len(set(seen)) == 5 and set(seen) == set(own)
    assert len(store.list_owned_sessions(b.user_id, cursor=store.list_owned_sessions(a.user_id, 1).next_cursor).items) <= 1
    for limit in (0, 51, True, "20"):
        with pytest.raises(ValueError): store.list_owned_sessions(a.user_id, limit=limit)
    for cursor in ("!", "eyJ4IjoxfQ", "x" * 1001):
        with pytest.raises(ValueError): store.list_owned_sessions(a.user_id, cursor=cursor)
    store.close()


def test_external_identity_is_unique_no_guest_merge_and_principal_immutable(tmp_path):
    store = Store(str(tmp_path / "db.sqlite"))
    guest = store.create_user()
    user = store.resolve_identity("telegram:123", "456")
    assert user.kind == "registered" and user.user_id != guest.user_id
    assert store.resolve_identity("telegram:123", "456") == user
    assert store.resolve_identity("telegram:124", "456").user_id != user.user_id
    with pytest.raises(FrozenInstanceError): Principal(user.user_id).user_id = "tampered"
    store.close()


def test_access_sessions_store_only_hashes_expire_revoke_and_forbid_legacy(tmp_path):
    store = Store(str(tmp_path / "db.sqlite"))
    user = store.create_user()
    credentials = store.create_access_session(user.user_id)
    raw = dict(store.db.execute("SELECT * FROM access_sessions").fetchone())
    assert credentials.token not in str(raw) and credentials.csrf_token not in str(raw)
    assert raw["token_hash"] == hashlib.sha256(credentials.token.encode()).hexdigest()
    resolved = store.resolve_access_session(credentials.token)
    assert resolved.principal.user_id == user.user_id and resolved.principal.issuer == "access_session"
    assert resolved.csrf_hash == hashlib.sha256(credentials.csrf_token.encode()).hexdigest()
    assert 43190 < (credentials.expires_at - datetime.now(timezone.utc)).total_seconds() <= 43200
    assert store.resolve_access_session("x" * 43) is None
    assert store.revoke_access_session(credentials.token)
    assert not store.revoke_access_session(credentials.token)
    assert store.resolve_access_session(credentials.token) is None
    second = store.create_access_session(user.user_id)
    with store.transaction():
        store.db.execute("UPDATE access_sessions SET expires_at=?", ((datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat(),))
    assert store.resolve_access_session(second.token) is None
    with pytest.raises(KeyError): store.create_access_session(LEGACY_USER_ID)
    with pytest.raises(sqlite3.IntegrityError):
        with store.transaction():
            store.db.execute("INSERT INTO external_identities VALUES ('bad','bad',?,'now')", (LEGACY_USER_ID,))
    with store.transaction():
        store.db.execute("UPDATE users SET deleted_at=? WHERE user_id=?", (datetime.now(timezone.utc).isoformat(), user.user_id))
    assert store.get_user(user.user_id) is None
    with pytest.raises(KeyError): store.create_access_session(user.user_id)
    store.close()


def test_operator_assignment_dry_run_backup_and_one_target_only(tmp_path):
    path = tmp_path / "db.sqlite"
    store = Store(str(path))
    first, other = store.create_session(), store.create_session()
    user = store.create_user()
    before = store.db.execute("SELECT data FROM sessions WHERE id=?", (first.id,)).fetchone()[0]
    store.close()
    script = Path(__file__).resolve().parents[2] / "scripts" / "assign_legacy_trip.py"
    args = [sys.executable, str(script), "--database", str(path), "--session-id", first.id, "--owner-id", user.user_id]
    result = subprocess.run(args, capture_output=True, text=True, check=True)
    assert json.loads(result.stdout)["applied"] == 0
    assert first.id not in result.stdout
    assert subprocess.run(args + ["--apply"], capture_output=True).returncode != 0
    backup = tmp_path / "before.bak"
    result = subprocess.run(args + ["--apply", "--synthetic", "--backup", str(backup)], capture_output=True, text=True, check=True)
    assert json.loads(result.stdout)["applied"] == 1 and backup.is_file()
    store = Store(str(path))
    assert store.session_owner(first.id) == user.user_id
    assert store.session_owner(other.id) == LEGACY_USER_ID
    assert store.db.execute("SELECT data FROM sessions WHERE id=?", (first.id,)).fetchone()[0] == before
    assert store.db.execute("SELECT COUNT(*) FROM legacy_trip_assignments").fetchone()[0] == 1
    old = sqlite3.connect(backup)
    assert old.execute("SELECT user_id FROM session_owners WHERE session_id=?", (first.id,)).fetchone()[0] == LEGACY_USER_ID
    old.close()
    store.close()
