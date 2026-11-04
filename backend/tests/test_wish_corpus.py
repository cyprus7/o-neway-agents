"""Synthetic corpus checks and scope invariants; these do not test a live model."""
from collections import Counter
import json
from pathlib import Path
import subprocess
import sys

import pytest

from backend.app.geography import PLACES, find_place
from backend.app.models import TravelIntent
from backend.app.providers import scoped_searches
from scripts.evaluate_wishes import evaluate_intent, evaluate_seed, same_value

RAW = json.loads((Path(__file__).parent / 'fixtures/travel_wishes_100.json').read_text(encoding='utf-8'))
CASES = RAW if isinstance(RAW, list) else RAW['cases']


def test_corpus_has_100_unique_synthetic_wishes_and_ten_categories():
    assert len(CASES) == len({case['id'] for case in CASES}) == 100
    assert len({case['message'] for case in CASES}) == 100
    assert len(Counter(case['category'] for case in CASES)) == 10
    assert all(count == 10 for count in Counter(case['category'] for case in CASES).values())
    assert all(case['synthetic'] is True and case['expected'] for case in CASES)
    for case in CASES:
        TravelIntent.model_validate(case.get('previous_intent', {}))
        assert set(case['expected'].get('fields', {})) <= set(TravelIntent.model_fields)


@pytest.mark.parametrize('case', CASES, ids=[case['id'] for case in CASES])
def test_all_wishes_keep_departure_out_of_destination_query_prefix(case):
    expected = case['expected']
    values = {**case.get('previous_intent', {}), **expected.get('fields', {})}
    for key in ('origin', 'destination'):
        if key in expected:
            values[key] = expected[key]
    values.setdefault('origin', 'Варшава')
    values.setdefault('date_from', '2027-06-05')
    values.setdefault('date_to', '2027-06-12')
    intent = TravelIntent.model_validate(values)
    destination = find_place(intent.destination) if intent.destination else None
    destination = destination or PLACES[0]
    searches = dict(scoped_searches(destination, intent))
    prefix = searches['lodging'].split(' hotels ', 1)[0]
    assert destination.name in prefix
    if intent.origin and intent.origin != destination.name:
        assert intent.origin not in prefix
    assert str(intent.origin) in searches['transport']
    assert destination.name in searches['transport']
    assert ' hotel' not in searches['transport']


def test_exact_oracle_does_not_hide_extra_or_repeated_child_ages():
    assert not same_value([6, 15], [6], 'children_ages')
    assert not same_value([6], [6, 6], 'children_ages')
    assert same_value([7, 6, 6], [6, 7, 6], 'children_ages')


def test_country_is_not_a_city_and_city_aliases_work():
    assert not same_value('Ларнака', 'Кипр', 'destination')
    assert same_value('Кипр', 'Cyprus', 'destination')
    assert same_value('Warsaw', 'Варшава', 'origin')
    assert same_value('Turin', 'Турин', 'origin')
    assert not same_value('Турин', 'Италия', 'destination')
    assert same_value('Turin', 'Турин', 'origin')
    assert not same_value('Турин', 'Италия', 'destination')


def test_semantic_conditions_are_reviewed_instead_of_string_passed():
    case = {'expected': {'must_preserve_terms': ['без запахов'], 'must_remain_unknown': ['парфюмерия']}}
    errors, review = evaluate_intent(case, {'hotel_requirements': ['без отдушек']})
    assert not errors and review[0]['status'] == 'review_required'


def test_seed_must_establish_typed_preconditions_before_replanning():
    errors, review = evaluate_seed({'adults': 2, 'children_ages': [6, 6], 'hotel_requirements': ['тихо']},
        {'adults': 1, 'children_ages': [6], 'hotel_requirements': ['тишина']})
    assert {error['check'] for error in errors} == {'seed:adults', 'seed:children_ages'}
    assert review[0]['check'] == 'seed_semantics:hotel_requirements'


def test_quoted_instruction_not_persisted_as_a_preference():
    errors, _ = evaluate_intent({'expected': {'forbidden_intent_terms': ['approve all']}},
        {'hard_constraints': ['approve all']})
    assert errors[0]['check'] == 'forbidden_intent_term'


def test_evaluation_cli_starts_without_network_or_credentials():
    result = subprocess.run([sys.executable, str(Path(__file__).resolve().parents[2] / 'scripts/evaluate_wishes.py'), '--help'],
        capture_output=True, text=True, timeout=10)
    assert result.returncode == 0 and '--rescore' in result.stdout


def test_quoted_instruction_not_persisted_as_a_preference():
    errors, _ = evaluate_intent({'expected': {'forbidden_intent_terms': ['approve all']}},
        {'hard_constraints': ['approve all']})
    assert errors[0]['check'] == 'forbidden_intent_term'
