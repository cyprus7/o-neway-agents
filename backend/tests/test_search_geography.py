"""Synthetic SERP fixtures: city of departure is not the lodging destination."""
from datetime import date

import httpx
import pytest

from backend.app.geography import PLACES, find_place
from backend.app.models import TravelIntent
from backend.app.providers import Providers, lodging_result_matches
from backend.app.storage import Store


@pytest.mark.asyncio
async def test_lodging_uses_destination_and_rejects_origin_hotels(tmp_path):
    queries = []
    def handler(request):
        queries.append(request.url.params['q'])
        return httpx.Response(200, json={'results': [
            {'title': 'Pet-Friendly Hotels in Warsaw', 'url': 'https://example.org/warsaw-hotels'},
            {'title': 'Larnaca pet friendly hotels', 'url': 'https://example.org/larnaca-hotels'},
            {'title': 'Flights Warsaw to Larnaca', 'url': 'https://example.org/warsaw-larnaca-flights'},
            {'title': 'Pet-Friendly Hotels in Antalya', 'url': 'https://example.org/antalya-hotels'},
            {'title': 'Hotels in Paphos', 'url': 'https://example.org/paphos-hotels', 'content': 'A Cyprus guide comparing Paphos with Larnaca'},
        ]})
    store = Store(str(tmp_path / 'search.db'))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        evidence = await Providers(client, store).search(PLACES[0], TravelIntent(
            origin='Варшава', pets=['dog'], date_from=date(2027, 6, 1), date_to=date(2027, 6, 8)))
    assert len(queries) == 2
    lodging = next(q for q in queries if 'hotel' in q)
    assert 'Варшава' not in lodging and 'Warsaw' not in lodging
    labels = ' '.join(item.label for item in evidence)
    assert 'Hotels in Warsaw' not in labels and 'Hotels in Antalya' not in labels and 'Hotels in Paphos' not in labels
    assert 'Larnaca pet friendly hotels' in labels
    assert 'Flights Warsaw to Larnaca' in labels
    store.close()


def test_cabin_requirement_goes_to_route_not_lodging_and_remains_unverified():
    from backend.app.providers import scoped_searches
    from backend.app.planner import constraint_checks
    from backend.app.models import TravelComponent
    intent = TravelIntent(origin='Варшава', pets=['кошка в переноске'],
        hotel_requirements=['тихий номер'],
        hard_constraints=['transport_requirement:кошка в салоне, не в багажном отсеке'])
    queries = dict(scoped_searches(PLACES[0], intent))
    assert 'кошка в салоне' in queries['transport']
    assert 'багажном отсеке' not in queries['lodging']
    assert 'тихий номер' in queries['lodging'] and 'тихий номер' not in queries['transport']
    component = TravelComponent(title='Synthetic fixture', description='No availability evidence')
    checks = constraint_checks(intent, PLACES[0], (component, component, component))
    check = next(row for row in checks if 'кошка в салоне' in row['constraint'])
    assert check['status'] == 'unknown' and not check['constraint'].startswith('transport_requirement:')


def test_unsupported_ferry_is_not_silently_a_flight():
    from backend.app.providers import transport_mode
    from backend.app.planner import blocking_question
    intent = TravelIntent(origin='Афины', destination='Санторини', date_from=date(2027, 6, 1),
        date_to=date(2027, 6, 8), budget_total=2500, hard_constraints=['transport:ferry'])
    assert transport_mode(intent) == 'unsupported'
    assert 'способ передвижения' in blocking_question(intent)


def test_simulation_policy_is_an_application_fact_not_unverified_hotel_policy():
    from backend.app.models import TravelComponent
    from backend.app.planner import constraint_checks
    component = TravelComponent(title='Synthetic fixture', description='No provider facts')
    checks = constraint_checks(TravelIntent(hard_constraints=['no_purchases', 'no_bookings_without_consent']),
        PLACES[0], (component, component, component))
    policy = [row for row in checks if row['constraint'] == 'Без реальной покупки']
    assert len(policy) == 1 and policy[0]['status'] == 'passed'
    assert any(row['status'] == 'unknown' and 'Наличие' in row['constraint'] for row in checks)


@pytest.mark.asyncio
async def test_indexed_hotel_fallback_rejects_wrong_country_or_city(tmp_path, monkeypatch):
    monkeypatch.delenv('SERPAPI_API_KEY', raising=False)
    def handler(request):
        if request.url.path == '/search':
            return httpx.Response(200, json={'results': [
                {'title': 'Pet-Friendly Hotel Warsaw', 'url': 'https://www.booking.com/hotel/pl/warsaw.html'},
                {'title': 'Paphos Beach Hotel', 'url': 'https://www.booking.com/hotel/cy/paphos.html'},
                {'title': 'Larnaca Family Hotel', 'url': 'https://www.booking.com/hotel/cy/larnaca.html'},
            ]})
        return httpx.Response(503)
    store = Store(str(tmp_path / 'fallback.db'))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        from backend.app.geography import find_place
        _, stay, _ = await Providers(client, store).inventory(PLACES[0], find_place('Варшава'),
            TravelIntent(origin='Варшава', pets=['dog'], date_from=date(2027, 6, 1), date_to=date(2027, 6, 8)))
    assert stay.title == 'Larnaca Family Hotel'
    assert stay.details['property_location_verified'] is False
    store.close()


@pytest.mark.parametrize('title,url,accepted', [
    ('Аэропорт Берлин-Бранденбург (BER) из Катовице в автобусы',
     'https://www.rome2rio.com/ru/Автобус/Аэропорт-Берлин-Бранденбург-BER/Катовице', False),
    ('Магнетичният Берлин – Германия', 'https://boiana-mg.bg/programa/berlin-vyatarat', False),
    ('Berlin tour with hotels and flights', 'https://tour.example/tours/berlin-hotels', False),
    ('Berlin concert tickets', 'https://events.example/berlin', False),
    ('Berlin Airport Hotel and shuttle', 'https://hotel.example/berlin-airport-hotel', True),
    ('Berlin hotels near the station', 'https://lodging.example/berlin-hotels', True),
    ('Hotel Indigo Berlin East Side Gallery', 'https://www.booking.com/hotel/de/indigo-berlin.html', True),
    ('Berlin Airport Hotel', 'https://www.booking.com/hotel/pl/berlin.html', False),
])
def test_lodging_requires_page_class_as_well_as_destination(title, url, accepted):
    assert lodging_result_matches({'title': title, 'url': url}, find_place('Berlin'), 'Larnaca') is accepted


@pytest.mark.asyncio
async def test_concrete_discovery_uses_lodging_wishes_and_preserves_supported_source_identity(tmp_path, monkeypatch):
    monkeypatch.delenv('OLLAMA_API_KEY', raising=False)
    queries = []
    path = '/hotel/germany/berlin/mid8876313/hotel_indigo_berlin_east_side_gallery/'
    def handler(request):
        queries.append(request.url.params['q'])
        return httpx.Response(200, json={'results': [
            {'title': 'Berlin Hotel fixture - Booking.com', 'url': 'https://www.booking.com/hotel/de/fixture.html'},
            {'title': 'Berlin Hotel fixture - Ostrovok.ru', 'url': 'https://ostrovok.ru' + path},
        ]})
    intent = TravelIntent(origin='Larnaca', destination='Berlin', date_from='2026-10-30', date_to='2026-11-01',
        adults=2, hotel_requirements=['quiet private room', 'no hostel'],
        hard_constraints=['transport_requirement:direct flight only'])
    from backend.app.models import TravelComponent
    from price_worker.recipes.ostrovok import ostrovok_property_id
    stay = TravelComponent(title='Search', description='Fixture', source_url='https://www.booking.com/searchresults.html')
    store = Store(str(tmp_path / 'wishes.db'))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await Providers(client, store).hotel_search_fallback(find_place('Berlin'), intent, stay)
    assert queries and all('quiet private room' in query and 'no hostel' in query for query in queries)
    assert all('direct flight only' not in query and 'Larnaca' not in query for query in queries)
    assert stay.source_url == 'https://ostrovok.ru' + path
    assert stay.details['pricing_entity_id'] == ostrovok_property_id(path)
    assert stay.details['requirements_verified'] is False
    assert stay.quote is None and stay.price is None
    store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('wishes,expected_slug', [
    (['no hostels'], 'quiet_hotel'),
    (['без хостелов'], 'quiet_hotel'),
    (['avoid hostels'], 'quiet_hotel'),
    (['not avoid hostels'], 'budget_hostel'),
    (['do not exclude hostels'], 'budget_hostel'),
    (['не избегать хостелов'], 'budget_hostel'),
    (['quiet private room'], 'budget_hostel'),
])
async def test_initial_indexed_candidates_apply_explicit_kind_negation_before_identity(tmp_path, monkeypatch, wishes, expected_slug):
    monkeypatch.delenv('OLLAMA_API_KEY', raising=False)
    prefix = 'https://ostrovok.ru/hotel/germany/berlin/'
    def handler(request):
        assert request.url.path == '/search', 'Only mocked discovery, no worker or model'
        return httpx.Response(200, json={'results': [
            {'title': 'Berlin Budget Hostel', 'url': prefix + 'mid111/budget_hostel/'},
            {'title': 'Berlin Quiet Hotel', 'url': prefix + 'mid222/quiet_hotel/'},
        ]})
    async def original_order(rows, query):
        return rows
    from backend.app.models import TravelComponent
    from price_worker.recipes.ostrovok import ostrovok_property_id
    intent = TravelIntent(origin='Larnaca', destination='Berlin', date_from='2026-10-30', date_to='2026-11-01',
        adults=2, hotel_requirements=wishes)
    stay = TravelComponent(title='Search', description='Synthetic fixture')
    store = Store(str(tmp_path / 'explicit-kind.db'))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        providers = Providers(client, store)
        providers.ranker.rank_rows = original_order
        await providers.hotel_search_fallback(find_place('Berlin'), intent, stay)
    assert stay.source_url.endswith('/' + expected_slug + '/')
    expected_path = '/hotel/germany/berlin/' + ('mid222/' if expected_slug == 'quiet_hotel' else 'mid111/') + expected_slug + '/'
    assert stay.details['pricing_entity_id'] == ostrovok_property_id(expected_path)
    assert stay.details['requirements_verified'] is False and stay.quote is None
    assert intent.hotel_requirements == wishes
    store.close()


@pytest.mark.asyncio
async def test_initial_osm_candidate_uses_same_explicit_kind_filter(tmp_path):
    def handler(request):
        assert request.url.path == '/api/interpreter', 'Only mocked OSM discovery'
        return httpx.Response(200, json={'elements': [
            {'type': 'node', 'id': 111, 'lat': 52.52, 'lon': 13.40,
             'tags': {'tourism': 'hotel', 'name': 'A Berlin Budget Hostel'}},
            {'type': 'node', 'id': 222, 'lat': 52.52, 'lon': 13.41,
             'tags': {'tourism': 'hotel', 'name': 'Berlin Quiet Hotel'}},
        ]})
    from backend.app.models import TravelComponent
    intent = TravelIntent(origin='Larnaca', destination='Berlin', date_from='2026-10-30', date_to='2026-11-01',
        adults=2, hotel_requirements=['без хостелов'])
    stay, transfer = [TravelComponent(title='Synthetic fixture', description='Synthetic fixture') for _ in range(2)]
    store = Store(str(tmp_path / 'osm-explicit-kind.db'))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await Providers(client, store).real_places(find_place('Berlin'), intent, stay, transfer)
    assert stay.title == 'Berlin Quiet Hotel'
    assert stay.source_url.endswith('/node/222') and stay.quote is None
    assert stay.details['availability_verified'] is False
    store.close()
