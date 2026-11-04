"""Fixed source route refresh and source singleflight using synthetic HTTP."""
import asyncio
import json
from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest

from backend.app.infrastructure.provider_cache import ProviderCache
from backend.app.models import TravelComponent
from backend.app.pricing.providers import KeylessPricing
from backend.app.pricing.aggregation import summarize_prices
from backend.tests.test_flight_gateway import envelope, source_route, trip


def parts():
    return [TravelComponent(title=name, description='Synthetic initial component',
        details={'transport_mode': 'flight'} if name == 'flight' else {}) for name in ('flight', 'stay', 'transfer')]


def setup(monkeypatch, client, store):
    monkeypatch.setenv('KEYLESS_PRICING_ENABLED', '1')
    monkeypatch.setenv('WEB_FALLBACK_ENABLED', '1')
    monkeypatch.setenv('FLIGHT_SEARCH_URL', 'http://flight-search:8080')
    monkeypatch.delenv('PRICE_WORKER_URL', raising=False)
    router = KeylessPricing(client, store)
    async def no_airline(*args):
        return {'facts': [], 'diagnostics': []}
    router.airlines = SimpleNamespace(search=no_airline)
    return router


@pytest.mark.asyncio
async def test_browser_price_binds_whole_observed_route_without_airline_price_transplant(monkeypatch, tmp_path):
    route = source_route()
    store = ProviderCache(str(tmp_path / 'chosen.db'))
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(
            200, json=envelope(request, route)))) as client:
        router = setup(monkeypatch, client, store)
        async def cheap_airline(*args):
            return {'facts': [{'provider_id': 'aegean_public', 'scope': {'origin_iata': 'LCA',
                'destination_iata': 'BER', 'date_from': '2026-10-30', 'date_to': '2026-11-01'},
                'trip_type': 'roundtrip', 'amount': '19', 'currency': 'EUR',
                'source_url': 'https://flights.aegeanair.com/en/flights-from-larnaca-to-berlin',
                'observed_at': route.observed_at.isoformat(), 'retrieved_at': route.retrieved_at.isoformat(),
                'expires_at': route.expires_at.isoformat(), 'message': 'Synthetic unrelated airline reference'}], 'diagnostics': []}
        router.airlines.search = cheap_airline
        current = parts()
        current[0].price = 999
        current[0].details.update(arrival_at='2026-10-30T10:00:00+00:00', segments=5)
        await router.apply(*trip(), current, only=('flight',))
    flight = current[0]
    assert flight.quote.provider_id == 'google_flights' and flight.quote.value.min == '1622'
    assert flight.quote.normalized_group is None and flight.price is None
    assert flight.details['pricing_entity_id'] == 'google-flight:' + route.itinerary_id
    assert flight.details['flight_route']['inbound']['arrival_date'] == '2026-11-02'
    assert flight.details['duration_minutes'] == 195 and flight.details['direct_flight_verified'] is True
    assert 'arrival_at' not in flight.details and 'segments' not in flight.details and '2026-11-02' in flight.description
    assert flight.source_url == route.source_url and [row.provider for row in flight.evidence] == ['google_flights']
    assert not flight.details['connections_verified']
    summary = summarize_prices({'flight': flight.quote, 'stay': None, 'transfer': None}, currency='EUR', budget='5000')
    assert summary.full_trip is None and summary.hard_constraint_status != 'passed'
    store.close()


@pytest.mark.asyncio
async def test_malformed_optional_airline_result_cannot_erase_browser_route(monkeypatch, tmp_path):
    store = ProviderCache(str(tmp_path / 'malformed-airline.db'))
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(
            200, json=envelope(request)))) as client:
        router = setup(monkeypatch, client, store)
        async def malformed(*args):
            return {'facts': [{'missing': 'source scope'}]}
        router.airlines.search = malformed
        current = parts()
        await router.apply(*trip(), current, only=('flight',))
    assert current[0].quote.provider_id == 'google_flights' and current[0].quote.value.min == '1622'
    assert any(row['diagnostic_code'] == 'source_parse_error' for row in current[0].details['pricing_outcomes'])
    store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['blocked', 'different_route'])
async def test_failed_fixed_refresh_retains_same_route_and_original_times(monkeypatch, tmp_path, failure):
    calls = []
    original = source_route()
    def handler(request):
        body = json.loads(request.content)
        calls.append(body)
        if len(calls) == 1:
            return httpx.Response(200, json=envelope(request, original))
        if failure == 'different_route':
            return httpx.Response(200, json=envelope(request, source_route(departure_time='07:15')))
        return httpx.Response(200, json={'version': 'flight-search.v1', 'request_id': body['request_id'],
            'request_context_hash': body['request_context_hash'], 'outcome': 'blocked',
            'diagnostic_code': 'flight_challenge', 'diagnostics': [{'outcome': 'blocked',
                'diagnostic_code': 'flight_challenge', 'checked_at': original.retrieved_at.isoformat(),
                'retrieved_at': original.retrieved_at.isoformat(), 'dispatched': True, 'external_dispatched': True}]})
    store = ProviderCache(str(tmp_path / 'retain.db'))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        router = setup(monkeypatch, client, store)
        current = parts()
        await router.apply(*trip(), current, only=('flight',))
        first, first_route = current[0].quote, current[0].details['flight_route']
        store.db.execute('DELETE FROM cache')
        store.db.commit()
        async def forbidden(*args):
            pytest.fail('Fixed itinerary cannot fall back to generic airline fares')
        router.airlines.search = forbidden
        await router.apply(*trip(), current, refresh=True, only=('flight',))
    assert len(calls) == 2 and calls[1]['target_itinerary_id'] == original.itinerary_id
    assert calls[1]['request_context']['target_policy'] == 'fixed_entity'
    assert current[0].quote == first and current[0].details['flight_route'] == first_route
    assert current[0].quote.retrieved_at == original.retrieved_at
    assert current[0].quote.expires_at == original.expires_at
    assert current[0].details['pricing_refresh_warning']
    assert current[0].details['pricing_observation_delivery'] == 'cache_reuse'
    assert current[0].details['pricing_delivery'] == 'mixed'
    assert [row.provider for row in current[0].evidence] == ['google_flights']
    store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['date', 'party', 'duration', 'direct'])
async def test_failed_refresh_does_not_retain_incompatible_old_flight(monkeypatch, tmp_path, change):
    route = source_route(stops=1)
    count = 0
    def handler(request):
        nonlocal count
        count += 1
        if count == 1:
            return httpx.Response(200, json=envelope(request, route))
        return httpx.Response(503)
    store = ProviderCache(str(tmp_path / 'changed.db'))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        router = setup(monkeypatch, client, store)
        place, origin, intent = trip()
        current = parts()
        await router.apply(place, origin, intent, current, only=('flight',))
        store.db.execute('DELETE FROM cache')
        store.db.commit()
        intent = intent.model_copy(update={'date_to': intent.date_to + timedelta(days=1)} if change == 'date'
            else {'adults': 1} if change == 'party' else {'max_travel_minutes': 100} if change == 'duration'
            else {'hard_constraints': ['direct_flight']})
        await router.apply(place, origin, intent, current, refresh=True, only=('flight',))
    assert current[0].quote.value.kind == 'unknown'
    assert 'pricing_refresh_warning' not in current[0].details
    assert 'flight_route' not in current[0].details and 'duration_minutes' not in current[0].details
    assert not current[0].details['direct_flight_verified']
    assert not current[0].evidence
    store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('different_preferences', [False, True])
async def test_singleflight_is_scoped_to_all_flight_preferences(monkeypatch, tmp_path, different_preferences):
    calls = []
    started, release = asyncio.Event(), asyncio.Event()
    async def handler(request):
        calls.append(json.loads(request.content))
        started.set()
        await release.wait()
        return httpx.Response(200, json=envelope(request))
    store = ProviderCache(str(tmp_path / 'dedupe.db'))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        router = setup(monkeypatch, client, store)
        first, second = parts(), parts()
        place, origin, intent = trip()
        other = intent.model_copy(update={'direct_flight_preferred': True}) if different_preferences else intent
        tasks = [asyncio.create_task(router.apply(place, origin, intent, first, only=('flight',))),
                 asyncio.create_task(router.apply(place, origin, other, second, only=('flight',)))]
        try:
            await asyncio.wait_for(started.wait(), 1)
            async with asyncio.timeout(1):
                while sum(router._waiters.values()) < 2 or different_preferences and len(calls) < 2:
                    await asyncio.sleep(0)
            assert len(calls) == (2 if different_preferences else 1)
            release.set()
            await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        assert not router._inflight and not router._waiters
    assert first[0].quote.value.min == second[0].quote.value.min == '1622'
    assert (first[0].quote.id == second[0].quote.id) is not different_preferences
    store.close()


@pytest.mark.asyncio
async def test_only_last_waiter_cancels_private_source_request(monkeypatch, tmp_path):
    started, closed = asyncio.Event(), asyncio.Event()
    async def handler(request):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            closed.set()
    store = ProviderCache(str(tmp_path / 'last-waiter.db'))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        router = setup(monkeypatch, client, store)
        place, origin, intent = trip()
        first = asyncio.create_task(router.apply(place, origin, intent, parts(), only=('flight',)))
        second = asyncio.create_task(router.apply(place, origin, intent, parts(), only=('flight',)))
        try:
            await asyncio.wait_for(started.wait(), 1)
            async with asyncio.timeout(1):
                while sum(router._waiters.values()) < 2:
                    await asyncio.sleep(0)
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first
            assert not closed.is_set()
            second.cancel()
            with pytest.raises(asyncio.CancelledError):
                await second
            assert closed.is_set() and not router._inflight and not router._waiters
        finally:
            first.cancel()
            second.cancel()
            await asyncio.gather(first, second, return_exceptions=True)
    assert store.metrics()['flight_search_requests'] == 1
    store.close()
