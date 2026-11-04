import type { Job, Session } from '../types.js';

export type ConnectionStatus = 'connecting' | 'live' | 'reconnecting';
export type SessionSnapshot = {
  sequence: number;
  session: Session;
  job: Job | null;
  server_time: string;
  reason: 'connected' | 'updated' | 'freshness';
};
export type PendingCommand = { token: number; kind: Job['kind']; optionId?: string; jobId?: string; acceptedSequence?: number; revision: number; startedAfterSequence: number; previousJobId: string | null };
export type LiveState = {
  session: Session;
  job: Job | null;
  sequence: number;
  connection: ConnectionStatus;
  pending: PendingCommand | null;
};

export const isJobActive = (job: Job | null) => job?.status === 'queued' || job?.status === 'running';
export const isBusy = (state: LiveState) => state.pending !== null || isJobActive(state.job);
export const initialLiveState = (session: Session): LiveState => ({ session, job: null, sequence: -1, connection: 'connecting', pending: null });

/** The stream owns trip state. Equal cursors may contain a newer freshness projection. */
export function receiveSnapshot(state: LiveState, snapshot: SessionSnapshot): LiveState {
  if (snapshot.session.id !== state.session.id || !Number.isSafeInteger(snapshot.sequence) || snapshot.sequence < 0 || snapshot.sequence < state.sequence) return state;
  return {
    session: snapshot.session,
    job: snapshot.job,
    sequence: snapshot.sequence,
    connection: 'live',
    pending: state.pending?.acceptedSequence !== undefined && snapshot.sequence >= state.pending.acceptedSequence ? null : state.pending,
  };
}

/**
 * The acknowledgement is a durable cursor barrier, never a replacement job/session.
 * Keep controls busy until SSE catches up, including a terminal event received before POST.
 */
export function acknowledgeCommand(state: LiveState, token: number, jobId: string, sequence: number): LiveState {
  if (state.pending?.token !== token) return state;
  if (!Number.isSafeInteger(sequence) || sequence < 0) return state;
  return { ...state, pending: state.sequence >= sequence ? null : { ...state.pending, jobId, acceptedSequence: sequence } };
}
