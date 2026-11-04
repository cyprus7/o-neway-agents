"""Explicitly consented stable memory; it never infers preferences from text."""
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ..models import TravelIntent

MEMORY_FIELDS = frozenset({"origin", "currency", "climate_preferences", "trip_styles", "direct_flight_preferred", "max_travel_minutes", "pets"})


class MemoryInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: Any
    source_trip_id: str = Field(min_length=1, max_length=160)


def validate_memory(field: str, value):
    if field not in MEMORY_FIELDS:
        raise ValueError("unsupported_memory_field")
    if field in {"origin", "currency"}:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("invalid_memory_value")
        value = value.strip()
    elif field in {"climate_preferences", "trip_styles", "pets"}:
        if (not isinstance(value, list) or any(not isinstance(v, str) or not v.strip() or len(v) > 500 for v in value)):
            raise ValueError("invalid_memory_value")
    elif field == "direct_flight_preferred" and type(value) is not bool:
        raise ValueError("invalid_memory_value")
    elif field == "max_travel_minutes" and (type(value) is not int or value <= 0):
        raise ValueError("invalid_memory_value")
    return getattr(TravelIntent.model_validate({field: value}), field)


def fill_memory(session, items) -> list[str]:
    # Only brand-new trips can receive approved defaults. Replanning and saved
    # trips are never silently rewritten. Explicit false/EUR/[] also win.
    if session.revision != 0 or session.messages or session.options:
        return []
    explicit = session.intent.model_fields_set
    applied = []
    constrained = bool(session.intent.hard_constraints or session.intent.soft_constraints)
    for item in items:
        field = item["field"]
        if field in explicit:
            continue
        current = getattr(session.intent, field)
        default = getattr(TravelIntent(), field)
        if current != default:
            continue
        # Free-form constraints can include a negation. Do not ask inference to
        # reinterpret them merely to apply optional stored preferences.
        if constrained:
            continue
        setattr(session.intent, field, item["value"])
        applied.append(field)
    session.preferences["applied_memory_fields"] = applied
    return applied
