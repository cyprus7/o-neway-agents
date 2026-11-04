"""Pure deterministic illustrative calculator; provider facts remain unchanged."""
from __future__ import annotations
from decimal import Decimal, ROUND_HALF_UP
from typing import TYPE_CHECKING
from .models import PriceObservation, RangePrice
from .demo_models import ComponentKind
from .demo_models import DemoComponentPrice, DemoPricing, fee_minor
if TYPE_CHECKING:
    from ..models import TravelComponent, TravelIntent, TripOption

RATES = {"EUR": (180, 100, 90, 70), "USD": (200, 110, 100, 80),
         "GBP": (160, 90, 75, 65), "RUB": (18000, 10000, 9000, 7000)}
LIMIT = Decimal("100000000")


def minor(amount: Decimal) -> int:
    return int((amount * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def range_value(low: int, high: int, currency: str) -> RangePrice:
    return RangePrice(min=f"{Decimal(low) / 100:.2f}", max=f"{Decimal(high) / 100:.2f}", currency=currency)


def comparable(
    component: TravelComponent,
    kind: ComponentKind,
    currency: str,
    rooms: int,
    nights: int,
    party: int,
) -> tuple[int, int, PriceObservation] | None:
    quote = component.quote
    if quote is None or quote.scope is None or quote.scope.component != kind:
        return None
    # Only actual base-price observations qualify, never failed/ancillary results.
    if quote.outcome not in {"quoted", "reference"}:
        return None
    label = " ".join(str(value or "") for value in (quote.unit_label, quote.scope.rate_id)).casefold()
    ancillary_labels = (
        "deposit", "extra bed", "extra-bed", "pet fee", "pet charge",
        "extra_bed", "ancillary", "pet_fee", "залог", "доплата", "депозит",
    )
    if any(word in label for word in ancillary_labels):
        return None
    value = quote.normalized_group if quote.normalized_group is not None else quote.value
    if value.kind == "unknown" or value.currency != currency:
        return None
    multiplier = 1
    if quote.normalized_group is None:
        unit = quote.scope.unit
        if unit == "room_night":
            multiplier = rooms * nights
        elif unit == "group_night":
            multiplier = nights
        elif unit == "person_trip":
            multiplier = party * (2 if quote.scope.trip_type == "one_way" else 1)
        elif unit not in {"group_trip", "group_stay"}:
            return None
    try:
        low = Decimal(value.amount if value.kind == "point" else value.min) * multiplier
        if value.kind == "point":
            high = Decimal(value.amount) * multiplier
        elif value.kind == "lower_bound":
            high = Decimal(value.min) * multiplier
        else:
            high = Decimal(value.max) * multiplier
        if not low.is_finite() or not high.is_finite() or low <= 0 or high > LIMIT:
            return None
        if value.kind == "lower_bound":
            high *= Decimal("1.6")
        else:
            low *= Decimal("0.8")
            high *= Decimal("1.2")
        if high > LIMIT or low < Decimal("0.01"):
            return None
        return minor(low), minor(high), quote
    except (ArithmeticError, ValueError, TypeError):
        return None


def build_demo_pricing(intent: TravelIntent, option: TripOption) -> DemoPricing:
    requested = intent.currency
    currency = requested if requested in RATES else "EUR"
    if intent.date_from and intent.date_to:
        nights = (intent.date_to - intent.date_from).days
    else:
        nights = intent.nights_min or 1
    party = intent.adults + len(intent.children_ages)
    adult, child, room, vehicle = RATES[currency]
    synthetic = (adult * intent.adults + child * len(intent.children_ages),
        room * intent.rooms * nights,
        vehicle * ((party + 3) // 4))
    assumptions = ["Демонстрационный ориентир, не предложение бронирования; реальные цены, наличие и условия не подтверждены."]
    if requested != currency:
        assumptions.append("Для этой валюты нет демо-тарифа: показан независимый тариф EUR, без валютного пересчёта.")
    kinds = ("flight", "stay", "transfer")
    found_values = [comparable(getattr(option, kind),
        kind,
        currency,
        intent.rooms,
        nights,
        party) for kind in kinds]
    provisional_high = sum(item[1] if item else minor(Decimal(base) * Decimal("1.25")) for item,
        base in zip(found_values, synthetic))
    if provisional_high + fee_minor(provisional_high) > minor(LIMIT):
        found_values = [None, None, None]
        assumptions.append("Сопоставимые суммы превышают предел демо-расчёта; использованы вымышленные тарифы.")
    components = []
    for index, (kind, base) in enumerate(zip(kinds, synthetic)):
        found = found_values[index]
        if found:
            low, high, quote = found
            basis, url, observed = "comparable_source", quote.source_url, quote.observed_at
            assert quote.scope is not None
            unit = quote.scope.unit
            if quote.normalized_group is not None:
                scaling = "Нормализованная сумма за всю группу; множитель ×1, без повторного умножения."
            elif unit == "room_night":
                scaling = f"{intent.rooms} ном. × {nights} ноч. = множитель ×{intent.rooms * nights}."
            elif unit == "group_night":
                scaling = f"За группу из {party} чел. × {nights} ноч. = множитель ×{nights}."
            elif unit == "person_trip":
                legs = 2 if quote.scope.trip_type == "one_way" else 1
                scaling = f"{party} чел. × {legs} направления = множитель ×{party * legs}."
            else:
                scaling = f"Сумма {unit} за всю группу; множитель ×1. Состав группы источником не подтверждён."
            notes = [
                f"Запрошено: {party} чел., {intent.rooms} ном., {nights} ноч.",
                "Единица источника: " + unit + ". " + scaling,
                "Демо-расчёт по опубликованному сопоставимому ориентиру; даты, наличие и точный состав группы не подтверждены; источник может быть устаревшим.",
                "Диапазон: ±20% исходных границ; для нижней границы — от неё до ×1,6. Это не нормализованная котировка.",
            ]
        else:
            low, high = minor(Decimal(base) * Decimal("0.75")), minor(Decimal(base) * Decimal("1.25"))
            basis, url, observed = "synthetic_demo", None, None
            notes = ["Вымышленный независимый демо-тариф ±25%; не цена поставщика."]
            if kind == "flight":
                notes.append(f"{intent.adults} взр. × {adult} + {len(intent.children_ages)} дет. × {child} {currency}; туда и обратно.")
            elif kind == "stay":
                notes.append(f"{intent.rooms} ном. × {nights} ноч. × {room} {currency}; множитель ×{intent.rooms * nights}.")
            else:
                vehicles = (party + 3) // 4
                notes.append(f"{party} чел.; до 4 чел. на машину: {vehicles} машин × {vehicle} {currency}; туда и обратно.")
        components.append(
            DemoComponentPrice(
                kind=kind,
                value=range_value(low, high, currency),
                example_amount_minor=(low + high + 1) // 2,
                basis=basis,
                source_url=url,
                source_observed_at=observed,
                assumptions=notes,
            )
        )
    lows = sum(minor(Decimal(item.value.min)) for item in components)
    highs = sum(minor(Decimal(item.value.max)) for item in components)
    subtotal = sum(item.example_amount_minor for item in components)
    assessment, explanation = "not_requested", "Бюджет не задан."
    if intent.budget_total is not None:
        budget = Decimal(str(intent.budget_total)) * 100
        if requested != currency:
            assessment, explanation = "currency_mismatch", "Валюты бюджета и демо-ориентира различаются; сравнение не выполнялось."
        elif budget < lows + fee_minor(lows):
            assessment, explanation = "over_range", "Демо-диапазон выше бюджета; это оценка, не подтверждённая цена."
        elif budget < highs + fee_minor(highs):
            assessment, explanation = "crosses_range", "Бюджет пересекает демо-диапазон; реальный итог не подтверждён."
        else:
            assessment, explanation = "within_range", "Демо-диапазон укладывается в бюджет; реальные цены и условия требуют проверки."
    return DemoPricing(
        currency=currency,
        components=components,
        total=range_value(lows + fee_minor(lows), highs + fee_minor(highs), currency),
        service_fee=range_value(fee_minor(lows), fee_minor(highs), currency),
        example_service_fee_minor=fee_minor(subtotal),
        example_total_minor=subtotal + fee_minor(subtotal),
        scope_label=f"{intent.adults} взр. + {len(intent.children_ages)} дет. · {intent.rooms} ном. · {nights} ноч.",
        budget_assessment=assessment,
        budget_explanation=explanation,
        assumptions=assumptions,
    )
