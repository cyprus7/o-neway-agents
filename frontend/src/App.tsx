import { useEffect, useRef, useState } from 'react';
import { ArrowDown, ArrowRight, ArrowUpRight, Check, ChevronDown, CircleHelp, Compass, ExternalLink, Globe2, Leaf, LoaderCircle, MapPin, MessageCircle, Plane, Plus, Send, ShieldCheck, Sparkles, Sun, TrainFront, Wallet, X, Hotel, Clock3, CheckCircle2, AlertCircle, RotateCcw, PawPrint } from 'lucide-react';
import { api, ApiError, AUTH_CHANGED, completeIdentityBootstrap, currentIdentity, ensureIdentity, failIdentityBootstrap, logout } from './api';
import { canCloseTelegram, closeTelegram, telegramInitData, telegramTripHint, useTelegramBridge } from './telegram/bridge';
import type { Component, Config, DemoComponentPrice, Evidence, Option, Session } from './types';
import { ComponentPrice, PricingSummary, demoComponent } from './components/PricingSummary';
import { RegionalEvents } from './components/RegionalEvents';
import { DemoPayment } from './components/DemoPayment';
import { DecisionWorkspace, PreviousPlan, optionTitle } from './features/comparison/DecisionWorkspace';
import type { EvidenceAction } from './features/comparison/DecisionWorkspace';
import { TripLibrary } from './features/library/TripLibrary';
import { Aftercare } from './features/aftercare/Aftercare';
import { CatalogFacts } from './components/CatalogFacts';
import { FlightSearchDates, StayAndFlightDates } from './components/TravelDates';
import { CheckActivity, PriceRefreshButton } from './components/CheckActivity';
import { checkActivity } from './live/checkActivity';
import type { CheckActivityState } from './live/checkActivity';
import './pricing.css';
import { useLiveSession } from './live/useLiveSession';
import { ConnectionStatus } from './live/ConnectionStatus';
import { demoExamples } from './demoExamples';

const examples = demoExamples().map(example => ({ ...example,
  icon: example.id === 'sea' ? Sun : example.id === 'mountains' ? Leaf : TrainFront,
}));
const money = (value: number | null | undefined, currency = 'EUR') => value == null ? null : new Intl.NumberFormat('ru-RU', { style: 'currency', currency, maximumFractionDigits: 0 }).format(value);
const timestamp = (value: string) => new Intl.DateTimeFormat('ru-RU', { day: 'numeric', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit', timeZone: 'Asia/Nicosia' }).format(new Date(value));
const date = (value?: string | null) => value ? new Intl.DateTimeFormat('ru-RU', { day: 'numeric', month: 'short' }).format(new Date(value)) : '';
const labels: Record<string, string> = { live: 'Данные источника', cached: 'Из кеша', snapshot: 'Справочные данные', unavailable: 'Источник недоступен', observed: 'Найденная цена', partial: 'Часть цены', estimated: 'Расчётная оценка' };
const safeUrl = (url?: string | null) => url && /^https?:\/\//i.test(url) ? url : undefined;

function SourceList({ evidence }: { evidence: Evidence[] }) {
  return <details className="sources"><summary><ShieldCheck size={13} /> Источники и актуальность <span>{evidence.length}</span><ChevronDown size={13} /></summary><div className="source-list">{evidence.length ? evidence.map((e, i) => <a key={i} href={safeUrl(e.source_url)} target="_blank" rel="noreferrer"><span><strong>{e.label || e.provider}</strong><small>{labels[e.freshness] || e.freshness} · {timestamp(e.retrieved_at)}</small></span><ArrowUpRight size={14} /></a>) : <p>Источники пока не получены.</p>}</div></details>;
}

function Landscape({ index = 0, large = false }: { index?: number; large?: boolean }) {
  return <div className={`landscape landscape-${index % 3} ${large ? 'large' : ''}`} aria-hidden="true"><span className="land-sun"/><span className="land-mountain back"/><span className="land-mountain front"/><span className="land-water"/><span className="land-path"/><span className="land-grid"/></div>;
}

function WeatherBadge({ option }: { option: Option }) {
  const weather = option.weather;
  const temperature = weather?.temperature_max;
  const duration = option.flight.details?.duration_minutes;
  return <div className="option-facts"><span title={String(weather?.summary || 'Погодные данные не получены')}><Sun size={14}/>{typeof temperature === 'number' ? <>{Math.round(temperature)} °C <small>{weather?.kind === 'forecast' ? 'прогноз' : 'прошлый год'}</small></> : 'Погода уточняется'}</span><span><Clock3 size={13}/>{typeof duration === 'number' ? `${Math.floor(duration / 60)} ч ${duration % 60} мин` : 'Время в пути неизвестно'}</span></div>;
}

function Constraints({ option }: { option: Option }) {
  const checks = demoComponent(option, 'flight') ? option.constraint_checks.filter(check => check.status === 'passed' || check.status === 'failed') : option.constraint_checks;
  if (!checks.length) return null;
  return <section className="constraint-section"><h3><ShieldCheck size={17}/> Проверка условий и состыковок</h3><div className="constraint-grid">{checks.map((check, i) => <div className={`constraint constraint-${check.status}`} key={i}><span className="constraint-symbol">{check.status === 'passed' ? <CheckCircle2 size={15}/> : check.status === 'failed' ? <X size={15}/> : <CircleHelp size={15}/>}</span><div><strong>{String(check.constraint || 'Условие')}</strong><span className="constraint-state">{check.status === 'passed' ? 'Проверено' : check.status === 'failed' ? 'Не выполняется' : 'Требует проверки'}</span><p>{String(check.detail || '')}</p></div></div>)}</div></section>;
}

function OptionCard({ option, title, index, selected, onSelect, onRefresh, activity, disabled }: { option: Option; title: string; index: number; selected: boolean; onSelect: () => void; onRefresh: () => void; activity: CheckActivityState; disabled: boolean }) {
  return <article className={`option-card ${selected ? 'selected' : ''}`}>
    <div className="card-visual"><Landscape index={index}/><span className="option-tag">{index === 0 ? <><Sparkles size={12}/> Первый в подборке</> : index === 1 ? 'Ещё один взгляд' : 'План с характером'}</span><span className="visual-caption">{option.country}</span></div>
    <div className="card-content"><div className="destination-line"><h3>{title}</h3><span className="match-score" title="Соответствие вашим предпочтениям">{Math.round(option.score)}<small> / 100</small></span></div><p className="card-subtitle">{option.subtitle || option.country}</p>
      <WeatherBadge option={option}/>
      <div className="card-price"><PricingSummary option={option} compact/></div>
      <ul className="reasons">{option.reasons.slice(0, 2).map((reason, i) => <li key={i}><Check size={14}/>{reason}</li>)}</ul>
      {option.tradeoffs[0] && <p className="tradeoff"><ArrowDown size={13}/>{option.tradeoffs[0]}</p>}
      <CheckActivity option={option} activity={activity}/>
      <SourceList evidence={option.evidence}/>
      <PriceRefreshButton activity={activity} onClick={onRefresh} disabled={disabled} idleLabel="Проверить цены" ariaLabel={`Проверить цены: ${option.destination}`}/>
      <button className={`choose-button ${selected ? 'chosen' : ''}`} aria-pressed={selected} disabled={disabled} onClick={onSelect}>{selected ? <><CheckCircle2 size={15}/> Выбранный маршрут</> : <>Посмотреть маршрут <ArrowRight size={15}/></>}</button>
    </div>
  </article>;
}

function ItineraryPart({ part, kind, demo }: { part: Component; kind: 'flight' | 'stay' | 'transfer'; demo?: DemoComponentPrice }) {
  const Icon = kind === 'flight' ? part.details?.transport_mode === 'train' ? TrainFront : Plane : kind === 'stay' ? Hotel : MapPin;
  return <div className="itinerary-part"><span className={`part-icon ${kind}`}><Icon size={18}/></span><div className="part-description"><small>{kind === 'flight' ? 'Как добраться' : kind === 'stay' ? 'Где остановиться' : 'Последние километры'}</small><h4>{part.title}</h4>{kind === 'flight' && <FlightSearchDates part={part}/>}{part.description !== part.quote?.reason && (demo ? <details className="price-context"><summary>Описание источника</summary><p>{part.description}</p></details> : <p>{part.description}</p>)}{kind === 'stay' && <CatalogFacts part={part}/>}<ComponentPrice part={part} demo={demo}/>{part.details?.discovery_mode !== 'local_catalog' && !part.quote && safeUrl(part.source_url) && <a href={safeUrl(part.source_url)} target="_blank" rel="noreferrer">Открыть источник <ExternalLink size={11}/></a>}</div></div>;
}

export default function AuthenticatedApp() {
  const [epoch, setEpoch] = useState(0);
  const [signedOut, setSignedOut] = useState(false);
  useEffect(() => {
    const changed = (event: Event) => { setSignedOut(!!(event as CustomEvent).detail?.signedOut); setEpoch(value => value + 1); };
    window.addEventListener(AUTH_CHANGED, changed);
    return () => window.removeEventListener(AUTH_CHANGED, changed);
  }, []);
  return signedOut ? <main className="loading-state"><h1>Сеанс завершён</h1><p>Ваши поездки скрыты до следующего входа.</p><button className="primary-button" onClick={() => setSignedOut(false)}>Начать новый сеанс</button></main> : <App key={epoch}/>;
}

function App() {
  const [initialSession, setInitialSession] = useState<Session | null>(null);
  const [config, setConfig] = useState<Config | null>(null);
  const [input, setInput] = useState('');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [bootstrapFailed, setBootstrapFailed] = useState(false);
  const [telegramAuthFailed, setTelegramAuthFailed] = useState(false);
  const [checkoutBusy, setCheckoutBusy] = useState(false);
  const [mobileChat, setMobileChat] = useState(false);
  const [showAbout, setShowAbout] = useState(false);
  const [libraryOpen, setLibraryOpen] = useState(false);
  const [aftercareOpen, setAftercareOpen] = useState(false);
  const [receiptVersion, setReceiptVersion] = useState(0);
  const [applyMemory, setApplyMemory] = useState(false);
  const drafts = useRef(new Map<string, string>());
  const endRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const mounted = useRef(true);
  const activeSessionId = useRef<string | null>(null);
  const restoreComposer = useRef(false);
  const retryMessage = useRef<{ scope: string; key: string } | null>(null);
  const retryEvidence = useRef<{ scope: string; key: string } | null>(null);
  const live = useLiveSession(initialSession);
  const { session, busy, workKind } = live;
  const connectionReady = live.connection === 'live';
  const reportedJob = useRef('');
  const selected = session?.options.find(o => o.id === session.selected_option_id);
  useTelegramBridge(!loading && !!session && connectionReady, () => {
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));
    setShowAbout(false); setMobileChat(false); setLibraryOpen(false); setAftercareOpen(false);
  }, !loading && bootstrapFailed && !!error);

  useEffect(() => {
    mounted.current = true;
    let cancelled = false;
    void (async () => {
      try {
        await ensureIdentity();
        if (cancelled) return;
        const configuration = await api<Config>('/api/config');
        if (!cancelled) setConfig(configuration);
        const linkedTrip = telegramTripHint() || new URLSearchParams(window.location.search).get('trip');
        const saved = linkedTrip || localStorage.getItem('one-request-session');
        let current: Session;
        if (saved) { try { current = await api<Session>(`/api/sessions/${encodeURIComponent(saved)}`); } catch (reason) { if (!(reason instanceof ApiError) || reason.status !== 404) throw reason; if (linkedTrip) throw new ApiError('Поездка недоступна для текущего пользователя. Начните новую поездку.', 404, 'trip_unavailable'); current = await api<Session>('/api/sessions', {}); } }
        else current = await api<Session>('/api/sessions', {});
        if (!cancelled) {
          activeSessionId.current = current.id; setInitialSession(current); localStorage.setItem('one-request-session', current.id);
          localStorage.removeItem('one-request-job');
          completeIdentityBootstrap();
        }
      } catch (e) { if (!cancelled) { failIdentityBootstrap(); setBootstrapFailed(true);
        const telegramFailure = !!telegramInitData() && e instanceof ApiError && e.status === 401;
        setTelegramAuthFailed(telegramFailure);
        setError(telegramFailure ? (e.code === 'telegram_launch_expired' ? 'Срок действия запуска Telegram истёк. Закройте Mini App и откройте его заново через кнопку в Telegram.' : (e as Error).message + ' Закройте Mini App и откройте его заново через кнопку в Telegram.') : (e as Error).message); } }
      finally { if (!cancelled) setLoading(false); }
    })();
    return () => { cancelled = true; mounted.current = false; };
  }, []);
  useEffect(() => {
    if (live.connection === 'reconnecting') void currentIdentity().catch(() => {});
  }, [live.connection]);
  useEffect(() => {
    if (restoreComposer.current && !busy && connectionReady) {
      restoreComposer.current = false;
      inputRef.current?.focus();
    }
  }, [busy, connectionReady]);
  useEffect(() => {
    const job = live.job;
    if (!job || job.status === 'queued' || job.status === 'running') return;
    const outcome = `${job.session_id}:${job.id}:${job.status}`;
    if (reportedJob.current === outcome) return;
    reportedJob.current = outcome;
    if (job.status === 'completed') setMobileChat(false);
    else if (job.status === 'failed') setError(job.error || 'Не удалось завершить задачу. Попробуйте ещё раз.');
    else if (job.status === 'canceled') setError('Задание отменено. Последний план сохранён.');
    else if (job.status === 'interrupted') setError('Работа прервана перезапуском. Отправьте запрос снова, чтобы продолжить.');
    else setError('Параметры поездки изменились. Повторите проверку цен для текущего маршрута.');
  }, [live.job]);
  useEffect(() => { endRef.current?.scrollIntoView({ behavior: 'smooth', block: 'nearest' }); }, [session?.messages.length, busy]);
  useEffect(() => {
    if (!showAbout && !(mobileChat && window.matchMedia('(max-width:680px)').matches)) return;
    const previous = document.activeElement as HTMLElement | null;
    const dialog = document.querySelector<HTMLElement>('[role="dialog"]');
    if (mobileChat && !showAbout) inputRef.current?.focus();
    else dialog?.querySelector<HTMLElement>('button')?.focus();
    const handleKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') { setShowAbout(false); setMobileChat(false); setLibraryOpen(false); setAftercareOpen(false); }
      if (event.key !== 'Tab' || !dialog) return;
      const focusable = Array.from(dialog.querySelectorAll<HTMLElement>('button, a[href], input, textarea, [tabindex="0"]')).filter(element => !element.hasAttribute('disabled'));
      const first = focusable[0], last = focusable.at(-1);
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
    };
    document.addEventListener('keydown', handleKey);
    return () => { document.removeEventListener('keydown', handleKey); previous?.focus(); };
  }, [showAbout, mobileChat]);

  async function newTrip(preserveDraft = true) {
    try {
      setError('');
      const current = await api<Session>('/api/sessions', { apply_memory: applyMemory });
      if (!mounted.current) return;
      completeIdentityBootstrap(); setBootstrapFailed(false);
      if (session && preserveDraft) drafts.current.set(session.id, input);
      activeSessionId.current = current.id; setInitialSession(current); localStorage.setItem('one-request-session', current.id); setInput(''); setApplyMemory(false); setLibraryOpen(false); setAftercareOpen(false);
      const url = new URL(window.location.href); url.searchParams.set('trip', current.id); window.history.replaceState(null, '', url);
    } catch (e) { if (mounted.current) setError((e as Error).message); }
  }
  async function openTrip(id: string) {
    if (busy || checkoutBusy) throw new Error('Дождитесь завершения текущего действия.');
    const current = await api<Session>(`/api/sessions/${encodeURIComponent(id)}`);
    if (!mounted.current) return;
    adoptTrip(current);
  }
  function adoptTrip(current: Session) {
    completeIdentityBootstrap(); setBootstrapFailed(false);
    if (session) drafts.current.set(session.id, input);
    activeSessionId.current = current.id; setInitialSession(current); setInput(drafts.current.get(current.id) || '');
    localStorage.setItem('one-request-session', current.id);
    const url = new URL(window.location.href); url.searchParams.set('trip', current.id); window.history.replaceState(null, '', url);
  }
  async function deletedTrip(id: string) {
    drafts.current.delete(id);
    if (session?.id !== id) return;
    activeSessionId.current = null; setInitialSession(null); setInput(''); localStorage.removeItem('one-request-session');
    await newTrip(false);
  }
  async function send(content = input) {
    if (!content.trim() || busy || checkoutBusy || !session || !connectionReady) return;
    const sessionId = session.id;
    const command = live.beginCommand('message');
    if (!command) return;
    const scope = JSON.stringify([sessionId, session.revision, content.trim()]);
    if (retryMessage.current?.scope !== scope) retryMessage.current = { scope, key: crypto.randomUUID() };
    const key = retryMessage.current.key;
    restoreComposer.current = true;
    setError(''); setInput(''); setMobileChat(true);
    try {
      const accepted = await api<{ job_id: string; sequence: number }>(`/api/sessions/${sessionId}/messages`, { content: content.trim(), expected_revision: session.revision }, { idempotencyKey: key });
      live.acknowledge(command, accepted.job_id, accepted.sequence);
      retryMessage.current = null;
    } catch (e) {
      live.reject(command);
      if (mounted.current && activeSessionId.current === sessionId) { setError((e as Error).message); setInput(content); }
    }
  }
  async function refreshPrices(optionId?: string) {
    if (!session || busy || checkoutBusy || !connectionReady) return;
    const sessionId = session.id;
    const command = live.beginCommand('prices_refresh', optionId);
    if (!command) return;
    setError('');
    try {
      const accepted = await api<{ job_id: string; sequence: number }>(`/api/sessions/${sessionId}/prices/refresh`, { expected_revision: session.revision, ...(optionId ? { option_id: optionId } : {}) });
      live.acknowledge(command, accepted.job_id, accepted.sequence);
    } catch (e) {
      live.reject(command);
      if (mounted.current && activeSessionId.current === sessionId) setError((e as Error).message);
    }
  }
  async function requestEvidence(action: EvidenceAction, ids: string[]) {
    if (!session || busy || checkoutBusy || !connectionReady) return;
    const sessionId = session.id;
    if (!ids.length || ids.length > 3 || ids.some(id => !session.options.some(option => option.id === id))) return;
    const command = live.beginCommand('message');
    if (!command) return;
    const scope = JSON.stringify([sessionId, session.revision, action, ids]);
    if (retryEvidence.current?.scope !== scope) retryEvidence.current = { scope, key: crypto.randomUUID() };
    const key = retryEvidence.current.key;
    setError(''); setMobileChat(true);
    try {
      const accepted = await api<{ job_id: string; sequence: number }>(`/api/sessions/${sessionId}/messages`, {
        content: action === 'compare' ? 'Сравни выбранные варианты по сохранённым источникам.' : 'Объясни этот вариант по сохранённым источникам.',
        action, option_ids: ids, expected_revision: session.revision,
      }, { idempotencyKey: key });
      live.acknowledge(command, accepted.job_id, accepted.sequence);
      retryEvidence.current = null;
    } catch (reason) {
      live.reject(command);
      if (mounted.current && activeSessionId.current === sessionId) setError((reason as Error).message);
    }
  }
  async function select(option: Option) {
    if (!session || !connectionReady) return;
    const sessionId = session.id;
    try {
      setError('');
      // Commands never replace stream state: an older HTTP response may arrive last.
      await api<Session>(`/api/sessions/${sessionId}/select`, { option_id: option.id, expected_revision: session.revision });
    } catch (e) { if (mounted.current && activeSessionId.current === sessionId) setError((e as Error).message); }
  }
  const intent = session?.intent;
  const hasMessages = !!session?.messages.length;
  const hasOptions = !!session?.options.length;
  const priceActivity = checkActivity(live.activityState);
  const selectedActivity = selected ? checkActivity(live.activityState, selected.id) : null;
  const visualBusy = busy && (workKind === 'prices' ? priceActivity.active : connectionReady);
  const activeJob = live.job?.status === 'queued' || live.job?.status === 'running' ? live.job : null;
  const currentWorkLabel = workKind === 'prices' ? priceActivity.label || 'Синхронизируем состояние обновления цен…'
    : !connectionReady ? 'Восстанавливаем состояние задачи…' : activeJob?.progress || 'Передаём запрос планировщику…';
  const latestEvents = session?.events.slice(-5) || [];
  const guests = intent ? intent.adults + intent.children_ages.length : 0;
  const preferenceCopy = session?.preferences ? Object.entries(session.preferences).filter(([, value]) => value && (!Array.isArray(value) || value.length)).map(([key, value]) => `${({origin:'Город отправления',currency:'Валюта',trip_styles:'Формат отдыха',climate_preferences:'Климат',hotel_requirements:'Проживание',pets:'Питомцы',direct_flight_preferred:'Предпочтение прямого рейса'} as Record<string,string>)[key] || key}: ${Array.isArray(value) ? value.join(', ') : value === true ? 'да' : String(value)}`).join('; ') : '';

  return <div className="app-shell">
    <aside className={`conversation ${mobileChat ? 'mobile-open' : ''}`} role={mobileChat && window.matchMedia('(max-width:680px)').matches ? 'dialog' : undefined} aria-modal={mobileChat && window.matchMedia('(max-width:680px)').matches ? true : undefined} aria-label="Помощник по путешествиям">
      <a className="brand" href="/" aria-label="Один запрос — главная"><span className="brand-mark"><Compass size={23}/></span><span>один запрос<small>путешествие начинается с вас</small></span></a>
      <button className="new-trip" onClick={() => void newTrip()} disabled={busy || checkoutBusy || loading || telegramAuthFailed}><Plus size={16}/> Новая поездка <span>↗</span></button>
      <button className="text-button" disabled={checkoutBusy || loading || !session} onClick={() => { void logout().catch(reason => setError((reason as Error).message)); }}>Завершить сеанс</button>
      <div className="conversation-heading"><span>ВАШ ТРЕВЕЛ-ПОМОЩНИК</span><span className="connection-dot" title={config?.llm_configured ? 'LLM подключён' : 'LLM не настроен'}/><button className="mobile-close icon-button" onClick={() => setMobileChat(false)} aria-label="Закрыть чат"><X size={18}/></button></div>
      <div className="messages" aria-live="polite"><div className="assistant-intro"><span className="mini-avatar"><Sparkles size={16}/></span><p>Привет! Каким вы представляете ваше следующее путешествие?<br/><span>Расскажите о желаниях, а детали соберём вместе.</span></p></div>
        {session?.messages.map(message => <div key={message.id} className={`message ${message.role}`}><small>{message.role === 'user' ? 'Вы' : 'Один запрос'}</small><p>{message.content}</p></div>)}
        {busy && <div className="thinking">{visualBusy ? <LoaderCircle className="spin" size={15}/> : <RotateCcw size={15}/>}<span>{currentWorkLabel}</span></div>}
        <div ref={endRef}/>
      </div>
      <form className="composer" onSubmit={e => { e.preventDefault(); void send(); }}><label className="sr-only" htmlFor="request">Расскажите о путешествии</label><textarea id="request" ref={inputRef} value={input} onChange={e => setInput(e.target.value)} placeholder={hasMessages ? 'А если изменить планы?..' : 'Хочу к морю, в горы, в новый город…'} rows={3} maxLength={4000} disabled={busy || checkoutBusy || loading || !connectionReady} onKeyDown={e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); void send(); } }}/><div className="composer-bottom"><span>Можно просто своими словами</span><button className="send-button" type="submit" aria-label="Отправить запрос" disabled={busy || checkoutBusy || loading || !connectionReady || !input.trim()}>{visualBusy ? <LoaderCircle size={17} className="spin"/> : <ArrowRight size={18}/>}</button></div></form>
      <div className="sidebar-footer"><ShieldCheck size={13}/><span>Ваши планы сохраняются локально</span></div>
    </aside>

    <main className="workspace">
      <header className="topbar"><div className="breadcrumb"><Globe2 size={15}/><span>Мастерская путешествий</span><span className="breadcrumb-slash">/</span><strong>{hasOptions ? 'Ваши варианты' : 'Новая история'}</strong></div><button className="about-button" aria-expanded={libraryOpen} onClick={() => setLibraryOpen(value => !value)} disabled={!session || busy || checkoutBusy}>Мои поездки</button><button className="about-button" aria-expanded={aftercareOpen} onClick={() => setAftercareOpen(value => !value)} disabled={!session || checkoutBusy}>Квитанции и заметки</button><button className="about-button" onClick={() => setShowAbout(true)}><CircleHelp size={15}/><span>Как это работает</span></button></header>
      <div className="workspace-content">
        {libraryOpen && session && <TripLibrary session={session} applyMemory={applyMemory} onApplyMemory={setApplyMemory} onOpen={openTrip} onDeleted={deletedTrip}/>}
        {aftercareOpen && session && <Aftercare key={session.id} session={session} version={receiptVersion} unavailable={busy || checkoutBusy || !connectionReady} onLinkedTrip={current => { adoptTrip(current); setAftercareOpen(false); }}/>}
        {session && <ConnectionStatus status={live.connection} retry={live.retry}/>}
        {error && <div className="error-banner" role="alert"><AlertCircle size={18}/><span>{error}</span>{telegramAuthFailed && canCloseTelegram() && <button className="text-button" onClick={closeTelegram}>Закрыть приложение</button>}{bootstrapFailed && !telegramAuthFailed && <button className="text-button" onClick={() => window.location.reload()}>Повторить вход</button>}<button className="icon-button" onClick={() => setError('')} aria-label="Закрыть ошибку"><X size={15}/></button></div>}
        {loading ? <div className="loading-state"><LoaderCircle className="spin" size={24}/><p>Открываем мастерскую путешествий…</p></div> : <>
          <section className={`intro ${hasOptions ? 'compact' : ''}`}><div className="eyebrow"><span/> МЕНЬШЕ ПОИСКА. БОЛЬШЕ ПУТЕШЕСТВИЙ.</div><h1>{hasOptions ? <>У ваших планов<br/><em>появился маршрут.</em></> : <>Куда бы вы ни хотели.<br/><em>Начните с одного запроса.</em></>}</h1><p className="intro-description">{hasOptions ? 'Сравните варианты, выберите близкий вам — и меняйте детали по ходу.' : 'Расскажите, чего хочется. Мы подберём направление, соберём детали и поможем выбрать вашу поездку.'}</p><span className="intro-doodle" aria-hidden="true"><Plane size={26}/><span>а дальше — вместе</span></span></section>
          {intent && hasMessages && <section className="intent-bar" aria-label="Параметры поездки"><span className="intent-label"><Compass size={15}/> ВАШ ЗАПРОС</span>{intent.origin && <span><MapPin size={14}/>{intent.origin}{intent.destination ? ` → ${intent.destination}` : ''}</span>}{intent.flight_departure_from && <StayAndFlightDates intent={intent}/>}
          {!intent.flight_departure_from && intent.date_from && <span><Clock3 size={14}/>{date(intent.date_from)}{intent.date_to ? ` — ${date(intent.date_to)}` : ''}</span>}{!intent.flight_departure_from && intent.nights_min && <span>{intent.nights_min}{intent.nights_max && intent.nights_max !== intent.nights_min ? `–${intent.nights_max}` : ''} ночей</span>}{guests > 0 && <span>{intent.adults} взр.{intent.children_ages.length ? ` + ${intent.children_ages.length} дет.` : ''}</span>}{intent.budget_total != null && <span><Wallet size={14}/> до {money(intent.budget_total, intent.currency)}</span>}{!!intent.pets.length && <span><PawPrint size={14}/>{intent.pets.join(', ')}</span>}<span className="revision">версия {session?.revision}</span></section>}
          {Array.isArray(session?.preferences.applied_memory_fields) && session.preferences.applied_memory_fields.length > 0 && <p className="changes" role="status">Применены сохранённые предпочтения: {session.preferences.applied_memory_fields.map(field => ({ origin: "Город отправления", currency: "Валюта", climate_preferences: "Климат", trip_styles: "Формат отдыха", pets: "Питомцы", direct_flight_preferred: "Прямой рейс", max_travel_minutes: "Время в пути" } as Record<string,string>)[String(field)] || "Другое предпочтение").join(", ")}</p>}
          {session?.last_changes.length ? <div className="changes"><RotateCcw size={13}/><span>Обновили: {session.last_changes.join(' · ')}</span></div> : null}
          {session?.clarification && !busy && <div className="clarification"><MessageCircle size={20}/><div><strong>Уточним одну деталь</strong><p>{session.clarification}</p></div><button onClick={() => { setMobileChat(true); inputRef.current?.focus(); }} className="text-button">Ответить <ArrowRight size={14}/></button></div>}

          {!hasOptions && !busy && <>
            {!hasMessages && preferenceCopy && <div className="preference-banner"><Leaf size={19}/><div><strong>Знакомые предпочтения, новое путешествие</strong><p>{preferenceCopy}</p><span>Применим только после вашего выбора. Даты и бюджет задаются заново.</span></div><button className="text-button" onClick={() => { setInput(`Используй мои предпочтения: ${preferenceCopy}. `); setMobileChat(true); inputRef.current?.focus(); }}>Добавить к запросу <Plus size={14}/></button></div>}
            <section className="inspiration"><div className="section-heading"><h2>Всё начинается с «хочется»</h2><span>Например, вот так</span></div><div className="example-grid">{examples.map(({icon: Icon, title, text, color}) => <button className={`example-card ${color}`} key={title} onClick={() => { setInput(text); setMobileChat(true); inputRef.current?.focus(); }}><span className="example-icon"><Icon size={22}/></span><h3>{title}</h3><p>{text}</p><span className="example-link">Начать с этой идеи <ArrowUpRight size={16}/></span></button>)}</div></section>
            <section className="how-strip"><div><span className="step-number">01</span><div><h3>Расскажите о мечте</h3><p>Бюджет, даты, настроение. Как другу.</p></div></div><div><span className="step-number">02</span><div><h3>Сравните возможности</h3><p>Маршруты с источниками и компромиссами.</p></div></div><div><span className="step-number">03</span><div><h3>Меняйте планы на ходу</h3><p>Мы помним детали и пересобираем поездку.</p></div></div></section>
          </>}
          {(busy || !!latestEvents.length) && <section className={`activity ${visualBusy ? 'active' : ''}`}><div className="activity-heading">{visualBusy ? <LoaderCircle className="spin" size={15}/> : busy ? <RotateCcw size={15}/> : <CheckCircle2 size={15}/>}<strong>{busy ? currentWorkLabel : 'Как появился этот план'}</strong><span>Действия помощника</span></div><div className="event-list" aria-live="polite">{latestEvents.map(event => <div className={`event event-${event.status}`} key={event.id}><span className="event-dot"/><span>{event.message}</span>{event.status === 'completed' && <Check size={12}/>}</div>)}</div>{busy && !latestEvents.length && <p className="pending-note">{workKind === 'prices' ? priceActivity.detail : currentWorkLabel}</p>}</section>}
          {hasOptions && <section className="results"><div className="section-heading"><div><span className="eyebrow section-eyebrow">ПОДОБРАНО ПОД ВАС</span><h2>{session?.options.length === 1 ? 'Направление под ваш запрос' : `${session?.options.length} направления, разные впечатления`}</h2></div><div className="results-actions"><span className="results-caption">Сравните стоимость вариантов и сведения о поездке</span><PriceRefreshButton activity={priceActivity} disabled={busy || checkoutBusy || !connectionReady} onClick={() => void refreshPrices()} idleLabel="Проверить все цены"/></div></div><div className="options-grid">{session?.options.map((option, i) => <OptionCard key={option.id} title={optionTitle(option, session.options)} option={option} index={i} selected={selected?.id === option.id} onSelect={() => void select(option)} onRefresh={() => void refreshPrices(option.id)} activity={checkActivity(live.activityState, option.id)} disabled={busy || checkoutBusy || !connectionReady}/>)}</div></section>}
          {hasOptions && session && <DecisionWorkspace options={session.options} disabled={busy || checkoutBusy || !connectionReady} onAction={(action, ids) => void requestEvidence(action, ids)}/>}
          {session?.previous_plan && (busy || !hasOptions) && <PreviousPlan plan={session.previous_plan}/>}
          {selected && <section className="itinerary" aria-label="Выбранный маршрут"><div className="section-heading"><div><span className="eyebrow section-eyebrow">ПЛАН ПОЕЗДКИ</span><h2>Ваш маршрут: {selected.destination}</h2></div><span className="itinerary-stamp"><Compass size={15}/> Ваш маршрут</span></div>{selectedActivity && <CheckActivity option={selected} activity={selectedActivity} announce/>}<div className="itinerary-body"><div className="itinerary-parts"><ItineraryPart part={selected.flight} kind="flight" demo={demoComponent(selected, 'flight')}/><ItineraryPart part={selected.transfer} kind="transfer" demo={demoComponent(selected, 'transfer')}/><ItineraryPart part={selected.stay} kind="stay" demo={demoComponent(selected, 'stay')}/></div><aside className="checkout-summary"><span className="eyebrow">ЦЕНЫ ПОЕЗДКИ</span><PricingSummary option={selected}/><PriceRefreshButton activity={selectedActivity!} disabled={busy || checkoutBusy || !connectionReady} onClick={() => void refreshPrices(selected.id)} idleLabel="Проверить цены маршрута"/>{session && <DemoPayment session={session} option={selected} unavailable={busy || !connectionReady} onBusyChange={setCheckoutBusy} onReceiptCreated={() => setReceiptVersion(value => value + 1)}/>}</aside></div><RegionalEvents result={selected.regional_events} destination={selected.destination}/>{selected.risks.length > 0 && (demoComponent(selected, 'flight') ? <details className="price-context"><summary>Условия реальной поездки</summary><ul>{selected.risks.map((r,i) => <li key={i}>{r}</li>)}</ul></details> : <div className="risks"><AlertCircle size={16}/><div><strong>Что стоит учесть</strong><ul>{selected.risks.map((r,i) => <li key={i}>{r}</li>)}</ul></div></div>)}<SourceList evidence={selected.evidence}/></section>}
          {selected && <Constraints option={selected}/>}
          {session?.assumptions.length ? <details className="assumptions"><summary><CircleHelp size={14}/> Допущения в этом плане <ChevronDown size={13}/></summary><ul>{session.assumptions.map((a,i)=><li key={i}>{a}</li>)}</ul></details> : null}
          <footer className="workspace-footer"><span><Compass size={14}/> Один запрос. Много возможностей.</span><span>Ваши планы начинаются здесь</span></footer>
        </>}
      </div>
    </main>
    <button className="mobile-chat-toggle" onClick={() => setMobileChat(!mobileChat)}><MessageCircle size={19}/>{busy ? workKind === 'prices' ? priceActivity.label || 'Синхронизируем обновление цен…' : !connectionReady ? 'Восстанавливаем состояние…' : 'Помощник работает…' : 'Обсудить поездку'}</button>
    {showAbout && <div className="modal-overlay" onClick={() => setShowAbout(false)}><section className="modal" role="dialog" aria-modal="true" aria-labelledby="about-title" onClick={e=>e.stopPropagation()}><button className="modal-close icon-button" onClick={()=>setShowAbout(false)} aria-label="Закрыть"><X size={20}/></button><span className="modal-icon"><Compass size={28}/></span><h2 id="about-title">От желания<br/>к понятному плану.</h2><p>Помощник уточняет важное, ищет данные и сравнивает направления по вашим предпочтениям. Напишите новое условие — он сохранит контекст и обновит поездку.</p><p className="modal-note">Цены сопровождаются ссылкой на источник, датой и составом тарифа. Детали и доплаты можно раскрыть прямо в маршруте.</p><button className="primary-button" onClick={()=>setShowAbout(false)}>Давайте начнём <Send size={15}/></button></section></div>}
  </div>;
}
