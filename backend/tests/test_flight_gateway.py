"""Synthetic private HTTP contracts; no browser, model or public provider calls."""
import asyncio
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from backend.app.geography import find_place
from backend.app.infrastructure.provider_cache import ProviderCache
from backend.app.models import TravelIntent
from backend.app.pricing.flights import BrowserFlightSearch, MAX_RESPONSE_BYTES, entity_id
from backend.app.pricing.normalization import request_context_hash
from backend.app.pricing.providers import context_for
from flight_search.models import ObservedLeg, SelectedRoute, itinerary_id


def trip(**changes):
    intent = TravelIntent(origin='Ларнака', destination='Берлин', date_from='2026-10-30',
        date_to='2026-11-01', adults=2, **changes)
    place, origin = find_place('Берлин'), find_place('Ларнака')
    return place, origin, intent


def source_route(*, adults=2, date_from='2026-10-30', date_to='2026-11-01', stops=0, departure_time='06:15'):
    """Clearly synthetic source observation, including an overnight return."""
    start = datetime.fromisoformat(date_from).date()
    end = datetime.fromisoformat(date_to).date()
    outbound = ObservedLeg(origin='LCA', destination='BER', origin_name='Larnaca', destination_name='Berlin',
        departure_date=start, arrival_date=start, departure_time=departure_time, arrival_time='08:30',
        duration_minutes=195, stops=stops, carriers=('Synthetic fixture carrier',),
        observed_connections=tuple({'airport_name':f'Synthetic airport {index}', 'city_name':'Synthetic connection city',
                                    'duration_minutes':30} for index in range(stops)))
    inbound = ObservedLeg(origin='BER', destination='LCA', origin_name='Berlin', destination_name='Larnaca',
        departure_date=end, arrival_date=end + timedelta(days=1), departure_time='22:30', arrival_time='01:35',
        duration_minutes=185, stops=stops, carriers=('Synthetic fixture carrier',),
        observed_connections=tuple({'airport_name':f'Synthetic airport {index}', 'city_name':'Synthetic connection city',
                                    'duration_minutes':30} for index in range(stops)))
    checked = datetime.now(timezone.utc) - timedelta(seconds=30)
    return SelectedRoute(itinerary_id=itinerary_id(outbound, inbound), origin='LCA', destination='BER',
        date_from=start, date_to=end, adults=adults, outbound=outbound, inbound=inbound,
        price={'amount': '1622', 'adults': adults}, source_url='https://www.google.com/travel/flights?hl=en&curr=EUR',
        observed_at=checked, retrieved_at=checked, expires_at=checked + timedelta(minutes=10))


def envelope(request, route=None):
    body = json.loads(request.content)
    route = route or source_route()
    return {'version': 'flight-search.v1', 'request_id': body['request_id'],
        'request_context_hash': body['request_context_hash'], 'outcome': 'reference',
        'diagnostic_code': 'flight_reference', 'route': route.model_dump(mode='json'),
        'retrieved_at': route.retrieved_at.isoformat(), 'candidate_count': 2,
        'diagnostics': [{'provider_id': 'google_flights', 'outcome': 'reference',
            'diagnostic_code': 'flight_reference', 'dispatched': True, 'external_dispatched': True,
            'checked_at': route.observed_at.isoformat(), 'retrieved_at': route.retrieved_at.isoformat()}]}


@pytest.mark.asyncio
async def test_paired_return_price_is_reference_and_fixed_cache_preserves_observation(monkeypatch, tmp_path):
    monkeypatch.setenv('FLIGHT_SEARCH_URL', 'http://flight-search:8080')
    monkeypatch.setenv('WEB_FALLBACK_ENABLED', '1')
    calls = []
    route = source_route()
    def handler(request):
        calls.append(json.loads(request.content))
        assert request.url == 'http://flight-search:8080/v1/search'
        return httpx.Response(200, json=envelope(request, route))
    store = ProviderCache(str(tmp_path / 'cache.db'))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = BrowserFlightSearch(client, store)
        place, origin, intent = trip()
        context = context_for('flight', place, origin, intent)
        first, diagnostics, metadata = await adapter.search(context, intent)
        fixed = context.model_copy(update={'target_policy': 'fixed_entity', 'entity_id': entity_id(route)})
        refreshed, reused, selected = await adapter.search(fixed, intent)
    assert len(calls) == 1
    assert first.value.kind == 'lower_bound' and first.value.min == '1622'
    assert first.normalized_group is None and not first.availability_verified and not first.component_complete
    assert first.scope.unit == 'group_trip' and first.scope.adults == 2 and first.scope.mandatory_fees == 'unknown'
    assert refreshed.request_context_hash == request_context_hash(fixed)
    assert first.id == refreshed.id
    assert first.observed_at == refreshed.observed_at == route.observed_at
    assert first.retrieved_at == refreshed.retrieved_at == route.retrieved_at
    assert first.expires_at == refreshed.expires_at == route.expires_at
    assert metadata['route'].inbound.arrival_date.isoformat() == '2026-11-02'
    assert diagnostics[0]['external_dispatched'] is True
    assert selected['delivery'] == 'cache_reuse'
    assert reused[0]['delivery'] == 'cache_reuse' and reused[0]['dispatched'] is False
    assert reused[0]['checked_at'] == diagnostics[0]['checked_at']
    assert store.metrics()['flight_search_requests'] == 1 and store.metrics()['flight_search_cache_hits'] == 1
    store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('mutation', ['hash', 'request_id', 'party', 'year', 'route', 'currency', 'unit', 'source', 'fee_claim'])
async def test_rejects_echoed_hash_and_independently_incompatible_readback(monkeypatch, tmp_path, mutation):
    monkeypatch.setenv('FLIGHT_SEARCH_URL', 'http://flight-search:8080')
    monkeypatch.setenv('WEB_FALLBACK_ENABLED', '1')
    def handler(request):
        payload = envelope(request)
        if mutation == 'hash':
            payload['request_context_hash'] = 'a' * 64
        elif mutation == 'request_id':
            payload['request_id'] = 'another-request'
        elif mutation == 'party':
            payload['route'] = source_route(adults=1).model_dump(mode='json')
        elif mutation == 'year':
            payload['route'] = source_route(date_from='2027-10-30', date_to='2027-11-01').model_dump(mode='json')
        elif mutation == 'route':
            payload['route']['outbound']['origin'] = 'PFO'
        elif mutation == 'currency':
            payload['route']['currency'] = 'USD'
        elif mutation == 'unit':
            payload['route']['price']['unit'] = 'person_trip'
        elif mutation == 'source':
            payload['route']['source_url'] = 'https://www.google.com/travel/flights/booking'
        elif mutation == 'fee_claim':
            payload['route']['price']['fees_complete'] = True
        return httpx.Response(200, json=payload)
    store = ProviderCache(str(tmp_path / 'reject.db'))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = BrowserFlightSearch(client, store)
        place, origin, intent = trip()
        quote, diagnostics, selected = await adapter.search(context_for('flight', place, origin, intent), intent)
    assert quote is None and selected is None
    assert diagnostics[0]['outcome'] in ('context_mismatch', 'parse_error')
    assert store.db.execute('SELECT COUNT(*) FROM cache').fetchone()[0] == 0
    store.close()


@pytest.mark.asyncio
async def test_constraints_are_forwarded_and_isolate_cache(monkeypatch, tmp_path):
    monkeypatch.setenv('FLIGHT_SEARCH_URL', 'http://flight-search:8080')
    monkeypatch.setenv('WEB_FALLBACK_ENABLED', '1')
    calls = []
    def handler(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json=envelope(request))
    store = ProviderCache(str(tmp_path / 'policy.db'))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = BrowserFlightSearch(client, store)
        for prefs in ({}, {'direct_flight_preferred': True}, {'hard_constraints': ['direct_flight'], 'max_travel_minutes': 300}):
            place, origin, intent = trip(**prefs)
            quote, _, _ = await adapter.search(context_for('flight', place, origin, intent), intent)
            assert quote is not None
    assert len(calls) == 3
    assert calls[1]['constraints']['prefer_direct'] is True
    assert calls[2]['constraints'] == {'cabin': 'economy', 'direct_required': True, 'prefer_direct': False, 'max_flight_minutes': 300}
    assert calls[0]['request_context_hash'] == calls[2]['request_context_hash']
    store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('prefs', [{'hard_constraints': ['direct_flight']}, {'max_travel_minutes': 190}])
async def test_gateway_rechecks_service_candidate_against_hard_bounds(monkeypatch, tmp_path, prefs):
    monkeypatch.setenv('FLIGHT_SEARCH_URL', 'http://flight-search:8080')
    monkeypatch.setenv('WEB_FALLBACK_ENABLED', '1')
    store = ProviderCache(str(tmp_path / 'bounds.db'))
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(
            200, json=envelope(request, source_route(stops=1))))) as client:
        adapter = BrowserFlightSearch(client, store)
        place, origin, intent = trip(**prefs)
        quote, rows, _ = await adapter.search(context_for('flight', place, origin, intent), intent)
    assert quote is None and rows[0]['diagnostic_code'] == 'flight_context_mismatch'
    store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('url,web', [('https://flight-search:8080', '1'), ('http://evil.example', '1'),
    ('http://flight-search:8080?token=private', '1'), ('http://flight-search:8080', '0')])
async def test_unconfigured_or_disabled_adapter_dispatches_nothing(monkeypatch, tmp_path, url, web):
    monkeypatch.setenv('FLIGHT_SEARCH_URL', url)
    monkeypatch.setenv('WEB_FALLBACK_ENABLED', web)
    store = ProviderCache(str(tmp_path / 'disabled.db'))
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: pytest.fail('No dispatch permitted'))) as client:
        adapter = BrowserFlightSearch(client, store)
        place, origin, intent = trip()
        quote, rows, _ = await adapter.search(context_for('flight', place, origin, intent), intent)
    assert quote is None and rows[0]['dispatched'] is False
    assert not store.metrics()
    store.close()


@pytest.mark.asyncio
async def test_private_response_body_cap_is_enforced_before_parsing(monkeypatch, tmp_path):
    monkeypatch.setenv('FLIGHT_SEARCH_URL', 'http://flight-search:8080')
    monkeypatch.setenv('WEB_FALLBACK_ENABLED', '1')
    store = ProviderCache(str(tmp_path / 'body.db'))
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(
            200, content=b' ' * (MAX_RESPONSE_BYTES + 1)))) as client:
        adapter = BrowserFlightSearch(client, store)
        place, origin, intent = trip()
        quote, rows, _ = await adapter.search(context_for('flight', place, origin, intent), intent)
    assert quote is None and rows[0]['diagnostic_code'] == 'flight_response_too_large'
    store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('mutation', ['content_type', 'length', 'future', 'http_error', 'redirect'])
async def test_transport_and_future_readback_fail_without_price(monkeypatch, tmp_path, mutation):
    monkeypatch.setenv('FLIGHT_SEARCH_URL', 'http://flight-search:8080')
    monkeypatch.setenv('WEB_FALLBACK_ENABLED', '1')
    requests = []
    def handler(request):
        requests.append(request)
        payload = envelope(request)
        headers = {}
        if mutation == 'content_type':
            headers['content-type'] = 'text/html'
        elif mutation == 'length':
            headers['content-length'] = str(MAX_RESPONSE_BYTES + 1)
        elif mutation == 'future':
            future = datetime.now(timezone.utc) + timedelta(seconds=10)
            payload['route'].update(observed_at=future.isoformat(), retrieved_at=future.isoformat(),
                expires_at=(future + timedelta(minutes=10)).isoformat())
        elif mutation == 'http_error':
            return httpx.Response(503)
        elif mutation == 'redirect':
            return httpx.Response(302, headers={'location': 'https://example.org/forbidden'})
        return httpx.Response(200, json=payload, headers=headers)
    store = ProviderCache(str(tmp_path / 'transport.db'))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = BrowserFlightSearch(client, store)
        place, origin, intent = trip()
        quote, rows, _ = await adapter.search(context_for('flight', place, origin, intent), intent)
    assert quote is None and len(requests) == 1
    assert rows[0]['diagnostic_code'] == ({'length': 'flight_response_too_large', 'future': 'flight_context_mismatch',
        'http_error': 'flight_source_unavailable', 'redirect': 'flight_source_unavailable'}.get(mutation, 'flight_parse_error'))
    assert store.db.execute('SELECT COUNT(*) FROM cache').fetchone()[0] == 0
    store.close()
