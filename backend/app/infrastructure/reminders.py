"""Small consented reminder outbox; no inference or supplier communication."""
import asyncio
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..application.commands import CommandError
from ..infrastructure.telegram_client import Delivery


def reminder_due(body, *, now):
    try:
        zone = ZoneInfo(body.timezone)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError("invalid_timezone") from None
    local = body.local_time
    if local.tzinfo is not None:
        raise ValueError("local_time_requires_no_offset")
    possibilities = set()
    for fold in (0, 1):
        candidate = local.replace(tzinfo=zone, fold=fold).astimezone(timezone.utc)
        if candidate.astimezone(zone).replace(tzinfo=None) == local:
            possibilities.add(candidate)
    if len(possibilities) != 1:
        raise ValueError("ambiguous_or_nonexistent_local_time")
    due = possibilities.pop()
    if not now < due <= now + timedelta(days=365):
        raise ValueError("reminder_requires_future_within_year")
    return due


class ReminderService:
    def __init__(self, store, telegram_service, *, clock=None):
        self.store, self.telegram = store, telegram_service
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._wake, self._lock = asyncio.Event(), asyncio.Lock()
        self._task, self._inflight, self._current_id = None, None, None
        self._closed = False

    def available(self, owner_id):
        return bool(self.telegram.enabled and self.telegram.client and self.store.telegram_private_chat(self.telegram.bot_id, owner_id))

    def create(self, owner_id, session_id, body):
        if self.store.get_owned_session(session_id, owner_id) is None:
            raise CommandError("not_found", 404)
        if not self.available(owner_id):
            raise CommandError("reminders_unavailable", 409)
        due = reminder_due(body, now=self.clock())
        item = self.store.create_reminder(owner_id, session_id, body, due, self.telegram.bot_id)
        self.notify()
        return item

    def start(self):
        if self._closed:
            raise RuntimeError("reminder_service_closed")
        if self.telegram.enabled and self._task is None:
            self.store.recover_reminders(self.telegram.bot_id)
            self._task = asyncio.create_task(self._run(), name="consented-reminder-outbox")
            self.notify()
        return self

    def notify(self):
        self._wake.set()

    def cancel(self, reminder_id):
        if self._current_id == reminder_id and self._inflight is not None:
            self._inflight.cancel()
        self.notify()

    def cancel_unavailable(self):
        if self._current_id is not None and not self.store.reminder_can_send(self._current_id):
            self.cancel(self._current_id)

    async def send_once(self):
        async with self._lock:
            if not self.telegram.enabled:
                return False
            item = self.store.claim_due_reminder(self.telegram.bot_id, self.clock())
            if item is None:
                return False
            self._current_id = item["id"]
            try:
                # The same finite TelegramClient serializes status/reminder
                # sends and checks this synchronous guard after its send lock.
                self._inflight = asyncio.create_task(self.telegram.client.send_message(item["chat_id"],
                    "Ваше напоминание: " + item["text"], guard=lambda: self.store.reminder_can_send(item["id"])))
                try:
                    result = await self._inflight
                except asyncio.CancelledError:
                    self.store.finish_reminder(item["id"], "delivery_unknown", now=self.clock())
                    if self._closed:
                        raise
                    return True
                except Exception:
                    result = Delivery("delivery_unknown")
                self.store.finish_reminder(item["id"], result.outcome, now=self.clock(), retry_after=result.retry_after)
                return True
            finally:
                self._inflight, self._current_id = None, None

    async def _run(self):
        while not self._closed:
            self._wake.clear()
            if await self.send_once():
                continue
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=1)
            except TimeoutError:
                pass

    async def aclose(self):
        if self._closed:
            return
        self._closed = True
        if self._current_id is not None:
            self.store.finish_reminder(self._current_id, "delivery_unknown", now=self.clock())
        if self._inflight is not None:
            self._inflight.cancel()
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
