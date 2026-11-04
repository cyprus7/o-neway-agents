"""Frozen feature019 evaluation. Fixture evidence is never live model quality."""
from __future__ import annotations
import argparse
from datetime import datetime,timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
CORPUS = ROOT / 'tests/agent_scenarios'
CORPUS_FILES = ('tuning.v1.json', 'held-out.v1.json')
CORPUS_CANONICALIZATION = 'utf8-lf-no-bom-v1'


def canonical_corpus_bytes(raw: bytes) -> bytes:
    """Preserve UTF-8 text bytes except CRLF->LF; reject BOM and lone CR."""
    if raw.startswith(b'\xef\xbb\xbf'):
        raise ValueError('corpus_utf8_bom_forbidden')
    text = raw.decode('utf-8', errors='strict').replace('\r\n', '\n')
    if '\r' in text:
        raise ValueError('corpus_lone_cr_forbidden')
    return text.encode('utf-8')


def corpus_fingerprints(directory=None):
    folder = Path(directory) if directory is not None else CORPUS
    raw = {name: (folder / name).read_bytes() for name in CORPUS_FILES}
    canonical = {name: canonical_corpus_bytes(value) for name, value in raw.items()}
    return {'canonicalization': CORPUS_CANONICALIZATION,
            'files': {name: hashlib.sha256(value).hexdigest() for name, value in canonical.items()},
            'raw_files': {name: hashlib.sha256(value).hexdigest() for name, value in raw.items()},
            'canonical_sha256': hashlib.sha256(b''.join(canonical.values())).hexdigest(),
            'raw_sha256': hashlib.sha256(b''.join(raw.values())).hexdigest()}


def verify_corpus_files(directory=None):
    folder = Path(directory) if directory is not None else CORPUS
    manifest = json.loads(canonical_corpus_bytes((folder / 'manifest.v1.json').read_bytes()).decode('utf-8'))
    if manifest.get('canonicalization') != CORPUS_CANONICALIZATION:
        raise ValueError('corpus_canonicalization_unsupported')
    observed = corpus_fingerprints(folder)
    if manifest.get('files') != observed['files']:
        raise ValueError('frozen_corpus_checksum_mismatch')
    return observed


def load_cases(directory=None):
    folder = Path(directory) if directory is not None else CORPUS
    verify_corpus_files(folder)
    return sum((json.loads(canonical_corpus_bytes((folder / name).read_bytes()).decode('utf-8'))
                for name in CORPUS_FILES), [])


def validate_corpus(cases):
    assert len(cases) == 30 and len({c['id'] for c in cases}) == 30
    assert sum(len(c['turns']) > 1 for c in cases) == 10
    assert sum(c['adversarial'] for c in cases) == 8
    assert {c['stage'] for c in cases} == {'clarify','explore','refine','compare','explain','purchase'}
    for case in cases:
        assert case['useful_outcome'] and case['hard_constraints'] and case['provenance_scope']
        assert case['split'] in {'tuning','held_out'}


def authenticate_guest(client):
    """Use the same guest cookie/CSRF contract as the ordinary Web client."""
    origin=str(client.base_url).rstrip("/")
    response=client.post("/api/auth/guest", headers={"Origin":origin})
    response.raise_for_status()
    client.headers.update({"Origin":origin,"X-CSRF-Token":response.json()["csrf_token"]})


def run_cases(client, cases, *, fixture=False, app=None):
    rows=[]
    for case in cases:
        row={'id':case['id'],'split':case['split'],'status':'pending','reason':'capability_not_implemented_in_baseline_adapter: '+case['capability'],
             'clarifications':0,'first_useful_recommendation_seconds':None,'connection_errors':0,'model_dispatches':0 if fixture else None,
             'tool_calls':0 if fixture else None,'fixture_intent_calls':0 if fixture else None,'failures':[]}
        rows.append(row)
        if case['capability'] != 'clarification':
            continue
        started=time.monotonic()
        try:
            response=client.post('/api/sessions',json={})
            response.raise_for_status()
            sid=response.json()['id']
            for turn_index,turn in enumerate(case['turns']):
                if fixture:
                    async def extract(session, turn=turn):
                        row['fixture_intent_calls'] += 1
                        from backend.app.models import IntentExtraction, merge_intent
                        intent, changes=merge_intent(session.intent, turn['fixture_patch'])
                        return IntentExtraction(state_patch=turn['fixture_patch'],blocking_question='Which missing trip detail can you provide?'),intent,changes
                    app.state.planner.llm.extract=extract
                current=client.get(f'/api/sessions/{sid}')
                current.raise_for_status()
                ack=client.post(f'/api/sessions/{sid}/messages',
                    json={'content':turn['input'],'expected_revision':current.json()['revision']},
                    headers={'Idempotency-Key':f'eval-{sid}-{case["id"]}-{turn_index}'})
                ack.raise_for_status()
                deadline=time.monotonic()+120
                while True:
                    result=client.get('/api/jobs/'+ack.json()['job_id'])
                    result.raise_for_status()
                    job=result.json()
                    if job['status'] not in ('running','queued'): break
                    if time.monotonic()>deadline: raise TimeoutError('job_timeout')
                    time.sleep(.01 if fixture else .25)
                if job['status']!='completed': row['failures'].append('job_not_completed')
                snapshot=client.get(f'/api/sessions/{sid}').json()
                for key,value in turn['expected_fields'].items():
                    if snapshot['intent'].get(key)!=value: row['failures'].append('retained_field:'+key)
                if not snapshot.get('clarification'): row['failures'].append('blocking_question_missing')
                else: row['clarifications']+=1
                if snapshot.get('options'): row['failures'].append('unexpected_inventory')
                if snapshot.get('selected_option_id') is not None: row['failures'].append('stale_selection')
            row['status']='failed' if row['failures'] else 'passed'
            row['reason']='scripted_extraction_application_path' if fixture else 'actual_api_exact_field_checks_only'
        except Exception as exc:
            row['status']='failed'
            row['failures'].append(type(exc).__name__)
            if isinstance(exc, (ConnectionError,TimeoutError)): row['connection_errors']+=1
        row['elapsed_seconds']=round(time.monotonic()-started,4)
    return rows


def fixture_run(cases):
    """Current adapters execute production routes and bundled source data only."""
    import httpx
    from fastapi.testclient import TestClient
    from backend.app.main import create_app
    from backend.app.application.dependencies import AgentDependencies
    from backend.app.providers import Providers, LocalTravelDataPort
    from backend.app.infrastructure.provider_cache import ProviderCache
    counts={'intent':0,'data':0,'telegram':0,'data_failures':0}
    telegram_payloads=[]
    class Model:
        configured=False
        async def completion(self,*a,**k): raise AssertionError('model_dispatch_forbidden')
    class Data:
        def __init__(self,base): self.base=base;self.fail=False
        def __getattr__(self,name):
            method=getattr(self.base,name)
            if not callable(method): return method
            async def counted(*a,**k):
                counts['data']+=1
                if self.fail and name=='resolve_place':
                    counts['data_failures']+=1
                    raise TimeoutError('fixture_timeout')
                return await method(*a,**k)
            return counted
        async def aclose(self): self.base.providers.store.close()
    def transport(request):
        if request.url.host=='api.telegram.org':
            counts['telegram']+=1
            telegram_payloads.append(json.loads(request.content))
            return httpx.Response(200,json={'ok':True,'result':{'message_id':1}})
        raise AssertionError('external_fixture_network_forbidden')
    environment={'LOCAL_CATALOG_ENABLED':'1','WEB_FALLBACK_ENABLED':'0','KEYLESS_PRICING_ENABLED':'0',
        'TELEGRAM_BOT_TOKEN':'123456:fixture_token_never_real','TELEGRAM_MODE':'webhook',
        'TELEGRAM_WEBHOOK_SECRET':'fixture_webhook_secret','APP_PUBLIC_URL':'https://travel.example',
        'APP_ALLOWED_ORIGINS':'https://localhost'}
    with tempfile.TemporaryDirectory() as directory,patch.dict(os.environ,environment),patch('backend.app.main.instrument_app',lambda *a,**k:None):
        data=[]
        def factory(client,model):
            port=Data(LocalTravelDataPort(Providers(client,ProviderCache(str(Path(directory)/'providers.db')))))
            data.append(port)
            return port
        app=create_app(database_path=str(Path(directory)/'fixture.sqlite3'),transport=httpx.MockTransport(transport),
            dependencies=AgentDependencies(model_factory=lambda *_:Model(),travel_data_factory=factory))
        app.state.fixture_telegram_payloads=telegram_payloads
        with TestClient(app,base_url='https://localhost') as client:
            authenticate_guest(client)
            rows=run_cases(client,cases,fixture=True,app=app)
            for row in rows:
                row['fixture_data_calls']=0
            _current_adapters(client,app,cases,rows,counts,data[0])
            return rows


def _current_adapters(client,app,cases,rows,counts,data):
    from datetime import datetime,timedelta,timezone
    from backend.app.models import IntentExtraction,merge_intent
    import hashlib,hmac
    from urllib.parse import urlencode
    serial=0
    def expect(response,status=200):
        assert response.status_code==status, f'http_{response.status_code}_expected_{status}'
        return response.json() if response.content else None
    def create(): return expect(client.post('/api/sessions',json={}),201)['id']
    def read(sid): return expect(client.get('/api/sessions/'+sid))
    def command(sid,text,patch=None,action=None,ids=None,terminal='completed'):
        nonlocal serial
        async def extract(current):
            counts['intent']+=1
            intent,changes=merge_intent(current.intent,patch or {})
            return IntentExtraction(state_patch=patch or {},action='refine'),intent,changes
        app.state.planner.llm.extract=extract
        serial+=1
        body={'content':text,'expected_revision':read(sid)['revision']}
        if action: body.update(action=action,option_ids=ids or [])
        headers={'Idempotency-Key':f'current-fixture-{serial:06}'}
        ack=expect(client.post(f'/api/sessions/{sid}/messages',json=body,headers=headers),202)
        deadline=time.monotonic()+5
        while True:
            job=expect(client.get('/api/jobs/'+ack['job_id']))
            if job['status'] not in ('queued','running'): break
            assert time.monotonic()<deadline,'fixture_job_timeout'
            time.sleep(.005)
        assert job['status']==terminal,'unexpected_terminal_state'
        duplicate=expect(client.post(f'/api/sessions/{sid}/messages',json=body,headers=headers),202)
        assert duplicate['deduplicated'] and duplicate['job_id']==job['id']
        current=read(sid)
        row['clarifications']+=int(bool(current.get('clarification')))
        return current,job
    seed={'origin':'Warsaw','destination':'Paphos','date_from':'2027-06-01','date_to':'2027-06-08','budget_total':1500}
    def planned():
        sid=create()
        result,job=command(sid,'Plan the supplied Paphos trip.',seed)
        assert len(result['options'])==3
        return sid,result,job
    def scope(result):
        assert all(o['estimated_total'] is None for o in result['options'])
        assert len({o['stay']['details']['catalog_property_id'] for o in result['options']})==len(result['options'])
        assert all(o['stay']['details']['catalog_retrieved_at'] and o['stay']['source_url'].startswith('https://www.openstreetmap.org/') for o in result['options'])
    def telegram_login(user):
        fields={'auth_date':str(int(time.time())),'user':json.dumps({'id':user})}
        secret=hmac.new(b'WebAppData',os.environ['TELEGRAM_BOT_TOKEN'].encode(),hashlib.sha256).digest()
        check='\n'.join(f'{k}={v}' for k,v in sorted(fields.items()))
        fields['hash']=hmac.new(secret,check.encode(),hashlib.sha256).hexdigest()
        result=expect(client.post('/api/auth/telegram',json={'init_data':urlencode(fields)}))
        client.headers.update({'X-CSRF-Token':result['csrf_token']})
        return result
    pending={}
    supported={'A13','A14','A15','A16','A17','A18','A19','A20','A21','A22','A23','A24','A25','A26','A27','A28','A29','A30'}
    for case,row in zip(cases,rows):
        if case['id'] in pending:
            row['reason']=pending[case['id']]
        if case['id'] not in supported: continue
        client.cookies.clear();authenticate_guest(client)
        before=counts.copy();started=time.monotonic()
        row.update(status='failed',reason='production_api_scripted_intent_and_source_catalog',failures=[])
        try:
            cid=case['id']
            if cid in {'A13','A14','A15','A16','A17','A25','A26','A28'}:
                sid,result,job=planned();scope(result)
                row['first_useful_recommendation_seconds']=round(time.monotonic()-started,4)
                if cid=='A13':
                    assert {o['destination'] for o in result['options']}=={'Пафос'}
                    assert len({o['stay']['source_url'] for o in result['options']})==3
                elif cid in {'A15','A16'}:
                    option=result['options'][0]['id']
                    expect(client.post(f'/api/sessions/{sid}/select',json={'option_id':option}))
                    original=read(sid);calls=counts.copy()
                    for turn in case['turns']:
                        current,_=command(sid,turn['input'],action='compare' if cid=='A15' else 'explain',ids=[o['id'] for o in result['options']][:2] if cid=='A15' else [option])
                        assert current['revision']==original['revision'] and current['intent']==original['intent']
                        assert current['selected_option_id']==option and current['options']==original['options']
                        assert current['options'][0]['stay']['source_url'] in current['messages'][-1]['content']
                    assert counts==calls
                elif cid in {'A14','A28'}:
                    if cid=='A14':
                        result,_=command(sid,'Add explicit family wishes.',{'pets':['dog'],'children_ages':[6]})
                    original=result
                    option=result['options'][0]['id']
                    expect(client.post(f'/api/sessions/{sid}/select',json={'option_id':option}))
                    patch={'date_from':'2027-06-11','date_to':'2027-06-18'}
                    data.fail=cid=='A28'
                    current,_=command(sid,case['turns'][0]['input'],patch,terminal='failed' if data.fail else 'completed')
                    data.fail=False
                    assert current['intent']['date_from']=='2027-06-11' and current['selected_option_id'] is None
                    if cid=='A14': assert current['intent']['pets']==['dog'] and current['intent']['children_ages']==[6]
                    else:
                        assert current['previous_plan']['intent']==original['intent']
                        assert current['previous_plan']['options']==original['options'] and not current['options']
                    expect(client.post(f'/api/sessions/{sid}/checkout',json={'option_id':option}),409)
                else:
                    option=result['options'][0]['id']
                    expect(client.post(f'/api/sessions/{sid}/select',json={'option_id':option}))
                    invoice=expect(client.post(f'/api/sessions/{sid}/checkout/quote',json={'option_id':option,'expected_revision':result['revision']}))
                    receipt=expect(client.post(f'/api/sessions/{sid}/checkout',json={'option_id':option,'expected_revision':result['revision'],'quote_id':invoice['quote_id']}))
                    assert receipt['status']=='simulated' and receipt['estimated_total'] is None and receipt['ready_to_book'] is False
                    assert expect(client.get(f'/api/sessions/{sid}/receipts'))['items']==[receipt]
                    assert read(sid)['options']==result['options']
                    if cid=='A25':
                        saved_counts=counts.copy()
                        draft=expect(client.post(f'/api/sessions/{sid}/drafts',json={'kind':'early_checkin','text':'Please draft an early check-in request; do not send.'}),201)
                        assert draft['status']=='draft'
                        aftercare=expect(client.get(f'/api/sessions/{sid}/aftercare'))
                        assert aftercare['drafts']==[draft] and aftercare['receipts']==[receipt]
                        assert counts['intent']==saved_counts['intent'] and counts['data']==saved_counts['data']
                        assert all('Please draft an early check-in' not in payload.get('text','') for payload in app.state.fixture_telegram_payloads)
                    elif cid=='A26':
                        async def extract(current):
                            counts['intent']+=1
                            return IntentExtraction(state_patch={},action='refine'),current.intent,[]
                        app.state.planner.llm.extract=extract
                        changed=expect(client.post(f'/api/sessions/{sid}/amend',json={'receipt_id':receipt['id'],'date_from':'2027-06-11','date_to':'2027-06-18'},headers={'Idempotency-Key':'amend-00000001'}),202)
                        child=changed['session']['id'];deadline=time.monotonic()+5
                        while expect(client.get('/api/jobs/'+changed['job_id']))['status'] in ('queued','running'):
                            assert time.monotonic()<deadline;time.sleep(.005)
                        assert expect(client.get('/api/jobs/'+changed['job_id']))['status']=='completed'
                        amendment=read(child)
                        assert child!=sid and amendment['intent']['date_from']=='2027-06-11'
                        assert app.state.store.db.execute('SELECT parent_trip_id FROM session_owners WHERE session_id=?',(child,)).fetchone()[0]==sid
                        assert len(amendment['options'])==1
                        assert amendment['options'][0]['stay']['details']['catalog_property_id']==result['options'][0]['stay']['details']['catalog_property_id']
                        assert amendment['options'][0]['id']!=result['options'][0]['id']
                        assert expect(client.get(f'/api/sessions/{sid}/receipts'))['items']==[receipt]
                        assert read(sid)['intent']==result['intent']
            elif cid=='A29':
                row['separate_fixture_intent_calls']=_restart_fixture_case()
                row['clarifications']+=1
                row['fixture_model_interruptions']=1
            elif cid=='A30':
                row['separate_fixture_intent_calls']=_host_fixture_case()
                row['clarifications']+=2
            elif cid in {'A18','A19'}:
                sid=create();result,job=command(sid,'Need departure.',{'pets':['dog']})
                saved=app.state.store.get_session(sid).model_dump(mode='json')
                client.cookies.clear();authenticate_guest(client)
                for path in [f'/api/sessions/{sid}',f'/api/sessions/{sid}/events',f'/api/jobs/{job["id"]}']:
                    expect(client.get(path),404)
                expect(client.post('/api/jobs/'+job['id']+'/cancel'),404)
                assert app.state.store.get_session(sid).model_dump(mode='json')==saved
            elif cid in {'A20','A21'}:
                sid=create()
                saved=expect(client.put('/api/me/memory/pets',json={'value':['dog'],'source_trip_id':sid}))
                assert saved['consented_at'] and saved['source_trip_id']==sid
                plain=create();assert read(plain)['intent']['pets']==[]
                remembered=expect(client.post('/api/sessions',json={'apply_memory':True}),201)
                assert remembered['intent']['pets']==['dog']
                if cid=='A21':
                    expect(client.delete('/api/me/memory/pets'),204)
                    current,_=command(remembered['id'],case['turns'][0]['input'],{**seed,'pets':[]})
                    assert current['intent']['pets']==[]
                    assert expect(client.get('/api/me/memory'))['items']==[]
                    assert expect(client.post('/api/sessions',json={'apply_memory':True}),201)['intent']['pets']==[]
            elif cid=='A22':
                sid=create()
                expect(client.put('/api/me/memory/origin',json={'value':'Warsaw','source_trip_id':sid}))
                exported=expect(client.get('/api/me/export'))
                assert len(exported['memory'])==1 and any(s['id']==sid for s in exported['sessions'])
                expect(client.delete('/api/sessions/'+sid),204)
                expect(client.delete('/api/me/memory/origin'),204)
                assert expect(client.get('/api/me/memory'))['items']==[]
                expect(client.get('/api/sessions/'+sid),404)
            elif cid=='A27':
                update={'update_id':300,'message':{'chat':{'id':333,'type':'private'},'from':{'id':333},'text':'/new'}}
                expect(client.post('/api/telegram/webhook',json=update,headers={'X-Telegram-Bot-Api-Secret-Token':os.environ['TELEGRAM_WEBHOOK_SECRET']}),202)
                telegram_login(333)
                sid=expect(client.get('/api/sessions'))['items'][0]['id']
                due=(datetime.now(timezone.utc)+timedelta(days=2)).replace(tzinfo=None).isoformat()
                reminder=expect(client.post(f'/api/sessions/{sid}/reminders',json={'local_time':due,'timezone':'UTC','consent':True,'text':'Fixture cancelable reminder marker'}),201)
                assert reminder['state']=='scheduled' and reminder['timezone']=='UTC' and reminder['consent']
                expect(client.delete(f'/api/sessions/{sid}/reminders/'+reminder['id']),204)
                listed=expect(client.get(f'/api/sessions/{sid}/aftercare'))['reminders']
                assert listed[0]['state']=='canceled' and listed[0]['attempts']==0
                saved_clock=app.state.reminder_service.clock
                app.state.reminder_service.clock=lambda:datetime.now(timezone.utc)+timedelta(days=3)
                try:
                    assert client.portal.call(app.state.reminder_service.send_once) is False
                finally:
                    app.state.reminder_service.clock=saved_clock
                assert all('Fixture cancelable reminder marker' not in payload.get('text','') for payload in app.state.fixture_telegram_payloads)
            else:
                user=111 if cid=='A23' else 222
                update={'update_id':100 if cid=='A23' else 200,'message':{'chat':{'id':user,'type':'private'},'from':{'id':user},'text':'Need a trip'}}
                async def extract(current):
                    counts['intent']+=1
                    return IntentExtraction(state_patch={},blocking_question='Which departure?',action='clarify'),current.intent,[]
                app.state.planner.llm.extract=extract
                header={'X-Telegram-Bot-Api-Secret-Token':os.environ['TELEGRAM_WEBHOOK_SECRET']}
                first=expect(client.post('/api/telegram/webhook',json=update,headers=header),202)
                if cid=='A23':
                    repeat=expect(client.post('/api/telegram/webhook',json=update,headers=header),202)
                    assert repeat['duplicate']
                login=telegram_login(user)
                trips=expect(client.get('/api/sessions'))['items'];assert len(trips)==1
                sid=trips[0]['id']
                assert app.state.store.session_owner(sid)==login['user']['id']
                deadline=time.monotonic()+3
                while expect(client.get('/api/jobs/'+first['job_id']))['status'] in ('queued','running'):
                    assert time.monotonic()<deadline;time.sleep(.005)
                assert len([m for m in read(sid)['messages'] if m['role']=='user'])==1
                row['clarifications']+=int(bool(read(sid).get('clarification')))
                row['reason']='bot_webhook_and_signed_miniapp_auth_fixture; no_browser_rendering'
            row['status']='passed'
        except Exception as exc:
            row['failures'].append(type(exc).__name__+(':'+str(exc) if isinstance(exc,AssertionError) else ''))
        finally:
            data.fail=False
            row['fixture_intent_calls']=counts['intent']-before['intent']
            row['fixture_data_calls']=counts['data']-before['data']
            row['fixture_telegram_http_calls']=counts['telegram']-before['telegram']
            row['fixture_tool_failures']=counts['data_failures']-before['data_failures']
            row['tool_calls']=row['fixture_data_calls']
            row['elapsed_seconds']=round(time.monotonic()-started,4)

def _restart_fixture_case():
    import asyncio,threading,httpx
    from fastapi.testclient import TestClient
    from backend.app.main import create_app
    from backend.app.application.dependencies import AgentDependencies
    from backend.app.models import IntentExtraction
    calls=[];entered=threading.Event()
    class Model:
        def __init__(self,block): self.block=block
        async def extract(self,current):
            calls.append('intent');entered.set()
            if self.block: await asyncio.sleep(30)
            return IntentExtraction(state_patch={},blocking_question='Which dates?',action='clarify'),current.intent,[]
    def app_for(path,block):
        return create_app(database_path=path,dependencies=AgentDependencies(model_factory=lambda *_:Model(block),travel_data_factory=lambda *_:object()),
            transport=httpx.MockTransport(lambda _:(_ for _ in ()).throw(AssertionError('network_forbidden'))))
    with tempfile.TemporaryDirectory() as directory,patch.dict(os.environ,{'TELEGRAM_MODE':'disabled'}):
        path=str(Path(directory)/'restart.db')
        with TestClient(app_for(path,True),base_url='https://localhost') as client:
            authenticate_guest(client)
            sid=client.post('/api/sessions',json={}).json()['id']
            body={'content':'Plan this trip','expected_revision':0};headers={'Idempotency-Key':'restart-original-0001'}
            ack=client.post(f'/api/sessions/{sid}/messages',json=body,headers=headers)
            assert ack.status_code==202 and entered.wait(1)
            jid=ack.json()['job_id'];cookies=dict(client.cookies)
        with TestClient(app_for(path,False),base_url='https://localhost') as client:
            client.cookies.update(cookies);authenticate_guest(client)
            assert client.get('/api/jobs/'+jid).json()['status']=='interrupted'
            duplicate=client.post(f'/api/sessions/{sid}/messages',json=body,headers=headers)
            assert duplicate.status_code==202 and duplicate.json()['job_id']==jid and duplicate.json()['deduplicated']
            assert len(calls)==1
            retry=client.post(f'/api/sessions/{sid}/messages',json=body,headers={'Idempotency-Key':'restart-explicit-0002'})
            assert retry.status_code==202 and retry.json()['job_id']!=jid
            deadline=time.monotonic()+2
            while client.get('/api/jobs/'+retry.json()['job_id']).json()['status'] in ('queued','running'):
                assert time.monotonic()<deadline;time.sleep(.005)
            assert client.get('/api/jobs/'+retry.json()['job_id']).json()['status']=='completed'
            assert len(calls)==2
    return 2


def _host_fixture_case():
    import httpx
    from fastapi.testclient import TestClient
    from backend.app.application.dependencies import AgentDependencies
    from backend.app.domain.identity import Principal
    from backend.app.models import IntentExtraction
    from examples.host_integration import create_host
    calls=[]
    class Model:
        async def extract(self,current):
            calls.append(current.id)
            return IntentExtraction(state_patch={},blocking_question='Which dates?',action='clarify'),current.intent,[]
    model=Model()
    with tempfile.TemporaryDirectory() as directory,patch.dict(os.environ,{'TELEGRAM_MODE':'disabled'}):
        host=create_host(database_path=str(Path(directory)/'host.db'),dependencies=AgentDependencies(model_factory=lambda *_:model,
            travel_data_factory=lambda client,llm:object() if llm is model else None,
            identity_resolver=lambda request:getattr(request.state,'trusted_principal',None)))
        host.state.verified_subject='alice'
        @host.middleware('http')
        async def trusted(request,call_next):
            store=host.state.travel_agent.state.store
            user=store.resolve_identity('fixture-host',host.state.verified_subject)
            request.state.trusted_principal=Principal(user.user_id,'fixture-host',host.state.verified_subject)
            return await call_next(request)
        with TestClient(host,base_url='https://localhost') as client:
            trips=[]
            for subject in ('alice','bob'):
                host.state.verified_subject=subject
                response=client.post('/travel/api/sessions',json={});assert response.status_code==201
                sid=response.json()['id'];trips.append(sid)
                ack=client.post(f'/travel/api/sessions/{sid}/messages',json={'content':'Need a trip','expected_revision':0},headers={'Idempotency-Key':'host-fixture-'+subject})
                assert ack.status_code==202
                deadline=time.monotonic()+2
                while client.get('/travel/api/jobs/'+ack.json()['job_id']).json()['status'] in ('queued','running'):
                    assert time.monotonic()<deadline;time.sleep(.005)
                result=client.get('/travel/api/sessions/'+sid).json();assert result['clarification'] and result['revision']==1
            assert client.get('/travel/api/sessions/'+trips[0]).status_code==404
            assert client.get('/travel/api/sessions/'+trips[0]+'/events').status_code==404
            assert len(calls)==2 and host.state.travel_agent.state.planner.llm is model
    return 2


def report(cases, rows, mode):
    sha=subprocess.check_output(['git','-c',f'safe.directory={ROOT.as_posix()}','rev-parse','HEAD'],cwd=ROOT,text=True).strip()
    fingerprints=verify_corpus_files()
    return {'schema':'agent-evaluation.v1','recorded_at':datetime.now(timezone.utc).isoformat(),'source_sha':sha,'working_tree':'shared checkout; SHA alone does not identify uncommitted changes',
            'environment':{'python':platform.python_version(),'platform':platform.system()},'evidence_class':mode,
            'corpus_sha256':fingerprints['canonical_sha256'],'corpus_fingerprints':fingerprints,
            'completion_denominator':30,'passed':sum(r['status']=='passed' for r in rows),
            'failed':sum(r['status']=='failed' for r in rows),'pending':sum(r['status']=='pending' for r in rows),
            'live_useful_target':27,'safety_failure_tolerance':0,'pricing_002_gate':'separate; not measured',
            'measurement_limits':'Current fixture uses scripted intent extraction and bundled source data; no live inference/provider verification. Fixture latency excludes model/network. Live mode only runs clarification adapter. Pending assertions are unexecuted, not passing.', 'cases':rows}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live',action='store_true',help='Explicitly opt into shared app API/model quota; later QA window only.')
    parser.add_argument('--base-url',default='http://localhost:8080')
    parser.add_argument('--output',type=Path)
    args=parser.parse_args()
    cases=load_cases(); validate_corpus(cases)
    if args.live:
        import httpx
        # Sequential requests through the actual app: never reads a provider key or creates an LLM client.
        username,password=os.getenv("DEMO_USERNAME"),os.getenv("DEMO_PASSWORD")
        if bool(username)!=bool(password):
            parser.error("Both DEMO_USERNAME and DEMO_PASSWORD must be provided for protected-site Basic Auth")
        auth=httpx.BasicAuth(username,password) if username and password else None
        with httpx.Client(base_url=args.base_url,timeout=130,auth=auth) as client:
            authenticate_guest(client)
            rows=run_cases(client,cases)
    else: rows=fixture_run(cases)
    result=report(cases,rows,'live_api' if args.live else 'model_free_fixture')
    rendered=json.dumps(result,indent=2)+'\n'
    if args.output: args.output.write_text(rendered,encoding='utf-8')
    else: print(rendered)
    return 1 if result['failed'] else 0

if __name__=='__main__': raise SystemExit(main())
