"""Optional, destination-scoped discovery with a twelve-second whole-operation cap."""
from __future__ import annotations

import asyncio
import hashlib
from datetime import date, datetime, timezone
from typing import Any
from urllib.parse import urlsplit

import httpx

from .adapters import OfficialEventFetcher, Resolver
from .domain import (CACHE_TTL, SOURCE_HOSTS, EventResult, SourceFact, canonical_url, city_aliases,
                     eligible_event, parse_source_events, select_events, source_url, utcnow)


# Reviewed source URLs are discovery seeds, never stored event/date assertions.
# Every occurrence still requires freshly fetched/cached explicit structured facts.
SOURCE_SEEDS = {
    "istanbul": (
        "https://goturkiye.com/events/7th-presidential-international-yacht-races-republic-cup",
        "https://goturkiye.com/events/formula-1r-turkish-grand-prix-2027",
    ),
    "prague": (
        "https://www.ceskafilharmonie.cz/en/event/35655-czech-chamber-music-society-jeremias-pestalozzi/",
        "https://www.o2arena.cz/en/events/nickelback-everything-under-the-sun-tour-en/",
    ),
}


class RegionalEventDiscovery:
    def __init__(self, client: httpx.AsyncClient, store: Any, search: Any,
                 network_slots: asyncio.Semaphore | None = None, *, timeout_seconds: float = 12,
                 resolver: Resolver | None = None):
        self.client, self.store, self.search = client, store, search
        self.timeout_seconds = min(12.0, max(0.01, timeout_seconds))
        self.fetcher = OfficialEventFetcher(client, network_slots, resolver=resolver, timeout_seconds=min(7, self.timeout_seconds))

    def _metric(self, name: str, amount: float = 1) -> None:
        if self.store is not None and hasattr(self.store, "increment"):
            self.store.increment("regional_events_" + name, amount)

    def _cache_key(self, url: str) -> str:
        # Enum-shape validation changed in v2; pre-fix facts must not bypass parsing.
        return "regional-event-facts:v2:" + hashlib.sha256(canonical_url(url).encode()).hexdigest()

    def _cached(self, url: str) -> list[SourceFact] | None:
        cached = self.store.cache_get(self._cache_key(url)) if self.store is not None else None
        if cached is None:
            return None
        try:
            rows = cached[0]["facts"]
            if not isinstance(rows, list) or not 1 <= len(rows) <= 120:
                return None
            facts = [SourceFact.model_validate(row) for row in rows]
            now = datetime.now(timezone.utc)
            # Cache timestamp alone must not rejuvenate old or forged source facts.
            for fact in facts:
                observed = datetime.fromisoformat(fact.source.retrieved_at)
                if not 0 <= (now - observed).total_seconds() < CACHE_TTL:
                    return None
                if canonical_url(fact.source.source_url) != canonical_url(url):
                    return None
            self._metric("cache_hits")
            return [fact.model_copy(update={"source": fact.source.model_copy(update={"freshness": "cached"})}) for fact in facts]
        except (KeyError, ValueError, TypeError):
            return None

    async def _source_facts(self, url: str) -> tuple[list[SourceFact], bool]:
        cached = self._cached(url)
        if cached is not None:
            return cached, True
        self._metric("source_requests")
        document = await self.fetcher.fetch(url)
        if document is None:
            self._metric("source_errors")
            return [], False
        facts = parse_source_events(document.html, document.url, document.retrieved_at)
        if facts and self.store is not None:
            payload = {"facts": [fact.model_dump(mode="json") for fact in facts]}
            self.store.cache_put(self._cache_key(document.url), payload, CACHE_TTL)
            # A single allowed redirect may canonicalize the discovery URL. Keep the
            # original key only when it names the same final document; validation below
            # otherwise deliberately misses, rather than trusting a disguised cache row.
        return facts, True

    async def _candidate_urls(self, place: Any, start: date, end: date) -> tuple[list[str], bool]:
        aliases = city_aliases(place)
        seed_key = next((key for key in SOURCE_SEEDS if key in aliases), None)
        seeds = list(SOURCE_SEEDS.get(seed_key, ()))
        # A source URL's year can prioritize a fetch; it is never an occurrence fact.
        seeds.sort(key=lambda url: (str(start.year) not in url, url))
        country = str(getattr(place, "country_code", "")).upper()
        hosts = [host for host, (_, source_country, _) in SOURCE_HOSTS.items() if source_country == country]
        hosts = hosts or list(SOURCE_HOSTS)
        city = seed_key or str(place.name)
        query = f'"{city}" events festival concert exhibition {start.isoformat()} {end.isoformat()} ' + " OR ".join("site:" + host for host in hosts)
        search_ok, hits = False, []
        try:
            # A slow optional search must leave time to inspect actual source pages.
            async with asyncio.timeout(min(4, self.timeout_seconds)):
                outcomes = await self.search.search(query, ttl=CACHE_TTL)
            for outcome in outcomes:
                if getattr(outcome, "status", "unavailable") in ("live", "cached"):
                    search_ok = True
                # Interleave sources below; untrusted snippets never enter the parser.
                hits.extend(getattr(outcome, "hits", ()))
        except (TimeoutError, httpx.HTTPError, ValueError, TypeError, AttributeError):
            self._metric("search_errors")
        candidates = []
        for hit in hits:
            url = source_url(getattr(hit, "url", None))
            if url is not None and SOURCE_HOSTS[urlsplit(url).hostname][1] == country:
                candidates.append(url)
        # Reviewed seeds make supported-city coverage resilient to SERP gaps, while
        # the remaining slot can discover a new official page. No seed is a date fact.
        ordered = seeds + candidates
        selected, seen = [], set()
        for url in ordered:
            identity = canonical_url(url)
            if identity not in seen:
                selected.append(url)
                seen.add(identity)
            if len(selected) == 3:
                break
        return selected, search_ok

    async def discover(self, place: Any, intent: Any) -> EventResult:
        start, end = getattr(intent, "date_from", None), getattr(intent, "date_to", None)
        if not start or not end:
            return EventResult(status="dates_required", checked_at=utcnow())
        try:
            start = start if isinstance(start, date) else date.fromisoformat(str(start))
            end = end if isinstance(end, date) else date.fromisoformat(str(end))
            if end < start:
                return EventResult(status="dates_required", checked_at=utcnow())
        except (TypeError, ValueError):
            return EventResult(status="dates_required", checked_at=utcnow())
        completed: list[tuple[list[SourceFact], bool]] = []
        search_ok = False
        attempted_sources = False

        async def collect(url: str) -> None:
            completed.append(await self._source_facts(url))

        try:
            async with asyncio.timeout(self.timeout_seconds):
                urls, search_ok = await self._candidate_urls(place, start, end)
                attempted_sources = bool(urls)
                await asyncio.gather(*(collect(url) for url in urls))
        except TimeoutError:
            self._metric("deadline_exceeded")
        except (ValueError, TypeError, AttributeError, httpx.HTTPError, OSError):
            self._metric("errors")
        events = []
        for facts, _ in completed:
            for fact in facts:
                event = eligible_event(fact, place, start, end)
                if event is not None:
                    events.append(event)
        selected = select_events(events, getattr(intent, "trip_styles", []))
        self._metric("checks")
        if selected:
            self._metric("ready")
            return EventResult(status="ready", items=selected, checked_at=utcnow())
        # An attempted source fetch failing is unavailable even if SERPs succeeded.
        # A successfully inspected source with no eligible occurrence is truthful empty.
        any_source = any(success for _, success in completed)
        unavailable = not any_source and (attempted_sources or not search_ok)
        return EventResult(status="unavailable" if unavailable else "empty", checked_at=utcnow())
