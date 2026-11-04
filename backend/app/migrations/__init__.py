"""Versioned additive migrations. No session/evidence JSON is rewritten."""
from datetime import datetime, timezone
from pathlib import Path
import sqlite3

from ..domain.identity import LEGACY_USER_ID


def migrate(db: sqlite3.Connection) -> None:
    if db.in_transaction:
        raise RuntimeError("migration_requires_own_transaction")
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("BEGIN IMMEDIATE")
    try:
        db.execute("CREATE TABLE IF NOT EXISTS schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)")
        if db.execute("SELECT 1 FROM schema_migrations WHERE version=1").fetchone() is None:
            # Execute complete SQL statements individually: executescript would
            # implicitly commit before DDL and lose atomic upgrade guarantees.
            sql = (Path(__file__).parent / "001_identity.sql").read_text(encoding="utf-8")
            statement = ""
            for line in sql.splitlines(keepends=True):
                statement += line
                if sqlite3.complete_statement(statement):
                    db.execute(statement)
                    statement = ""
            if statement.strip():
                raise RuntimeError("incomplete_migration")
            now = datetime.now(timezone.utc).isoformat()
            db.execute("INSERT INTO users(user_id,kind,created_at) VALUES (?, 'legacy_unclaimed', ?)", (LEGACY_USER_ID, now))
            db.execute("INSERT INTO session_owners(session_id,user_id,parent_trip_id,updated_at) SELECT id,?,NULL,COALESCE(json_extract(data,'$.updated_at'),?) FROM sessions", (LEGACY_USER_ID, now))
            db.execute("INSERT INTO schema_migrations(version,applied_at) VALUES (1,?)", (now,))
        if db.execute("SELECT 1 FROM schema_migrations WHERE version=2").fetchone() is None:
            sql = (Path(__file__).parent / "002_commands.sql").read_text(encoding="utf-8")
            statement = ""
            for line in sql.splitlines(keepends=True):
                statement += line
                if sqlite3.complete_statement(statement):
                    db.execute(statement)
                    statement = ""
            if statement.strip():
                raise RuntimeError("incomplete_migration")
            db.execute("INSERT INTO schema_migrations(version,applied_at) VALUES (2,?)", (datetime.now(timezone.utc).isoformat(),))
        if db.execute("SELECT 1 FROM schema_migrations WHERE version=3").fetchone() is None:
            sql = (Path(__file__).parent / "003_memory.sql").read_text(encoding="utf-8")
            statement = ""
            for line in sql.splitlines(keepends=True):
                statement += line
                if sqlite3.complete_statement(statement):
                    db.execute(statement)
                    statement = ""
            if statement.strip():
                raise RuntimeError("incomplete_migration")
            db.execute("INSERT INTO schema_migrations(version,applied_at) VALUES (3,?)", (datetime.now(timezone.utc).isoformat(),))
        if db.execute("SELECT 1 FROM schema_migrations WHERE version=4").fetchone() is None:
            sql = (Path(__file__).parent / "004_telegram.sql").read_text(encoding="utf-8")
            statement = ""
            for line in sql.splitlines(keepends=True):
                statement += line
                if sqlite3.complete_statement(statement):
                    db.execute(statement)
                    statement = ""
            if statement.strip():
                raise RuntimeError("incomplete_migration")
            db.execute("INSERT INTO schema_migrations(version,applied_at) VALUES (4,?)", (datetime.now(timezone.utc).isoformat(),))
        if db.execute("SELECT 1 FROM schema_migrations WHERE version=5").fetchone() is None:
            sql = (Path(__file__).parent / "005_aftercare.sql").read_text(encoding="utf-8")
            statement = ""
            for line in sql.splitlines(keepends=True):
                statement += line
                if sqlite3.complete_statement(statement):
                    db.execute(statement)
                    statement = ""
            if statement.strip():
                raise RuntimeError("incomplete_migration")
            db.execute("INSERT INTO schema_migrations(version,applied_at) VALUES (5,?)", (datetime.now(timezone.utc).isoformat(),))
        db.commit()
    except BaseException:
        db.rollback()
        raise
