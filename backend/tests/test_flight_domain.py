"""Visible-source interpretation and shared values; no browser/network/model."""
from datetime import date, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from backend.app.pricing.models import RequestContext
from backend.app.pricing.normalization import request_context_hash as gateway_hash
from flight_search.domain import (Card, SourceError, eligible_leg, make_route, parse_card, query_url,
                                  read_scope, scope_diagnostic, select_route, stop_diagnostic)
from flight_search.models import (FlightConstraints, FlightRequestContext, FlightSearchRequest,
                                  FlightSearchResponse, SelectedRoute, context_hash, search_hash)


def command(**changes):
    context = FlightRequestContext(origin="LCA", destination="BER", date_from="2026-10-30", date_to="2026-11-01")
    values = dict(request_id="fixture", deadline_at=datetime.now(timezone.utc)+timedelta(seconds=35),
                  request_context=context, request_context_hash=context_hash(context))
    values.update(changes)
    if "request_context" in changes:
        values["request_context_hash"] = context_hash(values["request_context"])
    return FlightSearchRequest(**values)


def snapshot(**changes):
    value = dict(track_label="Track prices from Larnaca to Berlin departing 2026-10-30 and returning 2026-11-01",
        origin_input="Larnaca", destination_input="Berlin", trip="Round trip", cabin="Economy", currency="Currency EUR",
        passengers="2 passengers, change number of passengers.", departure="Fri, Oct 30", **{"return":"Sun, Nov 1"},
        body="Prices include required taxes + fees for 2 adults. Optional charges and bag fees may apply.")
    value.update(changes)
    return value


def rows():
    return ({"text":"LCA–BER\n€2,000", "label":"From 2000 euros round trip total. 1 stop flight with Edelweiss Air and Eurowings. Leaves Larnaka International Airport at 3:30 AM on Friday, October 30 and arrives at Berlin Brandenburg Airport at 10:30 AM on Friday, October 30. Total duration 8 hr. Layover (1 of 1) is a 2 hr 35 min layover at Zurich Airport in Zürich. Select flight"},
        {"text":"BER–LCA\n€1,622", "label":"From 1622 euros round trip total. 2 stops flight with Lufthansa and Discover Airlines. Leaves Berlin Brandenburg Airport at 2:40 PM on Sunday, November 1 and arrives at Larnaka International Airport at 2:05 AM on Monday, November 2. Total duration 10 hr 25 min. Layover (1 of 2) is a 1 hr 25 min layover at Frankfurt Airport in Frankfurt am Main. Layover (2 of 2) is a 3 hr 35 min layover at Munich International Airport in Munich. Select flight"})


def route():
    outbound, inbound = rows()
    return make_route(read_scope(snapshot(), command()), parse_card(outbound, "LCA", "BER", date(2026,10,30)),
        parse_card(inbound, "BER", "LCA", date(2026,11,1)), "https://www.google.com/travel/flights/search?hl=en&curr=EUR")


def test_shared_context_hash_is_exact_existing_contract_and_transport_identity_is_not_cache_identity():
    cmd = command()
    original = RequestContext.model_validate(cmd.request_context.model_dump(mode="json"))
    assert gateway_hash(original) == cmd.request_context_hash
    assert search_hash(cmd) == search_hash(command(request_id="other"))
    assert search_hash(cmd) != search_hash(command(constraints=FlightConstraints(prefer_direct=True)))
    assert search_hash(cmd) != search_hash(command(target_itinerary_id="a"*64))


def test_observed_overnight_route_preserves_dates_local_times_and_only_paired_return_amount():
    value = route()
    assert value.price.amount == "1622"  # Never outbound 2000 + return 1622.
    assert value.price.kind == "lower_bound" and value.price.unit == "group_trip" and value.price.adults == 2
    assert value.inbound.departure_date == date(2026,11,1) and value.inbound.arrival_date == date(2026,11,2)
    assert (value.outbound.departure_time, value.inbound.arrival_time) == ("03:30", "02:05")
    assert not value.price.final_supplier_offer and not value.price.availability_verified
    assert not value.price.fees_complete and not value.price.baggage_verified
    assert not value.inbound.timezone_verified and not value.inbound.connections_verified
    assert value.expires_at-value.observed_at == timedelta(minutes=10)


def test_identity_ignores_price_and_ranking_but_retains_flight_facts_and_fixed_target():
    value = route()
    outbound, inbound = rows()
    old = parse_card(inbound, "BER", "LCA", date(2026,11,1))
    changed = make_route(read_scope(snapshot(), command()), parse_card(outbound,"LCA","BER",date(2026,10,30)),
                         Card(old.label, old.leg, "1777"), value.source_url)
    assert changed.itinerary_id == value.itinerary_id and changed.price.amount != value.price.amount
    assert select_route([changed, value], command(target_itinerary_id=value.itinerary_id)).price.amount == "1622"
    assert select_route([value], command(target_itinerary_id="a"*64)) is None
    assert select_route([value], command(constraints=FlightConstraints(direct_required=True))) is None
    assert select_route([value], command(constraints=FlightConstraints(max_flight_minutes=600))) is None


def test_identity_retains_connection_airport_and_layover_duration():
    original = route()
    outbound, inbound = rows()
    for changed in (outbound["label"].replace("Zurich Airport in Zürich", "Vienna International Airport in Vienna"),
                    outbound["label"].replace("2 hr 35 min layover", "3 hr 35 min layover")):
        other = make_route(read_scope(snapshot(), command()), parse_card({**outbound,"label":changed},"LCA","BER",date(2026,10,30)),
            parse_card(inbound,"BER","LCA",date(2026,11,1)), original.source_url)
        assert other.itinerary_id != original.itinerary_id
        assert select_route([other], command(target_itinerary_id=original.itinerary_id)) is None


@pytest.mark.parametrize("change,code", [
    ({"track_label":snapshot()["track_label"].replace("2026", "2037")},"flight_date_mismatch"),
    ({"track_label":""},"flight_scope_not_verified"),
    ({"origin_input":"Paphos"},"flight_route_mismatch"),
    ({"departure":"Fri, Oct 3"},"flight_date_mismatch"),
    ({"passengers":"1 passenger, change number of passengers."},"flight_party_mismatch"),
    ({"body":"Prices include required taxes + fees for 1 adult."},"flight_party_mismatch"),
    ({"currency":"Currency USD"},"flight_currency_mismatch"),
    ({"cabin":"Business"},"flight_cabin_mismatch"),
])
def test_scope_is_independently_read_not_echoed(change, code):
    with pytest.raises(SourceError) as error:
        read_scope(snapshot(**change), command())
    assert error.value.code == code


@pytest.mark.parametrize("labels,accepted", [
    (["Friday, October 30, 2026, departure date.","Sunday, November 1, 2026, return date."],True),
    (["Friday, October 30, 2037, departure date.","Sunday, November 1, 2037, return date."],False),
    (["Friday, October 30, 2026, departure date."],False),
    (["Friday, October 30, 2026, departure date."]*2,False),
    (["Friday, October 30, 2026.","Sunday, November 1, 2026."],False),
])
def test_calendar_full_year_selected_date_markers_are_independent_source_proof(labels, accepted):
    value = snapshot(track_label="", selected_date_labels=labels)
    if accepted:
        assert read_scope(value,command()).date_from == date(2026,10,30)
    else:
        with pytest.raises(SourceError):
            read_scope(value,command())


@pytest.mark.parametrize("change", [
    {"text":"PFO–BER"}, {"text":"LCA–BER PFO–BER"}, {"text":"Larnaca to Berlin"},
    {"label":rows()[0]["label"].replace("euros round trip total", "euros per person")},
    {"label":rows()[0]["label"].replace("Friday, October 30", "Thursday, October 30")},
    {"label":rows()[0]["label"].replace("2000 euros", "0 euros")},
    {"label":rows()[0]["label"].replace("Larnaka International Airport", "Paphos International Airport")},
    {"label":rows()[0]["label"].replace("Layover (1 of 1)", "Layover (2 of 2)")},
    {"label":rows()[0]["label"].replace("Layover (1 of 1) is a 2 hr 35 min layover at Zurich Airport in Zürich. ", "")},
    {"text":"LCA–BER\nBusiness Class"},
    {"text":"LCA–BER\nBusiness Class + Economy"},
    {"text":"LCA–BER\nPremium Economy"},
])
def test_card_requires_explicit_airports_dates_and_group_roundtrip_basis(change):
    assert parse_card({**rows()[0], **change}, "LCA", "BER", date(2026,10,30)) is None


@pytest.mark.parametrize("context_change,constraints,code", [
    ({"destination":"XXX"},{},"flight_unsupported_route"),
    ({"children_ages":(5,)},{},"flight_unsupported_party"),
    ({"adults":3},{},"flight_unsupported_party"),
    ({"currency":"USD"},{},"flight_only_eur"),
    ({},{"cabin":"business"},"flight_unsupported_cabin"),
    ({"trip_type":"one_way"},{},"flight_unsupported_constraints"),
    ({"fare_constraints":("transport:train",)},{},"flight_unsupported_constraints"),
])
def test_unsupported_scope_is_explicit_before_navigation(context_change, constraints, code):
    context = FlightRequestContext.model_validate({**command().request_context.model_dump(), **context_change})
    assert scope_diagnostic(command(request_context=context, constraints=FlightConstraints(**constraints)))[0] == code


def test_fixed_policy_missing_target_or_changed_hash_is_rejected():
    context = command().request_context.model_copy(update={"target_policy":"fixed_entity", "entity_id":"google-flight:"+"a"*64})
    with pytest.raises(ValidationError):
        command(request_context=context)
    assert command(request_context=context, target_itinerary_id="a"*64).target_itinerary_id == "a"*64
    with pytest.raises(ValidationError):
        command(request_context_hash="b"*64)
    with pytest.raises(ValidationError):
        command(deadline_at=datetime(2026,10,30))


@pytest.mark.parametrize("url", ["https://vendor.example/pay", "https://www.google.com/travel/flights/booking",
    "https://user:secret@www.google.com/travel/flights", "http://www.google.com/travel/flights"])
def test_route_never_carries_vendor_or_booking_url(url):
    with pytest.raises(ValidationError):
        SelectedRoute.model_validate({**route().model_dump(), "source_url":url})


def test_query_is_structured_finite_and_source_errors_do_not_become_prices():
    assert "2026-10-30" in query_url(command()) and "Larnaca" in query_url(command())
    assert stop_diagnostic("Verify you are human")[1] == "blocked"
    assert stop_diagnostic("Sign in to continue")[1] == "requires_login"
    assert stop_diagnostic("Oops, something went wrong")[1] == "unavailable"
    with pytest.raises(ValidationError):
        FlightSearchResponse(request_id="x", request_context_hash="a"*64, outcome="timeout",
                             diagnostic_code="flight_timeout", route=route())
