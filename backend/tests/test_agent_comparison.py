"""Saved-evidence actions and accepted-plan commit boundaries, without inference/data HTTP."""
from datetime import timedelta

import pytest

from backend.app.application.commands import active_command_id
from backend.app.models import IntentExtraction, merge_intent
from backend.app.planner import Planner
from backend.app.storage import Store
from backend.tests.test_pricing_core import prepared_session


class Forbidden:
    def __getattr__(self,name):
        raise AssertionError('unexpected model/provider access: '+name)


@pytest.fixture
def planning(tmp_path):
    snapshots=[]
    refs={}
    def changed(sid):
        if 'store' in refs:
            snapshots.append(refs['store'].read_session_snapshot(sid))
    store=Store(str(tmp_path/'actions.db'),on_change=changed)
    refs['store']=store
    owner=store.create_user().user_id
    session=prepared_session(store,refs=True,option_count=2)
    store.assign_legacy_trip(session.id,owner)
    session.selected_option_id=session.options[0].id
    session.accepted_plan_revision=session.revision
    session.options[0].stay.title='Synthetic quiet hotel'
    session.options[1].stay.title='Synthetic central hotel'
    store.save_session(session)
    snapshots.clear()
    try: yield store,owner,session,snapshots
    finally: store.close()


async def execute(store,planner,job):
    claimed=store.claim_next_command()
    assert claimed.id==job.id
    token=active_command_id.set(job.id)
    try: await planner.run(job.id)
    finally: active_command_id.reset(token)


@pytest.mark.asyncio
@pytest.mark.parametrize('action',['compare','explain'])
async def test_explicit_saved_action_retains_revision_selection_intent_and_source_bytes(planning,action):
    store,owner,session,snapshots=planning
    before=session.model_dump(mode='json')
    ids=[o.id for o in session.options] if action=='compare' else [session.selected_option_id]
    job,_=store.admit_message(owner,session.id,'Use saved evidence','saved-action-0001',session.revision,action=action,option_ids=ids)
    await execute(store,Planner(store,Forbidden(),Forbidden()),job)
    current=store.get_session(session.id)
    assert store.get_job(job.id).status=='completed'
    assert current.revision==session.revision and current.intent==session.intent
    assert current.selected_option_id==session.selected_option_id
    assert current.accepted_plan_revision==session.accepted_plan_revision
    assert current.model_dump(mode='json')['options']==before['options']
    answer=current.messages[-1].content
    assert current.options[0].stay.title in answer
    if action=='compare': assert current.options[1].stay.title in answer
    assert current.options[0].stay.quote.outcome=='reference'
    assert current.options[0].estimated_total is None
    assert all(snapshot.job['status']=='completed' for snapshot in snapshots if snapshot.session.messages[-1].role=='assistant')


@pytest.mark.asyncio
async def test_stale_explicit_option_ids_request_clarification_without_replacing_current_plan(planning):
    store,owner,session,_=planning
    job,_=store.admit_message(owner,session.id,'Compare old ids','invalid-saved-0001',session.revision,action='compare',option_ids=['r0-stale'])
    await execute(store,Planner(store,Forbidden(),Forbidden()),job)
    current=store.get_session(session.id)
    assert store.get_job(job.id).status=='completed'
    assert current.revision==session.revision and current.options==session.options
    assert current.selected_option_id==session.selected_option_id and current.intent==session.intent
    assert current.messages[-1].role=='assistant' and current.messages[-1].content
    assert all(option.stay.title not in current.messages[-1].content for option in session.options)


@pytest.mark.asyncio
async def test_model_compare_combined_with_patch_is_not_applied(planning):
    store,owner,session,_=planning
    calls=[]
    class MalformedProposal:
        async def extract(self,current):
            calls.append(current.id)
            patch={'destination':'Berlin'}
            intent,changes=merge_intent(current.intent,patch)
            return IntentExtraction(state_patch=patch,action='compare'),intent,changes
    job,_=store.admit_message(owner,session.id,'Compare and secretly change city','mixed-action-0001',session.revision)
    await execute(store,Planner(store,MalformedProposal(),Forbidden()),job)
    current=store.get_session(session.id)
    assert calls==[session.id] and store.get_job(job.id).status=='completed'
    assert current.intent==session.intent and current.revision==session.revision
    assert current.selected_option_id==session.selected_option_id and current.options==session.options
    assert current.messages[-1].role=='assistant'


@pytest.mark.asyncio
async def test_failed_replan_retains_prior_plan_scope_and_provenance_and_invalidates_selection(planning):
    store,owner,session,_=planning
    original=session.model_dump(mode='json')
    class ChangedDates:
        async def extract(self,current):
            patch={'date_from':str(current.intent.date_from+timedelta(days=1)),
                   'date_to':str(current.intent.date_to+timedelta(days=1))}
            intent,changes=merge_intent(current.intent,patch)
            return IntentExtraction(state_patch=patch,action='refine'),intent,changes
    class Unavailable:
        async def resolve_place(self,name): raise RuntimeError('synthetic provider failure')
    job,_=store.admit_message(owner,session.id,'Move dates one day','failed-replan-0001',session.revision)
    await execute(store,Planner(store,ChangedDates(),Unavailable()),job)
    current=store.get_session(session.id)
    assert store.get_job(job.id).status=='failed'
    assert current.revision==session.revision+1 and current.selected_option_id is None
    assert not current.options
    assert current.previous_plan.revision==session.revision
    assert current.previous_plan.model_dump(mode='json')['options']==original['options']
    assert current.previous_plan.intent==session.intent
    assert current.accepted_plan_revision==session.revision
    assert current.intent.origin==session.intent.origin and current.intent.destination==session.intent.destination


@pytest.mark.asyncio
async def test_failed_terminal_job_write_cannot_publish_final_answer_as_running(planning,monkeypatch):
    store,owner,session,snapshots=planning
    job,_=store.admit_message(owner,session.id,'Explain saved option','atomic-result-0001',session.revision,action='explain')
    original=store.save_job
    def fail_completed(value):
        if value.status=='completed': raise RuntimeError('synthetic terminal write failure')
        return original(value)
    monkeypatch.setattr(store,'save_job',fail_completed)
    await execute(store,Planner(store,Forbidden(),Forbidden()),job)
    current=store.get_session(session.id)
    assert store.get_job(job.id).status=='failed'
    assert current.revision==session.revision and current.options==session.options
    assert store.metrics().get('jobs_completed',0)==0
    assert len([message for message in current.messages if message.role=='assistant'])==1
    assert session.options[0].stay.title not in current.messages[-1].content
    assistant_snapshots=[snap for snap in snapshots if snap.session.messages and snap.session.messages[-1].role=='assistant']
    assert assistant_snapshots and all(snap.job['status']=='failed' for snap in assistant_snapshots)


@pytest.mark.asyncio
async def test_actual_planner_command_allows_one_price_batch_across_parallel_shortlists(tmp_path):
    import asyncio
    import json
    from backend.app.application.commands import price_research_budget
    from backend.app.domain.travel_data import ComponentBundle, PriceDocumentsData
    from backend.app.geography import find_place
    from backend.app.infrastructure.knowledge_travel_data import KnowledgeTravelDataPort
    from backend.app.models import TravelComponent, TravelIntent
    from backend.tests.test_knowledge_prices import document
    store=Store(str(tmp_path/'price-budget.db'))
    owner=store.create_user().user_id
    session=store.create_owned_session(owner)
    session.intent=TravelIntent(origin='Warsaw',date_from='2027-06-01',date_to='2027-06-08',budget_total=1000)
    store.save_session(session)
    calls=[]
    lookups=[]
    class Base:
        async def resolve_place(self,name): return find_place(name)
        async def weather(self,*args): return {'kind':'unknown'}
        async def search(self,*args): return []
        async def inventory_candidates(self,place,origin,intent,*,limit):
            assert limit==3
            return [ComponentBundle(flight=TravelComponent(title='Flight',description='Unknown'),
                stay=TravelComponent(title=place.name+' Hotel '+str(i),description='Synthetic',source_url='https://synthetic.example/'+place.name+'/'+str(i)),
                transfer=TravelComponent(title='Transfer',description='Unknown')) for i in range(3)]
        async def price_documents(self,place,origin,intent,bundle):
            calls.append('documents')
            await asyncio.sleep(.01)  # Reservation must precede concurrent awaits.
            return PriceDocumentsData(documents=[document('flight','Synthetic flight EUR 100 per person',place,origin,intent,bundle)])
    class Model:
        configured=True
        async def extract(self,current):
            calls.append('intent')
            return IntentExtraction(state_patch={},action='explore'),current.intent,[]
        async def completion(self,messages,*args,**kwargs):
            calls.append('extraction')
            assert len(json.loads(messages[1]['content'])['documents'])==1
            return '{"prices":[]}'
    class RecordingKnowledge(KnowledgeTravelDataPort):
        async def _lookup(self,place,*args,**kwargs):
            lookups.append(place.name)
            return await super()._lookup(place,*args,**kwargs)
    model=Model()
    planner=Planner(store,model,RecordingKnowledge(Base(),model))
    async def shortlist(*args): return [find_place(name) for name in ('Berlin','Larnaca','Athens')]
    planner.candidates=shortlist
    job,_=store.admit_message(owner,session.id,'Explore three cities','shortlist-budget-0001',session.revision)
    try:
        assert price_research_budget.get() is None
        await execute(store,planner,job)
        assert store.get_job(job.id).status=='completed', (calls,lookups,store.get_job(job.id).error)
        assert len(lookups)==3 and len(set(lookups))==3
        assert calls.count('intent')==calls.count('documents')==calls.count('extraction')==1
        assert price_research_budget.get() is None
        current=store.get_session(session.id)
        assert len(current.options)==3 and len({o.destination for o in current.options})==3
        assert all(o.estimated_total is None for o in current.options)
    finally:
        store.close()
