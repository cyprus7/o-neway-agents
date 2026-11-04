"""Pure, bounded flight-search.v1 values shared with the provider gateway."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StrictBool, field_validator, model_validator, model_serializer


VERSION = "flight-search.v1"
ProviderID = Literal["google_flights", "yandex_avia"]
Outcome = Literal["reference", "no_offers", "not_configured", "unsupported_party", "currency_mismatch",
                  "context_mismatch", "timeout", "blocked", "requires_login", "unavailable", "parse_error"]
DiagnosticCode = Literal[
    "flight_reference", "flight_unsupported_route", "flight_unsupported_party", "flight_only_eur",
    "flight_unsupported_cabin", "flight_unsupported_constraints", "flight_invalid_deadline", "flight_overloaded",
    "flight_browser_unavailable", "flight_challenge", "flight_login", "flight_scope_not_verified",
    "flight_route_mismatch", "flight_date_mismatch", "flight_party_mismatch", "flight_currency_mismatch",
    "flight_cabin_mismatch", "flight_no_compatible_itinerary", "flight_target_not_found",
    "flight_price_not_verified", "flight_source_unavailable", "flight_parse_error", "flight_timeout",
    "flight_cancelled",
]
ShortText = Annotated[str, Field(min_length=1, max_length=160)]
Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
Airport = Annotated[str, Field(pattern=r"^[A-Z]{3}$")]


class Value(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _utc(value: datetime) -> datetime:
    if value.utcoffset() != timedelta(0):
        raise ValueError("UTC timestamp required")
    return value


class RoomAllocation(Value):
    adults: int = Field(ge=0, le=9)
    children_ages: tuple[int, ...] = Field(default=(), max_length=8)


class FlightRequestContext(Value):
    """Mirror existing RequestContext fields, preserving its exact hash contract."""
    component: Literal["flight"] = "flight"
    origin: str = Field(min_length=1, max_length=200)
    destination: str = Field(min_length=1, max_length=200)
    destination_city: str | None = Field(default=None, max_length=200)
    date_from: date
    date_to: date
    adults: int = Field(default=2, ge=1, le=9)
    children_ages: tuple[int, ...] = Field(default=(), max_length=8)
    rooms: int = Field(default=1, ge=1, le=9)
    room_allocation: tuple[RoomAllocation, ...] | None = Field(default=None, max_length=9)
    currency: str = Field(default="EUR", pattern=r"^[A-Z]{3}$")
    trip_type: Literal["roundtrip", "one_way", "not_applicable"] = "roundtrip"
    fare_constraints: tuple[Annotated[str, Field(max_length=600)], ...] = Field(default=(), max_length=20)
    target_policy: Literal["fixed_entity", "candidate_allowed"] = "candidate_allowed"
    entity_id: str | None = Field(default=None, max_length=512)

    @model_validator(mode="after")
    def valid_scope(self):
        if not 1 <= (self.date_to - self.date_from).days <= 90 or any(age < 0 or age > 17 for age in self.children_ages):
            raise ValueError("Invalid flight scope")
        if self.target_policy == "fixed_entity" and not self.entity_id:
            raise ValueError("Fixed identity is missing")
        if self.room_allocation is not None and (len(self.room_allocation) != self.rooms
                or sum(room.adults for room in self.room_allocation) != self.adults
                or sorted(age for room in self.room_allocation for age in room.children_ages) != sorted(self.children_ages)):
            raise ValueError("Invalid allocation")
        return self


class FlightConstraints(Value):
    arrival_date: date | None = None
    cabin: Literal["economy", "premium_economy", "business", "first"] = "economy"
    direct_required: StrictBool = False
    prefer_direct: StrictBool = False
    max_flight_minutes: int | None = Field(default=None, ge=1, le=10080)

    @model_serializer(mode="wrap")
    def legacy_optional_arrival(self, handler):
        data = handler(self)
        if self.arrival_date is None:
            data.pop("arrival_date", None)
        return data


def _hash(values: dict) -> str:
    values = dict(values)
    if values.get("children_ages") is not None:
        values["children_ages"] = sorted(values["children_ages"])
    for name in ("fare_constraints", "inclusions", "exclusions", "unverified_fields", "assumptions"):
        if values.get(name) is not None:
            values[name] = sorted(set(values[name]))
    if values.get("room_allocation") is not None:
        values["room_allocation"] = sorted(
            ({"adults":room["adults"], "children_ages":sorted(room["children_ages"])} for room in values["room_allocation"]),
            key=lambda room: (room["adults"], room["children_ages"]))
    return hashlib.sha256(json.dumps(values, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def context_hash(context: FlightRequestContext) -> str:
    return _hash(context.model_dump(mode="json"))


class FlightSearchRequest(Value):
    version: Literal["flight-search.v1"] = VERSION
    request_id: str = Field(min_length=1, max_length=100)
    deadline_at: AwareDatetime
    request_context_hash: Digest
    request_context: FlightRequestContext
    constraints: FlightConstraints = Field(default_factory=FlightConstraints)
    target_itinerary_id: Digest | None = None

    _deadline_utc = field_validator("deadline_at")(_utc)

    @model_validator(mode="after")
    def matching_context(self):
        if context_hash(self.request_context) != self.request_context_hash:
            raise ValueError("Flight context hash mismatch")
        if self.request_context.target_policy == "fixed_entity" and (
                not self.target_itinerary_id or self.request_context.entity_id != "google-flight:"+self.target_itinerary_id):
            raise ValueError("Fixed flight target mismatch")
        return self


def search_hash(request: FlightSearchRequest) -> str:
    return _hash({"version":request.version, "context":request.request_context.model_dump(mode="json"),
        "constraints":request.constraints.model_dump(mode="json"), "target":request.target_itinerary_id,
        "provider_policy":"google-flights-paired-return-v1"})


class ObservedConnection(Value):
    airport_name: ShortText
    city_name: ShortText
    duration_minutes: int = Field(ge=1, le=4320)


class ObservedLeg(Value):
    origin: Airport
    destination: Airport
    origin_name: ShortText
    destination_name: ShortText
    departure_date: date
    arrival_date: date
    departure_time: str = Field(pattern=r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]$")
    arrival_time: str = Field(pattern=r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]$")
    duration_minutes: int = Field(ge=1, le=4320)
    stops: int = Field(ge=0, le=3)
    carriers: tuple[ShortText, ...] = Field(min_length=1, max_length=4)
    operated_by: tuple[ShortText, ...] = Field(default=(), max_length=4)
    observed_connections: tuple[ObservedConnection, ...] = Field(default=(), max_length=3)
    timezone_verified: Literal[False] = False
    connections_verified: Literal[False] = False

    @model_validator(mode="after")
    def valid_leg(self):
        if (self.origin == self.destination or not 0 <= (self.arrival_date - self.departure_date).days <= 3
                or len(self.observed_connections) != self.stops):
            raise ValueError("Invalid observed leg")
        return self


class PriceReadback(Value):
    kind: Literal["lower_bound"] = "lower_bound"
    amount: str = Field(min_length=1, max_length=20)
    currency: Literal["EUR"] = "EUR"
    unit: Literal["group_trip"] = "group_trip"
    adults: int = Field(ge=1, le=2)
    required_taxes_included: Literal[True] = True
    fees_complete: Literal[False] = False
    baggage_verified: Literal[False] = False
    final_supplier_offer: Literal[False] = False
    availability_verified: Literal[False] = False

    @field_validator("amount")
    @classmethod
    def valid_amount(cls, value):
        if not re.fullmatch(r"[0-9]+(?:\.[0-9]{1,2})?", value):
            raise ValueError("Invalid source amount")
        try:
            if not Decimal(0) < Decimal(value) < Decimal("1000000"):
                raise ValueError("Invalid source amount")
        except InvalidOperation:
            raise ValueError("Invalid source amount") from None
        return value


def itinerary_id(outbound: ObservedLeg, inbound: ObservedLeg, *, provider_id="google_flights", cabin="economy") -> str:
    # No amount, ranking, position or URL participates in route identity.
    return _hash({"provider":provider_id, "cabin":cabin, "outbound":outbound.model_dump(mode="json"),
                  "inbound":inbound.model_dump(mode="json")})


class SelectedRoute(Value):
    provider_id: ProviderID = "google_flights"
    itinerary_id: Digest
    origin: Airport
    destination: Airport
    date_from: date
    date_to: date
    adults: int = Field(ge=1, le=2)
    children_ages: tuple[int, ...] = Field(default=(), max_length=0)
    currency: Literal["EUR"] = "EUR"
    cabin: Literal["economy"] = "economy"
    outbound: ObservedLeg
    inbound: ObservedLeg
    price: PriceReadback
    source_url: str = Field(min_length=1, max_length=6000)
    observed_at: AwareDatetime
    retrieved_at: AwareDatetime
    expires_at: AwareDatetime
    context_verified: Literal[False] = False
    availability_verified: Literal[False] = False

    _times_utc = field_validator("observed_at", "retrieved_at", "expires_at")(_utc)

    @model_validator(mode="after")
    def independent_route(self):
        parsed = urlsplit(self.source_url)
        if (self.provider_id != "google_flights" or parsed.scheme != "https" or parsed.hostname != "www.google.com"
                or parsed.port not in (None, 443) or parsed.username or parsed.password or parsed.fragment
                or parsed.path not in ("/travel/flights", "/travel/flights/search")):
            raise ValueError("Invalid route source")
        if ((self.outbound.origin, self.outbound.destination, self.inbound.origin, self.inbound.destination)
                != (self.origin, self.destination, self.destination, self.origin)
                or self.outbound.departure_date != self.date_from or self.inbound.departure_date != self.date_to
                or self.price.adults != self.adults
                or self.itinerary_id != itinerary_id(self.outbound, self.inbound, provider_id=self.provider_id, cabin=self.cabin)
                or not self.observed_at <= self.retrieved_at < self.expires_at
                or self.expires_at > self.observed_at + timedelta(minutes=10)):
            raise ValueError("Independent route scope mismatch")
        return self


class FlightDiagnostic(Value):
    provider_id: ProviderID = "google_flights"
    outcome: Outcome
    diagnostic_code: DiagnosticCode
    dispatched: StrictBool = False
    external_dispatched: StrictBool = False
    checked_at: AwareDatetime
    retrieved_at: AwareDatetime | None = None

    _times_utc = field_validator("checked_at", "retrieved_at")(lambda value: _utc(value) if value is not None else None)


class FlightSearchResponse(Value):
    version: Literal["flight-search.v1"] = VERSION
    request_id: str = Field(min_length=1, max_length=100)
    request_context_hash: Digest
    outcome: Outcome
    diagnostic_code: DiagnosticCode
    route: SelectedRoute | None = None
    diagnostics: tuple[FlightDiagnostic, ...] = Field(default=(), max_length=4)
    retrieved_at: AwareDatetime | None = None
    candidate_count: int = Field(default=0, ge=0, le=36)

    _time_utc = field_validator("retrieved_at")(lambda value: _utc(value) if value is not None else None)

    @model_validator(mode="after")
    def no_price_on_failure(self):
        if (self.outcome == "reference") != (self.route is not None):
            raise ValueError("Only a successful reference can carry a route")
        return self
