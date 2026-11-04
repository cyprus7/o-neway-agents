"""Caution affects source order only, with controls for desired negative conditions."""
import copy

import pytest

from backend.app.retrieval import score_rows, source_caution


@pytest.mark.parametrize('query,text', [
    ('Room allowing cats', 'Cats are not accepted.'),
    ('Комната принимает кошку, не только собак', 'Only dogs are admitted; cats are not accepted.'),
    ('Пускают с собакой-проводником', 'Собаки-проводники в здании запрещены.'),
    ('A puppy may stay alone', 'Щенка нельзя оставлять одного в номере.'),
    ('Pool access hoist', 'There is no pool lift or hoist.'),
    ('A quiet room', 'Quiet-room details are not published.'),
])
def test_explicit_restrictions_or_missing_details_receive_caution(query, text):
    assert source_caution(query, text) > 0


@pytest.mark.parametrize('query,text', [
    ('Без ступеней до номера', 'There are no stairs anywhere on the route.'),
    ('Step-free access', 'There are no steps between the pavement and bedrooms.'),
    ('No dogs near the room', 'Dogs are not allowed in this wing.'),
    ('A fragrance-free room', 'Perfumed cleaning agents are not allowed.'),
    ('Cats allowed, not just dogs', 'Cats are allowed; permission is not restricted to dogs.'),
    ('Room without a telephone', 'There is no telephone in this room.'),
    ('No more than 300 metres away', 'The entrance is 180 metres away.'),
])
def test_desired_absences_and_not_only_are_not_generic_denials(query, text):
    assert source_caution(query, text) == 0


def test_hybrid_reorders_a_negated_near_match_without_removing_or_verifying_it():
    rows = [{'title': 'Hotel A', 'content': 'Cats are not accepted.', 'price': None, 'verified': False},
            {'title': 'Hotel B', 'content': 'Cats are welcome in guestrooms.', 'price': None, 'verified': False}]
    before = copy.deepcopy(rows)
    query_vector = [1] + [0] * 1023
    vectors = [[0.85, (1 - 0.85 ** 2) ** 0.5] + [0] * 1022,
               [0.8, 0.6] + [0] * 1022]
    assert score_rows(rows, 'Room allowing cats', query_vector, vectors)[0] is rows[0]
    result = score_rows(rows, 'Room allowing cats', query_vector, vectors, 'hybrid')
    assert result[0] is rows[1] and result[1] is rows[0]
    assert rows == before
