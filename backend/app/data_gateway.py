"""Private provider service. This composition root owns no sessions or inference."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import os
from typing import Callable

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response
import httpx
from one_request_telemetry import instrument_app, queued, set_attributes, traced

from .domain.travel_data import (DEADLINES, EmptyInput, GatewayRequest, GatewayResponse, INPUTS,
    InventoryCandidatesData, MAX_REQUEST_BYTES, MAX_RESPONSE_BYTES, OUTPUTS, PATHS, PlaceData, ResolveData, SearchData,
    SourceCacheProtocol, TravelDataPort, fingerprint)
from .infrastructure.provider_cache import ProviderCache
from .providers import LocalTravelDataPort, Providers


def create_gateway_app(*, database_path: str | None = None,
                       transport: httpx.AsyncBaseTransport | None = None,
                       port_factory: Callable[[httpx.AsyncClient, SourceCacheProtocol], TravelDataPort] | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app):
        cache = ProviderCache(database_path or os.getenv("PROVIDER_CACHE_PATH", "provider-data/provider-cache.sqlite3"))
        client, providers, port = None, None, None
        try:
            client = httpx.AsyncClient(transport=transport, follow_redirects=False,
                headers={"User-Agent": "OneRequest-DataGateway/1 (bounded travel research; no booking)"},
                limits=httpx.Limits(max_connections=12))
            providers = Providers(client, cache) if port_factory is None else None
            port = LocalTravelDataPort(providers) if port_factory is None else port_factory(client, cache)
            app.state.cache, app.state.providers, app.state.port = cache, providers, port
            app.state.slots, app.state.pending = asyncio.Semaphore(8), 0
            yield
        finally:
            try:
                # A cancelled request must not strand bounded pricing singleflight tasks.
                pending = tuple(providers.pricing._inflight.values()) if providers is not None else ()
                for task in pending:
                    task.cancel()
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)
                if port_factory is not None and port is not None and hasattr(port, "aclose"):
                    await port.aclose()
            finally:
                # Construction or custom shutdown errors must not skip owned
                # resources, including errors occurring before the first yield.
                try:
                    if client is not None:
                        await client.aclose()
                finally:
                    cache.close()

    app = FastAPI(title="One Request provider gateway", version="1", lifespan=lifespan)

    @app.get("/health")
    async def health():
        with app.state.cache.lock:
            app.state.cache.db.execute("SELECT 1").fetchone()
        return {"status": "ok", "service": "provider-gateway", "contract_version": "travel-data.v1", "storage": "provider-cache"}

    @app.get("/ready")
    async def ready():
        await health()
        try:
            async with asyncio.timeout(2):
                capabilities = await app.state.port.capabilities()
            catalog = capabilities.get("providers", {}).get("catalog", {})
            return {"status": "ready", "catalog": catalog.get("status", "not_configured")}
        except Exception:
            return JSONResponse({"status": "unavailable"}, status_code=503)

    @app.get("/metrics")
    async def metrics():
        return app.state.cache.metrics()

    @traced("gateway.execute")
    async def execute(operation, payload):
        set_attributes(operation="other" if operation == "price_documents" else operation)
        port, data = app.state.port, payload.data
        if operation == "capabilities":
            return await port.capabilities()
        if operation == "resolve_place":
            place = await port.resolve_place(data.name)
            return ResolveData(place=PlaceData.from_place(place) if place is not None else None)
        place, intent = data.place.to_place(), data.intent.to_intent()
        if operation in ("inventory", "inventory_candidates", "refresh_inventory", "price_documents"):
            origin = data.origin.to_place()
            if operation == "inventory_candidates":
                return InventoryCandidatesData(candidates=await port.inventory_candidates(place, origin, intent, limit=data.limit))
            if operation in ("refresh_inventory", "price_documents"):
                return await getattr(port, operation)(place, origin, intent, data.current)
            return await port.inventory(place, origin, intent)
        result = await getattr(port, operation)(place, intent)
        return SearchData(evidence=result) if operation == "search" else result

    @traced("gateway.run")
    async def run(request: Request, operation: str):
        set_attributes(operation="other" if operation == "price_documents" else operation)
        if app.state.pending >= 32:
            set_attributes(outcome="overloaded")
            raise HTTPException(503, "gateway_overloaded")
        app.state.pending += 1
        payload = None
        try:
            async with asyncio.timeout(DEADLINES[operation]):
                try:
                    if request.query_params:
                        raise HTTPException(422, "query_commands_not_supported")
                    if int(request.headers.get("content-length", "0")) > MAX_REQUEST_BYTES:
                        raise HTTPException(413, "request_too_large")
                    if operation == "capabilities":
                        async for chunk in request.stream():
                            if chunk:
                                raise HTTPException(413 if len(chunk) > MAX_REQUEST_BYTES else 422, "capabilities_body_not_supported")
                        payload = GatewayRequest[EmptyInput](operation=operation, request_id=request.headers.get("X-Request-ID", ""),
                            version=request.headers.get("X-Travel-Version", ""), fingerprint=request.headers.get("X-Request-Fingerprint", ""),
                            deadline_at=request.headers.get("X-Deadline-At", ""), data=EmptyInput())
                    else:
                        if request.headers.get("content-type", "").split(";", 1)[0].lower() != "application/json":
                            raise HTTPException(415, "json_required")
                        body = bytearray()
                        async for chunk in request.stream():
                            body.extend(chunk)
                            if len(body) > MAX_REQUEST_BYTES:
                                raise HTTPException(413, "request_too_large")
                        payload = GatewayRequest[INPUTS[operation]].model_validate_json(body)
                    if payload.operation != operation or payload.fingerprint != fingerprint(operation, payload.data):
                        raise ValueError("request_identity_mismatch")
                except (ValueError, TypeError):
                    raise HTTPException(422, "invalid_gateway_request") from None
                remaining = (payload.deadline_at - datetime.now(timezone.utc)).total_seconds()
                if remaining <= 0:
                    raise TimeoutError
                async with asyncio.timeout(min(remaining, DEADLINES[operation])):
                    async with queued(app.state.slots, "gateway.queue"):
                        data = await execute(operation, payload)
                        response = GatewayResponse[OUTPUTS[operation]](operation=operation, request_id=payload.request_id,
                            fingerprint=payload.fingerprint, status="ok", data=data)
                        body = response.model_dump_json().encode()
                        if len(body) > MAX_RESPONSE_BYTES:
                            raise ValueError("response_too_large")
                        app.state.cache.increment("gateway_" + operation + "_completed")
                        set_attributes(outcome="ok")
                        return Response(body, media_type="application/json")
        except HTTPException:
            raise
        except TimeoutError:
            set_attributes(outcome="timeout")
            app.state.cache.increment("gateway_" + operation + "_timeouts")
            raise HTTPException(504, "gateway_deadline_exceeded") from None
        except Exception:
            set_attributes(outcome="unavailable")
            app.state.cache.increment("gateway_" + operation + "_errors")
            raise HTTPException(503, "gateway_operation_unavailable") from None
        finally:
            app.state.pending -= 1

    def endpoint(operation):
        async def handle(request: Request):
            return await run(request, operation)
        return handle

    for operation, path in PATHS.items():
        schema = GatewayRequest[INPUTS[operation]].model_json_schema()
        app.add_api_route(path, endpoint(operation), methods=["GET" if operation == "capabilities" else "POST"],
            name=operation, openapi_extra={"requestBody": {"required": True, "content": {"application/json": {"schema": schema}}}}
            if operation != "capabilities" else None)
    instrument_app(app, "one-request-gateway", trust_incoming=True)
    return app


app = create_gateway_app()
