"""Actual local encoder evaluation. Labels stay outside every ranking/encoding input."""
import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import statistics
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx

from backend.app.geography import find_place
from backend.app.providers import lodging_result_matches, public_url
from backend.app.retrieval import LocalRanker, document_text, score_rows
from backend.app.storage import Store


def lexical_order(rows, query):
    """Untranslated word-overlap baseline; no relevance labels or fixture-specific words."""
    tokens = set(re.findall(r"\w{3,}", query.casefold()))
    return sorted(rows, key=lambda row: -len(tokens & set(re.findall(r"\w{3,}", document_text(row).casefold()))))


def metrics(ranking, labels):
    grades = [labels[row['id']]['relevance'] for row in ranking[:3]]
    ideal = sorted((row['relevance'] for row in labels.values()), reverse=True)[:3]
    def dcg(values):
        return sum((2 ** value - 1) / math.log2(i + 2) for i, value in enumerate(values))
    ideal_dcg = dcg(ideal)
    complete = {id_ for id_, row in labels.items() if row['relevance'] == 2}
    top = [row['id'] for row in ranking[:3]]
    return {
        'ndcg_at_3': dcg(grades) / ideal_dcg if ideal_dcg else None,
        'precision_at_3': sum(grade >= 1 for grade in grades) / 3,
        'recall_at_3': len(complete.intersection(top)) / len(complete) if complete else None,
        'mrr_at_3': next((1 / (i + 1) for i, id_ in enumerate(top) if id_ in complete), 0) if complete else None,
        'complete_top_1': int(bool(top) and top[0] in complete) if complete else None,
        'wrong_city_at_3': sum(labels[id_]['wrong_city'] for id_ in top),
        'contradiction_at_3': sum(labels[id_]['contradiction'] for id_ in top),
        'contradiction_top_1': int(bool(top) and labels[top[0]]['contradiction']),
        'ranking': [row['id'] for row in ranking],
    }


def summarize(cases, variant):
    values = [case['methods'][variant] for case in cases]
    result = {'cases': len(values), 'zero_relevance_cases': sum(v['ndcg_at_3'] is None for v in values)}
    for key in ('ndcg_at_3', 'precision_at_3', 'recall_at_3', 'mrr_at_3', 'complete_top_1'):
        valid = [v[key] for v in values if v[key] is not None]
        result[key] = round(statistics.mean(valid), 6) if valid else None
    for key in ('wrong_city_at_3', 'contradiction_at_3', 'contradiction_top_1'):
        result[key] = sum(v[key] for v in values)
    return result


async def evaluate(args):
    fixture_bytes = args.fixture.read_bytes()
    fixture = json.loads(fixture_bytes)
    source_wishes = json.loads((Path(__file__).resolve().parents[1] / fixture['source_corpus']).read_text(encoding='utf-8'))
    known_ids = {wish['id'] for wish in source_wishes}
    assert fixture['synthetic'] and len(fixture['cases']) >= 40
    assert all(set(case['wish_ids']) <= known_ids for case in fixture['cases'])
    dev_ids = {id_ for case in fixture['cases'] if case['split'] == 'dev' for id_ in case['wish_ids']}
    test_ids = {id_ for case in fixture['cases'] if case['split'] == 'test' for id_ in case['wish_ids']}
    assert dev_ids.isdisjoint(test_ids)
    selected = [case for case in fixture['cases'] if args.split == 'all' or case['split'] == args.split]
    store = Store(str(args.cache))
    async with httpx.AsyncClient(follow_redirects=False) as client:
        ranker = LocalRanker(client, store)
        tags = (await client.get(os.environ['EMBEDDING_URL'].rstrip('/') + '/api/tags', timeout=5)).json()
        model = next(model for model in tags['models'] if model['name'] == os.environ['EMBEDDING_MODEL'])
        assert model['digest'] == os.environ['EMBEDDING_MODEL_DIGEST'], 'Model identity mismatch'
        report = {
            'created_at': datetime.now(timezone.utc).isoformat(),
            'synthetic': True, 'fixture_sha256': hashlib.sha256(fixture_bytes).hexdigest(),
            'label_revision': fixture['label_revision'], 'split': args.split, 'variant': args.variant,
            'ranker_sha256': hashlib.sha256((Path(__file__).resolve().parents[1] / 'backend/app/retrieval.py').read_bytes()).hexdigest(),
            'model': model, 'note': 'Offline source relevance and separately observed whole-deadline runtime ranking. Not verified inventory.',
            'cases': [],
        }
        if args.report.exists():
            raise ValueError('Refusing to overwrite an earlier report')
        args.report.parent.mkdir(parents=True, exist_ok=True)
        for case in selected:
            # IDs are opaque identifiers only. Never pass labels/grounds/split/flags to a scorer.
            rows = [{key: candidate[key] for key in ('id', 'title', 'content', 'url')} for candidate in case['candidates']]
            labels = {candidate['id']: candidate for candidate in case['candidates']}
            place = find_place(case['destination'])
            assert place, 'Benchmark needs an actual supported destination'
            eligible = [row for row in rows if public_url(row['url']) and lodging_result_matches(row, place, case['origin'])]
            counters_before = store.metrics()
            runtime_started = time.perf_counter()
            runtime_ranking = await ranker.rank_rows(eligible, case['query'], variant=args.variant)
            runtime_seconds = time.perf_counter() - runtime_started
            counters_after = store.metrics()
            runtime_fallback = counters_after.get('embedding_fallback', 0) > counters_before.get('embedding_fallback', 0)
            began = time.perf_counter()
            q_vector = (await ranker.embeddings([case['query']], role='query'))[0]
            # Loop in max4-input batches so each warm operation has its own bounded deadline.
            documents = []
            for offset in range(0, len(eligible), 4):
                documents.extend(await ranker.embeddings([document_text(row) for row in eligible[offset:offset+4]], role='document'))
            elapsed = time.perf_counter() - began
            semantic = score_rows(eligible, case['query'], q_vector, documents, variant='semantic')
            variants = {'provider_order': eligible, 'lexical': lexical_order(eligible, case['query']),
                        'semantic': semantic, 'runtime': runtime_ranking}
            if args.variant != 'semantic':
                variants[args.variant] = score_rows(eligible, case['query'], q_vector, documents, variant=args.variant)
            result = {'id': case['id'], 'wish_ids': case['wish_ids'], 'split': case['split'],
                      'query': case['query'], 'eligible_count': len(eligible), 'original_count': len(rows),
                      'encoding_seconds': round(elapsed, 4),
                      'runtime_seconds': round(runtime_seconds, 4), 'runtime_fallback': runtime_fallback,
                      'runtime_deadline_exceeded': runtime_seconds > 8.2,
                      'methods': {key: metrics(ranking, labels) for key, ranking in variants.items()}}
            report['cases'].append(result)
            report['summary'] = {split: {variant: summarize([row for row in report['cases'] if split == 'all' or row['split'] == split], variant)
                                         for variant in variants} for split in ('all', 'dev', 'test')}
            report['cache_metrics'] = store.metrics()
            args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
            print(json.dumps({'case': case['id'], 'runtime_seconds': result['runtime_seconds'],
                              'runtime_fallback': runtime_fallback}, ensure_ascii=False), flush=True)
        latencies = [row['encoding_seconds'] for row in report['cases']]
        report['timing'] = {'mean_case_encoding_seconds': statistics.mean(latencies), 'max_case_encoding_seconds': max(latencies)}
        report['timing'].update({'mean_runtime_seconds': statistics.mean(row['runtime_seconds'] for row in report['cases']),
                                'max_runtime_seconds': max(row['runtime_seconds'] for row in report['cases']),
                                'runtime_fallbacks': sum(row['runtime_fallback'] for row in report['cases']),
                                'runtime_deadline_violations': sum(row['runtime_deadline_exceeded'] for row in report['cases'])})
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps({'summary': report['summary'], 'timing': report['timing']}, ensure_ascii=False), flush=True)
    store.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fixture', type=Path, default=Path('backend/tests/fixtures/embedding_relevance.json'))
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--cache', type=Path, default=Path('/tmp/embedding-evaluation.sqlite3'))
    parser.add_argument('--split', choices=('dev', 'test', 'all'), default='dev')
    parser.add_argument('--variant', choices=('semantic', 'hybrid'), default='semantic')
    asyncio.run(evaluate(parser.parse_args()))
