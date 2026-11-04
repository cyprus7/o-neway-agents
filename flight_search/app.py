"""Private singleton Chromium flight service with bounded shared work."""
import asyncio
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import time

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from one_request_telemetry import instrument_app, queued, set_attributes, span, traced

from .domain import scope_diagnostic
from .guards import install_guards, pinned_resolver_rules
from .models import FlightDiagnostic, FlightSearchRequest, FlightSearchResponse, search_hash
from .recipe import google_flights


MAX_BODY = 64 * 1024
MAX_RESPONSE = 256 * 1024
MAX_PENDING = 16
OPERATION_SECONDS = 35
CLEANUP_SECONDS = 2


def utcnow():
    return datetime.now(timezone.utc)


@asynccontextmanager
async def chromium():
    from playwright.async_api import async_playwright
    async with async_playwright() as playwright:
        browser = None
        try:
            rules = await pinned_resolver_rules()
            browser = await playwright.chromium.launch(headless=True, chromium_sandbox=True,
                args=["--host-resolver-rules="+rules, "--disable-quic",
                      "--force-webrtc-ip-handling-policy=disable_non_proxied_udp"])
        except Exception:
            print("flight_browser_start_failed", flush=True)
        try:
            yield browser
        finally:
            if browser is not None:
                try:
                    await asyncio.wait_for(browser.close(), CLEANUP_SECONDS)
                except Exception:
                    print("flight_browser_shutdown_failed", flush=True)


@dataclass
class Job:
    checked_at: datetime
    task: asyncio.Task | None = None
    waiters: int = 0
    dispatched: bool = False


def create_app(*, browser_factory=chromium, recipe=google_flights, guard_factory=install_guards):
    @asynccontextmanager
    async def lifespan(app):
        app.state.slots = asyncio.Semaphore(1)
        app.state.pending = 0
        app.state.jobs = {}
        app.state.counters = {name:0 for name in ("flight_requests", "flight_source_requests", "flight_contexts_created",
            "flight_active_contexts", "flight_dedup_hits", "flight_completed", "flight_timeouts", "flight_cancelled",
            "flight_overloaded")}
        async with browser_factory() as browser:
            app.state.browser = browser
            try:
                yield
            finally:
                jobs = tuple(app.state.jobs.values())
                for job in jobs:
                    if job.task is not None:
                        job.task.cancel()
                if jobs:
                    await asyncio.gather(*(job.task for job in jobs if job.task is not None), return_exceptions=True)
                app.state.browser = None

    app = FastAPI(title="One Request flight search", version="1", lifespan=lifespan)

    @app.get("/health")
    @app.get("/ready")
    async def health():
        browser = app.state.browser
        ready = bool(browser is not None and browser.is_connected())
        return JSONResponse({"status":"ok" if ready else "degraded", "browser_ready":ready,
            "service":"flight-search", "version":"flight-search.v1", "context_limit":1}, status_code=200 if ready else 503)

    @app.get("/metrics")
    async def metrics():
        return {**app.state.counters, "flight_pending":app.state.pending, "flight_jobs":len(app.state.jobs)}

    def response(command, outcome, code, *, job=None, route=None, count=0):
        dispatched = bool(job and job.dispatched)
        checked = job.checked_at if job else utcnow()
        retrieved = route.retrieved_at if route else None
        value = FlightSearchResponse(request_id=command.request_id, request_context_hash=command.request_context_hash,
            outcome=outcome, diagnostic_code=code, route=route, candidate_count=count, retrieved_at=retrieved,
            diagnostics=(FlightDiagnostic(outcome=outcome, diagnostic_code=code, checked_at=checked,
                retrieved_at=retrieved, dispatched=dispatched, external_dispatched=dispatched),))
        body = value.model_dump_json().encode()
        if len(body) > MAX_RESPONSE:
            raise HTTPException(503, "flight_response_too_large")
        return JSONResponse(value.model_dump(mode="json"))

    async def close_context(context, browser):
        try:
            await asyncio.wait_for(context.close(), 1.5)
        except BaseException:
            # A stuck context must never permit a second active context. Closing
            # its owned browser invalidates readiness and all its source work.
            app.state.browser = None
            try:
                await asyncio.wait_for(browser.close(), .45)
            except BaseException:
                print("flight_browser_cleanup_failed", flush=True)

    async def invalidate_browser(browser):
        app.state.browser = None
        try:
            await asyncio.wait_for(browser.close(), 1.9)
        except BaseException:
            print("flight_browser_cleanup_failed", flush=True)

    async def run_job(command, job, seconds):
        context = None
        browser = app.state.browser
        resources = AsyncExitStack()
        try:
            # Reserve cleanup from the SAME whole-operation budget.
            async with asyncio.timeout(max(.001, seconds-CLEANUP_SECONDS-.1)):
                await resources.enter_async_context(queued(app.state.slots, "flight.queue"))
                if browser is None or browser is not app.state.browser or not browser.is_connected():
                    return {"outcome":"not_configured", "diagnostic_code":"flight_browser_unavailable"}
                with span("flight.browser", attributes={"provider":"google_flights"}):
                    try:
                        context = await browser.new_context(locale="en-US", service_workers="block", accept_downloads=False)
                    except BaseException:
                        # Playwright may allocate a context remotely before
                        # returning it. Invalidate its owned browser when
                        # acquisition fails so that allocation cannot leak.
                        await invalidate_browser(browser)
                        raise
                    else:
                        app.state.counters["flight_contexts_created"] += 1
                        app.state.counters["flight_active_contexts"] += 1
                        await guard_factory(context)
                        page = await context.new_page()
                        page.set_default_timeout(4000)
                        page.set_default_navigation_timeout(15000)
                        with span("flight.source", kind="client", attributes={"provider":"google_flights"}):
                            job.dispatched = True
                            app.state.counters["flight_source_requests"] += 1
                            result = await recipe(page, command)
                            set_attributes(outcome=result.get("outcome"), reason=result.get("diagnostic_code"),
                                           count=result.get("candidate_count", 0))
                        app.state.counters["flight_completed"] += 1
                        return result
        except asyncio.CancelledError:
            app.state.counters["flight_cancelled"] += 1
            raise
        except TimeoutError:
            app.state.counters["flight_timeouts"] += 1
            return {"outcome":"timeout", "diagnostic_code":"flight_timeout"}
        except Exception:
            return {"outcome":"parse_error", "diagnostic_code":"flight_parse_error"}
        finally:
            # Cleanup remains inside the whole budget and keeps the slot until
            # the context (or its browser) has closed, outside the work timer.
            try:
                if context is not None:
                    await close_context(context, browser)
                    app.state.counters["flight_active_contexts"] -= 1
            finally:
                await resources.aclose()

    async def disconnected(request):
        while True:
            # The body is already fully consumed. Await the ASGI disconnect
            # directly; polling is_disconnected uses an inner cancellation scope
            # that can swallow task cancellation and block response cleanup.
            if (await request.receive()).get("type") == "http.disconnect":
                return True

    @app.post("/v1/search")
    @traced("flight.search")
    async def search(request: Request):
        entered = time.monotonic()
        set_attributes(provider="google_flights")
        if app.state.pending >= MAX_PENDING:
            app.state.counters["flight_overloaded"] += 1
            raise HTTPException(503, "flight_overloaded")
        app.state.pending += 1
        app.state.counters["flight_requests"] += 1
        job = None
        key = None
        waiter = watch = None
        try:
            try:
                async with asyncio.timeout(OPERATION_SECONDS):
                    if request.query_params:
                        raise HTTPException(422, "flight_query_not_supported")
                    if request.headers.get("content-type", "").split(";", 1)[0].lower() != "application/json":
                        raise HTTPException(415, "flight_json_required")
                    if int(request.headers.get("content-length", "0")) > MAX_BODY:
                        raise HTTPException(413, "flight_request_too_large")
                    body = bytearray()
                    async for chunk in request.stream():
                        body.extend(chunk)
                        if len(body) > MAX_BODY:
                            raise HTTPException(413, "flight_request_too_large")
                    command = FlightSearchRequest.model_validate_json(body)
            except (ValueError, TypeError):
                raise HTTPException(422, "invalid_flight_request") from None
            diagnostic = scope_diagnostic(command)
            if diagnostic:
                set_attributes(outcome=diagnostic[1], reason=diagnostic[0])
                return response(command, diagnostic[1], diagnostic[0])
            remaining = (command.deadline_at-utcnow()).total_seconds()
            seconds = min(OPERATION_SECONDS-(time.monotonic()-entered), remaining)
            if seconds <= CLEANUP_SECONDS+.1 or remaining > 60:
                return response(command, "timeout", "flight_invalid_deadline")
            key = search_hash(command)
            job = app.state.jobs.get(key)
            if job is None:
                job = Job(checked_at=utcnow())
                app.state.jobs[key] = job
                job.task = asyncio.create_task(run_job(command, job, seconds))
            else:
                app.state.counters["flight_dedup_hits"] += 1
            job.waiters += 1
            async def await_job():
                return await asyncio.shield(job.task)
            waiter = asyncio.create_task(await_job())
            watch = asyncio.create_task(disconnected(request))
            async with asyncio.timeout(seconds):
                completed, _ = await asyncio.wait((waiter, watch), return_when=asyncio.FIRST_COMPLETED)
                if watch in completed:
                    raise asyncio.CancelledError
                result = await waiter
            set_attributes(outcome=result.get("outcome"), reason=result.get("diagnostic_code"))
            return response(command, result["outcome"], result["diagnostic_code"], job=job,
                            route=result.get("route"), count=result.get("candidate_count", 0))
        except TimeoutError:
            if job is not None:
                return response(command, "timeout", "flight_timeout", job=job)
            raise HTTPException(408, "flight_body_deadline") from None
        finally:
            for task in (waiter, watch):
                if task is not None and not task.done():
                    task.cancel()
            pending = [task for task in (waiter, watch) if task is not None]
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            if job is not None:
                job.waiters -= 1
                if job.waiters == 0:
                    if app.state.jobs.get(key) is job:
                        app.state.jobs.pop(key, None)
                    if not job.task.done():
                        job.task.cancel()
                    await asyncio.gather(job.task, return_exceptions=True)
            app.state.pending -= 1

    instrument_app(app, "one-request-flight-search", trust_incoming=True)
    return app


app = create_app()
