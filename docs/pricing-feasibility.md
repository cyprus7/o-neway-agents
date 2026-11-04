# Interpreting price evidence

A visible amount is not necessarily a quote for the requested trip. Preserve the source,
original observation time, currency, unit, traveler scope, property or route identity,
and included or unknown fees. Missing real-world prices remain unknown.

Keep three measures separate:

1. **Compatible normalized quote:** current scope is verified and `normalized_group`
   contains a total for the requested group and component.
2. **Displayable reference:** a source amount is useful with its literal unit and
   limitations, but does not establish a complete group total.
3. **Illustrative demo range:** deterministic example amounts under `demo_pricing`,
   calculated independently of factual totals and never treated as inventory evidence.

The public-airline adapters publish references. A single-adult fare does not confirm
child prices, seats for the whole group, baggage or other requested extras. Hotel search
controls do not by themselves prove rate occupancy, availability, taxes or room policy.
Never substitute airports or properties, multiply a reference into a factual total,
convert currency implicitly, or turn an ancillary charge into a base rate.

`PriceObservation` and normalization are defined in
[pricing models](../backend/app/pricing/models.py) and
[normalization](../backend/app/pricing/normalization.py). The
[API contract](API_CONTRACT.md) describes public projection and refresh behavior.
[Demo pricing](DEMO_PRICING_CONTRACT.md) separately defines fictional arithmetic and
its relationship to simulated checkout.

For local fixture verification:

```text
python -m pytest backend/tests/test_pricing_core.py backend/tests/test_pricing_foundation.py backend/tests/test_demo_price_estimates.py -q
```

These tests validate code behavior against fixtures. They do not measure current
provider coverage, availability, successful bookings or live model quality.
