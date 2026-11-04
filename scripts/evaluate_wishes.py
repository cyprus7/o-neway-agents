"""Evaluate synthetic wishes through the running app and its shared inference limiter.

No model/embedding key is read here. Exact fields have deterministic assertions;
special-condition paraphrases are explicitly queued for semantic review.
"""
from __future__ import annotations

import argparse
import hashlib
from http.cookiejar import CookieJar
from uuid import uuid4
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.app.geography import find_place, normalized  # noqa: E402
from backend.app.models import TravelIntent  # noqa: E402
from backend.app.planner import COUNTRY_ALIASES  # noqa: E402

SEMANTIC_FIELDS = {'pets', 'hotel_requirements', 'hard_constraints', 'soft_constraints', 'assumptions'}
# Evaluation aliases are explicit, not inferred from an embedding or a country alias.
EVALUATION_CITY_ALIASES = [
    {'турин', 'turin', 'torino'}, {'париж', 'paris'}, {'амстердам', 'amsterdam'},
    {'франкфурт', 'frankfurt', 'frankfurt am main'}, {'вильнюс', 'vilnius'},
    {'санторини', 'santorini'}, {'пальма-де-майорка', 'palma', 'palma de mallorca'},
]


def same_value(actual, expected, field: str) -> bool:
    if field in ('origin', 'destination') and isinstance(actual, str) and isinstance(expected, str):
        def country(value):
            value = normalized(value)
            return next((code for code, aliases in COUNTRY_ALIASES.items() if value in aliases or value == code.casefold()), None)
        actual_country, expected_country = country(actual), country(expected)
        if actual_country or expected_country:
            return actual_country is not None and actual_country == expected_country
        if any(normalized(actual) in aliases and normalized(expected) in aliases for aliases in EVALUATION_CITY_ALIASES):
            return True
        left, right = find_place(actual), find_place(expected)
        return left.name == right.name if left and right else normalized(actual) == normalized(expected)
    if isinstance(expected, list):
        # Compare multisets, preserving repeated child ages and rejecting added travelers.
        return isinstance(actual, list) and sorted(json.dumps(item, sort_keys=True, ensure_ascii=False) for item in actual) == sorted(json.dumps(item, sort_keys=True, ensure_ascii=False) for item in expected)
    return actual == expected


def evaluate_intent(case: dict, intent: dict) -> tuple[list[dict], list[dict]]:
    expected = case['expected']
    errors, review = [], []
    fields = dict(expected.get('fields', {}))
    for field in ('origin', 'destination'):
        if field in expected:
            fields[field] = expected[field]
    for field, value in fields.items():
        if not same_value(intent.get(field), value, field):
            errors.append({'check': field, 'expected': value, 'actual': intent.get(field)})
    banned = expected.get('forbidden_destination', [])
    if isinstance(banned, str):
        banned = [banned]
    for value in banned or []:
        if intent.get('destination') and same_value(intent['destination'], value, 'destination'):
            errors.append({'check': 'forbidden_destination', 'actual': intent['destination']})
    intent_text = json.dumps(intent, ensure_ascii=False).casefold()
    for term in expected.get('forbidden_intent_terms', []):
        if term.casefold() in intent_text:
            errors.append({'check': 'forbidden_intent_term', 'term': term})
    previous = case.get('previous_intent', {})
    for field in expected.get('preserved_fields', []):
        if field in SEMANTIC_FIELDS:
            review.append({'check': 'semantic_preserved:' + field, 'expected': previous.get(field),
                'actual': intent.get(field), 'status': 'review_required'})
            continue
        if not same_value(intent.get(field), previous.get(field), field):
            errors.append({'check': 'preserve:' + field, 'expected': previous.get(field), 'actual': intent.get(field)})
    if expected.get('must_preserve_terms') or expected.get('must_remain_unknown'):
        review.append({'check': 'semantic_preservation_and_unverified_status',
            'terms': expected.get('must_preserve_terms', []),
            'unverified_requirements': expected.get('must_remain_unknown', []),
            'extracted_requirements': {field: intent.get(field) for field in
                ('pets', 'hotel_requirements', 'hard_constraints', 'soft_constraints', 'assumptions')},
            'status': 'review_required', 'reason': 'Exact phrase matching and cosine similarity do not establish semantic entailment.'})
    return errors, review


def evaluate_seed(previous: dict, actual: dict) -> tuple[list[dict], list[dict]]:
    errors, review = [], []
    for field, value in previous.items():
        if field in SEMANTIC_FIELDS:
            # Plain language can be paraphrased. Preserve evidence for human review;
            # do not silently call a failed baseline a successful replanning test.
            review.append({'check': 'seed_semantics:' + field, 'expected': value,
                'actual': actual.get(field), 'status': 'review_required'})
        elif not same_value(actual.get(field), value, field):
            errors.append({'check': 'seed:' + field, 'expected': value, 'actual': actual.get(field)})
    return errors, review


class Runtime:
    def __init__(self, base_url: str, timeout: float = 300):
        parsed = urlparse(base_url)
        if parsed.scheme != 'http' or parsed.hostname not in ('localhost', '127.0.0.1', '::1'):
            raise ValueError('Evaluation is restricted to the local app')
        self.base = base_url.rstrip('/')
        self.timeout = timeout
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))
        self.http_lock = threading.RLock()
        self.origin = f'{parsed.scheme}://{parsed.netloc}'
        self.csrf = None
        self.csrf = self.call('/api/auth/guest', {})['csrf_token']

    def call(self, path, body=None, *, headers=None):
        supplied = {'Content-Type': 'application/json', 'Origin': self.origin}
        if self.csrf is not None:
            supplied['X-CSRF-Token'] = self.csrf
        supplied.update(headers or {})
        request = urllib.request.Request(self.base + path,
            data=None if body is None else json.dumps(body, ensure_ascii=False).encode('utf-8'),
            headers=supplied)
        with self.http_lock, self.opener.open(request, timeout=30) as response:
            return json.load(response)

    def metrics(self):
        try:
            return self.call('/api/metrics')
        except urllib.error.HTTPError as error:
            if error.code != 403:
                raise
            return {'status': 'unavailable', 'reason': 'operator_access_required'}

    def turn(self, session_id, message):
        revision = self.call('/api/sessions/' + session_id)['revision']
        key = 'wishes-' + uuid4().hex
        job = self.call(f'/api/sessions/{session_id}/messages', {'content': message, 'expected_revision': revision}, headers={'Idempotency-Key': key})
        started = time.monotonic()
        while time.monotonic() - started < self.timeout:
            status = self.call('/api/jobs/' + job['job_id'])
            if status['status'] in ('completed', 'failed', 'interrupted', 'canceled', 'superseded'):
                return status, self.call('/api/sessions/' + session_id)
            time.sleep(0.5)
        raise TimeoutError('App job did not finish within the evaluation timeout')

    def case(self, case):
        started = time.monotonic()
        result = {'id': case['id'], 'category': case['category'], 'synthetic': True,
            'layer': 'live_runtime_model_and_providers', 'input_message': case['message'],
            'assertion_errors': [], 'semantic_review': []}
        try:
            session = self.call('/api/sessions', {})
            result['session_id'] = session['id']
            if case.get('previous_intent'):
                seed = 'Создай поездку с этими условиями. Это синтетические данные для тестирования: ' + json.dumps(case['previous_intent'], ensure_ascii=False)
                if case['previous_intent'].get('max_travel_minutes'):
                    seed += f" Обязательное ограничение общего времени в пути: не более {case['previous_intent']['max_travel_minutes']} минут."
                result['seed_input_message'] = seed
                job, seeded = self.turn(session['id'], seed)
                if job['status'] != 'completed':
                    result.update(status='job_failed', stage='seed', error=job.get('error'))
                    return result
                result['seed_intent'] = seeded['intent']
                seed_errors, seed_review = evaluate_seed(case['previous_intent'], seeded['intent'])
                result['seed_semantic_review'] = seed_review
                if seed_errors:
                    result.update(status='seed_assertion_failed', stage='seed', assertion_errors=seed_errors)
                    return result
            job, state = self.turn(session['id'], case['message'])
            result['intent'] = state['intent']
            result['clarification'] = state.get('clarification')
            result['option_count'] = len(state['options'])
            result['options'] = [{key: option[key] for key in
                ('id', 'destination', 'country', 'constraint_checks', 'evidence', 'price_status', 'estimated_total')} for option in state['options']]
            errors, review = evaluate_intent(case, state['intent'])
            # A search-results link must select the option destination, not departure.
            for option in state['options']:
                stay_url = option['stay']['details'].get('booking_search_url', '')
                city = parse_qs(urlparse(stay_url).query).get('ss', [])
                if city and not same_value(city[0], option['destination'], 'destination'):
                    errors.append({'check': 'lodging_link_destination', 'actual': city[0], 'expected': option['destination']})
                if option.get('estimated_total') is not None and any(option[p]['price'] is None for p in ('flight', 'stay', 'transfer')):
                    errors.append({'check': 'invented_total_with_unknown_component'})
                if any(check.get('status') == 'passed' and 'питом' in check.get('constraint', '').lower() for check in option['constraint_checks']):
                    errors.append({'check': 'unverified_pet_policy_passed'})
            result.update(assertion_errors=errors, semantic_review=review,
                status='job_failed' if job['status'] != 'completed' else 'assertion_failed' if errors else 'assertions_passed')
            if job.get('error'):
                result['error'] = job['error']
        except (urllib.error.URLError, TimeoutError, ValueError, KeyError) as exc:
            # Never include access cookies, CSRF or provider data in runner errors.
            result.update(status='runner_error', error=type(exc).__name__)
        finally:
            result['elapsed_seconds'] = round(time.monotonic() - started, 2)
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true', help='Send synthetic requests through the running local app')
    parser.add_argument('--base-url', default='http://127.0.0.1:8080')
    parser.add_argument('--corpus', type=Path, default=ROOT / 'backend/tests/fixtures/travel_wishes_100.json')
    parser.add_argument('--report', type=Path, default=ROOT / 'tmp/wish-evaluation/live.json')
    parser.add_argument('--ids', help='Comma-separated IDs for a targeted rerun')
    parser.add_argument('--resume', action='store_true')
    parser.add_argument('--rescore', action='store_true', help='Re-evaluate saved raw responses with the current oracle, without network calls')
    parser.add_argument('--workers', type=int, choices=(1, 2), default=2)
    parser.add_argument('--context-budget', type=float, help='Supplement missing budgets for a separate E2E run; original corpus stays unchanged')
    args = parser.parse_args()
    data = json.loads(args.corpus.read_text(encoding='utf-8'))
    cases = data if isinstance(data, list) else data['cases']
    assert len(cases) == 100 and len({case['id'] for case in cases}) == 100
    for case in cases:
        assert case.get('synthetic') is True
        if case.get('previous_intent'):
            TravelIntent.model_validate(case['previous_intent'])
    if args.rescore:
        report = json.loads(args.report.read_text(encoding='utf-8'))
        by_id = {case['id']: case for case in cases}
        report.setdefault('initial_summary', report.get('summary'))
        for result in report['results']:
            if 'intent' not in result or result['status'] in ('job_failed', 'runner_error', 'seed_assertion_failed'):
                continue
            result.setdefault('initial_assertion_errors', result['assertion_errors'])
            errors, review = evaluate_intent(by_id[result['id']], result['intent'])
            errors += [error for error in result['initial_assertion_errors'] if error['check'] in
                ('lodging_link_destination', 'invented_total_with_unknown_component', 'unverified_pet_policy_passed')]
            result.update(assertion_errors=errors, semantic_review=review,
                status='assertion_failed' if errors else 'assertions_passed')
        report['oracle_version'] = 2
        report['oracle_note'] = 'Exact city aliases corrected (e.g. Turin/Турин), traveler arrays remain exact; forbidden terms checked; semantic evidence still requires review. No responses regenerated.'
        report['summary'] = {status: sum(item['status'] == status for item in report['results'])
            for status in ('assertions_passed', 'assertion_failed', 'seed_assertion_failed', 'job_failed', 'runner_error')}
        report['summary']['completed_cases'] = len(report['results'])
        report['summary']['semantic_review_required'] = sum(bool(item['semantic_review'] or item.get('seed_semantic_review')) for item in report['results'])
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(report['summary']))
        return
    if not args.live:
        print(json.dumps({'layer': 'corpus_validation_only', 'cases': len(cases), 'live_calls': 0}))
        return
    if args.context_budget is not None:
        if not 0 < args.context_budget <= 100_000_000:
            raise SystemExit('Invalid synthetic context budget')
        cases = json.loads(json.dumps(cases))
        for case in cases:
            if not case.get('previous_intent') and 'budget_total' not in case['expected'].get('fields', {}):
                currency = case['expected'].get('fields', {}).get('currency', 'EUR')
                case['message'] += f' Дополнительный контекст синтетического прогона: общий бюджет на всех {args.context_budget:g} {currency}; остальные условия не меняются.'
    runtime = Runtime(args.base_url)
    config = runtime.call('/api/config')
    if not config.get('llm_configured'):
        raise SystemExit('Runtime model not configured; no live cases executed')
    report = {'synthetic': True, 'created_at': datetime.now(timezone.utc).isoformat(),
        'layer': 'live_runtime_model_and_providers', 'model': config['llm_model'],
        'runtime_limit_per_rolling_minute': config['requests_per_minute'],
        'corpus_sha256': hashlib.sha256(args.corpus.read_bytes()).hexdigest(),
        'context_budget': args.context_budget,
        'embedding': {'status': 'not_used', 'reason': 'Separate credentials absent; embeddings are not a constraint/geography oracle.'},
        'metrics_before': runtime.metrics(), 'results': []}
    if args.resume and args.report.exists():
        report = json.loads(args.report.read_text(encoding='utf-8'))
    if args.ids:
        wanted = set(args.ids.split(','))
        cases = [case for case in cases if case['id'] in wanted]
    done = {item['id'] for item in report['results']}
    pending = [case for case in cases if case['id'] not in done]
    args.report.parent.mkdir(parents=True, exist_ok=True)
    lock = threading.Lock()
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(runtime.case, case) for case in pending]
        for future in as_completed(futures):
            result = future.result()
            with lock:
                report['results'].append(result)
                report['results'].sort(key=lambda item: item['id'])
                report['summary'] = {status: sum(item['status'] == status for item in report['results'])
                    for status in ('assertions_passed', 'assertion_failed', 'seed_assertion_failed', 'job_failed', 'runner_error')}
                report['summary']['completed_cases'] = len(report['results'])
                report['summary']['semantic_review_required'] = sum(bool(item['semantic_review'] or item.get('seed_semantic_review')) for item in report['results'])
                temp = args.report.with_suffix('.tmp')
                temp.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
                temp.replace(args.report)
            print(json.dumps({'case': result['id'], 'status': result['status'], 'done': len(report['results']),
                'errors': result['assertion_errors']}, ensure_ascii=True), flush=True)
    report['metrics_after'] = runtime.metrics()
    report['finished_at'] = datetime.now(timezone.utc).isoformat()
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report['summary'], ensure_ascii=True), flush=True)


if __name__ == '__main__':
    main()
