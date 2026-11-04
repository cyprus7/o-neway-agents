"""Ostrovok interpretation and private-worker contract; fixtures are not inventory."""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from backend.app.pricing.models import PriceObservation
from price_worker import guards
from price_worker.models import QuoteRequest
from price_worker.recipes.ostrovok import (
    canonical_property_path, context_diagnostic, euro_amount, is_challenge,
    ostrovok, ostrovok_property_id, parse_rate_groups, reference_observation,
    select_property, supported_context, visible_context,
)


PATH = "/hotel/greece/athens/mid7666591/marina_hotel_athens/"
CONTEXT = {"destination":"Athens", "destination_iata":"ATH", "date_from":"2026-10-31",
           "date_to":"2026-11-01", "adults":1, "children_ages":[], "rooms":1, "currency":"EUR"}


def snapshot(**overrides):
    return {"url":"https://ostrovok.ru" + PATH + "?dates=31.10.2026-01.11.2026&guests=1",
            "dates":["Oct 31, 2026, Sat", "Nov 01, 2026, Sun"],
            "party":["For 1 night, for 1 adult"], "room_controls":["1 room for"],
            "body":"Available options", **overrides}


def groups(**overrides):
    rate = {"amounts":["€\u202f51"], "scope":"for 1 night for 1 guest",
            "extras":["+ €\u202f2 taxes and fees"],
            "terms":["Breakfast included", "The booking price is non-refundable", "Pay now"]}
    return [{"room":"Standard Single room", "aligned":True, "rates":[{**rate, **overrides}]}]


def test_exact_city_and_bounded_party_before_browser_dispatch():
    assert supported_context(CONTEXT) is None
    assert supported_context({**CONTEXT,"destination":"Istanbul","destination_iata":"IST"}) is None
    assert supported_context({**CONTEXT,"destination":"Istanbul","destination_iata":"SAW"}) is None
    assert supported_context({**CONTEXT,"destination":"Piraeus"}) == "destination_city_airport_mismatch"
    assert supported_context({**CONTEXT,"destination":"Warsaw","destination_iata":"WAW"}) == "unsupported_destination"
    for change in ({"adults":3}, {"children_ages":[8]}, {"rooms":2}):
        assert supported_context({**CONTEXT,**change}) == "single_room_adult_only_recipe"
    assert supported_context({**CONTEXT,"currency":"USD"}) == "only_eur_supported"


@pytest.mark.parametrize("destination,iata,path", [
    ("Prague","PRG","/hotel/czech_republic/prague/mid123/fixture_hotel/"),
    ("Прага","PRG","/hotel/czech_republic/prague/mid123/fixture_hotel/"),
    ("Tbilisi","TBS","/hotel/georgia/tbilisi/mid123/fixture_hotel/"),
    ("Тбилиси","TBS","/hotel/georgia/tbilisi/mid123/fixture_hotel/"),
])
def test_observed_extra_city_paths_preserve_destination_alias_and_airport_scope(destination, iata, path):
    assert supported_context({**CONTEXT,"destination":destination,"destination_iata":iata}) is None
    city = "Prague" if iata == "PRG" else "Tbilisi"
    assert canonical_property_path("https://ostrovok.ru"+path,city) == path
    assert canonical_property_path("https://ostrovok.ru"+path,"Athens") is None


@pytest.mark.parametrize("changes,diagnostic", [
    ({"dates":["Oct 31, 2027, Sun","Nov 01, 2027, Mon"]},"ostrovok_dates_not_verified"),
    ({"dates":["Oct 31, Sat","Nov 01, Sun"]},"ostrovok_dates_not_verified"),
    ({"dates":["Oct 31, 2026, Sat"]},"ostrovok_dates_not_verified"),
    ({"party":["For 1 night, for 2 adults"]},"ostrovok_party_not_verified"),
    ({"party":["For 2 nights, for 1 adult"]},"ostrovok_party_not_verified"),
    ({"room_controls":["2 rooms for"]},"ostrovok_rooms_not_verified"),
    ({"url":"https://ostrovok.ru" + PATH + "?dateless_form=yes"},"ostrovok_dateless_results"),
    ({"body":"No dates selected"},"ostrovok_dateless_results"),
    ({"url":"https://ostrovok.ru/hotel/turkey/istanbul/mid123/example/"},"ostrovok_property_context_mismatch"),
])
def test_visible_context_rejects_wrong_year_party_rooms_and_dateless(changes, diagnostic):
    assert context_diagnostic(snapshot(**changes), CONTEXT, PATH) == diagnostic


def test_visible_context_requires_both_fully_qualified_dates():
    assert context_diagnostic(snapshot(), CONTEXT, PATH) is None
    assert context_diagnostic(snapshot(party=["For 1 night, for 2 adults"]), {**CONTEXT,"adults":2}, PATH) is None


def test_property_identity_comes_from_exact_canonical_city_path():
    assert canonical_property_path("https://ostrovok.ru"+PATH+"?guests=1", "Athens") == PATH
    assert ostrovok_property_id(PATH) == ostrovok_property_id(PATH)
    assert ostrovok_property_id(PATH).startswith("ostrovok:")
    for url in ("https://ostrovok.ru.attacker.example"+PATH, "https://user:secret@ostrovok.ru"+PATH,
                "http://ostrovok.ru"+PATH, "https://ostrovok.ru:444"+PATH,
                "https://ostrovok.ru/hotel/turkey/istanbul/mid123/another/",
                "https://ostrovok.ru"+PATH+"../", "https://ostrovok.ru"+PATH.replace("athens/","athens%2f")):
        assert canonical_property_path(url, "Athens") is None


def test_fixed_entity_is_found_after_reordering_and_never_substituted():
    candidates = [{"href":"https://ostrovok.ru"+PATH,"label":"Marina Hotel"},
                  {"href":"https://ostrovok.ru/hotel/greece/athens/mid7485633/plaka_hotel/","label":"Plaka Hotel"}]
    target = ostrovok_property_id(PATH)
    assert select_property(candidates, "Athens", target)["path"] == PATH
    assert select_property(list(reversed(candidates)), "Athens", target)["path"] == PATH
    assert select_property(candidates[1:], "Athens", target) is None
    assert select_property(candidates, "Istanbul", target) is None
    assert select_property(candidates, "Athens", "google-hotel:"+"a"*24) is None


def test_empty_photo_anchor_does_not_hide_later_named_property_link():
    candidates = [{"href":"https://ostrovok.ru"+PATH,"label":""},
                  {"href":"https://ostrovok.ru"+PATH,"label":"Marina Hotel"}]
    assert select_property(candidates, "Athens")["label"] == "Marina Hotel"


def test_actual_date_wrappers_and_popup_transition_do_not_corrupt_context_readback():
    # Observed worker DOM 25 Oct 2026: Dates_date matched wrappers/labels too;
    # the room label was absent until the popup completed its visible transition.
    class Locator:
        def __init__(self, labels, transition=False):
            self.labels, self.transition, self.waited = labels, transition, False
        def filter(self, **kwargs):
            return self
        @property
        def first(self):
            return self
        async def wait_for(self, **kwargs):
            self.waited = True
        async def all_inner_texts(self):
            return [] if self.transition and not self.waited else self.labels
    class Page:
        def __init__(self):
            self.rooms = Locator(["1 room for"], transition=True)
        def locator(self, selector):
            if selector == 'button[class*="Dates_date__"]':
                return Locator(["Oct 31, 2026, Sat", "Nov 01, 2026, Sun"])
            return Locator(["Check-in\nOct 31, 2026, Sat\nFrom 15:00\nCheck-out\nNov 01, 2026, Sun", "Check-in", "Oct 31, 2026, Sat", "Check-out", "Nov 01, 2026, Sun"])
        def get_by_text(self, text, **kwargs):
            return self.rooms if text == "1 room for" else Locator(["For 1 night, for 1 adult"])
        def get_by_role(self, *args, **kwargs):
            return kwargs["name"]
    clicks = []
    async def click(locator):
        clicks.append(locator)
    page = Page()
    controls = asyncio.run(visible_context(page,click))
    assert context_diagnostic({**snapshot(),**controls}, CONTEXT, PATH) is None
    assert page.rooms.waited and clicks == ["Change","popup-close"]


@pytest.mark.parametrize("value,expected", [("€\u202f51","51"),("€ 1,234.50","1234.50"),
    ("€51.20","51.20"),("€0",None),("€1,2",None),("€NaN",None),("RUB51",None),
    ("from €51",None),("€51 €60",None),("€1000000",None)])
def test_only_one_current_explicit_euro_amount_is_numeric(value, expected):
    assert euro_amount(value) == expected


def test_rate_amount_scope_terms_and_fees_stay_in_the_same_column():
    first = groups()[0]
    first["rates"].append({"amounts":["€ 74"],"scope":"for 1 night for 1 guest",
        "extras":["+ € 3 taxes and fees"],"terms":["Free cancellation", "Breakfast included"]})
    rate = parse_rate_groups([first], CONTEXT)
    assert rate["amount"] == "51"
    assert rate["extras"] == ["+ €\u202f2 taxes and fees"]
    assert "Free cancellation" not in rate["terms"]
    assert "non-refundable" in " ".join(rate["terms"])
    assert parse_rate_groups([{**first,"aligned":False}], CONTEXT) is None


@pytest.mark.parametrize("changes", [
    {"amounts":[]}, {"amounts":["€999","€51"]}, {"amounts":["₽4602"]},
    {"scope":"for 1 night for 2 guests"}, {"scope":"for 7 nights for 1 guest"},
    {"scope":"from €51 per night"},
])
def test_old_ambiguous_wrong_currency_or_party_prices_are_not_success(changes):
    assert parse_rate_groups(groups(**changes), CONTEXT) is None


def test_reference_preserves_full_stay_and_extra_charges_without_sum_or_normalization():
    rate = parse_rate_groups(groups(), CONTEXT)
    observed = PriceObservation.model_validate(reference_observation(CONTEXT, PATH, "Marina Hotel", rate))
    assert observed.value.min == "51" and observed.value.currency == "EUR"
    assert observed.outcome == "reference" and observed.normalized_group is None
    assert observed.scope.entity_id == ostrovok_property_id(PATH)
    assert observed.scope.provider_entity_id == "Marina Hotel"
    assert observed.scope.adults == 1 and observed.scope.rooms == 1
    assert observed.scope.context_verified is False
    assert observed.scope.unit == "group_stay" and observed.scope.taxes == "excluded"
    assert "+ €\u202f2 taxes and fees" in observed.excluded_charges
    assert observed.unknown_charges and not observed.component_complete and not observed.availability_verified
    assert "Standard Single" in observed.unit_label and "non-refundable" in observed.reason
    assert "dates=31.10.2026-01.11.2026" in observed.source_url


def test_live_positive_tax_label_is_included_and_never_misreported_as_an_extra():
    rate = parse_rate_groups(groups(extras=["All taxes included"]), CONTEXT)
    observed = PriceObservation.model_validate(reference_observation(CONTEXT, PATH, "Fixture Hotel", rate))
    assert observed.scope.taxes == "included"
    assert observed.included_charges == ("All taxes included",)
    assert observed.excluded_charges == ()
    assert observed.unknown_charges and observed.normalized_group is None
    for text in ("Not all taxes included", "Some charges may be included", "Taxes are not included"):
        changed = PriceObservation.model_validate(reference_observation(CONTEXT, PATH, "Fixture Hotel", {**rate,"extras":[text]}))
        assert changed.scope.taxes == "unknown" and changed.included_charges == ()
        assert text in changed.unknown_charges


@pytest.mark.parametrize("text", ["Verify you are human", "Подтвердите, что вы человек", "captcha", "Unusual traffic"])
def test_protective_challenges_are_explicit_stops(text):
    assert is_challenge(text)


def test_unsupported_or_mismatched_target_never_navigates():
    class NoNavigation:
        def __getattr__(self, name):
            raise AssertionError("Invalid context must fail before page access")
    assert asyncio.run(ostrovok(NoNavigation(), {**CONTEXT,"rooms":2}))["outcome"] == "unsupported_party"
    assert asyncio.run(ostrovok(NoNavigation(), {**CONTEXT,"target_entity_id":"google-hotel:"+"a"*24}))["outcome"] == "context_mismatch"


def request(provider="ostrovok", **changes):
    return {"request_id":"fixture", "provider_id":provider, "request_context":CONTEXT,
            "request_context_hash":"a"*64,
            "deadline_at":(datetime.now(timezone.utc)+timedelta(seconds=20)).isoformat(), **changes}


def test_worker_schema_allows_both_providers_and_rejects_provider_target_mismatch():
    assert QuoteRequest.model_validate(request()).provider_id == "ostrovok"
    for provider, target in (("ostrovok", "ostrovok:"+"a"*24), ("google_hotels", "google-hotel:"+"b"*24)):
        assert QuoteRequest.model_validate(request(provider,request_context={**CONTEXT,"target_entity_id":target}))
    for provider, target in (("ostrovok", "google-hotel:"+"a"*24), ("google_hotels", "ostrovok:"+"a"*24),
                             ("ostrovok", "https://127.0.0.1/admin")):
        with pytest.raises(ValidationError):
            QuoteRequest.model_validate(request(provider,request_context={**CONTEXT,"target_entity_id":target}))
    with pytest.raises(ValidationError):
        QuoteRequest.model_validate(request(request_context={**CONTEXT,"url":"http://127.0.0.1"}))


def test_exact_provider_domain_allowlist_keeps_private_and_unobserved_destinations_blocked():
    for host in ("ostrovok.ru","hf.fastrovok.net","mpf.fastrovok.net","www.google.com"):
        assert guards.allowed_url("https://"+host+"/public")
    for url in ("https://ostrovok.ru.attacker.example/", "https://arbitrary.fastrovok.net/",
                "https://api.flocktory.com/", "https://ostrovok.ru:444/", "http://ostrovok.ru/"):
        assert not guards.allowed_url(url)


def test_each_recipe_has_its_own_hosts_and_reservation_navigation_is_denied():
    assert guards.provider_url_allowed('https://ostrovok.ru'+PATH, 'ostrovok')
    assert not guards.provider_url_allowed('https://www.google.com/travel/', 'ostrovok')
    assert not guards.provider_url_allowed('https://ostrovok.ru'+PATH, 'google_hotels')
    assert guards.provider_url_allowed('https://www.google.com/travel/', 'google_hotels')
    assert not guards.provider_url_allowed('https://ostrovok.ru/orders/reserve/example', 'ostrovok')


def test_challenge_stops_recipe_before_currency_and_search_actions():
    class Body:
        async def inner_text(self):
            return 'Verify you are human'
    class Page:
        def __init__(self):
            self.navigations = []
        async def goto(self, url):
            self.navigations.append(url)
        def locator(self, selector):
            assert selector == 'body'
            return Body()
    page = Page()
    result = asyncio.run(ostrovok(page, CONTEXT))
    assert result['outcome'] == 'blocked' and 'observation' not in result
    assert page.navigations == ['https://ostrovok.ru/']


BERLIN_PATH = "/hotel/germany/berlin/mid8876313/hotel_indigo_berlin_east_side_gallery/"
BERLIN_CONTEXT = {**CONTEXT, "destination":"Berlin", "destination_iata":"BER", "date_from":"2026-10-30", "adults":2}


@pytest.mark.parametrize("destination", ["Berlin", "Берлин", "  берлин  "])
def test_berlin_official_path_is_supported_but_neighbor_and_wrong_city_are_not(destination):
    assert supported_context({**BERLIN_CONTEXT, "destination":destination}) is None
    assert canonical_property_path("https://ostrovok.ru" + BERLIN_PATH, "Berlin") == BERLIN_PATH
    assert canonical_property_path("https://ostrovok.ru" + BERLIN_PATH, "Athens") is None
    assert supported_context({**BERLIN_CONTEXT,"destination":"Potsdam"}) == "destination_city_airport_mismatch"


def test_worker_optional_fields_keep_legacy_and_enforce_exact_source_city_hash():
    assert QuoteRequest.model_validate(request()).request_context.discovery_terms == []
    context = {**BERLIN_CONTEXT, "target_entity_id":ostrovok_property_id(BERLIN_PATH),
        "target_property_path":BERLIN_PATH, "discovery_terms":["quiet room", "no hostels"]}
    validated = QuoteRequest.model_validate(request(request_context=context)).request_context
    assert validated.target_property_path == BERLIN_PATH
    assert validated.discovery_terms == ["quiet room", "no hostels"]
    for changes in ({"target_entity_id":None}, {"target_entity_id":"ostrovok:"+"a"*24}, {"destination":"Athens", "destination_iata":"ATH"}):
        with pytest.raises(ValidationError):
            QuoteRequest.model_validate(request(request_context={**context, **changes}))
    with pytest.raises(ValidationError):
        QuoteRequest.model_validate(request("google_hotels", request_context=context))


@pytest.mark.parametrize("path", ["https://ostrovok.ru"+BERLIN_PATH, "//ostrovok.ru"+BERLIN_PATH,
    BERLIN_PATH+"?guests=2", BERLIN_PATH+"#fragment", BERLIN_PATH+"../", BERLIN_PATH.replace("berlin/", "berlin%2f"),
    "/hotel/greece/athens/mid7666591/marina_hotel_athens/", "/hotel/germany/berlin/", "a"*257])
def test_bad_private_target_is_rejected_before_any_page_access(path):
    class NoNavigation:
        def __getattr__(self, name):
            raise AssertionError("Invalid target must fail before page access")
    context = {**BERLIN_CONTEXT, "target_entity_id":ostrovok_property_id(BERLIN_PATH), "target_property_path":path}
    with pytest.raises(ValidationError):
        QuoteRequest.model_validate(request(request_context=context))
    assert asyncio.run(ostrovok(NoNavigation(), context))["outcome"] == "context_mismatch"


@pytest.mark.parametrize("terms", [["term"]*5, ["x"*81], ["x"*80, "y"*80, "z"*41], [" "],
    ["quiet\nroom"], ["https://example.com"], ["www.example.com"], ["transport:train"], ["transport_requirement:no flight"]])
def test_private_discovery_terms_are_bounded_lodging_data(terms):
    with pytest.raises(ValidationError):
        QuoteRequest.model_validate(request(request_context={**BERLIN_CONTEXT, "discovery_terms":terms}))
    class NoNavigation:
        def __getattr__(self, name):
            raise AssertionError("Invalid terms must fail before page access")
    assert asyncio.run(ostrovok(NoNavigation(), {**BERLIN_CONTEXT, "discovery_terms":terms}))["diagnostic_code"] == "invalid_discovery_terms"


def test_private_wishes_are_preserved_and_only_guide_candidate_navigation():
    wishes = ["quiet private room", "no hostels", "pets allowed"]
    context = QuoteRequest.model_validate(request(request_context={**BERLIN_CONTEXT, "discovery_terms":wishes})).request_context
    assert context.discovery_terms == wishes
    candidates = [{"href":"https://ostrovok.ru"+BERLIN_PATH, "label":"City Hotel"},
        {"href":"https://ostrovok.ru/hotel/germany/berlin/mid123/quiet_garden/", "label":"Quiet Garden Hotel"},
        {"href":"https://ostrovok.ru/hotel/germany/berlin/mid456/quiet_hostel/", "label":"Quiet Hostel"}]
    assert select_property(candidates, "Berlin", discovery_terms=wishes)["label"] == "Quiet Garden Hotel"
    assert select_property(candidates[2:], "Berlin", discovery_terms=["no hostels"]) is None
    assert select_property(candidates, "Berlin", ostrovok_property_id(BERLIN_PATH), discovery_terms=wishes)["path"] == BERLIN_PATH


def test_exact_target_navigation_skips_city_index_and_stops_on_property_challenge():
    class Control:
        @property
        def first(self):
            return self
        async def count(self):
            return 1
        async def click(self):
            pass
    class Body:
        def __init__(self, page):
            self.page = page
        async def inner_text(self):
            return "Verify you are human" if len(self.page.navigations) > 1 else "Choose currency"
    class Page:
        def __init__(self):
            self.navigations = []
        async def goto(self, url):
            self.navigations.append(url)
        def locator(self, selector):
            assert selector == "body", "Exact targets must not scan the city index"
            return Body(self)
        def get_by_test_id(self, name):
            assert name == "currency-control"
            return Control()
        def get_by_role(self, role, **kwargs):
            assert role == "button" and kwargs["name"] == "EUR Euro , €"
            return Control()
    page = Page()
    result = asyncio.run(ostrovok(page, {**BERLIN_CONTEXT, "target_entity_id":ostrovok_property_id(BERLIN_PATH),
        "target_property_path":BERLIN_PATH, "discovery_terms":["quiet room"]}))
    assert result["outcome"] == "blocked" and result["diagnostic_code"] == "challenge_detected"
    assert page.navigations[0] == "https://ostrovok.ru/"
    assert page.navigations[1].startswith("https://ostrovok.ru"+BERLIN_PATH+"?dates=30.10.2026-01.11.2026&guests=2")
    assert len(page.navigations) == 2 and result["actions"] <= 12


def test_berlin_readback_still_requires_exact_year_party_and_one_room():
    controls = {"url":"https://ostrovok.ru"+BERLIN_PATH+"?dates=30.10.2026-01.11.2026&guests=2",
        "dates":["Oct 30, 2026, Fri", "Nov 01, 2026, Sun"], "party":["For 2 nights, for 2 adults"],
        "room_controls":["1 room for"], "body":"Available options"}
    assert context_diagnostic(controls, BERLIN_CONTEXT, BERLIN_PATH) is None
    for changes, diagnostic in (({"dates":["Oct 30, 2027, Sat", "Nov 01, 2027, Mon"]}, "ostrovok_dates_not_verified"),
            ({"dates":["Oct 30, Fri", "Nov 01, Sun"]}, "ostrovok_dates_not_verified"),
            ({"party":["For 2 nights, for 1 adult"]}, "ostrovok_party_not_verified"),
            ({"room_controls":["2 rooms for"]}, "ostrovok_rooms_not_verified"),
            ({"body":"No dates selected"}, "ostrovok_dateless_results")):
        assert context_diagnostic({**controls, **changes}, BERLIN_CONTEXT, BERLIN_PATH) == diagnostic


def test_berlin_index_challenge_is_detected_before_waiting_for_property_links():
    class Control:
        @property
        def first(self):
            return self
        async def count(self):
            return 1
        async def click(self):
            pass
    class Body:
        def __init__(self, page):
            self.page = page
        async def inner_text(self):
            return "Verify you are human" if len(self.page.navigations) > 1 else "Choose currency"
    class Page:
        def __init__(self):
            self.navigations = []
        async def goto(self, url):
            self.navigations.append(url)
        def locator(self, selector):
            assert selector == "body", "Challenge must stop before waiting for property links"
            return Body(self)
        def get_by_test_id(self, name):
            return Control()
        def get_by_role(self, role, **kwargs):
            return Control()
    page = Page()
    result = asyncio.run(ostrovok(page, BERLIN_CONTEXT))
    assert result["outcome"] == "blocked" and result["diagnostic_code"] == "challenge_detected"
    assert page.navigations == ["https://ostrovok.ru/", "https://ostrovok.ru/hotel/germany/berlin/"]
