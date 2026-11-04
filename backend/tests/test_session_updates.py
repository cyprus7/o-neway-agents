"""Deterministic session delivery tests; no inference or provider requests."""
import asyncio
import sqlite3
import threading
from datetime import datetime, timedelta, timezone

import pytest

from backend.app.application.session_views import SessionViews, public_session_data
from backend.app.infrastructure.session_notifications import SessionNotifications, SubscriptionClosed
from backend.app.storage import Store, TransactionAborted
from backend.tests.test_pricing_core import prepared_session


@pytest.fixture
def tracked_store(tmp_path):
    changed = []
    store = Store(str(tmp_path / "live.db"), on_change=changed.append)
    yield store, changed
    store.close()


def test_outer_commit_advances_once_and_notifies_only_durable_complete_state(tmp_path):
    observed = []
    path = str(tmp_path / "commit.db")
    store = Store(path)
    session = store.create_session()
    original = store.read_session_snapshot(session.id)

    def committed(session_id):
        with sqlite3.connect(path) as reader:
            data = reader.execute("SELECT data FROM sessions WHERE id=?", (session_id,)).fetchone()[0]
            sequence = reader.execute("SELECT sequence FROM session_update_versions WHERE session_id=?", (session_id,)).fetchone()[0]
        observed.append((data, sequence))

    store._on_change = committed
    with store.transaction():
        session.clarification = "Committed together"
        store.save_session(session)
        store.add_event(session.id, "test", "completed", "Nested event")
        store.increment("test_metric")
        assert observed == []
    assert len(observed) == 1
    assert observed[0][1] == original.sequence + 1
    assert "Committed together" in observed[0][0] and "Nested event" in observed[0][0]
    store.close()


def test_nested_failure_rolls_back_session_job_cursor_and_wakeup(tracked_store):
    store, changed = tracked_store
    session = store.create_session()
    before = store.read_session_snapshot(session.id)
    changed.clear()
    with pytest.raises(RuntimeError, match="abort"):
        with store.transaction():
            store.start_message(session.id, "Must roll back")
            store.increment("must_roll_back")
            raise RuntimeError("abort")
    after = store.read_session_snapshot(session.id)
    assert after == before and changed == []
    assert "must_roll_back" not in store.metrics()


def test_caught_nested_error_still_aborts_complete_unit_of_work(tracked_store):
    store, changed = tracked_store
    session = store.create_session()
    before = store.read_session_snapshot(session.id)
    changed.clear()
    with pytest.raises(TransactionAborted):
        with store.transaction():
            store.add_event(session.id, "test", "running", "Never commit")
            try:
                with store.transaction():
                    raise ValueError("Failed nested domain operation")
            except ValueError:
                pass
    assert store.read_session_snapshot(session.id) == before and changed == []


def test_queued_and_running_jobs_advance_cursor_without_itinerary_revision(tracked_store):
    store, changed = tracked_store
    session = store.create_session()
    before = store.read_session_snapshot(session.id)
    changed.clear()
    job = store.start_message(session.id, "Synthetic request")
    queued = store.read_session_snapshot(session.id)
    assert queued.sequence == before.sequence + 1 and queued.session.revision == before.session.revision
    assert queued.job["id"] == job.id and queued.job["status"] == "queued"
    assert len(queued.session.messages) == 1 and "payload" not in queued.job
    assert changed == [session.id]
    job.status, job.progress = "running", "Working"
    store.save_job(job)
    running = store.read_session_snapshot(session.id)
    assert running.sequence == queued.sequence + 1
    assert running.session.revision == queued.session.revision and running.job["progress"] == "Working"


@pytest.mark.parametrize("outcome", ["completed", "failed", "superseded"])
def test_refresh_terminal_outcomes_are_atomic_and_observable(tracked_store, outcome):
    store, changed = tracked_store
    session = prepared_session(store)
    job, _ = store.start_refresh(session.id, session.revision)
    if outcome == "failed":
        job.deadline_at = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        store.save_job(job)
    elif outcome == "superseded":
        session.intent.adults += 1
        store.save_session(session)
    before = store.read_session_snapshot(session.id)
    changed.clear()
    applied = store.apply_refresh(job, session.options, [])
    after = store.read_session_snapshot(session.id)
    assert applied == (outcome == "completed")
    assert after.sequence == before.sequence + 1 and changed == [session.id]
    assert after.job["status"] == outcome
    assert after.session.revision == before.session.revision + (outcome == "completed")
    assert "payload" not in after.job
    if applied:
        assert all(option.id.startswith(f"r{after.session.revision}-") for option in after.session.options)
        assert after.job["result_revision"] == after.session.revision
    store.apply_refresh(job, session.options, [])
    assert store.read_session_snapshot(session.id).sequence == after.sequence


def test_deduplicated_refresh_and_nonobservable_cache_do_not_advance_cursor(tracked_store):
    store, changed = tracked_store
    session = prepared_session(store)
    job, _ = store.start_refresh(session.id, session.revision)
    before = store.read_session_snapshot(session.id)
    changed.clear()
    duplicate, deduplicated = store.start_refresh(session.id, session.revision)
    store.cache_put("test", {"cached": True}, 60)
    store.increment("test")
    assert deduplicated and duplicate.id == job.id
    assert store.read_session_snapshot(session.id) == before and changed == []


def test_incomplete_refresh_cannot_publish_partial_change(tracked_store):
    store, changed = tracked_store
    session = prepared_session(store, option_count=2)
    job, _ = store.start_refresh(session.id, session.revision)
    before = store.read_session_snapshot(session.id)
    changed.clear()
    with pytest.raises(ValueError, match="Incomplete"):
        store.apply_refresh(job, session.options[:1], [])
    assert store.read_session_snapshot(session.id) == before and changed == []


def test_latest_job_is_deterministic_on_timestamp_tie_and_prefers_active(tracked_store):
    store, _ = tracked_store
    session = store.create_session()
    first = store.start_message(session.id, "First")
    first.status = "completed"
    store.save_job(first)
    latest = store.start_message(session.id, "Second")
    latest.created_at = first.created_at
    latest.status = "completed"
    store.save_job(latest)
    assert store.read_session_snapshot(session.id).job["id"] == latest.id
    first.status = "running"
    store.save_job(first)
    assert store.read_session_snapshot(session.id).job["id"] == first.id


def test_restart_preserves_cursor_and_recovers_active_job_in_one_change(tmp_path):
    path = str(tmp_path / "restart.db")
    store = Store(path)
    session = store.create_session()
    store.start_message(session.id, "Interrupted")
    before = store.read_session_snapshot(session.id)
    store.close()
    changed = []
    store = Store(path, on_change=changed.append)
    assert store.read_session_snapshot(session.id) == before
    store.recover_interrupted()
    after = store.read_session_snapshot(session.id)
    assert after.sequence == before.sequence + 1 and after.job["status"] == "failed"
    assert after.session.events[-1].step == "recovery" and changed == [session.id]
    store.close()


def test_existing_sessions_without_version_rows_start_at_zero(tmp_path):
    path = str(tmp_path / "migration.db")
    store = Store(path)
    session = store.create_session()
    with store.db:
        store.db.execute("DELETE FROM session_update_versions")
    store.close()
    store = Store(path)
    assert store.read_session_snapshot(session.id).sequence == 0
    store.save_session(session)
    assert store.read_session_snapshot(session.id).sequence == 1
    store.close()


def test_atomic_snapshot_cannot_mix_session_and_job_across_another_connection_commit(tmp_path):
    path = str(tmp_path / "atomic.db")
    writer = Store(path)
    session = writer.create_session()
    job = writer.start_message(session.id, "Atomic fixture")
    job.status = "completed"
    job.result_revision = session.revision
    writer.save_job(job)
    reader = Store(path)
    before = reader.read_session_snapshot(session.id)
    halfway, release = threading.Event(), threading.Event()
    errors = []

    def commit_in_thread():
        try:
            with writer.transaction():
                session.revision += 1
                writer.save_session(session)
                halfway.set()
                assert release.wait(2)
                job.result_revision = session.revision
                writer.save_job(job)
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=commit_in_thread)
    thread.start()
    try:
        assert halfway.wait(2)
        # WAL reader sees the prior committed pair while the writer is halfway.
        assert reader.read_session_snapshot(session.id) == before
    finally:
        release.set()
        thread.join(2)
    assert not thread.is_alive() and errors == []
    after = reader.read_session_snapshot(session.id)
    assert after.sequence == before.sequence + 1
    assert after.session.revision == after.job["result_revision"] == session.revision
    reader.close()
    writer.close()


def test_failed_wakeup_does_not_turn_committed_command_into_retry(tracked_store, caplog):
    store, _ = tracked_store
    session = store.create_session()
    before = store.read_session_snapshot(session.id)

    def broken_notification(_):
        raise RuntimeError("Closed notification adapter")

    store._on_change = broken_notification
    job = store.start_message(session.id, "Already committed")
    after = store.read_session_snapshot(session.id)
    assert after.sequence == before.sequence + 1 and after.job["id"] == job.id
    assert "Committed session notification failed" in caplog.text


@pytest.mark.asyncio
async def test_subscribe_before_initial_read_cannot_lose_commit_and_isolates_sessions(tmp_path):
    hub = SessionNotifications()
    store = Store(str(tmp_path / "race.db"), on_change=hub.notify)
    session, other = store.create_session(), store.create_session()
    subscription = hub.subscribe(session.id)
    isolated = hub.subscribe(other.id)
    session.clarification = "Committed in subscription/read gap"
    await asyncio.to_thread(store.save_session, session)
    initial = store.read_session_snapshot(session.id)
    assert initial.session.clarification == session.clarification
    await asyncio.wait_for(subscription.wait(), timeout=1)
    assert store.read_session_snapshot(session.id).sequence == initial.sequence
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(isolated.wait(), timeout=0.02)
    subscription.close()
    isolated.close()
    assert hub.subscriber_count == 0
    hub.close()
    store.close()


@pytest.mark.asyncio
async def test_slow_reader_coalesces_wakes_including_cross_thread_scheduling():
    hub = SessionNotifications()
    subscription = hub.subscribe("trip")
    def burst():
        for _ in range(10_000):
            hub.notify("trip")
    await asyncio.to_thread(burst)
    await asyncio.wait_for(subscription.wait(), timeout=1)
    assert subscription._queue.qsize() == 0
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(subscription.wait(), timeout=0.02)
    hub.notify("trip")
    await asyncio.wait_for(subscription.wait(), timeout=1)
    subscription.close()
    assert hub.subscriber_count == 0


@pytest.mark.asyncio
async def test_cancel_and_close_release_subscriptions_and_wake_pending_waits():
    hub = SessionNotifications()
    subscription = hub.subscribe("trip")
    waiter = asyncio.create_task(subscription.wait())
    await asyncio.sleep(0)
    hub.close()
    with pytest.raises(SubscriptionClosed):
        await waiter
    subscription.close()
    assert hub.subscriber_count == 0
    with pytest.raises(SubscriptionClosed):
        hub.subscribe("trip")


def test_projection_expires_at_same_cursor_preserves_observations_and_has_no_writes(tracked_store):
    store, changed = tracked_store
    session = prepared_session(store)
    clock = datetime.now(timezone.utc)
    expiry = clock + timedelta(seconds=5)
    session.options[0].flight.quote = session.options[0].flight.quote.model_copy(update={"expires_at": expiry})
    store.save_session(session)
    before = store.read_session_snapshot(session.id)
    changed.clear()
    views = SessionViews(store)
    live = views.read(session.id, reason="connected", now=clock)
    expired = views.read(session.id, reason="freshness", now=expiry)
    assert live.next_expiry == expiry
    assert live.sequence == expired.sequence == before.sequence
    assert live.session == public_session_data(session, now=clock)
    original_quote = live.session["options"][0]["flight"]["quote"]
    stale_quote = expired.session["options"][0]["flight"]["quote"]
    assert original_quote["freshness"] == "live" and stale_quote["freshness"] == "stale"
    for key in ("id", "value", "observed_at", "retrieved_at", "expires_at"):
        assert stale_quote[key] == original_quote[key]
    assert expired.session["options"][0]["flight"]["price"] is None
    assert expired.session["options"][0]["pricing_summary"]["hard_constraint_status"] == "unknown"
    assert store.read_session_snapshot(session.id) == before and changed == []
    assert "next_expiry" not in expired.to_dict()
    assert views.read("missing") is None
