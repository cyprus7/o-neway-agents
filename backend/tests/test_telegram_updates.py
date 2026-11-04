"""Telegram fixture-only admission/delivery; no real registration or messages."""
import asyncio
from datetime import datetime, timedelta, timezone
import json
import logging
from types import SimpleNamespace

from fastapi import FastAPI
import httpx
import pytest

from backend.app.application.trips import TripApplication
from backend.app.infrastructure.telegram_client import TelegramClient
from backend.app.storage import Store
from backend.app.transport.telegram import TelegramService, install_telegram, BODY_LIMIT

TOKEN = "123456:fixture_token_never_real"
SECRET = "fixture_webhook_secret"


class Worker:
    def __init__(self): self.notified, self.canceled = [], []
    def notify(self, job_id=None): self.notified.append(job_id)
    def cancel(self, job_id): self.canceled.append(job_id)


@pytest.fixture
def store(tmp_path):
    value = Store(str(tmp_path / "telegram.db"))
    yield value
    value.close()


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setenv("TELEGRAM_MODE", "webhook")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", SECRET)
    monkeypatch.setenv("APP_PUBLIC_URL", "https://travel.example")


def update(update_id=1, text="Plan a beach trip", user=111, **changes):
    result = {"update_id": update_id, "message": {"chat": {"id": user, "type": "private"},
        "from": {"id": user, "is_bot": False}, "text": text}}
    result.update(changes)
    return result


def service(store, client):
    return TelegramService(store, TripApplication(store), Worker(), client)


@pytest.mark.asyncio
async def test_duplicate_lost_ack_and_restart_accept_one_command_or_new_trip(store, configured):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"ok": True, "result": {}}))) as client:
        first = service(store, client)
        result = await first.handle_update(update())
        repeated = await first.handle_update(update())
        assert result["status"] == "accepted" and repeated["duplicate"]
        restarted = service(store, client)
        assert (await restarted.handle_update(update()))["duplicate"]
        assert store.db.execute("SELECT COUNT(*) FROM commands").fetchone()[0] == 1
        assert store.db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1
        assert len(first.worker.notified) == 1
        assert (await restarted.handle_update(update(2, "/new")))["status"] == "accepted"
        assert (await restarted.handle_update(update(2, "/new")))["duplicate"]
        assert store.db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 2


@pytest.mark.asyncio
async def test_private_only_positive_identity_and_unsupported_updates_no_mutation(store, configured):
    async with httpx.AsyncClient() as client:
        bot = service(store, client)
        group = update()
        group["message"]["chat"]["type"] = "group"
        negative = update(user=-1)
        wrongchat = update()
        wrongchat["message"]["chat"]["id"] = 999
        for invalid in (group, negative, wrongchat, {"update_id": 3, "callback_query": {}}, update(message={"photo": []}), {"update_id": True}):
            assert (await bot.handle_update(invalid))["status"] == "ignored"
        assert store.db.execute("SELECT COUNT(*) FROM external_identities").fetchone()[0] == 0
        assert store.db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_other_owner_start_parameter_and_reordered_updates_cannot_change_active_trip(store, configured):
    async with httpx.AsyncClient() as client:
        bot = service(store, client)
        await bot.handle_update(update(10, "/new", user=111))
        await bot.handle_update(update(11, "/new", user=222))
        own = store.telegram_chat(bot.bot_id, 111)["active_session_id"]
        foreign = store.telegram_chat(bot.bot_id, 222)["active_session_id"]
        await bot.handle_update(update(12, "/start trip_"+foreign, user=111))
        assert store.telegram_chat(bot.bot_id, 111)["active_session_id"] == own
        assert (await bot.handle_update(update(9, "/new", user=111)))["status"] == "ignored"
        assert store.telegram_chat(bot.bot_id, 111)["active_session_id"] == own
        assert store.db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 2
        user = store.resolve_identity("telegram:"+bot.bot_id, "111")
        assert store.get_owned_session(foreign, user.user_id) is None


@pytest.mark.asyncio
async def test_stop_uses_same_owner_command_cancel(store, configured):
    async with httpx.AsyncClient() as client:
        bot = service(store, client)
        result = await bot.handle_update(update())
        await bot.handle_update(update(2, "/stop"))
        assert store.get_job(result["job_id"]).status == "canceled"
        assert bot.worker.canceled == [result["job_id"]]


@pytest.mark.asyncio
async def test_webhook_secret_is_checked_before_body_and_bounded_body(store, configured):
    async with httpx.AsyncClient() as outbound:
        bot = service(store, outbound)
        app = FastAPI()
        app.state.telegram_service = bot
        install_telegram(app)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            async def must_not_read():
                raise AssertionError("Secret rejected before body")
                yield b""
            rejected = await client.post("/api/telegram/webhook", content=must_not_read())
            assert rejected.status_code == 403
            headers = {"x-telegram-bot-api-secret-token": SECRET}
            assert (await client.post("/api/telegram/webhook", content=b"x"*(BODY_LIMIT+1), headers=headers)).status_code == 413
            assert (await client.post("/api/telegram/webhook", content=b'{"update_id":1,"update_id":2}', headers=headers)).status_code == 400
            accepted = await client.post("/api/telegram/webhook", json=update(), headers=headers)
            assert accepted.status_code == 202
            duplicate = await client.post("/api/telegram/webhook", json=update(), headers=headers)
            assert duplicate.json()["duplicate"]
        assert store.db.execute("SELECT COUNT(*) FROM commands").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_sender_sends_ack_and_one_terminal_result_without_private_details(store, configured):
    sent, result_sent = [], asyncio.Event()
    def handler(request):
        payload = json.loads(request.content)
        sent.append(payload)
        if len(sent) >= 2: result_sent.set()
        return httpx.Response(200, json={"ok": True, "result": {"message_id": len(sent)}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        bot = service(store, client)
        admitted = await bot.handle_update(update(text="PRIVATE_USER_WISH"))
        job = store.get_job(admitted["job_id"])
        job.status = "completed"
        store.save_job(job)
        bot.start()
        await asyncio.wait_for(result_sent.wait(), 1)
        await bot.aclose()
        assert len(sent) == 2
        assert "Запрос принят" in sent[0]["text"] and "завершён" in sent[1]["text"]
        assert "PRIVATE_USER_WISH" not in str(sent)
        url = sent[0]["reply_markup"]["inline_keyboard"][0][0]["web_app"]["url"]
        assert url.startswith("https://travel.example/") and "start_param=trip_" in url
        row = store.telegram_update(bot.bot_id, 1)
        assert row["ack_state"] == row["result_state"] == "sent"
        assert row["ack_attempts"] == row["result_attempts"] == 1


@pytest.mark.asyncio
async def test_ambiguous_send_is_not_retried_and_restart_sending_becomes_unknown(store, configured):
    calls, dispatched = [], asyncio.Event()
    async def timeout(request):
        calls.append(1)
        dispatched.set()
        raise httpx.ReadTimeout("never expose token or payload")
    async with httpx.AsyncClient(transport=httpx.MockTransport(timeout)) as client:
        bot = service(store, client)
        await bot.handle_update(update(text="/help"))
        bot.start()
        await asyncio.wait_for(dispatched.wait(), 1)
        await asyncio.sleep(.01)
        await bot.aclose()
        assert calls == [1]
        assert store.telegram_update(bot.bot_id, 1)["ack_state"] == "delivery_unknown"
        with store.transaction():
            store.db.execute("UPDATE telegram_inbox SET ack_state='sending'")
        restarted = service(store, client).start()
        await asyncio.sleep(.01)
        await restarted.aclose()
        assert calls == [1]
        assert store.telegram_update(bot.bot_id, 1)["ack_state"] == "delivery_unknown"


@pytest.mark.asyncio
async def test_blocked_bot_disables_sends_and_definite_retry_is_capped(store, configured):
    called = asyncio.Event()
    def blocked(request):
        called.set()
        return httpx.Response(403, json={"ok": False, "error_code": 403})
    async with httpx.AsyncClient(transport=httpx.MockTransport(blocked)) as client:
        bot = service(store, client)
        await bot.handle_update(update(text="/help"))
        bot.start()
        await asyncio.wait_for(called.wait(), 1)
        await asyncio.sleep(.01)
        await bot.aclose()
        assert store.telegram_chat(bot.bot_id, 111)["blocked"] == 1
        assert store.telegram_update(bot.bot_id, 1)["ack_state"] == "blocked"
        with store.transaction():
            store.db.execute("UPDATE telegram_chats SET blocked=0")
            store.db.execute("UPDATE telegram_inbox SET ack_state='pending',ack_attempts=0")
        for attempt in range(3):
            claimed = store.claim_telegram_delivery(bot.bot_id)
            assert claimed
            store.finish_telegram_delivery(bot.bot_id, 1, "ack", "retryable", 1)
            with store.transaction():
                store.db.execute("UPDATE telegram_inbox SET ack_next_at=NULL")
        assert store.telegram_update(bot.bot_id, 1)["ack_state"] == "failed"
        assert store.claim_telegram_delivery(bot.bot_id) is None


@pytest.mark.asyncio
async def test_polling_same_handler_cursor_persists_and_both_modes_refused(store, configured, monkeypatch):
    monkeypatch.setenv("TELEGRAM_MODE", "polling")
    async with httpx.AsyncClient() as client:
        assert not service(store, client).enabled  # Webhook secret conflicts.
    monkeypatch.delenv("TELEGRAM_WEBHOOK_SECRET")
    offsets = []
    def handler(request):
        payload = json.loads(request.content)
        offsets.append(payload["offset"])
        assert payload["limit"] == 20 and payload["allowed_updates"] == ["message"]
        return httpx.Response(200, json={"ok": True, "result": [update(20)]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        bot = service(store, client)
        assert await bot.poll_once()
        assert store.telegram_poll_offset(bot.bot_id) == 21
        restarted = service(store, client)
        assert await restarted.poll_once()
        assert offsets == [0, 21]
        assert store.db.execute("SELECT COUNT(*) FROM commands").fetchone()[0] == 1
        assert store.db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_client_has_no_ambient_headers_or_token_url_logs(configured, caplog):
    captured = []
    def handler(request):
        captured.append(request)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})
    caplog.set_level(logging.INFO, logger="httpx")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), headers={"traceparent": "PRIVATE_TRACE", "baggage": "PRIVATE_BAGGAGE"}, auth=("user", "secret"), cookies={"sensitive": "cookie"}, params={"private": "query"}) as client:
        result = await TelegramClient(client, TOKEN).send_message(111, "Accepted")
    assert result.outcome == "sent"
    assert all(key not in captured[0].headers for key in ("traceparent", "baggage", "authorization", "cookie"))
    assert not captured[0].url.query
    assert TOKEN not in caplog.text and "PRIVATE_TRACE" not in caplog.text


@pytest.mark.asyncio
async def test_deletion_preserves_only_dedup_tombstone_and_does_not_recreate_trip(store, configured):
    async with httpx.AsyncClient() as client:
        bot = service(store, client)
        await bot.handle_update(update())
        mapping = store.telegram_chat(bot.bot_id, 111)
        store.delete_user_data(mapping["owner_id"])
        assert store.db.execute("SELECT COUNT(*) FROM telegram_chats").fetchone()[0] == 0
        assert store.db.execute("SELECT COUNT(*) FROM telegram_inbox").fetchone()[0] == 0
        assert (await bot.handle_update(update()))["duplicate"]
        assert store.db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_debug_httpcore_children_and_existing_prefix_logs_redact_tokens(configured, caplog):
    existing = logging.getLogger("httpcore.fixture_existing_child")
    caplog.set_level(logging.DEBUG)
    async with httpx.AsyncClient() as client:
        TelegramClient(client, TOKEN)
        for logger in (logging.getLogger("httpcore.http11"), logging.getLogger("httpcore.http2"), existing):
            logger.debug("request path /bot%s/sendMessage literal %s", TOKEN, TOKEN)
    assert TOKEN not in caplog.text
    assert "[redacted]" in caplog.text


@pytest.mark.asyncio
async def test_inbox_bound_backpressure_rolls_back_admission_and_retention_is_bounded(store, configured, monkeypatch):
    import backend.app.transport.telegram as adapter
    monkeypatch.setattr(adapter, "INBOX_LIMIT", 1)
    async with httpx.AsyncClient() as client:
        bot = service(store, client)
        await bot.handle_update(update(1, "/new"))
        from backend.app.application.commands import CommandError
        with pytest.raises(CommandError) as exc:
            await bot.handle_update(update(2, "/new"))
        assert exc.value.code == "telegram_inbox_full"
        assert store.db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1
        now = datetime.now(timezone.utc)
        with store.transaction():
            store.db.execute("UPDATE telegram_inbox SET processed_at=?", ((now-timedelta(days=31)).isoformat(),))
        purged = store.cleanup_retention(now=now, limit=1)
        assert purged["telegram_updates"] == 1 and sum(purged.values()) == 1


@pytest.mark.asyncio
async def test_polling_webhook_conflict_stops_instead_of_unregistering(store, configured, monkeypatch):
    monkeypatch.setenv("TELEGRAM_MODE", "polling")
    monkeypatch.delenv("TELEGRAM_WEBHOOK_SECRET")
    methods = []
    def handler(request):
        methods.append(str(request.url).rsplit("/", 1)[-1])
        return httpx.Response(409, json={"ok": False, "error_code": 409})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        bot = service(store, client)
        assert not await bot.poll_once()
        assert bot.reason == "polling_webhook_conflict"
    assert methods == ["getUpdates"]


def test_receipt_read_is_owner_scoped_immutable_and_newest_first(store):
    from backend.app.application.commands import CommandError
    a, b = store.create_user().user_id, store.create_user().user_id
    trip = store.create_owned_session(a)
    for index in range(2):
        store.save_checkout({"id": "receipt-"+str(index), "session_id": trip.id, "status": "simulated", "source_time": "2026-01-01"})
    before = [r[0] for r in store.db.execute("SELECT data FROM checkouts ORDER BY rowid")]
    with pytest.raises(CommandError): store.list_owned_receipts(b, trip.id)
    receipts = store.list_owned_receipts(a, trip.id)
    assert [r["id"] for r in receipts] == ["receipt-1", "receipt-0"]
    assert all(r["source_time"] == "2026-01-01" for r in receipts)
    assert [r[0] for r in store.db.execute("SELECT data FROM checkouts ORDER BY rowid")] == before
