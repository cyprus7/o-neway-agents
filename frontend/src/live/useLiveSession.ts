import { useEffect, useRef, useState } from 'react';
import type { Job, Session } from '../types';
import { LiveSessionClient } from './client';
import { initialLiveState, isBusy, isJobActive } from './state';
import type { LiveState } from './state';

type CommandHandle = { owner: LiveSessionClient; token: number };

export function useLiveSession(initial: Session | null) {
  const [view, setView] = useState<LiveState | null>(null);
  const clientRef = useRef<LiveSessionClient | null>(null);
  const sessionId = initial?.id;
  useEffect(() => {
    if (!initial) return;
    const client = new LiveSessionClient(initial, next => {
      if (clientRef.current === client) setView(next);
    });
    clientRef.current = client;
    setView(client.state);
    client.start();
    return () => { client.stop(); if (clientRef.current === client) clientRef.current = null; };
    // Bootstrap data is immutable for a trip; all later state comes from its stream.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessionId]);
  const state = initial ? view?.session.id === initial.id ? view : initialLiveState(initial) : null;
  const currentClient = () => clientRef.current?.state.session.id === sessionId ? clientRef.current : null;
  return {
    activityState: state,
    session: state?.session ?? null,
    job: state?.job ?? null,
    busy: state ? isBusy(state) : false,
    connection: state?.connection ?? 'connecting',
    workKind: (state?.pending?.kind ?? (isJobActive(state?.job ?? null) ? state?.job?.kind : null)) === 'prices_refresh' ? 'prices' as const : 'planning' as const,
    retry: () => currentClient()?.retry(),
    beginCommand: (kind: Job['kind'], optionId?: string): CommandHandle | null => {
      const owner = currentClient();
      return owner ? { owner, token: owner.beginCommand(kind, optionId) } : null;
    },
    acknowledge: (command: CommandHandle, jobId: string, sequence: number) => command.owner.acknowledge(command.token, jobId, sequence),
    reject: (command: CommandHandle) => command.owner.reject(command.token),
  };
}
