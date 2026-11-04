"""Refresh activity attribution: synthetic state only, no provider/model dispatch."""
import asyncio
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from backend.app.application.session_views import SessionViews
from backend.app.infrastructure.session_notifications import SessionNotifications
from backend.app.models import Job
from backend.app.storage import Store
from backend.app.transport.session_events import session_events
from backend.tests.factories import create_app
from backend.tests.test_pricing_core import prepared_session


@pytest.mark.parametrize("all_options", [False, True])
def test_scope_survives_reload_and_attributes_only_refreshed_result_ids(tmp_path, all_options):
    path = str(tmp_path / "scope.db")
    store = Store(path)
    session = prepared_session(store, option_count=3)
    old_ids = [option.id for option in session.options]
    targets = old_ids if all_options else old_ids[1:2]
    job, _ = store.start_refresh(session.id, 1, None if all_options else targets[0])
    assert job.target_option_ids == targets and job.result_option_ids == []
    store.close()

    store = Store(path)
    job = store.get_job(job.id)
    assert store.read_session_snapshot(session.id).job["target_option_ids"] == targets
    replacements = [option.model_copy(deep=True) for option in session.options if option.id in targets]
    assert store.apply_refresh(job, replacements, [])
    snapshot = store.read_session_snapshot(session.id)
    new_ids = [option.id for option in snapshot.session.options]
    expected = new_ids if all_options else new_ids[1:2]
    assert snapshot.job["result_revision"] == snapshot.session.revision == 2
    assert snapshot.job["target_option_ids"] == targets
    assert snapshot.job["result_option_ids"] == expected
    assert not set(new_ids) & set(old_ids)
    assert len(snapshot.job["result_option_ids"]) == len(targets)
    store.close()


@pytest.mark.parametrize("expired", [False, True])
def test_unapplied_job_does_not_claim_any_result_scope(tmp_path, expired):
    store = Store(str(tmp_path / "unapplied.db"))
    session = prepared_session(store)
    job, _ = store.start_refresh(session.id, 1)
    if expired:
        job.deadline_at = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        store.save_job(job)
    else:
        session.revision += 1
        store.save_session(session)
    assert not store.apply_refresh(job, session.options, [])
    result = store.get_job(job.id)
    assert result.status == ("failed" if expired else "superseded")
    assert result.target_option_ids == [session.options[0].id]
    assert result.result_option_ids == [] and result.result_revision is None
    store.close()


def test_old_jobs_and_message_jobs_have_no_invented_scope_and_scope_is_bounded():
    legacy = Job.model_validate({"session_id": "legacy", "kind": "prices_refresh"})
    assert legacy.target_option_ids == legacy.result_option_ids == []
    for field in ("target_option_ids", "result_option_ids"):
        with pytest.raises(ValidationError):
            Job(session_id="bounded", **{field: ["a", "b", "c", "d"]})


def test_interrupted_refresh_keeps_target_and_offers_the_matching_retry(tmp_path):
    store = Store(str(tmp_path / "interrupted.db"))
    session = prepared_session(store)
    job, _ = store.start_refresh(session.id, 1)
    store.recover_interrupted()
    recovered = store.read_session_snapshot(session.id).job
    assert recovered["id"] == job.id and recovered["status"] == "failed"
    assert recovered["target_option_ids"] == [session.options[0].id]
    assert recovered["result_option_ids"] == []
    assert "Повторите проверку цен" in recovered["error"]
    assert store.get_session(session.id).revision == 1
    store.close()


def test_get_job_exposes_scope_but_keeps_private_context_hidden(tmp_path):
    app = create_app(database_path=str(tmp_path / "public.db"), transport=httpx.MockTransport(lambda _: httpx.Response(503)))
    with TestClient(app) as client:
        session = prepared_session(app.state.store)
        job, _ = app.state.store.start_refresh(session.id, 1)
        public = client.get(f"/api/jobs/{job.id}").json()
        assert public["target_option_ids"] == [session.options[0].id]
        assert public["result_option_ids"] == []
        assert "payload" not in public and "request_hash" not in json.dumps(public)


def test_two_streams_and_reconnect_receive_atomic_scope_without_private_payload(tmp_path):
    async def scenario():
        notifications = SessionNotifications()
        store = Store(str(tmp_path / "stream.db"), on_change=notifications.notify)
        session = prepared_session(store, option_count=2)
        streams = [session_events(session.id, SessionViews(store), notifications) for _ in range(2)]

        async def snapshot(stream):
            frame = await asyncio.wait_for(anext(stream), 1)
            return json.loads(next(line[6:] for line in frame.splitlines() if line.startswith("data: ")))

        for stream in streams:
            await anext(stream)  # retry directive
            await snapshot(stream)
        job, _ = store.start_refresh(session.id, 1, session.options[1].id)
        for stream in streams:
            event = await snapshot(stream)
            assert event["job"]["target_option_ids"] == [session.options[1].id]
            assert "payload" not in event["job"]
        assert store.apply_refresh(job, [session.options[1].model_copy(deep=True)], [])
        for stream in streams:
            event = await snapshot(stream)
            assert event["job"]["result_option_ids"] == [event["session"]["options"][1]["id"]]
            assert event["job"]["result_revision"] == event["session"]["revision"] == 2
            assert "request_hash" not in json.dumps(event["job"])
        for stream in streams:
            await stream.aclose()
        reconnect = session_events(session.id, SessionViews(store), notifications)
        await anext(reconnect)
        event = await snapshot(reconnect)
        assert event["job"]["result_option_ids"] == [event["session"]["options"][1]["id"]]
        await reconnect.aclose()
        assert notifications.subscriber_count == 0
        store.close()

    asyncio.run(scenario())
