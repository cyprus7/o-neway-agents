"""Owner-validated aftercare and reminder endpoints, protected by shared auth."""
from fastapi import Request, Response

from ..application.aftercare import AftercareApplication, DraftInput, ArchiveInput, ManualContextInput, AmendInput, ReminderInput
from ..application.errors import ApplicationError
from ..application.session_views import public_session_data


def _owner(request):
    principal = getattr(request.state, "principal", None)
    user = request.app.state.store.get_user(principal.user_id) if principal else None
    if user is None or user.kind == "legacy_unclaimed":
        raise ApplicationError(401, "authentication_required", "Необходимо войти.")
    return user.user_id


def install_aftercare(app):
    @app.get("/api/sessions/{session_id}/aftercare")
    async def aftercare(session_id: str, request: Request):
        store, owner = request.app.state.store, _owner(request)
        reminders = getattr(request.app.state, "reminder_service", None)
        return {"receipts": store.list_owned_receipts(owner, session_id), "drafts": store.list_aftercare_drafts(owner, session_id),
            "manual_context": store.get_manual_context(owner, session_id), "reminders": store.list_reminders(owner, session_id),
            "reminders_available": bool(reminders and reminders.available(owner))}

    @app.post("/api/sessions/{session_id}/drafts", status_code=201)
    async def draft(session_id: str, body: DraftInput, request: Request):
        return request.app.state.store.create_aftercare_draft(_owner(request), session_id, body)

    @app.patch("/api/sessions/{session_id}/drafts/{draft_id}")
    async def archive(session_id: str, draft_id: str, body: ArchiveInput, request: Request):
        return request.app.state.store.archive_aftercare_draft(_owner(request), session_id, draft_id)

    @app.put("/api/sessions/{session_id}/manual-context")
    async def manual(session_id: str, body: ManualContextInput, request: Request):
        return request.app.state.store.put_manual_context(_owner(request), session_id, body)

    @app.post("/api/sessions/{session_id}/amend", status_code=202)
    async def amend(session_id: str, body: AmendInput, request: Request):
        application = AftercareApplication(request.app.state.store, request.app.state.trips)
        session, job = application.amend(_owner(request), session_id, body, request.headers.get("idempotency-key"))
        request.app.state.command_worker.notify(job.id)
        return {"session": public_session_data(session), "job_id": job.id}


def install_reminders(app):
    @app.post("/api/sessions/{session_id}/reminders", status_code=201)
    async def reminder(session_id: str, body: ReminderInput, request: Request):
        service = request.app.state.reminder_service
        try:
            return service.create(_owner(request), session_id, body)
        except ValueError as exc:
            from ..application.commands import CommandError
            if isinstance(exc, CommandError):
                raise
            raise ApplicationError(422, "invalid_reminder_time", "Укажите однозначное местное время и часовой пояс IANA в пределах года.") from None

    @app.delete("/api/sessions/{session_id}/reminders/{reminder_id}", status_code=204)
    async def cancel(session_id: str, reminder_id: str, request: Request):
        request.app.state.store.cancel_reminder(_owner(request), session_id, reminder_id)
        request.app.state.reminder_service.cancel(reminder_id)
        return Response(status_code=204)
