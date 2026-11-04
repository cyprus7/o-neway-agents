"""Immutable receipt context and unsent drafts; amendments reuse trip commands."""
from datetime import date, datetime
import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .commands import CommandError
from .errors import ApplicationError
from ..domain.identity import Principal
from ..models import TravelIntent, TripOption, PlanSnapshot, local_today


class DraftInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["early_checkin", "service_request"]
    text: str = Field(min_length=1, max_length=1500)

    @field_validator("text")
    @classmethod
    def nonblank(cls, value):
        if not value.strip(): raise ValueError("blank_draft")
        return value.strip()


class ArchiveInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["archived"]


class ManualContextInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    destination: str = Field(min_length=1, max_length=160)
    hotel: str = Field(min_length=1, max_length=200)
    date_from: date
    date_to: date

    @field_validator("destination", "hotel")
    @classmethod
    def nonblank(cls, value):
        if not value.strip(): raise ValueError("blank_context")
        return value.strip()

    @model_validator(mode="after")
    def valid_dates(self):
        TravelIntent(destination=self.destination, date_from=self.date_from, date_to=self.date_to)
        return self


class AmendInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    receipt_id: str | None = Field(default=None, min_length=1, max_length=160)
    date_from: date | None = None
    date_to: date | None = None
    request: str | None = Field(default=None, min_length=1, max_length=1500)

    @field_validator("request")
    @classmethod
    def nonblank(cls, value):
        if value is not None and not value.strip(): raise ValueError("blank_request")
        return value.strip() if value else value

    @model_validator(mode="after")
    def useful_request(self):
        if not any((self.date_from, self.date_to, self.request)):
            raise ValueError("amendment_required")
        return self


class AftercareApplication:
    def __init__(self, store, trips):
        self.store, self.trips = store, trips

    def amend(self, owner_id, session_id, body: AmendInput, key):
        if not isinstance(key, str) or not 8 <= len(key) <= 128 or not key.strip() or any(ord(c)<32 for c in key):
            raise CommandError("invalid_idempotency_key", 422)
        request_hash = hashlib.sha256(json.dumps([session_id,body.model_dump(mode="json")], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        with self.store.transaction():
            if self.store.get_owned_session(session_id, owner_id) is None:
                raise CommandError("not_found", 404)
            prior = self.store.find_owned_command(owner_id, key)
            if prior:
                child = self.store.get_owned_session(prior.session_id, owner_id)
                if child and child.preferences.get("aftercare_amendment", {}).get("request_hash") == request_hash:
                    return child, prior
                raise CommandError("idempotency_conflict", 409)
            receipts = self.store.list_owned_receipts(owner_id, session_id)
            receipt = next((r for r in receipts if r.get("id") == body.receipt_id), None) if body.receipt_id else (receipts[0] if receipts else None)
            if body.receipt_id and receipt is None:
                raise CommandError("not_found", 404)
            manual = self.store.get_manual_context(owner_id, session_id)
            itinerary = receipt.get("itinerary") if receipt else None
            source_snapshot = None
            if isinstance(itinerary, dict):
                try:
                    intent = TravelIntent.model_validate(itinerary["intent"])
                    option = TripOption.model_validate(itinerary["option"])
                    source_snapshot = PlanSnapshot(revision=receipt.get("revision", 0), intent=intent.model_copy(deep=True), options=[option.model_copy(deep=True)])
                except (ValueError, KeyError, TypeError):
                    raise ApplicationError(409, "manual_context_required", "В чеке нет полного маршрута. Добавьте контекст вручную.") from None
                hotel = option.stay.title
                identity = option.stay.details.get("pricing_entity_id") or option.stay.details.get("catalog_property_id")
                if not intent.destination:
                    intent.destination = option.destination
            elif manual:
                intent = TravelIntent(destination=manual["destination"], date_from=manual["date_from"], date_to=manual["date_to"])
                hotel, identity = manual["hotel"], None
            else:
                raise ApplicationError(409, "manual_context_required", "В старом чеке нет маршрута. Добавьте контекст вручную.")
            patch = intent.model_dump(mode="json")
            if body.date_from: patch["date_from"] = body.date_from.isoformat()
            if body.date_to: patch["date_to"] = body.date_to.isoformat()
            # Preserve source identity as labeled context, never a real booking.
            label = "aftercare_hotel_unverified:" + hotel + ("; source_id="+str(identity) if identity else "")
            patch["soft_constraints"] = list(dict.fromkeys([*intent.soft_constraints, label]))
            try:
                intent = TravelIntent.model_validate(patch)
                if not intent.date_from or not intent.date_to or intent.date_from < local_today():
                    raise ValueError("new_dates_required")
            except ValueError:
                raise ApplicationError(422, "invalid_amendment_dates", "Укажите будущие даты поездки от 1 до 90 ночей.") from None
            application = self.trips.for_principal(Principal(owner_id))
            child = self.store.create_owned_session(owner_id, parent_trip_id=session_id)
            child.intent = intent
            child.previous_plan = source_snapshot
            child.preferences["aftercare_amendment"] = {"parent_trip_id": session_id, "request_hash": request_hash,
                "receipt_id": receipt.get("id") if receipt else None, "hotel": hotel, "source_identity": identity,
                "booking_verified": False, "fixed_hotel": source_snapshot is not None}
            self.store.save_session(child)
            content = body.request or f"Измени даты поездки на {intent.date_from} — {intent.date_to}. Сохрани остальные пожелания и контекст размещения."
            job, _ = application.message(child.id, content, key=key, expected_revision=0)
            return self.store.get_owned_session(child.id, owner_id), job


class ReminderInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    local_time: datetime
    timezone: str = Field(min_length=1, max_length=100)
    consent: bool = Field(strict=True)
    text: str = Field(min_length=1, max_length=500)

    @field_validator("local_time")
    @classmethod
    def naive_time(cls, value):
        if value.tzinfo is not None: raise ValueError("local_time_requires_no_offset")
        return value

    @field_validator("consent")
    @classmethod
    def explicit_consent(cls, value):
        if value is not True: raise ValueError("explicit_consent_required")
        return value

    @field_validator("text")
    @classmethod
    def nonblank(cls, value):
        if not value.strip(): raise ValueError("blank_reminder")
        return value.strip()
