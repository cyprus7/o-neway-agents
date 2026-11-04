"""Catalog/provider policy integration with synthetic source records and no live HTTP."""
from datetime import date
from types import SimpleNamespace

import httpx
import pytest

from backend.app.geography import find_place
from backend.app.models import TravelIntent
from backend.app.planner import Planner, apply_option_pricing, constraint_checks
from backend.app.providers import LocalTravelDataPort, Providers
from backend.app.storage import Store


SNAPSHOT_TIME = "2026-10-24T10:00:00+00:00"


def intent(**changes):
    values = dict(origin="Варшава", destination="Ларнака", date_from=date(2027, 6, 1),
                  date_to=date(2027, 6, 8), budget_total=1500)
    values.update(changes)
    return TravelIntent(**values)


def hotel(**changes):
    row = dict(hotel_id="osm:node:123456", name="Synthetic Larnaca Catalog Hotel",
               latitude=34.92, longitude=33.63, source_url="https://www.openstreetmap.org/node/123456",
               website="https://hotel.example/", tags={"wheelchair": "yes"},
               retrieved_at=SNAPSHOT_TIME, source_updated_at="2026-10-23T09:00:00+00:00",
               destination_id="larnaca")
    row.update(changes)
    return row


class FixtureCatalog:
    def __init__(self, status="matched", reason="matched", rows=None):
        self.status, self.reason = status, reason
        self.rows = (hotel(),) if rows is None else tuple(rows)
        self.calls = []

    def search(self, place, travel_intent, limit=3):
        self.calls.append((place.name, travel_intent.origin))
        return SimpleNamespace(status=self.status, reason=self.reason, hotels=self.rows[:limit],
                               release_id="synthetic-release-v1", retrieved_at=SNAPSHOT_TIME)

    def capabilities(self):
        return {"configured": True, "status": "ready", "row_count": len(self.rows)}


def forbidden(*args, **kwargs):
    raise AssertionError("Unexpected travel-data HTTP or lodging source dispatch")


@pytest.mark.asyncio
async def test_covered_catalog_suppresses_hotel_discovery_and_pricing(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCAL_CATALOG_ENABLED", "1")
    monkeypatch.setenv("KEYLESS_PRICING_ENABLED", "1")
    monkeypatch.setenv("SERPAPI_API_KEY", "synthetic-serp-key")
    monkeypatch.setenv("PRICE_WORKER_URL", "http://price-worker:8090")
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.path == "/search":
            assert "hotel" not in request.url.params["q"].casefold()
            return httpx.Response(200, json={"results": []})
        if request.url.host == "serpapi.com":
            assert request.url.params["engine"] == "google_flights"
            return httpx.Response(200, json={"best_flights": []})
        raise AssertionError(f"Unexpected HTTP category: {request.url.host}")

    store = Store(str(tmp_path / "catalog.db"))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        providers = Providers(client, store)
        providers.catalog = FixtureCatalog()
        providers.ranker.rank_rows = forbidden
        providers.pricing.hotels.search = forbidden
        providers.pricing._browser = forbidden
        async def no_fares(*args):
            return {"facts": [], "diagnostics": []}
        providers.pricing.airlines.search = no_fares
        travel_intent = intent(pets=["dog"], hotel_requirements=["тихий номер"])
        place, origin = find_place("Ларнака"), find_place("Варшава")
        sources = await providers.search(place, travel_intent)
        parts = await providers.inventory(place, origin, travel_intent)
        stay = parts[1]
        assert stay.title == hotel()["name"]
        assert stay.details["catalog_property_id"] == hotel()["hotel_id"]
        assert stay.details["pricing_entity_id"] == hotel()["hotel_id"]
        assert stay.details["discovery_mode"] == "local_catalog"
        assert stay.details["catalog_version"] == "synthetic-release-v1"
        assert stay.details["availability_verified"] is False
        assert stay.details["pet_policy_verified"] is False
        assert stay.details["requirements_verified"] is False
        assert stay.price is None and stay.quote.value.kind == "unknown"
        assert stay.quote.normalized_group is None
        assert stay.quote.outcome == "policy_disabled"
        assert stay.source_url == hotel()["source_url"]
        assert stay.evidence[0].freshness == "snapshot"
        assert stay.evidence[0].retrieved_at == SNAPSHOT_TIME
        assert any(source.freshness == "snapshot" for source in sources)
        checks = constraint_checks(travel_intent, place, parts)
        assert next(check for check in checks if check["constraint"] == "Общий бюджет")["status"] == "unknown"
        assert all("overpass" not in request.url.host for request in requests)
        assert providers.catalog.calls == [(place.name, "Варшава"), (place.name, "Варшава")]
    store.close()


@pytest.mark.asyncio
async def test_local_property_and_snapshot_evidence_survive_price_refresh(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCAL_CATALOG_ENABLED", "1")
    monkeypatch.setenv("WEB_FALLBACK_ENABLED", "0")
    monkeypatch.setenv("KEYLESS_PRICING_ENABLED", "1")
    store = Store(str(tmp_path / "refresh.db"))
    async with httpx.AsyncClient(transport=httpx.MockTransport(forbidden)) as client:
        providers = Providers(client, store)
        providers.catalog = FixtureCatalog()
        travel_intent = intent()
        place, origin = find_place("Ларнака"), find_place("Варшава")
        parts = await providers.inventory(place, origin, travel_intent)
        prior = (parts[1].title, parts[1].source_url, dict(parts[1].details),
                 [source.model_dump() for source in parts[1].evidence])
        await providers.pricing.apply(place, origin, travel_intent, parts, refresh=True)
        assert (parts[1].title, parts[1].source_url, parts[1].details,
                [source.model_dump() for source in parts[1].evidence]) == prior
        assert parts[1].quote.provider_id == "local_catalog"
        assert parts[1].quote.value.kind == "unknown" and parts[1].quote.normalized_group is None
        assert parts[0].quote.outcome == "policy_disabled"
    store.close()


@pytest.mark.asyncio
async def test_disabled_fallback_plans_and_refreshes_without_external_http(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCAL_CATALOG_ENABLED", "1")
    monkeypatch.setenv("WEB_FALLBACK_ENABLED", "0")
    monkeypatch.setenv("KEYLESS_PRICING_ENABLED", "1")
    monkeypatch.setenv("SERPAPI_API_KEY", "synthetic-serp-key")
    monkeypatch.setenv("OLLAMA_API_KEY", "synthetic-search-key")
    store = Store(str(tmp_path / "offline.db"))
    async with httpx.AsyncClient(transport=httpx.MockTransport(forbidden)) as client:
        providers = Providers(client, store)
        providers.catalog = FixtureCatalog()
        travel_intent = intent()
        origin, place = find_place("Варшава"), find_place("Ларнака")
        assert await providers.resolve_place("An unknown city") is None
        assert await providers.resolve_place("Ларнака") == place
        session = store.create_session()
        session.intent = travel_intent
        options = await Planner(store, None, providers).build_options(session, origin, [place])
        assert len(options) == 1
        option = options[0]
        assert option.stay.details["catalog_property_id"] == hotel()["hotel_id"]
        assert option.weather["kind"] == "unknown"
        assert option.regional_events.status == "not_checked"
        assert option.estimated_total is None
        await providers.pricing.apply(place, origin, travel_intent,
                                      (option.flight, option.stay, option.transfer), refresh=True)
        apply_option_pricing(option, travel_intent)
        assert option.estimated_total is None and option.price_status == "unavailable"
        assert providers.search_capabilities()["configured"] is False
    store.close()


@pytest.mark.asyncio
async def test_shipped_catalog_plans_both_cities_and_persists_offline_refresh(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCAL_CATALOG_ENABLED", "1")
    monkeypatch.setenv("WEB_FALLBACK_ENABLED", "0")
    monkeypatch.setenv("KEYLESS_PRICING_ENABLED", "1")
    store = Store(str(tmp_path / "shipped-catalog.db"))
    async with httpx.AsyncClient(transport=httpx.MockTransport(forbidden)) as client:
        providers = Providers(client, store)
        capabilities = providers.catalog_capabilities()
        assert capabilities["configured"] is True
        assert capabilities["row_count"] == 1300
        assert capabilities["coverage"]["larnaca"] == 99 and capabilities["coverage"]["paphos"] == 160
        assert len(capabilities["coverage"]) == 8
        assert capabilities["kind_counts"]["hotel"] == 682
        assert capabilities["source_record_counts"] == {"osm": 1300, "wikidata": 20}
        assert capabilities["release_id"] == "cyprus-lodgings-20261025-v2"
        session = store.create_session()
        session.intent = intent(destination="Кипр", children_ages=[6], pets=["dog"])
        planner = Planner(store, None, LocalTravelDataPort(providers))
        destinations = [find_place("Ларнака"), find_place("Пафос")]
        session.options = await planner.build_options(session, find_place("Варшава"), destinations)
        assert {option.destination for option in session.options} == {place.name for place in destinations}
        assert len(session.options) == 3
        assert len({o.stay.details["catalog_property_id"] for o in session.options}) == 3
        assert len({o.stay.source_url for o in session.options}) == 3
        assert len({o.id for o in session.options}) == 3
        before = {}
        for option in session.options:
            stay = option.stay
            identity = stay.details["catalog_property_id"]
            _, kind, number = identity.split(":")
            assert kind in ("node", "way") and number.isdecimal()
            assert stay.source_url == f"https://www.openstreetmap.org/{kind}/{number}"
            assert stay.details["catalog_retrieved_at"] == capabilities["retrieved_at"]
            assert stay.details["catalog_version"] == capabilities["release_id"]
            assert stay.evidence[0].freshness == "snapshot"
            assert stay.evidence[0].retrieved_at == capabilities["retrieved_at"]
            assert stay.price is None and (stay.quote is None or stay.quote.value.kind == "unknown")
            assert (stay.quote is None or stay.quote.normalized_group is None) and option.estimated_total is None
            assert option.weather["kind"] == "unknown"
            assert option.regional_events.status == "not_checked"
            assert option.regional_events.checked_at is None
            assert all(part.price is None for part in (option.flight, stay, option.transfer))
            assert next(check for check in option.constraint_checks if check["constraint"] == "Общий бюджет")["status"] == "unknown"
            before[identity] = (stay.title, stay.source_url, identity,
                                          [source.model_dump() for source in stay.evidence])
        store.save_session(session)
        job, deduplicated = store.start_refresh(session.id, session.revision)
        assert deduplicated is False
        await planner.refresh_prices(job.id)
        assert store.get_job(job.id).status == "completed"
        refreshed = store.get_session(session.id)
        assert refreshed.revision == session.revision + 1
        assert refreshed.selected_option_id is None
        for option in refreshed.options:
            stay = option.stay
            assert (stay.title, stay.source_url, stay.details["catalog_property_id"],
                    [source.model_dump() for source in stay.evidence]) == before[stay.details["catalog_property_id"]]
            assert stay.details["catalog_retrieved_at"] == capabilities["retrieved_at"]
            assert stay.price is None and (stay.quote is None or stay.quote.value.kind == "unknown")
            assert (stay.quote is None or stay.quote.normalized_group is None) and option.estimated_total is None
    store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("status,reason", [("unavailable", "checksum_mismatch"),
                                          ("out_of_scope", "unsupported_destination"),
                                          ("empty", "no_eligible_hotels")])
async def test_missing_catalog_uses_explicit_remote_fallback(tmp_path, monkeypatch, status, reason):
    monkeypatch.setenv("LOCAL_CATALOG_ENABLED", "1")
    monkeypatch.delenv("SERPAPI_API_KEY", raising=False)
    monkeypatch.setenv("KEYLESS_PRICING_ENABLED", "0")
    requests = []
    def handler(request):
        requests.append(request)
        if "overpass" in request.url.host:
            return httpx.Response(200, json={"elements": [{"type": "node", "id": 654321,
                "lat": 34.92, "lon": 33.63, "tags": {"tourism": "hotel", "name": "Synthetic Remote Hotel"}}]})
        return httpx.Response(200, json={"results": []})
    store = Store(str(tmp_path / "fallback.db"))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        providers = Providers(client, store)
        providers.catalog = FixtureCatalog(status, reason, rows=[])
        _, stay, _ = await providers.inventory(find_place("Ларнака"), find_place("Варшава"), intent())
        assert stay.title == "Synthetic Remote Hotel"
        assert stay.details["catalog_fallback_reason"] == reason
        assert any("Резервный" in source.label for source in stay.evidence)
        assert "catalog_property_id" not in stay.details
        assert any("overpass" in request.url.host for request in requests)
    store.close()


@pytest.mark.asyncio
async def test_fixed_city_real_catalog_planner_displays_three_distinct_properties(tmp_path,monkeypatch):
    monkeypatch.setenv("LOCAL_CATALOG_ENABLED","1")
    monkeypatch.setenv("WEB_FALLBACK_ENABLED","0")
    store=Store(str(tmp_path/"fixed-city.db"))
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(forbidden)) as client:
            providers=Providers(client,store)
            session=store.create_session()
            session.intent=intent()
            place=find_place(session.intent.destination)
            options=await Planner(store,None,LocalTravelDataPort(providers)).build_options(session,find_place(session.intent.origin),[place])
            assert len(options)==3 and {o.destination for o in options}=={place.name}
            identities={o.stay.details["catalog_property_id"] for o in options}
            assert len(identities)==3 and len({o.stay.source_url for o in options})==3
            assert len({o.id for o in options})==3
            for option in options:
                assert option.stay.details["catalog_sources"]
                assert option.stay.details["catalog_retrieved_at"]==providers.catalog_capabilities()["retrieved_at"]
                assert option.stay.evidence[0].freshness=="snapshot"
                assert option.stay.price is None
                assert option.stay.quote is None or option.stay.quote.normalized_group is None
                assert option.estimated_total is None
    finally:
        store.close()
