"""T022 in-process production ASGI/SQLite fixture load; never live inference."""
from __future__ import annotations
import argparse
import asyncio
from collections import Counter
from contextlib import AsyncExitStack
from datetime import datetime,timezone
import json
import math
import os
from pathlib import Path
import platform
import subprocess
import sys
import time
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))


def timing(samples):
    ordered=sorted(samples)
    def percentile(fraction):
        return ordered[max(0,math.ceil(len(ordered)*fraction)-1)]*1000 if ordered else None
    return {'count':len(samples),'p50_ms':percentile(.50),'p95_ms':percentile(.95),'max_ms':max(samples)*1000 if samples else None}


def assess_limits(reads,acks):
    return {'read_p95_le_500ms':reads['p95_ms'] is not None and reads['p95_ms']<=500,
            'ack_p95_le_1000ms':acks['p95_ms'] is not None and acks['p95_ms']<=1000,
            'ack_max_le_1000ms':acks['max_ms'] is not None and acks['max_ms']<=1000}


async def run_load():
    import tempfile
    import httpx
    from backend.app.application.dependencies import AgentDependencies
    from backend.app.main import create_app
    from backend.app.models import IntentExtraction
    gate=asyncio.Event()
    entered=asyncio.Event()
    model_order=[]
    owners={};sessions={}
    dispatches={'external_http':0,'model_completion':0,'fixture_intent':0,'data':0}
    read_times=[];ack_times=[];read_status=Counter();ack_status=Counter()
    jobs={};failures=[];queue_samples=[]
    class Model:
        async def extract(self,session):
            dispatches['fixture_intent']+=1
            model_order.append(owners[session.id])
            entered.set()
            await gate.wait()
            return IntentExtraction(state_patch={},blocking_question='Which dates?',action='clarify'),session.intent,[]
        async def completion(self,*a,**k):
            dispatches['model_completion']+=1
            raise AssertionError('model_completion_forbidden')
    class Data:
        def __getattr__(self,name):
            dispatches['data']+=1
            raise AssertionError('data_dispatch_forbidden')
        async def aclose(self): pass
    def reject_http(request):
        dispatches['external_http']+=1
        raise AssertionError('external_http_forbidden')
    def sample(store):
        values=dict(store.db.execute('SELECT status,count(*) FROM commands GROUP BY status').fetchall())
        queue_samples.append({'queued':values.get('queued',0),'running':values.get('running',0)})
    def check(condition,label):
        if not condition: failures.append(label)
    with tempfile.TemporaryDirectory() as directory,patch.dict(os.environ,{'TELEGRAM_MODE':'disabled'}),patch('backend.app.main.instrument_app',lambda *a,**k:None):
        app=create_app(database_path=str(Path(directory)/'load.db'),transport=httpx.MockTransport(reject_http),
            dependencies=AgentDependencies(model_factory=lambda *_:Model(),travel_data_factory=lambda *_:Data()))
        async with app.router.lifespan_context(app),AsyncExitStack() as resources:
            clients=[];owner_ids={}
            for number in range(5):
                client=await resources.enter_async_context(httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://localhost'))
                login=await client.post('/api/auth/guest',json={},headers={'Origin':'http://localhost'})
                login.raise_for_status()
                client.headers.update({'Origin':'http://localhost','X-CSRF-Token':login.json()['csrf_token']})
                created=await client.post('/api/sessions',json={});created.raise_for_status()
                sid=created.json()['id'];label=f'owner-{number+1}'
                sessions[label]=sid;owners[sid]=label;owner_ids[label]=login.json()['user']['id'];clients.append(client)
            check(len(set(owner_ids.values()))==5,'five_distinct_guest_owners')
            foreign=await clients[0].get('/api/sessions/'+sessions['owner-2'])
            check(foreign.status_code==404,'cross_owner_read_hidden')
            ready=asyncio.Event()
            async def sender(number):
                await ready.wait()
                label=f'owner-{number+1}';sid=sessions[label]
                current=await clients[number].get('/api/sessions/'+sid);current.raise_for_status()
                body={'content':'Synthetic fixture trip','expected_revision':current.json()['revision']}
                key='load-command-'+label
                started=time.perf_counter()
                response=await clients[number].post('/api/sessions/'+sid+'/messages',json=body,headers={'Idempotency-Key':key})
                ack_times.append(time.perf_counter()-started);ack_status[response.status_code]+=1
                if response.status_code==202: jobs[label]=(response.json()['job_id'],body,key)
                else: failures.append('command_admission_'+label)
                sample(app.state.store)
            async def reader(number):
                await ready.wait()
                client=clients[number%5];sid=sessions[f'owner-{number%5+1}']
                for _ in range(20):
                    started=time.perf_counter()
                    response=await client.get('/api/sessions/'+sid)
                    read_times.append(time.perf_counter()-started);read_status[response.status_code]+=1
                    if response.status_code!=200 or response.json().get('id')!=sid:
                        failures.append('reader_owner_or_status')
                    sample(app.state.store)
            tasks=[asyncio.create_task(sender(i)) for i in range(5)]+[asyncio.create_task(reader(i)) for i in range(20)]
            ready.set()
            try:
                await asyncio.wait_for(asyncio.gather(*tasks),15)
                await asyncio.wait_for(entered.wait(),1)
                sample(app.state.store)
                check(len(read_times)==400,'400_completed_reads')
                check(dispatches['fixture_intent']==1,'one_gated_worker_during_reads')
                check(all(app.state.store.get_job(value[0]).status in ('queued','running') for value in jobs.values()),'no_completion_before_read_gate')
            finally:
                gate.set()
            deadline=time.monotonic()+3
            while any(app.state.store.get_job(value[0]).status in ('queued','running') for value in jobs.values()):
                if time.monotonic()>deadline:
                    failures.append('queue_drain_timeout');break
                sample(app.state.store);await asyncio.sleep(.005)
            terminal={label:app.state.store.get_job(value[0]).status for label,value in jobs.items()}
            check(len(jobs)==5 and all(state=='completed' for state in terminal.values()),'five_terminal_completions')
            check(Counter(model_order)==Counter({f'owner-{i+1}':1 for i in range(5)}),'each_owner_executes_once')
            if model_order:
                first=model_order[0]
                remaining=sorted((label for label in owner_ids if label!=first),key=lambda label:(owner_ids[label]<=owner_ids[first],owner_ids[label]))
                expected=[first,*remaining]
                check(model_order==expected,'cyclic_owner_fairness_order')
            else: expected=[]
            for number in range(5):
                label=f'owner-{number+1}'
                if label not in jobs: continue
                jid,body,key=jobs[label];sid=sessions[label]
                repeat=await clients[number].post('/api/sessions/'+sid+'/messages',json=body,headers={'Idempotency-Key':key})
                check(repeat.status_code==202 and repeat.json().get('job_id')==jid and repeat.json().get('deduplicated') is True,'duplicate_same_job_'+label)
                current=(await clients[number].get('/api/sessions/'+sid)).json()
                check(current['revision']==1 and len([m for m in current['messages'] if m['role']=='user'])==1,'one_committed_result_'+label)
            check(dispatches['fixture_intent']==5,'five_model_fixture_calls_only')
    reads=timing(read_times);acks=timing(ack_times);limits=assess_limits(reads,acks)
    maxima={name:max((point[name] for point in queue_samples),default=0) for name in ('queued','running')}
    check(maxima['running']<=1,'singleton_running_bound')
    check(maxima['queued']<=32,'global_queue_bound')
    check(dispatches['external_http']==dispatches['model_completion']==dispatches['data']==0,'no_external_or_provider_dispatch')
    for name,passed in limits.items():
        check(passed,name)
    return {'schema':'product-load.v1','recorded_at':datetime.now(timezone.utc).isoformat(),
        'evidence_class':'in_process_production_ASGI_SQLite_scripted_model','environment':{'python':platform.python_version(),'platform':platform.platform(),
            'machine':platform.machine(),'processor':platform.processor(),'logical_cpus':os.cpu_count()},
        'workload':{'owners':5,'concurrent_senders':5,'parallel_readers':20,'reads_per_reader':20,'total_reads':400},
        'reads':reads,'acknowledgements':acks,'read_statuses':dict(read_status),'ack_statuses':dict(ack_status),
        'queue_maxima':maxima,'fairness_order':model_order,'expected_fairness_order':expected,'terminal_statuses':terminal,
        'dispatch_counts':dispatches,'thresholds':limits,'failures':failures,'passed':not failures,
        'raw':{'read_seconds':read_times,'ack_seconds':ack_times,'queue_samples':queue_samples},
        'limits':'ASGI requests have no TCP/TLS/browser/network latency. Single-machine fixture measurement, not a public server SLO.'}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path)
    args=parser.parse_args()
    result=asyncio.run(run_load())
    result['source_sha']=subprocess.check_output(['git','-c',f'safe.directory={ROOT.as_posix()}','rev-parse','HEAD'],cwd=ROOT,text=True).strip()
    result['source_state']='shared working checkout; uncommitted integration changes'
    path=args.output or ROOT/'tmp/product-load'/datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ.json')
    path.parent.mkdir(parents=True,exist_ok=True);path.write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({k:v for k,v in result.items() if k not in ('raw','environment')},indent=2))
    return 0 if result['passed'] else 1

if __name__=='__main__': raise SystemExit(main())
