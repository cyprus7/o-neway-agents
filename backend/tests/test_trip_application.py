"""T003 channel-independent commands and explicit resource ownership."""
from datetime import timedelta
import sqlite3

import pytest

from backend.app.application.dependencies import AgentDependencies
from backend.app.application.commands import CommandError
from backend.app.application.errors import ApplicationError
from backend.app.application.trips import TripApplication
from backend.app.checkout import CheckoutInput, QuoteInput
from backend.app.domain.identity import Principal
from backend.app.main import create_app
from backend.app.models import PriceRefreshInput, TravelComponent, TravelIntent, TripOption, local_today
from backend.app.storage import Store


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', [None, 'factory', 'body', 'close'])
async def test_injected_resources_share_one_model_and_close_once(tmp_path, failure, monkeypatch):
    monkeypatch.setattr('backend.app.main.instrument_app', lambda *a, **k: None)
    calls={'store':0,'model':0,'data':0}
    refs={}
    class RecordingStore(Store):
        def close(self):
            calls['store']+=1
            super().close()
    class Model:
        async def aclose(self): calls['model']+=1
    class Data:
        async def aclose(self):
            calls['data']+=1
            if failure=='close': raise RuntimeError('close_failure')
    def repository(path, **kwargs):
        refs['store']=RecordingStore(path,**kwargs)
        return refs['store']
    def model(client,store):
        assert store is refs['store']
        assert 'model' not in refs
        refs['client']=client
        refs['model']=Model()
        return refs['model']
    def data(client,llm):
        assert client is refs['client'] and llm is refs['model']
        if failure=='factory': raise RuntimeError('factory_failure')
        refs['data']=Data()
        return refs['data']
    app=create_app(database_path=str(tmp_path/'owned.db'),dependencies=AgentDependencies(
        repository_factory=repository,model_factory=model,travel_data_factory=data))
    async def run():
        async with app.router.lifespan_context(app):
            assert app.state.llm is refs['model'] is app.state.planner.llm
            assert app.state.travel_data is refs['data'] is app.state.planner.providers
            if failure=='body': raise RuntimeError('body_failure')
    if failure:
        with pytest.raises(RuntimeError,match=failure+'_failure'): await run()
    else: await run()
    assert calls=={'store':1,'model':1,'data':0 if failure=='factory' else 1}
    assert refs['client'].is_closed
    with pytest.raises(sqlite3.ProgrammingError): refs['store'].db.execute('SELECT 1')


@pytest.mark.asyncio
async def test_overlapping_lifespan_rejected_without_destroying_first_runtime(tmp_path, monkeypatch):
    monkeypatch.setattr('backend.app.main.instrument_app', lambda *a, **k: None)
    class Resource:
        closes=0
        async def aclose(self): self.closes+=1
    model,data=Resource(),Resource()
    app=create_app(database_path=str(tmp_path/'overlap.db'),dependencies=AgentDependencies(
        model_factory=lambda *_:model,travel_data_factory=lambda *_:data))
    async with app.router.lifespan_context(app):
        with pytest.raises(RuntimeError,match='already started'):
            async with app.router.lifespan_context(app): pytest.fail('must reject overlapping startup')
        user=app.state.store.create_user()
        assert app.state.trips.for_principal(Principal(user.user_id)).create().id
        assert model.closes==data.closes==0
    assert model.closes==data.closes==1


@pytest.fixture
def trips(tmp_path):
    store=Store(str(tmp_path/'commands.db'))
    app=TripApplication(store, Principal(store.create_user().user_id))
    try: yield app,store
    finally: store.close()


def seed_option(app,store):
    session=app.create()
    session.revision=1
    session.intent=TravelIntent(origin='Berlin',destination='Paphos',date_from=local_today()+timedelta(days=40),date_to=local_today()+timedelta(days=47))
    component=TravelComponent(title='Synthetic',description='Unknown')
    session.options=[TripOption(id='r1-paphos',destination='Paphos',country='Cyprus',subtitle='Fixture',latitude=34.8,longitude=32.4,score=80,currency='EUR',price_status='unavailable',flight=component,stay=component,transfer=component,reasons=[],tradeoffs=[],risks=[],evidence=[],constraint_checks=[])]
    store.save_session(session)
    return session


def test_application_message_and_refresh_errors_match_commands_without_http(trips):
    app,store=trips
    with pytest.raises(ApplicationError) as missing: app.message('missing','hello',key='missing-trip-0001',expected_revision=0)
    assert missing.value.status_code==404
    session=app.create()
    job,dedup=app.message(session.id,'Berlin to Paphos',key='message-trip-0001',expected_revision=0)
    assert dedup is False
    assert app.job(job.id).id==job.id
    with pytest.raises(CommandError) as busy:
        app.message(session.id,'second',key='message-trip-0002',expected_revision=0)
    assert busy.value.code=='session_busy'
    other=app.create()
    with pytest.raises(CommandError) as stale:
        app.refresh(other.id,PriceRefreshInput(expected_revision=99),key="refresh-trip-0001")
    assert stale.value.code=='revision_mismatch'


def test_application_checkout_is_atomic_and_idempotent_without_http(trips,monkeypatch):
    app,store=trips
    session=seed_option(app,store)
    selected=app.select(session.id,session.options[0].id)
    invoice=app.quote(session.id,QuoteInput(option_id=selected.selected_option_id,expected_revision=1))
    body=CheckoutInput(option_id=selected.selected_option_id,expected_revision=1,quote_id=invoice['quote_id'])
    before=store.read_session_snapshot(session.id).sequence
    original_event=store.add_event
    def fail_event(*args,**kwargs): raise RuntimeError('event_write_failed')
    monkeypatch.setattr(store,'add_event',fail_event)
    with pytest.raises(RuntimeError,match='event_write_failed'): app.checkout(session.id,body)
    assert store.find_checkout(session.id,1,selected.selected_option_id) is None
    assert store.metrics().get('simulated_checkouts',0)==0
    assert store.read_session_snapshot(session.id).sequence==before
    monkeypatch.setattr(store,'add_event',original_event)
    first=app.checkout(session.id,body)
    assert first['status']=='simulated' and first['estimated_total'] is None
    assert app.checkout(session.id,body)['id']==first['id']
    assert store.metrics()['simulated_checkouts']==1
    current=store.get_session(session.id);current.revision+=1;store.save_session(current)
    with pytest.raises(ApplicationError) as stale: app.checkout(session.id,body)
    assert stale.value.detail['code']=='revision_mismatch'



def test_fixture_factory_uses_real_isolated_owners_and_preserves_custom_dependencies(tmp_path):
    from fastapi.testclient import TestClient
    from backend.tests.factories import create_app as fixture_app
    from backend.tests.factories import FixtureStore
    class Data:
        async def aclose(self): pass
    first=fixture_app(database_path=str(tmp_path/'first.db'),travel_data_factory=lambda _:Data())
    second=fixture_app(database_path=str(tmp_path/'second.db'),travel_data_factory=lambda _:Data())
    with TestClient(first) as left,TestClient(second) as right:
        one=left.post('/api/sessions',json={})
        assert one.status_code==201
        sid=one.json()['id']
        assert first.state.store.session_owner(sid)==first.state.store.fixture_user.user_id
        assert first.state.store.fixture_user.user_id!=second.state.store.fixture_user.user_id
        assert right.get('/api/sessions/'+sid).status_code==404
    refs={}
    def repository(path,**kwargs):
        refs['store']=Store(path,**kwargs)
        refs['user']=refs['store'].create_user()
        return refs['store']
    def resolver(request): return Principal(refs['user'].user_id)
    model,data=object(),Data()
    dependencies=AgentDependencies(repository_factory=repository,model_factory=lambda *_:model,
        travel_data_factory=lambda client,llm:data if llm is model else pytest.fail('wrong model'),identity_resolver=resolver)
    custom=fixture_app(database_path=str(tmp_path/'custom.db'),dependencies=dependencies)
    with TestClient(custom) as client:
        response=client.post('/api/sessions',json={})
        assert response.status_code==201
        assert custom.state.store is refs['store'] and not isinstance(refs['store'],FixtureStore)
        assert custom.state.identity_resolver is resolver
        assert custom.state.llm is model and custom.state.travel_data is data
        assert refs['store'].session_owner(response.json()['id'])==refs['user'].user_id
