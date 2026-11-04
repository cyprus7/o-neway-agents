"""Internal worker contract check. All cases stop before public navigation; no LLM."""
from copy import deepcopy
from datetime import datetime,timedelta,timezone
import json
from urllib.error import HTTPError
from urllib.request import Request,urlopen


def main():
    clock=datetime.now(timezone.utc)
    base={'provider_id':'google_hotels','request_id':'contract-check',
          'request_context_hash':'0'*64,'deadline_at':(clock+timedelta(seconds=21)).isoformat(),
          'request_context':{'destination':'Афины','destination_iata':'ATH','date_from':'2026-10-25',
              'date_to':'2026-11-01','adults':1,'children_ages':[],'rooms':1,'currency':'EUR'}}
    checks=[]
    for label,change in [('extra_url',{'url':'http://127.0.0.1/private'}),('provider',{'provider_id':'arbitrary'}),
                         ('naive_deadline',{'deadline_at':'2026-10-25T00:00:00'})]:
        checks.append((label,base|change,422,None))
    for field,value in [('adults',0),('rooms',10),('children_ages',[-1]),('currency','eur'),('destination_iata','ATH:80')]:
        body=deepcopy(base)
        body['request_context'][field]=value
        checks.append((field,body,422,None))
    checks.append(('expired',base|{'deadline_at':(clock-timedelta(seconds=1)).isoformat()},200,'timeout'))
    body=deepcopy(base)
    body['request_context']['children_ages']=[6]
    checks.append(('unsupported_party',body,200,'unsupported_party'))
    results=[]
    for label,body,status,outcome in checks:
        request=Request('http://price-worker:8080/v1/quotes',data=json.dumps(body).encode(),
                        headers={'Content-Type':'application/json'},method='POST')
        try:
            with urlopen(request,timeout=25) as response:
                actual=response.status
                payload=json.load(response)
        except HTTPError as error:
            actual=error.code
            payload={}
        assert actual==status,(label,actual)
        assert outcome is None or payload.get('outcome')==outcome,(label,payload.get('outcome'))
        results.append({'case':label,'status':actual,'outcome':payload.get('outcome')})
    print(json.dumps({'passed':len(results),'scope':'validation_and_pre_navigation_rejections','cases':results}))


main()
