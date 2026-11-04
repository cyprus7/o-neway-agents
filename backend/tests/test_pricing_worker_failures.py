"""Offline private-worker boundary failures must not masquerade as timeouts."""
from dataclasses import replace
from datetime import date

import httpx
import pytest

from backend.app.geography import find_place
from backend.app.models import TravelIntent
from backend.app.pricing.providers import KeylessPricing, context_for


class Cache:
    def cache_get(self, key):
        return None


@pytest.mark.asyncio
@pytest.mark.parametrize("airport", [None, "", "Male", "mle", "MV"])
async def test_missing_or_invalid_iata_does_not_dispatch_or_mutate_identity(airport):
    place = replace(find_place("Афины"), airport=airport)
    origin = find_place("Москва")
    intent = TravelIntent(date_from=date(2026, 12, 31), date_to=date(2027, 1, 1))
    context = context_for("stay", place, origin, intent)
    def forbidden(request):
        raise AssertionError("Invalid worker request must never dispatch")
    async with httpx.AsyncClient(transport=httpx.MockTransport(forbidden)) as client:
        router = KeylessPricing(client, Cache())
        router.worker_url = "http://price-worker:8080"
        original = (place.name, place.airport, context.entity_id)
        quote, rows = await router._browser(context, place)
        assert quote.outcome == "context_mismatch"
        assert rows[0]["diagnostic_code"] == "unsupported_destination"
        assert rows[0]["worker_dispatched"] is False
        assert rows[0]["external_dispatched"] is False
        assert rows[0]["delivery"] == "not_attempted"
        assert (place.name, place.airport, context.entity_id) == original


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [422, 503, "connect", "timeout"])
async def test_status_and_connection_failures_are_distinct_from_timeouts(failure):
    place, origin = find_place("Афины"), find_place("Москва")
    intent = TravelIntent(date_from=date(2026, 12, 31), date_to=date(2027, 1, 1))
    context = context_for("stay", place, origin, intent)
    calls = []
    def handler(request):
        calls.append(request)
        if failure == "timeout":
            raise httpx.ReadTimeout("private payload must not leak", request=request)
        if failure == "connect":
            raise httpx.ConnectError("private payload must not leak", request=request)
        return httpx.Response(failure, json={"detail": "private payload must not leak"})
    store = Cache()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        router = KeylessPricing(client, store)
        router.worker_url = "http://price-worker:8080"
        quote, rows = await router._browser(context, place)
        assert len(calls) == 1
        expected = "timeout" if failure == "timeout" else "unavailable"
        assert quote.outcome == expected
        assert rows[0]["diagnostic_code"] == ("worker_timeout" if failure == "timeout" else "worker_http_error")
        assert rows[0]["worker_dispatched"] is True
        assert "private payload" not in str(rows) + quote.model_dump_json()


@pytest.mark.asyncio
async def test_worker_cancellation_propagates_without_public_failure_conversion():
    import asyncio
    place, origin = find_place("Athens"), find_place("Moscow")
    intent = TravelIntent(date_from=date(2026, 12, 31), date_to=date(2027, 1, 1))
    started = asyncio.Event()
    canceled = asyncio.Event()

    async def handler(request):
        started.set()
        try:
            await asyncio.Future()
        finally:
            canceled.set()

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        router = KeylessPricing(client, Cache())
        router.worker_url = "http://price-worker:8080"
        task = asyncio.create_task(router._browser(context_for("stay", place, origin, intent), place))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert canceled.is_set()
        assert router._worker_slots._value == 2
