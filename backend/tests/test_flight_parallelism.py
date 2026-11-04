"""Controlled source barriers, not timing assumptions or live providers."""
import asyncio

import httpx
import pytest

from backend.app.geography import find_place
from backend.app.infrastructure.provider_cache import ProviderCache
from backend.app.models import TravelIntent
from backend.app.pricing.providers import unknown
from backend.app.pricing.flights import BrowserFlightSearch
from backend.app.providers import Providers
from backend.tests.test_flight_gateway import source_route


def trip():
    return find_place('Берлин'), find_place('Ларнака'), TravelIntent(
        origin='Ларнака', destination='Берлин', date_from='2026-10-30',
        date_to='2026-11-01', adults=2)


@pytest.mark.asyncio
async def test_flight_lookup_starts_before_lodging_discovery_finishes(monkeypatch, tmp_path):
    monkeypatch.setenv('KEYLESS_PRICING_ENABLED', '1')
    monkeypatch.setenv('WEB_FALLBACK_ENABLED', '1')
    monkeypatch.delenv('SERPAPI_API_KEY', raising=False)
    flight_started, lodging_started = asyncio.Event(), asyncio.Event()
    store = ProviderCache(str(tmp_path / 'parallel.db'))
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: pytest.fail('Unexpected HTTP'))) as client:
        providers = Providers(client, store)

        async def lodging(place, intent, stay, transfer):
            lodging_started.set()
            await flight_started.wait()
            stay.title = 'Independent source hotel'
            stay.details['catalog_property_id'] = 'synthetic-catalog-id'

        async def find(component, context, *args, **kwargs):
            assert component == 'flight'
            flight_started.set()
            await lodging_started.wait()
            return unknown('google_flights', context, 'Synthetic blocked flight', 'blocked'), [], None

        providers.real_places = lodging
        providers.pricing._find = find
        result = await asyncio.wait_for(providers.inventory(*trip()), 1)
        assert result[1].title == 'Independent source hotel'
        assert result[1].quote.provider_id == 'local_catalog'
        assert result[0].quote.outcome == 'blocked'
        assert flight_started.is_set() and lodging_started.is_set()
        assert not providers.pricing._inflight and not providers.pricing._waiters
    store.close()


@pytest.mark.asyncio
async def test_legacy_flight_source_cannot_delay_browser_branch(monkeypatch, tmp_path):
    monkeypatch.setenv('KEYLESS_PRICING_ENABLED', '1')
    monkeypatch.setenv('WEB_FALLBACK_ENABLED', '1')
    monkeypatch.setenv('SERPAPI_API_KEY', 'synthetic-credential-never-a-real-key')
    browser_started, legacy_started = asyncio.Event(), asyncio.Event()
    async def handler(request):
        if request.url.params['engine'] == 'google_flights':
            legacy_started.set()
            await browser_started.wait()
        return httpx.Response(200, json={})
    store = ProviderCache(str(tmp_path / 'legacy.db'))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        providers = Providers(client, store)
        async def lodging(*args):
            await browser_started.wait()
        async def find(component, context, *args, **kwargs):
            if component == 'flight':
                browser_started.set()
                await legacy_started.wait()
            return unknown('public_' + component, context, 'Synthetic unavailable'), [], None
        providers.real_places, providers.pricing._find = lodging, find
        await asyncio.wait_for(providers.inventory(*trip()), 1)
    assert browser_started.is_set() and legacy_started.is_set()
    store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('failed_branch', ['lodging', 'flight'])
async def test_known_source_failure_preserves_independent_component(monkeypatch, tmp_path, failed_branch):
    monkeypatch.setenv('KEYLESS_PRICING_ENABLED', '1')
    monkeypatch.setenv('WEB_FALLBACK_ENABLED', '1')
    monkeypatch.delenv('SERPAPI_API_KEY', raising=False)
    store = ProviderCache(str(tmp_path / 'failure.db'))
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: pytest.fail('Unexpected HTTP'))) as client:
        providers = Providers(client, store)
        async def lodging(place, intent, stay, transfer):
            if failed_branch == 'lodging':
                raise httpx.ReadError('Synthetic discovery failure')
            stay.title = 'Independent lodging source'
            stay.details['catalog_property_id'] = 'synthetic-catalog-id'
        async def find(component, context, *args, **kwargs):
            if component == 'flight' and failed_branch == 'flight':
                raise httpx.ReadError('Synthetic flight source failure')
            if component == 'flight':
                route = source_route()
                return BrowserFlightSearch._reference(route, context), [], {'route': route, 'delivery': 'live_fetch'}
            return unknown('public_stay', context, 'Synthetic unknown'), [], None
        providers.real_places, providers.pricing._find = lodging, find
        result = await providers.inventory(*trip())
    if failed_branch == 'lodging':
        assert result[0].quote.value.min == '1622'
        assert result[1].details['discovery_outcome'] == 'unavailable'
    else:
        assert result[1].title == 'Independent lodging source'
        assert result[0].quote.value.kind == 'unknown'
    assert not providers.pricing._inflight and not providers.pricing._waiters
    store.close()


@pytest.mark.asyncio
async def test_abandoned_inventory_awaits_both_source_branches(monkeypatch, tmp_path):
    monkeypatch.setenv('KEYLESS_PRICING_ENABLED', '1')
    monkeypatch.setenv('WEB_FALLBACK_ENABLED', '1')
    monkeypatch.delenv('SERPAPI_API_KEY', raising=False)
    flight_started, lodging_started = asyncio.Event(), asyncio.Event()
    flight_closed, lodging_closed = asyncio.Event(), asyncio.Event()
    store = ProviderCache(str(tmp_path / 'cancel.db'))
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: pytest.fail('Unexpected HTTP'))) as client:
        providers = Providers(client, store)

        async def lodging(*args):
            lodging_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                lodging_closed.set()

        async def find(component, *args, **kwargs):
            assert component == 'flight'
            flight_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                flight_closed.set()

        providers.real_places, providers.pricing._find = lodging, find
        task = asyncio.create_task(providers.inventory(*trip()))
        await asyncio.wait_for(asyncio.gather(flight_started.wait(), lodging_started.wait()), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert flight_closed.is_set() and lodging_closed.is_set()
        assert not providers.pricing._inflight and not providers.pricing._waiters
    store.close()
