import { CheckCircle2, ChevronDown, CircleHelp, Clock3, LoaderCircle, RotateCcw, XCircle } from 'lucide-react';
import type { Option } from '../types';
import type { CheckActivityState } from '../live/checkActivity';
import { demoComponent } from './PricingSummary';
import { conditionSummary } from '../live/checkActivity';
import './check-activity.css';

export function PriceRefreshButton({ activity, disabled, onClick, idleLabel, ariaLabel }: {
  activity: CheckActivityState; disabled: boolean; onClick: () => void; idleLabel: string; ariaLabel?: string;
}) {
  const spinning = activity.phase === 'submitting' || activity.phase === 'running';
  const Icon = spinning ? LoaderCircle : activity.phase === 'queued' ? Clock3 : RotateCcw;
  const label = activity.phase === 'submitting' ? 'Отправляем запрос…' : activity.phase === 'queued' ? 'Обновление цен в очереди'
    : activity.phase === 'running' ? 'Обновляем цены…' : activity.phase === 'recovering' ? 'Восстанавливаем состояние…' : idleLabel;
  return <button className="refresh-prices" onClick={onClick} disabled={disabled} aria-label={ariaLabel}><Icon className={spinning ? 'check-spinner' : undefined} size={14} aria-hidden="true"/> {label}</button>;
}

export function CheckActivity({ option, activity, announce = false }: {
  option: Option; activity: CheckActivityState; announce?: boolean;
}) {
  const counts = conditionSummary(option.constraint_checks);
  const demo = !!demoComponent(option, 'flight');
  const failures = option.constraint_checks.filter(condition => condition.status === 'failed');
  const Icon = activity.phase === 'running' || activity.phase === 'submitting' ? LoaderCircle
    : activity.phase === 'queued' ? Clock3 : activity.phase === 'completed' ? CheckCircle2
      : activity.phase === 'failed' || activity.phase === 'superseded' ? XCircle : RotateCcw;
  return <section className={`check-activity check-activity-${activity.phase}`} aria-label={`Условия маршрута: ${option.destination}`}>
    {demo && failures.length > 0 && <div className="condition-failed"><strong>{counts.failedLabel}</strong><ul>{failures.map((condition, index) => <li key={index}>{String(condition.constraint)}: {String(condition.detail || '')}</li>)}</ul></div>}
    <details className="condition-disclosure">
      <summary><CircleHelp size={15} aria-hidden="true"/><span>{demo ? 'Перед реальной поездкой' : counts.label}{!demo && counts.unknown > 0 && counts.failed > 0 && <small>{counts.failedLabel}</small>}</span><ChevronDown className="condition-chevron" size={14} aria-hidden="true"/></summary>
      <div className="condition-details">
        <p>Это статусы сведений, а не список фоновых задач. Обновление цен проверяет опубликованные суммы и связанные расчёты. Наличие, правила отеля, перевозка питомцев и документы требуют отдельного подтверждения.</p>
        {option.constraint_checks.length > 0 && <ul>{option.constraint_checks.map((condition, index) => {
          const passed = condition.status === 'passed', failed = condition.status === 'failed';
          const StateIcon = passed ? CheckCircle2 : failed ? XCircle : CircleHelp;
          return <li key={index} className={passed ? 'condition-passed' : failed ? 'condition-failed' : 'condition-unknown'}><StateIcon size={15} aria-hidden="true"/><div><strong>{String(condition.constraint || 'Условие')}</strong><span>{passed ? 'Проверено' : failed ? 'Не выполняется' : 'Требует проверки'}</span><p>{String(condition.detail || (passed ? 'По данным текущего плана.' : 'Уточните условия у источника.'))}</p></div></li>;
        })}</ul>}
      </div>
    </details>
    {activity.phase !== 'idle' ? <div className="check-status" role={announce ? 'status' : undefined} aria-live={announce ? 'polite' : undefined} aria-atomic={announce ? true : undefined}>
      <Icon size={15} className={activity.phase === 'running' || activity.phase === 'submitting' ? 'check-spinner' : undefined} aria-hidden="true"/>
      <div><strong>{activity.label}</strong><p>{activity.detail}</p>{activity.connectionLost && activity.phase !== 'recovering' && <p>Показан последний полученный результат; связь с обновлениями восстанавливается.</p>}{!demo && activity.phase === 'completed' && counts.unknown > 0 && <p className="check-remaining">{counts.label}.</p>}</div>
    </div> : activity.connectionLost ? <p className="check-idle-note">{activity.detail}</p> : !demo && counts.unknown > 0 && <p className="check-idle-note">Неподтверждённые условия остаются в списке. Для актуальных сумм можно обновить цены маршрута.</p>}
  </section>;
}
