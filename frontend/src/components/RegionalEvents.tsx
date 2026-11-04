import { useId } from 'react';
import { CalendarDays, ExternalLink, MapPin, Sparkles } from 'lucide-react';
import type { RegionalEvent, RegionalEventResult } from '../types';
import './regional-events.css';

const categories: Record<RegionalEvent['category'], string> = {
  culture: 'Культура', music: 'Музыка', sport: 'Спорт', exhibition: 'Выставка', other: 'Событие',
};
const stateCopy = {
  not_checked: 'События для этого маршрута пока не проверяли.',
  dates_required: 'Укажите точные даты поездки — тогда сможем проверить события рядом.',
  empty: 'Не нашли событий с подтверждёнными датами и местом проведения на время поездки. Это не означает, что в регионе нет событий.',
  unavailable: 'Сейчас не удалось проверить источники событий. Это не мешает пользоваться основным маршрутом.',
};

/** Preserve the publisher's calendar day, regardless of the viewer's timezone. */
export function formatEventDate(value: string): string {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) return 'Дата не распознана';
  const parsed = new Date(`${value}T00:00:00Z`);
  if (!Number.isFinite(parsed.getTime()) || parsed.toISOString().slice(0, 10) !== value) return 'Дата не распознана';
  return new Intl.DateTimeFormat('ru-RU', { day: 'numeric', month: 'short', year: 'numeric', timeZone: 'UTC' }).format(parsed);
}

export function formatEventTimestamp(value?: string | null): string | undefined {
  if (!value) return undefined;
  const parsed = new Date(value);
  if (!Number.isFinite(parsed.getTime())) return undefined;
  return new Intl.DateTimeFormat('ru-RU', {
    day: 'numeric', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit', timeZone: 'Asia/Nicosia',
  }).format(parsed);
}

/** Event facts are sourced over HTTPS; unsupported links are rendered as text. */
export function eventSourceUrl(value: string): string | undefined {
  try {
    const url = new URL(value);
    return url.protocol === 'https:' && !url.username && !url.password ? url.href : undefined;
  } catch { return undefined; }
}

function EventCard({ event }: { event: RegionalEvent }) {
  const sourceUrl = eventSourceUrl(event.source.source_url);
  const retrieved = formatEventTimestamp(event.source.retrieved_at);
  return <li className="regional-event-card">
    <div className="regional-event-meta"><span className="regional-event-category">{categories[event.category] || 'Событие'}</span>{event.significance === 'plan_ahead' && <span className="or-badge or-badge--warning regional-event-badge">Учесть в планах</span>}</div>
    <p className="regional-event-dates"><CalendarDays size={15} aria-hidden="true"/><span><time dateTime={event.start_date}>{formatEventDate(event.start_date)}</time>{event.end_date !== event.start_date && <> — <time dateTime={event.end_date}>{formatEventDate(event.end_date)}</time></>}</span></p>
    <h4>{event.title}</h4>
    <p className="regional-event-place"><MapPin size={15} aria-hidden="true"/><span>{event.location_name}{event.location_city && event.location_city !== event.location_name ? ` · ${event.location_city}` : ''}</span></p>
    <p className="regional-event-reason">{event.reason}</p>
    <div className="regional-event-source">
      {sourceUrl ? <a href={sourceUrl} target="_blank" rel="noopener noreferrer" aria-label={`Открыть источник события «${event.title}»: ${event.source.provider} (новая вкладка)`}><span>Источник: {event.source.provider}</span><ExternalLink size={14} aria-hidden="true"/></a> : <span className="regional-event-source-unlinked">Источник: {event.source.provider} · ссылка недоступна</span>}
      <p>{event.source.freshness === 'cached' ? 'Из кеша' : 'Получено от источника'}{retrieved && <> · <time dateTime={event.source.retrieved_at} title="Время получения, Asia/Nicosia">{retrieved}</time></>}</p>
    </div>
  </li>;
}

export function RegionalEvents({ result, destination }: { result?: RegionalEventResult | null; destination: string }) {
  const headingId = useId();
  const status = result?.status ?? 'not_checked';
  const items = status === 'ready' && Array.isArray(result?.items) ? result.items.slice(0, 3) : [];
  const displayStatus = status === 'ready' && items.length === 0 ? 'empty' : status;
  const checked = formatEventTimestamp(result?.checked_at);
  return <section className="regional-events" aria-labelledby={headingId}>
    <div className="regional-events-heading"><div><p className="regional-events-eyebrow"><Sparkles size={13} aria-hidden="true"/> Необязательные идеи для поездки</p><h3 id={headingId}>События рядом в даты поездки</h3></div>{checked && <p className="regional-events-checked">Поиск выполнен <time dateTime={result?.checked_at || undefined} title="Время поиска, Asia/Nicosia">{checked}</time></p>}</div>
    {displayStatus === 'ready' ? <>
      <p className="regional-events-intro">{destination}: идеи для новых впечатлений и поводы заранее уточнить планы. Включать их в маршрут необязательно.</p>
      <ul className="regional-events-grid">{items.map(event => <EventCard key={event.id} event={event}/>)}</ul>
      <p className="regional-events-disclaimer">Даты и места указаны по данным источников. Перед поездкой уточните расписание у организатора. Наличие билетов и возможность посещения не проверены.</p>
    </> : <p className="regional-events-state">{stateCopy[displayStatus as keyof typeof stateCopy] || stateCopy.not_checked}</p>}
  </section>;
}
