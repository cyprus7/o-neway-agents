"""Black-box checks; --live-llm explicitly enables three billable/quota-counting turns."""
import argparse
import json
from http.cookiejar import CookieJar
from urllib.parse import urlsplit
from uuid import uuid4
import time
import urllib.error
import urllib.request
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--base-url', default='http://127.0.0.1:8080')
    parser.add_argument('--live-llm', action='store_true')
    args = parser.parse_args()
    base = args.base_url.rstrip('/')

    parsed = urlsplit(base)
    origin = f'{parsed.scheme}://{parsed.netloc}'
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))
    csrf = None

    def call(path, body=None, *, headers=None):
        supplied = {'Content-Type': 'application/json', 'Origin': origin}
        if csrf is not None:
            supplied['X-CSRF-Token'] = csrf
        supplied.update(headers or {})
        request = urllib.request.Request(base + path, data=None if body is None else json.dumps(body).encode(), headers=supplied)
        with opener.open(request, timeout=30) as response:
            return json.load(response)

    def turn(session_id, content):
        started = time.monotonic()
        revision = call('/api/sessions/' + session_id)['revision']
        key = 'smoke-' + uuid4().hex
        job = call(f'/api/sessions/{session_id}/messages', {'content': content, 'expected_revision': revision}, headers={'Idempotency-Key': key})
        while time.monotonic() - started < 240:
            state = call('/api/jobs/' + job['job_id'])
            if state['status'] in ('failed', 'interrupted', 'canceled', 'superseded'):
                raise AssertionError(state.get('error', 'Job failed'))
            if state['status'] == 'completed':
                return call('/api/sessions/' + session_id), round(time.monotonic() - started, 2)
            time.sleep(1)
        raise AssertionError('Planning timed out')

    health = call('/health')
    assert health['status'] == 'ok', health
    config = call('/api/config')
    csrf = call('/api/auth/guest', {})['csrf_token']
    session = call('/api/sessions', {})
    assert call('/api/sessions/' + session['id'])['id'] == session['id']
    results = {'health': 'passed', 'sessions': 'passed', 'llm_configured': config['llm_configured']}
    if args.live_llm:
        first, elapsed = turn(session['id'], 'Из Варшавы к тёплому морю в конце ноября 2026 на 7–9 ночей. Двое взрослых и ребёнок 6 лет, до 2500 евро на всех. Предпочитаем короткий перелёт.')
        intent = first['intent']
        assert intent['origin'] and intent['adults'] == 2 and intent['children_ages'] == [6], intent
        assert intent['budget_total'] == 2500 and intent['currency'] == 'EUR', intent
        assert intent['max_travel_minutes'] is None, 'Soft short-flight preference became a hard duration limit'
        assert len(first['options']) == 3, first.get('clarification')
        assert all(option['evidence'] for option in first['options'])
        for option in first['options']:
            if any(option[part]['price'] is None for part in ('flight', 'stay', 'transfer')):
                assert option['estimated_total'] is None
            if option['weather'] and option['weather'].get('kind') == 'historical':
                assert 'НЕ прогноз' in option['weather']['summary']
        option_id = first['options'][0]['id']
        try:
            call(f'/api/sessions/{session["id"]}/checkout', {'option_id': option_id})
        except urllib.error.HTTPError as error:
            assert error.code == 409
        else:
            raise AssertionError('Checkout did not require a selected option')
        chosen = call(f'/api/sessions/{session["id"]}/select', {'option_id': option_id})
        assert chosen['selected_option_id'] == option_id
        checkout = call(f'/api/sessions/{session["id"]}/checkout', {'option_id': option_id})
        assert checkout['status'] == 'simulated'
        assert call(f'/api/sessions/{session["id"]}/checkout', {'option_id': option_id})['id'] == checkout['id']
        second, replan_elapsed = turn(session['id'], 'Бюджет теперь 2000 евро, и берём с собой собаку. Остальное оставь как было.')
        assert second['revision'] > first['revision']
        assert second['intent']['budget_total'] == 2000 and second['intent']['pets']
        for field in ('origin', 'date_from', 'date_to', 'adults', 'children_ages'):
            assert second['intent'][field] == first['intent'][field], field
        assert all(any(check['status'] == 'unknown' and 'питом' in check['constraint'].lower() for check in option['constraint_checks']) for option in second['options'])
        try:
            call(f'/api/sessions/{session["id"]}/checkout', {'option_id': option_id})
        except urllib.error.HTTPError as error:
            assert error.code in (400, 409, 422)
        else:
            raise AssertionError('Stale checkout was accepted')
        missing = call('/api/sessions', {})
        clarification, clarification_elapsed = turn(missing['id'], 'Хочется к морю, помоги выбрать.')
        assert clarification['clarification'] and not clarification['options']
        results.update({'planning': 'passed', 'planning_seconds': elapsed, 'replan': 'passed', 'replan_seconds': replan_elapsed, 'clarification': 'passed', 'clarification_seconds': clarification_elapsed, 'soft_duration_guard': 'passed', 'simulated_checkout': 'passed', 'checkout_selection_required': True, 'checkout_idempotent': True, 'stale_checkout_rejected': True, 'session_id': session['id'], 'options': [o['destination'] for o in second['options']], 'evidence_count': sum(len(o['evidence']) for o in second['options']), 'stay_candidates': [o['stay']['title'] for o in second['options']]})
        out = Path(__file__).resolve().parents[1] / 'tmp' / 'smoke'
        out.mkdir(parents=True, exist_ok=True)
        (out / 'session.json').write_text(json.dumps(second, ensure_ascii=False, indent=2), encoding='utf-8')
    try:
        results['metrics'] = call('/api/metrics')
    except urllib.error.HTTPError as error:
        assert error.code == 403
        results['metrics'] = {'status': 'unavailable', 'reason': 'operator_access_required'}
    output = Path(__file__).resolve().parents[1] / 'tmp' / 'smoke'
    output.mkdir(parents=True, exist_ok=True)
    (output / 'results.json').write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding='utf-8')
    # Safe in Windows cp1251 consoles, including international property names.
    print(json.dumps(results, ensure_ascii=True, indent=2))


if __name__ == '__main__':
    main()
