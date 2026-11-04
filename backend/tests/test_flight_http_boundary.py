"""Agent rejects a different flight even in a valid gateway response envelope."""
from datetime import datetime, timedelta, timezone

import pytest
import httpx

from backend.app.domain.travel_data import ComponentBundle, context_for
from backend.app.data_gateway import create_gateway_app
from backend.app.geography import find_place
from backend.app.infrastructure.http_travel_data import HttpTravelDataPort
from backend.app.models import TravelComponent, TravelIntent
from backend.app.pricing.models import LowerBoundPrice, PriceObservation, QuoteScope
from backend.app.pricing.normalization import request_context_hash
from backend.app.pricing.providers import unknown


IDENTITY = "google-flight:" + "a" * 64


def observed(*, fixed=False):
    origin, place = find_place("Ларнака"), find_place("Берлин")
    intent = TravelIntent(origin=origin.name, destination=place.name,
                          date_from="2026-10-30", date_to="2026-11-01", adults=2)
    context = context_for("flight", place, origin, intent, entity_id=IDENTITY if fixed else None)
    now = datetime.now(timezone.utc)
    quote = PriceObservation(provider_id="google_flights", outcome="reference",
        value=LowerBoundPrice(min="1642", currency="EUR"),
        request_context_hash=request_context_hash(context),
        scope=QuoteScope(component="flight", entity_level="route", entity_id=IDENTITY,
            origin=context.origin, destination=context.destination,
            date_from=context.date_from, date_to=context.date_to, adults=2, children_ages=(),
            trip_type="roundtrip", unit="group_trip", currency="EUR",
            unverified_fields=("mandatory_fees", "availability")),
        source_url="https://www.google.com/travel/flights", observed_at=now, retrieved_at=now,
        expires_at=now + timedelta(minutes=10))
    parts = [TravelComponent(title=name, description="Source observation")
             for name in ("flight", "stay", "transfer")]
    parts[0].quote = quote
    parts[0].details["pricing_entity_id"] = IDENTITY
    return ComponentBundle.from_components(parts), place, origin, intent


def test_flight_reference_initial_and_fixed_contexts_are_separate():
    current, place, origin, intent = observed()
    refreshed, _, _, _ = observed(fixed=True)
    assert HttpTravelDataPort._compatible_quotes(current, place, origin, intent)
    assert HttpTravelDataPort._compatible_refresh(current, refreshed, place, origin, intent)
    assert not HttpTravelDataPort._compatible_quotes(refreshed, place, origin, intent)
    assert not HttpTravelDataPort._compatible_refresh(current, current, place, origin, intent.model_copy(update={"adults": 3}))


@pytest.mark.parametrize("field,value", [
    ("adults", 1), ("origin", "PFO"), ("destination", "ATH"),
    ("date_from", "2026-10-29"), ("currency", "USD"), ("unit", "person_trip"),
    ("entity_id", "google-flight:" + "b" * 64),
])
def test_flight_reference_cannot_echo_hash_to_hide_contradictory_source_scope(field, value):
    result, place, origin, intent = observed()
    # An untrusted service can copy a valid request hash; independently inspect
    # the claimed source scope even though the price remains only a reference.
    result.flight.quote = result.flight.quote.model_copy(update={
        "scope": result.flight.quote.scope.model_copy(update={field: value})})
    assert not HttpTravelDataPort._compatible_quotes(result, place, origin, intent)


def test_refresh_cannot_change_itinerary_when_price_is_unknown_or_retained():
    current, place, origin, intent = observed()
    changed = current.model_copy(deep=True)
    changed.flight.details["pricing_entity_id"] = "google-flight:" + "b" * 64
    assert not HttpTravelDataPort._compatible_refresh(current, changed, place, origin, intent)


@pytest.mark.asyncio
async def test_actual_http_refresh_can_discover_first_route_after_unknown_inventory(tmp_path,monkeypatch):
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",raising=False)
    discovered, place, origin, intent = observed()
    current = discovered.model_copy(deep=True)
    current.flight.details = {}
    current.flight.quote = unknown("google_flights",context_for("flight",place,origin,intent),
                                   "Synthetic first source absence","no_offers")
    calls = []
    class FixturePort:
        async def inventory(self,*_):
            calls.append("inventory")
            return current
        async def refresh_inventory(self,actual_place,actual_origin,actual_intent,previous):
            calls.append("refresh_inventory")
            assert previous.flight.quote.value.kind == "unknown"
            assert not previous.flight.details.get("pricing_entity_id")
            return discovered
    app = create_gateway_app(database_path=str(tmp_path/"http-flight.db"),port_factory=lambda *_:FixturePort())
    async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://gateway") as client:
        port = HttpTravelDataPort(client,"http://gateway")
        initial = await port.inventory(place,origin,intent)
        assert initial.flight.quote.value.kind == "unknown" and not initial.flight.details.get("pricing_entity_id")
        refreshed = await port.refresh_inventory(place,origin,intent,initial)
    assert calls == ["inventory","refresh_inventory"]
    assert refreshed.flight.quote.provider_id == "google_flights" and refreshed.flight.quote.value.kind == "lower_bound"
    assert refreshed.flight.quote.value.min == "1642"
    assert refreshed.flight.details["pricing_entity_id"] == IDENTITY
    assert refreshed.flight.quote.observed_at == discovered.flight.quote.observed_at
    assert refreshed.flight.quote.retrieved_at == discovered.flight.quote.retrieved_at
    assert refreshed.flight.quote.expires_at == discovered.flight.quote.expires_at
    assert not refreshed.flight.details.get("pricing_refresh_warning")
    assert initial.flight.quote.value.kind == "unknown" and not initial.flight.details


@pytest.mark.parametrize("field,value", [("adults",1),("origin","PFO"),("date_from","2026-10-29"),
    ("currency","USD"),("unit","person_trip"),("entity_id","google-flight:"+"b"*64)])
def test_first_route_on_refresh_still_rejects_independently_wrong_scope(field,value):
    discovered, place, origin, intent = observed()
    current = discovered.model_copy(deep=True)
    current.flight.details = {}
    current.flight.quote = unknown("google_flights",context_for("flight",place,origin,intent),"No source fact","no_offers")
    discovered.flight.quote = discovered.flight.quote.model_copy(update={"scope":discovered.flight.quote.scope.model_copy(update={field:value})})
    assert not HttpTravelDataPort._compatible_refresh(current,discovered,place,origin,intent)


def test_fixed_refresh_cannot_replace_route_even_with_consistent_new_identity_and_hash():
    current, place, origin, intent = observed()
    replacement = current.model_copy(deep=True)
    new_identity = "google-flight:"+"b"*64
    replacement.flight.details["pricing_entity_id"] = new_identity
    replacement.flight.quote = replacement.flight.quote.model_copy(update={
        "request_context_hash":request_context_hash(context_for("flight",place,origin,intent,entity_id=new_identity)),
        "scope":replacement.flight.quote.scope.model_copy(update={"entity_id":new_identity})})
    assert not HttpTravelDataPort._compatible_refresh(current,replacement,place,origin,intent)
