from datetime import datetime, timezone
from types import SimpleNamespace
import pytest
from pydantic import ValidationError
from backend.app.models import TravelIntent, TravelComponent
from backend.app.pricing.demo import build_demo_pricing
from backend.app.pricing.demo_models import DemoPricing
from backend.app.pricing.models import PointPrice, RangePrice, LowerBoundPrice, PriceObservation, QuoteScope

def option(quote=None, kind='stay'):
    parts = {name: TravelComponent(title=name, description='fixture') for name in ('flight', 'stay', 'transfer')}
    parts[kind].quote = quote
    return SimpleNamespace(**parts)

def observation(kind='stay', unit='room_night', value=None, **scope):
    return PriceObservation(provider_id='fixture', outcome='reference', value=value or PointPrice(amount='100', currency='EUR'), scope=QuoteScope(component=kind, entity_level='property' if kind == 'stay' else 'route', unit=unit, **scope), source_url='https://example.org/tariff', observed_at=datetime(2020, 1, 1, tzinfo=timezone.utc))

def test_unknowns_complete_deterministic_independent_of_budget_and_source_untouched():
    intent = TravelIntent(adults=2, children_ages=[8], rooms=2, nights_min=3)
    source = option()
    first = build_demo_pricing(intent, source)
    assert first == build_demo_pricing(intent, source)
    assert [item.example_amount_minor for item in first.components] == [46000, 54000, 7000]
    assert first.example_total_minor == 109675
    assert all((item.basis == 'synthetic_demo' for item in first.components))
    assert source.stay.price is None and source.stay.quote is None
    second = build_demo_pricing(intent.model_copy(update={'budget_total': 1}), source)
    assert first.total == second.total and second.budget_assessment == 'over_range'

@pytest.mark.parametrize('kind,unit,extra,expected', [('stay', 'room_night', {}, ('480.00', '720.00')), ('stay', 'group_night', {}, ('240.00', '360.00')), ('flight', 'person_trip', {'trip_type': 'one_way'}, ('480.00', '720.00')), ('flight', 'group_trip', {}, ('80.00', '120.00')), ('stay', 'group_stay', {}, ('80.00', '120.00'))])
def test_reference_unit_scaling_preserves_original(kind, unit, extra, expected):
    quote = observation(kind, unit, **extra)
    original = quote.model_dump()
    result = build_demo_pricing(TravelIntent(adults=2, children_ages=[5], rooms=2, nights_min=3), option(quote, kind))
    item = next((item for item in result.components if item.kind == kind))
    assert (item.value.min, item.value.max) == expected
    assert item.basis == 'comparable_source' and item.source_observed_at == quote.observed_at
    assert quote.model_dump() == original and quote.normalized_group is None

@pytest.mark.parametrize('value,expected', [(LowerBoundPrice(min='100', currency='EUR'), ('100.00', '160.00')), (RangePrice(min='100', max='200', currency='EUR'), ('80.00', '240.00'))])
def test_range_and_lower_bound(value, expected):
    item = build_demo_pricing(TravelIntent(), option(observation(unit='group_stay', value=value))).components[1]
    assert (item.value.min, item.value.max) == expected

@pytest.mark.parametrize('value,unit,label', [(PointPrice(amount='100', currency='USD'), 'room_night', None), (PointPrice(amount='0', currency='EUR'), 'room_night', None), (PointPrice(amount='100000000', currency='EUR'), 'group_stay', None), (PointPrice(amount='10', currency='EUR'), 'unknown', None), (PointPrice(amount='10', currency='EUR'), 'room_night', 'pet fee')])
def test_invalid_or_ancillary_source_uses_fictional_tariff(value, unit, label):
    quote = observation(unit=unit, value=PointPrice(amount='1', currency='EUR')).model_copy(update={'value': value, 'unit_label': label})
    assert build_demo_pricing(TravelIntent(), option(quote)).components[1].basis == 'synthetic_demo'

def test_normalized_group_never_multiplied_twice():
    quote = observation(unit='room_night').model_copy(update={'normalized_group': PointPrice(amount='300', currency='EUR')})
    item = build_demo_pricing(TravelIntent(rooms=2, nights_min=4), option(quote)).components[1]
    assert (item.value.min, item.value.max) == ('240.00', '360.00')

def test_unsupported_currency_no_fx_and_exact_date_nights():
    from datetime import date
    result = build_demo_pricing(TravelIntent(currency='CAD', budget_total=1000, date_from=date(2030, 1, 1), date_to=date(2030, 1, 3), nights_min=8), option())
    assert result.currency == 'EUR' and result.budget_assessment == 'currency_mismatch'
    assert result.components[1].example_amount_minor == 18000

@pytest.mark.parametrize('field', ['example_total_minor', 'example_service_fee_minor', 'total', 'service_fee', 'components'])
def test_dto_rejects_corrupted_invoice(field):
    data = build_demo_pricing(TravelIntent(), option()).model_dump()
    if field in {'total', 'service_fee'}:
        data[field]['max'] = '999999.00'
    elif field == 'components':
        data[field] = list(reversed(data[field]))
    else:
        data[field] += 1
    with pytest.raises(ValidationError):
        DemoPricing.model_validate(data)

def test_nonfinite_bypassed_source_and_aggregate_extreme_fail_closed():
    bad = observation(unit='group_stay').model_copy(update={'value': PointPrice.model_construct(amount='NaN', currency='EUR', kind='point')})
    assert build_demo_pricing(TravelIntent(), option(bad)).components[1].basis == 'synthetic_demo'
    source = option()
    for kind in ('flight', 'stay', 'transfer'):
        getattr(source, kind).quote = observation(kind, 'group_stay' if kind == 'stay' else 'group_trip', value=PointPrice(amount='40000000', currency='EUR'))
    result = build_demo_pricing(TravelIntent(), source)
    assert all((item.basis == 'synthetic_demo' for item in result.components))

def test_dto_rejects_currency_and_component_midpoint_corruption():
    result = build_demo_pricing(TravelIntent(), option())
    for mutate in (lambda data: data['components'][0]['value'].update(currency='USD'), lambda data: data['components'][0].update(example_amount_minor=0)):
        data = result.model_dump()
        mutate(data)
        with pytest.raises(ValidationError):
            DemoPricing.model_validate(data)

def test_subcent_comparable_falls_back_before_rounding_to_zero():
    quote = observation(unit='group_stay', value=PointPrice(amount='0.001', currency='EUR'))
    result = build_demo_pricing(TravelIntent(), option(quote))
    assert result.components[1].basis == 'synthetic_demo'
    assert result.components[1].value.min != '0.00'

def test_assumptions_explain_actual_counts_and_multiplier():
    intent = TravelIntent(adults=3, children_ages=[5, 7], rooms=2, nights_min=3)
    result = build_demo_pricing(intent, option(observation()))
    assert any(('2 ном. × 3 ноч.' in note and '×6' in note for note in result.components[1].assumptions))
    assert any(('5 чел.' in note and '2 машин' in note for note in result.components[2].assumptions))
