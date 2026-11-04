"""Grounded min/max ranges, never arbitrary model-generated padding."""
from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from decimal import Decimal

from .models import EstimateBasis, PriceObservation, ProviderPolicy, QuoteScope, RangePrice, RequestContext, utc_now
from .normalization import compatible_scope, quantize_money, request_context_hash


def estimate_from_observations(observations: Sequence[PriceObservation], context: RequestContext,
                               policy: ProviderPolicy, *, now: datetime | None = None,
                               entity_level: str | None = None) -> PriceObservation | None:
    clock = now or utc_now()
    if not policy.enabled or not policy.retention_allowed or not policy.historical_estimate_allowed or policy.retention_seconds == 0:
        return None
    maximum_age = min(policy.retention_seconds, 7 * 86400)
    groups: dict[tuple, list[PriceObservation]] = {}
    seen_ids: set[str] = set()
    seen_upstream: set[tuple] = set()
    for observation in sorted(observations, key=lambda item: (item.observed_at or clock, item.id)):
        if observation.provider_id != policy.id or observation.outcome != "quoted" or observation.basis is not None:
            continue
        if observation.normalized_group is None or observation.normalized_group.kind != "point" or observation.observed_at is None:
            continue
        if not 0 <= (clock - observation.observed_at).total_seconds() <= maximum_age:
            continue
        if not compatible_scope(context, observation.scope):
            continue
        scope = observation.scope
        level = entity_level or scope.entity_level
        if level not in ("property", "route", "destination_lodging"):
            raise ValueError("Unsupported estimate entity level")
        if level == "destination_lodging" and context.component != "stay":
            continue
        # A property estimate is tied to that identity; destination comparables
        # explicitly permit different properties, retaining their IDs as basis.
        comparison = (scope.component, scope.destination, scope.origin, scope.date_from, scope.date_to, scope.adults,
                      tuple(sorted(scope.children_ages)), scope.rooms, scope.room_allocation, scope.trip_type,
                      scope.currency, scope.unit, scope.fare_class, scope.rate_id, scope.taxes, scope.mandatory_fees,
                      tuple(sorted(scope.inclusions)), tuple(sorted(scope.exclusions)), scope.entity_id if level != "destination_lodging" else None,
                      tuple(sorted(observation.unknown_charges)), tuple(sorted(observation.excluded_charges)), observation.component_complete)
        if observation.upstream_id:
            upstream_key = ("id", observation.upstream_id)
        elif observation.request_key:
            upstream_key = ("request", observation.provider_id, observation.request_key, observation.observed_at)
        else:
            upstream_key = ("source", observation.source_url, observation.observed_at)
        scoped_upstream_key = (comparison, upstream_key)
        if observation.id in seen_ids or scoped_upstream_key in seen_upstream:
            continue
        seen_ids.add(observation.id)
        seen_upstream.add(scoped_upstream_key)
        groups.setdefault(comparison, []).append(observation)
    # Pick the largest mutually comparable group, independently of input order.
    # A first incompatible row must not hide two subsequent valid observations.
    eligible = min(groups.items(), key=lambda entry: (-len(entry[1]), repr(entry[0])))[1] if groups else []
    if len(eligible) < 2:
        return None
    eligible.sort(key=lambda item: (item.observed_at, item.id))
    amounts = [Decimal(item.normalized_group.amount) for item in eligible]
    first = eligible[0]
    level = entity_level or first.scope.entity_level
    changed_entities = len({item.scope.entity_id for item in eligible}) > 1
    differences = ("Разные объекты проживания в одном направлении; диапазон не является ценой выбранного отеля",) if changed_entities else ()
    basis = EstimateBasis(basis_ids=tuple(item.id for item in eligible), sample_count=len(eligible), observed_from=eligible[0].observed_at,
                          observed_to=eligible[-1].observed_at, entity_level=level, differences=differences,
                          assumptions=("Диапазон равен минимуму и максимуму совместимых наблюдений; наличие не подтверждено",))
    value = RangePrice(min=quantize_money(min(amounts), context.currency), max=quantize_money(max(amounts), context.currency), currency=context.currency)
    scope_data = first.scope.model_dump()
    scope_data["entity_level"] = level
    if level == "destination_lodging":
        scope_data.update(entity_id=None, provider_entity_id=None)
    remaining_retention = maximum_age - (clock - eligible[0].observed_at).total_seconds()
    if remaining_retention <= 0:
        return None
    return PriceObservation(provider_id="grounded-minmax", request_context_hash=request_context_hash(context), scope=QuoteScope(**scope_data), value=value,
                            normalized_group=value, source_url=first.source_url, observed_at=eligible[-1].observed_at, retrieved_at=clock,
                            expires_at=clock + timedelta(seconds=min(policy.ttl_seconds, remaining_retention)), outcome="quoted", basis=basis,
                            provider_policy_id=policy.id, unit_label="Обоснованный ориентир за всю группу и весь период/маршрут",
                            reason="Диапазон по совместимым наблюдениям, не текущая цена или подтверждение наличия",
                            component_complete=all(item.component_complete for item in eligible), unknown_charges=first.unknown_charges,
                            excluded_charges=first.excluded_charges, included_charges=first.included_charges, availability_verified=False)
