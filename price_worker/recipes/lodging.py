"""Ordinary Google Hotels UI, never a general browser agent or a checkout recipe."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import re
import unicodedata
from urllib.parse import urlencode


CITIES = {"ATH": "Athens", "IST": "Istanbul", "SAW": "Istanbul", "TBS": "Tbilisi",
          "LCA": "Larnaca", "PFO": "Paphos", "AYT": "Antalya", "TFS": "Tenerife",
          "HRG": "Hurghada", "SSH": "Sharm el Sheikh", "DXB": "Dubai", "LIS": "Lisbon",
          "AGP": "Malaga", "NCE": "Nice", "FCO": "Rome", "BUD": "Budapest",
          "PRG": "Prague", "VIE": "Vienna", "KEF": "Reykjavik", "BCN": "Barcelona",
          "FNC": "Madeira", "SOF": "Bansko", "LJU": "Ljubljana", "HEL": "Helsinki", "DPS": "Bali",
          "BER": "Berlin"}
CITY_ALIASES = {
    "Athens": ("Афины",), "Istanbul": ("Стамбул", "İstanbul"), "Tbilisi": ("Тбилиси",),
    "Larnaca": ("Ларнака", "Larnaka"), "Paphos": ("Пафос",), "Antalya": ("Анталья", "Анталия"),
    "Tenerife": ("Тенерифе",), "Hurghada": ("Хургада",),
    "Sharm el Sheikh": ("Шарм-эш-Шейх", "Шарм-эль-Шейх", "Sharm el-Sheikh"),
    "Dubai": ("Дубай",), "Lisbon": ("Лиссабон", "Lisboa"), "Malaga": ("Малага", "Málaga"),
    "Nice": ("Ницца",), "Rome": ("Рим", "Roma"), "Budapest": ("Будапешт",),
    "Prague": ("Прага", "Praha"), "Vienna": ("Вена", "Wien"),
    "Reykjavik": ("Рейкьявик", "Reykjavík"), "Barcelona": ("Барселона",),
    "Madeira": ("Мадейра",), "Bansko": ("Банско",), "Ljubljana": ("Любляна",),
    "Helsinki": ("Хельсинки",), "Bali": ("Бали",), "Berlin": ("Берлин",),
}
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def _city_name(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).casefold().replace("ё", "е")
    return " ".join(re.sub(r"[-–—]", " ", value).split())


def verified_destination_query(context: dict) -> str | None:
    """An airport cannot silently replace the requested lodging destination.

    SOF serves our catalogue's Bansko trip. A Sofia/SOF request is therefore
    unsupported by this recipe rather than being rewritten to Bansko.
    Country and region aliases are deliberately not accepted as city matches.
    """
    canonical = CITIES.get(context.get("destination_iata"))
    destination = context.get("destination")
    if not canonical or not isinstance(destination, str) or not destination.strip():
        return None
    permitted = {_city_name(value) for value in (canonical, *CITY_ALIASES[canonical])}
    return canonical if _city_name(destination) in permitted else None


def google_property_id(destination_iata: str, property_name: str) -> str:
    return "google-hotel:" + hashlib.sha256((destination_iata + property_name[:300]).encode()).hexdigest()[:24]


def valid_discovery_terms(terms: object) -> bool:
    """Accept small plain lodging hints, never component commands or URLs."""
    if not isinstance(terms, list) or len(terms) > 4:
        return False
    if any(not isinstance(term, str) or not term.strip() or len(term) > 80 for term in terms):
        return False
    if sum(map(len, terms)) > 200:
        return False
    component_prefixes = ("transport:", "transport_requirement:", "route:", "weather:", "climate:",
                          "exclude_destination:", "exclude_country:")
    url = re.compile(r"[a-z][a-z0-9+.-]*:/{1,2}|www\.|\b[a-z0-9-]+\.[a-z]{2,63}(?:[/\s:]|$)", re.IGNORECASE)
    return all(not term.strip().casefold().startswith(component_prefixes)
               and not url.search(term)
               and not any(unicodedata.category(char).startswith("C") for char in term)
               for term in terms)


def property_preference(label: str, discovery_terms: list[str]) -> tuple[int, int] | None:
    """Bounded lexical navigation preference; it establishes no property policy."""
    # Negating an exclusion merely permits this kind; it neither forbids it nor
    # expresses a positive preference. Keep original query terms untouched and
    # neutralize only this finite lexical phrase for navigation scoring.
    discovery_terms = [re.sub(
        r"\b(?:(?:do\s+)?not|never|don't)\s+(?:avoid|exclude|without|no)\s+(?:a\s+)?hostels?\b"
        r'|\bне\s+(?:избегать|исключать|исключить|без)\s+хостел\w*',
        ' ', term, flags=re.I) for term in discovery_terms]
    words = set(re.findall(r"[^\W_]+", _city_name(label)))
    if any(re.search(r"\b(?:no|not|without|avoid|exclude)\s+(?:a\s+)?hostels?\b|\b(?:не|без|избегать|исключить)\s+хостел\w*", term.casefold())
           for term in discovery_terms) and any(word.startswith("хостел") or word in {"hostel", "hostels"} for word in words):
        return None
    positive, negative = set(), set()
    ignored = {"a", "an", "the", "and", "with", "for", "hotel", "hotels", "room", "rooms", "отель", "отеля", "номер", "номера", "для", "с", "в", "на"}
    for term in discovery_terms:
        negated = False
        for word in re.findall(r"[^\W_]+", _city_name(term)):
            if word in {"no", "not", "without", "avoid", "exclude", "не", "без", "избегать"}:
                negated = True
            elif word in {"and", "but", "и", "но"}:
                negated = False
            elif word not in ignored:
                (negative if negated else positive).add(word.rstrip("s") if word.isascii() else word)
    normalized = {word.rstrip("s") if word.isascii() else word for word in words}
    score = len(normalized & positive) - 2 * len(normalized & negative)
    hotel = int("hotel" in label.casefold() and "hostel" not in label.casefold())
    return score, hotel


def select_property_label(labels: list[str], destination_iata: str,
                          target_entity_id: str | None = None,
                          discovery_terms: list[str] | None = None) -> str | None:
    candidates = [label for label in labels[:100] if isinstance(label, str)
                  and label.startswith("View prices for ") and label.removeprefix("View prices for ").strip()]
    if target_entity_id is not None:
        # A refresh is scoped to the selected property. A reordered city result
        # list must not silently substitute the next/first hotel for that entity.
        return next((label for label in candidates if google_property_id(
            destination_iata, label.removeprefix("View prices for ")) == target_entity_id), None)
    terms = discovery_terms if discovery_terms is not None else []
    if not valid_discovery_terms(terms):
        return None
    ranked = [(preference, -index, label) for index, label in enumerate(candidates)
              if (preference := property_preference(label.removeprefix("View prices for "), terms)) is not None]
    return max(ranked, key=lambda row: (row[0], row[1]))[2] if ranked else None


def ui_date_matches(value: str, expected: date) -> bool:
    """Month/day readback only; full-year proof is required by date_controls_match."""
    return bool(re.search(r"\b" + MONTHS[expected.month - 1] + r"\s+" + str(expected.day) + r"\b", value))


def selected_dates_match(cells: list[dict], start: date, end: date) -> bool:
    selected = {row.get("iso"): row for row in cells if row.get("selected") is True}
    if len(selected) != 2 or set(selected) != {start.isoformat(), end.isoformat()}:
        return False
    start_label = str(selected[start.isoformat()].get("label", "")).casefold()
    end_label = str(selected[end.isoformat()].get("label", "")).casefold()
    return ("departure date" in start_label and "return date" in end_label
            and bool(re.search(r"\b" + str(start.year) + r"\b", start_label))
            and bool(re.search(r"\b" + str(end.year) + r"\b", end_label)))


def date_controls_match(rows: list[dict], start: date, end: date, selected: list[dict]) -> bool:
    if {row.get("label") for row in rows} != {"Check-in", "Check-out"}:
        return False
    return selected_dates_match(selected, start, end) and all(
        ui_date_matches(str(row.get("value", "")), start if row["label"] == "Check-in" else end)
        for row in rows)


async def selected_calendar_dates(page) -> list[dict]:
    return await page.locator('[data-iso][aria-selected="true"]').filter(visible=True).evaluate_all(
        'els=>els.map(x=>({iso:x.getAttribute("data-iso"),selected:x.getAttribute("aria-selected")==="true",label:x.querySelector("[aria-label]")?.getAttribute("aria-label")}))')


def euro_amount(value: str) -> str | None:
    match = re.fullmatch(r"\s*€\s*((?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d{1,2})?)\s*", value)
    if not match:
        return None
    try:
        amount = Decimal(match[1].replace(",", ""))
        return str(amount) if Decimal("0") < amount < Decimal("1000000") else None
    except InvalidOperation:
        return None


def choose_detailed_rate(rows: list[dict]) -> dict | None:
    """Prefer an explicitly described private room; preserve its displayed total."""
    candidates = []
    for index, row in enumerate(rows[:10]):
        label, text = str(row.get("label", "")), str(row.get("text", ""))
        totals = row.get("current_totals", [])
        # Only the visible Stay-total amount from the specific rate row is
        # eligible. A first currency-looking string may be an old crossed price.
        total = euro_amount(totals[0]) if isinstance(totals, list) and len(totals) == 1 and isinstance(totals[0], str) else None
        if not total or not label:
            continue
        lower = label.casefold()
        dorm = bool(re.search(r"\bdorm(?:itory)?\b|\bbed in\b|\bshared room\b", lower))
        priority = 0 if dorm else 2 if any(word in lower for word in ("private", "ensuite", "en suite")) else 1 if re.search(r"\b(?:double|twin|single|triple)\s+room\b", lower) else 0
        guest = re.search(r"\b\d+ guests?\b", text)
        candidates.append((priority, -index, {"label": label[:300], "total": total, "guests": guest[0] if guest else None}))
    return max(candidates, key=lambda row: (row[0], row[1]))[2] if candidates else None


def reference_observation(*, context: dict, property_name: str, source_url: str,
                          total: str, room_label: str | None, room_guests: str | None) -> dict:
    observed = datetime.now(timezone.utc)
    nights = (date.fromisoformat(context["date_to"]) - date.fromisoformat(context["date_from"])).days
    label = f"Указано источником за {nights} ночей; тип размещения и номер требуют проверки"
    if room_label:
        label = f"{room_label}; {room_guests or 'состав тарифа не указан'}; за {nights} ночей"
    # Google may surface a dorm bed even when the property calls itself a hotel.
    # Its date/party controls do not establish a room allocation or rate policy.
    return {
        "provider_id": "google_hotels", "outcome": "reference",
        "scope": {"component": "stay", "entity_level": "property",
                  "entity_id": google_property_id(context["destination_iata"], property_name),
                  "provider_entity_id": property_name, "destination": context["destination_iata"],
                  "destination_city": context.get("destination"),
                  "date_from": context["date_from"], "date_to": context["date_to"],
                  "adults": None, "children_ages": None, "rooms": None,
                  "trip_type": "not_applicable", "unit": "unknown", "currency": "EUR",
                  "taxes": "unknown", "mandatory_fees": "unknown",
                  "context_verified": False, "unverified_fields": ["adults", "children_ages", "rooms", "room_allocation", "rate_policy"],
                  "assumptions": [f"Google Hotels UI requested {context['adults']} adults and no children; the advertised rate's party/allocation was not verified"]},
        "value": {"kind": "lower_bound", "min": total, "max": None, "currency": "EUR"},
        "normalized_group": None, "source_url": source_url,
        "observed_at": observed.isoformat(), "retrieved_at": observed.isoformat(),
        "expires_at": (observed + timedelta(minutes=15)).isoformat(),
        "reason": f"{property_name}: публичная цена Google Hotels. {label}. Наличие и применимость тарифа не подтверждены.",
        "unit_label": label, "included_charges": [],
        "unknown_charges": ["Налоги, обязательные сборы, дополнительные услуги и специальные условия"],
        "component_complete": False, "availability_verified": False,
        "normalization_note": "Stay total прочитан напрямую; округлённая цена за ночь не умножалась.",
    }


async def google_hotels(page, context: dict) -> dict:
    """At most 19 ordinary UI actions, bounded by the caller's whole-operation timeout."""
    if context.get("destination_iata") not in CITIES:
        return {"outcome": "not_configured", "diagnostic_code": "unsupported_destination"}
    city = verified_destination_query(context)
    if city is None:
        return {"outcome": "context_mismatch", "diagnostic_code": "destination_city_airport_mismatch"}
    if context.get("currency") != "EUR":
        return {"outcome": "currency_mismatch", "diagnostic_code": "only_eur_supported"}
    if context.get("children_ages") or context.get("rooms", 1) != 1 or context.get("adults") not in (1, 2):
        return {"outcome": "unsupported_party", "diagnostic_code": "single_room_adult_only_recipe"}
    terms = context.get("discovery_terms", [])
    if not valid_discovery_terms(terms):
        return {"outcome":"context_mismatch", "diagnostic_code":"invalid_discovery_terms"}
    if context.get("target_property_path") is not None or (context.get("target_entity_id") is not None
            and not re.fullmatch(r"google-hotel:[a-f0-9]{24}", str(context["target_entity_id"]))):
        return {"outcome":"context_mismatch", "diagnostic_code":"provider_target_mismatch"}
    start, end = date.fromisoformat(context["date_from"]), date.fromisoformat(context["date_to"])
    if not 1 <= (end - start).days <= 30:
        return {"outcome": "context_mismatch", "diagnostic_code": "unsupported_duration"}
    actions = 0
    async def click(locator, **options):
        nonlocal actions
        actions += 1
        if actions > 19:
            raise RuntimeError("action_limit")
        await locator.click(**options)
    query = " ".join(["hotels in", city, *terms])
    await page.goto("https://www.google.com/travel/hotels?" + urlencode({"q": query, "curr": "EUR", "hl": "en"}))
    actions += 1
    body = (await page.locator("body").inner_text())[:3000].casefold()
    if any(term in body for term in ("unusual traffic", "verify you are human", "recaptcha")):
        return {"outcome": "blocked", "diagnostic_code": "challenge_detected"}
    reject = page.get_by_role("button", name="Reject all", exact=True)
    if await reject.count():
        await click(reject)
    # Never trust checkin/checkout/adults query parameters: Google ignores them.
    links = page.locator('a[aria-label^="View prices for"]').filter(visible=True)
    await links.first.wait_for(timeout=6000)
    await click(page.get_by_role("textbox", name="Check-in", exact=True))
    await click(page.locator(f'[data-iso="{start.isoformat()}"]'))
    await click(page.locator(f'[data-iso="{end.isoformat()}"]'))
    selected = await selected_calendar_dates(page)
    if not selected_dates_match(selected, start, end):
        return {"outcome": "context_mismatch", "diagnostic_code": "calendar_year_or_selection_mismatch", "actions": actions}
    # Google temporarily aria-hides an ancestor of this visible dialog. The
    # observed Done button is visible but transitions while the calendar updates.
    await click(page.get_by_role("button", name="Done", exact=True, include_hidden=True).filter(visible=True), force=True)
    travelers = page.locator('[aria-label^="Number of travelers."]').filter(visible=True)
    await travelers.wait_for()
    if context["adults"] == 1:
        await click(travelers)
        await click(page.get_by_role("button", name="Remove adult", exact=True))
        await click(page.get_by_role("button", name="Done", exact=True, include_hidden=True).filter(visible=True))
    current = await travelers.get_attribute("aria-label")
    check_in = await page.locator('input[aria-label="Check-in"]').filter(visible=True).input_value()
    check_out = await page.locator('input[aria-label="Check-out"]').filter(visible=True).input_value()
    controls = [{"label": "Check-in", "value": check_in}, {"label": "Check-out", "value": check_out}]
    if not date_controls_match(controls, start, end, selected) or f"is {context['adults']}." not in (current or ""):
        return {"outcome": "context_mismatch", "diagnostic_code": "ui_context_not_applied", "actions": actions}
    await links.first.wait_for(timeout=6000)
    labels = await links.evaluate_all('(els)=>els.slice(0,100).map(x=>x.getAttribute("aria-label"))')
    # Prefer an explicitly named hotel; this is a navigation choice, not proof
    # that its cheapest rate is a private room. The rate label is preserved below.
    target = select_property_label(labels, context["destination_iata"], context.get("target_entity_id"), terms)
    if target is None:
        diagnostic = "selected_property_not_found" if context.get("target_entity_id") is not None else "no_property_links"
        return {"outcome": "no_offers", "diagnostic_code": diagnostic, "actions": actions}
    property_name = target.removeprefix("View prices for ")[:300]
    await click(page.get_by_role("link", name=target, exact=True))
    detail_body = (await page.locator("body").inner_text())[:3000].casefold()
    if any(term in detail_body for term in ("unusual traffic", "verify you are human", "recaptcha")):
        return {"outcome":"blocked", "diagnostic_code":"challenge_detected", "actions":actions}
    await page.locator("h1").filter(has_text=property_name).last.wait_for(timeout=6000)
    display = page.locator('[aria-label^="Price displayed"]').filter(visible=True).last
    try:
        await display.wait_for(timeout=3500)
    except Exception:
        detail_text = (await page.locator("body").inner_text()).casefold()
        if "having trouble loading results" in detail_text:
            return {"outcome": "unavailable", "diagnostic_code": "google_property_results_unavailable", "actions": actions}
        return {"outcome": "parse_error", "diagnostic_code": "price_mode_control_not_found", "actions": actions}
    await click(display)
    await click(page.get_by_role("radio", name=re.compile("Stay total"), include_hidden=True).filter(visible=True))
    await click(page.get_by_role("button", name="Done", exact=True, include_hidden=True).filter(visible=True))
    # The site's aria-label stays at "Nightly total" after changing the visible
    # mode. Read the displayed control text, which is the authoritative UI state.
    await page.locator('[aria-label^="Price displayed"]').filter(visible=True).filter(has_text=re.compile(r"^Stay total$")).last.wait_for(timeout=3000)
    # Re-open the detail calendar and prove its selected full ISO dates/year.
    # Month/day text alone could match a different year or only one date field.
    await click(page.locator('input[aria-label="Check-in"]').filter(visible=True).last)
    final_selected = await selected_calendar_dates(page)
    await click(page.get_by_role("button", name="Done", exact=True, include_hidden=True).filter(visible=True), force=True)
    final_dates = await page.locator('input[aria-label="Check-in"],input[aria-label="Check-out"]').filter(visible=True).evaluate_all(
        'els=>els.map(x=>({label:x.getAttribute("aria-label"),value:x.value}))')
    if not date_controls_match(final_dates, start, end, final_selected):
        return {"outcome": "context_mismatch", "diagnostic_code": "detail_dates_changed", "actions": actions}
    final_text = await page.locator("body").inner_text()
    if any(term in final_text[:18000].casefold() for term in ("unusual traffic", "verify you are human", "recaptcha")):
        return {"outcome":"blocked", "diagnostic_code":"challenge_detected", "actions":actions}
    if not re.search(r"Currency\s*EUR", final_text.replace("\u200b", "")):
        return {"outcome": "currency_mismatch", "diagnostic_code": "eur_display_not_verified", "actions": actions}
    # Rate labels can render before their asynchronous totals. Wait for the
    # observed current-total marker within the same whole-request deadline.
    try:
        await page.locator('span.UeIHqb').filter(visible=True).first.wait_for(timeout=3500)
    except Exception:
        return {"outcome": "parse_error", "diagnostic_code": "detailed_stay_total_not_found", "actions": actions}
    # Extract the first detailed rate row with its actual displayed total, rather
    # than unrelated advertisements, hidden preloaded amounts or rounded nightly prices.
    rows = await page.locator("h4").filter(visible=True).evaluate_all("""els => els.map(h => {
      const row = h.parentElement?.parentElement?.parentElement;
      const eligible = el => {
        if (!el.getClientRects().length) return false;
        for (let node = el; node; node = node.parentElement) {
          const style = getComputedStyle(node);
          if (['DEL','S'].includes(node.tagName) || node.getAttribute('aria-hidden') === 'true'
              || style.display === 'none' || style.visibility !== 'visible'
              || style.textDecorationLine.includes('line-through')) return false;
        }
        return true;
      };
      // Observed Google markup: UeIHqb is Stay total, whereas iqYCVb/MW1oTb
      // are nightly prices. Markup changes fail closed; no text-wide regex.
      const totals = row ? [...row.querySelectorAll('span.UeIHqb')].filter(eligible).map(x => x.innerText.trim()) : [];
      return {label:h.innerText, text:row?.innerText || '', current_totals:totals};
    })""")
    row = choose_detailed_rate(rows)
    if row:
        observation = reference_observation(context=context, property_name=property_name,
            source_url=page.url, total=row["total"], room_label=row["label"], room_guests=row["guests"])
        return {"outcome": "reference", "observation": observation, "diagnostic_code": "public_stay_total_room_scope_unverified", "actions": actions}
    return {"outcome": "parse_error", "diagnostic_code": "detailed_stay_total_not_found", "actions": actions}
