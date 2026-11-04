"""Synthetic evidence: money and scope correctness, not live-provider coverage."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from backend.app.pricing.aggregation import sum_price_values, summarize_prices
from backend.app.pricing.estimates import estimate_from_observations
from backend.app.pricing.models import (
    LowerBoundPrice, PointPrice, PriceObservation, ProviderPolicy, QuoteScope,
    RangePrice, RequestContext, UnknownPrice,
)
from backend.app.pricing.normalization import (
    compatible_scope, normalize_observation, offer_scope_hash, quantize_money,
    request_context_hash,
)


NOW = datetime.now(timezone.utc) - timedelta(minutes=2)


def context(component="flight", **changes):
    values = dict(component=component, origin="LCA", destination="ATH", date_from="2027-06-10", date_to="2027-06-17", adults=2, children_ages=[], currency="EUR", trip_type="roundtrip" if component != "stay" else "not_applicable")
    values.update(changes)
    return RequestContext(**values)


def quote(component="flight", amount="100.10", **changes):
    ctx = context(component)
    scope = QuoteScope(**ctx.model_dump(exclude={"target_policy", "entity_id"}), entity_level="property" if component == "stay" else "route", entity_id="synthetic-hotel" if component == "stay" else "LCA-ATH", unit="group_stay" if component == "stay" else "group_trip", context_verified=True, taxes="included", mandatory_fees="included")
    values = dict(provider_id="synthetic-provider", request_context_hash=request_context_hash(ctx), scope=scope, value=PointPrice(amount=amount, currency="EUR"), normalized_group=PointPrice(amount=amount, currency="EUR"), source_url="https://synthetic.example/" + component, observed_at=NOW - timedelta(minutes=2), retrieved_at=NOW - timedelta(minutes=1), expires_at=NOW + timedelta(minutes=25), outcome="quoted", component_complete=True)
    values.update(changes)
    return PriceObservation(**values)


@pytest.mark.parametrize("amount", ["NaN", "Infinity", "-1", True, 1.2, "1e20", "1.000000001"])
def test_invalid_or_binary_money_is_rejected(amount):
    with pytest.raises(ValidationError):
        PointPrice(amount=amount, currency="EUR")


def test_decimal_serialization_is_string_and_rounding_uses_currency_minor_units():
    assert PointPrice(amount=Decimal("12.30"), currency="EUR").model_dump(mode="json")["amount"] == "12.30"
    assert quantize_money("12.345", "EUR") == "12.35"
    assert quantize_money("12.5", "JPY") == "13"
    assert quantize_money("12.3456", "KWD") == "12.346"
    with pytest.raises(ValueError):
        quantize_money("10", "XXX")


def test_ranges_reject_inversion_and_lower_bound_rejects_finite_upper():
    with pytest.raises(ValidationError):
        RangePrice(min="20", max="10", currency="EUR")
    with pytest.raises(ValidationError):
        LowerBoundPrice(min="10", max="20", currency="EUR")


def test_unknown_never_serializes_as_zero():
    value = sum_price_values([PointPrice(amount="10.10", currency="EUR"), UnknownPrice(reason="Missing rate")])
    assert value.kind == "unknown"
    assert "amount" not in value.model_dump()
    assert sum_price_values([]).kind == "unknown"


def test_decimal_addition_and_lower_bound_keep_unbounded_upper():
    assert sum_price_values([PointPrice(amount="0.10", currency="EUR"), PointPrice(amount="0.20", currency="EUR")]).amount == "0.30"
    value = sum_price_values([RangePrice(min="100", max="150", currency="EUR"), LowerBoundPrice(min="25", currency="EUR")])
    assert value.kind == "lower_bound" and value.min == "125.00" and value.max is None
    mixed = sum_price_values([PointPrice(amount="100", currency="EUR"), PointPrice(amount="120", currency="USD")])
    assert mixed.kind == "unknown" and "currency" in mixed.reason


def test_context_hash_keeps_repeated_ages_and_ignores_order_but_not_group():
    first = context(children_ages=[4, 7, 4])
    assert request_context_hash(first) == request_context_hash(context(children_ages=[7, 4, 4]))
    assert request_context_hash(first) != request_context_hash(context(children_ages=[4, 7]))
    with pytest.raises(ValidationError):
        RequestContext(**first.model_dump(), budget_total=1000)


def test_room_allocation_requires_exact_party_multiset():
    context("stay", rooms=2, children_ages=[4, 4], room_allocation=[{"adults": 1, "children_ages": [4]}, {"adults": 1, "children_ages": [4]}])
    with pytest.raises(ValidationError):
        context("stay", rooms=2, children_ages=[4, 4], room_allocation=[{"adults": 1, "children_ages": [4]}, {"adults": 1, "children_ages": []}])


@pytest.mark.parametrize("change", [{"date_from": "2027-06-11"}, {"adults": 3}, {"children_ages": [0]}, {"trip_type": "one_way"}, {"origin": "PFO"}, {"destination": "SKG"}, {"currency": "USD"}])
def test_old_or_different_scope_cannot_be_normalized(change):
    current = context(**change)
    assert not compatible_scope(current, quote().scope)
    result = normalize_observation(quote(), current)
    assert result.normalized_group is None and result.outcome in ("context_mismatch", "currency_mismatch")


def test_unknown_unit_refinement_changes_offer_hash_not_request_hash():
    current = context()
    known = quote().scope
    raw = QuoteScope(**{**known.model_dump(), "context_verified": False, "unit": "unknown"})
    assert request_context_hash(current) == request_context_hash(current)
    assert offer_scope_hash(raw) != offer_scope_hash(known)
    raw_quote = PriceObservation(**{**quote().model_dump(), "outcome": "reference", "scope": raw, "normalized_group": None})
    assert normalize_observation(raw_quote, current).normalized_group is None
    refined = PriceObservation(**{**raw_quote.model_dump(), "scope": known})
    assert normalize_observation(refined, current).normalized_group.amount == "100.10"


def test_published_per_person_and_nightly_references_are_visible_but_not_trip_prices():
    for component, unit, label in [("flight", "person_trip", "На взрослого, в одну сторону"), ("stay", "room_night", "За номер/ночь, применимый год неизвестен")]:
        scope = QuoteScope(component=component, entity_level="property" if component == "stay" else "route", unit=unit, currency="EUR", destination="ATH")
        reference = PriceObservation(provider_id="synthetic-public-rate", scope=scope, value=LowerBoundPrice(min="49.99", currency="EUR"), source_url="https://synthetic.example/public-rate", observed_at=NOW, retrieved_at=NOW, outcome="reference", unit_label=label, reason="Dates/party not established")
        normalized = normalize_observation(reference, context(component))
        assert normalized.value.min == "49.99" and normalized.normalized_group is None
        assert normalized.outcome == "reference"
        result = summarize_prices({component: normalized}, currency="EUR", budget="500", now=NOW)
        assert result.known_partial is None and result.full_trip is None
        assert result.budget_assessment in ("incomplete", "unknown") and result.hard_constraint_status == "unknown"
        assert component in result.reference_components


def test_fixed_property_cannot_be_replaced_but_candidate_can_refine_entity():
    ctx = context("stay", target_policy="fixed_entity", entity_id="another-hotel")
    assert not compatible_scope(ctx, quote("stay").scope)
    assert compatible_scope(context("stay"), quote("stay").scope)


def test_fare_constraints_need_evidence_in_actual_scope():
    assert not compatible_scope(context(fare_constraints=["direct_flight"]), quote().scope)


def test_timestamps_are_immutable_and_expiry_derived_without_cache_extension():
    original = quote()
    saved = original.model_dump_json()
    reloaded = PriceObservation.model_validate_json(saved)
    assert reloaded.freshness(NOW, "cache_reuse") == "cached"
    assert reloaded.freshness(NOW + timedelta(minutes=25)) == "stale"
    assert reloaded.freshness(NOW, "history") == "stale"
    assert reloaded.model_dump_json() == saved
    with pytest.raises(ValidationError):
        reloaded.expires_at = NOW + timedelta(days=1)
    projection = reloaded.public_dict(NOW)
    assert projection["amount_kind"] == "observed" and projection["freshness"] == "live"


@pytest.mark.parametrize("change", [{"observed_at": NOW + timedelta(days=1)}, {"retrieved_at": datetime.now(timezone.utc) + timedelta(days=1)}, {"retrieved_at": datetime(2026, 10, 24)}, {"expires_at": NOW - timedelta(days=1)}])
def test_bad_timestamp_order_timezone_and_future_clock_are_rejected(change):
    with pytest.raises(ValidationError):
        quote(**change)


def test_quote_scope_and_uncertainty_are_not_optional_for_numeric_group_value():
    with pytest.raises(ValidationError):
        quote(scope=None)
    with pytest.raises(ValidationError):
        quote(value=LowerBoundPrice(min="100", currency="EUR"))
    with pytest.raises(ValidationError):
        quote(amount="0")
    with pytest.raises(ValidationError):
        quote(scope=QuoteScope(**{**quote().scope.model_dump(), "unit": "person_trip"}))
    with pytest.raises(ValidationError):
        quote(normalized_group=PointPrice(amount="200.20", currency="EUR"))


@pytest.mark.parametrize("url", ["https://user:secret@synthetic.example/", "https://synthetic.example/?api_key=secret", "https://synthetic.example/?session_id=secret", "javascript:alert(1)"])
def test_source_urls_cannot_serialize_credentials_or_executable_schemes(url):
    with pytest.raises(ValidationError):
        quote(source_url=url)


def test_flight_and_stay_subtotal_survives_unknown_transfer_and_charges():
    result = summarize_prices({"flight": quote(amount="100.10"), "stay": quote("stay", amount="200.20"), "transfer": None}, currency="EUR", budget="500", now=NOW)
    assert result.transport_stay.amount == "300.30"
    assert result.known_partial.amount == "300.30" and result.full_trip is None
    assert result.missing_components == ("transfer",)
    assert result.hard_constraint_status == "unknown" and result.budget_assessment == "incomplete"


def test_partial_over_budget_can_fail_but_under_budget_does_not_pass():
    over = summarize_prices({"flight": quote(amount="600"), "stay": None, "transfer": None}, currency="EUR", budget="500", now=NOW)
    assert over.budget_assessment == "over_observed" and over.hard_constraint_status == "failed"
    under = summarize_prices({"flight": quote(amount="100"), "stay": None, "transfer": None}, currency="EUR", budget="500", now=NOW)
    assert under.budget_assessment == "incomplete" and under.hard_constraint_status == "unknown"


def test_estimated_or_expired_amounts_never_fail_or_pass_hard_budget():
    expired = quote(amount="600", expires_at=NOW - timedelta(seconds=1))
    result = summarize_prices({"flight": expired}, currency="EUR", budget="500", now=NOW)
    assert result.known_partial is None and result.hard_constraint_status == "unknown"
    ranged = RangePrice(min="600", max="700", currency="EUR")
    estimated = quote(value=ranged, normalized_group=ranged)
    result = summarize_prices({"flight": estimated}, currency="EUR", budget="500", now=NOW)
    assert result.budget_assessment == "over_estimate" and result.hard_constraint_status == "unknown"


def test_observed_partial_over_budget_remains_a_proof_when_other_component_is_estimated():
    ranged = RangePrice(min="100", max="200", currency="EUR")
    estimated = quote("stay", value=ranged, normalized_group=ranged)
    result = summarize_prices({"flight": quote(amount="600"), "stay": estimated}, currency="EUR", budget="500", now=NOW)
    assert result.budget_assessment == "over_observed" and result.hard_constraint_status == "failed"


def test_mixed_currency_component_does_not_enter_requested_currency_subtotal():
    usd_scope = QuoteScope(**{**quote("stay").scope.model_dump(), "currency": "USD"})
    usd_price = PointPrice(amount="200", currency="USD")
    usd_quote = quote("stay", scope=usd_scope, value=usd_price, normalized_group=usd_price)
    result = summarize_prices({"flight": quote(), "stay": usd_quote, "transfer": None}, currency="EUR", now=NOW)
    assert result.transport_stay is None and result.known_partial.amount == "100.10"
    assert "currency_mismatch" in result.missing_reasons["stay"]


@pytest.mark.parametrize("change", [{"adults": 1}, {"children_ages": [2]}, {"date_from": "2027-06-11"}, {"destination": "SKG"}])
def test_components_of_different_trips_cannot_be_added(change):
    stay_scope = QuoteScope(**{**quote("stay").scope.model_dump(), **change})
    result = summarize_prices({"flight": quote(), "stay": quote("stay", scope=stay_scope)}, currency="EUR", now=NOW)
    assert result.transport_stay is None and result.known_partial.amount == "100.10"
    assert "context_mismatch" in result.missing_reasons["stay"]


def test_complete_components_only_make_full_total_with_known_mandatory_charges():
    parts = {name: quote(name, amount="100") for name in ("flight", "stay", "transfer")}
    result = summarize_prices(parts, currency="EUR", budget="500", now=NOW)
    assert result.full_trip.amount == "300.00" and result.budget_assessment == "within_observed"
    parts["stay"] = quote("stay", amount="100", unknown_charges=["tourist tax"], component_complete=False)
    result = summarize_prices(parts, currency="EUR", budget="500", now=NOW)
    assert result.full_trip is None and "tourist tax" in result.unknown_charges
    assert result.hard_constraint_status == "unknown"


def policy(**changes):
    return ProviderPolicy(**{"id": "synthetic-provider", "retention_allowed": True, "historical_estimate_allowed": True, "retention_seconds": 86400 * 7, **changes})


def test_estimate_requires_two_distinct_compatible_real_points_and_preserves_basis():
    first = quote(id="first", amount="100", observed_at=NOW - timedelta(days=1), retrieved_at=NOW - timedelta(days=1), expires_at=NOW - timedelta(hours=23))
    second = quote(id="second", amount="140", observed_at=NOW - timedelta(hours=1), retrieved_at=NOW - timedelta(hours=1), expires_at=NOW - timedelta(minutes=30))
    estimate = estimate_from_observations([first, second], context(), policy(), now=NOW)
    assert estimate is not None and estimate.value.kind == "closed_range"
    assert estimate.value.min == "100.00" and estimate.value.max == "140.00"
    assert estimate.basis.basis_ids == ("first", "second")
    assert estimate.basis.observed_from == first.observed_at
    assert estimate.amount_kind == "estimate_range" and not estimate.availability_verified
    assert estimate_from_observations([first], context(), policy(), now=NOW) is None
    assert estimate_from_observations([first, first], context(), policy(), now=NOW) is None


def test_duplicate_upstream_result_and_policy_prohibition_cannot_make_estimate():
    first = quote(id="one", upstream_id="same-upstream")
    second = quote(id="two", amount="140", upstream_id="same-upstream")
    assert estimate_from_observations([first, second], context(), policy(), now=NOW) is None
    assert estimate_from_observations([first, quote(id="three", amount="150")], context(), policy(historical_estimate_allowed=False), now=NOW) is None


def test_old_dates_group_rate_currency_and_more_than_seven_days_do_not_make_estimate():
    first = quote(id="one")
    old = quote(id="old", amount="120", observed_at=NOW - timedelta(days=8), retrieved_at=NOW - timedelta(days=8), expires_at=NOW - timedelta(days=7))
    assert estimate_from_observations([first, old], context(), policy(), now=NOW) is None
    assert estimate_from_observations([first, quote(id="two", amount="140")], context(adults=3), policy(), now=NOW) is None
    changed_rate = QuoteScope(**{**first.scope.model_dump(), "fare_class": "business"})
    assert estimate_from_observations([first, quote(id="two", amount="140", scope=changed_rate)], context(), policy(), now=NOW) is None


def test_estimate_selects_mutually_comparable_group_independently_of_input_order():
    different = quote(id="different", amount="600", scope=QuoteScope(**{**quote().scope.model_dump(), "fare_class": "business"}))
    first = quote(id="first", amount="120", observed_at=NOW - timedelta(minutes=10))
    second = quote(id="second", amount="140")
    forward = estimate_from_observations([different, first, second], context(), policy(), now=NOW)
    reverse = estimate_from_observations([second, first, different], context(), policy(), now=NOW)
    assert forward.basis == reverse.basis
    assert forward.value == reverse.value == RangePrice(min="120.00", max="140.00", currency="EUR")


def test_destination_comparables_are_not_attributed_to_one_property():
    first = quote("stay", id="hotel-one", amount="120")
    other_scope = QuoteScope(**{**first.scope.model_dump(), "entity_id": "another-hotel"})
    second = quote("stay", id="hotel-two", amount="140", scope=other_scope, source_url="https://synthetic.example/other-hotel")
    assert estimate_from_observations([first, second], context("stay"), policy(), now=NOW) is None
    estimate = estimate_from_observations([first, second], context("stay"), policy(), now=NOW, entity_level="destination_lodging")
    assert estimate.scope.entity_id is None and estimate.scope.entity_level == "destination_lodging"
    assert estimate.basis.differences
