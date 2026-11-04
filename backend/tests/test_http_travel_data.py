"""Agent HTTP adapter failure and evidence identity contracts; no concrete sources."""
import asyncio
from datetime import datetime, timedelta, timezone
import json

import httpx
import pytest

from backend.app.domain.travel_data import (ComponentBundle, GatewayResponse, MAX_RESPONSE_BYTES,
    OUTPUTS, ResolveData, PlaceData, SearchData, context_for)
from backend.app.geography import find_place
from backend.app.infrastructure.http_travel_data import HttpTravelDataPort
from backend.app.models import Evidence, TravelComponent, TravelIntent
from backend.app.pricing.models import PointPrice, PriceObservation, QuoteScope
from backend.app.pricing.normalization import request_context_hash


def travel():
    return find_place("Ларнака"), find_place("Варшава"), TravelIntent(origin="Варшава", destination="Ларнака",
        date_from="2027-06-01", date_to="2027-06-08", adults=2, budget_total=1500, assumptions=["private agent assumption"])


def bundle():
    parts = [TravelComponent(title=title, description="Synthetic previous fact") for title in ("flight", "hotel", "transfer")]
    parts[1].source_url = "https://www.openstreetmap.org/node/123"
    parts[1].details = {"catalog_property_id": "osm:node:123", "pricing_entity_id": "osm:node:123",
                        "catalog_retrieved_at": "2026-10-24T00:00:00Z"}
    parts[1].evidence = [Evidence(provider="Synthetic catalog", source_url=parts[1].source_url,
        retrieved_at="2026-10-24T00:00:00Z", freshness="snapshot", confidence="snapshot", label="Synthetic fact")]
    return ComponentBundle.from_components(parts)


def response(request, data, **changes):
    incoming = json.loads(request.content)
    payload = {"operation": incoming["operation"], "request_id": incoming["request_id"],
               "fingerprint": incoming["fingerprint"], "status": "ok", "data": data}
    payload.update(changes)
    return httpx.Response(200, json=GatewayResponse[OUTPUTS[incoming["operation"]]](**payload).model_dump(mode="json"))


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["http", "json", "version", "fingerprint", "extra", "oversized", "unavailable_with_data"])
async def test_failures_are_bounded_unavailable_without_retries_or_local_fallback(failure):
    calls = []
    def handler(request):
        calls.append(request)
        if failure == "http":
            return httpx.Response(503, text="upstream-private-error")
        if failure == "json":
            return httpx.Response(200, text="secret invalid json", headers={"content-type": "application/json"})
        if failure == "oversized":
            return httpx.Response(200, content=b"x" * (MAX_RESPONSE_BYTES + 1), headers={"content-type": "application/json"})
        result = json.loads(response(request, SearchData(evidence=[])).content)
        if failure == "version": result["version"] = "travel-data.v99"
        if failure == "fingerprint": result["fingerprint"] = "0" * 64
        if failure == "extra": result["api_key"] = "do-not-expose"
        if failure == "unavailable_with_data": result.update(status="unavailable", reason="gateway_unavailable")
        return httpx.Response(200, json=result)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        port = HttpTravelDataPort(client, "http://provider-gateway:8091")
        place, origin, intent = travel()
        facts = await port.search(place, intent)
        assert len(calls) == 1
        assert facts[0].freshness == "unavailable" and facts[0].source_url is None
        assert "secret" not in facts[0].label and "do-not-expose" not in facts[0].label
        assert "budget_total" not in json.loads(calls[0].content)["data"]["intent"]
        assert "assumptions" not in calls[0].content.decode()


@pytest.mark.asyncio
async def test_transport_failure_circuit_stops_repeated_dispatch():
    calls = []
    def handler(request):
        calls.append(request)
        raise httpx.ConnectError("private host details")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        port = HttpTravelDataPort(client, "http://provider-gateway:8091")
        for _ in range(4):
            assert await port.resolve_place("Ларнака") is None
        assert len(calls) == 3
        capabilities = await port.capabilities()
        assert capabilities["gateway"]["reason"] == "circuit_open"
        assert len(calls) == 3


class SlowResponse(httpx.AsyncByteStream):
    async def __aiter__(self):
        await asyncio.sleep(0.1)
        yield b"{}"


@pytest.mark.asyncio
async def test_client_deadline_covers_response_consumption_and_queue():
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200,
            stream=SlowResponse(), headers={"content-type": "application/json"}))) as client:
        port = HttpTravelDataPort(client, "http://provider-gateway:8091", timeout_seconds=0.02)
        place, _, intent = travel()
        assert (await port.weather(place, intent))["kind"] == "unknown"
        for _ in range(8):
            await port._slots.acquire()
        try:
            assert await asyncio.wait_for(port.resolve_place("Ларнака"), timeout=0.2) is None
        finally:
            for _ in range(8): port._slots.release()


@pytest.mark.asyncio
async def test_refresh_rejects_fixed_hotel_replacement_and_preserves_source_times():
    current = bundle()
    before = current.model_dump_json()
    changed = bundle()
    changed.stay.title = "An unrelated hotel"
    changed.stay.details["catalog_property_id"] = "osm:node:456"
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: response(request, changed))) as client:
        port = HttpTravelDataPort(client, "http://provider-gateway:8091")
        place, origin, intent = travel()
        result = await port.refresh_inventory(place, origin, intent, current)
    assert current.model_dump_json() == before
    assert result.stay.title == current.stay.title and result.stay.source_url == current.stay.source_url
    assert result.stay.evidence == current.stay.evidence
    assert result.stay.details["catalog_retrieved_at"] == "2026-10-24T00:00:00Z"
    assert result.stay.details["pricing_refresh_warning"]


@pytest.mark.asyncio
async def test_inventory_rejects_another_party_quote_even_with_matching_envelope():
    place, origin, intent = travel()
    wrong = intent.model_copy(update={"adults": 1})
    context = context_for("stay", place, origin, wrong)
    now = datetime.now(timezone.utc)
    quote = PriceObservation(provider_id="synthetic", request_context_hash=request_context_hash(context),
        scope=QuoteScope(component="stay", entity_level="property", destination="LCA", destination_city=place.name,
            date_from=wrong.date_from, date_to=wrong.date_to, adults=1, children_ages=(), rooms=1,
            trip_type="not_applicable", unit="group_stay", currency="EUR", context_verified=True),
        value=PointPrice(amount="10", currency="EUR"), normalized_group=PointPrice(amount="10", currency="EUR"),
        source_url="https://example.org/fact", observed_at=now, retrieved_at=now,
        expires_at=now+timedelta(minutes=10), outcome="quoted")
    result = bundle()
    result.stay.quote = quote
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: response(request, result))) as client:
        accepted = await HttpTravelDataPort(client, "http://provider-gateway:8091").inventory(place, origin, intent)
    assert accepted.stay.quote.value.kind == "unknown"
    assert accepted.stay.quote.normalized_group is None and accepted.stay.price is None


def test_operator_url_cannot_include_navigation_or_credentials():
    for url in ("file:///tmp/a", "http://user:secret@provider-gateway:8091", "http://provider-gateway:8091/target", "http://provider-gateway:8091?url=x"):
        with pytest.raises(ValueError):
            HttpTravelDataPort(None, url)


@pytest.mark.asyncio
@pytest.mark.parametrize('mutation', [None, 'date', 'hash', 'identity', 'provider', 'party'])
async def test_gateway_initial_same_provider_target_crosses_http_boundary_without_weakening_scope(tmp_path, monkeypatch, mutation):
    """Real gateway/pricing/serialization over ASGI; only external supplier data is a fixture."""
    from backend.app.data_gateway import create_gateway_app
    from price_worker.recipes.ostrovok import reference_observation
    monkeypatch.setenv('KEYLESS_PRICING_ENABLED', '1')
    monkeypatch.setenv('PRICE_WORKER_URL', 'http://price-worker:8080')
    path = '/hotel/germany/berlin/mid8876313/hotel_indigo_berlin_east_side_gallery/'
    worker_requests = []
    def supplier(request):
        assert request.url.host == 'price-worker', 'No real provider/model dispatch'
        body = json.loads(request.content)
        worker_requests.append(body)
        context = body['request_context']
        observation = reference_observation(context, path, 'Synthetic Berlin property',
            {'room': 'Private Double Room', 'amount': '251', 'scope': 'for 2 nights for 2 guests', 'extras': [], 'terms': []})
        return httpx.Response(200, json={'request_context_hash': body['request_context_hash'], 'outcome': 'reference',
            'diagnostic_code': 'ostrovok_public_rate_charges_unverified', 'observation': observation})
    app = create_gateway_app(database_path=str(tmp_path / 'gateway.db'), transport=httpx.MockTransport(supplier))
    async with app.router.lifespan_context(app):
        async def discovered(place, intent, stay, transfer):
            stay.source_url = 'https://ostrovok.ru' + path
            stay.details = {'link_type': 'indexed_hotel_page', 'requirements_verified': False}
        async def no_airfares(*args):
            return {'facts': [], 'diagnostics': []}
        app.state.providers.real_places = discovered
        app.state.providers.pricing.airlines.search = no_airfares
        inner = httpx.ASGITransport(app=app)
        class BoundaryTransport(httpx.AsyncBaseTransport):
            async def handle_async_request(self, request):
                result = await inner.handle_async_request(request)
                payload = json.loads(await result.aread())
                if mutation is not None:
                    quote = payload['data']['stay']['quote']
                    if mutation == 'date': quote['scope']['date_from'] = '2026-10-29'
                    if mutation == 'hash': quote['request_context_hash'] = '0' * 64
                    if mutation == 'identity': payload['data']['stay']['details']['pricing_entity_id'] = 'ostrovok:' + 'a' * 24
                    if mutation == 'provider': quote['provider_id'] = 'google_hotels'
                    if mutation == 'party': quote['scope']['adults'] = 1
                return httpx.Response(result.status_code, json=payload)
        async with httpx.AsyncClient(transport=BoundaryTransport(), base_url='http://provider-gateway:8091') as client:
            port = HttpTravelDataPort(client, 'http://provider-gateway:8091')
            intent = TravelIntent(origin='Larnaca', destination='Berlin', date_from='2026-10-30', date_to='2026-11-01', adults=2)
            place, origin = find_place('Berlin'), find_place('Larnaca')
            result = await port.inventory(place, origin, intent)
            if mutation is not None:
                assert result.stay.quote.value.kind == 'unknown'
                assert result.stay.details['gateway_reason'] == 'invalid_response'
            else:
                assert result.stay.quote.value.min == '251'
                assert result.stay.quote.normalized_group is None
                assert result.stay.quote.scope.entity_id == result.stay.details['pricing_entity_id']
                first = result.stay.quote.model_dump_json()
                refreshed = await port.refresh_inventory(place, origin, intent, result)
                assert refreshed.stay.quote.model_dump_json() == first
                assert refreshed.stay.details['pricing_entity_id'] == result.stay.details['pricing_entity_id']
                assert len(worker_requests) == 1, 'Refresh uses the same fixed-property cache, not another hotel'
