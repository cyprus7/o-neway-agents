"""Versioned travel-data boundary, independent of HTTP, providers and persistence."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import hashlib
import ipaddress
import json
from typing import Any, Generic, Literal, Protocol, TypeVar
from urllib.parse import urlsplit
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator, model_serializer

from ..geography import Place
from ..travel_dates import flight_departure_date
from ..models import Evidence, TravelComponent, TravelIntent
from ..pricing.models import RequestContext, RoomAllocation
from ..regional_events.domain import EventResult


VERSION = "travel-data.v1"
MAX_REQUEST_BYTES = 256 * 1024
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
Operation = Literal["resolve_place", "weather", "search", "inventory", "inventory_candidates", "refresh_inventory", "price_documents", "regional_events", "capabilities"]
DEADLINES: dict[str, float] = {"resolve_place": 8, "weather": 18, "search": 22, "inventory": 45,
                              "inventory_candidates": 45, "refresh_inventory": 45, "price_documents": 22, "regional_events": 12, "capabilities": 2}
PATHS = {"resolve_place": "/v1/places/resolve", "weather": "/v1/weather", "search": "/v1/search",
         "inventory": "/v1/inventory", "inventory_candidates": "/v1/inventory/candidates", "refresh_inventory": "/v1/inventory/refresh",
         "price_documents": "/v1/pricing/documents", "regional_events": "/v1/events", "capabilities": "/v1/capabilities"}


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class SourceCacheProtocol(Protocol):
    def cache_get(self, key: str, allow_stale: bool = False) -> tuple[Any, float, bool] | None: ...
    def cache_put(self, key: str, value: Any, ttl: int) -> None: ...
    def increment(self, name: str, amount: float = 1) -> None: ...


class ContractEvidence(Evidence):
    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="before")
    @classmethod
    def original_evidence(cls, value):
        return value.model_dump() if isinstance(value, Evidence) else value


class ContractComponent(TravelComponent):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    evidence: list[ContractEvidence] = Field(default_factory=list, max_length=24)

    @model_validator(mode="before")
    @classmethod
    def original_component(cls, value):
        return value.model_dump() if isinstance(value, TravelComponent) else value


class ComponentBundle(StrictModel):
    candidate_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    flight: ContractComponent
    stay: ContractComponent
    transfer: ContractComponent

    @model_serializer(mode="wrap")
    def serialize_bundle(self, handler):
        data = handler(self)
        if self.candidate_id is None:
            data.pop("candidate_id", None)
        return data

    def __iter__(self):
        return iter((self.flight, self.stay, self.transfer))

    def __getitem__(self, index):
        return (self.flight, self.stay, self.transfer)[index]

    @classmethod
    def from_components(cls, value) -> "ComponentBundle":
        if isinstance(value, cls):
            return value.model_copy(deep=True)
        return cls(flight=value[0], stay=value[1], transfer=value[2]).model_copy(deep=True)


class PlaceRef(StrictModel):
    name: str = Field(min_length=1, max_length=200)
    country: str = Field(max_length=200)
    country_code: str = Field(pattern=r"^[A-Z]{2}$")
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    airport: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    scope: Literal["locality", "country"] = "locality"
    tags: tuple[str, ...] = Field(default=(), max_length=24)
    aliases: tuple[str, ...] = Field(default=(), max_length=24)

    @field_validator("tags", "aliases")
    @classmethod
    def bounded_names(cls, values):
        if any(len(value) > 200 for value in values):
            raise ValueError("place_text_too_long")
        return values

    @classmethod
    def from_place(cls, place: Place):
        return cls(**{name: getattr(place, name) for name in cls.model_fields if hasattr(place, name)})

    def to_place(self) -> Place:
        return Place(**self.model_dump(), wiki="", subtitle="")


class PlaceData(PlaceRef):
    wiki: str = Field(max_length=4000)
    subtitle: str = Field(max_length=1500)
    source_provider: str = Field(max_length=200)
    retrieved_at: str = Field(max_length=60)
    freshness: Literal["snapshot", "live", "cached", "unavailable"]

    def to_place(self) -> Place:
        return Place(**self.model_dump())


class DataIntent(StrictModel):
    origin: str | None = Field(default=None, max_length=160)
    destination: str | None = Field(default=None, max_length=160)
    date_from: date | None = None
    date_to: date | None = None
    flight_departure_from: date | None = None
    flight_departure_to: date | None = None
    adults: int = Field(default=2, ge=1, le=9)
    children_ages: list[int] = Field(default_factory=list, max_length=8)
    rooms: int = Field(default=1, ge=1, le=9)
    room_allocation: list[RoomAllocation] | None = None
    pets: list[str] = Field(default_factory=list, max_length=5)
    currency: str = Field(default="EUR", pattern=r"^[A-Z]{3}$")
    max_travel_minutes: int | None = Field(default=None, ge=1, le=10080)
    direct_flight_preferred: bool = False
    trip_styles: list[str] = Field(default_factory=list, max_length=12)
    hotel_requirements: list[str] = Field(default_factory=list, max_length=20)
    hard_constraints: list[str] = Field(default_factory=list, max_length=20)
    soft_constraints: list[str] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def valid_intent(self):
        TravelIntent.model_validate(self.model_dump())
        if any(len(item) > 500 for key in ("pets", "trip_styles", "hotel_requirements", "hard_constraints", "soft_constraints") for item in getattr(self, key)):
            raise ValueError("intent_text_too_long")
        return self

    @classmethod
    def from_intent(cls, intent: TravelIntent):
        return cls.model_validate(intent.model_dump(include=set(cls.model_fields)))

    def to_intent(self) -> TravelIntent:
        return TravelIntent.model_validate(self.model_dump())


class EmptyInput(StrictModel):
    pass


class ResolveInput(StrictModel):
    name: str = Field(min_length=1, max_length=160)


class DestinationInput(StrictModel):
    place: PlaceRef
    intent: DataIntent


class InventoryInput(DestinationInput):
    origin: PlaceRef

    @model_validator(mode="after")
    def dated(self):
        if not self.intent.date_from or not self.intent.date_to:
            raise ValueError("inventory_dates_required")
        return self


class InventoryCandidatesInput(InventoryInput):
    limit: int = Field(default=3, ge=1, le=9, strict=True)


class InventoryCandidatesData(StrictModel):
    candidates: list[ComponentBundle] = Field(default_factory=list, max_length=9)


class RefreshInput(InventoryInput):
    current: ComponentBundle


class ResolveData(StrictModel):
    place: PlaceData | None


class WeatherObservation(StrictModel):
    kind: Literal["unknown", "forecast", "historical"]
    status: Literal["unavailable", "live", "cached"]
    summary: str = Field(max_length=4000)
    evidence: list[ContractEvidence] = Field(default_factory=list, max_length=8)
    temperature_max: float | None = None
    temperature_min: float | None = None
    temperature_unit: Literal["°C"] | None = None
    precipitation_total_mm: float | None = Field(default=None, ge=0)
    period_from: date | None = None
    period_to: date | None = None
    requested_from: date | None = None
    requested_to: date | None = None


class SearchData(StrictModel):
    evidence: list[ContractEvidence] = Field(max_length=24)


def public_document_url(value: str) -> str:
    """Only public HTTPS evidence targets; never local/service/file commands."""
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").rstrip(".").lower()
        if (parsed.scheme != "https" or not host or parsed.username or parsed.password
                or parsed.port not in (None, 443) or parsed.fragment
                or any(ord(char) < 33 for char in value)
                or "." not in host or host.endswith((".localhost", ".local", ".internal", ".home", ".lan"))):
            raise ValueError("invalid_public_document_url")
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
        # A literal address is never a registered travel-source identity.
        if address is not None:
            raise ValueError("invalid_public_document_url")
    except (ValueError, TypeError):
        raise ValueError("invalid_public_document_url") from None
    return value


class PriceDocument(StrictModel):
    component: Literal["flight", "stay", "transfer"]
    source_url: str = Field(min_length=1, max_length=3000)
    title: str = Field(max_length=500)
    content: str = Field(min_length=1, max_length=14000)
    retrieved_at: datetime
    context_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    delivery: Literal["live_fetch", "cache_reuse"] = "live_fetch"

    @field_validator("source_url")
    @classmethod
    def public_source(cls, value):
        return public_document_url(value)

    @field_validator("retrieved_at")
    @classmethod
    def aware_retrieval(cls, value):
        if value.tzinfo is None:
            raise ValueError("document_timezone_required")
        if value > datetime.now(timezone.utc) + timedelta(minutes=5):
            raise ValueError("document_time_in_future")
        return value.astimezone(timezone.utc)


class PriceDocumentsData(StrictModel):
    documents: list[PriceDocument] = Field(default_factory=list, max_length=9)
    outcome: Literal["documents", "no_documents", "disabled", "unavailable", "timeout"] = "no_documents"
    reason: Literal["source_documents", "no_matching_pages", "external_fallback_disabled", "missing_fetch_key",
                    "source_unavailable", "deadline_exceeded", "gateway_unavailable", "invalid_response", "circuit_open", "overloaded"] | None = None


class GatewayCapabilities(StrictModel):
    inventory_candidates: bool = False
    external_data_fallback_enabled: bool
    providers: dict[str, dict[str, Any]]
    pricing: dict[str, Any]
    gateway: dict[str, Any] = Field(default_factory=dict)


T = TypeVar("T")


class GatewayRequest(StrictModel, Generic[T]):
    version: Literal["travel-data.v1"] = VERSION
    operation: Operation
    request_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    deadline_at: datetime
    data: T

    @field_validator("deadline_at")
    @classmethod
    def aware(cls, value):
        if value.tzinfo is None:
            raise ValueError("deadline_timezone_required")
        return value.astimezone(timezone.utc)


class GatewayResponse(StrictModel, Generic[T]):
    version: Literal["travel-data.v1"] = VERSION
    operation: Operation
    request_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    status: Literal["ok", "unavailable"]
    reason: Literal["deadline_exceeded", "gateway_unavailable", "invalid_response", "circuit_open", "overloaded"] | None = None
    data: T | None = None

    @model_validator(mode="after")
    def complete(self):
        if self.status == "ok" and self.data is None:
            raise ValueError("response_data_required")
        if self.status == "ok" and self.reason is not None:
            raise ValueError("successful_response_cannot_have_failure")
        if self.status == "unavailable" and self.reason is None:
            raise ValueError("response_reason_required")
        if self.status == "unavailable" and self.data is not None:
            raise ValueError("unavailable_response_cannot_have_data")
        return self


INPUTS = {"resolve_place": ResolveInput, "weather": DestinationInput, "search": DestinationInput,
          "inventory": InventoryInput, "inventory_candidates": InventoryCandidatesInput, "refresh_inventory": RefreshInput, "price_documents": RefreshInput, "regional_events": DestinationInput,
          "capabilities": EmptyInput}
OUTPUTS = {"resolve_place": ResolveData, "weather": WeatherObservation, "search": SearchData,
           "inventory": ComponentBundle, "inventory_candidates": InventoryCandidatesData, "refresh_inventory": ComponentBundle, "price_documents": PriceDocumentsData, "regional_events": EventResult,
           "capabilities": GatewayCapabilities}


def fingerprint(operation: str, data: BaseModel) -> str:
    payload = json.dumps({"operation": operation, "data": data.model_dump(mode="json")},
                         ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(payload.encode()).hexdigest()


def make_request(operation: Operation, data: BaseModel, *, timeout: float | None = None) -> GatewayRequest:
    return GatewayRequest[INPUTS[operation]](operation=operation, request_id=uuid4().hex,
        fingerprint=fingerprint(operation, data), deadline_at=datetime.now(timezone.utc) + timedelta(seconds=timeout or DEADLINES[operation]), data=data)


def transport_mode(intent: TravelIntent) -> str:
    for value in intent.hard_constraints + intent.soft_constraints:
        if value.startswith("transport:"):
            mode = value.split(":", 1)[1]
            return mode if mode in ("flight", "train", "bus", "car") else "unsupported"
    return "flight"


def context_for(component, place, origin, intent, *, entity_id=None, departure_date=None) -> RequestContext:
    return RequestContext(component=component, origin=(origin.airport or origin.name) if component == "flight" else None,
        destination=place.airport or place.name, destination_city=place.name if component == "stay" else None,
        date_from=flight_departure_date(intent, departure_date) if component == "flight" else intent.date_from,
        date_to=intent.date_to, adults=intent.adults, children_ages=tuple(intent.children_ages),
        rooms=intent.rooms, room_allocation=intent.room_allocation, currency=intent.currency,
        trip_type="roundtrip" if component == "flight" else "not_applicable",
        fare_constraints=tuple(value for value in intent.hard_constraints if value.startswith("transport")) if component == "flight" else (),
        target_policy="fixed_entity" if entity_id else "candidate_allowed", entity_id=entity_id)


class TravelDataPort(Protocol):
    async def capabilities(self) -> dict[str, Any]: ...
    async def resolve_place(self, name: str) -> Place | None: ...
    async def weather(self, place: Place, intent: TravelIntent) -> dict[str, Any]: ...
    async def search(self, place: Place, intent: TravelIntent) -> list[Evidence]: ...
    async def inventory(self, place: Place, origin: Place, intent: TravelIntent) -> ComponentBundle: ...
    async def inventory_candidates(self, place: Place, origin: Place, intent: TravelIntent, *, limit: int = 3) -> list[ComponentBundle]: ...
    async def regional_events(self, place: Place, intent: TravelIntent) -> EventResult: ...
    async def refresh_inventory(self, place: Place, origin: Place, intent: TravelIntent, current: ComponentBundle) -> ComponentBundle: ...
    async def price_documents(self, place: Place, origin: Place, intent: TravelIntent, current: ComponentBundle) -> PriceDocumentsData: ...


def candidate_identity(bundle: ComponentBundle, place: Place, origin: Place, intent: TravelIntent) -> str:
    """Stable request/source identity; observation prices and load times are excluded."""
    scope = InventoryInput(place=PlaceRef.from_place(place), origin=PlaceRef.from_place(origin), intent=DataIntent.from_intent(intent))
    identities = [(part.details.get("pricing_entity_id") or part.details.get("catalog_property_id"), part.source_url, part.title) for part in bundle]
    return hashlib.sha256(json.dumps([scope.model_dump(mode="json"), identities], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
