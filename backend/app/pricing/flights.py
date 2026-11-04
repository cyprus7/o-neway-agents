"""Gateway-owned flight-search adapter. The remote service never owns this cache.

Only the lightweight versioned values are shared with the independent service;
no browser, service lifecycle or provider recipe is imported by the gateway.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import os
import re
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from one_request_telemetry import internal_headers, queued, set_attributes, span, traced

from flight_search.models import FlightConstraints, FlightSearchRequest, FlightSearchResponse, SelectedRoute, search_hash
from .models import LowerBoundPrice, PriceObservation, QuoteScope
from .normalization import request_context_hash
from ..travel_dates import arrival_date_required


MAX_RESPONSE_BYTES = 256 * 1024
SERVICE_SECONDS = 35.5
ENTITY_PREFIXES = {'google_flights': 'google-flight:', 'yandex_avia': 'yandex-flight:'}
FLIGHT_MESSAGES = {
    'flight_reference': 'Прочитан ориентир за выбранный маршрут туда и обратно; окончательное предложение продавца, наличие, багаж и полный состав сборов не подтверждены.',
    'flight_unsupported_route': 'Браузерный источник пока не поддерживает эти аэропорты.',
    'flight_unsupported_party': 'Браузерный поиск пока поддерживает одного или двух взрослых без детей.',
    'flight_only_eur': 'Браузерный источник пока проверяем только в евро.',
    'flight_unsupported_cabin': 'Браузерный источник пока проверяем только для экономкласса.',
    'flight_unsupported_constraints': 'Источник не подтвердил обязательные условия перелёта.',
    'flight_invalid_deadline': 'Время на проверку перелёта истекло.',
    'flight_overloaded': 'Источник перелётов занят; повторите проверку позже.',
    'flight_browser_unavailable': 'Браузерный источник перелётов временно недоступен.',
    'flight_challenge': 'Источник перелётов запросил проверку посетителя; поиск остановлен.',
    'flight_login': 'Источник перелётов запросил вход; поиск остановлен.',
    'flight_scope_not_verified': 'Источник не подтвердил все параметры поиска перелёта.',
    'flight_route_mismatch': 'Аэропорты найденного маршрута отличаются от запроса.',
    'flight_date_mismatch': 'Даты найденного перелёта отличаются от запроса.',
    'flight_party_mismatch': 'Источник не подтвердил нужный состав путешественников.',
    'flight_currency_mismatch': 'Валюта источника отличается от валюты поездки.',
    'flight_cabin_mismatch': 'Источник не подтвердил нужный класс перелёта.',
    'flight_no_compatible_itinerary': 'Источник не вернул маршрут, совместимый с запросом.',
    'flight_target_not_found': 'Выбранный маршрут не найден при обновлении; другой перелёт не подставлялся.',
    'flight_price_not_verified': 'Источник не подтвердил единицу или сумму цены выбранного маршрута.',
    'flight_source_unavailable': 'Источник перелётов не загрузил подходящие результаты.',
    'flight_parse_error': 'Не удалось проверить ответ источника перелётов.',
    'flight_timeout': 'Источник перелётов не ответил за отведённое время.',
    'flight_cancelled': 'Проверка перелёта отменена.',
    'flight_context_mismatch': 'Ответ источника относится к другому маршруту или составу поездки.',
    'flight_response_too_large': 'Ответ источника перелётов превысил допустимый размер.',
    'not_configured': 'Сервис поиска перелётов не подключён.',
    'policy_disabled': 'Внешние источники перелётов отключены.',
}


def now():
    return datetime.now(timezone.utc)


def flight_constraints(intent):
    return FlightConstraints(arrival_date=arrival_date_required(intent), direct_required='direct_flight' in intent.hard_constraints,
        prefer_direct=bool(intent.direct_flight_preferred), max_flight_minutes=intent.max_travel_minutes)


def itinerary_target(entity):
    if not isinstance(entity, str):
        return None
    match = re.fullmatch(r'(?:google|yandex)-flight:([a-f0-9]{64})', entity)
    return match[1] if match else None


def entity_id(route):
    return ENTITY_PREFIXES[route.provider_id] + route.itinerary_id


def describe_route(route):
    def leg(value):
        stops = 'без пересадок' if value.stops == 0 else f'пересадок: {value.stops}'
        via = ('; через ' + ', '.join(connection.airport_name for connection in value.observed_connections)
               if value.observed_connections else '')
        return (f'{value.origin} {value.departure_date} {value.departure_time} → '
                f'{value.destination} {value.arrival_date} {value.arrival_time}; '
                f'{value.duration_minutes} мин.; {stops}{via}; {", ".join(value.carriers)}')
    return (leg(route.outbound) + '. Обратно: ' + leg(route.inbound)
        + '. Время местное; часовые пояса и наземные состыковки не подтверждены.')


class BrowserFlightSearch:
    def __init__(self, client, store):
        self.client, self.store = client, store
        self.url = os.getenv('FLIGHT_SEARCH_URL', '').rstrip('/')
        self.web_enabled = os.getenv('WEB_FALLBACK_ENABLED', '1') == '1'
        try:
            parsed = urlsplit(self.url)
            if (parsed.scheme != 'http' or parsed.hostname not in ('flight-search', 'localhost', '127.0.0.1', '::1')
                    or parsed.username or parsed.password or parsed.path not in ('', '/')
                    or parsed.query or parsed.fragment or parsed.port is not None and not 1 <= parsed.port <= 65535):
                self.url = ''
        except ValueError:
            self.url = ''
        self.status = 'degraded' if self.url else 'disabled'
        self._slots = asyncio.Semaphore(2)

    def request(self, context, intent):
        return FlightSearchRequest(request_id=uuid4().hex, deadline_at=now() + timedelta(seconds=35),
            request_context_hash=request_context_hash(context), request_context=context.model_dump(mode='json'),
            constraints=flight_constraints(intent), target_itinerary_id=itinerary_target(context.entity_id))

    def key(self, context, intent):
        # Preferences and hard bounds are deliberately separate from the existing
        # pricing hash, but participate in both cache and parent singleflight.
        return 'flight-search-v1:' + search_hash(self.request(context, intent))

    @staticmethod
    def _valid_route(route, context, intent, *, allow_expired=False):
        if (route.origin != context.origin or route.destination != context.destination
                or route.date_from != context.date_from or route.date_to != context.date_to
                or route.adults != context.adults or tuple(route.children_ages) != tuple(context.children_ages)
                or route.currency != context.currency or route.cabin != flight_constraints(intent).cabin
                or context.trip_type != 'roundtrip' or not allow_expired and route.expires_at <= now()
                or route.observed_at > now() + timedelta(seconds=5) or route.retrieved_at > now() + timedelta(seconds=5)
                or len(route.source_url) > 4000):
            return False
        if context.target_policy == 'fixed_entity' and entity_id(route) != context.entity_id:
            return False
        constraints = flight_constraints(intent)
        if constraints.arrival_date is not None and route.outbound.arrival_date != constraints.arrival_date:
            return False
        if constraints.direct_required and (route.outbound.stops or route.inbound.stops):
            return False
        return not constraints.max_flight_minutes or max(route.outbound.duration_minutes,
            route.inbound.duration_minutes) <= constraints.max_flight_minutes

    @staticmethod
    def _reference(route, context, observation_id=None):
        return PriceObservation(id=observation_id or uuid4().hex, provider_id=route.provider_id,
            request_context_hash=request_context_hash(context),
            scope=QuoteScope(component='flight', entity_level='route', entity_id=entity_id(route),
                origin=route.origin, destination=route.destination, date_from=route.date_from, date_to=route.date_to,
                adults=route.adults, children_ages=route.children_ages, currency=route.currency,
                trip_type='roundtrip', unit='group_trip', fare_class=route.cabin,
                taxes='included', mandatory_fees='unknown', context_verified=False,
                unverified_fields=('final_supplier_offer', 'availability', 'mandatory_fees', 'baggage')),
            value=LowerBoundPrice(min=route.price.amount, currency=route.currency), source_url=route.source_url,
            observed_at=route.observed_at, retrieved_at=route.retrieved_at, expires_at=route.expires_at,
            outcome='reference', reason=FLIGHT_MESSAGES['flight_reference'],
            unit_label=f'за {route.adults} взр., туда-обратно · экономкласс',
            included_charges=('Стандартные налоги по подписи поискового источника',),
            unknown_charges=('Окончательное предложение продавца и все обязательные сборы', 'Багаж и правила тарифа',
                             'Наличие мест и применимость к группе'), availability_verified=False, component_complete=False)

    @staticmethod
    def _diagnostic(code, *, outcome='unavailable', service_dispatched=False):
        return {'provider_id': 'google_flights', 'outcome': outcome, 'diagnostic_code': code,
            'message': FLIGHT_MESSAGES[code], 'delivery': 'not_attempted',
            # A private POST is not evidence of a provider navigation. Without
            # a valid source diagnostic its external dispatch remains unknown.
            'dispatched': False, 'service_dispatched': service_dispatched,
            'external_dispatched': None if service_dispatched else False,
            'checked_at': now().isoformat(), 'retrieved_at': None}

    @traced('flight.lookup')
    async def search(self, context, intent):
        if not self.web_enabled:
            row = self._diagnostic('policy_disabled', outcome='policy_disabled')
            row['delivery'] = 'disabled'
            return None, [row], None
        if not self.url:
            return None, [self._diagnostic('not_configured', outcome='not_configured')], None
        request = self.request(context, intent)
        key = 'flight-search-v1:' + search_hash(request)
        with span('flight.cache'):
            cached = self.store.cache_get(key)
            if cached:
                try:
                    route = SelectedRoute.model_validate(cached[0]['route'])
                    if self._valid_route(route, context, intent):
                        quote = self._reference(route, context, cached[0]['observation_id'])
                        rows = [dict(row, delivery='cache_reuse', dispatched=False,
                            service_dispatched=False, external_dispatched=False) for row in cached[0]['diagnostics']]
                        self.store.increment('flight_search_cache_hits')
                        set_attributes(cache_hit=True, outcome='cached')
                        return quote, rows, {'route': route, 'delivery': 'cache_reuse'}
                except (ValueError, TypeError, KeyError):
                    pass
            set_attributes(cache_hit=False, reason='cache_miss')
        dispatched = False
        reported_rows = None
        code, outcome = 'flight_parse_error', 'parse_error'
        try:
            async with asyncio.timeout(SERVICE_SECONDS):
                async with queued(self._slots, 'flight.queue'):
                    # Queue time is deducted rather than granting a fresh 35s.
                    remaining = (request.deadline_at - now()).total_seconds()
                    if remaining <= 0:
                        raise TimeoutError
                    with span('flight.client', kind='client'):
                        self.store.increment('flight_search_requests')
                        dispatched = True
                        async with self.client.stream('POST', self.url + '/v1/search',
                                json=request.model_dump(mode='json'), headers=internal_headers(),
                                timeout=remaining, follow_redirects=False) as response:
                            set_attributes(http_response_status_code=response.status_code)
                            response.raise_for_status()
                            declared = response.headers.get('content-length')
                            if declared is not None:
                                if not declared.isdecimal():
                                    raise ValueError('invalid_content_length')
                                if int(declared) > MAX_RESPONSE_BYTES:
                                    code = 'flight_response_too_large'
                                    raise ValueError('response_too_large')
                            if response.headers.get('content-type', '').split(';')[0].strip().lower() != 'application/json':
                                raise ValueError('invalid_content_type')
                            body = bytearray()
                            async for chunk in response.aiter_bytes(chunk_size=16 * 1024):
                                body.extend(chunk)
                                if len(body) > MAX_RESPONSE_BYTES:
                                    code = 'flight_response_too_large'
                                    raise ValueError('response_too_large')
            payload = FlightSearchResponse.model_validate_json(body)
            if payload.request_id != request.request_id or payload.request_context_hash != request.request_context_hash:
                code, outcome = 'flight_context_mismatch', 'context_mismatch'
                raise ValueError('response_context_mismatch')
            self.status = 'degraded' if payload.diagnostic_code == 'flight_browser_unavailable' else 'ready'
            rows = []
            for row in payload.diagnostics:
                value = row.model_dump(mode='json')
                value.update(message=FLIGHT_MESSAGES[row.diagnostic_code], service_dispatched=True,
                    delivery='live_fetch' if row.external_dispatched else 'not_attempted')
                rows.append(value)
            if not rows:
                row = self._diagnostic(payload.diagnostic_code, outcome=payload.outcome, service_dispatched=True)
                rows.append(row)
            reported_rows = rows
            if payload.route is None:
                set_attributes(outcome=payload.outcome, reason=payload.diagnostic_code)
                return None, rows, None
            route = payload.route
            if not self._valid_route(route, context, intent):
                code, outcome = 'flight_context_mismatch', 'context_mismatch'
                raise ValueError('route_context_mismatch')
            quote = self._reference(route, context)
            ttl = int((route.expires_at - now()).total_seconds())
            cached = {'route': route.model_dump(mode='json'), 'diagnostics': rows, 'observation_id': quote.id}
            if ttl > 0:
                self.store.cache_put(key, cached, ttl)
                if context.target_policy == 'candidate_allowed':
                    # The exact observed route may satisfy a subsequent fixed
                    # refresh; narrowing does not invent a new source check.
                    fixed = context.model_copy(update={'target_policy': 'fixed_entity', 'entity_id': entity_id(route)})
                    self.store.cache_put(self.key(fixed, intent), cached, ttl)
            set_attributes(outcome='reference', reason=payload.diagnostic_code)
            return quote, rows, {'route': route, 'delivery': 'live_fetch'}
        except httpx.HTTPStatusError:
            code, outcome = 'flight_source_unavailable', 'unavailable'
        except (httpx.HTTPError, TimeoutError):
            code, outcome = 'flight_timeout', 'timeout'
        except (ValueError, TypeError, KeyError):
            pass
        self.status = 'degraded'
        set_attributes(outcome=outcome, reason=code)
        rows = ([dict(row, outcome=outcome, diagnostic_code=code, message=FLIGHT_MESSAGES[code]) for row in reported_rows]
            if reported_rows else [self._diagnostic(code, outcome=outcome, service_dispatched=dispatched)])
        return None, rows, None
