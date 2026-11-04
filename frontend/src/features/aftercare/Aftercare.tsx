import { useEffect, useRef, useState } from 'react';
import { api, ApiError } from '../../api';
import type { DemoReceipt, Intent, Option, Session } from '../../types';
import './aftercare.css';

type Receipt = DemoReceipt & { itinerary?: { intent: Intent; option: Option } | null };
type Draft = { id: string; kind: 'early_checkin' | 'service_request'; text: string; status: 'draft' | 'archived'; created_at: string };
type Reminder = { id: string; text: string; state: 'scheduled' | 'canceled' | 'sending' | 'sent' | 'failed' | 'delivery_unknown'; local_time?: string; timezone?: string };
type ManualContext = { destination?: string; hotel?: string; date_from?: string | null; date_to?: string | null };
type AftercareData = { receipts: Receipt[]; drafts: Draft[]; manual_context: ManualContext | null; reminders: Reminder[]; reminders_available: boolean };
const reminderLabels: Record<Reminder['state'], string> = { scheduled: 'Запланировано', canceled: 'Отменено', sending: 'Отправка выполняется', sent: 'Отправлено в Telegram', failed: 'Не удалось отправить', delivery_unknown: 'Доставка не подтверждена' };
const money = (minor: number, currency: string) => new Intl.NumberFormat('ru-RU', { style: 'currency', currency }).format(minor / 100);

export function Aftercare({ session, version, unavailable, onLinkedTrip }: { session: Session; version: number; unavailable: boolean; onLinkedTrip: (trip: Session) => void }) {
  const [data, setData] = useState<AftercareData | null>(null);
  const [busy, setBusy] = useState(true);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [kind, setKind] = useState<Draft['kind']>('early_checkin');
  const [draft, setDraft] = useState('');
  const [manual, setManual] = useState<ManualContext>({ destination: '', hotel: '', date_from: '', date_to: '' });
  const [receiptId, setReceiptId] = useState('');
  const [dateFrom, setDateFrom] = useState(''); const [dateTo, setDateTo] = useState(''); const [request, setRequest] = useState('');
  const [consent, setConsent] = useState(false);
  const [localTime, setLocalTime] = useState('');
  const [timezone, setTimezone] = useState(() => Intl.DateTimeFormat().resolvedOptions().timeZone);
  const [reminderText, setReminderText] = useState('');
  const mounted = useRef(true);
  const loadToken = useRef(0);
  const amendmentKey = useRef<{ scope: string; key: string } | null>(null);
  const path = `/api/sessions/${encodeURIComponent(session.id)}`;
  async function reload() {
    const token = ++loadToken.current;
    const next = await api<AftercareData>(`${path}/aftercare`);
    if (mounted.current && token === loadToken.current) { setData(next); if (next.manual_context) setManual(next.manual_context); }
  }
  useEffect(() => {
    mounted.current = true; setBusy(true);
    void reload().catch(reason => { if (mounted.current) setError((reason as Error).message); }).finally(() => { if (mounted.current) setBusy(false); });
    return () => { mounted.current = false; loadToken.current++; };
  }, [path, version]);
  async function run(work: () => Promise<void>, timeSensitive = false) {
    if (busy || unavailable) return;
    setBusy(true); setError(''); setNotice('');
    try { await work(); } catch (reason) {
      if (mounted.current) setError(timeSensitive && reason instanceof ApiError && reason.status === 422 ? `${reason.message} Проверьте даты, местное время и часовой пояс; для перехода на летнее время выберите другое время.` : (reason as Error).message);
    } finally { if (mounted.current) setBusy(false); }
  }
  const disabled = busy || unavailable;
  const selectedReceipt = data?.receipts.find(item => item.id === receiptId);
  return <section className="aftercare" aria-labelledby="aftercare-title" aria-busy={busy}>
    <h2 id="aftercare-title">Демо-квитанции и заметки к поездке</h2>
    <p className="aftercare-notice">Демо-оплата не создаёт бронирование. Деньги не списаны; билеты, отель и трансфер не забронированы. Заметки и черновики не отправляются поставщикам.</p>
    {error && <p className="aftercare-error" role="alert">{error}</p>}{notice && <p role="status">{notice}</p>}{busy && <p role="status">Загружаем сохранённые данные…</p>}
    <div className="aftercare-receipts">{data?.receipts.map(receipt => <article key={receipt.id} className="aftercare-receipt" aria-label={`Демонстрационная квитанция ${receipt.id}`}>
      <h3>Демонстрационная квитанция</h3><p><strong>Симуляция • реальной брони нет</strong></p>
      <p>{receipt.demo_payment.invoice.destination} · {receipt.demo_payment.invoice.scope_label}</p><strong>{money(receipt.demo_payment.paid_amount_minor, receipt.demo_payment.currency)}</strong>
      <p className="aftercare-id">Квитанция {receipt.id} · {receipt.created_at}</p>
      {receipt.itinerary ? <><p>Снимок прошлого плана. Свежесть цен, наличие и реальные условия не подтверждены.</p><dl><div><dt>Сохранённый маршрут</dt><dd>{receipt.itinerary.intent.origin || 'Отправление неизвестно'} → {receipt.itinerary.intent.destination || 'Направление неизвестно'}</dd></div><div><dt>Сохранённые даты</dt><dd>{receipt.itinerary.intent.date_from || 'Неизвестно'} — {receipt.itinerary.intent.date_to || 'Неизвестно'}</dd></div><div><dt>Сохранённый транспорт</dt><dd>{receipt.itinerary.option.flight.title}</dd></div><div><dt>Сохранённое проживание</dt><dd>{receipt.itinerary.option.stay.title}</dd></div><div><dt>Сохранённый трансфер</dt><dd>{receipt.itinerary.option.transfer.title}</dd></div></dl><details><summary>Сохранённые источники снимка</summary>{receipt.itinerary.option.evidence.length ? <ul>{receipt.itinerary.option.evidence.map((source, index) => <li key={index}>{source.label || source.provider} · {source.freshness} · <time dateTime={source.retrieved_at}>{source.retrieved_at}</time></li>)}</ul> : <p>Источники в этом снимке не указаны.</p>}</details></> : <p>Снимок маршрута этой старой квитанции отсутствует. Реальные условия неизвестны.</p>}
      <details><summary>Вымышленный состав счёта</summary><ul>{receipt.demo_payment.invoice.line_items.map(line => <li key={line.key}>{line.label}: {money(line.amount_minor, receipt.demo_payment.currency)}</li>)}</ul><p>{receipt.demo_payment.invoice.service_fee_label}: {money(receipt.demo_payment.invoice.service_fee_minor, receipt.demo_payment.currency)}</p><p>{receipt.demo_payment.invoice.tax_label}</p></details>
    </article>)}</div>
    {data && !data.receipts.length && <p>Демо-квитанций пока нет. Можно вести свои непроверенные заметки о поездке.</p>}
    <section><h3>Неотправленные черновики</h3><p>Сохранение и архивирование происходят только в приложении. Услуга и ранний заезд не подтверждены.</p>
      <form onSubmit={event => { event.preventDefault(); void run(async () => { await api(`${path}/drafts`, { kind, text: draft.trim() }); if (mounted.current) { setDraft(''); setNotice('Неотправленный черновик сохранён.'); } await reload(); }); }}>
        <label>Тип черновика<select value={kind} onChange={event => setKind(event.target.value as Draft['kind'])}><option value="early_checkin">Ранний заезд</option><option value="service_request">Пожелание по услуге</option></select></label>
        <label>Текст неотправленного черновика<textarea value={draft} onChange={event => setDraft(event.target.value)} maxLength={1500} rows={3}/></label>
        <button className="text-button" disabled={disabled || !draft.trim()}>Сохранить неотправленный черновик</button>
      </form>
      <ul className="aftercare-list">{data?.drafts.map(item => <li key={item.id}><strong>{item.kind === 'early_checkin' ? 'Ранний заезд' : 'Пожелание по услуге'} • {item.status === 'archived' ? 'Архив, не отправлено' : 'Черновик, не отправлено'}</strong><p>{item.text}</p>{item.status === 'draft' && <button className="text-button" disabled={disabled} onClick={() => void run(async () => { await api(`${path}/drafts/${encodeURIComponent(item.id)}`, { status: 'archived' }, { method: 'PATCH' }); await reload(); })}>В архив<span className="sr-only"> {item.text}</span></button>}<button className="text-button" disabled={disabled} onClick={() => { setKind(item.kind); setDraft(item.text); }}>Редактировать копию<span className="sr-only"> {item.text}</span></button></li>)}</ul>
    </section>
    <section><h3>Мои сведения о поездке • не проверены</h3><p>Это сведения пользователя, а не подтверждение брони или факты поставщика. Для сохранения нужны направление, отель и обе даты; неизвестные сведения оставьте пустыми.</p>
      {data?.manual_context && <p role="status">Сохранено пользователем • не проверено: {data.manual_context.destination || 'Направление неизвестно'}, {data.manual_context.hotel || 'Отель неизвестен'}.</p>}
      <form onSubmit={event => { event.preventDefault(); void run(async () => { await api(`${path}/manual-context`, { destination: manual.destination || '', hotel: manual.hotel || '', date_from: manual.date_from || null, date_to: manual.date_to || null }, { method: 'PUT' }); await reload(); if (mounted.current) setNotice('Непроверенные сведения пользователя сохранены.'); }); }}>
        <label>Направление пользователя<input value={manual.destination || ''} onChange={event => setManual(value => ({ ...value, destination: event.target.value }))} maxLength={160}/></label>
        <label>Отель пользователя<input value={manual.hotel || ''} onChange={event => setManual(value => ({ ...value, hotel: event.target.value }))} maxLength={200}/></label>
        <label>Дата начала из моих сведений<input type="date" value={manual.date_from || ''} onChange={event => setManual(value => ({ ...value, date_from: event.target.value }))}/></label>
        <label>Дата окончания из моих сведений<input type="date" value={manual.date_to || ''} onChange={event => setManual(value => ({ ...value, date_to: event.target.value }))}/></label>
        <button className="text-button" disabled={disabled || !manual.destination?.trim() || !manual.hotel?.trim() || !manual.date_from || !manual.date_to}>Сохранить непроверенные сведения</button>
      </form>
    </section>
    <section><h3>Другие даты или трансфер</h3><p>Создадим новую связанную поездку для нового поиска. Старая квитанция остаётся неизменной; существующие брони не меняются.</p>
      <form onSubmit={event => { event.preventDefault(); void run(async () => {
        const body = { ...(receiptId ? { receipt_id: receiptId } : {}), ...(dateFrom ? { date_from: dateFrom } : {}), ...(dateTo ? { date_to: dateTo } : {}), ...(request.trim() ? { request: request.trim() } : {}) };
        const scope = JSON.stringify([session.id, body]); if (amendmentKey.current?.scope !== scope) amendmentKey.current = { scope, key: crypto.randomUUID() };
        const result = await api<{ session: Session; job_id: string }>(`${path}/amend`, body, { idempotencyKey: amendmentKey.current.key });
        amendmentKey.current = null; if (mounted.current) onLinkedTrip(result.session);
      }); }}>
        <label>Контекст демо-квитанции<select value={receiptId} onChange={event => setReceiptId(event.target.value)}><option value="">Текущая поездка / мои сведения</option>{data?.receipts.map(item => <option key={item.id} value={item.id}>{item.demo_payment.invoice.destination} · {item.id}</option>)}</select></label>
        {selectedReceipt && <p>Используем сохранённый контекст демо-квитанции; она не станет реальной бронью.</p>}
        <label>Новая дата начала<input type="date" value={dateFrom} onChange={event => setDateFrom(event.target.value)}/></label><label>Новая дата окончания<input type="date" value={dateTo} onChange={event => setDateTo(event.target.value)}/></label>
        <label>Пожелание для нового поиска, например трансфер<textarea value={request} onChange={event => setRequest(event.target.value)} maxLength={1500} rows={2}/></label>
        <button className="text-button" disabled={disabled || !(dateFrom || dateTo || request.trim())}>Создать новую связанную поездку</button>
      </form>
    </section>
    <section><h3>Напоминания</h3>{data && !data.reminders_available && <p>Напоминания доступны только при настроенной связи с Telegram. Подписка не включена.</p>}
      <form onSubmit={event => { event.preventDefault(); if (!consent || !data?.reminders_available) return; void run(async () => { await api(`${path}/reminders`, { local_time: localTime, timezone, consent: true, text: reminderText.trim() }); if (mounted.current) { setConsent(false); setReminderText(''); setNotice('Напоминание запланировано с вашего согласия.'); } await reload(); }, true); }}>
        <label>Местное время напоминания<input type="datetime-local" value={localTime} onChange={event => setLocalTime(event.target.value)} disabled={!data?.reminders_available}/></label>
        <label>Часовой пояс IANA<input value={timezone} onChange={event => setTimezone(event.target.value)} placeholder="Asia/Nicosia" disabled={!data?.reminders_available}/></label>
        <label>Текст напоминания<textarea value={reminderText} onChange={event => setReminderText(event.target.value)} rows={2} maxLength={500} disabled={!data?.reminders_available}/></label>
        <label className="aftercare-consent"><input type="checkbox" checked={consent} onChange={event => setConsent(event.target.checked)} disabled={!data?.reminders_available}/>Согласен получить это напоминание в Telegram</label>
        <button className="text-button" disabled={disabled || !data?.reminders_available || !consent || !localTime || !timezone.trim() || !reminderText.trim()}>Запланировать напоминание</button>
      </form>
      <ul className="aftercare-list">{data?.reminders.map(item => <li key={item.id}><strong>{reminderLabels[item.state]}</strong><p>{item.text}</p><small>{item.local_time} {item.timezone}</small>{['scheduled', 'sending'].includes(item.state) && <button className="text-button" disabled={disabled} onClick={() => void run(async () => { await api(`${path}/reminders/${encodeURIComponent(item.id)}`, undefined, { method: 'DELETE' }); await reload(); })}>Отменить напоминание<span className="sr-only"> {item.text}</span></button>}</li>)}</ul>
    </section>
  </section>;
}
