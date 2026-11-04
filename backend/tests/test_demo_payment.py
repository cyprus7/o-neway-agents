"""Fictional payment contract tests; no inventory, gateway or model calls."""
from datetime import timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from backend.app import checkout as demo
from backend.tests.factories import create_app
from backend.app.models import TravelComponent, TravelIntent, TripOption, local_today


@pytest.fixture
def payment(tmp_path):
    def no_network(request):
        raise AssertionError("Checkout must never dispatch network requests")

    app = create_app(database_path=str(tmp_path / "payment.db"), transport=httpx.MockTransport(no_network))
    with TestClient(app) as client:
        session = app.state.store.create_session()
        session.revision = 1
        session.intent = TravelIntent(origin="Варшава", destination="Ларнака", date_from=local_today() + timedelta(days=40),
                                      date_to=local_today() + timedelta(days=47), adults=2, children_ages=[6], rooms=1)
        session.options = [TripOption(id="r1-cyprus", destination="Ларнака", country="Кипр", subtitle="Synthetic fixture",
            latitude=34.9, longitude=33.6, score=80, currency="EUR", price_status="unavailable",
            flight=TravelComponent(title="Transport", description="Unknown"),
            stay=TravelComponent(title="Hotel", description="Unknown"),
            transfer=TravelComponent(title="Transfer", description="Unknown"),
            reasons=[], tradeoffs=[], risks=[], evidence=[], constraint_checks=[{"constraint": "Наличие", "status": "unknown"}])]
        session.selected_option_id = session.options[0].id
        app.state.store.save_session(session)
        yield app, client, session, f"/api/sessions/{session.id}"


def quote(client, session, url):
    response = client.post(url + "/checkout/quote", json={"option_id": session.selected_option_id, "expected_revision": session.revision})
    assert response.status_code == 200, response.text
    return response.json()


def pay(client, session, url, invoice):
    return client.post(url + "/checkout", json={"option_id": session.selected_option_id,
        "expected_revision": session.revision, "quote_id": invoice["quote_id"]})


def test_exact_fictional_invoice_preserves_real_unknowns_and_pays_idempotently(payment):
    app, client, session, url = payment
    before = app.state.store.get_session(session.id).model_dump_json()
    invoice = quote(client, session, url)
    assert invoice["pricing_basis"] == "synthetic_demo" and invoice["currency"] == "EUR"
    assert [line["amount_minor"] for line in invoice["line_items"]] == [46000, 63000, 7000]
    assert invoice["subtotal_minor"] == 116000 and invoice["service_fee_minor"] == 2900
    assert invoice["total_minor"] == 118900 and invoice["source_total"] is None
    assert "вымышленные" in invoice["disclaimer"]
    assert app.state.store.get_session(session.id).model_dump_json() == before
    assert app.state.store.find_checkout(session.id, session.revision, session.selected_option_id) is None
    response = pay(client, session, url, invoice)
    assert response.status_code == 200, response.text
    receipt = response.json()
    assert receipt["status"] == "simulated" and receipt["ready_to_book"] is False
    assert receipt["estimated_total"] is None
    assert receipt["demo_payment"]["status"] == "simulated_paid"
    assert receipt["demo_payment"]["paid_amount_minor"] == invoice["total_minor"]
    assert receipt["demo_payment"]["invoice"] == invoice
    again = pay(client, session, url, quote(client, session, url))
    assert again.json()["id"] == receipt["id"]
    assert app.state.store.metrics()["simulated_checkouts"] == 1
    assert "1234567890" not in str(receipt)


@pytest.mark.parametrize("change", ["intent", "price", "revision", "selection", "failed"])
def test_context_changes_reject_old_invoice_before_receipt_lookup(payment, change):
    app, client, session, url = payment
    invoice = quote(client, session, url)
    current = app.state.store.get_session(session.id)
    if change == "intent":
        current.intent.children_ages = [7]
    elif change == "price":
        current.options[0].stay.details["new_source_fact"] = "changed"
    elif change == "revision":
        current.revision += 1
    elif change == "selection":
        current.selected_option_id = None
    else:
        current.options[0].constraint_checks.append({"constraint": "Hard", "status": "failed"})
    app.state.store.save_session(current)
    response = pay(client, session, url, invoice)
    assert response.status_code == 409
    assert app.state.store.find_checkout(session.id, session.revision, session.selected_option_id) is None


def test_expired_tampered_invoices_and_extra_card_amounts_are_rejected(payment, monkeypatch):
    app, client, session, url = payment
    invoice = quote(client, session, url)
    bad = {**invoice, "quote_id": invoice["quote_id"][:-1] + ("a" if invoice["quote_id"][-1] != "a" else "b")}
    assert pay(client, session, url, bad).json()["detail"]["code"] == "demo_quote_invalid"
    real_clock = demo._clock()
    monkeypatch.setattr(demo, "_clock", lambda: real_clock + 901)
    expired = pay(client, session, url, invoice)
    assert expired.status_code == 409 and expired.json()["detail"]["code"] == "demo_quote_expired"
    for extra in ({"card_number": "4242424242424242"}, {"amount_minor": 1}, {"cvc": "123"}):
        response = client.post(url + "/checkout", json={"option_id": session.selected_option_id, **extra})
        assert response.status_code == 422
    assert app.state.store.find_checkout(session.id, session.revision, session.selected_option_id) is None


@pytest.mark.parametrize("token", ["ё.ё.ё", "1.abc.abc", "1." + "0" * 64 + "." + "я" * 64, "-1." + "0" * 64 + "." + "0" * 64])
def test_malformed_invoice_tokens_return_controlled_errors(payment, token):
    app, client, session, url = payment
    response = pay(client, session, url, {"quote_id": token})
    assert response.status_code == 409 and response.json()["detail"]["code"] == "demo_quote_invalid"


def test_expiry_is_checked_before_idempotent_receipt(payment, monkeypatch):
    app, client, session, url = payment
    invoice = quote(client, session, url)
    assert pay(client, session, url, invoice).status_code == 200
    future = demo._clock() + 901
    monkeypatch.setattr(demo, "_clock", lambda: future)
    assert pay(client, session, url, invoice).json()["detail"]["code"] == "demo_quote_expired"
    assert app.state.store.metrics()["simulated_checkouts"] == 1


def test_quote_requires_current_selection_revision_and_idle_session(payment):
    app, client, session, url = payment
    assert client.post(url + "/checkout/quote", json={"option_id": session.selected_option_id, "expected_revision": 0}).status_code == 409
    assert client.post(url + "/checkout/quote", json={"option_id": "other", "expected_revision": 1}).status_code == 409
    app.state.store.start_message(session.id, "Pending replan")
    assert client.post(url + "/checkout/quote", json={"option_id": session.selected_option_id, "expected_revision": 1}).status_code == 409


def test_legacy_checkout_remains_available(payment):
    app, client, session, url = payment
    first = client.post(url + "/checkout", json={"option_id": session.selected_option_id})
    assert first.status_code == 200 and first.json()["status"] == "simulated"
    assert client.post(url + "/checkout", json={"option_id": session.selected_option_id}).json()["id"] == first.json()["id"]
    invoice = quote(client, session, url)
    assert pay(client, session, url, invoice).json()["demo_payment"]["status"] == "simulated_paid"
