"""Demo estimates cross projection/payment boundaries without becoming source facts."""
import asyncio
import json
from datetime import timedelta
from decimal import Decimal

import httpx
import pytest
from fastapi.testclient import TestClient

from backend.app.application.session_views import SessionViews, public_session_data
from backend.app.checkout import DemoQuoteError, demo_invoice
from backend.app.infrastructure.session_notifications import SessionNotifications
from backend.app.models import PlanSnapshot, TravelComponent, TravelIntent, TripOption, local_today
from backend.app.pricing.demo import build_demo_pricing
from backend.app.transport.session_events import session_events
from backend.tests.factories import create_app
from backend.tests.test_pricing_core import published_reference


@pytest.fixture
def legacy_trip(tmp_path):
    def forbidden_request(request):
        pytest.fail("Projection/payment must not dispatch providers or inference")

    app = create_app(database_path=str(tmp_path / "projection.db"),
                     transport=httpx.MockTransport(forbidden_request))
    with TestClient(app) as client:
        session = app.state.store.create_session()
        session.revision = 1
        session.intent = TravelIntent(origin="Warsaw", destination="Larnaca",
            date_from=local_today() + timedelta(days=45), date_to=local_today() + timedelta(days=52),
            adults=2, children_ages=[6], rooms=1, budget_total=2000, pets=["small dog 8kg; cabin only"])
        for index in range(3):
            session.options.append(TripOption(id=f"legacy-{index}", destination="Larnaca", country="Cyprus",
                subtitle="Synthetic property fixture", latitude=34.9, longitude=33.6, score=80,
                currency="EUR", price_status="unavailable",
                flight=TravelComponent(title="Flight", description="Unknown"),
                stay=TravelComponent(title=f"Property {index}", description="Unknown",
                    source_url=f"https://synthetic.example/hotel/{index}", details={"source_identity": str(index)}),
                transfer=TravelComponent(title="Transfer", description="Unknown"),
                reasons=[], tradeoffs=[], risks=[], evidence=[],
                constraint_checks=[{"constraint": "Availability", "status": "unknown"},
                                   {"constraint": "Cabin pet", "status": "failed"}]))
        session.selected_option_id = session.options[0].id
        app.state.store.save_session(session)
        yield app, client, session


def assert_complete(pricing):
    assert pricing["schema_version"] == "demo-pricing.v1"
    assert [part["kind"] for part in pricing["components"]] == ["flight", "stay", "transfer"]
    for part in pricing["components"]:
        assert Decimal(part["value"]["min"]) > 0
        assert Decimal(part["value"]["min"]) <= Decimal(part["example_amount_minor"]) / 100 <= Decimal(part["value"]["max"])
        assert part["value"]["currency"] == pricing["currency"]
    assert sum(part["example_amount_minor"] for part in pricing["components"]) + pricing["example_service_fee_minor"] == pricing["example_total_minor"]


def test_legacy_get_projects_three_options_without_rewriting_real_unknowns(legacy_trip):
    app, client, session = legacy_trip
    before = app.state.store.get_session(session.id).model_dump_json()
    assert all(option.demo_pricing is None for option in session.options)
    response = client.get(f"/api/sessions/{session.id}")
    assert response.status_code == 200
    data = response.json()
    assert len(data["options"]) == 3
    for original, option in zip(session.options, data["options"]):
        assert_complete(option["demo_pricing"])
        assert option["estimated_total"] is None and option["price_status"] == "unavailable"
        assert option["constraint_checks"] == original.constraint_checks
        for name in ("flight", "stay", "transfer"):
            assert option[name]["quote"] is None and option[name]["price"] is None
        assert option["stay"]["source_url"] == original.stay.source_url
        assert option["stay"]["details"] == original.stay.details
    assert app.state.store.get_session(session.id).model_dump_json() == before
    assert session.model_dump_json() == before


@pytest.mark.parametrize("currency,expected", [("EUR", "EUR"), ("USD", "USD"), ("GBP", "GBP"), ("RUB", "RUB"), ("JPY", "EUR")])
def test_projection_and_invoice_share_currency_and_exact_example(legacy_trip, currency, expected):
    _, _, session = legacy_trip
    session.intent.currency = currency
    option = session.options[0]
    pricing = public_session_data(session)["options"][0]["demo_pricing"]
    invoice = demo_invoice(session, option)
    assert_complete(pricing)
    assert pricing["currency"] == invoice["currency"] == expected
    assert [line["amount_minor"] for line in invoice["line_items"]] == [part["example_amount_minor"] for part in pricing["components"]]
    assert invoice["service_fee_minor"] == pricing["example_service_fee_minor"]
    assert invoice["total_minor"] == pricing["example_total_minor"]
    assert invoice["source_total"] is None


def test_previous_plan_uses_own_dates_party_and_currency(legacy_trip):
    _, _, session = legacy_trip
    previous_intent = session.intent.model_copy(deep=True)
    previous_intent.currency = "GBP"
    previous_intent.adults = 1
    previous_intent.children_ages = []
    previous_intent.date_to = previous_intent.date_from + timedelta(days=2)
    session.previous_plan = PlanSnapshot(revision=0, intent=previous_intent,
        options=[session.options[0].model_copy(deep=True)])
    before = session.model_dump_json()
    data = public_session_data(session)
    historical = data["previous_plan"]["options"][0]["demo_pricing"]
    assert historical == build_demo_pricing(previous_intent, session.previous_plan.options[0]).model_dump(mode="json")
    assert historical["currency"] == "GBP"
    assert historical["example_total_minor"] != data["options"][0]["demo_pricing"]["example_total_minor"]
    assert session.model_dump_json() == before


def test_reference_provenance_survives_projection_without_becoming_verified(legacy_trip):
    _, _, session = legacy_trip
    source = published_reference("stay", expired=True)
    session.options[0].stay.quote = source
    before = session.model_dump_json()
    option = public_session_data(session)["options"][0]
    stay = option["demo_pricing"]["components"][1]
    assert stay["basis"] == "comparable_source"
    assert stay["source_url"] == source.source_url
    assert stay["source_observed_at"] == source.observed_at.isoformat().replace("+00:00", "Z")
    assert option["stay"]["quote"]["normalized_group"] is None
    assert option["stay"]["price"] is None and option["estimated_total"] is None
    assert option["pricing_summary"]["full_trip"] is None
    assert option["pricing_summary"]["hard_constraint_status"] == "unknown"
    assert session.model_dump_json() == before


@pytest.mark.parametrize("changed", ["intent", "source"])
def test_old_invoice_rejects_same_revision_context_changes(legacy_trip, changed):
    _, _, session = legacy_trip
    option = session.options[0]
    invoice = demo_invoice(session, option)
    if changed == "intent":
        session.intent.children_ages = [7]
    else:
        option.stay.source_url = "https://synthetic.example/different-property"
    with pytest.raises(DemoQuoteError) as error:
        demo_invoice(session, option, invoice["quote_id"])
    assert error.value.code == "demo_quote_changed"


def test_cached_demo_is_ignored_for_projection_and_invoice_signing(legacy_trip):
    _, _, session = legacy_trip
    option = session.options[0]
    invoice = demo_invoice(session, option)
    other_intent = session.intent.model_copy(update={"currency": "USD", "adults": 9})
    option.demo_pricing = build_demo_pricing(other_intent, option)
    cached = option.demo_pricing.model_dump_json()
    assert demo_invoice(session, option, invoice["quote_id"]) == invoice
    projected = public_session_data(session)["options"][0]["demo_pricing"]
    assert projected == build_demo_pricing(session.intent, option).model_dump(mode="json")
    assert projected["currency"] == "EUR"
    assert option.demo_pricing.model_dump_json() == cached


def test_sse_snapshot_projects_estimates_without_persisting_them(legacy_trip):
    app, _, session = legacy_trip
    before = app.state.store.get_session(session.id).model_dump_json()

    async def read_once():
        notifications = SessionNotifications()
        stream = session_events(session.id, SessionViews(app.state.store), notifications)
        try:
            assert await anext(stream) == "retry: 2000\n\n"
            frame = await anext(stream)
            return json.loads(next(line[6:] for line in frame.splitlines() if line.startswith("data: ")))
        finally:
            await stream.aclose()
            assert notifications.subscriber_count == 0

    payload = asyncio.run(read_once())
    for option in payload["session"]["options"]:
        assert_complete(option["demo_pricing"])
    assert app.state.store.get_session(session.id).model_dump_json() == before
