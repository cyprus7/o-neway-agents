"""Read-only, keyless public fare proof; no browser, account, cookies or LLM.

This diagnostic is deliberately separate from application inventory. A successful
response proves a cached published number, not a bookable party-sized quote.
"""
from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
import json
import re
from typing import Any

import httpx


AIRPORT_SLUGS = {
    "ATH": "athens", "LCA": "larnaca", "PFO": "paphos", "IST": "istanbul",
    "TBS": "tbilisi", "WAW": "warsaw", "WMI": "warsaw-modlin", "BER": "berlin",
    "LIS": "lisbon", "VIE": "vienna", "FCO": "rome", "PRG": "prague",
}
MAX_BYTES = 3_000_000


def amount(value: Any) -> str | None:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return str(number) if number.is_finite() and number > 0 else None


def load(client: httpx.Client, url: str, params: dict | None = None) -> tuple[dict, str]:
    with client.stream("GET", url, params=params) as response:
        info = {"url": str(response.url), "status": response.status_code,
                "retrieved_at": datetime.now(timezone.utc).isoformat()}
        data = bytearray()
        for chunk in response.iter_bytes():
            data.extend(chunk)
            if len(data) > MAX_BYTES:
                return info | {"error": "response_too_large"}, ""
        return info, data.decode("utf-8", errors="replace")


def ryanair(client: httpx.Client, origin: str, destination: str, departure: str,
            returning: str | None) -> dict:
    endpoint = "roundTripFares" if returning else "oneWayFares"
    params = dict(departureAirportIataCode=origin, arrivalAirportIataCode=destination,
                  outboundDepartureDateFrom=departure, outboundDepartureDateTo=departure,
                  language="en", limit=3, market="en-gb", priceValueTo=5000)
    if returning:
        duration = (date.fromisoformat(returning) - date.fromisoformat(departure)).days
        params.update(inboundDepartureDateFrom=returning, inboundDepartureDateTo=returning,
                      durationFrom=duration, durationTo=duration)
    info, body = load(client, f"https://www.ryanair.com/api/farfnd/v4/{endpoint}", params)
    result = info | {"provider": "ryanair_farefinder", "fares": []}
    if info["status"] != 200:
        return result
    for fare in json.loads(body, parse_float=Decimal).get("fares", []):
        outbound, inbound = fare.get("outbound", {}), fare.get("inbound", {})
        if (outbound.get("departureAirport", {}).get("iataCode") != origin
                or outbound.get("arrivalAirport", {}).get("iataCode") != destination
                or not outbound.get("departureDate", "").startswith(departure)):
            continue
        if returning and (inbound.get("departureAirport", {}).get("iataCode") != destination
                          or inbound.get("arrivalAirport", {}).get("iataCode") != origin
                          or not inbound.get("departureDate", "").startswith(returning)):
            continue
        price = fare.get("summary", {}).get("price", {})
        value = amount(price.get("value"))
        if not value or not re.fullmatch("[A-Z]{3}", price.get("currencyCode", "")):
            continue
        result["fares"].append({"amount": value, "currency": price["currencyCode"],
            "departure": outbound.get("departureDate"), "return": inbound.get("departureDate"),
            "flights": [leg.get("flightNumber") for leg in [outbound, inbound] if leg],
            "price_updated_ms": [leg.get("priceUpdated") for leg in [outbound, inbound] if leg],
            "scope": "published baseline fare; party availability and ancillary fees unverified"})
    return result


def aegean(client: httpx.Client, origin: str, destination: str, departure: str) -> dict:
    url = ("https://flights.aegeanair.com/en/flights-from-"
           f"{AIRPORT_SLUGS[origin]}-to-{AIRPORT_SLUGS[destination]}")
    info, body = load(client, url)
    result = info | {"provider": "aegean_public_page", "fares": []}
    if info["status"] != 200 or info["url"].rstrip("/") != url:
        return result
    match = re.search(r'<script\b[^>]*\bid="__NEXT_DATA__"[^>]*>(.*?)</script>', body, re.S)
    if not match:
        return result
    data = json.loads(match.group(1), parse_float=Decimal).get("props", {}).get("pageProps", {}).get("apolloState", {}).get("data", {})
    for key, value in data.items():
        if "fare-daily-histogram" not in key:
            continue
        state = value.get("cmpConfig", {}).get("settings", {}).get("initialState", {})
        for fare in state.get("dailyHistogram", []):
            leg = fare.get("outboundFlight", {})
            price = fare.get("priceSpecification", {})
            number = amount(price.get("totalPrice"))
            if (fare.get("emptyFare") or not number or fare.get("departureDate") != departure
                    or leg.get("departureAirportIataCode") != origin
                    or leg.get("arrivalAirportIataCode") != destination
                    or fare.get("journeyType") != "ONE_WAY"
                    or not re.fullmatch("[A-Z]{3}", price.get("currencyCode", ""))):
                continue
            result["fares"].append({"amount": number, "currency": price["currencyCode"],
                "departure": fare["departureDate"], "journey_type": fare["journeyType"],
                "fare_class": leg.get("fareClassInput"), "search_date": fare.get("searchDate"),
                "scope": "cached published one-way fare; party availability and ancillary fees unverified"})
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("provider", choices=["ryanair", "aegean"])
    parser.add_argument("origin", choices=sorted(AIRPORT_SLUGS))
    parser.add_argument("destination", choices=sorted(AIRPORT_SLUGS))
    parser.add_argument("departure", type=lambda text: date.fromisoformat(text).isoformat())
    parser.add_argument("--return-date", type=lambda text: date.fromisoformat(text).isoformat())
    args = parser.parse_args()
    if args.return_date and args.return_date < args.departure:
        parser.error("return date precedes departure")
    with httpx.Client(timeout=httpx.Timeout(20), follow_redirects=True, max_redirects=3) as client:
        try:
            if args.provider == "ryanair":
                results = [ryanair(client, args.origin, args.destination, args.departure, args.return_date)]
            else:
                results = [aegean(client, args.origin, args.destination, args.departure)]
                if args.return_date:
                    results.append(aegean(client, args.destination, args.origin, args.return_date))
            print(json.dumps(results, ensure_ascii=True, indent=2))
        except (httpx.HTTPError, ValueError) as error:
            print(json.dumps({"error": type(error).__name__}))
            raise SystemExit(1) from None


if __name__ == "__main__":
    main()
