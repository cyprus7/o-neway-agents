"""Synthetic routing invariants for the real-browser provider contract."""
import asyncio
import json
from types import SimpleNamespace

import httpx

from backend.app.geography import find_place
from backend.app.models import TravelComponent, TravelIntent
from backend.app.pricing.providers import KeylessPricing, context_for
from backend.app.storage import Store
from backend.tests.test_ostrovok_recipe import CONTEXT, PATH, groups
from price_worker.recipes.ostrovok import parse_rate_groups, reference_observation, select_property
from price_worker.recipes.ostrovok import ostrovok_property_id


def test_empty_photo_anchor_does_not_hide_later_named_property_anchor():
    links = [{"href": "https://ostrovok.ru" + PATH, "label": ""},
             {"href": "https://ostrovok.ru" + PATH, "label": "Marina Hotel"}]
    assert select_property(links, "Athens")["label"] == "Marina Hotel"


def test_ostrovok_first_reference_resets_old_property_and_failed_refresh_keeps_identity(tmp_path, monkeypatch):
    monkeypatch.setenv("KEYLESS_PRICING_ENABLED", "1")
    monkeypatch.setenv("PRICE_WORKER_URL", "http://price-worker:8080")
    requests = []
    substitute = False

    def worker(request):
        body = json.loads(request.content)
        requests.append(body)
        assert body["provider_id"] == "ostrovok"
        path = "/hotel/greece/athens/mid7485633/plaka_hotel/" if substitute else PATH
        context = body["request_context"]
        observation = reference_observation(context, path, "Marina Hotel", parse_rate_groups(groups(), context))
        return httpx.Response(200, json={"request_context_hash": body["request_context_hash"],
            "outcome": "reference", "diagnostic_code": "ostrovok_public_rate_charges_unverified", "observation": observation})

    async def scenario():
        nonlocal substitute
        store = Store(str(tmp_path / "router.db"))
        async with httpx.AsyncClient(transport=httpx.MockTransport(worker)) as client:
            router = KeylessPricing(client, store)

            async def no_hotels(*args):
                return {"status": "unknown"}

            async def no_flights(*args):
                return {"facts": [], "diagnostics": []}

            router.hotels = SimpleNamespace(search=no_hotels)
            router.airlines = SimpleNamespace(search=no_flights)
            intent = TravelIntent(origin="Larnaca", destination="Athens", date_from="2026-10-31", date_to="2026-11-01", adults=1, budget_total=500)
            parts = [TravelComponent(title=name, description="Synthetic fixture", details={"latitude": 34.5, "old_osm_id": 7}) for name in ("flight", "stay", "transfer")]
            await router.apply(find_place("Athens"), find_place("Larnaca"), intent, parts)
            stay = parts[1]
            assert stay.title == "Marina Hotel" and stay.details["property_source"] == "ostrovok"
            assert stay.quote.provider_id == "ostrovok" and stay.quote.value.min == "51"
            assert stay.quote.normalized_group is None and not stay.quote.availability_verified
            assert "latitude" not in stay.details and "old_osm_id" not in stay.details
            assert "latitude" not in parts[2].details and parts[2].quote is None
            assert stay.quote.excluded_charges
            original = stay.quote.model_dump_json()
            substitute = True
            await router.apply(find_place("Athens"), find_place("Larnaca"), intent, parts, refresh=True)
            assert requests[-1]["request_context"]["target_entity_id"] == stay.details["pricing_entity_id"]
            assert stay.quote.model_dump_json() == original
            assert stay.title == "Marina Hotel" and stay.details["pricing_refresh_warning"]
            assert len(requests) == 2
        store.close()

    asyncio.run(scenario())


def test_fixed_google_property_never_routes_to_ostrovok(tmp_path, monkeypatch):
    monkeypatch.setenv("PRICE_WORKER_URL", "http://price-worker:8080")
    providers = []

    def worker(request):
        body = json.loads(request.content)
        providers.append(body["provider_id"])
        return httpx.Response(200, json={"request_context_hash": body["request_context_hash"], "outcome": "no_offers"})

    async def scenario():
        store = Store(str(tmp_path / "fixed.db"))
        async with httpx.AsyncClient(transport=httpx.MockTransport(worker)) as client:
            router = KeylessPricing(client, store)

            async def no_hotels(*args):
                return {"status": "unknown"}

            router.hotels = SimpleNamespace(search=no_hotels)
            intent = TravelIntent(origin="Larnaca", date_from="2026-10-31", date_to="2026-11-01", adults=1)
            place, origin = find_place("Athens"), find_place("Larnaca")
            context = context_for("stay", place, origin, intent, entity_id="google-hotel:" + "a" * 24)
            await router._find("stay", context, place, origin, intent)
        store.close()

    asyncio.run(scenario())
    assert providers == ["google_hotels"]


def test_discovered_same_provider_property_sends_exact_target_and_lodging_wishes(tmp_path, monkeypatch):
    monkeypatch.setenv('KEYLESS_PRICING_ENABLED', '1')
    monkeypatch.setenv('PRICE_WORKER_URL', 'http://price-worker:8080')
    requests = []
    def worker(request):
        body = json.loads(request.content)
        requests.append(body)
        context = body['request_context']
        observation = reference_observation(context, PATH, 'Synthetic same-source hotel', parse_rate_groups(groups(), context))
        return httpx.Response(200, json={'request_context_hash': body['request_context_hash'], 'outcome': 'reference',
            'diagnostic_code': 'ostrovok_public_rate_charges_unverified', 'observation': observation})
    async def run():
        store = Store(str(tmp_path / 'target.db'))
        async with httpx.AsyncClient(transport=httpx.MockTransport(worker)) as client:
            router = KeylessPricing(client, store)
            async def no_flights(*args):
                return {'facts': [], 'diagnostics': []}
            async def no_hotels(*args):
                return {'status': 'unknown'}
            router.airlines = SimpleNamespace(search=no_flights)
            router.hotels = SimpleNamespace(search=no_hotels)
            intent = TravelIntent(origin='Larnaca', destination='Athens', date_from='2026-10-31', date_to='2026-11-01', adults=1,
                hotel_requirements=['quiet private room', 'no hostel'], hard_constraints=['transport_requirement:direct flight only'])
            parts = [TravelComponent(title='Synthetic fixture', description='Synthetic fixture') for _ in range(3)]
            parts[1].source_url = 'https://www.ostrovok.ru' + PATH + '?old-query=discarded'
            parts[1].details = {'link_type': 'indexed_hotel_page', 'requirements_verified': False}
            await router.apply(find_place('Athens'), find_place('Larnaca'), intent, parts)
            assert parts[1].details['pricing_entity_id'] == ostrovok_property_id(PATH)
            assert parts[1].quote.scope.entity_id == ostrovok_property_id(PATH)
            assert parts[1].quote.normalized_group is None and not parts[1].details['requirements_verified']
        store.close()
    asyncio.run(run())
    assert len(requests) == 1
    context = requests[0]['request_context']
    assert context['target_property_path'] == PATH
    assert context['target_entity_id'] == ostrovok_property_id(PATH)
    assert context['discovery_terms'] == ['quiet private room', 'no hostel']


def test_target_path_mismatch_is_rejected_before_cache_or_worker_access(tmp_path, monkeypatch):
    monkeypatch.setenv('PRICE_WORKER_URL', 'http://price-worker:8080')
    async def run():
        store = Store(str(tmp_path / 'bad-target.db'))
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: (_ for _ in ()).throw(AssertionError('No worker dispatch')))) as client:
            router = KeylessPricing(client, store)
            intent = TravelIntent(origin='Larnaca', date_from='2026-10-31', date_to='2026-11-01', adults=1)
            place, origin = find_place('Athens'), find_place('Larnaca')
            context = context_for('stay', place, origin, intent, entity_id='ostrovok:' + 'a' * 24)
            quote, diagnostics = await router._browser(context, place, 'ostrovok', target_property_path=PATH)
            assert quote.outcome == 'context_mismatch'
            assert diagnostics[0]['diagnostic_code'] == 'provider_target_mismatch'
            assert diagnostics[0]['dispatched'] is False
        store.close()
    asyncio.run(run())
