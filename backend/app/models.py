"""Public API models. Unknown inventory is always represented by null prices."""
from datetime import date, datetime, timedelta, timezone
import os
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .pricing.models import PriceObservation, PricingSummary, RoomAllocation, validate_allocation
from .pricing.demo_models import DemoPricing
from .regional_events import EventResult


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def local_today() -> date:
    try:
        zone = ZoneInfo(os.getenv("APP_TIMEZONE", "Asia/Nicosia"))
    except ZoneInfoNotFoundError:
        zone = timezone.utc
    return datetime.now(zone).date()


def uid() -> str:
    return uuid4().hex


class TravelIntent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    origin: str | None = Field(default=None, max_length=160)
    destination: str | None = Field(default=None, max_length=160)
    date_from: date | None = None
    date_to: date | None = None
    flight_departure_from: date | None = None
    flight_departure_to: date | None = None
    nights_min: int | None = Field(default=None, ge=1, le=90)
    nights_max: int | None = Field(default=None, ge=1, le=90)
    adults: int = Field(default=2, ge=1, le=9)
    children_ages: list[int] = Field(default_factory=list, max_length=8)
    rooms: int = Field(default=1, ge=1, le=9)
    room_allocation: list[RoomAllocation] | None = None
    pets: list[str] = Field(default_factory=list, max_length=5)
    budget_total: float | None = Field(default=None, gt=0, le=100_000_000)
    currency: str = Field(default="EUR", pattern=r"^[A-Z]{3}$")
    climate_preferences: list[str] = Field(default_factory=list, max_length=12)
    trip_styles: list[str] = Field(default_factory=list, max_length=12)
    max_travel_minutes: int | None = Field(default=None, ge=1, le=10080)
    direct_flight_preferred: bool = False
    hotel_requirements: list[str] = Field(default_factory=list, max_length=20)
    hard_constraints: list[str] = Field(default_factory=list, max_length=20)
    soft_constraints: list[str] = Field(default_factory=list, max_length=20)
    assumptions: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("children_ages")
    @classmethod
    def valid_ages(cls, values: list[int]) -> list[int]:
        if any(age < 0 or age > 17 for age in values):
            raise ValueError("Children must be aged 0–17")
        return values

    @field_validator("pets", "climate_preferences", "trip_styles", "hotel_requirements", "hard_constraints", "soft_constraints", "assumptions")
    @classmethod
    def short_strings(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(value.strip()[:500] for value in values if value.strip()))

    @model_validator(mode="after")
    def check_ranges(self) -> "TravelIntent":
        if (self.flight_departure_from is None) != (self.flight_departure_to is None):
            raise ValueError("Both departure window dates are required")
        if self.flight_departure_from is not None:
            if not 0 <= (self.flight_departure_to - self.flight_departure_from).days <= 2:
                raise ValueError("Departure window must contain at most three dates")
            if self.date_from and not self.date_from - timedelta(days=3) <= self.flight_departure_from <= self.flight_departure_to <= self.date_from:
                raise ValueError("Departure window must be within three days before check-in")
        if self.date_from and self.date_to:
            nights = (self.date_to - self.date_from).days
            if nights < 1 or nights > 90:
                raise ValueError("Trip duration must be 1–90 nights")
        if self.nights_min and self.nights_max and self.nights_min > self.nights_max:
            raise ValueError("Invalid night range")
        validate_allocation(self.adults, tuple(self.children_ages), self.rooms,
                            tuple(self.room_allocation) if self.room_allocation is not None else None)
        return self


class IntentExtraction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # A dictionary deliberately records which fields the model actually changed.
    state_patch: dict[str, Any]
    blocking_question: str | None = Field(default=None, max_length=1500)
    action: Literal["clarify", "explore", "refine", "compare", "explain"] = "refine"
    option_ids: list[str] = Field(default_factory=list, max_length=3)


def merge_intent(previous: TravelIntent, patch: dict[str, Any]) -> tuple[TravelIntent, list[str]]:
    """Replace only explicitly supplied fields, preserving prior clarification state."""
    unknown = set(patch) - set(TravelIntent.model_fields)
    if unknown:
        raise ValueError("Unknown intent fields")
    before = previous.model_dump(mode="json")
    merged = dict(before)
    merged.update(patch)
    if ("date_from" in patch and patch["date_from"] and previous.date_from
            and not {"flight_departure_from", "flight_departure_to"} & set(patch)
            and previous.flight_departure_from is not None):
        delta = date.fromisoformat(str(patch["date_from"])) - previous.date_from
        merged["flight_departure_from"] = previous.flight_departure_from + delta
        merged["flight_departure_to"] = previous.flight_departure_to + delta
    # A duration change must not leave an old, inconsistent return date in place.
    if "date_to" not in patch and ("date_from" in patch or "nights_min" in patch or "nights_max" in patch):
        start = merged.get("date_from")
        nights = merged.get("nights_min") or merged.get("nights_max")
        if start and nights:
            from datetime import timedelta
            start_date = date.fromisoformat(str(start)) if not isinstance(start, date) else start
            merged["date_to"] = (start_date + timedelta(days=int(nights))).isoformat()
    intent = TravelIntent.model_validate(merged)
    after = intent.model_dump(mode="json")
    return intent, [key for key in before if before[key] != after[key]]


class Message(BaseModel):
    id: str = Field(default_factory=uid)
    role: Literal["user", "assistant"]
    content: str
    created_at: str = Field(default_factory=utcnow)


class AgentEvent(BaseModel):
    id: str = Field(default_factory=uid)
    step: str
    status: Literal["running", "completed", "warning", "failed"]
    message: str
    created_at: str = Field(default_factory=utcnow)
    details: dict[str, Any] = Field(default_factory=dict)


class Evidence(BaseModel):
    provider: str
    source_url: str | None
    retrieved_at: str
    freshness: Literal["live", "cached", "snapshot", "unavailable"]
    confidence: str
    label: str


class TravelComponent(BaseModel):
    title: str
    description: str
    price: float | None = Field(default=None, ge=0)
    currency: str = "EUR"
    source_url: str | None = None
    status: Literal["observed", "unavailable", "estimated"] = "unavailable"
    details: dict[str, Any] = Field(default_factory=dict)
    evidence: list[Evidence] = Field(default_factory=list)
    quote: PriceObservation | None = None


class TripOption(BaseModel):
    id: str
    destination: str
    country: str
    country_code: str | None = Field(default=None, pattern=r"^[A-Z]{2}$")
    destination_scope: Literal["locality", "country"] = "locality"
    destination_aliases: tuple[str, ...] = Field(default=(), max_length=24)
    subtitle: str
    latitude: float
    longitude: float
    score: float
    estimated_total: float | None = None
    currency: str
    price_status: Literal["observed", "partial", "unavailable"]
    flight: TravelComponent
    stay: TravelComponent
    transfer: TravelComponent
    weather: dict[str, Any] | None = None
    reasons: list[str]
    tradeoffs: list[str]
    risks: list[str]
    evidence: list[Evidence]
    constraint_checks: list[dict[str, Any]]
    image_url: str | None = None
    booking_url: str | None = None
    pricing_summary: PricingSummary | None = None
    demo_pricing: DemoPricing | None = None
    regional_events: EventResult = Field(default_factory=EventResult)


class PlanSnapshot(BaseModel):
    revision: int
    intent: TravelIntent
    options: list[TripOption] = Field(default_factory=list, max_length=3)


class Session(BaseModel):
    id: str = Field(default_factory=uid)
    created_at: str = Field(default_factory=utcnow)
    updated_at: str = Field(default_factory=utcnow)
    revision: int = 0
    intent: TravelIntent = Field(default_factory=TravelIntent)
    messages: list[Message] = Field(default_factory=list)
    options: list[TripOption] = Field(default_factory=list)
    events: list[AgentEvent] = Field(default_factory=list)
    selected_option_id: str | None = None
    assumptions: list[str] = Field(default_factory=list)
    clarification: str | None = None
    last_changes: list[str] = Field(default_factory=list)
    preferences: dict[str, Any] = Field(default_factory=dict)
    accepted_plan_revision: int | None = None
    previous_plan: PlanSnapshot | None = None


class Job(BaseModel):
    id: str = Field(default_factory=uid)
    session_id: str
    kind: Literal["message", "prices_refresh"] = "message"
    status: Literal["queued", "running", "completed", "failed", "superseded", "canceled", "interrupted"] = "queued"
    error: str | None = None
    captured_revision: int | None = None
    result_revision: int | None = None
    target_option_ids: list[str] = Field(default_factory=list, max_length=3)
    result_option_ids: list[str] = Field(default_factory=list, max_length=3)
    created_at: str = Field(default_factory=utcnow)
    started_at: str | None = None
    deadline_at: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    outcomes: list[dict[str, Any]] = Field(default_factory=list)
    progress: str | None = None


class MessageInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int | None = Field(default=None, ge=0, strict=True)
    content: str = Field(min_length=1, max_length=6000)
    action: Literal["compare", "explain"] | None = None
    option_ids: list[str] = Field(default_factory=list, max_length=3)

    @field_validator("content")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Message cannot be empty")
        return value.strip()


class OptionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    option_id: str = Field(min_length=1, max_length=160)
    expected_revision: int | None = Field(default=None, ge=0)


class PriceRefreshInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=0)
    option_id: str | None = Field(default=None, min_length=1, max_length=160)
