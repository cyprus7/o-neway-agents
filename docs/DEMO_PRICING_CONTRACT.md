# Demo pricing contract

The demo displays explicitly illustrative travel prices and ranges.
Keep real provider observations unchanged. Add `TripOption.demo_pricing: DemoPricing | null`;
old sessions receive it through the existing public projection. No endpoint or gateway change.

Pure function: `build_demo_pricing(intent: TravelIntent, option: TripOption) -> DemoPricing`
in `backend/app/pricing/demo.py`. DTOs in `backend/app/pricing/demo_models.py` (no import of
app.models, avoiding a cycle). Use integer cents/Decimal, deterministic output, no IO or clock.

```text
DemoComponentPrice:
  kind: flight | stay | transfer
  value: RangePrice {kind: closed_range, min: decimal string, max: decimal string, currency}
  example_amount_minor: nonnegative integer
  basis: comparable_source | synthetic_demo
  source_url: string | null
  source_observed_at: datetime | null
  assumptions: list[string]
DemoPricing:
  schema_version: literal demo-pricing.v1
  basis: literal demo_estimate
  currency: three uppercase letters
  components: exactly one of flight, stay, transfer, in that order
  total: RangePrice (sum of component ranges + service fee range)
  service_fee: RangePrice (2.5% of subtotal endpoints, rounded to nearest cent)
  example_service_fee_minor: integer
  example_total_minor: integer (sum of example components + exact 2.5% fee)
  scope_label: string
  budget_assessment: within_range | crosses_range | over_range | not_requested | currency_mismatch
  budget_explanation: string
  assumptions: list[string]
```

The example amount is the midpoint of each component range in cents. The invoice uses those
same amounts. Validate currency consistency, endpoint sums, fee and example bounds in DTOs.
No estimates enter `PriceObservation`, `normalized_group`, factual `PricingSummary`, or verified
budget/availability/condition checks. Provider source URL/time are preserved, never renewed.

Comparable numeric observations with the chosen currency and a known unit can seed a range:
room_night × requested rooms × nights; group_night × nights; person_trip × party size (×2
only when source trip_type is one_way); group_trip / group_stay remain whole-group reference
amounts, with an explicit comparable-scope assumption. Prefer normalized_group when present
without applying the raw unit multiplier again. Point: ±20%; closed range: expand endpoints
by 20%; lower bound: [bound, bound ×1.6]. These are demo calculations, not normalized quotes.
Unknown units, mixed currency, zero, nonfinite or total >100,000,000 major currency units fall
back to fictional tariffs. Never use ancillary fee/charge amounts as base prices. Stale sources
are allowed as comparables and retain the original observed time, with an assumption explaining
that dates, availability and exact party scope are not confirmed.

Fictional independent rate cards (major units, NOT foreign-exchange conversions):
EUR adult flight180 / child100 / room-night90 / vehicle transfer70;
USD200 /110 /100 /80; GBP160 /90 /75 /65; RUB18000 /10000 /9000 /7000.
Unknown requested currency falls back to EUR and states that assumption. Synthetic flight
includes return; transfer includes return and ceil(party/4) vehicles. Stay uses requested rooms
and nights. Deterministic property variation may be omitted for simplicity. Synthetic range
is ±25%. Never adjust estimates to fit the user's budget. Nights use exact dates, otherwise
nights_min or1. Currency is intent currency when supported, else EUR.

Frontend: prefer demo range when factual full_trip is missing; label `Ориентировочно · демо`.
Render all three components and service fee from backend data; no frontend arithmetic.
`ComponentPrice` accepts an optional DemoComponentPrice, supplied by each option-aware caller.
Keep factual sources available under details; comparable/synthetic basis is readable. Show
explicit budget estimate and scope. Unknown factual conditions in demo flow go into a closed
`details` block `Перед реальной поездкой`; known failures and operational errors stay prominent.
Refresh completion must not turn unknown conditions into verified facts. Preserve auth, SSE,
job/revision fencing, previous-plan intent and real failed-constraint behavior.

