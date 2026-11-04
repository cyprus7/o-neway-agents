"""Source-reported regional event values and deterministic eligibility.

These values are intentionally independent of travel inventory and runtime LLM models.
Only explicit Event JSON-LD is read; offers, article dates and annual recurrence are ignored.
"""
from __future__ import annotations

import hashlib
import html
import json
import math
import re
import unicodedata
from datetime import date, datetime, timezone
from html.parser import HTMLParser
from typing import Any, Literal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


MAX_HTML_BYTES = 1024 * 1024
MAX_RESULTS = 3
CACHE_TTL = 3600

# Exact official hosts whose actual Event structured data was checked for feature007.
# Host timezone is publisher context, never the browser or departure timezone.
SOURCE_HOSTS = {
    "goturkiye.com": ("Go Türkiye — официальный туристический портал", "TR", "Europe/Istanbul"),
    "www.ceskafilharmonie.cz": ("Czech Philharmonic — официальный организатор", "CZ", "Europe/Prague"),
    "www.o2arena.cz": ("O2 arena — официальная площадка", "CZ", "Europe/Prague"),
}

COUNTRIES = {
    "TR": ("tr", "turkey", "turkiye", "türkiye", "турция"),
    "CZ": ("cz", "czechia", "czech republic", "česko", "чехия"),
    "CY": ("cy", "cyprus", "кипр"), "GR": ("gr", "greece", "греция"),
    "GE": ("ge", "georgia", "грузия"), "ES": ("es", "spain", "испания"),
    "PT": ("pt", "portugal", "португалия"), "FR": ("fr", "france", "франция"),
    "GB": ("gb", "uk", "united kingdom", "great britain", "великобритания"),
    "US": ("us", "usa", "united states", "united states of america", "сша"),
    "AT": ("at", "austria", "австрия"), "DE": ("de", "germany", "германия"),
    "IT": ("it", "italy", "италия"), "AE": ("ae", "uae", "united arab emirates", "оаэ"),
    "EG": ("eg", "egypt", "египет"), "ID": ("id", "indonesia", "индонезия"),
    "HU": ("hu", "hungary", "венгрия"), "BG": ("bg", "bulgaria", "болгария"),
    "SI": ("si", "slovenia", "словения"), "IS": ("is", "iceland", "исландия"),
    "FI": ("fi", "finland", "финляндия"), "PL": ("pl", "poland", "польша"),
    "RU": ("ru", "russia", "russian federation", "россия"),
}

CITY_ALIASES = {
    "istanbul": ("istanbul", "i̇stanbul", "стамбул"),
    "prague": ("prague", "praha", "прага"),
}

EVENT_TYPES = frozenset({
    "Event", "MusicEvent", "SportsEvent", "Festival", "ExhibitionEvent", "TheaterEvent",
    "DanceEvent", "VisualArtsEvent", "ComedyEvent", "FoodEvent", "SocialEvent", "EducationEvent",
    "ChildrensEvent", "ScreeningEvent", "LiteraryEvent", "BusinessEvent",
})


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalized(value: str) -> str:
    value = unicodedata.normalize("NFKD", value.casefold().replace("ё", "е"))
    value = "".join(char for char in value if not unicodedata.combining(char))
    return " ".join(re.sub(r"[^\w]+", " ", value, flags=re.UNICODE).split())


def source_url(value: Any) -> str | None:
    """No arbitrary hosts, credentials, ports, active schemes or redirect parameters.

    Tracking parameters may be discarded. A small numeric event-id/language query can
    identify a real source; all other query payloads are rejected before DNS or HTTP.
    """
    if not isinstance(value, str) or len(value) > 2048 or re.search(r"[\x00-\x20\\]", value):
        return None
    try:
        parsed = urlsplit(value)
        host = parsed.hostname or ""
        if (parsed.scheme != "https" or host not in SOURCE_HOSTS or parsed.netloc != host
                or parsed.username is not None or parsed.password is not None or parsed.port is not None):
            return None
        params = []
        for key, item in parse_qsl(parsed.query, keep_blank_values=True, max_num_fields=12):
            if key.casefold().startswith("utm_") or key.casefold() in ("gclid", "fbclid"):
                continue
            if key not in ("id", "event", "event_id", "eventId", "lang") or not re.fullmatch(r"[A-Za-z0-9_-]{1,50}", item):
                return None
            params.append((key, item))
        return urlunsplit(("https", host, parsed.path or "/", urlencode(params), ""))
    except (ValueError, UnicodeError):
        return None


def canonical_url(value: str) -> str:
    parsed = urlsplit(value)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/") or "/", parsed.query, ""))


def aware_timestamp(value: str) -> str:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp_timezone_required")
    return parsed.astimezone(timezone.utc).isoformat()


class EventSource(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider: str = Field(min_length=1, max_length=180)
    source_url: str
    retrieved_at: str
    freshness: Literal["live", "cached"]

    @field_validator("source_url")
    @classmethod
    def official_url(cls, value: str) -> str:
        result = source_url(value)
        if result is None:
            raise ValueError("unreviewed_event_source")
        return result

    @field_validator("retrieved_at")
    @classmethod
    def timestamp(cls, value: str) -> str:
        return aware_timestamp(value)


class RegionalEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=100)
    title: str = Field(min_length=1, max_length=240)
    start_date: date
    end_date: date
    location_name: str = Field(min_length=1, max_length=240)
    location_city: str = Field(min_length=1, max_length=180)
    category: Literal["culture", "music", "sport", "exhibition", "other"]
    significance: Literal["experience", "plan_ahead"]
    reason: str = Field(min_length=1, max_length=700)
    source: EventSource
    verification: Literal["source_reported"] = "source_reported"

    @model_validator(mode="after")
    def ordered(self) -> "RegionalEvent":
        if self.end_date < self.start_date:
            raise ValueError("invalid_event_date_order")
        return self


class EventResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["ready", "empty", "unavailable", "dates_required", "not_checked"] = "not_checked"
    items: list[RegionalEvent] = Field(default_factory=list, max_length=MAX_RESULTS)
    checked_at: str | None = None

    @field_validator("checked_at")
    @classmethod
    def timestamp(cls, value: str | None) -> str | None:
        return aware_timestamp(value) if value is not None else None

    @model_validator(mode="after")
    def valid_state(self) -> "EventResult":
        if self.status == "ready" and (not self.items or self.checked_at is None):
            raise ValueError("ready_requires_items_and_check_time")
        if self.status != "ready" and self.items:
            raise ValueError("nonready_result_cannot_contain_events")
        return self


# An import alias makes the prose entity name convenient without changing the agreed API.
RegionalEventResult = EventResult


class SourceFact(BaseModel):
    """Typed, destination-independent facts persisted for at most one hour."""
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=240)
    start_date: date
    end_date: date
    venue: str = Field(min_length=1, max_length=240)
    city: str = Field(default="", max_length=180)
    country: str | None = Field(default=None, max_length=120)
    latitude: float | None = None
    longitude: float | None = None
    category: Literal["culture", "music", "sport", "exhibition", "other"]
    significance: Literal["experience", "plan_ahead"]
    source: EventSource

    @model_validator(mode="after")
    def valid_fact(self) -> "SourceFact":
        if self.end_date < self.start_date:
            raise ValueError("invalid_source_date_order")
        if (self.latitude is None) != (self.longitude is None):
            raise ValueError("incomplete_source_coordinates")
        if self.latitude is not None and (not math.isfinite(self.latitude) or not math.isfinite(self.longitude)
                                           or not -90 <= self.latitude <= 90 or not -180 <= self.longitude <= 180):
            raise ValueError("invalid_source_coordinates")
        return self


class _PlainText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def plain_text(value: Any, limit: int) -> str:
    if not isinstance(value, str) or len(value) > 20_000:
        return ""
    parser = _PlainText()
    parser.feed(html.unescape(value))
    text = " ".join(" ".join(parser.parts).split())
    # Strip invisible/control characters, retaining ordinary readable text only.
    text = "".join(char for char in text if unicodedata.category(char)[0] != "C")
    return text[:limit]


class _StructuredData(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.blocks: list[str] = []
        self.current: list[str] | None = None
        self.size = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "script" and len(self.blocks) < 40:
            content_type = dict(attrs).get("type", "") or ""
            if content_type.casefold().split(";", 1)[0].strip() == "application/ld+json":
                self.current = []
                self.size = 0

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self.current is not None:
            if self.size <= 256_000:
                self.blocks.append("".join(self.current))
            self.current = None

    def handle_data(self, data: str) -> None:
        if self.current is not None:
            self.size += len(data)
            if self.size <= 256_000:
                self.current.append(data)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("ambiguous_duplicate_json_key")
        value[key] = item
    return value


def _local_date(value: Any, zone: str) -> date:
    if not isinstance(value, str) or len(value) > 80:
        raise ValueError("explicit_event_date_required")
    value = html.unescape(value).strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return date.fromisoformat(value)
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d{1,9})?)?(?:Z|[+-]\d{2}:\d{2})?", value):
        raise ValueError("invalid_event_timestamp")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    # Unzoned source timestamps are already publisher-local. Zoned timestamps use
    # the reviewed source's country timezone, preserving the actual local event day.
    return parsed.astimezone(ZoneInfo(zone)).date() if parsed.tzinfo else parsed.date()


def _type(value: Any) -> str:
    return value.rsplit("/", 1)[-1].rsplit("#", 1)[-1] if isinstance(value, str) else ""


def _schema_terms(value: Any, depth: int = 0) -> set[str] | None:
    """Normalize bounded JSON-LD enum/type references without trusting unknown shapes."""
    if depth > 2:
        return None
    if isinstance(value, str) and len(value) <= 300:
        term = _type(value)
        return {term} if re.fullmatch(r"[A-Za-z][A-Za-z0-9]*", term) else None
    if isinstance(value, dict) and isinstance(value.get("@id"), str):
        return _schema_terms(value["@id"], depth + 1)
    if isinstance(value, list) and 1 <= len(value) <= 8:
        result: set[str] = set()
        for item in value:
            terms = _schema_terms(item, depth + 1)
            if not terms:
                return None
            result.update(terms)
        return result
    return None


def _country(value: Any) -> str | None:
    if isinstance(value, dict):
        value = value.get("name") or value.get("identifier")
    if not isinstance(value, str) or not value.strip():
        return None
    result = plain_text(value, 120)
    for code, aliases in COUNTRIES.items():
        if normalized(result) in tuple(map(normalized, aliases)):
            return code
    return result.upper()  # An unknown explicit country cannot accidentally match.


def _classification(types: set[str], title: str) -> tuple[str, str]:
    text = normalized(title)
    if "SportsEvent" in types:
        return "sport", "plan_ahead"
    if "MusicEvent" in types:
        return "music", "experience"
    if "ExhibitionEvent" in types:
        return "exhibition", "experience"
    if "Festival" in types:
        return "culture", "plan_ahead"
    if types.intersection({"TheaterEvent", "DanceEvent", "VisualArtsEvent", "ScreeningEvent", "LiteraryEvent", "ComedyEvent"}):
        return "culture", "experience"
    if re.search(r"\b(marathon|regatta|yacht|championship|grand prix|races|марафон|регата|чемпионат)\b", text):
        return "sport", "plan_ahead"
    if re.search(r"\b(concert|symphony|orchestra|philharmonic|chamber music|концерт|оркестр)\b", text):
        return "music", "experience"
    if re.search(r"\b(exhibition|выставка)\b", text):
        return "exhibition", "experience"
    if re.search(r"\b(festival|фестиваль)\b", text):
        return "culture", "plan_ahead"
    return "other", "experience"


def _fact(row: dict[str, Any], fetched_url: str, retrieved_at: str) -> SourceFact | None:
    host = urlsplit(fetched_url).hostname
    provider, _, zone = SOURCE_HOSTS[host]
    types = _schema_terms(row.get("@type"))
    if not types or not types.intersection(EVENT_TYPES):
        return None
    if "eventStatus" in row:
        statuses = _schema_terms(row["eventStatus"])
        if statuses != {"EventScheduled"}:
            return None
    if "eventAttendanceMode" in row:
        modes = _schema_terms(row["eventAttendanceMode"])
        # Hybrid events remain eligible only with the physical venue checks below.
        # Any Online-only marker or unknown supplied enum fails conservatively.
        if not modes or not modes.issubset({"OfflineEventAttendanceMode", "MixedEventAttendanceMode"}):
            return None
    title = plain_text(row.get("name"), 240)
    rejection = normalized(title + " " + plain_text(row.get("description", ""), 3000))
    if not title or re.search(r"\b(cancelled|canceled|postponed|отменен|отменено|отменена|перенесен|перенесено|перенесена)\b|\bonline only\b|только онлайн", rejection):
        return None
    location = row.get("location")
    if isinstance(location, list):
        location = location[0] if len(location) == 1 else None
    if not isinstance(location, dict):
        return None
    if "@type" in location:
        location_types = _schema_terms(location["@type"])
        if not location_types or "VirtualLocation" in location_types:
            return None
    venue = plain_text(location.get("name"), 240)
    address = location.get("address")
    address = address if isinstance(address, dict) else {}
    city = plain_text(address.get("addressLocality"), 180)
    country = _country(address.get("addressCountry"))
    coordinates = location.get("geo")
    lat = lon = None
    try:
        if coordinates is not None:
            if not isinstance(coordinates, dict) or isinstance(coordinates.get("latitude"), bool) or isinstance(coordinates.get("longitude"), bool):
                return None
            lat, lon = float(coordinates["latitude"]), float(coordinates["longitude"])
        start = _local_date(row.get("startDate"), zone)
        end = _local_date(row["endDate"], zone) if row.get("endDate") is not None else start
        if not venue or not city and lat is None:
            return None
        category, significance = _classification(types, title)
        return SourceFact(title=title, start_date=start, end_date=end, venue=venue, city=city, country=country,
                          latitude=lat, longitude=lon, category=category, significance=significance,
                          source=EventSource(provider=provider, source_url=fetched_url, retrieved_at=retrieved_at, freshness="live"))
    except (KeyError, ValueError, TypeError, OverflowError):
        return None


def parse_source_events(document: str, fetched_url: str, retrieved_at: str) -> list[SourceFact]:
    if source_url(fetched_url) is None or len(document.encode("utf-8")) > MAX_HTML_BYTES:
        return []
    parser = _StructuredData()
    try:
        parser.feed(document)
    except (ValueError, RecursionError):
        return []
    facts: list[SourceFact] = []
    for block in parser.blocks:
        try:
            value = json.loads(block, object_pairs_hook=_unique_object,
                               parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite_json")))
        except (ValueError, RecursionError):
            continue
        stack, seen = [(value, 0)], 0
        while stack and seen < 2500 and len(facts) < 120:
            row, depth = stack.pop()
            seen += 1
            if depth > 16:
                continue
            if isinstance(row, dict):
                fact = _fact(row, fetched_url, retrieved_at)
                if fact is not None:
                    facts.append(fact)
                stack.extend((item, depth + 1) for item in row.values() if isinstance(item, (dict, list)))
            elif isinstance(row, list):
                stack.extend((item, depth + 1) for item in row[:2500] if isinstance(item, (dict, list)))
    return facts


def city_aliases(place: Any) -> set[str]:
    names = {normalized(str(place.name)), *(normalized(str(item)) for item in getattr(place, "aliases", ()))}
    country_names = {normalized(str(place.country)), normalized(str(place.country_code))}
    for aliases in COUNTRIES.values():
        country_names.update(map(normalized, aliases))
    names.difference_update(country_names)
    airport = normalized(str(getattr(place, "airport", "") or ""))
    names.discard(airport)
    names.discard("")
    for group in CITY_ALIASES.values():
        if names.intersection(map(normalized, group)):
            names.update(map(normalized, group))
    return names


def _city_matches(city: str, aliases: set[str]) -> bool:
    city = normalized(city)
    return city in aliases or any(re.fullmatch(re.escape(alias) + r"\s+\d{1,2}", city) for alias in aliases)


def eligible_event(fact: SourceFact, place: Any, start: date, end: date) -> RegionalEvent | None:
    if fact.end_date < start or fact.start_date > end or end < start:
        return None
    country_code = str(place.country_code).upper()
    if fact.country is not None and fact.country != country_code:
        return None
    exact_city = _city_matches(fact.city, city_aliases(place))
    nearby = False
    if fact.latitude is not None:
        try:
            lat1, lat2 = math.radians(float(place.latitude)), math.radians(fact.latitude)
            dlat, dlon = lat2 - lat1, math.radians(fact.longitude - float(place.longitude))
            km = 6371 * 2 * math.asin(min(1, math.sqrt(math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2)))
            if not math.isfinite(km) or km > 50:
                return None
            nearby = fact.country == country_code
        except (ValueError, TypeError, OverflowError):
            return None
    if not exact_city and not nearby:
        return None
    # With no explicit country, a matching reviewed national source plus exact city
    # is required; country text alone still cannot establish event location.
    source_country = SOURCE_HOSTS[urlsplit(fact.source.source_url).hostname][1]
    if fact.country is None and (not exact_city or source_country != country_code):
        return None
    region = "Событие в выбранном городе" if exact_city else "Событие рядом с направлением (до 50 км по координатам источника)"
    reason = region + ", даты источника пересекаются с поездкой. "
    if fact.significance == "plan_ahead":
        reason += "Стоит учесть при планировании: заранее проверьте программу и транспорт у организатора; изменения движения не подтверждены. "
    reason += "Билеты и возможность посещения не проверены."
    identity = canonical_url(fact.source.source_url) + "|" + "|".join((normalized(fact.title), str(fact.start_date), normalized(fact.venue)))
    return RegionalEvent(id="event-" + hashlib.sha256(identity.encode()).hexdigest()[:20], title=fact.title,
                         start_date=fact.start_date, end_date=fact.end_date, location_name=fact.venue,
                         location_city=fact.city or "Рядом с: " + str(place.name) + " (по координатам)",
                         category=fact.category, significance=fact.significance, reason=reason, source=fact.source)


def select_events(events: list[RegionalEvent], interests: list[str] | tuple[str, ...] = ()) -> list[RegionalEvent]:
    terms = {normalized(value) for value in interests if isinstance(value, str) and 1 < len(value) <= 120}
    ranked = sorted(events, key=lambda event: (event.significance != "plan_ahead",
                    -sum(term in normalized(event.title + " " + event.category) for term in terms), event.start_date, normalized(event.title)))
    selected, urls, facts = [], set(), set()
    for event in ranked:
        url = canonical_url(event.source.source_url)
        identity = (normalized(event.title), event.start_date, event.end_date, normalized(event.location_name))
        if url in urls or identity in facts:
            continue
        selected.append(event)
        urls.add(url)
        facts.add(identity)
        if len(selected) == MAX_RESULTS:
            break
    return selected
