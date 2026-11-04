"""Document roundtrip binding uses sourced flight date, never hotel-date shifts."""
from datetime import date, datetime, timezone
import json
from types import SimpleNamespace

import pytest

from backend.app.domain.travel_data import ComponentBundle, PriceDocument, context_for
from backend.app.geography import find_place
from backend.app.models import TravelComponent, TravelIntent
from backend.app.pricing.documents import _context_hash, price_query
from backend.app.pricing.normalization import request_context_hash
from backend.app.infrastructure.knowledge_travel_data import ExtractedPrice, KnowledgeTravelDataPort, accepted_observation
from backend.app.travel_dates import selected_departure_date


def scenario():
    intent = TravelIntent(origin="Larnaca", destination="Berlin", date_from="2026-12-31",
                          date_to="2027-01-07", flight_departure_from="2026-12-29",
                          flight_departure_to="2026-12-31", adults=2)
    bundle = ComponentBundle(flight=TravelComponent(title="Flight", description="Unknown",
        details={"flight_route": {"date_from": "2026-12-30"}}),
        stay=TravelComponent(title="Fixture Hotel Berlin", description="Unknown",
            source_url="https://example.org/hotel"),
        transfer=TravelComponent(title="Airport transfer", description="Unknown"))
    return find_place("Berlin"), find_place("Larnaca"), intent, bundle


def flight_item():
    return ExtractedPrice(component="flight", document_index=0, amount="800", currency="EUR",
        evidence_text="LCA to BER flight EUR 800 per person roundtrip.", price_text="EUR 800",
        unit_text="per person roundtrip", unit="person_trip", trip_type="roundtrip")


def doc(name, text, place, origin, intent, bundle):
    return PriceDocument(component=name, title="Synthetic tariff", content=text,
        source_url="https://supplier.example/" + name, retrieved_at=datetime.now(timezone.utc),
        context_hash=_context_hash(name, place, origin, intent, bundle))


def test_selected_flight_query_and_document_roundtrip_do_not_shift_hotel_dates():
    place, origin, intent, bundle = scenario()
    expected = context_for("flight", place, origin, intent, departure_date=date(2026, 12, 30))
    assert _context_hash("flight", place, origin, intent, bundle) == request_context_hash(expected)
    assert "2026-12-30 2027-01-07" in price_query("flight", place, origin, intent, bundle)
    assert "2026-12-31 2027-01-07" in price_query("stay", place, origin, intent, bundle)
    item = flight_item()
    document = doc("flight", item.evidence_text, place, origin, intent, bundle)
    result = accepted_observation(item, document, bundle, place, origin, intent)
    assert result.request_context_hash == document.context_hash
    assert result.scope.date_from is None  # Requested date never becomes source proof.
    assert result.value.amount == "800" and result.outcome == "reference"
    assert (intent.date_to - intent.date_from).days == 7


def test_outside_window_date_never_authorizes_old_document_or_quote_retention():
    place, origin, intent, bundle = scenario()
    item = flight_item()
    old = doc("flight", item.evidence_text, place, origin, intent, bundle)
    bundle.flight.quote = accepted_observation(item, old, bundle, place, origin, intent)
    bundle.flight.details["flight_route"]["date_from"] = "2026-12-20"
    assert selected_departure_date(intent, bundle.flight) == date(2026, 12, 31)
    with pytest.raises(ValueError, match="document_context_mismatch"):
        accepted_observation(item, old, bundle, place, origin, intent)


@pytest.mark.asyncio
async def test_single_extraction_batch_gets_component_dates_and_cache_rebinds_window():
    place, origin, intent, bundle = scenario()
    item = flight_item()
    class Base:
        calls = 0
        async def price_documents(self, p, o, i, current):
            self.calls += 1
            return SimpleNamespace(documents=[doc("flight", item.evidence_text, p, o, i, current)])
    class Model:
        configured = True
        calls = []
        async def completion(self, messages, *args, **kwargs):
            self.calls.append(json.loads(messages[-1]["content"]))
            return json.dumps({"prices": [item.model_dump()]})
    model, base = Model(), Base()
    port = KnowledgeTravelDataPort(base, model)
    first = await port._lookup(place, origin, intent, bundle.model_copy(deep=True))
    second = await port._lookup(place, origin, intent, bundle.model_copy(deep=True))
    assert first.flight.quote == second.flight.quote
    assert len(model.calls) == 1 and base.calls == 1
    requested = model.calls[0]["requested"]
    assert requested["component_dates"]["flight"]["date_from"] == "2026-12-30"
    assert requested["component_dates"]["stay"] == {"date_from": "2026-12-31", "date_to": "2027-01-07"}
    assert model.calls[0]["documents"][0]["requested_dates"]["date_from"] == "2026-12-30"
    bundle.flight.details["flight_route"]["date_from"] = "2026-12-20"
    await port._lookup(place, origin, intent, bundle)
    assert len(model.calls) == 2 and base.calls == 2
    assert model.calls[1]["requested"]["flight_departure_date"] == "2026-12-31"


@pytest.mark.asyncio
@pytest.mark.parametrize("stored_date,retained", [("2026-12-30", True), ("2026-12-20", False)])
async def test_refresh_retains_only_same_allowed_departure_hash(stored_date, retained):
    place, origin, intent, bundle = scenario()
    item = flight_item()
    document = doc("flight", item.evidence_text, place, origin, intent, bundle)
    previous = accepted_observation(item, document, bundle, place, origin, intent)
    bundle.flight.quote = previous
    bundle.flight.details["flight_route"]["date_from"] = stored_date
    class Base:
        async def refresh_inventory(self, p, o, i, current):
            updated = current.model_copy(deep=True)
            updated.flight.quote = None
            return updated
    port = KnowledgeTravelDataPort(Base(), SimpleNamespace(configured=False))
    result = await port.refresh_inventory(place, origin, intent, bundle)
    assert (result.flight.quote == previous) is retained
    if not retained:
        assert result.flight.quote is None


def test_selector_checks_route_then_quote_but_neither_can_expand_window():
    place, origin, intent, bundle = scenario()
    item = flight_item()
    document = doc("flight", item.evidence_text, place, origin, intent, bundle)
    quote = accepted_observation(item, document, bundle, place, origin, intent)
    bundle.flight.quote = quote.model_copy(update={"scope": quote.scope.model_copy(update={"date_from": date(2026, 12, 29)})})
    assert selected_departure_date(intent, bundle.flight) == date(2026, 12, 30)
    bundle.flight.details["flight_route"]["date_from"] = "invalid"
    assert selected_departure_date(intent, bundle.flight) == date(2026, 12, 29)
    bundle.flight.quote = quote.model_copy(update={"scope": quote.scope.model_copy(update={"date_from": date(2026, 12, 20)})})
    assert selected_departure_date(intent, bundle.flight) == date(2026, 12, 31)
