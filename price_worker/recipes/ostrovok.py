"""Read bounded public Ostrovok UI observations; never enter a reservation flow."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import re
from urllib.parse import parse_qs, urlencode, urlsplit

from .lodging import property_preference, valid_discovery_terms, verified_destination_query


# Paths were observed through the official site's public search/homepage links.
CITY_PATHS = {"Athens":"/hotel/greece/athens/", "Istanbul":"/hotel/turkey/istanbul/",
              "Prague":"/hotel/czech_republic/prague/", "Tbilisi":"/hotel/georgia/tbilisi/",
              "Berlin":"/hotel/germany/berlin/"}
PROPERTY_PATH = re.compile(r"^(?:" + "|".join(re.escape(path) for path in CITY_PATHS.values())
                           + r")mid[0-9]{1,12}/[a-zA-Z0-9_-]{1,160}/$")
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def supported_context(context: dict) -> str | None:
    city = verified_destination_query(context)
    if context.get("destination_iata") not in ("ATH", "IST", "SAW", "PRG", "TBS", "BER"):
        return "unsupported_destination"
    if city not in CITY_PATHS:
        return "destination_city_airport_mismatch"
    if context.get("currency") != "EUR":
        return "only_eur_supported"
    if context.get("children_ages") or context.get("rooms", 1) != 1 or context.get("adults") not in (1, 2):
        return "single_room_adult_only_recipe"
    try:
        nights = (date.fromisoformat(context["date_to"]) - date.fromisoformat(context["date_from"])).days
    except (KeyError, TypeError, ValueError):
        return "unsupported_duration"
    return None if 1 <= nights <= 30 else "unsupported_duration"


def canonical_property_path(url: str, city: str) -> str | None:
    try:
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or parsed.hostname != "ostrovok.ru" or parsed.port not in (None,443)
                or parsed.username or parsed.password or parsed.fragment or city not in CITY_PATHS):
            return None
        if PROPERTY_PATH.fullmatch(parsed.path) and parsed.path.startswith(CITY_PATHS[city]):
            return parsed.path
    except (ValueError, TypeError):
        pass
    return None


def ostrovok_property_id(path: str) -> str:
    if not PROPERTY_PATH.fullmatch(path):
        raise ValueError("Invalid canonical Ostrovok property path")
    return "ostrovok:" + hashlib.sha256(path.encode()).hexdigest()[:24]


def target_property_diagnostic(context: dict) -> str | None:
    """Validate an optional exact source target before any browser action."""
    path = context.get("target_property_path")
    if path is None:
        return None
    city = verified_destination_query(context)
    if (not isinstance(path, str) or len(path) > 256 or not PROPERTY_PATH.fullmatch(path)
            or canonical_property_path("https://ostrovok.ru" + path, city) != path):
        return "ostrovok_property_context_mismatch"
    if context.get("target_entity_id") != ostrovok_property_id(path):
        return "provider_target_mismatch"
    return None


def select_property(links: list[dict], city: str, target: str | None = None,
                    discovery_terms: list[str] | None = None) -> dict | None:
    candidates, seen = [], set()
    for row in links[:100]:
        if not isinstance(row, dict) or not isinstance(row.get("href"), str):
            continue
        path = canonical_property_path(row["href"], city)
        if path is None or path in seen:
            continue
        label = str(row.get("label", "")).strip()[:300]
        if not label:
            continue
        seen.add(path)
        candidate = {"path":path, "label":label}
        if target is not None:
            if ostrovok_property_id(path) == target:
                return candidate
        else:
            candidates.append(candidate)
    if target is not None:
        return None
    terms = discovery_terms if discovery_terms is not None else []
    if not valid_discovery_terms(terms):
        return None
    ranked = [(preference, -index, row) for index, row in enumerate(candidates)
              if (preference := property_preference(row["label"], terms)) is not None]
    return max(ranked, key=lambda row: (row[0], row[1]))[2] if ranked else None


def _date_label(value: str, expected: date) -> bool:
    # The actual property controls expose month, day, four-digit year and weekday.
    pattern = rf"^{MONTHS[expected.month-1]}\s+0?{expected.day},\s+{expected.year},\s+[A-Za-z]{{3}}$"
    return bool(re.fullmatch(pattern, value.strip()))


def context_diagnostic(snapshot: dict, context: dict, expected_path: str) -> str | None:
    url = str(snapshot.get("url", ""))
    body = str(snapshot.get("body", "")).casefold()
    if parse_qs(urlsplit(url).query).get("dateless_form") == ["yes"] or "no dates selected" in body:
        return "ostrovok_dateless_results"
    city = verified_destination_query(context)
    if canonical_property_path(url, city) != expected_path:
        return "ostrovok_property_context_mismatch"
    start, end = date.fromisoformat(context["date_from"]), date.fromisoformat(context["date_to"])
    dates = snapshot.get("dates", [])
    if len(dates) != 2 or not _date_label(dates[0], start) or not _date_label(dates[1], end):
        return "ostrovok_dates_not_verified"
    nights = (end-start).days
    party = [" ".join(str(label).split()).casefold() for label in snapshot.get("party", [])]
    expected = f"for {nights} {'night' if nights == 1 else 'nights'}, for {context['adults']} {'adult' if context['adults'] == 1 else 'adults'}"
    if party != [expected]:
        return "ostrovok_party_not_verified"
    if snapshot.get("room_controls") != ["1 room for"]:
        return "ostrovok_rooms_not_verified"
    return None


def euro_amount(value: str) -> str | None:
    # Thin/nonbreaking spaces separate the currency in the observed UI.
    value = value.replace("\u202f", " ").replace("\u00a0", " ")
    match = re.fullmatch(r"\s*€\s*((?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d{1,2})?)\s*", value)
    if not match:
        return None
    try:
        amount = Decimal(match[1].replace(",", ""))
        return str(amount) if Decimal(0) < amount < Decimal("1000000") else None
    except InvalidOperation:
        return None


def parse_rate_groups(groups: list[dict], context: dict) -> dict | None:
    nights = (date.fromisoformat(context["date_to"]) - date.fromisoformat(context["date_from"])).days
    expected = f"for {nights} {'night' if nights == 1 else 'nights'} for {context['adults']} {'guest' if context['adults'] == 1 else 'guests'}"
    for group in groups[:10]:
        room = str(group.get("room", "")).strip()[:300]
        if not room or group.get("aligned") is not True:
            continue
        for rate in group.get("rates", [])[:12]:
            amounts = rate.get("amounts", [])
            if not isinstance(amounts, list) or len(amounts) != 1 or not isinstance(amounts[0], str):
                continue
            amount = euro_amount(amounts[0])
            scope = " ".join(str(rate.get("scope", "")).split()).casefold()
            if amount is None or scope != expected:
                continue
            return {"room":room, "amount":amount, "scope":scope,
                    "extras":[str(text)[:400] for text in rate.get("extras", [])[:8] if str(text).strip()],
                    "terms":[str(text)[:300] for text in rate.get("terms", [])[:8] if str(text).strip()]}
    return None


def reference_observation(context: dict, path: str, property_name: str, rate: dict) -> dict:
    observed = datetime.now(timezone.utc)
    start, end = date.fromisoformat(context["date_from"]), date.fromisoformat(context["date_to"])
    source = "https://ostrovok.ru" + path + "?" + urlencode({"dates":f"{start:%d.%m.%Y}-{end:%d.%m.%Y}","guests":str(context["adults"]),"search":"yes"})
    label = f"{rate['room']}; {rate['scope']}"[:400]
    extras = rate["extras"]
    # Price_taxes is also used for the positive text "All taxes included".
    # Preserve its meaning, and retain unrecognized charge wording as unknown.
    included = [text for text in extras if re.fullmatch(r"(?:all )?taxes(?: and fees)? included", " ".join(text.split()).casefold())]
    excluded = [text for text in extras if text.lstrip().startswith("+")]
    unknown = [text for text in extras if text not in included and text not in excluded]
    taxes = "excluded" if any("tax" in text.casefold() for text in excluded) else "included" if included else "unknown"
    return {"provider_id":"ostrovok", "outcome":"reference",
        "scope":{"component":"stay", "entity_level":"property", "entity_id":ostrovok_property_id(path),
            "provider_entity_id":property_name, "destination":context["destination_iata"],
            "destination_city":context["destination"], "date_from":context["date_from"], "date_to":context["date_to"],
            "adults":context["adults"], "children_ages":[], "rooms":1,
            "trip_type":"not_applicable", "unit":"group_stay", "currency":"EUR",
            "taxes":taxes,
            "mandatory_fees":"unknown", "context_verified":False,
            "unverified_fields":["mandatory_fees", "rate_policy", "availability"],
            "assumptions":["Displayed date/year and adult-only single-room scope were checked; mandatory-charge completeness is not established"]},
        "value":{"kind":"lower_bound", "min":rate["amount"], "max":None, "currency":"EUR"},
        "normalized_group":None, "source_url":source,
        "observed_at":observed.isoformat(), "retrieved_at":observed.isoformat(),
        "expires_at":(observed + timedelta(minutes=15)).isoformat(),
        "unit_label":label,
        "reason":f"{property_name}: публичная цена Островка. {label}. {'; '.join(rate['terms'])}. Наличие и полный состав обязательных платежей не подтверждены."[:1500],
        "included_charges":included, "excluded_charges":excluded,
        "unknown_charges":[*unknown,"Полнота обязательных платежей, дополнительные услуги и специальные условия"],
        "component_complete":False, "availability_verified":False,
        "normalization_note":"Прочитана текущая цена конкретного тарифа за указанный срок; отдельные сборы не прибавлялись, цена за ночь не умножалась."}


def is_challenge(text: str) -> bool:
    return any(term in text.casefold() for term in ("captcha", "verify you are human", "unusual traffic", "подтвердите, что вы человек"))


async def visible_context(page, click) -> dict:
    # The broad substring Dates_date also matches wrappers such as Dates_dates
    # and Dates_dateWrapper. Only the two actual buttons carry date__<hash>.
    dates = page.locator('button[class*="Dates_date__"]').filter(visible=True)
    await dates.first.wait_for(timeout=4000)
    labels = await dates.all_inner_texts()
    party = await page.get_by_text(re.compile(r"^For \d+ nights?, for \d+ adults?$")).filter(visible=True).all_inner_texts()
    await click(page.get_by_role("button",name="Change",exact=True))
    rooms = page.get_by_text("1 room for",exact=True).filter(visible=True)
    # all_inner_texts is an immediate read. The popup animates in after the
    # Change click, so reading without visibility wait returned an empty list.
    await rooms.wait_for(timeout=4000)
    room_controls = await rooms.all_inner_texts()
    await click(page.get_by_role("button",name="popup-close",exact=True))
    return {"dates":labels,"party":party,"room_controls":room_controls}


async def ostrovok(page, context: dict) -> dict:
    diagnostic = supported_context(context)
    if diagnostic:
        outcome = {"single_room_adult_only_recipe":"unsupported_party", "only_eur_supported":"currency_mismatch", "unsupported_destination":"not_configured"}.get(diagnostic,"context_mismatch")
        return {"outcome":outcome,"diagnostic_code":diagnostic}
    target = context.get("target_entity_id")
    if target is not None and (not isinstance(target, str) or not re.fullmatch(r"ostrovok:[a-f0-9]{24}", target)):
        return {"outcome":"context_mismatch","diagnostic_code":"provider_target_mismatch"}
    diagnostic = target_property_diagnostic(context)
    if diagnostic:
        return {"outcome":"context_mismatch","diagnostic_code":diagnostic}
    terms = context.get("discovery_terms", [])
    if not valid_discovery_terms(terms):
        return {"outcome":"context_mismatch","diagnostic_code":"invalid_discovery_terms"}
    city = verified_destination_query(context)
    start, end = date.fromisoformat(context["date_from"]), date.fromisoformat(context["date_to"])
    query = urlencode({"dates":f"{start:%d.%m.%Y}-{end:%d.%m.%Y}", "guests":str(context["adults"]), "search":"yes"})
    actions = 0
    async def click(locator):
        nonlocal actions
        actions += 1
        if actions > 12:
            raise RuntimeError("action_limit")
        await locator.click()

    # Set the currency through the ordinary UI in a new context; query arguments
    # or stored/default currency do not establish what prices the page displays.
    await page.goto("https://ostrovok.ru/")
    actions += 1
    if is_challenge((await page.locator("body").inner_text())[:12000]):
        return {"outcome":"blocked","diagnostic_code":"challenge_detected","actions":actions}
    await click(page.get_by_test_id("currency-control"))
    euro = page.get_by_role("button", name="EUR Euro , €", exact=True).first
    if not await euro.count():
        return {"outcome":"currency_mismatch","diagnostic_code":"ostrovok_currency_control_unavailable","actions":actions}
    await click(euro)
    # The city index supplies property identities only. Asking for city-wide
    # priced availability and then the same property's rates performed two
    # expensive inventory searches. Actual amounts are read exclusively from
    # the dated property page after visible date/year/party verification below.
    path = context.get("target_property_path")
    if path is None:
        await page.goto("https://ostrovok.ru" + CITY_PATHS[city])
        actions += 1
        if is_challenge((await page.locator("body").inner_text())[:12000]):
            return {"outcome":"blocked","diagnostic_code":"challenge_detected","actions":actions}
        links = page.locator('a[href*="/mid"]').filter(visible=True)
        await links.first.wait_for(timeout=8000)
        if is_challenge((await page.locator("body").inner_text())[:12000]):
            return {"outcome":"blocked","diagnostic_code":"challenge_detected","actions":actions}
        candidates = await links.evaluate_all("els=>els.slice(0,100).map(el=>({href:el.href,label:el.innerText.trim()}))")
        selected = select_property(candidates, city, target, terms)
        if selected is None:
            return {"outcome":"no_offers","diagnostic_code":"selected_property_not_found" if target else "ostrovok_no_property_links","actions":actions}
        path = selected["path"]
    await page.goto("https://ostrovok.ru" + path + "?" + query)
    actions += 1
    body = (await page.locator("body").inner_text())[:18000]
    if is_challenge(body):
        return {"outcome":"blocked","diagnostic_code":"challenge_detected","actions":actions}
    if parse_qs(urlsplit(page.url).query).get("dateless_form") == ["yes"] or "no dates selected" in body.casefold():
        return {"outcome":"context_mismatch","diagnostic_code":"ostrovok_dateless_results","actions":actions}
    # Read the visible single-room form too, rather than inferring room count
    # solely from the requested URL. Opening Change does not submit a booking.
    controls = await visible_context(page, click)
    diagnostic = context_diagnostic({"url":page.url,"body":body,**controls},context,path)
    if diagnostic:
        return {"outcome":"context_mismatch","diagnostic_code":diagnostic,"actions":actions}
    await page.locator('[class*="Price_baseAmount"]').filter(visible=True).first.wait_for(timeout=8000)
    if is_challenge((await page.locator("body").inner_text())[:18000]):
        return {"outcome":"blocked","diagnostic_code":"challenge_detected","actions":actions}
    groups = await page.locator('[class*="RoomGroup_wrapper"]').filter(visible=True).evaluate_all("""els=>els.slice(0,10).map(group=>{
      const visible=el=>{
        if(!el.getClientRects().length)return false;
        for(let node=el;node;node=node.parentElement){
          const style=getComputedStyle(node);
          if(['DEL','S'].includes(node.tagName)||node.getAttribute('aria-hidden')==='true'||style.display==='none'||style.visibility!=='visible'||style.textDecorationLine.includes('line-through'))return false;
        }return true;
      };
      const rows=[...group.querySelectorAll('table tr')];
      const priceRow=rows.find(row=>row.querySelector('[class*="Price_baseAmount"]'));
      const prices=priceRow?[...priceRow.querySelectorAll('td')]:[];
      const aligned=!!prices.length&&rows.every(row=>row.querySelectorAll('td').length===prices.length);
      return {room:group.querySelector('[class*="RoomGroup_name"]')?.innerText||'',aligned,
        rates:prices.slice(0,12).map((cell,i)=>({
          amounts:[...cell.querySelectorAll('[class*="Price_baseAmount"]')].filter(visible).map(el=>el.innerText.trim()),
          scope:cell.querySelector('[class*="Price_guestsAndNights"]')?.innerText||'',
          extras:[...cell.querySelectorAll('[class*="Price_taxes"]')].filter(visible).map(el=>el.innerText.trim()),
          terms:rows.filter(row=>row!==priceRow).map(row=>row.querySelectorAll('td')[i]).filter(cell=>cell&&visible(cell)&&!cell.querySelector('a[href*="/orders/"]')).map(cell=>cell.innerText.trim()).filter(Boolean)
        }))};
    })""")
    rate = parse_rate_groups(groups, context)
    if rate is None:
        return {"outcome":"parse_error","diagnostic_code":"ostrovok_current_rate_not_found","actions":actions}
    # h1 provides the real property's name, not an unrelated image/review label.
    name = (await page.locator("h1").inner_text()).strip()[:300]
    return {"outcome":"reference","diagnostic_code":"ostrovok_public_rate_charges_unverified","actions":actions,
            "observation":reference_observation(context,path,name,rate)}
