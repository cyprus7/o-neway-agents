"""Generic price research: bounded search and fetched pages, without inference.

Search snippets identify candidate pages only. Only the separately fetched page
content crosses the private boundary for the agent's shared model to interpret.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import hashlib
import json
import os
import re
import time

import httpx

from ..domain.travel_data import PriceDocument, PriceDocumentsData, context_for, public_document_url, transport_mode
from ..pricing.normalization import request_context_hash
from ..travel_dates import selected_departure_date
from ..search import fair_merge
from ..search.adapters import retry_delay


FETCH_URL = "https://ollama.com/api/web_fetch"
MAX_FETCH_BYTES = 300_000
MAX_PAGES_PER_COMPONENT = 3
FETCH_TIMEOUT = 6.0
OPERATION_TIMEOUT = 21.5
SCHEMA = "price-documents-v1"


def _text(value, maximum):
    return " ".join(str(value or "").split())[:maximum]


def _targets(current):
    for name in ("stay", "transfer", "flight"):
        component = getattr(current, name)
        if name == "stay" and component.details.get("catalog_property_id"):
            continue
        if name == "flight" and component.details.get("transport_mode", "flight") != "flight":
            continue
        quote = component.quote
        if (component.price is None and (quote is None or quote.value.kind == "unknown")
                or quote is not None and (quote.provider_id == "knowledge-prices" or quote.freshness() == "stale")):
            yield name



def _context_hash(name, place, origin, intent, current):
    entity = current.stay.details.get("pricing_entity_id") if name == "stay" else None
    if name == "stay" and not entity:
        entity = "knowledge-property:" + hashlib.sha256((current.stay.title + "\n" + str(current.stay.source_url)).encode()).hexdigest()[:24]
    return request_context_hash(context_for(name, place, origin, intent, entity_id=entity,
        departure_date=selected_departure_date(intent, current.flight) if name == "flight" else None))


def price_query(name, place, origin, intent, current):
    """All destinations use the same scoped query construction."""
    city = next((alias for alias in place.aliases if alias.isascii() and len(alias) > 3), place.name)
    destination = f'"{_text(city, 120)}" "{_text(place.country, 120)}"'
    start = selected_departure_date(intent, current.flight) if name == 'flight' else intent.date_from
    period = f'{start} {intent.date_to} {intent.adults} adults {intent.rooms} room {intent.currency}'
    hotel = _text(current.stay.title, 180).replace('"', " ")
    if name == "stay":
        return f'"{hotel}" {destination} hotel room price rates per night {period}'
    if name == "transfer":
        return f'{_text(place.airport or city, 120)} {destination} airport transfer taxi shuttle public transport tariff ticket price per person per car one way return {intent.date_from} {intent.date_to} {intent.currency}'
    return f'{_text(origin.airport or origin.name, 120)} to {_text(place.airport or city, 120)} flight return ticket price {period}'


class PriceDocumentResearch:
    def __init__(self, client, store, search, slots):
        self.client, self.store, self.search, self.slots = client, store, search, slots
        self._key = os.getenv("OLLAMA_API_KEY", "").strip()
        self._cooldown_until = 0.0

    @property
    def configured(self):
        return bool(self._key)

    @staticmethod
    def _safe_url(value):
        try:
            return public_document_url(value)
        except (ValueError, TypeError):
            return None

    def _cache_key(self, url):
        return "price-page:" + hashlib.sha256((SCHEMA + "\n" + url).encode()).hexdigest()

    def _page(self, payload):
        if (not isinstance(payload, dict) or not isinstance(payload.get("content"), str)
                or not payload["content"].strip() or not isinstance(payload.get("title", ""), str)):
            raise ValueError("invalid_page")
        content = payload["content"]
        # A challenge is a stop, never a document for price extraction. No
        # bypass, interactive solve, retry or browser escalation is attempted.
        if re.search(r"captcha|verify (?:that )?you are human|checking your browser|access denied|enable javascript and cookies|проверка.{0,15}робот|подтвердите.{0,15}человек", content[:5000], re.I):
            raise ValueError("source_challenge")
        retrieved = datetime.fromisoformat(payload["retrieved_at"])
        if retrieved.tzinfo is None or retrieved > datetime.now(timezone.utc):
            raise ValueError("invalid_page_time")
        return {"title": payload.get("title", "").replace(self._key, "[redacted]")[:500],
                "content": content.replace(self._key, "[redacted]")[:14000], "retrieved_at": retrieved}

    async def _fetch(self, url):
        key = self._cache_key(url)
        cached = self.store.cache_get(key)
        if cached is not None:
            try:
                page = self._page(cached[0])
                self.store.increment("price_documents_cache_hits")
                return {**page, "delivery": "cache_reuse"}
            except (ValueError, TypeError, KeyError):
                pass
        try:
            # Queue time and streamed response share the same finite deadline.
            async with asyncio.timeout(FETCH_TIMEOUT):
                async with self.slots:
                    if self._cooldown_until > time.monotonic():
                        return None
                    self.store.increment("price_documents_fetch_requests")
                    async with self.client.stream("POST", FETCH_URL, json={"url": url},
                            headers={"Authorization": "Bearer " + self._key}, follow_redirects=False,
                            timeout=httpx.Timeout(FETCH_TIMEOUT, connect=min(3, FETCH_TIMEOUT))) as response:
                        if response.status_code == 429:
                            self._cooldown_until = max(self._cooldown_until,
                                time.monotonic() + retry_delay(response.headers.get("Retry-After")))
                        if response.status_code != 200:
                            return None
                        if int(response.headers.get("content-length", "0")) > MAX_FETCH_BYTES:
                            return None
                        raw = bytearray()
                        async for chunk in response.aiter_bytes(chunk_size=32768):
                            raw.extend(chunk)
                            if len(raw) > MAX_FETCH_BYTES:
                                return None
                        payload = json.loads(raw)
                    if not isinstance(payload, dict):
                        return None
                    payload["retrieved_at"] = datetime.now(timezone.utc).isoformat()
                    page = self._page(payload)
                    self.store.cache_put(key, {**page, "retrieved_at": page["retrieved_at"].isoformat()}, 900)
                    return {**page, "delivery": "live_fetch"}
        except (httpx.HTTPError, TimeoutError, ValueError, TypeError, KeyError, OverflowError):
            self.store.increment("price_documents_fetch_errors")
            return None

    async def lookup(self, place, origin, intent, current, *, enabled=True):
        if not enabled:
            return PriceDocumentsData(outcome="disabled", reason="external_fallback_disabled")
        if not self.configured:
            return PriceDocumentsData(outcome="disabled", reason="missing_fetch_key")
        targets = tuple(_targets(current))
        if transport_mode(intent) != "flight":
            targets = tuple(name for name in targets if name != "transfer")
        if not targets:
            return PriceDocumentsData(outcome="no_documents", reason="no_matching_pages")
        documents = []
        timed_out = False

        async def component(name):
            outcomes = await self.search.search(price_query(name, place, origin, intent, current), ttl=900)
            urls = []
            if name == "stay" and current.stay.details.get("link_type") in (
                    "indexed_hotel_page", "place", "published_tariff", "price_search_result"):
                direct = self._safe_url(current.stay.source_url)
                if direct:
                    urls.append(direct)
            for hit in fair_merge([outcome.hits for outcome in outcomes]):
                url = self._safe_url(hit.url)
                if url and url not in urls:
                    urls.append(url)
                if len(urls) == MAX_PAGES_PER_COMPONENT:
                    break

            async def fetch(url):
                page = await self._fetch(url)
                if page is not None:
                    documents.append(PriceDocument(component=name, source_url=url,
                        context_hash=_context_hash(name, place, origin, intent, current), **page))

            async with asyncio.TaskGroup() as group:
                for url in urls[:MAX_PAGES_PER_COMPONENT]:
                    group.create_task(fetch(url))

        try:
            # Every search and fetch, including its queue, fits the operation.
            async with asyncio.timeout(OPERATION_TIMEOUT):
                async with asyncio.TaskGroup() as group:
                    for name in targets:
                        group.create_task(component(name))
        except TimeoutError:
            timed_out = True
        if documents:
            # Completion order must not change document indices for the model.
            documents.sort(key=lambda item: (targets.index(item.component), item.source_url))
            return PriceDocumentsData(documents=documents, outcome="documents", reason="source_documents")
        return PriceDocumentsData(outcome="timeout" if timed_out else "no_documents",
                                  reason="deadline_exceeded" if timed_out else "no_matching_pages")
