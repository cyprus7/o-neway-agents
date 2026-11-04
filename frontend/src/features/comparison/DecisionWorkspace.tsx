import type { Intent, Option } from '../../types';
import { ComponentPrice, PricingSummary, demoComponent } from '../../components/PricingSummary';
import { StayAndFlightDates } from '../../components/TravelDates';
import './decision-workspace.css';

export type EvidenceAction = 'compare' | 'explain';
export function optionTitle(option: Option, options: Option[]) {
  return options.filter(other => other.destination === option.destination).length > 1
    ? `${option.destination} · ${option.stay.title}` : option.destination;
}
export function DecisionWorkspace({ options, disabled, onAction }: {
  options: Option[]; disabled: boolean; onAction: (action: EvidenceAction, ids: string[]) => void;
}) {
  const shortlist = options.slice(0, 3);
  return <section className="decision-workspace" aria-labelledby="comparison-title">
    <div className="section-heading"><div><h2 id="comparison-title">Сравнить варианты</h2><p>{shortlist.some(option => demoComponent(option, 'flight')) ? 'Сравните ориентировочную стоимость и состав поездки.' : 'Сохранённые сведения текущего плана. Неизвестные условия требуют проверки.'}</p></div>
      <button className="text-button" disabled={disabled || shortlist.length < 2} onClick={() => onAction('compare', shortlist.map(option => option.id))}>Сравнить по источникам</button>
    </div>
    <div className="comparison-grid">{shortlist.map(option => {
      const demo = !!demoComponent(option, 'flight');
      const passed = option.constraint_checks.filter(check => check.status === 'passed');
      const failed = option.constraint_checks.filter(check => check.status === 'failed');
      const unknown = option.constraint_checks.filter(check => check.status !== 'passed' && check.status !== 'failed');
      return <article key={option.id} className="comparison-option" aria-label={optionTitle(option, options)}>
        <h3>{optionTitle(option, options)}</h3><PricingSummary option={option} compact/>
        <dl className="comparison-facts"><div><dt>Почему подходит</dt><dd>{option.reasons.join('; ') || 'Обоснование не получено'}</dd></div>
          <div><dt>Компромиссы</dt><dd>{option.tradeoffs.join('; ') || 'Компромиссы не описаны'}</dd></div>
          <div><dt>Условия</dt><dd>{demo ? (failed.length ? `${failed.length} ограничений требуют внимания` : 'Реальные условия — в подробностях') : option.constraint_checks.length ? `Проверено: ${passed.length}. Не выполняется: ${failed.length}. Неизвестно: ${unknown.length}.` : 'Сведения об условиях не получены'}</dd></div>
        </dl>
        {(demo ? failed : [...failed, ...unknown]).length > 0 && <ul className="comparison-conditions">{(demo ? failed : [...failed, ...unknown]).map((check, index) => <li key={index}><strong>{check.status === 'failed' ? 'Не выполняется' : 'Неизвестно'}:</strong> {String(check.constraint || 'Условие')} {String(check.detail || '')}</li>)}</ul>}
        {demo && unknown.length > 0 && <details><summary>Перед реальной поездкой</summary><ul>{unknown.map((check, index) => <li key={index}>{String(check.constraint || 'Условие')}: {String(check.detail || 'Требует проверки у источника.')}</li>)}</ul></details>}
        <details><summary>Цены, состав и время источников</summary><p>Каждая цена относится к указанной единице и составу тарифа. Демо-оценка отмечена отдельно; условия источника не становятся подтверждёнными.</p>
          {([['Перелёт', option.flight, 'flight'], ['Проживание', option.stay, 'stay'], ['Трансфер', option.transfer, 'transfer']] as const).map(([label, part, kind]) => <section key={label} className="comparison-source"><h4>{label}: {part.title}</h4><ComponentPrice part={part} demo={demoComponent(option, kind)}/></section>)}
        </details>
        <button className="text-button" disabled={disabled} onClick={() => onAction('explain', [option.id])} aria-label={`Объяснить вариант: ${optionTitle(option, options)}`}>Почему этот вариант?</button>
      </article>;
    })}</div>
  </section>;
}

export function PreviousPlan({ plan }: { plan: { revision: number; intent: Intent; options: Option[] } }) {
  return <section className="previous-plan" aria-labelledby="previous-plan-title">
    <h2 id="previous-plan-title">Предыдущий план · версия {plan.revision}</h2>
    <p role="status">Этот план устарел. Показан для сравнения с новым запросом. Выбор и демо-оплата недоступны.</p>
    <p>{plan.intent.origin || 'Отправление не задано'} → {plan.intent.destination || 'Направление не задано'}{!plan.intent.flight_departure_from && <> · {plan.intent.date_from || 'Даты не заданы'}{plan.intent.date_to ? ` — ${plan.intent.date_to}` : ''}</>}</p>
    {plan.intent.flight_departure_from && <div className="intent-bar"><StayAndFlightDates intent={plan.intent}/></div>}
    <div className="comparison-grid">{plan.options.slice(0, 3).map(option => <article key={option.id} className="comparison-option"><h3>{optionTitle(option, plan.options)}</h3><p>{option.stay.title}</p><PricingSummary option={option} compact/></article>)}</div>
  </section>;
}
