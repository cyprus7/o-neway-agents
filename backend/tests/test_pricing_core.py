"""Synthetic API checks: references, current quote context and persisted time."""
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from backend.app.geography import find_place
from backend.tests.factories import create_app
from backend.app.models import TravelComponent, TravelIntent, TripOption, local_today
from backend.app.planner import apply_option_pricing, constraint_checks
from backend.app.pricing.models import LowerBoundPrice, PointPrice, PriceObservation, QuoteScope
from backend.app.storage import Store


def scoped_quote(component, intent, amount="100.10", *, expires_at=None, observed_at=None):
    clock = datetime.now(timezone.utc)
    observed = observed_at or clock - timedelta(minutes=1)
    scope = QuoteScope(component=component, entity_level="property" if component == "stay" else "route",
                       entity_id="synthetic-hotel" if component == "stay" else "synthetic-route", origin="LCA", destination="ATH",
                       date_from=intent.date_from, date_to=intent.date_to, adults=intent.adults, children_ages=intent.children_ages,
                       rooms=intent.rooms, room_allocation=intent.room_allocation, trip_type="roundtrip" if component == "flight" else "not_applicable",
                       currency=intent.currency, unit="group_stay" if component == "stay" else "group_trip", context_verified=True,
                       taxes="included", mandatory_fees="included")
    value = PointPrice(amount=amount, currency=intent.currency)
    return PriceObservation(provider_id="synthetic-provider", scope=scope, value=value, normalized_group=value,
                            observed_at=observed, retrieved_at=clock, expires_at=expires_at or clock + timedelta(minutes=25),
                            source_url="https://synthetic.example/" + component, outcome="quoted", component_complete=True)


def published_reference(component="flight", *, expired=False):
    clock = datetime.now(timezone.utc)
    return PriceObservation(provider_id="synthetic-public-rate", scope=QuoteScope(component=component,
        entity_level="route" if component == "flight" else "property", origin="LCA", destination="ATH", currency="EUR",
        unit="person_trip" if component == "flight" else "room_night"),
        value=LowerBoundPrice(min="49.99", currency="EUR"), source_url="https://synthetic.example/public-rate",
        observed_at=clock - timedelta(hours=1), retrieved_at=clock,
        expires_at=clock - timedelta(minutes=1) if expired else clock + timedelta(minutes=10),
        outcome="reference", unit_label="На взрослого в одну сторону" if component == "flight" else "За номер/ночь; применимый год неизвестен",
        reason="Даты и состав группы не подтверждены")


def prepared_session(store, *, refs=False, complete=False, option_count=1):
    session = store.create_session()
    session.revision = 1
    session.intent = TravelIntent(origin="Ларнака", destination="Афины", date_from=local_today() + timedelta(days=45),
                                  date_to=local_today() + timedelta(days=52), adults=1, budget_total=500)
    for index in range(option_count):
        components = []
        for name in ("flight", "stay", "transfer"):
            observation = published_reference(name) if refs and name != "transfer" else scoped_quote(name, session.intent) if (complete or name != "transfer") and not refs else None
            components.append(TravelComponent(title="Synthetic " + name, description="Synthetic fixture only", quote=observation))
        flight, stay, transfer = components
        option = TripOption(id=f"r1-athens{index}", destination="Афины", country="Греция", subtitle="Synthetic fixture", latitude=37.98,
                            longitude=23.73, score=70, currency="EUR", price_status="unavailable", flight=flight, stay=stay, transfer=transfer,
                            reasons=[], tradeoffs=[], risks=[], evidence=[], constraint_checks=constraint_checks(session.intent, find_place("Афины"), tuple(components)))
        apply_option_pricing(option, session.intent)
        session.options.append(option)
    store.save_session(session)
    return session


def test_legacy_intent_gets_one_room_and_allocation_preserves_duplicate_ages():
    assert TravelIntent.model_validate({"adults": 2}).rooms == 1
    TravelIntent(adults=2, children_ages=[4, 4], rooms=2, room_allocation=[{"adults": 1, "children_ages": [4]}, {"adults": 1, "children_ages": [4]}])
    with pytest.raises(ValidationError):
        TravelIntent(adults=2, children_ages=[4, 4], rooms=2, room_allocation=[{"adults": 1, "children_ages": [4]}, {"adults": 1, "children_ages": []}])


def test_public_session_shows_reference_digits_and_no_trip_total_or_verified_budget(tmp_path):
    app = create_app(database_path=str(tmp_path / "public.db"), transport=httpx.MockTransport(lambda _: httpx.Response(503)))
    with TestClient(app) as client:
        session = prepared_session(app.state.store, refs=True)
        response = client.get(f"/api/sessions/{session.id}")
        option = response.json()["options"][0]
        assert option["flight"]["quote"]["value"]["min"] == "49.99"
        assert option["flight"]["quote"]["normalized_group"] is None
        assert option["flight"]["price"] is None and option["estimated_total"] is None
        assert option["pricing_summary"]["hard_constraint_status"] == "unknown"
        assert option["pricing_summary"]["known_partial"] is None


def test_public_projection_computes_cache_freshness_without_changing_observation_time(tmp_path):
    app = create_app(database_path=str(tmp_path / "time.db"), transport=httpx.MockTransport(lambda _: httpx.Response(503)))
    with TestClient(app) as client:
        session = prepared_session(app.state.store)
        session.options[0].flight.details["pricing_delivery"] = "cache_reuse"
        app.state.store.save_session(session)
        original = session.options[0].flight.quote.model_dump_json()
        response = client.get(f"/api/sessions/{session.id}").json()
        assert response["options"][0]["flight"]["quote"]["freshness"] == "cached"
        assert app.state.store.get_session(session.id).options[0].flight.quote.model_dump_json() == original


def test_public_projection_uses_chosen_observation_delivery_despite_mixed_attempts(tmp_path):
    app = create_app(database_path=str(tmp_path / 'mixed-time.db'), transport=httpx.MockTransport(lambda _: httpx.Response(503)))
    with TestClient(app) as client:
        session = prepared_session(app.state.store, refs=True)
        stay = session.options[0].stay
        stay.details.update(pricing_delivery='mixed', pricing_observation_delivery='cache_reuse')
        original = stay.quote.model_dump_json()
        app.state.store.save_session(session)
        result = client.get(f'/api/sessions/{session.id}').json()['options'][0]['stay']
        assert result['details']['pricing_delivery'] == 'mixed'
        assert result['quote']['freshness'] == 'cached'
        assert result['quote']['retrieved_at'] == stay.quote.retrieved_at.isoformat().replace('+00:00', 'Z')
        assert app.state.store.get_session(session.id).options[0].stay.quote.model_dump_json() == original


def test_expired_quote_is_rejected_before_existing_idempotent_receipt(tmp_path):
    app = create_app(database_path=str(tmp_path / "expired.db"), transport=httpx.MockTransport(lambda _: httpx.Response(503)))
    with TestClient(app) as client:
        session = prepared_session(app.state.store)
        url = f"/api/sessions/{session.id}"
        option_id = session.options[0].id
        assert client.post(url + "/select", json={"option_id": option_id}).status_code == 200
        receipt = client.post(url + "/checkout", json={"option_id": option_id})
        assert receipt.status_code == 200
        session = app.state.store.get_session(session.id)
        session.options[0].flight.quote = scoped_quote("flight", session.intent, observed_at=datetime.now(timezone.utc) - timedelta(hours=1), expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))
        app.state.store.save_session(session)
        rejected = client.post(url + "/checkout", json={"option_id": option_id})
        assert rejected.status_code == 409 and rejected.json()["detail"]["code"] == "price_expired"
        assert app.state.store.find_checkout(session.id, session.revision, option_id)["id"] == receipt.json()["id"]
        projected = client.get(url).json()["options"][0]
        assert projected["flight"]["quote"]["freshness"] == "stale" and projected["flight"]["price"] is None


@pytest.mark.parametrize("change", [{"adults": 2}, {"children_ages": [0]}, {"rooms": 2}, {"currency": "USD"}, {"date_to": "+1day"}])
def test_changed_trip_cannot_checkout_old_quote_without_revision_change(tmp_path, change):
    app = create_app(database_path=str(tmp_path / "context.db"), transport=httpx.MockTransport(lambda _: httpx.Response(503)))
    with TestClient(app) as client:
        session = prepared_session(app.state.store)
        # Collection and execution can straddle midnight. Always change the
        # actual quoted context instead of comparing two separate wall clocks.
        if change.get("date_to") == "+1day":
            change = {"date_to": session.intent.date_to + timedelta(days=1)}
        option_id = session.options[0].id
        session.selected_option_id = option_id
        session.intent = TravelIntent(**{**session.intent.model_dump(), **change})
        app.state.store.save_session(session)
        rejected = client.post(f"/api/sessions/{session.id}/checkout", json={"option_id": option_id})
        assert rejected.status_code == 409 and rejected.json()["detail"]["code"] == "price_context_mismatch"


def test_expired_reference_allows_only_warning_bearing_research_simulation(tmp_path):
    app = create_app(database_path=str(tmp_path / "reference.db"), transport=httpx.MockTransport(lambda _: httpx.Response(503)))
    with TestClient(app) as client:
        session = prepared_session(app.state.store, refs=True)
        session.options[0].flight.quote = published_reference(expired=True)
        app.state.store.save_session(session)
        option_id = session.options[0].id
        assert client.post(f"/api/sessions/{session.id}/select", json={"option_id": option_id}).status_code == 200
        result = client.post(f"/api/sessions/{session.id}/checkout", json={"option_id": option_id})
        assert result.status_code == 200 and result.json()["ready_to_book"] is False
        assert any("Опубликованный ориентир" in text for text in result.json()["warnings"])


def test_changed_property_identity_does_not_reuse_another_hotels_quote(tmp_path):
    app = create_app(database_path=str(tmp_path / "entity.db"), transport=httpx.MockTransport(lambda _: httpx.Response(503)))
    with TestClient(app) as client:
        session = prepared_session(app.state.store)
        session.options[0].stay.details["pricing_entity_id"] = "another-property"
        session.selected_option_id = session.options[0].id
        app.state.store.save_session(session)
        response = client.post(f"/api/sessions/{session.id}/checkout", json={"option_id": session.selected_option_id})
        assert response.status_code == 409 and response.json()["detail"]["code"] == "price_context_mismatch"


def test_observation_retention_is_explicit_immutable_and_survives_restart(tmp_path):
    path = str(tmp_path / "retain.db")
    store = Store(path)
    session = prepared_session(store)
    observation = session.options[0].flight.quote
    data = {**observation.model_dump(), "request_context_hash": "synthetic-request"}
    observation = PriceObservation(**data)
    store.save_observation(observation)
    assert store.observations_for_request("synthetic-request", observation.provider_id) == []
    store.save_observation(observation, retention_seconds=3600)
    store.close()
    restored = Store(path)
    rows = restored.observations_for_request("synthetic-request", observation.provider_id)
    assert len(rows) == 1 and rows[0] == observation
    assert restored.get_session(session.id).options[0].flight.quote.expires_at == session.options[0].flight.quote.expires_at
    restored.close()
