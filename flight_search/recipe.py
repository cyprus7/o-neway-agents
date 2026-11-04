"""Bounded visible Google Flights research; never a seller or booking recipe."""
import asyncio
import re
import time
from one_request_telemetry import set_attributes, span, traced

from .domain import (MAX_CARDS, MAX_OUTBOUND_BRANCHES, SourceError, eligible_leg, make_route, parse_card,
                     query_url, read_scope, scope_diagnostic, select_route, stop_diagnostic)


FLIGHT_LABEL = re.compile(r"^From .* round trip total\..*Select flight$", re.S)
# Three outbound branches, their visible calendars and one optional consent
# dismissal. This cap does not reset the service's whole-operation deadline.
MAX_ACTIONS = 13


async def _body(page):
    return (await page.locator("body").inner_text(timeout=2000))[:18000]


async def _check(page):
    body = await _body(page)
    diagnostic = stop_diagnostic(body, page.url)
    if diagnostic:
        raise SourceError(*diagnostic)
    return body


@traced("flight.scope")
async def _scope_snapshot(page, *, initial, calendar_action=None):
    controls = await page.locator('[role="combobox"]').evaluate_all(
        "els=>els.filter(el=>el.getClientRects().length).map(el=>el.innerText.trim()).filter(Boolean).slice(0,4)")
    snapshot = {"body":await _check(page), "trip":controls[0] if controls else "",
        "cabin":controls[1] if len(controls)>1 else "",
        "currency":await page.get_by_role("button", name=re.compile(r"^Currency")).get_attribute("aria-label"),
        "passengers":await page.get_by_role("button", name=re.compile(r"^\d+ passengers?(?:, change number of passengers\.)?$")).get_attribute("aria-label"),
        "departure":await page.get_by_role("textbox", name="Departure", exact=True).input_value(),
        "return":await page.get_by_role("textbox", name="Return", exact=True).input_value()}
    if initial:
        tracks = await page.get_by_role("switch", name=re.compile(r"^Track prices from .* departing \d{4}-\d{2}-\d{2} and returning")).all()
        snapshot.update(origin_input=await page.get_by_role("combobox", name="Where from?", exact=True).input_value(),
            destination_input=await page.get_by_role("combobox", name="Where to?", exact=True).input_value())
        if len(tracks) == 1:
            snapshot["track_label"] = await tracks[0].get_attribute("aria-label")
        elif not tracks and calendar_action is not None:
            with span("flight.calendar"):
                dialog = page.get_by_role("dialog")
                calendar_action()
                await page.get_by_role("textbox", name="Departure", exact=True).click()
                try:
                    if await dialog.count() != 1 or not await dialog.is_visible():
                        raise SourceError("flight_scope_not_verified")
                    snapshot["selected_date_labels"] = await dialog.locator("[aria-label]").evaluate_all(r"""els=>els
                        .filter(el=>el.getClientRects().length && /(?:departure|return) date\.$/.test(el.getAttribute('aria-label')||''))
                        .slice(0,4).map(el=>el.getAttribute('aria-label'))""")
                finally:
                    calendar_action()
                    await dialog.press("Escape")
        else:
            raise SourceError("flight_scope_not_verified")
    return snapshot


@traced("flight.cards")
async def _cards(page, *, expected_pair=None):
    end = time.monotonic()+8
    links = page.get_by_role("link", name=FLIGHT_LABEL)
    while True:
        body = await _check(page)
        if any(term in body.casefold() for term in ("no flights found", "no results found", "no matching flights")):
            return []
        if await links.count():
            rows = await links.evaluate_all("""els=>els.filter(el=>el.getClientRects().length).slice(0,12).map(el=>({
                label:(el.getAttribute('aria-label')||'').slice(0,1600),text:(el.parentElement?.innerText||'').slice(0,2500)}))""")
            # SPA navigation can briefly retain the outbound cards. Wait for
            # actual reverse-direction visible rows before reading return data.
            if expected_pair is None or any(set(re.findall(r"\b([A-Z]{3})\s*[–—-]\s*([A-Z]{3})\b", row["text"])) == {expected_pair}
                                            for row in rows):
                return rows
        if time.monotonic() >= end:
            raise SourceError("flight_source_unavailable", "unavailable")
        await asyncio.sleep(.1)


async def google_flights(page, request):
    diagnostic = scope_diagnostic(request)
    if diagnostic:
        return {"outcome":diagnostic[1], "diagnostic_code":diagnostic[0]}
    actions = 0
    stage = "flight_navigation"
    def at_stage(value):
        nonlocal stage
        stage = value
        set_attributes(operation=value)
    def calendar_action():
        nonlocal actions
        actions += 1
        if actions > MAX_ACTIONS:
            raise SourceError("flight_timeout", "timeout")
    async def navigate():
        nonlocal actions
        actions += 1
        if actions > MAX_ACTIONS:
            raise SourceError("flight_timeout", "timeout")
        at_stage("flight_navigation")
        with span("flight.navigation"):
            await page.goto(query_url(request), wait_until="domcontentloaded")
            await _check(page)
    async def activate(label):
        nonlocal actions
        at_stage("flight_selection")
        await _check(page)
        actions += 1
        if actions > MAX_ACTIONS:
            raise SourceError("flight_timeout", "timeout")
        # Normal keyboard activation of an observed accessible source link.
        with span("flight.select"):
            await page.get_by_role("link", name=label, exact=True).press("Enter")
    try:
        await navigate()
        reject = page.get_by_role("button", name="Reject all", exact=True)
        if await reject.count() and await reject.is_visible():
            calendar_action()
            await reject.click()
        at_stage("flight_cards")
        rows = await _cards(page)
        at_stage("flight_scope")
        scope = read_scope(await _scope_snapshot(page, initial=True), request)
        outbound = [card for row in rows[:MAX_CARDS] if (card := parse_card(row, scope.origin, scope.destination, scope.date_from))
                    is not None and eligible_leg(card.leg, request.constraints)
                    and (request.constraints.arrival_date is None
                         or card.leg.arrival_date == request.constraints.arrival_date)]
        if request.constraints.prefer_direct:
            outbound.sort(key=lambda card: (card.leg.stops, card.leg.duration_minutes))
        branches = outbound[:MAX_OUTBOUND_BRANCHES]
        count = 0
        for index, candidate in enumerate(branches):
            if index:
                await navigate()
                at_stage("flight_cards")
                current_rows = await _cards(page)
                at_stage("flight_scope")
                read_scope(await _scope_snapshot(page, initial=True), request)
                matching = [card for row in current_rows if (card := parse_card(row, scope.origin, scope.destination, scope.date_from))
                            is not None and card.leg == candidate.leg]
                if not matching:
                    continue
                candidate = matching[0]
            await activate(candidate.label)
            at_stage("flight_return_cards")
            rows = await _cards(page, expected_pair=(scope.destination, scope.origin))
            # A fresh year/route readback is required after source navigation;
            # month/day controls alone cannot establish the travel year.
            at_stage("flight_scope")
            read_scope(await _scope_snapshot(page, initial=True, calendar_action=calendar_action), request)
            routes = []
            for row in rows[:MAX_CARDS]:
                inbound = parse_card(row, scope.destination, scope.origin, scope.date_to)
                if inbound is not None and eligible_leg(inbound.leg, request.constraints):
                    routes.append(make_route(scope, candidate, inbound, page.url))
            count += len(routes)
            selected = select_route(routes, request)
            if selected is not None:
                return {"outcome":"reference", "diagnostic_code":"flight_reference", "route":selected,
                        "candidate_count":count}
        return {"outcome":"no_offers", "diagnostic_code":"flight_target_not_found" if request.target_itinerary_id
                else "flight_no_compatible_itinerary", "candidate_count":count}
    except SourceError as error:
        return {"outcome":error.outcome, "diagnostic_code":error.code}
    except Exception as error:
        # The browser's timeout class is distinct from built-in TimeoutError.
        # Preserve only its finite class and stage; never exception text/URL.
        timeout = isinstance(error, TimeoutError) or (type(error).__name__ == "TimeoutError"
            and type(error).__module__.startswith("playwright."))
        set_attributes(error_type="TimeoutError" if timeout else type(error).__name__)
        if timeout:
            return {"outcome":"timeout", "diagnostic_code":"flight_timeout"}
        return {"outcome":"context_mismatch" if stage == "flight_scope" else "unavailable",
                "diagnostic_code":"flight_scope_not_verified" if stage == "flight_scope" else "flight_source_unavailable"}
