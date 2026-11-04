"""Telegram HMAC/identity boundary, synthetic signed launch data; no network."""
import hashlib
import hmac
import json
from urllib.parse import urlencode

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
import pytest

from backend.app.storage import Store
from backend.app.transport.auth import AUTH_COOKIE, CSRF_COOKIE, install_auth
from backend.app.transport.telegram_auth import BODY_LIMIT, MAX_USER_ID, install_telegram_auth

TOKEN='123456789:synthetic-only-never-real-token'
NOW=1800000000
ORIGIN='https://travel.test'


def signed(*, user=None, stamp=NOW, extras=None, token=TOKEN, reverse=False):
    fields={'auth_date':str(stamp),'user':json.dumps({'id':42,'username':'first'} if user is None else user,separators=(',',':'))}
    fields.update(extras or {})
    secret=hmac.new(b'WebAppData',token.encode(),hashlib.sha256).digest()
    check='\n'.join(f'{k}={v}' for k,v in sorted(fields.items()))
    fields['hash']=hmac.new(secret,check.encode(),hashlib.sha256).hexdigest()
    pairs=list(fields.items())
    return urlencode(list(reversed(pairs)) if reverse else pairs)


@pytest.fixture
def client(tmp_path,monkeypatch):
    monkeypatch.setenv('TELEGRAM_BOT_TOKEN',TOKEN)
    monkeypatch.delenv('APP_ALLOWED_ORIGINS',raising=False)
    monkeypatch.setattr('backend.app.transport.telegram_auth.time.time',lambda:NOW)
    app=FastAPI()
    app.state.store=Store(str(tmp_path/'telegram.db'))
    app.state.identity_resolver=None
    install_auth(app)
    install_telegram_auth(app)
    with TestClient(app,base_url=ORIGIN) as client:
        yield client
    app.state.store.close()


def login(client,raw=None,**kwargs):
    return client.post('/api/auth/telegram',json={'init_data':signed() if raw is None else raw},headers={'Origin':ORIGIN},**kwargs)


def test_valid_reordered_launch_secure_cookies_same_user_two_sessions_and_username_rename(client):
    first=login(client)
    assert first.status_code==200
    first_user=first.json()['user']['id']
    first_token=client.cookies[AUTH_COOKIE]
    second=login(client,signed(user={'id':42,'username':'renamed'},reverse=True))
    assert second.status_code==200 and second.json()['user']['id']==first_user
    assert client.cookies[AUTH_COOKIE]!=first_token
    store=client.app.state.store
    assert store.resolve_access_session(first_token).principal.user_id==first_user
    assert store.resolve_access_session(client.cookies[AUTH_COOKIE]).principal.user_id==first_user
    cookie_headers=second.headers.get_list('set-cookie')
    auth=next(v for v in cookie_headers if v.startswith(AUTH_COOKIE+'=') and 'Max-Age=43200' in v)
    csrf=next(v for v in cookie_headers if v.startswith(CSRF_COOKIE+'=') and 'Max-Age=43200' in v)
    assert 'HttpOnly' in auth and 'Secure' in auth and 'SameSite=none' in auth and '; Partitioned' in auth
    assert 'HttpOnly' not in csrf and 'Secure' in csrf and 'SameSite=none' in csrf and '; Partitioned' in csrf
    assert client.get('/api/auth/session').json()['user']['id']==first_user
    text=second.text+str(second.headers)
    assert TOKEN not in text and 'renamed' not in text and 'auth_date' not in text


@pytest.mark.parametrize('delta,expected',[(-301,401),(-300,200),(0,200),(30,200),(31,401)])
def test_freshness_boundaries(client,delta,expected):
    assert login(client,signed(stamp=NOW+delta)).status_code==expected


@pytest.mark.parametrize('user',[{'id':True},{'id':0},{'id':-1},{'id':'42'},{'id':42.0},{'id':MAX_USER_ID+1},{'username':'only'},[],None])
def test_numeric_subject_strict_and_bounded(client,user):
    raw=signed(user=user) if user is not None else signed(extras={'user':'null'})
    assert login(client,raw).status_code==401
    assert client.app.state.store.db.execute("SELECT count(*) FROM external_identities").fetchone()[0]==0


def test_max_52_bit_subject_preserves_decimal_identity(client):
    response=login(client,signed(user={'id':MAX_USER_ID}))
    assert response.status_code==200
    row=client.app.state.store.db.execute('SELECT issuer,subject FROM external_identities').fetchone()
    assert tuple(row)==('telegram:123456789',str(MAX_USER_ID))


@pytest.mark.parametrize('raw',[
    'user=invalid',
    signed()+'&auth_date='+str(NOW),
    signed()+'&hash='+'0'*64,
    signed()+'&user=other',
    signed()+'&%75ser=other',
    signed().replace('auth_date=','auth_date=%ZZ'),
    signed()+'&incomplete',
    signed(extras={'auth_date':'NaN'}),
    signed(extras={'user':'{"id":42,"id":43}'}),
    signed(extras={'user':'{"id":NaN}'}),
    signed(extras={'user':'{"id":42'}),
    signed(token='987:wrong-synthetic-token'),
    signed().replace('auth_date='+str(NOW),'auth_date='+str(NOW-1)),
])
def test_tamper_duplicate_and_malformed_fields_are_safe(client,raw):
    response=login(client,raw)
    assert response.status_code==401
    assert TOKEN not in response.text and raw not in response.text
    assert AUTH_COOKIE not in response.headers.get('set-cookie','')
    assert client.app.state.store.db.execute('SELECT count(*) FROM external_identities').fetchone()[0]==0


@pytest.mark.parametrize('token',['','not-a-token','abc:secret','0:secret'])
def test_missing_or_invalid_configuration_returns_safe_unavailable(client,monkeypatch,token):
    monkeypatch.setenv('TELEGRAM_BOT_TOKEN',token)
    response=login(client)
    assert response.status_code==503
    assert response.json()['detail']['code']=='telegram_unavailable'
    assert 'secret' not in response.text


def test_origin_required_and_init_data_unsafe_never_authorizes(client):
    assert client.post('/api/auth/telegram',json={'init_data':signed()}).status_code==403
    assert client.post('/api/auth/telegram',json={'init_data':signed()},headers={'Origin':'https://evil.test'}).status_code==403
    assert client.post('/api/auth/telegram',json={'initDataUnsafe':{'user':{'id':42}}},headers={'Origin':ORIGIN}).status_code==401


@pytest.mark.parametrize('body',['{','[]','{"init_data":true}','{"init_data":"x","init_data":"y"}','{"init_data":"x","user":{"id":42}}'])
def test_malformed_json_body_has_controlled_failure(client,body):
    response=client.post('/api/auth/telegram',content=body,headers={'Origin':ORIGIN})
    assert response.status_code==401


def test_stream_and_init_data_byte_limits_before_parsing(client):
    def chunks():
        yield b'{"init_data":"'
        yield b'x'*BODY_LIMIT
    response=client.post('/api/auth/telegram',content=chunks(),headers={'Origin':ORIGIN})
    assert response.status_code==413
    response=login(client,'x'*8193)
    assert response.status_code==401
    # 4097 two-byte UTF-8 chars violate the byte limit despite fewer characters.
    assert login(client,'é'*4097).status_code==401


def test_guest_and_other_telegram_user_are_never_automatically_merged(client):
    guest=client.post('/api/auth/guest',headers={'Origin':ORIGIN})
    guest_id=guest.json()['user']['id']
    guest_token=client.cookies[AUTH_COOKIE]
    store=client.app.state.store
    trip=store.create_owned_session(guest_id)
    first=login(client)
    one=first.json()['user']['id']
    second=login(client,signed(user={'id':43,'username':'first'}))
    two=second.json()['user']['id']
    assert len({guest_id,one,two})==3
    assert store.get_owned_session(trip.id,one) is None
    assert store.get_owned_session(trip.id,two) is None
    assert store.session_owner(trip.id)==guest_id
    assert store.resolve_access_session(guest_token).principal.user_id==guest_id


@pytest.mark.asyncio
async def test_bounded_asgi_stream_stops_before_another_receive_or_json_parse(client,monkeypatch):
    calls=[]
    chunks=[b'{"init_data":"', b'x'*BODY_LIMIT]
    async def receive():
        calls.append(1)
        assert chunks, 'must stop receiving after crossing limit'
        return {'type':'http.request','body':chunks.pop(0),'more_body':True}
    scope={'type':'http','scheme':'https','server':('travel.test',443),'path':'/api/auth/telegram',
           'headers':[(b'host',b'travel.test'),(b'origin',ORIGIN.encode())],'app':client.app}
    request=Request(scope,receive)
    endpoint=next(route.endpoint for route in client.app.routes if getattr(route,'path',None)=='/api/auth/telegram')
    monkeypatch.setattr('backend.app.transport.telegram_auth._json',lambda _:pytest.fail('must cap before JSON parsing'))
    response=await endpoint(request)
    assert response.status_code==413 and len(calls)==2


def cookie_headers(response,*,active):
    marker='Max-Age=43200' if active else 'Max-Age=0'
    return [header for header in response.headers.get_list('set-cookie') if marker in header]


def test_guest_cookies_remain_lax_and_remove_partitioned_shadow_before_issuance(client):
    response=client.post('http://localhost/api/auth/guest',headers={'Origin':'http://localhost'})
    assert response.status_code==200 and response.headers['cache-control']=='no-store'
    headers=response.headers.get_list('set-cookie')
    expired=cookie_headers(response,active=False);active=cookie_headers(response,active=True)
    assert len(expired)==len(active)==2 and headers==expired+active
    assert all('Secure' in header and 'SameSite=none' in header and '; Partitioned' in header and 'expires=' in header.lower() for header in expired)
    assert all('SameSite=lax' in header and 'Secure' not in header and 'Partitioned' not in header for header in active)
    assert 'HttpOnly' in active[0] and 'HttpOnly' not in active[1]


def test_guest_on_arbitrary_http_keeps_secure_requirement(client):
    response=client.post('http://arbitrary.test/api/auth/guest',headers={'Origin':'http://arbitrary.test'})
    assert response.status_code==200
    assert all('Secure' in header and 'SameSite=lax' in header and 'Partitioned' not in header
               for header in cookie_headers(response,active=True))


def test_local_http_logout_revokes_guest_even_if_a_client_retains_expired_cookie(client):
    origin='http://localhost'
    login_response=client.post(origin+'/api/auth/guest',headers={'Origin':origin})
    token=client.cookies[AUTH_COOKIE]
    response=client.post(origin+'/api/auth/logout',headers={'Origin':origin,'X-CSRF-Token':login_response.json()['csrf_token']})
    assert response.status_code==200
    expired=cookie_headers(response,active=False)
    assert len(expired)==4 and all('Secure' in header for header in expired)
    assert client.app.state.store.resolve_access_session(token) is None
    # HTTPX is not a browser and does not model CHIPS/localhost exemptions.
    # The backend must reject the old token even if a client sends it again.
    client.cookies.set(AUTH_COOKIE,token,domain='localhost.local',path='/')
    assert client.get(origin+'/api/auth/session').status_code==401


@pytest.mark.parametrize('url',['http://localhost','http://127.0.0.1','http://arbitrary.test','https://travel.test'])
def test_verified_telegram_always_secure_partitioned_and_clears_unpartitioned_first(client,url):
    response=client.post(url+'/api/auth/telegram',json={'init_data':signed()},headers={'Origin':url})
    assert response.status_code==200 and response.headers['cache-control']=='no-store'
    expired=cookie_headers(response,active=False);active=cookie_headers(response,active=True)
    assert response.headers.get_list('set-cookie')==expired+active
    assert len(expired)==len(active)==2
    assert all('Secure' in header and 'SameSite=lax' in header and 'Partitioned' not in header for header in expired)
    assert all('Secure' in header and 'SameSite=none' in header and '; Partitioned' in header for header in active)


def test_guest_bootstrap_reuses_valid_telegram_credentials_without_cookie_policy_change(client):
    telegram=login(client);assert telegram.status_code==200
    token=client.cookies[AUTH_COOKIE]
    response=client.post('/api/auth/guest',headers={'Origin':ORIGIN})
    assert response.status_code==200 and response.headers['cache-control']=='no-store'
    assert response.json()==telegram.json() and not response.headers.get_list('set-cookie')
    assert client.cookies[AUTH_COOKIE]==token


def test_guest_bootstrap_reissues_registered_credentials_using_telegram_delivery_policy(client):
    telegram=login(client);old_token=client.cookies[AUTH_COOKIE]
    client.cookies.delete(CSRF_COOKIE)
    response=client.post('/api/auth/guest',headers={'Origin':ORIGIN})
    assert response.status_code==200 and response.json()['user']==telegram.json()['user']
    assert client.cookies[AUTH_COOKIE]!=old_token
    assert client.app.state.store.resolve_access_session(old_token) is None
    assert all('Secure' in header and 'SameSite=none' in header and '; Partitioned' in header for header in cookie_headers(response,active=True))
    assert client.get('/api/auth/session').json()['user']==telegram.json()['user']


def test_logout_requires_csrf_then_revokes_and_expires_both_variants(client):
    telegram=login(client);token=client.cookies[AUTH_COOKIE]
    bad=client.post('/api/auth/logout',headers={'Origin':ORIGIN,'X-CSRF-Token':'wrong'})
    assert bad.status_code==403 and not bad.headers.get_list('set-cookie')
    assert bad.headers['cache-control']=='no-store'
    assert client.app.state.store.resolve_access_session(token) is not None
    response=client.post('/api/auth/logout',headers={'Origin':ORIGIN,'X-CSRF-Token':telegram.json()['csrf_token']})
    assert response.status_code==200 and response.headers['cache-control']=='no-store'
    expired=cookie_headers(response,active=False)
    assert len(expired)==4 and not cookie_headers(response,active=True)
    for name in (AUTH_COOKIE,CSRF_COOKIE):
        variants=[header for header in expired if header.startswith(name+'=')]
        assert len(variants)==2 and all('Secure' in header and 'expires=' in header.lower() for header in variants)
        assert sum('Partitioned' in header for header in variants)==1
        assert any('SameSite=lax' in header and 'Partitioned' not in header for header in variants)
        assert any('SameSite=none' in header and 'Partitioned' in header for header in variants)
    assert client.app.state.store.resolve_access_session(token) is None
    client.cookies.set(AUTH_COOKIE,token)
    assert client.get('/api/auth/session').status_code==401


@pytest.mark.parametrize('path,payload',[('/api/auth/guest',{}),('/api/auth/telegram',{'init_data':signed()})])
def test_rejected_auth_is_not_cacheable_and_mints_no_cookie(client,path,payload):
    response=client.post(path,json=payload,headers={'Origin':'https://wrong.test'})
    assert response.status_code==403 and response.headers['cache-control']=='no-store'
    assert not response.headers.get_list('set-cookie')


def test_partitioned_literal_only_changes_new_cookie_headers_and_not_simplecookie_globals():
    from http.cookies import Morsel
    from types import SimpleNamespace
    from fastapi.responses import JSONResponse
    from backend.app.transport.auth import set_credentials
    reserved=dict(Morsel._reserved)
    response=JSONResponse({})
    response.set_cookie('unrelated','untouched',samesite='strict')
    original=list(response.raw_headers)
    request=Request({'type':'http','scheme':'https','server':('travel.test',443),'path':'/','headers':[(b'host',b'travel.test')]})
    set_credentials(response,request,SimpleNamespace(token='synthetic-token',csrf_token='synthetic-csrf'),partitioned=True)
    assert response.raw_headers[:len(original)]==original
    assert Morsel._reserved==reserved
    assert sum(b'; Partitioned' in value for key,value in response.raw_headers if key==b'set-cookie')==2


def access_rows(store):
    return [tuple(row) for row in store.db.execute('SELECT * FROM access_sessions ORDER BY token_hash')]


def expired_request(client,raw,csrf=None,origin=ORIGIN):
    headers={'Origin':origin}
    if csrf is not None: headers['X-CSRF-Token']=csrf
    return client.post('/api/auth/telegram',json={'init_data':raw},headers=headers)


def test_modern_signature_field_is_hmac_protected_and_expiration_is_typed_after_schema_validation():
    from backend.app.transport.telegram_auth import ExpiredInitData,InvalidInitData,verify_init_data
    modern=signed(extras={'signature':'synthetic-modern-signature_+/='})
    assert verify_init_data(modern,TOKEN,now=NOW)=='42'
    with pytest.raises(InvalidInitData):
        verify_init_data(modern.replace('synthetic-modern','tampered-modern'),TOKEN,now=NOW)
    with pytest.raises(ExpiredInitData) as expired:
        verify_init_data(signed(stamp=NOW-1072,extras={'signature':'modern-signed-value'}),TOKEN,now=NOW)
    assert expired.value.subject=='42' and str(expired.value)=='telegram_launch_expired'
    with pytest.raises(InvalidInitData):
        verify_init_data(signed(stamp=NOW-1072,user={'id':True}),TOKEN,now=NOW)
    with pytest.raises(InvalidInitData):
        verify_init_data(signed(stamp=NOW+31),TOKEN,now=NOW)


@pytest.mark.parametrize('age',[301,1072,1000000])
def test_authentic_expired_launch_resumes_only_existing_same_owner_without_mint_or_extension(client,monkeypatch,age):
    fresh=login(client)
    store=client.app.state.store
    before=access_rows(store)
    owners=store.db.execute('SELECT count(*) FROM users').fetchone()[0]
    csrf=fresh.json()['csrf_token']
    token=client.cookies[AUTH_COOKIE]
    def forbidden(*a,**k): pytest.fail('Expired launch must not create identity/access credentials')
    monkeypatch.setattr(store,'resolve_identity',forbidden)
    monkeypatch.setattr(store,'create_access_session',forbidden)
    response=expired_request(client,signed(stamp=NOW-age,extras={'signature':'signed-modern-launch'}),csrf)
    assert response.status_code==200 and response.json()==fresh.json()
    assert response.headers['cache-control']=='no-store' and not response.headers.get_list('set-cookie')
    assert client.cookies[AUTH_COOKIE]==token
    assert access_rows(store)==before and store.db.execute('SELECT count(*) FROM users').fetchone()[0]==owners
    assert TOKEN not in response.text and 'signed-modern-launch' not in response.text and 'auth_date' not in response.text


@pytest.mark.parametrize('condition',['missing_cookie','missing_csrf','wrong_csrf','revoked','expired_access','deleted_user','other_subject','other_owner','other_bot','guest'])
def test_expired_launch_cannot_resume_without_exact_live_cookie_csrf_and_identity(client,monkeypatch,condition):
    fresh=login(client);store=client.app.state.store
    csrf=fresh.json()['csrf_token'];user=fresh.json()['user']['id'];token=client.cookies[AUTH_COOKIE]
    raw=signed(stamp=NOW-1072)
    if condition=='missing_cookie': client.cookies.clear()
    if condition=='missing_csrf': csrf=None
    if condition=='wrong_csrf': csrf='wrong-csrf'
    if condition=='revoked': store.revoke_access_session(token)
    if condition=='expired_access':
        with store.transaction(): store.db.execute('UPDATE access_sessions SET expires_at=?',('2020-01-01T00:00:00+00:00',))
    if condition=='deleted_user':
        with store.transaction(): store.db.execute('UPDATE users SET deleted_at=? WHERE user_id=?',('2020-01-01T00:00:00+00:00',user))
    if condition=='other_subject': raw=signed(stamp=NOW-1072,user={'id':43})
    if condition=='other_owner':
        other=login(client,signed(user={'id':43}))
        csrf=other.json()['csrf_token']
    if condition=='other_bot':
        other='987654:synthetic-other-bot-token'
        monkeypatch.setenv('TELEGRAM_BOT_TOKEN',other)
        raw=signed(stamp=NOW-1072,token=other)
    if condition=='guest':
        client.cookies.clear()
        guest=client.post('/api/auth/guest',headers={'Origin':ORIGIN});assert guest.status_code==200
        csrf=guest.json()['csrf_token']
    before=access_rows(store)
    identities=store.db.execute('SELECT count(*) FROM external_identities').fetchone()[0]
    def forbidden(*a,**k): pytest.fail('Expired launch must not resolve/create identity or credentials')
    monkeypatch.setattr(store,'resolve_identity',forbidden);monkeypatch.setattr(store,'create_access_session',forbidden)
    response=expired_request(client,raw,csrf)
    assert response.status_code==401 and response.json()['detail']['code']=='telegram_launch_expired'
    assert response.headers['cache-control']=='no-store' and not response.headers.get_list('set-cookie')
    assert access_rows(store)==before and store.db.execute('SELECT count(*) FROM external_identities').fetchone()[0]==identities
    assert TOKEN not in response.text and raw not in response.text


@pytest.mark.parametrize('raw',[signed(stamp=NOW-1072).replace('auth_date='+str(NOW-1072),'auth_date='+str(NOW-1073)),
    signed(stamp=NOW+31),signed(stamp=NOW-1072,user={'id':False}),signed(stamp=NOW-1072,extras={'auth_date':'NaN'})])
def test_bad_or_future_launch_never_resumes_even_with_valid_matching_credentials(client,raw):
    fresh=login(client);before=access_rows(client.app.state.store)
    response=expired_request(client,raw,fresh.json()['csrf_token'])
    assert response.status_code==401 and response.json()['detail']['code']=='telegram_init_data_invalid'
    assert not response.headers.get_list('set-cookie')
    assert access_rows(client.app.state.store)==before


def test_expired_launch_still_requires_exact_origin_before_cookie_checks(client):
    fresh=login(client);before=access_rows(client.app.state.store)
    response=expired_request(client,signed(stamp=NOW-1072),fresh.json()['csrf_token'],origin='https://wrong.test')
    assert response.status_code==403 and not response.headers.get_list('set-cookie')
    assert access_rows(client.app.state.store)==before


def test_missing_auth_date_with_otherwise_valid_hmac_is_invalid_not_expired(client):
    fields={'user':json.dumps({'id':42})}
    secret=hmac.new(b'WebAppData',TOKEN.encode(),hashlib.sha256).digest()
    fields['hash']=hmac.new(secret,('user='+fields['user']).encode(),hashlib.sha256).hexdigest()
    response=expired_request(client,urlencode(fields))
    assert response.status_code==401 and response.json()['detail']['code']=='telegram_init_data_invalid'


def test_identity_matches_is_read_only_and_rejects_other_owner_bot_subject_or_deleted_user(client):
    store=client.app.state.store
    user=store.resolve_identity('telegram:123456789','42')
    other=store.create_user()
    changes=store.db.total_changes
    assert store.identity_matches(user.user_id,'telegram:123456789','42')
    assert not store.identity_matches(other.user_id,'telegram:123456789','42')
    assert not store.identity_matches(user.user_id,'telegram:987654','42')
    assert not store.identity_matches(user.user_id,'telegram:123456789','43')
    assert store.db.total_changes==changes
    with store.transaction(): store.db.execute('UPDATE users SET deleted_at=? WHERE user_id=?',('2020-01-01T00:00:00+00:00',user.user_id))
    changes=store.db.total_changes
    assert not store.identity_matches(user.user_id,'telegram:123456789','42')
    assert store.db.total_changes==changes
