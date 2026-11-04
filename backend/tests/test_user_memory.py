"""Explicit memory, owner boundaries, deletion fences and bounded retention."""
import asyncio
from datetime import datetime, timedelta, timezone
import json

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
import pytest

from backend.app.application.commands import CommandError, StaleCommand, active_command_id
from backend.app.application.errors import ApplicationError
from backend.app.infrastructure.command_worker import CommandWorker
from backend.app.models import TravelIntent
from backend.app.storage import Store
from backend.app.transport.auth import install_auth
from backend.app.transport.memory import install_memory


@pytest.fixture
def store(tmp_path):
    store = Store(str(tmp_path / "memory.db"))
    yield store
    store.close()


def owned(store, kind="guest"):
    owner = store.create_user(kind).user_id
    return owner, store.create_owned_session(owner)


def test_memory_consent_restart_cross_owner_and_forget(tmp_path):
    path = str(tmp_path / "restart.db")
    store = Store(path)
    first, source = owned(store)
    second, foreign = owned(store)
    memory = store.put_memory(first, "origin", "Larnaca", source.id)
    assert memory["source_trip_id"] == source.id and memory["consented_at"] and memory["updated_at"]
    with pytest.raises(CommandError) as exc:
        store.put_memory(first, "origin", "Warsaw", foreign.id)
    assert exc.value.status_code == 404
    assert store.list_memory(second) == []
    store.close()
    store = Store(path)
    assert store.list_memory(first)[0] == memory
    assert store.forget_memory(first, "origin")
    assert not store.forget_memory(first, "origin")
    assert store.list_memory(first) == []
    store.close()


@pytest.mark.parametrize("field,value", [("date_from", "2027-01-01"), ("budget_total", 1000), ("adults", 3),
    ("children_ages", [4]), ("hard_constraints", ["anything"]), ("origin", ""), ("currency", "usd"),
    ("direct_flight_preferred", "true"), ("max_travel_minutes", True), ("max_travel_minutes", 0),
    ("trip_styles", "beach"), ("climate_preferences", ["x" * 501])])
def test_memory_finite_strict_allowlist(store, field, value):
    owner, source = owned(store)
    with pytest.raises(ValueError): store.put_memory(owner, field, value, source.id)
    assert store.list_memory(owner) == []


def test_apply_is_opt_in_new_trip_only_and_explicit_defaults_or_negations_win(store):
    owner, source = owned(store)
    for field, value in (("origin", "Larnaca"), ("currency", "USD"), ("trip_styles", ["beach"]),
                         ("direct_flight_preferred", True), ("max_travel_minutes", 120)):
        store.put_memory(owner, field, value, source.id)
    new = store.create_owned_session(owner)
    assert new.intent.origin is None  # No hidden copying on creation.
    applied = store.apply_memory(owner, new)
    assert set(applied) == {"origin", "currency", "trip_styles", "direct_flight_preferred", "max_travel_minutes"}
    assert new.intent.origin == "Larnaca" and new.intent.currency == "USD"
    assert new.preferences["applied_memory_fields"] == applied
    explicit = store.create_owned_session(owner)
    explicit.intent = TravelIntent(origin="Warsaw", currency="EUR", trip_styles=[], direct_flight_preferred=False, max_travel_minutes=60)
    assert store.apply_memory(owner, explicit) == []
    negated = store.create_owned_session(owner)
    negated.intent = TravelIntent(hard_constraints=["not beach; do not use Larnaca origin"])
    assert store.apply_memory(owner, negated) == []
    source.revision = 1
    assert store.apply_memory(owner, source) == []
    foreign, trip = owned(store)
    with pytest.raises(CommandError): store.apply_memory(foreign, new)


def test_export_only_owned_public_sessions_receipts_memory_and_no_job_or_auth_payload(store):
    owner, source = owned(store)
    foreign, other = owned(store)
    store.put_memory(owner, "currency", "EUR", source.id)
    credentials = store.create_access_session(owner)
    job, _ = store.admit_message(owner, source.id, "Hello", "command-001", 0)
    job.payload["private_document"] = "UNTRUSTED_RAW_PRICE_PAGE"
    store.save_job(job)
    store.save_checkout({"id": "receipt", "session_id": source.id, "status": "simulated", "revision": 0})
    store.save_checkout({"id": "foreign-receipt", "session_id": other.id, "status": "simulated", "revision": 0})
    result = store.export_owned_data(owner)
    assert [s["id"] for s in result["sessions"]] == [source.id]
    assert [r["id"] for r in result["receipts"]] == ["receipt"]
    assert result["memory"][0]["field"] == "currency"
    text = json.dumps(result)
    assert other.id not in text and "UNTRUSTED_RAW_PRICE_PAGE" not in text
    assert credentials.token not in text and credentials.csrf_token not in text
    assert "token_hash" not in text and "jobs" not in result


def test_trip_deletion_fences_late_commits_detaches_children_and_preserves_other_owner(store):
    owner, source = owned(store)
    child = store.create_owned_session(owner, parent_trip_id=source.id)
    foreign, other = owned(store)
    store.put_memory(owner, "origin", "Larnaca", source.id)
    job, _ = store.admit_message(owner, source.id, "hello", "command-001", 0)
    with pytest.raises(CommandError): store.delete_owned_session(foreign, source.id)
    assert store.delete_owned_session(owner, source.id) == [job.id]
    assert store.get_session(source.id) is None and store.get_job(job.id) is None
    assert store.db.execute("SELECT 1 FROM session_update_versions WHERE session_id=?", (source.id,)).fetchone() is None
    assert store.db.execute("SELECT parent_trip_id FROM session_owners WHERE session_id=?", (child.id,)).fetchone()[0] is None
    assert store.get_owned_session(other.id, foreign) is not None
    assert store.list_memory(owner) == []
    with pytest.raises(StaleCommand): store.save_session(source)
    token = active_command_id.set(job.id)
    try:
        with pytest.raises(StaleCommand): store.save_session(source)
    finally:
        active_command_id.reset(token)


def test_account_deletion_revokes_all_identity_and_owned_data_keeps_global_provider_cache(store):
    user = store.resolve_identity("telegram:fixture", "123")
    source = store.create_owned_session(user.user_id)
    credentials = store.create_access_session(user.user_id)
    foreign, other = owned(store)
    store.put_memory(user.user_id, "origin", "Larnaca", source.id)
    store.cache_put("shared-provider-fact", {"original_time": "2026-01-01"}, 60)
    store.delete_user_data(user.user_id)
    assert store.get_user(user.user_id) is None
    assert store.resolve_access_session(credentials.token) is None
    assert store.get_session(source.id) is None
    assert store.db.execute("SELECT COUNT(*) FROM external_identities WHERE user_id=?", (user.user_id,)).fetchone()[0] == 0
    assert store.db.execute("SELECT COUNT(*) FROM approved_memory WHERE owner_id=?", (user.user_id,)).fetchone()[0] == 0
    assert store.get_owned_session(other.id, foreign) is not None
    assert store.cache_get("shared-provider-fact")[0]["original_time"] == "2026-01-01"
    assert {r[0] for r in store.db.execute("SELECT scope FROM deletion_tombstones WHERE owner_id=?", (user.user_id,))} == {"trip", "user"}


@pytest.mark.asyncio
async def test_delete_running_trip_cancels_worker_and_cannot_resurrect(store):
    owner, source = owned(store)
    job, _ = store.admit_message(owner, source.id, "hello", "command-001", 0)
    entered, canceled = asyncio.Event(), asyncio.Event()
    class Planner:
        async def run(self, job_id):
            entered.set()
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                with pytest.raises(StaleCommand): store.save_session(source)
                canceled.set()
                raise
    worker = CommandWorker(store, Planner()).start()
    await asyncio.wait_for(entered.wait(), 1)
    for job_id in store.delete_owned_session(owner, source.id): worker.cancel(job_id)
    await asyncio.wait_for(canceled.wait(), 1)
    await worker.aclose()
    assert store.get_session(source.id) is None


def test_retention_is_daily_bounded_preserves_registered_trips_and_source_bytes(store):
    # Keep the one-hour repeat on the same UTC calendar day, even when the
    # suite runs after 23:00. The later +1 day call tests the next daily pass.
    now = datetime.now(timezone.utc).replace(hour=12, minute=0, second=0, microsecond=0)
    old = (now-timedelta(days=35)).isoformat()
    guest, expired_trip = owned(store)
    registered, retained = owned(store, "registered")
    credentials = store.create_access_session(guest)
    job, _ = store.admit_message(registered, retained.id, "hello", "command-001", 0)
    job.status = "completed"
    store.save_job(job)
    with store.transaction():
        store.db.execute("UPDATE users SET last_active_at=? WHERE user_id IN (?,?)", (old, guest, registered))
        store.db.execute("UPDATE commands SET finished_at=? WHERE command_id=?", (old, job.id))
        store.db.execute("UPDATE access_sessions SET expires_at=? WHERE user_id=?", ((now-timedelta(days=1)).isoformat(), guest))
        store.db.execute("UPDATE metric_activity SET updated_at=?", ((now-timedelta(days=91)).isoformat(),))
    raw = store.db.execute("SELECT data FROM sessions WHERE id=?", (retained.id,)).fetchone()[0]
    first = store.cleanup_retention(now=now, limit=1)
    assert sum(first.values()) == 1 and first["access_sessions"] == 1
    assert sum(store.cleanup_retention(now=now+timedelta(hours=1), limit=1).values()) == 0
    final = store.cleanup_retention(now=now+timedelta(days=1), limit=100)
    assert final["commands"] == 1 and final["trips"] == 1 and final["users"] == 1 and final["metrics"] >= 1
    assert store.get_user(guest) is None and store.get_session(expired_trip.id) is None
    assert store.get_owned_session(retained.id, registered) is not None
    assert store.db.execute("SELECT data FROM sessions WHERE id=?", (retained.id,)).fetchone()[0] == raw


def test_memory_routes_require_cookie_csrf_and_same_owner_source(store):
    app = FastAPI()
    app.state.store, app.state.identity_resolver = store, None
    install_auth(app)
    install_memory(app)
    @app.exception_handler(ApplicationError)
    async def application_error(request, exc):
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
    @app.exception_handler(CommandError)
    async def command_error(request, exc):
        return JSONResponse({"detail": {"code": exc.code}}, status_code=exc.status_code)
    with TestClient(app, base_url="http://localhost") as client:
        assert client.get("/api/me/memory").status_code == 401
        login = client.post("/api/auth/guest", headers={"origin": "http://localhost"}).json()
        owner = login["user"]["id"]
        source = store.create_owned_session(owner)
        foreign, other = owned(store)
        body = {"value": "Larnaca", "source_trip_id": source.id}
        assert client.put("/api/me/memory/origin", json=body).status_code == 403
        headers = {"origin": "http://localhost", "x-csrf-token": login["csrf_token"]}
        assert client.put("/api/me/memory/origin", json=body, headers=headers).status_code == 200
        assert client.get("/api/me/memory").json()["items"][0]["field"] == "origin"
        body["source_trip_id"] = other.id
        assert client.put("/api/me/memory/origin", json=body, headers=headers).status_code == 404
        assert client.get("/api/me/export").status_code == 200
        assert client.delete("/api/sessions/"+other.id, headers=headers).status_code == 404
        assert client.delete("/api/me/memory/origin", headers=headers).status_code == 204
        assert client.delete("/api/me", headers=headers).status_code == 204
        assert client.get("/api/me/memory").status_code == 401


def test_explicit_pets_memory_retains_species_count_and_qualifiers(store):
    owner, source = owned(store)
    pets = ["two small dogs, each under 8 kg", "one indoor cat; no other animals"]
    saved = store.put_memory(owner, "pets", pets, source.id)
    assert saved["value"] == pets
    child = store.create_owned_session(owner)
    assert store.apply_memory(owner, child) == ["pets"]
    assert child.intent.pets == pets
    with pytest.raises(ValueError): store.put_memory(owner, "pets", ["dog"] * 6, source.id)
