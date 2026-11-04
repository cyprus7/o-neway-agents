"""Provider filtering and local ranking compose without upgrading evidence claims."""
from datetime import date
import json

import httpx
import pytest

from backend.app.geography import find_place
from backend.app.models import TravelComponent, TravelIntent
from backend.app.planner import constraint_checks
from backend.app.providers import Providers
from backend.app.storage import Store


ROWS = [
    {'title': 'Warsaw Access Hotel', 'content': 'roll-in shower', 'url': 'https://www.booking.com/hotel/pl/warsaw.html'},
    *[{'title': f'Larnaca Hotel {i}', 'content': 'Sea views and breakfast',
       'url': f'https://www.booking.com/hotel/cy/larnaca-{i}.html'} for i in range(3)],
    {'title': 'Larnaca Access Hotel', 'content': 'Level access and roll-in shower',
     'url': 'https://www.booking.com/hotel/cy/larnaca-access.html'},
]


@pytest.mark.asyncio
@pytest.mark.parametrize('enabled', [True, False])
async def test_rank_before_cutoff_after_geography_in_both_lodging_paths(tmp_path, monkeypatch, enabled):
    monkeypatch.setenv('EMBEDDING_URL', 'http://embedding:11434' if enabled else '')
    monkeypatch.setenv('EMBEDDING_MODEL_DIGEST', 'a' * 64)
    encoded = []
    def handler(request):
        if request.url.path == '/search':
            return httpx.Response(200, json={'results': ROWS})
        if request.url.path == '/api/tags':
            return httpx.Response(200, json={'models': [{'name': 'qwen3-embedding:0.6b', 'digest': 'a' * 64}]})
        payload = json.loads(request.content)
        encoded.extend(payload['input'])
        values = [[1, 0] + [0] * 1022 if text.startswith('Instruct:') or 'roll-in shower' in text
                  else [0, 1] + [0] * 1022 for text in payload['input']]
        return httpx.Response(200, json={'embeddings': values})
    store = Store(str(tmp_path / 'integration.db'))
    intent = TravelIntent(origin='Warsaw', destination='Larnaca', hotel_requirements=['душ без порога'],
                          date_from=date(2027, 6, 5), date_to=date(2027, 6, 12))
    place = find_place('Larnaca')
    component = TravelComponent(title='Unknown', description='Unknown',
                               details={'availability_verified': False, 'requirements_verified': False})
    before = constraint_checks(intent, place, (component, component, component))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = Providers(client, store)
        evidence = await provider.search(place, intent)
        assert len(evidence) == 3
        assert 'Access Hotel' in evidence[0].label if enabled else 'Hotel 0' in evidence[0].label
        assert all('Warsaw' not in item.label for item in evidence)
        assert all('не инвентарь' in item.confidence and 'не подтверждены' in item.label for item in evidence)
        await provider.hotel_search_fallback(place, intent, component)
    assert component.title == ('Larnaca Access Hotel' if enabled else 'Larnaca Hotel 0')
    assert component.price is None
    assert component.details['availability_verified'] is False
    assert component.details['requirements_verified'] is False
    assert component.details['property_location_verified'] is False
    assert constraint_checks(intent, place, (component, component, component)) == before
    assert not any('Warsaw Access Hotel' in text for text in encoded)
    assert bool(encoded) is enabled
    store.close()


@pytest.mark.asyncio
async def test_transport_order_and_requirements_never_enter_lodging_encoder(tmp_path, monkeypatch):
    monkeypatch.setenv('EMBEDDING_URL', 'http://embedding:11434')
    monkeypatch.setenv('EMBEDDING_MODEL_DIGEST', 'a' * 64)
    inputs = []
    route = [{'title': f'Warsaw to Larnaca flights {i}', 'url': f'https://example.org/flights/{i}'} for i in range(4)]
    def handler(request):
        if request.url.path == '/search':
            return httpx.Response(200, json={'results': route if 'tickets timetable' in request.url.params['q'] else ROWS})
        if request.url.path == '/api/tags':
            return httpx.Response(200, json={'models': [{'name': 'qwen3-embedding:0.6b', 'digest': 'a' * 64}]})
        batch = json.loads(request.content)['input']
        inputs.extend(batch)
        return httpx.Response(200, json={'embeddings': [[1] + [0] * 1023 for _ in batch]})
    store = Store(str(tmp_path / 'scope.db'))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        evidence = await Providers(client, store).search(find_place('Larnaca'), TravelIntent(
            origin='Warsaw', hotel_requirements=['quiet room'],
            soft_constraints=['transport_requirement:cello in cabin', 'short_flight']))
    assert [row.source_url for row in evidence[3:]] == [row['url'] for row in route[:3]]
    assert not any('cello' in text or 'short_flight' in text or 'flights' in text for text in inputs)
    store.close()


def test_evaluation_metrics_use_full_original_relevance_set_and_zero_answer():
    from scripts.evaluate_embeddings import metrics
    labels = {'a': {'relevance': 2, 'wrong_city': False, 'contradiction': False},
              'b': {'relevance': 2, 'wrong_city': False, 'contradiction': False},
              'c': {'relevance': 0, 'wrong_city': True, 'contradiction': True}}
    result = metrics([{'id': 'a'}, {'id': 'c'}], labels)
    assert result['precision_at_3'] == 1 / 3
    assert result['recall_at_3'] == 0.5  # Removing a relevant result is not free recall.
    assert result['wrong_city_at_3'] == 1 and result['contradiction_at_3'] == 1
    empty = metrics([{'id': 'c'}], {'c': labels['c']})
    assert empty['ndcg_at_3'] is None and empty['recall_at_3'] is None
