"""Stateful accessible-page fixtures for the bounded research recipe."""
import asyncio
from datetime import date
from types import SimpleNamespace

import pytest

from flight_search import recipe
from flight_search.guards import allowed_url, public_destination
from flight_search.models import FlightConstraints
from flight_search.domain import SourceError, make_route, parse_card, read_scope
from test_flight_domain import command, rows, route, snapshot


class Locator:
    def __init__(self, page, kind, name=None):
        self.page, self.kind, self.name = page, kind, name

    async def inner_text(self, **_):
        return self.page.body

    async def count(self):
        return 0 if self.kind == "button" else len(self.page.current_rows())

    async def evaluate_all(self, _):
        return self.page.current_rows(consume=True)

    async def press(self, key):
        assert key == "Enter" and self.name == rows()[0]["label"]
        self.page.activations.append(self.name)
        self.page.stage = "return"


class Page:
    def __init__(self, *, return_scope=None, return_rows=None, stale=False, body=None):
        self.url = "https://www.google.com/travel/flights/search?hl=en&curr=EUR"
        self.stage = "outbound"
        self.return_scope = return_scope or snapshot()
        self.return_rows = return_rows if return_rows is not None else [rows()[1]]
        self.stale = stale
        self.body = body or snapshot()["body"]
        self.activations, self.navigations = [], []

    async def goto(self, url, **_):
        self.navigations.append(url)
        self.stage = "outbound"

    def locator(self, selector):
        assert selector == "body"
        return Locator(self, "body")

    def get_by_role(self, role, *, name, **_):
        return Locator(self, role, name)

    def current_rows(self, *, consume=False):
        if self.stage == "outbound" or self.stale:
            if consume and self.stage == "return":
                self.stale = False
            return [rows()[0]]
        return self.return_rows


@pytest.fixture
def source_scope(monkeypatch):
    async def actual_snapshot(page, *, initial, **_):
        assert initial  # The return view must prove full ISO dates as well.
        return snapshot() if page.stage == "outbound" else page.return_scope
    monkeypatch.setattr(recipe, "_scope_snapshot", actual_snapshot)


def test_paired_return_card_is_reference_without_final_return_booking_activation(source_scope):
    page = Page(stale=True)
    result = asyncio.run(recipe.google_flights(page, command()))
    assert result["outcome"] == "reference" and result["candidate_count"] == 1
    assert result["route"].price.amount == "1622"
    assert len(page.navigations) == 1 and len(page.activations) == 1
    assert not result["route"].availability_verified


@pytest.mark.parametrize("changed,code", [
    ({"track_label":snapshot()["track_label"].replace("2026", "2037")},"flight_date_mismatch"),
    ({"track_label":""},"flight_scope_not_verified"),
    ({"origin_input":"Paphos"},"flight_route_mismatch"),
    ({"passengers":"1 passenger, change number of passengers."},"flight_party_mismatch"),
    ({"passengers":"1 passenger"},"flight_party_mismatch"),
    ({"currency":"Currency USD"},"flight_currency_mismatch"),
    ({"cabin":"Business"},"flight_cabin_mismatch"),
])
def test_return_navigation_rechecks_full_scope_including_same_weekday_wrong_year(source_scope, changed, code):
    # Oct30 and Nov1 in2037 have the same weekdays as2026; month/day/card
    # weekday checks alone would therefore incorrectly accept this source.
    page = Page(return_scope=snapshot(**changed))
    result = asyncio.run(recipe.google_flights(page, command()))
    assert result == {"outcome":"context_mismatch", "diagnostic_code":code}
    assert len(page.activations) == 1 and "route" not in result


def test_fixed_refresh_finds_exact_identity_or_returns_missing_without_substitution(source_scope):
    value = route()
    found = asyncio.run(recipe.google_flights(Page(), command(target_itinerary_id=value.itinerary_id)))
    missing = asyncio.run(recipe.google_flights(Page(), command(target_itinerary_id="a"*64)))
    assert found["route"].itinerary_id == value.itinerary_id
    assert missing == {"outcome":"no_offers", "diagnostic_code":"flight_target_not_found", "candidate_count":1}


def test_calendar_marked_full_year_dates_are_read_after_return_navigation(source_scope):
    selected = ["Friday, October 30, 2026, departure date.", "Sunday, November 1, 2026, return date."]
    page = Page(return_scope=snapshot(track_label="", selected_date_labels=selected))
    result = asyncio.run(recipe.google_flights(page, command()))
    assert result["outcome"] == "reference" and len(page.activations) == 1


def test_observed_short_return_passenger_label_still_requires_matching_independent_adult_header(source_scope):
    page = Page(return_scope=snapshot(passengers="2 passengers"))
    result = asyncio.run(recipe.google_flights(page,command()))
    assert result["outcome"] == "reference" and result["route"].adults == 2
    page = Page(return_scope=snapshot(passengers="2 passengers",body="Prices include required taxes + fees for 1 adult."))
    assert asyncio.run(recipe.google_flights(page,command())) == {
        "outcome":"context_mismatch","diagnostic_code":"flight_party_mismatch"}


@pytest.mark.parametrize("fixed",[False,True])
def test_first_compatible_route_can_be_found_in_third_bounded_outbound_branch_with_calendar_actions(monkeypatch,fixed):
    candidates = [{**rows()[0], "label":rows()[0]["label"].replace("3:30 AM", f"{hour}:30 AM")} for hour in (3,4,5)]
    target = make_route(read_scope(snapshot(),command()),parse_card(candidates[2],"LCA","BER",date(2026,10,30)),
                        parse_card(rows()[1],"BER","LCA",date(2026,11,1)),"https://www.google.com/travel/flights/search")
    class BranchLocator(Locator):
        async def press(self,key):
            assert key == "Enter" and self.name in [item["label"] for item in candidates]
            self.page.activations.append(self.name)
            self.page.selected = [item["label"] for item in candidates].index(self.name)
            self.page.stage = "return"
    class BranchPage(Page):
        def get_by_role(self,role,*,name,**_):
            return BranchLocator(self,role,name)
        def current_rows(self,**_):
            if self.stage == "outbound":
                return candidates
            return [rows()[1] if fixed or self.selected == 2 else {**rows()[1],"text":rows()[1]["text"]+" Business Class"}]
    calendars = []
    async def scope(page,*,initial,calendar_action=None):
        if page.stage == "return":
            assert calendar_action is not None
            calendar_action()
            calendar_action()
            calendars.append(1)
        return snapshot()
    monkeypatch.setattr(recipe,"_scope_snapshot",scope)
    page = BranchPage()
    result = asyncio.run(recipe.google_flights(page,command(target_itinerary_id=target.itinerary_id if fixed else None)))
    assert result["route"].itinerary_id == target.itinerary_id
    assert len(page.activations) == len(page.navigations) == len(calendars) == 3
    assert result["candidate_count"] == (3 if fixed else 1)


def test_calendar_scope_reads_only_visible_selected_labels_and_closes_without_editing():
    class Element:
        def __init__(self, page, kind, name=None):
            self.page, self.kind, self.name = page, kind, name
        async def evaluate_all(self, _):
            if self.kind == "controls":
                return ["Round trip", "Economy"]
            assert self.kind == "selected"
            return ["Friday, October 30, 2026, departure date.", "Sunday, November 1, 2026, return date."]
        async def inner_text(self, **_):
            return snapshot()["body"]
        async def get_attribute(self, _):
            return "Currency EUR" if "Currency" in str(self.name) else snapshot()["passengers"]
        async def input_value(self):
            return {"Departure":"Fri, Oct 30", "Return":"Sun, Nov 1", "Where from?":"Larnaca", "Where to?":"Berlin"}[self.name]
        async def all(self):
            return []
        async def count(self):
            return 1 if self.page.opened else 0
        async def is_visible(self):
            return self.page.opened
        async def click(self):
            assert self.name == "Departure"
            self.page.actions.append("open")
            self.page.opened = True
        async def press(self, key):
            assert self.kind == "dialog" and key == "Escape"
            self.page.actions.append("close")
            self.page.opened = False
        def locator(self, selector):
            assert self.kind == "dialog" and selector == "[aria-label]"
            return Element(self.page,"selected")
    class CalendarPage:
        url = "https://www.google.com/travel/flights/search"
        opened = False
        def __init__(self):
            self.actions = []
        def locator(self, selector):
            return Element(self,"controls" if selector == '[role="combobox"]' else "body")
        def get_by_role(self, role, *, name=None, **_):
            return Element(self, role, name)
    page = CalendarPage()
    counted = []
    value = asyncio.run(recipe._scope_snapshot(page,initial=True,calendar_action=lambda:counted.append(1)))
    assert len(value["selected_date_labels"]) == 2 and "track_label" not in value
    assert page.actions == ["open","close"] and len(counted) == 2 and not page.opened
    assert value["departure"] == "Fri, Oct 30" and value["return"] == "Sun, Nov 1"


def test_hard_direct_is_eligibility_not_preference_and_unsupported_party_never_navigates(source_scope):
    page = Page()
    result = asyncio.run(recipe.google_flights(page, command(constraints=FlightConstraints(direct_required=True))))
    assert result["outcome"] == "no_offers" and not page.activations
    context = command().request_context.model_copy(update={"children_ages":(8,)})
    page = Page()
    result = asyncio.run(recipe.google_flights(page, command(request_context=context)))
    assert result["outcome"] == "unsupported_party" and not page.navigations


@pytest.mark.parametrize("body,outcome", [("Verify you are human", "blocked"), ("Sign in to continue", "requires_login"),
    ("Oops, something went wrong", "unavailable")])
def test_source_challenge_and_errors_stop_without_selection(source_scope, body, outcome):
    page = Page(body=body)
    result = asyncio.run(recipe.google_flights(page, command()))
    assert result["outcome"] == outcome and not page.activations


@pytest.mark.parametrize("url,navigation,accepted", [
    ("https://www.google.com/travel/flights?q=route",True,True),
    ("https://www.google.com/travel/flights/search?tfs=scope",True,True),
    ("https://consent.google.com/m",True,True),
    ("https://www.gstatic.com/assets.js",False,True),
    ("https://www.gstatic.com/assets.js",True,False),
    ("https://www.google.com/travel/flights/booking",True,False),
    ("https://www.google.com/url?url=https://vendor.example",False,False),
    ("https://accounts.google.com/ServiceLogin",True,False),
    ("https://vendor.example/",False,False),
    ("https://user:pass@www.google.com/travel/flights",True,False),
    ("http://www.google.com/travel/flights",True,False),
    ("wss://www.google.com/socket",False,False),
    ("https://www.google.com:444/travel/flights",True,False),
])
def test_egress_has_exact_public_hosts_and_read_only_navigation_paths(url, navigation, accepted):
    assert allowed_url(url, navigation=navigation) is accepted


@pytest.mark.parametrize("method,accepted", [("POST",True),("GET",False),("PUT",False),("DELETE",False),("post",False)])
def test_only_observed_exact_consent_reject_form_post_is_permitted(method,accepted):
    assert allowed_url("https://consent.google.com/save",navigation=True,method=method) is accepted
    assert allowed_url("https://consent.google.com/save",method=method) is accepted
    assert not allowed_url("https://www.google.com/save",navigation=True,method=method)
    assert not allowed_url("https://consent.google.com/save/other",navigation=True,method=method)
    assert not allowed_url("http://consent.google.com/save",navigation=True,method=method)
    assert not allowed_url("https://consent.google.com:444/save",navigation=True,method=method)
    assert not allowed_url("https://accounts.google.com/save",navigation=True,method=method)
    assert not allowed_url("https://www.google.com/travel/flights/booking",navigation=True,method=method)


def test_consent_post_still_requires_public_dns(monkeypatch):
    monkeypatch.setattr("flight_search.guards.socket.getaddrinfo", lambda *a, **k: [(None,None,None,None,("127.0.0.1",443))])
    assert not asyncio.run(public_destination("https://consent.google.com/save",method="POST"))


def test_dns_rebinding_private_address_is_rejected(monkeypatch):
    monkeypatch.setattr("flight_search.guards.socket.getaddrinfo", lambda *a, **k: [(None,None,None,None,("127.0.0.1",443))])
    assert not asyncio.run(public_destination("https://www.google.com/travel/flights"))


@pytest.mark.parametrize("stage,outcome,code", [("navigation","unavailable","flight_source_unavailable"),
    ("scope","context_mismatch","flight_scope_not_verified")])
def test_unexpected_source_failure_retains_finite_stage_without_exception_text(monkeypatch,source_scope,stage,outcome,code):
    page = Page()
    async def fail(*_,**__):
        raise RuntimeError("https://private.invalid/secret?key=do-not-export")
    if stage == "navigation":
        page.goto = fail
    else:
        monkeypatch.setattr(recipe,"_scope_snapshot",fail)
    attributes = []
    monkeypatch.setattr(recipe,"set_attributes",lambda **values:attributes.append(values))
    result = asyncio.run(recipe.google_flights(page,command()))
    assert result == {"outcome":outcome,"diagnostic_code":code}
    assert {"operation":"flight_"+stage} in attributes and {"error_type":"RuntimeError"} in attributes
    assert "secret" not in str(result)+str(attributes)


def test_actual_playwright_timeout_class_is_classified_as_timeout_without_message(source_scope):
    from playwright.async_api import TimeoutError as BrowserTimeout
    page = Page()
    async def fail(*_,**__):
        raise BrowserTimeout("sensitive browser URL, payload and detail")
    page.goto = fail
    assert asyncio.run(recipe.google_flights(page,command())) == {"outcome":"timeout","diagnostic_code":"flight_timeout"}


def test_poll_exhaustion_is_source_unavailable_not_a_claim_of_no_offers(monkeypatch):
    page = Page()
    page.current_rows = lambda **_: []
    clock = iter((0,9))
    monkeypatch.setattr(recipe,"time",SimpleNamespace(monotonic=lambda:next(clock)))
    with pytest.raises(SourceError) as error:
        asyncio.run(recipe._cards(page))
    assert (error.value.code,error.value.outcome) == ("flight_source_unavailable","unavailable")
