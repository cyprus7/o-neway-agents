"""Bounded wakeups: a notification says to read durable current state."""
from __future__ import annotations

import asyncio
import threading


class SubscriptionClosed(asyncio.CancelledError):
    """The application or subscriber closed a live view."""


class SessionSubscription:
    def __init__(self, owner: SessionNotifications, session_id: str, loop: asyncio.AbstractEventLoop):
        self._owner = owner
        self.session_id = session_id
        self._loop = loop
        self._queue: asyncio.Queue[None] = asyncio.Queue(maxsize=1)
        self._lock = threading.Lock()
        self._pending = False
        self._closed = False

    def _offer(self) -> None:
        with self._lock:
            if self._closed or self._pending:
                return
            # Covers scheduled callbacks as well as the queue, so writes from
            # another thread cannot create an unbounded event-loop backlog.
            self._pending = True
        try:
            self._loop.call_soon_threadsafe(self._enqueue)
        except RuntimeError:  # The owner loop was already shut down.
            self.close()

    def _enqueue(self) -> None:
        if not self._queue.full():
            self._queue.put_nowait(None)

    async def wait(self) -> None:
        with self._lock:
            if self._closed:
                raise SubscriptionClosed
        await self._queue.get()
        with self._lock:
            self._pending = False
            if self._closed:
                raise SubscriptionClosed

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._owner._remove(self)
        try:
            self._loop.call_soon_threadsafe(self._enqueue)
        except RuntimeError:
            pass


class SessionNotifications:
    """Single-process adapter; SQLite remains the authority on reconnect."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subscribers: dict[str, set[SessionSubscription]] = {}
        self._closed = False

    def subscribe(self, session_id: str) -> SessionSubscription:
        subscription = SessionSubscription(self, session_id, asyncio.get_running_loop())
        with self._lock:
            if self._closed:
                raise SubscriptionClosed
            self._subscribers.setdefault(session_id, set()).add(subscription)
        return subscription

    def notify(self, session_id: str) -> None:
        with self._lock:
            subscriptions = tuple(self._subscribers.get(session_id, ()))
        for subscription in subscriptions:
            subscription._offer()

    def _remove(self, subscription: SessionSubscription) -> None:
        with self._lock:
            subscriptions = self._subscribers.get(subscription.session_id)
            if subscriptions is not None:
                subscriptions.discard(subscription)
                if not subscriptions:
                    del self._subscribers[subscription.session_id]

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return sum(len(group) for group in self._subscribers.values())

    def close(self) -> None:
        with self._lock:
            self._closed = True
            subscriptions = [item for group in self._subscribers.values() for item in group]
        for subscription in subscriptions:
            subscription.close()
