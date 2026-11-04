"""Private browser request contract, independently testable without a browser runtime."""
from datetime import date
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator


class Context(BaseModel):
    model_config = ConfigDict(extra="forbid")
    destination: str = Field(min_length=2, max_length=160)
    destination_iata: str = Field(pattern=r"^[A-Z]{3}$")
    date_from: date
    date_to: date
    adults: int = Field(ge=1, le=9)
    children_ages: list[int] = Field(default_factory=list, max_length=8)
    rooms: int = Field(default=1, ge=1, le=9)
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    target_entity_id: str | None = Field(default=None, pattern=r"^(?:google-hotel|ostrovok):[a-f0-9]{24}$")
    target_property_path: str | None = Field(default=None, min_length=1, max_length=256)
    discovery_terms: list[Annotated[str, Field(min_length=1, max_length=80)]] = Field(default_factory=list, max_length=4)

    @model_validator(mode="after")
    def valid_trip(self):
        if not 1 <= (self.date_to - self.date_from).days <= 90 or any(age < 0 or age > 17 for age in self.children_ages):
            raise ValueError("Invalid dates or child ages")
        from .recipes.lodging import valid_discovery_terms
        from .recipes.ostrovok import target_property_diagnostic
        if not valid_discovery_terms(self.discovery_terms):
            raise ValueError("Invalid lodging discovery terms")
        if target_property_diagnostic(self.model_dump(mode="json")) is not None:
            raise ValueError("Invalid canonical property target")
        return self


class QuoteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: str = Field(min_length=1, max_length=100)
    provider_id: Literal["google_hotels", "ostrovok"]
    request_context: Context
    request_context_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    deadline_at: AwareDatetime

    @model_validator(mode="after")
    def matching_provider(self):
        target = self.request_context.target_entity_id
        prefix = "google-hotel:" if self.provider_id == "google_hotels" else "ostrovok:"
        if target is not None and not target.startswith(prefix):
            raise ValueError("Target property belongs to another provider")
        if self.request_context.target_property_path is not None and self.provider_id != "ostrovok":
            raise ValueError("Canonical property paths are supported only for Ostrovok")
        return self
