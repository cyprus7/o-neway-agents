"""Owner-scoped approved memory and personal data lifecycle routes."""
from fastapi import Request, Response

from ..application.commands import CommandError
from ..application.errors import ApplicationError
from ..application.memory import MemoryInput
from ..transport.auth import AUTH_COOKIE, CSRF_COOKIE


def install_memory(app):
    def owner(request):
        principal = getattr(request.state, "principal", None)
        user = request.app.state.store.get_user(principal.user_id) if principal else None
        if user is None or user.kind == "legacy_unclaimed":
            raise ApplicationError(401, "authentication_required", "Необходимо войти.")
        return user.user_id

    def notify_deletion(request, job_ids):
        worker = getattr(request.app.state, "command_worker", None)
        if worker:
            for job_id in job_ids:
                worker.cancel(job_id)
            worker.notify()
        reminders = getattr(request.app.state, "reminder_service", None)
        if reminders:
            reminders.cancel_unavailable()

    @app.get("/api/me/memory")
    async def memory(request: Request):
        return {"items": request.app.state.store.list_memory(owner(request))}

    @app.put("/api/me/memory/{field}")
    async def remember(field: str, body: MemoryInput, request: Request):
        try:
            return request.app.state.store.put_memory(owner(request), field, body.value, body.source_trip_id)
        except CommandError:
            raise
        except ValueError:
            raise ApplicationError(422, "invalid_memory", "Этот параметр нельзя сохранить как предпочтение.") from None

    @app.delete("/api/me/memory/{field}", status_code=204)
    async def forget(field: str, request: Request):
        try:
            request.app.state.store.forget_memory(owner(request), field)
        except CommandError:
            raise
        except ValueError:
            raise ApplicationError(422, "invalid_memory", "Этот параметр нельзя сохранить как предпочтение.") from None
        return Response(status_code=204)

    @app.get("/api/me/export")
    async def export(request: Request):
        return request.app.state.store.export_owned_data(owner(request))

    @app.delete("/api/sessions/{session_id}", status_code=204)
    async def delete_trip(session_id: str, request: Request):
        job_ids = request.app.state.store.delete_owned_session(owner(request), session_id)
        notify_deletion(request, job_ids)
        return Response(status_code=204)

    @app.delete("/api/me", status_code=204)
    async def delete_account(request: Request):
        job_ids = request.app.state.store.delete_user_data(owner(request))
        notify_deletion(request, job_ids)
        response = Response(status_code=204)
        response.delete_cookie(AUTH_COOKIE, path="/")
        response.delete_cookie(CSRF_COOKIE, path="/")
        return response
