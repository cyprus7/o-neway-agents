"""Pure interpretation checks for public browser prices; no external network."""
from datetime import date
import asyncio
import json
from pathlib import Path

import pytest

from backend.app.pricing.models import PriceObservation
from price_worker.recipes.lodging import CITIES, choose_detailed_rate, date_controls_match, euro_amount, google_hotels, google_property_id, reference_observation, select_property_label, ui_date_matches, verified_destination_query


@pytest.mark.parametrize("text,expected", [("€83", "83"), (" €1,234.56 ", "1234.56"),
    ("€0", None), ("€1,2", None), ("from €83", None), ("$83", None), ("€83 per night", None), ("€NaN", None)])
def test_only_explicit_well_formed_positive_euro_amounts(text, expected):
    assert euro_amount(text) == expected


def test_ui_date_readback_rejects_default_month_and_day_substring():
    assert ui_date_matches("Sun, Nov 1", date(2026, 11, 1))
    assert not ui_date_matches("Sun, Nov 11", date(2026, 11, 1))
    assert not ui_date_matches("Wed, Nov 25", date(2026, 10, 25))


def test_selected_full_year_and_both_date_controls_are_required():
    start, end = date(2026, 10, 25), date(2026, 11, 1)
    controls = [{"label":"Check-in", "value":"Sun, Oct 25"}, {"label":"Check-out", "value":"Sun, Nov 1"}]
    selected = [{"iso":"2026-10-25", "selected":True, "label":"Sunday, October 25, 2026, departure date."},
                {"iso":"2026-11-01", "selected":True, "label":"Sunday, November 1, 2026, return date."}]
    assert date_controls_match(controls, start, end, selected)
    assert not date_controls_match(controls[:1], start, end, selected)
    assert not date_controls_match([], start, end, selected)
    assert not date_controls_match(controls, start, end, [])
    wrong_year = [{**row, "iso":row["iso"].replace("2026", "2027"), "label":row["label"].replace("2026", "2027")} for row in selected]
    assert not date_controls_match(controls, start, end, wrong_year)
    assert not date_controls_match(controls, start, end, [selected[0]])


def test_dorm_total_is_preserved_as_reference_without_rounded_nightly_arithmetic():
    context = {"destination_iata": "ATH", "date_from": "2026-10-25", "date_to": "2026-11-01", "adults": 1}
    payload = reference_observation(context=context, property_name="Example Hotel",
        source_url="https://www.google.com/travel/search?q=hotels+in+Athens", total="83",
        room_label="Bed in 6-Bed Mixed Dormitory Room", room_guests="1 guest")
    observation = PriceObservation.model_validate(payload)
    assert observation.value.min == "83"  # Not displayed nightly 12 × 7 = 84.
    assert observation.outcome == "reference" and observation.normalized_group is None
    assert "Dormitory" in observation.unit_label and "7" in observation.unit_label
    assert observation.scope.unit == "unknown"
    assert observation.scope.rooms is None and observation.scope.adults is None
    assert observation.scope.context_verified is False and observation.availability_verified is False
    assert observation.scope.taxes == "unknown" and observation.scope.mandatory_fees == "unknown"
    assert observation.included_charges == ()


def test_advertised_two_guest_room_is_not_presented_as_verified_one_adult_scope():
    payload = reference_observation(context={"destination":"Афины", "destination_iata":"ATH", "date_from":"2026-10-25", "date_to":"2026-11-01", "adults":1},
        property_name="Example Hotel", source_url="https://www.google.com/travel/search", total="550",
        room_label="Twin Room Ensuite", room_guests="2 guests")
    observation = PriceObservation.model_validate(payload)
    assert "2 guests" in observation.unit_label
    assert observation.scope.adults is None and observation.scope.children_ages is None
    assert observation.scope.destination_city == "Афины"
    assert observation.normalized_group is None and observation.component_complete is False


def test_private_room_selected_over_cheaper_dorm_and_shared_bathroom():
    rows = [
        {"label": "Bed in 6-Bed Mixed Dormitory Room", "text": "Bed in 6-Bed Mixed Dormitory Room\n1 guest\n€83\nVisit site", "current_totals":["€83"]},
        {"label": "Triple Room with Shared Bathroom", "text": "Triple Room with Shared Bathroom\n3 guests\n€277\nVisit site", "current_totals":["€277"]},
        {"label": "Economy Double or Twin Room with Private Bathroom (2 Adults)", "text": "Economy Double or Twin Room with Private Bathroom (2 Adults)\n2 single beds · 2 guests · Balcony\n€251\nVisit site", "current_totals":["€251"]},
    ]
    chosen = choose_detailed_rate(rows)
    assert chosen["total"] == "251" and chosen["guests"] == "2 guests"
    assert "Private Bathroom" in chosen["label"]


def test_rate_without_explicit_displayed_total_is_not_invented_and_dorm_label_survives():
    assert choose_detailed_rate([{"label":"Double room", "text":"Double room\nPrice on request"}]) is None
    chosen = choose_detailed_rate([{"label":"Bed in 6-Bed Mixed Dormitory Room", "text":"Bed in 6-Bed Mixed Dormitory Room\n1 guest\n€83\nVisit site", "current_totals":["€83"]}])
    assert chosen["total"] == "83" and "Dormitory" in chosen["label"]


def test_old_first_currency_amount_is_ignored_and_ambiguous_current_prices_fail():
    row = {"label":"Double Room with Private Bathroom", "text":"Double Room with Private Bathroom\n2 guests\n€999\n€251", "current_totals":["€251"]}
    assert choose_detailed_rate([row])["total"] == "251"
    assert choose_detailed_rate([{**row, "current_totals":[]}]) is None
    assert choose_detailed_rate([{**row, "current_totals":["€251", "€277"]}]) is None
    assert choose_detailed_rate([{**row, "current_totals":["€999 €251"]}]) is None


@pytest.mark.parametrize("destination,iata,query", [
    ("Athens", "ATH", "Athens"), ("Афины", "ATH", "Athens"),
    ("  афины  ", "ATH", "Athens"), ("Sofia", "SOF", None), ("София", "SOF", None),
    ("Bansko", "SOF", "Bansko"), ("Банско", "SOF", "Bansko"),
    ("Греция", "ATH", None), ("Пирей", "ATH", None), ("Athens", "IST", None),
    (None, "ATH", None), ("", "ATH", None), ("Афины", "XXX", None),
])
def test_lodging_destination_alias_must_match_the_airport_catalogue_city(destination, iata, query):
    assert verified_destination_query({"destination":destination, "destination_iata":iata}) == query


def test_city_mismatch_never_opens_a_browser_query():
    class NoQueryPage:
        def __getattr__(self, name):
            raise AssertionError("A mismatched lodging city must not use a page")
    result = asyncio.run(google_hotels(NoQueryPage(), {"destination":"Sofia", "destination_iata":"SOF"}))
    assert result == {"outcome":"context_mismatch", "diagnostic_code":"destination_city_airport_mismatch"}


def test_frozen_twenty_trip_manifest_keeps_every_previously_supported_city_query():
    manifest = json.loads((Path(__file__).resolve().parents[2] / "specs/002-travel-pricing/benchmark.json").read_text(encoding="utf-8"))
    supported = 0
    for case in manifest["cases"]:
        previous = CITIES.get(case["destination_iata"])
        actual = verified_destination_query({"destination_iata":case["destination_iata"], "destination":case["stay_destination"]})
        assert actual == previous, case["id"]
        supported += previous is not None
    assert len(manifest["cases"]) == 20 and supported == 17


def test_fixed_property_refresh_uses_matching_entity_after_results_reorder():
    labels = ["View prices for Different Hotel", "View prices for Roy Hotel", "View prices for Other Hotel"]
    target = google_property_id("ATH", "Roy Hotel")
    assert select_property_label(labels, "ATH", target) == "View prices for Roy Hotel"
    assert select_property_label(list(reversed(labels)), "ATH", target) == "View prices for Roy Hotel"
    observation = reference_observation(context={"destination":"Афины", "destination_iata":"ATH", "date_from":"2026-10-25", "date_to":"2026-11-01", "adults":1},
        property_name="Roy Hotel", source_url="https://www.google.com/travel/search", total="281", room_label="Double Room", room_guests="2 guests")
    assert observation["scope"]["entity_id"] == target


def test_missing_fixed_property_never_falls_back_to_first_or_different_airport_entity():
    labels = ["View prices for Different Hotel", "View prices for Other Hotel"]
    assert select_property_label(labels, "ATH", google_property_id("ATH", "Roy Hotel")) is None
    assert select_property_label([], "ATH", google_property_id("ATH", "Roy Hotel")) is None
    assert select_property_label(["View prices for Roy Hotel"], "IST", google_property_id("ATH", "Roy Hotel")) is None


def test_candidate_property_search_keeps_existing_selection_rule_without_target():
    labels = ["View prices for City Hostel", "View prices for Roy Hotel", "View prices for Other Hotel"]
    assert select_property_label(labels, "ATH") == "View prices for Roy Hotel"
    assert select_property_label(["View prices for Apartment One", "View prices for Apartment Two"], "ATH") == "View prices for Apartment One"


@pytest.mark.parametrize("destination,expected", [("Berlin", "Berlin"), ("Берлин", "Berlin"),
    ("  берлин  ", "Berlin"), ("Potsdam", None), ("Потсдам", None), ("Germany", None)])
def test_berlin_coverage_requires_the_exact_reviewed_city(destination, expected):
    assert verified_destination_query({"destination": destination, "destination_iata": "BER"}) == expected


def test_lodging_wishes_prefer_a_candidate_without_claiming_its_policy():
    labels = ["View prices for City Hotel", "View prices for Quiet Garden Hotel", "View prices for Quiet Hostel"]
    assert select_property_label(labels, "BER", discovery_terms=["quiet room", "no hostels"]) == "View prices for Quiet Garden Hotel"
    assert select_property_label(["View prices for Quiet Hostel"], "BER", discovery_terms=["no hostels"]) is None
    target = google_property_id("BER", "City Hotel")
    assert select_property_label(labels, "BER", target, discovery_terms=["quiet room"]) == "View prices for City Hotel"
    observed = PriceObservation.model_validate(reference_observation(context={"destination":"Berlin", "destination_iata":"BER",
        "date_from":"2026-10-30", "date_to":"2026-11-01", "adults":2}, property_name="Quiet Garden Hotel",
        source_url="https://www.google.com/travel/search", total="251", room_label="Private Double Room", room_guests="2 guests"))
    assert not observed.scope.context_verified and not observed.availability_verified
    assert observed.normalized_group is None


def test_berlin_preflight_rejects_neighbor_currency_party_and_bad_terms_without_navigation():
    class NoNavigation:
        def __getattr__(self, name):
            raise AssertionError("Invalid context must fail before page access")
    context = {"destination":"Berlin", "destination_iata":"BER", "date_from":"2026-10-30",
        "date_to":"2026-11-01", "adults":2, "children_ages":[], "rooms":1, "currency":"EUR"}
    for changes, diagnostic in (({"destination":"Potsdam"}, "destination_city_airport_mismatch"),
            ({"currency":"USD"}, "only_eur_supported"), ({"children_ages":[8]}, "single_room_adult_only_recipe"),
            ({"rooms":2}, "single_room_adult_only_recipe"), ({"discovery_terms":["https://example.com"]}, "invalid_discovery_terms")):
        assert asyncio.run(google_hotels(NoNavigation(), {**context, **changes}))["diagnostic_code"] == diagnostic


def test_berlin_query_preserves_bounded_wishes_and_stops_on_challenge():
    from urllib.parse import parse_qs, urlsplit
    class Body:
        async def inner_text(self):
            return "Verify you are human"
    class Page:
        def __init__(self):
            self.navigations = []
        async def goto(self, url):
            self.navigations.append(url)
        def locator(self, selector):
            assert selector == "body"
            return Body()
    page = Page()
    result = asyncio.run(google_hotels(page, {"destination":"Берлин", "destination_iata":"BER",
        "date_from":"2026-10-30", "date_to":"2026-11-01", "adults":2, "rooms":1, "currency":"EUR",
        "discovery_terms":["quiet private room", "no hostels"]}))
    assert result == {"outcome":"blocked", "diagnostic_code":"challenge_detected"}
    assert len(page.navigations) == 1
    query = parse_qs(urlsplit(page.navigations[0]).query)["q"][0]
    assert "Berlin" in query and "quiet private room" in query and "no hostels" in query


def test_bounded_candidate_set_and_lodging_negation_do_not_match_a_forbidden_name():
    labels = ["View prices for City Hotel"] * 100 + ["View prices for Quiet Garden Hotel"]
    assert select_property_label(labels, "BER", discovery_terms=["quiet room"]) == "View prices for City Hotel"
    assert select_property_label(["View prices for Quiet Hostel", "View prices for City Apartment"],
        "BER", discovery_terms=["без хостелов", "quiet room"]) == "View prices for City Apartment"


@pytest.mark.parametrize('wish', ['do not exclude hostels', 'not avoid hostels', 'never exclude hostels',
    "don't avoid hostels", 'не избегать хостелов', 'не исключать хостелы'])
def test_negating_a_kind_exclusion_does_not_exclude_or_penalize_that_kind(wish):
    from price_worker.recipes.lodging import property_preference
    from price_worker.recipes.ostrovok import select_property
    # Allowing a hostel is not a positive claim about its rooms or policies.
    assert property_preference('Berlin Budget Hostel', [wish]) == property_preference('Berlin Budget Hostel', [])
    label = 'View prices for Berlin Budget Hostel'
    assert select_property_label([label], 'BER', discovery_terms=[wish]) == label
    path = '/hotel/germany/berlin/mid111/budget_hostel/'
    candidate = {'href': 'https://ostrovok.ru' + path, 'label': 'Berlin Budget Hostel'}
    assert select_property([candidate], 'Berlin', discovery_terms=[wish])['path'] == path


@pytest.mark.parametrize('wish', ['no hostels', 'avoid hostels', 'exclude hostels', 'без хостелов',
    'избегать хостелов', 'исключить хостелы'])
def test_explicit_kind_exclusion_still_rejects_the_only_forbidden_candidate(wish):
    from price_worker.recipes.lodging import property_preference
    assert property_preference('Berlin Budget Hostel', [wish]) is None
    assert select_property_label(['View prices for Berlin Budget Hostel'], 'BER', discovery_terms=[wish]) is None


def test_berlin_property_challenge_stops_before_any_rate_or_property_readback():
    selected = [{"iso":"2026-10-30", "selected":True, "label":"Friday, October 30, 2026, departure date."},
                {"iso":"2026-11-01", "selected":True, "label":"Sunday, November 1, 2026, return date."}]
    class Locator:
        def __init__(self, page, name):
            self.page, self.name = page, name
        def filter(self, **kwargs):
            return self
        @property
        def first(self):
            return self
        async def wait_for(self, **kwargs):
            pass
        async def count(self):
            return 0 if self.name == "Reject all" else 1
        async def click(self, **kwargs):
            if self.name == "View prices for Quiet Garden Hotel":
                self.page.opened_property = True
        async def inner_text(self):
            return "Verify you are human" if self.page.opened_property else "Hotels in Berlin"
        async def get_attribute(self, name):
            return "Number of travelers. Current selection is 2."
        async def input_value(self):
            return "Fri, Oct 30" if "Check-in" in self.name else "Sun, Nov 1"
        async def evaluate_all(self, script):
            return selected if "data-iso" in self.name else ["View prices for Quiet Garden Hotel"]
    class Page:
        def __init__(self):
            self.opened_property = False
        async def goto(self, url):
            pass
        def locator(self, selector):
            assert selector != "h1", "A challenge must stop property and rate processing"
            return Locator(self, selector)
        def get_by_role(self, role, **kwargs):
            return Locator(self, kwargs["name"])
    result = asyncio.run(google_hotels(Page(), {"destination":"Berlin", "destination_iata":"BER",
        "date_from":"2026-10-30", "date_to":"2026-11-01", "adults":2, "rooms":1, "currency":"EUR"}))
    assert result["outcome"] == "blocked" and result["diagnostic_code"] == "challenge_detected"
    assert result["actions"] <= 19
