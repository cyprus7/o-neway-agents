"""Clock-controlled consent/DST/outbox/race tests. Every send is a fixture."""
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError

from backend.app.application.aftercare import ReminderInput
from backend.app.application.commands import CommandError
from backend.app.infrastructure.reminders import ReminderService, reminder_due
from backend.app.infrastructure.telegram_client import Delivery, TelegramClient
from backend.app.storage import Store

NOW = datetime(2030, 1, 1, 12, tzinfo=timezone.utc)


class FakeClient:
    def __init__(self, outcomes=None):
        self.outcomes = list(outcomes or [Delivery("sent")])
        self.calls = []
    async def send_message(self, chat_id, text, *, guard=None):
        if guard is not None and not guard(): return Delivery("canceled")
        self.calls.append((chat_id, text))
        return self.outcomes.pop(0) if self.outcomes else Delivery("sent")


@pytest.fixture
def store(tmp_path):
    value = Store(str(tmp_path / "reminders.db"))
    yield value
    value.close()


def setup_service(store, client=None):
    owner = store.resolve_identity("telegram:123", "111").user_id
    session = store.create_owned_session(owner)
    store.set_telegram_chat("123", 111, owner, session.id, 1)
    telegram = SimpleNamespace(enabled=True, bot_id="123", client=client or FakeClient())
    clock = [NOW]
    service = ReminderService(store, telegram, clock=lambda: clock[0])
    return owner, session, service, clock


def body(**changes):
    return ReminderInput(local_time="2030-01-01T13:00:00", timezone="UTC", consent=True,
                         text="Take the travel documents", **changes) if not changes else ReminderInput(**{
        "local_time": "2030-01-01T13:00:00", "timezone": "UTC", "consent": True,
        "text": "Take the travel documents", **changes})


@pytest.mark.parametrize("local", ["2030-03-31T02:30:00", "2030-10-27T02:30:00"])
def test_dst_nonexistent_or_ambiguous_local_time_requires_clarification(local):
    with pytest.raises(ValueError, match="ambiguous_or_nonexistent"):
        reminder_due(body(local_time=local, timezone="Europe/Berlin"), now=NOW)


def test_explicit_consent_naive_time_zone_and_future_bounds():
    for value in (False, 1, "true"):
        with pytest.raises(ValidationError): body(consent=value)
    with pytest.raises(ValidationError): body(local_time="2030-01-01T13:00:00+00:00")
    with pytest.raises(ValueError): reminder_due(body(timezone="Not/AZone"), now=NOW)
    with pytest.raises(ValueError): reminder_due(body(local_time="2030-01-01T11:00:00"), now=NOW)
    with pytest.raises(ValueError): reminder_due(body(local_time="2032-01-01T13:00:00"), now=NOW)
    assert reminder_due(body(local_time="2030-04-01T10:00:00", timezone="Europe/Berlin"), now=NOW) == datetime(2030,4,1,8,tzinfo=timezone.utc)


def test_requires_configured_verified_private_chat_and_ownership(store):
    owner, session, service, clock = setup_service(store)
    other = store.create_user().user_id
    with pytest.raises(CommandError) as exc: service.create(other, session.id, body())
    assert exc.value.status_code == 404
    with store.transaction(): store.db.execute("DELETE FROM telegram_chats")
    assert not service.available(owner)
    with pytest.raises(CommandError, match="reminders_unavailable"): service.create(owner, session.id, body())
    assert store.db.execute("SELECT COUNT(*) FROM reminders").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_future_schedule_sends_once_preserves_local_zone_and_consent(store):
    owner, session, service, clock = setup_service(store)
    item = service.create(owner, session.id, body())
    assert item["state"] == "scheduled" and item["consent"] is True
    assert item["local_time"] == "2030-01-01T13:00:00" and item["timezone"] == "UTC"
    assert not await service.send_once()
    clock[0] += timedelta(hours=2)
    assert await service.send_once()
    assert not await service.send_once()
    current = store.list_reminders(owner, session.id)[0]
    assert current["state"] == "sent" and current["attempts"] == 1
    assert len(service.telegram.client.calls) == 1
    assert current["due_at"] == item["due_at"]


@pytest.mark.asyncio
async def test_definite_retry_has_three_total_attempts_and_unknown_never_retries(store):
    client = FakeClient([Delivery("retryable", 1)] * 3)
    owner, session, service, clock = setup_service(store, client)
    item = service.create(owner, session.id, body())
    clock[0] += timedelta(hours=2)
    for index in range(3):
        assert await service.send_once()
        assert not await service.send_once()
        clock[0] += timedelta(seconds=2)
    assert len(client.calls) == 3 and store.list_reminders(owner, session.id)[0]["state"] == "failed"
    client.outcomes = [Delivery("delivery_unknown")]
    next_item = service.create(owner, session.id, body(local_time="2030-01-01T16:00:00"))
    clock[0] += timedelta(hours=3)
    assert await service.send_once()
    assert not await service.send_once()
    rows = {r["id"]: r for r in store.list_reminders(owner, session.id)}
    assert rows[next_item["id"]]["state"] == "delivery_unknown" and rows[next_item["id"]]["attempts"] == 1


@pytest.mark.asyncio
async def test_restart_sending_is_uncertain_and_never_replayed(store):
    owner, session, service, clock = setup_service(store)
    item = service.create(owner, session.id, body())
    clock[0] += timedelta(hours=2)
    assert store.claim_due_reminder("123", clock[0])["id"] == item["id"]
    service.start()
    service.start()
    await asyncio.sleep(.01)
    await service.aclose()
    assert store.list_reminders(owner, session.id)[0]["state"] == "delivery_unknown"
    assert service.telegram.client.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["block", "unlink"])
async def test_block_or_unlink_cancels_before_claim(store, change):
    owner, session, service, clock = setup_service(store)
    service.create(owner, session.id, body())
    clock[0] += timedelta(hours=2)
    with store.transaction():
        if change == "block": store.db.execute("UPDATE telegram_chats SET blocked=1")
        else: store.db.execute("DELETE FROM telegram_chats")
    assert not await service.send_once()
    assert store.list_reminders(owner, session.id)[0]["state"] == "canceled"
    assert service.telegram.client.calls == []


@pytest.mark.asyncio
async def test_scheduled_cancel_and_foreign_cancel_are_safe(store):
    owner, session, service, clock = setup_service(store)
    item = service.create(owner, session.id, body())
    foreign = store.create_user().user_id
    with pytest.raises(CommandError): store.cancel_reminder(foreign, session.id, item["id"])
    assert store.cancel_reminder(owner, session.id, item["id"])["state"] == "canceled"
    clock[0] += timedelta(hours=2)
    assert not await service.send_once() and service.telegram.client.calls == []


@pytest.mark.asyncio
async def test_inflight_cancel_or_delete_is_uncertain_and_cannot_resurrect(store):
    started, canceled = asyncio.Event(), asyncio.Event()
    class Waiting:
        async def send_message(self, *args, guard=None, **kwargs):
            assert guard()
            started.set()
            try: await asyncio.sleep(30)
            except asyncio.CancelledError:
                canceled.set()
                raise
    owner, session, service, clock = setup_service(store, Waiting())
    item = service.create(owner, session.id, body())
    clock[0] += timedelta(hours=2)
    task = asyncio.create_task(service.send_once())
    await asyncio.wait_for(started.wait(), 1)
    assert store.cancel_reminder(owner, session.id, item["id"])["state"] == "delivery_unknown"
    service.cancel(item["id"])
    await asyncio.wait_for(task, 1)
    assert canceled.is_set()
    assert store.list_reminders(owner, session.id)[0]["state"] == "delivery_unknown"
    # A second delivery is deleted while awaiting its HTTP result.
    started.clear(); canceled.clear()
    service.create(owner, session.id, body(local_time="2030-01-01T16:00:00"))
    clock[0] += timedelta(hours=3)
    task = asyncio.create_task(service.send_once())
    await asyncio.wait_for(started.wait(), 1)
    store.delete_owned_session(owner, session.id)
    service.cancel_unavailable()
    await asyncio.wait_for(task, 1)
    assert canceled.is_set() and store.db.execute("SELECT COUNT(*) FROM reminders").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_shared_send_lock_guard_blocks_unlinked_reminder_before_network_dispatch(store):
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []
    async def handler(request):
        calls.append(request)
        entered.set()
        await release.wait()
        return httpx.Response(200, json={"ok": True, "result": {"message_id": len(calls)}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = TelegramClient(http, "123:fixture_not_real")
        owner, session, service, clock = setup_service(store, client)
        service.create(owner, session.id, body())
        clock[0] += timedelta(hours=2)
        status = asyncio.create_task(client.send_message(111, "Status"))
        await asyncio.wait_for(entered.wait(), 1)
        reminder = asyncio.create_task(service.send_once())
        await asyncio.sleep(.01)
        with store.transaction(): store.db.execute("UPDATE telegram_chats SET blocked=1")
        release.set()
        await asyncio.wait_for(asyncio.gather(status, reminder), 1)
        assert len(calls) == 1
        assert store.list_reminders(owner, session.id)[0]["state"] == "canceled"


@pytest.mark.asyncio
async def test_duplicate_scheduler_wakeups_do_not_send_concurrently(store):
    entered, release = asyncio.Event(), asyncio.Event()
    class Once:
        def __init__(self): self.calls=0
        async def send_message(self, *args, guard=None, **kwargs):
            assert guard()
            self.calls += 1
            entered.set()
            await release.wait()
            return Delivery("sent")
    client = Once()
    owner, session, service, clock = setup_service(store, client)
    service.create(owner, session.id, body())
    clock[0] += timedelta(hours=2)
    first = asyncio.create_task(service.send_once())
    await asyncio.wait_for(entered.wait(), 1)
    second = asyncio.create_task(service.send_once())
    release.set()
    assert await asyncio.wait_for(first, 1)
    assert not await asyncio.wait_for(second, 1)
    assert client.calls == 1


def test_export_and_guest_retention_follow_owned_trip(store):
    owner, session, service, clock = setup_service(store)
    service.create(owner, session.id, body())
    exported = store.export_owned_data(owner)
    assert len(exported["reminders"]) == 1
    assert "chat_id" not in exported["reminders"][0] and "bot_id" not in exported["reminders"][0]
    store.delete_user_data(owner)
    assert store.db.execute("SELECT COUNT(*) FROM reminders").fetchone()[0] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('delivery',['sent','failed','delivery_unknown'])
async def test_aftercare_http_projection_uses_canonical_state_for_delivery_and_cancellation(store,monkeypatch,delivery):
    from fastapi import FastAPI
    from backend.app.transport.auth import AUTH_COOKIE,install_auth
    from backend.app.transport.aftercare import install_aftercare,install_reminders
    monkeypatch.setenv('APP_ALLOWED_ORIGINS','https://reminders.test')
    owner,session,service,clock=setup_service(store,FakeClient([Delivery(delivery)]))
    credentials=store.create_access_session(owner)
    app=FastAPI();app.state.store=store;app.state.identity_resolver=None;app.state.reminder_service=service
    install_auth(app);install_aftercare(app);install_reminders(app)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='https://reminders.test',
            cookies={AUTH_COOKIE:credentials.token},headers={'Origin':'https://reminders.test','X-CSRF-Token':credentials.csrf_token}) as api:
        url=f'/api/sessions/{session.id}'
        first=await api.post(url+'/reminders',json=body().model_dump(mode='json'))
        second=await api.post(url+'/reminders',json=body(local_time='2030-01-01T14:00:00').model_dump(mode='json'))
        assert first.status_code==second.status_code==201
        assert first.json()['state']==second.json()['state']=='scheduled'
        assert 'status' not in first.json() and 'status' not in second.json()
        canceled=await api.delete(url+'/reminders/'+second.json()['id'])
        assert canceled.status_code==204
        before=await api.get(url+'/aftercare');assert before.status_code==200
        rows={row['id']:row for row in before.json()['reminders']}
        assert rows[first.json()['id']]['state']=='scheduled' and rows[second.json()['id']]['state']=='canceled'
        clock[0]+=timedelta(hours=2)
        assert await service.send_once() and not await service.send_once()
        after=await api.get(url+'/aftercare');assert after.status_code==200
        rows={row['id']:row for row in after.json()['reminders']}
        assert rows[first.json()['id']]['state']==delivery and rows[first.json()['id']]['attempts']==1
        assert rows[second.json()['id']]['state']=='canceled' and rows[second.json()['id']]['attempts']==0
        assert all('status' not in row for row in rows.values())
        assert len(service.telegram.client.calls)==1


@pytest.mark.asyncio
async def test_aftercare_http_projection_keeps_sending_then_uncertain_cancel_state(store,monkeypatch):
    from fastapi import FastAPI
    from backend.app.transport.auth import AUTH_COOKIE,install_auth
    from backend.app.transport.aftercare import install_aftercare,install_reminders
    monkeypatch.setenv('APP_ALLOWED_ORIGINS','https://reminders.test')
    entered=asyncio.Event()
    class Waiting:
        async def send_message(self,*args,guard=None,**kwargs):
            assert guard();entered.set();await asyncio.sleep(30)
    owner,session,service,clock=setup_service(store,Waiting())
    credentials=store.create_access_session(owner)
    app=FastAPI();app.state.store=store;app.state.identity_resolver=None;app.state.reminder_service=service
    install_auth(app);install_aftercare(app);install_reminders(app)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='https://reminders.test',
            cookies={AUTH_COOKIE:credentials.token},headers={'Origin':'https://reminders.test','X-CSRF-Token':credentials.csrf_token}) as api:
        url=f'/api/sessions/{session.id}'
        created=await api.post(url+'/reminders',json=body().model_dump(mode='json'));assert created.status_code==201
        clock[0]+=timedelta(hours=2)
        sending=asyncio.create_task(service.send_once())
        await asyncio.wait_for(entered.wait(),1)
        during=(await api.get(url+'/aftercare')).json()['reminders'][0]
        assert during['state']=='sending' and 'status' not in during
        assert (await api.delete(url+'/reminders/'+created.json()['id'])).status_code==204
        assert await asyncio.wait_for(sending,1)
        final=(await api.get(url+'/aftercare')).json()['reminders'][0]
        assert final['state']=='delivery_unknown' and 'status' not in final
