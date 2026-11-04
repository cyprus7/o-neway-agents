"""Bounded alternatives use actual source properties; all network is fixture-only."""
import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError

from backend.app.data_gateway import create_gateway_app
from backend.app.domain.travel_data import (ComponentBundle, DataIntent, GatewayCapabilities,
    InventoryCandidatesInput, PlaceRef, candidate_identity)
from backend.app.geography import find_place
from backend.app.infrastructure.http_travel_data import HttpTravelDataPort
from backend.app.infrastructure.knowledge_travel_data import KnowledgeTravelDataPort
from backend.app.models import TravelComponent, TravelIntent


def travel():
    return find_place("Larnaca"), find_place("Warsaw"), TravelIntent(origin="Warsaw", destination="Larnaca",
        date_from="2027-06-01", date_to="2027-06-08")


def no_network(request):
    raise AssertionError("No external provider/model calls authorized")


@pytest.mark.asyncio
async def test_real_catalog_alternatives_cross_gateway_distinct_stable_scopes(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCAL_CATALOG_ENABLED", "1")
    monkeypatch.setenv("WEB_FALLBACK_ENABLED", "0")
    app = create_gateway_app(database_path=str(tmp_path / "gateway.db"), transport=httpx.MockTransport(no_network))
    place, origin, intent = travel()
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as client:
            port = HttpTravelDataPort(client, "http://gateway")
            assert (await port.capabilities())["inventory_candidates"] is True
            results = await port.inventory_candidates(place, origin, intent, limit=9)
            assert 2 <= len(results) <= 3  # Existing catalog's honest shortlist cap.
            assert len({b.stay.details["catalog_property_id"] for b in results}) == len(results)
            assert len({b.candidate_id for b in results}) == len(results)
            for bundle in results:
                assert bundle.candidate_id == candidate_identity(bundle, place, origin, intent)
                assert bundle.stay.evidence[0].freshness == "snapshot"
                assert bundle.stay.details["catalog_sources"]
                assert bundle.stay.quote is None and bundle.stay.price is None
                assert bundle.transfer.price is None and bundle.transfer.quote is None
                assert bundle.transfer.details["upstream_property_id"] == bundle.stay.details["catalog_property_id"]
                assert not bundle.stay.details["availability_verified"]
            changed = intent.model_copy(update={"budget_total": 100})
            again = await port.inventory_candidates(place, origin, changed, limit=9)
            assert [b.candidate_id for b in again] == [b.candidate_id for b in results]
            changed = intent.model_copy(update={"adults": 3})
            assert candidate_identity(results[0], place, origin, changed) != results[0].candidate_id
            assert len(await port.inventory_candidates(place, origin, intent, limit=1)) == 1


@pytest.mark.parametrize("limit", [0, 10, True, "3"])
def test_strict_candidate_limit(limit):
    place, origin, intent = travel()
    with pytest.raises(ValidationError):
        InventoryCandidatesInput(place=PlaceRef.from_place(place), origin=PlaceRef.from_place(origin),
                                 intent=DataIntent.from_intent(intent), limit=limit)
    assert GatewayCapabilities(external_data_fallback_enabled=False, providers={}, pricing={}).inventory_candidates is False


@pytest.mark.asyncio
async def test_old_peer_falls_back_once_without_candidate_probe():
    place, origin, intent = travel()
    calls = []
    bundle = ComponentBundle.from_components([TravelComponent(title=n, description="unknown") for n in ("route", "stay", "transfer")])
    def handler(request):
        operation = "capabilities" if request.method == "GET" else json.loads(request.content)["operation"]
        calls.append(operation)
        envelope = ({"request_id": request.headers["X-Request-ID"], "fingerprint": request.headers["X-Request-Fingerprint"]}
                    if request.method == "GET" else json.loads(request.content))
        data = {"external_data_fallback_enabled": False, "providers": {}, "pricing": {}} if operation == "capabilities" else bundle.model_dump(mode="json")
        return httpx.Response(200, json={"operation": operation, "request_id": envelope["request_id"],
            "fingerprint": envelope["fingerprint"], "status": "ok", "data": data})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await HttpTravelDataPort(client, "http://gateway").inventory_candidates(place, origin, intent)
    assert len(result) == 1 and result[0].candidate_id
    assert calls == ["capabilities", "inventory"]


@pytest.mark.asyncio
async def test_shortlist_enrichment_has_one_batch_and_never_copies_property_transfers():
    place, origin, intent = travel()
    bundles = [ComponentBundle.from_components([TravelComponent(title=n, description="unknown")
               for n in ("shared flight", f"hotel {i}", "transfer")]) for i in range(3)]
    class Base:
        async def inventory_candidates(self, *args, **kwargs): return bundles
    port = KnowledgeTravelDataPort(Base(), SimpleNamespace(configured=True))
    batches = []
    async def enrich(*args):
        bundle = args[-1]
        batches.append(bundle)
        bundle.flight.details["knowledge_price_outcome"] = "reference"
        bundle.transfer.details["knowledge_price_outcome"] = "reference"
        return bundle
    port._enrich = enrich
    result = await port.inventory_candidates(place, origin, intent)
    assert len(batches) == 1
    assert all(b.flight.details["knowledge_price_outcome"] == "reference" for b in result)
    assert all("knowledge_price_outcome" not in b.transfer.details for b in result[1:])


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["identity", "scope", "duplicate", "too_many"])
async def test_candidate_response_rejects_unbound_or_over_limit_results(failure):
    place, origin, intent = travel()
    bundle = ComponentBundle.from_components([TravelComponent(title=n, description="unknown") for n in ("route", "hotel", "transfer")])
    bundle.candidate_id = candidate_identity(bundle, place, origin, intent)
    if failure == "identity": bundle.candidate_id = "0" * 64
    if failure == "scope": bundle.candidate_id = candidate_identity(bundle, place, origin, intent.model_copy(update={"adults": 3}))
    candidates = [bundle.model_dump(mode="json")] * (2 if failure in ("duplicate", "too_many") else 1)
    def handler(request):
        if request.method == "GET":
            operation, request_id, fingerprint = "capabilities", request.headers["X-Request-ID"], request.headers["X-Request-Fingerprint"]
            data = {"inventory_candidates": True, "external_data_fallback_enabled": False, "providers": {}, "pricing": {}}
        else:
            incoming = json.loads(request.content)
            operation, request_id, fingerprint = incoming["operation"], incoming["request_id"], incoming["fingerprint"]
            data = {"candidates": candidates}
        return httpx.Response(200, json={"operation": operation, "request_id": request_id,
            "fingerprint": fingerprint, "status": "ok", "data": data})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await HttpTravelDataPort(client, "http://gateway").inventory_candidates(place, origin, intent,
            limit=1 if failure == "too_many" else 3)
    assert len(result) == 1
    assert result[0].stay.details["gateway_reason"] == "invalid_response"
    assert result[0].stay.price is None


@pytest.mark.asyncio
async def test_whole_candidate_timeout_cancels_capability_dispatch(monkeypatch):
    from backend.app.domain.travel_data import DEADLINES
    place, origin, intent = travel()
    canceled = asyncio.Event()
    async def slow(request):
        try:
            await asyncio.sleep(10)
        finally:
            canceled.set()
    monkeypatch.setitem(DEADLINES, "inventory_candidates", .02)
    async with httpx.AsyncClient(transport=httpx.MockTransport(slow)) as client:
        result = await HttpTravelDataPort(client, "http://gateway").inventory_candidates(place, origin, intent)
    assert canceled.is_set()
    assert result[0].stay.details["gateway_reason"] == "deadline_exceeded"


@pytest.mark.asyncio
async def test_actual_knowledge_document_and_model_dispatch_once_for_catalog_shortlist():
    from backend.app.domain.travel_data import PriceDocumentsData
    from backend.tests.test_knowledge_prices import document, scenario
    place, origin, intent, first = scenario()
    bundles = [first.model_copy(deep=True) for _ in range(3)]
    for i, bundle in enumerate(bundles):
        bundle.stay.title = f"Catalog hotel {i}"
        bundle.stay.details["catalog_property_id"] = f"osm:node:{i}"
    calls = []
    class Base:
        async def inventory_candidates(self, *args, **kwargs): return bundles
        async def price_documents(self, *args):
            calls.append("documents")
            return PriceDocumentsData(documents=[document("transfer", "BER airport Berlin taxi EUR 65 per vehicle", place, origin, intent, first)])
    class Model:
        configured = True
        async def completion(self, messages, *args, **kwargs):
            calls.append("model")
            assert kwargs["max_tokens"] == 6000
            payload = json.loads(messages[1]["content"])
            assert len(payload["documents"]) == 1
            return '{"prices": []}'
    result = await KnowledgeTravelDataPort(Base(), Model()).inventory_candidates(place, origin, intent)
    assert len(result) == 3 and calls == ["documents", "model"]
    assert all(b.stay.quote is None for b in result)


@pytest.mark.asyncio
async def test_catalog_exclusion_does_not_fabricate_placeholder(tmp_path, monkeypatch):
    from backend.app.catalog import CatalogOutcome
    monkeypatch.setenv("LOCAL_CATALOG_ENABLED", "1")
    monkeypatch.setenv("WEB_FALLBACK_ENABLED", "0")
    app = create_gateway_app(database_path=str(tmp_path / "gateway.db"), transport=httpx.MockTransport(no_network))
    place, origin, intent = travel()
    async with app.router.lifespan_context(app):
        monkeypatch.setattr(app.state.port.providers.catalog, "search", lambda *a, **kw:
            CatalogOutcome("empty", "no_eligible_local_hotels", (), "fixture", "2026-10-24T00:00:00Z"))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as client:
            assert await HttpTravelDataPort(client, "http://gateway").inventory_candidates(place, origin, intent) == []


def test_legacy_bundle_serialization_omits_unknown_candidate_metadata():
    bundle = ComponentBundle.from_components([TravelComponent(title=n, description="unknown") for n in ("route", "hotel", "transfer")])
    assert "candidate_id" not in bundle.model_dump(mode="json")
    bundle.candidate_id = "a" * 64
    assert bundle.model_dump(mode="json")["candidate_id"] == "a" * 64
