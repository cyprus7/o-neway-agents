"""Country-scoped discovery keeps real indexed property identities, never SERP placeholders."""
import httpx
import pytest
import pytest_asyncio

from backend.app.data_gateway import create_gateway_app
from backend.app.domain.travel_data import PlaceRef, candidate_identity
from backend.app.geography import Place, find_place
from backend.app.infrastructure.http_travel_data import HttpTravelDataPort
from backend.app.infrastructure.provider_cache import ProviderCache
from backend.app.models import TravelIntent
from backend.app.planner import Planner, option_place
from backend.app.providers import LocalTravelDataPort, Providers, lodging_result_matches
from backend.app.search import SearchHit, SearchOutcome
from backend.app.storage import Store
from backend.tests.test_pricing_core import published_reference


STAMP = "2026-10-20T10:00:00+00:00"


def country():
    return Place("Мальдивы", "Мальдивы", "MV", 3.2, 73.2, None, (),
                 ("мальдивы", "maldives"), "https://en.wikivoyage.org/wiki/Maldives", "Country fixture", scope="country")


def travel():
    return TravelIntent(origin="Warsaw", destination="Мальдивы", date_from="2027-06-01", date_to="2027-06-08",
                        adults=2, children_ages=[6], rooms=1, currency="EUR")


def hotel(index, *, code="mv"):
    return SearchHit("searxng", f"Synthetic Island Hotel {index} - Booking.com",
        f"https://www.booking.com/hotel/{code}/synthetic-island-{index}.html", "Indexed lodging fixture only",
        STAMP, "cached")


def no_network(request):
    raise AssertionError("Country candidate fixture must not dispatch network/model requests")


@pytest_asyncio.fixture
async def providers(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCAL_CATALOG_ENABLED", "0")
    monkeypatch.setenv("WEB_FALLBACK_ENABLED", "1")
    cache = ProviderCache(str(tmp_path / "country-cache.db"))
    client = httpx.AsyncClient(transport=httpx.MockTransport(no_network))
    source = Providers(client, cache)
    async def rank(rows, query):
        return rows
    source.ranker.rank_rows = rank
    yield source
    await client.aclose()
    cache.close()


def set_hits(source, hits):
    async def search(*args, **kwargs):
        return [SearchOutcome("searxng", tuple(hits), "cached", retrieved_at=STAMP)]
    source.web_search.search = search


@pytest.mark.asyncio
async def test_pcli_scope_english_alias_survives_actual_gateway_http_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCAL_CATALOG_ENABLED", "0")
    calls = []
    def geocode(request):
        calls.append(request.url.host)
        assert request.url.host == "geocoding-api.open-meteo.com"
        return httpx.Response(200, json={"results": [{"name": "Мальдивы", "country": "Мальдивы",
            "country_code": "MV", "latitude": 3.2, "longitude": 73.2, "feature_code": "PCLI"}]})
    app = create_gateway_app(database_path=str(tmp_path / "gateway.db"), transport=httpx.MockTransport(geocode))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as client:
            place = await HttpTravelDataPort(client, "http://gateway").resolve_place("Мальдивы")
    assert place.scope == "country" and place.airport is None
    assert "maldives" in place.aliases
    assert PlaceRef.model_validate(PlaceRef.from_place(place).model_dump(mode="json")).to_place().scope == "country"
    assert find_place("Warsaw").scope == "locality"
    assert calls == ["geocoding-api.open-meteo.com"]


@pytest.mark.parametrize("row,accepted", [
    ({"title": "Island Hotel", "url": "https://www.booking.com/hotel/mv/island.html"}, True),
    ({"title": "Maldives Hotel", "url": "https://www.booking.com/hotel/cy/island.html"}, False),
    ({"title": "Maldives hotels", "url": "https://www.booking.com/searchresults.html?ss=Maldives"}, True),
    ({"title": "Island Hotel", "url": "https://www.booking.com/hotel/tr/island.html"}, False),
])
def test_country_booking_country_path_eligibility(row, accepted):
    assert lodging_result_matches(row, country(), "Warsaw") is accepted


def test_locality_city_filter_remains_strict_even_matching_country_path():
    larnaca = find_place("Larnaca")
    assert not lodging_result_matches({"title": "Paphos Hotel", "url": "https://www.booking.com/hotel/cy/paphos.html"}, larnaca, "Warsaw")
    assert not lodging_result_matches({"title": "Larnaca Hotel", "url": "https://www.booking.com/hotel/mv/larnaca.html"}, larnaca, "Warsaw")


@pytest.mark.asyncio
async def test_country_real_places_skips_country_centroid_overpass(providers):
    set_hits(providers, [hotel(1)])
    async def forbidden_overpass(*args, **kwargs):
        pytest.fail("Country centroid must not trigger a 6km Overpass lodging search")
    providers.cached_get = forbidden_overpass
    _, stay, transfer = providers._inventory_components(country(), find_place("Warsaw"), travel())
    await providers.real_places(country(), travel(), stay, transfer)
    assert stay.source_url == hotel(1).url
    assert stay.details["link_type"] == "indexed_hotel_page"
    assert stay.price is None and stay.quote is None
    assert transfer.price is None and transfer.quote is None


async def indexed_inventory(source, place, origin, intent):
    parts = source._inventory_components(place, origin, intent)
    await source.hotel_search_fallback(place, intent, parts[1])
    return parts


@pytest.mark.asyncio
async def test_indexed_no_iata_alternatives_are_bounded_source_bound_and_isolated(providers):
    place, origin, intent = country(), find_place("Warsaw"), travel()
    set_hits(providers, [hotel(1), hotel(1), hotel(2), hotel(3), hotel(4)])
    async def inventory(*args):
        parts = await indexed_inventory(providers, *args)
        parts[0].details["shared_route_fixture"] = True
        # Property-dependent observations from the original must not leak into alternatives.
        parts[2].price = 999
        parts[2].quote = published_reference("transfer")
        parts[2].details["original_property_marker"] = "only-first-source"
        return parts
    providers.inventory = inventory
    port = LocalTravelDataPort(providers)
    results = await port.inventory_candidates(place, origin, intent, limit=9)
    assert len(results) == 3 and len({item.stay.source_url for item in results}) == 3
    assert len({item.candidate_id for item in results}) == 3
    for item in results:
        assert item.candidate_id == candidate_identity(item, place, origin, intent)
        assert item.stay.details["link_type"] == "indexed_hotel_page"
        assert not item.stay.details.get("pricing_entity_id")
        assert item.stay.quote is None and item.stay.price is None
        assert item.stay.evidence and {e.source_url for e in item.stay.evidence} == {item.stay.source_url}
        assert all(e.retrieved_at == STAMP for e in item.stay.evidence)
        assert item.flight.details["shared_route_fixture"] is True
    assert results[0].transfer.price == 999
    assert results[0].transfer.quote is not None
    for item in results[1:]:
        assert item.transfer.price is None and item.transfer.quote is None
        assert "original_property_marker" not in item.transfer.details
    results[0].flight.details["private_mutation"] = True
    assert "private_mutation" not in results[1].flight.details
    results[0].stay.evidence.clear()
    assert results[1].stay.evidence
    assert len(await port.inventory_candidates(place, origin, intent, limit=1)) == 1
    changed = intent.model_copy(update={"adults": 3, "date_to": intent.date_to.replace(day=9)})
    assert candidate_identity(results[1], place, origin, changed) != results[1].candidate_id


@pytest.mark.asyncio
async def test_search_placeholders_and_wrong_country_never_become_candidate_cards(providers):
    set_hits(providers, [hotel(1, code="tr"), SearchHit("searxng", "Maldives hotels",
        "https://www.booking.com/searchresults.html?ss=Maldives", "Search only", STAMP, "cached")])
    async def inventory(*args):
        return await indexed_inventory(providers, *args)
    providers.inventory = inventory
    assert await LocalTravelDataPort(providers).inventory_candidates(country(), find_place("Warsaw"), travel()) == []


@pytest.mark.asyncio
async def test_real_indexed_country_candidates_reach_planner_with_demo_ranges(providers, tmp_path):
    set_hits(providers, [hotel(1), hotel(2), hotel(3)])
    async def inventory(*args):
        return await indexed_inventory(providers, *args)
    providers.inventory = inventory
    port = LocalTravelDataPort(providers)
    async def weather(*args):
        return {"kind": "unknown", "status": "unavailable", "evidence": []}
    async def search(*args):
        return []
    port.weather, port.search = weather, search
    port.regional_events = None
    store = Store(str(tmp_path / "planner.db"))
    try:
        session = store.create_session()
        session.intent, session.revision = travel(), 1
        before_intent = session.intent.model_dump_json()
        options = await Planner(store, None, port).build_options(session, find_place("Warsaw"), [country()])
        assert len(options) == 3
        assert len({option.stay.source_url for option in options}) == 3
        for option in options:
            context = PlaceRef.from_place(option_place(option))
            assert context.country_code == "MV" and context.scope == "country"
            assert context.airport is None and "maldives" in context.aliases
            assert option.demo_pricing and len(option.demo_pricing.components) == 3
            assert option.estimated_total is None
            assert option.stay.quote is None and option.transfer.quote is None
            assert option.stay.details["availability_verified"] is False
            assert [p.kind for p in option.demo_pricing.components] == ["flight", "stay", "transfer"]
        assert session.intent.model_dump_json() == before_intent
        session.options = options
        store.save_session(session)
        restored = store.get_session(session.id)
        assert all(PlaceRef.from_place(option_place(option)).scope == "country" for option in restored.options)
    finally:
        store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy", [False, True])
async def test_country_refresh_retains_indexed_sources_and_demo_ranges(providers, tmp_path, legacy):
    set_hits(providers, [hotel(1), hotel(2), hotel(3)])
    async def inventory(*args):
        return await indexed_inventory(providers, *args)
    providers.inventory = inventory
    port = LocalTravelDataPort(providers)
    async def weather(*args):
        return {"kind": "unknown", "status": "unavailable", "evidence": []}
    async def search(*args):
        return []
    port.weather, port.search, port.regional_events = weather, search, None
    resolved, refreshed = [], []
    async def resolve(name):
        resolved.append(name)
        return find_place("Warsaw") if name == "Warsaw" else country()
    async def refresh(place, origin, intent, current):
        context = PlaceRef.from_place(place)
        assert context.country_code == "MV" and context.scope == "country" and context.airport is None
        assert "maldives" in context.aliases
        assert intent.date_from == travel().date_from and intent.children_ages == [6]
        refreshed.append(current.stay.source_url)
        return current
    port.resolve_place, port.refresh_inventory = resolve, refresh
    store = Store(str(tmp_path / "refresh-country.db"))
    try:
        session = store.create_session()
        session.intent, session.revision = travel(), 1
        planner = Planner(store, None, port)
        session.options = await planner.build_options(session, find_place("Warsaw"), [country()])
        if legacy:
            for option in session.options:
                option.country_code, option.destination_scope, option.destination_aliases = None, "locality", ()
        original_sources = [option.stay.source_url for option in session.options]
        original_ids = [option.id for option in session.options]
        store.save_session(session)
        job, _ = store.start_refresh(session.id, 1)
        await planner.refresh_prices(job.id)
        assert store.get_job(job.id).status == "completed"
        current = store.get_session(session.id)
        assert current.revision == 2 and current.accepted_plan_revision == 2
        assert [option.stay.source_url for option in current.options] == original_sources == refreshed
        assert all(option.id not in original_ids for option in current.options)
        for option in current.options:
            context = PlaceRef.from_place(option_place(option))
            assert context.country_code == "MV" and context.scope == "country"
            assert option.demo_pricing and option.estimated_total is None
            assert option.stay.quote is None and option.transfer.quote is None
        assert resolved == (["Warsaw"] + ["Мальдивы"] * 3 if legacy else ["Warsaw"])
    finally:
        store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["wrong_country", "search_url", "missing_evidence", "foreign_evidence", "duplicate"])
async def test_late_alternative_admission_rechecks_source_and_geography(providers, invalid):
    set_hits(providers, [hotel(1), hotel(2), hotel(3)])
    async def inventory(*args):
        parts = await indexed_inventory(providers, *args)
        alternative = parts[1].details["alternative_properties"][0]
        if invalid == "wrong_country":
            alternative["source_url"] = hotel(2, code="tr").url
        elif invalid == "search_url":
            alternative["source_url"] = "https://www.booking.com/searchresults.html?ss=Maldives"
        elif invalid == "missing_evidence":
            alternative.pop("evidence")
        elif invalid == "foreign_evidence":
            alternative["evidence"]["source_url"] = hotel(99).url
        else:
            alternative["source_url"] = parts[1].source_url
        return parts
    providers.inventory = inventory
    candidates = await LocalTravelDataPort(providers).inventory_candidates(country(), find_place("Warsaw"), travel())
    assert [item.stay.source_url for item in candidates] == [hotel(1).url, hotel(3).url]
    assert all({e.source_url for e in item.stay.evidence} == {item.stay.source_url} for item in candidates)


@pytest.mark.asyncio
async def test_empty_country_search_emits_warning_not_success(providers, tmp_path):
    set_hits(providers, [])
    async def inventory(*args):
        return await indexed_inventory(providers, *args)
    providers.inventory = inventory
    port = LocalTravelDataPort(providers)
    async def weather(*args):
        return {"kind": "unknown", "status": "unavailable", "evidence": []}
    async def search(*args):
        return []
    port.weather, port.search, port.regional_events = weather, search, None
    store = Store(str(tmp_path / "empty-planner.db"))
    try:
        session = store.create_session()
        session.intent, session.revision = travel(), 1
        assert await Planner(store, None, port).build_options(session, find_place("Warsaw"), [country()]) == []
        events = [event for event in store.get_session(session.id).events if event.step == "search"]
        assert events[-1].status == "warning"
        assert events[-1].details["options"] == 0
        assert not any(event.status == "completed" for event in events)
    finally:
        store.close()
