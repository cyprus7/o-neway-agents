"""Bounded HTTP adapter for the agent's travel-data port. No concrete fallback."""
from __future__ import annotations

import asyncio
from collections import deque
from datetime import datetime, timezone
import hashlib
import json
import re
import time
from typing import Any
from urllib.parse import urlsplit

import httpx
from one_request_telemetry import internal_headers, queued, set_attributes, traced

from ..domain.travel_data import (ComponentBundle, DataIntent, DEADLINES, DestinationInput, EmptyInput,
    GatewayRequest, GatewayResponse, InventoryInput, InventoryCandidatesInput, candidate_identity, MAX_REQUEST_BYTES, MAX_RESPONSE_BYTES, OUTPUTS,
    PATHS, PlaceRef, PriceDocumentsData, RefreshInput, ResolveInput, context_for, make_request)
from ..models import Evidence, TravelComponent, utcnow
from ..pricing.models import PriceObservation, UnknownPrice
from ..pricing.normalization import compatibility_errors, request_context_hash
from ..regional_events.domain import EventResult
from ..travel_dates import departure_dates, arrival_date_required, selected_departure_date


UNAVAILABLE = "Источник данных временно недоступен; цена, наличие и условия остаются неизвестными."


class HttpTravelDataPort:
    def __init__(self, client: httpx.AsyncClient, base_url: str, *, timeout_seconds: float | None = None):
        parsed = urlsplit(base_url)
        parsed.port  # Validate a configured port before any request is attempted.
        if (parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password
                or parsed.query or parsed.fragment or parsed.path not in ("", "/")):
            raise ValueError("invalid_provider_gateway_url")
        self.client, self.base_url = client, base_url.rstrip("/")
        self.timeout_seconds = min(55.0, max(0.01, timeout_seconds)) if timeout_seconds is not None else None
        self._slots = asyncio.Semaphore(8)
        self._failures: deque[float] = deque()
        self._open_until = 0.0

    def _failure(self) -> None:
        now = time.monotonic()
        self._failures.append(now)
        while self._failures and self._failures[0] < now - 30:
            self._failures.popleft()
        if len(self._failures) >= 3:
            self._open_until = now + 10

    @traced("travel_data.client", kind="client")
    async def _call(self, operation, data):
        set_attributes(operation="other" if operation == "price_documents" else operation)
        if self._open_until > time.monotonic():
            set_attributes(outcome="circuit_open")
            return None, "circuit_open"
        timeout = min(DEADLINES[operation], self.timeout_seconds or DEADLINES[operation])
        request = make_request(operation, data, timeout=timeout)
        try:
            body = request.model_dump_json().encode()
            if len(body) > MAX_REQUEST_BYTES:
                set_attributes(outcome="invalid_response")
                return None, "invalid_response"
            headers = {"Content-Type": "application/json"}
            # Propagate only across this explicitly private service boundary.
            headers.update(internal_headers())
            method = "POST"
            if operation == "capabilities":
                method = "GET"
                headers.update({"X-Travel-Version": request.version, "X-Request-ID": request.request_id,
                    "X-Request-Fingerprint": request.fingerprint, "X-Deadline-At": request.deadline_at.isoformat()})
            async with asyncio.timeout(timeout):
                async with queued(self._slots, "travel_data.client.queue"):
                    async with self.client.stream(method, self.base_url + PATHS[operation],
                            content=body if method == "POST" else None, headers=headers,
                            timeout=httpx.Timeout(timeout, connect=min(2, timeout)), follow_redirects=False) as response:
                        set_attributes(http_response_status_code=response.status_code)
                        response.raise_for_status()
                        if response.headers.get("content-type", "").split(";", 1)[0].lower() != "application/json":
                            raise ValueError("invalid_content_type")
                        if int(response.headers.get("content-length", "0")) > MAX_RESPONSE_BYTES:
                            raise ValueError("response_too_large")
                        payload = bytearray()
                        async for chunk in response.aiter_bytes(chunk_size=65536):
                            payload.extend(chunk)
                            if len(payload) > MAX_RESPONSE_BYTES:
                                raise ValueError("response_too_large")
                        result = GatewayResponse[OUTPUTS[operation]].model_validate_json(payload)
                        if (result.request_id != request.request_id or result.fingerprint != request.fingerprint
                                or result.operation != operation):
                            raise ValueError("response_identity_mismatch")
            self._failures.clear()
            self._open_until = 0
            set_attributes(outcome="ok")
            return result.data, result.reason
        except (httpx.HTTPError, TimeoutError, ValueError, TypeError, OverflowError):
            set_attributes(outcome="gateway_unavailable")
            self._failure()
            return None, "gateway_unavailable"

    @staticmethod
    def _destination(place, intent):
        return DestinationInput(place=PlaceRef.from_place(place), intent=DataIntent.from_intent(intent))

    @staticmethod
    def _evidence(reason):
        return Evidence(provider="Шлюз данных путешествия", source_url=None, retrieved_at=utcnow(),
            freshness="unavailable", confidence="Нет подтверждённых данных", label=UNAVAILABLE)

    async def capabilities(self) -> dict[str, Any]:
        result, reason = await self._call("capabilities", EmptyInput())
        if result is not None:
            return result.model_dump(mode="json")
        return {"external_data_fallback_enabled": False,
            "providers": {name: {"configured": False, "status": "unavailable", "reason": reason}
                          for name in ("catalog", "search", "weather", "places", "inventory")},
            "pricing": {"refresh_enabled": False, "providers": []},
            "gateway": {"status": "unavailable", "reason": reason, "contract_version": "travel-data.v1"}}

    async def resolve_place(self, name):
        result, _ = await self._call("resolve_place", ResolveInput(name=name))
        return result.place.to_place() if result is not None and result.place is not None else None

    async def weather(self, place, intent):
        result, reason = await self._call("weather", self._destination(place, intent))
        if result is not None:
            # Explicit nulls such as Evidence.source_url remain required public
            # fields even when a source is unavailable.
            return result.model_dump(mode="json")
        return {"kind": "unknown", "status": "unavailable", "summary": UNAVAILABLE,
                "evidence": [self._evidence(reason).model_dump()]}

    async def search(self, place, intent):
        result, reason = await self._call("search", self._destination(place, intent))
        return result.evidence if result is not None else [self._evidence(reason)]

    def _unavailable_bundle(self, place, origin, intent, reason):
        parts = []
        for component, title in (("flight", f"{origin.name} → {place.name} → {origin.name}"),
                                  ("stay", "Размещение: сведения недоступны"), ("transfer", "Трансфер: сведения недоступны")):
            quote = PriceObservation(provider_id="provider-gateway", value=UnknownPrice(reason=UNAVAILABLE),
                reason=UNAVAILABLE, outcome="unavailable",
                request_context_hash=request_context_hash(context_for(component, place, origin, intent)))
            parts.append(TravelComponent(title=title, description=UNAVAILABLE, currency=intent.currency,
                quote=quote, details={"availability_verified": False, "gateway_reason": reason},
                evidence=[self._evidence(reason)]))
        return ComponentBundle.from_components(parts)

    async def inventory(self, place, origin, intent):
        data = InventoryInput(place=PlaceRef.from_place(place), origin=PlaceRef.from_place(origin), intent=DataIntent.from_intent(intent))
        result, reason = await self._call("inventory", data)
        if result is not None and not self._compatible_quotes(result, place, origin, intent):
            result, reason = None, "invalid_response"
        return result if result is not None else self._unavailable_bundle(place, origin, intent, reason)

    async def inventory_candidates(self, place, origin, intent, *, limit=3):
        data = InventoryCandidatesInput(place=PlaceRef.from_place(place), origin=PlaceRef.from_place(origin),
                                        intent=DataIntent.from_intent(intent), limit=limit)
        # Capability absence is the old-peer contract, not an inferred error retry.
        try:
            async with asyncio.timeout(DEADLINES["inventory_candidates"]):
                capabilities = await self.capabilities()
                if not capabilities.get("inventory_candidates", False):
                    bundle = await self.inventory(place, origin, intent)
                    bundle.candidate_id = candidate_identity(bundle, place, origin, intent)
                    return [bundle]
                result, reason = await self._call("inventory_candidates", data)
                if result is not None:
                    candidates = result.candidates
                    if (len(candidates) <= limit and len({b.candidate_id for b in candidates}) == len(candidates)
                            and all(b.candidate_id == candidate_identity(b, place, origin, intent)
                                    and self._compatible_quotes(b, place, origin, intent) for b in candidates)):
                        return candidates
                    reason = "invalid_response"
                bundle = self._unavailable_bundle(place, origin, intent, reason)
                bundle.candidate_id = candidate_identity(bundle, place, origin, intent)
                return [bundle]
        except TimeoutError:
            bundle = self._unavailable_bundle(place, origin, intent, "deadline_exceeded")
            bundle.candidate_id = candidate_identity(bundle, place, origin, intent)
            return [bundle]

    async def regional_events(self, place, intent):
        result, _ = await self._call("regional_events", self._destination(place, intent))
        return result if result is not None else EventResult(status="unavailable", checked_at=utcnow())

    async def price_documents(self, place, origin, intent, current):
        current = ComponentBundle.from_components(current)
        current.candidate_id = None  # Legacy fixed-refresh/document peers do not know candidate metadata.
        data = RefreshInput(place=PlaceRef.from_place(place), origin=PlaceRef.from_place(origin),
                            intent=DataIntent.from_intent(intent), current=current)
        result, reason = await self._call("price_documents", data)
        if result is not None:
            for document in result.documents:
                entity = current.stay.details.get("pricing_entity_id") if document.component == "stay" else None
                if document.component == "stay" and not entity:
                    entity = "knowledge-property:" + hashlib.sha256((current.stay.title + "\n" + str(current.stay.source_url)).encode()).hexdigest()[:24]
                expected = context_for(document.component, place, origin, intent, entity_id=entity,
                    departure_date=selected_departure_date(intent, current.flight) if document.component == "flight" else None)
                if document.context_hash != request_context_hash(expected):
                    return PriceDocumentsData(outcome="unavailable", reason="invalid_response")
            return result
        return PriceDocumentsData(outcome="unavailable", reason=reason or "gateway_unavailable")

    @staticmethod
    def _compatible_quotes(result, place, origin, intent, current=None):
        for component in ("flight", "stay", "transfer"):
            quote = getattr(result, component).quote
            previous = getattr(current, component).quote if current is not None else None
            departure = None
            if component == "flight" and quote is not None and quote.value.kind != "unknown" and quote.scope is not None:
                departure = quote.scope.date_from
                if departure is not None and departure not in departure_dates(intent):
                    return False
            if component == "flight" and quote is not None and quote.value.kind != "unknown" and quote.provider_id in ("google_flights", "yandex_avia"):
                # A request hash can be echoed by an untrusted service. Browser
                # references still require independently matching stated facts.
                identity = result.flight.details.get("pricing_entity_id")
                prefix = {"google_flights": "google-flight:", "yandex_avia": "yandex-flight:"}[quote.provider_id]
                scope = quote.scope
                candidate = context_for("flight", place, origin, intent, departure_date=departure)
                if (not isinstance(identity, str) or not re.fullmatch(re.escape(prefix) + r"[a-f0-9]{64}", identity)
                        or scope is None or scope.entity_id != identity or scope.entity_level != "route"
                        or scope.unit != "group_trip" or quote.value.currency != intent.currency):
                    return False
                for name in ("component", "origin", "destination", "date_from", "date_to", "adults", "children_ages", "trip_type", "currency"):
                    if getattr(scope, name) != getattr(candidate, name):
                        return False
                if arrival_date_required(intent) is not None:
                    route = result.flight.details.get("flight_route")
                    if not isinstance(route, dict) or not isinstance(route.get("outbound"), dict):
                        return False
                    if (route.get("date_from") != departure.isoformat()
                            or route["outbound"].get("departure_date") != departure.isoformat()
                            or route["outbound"].get("arrival_date") != arrival_date_required(intent).isoformat()):
                        return False
                # A refresh is fixed only after a route was actually selected.
                # An earlier unknown result still permits candidate discovery;
                # its first valid route retains that original candidate hash.
                fixed = current is not None and current.flight.details.get("pricing_entity_id")
                expected = context_for("flight", place, origin, intent, entity_id=identity, departure_date=departure) if fixed else candidate
                allowed_hashes = {request_context_hash(expected)}
                if previous is not None and quote == previous:
                    # Retention permits the original discovery scope, never a
                    # different party/date/airport scope or a new source time.
                    allowed_hashes.add(request_context_hash(candidate))
                if quote.request_context_hash not in allowed_hashes:
                    return False
            if quote is None or quote.value.kind == "unknown" or previous is not None and quote == previous:
                continue
            entity = (getattr(current, component).details.get("pricing_entity_id")
                      if current is not None and component in ("flight", "stay") else None)
            context = context_for(component, place, origin, intent, entity_id=entity, departure_date=departure)
            if quote.request_context_hash != request_context_hash(context):
                # Initial discovery may narrow a city-wide request to one
                # source-identified property. Accept only that strictly narrower
                # context; all other request fields/hash stay exactly bound.
                scope = quote.scope
                identity = result.stay.details.get('pricing_entity_id') if component == 'stay' else None
                prefix = {'ostrovok': 'ostrovok:', 'google_hotels': 'google-hotel:'}.get(quote.provider_id)
                if (current is not None or component != 'stay' or not prefix or scope is None
                        or scope.entity_level != 'property' or scope.entity_id != identity
                        or not isinstance(identity, str) or not re.fullmatch(re.escape(prefix) + r'[a-f0-9]{24}', identity)):
                    return False
                narrowed = context_for(component, place, origin, intent, entity_id=identity)
                if quote.request_context_hash != request_context_hash(narrowed):
                    return False
                # References may leave facts unknown, but a stated field may not
                # contradict this newly granted fixed-property request scope.
                for name in ('component', 'origin', 'destination', 'destination_city', 'date_from', 'date_to',
                             'adults', 'children_ages', 'rooms', 'room_allocation', 'trip_type', 'currency'):
                    observed, expected = getattr(scope, name), getattr(narrowed, name)
                    if isinstance(observed, str) and isinstance(expected, str):
                        observed, expected = observed.strip().casefold(), expected.strip().casefold()
                    if name == 'children_ages' and observed is not None:
                        observed, expected = sorted(observed), sorted(expected)
                    if observed is not None and observed != expected:
                        return False
                context = narrowed
            if quote.outcome == "quoted" and compatibility_errors(context, quote.scope):
                return False
        return True

    @staticmethod
    def _compatible_refresh(current, result, place, origin, intent):
        flight_identity = current.flight.details.get("pricing_entity_id")
        if flight_identity and result.flight.details.get("pricing_entity_id") != flight_identity:
            return False
        if flight_identity and result.flight.quote is not None and result.flight.quote.value.kind != "unknown":
            scope = result.flight.quote.scope
            previous = current.flight.quote
            if (scope is None or previous is None or previous.scope is None
                    or scope.date_from != previous.scope.date_from
                    or scope.date_from != selected_departure_date(intent, current.flight)):
                return False
        for field in ("catalog_property_id", "pricing_entity_id"):
            if current.stay.details.get(field) and result.stay.details.get(field) != current.stay.details[field]:
                return False
        if current.stay.details.get("catalog_property_id") and (
                current.stay.source_url != result.stay.source_url or current.stay.title != result.stay.title
                or current.stay.details.get("catalog_retrieved_at") != result.stay.details.get("catalog_retrieved_at")):
            return False
        return HttpTravelDataPort._compatible_quotes(result, place, origin, intent, current)

    async def refresh_inventory(self, place, origin, intent, current):
        current = ComponentBundle.from_components(current)
        data = RefreshInput(place=PlaceRef.from_place(place), origin=PlaceRef.from_place(origin),
                            intent=DataIntent.from_intent(intent), current=current.model_copy(update={"candidate_id": None}))
        result, reason = await self._call("refresh_inventory", data)
        if result is not None and self._compatible_refresh(current, result, place, origin, intent):
            result.candidate_id = current.candidate_id
            return result
        for part in current:
            part.details["pricing_refresh_warning"] = "Шлюз данных не обновил этот вариант. Сохранены предыдущие сведения с исходными сроками."
            part.details["gateway_reason"] = reason or "invalid_response"
        return current
