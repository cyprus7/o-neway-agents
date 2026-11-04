"""Real ASGI service boundary with controlled browser allocations, no network."""
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import importlib
import json

import httpx
import pytest

from test_flight_domain import command, route


service = importlib.import_module("flight_search.app")


class Page:
    def set_default_timeout(self, value):
        assert value == 4000

    def set_default_navigation_timeout(self, value):
        assert value == 15000


class Context:
    def __init__(self, browser):
        self.browser, self.closed = browser, False

    async def new_page(self):
        return Page()

    async def close(self):
        self.closed = True


class Browser:
    def __init__(self, *, delayed=False):
        self.contexts, self.connected, self.delayed = [], True, delayed
        self.allocated = asyncio.Event()

    def is_connected(self):
        return self.connected

    async def new_context(self, **options):
        assert options == {"locale":"en-US", "service_workers":"block", "accept_downloads":False}
        assert all(context.closed for context in self.contexts)
        context = Context(self)
        self.contexts.append(context)
        self.allocated.set()
        if self.delayed:
            await asyncio.Event().wait()  # Allocated remotely, not yet returned.
        return context

    async def close(self):
        self.connected = False
        for context in self.contexts:
            context.closed = True


async def guards(context):
    assert not context.closed


def make_app(browser, recipe):
    @asynccontextmanager
    async def factory():
        try:
            yield browser
        finally:
            if browser is not None:
                await browser.close()
    return service.create_app(browser_factory=factory, recipe=recipe, guard_factory=guards)


async def until(predicate):
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(.005)


def payload(**kwargs):
    return command(**kwargs).model_dump(mode="json")


@pytest.fixture(autouse=True)
def tracing_off(monkeypatch):
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", raising=False)


def test_real_http_boundary_reference_original_times_and_safe_counters():
    async def run():
        observed = route()
        async def recipe(page, cmd):
            return {"outcome":"reference", "diagnostic_code":"flight_reference", "route":observed, "candidate_count":1}
        browser = Browser()
        app = make_app(browser, recipe)
        async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://flight") as client:
            response = await client.post("/v1/search", json=payload())
            assert response.status_code == 200
            body = response.json()
            assert body["route"]["price"]["amount"] == "1622"
            assert body["route"]["observed_at"] == observed.model_dump(mode="json")["observed_at"]
            assert body["diagnostics"][0]["external_dispatched"] is True
            assert body["diagnostics"][0]["retrieved_at"] == body["route"]["retrieved_at"]
            counters = (await client.get("/metrics")).json()
            assert counters["flight_contexts_created"] == counters["flight_source_requests"] == 1
            assert counters["flight_active_contexts"] == counters["flight_pending"] == counters["flight_jobs"] == 0
            assert (await client.get("/ready")).status_code == 200
            assert all(context.closed for context in browser.contexts)
    asyncio.run(run())


def test_dedupe_has_one_context_one_actual_dispatch_and_one_cancelled_waiter_does_not_cancel_peer():
    async def run():
        entered, release = asyncio.Event(), asyncio.Event()
        calls = 0
        async def recipe(page, cmd):
            nonlocal calls
            calls += 1
            entered.set()
            await release.wait()
            return {"outcome":"no_offers", "diagnostic_code":"flight_no_compatible_itinerary"}
        browser = Browser()
        app = make_app(browser, recipe)
        async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://flight") as client:
            first = asyncio.create_task(client.post("/v1/search",json=payload(request_id="first")))
            await entered.wait()
            second = asyncio.create_task(client.post("/v1/search",json=payload(request_id="second")))
            await until(lambda: app.state.counters["flight_dedup_hits"] == 1)
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first
            assert not browser.contexts[0].closed and calls == 1
            release.set()
            body = (await second).json()
            assert body["request_id"] == "second" and body["diagnostics"][0]["external_dispatched"] is True
            assert browser.contexts[0].closed and app.state.pending == 0 and not app.state.jobs
    asyncio.run(run())


def test_last_waiter_cancellation_cancels_recipe_closes_context_and_allows_next_request():
    async def run():
        entered, cancelled = asyncio.Event(), asyncio.Event()
        async def recipe(page, cmd):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        browser = Browser()
        app = make_app(browser, recipe)
        async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://flight") as client:
            task = asyncio.create_task(client.post("/v1/search",json=payload()))
            await entered.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert cancelled.is_set() and browser.contexts[0].closed
            assert app.state.pending == 0 and not app.state.jobs and app.state.slots._value == 1
            assert app.state.counters["flight_active_contexts"] == 0
    asyncio.run(run())


def test_delayed_new_context_allocation_cancellation_closes_owned_browser_and_invalidates_readiness():
    async def run():
        async def no_recipe(*_):
            raise AssertionError("Allocation never returned")
        browser = Browser(delayed=True)
        app = make_app(browser, no_recipe)
        async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://flight") as client:
            task = asyncio.create_task(client.post("/v1/search",json=payload()))
            await browser.allocated.wait()
            assert not browser.contexts[0].closed
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert browser.contexts[0].closed and not browser.connected
            assert (await client.get("/ready")).status_code == 503
            assert app.state.counters["flight_source_requests"] == 0 and app.state.slots._value == 1
    asyncio.run(run())


def test_different_searches_serialize_browser_and_queue_cancel_never_dispatches():
    async def run():
        entered, release = asyncio.Event(), asyncio.Event()
        async def recipe(page, cmd):
            entered.set()
            await release.wait()
            return {"outcome":"no_offers", "diagnostic_code":"flight_no_compatible_itinerary"}
        browser = Browser()
        app = make_app(browser, recipe)
        async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://flight") as client:
            first = asyncio.create_task(client.post("/v1/search",json=payload()))
            await entered.wait()
            second = asyncio.create_task(client.post("/v1/search",json=payload(target_itinerary_id="a"*64)))
            await until(lambda: app.state.pending == 2)
            assert len(browser.contexts) == 1 and app.state.counters["flight_source_requests"] == 1
            second.cancel()
            with pytest.raises(asyncio.CancelledError):
                await second
            release.set()
            assert (await first).status_code == 200
            assert len(browser.contexts) == 1 and app.state.counters["flight_cancelled"] == 1
            assert app.state.pending == 0 and app.state.slots._value == 1
    asyncio.run(run())


def test_deadline_includes_queue_work_cleanup_and_source_timeout_is_typed(monkeypatch):
    monkeypatch.setattr(service, "OPERATION_SECONDS", .18)
    monkeypatch.setattr(service, "CLEANUP_SECONDS", .05)
    async def run():
        async def slow(*_):
            await asyncio.sleep(10)
        browser = Browser()
        app = make_app(browser, slow)
        async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://flight") as client:
            started = asyncio.get_running_loop().time()
            body = (await client.post("/v1/search",json=payload())).json()
            assert body["outcome"] == "timeout" and body["diagnostic_code"] == "flight_timeout"
            assert asyncio.get_running_loop().time()-started < .5
            assert browser.contexts[0].closed and app.state.counters["flight_timeouts"] == 1
    asyncio.run(run())


def test_real_asgi_http_disconnect_stops_browser_work():
    async def run():
        entered, disconnect, stopped = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def slow(*_):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()
        browser = Browser()
        app = make_app(browser, slow)
        body = json.dumps(payload()).encode()
        delivered = False
        async def receive():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type":"http.request", "body":body, "more_body":False}
            await disconnect.wait()
            return {"type":"http.disconnect"}
        async def send(_):
            pass
        scope = {"type":"http", "asgi":{"version":"3.0"}, "http_version":"1.1", "method":"POST",
            "scheme":"http", "path":"/v1/search", "raw_path":b"/v1/search", "query_string":b"",
            "headers":[(b"content-type",b"application/json")], "client":("127.0.0.1",1234),"server":("flight",8080)}
        async with app.router.lifespan_context(app):
            task = asyncio.create_task(app(scope,receive,send))
            await entered.wait()
            disconnect.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task,1)
            assert stopped.is_set() and browser.contexts[0].closed
            assert app.state.pending == 0 and not app.state.jobs
    asyncio.run(run())


def test_body_validation_preflight_and_pending_cap_never_create_context():
    async def run():
        async def never(*_):
            raise AssertionError("No source request authorized")
        browser = Browser()
        app = make_app(browser, never)
        async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://flight") as client:
            assert (await client.post("/v1/search",content=b"x")).status_code == 415
            assert (await client.post("/v1/search",content=b"{",headers={"content-type":"application/json"})).status_code == 422
            assert (await client.post("/v1/search",content=b"x"*(service.MAX_BODY+1),headers={"content-type":"application/json"})).status_code == 413
            assert (await client.post("/v1/search?url=arbitrary",json=payload())).status_code == 422
            context = command().request_context.model_copy(update={"children_ages":(5,)})
            body = (await client.post("/v1/search",json=payload(request_context=context))).json()
            assert body["outcome"] == "unsupported_party" and body["diagnostics"][0]["external_dispatched"] is False
            expired = datetime.now(timezone.utc)-timedelta(seconds=1)
            body = (await client.post("/v1/search",json=payload(deadline_at=expired))).json()
            assert body["diagnostic_code"] == "flight_invalid_deadline"
            app.state.pending = service.MAX_PENDING
            assert (await client.post("/v1/search",json=payload())).status_code == 503
            assert app.state.pending == 16 and not browser.contexts
            app.state.pending = 0
    asyncio.run(run())


def test_missing_browser_is_not_configured_without_source_dispatch():
    async def run():
        async def never(*_):
            raise AssertionError("No browser")
        app = make_app(None, never)
        async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://flight") as client:
            body = (await client.post("/v1/search",json=payload())).json()
            assert body["diagnostic_code"] == "flight_browser_unavailable"
            assert not body["diagnostics"][0]["external_dispatched"]
            assert (await client.get("/ready")).status_code == 503
    asyncio.run(run())


def test_sixteen_actual_pending_requests_are_bounded_and_drain_through_one_context():
    async def run():
        release = asyncio.Event()
        async def recipe(*_):
            await release.wait()
            return {"outcome":"no_offers", "diagnostic_code":"flight_target_not_found"}
        browser = Browser()
        app = make_app(browser, recipe)
        async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://flight") as client:
            tasks = [asyncio.create_task(client.post("/v1/search",json=payload(target_itinerary_id=f"{index:064x}")))
                     for index in range(16)]
            await until(lambda: app.state.pending == 16)
            await browser.allocated.wait()
            assert len(browser.contexts) == 1 and app.state.counters["flight_source_requests"] == 1
            overflow = await client.post("/v1/search",json=payload(target_itinerary_id="f"*64))
            assert overflow.status_code == 503 and app.state.pending == 16 and len(app.state.jobs) == 16
            release.set()
            responses = await asyncio.gather(*tasks)
            assert all(response.status_code == 200 for response in responses)
            assert len(browser.contexts) == app.state.counters["flight_source_requests"] == 16
            assert all(context.closed for context in browser.contexts)
            assert app.state.pending == app.state.counters["flight_active_contexts"] == 0 and not app.state.jobs
    asyncio.run(run())


def test_context_close_failure_closes_browser_before_allowing_next_work():
    async def run():
        browser = Browser()
        original_new_context = browser.new_context
        async def allocated(**kwargs):
            context = await original_new_context(**kwargs)
            async def failed_close():
                raise RuntimeError("sensitive detail must not be projected")
            context.close = failed_close
            return context
        browser.new_context = allocated
        async def recipe(*_):
            return {"outcome":"no_offers", "diagnostic_code":"flight_no_compatible_itinerary"}
        app = make_app(browser, recipe)
        async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://flight") as client:
            assert (await client.post("/v1/search",json=payload())).status_code == 200
            assert browser.contexts[0].closed and not browser.connected
            assert (await client.get("/ready")).status_code == 503
            second = (await client.post("/v1/search",json=payload())).json()
            assert second["diagnostic_code"] == "flight_browser_unavailable"
            assert len(browser.contexts) == 1 and app.state.counters["flight_source_requests"] == 1
            assert "sensitive" not in json.dumps(second)
    asyncio.run(run())
