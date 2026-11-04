"""Model/provider-network-free server for real browser integration only.

Run with the repository Python: -m uvicorn backend.tests.product_fixture_server:app
--host 127.0.0.1 --port 18080. Never use this module for deployment.
"""
import os
from contextlib import asynccontextmanager
from datetime import timedelta

import httpx

from backend.app.application.dependencies import AgentDependencies
from backend.app.data_gateway import create_gateway_app
from backend.app.infrastructure.http_travel_data import HttpTravelDataPort
from backend.app.main import create_app
from backend.app.models import IntentExtraction, local_today, merge_intent
from backend.app.geography import Place
from backend.app.search.domain import SearchHit, SearchOutcome

# Explicit test-only composition: no ambient credentials, exporter or live I/O.
os.environ["WEB_FALLBACK_ENABLED"] = "0"
os.environ["LOCAL_CATALOG_ENABLED"] = "1"
os.environ["OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"] = ""
os.environ["TELEGRAM_MODE"] = "disabled"
os.environ["TELEGRAM_BOT_TOKEN"] = ""
os.environ["APP_ALLOWED_ORIGINS"] = "http://127.0.0.1:18080"


def forbidden(request):
    raise AssertionError("Fixture server forbids external model/provider HTTP")


class FixtureModel:
    configured = True
    enabled = True
    model = "fixture-only-no-inference"
    bora_active = False

    async def extract(self, session):
        patch = {}
        message = next((item.content for item in reversed(session.messages) if item.role == "user"), "")
        FixtureModel.bora_active = message.startswith("Из Казани на Бора-Бора") or session.intent.destination == "Бора-Бора"
        if message.startswith("Из Казани на Бора-Бора"):
            patch = {"origin": "Казань", "destination": "Бора-Бора",
                     "date_from": "2026-12-31", "date_to": "2027-01-07", "nights_min": 7, "nights_max": 7,
                     "flight_departure_from": "2026-12-28", "flight_departure_to": "2026-12-30",
                     "adults": 2, "budget_total": 5000, "currency": "EUR", "direct_flight_preferred": False,
                     "hotel_requirements": ["quiet hotel", "breakfast"], "soft_constraints": ["connections allowed"]}
        elif message == "Вылет только 29–30 декабря, остальное без изменений." and session.intent.destination == "Бора-Бора":
            patch = {"flight_departure_from": "2026-12-29", "flight_departure_to": "2026-12-30"}
        elif message == "А если Мальдивы?" and session.intent.origin == "Казань":
            patch = {"destination": "Мальдивы"}
        elif message.startswith("Из Казани в Ларнаку"):
            year = local_today().year + (1 if local_today().month == 12 and local_today().day == 31 else 0)
            patch = {"origin": "Казань", "destination": "Ларнака",
                     "date_from": f"{year}-12-31", "date_to": f"{year + 1}-01-01",
                     "nights_min": 1, "nights_max": 1, "adults": 2,
                     "budget_total": 100000, "currency": "RUB",
                     "hotel_requirements": ["quiet hotel", "breakfast"], "hard_constraints": ["no_hostels"]}
        elif not session.intent.origin:
            start = local_today() + timedelta(days=60)
            patch = {"origin": "Warsaw", "destination": "Larnaca",
                     "date_from": start.isoformat(), "date_to": (start + timedelta(days=7)).isoformat(),
                     "adults": 2, "budget_total": 2000, "currency": "EUR"}
        extraction = IntentExtraction(state_patch=patch)
        intent, changed = merge_intent(session.intent, patch)
        return extraction, intent, changed


gateway = create_gateway_app(database_path=":memory:", transport=httpx.MockTransport(forbidden))


class FixturePort(HttpTravelDataPort):
    async def aclose(self):
        await self.fixture_client.aclose()


def port_factory(client, model):
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=gateway))
    port = FixturePort(client, "http://fixture-gateway")
    port.fixture_client = client
    return port


app = create_app(database_path=":memory:", transport=httpx.MockTransport(forbidden),
                 dependencies=AgentDependencies(model_factory=lambda *_: FixtureModel(),
                                                travel_data_factory=port_factory))
agent_lifespan = app.router.lifespan_context


def install_no_airport_sources(providers):
    """Finite test source inputs; retain production candidate/fallback/calculator code."""
    original_resolve = providers.resolve_place
    async def resolve(name):
        normalized = name.casefold().strip()
        if normalized in {"казань", "kazan"}:
            return Place("Казань", "Россия", "RU", 55.79, 49.12, "KZN" if FixtureModel.bora_active else None, (), ("kazan", "казань"), "", "Fixture origin", source_provider="fixture-only")
        if normalized in {"бора-бора", "bora bora", "bora-bora"}:
            return Place("Бора-Бора", "Французская Полинезия", "PF", -16.50, -151.74, "BOB", ("beach", "warm"), ("bora bora", "bora-bora", "бора-бора"), "", "Fixture destination; not fetched inventory", source_provider="fixture-only")
        if normalized in {"мальдивы", "maldives"}:
            return Place("Мальдивы", "Мальдивы", "MV", 3.2, 73.22, None, ("beach", "warm"), ("maldives", "мальдивы"), "", "Fixture country without an inferred airport", source_provider="fixture-only", scope="country")
        return await original_resolve(name)
    async def empty_get(*args, **kwargs):
        return {}, "unavailable", "2026-10-28T10:00:00Z"
    async def search(query, **kwargs):
        if any(term in query.casefold() for term in ("bora bora", "bora-bora", "бора-бора")):
            rows = [("The St Regis Bora Bora Resort", "https://www.booking.com/hotel/pf/the-st-regis-bora-bora-resort.html"),
                    ("Four Seasons Resort Bora Bora", "https://www.booking.com/hotel/pf/four-seasons-resort-bora-bora.html"),
                    ("Intercontinental Bora Bora Le Moana Resort", "https://www.booking.com/hotel/pf/intercontinental-bora-bora-le-moana-resort.html")]
            place_label = "Bora Bora"
        elif any(term in query.casefold() for term in ("maldives", "мальдив")):
            rows = [("VARU by Atmosphere Maldives", "https://www.booking.com/hotel/mv/varu-by-atmosphere.ru.html"),
                    ("Baros Maldives", "https://www.booking.com/hotel/mv/baros-maldives.html"),
                    ("Ayada Maldives", "https://www.booking.com/hotel/mv/ayada-maldives.html")]
            place_label = "Maldives"
        else:
            return (SearchOutcome("fixture-only", (), "disabled"),)
        hits = tuple(SearchHit("fixture-only", title, url, place_label + " fixture source link; rates, availability and conditions unverified.", "2026-10-28T10:00:00Z", "cached") for title, url in rows)
        return (SearchOutcome("fixture-only", hits, "cached"),)
    async def identity_rank(rows, *args, **kwargs):
        return rows
    providers.resolve_place = resolve
    providers.cached_get = empty_get
    providers.web_search.search = search
    providers.ranker.rank_rows = identity_rank
    providers.web_fallback_enabled = True
    providers.pricing.enabled = False


@asynccontextmanager
async def fixture_lifespan(owner):
    async with gateway.router.lifespan_context(gateway):
        install_no_airport_sources(gateway.state.providers)
        async with agent_lifespan(owner):
            yield


app.router.lifespan_context = fixture_lifespan
