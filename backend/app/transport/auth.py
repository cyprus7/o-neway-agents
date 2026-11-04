"""Same-origin cookie authentication and host identity boundary."""
import hashlib
import hmac
import inspect
import os

from fastapi import Request
from fastapi.responses import JSONResponse

from ..domain.identity import Principal

AUTH_COOKIE = "one_request_auth"
CSRF_COOKIE = "one_request_csrf"
PUBLIC_API = {"/api/config", "/api/auth/guest", "/api/auth/telegram", "/api/telegram/webhook"}


def route_path(request):
    path = request.scope["path"]
    root = request.scope.get("root_path", "")
    return path[len(root):] if root and path.startswith(root) else path


def origin_allowed(request):
    origin = request.headers.get("origin")
    configured = os.getenv("APP_ALLOWED_ORIGINS", "")
    origins = {item.strip().rstrip("/") for item in configured.split(",") if item.strip()}
    if not origins:
        origins = {f"{request.url.scheme}://{request.url.netloc}"}
    return origin is not None and origin.rstrip("/") in origins


def error(status, code, message):
    return JSONResponse({"detail": {"code": code, "message": message}}, status_code=status,
                        headers={"Cache-Control": "no-store"})


def access_still_valid(request, session_id=None):
    store = request.app.state.store
    principal = getattr(request.state, "principal", None)
    if principal is None:
        return False
    user = store.get_user(principal.user_id)
    if user is None or user.kind == "legacy_unclaimed":
        return False
    token = getattr(request.state, "access_token", None)
    if token:
        current = store.resolve_access_session(token)
        if current is None or current.principal.user_id != principal.user_id:
            return False
    return session_id is None or store.session_owner(session_id) == principal.user_id


def _cookie(response, name, value, *, partitioned=False, **attributes):
    first = len(response.raw_headers)
    response.set_cookie(name, value, **attributes)
    if partitioned:
        # Python <3.14 cannot represent this attribute in SimpleCookie. Change
        # only the Set-Cookie header(s) emitted by this call, never global parsing.
        for index in range(first, len(response.raw_headers)):
            key, header = response.raw_headers[index]
            if key.lower() == b"set-cookie":
                response.raw_headers[index] = (key, header + b"; Partitioned")


def clear_credentials(response, *, partitioned):
    for name in (AUTH_COOKIE, CSRF_COOKIE):
        _cookie(response, name, "", max_age=0, expires=0, path="/", secure=True,
                httponly=name == AUTH_COOKIE, samesite="none" if partitioned else "lax",
                partitioned=partitioned)


def set_credentials(response, request, credentials, *, partitioned=False):
    response.headers["Cache-Control"] = "no-store"
    # Remove the opposite storage variant before minting the active pair, so
    # same-name cookies cannot shadow the verified channel's credentials.
    clear_credentials(response, partitioned=not partitioned)
    # Secure is waived only for a loopback development host, never arbitrary HTTP.
    secure = partitioned or request.url.scheme == "https" or request.url.hostname not in {"localhost", "127.0.0.1", "::1"}
    _cookie(response, AUTH_COOKIE, credentials.token, max_age=43200, httponly=True,
            secure=secure, samesite="none" if partitioned else "lax", path="/", partitioned=partitioned)
    _cookie(response, CSRF_COOKIE, credentials.csrf_token, max_age=43200, httponly=False,
            secure=secure, samesite="none" if partitioned else "lax", path="/", partitioned=partitioned)


def install_auth(app):
    @app.middleware("http")
    async def identity_boundary(request, call_next):
        path = route_path(request)
        if not path.startswith("/api/") or path in PUBLIC_API:
            response = await call_next(request)
            if path.startswith("/api/auth/"):
                response.headers["Cache-Control"] = "no-store"
            return response
        store = request.app.state.store
        resolver = request.app.state.identity_resolver
        if resolver:
            principal = resolver(request)
            if inspect.isawaitable(principal):
                principal = await principal
            if not isinstance(principal, Principal):
                return error(401, "authentication_required", "Необходимо войти.")
            user = store.get_user(principal.user_id)
            if user is None or user.kind == "legacy_unclaimed":
                return error(401, "authentication_required", "Необходимо войти.")
            store.touch_user_activity(principal.user_id)
            request.state.principal = principal
        else:
            token = request.cookies.get(AUTH_COOKIE, "")
            access = store.resolve_access_session(token)
            if access is None:
                return error(401, "authentication_required", "Сеанс завершён. Войдите снова.")
            request.state.principal = access.principal
            request.state.access_token = token
            if request.method not in {"GET", "HEAD", "OPTIONS"}:
                supplied = request.headers.get("x-csrf-token", "")
                if not origin_allowed(request) or not hmac.compare_digest(
                        hashlib.sha256(supplied.encode()).hexdigest(), access.csrf_hash):
                    return error(403, "csrf_rejected", "Обновите страницу и повторите действие.")
        if path == "/api/metrics" and not request.state.principal.operator:
            return error(403, "operator_required", "Доступ только для оператора.")
        response = await call_next(request)
        if path.startswith("/api/auth/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.post("/api/auth/guest")
    async def guest(request: Request):
        if not origin_allowed(request):
            return error(403, "origin_rejected", "Недопустимый источник запроса.")
        store = request.app.state.store
        token = request.cookies.get(AUTH_COOKIE, "")
        current = store.resolve_access_session(token)
        csrf = request.cookies.get(CSRF_COOKIE, "")
        if current and hmac.compare_digest(hashlib.sha256(csrf.encode()).hexdigest(), current.csrf_hash):
            user = store.get_user(current.principal.user_id)
            return {"user": {"id": user.user_id, "kind": user.kind}, "csrf_token": csrf}
        user = store.get_user(current.principal.user_id) if current else store.create_user()
        credentials = store.create_access_session(user.user_id)
        if current:
            store.revoke_access_session(token)
        response = JSONResponse({"user": {"id": user.user_id, "kind": user.kind}, "csrf_token": credentials.csrf_token})
        # Default registered cookie users come from verified Telegram login.
        # This selects delivery attributes; it does not establish identity.
        # Host IdentityResolver integrations retain their own edge policy.
        set_credentials(response, request, credentials, partitioned=user.kind == "registered")
        return response

    @app.get("/api/auth/session")
    async def identity(request: Request):
        user = request.app.state.store.get_user(request.state.principal.user_id)
        return {"user": {"id": user.user_id, "kind": user.kind}}

    @app.post("/api/auth/logout")
    async def logout(request: Request):
        token = getattr(request.state, "access_token", None)
        if token:
            request.app.state.store.revoke_access_session(token)
        response = JSONResponse({"status": "signed_out"})
        clear_credentials(response, partitioned=False)
        clear_credentials(response, partitioned=True)
        return response
