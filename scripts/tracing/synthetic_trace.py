"""Export a model-free, three-service synthetic trace using the real ASGI apps.

Run this in a fresh process with the project's virtual environment. The only
network traffic is the configured OTLP exporter. Private application HTTP hops
use ASGITransport; the actual browser endpoint deliberately has no browser.
Receiver readback is a separate verification step, never inferred from an ACK.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import AsyncExitStack, asynccontextmanager, contextmanager
from datetime import datetime, timedelta, timezone
import importlib
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import threading


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


class ValidationFailure(Exception):
    """A fixed, non-sensitive diagnostic suitable for the CLI."""


@contextmanager
def _environment(values):
    previous = {key: os.environ.get(key) for key in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


class ExportEvidence:
    """Record only privacy-filtered spans accepted by the real OTLP delegate."""

    def __init__(self):
        self._lock = threading.Lock()
        self._spans = {}

    def accepted(self, spans):
        records = []
        for value in spans:
            context, parent = value.context, value.parent
            attributes = value.attributes or {}
            record = {
                "service": value.resource.attributes.get("service.name"),
                "name": value.name,
                "trace_id": f"{context.trace_id:032x}",
                "span_id": f"{context.span_id:016x}",
                "parent_span_id": f"{parent.span_id:016x}" if parent is not None else None,
                "start_time_unix_ns": value.start_time,
                "duration_ms": round((value.end_time - value.start_time) / 1_000_000, 3),
                "status": value.status.status_code.name,
            }
            # Exported records contain no original request or exception data.
            for source, target in (("one_request.operation", "operation"),
                                   ("one_request.outcome", "outcome"),
                                   ("http.response.status_code", "http_status_code")):
                if source in attributes:
                    record[target] = attributes[source]
            records.append(record)
        with self._lock:
            for record in records:
                self._spans[(record["trace_id"], record["span_id"])] = record

    def snapshot(self):
        with self._lock:
            # Keep exporter order for timestamp ties (short waits can share the
            # platform clock tick); random span IDs do not describe chronology.
            return sorted(self._spans.values(), key=lambda item: item["start_time_unix_ns"])


def _tree(spans):
    children = {}
    for value in spans:
        children.setdefault(value["parent_span_id"], []).append(value)
    lines = []

    def visit(value, prefix, last, root=False):
        branch = "" if root else "`-- " if last else "+-- "
        suffix = f" [{value['outcome']}]" if "outcome" in value else ""
        lines.append(f"{prefix}{branch}{value['service']}: {value['name']} ({value['duration_ms']:.3f} ms){suffix}")
        descendants = children.get(value["span_id"], [])
        next_prefix = prefix if root else prefix + ("    " if last else "|   ")
        for index, child in enumerate(descendants):
            visit(child, next_prefix, index == len(descendants) - 1)

    for root in children.get(None, []):
        visit(root, "", True, root=True)
    return "\n".join(lines)


async def validate():
    """Run isolated application fixtures and return ACK-based export evidence."""
    import httpx
    from dotenv import load_dotenv

    load_dotenv(PROJECT_ROOT / ".env")
    if not os.getenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "").strip():
        raise ValidationFailure("A nonempty OTEL_EXPORTER_OTLP_TRACES_ENDPOINT is required.")

    # These overrides affect this synthetic CLI process only. ASGI transports
    # reject every unrecognised host/path even if an adapter regresses.
    overrides = {
        "LLM_ENABLED": "0", "LLM_API_KEY": "", "OLLAMA_API_KEY": "", "SERPAPI_API_KEY": "",
        "WEB_FALLBACK_ENABLED": "0", "LOCAL_CATALOG_ENABLED": "1", "KEYLESS_PRICING_ENABLED": "0",
        "EMBEDDING_URL": "", "PRICE_WORKER_URL": "http://price-worker:8092",
        "PROVIDER_GATEWAY_URL": "http://provider-gateway:8091", "OTEL_TRACES_SAMPLER_ARG": "1",
    }
    evidence = ExportEvidence()
    calls = {"gateway": 0, "worker": 0, "rejected": 0}
    observations = {"browser_outcome": None, "catalog_configured": False}

    class PrivateBoundaryTransport(httpx.AsyncBaseTransport):
        def __init__(self, app, host, path, boundary):
            self.inner = httpx.ASGITransport(app=app)
            self.host, self.path, self.boundary = host, path, boundary

        async def handle_async_request(self, request):
            if request.url.host != self.host or request.url.path != self.path:
                calls["rejected"] += 1
                raise httpx.ConnectError("synthetic transport denied an unexpected boundary")
            calls[self.boundary] += 1
            return await self.inner.handle_async_request(request)

        async def aclose(self):
            await self.inner.aclose()

    @asynccontextmanager
    async def no_browser_lifespan(app):
        # The production route/decorators remain intact; Playwright is not run.
        app.state.browser = None
        app.state.slots = asyncio.Semaphore(2)
        yield

    with _environment(overrides), TemporaryDirectory(prefix="one-request-tracing-") as directory:
        # Construct uninstrumented fixtures, then attach the public runtime
        # factory seam. No production module, exporter or session is patched.
        with _environment({"OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": ""}):
            from backend.app.data_gateway import create_gateway_app
            from backend.app.geography import find_place
            from backend.app.main import create_app
            from backend.app.models import TravelIntent
            from backend.app.pricing.providers import context_for
            from backend.app.providers import LocalTravelDataPort, Providers
            from one_request_telemetry import create_runtime, instrument_app

            worker_module = importlib.import_module("price_worker.app")
            worker = worker_module.app
            if getattr(worker.state, "_one_request_tracing", False):
                raise ValidationFailure("Run synthetic trace validation in a fresh process.")
            worker.router.lifespan_context = no_browser_lifespan
            worker_transport = PrivateBoundaryTransport(worker, "price-worker", "/v1/quotes", "worker")

            class BrowserBoundaryPort:
                """Synthetic-only capability probe through the real browser adapter."""

                def __init__(self, client, cache):
                    self.providers = Providers(client, cache)
                    self.delegate = LocalTravelDataPort(self.providers)

                async def capabilities(self):
                    result = await self.delegate.capabilities()
                    observations["catalog_configured"] = bool(result["providers"]["catalog"]["configured"])
                    place, origin = find_place("Larnaca"), find_place("Warsaw")
                    if place is None or origin is None:
                        raise ValidationFailure("Synthetic geography fixture is unavailable.")
                    start = datetime.now(timezone.utc).date() + timedelta(days=30)
                    intent = TravelIntent(origin=origin.name, destination=place.name, date_from=start,
                        date_to=start + timedelta(days=3), adults=2, currency="EUR")
                    quote, _ = await self.providers.pricing._browser(context_for("stay", place, origin, intent),
                        place, provider="google_hotels")
                    observations["browser_outcome"] = quote.outcome
                    return result

                async def aclose(self):
                    # The actual gateway lifespan owns the cache/client.
                    pass

            gateway = create_gateway_app(database_path=str(Path(directory) / "gateway.sqlite3"),
                transport=worker_transport, port_factory=BrowserBoundaryPort)
            gateway_transport = PrivateBoundaryTransport(gateway, "provider-gateway", "/v1/capabilities", "gateway")
            agent = create_app(database_path=str(Path(directory) / "sessions.sqlite3"), transport=gateway_transport)

        def runtime_factory(settings):
            # Keep the production privacy exporter and hardened OTLP session.
            return create_runtime(settings, on_export=evidence.accepted)

        for app, name, trust in ((worker, "one-request-price-worker", True),
                                 (gateway, "one-request-gateway", True),
                                 (agent, "one-request-agent", False)):
            instrument_app(app, name, trust_incoming=trust, runtime_factory=runtime_factory)

        async with AsyncExitStack() as stack:
            for app in (worker, gateway, agent):
                await stack.enter_async_context(app.router.lifespan_context(app))
                if getattr(app.state, "tracing", None) is None:
                    raise ValidationFailure("The tracing runtime did not initialize.")
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=agent), base_url="http://synthetic-agent") as client:
                async with asyncio.timeout(15):
                    response = await client.get("/api/config")
                if response.status_code != 200:
                    raise ValidationFailure("The synthetic application request failed.")
                if observations["browser_outcome"] != "not_configured":
                    raise ValidationFailure("The no-browser boundary did not return its expected outcome.")
                if calls != {"gateway": 1, "worker": 1, "rejected": 0}:
                    raise ValidationFailure("The synthetic request used an unexpected application boundary.")
        # Lifespans close in reverse order and bounded exporters finish before
        # accepting this mirror as evidence. Failed batches never enter it.

    spans = evidence.snapshot()
    trace_ids = {value["trace_id"] for value in spans}
    services = {value["service"] for value in spans}
    expected_services = {"one-request-agent", "one-request-gateway", "one-request-price-worker"}
    if len(trace_ids) != 1 or services != expected_services:
        raise ValidationFailure("A complete three-service trace was not accepted by the exporter.")
    roots = [value for value in spans if value["parent_span_id"] is None]
    identities = {value["span_id"] for value in spans}
    if len(roots) != 1 or any(value["parent_span_id"] not in identities for value in spans if value["parent_span_id"] is not None):
        raise ValidationFailure("The exported synthetic trace has incomplete parentage.")
    expected_names = {"GET /api/config", "travel_data.client", "GET /v1/capabilities", "gateway.execute",
                      "pricing.worker.client", "POST /v1/quotes", "price_worker.quotes"}
    if not expected_names.issubset({value["name"] for value in spans}):
        raise ValidationFailure("The exported synthetic trace is missing a required operation.")
    return {
        "scenario": "synthetic_asgi_three_service_config_no_browser",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "trace_id": next(iter(trace_ids)), "span_count": len(spans), "services": sorted(services),
        "export_acknowledged": True, "receiver_readback": "not_performed",
        "application_transport": "in_process_asgi", "llm_dispatches": 0,
        "external_travel_requests": 0, "browser_launched": False,
        "browser_recipe_executed": False, "observations": observations, "calls": calls,
        "tree": _tree(spans), "spans": spans,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="Print safe structured evidence instead of the tree.")
    parser.add_argument("--output", type=Path, help="Also save safe evidence as UTF-8 JSON at this path.")
    args = parser.parse_args()
    try:
        result = asyncio.run(validate())
        encoded = json.dumps(result, ensure_ascii=True, indent=2) + "\n"
        if args.output is not None:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(encoded, encoding="utf-8")
        if args.json:
            print(encoded, end="")
        else:
            print("Synthetic ASGI integration; three real application boundaries; no model/browser/provider calls.")
            print("OTLP export acknowledged; receiver readback remains a separate check.")
            print(f"trace_id={result['trace_id']} spans={result['span_count']}")
            print(result["tree"])
        return 0
    except ValidationFailure as error:
        print(str(error), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Synthetic trace validation interrupted.", file=sys.stderr)
        return 130
    except Exception:
        # No exception text, payload, endpoint, header or traceback is emitted.
        print("Synthetic trace validation failed; no diagnostic credentials were printed.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
