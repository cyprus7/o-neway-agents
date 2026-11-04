"""Thin Telegram edge: one application, durable update admission and bounded delivery."""
import asyncio
import hmac
import os
import re
from urllib.parse import urlencode, urlsplit

from fastapi import Request
from fastapi.responses import JSONResponse

from ..application.commands import CommandError
from ..application.errors import ApplicationError
from ..domain.identity import Principal
from ..infrastructure.telegram_client import TelegramClient
from .telegram_auth import _json, MAX_USER_ID

BODY_LIMIT = 64 * 1024
INBOX_LIMIT = 1024


class TelegramService:
    def __init__(self, store, trips, worker, http_client):
        self.store, self.trips, self.worker = store, trips, worker
        self.mode = os.getenv("TELEGRAM_MODE", "disabled").strip()
        token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        self._secret = os.getenv("TELEGRAM_WEBHOOK_SECRET", "")
        self.public_url = os.getenv("APP_PUBLIC_URL", "").rstrip("/")
        self.enabled, self.reason, self.bot_id = False, "disabled", None
        self._tasks, self._closed = [], False
        self._wake = asyncio.Event()
        self.client = None
        if self.mode == "disabled":
            self.reason = "explicit_mode_required" if token and "TELEGRAM_MODE" not in os.environ else "disabled"
            return
        if self.mode not in {"webhook", "polling"}:
            self.reason = "invalid_mode"
            return
        match = re.fullmatch(r"([0-9]{1,20}):([^\s:]{1,256})", token)
        try:
            url = urlsplit(self.public_url)
            valid_url = (url.scheme == "https" and url.hostname and "." in url.hostname and not url.username
                and not url.password and not url.query and not url.fragment and url.port in (None, 443)
                and not any(ord(c) < 33 for c in self.public_url))
        except ValueError:
            valid_url = False
        if not match or int(match[1]) <= 0 or not valid_url:
            self.reason = "invalid_configuration"
            return
        if (self.mode == "webhook" and not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", self._secret)
                or self.mode == "polling" and self._secret):
            self.reason = "conflicting_or_missing_mode_secret"
            return
        self.bot_id = str(int(match[1]))
        self.enabled, self.reason = True, "configured"
        self.client = TelegramClient(http_client, token)

    def start(self):
        if self._closed:
            raise RuntimeError("telegram_service_closed")
        if self.enabled and not self._tasks:
            self.store.recover_telegram_deliveries(self.bot_id)
            self._tasks.append(asyncio.create_task(self._sender(), name="telegram-status-sender"))
            if self.mode == "polling":
                self._tasks.append(asyncio.create_task(self._poll(), name="telegram-update-poller"))
            self.wake_sender()
        return self

    def wake_sender(self):
        self._wake.set()

    def _url(self, trip_id):
        return self.public_url + ("/?" + urlencode({"start_param": "trip_" + trip_id}) if trip_id else "/")

    async def handle_update(self, update):
        if not self.enabled:
            return {"status": "disabled"}
        if not isinstance(update, dict) or type(update.get("update_id")) is not int or not 0 <= update["update_id"] < 2**63:
            return {"status": "ignored"}
        update_id = update["update_id"]
        message = update.get("message")
        if not isinstance(message, dict):
            return {"status": "ignored"}
        chat, author, text = message.get("chat"), message.get("from"), message.get("text")
        if (not isinstance(chat, dict) or not isinstance(author, dict) or chat.get("type") != "private"
                or type(author.get("id")) is not int or not 0 < author["id"] <= MAX_USER_ID
                or type(chat.get("id")) is not int or chat["id"] != author["id"] or author.get("is_bot") is True
                or not isinstance(text, str) or not text.strip() or len(text) > 4096):
            return {"status": "ignored"}
        text, chat_id = text.strip(), chat["id"]
        cancel_id, job_id = None, None
        with self.store.transaction():
            prior = self.store.telegram_update(self.bot_id, update_id)
            if prior is not None:
                return {"status": "duplicate", "duplicate": True}
            if self.store.telegram_inbox_size(self.bot_id) >= INBOX_LIMIT:
                raise CommandError("telegram_inbox_full", 429, retry_after=30)
            user = self.store.resolve_identity("telegram:" + self.bot_id, str(author["id"]))
            application = self.trips.for_principal(Principal(user.user_id, "telegram:" + self.bot_id, str(author["id"])))
            mapping = self.store.telegram_chat(self.bot_id, chat_id)
            if mapping and update_id <= mapping["last_update_id"]:
                self.store.record_telegram_update(self.bot_id, update_id, user.user_id, chat_id, None, None, "", ignored=True)
                return {"status": "ignored"}
            active = mapping["active_session_id"] if mapping and mapping["owner_id"] == user.user_id else None
            if active and self.store.session_owner(active) != user.user_id:
                active = None
            head, _, argument = text.partition(" ")
            command = head.split("@", 1)[0].lower() if head.startswith("/") else None
            ack = ""
            if command == "/help":
                ack = "Опишите поездку. /new — новая, /trips — ваши поездки, /stop — отменить поиск. Решения и демо-оплата доступны в приложении."
            elif command == "/trips":
                items = application.list(limit=5).items
                ack = "Ваши поездки: " + ("; ".join((s.intent.destination or "Новая поездка")[:60] for s in items) if items else "пока нет") + ". Откройте список в приложении."
            elif command == "/stop":
                job = self.store.active_job_record(active) if active else None
                if job:
                    application.cancel(job.id)
                    cancel_id = job.id
                ack = "Поиск отменён." if job else "Активного поиска нет."
            elif command in {"/new", "/start"}:
                requested = argument.strip()
                if command == "/start" and requested:
                    trip_id = requested[5:] if requested.startswith("trip_") else ""
                    if trip_id and len(trip_id) <= 160 and self.store.get_owned_session(trip_id, user.user_id):
                        active = trip_id
                        ack = "Поездка открыта."
                    else:
                        ack = "Поездка не найдена. Откройте свои поездки в приложении."
                else:
                    if command == "/new" or not active:
                        active = application.create().id
                    ack = "Опишите направление, даты и пожелания. Продолжить можно в приложении."
            elif command is not None:
                ack = "Команда не поддерживается. Используйте /help."
            else:
                if not active:
                    active = application.create().id
                session = application.session(active)
                job, _ = application.message(active, text, key=f"telegram:{self.bot_id}:{update_id}", expected_revision=session.revision)
                job_id = job.id
                ack = "Запрос принят. Результат появится в приложении; я сообщу о завершении."
            self.store.set_telegram_chat(self.bot_id, chat_id, user.user_id, active, update_id)
            self.store.record_telegram_update(self.bot_id, update_id, user.user_id, chat_id, active, job_id, ack)
        if cancel_id:
            self.worker.cancel(cancel_id)
        if job_id:
            self.worker.notify(job_id)
        self.wake_sender()
        return {"status": "accepted", "duplicate": False, **({"job_id": job_id} if job_id else {})}

    async def _sender(self):
        while not self._closed:
            self._wake.clear()
            delivery = self.store.claim_telegram_delivery(self.bot_id)
            if delivery is None:
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=1)
                except TimeoutError:
                    pass
                continue
            if delivery["stage"] == "ack":
                text = delivery["ack_text"]
            else:
                text = {"completed": "Поиск завершён. Сравните варианты в приложении.",
                    "canceled": "Поиск отменён.", "interrupted": "Поиск прерван остановкой сервиса. Повторите запрос в приложении.",
                    "superseded": "Условия поездки изменились. Откройте текущий результат в приложении."}.get(delivery["job_status"], "Поиск не завершился. Состояние сохранено; повторите запрос в приложении.")
            result = await self.client.send_message(delivery["chat_id"], text, web_app_url=self._url(delivery["session_id"]))
            self.store.finish_telegram_delivery(self.bot_id, delivery["update_id"], delivery["stage"], result.outcome, result.retry_after)

    async def poll_once(self):
        if not self.enabled or self.mode != "polling":
            raise RuntimeError("polling_mode_required")
        updates, outcome = await self.client.get_updates(self.store.telegram_poll_offset(self.bot_id))
        if outcome == "conflict":
            self.reason = "polling_webhook_conflict"
            return False
        if outcome != "ok":
            return True
        for update in updates:
            if not isinstance(update, dict) or type(update.get("update_id")) is not int or not 0 <= update["update_id"] < 2**63:
                continue
            await self.handle_update(update)
            self.store.advance_telegram_poll(self.bot_id, update["update_id"])
        return True

    async def _poll(self):
        while not self._closed:
            try:
                if not await self.poll_once():
                    return
            except CommandError:
                pass  # Admission/inbox backpressure: do not advance the cursor.
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=2)
                self._wake.clear()
            except TimeoutError:
                pass

    async def aclose(self):
        self._closed = True
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()


def install_telegram(app):
    @app.post("/api/telegram/webhook")
    async def webhook(request: Request):
        service = request.app.state.telegram_service
        if not service.enabled or service.mode != "webhook":
            return JSONResponse({"detail": {"code": "telegram_unavailable"}}, status_code=503)
        supplied = request.headers.get("x-telegram-bot-api-secret-token", "")
        if not hmac.compare_digest(supplied.encode(), service._secret.encode()):
            return JSONResponse({"detail": {"code": "telegram_secret_rejected"}}, status_code=403)
        body = bytearray()
        async for chunk in request.stream():
            if len(body)+len(chunk) > BODY_LIMIT:
                return JSONResponse({"detail": {"code": "request_too_large"}}, status_code=413)
            body.extend(chunk)
        try:
            payload = _json(body)
        except (ValueError, TypeError, UnicodeError, RecursionError):
            return JSONResponse({"detail": {"code": "invalid_update"}}, status_code=400)
        try:
            result = await service.handle_update(payload)
        except (CommandError, ApplicationError) as exc:
            status = getattr(exc, "status_code", 409)
            return JSONResponse({"detail": {"code": "telegram_admission_rejected"}}, status_code=status)
        return JSONResponse(result, status_code=202)
