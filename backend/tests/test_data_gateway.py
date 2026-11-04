"""Typed gateway boundary with isolated cache and forbidden upstream dispatch."""
import asyncio
from datetime import datetime, timedelta, timezone
import json
import subprocess
import sys

import httpx
import pytest

from backend.app.data_gateway import create_gateway_app
from backend.app.domain.travel_data import (DataIntent, DestinationInput, EmptyInput, InventoryInput, MAX_REQUEST_BYTES,
    PlaceRef, RefreshInput, ResolveInput, fingerprint, make_request)
from backend.app.geography import find_place
from backend.app.infrastructure.http_travel_data import HttpTravelDataPort
from backend.app.models import Evidence, TravelIntent, TravelComponent
from backend.app.pricing.providers import unknown
from backend.app.domain.travel_data import ComponentBundle


def no_network(request):
    raise AssertionError("Gateway must not dispatch external data/model requests in this test")


@pytest.mark.asyncio
async def test_all_operations_cross_http_and_gateway_cache_has_no_sessions(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCAL_CATALOG_ENABLED", "1")
    monkeypatch.setenv("WEB_FALLBACK_ENABLED", "0")
    monkeypatch.setenv("KEYLESS_PRICING_ENABLED", "1")
    app = create_gateway_app(database_path=str(tmp_path / "gateway.db"), transport=httpx.MockTransport(no_network))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as client:
            port = HttpTravelDataPort(client, "http://provider-gateway:8091")
            intent = TravelIntent(origin="Варшава", destination="Ларнака", date_from="2027-06-01", date_to="2027-06-08")
            place, origin = find_place("Ларнака"), find_place("Варшава")
            assert (await port.resolve_place("Ларнака")).name == place.name
            assert (await port.capabilities())["providers"]["catalog"]["configured"] is True
            weather = await port.weather(place, intent)
            assert weather["kind"] == "unknown"
            assert Evidence.model_validate(weather["evidence"][0]).source_url is None
            facts = await port.search(place, intent)
            assert any(fact.freshness == "snapshot" for fact in facts)
            current = await port.inventory(place, origin, intent)
            assert current.stay.details["catalog_property_id"]
            previous = current.model_dump_json()
            refreshed = await port.refresh_inventory(place, origin, intent, current)
            assert current.model_dump_json() == previous
            assert refreshed.stay.details["catalog_property_id"] == current.stay.details["catalog_property_id"]
            assert refreshed.stay.evidence == current.stay.evidence
            assert refreshed.stay.quote.value.kind == "unknown"
            assert (await port.regional_events(place, intent)).status == "not_checked"
            assert (await client.get("http://provider-gateway:8091/health")).status_code == 200
            assert (await client.get("http://provider-gateway:8091/ready")).status_code == 200
        tables = {row[0] for row in app.state.cache.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert tables == {"cache", "metrics"}
        assert app.state.cache.metrics()["gateway_inventory_completed"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["extra", "url", "version", "fingerprint", "expired", "large"])
async def test_gateway_rejects_invalid_commands_with_controlled_errors(tmp_path, mutation):
    app = create_gateway_app(database_path=str(tmp_path / "gateway.db"), transport=httpx.MockTransport(no_network))
    async with app.router.lifespan_context(app):
        payload = make_request("resolve_place", ResolveInput(name="Ларнака")).model_dump(mode="json")
        if mutation == "extra": payload["llm_api_key"] = "never-echo-this"
        if mutation == "url": payload["data"]["target_url"] = "http://127.0.0.1/private"
        if mutation == "version": payload["version"] = "travel-data.v999"
        if mutation == "fingerprint": payload["fingerprint"] = "0" * 64
        if mutation == "expired": payload["deadline_at"] = (datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat()
        if mutation == "large": payload["data"]["name"] = "x" * MAX_REQUEST_BYTES
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://provider-gateway:8091") as client:
            result = await client.post("/v1/places/resolve", json=payload)
        assert result.status_code == (504 if mutation == "expired" else 413 if mutation == "large" else 422)
        assert "never-echo-this" not in result.text and "private" not in result.text


@pytest.mark.asyncio
async def test_gateway_queue_time_counts_toward_request_deadline(tmp_path):
    app = create_gateway_app(database_path=str(tmp_path / "gateway.db"), transport=httpx.MockTransport(no_network))
    async with app.router.lifespan_context(app):
        for _ in range(8): await app.state.slots.acquire()
        payload = make_request("resolve_place", ResolveInput(name="Ларнака"), timeout=0.02)
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://provider-gateway:8091") as client:
                result = await asyncio.wait_for(client.post("/v1/places/resolve", json=payload.model_dump(mode="json")), timeout=0.2)
            assert result.status_code == 504
            assert app.state.pending == 0
        finally:
            for _ in range(8): app.state.slots.release()


def test_gateway_import_does_not_load_session_store_model_client_or_agent_composition():
    script = "import sys,json; import backend.app.data_gateway; print(json.dumps([name for name in ('backend.app.storage','backend.app.llm','backend.app.main','backend.app.planner') if name in sys.modules]))"
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, check=True)
    assert json.loads(result.stdout) == []


@pytest.mark.asyncio
async def test_expired_requests_cancel_last_waiter_sources_before_releasing_capacity(tmp_path, monkeypatch):
    monkeypatch.setenv("WEB_FALLBACK_ENABLED", "1")
    monkeypatch.setenv("KEYLESS_PRICING_ENABLED", "1")
    app = create_gateway_app(database_path=str(tmp_path / "gateway.db"), transport=httpx.MockTransport(no_network))
    async with app.router.lifespan_context(app):
        active, peak, started = 0, 0, 0
        async def source(component, context, place, origin, intent):
            nonlocal active, peak, started
            active += 1
            started += 1
            peak = max(peak, active)
            try:
                await asyncio.Event().wait()
            finally:
                active -= 1
        app.state.providers.pricing._find = source
        current = ComponentBundle.from_components([TravelComponent(title=name, description="Synthetic") for name in ("flight", "stay", "transfer")])
        requests = []
        for day in range(1, 11):
            intent = TravelIntent(origin="Варшава", date_from=f"2027-06-{day:02d}", date_to=f"2027-06-{day+7:02d}")
            data = RefreshInput(place=PlaceRef.from_place(find_place("Ларнака")), origin=PlaceRef.from_place(find_place("Варшава")),
                                intent=DataIntent.from_intent(intent), current=current)
            requests.append(make_request("refresh_inventory", data, timeout=0.04).model_dump(mode="json"))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://provider-gateway:8091") as client:
            responses = await asyncio.gather(*(client.post("/v1/inventory/refresh", json=payload) for payload in requests))
        assert all(response.status_code == 504 for response in responses)
        assert started > 0 and peak <= 8
        assert active == 0 and app.state.pending == 0
        assert app.state.slots._value == 8
        assert not app.state.providers.pricing._inflight
        assert not app.state.providers.pricing._waiters


@pytest.mark.asyncio
async def test_cancelling_one_request_preserves_another_singleflight_waiter(tmp_path, monkeypatch):
    monkeypatch.setenv("WEB_FALLBACK_ENABLED", "1")
    monkeypatch.setenv("KEYLESS_PRICING_ENABLED", "1")
    app = create_gateway_app(database_path=str(tmp_path / "gateway.db"), transport=httpx.MockTransport(no_network))
    async with app.router.lifespan_context(app):
        started = []
        async def source(component, context, place, origin, intent):
            started.append(component)
            await asyncio.sleep(0.06)
            return unknown("synthetic", context, "Synthetic unavailable"), [], None
        app.state.providers.pricing._find = source
        current = ComponentBundle.from_components([TravelComponent(title=name, description="Synthetic") for name in ("flight", "stay", "transfer")])
        intent = TravelIntent(origin="Варшава", date_from="2027-06-01", date_to="2027-06-08")
        data = RefreshInput(place=PlaceRef.from_place(find_place("Ларнака")), origin=PlaceRef.from_place(find_place("Варшава")),
                            intent=DataIntent.from_intent(intent), current=current)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://provider-gateway:8091") as client:
            first, second = await asyncio.gather(*(client.post("/v1/inventory/refresh", json=make_request("refresh_inventory", data, timeout=timeout).model_dump(mode="json"))
                                                   for timeout in (0.02, 0.3)))
        assert first.status_code == 504 and second.status_code == 200
        assert sorted(started) == ["flight", "stay"]
        assert not app.state.providers.pricing._inflight and not app.state.providers.pricing._waiters


@pytest.mark.asyncio
async def test_custom_company_data_port_factory_owns_no_default_provider_and_closes(tmp_path, monkeypatch):
    from backend.app import data_gateway
    created, closed = [], []
    class CompanyPort:
        async def resolve_place(self, name):
            return find_place(name)
        async def capabilities(self):
            return {"external_data_fallback_enabled": False, "providers": {"company_api": {"configured": True}}, "pricing": {"refresh_enabled": False}}
        async def aclose(self):
            closed.append(True)
    def factory(client, cache):
        created.append((client, cache))
        return CompanyPort()
    def forbidden_default(*args):
        raise AssertionError("Explicit company port must replace default providers")
    monkeypatch.setattr(data_gateway, "Providers", forbidden_default)
    app = create_gateway_app(database_path=str(tmp_path / "company.db"), transport=httpx.MockTransport(no_network), port_factory=factory)
    async with app.router.lifespan_context(app):
        assert app.state.providers is None
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as client:
            port = HttpTravelDataPort(client, "http://provider-gateway:8091")
            assert (await port.resolve_place("Ларнака")).country_code == "CY"
            assert (await port.capabilities())["providers"]["company_api"]["configured"] is True
            assert (await client.get("http://provider-gateway:8091/ready")).status_code == 200
    assert len(created) == len(closed) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["factory", "default_provider", "port_close"])
async def test_gateway_startup_and_custom_shutdown_failures_close_owned_resources(tmp_path, monkeypatch, failure):
    from backend.app import data_gateway
    original_cache, original_client = data_gateway.ProviderCache, data_gateway.httpx.AsyncClient
    caches, clients = [], []
    class RecordingCache(original_cache):
        closed = False
        def __init__(self, path):
            super().__init__(path)
            caches.append(self)
        def close(self):
            super().close()
            self.closed = True
    class RecordingClient(original_client):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            clients.append(self)
    class FailingClosePort:
        async def aclose(self):
            raise RuntimeError("port_close")
    def failing_factory(client, cache):
        raise RuntimeError("factory")
    def failing_provider(client, cache):
        raise RuntimeError("default_provider")
    monkeypatch.setattr(data_gateway, "ProviderCache", RecordingCache)
    monkeypatch.setattr(data_gateway.httpx, "AsyncClient", RecordingClient)
    factory = failing_factory if failure == "factory" else (lambda *_: FailingClosePort())
    if failure == "default_provider":
        monkeypatch.setattr(data_gateway, "Providers", failing_provider)
        factory = None
    app = create_gateway_app(database_path=str(tmp_path / "failed-startup.db"),
                             transport=httpx.MockTransport(no_network), port_factory=factory)
    with pytest.raises(RuntimeError, match=failure):
        async with app.router.lifespan_context(app):
            assert failure == "port_close"
    assert len(caches) == len(clients) == 1
    assert caches[0].closed is True and clients[0].is_closed is True


@pytest.mark.asyncio
async def test_gateway_shutdown_awaits_pricing_tasks_before_closing_cache(tmp_path):
    app = create_gateway_app(database_path=str(tmp_path / "shutdown.db"), transport=httpx.MockTransport(no_network))
    cancelled = []
    async def source():
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.append(True)
    async with app.router.lifespan_context(app):
        task = asyncio.create_task(source())
        await asyncio.sleep(0)
        app.state.providers.pricing._inflight["synthetic-shutdown"] = task
    assert task.cancelled() and cancelled == [True]
