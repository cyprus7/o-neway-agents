import { ExternalLink, Info } from 'lucide-react';
import type { Component, DemoComponentPrice, Option, PriceObservation, PriceValue } from '../types';

const componentLabels: Record<string, string> = { flight: 'Перелёт', stay: 'Проживание', transfer: 'Трансфер', rail: 'Поезд', bus: 'Автобус', car: 'Автомобиль' };
const freshnessLabels: Record<string, string> = { live: 'Получено у источника', cached: 'Сохранённая проверка', stale: 'Предыдущая цена', unavailable: 'Цена не получена' };
const providerLabels: Record<string, string> = { aegean_public: 'AEGEAN', ryanair_farefinder: 'Ryanair', direct_hotel_tariffs: 'Официальный сайт отеля', google_flights: 'Google Flights', google_hotels: 'Google Hotels', 'knowledge-prices': 'Источник тарифа' };

function safeSource(url?: string | null): string | undefined {
  if (!url) return undefined;
  try {
    const parsed = new URL(url);
    return ['http:', 'https:'].includes(parsed.protocol) && !parsed.username && !parsed.password ? parsed.href : undefined;
  } catch { return undefined; }
}

function formatMoney(value: string, currency: string): string {
  // Formatting only: sums and group/night multiplication belong to the server.
  const number = Number(value);
  if (!Number.isFinite(number)) return 'Цена не получена';
  try { return new Intl.NumberFormat('ru-RU', { style: 'currency', currency, maximumFractionDigits: 2 }).format(number); }
  catch { return `${value} ${currency}`; }
}

export function priceLabel(value?: PriceValue | null): string | null {
  if (!value || value.kind === 'unknown') return null;
  if (value.kind === 'point') return formatMoney(value.amount, value.currency);
  if (value.kind === 'lower_bound') return `от ${formatMoney(value.min, value.currency)}`;
  return `${formatMoney(value.min, value.currency)} – ${formatMoney(value.max, value.currency)}`;
}

function checkedTime(value?: string | null): string {
  if (!value || Number.isNaN(new Date(value).getTime())) return 'время источника неизвестно';
  return new Intl.DateTimeFormat('ru-RU', { day: '2-digit', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit', timeZone: 'Asia/Nicosia' }).format(new Date(value));
}

function sourceDate(value?: string | null): string {
  if (!value || Number.isNaN(new Date(value).getTime())) return 'дата не указана';
  return new Intl.DateTimeFormat('ru-RU', { day: 'numeric', month: 'short', year: 'numeric', timeZone: 'UTC' }).format(new Date(value));
}

function unitLabel(quote: PriceObservation): string {
  if (quote.unit_label) return quote.unit_label;
  const unit = quote.scope?.unit;
  const trip = quote.scope?.trip_type === 'one_way' ? ', в одну сторону' : quote.scope?.trip_type === 'roundtrip' ? ', туда-обратно' : '';
  if (unit === 'person_trip') return `на 1 взрослого${trip}`;
  if (unit === 'room_night') return 'за 1 номер / 1 ночь';
  if (unit === 'group_night') return 'за группу / 1 ночь';
  if (unit === 'group_trip') return `за всех путешественников${trip}`;
  if (unit === 'group_stay') return 'за всех гостей и весь срок проживания';
  return 'Источник не указал единицу цены';
}

function counted(value: number, forms: [string, string, string]): string {
  const lastTwo = value % 100;
  const last = value % 10;
  return `${value} ${lastTwo >= 11 && lastTwo <= 14 ? forms[2] : last === 1 ? forms[0] : last >= 2 && last <= 4 ? forms[1] : forms[2]}`;
}

function compactUnitLabel(quote: PriceObservation): string {
  const original = unitLabel(quote);
  if (quote.provider_id !== 'google_hotels' || !/\bprivate\s+bathroom\b/i.test(original)
      || /\b(?:shared|dorm(?:itory)?|bunk)\b|\bbed\s+in\b/i.test(original)) return original;
  const guests = original.match(/\b(\d+)\s+guests?\b/i);
  const nights = original.match(/(?:^|[\s;·])за\s+(\d+)\s+ноч(?:ей|ь|и)(?=$|[\s;,.·])/i);
  if (!guests || !nights) return original;
  // This describes the displayed rate only. It does not assert a private room,
  // availability or suitability for the travelers in the current request.
  return `Своя ванная · в тарифе ${counted(Number(guests[1]), ['гость', 'гостя', 'гостей'])} · за ${counted(Number(nights[1]), ['ночь', 'ночи', 'ночей'])}`;
}

export function demoComponent(option: Option, kind: DemoComponentPrice['kind']) {
  return priceLabel(option.pricing_summary?.full_trip) ? undefined : option.demo_pricing?.components.find(item => item.kind === kind);
}

export function ComponentPrice({ part, compact = false, demo }: { part: Component; compact?: boolean; demo?: DemoComponentPrice }) {
  if (demo) return <div className={`component-price price-reference ${compact ? 'price-compact' : ''}`}>
    <div className="price-value-line"><strong>{priceLabel(demo.value)}</strong><span className="price-kind">Оценка · демо</span></div>
    <details className="price-context"><summary>Основание оценки</summary>
      <p>{demo.basis === 'comparable_source' ? 'Демо-оценка по сопоставимой цене источника. Это не подтверждённый тариф для вашей поездки.' : 'Вымышленная демо-оценка для сравнения вариантов.'}</p>
      {safeSource(demo.source_url) && <a href={safeSource(demo.source_url)} target="_blank" rel="noreferrer">Источник сравнения <ExternalLink size={11}/></a>}
      {demo.source_observed_at && <p>Дата наблюдения источника: {checkedTime(demo.source_observed_at)}</p>}
      {demo.assumptions.length > 0 && <ul>{demo.assumptions.map((value, index) => <li key={index}>{value}</li>)}</ul>}
      <details><summary>Фактические сведения о цене</summary><ComponentPrice part={part} compact={compact}/></details>
    </details>
  </div>;
  const quote = part.quote;
  const label = priceLabel(quote?.value);
  if (!quote || !label) {
    const reason = quote?.reason || (quote?.value.kind === 'unknown' ? quote.value.reason : null);
    return <div className={`component-price price-unavailable ${compact ? 'price-compact' : ''}`}>
      <strong>Цена пока не найдена</strong>
      {reason && <details className="price-context"><summary>Детали поиска</summary><p>{reason}</p></details>}
    </div>;
  }
  const reference = quote.outcome !== 'quoted' || !quote.normalized_group;
  const source = safeSource(quote.source_url);
  const sourceName = providerLabels[quote.provider_id] || quote.provider_id.replaceAll('_', ' ');
  const excluded = quote.excluded_charges || [];
  const unknown = quote.unknown_charges || [];
  const included = quote.included_charges || [];
  const legs = Array.isArray(part.details?.pricing_legs) ? part.details.pricing_legs as Record<string, unknown>[] : [];
  const originalUnit = unitLabel(quote);
  const displayUnit = compact ? compactUnitLabel(quote) : originalUnit;
  const refreshWarning = typeof part.details?.pricing_refresh_warning === 'string' ? part.details.pricing_refresh_warning : null;
  return <div className={`component-price ${reference ? 'price-reference' : 'price-quoted'} ${compact ? 'price-compact' : ''}`}>
    <div className="price-value-line"><strong>{label}</strong><span className="price-kind">{quote.freshness === 'stale' ? 'Предыдущая цена' : reference ? 'Цена источника' : 'Найденная цена'}</span></div>
    <span className="price-unit">{displayUnit}</span>
    {!compact && refreshWarning && <p className="price-refresh-warning">{refreshWarning}</p>}
    <div className="price-provenance">
      {source ? <a href={source} target="_blank" rel="noreferrer">{sourceName} <ExternalLink size={11}/></a> : <span>{sourceName}</span>}
      <span>{freshnessLabels[quote.freshness] || quote.freshness} · {checkedTime(quote.retrieved_at)}</span>
    </div>
    <details className="price-context"><summary>Даты, условия и доплаты</summary><div>
      {quote.reason && <p className="price-reason">{quote.reason}</p>}
      {compact && refreshWarning && <p className="price-refresh-warning">{refreshWarning}</p>}
      {quote.unit_label && (compact || /[A-Za-z]/.test(originalUnit)) && <p>Подпись источника: {originalUnit}</p>}
      <p>Наблюдение источника: {checkedTime(quote.observed_at)}</p>
      <p>Даты источника: {sourceDate(quote.scope?.date_from)}{quote.scope?.date_to ? ` — ${sourceDate(quote.scope.date_to)}` : ''}</p>
      {quote.scope?.origin && <p>Маршрут источника: {quote.scope.origin} → {quote.scope.destination || 'назначение не указано'}.</p>}
      {legs.map((leg, i) => {
        const url = safeSource(typeof leg.source_url === 'string' ? leg.source_url : null);
        return <p key={i}>{String(leg.origin_iata || '')} → {String(leg.destination_iata || '')}, {sourceDate(String(leg.date || ''))}: {formatMoney(String(leg.amount || ''), String(leg.currency || 'EUR'))} на 1 взрослого. {url && <a href={url} target="_blank" rel="noreferrer">Источник тарифа <ExternalLink size={11}/></a>}</p>;
      })}
      {included.length > 0 && <p>Включено: {included.join('; ')}.</p>}
      {excluded.length > 0 && <p>Не включено: {excluded.join('; ')}.</p>}
      {unknown.length > 0 && <p>Уточнить у продавца: {unknown.join('; ')}.</p>}
      {!quote.availability_verified && <p>Наличие мест и условия для вашей группы — у продавца.</p>}
      {quote.expires_at && <p>Повторная проверка после {checkedTime(quote.expires_at)}.</p>}
    </div></details>
    {!compact && excluded.length > 0 && <p className="price-exclusions">Без: {excluded.slice(0, 3).join('; ')}.</p>}
  </div>;
}

export function PricingSummary({ option, compact = false }: { option: Option; compact?: boolean }) {
  const summary = option.pricing_summary;
  const full = priceLabel(summary?.full_trip);
  const demo = !full && option.demo_pricing;
  if (demo) return <div className={`pricing-summary ${compact ? 'pricing-compact' : ''}`} aria-label="Цены и состав расходов">
    <span className="price-summary-label">Ориентировочно · демо</span>
    <strong className="price-headline">{priceLabel(demo.total)}</strong>
    <p className="price-scope">{demo.scope_label}</p>
    {demo.budget_explanation && <p className="price-budget">{demo.budget_explanation}</p>}
    <details className="price-context"><summary>Состав оценки и допущения</summary>
      <p>Иллюстрация для сравнения вариантов. Наличие и реальные условия не подтверждены.</p>
      {demo.components.map(item => <section className="price-reference-item" key={item.kind}>
        <span className="price-component-label">{componentLabels[item.kind]}</span><ComponentPrice part={option[item.kind]} demo={item} compact={compact}/>
      </section>)}
      <p>Демо-сбор: {priceLabel(demo.service_fee)}</p>
      {demo.assumptions.length > 0 && <ul>{demo.assumptions.map((value, index) => <li key={index}>{value}</li>)}</ul>}
    </details>
  </div>;
  const subtotal = priceLabel(summary?.transport_stay);
  const partial = priceLabel(summary?.known_partial);
  const numeric = full || subtotal || partial;
  const references = (['flight', 'stay', 'transfer'] as const)
    .map(kind => ({ kind, part: option[kind] }))
    .filter(({ part }) => priceLabel(part.quote?.value));
  const title = full ? 'Полная стоимость поездки' : subtotal ? 'Перелёт и проживание' : 'Известная часть расходов';
  return <div className={`pricing-summary ${compact ? 'pricing-compact' : ''}`} aria-label="Цены и состав расходов">
    {numeric ? <>
      <span className="price-summary-label">{title}</span><strong className="price-headline">{numeric}</strong>
      <p className="price-scope">{summary?.scope_label}</p>
      {summary?.budget_explanation && <p className="price-budget">{summary.budget_explanation}</p>}
      {!full && <p className="price-summary-note"><Info size={12}/> Полная стоимость поездки ещё неизвестна.</p>}
      {!!summary?.missing_components.length && <p className="price-exclusions">Не учтено: {summary.missing_components.map(kind => componentLabels[kind] || kind).join(', ')}.</p>}
      {!!summary?.excluded_charges.length && <p className="price-exclusions">Не включено: {summary.excluded_charges.join('; ')}.</p>}
      {!!summary?.unknown_charges.length && <p className="price-exclusions">Требует проверки: {summary.unknown_charges.join('; ')}.</p>}
      {summary?.as_of && <p className="price-summary-time">Данные на {checkedTime(summary.as_of)}</p>}
    </> : references.length ? <>
      <span className="price-summary-label">Цены из источников</span>
      <div className="price-references">{references.map(({ kind, part }) => <section className="price-reference-item" key={kind}>
        <span className="price-component-label">{componentLabels[kind]}</span><ComponentPrice part={part} compact={compact}/>
      </section>)}</div>
      <p className="price-summary-note"><Info size={12}/> Отдельные цены по позициям. Полный итог поездки ещё не рассчитан.</p>
    </> : <>
      <strong className="price-headline price-headline-unknown">Цены пока не найдены</strong>
      <p className="price-summary-note">Источники ещё не вернули подходящие суммы для этого маршрута.</p>
      {!compact && summary?.missing_reasons && <ul className="price-missing-reasons">{Object.entries(summary.missing_reasons).map(([kind, reason]) => <li key={kind}><strong>{componentLabels[kind] || kind}:</strong> {reason}</li>)}</ul>}
    </>}
  </div>;
}
