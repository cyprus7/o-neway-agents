"""Provider persistence is bounded and structurally separate from agent state."""
import sqlite3
import time

import pytest

from backend.app.infrastructure.provider_cache import MAX_CACHE_BYTES, ProviderCache


def test_cache_restart_ttl_stale_and_tables(tmp_path, monkeypatch):
    path = str(tmp_path / "providers.sqlite3")
    cache = ProviderCache(path)
    cache.cache_put("fact", {"retrieved_at": "2026-10-24T00:00:00Z"}, 10)
    cache.increment("source_requests")
    original = cache.cache_get("fact")
    cache.close()
    cache = ProviderCache(path)
    assert cache.cache_get("fact") == original
    assert cache.metrics() == {"source_requests": 1}
    tables = {row[0] for row in cache.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert tables == {"cache", "metrics"}
    monkeypatch.setattr(time, "time", lambda: original[1] + 11)
    assert cache.cache_get("fact") is None
    assert cache.cache_get("fact", allow_stale=True) == (original[0], original[1], True)
    cache.close()


def test_cache_rejects_unbounded_values_and_invalid_counters(tmp_path):
    cache = ProviderCache(str(tmp_path / "provider.sqlite3"))
    with pytest.raises(ValueError):
        cache.cache_put("large", "x" * MAX_CACHE_BYTES, 10)
    with pytest.raises(ValueError):
        cache.cache_put("ttl", {}, 0)
    with pytest.raises(ValueError):
        cache.increment("bad metric", 1)
    with pytest.raises(ValueError):
        cache.increment("metric", float("nan"))
    cache.close()


def test_aggregate_payload_budget_evicts_oldest_utf8_values(tmp_path, monkeypatch):
    from backend.app.infrastructure import provider_cache
    monkeypatch.setattr(provider_cache, "MAX_TOTAL_CACHE_BYTES", 45)
    ticks = iter((99.0, 100.0, 101.0, 102.0))
    monkeypatch.setattr(provider_cache.time, "time", lambda: next(ticks))
    cache = ProviderCache(str(tmp_path / "budget.sqlite3"))
    cache.cache_put("oldest", "я" * 10, 100)  # 22 UTF-8 bytes including JSON quotes.
    cache.cache_put("middle", "я" * 10, 100)
    cache.cache_put("newest", "я" * 10, 100)
    assert {row[0] for row in cache.db.execute("SELECT key FROM cache")} == {"middle", "newest"}
    assert cache.db.execute("SELECT SUM(length(CAST(data AS BLOB))) FROM cache").fetchone()[0] == 44
    cache.close()
