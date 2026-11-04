"""Credential-free published hotel tariffs; these are references, never quotes."""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from html.parser import HTMLParser
import re
from typing import Any

import httpx


SOURCES = {
    "ATH": ("Apollo Hotel Athens", "https://www.apollonhotel-athens.com/rates.php"),
    "IST": ("Saruhan Hotel Istanbul", "https://www.saruhanhotel.com/en/rates.html"),
    "SAW": ("Saruhan Hotel Istanbul", "https://www.saruhanhotel.com/en/rates.html"),
}


class _Text(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.hidden: list[str] = []
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "noscript"}:
            self.hidden.append(tag)

    def handle_endtag(self, tag: str) -> None:
        if self.hidden and tag == self.hidden[-1]:
            self.hidden.pop()

    def handle_data(self, data: str) -> None:
        if not self.hidden:
            self.parts.append(data)


def visible_text(html: str) -> str:
    parser = _Text()
    parser.feed(html)
    return " ".join(" ".join(parser.parts).split())


def _decimal(value: str) -> str:
    return str(Decimal(value))


def parse_apollo(text: str) -> dict[str, Any] | None:
    if not re.search(r"Prices\s+EURO\s*/\s*night", text, re.I):
        return None
    if not re.search(r"Single room\s+Double Room\s+Triple room", text, re.I):
        return None
    seasons = []
    for start, end in (("01/11", "31/03"), ("01/04", "31/05"), ("01/06", "31/10")):
        match = re.search(
            re.escape(start) + r"\s*-\s*" + re.escape(end)
            + r"\s+(\d+(?:\.\d{1,2})?)\s+(\d+(?:\.\d{1,2})?)\s+(\d+(?:\.\d{1,2})?)(?!\d)",
            text,
        )
        if not match:
            return None
        values = [_decimal(match[index]) for index in (1, 2, 3)]
        if any(not Decimal("0") < Decimal(value) < Decimal("10000") for value in values):
            return None
        seasons.append({"start": start, "end": end, "single": values[0], "double": values[1], "triple": values[2]})
    return {"seasons": seasons, "taxes": "unknown", "breakfast_included": "prices include breakfast" in text.casefold()}


def parse_saruhan(text: str) -> dict[str, Any] | None:
    matches = list(re.finditer(r"STANDARD DOUBLE/TWIN ROOM\s*:\s*(\d+(?:\.\d{1,2})?)-(\d+(?:\.\d{1,2})?)\s*EURO", text, re.I))
    if len(matches) != 2 or not all(value in text.casefold() for value in ("high season", "low season", "per room", "not per person")):
        return None
    ranges = [{"amount_from": _decimal(match[1]), "amount_to": _decimal(match[2])} for match in matches]
    if any(not Decimal("0") < Decimal(row["amount_from"]) <= Decimal(row["amount_to"]) < Decimal("10000") for row in ranges):
        return None
    positive_taxes = re.search(r"\bincluding\s+Turkish\s+breakfast,\s*tax\s+and\s+service\s+charges\b", text, re.I)
    negative_taxes = re.search(r"\b(?:tax(?:es)?|service charges)\b.{0,100}\b(?:not included|excluded|extra|additional|separately)\b|\b(?:not included|excluded|extra|additional|separately)\b.{0,80}\b(?:tax(?:es)?|service charges)\b", text, re.I)
    return {"high": ranges[0], "low": ranges[1], "taxes": "included" if positive_taxes and not negative_taxes else "unknown",
            "breakfast_included": "including turkish breakfast" in text.casefold()}


def _nights(date_from: str | date | None, date_to: str | date | None) -> list[date]:
    try:
        start = date_from if isinstance(date_from, date) else date.fromisoformat(str(date_from))
        end = date_to if isinstance(date_to, date) else date.fromisoformat(str(date_to))
        count = (end - start).days
        if not 1 <= count <= 30:
            return []
        return [start + timedelta(days=index) for index in range(count)]
    except (TypeError, ValueError):
        return []


def tariff_fact(destination: str, parsed: dict[str, Any], observed_at: str,
                date_from: str | date | None, date_to: str | date | None,
                adults: int, children_ages: list[int], rooms: int) -> dict[str, Any]:
    name, url = SOURCES[destination]
    fact: dict[str, Any] = {
        "status": "reference", "kind": "published_tariff_reference",
        "source_url": url, "source_name": name, "property_name": name,
        "destination_iata": destination, "observed_at": observed_at,
        "currency": "EUR", "unit": "room_per_night", "tariff_year": None,
        "availability_verified": False, "requested_dates_matched": False,
        "taxes": parsed["taxes"], "breakfast_included": parsed["breakfast_included"],
        "limitations": ["tariff_year_unknown", "availability_unknown", "special_conditions_not_verified"],
        "scope": {"requested_date_from": str(date_from) if date_from else None,
                  "requested_date_to": str(date_to) if date_to else None,
                  "requested_adults": adults, "requested_children_ages": children_ages,
                  "requested_rooms": rooms, "matched": False},
    }
    dates = _nights(date_from, date_to)
    compatible_party = rooms == 1 and not children_ages and adults in (1, 2, 3)
    if destination == "ATH":
        room_type = {1: "single", 2: "double", 3: "triple"}.get(adults, "double")
        selected = [row[room_type] for row in parsed["seasons"]]
        fact["room_type"] = room_type
        fact["published_seasons"] = parsed["seasons"]
        fact["amount_from"] = str(min(map(Decimal, selected)))
        fact["amount_to"] = str(max(map(Decimal, selected)))
        daily = []
        for day in dates:
            month = day.month
            index = 0 if month in (11, 12, 1, 2, 3) else 1 if month in (4, 5) else 2
            value = Decimal(parsed["seasons"][index][room_type])
            daily.append((value, value))
    else:
        fact["room_type"] = "double_or_twin"
        fact["published_seasons"] = {"high": parsed["high"], "low": parsed["low"]}
        fact["amount_from"] = str(min(Decimal(parsed[key]["amount_from"]) for key in ("high", "low")))
        fact["amount_to"] = str(max(Decimal(parsed[key]["amount_to"]) for key in ("high", "low")))
        compatible_party = compatible_party and adults in (1, 2)
        daily = []
        for day in dates:
            # Published season boundaries are vague. March/November and the
            # unspecified New Year period cannot be assigned to the cheap tier.
            if day.month in (3, 11, 12, 1):
                daily = []
                fact["limitations"].append("season_boundary_or_new_year_unknown")
                break
            band = parsed["low"] if day.month == 2 else parsed["high"]
            daily.append((Decimal(band["amount_from"]), Decimal(band["amount_to"])))
    if dates and daily and compatible_party:
        fact.update({"nights": len(dates), "reference_total_from": str(sum(row[0] for row in daily)),
                     "reference_total_to": str(sum(row[1] for row in daily)),
                     "projection_kind": "published_tariff_projection_not_quote"})
        fact["amount_from"] = str(min(row[0] for row in daily))
        fact["amount_to"] = str(max(row[1] for row in daily))
    elif not compatible_party:
        fact["limitations"].append("party_not_covered_by_published_tariff")
    return fact


class PublicHotelPrices:
    def __init__(self, client: httpx.AsyncClient, store: Any):
        self.client, self.store = client, store

    async def search(self, destination_iata: str, date_from: str | date | None,
                     date_to: str | date | None, adults: int = 1,
                     children_ages: list[int] | None = None, rooms: int = 1,
                     currency: str = "EUR") -> dict[str, Any]:
        destination = str(destination_iata).upper()
        unknown: dict[str, Any] = {"status": "unknown", "kind": "published_tariff_reference",
                                   "availability_verified": False, "reason": "unsupported_destination"}
        if destination not in SOURCES:
            return unknown
        if currency != "EUR":
            return {**unknown, "reason": "unsupported_currency"}
        _, url = SOURCES[destination]
        key = "public-hotel-tariff:v2:" + ("IST" if destination == "SAW" else destination)
        cached = self.store.cache_get(key) if self.store else None
        if cached:
            payload = cached[0]
            parsed, observed_at = payload["tariff"], payload["observed_at"]
            mode = "cached"
        else:
            try:
                async with asyncio.timeout(9):
                    async with self.client.stream("GET", url, timeout=8, follow_redirects=False) as response:
                        if response.status_code != 200:
                            return {**unknown, "reason": f"http_{response.status_code}", "source_url": url}
                        data = bytearray()
                        async for chunk in response.aiter_bytes():
                            data.extend(chunk)
                            if len(data) > 300_000:
                                return {**unknown, "reason": "response_too_large", "source_url": url}
                        text = visible_text(data.decode(response.encoding or "utf-8", errors="replace"))
                parsed = parse_apollo(text) if destination == "ATH" else parse_saruhan(text)
                if not parsed:
                    return {**unknown, "reason": "tariff_not_found", "source_url": url}
                observed_at = datetime.now(timezone.utc).isoformat()
                if self.store:
                    self.store.cache_put(key, {"tariff": parsed, "observed_at": observed_at}, 6 * 3600)
                mode = "live"
            except (httpx.HTTPError, TimeoutError, LookupError):
                return {**unknown, "reason": "network_or_timeout", "source_url": url}
        fact = tariff_fact(destination, parsed, observed_at, date_from, date_to, adults, children_ages or [], rooms)
        fact["delivery"] = mode
        return fact
