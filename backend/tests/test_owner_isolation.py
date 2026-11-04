"""Production cookie/owner boundary: no fixture authentication bypass."""
import asyncio

import httpx
import pytest
from fastapi.testclient import TestClient

from backend.app.main import create_app
from backend.app.transport.auth import AUTH_COOKIE
from backend.app.transport.session_events import session_events

ORIGIN = "https://travel.test"


def app_for(tmp_path):
    class Data:
        async def capabilities(self):
            return {"external_data_fallback_enabled": False, "providers": {}, "pricing": {}, "gateway": {}}
    return create_app(database_path=str(tmp_path / "owners.db"),
                      transport=httpx.MockTransport(lambda r: (_ for _ in ()).throw(AssertionError("no network"))),
                      travel_data_factory=lambda _: Data())


def login(client):
    result = client.post("/api/auth/guest", headers={"Origin": ORIGIN})
    assert result.status_code == 200, result.text
    return dict(client.cookies), {"Origin": ORIGIN, "X-CSRF-Token": result.json()["csrf_token"]}


def test_cookie_auth_origin_csrf_and_logout(tmp_path):
    with TestClient(app_for(tmp_path), base_url=ORIGIN) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/api/config").status_code == 200
        assert client.post("/api/sessions", json={}).status_code == 401
        assert client.post("/api/auth/guest").status_code == 403
        assert client.post("/api/auth/guest", headers={"Origin": "https://evil.test"}).status_code == 403
        cookies, headers = login(client)
        assert client.post("/api/sessions", json={}).status_code == 403
        assert client.post("/api/sessions", json={}, headers={**headers, "Origin": "https://evil.test"}).status_code == 403
        created = client.post("/api/sessions", json={}, headers=headers)
        assert created.status_code == 201
        sid = created.json()["id"]
        assert client.get("/api/sessions").json()["items"][0]["id"] == sid
        assert client.get("/api/metrics").status_code == 403
        assert client.post("/api/auth/logout", headers=headers).status_code == 200
        client.cookies.update(cookies)
        assert client.get(f"/api/sessions/{sid}").status_code == 401


def test_cross_owner_and_legacy_are_hidden_on_every_existing_trip_surface(tmp_path):
    app = app_for(tmp_path)
    with TestClient(app, base_url=ORIGIN) as client:
        a, ah = login(client)
        sid = client.post("/api/sessions", json={}, headers=ah).json()["id"]
        job = app.state.store.start_message(sid, "Synthetic private wish")
        legacy = app.state.store.create_session()
        client.cookies.clear()
        b, bh = login(client)
        for path in [f"/api/sessions/{sid}", f"/api/sessions/{sid}/events",
                     f"/api/jobs/{job.id}", f"/api/sessions/{legacy.id}"]:
            assert client.get(path).status_code == 404, path
        for path, body in [
            (f"/api/sessions/{sid}/messages", {"content": "steal"}),
            (f"/api/sessions/{sid}/prices/refresh", {"expected_revision": 0}),
            (f"/api/sessions/{sid}/select", {"option_id": "x"}),
            (f"/api/sessions/{sid}/checkout/quote", {"option_id": "x", "expected_revision": 0}),
            (f"/api/sessions/{sid}/checkout", {"option_id": "x", "expected_revision": 0}),
            ("/api/sessions", {"preference_session_id": sid}),
            ("/api/sessions", {"preference_session_id": legacy.id}),
        ]:
            assert client.post(path, json=body, headers=bh).status_code == 404, path
        assert client.get("/api/sessions").json()["items"] == []
        assert app.state.store.get_session(sid).messages[-1].content == "Synthetic private wish"
        client.cookies.clear()
        client.cookies.update(a)
        assert client.get(f"/api/sessions/{sid}").status_code == 200


@pytest.mark.asyncio
async def test_stream_stops_before_next_snapshot_or_heartbeat_after_revocation(tmp_path):
    app = app_for(tmp_path)
    async with app.router.lifespan_context(app):
        store = app.state.store
        user = store.create_user()
        credentials = store.create_access_session(user.user_id)
        trip = store.create_owned_session(user.user_id)
        stream = session_events(trip.id, app.state.session_views, app.state.session_notifications,
                                heartbeat_seconds=0.001,
                                authorize=lambda: store.resolve_access_session(credentials.token) is not None)
        assert "retry" in await anext(stream)
        assert "event: session" in await anext(stream)
        store.revoke_access_session(credentials.token)
        with pytest.raises(StopAsyncIteration):
            await asyncio.wait_for(anext(stream), 1)


def test_expiry_and_header_spoof_do_not_authenticate(tmp_path):
    app = app_for(tmp_path)
    with TestClient(app, base_url=ORIGIN) as client:
        cookies, headers = login(client)
        sid = client.post("/api/sessions", json={}, headers=headers).json()["id"]
        with app.state.store.transaction():
            app.state.store.db.execute("UPDATE access_sessions SET expires_at='2000-01-01T00:00:00+00:00'")
        assert client.get(f"/api/sessions/{sid}").status_code == 401
        client.cookies.clear()
        assert client.get(f"/api/sessions/{sid}", headers={"X-User-ID": app.state.store.session_owner(sid)}).status_code == 401
