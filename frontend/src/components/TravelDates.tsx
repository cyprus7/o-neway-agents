import type { Component, Intent } from '../types';
import './travel-dates.css';

export function calendarDate(value?: string | null) {
  if (!value || !/^\d{4}-\d{2}-\d{2}$/.test(value)) return 'не указана';
  const date = new Date(`${value}T00:00:00Z`);
  return Number.isNaN(date.getTime()) ? 'не указана' : new Intl.DateTimeFormat('ru-RU', { day: 'numeric', month: 'short', timeZone: 'UTC' }).format(date);
}
const dates = (from?: string | null, to?: string | null) => `${calendarDate(from)}${to && to !== from ? ` — ${calendarDate(to)}` : ''}`;
function nights(intent: Intent) {
  if (!intent.nights_min) return '';
  if (intent.nights_max && intent.nights_max !== intent.nights_min) return `, ${intent.nights_min}–${intent.nights_max} ночей`;
  const count = intent.nights_min, lastTwo = count % 100, last = count % 10;
  return `, ${count} ${lastTwo >= 11 && lastTwo <= 14 ? 'ночей' : last === 1 ? 'ночь' : last >= 2 && last <= 4 ? 'ночи' : 'ночей'}`;
}

/** Stay duration comes from the server; the departure window never adds nights. */
export function StayAndFlightDates({ intent }: { intent: Intent }) {
  if (!intent.flight_departure_from) return null;
  return <>
    <span className="travel-date-window">Проживание: {dates(intent.date_from, intent.date_to)}{nights(intent)}.</span>
    <span className="travel-date-window">Вылет: {dates(intent.flight_departure_from, intent.flight_departure_to)}; прибытие {calendarDate(intent.date_from)} (дата поиска, не подтверждённый рейс).</span>
  </>;
}

export function FlightSearchDates({ part }: { part: Component }) {
  const raw = part.details?.departure_window;
  const window = raw && typeof raw === 'object' && !Array.isArray(raw) ? raw as Record<string, unknown> : null;
  const from = typeof window?.date_from === 'string' ? window.date_from : null;
  const to = typeof window?.date_to === 'string' ? window.date_to : null;
  const arrival = typeof window?.arrival_date === 'string' ? window.arrival_date : null;
  const selected = typeof part.details?.selected_departure_date === 'string' ? part.details.selected_departure_date : null;
  if (!from && !selected) return null;
  return <section className="flight-search-dates" aria-label="Даты поиска перелёта">
    {from && <p>Окно вылета для поиска: {dates(from, to)}; прибытие {calendarDate(arrival)}. Это параметры поиска, не подтверждение рейса.</p>}
    {selected && <p>Дата вылета в источнике: {calendarDate(selected)}. Время и пересадки — в описании источника.</p>}
  </section>;
}
