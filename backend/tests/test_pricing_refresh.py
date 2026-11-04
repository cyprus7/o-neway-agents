"""Refresh/state tests use synthetic providers and never the inference service."""
import asyncio
import time
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

from backend.app.geography import find_place
from backend.tests.factories import create_app
from backend.app.models import IntentExtraction, TravelComponent, TravelIntent
from backend.app.planner import Planner
from backend.app.pricing.normalization import request_context_hash
from backend.app.pricing.providers import KeylessPricing, context_for, unknown
from backend.app.storage import BusySession, Store
from backend.tests.test_pricing_core import prepared_session, published_reference


class FakePricing:
    def __init__(self):
        self.calls = 0

    async def apply(self, place, origin, intent, components, refresh=False):
        assert refresh
        self.calls += 1
        components[0].quote = published_reference()

    def capabilities(self):
        return {"refresh_enabled": True, "providers": [{"id": "synthetic", "configured": True}]}


def poll(client, job_id):
    for _ in range(100):
        result = client.get("/api/jobs/" + job_id).json()
        if result["status"] not in ("queued", "running"):
            return result
        time.sleep(0.005)
    raise AssertionError("Synthetic refresh did not finish")


def test_one_active_job_deduplicates_refresh_and_blocks_message_and_other_targets(tmp_path):
    store = Store(str(tmp_path / "jobs.db"))
    session = prepared_session(store, option_count=2)
    first, deduplicated = store.start_refresh(session.id, 1, session.options[0].id)
    assert not deduplicated
    repeated, deduplicated = store.start_refresh(session.id, 1, session.options[0].id)
    assert repeated.id == first.id and deduplicated
    with pytest.raises(BusySession) as active:
        store.start_message(session.id, "Change dates")
    assert active.value.job_id == first.id
    with pytest.raises(BusySession):
        store.start_refresh(session.id, 1, session.options[1].id)
    assert store.get_session(session.id).options
    store.close()


@pytest.mark.parametrize("expected_revision,option_id", [(0, None), (2, None), (1, "missing")])
def test_stale_revision_or_option_cannot_start_refresh(tmp_path, expected_revision, option_id):
    store = Store(str(tmp_path / "invalid.db"))
    session = prepared_session(store)
    with pytest.raises(ValueError):
        store.start_refresh(session.id, expected_revision, option_id)
    assert not store.active_job(session.id)
    store.close()


def test_atomic_apply_changes_all_ids_and_selection_even_for_single_target(tmp_path):
    store = Store(str(tmp_path / "apply.db"))
    session = prepared_session(store, option_count=2)
    session.selected_option_id = session.options[0].id
    store.save_session(session)
    first_id, other_id = [option.id for option in session.options]
    job, _ = store.start_refresh(session.id, 1, first_id)
    changed = session.options[0].model_copy(deep=True)
    changed.flight.quote = published_reference()
    assert store.apply_refresh(job, [changed], [{"component": "flight", "outcome": "reference"}])
    current = store.get_session(session.id)
    assert current.revision == 2 and current.selected_option_id is None
    assert all(option.id.startswith("r2-") for option in current.options)
    assert all(option.id not in (first_id, other_id) for option in current.options)
    assert store.get_job(job.id).status == "completed"
    assert store.get_job(job.id).result_revision == 2
    assert not store.apply_refresh(job, [changed], [])
    assert store.get_job(job.id).status == "completed"
    store.close()


@pytest.mark.parametrize("change_revision", [True, False])
def test_late_result_is_superseded_on_revision_or_request_context_change(tmp_path, change_revision):
    store = Store(str(tmp_path / "late.db"))
    session = prepared_session(store)
    job, _ = store.start_refresh(session.id, 1)
    newer = session.model_copy(deep=True)
    if change_revision:
        newer.revision = 2
    else:
        newer.intent.adults = 2
    store.save_session(newer)
    assert not store.apply_refresh(job, session.options, [])
    persisted = store.get_session(session.id)
    assert persisted.revision == newer.revision and persisted.intent.adults == newer.intent.adults
    assert store.get_job(job.id).status == "superseded"
    store.close()


def test_http_refresh_uses_no_generation_calls_and_public_job_has_no_internal_snapshot(tmp_path):
    app = create_app(database_path=str(tmp_path / "http.db"), transport=httpx.MockTransport(lambda _: httpx.Response(503)))
    with TestClient(app) as client:
        session = prepared_session(app.state.store)
        pricing = FakePricing()
        app.state.travel_data.providers.pricing = pricing

        async def forbidden(*args):
            raise AssertionError("Refresh must not call LLM")
        app.state.llm.extract = forbidden
        response = client.post(f"/api/sessions/{session.id}/prices/refresh", json={"expected_revision": 1}, headers={"Idempotency-Key": "refresh-http-0001"})
        assert response.status_code == 202
        job = poll(client, response.json()["job_id"])
        assert job["status"] == "completed" and job["kind"] == "prices_refresh" and "payload" not in job
        assert pricing.calls == 1
        current = client.get(f"/api/sessions/{session.id}").json()
        assert current["revision"] == 2 and current["options"][0]["flight"]["quote"]["outcome"] == "reference"
        assert client.get("/api/config").json()["pricing"]["refresh_enabled"]
        assert client.post(f"/api/sessions/{session.id}/prices/refresh", json={"expected_revision": 2, "url": "http://localhost"}, headers={"Idempotency-Key": "refresh-invalid-0001"}).status_code == 422


def test_expired_queue_deadline_does_not_start_source_or_generation_calls(tmp_path):
    store = Store(str(tmp_path / "deadline.db"))
    session = prepared_session(store)
    job, _ = store.start_refresh(session.id, 1)
    job.deadline_at = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    store.save_job(job)
    pricing = FakePricing()

    class Providers:
        async def resolve_place(self, name):
            raise AssertionError("Expired queue must not perform network work")

    providers = Providers()
    providers.pricing = pricing
    planner = Planner(store, None, providers)
    asyncio.run(planner.refresh_prices(job.id))
    assert store.get_job(job.id).status == "failed" and pricing.calls == 0
    assert store.get_session(session.id).revision == 1
    store.close()


def test_deadline_also_covers_atomic_apply_and_cannot_publish_late_numbers(tmp_path):
    store = Store(str(tmp_path / "apply-deadline.db"))
    session = prepared_session(store)
    job, _ = store.start_refresh(session.id, 1)
    job.deadline_at = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    store.save_job(job)
    assert not store.apply_refresh(job, session.options, [])
    assert store.get_session(session.id).revision == 1
    assert store.get_job(job.id).status == "failed"
    store.close()


def test_budget_only_replan_reuses_compatible_quotes_without_external_search(tmp_path):
    store = Store(str(tmp_path / "budget.db"))
    session = prepared_session(store)
    original = session.options[0].flight.quote
    job = store.start_message(session.id, "Теперь бюджет 100 евро")
    # Admission keeps the readable accepted plan; active work blocks checkout.
    admitted = store.get_session(session.id)
    assert admitted.options == session.options and store.active_job(session.id)
    assert admitted.selected_option_id == session.selected_option_id

    class LLM:
        async def extract(self, current):
            intent = TravelIntent(**{**current.intent.model_dump(), "budget_total": 100})
            return IntentExtraction(state_patch={"budget_total": 100}), intent, ["budget_total"]

    class NoProviders:
        def __getattr__(self, name):
            raise AssertionError("Budget-only replan must not call providers: " + name)

    asyncio.run(Planner(store, LLM(), NoProviders()).run(job.id))
    current = store.get_session(session.id)
    assert store.get_job(job.id).status == "completed" and current.revision == 2
    assert current.options[0].flight.quote == original
    assert current.options[0].pricing_summary.hard_constraint_status == "failed"
    assert current.options[0].id.startswith("r2-")
    assert store.metrics()["pricing_budget_reuses"] == 1
    store.close()


def _overload_trip(*, binding="fixed", expired=False, change=None):
    place, origin = find_place("Athens"), find_place("Larnaca")
    intent = TravelIntent(origin=origin.name, destination=place.name, date_from="2026-10-30",
        date_to="2026-11-01", adults=1, currency="EUR")
    entity = "google-hotel:" + "a" * 24
    prior_context = context_for("stay", place, origin, intent, entity_id=entity if binding == "fixed" else None)
    quote = published_reference("stay", expired=expired)
    quote = quote.model_copy(update={"provider_id":"google_hotels",
        "scope":quote.scope.model_copy(update={"entity_id":entity, "destination_city":place.name}),
        "request_context_hash":request_context_hash(prior_context)})
    if change == "dates":
        intent.date_to += timedelta(days=1)
    elif change == "party":
        intent.adults = 2
    elif change == "entity":
        entity = "google-hotel:" + "b" * 24
    parts = [TravelComponent(title="Synthetic train", description="Fixture", details={"transport_mode":"train"}),
        TravelComponent(title="Synthetic selected hotel", description="Fixture", quote=quote, status="estimated",
            source_url=quote.source_url, details={"pricing_entity_id":entity, "pricing_delivery":"live_fetch",
                "pricing_observation_delivery":"live_fetch", "pricing_refresh_warning":"Previous warning"}),
        TravelComponent(title="Synthetic transfer", description="Fixture")]
    return place, origin, intent, parts, quote


@pytest.mark.parametrize("binding,expired,change,retain", [
    ("fixed", False, None, True), ("candidate", False, None, True), ("candidate", True, None, True),
    ("fixed", False, "dates", False), ("candidate", False, "party", False), ("candidate", False, "entity", False),
])
def test_overloaded_refresh_preserves_only_compatible_previous_observation_without_source_work(
        tmp_path, monkeypatch, binding, expired, change, retain):
    monkeypatch.setenv("KEYLESS_PRICING_ENABLED", "1")
    monkeypatch.setenv("WEB_FALLBACK_ENABLED", "1")

    async def scenario():
        store = Store(str(tmp_path / "overload.db"))
        requests, lookups = [], []
        release = asyncio.Event()
        active = []
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: requests.append(request))) as client:
            router = KeylessPricing(client, store)
            async def forbidden(*args, **kwargs):
                lookups.append(args)
                raise AssertionError("An overloaded new lookup must not start")
            router._find = forbidden
            place, origin, intent, parts, previous = _overload_trip(binding=binding, expired=expired, change=change)
            previous_json = previous.model_dump_json()
            store.cache_put("overload-fixture", {"retrieved_at":previous.retrieved_at.isoformat()}, 600)
            cache_before = store.db.execute("SELECT * FROM cache").fetchall()
            try:
                for index in range(16):
                    task = asyncio.create_task(release.wait())
                    active.append(task)
                    router._inflight[f"busy-{index}"] = task
                    router._waiters[task] = 1
                await asyncio.sleep(0)
                inflight_before, waiters_before = dict(router._inflight), dict(router._waiters)
                await router.apply(place, origin, intent, parts, refresh=True)
                stay = parts[1]
                if retain:
                    assert stay.quote is previous and stay.quote.model_dump_json() == previous_json
                    assert stay.details["pricing_refresh_warning"] != "Previous warning"
                    assert stay.details["pricing_delivery"] == stay.details["pricing_observation_delivery"] == "cache_reuse"
                    assert stay.evidence[0].retrieved_at == previous.retrieved_at.isoformat()
                    assert stay.evidence[0].freshness == "cached"
                    assert stay.quote.freshness() == ("stale" if expired else "live")
                else:
                    assert stay.quote.value.kind == "unknown" and stay.quote.outcome == "timeout"
                    assert stay.quote.request_context_hash != previous.request_context_hash
                    assert "pricing_refresh_warning" not in stay.details and "pricing_observation_delivery" not in stay.details
                    assert stay.details["pricing_delivery"] == "not_attempted"
                    assert stay.price is None and stay.status == "unavailable"
                diagnostic, = stay.details["pricing_outcomes"]
                assert diagnostic["diagnostic_code"] == "source_overloaded"
                assert diagnostic["delivery"] == "not_attempted" and diagnostic["dispatched"] is False
                assert not requests and not lookups
                assert store.db.execute("SELECT * FROM cache").fetchall() == cache_before
                assert router._inflight == inflight_before and router._waiters == waiters_before
                assert all(not task.done() for task in active)
                assert not any(name.endswith("_requests") for name in store.metrics())
            finally:
                release.set()
                await asyncio.gather(*active, return_exceptions=True)
                store.close()
    asyncio.run(scenario())


def test_full_source_pool_still_shares_an_existing_refresh_lookup(tmp_path, monkeypatch):
    monkeypatch.setenv("KEYLESS_PRICING_ENABLED", "1")
    monkeypatch.setenv("WEB_FALLBACK_ENABLED", "1")

    async def scenario():
        store = Store(str(tmp_path / "deduplicated-overload.db"))
        requests, lookups = [], []
        release, started, unrelated_release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        callers, unrelated = [], []
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: requests.append(request))) as client:
            router = KeylessPricing(client, store)
            async def delayed(component, context, *args, **kwargs):
                lookups.append(component)
                started.set()
                await release.wait()
                return unknown("public_stay", context, "Synthetic unavailable source", "timeout"), [], None
            router._find = delayed
            place, origin, intent, first, previous = _overload_trip()
            second = [part.model_copy(deep=True) for part in first]
            original = previous.model_dump_json()
            store.cache_put("singleflight-fixture", {"retrieved_at":previous.retrieved_at.isoformat()}, 600)
            cache_before = store.db.execute("SELECT * FROM cache").fetchall()
            try:
                callers.append(asyncio.create_task(router.apply(place, origin, intent, first, refresh=True)))
                await asyncio.wait_for(started.wait(), 1)
                source_task, = router._inflight.values()
                for index in range(15):
                    task = asyncio.create_task(unrelated_release.wait())
                    unrelated.append(task)
                    router._inflight[f"other-{index}"] = task
                    router._waiters[task] = 1
                callers.append(asyncio.create_task(router.apply(place, origin, intent, second, refresh=True)))
                async with asyncio.timeout(1):
                    while router._waiters[source_task] != 2:
                        await asyncio.sleep(0)
                assert len(router._inflight) == 16 and lookups == ["stay"]
                release.set()
                await asyncio.gather(*callers)
                assert store.metrics()["pricing_dedup_hits"] == 1
                for parts in (first, second):
                    assert parts[1].quote.model_dump_json() == original
                    assert parts[1].details["pricing_refresh_warning"]
                    assert parts[1].details["pricing_observation_delivery"] == "cache_reuse"
                assert source_task not in router._waiters and source_task not in router._inflight.values()
                assert len(router._inflight) == len(router._waiters) == 15
                assert all(not task.done() for task in unrelated)
                assert not requests and store.db.execute("SELECT * FROM cache").fetchall() == cache_before
            finally:
                release.set()
                unrelated_release.set()
                await asyncio.gather(*callers, *unrelated, return_exceptions=True)
                store.close()
    asyncio.run(scenario())
