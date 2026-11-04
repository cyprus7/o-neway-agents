"""Receipt context, unsent drafts and linked amendments without inference."""
from datetime import timedelta
import json

import pytest
from pydantic import ValidationError

from backend.app.application.aftercare import AftercareApplication, AmendInput, DraftInput, ManualContextInput, ArchiveInput
from backend.app.application.commands import CommandError
from backend.app.application.errors import ApplicationError
from backend.app.application.trips import TripApplication
from backend.app.models import TravelIntent, local_today
from backend.app.storage import Store
from backend.tests.test_pricing_core import prepared_session


@pytest.fixture
def store(tmp_path):
    value = Store(str(tmp_path / "aftercare.db"))
    yield value
    value.close()


def receipt_fixture(store):
    owner = store.create_user().user_id
    session = prepared_session(store)
    store.assign_legacy_trip(session.id, owner)
    receipt = {"id": "receipt-original", "session_id": session.id, "revision": session.revision,
        "status": "simulated", "paid_amount_minor": 12345, "created_at": "2026-10-20T12:00:00Z",
        "itinerary": {"intent": session.intent.model_dump(mode="json"), "option": session.options[0].model_dump(mode="json")}}
    store.save_checkout(receipt)
    return owner, session, receipt


def test_receipt_restart_and_current_trip_changes_do_not_change_amendment_source(tmp_path):
    path = str(tmp_path / "restart.db")
    store = Store(path)
    owner, session, receipt = receipt_fixture(store)
    original = store.db.execute("SELECT data FROM checkouts").fetchone()[0]
    session.intent = TravelIntent(origin="Paris", destination="Berlin", adults=8)
    store.save_session(session)
    store.close()
    store = Store(path)
    body = AmendInput(date_from=local_today()+timedelta(days=60), date_to=local_today()+timedelta(days=65))
    child, job = AftercareApplication(store, TripApplication(store)).amend(owner, session.id, body, "amend-key-0001")
    assert child.intent.origin == receipt["itinerary"]["intent"]["origin"]
    assert child.intent.destination == receipt["itinerary"]["intent"]["destination"]
    assert child.intent.adults == receipt["itinerary"]["intent"]["adults"]
    assert child.intent.date_from == body.date_from and not child.options and child.selected_option_id is None
    assert child.previous_plan.intent.date_from.isoformat() == receipt["itinerary"]["intent"]["date_from"]
    assert child.previous_plan.options[0].id == receipt["itinerary"]["option"]["id"]
    assert child.preferences["aftercare_amendment"]["fixed_hotel"] is True
    assert child.preferences["aftercare_amendment"]["booking_verified"] is False
    assert any("aftercare_hotel_unverified:" in c for c in child.intent.soft_constraints)
    assert store.db.execute("SELECT parent_trip_id FROM session_owners WHERE session_id=?", (child.id,)).fetchone()[0] == session.id
    assert job.session_id == child.id and job.captured_revision == 0
    assert store.db.execute("SELECT data FROM checkouts").fetchone()[0] == original
    store.close()


def test_amendment_idempotency_and_conflict_leave_one_child(store):
    owner, session, receipt = receipt_fixture(store)
    application = AftercareApplication(store, TripApplication(store))
    body = AmendInput(request="Find airport transfer while preserving this hotel")
    child, job = application.amend(owner, session.id, body, "amend-key-0001")
    repeated, same = application.amend(owner, session.id, body, "amend-key-0001")
    assert repeated.id == child.id and same.id == job.id
    with pytest.raises(CommandError, match="idempotency_conflict"):
        application.amend(owner, session.id, AmendInput(request="Changed request"), "amend-key-0001")
    assert store.db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 2
    assert store.db.execute("SELECT COUNT(*) FROM commands").fetchone()[0] == 1


def test_legacy_receipt_never_reconstructs_from_current_trip_requires_manual_context(store):
    owner, session, receipt = receipt_fixture(store)
    with store.transaction():
        store.db.execute("UPDATE checkouts SET data=?", (json.dumps({"id": "legacy", "session_id": session.id, "status": "simulated"}),))
    application = AftercareApplication(store, TripApplication(store))
    with pytest.raises(ApplicationError) as exc:
        application.amend(owner, session.id, AmendInput(request="Transfer"), "amend-key-0001")
    assert exc.value.detail["code"] == "manual_context_required"
    assert store.db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1
    manual = ManualContextInput(destination="Athens", hotel="User named hotel", date_from=local_today()+timedelta(days=10), date_to=local_today()+timedelta(days=14))
    stored = store.put_manual_context(owner, session.id, manual)
    assert stored["source"] == "user_provided_unverified"
    child, job = application.amend(owner, session.id, AmendInput(request="Transfer"), "amend-key-0001")
    assert child.intent.destination == "Athens"
    assert child.previous_plan is None and not child.preferences["aftercare_amendment"]["fixed_hotel"]


def test_unsent_draft_archive_owner_checks_and_receipt_immutability(store):
    owner, session, receipt = receipt_fixture(store)
    other = store.create_user().user_id
    raw = store.db.execute("SELECT data FROM checkouts").fetchone()[0]
    body = DraftInput(kind="early_checkin", text="Please consider early check-in; not sent")
    draft = store.create_aftercare_draft(owner, session.id, body)
    assert draft["status"] == "draft"
    archived = store.archive_aftercare_draft(owner, session.id, draft["id"])
    assert archived["status"] == "archived"
    with pytest.raises(CommandError): store.list_aftercare_drafts(other, session.id)
    with pytest.raises(CommandError): store.archive_aftercare_draft(other, session.id, draft["id"])
    with pytest.raises(CommandError): AftercareApplication(store, TripApplication(store)).amend(other, session.id, AmendInput(request="transfer"), "other-key-0001")
    assert store.db.execute("SELECT data FROM checkouts").fetchone()[0] == raw
    with pytest.raises(ValidationError): ArchiveInput(status="sent")
    with pytest.raises(ValidationError): DraftInput(kind="supplier_send", text="anything")


def test_draft_requires_context_and_manual_data_is_finite_and_unverified(store):
    owner = store.create_user().user_id
    session = store.create_owned_session(owner)
    with pytest.raises(CommandError, match="manual_context_required"):
        store.create_aftercare_draft(owner, session.id, DraftInput(kind="service_request", text="Help"))
    with pytest.raises(ValidationError):
        ManualContextInput(destination="Athens", hotel="Hotel", date_from="2027-01-05", date_to="2027-01-01")
    with pytest.raises(ValidationError):
        ManualContextInput(destination="Athens", hotel="Hotel", date_from="2027-01-01", date_to="2027-01-02", card_number="not accepted")
    body = ManualContextInput(destination="Athens", hotel="Hotel", date_from="2027-01-01", date_to="2027-01-02")
    store.put_manual_context(owner, session.id, body)
    assert store.create_aftercare_draft(owner, session.id, DraftInput(kind="service_request", text="Help"))["status"] == "draft"


def test_invalid_new_dates_or_foreign_receipt_roll_back_children(store):
    owner, session, receipt = receipt_fixture(store)
    application = AftercareApplication(store, TripApplication(store))
    with pytest.raises(ApplicationError) as exc:
        application.amend(owner, session.id, AmendInput(date_from=local_today()+timedelta(days=60), date_to=local_today()+timedelta(days=59)), "amend-key-0001")
    assert exc.value.status_code == 422
    with pytest.raises(CommandError):
        application.amend(owner, session.id, AmendInput(receipt_id="foreign", request="transfer"), "amend-key-0001")
    assert store.db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1


def test_aftercare_exports_and_deletion_follow_trip(store):
    owner, session, receipt = receipt_fixture(store)
    store.put_manual_context(owner, session.id, ManualContextInput(destination="Athens", hotel="Hotel", date_from="2027-01-01", date_to="2027-01-02"))
    store.create_aftercare_draft(owner, session.id, DraftInput(kind="service_request", text="Unsent"))
    exported = store.export_owned_data(owner)
    assert len(exported["drafts"]) == len(exported["manual_contexts"]) == 1
    store.delete_owned_session(owner, session.id)
    assert store.db.execute("SELECT COUNT(*) FROM aftercare_drafts").fetchone()[0] == 0
    assert store.db.execute("SELECT COUNT(*) FROM aftercare_manual").fetchone()[0] == 0


def test_planning_preferences_preserve_only_known_metadata_without_copying_to_other_trip(store):
    owner, session, receipt = receipt_fixture(store)
    application = AftercareApplication(store, TripApplication(store))
    body = AmendInput(request="Find transfer")
    child, job = application.amend(owner, session.id, body, "amend-key-0001")
    metadata = dict(child.preferences["aftercare_amendment"])
    child.preferences["aftercare_amendment"]["fixed_hotel"] = False  # Explicit destination change.
    child.preferences["applied_memory_fields"] = ["pets"]
    child.preferences["unrecognized_private_metadata"] = "must not survive suggestion rewrite"
    store.save_preferences(child)
    store.save_session(child)
    reloaded = store.get_owned_session(child.id, owner)
    assert reloaded.preferences["aftercare_amendment"]["fixed_hotel"] is False
    assert reloaded.preferences["aftercare_amendment"]["request_hash"] == metadata["request_hash"]
    assert reloaded.preferences["applied_memory_fields"] == ["pets"]
    assert "unrecognized_private_metadata" not in reloaded.preferences
    repeated, same = application.amend(owner, session.id, body, "amend-key-0001")
    assert repeated.id == child.id and same.id == job.id
    fresh = store.create_owned_session(owner, preference_id=child.id)
    assert "aftercare_amendment" not in fresh.preferences and "applied_memory_fields" not in fresh.preferences


def test_migration_five_preserves_consented_memory_values_and_source_times(tmp_path, monkeypatch):
    from pathlib import Path
    path = str(tmp_path / "upgrade-four.db")
    original = Path.read_text
    def old_schema(path, *args, **kwargs):
        return "" if path.name == "005_aftercare.sql" else original(path, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(Path, "read_text", old_schema)
        store = Store(path)
        owner = store.create_user().user_id
        source = store.create_owned_session(owner)
        before = store.put_memory(owner, "origin", "Larnaca", source.id)
        with store.transaction(): store.db.execute("DELETE FROM schema_migrations WHERE version=5")
        store.close()
    store = Store(path)
    assert store.list_memory(owner) == [before]
    pets = ["two small dogs; each under 8 kg"]
    assert store.put_memory(owner, "pets", pets, source.id)["value"] == pets
    store.close()
