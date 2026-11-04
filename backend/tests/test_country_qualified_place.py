import pytest

from backend.app.geography import find_place


@pytest.mark.parametrize('qualified,plain', [
    ('Warsaw, Poland', 'Warsaw'), ('Warszawa, PL', 'Warsaw'), ('Варшава, Польша', 'Warsaw'),
    ('Larnaca, Cyprus', 'Larnaca'), ('Ларнака, CY', 'Larnaca'), ('Berlin, Germany', 'Berlin'),
    ('London, United Kingdom', 'London'), ('Лондон, Великобритания', 'London'),
])
def test_known_city_and_matching_country_resolve_without_discarding_qualifier(qualified, plain):
    assert find_place(qualified) == find_place(plain)


@pytest.mark.parametrize('value', ['', ' ', 'Warsaw, Cyprus', 'Larnaca, Poland', 'London, Canada',
    'Berlin, unknown', 'Cyprus, Cyprus', 'Larnaca,', ', Cyprus', 'Larnaca, Cyprus, Poland'])
def test_missing_ambiguous_or_conflicting_qualifiers_never_fall_through(value):
    assert find_place(value) is None
