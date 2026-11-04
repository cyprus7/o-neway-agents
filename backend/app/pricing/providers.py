"""Public price sources are always independent of optional credentialed integrations."""
import asyncio
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import os
import re
import unicodedata
from typing import Any
from urllib.parse import urlencode, urlsplit
from uuid import uuid4

import httpx
from one_request_telemetry import internal_headers, queued, set_attributes, span, traced

from .airlines import PublicAirlinePrices
from .flights import BrowserFlightSearch, FLIGHT_MESSAGES, describe_route, entity_id, itinerary_target
from .lodging import PublicHotelPrices, SOURCES as HOTEL_TARIFF_SOURCES
from .models import (LowerBoundPrice, PointPrice, PriceObservation, QuoteScope, RangePrice,
                     RequestContext, UnknownPrice)
from .normalization import normalize_observation, request_context_hash
from ..travel_dates import departure_dates, flight_departure_date, arrival_date_required


def now():
    return datetime.now(timezone.utc)


BROWSER_MESSAGES = {
    'ostrovok_currency_control_unavailable':'Островок не подтвердил выбранную валюту; цена отклонена.',
    'ostrovok_no_property_links':'Островок не показал подходящих вариантов размещения.',
    'ostrovok_dateless_results':'Островок сбросил даты поиска; цена отклонена.',
    'ostrovok_property_context_mismatch':'Островок открыл другой отель или город; цена отклонена.',
    'ostrovok_dates_not_verified':'Не удалось подтвердить даты и год предложения Островка.',
    'ostrovok_party_not_verified':'Островок не подтвердил состав гостей; цена отклонена.',
    'ostrovok_rooms_not_verified':'Островок не подтвердил число номеров; цена отклонена.',
    'ostrovok_current_rate_not_found':'Не удалось прочитать текущий тариф Островка.',
    'ostrovok_public_rate_charges_unverified':'Прочитан тариф Островка; сборы, наличие и применимость требуют проверки.',
    'provider_target_mismatch':'Выбранный отель относится к другому источнику; замена не выполнялась.',
    'unsupported_destination':'Для этого направления браузерный источник пока не подключён.',
    'destination_city_airport_mismatch':'Город размещения не совпал с направлением источника; чужая цена отклонена.',
    'only_eur_supported':'Этот источник пока проверяем только в евро.',
    'single_room_adult_only_recipe':'Браузерный поиск пока поддерживает один номер и 1–2 взрослых без детей.',
    'unsupported_duration':'Браузерный поиск пока поддерживает поездки до 30 ночей.',
    'challenge_detected':'Источник запросил проверку посетителя; автоматическая проверка остановлена.',
    'invalid_deadline':'Время на проверку истекло; повторите запрос.',
    'component_deadline':'Источник не успел ответить за отведённое время.',
    'browser_sandbox_unavailable':'Локальный браузер временно недоступен.',
    'no_property_links':'Источник не показал подходящих вариантов размещения.',
    'selected_property_not_found':'Выбранный отель не появился в новой выдаче; другой объект не подставлялся.',
    'google_property_results_unavailable':'Источник временно не загрузил предложения выбранного отеля.',
    'calendar_year_or_selection_mismatch':'Не удалось подтвердить выбранные даты и год; цена отклонена.',
    'ui_context_not_applied':'Источник не применил даты или состав поездки; цена отклонена.',
    'detail_dates_changed':'Даты предложения отличаются от запроса; цена отклонена.',
    'eur_display_not_verified':'Валюту предложения не удалось подтвердить.',
    'public_stay_total_room_scope_unverified':'Прочитана цена за весь срок; состав тарифа и наличие требуют проверки.',
    'unsupported_property_mapping':'Для этого объекта нет подтверждённой связи с подключённым источником цен; другой отель не подставлялся.',
    'invalid_discovery_terms':'Пожелания для поиска не прошли ограниченную проверку формата.',
    'not_configured':'Локальный браузерный источник недоступен.',
    'worker_http_error':'Браузерный источник отклонил запрос или временно недоступен.',
    'worker_timeout':'Браузерный источник не ответил за отведённое время.',
    'worker_parse_error':'Не удалось разобрать ответ браузерного источника.',
    'worker_context_mismatch':'Ответ браузерного источника относится к другому объекту или запросу.',
    'observed':'Прочитано наблюдение публичного источника; применимость и наличие требуют проверки.',
}

OSTROVOK_DESTINATIONS = {
    'ATH': ('/hotel/greece/athens/', ('Athens', 'Афины')),
    'IST': ('/hotel/turkey/istanbul/', ('Istanbul', 'Стамбул', 'İstanbul')),
    'SAW': ('/hotel/turkey/istanbul/', ('Istanbul', 'Стамбул', 'İstanbul')),
    'PRG': ('/hotel/czech_republic/prague/', ('Prague', 'Прага', 'Praha')),
    'TBS': ('/hotel/georgia/tbilisi/', ('Tbilisi', 'Тбилиси')),
    'BER': ('/hotel/germany/berlin/', ('Berlin', 'Берлин')),
}
GOOGLE_DESTINATIONS = ('ATH', 'IST', 'SAW', 'TBS', 'LCA', 'PFO', 'AYT', 'TFS', 'HRG', 'SSH',
    'DXB', 'LIS', 'AGP', 'NCE', 'FCO', 'BUD', 'PRG', 'VIE', 'KEF', 'BCN', 'FNC', 'SOF', 'LJU', 'HEL', 'DPS', 'BER')
_SKIPPED_BROWSER_CODES = frozenset({'unsupported_destination', 'destination_city_airport_mismatch',
    'only_eur_supported', 'single_room_adult_only_recipe', 'unsupported_duration',
    'provider_target_mismatch', 'invalid_discovery_terms', 'invalid_deadline', 'browser_sandbox_unavailable'})


def lodging_discovery_terms(intent) -> tuple[str, ...]:
    """Bound lodging wishes without cutting a qualifier/negation out of a term."""
    terms, total = [], 0
    candidates = list(getattr(intent, 'hotel_requirements', ()) or ())
    if getattr(intent, 'pets', None):
        candidates.append('pet friendly')
    for value in candidates:
        if not isinstance(value, str) or any(unicodedata.category(char).startswith('C') for char in value):
            continue
        value = ' '.join(value.split())
        if (not value or len(value) > 80 or value in terms
                or re.search(r'[a-z][a-z0-9+.-]*:/{1,2}|www\.|\b[a-z0-9-]+\.[a-z]{2,63}(?:[/\s:]|$)|[<>]|\b(?:site|file|javascript):', value, re.I)
                or re.match(r'^(?:transport|transport_requirement|route|weather|climate|exclude_destination|exclude_country):', value, re.I)):
            continue
        if len(terms) == 4 or total + len(value) > 200:
            continue
        terms.append(value)
        total += len(value)
    return tuple(terms)


def ostrovok_property_target(source_url, place):
    """Derive only a same-provider reviewed-city identity, never a name mapping."""
    destination = OSTROVOK_DESTINATIONS.get(place.airport)
    if not destination or place.name.casefold() not in {name.casefold() for name in destination[1]}:
        return None
    if not isinstance(source_url, str) or len(source_url) > 3000:
        return None
    try:
        parsed = urlsplit(source_url)
        if (parsed.scheme != 'https' or parsed.hostname not in ('ostrovok.ru', 'www.ostrovok.ru')
                or parsed.port not in (None, 443) or parsed.username or parsed.password or parsed.fragment
                or len(parsed.path) > 256
                or not re.fullmatch(re.escape(destination[0]) + r'mid[0-9]{1,12}/[a-zA-Z0-9_-]{1,160}/', parsed.path)):
            return None
        return 'ostrovok:' + hashlib.sha256(parsed.path.encode()).hexdigest()[:24], parsed.path
    except ValueError:
        return None


def attempt(provider, outcome, code, message, *, delivery='not_attempted', **extra):
    return {'provider_id': provider, 'outcome': outcome, 'diagnostic_code': code, 'message': message,
            'delivery': delivery, 'dispatched': delivery == 'live_fetch', **extra}


def pricing_delivery(diagnostics, *, retained=False, source_delivery=None):
    """Delivery describes source work, including empty results, not job completion."""
    values = {row.get('delivery') for row in diagnostics}
    if source_delivery:
        values.add(source_delivery)
    if retained:
        values.add('cache_reuse')
    if 'cached' in values:
        values.add('cache_reuse')
    if 'live' in values:
        values.add('live_fetch')
    active = values & {'live_fetch', 'cache_reuse'}
    if len(active) == 2 or 'mixed' in values:
        return 'mixed'
    if active:
        return next(iter(active))
    return 'disabled' if 'disabled' in values else 'not_attempted'


def observation_delivery(quote, diagnostics, *, retained=False, source_delivery=None):
    """Observation freshness is independent from other attempted providers."""
    if retained:
        return 'cache_reuse'
    if source_delivery in ('live', 'live_fetch', 'cached', 'cache_reuse'):
        return 'cache_reuse' if source_delivery in ('cached', 'cache_reuse') else 'live_fetch'
    matching = [row for row in diagnostics if row.get('provider_id') == quote.provider_id]
    if any(row.get('delivery') in ('cache_reuse', 'cached') for row in matching):
        return 'cache_reuse'
    return 'live_fetch'


def context_for(component, place, origin, intent, *, entity_id=None, departure_date=None):
    # Shared domain construction keeps hotel dates separate from flight dates.
    from ..domain.travel_data import context_for as shared_context_for
    return shared_context_for(component, place, origin, intent, entity_id=entity_id,
                              departure_date=departure_date)


def unknown(provider, context, reason, outcome='unavailable'):
    permitted = {'no_offers','blocked','requires_login','timeout','parse_error','context_mismatch','not_configured',
                 'policy_disabled','unsupported_party','currency_mismatch','unavailable'}
    return PriceObservation(provider_id=provider, request_context_hash=request_context_hash(context),
        value=UnknownPrice(reason=reason), outcome=outcome if outcome in permitted else 'unavailable', reason=reason)


class KeylessPricing:
    def __init__(self, client, store):
        self.client, self.store = client, store
        self.enabled = os.getenv('KEYLESS_PRICING_ENABLED', '0') == '1'
        self.web_fallback_enabled = os.getenv('WEB_FALLBACK_ENABLED', '1') == '1'
        self.airlines = PublicAirlinePrices(client, store)
        self.flights = BrowserFlightSearch(client, store)
        self.hotels = PublicHotelPrices(client, store)
        self.worker_url = os.getenv('PRICE_WORKER_URL', '').rstrip('/')
        try:
            parsed = urlsplit(self.worker_url)
            if parsed.scheme != 'http' or parsed.hostname not in ('price-worker', 'localhost', '127.0.0.1', '::1') or parsed.username or parsed.password or parsed.path not in ('', '/'):
                self.worker_url = ''
        except ValueError:
            self.worker_url = ''
        self.worker_status = 'degraded' if self.worker_url else 'disabled'
        self._worker_slots = asyncio.Semaphore(2)
        self._inflight: dict[str, asyncio.Task] = {}
        self._waiters: dict[asyncio.Task, int] = {}
        self._source_slots = asyncio.Semaphore(8)

    def capabilities(self):
        enabled = self.enabled and self.web_fallback_enabled
        worker_status = self.worker_status if self.web_fallback_enabled else 'disabled'
        return {'refresh_enabled': enabled, 'browser_worker': worker_status,
                'external_fallback_enabled': self.web_fallback_enabled,
                'providers': [{'id': name, 'configured': enabled, 'supported': supported,
                               'status': 'keyless' if enabled else 'disabled'} for name, supported in
                              [('aegean_public', 'published route/date fares'), ('ryanair_farefinder', 'published route/date fares'),
                               ('direct_hotel_tariffs', 'published nightly references')]] +
                              [{'id': 'google_flights', 'configured': bool(self.flights.url) and enabled,
                                'supported': 'reviewed airports, EUR, economy, roundtrip, 1-2 adults; source references only',
                                'status': self.flights.status if enabled else 'disabled'},
                              {'id': 'google_hotels', 'configured': bool(self.worker_url) and enabled, 'supported': 'reviewed cities, EUR, one room, 1-2 adults, up to 30 nights',
                               'destination_iata': list(GOOGLE_DESTINATIONS), 'status': worker_status},
                              {'id': 'ostrovok', 'configured': bool(self.worker_url) and enabled, 'supported': 'authorized bounded browser search; Athens/Istanbul/Prague/Tbilisi/Berlin, EUR, one room, 1-2 adults',
                               'destination_iata': list(OSTROVOK_DESTINATIONS), 'status': worker_status}], 'estimate_policy_version': 'keyless-reference-v1'}

    def _flight_reference(self, fact, context):
        source = fact['scope']
        scope = QuoteScope(component='flight', entity_level='route', origin=source['origin_iata'],
            destination=source['destination_iata'], date_from=source['date_from'], date_to=source.get('date_to'),
            trip_type=fact['trip_type'], unit='person_trip', currency=fact['currency'], taxes='unknown',
            context_verified=False, unverified_fields=('party_availability','mandatory_fees','baggage'),
            assumptions=('Опубликованный базовый тариф на одного взрослого; не цена всей группы',))
        return PriceObservation(provider_id=fact['provider_id'], request_context_hash=request_context_hash(context),
            scope=scope, value=LowerBoundPrice(min=fact['amount'], currency=fact['currency']),
            source_url=fact['source_url'], observed_at=fact['observed_at'], retrieved_at=fact['retrieved_at'],
            expires_at=fact['expires_at'], outcome='reference', reason=fact['message'],
            unit_label=('на 1 взрослого, два отдельных перелёта' if fact['provider_id']=='aegean_public' else 'на 1 взрослого, туда-обратно') if fact['trip_type']=='roundtrip' else 'на 1 взрослого, в одну сторону',
            included_charges=tuple(fact.get('inclusions', [])), excluded_charges=tuple(fact.get('exclusions', [])),
            unknown_charges=('Налоги/обязательные сборы и наличие для всей группы',),
            availability_verified=False, evidence_ids=tuple(fact.get('source_observation_ids', [])))

    def _hotel_reference(self, fact, context):
        low, high = fact['amount_from'], fact['amount_to']
        value = PointPrice(amount=low,currency=fact['currency']) if Decimal(low)==Decimal(high) else RangePrice(min=low,max=high,currency=fact['currency'])
        retrieved = fact.get('retrieved_at') or fact['observed_at']
        return PriceObservation(provider_id='direct_hotel_tariffs', request_context_hash=request_context_hash(context),
            scope=QuoteScope(component='stay', entity_level='property', entity_id=fact['source_url'],
                provider_entity_id=fact['property_name'], destination=context.destination, destination_city=context.destination_city, rooms=1,
                unit='room_night', currency=fact['currency'], taxes=fact.get('taxes','unknown'), context_verified=False,
                unverified_fields=('calendar_year','requested_dates','party','availability'),
                assumptions=tuple(fact.get('limitations', []))),
            value=value, source_url=fact['source_url'], observed_at=fact['observed_at'], retrieved_at=retrieved,
            expires_at=datetime.fromisoformat(fact['observed_at']) + timedelta(hours=6), outcome='reference',
            reason='Опубликованный тариф отеля; календарный год, цена на выбранные даты и размещение вашей группы не подтверждены.',
            unit_label='за 1 номер / ночь · ' + fact.get('room_type','тип номера требует проверки'),
            included_charges=('Завтрак',) if fact.get('breakfast_included') else (),
            unknown_charges=('Доступность на выбранные даты и обязательные доплаты',), availability_verified=False)

    @traced("pricing.browser")
    async def _browser(self, context, place, provider='google_hotels', *, target_property_path=None, discovery_terms=()):
        worker_dispatched = False
        def browser_attempt(source, outcome, code, message, **metadata):
            # This client dispatches a private worker POST. Browser navigation
            # is a different fact and cannot be inferred from a worker timeout.
            result = attempt(source, outcome, code, message, **metadata)
            result.update(dispatch_scope='worker', dispatched=worker_dispatched,
                          worker_dispatched=worker_dispatched)
            result['external_dispatched'] = False if not worker_dispatched or code in _SKIPPED_BROWSER_CODES else None
            return result
        set_attributes(provider=provider if provider in ('google_hotels', 'ostrovok') else 'other')
        # Discovery may resolve a country or city without an airport. The
        # deployed worker contract requires IATA; do not send an invalid body.
        if not isinstance(place.airport, str) or re.fullmatch(r'[A-Z]{3}', place.airport) is None:
            code = 'unsupported_destination'
            set_attributes(outcome='context_mismatch', reason=code)
            message = BROWSER_MESSAGES[code]
            return unknown(provider, context, message, 'context_mismatch'), [
                browser_attempt(provider, 'context_mismatch', code, message, delivery='not_attempted')]
        prefix = 'google-hotel:' if provider == 'google_hotels' else 'ostrovok:'
        if context.entity_id and not context.entity_id.startswith(prefix):
            code = 'provider_target_mismatch'
            set_attributes(outcome='context_mismatch', reason=code)
            message = BROWSER_MESSAGES[code]
            return unknown(provider, context, message, 'context_mismatch'), [browser_attempt(provider, 'context_mismatch', code, message)]
        if target_property_path is not None:
            target = (ostrovok_property_target('https://ostrovok.ru' + target_property_path, place)
                      if isinstance(target_property_path, str) and provider == 'ostrovok' else None)
            if not target or target[0] != context.entity_id or target[1] != target_property_path:
                code = 'provider_target_mismatch'
                set_attributes(outcome='context_mismatch', reason=code)
                return unknown(provider, context, BROWSER_MESSAGES[code], 'context_mismatch'), [browser_attempt(provider, 'context_mismatch', code, BROWSER_MESSAGES[code])]
        if not self.worker_url:
            code = 'not_configured'
            set_attributes(outcome='not_configured', reason=code)
            return unknown(provider, context, BROWSER_MESSAGES[code], 'not_configured'), [browser_attempt(provider, 'not_configured', code, BROWSER_MESSAGES[code], delivery='disabled')]
        key = ('google-hotels-v2:' if provider == 'google_hotels' else 'ostrovok-v1:') + request_context_hash(context) + ':' + place.name.casefold()
        if discovery_terms:
            key += ':wishes:' + hashlib.sha256('\n'.join(discovery_terms).encode()).hexdigest()
        cached = self.store.cache_get(key)
        if cached:
            try:
                candidate = PriceObservation.model_validate(cached[0])
                if candidate.provider_id == provider and candidate.request_context_hash == request_context_hash(context) and candidate.freshness() != 'stale':
                    set_attributes(cache_hit=True, outcome="cached")
                    return candidate, [browser_attempt(provider, candidate.outcome, 'observed', candidate.reason or '',
                        delivery='cache_reuse', retrieved_at=candidate.retrieved_at.isoformat() if candidate.retrieved_at else None)]
            except (ValueError, TypeError):
                pass
        body = {'request_id': uuid4().hex, 'provider_id': provider,
            'request_context_hash': request_context_hash(context),
            'request_context': {'destination': place.name, 'destination_iata': place.airport,
                'date_from': str(context.date_from), 'date_to': str(context.date_to), 'adults':context.adults,
                'children_ages':list(context.children_ages), 'rooms':context.rooms, 'currency':context.currency}}
        if context.entity_id:
            body['request_context']['target_entity_id'] = context.entity_id
        if target_property_path is not None:
            body['request_context']['target_property_path'] = target_property_path
        if discovery_terms:
            body['request_context']['discovery_terms'] = list(discovery_terms)
        checked_at = now().isoformat()
        try:
            # Queue time belongs to the same deadline, including with 3+ options.
            async with asyncio.timeout(30):
                async with queued(self._worker_slots, "pricing.worker.queue"):
                    body['deadline_at'] = (now()+timedelta(seconds=21)).isoformat()
                    with span("pricing.worker.client", kind="client"):
                        worker_dispatched = True
                        response = await self.client.post(self.worker_url+'/v1/quotes', json=body,
                            headers=internal_headers(), timeout=22, follow_redirects=False)
                        set_attributes(http_response_status_code=response.status_code)
            response.raise_for_status()
            payload = response.json()
            self.worker_status = 'degraded' if payload.get('diagnostic_code') == 'browser_sandbox_unavailable' else 'ready'
            if payload.get('request_context_hash') != request_context_hash(context):
                code = 'worker_context_mismatch'
                set_attributes(outcome='context_mismatch', reason=code)
                return unknown(provider,context,BROWSER_MESSAGES[code],'context_mismatch'), [browser_attempt(provider, 'context_mismatch', code, BROWSER_MESSAGES[code], delivery='live_fetch', checked_at=checked_at)]
            observation = payload.get('observation')
            code = payload.get('diagnostic_code')
            code = ('observed' if code is None and isinstance(observation, dict)
                    else code if code in BROWSER_MESSAGES else 'worker_parse_error')
            message = BROWSER_MESSAGES[code]
            outcome = payload.get('outcome')
            outcome = outcome if outcome in {'quoted', 'reference', 'no_offers', 'blocked', 'requires_login', 'timeout',
                'parse_error', 'context_mismatch', 'not_configured', 'policy_disabled', 'unsupported_party', 'currency_mismatch', 'unavailable'} else 'parse_error'
            delivery = 'not_attempted' if code in _SKIPPED_BROWSER_CODES else 'live_fetch'
            diagnostics = [browser_attempt(provider, outcome, code, message, delivery=delivery, checked_at=checked_at)]
            if type(payload.get('actions')) is int and payload['actions'] > 0:
                diagnostics[0]['external_dispatched'] = True
            if not isinstance(observation, dict):
                set_attributes(outcome=outcome, reason=code)
                return unknown(provider,context,message,outcome), diagnostics
            # The recipe must return actual page scope, never the input copied as proof.
            quote = PriceObservation.model_validate(observation)
            scope = quote.scope
            conflicts = quote.provider_id != provider or scope is None or scope.component != 'stay' or scope.destination != context.destination
            if scope:
                if context.destination_city is not None and scope.destination_city is not None:
                    conflicts = conflicts or scope.destination_city.casefold()!=context.destination_city.casefold()
                conflicts = conflicts or any(getattr(scope,name) is not None and getattr(scope,name) != getattr(context,name)
                    for name in ('date_from','date_to','currency','adults','rooms'))
                conflicts = conflicts or (context.target_policy=='fixed_entity' and scope.entity_id!=context.entity_id)
            conflicts = conflicts or (quote.request_context_hash is not None and quote.request_context_hash!=request_context_hash(context))
            if conflicts:
                set_attributes(outcome='context_mismatch', reason='worker_context_mismatch')
                diagnostics[0].update(outcome='context_mismatch', diagnostic_code='worker_context_mismatch', message=BROWSER_MESSAGES['worker_context_mismatch'])
                return unknown(provider,context,'Браузер вернул другой объект, даты или состав поездки.','context_mismatch'), diagnostics
            quote = normalize_observation(quote, context)
            if quote.outcome == 'context_mismatch':
                set_attributes(outcome="context_mismatch")
                return unknown(provider,context,'Цена источника относится к другому отелю, датам или составу группы.','context_mismatch'), diagnostics
            if quote.value.kind != 'unknown' and quote.expires_at:
                self.store.cache_put(key, quote.model_dump(mode='json'), max(1,int((quote.expires_at-now()).total_seconds())))
            diagnostics[0]['retrieved_at'] = quote.retrieved_at.isoformat() if quote.retrieved_at else None
            diagnostics[0]['external_dispatched'] = True
            set_attributes(cache_hit=False, outcome=quote.outcome, reason=code)
            return quote, diagnostics
        except (ValueError, TypeError):
            set_attributes(outcome="parse_error")
            self.worker_status = 'degraded'
            return unknown(provider,context,BROWSER_MESSAGES['worker_parse_error'],'parse_error'), [browser_attempt(provider, 'parse_error', 'worker_parse_error', BROWSER_MESSAGES['worker_parse_error'], delivery='live_fetch' if worker_dispatched else 'not_attempted', checked_at=checked_at)]
        except (httpx.TimeoutException, TimeoutError):
            set_attributes(outcome="timeout")
            self.worker_status = 'degraded'
            return unknown(provider,context,BROWSER_MESSAGES['worker_timeout'],'timeout'), [browser_attempt(provider, 'timeout', 'worker_timeout', BROWSER_MESSAGES['worker_timeout'], delivery='live_fetch' if worker_dispatched else 'not_attempted', checked_at=checked_at)]
        except httpx.HTTPError:
            # A fast HTTP rejection/connection error is not evidence of a timeout.
            # Keep response bodies and exception text out of public diagnostics.
            code = 'worker_http_error'
            set_attributes(outcome='unavailable', reason='http_error')
            self.worker_status = 'degraded'
            return unknown(provider, context, BROWSER_MESSAGES[code], 'unavailable'), [
                browser_attempt(provider, 'unavailable', code, BROWSER_MESSAGES[code],
                                delivery='live_fetch' if worker_dispatched else 'not_attempted',
                                checked_at=checked_at)]

    async def _find(self, component, context, place, origin, intent, *, target_property_path=None, deadline=None, pinned_departure=None):
        if component != 'flight' or context.target_policy == 'fixed_entity':
            return await self._find_date(component, context, place, origin, intent,
                                         target_property_path=target_property_path)
        diagnostics = []
        result = None
        reference = None
        for departure in ((pinned_departure,) if pinned_departure is not None else departure_dates(intent)):
            dated_context = context.model_copy(update={'date_from': departure})
            try:
                async with asyncio.timeout_at(deadline):
                    quote, rows, metadata = await self._find_date(component, dated_context, place, origin, intent)
            except TimeoutError:
                if reference is None:
                    raise
                break
            diagnostics.extend(rows)
            result = quote, diagnostics, metadata
            if quote.value.kind != 'unknown':
                if metadata and metadata.get('route'):
                    return result
                if reference is None:
                    reference = quote, metadata
            if quote.outcome in {'blocked', 'requires_login'} or any(
                    row.get('diagnostic_code') in {'flight_challenge', 'flight_login',
                        'flight_overloaded', 'source_overloaded'} for row in rows):
                break
        if reference is not None:
            return reference[0], diagnostics, reference[1]
        return result if result is not None else (
            unknown('public_airlines', context, 'Допустимые даты вылета не определены.', 'context_mismatch'), [], None)

    @traced("pricing.lookup")
    async def _find_date(self, component, context, place, origin, intent, *, target_property_path=None):
        set_attributes(operation=component if component in ('flight', 'stay', 'transfer') else 'other')
        if component=='flight':
            if any(value in ('transport:train','transport:bus','transport:car','transport:no_flight') for value in context.fare_constraints):
                return unknown('public_airlines',context,'Для выбранного наземного транспорта авиационные тарифы не применяются.','not_configured'), [], None
            if not origin.airport or not place.airport:
                return unknown('public_airlines',context,'Для источника нужен точный аэропорт отправления и прибытия.','context_mismatch'), [], None
            if context.target_policy == 'fixed_entity':
                if not itinerary_target(context.entity_id):
                    message = FLIGHT_MESSAGES['flight_target_not_found']
                    return unknown('google_flights', context, message, 'context_mismatch'), [
                        attempt('google_flights', 'context_mismatch', 'flight_target_not_found', message)], None
                quote, diagnostics, metadata = await self.flights.search(context, intent)
                if quote is not None:
                    return quote, diagnostics, metadata
                # A generic route tariff is not a price for an identified set of
                # flights. It must never inherit an already selected itinerary.
                reason = next((row['message'] for row in reversed(diagnostics)), FLIGHT_MESSAGES['flight_target_not_found'])
                return unknown('google_flights', context, reason,
                    diagnostics[-1]['outcome'] if diagnostics else 'unavailable'), diagnostics, None
            if not self.flights.url:
                return await self._airline_lookup(context, place, origin)
            tasks = [asyncio.create_task(self.flights.search(context, intent)),
                     asyncio.create_task(self._airline_lookup(context, place, origin))]
            try:
                browser, airline = await asyncio.gather(*tasks)
            finally:
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
            diagnostics = browser[1] + airline[1]
            if browser[0] is not None:
                return browser[0], diagnostics, browser[2]
            if airline[0].value.kind != 'unknown':
                return airline[0], diagnostics, airline[2]
            reason = next((row.get('message') for row in reversed(browser[1]) if row.get('message')), airline[0].reason)
            return unknown('public_airlines', context, reason,
                browser[1][-1]['outcome'] if browser[1] else airline[0].outcome), diagnostics, None
        if (context.target_policy == 'fixed_entity' and not context.entity_id.startswith(('ostrovok:', 'google-hotel:'))
                and context.entity_id not in {source[1] for source in HOTEL_TARIFF_SOURCES.values()}):
            code = 'unsupported_property_mapping'
            message = BROWSER_MESSAGES[code]
            set_attributes(outcome='context_mismatch', reason=code)
            return unknown('public_lodging', context, message, 'context_mismatch'), [attempt('public_lodging', 'context_mismatch', code, message)], None
        direct_city = ((place.airport=='ATH' and place.name.casefold() in ('афины','athens'))
                       or (place.airport=='IST' and place.name.casefold() in ('стамбул','istanbul')))
        hotel = (await self.hotels.search(place.airport or '',context.date_from,context.date_to,intent.adults,
                                        intent.children_ages,getattr(intent,'rooms',1),intent.currency)
                 if direct_city and (context.target_policy != 'fixed_entity'
                     or context.entity_id in {source[1] for source in HOTEL_TARIFF_SOURCES.values()})
                 else {'status':'unknown','reason':'unsupported_destination'})
        reference = self._hotel_reference(hotel, context) if hotel.get('status')=='reference' else None
        providers = (['ostrovok'] if context.entity_id and context.entity_id.startswith('ostrovok:')
                     else ['google_hotels'] if context.entity_id and context.entity_id.startswith('google-hotel:')
                     else ['ostrovok', 'google_hotels'] if context.target_policy != 'fixed_entity' else [])
        diagnostics = []
        browser = unknown('public_lodging', context, 'Для выбранного объекта нет совместимого браузерного источника.', 'not_configured')
        for provider in providers:
            browser, outcomes = await self._browser(context, place, provider,
                target_property_path=target_property_path if provider == 'ostrovok' else None,
                discovery_terms=lodging_discovery_terms(intent))
            diagnostics.extend(outcomes)
            if browser.value.kind != 'unknown':
                return browser, diagnostics, None
        if reference:
            return reference, diagnostics, hotel
        reason = browser.reason if self.worker_url else 'Публичный тариф для этого города не найден; браузерный источник недоступен.'
        return unknown('public_lodging',context,reason,browser.outcome), diagnostics, None

    async def _airline_lookup(self, context, place, origin):
        try:
            return await self._published_airline_lookup(context, place, origin)
        except (httpx.HTTPError, TimeoutError, ValueError, TypeError, KeyError):
            message = 'Не удалось проверить опубликованные тарифы авиакомпаний.'
            return unknown('public_airlines', context, message, 'unavailable'), [
                attempt('public_airlines', 'unavailable', 'source_parse_error', message)], None

    async def _published_airline_lookup(self, context, place, origin):
        result = await self.airlines.search(origin.airport, place.airport, str(context.date_from), str(context.date_to), context.currency)
        # Prefer a roundtrip. A partial reverse leg never becomes the requested outgoing trip.
        compatible = [fact for fact in result['facts'] if fact['scope']['origin_iata'] == origin.airport
            and fact['scope']['destination_iata'] == place.airport]
        compatible.sort(key=lambda fact: (fact['trip_type'] != 'roundtrip', Decimal(fact['amount'])))
        if compatible:
            return self._flight_reference(compatible[0], context), result['diagnostics'], {
                'delivery': compatible[0].get('delivery'), 'legs': compatible[0].get('legs', [])}
        diagnostics = result['diagnostics']
        reason = next((row.get('message') for row in reversed(diagnostics) if row.get('message')),
            'На выбранные даты опубликованный тариф не найден.')
        return unknown('public_airlines', context, reason,
            diagnostics[-1].get('outcome') if diagnostics else 'no_offers'), diagnostics, None

    @traced("pricing.apply")
    async def apply(self, place, origin, intent, components, refresh=False, *, only=('flight', 'stay')):
        if not self.enabled:
            return components
        if not intent.date_from or not intent.date_to:
            return components
        async def one(component, index):
            part=components[index]
            if component=='flight' and part.details.get('transport_mode', 'flight') != 'flight':
                return
            local_entity = part.details.get('catalog_property_id') if component=='stay' else None
            entity=local_entity or (part.details.get('pricing_entity_id') if refresh else None)
            target_property_path = None
            if component == 'stay' and not local_entity:
                source_target = ostrovok_property_target(part.source_url, place)
                if source_target and (entity is None or entity == source_target[0]):
                    entity, target_property_path = source_target
                    part.details['pricing_entity_id'] = entity
                elif refresh and entity is None and part.source_url and part.details.get('link_type') in (
                        'indexed_hotel_page', 'place', 'published_tariff', 'price_search_result'):
                    # A concrete source identity is fixed even when no connected
                    # adapter can price it. Never use another hotel's city rate.
                    entity = part.source_url
            selected_departure = None
            allowed_departures = departure_dates(intent)
            if component == 'flight' and refresh:
                observed = part.details.get('flight_route', {}).get('date_from')
                if observed is None and part.quote and part.quote.scope:
                    observed = part.quote.scope.date_from
                try:
                    observed = date.fromisoformat(observed) if isinstance(observed, str) else observed
                except ValueError:
                    observed = None
                if observed in allowed_departures:
                    selected_departure = observed
            context=context_for(component,place,origin,intent,entity_id=entity,
                                departure_date=selected_departure)
            if component == 'flight' and arrival_date_required(intent) is not None:
                part.details['departure_window'] = {
                    'date_from': min(allowed_departures).isoformat(),
                    'date_to': max(allowed_departures).isoformat(),
                    'arrival_date': arrival_date_required(intent).isoformat(),
                }
            elif component == 'flight':
                part.details.pop('departure_window', None)
            if local_entity or not self.web_fallback_enabled:
                reason = ('Локальный каталог содержит сведения об объекте, без цен и наличия на даты. Другой отель для проверки цены не подставлялся.'
                          if local_entity else 'Внешние источники цен отключены; стоимость неизвестна.')
                part.quote = unknown('local_catalog' if local_entity else 'public_'+component, context, reason, 'policy_disabled')
                part.price, part.status = None, 'unavailable'
                part.details['pricing_outcomes'] = [{'provider': 'local_catalog' if local_entity else 'public_'+component,
                    'provider_id': 'local_catalog' if local_entity else 'public_'+component,
                    'outcome': 'policy_disabled', 'delivery': 'disabled', 'dispatched': False,
                    'reason': 'catalog_snapshot_no_rates' if local_entity else 'external_fallback_disabled'}]
                part.details['pricing_delivery'] = 'snapshot' if local_entity else 'disabled'
                part.details.pop('pricing_observation_delivery', None)
                part.details.pop('pricing_refresh_warning', None)
                return
            key=component+':'+request_context_hash(context)
            if component == 'stay' and lodging_discovery_terms(intent):
                key += ':wishes:' + hashlib.sha256('\n'.join(lodging_discovery_terms(intent)).encode()).hexdigest()
            if component == 'flight':
                key += ':' + self.flights.key(context, intent)
                key += ':departures:' + ','.join(day.isoformat() for day in allowed_departures)
            task=self._inflight.get(key)
            if task is None and len(self._inflight) >= 16:
                # Capacity rejection performs no source work, but must use the
                # same compatible-observation preservation as any failed lookup.
                message = 'Источники заняты; повторите проверку позже.'
                quote,diagnostics,hotel=unknown('public_'+component,context,message,'timeout'),[
                    attempt('public_'+component, 'timeout', 'source_overloaded', message)],None
                part.price, part.status = None, 'unavailable'
            else:
                if task is None:
                    async def lookup():
                        # Source queue and work share a deadline and a bounded pool.
                        async with asyncio.timeout(42) as source_deadline:
                            async with queued(self._source_slots, "pricing.source.queue"):
                                if component == 'flight' and refresh and (entity or part.quote and part.quote.scope) and selected_departure is None:
                                    return unknown('public_airlines', context,
                                        'Дата выбранного перелёта вне разрешённого окна; другой маршрут не подставлялся.',
                                        'context_mismatch'), [], None
                                if target_property_path is not None:
                                    return await self._find(component,context,place,origin,intent,target_property_path=target_property_path)
                                if component == 'flight' and arrival_date_required(intent) is not None:
                                    return await self._find(component,context,place,origin,intent,
                                                            deadline=source_deadline.when() - 0.05,
                                                            pinned_departure=selected_departure if refresh else None)
                                return await self._find(component,context,place,origin,intent)
                    task=asyncio.create_task(lookup())
                    self._inflight[key]=task
                    def forget(finished, request_key=key):
                        if self._inflight.get(request_key) is finished:
                            self._inflight.pop(request_key,None)
                        if not finished.cancelled():
                            finished.exception()  # Collect failure even if the caller was cancelled.
                    task.add_done_callback(forget)
                else:
                    self.store.increment('pricing_dedup_hits')
                self._waiters[task] = self._waiters.get(task, 0) + 1
                try:
                    async with asyncio.timeout(44):
                        quote,diagnostics,hotel=await asyncio.shield(task)
                except (TimeoutError,httpx.HTTPError,ValueError,TypeError,KeyError):
                    message = 'Проверка источников не завершилась вовремя.'
                    quote,diagnostics,hotel=unknown('public_'+component,context,message,'timeout'),[attempt('public_'+component, 'timeout', 'source_deadline', message)],None
                finally:
                    remaining_waiters = self._waiters[task] - 1
                    if remaining_waiters:
                        self._waiters[task] = remaining_waiters
                    else:
                        self._waiters.pop(task, None)
                        if self._inflight.get(key) is task:
                            self._inflight.pop(key,None)
                        if not task.done():
                            task.cancel()
                        # Do not release the gateway operation slot with orphaned
                        # source work. Other active waiters keep their shared task.
                        await asyncio.gather(task, return_exceptions=True)
            if component == 'flight' and quote.value.kind != 'unknown' and quote.scope:
                if quote.scope.date_from in allowed_departures:
                    context = context.model_copy(update={'date_from': quote.scope.date_from})
                    part.details['selected_departure_date'] = quote.scope.date_from.isoformat()
            previous=part.quote
            previous_hashes={request_context_hash(context)}
            if context.target_policy=='fixed_entity' and previous and previous.scope and previous.scope.entity_id==context.entity_id:
                previous_hashes.add(request_context_hash(context.model_copy(update={'target_policy':'candidate_allowed','entity_id':None})))
            if component == 'flight' and hotel and hotel.get('route') and quote.value.kind != 'unknown':
                route = hotel['route']
                # A paired route replaces legacy flight observations as a whole;
                # no airline headline, duration or evidence is transplanted.
                part.price = None
                part.evidence = []
                part.source_url = route.source_url
                part.description = describe_route(route)
                part.details.update(pricing_entity_id=entity_id(route),
                    flight_route=route.model_dump(mode='json'),
                    duration_minutes=max(route.outbound.duration_minutes, route.inbound.duration_minutes),
                    direct_flight_verified=route.outbound.stops == 0 and route.inbound.stops == 0,
                    availability_verified=False, timezone_verified=False, connections_verified=False,
                    link_type='flight_search_result')
                part.details.pop('arrival_at', None)
                part.details.pop('departure_at', None)
                for legacy_field in ('segments', 'price_scope', 'retrieved_at', 'cache_freshness', 'cache_stale'):
                    part.details.pop(legacy_field, None)
            if component=='stay' and hotel and hotel.get('property_name') and quote.value.kind!='unknown':
                if context.target_policy=='fixed_entity' and context.entity_id!=hotel['source_url']:
                    quote=unknown('direct_hotel_tariffs',context,'Опубликованный тариф относится к другому отелю.','context_mismatch')
                else:
                    if part.details.get('pricing_entity_id')!=hotel['source_url']:
                        # A public tariff is not a price for the old OSM property.
                        part.details={'availability_verified':False,'requirements_verified':False,'pet_policy_verified':False,
                                      'property_location_verified':False,'property_source':'direct_hotel_tariffs','link_type':'published_tariff'}
                        part.evidence=[]
                        transfer=components[2]
                        transfer.title='Точка прибытия → отель → точка отправления'
                        transfer.description='После смены отеля маршрут, время и стоимость трансфера требуется проверить заново.'
                        transfer.details={'availability_verified':False,'link_type':'map'}
                        transfer.price=None
                        transfer.quote=None
                        transfer.status='unavailable'
                        transfer.evidence=[]
                        transfer.source_url='https://www.google.com/maps/search/?'+urlencode({'api':1,'query':place.name})
                    part.details['pricing_entity_id']=hotel['source_url']
                    part.price=None
                    part.title=hotel['property_name']
                    part.source_url=hotel['source_url']
                    part.description='Опубликованный тариф самого отеля. Это справочный ориентир за номер и ночь; стоимость и наличие на выбранные даты не подтверждены.'
            if component=='stay' and quote.provider_id in ('google_hotels', 'ostrovok') and quote.value.kind!='unknown' and quote.scope and quote.scope.entity_id:
                if part.details.get('pricing_entity_id') != quote.scope.entity_id:
                    part.details={'availability_verified':False,'requirements_verified':False,'pet_policy_verified':False,
                                  'property_location_verified':False,'property_source':quote.provider_id,
                                  'pricing_entity_id':quote.scope.entity_id,'link_type':'price_search_result'}
                    part.price=None
                    part.evidence=[]
                    transfer=components[2]
                    transfer.price=None
                    transfer.quote=None
                    transfer.status='unavailable'
                    transfer.details={'availability_verified':False,'link_type':'map'}
                    transfer.evidence=[]
                    transfer.source_url='https://www.google.com/maps/search/?'+urlencode({'api':1,'query':place.name})
                    transfer.description='После смены отеля наземный маршрут и стоимость требуется проверить заново.'
                part.title=quote.scope.provider_entity_id or 'Размещение из Google Hotels'
                part.source_url=quote.source_url
                part.description=quote.reason or 'Цена найдена в поиске поставщиков; наличие и окончательные условия проверяются у продавца.'
            # A failed refresh must not erase the visible previous check. This
            # is the session's own observation, not a renewed cache/estimate.
            previous_compatible = bool(previous and previous.request_context_hash in previous_hashes)
            if component == 'flight' and previous and previous.provider_id in ('google_flights', 'yandex_avia'):
                # Preferences are outside the old pricing hash. Never retain a
                # route that violates a newly changed hard direct/duration bound.
                from flight_search.models import SelectedRoute
                try:
                    route = SelectedRoute.model_validate(part.details.get('flight_route'))
                    previous_compatible = previous_compatible and self.flights._valid_route(route, context, intent, allow_expired=True)
                except (ValueError, TypeError):
                    previous_compatible = False
            if quote.value.kind=='unknown' and previous and previous.value.kind!='unknown' and previous_compatible:
                quote=previous
                part.details['pricing_refresh_warning']='Обновить цену выбранного варианта не удалось. Показана предыдущая проверка с исходным сроком действия.'
            else:
                part.details.pop('pricing_refresh_warning',None)
                if component == 'flight' and quote.value.kind == 'unknown' and previous and not previous_compatible:
                    part.description = quote.reason or 'Маршрут для нового контекста не подтверждён.'
                    part.details.pop('flight_route', None)
                    part.details.pop('duration_minutes', None)
                    part.details['direct_flight_verified'] = False
                    part.details.pop('selected_departure_date', None)
            part.quote=quote
            if component=='flight' and hotel and hotel.get('legs'):
                fields=('source_url','origin_iata','destination_iata','date','amount','currency','observed_at')
                part.details['pricing_legs']=[{name:leg.get(name) for name in fields} for leg in hotel['legs'][:2]]
            elif component=='flight' and quote is not previous:
                part.details.pop('pricing_legs',None)
            part.details['pricing_outcomes']=diagnostics
            part.details['pricing_delivery'] = pricing_delivery(diagnostics, retained=quote is previous,
                source_delivery=hotel.get('delivery') if hotel else None)
            if quote.value.kind != 'unknown':
                part.details['pricing_observation_delivery'] = observation_delivery(quote, diagnostics,
                    retained=quote is previous, source_delivery=hotel.get('delivery') if hotel else None)
            else:
                part.details.pop('pricing_observation_delivery', None)
            part.evidence=[item for item in part.evidence if item.provider not in
                           ('aegean_public','ryanair_farefinder','direct_hotel_tariffs','google_hotels','ostrovok','google_flights','yandex_avia')]
            if quote.value.kind!='unknown':
                from ..models import Evidence
                part.status='observed' if quote.outcome=='quoted' else 'estimated'
                if component=='flight' and quote.reason and quote.provider_id not in ('google_flights', 'yandex_avia'):
                    part.description=quote.reason
                part.evidence.append(Evidence(provider=quote.provider_id,source_url=quote.source_url,
                    retrieved_at=quote.retrieved_at.isoformat(),freshness='cached' if quote.freshness(delivery=part.details['pricing_observation_delivery']) in ('stale','cached') else 'live',
                    confidence='Опубликованный тариф, не подтверждение бронирования',
                    label=(quote.unit_label or 'Цена источника')+'. '+(quote.reason or 'Проверьте условия у источника.')))
                self.store.increment('keyless_source_observed_components' if quote.outcome=='quoted' else 'keyless_source_reference_components')
            else:
                self.store.increment('keyless_source_unknown_components')
        await asyncio.gather(*(one(component, index) for component, index in (('flight', 0), ('stay', 1)) if component in only))
        return components
