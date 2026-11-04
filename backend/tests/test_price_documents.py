"""Generic source research contracts; fixtures never establish supplier prices."""
import asyncio
from datetime import datetime, timezone
import json

import httpx
import pytest
from pydantic import ValidationError

from backend.app.data_gateway import create_gateway_app
from backend.app.domain.travel_data import ComponentBundle, PriceDocument, PriceDocumentsData
from backend.app.geography import find_place
from backend.app.infrastructure.http_travel_data import HttpTravelDataPort
from backend.app.infrastructure.provider_cache import ProviderCache
from backend.app.models import TravelComponent, TravelIntent
from backend.app.pricing.documents import PriceDocumentResearch, _context_hash, price_query
from backend.app.search.domain import SearchHit, SearchOutcome


KEY = "fixture-page-fetch-key"


def case(city="Berlin", hotel="Hotel Indigo Berlin - East Side Gallery by IHG"):
    place, origin = find_place(city), find_place("Larnaca")
    intent = TravelIntent(origin="Larnaca", destination=city, date_from="2026-10-30", date_to="2026-11-01")
    current = ComponentBundle.from_components([TravelComponent(title="Flight", description="Source"),
        TravelComponent(title=hotel, description="Source", source_url="https://hotel.example/rates",
            details={"link_type": "indexed_hotel_page"}),
        TravelComponent(title="Airport → hotel → airport", description="Source")])
    return place, origin, intent, current


class Search:
    def __init__(self, urls):
        self.urls, self.queries = urls, []

    async def search(self, query, **kwargs):
        self.queries.append(query)
        hits = tuple(SearchHit("ollama", "Fixture", url, "SNIPPET ONLY: EUR99999",
            "2026-10-24T00:00:00Z", "live") for url in self.urls)
        return (SearchOutcome("ollama", hits, "live"),)


def document(place, origin, intent, current, **changes):
    value = {"component": "stay", "source_url": "https://hotel.example/rates", "title": "Hotel rate",
        "content": "A hotel rate EUR120 per night.", "retrieved_at": datetime.now(timezone.utc),
        "context_hash": _context_hash("stay", place, origin, intent, current)}
    return PriceDocument(**{**value, **changes})


@pytest.mark.parametrize("url", ["http://hotel.example/rates", "https://127.0.0.1/rates", "https://localhost/rates",
    "https://service.internal/rates", "https://hotel.example:8443/rates", "https://user:secret@hotel.example/rates",
    "file:///etc/passwd", "https://hotel.example/rates#command"])
def test_document_rejects_unsafe_source_targets(url):
    with pytest.raises(ValidationError):
        document(*case(), source_url=url)


def test_document_requires_aware_time_and_bounded_content():
    with pytest.raises(ValidationError):
        document(*case(), retrieved_at=datetime(2026, 10, 24))
    with pytest.raises(ValidationError):
        document(*case(), content="x" * 14001)
    with pytest.raises(ValidationError):
        PriceDocumentsData(documents=[document(*case())] * 10)


@pytest.mark.asyncio
async def test_generic_search_fetches_pages_and_cache_preserves_time(tmp_path, monkeypatch):
    monkeypatch.setenv("OLLAMA_API_KEY", KEY)
    calls = []
    search = Search(["https://hotel.example/rates", "https://source.example/airport-tariff"])

    def handler(request):
        calls.append(request)
        assert str(request.url) == "https://ollama.com/api/web_fetch"
        assert request.headers["authorization"] == "Bearer " + KEY
        assert "traceparent" not in request.headers
        url = json.loads(request.content)["url"]
        assert url.startswith("https://")
        return httpx.Response(200, json={"title": "Actual page", "content": "Fetched rate EUR120 per night " + KEY,
            "links": ["http://127.0.0.1/admin"]})

    cache = ProviderCache(str(tmp_path / "pages.sqlite"))
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            research = PriceDocumentResearch(client, cache, search, asyncio.Semaphore(5))
            first = await research.lookup(*case())
            second = await research.lookup(*case())
            assert first.outcome == second.outcome == "documents"
            assert {doc.component for doc in first.documents} == {"flight", "stay", "transfer"}
            assert len(first.documents) == 6
            assert any(doc.delivery == "live_fetch" for doc in first.documents)
            assert all(doc.delivery == "cache_reuse" for doc in second.documents)
            assert all("SNIPPET ONLY" not in doc.content and KEY not in doc.content for doc in first.documents)
            assert [(doc.component, doc.source_url, doc.retrieved_at) for doc in first.documents] == [
                (doc.component, doc.source_url, doc.retrieved_at) for doc in second.documents]
            assert len(calls) <= 6  # Concurrent components can share a URL cache miss.
    finally:
        cache.close()


@pytest.mark.asyncio
async def test_page_delivery_distinguishes_fetch_from_cache_without_retiming(tmp_path, monkeypatch):
    monkeypatch.setenv("OLLAMA_API_KEY", KEY)
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"title": "Actual tariff", "content": "EUR120 per room per night"})

    cache = ProviderCache(str(tmp_path / "delivery.sqlite"))
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            research = PriceDocumentResearch(client, cache, Search([]), asyncio.Semaphore(5))
            first = await research._fetch("https://hotel.example/rates")
            second = await research._fetch("https://hotel.example/rates")
            assert first["delivery"] == "live_fetch" and second["delivery"] == "cache_reuse"
            assert first["retrieved_at"] == second["retrieved_at"]
            assert len(requests) == 1
            assert document(*case()).delivery == "live_fetch"
            with pytest.raises(ValidationError):
                document(*case(), delivery="model_extraction")
    finally:
        cache.close()


@pytest.mark.asyncio
async def test_disabled_and_missing_fetch_key_do_not_search_or_fetch(tmp_path, monkeypatch):
    def forbidden(request):
        pytest.fail("Disabled research must perform no HTTP dispatch")
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
    search = Search([])
    cache = ProviderCache(str(tmp_path / "off.sqlite"))
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(forbidden)) as client:
            research = PriceDocumentResearch(client, cache, search, asyncio.Semaphore(5))
            assert (await research.lookup(*case(), enabled=False)).reason == "external_fallback_disabled"
            assert (await research.lookup(*case())).reason == "missing_fetch_key"
            assert search.queries == []
    finally:
        cache.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [httpx.Response(403, text="Forbidden"),
    httpx.Response(302, headers={"Location": "http://127.0.0.1/admin"}),
    httpx.Response(200, json={"title": "Challenge", "content": "Verify you are human captcha EUR120"}),
    httpx.Response(200, json={"title": "Bad", "content": "x" * 300001}),
    httpx.Response(200, json={"title": "Bad", "content": None})])
async def test_failed_challenged_oversized_pages_never_fall_back_to_snippets(tmp_path, monkeypatch, response):
    monkeypatch.setenv("OLLAMA_API_KEY", KEY)
    search = Search(["https://hotel.example/rates"])
    cache = ProviderCache(str(tmp_path / "fail.sqlite"))
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: response)) as client:
            research = PriceDocumentResearch(client, cache, search, asyncio.Semaphore(5))
            result = await research.lookup(*case())
            assert result.documents == []
    finally:
        cache.close()


@pytest.mark.asyncio
async def test_caps_and_unsafe_search_results_and_catalog_policy(tmp_path, monkeypatch):
    monkeypatch.setenv("OLLAMA_API_KEY", KEY)
    search = Search(["http://unsafe.example/rate", "https://127.0.0.1/admin"] + [f"https://rate{i}.example/prices" for i in range(20)])
    calls = []

    def handler(request):
        calls.append(json.loads(request.content)["url"])
        return httpx.Response(200, json={"title": "Tariff", "content": "Actual published tariff EUR20 per person"})

    place, origin, intent, current = case()
    current.stay.details["catalog_property_id"] = "osm:node:1"
    cache = ProviderCache(str(tmp_path / "caps.sqlite"))
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await PriceDocumentResearch(client, cache, search, asyncio.Semaphore(5)).lookup(place, origin, intent, current)
            assert len(result.documents) == 6
            assert all(doc.component != "stay" for doc in result.documents)
            assert len(search.queries) == 2
            assert all("127.0.0.1" not in url and url.startswith("https://") for url in calls)
    finally:
        cache.close()


def test_queries_and_context_are_generic_and_bind_selected_property():
    for city, hotel in (("Berlin", "Hotel Indigo Berlin - East Side Gallery by IHG"), ("Prague", "Hotel Kampa Prague")):
        place, origin, intent, current = case(city, hotel)
        query = price_query("stay", place, origin, intent, current)
        assert hotel in query and str(intent.date_from) in query and "EUR" in query
        transfer = price_query("transfer", place, origin, intent, current)
        assert place.airport in transfer and "public transport tariff" in transfer
        first = _context_hash("stay", place, origin, intent, current)
        current.stay.title += " Annex"
        assert _context_hash("stay", place, origin, intent, current) != first


@pytest.mark.asyncio
async def test_research_cancellation_closes_all_fetches(tmp_path, monkeypatch):
    monkeypatch.setenv("OLLAMA_API_KEY", KEY)
    active, cancelled = 0, 0
    started = asyncio.Event()

    async def handler(request):
        nonlocal active, cancelled
        active += 1
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled += 1
            raise
        finally:
            active -= 1

    cache = ProviderCache(str(tmp_path / "cancel.sqlite"))
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            research = PriceDocumentResearch(client, cache, Search(["https://hotel.example/rates"]), asyncio.Semaphore(5))
            task = asyncio.create_task(research.lookup(*case()))
            await started.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert active == 0 and cancelled > 0
    finally:
        cache.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("wrong_hash", [False, True])
async def test_price_documents_cross_actual_gateway_http_contract(tmp_path, wrong_hash):
    place, origin, intent, current = case()

    class Port:
        async def price_documents(self, place, origin, intent, current):
            doc = document(place, origin, intent, current)
            if wrong_hash:
                doc.context_hash = "0" * 64
            return PriceDocumentsData(documents=[doc], outcome="documents", reason="source_documents")

    app = create_gateway_app(database_path=str(tmp_path / "gateway.sqlite"), port_factory=lambda *_: Port())
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as client:
            result = await HttpTravelDataPort(client, "http://provider-gateway:8091").price_documents(place, origin, intent, current)
            assert len(result.documents) == (0 if wrong_hash else 1)
            assert result.reason == ("invalid_response" if wrong_hash else "source_documents")


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled,key,configured", [(False, KEY, False), (True, "", False), (True, KEY, True)])
async def test_capabilities_report_actual_fetch_readiness(tmp_path, monkeypatch, enabled, key, configured):
    monkeypatch.setenv("WEB_FALLBACK_ENABLED", "1" if enabled else "0")
    monkeypatch.setenv("OLLAMA_API_KEY", key)
    app = create_gateway_app(database_path=str(tmp_path / "capability.sqlite"),
        transport=httpx.MockTransport(lambda _: pytest.fail("Capabilities must not dispatch sources")))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as client:
            result = await HttpTravelDataPort(client, "http://provider-gateway:8091").capabilities()
            capability = result["providers"]["price_documents"]
            assert capability["configured"] is configured
            assert capability["status"] == ("ready" if configured else "disabled")
            assert capability["capability"] == "fetched_source_documents"
