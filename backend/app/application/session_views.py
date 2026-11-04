"""Read complete committed sessions without invoking providers or inference."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from ..domain.session_updates import SessionSnapshotReader, SessionView, UpdateReason
from ..models import Session
from ..planner import apply_option_pricing
from ..regional_events import EventResult


def public_session_data(session: Session, *, now: datetime | None = None) -> dict[str, Any]:
    """Projection time changes freshness, never saved observation timestamps."""
    clock = now or datetime.now(timezone.utc)
    projected = session.model_copy(deep=True)
    for option in projected.options:
        apply_option_pricing(option, projected.intent, now=clock)
        events = option.regional_events
        expiry = events_expiry(events)
        if events.status in {"ready", "empty"} and expiry is not None and expiry <= clock:
            events.status, events.items = "unavailable", []
    payload = projected.model_dump(mode="json")
    if projected.previous_plan is not None:
        previous = projected.previous_plan
        # Historical scope is independent of the current draft's changed dates/party.
        payload['previous_plan']['options'] = public_session_data(
            Session(intent=previous.intent, options=previous.options), now=clock)['options']
    for option, output in zip(projected.options, payload["options"]):
        for name in ("flight", "stay", "transfer"):
            component = getattr(option, name)
            quote = component.quote
            if quote is not None:
                delivery = component.details.get('pricing_observation_delivery', component.details.get("pricing_delivery", "live_fetch"))
                output[name]["quote"] = quote.public_dict(now=clock, delivery=delivery if delivery in ("live_fetch", "cache_reuse", "history") else "live_fetch")
    return payload


def event_expiry(checked_at: str | None) -> datetime | None:
    if not checked_at:
        return None
    try:
        checked = datetime.fromisoformat(checked_at.replace("Z", "+00:00"))
        return checked + timedelta(hours=1) if checked.tzinfo else None
    except (ValueError, TypeError):
        return None


def events_expiry(events: EventResult) -> datetime | None:
    # A fresh search/cache read cannot renew the original source's age.
    times = [events.checked_at, *(item.source.retrieved_at for item in events.items)]
    return min((expiry for stamp in times if (expiry := event_expiry(stamp)) is not None), default=None)


def next_price_expiry(session: Session, *, now: datetime) -> datetime | None:
    options = session.options + (session.previous_plan.options if session.previous_plan else [])
    deadlines = [component.quote.expires_at
                for option in options
                for component in (option.flight, option.stay, option.transfer)
                if component.quote is not None and component.quote.expires_at is not None
                and component.quote.expires_at > now]
    deadlines.extend(expiry for option in options
                     if option.regional_events.status in {"ready", "empty"}
                     and (expiry := events_expiry(option.regional_events)) is not None and expiry > now)
    return min(deadlines, default=None)


class SessionViews:
    def __init__(self, snapshots: SessionSnapshotReader):
        self._snapshots = snapshots

    def read(self, session_id: str, *, reason: UpdateReason = "updated", now: datetime | None = None) -> SessionView | None:
        snapshot = self._snapshots.read_session_snapshot(session_id)
        if snapshot is None:
            return None
        clock = now or datetime.now(timezone.utc)
        return SessionView(sequence=snapshot.sequence,
                           session=public_session_data(snapshot.session, now=clock),
                           job=snapshot.job, server_time=clock.isoformat(), reason=reason,
                           next_expiry=next_price_expiry(snapshot.session, now=clock))
