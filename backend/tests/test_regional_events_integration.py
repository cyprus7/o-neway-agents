"""Synthetic integration checks: optional context never becomes price/constraint evidence."""
import asyncio
from datetime import datetime, timedelta, timezone
import json

import pytest

from backend.app.application.session_views import SessionViews, public_session_data
from backend.app.geography import find_place
from backend.app.models import IntentExtraction, Session, TravelIntent, TripOption
from backend.app.planner import Planner
from backend.app.providers import lodging_result_matches
from backend.app.regional_events import EventResult
from backend.app.storage import Store
from backend.app.transport.session_events import session_events
from backend.app.infrastructure.session_notifications import SessionNotifications
from backend.tests.test_pricing_core import prepared_session
from backend.tests.test_pricing_refresh import FakePricing


def fixture_result(session, *, checked=None, retrieved=None):
    now = datetime.now(timezone.utc)
    return EventResult.model_validate({"status":"ready","checked_at":(checked or now).isoformat(),"items":[{
        "id":"synthetic-event","title":"Synthetic fixture — city exhibition",
        "start_date":str(session.intent.date_from),"end_date":str(session.intent.date_to),
        "location_name":"Synthetic venue","location_city":"Athens","category":"exhibition",
        "significance":"experience","reason":"Synthetic fixture only; not an actual listing.",
        "source":{"provider":"Synthetic fixture","source_url":"https://goturkiye.com/synthetic-fixture",
                  "retrieved_at":(retrieved or checked or now).isoformat(),"freshness":"live"},
        "verification":"source_reported"}]})


def test_city_festival_cannot_qualify_as_a_lodging_source():
    place=find_place('Prague')
    assert not lodging_result_matches({'title':'Prague international festival', 'url':'https://eaff.example/events/prague'},place,'Warsaw')
    assert lodging_result_matches({'title':'Hotels near Prague festival', 'url':'https://lodging.example/prague-hotels'},place,'Warsaw')


@pytest.fixture
def store(tmp_path):
    value=Store(str(tmp_path/'events.db'))
    yield value
    value.close()


def test_legacy_option_load_and_event_roundtrip_do_not_change_price_or_constraints(store):
    session=prepared_session(store,refs=True)
    original=session.options[0].model_dump(mode='json')
    legacy=dict(original);legacy.pop('regional_events')
    assert TripOption.model_validate(legacy).regional_events.status=='not_checked'
    session.options[0].regional_events=fixture_result(session)
    store.save_session(session)
    stored=store.get_session(session.id).options[0]
    assert stored.regional_events.items[0].id=='synthetic-event'
    current=stored.model_dump(mode='json');current.pop('regional_events');original.pop('regional_events')
    assert current==original


def test_expired_cache_fact_is_not_rejuvenated_by_a_new_checked_at_and_sse_gets_deadline(store):
    session=prepared_session(store)
    now=datetime.now(timezone.utc)
    session.options[0].regional_events=fixture_result(session,checked=now,retrieved=now-timedelta(minutes=59))
    store.save_session(session)
    view=SessionViews(store).read(session.id,now=now)
    assert view.next_expiry<=now+timedelta(minutes=1)
    projected=public_session_data(session,now=now+timedelta(minutes=2))
    assert projected['options'][0]['regional_events']['status']=='unavailable'
    assert projected['options'][0]['regional_events']['items']==[]
    assert store.get_session(session.id).options[0].regional_events.status=='ready'
    assert store.get_session(session.id).options[0].regional_events.items[0].source.retrieved_at==(now-timedelta(minutes=59)).isoformat()


def test_price_refresh_preserves_event_identity_without_generation_or_event_search(store):
    session=prepared_session(store)
    session.options[0].regional_events=fixture_result(session);store.save_session(session)
    original=session.options[0].regional_events.model_dump()
    job,_=store.start_refresh(session.id,session.revision)
    class Providers:
        pricing=FakePricing()
        async def resolve_place(self,name):return find_place(name)
        async def refresh_inventory(self,place,origin,intent,current):
            from backend.app.domain.travel_data import ComponentBundle
            result = ComponentBundle.from_components(current)
            await self.pricing.apply(place, origin, intent, tuple(result), refresh=True)
            return result
        async def regional_events(self,*args):raise AssertionError('Price refresh must not rediscover events')
    asyncio.run(Planner(store,None,Providers()).refresh_prices(job.id))
    assert store.get_job(job.id).status=='completed'
    assert store.get_session(session.id).options[0].regional_events.model_dump()==original


def test_optional_source_error_does_not_fail_trip_or_modify_scores_and_hotel_evidence(store):
    session=prepared_session(store)
    component_template=session.options[0]
    class Providers:
        async def weather(self,*args):return {'kind':'unknown','evidence':[]}
        async def search(self,*args):return []
        async def inventory(self,*args):return tuple(getattr(component_template,key).model_copy(deep=True) for key in ('flight','stay','transfer'))
        async def regional_events(self,*args):raise ValueError('Untrusted provider body must not leak')
    planner=Planner(store,None,Providers())
    result=asyncio.run(planner.build_options(session,find_place('Larnaca'),[find_place('Athens')]))[0]
    assert result.regional_events.status=='unavailable' and not result.regional_events.items
    assert all('Untrusted' not in str(e) for e in result.evidence)
    assert result.stay.quote==component_template.stay.quote
    class ReadyProviders(Providers):
        async def regional_events(self,*args):return fixture_result(session)
    ready=asyncio.run(Planner(store,None,ReadyProviders()).build_options(session,find_place('Larnaca'),[find_place('Athens')]))[0]
    assert ready.regional_events.status=='ready'
    assert ready.score==result.score and ready.constraint_checks==result.constraint_checks
    assert ready.pricing_summary==result.pricing_summary
    assert ready.stay.evidence==result.stay.evidence


def test_date_replan_retains_old_scope_until_extraction_then_rebuilds_current_events_over_sse(tmp_path):
    async def scenario():
        notifications=SessionNotifications()
        store=Store(str(tmp_path/'live-events.db'),on_change=notifications.notify)
        session=prepared_session(store)
        owner=store.create_user().user_id
        store.assign_legacy_trip(session.id,owner)
        session.options[0].regional_events=fixture_result(session)
        session.selected_option_id=session.options[0].id
        store.save_session(session)
        stream=session_events(session.id,SessionViews(store),notifications)
        await anext(stream);old=await anext(stream)
        assert 'synthetic-event' in old
        job=store.start_message(session.id,'Synthetic fixture: change dates')
        admitted=store.get_session(session.id)
        assert admitted.options==session.options and admitted.intent==session.intent
        from backend.app.application.trips import TripApplication
        from backend.app.application.errors import ApplicationError
        from backend.app.domain.identity import Principal
        from backend.app.checkout import QuoteInput
        with pytest.raises(ApplicationError) as busy:
            TripApplication(store,Principal(owner)).quote(session.id,QuoteInput(option_id=session.selected_option_id,expected_revision=session.revision))
        assert busy.value.detail['code']=='session_busy'
        update=await anext(stream)
        data=json.loads(next(line[6:] for line in update.splitlines() if line.startswith('data: ')))
        assert data['session']['intent']['date_from']==str(session.intent.date_from)
        assert data['session']['options'][0]['regional_events']['items'][0]['id']=='synthetic-event'
        entered,release=asyncio.Event(),asyncio.Event()
        class LLM:
            async def extract(self,current):
                patch={'date_from':str(current.intent.date_from+timedelta(days=10)),
                       'date_to':str(current.intent.date_to+timedelta(days=10))}
                from backend.app.models import merge_intent
                changed,fields=merge_intent(current.intent,patch)
                return IntentExtraction(state_patch=patch),changed,fields
        class Providers:
            async def resolve_place(self,name):
                draft=store.get_session(session.id)
                assert not draft.options and draft.selected_option_id is None
                assert draft.intent.date_from!=session.intent.date_from
                assert draft.previous_plan.intent==session.intent
                assert draft.previous_plan.options[0].regional_events.items[0].id=='synthetic-event'
                entered.set();await release.wait()
                return find_place(name)
        planner=Planner(store,LLM(),Providers())
        async def rebuilt(current,*args):
            assert not current.options
            assert current.previous_plan.intent.date_from==session.intent.date_from
            assert current.previous_plan.options[0].regional_events.items[0].id=='synthetic-event'
            option=session.options[0].model_copy(deep=True)
            option.regional_events=fixture_result(current)
            option.regional_events.items[0].id='synthetic-new-date-event'
            return [option]
        planner.build_options=rebuilt
        task=asyncio.create_task(planner.run(job.id))
        await asyncio.wait_for(entered.wait(),1)
        during=await anext(stream)
        projected=json.loads(next(line[6:] for line in during.splitlines() if line.startswith('data: ')))['session']
        assert projected['options']==[]
        assert projected['previous_plan']['intent']['date_from']==str(session.intent.date_from)
        assert projected['previous_plan']['options'][0]['regional_events']['items'][0]['id']=='synthetic-event'
        assert projected['intent']['date_from']!=str(session.intent.date_from)
        release.set();await task
        final=store.get_session(session.id)
        assert store.get_job(job.id).status=='completed'
        assert final.previous_plan is None
        assert final.options[0].regional_events.items[0].start_date==final.intent.date_from
        assert final.options[0].regional_events.items[0].id=='synthetic-new-date-event'
        current=await anext(stream)
        assert 'synthetic-new-date-event' in current and '"id":"synthetic-event"' not in current
        await stream.aclose();store.close()
    asyncio.run(scenario())
