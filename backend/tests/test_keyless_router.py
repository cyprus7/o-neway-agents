"""Synthetic adapter integration; no keys, inference or live internet."""
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest

from backend.app.geography import find_place
from backend.app.models import TravelComponent, TravelIntent
from backend.app.pricing.aggregation import summarize_prices
from backend.app.pricing.providers import KeylessPricing, context_for
from backend.app.pricing.providers import unknown
from backend.app.pricing.models import PriceObservation
from backend.app.pricing.normalization import request_context_hash
from backend.app.storage import Store
from backend.tests.test_public_lodging_prices import APOLLO
from backend.app.pricing.lodging import parse_apollo, tariff_fact
from price_worker.recipes.lodging import reference_observation


def trip():
    intent = TravelIntent(origin='Ларнака', date_from='2026-10-25', date_to='2026-11-01', adults=1, budget_total=500)
    parts = [TravelComponent(title=name, description='Synthetic fixture', price=999,
        details={'transport_mode':'flight'} if name=='flight' else {'old_osm_id':123,'latitude':34.5}) for name in ('flight','stay','transfer')]
    return find_place('Афины'), find_place('Ларнака'), intent, parts


def fare():
    now = datetime.now(timezone.utc)
    return {'provider_id':'aegean_public', 'scope':{'origin_iata':'LCA','destination_iata':'ATH',
        'date_from':'2026-10-25','date_to':'2026-11-01'}, 'trip_type':'roundtrip','amount':'219.17',
        'currency':'EUR','source_url':'https://flights.aegeanair.com/en/flights-from-larnaca-to-athens',
        'observed_at':now.isoformat(),'retrieved_at':now.isoformat(),'expires_at':(now+timedelta(minutes=10)).isoformat(),
        'message':'Synthetic published fare', 'delivery':'cached'}


def setup(monkeypatch, tmp_path, handler=None):
    monkeypatch.setenv('KEYLESS_PRICING_ENABLED','1')
    monkeypatch.delenv('PRICE_WORKER_URL',raising=False)
    store=Store(str(tmp_path/'router.db'))
    client=httpx.AsyncClient(transport=httpx.MockTransport(handler or (lambda _: httpx.Response(503))))
    router=KeylessPricing(client,store)
    async def airlines(*args):
        return {'facts':[fare()], 'diagnostics':[]}
    async def hotels(*args):
        return tariff_fact('ATH',parse_apollo(APOLLO),datetime.now(timezone.utc).isoformat(),
            '2026-10-25','2026-11-01',1,[],1)
    router.airlines=SimpleNamespace(search=airlines)
    router.hotels=SimpleNamespace(search=hotels)
    return router,store,client


def test_keyless_numbers_survive_worker_absence_without_false_group_total(monkeypatch,tmp_path):
    async def run():
        router,store,client=setup(monkeypatch,tmp_path)
        try:
            place,origin,intent,parts=trip()
            parts[0].description='Цена билетов пока не получена.'
            await router.apply(place,origin,intent,parts)
            assert parts[0].quote.value.min=='219.17'
            assert 'пока не получена' not in parts[0].description
            assert 'Synthetic published fare' in parts[0].description
            assert parts[1].quote.value.amount=='60'
            assert all(part.quote.normalized_group is None for part in parts[:2])
            assert parts[0].details['pricing_delivery']=='cache_reuse'
            summary=summarize_prices({'flight':parts[0].quote,'stay':parts[1].quote,'transfer':None},currency='EUR',budget='500')
            assert summary.full_trip is None and summary.transport_stay is None and summary.known_partial is None
            assert parts[1].title=='Apollo Hotel Athens'
            assert 'old_osm_id' not in parts[1].details and 'latitude' not in parts[1].details
            assert parts[1].price is None and parts[2].price is None and parts[2].quote is None
            assert parts[2].status=='unavailable' and 'latitude' not in parts[2].details
        finally:
            store.close()
            await client.aclose()
    asyncio.run(run())


def test_ground_transport_does_not_fetch_or_show_airline_prices(monkeypatch,tmp_path):
    async def run():
        router,store,client=setup(monkeypatch,tmp_path)
        async def forbidden(*args):
            raise AssertionError('Train must not be replaced by air fare')
        router.airlines.search=forbidden
        place,origin,intent,parts=trip()
        intent.hard_constraints=['transport:train']
        parts[0].details['transport_mode']='train'
        await router.apply(place,origin,intent,parts)
        assert parts[0].quote is None and parts[0].details['transport_mode']=='train'
        store.close()
        await client.aclose()
    asyncio.run(run())


def test_refresh_fixed_property_rejects_alternative_hotel_tariff(monkeypatch,tmp_path):
    async def run():
        router,store,client=setup(monkeypatch,tmp_path)
        place,origin,intent,parts=trip()
        parts[1].details['pricing_entity_id']='https://other.example/hotel'
        await router.apply(place,origin,intent,parts,refresh=True)
        assert parts[1].quote.outcome=='context_mismatch'
        assert parts[1].title=='stay' and parts[2].price==999
        store.close()
        await client.aclose()
    asyncio.run(run())


def test_concurrent_identical_contexts_share_source_work(monkeypatch,tmp_path):
    async def run():
        router,store,client=setup(monkeypatch,tmp_path)
        calls=[]
        async def airlines(*args):
            calls.append(args)
            await asyncio.sleep(0.01)
            return {'facts':[fare()], 'diagnostics':[]}
        router.airlines.search=airlines
        place,origin,intent,parts=trip()
        other=trip()[3]
        await asyncio.gather(router.apply(place,origin,intent,parts),router.apply(place,origin,intent,other))
        assert len(calls)==1 and parts[0].quote.id==other[0].quote.id
        assert not router._inflight
        store.close()
        await client.aclose()
    asyncio.run(run())


def test_browser_raw_reference_is_cached_without_resetting_source_time(monkeypatch,tmp_path):
    async def run():
        calls=[]
        def handler(request):
            import json
            calls.append(request)
            assert 'authorization' not in request.headers
            payload=json.loads(request.content)
            obs=reference_observation(context=payload['request_context'],property_name='Synthetic Hotel',
                source_url='https://www.google.com/travel/search?q=Athens',total='83',
                room_label='Bed in Mixed Dormitory Room',room_guests='1 guest')
            return httpx.Response(200,json={'request_context_hash':payload['request_context_hash'],
                'outcome':'reference','observation':obs})
        router,store,client=setup(monkeypatch,tmp_path,handler)
        router.worker_url='http://price-worker:8080'
        place,origin,intent,parts=trip()
        context=context_for('stay',place,origin,intent)
        quote,_=await router._browser(context,place)
        cached,diagnostics=await router._browser(context,place)
        assert quote.value.min=='83' and quote.normalized_group is None
        assert quote.observed_at==cached.observed_at and quote.expires_at==cached.expires_at
        assert len(calls)==1 and diagnostics[0]['delivery']=='cache_reuse'
        assert cached.request_context_hash==request_context_hash(context)
        store.close()
        await client.aclose()
    asyncio.run(run())


def test_corrupt_browser_cache_falls_back_to_live_failure(monkeypatch,tmp_path):
    async def run():
        router,store,client=setup(monkeypatch,tmp_path)
        router.worker_url='http://price-worker:8080'
        place,origin,intent,_=trip()
        context=context_for('stay',place,origin,intent)
        store.cache_put('google-hotels-v2:'+request_context_hash(context)+':'+place.name.casefold(),{'broken':True},60)
        quote,_=await router._browser(context,place)
        assert quote.value.kind=='unknown' and quote.outcome=='unavailable'
        store.close()
        await client.aclose()
    asyncio.run(run())


@pytest.mark.parametrize('change',[{'date_from':'2026-10-24'}, {'destination':'WAW'},
    {'adults':2}, {'entity_id':'google-hotel:other'}])
def test_browser_reference_cannot_hide_a_known_context_mismatch(monkeypatch,tmp_path,change):
    async def run():
        import json
        def handler(request):
            payload=json.loads(request.content)
            obs=reference_observation(context=payload['request_context'],property_name='Synthetic Hotel',
                source_url='https://www.google.com/travel/search',total='83',room_label='Double Room',room_guests='2 guests')
            obs['scope'].update(change)
            return httpx.Response(200,json={'request_context_hash':payload['request_context_hash'],
                'outcome':'reference','observation':obs})
        router,store,client=setup(monkeypatch,tmp_path,handler)
        router.worker_url='http://price-worker:8080'
        place,origin,intent,_=trip()
        context=context_for('stay',place,origin,intent,entity_id='fixed-hotel' if 'entity_id' in change else None)
        quote,_=await router._browser(context,place)
        assert quote.value.kind=='unknown' and quote.outcome=='context_mismatch'
        store.close()
        await client.aclose()
    asyncio.run(run())


@pytest.mark.parametrize('expired',[False,True])
def test_failed_fixed_property_refresh_retains_previous_observation_without_renewal(monkeypatch,tmp_path,expired):
    async def run():
        router,store,client=setup(monkeypatch,tmp_path)
        calls=[]
        async def find(component,context,*args):
            if component=='flight':
                return unknown('public_airlines',context,'No synthetic flight'),[],None
            calls.append(context)
            if len(calls)>1:
                return unknown('google_hotels',context,'Synthetic source outage','timeout'),[],None
            data=reference_observation(context={'destination_iata':'ATH','date_from':'2026-10-25',
                'date_to':'2026-11-01','adults':1},property_name='Synthetic Hotel',
                source_url='https://www.google.com/travel/search',total='281',room_label='Double Room',room_guests='2 guests')
            data['request_context_hash']=request_context_hash(context)
            return PriceObservation.model_validate(data),[],None
        router._find=find
        place,origin,intent,parts=trip()
        await router.apply(place,origin,intent,parts)
        if expired:
            clock=datetime.now(timezone.utc)
            parts[1].quote=parts[1].quote.model_copy(update={'observed_at':clock-timedelta(minutes=20),
                'retrieved_at':clock-timedelta(minutes=20),'expires_at':clock-timedelta(minutes=5)})
        first=parts[1].quote
        await router.apply(place,origin,intent,parts,refresh=True)
        assert calls[1].target_policy=='fixed_entity'
        assert parts[1].quote.id==first.id and parts[1].quote.observed_at==first.observed_at
        assert parts[1].quote.expires_at==first.expires_at
        assert (parts[1].quote.freshness()=='stale')==expired
        assert parts[1].details['pricing_refresh_warning']
        assert parts[1].details['pricing_delivery']=='cache_reuse'
        store.close()
        await client.aclose()
    asyncio.run(run())


def test_lodging_request_identity_distinguishes_cities_using_one_airport():
    place,origin,intent,_=trip()
    nearby=SimpleNamespace(name='A different town',airport=place.airport)
    assert request_context_hash(context_for('stay',place,origin,intent)) != request_context_hash(context_for('stay',nearby,origin,intent))
    assert request_context_hash(context_for('flight',place,origin,intent)) == request_context_hash(context_for('flight',nearby,origin,intent))


@pytest.mark.parametrize('deliveries,expected', [
    (['cache_reuse', 'cache_reuse'], 'cache_reuse'),
    (['cache_reuse', 'live_fetch'], 'mixed'),
    (['not_attempted', 'not_attempted'], 'not_attempted'),
])
def test_empty_facts_use_actual_attempt_delivery(monkeypatch, tmp_path, deliveries, expected):
    async def run():
        router, store, client = setup(monkeypatch, tmp_path)
        async def no_facts(*args):
            return {'facts': [], 'diagnostics': [
                {'provider_id': 'aegean_public', 'outcome': 'no_offers', 'diagnostic_code': 'no_matching_date_fare',
                 'delivery': delivery, 'checked_at': '2026-10-25T21:00:00Z', 'message': 'No matching published fare'}
                for delivery in deliveries]}
        router.airlines.search = no_facts
        place, origin, intent, parts = trip()
        await router.apply(place, origin, intent, parts)
        assert parts[0].details['pricing_delivery'] == expected
        assert all(row['checked_at'] == '2026-10-25T21:00:00Z' for row in parts[0].details['pricing_outcomes'])
        assert parts[0].quote.value.kind == 'unknown'
        store.close()
        await client.aclose()
    asyncio.run(run())


def test_booking_only_refresh_is_fixed_without_unrelated_browser_request(monkeypatch, tmp_path):
    async def run():
        router, store, client = setup(monkeypatch, tmp_path, lambda _: (_ for _ in ()).throw(AssertionError('No mapped browser identity')))
        router.worker_url = 'http://price-worker:8080'
        place, origin, intent, parts = trip()
        parts[1].source_url = 'https://www.booking.com/hotel/gr/fixture.html'
        parts[1].details = {'link_type': 'indexed_hotel_page', 'requirements_verified': False}
        await router.apply(place, origin, intent, parts, refresh=True)
        assert parts[1].source_url == 'https://www.booking.com/hotel/gr/fixture.html'
        assert parts[1].title == 'stay' and parts[1].quote.value.kind == 'unknown'
        outcomes = parts[1].details['pricing_outcomes']
        assert outcomes[0]['diagnostic_code'] == 'unsupported_property_mapping'
        assert outcomes[0]['delivery'] == 'not_attempted'
        assert parts[1].details['pricing_delivery'] == 'not_attempted'
        store.close()
        await client.aclose()
    asyncio.run(run())


def test_browser_skipped_coverage_preserves_finite_reason_and_no_dispatch(monkeypatch, tmp_path):
    async def run():
        import json
        def handler(request):
            body = json.loads(request.content)
            return httpx.Response(200, json={'request_context_hash': body['request_context_hash'],
                'outcome': 'not_configured', 'diagnostic_code': 'unsupported_destination'})
        router, store, client = setup(monkeypatch, tmp_path, handler)
        router.worker_url = 'http://price-worker:8080'
        place, origin, intent, _ = trip()
        quote, diagnostics = await router._browser(context_for('stay', place, origin, intent), place)
        assert quote.outcome == 'not_configured'
        assert diagnostics[0]['diagnostic_code'] == 'unsupported_destination'
        assert diagnostics[0]['delivery'] == 'not_attempted' and diagnostics[0]['dispatched'] is True
        assert diagnostics[0]['dispatch_scope'] == 'worker' and diagnostics[0]['external_dispatched'] is False
        store.close()
        await client.aclose()
    asyncio.run(run())


def test_browser_cache_does_not_reuse_candidate_search_with_different_wishes(monkeypatch, tmp_path):
    async def run():
        import json
        received = []
        def handler(request):
            body = json.loads(request.content)
            received.append(body['request_context']['discovery_terms'])
            observation = reference_observation(context=body['request_context'], property_name='Synthetic Hotel',
                source_url='https://www.google.com/travel/search', total='83', room_label='Private room', room_guests='2 guests')
            return httpx.Response(200, json={'request_context_hash': body['request_context_hash'],
                'outcome': 'reference', 'observation': observation})
        router, store, client = setup(monkeypatch, tmp_path, handler)
        router.worker_url = 'http://price-worker:8080'
        place, origin, intent, _ = trip()
        context = context_for('stay', place, origin, intent)
        first, _ = await router._browser(context, place, discovery_terms=('no hostel',))
        await router._browser(context, place, discovery_terms=('quiet room',))
        cached, outcomes = await router._browser(context, place, discovery_terms=('no hostel',))
        assert received == [['no hostel'], ['quiet room']]
        assert cached.id == first.id and cached.observed_at == first.observed_at
        assert outcomes[0]['delivery'] == 'cache_reuse' and outcomes[0]['dispatched'] is False
        store.close()
        await client.aclose()
    asyncio.run(run())


def test_bounded_lodging_terms_keep_negations_and_drop_transport_urls_and_overlong_fragments():
    from backend.app.pricing.providers import lodging_discovery_terms
    intent = SimpleNamespace(hotel_requirements=['no hostel', 'transport_requirement:direct flight only',
        'https://private.example/query', 'x' * 81, 'quiet private room'], pets=['dog'])
    assert lodging_discovery_terms(intent) == ('no hostel', 'quiet private room', 'pet friendly')


def test_gateway_lodging_term_filter_matches_worker_unicode_and_bare_url_policy():
    from backend.app.pricing.providers import lodging_discovery_terms
    from price_worker.recipes.lodging import valid_discovery_terms
    terms = lodging_discovery_terms(SimpleNamespace(hotel_requirements=['quiet\u200b room', 'hotel.example',
        'see hotel.example/prices', 'no hostel', 'quiet private room'], pets=[]))
    assert terms == ('no hostel', 'quiet private room')
    assert valid_discovery_terms(list(terms))


def test_legacy_direct_hotel_live_delivery_is_explicit():
    from backend.app.pricing.providers import pricing_delivery
    assert pricing_delivery([], source_delivery='live') == 'live_fetch'


def test_concurrent_candidate_lookups_do_not_deduplicate_different_lodging_wishes(monkeypatch, tmp_path):
    async def run():
        router, store, client = setup(monkeypatch, tmp_path)
        wishes = []
        async def find(component, context, place, origin, intent):
            if component == 'stay':
                wishes.append(tuple(intent.hotel_requirements))
                await asyncio.sleep(0.01)
            return unknown('public_' + component, context, 'Synthetic source result'), [], None
        router._find = find
        place, origin, first_intent, first_parts = trip()
        first_intent.hotel_requirements = ['no hostel']
        second_intent = first_intent.model_copy(update={'hotel_requirements': ['quiet room']})
        second_parts = trip()[3]
        await asyncio.gather(router.apply(place, origin, first_intent, first_parts),
                             router.apply(place, origin, second_intent, second_parts))
        assert sorted(wishes) == [('no hostel',), ('quiet room',)]
        assert not router._inflight and not router._waiters
        store.close()
        await client.aclose()
    asyncio.run(run())


def test_retained_observation_stays_cached_when_failed_refresh_attempt_is_live(monkeypatch, tmp_path):
    async def run():
        router, store, client = setup(monkeypatch, tmp_path)
        stay_calls = 0
        async def find(component, context, *args):
            nonlocal stay_calls
            if component == 'flight':
                return unknown('public_airlines', context, 'Synthetic no fare'), [], None
            stay_calls += 1
            if stay_calls == 1:
                data = reference_observation(context={'destination_iata': 'ATH', 'destination': 'Афины',
                    'date_from': '2026-10-25', 'date_to': '2026-11-01', 'adults': 1}, property_name='Synthetic Hotel',
                    source_url='https://www.google.com/travel/search', total='281', room_label='Private room', room_guests='2 guests')
                data['request_context_hash'] = request_context_hash(context)
                return PriceObservation.model_validate(data), [{'provider_id': 'google_hotels', 'delivery': 'live_fetch'}], None
            return unknown('google_hotels', context, 'Synthetic worker timeout', 'timeout'), [
                {'provider_id': 'google_hotels', 'delivery': 'live_fetch', 'outcome': 'timeout'}], None
        router._find = find
        place, origin, intent, parts = trip()
        await router.apply(place, origin, intent, parts)
        before = parts[1].quote.model_dump_json()
        await router.apply(place, origin, intent, parts, refresh=True)
        assert parts[1].quote.model_dump_json() == before
        assert parts[1].details['pricing_delivery'] == 'mixed'
        assert parts[1].details['pricing_observation_delivery'] == 'cache_reuse'
        assert parts[1].evidence[-1].freshness == 'cached'
        assert parts[1].details['pricing_refresh_warning']
        store.close()
        await client.aclose()
    asyncio.run(run())
