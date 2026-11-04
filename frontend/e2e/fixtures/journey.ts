import type { Page } from '@playwright/test';
import type { DemoInvoice, DemoReceipt, Option, Session } from '../../src/types';

const time = '2026-10-28T10:00:00Z';
const component = (title: string) => ({ title, description: 'Демонстрационные данные. Условия и наличие неизвестны.', price: null, currency: 'EUR', source_url: null, status: 'unavailable' });
const option: Option = {
  id: 'fixture-option', destination: 'Пафос', country: 'Кипр', subtitle: 'Демонстрационные данные', score: 80,
  estimated_total: null, currency: 'EUR', price_status: 'unavailable',
  flight: component('Перелёт Варшава — Пафос'), stay: component('Проживание в Пафосе'), transfer: component('Трансфер'),
  weather: null, reasons: ['Демонстрационные данные'], tradeoffs: ['Наличие неизвестно'], risks: ['Реальные цены не проверены'], evidence: [], constraint_checks: [],
};
export const sessionFixture: Session = {
  id: 'fixture-trip', revision: 1,
  intent: { origin: 'Варшава', destination: 'Пафос', date_from: '2026-11-10', date_to: '2026-11-15', nights_min: 5, nights_max: 5, adults: 2, children_ages: [], pets: [], budget_total: null, currency: 'EUR', climate_preferences: [], trip_styles: [], max_travel_minutes: null, hotel_requirements: [] },
  messages: [{ id: 'fixture-message', role: 'assistant', content: 'Демонстрационные данные: маршрут для проверки интерфейса.', created_at: time }],
  options: [option], events: [], selected_option_id: null, assumptions: [], clarification: null, last_changes: [], preferences: {},
};
export const invoice: DemoInvoice = {
  quote_id: 'fixture-quote', context_id: 'fixture-context', session_id: sessionFixture.id, option_id: option.id, revision: 1,
  destination: 'Пафос', scope_label: '2 взрослых · 5 ночей', expires_at: '2099-01-01T00:00:00Z', pricing_basis: 'synthetic_demo', currency: 'EUR',
  line_items: [{ key: 'stay', label: 'Демо-проживание', detail: 'Вымышленная сумма', amount_minor: 10000 }],
  subtotal_minor: 10000, service_fee_minor: 500, service_fee_label: 'Демо-сбор', tax_label: 'Демо-налоги включены', total_minor: 10500,
  disclaimer: 'Все суммы вымышленные. Деньги не списываются.', unknown_real_conditions: ['Наличие и реальные цены неизвестны'], source_total: null,
};

/** Fixtures are test data only; every API is intercepted and external traffic blocked. */
export async function installJourney(page: Page, initial: Session = sessionFixture) {
  let session = structuredClone(initial);
  let sequence = 1;
  const requests: { path: string; body: unknown }[] = [];
  await page.addInitScript(() => {
    class FixtureEventSource extends EventTarget {
      readyState = 1;
      constructor(_url: string) {
        super();
        window.addEventListener('fixture-session', event => this.dispatchEvent(new MessageEvent('session', { data: (event as CustomEvent).detail })));
        void fetch('/api/fixture-snapshot').then(response => response.text()).then(data => this.dispatchEvent(new MessageEvent('session', { data })));
      }
      close() { this.readyState = 2; }
    }
    Object.defineProperty(window, 'EventSource', { value: FixtureEventSource });
  });
  const snapshot = () => ({ session, sequence, job: null, server_time: time });
  await page.route('**/*', async route => {
    const url = new URL(route.request().url());
    if (url.origin !== 'http://127.0.0.1:4179') return route.abort('blockedbyclient');
    if (!url.pathname.startsWith('/api/')) return route.continue();
    const path = url.pathname;
    let body: unknown;
    try { body = route.request().postDataJSON(); } catch { body = null; }
    requests.push({ path, body });
    let response: unknown;
    if (path === '/api/auth/guest') {
      await page.context().addCookies([{ name: 'one_request_csrf', value: 'fixture-csrf', url: 'http://127.0.0.1:4179', sameSite: 'Lax' }]);
      response = { user: { id: 'fixture-user', kind: 'guest' }, csrf_token: 'fixture-csrf' };
    } else if (path === '/api/auth/session') response = { user: { id: 'fixture-user', kind: 'guest' } };
    else if (path === '/api/auth/logout') response = { status: 'signed_out' };
    else if (route.request().method() === 'POST' && route.request().headers()['x-csrf-token'] !== 'fixture-csrf') return route.fulfill({ status: 403, json: { detail: 'Fixture rejected missing CSRF' } });
    else if (path === '/api/config') response = { llm_configured: false, providers: {} };
    else if (path === '/api/fixture-snapshot') response = snapshot();
    else if (path === '/api/sessions' || path === `/api/sessions/${session.id}`) response = session;
    else if (path.endsWith('/messages')) {
      const { content, action } = body as { content: string; action?: 'compare' | 'explain' };
      session = action ? { ...session, messages: [...session.messages, { id: `fixture-evidence-${sequence}`, role: 'assistant', content: action === 'compare' ? 'Сравнение по сохранённым источникам. Реальные цены и наличие неизвестны.' : 'Объяснение по сохранённым источникам. Условия требуют проверки.', created_at: time }] }
        : { ...session, revision: session.revision + 1, selected_option_id: null,
          messages: [...session.messages, { id: `fixture-user-${sequence}`, role: 'user', content, created_at: time }],
          last_changes: ['Демонстрационное уточнение'] };
      sequence++;
      response = { job_id: 'fixture-job', sequence };
      await page.evaluate(data => window.dispatchEvent(new CustomEvent('fixture-session', { detail: data })), JSON.stringify(snapshot()));
    } else if (path.endsWith('/select')) {
      session = { ...session, selected_option_id: option.id }; sequence++;
      response = session;
      await page.evaluate(data => window.dispatchEvent(new CustomEvent('fixture-session', { detail: data })), JSON.stringify(snapshot()));
    } else if (path.endsWith('/checkout/quote')) response = invoice;
    else if (path.endsWith('/checkout')) response = { id: 'fixture-receipt', message: 'Симуляция', created_at: time, status: 'simulated', ready_to_book: false, demo_payment: { status: 'simulated_paid', invoice, paid_amount_minor: invoice.total_minor, currency: invoice.currency, card_label: 'Вымышленная карта' } } satisfies DemoReceipt;
    else return route.fulfill({ status: 501, json: { detail: `Unexpected fixture endpoint: ${path}` } });
    await route.fulfill({ json: response });
  });
  return requests;
}
