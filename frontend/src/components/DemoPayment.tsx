import { useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { ArrowRight, Check, CheckCircle2, CreditCard, LoaderCircle, LockKeyhole, RotateCcw, ShieldCheck, X } from 'lucide-react';
import { api } from '../api';
import type { DemoInvoice, DemoReceipt, Option, Session } from '../types';
import '../demo-payment.css';

const amount = (minor: number, currency = 'EUR') => new Intl.NumberFormat('ru-RU', { style: 'currency', currency, minimumFractionDigits: 2 }).format(minor / 100);

function InvoiceLines({ invoice }: { invoice: DemoInvoice }) {
  return <div className="demo-invoice-lines">
    <dl>{invoice.line_items.map(line => <div className="demo-line" key={line.key}><dt>{line.label}<small>{line.detail}</small></dt><dd>{amount(line.amount_minor, invoice.currency)}</dd></div>)}
      <div className="demo-line demo-fee"><dt>{invoice.service_fee_label}</dt><dd>{amount(invoice.service_fee_minor, invoice.currency)}</dd></div>
      <div className="demo-line demo-total"><dt>Итого в демо</dt><dd>{amount(invoice.total_minor, invoice.currency)}</dd></div>
    </dl><p className="demo-tax"><Check size={14} aria-hidden="true"/>{invoice.tax_label}</p>
  </div>;
}

export function DemoPayment({ session, option, unavailable, onBusyChange, onReceiptCreated }: {
  session: Session; option: Option; unavailable: boolean; onBusyChange: (busy: boolean) => void; onReceiptCreated?: () => void;
}) {
  const [invoice, setInvoice] = useState<DemoInvoice | null>(null);
  const [receipt, setReceipt] = useState<DemoReceipt | null>(null);
  const [loading, setLoading] = useState(false);
  const [paying, setPaying] = useState(false);
  const [open, setOpen] = useState(false);
  const [error, setError] = useState('');
  const [attempt, setAttempt] = useState(0);
  const modal = useRef<HTMLElement>(null);
  const mounted = useRef(true);
  const inFlight = useRef(false);
  const scope = JSON.stringify({ session: session.id, revision: session.revision, intent: session.intent, option });
  const currentScope = useRef(scope);
  currentScope.current = scope;

  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; onBusyChange(false); };
  }, [onBusyChange]);

  useEffect(() => {
    let cancelled = false;
    setInvoice(null); setReceipt(null); setOpen(false); setError('');
    if (unavailable) { setLoading(false); return; }
    setLoading(true);
    void api<DemoInvoice>(`/api/sessions/${session.id}/checkout/quote`, { option_id: option.id, expected_revision: session.revision })
      .then(value => { if (!cancelled) setInvoice(value); })
      .catch(reason => { if (!cancelled) setError((reason as Error).message); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [scope, unavailable, attempt]);

  useEffect(() => {
    if (!open) return;
    const previous = document.activeElement as HTMLElement | null;
    const overflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    modal.current?.querySelector<HTMLElement>('button')?.focus();
    const keydown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') { event.preventDefault(); setOpen(false); }
      if (event.key !== 'Tab') return;
      const controls = Array.from(modal.current?.querySelectorAll<HTMLElement>('button, input, summary, a[href], [tabindex="0"]') || []).filter(control => !control.hasAttribute('disabled'));
      const first = controls[0], last = controls.at(-1);
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
    };
    document.addEventListener('keydown', keydown);
    return () => { document.removeEventListener('keydown', keydown); document.body.style.overflow = overflow; if (previous?.isConnected) previous.focus(); };
  }, [open]);

  useEffect(() => { if (receipt && open) modal.current?.querySelector<HTMLElement>('[data-receipt-title]')?.focus(); }, [receipt, open]);

  function reviewAgain() { setOpen(false); setAttempt(value => value + 1); }
  function openPayment() {
    if (!invoice || unavailable || inFlight.current) return;
    if (!receipt && Date.parse(invoice.expires_at) <= Date.now()) { setError('Срок демо-счёта истёк. Обновите счёт перед оплатой.'); return; }
    setOpen(true);
  }
  async function pay() {
    if (!invoice || unavailable || inFlight.current || receipt) return;
    const expectedScope = scope;
    inFlight.current = true; setPaying(true); setError(''); onBusyChange(true);
    try {
      const result = await api<DemoReceipt>(`/api/sessions/${session.id}/checkout`, {
        option_id: option.id, expected_revision: session.revision, quote_id: invoice.quote_id,
      });
      if (mounted.current && currentScope.current === expectedScope) { setReceipt(result); onReceiptCreated?.(); }
    } catch (reason) {
      if (mounted.current && currentScope.current === expectedScope) setError((reason as Error).message);
    } finally {
      inFlight.current = false;
      if (mounted.current) { setPaying(false); onBusyChange(false); }
    }
  }

  return <section className="demo-payment" aria-label="Демонстрационный счёт и оплата">
    <span className="demo-badge"><CreditCard size={14} aria-hidden="true"/> ДЕМО ПОКУПКИ</span>
    <h3>Пройдите путь до оплаты</h3>
    <p className="demo-separation">{option.demo_pricing ? 'Здесь — примерная сумма демо-оценки и симуляция оплаты. Реальные тарифы не подтверждены.' : 'Цены из источников показаны выше. Здесь — вымышленный счёт и симуляция оплаты.'}</p>
    {loading && <p className="demo-loading" role="status"><LoaderCircle size={16} className="spin"/> Готовим демо-счёт…</p>}
    {unavailable && <p className="demo-separation">Дождитесь завершения обновления и подключения, чтобы открыть демо-счёт.</p>}
    {invoice && <><p className="demo-scope">{invoice.scope_label}</p><InvoiceLines invoice={invoice}/>
      <button className="primary-button demo-book-button" disabled={unavailable || paying || !!error} onClick={openPayment}>
        <span>{receipt ? 'Открыть демо-квитанцию' : `Забронировать в демо · ${amount(invoice.total_minor, invoice.currency)}`}</span><ArrowRight size={17}/>
      </button><p className="demo-disclaimer">Все суммы вымышленные. Деньги не списываются, реальной брони нет.</p>
      {receipt && <p className="demo-paid" role="status"><CheckCircle2 size={16}/> Демо-оплата завершена</p>}
    </>}
    {error && <div className="demo-error" role="alert"><p>{error}</p><button className="demo-retry" onClick={reviewAgain} disabled={unavailable || paying}><RotateCcw size={15}/> Обновить демо-счёт</button></div>}
    {open && invoice && createPortal(<div className="modal-overlay demo-overlay" onClick={() => setOpen(false)}>
      <section ref={modal} className="modal demo-modal" role="dialog" aria-modal="true" aria-labelledby="demo-payment-title" aria-describedby="demo-payment-disclaimer" onClick={event => event.stopPropagation()}>
        <button className="modal-close icon-button" onClick={() => setOpen(false)} aria-label="Закрыть окно демо-оплаты"><X size={20}/></button>
        <ol className="demo-steps" aria-label="Шаги оформления"><li className="done"><Check size={13}/> Счёт</li><li className={receipt ? 'done' : 'current'}>{receipt ? <Check size={13}/> : '2'} Демо-оплата</li><li className={receipt ? 'current' : ''}>3 Квитанция</li></ol>
        {receipt ? <>
          <span className="demo-success-icon"><CheckCircle2 size={32}/></span>
          <h2 id="demo-payment-title" tabIndex={-1} data-receipt-title>Демо-оплата прошла</h2>
          <p className="demo-receipt-amount">{amount(receipt.demo_payment.paid_amount_minor, receipt.demo_payment.currency)}</p>
          <p id="demo-payment-disclaimer" className="demo-notice">Деньги не списаны. Билеты, отель и трансфер не забронированы. Вы прошли демонстрационный сценарий покупки.</p>
          <dl className="demo-receipt-details"><div><dt>Маршрут</dt><dd>{receipt.demo_payment.invoice.destination}</dd></div><div><dt>Демо-квитанция</dt><dd>{receipt.id}</dd></div><div><dt>Время (Кипр)</dt><dd>{new Intl.DateTimeFormat('ru-RU', { dateStyle: 'short', timeStyle: 'short', timeZone: 'Asia/Nicosia' }).format(new Date(receipt.created_at))}</dd></div></dl>
          <details className="demo-receipt-breakdown"><summary>Состав демо-счёта</summary><InvoiceLines invoice={receipt.demo_payment.invoice}/></details>
          <button className="primary-button" onClick={() => setOpen(false)}>Вернуться к путешествию <ArrowRight size={16}/></button>
        </> : <>
          <span className="demo-badge"><ShieldCheck size={14}/> СИМУЛЯЦИЯ · БЕЗ СПИСАНИЯ</span>
          <h2 id="demo-payment-title">Оплата демо-поездки</h2><p className="demo-destination">{invoice.destination} · {invoice.scope_label}</p>
          <InvoiceLines invoice={invoice}/>
          <p id="demo-payment-disclaimer" className="demo-notice">{invoice.disclaimer}</p>
          <div className="demo-card" aria-label="Вымышленная карта, данные изменять не нужно"><span><CreditCard size={20}/> ТЕСТОВАЯ КАРТА</span>
            <label htmlFor="demo-card-number">Номер карты<input id="demo-card-number" value="1234 5678 9012 3456" readOnly autoComplete="off"/></label>
            <div className="demo-card-row"><label htmlFor="demo-card-holder">Держатель<input id="demo-card-holder" value="DEMO TRAVELER" readOnly autoComplete="off"/></label><label htmlFor="demo-card-expiry">Срок<input id="demo-card-expiry" value="12/30" readOnly autoComplete="off"/></label><label htmlFor="demo-card-cvc">CVC<input id="demo-card-cvc" value="123" readOnly autoComplete="off"/></label></div>
          </div><p className="demo-card-helper"><LockKeyhole size={14}/> Карта вымышленная. Поля заполнены и не редактируются.</p>
          <details className="demo-real-conditions"><summary>Что ещё неизвестно о реальной поездке</summary><ul>{invoice.unknown_real_conditions.map((condition, index) => <li key={index}>{condition}</li>)}</ul></details>
          {error && <div className="demo-error" role="alert"><p>{error}</p><button className="demo-retry" onClick={reviewAgain} disabled={paying}><RotateCcw size={15}/> Обновить демо-счёт</button></div>}
          <button className="primary-button demo-pay-button" onClick={() => void pay()} disabled={paying || unavailable || !!error}>{paying ? <><LoaderCircle className="spin" size={17}/> Подтверждаем демо-оплату…</> : <><span>Оплатить {amount(invoice.total_minor, invoice.currency)} в демо</span><LockKeyhole size={16}/></>}</button>
          <p className="demo-disclaimer" role={paying ? 'status' : undefined}>{paying ? 'Выполняется только симуляция на нашем сервере.' : 'Нажатие создаст только демо-квитанцию.'}</p>
        </>}
      </section>
    </div>, document.body)}
  </section>;
}
