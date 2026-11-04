"""Price evidence never implies inventory or applicability to a different trip.

``value`` preserves a sourced public amount, including a published reference.
Only ``normalized_group`` may participate in the requested trip's arithmetic.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Annotated, Literal, Union
from urllib.parse import parse_qsl, urlsplit
from uuid import uuid4

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, field_validator, model_validator


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def decimal_string(value: object) -> str:
    # Do not turn a binary float into an apparently exact monetary amount.
    if isinstance(value, (float, bool)) or not isinstance(value, (str, Decimal, int)):
        raise ValueError("Money must be a decimal string, Decimal or integer")
    try:
        amount = Decimal(value)
    except (InvalidOperation, ValueError):
        raise ValueError("Invalid decimal amount") from None
    if not amount.is_finite() or amount < 0 or amount > Decimal("1000000000000"):
        raise ValueError("Money must be finite, nonnegative and bounded")
    if amount.as_tuple().exponent < -8:
        raise ValueError("At most eight decimal places are supported")
    return format(amount, "f")


Money = Annotated[str, BeforeValidator(decimal_string)]
Currency = Annotated[str, Field(pattern=r"^[A-Z]{3}$")]
ComponentKind = Literal["flight", "stay", "transfer", "rail", "bus", "car"]
TripType = Literal["roundtrip", "one_way", "not_applicable"]
PriceUnit = Literal["group_trip", "group_stay", "person_trip", "room_night", "group_night", "unknown"]
Freshness = Literal["live", "cached", "stale", "unavailable"]
Outcome = Literal["quoted", "reference", "no_offers", "blocked", "requires_login", "timeout", "parse_error", "context_mismatch", "not_configured", "policy_disabled", "unsupported_party", "currency_mismatch", "unavailable"]


class FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PointPrice(FrozenModel):
    kind: Literal["point"] = "point"
    amount: Money
    currency: Currency


class RangePrice(FrozenModel):
    kind: Literal["closed_range"] = "closed_range"
    min: Money
    max: Money
    currency: Currency

    @model_validator(mode="after")
    def ordered(self) -> "RangePrice":
        if Decimal(self.min) > Decimal(self.max):
            raise ValueError("Range minimum exceeds maximum")
        return self


class LowerBoundPrice(FrozenModel):
    kind: Literal["lower_bound"] = "lower_bound"
    min: Money
    max: None = None
    currency: Currency


class UnknownPrice(FrozenModel):
    kind: Literal["unknown"] = "unknown"
    reason: str = Field(min_length=1, max_length=1000)


PriceValue = Annotated[Union[PointPrice, RangePrice, LowerBoundPrice, UnknownPrice], Field(discriminator="kind")]


class RoomAllocation(FrozenModel):
    adults: int = Field(ge=0, le=9)
    children_ages: tuple[int, ...] = Field(default=(), max_length=8)

    @field_validator("children_ages")
    @classmethod
    def ages(cls, values: tuple[int, ...]) -> tuple[int, ...]:
        if any(age < 0 or age > 17 for age in values):
            raise ValueError("Children must be aged 0–17")
        return values

    @model_validator(mode="after")
    def occupied(self) -> "RoomAllocation":
        if self.adults == 0 and not self.children_ages:
            raise ValueError("An allocated room cannot be empty")
        return self


def validate_allocation(adults: int | None, ages: tuple[int, ...] | None, rooms: int | None,
                        allocation: tuple[RoomAllocation, ...] | None) -> None:
    if allocation is not None:
        if adults is None or ages is None or rooms is None:
            raise ValueError("Room allocation requires a known party and room count")
        if len(allocation) != rooms or sum(room.adults for room in allocation) != adults:
            raise ValueError("Room allocation does not match adults/rooms")
        allocated_ages = [age for room in allocation for age in room.children_ages]
        if sorted(allocated_ages) != sorted(ages):
            raise ValueError("Room allocation does not match child ages")


class RequestContext(FrozenModel):
    component: ComponentKind
    origin: str | None = Field(default=None, max_length=200)
    destination: str = Field(min_length=1, max_length=200)
    destination_city: str | None = Field(default=None, max_length=200)
    date_from: date
    date_to: date
    adults: int = Field(default=2, ge=1, le=9)
    children_ages: tuple[int, ...] = Field(default=(), max_length=8)
    rooms: int = Field(default=1, ge=1, le=9)
    room_allocation: tuple[RoomAllocation, ...] | None = None
    currency: Currency = "EUR"
    trip_type: TripType = "roundtrip"
    fare_constraints: tuple[str, ...] = ()
    target_policy: Literal["fixed_entity", "candidate_allowed"] = "candidate_allowed"
    entity_id: str | None = None

    @field_validator("children_ages")
    @classmethod
    def ages(cls, values: tuple[int, ...]) -> tuple[int, ...]:
        return RoomAllocation.ages(values)

    @model_validator(mode="after")
    def valid_context(self) -> "RequestContext":
        if not 1 <= (self.date_to - self.date_from).days <= 90:
            raise ValueError("Trip duration must be 1–90 nights")
        if self.component in ("flight", "rail", "bus", "car") and not self.origin:
            raise ValueError("Transport requires an origin")
        if self.target_policy == "fixed_entity" and not self.entity_id:
            raise ValueError("Fixed-entity requests require an entity ID")
        validate_allocation(self.adults, self.children_ages, self.rooms, self.room_allocation)
        return self


class QuoteScope(FrozenModel):
    component: ComponentKind
    entity_level: Literal["property", "route", "destination_lodging"]
    entity_id: str | None = None
    provider_entity_id: str | None = None
    origin: str | None = Field(default=None, max_length=200)
    destination: str | None = Field(default=None, max_length=200)
    destination_city: str | None = Field(default=None, max_length=200)
    date_from: date | None = None
    date_to: date | None = None
    adults: int | None = Field(default=None, ge=1, le=9)
    children_ages: tuple[int, ...] | None = Field(default=None, max_length=8)
    rooms: int | None = Field(default=None, ge=1, le=9)
    room_allocation: tuple[RoomAllocation, ...] | None = None
    trip_type: TripType = "not_applicable"
    unit: PriceUnit = "unknown"
    currency: Currency | None = None
    fare_class: str | None = None
    rate_id: str | None = None
    fare_constraints: tuple[str, ...] = ()
    taxes: Literal["included", "excluded", "unknown"] = "unknown"
    mandatory_fees: Literal["included", "excluded", "unknown"] = "unknown"
    inclusions: tuple[str, ...] = ()
    exclusions: tuple[str, ...] = ()
    context_verified: bool = False
    unverified_fields: tuple[str, ...] = ()
    assumptions: tuple[str, ...] = ()

    @field_validator("children_ages")
    @classmethod
    def ages(cls, values: tuple[int, ...] | None) -> tuple[int, ...] | None:
        return RoomAllocation.ages(values) if values is not None else None

    @model_validator(mode="after")
    def valid_scope(self) -> "QuoteScope":
        if self.date_from and self.date_to and self.date_to <= self.date_from:
            raise ValueError("Invalid quote dates")
        validate_allocation(self.adults, self.children_ages, self.rooms, self.room_allocation)
        if self.context_verified:
            required = (self.destination, self.date_from, self.date_to, self.adults, self.currency)
            if any(item is None for item in required) or self.children_ages is None or self.unit == "unknown":
                raise ValueError("Verified context requires dates, destination, full party, currency and unit")
            if self.component == "stay" and self.rooms is None:
                raise ValueError("Verified lodging requires room count")
            if self.component in ("flight", "rail", "bus", "car") and not self.origin:
                raise ValueError("Verified transport requires an origin")
            if self.unverified_fields:
                raise ValueError("A verified context cannot contain unverified fixed fields")
        return self


class EstimateBasis(FrozenModel):
    method: Literal["minmax_comparables_v1"] = "minmax_comparables_v1"
    basis_ids: tuple[str, ...] = Field(min_length=2)
    sample_count: int = Field(ge=2)
    observed_from: datetime
    observed_to: datetime
    entity_level: Literal["property", "route", "destination_lodging"]
    differences: tuple[str, ...] = ()
    assumptions: tuple[str, ...] = ()

    @model_validator(mode="after")
    def valid_basis(self) -> "EstimateBasis":
        if len(set(self.basis_ids)) != self.sample_count or len(self.basis_ids) != self.sample_count:
            raise ValueError("Estimate basis requires distinct observations")
        if self.observed_from.tzinfo is None or self.observed_to.tzinfo is None or self.observed_from > self.observed_to:
            raise ValueError("Invalid estimate observation window")
        return self


class ProviderPolicy(FrozenModel):
    id: str
    enabled: bool = True
    access_mode: Literal["structured", "browser", "published_reference"] = "structured"
    permitted_domains: tuple[str, ...] = ()
    supported_components: tuple[ComponentKind, ...] = ("flight", "stay")
    cache_reuse_allowed: bool = False
    retention_allowed: bool = False
    historical_estimate_allowed: bool = False
    ttl_seconds: int = Field(default=1800, ge=1, le=86400 * 7)
    retention_seconds: int = Field(default=0, ge=0, le=86400 * 7)
    query_rate_per_minute: int = Field(default=6, ge=1, le=100)
    concurrency: int = Field(default=2, ge=1, le=2)
    deadline_seconds: int = Field(default=20, ge=1, le=55)
    credential_configured: bool = False


class PriceObservation(FrozenModel):
    id: str = Field(default_factory=lambda: uuid4().hex)
    provider_id: str = Field(min_length=1, max_length=160)
    request_context_hash: str | None = None
    scope: QuoteScope | None = None
    value: PriceValue = Field(default_factory=lambda: UnknownPrice(reason="Цена не получена"))
    normalized_group: PriceValue | None = None
    source_url: str | None = Field(default=None, max_length=4000)
    observed_at: datetime | None = None
    retrieved_at: datetime = Field(default_factory=utc_now)
    expires_at: datetime | None = None
    outcome: Outcome = "unavailable"
    diagnostic_code: str | None = Field(default=None, max_length=160)
    reason: str | None = Field(default=None, max_length=1500)
    unit_label: str | None = Field(default=None, max_length=400)
    included_charges: tuple[str, ...] = ()
    excluded_charges: tuple[str, ...] = ()
    unknown_charges: tuple[str, ...] = ()
    component_complete: bool = False
    availability_verified: bool = False
    free_component: bool = False
    normalization_note: str | None = None
    evidence_ids: tuple[str, ...] = ()
    provider_policy_id: str | None = None
    request_key: str | None = None
    upstream_id: str | None = None
    basis: EstimateBasis | None = None

    @field_validator("source_url")
    @classmethod
    def public_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parsed = urlsplit(value)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("Price source must be an HTTP(S) URL without credentials")
        secret_names = {"api_key", "apikey", "access_token", "token", "auth", "authorization", "session", "session_id", "sid", "cookie"}
        if any(name.casefold() in secret_names for name, _ in parse_qsl(parsed.query)):
            raise ValueError("Price source URL must not contain credential/session query parameters")
        return value

    @field_validator("observed_at", "retrieved_at", "expires_at")
    @classmethod
    def aware_utc(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("Price timestamps must include a timezone")
        return value.astimezone(timezone.utc) if value is not None else None

    @model_validator(mode="after")
    def valid_observation(self) -> "PriceObservation":
        if self.retrieved_at > utc_now() + timedelta(seconds=60):
            raise ValueError("Retrieval timestamp is in the future")
        if self.observed_at and self.observed_at > self.retrieved_at:
            raise ValueError("Observation cannot be later than retrieval")
        if self.expires_at and (self.observed_at is None or self.expires_at <= self.observed_at):
            raise ValueError("Expiry must follow the observation")
        if self.value.kind != "unknown":
            if not self.source_url or self.observed_at is None:
                raise ValueError("A sourced numeric value requires a URL and observation time")
            minimum = self.value.amount if self.value.kind == "point" else self.value.min
            if Decimal(minimum) == 0 and not self.free_component:
                raise ValueError("Zero is reserved for explicitly sourced free components")
            if self.scope and self.scope.currency and self.scope.currency != self.value.currency:
                raise ValueError("Raw value and source scope currencies differ")
        if self.normalized_group is not None:
            if self.outcome != "quoted" or not self.scope or not self.scope.context_verified:
                raise ValueError("Only a context-verified quote may have a normalized group value")
            if self.normalized_group.kind == "unknown" or self.value.kind == "unknown":
                raise ValueError("Normalized group value must be numeric")
            if self.normalized_group.currency != self.value.currency:
                raise ValueError("No implicit FX conversion is permitted")
            if self.normalized_group.kind != self.value.kind:
                raise ValueError("Normalization must preserve point/range/lower-bound uncertainty")
            permitted_units = ("group_stay",) if self.scope.component == "stay" else ("group_trip",)
            if self.scope.unit not in permitted_units:
                raise ValueError("This release normalizes only explicitly quoted whole-group amounts")
            from .normalization import normalized_value
            expected = normalized_value(self.value)
            if normalized_value(self.normalized_group) != expected:
                raise ValueError("Whole-group normalization cannot multiply or change a sourced amount")
            object.__setattr__(self, "normalized_group", expected)
        if self.outcome == "quoted":
            if not self.scope or not self.scope.context_verified or self.normalized_group is None or self.expires_at is None:
                raise ValueError("A quote needs verified scope, normalization and expiry")
        if self.outcome == "reference" and self.normalized_group is not None:
            raise ValueError("A published reference is not a requested-group quote")
        return self

    @property
    def amount_kind(self) -> str:
        return {"point": "observed", "closed_range": "estimate_range", "lower_bound": "lower_bound", "unknown": "unknown"}[self.value.kind]

    def freshness(self, now: datetime | None = None, delivery: Literal["live_fetch", "cache_reuse", "history"] = "live_fetch") -> Freshness:
        clock = now or utc_now()
        if clock.tzinfo is None:
            raise ValueError("Freshness clock must include timezone")
        if self.value.kind == "unknown" or self.observed_at is None:
            return "unavailable"
        if (self.expires_at is not None and clock >= self.expires_at) or delivery == "history":
            return "stale"
        return "cached" if delivery == "cache_reuse" else "live"

    def public_dict(self, now: datetime | None = None, delivery: Literal["live_fetch", "cache_reuse", "history"] = "live_fetch") -> dict:
        from .normalization import offer_scope_hash
        data = self.model_dump(mode="json")
        data["freshness"] = self.freshness(now, delivery)
        data["amount_kind"] = self.amount_kind
        data["offer_scope_hash"] = offer_scope_hash(self.scope) if self.scope else None
        return data


Quote = PriceObservation


class PricingSummary(FrozenModel):
    display_kind: Literal["observed_subtotal", "estimated_subtotal", "lower_bound", "partial", "unavailable"]
    transport_stay: PriceValue | None = None
    known_partial: PriceValue | None = None
    full_trip: PriceValue | None = None
    included_components: tuple[str, ...] = ()
    missing_components: tuple[str, ...] = ()
    reference_components: tuple[str, ...] = ()
    missing_reasons: dict[str, str] = Field(default_factory=dict)
    excluded_charges: tuple[str, ...] = ()
    unknown_charges: tuple[str, ...] = ()
    currency: Currency
    scope_label: str = "За всех путешественников и весь срок поездки"
    as_of: datetime | None = None
    expires_at: datetime | None = None
    quote_ids: tuple[str, ...] = ()
    assumptions: tuple[str, ...] = ()
    budget_assessment: Literal["within_observed", "over_observed", "within_estimate", "crosses_estimate", "over_estimate", "incomplete", "unknown"] = "unknown"
    budget_explanation: str = "Соответствие бюджету не подтверждено"
    hard_constraint_status: Literal["passed", "failed", "unknown"] = "unknown"
