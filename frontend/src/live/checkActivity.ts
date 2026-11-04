import type { LiveState } from './state.js';

export type CheckPhase = 'idle' | 'submitting' | 'queued' | 'running' | 'completed' | 'failed' | 'superseded' | 'canceled' | 'interrupted' | 'recovering';
export type CheckActivityState = { phase: CheckPhase; active: boolean; label: string; detail: string; connectionLost: boolean };

const activePhases = new Set<CheckPhase>(['submitting', 'queued', 'running']);

/** Unknown evidence is independent of a refresh job's lifecycle. */
export function conditionSummary(conditions: Array<Record<string, unknown>>) {
  const unknown = conditions.filter(condition => condition.status !== 'passed' && condition.status !== 'failed').length;
  const failed = conditions.filter(condition => condition.status === 'failed').length;
  const noun = (count: number) => count % 100 >= 11 && count % 100 <= 14 ? 'условий' : count % 10 === 1 ? 'условие' : count % 10 >= 2 && count % 10 <= 4 ? 'условия' : 'условий';
  const unknownLabel = `${unknown} ${noun(unknown)} ${noun(unknown) === 'условие' ? 'требует' : 'требуют'} проверки`;
  const failedLabel = `${failed} ${noun(failed)} ${noun(failed) === 'условие' ? 'не выполняется' : 'не выполняются'}`;
  return { unknown, failed, failedLabel, label: unknown ? unknownLabel : failed ? failedLabel : conditions.length ? 'Условия проверены' : 'Сведения об условиях пока не получены' };
}

/** Use only explicit revision/ID scope. Missing legacy metadata never guesses a route. */
export function checkActivity(state: LiveState | null, optionId?: string): CheckActivityState {
  const connectionLost = state?.connection !== 'live';
  const result = (phase: CheckPhase, label: string, detail: string): CheckActivityState => {
    if (connectionLost && activePhases.has(phase)) return { phase: 'recovering', active: false, connectionLost: true,
      label: 'Восстанавливаем состояние проверки',
      detail: `Последнее полученное состояние: ${phase === 'running' ? 'обновляли цены и связанные расчёты' : phase === 'queued' ? 'обновление цен ожидало очереди' : 'отправляли запрос на обновление цен'}. Текущий ход работы пока неизвестен.` };
    return { phase, label, detail, active: activePhases.has(phase), connectionLost };
  };
  const idle = () => result('idle', '', connectionLost ? 'Показаны последние полученные сведения; связь с обновлениями восстанавливается.' : '');
  if (!state || optionId !== undefined && !state.session.options.some(option => option.id === optionId)) return idle();
  const { job, pending, session } = state;
  const scoped = (ids?: string[]) => optionId === undefined ? true : Array.isArray(ids) && ids.includes(optionId);
  const currentJob = job?.session_id === session.id && job.kind === 'prices_refresh';
  let phase: CheckPhase = 'idle';
  if (currentJob) {
    if (job.status === 'completed' && job.result_revision === session.revision && scoped(job.result_option_ids)) phase = 'completed';
    else if (job.status !== 'completed' && job.captured_revision === session.revision && scoped(job.target_option_ids)) phase = job.status;
  }
  const pendingHere = pending?.kind === 'prices_refresh' && pending.revision === session.revision
    && (optionId === undefined || pending.optionId === undefined || pending.optionId === optionId);
  // A new committed job beats its still-unacknowledged POST. A prior terminal job
  // must not mask a fresh submission, including when another snapshot arrives.
  const committedAfterPending = currentJob && phase !== 'idle' && pending
    && (pending.jobId === job.id || job.id !== pending.previousJobId && state.sequence > pending.startedAfterSequence);
  if (pendingHere && !committedAfterPending) return result('submitting', pending.jobId ? 'Запрос принят. Ждём состояние проверки…' : 'Отправляем запрос на обновление цен…',
    pending.jobId ? 'Синхронизируем принятое сервером задание.' : 'Ожидаем принятия запроса сервером.');
  if (phase === 'queued') return result(phase, 'Обновление цен в очереди', 'Задание принято сервером и ожидает выполнения.');
  if (phase === 'running') return result(phase, 'Обновляем цены и связанные расчёты…', job?.progress || 'Выполняется обновление опубликованных цен и расчёт проверок маршрута.');
  if (phase === 'completed') return result(phase, 'Обновление цен завершено', 'Результаты проверки цен и условия поездки показаны в маршруте.');
  if (phase === 'failed') return result(phase, 'Не удалось обновить цены', job?.error || 'Показаны последние полученные сведения. Повторите обновление цен.');
  if (phase === 'superseded') return result(phase, 'Обновление цен устарело', 'Задача больше не относится к актуальному плану. Повторите обновление для текущего маршрута.');
  if (phase === 'canceled') return result(phase, 'Обновление отменено', 'Последние полученные сведения сохранены.');
  if (phase === 'interrupted') return result(phase, 'Обновление прервано', 'Сервис перезапустился. При необходимости отправьте новый запрос.');
  return idle();
}
