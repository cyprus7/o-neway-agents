"""Persistent command admission/crash/cancellation matrix; no model or network."""
import asyncio
import sqlite3
from types import SimpleNamespace

import pytest

from backend.app.application.commands import CommandError, StaleCommand, active_command_id
from backend.app.infrastructure.command_worker import CommandWorker
from backend.app.models import Message, PriceRefreshInput
from backend.app.storage import Store


@pytest.fixture
def store(tmp_path):
    value = Store(str(tmp_path / "commands.db"))
    yield value
    value.close()


def owned(store, owner=None):
    owner = owner or store.create_user().user_id
    return owner, store.create_owned_session(owner)


def admit(store, owner, session, key="message-0001", **kwargs):
    return store.admit_message(owner, session.id, "Plan a trip", key, session.revision, **kwargs)


def complete(store, job):
    job.status = "completed"
    store.save_job(job)


def test_idempotent_ack_survives_revision_change_but_payload_changes_conflict(store):
    owner, session = owned(store)
    job, dedup = admit(store, owner, session)
    assert not dedup
    stored = store.get_session(session.id)
    stored.revision += 1
    store.save_session(stored)
    repeated, dedup = admit(store, owner, session)
    assert dedup and repeated.id == job.id
    assert len(store.get_session(session.id).messages) == 1
    with pytest.raises(CommandError, match="idempotency_conflict"):
        store.admit_message(owner, session.id, "different", "message-0001", session.revision)
    with pytest.raises(CommandError, match="revision_mismatch"):
        admit(store, owner, session, key="message-0002")
    assert store.db.execute("SELECT COUNT(*) FROM commands").fetchone()[0] == 1


def test_owner_failure_revision_and_key_rejection_have_no_side_effects(store):
    owner, session = owned(store)
    other = store.create_user().user_id
    with pytest.raises(CommandError) as exc:
        admit(store, other, session)
    assert exc.value.status_code == 404
    for key in (None, "short", "x" * 129, "bad\nkeyxxxxxxxx"):
        with pytest.raises(CommandError, match="invalid_idempotency_key"):
            admit(store, owner, session, key=key)
    for revision in (None, True, -1):
        with pytest.raises(CommandError, match="revision_required"):
            store.admit_message(owner, session.id, "hello", "message-0001", revision)
    assert not store.get_session(session.id).messages
    assert store.db.execute("SELECT COUNT(*) FROM commands").fetchone()[0] == 0


def test_per_trip_and_per_user_limits_then_running_plus_two_queued(store):
    owner, first = owned(store)
    others = [owned(store, owner)[1] for _ in range(3)]
    job, _ = admit(store, owner, first)
    with pytest.raises(CommandError, match="session_busy"):
        admit(store, owner, first, key="otherkey-0001")
    admit(store, owner, others[0], key="message-0002")
    with pytest.raises(CommandError, match="user_queue_full"):
        admit(store, owner, others[1], key="message-0003")
    assert store.claim_next_command().id == job.id
    assert store.claim_next_command() is None
    admit(store, owner, others[1], key="message-0003")
    with pytest.raises(CommandError, match="user_queue_full"):
        admit(store, owner, others[2], key="message-0004")


def test_global_queue_limit_32_is_transactional(store):
    for _ in range(32):
        owner, session = owned(store)
        admit(store, owner, session)
    owner, session = owned(store)
    with pytest.raises(CommandError) as exc:
        admit(store, owner, session)
    assert exc.value.code == "global_queue_full" and exc.value.status_code == 429
    assert exc.value.retry_after == 2
    assert not store.get_session(session.id).messages
    assert store.db.execute("SELECT COUNT(*) FROM commands").fetchone()[0] == 32


def test_owner_round_robin_and_fifo_within_owner(store):
    owners = [store.create_user().user_id for _ in range(3)]
    ids = {}
    for owner in owners:
        ids[owner] = [admit(store, owner, owned(store, owner)[1], key=f"message-{i:04}")[0].id for i in range(2)]
    last, observed = None, []
    for _ in range(6):
        job = store.claim_next_command(last)
        owner = store.session_owner(job.session_id)
        observed.append((owner, job.id))
        last = owner
        complete(store, job)
    expected_owners = sorted(owners) * 2
    assert [o for o, _ in observed] == expected_owners
    for owner in owners:
        assert [j for o, j in observed if o == owner] == ids[owner]


def test_recovery_resumes_only_never_started_and_interrupts_running(store):
    owner, first = owned(store)
    queued_session = owned(store, owner)[1]
    running, _ = admit(store, owner, first)
    queued, _ = admit(store, owner, queued_session, key="message-0002")
    assert store.claim_next_command().id == running.id
    _, legacy_session = owned(store)
    legacy = store.start_message(legacy_session.id, "Old unmanaged job")
    store.recover_commands()
    assert store.get_job(running.id).status == "interrupted"
    assert store.get_job(queued.id).status == "queued"
    assert store.get_job(legacy.id).status == "failed"
    store.recover_commands()
    assert store.claim_next_command().id == queued.id
    repeated, dedup = admit(store, owner, first)
    assert repeated.status == "interrupted" and dedup


def test_cancel_is_owner_scoped_idempotent_and_fences_late_session_and_job_writes(store):
    owner, session = owned(store)
    other = store.create_user().user_id
    job, _ = admit(store, owner, session)
    job = store.claim_next_command()
    with pytest.raises(CommandError) as exc:
        store.cancel_command(other, job.id)
    assert exc.value.status_code == 404
    assert store.cancel_command(owner, job.id).status == "canceled"
    assert store.cancel_command(owner, job.id).status == "canceled"
    before = store.db.execute("SELECT data FROM sessions WHERE id=?", (session.id,)).fetchone()[0]
    token = active_command_id.set(job.id)
    try:
        session.messages.append(Message(role="assistant", content="Late result"))
        with pytest.raises(StaleCommand): store.save_session(session)
        with pytest.raises(StaleCommand): store.save_preferences(session)
        job.status = "failed"
        store.save_job(job)  # Cancellation handler is a no-op, not a failed job.
    finally:
        active_command_id.reset(token)
    assert store.get_job(job.id).status == "canceled"
    assert store.db.execute("SELECT data FROM sessions WHERE id=?", (session.id,)).fetchone()[0] == before


def test_deleted_command_and_owner_cannot_publish_late_state(store):
    owner, session = owned(store)
    job, _ = admit(store, owner, session)
    store.claim_next_command()
    with store.transaction():
        store.db.execute("DELETE FROM commands WHERE command_id=?", (job.id,))
    token = active_command_id.set(job.id)
    try:
        with pytest.raises(StaleCommand): store.save_session(session)
    finally:
        active_command_id.reset(token)


@pytest.mark.asyncio
async def test_worker_single_execution_restart_queue_and_terminal_commit(store):
    owner, session = owned(store)
    job, _ = admit(store, owner, session)
    entered, release, done = asyncio.Event(), asyncio.Event(), asyncio.Event()
    calls = []
    class Planner:
        async def run(self, job_id):
            calls.append(job_id)
            entered.set()
            await release.wait()
            complete(store, store.get_job(job_id))
            done.set()
    worker = CommandWorker(store, Planner()).start()
    worker.start()  # No second consumer.
    await asyncio.wait_for(entered.wait(), 1)
    duplicate, dedup = admit(store, owner, session)
    assert dedup and duplicate.id == job.id and calls == [job.id]
    release.set()
    await asyncio.wait_for(done.wait(), 1)
    await worker.aclose()
    assert store.get_job(job.id).status == "completed"
    assert store.db.execute("SELECT status FROM commands WHERE command_id=?", (job.id,)).fetchone()[0] == "completed"


@pytest.mark.asyncio
async def test_worker_cancellation_propagates_and_ignores_late_result(store):
    owner, session = owned(store)
    job, _ = admit(store, owner, session)
    entered, canceled, attempted = asyncio.Event(), asyncio.Event(), asyncio.Event()
    class Planner:
        async def run(self, job_id):
            entered.set()
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                canceled.set()
                with pytest.raises(StaleCommand): store.save_session(session)
                stale = store.get_job(job_id)
                stale.status = "failed"
                store.save_job(stale)
                attempted.set()
                raise
    worker = CommandWorker(store, Planner()).start()
    await asyncio.wait_for(entered.wait(), 1)
    store.cancel_command(owner, job.id)
    worker.cancel(job.id)
    await asyncio.wait_for(attempted.wait(), 1)
    assert canceled.is_set() and store.get_job(job.id).status == "canceled"
    await worker.aclose()


@pytest.mark.asyncio
async def test_shutdown_interruption_is_persisted_queued_survives_and_worker_resumes(store):
    owner, session = owned(store)
    running, _ = admit(store, owner, session)
    entered = asyncio.Event()
    class Slow:
        async def run(self, job_id):
            entered.set()
            await asyncio.sleep(30)
    worker = CommandWorker(store, Slow()).start()
    await asyncio.wait_for(entered.wait(), 1)
    queued_session = owned(store, owner)[1]
    queued, _ = admit(store, owner, queued_session, key="message-0002")
    await worker.aclose()
    assert store.get_job(running.id).status == "interrupted"
    assert store.get_job(queued.id).status == "queued"
    done, calls = asyncio.Event(), []
    class Resume:
        async def run(self, job_id):
            calls.append(job_id)
            complete(store, store.get_job(job_id))
            done.set()
    second = CommandWorker(store, Resume()).start()
    await asyncio.wait_for(done.wait(), 1)
    await second.aclose()
    assert calls == [queued.id]


@pytest.mark.asyncio
async def test_refresh_admission_hash_and_worker_dispatch_use_same_queue(store):
    from backend.tests.test_pricing_core import prepared_session
    owner = store.create_user().user_id
    # Reuse the repository's scoped fixture option, never a live quote.
    session = prepared_session(store)
    store.assign_legacy_trip(session.id, owner)
    body = PriceRefreshInput(expected_revision=session.revision)
    job, dedup = store.admit_refresh(owner, session.id, body, "refresh-0001")
    assert not dedup and job.kind == "prices_refresh"
    same, dedup = store.admit_refresh(owner, session.id, body, "refresh-0001")
    assert dedup and same.id == job.id
    with pytest.raises(CommandError, match="idempotency_conflict"):
        store.admit_refresh(owner, session.id, PriceRefreshInput(expected_revision=session.revision, option_id=session.options[0].id), "refresh-0001")
    done, calls = asyncio.Event(), []
    class Planner:
        async def refresh_prices(self, job_id):
            calls.append(job_id)
            complete(store, store.get_job(job_id))
            done.set()
        async def run(self, job_id):
            raise AssertionError("Wrong command branch")
    worker = CommandWorker(store, Planner()).start()
    await asyncio.wait_for(done.wait(), 1)
    await worker.aclose()
    assert calls == [job.id]


def test_explicit_action_option_ids_are_persisted_and_hash_bound(store):
    owner, session = owned(store)
    job, _ = admit(store, owner, session, action="compare", option_ids=["option-a", "option-b"])
    assert job.payload["action"] == "compare" and job.payload["option_ids"] == ["option-a", "option-b"]
    with pytest.raises(CommandError, match="idempotency_conflict"):
        admit(store, owner, session, action="explain", option_ids=["option-a", "option-b"])
    with pytest.raises(CommandError, match="idempotency_conflict"):
        admit(store, owner, session, action="compare", option_ids=["other"])


def test_persisted_never_started_command_survives_store_reopen(tmp_path):
    path = str(tmp_path / "restart.db")
    store = Store(path)
    owner, session = owned(store)
    job, _ = admit(store, owner, session)
    store.close()
    store = Store(path)
    store.recover_commands()
    assert store.get_job(job.id).status == "queued"
    assert store.claim_next_command().id == job.id
    store.close()
