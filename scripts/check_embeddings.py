"""Local encoder + provider fixture smoke, without external search or LLM calls."""
import argparse
import asyncio
import json
import math
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx

from backend.app.geography import find_place
from backend.app.models import TravelComponent, TravelIntent
from backend.app.planner import constraint_checks
from backend.app.providers import Providers, lodging_result_matches
from backend.app.retrieval import lodging_query
from backend.app.storage import Store


async def check(args):
    fixture = json.loads(Path('backend/tests/fixtures/embedding_relevance.json').read_text(encoding='utf-8'))
    case = fixture['cases'][0]
    rows = [{key: row[key] for key in ('title', 'url', 'content')} for row in case['candidates']]
    place = find_place(case['destination'])
    intent = TravelIntent(origin=case['origin'], destination=case['destination'], hotel_requirements=[case['query']])
    store = Store(':memory:')
    async with httpx.AsyncClient(follow_redirects=False) as client:
        provider = Providers(client, store)
        async def synthetic_search(*_args, **_kwargs):
            return {'results': rows}, 'snapshot', '2026-10-24T12:00:00+00:00'
        provider.cached_get = synthetic_search
        result = {'synthetic': True, 'external_llm_calls': 0}
        if args.expect == 'enabled':
            start = time.perf_counter()
            vectors = await provider.ranker.embeddings([case['query']], role='query')
            result.update({'encoding_seconds': round(time.perf_counter() - start, 4),
                           'dimensions': len(vectors[0]), 'l2_norm': math.hypot(*vectors[0])})
            assert result['dimensions'] == 1024 and abs(result['l2_norm'] - 1) < 1e-5
        eligible = [row for row in rows if lodging_result_matches(row, place, intent.origin)]
        start = time.perf_counter()
        ranked = await provider.ranker.rank_rows(eligible, lodging_query(intent))
        result['ranking_seconds'] = round(time.perf_counter() - start, 4)
        if args.expect != 'enabled':
            assert ranked == eligible
            assert result['ranking_seconds'] <= 8.2
            counters = store.metrics()
            assert counters.get('embedding_rankings', 0) == 0
            if args.expect == 'fallback':
                assert provider.ranker.configured
                assert counters.get('embedding_fallback', 0) >= 1
            else:
                assert not provider.ranker.url
                assert counters.get('embedding_disabled', 0) >= 1
        else:
            assert store.metrics().get('embedding_rankings', 0) >= 1
            assert store.metrics().get('embedding_fallback', 0) == 0
        evidence = await provider.search(place, intent)
        assert all('Warsaw' not in row.label for row in evidence)
        assert all('не подтверждены' in row.label for row in evidence)
        component = TravelComponent(title='Synthetic unknown', description='Fixture, not inventory')
        checks = constraint_checks(intent, place, (component, component, component))
        assert component.price is None
        assert not any(row['status'] == 'passed' and case['query'] in row['constraint'] for row in checks)
        result.update({'evidence_count': len(evidence), 'wrong_city_sources': 0, 'price': None,
                       'mode': args.expect, 'metrics': store.metrics(), 'unknown_claims_preserved': True})
        print(json.dumps(result, ensure_ascii=False))
    store.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--expect', choices=('enabled', 'fallback', 'disabled'), default='enabled')
    asyncio.run(check(parser.parse_args()))
