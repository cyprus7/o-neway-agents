"""Private, bounded Chromium service. Browser recipes receive structured travel inputs only."""
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import time

from fastapi import FastAPI
from playwright.async_api import async_playwright, TimeoutError as BrowserTimeout
from one_request_telemetry import instrument_app, queued, set_attributes, span, traced

from .guards import install_guards, pinned_resolver_rules
from .models import Context, QuoteRequest


@asynccontextmanager
async def lifespan(app):
    app.state.slots = asyncio.Semaphore(2)
    app.state.browser = None
    async with async_playwright() as playwright:
        try:
            resolver_rules = await pinned_resolver_rules()
            app.state.browser = await playwright.chromium.launch(headless=True, chromium_sandbox=True,
                args=['--host-resolver-rules=' + resolver_rules, '--disable-quic',
                      '--force-webrtc-ip-handling-policy=disable_non_proxied_udp'])
        except Exception as error:
            # A daemon healthcheck does not pretend a sandboxed browser is available.
            print('browser_start_failed:', type(error).__name__, flush=True)
        yield
        if app.state.browser:
            await app.state.browser.close()


app = FastAPI(lifespan=lifespan)
instrument_app(app, "one-request-price-worker", trust_incoming=True)


@app.get('/health')
async def health():
    ready = bool(app.state.browser and app.state.browser.is_connected())
    return {'status': 'ok' if ready else 'degraded', 'browser_ready': ready, 'version': '006-expanded-search-v1'}


@app.post('/v1/quotes')
@traced("price_worker.quotes")
async def quotes(request: QuoteRequest):
    set_attributes(provider=request.provider_id)
    started = time.monotonic()
    output = {'request_id': request.request_id, 'request_context_hash': request.request_context_hash}
    remaining = (request.deadline_at - datetime.now(timezone.utc)).total_seconds()
    if remaining <= 0 or remaining > 60:
        set_attributes(outcome="timeout", reason="invalid_deadline")
        return {**output, 'outcome': 'timeout', 'diagnostic_code': 'invalid_deadline'}
    if not app.state.browser or not app.state.browser.is_connected():
        set_attributes(outcome="not_configured", reason="browser_sandbox_unavailable")
        return {**output, 'outcome': 'not_configured', 'diagnostic_code': 'browser_sandbox_unavailable'}
    context = None
    try:
        async with asyncio.timeout(min(20, remaining)):
            async with queued(app.state.slots, "price_worker.queue"):
                context = await app.state.browser.new_context(locale='en-GB', service_workers='block', accept_downloads=False)
                await install_guards(context, request.provider_id)
                page = await context.new_page()
                page.set_default_timeout(4000)
                page.set_default_navigation_timeout(15000)
                from .recipes.lodging import google_hotels
                from .recipes.ostrovok import ostrovok
                recipe = ostrovok if request.provider_id == 'ostrovok' else google_hotels
                with span("price_worker.recipe"):
                    set_attributes(provider=request.provider_id)
                    result = await recipe(page, request.request_context.model_dump(mode='json'))
                    outcome = result.get('outcome')
                    outcome = outcome if outcome in ('quoted', 'reference', 'no_offers', 'blocked', 'requires_login',
                        'timeout', 'parse_error', 'context_mismatch', 'not_configured', 'policy_disabled',
                        'unsupported_party', 'currency_mismatch', 'unavailable') else 'unavailable'
                    set_attributes(outcome=outcome, reason=result.get('diagnostic_code'))
                set_attributes(outcome=outcome, reason=result.get('diagnostic_code'))
                return {**output, **result, 'elapsed_ms': round((time.monotonic() - started) * 1000)}
    except (TimeoutError, BrowserTimeout):
        set_attributes(outcome="timeout", reason="component_deadline")
        return {**output, 'outcome': 'timeout', 'diagnostic_code': 'component_deadline'}
    except Exception:
        set_attributes(outcome="parse_error", reason="recipe_unavailable")
        return {**output, 'outcome': 'parse_error', 'diagnostic_code': 'recipe_unavailable'}
    finally:
        if context:
            try:
                await asyncio.wait_for(context.close(), timeout=2)
            except Exception:
                pass
