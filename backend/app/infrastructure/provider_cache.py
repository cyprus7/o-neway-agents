"""Gateway-owned TTL facts and counters; deliberately no session tables or API."""
from __future__ import annotations

import json
import math
from pathlib import Path
import re
import sqlite3
import threading
import time
from typing import Any


MAX_CACHE_BYTES = 2 * 1024 * 1024
MAX_CACHE_ROWS = 10000
MAX_TOTAL_CACHE_BYTES = 64 * 1024 * 1024


class ProviderCache:
    def __init__(self, path: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.lock = threading.RLock()
        with self.lock, self.db:
            self.db.executescript("""
                PRAGMA journal_mode=WAL;
                PRAGMA busy_timeout=5000;
                CREATE TABLE IF NOT EXISTS cache(key TEXT PRIMARY KEY, data TEXT NOT NULL, fetched_at REAL NOT NULL, expires_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS metrics(name TEXT PRIMARY KEY, value REAL NOT NULL DEFAULT 0);
            """)
            self._prune(time.time())

    def _prune(self, now: float) -> None:
        self.db.execute("DELETE FROM cache WHERE expires_at<?", (now - 7 * 86400,))
        self.db.execute("DELETE FROM cache WHERE key IN (SELECT key FROM cache ORDER BY fetched_at DESC,key DESC LIMIT -1 OFFSET ?)", (MAX_CACHE_ROWS,))
        total = self.db.execute("SELECT COALESCE(SUM(length(CAST(data AS BLOB))),0) FROM cache").fetchone()[0]
        if total > MAX_TOTAL_CACHE_BYTES:
            evicted = []
            for old_key, size in self.db.execute("SELECT key,length(CAST(data AS BLOB)) FROM cache ORDER BY fetched_at,key").fetchall():
                if total <= MAX_TOTAL_CACHE_BYTES:
                    break
                evicted.append((old_key,))
                total -= size
            self.db.executemany("DELETE FROM cache WHERE key=?", evicted)

    def cache_get(self, key: str, allow_stale: bool = False) -> tuple[Any, float, bool] | None:
        with self.lock:
            row = self.db.execute("SELECT data,fetched_at,expires_at FROM cache WHERE key=?", (key,)).fetchone()
        now = time.time()
        if row is None or row[2] < now - 7 * 86400 or row[2] < now and not allow_stale:
            return None
        try:
            if len(row[0].encode()) > MAX_CACHE_BYTES:
                return None
            return json.loads(row[0]), row[1], row[2] < now
        except (ValueError, TypeError):
            return None

    def cache_put(self, key: str, value: Any, ttl: int) -> None:
        if not isinstance(key, str) or not 1 <= len(key) <= 500:
            raise ValueError("invalid_cache_key")
        if isinstance(ttl, bool) or not isinstance(ttl, int) or not 1 <= ttl <= 31 * 86400:
            raise ValueError("invalid_cache_ttl")
        payload = json.dumps(value, ensure_ascii=False, allow_nan=False)
        if len(payload.encode()) > MAX_CACHE_BYTES:
            raise ValueError("cache_value_too_large")
        now = time.time()
        with self.lock, self.db:
            # Retain bounded stale-if-error evidence for at most seven days.
            self.db.execute("INSERT OR REPLACE INTO cache VALUES (?,?,?,?)", (key, payload, now, now + ttl))
            self._prune(now)

    def increment(self, name: str, amount: float = 1) -> None:
        if not re.fullmatch(r"[a-zA-Z0-9_.:-]{1,160}", name) or not math.isfinite(amount):
            raise ValueError("invalid_metric")
        with self.lock, self.db:
            self.db.execute("INSERT INTO metrics(name,value) VALUES (?,?) ON CONFLICT(name) DO UPDATE SET value=value+excluded.value", (name, amount))

    def metrics(self) -> dict[str, float]:
        with self.lock:
            return dict(self.db.execute("SELECT name,value FROM metrics"))

    def close(self) -> None:
        self.db.close()
