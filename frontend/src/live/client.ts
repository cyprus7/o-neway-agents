import type { Job, Session } from '../types.js';
import { acknowledgeCommand, initialLiveState, receiveSnapshot } from './state.js';
import type { ConnectionStatus, LiveState, SessionSnapshot } from './state.js';

interface EventSourcePort {
  readonly readyState: number;
  addEventListener(type: string, listener: EventListener): void;
  close(): void;
}
type Timer = unknown;
export interface LiveTransport {
  createSource(url: string): EventSourcePort;
  setTimer(callback: () => void, delay: number): Timer;
  clearTimer(timer: Timer): void;
}

const browserTransport: LiveTransport = {
  createSource: url => new EventSource(url),
  setTimer: (callback, delay) => window.setTimeout(callback, delay),
  clearTimer: timer => window.clearTimeout(timer as number),
};
const WATCHDOG_MS = 45_000;

/** Owns one trip's stream. Transport generations fence late callbacks after close/retry. */
export class LiveSessionClient {
  state: LiveState;
  private source: EventSourcePort | null = null;
  private generation = 0;
  private stopped = false;
  private reconnectAttempt = 0;
  private retryTimer: Timer | null = null;
  private watchdog: Timer | null = null;
  private commandSequence = 0;

  constructor(session: Session, private readonly changed: (state: LiveState) => void, private readonly transport: LiveTransport = browserTransport) {
    this.state = initialLiveState(session);
  }

  start() { this.connect(); }
  stop() { this.stopped = true; this.closeConnection(); }
  retry() {
    if (this.stopped) return;
    this.reconnectAttempt = 0;
    this.connect();
  }
  beginCommand(kind: Job['kind'], optionId?: string): number {
    const token = ++this.commandSequence;
    this.update({ ...this.state, pending: { token, kind, optionId, revision: this.state.session.revision,
      startedAfterSequence: this.state.sequence, previousJobId: this.state.job?.id ?? null } });
    return token;
  }
  acknowledge(token: number, jobId: string, sequence: number) { this.update(acknowledgeCommand(this.state, token, jobId, sequence)); }
  reject(token: number) {
    if (this.state.pending?.token === token) this.update({ ...this.state, pending: null });
  }

  private update(state: LiveState) {
    if (this.stopped || state === this.state) return;
    this.state = state;
    this.changed(state);
  }
  private status(connection: ConnectionStatus) {
    if (this.state.connection !== connection) this.update({ ...this.state, connection });
  }
  private clearTimer(timer: Timer | null) { if (timer !== null) this.transport.clearTimer(timer); }
  private closeConnection() {
    ++this.generation;
    this.source?.close();
    this.source = null;
    this.clearTimer(this.retryTimer);
    this.clearTimer(this.watchdog);
    this.retryTimer = this.watchdog = null;
  }
  private connect() {
    if (this.stopped) return;
    this.closeConnection();
    this.status(this.state.sequence < 0 && this.reconnectAttempt === 0 ? 'connecting' : 'reconnecting');
    const generation = this.generation;
    const current = () => !this.stopped && generation === this.generation;
    let source: EventSourcePort;
    try { source = this.transport.createSource(`/api/sessions/${encodeURIComponent(this.state.session.id)}/events`); }
    catch { this.scheduleReconnect(); return; }
    this.source = source;
    this.armWatchdog(generation);
    source.addEventListener('session', event => {
      if (!current()) return;
      let snapshot: SessionSnapshot;
      try {
        snapshot = JSON.parse((event as MessageEvent<string>).data) as SessionSnapshot;
        if (!snapshot.session || !('job' in snapshot) || !snapshot.server_time) return;
      } catch { return; }
      const next = receiveSnapshot(this.state, snapshot);
      if (next === this.state) return;
      this.reconnectAttempt = 0;
      this.armWatchdog(generation);
      this.update(next);
    });
    source.addEventListener('heartbeat', () => {
      if (current()) this.armWatchdog(generation);
    });
    source.addEventListener('error', () => {
      if (!current()) return;
      this.status('reconnecting');
      // CONNECTING is native EventSource retry. CLOSED needs an explicit new instance.
      if (source.readyState === 2) this.scheduleReconnect();
    });
  }
  private armWatchdog(generation: number) {
    this.clearTimer(this.watchdog);
    this.watchdog = this.transport.setTimer(() => {
      if (!this.stopped && generation === this.generation) this.scheduleReconnect();
    }, WATCHDOG_MS);
  }
  private scheduleReconnect() {
    if (this.stopped) return;
    this.closeConnection();
    this.status('reconnecting');
    const delay = Math.min(1_000 * 2 ** this.reconnectAttempt++, 4_000);
    this.retryTimer = this.transport.setTimer(() => { this.retryTimer = null; this.connect(); }, delay);
  }
}
