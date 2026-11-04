"""Independent scope regressions; synthetic pages do not prove live tariffs."""
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from backend.app.domain.travel_data import ComponentBundle, context_for
from backend.app.geography import find_place
from backend.app.infrastructure.knowledge_travel_data import Extraction, ExtractedPrice, _model_excerpts, accepted_observation
from backend.app.models import TravelComponent, TravelIntent
from backend.app.pricing.normalization import request_context_hash


HOTEL = "Hotel Indigo Berlin - East Side Gallery by IHG"


def candidate(component, text, unit_text, *, unit="group_trip", direction="not_applicable", endpoint="Berlin",
              amount="65", max_amount=None, price_text=None, currency="EUR", city="Berlin"):
    place, origin = find_place(city), find_place("Larnaca")
    intent = TravelIntent(origin="Larnaca", destination=city, date_from="2026-11-06", date_to="2026-11-08")
    bundle = ComponentBundle(
        flight=TravelComponent(title="Flight", description="Synthetic test"),
        stay=TravelComponent(title=HOTEL, description="Synthetic test", details={"pricing_entity_id": "fixture:indigo"}),
        transfer=TravelComponent(title="Airport to hotel and return", description="Synthetic test"),
    )
    item = ExtractedPrice(component=component, document_index=0, amount=amount, max_amount=max_amount, currency=currency,
        evidence_text=text, price_text=price_text or currency + " " + amount, unit_text=unit_text,
        unit=unit, trip_type=direction, destination_text=endpoint if component == "transfer" else None)
    context = context_for(component, place, origin, intent,
        entity_id="fixture:indigo" if component == "stay" else None)
    document = SimpleNamespace(component=component, content=text, source_url="https://supplier.example/tariff",
        retrieved_at=datetime(2026, 10, 26, 12, 0, tzinfo=timezone.utc),
        context_hash=request_context_hash(context))
    return item, document, bundle, place, origin, intent


@pytest.mark.parametrize("source_unit", ["per person per night", "per room per night per person"])
def test_hotel_nightly_reference_needs_room_scope_and_cannot_relabel_person_rate(source_unit):
    text = f"{HOTEL}, Berlin: EUR 65 {source_unit}."
    with pytest.raises(ValueError, match="night_unit_missing"):
        accepted_observation(*candidate("stay", text, source_unit, unit="room_night"))


@pytest.mark.parametrize("source_unit", ["per night", "nightly"])
def test_model_room_claim_is_downgraded_when_source_only_supports_nightly_amount(source_unit):
    text = f"{HOTEL}, Berlin: EUR 65 {source_unit}."
    result = accepted_observation(*candidate("stay", text, source_unit, unit="room_night"))
    assert result.value.amount == "65" and result.scope.unit == "unknown"
    assert result.unit_label == "По тарифу источника: " + source_unit
    assert result.normalized_group is None and result.scope.rooms is None


@pytest.mark.parametrize("source_unit", ["return per vehicle", "per vehicle"])
def test_one_way_is_not_inferred_from_return_or_unspecified_source(source_unit):
    text = f"BER airport to Berlin city centre, taxi EUR 65 {source_unit}."
    with pytest.raises(ValueError, match="one_way_not_sourced"):
        accepted_observation(*candidate("transfer", text, source_unit, direction="one_way"))


@pytest.mark.parametrize("source_unit", ["transfer per person", "per vehicle per person"])
def test_per_person_transfer_cannot_be_labeled_per_vehicle(source_unit):
    text = f"BER airport to Berlin city centre, shuttle EUR 65 {source_unit}."
    with pytest.raises(ValueError, match="vehicle_unit_missing"):
        accepted_observation(*candidate("transfer", text, source_unit))


def test_city_inside_airport_name_does_not_establish_destination_endpoint():
    text = "Berlin Brandenburg Airport to Potsdam taxi EUR 65 per vehicle one way."
    with pytest.raises(ValueError, match="transfer_destination_missing"):
        accepted_observation(*candidate("transfer", text, "per vehicle one way", direction="one_way"))


def test_airport_to_requested_city_accepts_actual_per_person_one_way_scope():
    text = "BER airport to Berlin city centre, shuttle EUR 65 per person one way."
    result = accepted_observation(*candidate("transfer", text, "per person one way",
        unit="person_trip", direction="one_way"))
    assert result.value.amount == "65"
    assert result.scope.unit == "person_trip" and result.scope.trip_type == "one_way"
    assert result.normalized_group is None and "одну сторону" in result.unit_label


def test_explicit_return_scope_is_retained_without_adding_a_second_fare():
    text = "BER airport to Berlin city centre, taxi EUR 65 per vehicle return."
    result = accepted_observation(*candidate("transfer", text, "per vehicle return", direction="roundtrip"))
    assert result.value.amount == "65" and result.scope.trip_type == "roundtrip"
    assert result.normalized_group is None and "туда и обратно" in result.unit_label


def test_unstated_transfer_direction_remains_unstated_with_original_source_time():
    text = "BER airport to Berlin city centre, taxi EUR 65 per vehicle."
    args = candidate("transfer", text, "per vehicle")
    result = accepted_observation(*args)
    assert result.scope.trip_type == "not_applicable"
    assert "в одну сторону" not in result.unit_label and "туда и обратно" not in result.unit_label
    assert result.observed_at == result.retrieved_at == args[1].retrieved_at
    assert result.normalized_group is None


@pytest.mark.parametrize("gap", ["\n", "\t", "\u00a0", "  "])
def test_whitespace_rendering_differences_preserve_russian_vehicle_tariff(gap):
    source = f"Такси из аэропорта BER в Берлин: 71,40{gap}EUR на{gap}машину в одну сторону."
    args = candidate("transfer", source, "на машину в одну сторону", endpoint="Берлин",
        amount="71.40", price_text="71,40 EUR", direction="one_way")
    # Extraction may flatten the page renderer's newline/NBSP without changing its words.
    args[0].evidence_text = "Такси из аэропорта BER в Берлин: 71,40 EUR на машину в одну сторону."
    result = accepted_observation(*args)
    assert result.value.amount == "71.40" and result.scope.unit == "group_trip"
    assert result.scope.trip_type == "one_way" and result.normalized_group is None
    assert result.retrieved_at == args[1].retrieved_at


def test_russian_person_ticket_stays_per_person_without_trip_total():
    text = "Аэропорт BER → Берлин: билет EUR 5 за человека в одну сторону, зоны ABC."
    result = accepted_observation(*candidate("transfer", text, "за человека в одну сторону",
        unit="person_trip", endpoint="Берлин", amount="5", direction="one_way"))
    assert result.value.amount == "5" and result.scope.unit == "person_trip"
    assert result.normalized_group is None and result.scope.adults is None


@pytest.mark.parametrize("price_text", ["EUR 60–70", "EUR 60-70", "60 до 70 EUR"])
def test_explicit_source_range_retains_both_endpoints_and_russian_vehicle_unit(price_text):
    text = f"Такси аэропорт BER → Берлин: {price_text} на машину в одну сторону."
    result = accepted_observation(*candidate("transfer", text, "на машину в одну сторону",
        endpoint="Берлин", amount="60", max_amount="70", price_text=price_text, direction="one_way"))
    assert result.value.kind == "closed_range"
    assert result.value.min == "60" and result.value.max == "70"
    assert result.scope.unit == "group_trip" and result.normalized_group is None


def test_model_cannot_collapse_a_printed_range_to_one_endpoint():
    text = "BER airport to Berlin city centre taxi EUR 60–70 per vehicle one way."
    with pytest.raises(ValueError, match="range_endpoint_missing"):
        accepted_observation(*candidate("transfer", text, "per vehicle one way",
            amount="60", price_text="EUR 60–70", direction="one_way"))


@pytest.mark.parametrize("maximum", ["50", "75"])
def test_model_cannot_invent_or_reverse_a_range_endpoint(maximum):
    text = "BER airport to Berlin city centre taxi EUR 60–70 per vehicle one way."
    with pytest.raises(ValueError, match="ungrounded_range"):
        accepted_observation(*candidate("transfer", text, "per vehicle one way",
            amount="60", max_amount=maximum, price_text="EUR 60–70", direction="one_way"))


@pytest.mark.parametrize("endpoint", ["60", "70"])
def test_model_cannot_repeat_one_endpoint_to_hide_the_rest_of_source_range(endpoint):
    text = "BER airport to Berlin city centre taxi EUR 60–70 per vehicle one way."
    with pytest.raises(ValueError):
        accepted_observation(*candidate("transfer", text, "per vehicle one way",
            amount=endpoint, max_amount=endpoint, price_text="EUR 60–70", direction="one_way"))


def test_separate_source_numbers_without_range_marker_do_not_become_range():
    text = "BER airport to Berlin city centre taxi EUR 60 per vehicle one way; phone 70."
    with pytest.raises(ValueError, match="ungrounded_range"):
        accepted_observation(*candidate("transfer", text, "per vehicle one way",
            amount="60", max_amount="70", price_text="EUR 60", direction="one_way"))


def test_whitespace_normalization_does_not_allow_changed_source_words():
    source = "BER airport to Berlin city centre taxi EUR 65 per vehicle return."
    args = candidate("transfer", source, "per vehicle one way", direction="one_way")
    args[0].evidence_text = source.replace("return", "one way")
    with pytest.raises(ValueError, match="ungrounded_proof"):
        accepted_observation(*args)


def test_russian_return_tariff_cannot_be_reinterpreted_as_outbound_word():
    text = "Трансфер аэропорт BER → Берлин: EUR 65 на машину туда и обратно."
    with pytest.raises(ValueError):
        accepted_observation(*candidate("transfer", text, "на машину туда и обратно",
            endpoint="Берлин", direction="one_way"))


def test_printed_cad_nightly_reference_does_not_invent_room_scope_or_eur_conversion():
    text = f"{HOTEL}, Berlin: CAD 109 nightly."
    result = accepted_observation(*candidate("stay", text, "nightly",
        unit="unknown", amount="109", currency="CAD"))
    assert result.value.amount == "109" and result.value.currency == "CAD"
    assert result.scope.unit == "unknown" and result.scope.adults is None and result.scope.rooms is None
    assert result.unit_label == "По тарифу источника: nightly"
    assert result.outcome == "reference" and result.normalized_group is None


def test_unknown_hotel_scope_still_requires_explicit_nightly_source_wording():
    text = f"{HOTEL}, Berlin: CAD 109 for accommodation."
    with pytest.raises(ValueError, match="night_unit_missing"):
        accepted_observation(*candidate("stay", text, "for accommodation",
            unit="unknown", amount="109", currency="CAD"))


@pytest.mark.parametrize("charge", ["security deposit", "pet fee", "extra bed", "депозит", "за питомца"])
def test_ancillary_hotel_charges_cannot_fill_missing_accommodation_rate(charge):
    text = f"{HOTEL}, Berlin: {charge} CAD 109 nightly."
    with pytest.raises(ValueError, match="ancillary_not_lodging_rate"):
        accepted_observation(*candidate("stay", text, "nightly",
            unit="unknown", amount="109", currency="CAD"))


def test_alternative_candidate_schema_is_bounded_at_nine_outputs():
    item = candidate("transfer", "BER airport to Berlin taxi EUR 65 per vehicle.", "per vehicle")[0]
    assert len(Extraction(prices=[item] * 9).prices) == 9
    with pytest.raises(ValidationError):
        Extraction(prices=[item] * 10)


@pytest.mark.parametrize("city,airport,endpoint,heading", [
    ("Berlin", "BER", "Берлин", "| Класс автомобиля | Человек | Багаж | Цена |"),
    ("Prague", "PRG", "Prague", "| Vehicle class | Passengers | Luggage | Price |"),
])
def test_structured_vehicle_table_is_generic_and_retains_class_capacity_unknown_direction(city, airport, endpoint, heading):
    text = f"Transfer from {airport} airport to {endpoint}. {heading} | Saloon | 4 | 3 | EUR 81.25 |"
    result = accepted_observation(*candidate("transfer", text, "Saloon", amount="81.25",
        direction="one_way", city=city, endpoint=endpoint))
    assert result.value.amount == "81.25" and result.scope.unit == "group_trip"
    assert "Saloon" in result.unit_label and "до 4 пассажиров" in result.unit_label
    assert "за автомобиль" in result.unit_label
    assert result.scope.trip_type == "not_applicable"
    assert "в одну сторону" not in result.unit_label and "туда и обратно" not in result.unit_label
    assert result.normalized_group is None


def test_structured_vehicle_class_must_belong_to_the_row_with_printed_amount():
    text = ("BER airport to Berlin transfer. | Vehicle class | Passengers | Luggage | Price | "
        "| Saloon | 4 | 3 | EUR 81.25 | | Estate | 4 | 4 | EUR 90.50 |")
    with pytest.raises(ValueError, match="vehicle_unit_missing"):
        accepted_observation(*candidate("transfer", text, "Estate", amount="81.25"))


@pytest.mark.parametrize("heading", [
    "| Passengers | Luggage | Price |",
    "| Description | Passengers | Luggage | Price |",
    "",
])
def test_capacity_row_without_explicit_vehicle_structure_cannot_imply_per_car_price(heading):
    text = f"BER airport to Berlin transfer. {heading} | Saloon | 4 | 3 | EUR 81.25 |"
    with pytest.raises(ValueError, match="vehicle_unit_missing"):
        accepted_observation(*candidate("transfer", text, "Saloon", amount="81.25"))


def test_structured_vehicle_table_cannot_override_source_per_person_charge():
    text = ("BER airport to Berlin transfer, prices per person. "
        "| Vehicle class | Passengers | Luggage | Price | | Saloon | 4 | 3 | EUR 81.25 |")
    with pytest.raises(ValueError, match="vehicle_unit_missing"):
        accepted_observation(*candidate("transfer", text, "Saloon", amount="81.25"))


def test_structural_class_and_currency_price_proof_cannot_be_taken_from_different_rows():
    text = ("BER airport to Berlin transfer. | Vehicle class | Passengers | Luggage | Price | "
        "| Saloon | 4 | 3 | GBP 81.25 | | Estate | 4 | 4 | EUR 81.25 |")
    with pytest.raises(ValueError):
        accepted_observation(*candidate("transfer", text, "Saloon", amount="81.25", currency="EUR"))


def test_structural_vehicle_range_endpoints_must_belong_to_the_selected_class_row():
    text = ("BER airport to Berlin transfer. | Vehicle class | Passengers | Luggage | Price | "
        "| Saloon | 4 | 3 | EUR 60–70 | | Estate | 4 | 4 | EUR 60–80 |")
    with pytest.raises(ValueError):
        accepted_observation(*candidate("transfer", text, "Saloon", amount="60", max_amount="80",
            price_text="EUR 60–80"))


def separate_route_candidate(route="BER airport to Berlin transfer", *, distance=80):
    table = "| Vehicle class | Passengers | Luggage | Price | | Saloon | 4 | 3 | EUR 81.25 |"
    args = candidate("transfer", table, "Saloon", amount="81.25", direction="one_way")
    args[0].route_text = route
    args[1].content = route + " " + ("x" * distance) + " " + table
    return args


def test_separate_copied_route_heading_and_same_page_vehicle_table_qualify():
    args = separate_route_candidate()
    result = accepted_observation(*args)
    assert result.value.amount == "81.25" and result.scope.unit == "group_trip"
    assert result.scope.trip_type == "not_applicable" and "Saloon" in result.unit_label
    assert result.normalized_group is None and result.retrieved_at == args[1].retrieved_at


@pytest.mark.parametrize("route", ["MUC airport to Munich transfer", "BER airport to Potsdam transfer"])
def test_separate_route_proof_still_requires_requested_airport_and_destination(route):
    with pytest.raises(ValueError):
        accepted_observation(*separate_route_candidate(route))


def test_route_heading_not_copied_from_selected_document_is_rejected():
    args = separate_route_candidate()
    args[0].route_text = "BER airport to Berlin chauffeured transfer"
    with pytest.raises(ValueError, match="ungrounded_route"):
        accepted_observation(*args)


def test_source_route_heading_cannot_bind_a_price_excerpt_more_than_3000_characters_away():
    with pytest.raises(ValueError, match="unrelated_route_price"):
        accepted_observation(*separate_route_candidate(distance=3100))


def test_separate_route_proof_does_not_allow_per_person_header_to_become_vehicle_rate():
    with pytest.raises(ValueError, match="vehicle_unit_missing"):
        accepted_observation(*separate_route_candidate("BER airport to Berlin transfer prices per person"))


def test_hotel_property_proof_cannot_be_moved_into_transport_only_route_field():
    evidence = "CAD 109 nightly."
    args = candidate("stay", evidence, "nightly", unit="unknown", amount="109", currency="CAD")
    args[0].route_text = f"{HOTEL}, Berlin"
    args[1].content = args[0].route_text + " " + evidence
    with pytest.raises(ValueError, match="property_or_city_mismatch"):
        accepted_observation(*args)


@pytest.mark.parametrize("route,heading", [
    ("From: BER airport To: Berlin 25 km", "| Vehicle class | Passengers | Luggage | Price |"),
    ("Из: аэропорт BER В: Берлин 25 км", "| Класс автомобиля | Человек | Багаж | Цена |"),
])
def test_actual_source_route_and_vehicle_headers_recover_model_row_only_quote(route, heading):
    row = "| Saloon | 4 | 3 | EUR 81.25 |"
    args = candidate("transfer", row, "Saloon", amount="81.25", direction="one_way")
    args[0].destination_text = None
    args[1].content = route + " " + heading + " " + row
    result = accepted_observation(*args)
    assert result.value.amount == "81.25" and result.value.currency == "EUR"
    assert result.scope.unit == "group_trip" and result.scope.trip_type == "not_applicable"
    assert "Saloon" in result.unit_label and "до 4 пассажиров" in result.unit_label
    assert "в одну сторону" not in result.unit_label and "туда и обратно" not in result.unit_label
    assert result.normalized_group is None and result.retrieved_at == args[1].retrieved_at


def test_literal_source_recovery_uses_actual_destination_instead_of_model_request_city():
    row = "| Saloon | 4 | 3 | EUR 81.25 |"
    args = candidate("transfer", row, "Saloon", amount="81.25", direction="one_way", endpoint="Berlin")
    args[1].content = ("From: BER airport To: Potsdam 25 km "
        "| Vehicle class | Passengers | Luggage | Price | " + row)
    with pytest.raises(ValueError, match="transfer_destination_missing"):
        accepted_observation(*args)


def test_transfer_unit_omitted_from_excerpt_is_recovered_literally_and_invented_words_reject():
    args = candidate("transfer", "Ticket EUR 5.", "per person one way",
        amount="5", unit="person_trip", direction="one_way")
    args[0].destination_text = None
    args[1].content = ("From: BER airport To: Berlin 25 km Ticket EUR 5. "
        "Single adult ticket per person one way.")
    result = accepted_observation(*args)
    assert result.value.amount == "5" and result.scope.unit == "person_trip"
    assert result.scope.trip_type == "one_way" and result.normalized_group is None
    invented = args[0].model_copy(update={"unit_text": "per vehicle one way", "unit": "group_trip"})
    with pytest.raises(ValueError, match="ungrounded_proof"):
        accepted_observation(invented, *args[1:])


def test_transfer_model_price_passages_are_bounded_contiguous_original_page_slices():
    content = ("a" * 1700 + " EUR 11 " + "b" * 4000 + " EUR 22 " + "c" * 4000
        + " EUR 33 " + "d" * 4000 + " EUR 44 " + "e" * 1700)
    excerpts = _model_excerpts(content)
    assert len(excerpts) == 3
    assert all(excerpt in content and len(excerpt) <= 3220 for excerpt in excerpts)
    assert all(any(price in excerpt for excerpt in excerpts) for price in ("EUR 11", "EUR 22", "EUR 33"))
    assert not any("EUR 44" in excerpt for excerpt in excerpts)
