"""Deterministic scope checks; do not infer units, party pricing or exchange rates."""
from __future__ import annotations

import hashlib
import json
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from .models import (
    LowerBoundPrice, PointPrice, PriceObservation, PriceValue, QuoteScope, RangePrice,
    RequestContext, UnknownPrice, decimal_string,
)


# Supported ISO currencies have explicit minor-unit semantics. Unknown codes are
# rejected rather than silently assuming cents (for example JPY vs KWD).
_ZERO_MINOR = frozenset("BIF CLP DJF GNF ISK JPY KMF KRW PYG RWF UGX VND VUV XAF XOF XPF".split())
_THREE_MINOR = frozenset("BHD IQD JOD KWD LYD OMR TND".split())
_TWO_MINOR = frozenset("AED AFN ALL AMD ANG AOA ARS AUD AWG AZN BAM BBD BDT BGN BMD BND BOB BRL BSD BTN BWP BYN BZD CAD CDF CHF CNY COP CRC CUP CVE CZK DKK DOP DZD EGP ERN ETB EUR FJD FKP GBP GEL GHS GIP GMD GTQ GYD HKD HNL HTG HUF IDR ILS INR IRR ISK JMD KES KGS KHR KYD KZT LAK LBP LKR LRD LSL MAD MDL MGA MKD MMK MNT MOP MRU MUR MVR MWK MXN MYR MZN NAD NGN NIO NOK NPR NZD PAB PEN PGK PHP PKR PLN QAR RON RSD RUB SAR SBD SCR SDG SEK SGD SHP SLE SOS SRD SSP STN SVC SYP SZL THB TJS TMT TOP TRY TTD TWD TZS UAH USD UYU UZS WST XCD YER ZAR ZMW ZWG".split())


def quantize_money(amount: object, currency: str) -> str:
    value = Decimal(decimal_string(amount))
    if currency in _ZERO_MINOR:
        places = 0
    elif currency in _THREE_MINOR:
        places = 3
    elif currency in _TWO_MINOR:
        places = 2
    else:
        raise ValueError("Unsupported currency minor unit")
    unit = Decimal(1).scaleb(-places)
    return format(value.quantize(unit, rounding=ROUND_HALF_UP), f".{places}f")


def normalized_value(value: PriceValue) -> PriceValue:
    if value.kind == "point":
        return PointPrice(amount=quantize_money(value.amount, value.currency), currency=value.currency)
    if value.kind == "closed_range":
        return RangePrice(min=quantize_money(value.min, value.currency), max=quantize_money(value.max, value.currency), currency=value.currency)
    if value.kind == "lower_bound":
        return LowerBoundPrice(min=quantize_money(value.min, value.currency), currency=value.currency)
    return value


def _canonical(data: dict[str, Any]) -> dict[str, Any]:
    values = dict(data)
    if values.get("children_ages") is not None:
        values["children_ages"] = sorted(values["children_ages"])
    for name in ("fare_constraints", "inclusions", "exclusions", "unverified_fields", "assumptions"):
        if values.get(name) is not None:
            values[name] = sorted(set(values[name]))
    if values.get("room_allocation") is not None:
        allocation = []
        for room in values["room_allocation"]:
            allocation.append({"adults": room["adults"], "children_ages": sorted(room["children_ages"])})
        values["room_allocation"] = sorted(allocation, key=lambda room: (room["adults"], room["children_ages"]))
    return values


def _hash(data: dict[str, Any]) -> str:
    encoded = json.dumps(_canonical(data), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode()).hexdigest()


def request_context_hash(context: RequestContext) -> str:
    return _hash(context.model_dump(mode="json"))


def offer_scope_hash(scope: QuoteScope) -> str:
    return _hash(scope.model_dump(mode="json"))


def _same_place(first: str | None, second: str | None) -> bool:
    return first is not None and second is not None and first.strip().casefold() == second.strip().casefold()


def compatibility_errors(context: RequestContext, scope: QuoteScope | None) -> tuple[str, ...]:
    if scope is None:
        return ("missing_scope",)
    problems: list[str] = []
    if not scope.context_verified or scope.unverified_fields:
        problems.append("context_not_verified")
    for name in ("component", "date_from", "date_to", "adults", "trip_type", "currency"):
        if getattr(context, name) != getattr(scope, name):
            problems.append(name + "_mismatch")
    if not _same_place(context.destination, scope.destination):
        problems.append("destination_mismatch")
    if context.component in ("flight", "rail", "bus", "car") and not _same_place(context.origin, scope.origin):
        problems.append("origin_mismatch")
    if scope.children_ages is None or sorted(context.children_ages) != sorted(scope.children_ages):
        problems.append("children_ages_mismatch")
    if context.component == "stay":
        if context.destination_city is not None and not _same_place(context.destination_city, scope.destination_city):
            problems.append("destination_city_mismatch")
        if context.rooms != scope.rooms:
            problems.append("rooms_mismatch")
        if context.room_allocation is not None:
            expected = _canonical(context.model_dump(mode="json"))["room_allocation"]
            observed = _canonical(scope.model_dump(mode="json"))["room_allocation"]
            if expected != observed:
                problems.append("room_allocation_mismatch")
    if context.target_policy == "fixed_entity" and context.entity_id != scope.entity_id:
        problems.append("entity_mismatch")
    if not set(context.fare_constraints).issubset(scope.fare_constraints):
        problems.append("fare_constraints_unverified")
    return tuple(problems)


def compatible_scope(context: RequestContext, scope: QuoteScope | None) -> bool:
    return not compatibility_errors(context, scope)


def normalize_observation(observation: PriceObservation, context: RequestContext) -> PriceObservation:
    """Only proven whole-group/trip or whole-group/stay amounts are normalized.

    A published per-person/from/nightly amount remains visible as ``value``. No
    guessed multiplicity may make it a requested-party total or budget proof.
    """
    data = observation.model_dump()
    data["normalized_group"] = None
    context_hash = request_context_hash(context)
    source_hash = observation.request_context_hash
    data["request_context_hash"] = source_hash or context_hash
    if observation.value.kind == "unknown":
        return PriceObservation(**data)
    if observation.value.currency != context.currency:
        data.update(outcome="currency_mismatch", diagnostic_code="currency_mismatch", reason="Валюта источника отличается от валюты поездки; пересчет не выполнялся")
        return PriceObservation(**data)
    problems = compatibility_errors(context, observation.scope)
    if source_hash is not None and source_hash != context_hash:
        problems += ("request_context_hash_mismatch",)
    allowed_units = ("group_stay",) if context.component == "stay" else ("group_trip",)
    if observation.scope is None or observation.scope.unit not in allowed_units:
        problems += ("unit_not_group_total",)
    if problems or observation.expires_at is None:
        # Reference is deliberately a distinct displayable result, not a failed
        # quote whose digits may accidentally enter a subtotal.
        reference = observation.outcome == "reference"
        data.update(outcome="reference" if reference else "context_mismatch", diagnostic_code=problems[0] if problems else "missing_expiry",
                    reason=observation.reason if reference and observation.reason else "Цена источника не подтверждена для дат, группы или единицы текущей поездки")
        return PriceObservation(**data)
    try:
        value = normalized_value(observation.value)
    except ValueError:
        data.update(outcome="currency_mismatch", diagnostic_code="currency_minor_unit_unknown", reason="Правила округления валюты источника не поддерживаются")
        return PriceObservation(**data)
    data.update(outcome="quoted", normalized_group=value, normalization_note="Источник явно указал стоимость всей группы за весь запрошенный период/маршрут", diagnostic_code=None)
    return PriceObservation(**data)
