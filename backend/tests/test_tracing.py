"""Tracing boundaries with ASGI fixtures and a loopback OTLP receiver; no provider/model calls."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import gzip
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

import httpx
import pytest
from fastapi import FastAPI, Request
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import Status, StatusCode


ROOT = Path(__file__).resolve().parents[2]
CANARY = "sensitive-traveler-prompt-token-DO-NOT-EXPORT"
ATTACKER_TRACE = "1234567890abcdef1234567890abcdef"
ATTACKER_PARENT = "1234567890abcdef"
TRACEPARENT = f"00-{ATTACKER_TRACE}-{ATTACKER_PARENT}-01"


@pytest.fixture(autouse=True)
def isolated_telemetry_environment(monkeypatch):
    # main.py loads the operator .env during collection in the broader suite.
    # No test may export to that receiver or depend on its sampling settings.
    for name in tuple(os.environ):
        if name.startswith("OTEL_"):
            monkeypatch.delenv(name, raising=False)


class RecordingExporter(InMemorySpanExporter):
    def __init__(self):
        super().__init__()
        self.shutdown_calls = 0

    def shutdown(self):
        self.shutdown_calls += 1
        super().shutdown()


class Recorders:
    def __init__(self, exporter_factory=RecordingExporter):
        from one_request_telemetry import create_runtime
        self._build_runtime = create_runtime
        self.exporter_factory = exporter_factory
        self.runtimes = []
        self.exporters = []

    def __call__(self, settings):
        exporter = self.exporter_factory()
        runtime = self._build_runtime(settings, exporter=exporter)
        self.runtimes.append(runtime)
        self.exporters.append(exporter)
        return runtime

    def spans(self):
        for runtime in self.runtimes:
            assert runtime.force_flush(timeout_millis=3000)
        return tuple(span for exporter in self.exporters for span in exporter.get_finished_spans())


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "http://127.0.0.1:4318/v1/traces")
    monkeypatch.setenv("OTEL_TRACES_SAMPLER_ARG", "1")
    monkeypatch.setenv("OTEL_DEPLOYMENT_ENVIRONMENT", "test")
    return Recorders()


def instrument(app, recorders, name="one-request-agent", *, private=False):
    from one_request_telemetry import instrument_app
    instrument_app(app, name, trust_incoming=private, runtime_factory=recorders)
    return app


def exported_text(spans):
    return "\n".join(span.to_json() for span in spans)


@pytest.fixture
def otlp_receiver():
    """A receiver we own completely, including its status, headers and raw bytes."""
    captured = []
    configuration = {"status": 200, "body": b"", "headers": {}, "drip_seconds": 0}

    class Receiver(BaseHTTPRequestHandler):
        def do_POST(self):
            captured.append({"path": self.path, "headers": dict(self.headers.items()),
                             "body": self.rfile.read(int(self.headers.get("Content-Length", "0")))})
            self.send_response(configuration["status"])
            for key, value in configuration["headers"].items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(configuration["body"])))
            self.end_headers()
            try:
                if configuration["drip_seconds"]:
                    for value in configuration["body"]:
                        self.wfile.write(bytes([value]))
                        self.wfile.flush()
                        time.sleep(configuration["drip_seconds"])
                else:
                    self.wfile.write(configuration["body"])
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                # Error/redirect bodies are intentionally closed without reading.
                pass

        def log_message(self, format, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Receiver)
    server.daemon_threads = True
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1/traces", captured, configuration
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


@pytest.mark.parametrize("endpoint", [None, "", "  "])
def test_disabled_owned_services_import_no_sdk_exporter_or_tracing_middleware(endpoint):
    # A fresh interpreter is essential: the test exporter itself imports the SDK.
    # Suppress dotenv discovery so a private operator config cannot enable tracing.
    script = """
import asyncio, json, sys
import dotenv
dotenv.load_dotenv = lambda *args, **kwargs: False
from backend.app.main import create_app
from backend.app.data_gateway import create_gateway_app
from price_worker.app import app as browser
from flight_search.app import app as flight_browser
from fastapi import FastAPI
from one_request_telemetry import instrument_app
calls = []
plain = FastAPI()
instrument_app(plain, 'one-request-agent', runtime_factory=lambda settings: calls.append(settings))
apps = [create_app(), create_gateway_app(), browser, flight_browser, plain]
async def verify():
    async with plain.router.lifespan_context(plain):
        pass
asyncio.run(verify())
print(json.dumps({'modules': [name for name in sys.modules if name.startswith(('opentelemetry.sdk', 'opentelemetry.exporter'))],
 'middleware': [str(item.cls) for app in apps for item in app.user_middleware if 'trac' in str(item.cls).lower()],
 'factories': len(calls)}))
"""
    environment = {name: value for name, value in os.environ.items() if not name.startswith("OTEL_")}
    if endpoint is not None:
        environment["OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"] = endpoint
    result = subprocess.run([sys.executable, "-c", script], cwd=ROOT, env=environment,
                            capture_output=True, text=True, check=True, timeout=20)
    assert json.loads(result.stdout) == {"modules": [], "middleware": [], "factories": 0}


@pytest.mark.asyncio
@pytest.mark.parametrize("setting,value", [
    ("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", f"http://user:{CANARY}@receiver/v1/traces"),
    ("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", f"https://receiver/v1/traces?token={CANARY}"),
    ("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", f"file:///{CANARY}"),
    ("OTEL_EXPORTER_OTLP_TRACES_HEADERS", CANARY),
    ("OTEL_TRACES_SAMPLER_ARG", CANARY),
])
async def test_invalid_config_is_safe_and_leaves_application_usable(enabled, monkeypatch, caplog, setting, value):
    monkeypatch.setenv(setting, value)
    app = FastAPI()

    @app.get("/work")
    async def work():
        return {"ok": True}

    instrument(app, enabled)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://agent") as client:
            assert (await client.get("/work")).json() == {"ok": True}
    assert not enabled.runtimes
    assert CANARY not in caplog.text


@pytest.mark.asyncio
async def test_public_request_ignores_remote_parent_and_private_request_accepts_only_traceparent(enabled):
    public, private = FastAPI(), FastAPI()
    received = []

    @private.post("/v1/work")
    async def private_work(request: Request):
        from one_request_telemetry import internal_headers
        received.append(internal_headers())
        return {"ok": True}

    instrument(private, enabled, "one-request-provider-gateway", private=True)

    @public.post("/api/work/{session_id}")
    async def public_work():
        from one_request_telemetry import internal_headers, span
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=private), base_url="http://gateway") as client:
            with span("travel_data.resolve_place"):
                return (await client.post("/v1/work", headers=internal_headers())).json()

    instrument(public, enabled)
    async with private.router.lifespan_context(private), public.router.lifespan_context(public):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=public), base_url="http://agent") as client:
            result = await client.post(f"/api/work/{CANARY}?secret={CANARY}", headers={
                "traceparent": TRACEPARENT, "tracestate": f"vendor={CANARY}", "baggage": f"token={CANARY}",
                "Authorization": f"Bearer {CANARY}",
            }, json={"content": CANARY})
        assert result.json() == {"ok": True}
        spans = enabled.spans()
    roots = [item for item in spans if item.parent is None]
    assert len(roots) == 1
    assert roots[0].context.trace_id != int(ATTACKER_TRACE, 16)
    assert len({item.context.trace_id for item in spans}) == 1
    gateway = next(item for item in spans if item.resource.attributes["service.name"] == "one-request-provider-gateway")
    client_span = next(item for item in spans if item.name == "travel_data.resolve_place")
    assert gateway.parent.span_id == client_span.context.span_id
    assert received and all(set(headers) == {"traceparent"} for headers in received)
    assert CANARY not in exported_text(spans)
    assert all(not item.events and not item.status.description for item in spans)


@pytest.mark.asyncio
@pytest.mark.parametrize("parent", [TRACEPARENT, "invalid", "00-" + "0" * 32 + "-" + ATTACKER_PARENT + "-01"])
async def test_private_parent_validation(enabled, parent):
    app = FastAPI()

    @app.get("/v1/work")
    async def work():
        return {"ok": True}

    instrument(app, enabled, "one-request-provider-gateway", private=True)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gateway") as client:
            assert (await client.get("/v1/work", headers={"traceparent": parent})).status_code == 200
        spans = enabled.spans()
    assert len(spans) == 1
    if parent == TRACEPARENT:
        assert spans[0].context.trace_id == int(ATTACKER_TRACE, 16)
        assert spans[0].parent.span_id == int(ATTACKER_PARENT, 16)
    else:
        assert spans[0].parent is None


@pytest.mark.asyncio
async def test_request_and_sdk_canaries_are_removed_from_entire_export(enabled, monkeypatch):
    monkeypatch.setenv("OTEL_RESOURCE_ATTRIBUTES", f"private.resource={CANARY},service.instance.id={CANARY}")
    monkeypatch.setenv("OTEL_SERVICE_NAME", CANARY)
    app = FastAPI()

    @app.post("/api/work/{session_id}")
    async def work(request: Request):
        assert CANARY in (await request.body()).decode()
        runtime = request.app.state.tracing
        # Deliberately bypass the safe facade to exercise the final export guard.
        dynamic_name = CANARY.lower().replace("-", "_")
        private_tracer = runtime.provider.get_tracer(CANARY, CANARY)
        with private_tracer.start_as_current_span(dynamic_name, record_exception=False,
                                                 set_status_on_exception=False) as raw:
            raw.set_attribute("session.id", CANARY)
            raw.set_attribute("http.url", f"https://{CANARY}/?token={CANARY}")
            raw.set_attribute("error.type", CANARY)
            raw.set_attribute("one_request.operation", CANARY)
            raw.set_attribute("one_request.provider", CANARY)
            raw.set_attribute("one_request.outcome", CANARY)
            raw.add_event(CANARY, {"exception.message": CANARY, "exception.stacktrace": CANARY})
            raw.set_status(Status(StatusCode.ERROR, CANARY))
        raise ValueError(CANARY)

    instrument(app, enabled)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
                                     base_url=f"http://{CANARY}.invalid") as client:
            result = await client.post(f"/api/work/{CANARY}?query={CANARY}", content=CANARY,
                                       headers={"Authorization": f"Bearer {CANARY}"})
        assert result.status_code == 500
        spans = enabled.spans()
    assert len(spans) == 2
    assert CANARY not in exported_text(spans)
    assert CANARY.lower().replace("-", "_") not in exported_text(spans)
    assert all(not item.events and not item.status.description for item in spans)
    assert all(set(item.resource.attributes) <= {"service.name", "service.version", "deployment.environment.name"}
               for item in spans)
    assert any(item.status.status_code == StatusCode.ERROR for item in spans)


@pytest.mark.asyncio
async def test_probe_static_and_event_stream_requests_are_excluded(enabled):
    app = FastAPI()
    paths = ["/health", "/ready", "/metrics", "/assets/app.js", "/api/sessions/private-session/events"]

    async def response():
        return {"ok": True}

    for path in paths:
        app.add_api_route(path, response)
    app.add_api_route("/api/work", response)
    instrument(app, enabled)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://agent") as client:
            for path in paths + ["/api/work"]:
                assert (await client.get(path)).status_code == 200
        spans = enabled.spans()
    assert len(spans) == 1
    assert "/api/work" in spans[0].name


@pytest.mark.asyncio
async def test_background_work_after_202_keeps_parent(enabled):
    from one_request_telemetry import traced
    started, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
    app, tasks = FastAPI(), []

    @traced("planner.run")
    async def background():
        started.set()
        await release.wait()
        finished.set()

    @app.post("/api/work", status_code=202)
    async def work():
        tasks.append(asyncio.create_task(background()))
        return {"accepted": True}

    instrument(app, enabled)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://agent") as client:
            result = await client.post("/api/work")
        assert result.status_code == 202
        await asyncio.wait_for(started.wait(), 0.5)
        assert not finished.is_set()
        release.set()
        await asyncio.gather(*tasks)
        spans = enabled.spans()
    root = next(item for item in spans if item.parent is None)
    background_span = next(item for item in spans if item.name == "planner.run")
    assert background_span.parent.span_id == root.context.span_id
    assert background_span.context.trace_id == root.context.trace_id
    assert background_span.end_time >= root.end_time


@pytest.mark.asyncio
async def test_concurrent_apps_and_background_contexts_do_not_share_provider_or_parent(enabled):
    from opentelemetry.trace import get_tracer_provider
    from one_request_telemetry import internal_headers, traced
    global_provider = get_tracer_provider()
    first, second = FastAPI(), FastAPI()
    entered, release = [asyncio.Event(), asyncio.Event()], asyncio.Event()
    headers, tasks = {}, []

    @traced("planner.run")
    async def background(index):
        entered[index].set()
        await release.wait()
        headers[index] = internal_headers()

    def route(index):
        async def work():
            tasks.append(asyncio.create_task(background(index)))
            return {"accepted": True}
        return work

    first.add_api_route("/api/work", route(0), methods=["POST"], status_code=202)
    second.add_api_route("/api/work", route(1), methods=["POST"], status_code=202)
    instrument(first, enabled, "one-request-agent")
    instrument(second, enabled, "one-request-provider-gateway")
    async with first.router.lifespan_context(first), second.router.lifespan_context(second):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=first), base_url="http://first") as left:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=second), base_url="http://second") as right:
                responses = await asyncio.gather(left.post("/api/work"), right.post("/api/work"))
        assert all(response.status_code == 202 for response in responses)
        await asyncio.wait_for(asyncio.gather(*(event.wait() for event in entered)), 0.5)
        release.set()
        await asyncio.gather(*tasks)
        assert first.state.tracing.provider is not second.state.tracing.provider
        assert get_tracer_provider() is global_provider
        spans = enabled.spans()
    by_service = {}
    for item in spans:
        by_service.setdefault(item.resource.attributes["service.name"], []).append(item)
    assert set(by_service) == {"one-request-agent", "one-request-provider-gateway"}
    traces = []
    for service_spans in by_service.values():
        assert len(service_spans) == 2
        root = next(item for item in service_spans if item.parent is None)
        child = next(item for item in service_spans if item.parent is not None)
        assert child.parent.span_id == root.context.span_id
        assert child.context.trace_id == root.context.trace_id
        traces.append(root.context.trace_id)
    assert traces[0] != traces[1]
    assert headers[0]["traceparent"].split("-")[1] != headers[1]["traceparent"].split("-")[1]
    assert internal_headers() == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("private,parent,ratio", [
    (False, TRACEPARENT, "0"),
    (True, TRACEPARENT[:-2] + "00", "1"),
])
async def test_public_parent_cannot_force_sampling_and_private_unsampled_parent_is_preserved(enabled, monkeypatch,
                                                                                          private, parent, ratio):
    from one_request_telemetry import internal_headers
    monkeypatch.setenv("OTEL_TRACES_SAMPLER_ARG", ratio)
    app, forwarded = FastAPI(), []

    @app.get("/api/work")
    async def work():
        forwarded.append(internal_headers())
        return {"ok": True}

    instrument(app, enabled, private=private)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://agent") as client:
            assert (await client.get("/api/work", headers={"traceparent": parent})).json() == {"ok": True}
        assert enabled.spans() == ()
    assert forwarded[0]["traceparent"].endswith("-00")
    if private:
        assert forwarded[0]["traceparent"].split("-")[1] == ATTACKER_TRACE
    else:
        assert forwarded[0]["traceparent"].split("-")[1] != ATTACKER_TRACE


@pytest.mark.asyncio
async def test_sdk_startup_failure_preserves_application_startup_and_request(enabled, caplog):
    def unavailable_sdk(settings):
        raise RuntimeError(CANARY)

    from one_request_telemetry import instrument_app
    app = FastAPI()

    @app.get("/api/work")
    async def work():
        return {"ok": True}

    instrument_app(app, "one-request-agent", runtime_factory=unavailable_sdk)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://agent") as client:
            assert (await client.get("/api/work")).json() == {"ok": True}
        assert app.state.tracing is None
    assert CANARY not in caplog.text


@pytest.mark.asyncio
async def test_cancelled_queue_wait_preserves_capacity_and_cancellation(enabled):
    from one_request_telemetry import queued
    app, lock, started = FastAPI(), asyncio.Semaphore(1), asyncio.Event()
    await lock.acquire()

    @app.get("/api/work")
    async def work():
        started.set()
        async with queued(lock, "gateway.queue"):
            raise AssertionError("Cancelled waiter must never enter the protected operation")

    instrument(app, enabled)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://agent") as client:
            task = asyncio.create_task(client.get("/api/work"))
            await asyncio.wait_for(started.wait(), 0.5)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert lock.locked()
        lock.release()
        await asyncio.wait_for(lock.acquire(), 0.1)
        lock.release()
        spans = enabled.spans()
    assert len(spans) == 2
    assert any(item.name == "gateway.queue" for item in spans)
    assert any(any(key.endswith("cancelled") and value is True for key, value in item.attributes.items())
               for item in spans)
    assert all(not item.events and not item.status.description for item in spans)


@pytest.mark.asyncio
async def test_failing_exporter_cannot_change_api_or_log_secret_errors(enabled, caplog):
    class FailingExporter(RecordingExporter):
        def export(self, spans):
            raise RuntimeError(CANARY)

    recorders, app = Recorders(FailingExporter), FastAPI()

    @app.post("/api/work", status_code=202)
    async def work():
        return {"accepted": True}

    instrument(app, recorders)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://agent") as client:
            for _ in range(3):
                response = await asyncio.wait_for(client.post("/api/work"), 0.5)
                assert response.status_code == 202 and response.json() == {"accepted": True}
        # Force the failure while caplog is still active; a telemetry failure is
        # absorbed by the export boundary rather than re-raised into the caller.
        recorders.runtimes[0].force_flush(timeout_millis=3000)
    assert recorders.exporters[0].shutdown_calls == 1
    assert CANARY not in caplog.text


@pytest.mark.asyncio
async def test_stalled_export_is_off_request_path_and_buffer_is_bounded(enabled):
    exporting, release = threading.Event(), threading.Event()

    class SlowExporter(RecordingExporter):
        def export(self, spans):
            exporting.set()
            release.wait(5)
            return super().export(spans)

    from one_request_telemetry import span
    recorders, app = Recorders(SlowExporter), FastAPI()

    @app.post("/api/work")
    async def work():
        for _ in range(1000):
            with span("planner.run"):
                pass
        return {"ok": True}

    instrument(app, recorders)
    try:
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://agent") as client:
                first = await asyncio.wait_for(client.post("/api/work"), 1)
                assert first.json() == {"ok": True}
                assert await asyncio.to_thread(exporting.wait, 1)
                second = await asyncio.wait_for(client.post("/api/work"), 1)
                assert second.json() == {"ok": True}
                assert not release.is_set()
            release.set()
            spans = recorders.spans()
        # Both bursts created 2002 spans. With the receiver blocked, buffering
        # remains finite and excess telemetry is dropped instead of retained.
        assert 0 < len(spans) < 1000
    finally:
        release.set()


@pytest.mark.asyncio
async def test_startup_failure_closes_owned_exporter(enabled):
    @asynccontextmanager
    async def broken_startup(app):
        raise RuntimeError(CANARY)
        yield  # pragma: no cover -- asynccontextmanager requires an async generator

    app = instrument(FastAPI(lifespan=broken_startup), enabled)
    with pytest.raises(RuntimeError, match=CANARY):
        async with app.router.lifespan_context(app):
            pytest.fail("startup should fail")
    assert len(enabled.exporters) == 1
    assert enabled.exporters[0].shutdown_calls == 1


@pytest.mark.asyncio
async def test_receiver_shutdown_is_finite_even_when_exporter_never_finishes(enabled):
    shutdown_started, release = threading.Event(), threading.Event()

    class StuckShutdownExporter(RecordingExporter):
        def shutdown(self):
            shutdown_started.set()
            release.wait(10)
            super().shutdown()

    recorders = Recorders(StuckShutdownExporter)
    app = instrument(FastAPI(), recorders)
    started = time.monotonic()
    try:
        async with app.router.lifespan_context(app):
            pass
        elapsed = time.monotonic() - started
        assert shutdown_started.is_set()
        assert elapsed < 5.5
    finally:
        release.set()


@pytest.mark.asyncio
async def test_real_agent_gateway_adapter_propagates_only_on_private_hop(enabled, monkeypatch, tmp_path):
    import one_request_telemetry
    from backend.app.data_gateway import create_gateway_app
    from backend.app.geography import find_place
    from backend.app.main import create_app
    from backend.app.application.dependencies import AgentDependencies
    from backend.tests.factories import fixture_principal

    class FixturePort:
        async def resolve_place(self, name):
            assert name == CANARY
            return find_place("Ларнака")

        async def aclose(self):
            pass

    monkeypatch.setattr(one_request_telemetry, "create_runtime", enabled)
    gateway = create_gateway_app(database_path=str(tmp_path / "gateway.sqlite3"),
                                 port_factory=lambda client, cache: FixturePort())
    gateway_transport = httpx.ASGITransport(app=gateway)
    observed = []

    async def route(request):
        observed.append((request.url.host, dict(request.headers)))
        if request.url.host == "provider-gateway":
            return await gateway_transport.handle_async_request(request)
        assert request.url.host == "external.invalid"
        return httpx.Response(200, json={"ok": True})

    agent = create_app(database_path=str(tmp_path / "agent.sqlite3"), transport=httpx.MockTransport(route),
                       dependencies=AgentDependencies(identity_resolver=fixture_principal))

    @agent.get("/api/tracing-probe")
    async def probe():
        data = agent.state.travel_data
        place = await data.resolve_place(CANARY)
        # Reuse the same HTTPX client as the private adapter. An automatic HTTPX
        # instrumentor would erroneously propagate to this provider request.
        response = await data.client.get(f"https://external.invalid/private?token={CANARY}")
        assert response.status_code == 200
        return {"place": place.name}

    # The production SPA fallback deliberately catches unknown /api paths.
    # Insert this test-only probe ahead of that fallback.
    agent.router.routes.insert(0, agent.router.routes.pop())
    async with gateway.router.lifespan_context(gateway), agent.router.lifespan_context(agent):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=agent), base_url="http://agent") as client:
            result = await client.get("/api/tracing-probe", headers={
                "traceparent": TRACEPARENT, "baggage": f"token={CANARY}", "tracestate": f"vendor={CANARY}",
            })
        assert result.status_code == 200 and result.json()["place"]
        spans = enabled.spans()
    private_headers = next(headers for host, headers in observed if host == "provider-gateway")
    external_headers = next(headers for host, headers in observed if host == "external.invalid")
    assert "traceparent" in private_headers
    assert not {"tracestate", "baggage"} & private_headers.keys()
    assert not {"traceparent", "tracestate", "baggage"} & external_headers.keys()
    assert {item.resource.attributes["service.name"] for item in spans} == {"one-request-agent", "one-request-gateway"}
    assert len({item.context.trace_id for item in spans}) == 1
    assert next(item for item in spans if item.parent is None).context.trace_id != int(ATTACKER_TRACE, 16)
    assert CANARY not in exported_text(spans)


@pytest.mark.asyncio
async def test_real_agent_message_202_background_inherits_request_context(enabled, monkeypatch, tmp_path):
    import one_request_telemetry
    from one_request_telemetry import traced
    from backend.app.main import create_app
    from backend.app.application.dependencies import AgentDependencies
    from backend.tests.factories import fixture_principal

    monkeypatch.setattr(one_request_telemetry, "create_runtime", enabled)
    started, release = asyncio.Event(), asyncio.Event()
    app = create_app(database_path=str(tmp_path / "agent.sqlite3"), travel_data_factory=lambda client: object(),
                     dependencies=AgentDependencies(identity_resolver=fixture_principal))

    @traced("planner.run")
    async def plan(job_id):
        started.set()
        await release.wait()

    async with app.router.lifespan_context(app):
        app.state.planner.run = plan
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://agent") as client:
            session_id = (await client.post("/api/sessions")).json()["id"]
            response = await client.post(f"/api/sessions/{session_id}/messages", json={"content": CANARY, "expected_revision": 0}, headers={"Idempotency-Key": "tracing-message-0001"})
        assert response.status_code == 202
        await asyncio.wait_for(started.wait(), 0.5)
        release.set()
        deadline=asyncio.get_running_loop().time()+1
        while app.state.command_worker._running is not None:
            assert asyncio.get_running_loop().time()<deadline
            await asyncio.sleep(0.01)
        spans = enabled.spans()
    work = next(item for item in spans if item.name == "planner.run")
    request_span = next(item for item in spans if item.context.span_id == work.parent.span_id)
    assert request_span.name == "POST /api/sessions/{session_id}/messages"
    assert work.context.trace_id == request_span.context.trace_id
    assert work.end_time >= request_span.end_time
    assert CANARY not in exported_text(spans) and session_id not in exported_text(spans)


@pytest.mark.asyncio
async def test_actual_browser_client_worker_queue_and_recipe_form_one_trace(enabled, monkeypatch, tmp_path):
    import importlib
    from backend.app.geography import find_place
    from backend.app.infrastructure.provider_cache import ProviderCache
    from backend.app.models import TravelIntent
    from backend.app.pricing.providers import KeylessPricing, context_for
    worker = importlib.import_module("price_worker.app")
    recipe = importlib.import_module("price_worker.recipes.ostrovok")
    closed, requests = [], []

    class FixturePage:
        def set_default_timeout(self, value):
            pass

        def set_default_navigation_timeout(self, value):
            pass

    class FixtureContext:
        async def new_page(self):
            return FixturePage()

        async def close(self):
            closed.append(True)

    class FixtureBrowser:
        def is_connected(self):
            return True

        async def new_context(self, **kwargs):
            return FixtureContext()

    async def no_guards(context, provider):
        assert provider == "ostrovok"

    async def no_external_recipe(page, context):
        requests.append(context)
        return {"outcome": "no_offers", "diagnostic_code": "ostrovok_no_property_links"}

    @asynccontextmanager
    async def fixture_browser_lifespan(app):
        app.state.slots = asyncio.Semaphore(2)
        app.state.browser = FixtureBrowser()
        yield

    browser = FastAPI(lifespan=fixture_browser_lifespan)
    monkeypatch.setattr(worker, "app", browser)
    monkeypatch.setattr(worker, "install_guards", no_guards)
    monkeypatch.setattr(recipe, "ostrovok", no_external_recipe)
    browser.add_api_route("/v1/quotes", worker.quotes, methods=["POST"])
    instrument(browser, enabled, "one-request-price-worker", private=True)
    gateway = FastAPI()
    monkeypatch.setenv("PRICE_WORKER_URL", "http://price-worker:8090")
    cache = ProviderCache(str(tmp_path / "provider.sqlite3"))
    intent = TravelIntent(origin="Варшава", destination="Афины", date_from="2027-06-01", date_to="2027-06-08")
    destination, origin = find_place("Афины"), find_place("Варшава")

    @gateway.post("/v1/work")
    async def work():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=browser)) as client:
            pricing = KeylessPricing(client, cache)
            observation, diagnostics = await pricing._browser(context_for("stay", destination, origin, intent),
                                                               destination, provider="ostrovok")
        assert observation.value.kind == "unknown"
        return {"outcome": observation.outcome}

    instrument(gateway, enabled, "one-request-gateway", private=True)
    try:
        async with browser.router.lifespan_context(browser), gateway.router.lifespan_context(gateway):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=gateway), base_url="http://gateway") as client:
                response = await client.post("/v1/work")
            assert response.json() == {"outcome": "no_offers"}
            spans = enabled.spans()
        assert requests and closed == [True]
        assert len({item.context.trace_id for item in spans}) == 1
        worker_server = next(item for item in spans if item.name == "POST /v1/quotes")
        caller = next(item for item in spans if item.name == "pricing.worker.client")
        assert worker_server.parent.span_id == caller.context.span_id
        assert {"pricing.browser", "pricing.worker.queue", "pricing.worker.client", "price_worker.quotes",
                "price_worker.queue", "price_worker.recipe"} <= {item.name for item in spans}
        assert {item.resource.attributes["service.name"] for item in spans} == {"one-request-gateway", "one-request-price-worker"}
    finally:
        cache.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("generic_headers", [CANARY, f"Authorization=Bearer%20{CANARY},x-other-project={CANARY}"])
async def test_actual_otlp_export_ignores_ambient_credentials_and_transport_environment(monkeypatch, caplog,
                                                                                     otlp_receiver, generic_headers):
    from one_request_telemetry import instrument_app
    from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest

    endpoint, captured, _ = otlp_receiver
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", endpoint)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_HEADERS", f"uptrace-dsn=explicit-{CANARY}")
    # The stock exporter used to parse generic headers even when handed explicit
    # settings, leaking malformed secrets to stdout or inheriting another key.
    traps = {
        "OTEL_EXPORTER_OTLP_HEADERS": generic_headers,
        "OTEL_EXPORTER_OTLP_ENDPOINT": f"https://{CANARY}.invalid/v1/traces",
        "OTEL_EXPORTER_OTLP_COMPRESSION": CANARY,
        "OTEL_EXPORTER_OTLP_TRACES_COMPRESSION": CANARY,
        "OTEL_EXPORTER_OTLP_TIMEOUT": CANARY,
        "OTEL_EXPORTER_OTLP_TRACES_TIMEOUT": CANARY,
        "OTEL_PYTHON_EXPORTER_OTLP_HTTP_TRACES_CREDENTIAL_PROVIDER": CANARY,
        "OTEL_PYTHON_SDK_INTERNAL_METRICS_ENABLED": "true",
        "OTEL_TRACES_SAMPLER": "always_off",
        "HTTP_PROXY": f"http://{CANARY}.invalid:9",
        "HTTPS_PROXY": f"http://{CANARY}.invalid:9",
        "ALL_PROXY": f"http://{CANARY}.invalid:9",
        "NO_PROXY": "",
        "NETRC": str(ROOT / CANARY),
        "REQUESTS_CA_BUNDLE": str(ROOT / CANARY),
        "CURL_CA_BUNDLE": str(ROOT / CANARY),
    }
    for prefix in ("OTEL_EXPORTER_OTLP", "OTEL_EXPORTER_OTLP_TRACES"):
        for suffix in ("CERTIFICATE", "CLIENT_KEY", "CLIENT_CERTIFICATE"):
            traps[f"{prefix}_{suffix}"] = str(ROOT / CANARY)
    for name, value in traps.items():
        monkeypatch.setenv(name, value)
    before = {name: os.environ[name] for name in traps}
    app = FastAPI()

    @app.get("/api/work")
    async def work():
        return {"ok": True}

    instrument_app(app, "one-request-agent")
    async with app.router.lifespan_context(app):
        assert app.state.tracing is not None
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://agent",
                                     trust_env=False) as client:
            assert (await client.get("/api/work")).json() == {"ok": True}
        assert await asyncio.to_thread(app.state.tracing.force_flush, timeout_millis=3000)
    assert len(captured) == 1 and captured[0]["path"] == "/v1/traces"
    headers = {name.lower(): value for name, value in captured[0]["headers"].items()}
    assert headers["uptrace-dsn"] == f"explicit-{CANARY}"
    assert "authorization" not in headers and "x-other-project" not in headers
    assert headers["content-type"] == "application/x-protobuf" and headers["content-encoding"] == "gzip"
    request = ExportTraceServiceRequest.FromString(gzip.decompress(captured[0]["body"]))
    spans = [span for resource in request.resource_spans for scope in resource.scope_spans for span in scope.spans]
    assert [span.name for span in spans] == ["GET /api/work"]
    assert CANARY not in str(request) and CANARY not in caplog.text
    assert {name: os.environ[name] for name in traps} == before


@pytest.mark.asyncio
@pytest.mark.parametrize("status,expected_attempts", [(302, 1), (401, 1), (429, 2), (503, 2)])
async def test_actual_otlp_rejections_do_not_follow_redirects_or_disclose_receiver_content(monkeypatch, caplog,
                                                                                        otlp_receiver,
                                                                                        status, expected_attempts):
    from one_request_telemetry import create_runtime, instrument_app
    endpoint, captured, configuration = otlp_receiver
    configuration.update(status=status, body=CANARY.encode(), headers={
        "Location": endpoint + "?token=" + CANARY,
        "X-Receiver-Secret": CANARY,
        "Retry-After": "0",
    })
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", endpoint)
    successes = []
    app = FastAPI()

    @app.get("/api/work")
    async def work():
        return {"ok": True}

    instrument_app(app, "one-request-agent", runtime_factory=lambda settings: create_runtime(settings,
                                                                                           on_export=successes.extend))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://agent") as client:
            assert (await client.get("/api/work")).json() == {"ok": True}
        assert await asyncio.to_thread(app.state.tracing.force_flush, timeout_millis=3000)
    assert len(captured) == expected_attempts
    assert all(request["path"] == "/v1/traces" for request in captured)
    assert not successes
    assert CANARY not in caplog.text


@pytest.mark.asyncio
async def test_mounted_child_excludes_probes_static_and_session_streams_using_child_path(enabled):
    child = FastAPI()

    async def response():
        return {"ok": True}

    excluded = ["/health", "/ready", "/metrics", "/assets/app.js", "/api/sessions/private-session/events"]
    for path in excluded + ["/api/work"]:
        child.add_api_route(path, response)
    instrument(child, enabled)

    @asynccontextmanager
    async def lifespan(host):
        async with child.router.lifespan_context(child):
            yield

    host = FastAPI(lifespan=lifespan)
    host.mount("/travel", child)
    async with host.router.lifespan_context(host):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=host), base_url="http://host") as client:
            for path in excluded + ["/api/work"]:
                assert (await client.get("/travel" + path)).status_code == 200
        spans = enabled.spans()
    assert [span.name for span in spans] == ["GET /api/work"]


@pytest.mark.asyncio
async def test_actual_otlp_acknowledgement_slow_drip_cannot_extend_whole_export_budget(monkeypatch, otlp_receiver):
    from one_request_telemetry import create_runtime, instrument_app
    endpoint, captured, configuration = otlp_receiver
    # Each socket read arrives before the per-read timeout, while the complete
    # response needs four seconds. Waiting for a whole 4 KiB chunk would defeat
    # the documented two-second export budget.
    configuration.update(body=b"\0" * 32, drip_seconds=0.125)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", endpoint)
    successes = []
    app = FastAPI()

    @app.get("/api/work")
    async def work():
        return {"ok": True}

    instrument_app(app, "one-request-agent", runtime_factory=lambda settings: create_runtime(settings,
                                                                                           on_export=successes.extend))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://agent") as client:
            assert (await client.get("/api/work")).json() == {"ok": True}
        started = time.monotonic()
        assert await asyncio.to_thread(app.state.tracing.force_flush, timeout_millis=3000)
        assert time.monotonic() - started < 3.2
    assert len(captured) == 1
    assert not successes


@pytest.mark.asyncio
@pytest.mark.parametrize("acknowledgement", ["accepted", "partial_rejection", "invalid", "oversized"])
async def test_actual_otlp_success_observer_requires_bounded_valid_acceptance(monkeypatch, caplog, otlp_receiver,
                                                                           acknowledgement):
    from one_request_telemetry import create_runtime, instrument_app
    from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceResponse
    endpoint, captured, configuration = otlp_receiver
    if acknowledgement == "accepted":
        configuration["body"] = b""
    elif acknowledgement == "partial_rejection":
        response = ExportTraceServiceResponse()
        response.partial_success.rejected_spans = 1
        response.partial_success.error_message = CANARY
        configuration["body"] = response.SerializeToString()
    elif acknowledgement == "invalid":
        configuration["body"] = b"\x80"
    else:
        configuration["body"] = CANARY.encode() * 128
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", endpoint)
    successes = []
    app = FastAPI()

    @app.get("/api/work")
    async def work():
        return {"ok": True}

    instrument_app(app, "one-request-agent", runtime_factory=lambda settings: create_runtime(settings,
                                                                                           on_export=successes.extend))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://agent") as client:
            assert (await client.get("/api/work")).json() == {"ok": True}
        assert await asyncio.to_thread(app.state.tracing.force_flush, timeout_millis=3000)
    assert len(captured) == 1
    assert [span.name for span in successes] == (["GET /api/work"] if acknowledgement == "accepted" else [])
    assert CANARY not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("provider,reason", [
    ("aegean_public", "no_matching_date_fare"), ("ryanair_farefinder", "http_rate_limited"),
    ("searxng", "negative_cache_hit"), ("ollama_web_search", "authentication_failed"),
    ("ostrovok", "invalid_discovery_terms"), ("google_hotels", "calendar_year_or_selection_mismatch"),
])
async def test_source_provider_reason_allowlists_apply_at_recording_and_export(enabled, provider, reason):
    from one_request_telemetry import span
    app = FastAPI()

    @app.get("/api/work")
    async def work(request: Request):
        with span("airline.lookup", attributes={"provider": provider, "reason": reason}):
            pass
        # The final boundary must independently reject payload-shaped or merely
        # identifier-shaped values even when the safe facade is bypassed.
        with request.app.state.tracing.tracer.start_as_current_span("search.http") as raw:
            raw.set_attribute("one_request.provider", CANARY)
            raw.set_attribute("one_request.reason", "sensitive_traveler_prompt")
            raw.set_attribute("raw.query", CANARY)
        return {"ok": True}

    instrument(app, enabled)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://agent") as client:
            assert (await client.get("/api/work")).status_code == 200
        spans = enabled.spans()
    lookup = next(item for item in spans if item.name == "airline.lookup")
    assert lookup.attributes["one_request.provider"] == provider
    assert lookup.attributes["one_request.reason"] == reason
    raw = next(item for item in spans if item.name == "search.http")
    assert "one_request.provider" not in raw.attributes and "one_request.reason" not in raw.attributes
    assert CANARY not in exported_text(spans) and "sensitive_traveler_prompt" not in exported_text(spans)


@pytest.mark.asyncio
async def test_airline_negative_cache_has_no_fictitious_dispatch_leaf_or_external_trace_headers(enabled, tmp_path):
    from backend.app.pricing.airlines import AEGEAN, RYANAIR, PublicAirlinePrices
    from backend.app.storage import Store
    calls = []
    empty = {"props": {"pageProps": {"apolloState": {"data": {
        "fare-daily-histogram": {"cmpConfig": {"settings": {"initialState": {"dailyHistogram": []}}}}
    }}}}}

    def respond(request):
        calls.append(request)
        assert all(key not in request.headers for key in ("traceparent", "tracestate", "baggage"))
        if request.url.host == "www.ryanair.com":
            return httpx.Response(200, json={"fares": [], "private": CANARY})
        return httpx.Response(200, text='<script id="__NEXT_DATA__">' + json.dumps(empty) + '</script>')

    store, app = Store(str(tmp_path / "airline-tracing.db")), FastAPI()
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as provider_client:
            adapter = PublicAirlinePrices(provider_client, store)

            @app.get("/api/work")
            async def work():
                return await adapter.search("LCA", "ATH", "2026-10-30", "2026-11-01", "EUR")

            instrument(app, enabled)
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://agent") as client:
                    fresh = (await client.get("/api/work")).json()
                    first = enabled.spans()
                    cached = (await client.get("/api/work")).json()
                    all_spans = enabled.spans()
        dispatched = [item for item in first if item.name == "airline.http"]
        assert len(calls) == len(dispatched) == store.metrics()["public_airline_requests"] == 3
        assert [item.attributes["one_request.provider"] for item in dispatched].count(AEGEAN) == 2
        assert [item.attributes["one_request.provider"] for item in dispatched].count(RYANAIR) == 1
        assert all(item.attributes["http.response.status_code"] == 200 for item in dispatched)
        later = [item for item in all_spans if item.context.trace_id != first[0].context.trace_id]
        assert not any(item.name == "airline.http" for item in later)
        cache, = [item for item in later if item.name == "airline.cache"]
        assert cache.attributes["one_request.reason"] == "negative_cache_hit"
        assert all(row["delivery"] == "live_fetch" for row in fresh["diagnostics"])
        assert all(row["delivery"] == "cache_reuse" for row in cached["diagnostics"])
        assert [row["retrieved_at"] for row in fresh["diagnostics"]] == [row["retrieved_at"] for row in cached["diagnostics"]]
        assert CANARY not in exported_text(all_spans)
    finally:
        store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["searxng", "ollama"])
async def test_search_dispatch_cache_and_privacy_match_actual_requests(enabled, tmp_path, provider):
    from backend.app.search.adapters import SearchAdapter
    from backend.app.storage import Store
    requests, store, app = [], Store(str(tmp_path / "search-tracing.db")), FastAPI()

    def respond(request):
        requests.append(request)
        assert all(key not in request.headers for key in ("traceparent", "tracestate", "baggage"))
        return httpx.Response(200, json={"results": []})

    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as provider_client:
            adapter = SearchAdapter(provider, provider_client, store, asyncio.Semaphore(1),
                                    endpoint="https://example.invalid/" + CANARY, key=CANARY)

            @app.get("/api/work")
            async def work():
                value = await adapter.search(CANARY)
                return {"status": value.status}

            instrument(app, enabled)
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://agent") as client:
                    assert (await client.get("/api/work")).json()["status"] == "live"
                    first = enabled.spans()
                    assert (await client.get("/api/work")).json()["status"] == "cached"
                    spans = enabled.spans()
        dispatch, = [item for item in spans if item.name == "search.http"]
        assert len(requests) == store.metrics()["search_" + provider + "_requests"] == 1
        assert dispatch.attributes["one_request.provider"] == ("ollama_web_search" if provider == "ollama" else "searxng")
        assert dispatch.attributes["one_request.reason"] == "no_results"
        assert dispatch.attributes["http.response.status_code"] == 200
        later = [item for item in spans if item.context.trace_id != first[0].context.trace_id]
        assert not any(item.name == "search.http" for item in later)
        assert any(item.attributes.get("one_request.reason") == "negative_cache_hit" for item in later)
        assert CANARY not in exported_text(spans)
    finally:
        store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["disabled", "enabled", "receiver_failed"])
async def test_search_results_and_dispatch_budget_are_equivalent_with_tracing_off_or_failed(enabled, monkeypatch,
                                                                                        tmp_path, caplog, mode):
    from backend.app.search.adapters import SearchAdapter
    from backend.app.storage import Store
    if mode == "disabled":
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "")

    class FailedExporter(RecordingExporter):
        def export(self, spans):
            raise RuntimeError(CANARY)

    recorders = Recorders(FailedExporter) if mode == "receiver_failed" else enabled
    requests, store, app = [], Store(str(tmp_path / "search-modes.db")), FastAPI()

    def respond(request):
        requests.append(request)
        assert "traceparent" not in request.headers and "baggage" not in request.headers
        return httpx.Response(403, text=CANARY)

    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as provider_client:
            adapter = SearchAdapter("ollama", provider_client, store, asyncio.Semaphore(1),
                                    endpoint="https://example.invalid/" + CANARY, key=CANARY)

            @app.get("/api/work")
            async def work():
                outcome = await adapter.search(CANARY)
                return {"status": outcome.status, "reason": outcome.reason, "count": len(outcome.hits)}

            instrument(app, recorders)
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://agent") as client:
                    for _ in range(2):
                        assert (await client.get("/api/work")).json() == {
                            "status": "unavailable", "reason": "authentication_failed", "count": 0}
                for runtime in recorders.runtimes:
                    runtime.force_flush()
        assert len(requests) == store.metrics()["search_ollama_requests"] == store.metrics()["search_ollama_errors"] == 2
        assert CANARY not in caplog.text
        if mode == "disabled":
            assert not recorders.runtimes and not app.user_middleware
    finally:
        store.close()


@pytest.mark.asyncio
async def test_flight_spans_keep_finite_outcomes_and_discard_travel_content(enabled):
    from one_request_telemetry import span
    app = FastAPI()

    @app.get("/work")
    async def work():
        with span("flight.search", attributes={"provider": "google_flights", "outcome": "reference",
                  "reason": "flight_reference", "request_id": CANARY, "source_url": CANARY,
                  "itinerary_id": CANARY, "prompt": CANARY}):
            with span("flight.source", attributes={"reason": CANARY, "provider": CANARY}):
                pass
        return {"ok": True}

    instrument(app, enabled, name="one-request-flight-search", private=True)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://private") as client:
            assert (await client.get("/work")).status_code == 200
        spans = enabled.spans()
    recorded, = [value for value in spans if value.name == "flight.search"]
    assert recorded.attributes == {"one_request.provider": "google_flights",
                                   "one_request.outcome": "reference", "one_request.reason": "flight_reference"}
    source, = [value for value in spans if value.name == "flight.source"]
    assert not source.attributes
    assert CANARY not in exported_text(spans)
