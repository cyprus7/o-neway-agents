"""SSE adapter: full current state, bounded wakeups, no provider side effects."""
from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from datetime import datetime, timezone

from anyio import CancelScope
from starlette.responses import StreamingResponse

from ..application.session_views import SessionViews
from ..domain.session_updates import SessionUpdateNotifications


HEARTBEAT_SECONDS = 15.0


class SessionEventResponse(StreamingResponse):
    """Release the subscription even if sending fails while a frame is yielded."""

    async def __call__(self, scope, receive, send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            # StreamingResponse cancels on disconnect, but may leave its async
            # iterator suspended at yield when send raises. Own its lifetime.
            with CancelScope(shield=True):
                await self.body_iterator.aclose()


def frame(event: str, payload: dict, sequence: int | None = None) -> str:
    identity = f"id: {sequence}\n" if sequence is not None else ""
    return f"{identity}event: {event}\ndata: {json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}\n\n"


async def session_events(session_id: str, views: SessionViews,
                         notifications: SessionUpdateNotifications,
                         *, heartbeat_seconds: float = HEARTBEAT_SECONDS,
                         authorize: Callable[[], bool] | None = None) -> AsyncIterator[str]:
    # Subscribe before reading: a concurrent commit can only cause a harmless
    # extra wake, never a missing update between the initial read and subscribe.
    subscription = notifications.subscribe(session_id)
    try:
        if authorize and not authorize():
            return
        current = views.read(session_id, reason="connected")
        if current is None:
            return
        yield "retry: 2000\n\n"
        yield frame("session", current.to_dict(), current.sequence)
        while True:
            if authorize and not authorize():
                return
            now = datetime.now(timezone.utc)
            expiry = current.next_expiry
            until_expiry = max(0.001, (expiry - now).total_seconds()) if expiry else None
            timeout = min(heartbeat_seconds, until_expiry) if until_expiry is not None else heartbeat_seconds
            try:
                await asyncio.wait_for(subscription.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                if authorize and not authorize():
                    return
                if expiry is None or datetime.now(timezone.utc) < expiry:
                    # Comments keep proxy connections open; the named event is
                    # observable by EventSource so clients can detect a stall.
                    yield ": heartbeat\n" + frame("heartbeat", {"server_time": datetime.now(timezone.utc).isoformat()})
                    continue
                reason = "freshness"
            else:
                reason = "updated"
            if authorize and not authorize():
                return
            updated = views.read(session_id, reason=reason)
            if updated is None:
                return
            if updated.sequence > current.sequence or reason == "freshness":
                current = updated
                yield frame("session", current.to_dict(), current.sequence)
    finally:
        subscription.close()
