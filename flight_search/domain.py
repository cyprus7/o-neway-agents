"""Deterministic interpretation of visible flight cards, never inventory math."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import re
from urllib.parse import urlencode

from .models import FlightSearchRequest, ObservedConnection, ObservedLeg, PriceReadback, SelectedRoute, itinerary_id


# Geographic query labels, not fixtures or a claim of live source coverage.
AIRPORT_CITIES = {"LCA":"Larnaca", "BER":"Berlin", "ATH":"Athens", "PRG":"Prague", "PFO":"Paphos",
                  "WAW":"Warsaw", "IST":"Istanbul", "TBS":"Tbilisi", "LIS":"Lisbon", "VIE":"Vienna",
                  "FCO":"Rome", "BCN":"Barcelona", "DXB":"Dubai",
                  "KZN":"Kazan", "BOB":"Bora Bora", "PPT":"Papeete"}
MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December")
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
MAX_CARDS = 12
MAX_OUTBOUND_BRANCHES = 3
# Exact names actually read in the reviewed source; additional IATA pairs still
# require the visible route row, without inventing a name-to-airport mapping.
AIRPORT_ALIASES = {"LCA":frozenset({"larnaka international airport", "larnaca international airport"}),
                   "BER":frozenset({"berlin brandenburg airport"}),
                   "PFO":frozenset({"paphos international airport"})}


class SourceError(Exception):
    def __init__(self, code, outcome="context_mismatch"):
        self.code, self.outcome = code, outcome
        super().__init__(code)


@dataclass(frozen=True)
class ReadScope:
    origin: str
    destination: str
    date_from: date
    date_to: date
    adults: int
    currency: str
    cabin: str


@dataclass(frozen=True)
class Card:
    label: str
    leg: ObservedLeg
    amount: str


def scope_diagnostic(request: FlightSearchRequest):
    scope = request.request_context
    if scope.origin not in AIRPORT_CITIES or scope.destination not in AIRPORT_CITIES or scope.origin == scope.destination:
        return "flight_unsupported_route", "not_configured"
    if scope.children_ages or scope.adults not in (1, 2):
        return "flight_unsupported_party", "unsupported_party"
    if scope.currency != "EUR":
        return "flight_only_eur", "currency_mismatch"
    if request.constraints.cabin != "economy":
        return "flight_unsupported_cabin", "not_configured"
    if scope.trip_type != "roundtrip" or any(value in {"transport:train", "transport:bus", "transport:car", "transport:no_flight"} for value in scope.fare_constraints):
        return "flight_unsupported_constraints", "not_configured"
    return None


def query_url(request: FlightSearchRequest):
    context = request.request_context
    query = (f"Flights from {AIRPORT_CITIES[context.origin]} to {AIRPORT_CITIES[context.destination]} "
             f"on {context.date_from.isoformat()} returning {context.date_to.isoformat()} for {context.adults} adults")
    return "https://www.google.com/travel/flights?" + urlencode({"hl":"en", "curr":"EUR", "q":query})


def _plain(value):
    return " ".join(str(value).replace("\u200b", "").replace("\u202f", " ").split())


def stop_diagnostic(body, url=""):
    text = _plain(body).casefold()
    if any(term in text for term in ("verify you are human", "unusual traffic", "recaptcha", "captcha", "automated queries")):
        return "flight_challenge", "blocked"
    if any(term in text for term in ("sign in to continue", "sign in to google", "login required")) or "accounts.google.com" in url:
        return "flight_login", "requires_login"
    if any(term in text for term in ("oops, something went wrong", "unable to load results")):
        return "flight_source_unavailable", "unavailable"
    return None


def verify_controls(snapshot, scope: ReadScope):
    if _plain(snapshot.get("trip")) != "Round trip" or _plain(snapshot.get("cabin")) != "Economy":
        raise SourceError("flight_cabin_mismatch")
    if not re.fullmatch(r"Currency\s*EUR", _plain(snapshot.get("currency"))):
        raise SourceError("flight_currency_mismatch")
    passengers = re.fullmatch(r"(\d+) passengers?(?:, change number of passengers\.)?", _plain(snapshot.get("passengers")))
    header = re.search(r"Prices include required taxes \+ fees for (\d+) adults?\.", _plain(snapshot.get("body")))
    if not passengers or not header or int(passengers[1]) != scope.adults or int(header[1]) != scope.adults:
        raise SourceError("flight_party_mismatch")
    for name, expected in (("departure", scope.date_from), ("return", scope.date_to)):
        value = _plain(snapshot.get(name))
        if not re.search(r"\b"+MONTHS[expected.month-1][:3]+r"\s+"+str(expected.day)+r"\b", value):
            raise SourceError("flight_date_mismatch")


def city_label_matches(value: str, airport: str) -> bool:
    # Finite query-label aliases only; visible legs still require exact IATA.
    aliases = {"PPT": {"papeete", "tahiti"}, "BOB": {"bora bora", "bora-bora"}}
    return value.casefold() in aliases.get(airport, {AIRPORT_CITIES[airport].casefold()})


def read_scope(snapshot, request: FlightSearchRequest) -> ReadScope:
    track = re.fullmatch(r"Track prices from (.{1,160}) to (.{1,160}) departing (\d{4}-\d{2}-\d{2}) and returning (\d{4}-\d{2}-\d{2})", _plain(snapshot.get("track_label")))
    context = request.request_context
    origin, destination = _plain(snapshot.get("origin_input")), _plain(snapshot.get("destination_input"))
    if (not city_label_matches(origin, context.origin)
            or not city_label_matches(destination, context.destination)
            or track and (not city_label_matches(track[1], context.origin)
                          or not city_label_matches(track[2], context.destination))):
        raise SourceError("flight_route_mismatch")
    try:
        if track:
            start, end = date.fromisoformat(track[3]), date.fromisoformat(track[4])
        else:
            # On the return page the source removes Track prices. Its ordinary
            # visible calendar marks exactly the selected departure/return days
            # with full year and weekday in their accessible labels.
            labels = snapshot.get("selected_date_labels", ())
            if not isinstance(labels, (list, tuple)) or len(labels) != 2:
                raise SourceError("flight_scope_not_verified")
            selected = {}
            for label in labels:
                match = re.fullmatch(r"([A-Za-z]+), ([A-Za-z]+) (\d{1,2}), (\d{4}), (departure|return) date\.", _plain(label))
                if not match or match[5] in selected or match[2] not in MONTHS:
                    raise SourceError("flight_scope_not_verified")
                value = date(int(match[4]), MONTHS.index(match[2])+1, int(match[3]))
                if WEEKDAYS[value.weekday()] != match[1]:
                    raise SourceError("flight_date_mismatch")
                selected[match[5]] = value
            if set(selected) != {"departure", "return"}:
                raise SourceError("flight_scope_not_verified")
            start, end = selected["departure"], selected["return"]
    except ValueError:
        raise SourceError("flight_date_mismatch") from None
    if (start, end) != (context.date_from, context.date_to):
        raise SourceError("flight_date_mismatch")
    # Adult count comes from two independent visible source labels, not the URL.
    passengers = re.fullmatch(r"(\d+) passengers?(?:, change number of passengers\.)?", _plain(snapshot.get("passengers")))
    if not passengers:
        raise SourceError("flight_party_mismatch")
    scope = ReadScope(context.origin, context.destination, start, end, int(passengers[1]), "EUR", "economy")
    verify_controls(snapshot, scope)
    if scope.adults != context.adults:
        raise SourceError("flight_party_mismatch")
    return scope


def _clock(value):
    return datetime.strptime(value, "%I:%M %p").strftime("%H:%M")


def _label_date(weekday, month, day, departure, *, arrival=False):
    candidates = [departure + timedelta(days=offset) for offset in range(4 if arrival else 1)]
    for value in candidates:
        if MONTHS[value.month-1] == month and value.day == int(day) and WEEKDAYS[value.weekday()] == weekday:
            return value
    raise ValueError("Source date differs")


_CARD = re.compile(
    r"^From (?P<amount>(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d{1,2})?) euros? round trip total\. "
    r"(?:(?P<nonstop>Nonstop|Non-stop)|(?P<stops>[1-3]) stops?) flight with (?P<carriers>[^.]{1,500})\. "
    r"(?:Operated by (?P<operator>[^.]{1,300})\. )?"
    r"Leaves (?P<origin>.{1,160}?) at (?P<depart>\d{1,2}:\d{2} [AP]M) on (?P<dw>[A-Za-z]+), (?P<dm>[A-Za-z]+) (?P<dd>\d{1,2}) "
    r"and arrives at (?P<destination>.{1,160}?) at (?P<arrive>\d{1,2}:\d{2} [AP]M) on (?P<aw>[A-Za-z]+), (?P<am>[A-Za-z]+) (?P<ad>\d{1,2})\. "
    r"Total duration (?:(?P<hours>\d{1,2}) hr(?: (?P<minutes>\d{1,2}) min)?|(?P<only_minutes>\d{1,3}) min)\. (?P<connections>.*)Select flight$")
_CONNECTION = re.compile(r"Layover \((?P<index>[1-3]) of (?P<total>[1-3])\) is a "
    r"(?:(?P<hours>\d{1,2}) hr(?: (?P<minutes>\d{1,2}) min)?|(?P<only_minutes>\d{1,3}) min) "
    r"layover at (?P<airport>.{1,160}?) in (?P<city>.{1,160}?)\.\s*")


def _airport_matches(code, label):
    label = _plain(label).casefold()
    known = next((airport for airport, aliases in AIRPORT_ALIASES.items() if label in aliases), None)
    return (known is None or known == code) and (code not in AIRPORT_ALIASES or label in AIRPORT_ALIASES[code])


def parse_card(row, origin, destination, departure) -> Card | None:
    if not isinstance(row, dict) or not isinstance(row.get("label"), str) or len(row["label"]) > 1600:
        return None
    label = _plain(row["label"])
    route_text = _plain(row.get("text", ""))[:2500]
    pairs = set(re.findall(r"\b([A-Z]{3})\s*[–—-]\s*([A-Z]{3})\b", route_text))
    match = _CARD.fullmatch(label)
    if (pairs != {(origin, destination)} or not match
            or re.search(r"\b(?:business|first)(?:\s+class)?\b|\bpremium\s+economy\b", route_text, re.I)):
        return None
    try:
        if not _airport_matches(origin, match["origin"]) or not _airport_matches(destination, match["destination"]):
            return None
        amount = match["amount"].replace(",", "")
        if not Decimal(0) < Decimal(amount) < Decimal("1000000"):
            return None
        carriers = tuple(part.strip() for part in re.split(r",| and ", match["carriers"]) if part.strip())
        stops = int(match["stops"] or 0)
        details = match["connections"].strip()
        connections = list(_CONNECTION.finditer(details))
        if (len(connections) != stops or "".join(item[0] for item in connections).strip() != details
                or any(int(item["index"]) != index or int(item["total"]) != stops
                       for index, item in enumerate(connections, 1))):
            return None
        leg = ObservedLeg(origin=origin, destination=destination, origin_name=match["origin"], destination_name=match["destination"],
            departure_date=_label_date(match["dw"], match["dm"], match["dd"], departure),
            arrival_date=_label_date(match["aw"], match["am"], match["ad"], departure, arrival=True),
            departure_time=_clock(match["depart"]), arrival_time=_clock(match["arrive"]),
            duration_minutes=int(match["hours"] or 0)*60+int(match["minutes"] or match["only_minutes"] or 0),
            stops=stops, carriers=carriers, operated_by=(match["operator"],) if match["operator"] else (),
            observed_connections=tuple(ObservedConnection(airport_name=item["airport"], city_name=item["city"],
                duration_minutes=int(item["hours"] or 0)*60+int(item["minutes"] or item["only_minutes"] or 0))
                for item in connections))
        return Card(row["label"], leg, amount)
    except (ValueError, TypeError):
        return None


def eligible_leg(leg, constraints):
    return (not constraints.direct_required or leg.stops == 0) and (
        constraints.max_flight_minutes is None or leg.duration_minutes <= constraints.max_flight_minutes)


def make_route(scope, outbound: Card, inbound: Card, source_url, *, clock=None):
    observed = clock or datetime.now(timezone.utc)
    return SelectedRoute(provider_id="google_flights", itinerary_id=itinerary_id(outbound.leg, inbound.leg),
        origin=scope.origin, destination=scope.destination, date_from=scope.date_from, date_to=scope.date_to,
        adults=scope.adults, outbound=outbound.leg, inbound=inbound.leg,
        price=PriceReadback(amount=inbound.amount, adults=scope.adults), source_url=source_url,
        observed_at=observed, retrieved_at=observed, expires_at=observed+timedelta(minutes=10))


def select_route(routes, request):
    eligible = [route for route in routes if eligible_leg(route.outbound, request.constraints)
                and eligible_leg(route.inbound, request.constraints)
                and (request.constraints.arrival_date is None
                     or route.outbound.arrival_date == request.constraints.arrival_date)
                and (request.target_itinerary_id is None or route.itinerary_id == request.target_itinerary_id)]
    return min(eligible, key=lambda route: (
        route.outbound.stops+route.inbound.stops if request.constraints.prefer_direct else 0,
        route.outbound.duration_minutes+route.inbound.duration_minutes, Decimal(route.price.amount), route.itinerary_id)) if eligible else None
