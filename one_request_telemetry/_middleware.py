"""ASGI request context binding; no OpenTelemetry imports here."""
from __future__ import annotations

from . import _METHODS, _ROUTE, _runtime


def _excluded(path: str) -> bool:
    return (path in {"/", "/health", "/ready", "/metrics", "/api/metrics", "/favicon.ico", "/docs", "/redoc", "/openapi.json"}
            or path.startswith(("/assets/", "/static/"))
            or path.startswith("/api/sessions/") and path.endswith("/events")
            )


def _route_path(scope) -> str:
    """Match exclusions against a mounted application's own ASGI path."""
    path, root = scope.get("path", ""), scope.get("root_path", "").rstrip("/")
    if root and path == root:
        return "/"
    if root and path.startswith(root + "/"):
        return path[len(root):]
    return path


class TracingMiddleware:
    def __init__(self, app, *, owner, trust_incoming: bool = False):
        self.app, self.owner, self.trust_incoming = app, owner, trust_incoming

    async def __call__(self, scope, receive, send):
        runtime = getattr(self.owner.state, "tracing", None)
        if scope["type"] != "http" or runtime is None or _excluded(_route_path(scope)):
            await self.app(scope, receive, send)
            return
        method = scope.get("method", "")
        method = method if method in _METHODS else "_OTHER"
        try:
            context = runtime.request_context(scope.get("headers", ()), self.trust_incoming)
            manager = runtime.open_span("HTTP " + method, "server", {"http.request.method": method}, context=context)
            active = manager.__enter__()
        except Exception:
            await self.app(scope, receive, send)
            return
        token = _runtime.set(runtime)
        response_status = None

        async def traced_send(message):
            nonlocal response_status
            if message["type"] == "http.response.start":
                response_status = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, traced_send)
        except BaseException as error:
            try:
                runtime.mark_error(active, error)
            except Exception:
                pass
            raise
        finally:
            try:
                route = getattr(scope.get("route"), "path", None)
                if isinstance(route, str) and _ROUTE.fullmatch(route):
                    runtime.allow_route(route)
                    active.update_name(method + " " + route)
                    active.set_attribute("http.route", route)
                if response_status is not None:
                    active.set_attribute("http.response.status_code", response_status)
                    if response_status >= 500:
                        runtime.mark_http_error(active)
            except Exception:
                pass
            _runtime.reset(token)
            try:
                manager.__exit__(None, None, None)
            except Exception:
                pass
