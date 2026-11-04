import assert from 'node:assert/strict';
import test from 'node:test';
import { LiveSessionClient } from '../.test-build/live/client.js';
import { isBusy } from '../.test-build/live/state.js';

const trip = (id = 'trip-a', revision = 1, selected = null) => ({ id, revision, selected_option_id: selected, options: [], messages: [], events: [] });
const job = (status, id = 'job-a', kind = 'prices_refresh') => ({ id, session_id: 'trip-a', status, kind });
const snapshot = (sequence, { session = trip(), currentJob = null, reason = 'updated' } = {}) => ({ sequence, session, job: currentJob, server_time: '2026-10-24T12:00:00Z', reason });

class FakeSource {
  readyState = 1;
  closed = false;
  listeners = new Map();
  addEventListener(type, listener) {
    const list = this.listeners.get(type) ?? [];
    list.push(listener); this.listeners.set(type, list);
  }
  emit(type, data = '') { for (const listener of this.listeners.get(type) ?? []) listener({ data: typeof data === 'string' ? data : JSON.stringify(data) }); }
  close() { this.closed = true; this.readyState = 2; }
}

function setup(initial = trip()) {
  const sources = [], updates = [], timers = new Map();
  let now = 0, timerId = 0;
  const transport = {
    createSource(url) { const source = new FakeSource(); sources.push({ source, url }); return source; },
    setTimer(callback, delay) { const id = ++timerId; timers.set(id, { due: now + delay, callback }); return id; },
    clearTimer(id) { timers.delete(id); },
  };
  const advance = duration => {
    const end = now + duration;
    while (true) {
      const next = [...timers].sort((a, b) => a[1].due - b[1].due).find(([, timer]) => timer.due <= end);
      if (!next) break;
      const [id, timer] = next; now = timer.due; timers.delete(id); timer.callback();
    }
    now = end;
  };
  const client = new LiveSessionClient(initial, state => updates.push(state), transport);
  client.start();
  return { client, sources, updates, timers, advance, source: () => sources.at(-1).source };
}

test('complete snapshots deliver progress and selection without itinerary revision changes', () => {
  const { client, source, sources } = setup();
  assert.equal(sources[0].url, '/api/sessions/trip-a/events');
  source().emit('session', snapshot(10, { currentJob: job('running') }));
  assert.equal(client.state.connection, 'live');
  assert.equal(isBusy(client.state), true);
  source().emit('session', snapshot(11, { session: trip('trip-a', 1, 'athens'), currentJob: job('completed') }));
  assert.equal(client.state.session.selected_option_id, 'athens');
  assert.equal(client.state.session.revision, 1);
  assert.equal(isBusy(client.state), false);
});

test('older cursors and another trip cannot regress current state; equal expiry projection is accepted', () => {
  const { client, source } = setup();
  source().emit('session', snapshot(12, { session: trip('trip-a', 5, 'rome') }));
  source().emit('session', snapshot(11, { session: trip('trip-a', 4) }));
  source().emit('session', snapshot(13, { session: trip('trip-b', 7) }));
  assert.equal(client.state.session.selected_option_id, 'rome');
  const stale = { ...trip('trip-a', 5, 'rome'), options: [{ quote: { freshness: 'stale', amount: '262.00', observed_at: 'original-time' } }] };
  source().emit('session', snapshot(12, { session: stale, reason: 'freshness' }));
  assert.deepEqual(client.state.session.options, stale.options);
});

test('terminal before POST response cannot be restored to busy by a late acknowledgement', () => {
  const { client, source } = setup();
  const token = client.beginCommand('prices_refresh', 'athens');
  source().emit('session', snapshot(3, { currentJob: job('completed') }));
  assert.equal(isBusy(client.state), true, 'only the HTTP submission is still pending');
  client.acknowledge(token, 'job-a', 2);
  assert.equal(isBusy(client.state), false);
});

test('POST acknowledgement before SSE keeps controls busy until its durable cursor is observed', () => {
  const { client, source } = setup();
  source().emit('session', snapshot(2, { currentJob: job('completed', 'previous-job') }));
  const token = client.beginCommand('prices_refresh', 'athens');
  client.acknowledge(token, 'job-a', 5);
  assert.equal(isBusy(client.state), true);
  assert.equal(client.state.pending.acceptedSequence, 5);
  source().emit('session', snapshot(4, { currentJob: job('completed', 'previous-job') }));
  assert.equal(isBusy(client.state), true, 'a newer but pre-command snapshot cannot unlock controls');
  source().emit('session', snapshot(5, { currentJob: job('running') }));
  assert.equal(client.state.pending, null);
  assert.equal(isBusy(client.state), true, 'execution remains busy through the streamed job');
  source().emit('session', snapshot(6, { currentJob: job('completed') }));
  assert.equal(isBusy(client.state), false);
});

test('reconnect terminal snapshot crosses the pending acknowledgement barrier without replay', () => {
  const { client, source } = setup();
  source().emit('session', snapshot(2));
  const token = client.beginCommand('message');
  client.acknowledge(token, 'job-a', 3);
  client.retry();
  assert.equal(isBusy(client.state), true);
  source().emit('session', snapshot(8, { currentJob: job('completed', 'job-b', 'message'), reason: 'connected' }));
  assert.equal(client.state.pending, null);
  assert.equal(isBusy(client.state), false, 'latest job may supersede the acknowledged operation');
});

test('POST acknowledgement cannot overwrite a newer job or strand pending state', () => {
  const { client, source } = setup();
  const token = client.beginCommand('prices_refresh');
  source().emit('session', snapshot(5, { currentJob: job('completed', 'job-a') }));
  source().emit('session', snapshot(8, { currentJob: job('failed', 'job-b') }));
  client.acknowledge(token, 'job-a', 4);
  assert.equal(client.state.job.id, 'job-b');
  assert.equal(client.state.job.status, 'failed');
  assert.equal(isBusy(client.state), false);
});

test('HTTP rejection clears submission but leaves a streamed active operation busy', () => {
  const { client, source } = setup();
  const token = client.beginCommand('message');
  source().emit('session', snapshot(2, { currentJob: job('running', 'other-tab-job', 'message') }));
  client.reject(token);
  assert.equal(isBusy(client.state), true);
  source().emit('session', snapshot(3, { currentJob: job('superseded', 'other-tab-job', 'message') }));
  assert.equal(isBusy(client.state), false);
});

test('a response belonging to an earlier command cannot settle the new submission', () => {
  const { client, source } = setup();
  source().emit('session', snapshot(2));
  const previous = client.beginCommand('message');
  client.acknowledge(previous, 'previous-job', 2);
  const current = client.beginCommand('prices_refresh');
  client.acknowledge(previous, 'previous-job', 2);
  client.reject(previous);
  assert.equal(client.state.pending.token, current);
});

test('retry fences late callbacks and preserves readable state and monotonic cursor', () => {
  const { client, source } = setup();
  const old = source();
  old.emit('session', snapshot(20, { session: trip('trip-a', 8, 'prague') }));
  old.readyState = 0; old.emit('error');
  assert.equal(client.state.connection, 'reconnecting');
  assert.equal(client.state.session.selected_option_id, 'prague');
  client.retry();
  assert.equal(old.closed, true);
  old.emit('session', snapshot(99, { session: trip('trip-a', 9, 'old-stream') }));
  source().emit('session', snapshot(19));
  assert.equal(client.state.session.selected_option_id, 'prague');
  source().emit('session', snapshot(21, { session: trip('trip-a', 9, 'berlin') }));
  assert.equal(client.state.session.selected_option_id, 'berlin');
  assert.equal(client.state.connection, 'live');
});

test('fatal CLOSED streams recover with delays capped at four seconds, without polling', () => {
  const { source, sources, advance, timers } = setup();
  for (const delay of [1000, 2000, 4000, 4000]) {
    source().readyState = 2; source().emit('error');
    const before = sources.length;
    assert.equal(timers.size, 1);
    advance(delay - 1); assert.equal(sources.length, before);
    advance(1); assert.equal(sources.length, before + 1);
  }
  assert.equal(sources.every(item => item.url.endsWith('/events')), true);
});

test('native CONNECTING retries do not create duplicate EventSources', () => {
  const { client, source, sources, advance } = setup();
  source().emit('session', snapshot(4));
  source().readyState = 0; source().emit('error');
  advance(2000);
  assert.equal(sources.length, 1);
  source().readyState = 1; source().emit('session', snapshot(8, { session: trip('trip-a', 5) }));
  assert.equal(client.state.connection, 'live');
  assert.equal(client.state.session.revision, 5);
});

test('named heartbeats keep an idle stream alive; silent stream is visibly retried', () => {
  const { client, source, sources, advance } = setup();
  source().emit('session', snapshot(1));
  for (let i = 0; i < 4; i++) { advance(15_000); source().emit('heartbeat', { server_time: 'now' }); }
  assert.equal(sources.length, 1, 'one idle stream, no periodic reads');
  advance(45_000);
  assert.equal(client.state.connection, 'reconnecting');
  assert.equal(source().closed, true);
  advance(1_000);
  assert.equal(sources.length, 2);
});

test('unmount/session switch closes resources and ignores all late callbacks and responses', () => {
  const { client, source, updates, timers, advance, sources } = setup();
  const token = client.beginCommand('prices_refresh');
  const old = source(), before = updates.length;
  client.stop();
  assert.equal(old.closed, true);
  assert.equal(timers.size, 0);
  old.emit('session', snapshot(100)); old.emit('error'); old.emit('heartbeat');
  client.acknowledge(token, 'job-a', 2); client.reject(token); client.retry();
  advance(90_000);
  assert.equal(updates.length, before);
  assert.equal(sources.length, 1);
  const other = setup(trip('trip-b'));
  other.source().emit('session', snapshot(1, { session: trip('trip-b', 2) }));
  old.emit('session', snapshot(101));
  assert.equal(other.client.state.session.id, 'trip-b');
});

test('malformed JSON and invalid cursors cannot replace the public state', () => {
  const { client, source } = setup();
  source().emit('session', snapshot(2));
  for (const invalid of ['{oops', 'null', '{}', snapshot(-1), snapshot(2.5), snapshot(Number.MAX_SAFE_INTEGER + 1)]) source().emit('session', invalid);
  assert.equal(client.state.sequence, 2);
  assert.equal(client.state.session.id, 'trip-a');
});
