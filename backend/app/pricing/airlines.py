"""Keyless published airline fares; observations are never seat-inventory quotes.

Public route pages and Fare Finder are read without cookies, browser impersonation
or JavaScript execution. A shared per-Store budget bounds all adapter instances.
"""
from __future__ import annotations

import asyncio
from collections import deque
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re
import threading
import time
from typing import Any
from weakref import WeakKeyDictionary

import httpx
from one_request_telemetry import queued, set_attributes, span, traced

from ..domain.travel_data import SourceCacheProtocol


AIRPORT_SLUGS = {
    "ATH": "athens", "LCA": "larnaca", "PFO": "paphos", "IST": "istanbul",
    "TBS": "tbilisi", "WAW": "warsaw", "WMI": "warsaw-modlin", "BER": "berlin",
    "LIS": "lisbon", "VIE": "vienna", "FCO": "rome", "PRG": "prague",
    "DXB": "dubai", "BCN": "barcelona", "CDG": "paris", "FRA": "frankfurt",
    "BUD": "budapest", "LHR": "london", "MXP": "milan", "SOF": "sofia",
}
CACHE_TTL = 600
MAX_SOURCE_AGE = timedelta(hours=48)
MAX_BYTES = 3_000_000
HTTP_TIMEOUT = 12.0
SEARCH_TIMEOUT = 20.0
REQUESTS_PER_MINUTE = 6
AEGEAN = "aegean_public"
RYANAIR = "ryanair_farefinder"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _stamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _parse_time(value: Any, now: datetime, *, milliseconds: bool = False) -> datetime | None:
    try:
        if milliseconds:
            if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
                return None
            result = datetime.fromtimestamp(float(value) / 1000, timezone.utc)
        else:
            if not isinstance(value, str):
                return None
            result = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if result.tzinfo is None:
                return None
            result = result.astimezone(timezone.utc)
        if result > now + timedelta(minutes=5) or now - result >= MAX_SOURCE_AGE:
            return None
        return result
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def _money(value: Any) -> Decimal | None:
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        return None
    try:
        result = Decimal(str(value))
        return result if result.is_finite() and 0 < result <= 1_000_000 else None
    except InvalidOperation:
        return None


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def _diagnostic(provider: str, outcome: str, code: str, message: str, **fields) -> dict:
    return {"provider_id": provider, "outcome": outcome, "diagnostic_code": code, "message": message,
            "delivery": "not_attempted", "dispatched": False, "checked_at": _stamp(_now()),
            "retrieved_at": None, **fields}


def _finished(diagnostic: dict, **changes) -> dict:
    diagnostic.update(changes)
    result = diagnostic
    set_attributes(provider=result["provider_id"], outcome=result["outcome"], reason=result["diagnostic_code"])
    return result


@dataclass
class _Budget:
    slots: asyncio.Semaphore = field(default_factory=lambda: asyncio.Semaphore(2))
    dispatches: deque = field(default_factory=deque)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def reserve(self) -> bool:
        now = time.monotonic()
        with self.lock:
            while self.dispatches and self.dispatches[0] <= now - 60:
                self.dispatches.popleft()
            if len(self.dispatches) >= REQUESTS_PER_MINUTE:
                return False
            self.dispatches.append(now)
            return True


_budgets: WeakKeyDictionary = WeakKeyDictionary()
_budget_lock = threading.Lock()


class PublicAirlinePrices:
    def __init__(self, client: httpx.AsyncClient, store: SourceCacheProtocol):
        self.client, self.store = client, store
        with _budget_lock:
            if store not in _budgets:
                _budgets[store] = _Budget()
            self.budget = _budgets[store]

    async def _get(self, provider: str, url: str, params: dict | None = None, *, journal: list | None = None) -> tuple[str | None, dict]:
        diagnostic = _diagnostic(provider, "no_offers", "no_offers", "Источник не вернул подходящий тариф.",
                                 source_url=str(httpx.URL(url, params=params)))
        if journal is not None:
            journal.append(diagnostic)
        try:
            async with asyncio.timeout(HTTP_TIMEOUT), queued(self.budget.slots, "airline.queue"):
                if not self.budget.reserve():
                    return None, _finished(diagnostic, outcome="rate_limited", diagnostic_code="local_rate_limit",
                        message="Лимит открытых авиаисточников: 6 запросов в минуту; повторите позже.")
                diagnostic.update(delivery="live_fetch", dispatched=True)
                with span("airline.http", kind="client", attributes={"provider": provider, "http_method": "GET"}):
                    self.store.increment("public_airline_requests")
                    self.store.increment("public_airline_" + (provider if provider in (AEGEAN, RYANAIR) else "other") + "_requests")
                    try:
                        async with self.client.stream("GET", url, params=params,
                                timeout=httpx.Timeout(HTTP_TIMEOUT, connect=6), follow_redirects=False) as response:
                            diagnostic["retrieved_at"] = _stamp(_now())
                            diagnostic["http_status"] = response.status_code
                            set_attributes(http_status_code=response.status_code)
                            if response.status_code != 200:
                                outcome, code = (("blocked", "http_blocked") if response.status_code in (401, 403, 405)
                                    else ("rate_limited", "http_rate_limited") if response.status_code == 429
                                    else ("context_mismatch", "http_redirect") if 300 <= response.status_code < 400
                                    else ("unavailable", "http_error"))
                                return None, _finished(diagnostic, outcome=outcome, diagnostic_code=code,
                                    message="Открытый авиаисточник временно недоступен или ограничил доступ.")
                            data = bytearray()
                            async for chunk in response.aiter_bytes():
                                data.extend(chunk)
                                if len(data) > MAX_BYTES:
                                    return None, _finished(diagnostic, outcome="parse_error", diagnostic_code="response_too_large",
                                        message="Ответ авиаисточника превысил допустимый размер.")
                            set_attributes(outcome="ok", reason="http_ok")
                            return data.decode("utf-8", errors="replace"), diagnostic
                    except asyncio.CancelledError:
                        set_attributes(reason="request_cancelled")
                        raise
                    except (httpx.TimeoutException, TimeoutError):
                        set_attributes(outcome="timeout", reason="provider_timeout")
                        raise
                    except httpx.HTTPError:
                        set_attributes(outcome="unavailable", reason="network_error")
                        raise
        except asyncio.CancelledError:
            _finished(diagnostic, outcome="timeout", diagnostic_code="request_cancelled",
                      message="Проверка авиаисточника отменена.")
            raise
        except (httpx.TimeoutException, TimeoutError):
            return None, _finished(diagnostic, outcome="timeout",
                diagnostic_code="provider_timeout" if diagnostic["dispatched"] else "queue_deadline",
                message="Авиаисточник не ответил вовремя.")
        except httpx.HTTPError:
            return None, _finished(diagnostic, outcome="unavailable", diagnostic_code="network_error",
                                  message="Не удалось загрузить публичный авиаисточник.")

    @staticmethod
    def _leg(provider: str, source: str, origin: str, destination: str, departure: str,
             value: Decimal, currency: str, observed: datetime, retrieved: str,
             identity: Any, **extra: Any) -> dict:
        observation_id = provider + ":" + _digest([source, identity, value, currency, _stamp(observed)])
        return dict(provider_id=provider, source_url=source, origin_iata=origin,
                    destination_iata=destination, date=departure, amount=str(value), currency=currency,
                    observed_at=_stamp(observed), retrieved_at=retrieved,
                    source_observation_id=observation_id, **extra)

    @traced("airline.lookup")
    async def _aegean(self, origin: str, destination: str, departure: str, currency: str, *, journal: list | None = None) -> tuple[dict | None, dict]:
        provider = AEGEAN
        set_attributes(provider=provider)
        if origin not in AIRPORT_SLUGS or destination not in AIRPORT_SLUGS:
            return None, _finished(_diagnostic(provider, "unsupported_route", "unsupported_route",
                                              "Нет проверенного шаблона страницы для этого аэропорта."))
        url = f"https://flights.aegeanair.com/en/flights-from-{AIRPORT_SLUGS[origin]}-to-{AIRPORT_SLUGS[destination]}"
        body, diagnostic = await self._get(provider, url, journal=journal)
        if body is None:
            return None, _finished(diagnostic)
        diagnostic["diagnostic_code"] = "no_matching_date_fare"
        try:
            script = re.search(r'<script\b[^>]*\bid=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>', body, re.S)
            if not script:
                raise ValueError("Missing structured page data")
            data = json.loads(script.group(1), parse_float=Decimal)
            data = data["props"]["pageProps"]["apolloState"]["data"]
            if not isinstance(data, dict):
                raise ValueError("Malformed data")
            rows = []
            histogram_found = False
            for key, item in data.items():
                if "fare-daily-histogram" not in key or not isinstance(item, dict):
                    continue
                histogram_found = True
                state = item.get("cmpConfig", {}).get("settings", {}).get("initialState", {})
                if not isinstance(state, dict) or not isinstance(state.get("dailyHistogram", []), list):
                    raise ValueError("Malformed histogram")
                rows.extend(state.get("dailyHistogram", [])[:100])
            if not histogram_found:
                raise ValueError("Missing fare histogram")
            eligible = []
            rejected = False
            for row in rows:
                if not isinstance(row, dict) or row.get("emptyFare"):
                    continue
                leg, price = row.get("outboundFlight", {}), row.get("priceSpecification", {})
                if not isinstance(leg, dict) or not isinstance(price, dict):
                    rejected = True
                    continue
                if row.get("departureDate") != departure or row.get("journeyType") != "ONE_WAY":
                    continue
                if (leg.get("departureAirportIataCode") != origin
                        or leg.get("arrivalAirportIataCode") != destination):
                    diagnostic.update(outcome="context_mismatch", diagnostic_code="route_mismatch", message="Маршрут опубликованного тарифа отличается от запроса.")
                    continue
                value, observed = _money(price.get("totalPrice")), _parse_time(row.get("searchDate"), _now())
                if not value:
                    continue
                if price.get("currencyCode") != currency:
                    diagnostic.update(outcome="currency_mismatch", diagnostic_code="currency_mismatch", message="Валюта опубликованного тарифа отличается от валюты запроса.")
                    continue
                if observed is None:
                    diagnostic.update(outcome="stale_observation", diagnostic_code="stale_observation", message="Время исходного тарифа неизвестно или тариф старше 48 часов.")
                    continue
                eligible.append(self._leg(provider, url, origin, destination, departure, value, currency,
                    observed, diagnostic["retrieved_at"], row.get("datacoreId") or row.get("searchDate"),
                    fare_class=leg.get("fareClassInput")))
            if eligible:
                return min(eligible, key=lambda item: Decimal(item["amount"])), _finished(diagnostic, outcome="observed",
                    diagnostic_code="observed", message="Найден опубликованный тариф на точную дату.")
            if rejected and diagnostic["outcome"] == "no_offers":
                diagnostic.update(outcome="parse_error", diagnostic_code="source_parse_error", message="Некорректный формат опубликованных тарифов.")
            return None, _finished(diagnostic)
        except (ValueError, TypeError, KeyError, AttributeError):
            return None, _finished(diagnostic, outcome="parse_error", diagnostic_code="source_parse_error",
                                  message="Не удалось прочитать структурированные тарифы на странице авиакомпании.")

    def _fact(self, provider: str, legs: list[dict], *, roundtrip: bool, amount: Decimal | None = None) -> dict:
        observed = min(datetime.fromisoformat(leg["observed_at"]) for leg in legs)
        retrieved = max(datetime.fromisoformat(leg["retrieved_at"]) for leg in legs)
        expires = min(retrieved + timedelta(seconds=CACHE_TTL), observed + MAX_SOURCE_AGE)
        trip_type = "roundtrip" if roundtrip else "one_way"
        value = amount if amount is not None else sum((Decimal(leg["amount"]) for leg in legs), Decimal(0))
        message = ("Сумма двух опубликованных тарифов в одну сторону на одного взрослого; это не единый билет туда-обратно."
                   if roundtrip and provider == AEGEAN else
                   "Опубликованный базовый тариф туда-обратно на одного взрослого."
                   if roundtrip else "Опубликованный базовый тариф в одну сторону; обратный перелёт не включён.")
        scope = {"origin_iata": legs[0]["origin_iata"], "destination_iata": legs[0]["destination_iata"],
                 "date_from": legs[0]["date"], "date_to": legs[-1]["date"] if roundtrip else None,
                 "trip_type": trip_type, "party_verified": False, "traveler_basis": "published_single_adult_baseline"}
        return dict(provider_id=provider, source_url=legs[0]["source_url"],
                    source_urls=list(dict.fromkeys(leg["source_url"] for leg in legs)),
                    observed_at=_stamp(observed), retrieved_at=_stamp(retrieved), expires_at=_stamp(expires),
                    amount=str(value), currency=legs[0]["currency"], trip_type=trip_type, unit="person_trip",
                    scope=scope, scope_verified_fields=["origin_iata", "destination_iata", "date_from", "trip_type", "currency"] + (["date_to"] if roundtrip else []),
                    source_observation_ids=[leg["source_observation_id"] for leg in legs],
                    inclusions=["Опубликованный базовый авиатариф"],
                    exclusions=["Багаж сверх базового тарифа", "Выбор мест и дополнительные услуги", "Трансфер до/из аэропорта"],
                    taxes="unknown", availability_verified=False, party_verified=False,
                    outcome="observed", message=message, legs=legs, delivery="live_fetch")

    @traced("airline.lookup")
    async def _ryanair(self, origin: str, destination: str, departure: str, returning: str, currency: str, *, journal: list | None = None) -> tuple[dict | None, dict]:
        set_attributes(provider=RYANAIR)
        duration = (date.fromisoformat(returning) - date.fromisoformat(departure)).days
        params = dict(departureAirportIataCode=origin, arrivalAirportIataCode=destination,
            outboundDepartureDateFrom=departure, outboundDepartureDateTo=departure,
            inboundDepartureDateFrom=returning, inboundDepartureDateTo=returning,
            durationFrom=duration, durationTo=duration, language="en", limit=3, market="en-gb", priceValueTo=1000000)
        url = "https://www.ryanair.com/api/farfnd/v4/roundTripFares"
        body, diagnostic = await self._get(RYANAIR, url, params, journal=journal)
        if body is None:
            return None, _finished(diagnostic)
        source = diagnostic["source_url"]
        try:
            payload = json.loads(body, parse_float=Decimal)
            if not isinstance(payload, dict) or not isinstance(payload.get("fares"), list):
                raise ValueError("Malformed fares")
            facts = []
            for row in payload["fares"][:3]:
                if not isinstance(row, dict):
                    continue
                summary = row.get("summary", {})
                if not isinstance(summary, dict) or not isinstance(summary.get("price"), dict):
                    continue
                price = summary["price"]
                total = _money(price.get("value"))
                if not total:
                    continue
                if price.get("currencyCode") != currency:
                    diagnostic.update(outcome="currency_mismatch", diagnostic_code="currency_mismatch", message="Валюта авиакомпании отличается; конвертация не выполняется.")
                    continue
                legs = []
                for name, a, b, day in [("outbound", origin, destination, departure), ("inbound", destination, origin, returning)]:
                    leg = row.get(name, {})
                    if not isinstance(leg, dict):
                        diagnostic.update(outcome="parse_error", diagnostic_code="source_parse_error",
                                          message="Некорректный формат плеча Fare Finder.")
                        break
                    value = _money(leg.get("price", {}).get("value")) if isinstance(leg.get("price"), dict) else None
                    observed = _parse_time(leg.get("priceUpdated"), _now(), milliseconds=True)
                    dep, arr = leg.get("departureAirport", {}), leg.get("arrivalAirport", {})
                    if not isinstance(dep, dict) or not isinstance(arr, dict) or not value:
                        diagnostic.update(outcome="parse_error", diagnostic_code="source_parse_error",
                                          message="Некорректные аэропорты или сумма плеча Fare Finder.")
                        break
                    if dep.get("iataCode") != a or arr.get("iataCode") != b:
                        diagnostic.update(outcome="context_mismatch", diagnostic_code="route_mismatch",
                                          message="Маршрут опубликованного тарифа отличается от запроса.")
                        break
                    if not isinstance(leg.get("departureDate"), str) or leg["departureDate"][:10] != day:
                        diagnostic.update(outcome="context_mismatch", diagnostic_code="no_matching_date_fare",
                                          message="Дата опубликованного тарифа отличается от запроса.")
                        break
                    if observed is None:
                        diagnostic.update(outcome="stale_observation", diagnostic_code="stale_observation",
                                          message="Время исходного тарифа неизвестно или тариф старше 48 часов.")
                        break
                    if leg["price"].get("currencyCode") != currency:
                        diagnostic.update(outcome="currency_mismatch", diagnostic_code="currency_mismatch",
                                          message="Валюта авиакомпании отличается; конвертация не выполняется.")
                        break
                    legs.append(self._leg(RYANAIR, source, a, b, day, value, currency, observed,
                        diagnostic["retrieved_at"], leg.get("flightKey") or leg.get("flightNumber"),
                        flight_number=leg.get("flightNumber"), departure_local=leg["departureDate"]))
                if len(legs) == 2 and sum((Decimal(leg["amount"]) for leg in legs), Decimal(0)) == total:
                    facts.append(self._fact(RYANAIR, legs, roundtrip=True, amount=total))
                elif len(legs) == 2:
                    diagnostic.update(outcome="context_mismatch", diagnostic_code="fare_total_mismatch",
                                      message="Итог Fare Finder не совпадает с суммой опубликованных плеч.")
            if facts:
                return min(facts, key=lambda item: Decimal(item["amount"])), _finished(diagnostic, outcome="observed",
                    diagnostic_code="observed", message="Найден опубликованный тариф туда-обратно.")
            return None, _finished(diagnostic)
        except (ValueError, TypeError, KeyError, AttributeError):
            return None, _finished(diagnostic, outcome="parse_error", diagnostic_code="source_parse_error", message="Некорректный ответ Fare Finder.")

    @traced("airline.search")
    async def search(self, origin_iata: str, destination_iata: str, date_from: str,
                     date_to: str, currency: str) -> dict:
        result: dict[str, Any] = {"facts": [], "diagnostics": []}
        try:
            departure, returning = date.fromisoformat(str(date_from)).isoformat(), date.fromisoformat(str(date_to)).isoformat()
            if (not isinstance(origin_iata, str) or not re.fullmatch("[A-Z]{3}", origin_iata)
                    or not isinstance(destination_iata, str) or not re.fullmatch("[A-Z]{3}", destination_iata)
                    or origin_iata == destination_iata or returning < departure
                    or not isinstance(currency, str) or not re.fullmatch("[A-Z]{3}", currency)):
                raise ValueError("Invalid scope")
        except (ValueError, TypeError):
            result["diagnostics"].append(_finished(_diagnostic("public_airlines", "context_mismatch", "invalid_context",
                                                              "Некорректный маршрут, даты или валюта.")))
            return result
        key = "public-airlines-v1:" + _digest([origin_iata, destination_iata, departure, returning, currency])
        with span("airline.cache"):
            cached = self.store.cache_get(key)
            if cached and isinstance(cached[0], dict):
                data = deepcopy(cached[0])
                try:
                    valid = (isinstance(data.get("facts"), list) and isinstance(data.get("diagnostics"), list)
                             and all(isinstance(row, dict) for row in data["diagnostics"])
                             and all(datetime.fromisoformat(fact["expires_at"]) > _now() for fact in data["facts"]))
                except (KeyError, ValueError, TypeError):
                    valid = False
                if valid:
                    for fact in data.get("facts", []):
                        fact["delivery"] = "cache_reuse"
                    for diagnostic in data.get("diagnostics", []):
                        diagnostic.update(delivery="cache_reuse", dispatched=False)
                        diagnostic.setdefault("checked_at", diagnostic.get("retrieved_at"))
                        diagnostic.setdefault("diagnostic_code", diagnostic.get("outcome", "no_offers"))
                    self.store.increment("public_airline_cache_hits")
                    self.store.increment("public_airline_" + ("positive" if data["facts"] else "negative") + "_cache_hits")
                    set_attributes(cache_hit=True, outcome="cached", count=len(data["facts"]),
                                   reason="positive_cache_hit" if data["facts"] else "negative_cache_hit")
                    return data
            set_attributes(cache_hit=False, reason="cache_miss")
        journal: list[dict] = []
        try:
            async with asyncio.timeout(SEARCH_TIMEOUT):
                attempts = await asyncio.gather(self._aegean(origin_iata, destination_iata, departure, currency, journal=journal),
                                               self._aegean(destination_iata, origin_iata, returning, currency, journal=journal))
                result["diagnostics"].extend(diagnostic for _, diagnostic in attempts)
                outbound, inbound = [leg for leg, _ in attempts]
                if outbound and inbound:
                    result["facts"] = [self._fact(AEGEAN, [outbound, inbound], roundtrip=True)]
                else:
                    result["facts"] = [self._fact(AEGEAN, [leg], roundtrip=False) for leg in [outbound, inbound] if leg]
                    fallback, diagnostic = await self._ryanair(origin_iata, destination_iata, departure, returning, currency, journal=journal)
                    result["diagnostics"].append(diagnostic)
                    if fallback:
                        result["facts"] = [fallback]
        except TimeoutError:
            # Preserve actual completed/pending source attempts when the shared
            # deadline cancels gather or the fallback. The local journal belongs
            # only to this search and adds no work or independent budget.
            for diagnostic in journal:
                if diagnostic.get("diagnostic_code") == "request_cancelled":
                    diagnostic.update(diagnostic_code="source_deadline", message="Проверка авиаисточника завершена по общему лимиту времени.")
                if diagnostic not in result["diagnostics"]:
                    result["diagnostics"].append(diagnostic)
            result["diagnostics"].append(_finished(_diagnostic("public_airlines", "timeout", "search_deadline",
                                                              "Общий поиск авиатарифов завершён по лимиту времени.")))
        if result["facts"]:
            ttl = min(CACHE_TTL, max(1, int(min(datetime.fromisoformat(fact["expires_at"]) for fact in result["facts"]).timestamp() - _now().timestamp())))
            self.store.cache_put(key, result, ttl)
        elif result["diagnostics"] and all(row["outcome"] in ("no_offers", "unsupported_route") for row in result["diagnostics"]):
            self.store.cache_put(key, result, CACHE_TTL)
        return result
