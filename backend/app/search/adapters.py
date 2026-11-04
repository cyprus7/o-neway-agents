"""Hosted Ollama and SearXNG adapters with separate caches and bounded failure paths."""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

import httpx
from one_request_telemetry import queued, set_attributes, span, traced

from ..domain.travel_data import SourceCacheProtocol
from .domain import SearchHit, SearchOutcome, public_search_url


OLLAMA_SEARCH_URL = "https://ollama.com/api/web_search"
SCHEMA = "federated-search-v1"
MAX_RESPONSE_BYTES = 1024 * 1024
MAX_STALE_SECONDS = 7 * 86400


def retry_delay(value: str | None, *, now: float | None = None) -> float:
    """Bound defensive input; the default is an operational backoff, not a quota claim."""
    try:
        delay = float(value) if value is not None else 60.0
    except ValueError:
        try:
            delay = parsedate_to_datetime(value).timestamp() - (time.time() if now is None else now)
        except (ValueError, TypeError, OverflowError):
            delay = 60.0
    return min(86400.0, max(1.0, delay)) if math.isfinite(delay) else 60.0


class SearchAdapter:
    def __init__(self, provider: str, client: httpx.AsyncClient, store: SourceCacheProtocol,
                 slots: asyncio.Semaphore, *, endpoint: str, key: str = "",
                 timeout_seconds: float = 10):
        self.provider, self.client, self.store, self.slots = provider, client, store, slots
        self.endpoint, self._key = endpoint, key
        self.timeout_seconds = min(10.0, max(0.01, timeout_seconds))
        self._cooldown_until = 0.0
        self.last_status = "ready" if self.configured else "disabled"
        self.last_reason = None if self.configured else "missing_key" if provider == "ollama" else "not_configured"

    @property
    def configured(self) -> bool:
        return bool(self._key) if self.provider == "ollama" else bool(self.endpoint)

    @property
    def trace_provider(self) -> str:
        return "ollama_web_search" if self.provider == "ollama" else "searxng" if self.provider == "searxng" else "other"

    def capabilities(self) -> dict[str, Any]:
        cooldown = self._cooldown_until > time.monotonic()
        return {"id": self.provider, "name": "Ollama Web Search" if self.provider == "ollama" else "SearXNG",
                "configured": self.configured, "status": "cooldown" if cooldown else self.last_status,
                "reason": self.last_reason, "capability": "search_evidence_only"}

    def cache_key(self, query: str) -> str:
        scope = [SCHEMA, self.provider, self.endpoint, query, 10 if self.provider == "ollama" else 15]
        return "search:" + hashlib.sha256(json.dumps(scope, ensure_ascii=False).encode()).hexdigest()

    def _text(self, value: Any, limit: int) -> str:
        value = value if isinstance(value, str) else ""
        return value.replace(self._key, "[redacted]")[:limit] if self._key else value[:limit]

    def _rows(self, payload: Any) -> list[dict[str, str]]:
        if not isinstance(payload, dict) or payload.get("error") or not isinstance(payload.get("results"), list):
            raise ValueError("invalid_search_response")
        rows = []
        for row in payload["results"][:10 if self.provider == "ollama" else 15]:
            if not isinstance(row, dict):
                continue
            url = public_search_url(row.get("url"))
            title = self._text(row.get("title"), 180).strip()
            if not url or not title or (self._key and self._key in url):
                continue
            rows.append({"title": title, "url": url, "content": self._text(row.get("content"), 1600),
                         "engine": self._text(row.get("engine", "web"), 30) or "web"})
        return rows

    def _outcome(self, rows: list[dict[str, str]], retrieved: str, status: str,
                 *, stale: bool = False, reason: str | None = None) -> SearchOutcome:
        hits = tuple(SearchHit(self.provider, row["title"], row["url"], row["content"], retrieved,
                               "cached" if status == "cached" else "live", row["engine"], stale) for row in rows)
        self.last_status, self.last_reason = status, reason
        return SearchOutcome(self.provider, hits, status, reason, retrieved_at=retrieved)

    def _cached(self, key: str, *, allow_stale: bool = False, reason: str | None = None) -> SearchOutcome | None:
        with span("search.cache", attributes={"provider": self.trace_provider}):
            cached = self.store.cache_get(key, allow_stale=allow_stale)
            if cached is None or (allow_stale and time.time() - cached[1] >= MAX_STALE_SECONDS):
                set_attributes(cache_hit=False, reason="cache_miss")
                return None
            try:
                payload = cached[0]
                rows = self._rows(payload)
                retrieved = payload["retrieved_at"]
                datetime.fromisoformat(retrieved)
            except (ValueError, TypeError, KeyError):
                set_attributes(cache_hit=False, reason="invalid_response")
                return None
            self.store.increment("search_" + self.provider + ("_stale_hits" if allow_stale else "_cache_hits"))
            set_attributes(cache_hit=True, count=len(rows), outcome="cached_stale" if allow_stale else "cached",
                           reason="stale_cache_hit" if allow_stale else "positive_cache_hit" if rows else "negative_cache_hit")
            return self._outcome(rows, retrieved, "cached", stale=allow_stale, reason=reason)

    @traced("search.http", kind="client")
    async def _request(self, query: str) -> list[dict[str, str]]:
        set_attributes(provider=self.trace_provider)
        kwargs = {"timeout": httpx.Timeout(self.timeout_seconds, connect=min(5, self.timeout_seconds)),
                  "follow_redirects": False}
        if self.provider == "ollama":
            method = "POST"
            kwargs.update(headers={"Authorization": "Bearer " + self._key}, json={"query": query, "max_results": 10})
        else:
            method = "GET"
            kwargs.update(params={"q": query, "format": "json", "language": "all", "categories": "general"})
        self.store.increment("search_" + self.provider + "_requests")
        set_attributes(http_method=method)
        parse_reason = "invalid_response"
        try:
            async with self.client.stream(method, self.endpoint, **kwargs) as response:
                set_attributes(http_status_code=response.status_code)
                if response.status_code == 429:
                    self._cooldown_until = max(self._cooldown_until,
                                               time.monotonic() + retry_delay(response.headers.get("Retry-After")))
                response.raise_for_status()
                body = bytearray()
                async for chunk in response.aiter_bytes(chunk_size=65536):
                    body.extend(chunk)
                    if len(body) > MAX_RESPONSE_BYTES:
                        parse_reason = "response_too_large"
                        raise ValueError("search_response_too_large")
                rows = self._rows(json.loads(body))
                set_attributes(outcome="ok" if rows else "no_offers", reason="search_results" if rows else "no_results", count=len(rows))
                return rows
        except httpx.HTTPStatusError as error:
            set_attributes(outcome="rate_limited" if error.response.status_code == 429 else "blocked" if error.response.status_code in (401, 403) else "unavailable",
                           reason="rate_limited" if error.response.status_code == 429 else "authentication_failed" if error.response.status_code in (401, 403) else "http_error")
            raise
        except (httpx.TimeoutException, TimeoutError):
            set_attributes(outcome="timeout", reason="deadline_exceeded")
            raise
        except httpx.HTTPError:
            set_attributes(outcome="unavailable", reason="network_error")
            raise
        except (ValueError, TypeError):
            set_attributes(outcome="parse_error", reason=parse_reason)
            raise

    @traced("search.lookup")
    async def search(self, query: str, *, ttl: int = 3600) -> SearchOutcome:
        set_attributes(provider=self.trace_provider)
        if not self.configured:
            set_attributes(outcome="disabled", reason=self.last_reason)
            return SearchOutcome(self.provider, (), "disabled", self.last_reason)
        if not isinstance(query, str) or not query.strip() or len(query) > 4000:
            set_attributes(outcome="unavailable", reason="invalid_query")
            return SearchOutcome(self.provider, (), "unavailable", "invalid_query")
        key = self.cache_key(query)
        cached = self._cached(key)
        if cached is not None:
            set_attributes(cache_hit=True, outcome="cached", count=len(cached.hits),
                           reason="positive_cache_hit" if cached.hits else "negative_cache_hit")
            return cached
        reason, status = "provider_unavailable", "unavailable"
        try:
            # Queue acquisition and response consumption share one hard deadline.
            async with asyncio.timeout(self.timeout_seconds):
                async with queued(self.slots, "search.queue"):
                    if self._cooldown_until > time.monotonic():
                        reason, status = "rate_limited", "cooldown"
                    else:
                        # Another in-flight request may already have filled this cache.
                        cached = self._cached(key)
                        if cached is not None:
                            set_attributes(cache_hit=True, outcome="cached", count=len(cached.hits),
                                           reason="positive_cache_hit" if cached.hits else "negative_cache_hit")
                            return cached
                        rows = await self._request(query)
                        retrieved = datetime.now(timezone.utc).isoformat()
                        self.store.cache_put(key, {"results": rows, "retrieved_at": retrieved}, ttl)
                        set_attributes(cache_hit=False, outcome="live", count=len(rows), reason="search_results" if rows else "no_results")
                        return self._outcome(rows, retrieved, "live")
        except (TimeoutError, httpx.TimeoutException):
            reason = "deadline_exceeded"
        except httpx.HTTPStatusError as exc:
            reason = "rate_limited" if exc.response.status_code == 429 else "authentication_failed" if exc.response.status_code in (401, 403) else "http_error"
        except httpx.HTTPError:
            reason = "network_error"
        except (ValueError, TypeError):
            reason = "invalid_response"
        self.store.increment("search_" + self.provider + "_errors")
        set_attributes(outcome=status, reason=reason)
        stale = self._cached(key, allow_stale=True, reason=reason)
        if stale is not None:
            set_attributes(cache_hit=True, outcome="cached_stale", reason=reason)
            return stale
        self.last_status, self.last_reason = status, reason
        return SearchOutcome(self.provider, (), status, reason)


class FederatedSearch:
    def __init__(self, client: httpx.AsyncClient, store: SourceCacheProtocol, slots: asyncio.Semaphore,
                 *, searxng_url: str, timeout_seconds: float = 10):
        self.adapters = (
            SearchAdapter("searxng", client, store, slots,
                          endpoint=searxng_url + "/search" if searxng_url else "", timeout_seconds=timeout_seconds),
            SearchAdapter("ollama", client, store, slots, endpoint=OLLAMA_SEARCH_URL,
                          key=os.getenv("OLLAMA_API_KEY", "").strip(), timeout_seconds=timeout_seconds),
        )

    @traced("search.federated")
    async def search(self, query: str, *, ttl: int = 3600) -> tuple[SearchOutcome, ...]:
        return tuple(await asyncio.gather(*(adapter.search(query, ttl=ttl) for adapter in self.adapters)))

    def capabilities(self) -> dict[str, Any]:
        return {"name": "SearXNG + Ollama Web Search", "configured": any(adapter.configured for adapter in self.adapters),
                "capability": "search_evidence_only", "providers": [adapter.capabilities() for adapter in self.adapters]}
