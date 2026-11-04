"""Aftercare amendments keep receipt identity through ordinary planner commands."""
from datetime import timedelta

import pytest

from backend.app.application.aftercare import AftercareApplication,AmendInput
from backend.app.application.trips import TripApplication
from backend.app.domain.travel_data import ComponentBundle
from backend.app.geography import find_place
from backend.app.models import IntentExtraction,local_today,merge_intent
from backend.app.planner import Planner
from backend.app.storage import Store
from backend.tests.test_pricing_core import prepared_session
from backend.tests.test_agent_comparison import execute


@pytest.mark.asyncio
@pytest.mark.parametrize('change',['dates','destination','wrong_identity','wrong_title','wrong_url'])
async def test_amendment_preserves_fixed_property_or_requires_explicit_destination_change(tmp_path,change):
    store=Store(str(tmp_path/'amend-planner.db'))
    owner=store.create_user().user_id
    original=prepared_session(store)
    store.assign_legacy_trip(original.id,owner)
    original.options[0].stay.details['catalog_property_id']='osm:node:42'
    original.options[0].stay.title='Synthetic fixed property'
    original.options[0].stay.source_url='https://www.openstreetmap.org/node/42'
    store.save_session(original)
    receipt={'id':'receipt-fixed','session_id':original.id,'revision':original.revision,'status':'simulated',
        'itinerary':{'intent':original.intent.model_dump(mode='json'),'option':original.options[0].model_dump(mode='json')}}
    store.save_checkout(receipt)
    receipt_bytes=store.db.execute('SELECT data FROM checkouts').fetchone()[0]
    body=AmendInput(receipt_id=receipt['id'],date_from=local_today()+timedelta(days=70),date_to=local_today()+timedelta(days=77))
    child,job=AftercareApplication(store,TripApplication(store)).amend(owner,original.id,body,'fixed-aftercare-0001')
    calls=[]
    class Model:
        async def extract(self,current):
            calls.append('intent')
            patch={'destination':'Berlin'} if change=='destination' else {}
            intent,fields=merge_intent(current.intent,patch)
            return IntentExtraction(state_patch=patch,action='refine'),intent,fields
        async def completion(self,*a,**k): raise AssertionError('no extra extraction permitted')
    class Data:
        async def resolve_place(self,name): return find_place(name)
        async def weather(self,*args): return {'kind':'unknown'}
        async def search(self,*args): return []
        async def refresh_inventory(self,place,origin,intent,current):
            calls.append('refresh')
            assert change!='destination'
            assert current.stay.details['catalog_property_id']=='osm:node:42'
            assert current.stay.title==original.options[0].stay.title and current.stay.source_url==original.options[0].stay.source_url
            assert intent.date_from==body.date_from and intent.date_to==body.date_to
            assert all(c.price is None and c.quote is None and c.status=='unavailable' for c in current)
            if change=='wrong_identity': current.stay.details['catalog_property_id']='osm:node:99'
            if change=='wrong_title': current.stay.title='Different hotel'
            if change=='wrong_url': current.stay.source_url='https://www.openstreetmap.org/node/99'
            return current
        async def inventory_candidates(self,place,origin,intent,*,limit):
            calls.append('discovery')
            assert change=='destination' and place==find_place('Berlin')
            bundle=ComponentBundle.from_components([getattr(original.options[0],name).model_copy(deep=True) for name in ('flight','stay','transfer')])
            for component in bundle: component.price,component.quote=None,None
            bundle.stay.details['catalog_property_id']='osm:node:99'
            bundle.stay.title='Synthetic explicitly requested Berlin hotel'
            bundle.stay.source_url='https://www.openstreetmap.org/node/99'
            return [bundle]
    try:
        await execute(store,Planner(store,Model(),Data()),job)
        current=store.get_session(child.id)
        assert calls.count('intent')==1
        assert store.db.execute('SELECT data FROM checkouts').fetchone()[0]==receipt_bytes
        assert original.options[0].stay.quote is not None
        if change.startswith('wrong_'):
            assert store.get_job(job.id).status=='failed' and not current.options
            assert current.previous_plan.options==original.options
            assert calls==['intent','refresh']
        else:
            assert store.get_job(job.id).status=='completed' and current.revision==1
            assert len(current.options)==1 and current.options[0].id!=original.options[0].id
            assert current.options[0].id.startswith('r1-') and current.selected_option_id is None
            assert all(c.quote is None and c.price is None for c in (current.options[0].flight,current.options[0].stay,current.options[0].transfer))
            if change=='dates':
                assert calls==['intent','refresh']
                assert current.options[0].stay.details['catalog_property_id']=='osm:node:42'
                assert current.options[0].stay.title==original.options[0].stay.title
            else:
                assert calls==['intent','discovery']
                assert current.options[0].stay.details['catalog_property_id']=='osm:node:99'
                assert current.preferences['aftercare_amendment']['fixed_hotel'] is False
            assert current.accepted_plan_revision==current.revision
    finally:
        store.close()
