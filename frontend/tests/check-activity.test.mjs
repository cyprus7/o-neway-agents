import assert from 'node:assert/strict';
import test from 'node:test';
import { checkActivity, conditionSummary } from '../.test-build/live/checkActivity.js';
import { acknowledgeCommand, receiveSnapshot } from '../.test-build/live/state.js';

const session = (revision = 1, ids = ['r1-a', 'r1-b']) => ({ id: 'trip-a', revision, options: ids.map(id => ({ id })) });
const job = (status, changes = {}) => ({ id: 'refresh-a', session_id: 'trip-a', kind: 'prices_refresh', status,
  captured_revision: 1, result_revision: null, target_option_ids: ['r1-a'], result_option_ids: [], ...changes });
const view = (changes = {}) => ({ session: session(), job: null, pending: null, connection: 'live', sequence: 10, ...changes });
const pending = (changes = {}) => ({ token: 1, kind: 'prices_refresh', optionId: 'r1-a', revision: 1,
  startedAfterSequence: 10, previousJobId: null, ...changes });

test('idle unknown evidence is not a running job', () => {
  const activity = checkActivity(view(), 'r1-a');
  assert.equal(activity.phase, 'idle');
  assert.equal(activity.active, false);
  assert.equal(conditionSummary([{ status: 'unknown' }, { status: 'unknown' }, { status: 'unknown' }]).label, '3 условия требуют проверки');
});

test('pending single/all refresh starts immediately only in its captured route scope', () => {
  const state = view({ pending: pending() });
  assert.equal(checkActivity(state, 'r1-a').phase, 'submitting');
  assert.equal(checkActivity(state, 'r1-b').phase, 'idle');
  assert.equal(checkActivity(view({ pending: pending({ optionId: undefined }) }), 'r1-b').phase, 'submitting');
  assert.equal(checkActivity(view({ pending: pending({ revision: 0 }) }), 'r1-a').phase, 'idle');
  assert.equal(checkActivity(state, 'missing-route').phase, 'idle');
});

test('accepted pending waits for committed state without claiming it is queued', () => {
  const result = checkActivity(view({ pending: pending({ jobId: 'refresh-a', acceptedSequence: 11 }) }), 'r1-a');
  assert.equal(result.phase, 'submitting');
  assert.match(result.label, /принят/);
});

test('queued/running state is authoritative across reload and another view', () => {
  for (const phase of ['queued', 'running']) {
    const state = view({ job: job(phase), pending: null });
    assert.equal(checkActivity(state, 'r1-a').phase, phase);
    assert.equal(checkActivity(state, 'r1-a').active, true);
    assert.equal(checkActivity(state, 'r1-b').phase, 'idle');
    assert.equal(checkActivity(state).phase, phase);
  }
  assert.equal(checkActivity(view({ job: job('running', { target_option_ids: ['r1-a', 'r1-b'] }) }), 'r1-b').phase, 'running');
});

test('success uses only result IDs at result revision even though every option ID changed', () => {
  const state = view({ session: session(2, ['r2-a', 'r2-b']), job: job('completed', { result_revision: 2, result_option_ids: ['r2-a'] }) });
  assert.equal(checkActivity(state, 'r2-a').phase, 'completed');
  assert.equal(checkActivity(state, 'r2-a').active, false);
  assert.equal(checkActivity(state, 'r2-b').phase, 'idle');
  assert.equal(checkActivity(view({ ...state, session: session(3, ['r2-a', 'r2-b']) }), 'r2-a').phase, 'idle');
});

test('failed/superseded outcomes stop animation and do not leak to a later revision', () => {
  for (const phase of ['failed', 'superseded']) {
    const state = view({ job: job(phase, { error: 'Synthetic failure' }) });
    assert.equal(checkActivity(state, 'r1-a').phase, phase);
    assert.equal(checkActivity(state, 'r1-a').active, false);
    assert.equal(checkActivity(state, 'r1-b').phase, 'idle');
    assert.equal(checkActivity({ ...state, session: session(2) }, 'r1-a').phase, 'idle');
  }
});

test('other sessions, message jobs, stale revisions and legacy unscoped jobs remain idle per route', () => {
  for (const current of [job('running', { session_id: 'trip-b' }), job('running', { kind: 'message' }),
    job('running', { captured_revision: 0 }), job('running', { target_option_ids: [] }),
    job('running', { target_option_ids: undefined }), job('completed', { result_revision: 1, result_option_ids: [] })]) {
    assert.equal(checkActivity(view({ job: current }), 'r1-a').phase, 'idle');
  }
});

test('disconnection shows last known work without claiming active source work', () => {
  for (const connection of ['connecting', 'reconnecting']) {
    for (const status of ['queued', 'running']) {
      const result = checkActivity(view({ connection, job: job(status) }), 'r1-a');
      assert.equal(result.phase, 'recovering');
      assert.equal(result.active, false);
      assert.match(result.detail, /Последнее/);
    }
  }
  const completed = checkActivity(view({ connection: 'reconnecting', job: job('completed', { result_revision: 1, result_option_ids: ['r1-a'] }) }), 'r1-a');
  assert.equal(completed.phase, 'completed');
  assert.equal(completed.active, false);
  assert.equal(completed.connectionLost, true);
});

test('terminal SSE before POST acknowledgement wins and late acknowledgement cannot resurrect work', () => {
  const initial = view({ pending: pending() });
  const updated = receiveSnapshot(initial, { sequence: 12, session: session(), job: job('failed'), server_time: 'now', reason: 'updated' });
  assert.notEqual(updated.pending, null, 'the HTTP response has not arrived');
  assert.equal(checkActivity(updated, 'r1-a').phase, 'failed');
  const accepted = acknowledgeCommand(updated, 1, 'refresh-a', 11);
  assert.equal(checkActivity(accepted, 'r1-a').phase, 'failed');
  assert.equal(checkActivity(accepted, 'r1-a').active, false);
});

test('a new request does not inherit the previous terminal job badge', () => {
  const previous = job('completed', { id: 'previous', result_revision: 1, result_option_ids: ['r1-a'] });
  const result = checkActivity(view({ job: previous, pending: pending({ previousJobId: 'previous' }) }), 'r1-a');
  assert.equal(result.phase, 'submitting');
  const committed = checkActivity(view({ sequence: 11, job: job('queued'), pending: pending({ previousJobId: 'previous' }) }), 'r1-a');
  assert.equal(committed.phase, 'queued');
});

test('condition counts preserve unknown/failed states and Russian grammar', () => {
  for (const [count, text] of [[1, '1 условие требует проверки'], [2, '2 условия требуют проверки'], [5, '5 условий требуют проверки'], [11, '11 условий требуют проверки'], [21, '21 условие требует проверки']]) {
    assert.equal(conditionSummary(Array.from({ length: count }, () => ({ status: 'unknown' }))).label, text);
  }
  assert.equal(conditionSummary([{ status: 'failed' }, { status: 'passed' }]).label, '1 условие не выполняется');
  assert.equal(conditionSummary([{ status: 'unknown' }, { status: 'failed' }]).failed, 1);
  assert.equal(conditionSummary([]).label, 'Сведения об условиях пока не получены');
  assert.equal(conditionSummary([{ status: 'unexpected' }]).unknown, 1);
});
