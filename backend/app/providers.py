"""Real, bounded HTTP adapters. Search results are evidence, never live inventory."""
import asyncio
import hashlib
import json
import os
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any
from urllib.parse import unquote, urlencode, urlparse

import httpx
from one_request_telemetry import queued, set_attributes, span, traced

from .geography import COUNTRY_QUALIFIERS, Place, distance_km, find_place, normalized
from .models import Evidence, TravelComponent, TravelIntent, local_today, utcnow
from .retrieval import LocalRanker, lodging_query
from .pricing.providers import KeylessPricing, lodging_discovery_terms, ostrovok_property_target
from .pricing.documents import PriceDocumentResearch
from .search import FederatedSearch, SearchHit, fair_merge
from .search.domain import url_identity
from .domain.travel_data import ComponentBundle, SourceCacheProtocol, candidate_identity, InventoryCandidatesInput, PlaceRef, DataIntent, transport_mode
from .regional_events import EventResult, RegionalEventDiscovery
from .catalog import LocalHotelCatalog


def public_url(value: Any) -> str | None:
    if not isinstance(value, str) or len(value) > 3000:
        return None
    try:
        parsed = urlparse(value)
        if parsed.scheme in ("https", "http") and parsed.hostname and not parsed.username and not parsed.password:
            return value
    except ValueError:
        pass
    return None


def timestamp(value: float) -> str:
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


def clean_payload(value: Any) -> Any:
    """Provider metadata can repeat API keys; do not persist those fields."""
    if isinstance(value, dict):
        return {key: clean_payload(item) for key, item in value.items() if key.casefold() not in ("api_key", "authorization", "search_metadata", "search_parameters")}
    if isinstance(value, list):
        return [clean_payload(item) for item in value]
    return value


def city_mentions(text: str, place: Place, *, airport: bool = False) -> bool:
    """Conservative textual relevance, never verification of an address or inventory.

    Catalogue aliases after the first two may denote a whole country/region. A Cyprus
    mention cannot qualify a Paphos hotel as evidence about Larnaca.
    """
    names = [place.name, *place.aliases[:2]]
    if airport and place.airport:
        names.append(place.airport)
    value = normalized(unquote(text)).replace('_', ' ')
    return any(re.search(r'(?<!\w)' + re.escape(normalized(name)) + r'(?!\w)', value)
               for name in names if name and len(name) > 2)


def lodging_result_matches(row: dict[str, Any], place: Place, origin: str | None) -> bool:
    title_url = str(row.get('title', '')) + ' ' + str(row.get('url', ''))
    concrete = indexed_hotel_url(str(row.get('url', '')))
    # A destination name is insufficient: transport/tour pages can name many
    # hotels while remaining unrelated to accommodation discovery.
    lodging_page = re.search(r'\b(?:hotels?|hostels?|apartments?|accommodation|lodging)\b|отел|гостиниц|апартамент', title_url, re.I)
    if not concrete and not lodging_page:
        return False
    if not concrete and re.search(r'/(?:tours?|programa|programmes?|packages?|routes?|bus|train|flights?)/|\b(?:package tour|tour with hotels)\b', unquote(title_url), re.I):
        return False
    # An event can be optional trip context, but is not a lodging-policy source.
    event_page = re.search(r'\b(?:events?|festivals?|concerts?|marathons?|regattas?)\b|фестивал|концерт|марафон|регат', title_url, re.I)
    if event_page and not lodging_page:
        return False
    # A comparison snippet can mention many cities. It must not legitimize a
    # clearly different hotel's title/URL as a source for this destination.
    try:
        parsed = urlparse(str(row.get('url', '')))
    except ValueError:
        return False
    country_path = re.match(r'^/hotel/([a-z]{2})/', parsed.path, re.I)
    booking_country = bool(concrete and parsed.hostname
                           and (parsed.hostname == 'booking.com' or parsed.hostname.endswith('.booking.com'))
                           and country_path and country_path[1].upper() == place.country_code.upper())
    if not city_mentions(title_url, place) and not (place.scope == "country" and booking_country):
        return False
    departure = find_place(origin) if origin else None
    if departure and normalized(departure.name) != normalized(place.name) and city_mentions(title_url, departure):
        return False
    # Booking's country path is useful for rejection, not proof of the city/address.
    try:
        parsed = urlparse(str(row.get('url', '')))
        if parsed.hostname and (parsed.hostname == 'booking.com' or parsed.hostname.endswith('.booking.com')):
            country = re.match(r'^/hotel/([a-z]{2})/', parsed.path, re.I)
            if country and place.country_code and country[1].upper() != place.country_code.upper():
                return False
        if parsed.hostname in ('ostrovok.ru', 'www.ostrovok.ru'):
            if not indexed_hotel_url(str(row.get('url', ''))):
                return False
            # Reject an explicitly different city in the property's structured path,
            # even when its title/snippet happens to mention this destination.
            location = parsed.path.split('/')[2 if place.scope == "country" else 3].replace('_', ' ').replace('-', ' ')
            if not city_mentions(location, place):
                return False
    except ValueError:
        return False
    return True


def indexed_hotel_url(url: str) -> bool:
    """Concrete hotel cards only; provider search/login/arbitrary subdomains are excluded."""
    if not public_url(url):
        return False
    parsed = urlparse(url)
    if parsed.hostname in ('ostrovok.ru', 'www.ostrovok.ru'):
        return bool(re.fullmatch(r'/hotel/[a-z0-9_-]+/[a-z0-9_-]+/mid[0-9]+/[a-z0-9_-]+/?', parsed.path, re.I))
    return bool(parsed.hostname and (parsed.hostname == 'booking.com' or parsed.hostname.endswith('.booking.com'))
                and re.fullmatch(r'/hotel/[a-z]{2}/[^/]+', parsed.path, re.I))


def lodging_candidate_matches_wishes(name: str, intent: TravelIntent) -> bool:
    """Reject an explicitly forbidden named kind only during initial discovery.

    This does not establish room quality, occupancy or a contractual policy, and
    is deliberately separate from fixed-identity price refresh.
    """
    if not re.search(r'\bhostels?\b|\bхостел\w*', name, re.I):
        return True
    exclusion = re.compile(r'\b(?:no|not|without|avoid|exclude)\s+(?:a\s+)?hostels?\b'
                           r'|\b(?:не|без|избегать|исключить)\s+хостел\w*', re.I)
    for term in lodging_discovery_terms(intent):
        for match in exclusion.finditer(term):
            # "Do not avoid hostels" / "не избегать хостелов" do not forbid
            # hostels. Preserve the wish instead of inventing that exclusion.
            if re.search(r"\b(?:not|never|don't|не)\s*$", term[:match.start()], re.I):
                continue
            return False
    return True


def scoped_searches(place: Place, intent: TravelIntent) -> list[tuple[str, str]]:
    """Keep accommodation conditions out of the origin-to-destination route query."""
    lodging = f'"{place.name}" "{place.country}" hotels {intent.date_from} {intent.date_to}'
    terms = lodging_discovery_terms(intent)
    if terms:
        lodging += ' ' + ' '.join(terms)
    mode = transport_mode(intent)
    route = f'from "{intent.origin}" to "{place.name}" {mode} tickets timetable {intent.date_from} {intent.date_to}'
    if intent.pets:
        route += ' ' + ('airline pet policy' if mode == 'flight' else f'{mode} pet policy')
    scoped_requirements = [value.split(':', 1)[1].strip()
        for value in intent.hard_constraints + intent.soft_constraints
        if value.startswith('transport_requirement:')]
    if scoped_requirements:
        route += ' ' + ' '.join(dict.fromkeys(scoped_requirements))[:400]
    return [('lodging', lodging), ('transport', route)]


class Providers:
    def __init__(self, client: httpx.AsyncClient, store: SourceCacheProtocol):
        self.client = client
        self.store = store
        self.web_fallback_enabled = os.getenv("WEB_FALLBACK_ENABLED", "1") == "1"
        self.catalog = LocalHotelCatalog()
        self.searxng_url = os.getenv("SEARXNG_URL", "http://searxng:8080").rstrip("/")
        self.serpapi_key = os.getenv("SERPAPI_API_KEY", "")
        self.network_slots = asyncio.Semaphore(5)
        self.web_search = FederatedSearch(client, store, self.network_slots, searxng_url=self.searxng_url)
        self.ranker = LocalRanker(client, store)
        self.pricing = KeylessPricing(client, store)
        self.price_research = PriceDocumentResearch(client, store, self.web_search, self.network_slots)
        self.event_discovery = RegionalEventDiscovery(client, store, self.web_search, self.network_slots)

    async def regional_events(self, place: Place, intent: TravelIntent) -> EventResult:
        if not self.web_fallback_enabled:
            return EventResult(status="not_checked")
        return await self.event_discovery.discover(place, intent)

    async def price_documents(self, place, origin, intent, current):
        return await self.price_research.lookup(place, origin, intent, current, enabled=self.web_fallback_enabled)

    def search_capabilities(self) -> dict[str, Any]:
        result = self.web_search.capabilities()
        result["fallback_enabled"] = self.web_fallback_enabled
        if not self.web_fallback_enabled:
            result["configured"] = False
            for provider in result["providers"]:
                provider.update(configured=False, status="disabled", reason="external_fallback_disabled")
        return result

    def catalog_capabilities(self) -> dict[str, Any]:
        return self.catalog.capabilities()

    @staticmethod
    def _catalog_evidence(hotel: dict[str, Any], result: Any) -> Evidence:
        return Evidence(provider="Локальный каталог / OpenStreetMap", source_url=hotel["source_url"],
            retrieved_at=hotel.get("retrieved_at") or result.retrieved_at, freshness="snapshot",
            confidence="Снимок открытых данных об объекте; не инвентарь",
            label=f'Объект размещения «{hotel["name"]}» из локального снимка. Цена, наличие, вместимость и условия на даты неизвестны. © OpenStreetMap contributors, ODbL.')

    @staticmethod
    def _catalog_fallback_evidence(result: Any, *, enabled: bool) -> Evidence:
        reasons = {"unsupported_destination": "направление не покрыто", "destination_not_covered": "направление не покрыто",
                   "out_of_scope": "направление не покрыто", "no_eligible_hotels": "нет подходящих объектов",
                   "no_eligible_local_hotels": "нет подходящих объектов", "empty": "нет подходящих объектов",
                   "disabled": "каталог отключён", "catalog_disabled": "каталог отключён"}
        reason = reasons.get(result.reason, "локальный снимок недоступен или не прошёл проверку")
        action = "Резервный поиск во внешних источниках." if enabled else "Внешний резервный поиск отключён; сведения остаются неизвестными."
        return Evidence(provider="Локальный каталог", source_url=None,
            retrieved_at=result.retrieved_at or utcnow(), freshness="unavailable", confidence="Нет локального подтверждения",
            label=f"Локальный каталог: {reason}. {action}")

    def _apply_catalog_hotel(self, stay: TravelComponent, result: Any, hotel=None) -> None:
        hotel = hotel or result.hotels[0]
        booking_search_url = stay.source_url
        stay.title = hotel["name"]
        stay.description = "Объект из локального снимка открытых данных. Доступность на ваши даты, номер, вместимость, цена и специальные условия НЕ подтверждены."
        stay.source_url = hotel["source_url"]
        stay.price, stay.quote, stay.status = None, None, "unavailable"
        stay.details.update({"latitude": hotel["latitude"], "longitude": hotel["longitude"],
            "osm_tags": dict(hotel["tags"]), "website": hotel.get("website"), "booking_search_url": booking_search_url,
            "property_source": "local_catalog", "discovery_mode": "local_catalog", "link_type": "place",
            "catalog_property_id": hotel["hotel_id"], "pricing_entity_id": hotel["hotel_id"],
            "catalog_version": result.release_id, "catalog_retrieved_at": hotel.get("retrieved_at") or result.retrieved_at,
            "catalog_source_updated_at": hotel.get("source_updated_at"),
            "accommodation_kind": hotel.get("kind", "hotel"),
            "catalog_sources": [{key: source.get(key) for key in
                ("source_id", "record_id", "url", "license", "license_url", "retrieved_at", "source_updated_at", "link_method", "link_status")}
                for source in hotel.get("source_records", [])[:4]],
            "alternative_properties": [dict(row) for row in result.hotels[1:3]],
            "availability_verified": False, "requirements_verified": False, "pet_policy_verified": False,
            "property_location_verified": False})
        stay.evidence.append(self._catalog_evidence(hotel, result))

    @traced("provider.cached_get")
    async def cached_get(self, url: str, params: dict[str, Any], ttl: int = 3600, request_timeout: float = 15) -> tuple[dict[str, Any], str, str]:
        if not self.web_fallback_enabled:
            set_attributes(outcome="policy_disabled")
            return {}, "unavailable", utcnow()
        key_params = {key: value for key, value in params.items() if key != "api_key"}
        key = hashlib.sha256((url + json.dumps(key_params, sort_keys=True)).encode()).hexdigest()
        cached = self.store.cache_get(key)
        if cached:
            set_attributes(cache_hit=True, outcome="cached")
            self.store.increment("cache_hits")
            return cached[0], "cached", timestamp(cached[1])
        try:
            async with queued(self.network_slots, "provider.http.queue"):
                with span("provider.http", kind="client"):
                    async with self.client.stream("GET", url, params=params,
                            timeout=httpx.Timeout(request_timeout, connect=min(6, request_timeout)), follow_redirects=False) as response:
                        set_attributes(http_response_status_code=response.status_code)
                        response.raise_for_status()
                        body = bytearray()
                        async for chunk in response.aiter_bytes(chunk_size=65536):
                            body.extend(chunk)
                            if len(body) > 2 * 1024 * 1024:
                                raise ValueError("provider_response_too_large")
                        payload = json.loads(body)
            if not isinstance(payload, dict) or payload.get("error"):
                raise ValueError("Invalid provider response")
            payload = clean_payload(payload)
            self.store.cache_put(key, payload, ttl)
            self.store.increment("provider_requests")
            set_attributes(cache_hit=False, outcome="live")
            return payload, "live", utcnow()
        except (httpx.HTTPError, ValueError, TypeError):
            self.store.increment("provider_errors")
            stale = self.store.cache_get(key, allow_stale=True)
            # At most seven days old; stale evidence always remains visibly cached.
            if stale and datetime.now(timezone.utc).timestamp() - stale[1] < 7 * 86400:
                data = dict(stale[0])
                data["_stale_if_error"] = True
                self.store.increment("cache_stale_hits")
                set_attributes(cache_hit=True, outcome="cached_stale")
                return data, "cached", timestamp(stale[1])
            set_attributes(outcome="unavailable")
            return {}, "unavailable", utcnow()

    async def resolve_place(self, name: str) -> Place | None:
        known = find_place(name)
        if known:
            return known
        url = "https://geocoding-api.open-meteo.com/v1/search"
        payload, freshness, retrieved = await self.cached_get(url, {"name": name, "count": 1, "language": "ru", "format": "json"}, ttl=86400 * 30)
        results = payload.get("results", [])
        if not results:
            return None
        row = results[0]
        try:
            lat, lon = float(row["latitude"]), float(row["longitude"])
            if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                return None
            scope = "country" if row.get("feature_code") == "PCLI" else "locality"
            country_code = str(row.get("country_code", "")).upper()
            aliases = tuple(dict.fromkeys((name, *COUNTRY_QUALIFIERS.get(country_code, ())))) if scope == "country" else (name,)
            return Place(str(row["name"]), str(row.get("country", "Страна не определена")), country_code, lat, lon, None, (), aliases,
                         url + "?" + urlencode({"name": name, "count": 1, "language": "ru"}),
                         "Направление из вашего запроса; характеристики требуют проверки", "Open-Meteo Geocoding / GeoNames", retrieved, freshness, scope)
        except (KeyError, ValueError, TypeError):
            return None

    async def search(self, place: Place, intent: TravelIntent) -> list[Evidence]:
        with span("catalog.search"):
            catalog = self.catalog.search(place, intent)
            set_attributes(outcome="matched" if catalog.status == "matched" else "disabled" if catalog.status == "disabled" else "unavailable")
        searches = scoped_searches(place, intent)
        evidence: list[Evidence] = []
        if catalog.status == "matched" and catalog.hotels:
            evidence.extend(self._catalog_evidence(hotel, catalog) for hotel in catalog.hotels[:3])
            searches = [(scope, query) for scope, query in searches if scope != "lodging"]
        elif catalog.status != "disabled":
            evidence.append(self._catalog_fallback_evidence(catalog, enabled=self.web_fallback_enabled))
        if not self.web_fallback_enabled:
            evidence.append(Evidence(provider="Внешний резервный поиск", source_url=None, retrieved_at=utcnow(),
                freshness="unavailable", confidence="Внешние источники отключены",
                label="Внешний поиск отключён. Дополнительные сведения о маршруте, ценах и наличии неизвестны."))
            return evidence
        results = await asyncio.gather(*(self.web_search.search(query) for _, query in searches))
        seen: set[str] = set()
        origin = find_place(intent.origin) if intent.origin else None
        for (scope, _), outcomes in zip(searches, results):
            groups: list[list[SearchHit]] = []
            for outcome in outcomes:
                group = []
                for hit in outcome.hits:
                    row, title = hit.row(), hit.title
                    if scope == 'lodging':
                        relevant = lodging_result_matches(row, place, intent.origin)
                        relevant = relevant and not re.search(r'\bflights?\b|авиабилет|перел[её]т', title, re.I)
                    else:
                        context = title + ' ' + hit.url + ' ' + hit.snippet[:1200]
                        relevant = city_mentions(context, place, airport=True)
                        relevant = relevant and (city_mentions(context, origin, airport=True) if origin else bool(intent.origin and normalized(intent.origin) in normalized(context)))
                        relevant = relevant and not re.search(r'\bhotels?\b|\bresorts?\b|отел|гостиниц', title, re.I)
                    if relevant:
                        group.append(hit)
                    else:
                        self.store.increment('search_results_rejected_geography')
                groups.append(group)
            eligible = [hit.row() for hit in fair_merge(groups, seen=seen)]
            if scope == 'lodging':
                eligible = await self.ranker.rank_rows(eligible, lodging_query(intent))
            for row in eligible[:3]:
                hit = row['_search_hit']
                label = ('Проживание в направлении: ' if scope == 'lodging' else 'Маршрут отправления → назначения: ') + hit.title + '. Поисковый результат; наличие и цена не подтверждены.'
                if hit.stale_if_error:
                    label += ' Сохранённый результат: источник временно недоступен.'
                evidence.append(Evidence(provider=hit.provider_label, source_url=hit.url, retrieved_at=hit.retrieved_at,
                    freshness=hit.freshness, confidence='Поисковая выдача, не инвентарь; адрес не проверен', label=label))
        if not evidence:
            evidence.append(Evidence(provider="Веб-поиск", source_url=None, retrieved_at=utcnow(), freshness="unavailable", confidence="Нет подтверждения", label="Поиск не вернул источников с подходящей географией; цены и наличие неизвестны."))
        return evidence

    async def weather(self, place: Place, intent: TravelIntent) -> dict[str, Any]:
        if not self.web_fallback_enabled:
            evidence = Evidence(provider="Open-Meteo", source_url=None, retrieved_at=utcnow(), freshness="unavailable",
                confidence="Внешние источники отключены", label="Внешний погодный источник отключён; температура неизвестна.")
            return {"kind": "unknown", "status": "unavailable", "summary": "Погодные данные неизвестны: внешние источники отключены", "evidence": [evidence.model_dump()]}
        if not intent.date_from or not intent.date_to:
            return {"kind": "unknown", "status": "unavailable", "summary": "Даты ещё не определены", "evidence": []}
        today = local_today()
        requested_start = intent.date_from
        requested_end = intent.date_to - timedelta(days=1)
        forecast = requested_start >= today and requested_end <= today + timedelta(days=15)
        if forecast:
            start, end = requested_start, requested_end
            endpoint = "https://api.open-meteo.com/v1/forecast"
            kind = "forecast"
        else:
            # Recent prior-year observations for the same calendar interval; never called a forecast or climate normal.
            safe_year = min(requested_start.year - 1, today.year - 1)
            try:
                start = requested_start.replace(year=safe_year)
            except ValueError:
                start = requested_start.replace(year=safe_year, day=28)
            end = start + (requested_end - requested_start)
            endpoint = "https://archive-api.open-meteo.com/v1/archive"
            kind = "historical"
        params = {"latitude": place.latitude, "longitude": place.longitude, "start_date": start.isoformat(), "end_date": end.isoformat(), "daily": "temperature_2m_max,temperature_2m_min,precipitation_sum", "timezone": "auto"}
        payload, freshness, retrieved = await self.cached_get(endpoint, params, ttl=3600 if forecast else 86400 * 7)
        daily = payload.get("daily", {})
        maxima = [float(value) for value in daily.get("temperature_2m_max", []) if isinstance(value, (float, int))]
        minima = [float(value) for value in daily.get("temperature_2m_min", []) if isinstance(value, (float, int))]
        rain = [float(value) for value in daily.get("precipitation_sum", []) if isinstance(value, (float, int))]
        source_url = endpoint + "?" + urlencode(params)
        if not maxima or not minima:
            evidence = Evidence(provider="Open-Meteo", source_url=source_url, retrieved_at=retrieved, freshness="unavailable", confidence="Нет данных", label="Погодные данные не получены. Температура не подставляется.")
            return {"kind": "unknown", "status": "unavailable", "summary": "Погода неизвестна; источник временно недоступен", "evidence": [evidence.model_dump()]}
        high, low = round(sum(maxima) / len(maxima), 1), round(sum(minima) / len(minima), 1)
        label = ("Прогноз" if forecast else "Исторические данные, НЕ прогноз") + f": {start.isoformat()} — {end.isoformat()}"
        if payload.get("_stale_if_error"):
            label += "; сохранённая копия при недоступном источнике"
        evidence = Evidence(provider="Open-Meteo", source_url=source_url, retrieved_at=retrieved, freshness=freshness, confidence="Модельный прогноз" if forecast else "Реанализ за один прошлый год; не климатическая норма", label=label)
        return {"kind": kind, "status": freshness, "temperature_max": high, "temperature_min": low, "temperature_unit": "°C", "precipitation_total_mm": round(sum(rain), 1) if rain else None,
                "period_from": start.isoformat(), "period_to": end.isoformat(), "requested_from": requested_start.isoformat(), "requested_to": requested_end.isoformat(),
                "summary": f"{label}. В среднем днём {high:g} °C, ночью {low:g} °C.", "evidence": [evidence.model_dump()]}

    def links(self, place: Place, intent: TravelIntent) -> tuple[str, str, str]:
        flight = "https://www.google.com/travel/flights?" + urlencode({"q": f"Flights from {intent.origin} to {place.name} on {intent.date_from} returning {intent.date_to}, {intent.adults} adults, {len(intent.children_ages)} children"})
        params: dict[str, Any] = {"ss": place.name, "checkin": str(intent.date_from), "checkout": str(intent.date_to), "group_adults": intent.adults, "group_children": len(intent.children_ages), "no_rooms": 1}
        stay = "https://www.booking.com/searchresults.html?" + urlencode(params)
        if intent.children_ages:
            stay += "&" + urlencode([("age", age) for age in intent.children_ages])
        transfer = "https://www.openstreetmap.org/?" + urlencode({"mlat": place.latitude, "mlon": place.longitude}) + "#map=12/" + str(place.latitude) + "/" + str(place.longitude)
        return flight, stay, transfer

    async def real_places(self, place: Place, intent: TravelIntent, stay: TravelComponent, transfer: TravelComponent) -> None:
        """Attach actual OSM hotel/airport POIs without implying availability on dates."""
        with span("catalog.search"):
            catalog = self.catalog.search(place, intent)
            set_attributes(outcome="matched" if catalog.status == "matched" else "disabled" if catalog.status == "disabled" else "unavailable")
        if catalog.status == "matched" and catalog.hotels:
            self._apply_catalog_hotel(stay, catalog)
            return
        stay.details["catalog_fallback_reason"] = catalog.reason
        if catalog.status != "disabled" or not self.web_fallback_enabled:
            stay.evidence.append(self._catalog_fallback_evidence(catalog, enabled=self.web_fallback_enabled))
        if not self.web_fallback_enabled:
            return
        if place.scope == "country":
            # A country's centroid is not a requested resort or a hotel search radius.
            await self.hotel_search_fallback(place, intent, stay)
            return
        endpoint = os.getenv("OVERPASS_URL", "https://overpass-api.de/api/interpreter")
        query = (f'[out:json][timeout:8];nwr(around:6000,{place.latitude},{place.longitude})["tourism"="hotel"]["name"];out center 12;'
                 f'nwr(around:100000,{place.latitude},{place.longitude})["aeroway"="aerodrome"]["iata"];out center 8;')
        payload, freshness, retrieved = await self.cached_get(endpoint, {"data": query}, ttl=86400, request_timeout=5)
        if freshness == "unavailable" and not os.getenv("OVERPASS_URL"):
            payload, freshness, retrieved = await self.cached_get("https://overpass.private.coffee/api/interpreter", {"data": query}, ttl=86400, request_timeout=5)
        hotels, airports = [], []
        for row in payload.get("elements", []):
            tags = row.get("tags", {})
            position = row.get("center", row)
            lat, lon = position.get("lat"), position.get("lon")
            if not isinstance(lat, (float, int)) or not isinstance(lon, (float, int)):
                continue
            if row.get("type") not in ("node", "way", "relation") or not isinstance(row.get("id"), int):
                continue
            entry = {"name": str(tags.get("name:ru") or tags.get("name", "Без названия"))[:180], "latitude": lat, "longitude": lon,
                     "source_url": f'https://www.openstreetmap.org/{row["type"]}/{row["id"]}', "tags": {key: str(tags[key])[:200] for key in ("stars", "wheelchair", "internet_access", "air_conditioning", "swimming_pool", "dog", "pets", "addr:street", "addr:housenumber", "iata") if key in tags},
                     "website": public_url(tags.get("website") or tags.get("contact:website"))}
            if (tags.get("tourism") == "hotel" and lodging_candidate_matches_wishes(entry['name'], intent)
                    and not (intent.children_ages and re.search(r"adults?[- ]*only|только.{0,8}взросл|18\+", entry["name"], re.IGNORECASE))):
                hotels.append(entry)
            elif tags.get("aeroway") == "aerodrome":
                airports.append(entry)
        if not hotels:
            stay.evidence.append(Evidence(provider="OpenStreetMap / Overpass", source_url="https://www.openstreetmap.org/copyright", retrieved_at=retrieved, freshness="unavailable", confidence="Нет подтверждённого объекта", label="Не удалось получить конкретный отель из OSM. Осталась ссылка поиска; наличие не утверждается."))
            await self.hotel_search_fallback(place, intent, stay)
            return
        # Prefer an explicitly tagged pet permission when requested, but never claim a contractual pet policy.
        hotels.sort(key=lambda hotel: (0 if intent.pets and (hotel["tags"].get("pets") == "yes" or hotel["tags"].get("dog") == "yes") else 1, hotel["name"]))
        hotel = hotels[0]
        booking_search_url = stay.source_url
        stay.title = hotel["name"]
        stay.description = "Реальный объект в OpenStreetMap, выбран для исследования маршрута. Доступность на ваши даты, номер, вместимость и цена НЕ подтверждены."
        stay.source_url = hotel["source_url"]
        stay.details.update({"latitude": hotel["latitude"], "longitude": hotel["longitude"], "osm_tags": hotel["tags"], "website": hotel["website"], "booking_search_url": booking_search_url,
                             "property_source": "OpenStreetMap", "alternative_properties": hotels[1:3], "availability_verified": False, "link_type": "place"})
        stay.evidence.append(Evidence(provider="OpenStreetMap / Overpass", source_url=hotel["source_url"], retrieved_at=retrieved, freshness=freshness, confidence="Реальный POI из сообщества, не бронируемое наличие", label=f'Отель «{hotel["name"]}» присутствует на карте OSM. Условия, вместимость и цена на даты неизвестны. © OpenStreetMap contributors, ODbL.'))
        airport = next((airport for airport in airports if airport["tags"].get("iata") == place.airport), None)
        if airport is None and not place.airport and airports:
            airport = min(airports, key=lambda row: (row["latitude"] - place.latitude) ** 2 + (row["longitude"] - place.longitude) ** 2)
        if airport:
            point_a = Place(airport["name"], place.country, place.country_code, airport["latitude"], airport["longitude"], None, (), (), airport["source_url"], "")
            point_b = Place(hotel["name"], place.country, place.country_code, hotel["latitude"], hotel["longitude"], None, (), (), hotel["source_url"], "")
            km = round(distance_km(point_a, point_b), 1)
            transfer.title = f'{airport["tags"].get("iata", airport["name"])} → {hotel["name"]}'
            transfer.description = f"Геометрическое расстояние по координатам OSM: {km:g} км по прямой. Дорожный маршрут, время, доступность и цена трансфера не проверены; требуется также обратный трансфер."
            transfer.source_url = "https://www.google.com/maps/dir/?" + urlencode({"api": 1, "origin": f'{airport["latitude"]},{airport["longitude"]}', "destination": f'{hotel["latitude"]},{hotel["longitude"]}', "travelmode": "driving"})
            transfer.details.update({"straight_line_distance_km": km, "distance_method": "haversine_from_osm_coordinates", "airport": airport, "hotel": hotel, "link_type": "directions_search", "road_route_verified": False})
            transfer.evidence.append(Evidence(provider="OpenStreetMap + геометрический расчёт", source_url=airport["source_url"], retrieved_at=retrieved, freshness=freshness, confidence="Координаты POI и детерминированное расстояние; не дорожная котировка", label="Расстояние по прямой вычислено формулой гаверсинуса; цена и время трансфера неизвестны. © OpenStreetMap contributors, ODbL."))

    async def hotel_search_fallback(self, place: Place, intent: TravelIntent, stay: TravelComponent) -> None:
        """A concrete indexed hotel page is useful evidence when OSM is unavailable.

        This is a SERP observation, never a scraped booking page or a verified property policy.
        """
        if stay.details.get("catalog_property_id") or not self.web_fallback_enabled:
            return
        place_query = next((alias for alias in place.aliases if alias.isascii() and " " not in alias and len(alias) > 3), place.name)
        terms = lodging_discovery_terms(intent)
        query = f"{place_query} {place.country} hotel {' '.join(terms)} (site:booking.com/hotel/ OR site:ostrovok.ru/hotel/)"
        outcomes = await self.web_search.search(query, ttl=86400)
        groups: list[list[SearchHit]] = []
        for outcome in outcomes:
            group = []
            for hit in outcome.hits:
                if not indexed_hotel_url(hit.url):
                    continue
                if not lodging_result_matches(hit.row(), place, intent.origin):
                    self.store.increment('search_results_rejected_geography')
                    continue
                if not lodging_candidate_matches_wishes(hit.title, intent):
                    continue
                if intent.children_ages and re.search(r"adults?[- ]*only|только.{0,8}взросл|18\+", hit.title, re.IGNORECASE):
                    continue
                if intent.pets and re.search(r"no pets|pets not allowed|без животн", hit.title, re.IGNORECASE):
                    continue
                group.append(hit)
            groups.append(group)
        eligible = [hit.row() for hit in fair_merge(groups)]
        eligible = await self.ranker.rank_rows(eligible, lodging_query(intent))
        # Only promote among the already ranked shortlist. A strict source path
        # can be checked by the connected adapter; a similar name cannot map IDs.
        eligible = sorted(eligible[:3], key=lambda row: ostrovok_property_target(row.get('url'), place) is None)
        properties = []
        for row in eligible[:3]:
            title = str(row.get("title", "")).strip()
            title = re.sub(r"\s*[-|–]\s*(?:Booking\.com|Ostrovok(?:\.ru)?|Островок(?:\.ру)?).*$", "", title, flags=re.IGNORECASE)
            title = re.sub(r"\s*\([^)]*(?:prices?|цен|fiyat|prix|precio|prezzi|preise)[^)]*\)", "", title, flags=re.IGNORECASE).strip()[:180]
            hit = row['_search_hit']
            label = "Название взято из реального поискового результата, а не сгенерировано. Доступность и цена неизвестны; источник может устареть."
            if hit.stale_if_error:
                label += " Сохранённый результат: источник временно недоступен."
            evidence = Evidence(provider=hit.provider_label + " / индексированная карточка отеля", source_url=row['url'], retrieved_at=hit.retrieved_at, freshness=hit.freshness, confidence="Конкретная ссылка из выдачи; объект и инвентарь не перепроверены", label=label)
            properties.append({"name": title, "source_url": row['url'], "evidence": evidence.model_dump(mode="json")})
        if not properties:
            return
        self._apply_indexed_hotel(stay, properties[0], place)
        stay.details["alternative_properties"] = properties[1:]

    @staticmethod
    def _apply_indexed_hotel(stay: TravelComponent, property_: dict, place: Place) -> None:
        evidence = Evidence.model_validate(property_["evidence"])
        stay.details.update({"booking_search_url": stay.source_url, "property_source": evidence.provider, "property_location_verified": False, "link_type": "indexed_hotel_page"})
        stay.details.update({'requirements_verified': False, 'pet_policy_verified': False, 'availability_verified': False})
        target = ostrovok_property_target(property_['source_url'], place)
        if target:
            stay.details['pricing_entity_id'] = target[0]
        stay.title = property_["name"]
        stay.source_url = property_["source_url"]
        stay.description = "Конкретная карточка отеля найдена через веб-поиск. Страница поставщика не сканировалась; адрес, актуальность объекта, вместимость, цена и наличие на даты требуют проверки."
        stay.evidence.append(evidence)

    async def inventory(self, place: Place, origin: Place, intent: TravelIntent) -> tuple[TravelComponent, TravelComponent, TravelComponent]:
        components = self._inventory_components(place, origin, intent)
        source_failures = (httpx.HTTPError, TimeoutError, ValueError, TypeError, KeyError)

        async def guarded(operation, component):
            try:
                await operation
            except source_failures:
                # A provider failure says nothing about the independent branch.
                # Keep any already obtained facts, with no exception text.
                component.details['discovery_outcome'] = 'unavailable'

        async def flight_branch():
            flight = components[0]
            legacy = flight.model_copy(deep=True)
            children = [asyncio.create_task(guarded(self._inventory_flight(place, origin, intent, legacy), legacy)),
                asyncio.create_task(guarded(self.pricing.apply(place, origin, intent, components, only=('flight',)), flight))]
            try:
                async with asyncio.timeout(42):
                    await asyncio.gather(*children)
            except TimeoutError:
                flight.details['discovery_outcome'] = 'timeout'
            finally:
                for child in children:
                    if not child.done():
                        child.cancel()
                await asyncio.gather(*children, return_exceptions=True)
            if legacy.price is not None and (flight.quote is None or flight.quote.value.kind == 'unknown'):
                # Legacy and keyless work on different copies. Only adopt the
                # legacy route when no numeric observation was selected; it
                # cannot supply facts/price to another source's itinerary.
                for field in ('title', 'description', 'price', 'source_url', 'status', 'evidence'):
                    setattr(flight, field, getattr(legacy, field))
                flight.details.update(legacy.details)

        async def lodging_branch():
            await guarded(self._inventory_base(place, origin, intent, components), components[1])
            await guarded(self.pricing.apply(place, origin, intent, components, only=('stay',)), components[1])

        # Components are constructed before either branch starts. Flight owns
        # only component 0; lodging owns 1/2, including hotel-driven transfers.
        tasks = [asyncio.create_task(flight_branch()), asyncio.create_task(lodging_branch())]
        try:
            async with asyncio.timeout(45):
                await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        return components

    def _inventory_components(self, place: Place, origin: Place, intent: TravelIntent) -> tuple[TravelComponent, TravelComponent, TravelComponent]:
        flight_url, stay_url, transfer_url = self.links(place, intent)
        party = f"{intent.adults} взр." + (f", дети: {', '.join(map(str, intent.children_ages))} лет" if intent.children_ages else "")
        flight = TravelComponent(title=f"{origin.name} → {place.name} → {origin.name}", description=f"Даты {intent.date_from} — {intent.date_to}; {party}. Расписание, багаж, пересадки и цена не подтверждены.", currency=intent.currency, source_url=flight_url,
                                 details={"transport_mode": "flight", "availability_verified": False, "airport": place.airport, "adults": intent.adults, "children_ages": intent.children_ages, "direct_flight_verified": False, "link_type": "search"})
        stay = TravelComponent(title="Размещение — поиск вариантов", description=f"{(intent.date_to - intent.date_from).days} ночей; {party}. Конкретный объект, правила и наличие не подтверждены.", currency=intent.currency, source_url=stay_url,
                               details={"availability_verified": False, "nights": (intent.date_to - intent.date_from).days, "pet_policy_verified": False, "requirements_verified": False, "link_type": "search"})
        transfer = TravelComponent(title="Аэропорт → размещение → аэропорт", description="Маршрут до конкретного отеля, время и стоимость требуют проверки после выбора рейса и проживания.", currency=intent.currency, source_url=transfer_url,
                                   details={"availability_verified": False, "link_type": "map"})
        mode = transport_mode(intent)
        if mode != "flight":
            labels = {"train": "Поезд", "bus": "Автобус", "car": "Автомобиль"}
            flight.title = f"{labels.get(mode, 'Запрошенный транспорт')}: {origin.name} → {place.name} → {origin.name}"
            flight.description = f"Запрошенный наземный транспорт; даты {intent.date_from} — {intent.date_to}, {party}. Расписание, пересадки, правила и цена не подтверждены."
            flight.source_url = "https://www.google.com/search?" + urlencode({"q": f"{intent.origin} {place.name} {mode} tickets timetable {intent.date_from}"})
            if mode == 'unsupported':
                flight.source_url = None
                flight.description = 'Запрошенный способ передвижения сохранен; для него нужен отдельный источник маршрута и цены.'
            flight.details.update({"transport_mode": mode, "airport": None, "link_type": "search"})
            transfer.title = "Вокзал / точка прибытия → размещение"
            transfer.description = "Точная станция или точка прибытия зависит от выбранного маршрута; местный путь до отеля и обратно, время и цена неизвестны."
            transfer.details = {"availability_verified": False, "transport_mode": mode, "link_type": "map"}
            transfer.evidence = []
            transfer.source_url = transfer_url
        return flight, stay, transfer

    async def _inventory_flight(self, place, origin, intent, flight):
        if (flight.details.get('transport_mode') != 'flight' or not self.serpapi_key
                or not self.web_fallback_enabled or not origin.airport or not place.airport):
            return
        # Credentialed observations remain an independent optional source; this
        # branch cannot write to lodging or wait for its initial discovery.
        params: dict[str, Any] = {"engine": "google_flights", "departure_id": origin.airport,
            "arrival_id": place.airport, "outbound_date": str(intent.date_from),
            "return_date": str(intent.date_to), "currency": intent.currency,
            "adults": intent.adults, "type": 1, "api_key": self.serpapi_key}
        if not origin.airport or not place.airport or any(age < 2 for age in intent.children_ages):
            return
        if intent.children_ages:
            params['children'] = len(intent.children_ages)
        payload, freshness, retrieved = await self.cached_get('https://serpapi.com/search.json', params, 1800)
        flights = payload.get('best_flights', []) + payload.get('other_flights', [])
        available = [row for row in flights if isinstance(row.get('price'), (float, int)) and row['price'] > 0]
        if 'direct_flight' in intent.hard_constraints:
            available = [row for row in available if len(row.get('flights', [])) == 1]
        if intent.max_travel_minutes:
            available = [row for row in available if isinstance(row.get('total_duration'), (float, int))
                         and row['total_duration'] <= intent.max_travel_minutes]
        if not available:
            return
        row = min(available, key=lambda item: item['price'])
        flight.price, flight.status = float(row['price']), 'observed'
        flight.description = 'Наблюдаемая цена из Google Flights через SerpApi; не бронь. Итоговую сумму и правила проверьте у продавца.'
        flight.details.update({'duration_minutes': row.get('total_duration'), 'segments': len(row.get('flights', [])),
            'direct_flight_verified': len(row.get('flights', [])) == 1,
            'price_scope': 'provider_search_result_scope_requires_vendor_confirmation', 'retrieved_at': retrieved,
            'cache_freshness': freshness, 'availability_verified': False})
        flight.evidence = [Evidence(provider='SerpApi / Google Flights', source_url=flight.source_url,
            retrieved_at=retrieved, freshness=freshness, confidence='Наблюдаемая поисковая цена, без удержания тарифа',
            label='Цена для параметров поискового запроса; наличие у продавца не гарантируется.')]
        if payload.get('_stale_if_error'):
            flight.evidence[0].label += ' Просроченная сохранённая копия: обновление источника не удалось.'
            flight.details['cache_stale'] = True

    async def _inventory_base(self, place, origin, intent, components=None):
        flight, stay, transfer = components or self._inventory_components(place, origin, intent)
        stay_url = self.links(place, intent)[1]
        await self.real_places(place, intent, stay, transfer)
        if transport_mode(intent) != 'flight':
            transfer.title = 'Вокзал / точка прибытия → размещение'
            transfer.description = 'Точная станция или точка прибытия зависит от выбранного маршрута; местный путь до отеля и обратно, время и цена неизвестны.'
            transfer.details = {'availability_verified': False, 'transport_mode': transport_mode(intent), 'link_type': 'map'}
            transfer.evidence = []
            transfer.source_url = self.links(place, intent)[2]
            return flight, stay, transfer
        if not self.serpapi_key or not self.web_fallback_enabled:
            return flight, stay, transfer
        hotel_params = {"engine": "google_hotels", "q": place.name + " hotels", "check_in_date": str(intent.date_from), "check_out_date": str(intent.date_to), "adults": intent.adults, "children": len(intent.children_ages), "currency": intent.currency, "api_key": self.serpapi_key}
        if intent.children_ages:
            hotel_params["children_ages"] = ",".join(map(str, intent.children_ages))
        payload, freshness, retrieved = (await self.cached_get('https://serpapi.com/search.json', hotel_params, 1800)
            if not stay.details.get('catalog_property_id') else ({}, 'unavailable', utcnow()))
        properties = payload.get("properties", [])
        available_stays = [row for row in properties if isinstance(row.get("total_rate", {}).get("extracted_lowest"), (float, int)) and row["total_rate"]["extracted_lowest"] > 0]
        if available_stays:
            row = min(available_stays, key=lambda item: item["total_rate"]["extracted_lowest"])
            stay.title = str(row.get("name", "Размещение из поисковой выдачи"))[:160]
            stay.price = float(row["total_rate"]["extracted_lowest"])
            stay.status = "observed"
            stay.description = "Наблюдаемая цена за период из Google Hotels через SerpApi. Вместимость, налоги, доплаты и правила объекта проверьте у продавца."
            stay.details.update({"price_scope": "total_rate_search_result", "retrieved_at": retrieved, "cache_freshness": freshness, "availability_verified": False})
            stay.source_url = public_url(row.get("link")) or stay_url
            stay.evidence = [Evidence(provider="SerpApi / Google Hotels", source_url=stay.source_url, retrieved_at=retrieved, freshness=freshness, confidence="Наблюдаемая цена за период, без подтверждения объекта", label="Цена выдачи; правила размещения и окончательный состав платежей требуют проверки.")]
            if payload.get("_stale_if_error"):
                stay.evidence[0].label += " Просроченная сохранённая копия: обновление источника не удалось."
                stay.details["cache_stale"] = True
        return flight, stay, transfer


class LocalTravelDataPort:
    """Gateway composition adapter. Never used as an implicit HTTP failure fallback."""
    def __init__(self, providers: Providers):
        self.providers = providers

    async def aclose(self):
        # This bridge owns its explicitly injected cache; the composition root
        # owns the shared HTTP client. Gateway lifespan closes its cache directly.
        self.providers.store.close()

    @traced("travel_data.capabilities")
    async def capabilities(self) -> dict[str, Any]:
        providers = self.providers
        return {"inventory_candidates": True, "external_data_fallback_enabled": providers.web_fallback_enabled,
            "providers": {"catalog": providers.catalog_capabilities(), "search": providers.search_capabilities(),
                "weather": {"name": "Open-Meteo / резервный источник", "configured": providers.web_fallback_enabled,
                            "capability": "forecast_or_labeled_past_year_observations"},
                "places": {"name": "Локальный каталог; OpenStreetMap / Overpass как резерв",
                           "configured": providers.catalog_capabilities()["configured"] or providers.web_fallback_enabled,
                           "capability": "property_facts_not_availability"},
                "inventory": {"name": "SerpApi / резервный источник", "configured": bool(providers.serpapi_key) and providers.web_fallback_enabled,
                              "capability": "observed_search_prices_not_booking"},
                "price_documents": {"name": "Источники опубликованных цен",
                                    "configured": providers.web_fallback_enabled and providers.price_research.configured,
                                    "status": "ready" if providers.web_fallback_enabled and providers.price_research.configured else "disabled",
                                    "capability": "fetched_source_documents"}},
            "pricing": providers.pricing.capabilities(), "gateway": {"status": "ready", "contract_version": "travel-data.v1"}}

    @traced("travel_data.resolve_place")
    async def resolve_place(self, name):
        return await self.providers.resolve_place(name)

    @traced("travel_data.weather")
    async def weather(self, place, intent):
        return await self.providers.weather(place, intent)

    @traced("travel_data.search")
    async def search(self, place, intent):
        return await self.providers.search(place, intent)

    @traced("travel_data.inventory")
    async def inventory(self, place, origin, intent):
        return ComponentBundle.from_components(await self.providers.inventory(place, origin, intent))

    async def inventory_candidates(self, place, origin, intent, *, limit=3):
        InventoryCandidatesInput(place=PlaceRef.from_place(place), origin=PlaceRef.from_place(origin),
                                 intent=DataIntent.from_intent(intent), limit=limit)
        async with asyncio.timeout(45):
            original = await self.inventory(place, origin, intent)
            catalog = self.providers.catalog.search(place, intent, limit=limit)
            candidates = []
            if catalog.status == "matched" and catalog.hotels:
                for hotel in catalog.hotels[:limit]:
                    if not lodging_candidate_matches_wishes(hotel["name"] + " " + hotel.get("kind", ""), intent):
                        continue
                    # The route is shared; lodging and property-dependent transfers
                    # have independent observations, never a Cartesian expansion.
                    parts = self.providers._inventory_components(place, origin, intent)
                    self.providers._apply_catalog_hotel(parts[1], catalog, hotel)
                    parts[2].details["upstream_property_id"] = hotel["hotel_id"]
                    bundle = ComponentBundle(flight=original.flight.model_copy(deep=True), stay=parts[1], transfer=parts[2])
                    bundle.candidate_id = candidate_identity(bundle, place, origin, intent)
                    candidates.append(bundle)
            elif (original.stay.details.get("pricing_entity_id") or original.stay.details.get("link_type") == "place"
                  or (original.stay.details.get("link_type") == "indexed_hotel_page"
                      and indexed_hotel_url(original.stay.source_url or "")
                      and lodging_result_matches({"title": original.stay.title, "url": original.stay.source_url}, place, intent.origin))):
                original.candidate_id = candidate_identity(original, place, origin, intent)
                candidates.append(original)
                if original.stay.details.get("link_type") == "indexed_hotel_page":
                    seen = {url_identity(original.stay.source_url)}
                    for alternative in original.stay.details.get("alternative_properties", [])[:2]:
                        if len(candidates) >= limit:
                            break
                        url = alternative.get("source_url", "")
                        if (not indexed_hotel_url(url) or url_identity(url) in seen
                                or not lodging_result_matches({"title": alternative.get("name", ""), "url": url}, place, intent.origin)
                                or not alternative.get("evidence")):
                            continue
                        try:
                            evidence = Evidence.model_validate(alternative["evidence"])
                            if not evidence.source_url or url_identity(evidence.source_url) != url_identity(url):
                                continue
                        except (ValueError, TypeError):
                            continue
                        parts = self.providers._inventory_components(place, origin, intent)
                        self.providers._apply_indexed_hotel(parts[1], alternative, place)
                        bundle = ComponentBundle(flight=original.flight.model_copy(deep=True), stay=parts[1], transfer=parts[2])
                        bundle.candidate_id = candidate_identity(bundle, place, origin, intent)
                        candidates.append(bundle)
                        seen.add(url_identity(url))
            return candidates

    @traced("travel_data.regional_events")
    async def regional_events(self, place, intent):
        return await self.providers.regional_events(place, intent)

    async def price_documents(self, place, origin, intent, current):
        return await self.providers.price_documents(place, origin, intent, ComponentBundle.from_components(current))

    @traced("travel_data.refresh_inventory")
    async def refresh_inventory(self, place, origin, intent, current):
        result = ComponentBundle.from_components(current)
        await self.providers.pricing.apply(place, origin, intent, tuple(result), refresh=True)
        return result
