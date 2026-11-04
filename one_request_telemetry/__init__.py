"""Opt-in, private-by-default tracing without SDK imports on the disabled path.

Only an explicit OTEL_EXPORTER_OTLP_TRACES_ENDPOINT enables an application.
The module deliberately uses neither SDK globals nor automatic instrumentation.
"""
from __future__ import annotations

from collections.abc import Mapping
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import wraps
import inspect
import logging
import math
import os
import re
from typing import Any
from urllib.parse import unquote, urlsplit


__all__ = [
    "TracingSettings", "create_runtime", "instrument_app", "internal_headers",
    "queued", "set_attributes", "span", "traced",
]

_logger = logging.getLogger(__name__)
_runtime: ContextVar[Any | None] = ContextVar("one_request_tracing_runtime", default=None)
_METADATA = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,95}\Z")
_HEADER_NAME = re.compile(r"[a-z][a-z0-9-]{0,63}\Z")
_ROUTE = re.compile(r"/[A-Za-z0-9_/{},:.-]{0,179}\Z")
_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "CONNECT", "TRACE"})
_ALIASES = {
    "operation": "one_request.operation",
    "outcome": "one_request.outcome",
    "provider": "one_request.provider",
    "reason": "one_request.reason",
    "attempt": "one_request.attempt",
    "count": "one_request.count",
    "cache_hit": "one_request.cache_hit",
    "wait_seconds": "one_request.wait_seconds",
    "error_type": "error.type",
    "cancelled": "one_request.cancelled",
    "http_method": "http.request.method",
    "http_route": "http.route",
    "http_status_code": "http.response.status_code",
    "http_response_status_code": "http.response.status_code",
}
_STRING_ATTRIBUTES = frozenset({"one_request.operation", "one_request.outcome", "one_request.provider", "one_request.reason", "error.type"})
_BOOL_ATTRIBUTES = frozenset({"one_request.cache_hit", "one_request.cancelled"})
_NUMBER_ATTRIBUTES = frozenset({"one_request.attempt", "one_request.count", "one_request.wait_seconds"})
_STRING_VALUES = {
    "one_request.operation": frozenset({
        "resolve_place", "weather", "search", "inventory", "refresh_inventory",
        "regional_events", "capabilities", "flight", "stay", "transfer", "other",
        "flight_navigation", "flight_cards", "flight_scope", "flight_selection", "flight_return_cards",
    }),
    "one_request.provider": frozenset({"google_hotels", "ostrovok", "aegean_public", "ryanair_farefinder",
                                        "public_airlines", "searxng", "ollama_web_search", "google_flights", "yandex_avia", "other"}),
    "one_request.reason": frozenset({
        "invalid_context", "unsupported_route", "local_rate_limit", "http_blocked", "http_rate_limited",
        "http_redirect", "http_error", "http_ok", "response_too_large", "provider_timeout", "queue_deadline",
        "network_error", "source_parse_error", "no_matching_date_fare", "route_mismatch", "currency_mismatch",
        "stale_observation", "observed", "no_offers", "search_deadline", "cache_miss", "positive_cache_hit",
        "negative_cache_hit", "stale_cache_hit", "search_results", "no_results", "missing_key", "not_configured",
        "invalid_query", "provider_unavailable", "rate_limited", "authentication_failed", "deadline_exceeded",
        "invalid_response", "unsupported_destination", "destination_city_airport_mismatch", "only_eur_supported",
        "single_room_adult_only_recipe", "unsupported_duration", "provider_target_mismatch", "challenge_detected",
        "ostrovok_currency_control_unavailable", "selected_property_not_found", "ostrovok_no_property_links",
        "ostrovok_dateless_results", "ostrovok_property_context_mismatch", "ostrovok_dates_not_verified",
        "ostrovok_party_not_verified", "ostrovok_rooms_not_verified", "ostrovok_current_rate_not_found",
        "ostrovok_public_rate_charges_unverified", "calendar_year_or_selection_mismatch", "ui_context_not_applied",
        "no_property_links", "google_property_results_unavailable", "price_mode_control_not_found",
        "detail_dates_changed", "eur_display_not_verified", "detailed_stay_total_not_found",
        "public_stay_total_room_scope_unverified", "invalid_deadline", "browser_sandbox_unavailable",
        "component_deadline", "recipe_unavailable", "invalid_discovery_terms",
        "unsupported_property_mapping", "worker_timeout", "worker_parse_error", "worker_context_mismatch",
        "source_deadline", "missing_airport",
        "request_cancelled", "fare_total_mismatch",
        "source_overloaded", "flight_context_mismatch", "flight_parse_error", "flight_timeout",
        "flight_response_too_large", "flight_source_error", "fixed_itinerary_not_found",
        "policy_disabled", "context_mismatch", "unsupported_party",
        "flight_reference", "flight_unsupported_route", "flight_unsupported_party", "flight_only_eur",
        "flight_unsupported_cabin", "flight_unsupported_constraints", "flight_invalid_deadline", "flight_overloaded",
        "flight_browser_unavailable", "flight_challenge", "flight_login", "flight_scope_not_verified",
        "flight_route_mismatch", "flight_date_mismatch", "flight_party_mismatch", "flight_currency_mismatch",
        "flight_cabin_mismatch", "flight_no_compatible_itinerary", "flight_target_not_found",
        "flight_price_not_verified", "flight_source_unavailable", "flight_cancelled",
    }),
    "one_request.outcome": frozenset({
        "ok", "error", "cancelled", "unknown", "matched", "disabled", "live", "cached", "cached_stale",
        "gateway_unavailable", "circuit_open", "invalid_response", "timeout", "not_configured",
        "parse_error", "context_mismatch", "unavailable", "overloaded", "policy_disabled", "rate_limited",
        "quoted", "reference", "no_offers", "blocked", "requires_login", "unsupported_party", "currency_mismatch",
        "observed", "unsupported_route", "stale_observation", "cooldown", "not_attempted",
    }),
    "error.type": frozenset({
        "Error", "Exception", "ValueError", "TypeError", "KeyError", "IndexError", "AttributeError",
        "AssertionError", "RuntimeError", "OSError", "IOError", "TimeoutError", "ConnectionError",
        "HTTPException", "PlannerUnavailable", "PriceValidationError", "DemoQuoteError", "BusySession",
        "HTTPError", "HTTPStatusError", "RequestError", "TransportError", "TimeoutException",
        "ConnectTimeout", "ReadTimeout", "WriteTimeout", "PoolTimeout", "NetworkError", "ConnectError",
        "ReadError", "WriteError", "CloseError", "ProtocolError", "RemoteProtocolError", "LocalProtocolError",
        "UnsupportedProtocol", "ProxyError", "TooManyRedirects", "DecodingError", "ValidationError", "JSONDecodeError",
    }),
}
_SPAN_NAMES = frozenset({
    "operation", "catalog.lookup", "catalog.search", "gateway.execute", "gateway.run", "gateway.queue",
    "llm.limiter.wait", "llm.completion", "llm.request.queue", "llm.dispatch", "llm.extract",
    "planner.candidates", "planner.build_options", "planner.run", "planner.refresh_prices",
    "planning.run", "planning.refresh", "pricing.browser", "pricing.worker.queue", "pricing.worker.client",
    "pricing.lookup", "pricing.apply", "pricing.source.queue", "price_worker.quotes", "price_worker.queue",
    "price_worker.recipe", "provider.cached_get", "provider.http.queue", "provider.http",
    "travel_data.client", "travel_data.client.queue", "travel_data.capabilities", "travel_data.resolve_place",
    "travel_data.weather", "travel_data.search", "travel_data.inventory", "travel_data.regional_events",
    "travel_data.refresh_inventory", "synthetic.command", "synthetic.background", "synthetic.gateway",
    "synthetic.worker", "synthetic.validation",
    "airline.search", "airline.lookup", "airline.cache", "airline.http", "airline.queue",
    "search.lookup", "search.cache", "search.http", "search.queue", "search.federated",
    "flight.search", "flight.queue", "flight.browser", "flight.source", "flight.lookup", "flight.cache", "flight.client",
    "flight.navigation", "flight.cards", "flight.scope", "flight.calendar", "flight.select",
})


@dataclass(frozen=True)
class TracingSettings:
    """Validated explicit settings. Repr never includes endpoint or credentials."""

    endpoint: str = field(repr=False)
    headers: dict[str, str] = field(repr=False)
    service_name: str
    environment: str = "local"
    service_version: str = "0.1.0"
    sample_ratio: float = 1.0


def _settings(service_name: str) -> TracingSettings | None:
    endpoint = os.getenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "").strip()
    if not endpoint:
        return None
    try:
        if len(endpoint) > 2048 or any(ord(char) < 33 or ord(char) > 126 for char in endpoint):
            raise ValueError
        parsed = urlsplit(endpoint)
        parsed.port
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.query or parsed.fragment or not parsed.path.endswith("/v1/traces")
                or not re.fullmatch(r"/[A-Za-z0-9_./~-]+", parsed.path)):
            raise ValueError
        headers: dict[str, str] = {}
        raw_headers = os.getenv("OTEL_EXPORTER_OTLP_TRACES_HEADERS", "")
        if len(raw_headers) > 8192:
            raise ValueError
        if raw_headers.strip():
            for item in raw_headers.split(","):
                key, separator, value = item.partition("=")
                key, value = key.strip().lower(), unquote(value.strip())
                if (not separator or not _HEADER_NAME.fullmatch(key) or not value
                        or len(value) > 4096 or any(ord(char) < 32 or ord(char) > 126 for char in value)
                        or key in headers or key in {"host", "content-length", "content-type", "transfer-encoding"}):
                    raise ValueError
                headers[key] = value
        environment = os.getenv("OTEL_DEPLOYMENT_ENVIRONMENT", "local")
        version = os.getenv("OTEL_SERVICE_VERSION", "0.1.0")
        ratio = float(os.getenv("OTEL_TRACES_SAMPLER_ARG", "1"))
        if (not _METADATA.fullmatch(service_name) or not _METADATA.fullmatch(environment)
                or not _METADATA.fullmatch(version) or not math.isfinite(ratio) or not 0 <= ratio <= 1):
            raise ValueError
        return TracingSettings(endpoint, headers, service_name, environment, version, ratio)
    except (ValueError, TypeError, OverflowError):
        _logger.warning("Tracing configuration rejected; tracing is disabled.")
        return None


def _attributes(values: Mapping[str, Any] | None) -> dict[str, Any]:
    """Deliberately narrow attributes shared by recording and export boundaries."""
    result: dict[str, Any] = {}
    if not isinstance(values, Mapping):
        return result
    for key, value in values.items():
        key = _ALIASES.get(key, key)
        if key in _STRING_ATTRIBUTES and isinstance(value, str) and value in _STRING_VALUES[key]:
            result[key] = value
        elif key in _BOOL_ATTRIBUTES and isinstance(value, bool):
            result[key] = value
        elif key in _NUMBER_ATTRIBUTES and type(value) in (int, float) and 0 <= value <= 1_000_000_000 and math.isfinite(value):
            result[key] = value
        elif key == "http.request.method" and isinstance(value, str) and value in _METHODS:
            result[key] = value
        elif key == "http.response.status_code" and type(value) is int and 100 <= value <= 599:
            result[key] = value
        elif key == "http.route" and isinstance(value, str) and _ROUTE.fullmatch(value):
            result[key] = value
    return result


def _name(value: Any) -> str:
    return value if isinstance(value, str) and value in _SPAN_NAMES else "operation"


class _SafeSpan:
    """Expose only safe attribute operations rather than raw SDK exception APIs."""

    def __init__(self, underlying=None):
        self._underlying = underlying

    def set_attribute(self, key: str, value: Any) -> None:
        self.set_attributes({key: value})

    def set_attributes(self, attributes: Mapping[str, Any]) -> None:
        try:
            if self._underlying is not None:
                self._underlying.set_attributes(_attributes(attributes))
        except Exception:
            pass

    def is_recording(self) -> bool:
        try:
            return bool(self._underlying is not None and self._underlying.is_recording())
        except Exception:
            return False


_NO_SPAN = _SafeSpan()


def create_runtime(settings: TracingSettings, *, exporter=None, on_export=None):
    """Construct a per-app SDK runtime; exporter injection is intended for tests.

    Example: runtime_factory=lambda settings: create_runtime(settings,
    exporter=InMemorySpanExporter()). The factory is invoked only when enabled.
    """
    from ._sdk import build_runtime

    return build_runtime(settings, exporter=exporter, on_export=on_export)


def instrument_app(app, service_name: str, *, trust_incoming: bool = False, runtime_factory=None):
    """Add optional pure-ASGI tracing and wrap the application's existing lifespan.

    Returns the supplied app. Invalid settings/SDK startup fail closed for tracing
    and leave application startup and request semantics intact.
    """
    settings = _settings(service_name)
    if settings is None or getattr(app.state, "_one_request_tracing", False):
        return app
    from ._middleware import TracingMiddleware

    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(owner):
        runtime = None
        try:
            runtime = (runtime_factory or create_runtime)(settings)
        except Exception:
            _logger.warning("Tracing startup failed; tracing is disabled.")
        owner.state.tracing = runtime
        try:
            async with original_lifespan(owner) as state:
                yield state
        finally:
            owner.state.tracing = None
            if runtime is not None:
                try:
                    await runtime.aclose()
                except Exception:
                    _logger.warning("Tracing shutdown did not complete.")

    try:
        app.add_middleware(TracingMiddleware, owner=app, trust_incoming=trust_incoming)
    except Exception:
        _logger.warning("Tracing setup failed; tracing is disabled.")
        return app
    app.router.lifespan_context = lifespan
    app.state._one_request_tracing = True
    return app


@contextmanager
def span(name: str, *, kind: str = "internal", attributes: Mapping[str, Any] | None = None):
    """Record a controlled operation, or yield a no-op without importing OTel."""
    runtime = _runtime.get()
    if runtime is None:
        yield _NO_SPAN
        return
    try:
        manager = runtime.open_span(_name(name), kind, _attributes(attributes))
        underlying = manager.__enter__()
    except Exception:
        yield _NO_SPAN
        return
    try:
        yield _SafeSpan(underlying)
    except BaseException as error:
        try:
            runtime.mark_error(underlying, error)
        except Exception:
            pass
        raise
    finally:
        # Never give SDK context managers exception text or traceback objects.
        try:
            manager.__exit__(None, None, None)
        except Exception:
            pass


def traced(name: str, *, kind: str = "internal"):
    """Decorate a sync/async function; arguments and return values are never read."""
    def decorate(function):
        if inspect.iscoroutinefunction(function):
            @wraps(function)
            async def asynchronous(*args, **kwargs):
                with span(name, kind=kind):
                    return await function(*args, **kwargs)
            return asynchronous

        @wraps(function)
        def synchronous(*args, **kwargs):
            with span(name, kind=kind):
                return function(*args, **kwargs)
        return synchronous
    return decorate


def set_attributes(**kwargs) -> None:
    """Set explicitly allowed attributes on the current app's active span."""
    runtime = _runtime.get()
    if runtime is not None:
        try:
            runtime.current_span().set_attributes(_attributes(kwargs))
        except Exception:
            pass


def internal_headers() -> dict[str, str]:
    """Return traceparent only; call exclusively at owned private HTTP boundaries."""
    runtime = _runtime.get()
    if runtime is None:
        return {}
    try:
        return runtime.internal_headers()
    except Exception:
        return {}


def capture_background_trace(runtime=None):
    """Finite in-memory context: runtime and traceparent only, never request data."""
    owner = _runtime.get() or runtime
    return (owner, internal_headers()) if owner is not None else None


@contextmanager
def background_trace(captured):
    if captured is None:
        yield
        return
    owner, headers = captured
    token = _runtime.set(owner)
    attached = None
    try:
        attached = owner.attach_background(headers)
    except Exception:
        pass
    try:
        yield
    finally:
        if attached is not None:
            try:
                owner.detach_background(attached)
            except Exception:
                pass
        _runtime.reset(token)


@asynccontextmanager
async def queued(semaphore, name: str):
    """Trace only waiting; preserve Lock/Semaphore ownership and cancellation."""
    acquired = False
    try:
        with span(name):
            await semaphore.acquire()
            acquired = True
        yield
    finally:
        if acquired:
            semaphore.release()
