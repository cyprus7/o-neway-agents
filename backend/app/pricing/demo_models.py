"""Illustrative prices, deliberately separate from provider observations."""
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Literal
from pydantic import Field, model_validator
from .models import Currency, FrozenModel, RangePrice

ComponentKind = Literal["flight", "stay", "transfer"]


def cents(value: str) -> int:
    number = Decimal(value) * 100
    if number != number.to_integral_value():
        raise ValueError("Demo prices require whole cents")
    return int(number)


def fee_minor(subtotal: int) -> int:
    return int((Decimal(subtotal) * Decimal("0.025")).quantize(Decimal(1), rounding=ROUND_HALF_UP))


class DemoComponentPrice(FrozenModel):
    kind: ComponentKind
    value: RangePrice
    example_amount_minor: int = Field(ge=0, strict=True)
    basis: Literal["comparable_source", "synthetic_demo"]
    source_url: str | None = None
    source_observed_at: datetime | None = None
    assumptions: list[str]

    @model_validator(mode="after")
    def valid_example(self) -> "DemoComponentPrice":
        low, high = cents(self.value.min), cents(self.value.max)
        if self.example_amount_minor != (low + high + 1) // 2:
            raise ValueError("Demo example must be rounded midpoint")
        return self


class DemoPricing(FrozenModel):
    schema_version: Literal["demo-pricing.v1"] = "demo-pricing.v1"
    basis: Literal["demo_estimate"] = "demo_estimate"
    currency: Currency
    components: list[DemoComponentPrice] = Field(min_length=3, max_length=3)
    total: RangePrice
    service_fee: RangePrice
    example_service_fee_minor: int = Field(ge=0, strict=True)
    example_total_minor: int = Field(ge=0, strict=True)
    scope_label: str
    budget_assessment: Literal[
        "within_range", "crosses_range", "over_range", "not_requested", "currency_mismatch"
    ]
    budget_explanation: str
    assumptions: list[str]

    @model_validator(mode="after")
    def valid_invoice(self) -> "DemoPricing":
        if [item.kind for item in self.components] != ["flight", "stay", "transfer"]:
            raise ValueError("Expected flight, stay, transfer in order")
        ranges = [item.value for item in self.components]
        if any(value.currency != self.currency for value in [*ranges, self.total, self.service_fee]):
            raise ValueError("Mixed demo currencies")
        low, high = (sum(cents(getattr(value, end)) for value in ranges) for end in ("min", "max"))
        if (cents(self.service_fee.min), cents(self.service_fee.max)) != (fee_minor(low), fee_minor(high)):
            raise ValueError("Incorrect demo service fee")
        if (cents(self.total.min), cents(self.total.max)) != (low + fee_minor(low), high + fee_minor(high)):
            raise ValueError("Incorrect demo endpoint sums")
        subtotal = sum(item.example_amount_minor for item in self.components)
        expected_fee = fee_minor(subtotal)
        if (
            self.example_service_fee_minor != expected_fee
            or self.example_total_minor != subtotal + expected_fee
        ):
            raise ValueError("Incorrect demo invoice sum")
        if not cents(self.total.min) <= self.example_total_minor <= cents(self.total.max):
            raise ValueError("Demo invoice outside range")
        return self
