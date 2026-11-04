"""Operator-local assignment of one explicitly acknowledged synthetic trip.

Dry-run by default. No public session-ID claim endpoint exists. Raw trip IDs and
session contents are deliberately omitted from command output.
"""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.app.domain.identity import LEGACY_USER_ID
from backend.app.storage import Store


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True)
    parser.add_argument("--session-id", default=os.environ.get("LEGACY_TRIP_SESSION_ID"))
    parser.add_argument("--owner-id", required=True)
    parser.add_argument("--synthetic", action="store_true", help="Operator acknowledges this one trip is synthetic and authorized for assignment")
    parser.add_argument("--apply", action="store_true", help="Apply after creating a consistent SQLite backup")
    parser.add_argument("--backup", help="New backup path; never overwrite an existing backup")
    args = parser.parse_args(argv)
    if not args.session_id:
        parser.error("--session-id or LEGACY_TRIP_SESSION_ID is required")
    if args.apply and not args.synthetic:
        parser.error("--apply requires explicit --synthetic acknowledgement")
    path = Path(args.database).resolve()
    if not path.is_file():
        parser.error("database must already exist")
    # Dry-run never opens Store: even schema migrations would mutate the input.
    source = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    try:
        tables = {r[0] for r in source.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"sessions", "session_owners", "users"} <= tables:
            parser.error("database must first have the reviewed ownership migration")
        target = source.execute("SELECT user_id FROM session_owners WHERE session_id=?", (args.session_id,)).fetchone()
        owner = source.execute("SELECT 1 FROM users WHERE user_id=? AND kind!='legacy_unclaimed' AND deleted_at IS NULL", (args.owner_id,)).fetchone()
        if target is None or target[0] != LEGACY_USER_ID or owner is None:
            parser.error("target must be one unclaimed trip and an existing login-capable owner")
        if not args.apply:
            print(json.dumps({"mode": "dry_run", "eligible_trips": 1, "would_assign": 1, "applied": 0}))
            return 0
        backup_path = Path(args.backup).resolve() if args.backup else path.with_name(path.name + ".before-assignment-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + ".bak")
        if backup_path.exists() or backup_path == path:
            parser.error("backup must be a new path")
        backup_path.parent.mkdir(parents=True, exist_ok=True)
        # Exclusive creation prevents overwriting another backup after preflight.
        with backup_path.open("xb"):
            pass
        with sqlite3.connect(backup_path) as destination:
            source.backup(destination)
    finally:
        source.close()
    store = Store(str(path))
    try:
        store.assign_legacy_trip(args.session_id, args.owner_id)
    finally:
        store.close()
    print(json.dumps({"mode": "apply", "eligible_trips": 1, "applied": 1, "backup": str(backup_path)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
