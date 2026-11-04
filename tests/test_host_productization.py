"""Mounted host fixture: trusted server principals, shared runtime and revoked SSE."""
import asyncio
import sqlite3
import threading
import time
from contextlib import asynccontextmanager
from datetime import timedelta

import httpx
from fastapi.testclient import TestClient
import pytest

from backend.app.application.dependencies import AgentDependencies
from backend.app.domain.identity import Principal
from backend.app.models import IntentExtraction,merge_intent
from backend.app.storage import Store
from backend.app.transport.session_events import session_events
from examples.host_integration import create_host


@pytest.mark.parametrize('mounted', [False, True], ids=['standalone', 'mounted'])
def test_full_six_stage_journey_foreign_job_receipt_replan_and_cancel(tmp_path, monkeypatch, mounted):
    """Same production planner/queue/HTTP catalog path on both application surfaces."""
    from backend.app.data_gateway import create_gateway_app
    from backend.app.infrastructure.http_travel_data import HttpTravelDataPort
    from backend.app.main import create_app
    from backend.app.models import local_today

    for key, value in {'TELEGRAM_MODE': 'disabled', 'TELEGRAM_BOT_TOKEN': '',
                       'OTEL_EXPORTER_OTLP_TRACES_ENDPOINT': '', 'LLM_API_KEY': '',
                       'OLLAMA_API_KEY': '', 'LOCAL_CATALOG_ENABLED': '1',
                       'WEB_FALLBACK_ENABLED': '0'}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr('backend.app.main.instrument_app', lambda *args, **kwargs: None)
    external_calls, data_calls = [], []
    counts = {'models': 0, 'intent': 0, 'model_close': 0, 'data_close': 0, 'store_close': 0}
    refs = {}

    def forbidden(request):
        external_calls.append(request.url.host)
        raise AssertionError('No external model/provider calls authorized')

    class Repository(Store):
        def close(self):
            counts['store_close'] += 1
            super().close()

    class Model:
        configured = True
        started = threading.Event()
        canceled = threading.Event()

        async def extract(self, current):
            counts['intent'] += 1
            command = [message.content for message in current.messages if message.role == 'user'][-1]
            if command == 'cancel wait':
                self.started.set()
                try:
                    await asyncio.Future()
                except asyncio.CancelledError:
                    self.canceled.set()
                    raise
            if command == 'clarify':
                patch, action, question = {'origin': 'Warsaw'}, 'clarify', 'Which dates and destination?'
            elif command == 'plan':
                start = local_today() + timedelta(days=60)
                patch = {'origin': 'Warsaw', 'destination': 'Larnaca', 'date_from': start.isoformat(),
                         'date_to': (start + timedelta(days=7)).isoformat(), 'adults': 2,
                         'children_ages': [6], 'budget_total': 2000, 'currency': 'EUR',
                         'pets': ['small dog 8 kg, cabin only, no cargo'], 'hotel_requirements': ['quiet']}
                action, question = 'explore', None
            else:
                assert command in {'move', 'move again'}
                patch = {'date_from': (current.intent.date_from + timedelta(days=1)).isoformat(),
                         'date_to': (current.intent.date_to + timedelta(days=1)).isoformat()}
                action, question = 'refine', None
            intent, changes = merge_intent(current.intent, patch)
            return IntentExtraction(state_patch=patch, action=action, blocking_question=question), intent, changes

        async def aclose(self):
            counts['model_close'] += 1

    gateway = create_gateway_app(database_path=str(tmp_path / 'gateway.db'), transport=httpx.MockTransport(forbidden))

    class Data(HttpTravelDataPort):
        failed = False

        async def _call(self, operation, data):
            data_calls.append(operation)
            return await super()._call(operation, data)

        async def resolve_place(self, name):
            if self.failed:
                raise RuntimeError('synthetic provider failure')
            return await super().resolve_place(name)

        async def aclose(self):
            counts['data_close'] += 1
            await self.client.aclose()

    def model_factory(client, store):
        counts['models'] += 1
        refs['model'], refs['store'] = Model(), store
        return refs['model']

    def data_factory(client, model):
        assert model is refs['model']
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=gateway))
        refs['data'] = Data(client, 'http://fixture-gateway')
        return refs['data']

    dependencies = AgentDependencies(repository_factory=Repository, model_factory=model_factory,
        travel_data_factory=data_factory, identity_resolver=lambda request: getattr(request.state, 'verified_principal', None))
    path = str(tmp_path / 'host.db')
    app = create_host(database_path=path, dependencies=dependencies) if mounted else create_app(
        database_path=path, transport=httpx.MockTransport(forbidden), dependencies=dependencies)
    agent = app.state.travel_agent if mounted else app
    prefix = '/travel' if mounted else ''
    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(owner):
        async with gateway.router.lifespan_context(gateway):
            async with original_lifespan(owner):
                yield

    app.router.lifespan_context = lifespan
    app.state.verified_subject = 'alice'

    @app.middleware('http')
    async def verified_host_middleware(request, call_next):
        # Test-controlled verified state, never public identity headers.
        subject = app.state.verified_subject
        user = refs['store'].resolve_identity('host:fixture-product', subject)
        request.state.verified_principal = Principal(user.user_id, 'host:fixture-product', subject)
        return await call_next(request)

    with TestClient(app, base_url='https://host.test') as client:
        def get(route):
            return client.get(prefix + route)

        def post(route, body, key=None):
            return client.post(prefix + route, json=body, headers={'Idempotency-Key': key} if key else {})

        def terminal(job_id, status='completed'):
            deadline = time.monotonic() + 5
            while True:
                response = get('/api/jobs/' + job_id)
                assert response.status_code == 200
                job = response.json()
                if job['status'] not in ('queued', 'running'):
                    assert job['status'] == status, job
                    return job
                assert time.monotonic() < deadline, job
                time.sleep(.005)

        created = post('/api/sessions', {})
        assert created.status_code == 201
        sid = created.json()['id']
        trip_path = '/api/sessions/' + sid

        def message(content, key, status='completed', **fields):
            revision = get(trip_path).json()['revision']
            response = post(trip_path + '/messages', {'content': content, 'expected_revision': revision, **fields}, key)
            assert response.status_code == 202, response.text
            job = terminal(response.json()['job_id'], status)
            return get(trip_path).json(), job

        # Stages 1/2: genuine clarification, then actual bundled catalog alternatives.
        clarified, _ = message('clarify', 'host-clarify-001')
        assert clarified['clarification'] and not clarified['options']
        planned, _ = message('plan', 'host-plan-0001')
        assert len(planned['options']) == 3
        assert len({option['stay']['details']['catalog_property_id'] for option in planned['options']}) == 3
        assert all(option['estimated_total'] is None and option['stay']['price'] is None for option in planned['options'])
        option = planned['options'][0]
        selected = post(trip_path + '/select', {'option_id': option['id'], 'expected_revision': planned['revision']})
        assert selected.status_code == 200

        # Stages 3/4: saved comparison and explanation consume no model/data work.
        before = selected.json()
        calls_before = (counts['intent'], len(data_calls))
        for action in ('compare', 'explain'):
            ids = [item['id'] for item in planned['options']] if action == 'compare' else [option['id']]
            answer, _ = message('saved evidence', 'host-saved-' + action, action=action, option_ids=ids)
            for field in ('revision', 'intent', 'options', 'selected_option_id'):
                assert answer[field] == before[field]
            assert answer['messages'][-1]['role'] == 'assistant'
        assert (counts['intent'], len(data_calls)) == calls_before

        # Stage 5: explicit fictional invoice/receipt; no real prices or booking.
        invoice = post(trip_path + '/checkout/quote', {'option_id': option['id'], 'expected_revision': planned['revision']})
        assert invoice.status_code == 200, invoice.text
        quote = invoice.json()
        assert quote['pricing_basis'] == 'synthetic_demo' and quote['source_total'] is None
        paid = post(trip_path + '/checkout', {'option_id': option['id'], 'expected_revision': planned['revision'], 'quote_id': quote['quote_id']})
        assert paid.status_code == 200, paid.text
        receipt = paid.json()
        assert receipt['status'] == 'simulated' and receipt['ready_to_book'] is False
        assert get(trip_path + '/receipts').json()['items'] == [receipt]

        # Stage 6: scope change preserves untouched wishes and rejects stale checkout.
        moved, _ = message('move', 'host-replan-001')
        for field in ('origin', 'destination', 'adults', 'children_ages', 'pets', 'budget_total', 'hotel_requirements'):
            assert moved['intent'][field] == planned['intent'][field]
        assert moved['revision'] == planned['revision'] + 1 and moved['selected_option_id'] is None
        assert moved['intent']['date_from'] != planned['intent']['date_from']
        assert post(trip_path + '/checkout', {'option_id': option['id'], 'expected_revision': planned['revision'], 'quote_id': quote['quote_id']}).status_code == 409
        refs['data'].failed = True
        failed, failed_job = message('move again', 'host-outage-001', status='failed')
        assert not failed['options'] and failed['previous_plan']['options'] == moved['options']
        assert failed['previous_plan']['intent'] == moved['intent'] and failed['selected_option_id'] is None
        assert get(trip_path + '/receipts').json()['items'] == [receipt]

        # Explicit foreign-owner API, SSE, jobs, cancel and receipts matrix.
        calls_before = (counts['intent'], len(data_calls))
        app.state.verified_subject = 'bob'
        for route in (trip_path, trip_path + '/events', trip_path + '/receipts', '/api/jobs/' + failed_job['id']):
            assert client.get(prefix + route, headers={'X-User-ID': 'alice'}).status_code == 404
        assert post('/api/jobs/' + failed_job['id'] + '/cancel', {}).status_code == 404
        assert post(trip_path + '/checkout', {'option_id': option['id'], 'expected_revision': failed['revision'], 'quote_id': quote['quote_id']}).status_code == 404
        assert (counts['intent'], len(data_calls)) == calls_before
        bob = post('/api/sessions', {})
        assert bob.status_code == 201 and bob.json()['id'] != sid

        # Cancellation propagates through the same singleton worker/model.
        app.state.verified_subject = 'alice'
        before_cancel = get(trip_path).json()
        ack = post(trip_path + '/messages', {'content': 'cancel wait', 'expected_revision': failed['revision']}, 'host-cancel-001')
        assert ack.status_code == 202
        assert refs['model'].started.wait(2)
        job_id = ack.json()['job_id']
        app.state.verified_subject = 'bob'
        assert get('/api/jobs/' + job_id).status_code == 404
        assert post('/api/jobs/' + job_id + '/cancel', {}).status_code == 404
        app.state.verified_subject = 'alice'
        assert post('/api/jobs/' + job_id + '/cancel', {}).status_code == 200
        terminal(job_id, 'canceled')
        assert refs['model'].canceled.wait(2)
        canceled = get(trip_path).json()
        for field in ('revision', 'intent', 'options', 'previous_plan'):
            assert canceled[field] == before_cancel[field]
        assert counts['models'] == 1 and agent.state.planner.llm is refs['model']
        assert not external_calls

    assert counts['model_close'] == counts['data_close'] == counts['store_close'] == 1


def test_host_two_verified_owners_share_one_model_and_close_resources_once(tmp_path,monkeypatch):
    monkeypatch.setattr('backend.app.main.instrument_app',lambda *a,**k:None)
    counts={'models':0,'model_close':0,'data_close':0,'store_close':0,'intent':0}
    refs={}
    class Repository(Store):
        def close(self): counts['store_close']+=1;super().close()
    class Model:
        async def extract(self,current):
            counts['intent']+=1
            intent,changes=merge_intent(current.intent,{'origin':'Warsaw'})
            return IntentExtraction(state_patch={'origin':'Warsaw'},blocking_question='Which dates?'),intent,changes
        async def aclose(self): counts['model_close']+=1
    class Data:
        async def aclose(self): counts['data_close']+=1
    def model(client,store):
        counts['models']+=1;refs['model']=Model();refs['store']=store
        return refs['model']
    def data(client,instance):
        assert instance is refs['model']
        return Data()
    host=create_host(database_path=str(tmp_path/'host.db'),dependencies=AgentDependencies(
        repository_factory=Repository,model_factory=model,travel_data_factory=data,
        identity_resolver=lambda request:getattr(request.state,'verified_principal',None)))
    host.state.verified_subject='alice'
    @host.middleware('http')
    async def trusted_fixture_identity(request,call_next):
        # Represents verified host middleware. Public headers cannot select a user.
        user=refs['store'].resolve_identity('fixture-host',host.state.verified_subject)
        request.state.verified_principal=Principal(user.user_id,'fixture-host',host.state.verified_subject)
        return await call_next(request)
    with TestClient(host,base_url='https://host.test') as client:
        ids=[]
        for subject in ('alice','bob'):
            host.state.verified_subject=subject
            response=client.post('/travel/api/sessions',json={},headers={'X-User-ID':'alice'})
            assert response.status_code==201
            sid=response.json()['id'];ids.append(sid)
            ack=client.post(f'/travel/api/sessions/{sid}/messages',json={'content':'Warsaw departure','expected_revision':0},headers={'Idempotency-Key':'host-journey-'+subject})
            assert ack.status_code==202
            import time
            deadline=time.monotonic()+1
            while client.get('/travel/api/jobs/'+ack.json()['job_id']).json()['status'] in ('queued','running'):
                assert time.monotonic()<deadline;time.sleep(.005)
            current=client.get('/travel/api/sessions/'+sid).json()
            assert current['intent']['origin']=='Warsaw' and current['clarification']
        assert counts['models']==1 and counts['intent']==2
        assert client.get('/travel/api/sessions/'+ids[0]).status_code==404
        assert client.get('/travel/api/sessions/'+ids[0]+'/events').status_code==404
        assert refs['store'].session_owner(ids[0])!=refs['store'].session_owner(ids[1])
        host.state.verified_subject='alice'
        assert client.get('/travel/api/sessions/'+ids[1]).status_code==404
        assert host.state.travel_agent.state.planner.llm is refs['model']
    assert counts['model_close']==counts['data_close']==counts['store_close']==1
    with pytest.raises(sqlite3.ProgrammingError): refs['store'].db.execute('SELECT 1')


@pytest.mark.asyncio
async def test_host_principal_revocation_closes_stream_before_next_private_snapshot(tmp_path,monkeypatch):
    monkeypatch.setattr('backend.app.main.instrument_app',lambda *a,**k:None)
    model=object()
    host=create_host(database_path=str(tmp_path/'revocation.db'),dependencies=AgentDependencies(
        model_factory=lambda *_:model,travel_data_factory=lambda client,llm:object(),
        identity_resolver=lambda request:getattr(request.state,'verified_principal',None)))
    async with host.router.lifespan_context(host):
        agent=host.state.travel_agent
        store=agent.state.store
        user=store.resolve_identity('fixture-host','alice')
        trip=agent.state.trips.for_principal(Principal(user.user_id)).create()
        stream=session_events(trip.id,agent.state.session_views,agent.state.session_notifications,
            heartbeat_seconds=.001,authorize=lambda:store.get_user(user.user_id) is not None)
        assert 'retry' in await anext(stream)
        assert trip.id in await anext(stream)
        with store.transaction():
            store.db.execute('UPDATE users SET deleted_at=? WHERE user_id=?',('2026-10-28T00:00:00+00:00',user.user_id))
        with pytest.raises(StopAsyncIteration): await anext(stream)
