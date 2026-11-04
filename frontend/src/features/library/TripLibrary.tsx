import { useEffect, useRef, useState } from 'react';
import { api, clearDeletedIdentity } from '../../api';
import type { Session } from '../../types';
import './library.css';

const fields = {
  origin: 'Город отправления', currency: 'Валюта', climate_preferences: 'Предпочтения климата',
  trip_styles: 'Формат отдыха', pets: 'Питомцы и условия перевозки', direct_flight_preferred: 'Прямой рейс', max_travel_minutes: 'Максимум минут в пути',
};
type Field = keyof typeof fields;
type Memory = { field: Field; value: unknown; source_trip_id: string | null; consented_at: string; updated_at: string };
type Trip = { id: string; title: string; destination: string | null; revision: number; created_at: string; updated_at: string };
const when = (value: string) => Number.isNaN(Date.parse(value)) ? "Время не указано" : new Intl.DateTimeFormat("ru-RU", { dateStyle: "short", timeStyle: "short" }).format(new Date(value));
const showValue = (value: unknown) => Array.isArray(value) ? value.join(', ') : typeof value === 'boolean' ? value ? 'да' : 'нет' : String(value ?? '');
const editValue = (field: Field, value: unknown) => field === 'pets' && Array.isArray(value) ? value.join('\n') : showValue(value);

export function TripLibrary({ session, applyMemory, onApplyMemory, onOpen, onDeleted }: {
  session: Session; applyMemory: boolean; onApplyMemory: (value: boolean) => void;
  onOpen: (id: string) => Promise<void>; onDeleted: (id: string) => Promise<void>;
}) {
  const [trips, setTrips] = useState<Trip[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [memory, setMemory] = useState<Memory[]>([]);
  const [busy, setBusy] = useState(true);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [field, setField] = useState<Field>('origin');
  const [value, setValue] = useState('');
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    void Promise.all([api<{ items: Trip[]; next_cursor: string | null }>('/api/sessions?limit=20'), api<{ items: Memory[] }>('/api/me/memory')])
      .then(([list, saved]) => { if (mounted.current) { setTrips(list.items); setCursor(list.next_cursor); setMemory(saved.items.filter(item => Object.hasOwn(fields, item.field))); } })
      .catch(reason => { if (mounted.current) setError((reason as Error).message); })
      .finally(() => { if (mounted.current) setBusy(false); });
    return () => { mounted.current = false; };
  }, []);
  async function run(work: () => Promise<void>) {
    if (busy) return;
    setBusy(true); setError(''); setNotice('');
    try { await work(); } catch (reason) { if (mounted.current) setError((reason as Error).message); }
    finally { if (mounted.current) setBusy(false); }
  }
  function useCurrent(next: Field) {
    setField(next);
    setValue(editValue(next, (session.intent as unknown as Record<string, unknown>)[next]));
  }
  async function save() {
    let parsed: unknown = value.trim();
    if (field === 'trip_styles' || field === 'climate_preferences') parsed = value.split(',').map(item => item.trim()).filter(Boolean);
    if (field === 'pets') parsed = value.split(/\r?\n/).map(item => item.trim()).filter(Boolean);
    if (field === 'pets' && Array.isArray(parsed) && parsed.length > 5) throw new Error('Можно сохранить не больше пяти условий о питомцах. Сохраняйте уточнения в каждом условии.');
    if (field === 'direct_flight_preferred') parsed = value === 'да';
    if (field === 'max_travel_minutes') { parsed = Number(value); if (!Number.isInteger(parsed) || Number(parsed) <= 0) throw new Error('Укажите положительное число минут.'); }
    if (field === 'currency' && !/^[A-Z]{3}$/.test(String(parsed))) throw new Error('Валюта: три заглавные латинские буквы, например EUR.');
    if (parsed === '' || Array.isArray(parsed) && parsed.length === 0) throw new Error('Укажите значение предпочтения.');
    await api(`/api/me/memory/${field}`, { value: parsed, source_trip_id: session.id }, { method: 'PUT' });
    const saved = await api<{ items: Memory[] }>('/api/me/memory');
    if (mounted.current) { setMemory(saved.items.filter(item => Object.hasOwn(fields, item.field))); setNotice('Предпочтение сохранено с вашего согласия.'); }
  }
  async function removeTrip(id: string) {
    if (!window.confirm('Удалить эту поездку и её историю? Это действие нельзя отменить.')) return;
    await api(`/api/sessions/${encodeURIComponent(id)}`, undefined, { method: 'DELETE' });
    if (mounted.current) setTrips(items => items.filter(item => item.id !== id));
    await onDeleted(id);
  }
  async function exportData() {
    const data = await api<unknown>('/api/me/export');
    if (!mounted.current) return;
    const url = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' }));
    const link = document.createElement('a'); link.href = url; link.download = 'one-request-export.json'; link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000); setNotice('Экспорт подготовлен в JSON.');
  }
  return <section className="trip-library" aria-labelledby="library-title" aria-busy={busy}>
    <h2 id="library-title">Мои поездки и предпочтения</h2>
    {error && <p role="alert">{error}</p>}{notice && <p role="status">{notice}</p>}
    {busy && <p role="status">Загружаем данные…</p>}
    <ul className="library-trips">{trips.map(trip => <li key={trip.id}><div><strong>{trip.title || trip.destination || 'Новая поездка'}</strong><small>Версия {trip.revision} · {when(trip.updated_at)}</small></div><button className="text-button" disabled={busy} onClick={() => void run(() => onOpen(trip.id))}>Открыть<span className="sr-only"> {trip.title || trip.id}</span></button><button className="text-button" disabled={busy} onClick={() => void run(() => removeTrip(trip.id))}>Удалить<span className="sr-only"> {trip.title || trip.id}</span></button></li>)}</ul>
    {!busy && !trips.length && <p>Сохранённых поездок пока нет.</p>}
    {cursor && <button className="text-button" disabled={busy} onClick={() => void run(async () => { const list = await api<{ items: Trip[]; next_cursor: string | null }>(`/api/sessions?limit=20&cursor=${encodeURIComponent(cursor)}`); if (mounted.current) { setTrips(items => [...items, ...list.items.filter(item => !items.some(old => old.id === item.id))]); setCursor(list.next_cursor); } })}>Загрузить ещё поездки</button>}
    <h3>Память о предпочтениях</h3><p>Сохраняются только выбранные вами общие предпочтения. Даты и бюджет каждой поездки задаются заново.</p>
    <label className="memory-optin"><input type="checkbox" checked={applyMemory} onChange={event => onApplyMemory(event.target.checked)}/>Применить сохранённые предпочтения к следующей новой поездке</label>
    <ul className="memory-items">{memory.map(item => <li key={item.field}><strong>{fields[item.field]}</strong><span>{showValue(item.value)}</span><small>Источник: {item.source_trip_id === session.id ? "текущая поездка" : trips.find(trip => trip.id === item.source_trip_id)?.title || "ранее сохранённая поездка"}. Согласие: {when(item.consented_at)}; обновлено: {when(item.updated_at)}</small><button className="text-button" disabled={busy} onClick={() => { setField(item.field); setValue(editValue(item.field, item.value)); }}>Изменить<span className="sr-only"> {fields[item.field]}</span></button><button className="text-button" disabled={busy} onClick={() => void run(async () => { await api(`/api/me/memory/${item.field}`, undefined, { method: 'DELETE' }); if (mounted.current) { setMemory(items => items.filter(saved => saved.field !== item.field)); setNotice('Предпочтение забыто.'); } })}>Забыть<span className="sr-only"> {fields[item.field]}</span></button></li>)}</ul>
    <div className="memory-editor"><label>Предпочтение<select aria-label="Предпочтение" value={field} onChange={event => useCurrent(event.target.value as Field)}>{Object.entries(fields).map(([key, label]) => <option key={key} value={key}>{label}</option>)}</select></label>
      <button className="text-button" disabled={busy} onClick={() => useCurrent(field)}>Взять из текущей поездки</button>
      <label>Значение{field === 'direct_flight_preferred' ? <select aria-label="Значение" value={value} onChange={event => setValue(event.target.value)}><option value="">Выберите</option><option value="да">Да</option><option value="нет">Нет</option></select> : field === "pets" ? <textarea aria-label="Значение" rows={3} value={value} onChange={event => setValue(event.target.value)} placeholder="Одно условие в строке, до пяти. Уточнения сохраняются."/> : <input aria-label="Значение" value={value} onChange={event => setValue(event.target.value)} placeholder={field === 'trip_styles' || field === 'climate_preferences' ? 'Через запятую' : undefined}/>}</label>
      <button className="text-button" disabled={busy || !value} onClick={() => void run(save)}>Сохранить предпочтение с моего согласия</button></div>
    <div className="library-data-actions"><button className="text-button" disabled={busy} onClick={() => void run(exportData)}>Скачать мои данные JSON</button><button className="text-button" disabled={busy} onClick={() => { if (window.confirm('Удалить все ваши поездки, предпочтения и учётную запись? Это действие нельзя отменить.')) void run(async () => { await api('/api/me', undefined, { method: 'DELETE' }); clearDeletedIdentity(); }); }}>Удалить все мои данные</button></div>
  </section>;
}
