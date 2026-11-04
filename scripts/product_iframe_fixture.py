"""Isolated HTTPS iframe regression fixture; never a production entry point.

Run with .venv Python after building frontend. Synthetic HMAC, in-memory store,
blocked outbound HTTP, no dotenv reads. TLS certificate is disposable under tmp;
the browser test ignores certificate trust only for these loopback test origins.
"""
import argparse
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from urllib.parse import parse_qs, urlencode

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import dotenv
dotenv.load_dotenv = lambda *args, **kwargs: False
os.environ.update(TELEGRAM_BOT_TOKEN="123456:synthetic-iframe-key-not-a-real-secret",
                  TELEGRAM_MODE="disabled", WEB_FALLBACK_ENABLED="0", LLM_ENABLED="0",
                  OTEL_EXPORTER_OTLP_TRACES_ENDPOINT="", APP_ALLOWED_ORIGINS="https://localhost:18443")

import httpx
import uvicorn
from starlette.responses import HTMLResponse, Response
from backend.app.application.dependencies import AgentDependencies
from backend.app.application.aftercare import ReminderInput
from backend.app.main import create_app
from backend.app.transport import auth, telegram_auth


def forbidden(*args, **kwargs):
    raise AssertionError("Iframe fixture forbids model/provider calls")


class NoModel:
    configured = False
    enabled = False
    model = "offline-iframe-fixture"
    extract = forbidden


class NoTravelData:
    async def capabilities(self):
        return {"external_data_fallback_enabled": False, "providers": {}, "pricing": {}}


def signed_data(user, expired=False, start_param=None):
    fields = {"auth_date": str(int(time.time()) - (1072 if expired else 0)), "user": json.dumps({"id": 101 if user == "alice" else 202}, separators=(",", ":"))}
    if start_param is not None:
        fields["start_param"] = start_param
    check = "\n".join(f"{key}={value}" for key, value in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", os.environ["TELEGRAM_BOT_TOKEN"].encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


def legacy_credentials(response, request, credentials, **kwargs):
    # Explicit negative control: historical Lax pair, no production switch.
    for name, value, private in ((auth.AUTH_COOKIE, credentials.token, True),
                                  (auth.CSRF_COOKIE, credentials.csrf_token, False)):
        response.set_cookie(name, value, max_age=43200, secure=True, httponly=private, samesite="lax", path="/")


class Child:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["path"] in {"/app/", "/fixture-sdk.js"}:
            if scope["path"] == "/app/":
                html = (ROOT / "frontend/dist/index.html").read_text(encoding="utf-8")
                user = parse_qs(scope.get("query_string", b"").decode()).get("user", ["alice"])[0]
                user = "bob" if user == "bob" else "alice"
                expired = parse_qs(scope.get("query_string", b"").decode()).get("expired") == ["1"]
                response = HTMLResponse(html.replace("<head>", f'<head><script src="/fixture-sdk.js?user={user}&expired={int(expired)}"></script>'))
            else:
                user = parse_qs(scope.get("query_string", b"").decode()).get("user", ["alice"])[0]
                # Offline SDK fixture only; all authentication requests and SSE are real.
                expired = parse_qs(scope.get("query_string", b"").decode()).get("expired") == ["1"]
                trip = getattr(self.app.state, "reminder_fixture_trips", {}).get(user)
                js = "window.iframeStats={ready:0,closed:0,errorOnReady:false};window.Telegram={WebApp:{initData:" + json.dumps(signed_data(user, expired, "trip_" + trip if trip else None)) + ",ready(){window.iframeStats.ready++;window.iframeStats.errorOnReady=!!document.querySelector('[role=alert]')},close(){window.iframeStats.closed++},onEvent(){},offEvent(){},BackButton:{show(){},hide(){},onClick(){},offClick(){}}}};"
                response = Response(js, media_type="text/javascript", headers={"Cache-Control": "no-store"})
            return await response(scope, receive, send)
        return await self.app(scope, receive, send)


async def top(scope, receive, send):
    if scope["type"] == "lifespan":
        while True:
            message = await receive()
            await send({"type": message["type"] + ".complete"})
            if message["type"] == "lifespan.shutdown":
                return
    user = parse_qs(scope.get("query_string", b"").decode()).get("user", ["alice"])[0]
    user = "bob" if user == "bob" else "alice"
    expired = parse_qs(scope.get("query_string", b"").decode()).get("expired") == ["1"]
    html = f'<!doctype html><html><head><title>Cross-site fixture</title><style>body{{margin:0}}iframe{{width:100%;height:100vh;border:0}}</style></head><body><iframe title="Mini App" src="https://localhost:18443/app/?user={user}&expired={int(expired)}"></iframe></body></html>'
    await HTMLResponse(html)(scope, receive, send)


def install_reminder_seeds(app):
    """Finite actual-Store rows for rendering; Telegram remains disabled/no sender."""
    original_lifespan = app.router.lifespan_context
    @asynccontextmanager
    async def seeded_lifespan(owner):
        async with original_lifespan(owner):
            store = app.state.store
            app.state.reminder_fixture_trips = {}
            states = ("scheduled", "sending", "sent", "canceled", "failed", "delivery_unknown")
            due = datetime.now(timezone.utc) + timedelta(days=1)
            for label, subject in (("alice", 101), ("bob", 202)):
                user = store.resolve_identity("telegram:123456", str(subject))
                trip = store.create_owned_session(user.user_id)
                trip.intent.destination = "Reminder projection fixture"
                store.save_session(trip)
                store.set_telegram_chat("123456", subject, user.user_id, trip.id, 1)
                app.state.reminder_fixture_trips[label] = trip.id
                for state in states:
                    body = ReminderInput(local_time=due.replace(tzinfo=None), timezone="UTC",
                                         consent=True, text="Canonical reminder " + state)
                    item = store.create_reminder(user.user_id, trip.id, body, due, "123456")
                    with store.transaction():
                        store.db.execute("UPDATE reminders SET state=?,attempts=? WHERE id=?",
                                         (state, 0 if state in {"scheduled", "canceled"} else 1, item["id"]))
            assert not app.state.telegram_service.enabled
            assert app.state.reminder_service._task is None
            yield
    app.router.lifespan_context = seeded_lifespan


async def main(legacy, seed_reminders=False):
    directory = ROOT / "tmp/product-iframe-fixture"
    directory.mkdir(parents=True, exist_ok=True)
    cert, key = directory / "cert.pem", directory / "key.pem"
    if not cert.exists() or not key.exists():
        openssl = shutil.which("openssl") or ("C:/Program Files/Git/usr/bin/openssl.exe" if sys.platform == "win32" else "openssl")
        subprocess.run([str(openssl), "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2",
                        "-keyout", str(key), "-out", str(cert), "-subj", "/CN=localhost",
                        "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1"], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if legacy:
        auth.set_credentials = telegram_auth.set_credentials = legacy_credentials
    app = create_app(database_path=":memory:", transport=httpx.MockTransport(forbidden),
                     dependencies=AgentDependencies(model_factory=lambda *_: NoModel(), travel_data_factory=lambda *_: NoTravelData()))
    if seed_reminders:
        install_reminder_seeds(app)
    settings = dict(host="127.0.0.1", ssl_certfile=str(cert), ssl_keyfile=str(key), access_log=False, log_level="error")
    child = uvicorn.Server(uvicorn.Config(Child(app), port=18443, **settings))
    parent = uvicorn.Server(uvicorn.Config(top, port=18444, **settings))
    await asyncio.gather(child.serve(), parent.serve())


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--legacy-lax", action="store_true")
    parser.add_argument("--seed-reminders", action="store_true")
    args = parser.parse_args()
    asyncio.run(main(args.legacy_lax, args.seed_reminders))
