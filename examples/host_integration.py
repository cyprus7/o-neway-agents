"""Mount the backend module in a host FastAPI product with explicit lifespan.

Run: python -m uvicorn examples.host_integration:app --port 8082
Use /travel/api/sessions and /travel/health. Host frontends use this API prefix;
the standalone demo frontend continues to be served by the Compose agent at /.
"""
from contextlib import asynccontextmanager
from fastapi import FastAPI

from backend.app.main import create_app


def create_host(*, database_path=None, travel_data_factory=None, dependencies=None) -> FastAPI:
    travel_agent = create_app(database_path=database_path, travel_data_factory=travel_data_factory,
                              dependencies=dependencies)

    @asynccontextmanager
    async def lifespan(host):
        # FastAPI does not automatically run mounted subapplication lifespans.
        async with travel_agent.router.lifespan_context(travel_agent):
            yield

    host = FastAPI(title="Existing product integration example", lifespan=lifespan)
    host.state.travel_agent = travel_agent
    host.mount("/travel", travel_agent)

    @host.get("/health")
    async def host_health():
        return {"status": "ok", "service": "host-product"}

    return host


app = create_host()
