"""Local One Request API and production frontend host."""
import asyncio
import os
from contextlib import asynccontextmanager, AsyncExitStack
from pathlib import Path
from collections.abc import Callable

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from one_request_telemetry import instrument_app

from .llm import LLMClient
from .models import MessageInput, OptionInput, PriceRefreshInput, Session
from .checkout import CheckoutInput, QuoteInput
from .planner import Planner
from .domain.travel_data import TravelDataPort
from .infrastructure.http_travel_data import HttpTravelDataPort
from .infrastructure.knowledge_travel_data import KnowledgeTravelDataPort
from .storage import Store
from .application.trips import TripApplication
from .application.dependencies import AgentDependencies
from .application.errors import ApplicationError
from .application.commands import CommandError
from .infrastructure.command_worker import CommandWorker
from .application.session_views import SessionViews, public_session_data
from .infrastructure.session_notifications import SessionNotifications
from .transport.session_events import SessionEventResponse, session_events
from .transport.auth import install_auth, access_still_valid
from .transport.telegram_auth import install_telegram_auth
from .transport.memory import install_memory
from .transport.telegram import TelegramService, install_telegram
from .transport.aftercare import install_aftercare, install_reminders
from .infrastructure.reminders import ReminderService


PROJECT_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(PROJECT_ROOT / ".env")


class CreateSessionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    preference_session_id: str | None = Field(default=None, max_length=80)
    apply_memory: bool = False


def public_session(session: Session, status_code: int = 200) -> JSONResponse:
    return JSONResponse(public_session_data(session), status_code=status_code)


def create_app(*, database_path: str | None = None, transport: httpx.AsyncBaseTransport | None = None,
               travel_data_factory: Callable[[httpx.AsyncClient], TravelDataPort] | None = None,
               dependencies: AgentDependencies | None = None) -> FastAPI:
    dependencies = dependencies or AgentDependencies()
    if travel_data_factory and dependencies.travel_data_factory:
        raise ValueError('Choose one explicit travel data factory')
    active = False
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        nonlocal active
        if active:
            raise RuntimeError("Agent runtime already started; mount one application instance")
        active = True
        try:
            async with AsyncExitStack() as resources:
                notifications = SessionNotifications()
                resources.callback(notifications.close)
                path = database_path or os.getenv("DATABASE_PATH", str(Path(os.getenv("DATA_DIR", "data")) / "one-request.sqlite3"))
                store = (dependencies.repository_factory or Store)(path, on_change=notifications.notify)
                resources.callback(store.close)
                client = await resources.enter_async_context(httpx.AsyncClient(
                    transport=transport, follow_redirects=False,
                    headers={"User-Agent": "OneRequest/0.1 (travel research; no booking)"},
                    limits=httpx.Limits(max_connections=12)))
                llm = (dependencies.model_factory or LLMClient)(client, store)
                if getattr(llm, "aclose", None):
                    resources.push_async_callback(llm.aclose)
                if dependencies.travel_data_factory:
                    providers = dependencies.travel_data_factory(client, llm)
                elif travel_data_factory:
                    providers = travel_data_factory(client)
                else:
                    providers = KnowledgeTravelDataPort(
                        HttpTravelDataPort(client, os.getenv("PROVIDER_GATEWAY_URL", "http://provider-gateway:8091")), llm)
                if providers is not llm and getattr(providers, "aclose", None):
                    resources.push_async_callback(providers.aclose)
                app.state.store = store
                app.state.session_notifications = notifications
                app.state.session_views = SessionViews(store)
                app.state.llm = llm
                app.state.travel_data = providers
                app.state.planner = Planner(store, llm, providers)
                app.state.trips = TripApplication(store)
                app.state.identity_resolver = dependencies.identity_resolver
                app.state.command_worker = CommandWorker(store, app.state.planner, tracing=getattr(app.state, 'tracing', None)).start()
                resources.push_async_callback(app.state.command_worker.aclose)
                app.state.telegram_service = TelegramService(store, app.state.trips, app.state.command_worker, client).start()
                resources.push_async_callback(app.state.telegram_service.aclose)
                app.state.reminder_service = ReminderService(store, app.state.telegram_service).start()
                resources.push_async_callback(app.state.reminder_service.aclose)
                store.cleanup_retention()

                async def retention():
                    while True:
                        await asyncio.sleep(3600)
                        store.cleanup_retention()
                        app.state.reminder_service.cancel_unavailable()

                maintenance = asyncio.create_task(retention(), name="trip-retention")
                async def stop_maintenance():
                    maintenance.cancel()
                    await asyncio.gather(maintenance, return_exceptions=True)
                resources.push_async_callback(stop_maintenance)
                yield
        finally:
            active = False

    app = FastAPI(title="One Request — Travel", version="0.1.0", lifespan=lifespan)

    install_auth(app)
    install_telegram_auth(app)
    install_memory(app)
    install_telegram(app)
    install_aftercare(app)
    install_reminders(app)

    def trips(request):
        return request.app.state.trips.for_principal(request.state.principal)

    @app.exception_handler(ApplicationError)
    async def application_error(request, exc):
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)

    @app.exception_handler(CommandError)
    async def command_error(request, exc):
        return JSONResponse({"detail": {"code": exc.code, "message": "Команда не принята. Обновите поездку и повторите запрос."}},
                            status_code=exc.status_code,
                            headers={"Retry-After": str(exc.retry_after)} if exc.retry_after else None)

    @app.get("/health")
    async def health(request: Request):
        store = request.app.state.store
        store.db.execute("SELECT 1").fetchone()
        return {"status": "ok", "service": "one-request", "storage": "sqlite", "mode": "local", "version": "0.1.0"}

    @app.get("/api/config")
    async def config(request: Request):
        llm = request.app.state.llm
        capabilities = await request.app.state.travel_data.capabilities()
        return {"llm_configured": getattr(llm, "configured", True), "llm_dispatch_enabled": getattr(llm, "enabled", True), "llm_model": getattr(llm, "model", "host"), "requests_per_minute": getattr(getattr(llm, "limiter", None), "limit", None), "timezone": os.getenv("APP_TIMEZONE", "Asia/Nicosia"),
                "external_data_fallback_enabled": capabilities["external_data_fallback_enabled"],
                "providers": capabilities["providers"], "pricing": capabilities["pricing"],
                "data_gateway": capabilities.get("gateway", {}),
                "checkout_mode": "simulation_only", "price_policy": "Sourced public references are labeled by unit; only compatible group quotes enter trip totals. Unknown prices remain null.", "preferences": "Saved per-session suggestions; explicit opt-in needed to apply to new trips."}

    @app.get("/api/sessions")
    async def list_sessions(request: Request, limit: int = 20, cursor: str | None = None):
        page = trips(request).list(limit, cursor)
        return {"items": [{"id": item.id, "created_at": item.created_at, "updated_at": item.updated_at,
                           "revision": item.revision, "destination": item.intent.destination,
                           "title": item.intent.destination or "Новая поездка"} for item in page.items],
                "next_cursor": page.next_cursor}

    @app.post("/api/sessions", response_model=Session, status_code=201)
    async def new_session(request: Request, body: CreateSessionInput | None = None):
        return public_session(trips(request).create(body.preference_session_id if body else None,
                              apply_memory=body.apply_memory if body else False), 201)

    @app.get("/api/sessions/{session_id}", response_model=Session)
    async def get_session(request: Request, session_id: str):
        return public_session(trips(request).session(session_id))

    @app.get("/api/sessions/{session_id}/receipts")
    async def session_receipts(request: Request, session_id: str):
        return {"items": trips(request).receipts(session_id)}

    @app.get("/api/sessions/{session_id}/events")
    async def live_session(request: Request, session_id: str):
        trips(request).session(session_id)
        return SessionEventResponse(
            session_events(session_id, request.app.state.session_views, request.app.state.session_notifications,
                           authorize=lambda: access_still_valid(request, session_id)),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/api/sessions/{session_id}/messages", status_code=status.HTTP_202_ACCEPTED)
    async def send_message(request: Request, session_id: str, body: MessageInput):
        job, deduplicated = trips(request).message(session_id, body.content,
            key=request.headers.get("Idempotency-Key", ""), expected_revision=body.expected_revision,
            action=body.action, option_ids=body.option_ids)
        request.app.state.command_worker.notify(job.id)
        snapshot = request.app.state.store.read_session_snapshot(session_id)
        return {"job_id": job.id, "sequence": snapshot.sequence, "revision": trips(request).session(session_id).revision,
                "status": job.status, "deduplicated": deduplicated}

    @app.get("/api/jobs/{job_id}")
    async def get_job(request: Request, job_id: str):
        return trips(request).job(job_id).model_dump(exclude={"payload"})

    @app.post("/api/jobs/{job_id}/cancel")
    async def cancel_job(request: Request, job_id: str):
        job = trips(request).cancel(job_id)
        request.app.state.command_worker.cancel(job_id)
        return job.model_dump(exclude={"payload"})

    @app.post("/api/sessions/{session_id}/prices/refresh", status_code=status.HTTP_202_ACCEPTED)
    async def refresh_prices(request: Request, session_id: str, body: PriceRefreshInput):
        job, deduplicated = trips(request).refresh(session_id, body, key=request.headers.get("Idempotency-Key", ""))
        request.app.state.command_worker.notify(job.id)
        return {"job_id": job.id, "sequence": request.app.state.store.read_session_snapshot(session_id).sequence,
                "session_id": session_id, "status": job.status, "deduplicated": deduplicated}

    @app.post("/api/sessions/{session_id}/select", response_model=Session)
    async def select_option(request: Request, session_id: str, body: OptionInput):
        return public_session(trips(request).select(session_id, body.option_id, body.expected_revision))

    @app.post("/api/sessions/{session_id}/checkout/quote")
    async def checkout_quote(request: Request, session_id: str, body: QuoteInput):
        return trips(request).quote(session_id, body)

    @app.post("/api/sessions/{session_id}/checkout")
    async def checkout(request: Request, session_id: str, body: CheckoutInput):
        return trips(request).checkout(session_id, body)

    @app.get("/api/metrics")
    async def metrics(request: Request):
        return request.app.state.store.metrics()

    frontend = Path(os.getenv("FRONTEND_DIST", str(PROJECT_ROOT / "frontend" / "dist"))).resolve()
    if (frontend / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=frontend / "assets"), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    async def frontend_route(path: str):
        if path.startswith(("api/", "health/")):
            raise HTTPException(404, "Маршрут не найден.")
        candidate = (frontend / path).resolve()
        if candidate.is_relative_to(frontend) and candidate.is_file():
            return FileResponse(candidate)
        if (frontend / "index.html").is_file():
            return FileResponse(frontend / "index.html")
        return {"service": "One Request", "message": "Frontend build not found. Build frontend/ or use Vite with the /api proxy.", "api_docs": "/docs"}

    instrument_app(app, "one-request-agent")
    return app


app = create_app()
