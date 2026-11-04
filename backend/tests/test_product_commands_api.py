"""Production HTTP identity/command/Telegram composition; fixture model, no providers."""
import asyncio
import threading
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from backend.app.application.dependencies import AgentDependencies
from backend.app.main import create_app
from backend.app.models import IntentExtraction, merge_intent
from backend.tests.test_telegram_auth import TOKEN, signed

ORIGIN='https://travel.test'


class FixtureModel:
    def __init__(self):
        self.calls=[]
        self.block=False
        self.entered=threading.Event()
        self.canceled=threading.Event()
    async def extract(self,session):
        self.calls.append(session.id)
        self.entered.set()
        if self.block:
            try: await asyncio.sleep(30)
            except asyncio.CancelledError:
                self.canceled.set()
                raise
        intent,changes=merge_intent(session.intent,{'origin':'Berlin'})
        return IntentExtraction(state_patch={'origin':'Berlin'},blocking_question='Which dates?'),intent,changes


@pytest.fixture
def running(tmp_path,monkeypatch):
    monkeypatch.delenv('APP_ALLOWED_ORIGINS',raising=False)
    monkeypatch.setenv('TELEGRAM_BOT_TOKEN',TOKEN)
    monkeypatch.setattr('backend.app.main.instrument_app',lambda *a,**k:None)
    model=FixtureModel()
    def no_network(request): raise AssertionError('no provider/model HTTP permitted')
    app=create_app(database_path=str(tmp_path/'product.db'),transport=httpx.MockTransport(no_network),
        dependencies=AgentDependencies(model_factory=lambda *_:model,travel_data_factory=lambda *_:object()))
    with TestClient(app,base_url=ORIGIN) as client:
        yield app,client,model


def guest(client):
    response=client.post('/api/auth/guest',headers={'Origin':ORIGIN})
    assert response.status_code==200
    client.headers.update({'Origin':ORIGIN,'X-CSRF-Token':response.json()['csrf_token']})
    return response.json()['user']['id']


def wait_job(client,jid,status):
    deadline=time.monotonic()+2
    while time.monotonic()<deadline:
        result=client.get('/api/jobs/'+jid)
        assert result.status_code==200
        if result.json()['status']==status: return result.json()
        time.sleep(.01)
    pytest.fail('job did not reach '+status)


def test_real_guest_command_admission_validation_duplicate_and_worker(running):
    app,client,model=running
    assert client.post('/api/sessions',json={}).status_code==401
    guest(client)
    sid=client.post('/api/sessions',json={}).json()['id']
    path=f'/api/sessions/{sid}/messages'
    body={'content':'From Berlin','expected_revision':0}
    assert client.post(path,json=body,headers={'X-CSRF-Token':'bad','Idempotency-Key':'csrf-00000001'}).status_code==403
    assert client.post(path,json=body).status_code==422
    assert client.post(path,json={'content':'From Berlin'},headers={'Idempotency-Key':'revision-missing-0001'}).status_code==422
    assert model.calls==[]
    first=client.post(path,json=body,headers={'Idempotency-Key':'product-command-0001'})
    assert first.status_code==202 and first.json()['deduplicated'] is False
    job=wait_job(client,first.json()['job_id'],'completed')
    assert 'payload' not in job
    repeated=client.post(path,json=body,headers={'Idempotency-Key':'product-command-0001'})
    assert repeated.status_code==202 and repeated.json()['deduplicated'] is True
    assert repeated.json()['job_id']==first.json()['job_id']
    changed=client.post(path,json={**body,'content':'From Paris'},headers={'Idempotency-Key':'product-command-0001'})
    assert changed.status_code==409 and changed.json()['detail']['code']=='idempotency_conflict'
    stale=client.post(path,json=body,headers={'Idempotency-Key':'product-command-0002'})
    assert stale.status_code==409 and stale.json()['detail']['code']=='revision_mismatch'
    assert model.calls==[sid]
    current=client.get('/api/sessions/'+sid).json()
    assert current['intent']['origin']=='Berlin' and current['revision']==1
    assert sum(m['role']=='user' for m in current['messages'])==1
    assert app.state.command_worker is not None and not hasattr(app.state, 'tasks')


def test_other_owner_cannot_read_or_cancel_running_command_and_owner_can_cancel(running):
    app,client,model=running
    guest(client)
    owner_cookies=dict(client.cookies)
    owner_headers=dict(client.headers)
    sid=client.post('/api/sessions',json={}).json()['id']
    model.block=True
    admitted=client.post(f'/api/sessions/{sid}/messages',json={'content':'Plan','expected_revision':0},headers={'Idempotency-Key':'cancel-command-0001'})
    assert admitted.status_code==202
    jid=admitted.json()['job_id']
    assert model.entered.wait(1)
    client.cookies.clear();guest(client)
    assert client.get('/api/jobs/'+jid).status_code==404
    assert client.post('/api/jobs/'+jid+'/cancel').status_code==404
    assert client.post(f'/api/sessions/{sid}/messages',json={'content':'steal','expected_revision':0},headers={'Idempotency-Key':'foreign-command-0001'}).status_code==404
    client.cookies.clear();client.cookies.update(owner_cookies);client.headers.update(owner_headers)
    canceled=client.post('/api/jobs/'+jid+'/cancel')
    assert canceled.status_code==200 and canceled.json()['status']=='canceled'
    assert 'payload' not in canceled.json()
    assert model.canceled.wait(1)
    assert client.post('/api/jobs/'+jid+'/cancel').json()['status']=='canceled'
    assert wait_job(client,jid,'canceled')['status']=='canceled'
    assert app.state.store.get_session(sid).revision==0


def test_installed_telegram_login_uses_same_private_command_application_without_guest_merge(running):
    app,client,model=running
    guest_id=guest(client)
    guest_sid=client.post('/api/sessions',json={}).json()['id']
    launch=signed(stamp=int(time.time()))
    login=client.post('/api/auth/telegram',json={'init_data':launch},headers={'Origin':ORIGIN})
    assert login.status_code==200
    assert login.json()['user']['id']!=guest_id
    client.headers.update({'X-CSRF-Token':login.json()['csrf_token']})
    assert client.get('/api/sessions/'+guest_sid).status_code==404
    sid=client.post('/api/sessions',json={}).json()['id']
    accepted=client.post(f'/api/sessions/{sid}/messages',json={'content':'Berlin','expected_revision':0},headers={'Idempotency-Key':'telegram-command-0001'})
    assert accepted.status_code==202
    assert wait_job(client,accepted.json()['job_id'],'completed')['result_revision']==1
    assert model.calls==[sid]
