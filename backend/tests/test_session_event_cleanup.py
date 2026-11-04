"""Response ownership releases live subscriptions even between generator yields."""
import asyncio
from datetime import datetime, timezone

import pytest
from starlette.requests import ClientDisconnect

from backend.app.domain.session_updates import SessionView
from backend.app.infrastructure.session_notifications import SessionNotifications
from backend.app.transport.session_events import SessionEventResponse, session_events


class CurrentView:
    def read(self, session_id, *, reason):
        return SessionView(sequence=7, session={"id": session_id}, job=None,
                           server_time=datetime.now(timezone.utc).isoformat(),
                           reason=reason, next_expiry=None)


def scope(spec_version):
    return {"type": "http", "asgi": {"version": "3.0", "spec_version": spec_version}}


async def no_disconnect():
    await asyncio.Future()


@pytest.mark.parametrize("spec_version", ["2.3", "2.4"])
@pytest.mark.parametrize("failed_body", [1, 2])
def test_send_error_closes_subscription_while_generator_is_suspended(spec_version, failed_body):
    async def scenario():
        notifications = SessionNotifications()
        stream = session_events("cleanup-trip", CurrentView(), notifications)
        response = SessionEventResponse(stream, media_type="text/event-stream")
        body_count = 0

        async def failed_send(message):
            nonlocal body_count
            if message["type"] == "http.response.body":
                body_count += 1
                if body_count == failed_body:
                    assert notifications.subscriber_count == 1
                    raise OSError("Client transport closed while sending frame")

        expected = ClientDisconnect if spec_version == "2.4" else OSError
        with pytest.raises(expected):
            await asyncio.wait_for(response(scope(spec_version), no_disconnect, failed_send), timeout=1)

        # Keep both objects alive: garbage collection must not be responsible
        # for eventually running the generator's cleanup.
        assert response.body_iterator is stream
        assert body_count == failed_body
        assert notifications.subscriber_count == 0

    asyncio.run(scenario())


def test_asgi_disconnect_during_frame_send_closes_subscription_before_response_returns():
    async def scenario():
        notifications = SessionNotifications()
        stream = session_events("cleanup-trip", CurrentView(), notifications)
        response = SessionEventResponse(stream, media_type="text/event-stream")
        sending_snapshot = asyncio.Event()
        send_cancelled = asyncio.Event()

        async def receive():
            await sending_snapshot.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            if message["type"] == "http.response.body" and b"event: session\n" in message["body"]:
                sending_snapshot.set()
                try:
                    await asyncio.Future()
                except asyncio.CancelledError:
                    send_cancelled.set()
                    raise

        await asyncio.wait_for(response(scope("2.3"), receive, send), timeout=1)
        assert sending_snapshot.is_set() and send_cancelled.is_set()
        assert response.body_iterator is stream
        assert notifications.subscriber_count == 0

    asyncio.run(scenario())


@pytest.mark.parametrize("spec_version", ["2.3", "2.4"])
def test_cancelled_request_at_yield_closes_subscription_and_propagates_cancellation(spec_version):
    async def scenario():
        notifications = SessionNotifications()
        stream = session_events("cleanup-trip", CurrentView(), notifications)
        response = SessionEventResponse(stream, media_type="text/event-stream")
        sending_snapshot = asyncio.Event()

        async def send(message):
            if message["type"] == "http.response.body" and b"event: session\n" in message["body"]:
                sending_snapshot.set()
                await asyncio.Future()

        request = asyncio.create_task(response(scope(spec_version), no_disconnect, send))
        await asyncio.wait_for(sending_snapshot.wait(), timeout=1)
        assert notifications.subscriber_count == 1
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(request, timeout=1)
        assert response.body_iterator is stream
        assert notifications.subscriber_count == 0

    asyncio.run(scenario())
