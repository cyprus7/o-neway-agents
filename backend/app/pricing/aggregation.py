"""Backend-owned comparable subtotals with explicit missing costs."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from collections.abc import Mapping, Sequence

from .models import (
    LowerBoundPrice, PointPrice, PriceObservation, PriceValue, PricingSummary,
    RangePrice, UnknownPrice, decimal_string, utc_now,
)
from .normalization import quantize_money


def sum_price_values(values: Sequence[PriceValue]) -> PriceValue:
    if not values or any(value.kind == "unknown" for value in values):
        return UnknownPrice(reason="unknown_component")
    currencies = {value.currency for value in values}
    if len(currencies) != 1:
        return UnknownPrice(reason="currency_mismatch: no implicit conversion")
    currency = next(iter(currencies))
    low = sum((Decimal(value.amount if value.kind == "point" else value.min) for value in values), Decimal(0))
    if any(value.kind == "lower_bound" for value in values):
        return LowerBoundPrice(min=quantize_money(low, currency), currency=currency)
    high = sum((Decimal(value.amount if value.kind == "point" else value.max) for value in values), Decimal(0))
    if any(value.kind == "closed_range" for value in values):
        return RangePrice(min=quantize_money(low, currency), max=quantize_money(high, currency), currency=currency)
    return PointPrice(amount=quantize_money(low, currency), currency=currency)


def _unique(values: Sequence[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))


def summarize_prices(components: Mapping[str, PriceObservation | None], *, currency: str,
                     budget: str | Decimal | int | None = None, now: datetime | None = None,
                     scope_label: str = "За всех путешественников и весь срок поездки") -> PricingSummary:
    clock = now or utc_now()
    # A missing mapping entry is still a missing travel component, never zero.
    names = tuple(dict.fromkeys(("flight", "stay", "transfer", *components.keys())))
    included: dict[str, PriceObservation] = {}
    missing: dict[str, str] = {}
    references: list[str] = []
    exclusions: list[str] = []
    unknown_charges: list[str] = []
    shared_scope = None
    for name in names:
        quote = components.get(name)
        if quote is None:
            missing[name] = "Цена не получена"
            continue
        if quote.outcome == "reference":
            references.append(name)
        if quote.outcome != "quoted" or quote.normalized_group is None or quote.normalized_group.kind == "unknown":
            missing[name] = quote.reason or ("Опубликованный ориентир не подтвержден для этой поездки" if quote.outcome == "reference" else quote.outcome)
            continue
        if quote.normalized_group.currency != currency:
            missing[name] = "currency_mismatch: другая валюта, пересчет не выполнялся"
            continue
        if quote.freshness(clock) in ("stale", "unavailable"):
            missing[name] = "Срок цены истек; требуется новая проверка"
            continue
        # Scope must actually belong to the component receiving the observation.
        if quote.scope is None or quote.scope.component != name or not quote.scope.context_verified:
            missing[name] = "context_mismatch: цена относится к другому компоненту"
            continue
        scope = quote.scope
        common = (scope.date_from, scope.date_to, scope.adults, tuple(sorted(scope.children_ages)),
                  scope.destination.strip().casefold())
        if shared_scope is not None and common != shared_scope:
            missing[name] = "context_mismatch: даты, путешественники или направление компонентов различаются"
            continue
        shared_scope = common
        included[name] = quote
        exclusions.extend(quote.excluded_charges)
        unknown_charges.extend(quote.unknown_charges)
        exclusions.extend(quote.scope.exclusions)
        if quote.scope.taxes != "included":
            unknown_charges.append(name + ": налоги не включены или неизвестны")
        if quote.scope.mandatory_fees != "included":
            unknown_charges.append(name + ": обязательные сборы не включены или неизвестны")
    values = [quote.normalized_group for quote in included.values()]
    known = sum_price_values(values) if values else None
    transport_stay = sum_price_values([included[name].normalized_group for name in ("flight", "stay")]) if "flight" in included and "stay" in included else None
    is_estimate = any(quote.basis is not None or quote.normalized_group.kind == "closed_range" for quote in included.values())
    complete = not missing and not unknown_charges and not exclusions and all(quote.component_complete for quote in included.values())
    full = known if complete and known is not None and known.kind not in ("unknown", "lower_bound") else None
    if known is None:
        display_kind = "unavailable"
    elif known.kind == "lower_bound":
        display_kind = "lower_bound"
    elif transport_stay is None:
        display_kind = "partial"
    else:
        display_kind = "estimated_subtotal" if is_estimate else "observed_subtotal"

    assessment = "unknown"
    explanation = "Соответствие бюджету не подтверждено: применимой суммы нет"
    hard_status = "unknown"
    if budget is not None and known is not None:
        limit = Decimal(decimal_string(budget))
        low = Decimal(known.amount if known.kind == "point" else known.min)
        high = Decimal(known.amount if known.kind == "point" else known.max) if known.kind != "lower_bound" else None
        observed_only = [quote.normalized_group for quote in included.values() if quote.basis is None and quote.normalized_group.kind in ("point", "lower_bound")]
        observed_minimum = sum((Decimal(value.amount if value.kind == "point" else value.min) for value in observed_only), Decimal(0))
        if observed_minimum > limit:
            assessment, explanation, hard_status = "over_observed", "Известная совместимая часть расходов уже превышает бюджет", "failed"
        elif is_estimate:
            if low > limit:
                assessment, explanation = "over_estimate", "Ориентир уже выше бюджета; это оценка, а не подтвержденное нарушение"
            elif high is None or (low <= limit < high):
                assessment, explanation = "crosses_estimate", "Ориентир пересекает бюджет; соответствие не подтверждено"
            elif full is not None:
                assessment, explanation = "within_estimate", "Оценочный полный итог в пределах бюджета; требуется проверка"
            else:
                assessment, explanation = "incomplete", "Известна оценка только части расходов; соответствие бюджету не подтверждено"
        elif low > limit:
            assessment, explanation, hard_status = "over_observed", "Известная совместимая часть расходов уже превышает бюджет", "failed"
        elif full is not None and known.kind == "point":
            assessment, explanation, hard_status = "within_observed", "Наблюдаемый полный итог не превышает бюджет; наличие не гарантируется", "passed"
        else:
            assessment, explanation = "incomplete", "Полная стоимость или обязательные доплаты неизвестны; соответствие бюджету не подтверждено"
    elif budget is not None and (missing or references):
        assessment = "incomplete"
    observations = list(included.values())
    return PricingSummary(display_kind=display_kind, transport_stay=transport_stay, known_partial=known, full_trip=full,
                          included_components=tuple(included), missing_components=tuple(missing), reference_components=tuple(references), missing_reasons=missing,
                          excluded_charges=_unique(exclusions), unknown_charges=_unique(unknown_charges), currency=currency, scope_label=scope_label,
                          as_of=min((quote.observed_at for quote in observations), default=None),
                          expires_at=min((quote.expires_at for quote in observations), default=None), quote_ids=tuple(quote.id for quote in observations),
                          assumptions=_unique([item for quote in observations for item in quote.scope.assumptions]),
                          budget_assessment=assessment, budget_explanation=explanation, hard_constraint_status=hard_status)
