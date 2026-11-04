"""SSE transport contracts using synthetic quotes; no external services."""
import asyncio
import json
import socket
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import httpx
import uvicorn
from fastapi.testclient import TestClient
from fastapi.testclient import TestClient

from backend.app.application.session_views import SessionViews
from backend.app.infrastructure.session_notifications import SessionNotifications
from backend.tests.factories import create_app
from backend.app.storage import Store
from backend.app.transport.session_events import session_events
from backend.tests.test_pricing_core import prepared_session, scoped_quote


def data(frame):
    return json.loads(next(line[6:] for line in frame.splitlines() if line.startswith("data: ")))


def test_stream_current_snapshot_coalesces_and_cleans_up(tmp_path):
    async def scenario():
        notifications = SessionNotifications()
        store = Store(str(tmp_path / "stream.db"), on_change=notifications.notify)
        session = prepared_session(store)
        stream = session_events(session.id, SessionViews(store), notifications)
        assert await anext(stream) == "retry: 2000\n\n"
        first = data(await anext(stream))
        assert first["reason"] == "connected"
        assert first["session"]["revision"] == 1
        for number in range(30):
            session.last_changes = [f"change-{number}"]
            store.save_session(session)
        updated = data(await asyncio.wait_for(anext(stream), 1))
        assert updated["session"]["last_changes"] == ["change-29"]
        assert updated["sequence"] > first["sequence"]
        assert updated["session"]["revision"] == 1
        assert notifications.subscriber_count == 1
        await stream.aclose()
        assert notifications.subscriber_count == 0
        store.close()
    asyncio.run(scenario())


def test_named_heartbeat_and_disconnect_cancel_release_subscription(tmp_path):
    async def scenario():
        notifications = SessionNotifications()
        store = Store(str(tmp_path / "heartbeat.db"), on_change=notifications.notify)
        session = store.create_session()
        stream = session_events(session.id, SessionViews(store), notifications, heartbeat_seconds=0.02)
        await anext(stream)
        await anext(stream)
        heartbeat = await asyncio.wait_for(anext(stream), 1)
        assert ": heartbeat\n" in heartbeat and "event: heartbeat\n" in heartbeat
        pending = asyncio.create_task(anext(stream))
        await asyncio.sleep(0)
        pending.cancel()
        try:
            await pending
        except asyncio.CancelledError:
            pass
        assert notifications.subscriber_count == 0
        store.close()
    asyncio.run(scenario())


def test_idle_expiry_sends_equal_cursor_without_writes_or_new_observations(tmp_path):
    async def scenario():
        notifications = SessionNotifications()
        store = Store(str(tmp_path / "expiry.db"), on_change=notifications.notify)
        session = prepared_session(store, complete=True)
        session.options[0].flight.quote = scoped_quote("flight", session.intent, expires_at=datetime.now(timezone.utc) + timedelta(seconds=0.2))
        store.save_session(session)
        before = store.get_session(session.id).model_dump_json()
        stream = session_events(session.id, SessionViews(store), notifications)
        await anext(stream)
        first = data(await anext(stream))
        expired = data(await asyncio.wait_for(anext(stream), 2))
        assert expired["reason"] == "freshness" and expired["sequence"] == first["sequence"]
        old_quote = first["session"]["options"][0]["flight"]["quote"]
        new_quote = expired["session"]["options"][0]["flight"]["quote"]
        assert old_quote["freshness"] == "live" and new_quote["freshness"] == "stale"
        assert {k: v for k, v in new_quote.items() if k != "freshness"} == {k: v for k, v in old_quote.items() if k != "freshness"}
        assert expired["session"]["options"][0]["pricing_summary"]["full_trip"] is None
        assert store.get_session(session.id).model_dump_json() == before
        await stream.aclose()
        store.close()
    asyncio.run(scenario())


@contextmanager
def serving(app):
    # Real socket verifies chunk delivery; ASGI/TestClient buffers infinite bodies.
    bound = socket.socket()
    bound.bind(("127.0.0.1", 0))
    port = bound.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", timeout_graceful_shutdown=1))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [bound]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.01)
    assert server.started
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        bound.close()
        assert not thread.is_alive()


def next_session(lines):
    for line in lines:
        if line.startswith("data: "):
            payload = json.loads(line[6:])
            if "session" in payload:
                return payload
    raise AssertionError("Stream closed before snapshot")


def test_command_acknowledgement_exposes_committed_cursor_barrier(tmp_path):
    app = create_app(database_path=str(tmp_path / "ack.db"), transport=httpx.MockTransport(lambda _: httpx.Response(503)))
    with TestClient(app) as client:
        store = app.state.store

        class FastPlanner:
            async def finish(self, job_id):
                job = store.get_job(job_id)
                job.status = "completed"
                store.save_job(job)

            run = finish
            refresh_prices = finish

        app.state.planner = FastPlanner()
        app.state.command_worker.planner = app.state.planner
        for command in ("messages", "prices/refresh"):
            session = prepared_session(store)
            before = store.read_session_snapshot(session.id).sequence
            body = {"content": "synthetic request", "expected_revision": 1} if command == "messages" else {"expected_revision": 1}
            response = client.post(f"/api/sessions/{session.id}/{command}", json=body,
                                   headers={"Idempotency-Key": "ack-" + session.id})
            assert response.status_code == 202
            accepted = response.json()
            after = store.read_session_snapshot(session.id)
            assert before < accepted["sequence"] <= after.sequence
            assert after.job["id"] == accepted["job_id"]


def test_command_acknowledgement_exposes_committed_cursor_barrier(tmp_path):
    app = create_app(database_path=str(tmp_path / "ack.db"), transport=httpx.MockTransport(lambda _: httpx.Response(503)))
    with TestClient(app) as client:
        store = app.state.store

        class FastPlanner:
            async def finish(self, job_id):
                job = store.get_job(job_id)
                job.status = "completed"
                store.save_job(job)

            run = finish
            refresh_prices = finish

        app.state.planner = FastPlanner()
        app.state.command_worker.planner = app.state.planner
        for command in ("messages", "prices/refresh"):
            session = prepared_session(store)
            before = store.read_session_snapshot(session.id).sequence
            body = {"content": "synthetic request", "expected_revision": 1} if command == "messages" else {"expected_revision": 1}
            response = client.post(f"/api/sessions/{session.id}/{command}", json=body,
                                   headers={"Idempotency-Key": "ack-" + session.id})
            assert response.status_code == 202
            accepted = response.json()
            after = store.read_session_snapshot(session.id)
            assert before < accepted["sequence"] <= after.sequence
            assert after.job["id"] == accepted["job_id"]


def test_http_two_streams_receive_selection_and_reconnect_current_state(tmp_path):
    app = create_app(database_path=str(tmp_path / "http.db"), transport=httpx.MockTransport(lambda _: httpx.Response(503)))
    with serving(app) as base, httpx.Client(base_url=base, timeout=3) as client:
        session = prepared_session(app.state.store)
        path = f"/api/sessions/{session.id}/events"
        assert client.get("/api/sessions/missing/events").status_code == 404
        with client.stream("GET", path) as first, client.stream("GET", path) as second:
            assert first.headers["content-type"].startswith("text/event-stream")
            assert first.headers["cache-control"] == "no-cache"
            assert first.headers["x-accel-buffering"] == "no"
            first_lines, second_lines = first.iter_lines(), second.iter_lines()
            initial = next_session(first_lines)
            assert next_session(second_lines)["sequence"] == initial["sequence"]
            started = time.monotonic()
            assert client.post(f"/api/sessions/{session.id}/select", json={"option_id": session.options[0].id}).status_code == 200
            for lines in (first_lines, second_lines):
                update = next_session(lines)
                assert update["session"]["selected_option_id"] == session.options[0].id
                assert update["sequence"] > initial["sequence"]
            assert time.monotonic() - started < 2
        for cursor in ("malformed", str(update["sequence"]), "999999999999999999999"):
            with client.stream("GET", path, headers={"Last-Event-ID": cursor}) as recovered:
                latest = next_session(recovered.iter_lines())
                assert latest["reason"] == "connected"
                assert latest["sequence"] == update["sequence"]
                assert latest["session"]["selected_option_id"] == session.options[0].id
        deadline = time.monotonic() + 2
        while app.state.session_notifications.subscriber_count and time.monotonic() < deadline:
            time.sleep(0.01)
        assert app.state.session_notifications.subscriber_count == 0
        assert app.state.store.metrics().get("llm_requests", 0) == 0
