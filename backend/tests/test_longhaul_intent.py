"""Stay dates, departure windows and agent/gateway boundaries stay independent."""
from datetime import date

import httpx
import pytest
from pydantic import ValidationError

from backend.app.data_gateway import create_gateway_app
from backend.app.domain.travel_data import DataIntent, context_for
from backend.app.geography import find_place
from backend.app.infrastructure.http_travel_data import HttpTravelDataPort
from backend.app.models import TravelIntent, merge_intent
from backend.app.planner import blocking_question
from backend.app.pricing.normalization import request_context_hash
from backend.app.travel_dates import departure_dates
from backend.tests.test_flight_http_boundary import observed, IDENTITY


def intent(**updates):
    return TravelIntent(origin="Казань", destination="Бора-Бора", date_from="2026-12-31",
        date_to="2027-01-07", nights_min=7, nights_max=7, soft_constraints=["budget_unlimited"],
        **updates)


@pytest.mark.parametrize("name", ["Бора-Бора", "Бора Бора", "боро-боро", "Bora Bora", "Bora-Bora"])
def test_bora_aliases_resolve_destination_airport_without_remote_geocoding(name):
    place = find_place(name)
    assert (place.name, place.country_code, place.airport) == ("Бора-Бора", "PF", "BOB")
    assert find_place("Казани").airport == "KZN"
    assert find_place("Tahiti").airport == "PPT"
    assert find_place("Bora Bora, France") is None


@pytest.mark.parametrize("start,end", [("2026-12-28", None), (None, "2026-12-30"),
    ("2026-12-27", "2026-12-29"), ("2026-12-28", "2026-12-31"),
    ("2026-12-30", "2027-01-01"), ("2026-12-30", "2026-12-29")])
def test_invalid_windows_rejected_in_both_intent_boundaries(start, end):
    payload = intent().model_dump() | {"flight_departure_from": start, "flight_departure_to": end}
    with pytest.raises(ValidationError):
        TravelIntent.model_validate(payload)
    with pytest.raises(ValidationError):
        DataIntent.model_validate({k: v for k, v in payload.items() if k in DataIntent.model_fields})


def test_window_replan_preserves_stay_and_stay_shift_preserves_lead_offsets():
    initial = intent(flight_departure_from="2026-12-28", flight_departure_to="2026-12-30")
    changed, fields = merge_intent(initial, {"flight_departure_from": "2026-12-29"})
    assert fields == ["flight_departure_from"]
    assert (changed.date_to - changed.date_from).days == 7
    assert departure_dates(changed) == (date(2026, 12, 30), date(2026, 12, 29))
    shifted, _ = merge_intent(changed, {"date_from": "2027-01-10"})
    assert (shifted.flight_departure_from, shifted.flight_departure_to) == (date(2027, 1, 8), date(2027, 1, 9))
    assert shifted.date_to == date(2027, 1, 17)
    assert DataIntent.from_intent(changed).to_intent().flight_departure_from == changed.flight_departure_from
    assert departure_dates(intent()) == (date(2026, 12, 31),)


def test_past_departure_window_clarifies_even_when_stay_is_future(monkeypatch):
    monkeypatch.setattr("backend.app.planner.local_today", lambda: date(2026, 12, 29))
    assert "окно вылета" in blocking_question(intent(flight_departure_from="2026-12-28", flight_departure_to="2026-12-30"))


def flight_result():
    bundle, place, origin, _ = observed()
    request = intent(flight_departure_from="2026-12-28", flight_departure_to="2026-12-30")
    place, origin = find_place("Бора-Бора"), find_place("Казань")
    departure = date(2026, 12, 29)
    context = context_for("flight", place, origin, request, departure_date=departure)
    bundle.flight.quote = bundle.flight.quote.model_copy(update={
        "request_context_hash": request_context_hash(context),
        "scope": bundle.flight.quote.scope.model_copy(update={"origin": "KZN", "destination": "BOB",
            "date_from": departure, "date_to": request.date_to})})
    bundle.flight.details["flight_route"] = {"date_from": departure.isoformat(),
        "outbound": {"departure_date": departure.isoformat(), "arrival_date": "2026-12-31"}}
    return bundle, place, origin, request


def test_agent_accepts_earlier_observed_date_and_rejects_echoed_wrong_date_or_arrival():
    bundle, place, origin, request = flight_result()
    assert HttpTravelDataPort._compatible_quotes(bundle, place, origin, request)
    fixed = bundle.model_copy(deep=True)
    fixed.flight.quote = fixed.flight.quote.model_copy(update={"request_context_hash": request_context_hash(
        context_for("flight", place, origin, request, entity_id=IDENTITY, departure_date=date(2026, 12, 29)))})
    assert HttpTravelDataPort._compatible_refresh(bundle, fixed, place, origin, request)
    different = fixed.model_copy(deep=True)
    different.flight.quote = different.flight.quote.model_copy(update={
        "scope": different.flight.quote.scope.model_copy(update={"date_from": date(2026, 12, 30)})})
    assert not HttpTravelDataPort._compatible_refresh(bundle, different, place, origin, request)
    for field, value in (("arrival_date", "2027-01-01"), ("departure_date", "2026-12-27")):
        bad = bundle.model_copy(deep=True)
        bad.flight.details["flight_route"]["outbound"][field] = value
        assert not HttpTravelDataPort._compatible_quotes(bad, place, origin, request)
    removed, _ = merge_intent(request, {"flight_departure_from": "2026-12-30"})
    assert not HttpTravelDataPort._compatible_refresh(bundle, bundle, place, origin, removed)


@pytest.mark.asyncio
async def test_actual_gateway_envelope_retains_earlier_date_and_stay_dates(tmp_path, monkeypatch):
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", raising=False)
    bundle, place, origin, request = flight_result()
    class FixturePort:
        async def inventory(self, actual_place, actual_origin, actual_intent):
            assert actual_intent.date_from == date(2026, 12, 31)
            assert actual_intent.date_to == date(2027, 1, 7)
            assert departure_dates(actual_intent) == departure_dates(request)
            return bundle
    app = create_gateway_app(database_path=str(tmp_path / "gateway.sqlite"), port_factory=lambda *_: FixturePort())
    async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gateway") as client:
        result = await HttpTravelDataPort(client, "http://gateway").inventory(place, origin, request)
    assert result.flight.quote.scope.date_from == date(2026, 12, 29)
    assert result.flight.quote == bundle.flight.quote
