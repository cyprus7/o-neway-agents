"""Synthetic early-departure/long-route coverage; no external requests."""
import asyncio
from datetime import date, timedelta
from types import SimpleNamespace

import httpx
import pytest

from backend.app.models import TravelIntent
from backend.app.infrastructure.provider_cache import ProviderCache
from backend.app.pricing.flights import BrowserFlightSearch
from backend.app.pricing.providers import KeylessPricing, context_for, unknown
from backend.app.travel_dates import departure_dates
from backend.tests.test_flight_gateway import source_route, trip
from backend.tests.test_flight_domain import command, snapshot
from flight_search.domain import city_label_matches, read_scope, select_route, scope_diagnostic
from flight_search.models import FlightConstraints


def window_intent():
    return TravelIntent(origin="Larnaca", destination="Berlin", date_from="2026-12-31",
                        date_to="2027-01-07", flight_departure_from="2026-12-29",
                        flight_departure_to="2026-12-31", adults=2)


def long_route(arrival=date(2026, 12, 31)):
    route = source_route(date_from="2026-12-30", date_to="2027-01-07", stops=2)
    outbound = route.outbound.model_copy(update={"arrival_date": arrival, "duration_minutes": 1500})
    return route.model_copy(update={"outbound": outbound})


def test_long_connection_route_filters_outbound_arrival_only():
    request = command(constraints=FlightConstraints(arrival_date=date(2026, 12, 31)))
    good, late = long_route(), long_route(date(2027, 1, 1))
    assert select_route([late, good], request) is good
    assert select_route([late], request) is None
    assert good.inbound.arrival_date == date(2027, 1, 8)
    assert good.outbound.duration_minutes == 1500 and good.outbound.stops == 2


def test_gateway_independently_rejects_late_arrival_and_preserves_hotel_dates():
    place, origin, _ = trip()
    intent = window_intent()
    context = context_for("flight", place, origin, intent, departure_date=date(2026, 12, 30))
    assert BrowserFlightSearch._valid_route(long_route(), context, intent)
    assert not BrowserFlightSearch._valid_route(long_route(date(2027, 1, 1)), context, intent)
    stay = context_for("stay", place, origin, intent)
    assert stay.date_from == date(2026, 12, 31) and stay.date_to == date(2027, 1, 7)
    assert (stay.date_to - stay.date_from).days == 7


def test_only_finite_new_airport_labels_and_aliases_are_supported():
    request = command()
    for airport in ("KZN", "BOB", "PPT"):
        context = request.request_context.model_copy(update={"origin": "KZN", "destination": airport if airport != "KZN" else "BOB"})
        assert scope_diagnostic(request.model_copy(update={"request_context": context})) is None
    assert city_label_matches("Tahiti", "PPT")
    assert city_label_matches("Bora-Bora", "BOB")
    assert not city_label_matches("Tahiti", "BOB")


@pytest.mark.asyncio
async def test_window_attempts_are_bounded_sequential_and_route_beats_literal_reference(tmp_path):
    store = ProviderCache(str(tmp_path / "cache.sqlite"))
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: (_ for _ in ()).throw(AssertionError("No HTTP")))) as client:
        router = KeylessPricing(client, store)
        place, origin, _ = trip()
        intent = window_intent()
        context = context_for("flight", place, origin, intent)
        calls = []
        async def lookup(component, dated, *args, **kwargs):
            calls.append(dated.date_from)
            quote = BrowserFlightSearch._reference(long_route(), dated)
            if len(calls) == 1:
                quote = quote.model_copy(update={"provider_id": "aegean_public"})
                return quote, [], {"legs": []}
            return quote, [], {"route": long_route()}
        router._find_date = lookup
        quote, _, metadata = await router._find("flight", context, place, origin, intent)
        assert calls == [date(2026, 12, 31), date(2026, 12, 30)]
        assert metadata["route"].date_from == date(2026, 12, 30)
        assert quote.scope.date_from == date(2026, 12, 30)
    store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("stop_code", [None, "flight_challenge", "flight_login", "flight_overloaded"])
async def test_max_three_dates_and_no_protection_or_overload_retry(tmp_path, stop_code):
    store = ProviderCache(str(tmp_path / "bounded.sqlite"))
    async with httpx.AsyncClient() as client:
        router = KeylessPricing(client, store)
        place, origin, _ = trip()
        intent = window_intent()
        context = context_for("flight", place, origin, intent)
        calls = []
        async def lookup(component, dated, *args, **kwargs):
            calls.append(dated.date_from)
            return unknown("fixture", dated, "unknown"), ([{"diagnostic_code": stop_code}] if stop_code else []), None
        router._find_date = lookup
        await router._find("flight", context, place, origin, intent)
        assert calls == list(departure_dates(intent)) if stop_code is None else calls == [date(2026, 12, 31)]
    store.close()


def test_browser_keys_bind_departure_and_required_arrival(tmp_path):
    store = ProviderCache(str(tmp_path / "keys.sqlite"))
    client = httpx.AsyncClient()
    adapter = BrowserFlightSearch(client, store)
    place, origin, _ = trip()
    intent = window_intent()
    first = context_for("flight", place, origin, intent, departure_date=date(2026, 12, 31))
    second = first.model_copy(update={"date_from": date(2026, 12, 30)})
    assert adapter.key(first, intent) != adapter.key(second, intent)
    legacy = intent.model_copy(update={"flight_departure_from": None, "flight_departure_to": None})
    assert adapter.key(first, intent) != adapter.key(first, legacy)
    asyncio.run(client.aclose())
    store.close()


@pytest.mark.asyncio
async def test_fixed_refresh_pins_observed_departure_and_rejects_removed_window(monkeypatch, tmp_path):
    from backend.tests.test_flight_refresh import parts, setup
    store = ProviderCache(str(tmp_path / "fixed.sqlite"))
    async with httpx.AsyncClient() as client:
        router = setup(monkeypatch, client, store)
        place, origin, _ = trip()
        intent = window_intent()
        components = parts()
        calls = []
        async def lookup(component, dated, *args, **kwargs):
            calls.append(dated.date_from)
            if dated.date_from != date(2026, 12, 30):
                return unknown("google_flights", dated, "No matching arrival"), [], None
            route = long_route()
            return BrowserFlightSearch._reference(route, dated), [], {"route": route}
        router._find_date = lookup
        await router.apply(place, origin, intent, components, only=("flight",))
        flight = components[0]
        assert flight.quote.scope.date_from == date(2026, 12, 30)
        assert flight.details["selected_departure_date"] == "2026-12-30"
        assert flight.details["departure_window"] == {
            "date_from": "2026-12-29", "date_to": "2026-12-31", "arrival_date": "2026-12-31"}
        calls.clear()
        await router.apply(place, origin, intent, components, refresh=True, only=("flight",))
        assert calls == [date(2026, 12, 30)]
        calls.clear()
        legacy = intent.model_copy(update={"flight_departure_from": None, "flight_departure_to": None})
        await router.apply(place, origin, legacy, components, refresh=True, only=("flight",))
        assert calls == []
        assert flight.quote.value.kind == "unknown"
        assert "selected_departure_date" not in flight.details
    store.close()


@pytest.mark.asyncio
async def test_cancel_window_attempt_cancels_active_source_and_never_tries_next_date(tmp_path):
    store = ProviderCache(str(tmp_path / "cancel.sqlite"))
    async with httpx.AsyncClient() as client:
        router = KeylessPricing(client, store)
        place, origin, _ = trip()
        intent = window_intent()
        entered, cleaned = asyncio.Event(), asyncio.Event()
        calls = []
        async def lookup(component, dated, *args, **kwargs):
            calls.append(dated.date_from)
            entered.set()
            try:
                await asyncio.Future()
            finally:
                cleaned.set()
        router._find_date = lookup
        task = asyncio.create_task(router._find("flight", context_for("flight", place, origin, intent), place, origin, intent))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert cleaned.is_set() and calls == [date(2026, 12, 31)]
    store.close()


@pytest.mark.asyncio
async def test_remaining_deadline_keeps_literal_reference_without_cancel_swallow(tmp_path):
    store = ProviderCache(str(tmp_path / "deadline.sqlite"))
    async with httpx.AsyncClient() as client:
        router = KeylessPricing(client, store)
        place, origin, _ = trip()
        intent = window_intent()
        context = context_for("flight", place, origin, intent)
        calls = []
        async def lookup(component, dated, *args, **kwargs):
            calls.append(dated.date_from)
            if len(calls) == 1:
                quote = BrowserFlightSearch._reference(long_route(), dated)
                return quote, [], {"legs": []}
            await asyncio.Future()
        router._find_date = lookup
        quote, _, metadata = await router._find(
            "flight", context, place, origin, intent,
            deadline=asyncio.get_running_loop().time() + 0.03)
        assert quote.value.kind != "unknown" and metadata == {"legs": []}
        assert len(calls) == 2
    store.close()


@pytest.mark.asyncio
async def test_parent_singleflight_binds_whole_departure_window(monkeypatch, tmp_path):
    from backend.tests.test_flight_refresh import parts, setup
    store = ProviderCache(str(tmp_path / "singleflight.sqlite"))
    async with httpx.AsyncClient() as client:
        router = setup(monkeypatch, client, store)
        place, origin, _ = trip()
        first = window_intent()
        second = first.model_copy(update={"flight_departure_from": date(2026, 12, 30)})
        entered, release = asyncio.Event(), asyncio.Event()
        calls = []
        async def lookup(component, context, *args, **kwargs):
            calls.append(context)
            if len(calls) == 2:
                entered.set()
            await release.wait()
            return unknown("fixture", context, "No observed route"), [], None
        router._find = lookup
        tasks = [asyncio.create_task(router.apply(place, origin, intent, parts(), only=("flight",)))
                 for intent in (first, second)]
        await asyncio.wait_for(entered.wait(), 1)
        assert len(router._inflight) == 2
        release.set()
        await asyncio.gather(*tasks)
        assert len(calls) == 2 and not router._inflight
    store.close()


def test_country_is_not_substituted_for_airport_and_visible_city_alias_is_finite():
    from flight_search.models import FlightRequestContext
    context = FlightRequestContext(origin="KZN", destination="BOB", date_from="2026-10-30", date_to="2026-11-01")
    request = command(request_context=context)
    page = snapshot(origin_input="Kazan", destination_input="Bora-Bora",
        track_label="Track prices from Kazan to Bora Bora departing 2026-10-30 and returning 2026-11-01")
    scope = read_scope(page, request)
    assert scope.origin == "KZN" and scope.destination == "BOB"
